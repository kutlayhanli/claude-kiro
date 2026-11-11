"""Non-functional requirements of run-principles (Task 87: TC-127 to TC-132).

Bounded arbiter cost (TC-127), runtime compatibility (TC-128), lint safety (TC-129), lint performance (TC-130, marked
`serial`), compatibility (TC-131) and determinism (TC-132).

Real boundaries only. The workflows run for real under the fake runtime (`tests/js/fake_project.mjs`, `fake_create.mjs`),
and every `arbiter-` thin step runs the real `ck` CLI against a real git repo (`arbiter_helpers.arbiter_project`), so the
ledger the limits read is a real file. The lint cases run the real `ck lint` subprocess over generated specs. Project
modules are imported inside test bodies, so the file collects before the feature exists.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from arbiter_helpers import ArbiterProject, arbiter_project  # noqa: E402
from unattended_helpers import ck_json, ck_run, spec_graph_project  # noqa: E402

JS = Path(__file__).parent / "js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

AGENTS = {
    "implementer": {"model": "haiku", "effort": "low", "escalate": ["sonnet", "opus"]},
    "test_writer": {"model": "haiku", "effort": "low"},
    "fixer": {"model": "haiku", "effort": "low", "escalate": ["sonnet", "opus"]},
    "orchestrator": {"model": "sonnet"},
    "resolver": {"model": "sonnet"},
    "arbiter": {"model": "opus", "effort": "high", "risky_model": "opus", "risky_effort": "max"},
}
AGENT_TYPES = {"arbiterAgent": "spec:arbiter", "auditorAgent": "spec:auditor", "runnerAgent": "spec:runner"}
PRINCIPLE = "project:test-intent"

# --------------------------------------------------------------------------- the workflow runner

# Runs one workflow under node with the fake runtime. `NFR_REQ` carries {kind, project, args, execCk, throwing}. The agent
# answers the `ck lint --simulate` thin step of the audit itself (the fake project has no label for it, and the label is
# the workflow's choice), records every call in order, and prints one JSON object. Create runs use a stub like
# tests/js/emit_labels.mjs's.
RUNNER = r"""
import { runWorkflow, throwingGlobals } from './load_workflow.mjs'
import { project, isCkCall, arbiterArgv, arbiterStdin } from './fake_project.mjs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
const req = JSON.parse(process.env.NFR_REQ)
const here = path.dirname(fileURLToPath(import.meta.url))
const calls = []
const envelope = json => ({ exitCode: 0, json, error: null })
const words = prompt => arbiterArgv(prompt) || []
const stdinJson = prompt => { try { return JSON.parse(arbiterStdin(prompt) || 'null') } catch { return null } }
const PRINCIPLES = [{ id: 'project:test-intent', layer: 'project', text: 'a test states what the requirement says' }]

