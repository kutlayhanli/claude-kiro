"""Red-team for Story 2's SHALL NOT clause (run-principles 2.5), through the real `ck lint` CLI.

2.5: a story with a `**Without it:**` line has its acceptance criteria inherit that reason, and the system SHALL NOT
require a line per criterion. Every case below gives a story that HAS its own line and criteria that do not, and
expects `ck lint` to report no justification finding. `test_positive_control_*` proves the check exists by fixing the
same text with the story's line removed: a lint that never reports (or never exists) cannot satisfy both.
"""

import json
import re
from pathlib import Path

import pytest

from test_lint import block, ck, write_spec

pytestmark = pytest.mark.usefixtures("no_network")

REPO = Path(__file__).resolve().parent.parent
REAL = REPO / "specs/run-principles/requirements.md"

LINE = "**Without it:** the thing breaks for someone concrete."


def story(num, title, *, line=LINE, criteria=None, before="", after=""):
    crit = criteria if criteria is not None else [f"- {num}.1 WHEN x happens, THE SYSTEM SHALL do y.", f"- {num}.2 WHEN z, THE SYSTEM SHALL w."]
    out = f"### Story {num}: {title}\n\n**As a** user **I want** a **So that** b.\n\n"
    if line is not None:
        out += line + "\n\n"
    out += before + "**Acceptance Criteria:**\n\n" + "\n".join(crit) + "\n" + after + "\n"
    return out


def spec(root: Path, *stories: str, newline="\n"):
    write_spec(root, block(1, "Core", files=["src/a.py"]))
    text = "# Requirements\n\n## User Stories\n\n" + "".join(stories)
    (root / "specs/sim/requirements.md").write_bytes(text.replace("\n", newline).encode())


def lint(root: Path):
    res = ck(root, "lint", "sim", "--json")
    data = json.loads(res.output)
    return res, data


def assert_clean(root: Path):
    res, data = lint(root)
    assert data["justifications"] == [], data["justifications"]
    assert res.exit_code == 0, res.output
    text = ck(root, "lint", "sim")
    assert text.exit_code == 0, text.output
    assert "Without it" not in text.output


def assert_reported(root: Path, num: str, title: str):
    res, data = lint(root)
    found = [(f["story"], f["title"]) for f in data["justifications"]]
    assert found == [(num, title)], data["justifications"]
    assert res.exit_code != 0


def test_positive_control_a_story_without_its_line_is_reported(tmp_path):
    spec(tmp_path, story(1, "Bare", line=None))
    assert_reported(tmp_path, "1", "Bare")


def test_positive_control_criteria_do_not_stand_in_for_the_story_line(tmp_path):
    crit = [f"- 1.1 WHEN a, THE SYSTEM SHALL b. {LINE}"]
    spec(tmp_path, story(1, "Bare", line=None, criteria=crit))
    assert_reported(tmp_path, "1", "Bare")


# --------------------------------------------------------------------------- criteria with no line of their own

def test_many_criteria_none_with_a_line(tmp_path):
    spec(tmp_path, story(1, "Wide", criteria=[f"- 1.{i} WHEN a{i}, THE SYSTEM SHALL b{i}." for i in range(1, 61)]))
    assert_clean(tmp_path)


def test_a_single_criterion_and_a_story_with_no_criteria_list(tmp_path):
    spec(tmp_path, story(1, "One", criteria=["- 1.1 WHEN a, THE SYSTEM SHALL b."]), story(2, "None", criteria=[]))
    assert_clean(tmp_path)


def test_each_story_line_covers_only_its_own_criteria_but_all_have_one(tmp_path):
    spec(tmp_path, story(1, "A"), story(2, "B", line="**Without it:** other reason"), story(3, "C", line="**Without it:** n/a"))
    assert_clean(tmp_path)


def test_wrapped_multiline_criteria_do_not_need_a_line(tmp_path):
    crit = ["- 1.1 WHEN a long criterion wraps,", "  THE SYSTEM SHALL keep going", "  across several lines.", "", "- 1.2 WHEN b, THE SYSTEM SHALL c."]
    spec(tmp_path, story(1, "Wrapped", criteria=crit))
    assert_clean(tmp_path)


def test_criterion_text_that_mentions_the_marker_inline(tmp_path):
    crit = ["- 1.1 WHEN writing, THE SYSTEM SHALL put a `**Without it:**` line on every story.", "- 1.2 THE SYSTEM SHALL NOT require a **Without it:** per criterion."]
    spec(tmp_path, story(1, "Mentions", criteria=crit))
    assert_clean(tmp_path)


# --------------------------------------------------------------------------- criteria carrying their own partial markers

