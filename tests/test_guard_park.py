"""`ck worktree park`, `ck worktree resolve`, the notification and the guard-ladder CLI (prompt-free-runs, task 7).

TC-37..TC-41, TC-78, TC-88 and the park half of TC-70, through the real CLI on a real git repo, with a real subprocess
notify command. Project modules are never imported at module level, so this file collects before the code exists.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from guard_helpers import (CK, ck_run_cmd, fake_sandbox_tools, notify_script, parking_fake_claude, set_notify)
from stats_helpers import spec_repo

QUOTED_TEST = "test_zebra_quartz_invariant must hold across the ledger boundary"
QUOTED_REQ = "The ledger SHALL reject negative balances within one transaction"
NOTE = {"file": "tests/test_ledger.py", "kind": "test", "test": QUOTED_TEST, "requirement": QUOTED_REQ,
        "decision": "The test contradicts the requirement; which one wins?"}


def clean_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = os.environ.copy()
    env.pop("CK_RUN_NOTIFY", None)
    env.pop("CK_NOTIFY_MESSAGE", None)
    env.update(extra or {})
    return env


def park(repo: Path, task: str, note: object = NOTE, *flags: str, spec: str = "demo", env: Optional[dict] = None,
         raw: Optional[str] = None) -> subprocess.CompletedProcess:
    stdin = raw if raw is not None else json.dumps(note)
    return ck_run_cmd(repo, "worktree", "park", spec, str(task), *flags, "--json", env=env or clean_env(), stdin=stdin)


def resolve(repo: Path, task: str, answer: object, spec: str = "demo", raw: Optional[str] = None) -> subprocess.CompletedProcess:
    stdin = raw if raw is not None else json.dumps({"answer": answer})
    return ck_run_cmd(repo, "worktree", "resolve", spec, str(task), "--json", env=clean_env(), stdin=stdin)


def report(repo: Path, spec: str = "demo") -> dict:
    done = ck_run_cmd(repo, "worktree", "report", spec, "--json", env=clean_env())
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


def mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def configure(repo: Path, tmp: Path, kind: str, name: str = "notify.json") -> Path:
    """Set run.notify_command to a fresh `kind` script; returns the file an `ok` script writes."""
    out = tmp / name
    cmd = notify_script(tmp / "bin", kind, out)
    done = set_notify(cmd)
    assert done.returncode == 0, done.stdout + done.stderr
    return out


def appending_notify(bin_dir: Path, out: Path) -> str:
    """A notify command that appends one JSON line per call (so duplicates are countable)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "notify-append"
    path.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                    f"open({str(out)!r}, 'a').write(json.dumps({{'stdin': sys.stdin.read(), 'env': os.environ.get('CK_NOTIFY_MESSAGE'), "
                    "'argv': sys.argv}) + '\\n')\n")
    path.chmod(0o755)
    return str(path)


