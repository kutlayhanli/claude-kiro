"""TC-42, TC-43, TC-49 and the doc halves of TC-36, TC-80 and TC-87: the guard ladder, parking and resolve flow in the
agent-mode instructions and the skills.

Static checks on the packaged Markdown and on their `claude_kiro.plugin.vendor_files()` renderings, in memory, as
`test_stats_agents_docs.py` does. The on-disk vendored copies are checked by the dogfood test (Task 14).
Project seams are imported inside test bodies.
"""

import json
import re
from pathlib import Path
from typing import List

import pytest

from claude_kiro import plugin as pl

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin"
SUMMARY = REPO / "tests" / "fixtures" / "hook_inputs" / "capture-summary.json"

SOURCES = {
    "implement": ("skills/implement/SKILL.md", ".claude/commands/spec/implement.md"),
    "create": ("skills/create/SKILL.md", ".claude/commands/spec/create.md"),
    "review": ("skills/review/SKILL.md", ".claude/commands/spec/review.md"),
    "spawn-worktree": ("skills/spawn-worktree/SKILL.md", ".claude/commands/spec/spawn-worktree.md"),
    "without-workflow-tool": ("resources/without-workflow-tool.md", pl.NO_WORKFLOW_GUIDE),
}
KINDS = ("packaged", "vendored")
MARK = "[ck:<run_id>] "
GUARD_TEMPLATE = MARK + "implement task <N> guard <k>"


def text_of(name: str, kind: str) -> str:
    packaged, vendored = SOURCES[name]
    if kind == "packaged":
        return (PLUGIN / packaged).read_text()
    return pl.vendor_files()[vendored]


def cases(names):
    return [(n, k) for n in names for k in KINDS]


def ids(case):
    return f"{case[0]}-{case[1]}"


def templates_of(text: str) -> List[str]:
    out = []
    for span in re.findall(r"`([^`\n]+)`", text):
        if span.startswith("[ck:<run_id>]") and span.strip() != "[ck:<run_id>]":
            out.append(span)
        elif span.startswith("... "):
            out.append(MARK + span[4:])
    return out


def need(text: str, pattern: str, what: str, flags=re.IGNORECASE) -> None:
    assert re.search(pattern, text, flags), f"missing: {what} (pattern {pattern!r})"


def nearby(text: str, anchor: str, pattern: str, radius: int = 600) -> bool:
    for m in re.finditer(anchor, text, flags=re.IGNORECASE):
        if re.search(pattern, text[max(0, m.start() - radius): m.end() + radius], flags=re.IGNORECASE | re.DOTALL):
            return True
    return False


# --------------------------------------------------------------------------- TC-42

SCHEDULER = cases(["without-workflow-tool", "spawn-worktree"])


@pytest.mark.parametrize("case", SCHEDULER, ids=ids)
def test_tc42_ladder_instructions(case):
    text = text_of(*case)
    need(text, r"guardStop", "a task result with guardStop starts the ladder")
    need(text, r"ck agents --guard-ladder <?[\w-]*>?\s*--json", "models come from `ck agents --guard-ladder <model> --json`")
    assert nearby(text, r"guard-ladder", r"escalate"), "must say the ladder is independent of `escalate`"
    assert nearby(text, r"guard-ladder", r"independent|regardless|ignor|not .{0,30}escalate"), "must say the ladder is independent of `escalate`"
    assert nearby(text, r"guard-ladder", r"once|one attempt"), "one attempt per model"
    assert nearby(text, r"guard-ladder", r"effort"), "same agent type and effort"
    assert nearby(text, r"guard-ladder", r"agent type|same type|subagent_type|same agent"), "same agent type"
    need(text, r"handoff", "reads the handoff note")
    need(text, r"decision\.md|decision note", "reads the decision note")
    assert GUARD_TEMPLATE in templates_of(text), f"agent description template {GUARD_TEMPLATE!r} missing"


@pytest.mark.parametrize("case", SCHEDULER, ids=ids)
def test_tc42_park_instructions(case):
    text = text_of(*case)
    need(text, r"ck worktree park\b", "unresolved and unattended: `ck worktree park`")
    assert nearby(text, r"ck worktree park", r"quot"), "park carries the quoted test and requirement"
    assert nearby(text, r"ck worktree park", r"test") and nearby(text, r"ck worktree park", r"requirement")
    assert nearby(text, r"ck worktree park|park(ed)?\b", r"dependents?[^.\n]{0,80}(never|not|no longer)[^.\n]{0,40}start|never start[^.\n]{0,40}dependents?|(never|not)[^.\n]{0,40}start[^.\n]{0,60}dependents?"), (
        "dependents of a parked task never start")
    assert nearby(text, r"ck worktree park|park(ed)?\b", r"independent"), "independent tasks continue"


