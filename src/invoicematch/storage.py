from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import StringConstraints

from invoicematch.domain import Contract, Finding, InvoiceExtraction, PurchaseOrder, Reconciliation

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_key TEXT NOT NULL PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT NOT NULL PRIMARY KEY,
    session_key TEXT NOT NULL REFERENCES sessions(session_key),
    pdf_hash TEXT NOT NULL,
    order_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    supplier_id TEXT,
    invoice_number TEXT,
    purchase_order TEXT NOT NULL,
    extraction TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_key, pdf_hash, order_id, mode)
);
CREATE INDEX IF NOT EXISTS reviews_identity
    ON reviews(session_key, supplier_id, invoice_number);
CREATE TABLE IF NOT EXISTS decision_events (
    sequence INTEGER PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES reviews(review_id),
    decision TEXT NOT NULL,
    note TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_review
    ON decision_events(review_id, sequence);
"""

Decision = Literal["accept", "reject", "hold"]
ReviewMode = Literal["rules"]


class DecisionInput(Contract):
    decision: Decision
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=400)] | None = None


class DecisionEvent(DecisionInput):
    recorded_at: datetime


class Review(Contract):
    review_id: str
    mode: ReviewMode
    order: PurchaseOrder
    extraction: InvoiceExtraction
    result: Reconciliation
    created_at: datetime
    decisions: tuple[DecisionEvent, ...]


class ReviewStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=2.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as connection:
            # Acquire the write lock before reads that determine the next write.
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                yield connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)

    def create_session(self) -> str:
        token = token_hex(32)
        key = sha256(token.encode("ascii")).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("INSERT INTO sessions (session_key) VALUES (?)", (key,))
        return token

    def session_key(self, token: str | None) -> str | None:
        if token is None or re.fullmatch(r"[a-f0-9]{64}", token) is None:
            return None
        key = sha256(token.encode("ascii")).hexdigest()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM sessions WHERE session_key = ?", (key,)
            ).fetchone()
        return key if row is not None else None

    @staticmethod
    def _read(connection: sqlite3.Connection, row: sqlite3.Row) -> Review:
        decisions = connection.execute(
            "SELECT decision, note, recorded_at FROM decision_events "
            "WHERE review_id = ? ORDER BY sequence",
            (row["review_id"],),
        )
        return Review(
            review_id=row["review_id"],
            mode=row["mode"],
            order=PurchaseOrder.model_validate_json(row["purchase_order"]),
            extraction=InvoiceExtraction.model_validate_json(row["extraction"]),
            result=Reconciliation.model_validate_json(row["result"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            decisions=tuple(DecisionEvent.model_validate(dict(event)) for event in decisions),
        )

    def get(self, session: str, review_id: str) -> Review | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM reviews WHERE session_key = ? AND review_id = ?",
                (session, review_id),
            ).fetchone()
            return self._read(connection, row) if row is not None else None

    @staticmethod
    def _exact(
        connection: sqlite3.Connection, session: str, pdf_hash: str, order_id: str, mode: ReviewMode
    ) -> sqlite3.Row | None:
        row: sqlite3.Row | None = connection.execute(
            "SELECT * FROM reviews WHERE session_key = ? AND pdf_hash = ? AND order_id = ? AND mode = ?",
            (session, pdf_hash, order_id, mode),
        ).fetchone()
        return row

    def find_exact(
        self, session: str, pdf_hash: str, order_id: str, mode: ReviewMode
    ) -> Review | None:
        with closing(self._connect()) as connection:
            row = self._exact(connection, session, pdf_hash, order_id, mode)
            return self._read(connection, row) if row is not None else None

    def add_review(
        self,
        session: str,
        pdf_hash: str,
        order: PurchaseOrder,
        mode: ReviewMode,
        extraction: InvoiceExtraction,
        result: Reconciliation,
    ) -> tuple[Review, bool]:
        """Persist a prepared review and return whether this call created it."""
        supplier = extraction.supplier_id.value if extraction.supplier_id else None
        invoice = extraction.invoice_number.value if extraction.invoice_number else None
        with self._write() as connection:
            existing = self._exact(connection, session, pdf_hash, order.order_id, mode)
            if existing is not None:
                return self._read(connection, existing), False

            conflicts: list[Finding] = []
            other_order = connection.execute(
                "SELECT order_id FROM reviews WHERE session_key = ? AND pdf_hash = ? "
                "AND order_id <> ? ORDER BY created_at, review_id LIMIT 1",
                (session, pdf_hash, order.order_id),
            ).fetchone()
            if other_order is not None:
                conflicts.append(
                    Finding(
                        code="conflicting_order_selection",
                        field="order_id",
                        expected=other_order["order_id"],
                        observed=order.order_id,
                    )
                )
            same_identity = connection.execute(
                "SELECT 1 FROM reviews WHERE session_key = ? AND supplier_id = ? "
                "AND invoice_number = ? AND pdf_hash <> ? LIMIT 1",
                (session, supplier, invoice, pdf_hash),
            ).fetchone()
            if same_identity is not None:
                conflicts.append(
                    Finding(
                        code="possible_duplicate",
                        field="invoice_number",
                        observed=invoice,
                        evidence=extraction.invoice_number.evidence
                        if extraction.invoice_number
                        else None,
                    )
                )
            if conflicts:
                result = Reconciliation(
                    verdict="needs_review", findings=(*conflicts, *result.findings)
                )

            review = Review(
                review_id=uuid4().hex,
                mode=mode,
                order=order,
                extraction=extraction,
                result=result,
                created_at=datetime.now(UTC),
                decisions=(),
            )
            connection.execute(
                "INSERT INTO reviews (review_id, session_key, pdf_hash, order_id, mode, supplier_id, "
                "invoice_number, purchase_order, extraction, result, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review.review_id,
                    session,
                    pdf_hash,
                    order.order_id,
                    mode,
                    supplier,
                    invoice,
                    order.model_dump_json(),
                    extraction.model_dump_json(),
                    result.model_dump_json(),
                    review.created_at.isoformat(),
                ),
            )
            return review, True

    def append_decision(
        self, session: str, review_id: str, decision: DecisionInput
    ) -> Review | None:
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM reviews WHERE session_key = ? AND review_id = ?",
                (session, review_id),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "INSERT INTO decision_events (review_id, decision, note, recorded_at) VALUES (?, ?, ?, ?)",
                (review_id, decision.decision, decision.note, datetime.now(UTC).isoformat()),
            )
            return self._read(connection, row)