function createAgent() {
  return async (prompt, opts) => {
    const l = opts.label
    calls.push({ label: l, thin: isCkCall(opts) })
    if (isCkCall(opts)) {
      const argv = words(prompt)
      const cmd = argv[0] === '-C' ? argv.slice(2) : argv
      if (cmd[0] === 'arbiter' && cmd[1] === 'context') return envelope({ principles: PRINCIPLES, planLayer: 'present', implementationStarted: false, missing: null, cut: 'apply' })
      if (cmd[0] === 'arbiter' && cmd[1] === 'lane') return envelope({ ok: true, lane: cmd[2] })
      if (cmd[0] === 'arbiter' && cmd[1] === 'review') {
        const input = stdinJson(prompt) || {}
        const results = (input.subjects || []).map((s, i) => ({ id: (s && s.id) || String(i + 1), action: 'recorded', reason: '', stronger: false, ruling: i + 1 }))
        return envelope({ results, lost: false })
      }
      if (/\bck .*\blint\b/.test(prompt)) return envelope({ ok: true, failures: [], warnings: [] })
      if (/\bck .*\bprogress\b/.test(prompt)) return envelope({ ok: true })
    }
    if (l.startsWith('write') || l.startsWith('link')) return { path: 'x', summary: 's', assumptions: [], concerns: [] }
    if (l === 'review: fidelity') return { findings: [
      { id: 'R1', file: 'requirements.md', location: '1', severity: 'major', problem: 'p', evidence: 'e', fix: 'f', needsUserDecision: false },
      { id: 'R2', file: 'requirements.md', location: '2', severity: 'minor', problem: 'p2', evidence: 'e', fix: 'f', needsUserDecision: false }] }
    if (l === 'refute: fidelity') return { verdicts: [{ id: 'R1', refuted: false, reason: 'real' }, { id: 'R2', refuted: true, reason: 'style only' }] }
    if (l === 'review: should-exist') return { findings: [{ id: 'C1', file: 'requirements.md', location: 'Story 3', severity: 'minor', problem: 'Story 3 serves no goal of PLAN.md', evidence: 'e', fix: 'cut Story 3', needsUserDecision: false, story: '3', title: 'Export', weakness: 'no plan decision needs it', saves: { criteria: 2, tasks: 1, testCases: 2 } }] }
    if (l.startsWith('review:')) return { findings: [] }
    if (/^arbiter refuted/.test(l)) return { rulings: [{ claim: 'R2', status: 'ruled', outcome: 'spec_edit', verdict: 'refutation stands', reasoning: 'wording only', principle: 'project:test-intent' }] }
    if (l === 'arbiter cuts') return { rulings: [{ claim: 'C1', status: 'needs_stronger_look', reasoning: 'the cut may drop a decided goal', established: 'Story 3 is in PLAN.md scope' }] }
    if (l === 'arbiter cuts risky') return { rulings: [{ claim: 'C1', status: 'ruled', outcome: 'spec_edit', verdict: 'cut is sound', reasoning: 'ok', principle: 'project:test-intent' }] }
    if (/^arbiter drift/.test(l)) return { rulings: [{ claim: 'drift', status: 'ruled', outcome: 'spec_edit', verdict: 'no drift', reasoning: 'the spec follows PLAN.md', principle: 'project:test-intent' }] }
    if (l.startsWith('apply-cuts')) return { applied: ['C1'], summary: 'removed Story 3' }
    if (l.startsWith('revise')) return { fixed: ['R1'], notFixed: [], escalated: [], result: 'Ready', schedule: 'Ready at start: Task 1' }
    if (l === 'snapshot spec before revision') return { copied: ['requirements.md'] }
    if (l.startsWith('consistency check')) return { contradictions: [] }
    if (l === 'remove pre-revision snapshot') return { removed: true }
    throw new Error('unexpected ' + l)
  }
}

