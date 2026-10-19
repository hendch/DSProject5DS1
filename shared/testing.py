"""Test helpers for any agent: a throwaway PostgreSQL with every migration applied,
one fresh database per test. No Docker needed.

Postgres comes from TEST_DATABASE_URL if set (e.g. the docker-compose one:
postgresql://devops:devops@localhost:5432/postgres), otherwise from the `pgserver`
package (in the dev extras), which runs a local PostgreSQL 16 with pgvector.
"""
import itertools
import os
import tempfile
import unittest
from unittest import mock

import psycopg2

from shared.migrate import migrate

TEMPLATE = "agents_test_template"
_admin_uri = None
_counter = itertools.count()


def _server_uri() -> str | None:
    if os.getenv("TEST_DATABASE_URL"):
        return os.environ["TEST_DATABASE_URL"]
    try:
        import pgserver
    except ImportError:
        return None
    return pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop").get_uri()


def with_db(uri: str, name: str) -> str:
    base, _, _ = uri.rpartition("/")
    return f"{base}/{name}"


def _admin(sql: str) -> None:
    conn = psycopg2.connect(admin_uri())
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
    finally:
        conn.close()


def admin_uri() -> str:
    """Start Postgres once per test process and build a fully migrated template database."""
    global _admin_uri
    if _admin_uri is None:
        uri = _server_uri()
        if uri is None:
            raise unittest.SkipTest("no Postgres: set TEST_DATABASE_URL or install the dev extras")
        _admin_uri = uri
        _admin(f"DROP DATABASE IF EXISTS {TEMPLATE}")
        _admin(f"CREATE DATABASE {TEMPLATE}")
        migrate(with_db(uri, TEMPLATE), quiet=True)
    return _admin_uri


def fresh_database() -> tuple[str, str]:
    """Create a new database from the migrated template. Returns (name, url)."""
    name = f"agents_test_{os.getpid()}_{next(_counter)}"
    _admin(f"CREATE DATABASE {name} TEMPLATE {TEMPLATE}")
    return name, with_db(admin_uri(), name)


def drop_database(name: str) -> None:
    _admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


class DbTestCase(unittest.TestCase):
    """Each test gets its own migrated database, with DATABASE_URL pointing at it."""

    def setUp(self):
        self.dbname, self.db_uri = fresh_database()
        p = mock.patch.dict(os.environ, {"DATABASE_URL": self.db_uri})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(drop_database, self.dbname)

    def sql(self, query, params=()):
        conn = psycopg2.connect(self.db_uri)
        try:
            with conn, conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchall() if cur.description else None
        finally:
            conn.close()

    def raw_event(self, body) -> int:
        """Put any JSON on the event log, as a broken producer might."""
        import json
        return self.sql("INSERT INTO events (body) VALUES (%s) RETURNING id", (json.dumps(body),))[0][0]

    def events(self) -> list[dict]:
        """Every event body on the log, oldest first."""
        return [r[0] for r in self.sql("SELECT body FROM events ORDER BY id")]

    def unhandled(self, consumer: str) -> int:
        """Events `consumer` has not finished with (no receipt yet, or waiting for a retry)."""
        return self.sql("""SELECT count(*) FROM events e LEFT JOIN event_receipts r
                           ON r.consumer = %s AND r.event_id = e.id
                           WHERE r.event_id IS NULL OR r.status = 'retry'""", (consumer,))[0][0]
