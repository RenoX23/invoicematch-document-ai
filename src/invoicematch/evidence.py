from __future__ import annotations

import re
from collections.abc import Sequence

from invoicematch.domain import (
    ADJUSTMENTS,
    DocumentEvidence,
    Evidence,
    HeaderName,
    HeaderValue,
    SourceRow,
)

_HEADER_FIELDS: dict[str, HeaderName] = {
    "invoice number": "invoice_number",
    "invoice no": "invoice_number",
    "invoice #": "invoice_number",
    "supplier id": "supplier_id",
    "supplier": "supplier_id",
    "vendor": "supplier_id",
    "vendor id": "supplier_id",
    "currency": "currency",
    "invoice total": "total",
    "grand total": "total",
    "subtotal": "subtotal",
    "sub total": "subtotal",
    "taxable value": "subtotal",
    "tax": "tax",
    "tax amount": "tax",
    "gst": "tax",
    "total gst": "tax",
    "cgst": "cgst",
    "sgst": "sgst",
    "igst": "igst",
    "discount": "discount",
    "freight": "freight",
    "shipping": "freight",
}
_REQUIRED_COLUMNS = frozenset({"item id", "quantity", "unit price", "line total"})
_COLUMN_ALIASES = {
    "sku": "item id",
    "item code": "item id",
    "product code": "item id",
    "qty": "quantity",
    "rate": "unit price",
    "unit rate": "unit price",
    "amount": "line total",
    "line amount": "line total",
    "particulars": "description",
}
_COLUMN_TOKEN = re.compile(r"\S+(?: \S+)*")
_NON_TABLE_LINE = re.compile(r"^(?:synthetic invoice|invoice|page [0-9]+(?: of [0-9]+)?)$", re.I)
_MAX_EXCERPT_LENGTH = 500

Columns = tuple[tuple[str, int], ...]


def _table_columns(line: str) -> Columns | None:
    tokens = (
        (token.group().casefold().rstrip("."), token.start())
        for token in _COLUMN_TOKEN.finditer(line)
    )
    columns = tuple((_COLUMN_ALIASES.get(name, name), start) for name, start in tokens)
    names = {name for name, _ in columns}
    if len(names) != len(columns) or names - {"description"} != _REQUIRED_COLUMNS:
        return None
    return columns


def _header_field(span: Evidence) -> HeaderValue | None:
    label, separator, value = span.excerpt.partition(":")
    name = _HEADER_FIELDS.get(label.strip().casefold().rstrip("."))
    if separator and name is not None:
        return HeaderValue(name=name, value=value.strip(), evidence=span)
    return None


def _cells(line: str, columns: Columns) -> dict[str, str]:
    # Use the heading offsets to keep a missing cell from shifting later values left.
    stops = [start for _, start in columns[1:]] + [len(line)]
    return {
        name: line[start:stop].strip() for (name, start), stop in zip(columns, stops, strict=True)
    }


def source_from_pages(pages: Sequence[str]) -> DocumentEvidence:
    """Locate source headers and table cells in position-preserving PDF page text."""
    headers: list[HeaderValue] = []
    rows: list[SourceRow] = []
    table_seen = False
    table_closed = False
    summary_started = False
    structure_is_uncertain = False

    for page_number, page in enumerate(pages, 1):
        columns: Columns | None = None
        previous_item_on_page = False
        lines = page.splitlines()
        page_left = min(
            (len(line) - len(line.lstrip()) for line in lines if line.strip()), default=0
        )
        for line_number, line in enumerate(lines, 1):
            excerpt = line.strip()
            if not excerpt:
                continue
            if len(excerpt) > _MAX_EXCERPT_LENGTH:
                structure_is_uncertain = True
                continue

            span = Evidence(page=page_number, line=line_number, excerpt=excerpt)
            new_columns = _table_columns(line)
            if new_columns is not None:
                if summary_started or columns is not None:
                    structure_is_uncertain = True
                columns = new_columns
                table_seen = True
                continue
            cells = _cells(line, columns) if columns is not None else None
            if (
                cells is not None
                and previous_item_on_page
                and cells.get("description")
                and not any(cells[name] for name in _REQUIRED_COLUMNS)
            ):
                continue

            # A label inside a row can look like a header after stripping whitespace.
            field = _header_field(span) if len(line) - len(line.lstrip()) == page_left else None
            if field is not None:
                if field.name in ADJUSTMENTS or field.name == "total":
                    headers.append(field)
                    if columns is not None:
                        columns = None
                        summary_started = True
                    if not summary_started or table_closed:
                        structure_is_uncertain = True
                    if field.name == "total" and summary_started:
                        table_closed = True
                elif table_seen:
                    structure_is_uncertain = True
                else:
                    headers.append(field)
                continue

            if columns is None or cells is None:
                if table_seen and not table_closed and not _NON_TABLE_LINE.fullmatch(excerpt):
                    structure_is_uncertain = True
                if table_closed and not (
                    excerpt.casefold().startswith("note:") or _NON_TABLE_LINE.fullmatch(excerpt)
                ):
                    structure_is_uncertain = True
                continue

            if line[: columns[0][1]].strip():
                structure_is_uncertain = True
            rows.append(
                SourceRow(
                    item_id=cells["item id"],
                    quantity=cells["quantity"],
                    unit_price=cells["unit price"],
                    total=cells["line total"],
                    evidence=span,
                )
            )
            previous_item_on_page = bool(cells["item id"])

    return DocumentEvidence(
        headers=tuple(headers),
        rows=tuple(rows),
        complete_table=table_seen and table_closed and not structure_is_uncertain,
    )
