"""Red-team for Story 3's SHALL NOT clauses (run-principles 3.4 and 3.7), written from the requirements only.

3.4: a `**Without it:**` line outside a story's own section, or inside a fenced code block, SHALL NOT count for any
story. Each case gives a story whose only marker sits in a place that must not count, and expects `ck lint` (the real
CLI, real files) to report that story and exit non-zero. `test_positive_control_*` fixes the same text by putting the
marker in the story's own section outside any fence, so a lint that never reports cannot satisfy both.

3.7: when the only lint findings are justification findings, the system SHALL NOT refuse a `/spec:implement` launch.
The real `ck lint` and the real `ck plan` read a real project holding many unjustified stories; the real plan answer
is what the real workflow (`spec-implement.js` through the bridge) launches from.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from landing_bridge import bridge  # noqa: F401,E402  (module-scoped fixture)
from test_lint import block, ck, write_spec  # noqa: E402

pytestmark = pytest.mark.usefixtures("no_network")

LINE = "**Without it:** the thing breaks for someone concrete."


def head(num, title, *, line=None):
    out = f"### Story {num}: {title}\n\n**As a** user **I want** a **So that** b.\n\n"
    if line:
        out += line + "\n\n"
    return out


def crit(num):
    return f"**Acceptance Criteria:**\n\n- {num}.1 WHEN x, THE SYSTEM SHALL y.\n\n"


def spec(root: Path, body: str, *, newline="\n"):
    write_spec(root, block(1, "Core", files=["src/a.py"]))
    text = "# Requirements\n\n" + body
    (root / "specs/sim/requirements.md").write_bytes(text.replace("\n", newline).encode())


def lint(root: Path):
    res = ck(root, "lint", "sim", "--json")
    return res, json.loads(res.output[res.output.index("{"):])


def assert_reported(root: Path, *expected):
    res, data = lint(root)
    found = [(f["story"], f["title"]) for f in data["justifications"]]
    assert found == list(expected), data["justifications"]
    assert res.exit_code != 0, res.output
    text = ck(root, "lint", "sim")
    assert text.exit_code != 0, text.output


def assert_clean(root: Path):
    res, data = lint(root)
    assert data["justifications"] == [], data["justifications"]
    assert res.exit_code == 0, res.output


# --------------------------------------------------------------------------- 3.4: positive controls

def test_positive_control_the_marker_in_its_own_section_counts(tmp_path):
    spec(tmp_path, "## User Stories\n\n" + head(1, "Own", line=LINE) + crit(1))
    assert_clean(tmp_path)


def test_positive_control_a_story_with_only_a_fenced_marker_is_reported(tmp_path):
    spec(tmp_path, "## User Stories\n\n" + head(1, "Bare") + f"```\n{LINE}\n```\n\n" + crit(1))
    assert_reported(tmp_path, ("1", "Bare"))


# --------------------------------------------------------------------------- 3.4: fences

@pytest.mark.parametrize("opener,closer", [
    ("```", "```"), ("~~~", "~~~"), ("````", "````"), ("   ```", "```"), ("```python", "```"),
    ("```", "~~~"),        # a closer of the other character does not close
    ("````", "```"),      # a closer shorter than the opener does not close
    ("~~~~~", "~~~"),
])
def test_a_marker_inside_any_fence_form_counts_for_no_story(tmp_path, opener, closer):
    spec(tmp_path, "## User Stories\n\n" + head(1, "Fenced") + f"{opener}\n{LINE}\n{closer}\n\n" + crit(1))
    assert_reported(tmp_path, ("1", "Fenced"))


def test_a_marker_after_a_too_short_closer_is_still_inside_the_fence(tmp_path):
    """Opened with four backticks, 'closed' with three: the fence is still open, so the marker after it is fenced."""
    body = "## User Stories\n\n" + head(1, "Long") + f"````\ncode\n```\n{LINE}\n\n" + crit(1)
    spec(tmp_path, body)
    assert_reported(tmp_path, ("1", "Long"))


def test_a_marker_in_a_nested_fence_inside_a_longer_fence(tmp_path):
    inner = f"````\n```\n{LINE}\n```\n````\n"
    spec(tmp_path, "## User Stories\n\n" + head(1, "Nested") + inner + "\n" + crit(1))
    assert_reported(tmp_path, ("1", "Nested"))


def test_a_marker_after_an_inner_fence_closes_but_before_the_outer_one_does(tmp_path):
    body = head(1, "Outer") + f"~~~~\n~~~\ninner closes nothing\n{LINE}\n~~~~\n\n" + crit(1)
    spec(tmp_path, "## User Stories\n\n" + body)
    assert_reported(tmp_path, ("1", "Outer"))


def test_an_unclosed_fence_swallows_every_later_marker(tmp_path):
    body = head(1, "Open") + "```\nnever closed\n\n" + LINE + "\n\n" + crit(1)
    spec(tmp_path, "## User Stories\n\n" + body)
    assert_reported(tmp_path, ("1", "Open"))


def test_an_unclosed_fence_in_one_story_does_not_hide_a_marker_in_an_earlier_story(tmp_path):
    body = head(1, "Good", line=LINE) + crit(1) + head(2, "Open") + "```\nnever closed\n" + LINE + "\n"
    spec(tmp_path, "## User Stories\n\n" + body)
    assert_reported(tmp_path, ("2", "Open"))


def test_a_fence_that_closes_lets_a_later_real_marker_count(tmp_path):
    body = head(1, "After") + f"```\n{LINE}\n```\n\n" + LINE + "\n\n" + crit(1)
    spec(tmp_path, "## User Stories\n\n" + body)
    assert_clean(tmp_path)


# --------------------------------------------------------------------------- 3.4: story headings inside fences

def test_a_story_heading_inside_a_fence_does_not_capture_a_later_marker(tmp_path):
    """The fenced heading opens no section, so the real marker below the fence still belongs to Story 1."""
    body = head(1, "Real") + "```\n### Story 2: Ghost\n```\n\n" + LINE + "\n\n" + crit(1)
    spec(tmp_path, "## User Stories\n\n" + body)
    assert_clean(tmp_path)


def test_a_fenced_story_heading_is_not_itself_a_story_to_report(tmp_path):
    body = head(1, "Real", line=LINE) + "```\n### Story 2: Ghost\n```\n\n" + crit(1)
    spec(tmp_path, "## User Stories\n\n" + body)
    assert_clean(tmp_path)


def test_a_marker_fenced_under_a_fenced_heading_counts_for_nobody(tmp_path):
    body = head(1, "Real") + f"```\n### Story 2: Ghost\n{LINE}\n```\n\n" + crit(1)
    spec(tmp_path, "## User Stories\n\n" + body)
    assert_reported(tmp_path, ("1", "Real"))


def test_a_fenced_heading_cannot_make_the_next_stories_marker_count_for_the_wrong_story(tmp_path):
    """Story 1 is bare; Story 2 has its line. A fenced heading in story 1 must not move Story 2's line to Story 1."""
    body = head(1, "Bare") + "```\n### Story 3: Ghost\n```\n\n" + crit(1) + head(2, "Fine", line=LINE) + crit(2)
    spec(tmp_path, "## User Stories\n\n" + body)
    assert_reported(tmp_path, ("1", "Bare"))


