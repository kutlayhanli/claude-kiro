"""`ck worktree report`: the run's outcome built from git, not from agent claims.

Real temporary git repositories: merge commits on the target branch say what merged,
tasks.md on the target says what is Done, and task branches with unlanded commits
say what was attempted and did not land.
"""

import json
from pathlib import Path

import pytest

from conftest import git
from test_worktree_land import ck, commit_in, wt

TASKS = """# Implementation Tasks: Demo

### Task 1: one
**Status:** Not Started
**Verify:** `test -f one.txt`

### Task 2: two
**Status:** Not Started
**Verify:** `test -f two.txt`

### Task 3: three
**Status:** Not Started
**Dependencies:** Task 2
**Verify:** `test -f three.txt`

### Task 4: four
**Status:** Not Started
**Dependencies:** Task 3
**Verify:** `test -f four.txt`

### Task 5: five
**Status:** Not Started
**Verify:** `test -f five.txt`

### Task 6: six
**Status:** Done
**Verify:** `true`
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    (root / ".gitignore").write_text(".ck/worktrees/\n")
    (root / "specs/demo").mkdir(parents=True)
    (root / "specs/demo/tasks.md").write_text(TASKS)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "branch", "integrate/demo")
    return root


def mark_done(path: Path, n: str):
    tasks = path / "specs/demo/tasks.md"
    text = tasks.read_text()
    head = f"### Task {n}:"
    start = text.index(head)
    at = text.index("**Status:** Not Started", start)
    tasks.write_text(text[:at] + "**Status:** Done" + text[at + len("**Status:** Not Started"):])


def finish(root: Path, n: str, name: str):
    commit_in(wt(root, n), name, f"{n}\n", f"task {n}")
    mark_done(wt(root, n), n)
    git(wt(root, n), "commit", "-qam", f"task {n} done")


def report(root: Path, *args: str):
    result = ck(root, "worktree", "report", "demo", *args, "--json")
    try:
        return result, json.loads(result.output)
    except ValueError:
        raise AssertionError(result.output)


def in_one_list(data, n):
    lists = [k for k in ("merged", "done", "failed", "blocked", "unstarted") if n in [e["task"] for e in data[k]]]
    return lists


def test_report_comes_from_merge_commits_and_tasks_md(repo):
    git(repo, "worktree", "add", "-q", str(repo / ".ck/worktrees/demo-integration"), "integrate/demo")
    ck(repo, "worktree", "create", "demo", "1", "2", "5", "--base", "integrate/demo")
    finish(repo, "1", "one.txt")
    finish(repo, "2", "two.txt")
    commit_in(wt(repo, "5"), "five.txt", "5\n", "task 5 attempt")  # attempted, never landed
    landed = ck(repo, "worktree", "land", "demo", "1", "2", "--into", "integrate/demo", "--json")
    assert landed.exit_code == 0, landed.output

    result, data = report(repo, "--into", "integrate/demo")
    assert result.exit_code == 0, result.output
    assert data["into"] == "integrate/demo"
    assert data["head"] == git(repo, "rev-parse", "integrate/demo").strip()
    merged = {e["task"]: e for e in data["merged"]}
    assert sorted(merged) == ["1", "2"]
    assert merged["1"]["commit"] and merged["1"]["mergedAt"] and merged["1"]["done"] is True
    assert merged["1"]["subject"] == "Merge feat/demo-task-1 (task 1)"
    # Done in tasks.md with no merge commit (an earlier run, or a direct commit).
    assert [e["task"] for e in data["done"]] == ["6"]
    failed = {e["task"]: e for e in data["failed"]}
    assert list(failed) == ["5"]
    assert failed["5"]["branch"] == "feat/demo-task-5" and failed["5"]["ahead"] == 1
    # 3's dependency (2) is Done, so it is ready but was never started; 4 waits on 3.
    assert [e["task"] for e in data["unstarted"]] == ["3"]
    assert data["blocked"] == [{"task": "4", "on": ["3"]}]
    for n in ("1", "2", "3", "4", "5", "6"):
        assert len(in_one_list(data, n)) == 1, (n, in_one_list(data, n))


def test_merged_wins_over_a_stale_branch_and_never_shows_as_failed(repo):
    # Bench v2: Task 17 was reported both merged and "merge failed: no branch".
    git(repo, "worktree", "add", "-q", str(repo / ".ck/worktrees/demo-integration"), "integrate/demo")
    ck(repo, "worktree", "create", "demo", "1", "--base", "integrate/demo")
    finish(repo, "1", "one.txt")
    assert ck(repo, "worktree", "land", "demo", "1", "--into", "integrate/demo", "--keep", "--json").exit_code == 0
    commit_in(wt(repo, "1"), "late.txt", "x\n", "a commit after landing")  # branch is ahead again
    _, data = report(repo, "--into", "integrate/demo")
    assert in_one_list(data, "1") == ["merged"]


def test_merges_inside_task_branches_do_not_count(repo):
    # Task agents merge the target into their own branch; only merges made ON the target count.
    git(repo, "worktree", "add", "-q", str(repo / ".ck/worktrees/demo-integration"), "integrate/demo")
    ck(repo, "worktree", "create", "demo", "1", "5", "--base", "integrate/demo")
    commit_in(wt(repo, "5"), "five.txt", "5\n", "task 5")
    commit_in(repo / ".ck/worktrees/demo-integration", "other.txt", "o\n", "unrelated work on the target")
    git(wt(repo, "5"), "merge", "-q", "--no-ff", "-m", "Merge integrate/demo into feat/demo-task-5", "integrate/demo")
    finish(repo, "1", "one.txt")
    git(repo / ".ck/worktrees/demo-integration", "merge", "-q", "--no-ff", "feat/demo-task-1")  # git's default message
    _, data = report(repo, "--into", "integrate/demo")
    assert [e["task"] for e in data["merged"]] == ["1"]
    assert "Merge branch 'feat/demo-task-1'" in data["merged"][0]["subject"]
    assert [e["task"] for e in data["failed"]] == ["5"]


def test_text_output_and_unknown_branch(repo):
    result = ck(repo, "worktree", "report", "demo", "--into", "integrate/demo")
    assert result.exit_code == 0, result.output
    assert "merged 0" in result.output and "blocked" in result.output
    bad = ck(repo, "worktree", "report", "demo", "--into", "nope", "--json")
    assert bad.exit_code == 3
    assert "error" in json.loads(bad.output)
