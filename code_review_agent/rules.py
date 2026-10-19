"""Trusted review rules.

Mandatory rules ship with the agent (code_review_agent/rules/). Project rules come
from `.review/rules/*.md` at the PR's *base* commit, so a PR cannot rewrite the
rules it is reviewed against.

`get_rules()` is the retrieval interface. The MVP returns everything (the rules are
small); a keyword or BM25 search can replace it later without changing callers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .policy import PACKAGE_DIR
from .shell import git

PROJECT_RULES_DIR = ".review/rules"


@dataclass(frozen=True)
class Passage:
    text: str
    source: str
    start_line: int
    end_line: int
    mandatory: bool

    @property
    def ref(self) -> str:
        return f"{self.source}#L{self.start_line}-L{self.end_line}"


def split_markdown(text: str, source: str, mandatory: bool) -> list[Passage]:
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("## ")] or [0]
    if starts[0] != 0:
        starts.insert(0, 0)
    passages = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        chunk = "\n".join(lines[start:end]).strip()
        if chunk:
            passages.append(Passage(chunk, source, start + 1, end, mandatory))
    return passages


def load_rules(repo: str | Path, base: str) -> list[Passage]:
    passages: list[Passage] = []
    for path in sorted((PACKAGE_DIR / "rules").glob("*.md")):
        source = f"code_review_agent/rules/{path.name}"
        passages += split_markdown(path.read_text(encoding="utf-8"), source, mandatory=True)

    try:
        names = git(repo, "ls-tree", "-r", "--name-only", base, "--", PROJECT_RULES_DIR).split()
    except RuntimeError:
        names = []
    for name in sorted(n for n in names if n.endswith(".md")):
        text = git(repo, "show", f"{base}:{name}")
        passages += split_markdown(text, f"{name}@base", mandatory=False)
    return passages


def get_rules(passages: list[Passage], query: str = "") -> list[Passage]:
    """Mandatory passages always; optional ones filtered by keyword when a query is given."""
    words = {w for w in re.findall(r"[a-z0-9_]+", query.lower()) if len(w) > 2}
    if not words:
        return list(passages)
    return [p for p in passages if p.mandatory or words & set(re.findall(r"[a-z0-9_]+", p.text.lower()))]


def render(passages: list[Passage]) -> str:
    return "\n\n".join(f"[source: {p.ref}]\n{p.text}" for p in passages)
