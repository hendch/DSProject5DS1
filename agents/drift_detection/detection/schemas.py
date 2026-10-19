"""Typed results produced by the detector."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["OK", "WARNING", "ALERT", "CRITICAL"]
SEVERITY_ORDER = {"OK": 0, "WARNING": 1, "ALERT": 2, "CRITICAL": 3}
FeatureStatus = Literal["stable", "moderate", "significant"]


class FeatureDrift(BaseModel):
    feature: str
    kind: Literal["numeric", "categorical"]
    test: Literal["ks", "chi2"]
    statistic: float | None
    p_value: float | None
    effect: float = Field(description="KS distance D (numeric) or Jensen-Shannon distance (categorical)")
    psi: float
    status: FeatureStatus
    confirmed: bool = Field(description="Counts towards severity (significant, or moderate twice in a row)")
    drift_score: float = Field(description="min(1, PSI / detection.scoring.psi_for_full_score)")
    importance: float
    weighted: float


class QualityIssue(BaseModel):
    feature: str
    check: Literal["missing_column", "unexpected_column", "type_mismatch", "missing_rate",
                   "new_category", "out_of_range"]
    reference_value: float | None = None
    current_value: float | None = None
    detail: str


class PredictionDrift(BaseModel):
    score_psi: float
    reference_positive_rate: float
    current_positive_rate: float
    drifted: bool


class PerformanceCheck(BaseModel):
    batch_id: int = Field(description="Newest labelled batch in the window")
    batches: list[int] = Field(default_factory=list, description="Labelled batches pooled for this check")
    auc: float | None
    f1: float | None
    baseline_auc: float
    baseline_f1: float
    auc_drop: float | None
    n: int
    positives: int
    degraded: bool
    skipped_reason: str | None = None


class DriftReport(BaseModel):
    batch_id: int
    model_version: str
    severity: Severity
    data_drift_score: float
    reasons: list[str]
    features: list[FeatureDrift]
    quality: list[QualityIssue]
    prediction: PredictionDrift
    performance: PerformanceCheck | None

    def drifted_features(self) -> list[FeatureDrift]:
        return [f for f in self.features if f.status != "stable"]
