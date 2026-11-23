import asyncio
import sqlite3
import subprocess
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, closing
from importlib.resources import files
from pathlib import Path
from threading import Event

import pytest
from httpx import ASGITransport, AsyncClient, Response

from invoicematch import storage, workers
from invoicematch.app import create_app
from invoicematch.model import Invocation
from invoicematch.pdf import MAX_CUSTOM_PDF_BYTES, PdfInputError
from scripts.generate_demo_fixtures import _text_pdf

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def reviewer(
    path: Path, live: Callable[[tuple[str, ...]], Invocation] | None = None
) -> AsyncIterator[AsyncClient]:
    app = create_app(path, live_runner=live)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/")).status_code == 200
        yield client


def invoice(
    *,
    supplier: str = "Acme Parts",
    item: str = "PART-7",
    total: str = "INR 1,190.00",
    amounts: tuple[str, ...] = (
        "Subtotal: INR 1,000.00",
        "CGST: 90.00",
        "SGST: 90.00",
        "Shipping: 20.00",
        "Discount: 10.00",
    ),
) -> bytes:
    columns = "{:<12}{:<8}{:<16}{}"
    return _text_pdf(
        (
            "Invoice No.: EXT-908",
            f"Vendor: {supplier}",
            "Currency: INR",
            columns.format("SKU", "Qty", "Unit rate", "Line amount"),
            columns.format(item, "1", "INR 1,000.00", "INR 1,000.00"),
            *amounts,
            f"Grand total: {total}",
        )
    )


def sample(name: str = "clean-match.pdf") -> bytes:
    return files("invoicematch").joinpath("samples", name).read_bytes()


async def save_order(
    client: AsyncClient, *, price: str = "1000.00", expected: int | None = None
) -> Response:
    return await client.put(
        "/api/orders",
        json={
            "order": {
                "order_id": "USER-PO",
                "supplier_id": "Acme Parts",
                "currency": "INR",
                "lines": [{"item_id": "PART-7", "quantity": 1, "unit_price": price}],
            },
            "expected_version": expected,
        },
    )


async def upload(
    client: AsyncClient,
    data: bytes,
    *,
    mode: str = "rules",
    order: str = "USER-PO",
    version: int | None = None,
) -> Response:
    query = {"order_id": order, "mode": mode}
    if version is not None:
        query["order_version"] = str(version)
    return await client.post(
        "/api/reviews", params=query, content=data, headers={"Content-Type": "application/pdf"}
    )


async def test_unseen_invoice_total_and_po_snapshot_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "reviews.sqlite3"
    async with reviewer(database) as client:
        saved = await save_order(client)
        assert saved.status_code == 200
        first = await upload(client, invoice(), version=1)
        assert first.status_code == 201, first.text
        original = first.json()
        assert original["result"]["verdict"] == "match", original["result"]
        assert len(original["extraction"]["adjustments"]) == 5
        assert original["extraction"]["adjustments"]["cgst"]["evidence"]["page"] == 1
        identifier = original["review_id"]
        stored = await client.post(
            f"/api/reviews/{identifier}/decisions", json={"decision": "accept"}
        )
        assert stored.status_code == 201
        cookie = dict(client.cookies)
        updated = await save_order(client, price="1020.00", expected=1)
        assert updated.json()["version"] == 2
        assert (await save_order(client, price="1050.00", expected=1)).status_code == 409
        assert (await upload(client, invoice(), version=1)).status_code == 409
        second = await upload(client, invoice(), version=2)
        assert second.status_code == 201
        assert second.json()["review_id"] != identifier
        assert second.json()["result"]["verdict"] == "mismatch"
        assert (await upload(client, invoice(), version=2)).status_code == 200

    async with reviewer(database) as client:
        client.cookies.update(cookie)
        earlier = (await client.get(f"/api/reviews/{identifier}")).json()
        assert earlier["order_version"] == 1
        assert earlier["order"]["lines"][0]["unit_price"] == "1000.00"
        assert earlier["decisions"][0]["decision"] == "accept"


async def test_gst_ambiguity_and_bad_grand_total_fail_closed(tmp_path: Path) -> None:
    async with reviewer(tmp_path / "reviews.sqlite3") as client:
        await save_order(client)
        mismatch = (await upload(client, invoice(total="1185.00"))).json()
        assert "invoice_total_mismatch" in {
            finding["code"] for finding in mismatch["result"]["findings"]
        }
    async with reviewer(tmp_path / "reviews.sqlite3") as client:
        await save_order(client)
        response = await upload(
            client, invoice(amounts=("CGST: 90.00", "SGST: 90.00", "Tax: 180.00"))
        )
        result = response.json()["result"]
        assert result["verdict"] == "needs_review"
        assert "ambiguous_tax_components" in {finding["code"] for finding in result["findings"]}


async def test_mapping_is_audited_and_keeps_original_extraction(tmp_path: Path) -> None:
    async with reviewer(tmp_path / "reviews.sqlite3") as client:
        await save_order(client)
        response = await upload(client, invoice(supplier="Acme Trading", item="ACME-7"))
        original = response.json()
        identifier = original["review_id"]
        assert original["result"]["verdict"] == "mismatch"
        assert (
            await client.post(f"/api/reviews/{identifier}/decisions", json={"decision": "hold"})
        ).status_code == 201
        invalid = await client.put(
            f"/api/reviews/{identifier}/mapping", json={"items": {"ACME-7": "MISSING"}}
        )
        assert invalid.status_code == 422
        confirmed = await client.put(
            f"/api/reviews/{identifier}/mapping",
            json={"confirm_supplier": True, "items": {"ACME-7": "PART-7"}},
        )
        assert confirmed.status_code == 200
        review = confirmed.json()
        assert review["result"]["verdict"] == "match"
        assert review["extraction"] == original["extraction"]
        assert review["mappings"][0]["mapping"]["items"] == {"ACME-7": "PART-7"}
        assert review["decisions"][0]["verdict"] == "mismatch"
        assert (await client.get(f"/api/reviews/{identifier}")).json() == review


