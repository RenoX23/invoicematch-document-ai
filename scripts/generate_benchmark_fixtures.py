"""Regenerate the versioned Phase 2 synthetic PDFs and gold labels."""

import json
from dataclasses import dataclass, replace
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from scripts.generate_demo_fixtures import _text_pdf

OUTPUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "phase2"
COMPACT = ("item_id", "quantity", "total", "unit_price", "description")
WRAPPED = ("item_id", "description", "total", "unit_price", "quantity")
LABELS = {
    "item_id": "Item ID",
    "quantity": "Quantity",
    "unit_price": "Unit price",
    "total": "Line total",
    "description": "Description",
}
WIDTHS = {"item_id": 10, "quantity": 10, "unit_price": 12, "total": 12, "description": 25}


@dataclass(frozen=True)
class Item:
    item_id: str
    quantity: int
    unit_price: str

    @property
    def total(self) -> str:
        return format(Decimal(self.unit_price) * self.quantity, ".2f")

    def labels(self) -> dict[str, str | int]:
        return {
            "item_id": self.item_id,
            "quantity": self.quantity,
            "unit_price": self.unit_price,
            "total": self.total,
        }


A = Item("A-10", 10, "100.00")
B = Item("B-20", 2, "50.00")


@dataclass(frozen=True)
class Case:
    case_id: str
    expected: str
    items: tuple[Item, ...] = (A, B)
    supplier_id: str = "SUP-001"
    printed_total: str | None = None
    note: str | None = None
    distractor: bool = False
    split_pages: bool = False

    @property
    def total(self) -> str:
        return self.printed_total or format(sum(Decimal(item.total) for item in self.items), ".2f")


CASES = (
    Case("H01", "match"),
    Case("H02", "match", (B, A)),
    Case("H03", "match", distractor=True),
    Case("H04", "mismatch", (replace(A, unit_price="120.00"), B)),
    Case("H05", "mismatch", (replace(A, quantity=9), B)),
    Case("H06", "mismatch", (A,)),
    Case("H07", "match"),
    Case("H08", "match", note="Note: Reference amount INR 125.00."),
    Case("H09", "match", split_pages=True),
    Case("H10", "mismatch", (replace(A, unit_price="105.00"), replace(B, unit_price="25.00"))),
    Case("H11", "mismatch", supplier_id="SUP-002"),
    Case("H12", "mismatch", printed_total="1150.00"),
)


def _line(columns: tuple[str, ...], values: dict[str, str]) -> str:
    return "".join(values.get(name, "").ljust(WIDTHS[name]) for name in columns).rstrip()


def _rows(case: Case, items: tuple[Item, ...], columns: tuple[str, ...]) -> tuple[str, ...]:
    lines = [_line(columns, LABELS)]
    for item in items:
        cells = {name: str(value) for name, value in item.labels().items()}
        cells["description"] = "Reference supply " + item.item_id
        if case.distractor and item.item_id == A.item_id:
            cells["description"] = "Related B-20 price 50.00"
        lines.append(_line(columns, cells))
        if columns == WRAPPED:
            lines.append(_line(columns, {"description": "Packaged for dispatch"}))
    return tuple(lines)


def _document(case: Case, columns: tuple[str, ...]) -> bytes:
    header = (
        f"Invoice number: {case.case_id}",
        f"Supplier ID: {case.supplier_id}",
        "Currency: INR",
        "",
    )
    footer = (f"Invoice total: {case.total}",)
    if case.note:
        footer = (*footer, case.note)
    if not case.split_pages:
        return _text_pdf((*header, *_rows(case, case.items, columns), *footer))

    writer = PdfWriter()
    writer.add_page(
        PdfReader(BytesIO(_text_pdf((*header, *_rows(case, case.items[:1], columns))))).pages[0]
    )
    writer.add_page(
        PdfReader(BytesIO(_text_pdf((*_rows(case, case.items[1:], columns), *footer)))).pages[0]
    )
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    labels = []
    for case in CASES:
        columns = COMPACT if case.case_id <= "H06" else WRAPPED
        pdf = _document(case, columns)
        (OUTPUT / f"{case.case_id}.pdf").write_bytes(pdf)
        labels.append(
            {
                "case_id": case.case_id,
                "layout": "compact" if columns == COMPACT else "wrapped",
                "sha256": sha256(pdf).hexdigest(),
                "expected": case.expected,
                "invoice_number": case.case_id,
                "supplier_id": case.supplier_id,
                "currency": "INR",
                "total": case.total,
                "lines": [item.labels() for item in case.items],
            }
        )
    manifest = {"dataset": "phase2-heldout-v1", "cases": labels}
    (OUTPUT / "labels.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


if __name__ == "__main__":
    main()
