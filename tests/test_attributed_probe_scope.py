"""Review round 1 of Task 20: the attributed gate's collection probe keeps the Verify's own selection, and the
compact object's `failedChecks` lists only non-command checks.

CLI case: the project's full-suite Verify deselects everything but tc3_, so the unowned test file is covered by no
run. Task 2's Verify (`-k "tc1_ or tc2_"` on tests/test_calc_tc.py) must be probed with its own arguments: the probe
collects only tc1_/tc2_ (covered by Task 1's file run), so the unowned test is never run and never reported.
"""

from pathlib import Path

import unattended_helpers as h
from claude_kiro.attribution import Attribution, gate_verdict
from claude_kiro.failures import Failure
from claude_kiro.gate import Check, GateResult

UNOWNED = "tests/test_extra.py::test_unowned_fail"


def test_a_verify_probe_keeps_its_own_selection_so_unselected_files_do_not_run(tmp_path):
    root = h.attr_project(
        tmp_path,
        tests={
            "tests/test_calc_tc.py::test_tc1_add": "pass",
            "tests/test_calc_tc.py::test_tc3_sub": "pass",
            UNOWNED: "fail",
        },
        config={"verify": ["python -m pytest -q -p no:cacheprovider tests/test_calc_tc.py -k tc3_"]},
    )
    code, obj, err = h.ck_json(root, "gate", "demo", "--attribute", "--json")
    assert isinstance(obj, dict), f"gate printed no JSON (exit {code}); stderr: {err[-600:]}"
    assert h.counter_lines(root, "test_unowned_fail") == 0, "a Verify's probe ran a test its own selection does not name"
    assert [f for f in obj["failures"] if f.get("node") == UNOWNED] == []


def test_excused_failures_on_a_green_gate_leave_failed_checks_empty():
    failure = Failure(
        node="tests/test_calc_tc.py::test_tc3_sub", file=None, kind="failed", message="boom", truncated=False,
        command="python -m pytest -q tests", exc_type=None, phase=None, crash_path=None, crash_line=None,
        detail="", missing_command=None,
    )
    attr = Attribution(tc_id="TC-3", tasks=("3",), by="tc", candidate=False, excused_by=("3",), warnings=())
    verify_run = Check("`python -m pytest -q tests`", False, "exit 1\nboom", command=True)
    result = GateResult(
        spec_dir=Path("specs/demo"), tasks=["2"], checks=[verify_run], failures=[failure], attributions=[attr],
        attribute=True,
    )
    result.verdict = gate_verdict([(failure, attr)], {}, other_checks_ok=True)
    compact = result.to_compact(None)
    assert compact["ok"] is True
    assert compact["failedChecks"] == []
    assert compact["summary"]["failedChecks"] == 0
    assert compact["excusedByTask"] == {"3": 1}


def test_a_failed_non_command_check_still_shows_in_failed_checks_under_attribute():
    failure = Failure(
        node="tests/test_calc_tc.py::test_tc3_sub", file=None, kind="failed", message="boom", truncated=False,
        command="python -m pytest -q tests", exc_type=None, phase=None, crash_path=None, crash_line=None,
        detail="", missing_command=None,
    )
    attr = Attribution(tc_id="TC-3", tasks=("3",), by="tc", candidate=False, excused_by=("3",), warnings=())
    verify_run = Check("`python -m pytest -q tests`", False, "exit 1\nboom", command=True)
    tamper = Check("No existing tests deleted or weakened", False, "tests/test_x.py: deleted")
    result = GateResult(
        spec_dir=Path("specs/demo"), tasks=["2"], checks=[verify_run, tamper], failures=[failure],
        attributions=[attr], attribute=True,
    )
    result.verdict = gate_verdict([(failure, attr)], {}, other_checks_ok=False)
    compact = result.to_compact(None)
    assert compact["ok"] is False
    assert [c["name"] for c in compact["failedChecks"]] == ["No existing tests deleted or weakened"]


# A Verify that runs nothing has nothing to attribute: a warning under --attribute (the plain task gate still fails).


def _extra(verify: str) -> str:
    return (f"### Task 9: Extra\n**Status:** Done\n**Track:** impl\n**Files:**\n- `extra.py` - x\n**Verify:** `{verify}`\n"
            "**Dependencies:** None\n\n**Acceptance:**\n- [x] works\n\n---\n\n")


def _attributed(tmp_path, verify):
    root = h.attr_project(tmp_path, statuses={"1": "Done", "2": "Done", "3": "Done"}, extra_tasks=_extra(verify),
                          tests={"tests/test_calc_tc.py::test_tc1_add": "pass", "tests/test_calc_tc.py::test_tc3_sub": "pass"})
    code, obj, err = h.ck_json(root, "gate", "demo", "--attribute", "--json")
    assert isinstance(obj, dict), f"gate printed no JSON (exit {code}); stderr: {err[-600:]}"
    return root, code, obj


def _names(obj, verify):
    return [f for f in obj["failures"] if verify in (f.get("command") or "")]


def test_a_verify_selecting_nothing_warns_and_adds_no_failure(tmp_path):
    verify = 'python -m pytest -q tests/test_calc_tc.py -k "tc999_"'
    _, code, obj = _attributed(tmp_path, verify)
    assert _names(obj, verify) == [], obj["failures"]
    assert any(verify in w and "selects no tests" in w for w in obj["warnings"]), obj["warnings"]
    assert obj["ok"] is True and code == 0, obj


def test_a_verify_naming_an_absent_file_warns_and_adds_no_failure(tmp_path):
    verify = "python -m pytest -q tests/test_absent.py"
    _, code, obj = _attributed(tmp_path, verify)
    assert _names(obj, verify) == [], obj["failures"]
    assert any(verify in w and "selects no tests" in w for w in obj["warnings"]), obj["warnings"]
    assert obj["ok"] is True and code == 0, obj


def test_a_verify_with_a_bad_option_is_still_a_failure(tmp_path):
    verify = "python -m pytest -q --no-such-option tests/test_calc_tc.py"
    _, code, obj = _attributed(tmp_path, verify)
    assert not any(verify in w and "selects no tests" in w for w in obj["warnings"]), obj["warnings"]
    assert obj["ok"] is False and code != 0, obj


def test_the_plain_task_gate_still_fails_on_a_verify_selecting_nothing(tmp_path):
    root, _, _ = _attributed(tmp_path, 'python -m pytest -q tests/test_calc_tc.py -k "tc999_"')
    code, obj, err = h.ck_json(root, "gate", "demo", "--task", "9", "--json")
    assert isinstance(obj, dict), f"gate printed no JSON (exit {code}); stderr: {err[-600:]}"
    assert obj["ok"] is False and code != 0, obj
