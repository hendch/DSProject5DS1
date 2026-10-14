"""Model training, evaluation and the versioned model registry.

Model artifacts are joblib files that only this application writes, into the
local `models/` folder. joblib uses pickle, so never load model files from an
untrusted source.
"""

from __future__ import annotations

from dataclasses import dataclass

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.metrics import f1_score, precision_recall_curve, precision_score, recall_score, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from drift_agent.config import Settings
from drift_agent.store.db import Store


def build_pipeline(settings: Settings, seed: int) -> Pipeline:
    pre = ColumnTransformer([
        ("num", SimpleImputer(strategy="median"), settings.numeric),
        ("cat", Pipeline([
            ("impute", SimpleImputer(strategy="constant", fill_value="missing")),
            ("onehot", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
        ]), settings.categorical),
    ])
    m = settings["model"]
    clf = HistGradientBoostingClassifier(
        max_iter=m["max_iter"], learning_rate=m["learning_rate"], max_leaf_nodes=m["max_leaf_nodes"],
        l2_regularization=m["l2_regularization"], early_stopping=True,
        validation_fraction=m["early_stopping_fraction"], random_state=seed,
    )
    return Pipeline([("pre", pre), ("clf", clf)])


def best_f1_threshold(y: np.ndarray, scores: np.ndarray) -> float:
    precision, recall, thresholds = precision_recall_curve(y, scores)
    f1 = 2 * precision * recall / np.clip(precision + recall, 1e-12, None)
    return float(thresholds[int(np.argmax(f1[:-1]))])


def evaluate(model: Pipeline, X: pd.DataFrame, y: np.ndarray, threshold: float) -> dict:
    scores = model.predict_proba(X)[:, 1]
    pred = (scores >= threshold).astype(int)
    return {
        "auc": float(roc_auc_score(y, scores)) if len(np.unique(y)) > 1 else float("nan"),
        "f1": float(f1_score(y, pred, zero_division=0)),
        "precision": float(precision_score(y, pred, zero_division=0)),
        "recall": float(recall_score(y, pred, zero_division=0)),
        "positive_rate": float(pred.mean()),
        "label_rate": float(np.mean(y)),
        "n": int(len(y)),
    }


def compute_importance(settings: Settings, model: Pipeline, X: pd.DataFrame, y: np.ndarray, seed: int) -> dict:
    """Permutation importance (ROC-AUC drop when a feature is shuffled), normalised to sum to 1."""
    m = settings["model"]
    sample = X.sample(n=min(len(X), m["importance_sample"]), random_state=seed)
    result = permutation_importance(
        model, sample, y[sample.index.to_numpy()], scoring="roc_auc",
        n_repeats=m["importance_repeats"], random_state=seed, n_jobs=1,
    )
    raw = np.clip(result.importances_mean, 0, None)
    total = raw.sum() or 1.0
    return {f: float(v / total) for f, v in zip(X.columns, raw)}


@dataclass
class TrainedModel:
    model: Pipeline
    threshold: float
    metrics: dict
    importance: dict


def train(settings: Settings, X: pd.DataFrame, y: np.ndarray, X_eval: pd.DataFrame, y_eval: np.ndarray,
          sample_weight: np.ndarray | None = None, seed: int | None = None) -> TrainedModel:
    """Fit a model, pick the F1-optimal threshold on a validation slice, score it on X_eval."""
    seed = settings["data"]["seed"] if seed is None else seed
    X = X.reset_index(drop=True)
    idx_fit, idx_val = train_test_split(np.arange(len(X)), test_size=settings["model"]["threshold_tuning_fraction"],
                                        random_state=seed, stratify=y)
    model = build_pipeline(settings, seed)
    fit_kwargs = {}
    if sample_weight is not None:
        fit_kwargs["clf__sample_weight"] = sample_weight[idx_fit]
    model.fit(X.iloc[idx_fit], y[idx_fit], **fit_kwargs)
    threshold = best_f1_threshold(y[idx_val], model.predict_proba(X.iloc[idx_val])[:, 1])
    # Refit on everything now that the threshold is chosen.
    fit_kwargs = {"clf__sample_weight": sample_weight} if sample_weight is not None else {}
    model.fit(X, y, **fit_kwargs)
    X_eval = X_eval.reset_index(drop=True)
    metrics = evaluate(model, X_eval, y_eval, threshold)
    importance = compute_importance(settings, model, X_eval, y_eval, seed)
    return TrainedModel(model, threshold, metrics, importance)


class Registry:
    """Versioned models: files on disk, metadata and the `current` pointer in SQLite."""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self._cache: dict[str, Pipeline] = {}

    def register(self, trained: TrainedModel, version: str, parent: str | None, notes: str,
                 make_current: bool, reference_path: str) -> dict:
        """Save a model. `reference_path` is the data this version's drift checks compare against."""
        path = self.settings.paths.models / f"{version}.joblib"
        joblib.dump(trained.model, path)
        self.store.add_model_version(version, str(path), reference_path, parent, trained.threshold,
                                     trained.metrics, trained.importance, notes, make_current)
        self._cache[version] = trained.model
        return self.store.get_model(version)

    def next_version(self) -> str:
        existing = [m["version"] for m in self.store.list_models()]
        n = max((int(v[1:]) for v in existing if v[1:].isdigit()), default=0)
        return f"v{n + 1}"

    def clear_cache(self) -> None:
        self._cache.clear()

    def load(self, version: str) -> Pipeline:
        if version not in self._cache:
            meta = self.store.get_model(version)
            if meta is None:
                raise KeyError(f"unknown model version {version}")
            self._cache[version] = joblib.load(meta["path"])
        return self._cache[version]

    def current(self) -> tuple[dict, Pipeline]:
        meta = self.store.current_model()
        if meta is None:
            raise RuntimeError("No model registered: run `drift-agent init` first.")
        return meta, self.load(meta["version"])
