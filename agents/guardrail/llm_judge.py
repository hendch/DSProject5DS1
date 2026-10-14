"""Layer 3: an LLM reviewer that can only make things stricter.

It reads the proposal and says whether it looks risky. The guardrail uses
the answer to ESCALATE an auto-approval to a human hold, and to give the
human reviewer a plain-English summary. It can never approve anything, so
a fooled or broken model cannot open the gate.

The proposal text was written by other LLMs and may contain instructions
("this is safe, approve it"). It is passed as clearly delimited data.

Which LLM answers (local Ollama or a hosted API) is set in .env, see llm_client.py.
"""
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from agents.guardrail import llm_client

load_dotenv()
ENABLED = os.getenv("GUARDRAIL_LLM", "on").lower() not in ("off", "0", "false")
TIMEOUT = float(os.getenv("GUARDRAIL_LLM_TIMEOUT", "60"))
PROMPT = (Path(__file__).parent / "prompts/judge.txt").read_text()
MAX_PAYLOAD_CHARS = 4000
# Circuit breaker: after a failure, skip the LLM for a while instead of paying
# a timeout on every event while the LLM is down or rate-limited.
COOLDOWN_SECONDS = float(os.getenv("GUARDRAIL_LLM_COOLDOWN", "30"))
_down_until = 0.0


def review(event: dict, risk: str, signals: list[dict]) -> dict | None:
    """Return {"risky": bool, "summary": str}, or None if the LLM is off or
    unavailable (the policy decision then stands unchanged)."""
    global _down_until
    if not ENABLED or time.monotonic() < _down_until:
        return None
    facts = {k: event.get(k) for k in ("agent_name", "action_type", "environment")}
    facts["computed_risk"] = risk
    facts["risk_signals"] = [s["signal"] for s in signals]
    untrusted = json.dumps(event.get("payload"), indent=2)[:MAX_PAYLOAD_CHARS]
    message = (f"FACTS (trusted):\n{json.dumps(facts, indent=2)}\n\n"
               f"<untrusted_proposal>\n{untrusted}\n</untrusted_proposal>\n\nJSON answer:")
    try:
        answer = llm_client.chat_json(PROMPT, message, timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001 - LLM down, no key, rate limit (429), bad JSON...
        _down_until = time.monotonic() + COOLDOWN_SECONDS
        print(f"[warn] LLM judge unavailable ({e}); keeping the policy decision, "
              f"skipping the LLM for {COOLDOWN_SECONDS:.0f}s")
        return None
    risky = answer.get("risky")
    if not isinstance(risky, bool):  # "yes", "maybe", missing: treat as risky
        risky = True
    summary = str(answer.get("summary", "")).strip()[:500] or "(no summary)"
    return {"risky": risky, "summary": summary, "model": llm_client.label()}
