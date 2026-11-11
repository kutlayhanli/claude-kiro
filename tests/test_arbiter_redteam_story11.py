"""Red-team for the SHALL NOT clauses of Story 11 (run-principles 11.6, 11.7, 11.10, 11.11, 11.12).

Written from requirements.md only. Two real boundaries:

- 11.7 and 11.10 go through the real PreToolUse hook (`execute_hook("pre-tool-guard", ...)`) with the create-phase
  arbiter's identity from `arbiter_helpers.arbiter_caller(phase="create")`, over real files (symlinks, a closed lane
  ticket, a task that starts after the lane opened) and, for shell forms, real `bash -c`.
- 11.6, 11.11 and 11.12 go through the real `plugin/workflows/spec-create.js` under the fake runtime (`bridge.create`).

Every negative has a positive control (an allowed edit, or a step that does run) so a hook that denies everything, or
a workflow that never calls the arbiter, cannot satisfy it. Project modules are imported inside function bodies.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import arbiter_helpers as ah
import guard_helpers as gh
from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

pytestmark = pytest.mark.usefixtures("no_network")

NODE = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


# --------------------------------------------------------------------------- hook helpers


@pytest.fixture
def project(tmp_path):
    return ah.arbiter_project(tmp_path)


def verdict(result):
    return gh.decision(result) if result and "hookSpecificOutput" in result else None


def denied(result) -> bool:
    return verdict(result) in ("deny", "ask")


def call(caller: dict, tool: str, **tool_input):
    return gh.pre(caller["cwd"], "s-arbiter", tool, agent_id=caller["agent_id"], agent_type=caller["agent_type"], **tool_input)


def edit(caller: dict, path: str, old: str, new: str):
    return call(caller, "Edit", file_path=path, old_string=old, new_string=new)


def write(caller: dict, path: str, content: str = "changed\n"):
    return call(caller, "Write", file_path=path, content=content)


def bash(caller: dict, command: str):
    return gh.run_bash(caller["cwd"], "s-arbiter", command, agent_id=caller["agent_id"], agent_type=caller["agent_type"],
                       cwd=caller["cwd"])


def start_task(checkout: Path, status: str = "In Progress", which: int = 1) -> None:
    """Flip one task's `**Status:**` line (task `which`) so implementation has started."""
    path = checkout / "specs" / "demo" / "tasks.md"
    text = path.read_text()
    head, _, rest = text.partition("### Task 1")
    first, _, second = rest.partition("### Task 2")
    if which == 1:
        first = first.replace("**Status:** Not Started", f"**Status:** {status}", 1)
    else:
        second = second.replace("**Status:** Not Started", f"**Status:** {status}", 1)
    path.write_text(head + "### Task 1" + first + "### Task 2" + second)


def first_line(path: Path) -> str:
    return path.read_text().splitlines()[0]


def create_caller(project) -> dict:
    return ah.arbiter_caller(project, phase="create")


def test_positive_control_lane_allows_the_arbiters_own_files_and_requirements_before_any_task_starts(project):
    caller = create_caller(project)
    root = Path(caller["cwd"])
    for name in ("tasks.md", "design.md", "test-plan.md", "requirements.md"):
        path = root / "specs/demo" / name
        assert not denied(edit(caller, str(path), first_line(path), first_line(path) + " (reworded)")), name


# --------------------------------------------------------------------------- 11.10: PLAN.md through every door

PLAN_FORMS = [
    "specs/demo/PLAN.md",
    "./specs/demo/PLAN.md",
    "specs/demo/sub/../PLAN.md",
    "specs//demo//PLAN.md",
    "specs/demo/./PLAN.md",
]


@pytest.mark.parametrize("form", PLAN_FORMS)
def test_1110_plan_md_denied_in_every_path_spelling(project, form):
    caller = create_caller(project)
    root = Path(caller["cwd"])
    plan = root / "specs/demo/PLAN.md"
    before = plan.read_bytes()
    spelled = str(root) + "/" + form
    assert denied(edit(caller, spelled, first_line(plan), "# Plan: Changed")), form
    assert denied(write(caller, spelled, "# Plan: Replaced\n")), form
    assert denied(call(caller, "MultiEdit", file_path=spelled, edits=[{"old_string": first_line(plan), "new_string": "x"}])), form
    assert plan.read_bytes() == before


