"""Command dependencies, write-set overlaps and missing-command failures (unattended-correctness Task 17).

TC-110 (12.1), TC-111 (12.2), TC-112 (12.3), TC-113b (12.4), TC-115 (12.5), TC-122b (13.4), TC-124b (13.6),
TC-125 (13.1). Everything runs the real `ck` CLI (`python -m claude_kiro.cli`) on real files; nothing is mocked.
TC-119 (12.9) is the existing tests/test_lint.py, tests/test_relink.py and tests/test_lint_verify_*.py, run unchanged.

The JSON entry shapes of the new lint keys are not fixed field by field in the design, so assertions look at what an
entry names (any leaf value), not at its key names.
"""

import json
import sys
from pathlib import Path
from typing import Any, Iterable, List

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from unattended_helpers import CLI_TABLE_HEADER, attr_project, ck_json, ck_text, spec_graph_project  # noqa: E402

SPEC = "sim"
THESIS_CMD = "mtr thesis new"


# --------------------------------------------------------------------------- helpers


def blk(num, title, *, provides: str = "", **kw) -> str:
    """`test_lint.block` plus an optional `**Provides:**` field (the helper has no such parameter)."""
    from test_lint import block

    text = block(num, title, **kw)
    if provides:
        text = text.replace("**Dependencies:**", f"**Provides:** `{provides}`\n**Dependencies:**", 1)
    return text


def leaves(obj: Any) -> List[str]:
    """Every scalar in a JSON value, as text."""
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in leaves(v)] + [str(k) for k in obj]
    if isinstance(obj, (list, tuple)):
        return [x for v in obj for x in leaves(v)]
    return [str(obj)]


def mentions(entry: Any, needle: str) -> bool:
    """The entry names `needle`: a leaf equals it, or is a task reference to it (`Task 5`, `task:5`), or contains it."""
    ls = leaves(entry)
    if needle.isdigit():
        return any(x in (needle, f"Task {needle}", f"task:{needle}") for x in ls)
    return any(needle in x for x in ls)


def entries(result: dict, key: str) -> list:
    assert key in result, f"lint JSON has no {key!r} key: {sorted(result)}"
    value = result[key]
    return list(value.items()) if isinstance(value, dict) else list(value)


def lint(root: Path) -> dict:
    code, obj, err = ck_json(root, "lint", SPEC, "--json")
    assert obj is not None, f"ck lint --json printed no JSON (exit {code}): {err}"
    return obj


def plan(root: Path) -> dict:
    code, obj, err = ck_json(root, "plan", SPEC, "--json")
    assert obj is not None, f"ck plan --json printed no JSON (exit {code}): {err}"
    return obj


def tasks_md(root: Path) -> Path:
    return root / "specs" / SPEC / "tasks.md"


def deps_of(root: Path, num: str) -> List[str]:
    from claude_kiro.hooks._shared.spec_parser import parse_tasks

    by_num = {t.num: t for t in parse_tasks(tasks_md(root))}
    return list(by_num[num].dependencies)


TEST_THESIS = (
    "import subprocess\n\n\n"
    "def test_thesis_new():\n"
    '    assert subprocess.run(["mtr", "thesis", "new"]).returncode == 0\n'
)


def command_project(tmp_path: Path, *, test_src: str = TEST_THESIS, test_deps: str = "None") -> Path:
    """Test task 1 (verifies Task 2) invokes `mtr thesis new`, which Task 5 owns the file for. Task 2 depends only
    on Task 1."""
    return spec_graph_project(
        tmp_path,
        tasks=(
            blk(1, "Thesis tests", track="test", files=["tests/test_thesis.py"], verifies="Task 2", deps=test_deps)
            + blk(2, "Report", files=["src/mtr/report.py"], vby="Task 1", deps="Task 1")
            + blk(5, "Thesis command", files=["src/mtr/thesis.py"], provides=THESIS_CMD)
        ),
        design_cli={THESIS_CMD: "src/mtr/thesis.py", "mtr version": "src/mtr/cli.py"},
        tests={"tests/test_thesis.py": test_src},
    )


# --------------------------------------------------------------------------- 12.1 to 12.3


def test_tc110_lint_reports_the_missing_command_edge(tmp_path):
    root = command_project(tmp_path)
    result = lint(root)
    found = entries(result, "commandEdges")
    assert len(found) == 1, found
    [edge] = found
    for needle in ("tests/test_thesis.py", THESIS_CMD, "5", "2"):
        assert mentions(edge, needle), f"commandEdges entry does not name {needle!r}: {edge}"
    assert result["cycles"] == []


def test_tc110_lint_exit_code_stays_zero_for_the_warning(tmp_path):
    root = command_project(tmp_path)
    code, _, _ = ck_text(root, "lint", SPEC)
    assert code == 0


