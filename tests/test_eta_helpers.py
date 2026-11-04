"""Smoke test of the ETA harness (`tests/eta_helpers.py`): pools, spec builder and import hygiene.

Runs against today's code: it exercises only the existing collector and `ck plan`.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import eta_helpers as h
from stats_helpers import ck_sub

pytestmark = pytest.mark.usefixtures("no_network")


def _ranks(values):
    order = sorted(range(len(values)), key=lambda k: values[k])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs, ys):
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den


def test_impl_pool_has_48_runs_with_the_collector_shape():
    records = h.history_records(h.POOL_IMPL)
    assert len(records) == h.IMPL_RUNS
    for rec in records:
        assert rec["workflow"] == "spec-implement"
        assert 4 <= len(rec["tasks"]) <= 8
        assert all(t["agentMinutes"] is not None and t["riskTag"] in ("normal", "safety") for t in rec["tasks"])
    tasks = [t for rec in records for t in rec["tasks"]]
    retried = sum(1 for t in tasks if t["attempts"] > 1) / len(tasks)
    escalated = sum(1 for t in tasks if t["escalations"]) / len(tasks)
    assert 0.08 <= retried <= 0.3
    assert 0.02 <= escalated <= 0.12
    waiting = sum(1 for rec in records if any((t["reviewWaitMs"] or 0) + (t["landingWaitMs"] or 0) > 0 for t in rec["tasks"]))
    assert waiting == h.IMPL_RUNS // 2


def test_impl_pool_agent_minutes_rise_with_size():
    by_run = {rec["runId"]: rec for rec in h.history_records(h.POOL_IMPL)}
    sizes, minutes = [], []
    for i in range(h.IMPL_RUNS):
        run_id, _spec, plan = h.impl_plan(i)
        recorded = {str(t["task"]): t for t in by_run[run_id]["tasks"]}
        for p in plan:
            rec = recorded[str(p.defn.id)]
            if rec["attempts"] == 1:
                sizes.append(p.defn.files + p.defn.criteria)
                minutes.append(rec["agentMinutes"])
    assert len(sizes) > 100
    assert spearman(sizes, minutes) > 0.5


def test_impl_nowait_pool_holds_only_runs_without_waits():
    records = h.history_records(h.POOL_IMPL_NOWAIT)
    assert len(records) == h.IMPL_RUNS // 2
    for rec in records:
        for t in rec["tasks"]:
            assert (t["reviewWaitMs"] or 0) == 0 and (t["landingWaitMs"] or 0) == 0
    all_ids = {r["runId"] for r in h.history_records(h.POOL_IMPL)}
    assert {r["runId"] for r in records} < all_ids


def test_create_pool_phases_and_totals():
    records = h.history_records(h.POOL_CREATE)
    assert len(records) == h.CREATE_RUNS
    phases = {"Requirements", "Design + Test Plan", "Task Tracks", "Link", "Adversarial Review", "Revise", "Consistency Check"}
    for rec in records:
        assert rec["workflow"] == "spec-create"
        assert set(rec["phases"]) == phases
        assert 58 * 60_000 * 0.99 <= rec["wallMs"] <= 96 * 60_000 * 1.01
    passes = sorted(rec["consistency"]["passes"] for rec in records)
    assert passes.count(2) == 3
    untimed = [rec for rec in records if any(a["startedAtMs"] is None for a in rec["agents"])]
    assert len(untimed) == 2


def test_costless_pool_has_no_usage():
    records = h.history_records(h.POOL_COSTLESS)
    assert len(records) == 10
    for rec in records:
        assert all(c.get("usd") in (None, 0, 0.0) for c in rec["costs"])
        assert all((a.get("usd") in (None, 0, 0.0)) for a in rec["agents"])


def test_sized_few_pool_keeps_the_specs_of_four_runs_only():
    records = h.history_records(h.POOL_SIZED_FEW)
    assert len(records) == h.SIZED_RUNS + h.UNSIZED_RUNS
    sized = [rec for rec in records if rec["runId"] in {h.impl_plan(i, "sized")[0] for i in range(h.SIZED_RUNS)}]
    assert len(sized) == h.SIZED_RUNS


def test_pool_path_depends_only_on_the_sources_so_workers_share_bytes():
    pool = h.build_pool(h.POOL_CREATE)
    assert pool.parent == h.pool_root()
    assert (pool / h._DIGEST).read_text() == h._digest_of(pool)
    assert all(str(pool.parent) in json.dumps(rec) for rec in h.history_records(h.POOL_CREATE))


def test_install_pool_copies_into_a_test_store(tmp_path):
    h.install_pool(h.POOL_CREATE, tmp_path / "xdg")
    assert len(list((tmp_path / "xdg" / "claude-kiro" / "runs").glob("*.json"))) == h.CREATE_RUNS


def test_eta_spec_parses_under_ck_plan(tmp_path):
    tasks = [h.TaskDef(1, files=2, criteria=3), h.TaskDef(2, "test", files=1, criteria=2, deps=(1,), verifies=(1,)),
             h.TaskDef(3, risk="safety", deps=(1,))]
    repo = h.eta_spec(tmp_path / "ledger", tasks, done=(1,))
    out = ck_sub("plan", "demo", "--json", cwd=repo, env={"TZ": "UTC"})
    assert out.exit_code == 0, out.output
    plan = json.loads(out.stdout)
    assert plan["done"] == ["1"] or plan["done"] == [1]
    text = (repo / "specs" / "demo" / "tasks.md").read_text()
    from claude_kiro.hooks._shared.spec_parser import parse_tasks_text

    parsed = {t.num: t for t in parse_tasks_text(text)}
    assert len(parsed["1"].files) == 2 and len(parsed["2"].files) == 1
    assert parsed["2"].track == "test" and parsed["3"].risk == "safety"
    assert parsed["1"].status == "Done" and parsed["2"].dependencies == ["1"]
    assert text.count("**Requirements:** 1.1, 1.2, 1.3") == 1


def test_helpers_module_imports_no_cli():
    tree = ast.parse((Path(__file__).parent / "eta_helpers.py").read_text())
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
            names += [f"{node.module}.{a.name}" for a in node.names]
    assert not [n for n in names if n.startswith("claude_kiro.cli")]
    module_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not [n for n in module_level if getattr(n, "module", "") and n.module.startswith("claude_kiro")]
