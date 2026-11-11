"""The reading audit before an impl task's first attempt (run-principles Story 13, D16).

TC-120 to TC-126, TC-142, TC-151, TC-161, TC-208. The real `plugin/workflows/spec-implement.js` runs under the fake
runtime through the Node bridge; every `arbiter-` thin step runs the real `ck` CLI on a real git project; the audit
agent is the fake `audit task <n>` label (`auditFindings`), so the event order is the real label sequence.

The plan answers of the fake project carry no `verifiedBy`, so each scenario scripts it through `planSeq` (one entry per
plan call, the last repeated by `plan_seq`): the workflow reads the current plan's `verifiedBy` after the first plan.
Project modules are imported inside the test bodies, so this file collects before the feature exists.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from arbiter_helpers import ArbiterProject, arbiter_project, decisions_entries
from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

REPO = Path(__file__).resolve().parents[1]
GREET = "tests/test_greet.py"
PRINCIPLE = "project:test-intent"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

ROLES = {
    "implementer": {"model": "haiku", "effort": "low", "escalate": ["sonnet", "opus"]},
    "test_writer": {"model": "haiku", "effort": "low"},
    "fixer": {"model": "haiku", "effort": "low", "escalate": ["sonnet", "opus"]},
    "reviewer": {"model": "sonnet", "effort": "high", "enabled": False, "rounds": 1, "cross_test": False},
    "orchestrator": {"model": "sonnet"},
    "resolver": {"model": "sonnet"},
    "arbiter": {"model": "opus", "effort": "high", "risky_model": "opus", "risky_effort": "max"},
}

PROBLEM = {"file": GREET, "test": "test_tc1_greets", "criterion": "1.1",
           "reason": "the test calls a command that only a later task adds", "needs": "Task 3's command"}


# --------------------------------------------------------------------------- scenario helpers


def waves(deps: Dict[str, List[str]], tracks: Dict[str, str]) -> Dict[str, int]:
    """Test-track tasks are wave 1; each impl task sits one wave after the deepest task it depends on."""
    out: Dict[str, int] = {}

    def depth(n: str) -> int:
        if n not in out:
            out[n] = 1 if tracks.get(n) == "test" and not deps[n] else 1 + max([depth(d) for d in deps[n] if d in deps] or [0])
        return out[n]

    for n in deps:
        depth(n)
    return out


def plan_seq(*phases: Dict[str, List[str]], repeat: int = 60) -> List[dict]:
    """`planSeq` entries: the first phase for the first plan, the second phase for every later plan (when given)."""
    first, rest = phases[0], phases[-1]
    return [{"verifiedBy": first}] + [{"verifiedBy": rest} for _ in range(repeat)]


def seed_tests(project: ArbiterProject) -> None:
    """The test file the selected tests resolve to, committed on the integration branch."""
    path = project.target / GREET
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def test_tc1_greets():\n    assert True\n")
    subprocess.run(["git", "add", "-A"], cwd=project.target, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed tests"], cwd=project.target, check=True, capture_output=True)


def run(bridge, project: ArbiterProject, *, deps, tracks, verified_by, phases=None, opts=None, args=None,
        cross_test=False, real_cli=True, auditor="spec:auditor") -> Dict[str, Any]:
    """Run the real workflow on the fake project. `verified_by` is the current plan's `{task: [test tasks]}`."""
    deps = {str(k): [str(d) for d in v] for k, v in deps.items()}
    project_opts = {"deps": deps, **(opts or {})}
    project_opts["planSeq"] = plan_seq(*(phases or [verified_by]))
    wf_args = {
        **project.run_args, "arbiterAgent": "spec:arbiter", "auditorAgent": auditor, "verifiedBy": verified_by,
        "tracks": tracks, "wave": waves(deps, tracks), "maxRetries": 1,
        "agents": {**ROLES, "reviewer": {**ROLES["reviewer"], "cross_test": cross_test}},
        **(args or {}),
    }
    return bridge.run(project_opts, wf_args, exec_ck=project.exec_ck if real_cli else None)


def started(out, n) -> List[str]:
    return [e for e in out["events"] if re.fullmatch(rf"task {n}( .*)?", e) and not e.endswith(" end")]


def audits(out, n=None) -> List[str]:
    pat = r"audit task \d+" if n is None else rf"audit task {n}"
    return [e for e in out["events"] if re.fullmatch(pat, e)]


