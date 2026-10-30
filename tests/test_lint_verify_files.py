"""`ck lint` / `ck plan`: the Verify-file cycle rule (run-followups Story 13, TC-77..TC-84, TC-99, TC-100).

X is an impl task whose Verify runs test file F. F belongs to test task T, which verifies impl task Y, and Y
depends on X: X's gate can't pass before Y lands. Everything here drives the real CLI (CliRunner) over real
tasks.md files, or `lint_spec` over this repository's real specs.
"""

import json
import shutil
from pathlib import Path

import pytest

from claude_kiro.lint import lint_spec
from landing_bridge import bridge  # noqa: F401
from test_lint import block, ck, market, write_spec  # noqa: F401  (market is a fixture reused for TC-83)

pytestmark = pytest.mark.usefixtures("no_network")

REPO = Path(__file__).resolve().parent.parent
PERF = "tests/test_perf.py"


def spec_blocks(*, x_verify=f"uv run pytest -q {PERF}", x_status="Not Started", x_vby="", t_verifies="Task 13", y_vby="", chain=False, extra=""):
    """TC-77's shape: X=8, T=9 owns PERF and verifies 13, 13 depends on 8 (through 11 when `chain`)."""
    out = block(8, "Perf core", files=["src/perf.py"], verify=x_verify, status=x_status, vby=x_vby)
    if chain:
        out += block(11, "Middle", files=["src/middle.py"], deps="Task 8 (uses perf)")
    out += block(9, "Perf tests", track="test", files=[PERF], verifies=t_verifies)
    out += block(13, "Report", files=["src/report.py"], deps="Task 11 (uses middle)" if chain else "Task 8 (uses perf)", vby=y_vby)
    return out + extra


def entries(root: Path):
    return [e for e in lint_spec(root / "specs/sim", root)["verifyCycles"] if e.get("kind") == "verify-file"]


def lint_json(root: Path):
    return json.loads(ck(root, "lint", "sim", "--json").output)


def keys(found):
    return sorted((e["task"], e["file"], e["testTask"], e["needs"]) for e in found)


# --------------------------------------------------------------------------- TC-77

def test_tc77_cli_names_task_file_owner_needs_chain_and_suggestion(tmp_path):
    write_spec(tmp_path, spec_blocks())
    out = ck(tmp_path, "lint", "sim")
    assert out.exit_code == 1, out.output
    assert "verify-file cycle" in out.output
    for needle in ("Task 8", PERF, "Task 9", "Task 13", "13 -> 8"):
        assert needle in out.output, needle
    assert "narrow task 8's verify" in out.output.lower()


def test_tc77_via_the_needed_tasks_verified_by(tmp_path):
    write_spec(tmp_path, spec_blocks(t_verifies="", y_vby="Task 9"))
    out = ck(tmp_path, "lint", "sim")
    assert out.exit_code == 1, out.output
    assert [(e["task"], e["testTask"], e["needs"]) for e in entries(tmp_path)] == [("8", "9", "13")]
    assert "13 -> 8" in out.output


def test_tc77_three_task_chain(tmp_path):
    write_spec(tmp_path, spec_blocks(chain=True))
    [entry] = entries(tmp_path)
    assert entry["chain"] == ["13", "11", "8"]
    assert "13 -> 11 -> 8" in ck(tmp_path, "lint", "sim").output


def test_tc77_test_file_need_not_exist_and_may_exist(tmp_path):
    write_spec(tmp_path, spec_blocks())
    assert not (tmp_path / PERF).exists()
    assert len(entries(tmp_path)) == 1
    (tmp_path / "tests").mkdir()
    (tmp_path / PERF).write_text("def test_x():\n    pass\n")
    assert len(entries(tmp_path)) == 1


# --------------------------------------------------------------------------- TC-78 (CLI and bridge)

def test_tc78_plan_json_blocks_x_and_lists_the_entry(tmp_path):
    write_spec(tmp_path, spec_blocks())
    plan = json.loads(ck(tmp_path, "plan", "sim", "--json").output)
    [entry] = [e for e in plan["verifyCycles"] if e.get("kind") == "verify-file"]
    assert entry["task"] == "8"
    assert "8" not in plan["ready"]


