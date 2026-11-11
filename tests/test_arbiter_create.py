"""The /spec:create requirements prompt and the should-this-exist lens (run-principles Story 2 and Story 4), run through the
real plugin/workflows/spec-create.js under node with the shared fake agent (tests/js/fake_create.mjs). Prompt cases read the
prompts the workflow really sends; nothing of the project's own code is mocked."""

import re
import shutil

import pytest

from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

pytestmark = [pytest.mark.usefixtures("no_network"), pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")]

LENS = "review: should-exist"
REVISE = "revise spec + write review.md"


def prompt_of(out: dict, label: str) -> str:
    assert label in out["prompts"], f"the workflow never sent a {label!r} prompt; sent: {sorted(out['prompts'])}"
    return out["prompts"][label]


def test_tc16_requirements_prompt_asks_for_a_without_it_line_on_every_story(bridge):
    prompt = prompt_of(bridge.create(), "write requirements.md")
    assert "**Without it:**" in prompt
    assert re.search(r"every (user )?story", prompt, re.I), prompt
    assert re.search(r"PLAN\.md|plan item", prompt) and re.search(r"reason", prompt, re.I)


def test_tc17_requirements_prompt_gives_a_story_no_plan_item_covers_its_own_line(bridge):
    prompt = prompt_of(bridge.create(), "write requirements.md")
    assert "**Without it:**" in prompt
    assert re.search(r"no plan item|not (covered|in) (by )?(any|the) plan|plan (item )?(does not|doesn't) cover", prompt, re.I), prompt
    assert re.search(r"its own", prompt, re.I)


@pytest.mark.parametrize("mode", ["create", "review"])
def test_tc26_the_should_exist_lens_runs_and_attacks_each_justification(bridge, mode):
    out = bridge.create({"mode": mode})
    prompt = prompt_of(out, LENS)
    assert re.search(r"each story|every story", prompt, re.I), prompt
    assert re.search(r"justification|Without it", prompt, re.I), prompt
    assert re.search(r"cut", prompt, re.I)


@pytest.mark.parametrize("mode", ["create", "review"])
def test_tc32_review_md_says_so_when_the_lens_finds_nothing(bridge, mode):
    out = bridge.create({"mode": mode}, overrides={LENS: {"findings": []}})
    assert LENS in out["prompts"], "the should-exist lens never ran"
    revise = prompt_of(out, REVISE)
    assert re.search(r"should[- ]exist", revise, re.I), revise
    assert re.search(r"(nothing|no (story|cut|proposed cut)s?)[^.\n]*(cut|to cut|proposed)", revise, re.I), revise


def test_tc33_a_lens_that_fails_to_return_is_counted_and_coverage_is_partial(bridge):
    out = bridge.create(overrides={LENS: None})
    assert LENS in out["prompts"], "the should-exist lens never ran"
    assert out["result"]["lensesLost"] == 1
    assert any("coverage is partial" in line for line in out["logs"]), out["logs"]
