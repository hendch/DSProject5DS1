"""Drift detector: compares a production batch against the reference snapshot.

Produces a DriftReport with per-feature drift, data quality issues, prediction
drift, performance on the most recent labelled batch, and an overall severity.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import f1_score, roc_auc_score

from drift_agent.config import Settings
from drift_agent.data.dataset import read_frame
from drift_agent.data.simulator import LABEL_COL, PRED_COL, SCORE_COL, Simulator
from drift_agent.detection import stats
from drift_agent.detection.schemas import (
    DriftReport, FeatureDrift, PerformanceCheck, PredictionDrift, QualityIssue, Severity,
)
from drift_agent.model.registry import Registry
from drift_agent.store.db import Store


class Detector:
    def __init__(self, settings: Settings, store: Store, registry: Registry, simulator: Simulator):
        self.settings = settings
        self.store = store
        self.registry = registry
        self.simulator = simulator
        self.cfg = settings["detection"]
        self._references: dict[str, pd.DataFrame] = {}
        self._ref_scores: dict[str, np.ndarray] = {}

    def clear_cache(self) -> None:
        self._references.clear()
        self._ref_scores.clear()

    def reference_for(self, version: str) -> pd.DataFrame:
        """The 'normal' data a model version is compared against."""
        if version not in self._references:
            meta = self.store.get_model(version)
            if meta is None:
                raise KeyError(f"unknown model version {version}")
            self._references[version] = read_frame(self.settings, meta["reference_path"])
        return self._references[version]

    def reference_scores(self, version: str) -> np.ndarray:
        if version not in self._ref_scores:
            model = self.registry.load(version)
            ref = self.reference_for(version)
            self._ref_scores[version] = model.predict_proba(ref[self.settings.features])[:, 1]
        return self._ref_scores[version]

    # ---- public -------------------------------------------------------------
    def check(self, batch_id: int, previous_features: dict[str, dict] | None = None,
              previous_perf_degraded: bool = False) -> DriftReport:
        batch_meta = self.store.get_batch(batch_id)
        if batch_meta is None:
            raise KeyError(f"batch {batch_id} not found")
        version = batch_meta["model_version"]
        model_meta = self.store.get_model(version)
        cur = self.simulator.load_batch(batch_id)

        ref = self.reference_for(version)
        quality = self._quality(ref, cur)
        features = self._features(ref, cur, model_meta["importance"], previous_features or {})
        prediction = self._prediction(cur, version, model_meta["threshold"])
        performance = self._performance(batch_id, version)

        score = float(sum(f.weighted for f in features if f.confirmed))
        severity, reasons = self._severity(score, features, quality, prediction, performance,
                                           previous_perf_degraded)
        return DriftReport(
            batch_id=batch_id, model_version=version, severity=severity,
            data_drift_score=round(score, 4), reasons=reasons, features=features,
            quality=quality, prediction=prediction, performance=performance,
        )

    # ---- data quality -------------------------------------------------------
    def _quality(self, ref: pd.DataFrame, cur: pd.DataFrame) -> list[QualityIssue]:
        q = self.cfg["quality"]
        issues: list[QualityIssue] = []
        expected = set(self.settings.features)
        present = set(c for c in cur.columns if not c.startswith("__"))
        for col in sorted(expected - present):
            issues.append(QualityIssue(feature=col, check="missing_column", detail=f"column '{col}' is missing"))
        for col in sorted(present - expected):
            issues.append(QualityIssue(feature=col, check="unexpected_column",
                                       detail=f"unexpected column '{col}'"))
        for col in self.settings.features:
            if col not in cur:
                continue
            ref_missing = float(ref[col].isna().mean())
            cur_missing = float(cur[col].isna().mean())
            if cur_missing - ref_missing > q["missing_rate_increase"]:
                issues.append(QualityIssue(
                    feature=col, check="missing_rate", reference_value=ref_missing, current_value=cur_missing,
                    detail=f"missing values in '{col}' rose from {ref_missing:.1%} to {cur_missing:.1%}"))
        for col in self.settings.categorical:
            if col not in cur:
                continue
            known = set(ref[col].dropna().unique())
            values = cur[col].dropna()
            unseen = values[~values.isin(known)]
            rate = len(unseen) / max(len(cur), 1)
            if rate > q["new_category_rate"]:
                cats = ", ".join(sorted(map(str, unseen.unique()))[:5])
                issues.append(QualityIssue(
                    feature=col, check="new_category", reference_value=0.0, current_value=rate,
                    detail=f"{rate:.1%} of rows have '{col}' values never seen in training: {cats}"))
        for col in self.settings.numeric:
            if col not in cur:
                continue
            values = pd.to_numeric(cur[col], errors="coerce")
            if values.isna().sum() > cur[col].isna().sum():
                issues.append(QualityIssue(feature=col, check="type_mismatch",
                                           detail=f"'{col}' contains non-numeric values"))
            lo, hi = float(ref[col].min()), float(ref[col].max())
            rate = float(((values < lo) | (values > hi)).mean())
            if rate > q["out_of_range_rate"]:
                issues.append(QualityIssue(
                    feature=col, check="out_of_range", reference_value=0.0, current_value=rate,
                    detail=f"{rate:.1%} of '{col}' values fall outside the reference range [{lo:g}, {hi:g}]"))
        return issues

    # ---- per-feature drift ----------------------------------------------------
    def _features(self, ref: pd.DataFrame, cur: pd.DataFrame, importance: dict,
                  previous: dict[str, dict]) -> list[FeatureDrift]:
        c_num, c_cat, c_psi = self.cfg["numeric"], self.cfg["categorical"], self.cfg["psi"]
        scoring = self.cfg["scoring"]
        share = scoring["importance_share"]
        results = []
        for col in self.settings.features:
            if col not in cur:
                continue
            if col in self.settings.numeric:
                r = stats.numeric_test(ref[col].to_numpy(dtype=float),
                                       pd.to_numeric(cur[col], errors="coerce").to_numpy(dtype=float),
                                       c_num["psi_bins"])
                significant_test = r.p_value < c_num["ks_pvalue"] and r.effect >= c_num["ks_d"]
                kind, test = "numeric", "ks"
            else:
                r = stats.categorical_test(ref[col], cur[col])
                significant_test = r.p_value < c_cat["chi2_pvalue"] and r.effect >= c_cat["js_distance"]
                kind, test = "categorical", "chi2"

            if r.psi >= c_psi["significant"] and (significant_test or r.psi >= c_psi["significant_alone"]):
                status = "significant"
            elif significant_test or r.psi >= c_psi["moderate"]:
                status = "moderate"
            else:
                status = "stable"
            prev_status = previous.get(col, {}).get("status", "stable")
            confirmed = status == "significant" or (status == "moderate" and prev_status != "stable")
            drift_score = min(1.0, r.psi / scoring["psi_for_full_score"])
            imp = float(importance.get(col, 0.0))
            # Every feature keeps a small floor weight so drift in a rarely used
            # feature is still visible, but important features dominate.
            weight = share * imp + (1 - share) / len(self.settings.features)
            results.append(FeatureDrift(
                feature=col, kind=kind, test=test,
                statistic=_clean(r.statistic), p_value=_clean(r.p_value),
                effect=round(r.effect, 4), psi=round(r.psi, 4), status=status, confirmed=confirmed,
                drift_score=round(drift_score, 4), importance=round(imp, 4),
                weighted=round(drift_score * weight, 4),
            ))
        results.sort(key=lambda f: f.weighted, reverse=True)
        return results

    # ---- prediction drift -------------------------------------------------
    def _prediction(self, cur: pd.DataFrame, version: str, threshold: float) -> PredictionDrift:
        ref_scores = self.reference_scores(version)
        cur_scores = cur[SCORE_COL].to_numpy(dtype=float)
        psi = stats.numeric_psi(ref_scores, cur_scores, self.cfg["numeric"]["psi_bins"])
        ref_rate = float((ref_scores >= threshold).mean())
        cur_rate = float(cur[PRED_COL].mean())
        p = self.cfg["prediction"]
        drifted = psi >= p["psi"] or abs(cur_rate - ref_rate) >= p["positive_rate_change"]
        return PredictionDrift(score_psi=round(psi, 4), reference_positive_rate=round(ref_rate, 4),
                               current_positive_rate=round(cur_rate, 4), drifted=drifted)

    # ---- performance (needs labels) -------------------------------------------
    def _performance(self, batch_id: int, version: str) -> PerformanceCheck | None:
        """ROC-AUC on the newest labelled batches, pooled to reduce noise.

        Only batches scored by `version` (the model serving this batch) count:
        right after a promotion, the old model's labels say nothing about the new one.
        """
        cfg = self.cfg["performance"]
        perf_batch = batch_id - self.settings["simulation"]["label_delay"]
        meta = self.store.get_batch(perf_batch) if perf_batch >= 1 else None
        if meta is None or not meta["labels_available"]:
            return None
        if meta["model_version"] != version:
            current = self.store.get_model(version)["metrics"]
            return PerformanceCheck(batch_id=perf_batch, auc=None, f1=None, baseline_auc=current["auc"],
                                    baseline_f1=current["f1"], auc_drop=None, n=0, positives=0, degraded=False,
                                    skipped_reason=f"waiting for labels on batches scored by {version}")
        window = []
        for b in range(perf_batch, max(0, perf_batch - cfg["window_batches"]), -1):
            m = self.store.get_batch(b)
            if m and m["labels_available"] and m["model_version"] == meta["model_version"]:
                window.append(b)
        df = pd.concat([self.simulator.load_batch(b, with_labels=True) for b in window], ignore_index=True)
        model_meta = self.store.get_model(meta["model_version"])
        base_auc, base_f1 = model_meta["metrics"]["auc"], model_meta["metrics"]["f1"]
        y = df[LABEL_COL].to_numpy(dtype=int)
        positives = int(y.sum())
        if positives < cfg["min_positives"] or positives == len(y):
            return PerformanceCheck(batch_id=perf_batch, batches=sorted(window), auc=None, f1=None, baseline_auc=base_auc,
                                    baseline_f1=base_f1, auc_drop=None, n=len(y), positives=positives,
                                    degraded=False, skipped_reason="not enough positive labels")
        auc = float(roc_auc_score(y, df[SCORE_COL]))
        f1 = float(f1_score(y, df[PRED_COL], zero_division=0))
        drop = base_auc - auc
        return PerformanceCheck(batch_id=perf_batch, batches=sorted(window), auc=round(auc, 4),
                                f1=round(f1, 4), baseline_auc=round(base_auc, 4), baseline_f1=round(base_f1, 4),
                                auc_drop=round(drop, 4), n=len(y), positives=positives,
                                degraded=drop >= cfg["auc_drop"])

    # ---- severity -----------------------------------------------------------
    def _severity(self, score: float, features: list[FeatureDrift], quality: list[QualityIssue],
                  prediction: PredictionDrift, performance: PerformanceCheck | None,
                  previous_perf_degraded: bool) -> tuple[Severity, list[str]]:
        sev = self.settings["severity"]
        tol = self.cfg["performance"]["auc_drop"]
        reasons: list[str] = []
        level = 0

        def raise_to(new: int, reason: str) -> None:
            nonlocal level
            level = max(level, new)
            reasons.append(reason)

        if quality:
            raise_to(3, f"{len(quality)} data quality issue(s): " + "; ".join(q.detail for q in quality[:3]))
        if performance and performance.degraded:
            drop = performance.auc_drop or 0.0
            span = "-".join(str(b) for b in (performance.batches[0], performance.batches[-1]))                 if len(performance.batches) > 1 else str(performance.batch_id)
            msg = (f"ROC-AUC on labelled batches {span} is {performance.auc:.3f} vs baseline "
                   f"{performance.baseline_auc:.3f} (drop {drop:.3f})")
            if drop >= self.cfg["performance"]["immediate_multiplier"] * tol or previous_perf_degraded:
                raise_to(3, msg + ", confirmed")
            else:
                raise_to(1, msg + ", waiting for confirmation")
        if score >= sev["alert"]:
            raise_to(2, f"importance-weighted data drift score {score:.3f} >= alert threshold {sev['alert']}")
        elif score >= sev["warning"]:
            raise_to(1, f"importance-weighted data drift score {score:.3f} >= warning threshold {sev['warning']}")
        unconfirmed = [f.feature for f in features if f.status != "stable" and not f.confirmed]
        if unconfirmed:
            raise_to(1, "unconfirmed moderate drift in: " + ", ".join(unconfirmed))
        if prediction.drifted:
            if score >= sev["warning"]:
                raise_to(2, f"prediction drift (score PSI {prediction.score_psi:.3f}) alongside data drift")
            else:
                raise_to(1, f"prediction drift (score PSI {prediction.score_psi:.3f}, predicted yes-rate "
                            f"{prediction.reference_positive_rate:.1%} -> {prediction.current_positive_rate:.1%})")
        levels: list[Severity] = ["OK", "WARNING", "ALERT", "CRITICAL"]
        return levels[level], reasons or ["no meaningful drift"]


def _clean(x: float) -> float | None:
    return None if x is None or np.isnan(x) else round(float(x), 6)
