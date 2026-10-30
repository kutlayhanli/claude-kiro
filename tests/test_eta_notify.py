"""Progress-driven notifications (TC-57, TC-59, TC-60 CLI part, TC-61, TC-101).

Every case drives the real `ck progress begin|event|end` and `ck eta --watch` subprocesses against a real notify command
(a script that records each call), real config (`ck agents set run.notify_command`) and real progress files. Message
counts are exact. Times are pinned with `CK_NOW` except the real-time `--watch` cases, where `CK_ETA_RECHECK_S=1` and a
test-level timeout bound the watcher.
"""

from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

import eta_helpers as h
from eta_strategies import NO_NETWORK, progress_lines
from guard_helpers import NOTE, QUOTED_REQ, QUOTED_TEST, clean_env, notify_script, park, set_notify
from stats_helpers import CONFIDENTIAL, CK, spec_repo

pytestmark = NO_NETWORK

T0 = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
SLIP_RE = re.compile(r"^ck: demo ETA \d\d:\d\d \(\+\d+ min, .+\)$")
RECORD = h.impl_plan(0)[0]  # a stored run of POOL_IMPL, so `--record` gives the end event a cost
FAIL_WARNING = "⚠ notification failed"

_KEYS_NOTIFY = """import json, os, sys
payload = json.loads(sys.stdin.read() or "{{}}")
with open({out!r}, "a") as fh:
    fh.write(json.dumps({{"keys": sorted(payload), "type": os.environ.get({env!r})}}) + "\\n")
"""


