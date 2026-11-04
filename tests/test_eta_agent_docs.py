"""TC-26, TC-52, TC-78 (document part) and TC-98: the skills and the agent-mode document name the progress commands.

Static reads of the packaged sources and of the `claude_kiro.plugin.vendor_files()` renderings. The agent-mode
document's command templates are cross-checked against the commands the Workflow scripts' prompts carry (the real
scripts under the bridge, prompts only).
"""

import re
import shutil
from pathlib import Path
from typing import Dict, List, Set

import pytest

from claude_kiro import plugin as pl
from eta_strategies import NO_NETWORK
from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)
from test_eta_workflow import claim_command, flag_map, land_command, IMPLEMENTER

pytestmark = [NO_NETWORK]

PLUGIN = Path(__file__).resolve().parents[1] / "plugin"

SOURCES = {
    "implement": ("skills/implement/SKILL.md", ".claude/commands/spec/implement.md"),
    "create": ("skills/create/SKILL.md", ".claude/commands/spec/create.md"),
    "without-workflow-tool": ("resources/without-workflow-tool.md", pl.NO_WORKFLOW_GUIDE),
}
KINDS = ("packaged", "vendored")
CLAIM_KINDS = ("task-start", "retry", "escalation", "revision", "landfix", "resolve")
PROGRESS_FLAGS = ("--progress-run", "--progress-event", "--progress-model", "--attempt", "--failed")


def doc(name: str, kind: str) -> str:
    packaged, vendored = SOURCES[name]
    if kind == "packaged":
        return (PLUGIN / packaged).read_text(encoding="utf-8")
    return pl.vendor_files()[vendored]


def spans(text: str) -> List[str]:
    """The inline-code spans (backticked, one line)."""
    return re.findall(r"`([^`\n]+)`", text)


def flag_names(span: str) -> Set[str]:
    return set(re.findall(r"--[a-z][a-z-]*", span))


def progress_flag_names(span: str) -> Set[str]:
    return {f for f in flag_names(span) if f in PROGRESS_FLAGS}


def span_with(text: str, *needles: str) -> str:
    for s in spans(text):
        if all(re.search(n, s) for n in needles):
            return s
    return ""


# --------------------------------------------------------------------------- TC-98: the skills' Workflow-mode steps

@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("name", ["implement", "create"])
def test_tc98_skills_run_progress_begin_events_and_end(name, kind):
    text = doc(name, kind)
    begin = re.search(r"`ck progress begin <spec> --kind " + name + r"\b[^`]*--mode workflow[^`]*--json[^`]*`", text)
    assert begin, f"{name}/{kind}: no backticked `ck progress begin <spec> --kind {name} --mode workflow --json`"
    first_args = text.index("progressRun")
    assert begin.start() < first_args, "begin comes before the workflow args that carry progressRun"
    assert "etaLine" in text and "progressRun" in text
    assert re.search(r"progressRun[^\n]*etaLine|etaLine[^\n]*progressRun", text), "both are passed in the workflow args"

    assert "AskUserQuestion" in text
    assert span_with(text, r"ck progress event <spec> approval-start --reason question"), f"{name}/{kind}: approval-start"
    assert span_with(text, r"ck progress event <spec> approval-end"), f"{name}/{kind}: approval-end"

    end = re.search(r"`ck progress end <spec> --outcome[^`]*`", text)
    assert end, f"{name}/{kind}: no backticked `ck progress end <spec> --outcome ...`"
    assert text.rfind("ck stats collect", 0, end.start()) != -1, "the end step comes after ck stats collect"
    tail = text[end.end(): end.end() + 700]
    assert re.search(r"quote", tail, re.I) and re.search(r"warning", tail, re.I), "quote its warning line"


# --------------------------------------------------------------------------- TC-78 (documents)

@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("name", ["implement", "without-workflow-tool"])
def test_tc78_documents_name_the_verify_file_error_and_the_single_task_refusal(name, kind):
    text = doc(name, kind)
    stop = r"refus|stop|don't (start|launch)|do not (start|launch)|not launch"
    lines = [l for l in text.splitlines() if "verify-file" in l]
    assert lines, f"{name}/{kind}: the verify-file lint error is not named"
    assert any("ck lint" in l and re.search(stop, l, re.I) for l in lines) or (
        "ck lint" in text and any(re.search(stop, l, re.I) for l in lines)), "stop on a lint cycle"
    single = [l for l in lines if re.search(r"single-task|one task|<spec> (X|N|<N>)", l) and re.search(stop, l, re.I)]
    assert single, f"{name}/{kind}: no refusal for a single-task run when a verify-file entry has that task"
    assert any(re.search(r"/spec:implement <spec> (X|N|<N>)", l) for l in single) or re.search(r"/spec:implement <spec> (X|N|<N>)", text)


# --------------------------------------------------------------------------- TC-26, TC-52: agent mode

