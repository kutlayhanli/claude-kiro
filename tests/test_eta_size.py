"""The task size signal: counts from tasks.md, local records, legacy records, bundled fallback (TC-11..TC-15)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Dict

import pytest

import eta_helpers as h
from eta_history import T, custom_history, install, records
from eta_strategies import SIZE_KEYS
from stats_helpers import flatten_paths, kiro_checkout, stats

pytestmark = pytest.mark.usefixtures("no_network")

NOW = "2026-10-30T15:00:00Z"


def env_for(xdg: Path, **extra: str) -> Dict[str, str]:
    return {"XDG_DATA_HOME": str(xdg), "CK_AGENT_SLOTS": "4", **extra}


def view_of(env, repo, *args):
    out = h.eta(env, repo, "demo", "--json", "--seed", "5", *args, now=NOW)
    assert out.exit_code == 0, out.output
    return json.loads(out.stdout)


def medians(view) -> Dict[str, float]:
    return {t["task"]: t["medianMinutes"] for t in view["tasks"]}


# --------------------------------------------------------------------------- TC-11


def test_tc11_bigger_tasks_take_longer(tmp_path):
    xdg = h.install_pool(h.POOL_IMPL, tmp_path / "xdg")
    repo = h.eta_spec(tmp_path / "ledger", [h.TaskDef(1, model="sonnet", files=1, criteria=1),
                                            h.TaskDef(2, model="sonnet", files=8, criteria=10)])
    view = view_of(env_for(xdg), repo)
    by = medians(view)
    assert by["2"] > by["1"]
    assert view["basis"]["sizeSignal"] == "local"


# --------------------------------------------------------------------------- TC-12


def test_tc12_counts_stay_local_and_never_reach_an_export(tmp_path):
    store = custom_history(tmp_path / "hist", lambda i: [T(files=2, criteria=3), T(files=4, criteria=1), T(files=1, criteria=5)], runs=2)
    env = h._pool_env(tmp_path / "hist")
    local = records(store)
    assert len(local) == 2
    for rec in local:
        assert [(t["filesCount"], t["criteriaCount"]) for t in rec["tasks"]] == [(2, 3), (4, 1), (1, 5)]
    checkout, _bare = kiro_checkout(tmp_path)
    out = stats("export", "--to", str(checkout), env=env)
    assert out.exit_code == 0, out.output
    exported = sorted((checkout / "analytics" / "runs").rglob("*.json"))
    assert len(exported) == 2
    for path in exported:
        paths = flatten_paths(json.loads(path.read_text()))
        for key in SIZE_KEYS:
            assert not [p for p in paths if p.split(".")[-1].split("[]")[0] == key or key in p], (path, key)
        assert not [k for k in SIZE_KEYS if k in path.read_text()]


# --------------------------------------------------------------------------- TC-13


def legacy_store(tmp_path: Path, *, keep_specs: bool) -> Path:
    """POOL_IMPL as it was collected before the size counts: keys deleted, projects copied so specs can go."""
    pool = h.build_pool(h.POOL_IMPL)
    projects = tmp_path / "projects"
    shutil.copytree(pool / "projects", projects)
    xdg = h.install_pool(h.POOL_IMPL, tmp_path / "xdg")
    for path in (xdg / "claude-kiro" / "runs").glob("*.json"):
        data = json.loads(path.read_text().replace(str(pool / "projects"), str(projects)))
        for task in data["tasks"]:
            for key in SIZE_KEYS:
                task.pop(key, None)
        path.write_text(json.dumps(data, indent=2))
    if not keep_specs:
        for proj in projects.iterdir():
            shutil.rmtree(proj / "specs")
    return xdg


def test_tc13_legacy_records_are_sized_from_tasks_md(tmp_path):
    xdg = legacy_store(tmp_path, keep_specs=True)
    repo = h.eta_spec(tmp_path / "ledger", [h.TaskDef(1, model="sonnet", files=1, criteria=1),
                                            h.TaskDef(2, model="sonnet", files=8, criteria=10)])
    view = view_of(env_for(xdg), repo)
    assert view["basis"]["sizeSignal"] == "local"
    assert medians(view)["2"] > medians(view)["1"]


def test_tc13_without_tasks_md_the_tasks_count_but_the_fit_falls_back(tmp_path):
    with_specs = view_of(env_for(legacy_store(tmp_path / "a", keep_specs=True)),
                         h.eta_spec(tmp_path / "ledger-a", [h.TaskDef(1, model="sonnet")]))
    xdg = legacy_store(tmp_path / "b", keep_specs=False)
    view = view_of(env_for(xdg), h.eta_spec(tmp_path / "ledger-b", [h.TaskDef(1, model="sonnet")]))
    assert view["basis"]["tasks"] == with_specs["basis"]["tasks"] > 0
    assert view["basis"]["sizeSignal"] != "local"


# --------------------------------------------------------------------------- TC-14


def test_tc14_few_sized_tasks_use_the_bundled_fit(tmp_path):
    xdg = h.install_pool(h.POOL_SIZED_FEW, tmp_path / "xdg")
    repo = h.eta_spec(tmp_path / "ledger", [h.TaskDef(1, model="sonnet", files=2, criteria=2)])
    view = view_of(env_for(xdg), repo)
    assert any("size" in part for part in view["basis"]["bundledParts"])
    assert view["basis"]["sizeSignal"] == "bundled"


def test_tc14_without_bundled_defaults_the_size_signal_is_off(tmp_path):
    xdg = h.install_pool(h.POOL_SIZED_FEW, tmp_path / "xdg")
    repo = h.eta_spec(tmp_path / "ledger", [h.TaskDef(1, model="sonnet", files=2, criteria=2)])
    env = env_for(xdg, **{h.DEFAULTS_ENV: str(tmp_path / "missing.json")})
    view = view_of(env, repo)
    assert view["basis"]["sizeSignal"] == "off"
    text = h.eta(env, repo, "demo", now=NOW).stdout
    assert "size signal not used" in text or "size signal off" in text


# --------------------------------------------------------------------------- TC-15


def test_tc15_an_unsized_task_is_treated_as_the_smallest(tmp_path):
    xdg = h.install_pool(h.POOL_IMPL, tmp_path / "xdg")
    repo = h.eta_spec(tmp_path / "ledger", [h.TaskDef(1, model="sonnet", files=0, criteria=0),
                                            h.TaskDef(2, model="sonnet", files=1, criteria=1)])
    text = (repo / "specs" / "demo" / "tasks.md").read_text()
    assert "**Requirements:** None" in text
    by = medians(view_of(env_for(xdg), repo))
    assert by["1"] > 0
    # the two tasks draw different random numbers, so allow sampling noise of 10% around "at least"
    assert by["1"] >= 0.9 * by["2"]
