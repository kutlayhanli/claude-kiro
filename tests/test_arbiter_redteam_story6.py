"""Red-team for the SHALL NOT clauses of run-principles Story 6 (6.2, 6.3, 6.4, 6.6, 6.7, 6.10).

Written from requirements.md only. Every case runs the real `plugin/workflows/spec-implement.js` under the fake
runtime (`landing_bridge.bridge`, or a node run of the same script for scripts the fake's options cannot express) with
the real `ck` as `execCk` over a real git project, and reads the workflow's own trace: its agent labels in order and its
result. Nothing of the project's own code is mocked.

- 6.2  a guard stop goes to the arbiter and SHALL NOT climb the guard ladder (no `task n guard k (model)` label, no
       further attempt). Near misses: a plain failure whose text quotes guard-stop and test-conflict words.
- 6.3  a review deadlock SHALL NOT escalate the revision up the model ladder (no `revise k (model)` label), alone or in a
       landing batch. Near miss: approval on the last configured round.
- 6.4  a gate failure with no spec claim runs today's retries and escalation unchanged, and SHALL NOT call the arbiter
       until the top model has also failed (every attempt count 1..N, with and without an escalate list).
- 6.6  a retry or escalation that makes the gate pass, even on the last possible rung, SHALL NOT call the arbiter.
- 6.7  a busy or uncreatable worktree SHALL NOT call the arbiter, however the task would otherwise stop.
- 6.10 every failed form of the arbiter call, combined with a prior guard stop, a test conflict or an exhausted ladder,
       is a needs-input entry and SHALL NOT fall back to the guard ladder or a confirmation agent.

The arbiter is reached by a label starting `arbiter` (its agent `arbiter task <n>` or a thin `arbiter-` step), so a
test never depends on the real CLI answering; the labels are the contract (test-plan Harness Contract).
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import arbiter_helpers as ah
from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

JS = Path(__file__).parent / "js"
pytestmark = [pytest.mark.usefixtures("no_network"), pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")]

ARBITER_ROLE = {"model": "opus", "effort": "high", "risky_model": "opus", "risky_effort": "max"}
CONFLICT = {"file": "tests/test_greet.py", "test": "tests/test_greet.py::test_greets", "criterion": "1.1",
            "reason": "the test needs a greeting the requirement forbids", "fix": "drop the assertion"}
SPEC_CLAIM_WORDS = [
    "guard stop", "guardStop", '{"guardStop": {"file": "tests/test_x.py", "kind": "test"}}', "[ck guard] denied the edit",
    "a test that can never pass", "test conflict", "blocked: test (tests/test_x.py)", '"testConflict": {"file": "t"}',
    "[ck post-check] a test was changed, weakened or deleted", "arbiter", "needs-input", "status: blocked",
]


def roles(model="haiku", escalate=(), review=False, rounds=2):
    return {
        "implementer": {"model": model, "effort": "medium", "escalate": list(escalate)},
        "test_writer": {"model": model, "effort": "medium", "escalate": list(escalate)},
        "fixer": {"model": model, "effort": "medium", "escalate": list(escalate)},
        "reviewer": {"model": "opus", "effort": "high", "enabled": review, "rounds": rounds},
        "arbiter": ARBITER_ROLE,
    }


def run(tmp_path, project_opts, *, agents=None, arbiter_agent=True, max_retries=1, args=None, exec_ck="real"):
    project = ah.arbiter_project(tmp_path)
    if exec_ck == "dead":  # every `ck` call exits 3, so a thin arbiter step is a failed call
        exec_ck_opts = {"cmd": [sys.executable, "-c", "import sys; sys.exit(3)"], "cwd": str(project.root)}
    else:
        exec_ck_opts = project.exec_ck
    full = {
        **project.run_args,
        "agents": agents or roles(),
        "maxRetries": max_retries,
        **({"arbiterAgent": "spec:arbiter"} if arbiter_agent else {}),
        **(args or {}),
    }
    return bridge_run(project_opts, full, exec_ck_opts)


_BRIDGE = {}


def bridge_run(project_opts, args, exec_ck_opts):
    return _BRIDGE["b"].run(project_opts, args=args, exec_ck=exec_ck_opts)


@pytest.fixture(autouse=True)
def _bind_bridge(bridge):  # noqa: F811
    _BRIDGE["b"] = bridge


def attempts(out, n="1"):
    """The implementer attempt labels of task n, in order (`task n`, `task n retry k ...`, `task n guard k ...`)."""
    return [e for e in out["events"] if re.match(rf"^task {n}( |$)", e) and not e.endswith(" end") and " revise " not in e]


def arbiter_labels(out):
    return [e for e in out["events"] if e.startswith("arbiter") and not e.endswith(" end")]


def guard_labels(out):
    return [e for e in out["events"] if re.search(r" guard \d+", e) and not e.endswith(" end")]


def confirm_or_escalation_labels(out, n="1"):
    """Anything that runs after the first attempt of task n and before/without the arbiter: retries and escalations."""
    return [e for e in attempts(out, n)[1:]]


def entries(out, n="1"):
    return [e for e in (out["result"].get("needsInput") or []) if str(e.get("task")) == n]


def first_arbiter_index(out):
    return next((i for i, e in enumerate(out["events"]) if e.startswith("arbiter")), None)


def index_of(out, label):
    return out["events"].index(label)


# --------------------------------------------------------------------------- 6.2 guard stops


@pytest.mark.parametrize("ceiling", ["haiku", "sonnet", "opus", "fable", "always"])
@pytest.mark.parametrize("escalate", [[], ["sonnet"], ["sonnet", "opus"]])
def test_a_guard_stop_never_climbs_the_ladder_whatever_model_stopped_or_escalate_says(tmp_path, ceiling, escalate):
    """The old ladder resolved a stop at or below `ceiling` by climbing; the arbiter route must not climb at all."""
    out = run(tmp_path, {"deps": {"1": []}, "guardStops": {"1": ceiling}, "rulings": {"1": [{"status": "ruled"}]}},
              agents=roles("haiku", escalate))
    assert guard_labels(out) == [], guard_labels(out)
    assert arbiter_labels(out), out["events"]
    assert attempts(out) == ["task 1"], attempts(out)  # no further implementer attempt of any kind
    assert first_arbiter_index(out) > index_of(out, "task 1 end")


@pytest.mark.parametrize("model", ["haiku", "sonnet", "opus", "fable"])
def test_a_guard_stop_on_the_top_model_goes_to_the_arbiter_not_a_park_of_the_ladder(tmp_path, model):
    out = run(tmp_path, {"deps": {"1": []}, "guardStops": {"1": "always"}, "rulings": {"1": [{"status": "ruled"}]}}, agents=roles(model))
    assert guard_labels(out) == []
    assert arbiter_labels(out), out["events"]
    assert attempts(out) == ["task 1"]


def test_a_guard_stop_in_a_review_revision_does_not_climb(tmp_path):
    out = run(tmp_path, {"deps": {"1": []}, "reviews": {"1": ["changes", "approve"]}, "guardStops": {"1": "haiku"}, "guardStopIn": {"1": "revise"},
                         "rulings": {"1": [{"status": "ruled"}]}}, agents=roles("haiku", ["sonnet"], review=True))
    assert guard_labels(out) == [], guard_labels(out)
    assert arbiter_labels(out), out["events"]
    assert not [e for e in out["events"] if re.search(r" revise \d+ \(", e)], "a revision on a higher model ran"


@pytest.mark.parametrize("words", SPEC_CLAIM_WORDS)
def test_a_plain_failure_quoting_guard_words_is_not_a_guard_stop(tmp_path, words):
    """Near miss: the failure text names every spec-claim word, but there is no guardStop or testConflict field. Today's
    retry runs and its pass is final; the arbiter is not called for the earlier failure (6.4, 6.6)."""
    failure = {"node": "tests/test_x.py::test_tc1_x", "tcId": "TC-1", "tasks": ["1"], "attributedBy": "tc", "file": "tests/test_x.py",
               "kind": "assertion", "message": words, "truncated": False}
    out = run(tmp_path, {"deps": {"1": []}, "failUntil": {"1": 1}, "taskFailures": {"1": [failure]}})
    assert arbiter_labels(out) == [], arbiter_labels(out)
    assert attempts(out) == ["task 1", "task 1 retry 1"], attempts(out)
    assert out["result"]["merged"] == ["1"]
    assert entries(out) == []


def test_a_conflict_claim_that_appears_only_in_a_retrys_output_still_goes_to_the_arbiter_without_confirmation(tmp_path):
    """The first attempt fails plainly; the retry reports the test that can never pass. The conflict is a spec claim: the
    arbiter follows it with no confirmation attempt and no escalation."""
    out = node_run(tmp_path, {"deps": {"1": []}, "failUntil": {"1": 1}, "rulings": {"1": [{"status": "ruled"}]}},
                   agents=roles("haiku", ["sonnet", "opus"]),
                   wrap="""
                   const inner = p.agent
                   p.agent = async (prompt, opts) => {
                     if (opts.label === 'task 1 retry 1') {
                       const r = await inner(prompt, opts)
                       return { ...r, status: 'blocked', gatePassed: false, blocker: 'test conflict', testConflict: %s }
                     }
                     return inner(prompt, opts)
                   }""" % json.dumps(CONFLICT))
    assert attempts(out) == ["task 1", "task 1 retry 1"], attempts(out)  # nothing after the retry that reported it
    assert arbiter_labels(out), out["events"]
    assert first_arbiter_index(out) > index_of(out, "task 1 retry 1 end")


# --------------------------------------------------------------------------- 6.3 review deadlocks


def deadlock_reviews(n=6):
    return ["changes"] * n


@pytest.mark.parametrize("escalate", [[], ["sonnet"], ["sonnet", "opus"]])
@pytest.mark.parametrize("rounds", [1, 2, 3])
def test_a_review_deadlock_on_a_task_reviewed_alone_never_escalates_the_revision(tmp_path, escalate, rounds):
    out = run(tmp_path, {"deps": {"1": []}, "reviews": {"1": deadlock_reviews()}, "rulings": {"1": [{"status": "ruled"}]}},
              agents=roles("haiku", escalate, review=True, rounds=rounds), args={"risk": {"1": "safety"}})
    revisions = [e for e in out["events"] if " revise " in e and not e.endswith(" end")]
    assert not [e for e in revisions if re.search(r"\(", e)], revisions
    assert len(revisions) <= rounds, revisions  # the configured rounds, never more
    assert arbiter_labels(out), out["events"]
    assert out["result"]["merged"] == []


def test_a_review_deadlock_flagged_by_a_landing_batch_never_escalates_the_revision(tmp_path):
    flag = {"verdict": "changes", "findings": [{"task": "1", "severity": "blocking", "file": "src/greet.py", "issue": "wrong", "fix": "right"}]}
    out = run(tmp_path, {"deps": {"1": [], "2": []}, "reviews": {"1": [flag] * 6}, "rulings": {"1": [{"status": "ruled"}]}},
              agents=roles("haiku", ["sonnet", "opus"], review=True), args={"mergeBatch": 2, "wave": {"1": 1, "2": 1}})
    revisions = [e for e in out["events"] if " revise " in e and not e.endswith(" end")]
    assert not [e for e in revisions if "(" in e], revisions
    assert arbiter_labels(out), out["events"]


@pytest.mark.parametrize("rounds", [1, 2, 3])
def test_an_approval_on_the_last_configured_round_is_not_a_deadlock(tmp_path, rounds):
    """Near miss: changes on every round but the last one, which approves. Nothing deadlocks, so no arbiter."""
    out = run(tmp_path, {"deps": {"1": []}, "reviews": {"1": ["changes"] * rounds + ["approve"]}},
              agents=roles("haiku", ["sonnet"], review=True, rounds=rounds), args={"risk": {"1": "safety"}})
    assert arbiter_labels(out) == [], arbiter_labels(out)
    assert out["result"]["merged"] == ["1"]


# --------------------------------------------------------------------------- 6.4 / 6.6 plain failures


def total_attempts(retries, escalate):
    return 1 + retries + len(escalate)


CASES = [(retries, esc) for retries in (0, 1, 2) for esc in ([], ["sonnet"], ["sonnet", "opus"], ["sonnet", "opus", "fable"])]


@pytest.mark.parametrize("retries,escalate", CASES)
def test_the_arbiter_waits_for_the_top_model_and_a_pass_on_the_last_rung_never_reaches_it(tmp_path, retries, escalate):
    total = total_attempts(retries, escalate)
    # Passing on the last possible rung: total - 1 failures, then a pass. No arbiter, ever.
    last = run(tmp_path / "last", {"deps": {"1": []}, "failUntil": {"1": total - 1}}, agents=roles("haiku", escalate), max_retries=retries)
    assert arbiter_labels(last) == [], arbiter_labels(last)
    assert len(attempts(last)) == total, attempts(last)
    assert last["result"]["merged"] == ["1"]
    # One failure more: every rung failed. Today's attempts all run first, and only then the arbiter.
    allfail = run(tmp_path / "all", {"deps": {"1": []}, "failUntil": {"1": total}, "rulings": {"1": [{"status": "ruled"}]}},
                  agents=roles("haiku", escalate), max_retries=retries)
    assert len(attempts(allfail)) == total, attempts(allfail)
    assert attempts(allfail) == attempts(last)  # unchanged labels, same order as today
    assert arbiter_labels(allfail), allfail["events"]
    assert first_arbiter_index(allfail) > index_of(allfail, attempts(allfail)[-1] + " end")


@pytest.mark.parametrize("words", SPEC_CLAIM_WORDS)
def test_a_plain_failure_that_quotes_claim_words_waits_for_the_top_model(tmp_path, words):
    """Near miss for 6.4: every attempt fails plainly with claim-like text. No arbiter before the last rung."""
    failure = {"node": "tests/test_x.py::test_tc1_x", "tcId": "TC-1", "tasks": ["1"], "attributedBy": "tc", "file": "tests/test_x.py",
               "kind": "assertion", "message": words, "truncated": False}
    out = run(tmp_path, {"deps": {"1": []}, "failUntil": {"1": 99}, "taskFailures": {"1": [failure]}, "rulings": {"1": [{"status": "ruled"}]}},
              agents=roles("haiku", ["sonnet", "opus"]))
    assert len(attempts(out)) == 4, attempts(out)
    assert guard_labels(out) == []
    assert arbiter_labels(out), out["events"]
    assert first_arbiter_index(out) > index_of(out, attempts(out)[-1] + " end")


def test_an_earlier_failure_text_is_not_a_claim_once_a_later_attempt_passes(tmp_path):
    """The system's own earlier output (a gate failure naming the arbiter) is not replayed as a claim after a pass."""
    failure = {"node": "tests/test_x.py::test_tc1_x", "tcId": "TC-1", "tasks": ["1"], "attributedBy": "tc", "file": "tests/test_x.py",
               "kind": "assertion", "message": "needs-input: ask the arbiter about this claim", "truncated": False}
    out = run(tmp_path, {"deps": {"1": [], "2": ["1"]}, "failUntil": {"1": 2}, "taskFailures": {"1": [failure]}}, agents=roles("haiku", ["sonnet", "opus"]))
    assert arbiter_labels(out) == [], arbiter_labels(out)
    assert out["result"]["merged"] == ["1", "2"]