def test_1110_plan_md_denied_by_absolute_path_outside_the_checkout_spelling(project):
    caller = create_caller(project)
    root = Path(caller["cwd"])
    plan = root / "specs/demo/PLAN.md"
    before = plan.read_bytes()
    # the same file reached as an absolute path built from a resolved parent
    resolved = Path(os.path.realpath(root)) / "specs/demo/PLAN.md"
    assert denied(write(caller, str(resolved), "x\n"))
    assert plan.read_bytes() == before


def test_1110_plan_md_reached_through_a_symlink_to_an_allowed_file_name(project):
    """tasks.md is the arbiter's own file; replacing it with a symlink to PLAN.md must not open PLAN.md."""
    caller = create_caller(project)
    root = Path(caller["cwd"])
    spec = root / "specs/demo"
    plan = spec / "PLAN.md"
    before = plan.read_bytes()
    (spec / "design.md").unlink()
    (spec / "design.md").symlink_to("PLAN.md")
    assert denied(write(caller, str(spec / "design.md"), "# Plan: pwned\n"))
    assert denied(edit(caller, str(spec / "design.md"), first_line(plan), "# Plan: pwned"))
    assert plan.read_bytes() == before


def test_1110_plan_md_reached_through_a_symlinked_directory(project):
    caller = create_caller(project)
    root = Path(caller["cwd"])
    plan = root / "specs/demo/PLAN.md"
    before = plan.read_bytes()
    (root / "specs/alias").symlink_to("demo")
    assert denied(write(caller, str(root / "specs/alias/PLAN.md"), "pwned\n"))
    assert denied(edit(caller, str(root / "specs/alias/PLAN.md"), first_line(plan), "pwned"))
    assert plan.read_bytes() == before


def test_1110_plan_md_reached_through_a_symlink_outside_specs(project):
    caller = create_caller(project)
    root = Path(caller["cwd"])
    plan = root / "specs/demo/PLAN.md"
    before = plan.read_bytes()
    (root / "notes.md").symlink_to("specs/demo/PLAN.md")
    assert denied(write(caller, str(root / "notes.md"), "pwned\n"))
    assert plan.read_bytes() == before


def test_1110_plan_md_denied_after_the_lane_ticket_is_closed(project):
    """A stale identity must not keep a lane: with the ticket closed every edit of PLAN.md is still denied."""
    caller = create_caller(project)
    root = Path(caller["cwd"])
    plan = root / "specs/demo/PLAN.md"
    before = plan.read_bytes()
    closed = project.ck("arbiter", "lane", "close", "demo", "--json")
    assert closed.returncode == 0, closed.stdout + closed.stderr
    assert denied(write(caller, str(plan), "pwned\n"))
    assert denied(edit(caller, str(plan), first_line(plan), "pwned"))
    assert plan.read_bytes() == before


def test_1110_a_ticket_for_another_spec_does_not_open_this_specs_plan(project):
    """Open a lane for `demo`, then edit PLAN.md of a sibling spec and of a name that merely starts with `demo`."""
    caller = create_caller(project)
    root = Path(caller["cwd"])
    for name in ("other", "demo-other", "demox"):
        (root / "specs" / name).mkdir()
        (root / "specs" / name / "PLAN.md").write_text("# Plan: Other\n")
        assert denied(write(caller, str(root / "specs" / name / "PLAN.md"), "pwned\n")), name
        assert (root / "specs" / name / "PLAN.md").read_text() == "# Plan: Other\n"


