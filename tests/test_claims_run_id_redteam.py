"""Red team of Story 16 (release claims by run ID): 16.2, 16.3, 16.5.

Written from the requirements, not the implementation. The clause under attack is 16.3: a claim whose recorded
run is running SHALL be kept and its task not taken. The other side of the same boundary is 16.2 / 16.5: a claim
whose run is finished, not running or has no record is released, and the claimed branch's commits are kept.

Seam cases (`test_tc145_redteam_seam_<slug>`) go against `claim_info`, `run_liveness` and `claim_release_decision`.
CLI cases (`test_tc145_redteam_<slug>`) go through `ck worktree claim` and `ck worktree start` with real git locks
and real progress records (`ck progress begin/end`, or records appended with `claude_kiro.progress.append`).

Recorded behaviour the design accepts (see the stale-live cases): a run whose last event is older than
`progress.STALE_MIN` (120 minutes) is "not running", even if its only agent is still working. Its claim is released
and its commits are kept.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from conftest import git
from test_worktree_cli import age_claim
from unattended_helpers import ck_json, ck_text, commit_all, init_repo, progress_run, task_worktree

SPEC = "demo"
STARTING = "demo--B"
GENUINE_TIME = "2026-01-01T00:00:00Z"

# --------------------------------------------------------------------------- helpers


def make_repo(tmp_path: Path, tasks=("1",)) -> Path:
    """A real repo with a spec and one task worktree per task, each with a commit of its own."""
    root = init_repo(tmp_path / "repo")
    (root / ".gitignore").write_text(".venv/\n.ck/worktrees/\n")
    (root / "shared.txt").write_text("base\n")
    (root / "specs" / SPEC).mkdir(parents=True)
    body = "".join(f"### Task {n}: t{n}\n**Status:** Not Started\n**Track:** impl\n**Dependencies:** None\n\n" for n in tasks)
    (root / "specs" / SPEC / "tasks.md").write_text("# Implementation Tasks: Demo\n\n## Task Breakdown\n\n" + body)
    commit_all(root, "base")
    code, out, err = ck_text(root, "worktree", "create", SPEC, *tasks)
    assert code == 0, (out, err)
    for n in tasks:
        path = task_worktree(root, n)
        (path / f"work-{n}.txt").write_text(f"work {n}\n")
        git(path, "add", "-A")
        git(path, "commit", "-qm", f"task {n}: work")
    return root


def head(root: Path, n: str) -> str:
    return git(task_worktree(root, n), "rev-parse", "HEAD").strip()


def lock_reason_of(root: Path, n: str) -> Optional[str]:
    """The git lock reason of a task worktree (None when unlocked), read with plain git."""
    target = str(task_worktree(root, n).resolve())
    path = None
    for line in git(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line.split(" ", 1)[1]
        elif path == target and (line == "locked" or line.startswith("locked ")):
            return line[len("locked "):] if line.startswith("locked ") else ""
    return None


def claim(root: Path, n: str, run: Optional[str], owner: str = "agent", age: Optional[int] = None) -> None:
    args = ["worktree", "claim", SPEC, n, "--owner", owner]
    if run is not None:
        args += ["--progress-run", run]
    code, out, err = ck_text(root, *args)
    assert code == 0 and "CLAIMED" in out, (out, err)
    if age is not None:
        age_claim(task_worktree(root, n), age)


def start(root: Path, run: str = STARTING, env: Optional[Dict[str, str]] = None):
    code, obj, err = ck_json(root, "worktree", "start", SPEC, "--run", run, "--json", env=env)
    assert isinstance(obj, dict), (code, obj, err)
    assert code == 0, (code, obj, err)
    return obj


def released(obj) -> Dict[str, Optional[str]]:
    return {str(c["task"]): c.get("run") for c in obj["releasedClaims"]}


def kept(obj) -> List[str]:
    return [str(c["task"]) for c in obj["keptClaims"]]


def assert_released(root: Path, obj, n: str, before: str, run: Optional[str] = None) -> None:
    assert n in released(obj), obj
    if run is not None:
        assert released(obj)[n] == run, obj
    assert n not in kept(obj), obj
    assert lock_reason_of(root, n) is None  # unlocked
    assert head(root, n) == before  # commits kept
    assert subprocess.run(["git", "rev-parse", "--verify", "-q", f"feat/{SPEC}-task-{n}"], cwd=root).returncode == 0


def assert_kept(root: Path, obj, n: str, before: str) -> None:
    assert n in kept(obj), obj
    assert n not in released(obj), obj
    assert lock_reason_of(root, n) is not None  # still locked
    assert head(root, n) == before


def assert_busy_for_other(root: Path, n: str) -> None:
    code, out, err = ck_text(root, "worktree", "claim", SPEC, n, "--owner", "agent-b")
    assert "BUSY" in out and "CLAIMED" not in out, (code, out, err)


def live(root: Path, minutes: float = 5) -> str:
    return progress_run(SPEC, "A", ended=False, last_event_age_min=minutes, cwd=root)


def ended(root: Path) -> str:
    return progress_run(SPEC, "D", ended=True, last_event_age_min=10, cwd=root)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(key: str, env: Dict[str, str], *, age_min: float, end: bool = False, now: Optional[datetime] = None) -> None:
    """Append a real run record for `key` (a start event, and optionally an end) under `env`'s progress dir."""
    from claude_kiro import progress

    now = now or datetime.now(timezone.utc)
    at = iso(now - timedelta(minutes=age_min))
    err = progress.append(key, {"kind": "start", "at": at, "run": key, "runKind": "implement", "spec": SPEC}, env=env)
    assert err is None, err
    if end:
        err = progress.append(key, {"kind": "end", "at": at, "outcome": "completed"}, env=env)
        assert err is None, err


