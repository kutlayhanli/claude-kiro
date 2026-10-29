"""Red team of the agent-mode role parser's no-guess rule (requirement 4.4).

A marked description that matches no template SHALL be attributed to `other`, counted in the run's unmatched agents, and
the system SHALL NOT guess a role. Every case here is a near-miss of a template that a lenient parser (case folding,
`\\d`, `strip()`, `re.search`, `$` before a newline, prefix matching, int() of anything, falling back to the type or to the
workflow grammar) would turn into a real role. Each must land in `other` with `matched` false and no task, attempt or
round:

* through the seam `claude_kiro.analytics.parse_agent_description(description, agent_type)`, marked and marker-less, and
* through the real CLI: a fake Claude home -> `ck stats collect --agents` -> `ck stats show <id> --json`, where the agents
  count only under `other`.

Follows `tests/test_stats_redteam_roles.py`. Written from requirements.md and design.md's template table only; project
modules are imported inside the test bodies.
"""

from __future__ import annotations

import json
from typing import Dict, List, Tuple

import pytest

from stats_helpers import FakeClaudeHome, ToolAgentSpec, begin, described, fresh_env, land, load_record, spec_repo, stats

RUN = "ag_0123abcd"

# (id, template remainder after the marker) that match no template of the design's table.
OTHER_RESTS: List[Tuple[str, str]] = [
    # case folding
    ("upper-implement", "IMPLEMENT TASK 3"),
    ("title-implement", "Implement task 3"),
    ("title-task-word", "implement Task 3"),
    ("upper-retry", "implement task 3 RETRY 1"),
    ("title-revise", "Revise task 3 round 1"),
    ("upper-round", "revise task 3 ROUND 1"),
    ("title-review", "Review task 3 round 1"),
    ("upper-resolve", "RESOLVE task 3"),
    ("title-fix", "Fix 1"),
    ("upper-attempt", "fix task 3 ATTEMPT 1"),
    # lookalike scripts, ligatures and digits other than 0-9
    ("cyrillic-i-implement", "\u0456mplement task 3"),
    ("cyrillic-e-implement", "impl\u0435ment task 3"),
    ("cyrillic-e-review", "r\u0435view task 3 round 1"),
    ("cyrillic-o-resolve", "res\u043elve task 3"),
    ("cyrillic-a-task", "implement t\u0430sk 3"),
    ("long-s-resolve", "re\u017folve task 3"),
    ("fi-ligature-fix", "\ufb01x 1"),
    ("fi-ligature-fix-task", "\ufb01x task 3 attempt 1"),
    ("dotted-capital-i-review", "REV\u0130EW task 3 round 1"),
    ("dotless-i-review", "rev\u0131ew task 3 round 1"),
    ("fullwidth-letters", "\uff49mplement task 3"),
    ("fullwidth-task-digit", "implement task \uff13"),
    ("arabic-indic-task-digit", "implement task \u0663"),
    ("arabic-indic-retry-digit", "implement task 3 retry \u0661"),
    ("fullwidth-retry-digit", "implement task 3 retry \uff11"),
    ("arabic-indic-round-digit", "revise task 3 round \u0661"),
    ("devanagari-round-digit", "review task 3 round \u0967"),
    ("arabic-indic-fix-digit", "fix \u0661"),
    ("fullwidth-attempt-digit", "fix task 3 attempt \uff11"),
    ("superscript-digit", "implement task 3 retry \u00b2"),
    ("circled-digit", "fix \u2460"),
    # strip() and `$` traps
    ("trailing-space", "implement task 3 "),
    ("trailing-tab", "implement task 3\t"),
    ("trailing-newline", "implement task 3\n"),
    ("trailing-crlf", "implement task 3\r\n"),
    ("trailing-cr", "implement task 3\r"),
    ("trailing-nbsp", "implement task 3\u00a0"),
    ("trailing-newline-retry", "implement task 3 retry 1\n"),
    ("trailing-newline-review", "review task 3 round 1\n"),
    ("trailing-newline-fix", "fix 1\n"),
    ("trailing-newline-create", "create requirements\n"),
    ("leading-space", " implement task 3"),
    ("leading-newline", "\nimplement task 3"),
    ("leading-tab", "\timplement task 3"),
    ("double-space", "implement  task 3"),
    ("tab-separator", "implement\ttask 3"),
    ("newline-separator", "implement\ntask 3"),
    ("nbsp-separator", "implement\u00a0task 3"),
    ("ideographic-space-separator", "implement\u3000task 3"),
    ("empty", ""),
    ("only-space", " "),
    # zero-width and control characters
    ("zwsp-in-verb", "impl\u200bement task 3"),
    ("zwsp-after-verb", "implement\u200b task 3"),
    ("zwsp-after-task", "implement task\u200b 3"),
    ("zwsp-in-id", "implement task 3\u200b"),
    ("zwj-trailing", "review task 3 round 1\u200d"),
    ("bom-leading", "\ufeffimplement task 3"),
    ("soft-hyphen-review", "re\u00adview task 3 round 1"),
    ("rtl-override", "implement task 3\u202e"),
    ("nul-trailing", "implement task 3\x00"),
    ("nul-inside", "implement task\x003"),
    # prefixes, suffixes and repeats
    ("prefix-please", "implement task 3 please"),
    ("prefix-colon-text", "implement task 3: add the parser"),
    ("prefix-model-paren", "implement task 3 (opus)"),
    ("retry-twice", "implement task 3 retry 2 retry 3"),
    ("retry-then-text", "implement task 3 retry 2 now"),
    ("round-twice", "revise task 3 round 1 round 2"),
    ("review-then-text", "review task 3 round 1 again"),
    ("resolve-then-text", "resolve task 3 now"),
    ("resolve-round-then-text", "resolve task 3 round 1 now"),
    ("fix-then-text", "fix 1 red"),
    ("fix-attempt-then-text", "fix task 3 attempt 1 red"),
    ("two-templates-one-line", "implement task 3 review task 3 round 1"),
    ("two-templates-two-lines", "implement task 3\nreview task 3 round 1"),
    ("two-templates-reversed", "review task 3 round 1\nimplement task 3"),
    ("two-implements", "implement task 3\nimplement task 4"),
    ("embedded-in-sentence", "please implement task 3"),
    ("embedded-after-colon", "x: implement task 3"),
    ("marker-inside-rest", "[ck:ag_0123abcd] implement task 3"),
    # missing pieces and near-miss words
    ("bare-implement", "implement"),
    ("implement-task-no-id", "implement task"),
    ("implement-task-trailing-space-no-id", "implement task "),
    ("implement-no-task-word", "implement 3"),
    ("implement-glued", "implement task3"),
    ("implement-colon", "implement task: 3"),
    ("implement-hash", "implement task #3"),
    ("implement-plural", "implement tasks 3"),
    ("implement-ing", "implementing task 3"),
    ("implement-ed", "implemented task 3"),
    ("retry-without-count", "implement task 3 retry"),
    ("retry-word-count", "implement task 3 retry x"),
    ("retry-negative", "implement task 3 retry -1"),
    ("retry-signed", "implement task 3 retry +1"),
    ("retry-decimal", "implement task 3 retry 1.5"),
    ("retry-comma", "implement task 3 retry 1,2"),
    ("retry-hex", "implement task 3 retry 0x1"),
    ("retry-zero", "implement task 3 retry 0"),
    ("retry-zero-zeros", "implement task 3 retry 00"),
    ("retries-plural", "implement task 3 retries 1"),
    ("retry-glued", "implement task 3 retry1"),
    ("revise-no-round", "revise task 3"),
    ("revise-round-zero", "revise task 3 round 0"),
    ("revise-round-zeros", "revise task 3 round 00"),
    ("revise-round-word", "revise task 3 round x"),
    ("revise-round-negative", "revise task 3 round -1"),
    ("revise-retry-instead-of-round", "revise task 3 retry 1"),
    ("implement-round-instead-of-retry", "implement task 3 round 1"),
    ("review-no-round", "review task 3"),
    ("review-round-zero", "review task 3 round 0"),
    ("review-round-word", "review task 3 round one"),
    ("review-retry", "review task 3 retry 1"),
    ("resolve-round-zero", "resolve task 3 round 0"),
    ("resolve-no-task", "resolve"),
    ("resolve-retry", "resolve task 3 retry 1"),
    ("fix-zero", "fix 0"),
    ("fix-zeros", "fix 00"),
    ("fix-negative", "fix -1"),
    ("fix-word", "fix x"),
    ("fix-no-count", "fix"),
    ("fix-decimal", "fix 1.5"),
    ("fix-task-attempt-zero", "fix task 3 attempt 0"),
    ("fix-task-no-attempt", "fix task 3"),
    ("fix-task-attempt-word", "fix task 3 attempt x"),
    ("fix-task-round-instead-of-attempt", "fix task 3 round 1"),
    ("fix-attempt-without-task", "fix attempt 1"),
    ("fix-parens", "fix 1 (opus)"),
    ("fix-model-bracket", "fix 1 [opus]"),
    # path-shaped and punctuated task ids (task ids are [0-9A-Za-z_]+)
    ("task-id-parent-path", "implement task ../x"),
    ("task-id-slash", "implement task a/b"),
    ("task-id-numeric-slash", "implement task 3/4"),
    ("task-id-absolute", "implement task /etc/passwd"),
    ("task-id-windows", "implement task C:\\x"),
    ("task-id-tilde", "implement task ~"),
    ("task-id-dot", "implement task 3.1"),
    ("task-id-hyphen", "implement task 3-4"),
    ("task-id-negative", "implement task -1"),
    ("task-id-plus", "implement task +3"),
    ("task-id-comma", "implement task 3,4"),
    ("task-id-space-inside", "implement task 3 4"),
    ("task-id-star", "implement task *"),
    ("task-id-dollar", "implement task $3"),
    ("task-id-backtick", "implement task `3`"),
    ("task-id-quoted", "implement task \"3\""),
    ("task-id-semicolon", "implement task 3;"),
    ("task-id-colon-suffix", "implement task 3:"),
    ("task-id-percent", "implement task 3%"),
    ("review-task-id-path", "review task ../3 round 1"),
    ("resolve-task-id-path", "resolve task a/b"),
    ("revise-task-id-path", "revise task a/b round 1"),
    ("fix-task-id-path", "fix task a/b attempt 1"),
    ("task-id-unicode-letter", "implement task \u00e9"),
    ("task-id-fullwidth-letter", "implement task \uff21"),
    ("task-id-cyrillic", "implement task \u0430"),
    ("task-id-zero-width", "implement task 3\u200b4"),
    # workflow labels carrying a marker (the agent-mode grammar is not the workflow grammar)
    ("workflow-task", "task 3"),
    ("workflow-task-retry", "task 3 retry 1"),
    ("workflow-task-revise", "task 3 revise 1"),
    ("workflow-task-test", "task 3 [test]"),
    ("workflow-review", "review 3"),
    ("workflow-review-round", "review 3 round 1"),
    ("workflow-resolve", "resolve 3"),
    ("workflow-fix-parens", "fix 1 (x)"),
    ("workflow-fix-model", "fix 1 (x) [opus]"),
    ("workflow-land", "land 1"),
    ("workflow-merge", "merge 3"),
    ("workflow-report", "report"),
    ("workflow-final-gate", "final gate"),
    ("workflow-full-gate", "full gate"),
    ("workflow-plan", "plan"),
    ("workflow-cross-test", "cross-test 1"),
    ("workflow-recheck", "re-check 3"),
    ("workflow-write-requirements", "write requirements.md"),
    ("workflow-write-design", "write design.md"),
    ("workflow-review-lens", "review: fidelity"),
    ("workflow-refute-lens", "refute: fidelity"),
    ("workflow-consistency", "consistency check: pass 1"),
    # create steps: case, extension, the workflow label itself, truncation, extra words
    ("create-bare", "create"),
    ("create-space-only", "create "),
    ("create-upper-verb", "CREATE requirements"),
    ("create-title-verb", "Create requirements"),
    ("create-title-step", "create Requirements"),
    ("create-upper-step", "create REQUIREMENTS"),
    ("create-md-extension", "create requirements.md"),
    ("create-design-md", "create design.md"),
    ("create-test-plan-md", "create test-plan.md"),
    ("create-workflow-label-requirements", "create write requirements.md"),
    ("create-workflow-label-design", "create write design.md"),
    ("create-workflow-label-link", "create link tracks into tasks.md"),
    ("create-workflow-label-review", "create review: fidelity"),
    ("create-workflow-label-consistency", "create consistency check: pass 1"),
    ("create-workflow-label-snapshot", "create snapshot spec before revision"),
    ("create-workflow-label-cleanup", "create remove pre-revision snapshot"),
    ("create-title-lens", "create review Fidelity"),
    ("create-upper-lens", "create review FIDELITY"),
    ("create-unknown-lens", "create review style"),
    ("create-review-no-lens", "create review"),
    ("create-refute-no-lens", "create refute"),
    ("create-lens-only", "create fidelity"),
    ("create-lens-double-space", "create review  fidelity"),
    ("create-lens-trailing-space", "create review fidelity "),
    ("create-lens-colon", "create review: fidelity"),
    ("create-consistency-bare", "create consistency"),
    ("create-consistency-zero", "create consistency 0"),
    ("create-consistency-three", "create consistency 3"),
    ("create-consistency-two-numbers", "create consistency 1 2"),
    ("create-consistency-word", "create consistency one"),
    ("create-test-plan-space", "create test plan"),
    ("create-testplan-glued", "create testplan"),
    ("create-impl-track-space", "create impl track"),
    ("create-impl-track-trailing", "create impl-track "),
    ("create-tracks-plural", "create test-tracks"),
    ("create-link-tracks", "create link tracks"),
    ("create-revise-spec", "create revise spec"),
    ("create-snapshot-spec", "create snapshot spec"),
    ("create-cleanup-now", "create cleanup now"),
    ("create-then-text", "create requirements please"),
    ("create-two-steps", "create requirements design"),
    ("create-two-steps-lines", "create requirements\ncreate design"),
    ("create-prefix-step", "create requirement"),
    ("create-suffix-step", "create requirementss"),
    ("create-zwsp", "create requirements\u200b"),
    ("create-cyrillic-e", "creat\u0435 requirements"),
    ("create-implement-mix", "create implement task 3"),
    ("implement-create-mix", "implement create requirements"),
    # the system's own vocabulary fed back
    ("role-implementer", "implementer"),
    ("role-test-writer", "test-writer"),
    ("role-reviewer", "reviewer"),
    ("role-fixer", "fixer"),
    ("role-resolver", "resolver"),
    ("role-other", "other"),
    ("role-upper", "REVIEWER"),
    ("role-with-task", "reviewer task 3"),
    ("role-colon-task", "implementer: 3"),
    ("role-equals", "role=reviewer"),
    ("json-role", '{"role": "fixer"}'),
    ("json-role-task", '{"role": "implementer", "task": "3"}'),
    ("show-other-line", "other: 3 agents, 3.0 minutes, $0.45"),
    ("show-role-line", "implementer: 2 agents, 10.0 minutes, $1.20"),
    ("show-byrole", "byRole"),
    ("unmatched-agents-word", "unmatched agents"),
    ("matched-true", "matched: true"),
    ("long-garbage", "x" * 100_000),
    ("long-trailing-spaces", "implement task 3" + " " * 10_000),
    ("long-task-id-then-junk", "implement task " + "9" * 200 + " junk"),
]

