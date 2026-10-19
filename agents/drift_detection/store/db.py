"""SQLite persistence: batches, runs, feature results, reports, actions, models, audit log.

A new connection is opened per operation so the store is safe to use from the
API server, the scheduler thread and the CLI at the same time.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sim_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    scenario TEXT NOT NULL,
    created_at TEXT NOT NULL,
    n_rows INTEGER NOT NULL,
    labels_available INTEGER NOT NULL DEFAULT 0,
    model_version TEXT NOT NULL,
    path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pipeline_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    perf_batch_id INTEGER,
    created_at TEXT NOT NULL,
    model_version TEXT NOT NULL,
    severity TEXT NOT NULL,
    score REAL NOT NULL,
    report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feature_results (
    run_id INTEGER NOT NULL,
    feature TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    psi REAL,
    statistic REAL,
    p_value REAL,
    effect REAL,
    drift_score REAL,
    importance REAL,
    weighted REAL,
    PRIMARY KEY (run_id, feature)
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    llm_model TEXT,
    report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
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
CREATE TABLE IF NOT EXISTS model_versions (
    version TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    path TEXT NOT NULL,
    reference_path TEXT NOT NULL,
    parent TEXT,
    threshold REAL NOT NULL,
    metrics_json TEXT NOT NULL,
    importance_json TEXT NOT NULL,
    notes TEXT,
    is_current INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    run_id INTEGER,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_batch ON runs(batch_id);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log(run_id);
CREATE INDEX IF NOT EXISTS idx_feature_results_feature ON feature_results(feature);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        try:
            yield con
            con.commit()
        finally:
            con.close()

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
        """Clear production history; keep model versions."""
        with self.connect() as con:
            for table in ("sim_state", "batches", "pipeline_events", "runs",
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
            ids = [r[0] for r in con.execute(
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

    # ---- pipeline events --------------------------------------------------
    def add_event(self, batch_id: int, source: str, message: str) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO pipeline_events(batch_id, created_at, source, message) VALUES (?, ?, ?, ?)",
                (batch_id, now(), source, message),
            )

    def events_between(self, start_batch: int, end_batch: int) -> list[dict]:
        return self._all(
            "SELECT * FROM pipeline_events WHERE batch_id BETWEEN ? AND ? ORDER BY batch_id, id",
            (start_batch, end_batch),
        )

    # ---- runs ---------------------------------------------------------------
    def add_run(self, batch_id: int, perf_batch_id: int | None, model_version: str,
                severity: str, score: float, report: dict, features: list[dict]) -> int:
        with self.connect() as con:
            cur = con.execute(
                "INSERT INTO runs(batch_id, perf_batch_id, created_at, model_version, severity, score, report_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (batch_id, perf_batch_id, now(), model_version, severity, score, _dumps(report)),
            )
            run_id = int(cur.lastrowid)
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
        rows = self._all(
            "SELECT r.id, r.batch_id, r.perf_batch_id, r.created_at, r.model_version, r.severity, r.score, "
            "b.scenario, (SELECT COUNT(*) FROM reports p WHERE p.run_id = r.id) AS has_report "
            "FROM runs r LEFT JOIN batches b ON b.id = r.batch_id ORDER BY r.id DESC LIMIT ?",
            (limit,),
        )
        return rows

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
            cur = con.execute(
                "INSERT INTO reports(run_id, created_at, source, llm_model, report_json) VALUES (?, ?, ?, ?, ?)",
                (run_id, now(), source, llm_model, _dumps(report)),
            )
            return int(cur.lastrowid)

    def get_report(self, run_id: int) -> dict | None:
        row = self._one("SELECT * FROM reports WHERE run_id = ? ORDER BY id DESC LIMIT 1", (run_id,))
        if row:
            row["report"] = json.loads(row.pop("report_json"))
        return row

    # ---- actions ------------------------------------------------------------
    def add_action(self, run_id: int | None, action: str, risk: str, status: str,
                   payload: dict, result: dict | None = None) -> int:
        with self.connect() as con:
            cur = con.execute(
                "INSERT INTO actions(run_id, created_at, action, risk, status, payload_json, result_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, now(), action, risk, status, _dumps(payload), _dumps(result) if result else None),
            )
            return int(cur.lastrowid)

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
                "INSERT OR REPLACE INTO model_versions(version, created_at, path, reference_path, parent, "
                "threshold, metrics_json, importance_json, notes, is_current) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
