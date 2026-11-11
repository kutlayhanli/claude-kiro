"""Red-team for the SHALL NOT clauses of Story 13 (run-principles 13.6 and 13.7), through the real workflow script.

13.6: WHEN a task is retried or escalated, THE SYSTEM SHALL NOT run the audit again for that task in that run.
13.7: WHEN an impl task has no verifying tests, or a task is on the test track, THE SYSTEM SHALL NOT run the audit for it.

Every case runs the real `plugin/workflows/spec-implement.js` under the fake runtime (the Node bridge). The audit is
counted from the agent calls the workflow makes: an agent started with the auditor's `agentType`, or any call whose
label starts `audit task <n>`. The positive controls prove the audit runs for the same shape, so a workflow with no
audit step cannot satisfy both the controls and the negatives (before the audit exists the controls fail: no audit
step). The scenarios that need the arbiter run against a real project and the real `ck`.
"""

import re

import pytest

from arbiter_helpers import arbiter_project
from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

pytestmark = pytest.mark.usefixtures("no_network")

AUDITOR = "spec:auditor"
AUDITOR_TYPES = ("spec:auditor", "ck-auditor")
DEPS = {"1": [], "2": ["1"]}
MANY = 60  # planSeq entries: enough that every plan call of a run carries the same verifiedBy


def plan_with(verified_by: dict) -> dict:
    """Fake-project options whose every plan answer carries `verifiedBy`, as `ck plan --json` does."""
    return {"planSeq": [{"verifiedBy": verified_by} for _ in range(MANY)]}


def scenario(*, deps=None, tracks=None, verified_by=None, plan_verified_by=None, project=None, args=None,
             auditor=AUDITOR):
    """(project options, workflow args). `verified_by` goes in the args and, unless `plan_verified_by` says
    otherwise, in every plan answer, so the audit decision is the same whichever one the workflow reads."""
    deps = deps if deps is not None else DEPS
    tracks = tracks if tracks is not None else {"1": "test", "2": "impl"}
    vb = verified_by if verified_by is not None else {"2": ["1"]}
    pvb = vb if plan_verified_by is None else plan_verified_by
    proj = {"deps": deps, **plan_with(pvb), **(project or {})}
    a = {"tracks": tracks, "verifiedBy": vb, "auditorAgent": auditor, "maxRetries": 1, **(args or {})}
    return proj, a


def audit_calls(out: dict, n: str | None = None) -> list:
    """Every agent call that is, or starts like, an audit (of task `n`, or of any task)."""
    want = f"audit task {n}" if n is not None else "audit task "
    found = []
    for entry in out["optsList"]:
        label = entry["label"]
        opts = entry["opts"] or {}
        if label.startswith(want) or opts.get("agentType") in AUDITOR_TYPES:
            found.append(entry)
    return found


def auditor_runs(out: dict, n: str) -> list:
    """The auditor agent's own calls for task `n` (the thin `ck lint --simulate` step is not one of them)."""
    return [e for e in out["optsList"]
            if (e["opts"] or {}).get("agentType") in AUDITOR_TYPES and re.search(rf"\b{n}\b", e["label"])
            or re.fullmatch(rf"audit task {n}", e["label"])]


def task_calls(out: dict, n: str) -> list:
    return [e["label"] for e in out["optsList"] if re.match(rf"task {n}\b", e["label"])]


def run(bridge, proj, args, **kw):
    return bridge.run(proj, args, clock="virtual", **kw)


def test_positive_control_the_audit_runs_once_before_the_first_attempt(bridge):
    proj, args = scenario()
    out = run(bridge, proj, args)
    events = out["events"]
    assert len(auditor_runs(out, "2")) == 1, events
    first_audit = next(i for i, e in enumerate(events) if re.match(r"audit task 2\b", e))
    first_attempt = next(i for i, e in enumerate(events) if re.match(r"task 2\b", e))
    assert first_audit < first_attempt, events


def test_positive_control_the_vendored_agent_type_audits_too(bridge):
    proj, args = scenario(auditor="ck-auditor")
    out = run(bridge, proj, args)
    assert len(auditor_runs(out, "2")) == 1, out["events"]


# --------------------------------------------------------------------------- 13.6: a retry or an escalation

RETRY_SHAPES = [
    pytest.param({"failUntil": {"2": 1}}, {"maxRetries": 1}, 2, id="one-retry"),
    pytest.param({"failUntil": {"2": 2}}, {"maxRetries": 3}, 3, id="escalation-after-two-failures"),
    pytest.param({"failUntil": {"2": 5}}, {"maxRetries": 4}, 5, id="exhausted-after-every-attempt"),
    pytest.param({"noCommitsFirst": ["2"]}, {"maxRetries": 1}, 2, id="no-op-first-attempt"),
]


