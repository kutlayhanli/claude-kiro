"""Red team of the role parser's no-guess rule (requirements 5.2, 6.2).

An agent whose label matches no known pattern is `other`, and the system SHALL NOT guess a role. Every case here is
an adversarial label that a lenient parser (case folding, `\\d`, `strip()`, `re.search`, `$` before a newline,
prefix matching, falling back to the other workflow's grammar, reading `.meta.json` instead of the label) would
turn into a real role. Each one must land in `other`:

* through the seam `claude_kiro.analytics.parse_label(label, workflow)`, and
* through the real CLI: a fake Claude home -> `ck stats collect --run` -> `ck stats show <id> --json`, where the
  agents are counted in the `other` summary and in no other role.

Written from requirements.md and design.md's grammar table only. Project modules are imported inside the test
bodies so the file collects before the implementation exists.
"""

from __future__ import annotations

import json
from typing import Dict, List, Tuple

import pytest

from stats_helpers import AgentSpec, FakeClaudeHome, fresh_env, git_project, load_record, stats

# --------------------------------------------------------------------------- adversarial labels

# (id, label) pairs that match no row of the grammar table of the spec-implement workflow.
IMPLEMENT_OTHER: List[Tuple[str, object]] = [
    # unicode lookalikes, other scripts' digits, invisible characters
    ("cyrillic-a-in-task", "tаsk 3"),
    ("cyrillic-e-in-review", "rеview 3"),
    ("cyrillic-o-in-resolve", "resоlve 3"),
    ("fullwidth-digit-task-id", "task ３"),
    ("fullwidth-letter-t", "ｔask 3"),
    ("arabic-indic-digit-task-id", "task ٣"),
    ("circled-digit-task-id", "task ③"),
    ("arabic-indic-round", "review 3 round ١"),
    ("arabic-indic-retry-count", "task 3 retry ١"),
    ("fullwidth-digit-in-retry", "task 3 retry １"),
    ("long-s-casefolds-to-s", "taſk 3"),
    ("fi-ligature-in-fix", "ﬁx 1 (x)"),
    ("zero-width-space-inside-keyword", "ta​sk 3"),
    ("zero-width-space-before-id", "task ​3"),
    ("zero-width-space-instead-of-space", "task​3"),
    ("zero-width-space-trailing", "task 3​"),
    ("zero-width-joiner-trailing", "review 3‍"),
    ("byte-order-mark-leading", "﻿task 3"),
    ("soft-hyphen-in-keyword", "re­view 3"),
    ("rtl-override-trailing", "task 3‮"),
    ("nul-trailing", "task 3\x00"),
    ("no-break-space-separator", "task 3"),
    ("ideographic-space-separator", "task　3"),
    ("trailing-no-break-space", "task 3 "),
    # whitespace and line breaks
    ("tab-separator", "task\t3"),
    ("tab-before-test-marker", "task 3\t[test]"),
    ("double-space", "task  3"),
    ("leading-space", " task 3"),
    ("trailing-space", "task 3 "),
    ("trailing-newline", "task 3\n"),
    ("trailing-carriage-return", "task 3\r"),
    ("trailing-crlf", "task 3\r\n"),
    ("leading-newline", "\ntask 3"),
    ("newline-instead-of-space", "task\n3"),
    ("two-templates-on-two-lines", "task 3\nreview 3"),
    ("two-templates-reversed", "review 3\ntask 3"),
    ("two-templates-one-line", "task 3 review 3"),
    ("two-templates-semicolon", "task 3; review 3"),
    ("two-reviews-on-two-lines", "review 3\nreview 3"),
    ("review-with-newline-before-round", "review 3\nround 2"),
    ("land-then-newline-land", "land 1\nland 2"),
    ("report-trailing-newline", "report\n"),
    ("report-trailing-space", "report "),
    ("final-gate-trailing-newline", "final gate\n"),
    # case changes
    ("upper-case-task", "TASK 3"),
    ("title-case-task", "Task 3"),
    ("title-case-review", "Review 3"),
    ("upper-case-round", "review 3 ROUND 2"),
    ("upper-case-test-marker", "task 3 [TEST]"),
    ("upper-case-retry", "task 3 RETRY 1"),
    ("title-case-report", "Report"),
    ("title-case-final-gate", "Final gate"),
    ("upper-case-land", "LAND 3"),
    ("upper-case-fix", "FIX 1 (x)"),
    # truncated templates
    ("bare-task", "task"),
    ("task-and-space", "task "),
    ("bare-review", "review"),
    ("bare-resolve", "resolve"),
    ("bare-land", "land"),
    ("bare-merge", "merge"),
    ("bare-fix", "fix"),
    ("fix-without-parens", "fix 1"),
    ("fix-unclosed-paren", "fix 1 (x"),
    ("fix-unclosed-model-bracket", "fix 1 (x) [opus"),
    ("bare-recheck", "re-check"),
    ("recheck-non-number", "re-check x"),
    ("bare-cross-test", "cross-test"),
    ("cross-test-without-number", "cross-test wave"),
    ("bare-plan", "plan"),
    ("bare-full-gate", "full gate"),
    ("truncated-final-gate", "final gat"),
    ("retry-without-count", "task 3 retry"),
    ("retry-with-word-count", "task 3 retry x"),
    ("revise-without-count", "task 3 revise"),
    ("unclosed-model-paren", "task 3 retry 1 (opus"),
    ("unclosed-test-marker", "task 3 [test"),
    ("round-without-number", "review 3 round"),
    ("round-with-word", "review 3 round x"),
    ("resolve-round-without-number", "resolve 3 round"),
    ("truncated-report", "repor"),
    # near misses
    ("no-space-task", "task3"),
    ("colon-task", "task: 3"),
    ("hyphen-review", "review-3"),
    ("underscore-review", "review_3"),
    ("glued-review", "review3"),
    ("comma-before-round", "review 3, round 1"),
    ("hyphenated-task-id", "task 3-4"),
    ("dotted-task-id", "task 3.1"),
    ("test-marker-twice", "task 3 [test] [test]"),
    ("test-marker-before-id", "task [test] 3"),
    ("model-without-retry", "task 3 (opus)"),
    ("trailing-junk-after-retry", "task 3 retry 1 (opus) extra"),
    ("trailing-junk-after-round", "review 3 round 1 extra"),
    ("trailing-junk-after-report", "report now"),
    ("trailing-junk-after-final-gate", "final gate passed"),
    ("negative-retry", "task 3 retry -1"),
    ("words-reversed", "3 task"),
    ("prefix-only", "tasks 3"),
    ("plural-review", "reviews 3"),
    ("recheck-no-hyphen", "recheck 1"),
    ("recheck-space", "re check 1"),
    ("cross-test-no-hyphen", "cross test wave 1"),
    ("full-gate-no-at", "full gate 1"),
    ("suffix-of-valid-label", "x task 3"),
    # the other workflow's valid templates
    ("create-write-design", "write design.md"),
    ("create-write-requirements", "write requirements.md"),
    ("create-write-test-plan", "write test-plan.md"),
    ("create-write-impl-track", "write implementation track"),
    ("create-write-test-track", "write test track"),
    ("create-link-tracks", "link tracks into tasks.md"),
    ("create-review-lens", "review: fidelity"),
    ("create-refute-lens", "refute: fidelity"),
    ("create-revise", "revise spec + write review.md"),
    ("create-consistency-pass", "consistency check: pass 1"),
    ("create-snapshot", "snapshot spec before revision"),
    ("create-cleanup", "remove pre-revision snapshot"),
    # the role names and the output vocabulary themselves
    ("role-name-implementer", "implementer"),
    ("role-name-test-writer", "test-writer"),
    ("role-name-reviewer", "reviewer"),
    ("role-name-fixer", "fixer"),
    ("role-name-resolver", "resolver"),
    ("role-name-gate", "gate"),
    ("role-name-land", "landing"),
    ("role-name-other", "other"),
    ("role-then-task", "implementer 3"),
    ("reviewer-then-task", "reviewer 3"),
    ("fixer-with-attempt", "fixer 1"),
    ("role-equals-form", "role=reviewer task=3"),
    ("role-colon-form", "role: reviewer"),
    ("role-in-brackets", "[reviewer] task 3"),
    # JSON-ish and structured labels
    ("json-role-object", '{"role": "reviewer"}'),
    ("json-role-and-task", '{"role": "implementer", "task": "3"}'),
    ("json-label-object", '{"label": "task 3"}'),
    ("json-array-label", '["task 3"]'),
    ("json-string-label", '"task 3"'),
    ("json-null-text", "null"),
    ("python-dict-text", "{'role': 'fixer'}"),
    ("xml-ish", "<label>task 3</label>"),
    # lines of `ck stats show` output and other ck prose
    ("stats-line-no-runs", "ℹ️ no runs collected yet; run ck stats collect --all"),
    ("stats-line-other-summary", "other: 3 agents, 3.0 minutes, $0.45"),
    ("stats-line-by-role", "byRole"),
    ("stats-line-implementer-cost", "implementer  $1.20  12.0 min"),
    ("stats-line-reviewer-cost", "reviewer  $0.40  3.0 min"),
    ("stats-line-collected", "collected: 1"),
    ("stats-line-unknown", "unknown"),
    ("stats-line-unfinished", "unfinished: 2"),
    # empty and degenerate
    ("empty-label", ""),
    ("single-space", " "),
    ("only-newline", "\n"),
    ("only-zero-width-space", "​"),
    ("only-digits", "3"),
    ("punctuation-only", "..."),
    ("dash-label", "-"),
    # very long labels
    ("very-long-garbage", "x" * 100_000),
    ("very-long-task-suffix", "task 3 " + "a" * 100_000),
    ("very-long-keyword-run", "task" * 25_000),
    ("very-long-trailing-whitespace", "review 3 round 1" + " " * 10_000),
    ("very-long-leading-whitespace", " " * 10_000 + "task 3"),
    ("very-long-newlines", "task 3" + "\n" * 5_000),
    ("very-long-repeated-templates", "task 3 " * 5_000),
]

