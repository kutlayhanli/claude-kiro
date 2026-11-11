"""Landing a spec edit through the merge queue (run-principles Story 8, 1.7, 9.1, 9.2).

TC-61, TC-64 to TC-70, TC-138, TC-149, TC-156. Real git, the real `ck worktree land demo arbiter<k> --into BRANCH
--gate --json`, the real lint, the real gate, the real hook. Every refusal compares the target's HEAD before and after.
Project modules are imported inside the test bodies, so this file collects before the feature exists.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from arbiter_helpers import CK, DEFAULT_PLAN, ArbiterProject, arbiter_caller, arbiter_project, ck_run, decisions_entries, seam

PROPERTIES = {
    "P-21": "test_p21_spec_text_changes_lists_exactly_status_and_mark_changes",
    "P-36": "test_p36_a_refused_landing_never_moves_the_integration_branch",
    "P-38": "test_p38_a_reserved_file_never_lands_whatever_the_ruling_says",
}

INTO = "integrate/demo"
PERF = "tests/test_perf.py"
GREET = "tests/test_greet.py"

# --------------------------------------------------------------------------- spec texts


def tasks_text(*, s1: str = "Not Started", v1: str = "python -c pass", v2: str = "python -c pass", d1: str = "None") -> str:
    """The stock two-task spec (Task 1 tests, Task 2 impl) with one acceptance box on Task 1 and tunable fields."""
    return f"""# Implementation Tasks: Demo

**Status:** Not Started

## Task Breakdown

### Task 1: Greeting tests
**Status:** {s1}
**Track:** test
**Requirements:** 1.1
**Description:** Test the greeting.
**Files:**
- `{GREET}` - the test
**Verify:** `{v1}`
**Verifies:** Task 2 (its tests are the oracle)

**Acceptance:**
- [ ] prints it

**Dependencies:** {d1}

---

### Task 2: Greeting
**Status:** Not Started
**Track:** impl
**Requirements:** 1.1
**Description:** Print the greeting.
**Files:**
- `src/greet.py` - the greeting
**Verify:** `{v2}`
**Verified by:** Task 1 ({GREET})
**Dependencies:** Task 1

---
"""


def perf_tasks(x_verify: str = "python -c pass", note: str = "Report it.") -> str:
    """Task 2 (impl) may run Task 1's test file; Task 1 verifies Task 3, which depends on Task 2: a verify-file cycle
    exactly when Task 2's Verify runs that file."""
    return f"""# Implementation Tasks: Demo

**Status:** Not Started

## Task Breakdown

### Task 1: Perf tests
**Status:** Not Started
**Track:** test
**Requirements:** 1.1
**Description:** Test the report.
**Files:**
- `{PERF}` - the test
**Verify:** `python -c pass`
**Verifies:** Task 3 (its tests are the oracle)
**Dependencies:** None

---

### Task 2: Perf core
**Status:** Not Started
**Track:** impl
**Requirements:** 1.1
**Description:** Measure it.
**Files:**
- `src/perf.py` - the core
**Verify:** `{x_verify}`
**Dependencies:** None

---

### Task 3: Report
**Status:** Not Started
**Track:** impl
**Requirements:** 1.1
**Description:** {note}
**Files:**
- `src/report.py` - the report
**Verify:** `python -c pass`
**Verified by:** Task 1 ({PERF})
**Dependencies:** Task 2

---
"""


# --------------------------------------------------------------------------- real git helpers


