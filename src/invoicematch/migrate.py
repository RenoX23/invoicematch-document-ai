from __future__ import annotations

import os

import psycopg

from invoicematch.storage import ReviewStore


def main() -> None:
    url = os.getenv("INVOICEMATCH_MIGRATION_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("Set INVOICEMATCH_MIGRATION_URL or DATABASE_URL to PostgreSQL.")
    try:
        ReviewStore(url).initialize()
    except psycopg.Error as error:
        reason = type(error).__name__
        raise SystemExit(f"Could not initialize PostgreSQL ({reason}).") from None
    print("InvoiceMatch PostgreSQL schema is ready.")


if __name__ == "__main__":
    main()
