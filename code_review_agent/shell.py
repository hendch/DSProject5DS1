"""Run subprocesses with secrets removed from the environment.

Every command the agent runs on repository content (ruff, pytest, git) goes
through here, so untrusted test code never sees the LLM key or a GitHub token.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SECRET_NAMES = {
    "LLM_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "ACTIONS_RUNTIME_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_URL",
}
_SECRET_SUFFIXES = ("_TOKEN", "_API_KEY", "_SECRET", "_PASSWORD")


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def tail(self, chars: int = 3000) -> str:
        text = (self.stdout + "\n" + self.stderr).strip()
        return text[-chars:]


def clean_env() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if k.upper() not in _SECRET_NAMES and not k.upper().endswith(_SECRET_SUFFIXES)
    }


def run(cmd: list[str], cwd: str | Path, timeout: int = 600) -> Result:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=clean_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return Result(124, "", f"timeout after {timeout}s: {' '.join(cmd)}")
    except FileNotFoundError as exc:
        return Result(127, "", str(exc))
    return Result(proc.returncode, proc.stdout, proc.stderr)


def git(repo: str | Path, *args: str) -> str:
    res = run(["git", *args], cwd=repo, timeout=120)
    if not res.ok:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout
