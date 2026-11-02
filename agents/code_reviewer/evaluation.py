"""Case benchmark: does the Code Reviewer find the planted issues without inventing others?

Every case in eval/cases.json is a pull request seeded on demo/base, reviewed end to end
exactly as the orchestrator runs it (throwaway git repo, ruff + pytest baseline, the agent
as a subprocess with CODE_REVIEWER_LLM_*). There is no rule-based baseline: without a
model the reviewer reports nothing.

    python -m agents.code_reviewer.evaluation --model grok-4.20-0309-non-reasoning

Runner, options and LangSmith experiments: shared/benchmark.py.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from pathlib import Path

from agents.orchestrator.adapters import ROOT, _demo_module, _run_reviewer
from shared.benchmark import Suite, main

BASE = Path(__file__).parent / "eval"


def run_one(case: dict, model: str) -> dict:
    demo = _demo_module()
    workdir = Path(tempfile.mkdtemp(prefix=f"cr-eval-{case['id']}-"))
    try:
        # make_repo overlays demo/scenarios/<name>; an absolute path overrides that folder
        repo, base, head = demo.make_repo(workdir, str(ROOT / case["scenario"]))
        reports = workdir / "reports"
        demo.baseline(repo, reports)
        facts = {"repo": str(repo), "base": base, "head": head, "lint": str(reports / "lint.json"),
                 "junit": str(reports / "junit.xml")}
        start = time.perf_counter()
        report = _run_reviewer(facts, workdir / "review", None, None)
        seconds = time.perf_counter() - start
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    findings = report.get("findings", [])
    categories = {f.get("category") for f in findings}
    requested = [m.group(1) for e in report.get("tool_log", []) if e.get("tool") == "request_safe_fix"
                 for m in [re.search(r'"rule_code"\s*:\s*"(\w+)"', e.get("args", ""))] if m]  # args are truncated
    found = [any(c in categories for c in group) for group in case["expect"]]
    bad_fix = any(code in case["forbid_fixes"] for code in requested)
    false_alarm = any(f.get("severity") in ("high", "medium") for f in findings) if case["clean"] else None
    return {
        "seconds": round(seconds, 2),
        "degraded": report.get("status") == "review_failed",
        "passed": all(found) and not bad_fix and not false_alarm,
        "recall": sum(found) / len(found) if found else None,
        "bad_fix": bad_fix if case["forbid_fixes"] else None,
        "false_alarm": false_alarm,
        "findings": len(findings),
        "status": report.get("status"),
        "categories": sorted(c for c in categories if c),
        "fix_requests": requested,
    }


def suite() -> Suite:
    data = json.loads((BASE / "cases.json").read_text(encoding="utf-8"))
    return Suite(name="code_review", title="Code Reviewer", agent="code_reviewer", cases=data["cases"],
                 run_one=run_one, has_baseline=False,
                 scores={"passed": "Passed", "recall": "Issues found (recall)", "bad_fix": "Harmful fix requested",
                         "false_alarm": "False alarm on a clean PR", "findings": "Mean findings"},
                 primary="passed", out_dir=BASE / "results",
                 note="Passed: every planted issue found, no forbidden fix requested and, on the clean PR, no "
                      "medium or high finding. Harmful fix: a lint fix that breaks the code (side_effect_pr's "
                      "F401 removes an import used for its side effect).")


if __name__ == "__main__":
    main(default_suites=["code_review"])
