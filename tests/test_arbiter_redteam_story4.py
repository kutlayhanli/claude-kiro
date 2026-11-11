"""Red-team for Story 4's SHALL NOT clauses (run-principles 4.3, 4.5, 4.8), written from the requirements only.

4.3  a proposed cut is never applied to a spec document without my approval, including during Revise.
4.5  an approval after implementation has started is never applied automatically (the requirements are frozen).
4.8  a serves-no-requirement finding never blocks a task's landing on its own, and the code stays.

4.3 and 4.5 run the real `plugin/workflows/spec-create.js` under node. Its thin `ck` steps (the workflow's `thinCk`)
run the REAL `ck` CLI against a real git project (tests/arbiter_helpers.py), so the started/not-started decision is
read from real `tasks.md` text. Only the agents are scripted (tests/js/fake_create.mjs). 4.8 runs the real
`plugin/workflows/spec-implement.js` through the bridge, with the fake project's reviewer.

Every 4.3 case first proves the cut was really proposed (the `review: should-exist` lens ran), so a workflow with no
cut handling at all fails here rather than passing because nothing was ever proposed. Project modules are imported
inside test bodies; nothing here imports `claude_kiro`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from arbiter_helpers import DEFAULT_REQUIREMENTS, arbiter_project
from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

JS = Path(__file__).parent / "js"
pytestmark = [pytest.mark.usefixtures("no_network"), pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")]

DOCS = ("requirements.md", "design.md", "test-plan.md", "tasks.md")

# --------------------------------------------------------------------------- the create driver (real workflow, real ck)

DRIVER = r"""
import { runWorkflow } from './load_workflow.mjs'
import { stub } from './fake_create.mjs'
import { execCkRun, shellWords } from './fake_project.mjs'
const cfg = JSON.parse(process.env.REDTEAM_CFG)
const s = stub(cfg.overrides)
const calls = [], execLog = [], execResults = []
// The one `ck ...` command line of a thin step's prompt, and the here-document or fenced block it reads on stdin.
const ckLine = prompt => {
  for (const m of prompt.matchAll(/^[ \t$>*-]*ck[ \t]+(.*)$/gm)) { const w = shellWords(m[1]); if (w) return w }
  return null
}
const stdinOf = prompt => {
  const h = /<<[ \t]*(['"]?)(\w+)\1[ \t]*\n([\s\S]*?)\n\2[ \t]*$/m.exec(prompt)
  if (h) return h[3]
  const f = /```(?:json)?[ \t]*\n([\s\S]*?)\n```/.exec(prompt)
  return f ? f[1] : null
}
const agent = async (prompt, opts) => {
  calls.push({ label: opts.label, prompt, agentType: opts.agentType ?? null })
  if (opts.schema && opts.schema.properties && opts.schema.properties.exitCode) {
    const argv = ckLine(prompt)
    if (!argv) return { exitCode: 0, json: {}, stdout: '' }
    const before = execResults.length
    const json = await execCkRun(cfg.execCk, argv, { log: execLog, results: execResults, input: stdinOf(prompt) })
    const res = execResults[before] ?? { code: 0, stdout: '', stderr: '' }
    return { exitCode: res.code, json: json && typeof json === 'object' ? json : null, stdout: res.stdout, stderr: res.stderr }
  }
  return s.agent(prompt, opts)
}
let result = null, error = null
try { ({ result } = await runWorkflow(cfg.script, { agent, args: cfg.args })) } catch (e) { error = String((e && e.message) || e) }
console.log(JSON.stringify({ result, error, calls, argvs: execResults.map(r => ({ argv: r.argv, code: r.code })) }))
"""

CREATE_SCRIPT = str(Path(__file__).resolve().parent.parent / "plugin/workflows/spec-create.js")

REQ_WITH_STORY_7 = DEFAULT_REQUIREMENTS + """
### Story 7: Extras

**As a** user **I want** extras **So that** I am delighted.

**Without it:** the app is plainer.

**Acceptance Criteria:**

- 7.1 WHEN the app starts, THE SYSTEM SHALL print a joke.
- 7.2 WHEN the app stops, THE SYSTEM SHALL print a poem.
"""


def create_args(project, mode: str, **extra: Any) -> Dict[str, Any]:
    return {
        "mode": mode, "feature": "demo", "projectRoot": str(project.root), "specDir": str(project.spec_dir), "date": "2026-10-04",
        "arbiterAgent": "spec:arbiter", "runnerAgent": "spec:runner", "agents": {"arbiter": {"model": "opus", "effort": "high"}},
        **extra,
    }


def run_create(project, args: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = {"script": CREATE_SCRIPT, "args": args, "overrides": overrides or {}, "execCk": project.exec_ck}
    import os

    proc = subprocess.run(["node", "--input-type=module", "-e", DRIVER], cwd=JS, capture_output=True, text=True, timeout=300,
                          env={**os.environ, "REDTEAM_CFG": json.dumps(cfg)})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def docs(project) -> Dict[str, bytes]:
    return {name: (project.spec_dir / name).read_bytes() for name in DOCS if (project.spec_dir / name).exists()}


# --------------------------------------------------------------------------- 4.3: a proposed cut is never applied

def fixmark(marker: str) -> str:
    """The marker carried only by the fix and evidence text. The Cut the design lists in review.md (id, story, title,
    weakness) may show `marker`; the text a reviser would APPLY (fix, evidence) must never reach a revise prompt."""
    return f"{marker}-FIX"


def cut(marker: str, cid: str = "C1", story: str = "7", *, weakness: Optional[str] = None, fix: Optional[str] = None,
        severity: str = "minor", title: Optional[str] = None) -> Dict[str, Any]:
    """A cut as the should-exist lens proposes it: the Cut shape plus the finding fields every lens finding carries."""
    weakness = weakness if weakness is not None else f"a nice-to-have reason, nothing breaks without it {marker}"
    return {
        "id": cid, "story": story, "title": title or f"Extras {marker}", "weakness": weakness,
        "saves": {"criteria": 2, "tasks": 1, "testCases": 3},
        "file": "requirements.md", "location": f"Story {story}", "severity": severity,
        "problem": weakness, "evidence": f"Story {story} has a justification that does not hold {fixmark(marker)}",
        "fix": fix if fix is not None else f"Remove Story {story} {fixmark(marker)}", "needsUserDecision": True,
    }


def fidelity(problem: str, fix: str = "reword it", cid: str = "R1") -> Dict[str, Any]:
    return {"id": cid, "file": "requirements.md", "location": "1.1", "severity": "major", "problem": problem, "evidence": "e", "fix": fix, "needsUserDecision": False}


def sound(cid: str = "C1") -> Dict[str, Any]:
    return {"rulings": [{"claim": cid, "status": "ruled", "reasoning": "the justification is weak", "principle": "project:remove-first",
                         "outcome": "spec_edit", "verdict": "sound"}]}


def assert_ruled_sound(out, cid: str = "C1") -> None:
    """A malformed ruling leaves the cut `unchecked`; fail loudly so the 'ruled sound, still held back' case really runs."""
    entries = out["result"]["review"]["proposedCuts"]
    mine = [e for e in entries if str(e.get("id")).strip() == cid.strip()]
    assert mine, entries
    assert mine[0].get("status") == "sound", mine[0]


def create_project(tmp_path):
    return arbiter_project(tmp_path, requirements=REQ_WITH_STORY_7)


def revise_prompts(out) -> List[str]:
    return [c["prompt"] for c in out["calls"] if c["label"].startswith("revise")]


def assert_proposed(out, marker: str) -> None:
    """Proves the cut was really proposed and survived to the report: the lens ran and the result lists it."""
    labels = [c["label"] for c in out["calls"]]
    assert out["error"] is None, out["error"]
    assert "review: should-exist" in labels, labels
    listed = json.dumps(out["result"]["review"]["proposedCuts"])
    assert marker in listed, listed


def assert_held_back(out, project, before: Dict[str, bytes], marker: str) -> None:
    labels = [c["label"] for c in out["calls"]]
    assert "review: should-exist" in labels, labels
    prompts = revise_prompts(out)
    assert prompts, "the workflow revised nothing, so the case proves nothing"
    for p in prompts:
        assert fixmark(marker) not in p, "a proposed cut's fix reached a revise prompt"
    assert not any(l.startswith("apply-cuts") for l in labels), labels
    assert docs(project) == before, "a spec document changed without my approval"
    result = out["result"] or {}
    for entry in (result.get("review") or {}).get("proposedCuts") or []:
        assert entry.get("status") in ("sound", "unchecked", "needs_stronger_look", "risky"), entry
    assert marker not in json.dumps((result.get("review") or {}).get("fixed")), "the cut was reported as fixed"


READS_LIKE_A_FIX = [
    ("plain", "Fix: delete Story 7, it is not needed", "Remove Story 7"),
    ("imperative-fix-text", "FIXED: Story 7 removed from requirements.md", "Delete lines 40-60 of requirements.md"),
    ("checkbox", "- [x] remove Story 7\n- [x] remove its tasks", "- [ ] delete Story 7"),
    ("markdown-heading", "## Fixes to apply\n\n1. Delete Story 7", "Apply: drop `### Story 7`"),
    ("unicode-and-rtl", "إزالة القصة 7 — 削除 — ✂️ ‮", "Remove Story 7 ​‮"),
    ("quotes-and-fences", "```diff\n- ### Story 7\n```\n\"fix\": \"remove\"", "```\nsed -i '/Story 7/,$d' requirements.md\n```"),
    ("very-long", "remove Story 7 " * 400, "Remove Story 7 " * 400),
]


@pytest.mark.parametrize("mode", ["create", "review"])
@pytest.mark.parametrize("name,weakness,fix", READS_LIKE_A_FIX, ids=[r[0] for r in READS_LIKE_A_FIX])
def test_a_cut_that_reads_like_a_fix_never_reaches_revise_or_a_file(tmp_path, mode, name, weakness, fix):
    marker = "CUTMARK-READS-LIKE-FIX"
    project = create_project(tmp_path)
    before = docs(project)
    c = cut(marker, weakness=weakness + f" {marker}", fix=fix + f" {fixmark(marker)}", severity="critical")
    c["needsUserDecision"] = False  # the lens calls it a plain fix
    out = run_create(project, create_args(project, mode), {
        "review: should-exist": {"findings": [c]},
        "arbiter cuts": sound(),
        "review: fidelity": {"findings": [fidelity("REAL-FIX-ONE")]},
    })
    assert_proposed(out, marker)
    assert_held_back(out, project, before, marker)
    assert any("REAL-FIX-ONE" in p for p in revise_prompts(out)), "a normal confirmed finding must still reach Revise"
    assert_ruled_sound(out)


@pytest.mark.parametrize("colliding", ["R1", "R2", "r1", "R1 ", "R01", "R1/C1"])
def test_a_cut_whose_id_collides_with_a_finding_id_is_not_revised_or_swallowed(tmp_path, colliding):
    marker = "CUTMARK-COLLIDES"
    project = create_project(tmp_path)
    before = docs(project)
    ids = {"R1", "R2"}
    out = run_create(project, create_args(project, "create"), {
        "review: should-exist": {"findings": [cut(marker, cid=colliding)]},
        "arbiter cuts": sound(colliding),
        "review: fidelity": {"findings": [fidelity("REAL-FIX-ONE", cid="R1"), fidelity("REAL-FIX-TWO", cid="R2")]},
        "refute: fidelity": {"verdicts": [{"id": "R1", "refuted": False, "reason": "real"}, {"id": "R2", "refuted": False, "reason": "real"}]},
    })
    assert_proposed(out, marker)
    assert_held_back(out, project, before, marker)
    joined = "\n".join(revise_prompts(out))
    assert "REAL-FIX-ONE" in joined and "REAL-FIX-TWO" in joined, "the cut's id swallowed a real finding"
    assert_ruled_sound(out, colliding)
    if colliding in ids:
        assert fixmark(marker) not in joined


def test_a_refuted_finding_with_the_cuts_id_does_not_unlock_the_cut(tmp_path):
    """The refuter's verdict for finding R1 must not be read as a verdict on a cut that happens to be called R1."""
    marker = "CUTMARK-REFUTED-ID"
    project = create_project(tmp_path)
    before = docs(project)
    out = run_create(project, create_args(project, "create"), {
        "review: should-exist": {"findings": [cut(marker, cid="R1")]},
        "arbiter cuts": sound("R1"),
        "review: fidelity": {"findings": [fidelity("REAL-FIX-ONE", cid="R1")]},
        "refute: fidelity": {"verdicts": [{"id": "R1", "refuted": True, "reason": "style"}]},
    })
    assert_proposed(out, marker)
    assert_held_back(out, project, before, marker)
    assert_ruled_sound(out, "R1")


def test_a_cut_is_not_carried_through_revise_by_a_confirmed_finding_on_the_same_story(tmp_path):
    marker = "CUTMARK-SAME-STORY"
    project = create_project(tmp_path)
    before = docs(project)
    same_story = fidelity("REAL-FIX-STORY-7 wording of 7.1 is ambiguous", fix="Reword criterion 7.1 only")
    same_story["location"] = "Story 7"
    out = run_create(project, create_args(project, "create"), {
        "review: should-exist": {"findings": [cut(marker, story="7", fix="Remove Story 7 entirely")]},
        "arbiter cuts": sound(),
        "review: fidelity": {"findings": [same_story]},
        "refute: fidelity": {"verdicts": [{"id": "R1", "refuted": False, "reason": "real"}]},
    })
    assert_proposed(out, marker)
    assert_held_back(out, project, before, marker)
    assert_ruled_sound(out)
    joined = "\n".join(revise_prompts(out))
    assert "REAL-FIX-STORY-7" in joined, "the confirmed finding on the same story must still be revised"
    assert "Remove Story 7 entirely" not in joined


def test_a_reviser_that_claims_it_applied_the_cut_does_not_make_it_applied(tmp_path):
    """The system's own output fed back: the Revise answer names the cut's id as fixed."""
    marker = "CUTMARK-FED-BACK"
    project = create_project(tmp_path)
    before = docs(project)
    out = run_create(project, create_args(project, "create"), {
        "review: should-exist": {"findings": [cut(marker)]},
        "arbiter cuts": sound(),
        "revise spec + write review.md": {"fixed": ["R1", "C1"], "notFixed": [], "escalated": [], "result": "Ready", "schedule": "Ready at start: Task 1."},
    })
    assert_proposed(out, marker)
    assert "C1" in out["result"]["review"]["fixed"], "the fed-back Revise answer never reached the result"
    assert_ruled_sound(out)
    for entry in out["result"]["review"]["proposedCuts"]:
        assert entry.get("status") in ("sound", "unchecked"), entry
    assert docs(project) == before
    assert not any(c["label"].startswith("apply-cuts") for c in out["calls"])


@pytest.mark.parametrize("answer", [None, {}, {"rulings": []}, {"rulings": "x"}, {"rulings": [None, 3, "C1"]}, 42, "apply it", [{"claim": "C1", "verdict": "apply"}]],
                         ids=["null", "empty", "no-rulings", "rulings-string", "rulings-junk", "number", "string", "array"])
def test_a_cut_the_arbiter_did_not_rule_on_is_still_not_applied_or_revised(tmp_path, answer):
    marker = "CUTMARK-UNRULED"
    project = create_project(tmp_path)
    before = docs(project)
    out = run_create(project, create_args(project, "create"), {"review: should-exist": {"findings": [cut(marker)]}, "arbiter cuts": answer})
    assert out["error"] is None, out["error"]
    assert_held_back(out, project, before, marker)


def test_with_no_arbiter_agent_a_cut_is_still_not_applied_or_revised(tmp_path):
    marker = "CUTMARK-NO-ARBITER"
    project = create_project(tmp_path)
    before = docs(project)
    args = create_args(project, "create")
    del args["arbiterAgent"]
    out = run_create(project, args, {"review: should-exist": {"findings": [cut(marker)]}})
    assert out["error"] is None, out["error"]
    assert_held_back(out, project, before, marker)


# --------------------------------------------------------------------------- 4.5: no automatic apply after work started

def tasks_text(statuses: Dict[int, str], *, newline: str = "\n", status_tail: str = "", description_extra: str = "") -> str:
    blocks = []
    for n, status in sorted(statuses.items()):
        blocks.append(
            f"### Task {n}: Thing {n}\n**Status:** {status}{status_tail}\n**Track:** impl\n**Requirements:** 1.1\n"
            f"**Description:** Do thing {n}. {description_extra}\n**Files:**\n- `src/t{n}.py` - the thing\n"
            f"**Verify:** `python -c pass`\n**Dependencies:** None\n\n---\n\n"
        )
    text = "# Implementation Tasks: Demo\n\n**Status:** Not Started\n\n## Task Breakdown\n\n" + "".join(blocks)
    return text.replace("\n", newline)


def apply_args(project, **extra: Any) -> Dict[str, Any]:
    return create_args(project, "apply-cuts", cuts=[{"id": "C1", "story": "7", "title": "Extras"}], **extra)


def started_project(tmp_path, tasks: str):
    project = arbiter_project(tmp_path, requirements=REQ_WITH_STORY_7)
    path = project.spec_dir / "tasks.md"
    path.write_bytes(tasks.encode())
    return project


def assert_frozen(out, project, before: Dict[str, bytes]) -> None:
    assert out["error"] is None, out["error"]
    labels = [c["label"] for c in out["calls"]]
    assert not any(l.startswith(("apply-cuts", "apply", "revise", "write", "link")) for l in labels), labels
    assert docs(project) == before, "a cut was applied after implementation started"
    text = json.dumps(out["result"]).lower()
    assert "frozen" in text, text
    assert re.search(r"\bmine\b|\bmy to make\b|\byours\b|\byou to make\b|user's to make|\buser's\b", text), text


def assert_applies(out) -> None:
    """The positive control: nothing started, so the same approval does reach the apply step."""
    labels = [c["label"] for c in out["calls"]]
    assert out["error"] is None, out["error"]
    assert any(l.startswith("apply-cuts") for l in labels), labels


def test_positive_control_with_nothing_started_an_approved_cut_is_applied(tmp_path):
    project = started_project(tmp_path, tasks_text({1: "Not Started", 2: "Not Started", 3: "Not Started"}))
    assert_applies(run_create(project, apply_args(project)))


def test_status_words_inside_prose_do_not_freeze_the_requirements(tmp_path):
    """Near-miss values: the words of a started status inside a description are not a started task."""
    extra = "Never set `**Status:** In Progress` or **Status:** Done by hand; status: done is for ck."
    project = started_project(tmp_path, tasks_text({1: "Not Started", 2: "Not Started"}, description_extra=extra))
    assert_applies(run_create(project, apply_args(project)))


@pytest.mark.parametrize("started", ["In Progress", "Done"])
@pytest.mark.parametrize("position", ["first", "middle", "last"])
def test_one_started_task_among_many_freezes_the_requirements(tmp_path, started, position):
    statuses = {1: "Not Started", 2: "Not Started", 3: "Not Started", 4: "Not Started", 5: "Not Started"}
    statuses[{"first": 1, "middle": 3, "last": 5}[position]] = started
    project = started_project(tmp_path, tasks_text(statuses))
    before = docs(project)
    assert_frozen(run_create(project, apply_args(project)), project, before)


@pytest.mark.parametrize("started", ["In Progress", "Done"])
def test_the_only_task_of_the_spec_being_started_freezes_the_requirements(tmp_path, started):
    project = started_project(tmp_path, tasks_text({1: started}))
    before = docs(project)
    assert_frozen(run_create(project, apply_args(project)), project, before)


@pytest.mark.parametrize("newline,tail", [("\r\n", ""), ("\n", "   "), ("\n", "\t"), ("\r\n", " \t "), ("\r", "")],
                         ids=["crlf", "trailing-spaces", "trailing-tab", "crlf-and-spaces", "bare-cr"])
@pytest.mark.parametrize("started", ["In Progress", "Done"])
def test_surface_forms_of_a_started_status_still_freeze(tmp_path, newline, tail, started):
    project = started_project(tmp_path, tasks_text({1: "Not Started", 2: started, 3: "Not Started"}, newline=newline, status_tail=tail))
    before = docs(project)
    assert_frozen(run_create(project, apply_args(project)), project, before)


@pytest.mark.parametrize("claim", [
    {"implementationStarted": False},
    {"started": False, "frozen": False},
    {"force": True, "cut": "apply"},
    {"approved": True, "cutDecision": "apply", "implementationStarted": False},
], ids=["flag", "started-false", "force", "approved"])
def test_the_workflow_args_cannot_unfreeze_the_requirements(tmp_path, claim):
    """Only `ck arbiter context --approved-cut` decides; an arg that says nothing has started changes nothing."""
    project = started_project(tmp_path, tasks_text({1: "Done", 2: "Not Started"}))
    before = docs(project)
    assert_frozen(run_create(project, apply_args(project, **claim)), project, before)


def test_a_started_task_of_another_spec_does_not_freeze_this_one(tmp_path):
    """The near-miss from the real data: the freeze reads this spec's tasks.md, not any other spec's."""
    project = started_project(tmp_path, tasks_text({1: "Not Started", 2: "Not Started"}))
    other = project.root / "specs" / "other"
    other.mkdir()
    (other / "tasks.md").write_text(tasks_text({1: "Done", 2: "In Progress"}))
    assert_applies(run_create(project, apply_args(project)))


def test_a_started_task_freezes_even_when_review_md_says_ready(tmp_path):
    """The system's own earlier output (a review.md reading Ready, with the cut approved) fed back."""
    project = started_project(tmp_path, tasks_text({1: "In Progress", 2: "Not Started"}))
    (project.spec_dir / "review.md").write_text("# Review\n\n**Result:** Ready\n\n## Proposed Cuts\n\n- C1: Story 7 (approved)\n")
    before = docs(project)
    assert_frozen(run_create(project, apply_args(project)), project, before)


# --------------------------------------------------------------------------- 4.8: a no-requirement finding never blocks

ROLES = {"reviewer": {"model": "opus", "effort": "high", "enabled": True, "rounds": 1}}


def no_req(issue: str = "this helper serves no requirement", severity: str = "blocking", file: str = "src/extra.py", **more: Any) -> Dict[str, Any]:
    return {"severity": severity, "file": file, "issue": issue, "fix": "remove it", "noRequirement": True, **more}


def real(severity: str = "blocking", issue: str = "contradicts design.md") -> Dict[str, Any]:
    return {"severity": severity, "file": "src/real.py", "issue": issue, "fix": "fix it"}


def run_review(bridge, reviews: Dict[str, list], deps: Optional[Dict[str, list]] = None, **args: Any) -> Dict[str, Any]:  # noqa: F811
    deps = deps or {"1": []}
    return bridge.run({"deps": deps, "reviews": reviews}, {"agents": ROLES, **args}, clock="virtual")


def assert_lands_with_cut(out, task: str, issue: str) -> None:
    result = out["result"]
    assert result["merged"] == [task], result.get("failed")
    assert not result["failed"], result["failed"]
    assert not any(f" revise " in e and e.startswith(f"task {task} ") for e in out["events"]), "the code was revised away"
    cuts = result["proposedCuts"]
    assert any(c["task"] == task and issue in c["issue"] for c in cuts), cuts


@pytest.mark.parametrize("severity", ["blocking", "critical", "major", "high", "BLOCKING", "minor"])
def test_a_serves_no_requirement_finding_alone_never_blocks_whatever_severity_the_reviewer_gave(bridge, severity):  # noqa: F811
    issue = f"serves no requirement ({severity})"
    out = run_review(bridge, {"1": [{"verdict": "changes", "findings": [no_req(issue, severity)], "summary": "dead code"}]})
    assert_lands_with_cut(out, "1", issue)
    assert out["result"]["tasks"][0]["review"]["verdict"] == "approve"


def test_the_default_serves_no_requirement_review_lands_and_names_the_code(bridge):  # noqa: F811
    out = run_review(bridge, {"1": [{"verdict": "changes", "finding": "serves-no-requirement"}]})
    assert_lands_with_cut(out, "1", "serves no requirement")
    entry = next(c for c in out["result"]["proposedCuts"] if c["task"] == "1")
    assert entry["file"] == "specs/demo/tasks.md"


def test_a_serves_no_requirement_finding_beside_a_real_minor_finding_never_blocks(bridge):  # noqa: F811
    findings = [real("minor", "rename the variable"), no_req("serves no requirement: dead branch")]
    out = run_review(bridge, {"1": [{"verdict": "changes", "findings": findings}]})
    assert_lands_with_cut(out, "1", "dead branch")


def test_the_order_of_the_findings_does_not_matter(bridge):  # noqa: F811
    findings = [no_req("serves no requirement: dead branch"), real("minor", "rename the variable")]
    out = run_review(bridge, {"1": [{"verdict": "changes", "findings": findings}]})
    assert_lands_with_cut(out, "1", "dead branch")


def test_many_serves_no_requirement_findings_still_never_block(bridge):  # noqa: F811
    findings = [no_req(f"serves no requirement #{i}", file=f"src/extra{i}.py") for i in range(12)]
    out = run_review(bridge, {"1": [{"verdict": "changes", "findings": findings}]})
    assert out["result"]["merged"] == ["1"], out["result"].get("failed")
    named = {c["file"] for c in out["result"]["proposedCuts"]}
    assert {f"src/extra{i}.py" for i in range(12)} <= named, named


def test_the_flag_does_not_make_a_real_blocking_finding_beside_it_harmless(bridge):  # noqa: F811
    """Positive control: a real blocking finding still blocks, so the review is not simply approved."""
    findings = [real("blocking", "REAL-BLOCKER breaks the build"), no_req("serves no requirement: dead branch")]
    out = run_review(bridge, {"1": [{"verdict": "changes", "findings": findings}, "approve"]})
    assert out["result"]["merged"] == ["1"]
    assert any(e.startswith("task 1 revise") for e in out["events"]), "the real blocking finding must send the task back"
    assert "REAL-BLOCKER" in out["prompts"]["task 1 revise 1"]
    assert any("dead branch" in c["issue"] for c in out["result"]["proposedCuts"]), out["result"]["proposedCuts"]


def test_a_real_blocking_finding_that_survives_still_stops_the_landing_beside_a_cut(bridge):  # noqa: F811
    findings = [real("blocking", "REAL-BLOCKER breaks the build"), no_req("serves no requirement: dead branch")]
    out = run_review(bridge, {"1": [{"verdict": "changes", "findings": findings}, {"verdict": "changes", "findings": findings}]})
    result = out["result"]
    assert result["merged"] == [], "a surviving real blocking finding must not land"
    assert [n["task"] for n in result["needsInput"]] == ["1"], result.get("needsInput")
    assert any("dead branch" in c["issue"] for c in result["proposedCuts"]), result["proposedCuts"]


def test_a_finding_that_does_not_carry_the_flag_is_not_downgraded(bridge):  # noqa: F811
    """Near miss: text that talks about requirements but is a real blocking finding stays blocking."""
    findings = [real("blocking", "this serves the requirement badly: REAL-BLOCKER")]
    out = run_review(bridge, {"1": [{"verdict": "changes", "findings": findings}, "approve"]})
    assert any(e.startswith("task 1 revise") for e in out["events"])
    assert not out["result"]["proposedCuts"], out["result"]["proposedCuts"]


def test_the_same_finding_raised_again_at_re_review_still_never_blocks(bridge):  # noqa: F811
    """The system's own output fed back: round 2 repeats the round 1 serves-no-requirement finding."""
    f = no_req("serves no requirement: dead branch")
    out = run_review(bridge, {"1": [{"verdict": "changes", "findings": [real("blocking", "REAL-BLOCKER"), f]},
                                    {"verdict": "changes", "findings": [f]}]})
    assert out["result"]["merged"] == ["1"], out["result"].get("failed")
    assert any("dead branch" in c["issue"] for c in out["result"]["proposedCuts"])


def test_the_code_stays_no_revision_is_asked_to_remove_it(bridge):  # noqa: F811
    out = run_review(bridge, {"1": [{"verdict": "changes", "finding": "serves-no-requirement"}]})
    assert out["result"]["merged"] == ["1"]
    assert not [e for e in out["events"] if "revise" in e], out["events"]
    assert not [l for l in out["prompts"] if "revise" in l], list(out["prompts"])


def test_a_batched_review_flagging_one_task_for_a_cut_lands_both_tasks(bridge):  # noqa: F811
    flagged = no_req("serves no requirement: dead branch", task="2", file="src/two.py")
    out = run_review(bridge, {"1": [{"verdict": "changes", "findings": [flagged]}]}, deps={"1": [], "2": []}, mergeBatch=2)
    assert sorted(out["result"]["merged"]) == ["1", "2"], out["result"].get("failed")
    assert not out["result"]["failed"]
    assert any(c["task"] == "2" and c["file"] == "src/two.py" for c in out["result"]["proposedCuts"]), out["result"]["proposedCuts"]


def test_a_cut_on_one_task_does_not_hold_back_its_dependent(bridge):  # noqa: F811
    out = run_review(bridge, {"1": [{"verdict": "changes", "finding": "serves-no-requirement"}]}, deps={"1": [], "2": ["1"]})
    assert out["result"]["merged"] == ["1", "2"], out["result"].get("failed")
