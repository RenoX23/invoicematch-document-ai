from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from decimal import Decimal
from functools import partial
from hashlib import sha256
from hmac import compare_digest
from importlib.resources import files
from pathlib import Path
from threading import BoundedSemaphore
from typing import Annotated, TypeVar

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

from invoicematch.domain import (
    Contract,
    Finding,
    InvoiceExtraction,
    OrderLine,
    PurchaseOrder,
    Reconciliation,
)
from invoicematch.evidence import source_from_pages
from invoicematch.model import Invocation, ModelFailure, normalize_model
from invoicematch.pdf import (
    MAX_CUSTOM_PDF_BYTES,
    MAX_PDF_BYTES,
    PdfInputError,
    read_pdf_pages,
    validate_pdf_header,
)
from invoicematch.reconciliation import extract_rules, reconcile
from invoicematch.storage import (
    SESSION_SECONDS,
    DecisionInput,
    MappingInput,
    OrderInput,
    Review,
    ReviewMode,
    ReviewStore,
    SavedOrder,
    StoreProblem,
)
from invoicematch.workers import model_isolated, parse_isolated

_LOG = logging.getLogger(__name__)
T = TypeVar("T", bound=Contract)
_SESSION_COOKIE = "invoicematch_session"
_SAMPLE_NAMES = ("clean-match.pdf", "price-mismatch.pdf", "ambiguous-extraction.pdf")
_ORDERS = {
    "PO-001": PurchaseOrder(
        order_id="PO-001",
        supplier_id="SUP-001",
        lines=(
            OrderLine(item_id="A-10", quantity=10, unit_price=Decimal("100.00")),
            OrderLine(item_id="B-20", quantity=2, unit_price=Decimal("50.00")),
        ),
    ),
    "PO-002": PurchaseOrder(
        order_id="PO-002",
        supplier_id="SUP-002",
        lines=(OrderLine(item_id="A-10", quantity=5, unit_price=Decimal("85.00")),),
    ),
}