def test_tc78_bridge_run_refuses_to_launch_from_the_start(bridge, tmp_path):
    write_spec(tmp_path, spec_blocks())
    plan = json.loads(ck(tmp_path, "plan", "sim", "--json").output)
    cycles = [e for e in plan["verifyCycles"] if e.get("kind") == "verify-file"]
    assert cycles, "ck plan reports no verify-file entry"
    out = bridge.run({"deps": {"8": [], "13": ["8"]}, "verifyCycles": cycles, "verifyCyclesFromStart": True})
    assert any("Refusing to launch" in line for line in out["logs"]), out["logs"]
    assert out["result"]["refused"] is True
    assert [v["task"] for v in out["result"]["verifyCycles"]] == ["8"]
    assert not [e for e in out["events"] if e.startswith("task")], out["events"]


# --------------------------------------------------------------------------- TC-79

NEGATIVES = {
    "file in no test task's files": dict(),
    "needed task does not depend on X": dict(),
    "test task verifies only X": dict(),
    "file only in an impl task's files": dict(),
}


def negative_spec(name: str) -> str:
    verify = f"uv run pytest -q {PERF}"
    if name == "file in no test task's files":
        return (
            block(8, "Perf core", files=["src/perf.py"], verify=verify)
            + block(9, "Perf tests", track="test", files=["tests/test_other.py"], verifies="Task 13")
            + block(13, "Report", files=["src/report.py"], deps="Task 8 (uses perf)")
        )
    if name == "needed task does not depend on X":
        return (
            block(8, "Perf core", files=["src/perf.py"], verify=verify)
            + block(9, "Perf tests", track="test", files=[PERF], verifies="Task 13")
            + block(13, "Report", files=["src/report.py"])
        )
    if name == "test task verifies only X":
        return (
            block(8, "Perf core", files=["src/perf.py"], verify=verify, vby="Task 9")
            + block(9, "Perf tests", track="test", files=[PERF], verifies="Task 8")
            + block(13, "Report", files=["src/report.py"], deps="Task 8 (uses perf)")
        )
    return (
        block(8, "Perf core", files=["src/perf.py"], verify=verify)
        + block(9, "Perf tests", track="test", files=["tests/test_other.py"], verifies="Task 13")
        + block(12, "Fixture owner", files=[PERF])
        + block(13, "Report", files=["src/report.py"], deps="Task 8 (uses perf)")
    )


@pytest.mark.parametrize("name", list(NEGATIVES))
def test_tc79_negatives(tmp_path, name):
    write_spec(tmp_path, negative_spec(name))
    assert entries(tmp_path) == []
    out = ck(tmp_path, "lint", "sim")
    assert out.exit_code == 0, out.output


def test_tc79_positive_control(tmp_path):
    write_spec(tmp_path, spec_blocks())
    assert ck(tmp_path, "lint", "sim").exit_code == 1


# --------------------------------------------------------------------------- TC-80

def test_tc80_done_x_is_exempt(tmp_path):
    write_spec(tmp_path, spec_blocks())
    assert len(entries(tmp_path)) == 1
    write_spec(tmp_path, spec_blocks(x_status="Done"))
    assert entries(tmp_path) == []
    assert ck(tmp_path, "lint", "sim").exit_code == 0


# --------------------------------------------------------------------------- TC-81

MATCHED = {
    "leading ./": f"uv run pytest -q ./{PERF}",
    "among other files and flags": f"pytest -q -n auto tests/a.py {PERF} -x",
    "second backticked command": f"echo hi` `uv run pytest -q {PERF}",
    "bare python -m pytest": f"python -m pytest {PERF}",
    "bare narrowed in one, bare in another": f"pytest {PERF}::test_a` `pytest -q {PERF}",
    "-k in one, bare in another": f"pytest {PERF} -k slow` `pytest -q {PERF}",
}

UNMATCHED = {
    "::test": f"pytest -q {PERF}::test_a",
    "::Class::test": f"python -m pytest {PERF}::C::test_b",
    "-k slow": f"pytest -q {PERF} -k slow",
    "-kslow": f"pytest -kslow {PERF}",
    "-k=slow": f"pytest {PERF} -k=slow",
    "glob": "pytest -q tests/test_*.py",
    "directory": "pytest -q tests/",
    "prefix of a longer name": f"pytest -q {PERF}x",
    "suffix of another path": f"pytest -q other/{PERF}",
    "bak": f"pytest -q {PERF}.bak",
}


@pytest.mark.parametrize("form", list(MATCHED))
def test_tc81_matched_forms(tmp_path, form):
    write_spec(tmp_path, spec_blocks(x_verify=MATCHED[form]))
    assert [(e["task"], e["file"]) for e in entries(tmp_path)] == [("8", PERF)]
    assert ck(tmp_path, "lint", "sim").exit_code == 1


