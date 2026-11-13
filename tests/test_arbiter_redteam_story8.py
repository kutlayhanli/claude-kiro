"""Red-team for the SHALL NOT clauses of Story 8 (run-principles 8.2, 8.4, 8.5, 8.6, 8.12).

Written from the requirements only. Every case tries to break one prohibition with an adversarial input and expects the
prohibition to hold. Each refusal case uses real git, the real `ck worktree land demo arbiter<k> --into BRANCH --gate
--json`, the real lint and the real hook, and compares the integration branch's HEAD before and after (8.4). The
positive controls land the same shape of edit when it is allowed, so a landing that refuses everything (or does not
exist yet) cannot satisfy both the controls and the refusals.

8.2 and 8.12 run the real `spec-implement.js` through the node bridge (the fake runtime of Task 2, real `ck` through
`exec_ck`), and the decisions-block seam `settleFindings` through the bridge `call` op.
Project modules are imported inside test bodies, so this file collects before the feature exists.
"""

from __future__ import annotations

import json
import re

import pytest

from arbiter_helpers import arbiter_project
from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)
from test_arbiter_land import (
    GREET,
    INTO,
    arbiter_wt,
    assert_not_landed,
    commit_in,
    commit_target,
    edit_spec,
    head,
    land,
    tasks_text,
)

pytestmark = pytest.mark.usefixtures("no_network")


def rewrite(wt, name, fn, msg="spec edit"):
    """Commit `fn(text)` as the new text of a spec file on the arbiter branch."""
    path = wt / "specs" / "demo" / name
    return commit_in(wt, {f"specs/demo/{name}": fn(path.read_text())}, msg)


def assert_lands(p, k=1):
    done, entry = land(p, k)
    assert done.returncode == 0 and entry["state"] == "MERGED", (entry, done.stdout, done.stderr)
    return entry


# --------------------------------------------------------------------------- three-task texts

def three(d1="None", d2="Task 1", d3="Task 2", s1="Not Started", s2="Not Started", s3="Not Started", mark="- [ ] done it") -> str:
    def block(n, deps, status, extra=""):
        return (f"### Task {n}: Step {n}\n**Status:** {status}\n**Track:** impl\n**Requirements:** 1.1\n"
                f"**Description:** Do step {n}.\n**Files:**\n- `src/s{n}.py` - step\n**Verify:** `python -c pass`\n"
                f"{extra}\n**Dependencies:** {deps}\n\n---\n")

    acc = f"\n**Acceptance:**\n{mark}\n"
    return ("# Implementation Tasks: Demo\n\n**Status:** Not Started\n\n## Task Breakdown\n\n"
            + block(1, d1, s1, acc) + "\n" + block(2, d2, s2) + "\n" + block(3, d3, s3))


# =========================================================================== 8.4: a landing that cannot land leaves the branch alone


def test_84_control_a_non_overlapping_target_move_still_lands(tmp_path):
    """The target moves in another spec file: the edit merges clean and lands, so the refusals below are not blanket."""
    p = arbiter_project(tmp_path, tasks=tasks_text())
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "**Description:** Print the greeting.", "**Description:** Print the greeting, once.")
    commit_target(p, "specs/demo/design.md", "# Design: Demo\n\nA greeting, revised.\n", "design moved")
    before = head(p)
    entry = assert_lands(p)
    assert head(p) != before and entry["commit"] == head(p)


def test_84_conflict_that_appears_only_after_the_target_moves(tmp_path):
    """Branch made from an older tip: clean then, conflicting now. The same refusal repeats and never moves HEAD."""
    p = arbiter_project(tmp_path, tasks=tasks_text())
    wt = arbiter_wt(p)
    edit_spec(wt, "design.md", "A greeting.", "A greeting from the arbiter.")
    commit_target(p, "specs/demo/design.md", "# Design: Demo\n\nA greeting from a task.\n", "target moved on the same line")
    before = head(p)
    assert_not_landed(p, 1, ("CONFLICT",), before)
    assert_not_landed(p, 1, ("CONFLICT",), before)  # asking again does not rule again and does not move the branch


