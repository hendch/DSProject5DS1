"""Collect everything the review needs into one ReviewContext. No LLM here."""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .rules import Passage, load_rules
from .shell import git


@dataclass
class LintIssue:
    code: str
    file: str
    line: int
    message: str
    fixable: bool


@dataclass
class TestFailure:
    name: str
    message: str


@dataclass
class ReviewContext:
    repo: Path
    base: str
    head: str
    changed_files: list[str]
    diff: str
    lint: list[LintIssue] = field(default_factory=list)
    test_failures: list[TestFailure] = field(default_factory=list)
    tests_summary: str = "no test report"
    diagnosis: str | None = None
    rules: list[Passage] = field(default_factory=list)

    def lint_for(self, path: str) -> list[LintIssue]:
        return [i for i in self.lint if i.file == path]


def _relpath(repo: Path, filename: str) -> str:
    p = Path(filename)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(repo.resolve())
        except ValueError:
            return filename.replace("\\", "/")
    return p.as_posix()


def parse_ruff_json(repo: Path, path: str | Path | None) -> list[LintIssue]:
    if not path or not Path(path).is_file():
        return []
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return []
    return [
        LintIssue(
            code=item.get("code") or "",
            file=_relpath(repo, item["filename"]),
            line=(item.get("location") or {}).get("row", 0),
            message=item.get("message", ""),
            fixable=item.get("fix") is not None,
        )
        for item in json.loads(text)
    ]


def parse_junit(path: str | Path | None) -> tuple[str, list[TestFailure]]:
    if not path or not Path(path).is_file():
        return "no test report", []
    root = ET.parse(path).getroot()
    failures, total = [], 0
    for case in root.iter("testcase"):
        total += 1
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is not None:
                name = f"{case.get('classname', '')}::{case.get('name', '')}"
                failures.append(TestFailure(name, (node.get("message") or node.text or "")[:500]))
    return f"{total} tests, {len(failures)} failed or errored", failures


def build_context(
    repo: str | Path,
    base: str,
    head: str,
    lint_report: str | None = None,
    junit_report: str | None = None,
    diagnosis_path: str | None = None,
) -> ReviewContext:
    repo = Path(repo).resolve()
    try:
        merge_base = git(repo, "merge-base", base, head).strip()
    except RuntimeError:
        merge_base = base
    names = git(repo, "diff", "--name-only", "--diff-filter=ACMR", merge_base, head).split("\n")
    diff = git(repo, "diff", "--unified=3", merge_base, head)
    summary, failures = parse_junit(junit_report)

    diagnosis = None
    if diagnosis_path and os.path.isfile(diagnosis_path):
        diagnosis = Path(diagnosis_path).read_text(encoding="utf-8")[:8000]

    return ReviewContext(
        repo=repo,
        base=merge_base,
        head=head,
        changed_files=[n for n in names if n],
        diff=diff,
        lint=parse_ruff_json(repo, lint_report),
        test_failures=failures,
        tests_summary=summary,
        diagnosis=diagnosis,
        rules=load_rules(repo, merge_base),
    )
