"""Progress spooling and mirroring under the real `ck run` (run-followups, TC-95, TC-96, TC-97).

`ck run demo --command implement --unsandboxed` runs as a real subprocess in a real git repo (built by
`eta_helpers.eta_spec`), with a test-owned fake `claude` first on PATH. The fake runs the real `ck progress ...` commands
with the env `ck run` gave it, following a JSON script, and exits with a scripted code. Project modules are never
imported at module level, so this file collects before the code exists.
"""
from __future__ import annotations

import json
import math
import os
import stat
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from eta_helpers import (NOW_ENV, POOL_IMPL, RECHECK_ENV, TEST_KINDS_ENV, TaskDef, begin, eta_json, eta_spec, install_pool,
                         typed_notices, typed_notify_script)
from eta_strategies import progress_lines
from guard_helpers import notify_script, set_notify
from stats_helpers import CK, CONFIDENTIAL, all_bytes, git

pytestmark = pytest.mark.usefixtures("no_network")

needs_user = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")

T0 = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
NOTE = {"file": "tests/test_ledger.py", "kind": "test", "test": "tests/test_ledger.py::test_balance",
        "requirement": "1.1", "decision": "The test contradicts the requirement; which one wins?"}
PARK_MESSAGE = "ck: demo task 1 needs a decision (parked as needs input)"

FAKE_CLAUDE = r'''#!{python}
import glob, json, os, subprocess, sys, time

script = json.load(open({scenario!r}))
record_path = script["record"]
record = {{}}


def save():
    tmp = record_path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(json.dumps(record))
    os.replace(tmp, record_path)


save()
for step in script["steps"]:
    if "ck" in step:
        env = dict(os.environ)
        env.update(step.get("env", {{}}))
        done = subprocess.run({ck!r} + step["ck"], env=env, input=step.get("stdin"), capture_output=True, text=True)
        record[step.get("save", "step%d" % len(record))] = {{"code": done.returncode, "stdout": done.stdout, "stderr": done.stderr}}
    elif "record_env" in step:
        record[step["save"]] = {{name: os.environ.get(name) for name in step["record_env"]}}
    elif "sleep" in step:
        time.sleep(step["sleep"])
    elif "touch" in step:
        open(step["touch"], "w").write("1")
    elif "wait_for" in step:
        end = time.time() + step.get("timeout", 60)
        while not os.path.exists(step["wait_for"]) and time.time() < end:
            time.sleep(0.05)
    elif "chmod" in step:
        for path in glob.glob(os.path.expandvars(step["chmod"])):
            os.chmod(path, step["mode"])
    elif "exit" in step:
        save()
        sys.exit(step["exit"])
    save()
save()
'''


