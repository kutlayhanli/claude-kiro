"""`ck eta` implement estimate: slot cap, history ladder, determinism, errors, cost (TC-1..TC-10).

Every case runs the real `ck eta` subprocess against an `eta_spec` repo and a store copied from a collected pool.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import pytest

import eta_helpers as h
from eta_history import T, custom_history, install
from eta_strategies import ETA_SCHEMA_RE, MATCH_STEPS
from stats_helpers import ck_sub

pytestmark = pytest.mark.usefixtures("no_network")

NOW = "2026-10-30T15:00:00Z"
RANGE = r"\d{2}:\d{2}\s*[–-]\s*(?:\d{2}:\d{2}\s*[–-]\s*)?\d{2}:\d{2}"
COST = r"\$\d+(?:\.\d+)?\s*[–-]\s*\$\d+(?:\.\d+)?"


def store_env(tmp_path: Path, pool: Optional[str] = h.POOL_IMPL, slots: int = 4, **extra: str) -> Dict[str, str]:
    xdg = tmp_path / "xdg"
    xdg.mkdir(parents=True, exist_ok=True)
    if pool:
        h.install_pool(pool, xdg)
    return {"XDG_DATA_HOME": str(xdg), "CK_AGENT_SLOTS": str(slots), **extra}


def when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def spec_tasks_one():
    return [h.TaskDef(1, model="sonnet"), h.TaskDef(2, model="sonnet", deps=(1,)), h.TaskDef(3, model="sonnet", deps=(2,)),
            h.TaskDef(4, model="sonnet", risk="safety"), h.TaskDef(5, model="sonnet", deps=(3, 4)), h.TaskDef(6, model="sonnet")]


def estimate(env, repo, *args, now=NOW):
    out = h.eta(env, repo, "demo", "--json", *args, now=now)
    assert out.exit_code == 0, out.output
    return json.loads(out.stdout)


# --------------------------------------------------------------------------- TC-1


def test_tc1_text_and_json_launch_estimate(tmp_path):
    env = store_env(tmp_path, slots=2)
    repo = h.eta_spec(tmp_path / "ledger", spec_tasks_one())
    out = h.eta(env, repo, "demo", now=NOW)
    assert out.exit_code == 0, out.output
    text = out.stdout
    assert re.search(r"safety-tagged:\s*1\b", text)
    assert "demo" in text and "6 open" in text and "2 slots" in text
    assert re.search(RANGE, text) and re.search(r"\d+ min", text)
    assert re.search(COST, text)
    for needle in ("slowest:", "safety-tagged:", "retry rate", "escalation rate", "not started"):
        assert needle in text, needle
    assert re.search(r"basis: \d+ past tasks, \d+ runs", text)
    plan = json.loads(ck_sub("plan", "demo", "--json", cwd=repo, env={"TZ": "UTC"}).stdout)
    view = estimate(env, repo, "--seed", "1")
    assert view["state"] == "not-started" and view["slots"] == 2
    assert re.match(ETA_SCHEMA_RE, view["schema"])
    assert view["criticalPath"]["tasks"] == [str(t) for t in plan["criticalPath"]]
    assert view["criticalPath"]["medianMinutes"] > 0
    assert " → ".join(view["criticalPath"]["tasks"]) in text or "→".join(view["criticalPath"]["tasks"]) in text
    f = [when(view["finish"][k]) for k in ("p10", "p50", "p90")]
    assert f[0] <= f[1] <= f[2]
    assert len(view["slowest"]) == 2
    medians = sorted((t["medianMinutes"] for t in view["tasks"]), reverse=True)
    assert [s["medianMinutes"] for s in view["slowest"]] == medians[:2]
    assert view["safetyTagged"] == 1 and view["open"] == 6
    assert 0 <= view["retryRate"] <= 1 and 0 <= view["escalationRate"] <= 1
    assert view["basis"]["tasks"] > 0 and view["basis"]["runs"] > 0
    c = view["cost"]
    assert 0 < c["p10"] <= c["p90"]


def test_tc1_json_equals_the_in_process_service(tmp_path, monkeypatch):
    env = store_env(tmp_path, slots=2)
    repo = h.eta_spec(tmp_path / "ledger", spec_tasks_one())
    cli = estimate(env, repo, "--seed", "11")
    for k, v in {**env, "TZ": "UTC"}.items():
        monkeypatch.setenv(k, v)
    from claude_kiro.eta import service

    view = service.estimate(repo, "demo", None, now=when(NOW), seed=11, env={**env, "TZ": "UTC"})
    if not isinstance(view, dict):
        import dataclasses

        view = view.to_json() if hasattr(view, "to_json") else dataclasses.asdict(view)
    for key in ("now", "cached", "progressFile"):
        cli.pop(key, None)
        view.pop(key, None)
    assert set(cli) == set(view)
    for key in cli:
        assert json.loads(json.dumps(view[key])) == cli[key], key


# --------------------------------------------------------------------------- TC-2


def test_tc2_one_slot_is_never_faster_than_eight(tmp_path):
    repo = h.eta_spec(tmp_path / "ledger", [h.TaskDef(i, model="sonnet") for i in range(1, 9)])
    one = estimate(store_env(tmp_path / "a", h.POOL_IMPL_NOWAIT, slots=1), repo, "--seed", "1")
    eight = estimate(store_env(tmp_path / "b", h.POOL_IMPL_NOWAIT, slots=8), repo, "--seed", "1")
    assert one["slots"] == 1 and eight["slots"] == 8
    for q in ("p10", "p50", "p90"):
        assert when(one["finish"][q]) >= when(eight["finish"][q]), q
        assert one["remainingMinutes"][q] >= eight["remainingMinutes"][q], q
    total = sum(t["medianMinutes"] for t in one["tasks"])
    assert len(one["tasks"]) == 8
    assert one["remainingMinutes"]["p50"] >= 0.8 * total


# --------------------------------------------------------------------------- TC-3


def test_tc3_only_open_tasks_count(tmp_path):
    env = store_env(tmp_path, slots=2)
    repo = h.eta_spec(tmp_path / "ledger", spec_tasks_one(), done=(1, 2))
    out = h.eta(env, repo, "demo", now=NOW)
    assert "4 open" in out.stdout
    view = estimate(env, repo, "--seed", "1")
    assert view["open"] == 4
    # Done tasks may be listed as merged (design does not say); every task still to run is one of the open ones.
    assert {t["task"] for t in view["tasks"] if t.get("state") != "merged"} == {"3", "4", "5", "6"}
    assert all(t["task"] in {"1", "2"} for t in view["tasks"] if t.get("state") == "merged")
    assert not {"1", "2"} & set(view["criticalPath"]["tasks"])


# --------------------------------------------------------------------------- TC-4


def _history_tasks_env(tmp_path, make_run, runs=12):
    store = custom_history(tmp_path / "hist", make_run, runs)
    xdg = install(store, tmp_path / "xdg")
    return {"XDG_DATA_HOME": str(xdg), "CK_AGENT_SLOTS": "4"}


def test_tc4a_safety_opus_tasks_take_longer(tmp_path):
    env = _history_tasks_env(tmp_path, lambda i: [T(model="opus", risk="safety", minutes=30), T(minutes=3), T(minutes=3), T(minutes=3)])
    repo = h.eta_spec(tmp_path / "ledger", [h.TaskDef(1, model="opus", risk="safety"), h.TaskDef(2, model="sonnet")])
    view = estimate(env, repo, "--seed", "3")
    by = {t["task"]: t["medianMinutes"] for t in view["tasks"]}
    assert by["1"] > 5 * by["2"], by


def test_tc4b_retries_raise_the_tail(tmp_path):
    repo = h.eta_spec(tmp_path / "ledger", [h.TaskDef(1, model="sonnet"), h.TaskDef(2, model="sonnet", deps=(1,)),
                                            h.TaskDef(3, model="sonnet", deps=(2,))])
    with_retries = _history_tasks_env(tmp_path / "r", lambda i: [T(attempts=2 if i % 2 else 1) for _ in range(4)])
    without = _history_tasks_env(tmp_path / "n", lambda i: [T(attempts=1) for _ in range(4)])
    a = estimate(with_retries, repo, "--seed", "3")
    b = estimate(without, repo, "--seed", "3")
    assert a["remainingMinutes"]["p90"] > b["remainingMinutes"]["p90"]
    assert a["retryRate"] > b["retryRate"]


def test_tc4c_review_and_landing_waits_add_time(tmp_path):
    repo = h.eta_spec(tmp_path / "ledger", [h.TaskDef(1, model="sonnet")])
    waits = _history_tasks_env(tmp_path / "w", lambda i: [T(wait=10) for _ in range(4)])
    plain = _history_tasks_env(tmp_path / "p", lambda i: [T(wait=0) for _ in range(4)])
    a = estimate(waits, repo, "--seed", "3")
    b = estimate(plain, repo, "--seed", "3")
    assert a["remainingMinutes"]["p50"] - b["remainingMinutes"]["p50"] >= 10


# --------------------------------------------------------------------------- TC-5


def _all(model="sonnet", risk="normal", track="impl"):
    return lambda i: [T(track=track, model=model, risk=risk) for _ in range(4)]


LADDER = [
    ("track+model+risk", _all(), 12),
    ("track+risk", _all(model="opus"), 12),
    ("track", _all(risk="safety"), 12),
    ("all", _all(track="test"), 12),
    ("bundled", lambda i: [T(), T()], 1),
]


@pytest.mark.parametrize("step,make_run,runs", LADDER, ids=[s for s, _, _ in LADDER])
def test_tc5_widening_ladder(tmp_path, step, make_run, runs):
    assert step in MATCH_STEPS
    env = _history_tasks_env(tmp_path, make_run, runs)
    repo = h.eta_spec(tmp_path / "ledger", [h.TaskDef(1, model="sonnet")])
    view = estimate(env, repo, "--seed", "2")
    assert view["basis"]["widestMatch"] == step
    if step != "track+model+risk":
        out = h.eta(env, repo, "demo", now=NOW)
        assert step in out.stdout


# --------------------------------------------------------------------------- TC-6


def test_tc6_seed_makes_output_byte_identical(tmp_path):
    env = store_env(tmp_path)
    repo = h.eta_spec(tmp_path / "ledger", spec_tasks_one())
    a = h.eta(env, repo, "demo", "--seed", "7", now=NOW)
    b = h.eta(env, repo, "demo", "--seed", "7", now=NOW)
    assert a.exit_code == 0 and a.stdout == b.stdout
    ja = h.eta_json(env, repo, "demo", "--seed", "7", now=NOW)
    jb = h.eta_json(env, repo, "demo", "--seed", "7", now=NOW)
    ja.pop("cached", None), jb.pop("cached", None)
    assert ja == jb


def test_tc6_without_a_seed_quantiles_drift_at_most_a_minute(tmp_path):
    env = store_env(tmp_path, slots=1)
    repo = h.eta_spec(tmp_path / "ledger", [h.TaskDef(i, model="sonnet", files=3, criteria=3) for i in range(1, 9)])
    a = estimate(env, repo)
    b = estimate(env, repo)
    assert a["remainingMinutes"]["p50"] > 20
    for q in ("p10", "p50", "p90"):
        assert abs(a["remainingMinutes"][q] - b["remainingMinutes"][q]) <= 1.0, q


# --------------------------------------------------------------------------- TC-7


def test_tc7a_no_tasks_md_is_an_error_for_implement(tmp_path):
    repo = h.eta_spec(tmp_path / "ledger", [h.TaskDef(1)])
    (repo / "specs" / "demo" / "tasks.md").unlink()
    (repo / "specs" / "demo" / "requirements.md").write_text("# Requirements\n")
    out = h.eta(store_env(tmp_path), repo, "demo", "--kind", "implement", now=NOW)
    assert out.exit_code == 1, out.output
    assert "no implement estimate for demo" in out.output and "tasks.md" in out.output
    assert "Traceback" not in out.output


def test_tc7b_tasks_md_without_task_blocks_is_an_error(tmp_path):
    repo = h.eta_spec(tmp_path / "ledger", [h.TaskDef(1)])
    (repo / "specs" / "demo" / "tasks.md").write_text("# Implementation Tasks: demo\n\nNothing here yet.\n")
    out = h.eta(store_env(tmp_path), repo, "demo", now=NOW)
    assert out.exit_code == 1, out.output
    assert "no implement estimate for demo" in out.output and "tasks.md" in out.output
    assert "Traceback" not in out.output


def test_tc7c_everything_done_prints_nothing_left(tmp_path):
    repo = h.eta_spec(tmp_path / "ledger", [h.TaskDef(1), h.TaskDef(2)], done=(1, 2))
    out = h.eta(store_env(tmp_path), repo, "demo", now=NOW)
    assert out.exit_code == 0, out.output
    assert "nothing left to run" in out.stdout


# --------------------------------------------------------------------------- TC-8


def test_tc8_cycle_is_blocked_and_names_both_tasks(tmp_path):
    repo = h.eta_spec(tmp_path / "ledger", [h.TaskDef(1), h.TaskDef(2, deps=(3,)), h.TaskDef(3, deps=(2,))])
    env = store_env(tmp_path)
    out = h.eta(env, repo, "demo", now=NOW)
    assert out.exit_code == 0, out.output
    blocked = [line for line in out.stdout.splitlines() if "blocked:" in line]
    assert blocked and "2" in blocked[0] and "3" in blocked[0]
    assert not re.search(r"\bfinish\s+\d{2}:\d{2}", out.stdout) and "ETA" not in out.stdout
    view = estimate(env, repo)
    assert view["state"] == "blocked" and view["finish"] is None
    assert "2" in view["blocked"]["reason"] and "3" in view["blocked"]["reason"]
    assert {"2", "3"} <= set(view["blocked"]["held"])


def test_tc8_unknown_dependency_is_blocked(tmp_path):
    repo = h.eta_spec(tmp_path / "ledger", [h.TaskDef(1), h.TaskDef(2, deps=(99,))])
    env = store_env(tmp_path)
    out = h.eta(env, repo, "demo", now=NOW)
    assert out.exit_code == 0, out.output
    blocked = [line for line in out.stdout.splitlines() if "blocked:" in line]
    assert blocked and "99" in blocked[0] and "2" in blocked[0]
    view = estimate(env, repo)
    assert view["state"] == "blocked" and view["finish"] is None
    assert "99" in view["blocked"]["reason"]


# --------------------------------------------------------------------------- TC-9


# Config files nest roles under "agents"; both layers below make `agents.resolve` raise AgentConfigError (design 1.8).
@pytest.mark.parametrize("bad", ['{"agents": {"run": "x"}}', '{"agents": {"run": {"max_concurrent": "zero"}}}'],
                         ids=["run-not-a-table", "cap-not-a-number"])
def test_tc9_unreadable_slot_cap_uses_the_default(tmp_path, bad):
    env = store_env(tmp_path, slots=3)
    repo = h.eta_spec(tmp_path / "ledger", spec_tasks_one())
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    env["CK_CONFIG_HOME"] = str(cfg)
    good = ck_sub("agents", "--json", env=env, cwd=repo)
    assert good.exit_code == 0, good.output
    expected = json.loads(good.stdout)["runtime"]["agentSlots"]
    (cfg / "config.json").write_text(bad)
    assert ck_sub("agents", "--json", env=env, cwd=repo).exit_code != 0
    view = estimate(env, repo)
    assert view["slotsSource"] == "default"
    assert view["slots"] == expected
    assert any(re.search(r"slot cap unreadable: using the default \d+", n) for n in view["notes"]), view["notes"]


# --------------------------------------------------------------------------- TC-10


def test_tc10_unpriced_history_shows_cost_unknown(tmp_path):
    env = store_env(tmp_path, h.POOL_COSTLESS)
    repo = h.eta_spec(tmp_path / "ledger", spec_tasks_one())
    out = h.eta(env, repo, "demo", now=NOW)
    assert out.exit_code == 0, out.output
    assert "cost unknown" in out.stdout and "$0" not in out.stdout
    cost = estimate(env, repo, "--seed", "1")["cost"]
    assert cost == {"p10": None, "p50": None, "p90": None}


def test_tc10_adding_priced_history_gives_a_cost(tmp_path):
    env = store_env(tmp_path, h.POOL_COSTLESS)
    h.install_pool(h.POOL_IMPL, tmp_path / "xdg")
    repo = h.eta_spec(tmp_path / "ledger", spec_tasks_one())
    cost = estimate(env, repo, "--seed", "1")["cost"]
    assert cost["p10"] > 0
