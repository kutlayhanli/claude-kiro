"""Claims record the progress run that made them, and a run start releases the claims of runs that are over.

TC-142 to TC-147, TC-148 (a) and TC-192, through the real ck CLI, real git locks and real progress records
(`ck progress begin/end`, `CK_NOW`). Nothing here mocks project code. Project modules are imported inside the
tests that call them, so this file collects before the implementation exists.

Releasing a claim only unlocks the worktree: every release below also checks that the branch's commits are kept.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from test_worktree_cli import age_claim, legacy_claim, lock_file
from unattended_helpers import ck_json, ck_text, commit_all, git, init_repo, progress_run

SPEC = "demo"
MIN = 60


# --------------------------------------------------------------------------- helpers


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = init_repo(tmp_path / "repo")
    (root / ".gitignore").write_text(".venv/\n.ck/worktrees/\n")
    (root / "shared.txt").write_text("base\n")
    (root / "specs" / SPEC).mkdir(parents=True)
    tasks = "".join(f"### Task {n}: t{n}\n**Status:** Not Started\n\n" for n in (1, 2, 3))
    (root / "specs" / SPEC / "tasks.md").write_text(tasks)
    commit_all(root, "base")
    return root


def wt(root: Path, n: int) -> Path:
    return root / ".ck" / "worktrees" / f"{SPEC}-task-{n}"


def is_locked(root: Path, n: int) -> bool:
    return lock_file(wt(root, n)).exists()


def make_task(root: Path, n: int, *, run: Optional[str] = None, owner: str = "agent-a", env=None) -> str:
    """Create Task n's worktree, claim it (recording `run` when given), commit work in it; return the head sha."""
    code, out, err = ck_text(root, "worktree", "create", SPEC, str(n))
    assert code == 0, out + err
    args = ["worktree", "claim", SPEC, str(n), "--owner", owner]
    if run:
        args += ["--progress-run", run]
    code, out, err = ck_text(root, *args, env=env)
    assert code == 0 and f"CLAIMED {n}" in out, out + err
    (wt(root, n) / f"task{n}.txt").write_text(f"{n}\n")
    git(wt(root, n), "add", "-A")
    git(wt(root, n), "commit", "-qm", f"task {n}: work")
    return git(wt(root, n), "rev-parse", "HEAD").strip()


def start(root: Path, run: str, env=None) -> dict:
    code, obj, err = ck_json(root, "worktree", "start", SPEC, "--run", run, "--json", env=env)
    assert code == 0 and isinstance(obj, dict), f"worktree start failed: code={code} out={obj!r} err={err}"
    return obj


def released(obj: dict) -> Dict[str, str]:
    """task -> run of every entry in `releasedClaims`."""
    entries = obj.get("releasedClaims")
    assert isinstance(entries, list), f"no releasedClaims list in {obj!r}"
    return {str(e["task"]): e.get("run") for e in entries}


def assert_commits_kept(root: Path, n: int, head: str) -> None:
    """Releasing is an unlock: the worktree, its files, its HEAD and its branch tip are all still there."""
    assert wt(root, n).is_dir(), f"worktree of Task {n} was removed"
    assert (wt(root, n) / f"task{n}.txt").read_text() == f"{n}\n"
    now = git(wt(root, n), "rev-parse", "HEAD").strip()
    branch = git(wt(root, n), "rev-parse", "--abbrev-ref", "HEAD").strip()
    tip = git(root, "rev-parse", branch).strip()
    assert git(root, "cat-file", "-t", head).strip() == "commit" and git(root, "merge-base", head, now).strip() == head, f"Task {n} lost its commit"
    assert tip == now, f"Task {n}'s branch {branch} does not point at the worktree HEAD"


def liveness(key: str, env, now: Optional[datetime] = None) -> str:
    from claude_kiro import progress

    got = progress.run_liveness(key, env, now or datetime.now(timezone.utc))
    return str(getattr(got, "value", got))


# --------------------------------------------------------------------------- TC-142


def test_tc142_claim_records_the_run_and_status_shows_it(repo):
    run = progress_run(SPEC, "A", ended=False, last_event_age_min=1, cwd=repo)
    make_task(repo, 1, run=run)
    make_task(repo, 2)  # a claim with no run records none

    reason = lock_file(wt(repo, 1)).read_text()
    assert run in reason, f"the lock reason does not record the run: {reason!r}"

    code, rows, err = ck_json(repo, "worktree", "status", SPEC, "--json")
    assert code in (0, 3) and isinstance(rows, list), f"status failed: code={code} err={err}"
    by_task = {str(r["task"]): r for r in rows}
    # status shows the lock reason (which carries the run) in `detail`
    assert f"run {run}" in str(by_task["1"].get("detail")), by_task["1"]
    assert " run " not in str(by_task["2"].get("detail")), by_task["2"]


