from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from invoicematch.domain import DocumentEvidence
from invoicematch.evidence import source_from_pages

MAX_PDF_BYTES = 256 * 1024
MAX_CUSTOM_PDF_BYTES = 2 * 1024 * 1024
MAX_PDF_PAGES = 8
MAX_LAYOUT_CHARACTERS = 50_000


class PdfInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def validate_pdf_header(data: bytes, *, max_bytes: int = MAX_PDF_BYTES) -> None:
    if len(data) > max_bytes:
        raise PdfInputError("pdf_too_large", "The invoice PDF exceeds the upload limit.")
    if not data.startswith(b"%PDF-"):
        raise PdfInputError("invalid_pdf", "Upload a valid PDF invoice.")


def read_pdf_source(data: bytes) -> DocumentEvidence:
    """Read positioned text and find invoice evidence in an approved sample."""
    return source_from_pages(read_pdf_pages(data))


def read_pdf_pages(data: bytes, *, max_bytes: int = MAX_PDF_BYTES) -> tuple[str, ...]:
    """Read bounded, position-preserving text from each PDF page."""
    validate_pdf_header(data, max_bytes=max_bytes)
    pages: list[str] = []
    characters = 0
    try:
        with BytesIO(data) as stream:
            reader = PdfReader(stream, strict=True)
            if reader.is_encrypted:
                raise PdfInputError("encrypted_pdf", "Encrypted invoices cannot be read.")
            if len(reader.pages) > MAX_PDF_PAGES:
                raise PdfInputError(
                    "pdf_too_large", "An invoice PDF can contain up to eight pages."
                )
            for page in reader.pages:
                text = page.extract_text(
                    extraction_mode="layout",
                    layout_mode_space_vertically=False,
                )
                characters += len(text)
                if characters > MAX_LAYOUT_CHARACTERS:
                    raise PdfInputError("pdf_too_large", "The invoice PDF contains too much text.")
                pages.append(text)
    except PdfInputError:
        raise
    except (PyPdfError, OSError, ValueError, RecursionError) as error:
        raise PdfInputError("invalid_pdf", "The invoice PDF could not be decoded.") from error
    return tuple(pages)
