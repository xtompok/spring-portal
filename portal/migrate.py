"""Tiny SQL migration runner.

Applies numbered .sql files from db/migrations in order, each in its own
transaction, recording applied versions in a schema_version table. Re-running is
a no-op for already-applied files. No framework, no ORM — the schema stays as
readable SQL.
"""

import os
import pathlib

from . import db

# Default to the repo layout (works for editable installs / running from source);
# override with PORTAL_MIGRATIONS_DIR when installed elsewhere (e.g. in the image).
_DEFAULT_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "db" / "migrations"
MIGRATIONS_DIR = pathlib.Path(
    os.environ.get("PORTAL_MIGRATIONS_DIR", _DEFAULT_MIGRATIONS_DIR)
)

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


def _discover():
    """Return [(version, path)] sorted by the numeric filename prefix."""
    files = sorted(
        p for p in MIGRATIONS_DIR.glob("*.sql") if p.stem[:3].isdigit()
    )
    return [(p.stem, p) for p in files]


def applied_versions(conn) -> set:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_version")
        return {r[0] for r in cur.fetchall()}


def run(echo=print) -> int:
    """Apply all pending migrations. Returns the number applied."""
    conn = db.connect()
    applied = 0
    try:
        with conn.cursor() as cur:
            cur.execute(_BOOTSTRAP)
        conn.commit()

        done = applied_versions(conn)
        for version, path in _discover():
            if version in done:
                continue
            echo(f"applying {version} ...")
            sql = path.read_text(encoding="utf-8")
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_version (version) VALUES (%s)", (version,)
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                echo(f"  ! failed on {version}, rolled back")
                raise
            applied += 1
        if applied == 0:
            echo("database is up to date")
        else:
            echo(f"applied {applied} migration(s)")
        return applied
    finally:
        conn.close()
