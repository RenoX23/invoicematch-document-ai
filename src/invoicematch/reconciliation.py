from __future__ import annotations

import re
from collections import Counter
from decimal import Context, Decimal, localcontext
from typing import TypeVar

from pydantic import ValidationError

from invoicematch.domain import (
    ADJUSTMENTS,
    Claim,
    Currency,
    DocumentEvidence,
    Finding,
    HeaderName,
    Identifier,
    InvoiceExtraction,
    InvoiceLine,
    Money,
    PurchaseOrder,
    Reconciliation,
    ReviewMapping,
    SourceRow,
    Supplier,
)

T = TypeVar("T")


def _source_claim(
    document: DocumentEvidence, name: HeaderName, claim_type: type[Claim[T]]
) -> Claim[T] | None:
    fields = [field for field in document.headers if field.name == name]
    if len(fields) != 1:
        return None
    field = fields[0]
    try:
        return claim_type.model_validate({"value": field.value, "evidence": field.evidence})
    except ValidationError:
        return None


def _source_line(row: SourceRow) -> InvoiceLine | None:
    if not re.fullmatch(r"[0-9]{1,7}", row.quantity):
        return None
    try:
        return InvoiceLine.model_validate(
            {
                "item_id": row.item_id,
                "quantity": int(row.quantity),
                "unit_price": row.unit_price,
                "total": row.total,
                "evidence": row.evidence,
            }
        )
    except ValidationError:
        return None


def extract_rules(document: DocumentEvidence) -> InvoiceExtraction:
    lines = tuple(line for row in document.rows if (line := _source_line(row)) is not None)
    return InvoiceExtraction(
        invoice_number=_source_claim(document, "invoice_number", Claim[Identifier]),
        supplier_id=_source_claim(document, "supplier_id", Claim[Supplier]),
        currency=_source_claim(document, "currency", Claim[Currency]),
        total=_source_claim(document, "total", Claim[Money]),
        lines=lines,
        adjustments={
            name: claim
            for name in ADJUSTMENTS
            if (claim := _source_claim(document, name, Claim[Money])) is not None
        },
    )


def _uncertain_evidence(document: DocumentEvidence, candidate: InvoiceExtraction) -> list[Finding]:
    currency_source = _source_claim(document, "currency", Claim[Currency])
    grounded_headers = (
        (
            "invoice_number",
            candidate.invoice_number,
            _source_claim(document, "invoice_number", Claim[Identifier]),
        ),
        (
            "supplier_id",
            candidate.supplier_id,
            _source_claim(document, "supplier_id", Claim[Supplier]),
        ),
        ("currency", candidate.currency, currency_source),
        ("total", candidate.total, _source_claim(document, "total", Claim[Money])),
    )
    findings: list[Finding] = []
    adjustments = (
        (name, candidate.adjustments.get(name), _source_claim(document, name, Claim[Money]))
        for name in ADJUSTMENTS
        if name in candidate.adjustments or any(field.name == name for field in document.headers)
    )
    for field, claimed, source in (*grounded_headers, *adjustments):
        if source is not None and claimed == source:
            continue
        source_fields = [header for header in document.headers if header.name == field]
        if source_fields:
            findings.extend(
                Finding(
                    code="unverified_header",
                    field=field,
                    observed=header.value,
                    evidence=header.evidence,
                )
                for header in source_fields
            )
        else:
            findings.append(Finding(code="unverified_header", field=field))
    if (
        currency_source is not None
        and currency_source == candidate.currency
        and currency_source.value != "INR"
    ):
        findings.append(
            Finding(
                code="unsupported_currency",
                field="currency",
                expected="INR",
                observed=currency_source.value,
                evidence=currency_source.evidence,
            )
        )

    if not document.complete_table or not document.rows:
        findings.append(Finding(code="incomplete_table", field="lines"))
    keys = candidate.adjustments.keys()
    has_split_tax = bool(keys & {"cgst", "sgst"})
    if (
        (keys & {"tax", "igst"} and has_split_tax)
        or {"tax", "igst"} <= keys
        or (has_split_tax and not {"cgst", "sgst"} <= keys)
    ):
        findings.append(Finding(code="ambiguous_tax_components", field="tax"))

    source_rows = {row.evidence: row for row in document.rows}
    claimed_rows = Counter(line.evidence for line in candidate.lines)
    for evidence, row in source_rows.items():
        if claimed_rows[evidence] != 1:
            findings.append(
                Finding(
                    code="unverified_line",
                    field="lines",
                    item_id=row.item_id,
                    evidence=evidence,
                )
            )
    for line in candidate.lines:
        source_row = source_rows.get(line.evidence)
        if source_row is None or _source_line(source_row) != line:
            findings.append(
                Finding(
                    code="unverified_line",
                    field="lines",
                    item_id=line.item_id,
                    evidence=source_row.evidence if source_row else None,
                )
            )
    if len({(row.evidence.page, row.evidence.line) for row in document.rows}) != len(document.rows):
        findings.append(Finding(code="overlapping_source_rows", field="lines"))

    item_ids = Counter(line.item_id for line in candidate.lines)
    for line in candidate.lines:
        if item_ids[line.item_id] > 1:
            findings.append(
                Finding(
                    code="repeated_line_id",
                    field="item_id",
                    item_id=line.item_id,
                    evidence=line.evidence if line.evidence in source_rows else None,
                )
            )
    return findings


