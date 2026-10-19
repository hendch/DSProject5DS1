"""Event bus on PostgreSQL (db/migrations/003_events.sql).

Publishing: `publish_and_log` writes the agent's action, its audit row and the event in
ONE transaction, so they exist together or not at all (transactional outbox).

Consuming: a `Consumer` reads events it has not handled yet, in order, and records a
receipt for each: done, retry later (with backoff), or dead after `max_attempts`
failures. Delivery is at-least-once, so handlers must be idempotent. `run()` sleeps on
PostgreSQL LISTEN/NOTIFY and wakes up as soon as an event is inserted.
"""
import json
import select
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable

import psycopg2.extensions
import psycopg2.extras

from shared.db import get_connection

CHANNEL = "events"


def make_event(agent_name, action_type, payload, correlation_id, environment="dev",
               blast_radius="low", event_id=None) -> dict:
    return {
        "event_id": str(event_id or uuid.uuid4()), "correlation_id": str(correlation_id),
        "agent_name": agent_name, "action_type": action_type,
        "environment": environment, "blast_radius": blast_radius,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _insert_event(cur, event) -> int:
    cur.execute("INSERT INTO events (body) VALUES (%s) RETURNING id", (json.dumps(event, default=str),))
    return cur.fetchone()[0]


def publish_and_log(agent_name, action_type, payload, correlation_id,
                    environment="dev", blast_radius="low",
                    category=None, input_summary=None, use_shared=False):
    """Record an agent's proposal: action + audit row + event, atomically. Returns its id."""
    event = make_event(agent_name, action_type, payload, correlation_id, environment, blast_radius)
    conn = get_connection(use_shared)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO actions (id, correlation_id, agent_name, action_type,
                   environment, blast_radius, payload) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (event["event_id"], correlation_id, agent_name, action_type,
                 environment, blast_radius, json.dumps(payload)),
            )
            cur.execute(
                """INSERT INTO reasoning_logs (correlation_id, agent_name, input_summary, decision)
                   VALUES (%s,%s,%s,%s)""",
                (correlation_id, agent_name, input_summary, category),
            )
            _insert_event(cur, event)
    finally:
        conn.close()
    return event["event_id"]


def publish(event, use_shared=False) -> int:
    """Append an event that has no action row (decisions, run lifecycle). Returns its id."""
    conn = get_connection(use_shared)
    try:
        with conn, conn.cursor() as cur:
            return _insert_event(cur, event)
    finally:
        conn.close()


def list_events(correlation_id=None, limit=100, use_shared=False) -> list[dict]:
    """Newest first: [{id, created_at, event}]."""
    conn = get_connection(use_shared)
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if correlation_id:
                cur.execute("""SELECT id, created_at, body AS event FROM events
                               WHERE body->>'correlation_id' = %s ORDER BY id DESC LIMIT %s""",
                            (str(correlation_id), limit))
            else:
                cur.execute("SELECT id, created_at, body AS event FROM events ORDER BY id DESC LIMIT %s",
                            (limit,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


class Consumer:
    """One named reader of the event log. Run a single instance per name."""

    CLAIM = """
        SELECT e.id, e.body FROM events e
        LEFT JOIN event_receipts r ON r.consumer = %(name)s AND r.event_id = e.id
        WHERE r.event_id IS NULL OR (r.status = 'retry' AND r.next_attempt_at <= now())
        ORDER BY e.id LIMIT %(batch)s"""

    def __init__(self, name: str, handler: Callable[[dict], object], use_shared=False,
                 max_attempts=5, retry_seconds=30, batch=20):
        self.name, self.handler, self.use_shared = name, handler, use_shared
        self.max_attempts, self.retry_seconds, self.batch = max_attempts, retry_seconds, batch

    def _receipt(self, cur, event_id: int, error: str | None) -> None:
        if error is None:
            cur.execute("""INSERT INTO event_receipts (consumer, event_id, status) VALUES (%s, %s, 'done')
                           ON CONFLICT (consumer, event_id) DO UPDATE SET status = 'done',
                             attempts = event_receipts.attempts + 1, last_error = NULL,
                             next_attempt_at = NULL, updated_at = now()""", (self.name, event_id))
            return
        cur.execute("""INSERT INTO event_receipts (consumer, event_id, status, last_error, next_attempt_at)
                       VALUES (%(c)s, %(e)s, CASE WHEN %(max)s <= 1 THEN 'dead' ELSE 'retry' END,
                               %(err)s, now() + make_interval(secs => %(wait)s))
                       ON CONFLICT (consumer, event_id) DO UPDATE SET
                         attempts = event_receipts.attempts + 1,
                         status = CASE WHEN event_receipts.attempts + 1 >= %(max)s THEN 'dead' ELSE 'retry' END,
                         last_error = %(err)s, updated_at = now(),
                         next_attempt_at = now() + make_interval(secs => %(wait)s * (event_receipts.attempts + 1))""",
                    {"c": self.name, "e": event_id, "max": self.max_attempts,
                     "err": error[:1000], "wait": self.retry_seconds})

    def poll(self) -> int:
        """Handle every event currently due. Returns how many were handled successfully."""
        handled = 0
        while True:
            conn = get_connection(self.use_shared)
            try:
                with conn, conn.cursor() as cur:
                    cur.execute(self.CLAIM, {"name": self.name, "batch": self.batch})
                    rows = cur.fetchall()
                if not rows:
                    return handled
                for event_id, body in rows:
                    try:
                        self.handler(body)
                        error = None
                    except Exception as e:  # noqa: BLE001 - recorded, retried later
                        error = f"{type(e).__name__}: {e}"
                        print(f"[{self.name}] event {event_id} failed ({error}); will retry")
                    with conn, conn.cursor() as cur:
                        self._receipt(cur, event_id, error)
                    handled += error is None
            finally:
                conn.close()

    def run(self, stop: threading.Event | None = None, idle_seconds=5.0,
            tick: Callable[[], None] | None = None) -> None:
        """Poll, then sleep until NOTIFY (or `idle_seconds`), forever or until `stop` is set.
        `tick` runs once per loop, for periodic jobs such as expiry sweeps."""
        stop = stop or threading.Event()
        listener = None
        while not stop.is_set():
            try:
                if listener is None:
                    listener = get_connection(self.use_shared)
                    listener.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
                    listener.cursor().execute(f"LISTEN {CHANNEL}")
                if tick:
                    tick()
                self.poll()
                if select.select([listener], [], [], idle_seconds)[0]:
                    listener.poll()
                    listener.notifies.clear()
            except Exception as e:  # noqa: BLE001 - database down: wait and reconnect
                print(f"[{self.name}] bus unavailable ({type(e).__name__}: {e}); retrying in 5s")
                if listener is not None:
                    try:
                        listener.close()
                    except Exception:  # noqa: BLE001
                        pass
                listener = None
                stop.wait(5)
        if listener is not None:
            listener.close()