def message_of(sent: dict) -> str:
    return json.loads(sent["stdin"])["message"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return spec_repo(tmp_path / "repo")


# --------------------------------------------------------------------------- TC-37


def test_park_sends_one_notification_without_quoted_text(repo, tmp_path):
    out = configure(repo, tmp_path, "ok")
    done = park(repo, "2")
    assert done.returncode == 0, done.stdout + done.stderr
    result = json.loads(done.stdout)
    assert result["parked"] is True and result["notification"] == "sent"
    sent = json.loads(out.read_text())
    payload = json.loads(sent["stdin"])
    message = payload["message"]
    assert "demo" in message and "2" in message and "decision" in message
    assert payload["hook_event_name"] == "Notification"
    for channel in (sent["stdin"], sent["env"]["CK_NOTIFY_MESSAGE"], " ".join(sent["argv"])):
        for text in (QUOTED_TEST, QUOTED_REQ, NOTE["decision"]):
            assert text not in channel
            for line in text.splitlines():
                assert line not in channel
    assert sent["env"]["CK_NOTIFY_MESSAGE"] == message
    note_path = Path(result["note"])
    assert note_path.exists() and mode(note_path) == 0o600
    stored = json.loads(note_path.read_text())
    assert stored["test"] == QUOTED_TEST and stored["requirement"] == QUOTED_REQ

    rep = report(repo)
    assert len(rep["needsInput"]) == 1
    entry = rep["needsInput"][0]
    assert entry["task"] == "2" and entry["file"] == NOTE["file"] and entry["kind"] == "test"
    assert entry["test"] == QUOTED_TEST and entry["requirement"] == QUOTED_REQ and entry["decision"] == NOTE["decision"]
    assert entry["parkedAt"]
    for key, items in rep.items():
        if key != "needsInput" and isinstance(items, list):
            assert "2" not in [str(i.get("task")) if isinstance(i, dict) else str(i) for i in items], key
    text = ck_run_cmd(repo, "worktree", "report", "demo", env=clean_env())
    assert text.returncode == 0
    assert "needs input" in text.stdout


def test_park_exactly_one_notification_per_park(repo, tmp_path):
    out = tmp_path / "calls.jsonl"
    assert set_notify(appending_notify(tmp_path / "bin", out)).returncode == 0
    assert park(repo, "2").returncode == 0
    assert len(out.read_text().splitlines()) == 1


def test_park_no_notify_flag_skips(repo, tmp_path):
    out = configure(repo, tmp_path, "ok")
    done = park(repo, "2", NOTE, "--no-notify")
    assert done.returncode == 0
    result = json.loads(done.stdout)
    assert result["parked"] is True and result["notification"] == "skipped"
    assert not out.exists()


def test_park_bad_stdin_exits_3(repo):
    for raw in ("not json", "", "[1, 2]"):
        done = park(repo, "2", raw=raw)
        assert done.returncode == 3, (raw, done.stdout, done.stderr)
    assert report(repo)["needsInput"] == []


# --------------------------------------------------------------------------- TC-38


def test_park_without_notify_command_says_how_to_configure(repo):
    done = park(repo, "2")
    assert done.returncode == 0, done.stdout + done.stderr
    result = json.loads(done.stdout)
    assert result["parked"] is True and result["notification"] == "not_configured"
    assert "ck agents set run.notify_command" in result["detail"]
    assert [e["task"] for e in report(repo)["needsInput"]] == ["2"]


# --------------------------------------------------------------------------- TC-39


@pytest.mark.parametrize("kind,expect", [("fail", ("exit 3",)), ("slow", ("timed out after 10 s",)),
                                         ("missing", ("not found", "127", "No such file"))])
def test_failing_notify_never_blocks_the_park(repo, tmp_path, kind, expect):
    configure(repo, tmp_path, kind)
    started = time.monotonic()
    done = park(repo, "2")
    elapsed = time.monotonic() - started
    assert done.returncode == 0, done.stdout + done.stderr
    assert elapsed < 15
    result = json.loads(done.stdout)
    assert result["parked"] is True and result["notification"] == "failed"
    assert any(e in result["detail"] for e in expect), result["detail"]
    rep = report(repo)
    assert [e["task"] for e in rep["needsInput"]] == ["2"]
    text = ck_run_cmd(repo, "worktree", "report", "demo", env=clean_env())
    assert "needs input" in text.stdout


# --------------------------------------------------------------------------- TC-40


def test_parked_task_is_ready_again_with_its_worktree_reused(repo):
    made = ck_run_cmd(repo, "worktree", "create", "demo", "2", "--base", "integrate/demo", "--json", env=clean_env())
    assert made.returncode == 0, made.stdout + made.stderr
    assert json.loads(made.stdout)[0]["state"] == "CREATED"
    assert park(repo, "2").returncode == 0
    plan = ck_run_cmd(repo, "plan", "demo", "--json", env=clean_env())
    assert plan.returncode == 0, plan.stdout + plan.stderr
    assert "2" in [str(n) for n in json.loads(plan.stdout)["ready"]]
    again = ck_run_cmd(repo, "worktree", "create", "demo", "2", "--base", "integrate/demo", "--json", env=clean_env())
    assert again.returncode == 0, again.stdout + again.stderr
    assert json.loads(again.stdout)[0]["state"] == "EXISTS"


def test_fresh_claim_clears_the_park_note(repo):
    from guard_helpers import claimed_task_worktree

    claimed_task_worktree(repo, "demo", 2, base="integrate/demo")
    note = Path(json.loads(park(repo, "2").stdout)["note"])
    assert note.exists()
    released = ck_run_cmd(repo, "worktree", "release", "demo", "2", env=clean_env())
    assert released.returncode == 0, released.stdout + released.stderr
    assert ck_run_cmd(repo, "worktree", "claim", "demo", "2", env=clean_env()).returncode == 0
    assert not note.exists()
    assert report(repo)["needsInput"] == []


# --------------------------------------------------------------------------- TC-41


LADDER = [("haiku", ["sonnet", "opus", "fable"]), ("sonnet", ["opus", "fable"]), ("claude-sonnet-5-5", ["opus", "fable"]),
          ("opus", ["fable"]), ("fable", []), ("some-unknown-model", ["sonnet", "opus", "fable"])]


@pytest.mark.parametrize("escalate", [None, ["opus"], ["sonnet", "opus"], ["fable"], ["haiku"]])
def test_guard_ladder_cli_ignores_escalate(tmp_path, escalate):
    if escalate is not None:
        done = ck_run_cmd(tmp_path, "agents", "set", "implementer.escalate", ",".join(escalate), env=clean_env())
        if done.returncode != 0:
            done = ck_run_cmd(tmp_path, "agents", "set", "implementer.escalate", json.dumps(escalate), env=clean_env())
        assert done.returncode == 0, done.stdout + done.stderr
    for model, expected in LADDER:
        got = ck_run_cmd(tmp_path, "agents", "--guard-ladder", model, "--json", env=clean_env())
        assert got.returncode == 0, got.stdout + got.stderr
        assert json.loads(got.stdout) == expected, (model, escalate)


# --------------------------------------------------------------------------- TC-78


def test_resolve_records_the_answer_and_keeps_it(repo):
    first = json.loads(park(repo, "2").stdout)
    assert json.loads((Path(first["note"])).read_text())["priorDecision"] is False
    done = resolve(repo, "2", "the code must satisfy the test")
    assert done.returncode == 0, done.stdout + done.stderr
    result = json.loads(done.stdout)
    assert result["resolved"] is True and result["hadParkNote"] is True
    decision = Path(result["decisionNote"])
    assert mode(decision) == 0o600
    assert "the code must satisfy the test" in decision.read_text()
    assert not Path(first["note"]).exists()
    assert report(repo)["needsInput"] == []

    second = resolve(repo, "2", "and add a regression test")
    assert second.returncode == 0
    text = decision.read_text()
    assert "the code must satisfy the test" in text and "and add a regression test" in text
    assert json.loads(second.stdout)["hadParkNote"] is False

    again = json.loads(park(repo, "2").stdout)
    assert json.loads(Path(again["note"]).read_text())["priorDecision"] is True


def test_resolve_bad_stdin_exits_3(repo):
    for raw in ("nope", "", "{}", '{"answer": 5}', "[]"):
        done = resolve(repo, "2", None, raw=raw)
        assert done.returncode == 3, (raw, done.stdout, done.stderr)


# --------------------------------------------------------------------------- TC-88


def run_ck(repo: Path, bin_dir: Path, log: Path, *extra: str) -> subprocess.CompletedProcess:
    env = clean_env({"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "")})
    return ck_run_cmd(repo, "run", "demo", "--log", str(log), *extra, env=env)


def test_park_inside_ck_run_is_deferred_and_notified_once_by_ck_run(repo, tmp_path):
    bin_dir = fake_sandbox_tools(tmp_path / "bin")
    out = tmp_path / "calls.jsonl"
    assert set_notify(appending_notify(bin_dir, out)).returncode == 0

    record2 = tmp_path / "park2.json"
    parking_fake_claude(bin_dir, record2, "demo", "2")
    log1 = tmp_path / "run1.log"
    done = run_ck(repo, bin_dir, log1)
    assert done.returncode == 0, done.stdout + done.stderr
    parked = json.loads(record2.read_text())
    assert parked["returncode"] == 0, parked
    assert json.loads(parked["stdout"])["notification"] == "deferred"
    calls = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(calls) == 1
    message = json.loads(calls[0]["stdin"])["message"]
    assert "demo" in message and "2" in message and "decision" in message
    assert "tests/test_x.py::test_x" not in calls[0]["stdin"]
    assert "The test contradicts the requirement." not in calls[0]["stdin"]
    assert log1.read_text().count("ck run: notification sent") == 1

    record3 = tmp_path / "park3.json"
    parking_fake_claude(bin_dir, record3, "demo", "3")
    log2 = tmp_path / "run2.log"
    done = run_ck(repo, bin_dir, log2)
    assert done.returncode == 0, done.stdout + done.stderr
    calls = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(calls) == 2
    second = json.loads(calls[1]["stdin"])["message"]
    assert "task 3" in second and "task 2" not in second
    assert log2.read_text().count("ck run: notification sent") == 1


def test_sandbox_settings_do_not_depend_on_the_notify_command(repo, tmp_path):
    bin_dir = fake_sandbox_tools(tmp_path / "bin")
    env = clean_env({"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "")})

    def dry() -> dict:
        done = ck_run_cmd(repo, "run", "demo", "--dry-run", "--json", "--log", str(tmp_path / "x.log"), env=env)
        assert done.returncode == 0, done.stdout + done.stderr
        return json.loads(done.stdout)

    without = dry()
    assert set_notify(notify_script(bin_dir, "ok", tmp_path / "o.json")).returncode == 0
    with_notify = dry()
    assert without["settings"] == with_notify["settings"]
    assert without["settings"]["sandbox"] == with_notify["settings"]["sandbox"]


# --------------------------------------------------------------------------- TC-70 (park half)


def test_park_without_notify_config_makes_no_network_call(repo, no_network):
    log = Path(os.environ["CK_TEST_NET_LOG"])
    done = park(repo, "2")
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["notification"] == "not_configured"
    assert not log.exists() or log.read_text().strip() == ""