def test_84_edit_against_a_block_the_target_deleted_is_a_conflict(tmp_path):
    p = arbiter_project(tmp_path, tasks=three())
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "**Description:** Do step 3.", "**Description:** Do step three.")
    path = p.target / "specs/demo/tasks.md"
    text = path.read_text()
    commit_target(p, "specs/demo/tasks.md", text[: text.index("### Task 3")], "target removed Task 3")
    assert_not_landed(p, 1, ("CONFLICT",), head(p))


def test_84_a_target_move_that_turns_a_clean_merge_red(tmp_path):
    """The merge is clean, the combined tree fails the Verify of a Done task the edit changed: state RED, HEAD unchanged.

    The arbiter edit moves Done Task 1's Verify onto the greeting test (so Task 1 is in the gate scope); the target
    meanwhile gained a failing greeting test, a different file, so the merge is clean but the merged tree is red.
    """
    p = arbiter_project(tmp_path, tasks=tasks_text(s1="Done"))
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "**Verify:** `python -c pass`\n**Verifies:**", f"**Verify:** `uv run pytest -q {GREET}`\n**Verifies:**")
    commit_target(p, GREET, "def test_greet():\n    assert False\n", "target gained a failing test")
    before = head(p)
    assert_not_landed(p, 1, ("RED", "CONFLICT", "REFUSED"), before)


def test_84_cycle_made_only_by_the_combination_of_branch_and_target(tmp_path):
    """Neither side closes a cycle alone; their merge does (1 -> 3 from the branch, 3 -> 2 -> 1 from the target)."""
    p = arbiter_project(tmp_path, tasks=three(d1="None", d2="Task 1", d3="None"))
    wt = arbiter_wt(p)
    rewrite(wt, "tasks.md", lambda t: t.replace("**Dependencies:** None\n\n---\n\n### Task 2", "**Dependencies:** Task 3\n\n---\n\n### Task 2", 1))
    path = p.target / "specs/demo/tasks.md"
    text = path.read_text()
    head3 = text.index("### Task 3")
    commit_target(p, "specs/demo/tasks.md", text[:head3] + text[head3:].replace("**Dependencies:** None", "**Dependencies:** Task 2", 1), "target: 3 after 2")
    before = head(p)
    assert_not_landed(p, 1, ("REFUSED", "CONFLICT"), before)


def test_84_refusal_names_a_reason_and_leaves_the_arbiter_branch_landable(tmp_path):
    """After a refused landing the branch still exists at its own tip, so the claim can go to me with the result."""
    p = arbiter_project(tmp_path, tasks=tasks_text())
    wt = arbiter_wt(p)
    tip = edit_spec(wt, "tasks.md", "### Task 1: Greeting tests\n**Status:** Not Started", "### Task 1: Greeting tests\n**Status:** Done")
    before = head(p)
    entry = assert_not_landed(p, 1, ("REFUSED",), before)
    assert entry["detail"], "the landing result must say why: " + json.dumps(entry)
    from test_arbiter_land import git

    assert git(wt, "rev-parse", "HEAD").strip() == tip


# =========================================================================== 8.5: cycles and new blocking findings


def test_85_control_a_harmless_edit_of_the_three_task_spec_lands(tmp_path):
    p = arbiter_project(tmp_path, tasks=three())
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "**Description:** Do step 2.", "**Description:** Do step two.")
    assert_lands(p)


def _swap_numbers(text: str) -> str:
    """Swap the numbers of the Task 1 and Task 3 headings and nothing else."""
    return (text.replace("### Task 1: Step 1", "### Task @@: Step 1").replace("### Task 3: Step 3", "### Task 1: Step 3")
            .replace("### Task @@: Step 1", "### Task 3: Step 1"))


def test_85_a_task_number_change_closes_a_cycle(tmp_path):
    """No Dependencies line is touched. Swapping two headings' numbers makes 1 depend on 2 and 2 on 1."""
    p = arbiter_project(tmp_path, tasks=three())
    wt = arbiter_wt(p)
    rewrite(wt, "tasks.md", _swap_numbers)
    assert_not_landed(p, 1, ("REFUSED",), head(p))


