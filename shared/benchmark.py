"""Benchmarks for the LLM agents: run fixed cases against one or more models and score them.

Each agent defines a Suite (its cases, how to run one, what to score) in its own
evaluation.py. This module runs suites, alone or all at once, writes a Markdown table per
suite, and with --langsmith also records each model as a LangSmith experiment.

    python -m shared.benchmark                                  # every suite, the models in .env
    python -m shared.benchmark --suite rca --suite triage       # some suites
    python -m shared.benchmark --model rules --model rca=deepseek-ai/DeepSeek-R1-Distill-Qwen-7B
    python -m shared.benchmark --repeats 3 --langsmith

A model is picked through <AGENT>_LLM_MODEL, so every model must be served by that agent's
endpoint (<AGENT>_LLM_BASE_URL in .env). "rules" is the suite's no-LLM baseline, when it has one.
Suites run in parallel threads; each one only changes its own agent's variable.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import statistics
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from shared.llm import for_agent, reset_cooldowns

load_dotenv()
RULES = "rules"
SUITES = {
    "rca": "agents.diagnostic_rca.evaluation",
    "triage": "agents.orchestrator.evaluation",
    "guardrail": "agents.guardrail.evaluation",
    "code_review": "agents.code_reviewer.evaluation",
}


@dataclass
class Suite:
    name: str                               # short id, used in file and experiment names
    title: str
    agent: str                              # picks <AGENT>_LLM_MODEL
    cases: list[dict]                       # each has an "id"
    run_one: Callable[[dict, str], dict]    # (case, model) -> row with "degraded", "seconds" and the scores
    scores: dict[str, str]                  # row key -> column label; 0/1 or numbers, averaged (None = n/a)
    primary: str                            # the score shown per case
    out_dir: Path
    has_baseline: bool = True               # whether RULES means anything for this agent
    setup: Callable[[Suite], Callable[[], None] | None] | None = None  # returns a cleanup
    meta: dict = field(default_factory=dict)
    note: str = ""


def load_suite(name: str) -> Suite:
    return importlib.import_module(SUITES[name]).suite()


def matches(text: str, groups: list[list[str]]) -> bool:
    """True when every group has at least one of its words in the text (case-insensitive)."""
    return all(any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text, re.IGNORECASE) for w in group) for group in groups)


def aggregate(suite: Suite, results: list[dict], models: list[str]) -> list[dict]:
    table = []
    for model in models:
        rs = [r for r in results if r["model"] == model]
        if not rs:
            continue
        row = {"model": model, "runs": len(rs)}
        for key in suite.scores:
            values = [r[key] for r in rs if r.get(key) is not None]
            row[key] = statistics.mean(values) if values else None
        ok = [r["seconds"] for r in rs if not r["degraded"]]
        row["degraded_rate"] = sum(r["degraded"] for r in rs) / len(rs)
        row["mean_seconds"] = statistics.mean(ok) if ok else None
        table.append(row)
    return table


def _fmt(v, pct: bool) -> str:
    if v is None:
        return "-"
    return f"{v:.0%}" if pct else (f"{v:.2f}" if isinstance(v, float) else str(v))


def _is_rate(results: list[dict], key: str) -> bool:
    return all(r.get(key) is None or isinstance(r[key], bool) for r in results)


def to_markdown(suite: Suite, table: list[dict], results: list[dict], meta: dict) -> str:
    rate = {k: _is_rate(results, k) for k in suite.scores}
    shown = " · ".join(f"{k}: {v}" for k, v in meta.items() if k not in ("date", "models"))
    lines = [f"# {suite.title} benchmark ({meta['date']})", "", shown, "",
             "| Model | Runs | " + " | ".join(suite.scores.values()) + " | Degraded | Mean seconds |",
             "|---|---|" + "---|" * len(suite.scores) + "---|---|"]
    for r in table:
        cells = [_fmt(r[k], rate[k]) for k in suite.scores]
        lines.append(f"| {r['model']} | {r['runs']} | " + " | ".join(cells)
                     + f" | {_fmt(r['degraded_rate'], True)} | {_fmt(r['mean_seconds'], False)} |")
    models = [r["model"] for r in table]
    lines += ["", f"## Per case ({suite.scores[suite.primary].lower()})", "",
              "| Case | " + " | ".join(models) + " |", "|---|" + "---|" * len(models)]
    for case in dict.fromkeys(r["case"] for r in results):
        cells = []
        for m in models:
            values = [r[suite.primary] for r in results if r["case"] == case and r["model"] == m
                      and r.get(suite.primary) is not None]
            cells.append(_fmt(statistics.mean(values), rate[suite.primary]) if values else "-")
        lines.append(f"| {case} | " + " | ".join(cells) + " |")
    lines += ["", suite.note, ("Degraded: the model failed and the fallback answered instead; those runs still "
                               "count, as they would in production.")]
    return "\n".join(lines) + "\n"


def langsmith_dataset(client, suite: Suite) -> str:
    """Upload the cases once per version (the name carries a content hash)."""
    digest = hashlib.sha256(json.dumps(suite.cases, sort_keys=True).encode()).hexdigest()[:8]
    name = f"{suite.name}-benchmark-{digest}"
    if not client.has_dataset(dataset_name=name):
        client.create_dataset(name, description=f"{suite.title} benchmark cases")
        client.create_examples(dataset_name=name, examples=[{"inputs": c} for c in suite.cases])
    return name


def run_langsmith(suite: Suite, model: str, repeats: int, meta: dict,
                  progress: Callable[[str], None] | None) -> list[dict]:
    """One LangSmith experiment per model, scored by the suite; returns the same rows as a local run."""
    from langsmith import Client, evaluate

    client = Client()
    by_id = {c["id"]: c for c in suite.cases}
    results = []

    def target(inputs: dict) -> dict:
        if progress:
            progress(f"{model} · {inputs['id']} (LangSmith)")
        row = run_one(suite, by_id[inputs["id"]], model)
        results.append(row)
        return row

    def scores(outputs: dict) -> dict:
        keys = [*suite.scores, "degraded", "seconds"]
        return {"results": [{"key": k, "score": float(outputs[k])} for k in keys if outputs.get(k) is not None]}

    evaluate(target, data=langsmith_dataset(client, suite), evaluators=[scores],
             experiment_prefix=f"{suite.name}-{model.split('/')[-1]}", num_repetitions=repeats,
             max_concurrency=0,  # sequential: the model is picked through an environment variable
             metadata={**meta, "model": model}, client=client)
    return results


def run_one(suite: Suite, case: dict, model: str) -> dict:
    return {"case": case["id"], "model": model, **suite.run_one(case, model)}


def run_suite(suite: Suite, models: list[str] | None = None, repeats: int = 1, case_ids: list[str] | None = None,
              progress: Callable[[str], None] | None = None, use_langsmith: bool = False) -> dict:
    models = [m for m in (models or [for_agent(suite.agent).profile.model]) if m != RULES or suite.has_baseline]
    suite.cases = [c for c in suite.cases if not case_ids or c["id"] in case_ids]
    variable = f"{suite.agent.upper()}_LLM_MODEL"
    previous = os.environ.get(variable)
    cleanup = suite.setup(suite) if suite.setup else None
    meta = {"date": datetime.now().isoformat(timespec="seconds"), "cases": len(suite.cases),
            "runs per case": repeats, "models": models, **suite.meta}
    results = []
    try:
        for model in models:
            if model != RULES:
                os.environ[variable] = model
            reset_cooldowns()  # one model's failure must not skip the next
            if use_langsmith:
                results += run_langsmith(suite, model, repeats, meta, progress)
                continue
            for case in suite.cases:
                for i in range(repeats):
                    if progress:
                        progress(f"{model} · {case['id']} · run {i + 1}/{repeats}")
                    results.append(run_one(suite, case, model))
    finally:
        if previous is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = previous
        if cleanup:
            cleanup()
    table = aggregate(suite, results, models)
    suite.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (suite.out_dir / f"benchmark-{stamp}.json").write_text(
        json.dumps({"meta": meta, "summary": table, "runs": results}, indent=2), encoding="utf-8")
    md = to_markdown(suite, table, results, meta)
    (suite.out_dir / f"benchmark-{stamp}.md").write_text(md, encoding="utf-8")
    (suite.out_dir / "latest.md").write_text(md, encoding="utf-8")
    return {"meta": meta, "summary": table, "runs": results, "markdown": md,
            "path": str(suite.out_dir / f"benchmark-{stamp}.md")}


def models_for(name: str, specs: list[str] | None) -> list[str] | None:
    """--model X applies to every suite, --model rca=X only to that one."""
    picked = []
    for spec in specs or []:
        prefix, sep, rest = spec.partition("=")
        if sep and prefix in SUITES:
            if prefix == name:
                picked.append(rest)
        else:
            picked.append(spec)
    return picked or None


def main(argv: list[str] | None = None, default_suites: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Benchmark the LLM agents on fixed cases.")
    parser.add_argument("--suite", action="append", choices=list(SUITES), help="Default: every suite")
    parser.add_argument("--model", action="append", dest="models",
                        help=f"Model to test (repeatable); 'suite=model' for one suite only; '{RULES}' = no LLM. "
                             "Default: each agent's model in .env")
    parser.add_argument("--repeats", type=int, default=1, help="Runs per case (models vary even at temperature 0)")
    parser.add_argument("--case", action="append", dest="cases", help="Limit to some case ids")
    parser.add_argument("--langsmith", action="store_true",
                        help="Also run each model as a LangSmith experiment (needs LANGSMITH_API_KEY)")
    parser.add_argument("--sequential", action="store_true", help="Run the suites one after another")
    args = parser.parse_args(argv)
    if args.langsmith and not os.getenv("LANGSMITH_API_KEY"):
        parser.error("--langsmith needs LANGSMITH_API_KEY in .env")
    names = args.suite or default_suites or list(SUITES)

    def run(name: str) -> dict:
        return run_suite(load_suite(name), models_for(name, args.models), args.repeats, args.cases,
                         progress=lambda m: print(f"[{name}] {m}", flush=True), use_langsmith=args.langsmith)

    with ThreadPoolExecutor(max_workers=1 if args.sequential else len(names)) as pool:
        outputs = dict(zip(names, pool.map(run, names)))
    for out in outputs.values():
        print("\n" + out["markdown"])
        print(f"Saved to {out['path']}")


if __name__ == "__main__":
    main()
