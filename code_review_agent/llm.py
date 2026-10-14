"""The only module that talks to a model. Two backends, chosen with LLM_BACKEND:

claude-cli (default)
  One bounded call to Claude Code in headless mode (`claude -p`) with every tool
  disabled and a JSON schema for the answer. Authenticates with the subscription
  token in CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`).
    LLM_MODEL  default claude-haiku-5-5

openai
  Bounded tool-calling loop over any OpenAI-compatible endpoint (hosted or local).
    LLM_BASE_URL, LLM_MODEL, LLM_API_KEY

Credentials come from the environment and never enter the prompt.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from .context import ReviewContext
from .policy import PACKAGE_DIR, Policy
from .rules import render
from .shell import git
from .tools import REVIEW_SCHEMA, ToolBox, untrusted

SKILL = (PACKAGE_DIR / "skill" / "minimal_fix.md").read_text(encoding="utf-8")
DEFAULT_CLI_MODEL = "claude-haiku-5-5"


@dataclass
class LLMConfig:
    base_url: str
    model: str
    api_key: str
    max_output_tokens: int = 4000

    @classmethod
    def from_env(cls) -> LLMConfig:
        missing = [k for k in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY") if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"missing environment variables: {', '.join(missing)}")
        return cls(os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"], os.environ["LLM_API_KEY"])


@dataclass
class Outcome:
    review: dict | None
    stop_reason: str
    tool_calls: int = 0
    turns: int = 0
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})


def changed_file_contents(ctx: ReviewContext, policy: Policy) -> list[str]:
    """Full text of changed files at the PR head, for the single-call backend (no read_file tool)."""
    budget = policy.limits.max_diff_chars
    blocks = []
    for path in ctx.changed_files:
        if budget <= 0 or policy.is_unreadable(path):
            continue
        try:
            text = git(ctx.repo, "show", f"{ctx.head}:{path}")
        except RuntimeError:
            continue
        numbered = "\n".join(f"{n:>5}  {line}" for n, line in enumerate(text.splitlines(), 1))[:budget]
        budget -= len(numbered)
        blocks.append(untrusted("file", numbered, path=path))
    return blocks


def first_message(ctx: ReviewContext, policy: Policy, single_shot: bool = False) -> str:
    changed = set(ctx.changed_files)
    lint = "\n".join(f"{i.file}:{i.line} {i.code} {i.message}" for i in ctx.lint if i.file in changed)
    tests = "\n".join(f"{f.name}: {f.message}" for f in ctx.test_failures[:30])
    parts = [
        f"Allowed auto-fix rule codes: {', '.join(policy.allowed_ruff_rules)}",
        f"<trusted_rules>\n{render(ctx.rules)}\n</trusted_rules>",
        "Changed files:\n" + ("\n".join(ctx.changed_files) or "(none)"),
        untrusted("diff", ctx.diff[: policy.limits.max_diff_chars]),
        untrusted("lint_report", lint or "(no lint findings in changed files)"),
        untrusted("test_report", f"{ctx.tests_summary}\n{tests}".strip()),
    ]
    if single_shot:
        parts += changed_file_contents(ctx, policy)
    if ctx.diagnosis:
        parts.append(untrusted("diagnosis_notes", ctx.diagnosis))
    if len(ctx.diff) > policy.limits.max_diff_chars:
        parts.append("Note: the diff was truncated.")
    if single_shot:
        parts.append(
            "You have no tools in this mode. Review the change and answer with the JSON schema. "
            "Put allowed lint fixes in `fix_requests` (only codes from the lint report for that file); "
            "everything else goes in `findings`."
        )
    else:
        parts.append("Review the change, request allowed fixes if any, then call submit_review.")
    return "\n\n".join(parts)


def _review_from_text(content: str | None) -> dict | None:
    """Some open models print the final JSON instead of calling the tool."""
    if not content:
        return None
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and "findings" in data else None


def run_review(ctx: ReviewContext, toolbox: ToolBox, policy: Policy, cfg: LLMConfig, client=None) -> Outcome:
    if client is None:
        from openai import OpenAI

        client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key, timeout=180, max_retries=2)

    limits = policy.limits
    messages: list[dict] = [
        {"role": "system", "content": SKILL},
        {"role": "user", "content": first_message(ctx, policy)},
    ]
    out = Outcome(review=None, stop_reason="")
    force_submit = False

    while out.turns < limits.max_llm_turns:
        out.turns += 1
        tool_choice = {"type": "function", "function": {"name": "submit_review"}} if force_submit else "auto"
        resp = client.chat.completions.create(
            model=cfg.model,
            messages=messages,
            tools=toolbox.specs(),
            tool_choice=tool_choice,
            temperature=0,
            max_tokens=cfg.max_output_tokens,
        )
        if resp.usage:
            out.usage["input_tokens"] += resp.usage.prompt_tokens or 0
            out.usage["output_tokens"] += resp.usage.completion_tokens or 0

        msg = resp.choices[0].message
        calls = msg.tool_calls or []
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                **({"tool_calls": [c.model_dump() for c in calls]} if calls else {}),
            }
        )

        if not calls:
            fallback = _review_from_text(msg.content)
            if fallback is not None and toolbox.call("submit_review", json.dumps(fallback)) == "accepted":
                out.stop_reason = "submitted_as_text"
                break
            messages.append({"role": "user", "content": "Call submit_review now with your findings."})
            force_submit = True
            continue

        for call in calls:
            name = call.function.name
            if out.tool_calls >= limits.max_tool_calls and name != "submit_review":
                result = "error: tool budget exhausted. Call submit_review now."
            else:
                out.tool_calls += 1
                result = toolbox.call(name, call.function.arguments)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

        if toolbox.review is not None:
            out.stop_reason = "submitted"
            break
        total = out.usage["input_tokens"] + out.usage["output_tokens"]
        if out.tool_calls >= limits.max_tool_calls or total >= limits.max_llm_tokens:
            force_submit = True

    if toolbox.review is None:
        out.stop_reason = out.stop_reason or "budget_exhausted"
    out.review = toolbox.review
    return out


# ---- claude-cli backend -------------------------------------------------------

CLI_MODE_NOTE = (
    "\n\n## Single-call mode\n\nTools are disabled. Answer once with the JSON schema: "
    "`fix_requests` replaces request_safe_fix, and `summary` + `findings` replace submit_review."
)
_CLI_ENV_KEYS = (
    "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "TEMP", "TMP",
    "LANG", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
)


def cli_schema() -> dict:
    schema = copy.deepcopy(REVIEW_SCHEMA)
    schema.pop("$schema", None)
    schema["required"] = [*schema["required"], "fix_requests"]
    schema["properties"]["fix_requests"] = {
        "type": "array",
        "maxItems": 20,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["file", "rule_code"],
            "properties": {"file": {"type": "string"}, "rule_code": {"type": "string"}},
        },
    }
    return schema


def run_review_cli(ctx: ReviewContext, toolbox: ToolBox, policy: Policy, model: str, runner=subprocess.run) -> Outcome:
    """One `claude -p` call: no tools, no repo settings, JSON schema enforced by Claude Code."""
    claude = shutil.which("claude")
    if claude is None:
        raise RuntimeError("Claude Code CLI ('claude') is not installed or not on PATH")
    cmd = [
        claude, "-p",
        "--model", model,
        "--tools", "",                      # no Bash, Read, Edit, web...: judgment only
        "--strict-mcp-config",              # no MCP servers
        "--setting-sources", "",            # ignore user/project settings and hooks
        "--no-session-persistence",
        "--output-format", "json",
        "--json-schema", json.dumps(cli_schema()),
        "--system-prompt", SKILL + CLI_MODE_NOTE,
    ]
    # Only what the CLI needs; never GitHub tokens. An empty cwd means no CLAUDE.md from the repo.
    env = {k: os.environ[k] for k in _CLI_ENV_KEYS if k in os.environ}
    with tempfile.TemporaryDirectory(prefix="cra-claude-") as cwd:
        proc = runner(
            cmd, input=first_message(ctx, policy, single_shot=True), cwd=cwd, env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=policy.limits.command_timeout_s,
        )

    out = Outcome(review=None, stop_reason="", turns=1)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out.stop_reason = f"cli_error: exit {proc.returncode}: {(proc.stderr or proc.stdout)[:300]}"
        return out

    usage = data.get("usage") or {}
    out.usage["input_tokens"] = sum(
        usage.get(k, 0) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    out.usage["output_tokens"] = usage.get("output_tokens", 0) or 0
    if data.get("total_cost_usd") is not None:
        out.usage["api_equivalent_usd"] = data["total_cost_usd"]

    answer = data.get("structured_output")
    if data.get("is_error") or not isinstance(answer, dict):
        out.stop_reason = f"cli_error: {data.get('subtype')}: {str(data.get('result'))[:300]}"
        return out

    # Same checks as the tool path: every fix request and the review go through the ToolBox.
    for req in answer.get("fix_requests", [])[:20]:
        toolbox.call("request_safe_fix", json.dumps(req))
    result = toolbox.call(
        "submit_review", json.dumps({"summary": answer.get("summary", ""), "findings": answer.get("findings", [])})
    )
    out.tool_calls = len(toolbox.log)
    out.stop_reason = "submitted" if result == "accepted" else f"invalid_review: {result[:200]}"
    out.review = toolbox.review
    return out