@pytest.mark.parametrize("partial", [
    "*Without it:* x",
    "**without it:** x",
    "Without it: x",
    "**Without it**: x",
    "**Without it:**",
    "**Without it:**   ",
    "**Without it:** **",
    "**Without it:** _ _",
    "**Without it:** <!-- -->",
    "** Without it: **",
    "**Without  it:** x",
    "**Without it:** x​",
])
def test_criteria_with_partial_or_empty_markers_are_not_findings(tmp_path, partial):
    """The story's own line is valid; a criterion that tries a marker and fails must not turn into a finding."""
    crit = [f"- 1.1 WHEN a, THE SYSTEM SHALL b. {partial}", f"  {partial}", f"- 1.2 WHEN c, THE SYSTEM SHALL d.", f"    {partial}"]
    spec(tmp_path, story(1, "Partial", criteria=crit))
    assert_clean(tmp_path)


@pytest.mark.parametrize("partial", ["*Without it:* x", "**Without it**: x", "Without it: x", "**without it:** x"])
def test_partial_markers_do_not_replace_the_story_line(tmp_path, partial):
    spec(tmp_path, story(1, "Partial", line=None, criteria=[f"- 1.1 WHEN a, THE SYSTEM SHALL b.", partial]))
    assert_reported(tmp_path, "1", "Partial")


def test_a_valid_line_on_one_criterion_does_not_demand_one_on_the_others(tmp_path):
    crit = ["- 1.1 WHEN a, THE SYSTEM SHALL b.", f"  {LINE}", "- 1.2 WHEN c, THE SYSTEM SHALL d.", "- 1.3 WHEN e, THE SYSTEM SHALL f."]
    spec(tmp_path, story(1, "Mixed", criteria=crit))
    res, data = lint(tmp_path)
    assert data["justifications"] == []
    assert res.exit_code == 0, res.output


# --------------------------------------------------------------------------- oddly numbered criteria

@pytest.mark.parametrize("crit", [
    ["- 0.0 WHEN a, THE SYSTEM SHALL b."],
    ["- 99.99 WHEN a, THE SYSTEM SHALL b."],
    ["- 1.1.1 WHEN a, THE SYSTEM SHALL b.", "- 1.1.2 WHEN c, THE SYSTEM SHALL d."],
    ["- 7.3 WHEN a, THE SYSTEM SHALL b.", "- 2.1 WHEN c, THE SYSTEM SHALL d."],
    ["- 1.1 WHEN a, THE SYSTEM SHALL b.", "- 1.1 WHEN a again, THE SYSTEM SHALL b again."],
    ["-1.1 WHEN a, THE SYSTEM SHALL b.", "* 1.2 WHEN c, THE SYSTEM SHALL d.", "1.3 WHEN e, THE SYSTEM SHALL f.", "1) WHEN g, THE SYSTEM SHALL h."],
    ["- 1.01 WHEN a, THE SYSTEM SHALL b.", "- 1.1a WHEN c, THE SYSTEM SHALL d.", "- 1. WHEN e, THE SYSTEM SHALL f.", "- .5 WHEN g, THE SYSTEM SHALL h."],
    ["- ١.٢ WHEN a, THE SYSTEM SHALL b.", "- １.３ WHEN c, THE SYSTEM SHALL d."],
    ["- 1.1 WHEN a, THE SYSTEM SHALL b.", "- 1.2 WHEN c, THE SYSTEM SHALL d.", "- 1.2 WHEN d, THE SYSTEM SHALL e.", "- 1.10 WHEN f, THE SYSTEM SHALL g.", "- 1.9 WHEN h, THE SYSTEM SHALL i."],
    ["- 1.1 WHEN a, THE SYSTEM SHALL b.", "- 2.1 **Without it:**", "- Story 2 criterion"],
])
def test_oddly_numbered_criteria_still_inherit(tmp_path, crit):
    spec(tmp_path, story(1, "Odd", criteria=crit))
    assert_clean(tmp_path)


def test_criterion_that_looks_like_a_story_heading_or_a_numbered_story(tmp_path):
    """A criterion-looking line must not be read as a new story that then lacks its own line."""
    crit = ["- 1.1 WHEN a, THE SYSTEM SHALL b.", "- 2.1 WHEN c, THE SYSTEM SHALL d.", "- Story 9: not a heading", "- **Story 9:** nor this"]
    spec(tmp_path, story(1, "Looks", criteria=crit))
    assert_clean(tmp_path)


# --------------------------------------------------------------------------- a criterion-looking line inside a story that has a line