@pytest.mark.parametrize("project,args,attempts", RETRY_SHAPES)
def test_a_retried_or_escalated_task_has_exactly_one_audit(bridge, project, args, attempts):
    proj, a = scenario(project=project, args=args)
    out = run(bridge, proj, a)
    assert len(task_calls(out, "2")) >= attempts, out["events"]  # the retries really ran
    runs = auditor_runs(out, "2")
    assert len(runs) == 1, [e["label"] for e in out["optsList"]]
    events = out["events"]
    audit_at = next(i for i, e in enumerate(events) if re.match(r"audit task 2\b", e))
    assert audit_at < next(i for i, e in enumerate(events) if re.match(r"task 2\b", e))
    assert not [e for e in events[audit_at + 1:] if re.match(r"audit task 2\b", e) and not e.endswith(" end")]


@pytest.mark.parametrize("project,args,attempts", RETRY_SHAPES)
def test_a_failed_first_audit_is_not_run_again_by_a_retry(bridge, project, args, attempts):
    """The audit throws (13.5: the task starts anyway). A retry must not try the audit a second time."""
    proj, a = scenario(project={**project, "auditFindings": {"2": "throw"}}, args=args)
    out = run(bridge, proj, a)
    assert len(task_calls(out, "2")) >= attempts, out["events"]
    assert len(auditor_runs(out, "2")) == 1, [e["label"] for e in out["optsList"]]
    assert out["result"].get("missedAudits"), out["result"]


def test_a_retry_after_an_audit_problem_runs_no_second_audit(bridge, tmp_path):
    """The system's own output fed back: the audit's problem, ruled `code`, then the task fails and is retried."""
    proj_dir = arbiter_project(tmp_path)
    problem = {"file": "tests/test_greet.py", "test": "test_greet", "criterion": "1.1", "reason": "r", "needs": "n"}
    ruling = {"status": "ruled", "outcome": "code", "principle": "project:test-intent",
              "reasoning": "the test is right; the code must change", "code": "print the greeting"}
    proj, args = scenario(
        project={"failUntil": {"2": 1}, "auditFindings": {"2": {"problems": [problem], "summary": "one"}},
                 "rulings": {"2": [ruling]}},
        args={**proj_dir.run_args, "arbiterAgent": "spec:arbiter", "maxRetries": 2},
    )
    out = run(bridge, proj, args, exec_ck=proj_dir.exec_ck)
    assert len(task_calls(out, "2")) >= 2, out["events"]
    assert len(auditor_runs(out, "2")) == 1, [e["label"] for e in out["optsList"]]


def test_a_second_task_is_audited_once_each_while_the_first_retries(bridge):
    deps = {"1": [], "2": ["1"], "3": ["1"]}
    tracks = {"1": "test", "2": "impl", "3": "impl"}
    proj, args = scenario(deps=deps, tracks=tracks, verified_by={"2": ["1"], "3": ["1"]},
                          project={"failUntil": {"2": 2, "3": 1}}, args={"maxRetries": 3})
    out = run(bridge, proj, args)
    assert len(auditor_runs(out, "2")) == 1 and len(auditor_runs(out, "3")) == 1, [e["label"] for e in out["optsList"]]


def test_a_resumed_run_does_not_audit_a_task_the_earlier_run_finished(bridge):
    """A fresh run that finds the task Done on the target starts nothing and audits nothing."""
    proj, args = scenario(project={"done": ["1", "2"]})
    out = run(bridge, proj, args)
    assert audit_calls(out) == [], out["events"]
    assert task_calls(out, "2") == [], out["events"]


def test_a_resumed_run_for_a_task_still_open_audits_it_once_not_per_attempt(bridge):
    """The earlier run audited task 2 and failed it; the resumed run has it open again. Whatever that run decides
    about the first attempt, it never audits the task more than once, however many attempts it spends."""
    proj, args = scenario(project={"done": ["1"], "failUntil": {"2": 2}}, args={"maxRetries": 3, "runId": "resumed-1"})
    out = run(bridge, proj, args)
    assert len(task_calls(out, "2")) >= 3, out["events"]
    assert len(auditor_runs(out, "2")) <= 1, [e["label"] for e in out["optsList"]]


# --------------------------------------------------------------------------- 13.7: no verifying tests, or the test track

NO_VERIFYING = [
    pytest.param({}, id="empty-map"),
    pytest.param({"2": []}, id="empty-list"),
    pytest.param({"3": ["1"]}, id="only-other-task-has-tests"),
    pytest.param({"2": ["99"]}, id="only-entry-names-no-task-in-the-spec"),
    pytest.param({"2": ["99", "100", "0"]}, id="every-entry-names-no-task-in-the-spec"),
    pytest.param({"2": ["2"]}, id="verified-by-itself"),
    pytest.param({"02": ["1"]}, id="key-with-a-leading-zero"),
    pytest.param({" 2": ["1"]}, id="key-with-a-space"),
    pytest.param({"Task 2": ["1"]}, id="key-in-the-markdown-form"),
]


