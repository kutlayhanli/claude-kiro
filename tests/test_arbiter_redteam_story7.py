"""Red-team for the SHALL NOT clauses of Story 7 (run-principles 7.2, 7.4, 7.6), in a run of the real workflow.

Written from requirements.md only. Every case runs `plugin/workflows/spec-implement.js` under the fake runtime
(`tests/js`), with the real `ck` CLI behind the `arbiter-` steps, a real git repo and a real `decisions.md`.

7.2: a ruling that cites no valid principle, names an unknown or several outcomes, or is malformed SHALL NOT be
     applied or recorded, and the claim goes to me as a needs-input entry with the arbiter's output.
7.4: a claim that returns after its ruling was applied SHALL NOT be ruled again; it goes to me with the ruling and the
     new evidence. Cases: the same claim under a reformatted test id (parameters, `./` prefix, whitespace).
7.6: a claim that would be the 4th ruling on a task SHALL NOT call the arbiter; the entry lists the 3 earlier rulings.
     Cases: a 4th claim in the same attempt, in a resumed run, and on parallel tasks each near the limit.

Each group has positive controls (a valid ruling is applied and recorded; a different claim gets its own ruling), so a
workflow that rejects everything, or never calls the arbiter, cannot satisfy both the controls and the negatives.
Not covered: a case-changed test id. Test ids are case-sensitive in pytest, so whether a case variant is "the same
claim" is the identity rule the design defines (7.3), not something 7.4 states.
"""

import json
import shutil

import pytest

from arbiter_helpers import arbiter_project, decisions_entries
from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

