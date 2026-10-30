"""`ck eta` create estimate: phases, optional second pass, live phases, untimed records, kind selection (TC-16..TC-20)."""

from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict

import pytest

import eta_helpers as h
from eta_history import create_history, install
from eta_strategies import CREATE_PHASES
from stats_helpers import git_project

pytestmark = pytest.mark.usefixtures("no_network")

T0 = datetime.fromisoformat("2026-10-30T15:00:00+00:00")
NOW = "2026-10-30T15:00:00Z"
CONSISTENCY = "Consistency Check"


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def orbit_repo(tmp_path: Path) -> Path:
    repo = git_project(tmp_path, name="orbit-repo")
    (repo / "specs" / "orbit").mkdir(parents=True)
    (repo / "specs" / "orbit" / "PLAN.md").write_text("# Plan: orbit\n")
    return repo


def env_of(xdg: Path) -> Dict[str, str]:
    return {"XDG_DATA_HOME": str(xdg), "CK_AGENT_SLOTS": "4"}


def pool_env(tmp_path: Path) -> Dict[str, str]:
    return env_of(h.install_pool(h.POOL_CREATE, tmp_path / "xdg"))


def view_of(env, repo, *args, now=NOW):
    out = h.eta(env, repo, "orbit", "--json", "--seed", "4", *args, now=now)
    assert out.exit_code == 0, out.output
    return json.loads(out.stdout)


def phase_medians(view) -> Dict[str, float]:
    return {p["phase"]: p["medianMinutes"] for p in view["phases"]}


# --------------------------------------------------------------------------- TC-16


def test_tc16_lists_every_phase_with_finish_and_cost(tmp_path):
    env = pool_env(tmp_path)
    repo = orbit_repo(tmp_path)
    out = h.eta(env, repo, "orbit", now=NOW)
    assert out.exit_code == 0, out.output
    for phase in CREATE_PHASES:
        assert phase in out.stdout, phase
    assert re.search(r"\d{2}:\d{2}\s*[–-]\s*(?:\d{2}:\d{2}\s*[–-]\s*)?\d{2}:\d{2}", out.stdout)
    assert re.search(r"\$\d+(?:\.\d+)?\s*[–-]\s*\$\d+", out.stdout)
    assert "not started" in out.stdout
    view = view_of(env, repo)
    assert view["runKind"] == "create"
    assert [p["phase"] for p in view["phases"]] == list(CREATE_PHASES)
    assert all(p["state"] == "pending" and p["medianMinutes"] > 0 for p in view["phases"])
    assert 55 <= view["remainingMinutes"]["p50"] <= 100


# --------------------------------------------------------------------------- TC-17


def second_pass_view(tmp_path: Path, name: str, second_in: int):
    store = create_history(tmp_path / name, lambda i: i < second_in, runs=8)
    env = env_of(install(store, tmp_path / f"xdg-{name}"))
    return view_of(env, orbit_repo(tmp_path / name / "x"))


def test_tc17_second_pass_is_included_with_its_observed_rate(tmp_path):
    none, some, every = (second_pass_view(tmp_path, f"v{n}", n) for n in (0, 3, 8))
    p50 = [v["remainingMinutes"]["p50"] for v in (none, some, every)]
    assert p50[0] <= p50[1] <= p50[2]
    extra = 0.05 * statistics.median(h.create_total_minutes(i) for i in range(8))
    assert abs((p50[2] - p50[0]) - extra) <= 0.5 * extra
    c = [phase_medians(v)[CONSISTENCY] for v in (none, some, every)]
    assert c[0] <= c[1] <= c[2] and c[0] < c[2]


# --------------------------------------------------------------------------- TC-18


def test_tc18_a_running_create_run_simulates_only_what_is_left(tmp_path):
    env = pool_env(tmp_path)
    repo = orbit_repo(tmp_path)
    started = h.begin(env, repo, "orbit", "create", now=at(0))
    assert started.get("run"), started
    cursor = 0
    for phase in CREATE_PHASES[:3]:
        assert h.event(env, repo, "orbit", "phase-start", now=at(cursor), phase=phase).exit_code == 0
        cursor += 10
        assert h.event(env, repo, "orbit", "step-end", now=at(cursor), phase=phase).exit_code == 0
    assert h.event(env, repo, "orbit", "phase-start", now=at(30), phase=CREATE_PHASES[3]).exit_code == 0
    now = at(40)
    view = view_of(env, repo, now=now)
    states = [p["state"] for p in view["phases"]]
    assert states == ["done"] * 3 + ["running"] + ["pending"] * (len(CREATE_PHASES) - 4)
    assert when(view["finish"]["p10"]) >= when(now)
    assert view["remainingMinutes"]["p10"] > 0
    text = h.eta(env, repo, "orbit", "--seed", "4", now=now).stdout
    assert "✓" in text and "▶" in text
    assert text.count("✓") >= 3


# --------------------------------------------------------------------------- TC-19


def test_tc19_records_without_phase_timing_are_left_out(tmp_path):
    pool = h.build_pool(h.POOL_CREATE)
    untimed = []
    for path in sorted((pool / "xdg-data" / "claude-kiro" / "runs").glob("*.json")):
        rec = json.loads(path.read_text())
        if any(a["startedAtMs"] is None for a in rec["agents"]):
            untimed.append(path.name)
    assert len(untimed) == 2
    without = h.install_pool(h.POOL_CREATE, tmp_path / "a")
    with_untimed = h.install_pool(h.POOL_CREATE, tmp_path / "b")
    for name in untimed:
        (without / "claude-kiro" / "runs" / name).unlink()
        path = with_untimed / "claude-kiro" / "runs" / name
        rec = json.loads(path.read_text())
        rec["phases"] = {k: None for k in rec["phases"]}
        for agent in rec["agents"]:
            agent["startedAtMs"] = None
        path.write_text(json.dumps(rec, indent=2))
    repo = orbit_repo(tmp_path)
    a = phase_medians(view_of(env_of(without), repo))
    b = phase_medians(view_of(env_of(with_untimed), repo))
    assert a == b
    assert all(v and v > 0 for v in b.values())


# --------------------------------------------------------------------------- TC-20


def test_tc20_kind_follows_the_spec_directory(tmp_path):
    env = pool_env(tmp_path)
    repo = orbit_repo(tmp_path)
    assert view_of(env, repo)["runKind"] == "create"
    (repo / "specs" / "orbit" / "tasks.md").write_text(
        (h.eta_spec(tmp_path / "other", [h.TaskDef(1)], spec="orbit") / "specs" / "orbit" / "tasks.md").read_text())
    assert view_of(env, repo)["runKind"] == "implement"
    assert view_of(env, repo, "--kind", "create")["runKind"] == "create"
    (repo / "specs" / "orbit" / "tasks.md").unlink()
    out = h.eta(env, repo, "orbit", "--kind", "implement", now=NOW)
    assert out.exit_code == 1 and "tasks.md" in out.output and "Traceback" not in out.output
