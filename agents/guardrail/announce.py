"""Publish the guardrail's decisions on the event log, in the shared event shape, so
the orchestrator knows whether a run may continue or must wait."""
from agents.guardrail.store import USE_SHARED
from shared.events import make_event, publish


def announce(correlation_id: str, environment: str, risk: str, action_ref: str,
             decision: str, decided_by: str | None, **extra) -> str:
    """decision: pending (-> approval_request) | approved | rejected (-> approval_result)."""
    event = make_event(
        "guardrail", "approval_request" if decision == "pending" else "approval_result",
        {"action_ref": str(action_ref), "decision": decision, "decided_by": decided_by, **extra},
        correlation_id, environment, risk)
    publish(event, use_shared=USE_SHARED)
    return event["event_id"]
