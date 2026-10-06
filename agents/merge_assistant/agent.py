import os
import shutil
import tempfile
import uuid
from pathlib import Path

import ollama
from git import Repo
from git.exc import GitCommandError

from shared.events import publish_and_log

BASE = Path(__file__).parent
DEMO_REPO = BASE / "test-repo"
MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:1b")


def make_demo_repo(path: Path) -> None:
    """Create a repo with two branches that edit the same line."""
    if path.exists():
        shutil.rmtree(path, onerror=lambda f, p, e: (os.chmod(p, 0o700), f(p)))
    path.mkdir(parents=True)
    repo = Repo.init(path)
    with repo.config_writer() as c:
        c.set_value("user", "name", "demo")
        c.set_value("user", "email", "demo@example.com")
    f = path / "config.py"
    f.write_text("TIMEOUT = 30\nRETRIES = 3\n")
    repo.git.add(A=True)
    repo.index.commit("base")
    repo.git.branch("-M", "main")
    for name, line in [("feature-a", "TIMEOUT = 60\n"), ("feature-b", "TIMEOUT = 10\n")]:
        repo.git.checkout("main")
        repo.git.checkout("-b", name)
        f.write_text(line + "RETRIES = 3\n")
        repo.git.add(A=True)
        repo.index.commit(f"{name}: change timeout")
    repo.git.checkout("main")


def detect_conflicts(repo_path: Path, a: str, b: str) -> dict:
    """Try merging b into a inside a temp clone. Returns {file: text with markers}."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        clone = Repo.clone_from(str(repo_path), tmp)
        with clone.config_writer() as c:
            c.set_value("user", "name", "merge-assistant")
            c.set_value("user", "email", "agent@example.com")
        clone.git.checkout(b)
        clone.git.checkout(a)
        try:
            clone.git.merge(b, "--no-commit", "--no-ff")
            return {}
        except GitCommandError:
            files = clone.git.diff("--name-only", "--diff-filter=U").splitlines()
            return {f: (Path(tmp) / f).read_text() for f in files}


def propose_resolution(conflicts: dict) -> str:
    text = "\n\n".join(f"FILE {f}:\n{c}" for f, c in conflicts.items())
    prompt = (
        "You are a merge assistant. Below are files with git conflict markers. "
        "For each file, give a short explanation of the conflict and the "
        "resolved content you suggest. Be brief.\n\n" + text
    )
    resp = ollama.chat(model=MODEL, messages=[{"role": "user", "content": prompt}])
    return resp["message"]["content"].strip()


def emit(correlation_id, branch_a, branch_b, summary, resolution, conflicts):
    publish_and_log(
        agent_name="merge_assistant",
        action_type="flag_conflict",
        payload={
            "branch_a": branch_a,
            "branch_b": branch_b,
            "conflict_summary": summary,
            "proposed_resolution": resolution,
        },
        correlation_id=correlation_id,
        environment="dev",
        blast_radius="low",  # only suggests, never merges
        category=f"flagged conflict in {', '.join(conflicts)}",  # saved in the decision column
        input_summary=f"Merge {branch_b} into {branch_a}: {len(conflicts)} conflicted file(s)",
    )


def run(repo_path: Path, branch_a: str, branch_b: str) -> None:
    conflicts = detect_conflicts(repo_path, branch_a, branch_b)
    if not conflicts:
        print("No conflicts, nothing to flag.")
        return
    resolution = propose_resolution(conflicts)
    summary = f"{len(conflicts)} conflicted file(s): {', '.join(conflicts)}"
    emit(str(uuid.uuid4()), branch_a, branch_b, summary, resolution, conflicts)
    print(summary)
    print(resolution)


if __name__ == "__main__":
    make_demo_repo(DEMO_REPO)
    run(DEMO_REPO, "feature-a", "feature-b")