# --------------------------------------------------------------------------- 3.4: outside the story's own section

def test_a_marker_above_the_first_story_counts_for_no_story(tmp_path):
    spec(tmp_path, f"## User Stories\n\n{LINE}\n\n" + head(1, "First") + crit(1))
    assert_reported(tmp_path, ("1", "First"))


def test_a_marker_in_the_preamble_before_the_stories_heading(tmp_path):
    spec(tmp_path, f"{LINE}\n\n## User Stories\n\n" + head(1, "First") + crit(1))
    assert_reported(tmp_path, ("1", "First"))


def test_a_marker_in_an_earlier_story_does_not_cover_a_later_one(tmp_path):
    body = head(1, "Has", line=LINE) + crit(1) + head(2, "Lacks") + crit(2)
    spec(tmp_path, "## User Stories\n\n" + body)
    assert_reported(tmp_path, ("2", "Lacks"))


def test_a_marker_after_the_last_story_under_another_heading(tmp_path):
    body = head(1, "Last") + crit(1) + f"## Out of Scope\n\n{LINE}\n"
    spec(tmp_path, "## User Stories\n\n" + body)
    assert_reported(tmp_path, ("1", "Last"))


def test_a_marker_under_a_later_level_two_heading_between_stories(tmp_path):
    body = head(1, "One") + crit(1) + f"## Notes\n\n{LINE}\n\n" + head(2, "Two", line=LINE) + crit(2)
    spec(tmp_path, "## User Stories\n\n" + body)
    assert_reported(tmp_path, ("1", "One"))





def test_a_marker_in_a_lookalike_story_heading_form_is_outside_every_section(tmp_path):
    """`#### Story 2:` and `### story 2:` are not story headings; the marker below them belongs to the story above."""
    body = head(1, "One") + crit(1) + f"### Not a story\n\n{LINE}\n"
    spec(tmp_path, "## User Stories\n\n" + body)
    assert_reported(tmp_path, ("1", "One"))


# --------------------------------------------------------------------------- 3.4: HTML comments

def test_a_marker_inside_a_one_line_html_comment(tmp_path):
    spec(tmp_path, "## User Stories\n\n" + head(1, "Commented") + f"<!-- {LINE} -->\n\n" + crit(1))
    assert_reported(tmp_path, ("1", "Commented"))


def test_a_marker_on_its_own_line_inside_a_multiline_html_comment(tmp_path):
    spec(tmp_path, "## User Stories\n\n" + head(1, "Commented") + f"<!--\n{LINE}\n-->\n\n" + crit(1))
    assert_reported(tmp_path, ("1", "Commented"))


# --------------------------------------------------------------------------- 3.4: line endings

