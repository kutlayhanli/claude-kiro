"""End-to-end smoke tests of the unattended-correctness workflow (TC-176 to TC-179, TC-43 part b).

The real `plugin/workflows/spec-implement.js` runs under Node with the OpenCode path's direct `ck()` global
(`realCk`: every ck call is the real CLI) on real git repos (`attr_project`, `spec_graph_project`) with real pytest and
real hooks. Only the agents are scripted (`tests/js/real_agents.mjs` plus the fallback below): no LLM, no mocks of the
project's own code.

Each scenario runs once (a module-scoped result) and several tests assert on it:
- TC-176 / TC-178: Task 2's first commit breaks `tc1` (found only by the attributed gate at merge 2, N=2; the scripted
  agents land in the order 2, 3, 7, 8, one per landing step); a scripted agent also attempts a raw
  `git merge --no-verify` of a protected branch during the run.
- TC-177 / TC-43b: a test asserting an unreachable value, a conflict and its confirmation, a scripted test-fixer, the
  real `ck testfix check` / `prove`, a scripted approve, the real land of the fix.
- TC-179: the command edge from lint and relink, and the live edge at runtime without relink.

Before the implementation exists these fail for missing behaviour: no attributed gate, no test-fix loop, no hooks, no
live edge.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import unattended_helpers as h  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "tests" / "js"
WORKFLOW = ROOT / "plugin" / "workflows" / "spec-implement.js"

pytestmark = [pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")]

PROPERTIES: Dict[str, str] = {}

# --------------------------------------------------------------------------- the Node driver

DRIVER = r"""
import fs from 'node:fs'
import { execFile } from 'node:child_process'
import path from 'node:path'
import { runWorkflow } from '__JS__/load_workflow.mjs'
import { realAgents, promptCommands } from '__JS__/real_agents.mjs'

const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
const sc = cfg.scenario || {}
const ckSpec = { cmd: cfg.ckCmd, cwd: cfg.root, env: cfg.env }
const ckCalls = []
const agentLog = []
const runs = []
const raw = []
const tr = { testfixDone: false, attempts: {}, order: [] }

const exec = (bin, args, opts = {}) => new Promise(resolve => {
  execFile(bin, args, { cwd: opts.cwd || cfg.root, env: { ...process.env, ...cfg.env }, encoding: 'utf8', maxBuffer: 1 << 26 }, (err, out, errOut) => {
    resolve({ code: err ? (typeof err.code === 'number' ? err.code : -1) : 0, stdout: out || '', stderr: errOut || '' })
  })
})
const parse = t => { try { const v = JSON.parse(t); return v && typeof v === 'object' ? v : null } catch { return null } }
const ckRun = async argv => {
  const [bin, ...pre] = cfg.ckCmd
  const res = await exec(bin, [...pre, ...argv])
  return { ...res, json: parse(res.stdout) }
}
// sc.landOrder: a task agent returns only after the landing step of every task before it in the order has run, so
// the landing queue takes the tasks in that order, one per landing step (the next task is not queued before the
// previous one's `ck worktree land` has returned). The agents still start together.
const landed = {}
const landSlot = n => landed[n] || (landed[n] = (() => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } })())
const landedBefore = async n => {
  const i = (sc.landOrder || []).indexOf(n)
  if (i >= 0) await Promise.all(sc.landOrder.slice(0, i).map(m => landSlot(m).promise))
}
const ck = async argv => {
  const r = await ckRun(argv)
  ckCalls.push({ argv, code: r.code, json: r.json })
  if (argv.includes('land')) for (const n of argv) if ((sc.landOrder || []).includes(n)) landSlot(n).resolve()
  return r
}
const git = (dir, ...a) => exec('git', ['-C', dir, ...a])
const commitFiles = async (dir, files, message) => {
  for (const [rel, content] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(dir, rel)), { recursive: true })
    fs.writeFileSync(path.join(dir, rel), content)
  }
  await git(dir, 'add', '-A')
  const c = await git(dir, '-c', 'user.name=scripted', '-c', 'user.email=scripted@example.com', 'commit', '-q', '-m', message)
  const head = await git(dir, 'rev-parse', '--short', 'HEAD')
  return { code: c.code, head: head.stdout.trim() }
}
const worktreeFor = async (prompt, name) => {
  const create = promptCommands(prompt, ['worktree create']).find(a => a.includes('worktree') && a.includes('create'))
  if (create) {
    const res = await ck(create.includes('--json') ? create : [...create, '--json'])
    const entry = (Array.isArray(res.json) ? res.json : []).find(e => String(e.task) === String(name))
    if (entry && entry.path) return path.resolve(cfg.root, entry.path)
  }
  return path.join(cfg.root, '.ck', 'worktrees', `${cfg.spec}-task-${name}`)
}
const tail = s => String(s || '').slice(-1500)
// Task `n` marked Done in a tasks.md `text` (the agent's own edit of the worktree's current file, as `mark_done`).
const markDone = (text, n) => {
  const start = text.indexOf(`### Task ${n}:`)
  if (start < 0) return text
  const nxt = text.indexOf('\n### Task ', start + 1)
  const end = nxt < 0 ? text.length : nxt
  const block = text.slice(start, end).replace('**Status:** Not Started', '**Status:** Done').replaceAll('- [ ]', '- [x]')
  return text.slice(0, start) + block + text.slice(end)
}
// A scenario file is text, or {markDone: n}: the worktree's current tasks.md with Task n marked Done.
const scenarioFiles = n => Object.fromEntries(Object.entries((sc.files && sc.files[n]) || {}).map(([rel, v]) =>
  [rel, v && typeof v === 'object' && v.markDone ? cur => markDone(cur, String(v.markDone)) : v]))

