"""Tools the LLM may call. All read-only, except that request_safe_fix adds to a
queue which code checks again before anything is changed."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from .context import ReviewContext
from .policy import PACKAGE_DIR, Policy
from .rules import get_rules, render
from .shell import git

REVIEW_SCHEMA = json.loads((PACKAGE_DIR / "schemas" / "review.schema.json").read_text(encoding="utf-8"))


@dataclass(frozen=True)
class FixRequest:
    file: str
    rule: str


def untrusted(kind: str, body: str, **attrs: str) -> str:
    """Wrap repository content so the model can tell data from instructions."""
    attr = "".join(f' {k}="{v}"' for k, v in attrs.items())
    body = body.replace("</untrusted", "<\\/untrusted")
    return f"<untrusted_{kind}{attr}>\n{body}\n</untrusted_{kind}>"


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


class ToolBox:
    def __init__(self, ctx: ReviewContext, policy: Policy):
        self.ctx = ctx
        self.policy = policy
        self.fix_requests: list[FixRequest] = []
        self.review: dict | None = None
        self.log: list[dict[str, str]] = []

    # ---- tool definitions shown to the model ---------------------------------
    def specs(self) -> list[dict]:
        return [
            _fn("list_changed_files", "List files changed in this PR.", {}, []),
            _fn("get_diff", "Unified diff for one changed file.", {"path": {"type": "string"}}, ["path"]),
            _fn(
                "read_file",
                f"Read lines of a file at the PR head (max {self.policy.limits.max_read_lines} lines per call).",
                {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                ["path"],
            ),
            _fn("get_lint_findings", "Lint findings, optionally for one file.", {"path": {"type": "string"}}, []),
            _fn("get_test_failures", "Failed tests from the test report.", {}, []),
            _fn("get_rules", "Search the trusted project rules.", {"query": {"type": "string"}}, []),
            _fn(
                "request_safe_fix",
                "Ask for an automatic lint fix. Only allowed rule codes on files where the lint report "
                "shows that code. Everything else must be reported as a finding instead.",
                {"file": {"type": "string"}, "rule_code": {"type": "string"}},
                ["file", "rule_code"],
            ),
            {
                "type": "function",
                "function": {
                    "name": "submit_review",
                    "description": "Submit the final review. Call exactly once, at the end.",
                    "parameters": REVIEW_SCHEMA,
                },
            },
        ]

    # ---- dispatch ------------------------------------------------------------
    def call(self, name: str, raw_args: str | None) -> str:
        try:
            args: dict[str, Any] = json.loads(raw_args or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            return self._record(name, "{}", f"error: invalid JSON arguments ({exc})")
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return self._record(name, raw_args or "", f"error: unknown tool '{name}'")
        try:
            out = handler(**args)
        except TypeError as exc:
            out = f"error: bad arguments ({exc})"
        except RuntimeError as exc:
            out = f"error: {exc}"
        return self._record(name, raw_args or "", out)

    def _record(self, name: str, args: str, out: str) -> str:
        self.log.append({"tool": name, "args": args[:300], "result": out[:200]})
        return out

    # ---- implementations -------------------------------------------------------
    def _check_path(self, path: str) -> str | None:
        path = path.replace("\\", "/").lstrip("/")
        if not path or ".." in Path(path).parts or self.policy.is_unreadable(path):
            return None
        return path

    def _t_list_changed_files(self) -> str:
        return "\n".join(self.ctx.changed_files) or "(none)"

    def _t_get_diff(self, path: str) -> str:
        path = self._check_path(path)
        if path is None or path not in self.ctx.changed_files:
            return "error: not a changed file"
        diff = git(self.ctx.repo, "diff", "--unified=5", self.ctx.base, self.ctx.head, "--", path)
        return untrusted("diff", diff[: self.policy.limits.max_diff_chars], path=path)

    def _t_read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        path = self._check_path(path)
        if path is None:
            return "error: path not allowed"
        try:
            text = git(self.ctx.repo, "show", f"{self.ctx.head}:{path}")
        except RuntimeError:
            return "error: file not found at PR head"
        lines = text.splitlines()
        start = max(1, int(start_line))
        end = min(len(lines), int(end_line or start + self.policy.limits.max_read_lines - 1))
        end = min(end, start + self.policy.limits.max_read_lines - 1)
        body = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(start, end + 1))
        return untrusted("file", body, path=path, lines=f"{start}-{end} of {len(lines)}")

    def _t_get_lint_findings(self, path: str | None = None) -> str:
        issues = self.ctx.lint_for(path) if path else self.ctx.lint
        body = "\n".join(f"{i.file}:{i.line} {i.code} {i.message}" for i in issues[:200]) or "(none)"
        return untrusted("lint_report", body)

    def _t_get_test_failures(self) -> str:
        body = "\n".join(f"{f.name}: {f.message}" for f in self.ctx.test_failures[:50])
        return untrusted("test_report", f"{self.ctx.tests_summary}\n{body}".strip())

    def _t_get_rules(self, query: str = "") -> str:
        return f"<trusted_rules>\n{render(get_rules(self.ctx.rules, query))}\n</trusted_rules>"

    def _t_request_safe_fix(self, file: str, rule_code: str) -> str:
        file = (self._check_path(file) or "")
        rule = rule_code.strip().upper()
        if rule not in self.policy.allowed_ruff_rules:
            return f"rejected: {rule} is not an allowed auto-fix rule; report it as a finding"
        if file not in self.ctx.changed_files or not self.policy.is_fixable_file(file):
            return "rejected: file is not a fixable file changed in this PR"
        if not any(i.code == rule for i in self.ctx.lint_for(file)):
            return f"rejected: the lint report shows no {rule} in {file}"
        req = FixRequest(file, rule)
        if req not in self.fix_requests:
            self.fix_requests.append(req)
        return f"queued: {rule} in {file} (will be applied by ruff and validated)"

    def _t_submit_review(self, **review: Any) -> str:
        try:
            jsonschema.validate(review, REVIEW_SCHEMA)
        except jsonschema.ValidationError as exc:
            return f"error: review does not match the schema: {exc.message}. Fix it and call submit_review again."
        self.review = review
        return "accepted"
