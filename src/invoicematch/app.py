from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from invoicematch.domain import OrderLine, PurchaseOrder
from invoicematch.pdf import MAX_PDF_BYTES, PdfInputError, read_pdf_source, validate_pdf_header
from invoicematch.reconciliation import extract_rules, reconcile
from invoicematch.storage import DecisionInput, Review, ReviewStore

_SESSION_COOKIE = "invoicematch_session"
_SAMPLE_NAMES = ("clean-match.pdf", "price-mismatch.pdf", "ambiguous-extraction.pdf")
_ORDERS = {
    "PO-001": PurchaseOrder(
        order_id="PO-001",
        supplier_id="SUP-001",
        lines=(
            OrderLine(item_id="A-10", quantity=10, unit_price=Decimal("100.00")),
            OrderLine(item_id="B-20", quantity=2, unit_price=Decimal("50.00")),
        ),
    ),
    "PO-002": PurchaseOrder(
        order_id="PO-002",
        supplier_id="SUP-002",
        lines=(OrderLine(item_id="A-10", quantity=5, unit_price=Decimal("85.00")),),
    ),
}


def _problem(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


async def _limited_body(request: Request, limit: int) -> bytes:
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    raise _problem(413, "body_too_large", "The request exceeds the allowed size.")
                body.extend(chunk)
    except TimeoutError as error:
        raise _problem(408, "upload_timeout", "The upload took too long.") from error
    return bytes(body)


def _require_media_type(request: Request, expected: str) -> None:
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != expected:
        raise _problem(415, "unsupported_media_type", f"Use {expected} for this request.")


def create_app(
    database_path: Path | None = None,
    *,
    additional_approved_hashes: frozenset[str] = frozenset(),
) -> FastAPI:
    path = database_path or Path(os.environ.get("INVOICEMATCH_DB_PATH", "instance/reviews.sqlite3"))
    store = ReviewStore(path)
    resources = files("invoicematch")
    html = resources.joinpath("web", "index.html").read_text(encoding="utf-8")
    script = resources.joinpath("web", "review.js").read_text(encoding="utf-8")
    samples = {name: resources.joinpath("samples", name).read_bytes() for name in _SAMPLE_NAMES}
    approved_hashes = {sha256(data).hexdigest() for data in samples.values()} | (
        additional_approved_hashes
    )
    secure_cookie = os.environ.get("INVOICEMATCH_SECURE_COOKIE") == "1"
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await run_in_threadpool(store.initialize)
        yield

    app = FastAPI(title="InvoiceMatch", lifespan=lifespan, docs_url=None, redoc_url=None)

    def require_session(request: Request) -> str:
        key = store.session_key(request.cookies.get(_SESSION_COOKIE))
        if key is None:
            raise _problem(401, "session_required", "Open the reviewer page to start a session.")
        return key

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error(_: Request, error: sqlite3.OperationalError) -> JSONResponse:
        code = getattr(error, "sqlite_errorcode", 0) & 0xFF
        if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise error
        return JSONResponse(
            {"detail": {"code": "store_busy", "message": "The review store is busy. Try again."}},
            status_code=503,
            headers={"Retry-After": "2", **headers},
        )

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def reviewer(request: Request) -> HTMLResponse:
        response = HTMLResponse(
            html,
            headers={
                **headers,
                "Content-Security-Policy": (
                    "default-src 'self'; object-src 'none'; base-uri 'none'; "
                    "frame-ancestors 'none'; form-action 'self'"
                ),
            },
        )
        if store.session_key(request.cookies.get(_SESSION_COOKIE)) is None:
            response.set_cookie(
                _SESSION_COOKIE,
                store.create_session(),
                httponly=True,
                secure=secure_cookie,
                samesite="strict",
            )
        return response

    @app.get("/review.js", include_in_schema=False)
    def reviewer_script() -> Response:
        return Response(script, media_type="text/javascript", headers=headers)

    @app.get("/samples/{filename}", include_in_schema=False)
    def download_sample(filename: str) -> Response:
        data = samples.get(filename)
        if data is None:
            raise _problem(404, "sample_not_found", "Choose a listed synthetic sample.")
        return Response(
            data,
            media_type="application/pdf",
            headers={**headers, "Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def submit_review(session: str, order: PurchaseOrder, data: bytes) -> tuple[Review, bool]:
        try:
            validate_pdf_header(data)
            pdf_hash = sha256(data).hexdigest()
            if pdf_hash not in approved_hashes:
                raise _problem(422, "unapproved_pdf", "Use one of the approved synthetic invoices.")
            existing = store.find_exact(session, pdf_hash, order.order_id, "rules")
            if existing is not None:
                return existing, False
            source = read_pdf_source(data)
        except PdfInputError as error:
            status = 413 if error.code == "pdf_too_large" else 422
            raise _problem(status, error.code, str(error)) from error

        extraction = extract_rules(source)
        result = reconcile(order, source, extraction)
        return store.add_review(session, pdf_hash, order, "rules", extraction, result)

    @app.post("/api/reviews", status_code=201)
    async def upload(
        request: Request,
        response: Response,
        order_id: Annotated[str, Query(max_length=64)],
    ) -> Review:
        _require_media_type(request, "application/pdf")
        session = await run_in_threadpool(require_session, request)
        order = _ORDERS.get(order_id)
        if order is None:
            raise _problem(404, "unknown_order", "Choose a seeded purchase order.")
        data = await _limited_body(request, MAX_PDF_BYTES)
        review, created = await run_in_threadpool(submit_review, session, order, data)
        if not created:
            response.status_code = 200
        response.headers.update(headers)
        return review

    @app.get("/api/reviews/{review_id}")
    def get_review(review_id: str, request: Request, response: Response) -> Review:
        review = store.get(require_session(request), review_id)
        if review is None:
            raise _problem(404, "review_not_found", "This review is unavailable in your session.")
        response.headers.update(headers)
        return review

    @app.post("/api/reviews/{review_id}/decisions", status_code=201)
    async def save_decision(review_id: str, request: Request, response: Response) -> Review:
        _require_media_type(request, "application/json")
        session = await run_in_threadpool(require_session, request)
        body = await _limited_body(request, 4096)
        try:
            decision = DecisionInput.model_validate_json(body)
        except ValidationError as error:
            raise _problem(
                422, "invalid_decision", "Select a decision and use up to 400 note characters."
            ) from error
        review = await run_in_threadpool(store.append_decision, session, review_id, decision)
        if review is None:
            raise _problem(404, "review_not_found", "This review is unavailable in your session.")
        response.headers.update(headers)
        return review

    return app
