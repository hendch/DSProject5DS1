"""Apply approved lint fixes with ruff. Deterministic: no LLM-written code."""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .policy import Policy
from .shell import run
from .tools import FixRequest


@dataclass
class FixResult:
    ok: bool
    files: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


def group_requests(requests: list[FixRequest], policy: Policy) -> dict[str, list[str]]:
    """Re-check every request against policy (defence in depth) and group by file."""
    grouped: dict[str, list[str]] = defaultdict(list)
    for req in requests:
        if req.rule in policy.allowed_ruff_rules and policy.is_fixable_file(req.file):
            if req.rule not in grouped[req.file]:
                grouped[req.file].append(req.rule)
    return dict(grouped)


def apply_fixes(repo: str | Path, requests: list[FixRequest], policy: Policy, python: str = sys.executable) -> FixResult:
    grouped = group_requests(requests, policy)
    result = FixResult(ok=True, files=sorted(grouped))
    timeout = policy.limits.command_timeout_s
    for file, rules in sorted(grouped.items()):
        # --isolated ignores repo config, so only the selected rules run; no --unsafe-fixes.
        cmd = [python, "-m", "ruff", "check", "--isolated", "--no-cache", "--select", ",".join(rules),
               "--fix", "--exit-zero", file]
        res = run(cmd, cwd=repo, timeout=timeout)
        result.log.append(f"$ ruff check --select {','.join(rules)} --fix {file}\n{res.tail(800)}")
        if not res.ok:
            result.ok = False
            return result
        if policy.format_files:
            res = run([python, "-m", "ruff", "format", "--isolated", "--no-cache", file], cwd=repo, timeout=timeout)
            result.log.append(f"$ ruff format {file}\n{res.tail(800)}")
            if not res.ok:
                result.ok = False
                return result
    return result
