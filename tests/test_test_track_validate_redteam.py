"""Red team for Story 6's SHALL NOT clauses.

6.5: a `test_track.validate` value that is not a string or a list of strings is a config error that
names the key, and the gate is never run as if the key were absent.
6.6: an impl-track gate never runs the test-track validation command.

Every case runs the real `ck gate` in a subprocess against a real git project. The validator is a
real script that appends a line to a marker file, so the marker proves whether (and how often) the
command ran. A gate that is green without the key must be red, or at least must not have run the
gate silently, when the key is malformed.
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional

import pytest

from conftest import git, write_tasks
from test_gate import implement_subtract, write_subtract_tests

CK = [sys.executable, "-m", "claude_kiro.cli"]
KEY = "test_track.validate"
ABSENT = object()

VALIDATOR = "import sys\nwith open(sys.argv[1], 'a') as fh:\n    fh.write('ran\\n')\n"


class Setup:
    """A project whose Task 1 (test track) and Task 2 (impl track) are Done and green."""

    def __init__(self, project: Path, tmp_path: Path):
        self.project = project
        self.marker = tmp_path / "validate.marker"
        self.config_home = Path(os.environ["CK_CONFIG_HOME"])
        write_subtract_tests(project)
        implement_subtract(project)
        write_tasks(project, t1="Done", t2="Done", a1="x", a2="x")
        (project / "scripts").mkdir()
        (project / "scripts" / "validate.py").write_text(VALIDATOR)
        self.command = f"{shlex.quote(sys.executable)} scripts/validate.py {shlex.quote(str(self.marker))}"
        self.config: dict = {"verify": ["python -m pytest -q tests/test_calc.py"]}

    def command_list(self, n: int = 1) -> List[str]:
        return [self.command] * n

    def write_config(self) -> None:
        (self.project / "specs" / "ck.json").write_text(json.dumps(self.config))
        git(self.project, "add", "-A")
        git(self.project, "commit", "-qm", "setup", "--allow-empty")

    def set_tasks(self, text: str) -> None:
        (self.project / "specs" / "calc" / "tasks.md").write_text(text)

    def gate(self, *tasks: str) -> subprocess.CompletedProcess:
        self.write_config()
        args = [a for t in tasks for a in ("--task", t)]
        return subprocess.run(
            [*CK, "gate", "calc", *args, "--no-reuse"] if tasks else [*CK, "gate", "calc"],
            cwd=self.project,
            capture_output=True,
            text=True,
            timeout=300,
        )

    @property
    def runs(self) -> int:
        return len(self.marker.read_text().splitlines()) if self.marker.exists() else 0


@pytest.fixture
def setup(project, tmp_path) -> Setup:
    return Setup(project, tmp_path)


def output_of(result: subprocess.CompletedProcess) -> str:
    return result.stdout + result.stderr


def assert_config_error(setup: Setup, result: subprocess.CompletedProcess) -> None:
    out = output_of(result)
    assert "Traceback" not in out, out
    assert result.returncode != 0, f"the gate passed as if the key were absent:\n{out}"
    assert KEY in out, f"the output does not name {KEY}:\n{out}"
    assert setup.runs == 0, "the validator ran although the value is invalid"


def assert_as_absent(setup: Setup, result: subprocess.CompletedProcess) -> None:
    out = output_of(result)
    assert "Traceback" not in out, out
    assert result.returncode == 0, out
    assert setup.runs == 0, "the validator ran for a key that is not test_track.validate"


# ---------------------------------------------------------------------------------------------
# 6.5: invalid values are config errors
# ---------------------------------------------------------------------------------------------

INVALID = {
    "int": 42,
    "zero": 0,
    "float": 1.5,
    "true": True,
    "false": False,
    "object": {"a": 1},
    "empty_object": {},
    "null": None,
    "mixed_list": ["ok", 3],
    "list_with_null": ["ok", None],
    "list_with_bool": [True],
    "nested_list": ["ok", ["a"]],
    "only_nested_list": [["a", "b"]],
    "list_of_objects": [{"cmd": "x"}],
    "list_of_numbers": [1, 2],
}


@pytest.mark.parametrize("name", sorted(INVALID))
def test_tc62_redteam_invalid_value_is_a_config_error(setup, name):
    setup.config["test_track"] = {"validate": INVALID[name]}
    assert_config_error(setup, setup.gate("1"))


@pytest.mark.parametrize("name", ["null", "int", "object", "mixed_list"])
def test_tc62_redteam_invalid_value_fails_the_whole_spec_gate_too(setup, name):
    setup.config["test_track"] = {"validate": INVALID[name]}
    result = setup.gate()
    assert_config_error(setup, result)


def test_tc62_redteam_invalid_value_with_the_valid_command_never_runs_a_prefix(setup):
    """A list whose first entry is a good command and whose second is a number: the first must not run."""
    setup.config["test_track"] = {"validate": [setup.command, 3]}
    assert_config_error(setup, setup.gate("1"))


def test_tc62_redteam_invalid_value_in_json_output_is_not_green(setup):
    """The machine-readable gate must not report green for a config error."""
    setup.config["test_track"] = {"validate": 42}
    setup.write_config()
    result = subprocess.run(
        [*CK, "gate", "calc", "--task", "1", "--no-reuse", "--json"],
        cwd=setup.project,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode != 0, output_of(result)
    assert "Traceback" not in output_of(result)
    assert KEY in output_of(result)
    assert setup.runs == 0


NOT_AN_OBJECT = {
    "string": "python scripts/validate.py",
    "list": ["python scripts/validate.py"],
    "null": None,
    "int": 7,
    "true": True,
}


@pytest.mark.parametrize("name", sorted(NOT_AN_OBJECT))
def test_tc62_redteam_test_track_itself_not_an_object(setup, name):
    """`test_track` declared as something other than an object cannot declare `validate`: the gate must
    neither crash nor pass as if nothing was declared, and it must not run any command."""
    setup.config["test_track"] = NOT_AN_OBJECT[name]
    result = setup.gate("1")
    out = output_of(result)
    assert "Traceback" not in out, out
    assert setup.runs == 0
    assert result.returncode != 0, f"the gate passed as if test_track were absent:\n{out}"
    assert "test_track" in out, out


def test_tc62_redteam_valid_list_still_runs_each_command_in_order(setup):
    """Guard against over-rejecting: two strings are valid and both run."""
    setup.config["test_track"] = {"validate": setup.command_list(2)}
    result = setup.gate("1")
    assert result.returncode == 0, output_of(result)
    assert setup.runs == 2


# ---------------------------------------------------------------------------------------------
# 6.5: values that are valid strings or lists of strings are not config errors
# ---------------------------------------------------------------------------------------------


def test_tc62_redteam_empty_list_is_declared_but_runs_nothing(setup):
    setup.config["test_track"] = {"validate": []}
    assert_as_absent(setup, setup.gate("1"))


@pytest.mark.parametrize("value", [[""], "", "   ", ["  \t"], ["", ""]])
def test_tc62_redteam_blank_strings_are_not_config_errors_and_do_not_crash(setup, value):
    setup.config["test_track"] = {"validate": value}
    result = setup.gate("1")
    out = output_of(result)
    assert "Traceback" not in out, out
    assert "config error" not in out.lower(), out
    assert setup.runs == 0


def test_tc62_redteam_single_string_runs_once(setup):
    setup.config["test_track"] = {"validate": setup.command}
    result = setup.gate("1")
    assert result.returncode == 0, output_of(result)
    assert setup.runs == 1


# ---------------------------------------------------------------------------------------------
# 6.5: near-miss key names are other keys, not an invalid `test_track.validate`
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("top", ["test-track", "testTrack", "TestTrack", "test_track ", "tests_track", "track"])
def test_tc62_redteam_near_miss_top_level_name_is_ignored(setup, top):
    setup.config[top] = {"validate": setup.command}
    assert_as_absent(setup, setup.gate("1"))


@pytest.mark.parametrize("name", ["Validate", "validator", "validate_cmd", "validate ", "validates", "command"])
def test_tc62_redteam_near_miss_inner_name_is_ignored(setup, name):
    setup.config["test_track"] = {name: setup.command}
    assert_as_absent(setup, setup.gate("1"))


def test_tc62_redteam_dotted_top_level_key_is_not_the_nested_key(setup):
    setup.config["test_track.validate"] = setup.command
    assert_as_absent(setup, setup.gate("1"))


def test_tc62_redteam_near_miss_name_with_invalid_value_is_not_an_error(setup):
    setup.config["testTrack"] = {"validate": 42}
    assert_as_absent(setup, setup.gate("1"))


def test_tc62_redteam_key_in_global_config_only_is_not_declared(setup):
    """The key lives in the project's specs/ck.json: a global preferences file does not declare it."""
    setup.config_home.mkdir(parents=True, exist_ok=True)
    (setup.config_home / "config.json").write_text(json.dumps({"test_track": {"validate": setup.command}}))
    assert_as_absent(setup, setup.gate("1"))


