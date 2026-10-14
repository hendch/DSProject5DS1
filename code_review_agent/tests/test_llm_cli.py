import json
from types import SimpleNamespace

import pytest

from code_review_agent import llm
from code_review_agent.context import LintIssue, build_context
from code_review_agent.policy import load_policy
from code_review_agent.tools import ToolBox

POLICY = load_policy()


def _setup(make_repo, monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/usr/bin/claude")
    repo, base, head = make_repo({"a.py": "x = 1\n"}, {"a.py": "import os\nx = 2\n"})
    ctx = build_context(repo, base, head)
    ctx.lint = [LintIssue("F401", "a.py", 1, "unused", True)]
    return ctx, ToolBox(ctx, POLICY)


def _runner(payload, seen):
    def run(cmd, **kwargs):
        seen.update(cmd=cmd, **kwargs)
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    return run


def test_cli_answer_goes_through_toolbox_checks(make_repo, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth")
    ctx, box = _setup(make_repo, monkeypatch)
    answer = {"summary": "s", "findings": [],
              "fix_requests": [{"file": "a.py", "rule_code": "F401"}, {"file": "a.py", "rule_code": "E501"}]}
    seen = {}
    payload = {"type": "result", "subtype": "success", "is_error": False, "structured_output": answer,
               "usage": {"input_tokens": 5, "cache_creation_input_tokens": 100, "output_tokens": 50},
               "total_cost_usd": 0.001}
    out = llm.run_review_cli(ctx, box, POLICY, "claude-haiku-5-5", runner=_runner(payload, seen))

    assert out.stop_reason == "submitted" and out.review == {"summary": "s", "findings": []}
    assert [(r.file, r.rule) for r in box.fix_requests] == [("a.py", "F401")]  # E501 dropped
    assert out.usage["input_tokens"] == 105 and out.usage["api_equivalent_usd"] == 0.001

    cmd = seen["cmd"]
    assert cmd[cmd.index("--tools") + 1] == ""  # all tools disabled
    assert "--json-schema" in cmd and cmd[cmd.index("--model") + 1] == "claude-haiku-5-5"
    assert "GITHUB_TOKEN" not in seen["env"] and seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth"
    assert str(ctx.repo) != seen["cwd"]  # repo CLAUDE.md is never loaded
    assert "import os" in seen["input"]  # file contents included since there is no read_file tool


@pytest.mark.parametrize("payload", [
    {"is_error": True, "subtype": "error_max_turns", "result": "x"},
    {"is_error": False, "subtype": "success", "structured_output": None},
])
def test_cli_errors_give_no_review(make_repo, monkeypatch, payload):
    ctx, box = _setup(make_repo, monkeypatch)
    out = llm.run_review_cli(ctx, box, POLICY, "m", runner=_runner(payload, {}))
    assert out.review is None and out.stop_reason.startswith("cli_error")
    assert box.fix_requests == []


def test_cli_non_json_output(make_repo, monkeypatch):
    ctx, box = _setup(make_repo, monkeypatch)

    def run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="Not logged in", stderr="")

    out = llm.run_review_cli(ctx, box, POLICY, "m", runner=run)
    assert out.review is None and "exit 1" in out.stop_reason


def test_cli_invalid_review_is_rejected(make_repo, monkeypatch):
    ctx, box = _setup(make_repo, monkeypatch)
    bad = {"summary": "s", "findings": [{"category": "vibes"}], "fix_requests": []}
    out = llm.run_review_cli(ctx, box, POLICY, "m", runner=_runner({"structured_output": bad}, {}))
    assert out.review is None and out.stop_reason.startswith("invalid_review")