def test_tc110_no_finding_when_the_dependency_exists(tmp_path):
    root = spec_graph_project(
        tmp_path,
        tasks=(
            blk(1, "Thesis tests", track="test", files=["tests/test_thesis.py"], verifies="Task 2")
            + blk(5, "Thesis command", files=["src/mtr/thesis.py"], provides=THESIS_CMD)
            + blk(2, "Report", files=["src/mtr/report.py"], vby="Task 1", deps="Task 1, Task 5")
        ),
        design_cli={THESIS_CMD: "src/mtr/thesis.py", "mtr version": "src/mtr/cli.py"},
        tests={"tests/test_thesis.py": TEST_THESIS},
    )
    assert entries(lint(root), "commandEdges") == []


def test_tc111_relink_commands_adds_the_dependency_and_is_idempotent(tmp_path):
    root = command_project(tmp_path)
    assert "5" not in deps_of(root, "2")
    code, out, err = ck_json(root, "relink", SPEC, "--commands", "--apply", "--json")
    assert code == 0, err
    assert out is not None, "ck relink --commands --apply --json printed no JSON"
    assert "5" in deps_of(root, "2")
    assert "provides" in tasks_md(root).read_text().split("### Task 2")[1].split("### Task 5")[0].lower()

    assert entries(lint(root), "commandEdges") == [], "a second lint is clean"

    before = tasks_md(root).read_bytes()
    code, _, err = ck_json(root, "relink", SPEC, "--commands", "--apply", "--json")
    assert code == 0, err
    assert tasks_md(root).read_bytes() == before, "a second relink changes nothing"


def test_tc111_relink_commands_without_apply_changes_nothing(tmp_path):
    root = command_project(tmp_path)
    before = tasks_md(root).read_bytes()
    code, out, err = ck_json(root, "relink", SPEC, "--commands", "--json")
    assert code == 0, err
    assert out is not None
    assert tasks_md(root).read_bytes() == before


def test_tc112_unknown_command_warns_and_existing_command_does_not(tmp_path):
    src = (
        "import subprocess\n\n\n"
        "def test_bogus():\n"
        '    assert subprocess.run(["mtr", "bogus"]).returncode == 0\n\n\n'
        "def test_version():\n"
        '    assert subprocess.run(["mtr", "version"]).returncode == 0\n'
    )
    root = spec_graph_project(
        tmp_path,
        tasks=blk(1, "Tests", track="test", files=["tests/test_mixed.py"], verifies="Task 2")
        + blk(2, "Impl", files=["src/mtr/impl.py"], vby="Task 1", deps="Task 1"),
        design_cli={"mtr version": "src/mtr/cli.py"},
        tests={"tests/test_mixed.py": src},
    )
    result = lint(root)
    unknown = entries(result, "unknownCommands")
    assert any(mentions(e, "mtr bogus") for e in unknown), unknown
    assert not any(mentions(e, "mtr version") for e in unknown), unknown


# --------------------------------------------------------------------------- 12.4


def test_tc113_graph_has_the_provides_edge_for_a_provides_field(tmp_path):
    root = spec_graph_project(
        tmp_path,
        tasks=blk(5, "Thesis command", files=["src/mtr/thesis.py"], provides=THESIS_CMD)
        + blk(6, "Other", files=["src/mtr/other.py"]),
        design_cli={"mtr version": "src/mtr/cli.py"},
    )
    code, graph, err = ck_json(root, "graph", SPEC, "--json")
    assert graph is not None, f"ck graph --json printed no JSON (exit {code}): {err}"
    provides = [e for e in graph["edges"] if e["kind"] == "provides"]
    assert [(e["src"], e["dst"], e["source"]) for e in provides if e["dst"] == f"cmd:{THESIS_CMD}"] == [
        ("task:5", f"cmd:{THESIS_CMD}", "Provides")
    ]
    assert any(n["id"] == f"cmd:{THESIS_CMD}" and n["kind"] == "command" for n in graph["nodes"])


# --------------------------------------------------------------------------- 12.5

# Three fixture CLIs that lack `thesis new`. Each test fails the way its framework reports it, and its message
# carries the invocation the test made, so ck can name the command path.
_CLICK_CLI = (
    "import click\n\n\n"
    "@click.group()\n"
    "def cli():\n    pass\n\n\n"
    '@cli.command("version")\n'
    "def version():\n    click.echo('1')\n\n\n"
    'if __name__ == "__main__":\n    cli()\n'
)
_ARGPARSE_CHOICE_CLI = (
    "import argparse\n\n\n"
    'p = argparse.ArgumentParser(prog="mtr")\n'
    'sub = p.add_subparsers(dest="cmd")\n'
    'sub.add_parser("version")\n'
    "p.parse_args()\n"
)
_ARGPARSE_UNRECOGNIZED_CLI = (
    "import argparse\n\n\n"
    'p = argparse.ArgumentParser(prog="mtr")\n'
    'sub = p.add_subparsers(dest="cmd")\n'
    'sub.add_parser("thesis")\n'
    "p.parse_args()\n"
)