let out
try {
  const globals = req.throwing ? throwingGlobals() : {}
  if (req.kind === 'create') {
    const script = path.join(here, '../../plugin/workflows/spec-create.js')
    const args = { mode: 'create', feature: 'f', projectRoot: '/p', specDir: '/p/specs/f', date: '2026-10-04', ...req.args }
    const { result } = await runWorkflow(script, { agent: createAgent(), args, ...globals })
    out = { result: result ?? null, calls }
  } else {
    const p = project({ ...req.project, ...(req.execCk ? { execCk: req.execCk } : {}) })
    const agent = async (prompt, opts) => {
      const simulate = isCkCall(opts) && /\blint\b/.test(prompt) && /--simulate/.test(prompt)
      calls.push({ label: opts.label, thin: isCkCall(opts), simulate: simulate ? ((prompt.match(/--task (\d+)/) || [])[1] ?? null) : null })
      if (simulate) {
        const n = (prompt.match(/--task (\d+)/) || [])[1] || '1'
        return envelope({ task: n, suites: [{ file: 'tests/test_greet.py', command: 'pytest' }], selected: [{ file: 'tests/test_greet.py', tests: null }] })
      }
      return p.agent(prompt, opts)
    }
    const script = path.join(here, '../../plugin/workflows/spec-implement.js')
    const args = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.ck/worktrees/demo-integration', ...req.args }
    const { result } = await runWorkflow(script, { agent, args, ...globals })
    out = { result: result ?? null, calls, events: p.state.events, auditCalls: p.state.auditCalls, rulingCalls: p.state.rulingCalls, arbiterSteps: p.state.arbiterSteps, attempts: p.state.attempts }
  }
} catch (e) {
  out = { error: String((e && e.stack) || e), calls }
}
console.log(JSON.stringify(out))
"""


def run_workflow(kind: str, *, project: Optional[dict] = None, args: Optional[dict] = None, exec_ck: Optional[dict] = None,
                 throwing: bool = False) -> Dict[str, Any]:
    req = {"kind": kind, "project": project or {}, "args": args or {}, "execCk": exec_ck, "throwing": throwing}
    proc = subprocess.run(["node", "--input-type=module", "-e", RUNNER], cwd=JS, capture_output=True, text=True, timeout=300,
                          env={**os.environ, "NFR_REQ": json.dumps(req)})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def labels(out: Dict[str, Any]) -> List[str]:
    return [c["label"] for c in out["calls"]]


def exec_ck(p: ArbiterProject) -> dict:
    return {"cmd": [sys.executable, "-m", "claude_kiro.cli"], "cwd": str(p.root), "env": {}}


def implement_args(p: ArbiterProject, **extra: Any) -> dict:
    return {**p.run_args, "agents": AGENTS, "tracks": {"1": "test"}, "maxRetries": 1, **AGENT_TYPES, **extra}


def ledger(p: ArbiterProject) -> dict:
    common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=p.root, check=True, capture_output=True, text=True).stdout.strip()
    path = (p.root / common) / "ck" / p.spec / "arbiter-ledger.json"
    return json.loads(path.read_text()) if path.exists() else {"claims": {}}


def task_calls(outs: List[Dict[str, Any]], task: str) -> int:
    """`arbiter task <n>` and `arbiter task <n> risky` agent calls across runs."""
    return sum(1 for o in outs for label in labels(o) if label in (f"arbiter task {task}", f"arbiter task {task} risky"))


# --------------------------------------------------------------------------- TC-127 (NFR Bounded arbiter cost, 5.7)

STRONGER = {"status": "needs_stronger_look", "reasoning": "the spec and the test disagree", "established": "which criterion the claim names"}
RULED_CODE = {"status": "ruled", "outcome": "code", "reasoning": "the test is right", "principle": PRINCIPLE, "code": "make the code meet 1.1"}


def test_tc127_a_repeated_claim_across_three_runs_makes_at_most_6_calls_on_a_task_and_2_on_a_claim(tmp_path):
    p = arbiter_project(tmp_path)
    outs = []
    for _ in range(3):
        out = run_workflow(
            "implement", exec_ck=exec_ck(p), args=implement_args(p),
            project={"deps": {"1": [], "2": ["1"]}, "done": ["1"], "guardStops": {"2": "always"}, "rulings": {"2": [STRONGER]}},
        )
        assert "error" not in out, out.get("error")
        outs.append(out)
    assert task_calls(outs, "2") >= 1, "no arbiter call: a guard stop is a claim and reaches the arbiter (6.2)"
    assert task_calls(outs, "2") <= 6, labels(outs[0])
    claims = ledger(p)["claims"]
    assert claims, "the ledger records the claim across runs"
    for key, claim in claims.items():
        assert claim["calls"] <= 2, (key, claim)
    # 5.7: a risky answer that still asks for a stronger look is not called again, in this run or in the resumed ones
    assert sum(c["calls"] for c in claims.values()) == task_calls(outs, "2")


def test_tc127_a_fourth_ruling_on_a_task_is_not_called_across_runs(tmp_path):
    p = arbiter_project(tmp_path)
    conflicts = [{"file": f"tests/test_t{k}.py", "test": f"test_tc{k}_x", "criterion": "1.1", "reason": f"asserts the opposite {k}", "fix": "assert what 1.1 says"} for k in range(1, 5)]
    outs = []
    for _ in range(3):
        out = run_workflow(
            "implement", exec_ck=exec_ck(p), args=implement_args(p),
            project={"deps": {"1": [], "2": ["1"]}, "done": ["1"], "testConflictsList": {"2": conflicts}, "rulings": {"2": [RULED_CODE]}},
        )
        assert "error" not in out, out.get("error")
        outs.append(out)
    calls = task_calls(outs, "2")
    assert calls >= 1, "no arbiter call: a test conflict is a claim and reaches the arbiter"
    assert calls <= 6, calls
    claims = ledger(p)["claims"]
    ruled = [k for k, c in claims.items() if c["status"] == "ruled"]
    assert len(ruled) <= 3, ("7.6: the 4th ruling on a task is never called", ruled)
    assert all(c["calls"] <= 2 for c in claims.values()), claims


# --------------------------------------------------------------------------- TC-128 (NFR Runtime compatibility)

PROBLEM = {"file": "tests/test_greet.py", "test": "test_tc1_greet", "criterion": "1.1", "reason": "asserts the opposite of 1.1", "needs": "assert what 1.1 says"}


def audit_scenario(p: ArbiterProject) -> dict:
    plan = {"verifiedBy": {"2": ["1"]}}
    return {
        "deps": {"1": [], "2": ["1"]}, "planSeq": [plan] * 30,
        "auditFindings": {"2": {"problems": [PROBLEM], "summary": "one problem"}},
        "rulings": {"2": [RULED_CODE]},
    }


def test_tc128_implement_runs_arbiter_audit_and_rulings_under_a_throwing_date_and_math_random(tmp_path):
    p = arbiter_project(tmp_path)
    out = run_workflow("implement", exec_ck=exec_ck(p), args=implement_args(p, verifiedBy={"2": ["1"]}), project=audit_scenario(p), throwing=True)
    assert "error" not in out, out.get("error")  # a clock or random read in new logic throws here
    seen = labels(out)
    assert "audit task 2" in seen, f"the audit runs before task 2's first attempt: {seen}"
    assert "arbiter task 2" in seen, f"the audit problem reaches the arbiter: {seen}"
    assert out["result"] is not None


def test_tc128_implement_guard_stop_ruling_under_a_throwing_date_and_math_random(tmp_path):
    p = arbiter_project(tmp_path)
    out = run_workflow("implement", exec_ck=exec_ck(p), args=implement_args(p), throwing=True,
                       project={"deps": {"1": [], "2": ["1"]}, "done": ["1"], "guardStops": {"2": "always"}, "rulings": {"2": [STRONGER]}})
    assert "error" not in out, out.get("error")
    seen = labels(out)
    assert "arbiter task 2" in seen and "arbiter task 2 risky" in seen, seen


def test_tc128_create_rulings_under_a_throwing_date_and_math_random():
    args = {**AGENT_TYPES, "agents": {"arbiter": AGENTS["arbiter"]}}
    out = run_workflow("create", args=args, throwing=True)
    assert "error" not in out, out.get("error")
    seen = labels(out)
    assert "arbiter refuted" in seen and "arbiter cuts" in seen and "arbiter cuts risky" in seen, seen
    assert any(label.startswith("arbiter drift") for label in seen), seen
    assert out["result"] is not None


# --------------------------------------------------------------------------- TC-129 (NFR Lint safety)

CANARY = (
    "import os\n"
    "from pathlib import Path\n\n"
    "with Path(os.environ['CANARY_MARK']).open('a') as fh:\n"
    "    fh.write('imported\\n')\n"
    "raise RuntimeError('canary module imported')\n"
)


def test_tc129_ck_lint_imports_no_project_code_and_makes_no_connection(tmp_path, no_network):
    from test_verify_scope_lint import RUN, SHARED, blk

    tasks = (
        blk(1, "Alpha tests", track="test", files=[SHARED], verify=RUN, verifies="Task 3")
        + blk(2, "Beta tests", track="test", files=[SHARED], verify=f"{RUN} -k test_tc2_", verifies="Task 4")
        + blk(3, "Alpha core", files=["src/mtr/a.py"], verify=RUN, vby="Task 1", deps="Task 1")
        + blk(4, "Beta core", files=["src/mtr/b.py"], verify="", vby="Task 2", deps="Task 2")
    )
    root = spec_graph_project(
        tmp_path, tasks=tasks, tests={SHARED: CANARY, "tests/conftest.py": CANARY, "tests/helpers.py": CANARY},
        sources={"src/mtr/a.py": CANARY, "src/mtr/b.py": CANARY, "src/mtr/sitecustomize.py": CANARY},
    )
    mark = tmp_path / "canary.mark"
    # Everything the canaries live in is importable: a lint that imported project code would find and run them.
    path = os.pathsep.join([os.environ.get("PYTHONPATH", ""), str(root), str(root / "src"), str(root / "tests")])
    env = {"CANARY_MARK": str(mark), "PYTHONPATH": path}
    for args in (["lint", "sim", "--json"], ["lint", "sim", "--simulate", "--task", "4", "--json"]):
        proc = ck_run(root, *args, env=env)
        assert proc.returncode in (0, 1), f"{args}: {proc.stdout}{proc.stderr}"  # 2 is a usage error: the check is not built
        assert json.loads(proc.stdout) is not None
    assert not mark.exists(), "ck lint imported a project module: " + mark.read_text()
    assert not no_network.exists() or no_network.read_text() == "", "ck lint opened a connection: " + no_network.read_text()
    code, obj, err = ck_json(root, "lint", "sim", "--json", env=env)
    assert "verifyScope" in obj, f"the Verify-scope check ran: {sorted(obj)}"  # the shared-suite scan read the canary files as text


# --------------------------------------------------------------------------- generated specs for TC-130 and TC-132


def generated_tasks(groups: int) -> str:
    """`groups` x 3 tasks: a test task, an impl task it verifies, and an impl task that runs a shared suite.

    Suites repeat across groups (9 shared files), so the Verify-scope check has real work: the chain of dependencies
    runs through every group.
    """
    from test_lint import block

    out = ""
    for g in range(groups):
        t = 3 * g + 1
        suite = f"tests/test_shared_{g % 9}.py"
        previous = f"Task {t - 1}" if g else "None"
        out += block(t, f"Group {g} tests", track="test", files=[suite], verify=f"uv run pytest -q {suite} -k test_tc{t}_", verifies=f"Task {t + 1}, Task {t + 2}")
        out += block(t + 1, f"Group {g} core", files=[f"src/mtr/g{g}.py"], verify=f"uv run pytest -q {suite}", vby=f"Task {t}", deps=f"Task {t}" if not g else f"Task {t}, {previous}")
        out += block(t + 2, f"Group {g} edge", files=[f"src/mtr/e{g}.py"], verify=f"uv run pytest -q {suite}", vby=f"Task {t}", deps=f"Task {t + 1}")
    return out


SHARED_TESTS = "".join(f"def test_tc{t}_x():\n    assert True\n\n\n" for t in range(1, 120, 3))


def generated_project(tmp_path: Path, groups: int) -> Path:
    return spec_graph_project(tmp_path, tasks=generated_tasks(groups), tests={f"tests/test_shared_{k}.py": SHARED_TESTS for k in range(9)})


# --------------------------------------------------------------------------- TC-130 (NFR Lint performance)


@pytest.mark.serial
def test_tc130_ck_lint_on_a_108_task_spec_finishes_under_10_seconds(tmp_path):
    root = generated_project(tmp_path, 36)
    tasks_md = (root / "specs" / "sim" / "tasks.md").read_text()
    assert tasks_md.count("### Task ") == 108
    started = time.perf_counter()
    proc = ck_run(root, "lint", "sim", "--json")
    elapsed = time.perf_counter() - started
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
    obj = json.loads(proc.stdout)
    assert "verifyScope" in obj, f"the Verify-scope check ran: {sorted(obj)}"
    assert elapsed < 10, f"ck lint took {elapsed:.1f}s on 108 tasks"
    started = time.perf_counter()
    proc = ck_run(root, "lint", "sim", "--simulate", "--task", "107", "--json")
    elapsed = time.perf_counter() - started
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
    assert elapsed < 10, f"ck lint --simulate took {elapsed:.1f}s on 108 tasks"


# --------------------------------------------------------------------------- TC-132 (NFR Determinism)


def test_tc132_two_ck_lint_json_runs_on_a_tree_with_findings_are_byte_identical(tmp_path):
    root = generated_project(tmp_path, 12)
    first = ck_run(root, "lint", "sim", "--json", env={"PYTHONHASHSEED": "1"})
    second = ck_run(root, "lint", "sim", "--json", env={"PYTHONHASHSEED": "2"})
    assert first.returncode in (0, 1), first.stdout + first.stderr
    obj = json.loads(first.stdout)
    assert obj.get("verifyScope"), f"the tree has Verify-scope findings to order: {sorted(obj)}"
    assert first.stdout == second.stdout
    assert first.returncode == second.returncode


# --------------------------------------------------------------------------- TC-131 (NFR Compatibility)

# Today's labels, in today's order, for the scenario below (read from the workflow before the arbiter and the audit).
TODAY = [
    "plan 1", "run start", "task 1 [test]", "plan 2", "review-wave 1 tasks 1", "land 1", "plan 3", "plan 4", "task 2",
    "task 2 retry 1", "plan 5", "review-wave 2 tasks 2", "land 2", "plan 6", "plan 7", "task 3", "plan 8",
    "review-wave 3 tasks 3", "land 3", "plan 9", "plan 10", "final gate", "report", "audit",
]


def clean_scenario(p: ArbiterProject) -> dict:
    plan = {"verifiedBy": {"2": ["1"]}}
    return {"deps": {"1": [], "2": ["1"], "3": ["2"]}, "failUntil": {"2": 1}, "planSeq": [plan] * 30}


def test_tc131_a_clean_spec_runs_todays_labels_with_only_the_audit_steps_added(tmp_path):
    p = arbiter_project(tmp_path)
    out = run_workflow("implement", exec_ck=exec_ck(p), args=implement_args(p, verifiedBy={"2": ["1"]}), project=clean_scenario(p))
    assert "error" not in out, out.get("error")
    seen = labels(out)
    audits = [i for i, c in enumerate(out["calls"]) if c["label"] == "audit task 2"]
    simulates = [i for i, c in enumerate(out["calls"]) if c.get("simulate")]
    assert len(audits) == 1, f"one audit for the verified impl task, none for its retry, none elsewhere: {seen}"
    assert simulates and out["calls"][simulates[0]]["simulate"] == "2", f"the thin `ck lint --simulate --task 2` step: {seen}"
    first_attempt = seen.index("task 2")
    assert max(audits[0], simulates[0]) < first_attempt and simulates[0] < audits[0], f"both come before the first attempt: {seen}"
    assert all(c["simulate"] in (None, "2") for c in out["calls"]), "no audit for a test-track task or one with no verifying tests"
    # removing the two audit steps leaves today's labels in today's order, and nothing else is new
    rest = [c["label"] for i, c in enumerate(out["calls"]) if i not in (audits[0], simulates[0])]
    assert rest == TODAY, rest
    assert not [label for label in seen if label.startswith("arbiter")], seen
    assert not list(p.root.rglob("decisions.md")), "no ruling, so no decisions.md"
    assert not list(p.target.rglob("decisions.md"))