def test_85_renumbering_a_task_to_its_own_dependency_is_a_cycle(tmp_path):
    p = arbiter_project(tmp_path, tasks=three())
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "### Task 2: Step 2", "### Task 1: Step 2")  # Task 2's own heading now says 1: "Dependencies: Task 1"
    assert_not_landed(p, 1, ("REFUSED",), head(p))


@pytest.mark.parametrize("spelling", ["Task 1, Task 3", "Task 3", "task 3", "Task 3 (after the swap)", "Tasks 3"])
def test_85_cycle_through_each_dependency_spelling(tmp_path, spelling):
    """Near-miss spellings of a dependency that closes 1 -> 3 -> 2 -> 1; the ones lint reads as a dependency are refused,
    the ones it does not read must not be reported as a refusal for a cycle that is not there."""
    p = arbiter_project(tmp_path, tasks=three(d3="Task 2"))
    wt = arbiter_wt(p)
    rewrite(wt, "tasks.md", lambda t: t.replace("**Dependencies:** None", f"**Dependencies:** {spelling}", 1))
    done, entry = land(p)
    from test_arbiter_land import git

    if entry["state"] == "MERGED":
        # Landed: then the text must not carry a cycle for the real lint.
        from arbiter_helpers import ck_run

        lint = json.loads(ck_run(p.target, "lint", "demo", "--json").stdout)
        assert not lint.get("cycles"), (spelling, lint)
    else:
        assert entry["state"] == "REFUSED", entry
        assert git(p.root, "rev-parse", INTO).strip() == head(p)


def test_85_an_edit_beside_a_verify_cycle_the_target_already_has_adds_none(tmp_path):
    """The target moves to a verify cycle after the branch was cut. The branch's edit adds no finding of its own, so it
    may land, and must leave the target's finding as it was; if it is refused the target does not move."""
    from test_arbiter_land import PERF, perf_tasks
    from arbiter_helpers import ck_run

    p = arbiter_project(tmp_path, tasks=perf_tasks())
    assert json.loads(ck_run(p.target, "lint", "demo", "--json").stdout)["verifyCycles"] == []
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "**Description:** Measure it.", "**Description:** Measure it twice.")
    path = p.target / "specs/demo/tasks.md"
    commit_target(p, "specs/demo/tasks.md",
                  path.read_text().replace("**Verify:** `python -c pass`\n**Dependencies:** None\n\n---\n\n### Task 3", f"**Verify:** `uv run pytest -q {PERF}`\n**Dependencies:** None\n\n---\n\n### Task 3"),
                  "target: Task 2 runs Task 1's file")
    found = json.loads(ck_run(p.target, "lint", "demo", "--json").stdout)["verifyCycles"]
    before = head(p)
    done, entry = land(p)
    # The finding exists on the target already, so the edit adds none: it may land, and must leave the finding as it was.
    if entry["state"] == "MERGED":
        assert json.loads(ck_run(p.target, "lint", "demo", "--json").stdout)["verifyCycles"] == found
    else:
        assert head(p) == before


@pytest.mark.parametrize("verify", [f"uv run pytest -q {{perf}}", f"uv run pytest -q -x {{perf}}", 'uv run pytest -q "{perf}"', "python -m pytest -q {perf}"])
def test_85_new_verify_cycle_in_every_resolvable_surface_form(tmp_path, verify):
    from test_arbiter_land import PERF, perf_tasks

    p = arbiter_project(tmp_path, tasks=perf_tasks())
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "**Verify:** `python -c pass`\n**Dependencies:** None\n\n---\n\n### Task 3",
              f"**Verify:** `{verify.format(perf=PERF)}`\n**Dependencies:** None\n\n---\n\n### Task 3")
    assert_not_landed(p, 1, ("REFUSED",), head(p))


# =========================================================================== 8.6: Status fields and acceptance marks