def git(cwd: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {done.stdout}{done.stderr}")
    return done.stdout


def head(p: ArbiterProject) -> str:
    return git(p.target, "rev-parse", "HEAD").strip()


def commit_target(p: ArbiterProject, rel: str, text: str, msg: str) -> None:
    path = p.target / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(p.target, "add", "-A")
    git(p.target, "commit", "-qm", msg)


def arbiter_wt(p: ArbiterProject, k: int = 1) -> Path:
    """The worktree `demo-task-arbiter<k>` on `feat/demo-task-arbiter<k>`, made from the integration branch."""
    path = p.root / ".ck" / "worktrees" / f"demo-task-arbiter{k}"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        git(p.root, "worktree", "add", "-q", "-b", f"feat/demo-task-arbiter{k}", str(path), p.into)
    return path


def commit_in(wt: Path, edits: Dict[str, str], msg: str = "spec edit") -> str:
    for rel, text in edits.items():
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(text)
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", msg)
    return git(wt, "rev-parse", "HEAD").strip()


def edit_spec(wt: Path, name: str, old: str, new: str, msg: str = "spec edit") -> str:
    path = wt / "specs" / "demo" / name
    text = path.read_text()
    assert old in text, f"{old!r} not in {name}"
    return commit_in(wt, {f"specs/demo/{name}": text.replace(old, new, 1)}, msg)


def land_cmd(k: int = 1) -> list:
    return [*CK, "worktree", "land", "demo", f"arbiter{k}", "--into", INTO, "--gate", "--json"]


def parse_entry(out: str) -> Dict[str, Any]:
    """The one landing entry, whether the command prints it bare or inside {"tasks": [entry]}."""
    try:
        data = json.loads(out)
    except ValueError:
        raise AssertionError("not JSON: " + out)
    if isinstance(data, dict) and "state" in data:
        return data
    tasks = data.get("tasks") if isinstance(data, dict) else data
    assert isinstance(tasks, list) and len(tasks) == 1, "expected one landing entry: " + out
    return tasks[0]


def land(p: ArbiterProject, k: int = 1):
    done = ck_run(p.root, *land_cmd(k)[len(CK):])
    return done, parse_entry(done.stdout)


def assert_untouched(p: ArbiterProject, before: str) -> None:
    assert head(p) == before, "the integration branch moved"
    assert git(p.target, "status", "--porcelain", "--untracked-files=no") == ""
    assert subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=p.target, capture_output=True).returncode != 0
    assert git(p.root, "rev-parse", p.into).strip() == before


def assert_not_landed(p: ArbiterProject, k: int, states: tuple, before: str):
    done, entry = land(p, k)
    assert entry["state"] in states, (entry, done.stdout, done.stderr)
    assert done.returncode != 0
    assert_untouched(p, before)
    assert decisions_entries(p) == [], "a refused landing must not write a ruling"
    return entry


# --------------------------------------------------------------------------- TC-61


def test_tc61_spec_edit_commits_on_a_branch_from_the_integration_branch_and_lands_under_the_lock(tmp_path):
    p = arbiter_project(tmp_path, tasks=tasks_text())
    before = head(p)
    wt = arbiter_wt(p)
    tip = edit_spec(wt, "tasks.md", "**Description:** Print the greeting.", "**Description:** Print the greeting loudly.")
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == "feat/demo-task-arbiter1"
    assert git(p.root, "merge-base", "feat/demo-task-arbiter1", INTO).strip() == before

    done, entry = land(p)
    assert done.returncode == 0, done.stdout + done.stderr
    assert entry["state"] == "MERGED", entry
    assert entry["commit"] == head(p) != before
    assert entry.get("moved", []) == []
    subprocess.run(["git", "merge-base", "--is-ancestor", tip, "HEAD"], cwd=p.target, check=True)
    assert "loudly" in (p.target / "specs/demo/tasks.md").read_text()
    common = Path(git(p.root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    assert (common / "ck-land-integrate-demo.lock").exists(), "the landing took the merge-queue lock"


# --------------------------------------------------------------------------- TC-64


def test_tc64_a_conflicting_edit_is_conflict_and_leaves_the_target_alone(tmp_path):
    p = arbiter_project(tmp_path, tasks=tasks_text())
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "**Description:** Print the greeting.", "**Description:** Print it from the arbiter.")
    path = p.target / "specs/demo/tasks.md"
    commit_target(p, "specs/demo/tasks.md", path.read_text().replace("**Description:** Print the greeting.", "**Description:** Print it from a task."), "other edit")
    before = head(p)

    assert_not_landed(p, 1, ("CONFLICT",), before)


# --------------------------------------------------------------------------- TC-65


def test_tc65_a_red_gate_is_red_and_the_target_is_reset(tmp_path):
    p = arbiter_project(tmp_path, tasks=tasks_text(s1="Done"))
    before = head(p)
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "**Verify:** `python -c pass`\n**Verifies:**", "**Verify:** `false`\n**Verifies:**")

    assert_not_landed(p, 1, ("RED",), before)

    # The reset left the branch landable: a green Verify on the same branch lands.
    edit_spec(wt, "tasks.md", "**Verify:** `false`\n**Verifies:**", "**Verify:** `python -c \"pass\"`\n**Verifies:**")
    done, entry = land(p)
    assert done.returncode == 0 and entry["state"] == "MERGED", (entry, done.stdout, done.stderr)


