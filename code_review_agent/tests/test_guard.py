from code_review_agent.guard import check_patch, check_worktree, same_code_ignoring_imports
from code_review_agent.policy import load_policy

POLICY = load_policy()


def test_import_and_whitespace_changes_are_equivalent():
    assert same_code_ignoring_imports("import os\nimport sys\nx = 1   \n", "import sys\nx = 1\n")


def test_logic_change_is_not_equivalent():
    assert not same_code_ignoring_imports("x = train_test_split(X, test_size=0.2)\n",
                                          "x = train_test_split(X, test_size=0.01)\n")


def test_protected_paths():
    assert POLICY.is_protected("requirements.txt")
    assert POLICY.is_protected("sub/requirements-dev.txt")
    assert POLICY.is_protected(".github/workflows/x.yml")
    assert not POLICY.is_protected("src/train.py")
    assert not POLICY.is_fixable_file("pkg/__init__.py")


def test_worktree_rejects_logic_edit_and_unlisted_file(make_repo):
    repo, _, _ = make_repo({}, {"train.py": "import os\nx = 1\n", "other.py": "y = 1\n"})
    (repo / "train.py").write_text("x = 2\n")
    (repo / "other.py").write_text("y = 2\n")
    problems = check_worktree(repo, POLICY, {"train.py": ["F401"]})
    assert any("not limited to imports" in p for p in problems)
    assert any(p.startswith("other.py") for p in problems)


def test_worktree_rejects_protected_and_new_files(make_repo):
    repo, _, _ = make_repo({}, {"requirements.txt": "numpy==1.0\n"})
    (repo / "requirements.txt").write_text("numpy==2.0\n")
    (repo / "new.py").write_text("")
    problems = check_worktree(repo, POLICY, {"requirements.txt": ["F401"]})
    assert any("protected" in p for p in problems)
    assert any("new files" in p for p in problems)


def test_worktree_accepts_clean_import_removal(make_repo):
    repo, _, _ = make_repo({}, {"train.py": "import os\nx = 1\n"})
    (repo / "train.py").write_text("x = 1\n")
    assert check_worktree(repo, POLICY, {"train.py": ["F401"]}) == []


def test_patch_checks():
    patch = "diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n+++ b/requirements.txt\n-a\n+b\n"
    assert any("protected" in p for p in check_patch(patch, POLICY, ["requirements.txt"]))
    new_file = "diff --git a/x.py b/x.py\nnew file mode 100644\n"
    assert any("creates" in p for p in check_patch(new_file, POLICY, ["x.py"]))
    big = "diff --git a/t.py b/t.py\n" + "-x\n" * 100
    assert any("exceeds" in p for p in check_patch(big, POLICY, ["t.py"]))
    assert any("not listed" in p for p in check_patch("diff --git a/t.py b/t.py\n-x\n", POLICY, []))
