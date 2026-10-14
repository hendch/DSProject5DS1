"""Postgres side of the guardrail: approvals, human votes, audit log.

Each public function runs in ONE transaction, so an approval and its audit
row are written together or not at all. Needs db/migrations/002_guardrail.sql.
"""
import json
import os
import uuid

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from shared.db import get_connection

load_dotenv()
USE_SHARED = os.getenv("GUARDRAIL_USE_SHARED", "false").lower() in ("1", "true", "yes")
APPROVAL_TTL_HOURS = float(os.getenv("GUARDRAIL_APPROVAL_TTL_HOURS", "4"))
POLICY_ACTOR = "guardrail:policy"

STATUS_FOR = {"auto_approve": "approved", "reject": "rejected", "hold": "pending"}


class ApprovalError(Exception):
    """A human decision that cannot be applied. http_status maps to the API response."""

    def __init__(self, http_status: int, message: str):
        super().__init__(message)
        self.http_status = http_status


def connect():
    return get_connection(USE_SHARED)


def _log(cur, correlation_id, input_summary, trace, decision):
    cur.execute(
        """INSERT INTO reasoning_logs (correlation_id, agent_name, input_summary,
                                      reasoning_trace, decision, confidence)
           VALUES (%s, 'guardrail', %s, %s, %s, NULL)""",  # deterministic step: no confidence
        (correlation_id, input_summary, json.dumps(trace), decision),
    )


def record_decision(event: dict, risk: str, signals: list, verdict: dict,
                    llm: dict | None, requested_by: str | None) -> dict | None:
    """Write the approval row + its audit row. Returns the approval, or None if
    this action was already decided (the event is being redelivered)."""
    status = STATUS_FOR[verdict["decision"]]
    conn = connect()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO approvals (action_id, status, decided_by, decided_at, risk, policy_rule,
                                          reason, llm_summary, required_approvals, requested_by, expires_at)
                   VALUES (%(action)s, %(status)s, %(by)s, CASE WHEN %(final)s THEN now() END,
                           %(risk)s, %(rule)s, %(reason)s, %(llm)s, %(required)s, %(requested_by)s,
                           CASE WHEN %(final)s THEN NULL ELSE now() + make_interval(secs => %(ttl)s) END)
                   ON CONFLICT (action_id) DO NOTHING
                   RETURNING id, status, decided_by, required_approvals, expires_at""",
                {"action": event["event_id"], "status": status, "final": status != "pending",
                 "by": None if status == "pending" else POLICY_ACTOR, "risk": risk,
                 "rule": verdict["rule"], "reason": verdict["reason"],
                 "llm": llm["summary"] if llm else None,
                 "required": max(1, int(verdict.get("required_approvals", 1))),
                 "requested_by": requested_by, "ttl": APPROVAL_TTL_HOURS * 3600},
            )
            approval = cur.fetchone()
            if approval is None:
                return None
            _log(cur, event["correlation_id"],
                 f"{event['action_type']} from {event['agent_name']} "
                 f"({event['environment']}, declared {event['blast_radius']})",
                 {"action_ref": event["event_id"], "declared_blast_radius": event["blast_radius"],
                  "computed_risk": risk, "signals": signals, "policy": verdict, "llm_judge": llm},
                 f"{verdict['decision']}: {verdict['rule']}")
        return {**approval, "id": str(approval["id"])}
    finally:
        conn.close()


def record_malformed(event: dict, problems: list[str]) -> None:
    """Audit a malformed event that cannot get an approval row (no valid action)."""
    try:
        corr = str(uuid.UUID(str(event.get("correlation_id"))))
    except (AttributeError, ValueError):
        corr = str(uuid.UUID(int=0))
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            _log(cur, corr, "malformed event", {"problems": problems, "event": event},
                 "reject: malformed_event")
    finally:
        conn.close()


PENDING_SQL = """
    SELECT a.id, a.action_id, a.status, a.risk, a.policy_rule, a.reason, a.llm_summary,
           a.required_approvals, a.requested_by, a.created_at, a.expires_at,
           act.correlation_id, act.agent_name, act.action_type, act.environment,
           act.blast_radius, act.payload,
           coalesce(array_agg(v.voter) FILTER (WHERE v.vote = 'approve'), ARRAY[]::text[]) AS approved_by
    FROM approvals a
    JOIN actions act ON act.id = a.action_id
    LEFT JOIN approval_votes v ON v.approval_id = a.id
    WHERE {where}
    GROUP BY a.id, act.id
    ORDER BY a.created_at