# --------------------------------------------------------------------------- TC-66


def test_tc66_a_branch_touching_a_refused_path_is_refused(tmp_path):
    p = arbiter_project(tmp_path, tasks=tasks_text())
    before = head(p)
    wt = arbiter_wt(p)
    text = (wt / "specs/demo/tasks.md").read_text().replace("Print the greeting.", "Print the greeting twice.", 1)
    commit_in(wt, {"specs/demo/tasks.md": text, ".claude/settings.json": "{}\n"})

    assert_not_landed(p, 1, ("PROTECTED", "REFUSED"), before)
    assert not (p.target / ".claude/settings.json").exists()


# --------------------------------------------------------------------------- TC-67


def test_tc67_an_edit_creating_a_dependency_cycle_is_not_landed(tmp_path):
    p = arbiter_project(tmp_path, tasks=tasks_text())
    before = head(p)
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "**Dependencies:** None", "**Dependencies:** Task 2")

    assert_not_landed(p, 1, ("REFUSED",), before)


# --------------------------------------------------------------------------- TC-68


def test_tc68_an_edit_creating_a_new_blocking_finding_is_not_landed(tmp_path):
    p = arbiter_project(tmp_path, tasks=perf_tasks())
    assert json.loads(ck_run(p.target, "lint", "demo", "--json").stdout)["verifyCycles"] == [], "baseline must be clean"
    before = head(p)
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "**Verify:** `python -c pass`\n**Dependencies:** None\n\n---\n\n### Task 3", f"**Verify:** `uv run pytest -q {PERF}`\n**Dependencies:** None\n\n---\n\n### Task 3")

    assert_not_landed(p, 1, ("REFUSED",), before)


def test_tc68_an_edit_that_leaves_an_existing_finding_as_it_was_may_land(tmp_path):
    p = arbiter_project(tmp_path, tasks=perf_tasks(x_verify=f"uv run pytest -q {PERF}"))
    found = json.loads(ck_run(p.target, "lint", "demo", "--json").stdout)["verifyCycles"]
    assert found, "baseline carries the finding"
    before = head(p)
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "**Description:** Report it.", "**Description:** Report it plainly.")

    done, entry = land(p)
    assert done.returncode == 0 and entry["state"] == "MERGED", (entry, done.stdout, done.stderr)
    assert head(p) != before
    assert json.loads(ck_run(p.target, "lint", "demo", "--json").stdout)["verifyCycles"] == found


# --------------------------------------------------------------------------- TC-69


def test_tc69_an_edit_changing_a_status_field_is_not_landed(tmp_path):
    p = arbiter_project(tmp_path, tasks=tasks_text())
    before = head(p)
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "### Task 1: Greeting tests\n**Status:** Not Started", "### Task 1: Greeting tests\n**Status:** Done")

    assert_not_landed(p, 1, ("REFUSED",), before)


def test_tc69_an_edit_changing_an_acceptance_mark_is_not_landed(tmp_path):
    p = arbiter_project(tmp_path, tasks=tasks_text())
    before = head(p)
    wt = arbiter_wt(p, 2)
    edit_spec(wt, "tasks.md", "- [ ] prints it", "- [x] prints it")

    assert_not_landed(p, 2, ("REFUSED",), before)


# --------------------------------------------------------------------------- TC-70

GREET_TESTS = "def test_tc3_loud():\n    assert True\n\n\ndef test_tc4_quiet():\n    assert True\n"


