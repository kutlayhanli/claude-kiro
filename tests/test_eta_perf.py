"""Performance and policy NFRs of the ETA feature: TC-85, TC-86, TC-89 (static) and TC-90.

Timing cases run on their own, outside `-n auto` (`uv run pytest -q tests/test_eta_perf.py`), against real pools built by the
real collector, the real `ck` CLI, the real `ck-status-line` entry point and the network guard.

TC-91 (backward compatibility of every existing command, record shape and setting) is covered by the full gate, which runs
the whole existing suite unchanged. It has no test of its own here.
"""

from __future__ import annotations

import ast
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import eta_helpers as h
from eta_strategies import NO_NETWORK
from stats_helpers import ck_sub

pytestmark = NO_NETWORK

TESTS_DIR = Path(__file__).resolve().parent
REAL_XDG = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")  # before any fixture isolates it
NOW = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-10-30T15:00:00Z"
SLOTS = 14
HISTORY_TASKS = 500
ESTIMATE_BUDGET_S = 1.0
CREATE_BUDGET_S = 0.5
CLI_BUDGET_S = 1.5
STATUS_BUDGET_S = 0.1
EVENTS_PER_RUN = 1000
OPT_IN = "CK_ETA_BACKTEST"


def data_home() -> Path:
    return Path(os.environ["XDG_DATA_HOME"])


def perf_env(**extra: str) -> dict:
    return {"TZ": "UTC", "CK_AGENT_SLOTS": str(SLOTS), h.NOW_ENV: NOW_ISO, **extra}


def _median_of(fn, runs: int = 3) -> float:
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return statistics.median(times)


def _double_history(xdg: Path) -> int:
    """Add a renamed copy of every stored run (POOL_IMPL repeated) and return the stored task count."""
    runs = xdg / "claude-kiro" / "runs"
    total = 0
    for path in sorted(runs.glob("*.json")):
        rec = json.loads(path.read_text())
        total += len(rec.get("tasks") or [])
        rec["runId"] = f"{rec['runId']}-again"
        (runs / f"{path.stem}-again.json").write_text(json.dumps(rec))
        total += len(rec.get("tasks") or [])
    return total


def _fifty_task_repo(root: Path) -> Path:
    tasks = [h.TaskDef(n, track="test" if n % 3 == 1 else "impl", deps=(n - 1,) if n % 5 in (2, 3, 4) else (),
                       files=1 + n % 4, criteria=1 + n % 3) for n in range(1, 51)]
    return h.eta_spec(root, tasks, spec="demo")


# --------------------------------------------------------------------------- TC-85


def test_tc85_estimate_of_a_50_task_graph_takes_under_one_second(tmp_path, monkeypatch):
    h.install_pool(h.POOL_IMPL, data_home())
    assert _double_history(data_home()) >= HISTORY_TASKS
    repo = _fifty_task_repo(tmp_path / "proj")
    for key, value in perf_env().items():
        monkeypatch.setenv(key, value)
    from claude_kiro.eta import service

    env = dict(os.environ)
    view = service.estimate(repo, "demo", "implement", now=NOW, seed=1, env=env)
    assert view is not None  # warm-up, and proof the call works
    median = _median_of(lambda: service.estimate(repo, "demo", "implement", now=NOW, seed=1, env=env))
    assert median < ESTIMATE_BUDGET_S, median


def test_tc85_create_estimate_takes_under_half_a_second(tmp_path, monkeypatch):
    h.install_pool(h.POOL_CREATE, data_home())
    repo = h.eta_spec(tmp_path / "proj", [h.TaskDef(1)], spec="demo")
    (repo / "specs" / "orbit").mkdir()
    (repo / "specs" / "orbit" / "PLAN.md").write_text("# Plan\n")
    for key, value in perf_env().items():
        monkeypatch.setenv(key, value)
    from claude_kiro.eta import service

    env = dict(os.environ)
    service.estimate(repo, "orbit", "create", now=NOW, seed=1, env=env)
    median = _median_of(lambda: service.estimate(repo, "orbit", "create", now=NOW, seed=1, env=env))
    assert median < CREATE_BUDGET_S, median


def test_tc85_eta_json_costs_under_one_and_a_half_seconds_over_the_startup(tmp_path):
    h.install_pool(h.POOL_IMPL, data_home())
    _double_history(data_home())
    repo = _fifty_task_repo(tmp_path / "proj")
    baseline = h.baseline_startup_s()
    ck_sub(h.ETA_CMD, "demo", "--json", env=perf_env(), cwd=repo)  # warm the file cache

    def run():
        done = ck_sub(h.ETA_CMD, "demo", "--json", env=perf_env(), cwd=repo)
        assert done.exit_code == 0, done.output
        assert json.loads(done.stdout)["state"] == "not-started"

    median = _median_of(run)
    assert median - baseline < CLI_BUDGET_S, (median, baseline)


