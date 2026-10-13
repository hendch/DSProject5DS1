import train  # noqa: F401  (importing train must register the extra metrics)
from registry import METRICS


def test_f1_metric_is_registered():
    assert "f1" in METRICS
