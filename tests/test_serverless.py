from __future__ import annotations

import asyncio
import importlib
from importlib.resources import files
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from invoicematch.database import Database
from invoicematch.storage import ReviewStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_vercel_entrypoint_uses_postgres_without_startup_migrations(
    postgres_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", postgres_database)
    monkeypatch.setenv("CRON_SECRET", "test-cleanup-authorization")
    monkeypatch.delenv("INVOICEMATCH_SECURE_COOKIE", raising=False)
    entrypoint = importlib.reload(importlib.import_module("asgi"))
    async with AsyncClient(
        transport=ASGITransport(app=entrypoint.app), base_url="https://reviewer.test"
    ) as client:
        assert (await client.get("/health")).status_code == 503
        ReviewStore(postgres_database).initialize()
        assert (await client.get("/health")).json() == {"status": "ok"}
        assert "Secure" in (await client.get("/")).headers["set-cookie"]
        data = files("invoicematch").joinpath("samples", "clean-match.pdf").read_bytes()
        review = await client.post(
            "/api/reviews?mode=replay&order_id=PO-001",
            content=data,
            headers={"Content-Type": "application/pdf"},
        )
        assert review.status_code == 201
        route = f"/api/reviews/{review.json()['review_id']}/decisions"
        assert (await client.post(route, json={"decision": "hold"})).status_code == 201

        with Database(postgres_database).transaction(write=True) as connection:
            connection.execute("UPDATE session_expiry SET expires_at = 0")
        assert (await client.get("/api/orders")).status_code == 401
        for authorization in ({}, {"Authorization": "Bearer wrong"}):
            assert (
                await client.get("/api/maintenance/purge", headers=authorization)
            ).status_code == 401
        with Database(postgres_database).transaction() as connection:
            assert connection.count("SELECT COUNT(*) AS total FROM decision_events") == 1

        for _ in range(2):
            response = await client.get(
                "/api/maintenance/purge",
                headers={"Authorization": "Bearer test-cleanup-authorization"},
            )
            assert response.status_code == 204
        with Database(postgres_database).transaction() as connection:
            assert connection.count("SELECT COUNT(*) AS total FROM sessions") == 0
            assert connection.count("SELECT COUNT(*) AS total FROM decision_events") == 0


async def test_concurrent_purchase_order_updates_on_separate_instances(
    database: Path | str,
) -> None:
    from invoicematch.app import create_app

    args = {"database_path": database} if isinstance(database, Path) else {"database_url": database}
    ReviewStore(database).initialize()
    async with (
        AsyncClient(
            transport=ASGITransport(app=create_app(**args)), base_url="http://test"
        ) as first,
        AsyncClient(
            transport=ASGITransport(app=create_app(**args)), base_url="http://test"
        ) as second,
    ):
        await first.get("/")
        second.cookies.update(first.cookies)
        order = {
            "order_id": "TEST-ORDER",
            "supplier_id": "Acme Parts",
            "currency": "INR",
            "lines": [{"item_id": "PART-7", "quantity": 1, "unit_price": "1.00"}],
        }
        assert (await first.put("/api/orders", json={"order": order})).status_code == 200
        edits = [
            client.put("/api/orders", json={"order": order, "expected_version": 1})
            for client in (first, second)
        ]
        assert sorted(response.status_code for response in await asyncio.gather(*edits)) == [
            200,
            409,
        ]
        assert (await second.get("/api/orders")).json()["custom"][0]["version"] == 2
