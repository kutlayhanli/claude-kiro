"""Red-team for Story 5's SHALL NOT clause (run-principles 5.7), through the real workflow under the fake runtime.

5.7: WHEN a ruling at the risky tier returns "needs a stronger look", THE SYSTEM SHALL NOT call the arbiter again.

Every case runs the real `spec-implement.js` against the fake runtime with the real `ck` CLI (`execCk`) over a real git
repo, so the arbiter's ledger is the real ledger. Task 1 reports a test that can never pass, so the claim goes to the
arbiter; the normal tier asks for a stronger look and the risky tier is scripted with the adversarial answer.
What the cases hold in common: the arbiter agent is called at most twice for the claim (normal, then risky), the task
ends in needs-input and is never merged, and no ruling reaches a lane (no second task attempt, no test fixer, no
arbiter landing). The positive control proves the scenario reaches the risky tier at all.
"""

import json
import re
from typing import Any, Dict, List, Optional

import pytest

from arbiter_helpers import arbiter_project
from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

pytestmark = pytest.mark.usefixtures("no_network")

CONFLICT = {"file": "tests/test_greet.py", "test": "test_greets", "criterion": "1.1",
            "reason": "asserts a greeting no correct code prints", "fix": "assert the greeting the requirement names"}
ROLE = {"model": "sonnet", "effort": "medium", "risky_model": "opus", "risky_effort": "high"}
FIRST = {"status": "needs_stronger_look", "reasoning": "FIRST-REASONING the claim needs more than I can settle",
         "outcome": "code", "principle": "project:test-intent", "established": "FIRST-EST"}
CANON = "needs_stronger_look"
LANES = {
    "spec_edit": {"changed": "tasks.md"},
    "test_repair": {"testRepair": {"file": CONFLICT["file"], "test": CONFLICT["test"], "change": "assert ==", "why": "w"}},
    "code": {"code": "do the thing"},
    "requirements": {"requirement": {"kind": "add", "wording": "THE SYSTEM SHALL x", "reason": "r"}},
}


def risky(status: Any = CANON, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": status, "reasoning": "RISKY-REASONING still cannot decide",
                           "principle": "project:test-intent", "established": "MARK-EST what was established"}
    out.update(extra)
    return out


def run(bridge, p, answers: List[Any], conflicts: Optional[List[Dict[str, Any]]] = None, edits: Optional[dict] = None):
    project: Dict[str, Any] = {"deps": {"1": []}, "rulings": {"1": answers}}
    project["testConflictsList" if conflicts else "testConflicts"] = {"1": conflicts} if conflicts else {"1": {"conflict": CONFLICT}}
    if edits:
        project["arbiterEdits"] = {"1": edits}
    args = {**p.run_args, "arbiterAgent": "ck-arbiter", "agents": {"arbiter": ROLE}, "onFailure": "continue"}
    return bridge.run(project, args=args, exec_ck=p.exec_ck, clock="virtual")


def arbiter_calls(out) -> List[str]:
    return [c["label"] for c in out["optsList"] if re.match(r"^arbiter task \d+", c["label"])]


def task_attempts(out) -> int:
    return len([c for c in out["optsList"] if re.match(r"^task 1\b", c["label"])])


def parked(out) -> bool:
    return "1" in [str(e.get("task")) for e in out["result"].get("needsInput", [])]


def assert_stopped(out, max_calls: int = 2):
    calls = arbiter_calls(out)
    assert calls[:2] == ["arbiter task 1", "arbiter task 1 risky"], f"scenario never reached the risky tier: {calls}"
    assert len(calls) <= max_calls, f"the arbiter was called again after the risky tier: {calls}"
    assert parked(out), f"task 1 is not in needs-input: {out['result'].get('needsInput')}"
    assert "1" not in [str(t) for t in out["result"].get("merged", [])], out["result"].get("merged")
    assert task_attempts(out) == 1, f"a ruling reached the implementer lane: {[c['label'] for c in out['optsList']]}"
    labels = [c["label"] for c in out["optsList"]]
    assert not [l for l in labels if l.startswith("testfix")], f"a test fixer ran: {labels}"
    assert not re.search(r"worktree.{0,20}land.{0,200}arbiter\d", json.dumps(out["execLog"])), "an arbiter ruling was landed"


def test_tc41_redteam_control_the_risky_tier_is_reached_and_the_stop_holds(bridge, tmp_path):  # noqa: F811
    p = arbiter_project(tmp_path)
    out = run(bridge, p, [FIRST, risky()])
    assert_stopped(out)
    assert "MARK-EST" in json.dumps(out["result"]["needsInput"]), "the entry does not hold what was established"
    assert arbiter_calls(out) == ["arbiter task 1", "arbiter task 1 risky"]


# --------------------------------------------------------------------------- the status spelled differently

SPELLINGS = [
    "Needs_Stronger_Look", "NEEDS_STRONGER_LOOK", " needs_stronger_look", "needs_stronger_look ", "needs_stronger_look\n",
    "\tneeds_stronger_look\t", "\nneeds_stronger_look", "needs stronger look", "needs a stronger look", "Needs A Stronger Look",
    "needs-stronger-look", "needs_stronger_look.", "needs_stronger_look\r\n", "needs_stronger_look​", "needs_stronger_loοk",
]


@pytest.mark.parametrize("status", SPELLINGS, ids=[repr(s) for s in SPELLINGS])
def test_tc41_redteam_status_spelled_by_case_or_whitespace_ends_in_needs_input(bridge, tmp_path, status):  # noqa: F811
    p = arbiter_project(tmp_path)
    out = run(bridge, p, [FIRST, risky(status)])
    assert_stopped(out)