def arbiters(out, n) -> List[str]:
    return [e for e in out["events"] if re.fullmatch(rf"arbiter task {n}( risky)?", e)]


def first(out, pattern: str) -> int:
    for i, e in enumerate(out["events"]):
        if re.search(pattern, e) and not e.endswith(" end"):
            return i
    return -1


def missed(out) -> List[str]:
    assert "missedAudits" in out["result"], "the result has no `missedAudits`"
    return [str(t) for t in out["result"]["missedAudits"]]


def needs_input(out, n) -> List[dict]:
    return [e for e in out["result"].get("needsInput", []) if str(e.get("task")) == str(n)]


def two(tmp_path, **kw) -> ArbiterProject:
    project = arbiter_project(tmp_path, **kw)
    seed_tests(project)
    return project


DEPS = {"1": [], "2": ["1"]}
TRACKS = {"1": "test", "2": "impl"}
VB = {"2": ["1"]}


def ruling(outcome: str = "code", **extra) -> dict:
    base = {"status": "ruled", "outcome": outcome, "principle": PRINCIPLE, "reasoning": "the principle decides it"}
    if outcome == "code":
        base["code"] = "make the code provide the later command first"
    if outcome == "test_repair":
        base["testRepair"] = {"file": GREET, "test": "test_tc1_greets", "change": "drop the call to the later command",
                              "why": "the test needs behaviour of a later task"}
    return {**base, **extra}


def problem_run(bridge, tmp_path, answers, **kw):
    project = two(tmp_path, **{k: kw.pop(k) for k in ("principles",) if k in kw})
    out = run(bridge, project, deps=DEPS, tracks=TRACKS, verified_by=VB,
              opts={"auditFindings": {"2": {"problems": [PROBLEM], "summary": "one problem"}}, "rulings": {"2": answers}, **kw})
    return project, out


# --------------------------------------------------------------------------- TC-120 / TC-121


def test_tc120_the_audit_runs_after_the_last_verifying_test_task_merged_and_before_the_first_attempt(bridge, tmp_path):
    out = run(bridge, two(tmp_path), deps=DEPS, tracks=TRACKS, verified_by=VB, real_cli=False)
    assert len(audits(out, 2)) == 1, f"no single `audit task 2` label: {out['events']}"
    a = first(out, r"^audit task 2$")
    landed = first(out, r"^land .*\b1\b")
    assert landed >= 0 and landed < a < first(out, r"^task 2( |$)"), out["events"]
    assert GREET in out["prompts"]["audit task 2"], "the audit prompt does not name the selected tests"
    assert "2" in [str(t) for t in out["result"]["merged"]]


def test_tc121_no_problem_starts_the_task_and_no_arbiter_label_runs(bridge, tmp_path):
    out = run(bridge, two(tmp_path), deps=DEPS, tracks=TRACKS, verified_by=VB, real_cli=False)
    assert audits(out, 2), f"the audit never ran: {out['events']}"
    assert started(out, 2), "the task did not start"
    assert not [e for e in out["events"] if e.startswith("arbiter")], out["events"]
    assert missed(out) == []


# --------------------------------------------------------------------------- TC-122


def test_tc122_a_problem_reaches_the_arbiter_before_any_task_label_and_the_ruling_is_recorded(bridge, tmp_path):
    project, out = problem_run(bridge, tmp_path, [ruling("code")])
    assert audits(out, 2), f"the audit never ran: {out['events']}"
    a, r, t = first(out, r"^audit task 2$"), first(out, r"^arbiter task 2( risky)?$"), first(out, r"^task 2( |$)")
    assert 0 <= a < r, out["events"]
    assert t < 0 or r < t, out["events"]
    assert started(out, 2), "a `code` ruling lets the task start"
    assert "make the code provide the later command first" in out["prompts"][out["optsList"][t]["label"]]
    entries = decisions_entries(project, "branch")
    assert [e for e in entries if e.get("Trigger") == "audit"], f"no audit ruling recorded: {entries}"
    assert missed(out) == []


# --------------------------------------------------------------------------- TC-123


