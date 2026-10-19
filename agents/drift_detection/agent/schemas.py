"""The structured incident report the investigator must return."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DriftType = Literal["covariate", "pipeline_bug", "concept", "prediction_only", "none"]
RecommendedAction = Literal["monitor", "investigate_upstream", "retrain_challenger", "rollback", "no_action"]


class Evidence(BaseModel):
    tool: str = Field(description="Name of the tool whose output supports this point")
    observation: str = Field(description="The specific numbers or facts taken from that tool output")


class RootCause(BaseModel):
    hypothesis: str
    confidence: Literal["low", "medium", "high"]
    evidence: list[Evidence] = Field(min_length=1)


class InvestigationReport(BaseModel):
    summary: str = Field(description="2-3 plain-language sentences for an engineer")
    drift_type: DriftType
    affected_features: list[str]
    root_causes: list[RootCause] = Field(min_length=1, description="Ranked, most likely first")
    impact: str = Field(description="What this means for the model's predictions")
    recommended_action: RecommendedAction
    action_rationale: str
    explanation: list[str] = Field(description="Step-by-step reasoning, one step per item")