def test_tc62_redteam_invalid_value_in_global_config_only_is_not_an_error(setup):
    setup.config_home.mkdir(parents=True, exist_ok=True)
    (setup.config_home / "config.json").write_text(json.dumps({"test_track": {"validate": 42}}))
    assert_as_absent(setup, setup.gate("1"))


def test_tc62_redteam_invalid_in_project_beats_valid_in_global(setup):
    setup.config_home.mkdir(parents=True, exist_ok=True)
    (setup.config_home / "config.json").write_text(json.dumps({"test_track": {"validate": setup.command}}))
    setup.config["test_track"] = {"validate": 42}
    assert_config_error(setup, setup.gate("1"))


def test_tc62_redteam_the_error_output_fed_back_as_config_is_still_an_error(setup):
    """The system's own output as input: a config whose value is the gate's error text is a string, which is valid
    (and runs as a command), so it never crashes the config load."""
    setup.config["test_track"] = {"validate": 42}
    first = setup.gate("1")
    assert_config_error(setup, first)
    setup.config["test_track"] = {"validate": output_of(first).strip().splitlines()[-1]}
    result = setup.gate("1")
    assert "Traceback" not in output_of(result), output_of(result)


# ---------------------------------------------------------------------------------------------
# 6.6: impl-track gates never run the validator
# ---------------------------------------------------------------------------------------------


