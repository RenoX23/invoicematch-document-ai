from decimal import Decimal

import pytest
from pydantic import ValidationError

from invoicematch.domain import (
    DocumentEvidence,
    Evidence,
    HeaderName,
    HeaderValue,
    InvoiceExtraction,
    OrderLine,
    PurchaseOrder,
    SourceRow,
)
from invoicematch.reconciliation import extract_rules, reconcile


@pytest.fixture
def order() -> PurchaseOrder:
    return PurchaseOrder(
        order_id="PO-001",
        supplier_id="SUP-001",
        lines=(
            OrderLine(item_id="A-10", quantity=10, unit_price=Decimal("100.00")),
            OrderLine(item_id="B-20", quantity=2, unit_price=Decimal("50.00")),
        ),
    )


def header(name: HeaderName, value: str, line: int) -> HeaderValue:
    return HeaderValue(
        name=name,
        value=value,
        evidence=Evidence(page=1, line=line, excerpt=f"{name}: {value}"),
    )


def row(item: str, quantity: str, price: str, total: str, line: int) -> SourceRow:
    return SourceRow(
        item_id=item,
        quantity=quantity,
        unit_price=price,
        total=total,
        evidence=Evidence(page=1, line=line, excerpt=f"{item} {quantity} {price} {total}"),
    )


@pytest.fixture
def source() -> DocumentEvidence:
    return DocumentEvidence(
        headers=(
            header("invoice_number", "INV-DEMO-01", 2),
            header("supplier_id", "SUP-001", 3),
            header("currency", "INR", 4),
            header("total", "1100.00", 9),
        ),
        rows=(
            row("A-10", "10", "100.00", "1000.00", 6),
            row("B-20", "2", "50.00", "100.00", 7),
        ),
        complete_table=True,
    )


def test_match_keeps_grounded_values(order: PurchaseOrder, source: DocumentEvidence) -> None:
    extraction = extract_rules(source)
    decision = reconcile(order, source, extraction)

    assert decision.verdict == "match"
    assert decision.findings == ()
    assert extraction.lines[0].unit_price == Decimal("100.00")
    assert extraction.lines[0].evidence == source.rows[0].evidence


def test_unit_price_mismatch_has_reference_and_source(
    order: PurchaseOrder, source: DocumentEvidence
) -> None:
    changed = row("A-10", "10", "120.00", "1200.00", 6)
    source = DocumentEvidence(
        headers=(*source.headers[:3], header("total", "1300.00", 9)),
        rows=(changed, source.rows[1]),
        complete_table=True,
    )
    decision = reconcile(order, source, extract_rules(source))

    assert decision.verdict == "mismatch"
    assert len(decision.findings) == 1
    finding = decision.findings[0]
    assert (finding.code, finding.expected, finding.observed) == (
        "unit_price_mismatch",
        "100.00",
        "120.00",
    )
    assert finding.evidence == changed.evidence


def test_rejects_wrong_row_even_if_candidate_amount_would_match_order(
    order: PurchaseOrder, source: DocumentEvidence
) -> None:
    # A price copied from B-20 would make A-10 appear to match an altered order.
    modified_order = PurchaseOrder(
        order_id="PO-002",
        supplier_id="SUP-001",
        lines=(
            OrderLine(item_id="A-10", quantity=10, unit_price=Decimal("50.00")),
            order.lines[1],
        ),
    )
    extraction = extract_rules(source)
    lines = (
        extraction.lines[0].model_copy(update={"unit_price": Decimal("50.00")}),
        extraction.lines[1],
    )
    candidate = extraction.model_copy(update={"lines": lines})
    decision = reconcile(modified_order, source, candidate)

    assert decision.verdict == "needs_review"
    assert [finding.code for finding in decision.findings] == ["unverified_line"]
    assert decision.findings[0].evidence == source.rows[0].evidence


