"""Run the predefined checks from policy.yaml after fixes are applied."""

from __future__ import annotations

import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .policy import Policy
from .shell import run

PYTEST_NO_TESTS = 5


@dataclass
class Validation:
    ok: bool
    results: list[dict] = field(default_factory=list)


def validate(repo: str | Path, policy: Policy, python: str = sys.executable) -> Validation:
    out = Validation(ok=True)
    for template in policy.validation_commands:
        cmd = [python if part == "{python}" else part for part in shlex.split(template)]
        res = run(cmd, cwd=repo, timeout=policy.limits.command_timeout_s)
        passed = res.ok or ("pytest" in cmd and res.code == PYTEST_NO_TESTS)
        out.results.append(
            {"command": template.replace("{python} -m ", ""), "exit_code": res.code, "passed": passed,
             "output": res.tail(1500)}
        )
        if not passed:
            out.ok = False
            break
    return out
