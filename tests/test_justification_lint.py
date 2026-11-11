"""Justification lint through the real CLI and at launch (run-principles Task 12: TC-13, TC-18 to TC-25).

Every case runs the real `ck` (`python -m claude_kiro.cli`) over real files in a real git project. TC-25 runs the real
`plugin/workflows/spec-implement.js` under Node against a real project with real `ck plan`, with scripted agents only.
Nothing of the project's own is mocked, and project modules are imported inside test bodies so the file collects
before the feature exists. Before it exists the cases fail on the missing `justifications` key, the missing `warnings`
entry, or the exit code that stays 0.
"""

import json
import shutil
import sys
from pathlib import Path
from typing import Any, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import unattended_helpers as h  # noqa: E402

SPEC = "sim"
STORY_BODY = "**As a** user **I want** x **So that** y.\n"
CRITERIA = "**Acceptance Criteria:**\n\n- {n}.1 WHEN x, THE SYSTEM SHALL y.\n"
# the keys `ck lint --json` printed before this feature
TODAY_KEYS = ["commandEdges", "criticalPath", "cycles", "graphProblems", "noWriteSet", "reasonless", "riskUntagged",
              "unknownCommands", "untaggedTests", "verifyCycles", "waves"]
NEW_KEYS = ["justifications", "verifyScope", "notChecked", "warnings"]


# --------------------------------------------------------------------------- helpers


def story(n: int, title: Optional[str] = None, line: Optional[str] = "**Without it:** the feature is missing.") -> str:
    """One story section: heading, body, an optional justification line, then its criteria."""
    title = title or f"Title {n}"
    parts = [f"### Story {n}: {title}\n", STORY_BODY]
    if line is not None:
        parts.append(line + "\n")
    parts.append(CRITERIA.format(n=n))
    return "\n".join(parts)


def doc(*stories: str, head: str = "") -> str:
    return "# Requirements: Sim\n\n## User Stories\n\n" + head + "\n".join(stories)


def project(tmp_path: Path, requirements: Optional[str], plan: Optional[str] = None) -> Path:
    root = h.spec_graph_project(tmp_path, tasks=[{"num": 1, "title": "Core", "files": ["src/mtr/a.py"]}])
    spec = root / "specs" / SPEC
    if requirements is None:
        (spec / "requirements.md").unlink()
    else:
        (spec / "requirements.md").write_text(requirements)
    if plan is not None:
        (spec / "PLAN.md").write_text(plan)
    return root


def lint(root: Path) -> dict:
    code, obj, err = h.ck_json(root, "lint", SPEC, "--json")
    assert obj is not None, f"ck lint --json printed no JSON (exit {code}): {err}"
    return obj


def exit_code(root: Path) -> int:
    return h.ck_text(root, "lint", SPEC)[0]


def justifications(obj: dict) -> List[dict]:
    assert "justifications" in obj, f"lint JSON has no 'justifications' key: {sorted(obj)}"
    return list(obj["justifications"])


def stories_reported(obj: dict) -> List[str]:
    return [str(e["story"]) for e in justifications(obj)]


# --------------------------------------------------------------------------- TC-13 (1.11)

PLAN_EMPTY = "# Plan: Sim\n\n## Principles\n\n## Scope\n\n- something\n"
PLAN_FILLED = "# Plan: Sim\n\n## Principles\n\n- **Nothing to add:** the project defaults apply.\n"


def test_tc13_empty_plan_principles_warns_naming_the_plan_and_exits_zero(tmp_path):
    root = project(tmp_path, doc(story(1)), plan=PLAN_EMPTY)
    obj = lint(root)
    assert "warnings" in obj, f"lint JSON has no 'warnings' key: {sorted(obj)}"
    found = [w for w in obj["warnings"] if w.get("kind") == "principles"]
    assert len(found) == 1, obj["warnings"]
    assert "PLAN.md" in json.dumps(found[0]) or SPEC in json.dumps(found[0]), found[0]
    assert exit_code(root) == 0


