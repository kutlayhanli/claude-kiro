"""Red team for Story 2's SHALL NOT clauses (requirement 2.3): a task whose worktree creation or claim reports BUSY is
recorded not merged with reason "worktree busy: <detail>", is never retried or escalated, never reviewed, landed, fixed
or resolved, its worktree is never touched again, and it never triggers `onFailure: halt`.

Workflow cases run the real script under the fake runtime: through `bridge.run` where the fake project's `worktree`
option can express the case, and otherwise through `run_driver`, a one-shot Node driver over the same
`load_workflow.mjs` / `fake_project.mjs` that only replaces the first task agent's answer (shapes a real agent could
return, which the fake cannot: BUSY next to `status: 'done'`, BUSY only in `blocker` text). CLI cases run the commands
the real script's task prompt names through the real `ck` CLI on real git, and check the BUSY worktree is untouched.
"""

import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

JS = Path(__file__).parent / "js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

ROOT = "/repo"
WTDIR = f"{ROOT}/.ck/worktrees"
REVIEWER = {"reviewer": {"model": "opus", "effort": "high", "enabled": True, "rounds": 2}, "implementer": {"escalate": ["opus"]}}

DRIVER = """
import { pathToFileURL } from 'node:url'
const [loadWorkflow, fakeProject, loadDecisions] = await Promise.all([
  import(pathToFileURL(process.argv[2]).href), import(pathToFileURL(process.argv[3]).href), import(pathToFileURL(process.argv[4]).href),
])
let input = ''
for await (const chunk of process.stdin) input += chunk
const req = JSON.parse(input)
const baseArgs = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.ck/worktrees/demo-integration' }
let out
try {
  const p = fakeProject.project(req.project)
  const override = req.override || {}
  const patch = req.planPatch || null
  const seen = new Set()
  const agent = async (prompt, opts) => {
    const r = await p.agent(prompt, opts)
    const m = /^task (\\d+)( \\[test\\])?$/.exec(opts.label)
    if (m && override[m[1]] && !seen.has(m[1])) {
      seen.add(m[1])
      return { task: m[1], startedAt: r && r.startedAt, finishedAt: r && r.finishedAt, ...override[m[1]] }
    }
    if (patch && /^plan \\d+/.test(opts.label) && r && !r.error) {
      const done = [...new Set([...(r.done || []), ...(patch.done || [])])]
      const deps = { ...(r.deps || {}), ...(patch.deps || {}) }
      return { ...r, done, deps }
    }
    return r
  }
  const slots = req.slots ?? undefined
  const args = { ...baseArgs, ...req.args, ...(slots ? { agentSlots: slots } : {}) }
  const { result, logs, trace } = await loadWorkflow.runWorkflow(loadDecisions.SCRIPT, { agent, args, slots })
  out = { ok: true, result, logs, trace, prompts: p.state.prompts, events: p.state.events }
} catch (e) {
  out = { ok: false, error: String((e && e.message) || e) }
}
process.stdout.write(JSON.stringify(out) + '\\n')
"""


@functools.lru_cache(maxsize=1)
def _driver_path() -> Path:
    d = Path(tempfile.mkdtemp(prefix="ck-redteam-driver-"))
    path = d / "driver.mjs"
    path.write_text(DRIVER)
    return path


def run_driver(project: Dict[str, Any], args: Optional[Dict[str, Any]] = None, slots: Optional[int] = None, override=None, plan_patch=None) -> Dict[str, Any]:
    """Run the real script once under the fake runtime, replacing the first task agent's answer per `override`
    ({n: result fields}) and merging `plan_patch` ({deps, done}) into every plan answer."""
    req = {"project": project, "args": args or {}, "slots": slots, "override": override or {}, "planPatch": plan_patch}
    proc = subprocess.run(
        ["node", str(_driver_path()), str(JS / "load_workflow.mjs"), str(JS / "fake_project.mjs"), str(JS / "load_decisions.mjs")],
        input=json.dumps(req), capture_output=True, text=True, timeout=120,
    )
    assert proc.stdout.strip(), proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out.pop("ok"), out.get("error")
    return out


