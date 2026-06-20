"""Database connection helper (raw psycopg2, no ORM)."""

import os

import psycopg2


def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env (or export it)."
        )
    return url


def connect():
    """Open a new psycopg2 connection. Caller manages the transaction."""
    return psycopg2.connect(dsn())
