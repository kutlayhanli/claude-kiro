"""`ck worktree create` outcomes that task agents read from its JSON (2.4), and many creates at once (2.1).

Every case runs the real `ck` CLI (`python -m claude_kiro.cli`) as a subprocess on a real git repo; no script
involved, so Task 9 can be gated on this file alone.
"""

import json
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List

import pytest

from conftest import git

SPEC = "demo"


def ck(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "claude_kiro.cli", *args], cwd=cwd, capture_output=True, text=True, timeout=300)


def entries(proc: subprocess.CompletedProcess) -> List[Dict[str, Any]]:
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(f"no JSON on stdout ({e}): stdout={proc.stdout!r} stderr={proc.stderr!r}")


def entry(proc: subprocess.CompletedProcess, task: str) -> Dict[str, Any]:
    mine = [e for e in entries(proc) if str(e.get("task")) == task]
    assert len(mine) == 1, proc.stdout
    return mine[0]


def make_repo(root: Path, install: str = "") -> Path:
    """Shape of tests/test_worktree_cli.py::repo; `install` goes in specs/ck.json when given."""
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    (root / ".gitignore").write_text(".venv/\n.ck/worktrees/\n")
    (root / "shared.txt").write_text("base\n")
    (root / "specs/demo").mkdir(parents=True)
    (root / "specs/demo/tasks.md").write_text("### Task 1: a\n**Status:** Not Started\n")
    if install:
        (root / "specs/ck.json").write_text(json.dumps({"install": install}))
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


def wt(root: Path, n: str) -> Path:
    return root / ".ck/worktrees" / f"{SPEC}-task-{n}"


# --- TC-64 (2.4) -----------------------------------------------------------------------------

def test_tc64_failed_git_worktree_add_is_a_json_error_entry(repo):
    """TC-64: a bad --base makes `git worktree add` fail; the create prints a JSON array whose task-1 entry is
    ERROR with git's message, and exits 3 (today: a plain `❌ git ...` line and no JSON)."""
    proc = ck(repo, "worktree", "create", SPEC, "1", "--install", "--base", "no-such-branch", "--json")
    e = entry(proc, "1")
    assert e["state"] == "ERROR", e
    assert "no-such-branch" in e["detail"], e  # git's own message names the bad reference
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert not wt(repo, "1").exists()


def test_tc64_other_tasks_in_the_same_call_keep_their_own_outcome(repo):
    """A failed add for one task does not hide the others' outcomes (design: each task gets its own entry)."""
    ck(repo, "worktree", "create", SPEC, "2", "--json")
    proc = ck(repo, "worktree", "create", SPEC, "1", "2", "--base", "no-such-branch", "--json")
    assert entry(proc, "1")["state"] == "ERROR", proc.stdout
    assert entry(proc, "2")["state"] == "EXISTS", proc.stdout
    assert proc.returncode == 3


# --- unchanged outcomes, in JSON --------------------------------------------------------------

def test_existing_unclaimed_worktree_is_exists_in_json(repo):
    first = ck(repo, "worktree", "create", SPEC, "1", "--json")
    assert first.returncode == 0 and entry(first, "1")["state"] == "CREATED", first.stdout + first.stderr
    again = ck(repo, "worktree", "create", SPEC, "1", "--json")
    assert again.returncode == 0, again.stdout + again.stderr
    assert entry(again, "1")["state"] == "EXISTS"


def test_live_claim_is_busy_in_json(repo):
    assert ck(repo, "worktree", "create", SPEC, "1", "--json").returncode == 0
    claimed = ck(repo, "worktree", "claim", SPEC, "1", "--owner", "agent-a")
    assert claimed.returncode == 0, claimed.stdout + claimed.stderr
    busy = ck(repo, "worktree", "create", SPEC, "1", "--json")
    e = entry(busy, "1")
    assert e["state"] == "BUSY" and e["detail"].startswith("claimed:") and "agent-a" in e["detail"], e
    assert busy.returncode == 3


def test_uncommitted_work_is_busy_in_json(repo):
    assert ck(repo, "worktree", "create", SPEC, "1", "--json").returncode == 0
    (wt(repo, "1") / "wip.txt").write_text("wip\n")
    busy = ck(repo, "worktree", "create", SPEC, "1", "--json")
    e = entry(busy, "1")
    assert e["state"] == "BUSY" and "uncommitted" in e["detail"], e
    assert busy.returncode == 3
    assert (wt(repo, "1") / "wip.txt").read_text() == "wip\n"


# --- concurrent creates (2.1) ----------------------------------------------------------------

THREADS = 12
ROUNDS = 5
INSTALL_SECONDS = 0.4


def test_concurrent_creates_all_succeed_and_installs_overlap(tmp_path):
    """Regression guard for `create_lock` (2.1): 12 threads x 5 rounds of `ck worktree create demo <n> --install
    --base main --json` for different tasks on one repo with a local base.

    Reproduction attempt without the lock (before Task 9): the collision did NOT reproduce with this recipe
    (12 threads x 5 rounds, 60 creates), so this test is a regression guard only and may pass before Task 9.
    Either way every create must succeed, and installs of different tasks must overlap (the install step runs
    outside the lock)."""
    log_dir = tmp_path / "install-log"
    log_dir.mkdir()
    script = (
        "import os, pathlib, sys, time; d = pathlib.Path(sys.argv[1]); name = pathlib.Path.cwd().name; "
        "(d / (name + '.start')).write_text(repr(time.time())); "
        f"time.sleep({INSTALL_SECONDS}); "
        "(d / (name + '.end')).write_text(repr(time.time()))"
    )
    install = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} {shlex.quote(str(log_dir))}"
    root = make_repo(tmp_path / "repo", install=install)

    results: Dict[str, subprocess.CompletedProcess] = {}
    lock = threading.Lock()

    def create(n: str) -> None:
        proc = ck(root, "worktree", "create", SPEC, n, "--install", "--base", "main", "--json")
        with lock:
            results[n] = proc

    for rnd in range(ROUNDS):
        nums = [str(rnd * THREADS + i + 1) for i in range(THREADS)]
        threads = [threading.Thread(target=create, args=(n,)) for n in nums]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    failures = {n: (p.returncode, p.stdout, p.stderr) for n, p in results.items() if p.returncode != 0}
    assert not failures, failures
    assert len(results) == THREADS * ROUNDS
    for n, proc in results.items():
        assert entry(proc, n)["state"] == "CREATED", (n, proc.stdout)
        assert git(wt(root, n), "branch", "--show-current").strip() == f"feat/{SPEC}-task-{n}"

    intervals = []
    for n in results:
        name = f"{SPEC}-task-{n}"
        intervals.append((float((log_dir / f"{name}.start").read_text()), float((log_dir / f"{name}.end").read_text())))
    overlapping = any(a[0] < b[1] and b[0] < a[1] for i, a in enumerate(intervals) for b in intervals[i + 1:])
    assert overlapping, "installs of different tasks never overlapped: the install step must run outside the lock"
