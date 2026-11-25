from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import DictRow, dict_row

Row = sqlite3.Row | DictRow
Cursor = sqlite3.Cursor | psycopg.Cursor[DictRow]


class Connection:
    def __init__(self, raw: sqlite3.Connection | psycopg.Connection[DictRow]) -> None:
        self.raw = raw

    @property
    def order_label(self) -> str:
        if isinstance(self.raw, sqlite3.Connection):
            return "json_extract(purchase_order, '$.order_id')"
        return "(purchase_order::jsonb ->> 'order_id')"

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> Cursor:
        if isinstance(self.raw, sqlite3.Connection):
            return self.raw.execute(sql, parameters)
        # The store uses static statements with ? placeholders and binds all input values.
        return self.raw.execute(sql.replace("?", "%s"), parameters or None)

    def count(self, sql: str, parameters: tuple[object, ...] = ()) -> int:
        row = self.execute(sql, parameters).fetchone()
        assert row is not None
        return int(row["total"])

    def insert_sequence(self, sql: str, parameters: tuple[object, ...]) -> int:
        if isinstance(self.raw, sqlite3.Connection):
            sequence = self.raw.execute(sql, parameters).lastrowid
            assert sequence is not None
            return sequence
        row = self.execute(f"{sql} RETURNING sequence", parameters).fetchone()
        assert row is not None
        return int(row["sequence"])


class Database:
    def __init__(self, target: Path | str) -> None:
        self.target = target

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        if isinstance(self.target, Path):
            with closing(
                sqlite3.connect(self.target, timeout=2.0, isolation_level=None)
            ) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA secure_delete = ON")
                yield Connection(connection)
        else:
            with closing(
                psycopg.connect(
                    self.target,
                    autocommit=True,
                    connect_timeout=5,
                    prepare_threshold=None,
                    row_factory=dict_row,
                )
            ) as pg_connection:
                yield Connection(pg_connection)

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[Connection]:
        with self.connect() as connection:
            raw = connection.raw
            if isinstance(raw, sqlite3.Connection):
                with raw:
                    raw.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                    yield connection
            else:
                with raw.transaction():
                    if write:
                        raw.execute("SET LOCAL lock_timeout = '2s'")
                        raw.execute("SET LOCAL statement_timeout = '5s'")
                        # Preserve SQLite's read-then-write guarantees across function instances.
                        raw.execute("SELECT pg_advisory_xact_lock(5064021, 1)")
                    else:
                        raw.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                        raw.execute("SET LOCAL statement_timeout = '5s'")
                    yield connection

    def initialize(self, sqlite_schema: str, postgres_schema: str) -> None:
        if isinstance(self.target, Path):
            self.target.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as connection:
                raw = connection.raw
                assert isinstance(raw, sqlite3.Connection)
                raw.execute("PRAGMA journal_mode = WAL")
                raw.executescript(sqlite_schema)
        else:
            with self.transaction(write=True) as connection:
                connection.execute(postgres_schema)
