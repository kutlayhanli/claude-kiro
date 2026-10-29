"""Smoke tests for the landing-speed test harness itself (fake runtime, bridge, ck plan helpers)."""

import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings

from landing_bridge import BridgeError, bridge, ck_plan, dags, git_repo, scenarios, write_spec  # noqa: F401

JS = Path(__file__).parent / "js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
SETTINGS = settings(derandomize=True, max_examples=25, database=None, deadline=None)


def starts_and_ends(trace):
    return [(e["ev"], e["label"]) for e in trace if e["ev"] in ("start", "end")]


@needs_node
def test_slots_one_never_overlaps_and_starts_fifo(bridge):
    project = {"deps": {"1": [], "2": [], "3": [], "4": []}, "durations": {"1": 8, "2": 8, "3": 8, "4": 8}}
    out = bridge.run(project, {"maxConcurrent": 4}, slots=1)
    active, peak = 0, 0
    for ev, _ in starts_and_ends(out["trace"]):
        active += 1 if ev == "start" else -1
        peak = max(peak, active)
    assert peak == 1
    requests = [e["label"] for e in out["trace"] if e["ev"] == "request"]
    starts = [e["label"] for e in out["trace"] if e["ev"] == "start"]
    assert starts == requests  # FIFO: started in the order requested
    assert [e["t"] for e in out["trace"]] == sorted(e["t"] for e in out["trace"])


@needs_node
def test_without_slots_nothing_queues(bridge):
    project = {"deps": {"1": [], "2": [], "3": []}, "durations": {"1": 20, "2": 20, "3": 20}}
    out = bridge.run(project, {"maxConcurrent": 3})
    labels = [e["label"] for e in out["trace"]]
    first_end = next(i for i, e in enumerate(out["trace"]) if e["ev"] == "end" and e["label"].startswith("task"))
    assert {e["label"] for e in out["trace"][:first_end] if e["ev"] == "start"} >= {"task 1", "task 2", "task 3"}, labels


@needs_node
def test_bridge_run_round_trips_a_two_task_scenario(bridge):
    out = bridge.run({"deps": {"1": [], "2": ["1"]}}, {})
    assert sorted(out["result"]["merged"]) == ["1", "2"]
    assert "task 2" in out["events"] and "task 1" in out["prompts"]
    assert out["trace"] and out["logs"] is not None


@needs_node
def test_bridge_reports_errors_as_ok_false(bridge):
    out = bridge.request({"op": "nope"})
    assert out["ok"] is False and "unknown op" in out["error"]
    assert bridge.request({"op": "ready", "deps": {}, "done": [], "exclude": []}) is not None  # still alive


@needs_node
def test_load_decisions_names_the_missing_marker(tmp_path):
    script = tmp_path / "no-markers.js"
    script.write_text("function readyTasks() {}\n")
    code = f"import('{JS}/load_decisions.mjs').then(m => m.loadDecisions({str(script)!r})).catch(e => {{ console.log(e.message); process.exit(3) }})"
    proc = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 3
    assert "decision marker missing" in proc.stdout and "decisions (pure; loaded by tests)" in proc.stdout


@needs_node
def test_load_decisions_names_a_missing_function(tmp_path):
    script = tmp_path / "partial.js"
    script.write_text("// --- decisions (pure; loaded by tests) ---\nfunction readyTasks() {}\n// --- end decisions ---\n")
    code = f"import('{JS}/load_decisions.mjs').then(m => m.loadDecisions({str(script)!r})).catch(e => {{ console.log(e.message); process.exit(3) }})"
    proc = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 3 and "chainLengths" in proc.stdout


@needs_node
def test_bridge_reports_a_script_without_markers_as_ok_false(bridge, tmp_path):
    script = tmp_path / "no-markers.js"
    script.write_text("function readyTasks() {}\n")
    out = bridge.request({"op": "ready", "script": str(script), "order": ["1"], "deps": {"1": []}, "done": [], "exclude": []})
    assert out["ok"] is False and "decision marker missing" in out["error"]


@needs_node
@pytest.mark.parametrize("state", ["CREATED", "EXISTS"])
def test_fake_worktree_created_or_exists_ends_once_after_its_request(bridge, state):
    out = bridge.run({"deps": {"1": [], "2": ["1"]}, "durations": {"1": 20}, "worktree": {"1": {"state": state}}})
    assert out["events"].count("task 1 end") == 1
    assert out["events"].index("task 1") < out["events"].index("task 1 end") < out["events"].index("task 2")
    assert sorted(out["result"]["merged"]) == ["1", "2"]


@needs_node
def test_bridge_ready_fails_clearly_on_todays_script_or_returns_a_list(bridge):
    # Before the decisions block exists this is the expected failure; after, it must answer.
    try:
        ready = bridge.ready(deps={"1": [], "2": ["1"]}, done=[], exclude=[])
    except BridgeError as e:
        assert "decision marker missing" in str(e)
    else:
        assert ready == ["1"]


def test_write_spec_and_ck_plan_on_a_hand_written_graph():
    deps = {"1": [], "2": [], "3": ["2"], "4": ["3"], "10": []}
    with git_repo() as repo:
        write_spec(repo, deps, done=[])
        plan = ck_plan(repo)
        assert plan["ready"] == ["2", "1", "10"]  # chain 2->3->4 first, then numeric id
        assert plan["remaining"] == list(deps)
        assert plan["deps"]["4"] == ["3"]
        assert ck_plan(repo, exclude=["2"])["ready"] == ["1", "10"]
        write_spec(repo, deps, done=["2"])
        again = ck_plan(repo)
        assert again["done"] == ["2"] and again["ready"] == ["3", "1", "10"]


@SETTINGS
@given(dags())
def test_dags_are_acyclic_and_valid(deps):
    ids = list(deps)
    assert ids == sorted(ids, key=int) and 1 <= len(ids) <= 25
    for pos, (t, ds) in enumerate(deps.items()):
        assert all(d in ids[:pos] or d not in ids for d in ds)  # lower tasks or dangling, never forward


@SETTINGS
@given(scenarios())
def test_scenarios_have_the_documented_knobs(sc):
    assert set(sc) == {"project", "args", "slots"}
    assert sc["slots"] is None or 1 <= sc["slots"] <= 6
    assert 1 <= sc["args"]["mergeBatch"] <= 4 and 1 <= sc["args"]["fullGateEvery"] <= 3
    assert set(sc["project"]["durations"]) == set(sc["project"]["deps"])
