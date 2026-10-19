"""Run the real pipeline (scripted review, real ruff/pytest/git) on the demo scenarios."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "demo"))

from run_local_demo import baseline, make_repo  # noqa: E402

from code_review_agent.agent import main  # noqa: E402
from code_review_agent.shell import git  # noqa: E402

pytest.importorskip("sklearn")


def _run(tmp_path, scenario):
    repo, base, head = make_repo(tmp_path, scenario)
    baseline(repo, tmp_path / "reports")
    code = main(["--repo", str(repo), "--base", base, "--head", head,
                 "--lint", str(tmp_path / "reports" / "lint.json"),
                 "--junit", str(tmp_path / "reports" / "junit.xml"),
                 "--out", str(tmp_path / "out"),
                 "--review-json", str(ROOT / "demo" / "scripted" / f"{scenario}.json")])
    report = json.loads((tmp_path / "out" / "report.json").read_text())
    # The agent must always leave the checkout untouched at the PR head.
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert git(repo, "rev-parse", "HEAD").strip() == head
    return code, report


def test_leaky_pr_is_repaired_and_findings_kept(tmp_path):
    code, report = _run(tmp_path, "leaky_pr")
    assert code == 0 and report["status"] == "repaired"
    assert report["fixed_files"] == {"train.py": ["F401", "I001", "W291"]}  # E501 request dropped
    assert {f["category"] for f in report["findings"]} >= {"leakage", "reproducibility", "evaluation", "injection"}
    patch = (tmp_path / "out" / "fix.patch").read_text()
    edited = [ln for ln in patch.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    assert edited and all(ln[1:].lstrip().startswith(("import", "from")) or ln.startswith("-    model")
                          or ln.startswith("+    model") for ln in edited)


def test_side_effect_fix_is_reverted(tmp_path):
    code, report = _run(tmp_path, "side_effect_pr")
    assert code == 1 and report["status"] == "validation_failed"
    assert not (tmp_path / "out" / "fix.patch").exists()