AGENT_TYPES: List[object] = ["general-purpose", "Explore", "spec:implementer", "spec:test-writer", "spec:reviewer", "spec:fixer",
                             "spec:resolver", "ck-implementer", "ck-test-writer", "ck-reviewer", "ck-fixer", "ck-resolver", None, ""]


def _assert_other(result: object, shown: str) -> None:
    assert isinstance(result, dict), f"parse_agent_description({shown}) did not return a dict: {result!r}"
    assert result.get("role") == "other", f"4.4: {shown} guessed a role: {result.get('role')!r}"
    assert result.get("matched") is False, f"4.4: {shown} is `matched`: {result!r}"
    for key in ("task", "attempt", "round", "step"):
        assert result.get(key) is None, f"4.4: {shown} filled {key}: {result.get(key)!r}"


def _short(rest: str, agent_type: object, marked: bool) -> str:
    text = repr(rest) if len(repr(rest)) < 100 else repr(rest)[:80] + "..."
    return f"{text}, type {agent_type!r}, {'marked' if marked else 'marker-less'}"


@pytest.mark.parametrize("rest", [pytest.param(r, id=i) for i, r in OTHER_RESTS])
def test_44_marked_description_that_matches_no_template_is_other_for_every_agent_type(rest):
    """4.4 through the seam, marked, for each spec and non-spec agent type."""
    from claude_kiro.analytics import parse_agent_description

    for agent_type in AGENT_TYPES:
        _assert_other(parse_agent_description(described(RUN, rest), agent_type), _short(rest, agent_type, True))


