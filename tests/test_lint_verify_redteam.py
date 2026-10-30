"""Red-team for the Verify-file cycle rule's SHALL NOT clauses (run-followups 13.4 and 13.5).

13.4: no error when F is in no test task's Files, or no impl task verified by an owning test task depends on X.
13.5: no error for a Done X.
The false-positive guards pass with or without the rule; `test_positive_control_*` and the `*_still_fire` cases
prove the rule fires on the same shapes, so a lint that reports nothing cannot satisfy both.
"""

import re
from pathlib import Path

import pytest

from claude_kiro.lint import lint_spec
from test_lint import block, ck, write_spec

pytestmark = pytest.mark.usefixtures("no_network")

REPO = Path(__file__).resolve().parent.parent
PERF = "tests/test_perf.py"


def shape(*, x_verify=f"uv run pytest -q {PERF}", x_status="Not Started", x_files=("src/perf.py",), x_desc_only=False, t_files=(PERF,),
          t_verifies="Task 13", y_deps="Task 8 (uses perf)", extra="", x_vby=""):
    out = block(8, "Perf core", files=list(x_files), verify=x_verify, status=x_status, vby=x_vby)
    out += block(9, "Perf tests", track="test", files=list(t_files), verifies=t_verifies)
    out += block(13, "Report", files=["src/report.py"], deps=y_deps)
    return out + extra


def entries(root: Path):
    return [e for e in lint_spec(root / "specs/sim", root)["verifyCycles"] if e.get("kind") == "verify-file"]


def control_fires(tmp_path):
    write_spec(tmp_path, shape())
    assert [(e["task"], e["needs"]) for e in entries(tmp_path)] == [("8", "13")], "positive control must fire"
    assert ck(tmp_path, "lint", "sim").exit_code == 1


def test_positive_control_the_base_shape_fires(tmp_path):
    control_fires(tmp_path)


def silent(tmp_path, blocks: str):
    write_spec(tmp_path, blocks)
    assert entries(tmp_path) == []
    out = ck(tmp_path, "lint", "sim")
    assert out.exit_code == 0, out.output


# --------------------------------------------------------------------------- 13.4: F is not an owned test file

@pytest.mark.parametrize("owned", ["tests/Test_Perf.py", "tests/test_perf.PY", "tests/test_perf.py.orig", "tests/test_perf.pyc", "tests/sub/tests/test_perf.py", "xtests/test_perf.py"])
def test_134_near_miss_file_names_in_the_test_tasks_files(tmp_path, owned):
    silent(tmp_path, shape(t_files=(owned,)))


@pytest.mark.parametrize("verify", [f"uv run pytest -q {PERF}.orig", f"uv run pytest -q {PERF}c", f"uv run pytest -q x{PERF}", f"uv run pytest -q ../{PERF}x", "uv run pytest -q tests/test_perf"])
def test_134_near_miss_file_names_in_x_verify(tmp_path, verify):
    silent(tmp_path, shape(x_verify=verify))


def test_134_f_only_in_x_description_not_verify(tmp_path):
    blocks = shape(x_verify="python -c 'import perf'")
    blocks = blocks.replace("**Track:** impl\n**Files:**\n- `src/perf.py` - x", f"**Track:** impl\n**Description:** runs {PERF} by hand\n**Files:**\n- `src/perf.py` - x", 1)
    assert PERF in blocks.split("### Task 9")[0]
    write_spec(tmp_path, blocks)
    assert entries(tmp_path) == []
    assert ck(tmp_path, "lint", "sim").exit_code == 0


def test_134_f_inside_a_k_expression_is_not_a_test_file_arg(tmp_path):
    silent(tmp_path, shape(x_verify=f'uv run pytest -q tests/other.py -k "{PERF}"'))


def test_134_f_only_in_an_impl_tasks_files(tmp_path):
    extra = block(12, "Fixture owner", files=[PERF])
    silent(tmp_path, shape(t_files=("tests/test_other.py",), extra=extra))


def test_134_owning_test_task_verifies_nothing(tmp_path):
    silent(tmp_path, shape(t_verifies=""))


# --------------------------------------------------------------------------- 13.4: no impl task verified by T depends on X