def test_criterion_lines_before_and_after_the_story_line(tmp_path):
    before = "- 1.0 WHEN early, THE SYSTEM SHALL cope.\n\n"
    after = "\n- 1.9 WHEN late, THE SYSTEM SHALL cope.\n- 1.10 WHEN later, THE SYSTEM SHALL cope.\n"
    spec(tmp_path, story(1, "Around", before=before, after=after))
    assert_clean(tmp_path)


def test_criteria_between_two_stories_belong_to_neither_for_justification(tmp_path):
    spec(tmp_path, story(1, "First"), story(2, "Second", line=None))
    assert_reported(tmp_path, "2", "Second")


def test_a_story_keeps_its_line_when_criteria_come_first(tmp_path):
    out = "### Story 1: Flipped\n\n**Acceptance Criteria:**\n\n- 1.1 WHEN a, THE SYSTEM SHALL b.\n\n" + LINE + "\n"
    spec(tmp_path, out)
    assert_clean(tmp_path)


def test_line_ending_in_a_criterion_table_or_quote_form(tmp_path):
    crit = ["> - 1.1 WHEN a, THE SYSTEM SHALL b.", "| 1.2 | WHEN c, THE SYSTEM SHALL d. |", "    - 1.3 WHEN e, THE SYSTEM SHALL f."]
    spec(tmp_path, story(1, "Forms", criteria=crit))
    assert_clean(tmp_path)


# --------------------------------------------------------------------------- encoding and line-ending tricks

def test_crlf_line_endings(tmp_path):
    spec(tmp_path, story(1, "Crlf"), story(2, "Crlf two"), newline="\r\n")
    assert_clean(tmp_path)


def test_crlf_story_without_a_line_is_still_reported(tmp_path):
    spec(tmp_path, story(1, "Crlf", line=None), newline="\r\n")
    res, data = lint(tmp_path)
    assert [f["story"] for f in data["justifications"]] == ["1"]


def test_unicode_titles_and_reasons_with_unnumbered_criteria(tmp_path):
    spec(tmp_path, story(1, "Grüße ☃", line="**Without it:** 用户会丢失数据", criteria=["- 1.1 WHEN é, THE SYSTEM SHALL ü."]))
    assert_clean(tmp_path)


# --------------------------------------------------------------------------- the system's own / real data fed back

def real_stories():
    text = REAL.read_text()
    parts = re.split(r"(?m)^(?=### Story )", text)
    return [p for p in parts[1:]]


def test_the_real_requirements_of_this_spec_have_no_justification_finding(tmp_path):
    write_spec(tmp_path, block(1, "Core", files=["src/a.py"]))
    (tmp_path / "specs/sim/requirements.md").write_text(REAL.read_text())
    assert_clean(tmp_path)


def test_real_story_2_block_alone_is_clean(tmp_path):
    story2 = next(s for s in real_stories() if s.startswith("### Story 2:"))
    assert "**Without it:**" in story2 and "SHALL NOT require a line per criterion" in story2
    spec(tmp_path, story2)
    assert_clean(tmp_path)


def test_every_real_story_alone_is_clean_and_each_is_reported_once_its_line_is_removed(tmp_path):
    stories = real_stories()
    assert len(stories) >= 3
    for i, s in enumerate(stories):
        root = tmp_path / f"s{i}"
        root.mkdir()
        spec(root, s)
        assert_clean(root)
        stripped = re.sub(r"(?m)^\*\*Without it:\*\*.*(?:\n[ \t]+\S.*)*\n", "", s, count=1)
        assert stripped != s
        root2 = tmp_path / f"t{i}"
        root2.mkdir()
        spec(root2, stripped)
        num, title = re.match(r"### Story (\S+?):\s*(.*)", s).groups()
        assert_reported(root2, num, title.strip())


def test_real_story_2_with_criteria_replicated_many_times(tmp_path):
    story2 = next(s for s in real_stories() if s.startswith("### Story 2:"))
    spec(tmp_path, story2 + story2.replace("### Story 2:", "### Story 20:"))
    assert_clean(tmp_path)


def test_the_json_output_of_lint_fed_back_as_a_story_changes_nothing(tmp_path):
    """The system's own output as input: a finding's message pasted into a criterion must not count as a line."""
    spec(tmp_path, story(1, "Bare", line=None))
    _, data = lint(tmp_path)
    message = data["justifications"][0]["message"]
    spec(tmp_path, story(1, "Bare", line=None, criteria=[f"- 1.1 WHEN a, THE SYSTEM SHALL b. {message}", f"  {json.dumps(data['justifications'])}"]))
    assert_reported(tmp_path, "1", "Bare")
