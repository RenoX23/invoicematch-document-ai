from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated, Generic, Literal, Self, TypeVar

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


def parse_money(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        amount = re.sub(r"^(?:INR|₹)\s*", "", value.strip())
        if re.fullmatch(
            r"(?:[0-9]{1,12}|[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,2}(?:,[0-9]{2})+,[0-9]{3})(?:\.[0-9]{1,2})?",
            amount,
        ):
            return Decimal(amount.replace(",", ""))
    raise ValueError("Use an INR amount with up to two fractional digits.")


Money = Annotated[
    Decimal,
    BeforeValidator(parse_money),
    Field(ge=0, max_digits=14, decimal_places=2, allow_inf_nan=False),
]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9_.-]{0,63}$")]
Supplier = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[^\x00-\x1f\x7f]{1,120}$")
]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Quantity = Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
AdjustmentName = Literal["subtotal", "tax", "cgst", "sgst", "igst", "discount", "freight"]
ADJUSTMENTS: tuple[AdjustmentName, ...] = (
    "subtotal",
    "tax",
    "cgst",
    "sgst",
    "igst",
    "discount",
    "freight",
)
HeaderName = Literal["invoice_number", "supplier_id", "currency", "total"] | AdjustmentName
Verdict = Literal["match", "mismatch", "needs_review"]
T = TypeVar("T")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Evidence(Contract):
    page: Annotated[int, Field(ge=1)]
    line: Annotated[int, Field(ge=1)]
    excerpt: Annotated[str, StringConstraints(min_length=1, max_length=500)]


class HeaderValue(Contract):
    name: HeaderName
    value: str
    evidence: Evidence


class SourceRow(Contract):
    item_id: str
    quantity: str
    unit_price: str
    total: str
    evidence: Evidence


class DocumentEvidence(Contract):
    headers: tuple[HeaderValue, ...] = ()
    rows: tuple[SourceRow, ...] = ()
    complete_table: bool = False


class Claim(Contract, Generic[T]):
    value: T
    evidence: Evidence


class InvoiceLine(Contract):
    item_id: Identifier
    quantity: Quantity
    unit_price: Money
    total: Money
    evidence: Evidence


class InvoiceExtraction(Contract):
    invoice_number: Claim[Identifier] | None = None
    supplier_id: Claim[Supplier] | None = None
    currency: Claim[Currency] | None = None
    total: Claim[Money] | None = None
    lines: tuple[InvoiceLine, ...] = ()
    adjustments: dict[AdjustmentName, Claim[Money]] = Field(default_factory=dict)


class OrderLine(Contract):
    item_id: Identifier
    quantity: Quantity
    unit_price: Money


class PurchaseOrder(Contract):
    order_id: Identifier
    supplier_id: Supplier
    currency: Literal["INR"] = "INR"
    lines: Annotated[tuple[OrderLine, ...], Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def unique_line_ids(self) -> Self:
        if len({line.item_id for line in self.lines}) != len(self.lines):
            raise ValueError("Purchase-order line identifiers must be unique.")
        return self


class ReviewMapping(Contract):
    confirm_supplier: bool = False
    items: Annotated[dict[Identifier, Identifier], Field(max_length=500)] = Field(
        default_factory=dict
    )


class Finding(Contract):
    code: str
    field: str
    item_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    evidence: Evidence | None = None


class Reconciliation(Contract):
    verdict: Verdict
    findings: tuple[Finding, ...] = ()
