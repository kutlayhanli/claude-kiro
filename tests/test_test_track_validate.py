"""Story 6: the project-declared test-track fixture validation command (TC-58 to TC-63).

Real projects, real worktrees, real `ck gate` and config load, real subprocesses. The validation script proves
whether and where it ran by writing a marker file under $CK_MARK_DIR.
"""

import json
import re
from pathlib import Path

import pytest

from unattended_helpers import (
    attr_project,
    ck_run,
    commit_all,
    integration_worktree,
    task_worktree,
)

PASSING_TESTS = {"tc1_add": "pass", "tc2_neg": "pass", "tc3_sub": "pass"}

MARKER_SCRIPT = """\
import os, pathlib, sys
mark = pathlib.Path(os.environ["CK_MARK_DIR"]) / (sys.argv[1] if len(sys.argv) > 1 else "validate")
mark.write_text(os.getcwd())
"""

CWD_SCRIPT = """\
import os, pathlib
(pathlib.Path(os.environ["CK_MARK_DIR"]) / "validate").write_text(os.getcwd())
"""

NOISY_FAIL_SCRIPT = """\
import os, pathlib, sys
(pathlib.Path(os.environ["CK_MARK_DIR"]) / "validate").write_text(os.getcwd())
for i in range(1, 61):
    print(f"validate-line-{i}")
sys.exit(1)
"""

SLEEP_SCRIPT = """\
import time
time.sleep(5)
"""

RAN_SCRIPT = """\
import os, pathlib, sys
(pathlib.Path(os.environ["CK_MARK_DIR"]) / sys.argv[1]).write_text(os.getcwd())
"""


def make_project(tmp_path, validate=None, *, extra_config=None, scripts=None, statuses=None, absent=False):
    """An attr_project whose scripts are committed on integrate/demo; returns (root, mark_dir)."""
    config = dict(extra_config or {})
    if not absent:
        config["test_track"] = {"validate": validate}
    root = attr_project(tmp_path, statuses=statuses or {}, tests=PASSING_TESTS, config=config)
    iw = integration_worktree(root)
    (iw / "scripts").mkdir(exist_ok=True)
    for name, body in (scripts or {"validate.py": CWD_SCRIPT}).items():
        (iw / "scripts" / name).write_text(body)
    tasks_md = iw / "specs" / "demo" / "tasks.md"
    tasks_md.write_text(tasks_md.read_text().replace("- [ ]", "- [x]"))  # unchecked boxes would fail every gate
    commit_all(iw, "add scripts")
    mark_dir = tmp_path / "marks"
    mark_dir.mkdir(exist_ok=True)
    return root, mark_dir


def gate(root, task, mark_dir, *, spec="demo"):
    code, _, err = _create(root, task)
    assert code == 0, err
    wt = task_worktree(root, task)
    out = ck_run(wt, "gate", spec, "--task", str(task), env={"CK_MARK_DIR": str(mark_dir)})
    return wt, out


def _create(root, task):
    out = ck_run(root, "worktree", "create", "demo", str(task), "--base", "integrate/demo")
    return out.returncode, out.stdout, out.stderr


def test_tc58_validate_runs_in_the_task_worktree(tmp_path):
    root, marks = make_project(tmp_path, "python scripts/validate.py")
    wt, out = gate(root, 1, marks)
    marker = marks / "validate"
    assert marker.exists(), f"the declared validation command did not run\n{out.stdout}\n{out.stderr}"
    assert Path(marker.read_text()).resolve() == wt.resolve()


def test_tc59a_nonzero_exit_fails_the_gate_and_shows_command_and_tail(tmp_path):
    root, marks = make_project(tmp_path, "python scripts/validate.py", scripts={"validate.py": NOISY_FAIL_SCRIPT})
    wt, out = gate(root, 1, marks)
    text = out.stdout + out.stderr
    assert (marks / "validate").exists(), f"the validation command did not run\n{text}"
    assert out.returncode != 0, text
    assert "python scripts/validate.py" in text
    assert "validate-line-60" in text
    assert "validate-line-1\n" not in text, "only the tail of the output should be shown"


def test_tc60_command_over_the_verify_timeout_fails_as_timeout(tmp_path):
    root, marks = make_project(
        tmp_path,
        "python scripts/slow.py",
        extra_config={"verify_timeout": 1},
        scripts={"slow.py": SLEEP_SCRIPT},
    )
    # Distinguish a timed-out validation check from a timeout of some other command: it must name the command.
    wt, out = gate(root, 1, marks)
    text = out.stdout + out.stderr
    assert out.returncode != 0, text
    block = [line for line in text.splitlines() if "slow.py" in line or "timeout" in line.lower() or "timed out" in line.lower()]
    assert any("slow.py" in line for line in block), f"no check names the validation command\n{text}"
    assert any(("timeout" in line.lower() or "timed out" in line.lower()) for line in block), text


def _check_lines(text):
    """The gate report with temp paths and durations normalised, so two projects compare equal."""
    text = re.sub(r"/\S*/(proj|tmp[^/\s]*)\S*", "<path>", text)
    text = re.sub(r"\d+(\.\d+)?\s*(s|ms)\b", "<t>", text)
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def test_tc61_no_key_means_no_validation_check_and_the_same_checks_as_before(tmp_path):
    # Preservation: the same project, one carrying the script, gated without the key.
    root_a, marks_a = make_project(tmp_path / "a", None, absent=True)
    wt_a, out_a = gate(root_a, 1, marks_a)
    root_b, marks_b = make_project(tmp_path / "b", None, absent=True, scripts={"unrelated.py": "pass\n"})
    wt_b, out_b = gate(root_b, 1, marks_b)
    assert not (marks_a / "validate").exists()
    text = out_a.stdout + out_a.stderr
    assert "validate" not in text.lower() and "scripts/" not in text
    assert out_a.returncode == out_b.returncode
    assert _check_lines(out_a.stdout) == _check_lines(out_b.stdout)


def _counting_project(tmp_path, validate):
    scripts = {"a.py": RAN_SCRIPT, "b.py": RAN_SCRIPT, "validate.py": CWD_SCRIPT}
    config = {"verify_always": ["python scripts/a.py ran_test_command"]}
    return make_project(tmp_path, validate, extra_config=config, scripts=scripts)


@pytest.mark.parametrize(
    "bad",
    [42, {"a": 1}, ["ok", 3], None],
    ids=["int", "mapping", "mixed-list", "null"],
)
def test_tc62_invalid_value_is_a_config_error_naming_the_key(tmp_path, bad):
    root, marks = _counting_project(tmp_path, bad)
    wt, out = gate(root, 1, marks)
    text = out.stdout + out.stderr
    assert out.returncode != 0, text
    assert "test_track.validate" in text, text
    assert not (marks / "ran_test_command").exists(), "no command may run when the config is invalid"


def test_tc62_list_of_strings_runs_every_command(tmp_path):
    root, marks = make_project(
        tmp_path,
        ["python scripts/a.py ran_a", "python scripts/b.py ran_b"],
        scripts={"a.py": RAN_SCRIPT, "b.py": RAN_SCRIPT},
    )
    wt, out = gate(root, 1, marks)
    assert (marks / "ran_a").exists(), out.stdout + out.stderr
    assert (marks / "ran_b").exists(), out.stdout + out.stderr


def test_tc63_impl_task_gate_does_not_run_validation(tmp_path):
    root, marks = make_project(tmp_path, "python scripts/validate.py", statuses={"1": "Done"})
    wt, out = gate(root, 2, marks)
    assert not (marks / "validate").exists(), "an impl-track gate must not run the test-track validation"
