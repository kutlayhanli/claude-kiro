"""The /spec:create requirements prompt and the should-this-exist lens (run-principles Story 2 and Story 4), run through the
real plugin/workflows/spec-create.js under node with the shared fake agent (tests/js/fake_create.mjs). Prompt cases read the
prompts the workflow really sends; nothing of the project's own code is mocked."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from arbiter_helpers import DEFAULT_REQUIREMENTS, DEFAULT_TASKS, arbiter_project, ck_run, decisions_entries
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


# --- run-principles Story 4: proposed cuts (TC-27 to TC-31) -----------------------------------------------------------
# These run the real spec-create.js under node with the shared fake agent, but over a real git project: the workflow's
# thin `ck` steps (an agent prompt holding one `ck ...` line and asking for `exitCode` and `json`) are run through the
# real CLI against real spec files, and the `apply-cuts` agent really rewrites those files. Nothing of the project's
# own code is mocked. Arguments the workflow reads that the spec does not pin (the `cuts` arg of `apply-cuts` mode) are
# named here once, in `CUT`.

JS = Path(__file__).parent / "js"
PLAN_MARK = "FIX-MARKER-remove-story-2-everywhere"

CUT = {
    "id": "C1",
    "story": "2",
    "title": "Farewell message",
    "weakness": "the Without-it line says only that a goodbye is nice to have",
    "saves": {"criteria": 1, "tasks": 2, "testCases": 1},
    "status": "sound",
    "ruling": None,
}

REQ = DEFAULT_REQUIREMENTS + """
### Story 2: Farewell message

**As a** user **I want** a farewell **So that** the exit feels polite.

**Without it:** a goodbye is nice to have.

**Acceptance Criteria:**

- 2.1 WHEN the app stops, THE SYSTEM SHALL print a farewell.
"""
DESIGN = "# Design: Demo\n\n## Greeting\n\nA greeting.\n\n## Farewell message\n\nPrint a farewell on stop (2.1).\n"
TEST_PLAN = (
    "# Test Plan: Demo\n\n"
    "| TC-1 | 1.1 | I | greeting printed | CLI | tests/test_greet.py |\n"
    "| TC-2 | 2.1 | I | farewell printed | CLI | tests/test_bye.py |\n"
)
TASKS = DEFAULT_TASKS + """### Task 3: Farewell tests
**Status:** Not Started
**Track:** test
**Requirements:** 2.1
**Description:** Test the farewell.
**Files:**
- `tests/test_bye.py` - the test
**Verify:** `uv run pytest -q tests/test_bye.py`
**Verifies:** Task 4 (its tests are the oracle)
**Dependencies:** None

---

### Task 4: Farewell
**Status:** Not Started
**Track:** impl
**Requirements:** 2.1
**Description:** Print the farewell.
**Files:**
- `src/bye.py` - the farewell
**Verify:** `uv run pytest -q tests/test_bye.py`
**Verified by:** Task 3 (tests/test_bye.py)
**Dependencies:** Task 3

---
"""
# What a correct removal leaves: Story 1 only, in all four documents.
REQ_AFTER = DEFAULT_REQUIREMENTS
DESIGN_AFTER = "# Design: Demo\n\n## Greeting\n\nA greeting.\n"
TEST_PLAN_AFTER = "# Test Plan: Demo\n\n| TC-1 | 1.1 | I | greeting printed | CLI | tests/test_greet.py |\n"
TASKS_AFTER = DEFAULT_TASKS
# A removal that leaves a dependency cycle in the task list: Task 1 now waits on Task 2, which waits on Task 1.
TASKS_CYCLE = DEFAULT_TASKS.replace("**Dependencies:** None", "**Dependencies:** Task 2")

DRIVER = r"""
import { runWorkflow } from './load_workflow.mjs'
import { stub } from './fake_create.mjs'
import { execCkRun } from './fake_project.mjs'
import { VirtualClock } from './virtual_clock.mjs'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const req = JSON.parse(fs.readFileSync(0, 'utf8'))
const CREATE = path.join(path.dirname(fileURLToPath(import.meta.url)), '../../plugin/workflows/spec-create.js')
const s = stub(req.overrides)
const clock = new VirtualClock()
const execLog = [], execResults = [], sent = [], labels = [], ckCalls = []

const words = line => [...line.matchAll(/"([^"]*)"|'([^']*)'|(\S+)/g)].map(m => m[1] ?? m[2] ?? m[3])
const heredoc = prompt => {
  const m = prompt.match(/<<'?(\w+)'?\n([\s\S]*?)\n\1\b/)
  return m ? m[2] : null
}
const fenced = prompt => { const m = prompt.match(/```json\n([\s\S]*?)\n```/); return m ? m[1] : null }