# --- oracle ------------------------------------------------------------------------------------


def labels_naming(trace, n):
    """Requested agent labels that act on task n (task, retry, revise, review, land, resolve, fix for it)."""
    out = []
    for e in trace:
        if e["ev"] != "request":
            continue
        label = e["label"]
        if (
            re.match(rf"^task {n}\b", label)
            or re.match(rf"^review {n}\b", label)
            or re.match(rf"^resolve {n}\b", label)
            or (label.startswith("land ") and n in label.split()[1:])
            or re.search(rf"\bTask {n}\b", label)
        ):
            out.append(label)
    return out


def task_entry(result, n):
    return next((t for t in result.get("tasks", []) if str(t.get("task")) == n), None)


def assert_busy_left_alone(out, n, detail=None, exact=True):
    result, trace, prompts = out["result"], out["trace"], out["prompts"]
    assert n not in result.get("merged", []), result.get("merged")
    entry = task_entry(result, n)
    assert entry is not None and entry["ok"] is False, result.get("tasks")
    reason = entry["reason"] or ""
    if detail is None:
        assert re.match(r"^worktree busy: ", reason), reason
    elif exact:
        assert reason == f"worktree busy: {detail}", reason
    else:
        assert reason.startswith(f"worktree busy: {detail[:200]}"), reason[:300]
    failed = {str(f["task"]): f["reason"] for f in result.get("failed", [])}
    assert n in failed and failed[n].startswith("worktree busy: "), result.get("failed")
    named = labels_naming(trace, n)
    assert named == [f"task {n}"], named
    path = f"{WTDIR}/demo-task-{n}"
    later = [label for label, prompt in prompts.items() if label != f"task {n}" and path in prompt]
    assert not later, later


def assert_merged(out, *tasks):
    assert set(tasks) <= set(out["result"].get("merged", [])), (out["result"].get("merged"), out["result"].get("reason"))


def args_for(deps, **extra):
    return {"deps": {k: list(v) for k, v in deps.items()}, "remaining": list(deps), "agents": REVIEWER, **extra}


DEPS3 = {"1": [], "2": [], "3": []}

DETAILS = [
    pytest.param("", id="empty"),
    pytest.param("claimed: ck task agent on hostA", id="plain"),
    pytest.param("claimed: ck task agent on h\u00f4st-\u2713 \U0001F680 \u0645\u0631\u062d\u0628\u0627", id="unicode"),
    pytest.param("claimed: ck task agent on hostA\nsecond line\n\tthird line", id="multi-line"),
    pytest.param("1 uncommitted change(s); commit or clean, or pass --reuse-dirty", id="uncommitted"),
]


# --- workflow cases: fake project's worktree option ---------------------------------------------


@needs_node
@pytest.mark.parametrize("on_failure", ["continue", "halt"])
@pytest.mark.parametrize("detail", DETAILS)
def test_busy_with_odd_details_is_recorded_and_left_alone(bridge, detail, on_failure):
    project = {"deps": DEPS3, "durations": {"1": 5, "3": 5}, "worktree": {"2": {"state": "BUSY", "detail": detail}}}
    out = bridge.run(project, args_for(DEPS3, onFailure=on_failure, maxConcurrent=1))
    assert_busy_left_alone(out, "2", detail)
    assert_merged(out, "1", "3")


@needs_node
@pytest.mark.parametrize("on_failure", ["continue", "halt"])
def test_busy_with_a_very_long_detail(bridge, on_failure):
    detail = "claimed: " + "x" * 6000
    project = {"deps": DEPS3, "worktree": {"2": {"state": "BUSY", "detail": detail}}}
    out = bridge.run(project, args_for(DEPS3, onFailure=on_failure))
    assert_busy_left_alone(out, "2", detail, exact=False)
    assert_merged(out, "1", "3")