# Not strings at all: the seam takes `object` and must not crash or guess.
NON_STRING_LABELS: List[Tuple[str, object]] = [
    ("none", None),
    ("integer", 3),
    ("float", 3.5),
    ("true", True),
    ("list-of-label", ["task 3"]),
    ("dict-of-label", {"label": "task 3"}),
    ("bytes-label", b"task 3"),
    ("empty-list", []),
]

# (id, label) pairs that match no row of the spec-create grammar table.
CREATE_OTHER: List[Tuple[str, object]] = [
    ("implement-task", "task 3"),
    ("implement-test-task", "task 3 [test]"),
    ("implement-review", "review 3"),
    ("implement-review-round", "review 3 round 2"),
    ("implement-resolve", "resolve 3"),
    ("implement-land", "land 3"),
    ("implement-fix", "fix 1 (x)"),
    ("implement-recheck", "re-check 1"),
    ("implement-final-gate", "final gate"),
    ("implement-cross-test", "cross-test wave 1"),
    ("implement-plan", "plan 1"),
    ("implement-report", "report"),
    ("cyrillic-e-in-requirements", "write rеquirements.md"),
    ("cyrillic-a-in-design", "write desіgn.md"),
    ("zero-width-space-in-design", "write des​ign.md"),
    ("double-space-write", "write  design.md"),
    ("tab-write", "write\tdesign.md"),
    ("trailing-space-design", "write design.md "),
    ("trailing-newline-design", "write design.md\n"),
    ("leading-space-requirements", " write requirements.md"),
    ("two-templates-on-two-lines", "write requirements.md\nwrite design.md"),
    ("upper-case-write", "Write design.md"),
    ("upper-case-extension", "write design.MD"),
    ("wrong-extension", "write design.mdx"),
    ("no-extension", "write design"),
    ("singular-requirements", "write requirement.md"),
    ("test-plan-with-space", "write test plan"),
    ("test-track-plural", "write test tracks"),
    ("impl-track-short", "write impl track"),
    ("impl-track-trailing-space", "write implementation track "),
    ("link-tracks-truncated", "link tracks"),
    ("link-tracks-wrong-file", "link tracks into design.md"),
    ("review-without-colon", "review fidelity"),
    ("review-upper-case", "REVIEW: fidelity"),
    ("review-space-before-colon", "review : fidelity"),
    ("refute-without-colon", "refute fidelity"),
    ("refute-upper-case", "Refute: fidelity"),
    ("revise-truncated", "revise spec"),
    ("revise-upper-case", "Revise spec + write review.md"),
    ("consistency-without-pass", "consistency check"),
    ("consistency-pass-without-number", "consistency check: pass"),
    ("consistency-without-colon", "consistency check pass 1"),
    ("consistency-two-lines", "consistency check: pass 1\nconsistency check: pass 2"),
    ("snapshot-truncated", "snapshot spec"),
    ("snapshot-upper-case", "Snapshot spec before revision"),
    ("cleanup-truncated", "remove snapshot"),
    ("role-name-reviewer", "reviewer"),
    ("role-name-fixer", "fixer"),
    ("role-name-implementer", "implementer"),
    ("json-role-object", '{"role": "reviewer"}'),
    ("stats-line-other-summary", "other: 3 agents, 3.0 minutes, $0.45"),
    ("empty-label", ""),
    ("very-long-garbage", "x" * 100_000),
    ("very-long-write-suffix", "write design.md" + " " * 10_000),
]

