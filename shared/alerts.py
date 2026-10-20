"""Alerts shown in the app (db/migrations/006_github_alerts.sql) and app settings."""
import json

import psycopg2.extras

from shared.db import get_connection

SEVERITIES = ("critical", "warning", "info", "success")


def _query(sql, params=(), one=False):
    conn = get_connection()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
            return (rows[0] if rows else None) if one else rows
    finally:
        conn.close()


def raise_alert(severity: str, title: str, message: str = "", source: str = "system",
                link: str | None = None, dedupe_key: str | None = None) -> int | None:
    """Create an alert. With a dedupe_key, the same alert is only ever raised once.
    Returns its id, or None when it was a duplicate. Never raises: alerts must not break callers."""
    if severity not in SEVERITIES:
        severity = "info"
    try:
        row = _query("""INSERT INTO alerts (severity, title, message, source, link, dedupe_key)
                        VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (dedupe_key) DO NOTHING RETURNING id""",
                     (severity, title[:200], (message or "")[:2000], source, link, dedupe_key), one=True)
        return row["id"] if row else None
    except Exception as e:  # noqa: BLE001
        print(f"[alerts] could not record alert {title!r}: {e}")
        return None


def list_alerts(limit=100):
    rows = _query("""SELECT id, severity, title, message, source, link, created_at, read_at IS NOT NULL AS read
                     FROM alerts ORDER BY id DESC LIMIT %s""", (limit,))
    return rows


def unread_count() -> int:
    return _query("SELECT count(*) AS n FROM alerts WHERE read_at IS NULL", one=True)["n"]


def mark_read(ids) -> int:
    if ids == "all":
        rows = _query("UPDATE alerts SET read_at = now() WHERE read_at IS NULL RETURNING id")
    else:
        rows = _query("UPDATE alerts SET read_at = now() WHERE read_at IS NULL AND id = ANY(%s) RETURNING id",
                      ([int(i) for i in ids],))
    return len(rows)


def get_setting(key: str, default=None):
    row = _query("SELECT value FROM app_settings WHERE key = %s", (key,), one=True)
    return row["value"] if row else default


def set_setting(key: str, value) -> None:
    _query("""INSERT INTO app_settings (key, value) VALUES (%s, %s)
              ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
           (key, json.dumps(value)))


def delete_setting(key: str) -> None:
    _query("DELETE FROM app_settings WHERE key = %s", (key,))