@needs_node
@pytest.mark.parametrize("state", ["busy", "Busy"])
def test_busy_state_in_another_case_is_still_busy(bridge, state):
    """The fake answers a non-upper-case BUSY like a normal attempt: status done, gate passed, commits. Still BUSY."""
    project = {"deps": DEPS3, "worktree": {"2": {"state": state, "detail": "claimed: other"}}}
    out = bridge.run(project, args_for(DEPS3))
    assert_busy_left_alone(out, "2", "claimed: other")
    assert_merged(out, "1", "3")


@needs_node
@pytest.mark.parametrize("on_failure", ["continue", "halt"])
def test_busy_first_task_under_halt_does_not_stop_later_starts(bridge, on_failure):
    project = {"deps": DEPS3, "worktree": {"1": {"state": "BUSY", "detail": "claimed: x"}}}
    out = bridge.run(project, args_for(DEPS3, onFailure=on_failure, maxConcurrent=1))
    assert_busy_left_alone(out, "1", "claimed: x")
    assert_merged(out, "2", "3")
    trace = out["trace"]
    end1 = next(i for i, e in enumerate(trace) if e["ev"] == "end" and e["label"] == "task 1")
    assert any(e["ev"] == "request" and e["label"] in ("task 2", "task 3") for e in trace[end1:])
    assert out["result"]["halted"] is False, out["result"].get("reason")


@needs_node
@pytest.mark.parametrize("on_failure", ["continue", "halt"])
def test_busy_on_the_last_remaining_task(bridge, on_failure):
    deps = {"1": [], "2": ["1"]}
    project = {"deps": deps, "worktree": {"2": {"state": "BUSY", "detail": "claimed: late"}}}
    out = bridge.run(project, args_for(deps, onFailure=on_failure))
    assert_busy_left_alone(out, "2", "claimed: late")
    assert_merged(out, "1")
    assert out["result"]["halted"] is False, out["result"].get("reason")


@needs_node
@pytest.mark.parametrize("on_failure", ["continue", "halt"])
def test_busy_task_with_dependents_never_starts_them(bridge, on_failure):
    deps = {"1": [], "2": ["1"], "3": [], "4": ["2"]}
    project = {"deps": deps, "worktree": {"1": {"state": "BUSY", "detail": "claimed: y"}}}
    out = bridge.run(project, args_for(deps, onFailure=on_failure))
    assert_busy_left_alone(out, "1", "claimed: y")
    requested = [e["label"] for e in out["trace"] if e["ev"] == "request"]
    assert "task 2" not in requested and "task 4" not in requested, requested
    assert_merged(out, "3")


@needs_node
def test_busy_never_reaches_a_review_even_with_reviews_and_safety_risk(bridge):
    project = {"deps": DEPS3, "worktree": {"2": {"state": "BUSY", "detail": "claimed: z"}}}
    out = bridge.run(project, args_for(DEPS3, risk={"2": "safety"}, complexity={"2": "high"}))
    assert_busy_left_alone(out, "2", "claimed: z")
    assert not [e for e in out["trace"] if e["label"].startswith("review 2")]


# --- workflow cases: answers the fake cannot produce --------------------------------------------


@needs_node
@pytest.mark.parametrize("on_failure", ["continue", "halt"])
def test_busy_reported_next_to_done_and_a_passed_gate(on_failure):
    override = {"2": {"status": "done", "gatePassed": True, "commits": "abc123", "summary": "done", "worktree": {"state": "BUSY", "detail": "claimed: other agent"}}}
    out = run_driver({"deps": DEPS3}, args_for(DEPS3, onFailure=on_failure), override=override)
    assert_busy_left_alone(out, "2", "claimed: other agent")
    assert_merged(out, "1", "3")


BLOCKERS = [
    "Worktree Busy: claimed: ck task agent on hostB",
    "WORKTREE BUSY: 2 uncommitted change(s)",
    "worktree busy:",
    "stopped: worktree busy: claimed by agent-c (claim race after EXISTS)",
]