def stamp(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def base_env() -> Dict[str, str]:
    return {"TZ": "UTC", "CK_AGENT_SLOTS": "4"}


def kinds_env() -> Dict[str, str]:
    return {**base_env(), h.TEST_KINDS_ENV: "1"}


def data_home() -> Path:
    return Path(os.environ["XDG_DATA_HOME"])


def write_spec(repo: Path, name: str, titles: Optional[List[str]] = None, deps: Optional[Dict[int, List[int]]] = None,
               count: int = 3) -> None:
    deps = deps or {n: [n - 1] for n in range(2, count + 1)}
    blocks = [f"# Implementation Tasks: {name}\n\n## Task Breakdown\n"]
    for n in range(1, count + 1):
        title = titles[n - 1] if titles else f"Build part {n}"
        dep = ", ".join(f"Task {d}" for d in deps.get(n, [])) or "None"
        blocks.append(f"### Task {n}: {title}\n**Status:** Not Started\n**Track:** impl\n**Requirements:** {n}.1\n"
                      f"**Description:** Part {n}.\n**Files:**\n- `src/{name}/part{n}.py` - part {n}\n"
                      f"**Verify:** `test -f part{n}.txt`\n**Dependencies:** {dep}\n**Complexity:** Low\n\n---\n")
    (repo / "specs" / name).mkdir(parents=True, exist_ok=True)
    (repo / "specs" / name / "tasks.md").write_text("\n".join(blocks))


class Run:
    """One implement run of `demo` with a recording notify command."""

    def __init__(self, tmp: Path, *, tasks: int = 3, deps: Optional[Dict[int, List[int]]] = None,
                 titles: Optional[List[str]] = None, notify: str = "typed", pool: bool = True, name: str = "proj"):
        if pool:
            h.install_pool(h.POOL_IMPL, data_home())
        self.repo = h.eta_spec(tmp / name, [h.TaskDef(1)], spec="demo")
        write_spec(self.repo, "demo", titles, deps, tasks)
        self.tmp = tmp
        self.out = tmp / f"notices-{name}.jsonl"
        if notify == "typed":
            self.configure(h.typed_notify_script(tmp / "bin", self.out))
        elif notify in ("ok", "fail", "slow"):
            self.configure(notify_script(tmp / "bin", notify, self.tmp / "ok.json"))
        self.p90 = 0.0
        self.file: Optional[Path] = None

    def configure(self, command: str) -> None:
        done = set_notify(command)
        assert done.returncode == 0, done.stdout + done.stderr

    def begin(self, minute: float = 0, kind: str = "implement", spec: str = "demo") -> dict:
        out = h.begin(base_env(), self.repo, spec, kind, now=stamp(minute))
        self.file = Path(out["file"])
        first = progress_lines(out["file"])[0]
        self.p90 = float(first["launch"]["remainingMin"][2])  # the launch p90 the slip is measured from
        return out

    def ev(self, kind: str, minute: float, spec: str = "demo", **flags) -> subprocess.CompletedProcess:
        done = h.event(kinds_env(), self.repo, spec, kind, now=stamp(minute), **flags)
        assert done.exit_code == 0, done.output
        return done

    def end(self, minute: float, outcome: str = "completed", spec: str = "demo", record: Optional[str] = None):
        args = [*h.PROGRESS_END, spec, "--outcome", outcome]
        if record:
            args += ["--record", record]
        done = ck_sub_env(args, self.repo, {**base_env(), h.NOW_ENV: stamp(minute)})
        assert done.exit_code == 0, done.output
        return done

    def notices(self) -> List[Tuple[str, Optional[str]]]:
        return h.typed_notices(self.out)

    def messages(self) -> List[str]:
        return [m for m, _ in self.notices()]


def ck_sub_env(args: List[str], cwd: Path, env: Dict[str, str]):
    from stats_helpers import ck_sub

    return ck_sub(*args, env=env, cwd=cwd, timeout=120)


def start_running(run: Run) -> None:
    run.begin(0)
    run.ev("task-start", 2, task=1)


# --------------------------------------------------------------------------- TC-57(a)


def cached_view(run: Run) -> dict:
    """The EtaView the last event's `after_event` decided on: the run's `<key>.eta.json` sidecar (same directory and stem
    as the progress file), written before `notify_decision` runs. A fresh `ck eta --json` would not do: the `notified`
    line appended after a send changes the events digest, so it would re-simulate with another default seed."""
    assert run.file is not None
    sidecar = run.file.parent / f"{run.file.stem}.eta.json"
    assert sidecar.exists(), sidecar
    body = json.loads(sidecar.read_text())
    return body if "finish" in body else body["view"]


def slip_level(run: Run, waited: float = 0.0, threshold: int = 15) -> Tuple[float, int]:
    """(slip minutes, level) of the view the last event decided on, by S7: slip = p50 finish - waited - launch p90,
    level = the largest whole k with slip > k x threshold (0 when there is none)."""
    view = cached_view(run)
    finish = datetime.fromisoformat(view["finish"]["p50"])
    launch = datetime.fromisoformat(view["launchAt"].replace("Z", "+00:00"))
    slip = (finish - launch).total_seconds() / 60 - waited - run.p90
    return slip, (max(0, math.ceil(slip / threshold) - 1) if slip > 0 else 0)


def sent_slip_levels(run: Run) -> List[Optional[int]]:
    """The `level` of each sent slip, from the run's `notified` events."""
    return [e.get("level") for e in progress_lines(str(run.file))
            if e.get("kind") == "notified" and e.get("notice") == "slip" and e.get("outcome") == "sent"]


def test_tc57a_one_slip_per_new_threshold_level(tmp_path):
    """One message per new 15-minute level of slip. The P+16/P+31/P+47 timeline of TC-57(a) cannot happen literally,
    since the p50 finish is never before now, so it is replaced by the S7 level rule: the expected level after each event
    comes from the cached view that event decided on, with the one-task spec leaving no pending tail to hide in."""
    run = Run(tmp_path, tasks=1)
    start_running(run)
    sent_level = expected = 0
    levels: List[int] = []
    for offset in range(0, 76, 3):
        minute = run.p90 + offset
        run.ev("unpark", minute, task=1)
        slip, level = slip_level(run)
        if level > sent_level:  # a new level sends exactly one message, however many levels it jumped
            sent_level, expected = level, expected + 1
            levels.append(level)
        assert len(run.messages()) == expected, (offset, slip, level, run.messages())
    assert sent_level >= 3, sent_level  # the estimate cannot finish before `now`, so P+75 is at least 60 minutes of slip
    assert sent_slip_levels(run) == levels
    for msg in run.messages():
        assert SLIP_RE.match(msg), msg


def test_tc57a_first_message_names_the_spec_the_new_time_the_change_and_the_cause(tmp_path):
    run = Run(tmp_path)
    start_running(run)
    run.ev("unpark", run.p90 + 16, task=1)
    (msg,) = run.messages()
    assert msg.startswith("ck: demo ETA ")
    assert re.search(r"\(\+\d+ min, [^)]+\)", msg)


def test_tc57a_a_smaller_threshold_sends_the_first_slip_at_p6(tmp_path):
    run = Run(tmp_path)
    from stats_helpers import ck_sub

    done = ck_sub("agents", "set", h.SLIP_KEY, "5", cwd=run.repo)
    assert done.exit_code == 0, done.output
    start_running(run)
    run.ev("unpark", run.p90 + 6, task=1)
    assert len(run.messages()) == 1, run.messages()
    assert SLIP_RE.match(run.messages()[0])


def test_tc57a_approval_minutes_never_count_as_a_slip(tmp_path):
    run = Run(tmp_path, tasks=1)
    start_running(run)
    run.ev("approval-start", 3, reason="question")
    run.ev("unpark", 20, task=1)
    assert run.messages() == []  # nothing while the approval is open
    run.ev("approval-end", 33)  # 30 waited minutes
    sent_level = expected = 0
    levels: List[int] = []
    for minute in (max(run.p90 + 16, 40), max(run.p90 + 30, 50)):
        run.ev("unpark", minute, task=1)
        naive, naive_level = slip_level(run)
        slip, level = slip_level(run, waited=30)
        assert naive_level >= 1, naive  # without the subtraction this would have sent a slip (p50 is never before now)
        assert slip == pytest.approx(naive - 30, abs=0.01)
        if level > sent_level:
            sent_level, expected = level, expected + 1
            levels.append(level)
        assert len(run.messages()) == expected, (minute, slip, level, run.messages())
    assert sent_slip_levels(run) == levels


# --------------------------------------------------------------------------- TC-57(b)


class Watcher:
    """`ck eta demo --watch` as a real subprocess with a test-level timeout."""

    def __init__(self, run: Run, now_minute: float):
        env = {**os.environ, **base_env(), h.NOW_ENV: stamp(now_minute), h.RECHECK_ENV: "1"}
        self.log = open(run.tmp / "watch.log", "w")
        self.proc = subprocess.Popen([*CK, h.ETA_CMD, "demo", "--watch"], cwd=run.repo, env=env, stdin=subprocess.DEVNULL,
                                     stdout=self.log, stderr=subprocess.STDOUT)

    def wait_for(self, run: Run, count: int, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if len(run.messages()) >= count:
                return True
            time.sleep(0.2)
        return len(run.messages()) >= count

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.log.close()


def test_tc57b_plain_eta_never_sends_a_slip(tmp_path):
    run = Run(tmp_path)
    start_running(run)
    out = h.eta(base_env(), run.repo, "demo", now=stamp(run.p90 + 16))
    assert out.exit_code == 0
    assert run.messages() == []


def test_tc57b_the_watcher_sends_exactly_one_time_only_slip(tmp_path):
    run = Run(tmp_path)
    start_running(run)
    watcher = Watcher(run, run.p90 + 16)
    try:
        assert watcher.wait_for(run, 1, 5), "no slip within 5 s"
        time.sleep(3.6)  # three more rechecks
        assert len(run.messages()) == 1, run.messages()
        assert "demo" in run.messages()[0] and "task 1 running" in run.messages()[0]
        assert watcher.proc.poll() is None
    finally:
        watcher.stop()


def test_tc57b_the_watcher_sends_nothing_while_an_approval_is_open(tmp_path):
    run = Run(tmp_path)
    start_running(run)
    run.ev("approval-start", run.p90 + 5, reason="question")
    watcher = Watcher(run, run.p90 + 16)
    try:
        assert not watcher.wait_for(run, 1, 4.5)
        assert run.messages() == []
    finally:
        watcher.stop()


def test_tc57b_without_a_watcher_the_next_event_sends_the_slip(tmp_path):
    run = Run(tmp_path)
    start_running(run)
    run.ev("unpark", run.p90 + 16, task=1)
    assert len(run.messages()) == 1


# --------------------------------------------------------------------------- TC-59


def test_tc59_end_of_implement_sends_one_done_message_with_count_time_and_cost(tmp_path):
    run = Run(tmp_path, tasks=6, deps={n: [n - 1] for n in range(2, 7)})
    run.begin(0)
    for n in range(1, 7):
        run.ev("task-start", n * 3 - 2, task=n)
        run.ev("merge", n * 3, task=n)
    assert run.messages() == []
    run.end(20, record=RECORD)
    (msg,) = run.messages()
    assert re.fullmatch(r"ck: demo done \d\d:\d\d: 6/6 merged, \d+ min, \$\d+(\.\d+)?", msg), msg
    assert "15:20" in msg and "20 min" in msg


def test_tc59_end_of_create_says_spec_written(tmp_path):
    h.install_pool(h.POOL_CREATE, data_home())
    run = Run(tmp_path, pool=False)
    (run.repo / "specs" / "orbit").mkdir()
    (run.repo / "specs" / "orbit" / "PLAN.md").write_text("# Plan\n")
    run.begin(0, kind="create", spec="orbit")
    run.end(30, spec="orbit")
    (msg,) = run.messages()
    assert re.fullmatch(r"ck: orbit done \d\d:\d\d: spec written, 30 min(, \$\d+(\.\d+)?)?", msg), msg


def _superseded_run(tmp_path, second_begin: float, last_event: float) -> Run:
    run = Run(tmp_path, tasks=6, deps={n: [n - 1] for n in range(2, 7)})
    run.begin(0)
    run.ev("task-start", 1, task=1)
    run.ev("task-start", 2, task=2)
    run.ev("merge", last_event - 1, task=1)
    run.ev("merge", last_event, task=2)
    run.begin(second_begin)
    return run


def test_tc59_a_second_begin_sends_one_superseded_message_for_the_first_run(tmp_path):
    run = _superseded_run(tmp_path, 40, 30)
    (msg,) = run.messages()
    assert msg == "ck: demo superseded 15:40: 2/6 merged, 40 min", msg
    run.end(50)
    assert len(run.messages()) == 2
    assert run.messages()[1].startswith("ck: demo done ")


def test_tc59_the_same_holds_when_the_first_run_was_stale(tmp_path):
    run = _superseded_run(tmp_path, 190, 10)
    (msg,) = run.messages()
    assert msg.startswith("ck: demo superseded ") and "2/6 merged" in msg and "$" not in msg, msg
    run.end(200)
    assert len(run.messages()) == 2


# --------------------------------------------------------------------------- TC-101


def _types(run: Run) -> List[Optional[str]]:
    return [t for _, t in run.notices()]


def test_tc101_slip_blocked_and_done_carry_their_own_type(tmp_path):
    slip = Run(tmp_path, name="slip")
    start_running(slip)
    slip.ev("unpark", slip.p90 + 16, task=1)
    assert _types(slip) == ["ck_eta_slip"]

    blocked = Run(tmp_path, name="blocked", deps={2: [1], 3: [1]})
    blocked.begin(0)
    blocked.ev("task-start", 1, task=1)
    blocked.ev("task-failed", 5, task=1)
    assert _types(blocked) == ["ck_eta_blocked"]
    blocked.end(10, outcome="halted")
    assert _types(blocked) == ["ck_eta_blocked", "ck_run_done"]

    done = Run(tmp_path, name="done", tasks=1)
    done.begin(0)
    done.ev("task-start", 1, task=1)
    done.ev("merge", 4, task=1)
    done.end(5)
    assert _types(done) == ["ck_run_done"]


def test_tc101_the_superseded_done_message_is_a_run_done(tmp_path):
    run = _superseded_run(tmp_path, 40, 30)
    assert _types(run) == ["ck_run_done"]


def test_tc101_the_park_message_keeps_ck_needs_input(tmp_path):
    repo = spec_repo(tmp_path / "park", tasks=4)
    out = tmp_path / "park-notices.jsonl"
    assert set_notify(h.typed_notify_script(tmp_path / "bin", out)).returncode == 0
    done = park(repo, "3", NOTE, env=clean_env())
    assert done.returncode == 0, done.stdout + done.stderr
    assert [t for _, t in h.typed_notices(out)] == ["ck_needs_input"]


def test_tc101_the_stdin_keys_are_the_same_for_every_type(tmp_path):
    out = tmp_path / "keys.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "notify-keys"
    script.write_text(f"#!{sys.executable}\n" + _KEYS_NOTIFY.format(out=str(out), env=h.NOTIFY_TYPE_ENV))
    script.chmod(0o755)

    run = Run(tmp_path, name="keys", notify=None, deps={2: [1], 3: [1]})
    run.configure(str(script))
    start_running(run)
    run.ev("unpark", run.p90 + 16, task=1)  # slip
    run.ev("task-failed", run.p90 + 17, task=1)  # blocked
    run.end(run.p90 + 20)  # done
    repo = spec_repo(tmp_path / "park", tasks=4)
    assert park(repo, "3", NOTE, env=clean_env()).returncode == 0  # park
    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert [r["type"] for r in rows] == ["ck_eta_slip", "ck_eta_blocked", "ck_run_done", "ck_needs_input"]
    assert len({tuple(r["keys"]) for r in rows}) == 1, rows


# --------------------------------------------------------------------------- TC-60


def test_tc60_without_a_notify_command_nothing_is_sent_and_every_exit_is_zero(tmp_path):
    run = Run(tmp_path, notify=None)
    start_running(run)
    run.ev("unpark", run.p90 + 16, task=1)
    done = run.end(run.p90 + 20)
    assert FAIL_WARNING not in done.output
    assert run.notices() == []


def test_tc60_a_failing_command_is_named_once_and_the_exit_stays_zero(tmp_path):
    run = Run(tmp_path, notify="fail")
    run.begin(0)
    run.ev("task-start", 1, task=1)
    run.ev("merge", 4, task=1)
    done = run.end(5)
    assert done.exit_code == 0
    lines = [ln for ln in done.output.splitlines() if FAIL_WARNING in ln]  # stdout and stderr: design names no stream
    assert len(lines) == 1, done.output
    assert re.match(r"^⚠ notification failed: .+", lines[0]), lines[0]


def test_tc60_a_slow_command_returns_within_13_seconds(tmp_path):
    run = Run(tmp_path, notify="slow")
    run.begin(0)
    run.ev("task-start", 1, task=1)
    started = time.monotonic()
    run.end(5)
    assert time.monotonic() - started < 13


# --------------------------------------------------------------------------- TC-61


def _seeded(text: str) -> None:
    for value in (*CONFIDENTIAL, QUOTED_TEST, QUOTED_REQ, "tests/test_ledger.py"):
        assert value not in text, (value, text)


def _scan(text: str) -> None:
    flat = re.sub(r"\b\d+/\d+\b", "", text)  # the `3/6` merged count is the one slash a message may hold
    assert "/" not in flat, text
    assert "\\" not in text, text
    assert "test_" not in text, text
    _seeded(text)


def test_tc61_no_message_carries_a_path_a_test_name_or_a_seeded_value(tmp_path):
    titles = [CONFIDENTIAL[1], f"{CONFIDENTIAL[3]} {QUOTED_TEST}", f"{CONFIDENTIAL[4]} {QUOTED_REQ}"]
    run = Run(tmp_path, name="seeded", titles=titles, deps={2: [1], 3: [1]})
    start_running(run)
    run.ev("unpark", run.p90 + 16, task=1)  # slip
    run.ev("task-failed", run.p90 + 17, task=1)  # blocked
    run.ev("park", run.p90 + 18, task=2)  # park event: no message of its own
    run.begin(run.p90 + 40)  # superseded
    done = run.end(run.p90 + 60, record=RECORD)
    repo = spec_repo(tmp_path / "park", tasks=4)
    parked = tmp_path / "parked.jsonl"
    assert set_notify(h.typed_notify_script(tmp_path / "bin2", parked)).returncode == 0
    assert park(repo, "3", NOTE, env=clean_env()).returncode == 0
    messages = run.messages() + [m for m, _ in h.typed_notices(parked)]
    assert len(messages) == 5, messages  # slip, blocked, superseded, done, park
    for text in messages:  # 8.10 covers notification messages; the CLI output may hold paths, but never a seeded value
        _scan(text)
    _seeded(done.output)
