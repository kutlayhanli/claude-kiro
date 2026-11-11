"""Verify-scope lint through the real CLI (run-principles Task 18: TC-109 to 113, 117, 118, 119, 136, 145).

Everything runs the real `ck` (`python -m claude_kiro.cli`) over real generated tasks.md files and real test files in a
temp git project. Nothing of the project's own is mocked. Project modules are imported inside test bodies, so the file
collects before the feature exists and each case fails on the missing `verifyScope` behaviour.

The entry shapes of `verifyScope` / `notChecked` are not fixed key by key in the test plan for the finding message, so
assertions look at what an entry names (any leaf value). Design.md fixes `task`, `suite` and `testTask` on a finding
(`ScopeFinding`); those keys are used where a case needs them.

Layout used by most cases: test tasks 1 and 2 share `tests/test_shared.py`; task 1 verifies impl task 3, task 2 verifies
impl task 4. An unfiltered run of the file at a gate where task 4 is not Done selects task 4's tests (12.2).
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from unattended_helpers import ck_json, ck_text, fake_claude, spec_graph_project  # noqa: E402

SPEC = "sim"
SHARED = "tests/test_shared.py"
SHARED_SRC = "def test_tc1_alpha():\n    assert True\n\n\ndef test_tc2_beta():\n    assert True\n"
RUN = f"uv run pytest -q {SHARED}"
RUN_T1 = f"{RUN} -k test_tc1_"
RUN_T2 = f"{RUN} -k test_tc2_"


# --------------------------------------------------------------------------- helpers


def blk(num, title, **kw) -> str:
    from test_lint import block

    return block(num, title, **kw)


def leaves(obj: Any) -> List[str]:
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in leaves(v)] + [str(k) for k in obj]
    if isinstance(obj, (list, tuple)):
        return [x for v in obj for x in leaves(v)]
    return [str(obj)]


def mentions(entry: Any, needle: str) -> bool:
    ls = leaves(entry)
    if needle.isdigit():
        return any(x in (needle, f"Task {needle}", f"task:{needle}") for x in ls)
    return any(needle in x for x in ls)


def lint(root: Path) -> dict:
    code, obj, err = ck_json(root, "lint", SPEC, "--json")
    assert obj is not None, f"ck lint --json printed no JSON (exit {code}): {err}"
    return obj


def key(obj: dict, name: str) -> list:
    assert name in obj, f"lint JSON has no {name!r} key: {sorted(obj)}"
    return list(obj[name])


def shared_blocks(*, v1: str = RUN, v2: str = RUN_T2, v3: str = RUN, v4: str = "",
                  deps3: str = "Task 1", deps4: str = "Task 2", status3: str = "Not Started",
                  status4: str = "Not Started", status1: str = "Not Started", status2: str = "Not Started",
                  extra: str = "") -> str:
    return (
        blk(1, "Alpha tests", track="test", files=[SHARED], verify=v1, verifies="Task 3", status=status1)
        + blk(2, "Beta tests", track="test", files=[SHARED], verify=v2, verifies="Task 4", status=status2)
        + blk(3, "Alpha core", files=["src/mtr/a.py"], verify=v3, vby="Task 1", deps=deps3, status=status3)
        + blk(4, "Beta core", files=["src/mtr/b.py"], verify=v4, vby="Task 2", deps=deps4, status=status4)
        + extra
    )


def shared_project(tmp_path: Path, **kw) -> Path:
    return spec_graph_project(tmp_path, tasks=shared_blocks(**kw), tests={SHARED: SHARED_SRC})


def finding_for(found: list, suite: str = SHARED) -> list:
    return [e for e in found if mentions(e, suite)]


# --------------------------------------------------------------------------- TC-109 (12.1)


def test_tc109_simulate_lists_due_suites_and_selections_for_not_done_tasks(tmp_path):
    root = shared_project(tmp_path, status1="Done", status3="Done", v1=RUN_T1, v3=RUN_T1)
    # Tasks 2 and 4 are not Done: task 4's gate makes task 2's suite due and runs it.
    code, obj, err = ck_json(root, "lint", SPEC, "--simulate", "--task", "4", "--json")
    assert obj is not None, f"--simulate printed no JSON (exit {code}): {err}"
    assert str(obj["task"]) == "4"
    assert any(mentions(s, SHARED) for s in obj["suites"]), obj["suites"]
    assert any(mentions(s, SHARED) for s in obj["selected"]), obj["selected"]
    assert all({"file", "tests"} <= set(s) for s in obj["selected"]), obj["selected"]

    code, obj, err = ck_json(root, "lint", SPEC, "--simulate", "--task", "2", "--json")
    assert obj is not None, f"--simulate printed no JSON (exit {code}): {err}"
    assert str(obj["task"]) == "2"
    assert any(mentions(s, SHARED) for s in obj["selected"]), obj["selected"]


def test_tc109_done_tasks_are_absent(tmp_path):
    root = shared_project(tmp_path, status1="Done", status3="Done", v1=RUN_T1, v3=RUN_T1)
    for done in ("1", "3"):
        code, obj, err = ck_json(root, "lint", SPEC, "--simulate", "--task", done, "--json")
        assert obj is None or (not obj.get("suites") and not obj.get("selected")), (done, obj)
    result = lint(root)
    for name in ("verifyScope", "notChecked"):
        for entry in key(result, name):
            assert not mentions(entry, "1") and not mentions(entry, "3"), (name, entry)


# --------------------------------------------------------------------------- TC-110 (12.2)


def test_tc110_unfiltered_shared_suite_names_task_and_suite(tmp_path):
    root = shared_project(tmp_path)
    found = finding_for(key(lint(root), "verifyScope"))
    assert found, "no verifyScope finding for the unfiltered shared suite"
    assert any(mentions(e, "1") or mentions(e, "3") for e in found), found
    assert any(mentions(e, "2") for e in found), f"no finding names the test task that makes it shared: {found}"


def test_tc110_no_finding_when_the_second_task_is_done(tmp_path):
    root = shared_project(tmp_path, status2="Done", status4="Done")
    assert finding_for(key(lint(root), "verifyScope")) == []


# --------------------------------------------------------------------------- TC-111 (12.3)

THESIS_CMD = "mtr thesis new"
TEST_THESIS = (
    "import subprocess\n\n\ndef test_thesis_new():\n"
    '    assert subprocess.run(["mtr", "thesis", "new"]).returncode == 0\n'
)


def test_tc111_later_tasks_command_is_not_a_scope_finding_and_stays_a_command_edge(tmp_path):
    tasks = (
        blk(1, "Thesis tests", track="test", files=["tests/test_thesis.py"], verifies="Task 2")
        + blk(2, "Report", files=["src/mtr/report.py"], vby="Task 1", deps="Task 1",
              verify="uv run pytest -q tests/test_thesis.py")
        + blk(5, "Thesis command", files=["src/mtr/thesis.py"]).replace(
            "**Dependencies:**", f"**Provides:** `{THESIS_CMD}`\n**Dependencies:**", 1)
    )
    root = spec_graph_project(
        tmp_path, tasks=tasks,
        design_cli={THESIS_CMD: "src/mtr/thesis.py", "mtr version": "src/mtr/cli.py"},
        tests={"tests/test_thesis.py": TEST_THESIS},
    )
    result = lint(root)
    assert key(result, "verifyScope") == []
    edges = key(result, "commandEdges")
    assert len(edges) == 1, edges
    for needle in ("tests/test_thesis.py", THESIS_CMD, "5", "2"):
        assert mentions(edges[0], needle), (needle, edges[0])
    code, _, _ = ck_text(root, "lint", SPEC)
    assert code == 0


# --------------------------------------------------------------------------- TC-112 (12.4)


def test_tc112_control_the_unfiltered_shape_is_a_finding(tmp_path):
    root = shared_project(tmp_path, v1=RUN_T1, v2=RUN_T2, v3=RUN)
    assert finding_for(key(lint(root), "verifyScope")), "control: unfiltered impl Verify must be a finding"


def test_tc112_suite_not_due_gives_no_finding(tmp_path):
    # The only unfiltered run is on a Done task, which is never simulated; everything due is filtered.
    root = shared_project(tmp_path, v1=RUN_T1, v2=RUN_T2, v3=RUN, status3="Done", status1="Done")
    assert finding_for(key(lint(root), "verifyScope")) == []


def test_tc112_filter_that_excludes_the_tests_gives_no_finding(tmp_path):
    root = shared_project(tmp_path, v1=RUN_T1, v2=RUN_T2, v3=RUN_T1)
    assert finding_for(key(lint(root), "verifyScope")) == []


def test_tc112_direct_dependency_gives_no_finding(tmp_path):
    root = shared_project(tmp_path, v1=RUN_T1, v2=RUN_T2, v3=RUN, deps3="Task 1, Task 4")
    assert finding_for(key(lint(root), "verifyScope")) == []


def test_tc112_transitive_dependency_gives_no_finding(tmp_path):
    extra = blk(5, "Middle", files=["src/mtr/m.py"], deps="Task 4")
    root = shared_project(tmp_path, v1=RUN_T1, v2=RUN_T2, v3=RUN, deps3="Task 1, Task 5", extra=extra)
    assert finding_for(key(lint(root), "verifyScope")) == []


# --------------------------------------------------------------------------- TC-113 (12.5)


def test_tc113_finding_fails_lint_and_appears_in_the_plan(tmp_path):
    root = shared_project(tmp_path)
    code, _, _ = ck_text(root, "lint", SPEC)
    assert code != 0
    code, obj, err = ck_json(root, "plan", SPEC, "--json")
    assert obj is not None, f"ck plan --json printed no JSON (exit {code}): {err}"
    assert finding_for(key(obj, "verifyScope")), obj.get("verifyScope")


def test_tc113_lint_exits_zero_without_a_finding(tmp_path):
    root = shared_project(tmp_path, v1=RUN_T1, v2=RUN_T2, v3=RUN_T1)
    result = lint(root)
    assert key(result, "verifyScope") == []
    code, _, _ = ck_text(root, "lint", SPEC)
    assert code == 0


# --------------------------------------------------------------------------- TC-117 (12.8)


def test_tc117_direct_lint_starts_no_agent_and_names_task_and_suite(tmp_path):
    root = shared_project(tmp_path)
    bin_dir = fake_claude(tmp_path)
    env = {"PATH": os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent), os.environ.get("PATH", "")])}

    code, out, _ = ck_text(root, "lint", SPEC, env=env)
    assert code != 0
    lines = [ln.lower() for ln in out.splitlines() if SHARED in ln]
    assert any(("task 1" in ln or "task 3" in ln) for ln in lines), out

    code, obj, err = ck_json(root, "lint", SPEC, "--json", env=env)
    assert obj is not None, err
    found = finding_for(key(obj, "verifyScope"))
    assert found and any(mentions(e, "1") or mentions(e, "3") for e in found), found

    assert not (bin_dir / "record.json").exists(), "ck lint started an agent"


# --------------------------------------------------------------------------- TC-118 (12.9)


@pytest.mark.parametrize("verify", ["uv run pytest -q $(ls tests)", "bash scripts/run_tests.sh"])
def test_tc118_unresolvable_verify_is_not_checked_and_does_not_block(tmp_path, verify):
    tasks = blk(1, "Core", files=["src/mtr/a.py"], verify=verify)
    root = spec_graph_project(tmp_path, tasks=tasks, tests={SHARED: SHARED_SRC, "scripts/run_tests.sh": "pytest\n"})
    result = lint(root)
    entries = key(result, "notChecked")
    assert any(mentions(e, "1") for e in entries), entries
    assert key(result, "verifyScope") == []
    code, _, _ = ck_text(root, "lint", SPEC)
    assert code == 0


# --------------------------------------------------------------------------- TC-119 (12.10)


def test_tc119_problem_already_in_verify_cycles_appears_once(tmp_path):
    from test_lint_verify_files import spec_blocks, PERF

    root = spec_graph_project(tmp_path, tasks=spec_blocks(), tests={})
    result = lint(root)
    cycles = [e for e in key(result, "verifyCycles") if mentions(e, PERF)]
    assert len(cycles) == 1, cycles
    assert [e for e in key(result, "verifyScope") if mentions(e, PERF)] == []
    assert [e for e in key(result, "notChecked") if mentions(e, PERF)] == []


# --------------------------------------------------------------------------- TC-136 (3.1, 12.2)


def test_tc136_justification_findings_agree_with_the_cli(tmp_path):
    from claude_kiro.justifications import justification_findings

    root = shared_project(tmp_path)
    text = (
        "# Requirements: Sim\n\n### Story 1: One\n\n**As a** user **I want** x **So that** y.\n\n"
        "**Without it:** something breaks.\n\n### Story 2: Two\n\n**As a** user **I want** x **So that** y.\n"
    )
    (root / "specs" / SPEC / "requirements.md").write_text(text)
    seam = {str(f.story) for f in justification_findings(text)}
    assert seam == {"2"}, seam
    cli = {str(e["story"]) for e in key(lint(root), "justifications")}
    assert cli == seam


def test_tc136_scope_findings_agree_with_the_cli(tmp_path):
    from claude_kiro.hooks._shared.spec_parser import parse_tasks
    from claude_kiro.lint_scope import SimInput, SimTest, scope_findings

    root = shared_project(tmp_path, v1="", v2=RUN_T2, v3=RUN)
    cli = {(str(e["task"]), e["suite"], str(e["testTask"])) for e in key(lint(root), "verifyScope")}
    assert cli, "the CLI reported no finding for the failing fixture"

    tasks = {t.num: t for t in parse_tasks(root / "specs" / SPEC / "tasks.md")}
    sim = SimInput(
        tasks=tasks,
        runs={"3": {SHARED: None}},
        unresolved={},
        tests={SHARED: [SimTest(name="test_tc1_alpha", cls="", source="def test_tc1_alpha():\n    assert True\n"),
                        SimTest(name="test_tc2_beta", cls="", source="def test_tc2_beta():\n    assert True\n")]},
        module_source={SHARED: ""},
        programs=("mtr",),
    )
    seam = {(f.task, f.suite, f.testTask) for f in scope_findings(sim).findings}
    assert seam <= cli and seam, (seam, cli)
    assert {t for t, _, _ in seam} == {"3"}


# --------------------------------------------------------------------------- TC-145 (12.9)


def test_tc145_selected_test_file_not_on_disk_is_not_checked(tmp_path):
    tasks = blk(1, "Core", files=["src/mtr/a.py"], verify="uv run pytest -q tests/test_missing.py")
    root = spec_graph_project(tmp_path, tasks=tasks, tests={})
    assert not (root / "tests/test_missing.py").exists()
    result = lint(root)
    entries = key(result, "notChecked")
    assert any(mentions(e, "1") for e in entries), entries
    assert key(result, "verifyScope") == []
    code, _, _ = ck_text(root, "lint", SPEC)
    assert code == 0


# =========================================================================== Task 19: launch blocked by findings
#
# TC-114, 115, 116, 143, 144 and 209 run the real `plugin/workflows/spec-implement.js` under Node, with the OpenCode
# path's direct `ck()` global (every ck call is the real CLI: real `ck plan`, `ck lint` inside it, `ck arbiter ...`,
# `ck worktree land ...`) on a real git project with a real integration worktree. Only the agents are scripted: the
# arbiter answers a `rulings` list per task and really writes and commits its `arbiterEdits` in the arbiter checkout its
# prompt names (real git, as fake_project's `arbiterEdits`), the test-fixer commits `testfix` files, task agents run the
# prompt's own worktree and gate commands through the real ck (`tests/js/real_agents.mjs`).
# Before the launch handling exists the workflow starts tasks despite the findings, so each case fails on that.

ROOT_DIR = Path(__file__).resolve().parent.parent
JS_DIR = ROOT_DIR / "tests" / "js"
WORKFLOW = ROOT_DIR / "plugin" / "workflows" / "spec-implement.js"
TASKS_HEAD = "# Implementation Tasks: Sim\n\n## Task Breakdown\n\n"
LATE = "tests/test_late.py"
RUN_LATE = f"uv run pytest -q {LATE}"
THREE_SRC = (
    "def test_tc1_alpha():\n    assert True\n\n\ndef test_tc2_beta():\n    assert True\n\n\n"
    "def test_tc3_gamma():\n    assert True\n"
)
PRINCIPLES_TEXT = (
    "# Project Principles\n\nDefaults every spec run upholds.\n\n"
    "- **priority-order:** requirements > design > test plan > task wiring.\n"
    "- **test-intent:** a test keeps its intent; only selection and setup may move.\n"
)

DRIVER2 = r"""
import fs from 'node:fs'
import { execFile } from 'node:child_process'
import path from 'node:path'
import { runWorkflow } from '__JS__/load_workflow.mjs'
import { realAgents } from '__JS__/real_agents.mjs'