def test_tc123_the_shipped_and_generated_auditor_list_exactly_read_grep_glob(tmp_path, monkeypatch):
    from claude_kiro import agents as ag

    shipped = REPO / "plugin" / "agents" / "auditor.md"
    text = shipped.read_text() if shipped.exists() else ""
    assert tools_of(text) == "Read, Grep, Glob", f"the shipped auditor lists tools {tools_of(text)!r}, not exactly Read, Grep, Glob"
    monkeypatch.setenv("CK_CONFIG_HOME", str(tmp_path / "none"))
    generated = tmp_path / "generated"
    generated.mkdir()
    for name, text in ag.agent_type_files(ag.resolve(tmp_path)["roles"]).items():
        (generated / name).write_text(text)
    made = generated / "ck-auditor.md"
    got = tools_of(made.read_text()) if made.exists() else None
    assert got == "Read, Grep, Glob", f"the generated ck-auditor.md lists tools {got!r}, not exactly Read, Grep, Glob"


def tools_of(text: str) -> Optional[str]:
    if text.count("---") < 2:
        return None
    head = text.split("---")[1]
    for line in head.splitlines():
        if line.startswith("tools:"):
            return line.split(":", 1)[1].strip()
    return None


def test_tc123_the_workflow_starts_the_audit_with_the_agent_type_from_the_args(bridge, tmp_path):
    out = run(bridge, two(tmp_path), deps=DEPS, tracks=TRACKS, verified_by=VB, real_cli=False, auditor="my-auditor")
    assert "audit task 2" in out["opts"], f"the audit never ran: {out['events']}"
    assert out["opts"]["audit task 2"].get("agentType") == "my-auditor"


# --------------------------------------------------------------------------- TC-124


def test_tc124_an_audit_that_throws_starts_the_task_and_the_report_notes_it(bridge, tmp_path):
    out = run(bridge, two(tmp_path), deps=DEPS, tracks=TRACKS, verified_by=VB, opts={"auditFindings": {"2": "throw"}})
    assert audits(out, 2), f"the audit never ran: {out['events']}"
    assert started(out, 2), "the task did not start"
    assert missed(out) == ["2"]
    assert not arbiters(out, 2)
    assert not needs_input(out, 2)


# --------------------------------------------------------------------------- TC-125


@pytest.mark.parametrize("fail_until", [1, 3])
def test_tc125_a_retried_and_an_escalated_task_has_one_audit(bridge, tmp_path, fail_until):
    out = run(bridge, two(tmp_path), deps=DEPS, tracks=TRACKS, verified_by=VB, opts={"failUntil": {"2": fail_until}}, real_cli=False)
    assert len(audits(out, 2)) == 1, f"expected one audit: {out['events']}"
    assert len(started(out, 2)) > 1, f"the task was not retried: {out['events']}"
    assert first(out, r"^audit task 2$") < first(out, r"^task 2( |$)")


# --------------------------------------------------------------------------- TC-126


def test_tc126_no_verifying_tests_means_no_audit(bridge, tmp_path):
    out = run(bridge, two(tmp_path), deps=DEPS, tracks=TRACKS, verified_by={}, real_cli=False)
    assert started(out, 2) and not audits(out)
    # the same run with the verifying test reads one audit, so the absence above is not a dead audit step
    again = run(bridge, two(tmp_path / "again"), deps=DEPS, tracks=TRACKS, verified_by=VB, real_cli=False)
    assert len(audits(again, 2)) == 1, again["events"]


def test_tc126_a_test_track_task_is_not_audited(bridge, tmp_path):
    deps, tracks = {"1": [], "2": ["1"]}, {"1": "test", "2": "test"}
    out = run(bridge, two(tmp_path), deps=deps, tracks=tracks, verified_by={"2": ["1"]}, real_cli=False,
              opts={"crossTest": {"verdict": "approve", "findings": [], "summary": "ok"}})
    assert started(out, 2) and not audits(out)
    control = run(bridge, two(tmp_path / "control"), deps=deps, tracks={"1": "test", "2": "impl"}, verified_by={"2": ["1"]}, real_cli=False)
    assert len(audits(control, 2)) == 1, control["events"]


CROSS_DEPS = {"1": [], "2": [], "3": ["1", "2"]}
CROSS_TRACKS = {"1": "test", "2": "test", "3": "impl"}
TEST_A = "tests/test_a.py"