# --------------------------------------------------------------------------- 6.7 busy and failed worktrees


@pytest.mark.parametrize("state", ["BUSY", "ERROR"])
@pytest.mark.parametrize("other", [
    {},
    {"guardStops": {"1": "always"}},
    {"testConflicts": {"1": {"conflict": CONFLICT}}},
    {"failUntil": {"1": 99}},
    {"guardStops": {"1": "haiku"}, "testConflicts": {"1": {"conflict": CONFLICT}}, "failUntil": {"1": 99}},
])
def test_a_busy_or_failed_worktree_never_calls_the_arbiter(tmp_path, state, other):
    project = {"deps": {"1": [], "2": ["1"], "3": []}, "worktree": {"1": {"state": state, "detail": "held by another run"}},
               "rulings": {"1": [{"status": "ruled"}]}, **other}
    out = run(tmp_path, project, agents=roles("haiku", ["sonnet", "opus"]))
    assert arbiter_labels(out) == [], arbiter_labels(out)
    assert guard_labels(out) == []
    assert "2" not in out["result"]["merged"] and "task 2" not in out["events"]
    assert "3" in out["result"]["merged"]  # an independent task is unaffected


@pytest.mark.parametrize("state", ["CREATED", "EXISTS"])
def test_a_worktree_that_is_fine_on_the_next_attempt_follows_the_plain_ladder_and_no_arbiter(tmp_path, state):
    """A worktree reported healthy with a plain failure: today's retry passes, and no arbiter."""
    out = run(tmp_path, {"deps": {"1": []}, "worktree": {"1": {"state": state}}, "failUntil": {"1": 1}}, agents=roles("haiku", ["sonnet"]))
    assert arbiter_labels(out) == [], arbiter_labels(out)
    assert out["result"]["merged"] == ["1"]


