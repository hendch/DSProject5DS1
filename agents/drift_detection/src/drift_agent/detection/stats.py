"""Statistical drift tests. Pure functions, no I/O, no LLM.

Numeric features: Kolmogorov-Smirnov test (significance + distance D) and PSI.
Categorical features: chi-square test (significance), Jensen-Shannon distance
(effect size) and PSI.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats
from scipy.spatial.distance import jensenshannon

EPS = 1e-4


def psi_from_proportions(ref: np.ndarray, cur: np.ndarray) -> float:
    """PSI = sum((cur - ref) * ln(cur / ref)). Empty bins get a small epsilon."""
    ref = np.clip(np.asarray(ref, dtype=float), EPS, None)
    cur = np.clip(np.asarray(cur, dtype=float), EPS, None)
    ref, cur = ref / ref.sum(), cur / cur.sum()
    return float(np.sum((cur - ref) * np.log(cur / ref)))


def quantile_edges(ref: np.ndarray, bins: int) -> np.ndarray:
    """Bin edges at reference quantiles; open-ended so out-of-range values still land in a bin."""
    qs = np.quantile(ref, np.linspace(0, 1, bins + 1)[1:-1])
    edges = np.unique(qs)
    return np.concatenate([[-np.inf], edges, [np.inf]])


def binned_proportions(values: np.ndarray, edges: np.ndarray) -> np.ndarray:
    counts, _ = np.histogram(values, bins=edges)
    return counts / max(counts.sum(), 1)


def numeric_psi(ref: np.ndarray, cur: np.ndarray, bins: int = 10) -> float:
    edges = quantile_edges(ref, bins)
    return psi_from_proportions(binned_proportions(ref, edges), binned_proportions(cur, edges))


def category_proportions(ref: pd.Series, cur: pd.Series) -> tuple[list[str], np.ndarray, np.ndarray]:
    """Aligned category shares; categories unseen in reference are kept so they show up in PSI."""
    ref_counts = ref.value_counts()
    cur_counts = cur.value_counts()
    cats = sorted(set(ref_counts.index) | set(cur_counts.index), key=str)
    r = np.array([ref_counts.get(c, 0) for c in cats], dtype=float)
    c = np.array([cur_counts.get(k, 0) for k in cats], dtype=float)
    return [str(x) for x in cats], r, c


@dataclass
class TestResult:
    statistic: float      # KS D or chi-square statistic
    p_value: float
    effect: float         # KS D (numeric) or Jensen-Shannon distance (categorical)
    psi: float


def numeric_test(ref: np.ndarray, cur: np.ndarray, bins: int = 10) -> TestResult:
    ref = ref[~np.isnan(ref)]
    cur = cur[~np.isnan(cur)]
    if len(ref) == 0 or len(cur) == 0:
        return TestResult(float("nan"), float("nan"), 0.0, 0.0)
    ks = stats.ks_2samp(ref, cur)
    return TestResult(float(ks.statistic), float(ks.pvalue), float(ks.statistic), numeric_psi(ref, cur, bins))


def categorical_test(ref: pd.Series, cur: pd.Series) -> TestResult:
    ref, cur = ref.dropna(), cur.dropna()
    if len(ref) == 0 or len(cur) == 0:
        return TestResult(float("nan"), float("nan"), 0.0, 0.0)
    _, r, c = category_proportions(ref, cur)
    # Chi-square test of homogeneity on the 2 x k contingency table.
    table = np.vstack([r, c])
    table = table[:, table.sum(axis=0) > 0]
    if table.shape[1] < 2:
        chi2, p = 0.0, 1.0
    else:
        chi2, p, _, _ = stats.chi2_contingency(table)
    js = float(jensenshannon(r / r.sum(), c / c.sum(), base=2))
    return TestResult(float(chi2), float(p), js, psi_from_proportions(r / r.sum(), c / c.sum()))
