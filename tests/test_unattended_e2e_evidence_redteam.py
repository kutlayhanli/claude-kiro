"""Red team of requirement 20.3 (TC-166): the rerun evidence's hand-repair rule.

Written from the requirements only. Each test builds a synthetic evidence document in `tmp_path`,
writes it as the fenced json block of a `reruns.md`, reads it back, and applies the same
`validate_evidence` that checks the committed evidence (tests/test_unattended_e2e_evidence.py).
Valid documents pass; every break is rejected, and the problem names criterion 20.3 or the
criterion the break violates, plus the run ID (20.7).
"""

import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

PROPERTIES: Dict[str, str] = {}

BASELINE_COST = 1.25
BASELINE_WALL = 4.5


def _arm(name: str, harness: str, run: str) -> Dict[str, Any]:
    return {
        "name": name,
        "harness": harness,
        "model": "some-model",
        "run": run,
        "passes": [{"progressRun": f"{run}-p1", "runId": run}],
        "tasksDone": 10,
        "tasksTotal": 10,
        "finalGate": {"state": "green", "afterLastMerge": True},
        "suite": {"passed": 120, "failed": 0, "errors": 0},
        "handRepairs": [],
        "approvals": [{"task": 93, "kind": "ck worktree approve"}, {"task": 94, "kind": "ck worktree approve"}],
        "hooksAtStart": True,
        "audit": {"complete": True, "clean": True},
        "costPerMergedTask": 1.0,
        "wallMinPerTask": 4.0,
        "failedCriteria": [],
    }


def _doc() -> Dict[str, Any]:
    return {
        "baseline": {"run": "base-run", "costPerMergedTask": BASELINE_COST, "wallMinPerTask": BASELINE_WALL},
        "arms": [
            _arm("muse", "opencode", "run-muse"),
            _arm("flash", "opencode", "run-flash"),
            _arm("claude", "claude", "run-claude"),
            _arm("dsh", "dsh", "run-dsh"),
        ],
        "thinAgentContext": {"leanCacheWrite": 40, "defaultCacheWrite": 100, "runs": ["a", "b"]},
        "sandboxTests": {"host": "linux", "tc88": "passed", "tc189": "passed"},
    }


def _validate(tmp_path: Path, doc: Any, dsh_findings: Any = None) -> List[str]:
    """Round-trip the document through a reruns.md file, as the committed evidence is read."""
    from test_unattended_e2e_evidence import _json_block, validate_evidence

    path = tmp_path / "reruns.md"
    path.write_text("# Reruns\n\n```json\n" + json.dumps(doc, indent=2) + "\n```\n", encoding="utf-8")
    return validate_evidence(_json_block(path), dsh_findings)


