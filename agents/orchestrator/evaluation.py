"""Case benchmark: does LLM triage send free-text incidents to the right route?

Every case in eval/cases.json is an incident with the expected route (or none). The model
answers through routing.llm_triage, as in production; an answer only counts as routed when
its confidence reaches ORCHESTRATOR_TRIAGE_MIN_CONFIDENCE, otherwise the run would wait for a
human. "rules" is the keyword matcher alone, as a baseline.

    python -m agents.orchestrator.evaluation --model rules --model meta-llama/Llama-3.1-8B-Instruct

Runner, options and LangSmith experiments: shared/benchmark.py.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from agents.orchestrator import routing
from shared.benchmark import RULES, Suite, main
from shared.llm import LLMUnavailable

BASE = Path(__file__).parent / "eval"


def run_one(case: dict, model: str) -> dict:
    start = time.perf_counter()
    degraded = False
    if model == RULES:
        found = routing.keyword_routes(case["text"])
        route, confidence = (found[0], 1.0) if len(found) == 1 else (None, 0.0)
    else:
        try:
            route, confidence, _ = routing.llm_triage(case["text"])
        except LLMUnavailable as e:
            print(f"[warn] {case['id']}: {e}")
            route, confidence, degraded = None, 0.0, True
    routed = route if confidence >= routing.MIN_CONFIDENCE else None
    return {
        "seconds": round(time.perf_counter() - start, 2),
        "degraded": degraded,
        "correct": routed == case["route"],
        "wrong_route": routed is not None and routed != case["route"],
        "escalated": routed is None,
        "confidence": round(confidence, 2),
        "answer": route,
    }


def suite() -> Suite:
    data = json.loads((BASE / "cases.json").read_text(encoding="utf-8"))
    return Suite(name="triage", title="Orchestrator triage", agent="orchestrator", cases=data["cases"],
                 run_one=run_one,
                 scores={"correct": "Accuracy", "wrong_route": "Wrong route", "escalated": "Sent to a human",
                         "confidence": "Mean confidence"},
                 primary="correct", out_dir=BASE / "results",
                 meta={"min confidence": routing.MIN_CONFIDENCE},
                 note="Accuracy: the routed answer (confidence at or above the threshold) is the expected route, "
                      "or no route when none fits. Wrong route: confidently sent to the wrong agents, the costly "
                      "mistake. Sent to a human: below the threshold or no valid route, a safe miss.")


if __name__ == "__main__":
    main(default_suites=["triage"])