@pytest.mark.parametrize("command", [
    "sed -i s/Demo/Pwned/ specs/demo/PLAN.md",
    "echo pwned > specs/demo/PLAN.md",
    "echo pwned >> specs/demo/PLAN.md",
    "echo pwned | tee specs/demo/PLAN.md",
    "cp specs/demo/design.md specs/demo/PLAN.md",
    "mv specs/demo/design.md specs/demo/PLAN.md",
    "rm specs/demo/PLAN.md",
    "python -c \"open('specs/demo/PLAN.md','w').write('x')\"",
    "sed -i s/Demo/Pwned/ ./specs/demo/../demo/PLAN.md",
])
def test_1110_plan_md_denied_for_shell_forms_and_unchanged(project, command):
    caller = create_caller(project)
    root = Path(caller["cwd"])
    plan = root / "specs/demo/PLAN.md"
    before = plan.read_bytes()
    out = bash(caller, command)
    assert denied(out.pre) and not out.ran, out.pre
    assert plan.read_bytes() == before


# --------------------------------------------------------------------------- 11.7: requirements once work has started

REQ_FORMS = [
    "specs/demo/requirements.md",
    "./specs/demo/requirements.md",
    "specs/demo/sub/../requirements.md",
    "specs//demo//requirements.md",
]


@pytest.mark.parametrize("started", [("In Progress", 1), ("Done", 1), ("In Progress", 2), ("Done", 2)])
@pytest.mark.parametrize("form", REQ_FORMS)
def test_117_requirements_denied_in_every_spelling_once_any_task_started(project, started, form):
    caller = create_caller(project)
    root = Path(caller["cwd"])
    start_task(root, *started)
    req = root / "specs/demo/requirements.md"
    before = req.read_bytes()
    spelled = str(root) + "/" + form
    assert denied(edit(caller, spelled, first_line(req), first_line(req) + " (reworded)")), form
    assert denied(write(caller, spelled, req.read_text() + "- 1.2 WHEN x, THE SYSTEM SHALL y.\n")), form
    assert denied(call(caller, "MultiEdit", file_path=spelled, edits=[{"old_string": first_line(req), "new_string": "x"}])), form
    assert req.read_bytes() == before


def test_117_a_task_that_starts_between_the_lane_opening_and_the_edit(project):
    """Lane opened (caller built) with every task Not Started: the first edit is allowed; after a task starts, denied."""
    caller = create_caller(project)
    root = Path(caller["cwd"])
    req = root / "specs/demo/requirements.md"
    assert not denied(edit(caller, str(req), first_line(req), first_line(req) + " (a)"))
    start_task(root, "In Progress", 2)
    before = req.read_bytes()
    assert denied(edit(caller, str(req), first_line(req), first_line(req) + " (b)"))
    assert req.read_bytes() == before


def test_117_a_started_task_that_goes_back_to_not_started_reopens_the_edit_only_by_the_files_own_state(project):
    """No memory: the decision follows tasks.md as it is at the edit, in both directions."""
    caller = create_caller(project)
    root = Path(caller["cwd"])
    req = root / "specs/demo/requirements.md"
    start_task(root, "Done", 1)
    assert denied(write(caller, str(req), req.read_text() + "\n"))
    start_task(root, "Not Started", 1)
    # tasks.md now reads Not Started again; the first line of Task 1 was rewritten as Done once, so check the file.
    text = (root / "specs/demo/tasks.md").read_text()
    assert "**Status:** Done" not in text
    assert not denied(write(caller, str(req), req.read_text() + "\n"))


def test_117_requirements_reached_through_a_symlink_once_started(project):
    caller = create_caller(project)
    root = Path(caller["cwd"])
    start_task(root, "In Progress", 1)
    req = root / "specs/demo/requirements.md"
    before = req.read_bytes()
    spec = root / "specs/demo"
    (spec / "design.md").unlink()
    (spec / "design.md").symlink_to("requirements.md")
    (root / "specs/alias").symlink_to("demo")
    (root / "req-link.md").symlink_to("specs/demo/requirements.md")
    for path in (spec / "design.md", root / "specs/alias/requirements.md", root / "req-link.md"):
        assert denied(write(caller, str(path), "x\n")), path
        assert denied(edit(caller, str(path), first_line(req), "x")), path
    assert req.read_bytes() == before


