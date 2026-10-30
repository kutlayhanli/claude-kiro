"""The run progress file through its real writers (run-followups, task 23).

TC-21..TC-24, TC-27..TC-31 (CLI part of TC-29) and TC-88: real `ck progress`, `ck worktree claim/land/park/resolve/report`
and `ck plan` subprocesses on real git repos, real concurrent processes, real file modes and real file bytes.
Project modules are never imported at module level, so this file collects before the code exists.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pytest

import eta_helpers as h
from eta_strategies import CREATE_PHASES, PROGRESS_SCHEMA, progress_lines
from guard_helpers import CK, ck_run_cmd
from stats_helpers import ck_sub, git, spec_repo

pytestmark = pytest.mark.usefixtures("no_network")

needs_user = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")

T0 = datetime(2026, 10, 30, 15, 0, 0, tzinfo=timezone.utc)
NOTE = {"file": "tests/test_ledger.py", "kind": "test", "test": "test_zebra_quartz_invariant must hold",
        "requirement": "The ledger SHALL reject negative balances", "decision": "Which one wins?"}


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def full_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {**os.environ, "TZ": "UTC", **(extra or {})}
    env.pop("CK_RUN_NOTIFY", None)
    return env


@pytest.fixture
def env() -> Dict[str, str]:
    return {"TZ": "UTC"}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return spec_repo(tmp_path / "repo")


def data_dir() -> Path:
    return Path(os.environ["XDG_DATA_HOME"]) / "claude-kiro"


def worktree(repo: Path, *args: str, env: Optional[Dict[str, str]] = None, now: Optional[str] = None):
    e = {"TZ": "UTC", **(env or {})}
    if now:
        e[h.NOW_ENV] = now
    return ck_sub(h.WORKTREE_CMD, *args, env=e, cwd=repo)


def create_worktrees(repo: Path, *tasks: int) -> None:
    done = worktree(repo, "create", "demo", *[str(t) for t in tasks])
    assert done.exit_code == 0, done.output


def commit_in_task(repo: Path, task: int, rel: str = "", content: str = "x\n") -> Path:
    wt = repo / ".ck" / "worktrees" / f"demo-task-{task}"
    target = wt / (rel or f"t{task}.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", f"task {task}")
    return wt


def park(repo: Path, task: int | str, note: object = NOTE, now: Optional[str] = None) -> subprocess.CompletedProcess:
    extra = {h.NOW_ENV: now} if now else None
    return ck_run_cmd(repo, "worktree", "park", "demo", str(task), "--json", env=full_env(extra), stdin=json.dumps(note))


def kinds(path: Path, drop=("notify-pending", "notified")) -> List[str]:
    return [e["kind"] for e in progress_lines(path) if e.get("kind") not in drop]


def start_run(env, repo, kind="implement", now=None) -> tuple:
    out = h.begin(env, repo, "demo", kind, now=now or at(0))
    return out["run"], Path(out["file"])


# --------------------------------------------------------------------------- TC-21, TC-88


def test_begin_writes_a_private_start_event_outside_the_repo(env, repo):
    out = h.begin(env, repo, "demo", "implement", now=at(0))
    run, file = out["run"], Path(out["file"])
    assert run and file.is_file()
    assert file.resolve().is_relative_to((data_dir()).resolve())
    assert not file.resolve().is_relative_to(repo.resolve())
    assert mode(file) == 0o600 and mode(file.parent) == 0o700
    model = file.parent / f"{run}.model.json"
    assert model.is_file() and mode(model) == 0o600
    first = progress_lines(file)[0]
    assert first["kind"] == "start" and first["schema"] == PROGRESS_SCHEMA
    assert first["runKind"] == "implement" and first["spec"] == "demo"
    assert first["at"] == at(0)
    assert first["slots"] == 4
    assert len(first["launch"]["finish"]) == 3


def test_eta_sidecar_is_private_too(env, repo):
    run, file = start_run(env, repo)
    view = h.eta_json(env, repo, "demo", now=at(1))
    assert view["progressFile"] == str(file)
    eta_side = file.parent / f"{run}.eta.json"
    assert eta_side.is_file() and mode(eta_side) == 0o600
    assert mode(file.parent) == 0o700


# --------------------------------------------------------------------------- TC-22


def test_events_come_from_their_real_writers_in_order(env, repo):
    run, file = start_run(env, repo)
    create_worktrees(repo, 1, 2, 3)

    assert h.claim_event(env, repo, "demo", 1, run=run, kind="task-start", model="sonnet", attempt=1, now=at(1)).exit_code == 0
    assert h.claim_event(env, repo, "demo", 2, run=run, kind="task-start", model="sonnet", attempt=1, now=at(2)).exit_code == 0
    worktree(repo, "release", "demo", "2")
    assert h.claim_event(env, repo, "demo", 2, run=run, kind="retry", model="sonnet", attempt=2, now=at(5)).exit_code == 0
    worktree(repo, "release", "demo", "2")
    assert h.claim_event(env, repo, "demo", 2, run=run, kind="escalation", model="opus", attempt=3, now=at(8)).exit_code == 0
    worktree(repo, "release", "demo", "2")
    worktree(repo, "release", "demo", "1")

    commit_in_task(repo, 1)
    landed = worktree(repo, "land", "demo", "1", "--progress-run", run, "--json", now=at(10))
    assert landed.exit_code == 0, landed.output
    assert park(repo, 3, now=at(11)).returncode == 0
    assert h.event(env, repo, "demo", "approval-start", now=at(12), reason="question").exit_code == 0
    assert h.event(env, repo, "demo", "approval-end", now=at(13)).exit_code == 0
    assert h.end(env, repo, "demo", "completed", now=at(20)).exit_code == 0

    assert kinds(file) == ["start", "task-start", "task-start", "retry", "escalation", "landing", "merge", "park",
                           "approval-start", "approval-end", "end"]
    rows = [e for e in progress_lines(file) if e["kind"] not in ("notify-pending", "notified")]
    assert all(e["at"] for e in rows)
    assert [e["task"] for e in rows if e["kind"] in ("task-start", "retry", "escalation")] == ["1", "2", "2", "2"]
    assert next(e for e in rows if e["kind"] == "escalation")["model"] == "opus"
    landing = next(e for e in rows if e["kind"] == "landing")
    assert landing["tasks"] == ["1"] and landing["merged"] == ["1"] and landing["red"] == []
    assert next(e for e in rows if e["kind"] == "merge")["task"] == "1"
    assert next(e for e in rows if e["kind"] == "park")["task"] == "3"
    assert rows[-1]["outcome"] == "completed"


def test_a_claim_that_reports_missing_appends_nothing(env, repo):
    run, file = start_run(env, repo)
    before = file.read_bytes()
    done = h.claim_event(env, repo, "demo", 4, run=run, now=at(1))
    assert "MISSING" in done.output
    assert file.read_bytes() == before


def test_create_then_claim_again_gives_exactly_one_start_event(env, repo):
    run, file = start_run(env, repo)
    assert "MISSING" in h.claim_event(env, repo, "demo", 4, run=run, now=at(1)).output
    create_worktrees(repo, 4)
    assert "CLAIMED" in h.claim_event(env, repo, "demo", 4, run=run, now=at(2)).output
    starts = [e for e in progress_lines(file) if e["kind"] == "task-start"]
    assert [e["task"] for e in starts] == ["4"]


def test_a_busy_claim_appends_nothing(env, repo):
    run, file = start_run(env, repo)
    create_worktrees(repo, 4)
    assert "CLAIMED" in h.claim_event(env, repo, "demo", 4, run=run, now=at(1)).output
    before = file.read_bytes()
    again = h.claim_event(env, repo, "demo", 4, run=run, now=at(2))
    assert "BUSY" in again.output
    assert file.read_bytes() == before


def test_a_protected_landing_appends_hand_landing_and_the_run_is_waiting_not_blocked(env, repo):
    run, file = start_run(env, repo)
    create_worktrees(repo, 2)
    commit_in_task(repo, 2, ".claude/settings.json", "{}\n")
    landed = worktree(repo, "land", "demo", "2", "--progress-run", run, "--json", now=at(10))
    assert landed.exit_code == 3, landed.output
    rows = progress_lines(file)
    hand = [e for e in rows if e["kind"] == "hand-landing"]
    assert [e["task"] for e in hand] == ["2"]
    assert next(e for e in rows if e["kind"] == "landing")["protected"] == ["2"]
    assert "merge" not in kinds(file)
    view = h.eta_json(env, repo, "demo", now=at(15))
    assert view["state"] == "waiting" and view["blocked"] is None


def test_task_failed_comes_from_plan_failed(env, repo):
    run, file = start_run(env, repo)
    done = ck_sub("plan", "demo", "--progress-run", run, "--failed", "1", env={"TZ": "UTC", h.NOW_ENV: at(5)}, cwd=repo)
    assert done.exit_code == 0, done.output
    failed = [e for e in progress_lines(file) if e["kind"] == "task-failed"]
    assert [e["task"] for e in failed] == ["1"]
    again = ck_sub("plan", "demo", "--progress-run", run, "--failed", "1", env={"TZ": "UTC", h.NOW_ENV: at(6)}, cwd=repo)
    assert again.exit_code == 0
    assert len([e for e in progress_lines(file) if e["kind"] == "task-failed"]) == 1  # not yet recorded as failed only


# --------------------------------------------------------------------------- TC-23


def test_a_create_run_writes_a_phase_start_and_step_end_per_phase_then_end(env, repo):
    run, file = start_run(env, repo, kind="create")
    phases = CREATE_PHASES[:3]
    for i, phase in enumerate(phases):
        assert h.event(env, repo, "demo", "phase-start", now=at(1 + 2 * i), phase=phase).exit_code == 0
        assert h.event(env, repo, "demo", "step-end", now=at(2 + 2 * i), phase=phase).exit_code == 0
    assert h.end(env, repo, "demo", "completed", now=at(10)).exit_code == 0
    rows = [e for e in progress_lines(file) if e["kind"] not in ("notify-pending", "notified")]
    assert rows[0]["kind"] == "start" and rows[0]["runKind"] == "create"
    assert rows[-1]["kind"] == "end"
    body = rows[1:-1]
    assert [e["phase"] for e in body] == [p for p in phases for _ in range(2)]
    assert [e["kind"] for e in body[0::2]] == ["phase-start"] * 3
    assert all(e["kind"] in ("phase-end", "step-end") for e in body[1::2])


# --------------------------------------------------------------------------- TC-24


def needs_input(repo: Path) -> List[str]:
    done = ck_run_cmd(repo, "worktree", "report", "demo", "--json", env=full_env())
    assert done.returncode == 0, done.stdout + done.stderr
    return [str(e["task"]) for e in json.loads(done.stdout)["needsInput"]]


def test_park_appends_park_and_resolve_appends_unpark(env, repo):
    run, file = start_run(env, repo)
    assert park(repo, 3, now=at(5)).returncode == 0
    parked = [e for e in progress_lines(file) if e["kind"] == "park"]
    assert [e["task"] for e in parked] == ["3"]
    assert needs_input(repo) == ["3"]
    done = ck_run_cmd(repo, "worktree", "resolve", "demo", "3", "--json", env=full_env({h.NOW_ENV: at(8)}),
                      stdin=json.dumps({"answer": "the requirement wins"}))
    assert done.returncode == 0, done.stdout + done.stderr
    assert [e["task"] for e in progress_lines(file) if e["kind"] == "unpark"] == ["3"]
    assert kinds(file)[-2:] == ["park", "unpark"]


def test_a_fresh_claim_appends_unpark(env, repo):
    run, file = start_run(env, repo)
    create_worktrees(repo, 3)
    assert park(repo, 3, now=at(5)).returncode == 0
    assert "CLAIMED" in worktree(repo, "claim", "demo", "3", now=at(9)).output
    assert [e["task"] for e in progress_lines(file) if e["kind"] == "unpark"] == ["3"]


# --------------------------------------------------------------------------- TC-27


@pytest.mark.parametrize("writers", [2, 8])
def test_concurrent_writers_and_a_park_lose_and_interleave_nothing(env, repo, writers):
    run, file = start_run(env, repo)

    def write(w: int) -> List[int]:
        return [ck_sub(*h.PROGRESS_EVENT, "demo", "unpark", "--task", f"w{w}-s{s}", env={"TZ": "UTC", h.NOW_ENV: at(1)}, cwd=repo).exit_code
                for s in range(50)]

    with ThreadPoolExecutor(max_workers=writers + 1) as pool:
        parked = pool.submit(park, repo, 3)
        results = [f.result() for f in [pool.submit(write, w) for w in range(writers)]]
        assert parked.result().returncode == 0
    assert all(code == 0 for row in results for code in row)

    raw = file.read_text(encoding="utf-8").splitlines()
    parsed = []
    for line in raw:
        obj = json.loads(line)  # a torn or interleaved line fails here
        assert isinstance(obj, dict)
        parsed.append(obj)
    seen = [e["task"] for e in parsed if e["kind"] == "unpark"]
    assert sorted(seen) == sorted(f"w{w}-s{s}" for w in range(writers) for s in range(50))
    assert len(seen) == len(set(seen))
    assert len([e for e in parsed if e["kind"] == "park"]) == 1
    assert parsed[0]["kind"] == "start"


# --------------------------------------------------------------------------- TC-28


def test_garbage_lines_leave_the_eta_unchanged(env, repo):
    run, file = start_run(env, repo)
    create_worktrees(repo, 1)
    assert "CLAIMED" in h.claim_event(env, repo, "demo", 1, run=run, now=at(1)).output

    def view() -> dict:
        v = h.eta_json(env, repo, "demo", "--seed", "4", now=at(10))
        return {k: v[k] for k in ("state", "open", "merged", "running", "waiting", "finish", "tasks", "remainingMinutes")}

    clean = view()
    lines = file.read_bytes().splitlines(keepends=True)
    garbage = [b'{"schema": "ck-progress/1", "kind": "task-st\n', b"[]\n", b'"text"\n',
               json.dumps({"schema": PROGRESS_SCHEMA, "at": at(2), "kind": "warp"}).encode() + b"\n",
               b"\x00\x00\x00\n", b"\n", b'{"k": "' + b"x" * 10240 + b'"}\n']
    mixed = lines[:1] + garbage[:3] + lines[1:] + garbage[3:]
    file.write_bytes(b"".join(mixed))
    assert view() == clean


# --------------------------------------------------------------------------- TC-29(a)


@needs_user
def test_an_unwritable_progress_dir_never_fails_the_caller(env, repo):
    run, file = start_run(env, repo)
    create_worktrees(repo, 1, 2)
    file.parent.chmod(0o500)
    try:
        ev = ck_sub(*h.PROGRESS_EVENT, "demo", "approval-start", "--reason", "question", "--json",
                    env={"TZ": "UTC", h.NOW_ENV: at(1)}, cwd=repo)
        assert ev.exit_code == 0, ev.output
        assert json.loads(ev.stdout)["ok"] is False
        claim = h.claim_event(env, repo, "demo", 1, run=run, now=at(2))
        assert claim.exit_code == 0 and "CLAIMED" in claim.output
        commit_in_task(repo, 2)
        landed = worktree(repo, "land", "demo", "2", "--progress-run", run, "--json", now=at(3))
        assert landed.exit_code == 0, landed.output
        assert json.loads(landed.stdout)["tasks"][0]["state"] == "MERGED"
        ended = h.end(env, repo, "demo", "completed", now=at(4))
        assert ended.exit_code == 0
        assert ended.output.count("⚠ progress:") == 1
        assert "event(s) not written" in ended.output
    finally:
        file.parent.chmod(0o700)


def test_a_progress_dir_that_is_a_file_never_fails_the_caller(env, repo, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    bad = {"TZ": "UTC", h.PROGRESS_DIR_ENV: str(blocker), h.NOW_ENV: at(1)}
    ev = ck_sub(*h.PROGRESS_EVENT, "demo", "approval-start", "--reason", "question", "--json", env=bad, cwd=repo)
    assert ev.exit_code == 0, ev.output
    assert json.loads(ev.stdout)["ok"] is False
    create_worktrees(repo, 1)
    claim = ck_sub(h.WORKTREE_CMD, "claim", "demo", "1", h.PROGRESS_RUN_FLAG, "k", h.PROGRESS_EVENT_FLAG, "task-start", env=bad, cwd=repo)
    assert claim.exit_code == 0 and "CLAIMED" in claim.output
    commit_in_task(repo, 1)
    landed = ck_sub(h.WORKTREE_CMD, "land", "demo", "1", h.PROGRESS_RUN_FLAG, "k", "--release", "--json", env=bad, cwd=repo)
    assert landed.exit_code == 0, landed.output
    assert json.loads(landed.stdout)["tasks"][0]["state"] == "MERGED"
    ended = ck_sub(*h.PROGRESS_END, "demo", "--outcome", "completed", env=bad, cwd=repo)
    assert ended.exit_code == 0


# --------------------------------------------------------------------------- TC-30


def test_extra_fields_never_reach_the_file_bytes(env, repo):
    run, file = start_run(env, repo)
    h.append_events(os.environ.copy(), run, [
        {"schema": PROGRESS_SCHEMA, "at": at(1), "kind": "task-start", "task": "1", "prompt": "CONFIDENTIAL-prompt",
         "transcript": "CONFIDENTIAL-transcript", "contents": "CONFIDENTIAL-contents"}])
    for flag in ("--prompt", "--transcript", "--contents", "--data"):
        ck_sub(*h.PROGRESS_EVENT, "demo", "unpark", "--task", "1", flag, "CONFIDENTIAL-cli", env={"TZ": "UTC"}, cwd=repo)
    assert b"CONFIDENTIAL" not in file.read_bytes()
    assert any(e["kind"] == "task-start" for e in progress_lines(file))


# --------------------------------------------------------------------------- TC-31


def test_begin_end_begin_leaves_two_files_and_the_first_untouched(env, repo):
    first = h.begin(env, repo, "demo", "implement", now=at(0))
    assert h.end(env, repo, "demo", "completed", now=at(5)).exit_code == 0
    before = Path(first["file"]).read_bytes()
    second = h.begin(env, repo, "demo", "implement", now=at(10))
    assert second["file"] != first["file"]
    assert Path(first["file"]).read_bytes() == before
    assert progress_lines(first["file"])[-1]["kind"] in ("end", "notified")
    assert len(list(Path(first["file"]).parent.glob("*.jsonl"))) == 2
    assert h.progress_file_of(env, "demo", repo) == Path(second["file"])


def test_begin_while_unfinished_supersedes_the_old_run(env, repo):
    first = h.begin(env, repo, "demo", "implement", now=at(0))
    second = h.begin(env, repo, "demo", "implement", now=at(40))
    ends = [e for e in progress_lines(first["file"]) if e["kind"] == "end"]
    assert [e["outcome"] for e in ends] == ["superseded"]
    assert progress_lines(second["file"])[0]["kind"] == "start"
    assert len(list(Path(first["file"]).parent.glob("*.jsonl"))) == 2