def workflow_commands(bridge) -> Dict[str, object]:  # noqa: F811
    """What the Workflow scripts' prompts carry: claim kinds and flag names, land/plan flags, create event flags."""
    deps = {str(i): [] for i in range(1, 7)}
    project = {
        "deps": deps, "durations": {"1": 3, "2": 3, "3": 3, "4": 3, "5": 3, "6": 1},
        "failUntil": {"2": 2, "6": 9}, "checkRed": ["3"], "reviews": {"4": ["changes", "approve"]}, "conflicts": ["5"],
        "planDurations": {str(k): 1 for k in range(1, 80)},
    }
    args = {"agents": {"implementer": dict(IMPLEMENTER), "reviewer": {"model": "opus", "effort": "high", "enabled": True, "rounds": 2}},
            "deps": deps, "remaining": list(deps), "mergeBatch": 1, "progressRun": "K", "etaLine": None}
    out = bridge.run(project, args, slots=3)
    claim_kinds, claim_flags, land_flags, plan_flags = set(), set(), set(), set()
    for prompt in out["prompts"].values():
        claim = claim_command(prompt)
        if claim and "--progress-event" in claim["flags"]:
            claim_kinds.add(claim["flags"]["--progress-event"])
            claim_flags |= {f for f in claim["flags"] if f in PROGRESS_FLAGS}
        land = land_command(prompt)
        if land:
            land_flags |= {f for f in land["flags"] if f in PROGRESS_FLAGS}
        m = re.search(r"\bplan demo ([^\n]*--failed[^\n]*)", prompt)
        if m:
            plan_flags |= {f for f in flag_map(m.group(1)) if f in PROGRESS_FLAGS}
    created = bridge.create({"progressRun": "K", "etaLine": None})
    events: Dict[str, Set[str]] = {}
    for prompt in created["prompts"].values():
        for m in re.finditer(r"progress event \S+ (phase-start|step-end)([^\n]*)", prompt):
            events.setdefault(m.group(1), set()).update(flag_names(m.group(2)))
    return {"claim_kinds": claim_kinds, "claim_flags": claim_flags, "land_flags": land_flags, "plan_flags": plan_flags, "events": events}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("kind", KINDS)
def test_tc26_agent_mode_document_templates_equal_the_workflow_prompts(bridge, kind):  # noqa: F811
    wf = workflow_commands(bridge)
    text = doc("without-workflow-tool", kind)
    sp = spans(text)

    # the Workflow prompts carried every claim kind and flag, so the cross-check is meaningful
    assert wf["claim_kinds"] == set(CLAIM_KINDS), wf["claim_kinds"]
    assert wf["claim_flags"] == {"--progress-run", "--progress-event", "--progress-model", "--attempt"}
    assert wf["land_flags"] == {"--progress-run"}
    assert wf["plan_flags"] == {"--progress-run", "--failed"}
    assert wf["events"] == {"phase-start": {"--phase", "--run"}, "step-end": {"--phase", "--run", "--json"}}

    # begin and end
    assert span_with(text, r"ck progress begin .*--mode agents"), "progress begin --mode agents"
    assert span_with(text, r"ck progress end .*--outcome"), "progress end --outcome"

    # claim: the same flags, and every claim kind named
    claims = [s for s in sp if "worktree claim" in s]
    assert claims, "claim template"
    assert set().union(*[progress_flag_names(s) for s in claims]) == wf["claim_flags"]
    doc_kinds = {k for s in sp for k in CLAIM_KINDS if re.search(rf"(?<![\w-]){k}(?![\w-])", s)}
    assert doc_kinds == wf["claim_kinds"], doc_kinds
    # land and plan
    lands = [s for s in sp if "worktree land" in s and "--progress" in s]
    assert lands and set().union(*[progress_flag_names(s) for s in lands]) == wf["land_flags"]
    plans = [s for s in sp if re.search(r"\bck .*\bplan\b", s) and "--failed" in s]
    assert plans and set().union(*[progress_flag_names(s) for s in plans]) == wf["plan_flags"]
    # event commands: the create phase events carry the flags the create prompts carry; the rest are named
    for ev, flags in wf["events"].items():
        found = [s for s in sp if re.search(rf"progress event \S+ {ev}\b", s)]
        assert found, ev
        assert set().union(*[flag_names(s) for s in found]) == flags, (ev, found)
    for ev in ("task-failed", "approval-start", "approval-end"):
        assert span_with(text, rf"progress event \S+ {ev}\b"), ev
    # no flag of this family that the Workflow prompts do not carry
    allowed = wf["claim_flags"] | wf["land_flags"] | wf["plan_flags"]
    for s in sp:
        if "ck " in s:
            assert progress_flag_names(s) <= allowed, s


@pytest.mark.parametrize("kind", KINDS)
def test_tc52_agent_mode_shows_the_eta_lines_or_eta_unavailable(kind):
    text = doc("without-workflow-tool", kind)
    assert "etaLine" in text
    assert re.search(r"etaLine[^\n]*(start|begin|first)|(start|begin|first)[^\n]*etaLine", text, re.I)
    assert re.search(r"`eta`[^\n]*(landing|land)|(landing|land)[^\n]*`eta`", text, re.I), "the land JSON's eta after each landing"
    assert re.search(r"step-end[^\n]*`eta`|`eta`[^\n]*step-end|step-end[^\n]*eta", text, re.I), "step-end's eta after each phase end"
    assert "ETA unavailable" in text