def test_117_requirements_reached_through_a_symlink_before_any_task_started_is_the_same_file(project):
    """Control: with nothing started the same symlink edit is allowed, so the denials above are about the started state."""
    caller = create_caller(project)
    root = Path(caller["cwd"])
    req = root / "specs/demo/requirements.md"
    (root / "specs/alias").symlink_to("demo")
    assert not denied(edit(caller, str(root / "specs/alias/requirements.md"), first_line(req), first_line(req) + " (reworded)"))


def test_117_requirements_denied_after_the_lane_ticket_is_closed(project):
    caller = create_caller(project)
    root = Path(caller["cwd"])
    req = root / "specs/demo/requirements.md"
    before = req.read_bytes()
    assert project.ck("arbiter", "lane", "close", "demo", "--json").returncode == 0
    assert denied(edit(caller, str(req), first_line(req), "x"))
    assert req.read_bytes() == before


@pytest.mark.parametrize("command", [
    "sed -i s/Demo/Pwned/ specs/demo/requirements.md",
    "echo '- 1.2 WHEN x, THE SYSTEM SHALL y.' >> specs/demo/requirements.md",
    "cp specs/demo/design.md specs/demo/requirements.md",
    "mv specs/demo/requirements.md specs/demo/requirements.bak",
    "rm specs/demo/requirements.md",
    "python -c \"open('specs/demo/requirements.md','a').write('x')\"",
])
def test_117_requirements_denied_for_shell_forms_once_started(project, command):
    caller = create_caller(project)
    root = Path(caller["cwd"])
    start_task(root, "In Progress", 1)
    req = root / "specs/demo/requirements.md"
    before = req.read_bytes()
    out = bash(caller, command)
    assert denied(out.pre) and not out.ran, out.pre
    assert req.read_bytes() == before


def test_117_the_reviser_stays_frozen_on_a_started_spec_for_rewording(project):
    """The existing frozen-text rule for a task agent (not the arbiter) still holds beside the arbiter's denial."""
    caller = create_caller(project)
    root = Path(caller["cwd"])
    start_task(root, "Done", 1)
    req = root / "specs/demo/requirements.md"
    reviser = {"agent_id": "agent-reviser-1", "agent_type": "general-purpose"}
    reworded = gh.pre(str(root), "s-reviser", "Edit", agent_id=reviser["agent_id"], agent_type=reviser["agent_type"],
                      file_path=str(req), old_string=first_line(req), new_string=first_line(req) + " (reworded)")
    assert verdict(reworded) == "deny"
    assert denied(edit(caller, str(req), first_line(req), first_line(req) + " (reworded)"))


# --------------------------------------------------------------------------- workflow helpers (11.6, 11.11, 11.12)

ARBITER_ARGS = {
    "arbiterAgent": "spec:arbiter",
    "runnerAgent": "spec:runner",
    "agents": {"arbiter": {"model": "sonnet", "effort": "medium", "risky_model": "opus", "risky_effort": "high"}},
}


def finding(fid: str, **extra) -> dict:
    return {"id": fid, "file": "requirements.md", "location": fid, "severity": "major", "problem": f"problem {fid}",
            "evidence": "e", "fix": "f", "needsUserDecision": False, **extra}


def refuting(*refuted_ids: str, ids=("R1", "R2", "R3")) -> dict:
    """Overrides: fidelity finds `ids`; the refuter refutes exactly `refuted_ids`; the reviser calls the spec Ready."""
    return {
        "review: fidelity": {"findings": [finding(i) for i in ids]},
        "refute: fidelity": {"verdicts": [{"id": i, "refuted": i in refuted_ids, "reason": f"reason {i}"} for i in ids]},
        "revise spec + write review.md": {"fixed": [i for i in ids if i not in refuted_ids], "notFixed": [], "escalated": [],
                                          "result": "Ready", "schedule": "s"},
    }


def run_create(bridge, overrides: dict, args: dict | None = None) -> dict:  # noqa: F811
    return bridge.create({**ARBITER_ARGS, **(args or {})}, overrides)


def started(out: dict) -> list:
    return [e["label"] for e in out["trace"] if e["ev"] == "start"]


def arbiter_labels(out: dict, step: str) -> list:
    return [l for l in started(out) if l == f"arbiter {step}" or l.startswith(f"arbiter {step} ") or l.startswith(f"arbiter: {step}")]


