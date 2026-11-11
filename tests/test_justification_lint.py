"""Justification lint through the real CLI and at launch (run-principles Task 12: TC-13, TC-18 to TC-25).

Every case runs the real `ck` (`python -m claude_kiro.cli`) over real files in a real git project. TC-25 runs the real
`plugin/workflows/spec-implement.js` under Node against a real project with real `ck plan`, with scripted agents only.
Nothing of the project's own is mocked, and project modules are imported inside test bodies so the file collects
before the feature exists. Before it exists the cases fail on the missing `justifications` key, the missing `warnings`
entry, or the exit code that stays 0.
"""

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Optional

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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


# --------------------------------------------------------------------------- properties (P-5 to P-8, P-41)

PROPERTIES = {
    "P-5": "test_p5_story_reported_exactly_when_no_valid_line_in_its_own_section",
    "P-6": "test_p6_any_non_empty_content_gives_no_finding",
    "P-7": "test_p7_justification_checks_never_change_another_lint_key",
    "P-8": "test_p8_only_a_story_12_finding_blocks_launch",
    "P-41": "test_p41_warnings_and_not_checked_never_change_the_verdict",
}

PURE = dict(derandomize=True, max_examples=200, database=None, deadline=None)
LABEL = "**Without it:**"
EMPTY_CONTENT = ["", " ", "   ", "\t", "**", "****", "_ _", "__", "<!-- -->", "<!--  -->", "** **"]
NONEMPTY_CONTENT = ["the feature is missing", "n/a", "nice to have", "x", "0", "\u00e9", "\u7406\u7531",
                    "https://example.com/a?b=1", "_italic_ reason", "**bold** reason"]
NEAR_MISSES = ["*Without it:* x", "**without it:** x", "Without it: x", "**Without it**: x", "**Without  it:** x",
               "some prose", "", "**Without it?** x"]
FENCES = ["```", "~~~", "  ```", "  ~~~", "````"]


def _line(content: str) -> str:
    return f"{LABEL} {content}"


@st.composite
def item_text(draw) -> str:
    """One chunk of lines placed in a section: a marker line (valid or empty), a near miss, a fenced chunk or an unclosed fence."""
    kind = draw(st.sampled_from(["valid", "empty", "label_alone", "near", "fenced_valid", "fenced_heading",
                                 "unclosed_valid", "unclosed_heading"]))
    if kind == "valid":
        return _line(draw(st.sampled_from(NONEMPTY_CONTENT)))
    if kind == "empty":
        return _line(draw(st.sampled_from(EMPTY_CONTENT)))
    if kind == "label_alone":
        return LABEL
    if kind == "near":
        return draw(st.sampled_from(NEAR_MISSES))
    fence = draw(st.sampled_from(FENCES))
    inner = _line("inside a fence") if kind.endswith("valid") else "### Story 99: Inside a fence"
    if kind.startswith("unclosed"):
        return f"{fence}\n{inner}"
    return f"{fence}\n{inner}\n{fence.strip()}"


ITEMS = st.lists(item_text(), max_size=4)


@st.composite
def requirements_doc(draw) -> str:
    """A requirements document: preamble chunks, 0 to 4 story sections, each with chunks and an optional `## Notes` tail."""
    out = ["# Requirements: Sim", "", "## User Stories", ""]
    out += draw(ITEMS)
    for n in range(1, draw(st.integers(0, 4)) + 1):
        out += ["", f"### Story {n}: Title {n}", "", STORY_BODY.strip(), ""]
        out += draw(ITEMS)
        out += ["", "**Acceptance Criteria:**", "", f"- {n}.1 WHEN x, THE SYSTEM SHALL y."]
        if draw(st.booleans()):
            out += ["", "## Notes", ""] + draw(ITEMS)
    return "\n".join(out) + "\n"


_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_STORY = re.compile(r"^### Story (\d+): (.*)$")
_HEADING = re.compile(r"^#{1,6} ")
_MARKER = re.compile(r"^\*\*Without it:\*\*(.*)$")


def _valid_line(line: str) -> bool:
    m = _MARKER.match(line)
    if not m:
        return False
    rest = re.sub(r"<!--.*?-->", "", m.group(1))
    return bool(re.sub(r"[\s*_]", "", rest))