def data_env() -> Dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "CK_PROGRESS_DIR"}


def spool_env(spool: Path) -> Dict[str, str]:
    return {**data_env(), "CK_PROGRESS_DIR": str(spool)}


# --------------------------------------------------------------------------- CLI: live runs are kept (16.3)


def test_tc145_redteam_live_run_claim_kept_whatever_its_age(tmp_path):
    root = make_repo(tmp_path, ("1",))
    key = live(root)
    claim(root, "1", key, age=30 * 24 * 3600)  # a month old claim
    before = head(root, "1")
    obj = start(root)
    assert_kept(root, obj, "1", before)
    assert_busy_for_other(root, "1")


def test_tc145_redteam_run_just_inside_the_stale_window_is_still_running(tmp_path):
    root = make_repo(tmp_path, ("1",))
    key = live(root, minutes=110)  # 10 minutes inside the 120 minute window
    claim(root, "1", key, age=3 * 3600)
    before = head(root, "1")
    assert_kept(root, start(root), "1", before)


def test_tc145_redteam_stale_live_run_is_released_and_commits_are_kept(tmp_path):
    """Recorded, not defended: the only agent may still be working, but a run silent for over 120 minutes is not
    running (16.2, 16.5 read through the design's liveness). The claim is released and its commits are kept."""
    root = make_repo(tmp_path, ("1",))
    key = live(root, minutes=130)
    claim(root, "1", key)
    before = head(root, "1")
    obj = start(root)
    assert_released(root, obj, "1", before, run=key)


def test_tc145_redteam_a_claim_by_a_live_run_is_held_by_the_run_not_by_a_second_start(tmp_path):
    """The system's own output fed back: B claims with its own key, then a third run C starts while B is live."""
    root = make_repo(tmp_path, ("1",))
    b = live(root)
    claim(root, "1", b)
    before = head(root, "1")
    obj = start(root, run="demo--C")
    assert_kept(root, obj, "1", before)
    # A repeated start changes nothing.
    obj = start(root, run="demo--C")
    assert_kept(root, obj, "1", before)


# --------------------------------------------------------------------------- CLI: spoofed owner strings