def _implement_params(workflow: str):
    return [pytest.param(label, workflow, id=f"{workflow}:{name}") for name, label in IMPLEMENT_OTHER]


# --------------------------------------------------------------------------- the seam


def _assert_other(result: object, label: object, workflow: str) -> None:
    shown = repr(label) if len(repr(label)) < 120 else repr(label)[:100] + "..."
    assert isinstance(result, dict), f"parse_label({shown}, {workflow!r}) did not return a dict: {result!r}"
    assert result.get("role") == "other", f"parse_label({shown}, {workflow!r}) guessed a role: {result.get('role')!r}"
    assert result.get("task") is None, f"parse_label({shown}, {workflow!r}) guessed a task: {result.get('task')!r}"
    assert result.get("attempt") is None, f"parse_label({shown}, {workflow!r}) guessed an attempt: {result.get('attempt')!r}"
    assert result.get("round") is None, f"parse_label({shown}, {workflow!r}) guessed a round: {result.get('round')!r}"
    if "matched" in result:
        assert result["matched"] is False
    for key in ("escalated_to", "step", "wave", "tasks"):
        assert not result.get(key), f"parse_label({shown}, {workflow!r}) filled {key}: {result.get(key)!r}"


@pytest.mark.parametrize("label,workflow", _implement_params("spec-implement"))
def test_implement_label_that_matches_no_template_is_other(label, workflow):
    from claude_kiro.analytics import parse_label

    _assert_other(parse_label(label, workflow), label, workflow)