def validate_mapping(
    order: PurchaseOrder, candidate: InvoiceExtraction, mapping: ReviewMapping
) -> None:
    invoice_ids = {line.item_id for line in candidate.lines}
    order_ids = {line.item_id for line in order.lines}
    chosen = {mapping.items.get(item, item) for item in invoice_ids}
    if (
        mapping.items.keys() - invoice_ids
        or set(mapping.items.values()) - order_ids
        or len(chosen) != len(invoice_ids)
    ):
        raise ValueError("Use existing invoice and PO items and map each PO item at most once.")
    if mapping.confirm_supplier and candidate.supplier_id is None:
        raise ValueError("A supplier must be present in the invoice evidence.")


def reconcile(
    order: PurchaseOrder,
    document: DocumentEvidence,
    candidate: InvoiceExtraction,
    mapping: ReviewMapping | None = None,
) -> Reconciliation:
    uncertainty = _uncertain_evidence(document, candidate)
    if uncertainty:
        return Reconciliation(verdict="needs_review", findings=tuple(uncertainty))

    # These required claims were verified against unique source fields above.
    assert candidate.supplier_id is not None
    assert candidate.total is not None
    findings: list[Finding] = []
    if (
        candidate.supplier_id.value.casefold().split() != order.supplier_id.casefold().split()
        and not (mapping and mapping.confirm_supplier)
    ):
        findings.append(
            Finding(
                code="supplier_mismatch",
                field="supplier_id",
                expected=order.supplier_id,
                observed=candidate.supplier_id.value,
                evidence=candidate.supplier_id.evidence,
            )
        )

    order_lines = {line.item_id: line for line in order.lines}
    item_map = mapping.items if mapping else {}
    invoice_ids = {item_map.get(line.item_id, line.item_id) for line in candidate.lines}
    for item_id in order_lines:
        if item_id not in invoice_ids:
            findings.append(
                Finding(
                    code="missing_order_line",
                    field="item_id",
                    item_id=item_id,
                    expected=item_id,
                )
            )

    with localcontext(Context(prec=32)):
        computed_total = Decimal(0)
        for line in candidate.lines:
            expected_line = order_lines.get(item_map.get(line.item_id, line.item_id))
            if expected_line is None:
                findings.append(
                    Finding(
                        code="unexpected_invoice_line",
                        field="item_id",
                        item_id=line.item_id,
                        observed=line.item_id,
                        evidence=line.evidence,
                    )
                )
            else:
                for field in ("quantity", "unit_price"):
                    expected = getattr(expected_line, field)
                    observed = getattr(line, field)
                    if expected != observed:
                        findings.append(
                            Finding(
                                code=f"{field}_mismatch",
                                field=field,
                                item_id=line.item_id,
                                expected=str(expected),
                                observed=str(observed),
                                evidence=line.evidence,
                            )
                        )
            computed_line_total = line.quantity * line.unit_price
            computed_total += computed_line_total
            if computed_line_total != line.total:
                findings.append(
                    Finding(
                        code="line_total_mismatch",
                        field="line_total",
                        item_id=line.item_id,
                        expected=str(computed_line_total),
                        observed=str(line.total),
                        evidence=line.evidence,
                    )
                )

        adjustments = candidate.adjustments
        subtotal = adjustments.get("subtotal")
        if subtotal and subtotal.value != computed_total:
            findings.append(
                Finding(
                    code="invoice_subtotal_mismatch",
                    field="subtotal",
                    expected=str(computed_total),
                    observed=str(subtotal.value),
                    evidence=subtotal.evidence,
                )
            )
        for name, claim in adjustments.items():
            if name != "subtotal":
                computed_total += -claim.value if name == "discount" else claim.value

    if computed_total != candidate.total.value:
        findings.append(
            Finding(
                code="invoice_total_mismatch",
                field="total",
                expected=str(computed_total),
                observed=str(candidate.total.value),
                evidence=candidate.total.evidence,
            )
        )
    return Reconciliation(verdict="mismatch" if findings else "match", findings=tuple(findings))