@pytest.mark.parametrize("state", ["BUSY", "ERROR"])
def test_a_busy_attempt_then_a_failing_one_in_a_retry_run_only_ever_calls_the_arbiter_for_the_failing_one(tmp_path, state):
    """Busy on one task, a plain failure past the ladder on another: the arbiter is called for the second only."""
    project = {"deps": {"1": [], "2": []}, "worktree": {"1": {"state": state, "detail": "d"}}, "failUntil": {"2": 99},
               "rulings": {"2": [{"status": "ruled"}]}}
    out = run(tmp_path, project, agents=roles("haiku"), max_retries=0)
    assert arbiter_labels(out), out["events"]
    assert attempts(out, "1") == ["task 1"]
    assert not any(e.startswith("arbiter task 1") for e in out["events"])


# --------------------------------------------------------------------------- 6.10 every failed arbiter call


FORMS = ah.FAILURE_FORMS


def form_id(f):
    return "throw" if f == "throw" else json.dumps(f)


STOPS = {
    "guard-stop": ({"guardStops": {"1": "haiku"}}, "haiku"),
    "test-conflict": ({"testConflicts": {"1": {"conflict": CONFLICT}}}, "haiku"),
    "gate-failure": ({"failUntil": {"1": 99}}, "haiku"),
}