async def test_custom_data_is_private_and_session_deletion_cleans_records(tmp_path: Path) -> None:
    database = tmp_path / "reviews.sqlite3"
    async with reviewer(database) as first:
        await save_order(first)
        created = (await upload(first, invoice())).json()["review_id"]
        async with reviewer(database) as second:
            assert (await second.get("/api/orders")).json()["custom"] == []
            assert (await second.get(f"/api/reviews/{created}")).status_code == 404
            assert (await upload(second, invoice())).status_code == 404
            assert (await second.put(f"/api/reviews/{created}/mapping", json={})).status_code == 404
        assert (await first.delete("/api/session")).status_code == 204
        assert (await first.get(f"/api/reviews/{created}")).status_code == 401
    with closing(sqlite3.connect(database)) as connection:
        for table in ("saved_orders", "reviews", "review_inputs"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


async def test_expired_sessions_are_purged(tmp_path: Path) -> None:
    database = tmp_path / "reviews.sqlite3"
    async with reviewer(database) as client:
        await save_order(client)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("UPDATE session_expiry SET expires_at=0")
        assert (await client.get("/api/orders")).status_code == 401
        storage.ReviewStore(database).purge_expired()
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM saved_orders").fetchone() == (0,)


async def test_custom_pdf_checks_and_no_provider_transmission(tmp_path: Path) -> None:
    def unexpected(_: tuple[str, ...]) -> Invocation:
        pytest.fail("Custom invoice reached the provider.")

    async with reviewer(tmp_path / "reviews.sqlite3", unexpected) as client:
        await save_order(client)
        denied = await upload(client, invoice(), mode="live")
        assert denied.json()["detail"]["code"] == "sample_mode_only"
        assert (await upload(client, b"x" * (MAX_CUSTOM_PDF_BYTES + 1))).status_code == 413
        malformed = await upload(client, b"%PDF-1.4\ninvalid")
        assert malformed.status_code == 422
        assert malformed.json()["detail"]["code"] == "invalid_pdf"
        unreadable = await upload(client, _text_pdf(("No machine-readable invoice fields",)))
        assert unreadable.status_code == 201
        assert unreadable.json()["result"]["verdict"] == "needs_review"


async def test_replays_preserve_all_sample_scenarios_without_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    async with reviewer(tmp_path / "reviews.sqlite3") as client:
        for name, expected in (
            ("clean-match.pdf", "match"),
            ("price-mismatch.pdf", "mismatch"),
            ("ambiguous-extraction.pdf", "needs_review"),
        ):
            response = await upload(client, sample(name), mode="replay", order="PO-001")
            assert response.status_code == 201, response.text
            assert response.json()["result"]["verdict"] == expected
        retry = await upload(client, sample(), mode="replay", order="PO-001")
        assert retry.status_code == 200
        assert retry.json()["mode"] == "replay"
        unavailable = await upload(client, sample(), mode="live", order="PO-001")
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"]["code"] == "live_unavailable"


async def test_live_timeout_is_recorded_once_under_concurrent_retries(tmp_path: Path) -> None:
    started = Event()
    release = Event()
    calls = []

    def delayed(pages: tuple[str, ...]) -> Invocation:
        calls.append(pages)
        started.set()
        assert release.wait(5)
        return Invocation(claims=None, error="provider_timeout")

    async with reviewer(tmp_path / "reviews.sqlite3", delayed) as client:
        initial = asyncio.create_task(upload(client, sample(), mode="live", order="PO-001"))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            duplicate = await upload(client, sample(), mode="live", order="PO-001")
            assert duplicate.status_code == 409
            assert duplicate.json()["detail"]["code"] == "review_processing"
        finally:
            release.set()
        result = await initial
        assert result.status_code == 201
        assert result.json()["result"]["findings"][0]["code"] == "provider_timeout"
        assert (await upload(client, sample(), mode="live", order="PO-001")).json() == result.json()
        assert len(calls) == 1


async def test_live_budget_applies_across_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "LIVE_PER_DAY", 1)
    calls = []

    def fake(pages: tuple[str, ...]) -> Invocation:
        calls.append(pages)
        return Invocation(claims=None, error="provider_timeout")

    database = tmp_path / "reviews.sqlite3"
    async with reviewer(database, fake) as first:
        assert (await upload(first, sample(), mode="live", order="PO-001")).status_code == 201
    async with reviewer(database, fake) as second:
        limited = await upload(second, sample(), mode="live", order="PO-001")
        assert limited.status_code == 429
        assert limited.json()["detail"]["code"] == "usage_limit"
    assert len(calls) == 1


def test_worker_deadlines_and_pdf_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key-never-forwarded-to-pdf")

    def timeout(command: list[str], **settings: object) -> None:
        assert "-I" in command
        assert "GROQ_API_KEY" not in settings["env"]
        deadline = workers.PDF_DEADLINE if command[-1] == "pdf" else workers.LIVE_DEADLINE
        assert settings["timeout"] == deadline
        raise subprocess.TimeoutExpired(command, deadline)

    monkeypatch.setattr(workers.subprocess, "run", timeout)
    with pytest.raises(PdfInputError) as error:
        workers.parse_isolated(invoice())
    assert error.value.code == "pdf_timeout"
    assert workers.model_isolated(("sample",), "test-only").error == "provider_timeout"
