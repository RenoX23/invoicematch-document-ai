from decimal import Decimal
from importlib.resources import files
from io import BytesIO

import pytest
from pypdf import PdfWriter

from invoicematch.domain import OrderLine, PurchaseOrder, Verdict
from invoicematch.pdf import MAX_PDF_BYTES, MAX_PDF_PAGES, PdfInputError, read_pdf_source
from invoicematch.reconciliation import extract_rules, reconcile

REFERENCE_ORDER = PurchaseOrder(
    order_id="PO-001",
    supplier_id="SUP-001",
    lines=(
        OrderLine(item_id="A-10", quantity=10, unit_price=Decimal("100.00")),
        OrderLine(item_id="B-20", quantity=2, unit_price=Decimal("50.00")),
    ),
)


@pytest.mark.parametrize(
    ("filename", "verdict"),
    [
        ("clean-match.pdf", "match"),
        ("price-mismatch.pdf", "mismatch"),
        ("ambiguous-extraction.pdf", "needs_review"),
    ],
)
def test_bundled_pdfs_through_the_normal_extractor(filename: str, verdict: Verdict) -> None:
    source = read_pdf_source(files("invoicematch").joinpath("samples", filename).read_bytes())
    extraction = extract_rules(source)
    result = reconcile(REFERENCE_ORDER, source, extraction)

    assert result.verdict == verdict
    assert extraction.invoice_number is not None
    assert extraction.invoice_number.evidence.page == 1


def _blank_pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_valid_pdf_with_no_readable_text_routes_to_review() -> None:
    source = read_pdf_source(_blank_pdf(1))

    assert not source.complete_table
    assert reconcile(REFERENCE_ORDER, source, extract_rules(source)).verdict == "needs_review"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"not a PDF", "invalid_pdf"),
        (b"%PDF-1.4\ninvalid content", "invalid_pdf"),
        (b"%PDF-1.4\n" + b"x" * MAX_PDF_BYTES, "pdf_too_large"),
    ],
    ids=["wrong-signature", "corrupt-document", "oversized-document"],
)
def test_rejects_malformed_or_oversized_pdf(data: bytes, code: str) -> None:
    with pytest.raises(PdfInputError) as error:
        read_pdf_source(data)

    assert error.value.code == code


def test_pdf_page_limit() -> None:
    with pytest.raises(PdfInputError, match="eight pages"):
        read_pdf_source(_blank_pdf(MAX_PDF_PAGES + 1))