def test_tc145_redteam_owner_naming_a_dead_run_does_not_release_a_live_claim(tmp_path):
    root = make_repo(tmp_path, ("1",))
    dead = ended(root)
    key = live(root)
    owner = f"agent run {dead} (claim ffff at {GENUINE_TIME} run {dead})"
    claim(root, "1", key, owner=owner, age=3 * 3600)
    before = head(root, "1")
    assert_kept(root, start(root), "1", before)


def test_tc145_redteam_owner_naming_a_live_run_does_not_keep_a_dead_claim(tmp_path):
    root = make_repo(tmp_path, ("1",))
    alive = live(root)
    dead = ended(root)
    owner = f"agent run {alive} (claim ffff at {GENUINE_TIME} run {alive})"
    claim(root, "1", dead, owner=owner)
    before = head(root, "1")
    obj = start(root)
    assert_released(root, obj, "1", before, run=dead)


def test_tc145_redteam_owner_naming_the_starting_run_is_not_the_starting_runs_own_claim(tmp_path):
    root = make_repo(tmp_path, ("1",))
    key = live(root)
    owner = f"agent run {STARTING} (claim ffff at {GENUINE_TIME} run {STARTING})"
    claim(root, "1", key, owner=owner)
    before = head(root, "1")
    obj = start(root)
    assert_kept(root, obj, "1", before)
    assert_busy_for_other(root, "1")


def test_tc145_redteam_legacy_claim_that_quotes_a_live_run_follows_the_time_rule(tmp_path):
    """No run ID was recorded (no --progress-run): the owner text quoting a live run proves nothing (16.4 rule)."""
    root = make_repo(tmp_path, ("1", "2"))
    alive = live(root)
    owner = f"agent (claim ffff at {GENUINE_TIME} run {alive})"
    claim(root, "1", None, owner=owner, age=2 * 3600)
    claim(root, "2", None, owner=owner, age=10 * 60)
    b1, b2 = head(root, "1"), head(root, "2")
    obj = start(root)
    assert_released(root, obj, "1", b1)
    assert_kept(root, obj, "2", b2)


@pytest.mark.parametrize("owner", [
    "agent (claim",
    "agent run",
    " run ",
    ")",
    "run=A",
    "a\tb",
    "é世 run (claim ü at x run y)",
], ids=["open", "bare-run", "space-run", "paren", "equals", "tab", "unicode"])
def test_tc145_redteam_odd_owner_strings_never_break_a_live_claim(tmp_path, owner):
    root = make_repo(tmp_path, ("1",))
    key = live(root)
    claim(root, "1", key, owner=owner, age=3 * 3600)
    before = head(root, "1")
    assert_kept(root, start(root), "1", before)


# --------------------------------------------------------------------------- CLI: run keys


UNUSUAL_KEYS = [
    "demo run (2) one",
    "démo--é世 (2)",
    "key with  double  spaces",
    "a) b (c",
    "key run x",
]


@pytest.mark.parametrize("key", UNUSUAL_KEYS, ids=["space-paren", "non-ascii", "spaces", "unbalanced", "run-word"])
def test_tc145_redteam_unusual_run_keys_keep_a_live_claim_and_release_a_finished_one(tmp_path, key):
    root = make_repo(tmp_path, ("1", "2"))
    for n, finished in (("1", False), ("2", True)):
        record(key + ("" if not finished else "-fin"), data_env(), age_min=5, end=finished)
    claim(root, "1", key)
    claim(root, "2", key + "-fin")
    b1, b2 = head(root, "1"), head(root, "2")
    obj = start(root)
    assert_kept(root, obj, "1", b1)
    assert_released(root, obj, "2", b2, run=key + "-fin")


def test_tc145_redteam_near_miss_keys_of_a_live_record_are_not_running(tmp_path):
    root = make_repo(tmp_path, ("1", "2", "3", "4"))
    key = live(root)
    claim(root, "1", key[:-1])  # prefix of a live key
    claim(root, "2", "demo--*")  # a glob that would match the live record
    claim(root, "3", key + "x")  # an extension of it
    claim(root, "4", key)  # control: the genuine key
    heads = {n: head(root, n) for n in "1234"}
    obj = start(root)
    for n in "123":
        assert_released(root, obj, n, heads[n])
    assert_kept(root, obj, "4", heads["4"])


