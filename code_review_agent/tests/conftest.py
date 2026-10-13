import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def make_repo(tmp_path):
    """make_repo(base_files, head_files) -> (repo, base_sha, head_sha); repo is left at head."""

    def factory(base: dict[str, str], head: dict[str, str]):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.name", "t")
        _git(repo, "config", "user.email", "t@example.invalid")
        _git(repo, "config", "core.autocrlf", "false")
        for files in (base, head):
            for name, text in files.items():
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(text.encode())
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "--allow-empty", "-m", "c")
            if files is base:
                base_sha = _git(repo, "rev-parse", "HEAD")
        return repo, base_sha, _git(repo, "rev-parse", "HEAD")

    return factory