_MISSING_TESTS = f'''\
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(module):
    proc = subprocess.run([sys.executable, "-m", module, "thesis", "new"], cwd=ROOT, capture_output=True, text=True)
    return proc


def test_click_no_such_command():
    proc = run("mtr_click_cli")
    assert proc.returncode == 0, "mtr thesis new: " + proc.stderr.strip().splitlines()[-1]


def test_argparse_invalid_choice():
    proc = run("mtr_choice_cli")
    assert proc.returncode == 0, "mtr thesis new: " + proc.stderr.strip().splitlines()[-1]


def test_argparse_unrecognized_arguments():
    proc = run("mtr_unrecognized_cli")
    assert proc.returncode == 0, "mtr thesis new: " + proc.stderr.strip().splitlines()[-1]
'''


# A whole-spec gate runs only the Verify commands of Done tasks ("tests that should already pass"), so the
# missing-command tests need a Done task whose Verify runs them; without one the gate runs nothing.
_MISSING_TASK = """### Task 9: Missing-command smoke
**Status:** Done
**Track:** impl
**Files:**
- `mtr_click_cli.py` - CLI stubs whose `mtr thesis new` is missing
**Verify:** `python -m pytest -q tests/test_missing_command.py`
**Dependencies:** None

**Acceptance:**
- [x] runs the missing-command tests

---

"""


def missing_command_project(tmp_path: Path) -> Path:
    root = attr_project(tmp_path, extra_tasks=_MISSING_TASK)
    (root / "mtr_click_cli.py").write_text(_CLICK_CLI)
    (root / "mtr_choice_cli.py").write_text(_ARGPARSE_CHOICE_CLI)
    (root / "mtr_unrecognized_cli.py").write_text(_ARGPARSE_UNRECOGNIZED_CLI)
    (root / "tests" / "test_missing_command.py").write_text(_MISSING_TESTS)
    design = root / "specs" / "demo" / "design.md"
    design.write_text(design.read_text() + "\n## CLI\n\n" + CLI_TABLE_HEADER + f"| `{THESIS_CMD}` | `calc.py` |\n")
    return root


@pytest.mark.parametrize(
    "test_name,needle",
    [
        ("test_click_no_such_command", "No such command"),
        ("test_argparse_invalid_choice", "invalid choice"),
        ("test_argparse_unrecognized_arguments", "unrecognized arguments"),
    ],
)
def test_tc115_failure_entries_carry_the_missing_command(tmp_path, test_name, needle):
    root = missing_command_project(tmp_path)
    code, out, err = ck_json(root, "gate", "demo", "--json")
    assert out is not None, f"ck gate --json printed no JSON (exit {code}): {err}"
    [failure] = [f for f in out["failures"] if f["node"].endswith(f"::{test_name}")]
    assert needle in failure["message"], failure["message"]
    assert failure["missingCommand"] is not None, failure
    assert failure["missingCommand"]["command"] == THESIS_CMD


# --------------------------------------------------------------------------- 13.1, 13.4, 13.6


def test_tc122_lint_warns_about_a_task_with_no_write_set(tmp_path):
    root = spec_graph_project(
        tmp_path,
        tasks=blk(1, "Has files", files=["src/mtr/a.py"]) + blk(7, "No files"),
        design_cli={"mtr version": "src/mtr/cli.py"},
    )
    no_write_set = entries(lint(root), "noWriteSet")
    assert any(mentions(e, "7") for e in no_write_set), no_write_set
    assert not any(mentions(e, "1") for e in no_write_set), no_write_set


def test_tc124_plan_leaves_tasks_md_byte_identical(tmp_path):
    root = spec_graph_project(
        tmp_path,
        tasks=blk(1, "A", files=["src/*.py"]) + blk(2, "B", files=["src/a.py"]) + blk(3, "C", files=["src/mtr/c.py"]),
        design_cli={"mtr version": "src/mtr/cli.py"},
    )
    before = tasks_md(root).read_bytes()
    first = plan(root)
    assert tasks_md(root).read_bytes() == before
    assert first["overlaps"], "the glob pair overlaps, so the plan reports it"
    second = plan(root)
    assert tasks_md(root).read_bytes() == before
    assert second["deps"] == first["deps"]
    assert deps_of(root, "2") == []


def overlap_pairs(overlaps: dict) -> set:
    return {tuple(sorted((a, b))) for a, bs in overlaps.items() for b in bs}


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("src/*.py", "src/a.py", True),
        ("src/**", "src/x/y.py", True),
        ("src/a.py", "src/ab.py", False),
    ],
)
def test_tc125_plan_overlaps_match_globs(tmp_path, a, b, expected):
    root = spec_graph_project(
        tmp_path,
        tasks=blk(1, "First", files=[a]) + blk(2, "Second", files=[b]),
        design_cli={"mtr version": "src/mtr/cli.py"},
    )
    result = plan(root)
    assert "overlaps" in result, sorted(result)
    pairs = overlap_pairs(result["overlaps"])
    assert (("1", "2") in pairs) is expected, result["overlaps"]