@pytest.mark.parametrize("case", SCHEDULER, ids=ids)
def test_tc42_interactive_resolve_instructions(case):
    text = text_of(*case)
    need(text, r"AskUserQuestion", "interactive: ask with AskUserQuestion")
    assert nearby(text, r"AskUserQuestion", r"quot"), "AskUserQuestion quotes the test and requirement"
    need(text, r"ck worktree resolve\b", "record the answer with `ck worktree resolve`")
    assert nearby(text, r"ck worktree resolve", r"main session"), "my change to the test or requirement is made from the main session (7.11)"
    assert nearby(text, r"main session", r"before[^.\n]{0,80}(next|re-?run|attempt)|(next|re-?run|attempt)[^.\n]{0,80}after"), (
        "the change comes before the task's next attempt")


@pytest.mark.parametrize("case", SCHEDULER, ids=ids)
def test_tc42_final_report_items(case):
    text = text_of(*case)
    need(text, r"needs[- ]input", "the final report lists needs-input tasks")
    assert nearby(text, r"needs[- ]input", r"merged") and nearby(text, r"needs[- ]input", r"failed") and nearby(text, r"needs[- ]input", r"blocked"), (
        "needs-input is listed apart from merged, failed and blocked")
    assert nearby(text, r"needs[- ]input", r"apart|separate|own (list|section|line)|not (as|under) (failed|blocked)"), "listed apart"
    assert nearby(text, r"needs[- ]input", r"escalat"), "names the escalation models"
    assert nearby(text, r"needs[- ]input", r"notif"), "names a notification failure or missing config"
    assert nearby(text, r"notif", r"not configured|not_configured|missing|run\.notify_command|failed"), "notification failure or missing config"
    assert nearby(text, r"needs[- ]input", r"ck worktree resolve"), "says how to resolve a parked task"


# --------------------------------------------------------------------------- TC-43


def test_tc43_guard_description_parses_as_an_implementer_guard_step():
    from claude_kiro.analytics import parse_agent_description

    got = parse_agent_description("[ck:ag_3f9c01ab] implement task 3 guard 1", "spec:implementer")
    assert got["role"] == "implementer"
    assert str(got["task"]) == "3"
    assert got["step"] == "guard"


def test_tc43_park_label_maps_to_the_orchestrator():
    from claude_kiro.analytics.roles import match_label

    got = match_label("park 3", "spec-implement")
    assert got.role == "orchestrator" and got.matched


@pytest.mark.parametrize("case", SCHEDULER, ids=ids)
def test_tc43_guard_template_renders_and_parses(case):
    from claude_kiro.analytics import parse_agent_description

    templates = [t for t in templates_of(text_of(*case)) if "guard" in t]
    assert templates, f"{ids(case)}: no guard template"
    for t in templates:
        rest = t[len(MARK):]
        assert not re.sub(r"<(run_id|N|k)>", "", rest).count("<"), f"{t!r}: placeholder outside <run_id>, <N>, <k>"
        for marked in (True, False):
            body = rest.replace("<N>", "3").replace("<k>", "2")
            desc = f"[ck:ag_3f9c01ab] {body}" if marked else body
            for agent_type in ("spec:implementer", "ck-implementer"):
                got = parse_agent_description(desc, agent_type)
                assert got["role"] != "other", f"{desc!r} ({agent_type}) parses to role other"
                assert got["step"] == "guard"


# --------------------------------------------------------------------------- TC-36 / TC-80 (doc halves)


@pytest.mark.parametrize("case", cases(["implement"]), ids=ids)
def test_tc36_implement_skill_asks_for_ask_entries(case):
    text = text_of(*case)
    need(text, r"interactive", "passes `interactive` into the workflow args")
    need(text, r"needsInput", "reads result.needsInput")
    need(text, r"action['`\"]?:?\s*['`\"]?ask|`ask`", "handles entries with action ask")
    need(text, r"AskUserQuestion", "asks with AskUserQuestion")
    assert nearby(text, r"AskUserQuestion", r"quot[^.\n]{0,80}test[^.\n]{0,60}requirement|quot[^.\n]{0,80}requirement[^.\n]{0,60}test"), "quotes both the test and the requirement"