@pytest.mark.parametrize("new", ["Done", "In Progress", "Blocked", "done", "DONE", "Done  ", "  Done", "Done\t"])
def test_86_a_status_value_change_in_any_form(tmp_path, new):
    p = arbiter_project(tmp_path, tasks=tasks_text())
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "### Task 2: Greeting\n**Status:** Not Started", f"### Task 2: Greeting\n**Status:** {new}")
    assert_not_landed(p, 1, ("REFUSED",), head(p))


def test_86_a_case_only_change_of_the_same_status_value_is_either_outcome(tmp_path):
    """Whether `Not started` for `Not Started` is a change is a reading of 8.6; either reading is allowed, a half-landing is not."""
    p = arbiter_project(tmp_path, tasks=tasks_text())
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "### Task 2: Greeting\n**Status:** Not Started", "### Task 2: Greeting\n**Status:** Not started")
    done, entry = land(p)
    assert entry["state"] in ("MERGED", "REFUSED"), entry


@pytest.mark.parametrize("old,new", [("Not Started", "In Progress"), ("In Progress", "Not Started"), ("Done", "In Progress"), ("Done", "Not Started")])
def test_86_a_status_reverted_or_reset_is_also_a_change(tmp_path, old, new):
    p = arbiter_project(tmp_path, tasks=tasks_text(s1=old))
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", f"### Task 1: Greeting tests\n**Status:** {old}", f"### Task 1: Greeting tests\n**Status:** {new}")
    assert_not_landed(p, 1, ("REFUSED",), head(p))


def test_86_the_overall_status_line_of_the_header_is_not_a_task_status(tmp_path):
    """Control: the file-level `**Status:**` is not a task's field in the sense of 8.6, but a task's is; only the task refuses."""
    p = arbiter_project(tmp_path, tasks=tasks_text())
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "# Implementation Tasks: Demo\n\n**Status:** Not Started", "# Implementation Tasks: Demo\n\n**Status:** In Progress")
    done, entry = land(p)
    assert entry["state"] in ("MERGED", "REFUSED"), entry  # either reading is allowed; the state must be one of the two, never a half-landing
    assert head(p) == __import__("subprocess").run(["git", "rev-parse", INTO], cwd=p.root, capture_output=True, text=True).stdout.strip()


def test_86_status_change_hidden_in_a_renamed_block(tmp_path):
    p = arbiter_project(tmp_path, tasks=tasks_text())
    wt = arbiter_wt(p)
    rewrite(wt, "tasks.md", lambda t: t.replace("### Task 2: Greeting\n**Status:** Not Started", "### Task 2: Greeting, politely\n**Status:** Done"))
    assert_not_landed(p, 1, ("REFUSED",), head(p))


def test_86_status_change_hidden_in_a_reordered_block(tmp_path):
    """Task 2's block moves above Task 1's and its Status flips in the same commit: a line diff sees only moved text."""
    p = arbiter_project(tmp_path, tasks=tasks_text())
    wt = arbiter_wt(p)

    def reorder(text):
        head_, rest = text.split("### Task 1:", 1)
        one, two = rest.split("### Task 2:", 1)
        two = two.replace("**Status:** Not Started", "**Status:** Done", 1)
        return head_ + "### Task 2:" + two.rstrip("\n") + "\n\n---\n\n### Task 1:" + one

    rewrite(wt, "tasks.md", reorder)
    assert_not_landed(p, 1, ("REFUSED",), head(p))


def test_86_status_value_swapped_between_two_tasks(tmp_path):
    """Both tasks keep a Status of the same set of values, but each task's own value changed."""
    p = arbiter_project(tmp_path, tasks=tasks_text(s1="Done"))
    wt = arbiter_wt(p)
    rewrite(wt, "tasks.md", lambda t: t.replace("### Task 1: Greeting tests\n**Status:** Done", "### Task 1: Greeting tests\n**Status:** @@")
            .replace("### Task 2: Greeting\n**Status:** Not Started", "### Task 2: Greeting\n**Status:** Done").replace("**Status:** @@", "**Status:** Not Started"))
    assert_not_landed(p, 1, ("REFUSED",), head(p))


