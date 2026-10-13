"""`ck worktree create|claim|release|merge|integration|status` against a real git repository."""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_kiro.cli.main import cli

from conftest import git


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
    (root / "specs/demo/tasks.md").write_text("### Task 1: a\n**Status:** Not Started\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def wt(root: Path, n: str) -> Path:
    return root / ".ck/worktrees" / f"demo-task-{n}"


def commit_in(path: Path, name: str, content: str, msg: str):
    (path / name).write_text(content)
    git(path, "add", "-A")
    git(path, "commit", "-qm", msg)


def test_create_is_idempotent_and_reports_busy(repo):
    first = ck(repo, "worktree", "create", "demo", "1", "2")
    assert first.exit_code == 0, first.output
    assert "CREATED 1 .ck/worktrees/demo-task-1 (feat/demo-task-1) - from main" in first.output
    assert git(wt(repo, "2"), "branch", "--show-current").strip() == "feat/demo-task-2"

    again = ck(repo, "worktree", "create", "demo", "1")
    assert "EXISTS 1" in again.output and again.exit_code == 0

    (wt(repo, "1") / "wip.txt").write_text("uncommitted")
    busy = ck(repo, "worktree", "create", "demo", "1")
    assert busy.exit_code == 3
    assert "BUSY 1" in busy.output and "uncommitted" in busy.output
    assert ck(repo, "worktree", "create", "demo", "1", "--reuse-dirty").exit_code == 0


def test_create_works_from_inside_a_worktree(repo):
    ck(repo, "worktree", "create", "demo", "1")
    result = ck(wt(repo, "1"), "worktree", "create", "demo", "2", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["state"] == "CREATED"
    assert wt(repo, "2").is_dir()  # placed under the main checkout, not nested


def test_claim_blocks_second_owner_and_setup(repo):
    ck(repo, "worktree", "create", "demo", "1")
    assert "CLAIMED 1" in ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-a").output
    second = ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-b")
    assert second.exit_code == 3 and "BUSY 1" in second.output and "agent-a" in second.output
    setup = ck(repo, "worktree", "create", "demo", "1")
    assert setup.exit_code == 3 and "claimed: agent-a" in setup.output
    takeover = ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-c", "--takeover")
    assert takeover.exit_code == 0 and "took over" in takeover.output
    assert "RELEASED 1" in ck(repo, "worktree", "release", "demo", "1").output
    assert ck(repo, "worktree", "create", "demo", "1").exit_code == 0


# --- Claim ownership: age, never process IDs ----------------------------------------
# Under Claude Code's Bash sandbox every command runs in its own PID namespace, so a PID
# recorded by one command means nothing to the next (bench v2 recorded "pid 2" for every
# claim). A claim is held while it shows activity: its lock, a heartbeat, or a commit.

HOURS = 3600


def lock_file(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--path-format=absolute", "--git-dir").strip()) / "locked"


def age_claim(path: Path, seconds: int):
    """Make the claim and the branch tip look `seconds` old, as if the agent stopped then."""
    import time

    old = time.time() - seconds
    os.utime(lock_file(path), (old, old))
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(old))
    env = dict(os.environ, GIT_COMMITTER_DATE=stamp, GIT_AUTHOR_DATE=stamp)
    import subprocess

    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "task 1: older work"], cwd=path, env=env, check=True)
    os.utime(lock_file(path), (old, old))


def legacy_claim(path: Path, pid: int, seconds_ago: int = 0):
    """A claim as ck 0.9 wrote it: a PID and a local time in the lock reason."""
    import time

    when = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - seconds_ago))
    git(path, "worktree", "lock", "--reason", f"ck task agent on host pid {pid} at {when}", str(path))