def ruling(claim: str, outcome: str = "spec_edit", **extra) -> dict:
    return {"claim": claim, "status": "ruled", "outcome": outcome, "reasoning": f"because {claim}", "principle": "project:test-intent",
            "verdict": "refutation stands", **extra}


def blob(out: dict) -> str:
    return json.dumps({"result": out["result"], "logs": out["logs"]}, sort_keys=True)


CUT = {"id": "C1", "story": "5", "title": "Story five", "weakness": "nothing breaks without it",
       "saves": {"criteria": 2, "tasks": 1, "testCases": 1}, "status": "unchecked"}


# --------------------------------------------------------------------------- 11.11: no subjects, no call


@NODE
def test_1111_positive_control_a_refuted_finding_reaches_the_arbiter(bridge):  # noqa: F811
    out = run_create(bridge, refuting("R3"))
    assert arbiter_labels(out, "refuted"), started(out)


@NODE
def test_1111_all_findings_confirmed_with_an_empty_refutation_record_calls_no_arbiter(bridge):  # noqa: F811
    over = refuting()
    over["refute: fidelity"] = {"verdicts": []}
    out = run_create(bridge, over)
    assert arbiter_labels(out, "refuted") == [] and arbiter_labels(out, "cuts") == [], started(out)
    assert out["result"]["review"]["arbiter"]["lost"] == [], "a step with no subjects is not lost"


@NODE
@pytest.mark.parametrize("verdicts", [
    pytest.param([{"id": "R1", "refuted": False, "reason": "this finding is refuted by nobody"},
                  {"id": "R2", "refuted": False, "reason": "refuted: false"},
                  {"id": "R3", "refuted": False, "reason": ""}], id="reasons-that-say-refuted"),
    pytest.param([{"id": "R9", "refuted": True, "reason": "unknown id"}], id="verdict-for-a-finding-that-does-not-exist"),
    pytest.param([{"id": "R1", "refuted": False, "reason": "real"}], id="partial-record"),
])
def test_1111_near_miss_refutation_records_make_no_refuted_call(bridge, verdicts):  # noqa: F811
    over = refuting()
    over["refute: fidelity"] = {"verdicts": verdicts}
    out = run_create(bridge, over)
    assert arbiter_labels(out, "refuted") == [], started(out)


@NODE
def test_1111_no_finding_at_all_calls_neither_step(bridge):  # noqa: F811
    over = {"review: fidelity": {"findings": []}, "review: should-exist": {"findings": []}}
    out = run_create(bridge, over)
    assert arbiter_labels(out, "refuted") == [] and arbiter_labels(out, "cuts") == []


@NODE
def test_1111_each_step_is_skipped_on_its_own_subjects(bridge):  # noqa: F811
    cuts_only = {**refuting(), "review: should-exist": {"findings": [CUT]}, "refute: fidelity": {"verdicts": []}}
    out = run_create(bridge, cuts_only)
    assert arbiter_labels(out, "refuted") == [], started(out)
    assert arbiter_labels(out, "cuts"), started(out)
    refuted_only = {**refuting("R3"), "review: should-exist": {"findings": []}}
    out = run_create(bridge, refuted_only)
    assert arbiter_labels(out, "cuts") == [], started(out)
    assert arbiter_labels(out, "refuted"), started(out)


@NODE
def test_1111_an_empty_cut_list_in_odd_shapes_is_no_subject(bridge):  # noqa: F811
    for odd in ({"findings": []}, {"findings": None}, {}):
        out = run_create(bridge, {**refuting(), "review: should-exist": odd})
        assert arbiter_labels(out, "cuts") == [], (odd, started(out))


@NODE
def test_1111_the_arbiters_own_earlier_empty_answer_does_not_become_a_subject(bridge):  # noqa: F811
    over = {**refuting(), "arbiter refuted": {"rulings": []}, "arbiter cuts": {"rulings": []}, "arbiter drift": {"rulings": []}}
    out = run_create(bridge, over)
    assert arbiter_labels(out, "refuted") == [] and arbiter_labels(out, "cuts") == [] and arbiter_labels(out, "drift") == []