# --------------------------------------------------------------------------- TC-143


def test_tc143_a_finished_run_releases_both_claims_and_keeps_commits(repo):
    run_a = progress_run(SPEC, "A", ended=True, last_event_age_min=5, cwd=repo)
    run_b = progress_run(SPEC, "B", ended=False, last_event_age_min=0, cwd=repo)
    heads = {n: make_task(repo, n, run=run_a) for n in (1, 2)}
    assert is_locked(repo, 1) and is_locked(repo, 2)

    obj = start(repo, run_b)

    assert released(obj) == {"1": run_a, "2": run_a}, obj
    for n, head in heads.items():
        assert not is_locked(repo, n), f"Task {n} is still claimed"
        assert_commits_kept(repo, n, head)


# --------------------------------------------------------------------------- TC-144


def test_tc144_a_stale_run_releases_its_claim(repo):
    # no end event, last event past the stale threshold (progress.STALE_MIN is 120 minutes)
    run_a = progress_run(SPEC, "A", ended=False, last_event_age_min=3 * 60, cwd=repo)
    run_b = progress_run(SPEC, "B", ended=False, last_event_age_min=0, cwd=repo)
    head = make_task(repo, 1, run=run_a)

    obj = start(repo, run_b)

    assert released(obj) == {"1": run_a}, obj
    assert not is_locked(repo, 1)
    assert_commits_kept(repo, 1, head)


# --------------------------------------------------------------------------- TC-145


def test_tc145_a_live_runs_old_claim_is_kept_and_busy_for_another_run(repo):
    run_a = progress_run(SPEC, "A", ended=False, last_event_age_min=1, cwd=repo)
    run_b = progress_run(SPEC, "B", ended=False, last_event_age_min=0, cwd=repo)
    head = make_task(repo, 1, run=run_a)
    age_claim(wt(repo, 1), 3 * 60 * MIN)  # the claim and its last commit are 3 hours old; the run is live

    obj = start(repo, run_b)

    assert "1" not in released(obj), obj
    assert is_locked(repo, 1), "a live run's claim was released"
    code, out, err = ck_text(repo, "worktree", "claim", SPEC, "1", "--owner", "agent-b", "--progress-run", run_b)
    assert code == 3 and "BUSY 1" in out, out + err
    assert is_locked(repo, 1)
    assert_commits_kept(repo, 1, git(wt(repo, 1), "rev-parse", "HEAD").strip())
    assert git(repo, "rev-parse", "--verify", "-q", head).strip() == head


# --------------------------------------------------------------------------- TC-146


def _legacy_task(repo: Path, n: int, minutes: int) -> str:
    code, out, err = ck_text(repo, "worktree", "create", SPEC, str(n))
    assert code == 0, out + err
    (wt(repo, n) / f"task{n}.txt").write_text(f"{n}\n")
    git(wt(repo, n), "add", "-A")
    git(wt(repo, n), "commit", "-qm", f"task {n}: work")
    head = git(wt(repo, n), "rev-parse", "HEAD").strip()
    legacy_claim(wt(repo, n), 999999999, seconds_ago=minutes * MIN)
    age_claim(wt(repo, n), minutes * MIN)
    return git(wt(repo, n), "rev-parse", "HEAD").strip() or head


def test_tc146_a_claim_with_no_run_follows_the_sixty_minute_rule(repo):
    run_b = progress_run(SPEC, "B", ended=False, last_event_age_min=0, cwd=repo)
    heads = {1: _legacy_task(repo, 1, 30), 2: _legacy_task(repo, 2, 61)}

    obj = start(repo, run_b)

    assert set(released(obj)) == {"2"}, obj
    assert is_locked(repo, 1), "a 30-minute-old claim with no run ID was released"
    assert not is_locked(repo, 2), "a 61-minute-old claim with no run ID was kept"
    for n, head in heads.items():
        assert_commits_kept(repo, n, head)


def test_tc146_claim_stale_minutes_is_honoured(repo):
    run_b = progress_run(SPEC, "B", ended=False, last_event_age_min=0, cwd=repo)
    heads = {1: _legacy_task(repo, 1, 30)}

    (repo / "specs" / "ck.json").write_text(json.dumps({"claim_stale_minutes": 10}))
    obj = start(repo, run_b)
    assert set(released(obj)) == {"1"}, "a 30-minute-old claim was kept with claim_stale_minutes 10"
    assert not is_locked(repo, 1)
    assert_commits_kept(repo, 1, heads[1])

    # and a larger value keeps a claim the default rule would release
    heads[2] = _legacy_task(repo, 2, 61)
    (repo / "specs" / "ck.json").write_text(json.dumps({"claim_stale_minutes": 180}))
    obj = start(repo, run_b)
    assert "2" not in released(obj), "a 61-minute-old claim was released with claim_stale_minutes 180"
    assert is_locked(repo, 2)
    assert_commits_kept(repo, 2, heads[2])