@pytest.mark.parametrize("pid", [2, os.getpid()])
def test_a_live_but_foreign_pid_does_not_keep_a_stale_claim(repo, pid):
    """Bench v2: Tasks 2 and 18 sat "claimed by a possibly-dead agent" (pid 2) with finished,
    committed work. A recorded PID that happens to be alive proves nothing; the claim's age decides."""
    ck(repo, "worktree", "create", "demo", "1")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1: finished work")
    legacy_claim(wt(repo, "1"), pid, seconds_ago=5 * HOURS)
    age_claim(wt(repo, "1"), 5 * HOURS)
    head = git(wt(repo, "1"), "rev-parse", "HEAD").strip()

    setup = ck(repo, "worktree", "create", "demo", "1")
    assert setup.exit_code == 0, setup.output
    assert "EXISTS 1" in setup.output and "stale claim" in setup.output
    claimed = ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-b")
    assert claimed.exit_code == 0 and "CLAIMED 1" in claimed.output, claimed.output
    # the finished work is still there for the new agent (and the landing) to use
    assert git(wt(repo, "1"), "rev-parse", "HEAD").strip() == head
    assert (wt(repo, "1") / "one.txt").read_text() == "1\n"


def test_a_recent_claim_is_busy_whatever_pid_it_records(repo):
    """A recycled or namespaced PID (pid 2 is a kernel thread on the host) is not read as dead either."""
    ck(repo, "worktree", "create", "demo", "1")
    legacy_claim(wt(repo, "1"), 999999999)  # no such process here, but the claim is fresh
    busy = ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-b")
    assert busy.exit_code == 3 and "BUSY 1" in busy.output
    assert ck(repo, "worktree", "create", "demo", "1").exit_code == 3


def test_claim_records_a_token_and_time_not_a_pid(repo):
    ck(repo, "worktree", "create", "demo", "1")
    out = ck(repo, "worktree", "claim", "demo", "1").output
    assert "CLAIMED 1" in out and " pid " not in out
    reason = (lock_file(wt(repo, "1"))).read_text()
    assert " pid " not in reason
    assert "claim " in reason and " at " in reason and reason.rstrip().endswith("Z)")
    # two claims are told apart by their token
    ck(repo, "worktree", "release", "demo", "1")
    ck(repo, "worktree", "claim", "demo", "1")
    assert lock_file(wt(repo, "1")).read_text() != reason


def test_activity_keeps_a_claim_alive(repo):
    """A commit or a gate run in the worktree is a heartbeat: an old claim with recent work stays held."""
    ck(repo, "worktree", "create", "demo", "1")
    ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-a")
    age_claim(wt(repo, "1"), 5 * HOURS)
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1: fresh work")
    assert "BUSY 1" in ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-b").output

    age_claim(wt(repo, "1"), 5 * HOURS)
    ck(wt(repo, "1"), "gate", "demo", "--task", "1")  # pass or fail, it is activity
    assert "BUSY 1" in ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-b").output


def test_claim_timeout_comes_from_ck_json(repo):
    ck(repo, "worktree", "create", "demo", "1")
    ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-a")
    age_claim(wt(repo, "1"), 30 * 60)
    assert "BUSY 1" in ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-b").output
    (repo / "specs/ck.json").write_text(json.dumps({"claim_stale_minutes": 10}))
    took = ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-b")
    assert took.exit_code == 0 and "stale claim" in took.output and "agent-a" in took.output, took.output
    status = ck(repo, "worktree", "status", "demo").output
    assert "claimed: agent-b" in status


def test_merge_removes_worktree_and_branch_and_skips_empty(repo):
    ck(repo, "worktree", "create", "demo", "1", "2")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1: work")
    (wt(repo, "1") / ".venv").mkdir()  # ignored install artifacts must not block removal
    (wt(repo, "1") / ".venv" / "x").write_text("")

    result = ck(repo, "worktree", "merge", "demo", "1", "2")
    assert result.exit_code == 0, result.output
    assert "MERGED 1" in result.output and "1 commit(s) into main" in result.output
    assert "SKIP 2" in result.output
    assert (repo / "one.txt").read_text() == "1\n"
    assert not wt(repo, "1").exists()
    assert "feat/demo-task-1" not in git(repo, "branch")
    assert "Merge feat/demo-task-1 (task 1)" in git(repo, "log", "-1", "--format=%s")