const agent = async (prompt, opts) => {
  const label = (opts && opts.label) || ''
  labels.push(label)
  sent.push({ label, prompt, opts: opts || {} })
  if (/^arbiter (refuted|cuts|drift)( risky)?$/.test(label)) {
    for (const [file, text] of Object.entries(req.arbiterEdits || {})) fs.writeFileSync(file, text)
  }
  const planned = req.overrides[label]
  if (planned && typeof planned === 'object' && planned.__throw) throw new Error('the arbiter threw')
  if (label.startsWith('apply-cuts')) {
    for (const [file, text] of Object.entries(req.edits || {})) fs.writeFileSync(file, text)
    if (req.applyAnswer) return req.applyAnswer
    return await s.agent(prompt, opts)
  }
  // A thin ck step: its prompt holds one `ck ...` line and asks for exitCode and json back.
  const line = prompt.split('\n').find(l => /^ck\s/.test(l.trim()))
  if (line && prompt.includes('exitCode') && !(label in req.overrides)) {
    const argv = words(line.trim().replace(/\s*<<'?\w+'?\s*$/, '')).slice(1)
    if (!/^(arbiter|lint)$/.test(argv[argv[0] === '-C' ? 2 : 0])) { ckCalls.push({ argv, code: 0, skipped: true }); return { exitCode: 0, json: {} } }
    const input = heredoc(prompt) ?? fenced(prompt)
    const out = await execCkRun(req.execCk, argv, { log: execLog, results: execResults, external: p => clock.external(p), input })
    const res = execResults[execResults.length - 1]
    ckCalls.push({ argv, code: res.code, stdout: res.stdout, stderr: res.stderr })
    return { exitCode: res.code, json: out && typeof out === 'object' ? out : {}, ...(res.code !== 0 && !(out && typeof out === 'object') ? { error: res.stderr } : {}) }
  }
  return await s.agent(prompt, opts)
}
const args = { mode: 'create', feature: 'demo', projectRoot: req.root, specDir: req.specDir, date: '2026-10-04', arbiterAgent: 'spec:arbiter', runnerAgent: 'spec:runner', agents: { arbiter: { model: 'sonnet', effort: 'medium' } }, ...req.args }
let out
try {
  const { result, logs } = await runWorkflow(CREATE, { agent, args, clock })
  out = { result, logs }
} catch (e) { out = { threw: String((e && e.message) || e), logs: [] } }
console.log(JSON.stringify({ ...out, sent, labels, ckCalls }))
"""


def run_create(project, args=None, overrides=None, edits=None, apply_answer=None, arbiter_edits=None) -> dict:
    payload = {
        "root": str(project.root),
        "specDir": str(project.spec_dir),
        "args": args or {},
        "overrides": overrides or {},
        "edits": edits or {},
        "arbiterEdits": arbiter_edits or {},
        "applyAnswer": apply_answer,
        "execCk": project.exec_ck,
    }
    proc = subprocess.run(["node", "--input-type=module", "-e", DRIVER], cwd=JS, input=json.dumps(payload), capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "threw" not in out, out["threw"]
    return out


def cut_project(tmp_path, started=False):
    tasks = TASKS.replace("### Task 1: Greeting tests\n**Status:** Not Started", "### Task 1: Greeting tests\n**Status:** In Progress") if started else TASKS
    project = arbiter_project(tmp_path, tasks=tasks, requirements=REQ)
    (project.spec_dir / "design.md").write_text(DESIGN)
    (project.spec_dir / "test-plan.md").write_text(TEST_PLAN)
    return project


def spec_files(project) -> dict:
    return {n: (project.spec_dir / n).read_text() for n in ("requirements.md", "design.md", "test-plan.md", "tasks.md")}


def cut_lens_answer() -> dict:
    return {"findings": [{
        "id": "C1", "file": "requirements.md", "location": "Story 2", "severity": "minor", "needsUserDecision": False,
        "problem": CUT["weakness"], "evidence": "Story 2's Without-it line", "fix": PLAN_MARK,
        "story": CUT["story"], "title": CUT["title"], "weakness": CUT["weakness"], "saves": CUT["saves"],
    }]}


def cut_ruling() -> dict:
    return {"rulings": [{
        "claim": "C1", "id": "C1", "status": "ruled", "outcome": "spec_edit", "verdict": "sound", "changed": "nothing",
        "reasoning": "the justification is weak and the cut saves work", "principle": "project:remove-first",
    }]}


def story_gone(files: dict) -> bool:
    return all("Story 2" not in t and "Farewell" not in t and "2.1" not in t and "test_bye" not in t and "Task 3" not in t for t in files.values())


def as_text(out: dict) -> str:
    return json.dumps(out.get("result")) + "\n" + "\n".join(out.get("logs") or [])


def surviving_cut(tmp_path):
    project = cut_project(tmp_path)
    out = run_create(project, overrides={"review: should-exist": cut_lens_answer(), "arbiter cuts": cut_ruling(), "arbiter cuts risky": cut_ruling()})
    assert "review: should-exist" in out["labels"], "the should-exist lens never ran"
    return project, out


def test_tc27_a_surviving_cut_is_listed_with_story_weakness_and_counts(tmp_path):
    _, out = surviving_cut(tmp_path)
    cuts = (out["result"].get("review") or {}).get("proposedCuts")
    assert cuts, f"review.proposedCuts is missing or empty: {out['result']}"
    cut = cuts[0]
    assert str(cut["story"]) == "2" or "Farewell" in str(cut.get("title", ""))
    assert "nice to have" in cut["weakness"]
    assert cut["saves"] == CUT["saves"]
    # review.md is written by the reviser from its prompt, which holds the section: story, weakness and the counts.
    # design.md's flowchart: "Revise: confirmed findings, review.md, sound cuts listed, never applied". So the revise prompt
    # lists the cut for review.md's Proposed cuts section, but never carries its fix as an edit to make (TC-28, PLAN_MARK).
    revise = [s["prompt"] for s in out["sent"] if s["label"].startswith("revise")]
    assert revise, out["labels"]
    assert all(CUT["weakness"] in p and "Farewell" in p for p in revise), "the revise prompt must carry the cut for review.md's section"
    assert all(re.search(r"propos\w* cuts?|cuts? proposed", p, re.I) for p in revise)


def test_tc28_a_surviving_cut_is_not_applied_and_no_revise_prompt_asks_for_it(tmp_path):
    project, out = surviving_cut(tmp_path)
    after = spec_files(project)
    assert after["requirements.md"] == REQ and after["tasks.md"] == TASKS  # the run left the documents alone
    revise = [s["prompt"] for s in out["sent"] if s["label"].startswith("revise")]
    assert revise, out["labels"]
    assert all(PLAN_MARK not in p for p in revise), "a revise prompt carries the cut's fix as a fix to apply"
    assert not any(label.startswith("apply-cuts") for label in out["labels"])
    assert re.search(r"not applied|never applied|awaiting (my )?approval|not removed|until (you|I) approve", as_text(out), re.I), as_text(out)


def test_tc29_an_approved_cut_with_nothing_started_removes_the_story_and_lint_finds_nothing_new(tmp_path):
    project = cut_project(tmp_path)
    assert ck_run(project.root, "lint", "demo", "--json").returncode == 0
    edits = {str(project.spec_dir / n): t for n, t in
             {"requirements.md": REQ_AFTER, "design.md": DESIGN_AFTER, "test-plan.md": TEST_PLAN_AFTER, "tasks.md": TASKS_AFTER}.items()}
    out = run_create(project, args={"mode": "apply-cuts", "cuts": [CUT]}, edits=edits,
                     apply_answer={"applied": ["C1"], "summary": "edited the four documents for C1"})
    assert sum(1 for label in out["labels"] if label.startswith("apply-cuts")) == 1, out["labels"]
    context = [c for c in out["ckCalls"] if "context" in c["argv"] and "--approved-cut" in c["argv"]]
    assert context, f"ck arbiter context --approved-cut never ran: {out['ckCalls']}"
    lints = [c for c in out["ckCalls"] if "lint" in c["argv"]]
    assert lints and all(c["code"] == 0 for c in lints), f"ck lint must run on the edited files and pass: {lints}"
    files = spec_files(project)
    assert story_gone(files), files
    assert ck_run(project.root, "lint", "demo", "--json").returncode == 0
    assert not reports_lint_failure(as_text(out)), as_text(out)


# A reported lint failure, not a success line that negates one ("ck lint: no new failure", "no lint failures", "without a
# new failure"). Negated phrases are dropped first, then the failure wording is searched; the self-checks below pin both sides.
NEGATED = re.compile(r"\b(no|not|without|zero|0|never)\b(\s+(a|an|any|new|lint|dependency))*\s+(failures?|failed|cycles?)\b", re.I)
LINT_FAILURE = re.compile(r"new (lint )?failure|dependency cycle|lint (failed|failure)", re.I)


def reports_lint_failure(text: str) -> bool:
    return bool(LINT_FAILURE.search(NEGATED.sub(" ", text)))


@pytest.mark.parametrize("text", ["ck lint: no new failure", "no new lint failure", "no lint failures", "lint passed without a new failure",
                                  "found no dependency cycle", "0 new failures"])
def test_tc29_lint_failure_check_ignores_negated_success_lines(text):
    assert not reports_lint_failure(text)


@pytest.mark.parametrize("text", ["ck lint found a new failure", "1 new lint failure: dependency cycle", "lint failed after the edit",
                                  "Task 1 -> Task 2 -> Task 1: dependency cycle"])
def test_tc29_lint_failure_check_still_catches_a_reported_failure(text):
    assert reports_lint_failure(text)


def test_tc30_a_removal_that_leaves_a_lint_failure_is_not_reported_as_applied(tmp_path):
    project = cut_project(tmp_path)
    edits = {str(project.spec_dir / n): t for n, t in
             {"requirements.md": REQ_AFTER, "design.md": DESIGN_AFTER, "test-plan.md": TEST_PLAN_AFTER, "tasks.md": TASKS_CYCLE}.items()}
    out = run_create(project, args={"mode": "apply-cuts", "cuts": [CUT]}, edits=edits,
                     apply_answer={"applied": ["C1"], "summary": "edited the four documents for C1"})
    assert sum(1 for label in out["labels"] if label.startswith("apply-cuts")) == 1, out["labels"]
    lints = [c for c in out["ckCalls"] if "lint" in c["argv"]]
    assert lints and lints[-1]["code"] != 0, f"real ck lint must fail on the cycle the edit left: {lints}"
    text = as_text(out)
    assert re.search(r"cycle", text, re.I), f"the run must show the lint finding: {text}"
    # `applied` is not a designed result key; this guards only the obvious name. The text check below is what pins the claim.
    applied = (out["result"] or {}).get("applied") if isinstance(out["result"], dict) else None
    assert not applied, f"the cut is reported as applied although lint failed: {out['result']}"
    assert not re.search(r"(?<!not )(?<!never )\bcut (was |is )?applied|(?<!not )story (was )?removed", text, re.I), text


def test_tc31_an_approved_cut_after_a_task_started_is_not_applied_and_the_requirements_are_frozen(tmp_path):
    project = cut_project(tmp_path, started=True)
    before = spec_files(project)
    out = run_create(project, args={"mode": "apply-cuts", "cuts": [CUT]}, edits={str(project.spec_dir / "requirements.md"): REQ_AFTER})
    assert not any(label.startswith("apply-cuts") for label in out["labels"]), out["labels"]
    context = [c for c in out["ckCalls"] if "context" in c["argv"] and "--approved-cut" in c["argv"]]
    assert context and "frozen" in context[-1]["stdout"], f"ck arbiter context --approved-cut must report the cut frozen: {out['ckCalls']}"
    assert spec_files(project) == before
    text = as_text(out)
    assert re.search(r"frozen", text, re.I), text
    assert re.search(r"\b(yours|mine|your (own )?(change|call)|you (make|decide)|change is (yours|mine))\b|to make", text, re.I), text


# --- run-principles Story 11: the arbiter's steps in /spec:create (TC-97 to TC-158) --------------------------------------
# Same driver: the real spec-create.js under node, the thin `ck arbiter ...` steps run through the real CLI over a real git
# project, so the rulings are validated and recorded by the real `ck arbiter review`, not read from the fake's answer. The
# design pins one call per step (`arbiter refuted`, `arbiter cuts`, `arbiter drift`, each with a ` risky` suffix) answering
# one ruling per finding or cut, keyed by `claim`. A drift ruling is keyed by an id the arbiter gives it (`D1` here).

ARBITER_ARGS = {
    "arbiterAgent": "spec:arbiter",
    "runnerAgent": "spec:runner",
    "agents": {"arbiter": {"model": "sonnet", "effort": "medium", "risky_model": "opus", "risky_effort": "high"}},
}
REVISE_PREFIX = "revise"
REVIEW_MD = "# Review: Demo\n\n**Result:** Ready\n\n## Needs Your Decision\n\n(none)\n"
DECIDED = "retries are capped at 3"


def finding(fid: str, **extra) -> dict:
    return {"id": fid, "file": "requirements.md", "location": fid, "severity": "major", "problem": f"problem {fid}",
            "evidence": "e", "fix": f"fix {fid}", "needsUserDecision": False, **extra}


def refuting(*refuted_ids: str, ids=("R1", "R2", "R3")) -> dict:
    """Overrides: fidelity finds `ids`; the refuter refutes exactly `refuted_ids`; the reviser calls the spec Ready."""
    return {
        "review: fidelity": {"findings": [finding(i) for i in ids]},
        "refute: fidelity": {"verdicts": [{"id": i, "refuted": i in refuted_ids, "reason": f"reason {i}"} for i in ids]},
        "revise spec + write review.md": {"fixed": [i for i in ids if i not in refuted_ids], "notFixed": [], "escalated": [],
                                          "result": "Ready", "schedule": "s"},
    }


def ruling(claim: str, outcome: str = "spec_edit", verdict: str = "refutation stands", **extra) -> dict:
    changed = {"changed": "nothing"} if outcome == "spec_edit" else {}
    return {"claim": claim, "status": "ruled", "outcome": outcome, "reasoning": f"because {claim}", "principle": "project:test-intent",
            "verdict": verdict, **changed, **extra}


def drift_requirement(wording: str) -> dict:
    return {"claim": "D1", "status": "ruled", "outcome": "requirements", "reasoning": "the draft drifted from PLAN.md",
            "principle": "project:test-intent", "verdict": "drift",
            "requirement": {"kind": "change", "wording": wording, "reason": f"PLAN.md decided: {DECIDED}"}}


def plan_project(tmp_path, plan=None):
    """A real project with PLAN.md (the default, or `plan`), principles, and a review.md for `ck arbiter review` to append to."""
    project = arbiter_project(tmp_path, **({} if plan is None else {"plan": plan}))
    (project.spec_dir / "review.md").write_text(REVIEW_MD)
    return project


def no_plan_project(tmp_path):
    project = arbiter_project(tmp_path, plan=None)
    (project.spec_dir / "review.md").write_text(REVIEW_MD)
    return project


def create(project, overrides, args=None, **kw) -> dict:
    return run_create(project, args={**ARBITER_ARGS, **(args or {})}, overrides=overrides, **kw)


def arbiter_calls(out: dict, step: str) -> list:
    return [l for l in out["labels"] if re.fullmatch(rf"arbiter {step}( risky)?", l)]


def review_calls(out: dict, step: str) -> list:
    return [c for c in out["ckCalls"] if "arbiter" in c["argv"] and "review" in c["argv"] and step in c["argv"]]


def sent_for(out: dict, label: str) -> list:
    return [s for s in out["sent"] if s["label"] == label]


def revise_prompts(out: dict) -> list:
    return [s["prompt"] for s in out["sent"] if s["label"].startswith(REVISE_PREFIX)]


def review_of(out: dict) -> dict:
    review = (out["result"] or {}).get("review")
    assert review, f"the result has no review: {out['result']}"
    return review


def arbiter_of(out: dict) -> dict:
    arb = review_of(out).get("arbiter")
    assert arb is not None, f"review.arbiter is missing: {review_of(out)}"
    return arb


def entries_of(project) -> list:
    return max((decisions_entries(project, where) for where in ("root", "target")), key=len)


def blob(out: dict) -> str:
    return json.dumps({"result": out["result"], "logs": out["logs"]}, sort_keys=True)


def two_cuts() -> dict:
    first = cut_lens_answer()["findings"][0]
    second = {**first, "id": "C2", "story": "3", "title": "Second cut", "weakness": "SECOND-CUT-WEAKNESS nothing breaks without it"}
    return {"findings": [first, second]}


def test_tc97_each_refuted_finding_gets_exactly_one_ruling_neither_dropped_nor_voted(tmp_path):
    project = plan_project(tmp_path)
    answer = {"rulings": [ruling("R1", verdict="finding stands"), ruling("R2"), ruling("R3")]}
    out = create(project, {**refuting("R1", "R2", "R3"), "arbiter refuted": answer})
    assert len(arbiter_calls(out, "refuted")) == 1, out["labels"]
    prompt = sent_for(out, "arbiter refuted")[0]["prompt"]
    assert all(fid in prompt for fid in ("R1", "R2", "R3")), "the arbiter step must carry every refuted finding"
    calls = review_calls(out, "refuted")
    assert calls and all(c["code"] == 0 for c in calls), f"the real `ck arbiter review --step refuted` must accept the answer: {calls}"
    arb = arbiter_of(out)
    assert "refuted" in arb["ran"] and "refuted" not in arb["lost"], arb
    assert len(arb["rulings"]) == 3 and len(set(arb["rulings"])) == 3, f"one recorded ruling per refuted finding: {arb}"
    assert len(entries_of(project)) == 3
    assert review_of(out)["refutedCount"] == 3, "no refuted finding is dropped"


def test_tc98_a_finding_ruled_standing_is_revised_and_one_ruled_refuted_stays_listed_with_its_ruling(tmp_path):
    project = plan_project(tmp_path)
    answer = {"rulings": [ruling("R1", verdict="finding stands", reasoning="STANDS-R1 the finding is right"),
                          ruling("R3", verdict="refutation stands", reasoning="REFUTED-R3 the refuter is right")]}
    out = create(project, {**refuting("R1", "R3"), "arbiter refuted": answer})
    assert len(review_calls(out, "refuted")) >= 1, out["ckCalls"]
    prompts = revise_prompts(out)
    assert prompts, out["labels"]
    for prompt in prompts:
        assert "problem R1" in prompt, "a finding ruled standing joins the confirmed set in the revise prompt"
        assert "REFUTED-R3" in prompt, "a finding ruled refuted stays listed with its ruling"
        near = prompt[max(0, prompt.index("REFUTED-R3") - 600): prompt.index("REFUTED-R3") + 600]
        assert re.search(r"refut", near, re.I), near


def test_tc99_with_a_plan_the_drift_step_runs_after_revise_with_fixes_and_with_none(tmp_path):
    for fixed in (["R1"], []):
        project = plan_project(tmp_path / f"fixes-{len(fixed)}")
        over = {"review: fidelity": {"findings": [finding("R1")]}, "refute: fidelity": {"verdicts": [{"id": "R1", "refuted": False, "reason": "real"}]},
                "revise spec + write review.md": {"fixed": fixed, "notFixed": [], "escalated": [], "result": "Ready", "schedule": "s"}}
        if not fixed:
            over = {"review: fidelity": {"findings": []}, "revise spec + write review.md": over["revise spec + write review.md"]}
        out = create(project, over)
        drift = [i for i, l in enumerate(out["labels"]) if l == "arbiter drift"]
        revise = [i for i, l in enumerate(out["labels"]) if l.startswith(REVISE_PREFIX)]
        assert drift and revise and min(drift) > max(revise), (fixed, out["labels"])
        assert review_calls(out, "drift"), out["ckCalls"]
    absent = create(no_plan_project(tmp_path / "no-plan"), refuting("R3"))
    assert "arbiter drift" not in absent["labels"], absent["labels"]
    assert not review_calls(absent, "drift")


def test_tc100_each_cut_gets_a_ruling_a_rejected_cut_is_absent_from_the_report_and_in_decisions_md(tmp_path):
    project = cut_project(tmp_path)
    answer = {"rulings": [cut_ruling()["rulings"][0], {**cut_ruling()["rulings"][0], "claim": "C2", "id": "C2", "verdict": "unsound"}]}
    out = create(project, {"review: should-exist": two_cuts(), "arbiter cuts": answer})
    assert len(arbiter_calls(out, "cuts")) == 1, out["labels"]
    prompt = sent_for(out, "arbiter cuts")[0]["prompt"]
    assert "C1" in prompt and "C2" in prompt, "the step carries each cut"
    calls = review_calls(out, "cuts")
    assert calls and all(c["code"] == 0 for c in calls), calls
    arb = arbiter_of(out)
    assert len(arb["rulings"]) == 2, f"each cut gets a ruling in the record: {arb}"
    cuts = review_of(out).get("proposedCuts") or []
    assert [str(c["id"]) for c in cuts] == ["C1"], f"only the cut found sound is shown: {cuts}"
    assert "SECOND-CUT-WEAKNESS" not in blob(out), "the rejected cut is absent from the report"
    assert all("SECOND-CUT-WEAKNESS" not in p for p in revise_prompts(out)), "and from the revise prompt"
    assert len(entries_of(project)) == 2, "both rulings, the rejected cut's too, are in decisions.md"


def test_tc102_a_drift_ruling_that_would_change_a_decision_is_not_applied_and_is_reported(tmp_path):
    project = plan_project(tmp_path, plan=f"# Plan: Demo\n\n## Decisions\n\n- {DECIDED}\n")
    before = spec_files(project)
    wording = "retries are capped at 5"
    out = create(project, {**refuting(), "review: fidelity": {"findings": []}, "arbiter drift": {"rulings": [drift_requirement(wording)]}})
    assert review_calls(out, "drift"), out["ckCalls"]
    assert spec_files(project) == before, "a plan-changing ruling is never applied to the spec"
    assert wording in (project.spec_dir / "review.md").read_text(), "review.md reports the change"
    assert wording in blob(out), "and so does the workflow's result"
    assert all(wording not in p for p in revise_prompts(out)), "Revise ran before it, so it is never asked to apply it"


def test_tc104_an_arbiter_change_with_revise_applying_no_fixes_still_runs_the_consistency_check(tmp_path):
    project = plan_project(tmp_path)
    reworded = (project.spec_dir / "requirements.md").read_text().replace("print a greeting", "print a welcome greeting")
    edited = {"rulings": [ruling("D1", verdict="drift", changed="requirements.md: reworded 1.1 to match PLAN.md")]}
    over = {**refuting(), "review: fidelity": {"findings": []}, "arbiter drift": edited}
    out = create(project, over, arbiter_edits={str(project.spec_dir / "requirements.md"): reworded})
    labels = out["labels"]
    assert "arbiter drift" in labels, labels
    later = [l for l in labels[labels.index("arbiter drift"):] if l.startswith("consistency check")]
    assert later, f"the arbiter changed requirements.md, so the consistency check must run after it: {labels}"
    quiet = create(plan_project(tmp_path / "quiet"), {**refuting(), "review: fidelity": {"findings": []}, "arbiter drift": {"rulings": [ruling("D1", verdict="no drift")]}})
    assert not [l for l in quiet["labels"] if l.startswith("consistency check")], "control: nothing changed, no fixes, no check"


def test_tc105_rulings_start_normal_rerun_once_at_risky_one_per_finding_and_reach_decisions_md(tmp_path):
    project = plan_project(tmp_path)
    again = {**ruling("R3"), "status": "needs_stronger_look", "established": "the refuter's reason holds in part"}
    first = {"rulings": [ruling("R1"), again, ruling("R2")]}
    second = {"rulings": [ruling("R3", reasoning="RISKY-R3 settled at the risky tier")]}
    out = create(project, {**refuting("R1", "R2", "R3"), "arbiter refuted": first, "arbiter refuted risky": second})
    labels = [l for l in out["labels"] if l.startswith("arbiter refuted")]
    assert labels == ["arbiter refuted", "arbiter refuted risky"], f"normal first, then one risky re-run: {labels}"
    assert "R3" in sent_for(out, "arbiter refuted risky")[0]["prompt"]
    assert "RISKY-R3" not in sent_for(out, "arbiter refuted risky")[0]["prompt"]
    tiers = [c["argv"][c["argv"].index("--tier") + 1] for c in review_calls(out, "refuted") if "--tier" in c["argv"]]
    assert tiers[:1] == ["normal"] and [t for t in tiers if t == "risky"] == ["risky"], tiers
    arb = arbiter_of(out)
    refuted = ["R1", "R2", "R3"]
    assert len(arb["rulings"]) == len(refuted), f"one ruling per finding, the stronger-look entry not counted twice: {arb}"
    entries = entries_of(project)
    assert len(entries) == len(refuted) and all(e.get("Commit") == "none" for e in entries), entries


BAD_ENTRIES = [
    pytest.param({"outcome": "REVERT"}, id="unknown-outcome"),
    pytest.param({"principle": "project:made-up"}, id="uncited-principle"),
    pytest.param({"principle": ""}, id="no-principle"),
    pytest.param({"principle": None}, id="null-principle"),
    pytest.param({"outcome": ["code", "spec_edit"]}, id="two-outcomes"),
    pytest.param({"reasoning": ""}, id="empty-reasoning"),
    pytest.param({"claim": "R9"}, id="claim-of-no-finding"),
    pytest.param({"status": 3, "reasoning": {"bad": True}}, id="wrong-types"),
]


@pytest.mark.parametrize("bad", BAD_ENTRIES)
def test_tc105_an_entry_invalid_in_the_same_forms_as_tc54_is_not_a_ruling_and_is_not_recorded(tmp_path, bad):
    project = plan_project(tmp_path)
    out = create(project, {**refuting("R1", "R2"), "arbiter refuted": {"rulings": [ruling("R1"), {**ruling("R2"), **bad}]}})
    arb = arbiter_of(out)
    assert len(arb["rulings"]) == 1, f"only the valid entry is a ruling: {arb}"
    assert len(entries_of(project)) == 1
    assert review_of(out)["result"] != "Ready"


def test_tc105_two_entries_for_one_finding_are_one_ruling(tmp_path):
    project = plan_project(tmp_path)
    out = create(project, {**refuting("R1", "R2"), "arbiter refuted": {"rulings": [ruling("R1"), ruling("R1", verdict="finding stands"), ruling("R2")]}})
    refuted = ["R1", "R2"]
    assert len(arbiter_of(out)["rulings"]) == len(refuted)
    assert len(entries_of(project)) == len(refuted)


def test_tc107_with_no_refuted_finding_and_no_cut_neither_step_runs(tmp_path):
    project = plan_project(tmp_path)
    out = create(project, {**refuting(), "review: should-exist": {"findings": []}})
    assert arbiter_calls(out, "refuted") == [] and arbiter_calls(out, "cuts") == [], out["labels"]
    assert not review_calls(out, "refuted") and not review_calls(out, "cuts"), out["ckCalls"]
    arb = arbiter_of(out)
    assert "refuted" not in arb["ran"] and "cuts" not in arb["ran"] and arb["lost"] == [], arb
    control = create(plan_project(tmp_path / "control"), refuting("R3"))
    assert arbiter_calls(control, "refuted"), "control: a refuted finding does reach the arbiter"


@pytest.mark.parametrize("junk", [{"__throw": True}, None, {}, 7, "a string", [], {"rulings": "not a list"}, {"rulings": [None, 7, "x"]}],
                         ids=["throws", "null", "empty", "number", "string", "array", "rulings-not-a-list", "junk-entries"])
def test_tc108_a_throwing_or_junk_arbiter_leaves_findings_as_they_stood_and_is_not_ready(tmp_path, junk):
    project = plan_project(tmp_path)
    out = create(project, {**refuting("R3"), "arbiter refuted": junk})
    review = review_of(out)
    assert review["result"] != "Ready", review["result"]
    arb = arbiter_of(out)
    assert "refuted" in arb["lost"] and arb["rulings"] == [], arb
    assert review["refutedCount"] == 1, "the finding stays refuted, neither dropped nor confirmed"
    assert entries_of(project) == []
    assert re.search(r"did not run|not run|lost", blob(out), re.I), blob(out)


@pytest.mark.parametrize("how", ["missing", "directory"])
def test_tc141_unusable_principles_file_means_no_arbiter_call_findings_stand_and_the_file_is_named(tmp_path, how):
    project = plan_project(tmp_path)
    principles = project.root / "specs" / "PRINCIPLES.md"
    principles.unlink()
    if how == "directory":
        principles.mkdir()
    out = create(project, refuting("R3"))
    assert not [l for l in out["labels"] if re.fullmatch(r"arbiter (refuted|cuts|drift)( risky)?", l)], f"no arbiter label may run: {out['labels']}"
    review = review_of(out)
    assert review["result"] != "Ready"
    assert review["refutedCount"] == 1
    arb = arbiter_of(out)
    assert "PRINCIPLES.md" in str(arb.get("missing")), arb
    assert "refuted" in arb["lost"], arb
    text = blob(out) + "\n" + "\n".join(revise_prompts(out))
    assert "ck principles init" in text and "PRINCIPLES.md" in text, "review.md's reviser and the report name the file and the command"


def _tc148_run(tmp_path):
    project = plan_project(tmp_path)
    again = {**ruling("R3"), "status": "needs_stronger_look", "established": "part"}
    over = {**refuting("R1", "R3"), "review: should-exist": cut_lens_answer(), "arbiter refuted": {"rulings": [ruling("R1"), again]},
            "arbiter refuted risky": {"rulings": [ruling("R3")]}, "arbiter cuts": cut_ruling()}
    out = create(project, over)
    steps = [s for s in out["sent"] if re.fullmatch(r"arbiter (refuted|cuts|drift)( risky)?", s["label"])]
    return out, steps


def _tc148_check_steps(steps):
    for s in steps:
        opts = s["opts"]
        assert opts.get("agentType") == "spec:arbiter", (s["label"], opts)
        want = ("opus", "high") if s["label"].endswith("risky") else ("sonnet", "medium")
        assert (opts.get("model"), opts.get("effort")) == want, (s["label"], opts)
        assert "ck progress" not in s["prompt"], f"{s['label']} must be a plain agent whose prompt holds no `ck progress` line"


def test_tc148_each_arbiter_step_starts_with_the_arbiter_agent_type_and_tier_options_by_plain_agent(tmp_path):
    out, steps = _tc148_run(tmp_path)
    assert {s["label"] for s in steps} >= {"arbiter refuted", "arbiter refuted risky", "arbiter cuts"}, out["labels"]
    _tc148_check_steps(steps)
    thin = [s for s in out["sent"] if re.search(r"^\s*ck\s.*\barbiter\b", s["prompt"], re.M)]
    assert thin, "the `ck arbiter` steps never ran as thin steps"
    assert all(s["opts"].get("agentType") == "spec:runner" for s in thin), [(s["label"], s["opts"]) for s in thin]


def test_arbiter_drift_step_starts_with_the_arbiter_agent_type_and_tier_options_by_plain_agent(tmp_path):
    out, steps = _tc148_run(tmp_path)
    assert "arbiter drift" in {s["label"] for s in steps}, out["labels"]
    _tc148_check_steps([s for s in steps if s["label"] == "arbiter drift"])


def test_tc148_with_no_arbiter_agent_the_steps_with_subjects_are_lost_and_the_result_is_not_ready(tmp_path):
    project = plan_project(tmp_path)
    out = create(project, {**refuting("R3"), "review: should-exist": cut_lens_answer()}, args={"arbiterAgent": None})
    assert not [l for l in out["labels"] if re.fullmatch(r"arbiter (refuted|cuts|drift)( risky)?", l)], out["labels"]
    arb = arbiter_of(out)
    assert "refuted" in arb["lost"] and "cuts" in arb["lost"], arb
    assert review_of(out)["result"] != "Ready"


def test_tc158_a_requirements_ruling_lands_in_needs_your_decision_and_the_result_is_ready_after_decisions(tmp_path):
    project = plan_project(tmp_path, plan=f"# Plan: Demo\n\n## Decisions\n\n- {DECIDED}\n")
    wording = "retries are capped at 5"
    out = create(project, {**refuting(), "review: fidelity": {"findings": []}, "arbiter drift": {"rulings": [drift_requirement(wording)]}})
    calls = review_calls(out, "drift")
    assert calls and all(c["code"] == 0 for c in calls), calls
    text = (project.spec_dir / "review.md").read_text()
    decision_section = text.split("## Needs Your Decision", 1)[1]
    assert wording in decision_section, f"the requirements item is a row of Needs Your Decision: {text}"
    assert re.search(r"\*\*Result:\*\*\s*Ready after decisions", text), text
    assert review_of(out)["result"] == "Ready after decisions", "review.md and the workflow's result agree"


def test_tc158_a_lost_drift_step_sets_needs_work_in_review_md_and_in_the_result(tmp_path):
    project = plan_project(tmp_path)
    out = create(project, {**refuting(), "review: fidelity": {"findings": []}, "arbiter drift": {"__throw": True}})
    calls = review_calls(out, "drift")
    assert calls and all(c["code"] == 0 for c in calls), f"a lost drift step still calls ck arbiter review, with output null: {out['ckCalls']}"
    text = (project.spec_dir / "review.md").read_text()
    assert re.search(r"\*\*Result:\*\*\s*Needs work", text), text
    assert review_of(out)["result"] == "Needs work"
    assert "drift" in arbiter_of(out)["lost"]


# --- run-principles properties for rulings during /spec:create (P-28, P-29, P-30, P-42, P-46) ---------------------------
# Each example runs the real spec-create.js under node over a real git project, with the thin `ck arbiter ...` steps run
# through the real CLI (no server). P-42 calls the decision seam `arbiter_policy.decide_cut` directly.

import itertools  # noqa: E402

from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402
from arbiter_helpers import arbiter_failures  # noqa: E402

PROPERTIES = {
    "P-28": "test_p28_every_refuted_finding_gets_exactly_one_ruling_and_none_is_lost",
    "P-29": "test_p29_no_cut_and_no_plan_changing_ruling_reaches_a_revise_prompt_but_normal_fixes_do",
    "P-30": "test_p30_each_cut_gets_one_ruling_a_failed_one_is_unchanged_and_the_result_is_not_ready",
    "P-42": "test_p42_decide_cut_applies_only_when_approved_and_nothing_has_started",
    "P-46": "test_p46_with_no_refuted_finding_and_no_cut_no_arbiter_step_runs",
}

PROP = settings(max_examples=30, derandomize=True, database=None, deadline=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
_EXAMPLE = itertools.count()


def fresh(tmp_path):
    return tmp_path / f"ex{next(_EXAMPLE)}"


def lens_findings(prefix: str, n: int) -> list:
    return [finding(f"{prefix}{i + 1}") for i in range(n)]


@st.composite
def finding_mixes(draw):
    """Two lenses, each with 0-3 findings; each finding refuted or not; each refuted one ruled either way."""
    lenses = {}
    for key, prefix in (("fidelity", "R"), ("implementability", "T")):
        n = draw(st.integers(0, 3))
        lenses[key] = [(f"{prefix}{i + 1}", draw(st.booleans()), draw(st.sampled_from(["finding stands", "refutation stands"]))) for i in range(n)]
    return lenses


def mix_overrides(mix: dict) -> dict:
    over = {"revise spec + write review.md": {"fixed": [], "notFixed": [], "escalated": [], "result": "Ready", "schedule": "s"}}
    for key, items in mix.items():
        over[f"review: {key}"] = {"findings": [finding(i) for i, _, _ in items]}
        if items:
            over[f"refute: {key}"] = {"verdicts": [{"id": i, "refuted": r, "reason": f"reason {i}"} for i, r, _ in items]}
    return over


@PROP
@given(mix=finding_mixes())
def test_p28_every_refuted_finding_gets_exactly_one_ruling_and_none_is_lost(tmp_path, mix):
    project = plan_project(fresh(tmp_path))
    items = [x for v in mix.values() for x in v]
    refuted = [(i, v) for i, r, v in items if r]
    over = mix_overrides(mix)
    over["arbiter refuted"] = {"rulings": [ruling(i, verdict=v) for i, v in refuted]}
    out = create(project, over)
    if not refuted:
        assert arbiter_calls(out, "refuted") == [], out["labels"]
    else:
        assert len(arbiter_calls(out, "refuted")) == 1, out["labels"]
        prompt = sent_for(out, "arbiter refuted")[0]["prompt"]
        assert all(i in prompt for i, _ in refuted), "the step carries every refuted finding"
        arb = arbiter_of(out)
        assert len(arb["rulings"]) == len(refuted) and len(set(arb["rulings"])) == len(refuted), arb
        assert len(entries_of(project)) == len(refuted)
    # Conservation: confirmed plus refuted is every finding; each reaches the revise prompt as a fix or a listed refutation.
    prompts = revise_prompts(out)
    assert prompts, out["labels"]
    for p in prompts:
        assert all(f"problem {i}" in p for i, _, _ in items), "a finding was dropped"


def cut_finding(i: int, fix: str) -> dict:
    n = i + 2
    return {**cut_lens_answer()["findings"][0], "id": f"C{i + 1}", "story": str(n), "title": f"Cut {n}", "fix": fix,
            "weakness": f"WEAK-{i + 1} nothing breaks without it"}


def cut_rulings(verdicts: dict) -> dict:
    return {"rulings": [{**cut_ruling()["rulings"][0], "claim": cid, "id": cid, "verdict": v} for cid, v in verdicts.items()]}


def cuts_project(tmp_path, plan=None):
    kw = {} if plan is None else {"plan": plan}
    project = arbiter_project(tmp_path, tasks=TASKS, requirements=REQ, **kw)
    (project.spec_dir / "design.md").write_text(DESIGN)
    (project.spec_dir / "test-plan.md").write_text(TEST_PLAN)
    (project.spec_dir / "review.md").write_text(REVIEW_MD)
    return project


@PROP
@given(verdicts=st.lists(st.sampled_from(["sound", "unsound"]), min_size=1, max_size=3), drift=st.booleans(),
       fixes=st.integers(0, 3))
def test_p29_no_cut_and_no_plan_changing_ruling_reaches_a_revise_prompt_but_normal_fixes_do(tmp_path, verdicts, drift, fixes):
    plan = f"# Plan: Demo\n\n## Decisions\n\n- {DECIDED}\n"
    project = cuts_project(fresh(tmp_path), plan=plan)
    ids = [f"C{i + 1}" for i in range(len(verdicts))]
    over = {**refuting(ids=tuple(f"R{i + 1}" for i in range(fixes))),
            "review: should-exist": {"findings": [cut_finding(i, f"CUTFIX-{i + 1}") for i in range(len(ids))]},
            "arbiter cuts": cut_rulings(dict(zip(ids, verdicts))), "arbiter cuts risky": cut_rulings(dict(zip(ids, verdicts)))}
    wording = "retries are capped at WORDING-5"
    if drift:
        over["arbiter drift"] = {"rulings": [drift_requirement(wording)]}
    out = create(project, over)
    prompts = revise_prompts(out)
    assert prompts, out["labels"]
    for p in prompts:
        assert not any(f"CUTFIX-{i + 1}" in p for i in range(len(ids))), "a proposed cut reaches a revise prompt as a fix"
        assert "WORDING-5" not in p, "a plan-changing ruling reaches a revise prompt"
        assert all(f"fix R{i + 1}" in p for i in range(fixes)), "a normal fix is missing"
    assert not any(label.startswith("apply-cuts") for label in out["labels"])


@PROP
@given(verdicts=st.lists(st.sampled_from(["sound", "unsound"]), min_size=1, max_size=3), failed=st.data(), form=arbiter_failures())
def test_p30_each_cut_gets_one_ruling_a_failed_one_is_unchanged_and_the_result_is_not_ready(tmp_path, verdicts, failed, form):
    project = cuts_project(fresh(tmp_path))
    ids = [f"C{i + 1}" for i in range(len(verdicts))]
    bad = failed.draw(st.sets(st.sampled_from(ids)))
    whole = form == "throw"
    if whole:
        bad = set(ids)
        answer = {"__throw": True}
    else:
        answer = {"rulings": [(form if cid in bad else cut_rulings({cid: v})["rulings"][0]) for cid, v in zip(ids, verdicts)]}
    over = {"review: should-exist": {"findings": [cut_finding(i, f"CUTFIX-{i + 1}") for i in range(len(ids))]},
            "arbiter cuts": answer, "arbiter cuts risky": answer}
    out = create(project, over)
    assert len(arbiter_calls(out, "cuts")) >= 1, out["labels"]
    review = review_of(out)
    arb = arbiter_of(out)
    shown = [str(c["id"]) for c in (review.get("proposedCuts") or [])]
    ok = {cid: v for cid, v in zip(ids, verdicts) if cid not in bad}
    assert len(arb["rulings"]) == len(ok), f"one ruling per cut that got one, none for a failed one: {arb}"
    assert {c for c in shown if c in ok} == {c for c, v in ok.items() if v == "sound"}, f"shown cuts differ from the soundly reasoned: {shown}"
    for cid in bad:
        cut = next((c for c in (review.get("proposedCuts") or []) if str(c["id"]) == cid), None)
        assert cut is None or not cut.get("ruling"), f"a failed cut changed: {cut}"
    if bad:
        assert review["result"] != "Ready", review["result"]
    assert not any(label.startswith("apply-cuts") for label in out["labels"])


@settings(max_examples=200, derandomize=True, database=None, deadline=None)
@given(approved=st.booleans(), started=st.booleans())
def test_p42_decide_cut_applies_only_when_approved_and_nothing_has_started(approved, started):
    from claude_kiro.arbiter_policy import decide_cut

    expected = ("frozen" if started else "apply") if approved else "hold"
    assert decide_cut(approved, started) == expected, (approved, started)


@PROP
@given(mode=st.sampled_from(["create", "review"]), confirmed=st.integers(0, 3))
def test_p46_with_no_refuted_finding_and_no_cut_no_arbiter_step_runs(tmp_path, mode, confirmed):
    project = plan_project(fresh(tmp_path))
    over = {**refuting(ids=tuple(f"R{i + 1}" for i in range(confirmed))), "review: should-exist": {"findings": []}}
    out = create(project, over, args={"mode": mode})
    assert arbiter_calls(out, "refuted") == [] and arbiter_calls(out, "cuts") == [], out["labels"]
    assert not review_calls(out, "refuted") and not review_calls(out, "cuts"), out["ckCalls"]
    arb = arbiter_of(out)
    assert "refuted" not in arb["ran"] and "cuts" not in arb["ran"], arb
    assert "refuted" not in arb["lost"] and "cuts" not in arb["lost"], arb