def oracle(text: str) -> List[str]:
    """Brute force: walk the lines, skip fenced ones, and name each story with no valid line before the next heading."""
    fence = None
    current = None
    order: List[str] = []
    has = {}
    for line in text.splitlines():
        if fence:
            close = re.match(r"^ {0,3}(`{3,}|~{3,})\s*$", line)
            if close and close.group(1)[0] == fence[0] and len(close.group(1)) >= len(fence):
                fence = None
            continue
        opened = _FENCE_OPEN.match(line)
        if opened:
            fence = opened.group(1)
            continue
        story_heading = _STORY.match(line)
        if story_heading:
            current = story_heading.group(1)
            order.append(current)
            has[current] = False
        elif _HEADING.match(line):
            current = None
        elif current is not None and _valid_line(line):
            has[current] = True
    return [n for n in order if not has[n]]


@settings(**PURE)
@given(requirements_doc())
def test_p5_story_reported_exactly_when_no_valid_line_in_its_own_section(text):
    from claude_kiro.justifications import justification_findings

    reported = [str(f.story) for f in justification_findings(text)]
    assert reported == oracle(text), text


# --------------------------------------------------------------------------- P-6


@st.composite
def justified_doc(draw) -> str:
    out = ["# Requirements: Sim", "", "## User Stories", ""]
    for n in range(1, draw(st.integers(1, 4)) + 1):
        out += ["", f"### Story {n}: Title {n}", "", STORY_BODY.strip(), "", _line(draw(st.sampled_from(NONEMPTY_CONTENT))),
                "", "**Acceptance Criteria:**", ""]
        for k in range(1, draw(st.integers(1, 3)) + 1):
            out.append(f"- {n}.{k} WHEN x, THE SYSTEM SHALL y.")
            if draw(st.booleans()):
                out.append(_line(draw(st.sampled_from(NONEMPTY_CONTENT))))
    return "\n".join(out) + "\n"


@settings(**PURE)
@given(justified_doc())
def test_p6_any_non_empty_content_gives_no_finding(text):
    from claude_kiro.justifications import justification_findings

    assert justification_findings(text) == [], text


# --------------------------------------------------------------------------- P-7

_SRC_A = "src/mtr/a.py"
VERIFY_CYCLE_TEST = "from mtr.runner import go\n\n\ndef test_v():\n    assert go()\n"


def task_variant(kind: str) -> str:
    from test_lint import block

    if kind == "plain":
        return block(1, "Core", files=[_SRC_A])
    if kind == "cycle":
        return block(1, "One", files=[_SRC_A], deps="Task 2") + block(2, "Two", files=["src/mtr/b.py"], deps="Task 1")
    return (
        block(1, "V tests", track="test", files=["tests/test_v.py"], verify="uv run pytest -q tests/test_v.py",
              verifies="Task 2")
        + block(2, "Core", files=[_SRC_A], vby="Task 1", deps="Task 1")
        + block(3, "Runner", files=["src/mtr/runner.py"], deps="Task 2")
    )


@settings(derandomize=True, max_examples=200, database=None, deadline=None, suppress_health_check=list(HealthCheck))
@given(st.one_of(st.none(), requirements_doc()), st.sampled_from(["plain", "cycle", "verify-cycle"]))
def _p7_example(root: Path, requirements: Optional[str], kind: str) -> None:
    spec = root / "specs" / SPEC
    (spec / "tasks.md").write_text("# Implementation Tasks: Sim\n\n## Task Breakdown\n\n" + task_variant(kind))
    req = spec / "requirements.md"
    if requirements is None:
        req.unlink(missing_ok=True)
    else:
        req.write_text(requirements)
    with_reqs = lint(root)
    req.unlink(missing_ok=True)
    without = lint(root)
    assert "justifications" in with_reqs, sorted(with_reqs)
    for k in sorted(set(with_reqs) | set(without)):
        if k != "justifications":
            assert with_reqs.get(k) == without.get(k), (k, kind, with_reqs.get(k), without.get(k))