@pytest.mark.parametrize("rest", [pytest.param(r, id=i) for i, r in OTHER_RESTS])
def test_44_markerless_rendition_of_the_same_near_miss_is_other_too(rest):
    """4.4: the marker-less rendition (no run id, 2.5) is held to the same grammar."""
    from claude_kiro.analytics import parse_agent_description

    for agent_type in ("general-purpose", "spec:implementer", "ck-reviewer", None):
        _assert_other(parse_agent_description(rest, agent_type), _short(rest, agent_type, False))


NON_STRING = [("none", None), ("int", 3), ("float", 1.5), ("bool", True), ("list", ["implement task 3"]), ("dict", {"d": "implement task 3"}),
              ("bytes", b"implement task 3")]


@pytest.mark.parametrize("description", [pytest.param(v, id=i) for i, v in NON_STRING])
def test_44_non_string_description_is_other_and_does_not_crash(description):
    """4.4: a description that is not text is no template; the parser must not raise."""
    from claude_kiro.analytics import parse_agent_description

    for agent_type in ("general-purpose", "spec:implementer", None, 5, ["spec:implementer"]):
        _assert_other(parse_agent_description(description, agent_type), f"{description!r}, type {agent_type!r}")


def test_44_huge_numbers_do_not_crash_the_parser_and_never_become_a_guessed_role():
    """4.4: a counter with thousands of digits (int() refuses past 4300) is handled without an exception."""
    from claude_kiro.analytics import ROLES, parse_agent_description

    huge = "9" * 6000
    for rest in (f"implement task 3 retry {huge}", f"revise task 3 round {huge}", f"review task 3 round {huge}", f"fix {huge}",
                 f"fix task 3 attempt {huge}", f"resolve task 3 round {huge}"):
        for agent_type in ("general-purpose", "spec:implementer"):
            result = parse_agent_description(described(RUN, rest), agent_type)
            assert result["role"] in ROLES
            if result["role"] == "other":
                _assert_other(result, _short(rest[:40], agent_type, True))
            else:
                for key in ("attempt", "round"):
                    assert result.get(key) is None or isinstance(result[key], int)


