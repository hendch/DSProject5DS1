"""Scenario benchmark: how well does the agent detect and diagnose each kind of drift?

Each (scenario, seed) pair runs in an isolated workspace, so the benchmark
never touches the demo database.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import statistics
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable

from drift_agent.config import Paths, Settings
from drift_agent.detection.schemas import SEVERITY_ORDER
from drift_agent.service import DriftService


def run_one(settings: Settings, scenario: str, seed: int, batches: int, use_llm: bool) -> dict:
    base = DriftService(settings, use_llm=False)
    v1 = base.store.get_model("v1")
    if v1 is None:
        raise RuntimeError("Run `drift-agent init` before evaluating.")
    workspace = Path(tempfile.mkdtemp(prefix=f"drift-eval-{scenario}-"))
    try:
        s = dataclasses.replace(settings, paths=Paths(settings.paths.root, workspace))
        s.paths.ensure()
        svc = DriftService(s, use_llm=use_llm)
        svc.store.add_model_version("v1", v1["path"], v1["reference_path"], None, v1["threshold"],
                                    v1["metrics"], v1["importance"], v1["notes"], make_current=True)
        svc.reset(scenario, seed=seed)
        onset = svc.simulator.drift_start
        rows = []
        for _ in range(batches):
            r = svc.step()
            inv = r["investigation"]
            rows.append({
                "batch": r["batch_id"], "severity": r["severity"],
                "drift_type": inv["drift_type"] if inv else None,
                "action": inv["recommended_action"] if inv else None,
                "source": inv["meta"]["source"] if inv else None,
                "challenger_gain": next((a["result"].get("auc_gain") for a in r["actions"]
                                         if a["action"] == "train_challenger" and a["result"]), None),
            })
        return summarise(scenario, settings.scenarios[scenario], seed, onset, rows)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def summarise(scenario: str, params: dict, seed: int, onset: int, rows: list[dict]) -> dict:
    """Score one run against the scenario's expected diagnosis and actions (config.yaml)."""
    alert = lambda r: SEVERITY_ORDER[r["severity"]] >= SEVERITY_ORDER["ALERT"]  # noqa: E731
    no_drift = params["expected_drift_type"] == "none"
    pre = rows if no_drift else [r for r in rows if r["batch"] < onset]
    post = [] if no_drift else [r for r in rows if r["batch"] >= onset]
    first_alert = next((r for r in post if alert(r)), None)
    first_diag = next((r for r in post if r["drift_type"]), None)
    return {
        "scenario": scenario, "seed": seed, "onset": onset, "batches": len(rows),
        "false_alarms": sum(alert(r) for r in pre), "clean_batches": len(pre),
        "detected": None if no_drift else first_alert is not None,
        "time_to_detect": (first_alert["batch"] - onset) if first_alert else None,
        "diagnosis": first_diag["drift_type"] if first_diag else None,
        "diagnosis_correct": (first_diag["drift_type"] == params["expected_drift_type"]) if first_diag
        else no_drift,
        "action": first_diag["action"] if first_diag else None,
        "action_ok": (first_diag["action"] in params["expected_actions"]) if first_diag else no_drift,
        "investigations": sum(1 for r in rows if r["drift_type"]),
        "challenger_gain": next((r["challenger_gain"] for r in rows if r["challenger_gain"] is not None), None),
        "investigator": next((r["source"] for r in rows if r["source"]), None),
        "timeline": [r["severity"][0] for r in rows],
    }


def aggregate(settings: Settings, results: list[dict]) -> list[dict]:
    table = []
    for scenario, params in settings.scenarios.items():
        rs = [r for r in results if r["scenario"] == scenario]
        if not rs:
            continue
        ttd = [r["time_to_detect"] for r in rs if r["time_to_detect"] is not None]
        gains = [r["challenger_gain"] for r in rs if r["challenger_gain"] is not None]
        fa = sum(r["false_alarms"] for r in rs)
        clean = sum(r["clean_batches"] for r in rs)
        table.append({
            "scenario": scenario, "runs": len(rs),
            "detection_rate": None if params["expected_drift_type"] == "none" else sum(bool(r["detected"]) for r in rs) / len(rs),
            "false_alarm_rate": fa / clean if clean else 0.0,
            "mean_time_to_detect": statistics.mean(ttd) if ttd else None,
            "diagnosis_accuracy": sum(r["diagnosis_correct"] for r in rs) / len(rs),
            "action_accuracy": sum(r["action_ok"] for r in rs) / len(rs),
            "mean_challenger_auc_gain": statistics.mean(gains) if gains else None,
        })
    return table


def to_markdown(table: list[dict], meta: dict) -> str:
    def fmt(v, pct=False):
        if v is None:
            return "-"
        return f"{v:.0%}" if pct else (f"{v:.2f}" if isinstance(v, float) else str(v))

    lines = [
        f"# Drift agent benchmark ({meta['date']})", "",
        f"Investigator: **{meta['investigator']}** · seeds: {meta['seeds']} · batches per run: {meta['batches']} "
        f"· drift starts at batch {meta['onset']} · label delay: {meta['label_delay']} batches", "",
        "| Scenario | Runs | Detection rate | False alarm rate | Time to detect (batches) | "
        "Diagnosis accuracy | Action accuracy | Challenger AUC gain |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in table:
        lines.append(f"| {r['scenario']} | {r['runs']} | {fmt(r['detection_rate'], True)} | "
                     f"{fmt(r['false_alarm_rate'], True)} | {fmt(r['mean_time_to_detect'])} | "
                     f"{fmt(r['diagnosis_accuracy'], True)} | {fmt(r['action_accuracy'], True)} | "
                     f"{fmt(r['mean_challenger_auc_gain'])} |")
    lines += ["", "False alarm rate counts ALERT/CRITICAL batches before drift starts (all batches for scenarios without drift)."]
    return "\n".join(lines) + "\n"


def run_benchmark(settings: Settings, seeds: list[int], batches: int, use_llm: bool,
                  scenarios: list[str] | None = None,
                  progress: Callable[[str], None] | None = None) -> dict:
    results = []
    for scenario in scenarios or list(settings.scenarios):
        for seed in seeds:
            if progress:
                progress(f"{scenario} · seed {seed}")
            results.append(run_one(settings, scenario, seed, batches, use_llm))
    table = aggregate(settings, results)
    meta = {"date": datetime.now().isoformat(timespec="seconds"), "seeds": seeds, "batches": batches,
            "investigator": f"Grok ({settings.llm.model})" if use_llm else "rule-based fallback",
            "onset": settings["simulation"]["drift_start"], "label_delay": settings["simulation"]["label_delay"]}
    out_dir = settings.paths.root / "eval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"benchmark-{stamp}.json").write_text(
        json.dumps({"meta": meta, "summary": table, "runs": results}, indent=2), encoding="utf-8")
    md = to_markdown(table, meta)
    (out_dir / f"benchmark-{stamp}.md").write_text(md, encoding="utf-8")
    (out_dir / "latest.md").write_text(md, encoding="utf-8")
    return {"meta": meta, "summary": table, "runs": results, "markdown": md,
            "path": str(out_dir / f"benchmark-{stamp}.md")}