def test_p7_justification_checks_never_change_another_lint_key(tmp_path):
    root = project(tmp_path, None)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_v.py").write_text(VERIFY_CYCLE_TEST)
    _p7_example(root)


# --------------------------------------------------------------------------- P-8

SHARED = "tests/test_shared.py"
RUN = f"uv run pytest -q {SHARED}"


def scope_tasks(story12: bool) -> str:
    """Two test tasks sharing one suite: an unfiltered Verify is a Story 12 finding, a filtered one is not."""
    from test_lint import block

    first = RUN if story12 else f"{RUN} -k test_tc1_"
    return (
        block(1, "Alpha tests", track="test", files=[SHARED], verify=first, verifies="Task 3")
        + block(2, "Beta tests", track="test", files=[SHARED], verify=f"{RUN} -k test_tc2_", verifies="Task 4")
        + block(3, "Alpha core", files=["src/mtr/a.py"], verify=first, vby="Task 1", deps="Task 1")
        + block(4, "Beta core", files=["src/mtr/b.py"], vby="Task 2", deps="Task 2")
    )


def _plan(root: Path) -> dict:
    code, obj, err = h.ck_json(root, "plan", SPEC, "--json")
    assert obj is not None, f"ck plan --json printed no JSON (exit {code}): {err}"
    assert "verifyScope" in obj, sorted(obj)
    return obj


@settings(derandomize=True, max_examples=30, database=None, deadline=None, suppress_health_check=list(HealthCheck))
@given(st.booleans(), st.booleans())
def _p8_example(root: Path, story12: bool, justification: bool) -> None:
    spec = root / "specs" / SPEC
    (spec / "tasks.md").write_text("# Implementation Tasks: Sim\n\n## Task Breakdown\n\n" + scope_tasks(story12))
    (spec / "requirements.md").write_text(doc(story(1), story(2, line=None if justification else "**Without it:** x")))
    plan = _plan(root)
    assert bool(plan["verifyScope"]) == story12, plan["verifyScope"]
    # a justification finding never adds to verifyScope: the same tasks with every story justified give the same list
    (spec / "requirements.md").write_text(doc(story(1), story(2)))
    assert _plan(root)["verifyScope"] == plan["verifyScope"]


def test_p8_only_a_story_12_finding_blocks_launch(tmp_path):
    root = h.spec_graph_project(
        tmp_path, tasks=[{"num": 1, "title": "Core", "files": ["src/mtr/a.py"]}],
        tests={SHARED: "def test_tc1_alpha():\n    assert True\n\n\ndef test_tc2_beta():\n    assert True\n"})
    _p8_example(root)


# --------------------------------------------------------------------------- P-41

ENTRY = st.dictionaries(st.sampled_from(["task", "suite", "testTask", "message", "story"]), st.text(max_size=8),
                        min_size=1, max_size=3)
ENTRIES = st.lists(ENTRY, max_size=3)


@st.composite
def lint_results(draw) -> dict:
    """A lint result: any mix of blocking keys, present or missing, possibly already carrying warnings or notChecked."""
    result = {}
    for k in ["cycles", "verifyCycles", "verifyScope", "justifications", "warnings", "notChecked"]:
        if draw(st.booleans()):
            result[k] = draw(ENTRIES)
    return result


@settings(**PURE)
@given(lint_results(), ENTRIES, ENTRIES)
def test_p41_warnings_and_not_checked_never_change_the_verdict(result, more_warnings, more_unchecked):
    from claude_kiro.lint_scope import lint_verdict

    before = lint_verdict(result)
    after = lint_verdict({**result, "warnings": list(result.get("warnings", [])) + more_warnings,
                          "notChecked": list(result.get("notChecked", [])) + more_unchecked})
    assert (after.exit_nonzero, after.launch_blockers) == (before.exit_nonzero, before.launch_blockers)
    # alone they never fail lint and never block a launch
    alone = lint_verdict({"warnings": result.get("warnings", []) + more_warnings,
                          "notChecked": result.get("notChecked", []) + more_unchecked})
    assert alone.exit_nonzero is False and tuple(alone.launch_blockers) == ()
    # a justification finding fails lint but never blocks a launch
    assert tuple(before.launch_blockers) == tuple(result.get("verifyScope") or ())