"""


def list_pending() -> list[dict]:
    return _query(PENDING_SQL.format(where="a.status = 'pending' AND a.expires_at > now()"), ())


def get_approval(approval_id: str) -> dict | None:
    rows = _query(PENDING_SQL.format(where="a.id = %s"), (approval_id,))
    return rows[0] if rows else None


def _query(sql, params) -> list[dict]:
    conn = connect()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def cast_vote(approval_id: str, voter: str, vote: str) -> dict:
    """Apply one human vote atomically. Raises ApprovalError when refused."""
    voter = voter.strip()
    if not voter:
        raise ApprovalError(400, "decided_by is required")
    conn = connect()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # FOR UPDATE: two people clicking at the same moment are served one
            # after the other, so the second one sees the first one's decision.
            cur.execute(
                """SELECT a.*, a.expires_at < now() AS expired, act.correlation_id,
                          act.environment, act.agent_name, act.action_type
                   FROM approvals a JOIN actions act ON act.id = a.action_id
                   WHERE a.id = %s FOR UPDATE OF a""", (approval_id,))
            a = cur.fetchone()
            if a is None:
                raise ApprovalError(404, "approval not found")
            if a["status"] == "expired":
                raise ApprovalError(410, "approval expired before a decision was made")
            if a["status"] != "pending":
                raise ApprovalError(409, f"already {a['status']} by {a['decided_by']}")
            if a["expired"]:
                cur.execute("UPDATE approvals SET status = 'expired', decided_at = now() WHERE id = %s",
                            (approval_id,))
                _log(cur, a["correlation_id"], f"vote by {voter} on expired approval",
                     {"action_ref": str(a["action_id"])}, "expired")
                conn.commit()
                raise ApprovalError(410, "approval expired before a decision was made")
            if a["requested_by"] and a["requested_by"] == voter:
                raise ApprovalError(403, "you cannot approve an action you triggered")

            try:
                cur.execute("INSERT INTO approval_votes (approval_id, voter, vote) VALUES (%s, %s, %s)",
                            (approval_id, voter, vote))
            except psycopg2.errors.UniqueViolation:
                raise ApprovalError(409, f"{voter} has already voted on this approval") from None

            cur.execute("""SELECT voter FROM approval_votes WHERE approval_id = %s AND vote = 'approve'
                           ORDER BY voted_at""", (approval_id,))
            approvers = [r["voter"] for r in cur.fetchall()]
            if vote == "reject":
                status, decided_by = "rejected", voter
            elif len(approvers) >= a["required_approvals"]:
                status, decided_by = "approved", ", ".join(approvers)
            else:
                status, decided_by = "pending", None
            if status != "pending":
                cur.execute("""UPDATE approvals SET status = %s, decided_by = %s, decided_at = now()
                               WHERE id = %s""", (status, decided_by, approval_id))
            _log(cur, a["correlation_id"], f"{vote} vote by {voter}",
                 {"action_ref": str(a["action_id"]), "approvals_so_far": approvers,
                  "required_approvals": a["required_approvals"]},
                 f"{status}" + (f" by {decided_by}" if decided_by else
                                f" ({len(approvers)}/{a['required_approvals']} approvals)"))
        return {"approval_id": approval_id, "action_ref": str(a["action_id"]),
                "correlation_id": str(a["correlation_id"]), "environment": a["environment"],
                "risk": a["risk"], "status": status, "decided_by": decided_by,
                "approvals": approvers, "required_approvals": a["required_approvals"]}
    finally:
        conn.close()


def expire_overdue() -> list[dict]:
    """Pending approvals nobody decided in time become 'expired' (never approved)."""
    conn = connect()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """UPDATE approvals a SET status = 'expired', decided_at = now()
                   FROM actions act
                   WHERE act.id = a.action_id AND a.status = 'pending' AND a.expires_at < now()
                   RETURNING a.id, a.action_id, a.risk, act.correlation_id, act.environment""")
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                _log(cur, r["correlation_id"], "approval expired without a decision",
                     {"action_ref": str(r["action_id"])}, "expired")
        return rows
    finally:
        conn.close()


def verify_chain() -> list[dict]:
    return _query("SELECT * FROM verify_reasoning_chain()", ())