@pytest.mark.parametrize("stop", sorted(STOPS))
@pytest.mark.parametrize("form", FORMS, ids=form_id)
def test_a_failed_arbiter_call_is_a_needs_input_entry_with_no_fallback(tmp_path, stop, form):
    extra, model = STOPS[stop]
    out = run(tmp_path, {"deps": {"1": [], "2": ["1"], "3": []}, "rulings": {"1": [form]}, **extra}, agents=roles(model, ["sonnet", "opus"]))
    assert len(entries(out)) == 1, out["result"].get("needsInput")
    assert "1" not in out["result"]["merged"]
    assert guard_labels(out) == []
    if stop != "gate-failure":
        assert attempts(out) == ["task 1"], attempts(out)  # no confirmation attempt, no escalation, no ladder
    assert "task 2" not in out["events"]  # its dependent waits
    assert "3" in out["result"]["merged"]


@pytest.mark.parametrize("form", FORMS, ids=form_id)
def test_a_guard_stop_and_a_failed_arbiter_call_still_never_run_a_ladder_attempt(tmp_path, form):
    """A prior guard stop that today's ladder would resolve on the next model (ceiling haiku): the failed call does not
    let that ladder run."""
    out = run(tmp_path, {"deps": {"1": []}, "guardStops": {"1": "haiku"}, "rulings": {"1": [form, form, form]}}, agents=roles("haiku", ["sonnet"]))
    assert attempts(out) == ["task 1"], attempts(out)
    assert out["result"]["merged"] == []
    assert len(entries(out)) == 1


