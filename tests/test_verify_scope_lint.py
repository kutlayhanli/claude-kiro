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
from typing import Any, List

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
