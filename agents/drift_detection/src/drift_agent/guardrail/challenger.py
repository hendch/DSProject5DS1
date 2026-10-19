"""Train a challenger model in the sandbox and compare it with the current model.

The challenger is registered as a new version but NOT made current: promotion
is a separate, human-approved action.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from drift_agent.data.dataset import load_split
from drift_agent.data.simulator import LABEL_COL
from drift_agent.model.registry import evaluate, train

if TYPE_CHECKING:
    from drift_agent.service import DriftService


def train_challenger(service: "DriftService") -> dict:
    settings, store = service.settings, service.store
    cfg = settings["retraining"]
    labelled = store.labelled_batches()
    holdout_n = int(cfg["holdout_batches"])
    if len(labelled) < holdout_n + 2:
        return {"trained": False, "reason": f"need at least {holdout_n + 2} labelled batches, "
                                            f"have {len(labelled)}"}

    holdout_ids = [b["id"] for b in labelled[-holdout_n:]]
    recent_ids = [b["id"] for b in labelled[:-holdout_n]]
    load = lambda i: service.simulator.load_batch(i, with_labels=True)  # noqa: E731
    holdout = pd.concat([load(i) for i in holdout_ids], ignore_index=True)
    recent = pd.concat([load(i) for i in recent_ids], ignore_index=True)

    base = load_split(settings, "train")
    feats = settings.features
    X = pd.concat([base[feats], recent[feats]], ignore_index=True)
    y = np.concatenate([base[settings.target].to_numpy(int), recent[LABEL_COL].to_numpy(int)])
    weights = np.concatenate([np.ones(len(base)), np.full(len(recent), float(cfg["recent_weight"]))])
    X_hold, y_hold = holdout[feats], holdout[LABEL_COL].to_numpy(int)

    champion_meta, champion = service.registry.current()
    champion_metrics = evaluate(champion, X_hold, y_hold, champion_meta["threshold"])
    trained = train(settings, X, y, X_hold, y_hold, sample_weight=weights)

    version = service.registry.next_version()
    # The new version's "normal" is the most recent labelled production data.
    ref_path = settings.paths.models / f"reference_{version}.csv"
    holdout[feats].to_csv(ref_path, index=False)
    service.registry.register(
        trained, version, parent=champion_meta["version"],
        notes=f"Challenger trained on the training split + labelled batches {recent_ids[0]}-{recent_ids[-1]} "
              f"(weight x{cfg['recent_weight']}); evaluated on batches {holdout_ids}",
        make_current=False, reference_path=str(ref_path),
    )
    gain = trained.metrics["auc"] - champion_metrics["auc"]
    return {
        "trained": True, "challenger_version": version, "champion_version": champion_meta["version"],
        "holdout_batches": holdout_ids, "train_batches": recent_ids,
        "champion_metrics": champion_metrics, "challenger_metrics": trained.metrics,
        "auc_gain": round(gain, 4), "better": bool(gain >= cfg["min_auc_gain"]),
        "min_auc_gain": cfg["min_auc_gain"],
    }