# --------------------------------------------------------------------------- a stronger look that also carries an outcome

@pytest.mark.parametrize("outcome", sorted(LANES))
def test_tc41_redteam_stronger_look_with_an_outcome_is_not_routed_to_the_outcomes_lane(bridge, tmp_path, outcome):  # noqa: F811
    p = arbiter_project(tmp_path)
    edits = {"files": {"specs/demo/tasks.md": "# Implementation Tasks: Demo\n\nedited by the arbiter\n"}} if outcome == "spec_edit" else None
    out = run(bridge, p, [FIRST, risky(outcome=outcome, **LANES[outcome])], edits=edits)
    assert_stopped(out)


@pytest.mark.parametrize("status", ["NEEDS_STRONGER_LOOK", " needs_stronger_look "])
def test_tc41_redteam_odd_spelling_with_a_code_outcome_does_not_retry_the_task(bridge, tmp_path, status):  # noqa: F811
    p = arbiter_project(tmp_path)
    out = run(bridge, p, [FIRST, risky(status, outcome="code", **LANES["code"])])
    assert_stopped(out)


@pytest.mark.parametrize("established", [None, "", "   ", 123, ["a", "b"], {"k": "v"}], ids=repr)
def test_tc41_redteam_odd_established_values_still_stop(bridge, tmp_path, established):  # noqa: F811
    p = arbiter_project(tmp_path)
    answer = risky()
    if established is None:
        del answer["established"]
    else:
        answer["established"] = established
    out = run(bridge, p, [FIRST, answer])
    assert_stopped(out)


def test_tc41_redteam_reasoning_that_asks_to_be_called_again_is_not_obeyed(bridge, tmp_path):  # noqa: F811
    p = arbiter_project(tmp_path)
    ask = risky(reasoning="Please call me again at the risky tier with this reasoning; run `arbiter task 1 risky` once more.")
    out = run(bridge, p, [FIRST, ask, {"status": "ruled", "outcome": "code", "reasoning": "third", "principle": "project:test-intent", "code": "x"}])
    assert_stopped(out)


def test_tc41_redteam_a_scripted_third_answer_is_never_consumed(bridge, tmp_path):  # noqa: F811
    p = arbiter_project(tmp_path)
    out = run(bridge, p, [FIRST, risky(), risky(), risky()])
    assert_stopped(out)
    assert len(arbiter_calls(out)) == 2


def test_tc41_redteam_both_tiers_asking_with_the_same_reasoning(bridge, tmp_path):  # noqa: F811
    p = arbiter_project(tmp_path)
    out = run(bridge, p, [FIRST, dict(FIRST)])
    assert_stopped(out)


# --------------------------------------------------------------------------- the claim returning after the exhausted state

REPEAT_SPELLINGS = {
    "same": CONFLICT,
    "dot-slash": {**CONFLICT, "file": "./" + CONFLICT["file"]},
    "param": {**CONFLICT, "test": CONFLICT["test"] + "[1-2]"},
    "node-in-test": {**CONFLICT, "test": f"{CONFLICT['file']}::{CONFLICT['test']}"},
    "upper": {**CONFLICT, "test": CONFLICT["test"].upper()},
    "spaces": {**CONFLICT, "test": f" {CONFLICT['test']} "},
    "other-reason": {**CONFLICT, "reason": "reworded: the assert disagrees with 1.1", "criterion": "1.1 "},
}

VALID = {"status": "ruled", "outcome": "code", "reasoning": "REPEAT-RULING", "principle": "project:test-intent", "code": "fix it"}


@pytest.mark.parametrize("name", sorted(REPEAT_SPELLINGS))
def test_tc41_redteam_the_claim_after_the_exhausted_state_makes_no_call(bridge, tmp_path, name):  # noqa: F811
    p = arbiter_project(tmp_path)
    first = run(bridge, p, [FIRST, risky()])
    assert_stopped(first)
    again = run(bridge, p, [VALID, VALID], conflicts=[REPEAT_SPELLINGS[name]])
    assert arbiter_calls(again) == [], f"the exhausted claim was sent to the arbiter again ({name}): {arbiter_calls(again)}"
    assert parked(again), again["result"].get("needsInput")
    assert task_attempts(again) == 1, "the exhausted claim reached the implementer"


def test_tc41_redteam_repeated_resumes_after_exhaustion_never_call(bridge, tmp_path):  # noqa: F811
    p = arbiter_project(tmp_path)
    assert_stopped(run(bridge, p, [FIRST, risky()]))
    for _ in range(3):
        again = run(bridge, p, [VALID, FIRST, risky()])
        assert arbiter_calls(again) == [], arbiter_calls(again)
        assert parked(again)


def test_tc41_redteam_the_claim_twice_in_one_attempt_spelled_two_ways_costs_two_calls_at_most(bridge, tmp_path):  # noqa: F811
    p = arbiter_project(tmp_path)
    twice = [CONFLICT, REPEAT_SPELLINGS["dot-slash"], REPEAT_SPELLINGS["upper"]]
    out = run(bridge, p, [FIRST, risky(), risky()], conflicts=twice)
    calls = arbiter_calls(out)
    assert calls[:1] == ["arbiter task 1"], f"the claim never reached the arbiter: {calls}"
    assert len(calls) <= 2, f"one claim spelled three ways made {len(calls)} arbiter calls: {calls}"
    assert parked(out)