def test_tc70_an_edit_moving_a_test_out_of_verify_names_it_in_moved_and_in_the_entry(tmp_path):
    wide = f'uv run pytest -q {GREET} -k "tc3_ or tc4_"'
    narrow = f'uv run pytest -q {GREET} -k "tc4_"'
    p = arbiter_project(tmp_path, tasks=tasks_text(v2=wide))
    commit_target(p, GREET, GREET_TESTS, "add the tests")
    before = head(p)
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", f"**Verify:** `{wide}`", f"**Verify:** `{narrow}`")

    done, entry = land(p)
    assert done.returncode == 0 and entry["state"] == "MERGED", (entry, done.stdout, done.stderr)
    assert head(p) != before
    moved = entry["moved"]
    assert moved and all(m["task"] == "2" for m in moved), moved
    assert any("tc3_" in (m.get("test") or "") for m in moved), moved
    assert not any("tc4_" in (m.get("test") or "") for m in moved), moved

    record = {
        "trigger": "lint", "claim_key": f"2|test:{GREET}::test_tc3_loud", "claim_text": "tc3 belongs to another task",
        "task": "2", "tier": "normal", "outcome": "spec_edit", "reasoning": "narrow the Verify selection",
        "principle_id": "project:test-intent", "principle_text": "a test states what the requirement says",
        "plan_layer": "absent", "changed": "tasks.md: Task 2 Verify narrowed", "moved": moved,
        "commit": entry["commit"], "landed": [True, ""],
    }
    recorded = ck_run(p.root, "arbiter", "record", "demo", "--into", INTO, "--json", stdin=json.dumps(record))
    assert recorded.returncode == 0, recorded.stdout + recorded.stderr
    [written] = decisions_entries(p)
    assert "tc3_" in written["What changed"], written
    assert "tc4_" not in written["What changed"], written


# --------------------------------------------------------------------------- TC-138


def _wait_for(condition, proc: subprocess.Popen, what: str, limit: float = 120.0) -> None:
    """Wait for a state, not for a time: poll the condition until it holds, the process dies, or the limit passes."""
    deadline = time.monotonic() + limit
    while not condition():
        if proc.poll() is not None:
            raise AssertionError(f"process ended before {what}: {proc.stdout.read()}")
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