@needs_node
@pytest.mark.parametrize("blocker", BLOCKERS)
@pytest.mark.parametrize("on_failure", ["continue", "halt"])
def test_busy_only_in_the_blocker_text(blocker, on_failure):
    override = {"2": {"status": "blocked", "gatePassed": False, "commits": "", "summary": "stopped", "blocker": blocker}}
    out = run_driver({"deps": DEPS3}, args_for(DEPS3, onFailure=on_failure, maxConcurrent=1), override=override)
    assert_busy_left_alone(out, "2")
    assert_merged(out, "1", "3")


@needs_node
def test_busy_blocker_with_done_status_and_passed_gate():
    override = {"2": {"status": "done", "gatePassed": True, "commits": "abc", "summary": "?", "blocker": "worktree busy: claimed: hostC"}}
    out = run_driver({"deps": DEPS3}, args_for(DEPS3), override=override)
    assert_busy_left_alone(out, "2")
    assert_merged(out, "1", "3")


@needs_node
def test_busy_on_a_test_track_task():
    override = {"2": {"status": "blocked", "gatePassed": False, "commits": "", "summary": "s", "worktree": {"state": "BUSY", "detail": "claimed: t"}}}
    out = run_driver({"deps": DEPS3}, args_for(DEPS3, tracks={"2": "test"}), override=override)
    result = out["result"]
    assert "2" not in result["merged"]
    named = [label for label in (e["label"] for e in out["trace"] if e["ev"] == "request") if re.match(r"^(task|review|resolve) 2\b", label) or (label.startswith("land ") and "2" in label.split()[1:])]
    assert named == ["task 2 [test]"], named
    assert task_entry(result, "2")["reason"] == "worktree busy: claimed: t"


# --- CLI cases: the prompt's own commands on a real repo ----------------------------------------

CMD = re.compile(r"ck -C \S+ worktree (?:create|claim)\b[^\n`\"']*")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text(".ck/worktrees/\n")
    (root / "shared.txt").write_text("base\n")
    (root / "specs/demo").mkdir(parents=True)
    (root / "specs/demo/tasks.md").write_text("### Task 1: a\n**Status:** Not Started\n\n**Dependencies:** None\n")
    # A recognisable install: if it ever runs in a BUSY worktree, the marker shows up in the snapshot.
    (root / "specs/ck.json").write_text(json.dumps({"install": "echo ran > INSTALL_RAN"}))
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "branch", "integrate/demo")
    return root


def ck_sub(*argv: str):
    return subprocess.run([sys.executable, "-m", "claude_kiro.cli", *argv], capture_output=True, text=True, timeout=120, env=dict(os.environ))


def prompt_commands(bridge, root: Path):
    out = bridge.run({"deps": {"1": []}}, {"root": str(root), "worktreeDir": str(root / ".ck/worktrees"), "into": "integrate/demo", "targetDir": str(root)})
    prompt = out["prompts"]["task 1"]
    cmds = [m.group(0).rstrip(" .,;:)") for m in CMD.finditer(prompt)]
    creates = [c for c in cmds if " worktree create " in c]
    claims = [c for c in cmds if " worktree claim " in c]
    assert creates, "the task 1 prompt names no `ck -C … worktree create` command:\n" + prompt
    assert claims, "the task 1 prompt names no `ck -C … worktree claim` command:\n" + prompt
    assert cmds.index(creates[0]) < cmds.index(claims[0]), cmds
    return cmds


def run_until_busy(cmds):
    """Run the prompt's commands in order, stopping at the first BUSY (as the prompt tells the agent)."""
    ran = []
    for cmd in cmds:
        argv = shlex.split(cmd)
        assert argv[0] == "ck", cmd
        proc = ck_sub(*argv[1:])
        ran.append((cmd, proc))
        busy = proc.returncode == 3 or "BUSY" in proc.stdout
        if " worktree create " in cmd and "--json" in argv:
            entries = json.loads(proc.stdout)
            busy = busy or any(e.get("state") == "BUSY" for e in entries)
        if busy:
            break
    return ran


