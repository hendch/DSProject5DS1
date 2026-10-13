"""The only module that talks to a model. Bounded tool-calling loop over any
OpenAI-compatible endpoint (hosted Qwen3-Coder now, a local Ollama/vLLM server later).

Configuration comes from the environment so the key never enters the prompt:
  LLM_BASE_URL   e.g. https://openrouter.ai/api/v1  or  http://localhost:11434/v1
  LLM_MODEL      e.g. qwen/qwen3-coder               or  qwen3-coder:30b
  LLM_API_KEY    secret (any non-empty string for local servers)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from .context import ReviewContext
from .policy import PACKAGE_DIR, Policy
from .rules import render
from .tools import ToolBox, untrusted

SKILL = (PACKAGE_DIR / "skill" / "minimal_fix.md").read_text(encoding="utf-8")


@dataclass
class LLMConfig:
    base_url: str
    model: str
    api_key: str
    max_output_tokens: int = 4000

    @classmethod
    def from_env(cls) -> "LLMConfig":
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


def first_message(ctx: ReviewContext, policy: Policy) -> str:
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
    if ctx.diagnosis:
        parts.append(untrusted("diagnosis_notes", ctx.diagnosis))
    if len(ctx.diff) > policy.limits.max_diff_chars:
        parts.append("Note: the diff was truncated. Use get_diff / read_file for the rest.")
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