def with_validator(setup: Setup, value: Optional[Any] = ABSENT) -> None:
    setup.config["test_track"] = {"validate": setup.command if value is ABSENT else value}


def test_tc63_redteam_impl_gate_does_not_run_validator(setup):
    with_validator(setup)
    result = setup.gate("2")
    assert result.returncode == 0, output_of(result)
    assert setup.runs == 0


def test_tc63_redteam_impl_gate_does_not_run_a_list_of_validators(setup):
    with_validator(setup, setup.command_list(2))
    result = setup.gate("2")
    assert result.returncode == 0, output_of(result)
    assert setup.runs == 0


def test_tc63_redteam_validator_that_would_fail_cannot_fail_an_impl_gate(setup):
    with_validator(setup, "python -c 'import sys; sys.exit(1)'")
    result = setup.gate("2")
    out = output_of(result)
    assert result.returncode == 0, out
    assert "test_track.validate" not in out


def test_tc63_redteam_test_gate_does_run_the_validator(setup):
    """Guards the other tests: the same project with a test-track gate does run it."""
    with_validator(setup)
    result = setup.gate("1")
    assert result.returncode == 0, output_of(result)
    assert setup.runs == 1


@pytest.mark.parametrize(
    "track", ["impl", "impl ", "Impl", "IMPL", " impl", "implementation", "impl test", "impl (tests included)", "integration", "tdd", ""]
)
def test_tc63_redteam_unusual_impl_track_lines_do_not_run_validator(setup, track):
    tasks = (setup.project / "specs" / "calc" / "tasks.md").read_text()
    setup.set_tasks(tasks.replace("**Track:** impl", f"**Track:** {track}"))
    with_validator(setup)
    result = setup.gate("2")
    assert "Traceback" not in output_of(result), output_of(result)
    assert setup.runs == 0


def test_tc63_redteam_missing_track_line_is_impl(setup):
    tasks = (setup.project / "specs" / "calc" / "tasks.md").read_text()
    setup.set_tasks(tasks.replace("**Track:** impl\n", ""))
    with_validator(setup)
    result = setup.gate("2")
    assert result.returncode == 0, output_of(result)
    assert setup.runs == 0