def lock_file(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--path-format=absolute", "--git-dir").strip()) / "locked"


def snapshot(path: Path):
    files = {}
    for p in sorted(path.rglob("*")):
        if p.is_file():
            files[str(p.relative_to(path))] = hashlib.sha256(p.read_bytes()).hexdigest()
    lock = lock_file(path)
    return {
        "files": files,
        "lock": lock.read_bytes() if lock.exists() else None,
        "head": git(path, "rev-parse", "HEAD"),
        "branch": git(path, "branch", "--show-current"),
        "status": git(path, "status", "--porcelain", "--untracked-files=all"),
    }


def make_worktree(root: Path) -> Path:
    made = ck_sub("-C", str(root), "worktree", "create", "demo", "1", "--base", "integrate/demo", "--json")
    assert made.returncode == 0, made.stdout + made.stderr
    wt = root / ".ck/worktrees/demo-task-1"
    (wt / "INSTALL_RAN").unlink(missing_ok=True)
    return wt


DIRTY = {
    "untracked": lambda wt: (wt / "wip.txt").write_text("uncommitted work\n"),
    "modified": lambda wt: (wt / "shared.txt").write_text("base\nlocal edit\n"),
    "unicode-name": lambda wt: (wt / "n\u00f6tes \u2713.txt").write_bytes("\u00fcber\n".encode()),
    "staged": lambda wt: ((wt / "staged.txt").write_text("staged\n"), git(wt, "add", "staged.txt")),
    "nested-untracked": lambda wt: ((wt / "deep/er").mkdir(parents=True), (wt / "deep/er/x.bin").write_bytes(bytes(range(256)))),
}


@needs_node
@pytest.mark.parametrize("kind", list(DIRTY))
def test_cli_worktree_with_uncommitted_work_is_byte_identical_after_the_prompt_commands(bridge, repo, kind):
    wt = make_worktree(repo)
    DIRTY[kind](wt)
    before = snapshot(wt)
    cmds = prompt_commands(bridge, repo)
    ran = run_until_busy(cmds)
    create_cmd, create = ran[0]
    assert " worktree create " in create_cmd
    entries = json.loads(create.stdout)
    entry = next(e for e in entries if str(e.get("task")) == "1")
    assert entry["state"] == "BUSY" and "uncommitted" in entry["detail"], entries
    assert create.returncode == 3
    assert len(ran) == 1, [c for c, _ in ran]  # stopped before the claim
    assert snapshot(wt) == before
    assert not lock_file(wt).exists()  # still unclaimed


@needs_node
@pytest.mark.parametrize("dirty", [False, True], ids=["clean", "dirty"])
def test_cli_worktree_claimed_by_a_live_agent_is_byte_identical_after_the_prompt_commands(bridge, repo, dirty):
    wt = make_worktree(repo)
    if dirty:
        (wt / "wip.txt").write_text("their work in progress\n")
    held = ck_sub("-C", str(wt), "worktree", "claim", "demo", "1", "--owner", "ck task agent on hostA")
    assert held.returncode == 0, held.stdout + held.stderr
    before = snapshot(wt)
    cmds = prompt_commands(bridge, repo)
    ran = run_until_busy(cmds)
    create_cmd, create = ran[0]
    assert " worktree create " in create_cmd
    entry = next(e for e in json.loads(create.stdout) if str(e.get("task")) == "1")
    assert entry["state"] == "BUSY" and entry["detail"].startswith("claimed:") and "hostA" in entry["detail"], entry
    assert create.returncode == 3
    assert len(ran) == 1, [c for c, _ in ran]
    assert snapshot(wt) == before
    # The race after EXISTS: the claim command alone must refuse and leave the other owner's claim in place.
    claim = next(c for c in cmds if " worktree claim " in c)
    raced = ck_sub(*shlex.split(claim)[1:])
    assert raced.returncode == 3 and "BUSY 1" in raced.stdout, raced.stdout + raced.stderr
    assert snapshot(wt) == before