@pytest.mark.parametrize("verified_by", NO_VERIFYING)
def test_an_impl_task_with_no_verifying_tests_is_not_audited(bridge, verified_by):
    proj, args = scenario(verified_by=verified_by)
    out = run(bridge, proj, args)
    assert task_calls(out, "2"), "Task 2 never started: " + str(out["events"])
    assert audit_calls(out) == [], [e["label"] for e in out["optsList"]]


@pytest.mark.parametrize("verified_by", NO_VERIFYING)
def test_no_verifying_tests_still_means_no_audit_when_the_task_is_retried(bridge, verified_by):
    proj, args = scenario(verified_by=verified_by, project={"failUntil": {"2": 2}}, args={"maxRetries": 3})
    out = run(bridge, proj, args)
    assert len(task_calls(out, "2")) >= 3, out["events"]
    assert audit_calls(out) == [], [e["label"] for e in out["optsList"]]


def test_a_near_miss_entry_beside_a_real_test_task_still_audits(bridge):
    """Control for the names-no-task cases: the same impl task with one real verifying test task is audited."""
    proj, args = scenario(verified_by={"2": ["99", "1"]})
    out = run(bridge, proj, args)
    assert len(auditor_runs(out, "2")) == 1, [e["label"] for e in out["optsList"]]


def test_the_plan_that_removes_the_last_verifying_test_task_wins_over_the_args(bridge):
    """The args say task 2 is verified; the plan the run reads says it is not (a landed spec edit): no audit."""
    proj, args = scenario(verified_by={"2": ["1"]}, plan_verified_by={})
    out = run(bridge, proj, args)
    assert task_calls(out, "2"), out["events"]
    assert audit_calls(out) == [], [e["label"] for e in out["optsList"]]


TEST_TRACK_VERIFIED_BY = [
    pytest.param({"2": ["1"]}, id="the-test-task-is-named-for-the-impl-task"),
    pytest.param({"1": ["2"], "2": ["1"]}, id="the-test-task-has-its-own-entry"),
    pytest.param({"1": ["1"]}, id="the-test-task-names-itself"),
    pytest.param({"1": ["2"]}, id="the-test-task-names-the-impl-task-and-nothing-names-the-test"),
]


@pytest.mark.parametrize("verified_by", TEST_TRACK_VERIFIED_BY)
def test_a_test_track_task_is_never_audited_even_when_it_names_a_verified_impl_task(bridge, verified_by):
    proj, args = scenario(verified_by=verified_by)
    out = run(bridge, proj, args)
    assert task_calls(out, "1"), out["events"]
    assert not [e for e in audit_calls(out, "1")], [e["label"] for e in out["optsList"]]
    first_attempt = next(i for i, e in enumerate(out["events"]) if re.match(r"task 1\b", e))
    assert not [e for e in out["events"][:first_attempt] if e.startswith("audit task 1")], out["events"]


def test_a_test_track_task_with_its_own_verifying_tests_is_not_audited_when_retried(bridge):
    """Two test tasks, the second verified by the first (a chain of tests); only impl tasks are audited."""
    deps = {"1": [], "2": ["1"], "3": ["2"]}
    tracks = {"1": "test", "2": "test", "3": "impl"}
    proj, args = scenario(deps=deps, tracks=tracks, verified_by={"2": ["1"], "3": ["2"]},
                          project={"failUntil": {"2": 1}}, args={"maxRetries": 2})
    out = run(bridge, proj, args)
    assert len(task_calls(out, "2")) >= 2, out["events"]
    assert audit_calls(out, "2") == [] and audit_calls(out, "1") == [], [e["label"] for e in out["optsList"]]
    assert len(auditor_runs(out, "3")) == 1, [e["label"] for e in out["optsList"]]


@pytest.mark.parametrize("track", ["test"])
def test_a_test_track_task_alone_with_a_verified_by_entry_of_its_own_is_not_audited(bridge, track):
    proj, args = scenario(deps={"1": []}, tracks={"1": track}, verified_by={"1": ["1"]})
    out = run(bridge, proj, args)
    assert task_calls(out, "1"), out["events"]
    assert audit_calls(out) == [], [e["label"] for e in out["optsList"]]


def test_an_audit_is_not_started_for_a_task_with_no_auditor_agent_named_either_way(bridge):
    """No `auditorAgent`: the control for the case above. The two negatives of 13.7 hold with or without it."""
    proj, args = scenario(verified_by={"2": ["99"]}, auditor="")
    out = run(bridge, proj, args)
    assert audit_calls(out) == [], [e["label"] for e in out["optsList"]]
