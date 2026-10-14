"""Application service: wires store, registry, simulator, detector, agent and guardrail.

Every entry point (CLI, API, scheduler, evaluation) goes through this class so
they all behave identically.
"""

from __future__ import annotations

import threading
from pathlib import Path

from drift_agent.config import Settings, get_settings
from drift_agent.data import dataset
from drift_agent.data.simulator import Simulator
from drift_agent.detection.detector import Detector
from drift_agent.detection.schemas import SEVERITY_ORDER, DriftReport
from drift_agent.model.registry import Registry, train
from drift_agent.store.db import Store


class DriftService:
    def __init__(self, settings: Settings | None = None, db_path: Path | None = None,
                 use_llm: bool | None = None):
        self.settings = settings or get_settings()
        self.store = Store(db_path or self.settings.paths.db)
        self.registry = Registry(self.settings, self.store)
        self.simulator = Simulator(self.settings, self.store, self.registry)
        self.detector = Detector(self.settings, self.store, self.registry, self.simulator)
        # None = automatic: use Grok whenever a key is configured (checked on every run).
        self._use_llm_override = use_llm
        self._lock = threading.Lock()
        # Imported here to avoid import cycles (they depend on this module's collaborators).
        from drift_agent.agent.investigator import Investigator
        from drift_agent.guardrail.policy import Guardrail

        self.guardrail = Guardrail(self)
        self.investigator = Investigator(self)

    @property
    def use_llm(self) -> bool:
        if self._use_llm_override is not None:
            return self._use_llm_override
        return self.settings.llm.enabled

    @use_llm.setter
    def use_llm(self, value: bool | None) -> None:
        self._use_llm_override = value

    # ---- setup ----------------------------------------------------------------
    def initialise(self, force_download: bool = False) -> dict:
        """Download data, build splits, train and register the baseline model v1."""
        dataset.download(self.settings, force=force_download)
        dataset.make_splits(self.settings)
        tr = dataset.load_split(self.settings, "train")
        ref = dataset.load_split(self.settings, "reference")
        trained = train(self.settings, tr[self.settings.features], tr[self.settings.target].to_numpy(),
                        ref[self.settings.features], ref[self.settings.target].to_numpy())
        self.store.delete_models_except("v1")
        meta = self.registry.register(trained, "v1", None, "Baseline model trained on the training split",
                                      make_current=True,
                                      reference_path=str(self.settings.paths.splits / "reference.csv"))
        self.store.audit("system", "model_registered", {"version": "v1", "metrics": trained.metrics})
        self.registry.clear_cache()
        self.detector.clear_cache()
        self.reset(next(iter(self.settings.scenarios)))
        return meta

    def reset(self, scenario: str, seed: int | None = None, drift_start: int | None = None) -> None:
        """Start a scenario from batch 1. Challenger versions are deleted, so drop cached ones too."""
        with self._lock:
            self.simulator.reset(scenario, seed=seed, drift_start=drift_start)
            self.registry.clear_cache()
            self.detector.clear_cache()

    def is_initialised(self) -> bool:
        return self.store.current_model() is not None

    # ---- main loop --------------------------------------------------------
    def step(self, investigate: bool = True) -> dict:
        """Generate the next production batch and check it."""
        with self._lock:
            batch_id = self.simulator.next_batch()
            return self._check(batch_id, investigate)

    def check(self, batch_id: int | None = None, investigate: bool = True) -> dict:
        with self._lock:
            batch_id = batch_id or self.store.latest_batch_id()
            if not batch_id:
                raise RuntimeError("No production batches yet: run `drift-agent simulate` first.")
            return self._check(batch_id, investigate)

    def _check(self, batch_id: int, investigate: bool) -> dict:
        prev = self.store.run_for_batch(batch_id - 1)
        prev_features = {f["feature"]: f for f in prev["report"]["features"]} if prev else {}
        prev_perf = bool(prev and prev["report"].get("performance")
                         and prev["report"]["performance"].get("degraded"))
        report = self.detector.check(batch_id, prev_features, prev_perf)
        run_id = self._save_run(report)
        self.store.audit("detector", "drift_check", {
            "batch_id": batch_id, "severity": report.severity, "score": report.data_drift_score,
            "reasons": report.reasons,
        }, run_id=run_id)

        result = {"run_id": run_id, "batch_id": batch_id, "severity": report.severity,
                  "score": report.data_drift_score, "reasons": report.reasons, "investigation": None,
                  "actions": []}
        threshold = SEVERITY_ORDER[self.settings["agent"]["investigate_at"]]
        if investigate and SEVERITY_ORDER[report.severity] >= threshold:
            if self._already_handled(run_id, report):
                self.store.audit("guardrail", "investigation_skipped",
                                 {"reason": "same incident already investigated and still open"}, run_id)
            else:
                investigation = self.investigator.investigate(run_id, report)
                result["investigation"] = investigation
                result["actions"] = self.guardrail.handle_recommendation(run_id, investigation)
        return result

    def _already_handled(self, run_id: int, report: DriftReport) -> bool:
        """Avoid re-investigating every batch of the same ongoing incident.

        An incident is considered open while the previous run was also at
        ALERT/CRITICAL and was investigated, and the model version is unchanged.
        """
        prev = self.store.previous_run(run_id)
        if not prev or SEVERITY_ORDER[prev["severity"]] < SEVERITY_ORDER["ALERT"]:
            return False
        if prev["model_version"] != report.model_version:
            return False
        if not self._incident_report_before(prev["id"]):
            return False
        # Re-investigate if the incident escalated to a higher severity.
        return SEVERITY_ORDER[report.severity] <= SEVERITY_ORDER[prev["severity"]]

    def _incident_report_before(self, run_id: int) -> bool:
        """True if some earlier run in the same unbroken ALERT+ streak has a report."""
        current = self.store.get_run(run_id)
        while current and SEVERITY_ORDER[current["severity"]] >= SEVERITY_ORDER["ALERT"]:
            if self.store.get_report(current["id"]):
                return True
            current = self.store.previous_run(current["id"])
        return False

    def investigate_run(self, run_id: int) -> dict:
        """Manually ask the agent to investigate a run (e.g. a WARNING the detector didn't escalate)."""
        with self._lock:
            report = self.get_report(run_id)
            self.store.audit("human", "investigation_requested", {}, run_id)
            investigation = self.investigator.investigate(run_id, report)
            actions = self.guardrail.handle_recommendation(run_id, investigation)
            return {"run_id": run_id, "investigation": investigation, "actions": actions}

    def _save_run(self, report: DriftReport) -> int:
        features = [f.model_dump() for f in report.features]
        return self.store.add_run(
            report.batch_id, report.performance.batch_id if report.performance else None,
            report.model_version, report.severity, report.data_drift_score,
            report.model_dump(), features,
        )

    def get_report(self, run_id: int) -> DriftReport:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"run {run_id} not found")
        return DriftReport.model_validate(run["report"])