@pytest.mark.parametrize("key", ["../x", "..", ".hidden", "a/b", "a\\b"], ids=["dotdot-x", "dotdot", "hidden", "slash", "backslash"])
def test_tc145_redteam_unusable_run_keys_are_not_found_not_a_crash(tmp_path, key):
    """16.5: a record that cannot be found means not running. A key ck cannot even use as a file name is not found."""
    root = make_repo(tmp_path, ("1",))
    code, out, err = ck_text(root, "worktree", "claim", SPEC, "1", "--owner", "agent")
    assert code == 0, (out, err)
    git(root, "worktree", "unlock", str(task_worktree(root, "1")))
    git(root, "worktree", "lock", "--reason", f"agent (claim abcd at {GENUINE_TIME} run {key})", str(task_worktree(root, "1")))
    before = head(root, "1")
    obj = start(root)
    assert_released(root, obj, "1", before)


def test_tc145_redteam_deleted_record_releases_but_a_surviving_copy_keeps(tmp_path):
    root = make_repo(tmp_path, ("1", "2"))
    spool = tmp_path / "spool"
    key1, key2 = "demo--gone", "demo--spooled"
    record(key1, data_env(), age_min=5)
    record(key2, spool_env(spool), age_min=5)
    claim(root, "1", key1)
    claim(root, "2", key2)
    (Path(data_env()["XDG_DATA_HOME"]) / "claude-kiro" / "progress" / f"{key1}.jsonl").unlink()
    b1, b2 = head(root, "1"), head(root, "2")
    obj = start(root, env=spool_env(spool))
    assert_released(root, obj, "1", b1, run=key1)  # no copy anywhere: not running (16.5)
    assert_kept(root, obj, "2", b2)  # the spool copy alone is live: no false "missing" (16.3)


# --------------------------------------------------------------------------- CLI: spool and data-dir copies


def test_tc145_redteam_forged_end_in_one_copy_beside_a_live_spool_copy(tmp_path):
    """Recorded: any copy with an `end` event makes the run finished (design: finished if some copy has end)."""
    root = make_repo(tmp_path, ("1",))
    spool = tmp_path / "spool"
    key = "demo--forged"
    record(key, spool_env(spool), age_min=5)  # live spool copy
    record(key, data_env(), age_min=5, end=True)  # data-dir copy with an end
    claim(root, "1", key)
    before = head(root, "1")
    obj = start(root, env=spool_env(spool))
    assert_released(root, obj, "1", before, run=key)


def test_tc145_redteam_disagreeing_copies_newest_event_decides(tmp_path):
    root = make_repo(tmp_path, ("1", "2", "3"))
    spool = tmp_path / "spool"
    stale, fresh = 150, 5
    record("demo--spool-fresh", spool_env(spool), age_min=fresh)
    record("demo--spool-fresh", data_env(), age_min=stale)
    record("demo--data-fresh", spool_env(spool), age_min=stale)
    record("demo--data-fresh", data_env(), age_min=fresh)
    record("demo--both-stale", spool_env(spool), age_min=stale)
    record("demo--both-stale", data_env(), age_min=stale + 10)
    for n, key in (("1", "demo--spool-fresh"), ("2", "demo--data-fresh"), ("3", "demo--both-stale")):
        claim(root, n, key)
    heads = {n: head(root, n) for n in "123"}
    obj = start(root, env=spool_env(spool))
    assert_kept(root, obj, "1", heads["1"])
    assert_kept(root, obj, "2", heads["2"])
    assert_released(root, obj, "3", heads["3"], run="demo--both-stale")


def test_tc145_redteam_a_record_a_task_failure_mentions_is_not_an_end(tmp_path):
    """A live run's record quoting an end event inside another event's text, and a truncated end line, are not ends."""
    from claude_kiro import progress

    root = make_repo(tmp_path, ("1",))
    env = data_env()
    key = "demo--quoting"
    record(key, env, age_min=5)
    err = progress.append(key, {"kind": "task-failed", "task": "1", "reason": '{"kind":"end","outcome":"completed"}'}, env=env)
    assert err is None, err
    path = Path(env["XDG_DATA_HOME"]) / "claude-kiro" / "progress" / f"{key}.jsonl"
    with path.open("a") as fh:
        fh.write('{"schema":"ck-progress/1","kind":"end","outcome":"comp')  # a torn write, no newline
    claim(root, "1", key)
    before = head(root, "1")
    assert_kept(root, start(root), "1", before)