pytestmark = [pytest.mark.usefixtures("no_network"), pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")]

TASKS = """# Implementation Tasks: Demo

**Status:** Not Started

## Task Breakdown

### Task 1: Greeting tests
**Status:** Done
**Track:** test
**Requirements:** 1.1
**Description:** Test the greeting.
**Files:**
- `tests/test_greet.py` - the test
**Verify:** `uv run pytest -q tests/test_greet.py`
**Verifies:** Task 2, Task 3
**Dependencies:** None

---

### Task 2: Greeting
**Status:** Not Started
**Track:** impl
**Requirements:** 1.1
**Description:** Print the greeting.
**Files:**
- `src/greet.py` - the greeting
**Verify:** `uv run pytest -q tests/test_greet.py`
**Verified by:** Task 1 (tests/test_greet.py)
**Dependencies:** Task 1

---

### Task 3: Farewell
**Status:** Not Started
**Track:** impl
**Requirements:** 1.1
**Description:** Print the farewell.
**Files:**
- `src/bye.py` - the farewell
**Verify:** `uv run pytest -q tests/test_greet.py`
**Verified by:** Task 1 (tests/test_greet.py)
**Dependencies:** Task 1

---
"""

AGENTS = {
    "implementer": {"model": "sonnet", "effort": "medium"},
    "test_writer": {"model": "opus", "effort": "high"},
    "reviewer": {"model": "sonnet", "effort": "medium", "enabled": False, "rounds": 1},
    "fixer": {"model": "sonnet", "effort": "medium"},
    "orchestrator": {"model": "sonnet", "effort": "low"},
    "resolver": {"model": "opus", "effort": "high"},
    "arbiter": {"model": "opus", "effort": "high", "risky_model": "opus", "risky_effort": "max"},
}
PRINCIPLE = "project:test-intent"
FILE = "tests/test_greet.py"


def key(task: str, test: str, file: str = FILE) -> str:
    return f"{task}|test:{file}::{test}"


def conflict(test: str, file: str = FILE, why: str = "") -> dict:
    return {"file": file, "test": test, "criterion": "1.1", "reason": why or f"{test} can never pass: the tolerance is tighter than the input",
            "fix": f"{test}: loosen the tolerance"}


def ruling(task: str, test: str, mark: str, **over) -> dict:
    """A valid `code` ruling on a test-conflict claim; `over` replaces fields, a None value removes one."""
    out = {"claim": key(task, test), "status": "ruled", "outcome": "code", "reasoning": f"the code must change {mark}",
           "principle": PRINCIPLE, "code": "round before comparing"}
    for k, v in over.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


def run(bridge, project, *, deps, conflicts, rulings, attempts=1, args=None):  # noqa: F811
    """Run the workflow once on `project`. `conflicts` and `rulings` are {task: [...]}; the conflicts are reported on
    the task's first `attempts` attempts, then the task passes. Returns the bridge's answer plus helpers' inputs."""
    options = {
        "deps": deps,
        "done": ["1"],
        "testConflictsList": {n: {"conflicts": c, "attempts": attempts} for n, c in conflicts.items()},
        "rulings": rulings,
    }
    run_args = {**project.run_args, "agents": AGENTS, "arbiterAgent": "spec:arbiter", "auditorAgent": "spec:auditor",
                "tracks": {"1": "test"}, "wave": {n: 1 for n in deps}, "maxRetries": 1, **(args or {})}
    return bridge.run(options, args=run_args, exec_ck=project.exec_ck)


def labels(out) -> list:
    return [e["label"] for e in out["trace"] if e.get("ev") == "request"]


def arbiter_calls(out, task: str) -> list:
    return [lab for lab in labels(out) if lab.startswith(f"arbiter task {task}")]


def entries(out, task: str) -> list:
    return [e for e in (out["result"].get("needsInput") or []) if str(e.get("task")) == task]


def text(entry) -> str:
    return json.dumps(entry, sort_keys=True)


def make(tmp_path):
    return arbiter_project(tmp_path, tasks=TASKS)


def record_steps(out) -> list:
    return [lab for lab in labels(out) if lab.startswith("arbiter-record")]


def land_steps(out) -> list:
    return [lab for lab in labels(out) if lab.startswith("arbiter-land") or lab.startswith("land arbiter")]


# --------------------------------------------------------------------------- 7.2: invalid rulings are neither applied nor recorded

MARK = "MARK-7-2-ARBITER-OUTPUT"
SECOND_OUTCOME = {"spec_edit", "test_repair", "requirements"}


def invalid_forms():
    r = lambda **over: ruling("2", "test_a", MARK, **over)  # noqa: E731
    base = r()
    earlier = {"ruling": 1, "claim": key("2", "test_a"), "outcome": "code", "reasoning": f"earlier {MARK}"}
    forms = {
        # principle: absent, quoted only in the reasoning, near-misses of a real id, text in place of the id
        "no-principle": r(principle=None),
        "empty-principle": r(principle=""),
        "principle-only-in-reasoning": r(principle=None, reasoning=f"I uphold {PRINCIPLE} {MARK}"),
        "principle-quoted-in-reasoning-wrong-field": r(principle="see reasoning", reasoning=f"`{PRINCIPLE}` applies {MARK}"),
        "trailing-space": r(principle=PRINCIPLE + " "),
        "leading-space": r(principle=" " + PRINCIPLE),
        "trailing-newline": r(principle=PRINCIPLE + "\n"),
        "trailing-nul": r(principle=PRINCIPLE + "\x00"),
        "case-variant": r(principle="Project:Test-Intent"),
        "upper-case": r(principle=PRINCIPLE.upper()),
        "truncated": r(principle="project:test-inten"),
        "no-layer": r(principle="test-intent"),
        "other-layer-same-slug": r(principle="plan:test-intent"),
        "decision-id": r(principle="D9"),
        "other-specs-plan-principle": r(principle="plan:made-up-in-another-spec"),
        "principle-text-not-id": r(principle="a test states what the requirement says; repair the test, never bend the requirement to it."),
        "two-ids-in-one": r(principle=f"{PRINCIPLE}, project:remove-first"),
        "principle-as-list": r(principle=[PRINCIPLE]),
        "principle-as-number": r(principle=7),
        # outcome: unknown, several, wrong form, wrong type
        "unknown-outcome": r(outcome="revert"),
        "case-variant-outcome": r(outcome="CODE"),
        "padded-outcome": r(outcome=" code"),
        "empty-outcome": r(outcome=""),
        "no-outcome": r(outcome=None),
        "two-outcomes-list": r(outcome=["code", "spec_edit"]),
        "two-outcomes-piped": r(outcome="code|spec_edit"),
        "two-outcomes-comma": r(outcome="code, test_repair"),
        "two-outcomes-by-fields": {**base, "changed": "tasks.md: Verify narrowed", "testRepair": {"file": FILE, "test": "test_a", "change": "x", "why": "y"}, "outcome": ["code", "test_repair"]},
        "outcome-as-number": r(outcome=3),
        "outcome-as-object": r(outcome={"code": True}),
        # the other required parts
        "no-claim": r(claim=None),
        "no-reasoning": r(reasoning=None),
        "reasoning-not-text": r(reasoning=["a", MARK]),
        "no-status": r(status=None),
        "unknown-status": r(status="maybe"),
        # the system's own output fed back as the answer
        "earlier-ruling-as-answer": earlier,
        "earlier-ruling-with-number-and-no-principle": {**earlier, "status": "ruled"},
        "ruling-record-fed-back": {"trigger": "test-conflict", "claim_key": key("2", "test_a"), "claim_text": "c", "task": "2", "tier": "normal",
                                   "outcome": "code", "reasoning": MARK, "principle_id": PRINCIPLE, "principle_text": "t", "plan_layer": "absent",
                                   "changed": "nothing", "moved": [], "commit": "none", "landed": None},
        "decisions-entry-text-as-answer": (f"## Ruling 1: Task 2 (test-conflict)\n\n- **Claim:** `{key('2', 'test_a')}` - c\n- **Ruling:** code - {MARK}\n"
                                           f"- **Principle cited:** {PRINCIPLE} - t\n"),
        "valid-ruling-as-json-text": json.dumps(base),
        "valid-ruling-wrapped": {"rulings": [base], "reasoning": MARK},
        "valid-ruling-in-array": [base, MARK],
        "empty-string": "",
        "plain-prose": f"I think the code should change. {MARK}",
        "empty-object": {"note": MARK},
    }
    return [pytest.param(v, id=k) for k, v in forms.items()]


@pytest.mark.parametrize("raw", invalid_forms())
def test_an_invalid_ruling_is_not_applied_not_recorded_and_goes_to_me_with_the_output(bridge, tmp_path, raw):  # noqa: F811
    project = make(tmp_path)
    out = run(bridge, project, deps={"2": []}, conflicts={"2": [conflict("test_a")]}, rulings={"2": [raw]}, attempts=9)
    assert len(arbiter_calls(out, "2")) >= 1, "the arbiter must have been asked, or this proves nothing: " + "\n".join(labels(out))
    assert decisions_entries(project) == [], "an invalid ruling was recorded"
    assert not (project.target / "specs" / "demo" / "decisions.md").exists()
    assert decisions_entries(project, "branch") == []
    assert record_steps(out) == [], labels(out)
    assert land_steps(out) == [], "an invalid ruling reached a landing"
    assert not [lab for lab in labels(out) if lab.startswith("testfix")], "an invalid test-repair ruling reached the fixer"
    assert (out["result"].get("rulings") or []) == [], "an invalid ruling is in this run's rulings"
    got = entries(out, "2")
    assert len(got) == 1, json.dumps(out["result"].get("needsInput"))
    body = text(got[0])
    marker = MARK if MARK in json.dumps(raw) else (raw if isinstance(raw, str) else None)
    if marker:
        assert marker in body, "the entry does not carry the arbiter's output: " + body
    assert not [lab for lab in labels(out) if lab.startswith("arbiter task 2")][1:], "a malformed ruling must not be re-asked at the same tier"


def test_positive_control_a_valid_ruling_is_applied_and_recorded(bridge, tmp_path):  # noqa: F811
    project = make(tmp_path)
    out = run(bridge, project, deps={"2": []}, conflicts={"2": [conflict("test_a")]}, rulings={"2": [ruling("2", "test_a", MARK)]})
    assert len(arbiter_calls(out, "2")) == 1
    entries_ = decisions_entries(project)
    assert len(entries_) == 1 and entries_[0]["number"] == 1, entries_
    assert entries(out, "2") == [], json.dumps(out["result"].get("needsInput"))
    assert len(out["result"].get("rulings") or []) == 1


def test_an_invalid_ruling_does_not_use_up_the_limit_it_was_never_a_ruling(bridge, tmp_path):  # noqa: F811
    """Three invalid answers in a row (three different claims) are no rulings: a fourth, valid claim still gets one."""
    project = make(tmp_path)
    tests = ["test_a", "test_b", "test_c", "test_d"]
    bad = [ruling("2", t, MARK, principle=PRINCIPLE + " ") for t in tests[:3]]
    good = ruling("2", "test_d", "GOOD")
    out = run(bridge, project, deps={"2": []}, conflicts={"2": [conflict(t) for t in tests]}, rulings={"2": [*bad, good]}, attempts=9)
    assert len(arbiter_calls(out, "2")) >= 1
    assert [e["number"] for e in decisions_entries(project)] in ([], [1]), "invalid answers must never be recorded"
    for e in decisions_entries(project):
        assert "GOOD" in json.dumps(e) or e.get("Claim", "").find("test_d") >= 0


# --------------------------------------------------------------------------- 7.4: a returning claim is not ruled again

RETURNING = [
    pytest.param(lambda c: {**c, "test": c["test"] + "[case-1]"}, id="parameter-suffix"),
    pytest.param(lambda c: {**c, "test": c["test"] + "[a-b-c]"}, id="parameters-with-dashes"),
    pytest.param(lambda c: {**c, "file": "./" + c["file"]}, id="dot-slash-prefix"),
    pytest.param(lambda c: {**c, "file": c["file"].replace("/", "//", 1)}, id="double-slash"),
    pytest.param(lambda c: {**c, "test": " " + c["test"]}, id="leading-space"),
    pytest.param(lambda c: {**c, "test": c["test"] + " "}, id="trailing-space"),
    pytest.param(lambda c: {**c, "test": c["test"] + "\n"}, id="trailing-newline"),
    pytest.param(lambda c: {**c, "file": " " + c["file"] + " "}, id="padded-file"),
    pytest.param(lambda c: {**c, "file": "./" + c["file"], "test": c["test"] + "[x]"}, id="prefix-and-parameters"),
    pytest.param(lambda c: {**c, "reason": "a different wording of the same problem, and a longer one", "fix": "something else"}, id="reworded-evidence"),
]


@pytest.mark.parametrize("reformat", RETURNING)
def test_a_claim_returning_in_another_surface_form_is_not_ruled_again(bridge, tmp_path, reformat):  # noqa: F811
    project = make(tmp_path)
    first = conflict("test_a")
    one = run(bridge, project, deps={"2": []}, conflicts={"2": [first]}, rulings={"2": [ruling("2", "test_a", "FIRST-RULING")]})
    assert len(arbiter_calls(one, "2")) == 1 and len(decisions_entries(project)) == 1, labels(one)
    again = reformat(first)
    canary = ruling("2", "test_a", "SECOND-RULING-MUST-NOT-HAPPEN")
    two = run(bridge, project, deps={"2": []}, conflicts={"2": [again]}, rulings={"2": [canary]}, attempts=9)
    assert arbiter_calls(two, "2") == [], "the arbiter ruled a returning claim again: " + "\n".join(labels(two))
    assert len(decisions_entries(project)) == 1, decisions_entries(project)
    got = entries(two, "2")
    assert len(got) == 1, json.dumps(two["result"].get("needsInput"))
    body = text(got[0])
    assert "FIRST-RULING" in body or '"ruling": 1' in body, "the entry does not carry the ruling: " + body
    assert str(again["reason"]) in body or json.dumps(again["reason"])[1:-1] in body, "the entry does not carry the new evidence: " + body
    assert "SECOND-RULING-MUST-NOT-HAPPEN" not in body


def test_positive_control_a_different_test_on_the_same_task_gets_its_own_ruling(bridge, tmp_path):  # noqa: F811
    project = make(tmp_path)
    run(bridge, project, deps={"2": []}, conflicts={"2": [conflict("test_a")]}, rulings={"2": [ruling("2", "test_a", "FIRST")]})
    two = run(bridge, project, deps={"2": []}, conflicts={"2": [conflict("test_b")]}, rulings={"2": [ruling("2", "test_b", "SECOND")]})
    assert len(arbiter_calls(two, "2")) == 1, labels(two)
    assert [e["number"] for e in decisions_entries(project)] == [1, 2]
    assert entries(two, "2") == []


@pytest.mark.parametrize("near", [
    pytest.param(lambda c: {**c, "test": "test_a_extra"}, id="longer-name"),
    pytest.param(lambda c: {**c, "test": "test_"}, id="prefix-of-the-name"),
    pytest.param(lambda c: {**c, "file": "tests/test_greet_two.py"}, id="other-file"),
    pytest.param(lambda c: {**c, "file": "tests/sub/test_greet.py"}, id="same-basename-other-dir"),
])
def test_positive_control_a_near_miss_is_a_different_claim_and_is_ruled(bridge, tmp_path, near):  # noqa: F811
    project = make(tmp_path)
    first = conflict("test_a")
    run(bridge, project, deps={"2": []}, conflicts={"2": [first]}, rulings={"2": [ruling("2", "test_a", "FIRST")]})
    other = near(first)
    claim = {"claim": key("2", other["test"], other["file"]), "status": "ruled", "outcome": "code", "reasoning": "SECOND", "principle": PRINCIPLE, "code": "x"}
    two = run(bridge, project, deps={"2": []}, conflicts={"2": [other]}, rulings={"2": [claim]})
    assert len(arbiter_calls(two, "2")) == 1, "a different claim was treated as the returning one: " + "\n".join(labels(two))


def test_a_returning_claim_after_the_ruling_of_another_task_is_still_ruled(bridge, tmp_path):  # noqa: F811
    """The same test named by another task is another claim (the identity includes the task)."""
    project = make(tmp_path)
    run(bridge, project, deps={"2": []}, conflicts={"2": [conflict("test_a")]}, rulings={"2": [ruling("2", "test_a", "TASK-TWO")]})
    two = run(bridge, project, deps={"3": []}, conflicts={"3": [conflict("test_a")]}, rulings={"3": [ruling("3", "test_a", "TASK-THREE")]})
    assert len(arbiter_calls(two, "3")) == 1, labels(two)


# --------------------------------------------------------------------------- 7.6: the 4th claim on a task is not ruled

TESTS = ["test_a", "test_b", "test_c", "test_d", "test_e", "test_f"]


def scripted(task: str, tests, tag: str):
    return [ruling(task, t, f"{tag}-{i + 1}") for i, t in enumerate(tests)]


def assert_lists_three(entry, tag: str, others=()):
    body = text(entry)
    for i in (1, 2, 3):
        assert f"{tag}-{i}" in body, f"the entry does not list earlier ruling {i}: {body}"
    for other in others:
        assert other not in body, f"the entry lists a ruling of another task ({other}): {body}"


def test_a_fourth_claim_in_one_attempt_gets_no_call_and_lists_the_three(bridge, tmp_path):  # noqa: F811
    project = make(tmp_path)
    out = run(bridge, project, deps={"2": []}, conflicts={"2": [conflict(t) for t in TESTS[:5]]}, rulings={"2": scripted("2", TESTS[:5], "R")}, attempts=9)
    assert len(arbiter_calls(out, "2")) == 3, "\n".join(labels(out))
    assert [e["number"] for e in decisions_entries(project)] == [1, 2, 3]
    got = entries(out, "2")
    assert got, json.dumps(out["result"].get("needsInput"))
    assert_lists_three(got[0], "R")
    assert "R-4" not in text(got[0]) and "R-5" not in text(got[0])


def test_positive_control_the_third_claim_still_gets_its_ruling(bridge, tmp_path):  # noqa: F811
    project = make(tmp_path)
    out = run(bridge, project, deps={"2": []}, conflicts={"2": [conflict(t) for t in TESTS[:3]]}, rulings={"2": scripted("2", TESTS[:3], "R")})
    assert len(arbiter_calls(out, "2")) == 3
    assert [e["number"] for e in decisions_entries(project)] == [1, 2, 3]
    assert entries(out, "2") == []


def test_a_fourth_claim_arriving_by_a_resumed_run_gets_no_call_and_lists_the_three(bridge, tmp_path):  # noqa: F811
    project = make(tmp_path)
    for i, t in enumerate(TESTS[:3]):
        done = run(bridge, project, deps={"2": []}, conflicts={"2": [conflict(t)]}, rulings={"2": [ruling("2", t, f"RUN-{i + 1}")]})
        assert len(arbiter_calls(done, "2")) == 1, labels(done)
    assert [e["number"] for e in decisions_entries(project)] == [1, 2, 3]
    out = run(bridge, project, deps={"2": []}, conflicts={"2": [conflict("test_d")]}, rulings={"2": [ruling("2", "test_d", "MUST-NOT-RUN")]}, attempts=9)
    assert arbiter_calls(out, "2") == [], "a resumed run called the arbiter for a 4th claim: " + "\n".join(labels(out))
    assert [e["number"] for e in decisions_entries(project)] == [1, 2, 3]
    got = entries(out, "2")
    assert len(got) == 1, json.dumps(out["result"].get("needsInput"))
    body = text(got[0])
    for i in (1, 2, 3):
        assert f"RUN-{i}" in body, body
    assert "MUST-NOT-RUN" not in body


def test_a_resumed_run_with_two_rulings_on_disk_gives_one_more_and_refuses_the_next(bridge, tmp_path):  # noqa: F811
    project = make(tmp_path)
    for i, t in enumerate(TESTS[:2]):
        run(bridge, project, deps={"2": []}, conflicts={"2": [conflict(t)]}, rulings={"2": [ruling("2", t, f"RUN-{i + 1}")]})
    out = run(bridge, project, deps={"2": []}, conflicts={"2": [conflict(t) for t in TESTS[2:5]]}, rulings={"2": scripted("2", TESTS[2:5], "NEW")}, attempts=9)
    assert len(arbiter_calls(out, "2")) == 1, "the limit is 3 rulings per task across runs: " + "\n".join(labels(out))
    assert [e["number"] for e in decisions_entries(project)] == [1, 2, 3]
    got = entries(out, "2")
    assert got and all(f"RUN-{i}" in text(got[0]) for i in (1, 2)) and "NEW-1" in text(got[0]), json.dumps(out["result"].get("needsInput"))


def test_parallel_tasks_each_near_the_limit_get_three_rulings_and_list_only_their_own(bridge, tmp_path):  # noqa: F811
    project = make(tmp_path)
    conflicts = {"2": [conflict(t) for t in TESTS[:5]], "3": [conflict(t) for t in TESTS[:5]]}
    rulings = {"2": scripted("2", TESTS[:5], "TWO"), "3": scripted("3", TESTS[:5], "THREE")}
    out = run(bridge, project, deps={"2": [], "3": []}, conflicts=conflicts, rulings=rulings, attempts=9, args={"agentSlots": 2})
    assert len(arbiter_calls(out, "2")) == 3 and len(arbiter_calls(out, "3")) == 3, "\n".join(labels(out))
    numbers = [e["number"] for e in decisions_entries(project)]
    assert numbers == [1, 2, 3, 4, 5, 6], "concurrent records must be numbered without gaps or repeats: " + str(numbers)
    for task, tag, other in (("2", "TWO", "THREE"), ("3", "THREE", "TWO")):
        got = entries(out, task)
        assert got, json.dumps(out["result"].get("needsInput"))
        assert_lists_three(got[0], tag, others=[f"{other}-"])


def test_parallel_tasks_one_at_the_limit_does_not_use_up_the_other(bridge, tmp_path):  # noqa: F811
    """Task 2 already has 3 rulings; task 3's first claim in the same run is still ruled (the limit is per task)."""
    project = make(tmp_path)
    for i, t in enumerate(TESTS[:3]):
        run(bridge, project, deps={"2": []}, conflicts={"2": [conflict(t)]}, rulings={"2": [ruling("2", t, f"RUN-{i + 1}")]})
    out = run(bridge, project, deps={"2": [], "3": []},
              conflicts={"2": [conflict("test_d")], "3": [conflict("test_a")]},
              rulings={"2": [ruling("2", "test_d", "NO")], "3": [ruling("3", "test_a", "YES")]}, attempts=9, args={"agentSlots": 2})
    assert arbiter_calls(out, "2") == [], labels(out)
    assert len(arbiter_calls(out, "3")) == 1, labels(out)
    assert entries(out, "2"), json.dumps(out["result"].get("needsInput"))


def test_a_returning_claim_is_not_counted_as_a_fourth_ruling_nor_given_one(bridge, tmp_path):  # noqa: F811
    """With 3 rulings, a returning claim (7.4) and a new claim (7.6) both go to me and neither calls the arbiter."""
    project = make(tmp_path)
    for i, t in enumerate(TESTS[:3]):
        run(bridge, project, deps={"2": []}, conflicts={"2": [conflict(t)]}, rulings={"2": [ruling("2", t, f"RUN-{i + 1}")]})
    out = run(bridge, project, deps={"2": []}, conflicts={"2": [conflict("test_a", file="./" + FILE), conflict("test_d")]},
              rulings={"2": [ruling("2", "test_d", "MUST-NOT-RUN")]}, attempts=9)
    assert arbiter_calls(out, "2") == [], labels(out)
    assert [e["number"] for e in decisions_entries(project)] == [1, 2, 3]
    assert entries(out, "2"), json.dumps(out["result"].get("needsInput"))