def test_merge_conflict_aborts_cleanly_and_resolves_in_task_worktree(repo):
    ck(repo, "worktree", "create", "demo", "1", "2")
    commit_in(wt(repo, "1"), "shared.txt", "from task 1\n", "task 1")
    commit_in(wt(repo, "2"), "shared.txt", "from task 2\n", "task 2")
    assert ck(repo, "worktree", "merge", "demo", "1").exit_code == 0

    conflict = ck(repo, "worktree", "merge", "demo", "2")
    assert conflict.exit_code == 2
    assert "CONFLICT 2" in conflict.output and "shared.txt" in conflict.output
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""

    # resolve inside the task worktree, as the merge agent is told to
    task2 = wt(repo, "2")
    import subprocess

    subprocess.run(["git", "merge", "main"], cwd=task2, capture_output=True)
    (task2 / "shared.txt").write_text("from task 1\nfrom task 2\n")
    git(task2, "add", "-A")
    git(task2, "commit", "-qm", "merge main into task 2")
    merged = ck(repo, "worktree", "merge", "demo", "2")
    assert merged.exit_code == 0, merged.output
    assert (repo / "shared.txt").read_text() == "from task 1\nfrom task 2\n"


def test_merge_refuses_claimed_or_dirty_task_and_dirty_target(repo):
    ck(repo, "worktree", "create", "demo", "1")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    ck(repo, "worktree", "claim", "demo", "1", "--owner", "agent-a")
    busy = ck(repo, "worktree", "merge", "demo", "1")
    assert busy.exit_code == 3 and "BUSY 1" in busy.output
    ck(repo, "worktree", "release", "demo", "1")

    (wt(repo, "1") / "one.txt").write_text("changed but not committed\n")
    dirty = ck(repo, "worktree", "merge", "demo", "1")
    assert dirty.exit_code == 3 and "uncommitted" in dirty.output
    git(wt(repo, "1"), "checkout", "--", "one.txt")

    (repo / "shared.txt").write_text("local edit on main\n")
    target = ck(repo, "worktree", "merge", "demo", "1")
    assert target.exit_code == 3 and "refusing to merge into main" in target.output


def test_merge_into_integration_branch_leaves_main_untouched(repo):
    assert "CREATED integration" in ck(repo, "worktree", "integration", "demo").output
    ck(repo, "worktree", "create", "demo", "1", "--base", "integrate/demo")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    result = ck(repo, "worktree", "merge", "demo", "1", "--into", "integrate/demo")
    assert result.exit_code == 0, result.output
    assert not (repo / "one.txt").exists()
    assert (repo / ".ck/worktrees/demo-integration/one.txt").exists()
    # the user fast-forwards main when ready
    git(repo, "merge", "--ff-only", "integrate/demo")
    assert (repo / "one.txt").exists()


def test_status_lists_claims_and_progress(repo):
    ck(repo, "worktree", "create", "demo", "1", "2")
    commit_in(wt(repo, "1"), "one.txt", "1\n", "task 1")
    ck(repo, "worktree", "claim", "demo", "2", "--owner", "agent-b")
    out = ck(repo, "worktree", "status", "demo").output
    assert "EXISTS 1" in out and "1 ahead of main" in out
    assert "BUSY 2" in out and "claimed: agent-b" in out


# --- Where worktrees live -------------------------------------------------------