# --------------------------------------------------------------------------- CLI: clock skew


def test_tc145_redteam_a_starting_process_behind_the_events_never_releases_a_live_claim(tmp_path):
    root = make_repo(tmp_path, ("1",))
    key = live(root)
    claim(root, "1", key, age=3 * 3600)
    before = head(root, "1")
    behind = iso(datetime.now(timezone.utc) - timedelta(hours=6))  # the run's events are "in the future"
    obj = start(root, env={"CK_NOW": behind})
    assert_kept(root, obj, "1", before)


def test_tc145_redteam_a_starting_process_far_ahead_decides_consistently(tmp_path):
    """Recorded: `CK_NOW` is the starting process's clock, so a clock 3 hours ahead sees the run's events as old.
    Whatever it decides, the task is in exactly one list and the commits are kept."""
    root = make_repo(tmp_path, ("1",))
    key = live(root)
    claim(root, "1", key)
    before = head(root, "1")
    ahead = iso(datetime.now(timezone.utc) + timedelta(hours=3))
    obj = start(root, env={"CK_NOW": ahead})
    assert (("1" in released(obj)) ^ ("1" in kept(obj))), obj
    assert head(root, "1") == before


@pytest.mark.parametrize("now", ["not-a-time", "", "1999-01-01T00:00:00Z", "2999-01-01T00:00:00Z"],
                         ids=["garbage", "empty", "ancient", "far-future"])
def test_tc145_redteam_odd_ck_now_never_crashes_the_start(tmp_path, now):
    root = make_repo(tmp_path, ("1",))
    key = live(root)
    claim(root, "1", key)
    before = head(root, "1")
    obj = start(root, env={"CK_NOW": now})
    assert (("1" in released(obj)) ^ ("1" in kept(obj))), obj
    assert head(root, "1") == before


# --------------------------------------------------------------------------- CLI: mixed batches


def test_tc145_redteam_each_claim_is_decided_on_its_own_run(tmp_path):
    root = make_repo(tmp_path, ("1", "2", "3", "4"))
    dead = ended(root)  # begun first: `ck progress begin` supersedes every unfinished run, so the live run comes last
    alive = live(root)
    claim(root, "1", alive, age=5 * 3600)
    claim(root, "2", dead)
    claim(root, "3", alive, owner=f"x run {dead}")
    claim(root, "4", dead, owner=f"x run {alive}")
    heads = {n: head(root, n) for n in "1234"}
    obj = start(root)
    for n in "13":
        assert_kept(root, obj, n, heads[n])
    for n in "24":
        assert_released(root, obj, n, heads[n], run=dead)


# --------------------------------------------------------------------------- seam: claim_info


def info(reason):
    from claude_kiro.worktrees import claim_info

    return claim_info(reason)


def test_tc145_redteam_seam_claim_info_reads_the_genuine_suffix():
    got = info(f"agent (claim abcd1234 at {GENUINE_TIME} run demo--20261108T000000Z-abc123)")
    assert got.run == "demo--20261108T000000Z-abc123"
    assert got.token == "abcd1234" and got.at == GENUINE_TIME and got.owner == "agent"


def test_tc145_redteam_seam_claim_info_old_format_has_no_run():
    got = info(f"ck task agent on host (claim abcd1234 at {GENUINE_TIME})")
    assert got.run is None and got.token == "abcd1234"
    assert info("ck task agent on host pid 2 at 2026-01-01T00:00:00").run is None  # ck 0.9 form