def test_crlf_a_marker_in_a_fence_still_counts_for_no_story(tmp_path):
    spec(tmp_path, "## User Stories\n\n" + head(1, "Crlf") + f"```\n{LINE}\n```\n\n" + crit(1), newline="\r\n")
    assert_reported(tmp_path, ("1", "Crlf"))


def test_crlf_a_fence_still_closes_so_the_real_marker_after_it_counts(tmp_path):
    body = head(1, "Crlf") + f"```\n{LINE}\n```\n\n" + LINE + "\n\n" + crit(1)
    spec(tmp_path, "## User Stories\n\n" + body, newline="\r\n")
    assert_clean(tmp_path)


def test_crlf_a_story_heading_in_a_fence_still_opens_no_section(tmp_path):
    body = head(1, "Crlf") + "```\n### Story 2: Ghost\n```\n\n" + LINE + "\n\n" + crit(1)
    spec(tmp_path, "## User Stories\n\n" + body, newline="\r\n")
    assert_clean(tmp_path)


def test_crlf_a_marker_outside_its_section_still_counts_for_no_story(tmp_path):
    body = head(1, "One") + crit(1) + f"## Notes\n\n{LINE}\n"
    spec(tmp_path, "## User Stories\n\n" + body, newline="\r\n")
    assert_reported(tmp_path, ("1", "One"))





# --------------------------------------------------------------------------- 3.7: launch with only justification findings

def _project(tmp_path: Path, stories: int):
    from arbiter_helpers import arbiter_project

    body = "# Requirements: Demo\n\n## User Stories\n\n" + "".join(
        head(i, f"Story number {i}") + f"**Acceptance Criteria:**\n\n- {i}.1 WHEN a, THE SYSTEM SHALL b.\n\n"
        for i in range(1, stories + 1))
    return arbiter_project(tmp_path, requirements=body)


def _real(project, *args):
    from arbiter_helpers import ck_run

    return ck_run(project.root, *args)


def _launch(bridge, project, plan_extra, text_noise=None):  # noqa: F811
    deps = {"1": [], "2": ["1"]}
    entry = dict(plan_extra)
    if text_noise is not None:
        entry["text"] = text_noise
    return bridge.run({"deps": deps, "planSeq": [entry]},
                      {"deps": deps, "remaining": ["1", "2"], "root": str(project.root), "into": project.into,
                       "targetDir": str(project.target)},
                      slots=2, exec_ck=project.exec_ck)


def _real_plan_fields(project, stories):
    lint_res = _real(project, "lint", "demo", "--json")
    lint_data = json.loads(lint_res.stdout[lint_res.stdout.index("{"):])
    assert len(lint_data["justifications"]) == stories, lint_data.get("justifications")
    assert lint_res.returncode != 0
    plan_res = _real(project, "plan", "demo", "--json")
    assert plan_res.returncode == 0, plan_res.stdout + plan_res.stderr
    plan = json.loads(plan_res.stdout[plan_res.stdout.index("{"):])
    assert len(plan["justifications"]) == stories, sorted(plan)
    assert plan.get("verifyScope", []) == []
    return {k: plan[k] for k in ("justifications", "verifyScope", "cycles", "verifyCycles") if k in plan}


def test_many_justification_findings_alone_never_refuse_the_launch(tmp_path, bridge):  # noqa: F811
    project = _project(tmp_path, 60)
    fields = _real_plan_fields(project, 60)
    out = _launch(bridge, project, fields)
    assert out["result"].get("refused") in (None, False), out["result"].get("reason")
    assert out["result"]["merged"] == ["1", "2"]
    assert any(e.startswith("task 1") for e in out["events"])


def test_justification_findings_mixed_into_text_output_never_refuse_the_launch(tmp_path, bridge):  # noqa: F811
    project = _project(tmp_path, 12)
    fields = _real_plan_fields(project, 12)
    text = _real(project, "lint", "demo")
    assert text.returncode != 0 and "Story number 1" in text.stdout + text.stderr  # the real text output names them
    out = _launch(bridge, project, fields, text_noise=text.stdout + text.stderr)
    assert out["result"].get("refused") in (None, False), out["result"].get("reason")
    assert out["result"]["merged"] == ["1", "2"]


def test_a_blank_justification_alone_never_refuses_the_launch(tmp_path, bridge):  # noqa: F811
    from arbiter_helpers import arbiter_project

    body = ("# Requirements: Demo\n\n## User Stories\n\n" + head(1, "Blank", line="**Without it:** **") +
            "**Acceptance Criteria:**\n\n- 1.1 WHEN a, THE SYSTEM SHALL b.\n")
    project = arbiter_project(tmp_path, requirements=body)
    fields = _real_plan_fields(project, 1)
    out = _launch(bridge, project, fields)
    assert out["result"].get("refused") in (None, False)
    assert out["result"]["merged"] == ["1", "2"]


def test_positive_control_a_cycle_still_refuses_the_same_launch(tmp_path, bridge):  # noqa: F811
    project = _project(tmp_path, 3)
    fields = _real_plan_fields(project, 3)
    fields["cycles"] = [["1", "2", "1"]]
    out = _launch(bridge, project, fields)
    assert out["result"]["refused"] is True
