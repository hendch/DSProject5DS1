"""Test helpers: a throwaway Postgres (one fresh database per test), a fake
Redis, and event builders. No Docker needed.

Postgres comes from TEST_DATABASE_URL if set (e.g. the docker-compose one:
postgresql://devops:devops@localhost:5432/postgres), otherwise from the
`pgserver` package (pip install pgserver), which runs a local PostgreSQL 16.
"""
import itertools
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import fakeredis
import psycopg2

ROOT = Path(__file__).resolve().parents[3]
SQL_FILES = [ROOT / "db/schema.sql", ROOT / "db/migrations/002_guardrail.sql"]
TEMPLATE = "guardrail_template"

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


def _with_db(uri: str, name: str) -> str:
    base, _, _ = uri.rpartition("/")
    return f"{base}/{name}"


def admin_uri() -> str:
    """Start Postgres once and build a template database holding the schema."""
    global _admin_uri
    if _admin_uri is None:
        uri = _server_uri()
        if uri is None:
            raise unittest.SkipTest("no Postgres: set TEST_DATABASE_URL or pip install pgserver")
        conn = psycopg2.connect(uri)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {TEMPLATE}")
            cur.execute(f"CREATE DATABASE {TEMPLATE}")
        conn.close()
        tconn = psycopg2.connect(_with_db(uri, TEMPLATE))
        with tconn, tconn.cursor() as cur:
            for f in SQL_FILES:
                cur.execute(f.read_text())
        tconn.close()
        _admin_uri = uri
    return _admin_uri


class DbTestCase(unittest.TestCase):
    """Each test gets its own database, DATABASE_URL pointing at it, and a fake Redis."""

    def setUp(self):
        uri = admin_uri()
        self.dbname = f"guardrail_test_{os.getpid()}_{next(_counter)}"
        conn = psycopg2.connect(uri)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE {self.dbname} TEMPLATE {TEMPLATE}")
        conn.close()
        self.db_uri = _with_db(uri, self.dbname)

        self.redis = fakeredis.FakeRedis()
        patches = [mock.patch.dict(os.environ, {"DATABASE_URL": self.db_uri})]
        patches += [mock.patch(f"{m}.get_redis", return_value=self.redis) for m in
                    ("shared.events", "agents.guardrail.announce", "agents.guardrail.agent")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._drop_db)

    def _drop_db(self):
        conn = psycopg2.connect(admin_uri())
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {self.dbname} WITH (FORCE)")
        conn.close()

    def sql(self, query, params=()):
        conn = psycopg2.connect(self.db_uri)
        try:
            with conn, conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchall() if cur.description else None
        finally:
            conn.close()

    def stream(self, name="pipeline_events") -> list[dict]:
        """Every well-formed event on the stream (garbage entries are skipped)."""
        import json
        events = []
        for _, f in self.redis.xrange(name):
            try:
                events.append(json.loads(f[b"data"]))
            except (KeyError, json.JSONDecodeError):
                pass
        return events

    def publish(self, **overrides) -> dict:
        """Publish a real agent event through shared.publish_and_log, so the
        actions row exists exactly as it would in the pipeline."""
        from shared.events import publish_and_log
        e = event(**overrides)
        e["event_id"] = publish_and_log(
            agent_name=e["agent_name"], action_type=e["action_type"], payload=e["payload"],
            correlation_id=e["correlation_id"], environment=e["environment"],
            blast_radius=e["blast_radius"], input_summary="test")
        return e


def event(**overrides) -> dict:
    e = {
        "event_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
        "agent_name": "dependency_resolver", "action_type": "propose_fix",
        "environment": "dev", "blast_radius": "low",
        "payload": {"cycle": ["terraform_data.a", "terraform_data.b"],
                    "category": "modeling_mistake", "proposed_diff": "Split the resources.",
                    "sources": {"detection": "tofu", "classification": "llm", "explanation": "llm"}},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload = overrides.pop("payload", None)
    e.update(overrides)
    if payload is not None:
        e["payload"] = {**e["payload"], **payload}
    return e
