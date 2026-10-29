"""TC-15, TC-16, TC-19: the worktree commands the real spec-implement script puts in a `task N` prompt,
run through the real `ck` CLI on a real git repo.

The prompt comes from the real `plugin/workflows/spec-implement.js` (through the Node bridge, with `root`,
`worktreeDir` and `into` pointing at the repo). Its `ck ... worktree create` and `ck ... worktree claim` lines are
cut out of the prompt text, split with `shlex`, and run as `python -m claude_kiro.cli ...` subprocesses, in prompt
order, stopping at the first BUSY or ERROR create entry, as the prompt tells the agent to.
"""

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from conftest import git
from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

SPEC = "demo"
INTO = "integrate/demo"
# The install step: a recognisable command that writes a marker under the ignored .venv/ (so a rerun of the
# create still sees a clean worktree).
INSTALL = f"{shlex.quote(sys.executable)} -c \"import pathlib; p = pathlib.Path('.venv'); p.mkdir(exist_ok=True); (p / 'installed-by-ck').write_text('ok')\""

CREATE_RE = re.compile(r"ck -C \S+ worktree create " + SPEC + r" 1(?: \S+)*? --json")
CLAIM_RE = re.compile(r"ck -C \S+ worktree claim " + SPEC + r" 1(?![\w-])")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Shape of tests/test_worktree_cli.py::repo, plus an `install` step in specs/ck.json and an
    `integrate/demo` branch one commit ahead of main (so "branched from integrate/demo" is checkable)."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    (root / ".gitignore").write_text(".venv/\n.ck/worktrees/\n")
    (root / "shared.txt").write_text("base\n")
    (root / "specs/demo").mkdir(parents=True)
    (root / "specs/demo/tasks.md").write_text("### Task 1: a\n**Status:** Not Started\n")
    (root / "specs/ck.json").write_text(json.dumps({"install": INSTALL}))
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "branch", INTO)
    git(root, "worktree", "add", "-q", str(tmp_path / "integration"), INTO)
    (tmp_path / "integration" / "integration-only.txt").write_text("on integrate/demo\n")
    git(tmp_path / "integration", "add", "-A")
    git(tmp_path / "integration", "commit", "-qm", "integration work")
    return root


def wt(root: Path) -> Path:
    return root / ".ck/worktrees" / f"{SPEC}-task-1"


def task_prompt(bridge, root: Path) -> str:  # noqa: F811
    """The first `task 1` prompt the real script produces for this repo."""
    args = {"spec": SPEC, "root": str(root), "worktreeDir": str(root / ".ck/worktrees"), "into": INTO,
            "targetDir": str(root.parent / "integration")}
    out = bridge.run({"deps": {"1": []}}, args)
    prompts = out["prompts"]
    assert "task 1" in prompts, f"no `task 1` agent was requested: {sorted(prompts)}"
    return prompts["task 1"]


def prompt_commands(prompt: str, root: Path) -> Tuple[str, str]:
    """The create and claim command lines of the prompt, in prompt order (create first)."""
    create = CREATE_RE.search(prompt)
    assert create, f"the task prompt has no `ck -C <root> worktree create {SPEC} 1 ... --json` line:\n{prompt}"
    claim = CLAIM_RE.search(prompt, create.end())
    assert claim, f"the task prompt has no `worktree claim {SPEC} 1` after the create line:\n{prompt}"
    create_argv, claim_argv = shlex.split(create.group(0)), shlex.split(claim.group(0))
    # The create runs from the main checkout and installs onto the run's base; the claim targets the task worktree.
    assert create_argv[:3] == ["ck", "-C", str(root)], create.group(0)
    assert "--install" in create_argv and create_argv[create_argv.index("--base") + 1] == INTO, create.group(0)
    assert claim_argv[:3] == ["ck", "-C", str(wt(root))], claim.group(0)
    return create.group(0), claim.group(0)