def test_tc13_clean_spec_has_an_empty_warnings_list(tmp_path):
    root = project(tmp_path, doc(story(1)), plan=PLAN_FILLED)
    obj = lint(root)
    assert "warnings" in obj, f"lint JSON has no 'warnings' key: {sorted(obj)}"
    assert obj["warnings"] == []
    assert exit_code(root) == 0


# --------------------------------------------------------------------------- TC-18 (2.5)


def test_tc18_a_line_per_story_and_none_per_criterion_gives_no_finding(tmp_path):
    root = project(tmp_path, doc(story(1), story(2), story(3)))
    obj = lint(root)
    assert justifications(obj) == []
    assert exit_code(root) == 0


# --------------------------------------------------------------------------- TC-19 (3.1)


def test_tc19_a_story_with_no_line_fails_lint_and_is_named(tmp_path):
    root = project(tmp_path, doc(story(1), story(2, "Needs a reason", line=None), story(3)))
    obj = lint(root)
    found = justifications(obj)
    assert [str(e["story"]) for e in found] == ["2"], found
    assert found[0]["title"] == "Needs a reason", found[0]
    code, out, err = h.ck_text(root, "lint", SPEC)
    assert code != 0, out + err
    assert "Story 2" in out + err and "Needs a reason" in out + err, out + err


# --------------------------------------------------------------------------- TC-20 (3.2)

EMPTY_FORMS = ["", " ", "   ", "**", "****", "_ _", "__", "** **", "_", "*"]


@pytest.mark.parametrize("form", EMPTY_FORMS)
def test_tc20_a_line_with_only_whitespace_or_markup_is_missing(tmp_path, form):
    root = project(tmp_path, doc(story(1), story(2, line=f"**Without it:** {form}")))
    obj = lint(root)
    assert stories_reported(obj) == ["2"], (form, justifications(obj))
    assert exit_code(root) != 0


def test_tc20_a_label_alone_is_missing(tmp_path):
    root = project(tmp_path, doc(story(1), story(2, line="**Without it:**")))
    assert stories_reported(lint(root)) == ["2"]
    assert exit_code(root) != 0


# --------------------------------------------------------------------------- TC-21 (3.3)

WHATEVER = ["n/a", "nice to have", "N/A", "none", "x", "because", "_italic_ reason", "**bold** reason"]


@pytest.mark.parametrize("text", WHATEVER)
def test_tc21_any_non_empty_line_gives_no_finding(tmp_path, text):
    root = project(tmp_path, doc(story(1, line=f"**Without it:** {text}"), story(2, line=f"**Without it:** {text}")))
    obj = lint(root)
    assert justifications(obj) == [], (text, obj["justifications"])
    assert exit_code(root) == 0


# --------------------------------------------------------------------------- TC-22 (3.4)

FENCED = "```\n**Without it:** inside a fence\n```"
TILDE = "~~~\n**Without it:** inside a tilde fence\n~~~"


def test_tc22_a_line_above_the_first_story_counts_for_no_story(tmp_path):
    text = doc(story(1, line=None), head="**Without it:** above every story.\n\n")
    assert stories_reported(lint(project(tmp_path, text))) == ["1"]


def test_tc22_a_line_under_another_heading_counts_for_no_story(tmp_path):
    text = doc(story(1, line=None) + "\n## Notes\n\n**Without it:** under another heading.\n")
    root = project(tmp_path, text)
    assert stories_reported(lint(root)) == ["1"]
    assert exit_code(root) != 0


@pytest.mark.parametrize("fence", [FENCED, TILDE], ids=["backticks", "tildes"])
def test_tc22_a_line_in_a_fence_counts_for_no_story(tmp_path, fence):
    text = doc(story(1, line=fence))
    assert stories_reported(lint(project(tmp_path, text))) == ["1"]


def test_tc22_a_line_in_the_previous_story_does_not_count_for_the_next(tmp_path):
    text = doc(story(1), story(2, line=None))
    assert stories_reported(lint(project(tmp_path, text))) == ["2"]


# --------------------------------------------------------------------------- TC-23 (3.5)