def test_44_the_known_templates_still_parse_so_other_is_not_a_catch_all():
    """Guard against the trivial fix (everything is `other`): the exact templates keep their roles."""
    from claude_kiro.analytics import parse_agent_description

    cases = [
        ("implement task 3", "spec:implementer", "implementer", "3"),
        ("implement task 3", "general-purpose", "implementer", "3"),
        ("review task 3 round 1", "spec:reviewer", "reviewer", "3"),
        ("review task 3 round 1", "general-purpose", "reviewer", "3"),
        ("resolve task 3", "general-purpose", "resolver", "3"),
        ("fix 1", "general-purpose", "fixer", None),
        ("fix task 3 attempt 1", "general-purpose", "fixer", "3"),
    ]
    for rest, agent_type, role, task in cases:
        for description in (described(RUN, rest), rest):
            got = parse_agent_description(description, agent_type)
            assert got["role"] == role and got["matched"] is True and got["task"] == task, (description, agent_type, got)
    assert parse_agent_description(described(RUN, "implement task 3 retry 2"), "general-purpose")["attempt"] == 3
    assert parse_agent_description(described(RUN, "revise task 3 round 2"), "general-purpose")["round"] == 2


def test_44_every_role_name_and_show_vocabulary_word_is_other_on_its_own_and_decorated():
    """4.4: the system's own role names, alone and in `<role> <n>` / `<role>: <n>` forms, are no template."""
    from claude_kiro.analytics import ROLES, parse_agent_description

    for role in sorted(ROLES):
        for rest in (role, role.upper(), f"{role} 3", f"{role}: 3", f"{role} task 3", f"{role}\n", f"{role} done"):
            for agent_type in ("general-purpose", "spec:reviewer"):
                _assert_other(parse_agent_description(described(RUN, rest), agent_type), _short(rest, agent_type, True))


