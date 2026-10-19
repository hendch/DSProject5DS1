"""PostgreSQL persistence: batches, runs, feature results, reports, actions, models, audit log.

Same interface as the former SQLite store. Tables live in their own schema:
`drift` for the agent's normal workspace (runtime/), and one `drift_ws_<hash>` schema per
isolated workspace (the evaluation and the tests), so runs never mix. A new connection
is opened per operation, so the store is safe from the API server, the scheduler thread
and the CLI at the same time.

Every audit entry of the normal workspace is also copied into the platform's shared,
hash-chained `public.reasoning_logs`, so drift history survives a simulation reset.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import psycopg2.extras

from shared.db import get_connection

DEFAULT_SCHEMA = "drift"

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS {s};
CREATE TABLE IF NOT EXISTS {s}.sim_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS {s}.batches (
    id INTEGER PRIMARY KEY,
    scenario TEXT NOT NULL,
    created_at TEXT NOT NULL,
    n_rows INTEGER NOT NULL,
    labels_available INTEGER NOT NULL DEFAULT 0,
    model_version TEXT NOT NULL,
    path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS {s}.sim_events (
    id BIGSERIAL PRIMARY KEY,
    batch_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS {s}.runs (
    id BIGSERIAL PRIMARY KEY,
    batch_id INTEGER NOT NULL,
    perf_batch_id INTEGER,
    created_at TEXT NOT NULL,
    model_version TEXT NOT NULL,
    severity TEXT NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS {s}.feature_results (
    run_id BIGINT NOT NULL,
    feature TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    psi DOUBLE PRECISION,
    statistic DOUBLE PRECISION,
    p_value DOUBLE PRECISION,
    effect DOUBLE PRECISION,
    drift_score DOUBLE PRECISION,
    importance DOUBLE PRECISION,
    weighted DOUBLE PRECISION,
    PRIMARY KEY (run_id, feature)
);
CREATE TABLE IF NOT EXISTS {s}.reports (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    llm_model TEXT,
    report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS {s}.actions (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT,
    created_at TEXT NOT NULL,
    action TEXT NOT NULL,
    risk TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT,
    decided_at TEXT,
    decided_by TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS {s}.model_versions (
    version TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    path TEXT NOT NULL,
    reference_path TEXT NOT NULL,
    parent TEXT,
    threshold DOUBLE PRECISION NOT NULL,
    metrics_json TEXT NOT NULL,
    importance_json TEXT NOT NULL,
    notes TEXT,
    is_current INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS {s}.audit_log (
    id BIGSERIAL PRIMARY KEY,
    created_at TEXT NOT NULL,
    run_id BIGINT,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_batch ON {s}.runs(batch_id);
CREATE INDEX IF NOT EXISTS idx_audit_run ON {s}.audit_log(run_id);
CREATE INDEX IF NOT EXISTS idx_feature_results_feature ON {s}.feature_results(feature);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str)


def schema_for(path: Path) -> str:
    """`drift` for the normal workspace (…/runtime/drift_agent.db), a stable private schema
    for any other workspace, so isolated runs keep their own tables as before."""
    path = Path(path).resolve()
    if path.parent.name == "runtime":
        return DEFAULT_SCHEMA
    return "drift_ws_" + hashlib.sha1(str(path).encode()).hexdigest()[:10]


def run_correlation_id(run_id: int | None) -> str:
    """Stable correlation id for a drift run, shared with the orchestrator and the audit log."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"drift-run-{run_id}" if run_id else "drift-system"))


