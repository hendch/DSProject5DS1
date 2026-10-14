"""Read-only investigation tools available to the agent.

None of these functions can change state: they only read the store, the
reference data and production batches. Actions are decided by the guardrail,
never by the agent.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Callable

import numpy as np
import pandas as pd

from drift_agent.detection import stats
from drift_agent.detection.schemas import DriftReport

if TYPE_CHECKING:
    from drift_agent.service import DriftService

def tool_specs(settings) -> list[dict]:
    """Tool definitions sent to the LLM. Defaults come from config.yaml (agent.tools)."""
    t = settings["agent"]["tools"]
    return [
        {
            "name": "get_drift_report",
            "description": "Summary of the current drift check: severity, reasons, drifted features with "
                           "their statistics, data quality issues, prediction drift and latest performance.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "compare_distributions",
            "description": "Compare one feature between the reference data and the current batch: "
                           "quantiles/means for numeric features, category shares for categorical ones.",
            "parameters": {
                "type": "object",
                "properties": {"feature": {"type": "string", "description": "Feature name"}},
                "required": ["feature"],
            },
        },
        {
            "name": "segment_breakdown",
            "description": "Check whether drift in `feature` is concentrated in some segments of another "
                           "categorical feature `by` (PSI of `feature` inside each segment).",
            "parameters": {
                "type": "object",
                "properties": {
                    "feature": {"type": "string"},
                    "by": {"type": "string", "description": "A categorical feature to split on"},
                },
                "required": ["feature", "by"],
            },
        },
        {
            "name": "feature_importance",
            "description": "How much the current model relies on each feature (permutation importance, sums to 1).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "drift_history",
            "description": "PSI and drift status of a feature over recent batches, with the onset batch and "
                           "whether the change was sudden or gradual.",
            "parameters": {
                "type": "object",
                "properties": {
                    "feature": {"type": "string"},
                    "n": {"type": "integer",
                          "description": f"Number of recent batches (default {t['history_batches']}, "
                                         f"max {t['max_history_batches']})"},
                },
                "required": ["feature"],
            },
        },
        {
            "name": "recent_pipeline_events",
            "description": "Deployments, jobs and other operational events logged around the recent batches.",
            "parameters": {
                "type": "object",
                "properties": {"window": {"type": "integer",
                                          "description": f"How many batches back (default {t['events_window']}, "
                                                         f"max {t['max_events_window']})"}},
                "required": [],
            },
        },
        {
            "name": "performance_history",
            "description": "ROC-AUC and F1 of the model on recent batches whose true labels have arrived, "
                           "compared with the model's baseline.",
            "parameters": {
                "type": "object",
                "properties": {"n": {"type": "integer",
                                     "description": f"Number of recent checks (default {t['performance_checks']})"}},
                "required": [],
            },
        },
        {
            "name": "model_info",
            "description": "Current model version, its baseline metrics, and previous versions available for rollback.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    ]


TOOL_NAMES = {"get_drift_report", "compare_distributions", "segment_breakdown", "feature_importance",
              "drift_history", "recent_pipeline_events", "performance_history", "model_info"}


def openai_tools(settings) -> list[dict]:
    return [{"type": "function", "function": spec} for spec in tool_specs(settings)]


def _r(x: Any, nd: int = 4) -> Any:
    if x is None:
        return None
    if isinstance(x, (float, np.floating)):
        return None if math.isnan(float(x)) else round(float(x), nd)
    if isinstance(x, np.integer):
        return int(x)
    return x


class ToolBox:
    def __init__(self, service: "DriftService", run_id: int, report: DriftReport):
        self.service = service
        self.store = service.store
        self.settings = service.settings
        self.run_id = run_id
        self.report = report
        self.calls: list[dict] = []
        self.cfg = service.settings["agent"]["tools"]

    # ---- dispatch -------------------------------------------------------
    def call(self, name: str, args: dict | None = None) -> dict:
        args = args or {}
        fn: Callable[..., dict] | None = getattr(self, f"tool_{name}", None) if name in TOOL_NAMES else None
        if fn is None:
            out = {"error": f"unknown tool '{name}'. Available: {sorted(TOOL_NAMES)}"}
        else:
            try:
                out = fn(**args)
            except TypeError as e:
                out = {"error": f"bad arguments for {name}: {e}"}
            except (KeyError, ValueError) as e:
                out = {"error": str(e)}
        self.calls.append({"tool": name, "args": args, "output": out})
        self.store.audit("agent", "tool_call", {"tool": name, "args": args, "output": out}, self.run_id)
        return out

    def called_tools(self) -> set[str]:
        return {c["tool"] for c in self.calls if "error" not in c["output"]}

    # ---- helpers ----------------------------------------------------------
    def _check_feature(self, feature: str) -> None:
        if feature not in self.settings.features:
            raise ValueError(f"unknown feature '{feature}'. Features: {self.settings.features}")

    def _frames(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        ref = self.service.detector.reference_for(self.report.model_version)
        cur = self.service.simulator.load_batch(self.report.batch_id)
        return ref, cur

    # ---- tools --------------------------------------------------------------
    def tool_get_drift_report(self) -> dict:
        r = self.report
        drifted = [{
            "feature": f.feature, "kind": f.kind, "status": f.status, "confirmed": f.confirmed,
            "psi": f.psi, "test": f.test, "p_value": _r(f.p_value, 6), "effect_size": f.effect,
            "importance": f.importance,
        } for f in r.features if f.status != "stable"]
        return {
            "batch_id": r.batch_id, "model_version": r.model_version, "severity": r.severity,
            "data_drift_score": r.data_drift_score, "reasons": r.reasons,
            "drifted_features": drifted,
            "stable_features": [f.feature for f in r.features if f.status == "stable"],
            "quality_issues": [q.model_dump() for q in r.quality],
            "prediction_drift": r.prediction.model_dump(),
            "performance": r.performance.model_dump() if r.performance else
            "no labelled batch available yet (labels arrive with a delay)",
        }

    def tool_compare_distributions(self, feature: str) -> dict:
        self._check_feature(feature)
        ref, cur = self._frames()
        out: dict[str, Any] = {"feature": feature,
                               "missing_rate": {"reference": _r(ref[feature].isna().mean()),
                                                "current": _r(cur[feature].isna().mean())}}
        if feature in self.settings.numeric:
            r = ref[feature].dropna().astype(float)
            c = pd.to_numeric(cur[feature], errors="coerce").dropna()
            qs = [0.05, 0.25, 0.5, 0.75, 0.95]
            out.update({
                "kind": "numeric",
                "reference": {"mean": _r(r.mean(), 2), "std": _r(r.std(), 2), "min": _r(r.min(), 2),
                              "max": _r(r.max(), 2), **{f"p{int(q*100)}": _r(r.quantile(q), 2) for q in qs}},
                "current": {"mean": _r(c.mean(), 2), "std": _r(c.std(), 2), "min": _r(c.min(), 2),
                            "max": _r(c.max(), 2), **{f"p{int(q*100)}": _r(c.quantile(q), 2) for q in qs}},
            })
            ratios = {}
            for q in (0.25, 0.5, 0.75):
                rv, cv = r.quantile(q), c.quantile(q)
                if abs(rv) > 1e-9:
                    ratios[f"p{int(q*100)}"] = _r(cv / rv, 3)
            out["current_to_reference_quantile_ratio"] = ratios
        else:
            cats, rc, cc = stats.category_proportions(ref[feature].dropna(), cur[feature].dropna())
            rs, cs = rc / max(rc.sum(), 1), cc / max(cc.sum(), 1)
            rows = sorted(zip(cats, rs, cs), key=lambda t: -abs(t[2] - t[1]))
            out.update({
                "kind": "categorical",
                "categories": [{"category": k, "reference_share": _r(a), "current_share": _r(b),
                                "change": _r(b - a)} for k, a, b in rows[:self.cfg["top_categories"]]],
                "new_categories": [k for k, a, b in rows if a == 0 and b > 0],
                "vanished_categories": [k for k, a, b in rows if a > 0 and b == 0],
            })
        return out

    def tool_segment_breakdown(self, feature: str, by: str) -> dict:
        self._check_feature(feature)
        self._check_feature(by)
        if by not in self.settings.categorical:
            raise ValueError(f"'by' must be categorical, one of {self.settings.categorical}")
        ref, cur = self._frames()
        segments = []
        for seg in ref[by].dropna().value_counts().index[:self.cfg["max_segments"]]:
            r = ref.loc[ref[by] == seg, feature]
            c = cur.loc[cur[by] == seg, feature]
            if len(c) < self.cfg["min_segment_rows"]:
                continue
            if feature in self.settings.numeric:
                psi = stats.numeric_psi(r.dropna().to_numpy(float),
                                        pd.to_numeric(c, errors="coerce").dropna().to_numpy(float))
            else:
                _, rc, cc = stats.category_proportions(r.dropna(), c.dropna())
                psi = stats.psi_from_proportions(rc / max(rc.sum(), 1), cc / max(cc.sum(), 1))
            segments.append({"segment": str(seg), "reference_share": _r(len(r) / len(ref)),
                             "current_share": _r(len(c) / len(cur)), "psi_within_segment": _r(psi),
                             "rows": int(len(c))})
        segments.sort(key=lambda s: -(s["psi_within_segment"] or 0))
        psis = [s["psi_within_segment"] for s in segments if s["psi_within_segment"] is not None]
        spread = (max(psis) - min(psis)) if psis else 0.0
        stable = self.settings["detection"]["psi"]["moderate"]
        return {"feature": feature, "by": by, "segments": segments,
                "concentrated": bool(psis and spread > self.cfg["concentrated_spread"] and min(psis) < stable)}

    def tool_feature_importance(self) -> dict:
        meta = self.store.get_model(self.report.model_version)
        ranked = sorted(meta["importance"].items(), key=lambda kv: -kv[1])
        return {"model_version": meta["version"],
                "importance": [{"feature": f, "importance": _r(v)} for f, v in ranked]}

    def tool_drift_history(self, feature: str, n: int | None = None) -> dict:
        self._check_feature(feature)
        n = max(2, min(int(n or self.cfg["history_batches"]), self.cfg["max_history_batches"]))
        hist = self.store.feature_history(feature, up_to_run=self.run_id, limit=n)
        series = [{"batch_id": h["batch_id"], "psi": _r(h["psi"]), "status": h["status"]} for h in hist]
        moderate = self.settings["detection"]["psi"]["moderate"]
        onset = next((s["batch_id"] for s in series if (s["psi"] or 0) >= moderate), None)
        pattern = "no drift in window"
        biggest_jump = 0.0
        if onset is not None:
            values = [s["psi"] or 0.0 for s in series]
            jumps = [b - a for a, b in zip(values, values[1:])]
            biggest_jump = max(jumps) if jumps else values[-1]
            final = values[-1] or 1e-9
            # Sudden: one step accounts for most of the current level.
            pattern = "sudden" if biggest_jump >= self.cfg["sudden_jump_share"] * final else "gradual"
        return {"feature": feature, "history": series, "onset_batch": onset,
                "largest_single_batch_increase": _r(biggest_jump), "pattern": pattern}

    def tool_recent_pipeline_events(self, window: int | None = None) -> dict:
        window = max(1, min(int(window or self.cfg["events_window"]), self.cfg["max_events_window"]))
        start = max(1, self.report.batch_id - window)
        events = self.store.events_between(start, self.report.batch_id)
        return {"from_batch": start, "to_batch": self.report.batch_id,
                "events": [{"batch_id": e["batch_id"], "source": e["source"], "message": e["message"]}
                           for e in events]}

    def tool_performance_history(self, n: int | None = None) -> dict:
        n = max(1, min(int(n or self.cfg["performance_checks"]), self.cfg["max_history_batches"]))
        runs = [self.store.get_run(r["id"]) for r in self.store.list_runs(limit=self.cfg["max_history_batches"] * 5)
                if r["id"] <= self.run_id]
        rows = []
        for run in runs:
            perf = run["report"].get("performance") if run else None
            if perf and perf.get("auc") is not None:
                rows.append({"batch_id": perf["batch_id"], "auc": perf["auc"], "f1": perf["f1"],
                             "baseline_auc": perf["baseline_auc"], "auc_drop": perf["auc_drop"],
                             "degraded": perf["degraded"], "model_version": run["model_version"]})
            if len(rows) >= n:
                break
        rows.reverse()
        return {"label_delay_batches": self.settings["simulation"]["label_delay"],
                "checks": rows or "no labelled batches yet"}

    def tool_model_info(self) -> dict:
        current = self.store.current_model()
        models = self.store.list_models()
        return {
            "current_version": current["version"] if current else None,
            "current_metrics": current["metrics"] if current else None,
            "decision_threshold": _r(current["threshold"]) if current else None,
            "versions": [{"version": m["version"], "parent": m["parent"], "created_at": m["created_at"],
                          "auc": _r(m["metrics"].get("auc")), "notes": m["notes"]} for m in models],
        }