def test_44_other_is_never_promoted_by_a_marker_with_a_mangled_id():
    """4.4/3.17: `[ck:<bad id>] implement task 3` is not marked; with or without a marker it must not be guessed to a role by type."""
    from claude_kiro.analytics import parse_agent_description

    for bad in ("[ck:ag_0123ABCD] implement task 3x", "[ck:ag_0123abc] implement task 3x", "[ck: ag_0123abcd] implement task 3x"):
        got = parse_agent_description(bad, "spec:implementer")
        assert got["run_id"] is None, bad
        assert got["role"] == "other" and got["matched"] is False and got["task"] is None, (bad, got)


# --------------------------------------------------------------------------- the real CLI over a fake Claude home


def _run_cli(env, rests: List[str], *, agent_type: str = "general-purpose", control: str = "implement task 1"):
    repo = spec_repo(env.root / "repo", "demo", "integrate/demo", tasks=2)
    land(repo, "demo", "integrate/demo", 1)
    session = FakeClaudeHome(env.claude).session(cwd=repo)
    run_id, _manifest = begin(env, repo, kind="implement", spec="demo", into="integrate/demo")
    specs = [ToolAgentSpec(described(run_id, control), agent_type="spec:implementer", agent_id="actrl000000000001")]
    specs += [ToolAgentSpec(described(run_id, rest), agent_type=agent_type, agent_id=f"aadv{idx:010d}") for idx, rest in enumerate(rests)]
    session.tool_agents(specs)
    return repo, session, run_id