def test_balanced_grand_total_does_not_hide_incorrect_line_extensions(
    order: PurchaseOrder, source: DocumentEvidence
) -> None:
    source = DocumentEvidence(
        headers=source.headers,
        rows=(
            row("A-10", "10", "100.00", "1010.00", 6),
            row("B-20", "2", "50.00", "90.00", 7),
        ),
        complete_table=True,
    )
    decision = reconcile(order, source, extract_rules(source))

    assert decision.verdict == "mismatch"
    assert [finding.code for finding in decision.findings] == [
        "line_total_mismatch",
        "line_total_mismatch",
    ]


@pytest.mark.parametrize("complete", [True, False])
def test_missing_order_line_only_mismatches_after_complete_table_verification(
    order: PurchaseOrder, source: DocumentEvidence, complete: bool
) -> None:
    source = DocumentEvidence(
        headers=(*source.headers[:3], header("total", "1000.00", 9)),
        rows=source.rows[:1],
        complete_table=complete,
    )
    decision = reconcile(order, source, extract_rules(source))

    assert decision.verdict == ("mismatch" if complete else "needs_review")
    assert decision.findings[0].code == ("missing_order_line" if complete else "incomplete_table")


@pytest.mark.parametrize(
    ("replacement", "code"),
    [
        (header("invoice_number", "", 2), "unverified_header"),
        (header("currency", "USD", 4), "unsupported_currency"),
        (header("total", "1100.001", 9), "unverified_header"),
    ],
)
def test_invalid_or_unsupported_header_needs_review(
    order: PurchaseOrder, source: DocumentEvidence, replacement: HeaderValue, code: str
) -> None:
    source = source.model_copy(
        update={
            "headers": tuple(
                replacement if value.name == replacement.name else value for value in source.headers
            )
        }
    )
    decision = reconcile(order, source, extract_rules(source))

    assert decision.verdict == "needs_review"
    assert [finding.code for finding in decision.findings] == [code]


def test_missing_source_row_in_candidate_cannot_pass(
    order: PurchaseOrder, source: DocumentEvidence
) -> None:
    extraction = extract_rules(source)
    candidate = extraction.model_copy(update={"lines": extraction.lines[:1]})
    decision = reconcile(order, source, candidate)

    assert decision.verdict == "needs_review"
    assert decision.findings[0].evidence == source.rows[1].evidence


def test_conflicting_headers_preserve_both_source_references(
    order: PurchaseOrder, source: DocumentEvidence
) -> None:
    conflict = header("invoice_number", "INV-OTHER", 5)
    source = source.model_copy(update={"headers": (*source.headers, conflict)})
    decision = reconcile(order, source, extract_rules(source))

    assert decision.verdict == "needs_review"
    assert [finding.evidence for finding in decision.findings] == [
        source.headers[0].evidence,
        conflict.evidence,
    ]


def test_repeated_item_id_requires_review(order: PurchaseOrder, source: DocumentEvidence) -> None:
    source = source.model_copy(
        update={"rows": (*source.rows, row("A-10", "1", "100.00", "100.00", 8))}
    )
    decision = reconcile(order, source, extract_rules(source))

    assert decision.verdict == "needs_review"
    assert {finding.code for finding in decision.findings} == {"repeated_line_id"}


def test_empty_candidate_requires_review(order: PurchaseOrder, source: DocumentEvidence) -> None:
    decision = reconcile(order, source, InvoiceExtraction())

    assert decision.verdict == "needs_review"
    assert {finding.code for finding in decision.findings} == {
        "unverified_header",
        "unverified_line",
    }


@pytest.mark.parametrize("amount", [1.2, "-1.00", "1.234", "1e2", "NaN", "Inf"])
def test_order_rejects_inexact_or_invalid_amount(amount: object) -> None:
    with pytest.raises(ValidationError, match="unit_price"):
        OrderLine.model_validate({"item_id": "A-10", "quantity": 10, "unit_price": amount})