@pytest.mark.parametrize("label,workflow", _implement_params("spec-implement-iso"))
def test_iso_implement_label_that_matches_no_template_is_other(label, workflow):
    from claude_kiro.analytics import parse_label

    _assert_other(parse_label(label, workflow), label, workflow)


@pytest.mark.parametrize("label", [pytest.param(label, id=name) for name, label in CREATE_OTHER])
def test_create_label_that_matches_no_template_is_other(label):
    from claude_kiro.analytics import parse_label

    _assert_other(parse_label(label, "spec-create"), label, "spec-create")


@pytest.mark.parametrize("label", [pytest.param(label, id=name) for name, label in NON_STRING_LABELS])
@pytest.mark.parametrize("workflow", ["spec-implement", "spec-create"])
def test_non_string_label_is_other_and_does_not_crash(label, workflow):
    from claude_kiro.analytics import parse_label

    _assert_other(parse_label(label, workflow), label, workflow)


def test_known_labels_still_parse_so_other_is_not_a_catch_all():
    """Guard against the trivial fix (everything is `other`): the exact templates still get their role."""
    from claude_kiro.analytics import parse_label

    assert parse_label("task 3", "spec-implement")["role"] == "implementer"
    assert parse_label("task 3", "spec-implement")["task"] == "3"
    assert parse_label("review 3", "spec-implement")["role"] == "reviewer"
    assert parse_label("write design.md", "spec-create")["role"] == "implementer"