const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
const sc = cfg.scenario || {}
const ckSpec = { cmd: cfg.ckCmd, cwd: cfg.root, env: cfg.env }
const ckCalls = []
const agentLog = []
const calls = {}
const edits = []
let seen
const rulingSeen = new Promise(r => { seen = r })

const exec = (bin, args, opts = {}) => new Promise(resolve => {
  const child = execFile(bin, args, { cwd: opts.cwd || cfg.root, env: { ...process.env, ...cfg.env }, encoding: 'utf8', maxBuffer: 1 << 26 }, (err, out, errOut) => {
    resolve({ code: err ? (typeof err.code === 'number' ? err.code : -1) : 0, stdout: out || '', stderr: errOut || '' })
  })
  child.stdin.on('error', () => {})
  child.stdin.end(opts.stdin == null ? '' : opts.stdin)
})
const parse = t => { try { const v = JSON.parse(t); return v && typeof v === 'object' ? v : null } catch { return null } }
// The workflow's direct ck: ck(argv, {cwd, stdin}) -> {code, stdout, stderr, json}
const ck = async (argv, o = {}) => {
  const [bin, ...pre] = cfg.ckCmd
  const res = await exec(bin, [...pre, ...argv], { cwd: o.cwd, stdin: o.stdin })
  const r = { ...res, json: parse(res.stdout) }
  ckCalls.push({ argv, code: r.code, json: r.json })
  return r
}
const git = (dir, ...a) => exec('git', ['-C', dir, ...a])
const IDENT = ['-c', 'user.name=arbiter', '-c', 'user.email=arbiter@example.invalid']
const commitFiles = async (dir, files, message) => {
  for (const [rel, content] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(dir, rel)), { recursive: true })
    fs.writeFileSync(path.join(dir, rel), content)
  }
  await git(dir, 'add', '-A')
  const c = await git(dir, ...IDENT, 'commit', '-q', '-m', message)
  const head = await git(dir, 'rev-parse', '--short', 'HEAD')
  return { code: c.code, head: head.stdout.trim() }
}
const markDone = (text, n) => {
  const start = text.indexOf(`### Task ${n}:`)
  if (start < 0) return text
  const nxt = text.indexOf('\n### Task ', start + 1)
  const end = nxt < 0 ? text.length : nxt
  const block = text.slice(start, end).replace('**Status:** Not Started', '**Status:** Done').replaceAll('- [ ]', '- [x]')
  return text.slice(0, start) + block + text.slice(end)
}
const scenarioFiles = n => Object.fromEntries(Object.entries((sc.files && sc.files[n]) || {}).map(([rel, v]) =>
  [rel, v && typeof v === 'object' && v.markDone ? cur => markDone(cur, String(v.markDone)) : v]))