# --------------------------------------------------------------------------- TC-86


def _status_world(tmp_path: Path):
    h.install_pool(h.POOL_IMPL, data_home())
    repo = h.eta_spec(tmp_path / "proj", [h.TaskDef(1), h.TaskDef(2, deps=(1,)), h.TaskDef(3, deps=(2,))], spec="demo")
    for name in ("one", "two"):
        spec_dir = repo / "specs" / name
        spec_dir.mkdir()
        (spec_dir / "tasks.md").write_text((repo / "specs" / "demo" / "tasks.md").read_text())
    env = {"TZ": "UTC", "CK_AGENT_SLOTS": "4"}
    for spec in ("demo", "one", "two"):
        started = h.begin(env, repo, spec, "implement", now=NOW_ISO)
        events = [{"schema": "ck-progress/1", "at": f"2026-10-30T15:{1 + i // 60:02d}:{i % 60:02d}Z", "kind": "unpark",
                   "task": "1"} for i in range(EVENTS_PER_RUN - 1)]
        events.insert(0, {"schema": "ck-progress/1", "at": "2026-10-30T15:00:30Z", "kind": "task-start", "task": "1",
                          "attempt": 1, "model": "sonnet"})
        failures = [e for e in h.append_events(env, started["run"], events) if e]
        assert not failures, failures[:3]
        view = h.eta_json(env, repo, spec, now="2026-10-30T15:20:00Z")  # writes the cache
        assert view["state"] != "not-started"
    return repo


@pytest.mark.serial  # a timing budget: runs in the serial verify step, not under -n auto load
def test_tc86_status_line_costs_under_100_ms_over_a_bare_import_and_uses_no_network(tmp_path):
    repo = _status_world(tmp_path)
    stdin = {"workspace": {"current_dir": str(repo)}}
    env = {"TZ": "UTC", h.NOW_ENV: "2026-10-30T15:20:00Z"}
    first = h.status_line(env, stdin)
    assert first.exit_code == 0 and first.stderr == ""
    assert len([ln for ln in first.stdout.splitlines() if ln.strip()]) == 3, first.stdout
    bare = h.bare_startup_s()
    median = _median_of(lambda: h.status_line(env, stdin), runs=5)
    assert median - bare < STATUS_BUDGET_S, (median, bare)
    log = Path(os.environ["CK_TEST_NET_LOG"])
    assert not log.exists() or log.read_text() == ""


def test_tc86_the_entry_point_imports_neither_click_nor_the_cli():
    done = subprocess.run([sys.executable, "-X", "importtime", "-c", "import claude_kiro.statusline"], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    names = [line.split("|")[-1].strip() for line in done.stderr.splitlines() if line.startswith("import time:")]
    assert "claude_kiro.statusline" in names
    loaded = [n for n in names if n == "click" or n.startswith("click.") or n == "claude_kiro.cli" or n.startswith("claude_kiro.cli.")]
    assert loaded == []


# --------------------------------------------------------------------------- TC-89


def _sets_no_network(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        if "pytestmark" in targets:
            dumped = ast.dump(node)
            if "no_network" in dumped or "NO_NETWORK" in dumped:
                return True
    return False


def test_tc89_every_eta_and_lint_verify_test_file_sets_no_network():
    files = sorted([*TESTS_DIR.glob("test_eta_*.py"), *TESTS_DIR.glob("test_lint_verify_*.py")])
    assert TESTS_DIR / "test_eta_perf.py" in files
    missing = [p.name for p in files if not _sets_no_network(p)]
    assert missing == [], missing


# --------------------------------------------------------------------------- TC-90


def test_tc90_the_backtest_over_the_pools_prints_runs_inside_and_rate():
    h.install_pool(h.POOL_IMPL, data_home())
    h.install_pool(h.POOL_CREATE, data_home())
    done = ck_sub(*h.BACKTEST_CMD, "--json", env={"TZ": "UTC"})
    assert done.exit_code == 0, done.output
    body = json.loads(done.stdout)
    assert {"runs", "inside", "rate"} <= set(body)
    assert body["runs"] == h.IMPL_RUNS + h.CREATE_RUNS
    assert 0 <= body["inside"] <= body["runs"]
    assert 0 <= body["rate"] <= 1


@pytest.mark.skipif(os.environ.get(OPT_IN) != "1", reason=f"opt-in calibration check: set {OPT_IN}=1")
def test_tc90_calibration_over_the_real_local_history_is_at_least_60_percent():
    done = ck_sub(*h.BACKTEST_CMD, "--json", env={"TZ": "UTC", "XDG_DATA_HOME": REAL_XDG})
    assert done.exit_code == 0, done.output
    body = json.loads(done.stdout)
    print(f"eta backtest over the local history: {body}")
    assert body["rate"] >= 0.60, body
