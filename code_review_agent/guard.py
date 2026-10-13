"""Policy checks on the change the fixer produced. Any violation means: revert, report, stop."""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

from .policy import Policy
from .shell import git, run


class _StripImports(ast.NodeTransformer):
    def visit_Import(self, node):  # noqa: N802
        return None

    def visit_ImportFrom(self, node):  # noqa: N802
        return None


def same_code_ignoring_imports(old: str, new: str) -> bool:
    """True if the two sources differ only in imports, whitespace, or formatting."""
    try:
        a, b = ast.parse(old), ast.parse(new)
    except SyntaxError:
        return False
    strip = _StripImports()
    return ast.dump(strip.visit(a)) == ast.dump(strip.visit(b))


def _numstat(repo: Path) -> list[tuple[int, int, str]]:
    rows = []
    for line in git(repo, "diff", "--numstat", "HEAD").splitlines():
        added, deleted, path = line.split("\t", 2)
        rows.append((int(added) if added != "-" else 10**6, int(deleted) if deleted != "-" else 10**6, path))
    return rows


def untracked_files(repo: str | Path) -> set[str]:
    return {ln[3:] for ln in git(repo, "status", "--porcelain").splitlines() if ln.startswith("??")}


def check_worktree(
    repo: str | Path,
    policy: Policy,
    allowed_files: dict[str, list[str]],
    untracked_before: set[str] = frozenset(),
    python: str = sys.executable,
) -> list[str]:
    """allowed_files maps each file the fixer was allowed to touch to its requested rule codes."""
    repo = Path(repo)
    problems: list[str] = []

    new_files = sorted(untracked_files(repo) - set(untracked_before))
    if new_files:
        problems.append(f"new files appeared: {', '.join(new_files[:5])}")

    rows = _numstat(repo)
    total = 0
    for added, deleted, path in rows:
        total += added + deleted
        if path not in allowed_files:
            problems.append(f"{path}: not a file the fixer was allowed to change")
        if policy.is_protected(path):
            problems.append(f"{path}: protected path")
        if path.endswith(".py") and path in allowed_files:
            old = git(repo, "show", f"HEAD:{path}")
            new = (repo / path).read_text(encoding="utf-8")
            if not same_code_ignoring_imports(old, new):
                problems.append(f"{path}: change is not limited to imports/whitespace")
    if total > policy.limits.max_changed_lines:
        problems.append(f"{total} changed lines exceeds the limit of {policy.limits.max_changed_lines}")

    # The requested lint codes must actually be gone.
    for path, rules in allowed_files.items():
        res = run(
            [python, "-m", "ruff", "check", "--isolated", "--no-cache", "--select", ",".join(rules),
             "--output-format", "json", "--exit-zero", path],
            cwd=repo,
        )
        remaining = {item["code"] for item in json.loads(res.stdout or "[]")}
        if remaining:
            problems.append(f"{path}: still reports {', '.join(sorted(remaining))} after fixing")
    return problems


_DIFF_PATH = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)


def check_patch(patch: str, policy: Policy, allowed_files: list[str]) -> list[str]:
    """Re-check the patch in the publish job before it is pushed."""
    problems = []
    paths = {p for pair in _DIFF_PATH.findall(patch) for p in pair}
    if not paths:
        problems.append("patch contains no file changes")
    for path in sorted(paths):
        if policy.is_protected(path):
            problems.append(f"{path}: protected path")
        if path not in allowed_files:
            problems.append(f"{path}: not listed as a fixed file in the report")
    if re.search(r"^(new|deleted) file mode|^rename from|^Binary files", patch, re.M):
        problems.append("patch creates, deletes, renames, or changes binary files")
    changed = sum(
        1 for ln in patch.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
    )
    if changed > policy.limits.max_changed_lines:
        problems.append(f"{changed} changed lines exceeds the limit of {policy.limits.max_changed_lines}")
    return problems