def test_default_worktree_dir_is_outside_dot_claude_and_plan_reports_it(repo):
    """Claude Code's Bash sandbox keeps .claude/ read-only, so task worktrees go to .ck/worktrees."""
    out = ck(repo, "worktree", "create", "demo", "1", "--json")
    assert out.exit_code == 0, out.output
    assert json.loads(out.output)[0]["path"] == ".ck/worktrees/demo-task-1"
    assert (repo / ".ck/worktrees/demo-task-1/shared.txt").exists()
    assert not (repo / ".claude/worktrees").exists()
    plan = json.loads(ck(repo, "plan", "demo", "--json").output)
    assert plan["worktreeDir"] == str(repo / ".ck/worktrees")


def test_worktree_dir_comes_from_ck_json(repo, tmp_path):
    (repo / "specs/ck.json").write_text(json.dumps({"worktree_dir": "build/wt"}))
    out = ck(repo, "worktree", "create", "demo", "1")
    assert "CREATED 1 build/wt/demo-task-1" in out.output, out.output
    assert json.loads(ck(repo, "plan", "demo", "--json").output)["worktreeDir"] == str(repo / "build/wt")

    outside = tmp_path / "elsewhere"
    (repo / "specs/ck.json").write_text(json.dumps({"worktree_dir": str(outside)}))
    out = ck(repo, "worktree", "create", "demo", "2", "--json")
    assert json.loads(out.output)[0]["path"] == str(outside / "demo-task-2")
    commit_in(outside / "demo-task-2", "two.txt", "2\n", "task 2")
    merged = ck(repo, "worktree", "merge", "demo", "2")
    assert merged.exit_code == 0 and "MERGED 2" in merged.output, merged.output
    assert (repo / "two.txt").exists() and not (outside / "demo-task-2").exists()


def test_in_flight_worktrees_in_the_legacy_dir_keep_working(repo):
    """A spec whose worktrees ck made in .claude/worktrees (older ck) keeps using them until they are gone."""
    legacy = repo / ".claude/worktrees/demo-task-1"
    git(repo, "worktree", "add", "-q", "-b", "feat/demo-task-1", str(legacy))
    assert json.loads(ck(repo, "plan", "demo", "--json").output)["worktreeDir"] == str(repo / ".claude/worktrees")
    assert "EXISTS 1 .claude/worktrees/demo-task-1" in ck(repo, "worktree", "create", "demo", "1").output
    assert "CLAIMED 1" in ck(repo, "worktree", "claim", "demo", "1").output
    assert "demo-task-1" in ck(repo, "worktree", "status", "demo").output
    ck(repo, "worktree", "release", "demo", "1")
    commit_in(legacy, "one.txt", "1\n", "task 1")
    landed = ck(repo, "worktree", "land", "demo", "1", "--json")
    assert landed.exit_code == 0, landed.output
    assert json.loads(landed.output)["tasks"][0]["state"] == "MERGED"
    assert not legacy.exists()
    # Once the spec has none left there, new worktrees go to the configured dir.
    assert "CREATED 2 .ck/worktrees/demo-task-2" in ck(repo, "worktree", "create", "demo", "2").output


def test_status_finds_worktrees_in_both_dirs(repo):
    git(repo, "worktree", "add", "-q", "-b", "feat/demo-task-1", str(repo / ".claude/worktrees/demo-task-1"))
    git(repo, "worktree", "add", "-q", "-b", "feat/demo-task-2", str(repo / ".ck/worktrees/demo-task-2"))
    out = ck(repo, "worktree", "status", "demo")
    assert ".claude/worktrees/demo-task-1" in out.output and ".ck/worktrees/demo-task-2" in out.output


def test_install_detection_ignores_empty_sandbox_stubs(tmp_path):
    """A sandboxed session leaves empty read-only package.json / package-lock.json stubs in the project root."""
    from claude_kiro.config import install_command, load_config

    (tmp_path / "package-lock.json").write_text("")
    (tmp_path / "package.json").write_text("")
    assert install_command(load_config(tmp_path), tmp_path) is None
    (tmp_path / "uv.lock").write_text("version = 1\n")
    assert install_command(load_config(tmp_path), tmp_path) == "uv sync --frozen -q"
