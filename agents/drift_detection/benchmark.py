"""Drift Detection as a suite of the shared benchmark runner (shared/benchmark.py), so its
models can be compared next to the other agents' in LangSmith.

One case per (scenario, seed) from config.yaml, each run and scored by evaluation.run_one
(the agent's own benchmark). Detection and false alarms come from the statistics and do not
depend on the model; diagnosis and action do. The model is picked through XAI_MODEL (served by
XAI_BASE_URL); "rules" is the rule-based investigator, as a baseline.

    python -m agents.drift_detection.benchmark --model rules --model grok-4.20-0309-reasoning
"""

from __future__ import annotations

import time
from pathlib import Path

from agents.drift_detection.config import get_settings
from agents.drift_detection.evaluation import run_one as run_scenario
from shared.benchmark import RULES, Suite, db_ready, main


def setup(suite: Suite) -> None:
    if not db_ready():
        raise RuntimeError("the drift benchmark needs PostgreSQL: install the dev extras (pgserver) "
                           "or set TEST_DATABASE_URL")
    from agents.drift_detection.service import DriftService

    svc = DriftService(get_settings(), use_llm=False)
    if svc.store.get_model("v1") is None:  # the benchmark database starts empty: register v1
        svc.initialise()


def run_one(case: dict, model: str) -> dict:
    start = time.perf_counter()
    s = run_scenario(get_settings(), case["scenario"], case["seed"], case["batches"], use_llm=model != RULES)
    return {
        "seconds": round(time.perf_counter() - start, 2),
        "degraded": model != RULES and s["investigator"] == "rules",
        "diagnosis_correct": s["diagnosis_correct"],
        "action_ok": s["action_ok"],
        "detected": s["detected"],
        "false_alarm_rate": s["false_alarms"] / s["clean_batches"] if s["clean_batches"] else None,
        "time_to_detect": s["time_to_detect"],
        "diagnosis": s["diagnosis"],
        "action": s["action"],
        "investigations": s["investigations"],
        "timeline": "".join(s["timeline"]),
    }


def suite() -> Suite:
    settings = get_settings()
    cfg = settings["evaluation"]
    cases = [{"id": f"{scenario}-seed{seed}", "scenario": scenario, "seed": seed, "batches": cfg["batches"]}
             for scenario in settings.scenarios for seed in range(1, cfg["seeds"] + 1)]
    return Suite(name="drift", title="Drift Detection", agent="drift_detection", cases=cases, run_one=run_one,
                 scores={"diagnosis_correct": "Diagnosis accuracy", "action_ok": "Action accuracy",
                         "detected": "Detection rate", "false_alarm_rate": "False alarm rate",
                         "time_to_detect": "Time to detect (batches)"},
                 primary="diagnosis_correct", out_dir=Path(__file__).parent / "eval" / "results" / "suite",
                 setup=setup, needs_db=True, model_variable="XAI_MODEL",
                 default_model=lambda: get_settings().llm.model,
                 meta={"batches per run": cfg["batches"], "drift starts at": settings["simulation"]["drift_start"],
                       "label delay": settings["simulation"]["label_delay"]},
                 note="Only diagnosis and action accuracy depend on the model; detection, false alarms and time "
                      "to detect come from the statistics. A run is degraded when its first investigation fell "
                      "back to rules.")


if __name__ == "__main__":
    main(default_suites=["drift"])
