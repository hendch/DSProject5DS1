import json

from code_review_agent.context import LintIssue, build_context
from code_review_agent.policy import load_policy
from code_review_agent.tools import ToolBox

POLICY = load_policy()


def _box(make_repo):
    repo, base, head = make_repo(
        {"train.py": "x = 1\n"},
        {"train.py": "import os\nx = 1\n", ".env": "SECRET=1\n", "pkg/__init__.py": "import os\n"},
    )
    ctx = build_context(repo, base, head)
    ctx.lint = [LintIssue("F401", "train.py", 1, "unused", True), LintIssue("F401", "pkg/__init__.py", 1, "u", True)]
    return ToolBox(ctx, POLICY)


def test_read_file_is_limited_to_safe_repo_paths(make_repo):
    box = _box(make_repo)
    assert "import os" in box.call("read_file", json.dumps({"path": "train.py"}))
    assert box.call("read_file", json.dumps({"path": "../outside.txt"})).startswith("error")
    assert box.call("read_file", json.dumps({"path": ".env"})).startswith("error")
    assert box.call("read_file", json.dumps({"path": ".git/config"})).startswith("error")


def test_repo_content_is_wrapped_as_untrusted(make_repo):
    out = _box(make_repo).call("read_file", json.dumps({"path": "train.py"}))
    assert out.startswith("<untrusted_file")


def test_fix_requests_are_checked(make_repo):
    box = _box(make_repo)
    assert box.call("request_safe_fix", json.dumps({"file": "train.py", "rule_code": "F401"})).startswith("queued")
    assert box.call("request_safe_fix", json.dumps({"file": "train.py", "rule_code": "E501"})).startswith("rejected")
    assert box.call("request_safe_fix", json.dumps({"file": "train.py", "rule_code": "I001"})).startswith("rejected")
    assert box.call("request_safe_fix", json.dumps({"file": "pkg/__init__.py", "rule_code": "F401"})).startswith(
        "rejected")
    assert len(box.fix_requests) == 1


def test_submit_review_validates_schema(make_repo):
    box = _box(make_repo)
    assert box.call("submit_review", json.dumps({"summary": "x"})).startswith("error")
    bad = {"summary": "x", "findings": [{"category": "vibes"}]}
    assert box.call("submit_review", json.dumps(bad)).startswith("error")
    assert box.review is None
    assert box.call("submit_review", json.dumps({"summary": "ok", "findings": []})) == "accepted"


def test_unknown_tool_and_bad_json(make_repo):
    box = _box(make_repo)
    assert box.call("run_shell", "{}").startswith("error: unknown tool")
    assert box.call("read_file", "{not json").startswith("error")