def test_tc138_an_arbiter_landing_waits_for_the_task_landing_that_holds_the_lock(tmp_path):
    import fcntl

    holding = tmp_path / "holding"
    fifo = tmp_path / "gate-fifo"
    os.mkfifo(fifo)
    hold = f"sh -c 'touch {holding}; read line < {fifo}'"
    p = arbiter_project(tmp_path, tasks=tasks_text(v1=hold))
    made = ck_run(p.root, "worktree", "create", "demo", "1", "--base", INTO)
    assert made.returncode == 0, made.stdout + made.stderr
    task_wt = p.root / ".ck" / "worktrees" / "demo-task-1"
    task_commit = commit_in(task_wt, {"one.txt": "1\n"}, "task 1")
    wt = arbiter_wt(p)
    edit_spec(wt, "tasks.md", "**Description:** Print the greeting.", "**Description:** Print the greeting twice.")
    common = Path(git(p.root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    lock = common / "ck-land-integrate-demo.lock"

    task_proc = subprocess.Popen([*CK, "worktree", "land", "demo", "1", "--into", INTO, "--gate", "--json"], cwd=p.root,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    arb_proc: Optional[subprocess.Popen] = None
    try:
        _wait_for(holding.exists, task_proc, "the task landing's gate to start")
        with open(lock, "a+") as probe:
            with pytest.raises(OSError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)  # land_lock is held by the task landing

        arb_proc = subprocess.Popen(land_cmd(1), cwd=p.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert arb_proc.poll() is None, "the arbiter landing finished while the task landing held the lock: " + (arb_proc.stdout.read() or "")
        assert task_proc.poll() is None
        with open(lock, "a+") as probe:
            with pytest.raises(OSError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with open(fifo, "w") as release:  # opens once the gate's read is waiting
            release.write("go\n")
        task_out, _ = task_proc.communicate(timeout=180)
        arb_out, _ = arb_proc.communicate(timeout=240)
    finally:
        for proc in (task_proc, arb_proc):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.communicate()

    assert task_proc.returncode == 0, task_out
    assert arb_proc.returncode == 0, arb_out
    task_entry = parse_entry(task_out)
    arb_entry = parse_entry(arb_out)
    assert task_entry["state"] == "MERGED" and arb_entry["state"] == "MERGED", (task_out, arb_out)
    assert arb_entry["commit"] == head(p)
    assert arb_entry["commit"] != task_commit
    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", task_commit, arb_entry["commit"]], cwd=p.target)
    assert ancestor.returncode == 0, "the arbiter commit is not after the task commit"
    assert (p.target / "one.txt").exists()
    assert "twice" in (p.target / "specs/demo/tasks.md").read_text()


# --------------------------------------------------------------------------- TC-149

RESERVING_PLAN = DEFAULT_PLAN + "- **Reserves design.md:** interface changes are mine to approve.\n"


def test_tc149_a_reserved_design_change_with_no_reserved_by_is_refused(tmp_path):
    p = arbiter_project(tmp_path, tasks=tasks_text(), plan=RESERVING_PLAN)
    before = head(p)
    wt = arbiter_wt(p)
    edit_spec(wt, "design.md", "A greeting.", "A greeting, with a new interface.")

    assert_not_landed(p, 1, ("REFUSED",), before)

    # The reservation covers design.md only: a tasks.md edit on another branch lands.
    other = arbiter_wt(p, 2)
    edit_spec(other, "tasks.md", "**Description:** Print the greeting.", "**Description:** Print the greeting once.")
    done, entry = land(p, 2)
    assert done.returncode == 0 and entry["state"] == "MERGED", (entry, done.stdout, done.stderr)


# --------------------------------------------------------------------------- TC-156


def test_tc156_open_then_hook_then_commit_then_land(tmp_path):
    from claude_kiro.cli.runner import execute_hook
    from guard_helpers import decision, hook_input, run_bash

    p = arbiter_project(tmp_path, tasks=tasks_text())
    before = head(p)
    claim = {"task": "1", "trigger": "test-conflict", "text": "the test asks for more than the requirement", "file": GREET,
             "test": "test_greet", "criterion": "1.1"}
    opened = ck_run(p.root, "arbiter", "open", "demo", "--task", "1", "--into", INTO, "--json",
                    stdin=json.dumps({"claim": claim, "task": {"risk": "normal", "complexity": "Low"}}))
    assert opened.returncode == 0, opened.stdout + opened.stderr
    result = json.loads(opened.stdout)
    assert result["action"] == "rule", result
    wt = Path(result["worktree"])
    assert wt.is_dir() and wt.name == "demo-task-arbiter" + str(result["number"]).removeprefix("arbiter")
    k = int(wt.name.removeprefix("demo-task-arbiter"))
    assert git(p.root, "merge-base", result["branch"], INTO).strip() == before

    who = arbiter_caller(p, checkout=wt)
    session = uuid.uuid4().hex

    def edit_hook(rel: str, old: str, new: str):
        data = hook_input(wt, session, "Edit", agent_id=who["agent_id"], agent_type=who["agent_type"],
                          file_path=str(wt / rel), old_string=old, new_string=new)
        return execute_hook("pre-tool-guard", data)

    tasks_edit = edit_hook("specs/demo/tasks.md", "Print the greeting.", "Print the greeting twice.")
    assert tasks_edit is None or decision(tasks_edit) == "allow", tasks_edit
    denied = edit_hook("specs/demo/requirements.md", "print a greeting", "print a long greeting")
    assert decision(denied) == "deny", denied

    path = wt / "specs/demo/tasks.md"
    path.write_text(path.read_text().replace("Print the greeting.", "Print the greeting twice.", 1))
    added = run_bash(wt, session, "git add specs/demo/tasks.md", agent_id=who["agent_id"], agent_type=who["agent_type"], cwd=wt)
    assert added.ran and added.returncode == 0, added
    committed = run_bash(wt, session, "git commit -m 'task 1 wording'", agent_id=who["agent_id"], agent_type=who["agent_type"], cwd=wt)
    assert committed.ran and committed.returncode == 0, committed

    done = ck_run(p.root, "worktree", "land", "demo", f"arbiter{k}", "--into", INTO, "--gate", "--json")
    entry = parse_entry(done.stdout)
    assert done.returncode == 0 and entry["state"] == "MERGED", (entry, done.stdout, done.stderr)
    assert "twice" in (p.target / "specs/demo/tasks.md").read_text()
    assert "greeting" in (p.target / "specs/demo/requirements.md").read_text()
    assert head(p) != before


# --------------------------------------------------------------------------- properties (P-21, P-36, P-38)

BASE = dict(derandomize=True, database=None, deadline=None,
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture, HealthCheck.data_too_large])

_STATUSES = ["Not Started", "In Progress", "Done"]
_MARK_FORMS = [("-", "x"), ("-", "X"), ("*", "x"), ("*", "X")]
_TEXTS = st.sampled_from(["python -c pass", "python -c 'print(1)'", "uv run pytest -q tests/x.py", "false"])
_PROSE = st.sampled_from(["Print it.", "Print it, and the Status is Done in prose.", "Status: In Progress is only a word here.",
                          "Mark `- [x]` in a sentence.", "Keep Status unchanged."])


@st.composite
def _task_spec(draw) -> dict:
    return {
        "status": draw(st.sampled_from(_STATUSES)),
        "form": draw(st.sampled_from(["plain", "spaces", "moved"])),
        "marks": [(draw(st.booleans()), draw(st.sampled_from(_MARK_FORMS))) for _ in range(3)],
        "verify": draw(_TEXTS), "deps": draw(st.sampled_from(["None", "Task 1", "Task 2"])), "desc": draw(_PROSE),
    }


def _render_p21(tasks: list) -> str:
    out = ["# Implementation Tasks: Demo\n", "**Status:** Not Started\n", "## Task Breakdown\n"]
    for n, t in enumerate(tasks, 1):
        value = {"plain": f"**Status:** {t['status']}", "spaces": f"**Status:**   {t['status']}  ", "moved": f"**Status:** {t['status']}"}[t["form"]]
        head_lines = [f"### Task {n}: Thing {n}"]
        body = ["**Track:** impl", "**Requirements:** 1.1", f"**Description:** {t['desc']}", f"**Verify:** `{t['verify']}`"]
        if t["form"] == "moved":
            body.insert(1, value)
        else:
            head_lines.append(value)
        marks = [f"{b} [{c if on else ' '}] criterion {i}" if on else f"{b} [ ] criterion {i}" for i, (on, (b, c)) in enumerate(t["marks"])]
        out.append("\n".join(head_lines + body + ["", "**Acceptance:**"] + marks + ["", f"**Dependencies:** {t['deps']}", "", "---", ""]))
    return "\n".join(out)


@st.composite
def _edits(draw):
    before = [draw(_task_spec()) for _ in range(2)]
    after = []
    changed = False
    for t in before:
        a = {k: (list(v) if isinstance(v, list) else v) for k, v in t.items()}
        if draw(st.booleans()):
            a["status"] = draw(st.sampled_from([s for s in _STATUSES if s != t["status"]]))
            a["form"] = draw(st.sampled_from(["plain", "spaces", "moved"]))
            changed = True
        for i in range(3):
            if draw(st.booleans()):
                on, form = t["marks"][i]
                a["marks"][i] = (not on, draw(st.sampled_from(_MARK_FORMS)))
                changed = True
        for key, pool in (("verify", _TEXTS), ("desc", _PROSE), ("deps", st.sampled_from(["None", "Task 1", "Task 2"]))):
            if draw(st.booleans()):
                a[key] = draw(pool)  # negatives: these never count
        after.append(a)
    return _render_p21(before), _render_p21(after), changed


def _count(changes) -> int:
    """How many changes a `SpecTextChanges` lists, whatever container the seam chose (dataclass, mapping, sequence)."""
    import dataclasses

    if changes is None:
        return 0
    if dataclasses.is_dataclass(changes) and not isinstance(changes, type):
        changes = dataclasses.asdict(changes)
    if isinstance(changes, dict):
        # design.md leaves the shape open: only collection-valued fields list changes (a name or summary string does not)
        return sum(_count(v) for v in changes.values() if isinstance(v, (dict, list, tuple, set, frozenset)))
    if isinstance(changes, (list, tuple, set, frozenset)):
        return sum(max(_count(v), 1) if isinstance(v, (dict, list, tuple, set, frozenset)) else 1 for v in changes)
    return 1 if changes else 0


@settings(max_examples=200, **BASE)
@given(_edits())
def test_p21_spec_text_changes_lists_exactly_status_and_mark_changes(case):
    spec_text_changes = seam("arbiter.spec_edit_refusals", "spec_text_changes")
    before, after, changed = case
    assert (_count(spec_text_changes(before, after)) > 0) == changed, (before, after)
    assert _count(spec_text_changes(before, before)) == 0


_LAND_KINDS = ["conflict", "red", "refused-path", "cycle", "blocking"]
_LAND_STATES = {"conflict": ("CONFLICT",), "red": ("RED",), "refused-path": ("PROTECTED", "REFUSED"),
                "cycle": ("REFUSED",), "blocking": ("REFUSED",)}


def _stage_refusal(p: ArbiterProject, kind: str, k: int, word: str) -> None:
    wt = arbiter_wt(p, k)
    if kind == "conflict":
        edit_spec(wt, "tasks.md", "**Description:** Print the greeting.", f"**Description:** from the arbiter {word}.")
        path = p.target / "specs/demo/tasks.md"
        commit_target(p, "specs/demo/tasks.md", path.read_text().replace("**Description:** Print the greeting.", f"**Description:** from a task {word}."), "other edit")
    elif kind == "red":
        edit_spec(wt, "tasks.md", "**Verify:** `python -c pass`\n**Verifies:**", "**Verify:** `false`\n**Verifies:**")
    elif kind == "refused-path":
        text = (wt / "specs/demo/tasks.md").read_text().replace("Print the greeting.", f"Print the greeting {word}.", 1)
        commit_in(wt, {"specs/demo/tasks.md": text, ".claude/settings.json": "{}\n"})
    elif kind == "cycle":
        edit_spec(wt, "tasks.md", "**Dependencies:** None", "**Dependencies:** Task 2")
    else:
        edit_spec(wt, "tasks.md", "**Verify:** `python -c pass`\n**Dependencies:** None\n\n---\n\n### Task 3",
                  f"**Verify:** `uv run pytest -q {PERF}`\n**Dependencies:** None\n\n---\n\n### Task 3")


@settings(max_examples=30, **BASE)
@given(kind=st.sampled_from(_LAND_KINDS), k=st.integers(1, 3), word=st.sampled_from(["once", "twice", "loudly"]))
def test_p36_a_refused_landing_never_moves_the_integration_branch(kind, k, word):
    import tempfile

    tasks = perf_tasks() if kind == "blocking" else tasks_text(s1="Done" if kind == "red" else "Not Started")
    with tempfile.TemporaryDirectory() as tmp:
        p = arbiter_project(Path(tmp), tasks=tasks)
        before = head(p)
        _stage_refusal(p, kind, k, word)
        if kind == "conflict":
            before = head(p)
        done, entry = land(p, k)
        assert done.returncode != 0 and entry["state"] in _LAND_STATES[kind], (kind, entry, done.stdout, done.stderr)
        assert_untouched(p, before)
        assert decisions_entries(p) == [], "a refused landing must not write a second ruling"


_LANE = ["tasks.md", "design.md", "test-plan.md"]
_NEAR = ["code", "test repairs", "requirements.md", "PLAN.md"]


@settings(max_examples=200, **BASE)
@given(changed=st.sets(st.sampled_from(_LANE), min_size=1), reserved=st.sets(st.sampled_from(_LANE + _NEAR)),
       order=st.randoms(use_true_random=False))
def test_p38_a_reserved_file_never_lands_whatever_the_ruling_says(changed, reserved, order):
    SpecEditFacts, decide_spec_edit = seam("arbiter.spec_edit_refusals", "SpecEditFacts", "decide_spec_edit")
    paths = [f"specs/demo/{f}" for f in changed]
    scopes = list(reserved)
    order.shuffle(paths)
    order.shuffle(scopes)
    facts = SpecEditFacts(spec="demo", changed=tuple(paths), status_changes=(), mark_changes=(), new_cycles=(),
                          new_blocking=(), reserved=tuple(scopes))
    decision = decide_spec_edit(facts)
    hit = sorted(changed & reserved)
    assert decision.land == (not hit), (paths, scopes, decision)
    reasons = " ".join(decision.reasons)
    for name in _LANE:
        assert (name in reasons) == (name in hit), (name, hit, decision.reasons)
