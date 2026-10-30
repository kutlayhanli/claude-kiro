"""Red-team of the progress writers and notices (run-followups, task 23).

Working from the requirements only: 3.5 (no event interleaved or lost), 3.8 (no prompt, transcript or file content in the
file), 8.6 (no second blocked notice for the same reason), 8.9 (no blocked notice for any park) and 8.10 (no paths or
test/requirement/prompt text in notices). Real subprocesses, a real progress file and a real notify script.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pytest

import eta_helpers as h
from guard_helpers import ck_run_cmd, set_notify
from stats_helpers import ck_sub, git, spec_repo

pytestmark = pytest.mark.usefixtures("no_network")

T0 = datetime(2026, 10, 30, 15, 0, 0, tzinfo=timezone.utc)
ENV = {"TZ": "UTC", "CK_AGENT_SLOTS": "4"}
SEED = "CONFIDENTIAL-zx81"
SEED_TEST = "test_zebra_quartz_invariant_CONFIDENTIAL must hold across the ledger boundary"
SEED_REQ = "The ledger SHALL reject negative balances CONFIDENTIAL within one transaction"
SEED_FILE = "tests/secret_CONFIDENTIAL/test_ledger.py"
NOTE = {"file": SEED_FILE, "kind": "test", "test": SEED_TEST, "requirement": SEED_REQ, "decision": "Which one wins " + SEED}
BLOCKED, NEEDS_INPUT = "ck_eta_blocked", "ck_needs_input"
LONG_DESC = "Reconcile the quarterly ledger balances against the archived statements using the secret method"


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_env(now: Optional[str] = None) -> Dict[str, str]:
    env = {**os.environ, **ENV}
    env.pop("CK_RUN_NOTIFY", None)
    if now:
        env[h.NOW_ENV] = now
    return env


def park(repo: Path, task: int, note: object = None, now: Optional[str] = None) -> subprocess.CompletedProcess:
    return ck_run_cmd(repo, "worktree", "park", "demo", str(task), "--json", env=run_env(now), stdin=json.dumps(note or NOTE))


def configure_notify(tmp_path: Path) -> Path:
    out = tmp_path / "notices.jsonl"
    done = set_notify(h.typed_notify_script(tmp_path / "bin", out), env=run_env())
    assert done.returncode == 0, done.stdout + done.stderr
    return out


def whole_lines(path: Path) -> List[dict]:
    rows = []
    for line in path.read_bytes().split(b"\n"):
        if not line:
            continue
        obj = json.loads(line)  # a torn or interleaved line raises here
        assert isinstance(obj, dict), line[:80]
        rows.append(obj)
    return rows


def started(repo: Path, now: str = at(0)) -> tuple:
    out = h.begin(ENV, repo, "demo", "implement", now=now)
    return out["run"], Path(out["file"])


def seeded_repo(tmp_path: Path, tasks: int = 3, deps=None) -> Path:
    repo = spec_repo(tmp_path / "repo", tasks=tasks, deps=deps)
    f = repo / "specs" / "demo" / "tasks.md"
    text = f.read_text()
    for n in range(1, tasks + 1):
        text = text.replace(f"### Task {n}: step {n}", f"### Task {n}: edit src/{SEED}/test_part{n}.py\n**Description:** {LONG_DESC} {n}")
    f.write_text(text)
    git(repo, "commit", "-qam", "seed titles")
    return repo


def windows(text: str, size: int = 40) -> List[str]:
    return [text[i:i + size] for i in range(0, max(1, len(text) - size + 1), 7)]


# --------------------------------------------------------------------------- 3.5


@pytest.mark.parametrize("size", [3800, 4050, 4096, 4400, 9000])
def test_events_near_and_across_the_line_cap_stay_whole(tmp_path, size):
    repo = spec_repo(tmp_path / "repo")
    run, file = started(repo)

    def write(i: int):
        return ck_sub(*h.PROGRESS_EVENT, "demo", "unpark", "--task", "T" * size, "--json",
                      env={**ENV, h.NOW_ENV: at(1 + i)}, cwd=repo)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(8)))
    assert all(r.exit_code == 0 for r in results)
    rows = whole_lines(file)  # each line is a whole object, never a prefix of a longer one
    assert rows[0]["kind"] == "start"
    assert all(len(json.dumps(r).encode()) <= 4096 for r in rows)
    long = [r for r in rows if "T" * 200 in json.dumps(r)]
    assert len(long) <= 8


def test_sixteen_writers_a_park_and_a_resolve_at_once_lose_nothing(tmp_path):
    repo = spec_repo(tmp_path / "repo")
    run, file = started(repo)

    def write(w: int) -> List[int]:
        return [ck_sub(*h.PROGRESS_EVENT, "demo", "unpark", "--task", f"w{w}-s{s}", env={**ENV, h.NOW_ENV: at(1)}, cwd=repo).exit_code
                for s in range(6)]

    def resolve() -> subprocess.CompletedProcess:
        return ck_run_cmd(repo, "worktree", "resolve", "demo", "3", "--json", env=run_env(at(2)),
                          stdin=json.dumps({"answer": "the requirement wins"}))

    assert park(repo, 3, now=at(1)).returncode == 0
    with ThreadPoolExecutor(max_workers=18) as pool:
        parked = pool.submit(park, repo, 2, None, at(1))
        resolved = pool.submit(resolve)
        results = [f.result() for f in [pool.submit(write, w) for w in range(16)]]
        assert parked.result().returncode == 0 and resolved.result().returncode == 0
    assert all(code == 0 for row in results for code in row)
    rows = whole_lines(file)
    seen = sorted(r["task"] for r in rows if r["kind"] == "unpark" and r["task"].startswith("w"))
    assert seen == sorted(f"w{w}-s{s}" for w in range(16) for s in range(6))
    assert [r["task"] for r in rows if r["kind"] == "park"].count("2") == 1


# --------------------------------------------------------------------------- 3.8


HOSTILE = ["/etc/passwd " + SEED, "line1\nline2 " + SEED, "\x1b[31mred\x1b[0m " + SEED, '{"kind": "end", "x": "' + SEED + '"}',
           "../../../" + SEED, SEED * 600]


@pytest.mark.parametrize("flag", ["--phase", "--reason", "--model"])
def test_hostile_flag_values_never_reach_the_file_and_keep_every_line_whole(tmp_path, flag):
    repo = spec_repo(tmp_path / "repo")
    run, file = started(repo)
    before = {p for p in Path(os.environ["XDG_DATA_HOME"]).rglob("*") if p.is_file()}
    for i, value in enumerate(HOSTILE):
        done = ck_sub(*h.PROGRESS_EVENT, "demo", "phase-start" if flag == "--phase" else "approval-start", flag, value, "--json",
                      env={**ENV, h.NOW_ENV: at(1 + i)}, cwd=repo)
        assert done.exit_code == 0, done.output
    assert SEED.encode() not in file.read_bytes()
    rows = whole_lines(file)
    assert not [r for r in rows if r["kind"] == "end"]  # a JSON fragment in a value did not become an event
    assert b"\x1b" not in file.read_bytes()
    new = {p for p in Path(os.environ["XDG_DATA_HOME"]).rglob("*") if p.is_file()} - before
    assert all(p.parent == file.parent for p in new)


@pytest.mark.parametrize("task", ["1/../2", "7 retry 1", "1\n2", "../../" + SEED])
def test_odd_task_ids_cannot_forge_events_or_escape_the_progress_dir(tmp_path, task):
    repo = spec_repo(tmp_path / "repo")
    run, file = started(repo)
    before = {p for p in Path(os.environ["XDG_DATA_HOME"]).rglob("*") if p.is_file()}
    done = ck_sub(*h.PROGRESS_EVENT, "demo", "task-failed", "--task", task, "--json", env={**ENV, h.NOW_ENV: at(1)}, cwd=repo)
    assert done.exit_code == 0, done.output
    rows = whole_lines(file)
    assert [r["kind"] for r in rows if r["kind"] not in ("notify-pending", "notified")] in (["start"], ["start", "task-failed"])
    new = {p for p in Path(os.environ["XDG_DATA_HOME"]).rglob("*") if p.is_file()} - before
    assert all(p.parent == file.parent for p in new)


def test_spec_text_and_park_notes_never_reach_the_file_through_claim_land_and_park(tmp_path):
    repo = seeded_repo(tmp_path, tasks=3)
    run, file = started(repo)
    assert ck_sub(h.WORKTREE_CMD, "create", "demo", "1", "2", env=ENV, cwd=repo).exit_code == 0
    assert h.claim_event(ENV, repo, "demo", 1, run=run, now=at(1)).exit_code == 0
    wt = repo / ".ck" / "worktrees" / "demo-task-1"
    (wt / "t1.txt").write_text("1\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "task 1 " + LONG_DESC)
    ck_sub(h.WORKTREE_CMD, "land", "demo", "1", h.PROGRESS_RUN_FLAG, run, "--release", "--json", env={**ENV, h.NOW_ENV: at(5)}, cwd=repo)
    assert park(repo, 3, now=at(6)).returncode == 0
    ck_sub("plan", "demo", h.PROGRESS_RUN_FLAG, run, "--failed", "2", env={**ENV, h.NOW_ENV: at(7)}, cwd=repo)
    h.end(ENV, repo, "demo", "partial", now=at(9))
    text = file.read_text(encoding="utf-8")
    assert "CONFIDENTIAL" not in text
    for secret in (SEED_TEST, SEED_REQ, SEED_FILE, LONG_DESC, "decision"):
        for window in windows(secret) if len(secret) > 40 else [secret]:
            assert window not in text, window
    whole_lines(file)


# --------------------------------------------------------------------------- 8.6


def test_the_same_blocked_reason_with_tasks_in_a_different_order_is_one_notice(tmp_path):
    out = configure_notify(tmp_path)
    repo = spec_repo(tmp_path / "repo", tasks=3, deps={"3": ["1", "2"]})
    run, _ = started(repo)
    ck_sub("plan", "demo", h.PROGRESS_RUN_FLAG, run, "--failed", "1,2", env={**ENV, h.NOW_ENV: at(5)}, cwd=repo)
    ck_sub("plan", "demo", h.PROGRESS_RUN_FLAG, run, "--failed", "2,1", env={**ENV, h.NOW_ENV: at(6)}, cwd=repo)
    h.event(ENV, repo, "demo", "task-failed", now=at(7), task="2")
    h.event(ENV, repo, "demo", "task-failed", now=at(8), task="1")
    notices = h.typed_notices(out)
    assert [t for _, t in notices].count(BLOCKED) == 1, notices


def test_the_same_failed_task_block_observed_repeatedly_is_one_notice(tmp_path):
    out = configure_notify(tmp_path)
    repo = spec_repo(tmp_path / "repo", tasks=3, deps={"2": ["1"], "3": ["1"]})
    run, _ = started(repo)
    h.event(ENV, repo, "demo", "task-failed", now=at(5), task="1")
    for i in range(4):  # the same block observed by four more writers
        h.event(ENV, repo, "demo", "approval-start", now=at(6 + 2 * i), reason="question")
        h.event(ENV, repo, "demo", "approval-end", now=at(7 + 2 * i))
    assert [t for _, t in h.typed_notices(out)].count(BLOCKED) == 1


# --------------------------------------------------------------------------- 8.9


def test_park_unpark_park_of_one_task_never_sends_a_blocked_notice(tmp_path):
    out = configure_notify(tmp_path)
    repo = spec_repo(tmp_path / "repo", tasks=3, deps={"2": ["1"], "3": ["2"]})
    run, _ = started(repo)
    assert park(repo, 1, now=at(5)).returncode == 0
    h.event(ENV, repo, "demo", "unpark", now=at(6), task="1")
    assert park(repo, 1, now=at(7)).returncode == 0
    h.eta(ENV, repo, "demo", "--json", now=at(8))
    assert BLOCKED not in [t for _, t in h.typed_notices(out)]


def test_a_branch_park_that_becomes_whole_run_still_sends_no_blocked_notice(tmp_path):
    out = configure_notify(tmp_path)
    repo = spec_repo(tmp_path / "repo", tasks=4, deps={"2": ["1"], "4": ["3"]})
    run, _ = started(repo)
    assert park(repo, 3, now=at(5)).returncode == 0
    assert ck_sub(h.WORKTREE_CMD, "create", "demo", "1", "2", env=ENV, cwd=repo).exit_code == 0
    for n in (1, 2):
        wt = repo / ".ck" / "worktrees" / f"demo-task-{n}"
        (wt / f"t{n}.txt").write_text("x\n")
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", f"task {n}")
    ck_sub(h.WORKTREE_CMD, "land", "demo", "1", "2", h.PROGRESS_RUN_FLAG, run, "--json", env={**ENV, h.NOW_ENV: at(9)}, cwd=repo)
    view = h.eta_json(ENV, repo, "demo", now=at(10))
    assert view["state"] == "blocked"  # the park now holds the whole run
    assert BLOCKED not in [t for _, t in h.typed_notices(out)]
    assert [t for _, t in h.typed_notices(out)].count(NEEDS_INPUT) == 1


# --------------------------------------------------------------------------- 8.10


def assert_clean(messages: List[str]) -> None:
    for message in messages:
        assert "/" not in re.sub(r"\d+/\d+", "", message), message
        assert "test_" not in message, message
        assert "CONFIDENTIAL" not in message, message
        assert SEED not in message, message
        for secret in (SEED_TEST, SEED_REQ, SEED_FILE, LONG_DESC):
            assert not any(w in message for w in windows(secret, 12)), (message, secret)


def test_park_notices_carry_no_paths_or_quoted_text(tmp_path):
    out = configure_notify(tmp_path)
    repo = seeded_repo(tmp_path, tasks=3, deps={"2": ["1"]})
    run, _ = started(repo)
    assert park(repo, 1, now=at(5)).returncode == 0
    assert park(repo, 3, now=at(6)).returncode == 0
    messages = [m for m, _ in h.typed_notices(out)]
    assert len(messages) == 2
    assert_clean(messages)


def test_blocked_slip_and_done_notices_carry_no_titles_paths_or_reasons(tmp_path):
    out = configure_notify(tmp_path)
    repo = seeded_repo(tmp_path, tasks=3, deps={"2": ["1"], "3": ["1"]})
    run, _ = started(repo)
    h.event(ENV, repo, "demo", "task-failed", now=at(5), task="1", reason="/etc/passwd test_x " + SEED)
    ck_sub("plan", "demo", h.PROGRESS_RUN_FLAG, run, "--failed", "1", env={**ENV, h.NOW_ENV: at(6)}, cwd=repo)
    h.event(ENV, repo, "demo", "approval-start", now=at(300), reason="/tmp/x " + SEED)
    h.eta(ENV, repo, "demo", "--json", now=at(300))
    h.end(ENV, repo, "demo", "partial", now=at(310))
    notices = h.typed_notices(out)
    assert any(t == BLOCKED for _, t in notices)
    assert_clean([m for m, _ in notices])