@pytest.mark.parametrize("stop", sorted(STOPS))
def test_no_arbiter_agent_is_a_needs_input_entry_and_no_fallback(tmp_path, stop):
    extra, model = STOPS[stop]
    out = run(tmp_path, {"deps": {"1": []}, **extra}, agents=roles(model, ["sonnet"]), arbiter_agent=False)
    assert not [e for e in out["events"] if e.startswith("arbiter task")]
    assert guard_labels(out) == []
    assert len(entries(out)) == 1, out["result"].get("needsInput")
    assert out["result"]["merged"] == []
    if stop != "gate-failure":
        assert attempts(out) == ["task 1"], attempts(out)


@pytest.mark.parametrize("stop", sorted(STOPS))
def test_a_failed_ck_arbiter_call_is_a_needs_input_entry_and_no_fallback(tmp_path, stop):
    extra, model = STOPS[stop]
    out = run(tmp_path, {"deps": {"1": []}, "rulings": {"1": [{"status": "ruled"}]}, **extra}, agents=roles(model, ["sonnet"]), exec_ck="dead")
    assert guard_labels(out) == []
    assert len(entries(out)) == 1, out["result"].get("needsInput")
    assert out["result"]["merged"] == []
    if stop != "gate-failure":
        assert attempts(out) == ["task 1"], attempts(out)


