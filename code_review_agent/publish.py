"""Publish job: comment on the PR and, for validated repairs, push a bot branch and
open (or update) one draft PR. No LLM, no repository code executed.

Needs GH_TOKEN (the job's GITHUB_TOKEN) and GITHUB_REPOSITORY in the environment.
This module never merges or approves anything.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .guard import check_patch
from .policy import load_policy

MARKER = "<!-- code-review-agent -->"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


def sh(*cmd: str, cwd: str | Path = ".", input_text: str | None = None) -> str:
    """Like shell.run, but keeps GH_TOKEN: this job is the one place that needs it."""
    proc = subprocess.run(cmd, cwd=cwd, input=input_text, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])}... failed: {proc.stderr.strip()[:500]}")
    return proc.stdout


def upsert_comment(slug: str, pr: int, body: str) -> None:
    """One sticky comment per PR, found by a hidden marker."""
    body = f"{MARKER}\n{body}"
    comments = json.loads(sh("gh", "api", "--paginate", f"repos/{slug}/issues/{pr}/comments") or "[]")
    mine = [c for c in comments if MARKER in (c.get("body") or "") and c["user"]["login"] == BOT_NAME]
    payload = json.dumps({"body": body})
    if mine:
        sh("gh", "api", "-X", "PATCH", f"repos/{slug}/issues/comments/{mine[-1]['id']}", "--input", "-",
           input_text=payload)
    else:
        sh("gh", "api", "-X", "POST", f"repos/{slug}/issues/{pr}/comments", "--input", "-", input_text=payload)


def push_fix_branch(repo: Path, patch: Path, head_sha: str, branch: str, report: dict) -> None:
    sh("git", "checkout", "--detach", head_sha, cwd=repo)
    sh("git", "apply", "--check", str(patch), cwd=repo)
    sh("git", "apply", str(patch), cwd=repo)
    fixes = "\n".join(f"- {path}: {', '.join(rules)}" for path, rules in report["fixed_files"].items())
    sh("git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "commit", "-a", "-q",
       "-m", f"Apply safe lint fixes (code review agent)\n\n{fixes}", cwd=repo)
    sh("git", "push", "--force", "origin", f"HEAD:refs/heads/{branch}", cwd=repo)


def open_or_update_pr(slug: str, branch: str, base_ref: str, pr: int, body_file: Path) -> str:
    existing = json.loads(
        sh("gh", "pr", "list", "-R", slug, "--head", branch, "--state", "open", "--json", "number,url") or "[]"
    )
    if existing:
        sh("gh", "pr", "edit", str(existing[0]["number"]), "-R", slug, "--body-file", str(body_file))
        return existing[0]["url"]
    return sh(
        "gh", "pr", "create", "-R", slug, "--draft", "--base", base_ref, "--head", branch,
        "--title", f"Safe lint fixes for #{pr} (code review agent)", "--body-file", str(body_file),
    ).strip()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Publish code review agent results")
    p.add_argument("--repo", required=True, help="checkout of the repository (with push credentials)")
    p.add_argument("--out", required=True, help="directory with report.json, report.md, fix.patch")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--head-ref", required=True, help="branch of the reviewed PR (base of the fix PR)")
    p.add_argument("--head-sha", required=True)
    args = p.parse_args(argv)

    slug = os.environ["GITHUB_REPOSITORY"]
    policy = load_policy()
    out = Path(args.out)
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    body = (out / "report.md").read_text(encoding="utf-8")
    patch = out / "fix.patch"

    if report["status"] != "repaired" or not patch.is_file():
        upsert_comment(slug, args.pr, body)
        return 0

    if report["head"] != args.head_sha:
        upsert_comment(slug, args.pr, body + "\n> Fix PR skipped: the report is for a different commit.\n")
        return 0

    problems = check_patch(patch.read_text(encoding="utf-8"), policy, list(report["fixed_files"]))
    if args.head_ref.startswith(policy.branch_prefix):
        problems.append("refusing to fix a bot branch")
    branch = f"{policy.branch_prefix}pr-{args.pr}"
    if not f"refs/heads/{branch}".startswith(policy.push_ref_prefix):
        problems.append(f"push target {branch} is outside {policy.push_ref_prefix}")
    if problems:
        upsert_comment(slug, args.pr, body + "\n> Fix PR blocked by publish checks:\n"
                       + "\n".join(f"> - {x}" for x in problems) + "\n")
        return 1

    push_fix_branch(Path(args.repo), patch, args.head_sha, branch, report)
    pr_body = out / "pr_body.md"
    pr_body.write_text(f"Fixes for #{args.pr}. Draft: review before merging.\n\n{body}", encoding="utf-8")
    url = open_or_update_pr(slug, branch, args.head_ref, args.pr, pr_body)
    upsert_comment(slug, args.pr, body + f"\n**Fix PR:** {url}\n")
    print(f"published {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