def test_the_systems_own_role_names_do_not_parse_back_into_roles():
    """Feed the parser the role names from `ROLES` and `<role> <task>` forms: none is a template."""
    from claude_kiro.analytics import ROLES, parse_label

    for role in sorted(ROLES):
        for label in (role, f"{role}: 3", role.upper(), f"{role}\n", f"{role} done!"):
            for workflow in ("spec-implement", "spec-create"):
                result = parse_label(label, workflow)
                if label == "report" and workflow == "spec-implement":
                    continue  # `report` is itself a template of the implement workflow
                _assert_other(result, label, workflow)


# --------------------------------------------------------------------------- the real CLI over a fake Claude home


def _collect_and_show(env, labels: List[object], workflow: str, *, control: str):
    """Write a run whose agents carry `labels` (plus one real control agent), collect it, return (record, show json)."""
    project = git_project(env.root)
    home = FakeClaudeHome(env.claude)
    session = home.session(cwd=project)
    agents = [AgentSpec(control, agent_id="aCTRL0000000001")]
    agents += [AgentSpec(label, agent_id=f"aadv{idx:010d}") for idx, label in enumerate(labels)]
    run_id = f"wf_redteam_{workflow.replace('-', '_')}"
    if workflow == "spec-create":
        session.create_run(run_id, agents=agents)
    else:
        session.implement_run(run_id, name=workflow, agents=agents)
    collected = stats("collect", "--run", run_id, env=env)
    assert collected.exit_code == 0, collected.output
    shown = stats("show", run_id, "--json", env=env)
    assert shown.exit_code == 0, shown.output
    return load_record(env.xdg, run_id), json.loads(shown.stdout)


def _check_all_other(record: dict, shown: dict, labels: List[object], *, control_role: str) -> None:
    by_id: Dict[str, dict] = {a["agentId"]: a for a in record["agents"]}
    wrong = []
    for idx, label in enumerate(labels):
        agent = by_id.get(f"aadv{idx:010d}")
        if agent is None or agent["role"] != "other" or agent["task"] is not None:
            shown_label = repr(label) if len(repr(label)) < 80 else repr(label)[:60] + "..."
            wrong.append((shown_label, None if agent is None else (agent["role"], agent["task"])))
    assert not wrong, f"adversarial labels that were not attributed to `other`: {wrong}"
    assert by_id["aCTRL0000000001"]["role"] == control_role  # `other` is not a catch-all

    other = shown["other"]
    assert other["agents"] == len(labels), f"`other` counts {other['agents']} agents, expected {len(labels)}"
    assert other["minutes"] == pytest.approx(len(labels), rel=0.05)  # every fake agent runs one minute
    expected_usd = sum(a["usd"] for a in record["agents"] if a["role"] == "other")
    assert other["usd"] == pytest.approx(expected_usd)
    assert other["usd"] > 0

    by_role = shown["byRole"]
    leaked = sorted(r for r, b in by_role.items() if r not in (control_role, "other") and b.get("agents", 0) != 0)
    assert not leaked, f"adversarial agents leaked into roles: {leaked}"
    assert by_role[control_role]["agents"] == 1
    assert by_role["other"]["agents"] == len(labels)


def test_cli_implement_run_with_every_adversarial_label_counts_them_all_as_other():
    labels = [label for _, label in IMPLEMENT_OTHER]
    with fresh_env() as env:
        record, shown = _collect_and_show(env, labels, "spec-implement", control="task 1")
        _check_all_other(record, shown, labels, control_role="implementer")


def test_cli_iso_run_with_every_adversarial_label_counts_them_all_as_other():
    labels = [label for _, label in IMPLEMENT_OTHER]
    with fresh_env() as env:
        record, shown = _collect_and_show(env, labels, "spec-implement-iso", control="task 1")
        _check_all_other(record, shown, labels, control_role="implementer")


def test_cli_create_run_with_every_adversarial_label_counts_them_all_as_other():
    labels = [label for _, label in CREATE_OTHER]
    with fresh_env() as env:
        record, shown = _collect_and_show(env, labels, "spec-create", control="write requirements.md")
        _check_all_other(record, shown, labels, control_role="implementer")