def test_134_the_dependency_runs_the_other_way(tmp_path):
    # X depends on Y, not Y on X: X's gate can pass once Y lands.
    silent(tmp_path, block(8, "Perf core", files=["src/perf.py"], verify=f"uv run pytest -q {PERF}", deps="Task 13 (needs report)")
           + block(9, "Perf tests", track="test", files=[PERF], verifies="Task 13")
           + block(13, "Report", files=["src/report.py"]))


def test_134_y_depends_on_a_sibling_not_on_x(tmp_path):
    silent(tmp_path, shape(y_deps="Task 12 (uses sibling)", extra=block(12, "Sibling", files=["src/sib.py"])))


def test_134_y_depends_only_on_the_test_task(tmp_path):
    silent(tmp_path, shape(y_deps="Task 9 (oracle)"))


def test_134_y_is_x_itself(tmp_path):
    silent(tmp_path, shape(t_verifies="Task 8"))


def test_134_unknown_dependency_is_not_x(tmp_path):
    silent(tmp_path, shape(y_deps="Task 99 (ghost)"))


def test_134_depending_on_x_through_a_chain_is_still_a_dependency(tmp_path):
    # Positive control for transitivity, including a Done task in the middle: reachability, not status, decides.
    write_spec(tmp_path, shape(y_deps="Task 11 (uses middle)", extra=block(11, "Middle", files=["src/mid.py"], deps="Task 8 (uses perf)", status="Done")))
    [entry] = entries(tmp_path)
    assert entry["chain"] == ["13", "11", "8"]


# --------------------------------------------------------------------------- 13.5: X is Done

@pytest.mark.parametrize("status", ["Done", "done", "DONE", "Done (merged)", "Done ✓", "✅ Done", "**Done**", "Complete", "Completed"])
def test_135_every_done_spelling_exempts_x(tmp_path, status):
    silent(tmp_path, shape(x_status=status))


@pytest.mark.parametrize("status", ["Not Started", "In Progress", "In Progress (almost done)", "Blocked"])
def test_135_not_done_statuses_still_fire(tmp_path, status):
    write_spec(tmp_path, shape(x_status=status))
    assert [(e["task"], e["needs"]) for e in entries(tmp_path)] == [("8", "13")]


def test_135_done_exempts_only_x_not_a_second_open_x(tmp_path):
    second = block(7, "Other perf", files=["src/p2.py"], verify=f"uv run pytest {PERF}")
    write_spec(tmp_path, shape(x_status="Done", extra=second, y_deps="Task 8 (a), Task 7 (b)"))
    assert [(e["task"], e["needs"]) for e in entries(tmp_path)] == [("7", "13")]


def test_135_done_y_or_t_does_not_exempt_x(tmp_path):
    write_spec(tmp_path, shape().replace("**Status:** Not Started\n**Track:** test", "**Status:** Done\n**Track:** test"))
    assert [(e["task"], e["needs"]) for e in entries(tmp_path)] == [("8", "13")]


def test_135_real_repository_specs_with_their_real_statuses_report_nothing(tmp_path):
    for tasks_md in sorted((REPO / "specs").glob("*/tasks.md")):
        spec = tmp_path / "specs" / tasks_md.parent.name
        spec.mkdir(parents=True, exist_ok=True)
        (spec / "tasks.md").write_text(tasks_md.read_text())
        found = [e for e in lint_spec(spec, tmp_path)["verifyCycles"] if "file" in e]
        assert found == [], f"{tasks_md.parent.name}: {found}"


def test_135_pre_fix_agent_mode_analytics_is_silent_once_task_8_is_done(tmp_path):
    text = (REPO / "tests/fixtures/lint/agent-mode-analytics-pre-fix/tasks.md").read_text()
    reset = re.sub(r"(?m)^(\*\*Status:\*\*).*$", r"\1 Not Started", text)
    spec = tmp_path / "specs/agent-mode-analytics"
    spec.mkdir(parents=True)
    (spec / "tasks.md").write_text(reset)
    assert [e for e in lint_spec(spec, tmp_path)["verifyCycles"] if "file" in e], "positive control: the pre-fix copy fails when open"
    marked = re.sub(r"(### Task 8:[^\n]*\n\*\*Status:\*\*)[^\n]*", r"\1 Done", reset, count=1)
    assert marked != reset
    (spec / "tasks.md").write_text(marked)
    assert [e for e in lint_spec(spec, tmp_path)["verifyCycles"] if "file" in e] == []
