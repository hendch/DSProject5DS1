"""Repository-wide pytest settings."""
from pathlib import Path

# drift_detection's `data/` package (dataset download + simulator) has not been pushed
# yet (see README, "Integration status"). Skip its tests with a clear reason instead of
# failing the whole run at collection time.
_DRIFT = Path(__file__).parent / "agents" / "drift_detection"
collect_ignore = [] if (_DRIFT / "data").is_dir() else ["agents/drift_detection/tests"]


def pytest_report_header(config):
    if collect_ignore:
        return "drift_detection tests skipped: agents/drift_detection/data/ is missing (not pushed yet)"
