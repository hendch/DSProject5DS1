"""Importing this module registers extra metrics (a side effect)."""

from sklearn.metrics import f1_score

from registry import METRICS

METRICS["f1"] = f1_score