@pytest.mark.parametrize("status", ["In Progress", "Done"])
def test_86_a_started_task_cannot_be_removed(tmp_path, status):
    p = arbiter_project(tmp_path, tasks=three(s1=status))
    wt = arbiter_wt(p)

    def drop_first(text):
        start = text.index("### Task 1:")
        end = text.index("### Task 2:")
        return text[:start] + text[end:].replace("**Dependencies:** Task 1", "**Dependencies:** None", 1)

    rewrite(wt, "tasks.md", drop_first)
    assert_not_landed(p, 1, ("REFUSED",), head(p))


def test_86_a_started_task_cannot_be_removed_by_renaming_its_heading_away(tmp_path):
    """The block is not deleted: its heading number changes so its Status line belongs to no task any more."""
    p = arbiter_project(tmp_path, tasks=three(s1="Done"))
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "### Task 1: Step 1\n**Status:** Done", "### Task 9: Step 1\n**Status:** Done")
    assert_not_landed(p, 1, ("REFUSED",), head(p))


def test_86_control_removing_a_task_that_never_started_is_not_a_status_change(tmp_path):
    p = arbiter_project(tmp_path, tasks=three())
    wt = arbiter_wt(p)
    rewrite(wt, "tasks.md", lambda t: t[: t.index("### Task 3:")].rstrip("\n") + "\n")
    done, entry = land(p)
    assert entry["state"] in ("MERGED", "REFUSED"), entry
    if entry["state"] == "REFUSED":
        pytest.fail("removing a Not Started task changes no Status field or acceptance mark and 8.6 does not refuse it: " + json.dumps(entry))


@pytest.mark.parametrize("base,new", [
    ("- [ ] done it", "- [x] done it"),
    ("- [ ] done it", "- [X] done it"),
    ("- [ ] done it", "* [x] done it"),
    ("- [ ] done it", "+ [x] done it"),
    ("- [ ] done it", "-   [x] done it"),
    ("- [ ] done it", "- [x]  done it"),
    ("- [ ] done it", "-\t[x] done it"),
    ("- [ ] done it", "  - [x] done it"),
    ("- [ ] done it", "- [x]done it"),
    ("- [x] done it", "- [ ] done it"),
    ("- [X] done it", "- [ ] done it"),
    ("- [x] done it", "- [X] done it"),
    ("- [x] done it", "- [x]  done it"),
    ("- [ ] done it", "- [ ] done it, reworded and ticked? [x]"),
])
def test_86_an_acceptance_mark_changed_in_any_surface_form(tmp_path, base, new):
    """Case and whitespace around a mark must neither hide a flip nor make a non-flip look like one."""
    p = arbiter_project(tmp_path, tasks=three(mark=base))
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", base, new)
    done, entry = land(p)
    from_mark = re.match(r"^\s*[-*+]\s*\[( |x|X)\]", base).group(1).lower() == "x"
    m = re.match(r"^\s*[-*+]\s*\[( |x|X)\]", new)
    to_mark = m is not None and m.group(1).lower() == "x"
    flipped = from_mark != to_mark
    if flipped:
        assert entry["state"] == "REFUSED", (base, new, entry)
        assert head(p) == __import__("subprocess").run(["git", "rev-parse", INTO], cwd=p.root, capture_output=True, text=True).stdout.strip()
    else:
        # a case or spacing change that leaves the mark's meaning alone: either reading is allowed, a half-landing is not
        assert entry["state"] in ("MERGED", "REFUSED"), (base, new, entry)


def test_86_a_mark_flipped_on_the_second_criterion_among_many(tmp_path):
    marks = "- [ ] first\n- [ ] second\n- [x] third\n"
    p = arbiter_project(tmp_path, tasks=three(mark=marks.rstrip("\n")))
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "- [ ] second", "- [x] second")
    assert_not_landed(p, 1, ("REFUSED",), head(p))


def test_86_a_mark_flipped_in_the_same_commit_as_an_allowed_edit(tmp_path):
    """An allowed Verify change beside one flipped mark: the whole branch is refused, not just the mark's line."""
    p = arbiter_project(tmp_path, tasks=three())
    wt = arbiter_wt(p)
    rewrite(wt, "tasks.md", lambda t: t.replace("**Description:** Do step 2.", "**Description:** Do step two.").replace("- [ ] done it", "- [x] done it"))
    assert_not_landed(p, 1, ("REFUSED",), head(p))


