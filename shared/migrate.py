"""Apply db/migrations/*.sql in order, once each, on any PostgreSQL.

    python -m shared.migrate            # local DATABASE_URL
    python -m shared.migrate --shared   # NEON_DATABASE_URL
    python -m shared.migrate --status   # list applied / pending, change nothing

Each file runs in its own transaction and is recorded in `schema_migrations` with a
checksum. A recorded file whose content changed is reported, never re-run: write a
new numbered file instead. Every migration is also written to be idempotent.
"""
import argparse
import hashlib
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

load_dotenv()
MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"


def _files() -> list[Path]:
    return sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _applied(cur) -> dict[str, str]:
    cur.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                       version    TEXT PRIMARY KEY,
                       checksum   TEXT NOT NULL,
                       applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    cur.execute("SELECT version, checksum FROM schema_migrations")
    return dict(cur.fetchall())


def migrate(database_url: str, quiet: bool = False) -> list[str]:
    """Apply every pending migration. Returns the versions applied now."""
    say = (lambda *a: None) if quiet else print
    conn = psycopg2.connect(database_url)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('schema_migrations'))")
            applied = _applied(cur)
        done = []
        for path in _files():
            version = path.stem
            if version in applied:
                if applied[version] != _checksum(path):
                    say(f"[migrate] WARNING {path.name} changed after it was applied; "
                        "put the change in a new migration file")
                continue
            with conn, conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('schema_migrations'))")
                cur.execute(path.read_text(encoding="utf-8"))
                cur.execute("INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s) "
                            "ON CONFLICT (version) DO NOTHING", (version, _checksum(path)))
            say(f"[migrate] applied {path.name}")
            done.append(version)
        if not done:
            say("[migrate] database is up to date")
        return done
    finally:
        conn.close()


def status(database_url: str) -> None:
    conn = psycopg2.connect(database_url)
    try:
        with conn, conn.cursor() as cur:
            applied = _applied(cur)
    finally:
        conn.close()
    for path in _files():
        state = "applied" if path.stem in applied else "PENDING"
        if path.stem in applied and applied[path.stem] != _checksum(path):
            state = "applied, CHANGED since"
        print(f"{state:24} {path.name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--shared", action="store_true", help="use NEON_DATABASE_URL")
    parser.add_argument("--status", action="store_true", help="list migrations, change nothing")
    args = parser.parse_args(argv)
    url = os.getenv("NEON_DATABASE_URL" if args.shared else "DATABASE_URL")
    if not url:
        print("No database URL: set DATABASE_URL (or NEON_DATABASE_URL with --shared) in .env")
        return 2
    status(url) if args.status else migrate(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