const agent = async (prompt, opts) => {
  const label = (opts && opts.label) || ''
  agentLog.push({ label, prompt, callIndex: ckCalls.length })
  // The same answers in every scenario, so no junk answer decides a run: the auditor finds no problem, and the arbiter
  // rules what the scenario scripts for the task (sc.arbiter[n]), else a `code` ruling citing the same principle.
  if (/^audit task /.test(label)) return { problems: [], summary: 'scripted: the selected tests are sound' }
  const ar = label.match(/^arbiter task (\d+)/)
  if (ar) {
    const key = (prompt.match(/\b\d+\|[^\s"'`,)]+/) || [''])[0]
    const base = { claim: key, status: 'ruled', principle: 'project:test-intent' }
    const scripted = sc.arbiter && sc.arbiter[ar[1]]
    return scripted ? { ...base, ...scripted } : { ...base, outcome: 'code', reasoning: 'scripted: the code is what changes', code: 'make the code meet the criterion' }
  }
  const t = label.match(/^task (\d+)/)
  if (t && !/ revise /.test(label)) {
    const n = t[1]
    tr.attempts[n] = (tr.attempts[n] || 0) + 1
    tr.order.push(`${n}#${tr.attempts[n]}`)
    if (sc.raw && sc.raw.task === n && tr.attempts[n] === 1) {
      const before = await git(cfg.target, 'rev-parse', 'HEAD')
      const r = await git(cfg.target, 'merge', '--no-verify', '--no-edit', sc.raw.branch)
      const after = await git(cfg.target, 'rev-parse', 'HEAD')
      // The refusal leaves the merge's index and files behind (a merge in progress, or staged files after a
      // fast-forward). The scripted agent cleans up after itself, as TC-76 and every hook test do, so the
      // integration checkout is as it found it; the ref never moved, so this resets nothing but that residue.
      // With sc.raw.noCleanup (TC-169) it leaves the residue, for the next landing to recover.
      const cleanup = sc.raw.noCleanup ? null : await git(cfg.target, 'reset', '--hard', 'HEAD')
      raw.push({ task: n, code: r.code, stderr: tail(r.stderr), before: before.stdout.trim(), after: after.stdout.trim(), cleanup: cleanup ? cleanup.code : null, callIndex: ckCalls.length })
    }
    const conflict = sc.conflicts && sc.conflicts[n]
    const blocked = conflict && !tr.testfixDone
    const files = blocked ? {} : scenarioFiles(n)
    const a = realAgents({ ck: ckSpec, scenario: { files: { [n]: files } } })
    const res = await a.agent(prompt, opts)
    runs.push(...a.runs.map(r => ({ ...r, attempt: tr.attempts[n] })))
    if (!res.gatePassed) await ckRun(['-C', path.join(cfg.root, '.ck', 'worktrees', `${cfg.spec}-task-${n}`), 'worktree', 'release', cfg.spec, n])
    await landedBefore(n)
    if (blocked) return { ...res, status: 'blocked', gatePassed: false, testConflict: conflict }
    return res
  }
  if (/^fix \d+/.test(label)) {
    const k = label.match(/^fix (\d+)/)[1]
    const dir = await worktreeFor(prompt, `fix${k}`)
    const c = await commitFiles(dir, sc.fix || {}, 'fix: scripted repair')
    const merge = promptCommands(prompt, ['worktree merge']).find(a => a.includes('merge'))
    const m = merge ? await ck(merge) : { code: 1 }
    return { green: c.code === 0 && m.code === 0, failing: m.code === 0 ? '' : 'fix merge failed' }
  }
  if (/^testfix review/.test(label) || /^review/.test(label)) {
    return { verdict: 'approve', findings: [], summary: 'scripted approve' }
  }
  const tf = label.match(/^testfix (\d+)/)
  if (tf) {
    const dir = await worktreeFor(prompt, `testfix${tf[1]}`)
    const c = await commitFiles(dir, sc.testfix || {}, 'test fix: scripted correction')
    tr.testfixDone = true
    return { verdict: 'fixed', changedTests: sc.testfixChanged || [], commits: c.head, summary: 'scripted test fix' }
  }
  // thin ck steps (plan, land, gate, re-check, relink, audit...): run the prompt's own command through the real ck
  const cmds = promptCommands(prompt, ['plan', 'worktree land', 'worktree merge', 'worktree audit', 'gate', 'relink', 'testfix', 'arbiter'])
  let last = null
  for (const argv of cmds) last = await ck(argv)
  if (/^(re-check|full gate|final gate)/.test(label)) {
    return { green: !!last && last.code === 0, failing: last && last.code !== 0 ? tail(last.stdout || last.stderr) : '' }
  }
  if (last && last.json) return { ...last.json, exitCode: last.code, json: last.json }
  return { exitCode: last ? last.code : 1, json: null, error: last ? tail(last.stderr) : 'no ck command in the prompt: ' + label }
}

const out = { ok: true }
try {
  const planned = await ckRun(['-C', cfg.target, 'plan', cfg.spec, '--json'])
  const args = { spec: cfg.spec, root: cfg.root, into: cfg.into, targetDir: cfg.target, deps: (planned.json && planned.json.deps) || {}, tracks: cfg.tracks, titles: cfg.titles, fullGateEvery: cfg.fullGateEvery, mergeBatch: cfg.mergeBatch, ...(cfg.args || {}) }
  const { result, logs } = await runWorkflow(cfg.workflow, { agent, args, ck })
  out.result = result
  out.logs = logs
} catch (e) { out.ok = false; out.error = String((e && e.stack) || e) }
out.ckCalls = ckCalls
out.agentLog = agentLog
out.runs = runs
out.raw = raw
out.order = tr.order
process.stdout.write('\n@@RESULT@@' + JSON.stringify(out) + '\n')
"""


class Run:
    """The outcome of one workflow run: the result plus every ck call, agent prompt and raw git attempt."""

    def __init__(self, out: Dict[str, Any]) -> None:
        self.out = out
        self.result: Dict[str, Any] = out.get("result") or {}
        self.ck_calls: List[Dict[str, Any]] = out.get("ckCalls", [])
        self.agents: List[Dict[str, str]] = out.get("agentLog", [])
        self.raw: List[Dict[str, Any]] = out.get("raw", [])

    def ck(self, *words: str) -> List[Dict[str, Any]]:
        """ck calls whose argv (after any `-C dir`) starts with `words`."""
        found = []
        for call in self.ck_calls:
            argv = call["argv"]
            rest = argv[2:] if argv[:1] == ["-C"] else argv
            if rest[: len(words)] == list(words):
                found.append(call)
        return found

    def prompts(self, label_re: str) -> List[str]:
        return [a["prompt"] for a in self.agents if re.search(label_re, a["label"])]

    def agent_calls(self, label_re: str) -> List[Dict[str, Any]]:
        return [a for a in self.agents if re.search(label_re, a["label"])]

    def gates(self) -> List[Dict[str, Any]]:
        return list(self.result.get("gates") or [])


def run_workflow(tmp_path: Path, root: Path, spec: str, scenario: Dict[str, Any], *, full_gate_every: int = 2,
                 merge_batch: int = 4, args: Optional[Dict[str, Any]] = None, extra_env: Optional[Dict[str, str]] = None) -> Run:
    target = h.integration_worktree(root, spec)
    text = (target / "specs" / spec / "tasks.md").read_text()
    tracks = {m.group(1): m.group(2).strip() for m in re.finditer(r"### Task (\d+):.*?\*\*Track:\*\* (\w+)", text, re.S)}
    titles = {m.group(1): m.group(2).strip() for m in re.finditer(r"### Task (\d+): (.*)", text)}
    env = h._ck_env(root, extra_env)
    cfg = {
        "root": str(root), "target": str(target), "spec": spec, "into": f"integrate/{spec}", "workflow": str(WORKFLOW),
        "ckCmd": h.CK_ARGV, "env": env, "scenario": scenario, "tracks": tracks, "titles": titles,
        "fullGateEvery": full_gate_every, "mergeBatch": merge_batch, "args": args or {},
    }
    (tmp_path / "cfg.json").write_text(json.dumps(cfg))
    driver = tmp_path / "driver.mjs"
    driver.write_text(DRIVER.replace("__JS__", JS.as_uri()))
    proc = subprocess.run(["node", str(driver), str(tmp_path / "cfg.json")], cwd=tmp_path, capture_output=True, text=True, timeout=1500)
    assert "@@RESULT@@" in proc.stdout, f"driver printed no result (exit {proc.returncode}): {proc.stderr[-2000:]}"
    out = json.loads(proc.stdout.split("@@RESULT@@", 1)[1])
    assert out.get("ok"), f"the workflow run threw: {out.get('error')}"
    return Run(out)


# --------------------------------------------------------------------------- repo builders

CALC_BAD = (
    "from calc import subtract\n\n\n"
    "def test_tc2_sub():\n    assert subtract(5, 3) == 99  # unreachable: 5 - 3 is 2\n\n\n"
    "def test_tc3_sub_again():\n    assert subtract(3, 1) == 2\n"
)
CALC_GOOD = CALC_BAD.replace("== 99  # unreachable: 5 - 3 is 2", "== 2")
TC1_TEST = "from calc import add\n\n\ndef test_tc1_add():\n    assert add(1, 2) == 3\n"
GOOD_CALC = "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n"
BROKEN_CALC = "def add(a, b):\n    return a - b\n\n\ndef subtract(a, b):\n    return a - b\n"


def mark_done(text: str, n: str) -> str:
    """tasks.md `text` with Task `n` Done and its acceptance boxes ticked (the agent's own edit)."""
    start = text.index(f"### Task {n}:")
    nxt = text.find("\n### Task ", start + 1)
    end = len(text) if nxt < 0 else nxt
    block = text[start:end].replace("**Status:** Not Started", "**Status:** Done").replace("- [ ]", "- [x]")
    return text[:start] + block + text[end:]


def add_to_main_and_integration(root: Path, files: Dict[str, str], message: str) -> None:
    """Commit `files` on main, then merge main into integrate/demo (attr_project branched it already)."""
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content)
    h.commit_all(root, message)
    h.git(h.integration_worktree(root), "merge", "-q", "--no-edit", "main")


PRINCIPLES = (
    "# Project Principles\n\nDefaults every spec run upholds.\n\n"
    "- **priority-order:** requirements > design > test plan > task wiring.\n"
    "- **test-intent:** a test keeps its intent; only selection and setup may move.\n"
    "- **fail-closed:** fail closed on security.\n"
    "- **remove-first:** remove before you optimize.\n"
)


def demo_project(tmp_path: Path, calc_tests: str) -> Path:
    """attr_project: tests 1 and 6 Done, impl tasks 2, 3, 7 and 8 left. TC-1 lives in its own file so Task 2's own
    Verify (tests/test_calc_tc.py) does not run it."""
    root = h.attr_project(tmp_path, statuses={"1": "Done", "6": "Done"}, tests={"test_tc5_more": "pass"})
    add_to_main_and_integration(
        root, {"tests/test_calc_tc.py": calc_tests, "tests/test_other.py": TC1_TEST, "specs/PRINCIPLES.md": PRINCIPLES},
        "real calc tests")
    return root


def calc_scenario() -> Dict[str, Any]:
    """Each task agent marks its own task Done in the worktree's current tasks.md (`{"markDone": n}`, applied by the
    driver), never a copy made before the run: a task that starts after another merged (Task 3 is held behind Task 2,
    both own calc.py, 13.1) must not revert that task's Done on landing."""
    return {"files": {
        "2": {"calc.py": BROKEN_CALC, "specs/demo/tasks.md": {"markDone": "2"}},
        "3": {"sub_ok.py": "SUB = True\n", "specs/demo/tasks.md": {"markDone": "3"}},
        "7": {"seven.py": "SEVEN = 7\n", "specs/demo/tasks.md": {"markDone": "7"}},
        "8": {"eight.py": "EIGHT = 8\n", "specs/demo/tasks.md": {"markDone": "8"}},
    }}


def protected_branch(root: Path) -> str:
    """A branch off integrate/demo that changes a protected file (.claude/CLAUDE.md); returns its name."""
    branch = "feat/demo-protected"
    wt = root.parent / "protected-wt"
    h.git(root, "worktree", "add", "-q", "-b", branch, str(wt), "integrate/demo")
    (wt / ".claude").mkdir(exist_ok=True)
    (wt / ".claude" / "CLAUDE.md").write_text("# changed behind ck's back\n")
    h.commit_all(wt, "protected change")
    h.git(root, "worktree", "remove", "--force", str(wt))
    return branch


# --------------------------------------------------------------------------- TC-176 and TC-178


@pytest.fixture(scope="module")
def tc176_run(tmp_path_factory) -> Dict[str, Any]:
    tmp = tmp_path_factory.mktemp("tc176")
    root = demo_project(tmp, CALC_GOOD)
    branch = protected_branch(root)
    target = h.integration_worktree(root)
    head_before = h.ref(root, "integrate/demo")
    scenario = calc_scenario()
    scenario["fix"] = {"calc.py": GOOD_CALC}
    scenario["raw"] = {"task": "7", "branch": branch}
    # The agents start together (every branch off the same integrate/demo) but land in this order, one per landing
    # step: Task 2 lands first through its task-scoped landing gate (2.10), which cannot see tc1, Task 3 is the
    # second merge, and the attributed gate at merge 2 (N=2) is the first gate that runs the whole suite. Left to
    # the finishing race, Task 2 lands in the batch that completes the spec, whose landing gate runs the full suite,
    # goes red, and is bisected onto another task, so the scenario never happens.
    scenario["landOrder"] = ["2", "3", "7", "8"]
    run = run_workflow(tmp, root, "demo", scenario, full_gate_every=2)
    return {"run": run, "root": root, "target": target, "branch": branch, "head_before": head_before}


def test_tc176_periodic_gate_at_merge_two_is_red_with_tc1_on_task_2(tc176_run):
    run: Run = tc176_run["run"]
    periodic = [g for g in run.gates() if g.get("kind") == "periodic"]
    assert periodic, f"no periodic gate in result.gates: {run.gates()}"
    first = periodic[0]
    assert first.get("afterMerges") == 2 and first.get("state") == "red", first
    entries = [r for r in first.get("regressions", []) if "tc1" in r.get("node", "")]
    assert entries, f"tc1 is not among the regressions: {first.get('regressions')}"
    assert "2" in [str(t) for t in entries[0].get("tasks", [])], entries[0]
    # the gate that found it is the real attributed gate
    gates = run.ck("gate")
    assert any("--attribute" in c["argv"] for c in gates), [c["argv"] for c in gates]


def test_tc176_fixer_gets_the_attributed_entry_and_the_recheck_is_green(tc176_run):
    run: Run = tc176_run["run"]
    fixers = run.prompts(r"^fix \d+")
    assert fixers, "no fixer agent ran after the red periodic gate"
    assert "test_tc1_add" in fixers[0], "the fixer prompt does not carry the tc1 failure entry"
    rechecks = [g for g in run.gates() if g.get("kind") == "recheck"]
    assert rechecks and rechecks[0].get("state") == "green", run.gates()


def test_tc176_final_gate_is_green_with_no_excused_entries(tc176_run):
    run: Run = tc176_run["run"]
    final = [g for g in run.gates() if g.get("kind") == "final"]
    assert final and final[-1].get("state") == "green", run.gates()
    assert not final[-1].get("excusedByTask"), final[-1].get("excusedByTask")
    code, obj, err = h.ck_json(tc176_run["target"], "gate", "demo", "--attribute", "--json")
    assert code == 0, f"the real final gate is not green after the run: {err}"
    text = (tc176_run["target"] / "specs" / "demo" / "tasks.md").read_text()
    for n in ("2", "3", "7", "8"):
        block = re.search(rf"### Task {n}:.*?(?=\n### Task |\Z)", text, re.S).group(0)
        assert "**Status:** Done" in block, f"Task {n} is not Done on integration"


def test_tc178_raw_no_verify_merge_of_a_protected_branch_is_refused(tc176_run):
    run: Run = tc176_run["run"]
    assert run.raw, "the scripted raw merge attempt never ran"
    attempt = run.raw[0]
    assert attempt["code"] != 0, f"the raw git merge --no-verify was accepted: {attempt}"
    # the integration branch did not take the protected change
    merged = h.raw(tc176_run["root"], "merge-base", "--is-ancestor", tc176_run["branch"], "integrate/demo")
    assert merged.returncode != 0, "the protected branch reached integrate/demo"
    protected = h.git(tc176_run["target"], "ls-tree", "-r", "--name-only", "HEAD")
    assert ".claude/CLAUDE.md" not in protected.split("\n"), "a protected file reached integration"


def test_tc178_the_run_continues_and_the_run_end_audit_is_clean(tc176_run):
    run: Run = tc176_run["run"]
    assert run.result.get("merged") or run.result.get("done") or run.gates(), run.result
    assert "7" in json.dumps(run.result), "Task 7 does not appear in the result: the run stopped at the refusal"
    code, obj, err = h.ck_json(tc176_run["root"], "worktree", "audit", "demo", "--json")
    assert obj is not None, f"ck worktree audit --json printed no JSON (exit {code}): {err}"
    assert obj.get("complete") is True and obj.get("clean") is True, obj
    audit = run.result.get("audit")
    assert audit and audit.get("clean") is True, f"the run's own audit is not clean: {audit}"


# --------------------------------------------------------------------------- TC-169 and TC-174


def find_key(obj: Any, key: str) -> List[Any]:
    """Every value of `key` anywhere in a parsed JSON `obj`."""
    found: List[Any] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                found.append(v)
            found.extend(find_key(v, key))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(find_key(v, key))
    return found


@pytest.fixture(scope="module")
def tc169_run(tmp_path_factory) -> Dict[str, Any]:
    """The TC-176 run with TC-178's raw merge (commit a64c0aa), whose scripted agent does not reset after the refusal."""
    tmp = tmp_path_factory.mktemp("tc169")
    root = demo_project(tmp, CALC_GOOD)
    branch = protected_branch(root)
    target = h.integration_worktree(root)
    scenario = calc_scenario()
    scenario["fix"] = {"calc.py": GOOD_CALC}
    scenario["raw"] = {"task": "7", "branch": branch, "noCleanup": True}
    scenario["landOrder"] = ["2", "3", "7", "8"]
    run = run_workflow(tmp, root, "demo", scenario, full_gate_every=2)
    return {"run": run, "root": root, "target": target, "branch": branch}


def test_tc169_the_run_goes_on_and_a_later_landing_recovers_the_refused_merge(tc169_run):
    run: Run = tc169_run["run"]
    assert run.raw, "the scripted raw merge attempt never ran"
    attempt = run.raw[0]
    assert attempt["code"] != 0 and attempt["cleanup"] is None, attempt
    assert attempt["before"] == attempt["after"], f"the refused merge moved the ref: {attempt}"
    assert "7" in json.dumps(run.result), "Task 7 does not appear in the result: the run stopped at the refusal"
    later = [c for c in run.ck_calls[attempt["callIndex"]:]
             if c["argv"][2:4] in (["worktree", "land"], ["worktree", "merge"]) or c["argv"][:2] in (["worktree", "land"], ["worktree", "merge"])]
    recovered = [r for c in later for r in find_key(c.get("json"), "recovered") if r]
    assert recovered, f"no landing entry after the refused merge carries `recovered`: {[c['argv'] for c in later]}"
    assert ".claude/CLAUDE.md" in json.dumps(recovered), recovered
    protected = h.git(tc169_run["target"], "ls-tree", "-r", "--name-only", "HEAD")
    assert ".claude/CLAUDE.md" not in protected.split("\n"), "a protected file reached integration"


def test_tc169_the_run_end_audit_is_clean(tc169_run):
    run: Run = tc169_run["run"]
    audit = run.result.get("audit")
    assert audit and audit.get("clean") is True, f"the run's own audit is not clean: {audit}"
    code, obj, err = h.ck_json(tc169_run["root"], "worktree", "audit", "demo", "--json")
    assert obj is not None, f"ck worktree audit --json printed no JSON (exit {code}): {err}"
    assert obj.get("complete") is True and obj.get("clean") is True, obj


@pytest.fixture(scope="module")
def tc174_run(tmp_path_factory) -> Dict[str, Any]:
    """The TC-176 scenario without `landOrder`: the tasks land as they finish."""
    tmp = tmp_path_factory.mktemp("tc174")
    root = demo_project(tmp, CALC_GOOD)
    branch = protected_branch(root)
    target = h.integration_worktree(root)
    scenario = calc_scenario()
    scenario["fix"] = {"calc.py": GOOD_CALC}
    scenario["raw"] = {"task": "7", "branch": branch}
    run = run_workflow(tmp, root, "demo", scenario, full_gate_every=2)
    return {"run": run, "root": root, "target": target}


def test_tc174_task_2_lands_in_the_batch_that_completes_the_spec_as_unreproduced_with_no_red_task(tc174_run):
    run: Run = tc174_run["run"]
    lands = [c for c in run.ck("worktree", "land") if "2" in c["argv"]]
    assert lands, f"Task 2 was never landed: {[c['argv'] for c in run.ck('worktree', 'land')]}"
    flagged = [c for c in lands if any(v for v in find_key(c.get("json"), "unreproduced"))]
    assert flagged, f"no landing of Task 2 reports `unreproduced`: {[c.get('json') for c in lands]}"
    states = [str(s) for c in run.ck("worktree", "land") for s in find_key(c.get("json"), "state")]
    assert "RED" not in states, f"a task was blamed RED: {states}"


def test_tc174_the_attributed_gate_names_test_tc1_add_on_task_2_and_the_fixer_prompt_carries_it(tc174_run):
    run: Run = tc174_run["run"]
    unrep = [g for g in run.gates() if g.get("kind") == "unreproduced"]
    assert unrep and unrep[0].get("state") == "red", f"no red `unreproduced` gate: {run.gates()}"
    entries = [r for r in unrep[0].get("regressions", []) if "tc1" in r.get("node", "")]
    assert entries, f"tc1 is not among the regressions: {unrep[0].get('regressions')}"
    assert "2" in [str(t) for t in entries[0].get("tasks", [])], entries[0]
    assert any("--attribute" in c["argv"] for c in run.ck("gate")), [c["argv"] for c in run.ck("gate")]
    fixers = run.prompts(r"^fix \d+")
    assert fixers, "no fixer agent ran after the red attributed gate"
    assert "test_tc1_add" in fixers[0], "the fixer prompt does not carry the tc1 failure entry"
    assert not run.prompts(r"red on landing"), "a fixer ran for a task blamed RED on landing"


def test_tc174_the_final_gate_is_green(tc174_run):
    run: Run = tc174_run["run"]
    final = [g for g in run.gates() if g.get("kind") == "final"]
    assert final and final[-1].get("state") == "green", run.gates()
    code, _, err = h.ck_json(tc174_run["target"], "gate", "demo", "--attribute", "--json")
    assert code == 0, f"the real final gate is not green after the run: {err}"


# --------------------------------------------------------------------------- TC-177 and TC-43b

CONFLICT = {"file": "tests/test_calc_tc.py", "test": "test_tc2_sub", "criterion": "2.1",
            "reason": "asserts subtract(5, 3) == 99, which no correct code returns", "fix": "assert == 2"}


@pytest.fixture(scope="module")
def tc177_run(tmp_path_factory) -> Dict[str, Any]:
    tmp = tmp_path_factory.mktemp("tc177")
    root = demo_project(tmp, CALC_BAD)
    target = h.integration_worktree(root)
    scenario = calc_scenario()
    scenario["files"]["2"] = {"calc.py": GOOD_CALC, "specs/demo/tasks.md": {"markDone": "2"}}
    scenario["conflicts"] = {"2": CONFLICT}
    scenario["testfix"] = {"tests/test_calc_tc.py": CALC_GOOD}
    scenario["testfixChanged"] = [{"file": "tests/test_calc_tc.py", "test": "test_tc2_sub"}]
    scenario["arbiter"] = {"2": {
        "outcome": "test_repair", "principle": "project:test-intent",
        "reasoning": "the test asserts a value no correct code returns; its intent (subtract) stands, only the value moves",
        "testRepair": {"file": CONFLICT["file"], "test": CONFLICT["test"], "change": "assert == 2",
                       "why": "5 - 3 is 2, so the expected value was wrong"}}}
    run = run_workflow(tmp, root, "demo", scenario, full_gate_every=2, args={"arbiterAgent": "arbiter"})
    return {"run": run, "root": root, "target": target}


def test_tc177_the_arbiter_ruled_on_the_conflict_then_the_real_testfix_check_and_prove(tc177_run):
    run: Run = tc177_run["run"]
    assert run.prompts(r"^task 2"), "Task 2 never ran"
    rulings = run.agent_calls(r"^arbiter task 2")
    assert rulings, f"no `arbiter task 2` agent ruled on Task 2's conflict: {[a['label'] for a in run.agents]}"
    assert not [a for a in run.agents if re.search(r"confirm", a["label"], re.I) or "Check that claim yourself" in a["prompt"]], \
        "a confirmation agent checked Task 2's conflict"
    assert "2#1" in run.out.get("order", []), run.out.get("order")
    checks = run.ck("testfix", "check")
    proves = run.ck("testfix", "prove")
    assert rulings[0]["callIndex"] < run.ck_calls.index(checks[0]), "the real testfix check ran before the arbiter ruled"
    assert checks and checks[0]["code"] == 0, run.ck_calls
    assert proves and proves[0]["code"] == 0, run.ck_calls
    assert any("test_tc2_sub" in " ".join(c["argv"]) for c in checks), "ck testfix check was not given the failing node"
    assert run.ck_calls.index(checks[0]) < run.ck_calls.index(proves[0])


def test_tc177_the_test_fixer_ran_and_the_fix_was_reviewed_then_landed_for_real(tc177_run):
    run: Run = tc177_run["run"]
    assert run.prompts(r"^testfix \d+ \(Task 2\)") or run.prompts(r"^testfix \d+"), "no test-fixer agent ran"
    assert run.prompts(r"review"), "the risky review of the fix never ran"
    lands = [c for c in run.ck("worktree", "land") if any(re.fullmatch(r"testfix\d+", a) for a in c["argv"])]
    assert lands, f"no `ck worktree land demo testfix<k>`: {[c['argv'] for c in run.ck('worktree', 'land')]}"
    assert "--gate" in lands[0]["argv"] and lands[0]["code"] == 0, lands[0]
    recs = run.result.get("testFixes") or []
    assert recs and recs[0].get("outcome") == "landed", recs


def test_tc43b_after_the_land_task_2_is_retried_and_merges_and_the_final_gate_is_green(tc177_run):
    run: Run = tc177_run["run"]
    order = run.out.get("order", [])
    assert "2#1" in order and any(o.startswith("2#") and o != "2#1" for o in order), order
    fix_lands = [i for i, c in enumerate(run.ck_calls)
                 if c["argv"][2:3] == ["worktree"] and any(re.fullmatch(r"testfix\d+", a) for a in c["argv"])]
    assert fix_lands, "the test fix was never landed"
    land_idx = max(fix_lands)
    retry_land = [c for c in run.ck_calls[land_idx + 1:] if "land" in c["argv"] and "2" in c["argv"]]
    assert retry_land, "Task 2 was not landed after the test fix"
    target: Path = tc177_run["target"]
    text = (target / "specs" / "demo" / "tasks.md").read_text()
    assert re.search(r"### Task 2:.*?\*\*Status:\*\* Done", text, re.S), "Task 2 is not Done on integration"
    assert "== 2" in (target / "tests" / "test_calc_tc.py").read_text(), "the corrected test is not on integration"
    code, _, err = h.ck_json(target, "gate", "demo", "--attribute", "--json")
    assert code == 0, f"the final real gate is not green: {err}"
    final = [g for g in run.gates() if g.get("kind") == "final"]
    assert final and final[-1].get("state") == "green", run.gates()


# --------------------------------------------------------------------------- TC-179

SHIM = """#!/bin/sh
# `mtr` for the smoke test: the fixture click CLI, whose `thesis` group exists only once the thesis command's source
# file is in the current worktree. Before that it answers as click does for a command that is not registered, the
# form `missing_command` reads (12.5).
if [ -f "$PWD/src/mtr/thesis.py" ]; then
  PYTHONPATH="$PWD/src" exec python -c 'from mtr.cli import cli; cli()' "$@"
fi
echo "Usage: mtr [OPTIONS] COMMAND [ARGS]..." >&2
echo "Try 'mtr --help' for help." >&2
echo "" >&2
echo "Error: No such command '$1'." >&2
exit 2
"""

# Test task 1's test, as a test that runs a CLI reports it: the invocation and the CLI's own last line in the
# assertion message, so the gate's failure entry carries the missing command (as `tests/test_command_deps.py` does).
SMOKE_THESIS_TEST = (
    "import subprocess\n\n\n"
    "def test_thesis_new():\n"
    '    proc = subprocess.run(["mtr", "thesis", "new"], capture_output=True, text=True)\n'
    '    assert proc.returncode == 0, "mtr thesis new: " + proc.stderr.strip().splitlines()[-1]\n'
)


def command_spec(tmp_path: Path) -> Path:
    """The test_command_deps shape: test task 1 (Done) invokes `mtr thesis new`, owned by Task 5; Task 2 depends on
    Task 1 only. The project is a git repo with an integration worktree for spec `sim`."""
    from test_command_deps import command_project

    root = command_project(tmp_path, test_src=SMOKE_THESIS_TEST)
    text = (root / "specs" / "sim" / "tasks.md").read_text()
    # test task 1's suite is its Verify: Task 2's task-scoped gate runs its oracle's suite (2.10), where the command
    # `mtr thesis new` goes missing until Task 5 lands
    text = text.replace("**Verifies:** Task 2", "**Verify:** `python -m pytest -q tests/test_thesis.py`\n**Verifies:** Task 2", 1)
    (root / "specs" / "sim" / "tasks.md").write_text(mark_done(text, "1"))
    h.commit_all(root, "task 1 done")
    code, _, err = h.ck_text(root, "worktree", "integration", "sim")
    assert code == 0, err
    return root


def shim_env(tmp_path: Path) -> Dict[str, str]:
    bin_dir = tmp_path / "shim-bin"
    bin_dir.mkdir(exist_ok=True)
    mtr = bin_dir / "mtr"
    mtr.write_text(SHIM)
    mtr.chmod(0o755)
    import os

    return {"PATH": str(bin_dir) + os.pathsep + h._ck_env(tmp_path, None)["PATH"]}


def sim_scenario() -> Dict[str, Any]:
    return {"files": {
        "5": {"src/mtr/thesis.py": "def new():\n    return 'new'\n", "specs/sim/tasks.md": {"markDone": "5"}},
        "2": {"src/mtr/report.py": "def report():\n    return 'report'\n", "specs/sim/tasks.md": {"markDone": "2"}},
    }}


def test_tc179_lint_reports_the_command_edge_and_relink_adds_it(tmp_path):
    root = command_spec(tmp_path)
    code, obj, err = h.ck_json(root, "lint", "sim", "--json")
    assert obj is not None, f"ck lint --json printed no JSON (exit {code}): {err}"
    edges = obj.get("commandEdges")
    assert edges, f"lint reports no command edge: {sorted(obj)}"
    assert "thesis new" in json.dumps(edges) and "5" in json.dumps(edges)
    code, _, err = h.ck_json(root, "relink", "sim", "--commands", "--apply", "--json")
    assert code == 0, err
    from claude_kiro.hooks._shared.spec_parser import parse_tasks

    deps = {t.num: list(t.dependencies) for t in parse_tasks(root / "specs" / "sim" / "tasks.md")}
    assert "5" in deps["2"], deps


def test_tc179_with_the_relinked_edge_task_5_merges_before_task_2(tmp_path):
    root = command_spec(tmp_path)
    code, _, err = h.ck_json(root, "relink", "sim", "--commands", "--apply", "--commit", "--json")
    assert code == 0, err
    h.git(h.integration_worktree(root, "sim"), "merge", "-q", "--no-edit", "main")
    run = run_workflow(tmp_path, root, "sim", sim_scenario(), full_gate_every=10, extra_env=shim_env(tmp_path))
    order = run.out.get("order", [])
    assert order and order[0].startswith("5"), f"Task 2 started before Task 5: {order}"
    assert not run.result.get("liveEdges"), run.result.get("liveEdges")


def test_tc179_without_relink_the_run_adds_the_live_edge_and_task_2_merges_after_task_5(tmp_path):
    root = command_spec(tmp_path)
    run = run_workflow(tmp_path, root, "sim", sim_scenario(), full_gate_every=10, extra_env=shim_env(tmp_path))
    edges = run.result.get("liveEdges") or []
    assert any(str(e.get("task")) == "2" and str(e.get("dependsOn")) == "5" for e in edges), f"no live edge 2 -> 5: {edges}"
    lands = [c for c in run.ck("worktree", "land")]
    landed = [next((a for a in c["argv"] if a in ("2", "5")), None) for c in lands]
    landed = [a for a in landed if a]
    assert landed.index("5") < landed.index("2"), f"Task 2 landed before Task 5: {landed}"
    target = h.integration_worktree(root, "sim")
    text = (target / "specs" / "sim" / "tasks.md").read_text()
    for n in ("2", "5"):
        assert re.search(rf"### Task {n}:.*?\*\*Status:\*\* Done", text, re.S), f"Task {n} is not Done"