def cross_run(bridge, tmp_path, finding):
    crossed = {"verdict": "approve", "findings": [], "summary": "ok"} if finding is None else \
        {"verdict": "changes", "findings": [{"severity": "blocking", "file": TEST_A, "issue": "reads a name the design does not define"}], "summary": "drift"}
    return run(bridge, two(tmp_path), deps=CROSS_DEPS, tracks=CROSS_TRACKS, verified_by={"3": ["1", "2"]}, cross_test=True,
               opts={"crossTest": crossed, "testVerifies": {TEST_A: ["3"]}, "testFix": {TEST_A: {}}})


def test_tc126_a_cross_test_check_that_read_the_verifying_tests_without_a_blocking_finding_skips_the_audit(bridge, tmp_path):
    out = cross_run(bridge, tmp_path, None)
    assert [e for e in out["events"] if e.startswith("cross-test")], "the cross-test check never ran"
    assert started(out, 3) and not audits(out, 3), out["events"]


def test_tc126_a_blocking_finding_on_one_of_those_test_files_leaves_one_audit(bridge, tmp_path):
    out = cross_run(bridge, tmp_path, TEST_A)
    assert [e for e in out["events"] if e.startswith("cross-test")], "the cross-test check never ran"
    assert len(audits(out, 3)) == 1, out["events"]
    assert first(out, r"^audit task 3$") < first(out, r"^task 3( |$)")


# --------------------------------------------------------------------------- TC-142


@pytest.mark.parametrize("answer", ["throw", None, {}], ids=["throws", "null", "empty"])
def test_tc142_an_arbiter_call_that_fails_or_returns_nothing_usable_starts_the_task(bridge, tmp_path, answer):
    _, out = problem_run(bridge, tmp_path, [answer])
    assert audits(out, 2) and arbiters(out, 2), f"the audit problem never reached the arbiter: {out['events']}"
    assert started(out, 2), "the task did not start"
    assert missed(out) == ["2"]
    assert not needs_input(out, 2)


def test_tc142_a_missing_principles_file_starts_the_task_without_calling_the_arbiter(bridge, tmp_path):
    _, out = problem_run(bridge, tmp_path, [ruling("code")], principles=None)
    assert audits(out, 2), f"the audit never ran: {out['events']}"
    assert not arbiters(out, 2), "the arbiter ran without a principles file"
    assert started(out, 2)
    assert missed(out) == ["2"]
    assert not needs_input(out, 2)


# --------------------------------------------------------------------------- TC-161


@pytest.mark.parametrize("bad", [
    {"status": "ruled", "outcome": "code", "reasoning": "r", "code": "c"},
    {"status": "ruled", "outcome": "code", "principle": "", "reasoning": "r", "code": "c"},
    {"status": "ruled", "outcome": "revert", "principle": PRINCIPLE, "reasoning": "r"},
    {"status": "ruled", "outcome": "SPEC_EDIT", "principle": PRINCIPLE, "reasoning": "r", "changed": "x"},
], ids=["no-principle", "empty-principle", "unknown-outcome", "wrong-case-outcome"])
def test_tc161_a_malformed_ruling_on_an_audit_problem_is_needs_input_and_the_task_does_not_start(bridge, tmp_path, bad):
    project, out = problem_run(bridge, tmp_path, [bad])
    assert audits(out, 2) and arbiters(out, 2), f"the audit problem never reached the arbiter: {out['events']}"
    assert not started(out, 2), "the task started on a malformed ruling"
    entries = needs_input(out, 2)
    assert entries, f"no needs-input entry: {out['result'].get('needsInput')}"
    assert bad["reasoning"] in json.dumps(entries) and bad["outcome"] in json.dumps(entries), "the entry lacks the arbiter's output"
    assert "2" not in missed(out)
    assert decisions_entries(project, "branch") == [], "a malformed ruling was recorded"


# --------------------------------------------------------------------------- TC-208


def test_tc208_a_test_repair_ruling_goes_to_the_fixer_then_the_first_attempt_starts_with_no_retry_spent(bridge, tmp_path):
    _, out = problem_run(bridge, tmp_path, [ruling("test_repair")], testFix={GREET: {}})
    assert audits(out, 2) and arbiters(out, 2), f"the audit problem never reached the arbiter: {out['events']}"
    r, fix, land, t = (first(out, p) for p in (r"^arbiter task 2", r"^testfix \d+ \(Task 2\)", r"^land .*testfix", r"^task 2( |$)"))
    assert 0 <= r < fix < land < t, out["events"]
    assert len(started(out, 2)) == 1, f"a retry was spent: {started(out, 2)}"
    task = [x for x in out["result"]["tasks"] if str(x["task"]) == "2"][0]
    assert task["attempts"] == 1 and "2" in [str(m) for m in out["result"]["merged"]], task
    assert "2" not in missed(out)