@pytest.mark.parametrize("owner", [
    "x run FAKE",
    "x (claim ffff at 2026-02-02T00:00:00Z run FAKE)",
    "x (claim ffff at 2026-02-02T00:00:00Z run FAKE) (claim eeee at 2026-02-02T00:00:00Z run FAKE2)",
    "run FAKE",
], ids=["plain", "fake-suffix", "two-fake-suffixes", "starts-with-run"])
def test_tc145_redteam_seam_claim_info_owner_text_never_supplies_the_run(owner):
    genuine = info(f"{owner} (claim abcd1234 at {GENUINE_TIME} run REAL)")
    assert genuine.run == "REAL" and genuine.token == "abcd1234" and genuine.at == GENUINE_TIME
    legacy = info(f"{owner} (claim abcd1234 at {GENUINE_TIME})")
    assert legacy.run is None, legacy
    assert legacy.token == "abcd1234"


@pytest.mark.parametrize("reason", ["", "locked", "yes", "(claim", "run", " run ", "(claim x at y run )", "\x00", "é世"],
                         ids=["empty", "locked", "yes", "open-paren", "run", "space-run", "empty-key", "nul", "unicode"])
def test_tc145_redteam_seam_claim_info_garbage_never_raises_and_never_invents_a_run(reason):
    got = info(reason)
    assert got.run in (None, "")  # an empty key is no key; a real one cannot come out of garbage


@pytest.mark.parametrize("key", UNUSUAL_KEYS, ids=["space-paren", "non-ascii", "spaces", "unbalanced", "run-word"])
def test_tc145_redteam_seam_claim_info_round_trips_the_claims_own_lock_reason(tmp_path, key):
    from claude_kiro import worktrees as wt

    root = make_repo(tmp_path, ("1",))
    path = wt.task_path(root, SPEC, "1")
    for owner in ("agent", "agent run other", f"agent (claim ffff at {GENUINE_TIME} run FAKE)"):
        if wt.lock_reason(root, path):
            git(root, "worktree", "unlock", str(path))
        out = wt.claim(root, SPEC, "1", owner, run=key)
        assert out.state == "CLAIMED", out
        got = wt.claim_info(wt.lock_reason(root, path))
        assert got.run == key, (owner, key, wt.lock_reason(root, path))


# --------------------------------------------------------------------------- seam: run_liveness


NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)


def liveness(key: str, env: Dict[str, str]) -> str:
    from claude_kiro.progress import run_liveness

    return run_liveness(key, env, NOW)


@pytest.fixture
def envs(tmp_path):
    base = {k: v for k, v in os.environ.items() if k not in ("CK_PROGRESS_DIR", "CK_DATA_HOME")}
    data = {**base, "XDG_DATA_HOME": str(tmp_path / "xdg")}
    spool = {**data, "CK_PROGRESS_DIR": str(tmp_path / "spool")}
    return data, spool


def test_tc145_redteam_seam_liveness_boundary_at_the_stale_window(envs):
    data, _ = envs
    record("k119", data, age_min=119, now=NOW)
    record("k121", data, age_min=121, now=NOW)
    record("kend", data, age_min=1, end=True, now=NOW)
    assert liveness("k119", data) == "running"
    assert liveness("k121", data) == "stale"
    assert liveness("kend", data) == "finished"


def test_tc145_redteam_seam_liveness_unknown_keys_are_missing(envs):
    data, _ = envs
    record("demo--20261108T000000Z-abc123", data, age_min=1, now=NOW)
    assert liveness("demo--20261108T000000Z-abc123", data) == "running"
    for key in ("demo--20261108T000000Z-abc12", "demo--*", "demo--20261108T000000Z-abc12?", "DEMO--20261108T000000Z-ABC123",
                "demo--20261108T000000Z-abc123x", "", "../x", "..", ".hidden", "a/b", "a\\b", "a\x00b"):
        assert liveness(key, data) == "missing", key


def test_tc145_redteam_seam_liveness_future_events_are_running(envs):
    data, _ = envs
    record("future", data, age_min=-600, now=NOW)  # ten hours after "now"
    assert liveness("future", data) == "running"


