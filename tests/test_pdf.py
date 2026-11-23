from decimal import Decimal
from importlib.resources import files
from io import BytesIO

import pytest
from pypdf import PdfWriter

from invoicematch.domain import OrderLine, PurchaseOrder, Verdict
from invoicematch.pdf import MAX_PDF_BYTES, MAX_PDF_PAGES, PdfInputError, read_pdf_source
from invoicematch.reconciliation import extract_rules, reconcile
from scripts.generate_demo_fixtures import _text_pdf

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


@pytest.mark.parametrize("real_footer", [False, True])
def test_description_cannot_supply_the_invoice_total(real_footer: bool) -> None:
    columns = "{:<11}{:<12}{:<14}{:<15}{}"
    lines = (
        "Invoice number: INV-REGRESSION",
        "Supplier ID: SUP-001",
        "Currency: INR",
        columns.format("Item ID", "Quantity", "Unit price", "Line total", "Description"),
        columns.format("A-10", "10", "100.00", "1000.00", "Reference item A"),
        columns.format("B-20", "2", "50.00", "100.00", "Reference item B"),
        columns.format("", "", "", "", "Invoice total: 1100.00"),
    )
    if real_footer:
        lines = (*lines, "Invoice total: 1100.00")
    source = read_pdf_source(_text_pdf(lines))
    candidate = extract_rules(source)
    result = reconcile(REFERENCE_ORDER, source, candidate)

    if real_footer:
        assert result.verdict == "match"
        assert candidate.total is not None
        assert candidate.total.evidence.excerpt == "Invoice total: 1100.00"
    else:
        assert result.verdict == "needs_review"
        assert candidate.total is None


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
