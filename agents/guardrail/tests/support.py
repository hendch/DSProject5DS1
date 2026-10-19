"""Guardrail test helpers: event builders on top of shared.testing (a fresh, fully
migrated PostgreSQL database per test; no Docker needed)."""
import uuid
from datetime import datetime, timezone

from shared.testing import DbTestCase as _SharedDbTestCase


class DbTestCase(_SharedDbTestCase):
    """A fresh database per test, plus guardrail-specific helpers."""

    def publish(self, **overrides) -> dict:
        """Publish a real agent event through shared.publish_and_log, so the
        actions row exists exactly as it would in the pipeline."""
        from shared.events import publish_and_log
        e = event(**overrides)
        e["event_id"] = publish_and_log(
            agent_name=e["agent_name"], action_type=e["action_type"], payload=e["payload"],
            correlation_id=e["correlation_id"], environment=e["environment"],
            blast_radius=e["blast_radius"], input_summary="test")
        return e


def event(**overrides) -> dict:
    e = {
        "event_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
        "agent_name": "dependency_resolver", "action_type": "propose_fix",
        "environment": "dev", "blast_radius": "low",
        "payload": {"cycle": ["terraform_data.a", "terraform_data.b"],
                    "category": "modeling_mistake", "proposed_diff": "Split the resources.",
                    "sources": {"detection": "tofu", "classification": "llm", "explanation": "llm"}},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload = overrides.pop("payload", None)
    e.update(overrides)
    if payload is not None:
        e["payload"] = {**e["payload"], **payload}
    return e