def test_tc23_no_requirements_document_gives_no_finding_and_the_rest_of_lint_runs(tmp_path):
    root = project(tmp_path, None)
    obj = lint(root)
    assert justifications(obj) == []
    for key in TODAY_KEYS:
        assert key in obj, (key, sorted(obj))
    assert exit_code(root) == 0


def test_tc23_a_document_with_no_stories_gives_no_finding_and_the_rest_of_lint_runs(tmp_path):
    root = project(tmp_path, "# Feature: Sim\n\n- 1.1 WHEN a THE SYSTEM SHALL b\n")
    obj = lint(root)
    assert justifications(obj) == []
    for key in TODAY_KEYS:
        assert key in obj, (key, sorted(obj))
    assert exit_code(root) == 0


# --------------------------------------------------------------------------- TC-24 (3.6)


def test_tc24_json_lists_each_finding_with_story_and_title(tmp_path):
    root = project(tmp_path, doc(story(1, "First", line=None), story(2), story(3, "Third", line="**Without it:** **")))
    found = justifications(lint(root))
    assert [(str(e["story"]), e["title"]) for e in found] == [("1", "First"), ("3", "Third")], found


def test_tc24_every_existing_key_equals_the_output_without_requirements(tmp_path):
    root = project(tmp_path, doc(story(1, line=None), story(2)))
    with_reqs = lint(root)
    assert stories_reported(with_reqs) == ["1"]
    (root / "specs" / SPEC / "requirements.md").unlink()
    without = lint(root)
    for key in TODAY_KEYS:
        assert key in with_reqs, (key, sorted(with_reqs))
        assert with_reqs[key] == without[key], (key, with_reqs[key], without[key])


# --------------------------------------------------------------------------- TC-25 (3.7)

pytestmark_workflow = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

STORIES_WITHOUT_LINES = (
    "# Feature: Demo\n\n## User Stories\n\n"
    "### Story 1: Add\n\n**As a** user **I want** add **So that** sums.\n\n"
    "**Acceptance Criteria:**\n\n- 1.1 WHEN a is added to b THE SYSTEM SHALL return the sum\n\n"
    "### Story 2: Subtract\n\n**As a** user **I want** subtract **So that** differences.\n\n"
    "**Acceptance Criteria:**\n\n- 2.1 WHEN a is subtracted from b THE SYSTEM SHALL return the difference\n"
)


@pytestmark_workflow
def test_tc25_only_justification_findings_do_not_refuse_the_launch(tmp_path):
    from test_unattended_smoke import CALC_GOOD, GOOD_CALC, add_to_main_and_integration, demo_project, run_workflow

    root = demo_project(tmp_path, CALC_GOOD)
    add_to_main_and_integration(root, {"specs/demo/requirements.md": STORIES_WITHOUT_LINES}, "stories with no justification")
    target = h.integration_worktree(root)

    # the only lint findings are justification findings, and they fail `ck lint`
    code, obj, err = h.ck_json(target, "lint", "demo", "--json")
    assert obj is not None, f"ck lint --json printed no JSON (exit {code}): {err}"
    assert sorted(stories_reported(obj)) == ["1", "2"], obj.get("justifications")
    assert code != 0
    assert not obj.get("verifyScope"), obj.get("verifyScope")

    scenario = {"files": {
        "2": {"calc.py": GOOD_CALC, "specs/demo/tasks.md": {"markDone": "2"}},
        "3": {"sub_ok.py": "SUB = True\n", "specs/demo/tasks.md": {"markDone": "3"}},
        "7": {"seven.py": "SEVEN = 7\n", "specs/demo/tasks.md": {"markDone": "7"}},
        "8": {"eight.py": "EIGHT = 8\n", "specs/demo/tasks.md": {"markDone": "8"}},
    }}
    run = run_workflow(tmp_path, root, "demo", scenario)
    assert run.ck("plan"), "the workflow never ran ck plan"
    started = run.agent_calls(r"^task \d+")
    assert started, f"no task agent started: result={json.dumps(run.result)[:600]}"
    text = json.dumps(run.result).lower()
    assert "refus" not in text, f"the launch was refused: {json.dumps(run.result)[:600]}"
