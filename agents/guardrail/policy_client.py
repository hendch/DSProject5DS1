"""Layer 1 client: ask OPA for a decision. If OPA is unreachable or answers
something unexpected, fail closed (hold for a human), never open."""
import os

import httpx
from dotenv import load_dotenv

load_dotenv()
OPA_URL = os.getenv("OPA_URL", "http://localhost:8181")
DECISIONS = {"auto_approve", "hold", "reject"}

FAIL_CLOSED = {
    "decision": "hold",
    "rule": "policy_engine_unavailable",
    "required_approvals": 1,
    "reason": "The policy engine could not be reached, so a human must decide.",
}


def evaluate(policy_input: dict) -> dict:
    try:
        resp = httpx.post(f"{OPA_URL}/v1/data/guardrail/decision",
                          json={"input": policy_input}, timeout=5)
        resp.raise_for_status()
        result = resp.json().get("result")
        if isinstance(result, dict) and result.get("decision") in DECISIONS:
            return result
        print(f"[warn] unexpected OPA answer {result!r}; failing closed")
    except Exception as e:  # noqa: BLE001 - connection refused, timeout, bad JSON...
        print(f"[warn] OPA unavailable ({e}); failing closed")
    return dict(FAIL_CLOSED)