const worktreeFor = async (prompt, name) => {
  const m = prompt.match(/(\/[^\s"'`]*-task-[\w-]*?\d+)/)
  return m ? m[1] : path.join(cfg.root, '.ck', 'worktrees', `${cfg.spec}-task-${name}`)
}

const agent = async (prompt, opts) => {
  const label = (opts && opts.label) || ''
  agentLog.push({ label, prompt, callIndex: ckCalls.length })
  if (/^audit task /.test(label)) return { problems: [], summary: 'scripted: the selected tests are sound' }
  const ar = label.match(/^arbiter task (\d+)/)
  if (ar) {
    const n = ar[1]
    const list = sc.rulings && sc.rulings[n]
    if (!list || !list.length) throw new Error(`no ruling scripted for "${label}"`)
    const k = calls[n] = (calls[n] || 0) + 1
    const key = (prompt.match(/\b\d+\|lint:[^\s"'`,)]+/) || prompt.match(/\b\d+\|[^\s"'`,)]+/) || [''])[0]
    const ruling = list[Math.min(k, list.length) - 1]
    const edit = sc.arbiterEdits && sc.arbiterEdits[n] && sc.arbiterEdits[n][Math.min(k, sc.arbiterEdits[n].length) - 1]
    if (edit) {
      const dm = prompt.match(/(\/[^\s"'`]*-task-arbiter\d+)/)
      if (!dm) throw new Error('the arbiter prompt names no arbiter checkout (a path ending -task-arbiter<k>)')
      const c = await commitFiles(dm[1], edit, `arbiter: task ${n} ruling`)
      edits.push({ task: n, dir: dm[1], code: c.code, head: c.head })
    }
    seen()
    return { claim: key, status: 'ruled', principle: 'project:test-intent', ...ruling }
  }
  const t = label.match(/^task (\d+)/)
  if (t && !/ revise /.test(label)) {
    const n = t[1]
    if ((sc.holdUntilRuling || []).includes(n)) await Promise.race([rulingSeen, new Promise(r => setTimeout(r, 40000))])
    const a = realAgents({ ck: ckSpec, scenario: { files: { [n]: scenarioFiles(n) } } })
    return a.agent(prompt, opts)
  }
  if (/^testfix review/.test(label) || /^review/.test(label)) return { verdict: 'approve', findings: [], summary: 'scripted approve' }
  const tf = label.match(/^testfix (\d+)/)
  if (tf) {
    const dir = await worktreeFor(prompt, `testfix${tf[1]}`)
    const c = await commitFiles(dir, sc.testfix || {}, 'test fix: scripted correction')
    return { verdict: 'fixed', changedTests: sc.testfixChanged || [], commits: c.head, summary: 'scripted test fix' }
  }
  return { exitCode: 1, json: null, error: `scripted driver has no answer for "${label}"` }
}

const out = { ok: true }
try {
  const planned = await ck(['-C', cfg.target, 'plan', cfg.spec, '--json'])
  ckCalls.pop()
  const args = { spec: cfg.spec, root: cfg.root, into: cfg.into, targetDir: cfg.target, deps: (planned.json && planned.json.deps) || {}, tracks: cfg.tracks, titles: cfg.titles, fullGateEvery: 50, mergeBatch: 4, ...(cfg.args || {}) }
  const { result, logs } = await runWorkflow(cfg.workflow, { agent, args, ck })
  out.result = result
  out.logs = logs
} catch (e) { out.ok = false; out.error = String((e && e.stack) || e) }
out.ckCalls = ckCalls
out.agentLog = agentLog
out.edits = edits
process.stdout.write('\n@@RESULT@@' + JSON.stringify(out) + '\n')
"""


def launch_project(tmp_path: Path, blocks: str, tests: dict) -> Path:
    """A git project on `main` with spec `sim` (real tasks.md and tests), a PRINCIPLES.md and the integration worktree."""
    import unattended_helpers as h

    root = spec_graph_project(tmp_path, tasks=blocks, tests=tests)
    (root / ".gitignore").write_text(".venv/\n.ck/worktrees/\n__pycache__/\n.pytest_cache/\n")
    (root / "specs" / "PRINCIPLES.md").write_text(PRINCIPLES_TEXT)
    (root / "specs" / "ck.json").write_text(
        json.dumps({"verify": ["python -m pytest -q -p no:cacheprovider tests"]}, indent=2) + "\n")
    h.commit_all(root, "principles and config")
    code, _, err = ck_text(root, "worktree", "integration", SPEC)
    assert code == 0, f"ck worktree integration failed: {err}"
    return root


class Launch:
    """One workflow run: the result, every ck call and agent prompt, and the arbiter's real commits."""

    def __init__(self, out: dict, root: Path, target: Path) -> None:
        import re

        self.out, self.root, self.target = out, root, target
        self.result = out.get("result") or {}
        self.ck_calls = out.get("ckCalls", [])
        self.agents = out.get("agentLog", [])
        self.edits = out.get("edits", [])
        self._re = re

    def labels(self, pattern: str) -> List[dict]:
        return [a for a in self.agents if self._re.search(pattern, a["label"])]

    def ck(self, *words: str) -> List[dict]:
        found = []
        for call in self.ck_calls:
            argv = call["argv"]
            rest = argv[2:] if argv[:1] == ["-C"] else argv
            if rest[: len(words)] == list(words):
                found.append(call)
        return found

    def task_labels(self) -> List[str]:
        return [a["label"] for a in self.agents if self._re.match(r"task \d+", a["label"])]


def run_launch(tmp_path: Path, root: Path, scenario: dict, args: dict) -> Launch:
    import re
    import subprocess
    import unattended_helpers as h

    target = h.integration_worktree(root, SPEC)
    text = (target / "specs" / SPEC / "tasks.md").read_text()
    cfg = {
        "root": str(root), "target": str(target), "spec": SPEC, "into": f"integrate/{SPEC}", "workflow": str(WORKFLOW),
        "ckCmd": h.CK_ARGV, "env": h._ck_env(root, None), "scenario": scenario, "args": args,
        "tracks": {m.group(1): m.group(2).strip() for m in re.finditer(r"### Task (\d+):.*?\*\*Track:\*\* (\w+)", text, re.S)},
        "titles": {m.group(1): m.group(2).strip() for m in re.finditer(r"### Task (\d+): (.*)", text)},
    }
    work = tmp_path / "driver"
    work.mkdir(exist_ok=True)
    (work / "cfg.json").write_text(json.dumps(cfg))
    (work / "driver.mjs").write_text(DRIVER2.replace("__JS__", JS_DIR.as_uri()))
    proc = subprocess.run(["node", str(work / "driver.mjs"), str(work / "cfg.json")], cwd=work, capture_output=True,
                          text=True, timeout=900)
    assert "@@RESULT@@" in proc.stdout, f"driver printed no result (exit {proc.returncode}): {proc.stderr[-2000:]}"
    out = json.loads(proc.stdout.split("@@RESULT@@", 1)[1])
    assert out.get("ok"), f"the workflow run threw: {out.get('error')}"
    return Launch(out, root, target)


def done_files(n: int, **extra: str) -> dict:
    return {**extra, f"specs/{SPEC}/tasks.md": {"markDone": str(n)}}


def impl_files(n: int, src: str) -> dict:
    return done_files(n, **{src: f"VALUE_{n} = {n}\n"})


def target_lint(run: Launch) -> dict:
    code, obj, err = ck_json(run.target, "lint", SPEC, "--json")
    assert obj is not None, f"ck lint --json printed no JSON (exit {code}): {err}"
    return obj


def need_node() -> None:
    import shutil

    if shutil.which("node") is None:  # a missing tool of the machine, not of the project: the gate machine has node
        pytest.fail("node is not installed; the workflow tests need it")


def scope_launch_blocks(**kw) -> str:
    """The shared-suite layout where Task 3's unfiltered run selects Task 2's tests; Task 4 runs its own filter."""
    kw.setdefault("v4", RUN_T2)
    return shared_blocks(**kw)


SPEC_EDIT = {"status": "ruled", "outcome": "spec_edit", "principle": "project:priority-order"}
ALL_FILES = {
    "1": done_files(1), "2": done_files(2),
    "3": impl_files(3, "src/mtr/a.py"), "4": impl_files(4, "src/mtr/b.py"),
}


# --------------------------------------------------------------------------- TC-114 (12.5)


def test_tc114_a_plan_with_verify_scope_refuses_the_launch_and_no_task_agent_runs(tmp_path):
    need_node()
    root = launch_project(tmp_path, scope_launch_blocks(), {SHARED: SHARED_SRC})
    run = run_launch(tmp_path, root, {"files": ALL_FILES}, {})  # no arbiterAgent: nothing can fix the finding
    assert run.result.get("refused") is True, {k: run.result.get(k) for k in ("halted", "refused", "reason")}
    assert run.result.get("halted") is True
    scope = list(run.result.get("verifyScope") or [])
    assert finding_for(scope), f"the refusal does not carry the finding: {run.result.get('verifyScope')}"
    assert run.task_labels() == [], f"a task agent ran: {run.task_labels()}"
    assert not run.ck("worktree", "create"), "a task worktree was created"


# --------------------------------------------------------------------------- TC-115 (12.6)


def fixed_tasks(**kw) -> str:
    return TASKS_HEAD + scope_launch_blocks(**kw)


def test_tc115_one_lint_claim_per_problem_and_a_wiring_edit_lands(tmp_path):
    need_node()
    root = launch_project(tmp_path, scope_launch_blocks(), {SHARED: SHARED_SRC})
    scenario = {
        "files": ALL_FILES,
        "rulings": {"3": [{**SPEC_EDIT, "reasoning": "task 3 only needs its own tests; filter its Verify",
                           "changed": "specs/sim/tasks.md: Task 3 Verify filtered to test_tc1_"}]},
        "arbiterEdits": {"3": [{f"specs/{SPEC}/tasks.md": fixed_tasks(v3=RUN_T1)}]},
    }
    run = run_launch(tmp_path, root, scenario, {"arbiterAgent": "arbiter"})
    rulings = run.labels(r"^arbiter task ")
    assert len(rulings) == 1, f"expected one claim for the one problem, got {[a['label'] for a in rulings]}"
    prompt = rulings[0]["prompt"]
    assert "lint" in prompt and SHARED in prompt, "the claim does not say it is a lint finding on the shared suite"
    assert run.edits and run.edits[0]["code"] == 0, f"the arbiter's edit was not committed: {run.edits}"
    lands = [c for c in run.ck("worktree", "land") if any(a.startswith("arbiter") for a in c["argv"])]
    assert lands and lands[0]["code"] == 0, f"the wiring edit was not landed through ck worktree land: {run.ck_calls}"
    for step in ("open", "settle", "record"):
        calls = run.ck("arbiter", step)
        assert calls and calls[0]["code"] == 0, f"ck arbiter {step} did not run cleanly: {calls}"
    import unattended_helpers as h

    changed = h.git(run.target, "log", "--format=%an", "--", f"specs/{SPEC}/tasks.md")
    assert "arbiter" in changed.split(), f"no commit by the arbiter touched tasks.md on the target: {changed!r}"
    assert RUN_T1 in (run.target / "specs" / SPEC / "tasks.md").read_text()
    assert key(target_lint(run), "verifyScope") == [], "real lint still reports a finding after the edit landed"
    assert run.result.get("refused") is not True
    assert run.task_labels(), "the launch did not go on once no finding remained"


# --------------------------------------------------------------------------- TC-116 (12.7)


def test_tc116_a_remaining_finding_keeps_the_launch_blocked_and_shows_the_proposed_fix(tmp_path):
    need_node()
    root = launch_project(tmp_path, scope_launch_blocks(), {SHARED: SHARED_SRC})
    proposed = "narrowed Task 3 Verify to test_tc2_ (still Task 2's tests)"
    scenario = {
        "files": ALL_FILES,
        "rulings": {"3": [{**SPEC_EDIT, "reasoning": "filter the Verify", "changed": proposed}]},
        # The edit lands but picks the wrong filter: Task 3 still selects Task 2's tests.
        "arbiterEdits": {"3": [{f"specs/{SPEC}/tasks.md": fixed_tasks(v3=RUN_T2)}]},
    }
    run = run_launch(tmp_path, root, scenario, {"arbiterAgent": "arbiter"})
    assert len(run.ck("plan")) >= 2, "no plan (and so no lint) ran after the fix landed"
    assert run.result.get("refused") is True, {k: run.result.get(k) for k in ("halted", "refused", "reason")}
    assert finding_for(list(run.result.get("verifyScope") or [])), "the remaining finding is not shown"
    proposals = run.result.get("proposals")
    assert proposals, f"the result carries no proposals: {sorted(run.result)}"
    assert any(proposed in leaf for leaf in leaves(proposals)), f"the proposed fix is not shown: {proposals}"
    assert len(run.labels(r"^arbiter task ")) == 1, "the same finding went to the arbiter twice"
    assert run.task_labels() == [], f"a task agent ran while the finding remained: {run.task_labels()}"


# --------------------------------------------------------------------------- TC-143 (12.6)


def three_blocks(**kw) -> str:
    v4, v5, v6 = kw.get("v4", RUN), kw.get("v5", RUN), kw.get("v6", f"{RUN} -k test_tc3_")
    return (
        blk(1, "Alpha tests", track="test", files=[SHARED], verify=RUN_T1, verifies="Task 4")
        + blk(2, "Beta tests", track="test", files=[SHARED], verify=RUN_T2, verifies="Task 5")
        + blk(3, "Gamma tests", track="test", files=[SHARED], verify=f"{RUN} -k test_tc3_", verifies="Task 6")
        + blk(4, "Alpha core", files=["src/mtr/a.py"], verify=v4, vby="Task 1", deps="Task 1")
        + blk(5, "Beta core", files=["src/mtr/b.py"], verify=v5, vby="Task 2", deps="Task 2")
        + blk(6, "Gamma core", files=["src/mtr/c.py"], verify=v6, vby="Task 3", deps="Task 3")
    )


def test_tc143_one_claim_lists_both_test_tasks_and_one_fix_clears_every_finding(tmp_path):
    need_node()
    root = launch_project(tmp_path, three_blocks(), {SHARED: THREE_SRC})
    before = lint(root)
    t4 = [e for e in key(before, "verifyScope") if mentions(e, "4")]
    assert any(mentions(e, "2") for e in t4) and any(mentions(e, "3") for e in t4), \
        f"control: Task 4's unfiltered run must name test tasks 2 and 3: {t4}"
    assert [e for e in key(before, "verifyScope") if mentions(e, "5")], "control: Task 5 must have a finding too"

    fixed = TASKS_HEAD + three_blocks(v4=RUN_T1, v5=RUN_T2)
    scenario = {
        "files": {"1": done_files(1), "2": done_files(2), "3": done_files(3), "4": impl_files(4, "src/mtr/a.py"),
                  "5": impl_files(5, "src/mtr/b.py"), "6": impl_files(6, "src/mtr/c.py")},
        "rulings": {"4": [{**SPEC_EDIT, "reasoning": "filter the unfiltered Verify lines",
                           "changed": "Tasks 4 and 5 Verify filtered"}]},
        "arbiterEdits": {"4": [{f"specs/{SPEC}/tasks.md": fixed}]},
    }
    run = run_launch(tmp_path, root, scenario, {"arbiterAgent": "arbiter"})
    import re

    rulings = run.labels(r"^arbiter task ")
    assert len(rulings) == 1, f"one fix clears both findings, so one ruling: {[a['label'] for a in rulings]}"
    assert rulings[0]["label"].startswith("arbiter task 4"), rulings[0]["label"]
    assert re.search(r"\b2\b", rulings[0]["prompt"]) and re.search(r"\b3\b", rulings[0]["prompt"]), \
        "the claim does not list both test tasks"
    assert run.result.get("needsInput") == [], run.result.get("needsInput")
    assert run.result.get("refused") is not True
    assert key(target_lint(run), "verifyScope") == []
    assert run.task_labels(), "the launch did not go on once the findings had gone"


# --------------------------------------------------------------------------- TC-144 (12.11)


def late_blocks(**kw) -> str:
    return (
        blk(1, "Alpha tests", track="test", files=[LATE], verify=f"uv run pytest -q {LATE} -k test_tc1_", verifies="Task 3")
        + blk(2, "Beta tests", track="test", files=[LATE], verify=f"uv run pytest -q {LATE} -k test_tc2_",
              verifies="Task 4", deps="Task 1")
        + blk(3, "Alpha core", files=["src/mtr/a.py"], verify=kw.get("v3", RUN_LATE), vby="Task 1", deps="Task 1")
        + blk(4, "Beta core", files=["src/mtr/b.py"], verify=f"uv run pytest -q {LATE} -k test_tc2_", vby="Task 2",
              deps="Task 2")
    )


def test_tc144_a_finding_after_launch_is_ruled_before_its_task_starts_and_only_once(tmp_path):
    need_node()
    root = launch_project(tmp_path, late_blocks(), {})
    assert not (root / LATE).exists()
    assert key(lint(root), "verifyScope") == [], "control: the test file is not on disk at launch, so nothing is checked"

    scenario = {
        "files": {
            "1": done_files(1, **{LATE: SHARED_SRC}),
            "2": done_files(2),
            "3": impl_files(3, "src/mtr/a.py"),
            "4": impl_files(4, "src/mtr/b.py"),
        },
        # Task 2 (the test task that makes the shared file shared) is held until the arbiter has been called, so the
        # finding for Task 3 is still there when the plan after Task 1's merge reads it.
        "holdUntilRuling": ["2"],
        "rulings": {"3": [{**SPEC_EDIT, "reasoning": "filter Task 3's Verify", "changed": "Task 3 Verify filtered"}]},
        "arbiterEdits": {"3": [{f"specs/{SPEC}/tasks.md": TASKS_HEAD + late_blocks(v3=f"{RUN_LATE} -k test_tc1_")}]},
    }
    run = run_launch(tmp_path, root, scenario, {"arbiterAgent": "arbiter"})
    assert run.result.get("refused") is not True, "the launch was refused; the finding only appears after it"
    order = [a["label"] for a in run.agents]
    rulings = [i for i, lab in enumerate(order) if lab.startswith("arbiter task 3")]
    starts = [i for i, lab in enumerate(order) if lab.startswith("task 3")]
    assert len(rulings) == 1, f"the finding must be ruled exactly once: {order}"
    assert starts, f"Task 3 never started: {order}"
    assert rulings[0] < starts[0], f"Task 3 started before its claim settled: {order}"
    assert "lint" in run.agents[rulings[0]]["prompt"] and LATE in run.agents[rulings[0]]["prompt"]
    assert len(run.ck("plan")) >= 3, "the run did not plan again after the claim"


# --------------------------------------------------------------------------- TC-209 (12.6, 8.9)


def test_tc209_a_launch_claim_ruled_test_repair_goes_to_the_fixer_then_the_launch_goes_on(tmp_path):
    need_node()
    root = launch_project(tmp_path, scope_launch_blocks(), {SHARED: SHARED_SRC})
    alpha_only = "def test_tc1_alpha():\n    assert True\n"
    beta = "def test_tc2_beta():\n    assert True\n"
    scenario = {
        "files": ALL_FILES,
        "rulings": {"3": [{
            "status": "ruled", "outcome": "test_repair", "principle": "project:test-intent",
            "reasoning": "the shared file selects another task's test; moving that test keeps its intent",
            "testRepair": {"file": SHARED, "test": "test_tc2_beta", "change": "move test_tc2_beta to tests/test_beta.py",
                           "why": "Task 3's unfiltered run selects it before Task 4 exists"}}]},
        "testfix": {SHARED: alpha_only, "tests/test_beta.py": beta},
        "testfixChanged": [{"file": SHARED, "test": "test_tc2_beta"}],
    }
    run = run_launch(tmp_path, root, scenario, {"arbiterAgent": "arbiter"})
    order = [a["label"] for a in run.agents]
    fixers = [i for i, lab in enumerate(order) if lab.startswith("testfix ") and not lab.startswith("testfix review")]
    assert fixers, f"the test-fixer never ran: {order}"
    assert len([lab for lab in order if lab.startswith("arbiter task ")]) == 1, order
    starts = [i for i, lab in enumerate(order) if lab.startswith("task ")]
    assert starts, f"the launch never went on: {order}"
    assert fixers[0] < starts[0], f"a task started before the fix: {order}"
    assert run.result.get("refused") is not True
    assert key(target_lint(run), "verifyScope") == [], "findings remain after the fix landed"
    assert (run.target / "tests" / "test_beta.py").exists(), "the fix did not land on the target"


# =========================================================================== properties (Task 20: P-31 to P-34)
#
# P-31, P-32 and P-33 run the pure seams `lint_scope.scope_findings` and `lint_scope.dedupe_scope` over generated spec
# models. P-34 runs the real `ck lint --json` over the same models rendered to tasks.md and test files.
#
# The oracle (brute force, written from requirements 12.1, 12.2, 12.4 and gate.py, not from lint_scope):
#   - Task N is simulated when it is not Done. Assumed Done at its gate: every Done task, N, and N's transitive
#     dependencies. A test task is due when it verifies at least one task and all of them are assumed Done.
#   - The gate of an impl task N runs N's own Verify commands, plus the Verify of each task in N's "Verified by" that
#     is Done and due. The gate of a test task N runs its own Verify only when N is due.
#   - A command selects, per test file, the tests its filter lets through: no filter selects the file whole, `-k WORD`
#     (spelled `-k x`, `-kx` or `-k=x`) selects the tests whose name holds WORD (case-insensitive), node ids select
#     exactly the named tests.
#   - A shared file is one that two or more test tasks list in **Files:**. A test task's tests in a file are what its own
#     Verify selects there. A finding (N, file, T) exists when N's gate selects a test of a shared file that belongs to a
#     test task T that is not due at N's gate (12.2). Due suites, filters that exclude the tests, dependencies on the
#     tasks the tests are for, and Done tasks give no finding (12.4).
# Generators keep a shared file's test tasks on disjoint tests (each filters to its own `t<N>_` group) so no reading of
# "whose tests are these" is needed beyond the one above.

import random  # noqa: E402
import tempfile  # noqa: E402
from dataclasses import dataclass  # noqa: E402

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

PROPERTIES = {
    "P-31": "test_p31_findings_equal_the_brute_force_oracle",
    "P-32": "test_p32_calls_to_later_commands_never_change_verify_scope",
    "P-33": "test_p33_no_problem_is_reported_twice",
    "P-34": "test_p34_lint_output_is_stable_and_sorted",
}

PROP_SETTINGS = dict(derandomize=True, max_examples=200, database=None, deadline=None,
                     suppress_health_check=list(HealthCheck))

PROP_FILES = ["tests/test_a.py", "tests/test_b.py"]
PROP_PROGRAMS = ["uv run pytest", "python -m pytest", "pytest"]
PROP_FLAGS = [[], ["-q"], ["-x"], ["-n", "auto"], ["-q", "-x"], ["-n", "auto", "-q"]]
PROP_K_STYLES = ["-k x", "-kx", "-k=x"]
PROP_CONFIG = {"tests": {"dirs": ["tests"], "files": ["test_*.py"]}}


def prop_render(cmd: dict) -> str:
    words = cmd["program"].split() + list(cmd["flags"])
    flt = cmd["filter"]
    if flt is not None and flt[0] == "nodes":
        words += [f"{cmd['files'][0]}::{n}" for n in flt[1]]
    else:
        words += list(cmd["files"])
    if flt is not None and flt[0] == "k":
        chunk = {"-k x": ["-k", flt[2]], "-kx": [f"-k{flt[2]}"], "-k=x": [f"-k={flt[2]}"]}[flt[1]]
        words = chunk + words if cmd["kfirst"] else words + chunk
    return " ".join(words)


def prop_selected(cmd: dict, path: str, names: List[str]) -> set:
    """The tests of `path` (whose test names are `names`) that `cmd` selects, from the structured command."""
    if path not in cmd["files"]:
        return set()
    flt = cmd["filter"]
    if flt is None:
        return set(names)
    if flt[0] == "k":
        return {n for n in names if flt[2].lower() in n.lower()}
    return {n for n in names if n in flt[1]}


@dataclass
class PropModel:
    nums: List[str]
    track: dict
    done: dict
    deps: dict
    verifies: dict
    files: dict
    commands: dict   # task -> [cmd dict]
    tests_of: dict   # file -> [test names]
    provides: dict   # task -> [command text]

    def verified_by(self, n: str) -> List[str]:
        return [t for t in self.nums if self.track[t] == "test" and n in self.verifies[t]]

    def owners(self, path: str) -> List[str]:
        return [t for t in self.nums if self.track[t] == "test" and path in self.files[t]]

    def verify_lines(self, n: str) -> List[str]:
        return [prop_render(c) for c in self.commands[n]]


@st.composite
def prop_models(draw, with_provides: bool = False) -> PropModel:
    n = draw(st.integers(2, 7))
    nums = [str(i) for i in range(1, n + 1)]
    track = {k: draw(st.sampled_from(["impl", "test", "test"])) for k in nums}
    if "impl" not in track.values():
        track[nums[-1]] = "impl"
    if "test" not in track.values():
        track[nums[0]] = "test"
    done = {k: draw(st.sampled_from([False, False, False, True])) for k in nums}
    deps = {k: draw(st.lists(st.sampled_from(nums[:i]), unique=True, max_size=2)) if i else []
            for i, k in enumerate(nums)}
    impls = [k for k in nums if track[k] == "impl"]
    tests = [k for k in nums if track[k] == "test"]
    verifies, files = {}, {}
    for k in nums:
        if track[k] == "test":
            verifies[k] = draw(st.lists(st.sampled_from(impls), min_size=1, max_size=2, unique=True))
            files[k] = draw(st.lists(st.sampled_from(PROP_FILES), min_size=1, max_size=2, unique=True))
        else:
            verifies[k], files[k] = [], [f"src/m{k}.py"]
    model = PropModel(nums, track, done, deps, verifies, files, {}, {}, {})
    for path in PROP_FILES:
        model.tests_of[path] = [f"test_t{t}_{j}" for t in model.owners(path) for j in (0, 1)] + ["test_misc_0"]

    def pick_filter(path: str, own: Optional[str]):
        names = model.tests_of[path]
        shared = len(model.owners(path)) >= 2
        if own is not None and shared:
            return draw(st.sampled_from([("k", None, f"t{own}_"), ("nodes", [f"test_t{own}_0", f"test_t{own}_1"])]))
        words = [f"t{m}_" for m in tests] + ["test_", "zzz", "misc"]
        kinds = [None, ("k", None, draw(st.sampled_from(words))),
                 ("nodes", draw(st.lists(st.sampled_from(names + ["test_nope"]), min_size=1, max_size=2, unique=True)))]
        return draw(st.sampled_from(kinds))

    def build(path: str, own: Optional[str]) -> dict:
        flt = pick_filter(path, own)
        if flt is not None and flt[0] == "k":
            flt = ("k", draw(st.sampled_from(PROP_K_STYLES)), flt[2])
        files_in = [path]
        if flt is None and own is None and draw(st.booleans()):
            files_in = draw(st.lists(st.sampled_from(PROP_FILES), min_size=1, max_size=2, unique=True))
        return {"program": draw(st.sampled_from(PROP_PROGRAMS)), "flags": draw(st.sampled_from(PROP_FLAGS)),
                "files": files_in, "filter": flt, "kfirst": draw(st.booleans())}

    for k in nums:
        if track[k] == "test":
            model.commands[k] = [build(path, k) for path in files[k]]
        else:
            model.commands[k] = [build(draw(st.sampled_from(PROP_FILES)), None)
                                 for _ in range(draw(st.integers(1, 2)))]
    for k in nums:
        later = draw(st.sampled_from(["x", "y", "z"])) if with_provides and track[k] == "impl" else None
        model.provides[k] = [f"ck {later}"] if later else []
    return model


def prop_oracle(model: PropModel) -> set:
    """{(task, suite, testTask)} by brute force over the model."""
    def closure(n: str) -> set:
        seen, frontier = set(), list(model.deps[n])
        while frontier:
            d = frontier.pop()
            if d not in seen:
                seen.add(d)
                frontier += model.deps[d]
        return seen

    def due(t: str, assumed: set) -> bool:
        return bool(model.verifies[t]) and all(v in assumed for v in model.verifies[t])

    found = set()
    for n in model.nums:
        if model.done[n]:
            continue
        assumed = {k for k in model.nums if model.done[k]} | {n} | closure(n)
        ran: List[dict] = []
        if model.track[n] == "test":
            if due(n, assumed):
                ran += model.commands[n]
        else:
            ran += model.commands[n]
            for t in model.verified_by(n):
                if model.done[t] and due(t, assumed):
                    ran += model.commands[t]
        for path in PROP_FILES:
            owners = model.owners(path)
            if len(owners) < 2:
                continue
            names = model.tests_of[path]
            chosen = set().union(*[prop_selected(c, path, names) for c in ran]) if ran else set()
            for t in owners:
                if due(t, assumed):
                    continue
                mine = set().union(*[prop_selected(c, path, names) for c in model.commands[t]])
                if chosen & mine:
                    found.add((n, path, t))
    return found


def prop_test_source(names: List[str], calls: Optional[dict] = None) -> str:
    out = []
    for name in names:
        body = (calls or {}).get(name, "")
        out.append(f"def {name}():\n    assert True\n" + (f"    {body}\n" if body else ""))
    return "\n\n".join(out)


def prop_sim(model: PropModel, calls: Optional[dict] = None):
    """A SimInput for the model; `calls` maps a test name to a line of code added to every file's test of that name."""
    from claude_kiro.hooks._shared.spec_parser import SpecTask
    from claude_kiro.lint import _verify_runs
    from claude_kiro.lint_scope import SimInput, SimTest

    tasks = {}
    for k in model.nums:
        tasks[k] = SpecTask(
            num=k, title=f"Task {k}", status="Done" if model.done[k] else "Not Started", track=model.track[k],
            files=list(model.files[k]), verify=model.verify_lines(k), verified_by=model.verified_by(k),
            verifies=list(model.verifies[k]), dependencies=list(model.deps[k]), provides=list(model.provides[k]))
    runs = {k: _verify_runs(tasks[k].verify, PROP_CONFIG) for k in model.nums}
    tests = {p: [SimTest(name=n, cls="", source=f"def {n}():\n    assert True\n" + (f"    {calls[n]}\n" if calls and n in calls else ""))
                 for n in names] for p, names in model.tests_of.items()}
    return SimInput(tasks=tasks, runs=runs, unresolved={k: [] for k in model.nums}, tests=tests,
                    module_source={p: "" for p in model.tests_of}, programs=("ck",))


def triples(result) -> set:
    return {(f.task, f.suite, f.testTask) for f in result.findings}


@settings(**PROP_SETTINGS)
@given(prop_models())
def test_p31_findings_equal_the_brute_force_oracle(model):
    from claude_kiro.lint_scope import scope_findings

    result = scope_findings(prop_sim(model))
    got = [(f.task, f.suite, f.testTask) for f in result.findings]
    want = prop_oracle(model)
    assert set(got) == want, (
        f"missing {sorted(want - set(got))}, spurious {sorted(set(got) - want)}\n"
        + "\n".join(f"Task {k} {model.track[k]} done={model.done[k]} deps={model.deps[k]} verifies={model.verifies[k]} "
                    f"files={model.files[k]} verify={model.verify_lines(k)}" for k in model.nums))
    assert len(got) == len(set(got)), f"a finding is repeated: {got}"
    assert all(f.kind == "unfiltered-shared-suite" for f in result.findings)
    assert [(int(f.task), f.kind, f.suite) for f in result.findings] == sorted(
        (int(f.task), f.kind, f.suite) for f in result.findings), "findings are not sorted by (task, kind, suite)"
    assert list(result.not_checked) == [], f"nothing is unresolved, yet: {result.not_checked}"


PROP_CALLS = ['subprocess.run(["ck", "x"])', 'subprocess.run(["ck", "x", "--y"])', 'run("ck x")', 'run("ck x --y=1")',
              'runner.invoke(cli, ["x"])', 'runner.invoke(cli, ["x", "--y"])', 'runner.invoke(cli, ["x", "--y=1"])',
              'subprocess.run(["ck", "y", "--y=1"], check=True)']


@settings(**PROP_SETTINGS)
@given(prop_models(with_provides=True), st.data())
def test_p32_calls_to_later_commands_never_change_verify_scope(model, data):
    from claude_kiro.lint_scope import scope_findings

    names = sorted({n for ns in model.tests_of.values() for n in ns})
    chosen = data.draw(st.lists(st.sampled_from(names), unique=True, max_size=len(names)))
    calls = {n: data.draw(st.sampled_from(PROP_CALLS)) for n in chosen}
    before = scope_findings(prop_sim(model))
    after = scope_findings(prop_sim(model, calls))
    assert after.findings == before.findings, f"calls {calls} changed verifyScope"
    assert triples(after) == prop_oracle(model)
    assert all(f.kind == "unfiltered-shared-suite" for f in after.findings), "a command-kind finding appeared"


@settings(**PROP_SETTINGS)
@given(prop_models(), st.data())
def test_p33_no_problem_is_reported_twice(model, data):
    from claude_kiro.lint_scope import dedupe_scope, scope_findings

    result = scope_findings(prop_sim(model))
    pairs = sorted({(f.task, f.suite) for f in result.findings})
    taken = {p for p in pairs if data.draw(st.booleans())}
    # An existing rule's entries (the verifyCycles shape) naming the same task and suite, plus unrelated ones.
    entries = [{"task": t, "verifiedBy": "1", "needs": "9", "module": s, "message": f"Task {t} runs {s}"}
               for t, s in sorted(taken)]
    entries += [{"task": "98", "verifiedBy": "1", "needs": "9", "module": "tests/test_other.py", "message": "other"}]
    existing = {"verifyCycles": entries}
    deduped, extra = dedupe_scope(result, existing)
    assert isinstance(extra, list)
    kept = {(f.task, f.suite) for f in deduped.findings}
    assert not (kept & taken), f"reported twice, in verifyCycles and verifyScope: {sorted(kept & taken)}"
    assert deduped.findings == tuple(f for f in result.findings if (f.task, f.suite) not in taken), \
        "dedupe changed or reordered findings that no existing rule reports"
    again, _ = dedupe_scope(deduped, existing)
    assert again.findings == deduped.findings, "dedupe is not idempotent"
    untouched, _ = dedupe_scope(result, {})
    assert untouched.findings == result.findings, "dedupe against nothing changed the findings"


def prop_write_project(root: Path, model: PropModel, order: List[str]) -> None:
    spec = root / "specs" / SPEC
    spec.mkdir(parents=True)
    blocks = {}
    for k in model.nums:
        lines = [f"### Task {k}: Task {k}", f"**Status:** {'Done' if model.done[k] else 'Not Started'}",
                 f"**Track:** {model.track[k]}", "**Files:**"] + [f"- `{f}` - x" for f in model.files[k]]
        if model.verify_lines(k):
            lines.append("**Verify:** " + " ".join(f"`{v}`" for v in model.verify_lines(k)))
        if model.verified_by(k) and model.track[k] == "impl":
            lines.append("**Verified by:** " + ", ".join(f"Task {t}" for t in model.verified_by(k)))
        if model.verifies[k]:
            lines.append("**Verifies:** " + ", ".join(f"Task {t}" for t in model.verifies[k]))
        lines.append("**Dependencies:** " + (", ".join(f"Task {d}" for d in model.deps[k]) or "None"))
        blocks[k] = "\n".join(lines) + "\n\n---\n\n"
    (spec / "tasks.md").write_text("# Implementation Tasks: Sim\n\n## Task Breakdown\n\n" + "".join(blocks[k] for k in order))
    (spec / "requirements.md").write_text("# Feature: Sim\n\n- 1.1 WHEN a THE SYSTEM SHALL b\n")
    (spec / "design.md").write_text("# Design: Sim\n")
    (spec / "test-plan.md").write_text("# Test Plan: Sim\n")
    for path, names in model.tests_of.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(prop_test_source(names))


def prop_lint_stdout(root: Path) -> str:
    code, out, err = ck_text(root, "lint", SPEC, "--json")
    assert out.strip().startswith("{"), f"ck lint --json printed no JSON (exit {code}): {out[:300]} {err[-300:]}"
    return out


@settings(**{**PROP_SETTINGS})
@given(prop_models(), st.randoms(use_true_random=False))
def test_p34_lint_output_is_stable_and_sorted(model, rnd):
    order = list(model.nums)
    shuffled = list(order)
    rnd.shuffle(shuffled)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        prop_write_project(base / "a", model, order)
        prop_write_project(base / "b", model, shuffled)
        first = prop_lint_stdout(base / "a")
        second = prop_lint_stdout(base / "a")
        assert first == second, "two lint runs over one project differ byte for byte"
        other = prop_lint_stdout(base / "b")
    one, two = json.loads(first), json.loads(other)
    scope = one["verifyScope"]
    keys = [(int(f["task"]), f["kind"], f["suite"]) for f in scope]
    assert keys == sorted(keys), f"verifyScope is not sorted: {keys}"
    assert scope == two["verifyScope"], f"verifyScope depends on the order of task blocks in tasks.md: {order} vs {shuffled}"
    assert one["notChecked"] == two["notChecked"]
