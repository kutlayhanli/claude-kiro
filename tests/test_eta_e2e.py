"""End-to-end ETA chains through the real CLI (run-followups, TC-92 and TC-93).

Real git repos, real `ck` subprocesses, the real `ck-status-line` entry point and a real notify script. Project modules
are never imported at module level, so this file collects before the code exists.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from eta_helpers import (NOW_ENV, POOL_CREATE, POOL_IMPL, begin, claim_event, end, eta_json, event, install_pool,
                         status_line, typed_notices, typed_notify_script)
from guard_helpers import ck_run_cmd, set_notify
from stats_helpers import _set_done, ck_sub, git, spec_repo

pytestmark = pytest.mark.usefixtures("no_network")

T0 = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
NOTE = {"file": "tests/test_ledger.py", "kind": "test", "test": "tests/test_ledger.py::test_balance",
        "requirement": "1.1", "decision": "The test contradicts the requirement; which one wins?"}
CREATE_PHASES = ("Requirements", "Design + Test Plan", "Task Tracks", "Link", "Adversarial Review", "Revise",
                 "Consistency Check")


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def xdg() -> Path:
    return Path(os.environ["XDG_DATA_HOME"])


def line_of(env: dict, repo: Path, minute: float) -> str:
    out = status_line({**env, NOW_ENV: at(minute)}, {"workspace": {"current_dir": str(repo)}}, repo)
    assert out.exit_code == 0, out.output
    return out.stdout.strip()


def kinds(notices: List[Tuple[str, Optional[str]]]) -> List[Optional[str]]:
    return [t for _, t in notices]


# --------------------------------------------------------------------------- TC-92


def test_tc92_claim_land_park_resolve_chain_agrees_at_every_step(tmp_path):
    install_pool(POOL_IMPL, xdg())
    repo = spec_repo(tmp_path / "repo", tasks=2, deps={"2": ["1"]})
    sink = tmp_path / "notices.jsonl"
    assert set_notify(typed_notify_script(tmp_path / "bin", sink)).returncode == 0
    env = {"CK_AGENT_SLOTS": "4"}

    def view(minute: float) -> dict:
        return eta_json(env, repo, "demo", now=at(minute))

    def notices() -> List[Tuple[str, Optional[str]]]:
        return typed_notices(sink)

    # (1) launch estimate: not started, nothing on the status line, nothing sent
    v = view(0)
    assert v["state"] == "not-started" and v["progressFile"] is None
    assert line_of(env, repo, 0) == ""
    assert notices() == []

    # (2) begin
    started = begin(env, repo, "demo", "implement", now=at(0))
    key = started["run"]
    assert key, started
    assert view(1)["state"] == "on-track"
    assert re.match(r"^ck demo 0/2 ETA \d\d:\d\d", line_of(env, repo, 1))
    assert notices() == []

    # (3) claim task 1 with progress flags
    made = ck_run_cmd(repo, "worktree", "create", "demo", "1")
    assert made.returncode == 0, made.stdout + made.stderr
    claimed = claim_event(env, repo, "demo", 1, run=key, model="sonnet", attempt=1, now=at(2))
    assert claimed.exit_code == 0, claimed.output
    v = view(2)
    assert v["state"] == "on-track" and v["running"] == 1
    assert re.match(r"^ck demo 0/2 ETA \d\d:\d\d", line_of(env, repo, 2))
    assert notices() == []

    # (4) land task 1 with --progress-run
    wt1 = repo / ".ck" / "worktrees" / "demo-task-1"
    (wt1 / "t1.txt").write_text("1\n")
    _set_done(wt1, "demo", 1)
    git(wt1, "add", "-A")
    git(wt1, "commit", "-qm", "task 1")
    landed = ck_sub("worktree", "land", "demo", "1", "--into", "main", "--release", "--progress-run", key, "--json",
                    env={**env, NOW_ENV: at(4), "TZ": "UTC"}, cwd=repo)
    assert landed.exit_code == 0, landed.output
    data = json.loads(landed.stdout)
    assert data["tasks"][0]["state"] == "MERGED"
    assert "eta" in data
    v = view(4)
    assert v["state"] == "on-track" and v["merged"] == 1
    assert re.match(r"^ck demo 1/2 ETA \d\d:\d\d", line_of(env, repo, 4))
    assert notices() == []

    # (5) park the only remaining path: blocked, and prompt-free-runs' park message is the only message
    made = ck_run_cmd(repo, "worktree", "create", "demo", "2")
    assert made.returncode == 0, made.stdout + made.stderr
    claimed = claim_event(env, repo, "demo", 2, run=key, model="sonnet", attempt=1, now=at(5))
    assert claimed.exit_code == 0, claimed.output
    parked = ck_run_cmd(repo, "worktree", "park", "demo", "2", "--json", env={**os.environ, NOW_ENV: at(6), "TZ": "UTC"},
                        stdin=json.dumps(NOTE))
    assert parked.returncode == 0, parked.stdout + parked.stderr
    assert json.loads(parked.stdout)["notification"] == "sent"
    v = view(6)
    assert v["state"] == "blocked" and v["finish"] is None
    assert "blocked" in line_of(env, repo, 6)
    park_message = "ck: demo task 2 needs a decision (parked as needs input)"
    assert notices() == [(park_message, "ck_needs_input")]

    # (6) resolve: back on track, nothing new sent
    resolved = ck_run_cmd(repo, "worktree", "resolve", "demo", "2", "--json", env={**os.environ, NOW_ENV: at(7), "TZ": "UTC"},
                          stdin=json.dumps({"answer": "keep the requirement"}))
    assert resolved.returncode == 0, resolved.stdout + resolved.stderr
    assert view(7)["state"] == "on-track"
    line = line_of(env, repo, 7)
    assert "blocked" not in line and re.match(r"^ck demo 1/2 ETA \d\d:\d\d", line)
    assert notices() == [(park_message, "ck_needs_input")]

    # (7) an approval wait: waiting while it is open, on track after it closes
    assert event(env, repo, "demo", "approval-start", now=at(8), reason="question").exit_code == 0
    v = view(10)
    assert v["state"] == "waiting"
    assert "waiting on you" in line_of(env, repo, 10)
    assert notices() == [(park_message, "ck_needs_input")]
    assert event(env, repo, "demo", "approval-end", now=at(10)).exit_code == 0
    assert view(10)["state"] == "on-track"

    # (8) end: done, no line, exactly one done message and no blocked message
    assert end(env, repo, "demo", "completed", now=at(11)).exit_code == 0
    assert view(11)["state"] == "done"
    assert line_of(env, repo, 11) == ""
    sent = notices()
    assert len(sent) == 2, sent
    assert sent[0] == (park_message, "ck_needs_input")
    done_message, done_type = sent[1]
    assert done_type == "ck_run_done" and done_message.startswith("ck: demo done ")
    assert not any("blocked" in m for m, _ in sent)
    assert kinds(sent).count("ck_eta_blocked") == 0


# --------------------------------------------------------------------------- TC-93


def test_tc93_create_chain_ends_with_spec_written(tmp_path):
    install_pool(POOL_CREATE, xdg())
    repo = tmp_path / "repo"
    (repo / "specs" / "orbit").mkdir(parents=True)
    (repo / "specs" / "orbit" / "requirements.md").write_text("# Requirements: orbit\n\n1.1 The orbit SHALL log.\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    sink = tmp_path / "notices.jsonl"
    assert set_notify(typed_notify_script(tmp_path / "bin", sink)).returncode == 0
    env = {"CK_AGENT_SLOTS": "4"}

    def view(minute: float) -> dict:
        return eta_json(env, repo, "orbit", now=at(minute))

    v = view(0)
    assert v["state"] == "not-started" and v["runKind"] == "create"
    assert line_of(env, repo, 0) == ""

    started = begin(env, repo, "orbit", "create", now=at(0))
    assert started["run"], started
    assert view(1)["state"] == "on-track"
    assert typed_notices(sink) == []

    for i, phase in enumerate(CREATE_PHASES):
        assert event(env, repo, "orbit", "phase-start", now=at(5 * i + 1), phase=phase).exit_code == 0
        assert re.match(rf"^ck orbit create {re.escape(phase)} ETA \d\d:\d\d", line_of(env, repo, 5 * i + 1)), phase
        stepped = event(env, repo, "orbit", "step-end", now=at(5 * i + 5), phase=phase, json=True)
        assert stepped.exit_code == 0, stepped.output
        reply = json.loads(stepped.stdout)
        assert reply["ok"] is True and isinstance(reply["eta"], str) and "ETA" in reply["eta"], reply
        v = view(5 * i + 5)
        assert v["state"] == "on-track"
        assert {p["phase"]: p["state"] for p in v["phases"]}[phase] == "done"
        assert typed_notices(sink) == []

    assert end(env, repo, "orbit", "completed", now=at(40)).exit_code == 0
    assert view(40)["state"] == "done"
    assert line_of(env, repo, 40) == ""
    sent = typed_notices(sink)
    assert len(sent) == 1, sent
    message, kind = sent[0]
    assert kind == "ck_run_done" and message.startswith("ck: orbit ") and "spec written" in message
