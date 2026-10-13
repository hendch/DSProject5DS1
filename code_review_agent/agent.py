"""Entry point: context -> LLM review -> fixer -> guard -> validate -> report.

This process never pushes, never holds a GitHub write token, and leaves the
working tree as it found it. Fixes leave as `fix.patch` for the publish job.

    python -m code_review_agent.agent --repo target --base <sha> --head <sha> \
        --lint reports/lint.json --junit reports/junit.xml --out agent-out
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .context import build_context
from .fixer import apply_fixes, group_requests
from .guard import check_worktree, untracked_files
from .llm import LLMConfig, Outcome, run_review
from .policy import load_policy
from .report import FAILED_STATUSES, build_report, render_markdown
from .shell import git
from .tools import ToolBox
from .validate import validate


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Code Review and Minimal Fix Agent")
    p.add_argument("--repo", default=".")
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--lint", help="ruff JSON report")
    p.add_argument("--junit", help="pytest JUnit XML report")
    p.add_argument("--diagnosis", help="optional Diagnosis Agent notes (treated as untrusted)")
    p.add_argument("--policy", help="override policy.yaml (tests only)")
    p.add_argument("--out", default="agent-out")
    p.add_argument("--review-json", help="skip the LLM and use this scripted review (tests and offline demos)")
    return p.parse_args(argv)


def scripted_review(toolbox: ToolBox, path: str) -> Outcome:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for req in data.get("fix_requests", []):
        toolbox.call("request_safe_fix", json.dumps(req))
    toolbox.call("submit_review", json.dumps({"summary": data.get("summary", ""), "findings": data.get("findings", [])}))
    return Outcome(review=toolbox.review, stop_reason="scripted")


def at_clean_head(repo: Path, head: str) -> bool:
    current = git(repo, "rev-parse", "HEAD").strip()
    wanted = git(repo, "rev-parse", head).strip()
    dirty = git(repo, "status", "--porcelain", "--untracked-files=no").strip()
    return current == wanted and not dirty


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    policy = load_policy(args.policy)
    ctx = build_context(repo, args.base, args.head, args.lint, args.junit, args.diagnosis)
    toolbox = ToolBox(ctx, policy)

    # 1. Judgment (LLM). Failures here never block the deterministic steps.
    model = "scripted"
    if args.review_json:
        outcome = scripted_review(toolbox, args.review_json)
    else:
        try:
            cfg = LLMConfig.from_env()
            model = cfg.model
            outcome = run_review(ctx, toolbox, policy, cfg)
        except Exception as exc:  # network, auth, provider errors
            outcome = Outcome(review=None, stop_reason=f"llm_error: {type(exc).__name__}: {str(exc)[:300]}")

    # 2. Deterministic repair, one attempt.
    allowed = group_requests(toolbox.fix_requests, policy)
    findings = (outcome.review or {}).get("findings", [])
    status = "findings_only" if findings else "no_issues"
    if outcome.review is None:
        status = "review_failed"
    fix_log: list[str] = []
    validation: list[dict] = []
    problems: list[str] = []

    if allowed and not at_clean_head(repo, args.head):
        status = "not_at_head"
    elif allowed:
        before = untracked_files(repo)
        fix = apply_fixes(repo, toolbox.fix_requests, policy)
        fix_log = fix.log
        if not fix.ok:
            status = "fix_failed"
        elif not git(repo, "diff", "--name-only", "HEAD").strip():
            fix_log.append("ruff made no changes")
        else:
            problems = check_worktree(repo, policy, allowed, before)
            if problems:
                status = "guard_rejected"
            else:
                result = validate(repo, policy)
                validation = result.results
                if result.ok:
                    status = "repaired"
                    (out_dir / "fix.patch").write_text(git(repo, "diff", "HEAD"), encoding="utf-8")
                else:
                    status = "validation_failed"
        # Leave the checkout exactly at the PR head; the publish job applies fix.patch.
        git(repo, "checkout", "--", ".")

    report = build_report(
        status=status,
        ctx=ctx,
        outcome=outcome,
        model=model,
        fixed_files=allowed if status == "repaired" else {},
        fix_log=fix_log,
        validation=validation,
        problems=problems,
        runtime_s=time.monotonic() - started,
    )
    report["tool_log"] = toolbox.log
    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    print(f"status={status} findings={len(report['findings'])} out={out_dir}")

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(render_markdown(report))

    if status in FAILED_STATUSES:
        return 1
    return 2 if status == "review_failed" else 0


if __name__ == "__main__":
    sys.exit(main())
