"""The drift statistics must give the expected answers on distributions we control."""

import numpy as np
import pandas as pd
import pytest

from drift_agent.detection import stats

rng = np.random.default_rng(0)


def test_psi_is_zero_for_identical_proportions():
    p = np.array([0.2, 0.3, 0.5])
    assert stats.psi_from_proportions(p, p) == pytest.approx(0.0, abs=1e-12)


def test_psi_matches_hand_computation():
    ref = np.array([0.5, 0.5])
    cur = np.array([0.8, 0.2])
    expected = (0.8 - 0.5) * np.log(0.8 / 0.5) + (0.2 - 0.5) * np.log(0.2 / 0.5)
    assert stats.psi_from_proportions(ref, cur) == pytest.approx(expected)


def test_psi_handles_empty_bins():
    assert np.isfinite(stats.psi_from_proportions(np.array([1.0, 0.0]), np.array([0.0, 1.0])))


def test_same_distribution_is_stable():
    ref, cur = rng.normal(0, 1, 5000), rng.normal(0, 1, 1000)
    r = stats.numeric_test(ref, cur)
    assert r.psi < 0.1
    assert r.effect < 0.1


def test_shifted_distribution_is_detected():
    ref, cur = rng.normal(0, 1, 5000), rng.normal(1, 1, 1000)
    r = stats.numeric_test(ref, cur)
    assert r.p_value < 1e-6
    assert r.effect > 0.3
    assert r.psi > 0.25


def test_scale_bug_gives_huge_psi():
    ref = rng.lognormal(6, 1, 5000)
    r = stats.numeric_test(ref, ref[:1000] * 100)
    assert r.psi > 2


def test_numeric_ignores_missing_values():
    ref = rng.normal(0, 1, 2000)
    cur = rng.normal(0, 1, 1000)
    cur[:300] = np.nan
    assert stats.numeric_test(ref, cur).psi < 0.1


def test_categorical_same_mix_is_stable():
    cats = ["a", "b", "c"]
    ref = pd.Series(rng.choice(cats, 5000, p=[0.5, 0.3, 0.2]))
    cur = pd.Series(rng.choice(cats, 1000, p=[0.5, 0.3, 0.2]))
    r = stats.categorical_test(ref, cur)
    assert r.effect < 0.1 and r.psi < 0.1


def test_categorical_shift_and_new_category_are_detected():
    ref = pd.Series(rng.choice(["a", "b"], 5000, p=[0.5, 0.5]))
    cur = pd.Series(rng.choice(["a", "b", "new"], 1000, p=[0.3, 0.3, 0.4]))
    r = stats.categorical_test(ref, cur)
    assert r.p_value < 1e-6
    assert r.psi > 0.25
    cats, rc, cc = stats.category_proportions(ref, cur)
    assert "new" in cats and rc[cats.index("new")] == 0


def test_js_distance_is_bounded():
    ref = pd.Series(["a"] * 100)
    cur = pd.Series(["b"] * 100)
    r = stats.categorical_test(ref, cur)
    assert 0.99 <= r.effect <= 1.0
