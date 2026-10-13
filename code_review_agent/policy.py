"""Load policy.yaml into typed objects and match paths against its patterns."""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_POLICY = PACKAGE_DIR / "policy.yaml"


@dataclass(frozen=True)
class Limits:
    max_changed_lines: int = 60
    max_tool_calls: int = 8
    max_llm_turns: int = 10
    max_llm_tokens: int = 60000
    max_read_lines: int = 300
    max_diff_chars: int = 60000
    repair_attempts: int = 1
    command_timeout_s: int = 600


@dataclass(frozen=True)
class Policy:
    allowed_ruff_rules: tuple[str, ...]
    format_files: bool
    skip_files: tuple[str, ...]
    protected_paths: tuple[str, ...]
    unreadable_paths: tuple[str, ...]
    validation_commands: tuple[str, ...]
    push_ref_prefix: str
    branch_prefix: str
    limits: Limits = field(default_factory=Limits)

    def is_protected(self, path: str) -> bool:
        return matches(path, self.protected_paths)

    def is_unreadable(self, path: str) -> bool:
        return matches(path, self.unreadable_paths)

    def is_fixable_file(self, path: str) -> bool:
        return (
            path.endswith(".py")
            and not matches(path, self.skip_files)
            and not self.is_protected(path)
        )


def matches(path: str, patterns: tuple[str, ...]) -> bool:
    path = path.replace("\\", "/")
    if path.startswith("./"):
        path = path[2:]
    for pat in patterns:
        if fnmatchcase(path, pat):
            return True
        # "**/x" should also match "x" at the repo root.
        if pat.startswith("**/") and fnmatchcase(path, pat[3:]):
            return True
    return False


def load_policy(path: str | Path | None = None) -> Policy:
    data = yaml.safe_load(Path(path or DEFAULT_POLICY).read_text(encoding="utf-8"))
    return Policy(
        allowed_ruff_rules=tuple(data["allowed_ruff_rules"]),
        format_files=bool(data.get("format_files", False)),
        skip_files=tuple(data.get("skip_files", [])),
        protected_paths=tuple(data.get("protected_paths", [])),
        unreadable_paths=tuple(data.get("unreadable_paths", [])),
        validation_commands=tuple(data.get("validation_commands", [])),
        push_ref_prefix=data["push_ref_prefix"],
        branch_prefix=data["branch_prefix"],
        limits=Limits(**data.get("limits", {})),
    )