# --------------------------------------------------------------------------- TC-147


def test_tc147_a_deleted_run_record_releases_its_claim(repo):
    from claude_kiro import progress

    run_a = progress_run(SPEC, "A", ended=False, last_event_age_min=1, cwd=repo)
    run_b = progress_run(SPEC, "B", ended=False, last_event_age_min=0, cwd=repo)
    head = make_task(repo, 1, run=run_a)
    import os

    record = progress.run_file(run_a, dict(os.environ))
    assert record.exists()
    record.unlink()

    obj = start(repo, run_b)

    assert released(obj) == {"1": run_a}, obj
    assert not is_locked(repo, 1)
    assert_commits_kept(repo, 1, head)


# --------------------------------------------------------------------------- TC-148 (a)


def test_tc148a_the_starting_runs_own_claim_is_kept_and_reclaimable_without_takeover(repo):
    run_b = progress_run(SPEC, "B", ended=False, last_event_age_min=0, cwd=repo)
    head = make_task(repo, 1, run=run_b)

    obj = start(repo, run_b)

    assert "1" not in released(obj), f"the starting run's own claim was released: {obj!r}"
    assert is_locked(repo, 1)
    code, out, err = ck_text(repo, "worktree", "claim", SPEC, "1", "--owner", "agent-b2", "--progress-run", run_b)
    assert code == 0 and "CLAIMED 1" in out, out + err
    assert "BUSY" not in out
    assert_commits_kept(repo, 1, head)


# --------------------------------------------------------------------------- TC-192


def _spool_pass(repo: Path):
    """A pass under the spool layout, killed: both tasks claimed, no `end`, last event 10 minutes old, no data-dir copy."""
    from claude_kiro import worktrees

    spool = Path(worktrees.worktrees_dir(worktrees.main_root(repo), SPEC)) / ".ck-progress"
    spool.mkdir(parents=True, exist_ok=True)
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 10 * MIN))
    env = {"CK_PROGRESS_DIR": str(spool), "CK_NOW": old}
    run_a = progress_run(SPEC, "A", ended=False, last_event_age_min=10, cwd=repo, env={"CK_PROGRESS_DIR": str(spool)})
    heads = {n: make_task(repo, n, run=run_a, env=env) for n in (1, 2)}
    return spool, run_a, heads


def _events(path: Path) -> List[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_tc192_a_killed_spool_pass_is_superseded_then_its_claims_are_released(repo):
    import os

    from claude_kiro import progress
    from claude_kiro.eta.service import prepare_spool

    run_b = progress_run(SPEC, "B", ended=False, last_event_age_min=0, cwd=repo)
    spool, run_a, heads = _spool_pass(repo)
    copy = spool / f"{run_a}.jsonl"
    assert copy.exists() and not any(e.get("kind") == "end" for e in _events(copy))
    data_copy = progress.run_file(run_a, {k: v for k, v in os.environ.items() if k != "CK_PROGRESS_DIR"})
    assert not data_copy.exists(), "the data-dir copy should be absent in the spool layout"

    prepare_spool(repo, SPEC, dict(os.environ))

    ends = [e for e in _events(copy) if e.get("kind") == "end"]
    assert ends and ends[-1].get("outcome") == "superseded", f"the spool copy was not superseded: {_events(copy)!r}"

    obj = start(repo, run_b, env={"CK_PROGRESS_DIR": str(spool)})

    assert released(obj) == {"1": run_a, "2": run_a}, obj
    for n, head in heads.items():
        assert not is_locked(repo, n)
        assert_commits_kept(repo, n, head)


def test_tc192_without_prepare_spool_run_liveness_still_reads_the_spool_copy(repo):
    import os

    spool, run_a, _heads = _spool_pass(repo)
    env = {**os.environ, "CK_PROGRESS_DIR": str(spool)}

    assert liveness(run_a, env) == "running", "a live spool run was read as missing or finished"
    # a spool pass that ended is finished, read from the spool copy alone
    from claude_kiro import progress

    assert progress.append(run_a, {"kind": "end", "at": "2026-01-01T00:00:00Z", "outcome": "completed", "merged": None,
                                   "total": None, "usd": None}, env) is None
    assert liveness(run_a, env) == "finished"
    # and with no copy anywhere it is missing
    assert liveness("demo--20200101T000000Z-000000", env) == "missing"