def test_86_a_flip_in_one_commit_and_an_allowed_edit_in_the_next_is_still_a_net_flip(tmp_path):
    """Flipping the mark in the first commit and doing an allowed edit in the second is still a net flip and is refused."""
    p = arbiter_project(tmp_path, tasks=three())
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "- [ ] done it", "- [x] done it", "tick")
    edit_spec(wt, "tasks.md", "**Description:** Do step 2.", "**Description:** Do step two.", "reword")
    assert_not_landed(p, 1, ("REFUSED",), head(p))


@pytest.mark.parametrize("old,new", [
    ("**Description:** Do step 2.", "**Description:** Status: Done is the goal of step 2."),
    ("**Description:** Do step 2.", "**Description:** Tick [x] when step 2 is finished; - [x] is how."),
    ("**Verify:** `python -c pass`\n\n**Dependencies:** Task 1", "**Verify:** `python -c \"print('**Status:** Done')\"`\n\n**Dependencies:** Task 1"),
])
def test_86_control_text_that_only_mentions_a_status_or_a_mark_lands(tmp_path, old, new):
    p = arbiter_project(tmp_path, tasks=three())
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", old, new)
    assert_lands(p)


def test_86_a_status_line_and_mark_in_design_md_is_no_task_status_and_lands(tmp_path):
    """A Status line and a checked box added to design.md are no task's Status or mark, so the edit lands."""
    p = arbiter_project(tmp_path, tasks=three())
    wt = arbiter_wt(p)
    rewrite(wt, "design.md", lambda t: t + "\n**Status:** Done\n- [x] a note\n")
    assert_lands(p)


# =========================================================================== 8.12: a settled finding never blocks again


def ident(file: str, issue: str) -> str:
    return f"{file}#{' '.join(issue.lower().split())}"


def finding(file="tests/test_a.py", issue="the test asserts the opposite of 1.1", severity="blocking", **extra):
    return {"severity": severity, "file": file, "issue": issue, "fix": "change it", **extra}


def settle(bridge, findings, settled):  # noqa: F811
    review = {"verdict": "changes", "findings": findings, "summary": "s"}
    out = bridge.call("settleFindings", review, settled)
    return out, [f["issue"] for f in (out or {}).get("findings", [])]


BASE_ISSUE = "the test asserts the opposite of 1.1"


@pytest.mark.parametrize("raised", [
    BASE_ISSUE.upper(),
    BASE_ISSUE.title(),
    "  the test   asserts the opposite of 1.1  ",
    "the test asserts\tthe opposite\nof 1.1",
    "The Test Asserts The Opposite Of 1.1\n",
])
def test_812_the_same_finding_in_case_and_whitespace_variants_does_not_block(bridge, raised):  # noqa: F811
    out, remaining = settle(bridge, [finding(issue=raised)], [ident("tests/test_a.py", BASE_ISSUE)])
    assert remaining == [], out


@pytest.mark.parametrize("raised_file", ["./tests/test_a.py", "tests/test_a.py"])
def test_812_a_leading_dot_slash_on_the_file_is_the_same_file(bridge, raised_file):  # noqa: F811
    out, remaining = settle(bridge, [finding(file=raised_file)], [ident("tests/test_a.py", BASE_ISSUE)])
    assert remaining == [], out


def test_812_a_settled_finding_raised_in_other_words_on_the_same_file_is_removed_when_marked(bridge):  # noqa: F811
    reworded = "1.1 says the opposite of what this assertion checks"
    out, remaining = settle(bridge, [finding(issue=reworded, settledBy=1)], [ident("tests/test_a.py", BASE_ISSUE)])
    assert remaining == [], out