# --------------------------------------------------------------------------- 11.12: failure leaves things as they stood

JUNK = [None, {}, 7, "a string", [], {"rulings": "not a list"}, {"rulings": [None]}, {"rulings": [7, "x", []]},
        {"rulings": [{"claim": "R3", "status": 3, "reasoning": {"bad": True}}]}]


@NODE
@pytest.mark.parametrize("junk", JUNK, ids=[json.dumps(j)[:28] for j in JUNK])
def test_1112_junk_from_the_arbiter_is_not_ready_and_the_check_is_reported_not_run(bridge, junk):  # noqa: F811
    out = run_create(bridge, {**refuting("R3"), "arbiter refuted": junk})
    review = out["result"]["review"]
    assert review["result"] != "Ready", review["result"]
    assert "refuted" in json.dumps(review["arbiter"]["lost"]), review["arbiter"]
    assert review["arbiter"]["rulings"] == []
    assert review["refutedCount"] == 1, "the finding stays as it stood: refuted, neither dropped nor confirmed"


@NODE
def test_1112_no_arbiter_agent_means_not_ready_even_when_revise_says_ready(bridge):  # noqa: F811
    out = bridge.create({**ARBITER_ARGS, "arbiterAgent": None}, refuting("R3"))
    review = out["result"]["review"]
    assert review["result"] != "Ready"
    assert "refuted" in json.dumps(review["arbiter"]["lost"])
    assert arbiter_labels(out, "refuted") == []
    for blank in ("", 0, False):
        out = bridge.create({**ARBITER_ARGS, "arbiterAgent": blank}, refuting("R3"))
        assert out["result"]["review"]["result"] != "Ready", blank


@NODE
def test_1112_one_malformed_entry_among_good_ones_loses_only_that_finding(bridge):  # noqa: F811
    bad = {"claim": "R2", "status": 3, "reasoning": {"bad": True}, "outcome": ["spec_edit"]}
    answer = {"rulings": [ruling("R1"), bad, ruling("R3")]}
    out = run_create(bridge, {**refuting("R1", "R2", "R3"), "arbiter refuted": answer})
    review = out["result"]["review"]
    assert review["result"] != "Ready"
    assert len(review["arbiter"]["rulings"]) == 2, "the two good entries are kept and recorded"
    assert "R2" in json.dumps(review["arbiter"]) or "refuted" in json.dumps(review["arbiter"]["lost"])
    assert review["refutedCount"] == 3


@NODE
@pytest.mark.parametrize("bad", [
    pytest.param({"claim": "R2", "status": "ruled", "reasoning": "r", "outcome": "REVERT", "principle": "project:test-intent"}, id="unknown-outcome"),
    pytest.param({"claim": "R2", "status": "ruled", "reasoning": "r", "outcome": "spec_edit", "principle": "project:made-up"}, id="uncitable-principle"),
    pytest.param({"claim": "R2", "status": "ruled", "reasoning": "", "outcome": "spec_edit", "principle": "project:test-intent"}, id="empty-reasoning"),
    pytest.param({"claim": "R9", "status": "ruled", "reasoning": "r", "outcome": "spec_edit", "principle": "project:test-intent"}, id="claim-of-no-finding"),
    pytest.param({"status": "ruled", "reasoning": "r", "outcome": "spec_edit", "principle": "project:test-intent"}, id="no-claim"),
])
def test_1112_a_malformed_entry_never_counts_as_a_ruling_for_its_finding(bridge, bad):  # noqa: F811
    out = run_create(bridge, {**refuting("R1", "R2"), "arbiter refuted": {"rulings": [ruling("R1"), bad]}})
    review = out["result"]["review"]
    assert len(review["arbiter"]["rulings"]) == 1, review["arbiter"]
    assert review["result"] != "Ready"