def run_ck(command: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run one prompt command line through the real `ck` CLI (`python -m claude_kiro.cli`)."""
    argv = shlex.split(command)
    assert argv[0] == "ck", command
    return subprocess.run([sys.executable, "-m", "claude_kiro.cli", *argv[1:]], cwd=cwd, capture_output=True, text=True, timeout=120)


def create_entry(proc: subprocess.CompletedProcess) -> Dict[str, Any]:
    """The task-1 entry of the create's JSON output (the command carries --json)."""
    try:
        entries = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(f"create printed no JSON ({e}): stdout={proc.stdout!r} stderr={proc.stderr!r}")
    mine = [e for e in entries if str(e.get("task")) == "1"]
    assert len(mine) == 1, entries
    return mine[0]


def run_steps(create: str, claim: str, cwd: Path) -> List[Tuple[str, subprocess.CompletedProcess, Optional[Dict[str, Any]]]]:
    """Run the commands in prompt order; stop after a BUSY or ERROR create entry, as the prompt says."""
    done = []
    proc = run_ck(create, cwd)
    entry = create_entry(proc)
    done.append(("create", proc, entry))
    if entry["state"] in ("BUSY", "ERROR"):
        return done
    done.append(("claim", run_ck(claim, cwd), None))
    return done


def locked(root: Path, path: Path) -> bool:
    """Whether git has `path` locked (a ck claim)."""
    blocks = git(root, "worktree", "list", "--porcelain").split("\n\n")
    for block in blocks:
        lines = block.strip().splitlines()
        if lines and Path(lines[0].split(" ", 1)[1]).resolve() == path.resolve():
            return any(line.startswith("locked") for line in lines)
    raise AssertionError(f"{path} is not a worktree of {root}")


def lock_reason(root: Path, path: Path) -> str:
    for block in git(root, "worktree", "list", "--porcelain").split("\n\n"):
        lines = block.strip().splitlines()
        if lines and Path(lines[0].split(" ", 1)[1]).resolve() == path.resolve():
            return "\n".join(line for line in lines if line.startswith("locked"))
    return ""


# --- TC-15 / TC-16 ---------------------------------------------------------------------------

def test_tc15_prompt_commands_create_install_and_claim_the_task_worktree(bridge, repo, tmp_path):  # noqa: F811
    """TC-15 (2.1): the commands taken from the real `task 1` prompt create the worktree on feat/demo-task-1
    from integrate/demo, run the install step, and claim it."""
    create, claim = prompt_commands(task_prompt(bridge, repo), repo)
    steps = run_steps(create, claim, tmp_path)
    (_, created, entry) = steps[0]
    assert created.returncode == 0, created.stdout + created.stderr
    assert entry["state"] == "CREATED", entry
    assert [name for name, _, _ in steps] == ["create", "claim"]
    path = wt(repo)
    assert path.is_dir()
    assert git(path, "branch", "--show-current").strip() == "feat/demo-task-1"
    assert git(path, "rev-parse", "HEAD").strip() == git(repo, "rev-parse", INTO).strip()  # branched from integrate/demo
    assert (path / "integration-only.txt").exists()
    assert (path / ".venv" / "installed-by-ck").read_text() == "ok"  # the install step ran in the worktree
    claimed = steps[1][1]
    assert claimed.returncode == 0 and "CLAIMED 1" in claimed.stdout, claimed.stdout + claimed.stderr
    assert locked(repo, path)


def test_tc16_rerun_after_release_reuses_the_worktree(bridge, repo, tmp_path):  # noqa: F811
    """TC-16 (2.2): with the claim released, the same commands report EXISTS (exit 0) and claim again."""
    create, claim = prompt_commands(task_prompt(bridge, repo), repo)
    first = run_steps(create, claim, tmp_path)
    assert first[0][2]["state"] == "CREATED" and first[1][1].returncode == 0, first
    released = run_ck(f"ck -C {shlex.quote(str(wt(repo)))} worktree release {SPEC} 1", tmp_path)
    assert released.returncode == 0, released.stdout + released.stderr
    assert not locked(repo, wt(repo))

    again = run_steps(create, claim, tmp_path)
    (_, proc, entry) = again[0]
    assert entry["state"] == "EXISTS", entry
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [name for name, _, _ in again] == ["create", "claim"]
    claimed = again[1][1]
    assert claimed.returncode == 0 and "CLAIMED 1" in claimed.stdout, claimed.stdout + claimed.stderr


# --- TC-19 -----------------------------------------------------------------------------------

def test_tc19a_uncommitted_work_is_busy_and_left_untouched(bridge, repo, tmp_path):  # noqa: F811
    """TC-19 (2.3), uncommitted work: the create's JSON entry is BUSY (uncommitted) and exits 3; the prompt
    says to stop there, so the claim is not run: the worktree stays unlocked and the file unchanged."""
    prompt = task_prompt(bridge, repo)
    create, claim = prompt_commands(prompt, repo)
    # The prompt tells the agent to stop on BUSY instead of going on to the claim.
    assert re.search(r"BUSY[^\n]*\bstop|\bstop[^\n]*BUSY", prompt, re.I), prompt
    first = run_steps(create, claim, tmp_path)
    assert first[0][2]["state"] == "CREATED", first
    assert run_ck(f"ck -C {shlex.quote(str(wt(repo)))} worktree release {SPEC} 1", tmp_path).returncode == 0
    wip = wt(repo) / "wip.txt"
    wip.write_text("half-done work\n")

    steps = run_steps(create, claim, tmp_path)
    (_, proc, entry) = steps[0]
    assert entry["state"] == "BUSY", entry
    assert "uncommitted" in entry["detail"], entry
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert [name for name, _, _ in steps] == ["create"]  # stopped before the claim
    assert not locked(repo, wt(repo))
    assert wip.read_text() == "half-done work\n"


def test_tc19b_live_claim_by_another_owner_is_busy_and_kept(bridge, repo, tmp_path):  # noqa: F811
    """TC-19 (2.3), live claim: the create's JSON entry is BUSY with a `claimed:` detail and exits 3; the
    extracted claim run on its own (the race after EXISTS) prints BUSY 1, exits 3 and keeps the other claim."""
    create, claim = prompt_commands(task_prompt(bridge, repo), repo)
    first = run_steps(create, claim, tmp_path)
    assert first[0][2]["state"] == "CREATED", first
    path = wt(repo)
    assert run_ck(f"ck -C {shlex.quote(str(path))} worktree release {SPEC} 1", tmp_path).returncode == 0
    other = run_ck(f"ck -C {shlex.quote(str(path))} worktree claim {SPEC} 1 --owner other-agent-on-hostB", tmp_path)
    assert other.returncode == 0 and "CLAIMED 1" in other.stdout, other.stdout + other.stderr
    before = lock_reason(repo, path)
    assert "other-agent-on-hostB" in before

    steps = run_steps(create, claim, tmp_path)
    (_, proc, entry) = steps[0]
    assert entry["state"] == "BUSY", entry
    assert entry["detail"].startswith("claimed:") and "other-agent-on-hostB" in entry["detail"], entry
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert [name for name, _, _ in steps] == ["create"]

    race = run_ck(claim, tmp_path)
    assert race.returncode == 3, race.stdout + race.stderr
    assert re.search(r"^BUSY 1\b", race.stdout, re.M), race.stdout + race.stderr
    assert lock_reason(repo, path) == before  # the other owner's claim is still in place
