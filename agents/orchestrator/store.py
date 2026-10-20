"""Orchestrator persistence (db/migrations/004_orchestrator.sql): runs, their agent
steps and the proposals they wait on. A run's id is its correlation_id."""
import json
import os

import psycopg2.extras
from dotenv import load_dotenv

from shared.db import get_connection

load_dotenv()
USE_SHARED = os.getenv("ORCHESTRATOR_USE_SHARED", "false").lower() in ("1", "true", "yes")


def _query(sql, params=(), one=False):
    conn = get_connection(USE_SHARED)
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
            return (rows[0] if rows else None) if one else rows
    finally:
        conn.close()


def _j(value):
    return json.dumps(value, default=str)


# ---- runs -------------------------------------------------------------------------
def create_run(run_id, trigger_type, trigger, requested_by=None):
    _query("""INSERT INTO orchestrator.runs (id, trigger_type, trigger, requested_by)
              VALUES (%s, %s, %s, %s)""", (run_id, trigger_type, _j(trigger), requested_by))


def update_run(run_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = %({k})s" for k in fields)
    fields = {k: (_j(v) if k == "trigger" else v) for k, v in fields.items()}
    finished = ", finished_at = now()" if fields.get("status") in ("completed", "failed", "cancelled") else ""
    _query(f"UPDATE orchestrator.runs SET {cols}, updated_at = now(){finished} WHERE id = %(id)s",
           {**fields, "id": run_id})


def get_run(run_id):
    return _query("SELECT * FROM orchestrator.runs WHERE id = %s", (str(run_id),), one=True)


def list_runs(limit=50, status=None):
    where, params = "", []
    if status:
        where, params = "WHERE r.status = %s", [status]
    return _query(f"""
        SELECT r.*,
               coalesce((SELECT array_agg(s.agent ORDER BY s.step_no) FROM orchestrator.run_steps s
                         WHERE s.run_id = r.id), ARRAY[]::text[]) AS agents,
               (SELECT count(*) FROM orchestrator.run_actions a
                WHERE a.run_id = r.id AND a.decision = 'pending') AS pending
        FROM orchestrator.runs r {where} ORDER BY r.created_at DESC LIMIT %s""", (*params, limit))


def runs_with_status(*statuses):
    return _query("SELECT * FROM orchestrator.runs WHERE status = ANY(%s) ORDER BY created_at",
                  (list(statuses),))


def run_counts():
    rows = _query("SELECT status, count(*) AS n FROM orchestrator.runs GROUP BY status")
    counts = {r["status"]: r["n"] for r in rows}
    return {"total": sum(counts.values()), **{s: counts.get(s, 0) for s in (
        "running", "waiting_approval", "completed", "failed", "needs_triage")}}


# ---- steps --------------------------------------------------------------------------
def start_step(run_id, step_no, agent, input_):
    return _query("""INSERT INTO orchestrator.run_steps (run_id, step_no, agent, status, input)
                     VALUES (%s, %s, %s, 'running', %s)
                     ON CONFLICT (run_id, step_no) DO UPDATE SET status = 'running', started_at = now(),
                       finished_at = NULL, error = NULL, output = NULL
                     RETURNING id""", (run_id, step_no, agent, _j(input_)), one=True)["id"]


def finish_step(step_id, status, output=None, error=None):
    _query("""UPDATE orchestrator.run_steps SET status = %s, output = %s, error = %s, finished_at = now()
              WHERE id = %s""", (status, _j(output) if output is not None else None, error, step_id))


def get_steps(run_id):
    return _query("SELECT * FROM orchestrator.run_steps WHERE run_id = %s ORDER BY step_no", (str(run_id),))


# ---- proposals (run_actions) ----------------------------------------------------------
def add_action(action_id, run_id, step_id, agent, action_type):
    _query("""INSERT INTO orchestrator.run_actions (action_id, run_id, step_id, agent, action_type)
              VALUES (%s, %s, %s, %s, %s) ON CONFLICT (action_id) DO NOTHING""",
           (action_id, run_id, step_id, agent, action_type))


def record_decision(action_id, decision, decided_by):
    """Store a guardrail decision. Returns the run_id it belongs to, or None if not ours."""
    row = _query("""UPDATE orchestrator.run_actions SET decision = %s, decided_by = %s, decided_at = now()
                    WHERE action_id = %s AND decision = 'pending' RETURNING run_id""",
                 (decision, decided_by, str(action_id)), one=True)
    if row:
        return str(row["run_id"])
    row = _query("SELECT run_id FROM orchestrator.run_actions WHERE action_id = %s", (str(action_id),), one=True)
    return str(row["run_id"]) if row else None


def record_execution(action_id, execution, result):
    _query("""UPDATE orchestrator.run_actions SET execution = %s, execution_result = %s, executed_at = now()
              WHERE action_id = %s""", (execution, _j(result), str(action_id)))


def get_actions(run_id):
    """Proposals of a run with the guardrail's approval row and the votes so far."""
    return _query("""
        SELECT ra.*, act.payload, act.environment, act.blast_radius AS declared_risk,
               ap.id AS approval_id, ap.status AS approval_status, ap.risk, ap.policy_rule, ap.reason,
               ap.llm_summary, ap.required_approvals, ap.requested_by, ap.expires_at,
               coalesce((SELECT array_agg(v.voter ORDER BY v.voted_at) FROM approval_votes v
                         WHERE v.approval_id = ap.id AND v.vote = 'approve'), ARRAY[]::text[]) AS approved_by
        FROM orchestrator.run_actions ra
        JOIN actions act ON act.id = ra.action_id
        LEFT JOIN approvals ap ON ap.action_id = ra.action_id
        WHERE ra.run_id = %s ORDER BY act.created_at""", (str(run_id),))


def pending_approvals():
    return _query("""
        SELECT ap.id, ra.run_id, r.trigger->>'title' AS run_title, act.agent_name AS agent, act.action_type,
               act.environment, act.blast_radius, ap.risk, ap.policy_rule, ap.reason, ap.llm_summary,
               ap.required_approvals, ap.requested_by, ap.expires_at, ap.created_at,
               coalesce((SELECT array_agg(v.voter ORDER BY v.voted_at) FROM approval_votes v
                         WHERE v.approval_id = ap.id AND v.vote = 'approve'), ARRAY[]::text[]) AS approved_by
        FROM approvals ap
        JOIN actions act ON act.id = ap.action_id
        LEFT JOIN orchestrator.run_actions ra ON ra.action_id = ap.action_id
        LEFT JOIN orchestrator.runs r ON r.id = ra.run_id
        WHERE ap.status = 'pending' AND ap.expires_at > now()
        ORDER BY ap.created_at""")


# ---- audit and events for a run's timeline -----------------------------------------------
def audit_for(run_id):
    return _query("""SELECT id, created_at, agent_name, input_summary, decision FROM reasoning_logs
                     WHERE correlation_id = %s ORDER BY id""", (str(run_id),))


def audit_rows(limit=100):
    return _query("""SELECT id, created_at, agent_name, decision, input_summary, prev_hash, row_hash
                     FROM reasoning_logs ORDER BY id DESC LIMIT %s""", (limit,))


def broken_chain_rows():
    return _query("SELECT * FROM verify_reasoning_chain()")