def test_tc63_redteam_bold_variants_of_the_track_line_are_impl(setup):
    tasks = (setup.project / "specs" / "calc" / "tasks.md").read_text()
    setup.set_tasks(tasks.replace("**Track:** impl", "Track: impl"))
    with_validator(setup)
    result = setup.gate("2")
    assert result.returncode == 0, output_of(result)
    assert setup.runs == 0


@pytest.mark.parametrize("track", ["test", "Test", "TEST", "test ", "tests"])
def test_tc63_redteam_unusual_test_track_lines_still_run_validator(setup, track):
    tasks = (setup.project / "specs" / "calc" / "tasks.md").read_text()
    setup.set_tasks(tasks.replace("**Track:** test", f"**Track:** {track}"))
    with_validator(setup)
    result = setup.gate("1")
    assert result.returncode == 0, output_of(result)
    assert setup.runs == 1


def test_tc63_redteam_impl_task_that_owns_test_files_is_still_impl(setup):
    tasks = (setup.project / "specs" / "calc" / "tasks.md").read_text()
    owned = tasks.replace("- `calc.py` - add subtract()", "- `calc.py` - add subtract()\n- `tests/test_subtract.py` - owned by the impl task")
    assert owned != tasks
    setup.set_tasks(owned)
    with_validator(setup)
    result = setup.gate("2")
    assert result.returncode == 0, output_of(result)
    assert setup.runs == 0


def test_tc63_redteam_impl_task_verify_naming_the_validator_is_not_the_validator(setup):
    """An impl task whose own Verify line mentions the script runs that line once, not the declared validator."""
    tasks = (setup.project / "specs" / "calc" / "tasks.md").read_text()
    setup.set_tasks(tasks.replace("**Verified by:** Task 1\n", "**Verified by:** Task 1\n**Verify:** `python -c pass`\n"))
    with_validator(setup)
    result = setup.gate("2")
    assert result.returncode == 0, output_of(result)
    assert setup.runs == 0


def test_tc63_redteam_impl_gate_with_invalid_value_does_not_run_it_either(setup):
    """The invalid value is a test-track concern: the impl gate must not run any command from it
    and must not crash."""
    with_validator(setup, [setup.command, 3])
    result = setup.gate("2")
    assert "Traceback" not in output_of(result), output_of(result)
    assert setup.runs == 0


def test_tc63_redteam_impl_gate_does_not_run_validator_with_no_reuse_off(setup):
    """A reused (cached) impl gate result is no reason to run the validator either."""
    with_validator(setup)
    setup.write_config()
    for _ in range(2):
        result = subprocess.run([*CK, "gate", "calc", "--task", "2"], cwd=setup.project, capture_output=True, text=True, timeout=300)
        assert result.returncode == 0, output_of(result)
    assert setup.runs == 0


def test_tc63_redteam_mixed_gate_runs_validator_once_for_the_test_task_only(setup):
    with_validator(setup)
    result = setup.gate("1", "2")
    assert result.returncode == 0, output_of(result)
    assert setup.runs == 1, "a mixed gate runs the validator once, for its test-track task, not once per task"


def test_tc63_redteam_mixed_gate_order_does_not_matter(setup):
    with_validator(setup)
    result = setup.gate("2", "1")
    assert result.returncode == 0, output_of(result)
    assert setup.runs == 1


def test_tc63_redteam_mixed_gate_with_failing_validator_fails_and_names_it(setup):
    with_validator(setup, "python -c 'import sys; sys.exit(3)'")
    result = setup.gate("1", "2")
    out = output_of(result)
    assert result.returncode != 0, out
    assert "test_track.validate" in out, out


def test_tc63_redteam_mixed_gate_with_invalid_value_is_a_config_error(setup):
    with_validator(setup, 42)
    assert_config_error(setup, setup.gate("1", "2"))


def test_tc63_redteam_impl_gate_runs_in_the_task_checkout_not_for_a_test_neighbour(setup):
    """Gating the impl task twice around a test-task gate: only the test-task gate leaves a marker line."""
    with_validator(setup)
    assert setup.gate("2").returncode == 0
    assert setup.runs == 0
    assert setup.gate("1").returncode == 0
    assert setup.runs == 1
    assert setup.gate("2").returncode == 0
    assert setup.runs == 1
