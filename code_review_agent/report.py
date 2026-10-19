"""Build report.json and render the markdown used for PR comments and the fix PR body."""

from __future__ import annotations

import os
import re

STATUS_TEXT = {
    "no_issues": "No findings and no fixes needed.",
    "findings_only": "Findings reported. No automatic fixes were applicable.",
    "repaired": "Safe lint fixes applied and validated. See the draft PR.",
    "guard_rejected": "Fixes were reverted: they broke a policy rule.",
    "fix_failed": "Fixes were reverted: the fixer failed.",
    "validation_failed": "Fixes were reverted: validation failed after fixing.",
    "not_at_head": "Fixes skipped: the checkout is not at the PR head or has local changes.",
    "review_failed": "The LLM review did not complete. Deterministic results only.",
}
FAILED_STATUSES = {"guard_rejected", "fix_failed", "validation_failed"}


def _clean(text: object, limit: int = 600) -> str:
    """Model and repo text go into markdown: keep it short, inert, and mention-free."""
    s = str(text)[:limit]
    s = s.replace("<", "&lt;").replace("@", "@​")
    return re.sub(r"\s*\n\s*", " ", s).replace("|", "\\|").strip()


def _cost(usage: dict) -> float | None:
    try:
        price_in = float(os.environ["LLM_PRICE_IN_PER_MTOK"])
        price_out = float(os.environ["LLM_PRICE_OUT_PER_MTOK"])
    except (KeyError, ValueError):
        return None
    return round(usage["input_tokens"] / 1e6 * price_in + usage["output_tokens"] / 1e6 * price_out, 5)


def build_report(*, status, ctx, outcome, model, fixed_files, fix_log, validation, problems, runtime_s) -> dict:
    review = outcome.review or {"summary": "", "findings": []}
    findings = [{"id": f"F{n}", **f} for n, f in enumerate(review["findings"], 1)]
    return {
        "status": status,
        "base": ctx.base,
        "head": ctx.head,
        "model": model,
        "llm": {
            "stop_reason": outcome.stop_reason,
            "turns": outcome.turns,
            "tool_calls": outcome.tool_calls,
            "usage": outcome.usage,
            "cost_usd": _cost(outcome.usage),
        },
        "summary": review["summary"],
        "findings": findings,
        "fixed_files": fixed_files,
        "fix_log": fix_log,
        "validation": validation,
        "problems": problems,
        "runtime_s": round(runtime_s, 1),
    }


def render_markdown(report: dict) -> str:
    lines = [
        "## Code Review Agent",
        "",
        f"**Status:** `{report['status']}`. {STATUS_TEXT.get(report['status'], '')}",
    ]
    if report["summary"]:
        lines += ["", _clean(report["summary"], 1500)]

    if report["findings"]:
        lines += ["", "### Findings (not changed automatically)", "",
                  "| # | Severity | Category | Location | Evidence | Explanation | Confidence |",
                  "|---|---|---|---|---|---|---|"]
        for f in report["findings"]:
            lines.append(
                f"| {f['id']} | {f['severity']} | {f['category']} | `{_clean(f['file'], 120)}:{f['line']}` "
                f"| `{_clean(f['evidence'], 200)}` | {_clean(f['explanation'])}"
                f"{' Suggested: ' + _clean(f['suggested_action'], 300) if f.get('suggested_action') else ''} "
                f"| {f['confidence']} |"
            )

    if report["fixed_files"]:
        lines += ["", "### Automatic fixes", ""]
        lines += [f"- `{_clean(path, 200)}`: {', '.join(rules)}" for path, rules in report["fixed_files"].items()]

    if report["validation"]:
        lines += ["", "### Validation", ""]
        for r in report["validation"]:
            lines.append(f"- `{r['command']}`: {'passed' if r['passed'] else 'FAILED'} (exit {r['exit_code']})")
        failed = [r for r in report["validation"] if not r["passed"]]
        if failed:
            lines += ["", "<details><summary>Failure output</summary>", "", "```",
                      failed[0]["output"].replace("```", "'''"), "```", "</details>"]

    if report["problems"]:
        lines += ["", "### Why fixes were not kept", ""]
        lines += [f"- {_clean(p, 300)}" for p in report["problems"]]

    llm = report["llm"]
    cost = f", ~${llm['cost_usd']}" if llm["cost_usd"] is not None else ""
    if "api_equivalent_usd" in llm["usage"]:
        cost = f", subscription (API-equivalent ~${llm['usage']['api_equivalent_usd']:.4f})"
    lines += [
        "",
        f"<sub>Model `{report['model']}` · {llm['tool_calls']} tool calls · "
        f"{llm['usage']['input_tokens']}+{llm['usage']['output_tokens']} tokens{cost} · "
        f"{report['runtime_s']}s. A human must review and merge; this agent never merges.</sub>",
    ]
    return "\n".join(lines) + "\n"