def test_812_reworded_and_marked_beside_a_different_issue_removes_only_the_settled_one(bridge):  # noqa: F811
    reworded = "1.1 says the opposite of what this assertion checks"
    other = "the fixture writes outside tmp_path"
    out, remaining = settle(bridge, [finding(issue=reworded, settledBy=1), finding(issue=other)], [ident("tests/test_a.py", BASE_ISSUE)])
    assert remaining == [other], out


def test_812_a_different_issue_on_the_same_file_still_blocks(bridge):  # noqa: F811
    other = "the test never calls the greeting"
    out, remaining = settle(bridge, [finding(issue=other)], [ident("tests/test_a.py", BASE_ISSUE)])
    assert remaining == [other], out


@pytest.mark.parametrize("raised_issue", [
    "the test asserts the opposite of 1.2",       # near miss: one character of the criterion
    "the test asserts the opposite of 1.1 and also checks stderr",  # superset
    "test asserts the opposite of 1.1",           # missing a word
    "the test asserts the opposite of 11",        # whitespace-free near miss
])
def test_812_a_near_miss_of_a_settled_issue_is_a_different_finding(bridge, raised_issue):  # noqa: F811
    out, remaining = settle(bridge, [finding(issue=raised_issue)], [ident("tests/test_a.py", BASE_ISSUE)])
    assert remaining == [raised_issue], out


@pytest.mark.parametrize("other_file", ["tests/test_b.py", "tests/TEST_A.py", "tests/test_a.py.bak", "src/tests/test_a.py", "tests/sub/test_a.py"])
def test_812_the_same_issue_on_another_file_is_a_different_finding(bridge, other_file):  # noqa: F811
    out, remaining = settle(bridge, [finding(file=other_file)], [ident("tests/test_a.py", BASE_ISSUE)])
    assert remaining == [BASE_ISSUE], out


def test_812_a_different_blocking_finding_beside_a_settled_one_stays(bridge):  # noqa: F811
    other = "an unrelated blocking problem"
    out, remaining = settle(bridge, [finding(), finding(issue=other)], [ident("tests/test_a.py", BASE_ISSUE)])
    assert remaining == [other], out
    assert all(f["severity"] == "blocking" for f in out["findings"] if f["issue"] == other)  # it is passed through unchanged


def test_812_no_settled_findings_removes_nothing_and_settledby_alone_is_not_a_match_on_an_empty_set(bridge):  # noqa: F811
    out, remaining = settle(bridge, [finding(), finding(issue="another")], [])
    assert remaining == [BASE_ISSUE, "another"], out


def test_812_hash_inside_the_issue_does_not_confuse_the_identity(bridge):  # noqa: F811
    issue = "section #3 of the file says x#y"
    out, remaining = settle(bridge, [finding(issue=issue), finding(issue="section")], [ident("tests/test_a.py", issue)])
    assert remaining == ["section"], out


def test_812_the_outputs_of_one_settlement_fed_back_settle_nothing_more(bridge):  # noqa: F811
    first, remaining = settle(bridge, [finding(), finding(issue="other")], [ident("tests/test_a.py", BASE_ISSUE)])
    again = bridge.call("settleFindings", first, [ident("tests/test_a.py", BASE_ISSUE)])
    assert [f["issue"] for f in again["findings"]] == remaining == ["other"]


# =========================================================================== 8.2: a landed spec edit spends no retry or escalation

AGENTS = {
    "implementer": {"model": "haiku", "effort": "low"},
    "test_writer": {"model": "haiku", "effort": "low"},
    "fixer": {"model": "haiku", "effort": "low"},
    "reviewer": {"enabled": False},
    "orchestrator": {"model": "sonnet"},
    "resolver": {"model": "sonnet"},
}
RULING_PRINCIPLE = "project:test-intent"
FAILURE = {"node": "tests/test_greet.py::test_greet", "tcId": "TC-1", "kind": "assertion"}
# design.md, Claim identity: `<task>|gate:<sorted failing ids>`, a test id keeping what follows the last `::`, lowercased.
CLAIM = "1|gate:test_greet"