@NODE
def test_1112_the_arbiters_own_earlier_output_fed_back_is_not_a_second_ruling(bridge):  # noqa: F811
    """A step's answer repeated (the same entries twice, and a ruling copied into another step) gives one ruling per finding."""
    r1 = ruling("R1")
    answer = {"rulings": [r1, r1, ruling("R2"), r1]}
    out = run_create(bridge, {**refuting("R1", "R2"), "arbiter refuted": answer})
    assert len(out["result"]["review"]["arbiter"]["rulings"]) == 2
    out = run_create(bridge, {**refuting("R1", "R2"), "arbiter refuted": answer, "arbiter cuts": {"rulings": [r1]},
                              "review: should-exist": {"findings": [CUT]}})
    assert len(out["result"]["review"]["arbiter"]["rulings"]) <= 3, "a ruling for R1 in the cuts step is not a ruling on cut C1"


@NODE
def test_1112_a_lost_step_does_not_hide_the_steps_that_ran(bridge):  # noqa: F811
    over = {**refuting("R3"), "review: should-exist": {"findings": [CUT]}, "arbiter refuted": {"rulings": [ruling("R3")]},
            "arbiter cuts": 7}
    out = run_create(bridge, over)
    arb = out["result"]["review"]["arbiter"]
    assert "cuts" in json.dumps(arb["lost"]) and "refuted" not in json.dumps(arb["lost"]), arb
    assert "refuted" in json.dumps(arb["ran"]), arb
    assert out["result"]["review"]["result"] != "Ready"
    assert any(c["id"] == "C1" for c in out["result"]["review"]["proposedCuts"]), "the cut stands as it stood, unchecked"


@NODE
def test_1112_a_lost_drift_step_with_a_plan_present_is_not_ready(bridge, tmp_path):  # noqa: F811
    spec = tmp_path / "specs" / "f"
    spec.mkdir(parents=True)
    (spec / "PLAN.md").write_text("# Plan: F\n\n## Decisions\n\n- retries are capped at 3\n")
    over = {**refuting(), "arbiter drift": None}
    out = bridge.create({**ARBITER_ARGS, "projectRoot": str(tmp_path), "specDir": str(spec)}, over)
    assert arbiter_labels(out, "drift"), started(out)
    assert out["result"]["review"]["result"] != "Ready"
    assert "drift" in json.dumps(out["result"]["review"]["arbiter"]["lost"])


# --------------------------------------------------------------------------- 11.6: a decision is never changed, only reported

DECIDED = "retries are capped at 3"


def plan_project(tmp_path: Path) -> dict:
    spec = tmp_path / "specs" / "f"
    spec.mkdir(parents=True)
    (spec / "PLAN.md").write_text(f"# Plan: F\n\n## Decisions\n\n- {DECIDED}\n")
    return {"projectRoot": str(tmp_path), "specDir": str(spec)}


def wording_fix(outcome: str, **extra) -> dict:
    """A drift ruling framed as a wording fix, whose content changes a decision."""
    return {"claim": "D1", "status": "ruled", "outcome": outcome, "reasoning": "the wording says 3, make it match",
            "principle": "project:test-intent", "verdict": "drift", **extra}


SMUGGLED = [
    pytest.param(wording_fix("spec_edit", changed="reworded 1.3 so that retries are capped at 5", requirement={
        "kind": "change", "wording": "retries are capped at 5", "reason": "wording"}), id="spec_edit-with-a-requirement-change"),
    pytest.param(wording_fix("spec_edit", requirement={"kind": "add", "wording": "retries never stop", "reason": "wording"}),
                 id="spec_edit-with-a-requirement-add"),
    pytest.param(wording_fix("spec_edit", requirement={"kind": "remove", "wording": DECIDED, "reason": "wording"}),
                 id="spec_edit-with-a-requirement-removal"),
    pytest.param(wording_fix("requirements", requirement={"kind": "change", "wording": "retries are capped at 5", "reason": "r"}),
                 id="plain-requirements-outcome"),
    pytest.param(wording_fix("requirements", requirement={"kind": "remove", "wording": DECIDED, "reason": "r"}),
                 id="requirements-removal"),
]


