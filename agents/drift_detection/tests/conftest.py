"""Fixtures: an isolated workspace that reuses the real dataset splits and the baseline model.

Integration tests need `drift-agent init` to have been run once (they are
skipped otherwise). They never touch the demo database.
"""

import dataclasses

import pytest

from drift_agent.config import Paths, load_settings
from drift_agent.service import DriftService


@pytest.fixture(scope="session")
def settings():
    return load_settings()


@pytest.fixture()
def service(settings, tmp_path):
    base = DriftService(settings, use_llm=False)
    v1 = base.store.get_model("v1")
    if v1 is None:
        pytest.skip("run `drift-agent init` first")
    s = dataclasses.replace(settings, paths=Paths(settings.paths.root, tmp_path))
    s.paths.ensure()
    svc = DriftService(s, use_llm=False)
    svc.store.add_model_version("v1", v1["path"], v1["reference_path"], None, v1["threshold"],
                                v1["metrics"], v1["importance"], v1["notes"], make_current=True)
    return svc
