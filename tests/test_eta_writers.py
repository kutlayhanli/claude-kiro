"""Landing ETA lines and notifications through the real writers (run-followups, task 23).

TC-53(b), TC-58 and TC-103: real `ck worktree land/park`, `ck plan` and `ck progress` subprocesses, a real progress file
and a real notify script. Project modules are never imported at module level.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

import eta_helpers as h
from guard_helpers import ck_run_cmd, set_notify
from stats_helpers import ck_sub, git, spec_repo

pytestmark = pytest.mark.usefixtures("no_network")

T0 = datetime(2026, 10, 30, 15, 0, 0, tzinfo=timezone.utc)
NOTE = {"file": "tests/test_ledger.py", "kind": "test", "test": "test_zebra_quartz_invariant must hold",
        "requirement": "The ledger SHALL reject negative balances", "decision": "Which one wins?"}
SLIP, BLOCKED, DONE, NEEDS_INPUT = "ck_eta_slip", "ck_eta_blocked", "ck_run_done", "ck_needs_input"


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def minutes_after_t0(stamp: str) -> float:
    return (parse(stamp) - T0).total_seconds() / 60


ENV = {"TZ": "UTC"}


def run_env(now: Optional[str] = None) -> Dict[str, str]:
    env = {**os.environ, **ENV}
    env.pop("CK_RUN_NOTIFY", None)
    if now:
        env[h.NOW_ENV] = now
    return env


def worktree(repo: Path, *args: str, now: Optional[str] = None):
    return ck_sub(h.WORKTREE_CMD, *args, env={**ENV, **({h.NOW_ENV: now} if now else {})}, cwd=repo)


def make_branch(repo: Path, task: int, rel: Optional[str] = None, content: str = "x\n") -> None:
    wt = repo / ".ck" / "worktrees" / f"demo-task-{task}"
    target = wt / (rel or f"t{task}.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", f"task {task}")


def land(repo: Path, run: str, *tasks: int, now: str):
    done = worktree(repo, "land", "demo", *[str(t) for t in tasks], h.PROGRESS_RUN_FLAG, run, "--json", now=now)
    try:
        data = json.loads(done.stdout)
    except ValueError:
        raise AssertionError(done.output)
    return done, data


def park(repo: Path, task: int, now: Optional[str] = None) -> subprocess.CompletedProcess:
    return ck_run_cmd(repo, "worktree", "park", "demo", str(task), "--json", env=run_env(now), stdin=json.dumps(NOTE))


def configure_notify(tmp_path: Path) -> Path:
    out = tmp_path / "notices.jsonl"
    done = set_notify(h.typed_notify_script(tmp_path / "bin", out), env=run_env())
    assert done.returncode == 0, done.stdout + done.stderr
    return out


def types(out: Path) -> List[str]:
    return [t for _, t in h.typed_notices(out)]


# --------------------------------------------------------------------------- TC-53(b)


LANDED = re.compile(r"^landed (?P<tasks>[\d,]+) · ETA (?P<clock>\d\d:\d\d)(?: \((?P<delta>[+-]\d+)\))?$")


def test_a_landing_that_moves_the_median_shows_the_change_and_one_that_does_not_shows_none(tmp_path):
    repo = spec_repo(tmp_path / "repo", tasks=9)
    started = h.begin(ENV, repo, "demo", "implement", now=at(0))
    run = started["run"]
    assert worktree(repo, "create", "demo", "4", "5", "6", "9").exit_code == 0
    make_branch(repo, 4)
    make_branch(repo, 9)
    make_branch(repo, 5, "shared.txt", "five\n")
    make_branch(repo, 6, "shared.txt", "six\n")

    done, data = land(repo, run, 4, 9, now=at(0))
    assert done.exit_code == 0, done.output
    m = LANDED.match(data["eta"])
    assert m, data["eta"]
    assert m["tasks"] == "4,9"
    assert m["delta"] is not None and int(m["delta"]) != 0  # two tasks merged: the median moved

    done, data = land(repo, run, 5, now=at(0))
    assert done.exit_code == 0, done.output
    assert LANDED.match(data["eta"]), data["eta"]

    done, data = land(repo, run, 6, now=at(0))  # conflicts with task 5: nothing merges, the median does not move
    assert {t["task"]: t["state"] for t in data["tasks"]}["6"] != "MERGED"
    m = LANDED.match(data["eta"])
    assert m, data["eta"]
    assert m["tasks"] == "6" and m["delta"] is None
    assert "(" not in data["eta"]


# --------------------------------------------------------------------------- TC-58


def test_a_failed_task_blocks_once_and_observing_it_again_sends_nothing(tmp_path):
    out = configure_notify(tmp_path)
    repo = spec_repo(tmp_path / "repo", tasks=3, deps={"2": ["1"], "3": ["1"]})
    run = h.begin(ENV, repo, "demo", "implement", now=at(0))["run"]

    first = ck_sub("plan", "demo", h.PROGRESS_RUN_FLAG, run, "--failed", "1", env={**ENV, h.NOW_ENV: at(5)}, cwd=repo)
    assert first.exit_code == 0, first.output
    notices = h.typed_notices(out)
    assert len(notices) == 1 and notices[0][1] == BLOCKED
    assert "demo" in notices[0][0]

    ck_sub("plan", "demo", h.PROGRESS_RUN_FLAG, run, "--failed", "1", env={**ENV, h.NOW_ENV: at(6)}, cwd=repo)
    h.event(ENV, repo, "demo", "approval-start", now=at(7), reason="question")
    h.event(ENV, repo, "demo", "approval-end", now=at(8))
    h.eta(ENV, repo, "demo", "--json", now=at(9))
    assert types(out) == [BLOCKED]


def test_parking_the_only_path_sends_the_park_message_only(tmp_path):
    out = configure_notify(tmp_path)
    repo = spec_repo(tmp_path / "repo", tasks=3, deps={"2": ["1"], "3": ["2"]})
    h.begin(ENV, repo, "demo", "implement", now=at(0))
    assert park(repo, 1, now=at(5)).returncode == 0
    h.event(ENV, repo, "demo", "approval-start", now=at(6), reason="question")
    h.event(ENV, repo, "demo", "approval-end", now=at(7))
    notices = h.typed_notices(out)
    assert [t for _, t in notices] == [NEEDS_INPUT]
    assert "demo" in notices[0][0]


def test_parking_one_branch_sends_the_park_message_only(tmp_path):
    out = configure_notify(tmp_path)
    repo = spec_repo(tmp_path / "repo", tasks=4, deps={"2": ["1"], "4": ["3"]})
    h.begin(ENV, repo, "demo", "implement", now=at(0))
    assert park(repo, 3, now=at(5)).returncode == 0
    h.eta(ENV, repo, "demo", "--json", now=at(6))
    assert types(out) == [NEEDS_INPUT]


# --------------------------------------------------------------------------- TC-103


def test_a_hand_landing_freezes_the_whole_run_until_the_run_ends(tmp_path):
    out = configure_notify(tmp_path)
    repo = spec_repo(tmp_path / "repo", tasks=4)
    started = h.begin(ENV, repo, "demo", "implement", now=at(0))
    run = started["run"]
    p90 = parse(started["estimate"]["finish"]["p90"])
    assert worktree(repo, "create", "demo", "2", "3", "4").exit_code == 0
    make_branch(repo, 2, ".claude/settings.json", "{}\n")
    make_branch(repo, 3)
    make_branch(repo, 4)

    done, data = land(repo, run, 2, now=at(20))
    assert {t["task"]: t["state"] for t in data["tasks"]}["2"] == "PROTECTED"

    done, data = land(repo, run, 3, now=at(25))
    assert done.exit_code == 0, done.output
    assert data["eta"] == "landed 3 · waiting on you 5 min"
    view25 = h.eta_json(ENV, repo, "demo", "--seed", "4", now=at(25))
    assert view25["state"] == "waiting"

    late_minutes = (p90 - T0).total_seconds() / 60 + 20  # past launch p90 plus the 15 minute slip threshold
    late = at(late_minutes)
    view_late = h.eta_json(ENV, repo, "demo", "--seed", "4", now=late)
    assert view_late["state"] == "waiting"
    assert view_late["remainingMinutes"] == view25["remainingMinutes"]

    done, data = land(repo, run, 4, now=late)
    assert done.exit_code == 0, done.output
    m = re.match(r"^landed 4 · waiting on you (\d+) min$", data["eta"])
    assert m, data["eta"]
    assert abs(int(m[1]) - (parse(late) - (T0 + timedelta(minutes=20))).total_seconds() / 60) <= 1
    after = h.eta_json(ENV, repo, "demo", "--seed", "4", now=late)
    assert after["state"] == "waiting"

    assert SLIP not in types(out)
    assert h.end(ENV, repo, "demo", "partial", now=at(late_minutes + 1)).exit_code == 0
    closed = h.eta_json(ENV, repo, "demo", now=at(late_minutes + 2))
    assert closed["ended"] is not None and closed["state"] == "done"
    assert types(out).count(DONE) == 1
    assert SLIP not in types(out)