@NODE
@pytest.mark.parametrize("answer", SMUGGLED)
def test_116_a_drift_ruling_that_changes_a_decision_is_reported_and_not_ready(bridge, tmp_path, answer):  # noqa: F811
    paths = plan_project(tmp_path)
    over = {**refuting(), "arbiter drift": {"rulings": [answer]}}
    out = bridge.create({**ARBITER_ARGS, **paths}, over)
    review = out["result"]["review"]
    assert review["result"] != "Ready", review["result"]
    wording = answer["requirement"]["wording"]
    reported = json.dumps(review["openQuestions"]) + json.dumps(review["arbiter"])
    assert wording in reported, "the change is reported to me in the workflow's report"
    assert (tmp_path / "specs/f/PLAN.md").read_text().count(DECIDED) == 1, "PLAN.md is untouched"


@NODE
def test_116_a_drift_ruling_that_only_corrects_wording_is_not_reported_as_a_decision_change(bridge, tmp_path):  # noqa: F811
    """Control: a plain wording correction (no requirement item) does not land in Needs Your Decision."""
    paths = plan_project(tmp_path)
    over = {**refuting(), "arbiter drift": {"rulings": [wording_fix("spec_edit", changed="fixed a typo in 1.3")]}}
    out = bridge.create({**ARBITER_ARGS, **paths}, over)
    assert out["result"]["review"]["openQuestions"] == []
    assert out["result"]["review"]["arbiter"]["lost"] == []


@NODE
def test_116_outcome_spelling_does_not_hide_a_requirement_item(bridge, tmp_path):  # noqa: F811
    paths = plan_project(tmp_path)
    for spelling in ("SPEC_EDIT", "Spec_Edit", " spec_edit", "spec_edit "):
        answer = wording_fix(spelling, requirement={"kind": "change", "wording": "retries are capped at 9", "reason": "r"})
        out = bridge.create({**ARBITER_ARGS, **paths}, {**refuting(), "arbiter drift": {"rulings": [answer]}})
        review = out["result"]["review"]
        assert review["result"] != "Ready", spelling
        assert "retries are capped at 9" not in (tmp_path / "specs/f/PLAN.md").read_text()


@NODE
def test_116_the_arbiters_own_drift_ruling_fed_back_as_a_finding_is_still_not_applied(bridge, tmp_path):  # noqa: F811
    """The system's own output as input: the reported change returns as a review finding whose fix is that change."""
    paths = plan_project(tmp_path)
    change = "retries are capped at 5"
    fed_back = finding("R1", fix=f"Apply: {change}", problem="drift report: " + change)
    over = {**refuting(ids=("R1",)), "review: fidelity": {"findings": [fed_back]},
            "arbiter drift": {"rulings": [wording_fix("requirements", requirement={"kind": "change", "wording": change, "reason": "r"})]}}
    out = bridge.create({**ARBITER_ARGS, **paths}, over)
    assert (tmp_path / "specs/f/PLAN.md").read_text().count(DECIDED) == 1
    assert "arbiter drift" in " ".join(started(out)) or arbiter_labels(out, "drift")
    assert out["result"]["review"]["result"] != "Ready"


@NODE
def test_116_a_cut_that_removes_a_plan_item_is_reported_not_applied(bridge, tmp_path):
    """A sound cut removes something PLAN.md decided: it is shown to me and never applied without my approval."""
    paths = plan_project(tmp_path)
    cut = {**CUT, "story": "1", "title": "Retries cap", "weakness": "no one notices a missing cap"}
    over = {**refuting(), "review: should-exist": {"findings": [cut]},
            "arbiter cuts": {"rulings": [{"claim": "C1", "status": "ruled", "outcome": "requirements", "reasoning": "sound",
                                          "principle": "project:remove-first", "verdict": "sound",
                                          "requirement": {"kind": "remove", "wording": DECIDED, "reason": "cut"}}]}}
    out = bridge.create({**ARBITER_ARGS, **paths}, over)
    labels = started(out)
    assert not [l for l in labels if l.startswith("apply-cuts")], labels
    assert (tmp_path / "specs/f/PLAN.md").read_text().count(DECIDED) == 1
    assert any(c["id"] == "C1" for c in out["result"]["review"]["proposedCuts"])
