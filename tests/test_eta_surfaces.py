"""The ETA surfaces that read or print one line: progress log lines (TC-53(a), TC-54) and the status line (TC-55, TC-56).

Real boundaries only: the real `ck progress` and `ck eta` subprocesses, real progress files and caches, and the real
`ck-status-line` entry point fed Claude Code's status JSON on stdin. Times are pinned with `CK_NOW` and `TZ=UTC`.
Directory-permission cases are skipped as root.
"""

from __future__ import annotations

import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import pytest

import eta_helpers as h
from eta_strategies import CREATE_PHASES, NO_NETWORK
from stats_helpers import ck_sub

pytestmark = NO_NETWORK

needs_user = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")

T0 = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
START_FORM = re.compile(r"ETA \d\d:\d\d \(p90 \d\d:\d\d\), (critical [\d→ ]+|\d+ phases)$")
LATER_FORM = re.compile(r"^.+ · ETA \d\d:\d\d( \([+-]\d+\))?$")
HHMM = r"\d\d:\d\d"
BAD_WORDS = ("error", "Traceback", "❌")
DESIGN_PHASE = "Design + Test Plan"
STATUS_NOW_MIN = 200  # minutes after T0 at which the status line runs


def stamp(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def base_env() -> Dict[str, str]:
    return {"TZ": "UTC", "CK_AGENT_SLOTS": "4"}


def data_home() -> Path:
    return Path(os.environ["XDG_DATA_HOME"])


def kinds_env() -> Dict[str, str]:
    return {**base_env(), h.TEST_KINDS_ENV: "1"}


# --------------------------------------------------------------------------- TC-53(a), TC-54


def test_tc53a_begin_prints_the_start_form_of_the_eta_line(tmp_path):
    h.install_pool(h.POOL_IMPL, data_home())
    repo = h.eta_spec(tmp_path / "proj", [h.TaskDef(1), h.TaskDef(2, deps=(1,)), h.TaskDef(3, deps=(2,))])
    out = h.begin(base_env(), repo, "demo", "implement", now=stamp(0))
    assert out["run"] and out["file"]
    assert START_FORM.match(out["etaLine"]), out["etaLine"]
    assert out["etaLine"].startswith("ETA ")


def test_tc53a_create_begin_and_step_end_print_the_eta_lines(tmp_path):
    h.install_pool(h.POOL_CREATE, data_home())
    repo = h.eta_spec(tmp_path / "proj", [h.TaskDef(1)], spec="demo")
    (repo / "specs" / "orbit").mkdir()
    (repo / "specs" / "orbit" / "PLAN.md").write_text("# Plan\n")
    out = h.begin(base_env(), repo, "orbit", "create", now=stamp(0))
    assert START_FORM.match(out["etaLine"]), out["etaLine"]
    ev = h.event(base_env(), repo, "orbit", "step-end", now=stamp(10), phase=CREATE_PHASES[0], json=True)
    assert ev.exit_code == 0, ev.output
    body = json.loads(ev.stdout)
    assert body["ok"] is True
    assert LATER_FORM.match(body["eta"]), body["eta"]


def test_tc53a_event_after_a_phase_names_the_phase_in_the_line(tmp_path):
    h.install_pool(h.POOL_CREATE, data_home())
    repo = h.eta_spec(tmp_path / "proj", [h.TaskDef(1)], spec="demo")
    (repo / "specs" / "orbit").mkdir()
    (repo / "specs" / "orbit" / "PLAN.md").write_text("# Plan\n")
    h.begin(base_env(), repo, "orbit", "create", now=stamp(0))
    ev = h.event(base_env(), repo, "orbit", "step-end", now=stamp(12), phase=CREATE_PHASES[0], json=True)
    assert CREATE_PHASES[0] in json.loads(ev.stdout)["eta"]


def test_tc54_tiny_line_timeout_gives_null_eta_and_exit_zero(tmp_path):
    h.install_pool(h.POOL_CREATE, data_home())
    repo = h.eta_spec(tmp_path / "proj", [h.TaskDef(1)], spec="demo")
    (repo / "specs" / "orbit").mkdir()
    (repo / "specs" / "orbit" / "PLAN.md").write_text("# Plan\n")
    env = {**base_env(), h.LINE_TIMEOUT_ENV: "0.001"}
    started = ck_sub(*h.PROGRESS_BEGIN, "orbit", "--kind", "create", "--json", env={**env, h.NOW_ENV: stamp(0)}, cwd=repo)
    assert started.exit_code == 0, started.output
    assert json.loads(started.stdout)["etaLine"] is None
    ev = h.event(env, repo, "orbit", "step-end", now=stamp(10), phase=CREATE_PHASES[0], json=True)
    assert ev.exit_code == 0, ev.output
    assert json.loads(ev.stdout)["eta"] is None


def test_tc54_unreadable_model_snapshot_gives_null_eta_and_exit_zero(tmp_path):
    h.install_pool(h.POOL_CREATE, data_home())
    repo = h.eta_spec(tmp_path / "proj", [h.TaskDef(1)], spec="demo")
    (repo / "specs" / "orbit").mkdir()
    (repo / "specs" / "orbit" / "PLAN.md").write_text("# Plan\n")
    started = h.begin(base_env(), repo, "orbit", "create", now=stamp(0))
    progress_file = Path(started["file"])
    snapshots = sorted(progress_file.parent.glob("*.model.json"))
    assert len(snapshots) == 1
    snapshots[0].write_text("{ this is not a model snapshot")
    ev = h.event(base_env(), repo, "orbit", "step-end", now=stamp(10), phase=CREATE_PHASES[0], json=True)
    assert ev.exit_code == 0, ev.output
    assert json.loads(ev.stdout)["eta"] is None


# --------------------------------------------------------------------------- the status-line world (TC-55, TC-56)


def _add_spec(repo: Path, name: str, tasks: int = 2, chain: bool = True) -> None:
    body = [f"# Implementation Tasks: {name}\n\n## Task Breakdown\n"]
    for n in range(1, tasks + 1):
        dep = f"Task {n - 1}" if chain and n > 1 else "None"
        body.append(f"### Task {n}: Build part {n}\n**Status:** Not Started\n**Track:** impl\n**Requirements:** {n}.1\n"
                    f"**Description:** Part {n}.\n**Files:**\n- `src/{name}/part{n}.py` - part {n}\n"
                    f"**Verify:** `test -f part{n}.txt`\n**Dependencies:** {dep}\n**Complexity:** Low\n\n---\n")
    (repo / "specs" / name).mkdir(parents=True, exist_ok=True)
    (repo / "specs" / name / "tasks.md").write_text("\n".join(body))


def _run(repo: Path, spec: str, kind: str, minute: float) -> dict:
    return h.begin(base_env(), repo, spec, kind, now=stamp(minute))


def _ev(repo: Path, spec: str, kind: str, minute: float, **flags) -> None:
    done = h.event(kinds_env(), repo, spec, kind, now=stamp(minute), **flags)
    assert done.exit_code == 0, done.output


def _cache(repo: Path, spec: str, minute: float = STATUS_NOW_MIN) -> dict:
    return h.eta_json(base_env(), repo, spec, now=stamp(minute))


class World:
    """Project A holds five runs of different shapes plus a stale one; project B holds one more."""

    def __init__(self, root: Path):
        self.a = h.eta_spec(root / "a", [h.TaskDef(i, deps=(i - 1,) if i > 1 else ()) for i in range(1, 7)], spec="demo")
        self.b = h.eta_spec(root / "b", [h.TaskDef(1), h.TaskDef(2, deps=(1,))], spec="other")
        (self.a / "specs" / "orbit").mkdir()
        (self.a / "specs" / "orbit" / "PLAN.md").write_text("# Plan\n")
        for name in ("blk", "wait", "fin", "old"):
            _add_spec(self.a, name)
        self.files: Dict[str, Path] = {}
        now = STATUS_NOW_MIN

        self.files["old"] = Path(_run(self.a, "old", "implement", 10)["file"])  # last event 190 min before the status line
        self.files["demo"] = Path(_run(self.a, "demo", "implement", now - 40)["file"])
        for n in (1, 2, 3):
            _ev(self.a, "demo", "task-start", now - 40 + n, task=n)
            _ev(self.a, "demo", "merge", now - 30 + n, task=n)
        _ev(self.a, "demo", "task-start", now - 20, task=4)
        self.files["orbit"] = Path(_run(self.a, "orbit", "create", now - 30)["file"])
        _ev(self.a, "orbit", "phase-start", now - 25, phase=DESIGN_PHASE)
        self.files["blk"] = Path(_run(self.a, "blk", "implement", now - 25)["file"])
        _ev(self.a, "blk", "task-start", now - 24, task=1)
        _ev(self.a, "blk", "task-failed", now - 10, task=1)
        self.files["wait"] = Path(_run(self.a, "wait", "implement", now - 25)["file"])
        _ev(self.a, "wait", "task-start", now - 24, task=1)
        _ev(self.a, "wait", "approval-start", now - 7, reason="question")
        self.files["fin"] = Path(_run(self.a, "fin", "implement", now - 20)["file"])
        assert h.end(base_env(), self.a, "fin", "completed", now=stamp(now - 5)).exit_code == 0
        self.files["other"] = Path(_run(self.b, "other", "implement", now - 15)["file"])
        self.caches = {name: _cache(self.a, name) for name in ("demo", "orbit", "blk", "wait")}
        _cache(self.b, "other")

    def stdin(self, root: Optional[Path] = None) -> dict:
        return {"workspace": {"current_dir": str(root or self.a)}, "session_id": "s-1", "model": {"display_name": "x"}}

    def env(self, **extra: str) -> Dict[str, str]:
        return {**base_env(), h.NOW_ENV: stamp(STATUS_NOW_MIN), **extra}


@pytest.fixture
def world(tmp_path) -> World:
    h.install_pool(h.POOL_IMPL, data_home())
    h.install_pool(h.POOL_CREATE, data_home())
    return World(tmp_path)


def _lines(out) -> List[str]:
    return [line for line in out.stdout.splitlines() if line.strip()]


# --------------------------------------------------------------------------- TC-55


def test_tc55_prints_exactly_one_line_per_active_run_of_project_a(world):
    out = h.status_line(world.env(), world.stdin())
    assert out.exit_code == 0 and out.stderr == "", out.output
    lines = _lines(out)
    assert len(lines) == 4, lines


def test_tc55_implement_line_has_merged_count_eta_and_cached_cost(world):
    lines = _lines(h.status_line(world.env(), world.stdin()))
    mine = [ln for ln in lines if ln.startswith("ck demo ")]
    assert len(mine) == 1
    m = re.fullmatch(rf"ck demo 3/6 ETA {HHMM} ~\$(\d+)", mine[0])
    assert m, mine[0]
    cost = world.caches["demo"]["cost"]["p50"]
    assert cost is not None
    assert int(m.group(1)) in {round(cost), int(cost + 0.5)}
    finish = world.caches["demo"]["finish"]["p50"]
    assert f"ETA {finish[11:16]}" in mine[0]


def test_tc55_create_blocked_and_waiting_lines(world):
    lines = _lines(h.status_line(world.env(), world.stdin()))
    assert any(re.fullmatch(rf"ck orbit create {re.escape(DESIGN_PHASE)} ETA {HHMM}", ln) for ln in lines), lines
    assert sum(1 for ln in lines if re.fullmatch(r"ck blk .*blocked", ln)) == 1, lines
    assert sum(1 for ln in lines if re.fullmatch(r"ck wait .*waiting on you 7 min", ln)) == 1, lines


def test_tc55_ignores_project_b_ended_and_stale_runs(world):
    out = h.status_line(world.env(), world.stdin()).stdout
    for name in ("other", "fin", "old"):
        assert f"ck {name} " not in out
    only_b = _lines(h.status_line(world.env(), world.stdin(world.b)))
    assert len(only_b) == 1 and only_b[0].startswith("ck other ")


def test_tc55_cost_part_is_left_out_when_the_cached_cost_is_unknown(tmp_path):
    h.install_pool(h.POOL_COSTLESS, data_home())
    repo = h.eta_spec(tmp_path / "a", [h.TaskDef(i, deps=(i - 1,) if i > 1 else ()) for i in range(1, 4)])
    missing = str(tmp_path / "no-such-defaults.json")
    env = {**base_env(), h.DEFAULTS_ENV: missing}
    h.begin(env, repo, "demo", "implement", now=stamp(0))
    for n in (1,):
        assert h.event({**env, h.TEST_KINDS_ENV: "1"}, repo, "demo", "task-start", now=stamp(1), task=n).exit_code == 0
        assert h.event({**env, h.TEST_KINDS_ENV: "1"}, repo, "demo", "merge", now=stamp(5), task=n).exit_code == 0
    view = h.eta_json(env, repo, "demo", now=stamp(6))
    assert view["cost"] is None or view["cost"]["p50"] is None
    out = h.status_line({**env, h.NOW_ENV: stamp(6)}, {"workspace": {"current_dir": str(repo)}})
    lines = _lines(out)
    assert len(lines) == 1 and re.fullmatch(rf"ck demo 1/3 ETA {HHMM}", lines[0]), lines


@needs_user
def test_tc55_output_is_unchanged_without_the_store_and_the_defaults(world):
    before = h.status_line(world.env(), world.stdin())
    runs = data_home() / "claude-kiro" / "runs"
    assert runs.is_dir()
    missing = str(world.a / "no-such-defaults.json")
    runs.chmod(0)
    try:
        after = h.status_line(world.env(**{h.DEFAULTS_ENV: missing}), world.stdin())
    finally:
        runs.chmod(stat.S_IRWXU)
    assert after.exit_code == 0
    assert after.stdout == before.stdout
    assert after.stderr == ""


def test_tc55_output_is_unchanged_with_the_defaults_file_missing(world):
    before = h.status_line(world.env(), world.stdin())
    after = h.status_line(world.env(**{h.DEFAULTS_ENV: str(world.a / "no-such-defaults.json")}), world.stdin())
    assert after.stdout == before.stdout and after.exit_code == 0


# --------------------------------------------------------------------------- TC-56


def test_tc56_no_active_run_prints_nothing(tmp_path):
    repo = h.eta_spec(tmp_path / "a", [h.TaskDef(1)])
    out = h.status_line({**base_env(), h.NOW_ENV: stamp(0)}, {"workspace": {"current_dir": str(repo)}})
    assert out.exit_code == 0
    assert out.stdout == ""
    assert out.stderr == ""


@needs_user
def test_tc56_an_unreadable_progress_file_drops_only_that_run(world):
    victim = world.files["blk"]
    victim.chmod(0)
    try:
        out = h.status_line(world.env(), world.stdin())
    finally:
        victim.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert out.exit_code == 0 and out.stderr == ""
    lines = _lines(out)
    assert len(lines) == 3, lines
    assert not any(ln.startswith("ck blk ") for ln in lines)
    assert any(ln.startswith("ck demo ") for ln in lines)


def test_tc56_a_corrupt_progress_file_drops_only_that_run(world):
    world.files["blk"].write_text("\x00\x01 not json at all\n{broken\n")
    out = h.status_line(world.env(), world.stdin())
    assert out.exit_code == 0 and out.stderr == ""
    lines = _lines(out)
    assert not any(ln.startswith("ck blk ") for ln in lines)
    assert any(ln.startswith("ck demo ") for ln in lines)


@pytest.mark.parametrize("payload", ["", "garbage", "{", "[1, 2, 3]", "null", '{"workspace": 7}', '{"workspace": {"current_dir": 5}}',
                                     "\x00\x01\x02", "{}"])
def test_tc56_garbage_stdin_exits_zero_with_no_error_text(tmp_path, payload):
    repo = h.eta_spec(tmp_path / "a", [h.TaskDef(1)])
    h.begin(base_env(), repo, "demo", "implement", now=stamp(0))
    out = h.status_line({**base_env(), h.NOW_ENV: stamp(1)}, payload, cwd=repo)
    assert out.exit_code == 0, out.output
    assert out.stderr == ""
    for line in _lines(out):
        assert not any(word in line for word in BAD_WORDS), line
