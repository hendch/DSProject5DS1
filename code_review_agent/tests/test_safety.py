"""Static checks that hard limits stay in the code."""

import re
from pathlib import Path

from code_review_agent.shell import clean_env

PKG = Path(__file__).resolve().parents[1]
SOURCES = {p.name: p.read_text(encoding="utf-8") for p in PKG.glob("*.py")}


def test_no_merge_or_approve_anywhere():
    for name, src in SOURCES.items():
        assert not re.search(r"pr\"?,\s*\"?(merge|review)|/merge|--admin|--approve|auto-merge", src), name


def test_only_publish_pushes():
    assert [n for n, s in SOURCES.items() if '"push"' in s] == ["publish.py"]


def test_no_unsafe_fixes():
    assert all('"--unsafe-fixes"' not in s for s in SOURCES.values())


def test_secrets_are_scrubbed_from_subprocesses(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    env = clean_env()
    assert not {"LLM_API_KEY", "GITHUB_TOKEN", "OPENROUTER_API_KEY"} & set(env)