def _problem(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


async def _limited_body(request: Request, limit: int) -> bytes:
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    raise _problem(413, "body_too_large", "The request exceeds the allowed size.")
                body.extend(chunk)
    except TimeoutError as error:
        raise _problem(408, "upload_timeout", "The upload took too long.") from error
    return bytes(body)


def _require_media_type(request: Request, expected: str) -> None:
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != expected:
        raise _problem(415, "unsupported_media_type", f"Use {expected} for this request.")


async def _json_input(
    request: Request, contract: type[T], limit: int, code: str, message: str
) -> T:
    _require_media_type(request, "application/json")
    body = await _limited_body(request, limit)
    try:
        return contract.model_validate_json(body)
    except ValidationError:
        raise _problem(422, code, message) from None


class OrderCatalog(Contract):
    samples: tuple[PurchaseOrder, ...]
    custom: tuple[SavedOrder, ...]
    live_enabled: bool


def create_app(
    database_path: Path | None = None,
    *,
    database_url: str | None = None,
    serverless: bool = False,
    additional_approved_hashes: frozenset[str] = frozenset(),
    live_runner: Callable[[tuple[str, ...]], Invocation] | None = None,
) -> FastAPI:
    if database_path is not None and (database_url is not None or serverless):
        raise ValueError("Choose one database target. Serverless mode requires PostgreSQL.")
    url = database_url if database_url is not None else os.getenv("DATABASE_URL", "")
    if serverless and not url.strip():
        raise ValueError("Set DATABASE_URL to the PostgreSQL connection URL.")
    store = ReviewStore(
        database_path
        or url.strip()
        or Path(os.environ.get("INVOICEMATCH_DB_PATH", "instance/reviews.sqlite3"))
    )
    resources = files("invoicematch")
    html = resources.joinpath("web", "index.html").read_text(encoding="utf-8")
    script = resources.joinpath("web", "review.js").read_text(encoding="utf-8")
    stylesheet = resources.joinpath("web", "review.css").read_text(encoding="utf-8")
    font = resources.joinpath("web", "instrument-sans.woff2").read_bytes()
    favicon = resources.joinpath("web", "favicon.png").read_bytes()
    samples = {name: resources.joinpath("samples", name).read_bytes() for name in _SAMPLE_NAMES}
    sample_hashes = {sha256(data).hexdigest() for data in samples.values()}
    approved_hashes = sample_hashes | additional_approved_hashes
    replay = TypeAdapter(dict[str, InvoiceExtraction]).validate_json(
        resources.joinpath("samples", "replay.json").read_bytes()
    )
    if replay.keys() != sample_hashes:
        raise ValueError("Sample replay capture does not match the bundled PDFs.")
    key = os.environ.get("GROQ_API_KEY", "").strip()
    invoke = live_runner or (partial(model_isolated, api_key=key) if key else None)
    parser_slots = BoundedSemaphore(2)
    live_slots = BoundedSemaphore(2)
    upload_slots = asyncio.Semaphore(16)
    secure_cookie = serverless or os.environ.get("INVOICEMATCH_SECURE_COOKIE") == "1"
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await run_in_threadpool(store.initialize)
        await run_in_threadpool(store.purge_expired)

        async def expire() -> None:
            while True:
                await asyncio.sleep(300)
                try:
                    await run_in_threadpool(store.purge_expired)
                except (sqlite3.Error, psycopg.Error):
                    _LOG.warning("Review session cleanup could not complete.")

        cleanup = asyncio.create_task(expire())
        try:
            yield
        finally:
            cleanup.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup

    app = FastAPI(
        title="InvoiceMatch",
        lifespan=None if serverless else lifespan,
        docs_url=None,
        redoc_url=None,
    )

    def require_session(request: Request) -> str:
        key = store.session_key(request.cookies.get(_SESSION_COOKIE))
        if key is None:
            raise _problem(401, "session_required", "Open the reviewer page to start a session.")
        return key

    @app.exception_handler(StoreProblem)
    async def store_problem(_: Request, error: StoreProblem) -> JSONResponse:
        return JSONResponse(
            {"detail": {"code": error.code, "message": str(error)}},
            status_code=error.status,
            headers=headers,
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error(_: Request, error: sqlite3.OperationalError) -> JSONResponse:
        code = getattr(error, "sqlite_errorcode", 0) & 0xFF
        if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise error
        return JSONResponse(
            {"detail": {"code": "store_busy", "message": "The review store is busy. Try again."}},
            status_code=503,
            headers={"Retry-After": "2", **headers},
        )

    @app.exception_handler(psycopg.OperationalError)
    @app.exception_handler(psycopg.errors.UndefinedTable)
    async def postgres_error(_: Request, error: psycopg.Error) -> JSONResponse:
        _LOG.warning("PostgreSQL review store unavailable (%s).", type(error).__name__)
        return JSONResponse(
            {
                "detail": {
                    "code": "store_busy",
                    "message": "The review store is unavailable. Try again.",
                }
            },
            status_code=503,
            headers={"Retry-After": "2", **headers},
        )

    @app.get("/health", include_in_schema=False)
    def health(response: Response) -> dict[str, str]:
        if serverless:
            store.check_ready()
            response.headers.update(headers)
        return {"status": "ok"}

    if serverless:
        cron_secret = os.getenv("CRON_SECRET", "")

        @app.get("/api/maintenance/purge", include_in_schema=False, status_code=204)
        def purge_expired(request: Request) -> Response:
            authorization = request.headers.get("authorization", "")
            if not cron_secret or not compare_digest(
                authorization.encode(), f"Bearer {cron_secret}".encode()
            ):
                raise _problem(401, "unauthorized", "Invalid maintenance credentials.")
            store.purge_expired()
            return Response(status_code=204, headers=headers)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def reviewer(request: Request) -> HTMLResponse:
        response = HTMLResponse(
            html,
            headers={
                **headers,
                "Content-Security-Policy": (
                    "default-src 'self'; object-src 'none'; base-uri 'none'; "
                    "frame-ancestors 'none'; form-action 'self'"
                ),
            },
        )
        if store.session_key(request.cookies.get(_SESSION_COOKIE)) is None:
            response.set_cookie(
                _SESSION_COOKIE,
                store.create_session(),
                httponly=True,
                secure=secure_cookie,
                samesite="strict",
                max_age=SESSION_SECONDS,
            )
        return response

    @app.get("/review.js", include_in_schema=False)
    def reviewer_script() -> Response:
        return Response(script, media_type="text/javascript", headers=headers)

    @app.get("/review.css", include_in_schema=False)
    def reviewer_stylesheet() -> Response:
        return Response(stylesheet, media_type="text/css", headers=headers)

    @app.get("/instrument-sans.woff2", include_in_schema=False)
    def reviewer_font() -> Response:
        return Response(font, media_type="font/woff2", headers=headers)

    @app.get("/favicon.png", include_in_schema=False)
    def reviewer_favicon() -> Response:
        return Response(favicon, media_type="image/png", headers=headers)

    @app.get("/samples/{filename}", include_in_schema=False)
    def download_sample(filename: str) -> Response:
        data = samples.get(filename)
        if data is None:
            raise _problem(404, "sample_not_found", "Choose a listed synthetic sample.")
        return Response(
            data,
            media_type="application/pdf",
            headers={**headers, "Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/orders")
    def list_orders(request: Request, response: Response) -> OrderCatalog:
        response.headers.update(headers)
        return OrderCatalog(
            samples=tuple(_ORDERS.values()),
            custom=store.orders(require_session(request)),
            live_enabled=invoke is not None,
        )

    @app.put("/api/orders")
    async def save_order(request: Request, response: Response) -> SavedOrder:
        session = await run_in_threadpool(require_session, request)
        body = await _json_input(
            request,
            OrderInput,
            64 * 1024,
            "invalid_order",
            "Enter a valid INR purchase order with unique item IDs, quantities, and prices.",
        )
        if body.order.order_id in _ORDERS:
            raise _problem(
                409, "sample_order_reserved", "Choose an order number different from the samples."
            )
        response.headers.update(headers)
        return await run_in_threadpool(store.save_order, session, body)

    @app.delete("/api/session", status_code=204)
    def clear_session(request: Request) -> Response:
        store.delete_session(require_session(request))
        response = Response(status_code=204, headers=headers)
        response.delete_cookie(
            _SESSION_COOKIE, secure=secure_cookie, httponly=True, samesite="strict"
        )
        return response

    def submit_review(
        session: str, order: PurchaseOrder, data: bytes, mode: ReviewMode, version: int | None
    ) -> tuple[Review, bool]:
        custom = version is not None
        order_key = f"custom:{order.order_id}:{version}" if custom else order.order_id
        try:
            validate_pdf_header(data, max_bytes=MAX_CUSTOM_PDF_BYTES if custom else MAX_PDF_BYTES)
            pdf_hash = sha256(data).hexdigest()
            if not custom and pdf_hash not in approved_hashes:
                raise _problem(
                    422, "unapproved_pdf", "Select your saved purchase order for a custom invoice."
                )
            if mode != "rules" and (custom or pdf_hash not in sample_hashes):
                raise _problem(
                    422,
                    "sample_mode_only",
                    "Replay and live extraction use a bundled sample and demo PO.",
                )
            existing = store.find_exact(session, pdf_hash, order_key, mode)
            if existing is not None:
                return existing, False
            if custom:
                if not parser_slots.acquire(blocking=False):
                    raise _problem(503, "parser_busy", "The PDF parser is busy. Try again shortly.")
                try:
                    pages = parse_isolated(data)
                finally:
                    parser_slots.release()
            else:
                pages = read_pdf_pages(data)
            source = source_from_pages(pages)
        except PdfInputError as error:
            status = 413 if error.code == "pdf_too_large" else 422
            raise _problem(status, error.code, str(error)) from error

        if mode == "replay":
            extraction = replay[pdf_hash]
            return store.add_review(
                session, pdf_hash, order, mode, extraction, reconcile(order, source, extraction)
            )
        if mode == "live":
            if invoke is None:
                raise _problem(
                    503,
                    "live_unavailable",
                    "Live AI extraction is unavailable. Choose rules or replay.",
                )
            if not live_slots.acquire(blocking=False):
                raise _problem(503, "live_busy", "Live AI extraction is busy. Try again shortly.")
            try:
                reservation = store.reserve_live(session, pdf_hash, order_key)
                if isinstance(reservation, Review):
                    return reservation, False
                try:
                    call = invoke(pages)
                    error_code = call.error
                    extraction = InvoiceExtraction()
                    if error_code is None:
                        if call.claims is None:
                            error_code = "missing_model_claims"
                        else:
                            try:
                                extraction = normalize_model(call.claims, source)
                            except ModelFailure as error:
                                error_code = error.code
                    result = (
                        Reconciliation(
                            verdict="needs_review",
                            findings=(Finding(code=error_code, field="extraction"),),
                        )
                        if error_code
                        else reconcile(order, source, extraction)
                    )
                    return store.add_review(
                        session,
                        pdf_hash,
                        order,
                        mode,
                        extraction,
                        result,
                        grounded_identity=extract_rules(source),
                    )
                finally:
                    store.release_live(reservation)
            finally:
                live_slots.release()

        extraction = extract_rules(source)
        result = reconcile(order, source, extraction)
        return store.add_review(
            session,
            pdf_hash,
            order,
            mode,
            extraction,
            result,
            order_version=version,
            source=source if custom else None,
        )

    @app.post("/api/reviews", status_code=201)
    async def upload(
        request: Request,
        response: Response,
        order_id: Annotated[str, Query(max_length=64)],
        mode: ReviewMode = "rules",
        order_version: Annotated[int | None, Query(ge=1)] = None,
    ) -> Review:
        _require_media_type(request, "application/pdf")
        if upload_slots.locked():
            raise _problem(
                503, "upload_busy", "Several invoices are processing. Try again shortly."
            )
        try:
            async with upload_slots, asyncio.timeout(22):
                session = await asyncio.to_thread(require_session, request)
                order = _ORDERS.get(order_id)
                version = None
                if order is None:
                    saved = await asyncio.to_thread(store.get_order, session, order_id)
                    if saved is None:
                        raise _problem(
                            404, "unknown_order", "Choose a sample or saved purchase order."
                        )
                    order, version = saved.order, saved.version
                if order_version is not None and order_version != version:
                    raise _problem(
                        409,
                        "order_version_conflict",
                        "Reload and select the current purchase order.",
                    )
                await asyncio.to_thread(store.admit_upload, session)
                data = await _limited_body(
                    request, MAX_CUSTOM_PDF_BYTES if version else MAX_PDF_BYTES
                )
                review, created = await asyncio.to_thread(
                    submit_review, session, order, data, mode, version
                )
        except TimeoutError:
            raise _problem(
                504, "review_timeout", "The review deadline elapsed. Retry the same upload."
            ) from None
        if not created:
            response.status_code = 200
        response.headers.update(headers)
        return review

    @app.get("/api/reviews/{review_id}")
    def get_review(review_id: str, request: Request, response: Response) -> Review:
        review = store.get(require_session(request), review_id)
        if review is None:
            raise _problem(404, "review_not_found", "This review is unavailable in your session.")
        response.headers.update(headers)
        return review

    @app.post("/api/reviews/{review_id}/decisions", status_code=201)
    async def save_decision(review_id: str, request: Request, response: Response) -> Review:
        session = await run_in_threadpool(require_session, request)
        decision = await _json_input(
            request,
            DecisionInput,
            4096,
            "invalid_decision",
            "Select a decision and use up to 400 note characters.",
        )
        review = await run_in_threadpool(store.append_decision, session, review_id, decision)
        if review is None:
            raise _problem(404, "review_not_found", "This review is unavailable in your session.")
        response.headers.update(headers)
        return review

    @app.put("/api/reviews/{review_id}/mapping")
    async def confirm_mapping(review_id: str, request: Request, response: Response) -> Review:
        session = await run_in_threadpool(require_session, request)
        mapping = await _json_input(
            request, MappingInput, 64 * 1024, "invalid_mapping", "Enter valid item IDs."
        )
        review = await run_in_threadpool(store.map_review, session, review_id, mapping)
        if review is None:
            raise _problem(404, "review_not_found", "This review is unavailable in your session.")
        response.headers.update(headers)
        return review

    return app