def _mutated(mutate: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    doc = copy.deepcopy(_doc())
    mutate(doc)
    return doc


def _rejected(tmp_path: Path, mutate: Callable[[Dict[str, Any]], None], criterion: str = "20.3",
              names_run: bool = True) -> None:
    problems = _validate(tmp_path, _mutated(mutate))
    assert problems, "the break was accepted"
    assert any(f"criterion {criterion}" in p for p in problems), problems
    if names_run:
        assert any("run-muse" in p or "run-flash" in p or "run-claude" in p or "run-dsh" in p
                   or "run ID" in p for p in problems), problems


def _first(doc: Dict[str, Any]) -> Dict[str, Any]:
    return doc["arms"][0]


# --- valid documents pass ------------------------------------------------------------------------


def test_tc166_redteam_valid_document_passes(tmp_path):
    assert _validate(tmp_path, _doc()) == []


def test_tc166_redteam_approvals_of_93_and_94_do_not_count_as_hand_repair(tmp_path):
    doc = _doc()
    for arm in doc["arms"]:
        arm["approvals"] = [{"task": 93, "kind": "ck worktree approve"}]
    doc["arms"][1]["approvals"] = []
    doc["arms"][2]["approvals"] = [{"task": 94, "kind": "ck worktree approve"},
                                   {"task": 93, "kind": "ck worktree approve"}]
    assert _validate(tmp_path, doc) == []


def test_tc166_redteam_multiple_passes_of_one_run_id_are_not_a_hand_repair(tmp_path):
    doc = _doc()
    _first(doc)["passes"] = [{"progressRun": "p1", "runId": "run-muse"}, {"progressRun": "p2", "runId": "run-muse"}]
    assert _validate(tmp_path, doc) == []


def test_tc166_redteam_three_arms_pass_only_with_dsh_findings(tmp_path):
    doc = _doc()
    doc["arms"] = doc["arms"][:3]
    findings = {"canHostAgent": False, "canHostCk": True}
    assert _validate(tmp_path, doc, findings) == []
    assert _validate(tmp_path, doc, None)
    assert _validate(tmp_path, doc, {"canHostAgent": True, "canHostCk": True})


def test_tc166_redteam_cost_exactly_at_baseline_passes(tmp_path):
    doc = _doc()
    _first(doc)["costPerMergedTask"] = BASELINE_COST
    _first(doc)["wallMinPerTask"] = BASELINE_WALL
    assert _validate(tmp_path, doc) == []


# --- approvals: nothing else passes as an approval -----------------------------------------------


def test_tc166_redteam_approval_for_another_task(tmp_path):
    for task in (1, 92, 95, 0, -93, 930, 9, 39):
        _rejected(tmp_path, lambda d, t=task: _first(d).update(approvals=[{"task": t, "kind": "ck worktree approve"}]))


@pytest.mark.parametrize("task", ["093", "93", "94", 93.0, 94.0, 93.5, True, None, [93], {"n": 93}, "Task 93", "٩٣"])
def test_tc166_redteam_task_number_surface_forms(tmp_path, task):
    _rejected(tmp_path, lambda d: _first(d).update(approvals=[{"task": task, "kind": "ck worktree approve"}]))


@pytest.mark.parametrize("kind", [
    "git commit", "hand commit", "git merge", "ck worktree land", "ck worktree approve --force",
    "CK WORKTREE APPROVE", "ck worktree approve ", " ck worktree approve", "ck worktree  approve",
    "ck worktree approved", "ck worktree", "", None, 1, ["ck worktree approve"], "manual", "ck approve",
])
def test_tc166_redteam_approval_kind_other_than_the_command(tmp_path, kind):
    _rejected(tmp_path, lambda d: _first(d).update(approvals=[{"task": 93, "kind": kind}]))


def test_tc166_redteam_hand_commit_listed_under_approvals(tmp_path):
    hand = {"task": 93, "kind": "git commit", "sha": "abc1234", "message": "approve protected change by hand"}
    _rejected(tmp_path, lambda d: _first(d).update(approvals=[hand]))


def test_tc166_redteam_one_bad_approval_among_good_ones(tmp_path):
    good = {"task": 93, "kind": "ck worktree approve"}
    _rejected(tmp_path, lambda d: _first(d).update(approvals=[good, {"task": 94, "kind": "git commit"}, good]))


def test_tc166_redteam_approval_missing_fields_or_wrong_shape(tmp_path):
    for bad in ({}, {"task": 93}, {"kind": "ck worktree approve"}, "ck worktree approve", 93, None, [93]):
        _rejected(tmp_path, lambda d, b=bad: _first(d).update(approvals=[b]))


@pytest.mark.parametrize("approvals", [None, "none", "ck worktree approve", 0, {"task": 93, "kind": "ck worktree approve"}])
def test_tc166_redteam_approvals_not_a_list(tmp_path, approvals):
    _rejected(tmp_path, lambda d: _first(d).update(approvals=approvals))


def test_tc166_redteam_missing_approvals_key(tmp_path):
    _rejected(tmp_path, lambda d: _first(d).pop("approvals"))


def test_tc166_redteam_approval_task_key_missing_but_extra_key_names_93(tmp_path):
    _rejected(tmp_path, lambda d: _first(d).update(approvals=[{"taskId": 93, "kind": "ck worktree approve"}]))


# --- handRepairs ---------------------------------------------------------------------------------


@pytest.mark.parametrize("hand", [
    None, "", "none", "[]", "0", 0, False, {}, [None], [""], [[]], [{}], ["fixed a test by hand"],
    [{"sha": "abc1234", "what": "edited a file"}], [93], ["approved 93"],
])
def test_tc166_redteam_hand_repairs_must_be_an_empty_list(tmp_path, hand):
    _rejected(tmp_path, lambda d: _first(d).update(handRepairs=hand))


def test_tc166_redteam_hand_repairs_key_missing(tmp_path):
    _rejected(tmp_path, lambda d: _first(d).pop("handRepairs"))


def test_tc166_redteam_hand_repair_in_the_last_arm_only(tmp_path):
    _rejected(tmp_path, lambda d: d["arms"][3].update(handRepairs=["edited by hand"]))


def test_tc166_redteam_hand_repair_hidden_by_valid_approvals(tmp_path):
    def mutate(d: Dict[str, Any]) -> None:
        _first(d).update(handRepairs=[{"what": "hand commit"}],
                         approvals=[{"task": 93, "kind": "ck worktree approve"}])
    _rejected(tmp_path, mutate)


# --- passes: the same run ID, no other change ----------------------------------------------------


def test_tc166_redteam_pass_run_id_differs_from_arm_run(tmp_path):
    _rejected(tmp_path, lambda d: _first(d)["passes"].append({"progressRun": "p2", "runId": "run-muse-2"}), "2.13")


def test_tc166_redteam_pass_run_id_is_a_near_miss(tmp_path):
    for near in ("run-muse ", " run-muse", "RUN-MUSE", "run-muse\n", "run_muse", "run-mus", "run-muse​"):
        _rejected(tmp_path, lambda d, n=near: _first(d).update(passes=[{"progressRun": "p", "runId": n}]), "2.13")


@pytest.mark.parametrize("run_id", [None, 0, 1, [], "", ["run-muse"]])
def test_tc166_redteam_pass_run_id_wrong_type_or_empty(tmp_path, run_id):
    _rejected(tmp_path, lambda d: _first(d).update(passes=[{"progressRun": "p", "runId": run_id}]), "2.13")


def test_tc166_redteam_pass_missing_run_id(tmp_path):
    _rejected(tmp_path, lambda d: _first(d).update(passes=[{"progressRun": "p"}]), "2.13")


def test_tc166_redteam_pass_without_progress_run(tmp_path):
    for p in ({"runId": "run-muse"}, {"progressRun": "", "runId": "run-muse"},
              {"progressRun": None, "runId": "run-muse"}, "run-muse", None):
        _rejected(tmp_path, lambda d, p=p: _first(d).update(passes=[p]), "2.13")


@pytest.mark.parametrize("passes", [None, [], "p1", {"progressRun": "p", "runId": "run-muse"}])
def test_tc166_redteam_passes_missing_empty_or_not_a_list(tmp_path, passes):
    _rejected(tmp_path, lambda d: _first(d).update(passes=passes), "2.13")


def test_tc166_redteam_second_pass_started_fresh_among_valid_passes(tmp_path):
    def mutate(d: Dict[str, Any]) -> None:
        _first(d)["passes"] = [{"progressRun": "p1", "runId": "run-muse"},
                               {"progressRun": "p2", "runId": "run-muse"},
                               {"progressRun": "p3", "runId": "another-run"}]
    _rejected(tmp_path, mutate, "2.13")


def test_tc166_redteam_arm_run_id_missing_or_empty(tmp_path):
    _rejected(tmp_path, lambda d: _first(d).pop("run"), "2.13")
    _rejected(tmp_path, lambda d: _first(d).update(run=""), "2.13")
    _rejected(tmp_path, lambda d: _first(d).update(run=None), "2.13")
    _rejected(tmp_path, lambda d: _first(d).update(run=7), "2.13")


# --- duplicate arms ------------------------------------------------------------------------------


def test_tc166_redteam_duplicate_arm_name(tmp_path):
    def mutate(d: Dict[str, Any]) -> None:
        d["arms"][1]["name"] = d["arms"][0]["name"]
    _rejected(tmp_path, mutate, "20.2", names_run=False)


def test_tc166_redteam_duplicate_arm_copied_whole(tmp_path):
    def mutate(d: Dict[str, Any]) -> None:
        d["arms"][3] = copy.deepcopy(d["arms"][0])
    _rejected(tmp_path, mutate, "20.2", names_run=False)


def test_tc166_redteam_two_arms_share_a_run_id(tmp_path):
    def mutate(d: Dict[str, Any]) -> None:
        d["arms"][1]["run"] = "run-muse"
        d["arms"][1]["passes"] = [{"progressRun": "p", "runId": "run-muse"}]
    _rejected(tmp_path, mutate, "20.2")


def test_tc166_redteam_five_arms_or_two_arms(tmp_path):
    _rejected(tmp_path, lambda d: d["arms"].append(_arm("extra", "claude", "run-extra")), "20.2", names_run=False)
    _rejected(tmp_path, lambda d: d.update(arms=d["arms"][:2]), "20.2", names_run=False)
    _rejected(tmp_path, lambda d: d.update(arms=[]), "20.2", names_run=False)


def test_tc166_redteam_three_arms_with_dsh_present_and_findings_applying(tmp_path):
    doc = _doc()
    doc["arms"] = [doc["arms"][3], doc["arms"][0], doc["arms"][1]]
    assert _validate(tmp_path, doc, {"canHostAgent": False, "canHostCk": False})


@pytest.mark.parametrize("arms", [None, "four arms", {"a": 1}, 4])
def test_tc166_redteam_arms_not_a_list(tmp_path, arms):
    problems = _validate(tmp_path, _mutated(lambda d: d.update(arms=arms)))
    assert problems


def test_tc166_redteam_arm_not_an_object(tmp_path):
    for bad in (None, "arm", 1, [], ["x"]):
        problems = _validate(tmp_path, _mutated(lambda d, b=bad: d["arms"].__setitem__(2, b)))
        assert problems, bad


@pytest.mark.parametrize("doc", [None, [], "evidence", 1, [{"arms": []}]])
def test_tc166_redteam_document_not_an_object(tmp_path, doc):
    assert _validate(tmp_path, doc)


# --- failedCriteria (20.7) -----------------------------------------------------------------------


def test_tc166_redteam_failed_bar_without_failed_criteria_key(tmp_path):
    def mutate(d: Dict[str, Any]) -> None:
        _first(d).update(handRepairs=["hand fix"])
        _first(d).pop("failedCriteria")
    _rejected(tmp_path, mutate, "20.7")


@pytest.mark.parametrize("fc", [None, "", "20.3", 0, {"criterion": "20.3", "runId": "run-muse"}])
def test_tc166_redteam_failed_criteria_not_a_list(tmp_path, fc):
    _rejected(tmp_path, lambda d: _first(d).update(failedCriteria=fc), "20.7")


def test_tc166_redteam_failed_bar_with_empty_failed_criteria(tmp_path):
    def mutate(d: Dict[str, Any]) -> None:
        _first(d).update(handRepairs=["hand fix"], failedCriteria=[])
    _rejected(tmp_path, mutate, "20.7")


def test_tc166_redteam_failed_criteria_without_run_id(tmp_path):
    def mutate(d: Dict[str, Any]) -> None:
        _first(d).update(handRepairs=["hand fix"], failedCriteria=[{"criterion": "20.3"}])
    _rejected(tmp_path, mutate, "20.7")


def test_tc166_redteam_failed_criteria_with_another_arms_run_id(tmp_path):
    def mutate(d: Dict[str, Any]) -> None:
        _first(d).update(handRepairs=["hand fix"], failedCriteria=[{"criterion": "20.3", "runId": "run-flash"}])
    _rejected(tmp_path, mutate, "20.7")


def test_tc166_redteam_failed_criteria_naming_the_wrong_criterion(tmp_path):
    def mutate(d: Dict[str, Any]) -> None:
        _first(d).update(handRepairs=["hand fix"], failedCriteria=[{"criterion": "20.5", "runId": "run-muse"}])
    _rejected(tmp_path, mutate, "20.7")


def test_tc166_redteam_recorded_failure_still_fails_the_document(tmp_path):
    def mutate(d: Dict[str, Any]) -> None:
        _first(d).update(handRepairs=["hand fix"], failedCriteria=[{"criterion": "20.3", "runId": "run-muse"}])
    problems = _validate(tmp_path, _mutated(mutate))
    assert problems and any("criterion 20.3" in p for p in problems)


def test_tc166_redteam_failed_criteria_entry_is_a_bare_string(tmp_path):
    def mutate(d: Dict[str, Any]) -> None:
        _first(d).update(failedCriteria=["20.3"])
    _rejected(tmp_path, mutate, "20.7")


# --- cost and wall bar at the boundary (20.5) ----------------------------------------------------


def test_tc166_redteam_cost_one_cent_over_baseline(tmp_path):
    _rejected(tmp_path, lambda d: _first(d).update(costPerMergedTask=BASELINE_COST + 0.01), "20.5")


def test_tc166_redteam_wall_minutes_over_baseline(tmp_path):
    _rejected(tmp_path, lambda d: _first(d).update(wallMinPerTask=BASELINE_WALL + 0.01), "20.5")


@pytest.mark.parametrize("value", [None, "1.00", "", True, False, [1.0], {"usd": 1.0}, -0.01])
def test_tc166_redteam_cost_not_a_usable_number(tmp_path, value):
    def mutate(d: Dict[str, Any]) -> None:
        _first(d).update(costPerMergedTask=value)
    problems = _validate(tmp_path, _mutated(mutate))
    assert any("criterion 20.5" in p for p in problems), problems


def test_tc166_redteam_cost_key_missing(tmp_path):
    _rejected(tmp_path, lambda d: _first(d).pop("costPerMergedTask"), "20.5")
    _rejected(tmp_path, lambda d: _first(d).pop("wallMinPerTask"), "20.5")


def test_tc166_redteam_baseline_missing_or_malformed(tmp_path):
    for bad in (None, "base", {}, {"run": "b", "costPerMergedTask": "1", "wallMinPerTask": 1},
                {"run": "b", "costPerMergedTask": 1}):
        problems = _validate(tmp_path, _mutated(lambda d, b=bad: d.update(baseline=b)))
        assert any("20.5" in p for p in problems), bad


# --- the other bars still bind next to a clean 20.3 ----------------------------------------------


def test_tc166_redteam_final_gate_not_green_or_not_after_last_merge(tmp_path):
    for gate in ({"state": "red", "afterLastMerge": True}, {"state": "error", "afterLastMerge": True},
                 {"state": "green", "afterLastMerge": False}, {"state": "green"}, {"state": "Green", "afterLastMerge": True},
                 {"state": "green", "afterLastMerge": "true"}, {"state": "green", "green": False, "afterLastMerge": True},
                 "green", None):
        _rejected(tmp_path, lambda d, g=gate: _first(d).update(finalGate=g), "20.2")


def test_tc166_redteam_tasks_not_all_done(tmp_path):
    for done, total in ((9, 10), (10, 9), (0, 0), ("10", "10"), (10.0, 10.0), (True, True), (None, None)):
        _rejected(tmp_path, lambda d, a=done, b=total: _first(d).update(tasksDone=a, tasksTotal=b), "20.2")


def test_tc166_redteam_suite_not_green(tmp_path):
    for suite in ({"passed": 0, "failed": 0, "errors": 0}, {"passed": 5, "failed": 1, "errors": 0},
                  {"passed": 5, "failed": 0, "errors": 1}, {"passed": 5}, {"passed": "5", "failed": 0, "errors": 0},
                  {"passed": 5, "failed": False, "errors": 0}, {"passed": 5, "failed": 0.0, "errors": 0}, None):
        _rejected(tmp_path, lambda d, s=suite: _first(d).update(suite=s), "20.2")


def test_tc166_redteam_hooks_and_audit(tmp_path):
    for value in (False, None, "true", 1):
        _rejected(tmp_path, lambda d, v=value: _first(d).update(hooksAtStart=v), "9.10")
    for audit in (None, "clean", {"clean": False, "complete": True}, {"complete": True}, {"clean": "true", "complete": True},
                  {"clean": True, "complete": False}, []):
        _rejected(tmp_path, lambda d, a=audit: _first(d).update(audit=a), "9.10")


def test_tc166_redteam_problem_names_the_run_id_of_the_broken_arm(tmp_path):
    problems = _validate(tmp_path, _mutated(lambda d: d["arms"][2].update(handRepairs=["hand fix"])))
    assert problems
    assert all("run-claude" in p for p in problems if "criterion 20.3" in p), problems


def test_tc166_redteam_system_output_fed_back_is_not_trusted(tmp_path):
    """The validator's own problem strings, pasted into the evidence, are not an approval or a repair list."""
    problems = _validate(tmp_path, _mutated(lambda d: _first(d).update(handRepairs=["x"])))
    assert problems
    _rejected(tmp_path, lambda d: _first(d).update(handRepairs=[], approvals=problems))
    _rejected(tmp_path, lambda d: _first(d).update(failedCriteria=problems), "20.7")
