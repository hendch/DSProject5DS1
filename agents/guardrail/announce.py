"""Publish the guardrail's decisions back on the stream, in the shared event
shape, so the Orchestrator knows whether a run may continue or must wait."""
import json
import uuid
from datetime import datetime, timezone

from shared.redis_client import get_redis

STREAM = "pipeline_events"


def announce(correlation_id: str, environment: str, risk: str, action_ref: str,
             decision: str, decided_by: str | None, **extra) -> str:
    """decision: pending (-> approval_request) | approved | rejected (-> approval_result)."""
    event_id = str(uuid.uuid4())
    event = {
        "event_id": event_id, "correlation_id": str(correlation_id),
        "agent_name": "guardrail",
        "action_type": "approval_request" if decision == "pending" else "approval_result",
        "environment": environment, "blast_radius": risk,
        "payload": {"action_ref": str(action_ref), "decision": decision,
                    "decided_by": decided_by, **extra},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    get_redis().xadd(STREAM, {"data": json.dumps(event, default=str)})
    return event_id
