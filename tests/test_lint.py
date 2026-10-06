"""`ck lint` / `ck plan`: cycles, verify-order cycles (market-sim Task 40), reasonless deps, ready set."""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_kiro.cli.main import cli
from claude_kiro.hooks._shared.spec_parser import _ref_items, parse_tasks
from claude_kiro.lint import lint_spec


def ck(cwd: Path, *args: str):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(cli, list(args))
    finally:
        os.chdir(old)


def block(num, title, *, track="impl", status="Not Started", files=(), deps="None", verify="", vby="", verifies=""):
    lines = [f"### Task {num}: {title}", f"**Status:** {status}", f"**Track:** {track}", "**Files:**"]
    lines += [f"- `{f}` - x" for f in files]
    if verify:
        lines.append(f"**Verify:** `{verify}`")
    if vby:
        lines.append(f"**Verified by:** {vby}")
    if verifies:
        lines.append(f"**Verifies:** {verifies}")
    lines.append(f"**Dependencies:** {deps}")
    return "\n".join(lines) + "\n\n---\n\n"


def write_spec(root: Path, blocks: str, groups: str = "") -> Path:
    spec = root / "specs" / "sim"
    spec.mkdir(parents=True, exist_ok=True)
    (spec / "tasks.md").write_text("# Implementation Tasks: Sim\n\n## Task Breakdown\n\n" + blocks + (f"## Parallel Groups\n{groups}\n" if groups else ""))
    return spec


@pytest.fixture
def market(tmp_path: Path) -> Path:
    """The market-sim Task 40 shape: test -> harness helper -> runner (owned by a later task)."""
    (tmp_path / "tests/harness").mkdir(parents=True)
    (tmp_path / "tests/harness/__init__.py").write_text("")
    (tmp_path / "tests/harness/engine.py").write_text(
        "def new_sim(**kw):\n    from sim.engine.runner import make_brain  # lazy: module not written yet\n    return make_brain(kw)\n"
    )
    (tmp_path / "tests/test_strategy.py").write_text(
        "from tests.harness.engine import new_sim\n\n\ndef test_strategy():\n    assert new_sim(brain='rules')\n"
    )
    return tmp_path


def test_verify_order_cycle_found_through_harness_import(market):
    write_spec(
        market,
        block(39, "Strategy tests", track="test", status="Done", files=["tests/test_strategy.py"], verify="uv run pytest tests/test_strategy.py", verifies="Task 40")
        + block(40, "Strategy core", files=["src/sim/strategy.py"], vby="Task 39", deps="Task 39 (its tests are the oracle)")
        + block(69, "Runner", files=["src/sim/engine/runner.py"], deps="Task 40 (runner calls the strategy)"),
    )
    result = lint_spec(market / "specs/sim", market)
    assert result["cycles"] == []
    [cycle] = result["verifyCycles"]
    assert (cycle["task"], cycle["verifiedBy"], cycle["needs"]) == ("40", "39", "69")
    assert cycle["module"] == "src/sim/engine/runner.py"

    out = ck(market, "lint", "sim")
    assert out.exit_code == 1
    assert "verify-order cycle: Task 40 is verified by Task 39" in out.output
    plan = json.loads(ck(market, "plan", "sim").output)
    assert "40" not in plan["ready"]  # the scheduler won't launch a task that can't pass


def test_relinked_plan_has_no_verify_cycle(market):
    # The fix used in market-sim: Task 40 becomes a foundation task; Task 39 verifies Task 69.
    write_spec(
        market,
        block(39, "Strategy tests", track="test", status="Done", files=["tests/test_strategy.py"], verify="uv run pytest tests/test_strategy.py", verifies="Task 69")
        + block(40, "Strategy core", files=["src/sim/strategy.py"], verify="python -c 'import sim.strategy'")
        + block(69, "Runner", files=["src/sim/engine/runner.py"], vby="Task 39", deps="Task 40 (runner calls the strategy), Task 39 (oracle)"),
    )
    result = lint_spec(market / "specs/sim", market)
    assert result["verifyCycles"] == []
    assert ck(market, "lint", "sim").exit_code == 0
    assert json.loads(ck(market, "plan", "sim").output)["ready"] == ["40"]


def test_dependency_cycle_refuses_and_nothing_is_ready(tmp_path):
    write_spec(tmp_path, block(1, "a", deps="Task 3 (x)") + block(2, "b", deps="Task 1 (x)") + block(3, "c", deps="Task 2 (x)") + block(4, "d"))
    out = ck(tmp_path, "lint", "sim")
    assert out.exit_code == 1
    assert "dependency cycle" in out.output
    plan = json.loads(ck(tmp_path, "plan", "sim").output)
    assert plan["cycles"] and plan["ready"] == ["4"]


def test_reasonless_dependencies_reported(tmp_path):
    write_spec(
        tmp_path,
        block(76, "report", files=["src/report.py"])
        + block(78, "compare", files=["src/compare.py"], deps="Task 76")
        + block(85, "check", files=["src/check.py"], deps="Task 78 (calls compare())"),
    )
    reasonless = lint_spec(tmp_path / "specs/sim", tmp_path)["reasonless"]
    assert reasonless == [{"task": "78", "dependsOn": "76", "sharedFiles": []}]
    assert "Task 78 -> Task 76; no shared files" in ck(tmp_path, "lint", "sim").output


def test_depending_on_own_verifier_is_not_reasonless(tmp_path):
    write_spec(tmp_path, block(1, "tests", track="test") + block(2, "impl", vby="Task 1", deps="Task 1"))
    assert lint_spec(tmp_path / "specs/sim", tmp_path)["reasonless"] == []


def test_plan_ready_respects_done_deps_and_exclude(tmp_path):
    write_spec(
        tmp_path,
        block(1, "a", status="Done") + block(2, "b", deps="Task 1 (x)") + block(3, "c", deps="Task 1 (x)") + block(4, "d", deps="Task 2 (x)"),
        "Wave 1: Task 1\nWave 2: Tasks 2, 3\nWave 3: Task 4",
    )
    plan = json.loads(ck(tmp_path, "plan", "sim", "--exclude", "3").output)
    assert plan["ready"] == ["2"]
    # The /spec:implement docs and the workflow's plan step pass --json; it must be accepted.
    assert json.loads(ck(tmp_path, "plan", "sim", "--json", "--exclude", "3").output) == plan
    assert plan["wave"] == {"1": 1, "2": 2, "3": 2, "4": 3}
    assert plan["criticalPath"] == ["2", "4"]
    assert plan["remaining"] == ["2", "3", "4"]


def test_notes_in_reference_fields_are_not_references(tmp_path):
    assert [n for n, _ in _ref_items("Task 39 (note: TC-25 also exercises Task 69)")] == ["39"]
    assert _ref_items("Task 76 (uses the report), Task 12 - shared file x.py") == [("76", "uses the report"), ("12", "shared file x.py")]
    write_spec(tmp_path, block(40, "x", vby="Task 39 (see also Task 69)", deps="None (foundation)") + block(39, "t", track="test"))
    task = {t.num: t for t in parse_tasks(tmp_path / "specs/sim/tasks.md")}["40"]
    assert task.verified_by == ["39"] and task.dependencies == []
