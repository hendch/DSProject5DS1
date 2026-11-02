"""Production traffic simulator with injectable drift scenarios.

Each call to `next_batch` samples clients from the production pool, applies the
active scenario's drift (if the batch is past `drift_start`), scores the batch
with the current model and stores it. True labels are released `label_delay`
batches later, as they would be in real life.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from agents.drift_detection.config import Settings
from agents.drift_detection.data.dataset import load_split, read_frame
from agents.drift_detection.model.registry import Registry
from agents.drift_detection.store.db import Store

# Scenario kinds implemented below. Their parameters, labels, descriptions and
# expected diagnoses live in config.yaml under simulation.scenarios.
SCENARIO_KINDS = ("clean", "gradual", "pipeline_bug", "concept")

LABEL_COL = "__label"
SCORE_COL = "__score"
PRED_COL = "__pred"


class Simulator:
    def __init__(self, settings: Settings, store: Store, registry: Registry):
        self.settings = settings
        self.store = store
        self.registry = registry
        self.cfg = settings["simulation"]
        self._pool: pd.DataFrame | None = None
        unknown = set(settings.scenarios) - set(SCENARIO_KINDS)
        if unknown:
            raise ValueError(f"config.yaml defines scenarios with no implementation: {sorted(unknown)}; "
                             f"implemented: {SCENARIO_KINDS}")

    @property
    def pool(self) -> pd.DataFrame:
        if self._pool is None:
            self._pool = load_split(self.settings, "production")
        return self._pool

    # ---- state ----------------------------------------------------------
    def reset(self, scenario: str, seed: int | None = None, drift_start: int | None = None) -> None:
        if scenario not in self.settings.scenarios:
            raise ValueError(f"unknown scenario {scenario!r}; choose from {', '.join(self.settings.scenarios)}")
        self.store.reset_simulation()
        for f in self.settings.paths.batches.glob("batch_*.csv"):
            f.unlink()
        # Drop challengers and go back to the baseline so every scenario starts equal.
        if self.store.get_model("v1"):
            for m in self.store.list_models():
                if m["version"] != "v1":
                    for path in (m["path"], m["reference_path"]):
                        Path(path).unlink(missing_ok=True)
            self.store.delete_models_except("v1")
            self.store.set_current_model("v1")
        self.store.set_state("scenario", scenario)
        self.store.set_state("seed", seed if seed is not None else self.settings["data"]["seed"])
        self.store.set_state("drift_start", drift_start or self.cfg["drift_start"])
        self.store.audit("system", "simulation_reset", {"scenario": scenario, "drift_start": drift_start
                                                        or self.cfg["drift_start"]})

    @property
    def scenario(self) -> str:
        return self.store.get_state("scenario", next(iter(self.settings.scenarios)))

    @property
    def drift_start(self) -> int:
        return int(self.store.get_state("drift_start", self.cfg["drift_start"]))

    # ---- batch generation -------------------------------------------------
    def next_batch(self) -> int:
        batch_id = self.store.latest_batch_id() + 1
        seed = int(self.store.get_state("seed", 0))
        rng = np.random.default_rng([seed, batch_id])
        scenario = self.scenario
        progress = batch_id - self.drift_start + 1  # 1 on the first drifted batch, <= 0 before
        df = self._sample(rng, scenario, progress)
        df = self._inject(df, rng, scenario, progress)

        meta, model = self.registry.current()
        X = df[self.settings.features]
        df[SCORE_COL] = model.predict_proba(X)[:, 1]
        df[PRED_COL] = (df[SCORE_COL] >= meta["threshold"]).astype(int)

        path = self.settings.paths.batches / f"batch_{batch_id:04d}.csv"
        df.to_csv(path, index=False)
        self.store.add_batch(batch_id, scenario, len(df), meta["version"], str(path))
        self._emit_events(batch_id, rng, scenario, progress)
        released = self.store.release_labels(batch_id - self.cfg["label_delay"])
        self.store.audit("simulator", "batch_generated", {
            "batch_id": batch_id, "rows": len(df), "model_version": meta["version"],
            "labels_released_for": released,
        })
        return batch_id

    def _params(self, scenario: str) -> dict:
        return self.settings.scenarios[scenario]

    def _sample(self, rng: np.random.Generator, scenario: str, progress: int) -> pd.DataFrame:
        n = self.cfg["batch_size"]
        pool = self.pool
        weights = None
        if scenario == "gradual" and progress > 0:
            # Tilt sampling towards higher values of the feature, a little more each batch.
            p = self._params(scenario)
            strength = min(p["strength_per_batch"] * progress, p["max_strength"])
            col = pool[p["feature"]]
            z = (col - col.mean()) / col.std()
            w = np.exp(strength * z.to_numpy())
            weights = w / w.sum()
        idx = rng.choice(len(pool), size=n, replace=False, p=weights)
        df = pool.iloc[idx].reset_index(drop=True).copy()
        df = df.rename(columns={self.settings.target: LABEL_COL})
        return df

    def _inject(self, df: pd.DataFrame, rng: np.random.Generator, scenario: str, progress: int) -> pd.DataFrame:
        if progress <= 0:
            return df
        n = len(df)
        p = self._params(scenario)
        if scenario == "pipeline_bug":
            # A new ETL release changes a unit, drops values and sends an unknown category.
            df[p["scale_feature"]] = df[p["scale_feature"]] * p["scale_factor"]
            df.loc[rng.random(n) < p["missing_rate"], p["missing_feature"]] = np.nan
            df.loc[rng.random(n) < p["unseen_rate"], p["unseen_feature"]] = p["unseen_value"]
        elif scenario == "concept":
            # Inside the segment, subscribing now depends on the driver feature being
            # high, not on the pattern the model learned.
            share = min(p["start_share"] + p["share_step"] * (progress - 1), 1.0)
            seg = (df[p["segment_feature"]] == p["segment_value"]).to_numpy()
            affected = seg & (rng.random(n) < share)
            cutoff = np.nanpercentile(self.pool[p["driver_feature"]], p["driver_percentile"])
            high = df[p["driver_feature"]] >= cutoff
            new_label = (high & (rng.random(n) < p["rate_if_driver_high"])) |                         (~high & (rng.random(n) < p["rate_if_driver_low"]))
            df.loc[affected, LABEL_COL] = new_label[affected].astype(int)
        return df

    def _emit_events(self, batch_id: int, rng: np.random.Generator, scenario: str, progress: int) -> None:
        noise = self.cfg["noise_events"]
        if noise and rng.random() < self.cfg["noise_event_probability"]:
            source, msg = noise[int(rng.integers(len(noise)))]
            self.store.add_event(batch_id, source, msg)
        event = self._params(scenario).get("event")
        if event and progress == 1:
            self.store.add_event(batch_id, event[0], event[1])

    # ---- reading batches --------------------------------------------------
    def load_batch(self, batch_id: int, with_labels: bool = False) -> pd.DataFrame:
        meta = self.store.get_batch(batch_id)
        if meta is None:
            raise KeyError(f"batch {batch_id} not found")
        df = read_frame(self.settings, meta["path"])
        if not with_labels:
            df = df.drop(columns=[LABEL_COL])
        elif not meta["labels_available"]:
            raise PermissionError(f"labels for batch {batch_id} have not arrived yet")
        return df