@pytest.mark.parametrize("form", list(UNMATCHED))
def test_tc81_unmatched_forms(tmp_path, form):
    write_spec(tmp_path, spec_blocks(x_verify=UNMATCHED[form]))
    assert entries(tmp_path) == []
    assert ck(tmp_path, "lint", "sim").exit_code == 0


# --------------------------------------------------------------------------- TC-82

def test_tc82_cycles_and_unknown_tasks_never_hide_entries(tmp_path):
    extra = block(11, "loop a", deps="Task 12 (x)") + block(12, "loop b", deps="Task 11 (x)") + block(14, "ghost", deps="Task 99 (x)")
    write_spec(tmp_path, spec_blocks(extra=extra))
    out = ck(tmp_path, "lint", "sim")
    assert out.exception is None or isinstance(out.exception, SystemExit), repr(out.exception)
    assert "Traceback" not in out.output
    assert out.exit_code == 1
    data = lint_json(tmp_path)
    assert {frozenset(c) for c in data["cycles"]} == {frozenset({"11", "12"})}
    assert [(e["task"], e["needs"]) for e in data["verifyCycles"] if e.get("kind") == "verify-file"] == [("8", "13")]
    assert "99" in out.output, "the unknown-task error is still reported"
    assert "verify-file cycle" in out.output


# --------------------------------------------------------------------------- TC-83

def test_tc83_json_keys_of_the_new_entry(tmp_path):
    write_spec(tmp_path, spec_blocks(chain=True))
    [entry] = [e for e in lint_json(tmp_path)["verifyCycles"] if e.get("kind") == "verify-file"]
    assert set(entry) == {"kind", "task", "file", "testTask", "needs", "chain", "message"}
    assert (entry["task"], entry["file"], entry["testTask"], entry["needs"]) == ("8", PERF, "9", "13")
    assert entry["chain"] == ["13", "11", "8"]
    assert isinstance(entry["message"], str) and entry["message"]


def test_tc83_import_entry_keeps_its_exact_shape(market):
    write_spec(
        market,
        block(39, "Strategy tests", track="test", status="Done", files=["tests/test_strategy.py"], verify="uv run pytest tests/test_strategy.py", verifies="Task 40")
        + block(40, "Strategy core", files=["src/sim/strategy.py"], vby="Task 39", deps="Task 39 (its tests are the oracle)")
        + block(69, "Runner", files=["src/sim/engine/runner.py"], deps="Task 40 (runner calls the strategy)"),
    )
    [entry] = lint_json(market)["verifyCycles"]
    assert set(entry) == {"task", "verifiedBy", "needs", "module", "message"}
    assert (entry["task"], entry["verifiedBy"], entry["needs"], entry["module"]) == ("40", "39", "69", "src/sim/engine/runner.py")


# --------------------------------------------------------------------------- TC-84

def reset_statuses(text: str) -> str:
    import re

    return re.sub(r"(?m)^(\*\*Status:\*\*).*$", r"\1 Not Started", text)


def project_with(tmp_path: Path, name: str, text: str) -> Path:
    spec = tmp_path / "specs" / name
    spec.mkdir(parents=True)
    (spec / "tasks.md").write_text(reset_statuses(text))
    return tmp_path


REAL_TASKS = sorted((REPO / "specs").glob("*/tasks.md"))


def test_tc84_real_specs_exist():
    assert REAL_TASKS, "no specs/*/tasks.md found"
    assert (REPO / "specs/landing-speed/tasks.md").is_file(), "landing-speed is asserted by name"


@pytest.mark.parametrize("tasks_md", REAL_TASKS, ids=lambda p: p.parent.name)
def test_tc84_no_real_spec_reports_a_verify_file_entry(tmp_path, tasks_md):
    root = project_with(tmp_path, tasks_md.parent.name, tasks_md.read_text())
    found = [e for e in lint_spec(root / "specs" / tasks_md.parent.name, root)["verifyCycles"] if "file" in e]
    assert found == [], f"{tasks_md.parent.name}: " + "; ".join(e["message"] for e in found)


def test_tc84_landing_speed_tasks_9_and_17_are_not_flagged(tmp_path):
    src = REPO / "specs/landing-speed/tasks.md"
    root = project_with(tmp_path, "landing-speed", src.read_text())
    found = [e for e in lint_spec(root / "specs/landing-speed", root)["verifyCycles"] if "file" in e]
    assert not [e for e in found if e["task"] in ("9", "17")], "landing-speed tasks 9 and 17 must stay exempt (13.9, 13.10)"