def _collect_and_show(env, repo, run_id: str):
    out = stats("collect", "--agents", run_id, "--json", env=env, cwd=repo)
    assert out.exit_code == 0, out.output
    shown = stats("show", run_id, "--json", env=env)
    assert shown.exit_code == 0, shown.output
    return load_record(env.xdg, run_id), json.loads(shown.stdout)


def _check_all_other(record: dict, shown: dict, rests: List[str]) -> None:
    by_id: Dict[str, dict] = {a["agentId"]: a for a in record["agents"]}
    wrong = []
    for idx, rest in enumerate(rests):
        agent = by_id.get(f"aadv{idx:010d}")
        if agent is None or agent["role"] != "other" or agent["task"] is not None:
            wrong.append((rest[:60], None if agent is None else (agent["role"], agent["task"])))
    assert not wrong, f"4.4: marked descriptions that were not attributed to `other`: {wrong}"
    assert by_id["actrl000000000001"]["role"] == "implementer"  # `other` is not a catch-all

    other = shown["other"]
    assert other["agents"] == len(rests), f"`other` counts {other['agents']} agents, expected {len(rests)}"
    assert other["minutes"] == pytest.approx(len(rests), rel=0.05)  # every fake agent runs one minute
    expected_usd = sum(a["usd"] for a in record["agents"] if a["role"] == "other")
    assert other["usd"] == pytest.approx(expected_usd)
    assert other["usd"] > 0
    by_role = shown["byRole"]
    leaked = sorted(r for r, b in by_role.items() if r not in ("implementer", "other") and b.get("agents", 0) != 0)
    assert not leaked, f"adversarial agents leaked into roles: {leaked}"
    assert by_role["implementer"]["agents"] == 1
    assert by_role["other"]["agents"] == len(rests)
    for a in record["agents"]:
        if a["role"] == "other":
            assert a.get("attempt") is None and a.get("round") is None, a


