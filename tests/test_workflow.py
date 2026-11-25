import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import sha256
from importlib.resources import files
from io import BytesIO
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient, Response
from pypdf import PdfWriter

from invoicematch.app import create_app
from invoicematch.database import Database
from invoicematch.pdf import MAX_PDF_BYTES

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _sample(name: str = "clean-match.pdf") -> bytes:
    return files("invoicematch").joinpath("samples", name).read_bytes()


@asynccontextmanager
async def _client(
    database: Path | str, *, approved_extra: tuple[bytes, ...] = ()
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        database if isinstance(database, Path) else None,
        database_url=database if isinstance(database, str) else None,
        additional_approved_hashes=frozenset(sha256(data).hexdigest() for data in approved_extra),
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


async def _submit(client: AsyncClient, data: bytes, order_id: str = "PO-001") -> Response:
    return await client.post(
        "/api/reviews",
        params={"order_id": order_id},
        content=data,
        headers={"Content-Type": "application/pdf"},
    )


async def test_reviewer_serves_its_assets_under_the_same_origin(database: Path | str) -> None:
    async with _client(database) as client:
        page = await client.get("/")
        assert "default-src 'self'" in page.headers["content-security-policy"]
        for path, content_type in (
            ("/review.css", "text/css"),
            ("/review.js", "text/javascript"),
            ("/instrument-sans.woff2", "font/woff2"),
            ("/favicon.png", "image/png"),
        ):
            assert f'"{path}"' in page.text
            asset = await client.get(path)
            assert asset.status_code == 200
            assert asset.headers["content-type"].split(";")[0] == content_type
            assert asset.content


@pytest.mark.parametrize(
    ("filename", "verdict"),
    [
        ("clean-match.pdf", "match"),
        ("price-mismatch.pdf", "mismatch"),
        ("ambiguous-extraction.pdf", "needs_review"),
    ],
)
async def test_normal_upload_path(database: Path | str, filename: str, verdict: str) -> None:
    async with _client(database) as client:
        assert (await client.get("/health")).json() == {"status": "ok"}
        assert "Check an invoice against a purchase order" in (await client.get("/")).text
        pdf = await client.get(f"/samples/{filename}")
        assert pdf.content == _sample(filename)
        response = await _submit(client, pdf.content)

    assert response.status_code == 201
    review = response.json()
    assert review["result"]["verdict"] == verdict
    assert review["extraction"]["invoice_number"]["evidence"]["page"] == 1
    if filename == "price-mismatch.pdf":
        finding = review["result"]["findings"][0]
        assert (finding["expected"], finding["observed"]) == ("100.00", "120.00")
        assert "A-10" in finding["evidence"]["excerpt"]
    if filename == "ambiguous-extraction.pdf":
        finding = review["result"]["findings"][0]
        assert finding["code"] == "unverified_line"
        assert "A-10" in finding["evidence"]["excerpt"]
    with Database(database).transaction() as connection:
        assert connection.count("SELECT COUNT(*) AS total FROM reviews") == 1


async def test_duplicate_workflow_and_decision_survive_worker_restart(database: Path | str) -> None:
    async with _client(database) as client:
        response = await client.get("/")
        assert "httponly" in response.headers["set-cookie"].lower()
        cookies = dict(client.cookies)
        created = await _submit(client, _sample())
        assert created.status_code == 201
        review_id = created.json()["review_id"]
        invalid = await client.post(
            f"/api/reviews/{review_id}/decisions",
            json={"decision": "accept", "note": "bad\x00note"},
        )
        assert invalid.status_code == 422
        saved = await client.post(
            f"/api/reviews/{review_id}/decisions",
            json={"decision": "accept", "note": "Checked lines."},
        )
        assert saved.status_code == 201

    async with _client(database) as client:
        client.cookies.update(cookies)
        record = await client.get(f"/api/reviews/{review_id}")
        assert record.status_code == 200
        assert record.json()["decisions"][0]["note"] == "Checked lines."
        repeated = await _submit(client, _sample())
        assert repeated.status_code == 200
        assert repeated.json() == record.json()
    with Database(database).transaction() as connection:
        assert connection.count("SELECT COUNT(*) AS total FROM reviews") == 1
        assert connection.count("SELECT COUNT(*) AS total FROM decision_events") == 1


async def test_concurrent_api_retries_commit_only_one_review(database: Path | str) -> None:
    async with _client(database) as first, _client(database) as second:
        await first.get("/")
        second.cookies.update(first.cookies)
        responses = await asyncio.gather(
            *(_submit(client, _sample()) for client in (first, second) * 4)
        )

    assert sorted(response.status_code for response in responses) == [200] * 7 + [201]
    assert len({response.json()["review_id"] for response in responses}) == 1
    with Database(database).transaction() as connection:
        assert connection.count("SELECT COUNT(*) AS total FROM reviews") == 1


async def test_sessions_cannot_read_or_change_each_others_reviews(database: Path | str) -> None:
    async with _client(database) as client:
        assert (await _submit(client, _sample())).status_code == 401
        await client.get("/")
        first = await _submit(client, _sample())
        review_id = first.json()["review_id"]
        client.cookies.clear()
        await client.get("/")

        assert (await client.get(f"/api/reviews/{review_id}")).status_code == 404
        forbidden = await client.post(
            f"/api/reviews/{review_id}/decisions", json={"decision": "hold"}
        )
        assert forbidden.status_code == 404
        second = await _submit(client, _sample())
        assert second.status_code == 201
        assert second.json()["review_id"] != review_id


async def test_changed_invoice_bytes_and_conflicting_order_need_review(
    database: Path | str,
) -> None:
    changed = _sample().replace(b"SYNTHETIC INVOICE", b"SYNTHETIC REPRINT")
    assert changed != _sample()
    assert len(changed) == len(_sample())
    async with _client(database, approved_extra=(changed,)) as client:
        await client.get("/")
        original = await _submit(client, _sample())
        duplicate = await _submit(client, changed)
        wrong_order = await _submit(client, _sample(), "PO-002")
        assert original.json()["result"]["verdict"] == "match"
        assert duplicate.json()["result"]["verdict"] == "needs_review"
        assert duplicate.json()["result"]["findings"][0]["code"] == "possible_duplicate"
        assert wrong_order.json()["result"]["verdict"] == "needs_review"
        assert wrong_order.json()["result"]["findings"][0]["code"] == "conflicting_order_selection"
        retry = await _submit(client, _sample())
        assert retry.json() == original.json()


async def test_upload_boundaries_and_unreadable_documents(database: Path | str) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    blank_pdf = BytesIO()
    writer.write(blank_pdf)
    unreadable = blank_pdf.getvalue()
    corrupt = b"%PDF-1.4\ninvalid"
    wrong_currency = _sample().replace(b"Currency: INR", b"Currency: USD")
    async with _client(database, approved_extra=(unreadable, corrupt, wrong_currency)) as client:
        await client.get("/")
        for data, order, status, code in (
            (b"x" * (MAX_PDF_BYTES + 1), "PO-001", 413, "body_too_large"),
            (corrupt, "PO-001", 422, "invalid_pdf"),
            (_sample(), "NO-ORDER", 404, "unknown_order"),
            (_sample() + b"x", "PO-001", 422, "unapproved_pdf"),
        ):
            response = await _submit(client, data, order)
            assert response.status_code == status
            assert response.json()["detail"]["code"] == code
        no_text = await _submit(client, unreadable)
        assert no_text.status_code == 201
        assert no_text.json()["result"]["verdict"] == "needs_review"
        currency = await _submit(client, wrong_currency)
        assert currency.status_code == 201
        assert currency.json()["result"]["findings"][0]["code"] == "unsupported_currency"
    with Database(database).transaction() as connection:
        assert connection.count("SELECT COUNT(*) AS total FROM reviews") == 2
