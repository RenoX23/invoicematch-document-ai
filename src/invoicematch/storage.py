from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
from time import time
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, StringConstraints

from invoicematch.domain import (
    Contract,
    DocumentEvidence,
    Finding,
    InvoiceExtraction,
    PurchaseOrder,
    Reconciliation,
    ReviewMapping,
    Verdict,
)
from invoicematch.reconciliation import reconcile, validate_mapping

SESSION_SECONDS = 24 * 60 * 60
MAX_SESSIONS = 2000
MAX_ORDERS = 20
MAX_REVIEWS = 40
LIVE_PER_SESSION = 6
LIVE_PER_MINUTE = 6
LIVE_PER_DAY = 50

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
CREATE TABLE IF NOT EXISTS decision_context (
    sequence INTEGER PRIMARY KEY REFERENCES decision_events(sequence) ON DELETE CASCADE,
    verdict TEXT NOT NULL,
    mapping_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS session_expiry (
    session_key TEXT PRIMARY KEY REFERENCES sessions(session_key) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS session_expiry_deadline ON session_expiry(expires_at);
CREATE TABLE IF NOT EXISTS saved_orders (
    session_key TEXT NOT NULL REFERENCES sessions(session_key) ON DELETE CASCADE,
    order_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (session_key, order_id)
);
CREATE TABLE IF NOT EXISTS review_inputs (
    review_id TEXT PRIMARY KEY REFERENCES reviews(review_id) ON DELETE CASCADE,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mapping_events (
    sequence INTEGER PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES reviews(review_id) ON DELETE CASCADE,
    mapping TEXT NOT NULL,
    result TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS mappings_review ON mapping_events(review_id, sequence);
CREATE TABLE IF NOT EXISTS usage_buckets (
    scope TEXT NOT NULL,
    bucket INTEGER NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (scope, bucket)
);
CREATE TABLE IF NOT EXISTS live_reservations (
    session_key TEXT NOT NULL REFERENCES sessions(session_key) ON DELETE CASCADE,
    pdf_hash TEXT NOT NULL,
    order_id TEXT NOT NULL,
    lease TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (session_key, pdf_hash, order_id)
);
"""

Decision = Literal["accept", "reject", "hold"]
ReviewMode = Literal["rules", "replay", "live"]


class StoreProblem(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class OrderInput(Contract):
    order: PurchaseOrder
    expected_version: Annotated[int, Field(strict=True, ge=1)] | None = None


class SavedOrder(Contract):
    order: PurchaseOrder
    version: int


class MappingEvent(Contract):
    mapping: ReviewMapping
    result: Reconciliation
    recorded_at: datetime


class DecisionInput(Contract):
    decision: Decision
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=400)] | None = None


class DecisionEvent(DecisionInput):
    recorded_at: datetime
    verdict: Verdict | None = None
    mapping_revision: int | None = None


class Review(Contract):
    review_id: str
    mode: ReviewMode
    order: PurchaseOrder
    extraction: InvoiceExtraction
    result: Reconciliation
    created_at: datetime
    decisions: tuple[DecisionEvent, ...]
    order_version: int | None = None
    mappings: tuple[MappingEvent, ...] = ()


class ReviewStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=2.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA secure_delete = ON")
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
            connection.execute(
                "INSERT OR IGNORE INTO session_expiry SELECT session_key, ? FROM sessions",
                (int(time()) + SESSION_SECONDS,),
            )

    @staticmethod
    def _active(connection: sqlite3.Connection, session: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM session_expiry WHERE session_key = ? AND expires_at > ?",
                (session, int(time())),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _require_active(connection: sqlite3.Connection, session: str) -> None:
        if not ReviewStore._active(connection, session):
            raise StoreProblem(
                401, "session_expired", "Open the reviewer page to start a new session."
            )

    @staticmethod
    def _delete(connection: sqlite3.Connection, session: str) -> None:
        connection.execute(
            "DELETE FROM decision_events WHERE review_id IN (SELECT review_id FROM reviews WHERE session_key = ?)",
            (session,),
        )
        connection.execute("DELETE FROM reviews WHERE session_key = ?", (session,))
        connection.execute("DELETE FROM sessions WHERE session_key = ?", (session,))
        connection.execute(
            "DELETE FROM usage_buckets WHERE scope IN (?, ?)",
            (f"uploads:{session}", f"live:{session}"),
        )

    def delete_session(self, session: str) -> None:
        with self._write() as connection:
            self._delete(connection, session)

    def purge_expired(self) -> None:
        now = int(time())
        with self._write() as connection:
            expired = connection.execute(
                "SELECT session_key FROM session_expiry WHERE expires_at <= ?", (now,)
            ).fetchall()
            for row in expired:
                self._delete(connection, row["session_key"])
            connection.execute(
                "DELETE FROM usage_buckets WHERE bucket < ?", (now - 2 * SESSION_SECONDS,)
            )
            connection.execute("DELETE FROM live_reservations WHERE expires_at <= ?", (now,))

    def create_session(self) -> str:
        token = token_hex(32)
        key = sha256(token.encode("ascii")).hexdigest()
        with self._write() as connection:
            self._consume(connection, "new-sessions", 60, 60)
            if connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= MAX_SESSIONS:
                raise StoreProblem(
                    429, "session_capacity", "The reviewer is busy. Try again later."
                )
            connection.execute("INSERT INTO sessions (session_key) VALUES (?)", (key,))
            connection.execute(
                "INSERT INTO session_expiry VALUES (?, ?)", (key, int(time()) + SESSION_SECONDS)
            )
        return token

    def session_key(self, token: str | None) -> str | None:
        if token is None or re.fullmatch(r"[a-f0-9]{64}", token) is None:
            return None
        key = sha256(token.encode("ascii")).hexdigest()
        with closing(self._connect()) as connection:
            return key if self._active(connection, key) else None

    def orders(self, session: str) -> tuple[SavedOrder, ...]:
        with closing(self._connect()) as connection:
            self._require_active(connection, session)
            rows = connection.execute(
                "SELECT version, payload FROM saved_orders WHERE session_key = ? ORDER BY order_id",
                (session,),
            )
            return tuple(
                SavedOrder(
                    order=PurchaseOrder.model_validate_json(row["payload"]), version=row["version"]
                )
                for row in rows
            )

    def get_order(self, session: str, order_id: str) -> SavedOrder | None:
        with closing(self._connect()) as connection:
            self._require_active(connection, session)
            row = connection.execute(
                "SELECT version, payload FROM saved_orders WHERE session_key = ? AND order_id = ?",
                (session, order_id),
            ).fetchone()
            if row is None:
                return None
            return SavedOrder(
                order=PurchaseOrder.model_validate_json(row["payload"]), version=row["version"]
            )

    def save_order(self, session: str, incoming: OrderInput) -> SavedOrder:
        order = incoming.order
        with self._write() as connection:
            self._require_active(connection, session)
            key = (session, order.order_id)
            row = connection.execute(
                "SELECT version FROM saved_orders WHERE session_key = ? AND order_id = ?", key
            ).fetchone()
            current = row["version"] if row is not None else None
            if current != incoming.expected_version:
                raise StoreProblem(
                    409,
                    "order_version_conflict",
                    "Reload the PO list and select the current version.",
                )
            if (
                current is None
                and connection.execute(
                    "SELECT COUNT(*) FROM saved_orders WHERE session_key = ?", (session,)
                ).fetchone()[0]
                >= MAX_ORDERS
            ):
                raise StoreProblem(
                    429, "order_capacity", "This review session has reached its PO limit."
                )
            new_version = (current or 0) + 1
            connection.execute(
                "INSERT INTO saved_orders VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_key, order_id) DO UPDATE SET version=excluded.version, payload=excluded.payload",
                (*key, new_version, order.model_dump_json()),
            )
            return SavedOrder(order=order, version=new_version)

    @staticmethod
    def _consume(connection: sqlite3.Connection, scope: str, window: int, limit: int) -> None:
        now = int(time())
        bucket = now - now % window
        count = connection.execute(
            "INSERT INTO usage_buckets VALUES (?, ?, 1) "
            "ON CONFLICT(scope, bucket) DO UPDATE SET count=count+1 WHERE count < ?",
            (scope, bucket, limit),
        ).rowcount
        if not count:
            raise StoreProblem(
                429, "usage_limit", "The request limit for this mode has been reached."
            )

    def admit_upload(self, session: str) -> None:
        with self._write() as connection:
            self._require_active(connection, session)
            self._consume(connection, "uploads", 60, 120)
            self._consume(connection, f"uploads:{session}", 60, 30)

    def reserve_live(self, session: str, pdf_hash: str, order_id: str) -> str | Review:
        with self._write() as connection:
            self._require_active(connection, session)
            existing = self._exact(connection, session, pdf_hash, order_id, "live")
            if existing is not None:
                return self._read(connection, existing)
            self._review_limit(connection, session)
            now = int(time())
            lease = uuid4().hex
            reserved = connection.execute(
                "INSERT INTO live_reservations VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_key, pdf_hash, order_id) DO UPDATE SET "
                "lease=excluded.lease, expires_at=excluded.expires_at WHERE expires_at <= ?",
                (session, pdf_hash, order_id, lease, now + 60, now),
            ).rowcount
            if not reserved:
                raise StoreProblem(
                    409, "review_processing", "This live review is already processing."
                )
            self._consume(connection, "live-minute", 60, LIVE_PER_MINUTE)
            self._consume(connection, "live-day", SESSION_SECONDS, LIVE_PER_DAY)
            self._consume(connection, f"live:{session}", SESSION_SECONDS, LIVE_PER_SESSION)
            return lease

    def release_live(self, lease: str) -> None:
        with self._write() as connection:
            connection.execute("DELETE FROM live_reservations WHERE lease = ?", (lease,))

    @staticmethod
    def _review_limit(connection: sqlite3.Connection, session: str) -> None:
        if (
            connection.execute(
                "SELECT COUNT(*) FROM reviews WHERE session_key = ?", (session,)
            ).fetchone()[0]
            >= MAX_REVIEWS
        ):
            raise StoreProblem(
                429, "review_capacity", "This review session has reached its review limit."
            )

    @staticmethod
    def _read(connection: sqlite3.Connection, row: sqlite3.Row) -> Review:
        decisions = connection.execute(
            "SELECT decision, note, recorded_at, verdict, mapping_revision FROM decision_events "
            "LEFT JOIN decision_context USING (sequence) WHERE review_id = ? ORDER BY sequence",
            (row["review_id"],),
        )
        mappings = tuple(
            MappingEvent(
                mapping=ReviewMapping.model_validate_json(event["mapping"]),
                result=Reconciliation.model_validate_json(event["result"]),
                recorded_at=datetime.fromisoformat(event["recorded_at"]),
            )
            for event in connection.execute(
                "SELECT mapping, result, recorded_at FROM mapping_events WHERE review_id = ? ORDER BY sequence",
                (row["review_id"],),
            )
        )
        key: str = row["order_id"]
        return Review(
            review_id=row["review_id"],
            mode=row["mode"],
            order=PurchaseOrder.model_validate_json(row["purchase_order"]),
            extraction=InvoiceExtraction.model_validate_json(row["extraction"]),
            result=mappings[-1].result
            if mappings
            else Reconciliation.model_validate_json(row["result"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            decisions=tuple(DecisionEvent.model_validate(dict(event)) for event in decisions),
            order_version=int(key.rsplit(":", 1)[1]) if key.startswith("custom:") else None,
            mappings=mappings,
        )

    def get(self, session: str, review_id: str) -> Review | None:
        with closing(self._connect()) as connection:
            self._require_active(connection, session)
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
        *,
        order_version: int | None = None,
        source: DocumentEvidence | None = None,
        grounded_identity: InvoiceExtraction | None = None,
    ) -> tuple[Review, bool]:
        """Persist a prepared review and return whether this call created it."""
        identity = grounded_identity if grounded_identity is not None else extraction
        supplier = (
            " ".join(identity.supplier_id.value.casefold().split())
            if identity.supplier_id
            else None
        )
        invoice = identity.invoice_number.value if identity.invoice_number else None
        order_key = (
            f"custom:{order.order_id}:{order_version}"
            if order_version is not None
            else order.order_id
        )
        with self._write() as connection:
            self._require_active(connection, session)
            existing = self._exact(connection, session, pdf_hash, order_key, mode)
            if existing is not None:
                return self._read(connection, existing), False

            self._review_limit(connection, session)
            conflicts: list[Finding] = []
            other_order = connection.execute(
                "SELECT json_extract(purchase_order, '$.order_id') AS order_label FROM reviews "
                "WHERE session_key = ? AND pdf_hash = ? AND order_label <> ? "
                "ORDER BY created_at, review_id LIMIT 1",
                (session, pdf_hash, order.order_id),
            ).fetchone()
            if other_order is not None:
                conflicts.append(
                    Finding(
                        code="conflicting_order_selection",
                        field="order_id",
                        expected=other_order["order_label"],
                        observed=order.order_id,
                    )
                )
            same_identity = connection.execute(
                "SELECT 1 FROM reviews WHERE session_key = ? AND supplier_id = ? COLLATE NOCASE "
                "AND invoice_number = ? AND pdf_hash <> ? LIMIT 1",
                (session, supplier, invoice, pdf_hash),
            ).fetchone()
            if same_identity is not None:
                conflicts.append(
                    Finding(
                        code="possible_duplicate",
                        field="invoice_number",
                        observed=invoice,
                        evidence=identity.invoice_number.evidence
                        if identity.invoice_number
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
                order_version=order_version,
            )
            connection.execute(
                "INSERT INTO reviews (review_id, session_key, pdf_hash, order_id, mode, supplier_id, "
                "invoice_number, purchase_order, extraction, result, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review.review_id,
                    session,
                    pdf_hash,
                    order_key,
                    mode,
                    supplier,
                    invoice,
                    order.model_dump_json(),
                    extraction.model_dump_json(),
                    result.model_dump_json(),
                    review.created_at.isoformat(),
                ),
            )
            if source is not None:
                connection.execute(
                    "INSERT INTO review_inputs VALUES (?, ?)",
                    (review.review_id, source.model_dump_json()),
                )
            return review, True

    def map_review(self, session: str, review_id: str, mapping: ReviewMapping) -> Review | None:
        with self._write() as connection:
            self._require_active(connection, session)
            row = connection.execute(
                "SELECT * FROM reviews WHERE session_key = ? AND review_id = ?",
                (session, review_id),
            ).fetchone()
            if row is None:
                return None
            source_row = connection.execute(
                "SELECT source FROM review_inputs WHERE review_id = ?", (review_id,)
            ).fetchone()
            if source_row is None:
                raise StoreProblem(
                    422, "mapping_unavailable", "Mappings are available for custom reviews."
                )
            review = self._read(connection, row)
            if review.mappings and review.mappings[-1].mapping == mapping:
                return review
            if len(review.mappings) >= 20:
                raise StoreProblem(
                    429, "mapping_capacity", "This review has reached its mapping limit."
                )
            try:
                validate_mapping(review.order, review.extraction, mapping)
            except ValueError as error:
                raise StoreProblem(422, "invalid_mapping", str(error)) from None
            source = DocumentEvidence.model_validate_json(source_row["source"])
            result = reconcile(review.order, source, review.extraction, mapping)
            originals = Reconciliation.model_validate_json(row["result"]).findings
            conflicts = tuple(
                finding
                for finding in originals
                if finding.code in {"possible_duplicate", "conflicting_order_selection"}
            )
            if conflicts:
                result = Reconciliation(
                    verdict="needs_review", findings=(*conflicts, *result.findings)
                )
            connection.execute(
                "INSERT INTO mapping_events (review_id, mapping, result, recorded_at) VALUES (?, ?, ?, ?)",
                (
                    review_id,
                    mapping.model_dump_json(),
                    result.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            return self._read(connection, row)

    def append_decision(
        self, session: str, review_id: str, decision: DecisionInput
    ) -> Review | None:
        with self._write() as connection:
            self._require_active(connection, session)
            row = connection.execute(
                "SELECT * FROM reviews WHERE session_key = ? AND review_id = ?",
                (session, review_id),
            ).fetchone()
            if row is None:
                return None
            if (
                connection.execute(
                    "SELECT COUNT(*) FROM decision_events WHERE review_id = ?", (review_id,)
                ).fetchone()[0]
                >= 100
            ):
                raise StoreProblem(
                    429, "decision_capacity", "This review has reached its decision limit."
                )
            review = self._read(connection, row)
            event = connection.execute(
                "INSERT INTO decision_events (review_id, decision, note, recorded_at) VALUES (?, ?, ?, ?)",
                (review_id, decision.decision, decision.note, datetime.now(UTC).isoformat()),
            )
            connection.execute(
                "INSERT INTO decision_context VALUES (?, ?, ?)",
                (event.lastrowid, review.result.verdict, len(review.mappings)),
            )
            return self._read(connection, row)