@pytest.mark.parametrize("agent_type", ["general-purpose", "spec:implementer", "spec:reviewer", "Explore"])
def test_44_cli_every_adversarial_description_counts_only_as_other(agent_type):
    """4.4 through `collect --agents` and `ck stats show --json`: the whole table lands in `other`, by agent type."""
    # a trailing newline etc. can be stripped by the fake Claude layout's JSON round trip never; the rest list is verbatim
    rests = [rest for _, rest in OTHER_RESTS if not rest.startswith("x" * 50) and "\x00" not in rest]
    with fresh_env() as env:
        repo, _session, run_id = _run_cli(env, rests, agent_type=agent_type)
        record, shown = _collect_and_show(env, repo, run_id)
        _check_all_other(record, shown, rests)


HEADLINE: List[Tuple[str, str]] = [
    ("cyrillic", "r\u0435view task 3 round 1"),
    ("fullwidth-digit", "implement task \uff13"),
    ("arabic-indic", "implement task 3 retry \u0661"),
    ("trailing-newline", "implement task 3\n"),
    ("prefix", "implement task 3 please"),
    ("retry-twice", "implement task 3 retry 2 retry 3"),
    ("retry-zero", "implement task 3 retry 0"),
    ("round-zero", "review task 3 round 0"),
    ("path-task", "implement task ../x"),
    ("workflow-label", "task 3"),
    ("create-md", "create requirements.md"),
    ("create-workflow-label", "create write requirements.md"),
    ("role-name", "reviewer"),
    ("show-line", "other: 3 agents, 3.0 minutes, $0.45"),
]


@pytest.mark.parametrize("rest", [pytest.param(r, id=i) for i, r in HEADLINE])
def test_44_cli_single_adversarial_description_is_one_other_agent(rest):
    with fresh_env() as env:
        repo, _session, run_id = _run_cli(env, [rest])
        record, shown = _collect_and_show(env, repo, run_id)
        _check_all_other(record, shown, [rest])


def test_44_cli_the_systems_own_show_output_fed_back_as_descriptions_stays_other():
    """4.4: collect, run `ck stats show`, use each of its lines as the description of a new marked agent, collect again."""
    with fresh_env() as env:
        repo, session, run_id = _run_cli(env, ["implement task 3 please"])
        _collect_and_show(env, repo, run_id)
        plain = stats("show", run_id, env=env)
        assert plain.exit_code == 0, plain.output
        lines = [ln for ln in plain.stdout.splitlines() if ln.strip()][:40]
        assert lines
        rests = ["implement task 3 please"] + lines
        specs = [ToolAgentSpec(described(run_id, ln), agent_type="general-purpose", agent_id=f"aadv{idx + 1:010d}") for idx, ln in enumerate(lines)]
        session.tool_agents(specs, start="2026-10-12T09:30:00Z")
        record, shown = _collect_and_show(env, repo, run_id)
        _check_all_other(record, shown, rests)


def test_44_cli_recollecting_keeps_the_other_count_stable():
    """4.4: collecting twice does not promote `other` agents (the record is not read back as a hint)."""
    rests = ["implement task 3 please", "review task 3", "fix 0", "implement task 3\n"]
    with fresh_env() as env:
        repo, _session, run_id = _run_cli(env, rests)
        record, shown = _collect_and_show(env, repo, run_id)
        _check_all_other(record, shown, rests)
        again = stats("collect", "--agents", run_id, "--json", env=env, cwd=repo)
        assert again.exit_code == 0 and json.loads(again.stdout)["result"] == "unchanged", again.output
        record2, shown2 = _collect_and_show(env, repo, run_id)
        _check_all_other(record2, shown2, rests)


def test_44_cli_meta_model_or_transcript_prompt_does_not_rescue_an_unmatched_description():
    """4.4: neither the transcript's first message nor the meta's other keys may supply the role."""
    with fresh_env() as env:
        repo, session, run_id = _run_cli(env, [])
        session.tool_agents(
            [ToolAgentSpec(described(run_id, "do the work"), agent_type="spec:reviewer", agent_id="aadv0000000000",
                           extra_meta={"role": "reviewer", "title": "review task 3 round 1", "prompt": "implement task 3"})],
            start="2026-10-12T09:20:00Z")
        record, shown = _collect_and_show(env, repo, run_id)
        assert shown["other"]["agents"] == 1
        agent = {a["agentId"]: a for a in record["agents"]}["aadv0000000000"]
        assert agent["role"] == "other" and agent["task"] is None
