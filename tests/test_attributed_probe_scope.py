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
