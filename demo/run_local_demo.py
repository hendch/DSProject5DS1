"""Run the agent end to end on a throwaway git repo, without GitHub.

    python demo/run_local_demo.py --scenario leaky_pr          # offline, scripted review
    python demo/run_local_demo.py --scenario leaky_pr --llm    # real model via LLM_* env vars
    python demo/run_local_demo.py --scenario side_effect_pr    # fix breaks a test -> reverted

It copies demo/base into a temp repo (commit "base"), overlays the scenario files
(commit "seeded PR"), runs baseline ruff + pytest, then runs the agent.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEMO = Path(__file__).resolve().parent
ROOT = DEMO.parent
sys.path.insert(0, str(ROOT))

from code_review_agent.agent import main as agent_main  # noqa: E402


def sh(*cmd: str, cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(tmp: Path, scenario: str) -> tuple[Path, str, str]:
    repo = tmp / "repo"
    shutil.copytree(DEMO / "base", repo)
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    sh("git", "config", "user.name", "demo", cwd=repo)
    sh("git", "config", "user.email", "demo@example.invalid", cwd=repo)
    sh("git", "config", "core.autocrlf", "false", cwd=repo)
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n.ruff_cache/\n")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", "base", cwd=repo)
    base = sh("git", "rev-parse", "HEAD", cwd=repo)
    sh("git", "switch", "-q", "-c", "feature", cwd=repo)
    for src in (DEMO / "scenarios" / scenario).iterdir():
        shutil.copy2(src, repo / src.name)
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", f"seeded PR: {scenario}", cwd=repo)
    return repo, base, sh("git", "rev-parse", "HEAD", cwd=repo)


def baseline(repo: Path, reports: Path) -> None:
    reports.mkdir(parents=True, exist_ok=True)
    lint = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "--extend-select", "I001,W291,W292,W293",
         "--output-format", "json", "--exit-zero", "."],
        cwd=repo, capture_output=True, text=True,
    )
    (reports / "lint.json").write_text(lint.stdout, encoding="utf-8")
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml", str(reports / "junit.xml")],
        cwd=repo, capture_output=True, text=True,
    )
    print(f"baseline: ruff {len(lint.stdout) > 2 and 'found issues' or 'clean'}, "
          f"pytest exit {tests.returncode}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", default="leaky_pr", choices=[d.name for d in (DEMO / "scenarios").iterdir()])
    p.add_argument("--llm", action="store_true", help="use the real model from LLM_* env vars")
    args = p.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="cra-demo-"))
    repo, base, head = make_repo(tmp, args.scenario)
    baseline(repo, tmp / "reports")

    argv = ["--repo", str(repo), "--base", base, "--head", head,
            "--lint", str(tmp / "reports" / "lint.json"), "--junit", str(tmp / "reports" / "junit.xml"),
            "--out", str(tmp / "out")]
    if not args.llm:
        argv += ["--review-json", str(DEMO / "scripted" / f"{args.scenario}.json")]
    code = agent_main(argv)

    print("\n" + (tmp / "out" / "report.md").read_text(encoding="utf-8"))
    patch = tmp / "out" / "fix.patch"
    if patch.is_file():
        print("---- fix.patch ----\n" + patch.read_text(encoding="utf-8"))
    print(f"(demo files in {tmp}; agent exit code {code})")
    return code


if __name__ == "__main__":
    sys.exit(main())
