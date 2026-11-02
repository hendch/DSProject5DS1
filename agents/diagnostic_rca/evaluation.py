"""Case benchmark: how often does the RCA agent name the right root cause?

Every case in eval/cases.json is a failed build log with the expected cause. The knowledge
base is loaded into the runner's throwaway PostgreSQL, so the benchmark never touches the
demo database. "rules" is the no-LLM fallback, as a baseline.

    python -m agents.diagnostic_rca.evaluation --model rules --model meta-llama/Llama-3.1-8B-Instruct

Runner, options and LangSmith experiments: shared/benchmark.py.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from agents.diagnostic_rca import agent, retrieval
from shared import knowledge
from shared.benchmark import RULES, Suite, db_ready, main, matches
from shared.llm import LLMUnavailable

BASE = Path(__file__).parent / "eval"
COLLECTION = "rca_eval"


def setup_kb(suite: Suite, kb: list[dict]) -> None:
    """Load the benchmark docs into the benchmark database; without one, run without retrieval."""
    suite.meta.update({"retrieval": "off (no Postgres)", "embedding": knowledge.EMBEDDING_MODEL})
    if not db_ready():
        retrieval.COLLECTION = f"{COLLECTION}_missing"  # never search the demo knowledge base
        return
    knowledge.add_documents([{"id": d["id"], "source": d["id"], "content": d["content"]} for d in kb],
                            collection=COLLECTION)
    retrieval.COLLECTION = COLLECTION
    suite.meta["retrieval"] = "on"


def run_one(case: dict, model: str) -> dict:
    error_context = agent.extract_error_context(agent.clean_log(case["log"]))
    docs = retrieval.retrieve(agent.build_queries(error_context))
    start = time.perf_counter()
    if model == RULES:
        result, source = agent.rank_by_rules(error_context, docs), "rules"
    else:
        try:
            result, source = agent.rank_causes(error_context, docs), "llm"
        except LLMUnavailable as e:
            print(f"[warn] {case['id']}: {e}")
            result, source = agent.rank_by_rules(error_context, docs), "rules"
    causes = result["ranked_causes"]
    hit = lambda c: matches(c["cause"], case["expect"])
    return {
        "seconds": round(time.perf_counter() - start, 2),
        "degraded": model != RULES and source == "rules",
        "top1": hit(causes[0]),
        "top3": any(hit(c) for c in causes),
        "source_ok": causes[0]["source"] == (case["source"] or "none"),
        "retrieved_expected": case["source"] is None or any(d["source"] == case["source"] for d in docs),
        "confidence": causes[0]["confidence"],
        "top_cause": causes[0]["cause"],
        "summary": result["failure_summary"],
    }


def suite() -> Suite:
    data = json.loads((BASE / "cases.json").read_text(encoding="utf-8"))
    return Suite(name="rca", title="RCA agent", agent="diagnostic_rca", cases=data["cases"], run_one=run_one,
              scores={"top1": "Top-1 accuracy", "top3": "Top-3 accuracy", "source_ok": "Citation accuracy",
                      "retrieved_expected": "Retrieval hit", "confidence": "Mean confidence"},
              primary="top1", out_dir=BASE / "results", setup=lambda s: setup_kb(s, data["kb"]), needs_db=True,
              note="Top-1: the first cause matches the expected one. Citation: it cites the expected "
                   "knowledge-base doc (or 'none' when no doc fits). Retrieval hit: the expected doc was "
                   "retrieved at all (independent of the model).")


if __name__ == "__main__":
    main(default_suites=["rca"])