FIXTURE = REPO / "tests/fixtures/lint/agent-mode-analytics-pre-fix/tasks.md"


def test_tc84_pre_fix_agent_mode_analytics_is_the_only_failing_copy(tmp_path):
    root = project_with(tmp_path, "agent-mode-analytics", FIXTURE.read_text())
    found = [e for e in lint_spec(root / "specs/agent-mode-analytics", root)["verifyCycles"] if "file" in e]
    assert found, "the pre-fix copy must fail"
    for e in found:
        assert (e["task"], e["needs"], e["file"]) == ("8", "13", "tests/test_stats_perf.py"), e
        assert e["testTask"] in ("9", "10"), e
    assert {(e["task"], e["needs"]) for e in found} == {("8", "13")}


# --------------------------------------------------------------------------- TC-99

def test_tc99a_t_also_verifies_x(tmp_path):
    write_spec(tmp_path, spec_blocks(t_verifies="Task 8, Task 13"))
    assert entries(tmp_path) == []
    assert ck(tmp_path, "lint", "sim").exit_code == 0
    write_spec(tmp_path, spec_blocks(x_vby="Task 9", t_verifies="", y_vby="Task 9"))
    assert entries(tmp_path) == []
    assert ck(tmp_path, "lint", "sim").exit_code == 0


def test_tc99a_second_owner_verifying_only_y_still_fails(tmp_path):
    second = block(10, "More perf tests", track="test", files=[PERF], verifies="Task 13")
    write_spec(tmp_path, spec_blocks(t_verifies="Task 8, Task 13", extra=second))
    assert [(e["task"], e["testTask"], e["needs"]) for e in entries(tmp_path)] == [("8", "10", "13")]
    assert ck(tmp_path, "lint", "sim").exit_code == 1


@pytest.mark.parametrize("narrow", [f"uv run pytest {PERF}::test_fast", f"uv run pytest {PERF} -k fast"])
def test_tc99b_narrowed_verify_is_exempt_until_a_bare_command_returns(tmp_path, narrow):
    write_spec(tmp_path, spec_blocks(x_verify=narrow))
    assert entries(tmp_path) == []
    assert ck(tmp_path, "lint", "sim").exit_code == 0
    write_spec(tmp_path, spec_blocks(x_verify=f"{narrow}` `uv run pytest -q {PERF}"))
    assert len(entries(tmp_path)) == 1
    assert ck(tmp_path, "lint", "sim").exit_code == 1


def test_tc99c_agent_mode_analytics_shape(tmp_path):
    write_spec(tmp_path, spec_blocks(x_vby="None"))
    assert len(entries(tmp_path)) == 1
    assert ck(tmp_path, "lint", "sim").exit_code == 1


# --------------------------------------------------------------------------- TC-100

def prompt_free_blocks():
    return (
        block(11, "Guard", files=["src/demo/guard.py"], verify="uv run pytest -q tests/test_guardx.py", vby="Task 3")
        + block(3, "Guard tests", track="test", files=["tests/test_guardx.py"], verifies="Task 11")
        + block(15, "Share", files=["src/demo/share.py"], deps="Task 11 (uses guard)")
    )


def test_tc100a_file_absent_is_the_documented_launch_gap(tmp_path):
    write_spec(tmp_path, prompt_free_blocks())
    assert entries(tmp_path) == []
    out = ck(tmp_path, "lint", "sim")
    assert out.exit_code == 0, out.output
    assert lint_spec(tmp_path / "specs/sim", tmp_path)["verifyCycles"] == []


def test_tc100b_file_present_gives_exactly_the_import_entry(tmp_path):
    write_spec(tmp_path, prompt_free_blocks())
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/demo").mkdir(parents=True)
    (tmp_path / "tests/guardx_helpers.py").write_text("def make():\n    from src.demo.share import run  # lazy\n    return run\n")
    (tmp_path / "tests/test_guardx.py").write_text("from tests.guardx_helpers import make\n\n\ndef test_x():\n    assert make()\n")
    cycles = lint_spec(tmp_path / "specs/sim", tmp_path)["verifyCycles"]
    assert len(cycles) == 1
    [entry] = cycles
    assert "kind" not in entry
    assert (entry["task"], entry["verifiedBy"], entry["needs"], entry["module"]) == ("11", "3", "15", "src/demo/share.py")
    assert entries(tmp_path) == []
    assert ck(tmp_path, "lint", "sim").exit_code == 1
    assert "11" not in json.loads(ck(tmp_path, "plan", "sim", "--json").output)["ready"]
