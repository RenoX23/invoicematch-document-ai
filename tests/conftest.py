from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo


@pytest.fixture
def postgres_database() -> Iterator[str]:
    url = os.getenv("INVOICEMATCH_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set INVOICEMATCH_TEST_POSTGRES_URL to a disposable PostgreSQL test database.")
    schema = "test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as connection:
        identifier = sql.Identifier(schema)
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(identifier))
        try:
            yield make_conninfo(url, options=f"-csearch_path={schema}")
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(identifier))


@pytest.fixture(params=["sqlite", "postgres"])
def database(request: pytest.FixtureRequest, tmp_path: Path) -> Path | str:
    if request.param == "sqlite":
        return tmp_path / "reviews.sqlite3"
    url: str = request.getfixturevalue("postgres_database")
    return url