def test_tc208_a_fix_the_fixer_does_not_land_is_a_test_fix_entry_and_no_task_label(bridge, tmp_path):
    finding = {"file": GREET, "test": "test_tc1_greets", "issue": "the test is right"}
    _, out = problem_run(bridge, tmp_path, [ruling("test_repair")],
                         testFix={GREET: {"verdict": "cannot-fix", "finding": finding}})
    assert audits(out, 2) and arbiters(out, 2), f"the audit problem never reached the arbiter: {out['events']}"
    assert not started(out, 2), "the task started although the fix did not land"
    assert [e for e in needs_input(out, 2) if e.get("kind") == "test-fix"], out["result"].get("needsInput")


# --------------------------------------------------------------------------- TC-151

SPEC_BASE = """# Implementation Tasks: Demo

**Status:** Not Started

## Task Breakdown

### Task 1: Greeting tests
**Status:** Not Started
**Track:** test
**Requirements:** 1.1
**Description:** Test the greeting.
**Files:**
- `tests/test_greet.py` - the test
**Verify:** `python -c pass`
**Verifies:** {verifies}
**Dependencies:** None

---

### Task 2: Greeting
**Status:** Not Started
**Track:** impl
**Requirements:** 1.1
**Description:** Print the greeting.
**Files:**
- `src/greet.py` - the greeting
**Verify:** `python -c pass`
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
**Verify:** `python -c pass`
{verified_by}**Dependencies:** Task 2

---
"""
ONE = "Task 2 (its tests are the oracle)"
BOTH = "Task 2 (its tests are the oracle), Task 3 (its tests are the oracle)"


def spec(task3_verified: bool) -> str:
    return SPEC_BASE.format(verifies=BOTH if task3_verified else ONE,
                            verified_by="**Verified by:** Task 1 (tests/test_greet.py)\n" if task3_verified else "")


CONFLICT = {"file": GREET, "test": "test_tc1_greets", "criterion": "1.1", "reason": "asserts a value no correct code returns", "fix": "x"}


def edit_run(bridge, tmp_path, before: bool):
    """Task 2 reports a conflict; the arbiter's `spec_edit` rewires Task 3's Verified by; Task 3 starts after it landed."""
    project = two(tmp_path, tasks=spec(before))
    wired = {"2": ["1"], "3": ["1"]}
    unwired = {"2": ["1"]}
    out = run(bridge, project, deps={"1": [], "2": ["1"], "3": ["2"]}, tracks={"1": "test", "2": "impl", "3": "impl"},
              verified_by=wired if before else unwired, phases=[wired if before else unwired, unwired if before else wired],
              opts={"testConflicts": {"2": {"conflict": CONFLICT, "attempts": 1}},
                    "rulings": {"2": [ruling("spec_edit", changed="Task 3's Verified by is " + ("removed" if before else "added"))]},
                    "arbiterEdits": {"2": {"files": {"specs/demo/tasks.md": spec(not before)}}}})
    return project, out


def test_tc151_a_landed_spec_edit_that_gives_a_task_its_first_verifying_test_makes_it_audited(bridge, tmp_path):
    project, out = edit_run(bridge, tmp_path, before=False)
    assert [e for e in out["events"] if e.startswith("arbiter task 2")], f"no ruling on Task 2's conflict: {out['events']}"
    assert audits(out, 2), "Task 2 is verified from the start and is audited"
    assert len(audits(out, 3)) == 1, f"Task 3 was not audited after the edit gave it a verifying test: {out['events']}"
    assert first(out, r"^audit task 3$") < first(out, r"^task 3( |$)")


def test_tc151_a_landed_spec_edit_that_removes_its_last_verifying_test_means_no_audit(bridge, tmp_path):
    project, out = edit_run(bridge, tmp_path, before=True)
    assert [e for e in out["events"] if e.startswith("arbiter task 2")], f"no ruling on Task 2's conflict: {out['events']}"
    assert audits(out, 2), "Task 2 is verified throughout and must be audited"
    assert started(out, 3), f"Task 3 never started: {out['events']}"
    assert not audits(out, 3), f"Task 3 was audited after the edit removed its last verifying test: {out['events']}"