@pytest.mark.parametrize("case", cases(["implement"]), ids=ids)
def test_tc80_implement_skill_records_answers_and_reruns_once(case):
    text = text_of(*case)
    need(text, r"ck worktree resolve <spec> <N>", "records each answer with `ck worktree resolve <spec> <N>`")
    assert nearby(text, r"ck worktree resolve", r"change the test or (the )?requirement"), "tells me to change the test or requirement"
    assert nearby(text, r"ck worktree resolve", r"main session"), "from the main session"
    assert nearby(text, r"ck worktree resolve", r"confirm"), "waits for me to confirm the change (7.11)"
    assert nearby(text, r"ck worktree resolve", r"re-?run[^.\n]{0,60}once|once[^.\n]{0,60}re-?run|one re-?run"), "re-runs once"


# --------------------------------------------------------------------------- TC-49

SKILLS = ("implement", "create", "review")


@pytest.mark.parametrize("case", cases(SKILLS), ids=ids)
def test_tc49_handoff_is_step_zero(case):
    name = case[0]
    text = text_of(*case)
    m = re.search(rf"ck handoff {name}\b", text)
    assert m, f"{ids(case)}: no `ck handoff {name}` step"
    assert "--json" in text[m.start(): m.start() + 200]
    assert re.search(r"step 0|0\.|before (choosing|the choice|anything)", text[max(0, m.start() - 300): m.start() + 300], flags=re.IGNORECASE), "it is step 0, before the Workflow/agent-mode choice"
    need(text, r"handoff: ?true|`handoff` is true|handoff.{0,10}true", "stop and relay on handoff true")
    need(text, r"manualCommand|manual command", "stop with the printed manual command on failure")
    need(text, r"handoff: ?false|`handoff` is false|handoff.{0,10}false", "continue on handoff false")


def test_tc49_single_task_path_of_implement_has_no_handoff():
    for kind in KINDS:
        text = text_of("implement", kind)
        assert re.search(r"single[- ]task[^\n]{0,200}(no|not|without|skip)[^\n]{0,80}hand-?off|hand-?off[^\n]{0,200}(whole[- ]spec|not[^\n]{0,40}single)", text, flags=re.IGNORECASE), (
            f"{kind}: implement must say the single-task path has no hand-off")


@pytest.mark.parametrize("case", cases(SKILLS), ids=ids)
def test_tc49_workflow_marker_wrap_when_agents_lack_agent_id(case):
    summary = json.loads(SUMMARY.read_text())
    if summary.get("workflowAgentHasAgentId") is not False:
        pytest.skip("capture-summary.json: Workflow agents carry agent_id, so the workflow marker (3.4) is not built")
    text = text_of(*case)
    begin = re.search(r"ck guard workflow-begin <spec>", text)
    end = re.search(r"ck guard workflow-end", text)
    assert begin and end, f"{ids(case)}: no `ck guard workflow-begin <spec>` / `ck guard workflow-end` wrap"
    assert begin.start() < end.start()


@pytest.mark.parametrize("case", cases(SKILLS), ids=ids)
def test_tc49_no_marker_wrap_when_workflow_agents_have_agent_id(case):
    summary = json.loads(SUMMARY.read_text())
    if summary.get("workflowAgentHasAgentId") is not True:
        pytest.skip("capture-summary.json: Workflow agents lack agent_id, so the marker wrap is required instead")
    assert "workflow-begin" not in text_of(*case), f"{ids(case)}: marker wrap present although the marker is not built"


# --------------------------------------------------------------------------- TC-87 (doc half)


@pytest.mark.parametrize("kind", KINDS)
def test_tc87_review_revise_agent_gets_the_frozen_spec_rule(kind):
    text = text_of("without-workflow-tool", kind)
    assert nearby(text, r"revise", r"Not Started", 1500), "the rule applies when a task is no longer Not Started"
    assert nearby(text, r"Not Started", r"requirements\.md", 800) and nearby(text, r"Not Started", r"test-plan\.md", 800)
    assert nearby(text, r"Not Started", r"(do not|never|not) apply|not applied", 800), "existing text is not applied"
    assert nearby(text, r"Not Started", r"Needs Your Decision", 1200), "returned as a decision for review.md 'Needs Your Decision'"
    assert nearby(text, r"Not Started", r"add(ing)?[- ]only|only add|adding", 1200), "add-only fixes stay allowed"


def test_tc87_vendored_workflow_equals_its_source():
    vendored = REPO / ".claude" / "workflows" / "spec-create.js"
    assert vendored.read_bytes() == (PLUGIN / "workflows" / "spec-create.js").read_bytes()