def run_with_ruling(bridge, tmp_path, *, escalate, fail_until, max_retries, ruled=True):  # noqa: F811
    p = arbiter_project(tmp_path, tasks=tasks_text())
    tasks = (p.target / "specs/demo/tasks.md").read_text()
    edited = tasks.replace("**Description:** Print the greeting.", "**Description:** Print the greeting, as 1.1 says.")
    implementer = {**AGENTS["implementer"], **({"escalate": escalate} if escalate else {})}
    project = {"deps": {"1": []}, "failUntil": {"1": fail_until}, "taskFailures": {"1": [FAILURE]}}
    if ruled:
        project["rulings"] = {"1": [{"claim": CLAIM, "status": "ruled", "outcome": "spec_edit", "reasoning": "the spec was wrong",
                                      "principle": RULING_PRINCIPLE, "changed": "tasks.md: Task 1 description"}]}
        project["arbiterEdits"] = {"1": {"files": {"specs/demo/tasks.md": edited}}}
    args = {**p.run_args, "spec": "demo", "agents": {**AGENTS, "implementer": implementer, "arbiter": {"model": "opus", "effort": "high", "risky_model": "opus", "risky_effort": "max"}},
            "arbiterAgent": "spec:arbiter", "maxRetries": max_retries, "wave": {"1": 1}}
    out = bridge.run(project=project, args=args, exec_ck=p.exec_ck, clock="virtual")
    labels = [e["label"] for e in out["trace"] if e["ev"] == "start"]
    return out, labels


def test_82_a_ruling_on_the_last_attempt_still_leaves_the_task_one_resumed_attempt(bridge, tmp_path):  # noqa: F811
    """No escalate list, one retry: attempts 1 and 2 fail, the ruling lands a spec edit, and the resumed attempt runs
    although the retry budget is spent. A budget that the ruling used up would end the task without it."""
    out, labels = run_with_ruling(bridge, tmp_path, escalate=None, fail_until=2, max_retries=1)
    [task] = [t for t in out["result"]["tasks"] if t["task"] == "1"]
    assert "arbiter task 1" in labels, labels
    assert task["ok"] is True, task
    assert task["attempts"] == 3, task  # the two configured attempts and the resumed one
    assert task["escalations"] == [], task  # the ruling is not an escalation


def test_82_a_ruling_after_the_top_escalated_model_failed_adds_no_escalation_and_no_retry(bridge, tmp_path):  # noqa: F811
    out, labels = run_with_ruling(bridge, tmp_path, escalate=["sonnet"], fail_until=3, max_retries=1)
    [task] = [t for t in out["result"]["tasks"] if t["task"] == "1"]
    assert "arbiter task 1" in labels, labels
    assert task["ok"] is True, task
    assert [e["model"] for e in task["escalations"]] == ["sonnet"], task  # exactly the configured rung, none for the ruling
    assert task["attempts"] == 4, task


def test_82_a_second_failure_after_the_ruling_is_not_forgiven_and_is_not_ruled_again(bridge, tmp_path):  # noqa: F811
    """The ruling forgives nothing else: when the resumed attempt fails too, the task is not retried again on the ruling's account."""
    out, labels = run_with_ruling(bridge, tmp_path, escalate=None, fail_until=9, max_retries=1)
    [task] = [t for t in out["result"]["tasks"] if t["task"] == "1"]
    assert task["ok"] is False, task
    assert task["attempts"] <= 4, task  # two configured, one resumed, at most one more; never an open loop
    assert labels.count("arbiter task 1") <= 2, labels  # the same claim is never ruled again (7.3)


def test_82_a_run_without_the_ruling_spends_exactly_the_configured_attempts(bridge, tmp_path):  # noqa: F811
    """Metamorphic base: with the same failures the unruled attempts are the configured ones, so the ruled run above
    differs from it by the resumed attempt only."""
    out, labels = run_with_ruling(bridge, tmp_path, escalate=None, fail_until=1, max_retries=1, ruled=False)
    [task] = [t for t in out["result"]["tasks"] if t["task"] == "1"]
    assert task["ok"] is True and task["attempts"] == 2 and task["escalations"] == [], task
    assert not [l for l in labels if l.startswith("arbiter")], labels

