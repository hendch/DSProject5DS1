"""Guardrail Agent: the gate every proposed action passes through.

    receive -> validate -> compute real risk -> OPA policy -> LLM may escalate
            -> save approval + hashed audit row -> receipt -> announce

It never executes anything. It decides whether an action is auto-approved,
held for a human, or rejected, and records why.

Run:      python -m agents.guardrail.agent           (loop forever)
          python -m agents.guardrail.agent --once    (handle pending events, then exit)
          python -m agents.guardrail.agent --verify  (check the audit hash chain)
"""
import sys
import threading
import time

from dotenv import load_dotenv

from agents.guardrail import llm_judge, policy_client, store
from agents.guardrail.announce import announce
from agents.guardrail.risk import ADVISORY, assess, validate
from shared import tracing
from shared.events import Consumer

load_dotenv()
CONSUMER = "guardrail"          # its receipts in event_receipts
MAX_ATTEMPTS = 5                # after this many failures an event is marked dead
EXPIRY_CHECK_SECONDS = 60
SKIP_AGENTS = {"guardrail", "orchestrator"}   # decisions and run lifecycle: never gated
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
    if isinstance(event, dict) and (event.get("agent_name") in SKIP_AGENTS
                                    or event.get("action_type") in OWN_EVENTS):
        return "skipped"  # decisions and lifecycle events: never gate ourselves
    problems = validate(event)
    if problems:
        store.record_malformed(event, problems)
        print(f"[guardrail] rejected malformed event: {problems}")
        return "malformed"

    with tracing.span(f"guardrail: {event['agent_name']}/{event['action_type']}",
                      inputs={"action_type": event["action_type"], "environment": event["environment"],
                              "declared": event["blast_radius"]},
                      metadata={"run_id": event["correlation_id"], "thread_id": event["correlation_id"],
                                "agent": "guardrail"}, tags=["guardrail"]) as span:
        risk, signals, verdict, llm = decide(event)
        span.outputs({"risk": risk, "decision": verdict["decision"], "rule": verdict["rule"],
                      "llm_risky": (llm or {}).get("risky")})
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


def expire_and_announce() -> None:
    for row in store.expire_overdue():
        announce(row["correlation_id"], row["environment"], row["risk"], row["action_id"],
                 "rejected", None, approval_id=str(row["id"]), reason="expired without a decision")
        print(f"[guardrail] approval {row['id']} expired")


def consumer() -> Consumer:
    # Each event is decided once (UNIQUE action_id), so redelivery after a crash is safe.
    return Consumer(CONSUMER, process_event, use_shared=store.USE_SHARED, max_attempts=MAX_ATTEMPTS)


def run(once: bool = False, stop: threading.Event | None = None) -> None:
    """Decide every event on the bus. `once`: handle what is there now, then return."""
    last_expiry = [0.0]

    def tick():
        if time.monotonic() - last_expiry[0] > EXPIRY_CHECK_SECONDS:
            try:
                expire_and_announce()
            except Exception as e:  # noqa: BLE001
                print(f"[warn] expiry sweep failed: {e}")
            last_expiry[0] = time.monotonic()

    print(f"[guardrail] consuming the event log as '{CONSUMER}' (OPA at {policy_client.OPA_URL})")
    if once:
        tick()
        consumer().poll()
    else:
        consumer().run(stop=stop, tick=tick)


if __name__ == "__main__":
    if "--verify" in sys.argv:
        broken = store.verify_chain()
        print("Audit log intact." if not broken else f"Audit log BROKEN at {len(broken)} row(s):")
        for b in broken:
            print(f"  row {b['id']}: {b['problem']}")
        sys.exit(1 if broken else 0)
    run(once="--once" in sys.argv)