class World:
    def __init__(self, tmp: Path, repo: Path) -> None:
        self.tmp, self.repo = tmp, repo
        self.bin = tmp / "bin"
        self.bin.mkdir(exist_ok=True)
        self.record = tmp / "record.json"
        self.procs: List[subprocess.Popen] = []
        self.restore: List[Path] = []
        self.runs = 0

    @property
    def progress_dir(self) -> Path:
        return Path(os.environ["XDG_DATA_HOME"]) / "claude-kiro" / "progress"

    @property
    def spool_dir(self) -> Path:
        return self.repo / ".ck" / "worktrees" / ".ck-progress"

    def script(self, steps: List[Dict[str, Any]]) -> None:
        self.record.unlink(missing_ok=True)
        scenario = self.tmp / "scenario.json"
        scenario.write_text(json.dumps({"record": str(self.record), "steps": steps}))
        fake = self.bin / "claude"
        fake.write_text(FAKE_CLAUDE.format(python=sys.executable, scenario=str(scenario), ck=list(CK)))
        fake.chmod(0o755)

    def child(self) -> Dict[str, Any]:
        try:
            return json.loads(self.record.read_text())
        except (OSError, ValueError):
            return {}

    def child_json(self, name: str) -> dict:
        step = self.child().get(name)
        assert step is not None, f"the fake claude never ran {name!r}: {self.child()}"
        assert step["code"] == 0, step
        return json.loads(step["stdout"])

    def start(self, env: Optional[Dict[str, str]] = None, *extra: str) -> subprocess.Popen:
        self.runs += 1
        out = open(self.tmp / f"run-{self.runs}.out", "w")
        full = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "TZ": "UTC", "CK_AGENT_SLOTS": "4", **(env or {})}
        proc = subprocess.Popen([*CK, "run", "demo", "--command", "implement", "--unsandboxed", *extra], cwd=self.repo,
                                env=full, stdout=out, stderr=subprocess.STDOUT, text=True)
        proc.out_path = self.tmp / f"run-{self.runs}.out"  # type: ignore[attr-defined]
        self.procs.append(proc)
        return proc

    def finish(self, proc: subprocess.Popen, timeout: float = 180) -> str:
        proc.wait(timeout=timeout)
        return proc.out_path.read_text()  # type: ignore[attr-defined]

    def close(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        for path in self.restore:
            if path.exists():
                path.chmod(0o700 if path.is_dir() else 0o600)


def wait_until(pred, timeout: float, what: str, proc: Optional[subprocess.Popen] = None, interval: float = 0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        if proc is not None and proc.poll() is not None:
            out = proc.out_path.read_text() if hasattr(proc, "out_path") else ""  # type: ignore[attr-defined]
            raise AssertionError(f"ck run ended (exit {proc.returncode}) before {what}:\n{out}")
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    install_pool(POOL_IMPL, Path(os.environ["XDG_DATA_HOME"]))
    repo = eta_spec(tmp_path / "repo", [TaskDef(1), TaskDef(2, deps=(1,))])
    w = World(tmp_path, repo)
    yield w
    w.close()


def configure_typed(w: World) -> Path:
    sink = w.tmp / "notices.jsonl"
    assert set_notify(typed_notify_script(w.bin, sink)).returncode == 0
    return sink


def data_lines(w: World, key: str) -> List[dict]:
    return progress_lines(w.progress_dir / f"{key}.jsonl")


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def begin_step(save: str = "begin", **env: str) -> dict:
    return {"ck": ["progress", "begin", "demo", "--kind", "implement", "--json"], "save": save, "env": env}


def ck_step(save: str, *args: str, stdin: Optional[str] = None, **env: str) -> dict:
    step: Dict[str, Any] = {"ck": list(args), "save": save, "env": env}
    if stdin is not None:
        step["stdin"] = stdin
    return step


# --------------------------------------------------------------------------- TC-95


def test_tc95_the_spool_is_private_ignored_mirrored_once_and_removed(world):
    # Only the spool's own .gitignore may keep it out of git: the repo's .gitignore says nothing.
    (world.repo / ".gitignore").write_text("# nothing ignored here\n")
    git(world.repo, "add", "-A")
    git(world.repo, "commit", "-qm", "ignore nothing")
    ready, go = world.tmp / "ready", world.tmp / "go"
    world.script([
        {"record_env": ["CK_PROGRESS_DIR"], "save": "env"},
        begin_step(),
        ck_step("approval-start", "progress", "event", "demo", "approval-start", "--reason", "question"),
        {"touch": str(ready)},
        {"wait_for": str(go), "timeout": 90},
        ck_step("approval-end", "progress", "event", "demo", "approval-end"),
        {"exit": 0},
    ])
    proc = world.start()
    wait_until(ready.exists, 90, "the fake claude to write its first events", proc)

    spool = Path(world.child()["env"]["CK_PROGRESS_DIR"])
    assert spool.resolve() == world.spool_dir.resolve()
    assert stat.S_IMODE(spool.stat().st_mode) == 0o700
    key = world.child_json("begin")["run"]
    spooled = spool / f"{key}.jsonl"
    assert spooled.exists()
    for path in spool.iterdir():
        if path.name != ".gitignore":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert subprocess.run(["git", "check-ignore", "-q", str(spooled)], cwd=world.repo).returncode == 0
    status = git(world.repo, "status", "--porcelain", "--untracked-files=all")
    assert ".ck-progress" not in status

    # every spool line reaches the data-dir file exactly once, within 5 s, while the child is still running
    spool_lines = progress_lines(spooled)
    assert [ln["kind"] for ln in spool_lines] == ["start", "approval-start"]
    deadline = time.monotonic() + 5

    def mirrored() -> bool:
        have = data_lines(world, key)
        return all(have.count(ln) == 1 for ln in spool_lines)

    while time.monotonic() < deadline and not mirrored():
        time.sleep(0.2)
    assert proc.poll() is None
    assert mirrored(), (spool_lines, data_lines(world, key))

    go.write_text("1")
    text = world.finish(proc)
    assert proc.returncode == 0, text
    final = data_lines(world, key)
    assert [ln["kind"] for ln in final].count("approval-end") == 1
    assert all(final.count(ln) == 1 for ln in final)
    assert all(final.count(ln) == 1 for ln in spool_lines)
    assert not spool.exists() or [p.name for p in spool.glob(f"{key}*")] == []


# --------------------------------------------------------------------------- TC-96


@needs_user
def test_tc96_prepared_snapshot_supersede_and_sidecars(world):
    sink = configure_typed(world)
    earlier = begin({}, world.repo, "demo", "implement")
    assert earlier["run"], earlier
    noread = world.tmp / "unreadable-data"
    noread.mkdir()
    noread.chmod(0)
    world.restore.append(noread)
    world.script([
        begin_step(CK_DATA_HOME=str(noread)),
        {"sleep": 3},
        {"exit": 0},
    ])
    proc = world.start()
    text = world.finish(proc)
    assert proc.returncode == 0, text

    child = world.child_json("begin")
    assert child["estimate"]["basis"]["source"] == "local", child["estimate"]["basis"]
    key = child["run"]
    assert (world.progress_dir / f"{key}.model.json").is_file()
    assert (world.progress_dir / f"{key}.eta.json").is_file()
    assert (world.progress_dir / f"{key}.jsonl").is_file()

    old = progress_lines(Path(earlier["file"]))
    ends = [ln for ln in old if ln["kind"] == "end"]
    assert len(ends) == 1 and ends[0]["outcome"] == "superseded", old
    sent = [(m, t) for m, t in typed_notices(sink) if "superseded" in m]
    assert len(sent) == 1, typed_notices(sink)
    assert sent[0][1] == "ck_run_done" and sent[0][0].startswith("ck: demo superseded")


# --------------------------------------------------------------------------- TC-97


def counts(notices) -> Counter:
    return Counter(t for _, t in notices)


def test_tc97_a_failed_task_holding_everything_sends_one_blocked_and_one_done_from_the_parent(world):
    sink = configure_typed(world)
    world.script([
        begin_step(),
        ck_step("failed", "progress", "event", "demo", "task-failed", "--task", "1", "--reason", "gate"),
        {"sleep": 4},
        ck_step("end", "progress", "end", "demo", "--outcome", "halted"),
        {"exit": 0},
    ])
    proc = world.start()
    text = world.finish(proc)
    assert proc.returncode == 0, text

    key = world.child_json("begin")["run"]
    sent = typed_notices(sink)
    assert counts(sent) == Counter({"ck_eta_blocked": 1, "ck_run_done": 1}), sent
    blocked = next(m for m, t in sent if t == "ck_eta_blocked")
    done = next(m for m, t in sent if t == "ck_run_done")
    assert blocked.startswith("ck: demo blocked")
    assert "halted" in done and done.startswith("ck: demo ")
    notified = [ln for ln in data_lines(world, key) if ln["kind"] == "notified"]
    assert Counter((ln["notice"], ln["outcome"]) for ln in notified) == Counter({("blocked", "sent"): 1, ("done", "sent"): 1}), notified


def test_tc97_a_whole_run_park_in_the_child_sends_only_the_park_message(world):
    sink = configure_typed(world)
    world.script([
        begin_step(),
        ck_step("task", "progress", "event", "demo", "task-start", "--task", "1", **{TEST_KINDS_ENV: "1"}),
        ck_step("park", "worktree", "park", "demo", "1", "--json", stdin=json.dumps(NOTE)),
        {"sleep": 4},
        {"exit": 0},
    ])
    # one task only, so the park holds the whole run
    spec_file = world.repo / "specs" / "demo" / "tasks.md"
    spec_file.write_text(spec_file.read_text().split("### Task 2:")[0])
    git(world.repo, "commit", "-qam", "one task")
    proc = world.start()
    text = world.finish(proc)
    assert proc.returncode == 0, text
    assert world.child()["park"]["code"] == 0, world.child()

    key = world.child_json("begin")["run"]
    assert any(ln["kind"] == "park" for ln in data_lines(world, key))
    assert typed_notices(sink) == [(PARK_MESSAGE, "ck_needs_input")]
    assert eta_json({}, world.repo, "demo")["state"] == "blocked"


def test_tc97_time_only_slip_is_sent_once_by_the_ck_run_watcher(world):
    sink = configure_typed(world)
    spec_file = world.repo / "specs" / "demo" / "tasks.md"
    spec_file.write_text(spec_file.read_text().split("### Task 2:")[0])
    git(world.repo, "commit", "-qam", "one task")
    launch = eta_json({}, world.repo, "demo", now=at(0))
    p90 = launch["remainingMinutes"]["p90"]
    late = at(math.ceil(p90) + 18)
    started, go = world.tmp / "started", world.tmp / "go"
    world.script([
        begin_step(**{NOW_ENV: at(0)}),
        ck_step("start", "progress", "event", "demo", "task-start", "--task", "1", "--model", "sonnet", "--attempt", "1",
                **{NOW_ENV: at(0), TEST_KINDS_ENV: "1"}),
        {"touch": str(started)},
        {"wait_for": str(go), "timeout": 90},
        ck_step("later", "progress", "event", "demo", "retry", "--task", "1", "--attempt", "2", **{NOW_ENV: late, TEST_KINDS_ENV: "1"}),
        {"sleep": 2},
        {"exit": 0},
    ])
    proc = world.start({NOW_ENV: late, RECHECK_ENV: "1"})
    wait_until(started.exists, 90, "the fake claude to start task 1", proc)
    wait_until(lambda: typed_notices(sink), 5, "the slip message", proc)
    sent = typed_notices(sink)
    assert len(sent) == 1, sent
    message, kind = sent[0]
    assert kind == "ck_eta_slip" and "demo" in message and "task 1 running" in message

    go.write_text("1")
    text = world.finish(proc)
    assert proc.returncode == 0, text
    assert world.child()["later"]["code"] == 0
    assert typed_notices(sink) == sent


def run_exit(w: World, code: int, steps: List[dict], env: Optional[Dict[str, str]] = None):
    w.script([*steps, {"exit": code}])
    proc = w.start(env)
    text = w.finish(proc)
    return proc.returncode, [ln for ln in text.splitlines() if ln.strip()][-1].split(";")[0], text


def test_tc97_a_failing_notify_command_changes_neither_exit_code_nor_outcome(world):
    assert set_notify(notify_script(world.bin, "fail", world.tmp / "unused.json")).returncode == 0
    base_code, base_outcome, _ = run_exit(world, 7, [])
    assert base_code == 7 and "claude exited 7" in base_outcome

    steps = [begin_step(), ck_step("failed", "progress", "event", "demo", "task-failed", "--task", "1", "--reason", "gate"),
             {"sleep": 4}, ck_step("end", "progress", "end", "demo", "--outcome", "halted")]
    code, outcome, text = run_exit(world, 7, steps)
    assert (code, outcome) == (base_code, base_outcome), text
    key = world.child_json("begin")["run"]
    failed = [ln for ln in data_lines(world, key) if ln["kind"] == "notified" and ln["outcome"] == "failed"]
    assert failed, data_lines(world, key)


@needs_user
def test_tc97_a_spool_made_unreadable_mid_run_changes_neither_exit_code_nor_outcome(world):
    configure_typed(world)
    base_code, base_outcome, _ = run_exit(world, 5, [])
    assert base_code == 5

    steps = [
        begin_step(),
        ck_step("first", "progress", "event", "demo", "task-failed", "--task", "1", "--reason", "gate"),
        {"chmod": "$CK_PROGRESS_DIR/*.jsonl", "mode": 0},
        ck_step("second", "progress", "event", "demo", "approval-start", "--reason", "question"),
        {"sleep": 4},
    ]
    world.restore.append(world.spool_dir)
    code, outcome, text = run_exit(world, 5, steps)
    for path in world.spool_dir.glob("*.jsonl"):
        world.restore.append(path)
    assert (code, outcome) == (base_code, base_outcome), text

    key = world.child_json("begin")["run"]
    evidence = [p for p in [*world.progress_dir.glob("*.errors"), *world.spool_dir.glob("*.errors")] if p.read_text().strip()]
    evidence += [p for p in world.progress_dir.glob(f"{key}.jsonl") if any("fail" in json.dumps(ln) for ln in progress_lines(p))]
    assert evidence or "progress" in text.lower(), "no failure was recorded in the progress file, a .errors sidecar or the run log"


def test_tc97_mirrored_lines_hold_no_confidential_flag_value(world):
    configure_typed(world)
    secrets = list(CONFIDENTIAL)
    steps = [
        begin_step(),
        ck_step("a", "progress", "event", "demo", "task-failed", "--task", secrets[0], "--reason", secrets[1]),
        ck_step("b", "progress", "event", "demo", "approval-start", "--reason", secrets[2]),
        ck_step("c", "progress", "event", "demo", "phase-start", "--phase", secrets[3]),
        ck_step("d", "progress", "event", "demo", "retry", "--task", "1", "--model", secrets[4], "--attempt", secrets[5],
                **{TEST_KINDS_ENV: "1"}),
        ck_step("e", "progress", "event", "demo", "step-end", "--phase", secrets[0], "--run", secrets[1]),
        {"sleep": 3},
    ]
    code, _, text = run_exit(world, 0, steps)
    assert code == 0, text
    key = world.child_json("begin")["run"]
    assert data_lines(world, key), "nothing was mirrored"
    blob = all_bytes(world.progress_dir)
    for secret in secrets:
        assert secret.encode() not in blob, secret
