"""`ck worktree land`: batch landing with a merge-tree precheck, one gate per batch, and bisection.

Real temporary git repositories; the gate runs each task's **Verify:** line in the target checkout.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_kiro.cli.main import cli

from conftest import git

TASKS = """# Implementation Tasks: Demo

### Task 1: one
**Status:** Not Started
**Verify:** `test -f one.txt`

### Task 2: two
**Status:** Not Started
**Verify:** `test ! -f bad.txt`

### Task 3: three
**Status:** Not Started
**Verify:** `test -f three.txt`

### Task 4: four
**Status:** Not Started
**Dependencies:** Task 3
**Verify:** `test -f four.txt`
"""


def ck(cwd: Path, *args: str):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(cli, list(args))
    finally:
        os.chdir(old)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    (root / ".gitignore").write_text(".venv/\n.ck/worktrees/\n")
    (root / "shared.txt").write_text("base\n")
    (root / "specs/demo").mkdir(parents=True)
    (root / "specs/demo/tasks.md").write_text(TASKS)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def wt(root: Path, n: str) -> Path:
    return root / ".ck/worktrees" / f"demo-task-{n}"


def commit_in(path: Path, name: str, content: str, msg: str):
    (path / name).write_text(content)
    git(path, "add", "-A")
    git(path, "commit", "-qm", msg)


def land(root: Path, *args: str):
    result = ck(root, "worktree", "land", "demo", *args, "--json")
    try:
        data = json.loads(result.output)
    except ValueError:
        raise AssertionError(result.output)
    return result, data, {t["task"]: t for t in data["tasks"]}


def head(path: Path) -> str:
    return git(path, "rev-parse", "HEAD").strip()


def test_clean_batch_lands_with_one_gate(repo):
    ck(repo, "worktree", "create", "demo", "1", "3")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    commit_in(wt(repo, "3"), "three.txt", "3\n", "task 3")

    result, data, by = land(repo, "1", "3", "--gate")
    assert result.exit_code == 0, result.output
    assert [by[n]["state"] for n in ("1", "3")] == ["MERGED", "MERGED"]
    assert all(by[n]["checkGreen"] is True and by[n]["mergedAt"] for n in ("1", "3"))
    assert [sorted(g["tasks"]) for g in data["gates"]] == [["1", "3"]]  # one gate for the batch
    assert data["bisected"] is False
    assert (repo / "one.txt").exists() and (repo / "three.txt").exists()
    subjects = git(repo, "log", "--format=%s", "-2").splitlines()
    assert sorted(subjects) == ["Merge feat/demo-task-1 (task 1)", "Merge feat/demo-task-3 (task 3)"]
    assert not wt(repo, "1").exists() and "feat/demo-task-3" not in git(repo, "branch")


def test_conflict_precheck_leaves_target_untouched(repo):
    ck(repo, "worktree", "create", "demo", "1", "2")
    commit_in(wt(repo, "1"), "shared.txt", "from task 1\n", "task 1")
    commit_in(wt(repo, "2"), "shared.txt", "from task 2\n", "task 2")
    assert ck(repo, "worktree", "merge", "demo", "1").exit_code == 0
    before = head(repo)

    result, data, by = land(repo, "2", "--gate")
    assert result.exit_code == 2, result.output
    assert by["2"]["state"] == "CONFLICT"
    assert by["2"]["files"] == ["shared.txt"]
    assert head(repo) == before
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert data["gates"] == []  # nothing landed, nothing gated
    assert wt(repo, "2").exists()  # the task worktree stays for the resolver


def test_tasks_that_conflict_with_each_other_split_across_batches(repo):
    ck(repo, "worktree", "create", "demo", "1", "2", "3")
    (wt(repo, "1") / "one.txt").write_text("1\n")
    commit_in(wt(repo, "1"), "shared.txt", "from task 1\n", "task 1")
    commit_in(wt(repo, "2"), "shared.txt", "from task 2\n", "task 2")
    commit_in(wt(repo, "3"), "three.txt", "3\n", "task 3")

    result, data, by = land(repo, "1", "2", "3", "--gate")
    assert result.exit_code == 0, result.output
    assert by["1"]["state"] == "MERGED" and by["3"]["state"] == "MERGED"
    assert by["2"]["state"] == "DEFERRED"
    assert by["2"]["files"] == ["shared.txt"] and "Task 1" in by["2"]["detail"]
    assert (repo / "shared.txt").read_text() == "from task 1\n"
    assert wt(repo, "2").exists()


def test_critical_path_lands_first(repo):
    # Task 3 has a dependent (Task 4), so it is on the longer chain and lands before 1.
    ck(repo, "worktree", "create", "demo", "1", "3")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    commit_in(wt(repo, "3"), "three.txt", "3\n", "task 3")
    result, data, by = land(repo, "1", "3", "--gate")
    assert result.exit_code == 0, result.output
    assert [t["task"] for t in data["tasks"]] == ["3", "1"]
    first_parents = git(repo, "log", "--first-parent", "--format=%s", "-2").splitlines()
    assert first_parents == ["Merge feat/demo-task-1 (task 1)", "Merge feat/demo-task-3 (task 3)"]


def test_red_batch_bisects_to_the_culprit(repo):
    ck(repo, "worktree", "create", "demo", "1", "2", "3")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    commit_in(wt(repo, "2"), "bad.txt", "breaks task 2's check\n", "task 2")
    commit_in(wt(repo, "3"), "three.txt", "3\n", "task 3")
    before = head(repo)

    result, data, by = land(repo, "1", "2", "3", "--gate")
    assert result.exit_code == 1, result.output
    assert by["1"]["state"] == "MERGED" and by["3"]["state"] == "MERGED"
    assert by["1"]["checkGreen"] is True and by["3"]["checkGreen"] is True
    assert by["2"]["state"] == "RED" and by["2"]["checkGreen"] is False
    assert "bad.txt" in by["2"]["checkTail"]
    assert data["bisected"] is True
    assert sorted(data["gates"][0]["tasks"]) == ["1", "2", "3"] and data["gates"][0]["green"] is False
    assert len(data["gates"]) < 5  # a green left half proves the right half red without re-gating it
    # The target holds the green tasks only; the culprit's branch and worktree stay for the fixer.
    assert (repo / "one.txt").exists() and (repo / "three.txt").exists()
    assert not (repo / "bad.txt").exists()
    assert git(repo, "merge-base", "--is-ancestor", before, "HEAD") == ""
    assert "feat/demo-task-2" in git(repo, "branch") and wt(repo, "2").exists()
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""


def test_single_task_equals_merge_then_task_gate(repo):
    ck(repo, "worktree", "create", "demo", "1", "2")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    result, data, by = land(repo, "1", "--gate")
    assert result.exit_code == 0, result.output
    assert by["1"]["state"] == "MERGED" and by["1"]["checkGreen"] is True
    assert data["gates"] == [{**data["gates"][0], "tasks": ["1"], "green": True}]
    assert "Merge feat/demo-task-1 (task 1)" in git(repo, "log", "-1", "--format=%s")
    assert ck(repo, "gate", "demo", "--task", "1").exit_code == 0

    commit_in(wt(repo, "2"), "bad.txt", "x\n", "task 2")
    before = head(repo)
    result, data, by = land(repo, "2", "--gate")
    assert result.exit_code == 1
    assert by["2"]["state"] == "RED" and "bad.txt" in by["2"]["checkTail"]
    assert data["bisected"] is False and len(data["gates"]) == 1
    assert head(repo) == before  # a red task never stays on the target


def test_without_gate_it_merges_like_merge(repo):
    ck(repo, "worktree", "create", "demo", "1", "2")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    result, data, by = land(repo, "1", "2")
    assert result.exit_code == 0, result.output
    assert by["1"]["state"] == "MERGED" and by["1"]["checkGreen"] is None
    assert by["2"]["state"] == "SKIP"
    assert data["gates"] == []


def test_claimed_task_is_busy_unless_released(repo):
    ck(repo, "worktree", "create", "demo", "1")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-a")
    result, _, by = land(repo, "1", "--gate")
    assert result.exit_code == 3 and by["1"]["state"] == "BUSY"
    result, _, by = land(repo, "1", "--gate", "--release")
    assert result.exit_code == 0, result.output
    assert by["1"]["state"] == "MERGED"


def test_refuses_dirty_target_and_concurrent_landing(repo):
    ck(repo, "worktree", "create", "demo", "1")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    (repo / "shared.txt").write_text("local edit\n")
    dirty = ck(repo, "worktree", "land", "demo", "1", "--gate")
    assert dirty.exit_code == 3 and "uncommitted" in dirty.output
    git(repo, "checkout", "--", "shared.txt")

    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    lock = common / "ck-land-main.lock"
    # Another process holds the lock (flock): refused, whatever PID the file records.
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOCK, str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        busy = ck(repo, "worktree", "land", "demo", "1", "--gate")
        assert busy.exit_code == 3 and "already landing" in busy.output
    finally:
        holder.communicate("")  # the holder exits; the kernel drops its lock
    # A lock file left by a process that is gone does not block, even when the PID it
    # records is alive now (PIDs are per sandbox namespace and get recycled).
    lock.write_text(str(os.getpid()))
    assert ck(repo, "worktree", "land", "demo", "1", "--gate").exit_code == 0


HOLD_LOCK = """
import fcntl, sys
f = open(sys.argv[1], "a+")
fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
f.write("2")
f.flush()
print("held", flush=True)
sys.stdin.read()
"""


def test_land_into_integration_branch(repo):
    ck(repo, "worktree", "integration", "demo")
    ck(repo, "worktree", "create", "demo", "1", "--base", "integrate/demo")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    result, _, by = land(repo, "1", "--into", "integrate/demo", "--gate")
    assert result.exit_code == 0, result.output
    assert by["1"]["state"] == "MERGED"
    assert not (repo / "one.txt").exists()
    assert (repo / ".ck/worktrees/demo-integration/one.txt").exists()


# --- protected paths: files Claude Code, git or the shell run on their own ---------


@pytest.mark.parametrize("protected", [".claude/settings.json", ".claude/hooks/x.sh", ".mcp.json", ".githooks/pre-commit",
                                       ".husky/pre-push", "specs/ck.json", "sub/.claude/agents/a.md"])
def test_a_branch_touching_a_protected_path_is_never_landed(repo, protected):
    ck(repo, "worktree", "create", "demo", "1", "3")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    target = wt(repo, "3") / protected
    target.parent.mkdir(parents=True, exist_ok=True)
    commit_in(wt(repo, "3"), protected, "{}\n", "task 3 plants a file")
    before = head(repo)

    result, data, by = land(repo, "1", "3", "--gate")
    assert result.exit_code == 3, result.output
    assert by["3"]["state"] == "PROTECTED"
    assert by["3"]["files"] == [protected]
    assert "human" in by["3"]["detail"] and "allow_paths" in by["3"]["detail"]
    assert by["1"]["state"] == "MERGED"  # the rest of the batch still lands
    assert git(repo, "rev-list", "--count", f"{before}..HEAD").strip() == "2"  # task 1's commit and its merge
    assert not (repo / protected).exists()
    assert wt(repo, "3").exists() and "feat/demo-task-3" in git(repo, "branch")  # kept for the human


def test_land_allow_paths_in_the_target_lets_a_spec_edit_protected_files(repo):
    (repo / "specs/ck.json").write_text(json.dumps({"land": {"allow_paths": [".claude/commands/**"]}}))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "opt in")
    ck(repo, "worktree", "create", "demo", "1", "3")
    (wt(repo, "1") / ".claude/commands").mkdir(parents=True)
    commit_in(wt(repo, "1"), ".claude/commands/x.md", "x\n", "task 1 edits a command")
    (wt(repo, "3") / ".claude").mkdir()
    commit_in(wt(repo, "3"), ".claude/settings.json", "{}\n", "task 3 edits settings")

    result, data, by = land(repo, "1", "3")
    assert by["1"]["state"] == "MERGED", result.output
    assert by["3"]["state"] == "PROTECTED" and by["3"]["files"] == [".claude/settings.json"]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_allowed_protected_paths_that_are_read_only_here_fail_fast_with_the_remedy(repo):
    # Claude Code's Bash sandbox keeps the main checkout's .claude/ read-only even when
    # land.allow_paths lets a spec edit it; git then dies mid-merge with "unable to unlink old".
    (repo / "specs/ck.json").write_text(json.dumps({"land": {"allow_paths": [".claude/commands/**"]}}))
    (repo / ".claude/commands").mkdir(parents=True)
    (repo / ".claude/commands/x.md").write_text("old\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "opt in")
    ck(repo, "worktree", "create", "demo", "1", "3")
    commit_in(wt(repo, "1"), ".claude/commands/x.md", "new\n", "task 1 edits a command")
    commit_in(wt(repo, "3"), "three.txt", "3\n", "task 3")
    before = head(repo)
    ro = repo / ".claude/commands"
    ro.chmod(0o555)  # what the sandbox's read-only bind looks like to access(2)
    try:
        result, data, by = land(repo, "1", "3")
    finally:
        ro.chmod(0o755)
    assert result.exit_code == 3, result.output
    assert by["1"]["state"] == "PROTECTED"
    assert by["1"]["files"] == [".claude/commands/x.md"]
    detail = by["1"]["detail"]
    assert "read-only" in detail and "sandbox" in detail and "attended" in detail
    assert "ck worktree land demo 1" in detail
    assert by["3"]["state"] == "MERGED"  # the rest of the batch still lands
    assert (repo / ".claude/commands/x.md").read_text() == "old\n"
    assert git(repo, "rev-list", "--count", f"{before}..HEAD").strip() == "2"
    assert wt(repo, "1").exists()


def test_a_branch_cannot_opt_itself_in(repo):
    """allow_paths is read from the target checkout, so a task branch editing specs/ck.json gains nothing."""
    ck(repo, "worktree", "create", "demo", "1")
    (wt(repo, "1") / ".claude").mkdir()
    (wt(repo, "1") / "specs/ck.json").write_text(json.dumps({"land": {"allow_paths": ["**"]}}))
    commit_in(wt(repo, "1"), ".claude/settings.json", "{}\n", "task 1")
    result, data, by = land(repo, "1")
    assert by["1"]["state"] == "PROTECTED"
    assert sorted(by["1"]["files"]) == [".claude/settings.json", "specs/ck.json"]


@pytest.mark.parametrize("command", ["land", "merge"])
def test_a_failed_cleanup_after_the_merge_keeps_the_task_merged(repo, monkeypatch, command):
    # A sandboxed run saw `git worktree remove` fail with "Device or resource busy" after
    # the merge; reporting the task as failed then blocked its dependents for good.
    from claude_kiro import worktrees as wtmod

    real = wtmod.git

    def flaky(cwd, *args, check=True):
        if args[:2] == ("worktree", "remove"):
            proc = subprocess.CompletedProcess(["git", *args], 255, "",
                                               "error: failed to delete '.ck/worktrees/demo-task-1': Device or resource busy")
            if check:
                raise wtmod.WorktreeError(proc.stderr)
            return proc
        return real(cwd, *args, check=check)

    monkeypatch.setattr(wtmod, "git", flaky)
    ck(repo, "worktree", "create", "demo", "1")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")

    if command == "land":
        result, _, by = land(repo, "1")
        state, detail = by["1"]["state"], by["1"]["detail"]
    else:
        result = ck(repo, "worktree", "merge", "demo", "1")
        state, detail = ("MERGED" if "MERGED" in result.output else result.output), result.output
    assert result.exit_code == 0, result.output
    assert state == "MERGED"
    assert "Device or resource busy" in detail and "the merge stands" in detail
    assert (repo / "one.txt").exists()
