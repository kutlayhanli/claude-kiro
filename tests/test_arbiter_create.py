"""The /spec:create requirements prompt and the should-this-exist lens (run-principles Story 2 and Story 4), run through the
real plugin/workflows/spec-create.js under node with the shared fake agent (tests/js/fake_create.mjs). Prompt cases read the
prompts the workflow really sends; nothing of the project's own code is mocked."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from arbiter_helpers import DEFAULT_REQUIREMENTS, DEFAULT_TASKS, arbiter_project, ck_run
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
  sent.push({ label, prompt })
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


def run_create(project, args=None, overrides=None, edits=None, apply_answer=None) -> dict:
    payload = {
        "root": str(project.root),
        "specDir": str(project.spec_dir),
        "args": args or {},
        "overrides": overrides or {},
        "edits": edits or {},
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
