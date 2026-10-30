"""`ck eta` live, ended, stale and watch views (run-followups Task 20: TC-32..TC-38, TC-94, TC-102).

The boundary under test is the `ck eta` reader. Runs start with the real `ck progress begin` at T0 = 15:00 UTC and later
events are seeded with `eta_helpers.append_events` at pinned times; `ck eta` runs at a pinned `CK_NOW`. Only TC-34 uses real
time and a real signal.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Sequence

import pytest

import eta_helpers as h
from stats_helpers import CK

pytestmark = pytest.mark.usefixtures("no_network")

T0 = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
SPEC = "demo"
ETA_SCHEMA_RE = r"^ck-eta/\d+$"

# 1; 2; 3 after 1; 4 after 2; 5 after 3; 6 after 5
TASKS6 = [h.TaskDef(1), h.TaskDef(2), h.TaskDef(3, deps=(1,)), h.TaskDef(4, deps=(2,)), h.TaskDef(5, deps=(3,)),
          h.TaskDef(6, deps=(5,))]
CHAIN3 = [h.TaskDef(1), h.TaskDef(2, deps=(1,)), h.TaskDef(3, deps=(2,))]


def at(minute: float) -> str:
    return (T0 + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")


def when(minute: float) -> datetime:
    return T0 + timedelta(minutes=minute)


def ev(kind: str, minute: float, **fields: Any) -> dict:
    return {"schema": "ck-progress/1", "at": at(minute), "kind": kind, **fields}


def start_task(minute: float, task: int, kind: str = "task-start", attempt: int = 1, model: str = "sonnet") -> dict:
    return ev(kind, minute, task=str(task), attempt=attempt, model=model)


def merge(minute: float, task: int) -> dict:
    return ev("merge", minute, task=str(task))


def park(minute: float, task: int) -> dict:
    return ev("park", minute, task=str(task), parkKind="needs_input", parkedAt=at(minute))


def approval_start(minute: float, ident: str = "a1b2c3d4") -> dict:
    return ev("approval-start", minute, id=ident, reason="question")


def approval_end(minute: float, ident: str = "a1b2c3d4") -> dict:
    return ev("approval-end", minute, id=ident)


def parse(iso_text: str) -> datetime:
    return datetime.fromisoformat(iso_text.replace("Z", "+00:00"))


def mark_done(repo: Path, ids: Sequence[int]) -> None:
    path = repo / "specs" / SPEC / "tasks.md"
    blocks = path.read_text().split("### Task ")
    out = [blocks[0]]
    for block in blocks[1:]:
        if block.split(":", 1)[0].strip() in {str(i) for i in ids}:
            block = block.replace("**Status:** Not Started", "**Status:** Done", 1)
        out.append(block)
    path.write_text("### Task ".join(out))


class Scenario:
    """A real repo with a spec, a real collected store, and (after `begin`) a real progress run."""

    def __init__(self, tmp_path: Path, tasks: Sequence[h.TaskDef], pool: str = h.POOL_IMPL, name: str = "s"):
        self.root = Path(tmp_path) / name
        self.root.mkdir(parents=True, exist_ok=True)
        self.repo = h.eta_spec(self.root / "proj", tasks, spec=SPEC)
        xdg = self.root / "xdg"
        h.install_pool(pool, xdg)
        for sub in ("ck-config", "claude-home", "home"):
            (self.root / sub).mkdir(exist_ok=True)
        cfg = self.root / "gitconfig"
        cfg.write_text("[user]\n\tname = Test User\n\temail = test@example.com\n")
        self.env = {"XDG_DATA_HOME": str(xdg), "CK_CONFIG_HOME": str(self.root / "ck-config"),
                    "CLAUDE_CONFIG_DIR": str(self.root / "claude-home"), "HOME": str(self.root / "home"),
                    "GIT_CONFIG_GLOBAL": str(cfg), "GIT_CONFIG_NOSYSTEM": "1", "TZ": "UTC",
                    h.TEST_KINDS_ENV: "1"}
        self.key: Optional[str] = None
        self.launch: Optional[dict] = None

    def begin(self, minute: float = 0, kind: str = "implement") -> dict:
        out = h.begin(self.env, self.repo, SPEC, kind, now=at(minute))
        assert out.get("run"), out
        self.key = out["run"]
        self.launch = out.get("estimate")
        return out

    def add(self, *events: dict) -> None:
        errors = h.append_events(self.env, self.key, list(events))
        assert all(e is None for e in errors), errors

    def view(self, minute: float, *args: str) -> dict:
        out = h.eta(self.env, self.repo, SPEC, *args, "--json", now=at(minute))
        assert out.exit_code == 0, out.output
        return json.loads(out.stdout)

    def text(self, minute: float, *args: str) -> str:
        out = h.eta(self.env, self.repo, SPEC, *args, now=at(minute))
        assert out.exit_code == 0, out.output
        return out.stdout

    def launch_p90_minute(self) -> float:
        return (parse(self.launch["finish"]["p90"]) - T0).total_seconds() / 60

    def status_line(self, minute: float) -> str:
        out = h.status_line({**self.env, h.NOW_ENV: at(minute)}, {"workspace": {"current_dir": str(self.repo)}}, cwd=self.repo)
        assert out.exit_code == 0, out.output
        return out.stdout


@pytest.fixture
def chain6(tmp_path):
    s = Scenario(tmp_path, TASKS6)
    s.begin()
    return s


# --------------------------------------------------------------------------- TC-32


def test_tc32_live_block(chain6):
    s = chain6
    s.add(start_task(0, 1), start_task(0, 2), merge(6, 1), ev("landing", 6, tasks=["1"], merged=["1"], red=[], conflict=[],
          protected=[], green=True), start_task(6, 3), start_task(10, 2, "retry", attempt=2))
    text = s.text(12)
    view = s.view(12)
    assert "12 min since launch" in text.replace("(", "").replace(")", "") or "12 min ago" in text
    assert "15:10" in text
    assert re.search(r"merged 1 \W+ running 2 \W+ waiting 3", text)
    assert re.search(r"finish \d\d:\d\d\W+\d\d:\d\d\W+\d\d:\d\d", text)
    assert "task 2 retry 1" in text
    assert "critical:" in text and "running:" in text and "waiting:" in text
    running = next(line for line in text.splitlines() if line.startswith("running:"))
    assert "10 min" in running and "6 min" in running
    waiting_line = next(line for line in text.splitlines() if line.startswith("waiting:"))
    assert re.search(r"4 on 2", waiting_line) and re.search(r"5 on 3", waiting_line)
    delta = (parse(view["finish"]["p50"]) - parse(s.launch["finish"]["p50"])).total_seconds() / 60
    shown = re.search(r"([+-]\d+) min vs launch", text)
    assert shown, text
    assert abs(int(shown.group(1)) - delta) <= 1
    assert abs(view["deltaVsLaunchMinutes"] - delta) <= 1
    assert parse(view["finish"]["p10"]) >= when(12)
    assert (view["merged"], view["running"], view["waiting"]) == (1, 2, 3)
    assert view["cause"] == "task 2 retry 1"
    by_task = {t["task"]: t for t in view["tasks"]}
    assert round(by_task["2"]["minutes"]) == 10 and round(by_task["3"]["minutes"]) == 6


# --------------------------------------------------------------------------- TC-33


def test_tc33_task_past_p90_is_conditioned(tmp_path):
    s = Scenario(tmp_path, [h.TaskDef(1)])
    s.begin()
    s.add(start_task(0, 1))
    view = s.view(60)
    assert parse(view["finish"]["p10"]) >= when(60)
    assert view["remainingMinutes"]["p10"] > 0


# --------------------------------------------------------------------------- TC-34


class Watcher:
    def __init__(self, env: dict, cwd: Path):
        full = {**os.environ, **env, "TZ": "UTC"}
        full.pop(h.NOW_ENV, None)
        self.proc = subprocess.Popen([*CK, "eta", SPEC, "--watch"], cwd=cwd, env=full, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        self.buf = bytearray()
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self) -> None:
        fd = self.proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            self.buf.extend(chunk)

    def mark(self) -> int:
        return len(self.buf)

    def seen(self, pattern: str, since: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if re.search(pattern, self.buf[since:].decode(errors="replace")):
                return True
            time.sleep(0.2)
        return False

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)


def _real_run(tmp_path, name):
    s = Scenario(tmp_path, TASKS6, name=name)
    out = h.begin(s.env, s.repo, SPEC, "implement")
    s.key = out["run"]
    return s


def test_tc34_watch_redraws_ends_and_handles_sigint(tmp_path):
    s = _real_run(tmp_path, "watch")
    w = Watcher(s.env, s.repo)
    try:
        assert w.seen(r"running 0", 0, 20), bytes(w.buf)
        since = w.mark()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        h.append_events(s.env, s.key, [{"schema": "ck-progress/1", "at": now, "kind": "task-start", "task": "1",
                                        "attempt": 1, "model": "sonnet"}])
        assert w.seen(r"running 1", since, 5), bytes(w.buf[since:])
        assert h.end(s.env, s.repo, SPEC, "completed").exit_code == 0
        assert w.proc.wait(timeout=10) == 0
        w.thread.join(timeout=5)
        assert "completed" in w.buf.decode(errors="replace")
    finally:
        w.stop()

    s2 = _real_run(tmp_path, "watch-sigint")
    w2 = Watcher(s2.env, s2.repo)
    try:
        assert w2.seen(r"ck eta", 0, 20), bytes(w2.buf)
        w2.proc.send_signal(signal.SIGINT)
        assert w2.proc.wait(timeout=10) == 0
    finally:
        w2.stop()


# --------------------------------------------------------------------------- TC-35


def _state_not_started(tmp_path):
    return Scenario(tmp_path, CHAIN3), 5


def _state_on_track(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    s.add(start_task(1, 1))
    return s, 5


def _state_slipping(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    late = int(s.launch_p90_minute()) + 40
    s.add(start_task(0, 1), start_task(late - 3, 1, "retry", attempt=2))
    return s, late


def _state_blocked(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    s.add(start_task(0, 1), park(5, 1))
    return s, 8


def _state_waiting(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    s.add(start_task(0, 1), approval_start(5))
    return s, 12


def _state_stale(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    s.add(start_task(0, 1))
    return s, 130


def _state_done(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    s.add(start_task(0, 1), merge(5, 1), start_task(5, 2), merge(10, 2), start_task(10, 3), merge(15, 3),
          ev("end", 15, outcome="completed", merged=3, total=3, usd=None))
    return s, 20


STATES = {"not-started": _state_not_started, "on-track": _state_on_track, "slipping": _state_slipping,
          "blocked": _state_blocked, "waiting": _state_waiting, "stale": _state_stale, "done": _state_done}


@pytest.mark.parametrize("state", list(STATES))
def test_tc35_view_schema_for_every_state(tmp_path, state):
    s, minute = STATES[state](tmp_path)
    view = s.view(minute)
    assert re.match(ETA_SCHEMA_RE, view["schema"])
    assert view["state"] == state
    assert view["runKind"] == "implement" and view["spec"] == SPEC
    for key in ("launchAt", "finish", "remainingMinutes", "cost", "confidence", "basis", "criticalPath", "blocked",
                "waitingOnYouMinutes", "staleSince", "notes", "now"):
        assert key in view, key
    assert isinstance(view["tasks"], list) and isinstance(view["phases"], list)
    if view["finish"] is not None:
        for p in ("p10", "p50", "p90"):
            parse(view["finish"][p])
    if view["launchAt"] is not None:
        parse(view["launchAt"])
    if view["remainingMinutes"] is not None:
        assert set(view["remainingMinutes"]) >= {"p10", "p50", "p90"}
    assert isinstance(view["cost"], dict) and set(view["cost"]) >= {"p10", "p50", "p90"}
    assert all(v is None or isinstance(v, (int, float)) for v in view["cost"].values())
    assert view["confidence"] in ("normal", "low", "none")
    assert (view["blocked"] is not None) == (state == "blocked")
    assert (view["waitingOnYouMinutes"] is not None) == (state == "waiting")


# --------------------------------------------------------------------------- TC-36


def test_tc36_cache_hit_then_miss(tmp_path):
    s = Scenario(tmp_path, TASKS6)
    s.begin()
    s.add(start_task(0, 1), start_task(0, 2))
    first = s.view(5)
    assert first["cached"] is False
    second = s.view(5)
    assert second["cached"] is True
    for key in ("cached", "now", "progressFile"):
        first.pop(key, None)
        second.pop(key, None)
    assert first == second
    s.add(merge(6, 1))
    third = s.view(7)
    assert third["cached"] is False


# --------------------------------------------------------------------------- TC-37


def test_tc37_unknown_spec_exits_2_naming_it(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    out = h.eta(s.env, s.repo, "ghost", now=at(0))
    assert out.exit_code == 2
    assert "ghost" in out.output


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions")
def test_tc37_unreadable_progress_file_falls_back_to_the_launch_estimate(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    s.add(start_task(0, 1))
    path = Path(s.view(3, "--seed", "1")["progressFile"])
    path.chmod(0)
    try:
        out = h.eta(s.env, s.repo, SPEC, "--seed", "1", now=at(5))
        assert out.exit_code == 0, out.output
        assert "live progress unavailable" in out.stdout
        assert re.search(r"finish|\d\d:\d\d", out.stdout)
    finally:
        path.chmod(0o600)


# --------------------------------------------------------------------------- TC-38


def _ended(tmp_path, name, done_ids):
    s = Scenario(tmp_path, TASKS6, name=name)
    s.begin()
    evs = []
    for n, t in enumerate([1, 2, 3, 4, 5, 6]):
        evs += [start_task(n * 5 + 0.5 if n else 0, t), merge(n * 5 + 4, t)]
    evs.append(ev("end", 70, outcome="completed", merged=6, total=6, usd=None))
    s.add(*evs)
    mark_done(s.repo, done_ids)
    return s


def _place(s: Scenario) -> str:
    lo, hi = s.launch["remainingMinutes"]["p10"], s.launch["remainingMinutes"]["p90"]
    return "below p10" if 70 < lo else "above p90" if 70 > hi else "within p10-p90"


PLACE_TEXT = {"below p10": "before p10", "above p90": "after p90", "within p10-p90": "inside p10-p90"}


def test_tc38_ended_block(tmp_path):
    s = _ended(tmp_path, "ended", [])
    text = s.text(75)
    view = s.view(75)
    assert view["state"] == "done"
    assert "completed" in text and "16:10" in text and "70 min" in text and "6 merged" in text
    assert "not started" not in text
    assert "$" in text
    place = _place(s)
    assert view["ended"]["vsLaunch"] == place
    assert PLACE_TEXT[place] in text or place in text
    assert view["ended"]["nothingLeft"] is False


def test_tc38_nothing_left_when_every_task_is_done(tmp_path):
    s = _ended(tmp_path, "ended-all", [1, 2, 3, 4, 5, 6])
    text = s.text(75)
    assert "nothing left to run" in text
    assert s.view(75)["ended"]["nothingLeft"] is True
    assert "not started" not in text


def test_tc38_two_open_tasks_show_their_launch_estimate(tmp_path):
    s = _ended(tmp_path, "ended-two", [1, 2, 3, 4])
    view = s.view(75)
    assert view["ended"]["nothingLeft"] is False
    assert view["ended"]["next"] is not None and view["ended"]["next"]["open"] == 2
    assert "not started" not in s.text(75)


# --------------------------------------------------------------------------- TC-94


def test_tc94_stale_after_two_hours(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    s.add(start_task(5, 1))
    notify_out = s.root / "notices.jsonl"
    cmd = h.typed_notify_script(s.root / "bin", notify_out)
    assert h.ck_sub("agents", "set", "run.notify_command", cmd, env=s.env, cwd=s.repo).exit_code == 0

    assert s.view(124)["state"] != "stale"
    view = s.view(126)
    assert view["state"] == "stale" and view["finish"] is None
    assert parse(view["staleSince"]) == when(5)
    assert "stale: no progress since 15:05" in s.text(126)
    assert s.status_line(126).strip() == ""
    assert h.typed_notices(notify_out) == []

    s.add(start_task(130, 2, "task-start"))
    assert s.view(131)["state"] != "stale"


# --------------------------------------------------------------------------- TC-102


def _stale_a(s):
    s.add(start_task(0, 1), approval_start(20))
    return [approval_end(150)], "waiting"


def _stale_b(s):
    s.add(start_task(0, 1), merge(10, 1), start_task(10, 2), ev("hand-landing", 20, task="2"))
    return [merge(150, 2)], "waiting"


def _stale_c(s):
    s.add(start_task(0, 1), park(20, 1))
    return [ev("unpark", 150, task="1")], "blocked"


@pytest.mark.parametrize("setup", [_stale_a, _stale_b, _stale_c], ids=["approval", "hand-landing", "park"])
def test_tc102_stale_wins_over_waiting_and_blocked(tmp_path, setup):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    ending, before = setup(s)
    view = s.view(139)
    assert view["state"] == before
    if before == "waiting":
        assert view["waitingOnYouMinutes"] == 119
        assert "waiting on you 119 min" in s.text(139)
    for minute in (141,):
        view = s.view(minute)
        text = s.text(minute)
        assert view["state"] == "stale"
        assert view["finish"] is None and view["waitingOnYouMinutes"] is None and view["blocked"] is None
        assert parse(view["staleSince"]) == when(20)
        assert "stale: no progress since 15:20" in text
        assert "waiting on you" not in text and "blocked:" not in text
        assert s.status_line(minute).strip() == ""
    s.add(*ending)
    assert s.view(152)["state"] not in ("stale",)
