"""Layer 2: validate an event and compute its real risk from evidence.

Agents label their own blast_radius, and today they all say "low". The
guardrail does not take that on trust: it scans what the action actually
touches and how reliable the agent's output was, and can only RAISE the
label, never lower it.

Everything here is pure (no I/O), so it is fully unit-testable.
"""
import json
import re

ENVIRONMENTS = ("dev", "staging", "prod")
LEVELS = ("low", "medium", "high")
AGENTS = ("dependency_resolver", "diagnostic_rca", "guardrail", "merge_assistant", "orchestrator")
ACTION_TYPES = ("propose_fix", "rank_causes", "flag_conflict", "approval_request", "approval_result")
REQUIRED = ("event_id", "correlation_id", "agent_name", "action_type",
            "environment", "blast_radius", "payload", "timestamp")

# Action types that only give advice and never change anything once approved.
ADVISORY = {"rank_causes"}

# Word edges where "_" and "-" also count as separators (plain \b does not
# fire inside names like aws_kms_key or app_prod_db).
_B, _E = r"(?<![a-z0-9])", r"(?![a-z0-9])"


def _words(*words: str) -> str:
    return f"{_B}({'|'.join(words)}){_E}"


# (pattern, level, reason). Matched against the lower-cased payload text.
SIGNALS = [
    (_words(r"prod", r"production"), "high", "touches production"),
    (_words(r"destroy", r"delete", r"drop\s+(table|database)", r"truncate", r"force-unlock",
            r"rm\s+-rf", r"terminate"), "high", "destructive operation"),
    (_words(r"iam", r"kms", r"secrets?", r"security_group", r"firewall", r"azurerm_role\w*"),
     "high", "changes permissions or security boundaries"),
    (_words(r"gpu", r"[pg][3-6]", r"nat_gateway", r"instance", r"db_instance", r"rds", r"eks_cluster",
            r"autoscaling\w*"), "medium", "can create billable infrastructure"),
    (_words(r"backend", r"tfstate", r"state\s+lock"), "medium", "touches infrastructure state"),
]

# Output produced by a fallback (no LLM, no tofu) is less reliable.
LOW_TRUST_SOURCES = {"heuristic", "static"}


def validate(event: dict) -> list[str]:
    """Return a list of problems; empty means the event is well-formed."""
    if not isinstance(event, dict):
        return ["event is not a JSON object"]
    problems = [f"missing field '{f}'" for f in REQUIRED if f not in event]
    checks = [("environment", ENVIRONMENTS), ("blast_radius", LEVELS),
              ("agent_name", AGENTS), ("action_type", ACTION_TYPES)]
    for field, allowed in checks:
        if field in event and event[field] not in allowed:
            problems.append(f"invalid {field} {event[field]!r}")
    if "payload" in event and not isinstance(event["payload"], dict):
        problems.append("payload is not an object")
    return problems


def raise_to(current: str, level: str) -> str:
    return max(current, level, key=LEVELS.index)


def assess(event: dict) -> tuple[str, list[dict]]:
    """Return (risk, signals). risk is never lower than the agent's own label."""
    declared = event.get("blast_radius", "high")
    risk = declared if declared in LEVELS else "high"
    signals = [{"signal": "declared_by_agent", "level": risk}]

    payload = event.get("payload") or {}
    if event.get("action_type") not in ADVISORY:  # advice cannot break anything
        text = json.dumps(payload).lower()
        for pattern, level, reason in SIGNALS:
            m = re.search(pattern, text)
            if m:
                signals.append({"signal": reason, "level": level, "evidence": m.group(0)})
                risk = raise_to(risk, level)

        sources = payload.get("sources") or {}
        weak = sorted(k for k, v in sources.items() if v in LOW_TRUST_SOURCES)
        if weak:
            bumped = LEVELS[min(LEVELS.index(risk) + 1, 2)]
            signals.append({"signal": "agent used fallback output", "level": bumped,
                            "evidence": ", ".join(weak)})
            risk = bumped

    return risk, signals