HEADLINE_TRICKS: List[Tuple[str, object, str, str]] = [
    ("cyrillic-a-in-task", "tаsk 3", "spec-implement", "task 1"),
    ("fullwidth-digit", "task ３", "spec-implement", "task 1"),
    ("zero-width-space", "task​ 3", "spec-implement", "task 1"),
    ("two-templates-on-two-lines", "task 3\nreview 3", "spec-implement", "task 1"),
    ("trailing-newline", "task 3\n", "spec-implement", "task 1"),
    ("create-template-in-implement-run", "write design.md", "spec-implement", "task 1"),
    ("implement-template-in-create-run", "review 3", "spec-create", "write requirements.md"),
    ("role-name", "reviewer", "spec-implement", "task 1"),
    ("json-ish", '{"role": "fixer"}', "spec-implement", "task 1"),
    ("stats-output-line", "other: 3 agents, 3.0 minutes, $0.45", "spec-implement", "task 1"),
]


@pytest.mark.parametrize("label,workflow,control", [pytest.param(l, w, c, id=i) for i, l, w, c in HEADLINE_TRICKS])
def test_cli_single_adversarial_label_is_one_other_agent(label, workflow, control):
    with fresh_env() as env:
        record, shown = _collect_and_show(env, [label], workflow, control=control)
        _check_all_other(record, shown, [label], control_role="implementer")


def test_cli_meta_description_does_not_rescue_a_label_that_matches_nothing():
    """The run file's label is `mystery agent`; the agent's `.meta.json` description says `task 3`. No guessing from it."""
    with fresh_env() as env:
        project = git_project(env.root)
        session = FakeClaudeHome(env.claude).session(cwd=project)
        run = session.implement_run(
            "wf_redteam_meta", agents=[AgentSpec("task 1", agent_id="aCTRL0000000001"), AgentSpec("mystery agent", agent_id=f"aadv{0:010d}")]
        )
        run.meta_path(f"aadv{0:010d}").write_text(json.dumps({"description": "task 3", "workflowPhase": "Wave 1", "model": "claude-sonnet-5-5"}))
        assert stats("collect", "--run", "wf_redteam_meta", env=env).exit_code == 0
        record = load_record(env.xdg, "wf_redteam_meta")
        agent = {a["agentId"]: a for a in record["agents"]}[f"aadv{0:010d}"]
        assert agent["role"] == "other" and agent["task"] is None
        shown = json.loads(stats("show", "wf_redteam_meta", "--json", env=env).stdout)
        assert shown["other"]["agents"] == 1


def test_cli_orphan_run_with_an_unrecognised_meta_description_is_other():
    """Run file gone (1.15): the label comes from `.meta.json`; an adversarial description is still `other`."""
    with fresh_env() as env:
        project = git_project(env.root)
        session = FakeClaudeHome(env.claude).session(cwd=project)
        run = session.implement_run(
            "wf_redteam_orphan",
            agents=[AgentSpec("task 1", agent_id="aCTRL0000000001"), AgentSpec("tаsk 3", agent_id=f"aadv{0:010d}")],
        )
        run.drop_run_file()
        collected = stats("collect", "--run", "wf_redteam_orphan", env=env)
        assert collected.exit_code == 0, collected.output
        record = load_record(env.xdg, "wf_redteam_orphan")
        by_id = {a["agentId"]: a for a in record["agents"]}
        assert by_id[f"aadv{0:010d}"]["role"] == "other" and by_id[f"aadv{0:010d}"]["task"] is None
        shown = json.loads(stats("show", "wf_redteam_orphan", "--json", env=env).stdout)
        assert shown["other"]["agents"] == 1


def test_cli_rerun_of_collect_keeps_the_other_count_stable():
    """Collect twice: the system's own record fed back in must not promote `other` agents to a role."""
    labels = ["tаsk 3", "task 3\nreview 3", "reviewer", "write design.md"]
    with fresh_env() as env:
        record, shown = _collect_and_show(env, labels, "spec-implement", control="task 1")
        _check_all_other(record, shown, labels, control_role="implementer")
        again = stats("collect", "--run", "wf_redteam_spec_implement", env=env)
        assert again.exit_code == 0, again.output
        record2 = load_record(env.xdg, "wf_redteam_spec_implement")
        shown2 = json.loads(stats("show", "wf_redteam_spec_implement", "--json", env=env).stdout)
        _check_all_other(record2, shown2, labels, control_role="implementer")