def test_tc145_redteam_seam_liveness_unicode_and_spaced_keys(envs):
    data, _ = envs
    for key in UNUSUAL_KEYS:
        record(key, data, age_min=1, now=NOW)
        assert liveness(key, data) == "running", key
        record(key + "-gone", data, age_min=1, end=True, now=NOW)
        assert liveness(key + "-gone", data) == "finished", key


def test_tc145_redteam_seam_liveness_reads_every_copy(envs):
    data, spool = envs
    # live spool copy, data copy with an end: finished
    record("forged", spool, age_min=1, now=NOW)
    record("forged", data, age_min=1, end=True, now=NOW)
    assert liveness("forged", spool) == "finished"
    # the same, the other way round
    record("forged2", spool, age_min=1, end=True, now=NOW)
    record("forged2", data, age_min=1, now=NOW)
    assert liveness("forged2", spool) == "finished"
    # newest event across copies decides running vs stale
    record("spool-new", spool, age_min=2, now=NOW)
    record("spool-new", data, age_min=200, now=NOW)
    record("data-new", spool, age_min=200, now=NOW)
    record("data-new", data, age_min=2, now=NOW)
    record("both-old", spool, age_min=200, now=NOW)
    record("both-old", data, age_min=300, now=NOW)
    assert liveness("spool-new", spool) == "running"
    assert liveness("data-new", spool) == "running"
    assert liveness("both-old", spool) == "stale"
    # one copy only: no false "missing"
    record("spool-only", spool, age_min=2, now=NOW)
    assert liveness("spool-only", spool) == "running"
    record("data-only", data, age_min=2, now=NOW)
    assert liveness("data-only", spool) == "running"


def test_tc145_redteam_seam_liveness_torn_and_quoted_end_lines_are_not_ends(envs):
    from claude_kiro import progress

    data, _ = envs
    record("torn", data, age_min=1, now=NOW)
    path = progress.run_file("torn", data)
    with path.open("a") as fh:
        fh.write('{"schema":"ck-progress/1","kind":"end","outcome":"comp')
    assert liveness("torn", data) == "running"
    record("quoted", data, age_min=1, now=NOW)
    err = progress.append("quoted", {"kind": "task-failed", "task": "1", "reason": '{"kind":"end"}', "at": iso(NOW)}, env=data)
    assert err is None, err
    assert liveness("quoted", data) == "running"


# --------------------------------------------------------------------------- seam: claim_release_decision


def decide(run, liveness_, age_s, stale_minutes=60.0, starting=STARTING):
    from claude_kiro.worktrees import ClaimInfo, claim_release_decision

    claim_ = ClaimInfo(owner="agent", token="tok", at=GENUINE_TIME, run=run)
    return claim_release_decision(claim_, starting, liveness_, age_s, stale_minutes)


@pytest.mark.parametrize("age_s", [0.0, 59.0 * 60, 61.0 * 60, 5 * 3600.0, 1e12, float("inf")])
def test_tc145_redteam_seam_decision_running_run_is_kept_at_any_age(age_s):
    assert decide("demo--A", "running", age_s) == "keep"
    assert decide("demo--A", "running", age_s, stale_minutes=1.0) == "keep"


@pytest.mark.parametrize("run", ["demo--B ", "demo--b", " demo--B", "demo--B\n", "demo--B​", "demo--"])
def test_tc145_redteam_seam_decision_near_miss_of_the_starting_run_is_not_own(run):
    assert decide(run, "running", 0.0) == "keep"
    assert decide(run, "finished", 0.0) == "release"
    assert decide(run, "missing", 0.0) == "release"


@pytest.mark.parametrize("liveness_", ["finished", "stale", "missing", None])
def test_tc145_redteam_seam_decision_not_running_releases_even_a_fresh_claim(liveness_):
    assert decide("demo--A", liveness_, 0.0) == "release"
    assert decide("demo--A", liveness_, float("inf")) == "release"


def test_tc145_redteam_seam_decision_without_a_starting_run_a_live_claim_is_kept():
    assert decide("demo--A", "running", 0.0, starting=None) == "keep"
    assert decide("demo--A", "finished", 0.0, starting=None) == "release"