def test_a_failed_call_on_one_task_does_not_hold_back_the_next_claim_of_another(tmp_path):
    """Two tasks each stop on a guard; the first arbiter call fails, the second is ruled. Each claim is handled on its own."""
    out = run(tmp_path, {"deps": {"1": [], "2": []}, "guardStops": {"1": "haiku", "2": "haiku"},
                         "rulings": {"1": ["throw"], "2": [{"status": "ruled"}]}}, agents=roles("haiku", ["sonnet"]))
    assert guard_labels(out) == []
    assert attempts(out, "1") == ["task 1"] and attempts(out, "2") == ["task 2"]
    assert len(entries(out, "1")) == 1


# --------------------------------------------------------------------------- a runner for scripts the fake's options cannot express


def node_run(tmp_path, project_opts, *, agents, wrap="", max_retries=1):
    project = ah.arbiter_project(tmp_path)
    args = {**project.run_args, "agents": agents, "maxRetries": max_retries, "arbiterAgent": "spec:arbiter"}
    script = (
        "import { runWorkflow, useVirtualTime } from './load_workflow.mjs'\n"
        "import { project } from './fake_project.mjs'\n"
        f"const p = project({{ ...{json.dumps(project_opts)}, execCk: {json.dumps(project.exec_ck)} }})\n"
        f"{wrap}\n"
        f"const {{ result }} = await runWorkflow('../../plugin/workflows/spec-implement.js', {{ agent: p.agent, args: {json.dumps(args)} }})\n"
        "console.log(JSON.stringify({ result, events: p.state.events }))"
    )
    proc = subprocess.run(["node", "--input-type=module", "-e", script], cwd=JS, capture_output=True, text=True, timeout=240)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])