class _Con:
    """sqlite3-like facade over a psycopg2 connection: `?` placeholders, rows as dicts."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, args: tuple = ()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql.replace("?", "%s"), args)
        return cur

    def executemany(self, sql: str, rows: list) -> None:
        with self._conn.cursor() as cur:
            cur.executemany(sql.replace("?", "%s"), rows)


class Store:
    def __init__(self, path: Path, use_shared: bool = False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.schema = schema_for(self.path)
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", self.schema):
            raise ValueError(f"unsafe schema name {self.schema!r}")
        self.use_shared = use_shared
        with self.connect() as con:
            con.execute(SCHEMA.format(s=self.schema))

    @contextmanager
    def connect(self) -> Iterator[_Con]:
        conn = get_connection(self.use_shared)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {self.schema}, public")
            yield _Con(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    # ---- simulation state ---------------------------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM sim_state WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO sim_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def reset_simulation(self) -> None:
        """Clear production history; keep model versions. The shared audit log keeps its copy."""
        with self.connect() as con:
            for table in ("sim_state", "batches", "sim_events", "runs",
                          "feature_results", "reports", "actions", "audit_log"):
                con.execute(f"DELETE FROM {table}")

    # ---- batches --------------------------------------------------------------
    def add_batch(self, batch_id: int, scenario: str, n_rows: int, model_version: str, path: str) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO batches(id, scenario, created_at, n_rows, model_version, path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (batch_id, scenario, now(), n_rows, model_version, path),
            )

    def release_labels(self, up_to_batch: int) -> list[int]:
        with self.connect() as con:
            ids = [r["id"] for r in con.execute(
                "SELECT id FROM batches WHERE id <= ? AND labels_available = 0", (up_to_batch,)
            ).fetchall()]
            con.execute("UPDATE batches SET labels_available = 1 WHERE id <= ?", (up_to_batch,))
        return ids

    def get_batch(self, batch_id: int) -> dict | None:
        return self._one("SELECT * FROM batches WHERE id = ?", (batch_id,))

    def list_batches(self) -> list[dict]:
        return self._all("SELECT * FROM batches ORDER BY id")

    def latest_batch_id(self) -> int:
        row = self._one("SELECT MAX(id) AS m FROM batches")
        return int(row["m"]) if row and row["m"] is not None else 0

    def labelled_batches(self) -> list[dict]:
        return self._all("SELECT * FROM batches WHERE labels_available = 1 ORDER BY id")

    # ---- simulated pipeline events ------------------------------------------
    def add_event(self, batch_id: int, source: str, message: str) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO sim_events(batch_id, created_at, source, message) VALUES (?, ?, ?, ?)",
                (batch_id, now(), source, message),
            )

    def events_between(self, start_batch: int, end_batch: int) -> list[dict]:
        return self._all(
            "SELECT * FROM sim_events WHERE batch_id BETWEEN ? AND ? ORDER BY batch_id, id",
            (start_batch, end_batch),
        )

    # ---- runs ---------------------------------------------------------------
    def add_run(self, batch_id: int, perf_batch_id: int | None, model_version: str,
                severity: str, score: float, report: dict, features: list[dict]) -> int:
        with self.connect() as con:
            run_id = int(con.execute(
                "INSERT INTO runs(batch_id, perf_batch_id, created_at, model_version, severity, score, report_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (batch_id, perf_batch_id, now(), model_version, severity, score, _dumps(report)),
            ).fetchone()["id"])
            con.executemany(
                "INSERT INTO feature_results(run_id, feature, kind, status, psi, statistic, p_value, effect, "
                "drift_score, importance, weighted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(run_id, f["feature"], f["kind"], f["status"], f["psi"], f["statistic"], f["p_value"],
                  f["effect"], f["drift_score"], f["importance"], f["weighted"]) for f in features],
            )
        return run_id

    def update_run_report(self, run_id: int, report: dict) -> None:
        with self.connect() as con:
            con.execute("UPDATE runs SET report_json = ? WHERE id = ?", (_dumps(report), run_id))

    def get_run(self, run_id: int) -> dict | None:
        row = self._one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if row:
            row["report"] = json.loads(row.pop("report_json"))
        return row

    def list_runs(self, limit: int = 200) -> list[dict]:
        return self._all(
            "SELECT r.id, r.batch_id, r.perf_batch_id, r.created_at, r.model_version, r.severity, r.score, "
            "b.scenario, (SELECT COUNT(*) FROM reports p WHERE p.run_id = r.id) AS has_report "
            "FROM runs r LEFT JOIN batches b ON b.id = r.batch_id ORDER BY r.id DESC LIMIT ?",
            (limit,),
        )

    def latest_run(self) -> dict | None:
        row = self._one("SELECT id FROM runs ORDER BY id DESC LIMIT 1")
        return self.get_run(row["id"]) if row else None

    def run_for_batch(self, batch_id: int) -> dict | None:
        row = self._one("SELECT id FROM runs WHERE batch_id = ? ORDER BY id DESC LIMIT 1", (batch_id,))
        return self.get_run(row["id"]) if row else None

    def feature_results(self, run_id: int) -> list[dict]:
        return self._all(
            "SELECT * FROM feature_results WHERE run_id = ? ORDER BY weighted DESC", (run_id,)
        )

    def feature_history(self, feature: str, up_to_run: int | None = None, limit: int = 30) -> list[dict]:
        sql = ("SELECT r.id AS run_id, r.batch_id, f.status, f.psi, f.statistic, f.p_value, f.effect, "
               "f.drift_score FROM feature_results f JOIN runs r ON r.id = f.run_id WHERE f.feature = ?")
        args: list[Any] = [feature]
        if up_to_run is not None:
            sql += " AND r.id <= ?"
            args.append(up_to_run)
        sql += " ORDER BY r.id DESC LIMIT ?"
        args.append(limit)
        return list(reversed(self._all(sql, tuple(args))))

    def next_run(self, run_id: int) -> dict | None:
        row = self._one("SELECT id FROM runs WHERE id > ? ORDER BY id ASC LIMIT 1", (run_id,))
        return self.get_run(row["id"]) if row else None

    def latest_run_with_report(self) -> dict | None:
        row = self._one("SELECT run_id AS id FROM reports ORDER BY id DESC LIMIT 1")
        return self.get_run(row["id"]) if row else None

    def run_ids_with_report(self) -> set[int]:
        return {r["run_id"] for r in self._all("SELECT DISTINCT run_id FROM reports")}

    def all_runs(self) -> list[dict]:
        """Every run, oldest first, with its full detector report."""
        rows = self._all("SELECT * FROM runs ORDER BY id")
        for r in rows:
            r["report"] = json.loads(r.pop("report_json"))
        return rows

    def previous_run(self, run_id: int) -> dict | None:
        row = self._one("SELECT id FROM runs WHERE id < ? ORDER BY id DESC LIMIT 1", (run_id,))
        return self.get_run(row["id"]) if row else None

    # ---- reports ----------------------------------------------------------
    def add_report(self, run_id: int, source: str, llm_model: str | None, report: dict) -> int:
        with self.connect() as con:
            return int(con.execute(
                "INSERT INTO reports(run_id, created_at, source, llm_model, report_json) "
                "VALUES (?, ?, ?, ?, ?) RETURNING id",
                (run_id, now(), source, llm_model, _dumps(report)),
            ).fetchone()["id"])

    def get_report(self, run_id: int) -> dict | None:
        row = self._one("SELECT * FROM reports WHERE run_id = ? ORDER BY id DESC LIMIT 1", (run_id,))
        if row:
            row["report"] = json.loads(row.pop("report_json"))
        return row

    # ---- actions ------------------------------------------------------------
    def add_action(self, run_id: int | None, action: str, risk: str, status: str,
                   payload: dict, result: dict | None = None) -> int:
        with self.connect() as con:
            return int(con.execute(
                "INSERT INTO actions(run_id, created_at, action, risk, status, payload_json, result_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (run_id, now(), action, risk, status, _dumps(payload), _dumps(result) if result else None),
            ).fetchone()["id"])

    def update_action(self, action_id: int, status: str, result: dict | None = None,
                      decided_by: str | None = None, note: str | None = None) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE actions SET status = ?, result_json = COALESCE(?, result_json), "
                "decided_at = CASE WHEN ? IS NOT NULL THEN ? ELSE decided_at END, "
                "decided_by = COALESCE(?, decided_by), note = COALESCE(?, note) WHERE id = ?",
                (status, _dumps(result) if result else None, decided_by, now(), decided_by, note, action_id),
            )

    def _action_row(self, row: dict) -> dict:
        row["payload"] = json.loads(row.pop("payload_json"))
        raw = row.pop("result_json")
        row["result"] = json.loads(raw) if raw else None
        return row

    def get_action(self, action_id: int) -> dict | None:
        row = self._one("SELECT a.*, r.batch_id FROM actions a LEFT JOIN runs r ON r.id = a.run_id "
                        "WHERE a.id = ?", (action_id,))
        return self._action_row(row) if row else None

    def list_actions(self, status: str | None = None, run_id: int | None = None) -> list[dict]:
        sql, args = "SELECT a.*, r.batch_id FROM actions a LEFT JOIN runs r ON r.id = a.run_id WHERE 1=1", []
        if status:
            sql += " AND a.status = ?"
            args.append(status)
        if run_id is not None:
            sql += " AND a.run_id = ?"
            args.append(run_id)
        sql += " ORDER BY a.id DESC"
        return [self._action_row(r) for r in self._all(sql, tuple(args))]

    # ---- model versions -----------------------------------------------------
    def add_model_version(self, version: str, path: str, reference_path: str, parent: str | None,
                          threshold: float, metrics: dict, importance: dict, notes: str,
                          make_current: bool) -> None:
        with self.connect() as con:
            if make_current:
                con.execute("UPDATE model_versions SET is_current = 0")
            con.execute(
                "INSERT INTO model_versions(version, created_at, path, reference_path, parent, "
                "threshold, metrics_json, importance_json, notes, is_current) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (version) DO UPDATE SET created_at = excluded.created_at, path = excluded.path, "
                "reference_path = excluded.reference_path, parent = excluded.parent, "
                "threshold = excluded.threshold, metrics_json = excluded.metrics_json, "
                "importance_json = excluded.importance_json, notes = excluded.notes, "
                "is_current = excluded.is_current",
                (version, now(), path, reference_path, parent, threshold, _dumps(metrics),
                 _dumps(importance), notes, 1 if make_current else 0),
            )

    def set_current_model(self, version: str) -> None:
        with self.connect() as con:
            con.execute("UPDATE model_versions SET is_current = 0")
            con.execute("UPDATE model_versions SET is_current = 1 WHERE version = ?", (version,))

    def _model_row(self, row: dict) -> dict:
        row["metrics"] = json.loads(row.pop("metrics_json"))
        row["importance"] = json.loads(row.pop("importance_json"))
        return row

    def current_model(self) -> dict | None:
        row = self._one("SELECT * FROM model_versions WHERE is_current = 1")
        return self._model_row(row) if row else None

    def get_model(self, version: str) -> dict | None:
        row = self._one("SELECT * FROM model_versions WHERE version = ?", (version,))
        return self._model_row(row) if row else None

    def list_models(self) -> list[dict]:
        return [self._model_row(r) for r in self._all("SELECT * FROM model_versions ORDER BY created_at")]

    def delete_models_except(self, keep: str) -> None:
        with self.connect() as con:
            con.execute("DELETE FROM model_versions WHERE version != ?", (keep,))

    # ---- audit log ----------------------------------------------------------
    def audit(self, actor: str, event: str, payload: dict | None = None, run_id: int | None = None) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO audit_log(created_at, run_id, actor, event, payload_json) VALUES (?, ?, ?, ?, ?)",
                (now(), run_id, actor, event, _dumps(payload or {})),
            )
            if self.schema == DEFAULT_SCHEMA:  # the platform's tamper-evident copy
                con.execute(
                    "INSERT INTO public.reasoning_logs (correlation_id, agent_name, input_summary, "
                    "reasoning_trace, decision) VALUES (?, 'drift_detection', ?, ?, ?)",
                    (run_correlation_id(run_id), event,
                     _dumps({"actor": actor, "run_id": run_id, **(payload or {})}), f"{actor}: {event}"),
                )

    def list_audit(self, run_id: int | None = None, actor: str | None = None, limit: int = 500) -> list[dict]:
        sql, args = "SELECT * FROM audit_log WHERE 1=1", []
        if run_id is not None:
            sql += " AND run_id = ?"
            args.append(run_id)
        if actor:
            sql += " AND actor = ?"
            args.append(actor)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = self._all(sql, tuple(args))
        for r in rows:
            r["payload"] = json.loads(r.pop("payload_json"))
        return rows
