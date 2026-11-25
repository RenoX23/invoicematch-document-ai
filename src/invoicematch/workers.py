from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import pydantic
import pydantic_core
import pypdf
from pydantic import TypeAdapter

from invoicematch.model import Invocation, invoke_groq
from invoicematch.pdf import MAX_CUSTOM_PDF_BYTES, PdfInputError, read_pdf_pages

PDF_DEADLINE = 6
LIVE_DEADLINE = 12
_MAX_OUTPUT = 512 * 1024
_MEMORY_LIMIT = 256 * 1024 * 1024
_PAGES = TypeAdapter(tuple[str, ...])
_BOOTSTRAP = (
    "import json, runpy, sys;"
    "sys.path[:0] = json.loads(sys.argv.pop(1));"
    "runpy.run_module('invoicematch.workers', run_name='__main__')"
)


def _worker_import_roots() -> tuple[str, ...]:
    # Vercel inserts vendored dependencies into the parent interpreter's search path.
    # Resolve only known installed packages so the isolated worker still ignores PYTHONPATH.
    paths = (__file__, pydantic.__file__, pydantic_core.__file__, pypdf.__file__)
    return tuple(dict.fromkeys(str(Path(path).resolve().parents[1]) for path in paths if path))


def _worker(mode: Literal["pdf", "live"], payload: bytes, timeout: float) -> bytes:
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    with TemporaryDirectory(prefix="invoicematch-") as working_dir:
        run = subprocess.run(
            [sys.executable, "-I", "-c", _BOOTSTRAP, json.dumps(_worker_import_roots()), mode],
            input=payload,
            capture_output=True,
            cwd=working_dir,
            env=env,
            timeout=timeout,
            check=True,
        )
    if len(run.stdout) > _MAX_OUTPUT:
        raise ValueError("Worker produced too much output.")
    return run.stdout


def parse_isolated(data: bytes) -> tuple[str, ...]:
    try:
        result = json.loads(_worker("pdf", data, PDF_DEADLINE))
        if isinstance(result, dict):
            code = result.get("error")
            if code in {"invalid_pdf", "encrypted_pdf", "pdf_too_large", "pdf_resource_limit"}:
                raise PdfInputError(
                    code, "The PDF could not be processed within the supported limits."
                )
        return _PAGES.validate_python(result)
    except PdfInputError:
        raise
    except subprocess.TimeoutExpired:
        raise PdfInputError("pdf_timeout", "The PDF took too long to read.") from None
    except (OSError, ValueError, subprocess.CalledProcessError):
        raise PdfInputError("pdf_worker_failed", "The PDF parser could not complete.") from None


def model_isolated(pages: tuple[str, ...], api_key: str) -> Invocation:
    try:
        output = _worker(
            "live", json.dumps({"pages": pages, "key": api_key}).encode(), LIVE_DEADLINE
        )
        return Invocation.model_validate_json(output)
    except subprocess.TimeoutExpired:
        return Invocation(claims=None, error="provider_timeout")
    except (OSError, ValueError, subprocess.CalledProcessError):
        return Invocation(claims=None, error="provider_worker_failed")


def _read_pdf_in_child() -> bytes:
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (_MEMORY_LIMIT, _MEMORY_LIMIT))
        resource.setrlimit(resource.RLIMIT_CPU, (4, 5))
    try:
        pages = read_pdf_pages(
            sys.stdin.buffer.read(MAX_CUSTOM_PDF_BYTES + 1), max_bytes=MAX_CUSTOM_PDF_BYTES
        )
        return _PAGES.dump_json(pages)
    except PdfInputError as error:
        return json.dumps({"error": error.code}).encode()
    except MemoryError:
        return b'{"error":"pdf_resource_limit"}'


def _main() -> None:
    if sys.argv[1:] == ["pdf"]:
        result = _read_pdf_in_child()
    elif sys.argv[1:] == ["live"]:
        payload = json.loads(sys.stdin.buffer.read(_MAX_OUTPUT))
        result = invoke_groq(tuple(payload["pages"]), payload["key"]).model_dump_json().encode()
    else:
        raise SystemExit(2)
    sys.stdout.buffer.write(result)


if __name__ == "__main__":
    _main()
