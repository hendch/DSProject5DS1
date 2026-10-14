"""Guardrail Agent: the gate every proposed action passes through.

    receive -> validate -> compute real risk -> OPA policy -> LLM may escalate
            -> save approval + hashed audit row -> ack -> announce

It never executes anything. It decides whether an action is auto-approved,
held for a human, or rejected, and records why.

Run:      python -m agents.guardrail.agent           (loop forever)
          python -m agents.guardrail.agent --once    (drain the stream, then exit)
          python -m agents.guardrail.agent --verify  (check the audit hash chain)
"""
import json
import os
import socket
import sys
import time

import psycopg2
import redis
from dotenv import load_dotenv

from agents.guardrail import llm_judge, policy_client, store
from agents.guardrail.announce import STREAM, announce
from agents.guardrail.risk import ADVISORY, assess, validate
from shared.redis_client import get_redis

load_dotenv()
GROUP = "guardrail"
CONSUMER = os.getenv("GUARDRAIL_CONSUMER", f"guardrail-{socket.gethostname()}")
DEAD_LETTER = "pipeline_events_dead"
MAX_DELIVERIES = 5             # after this many failed attempts, park the event
RECLAIM_IDLE_MS = 30_000       # retry events a crashed/failed attempt left unacknowledged
EXPIRY_CHECK_SECONDS = 60
ACTION_ROW_WAITS = 10          # x 0.2 s: wait for the agent's actions row (see handle)
OWN_EVENTS = {"approval_request", "approval_result"}


def decide(event: dict) -> tuple[str, list, dict, dict | None]:
    """Layers 2 -> 1 -> 3. Returns (risk, signals, verdict, llm_review)."""
    risk, signals = assess(event)
    verdict = policy_client.evaluate({**event, "risk": risk})

    llm = None
    if verdict["decision"] != "reject" and event["action_type"] not in ADVISORY:
        llm = llm_judge.review(event, risk, signals)
        if llm and llm["risky"] and verdict["decision"] == "auto_approve":
            # The LLM can only escalate: auto_approve -> hold. Never the other way.
            verdict = {"decision": "hold", "rule": "llm_escalation", "required_approvals": 1,
                       "reason": f"The policy allowed it, but the LLM reviewer flagged it: {llm['summary']}",
                       "policy_verdict": verdict}
    return risk, signals, verdict, llm


def process_event(event: dict) -> str:
    """Decide one event end to end. Returns an outcome label (for logs and tests).
    Raises on infrastructure errors so the caller does NOT acknowledge the event."""
    if isinstance(event, dict) and (event.get("agent_name") == "guardrail"
                                    or event.get("action_type") in OWN_EVENTS):
        return "skipped"  # our own announcements: never gate ourselves
    problems = validate(event)
    if problems:
        store.record_malformed(event, problems)
        print(f"[guardrail] rejected malformed event: {problems}")
        return "malformed"

    risk, signals, verdict, llm = decide(event)
    payload = event["payload"]
    requested_by = event.get("triggered_by") or payload.get("triggered_by")
    approval = store.record_decision(event, risk, signals, verdict, llm, requested_by)
    if approval is None:
        return "duplicate"  # redelivered after a crash: already decided

    announce(event["correlation_id"], event["environment"], risk, event["event_id"],
             approval["status"], approval["decided_by"], approval_id=approval["id"],
             rule=verdict["rule"], reason=verdict["reason"],
             required_approvals=approval["required_approvals"])
    print(f"[guardrail] {event['agent_name']}/{event['action_type']} "
          f"({event['environment']}, declared {event['blast_radius']} -> risk {risk}): "
          f"{verdict['decision']} [{verdict['rule']}]")
    return approval["status"]


def handle(r: redis.Redis, msg_id, fields: dict) -> None:
    raw = fields.get(b"data") or fields.get("data") or b""
    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        event = {"raw": raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)}
    try:
        for attempt in range(ACTION_ROW_WAITS + 1):
            try:
                process_event(event)
                break
            except psycopg2.errors.ForeignKeyViolation:
                # publish_and_log writes Redis before Postgres, so we can wake up
                # before the agent's actions row is committed. Give it a moment.
                if attempt == ACTION_ROW_WAITS:
                    raise
                time.sleep(0.2)
    except Exception as e:  # noqa: BLE001 - DB/Redis down, action row never written...
        print(f"[warn] could not process {msg_id!r} ({type(e).__name__}: {e}); will retry")
        return  # not acknowledged: it stays pending and is reclaimed later
    r.xack(STREAM, GROUP, msg_id)


def reclaim(r: redis.Redis) -> None:
    """Retry events left unacknowledged; park the ones that keep failing."""
    _, messages, *_ = r.xautoclaim(STREAM, GROUP, CONSUMER, RECLAIM_IDLE_MS, "0-0", count=20)
    for msg_id, fields in messages:
        if fields is None:  # deleted from the stream meanwhile
            r.xack(STREAM, GROUP, msg_id)
            continue
        info = r.xpending_range(STREAM, GROUP, min=msg_id, max=msg_id, count=1)
        if info and info[0]["times_delivered"] > MAX_DELIVERIES:
            r.xadd(DEAD_LETTER, {**fields, "original_id": msg_id})
            r.xack(STREAM, GROUP, msg_id)
            print(f"[guardrail] parked {msg_id!r} in {DEAD_LETTER} after {MAX_DELIVERIES} failures")
        else:
            handle(r, msg_id, fields)


def expire_and_announce() -> None:
    for row in store.expire_overdue():
        announce(row["correlation_id"], row["environment"], row["risk"], row["action_id"],
                 "rejected", None, approval_id=str(row["id"]), reason="expired without a decision")
        print(f"[guardrail] approval {row['id']} expired")


def ensure_group(r: redis.Redis) -> None:
    try:  # "0": on first start, also decide events published before the guardrail existed
        r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def run(once: bool = False) -> None:
    r = get_redis()
    ensure_group(r)
    print(f"[guardrail] consuming '{STREAM}' as {GROUP}/{CONSUMER} (OPA at {policy_client.OPA_URL})")
    last_expiry = 0.0
    while True:
        if time.monotonic() - last_expiry > EXPIRY_CHECK_SECONDS:
            try:
                expire_and_announce()
            except Exception as e:  # noqa: BLE001
                print(f"[warn] expiry sweep failed: {e}")
            last_expiry = time.monotonic()
        reclaim(r)
        batch = r.xreadgroup(GROUP, CONSUMER, {STREAM: ">"}, count=10, block=None if once else 5000)
        if once and not batch:
            return
        for _, messages in batch or []:
            for msg_id, fields in messages:
                handle(r, msg_id, fields)


if __name__ == "__main__":
    if "--verify" in sys.argv:
        broken = store.verify_chain()
        print("Audit log intact." if not broken else f"Audit log BROKEN at {len(broken)} row(s):")
        for b in broken:
            print(f"  row {b['id']}: {b['problem']}")
        sys.exit(1 if broken else 0)
    run(once="--once" in sys.argv)
