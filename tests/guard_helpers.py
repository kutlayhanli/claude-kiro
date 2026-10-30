"""Shared helpers for the guard, park, hand-off and interruption tests (prompt-free-runs).

Test-owned. Real boundaries only: real git, real `bash -c`, real `execute_hook`, real `ck worktree`.
Project modules are imported inside function bodies, so every test file collects before the implementation exists.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from test_guard import decision  # noqa: F401  (re-exported)

CK = [sys.executable, "-m", "claude_kiro.cli"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


# --------------------------------------------------------------------------- hook inputs and drivers


def hook_input(project, session, tool, *, agent_id: Optional[str] = None, agent_type: Optional[str] = None,
               tool_use_id: Optional[str] = None, event: str = "PreToolUse", tool_response: Optional[dict] = None,
               **tool_input: Any) -> dict:
    """A Claude Code-shaped hook input. `agent_id`/`agent_type` appear only when given."""
    data: Dict[str, Any] = {
        "session_id": session,
        "transcript_path": str(Path(str(project)) / ".claude" / "transcript.jsonl"),
        "cwd": str(project),
        "permission_mode": "default",
        "hook_event_name": event,
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_use_id": tool_use_id or "toolu_" + uuid.uuid4().hex[:20],
    }
    if tool_response is not None:
        data["tool_response"] = tool_response
    if agent_id is not None:
        data["agent_id"] = agent_id
    if agent_type is not None:
        data["agent_type"] = agent_type
    return data


@contextlib.contextmanager
def _temp_env(monkeypatch, env: Optional[Dict[str, Optional[str]]]):
    """Set (or, for a None value, unset) variables through `monkeypatch` only; never touch os.environ directly."""
    for key, value in (env or {}).items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    yield


def pre(project, session, tool, *, agent_id: Optional[str] = None, agent_type: Optional[str] = None, env: Optional[dict] = None,
        monkeypatch=None, **tool_input) -> Optional[dict]:
    """Run the PreToolUse guard through the real entry point. `env` needs `monkeypatch` (never os.environ directly)."""
    from claude_kiro.cli.runner import execute_hook

    if env and monkeypatch is None:
        raise TypeError("pre(env=...) needs monkeypatch=")
    data = hook_input(project, session, tool, agent_id=agent_id, agent_type=agent_type, **tool_input)
    if env:
        with _temp_env(monkeypatch, env):
            return execute_hook("pre-tool-guard", data)
    return execute_hook("pre-tool-guard", data)


@dataclass
class BashOutcome:
    pre: Optional[dict]
    ran: bool
    returncode: Optional[int]
    post: Optional[dict]


def run_bash(project, session, command: str, *, agent_id: Optional[str] = None, agent_type: Optional[str] = None,
             cwd=None, via: str = "post-bash-check") -> BashOutcome:
    """PreToolUse hook, then (unless it denied or asked) the real `bash -c`, then the PostToolUse hook."""
    from claude_kiro.cli.runner import execute_hook

    first = hook_input(project, session, "Bash", agent_id=agent_id, agent_type=agent_type, command=command)
    pre_result = execute_hook("pre-tool-guard", first)
    verdict = decision(pre_result) if pre_result and "hookSpecificOutput" in pre_result else None
    if verdict in ("deny", "ask"):
        return BashOutcome(pre_result, False, None, None)
    done = subprocess.run(["bash", "-c", command], cwd=str(cwd or project), capture_output=True, text=True)
    second = hook_input(project, session, "Bash", agent_id=agent_id, agent_type=agent_type, tool_use_id=first["tool_use_id"],
                        event="PostToolUse", tool_response={"stdout": done.stdout, "stderr": done.stderr, "interrupted": False},
                        command=command)
    return BashOutcome(pre_result, True, done.returncode, execute_hook(via, second))


def post_message(result: Optional[dict]) -> Optional[str]:
    """The text a PostToolUse result shows Claude, whichever output form the hook uses."""
    if not result:
        return None
    if result.get("decision") == "block":
        return result.get("reason")
    out = result.get("hookSpecificOutput") or {}
    return out.get("additionalContext")


# --------------------------------------------------------------------------- git state


def commit_on_base(project: Path, rel: str, content: str) -> None:
    """Add a file on `main` and merge it into `feature`, so it exists on the base branch."""
    cur = _git(project, "rev-parse", "--abbrev-ref", "HEAD").strip()
    _git(project, "checkout", "-q", "main")
    target = project / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", f"base: {rel}")
    _git(project, "checkout", "-q", cur)
    if cur != "main":
        _git(project, "merge", "-q", "--no-edit", "main")


def new_on_branch(project: Path, rel: str, content: str) -> None:
    """Commit a file on the current (feature) branch only."""
    target = project / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", f"feature: {rel}")


def ck_json(project: Path, **guard_modes: Any) -> None:
    """Write `specs/ck.json` with the verify command kept and the given guard modes merged in."""
    path = Path(project) / "specs" / "ck.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data.update(guard_modes)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def big_repo(tmp: Path) -> Path:
    """A git repo with 1,000 tracked files: 950 source and 50 test files."""
    root = Path(tmp) / "big"
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "src").mkdir()
    (root / "tests").mkdir()
    for i in range(950):
        (root / "src" / f"mod_{i:04d}.py").write_text(f"def f_{i}():\n    return {i}\n")
    for i in range(50):
        (root / "tests" / f"test_mod_{i:03d}.py").write_text(f"def test_{i}():\n    assert {i} == {i}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "big")
    return root


def worktree_of(project: Path, name: str, under: str = ".claude/worktrees") -> Path:
    """`git worktree add` a linked worktree at `<project>/<under>/<name>` on a new branch."""
    path = Path(project) / under / name
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(project, "worktree", "add", "-q", "-b", f"wt-{name}", str(path))
    return path


def ck_run_cmd(cwd: Path, *args: str, env: Optional[dict] = None, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run([*CK, *args], cwd=str(cwd), env=env if env is not None else os.environ.copy(), input=stdin,
                          capture_output=True, text=True)


def claimed_task_worktree(project: Path, spec: str, n: int | str, *, base: Optional[str] = None) -> Path:
    """The real `ck worktree create <spec> <n>` and `ck worktree claim`; returns the worktree's absolute path.
    The cwd of both commands stays the main checkout, the way task agents use it."""
    args = ["worktree", "create", spec, str(n), "--json"]
    if base:
        args += ["--base", base]
    made = ck_run_cmd(project, *args)
    assert made.returncode == 0, made.stdout + made.stderr
    entry = next(e for e in json.loads(made.stdout) if str(e["task"]) == str(n))
    assert entry["state"] in ("CREATED", "EXISTS"), entry
    path = (Path(project) / entry["path"]).resolve()
    claimed = ck_run_cmd(project, "worktree", "claim", spec, str(n))
    assert claimed.returncode == 0, claimed.stdout + claimed.stderr
    return path


# --------------------------------------------------------------------------- fake executables


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def fake_git(bin_dir: Path, mode: str) -> Path:
    """A `git` wrapper on PATH that `fail`s (exit 128) or `sleep`s 120 s instead of running git."""
    if mode == "fail":
        body = "#!/bin/sh\necho 'fatal: fake git failure' >&2\nexit 128\n"
    elif mode == "sleep":
        body = "#!/bin/sh\nsleep 120\n"
    else:
        raise ValueError(mode)
    return _script(Path(bin_dir) / "git", body)


def notify_script(bin_dir: Path, mode: str, out: Path) -> str:
    """Write a notify command and return its path. `ok` writes its stdin, env and argv to `out` and exits 0;
    `fail` exits 3; `slow` sleeps 30 s; `missing` is a path that does not exist."""
    bin_dir = Path(bin_dir)
    if mode == "missing":
        return str(bin_dir / "no-such-notify-command")
    if mode == "ok":
        body = (f"#!{sys.executable}\nimport json, os, sys\n"
                f"open({str(out)!r}, 'w').write(json.dumps({{'stdin': sys.stdin.read(), 'env': dict(os.environ), 'argv': sys.argv}}))\n")
    elif mode == "fail":
        body = "#!/bin/sh\nexit 3\n"
    elif mode == "slow":
        body = "#!/bin/sh\nsleep 30\n"
    else:
        raise ValueError(mode)
    return str(_script(bin_dir / f"notify-{mode}", body))


def set_notify(command: str, *, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    """`ck agents set run.notify_command <command>` against the isolated CK_CONFIG_HOME."""
    done = ck_run_cmd(Path.cwd(), "agents", "set", "run.notify_command", command, env=env)
    return done


def guard_events(env: Optional[dict] = None) -> List[dict]:
    """The guard log's events, rotated file first, then the current one."""
    from claude_kiro.analytics import guardlog

    current = Path(guardlog.log_path(env))
    events: List[dict] = []
    for path in (current.with_name("guard-events.1.jsonl"), current):
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    with contextlib.suppress(ValueError):
                        events.append(json.loads(line))
    return events


def fake_sandbox_tools(bin_dir: Path) -> Path:
    """Executable `bwrap` and `socat` stubs, so a child process's PATH satisfies the sandbox check."""
    for tool in ("bwrap", "socat"):
        _script(Path(bin_dir) / tool, "#!/bin/sh\nexit 0\n")
    return Path(bin_dir)


def sleeping_fake_claude(bin_dir: Path, record: Path) -> Path:
    """A fake `claude` that records argv and cwd to `record`, then sleeps 5 s (outliving the 3-s early-exit
    window of a detached `ck run`). Poll for `record` with a bounded wait, never a fixed sleep."""
    body = (f"#!{sys.executable}\nimport json, os, sys, time\n"
            f"open({str(record)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'cwd': os.getcwd()}}))\n"
            "time.sleep(5)\n")
    return _script(Path(bin_dir) / "claude", body)


def parking_fake_claude(bin_dir: Path, record: Path, spec: str, task: int | str) -> Path:
    """A fake `claude` that runs the real `ck worktree park <spec> <task> --json` with a fixed note on stdin
    (inheriting the env `ck run` gave it), writes that output to `record`, sleeps 3 s and exits 0 (TC-88)."""
    body = (f"#!{sys.executable}\nimport json, subprocess, sys, time\n"
            f"done = subprocess.run({CK!r} + ['worktree', 'park', {spec!r}, {str(task)!r}, '--json'],\n"
            "                      input='The test contradicts the requirement.\\n', capture_output=True, text=True)\n"
            f"open({str(record)!r}, 'w').write(json.dumps({{'returncode': done.returncode, 'stdout': done.stdout, 'stderr': done.stderr}}))\n"
            "time.sleep(3)\n")
    return _script(Path(bin_dir) / "claude", body)


def test_hook_input_and_post_message_shapes():
    """Self-check of the pure helpers. pytest does not collect this module (its name has no `test_` prefix) and `__all__`
    keeps the function out of `import *`; it exists so the harness task has a checkable test case, and runs when this
    file is passed to pytest explicitly."""
    data = hook_input("/p", "s1", "Bash", command="ls")
    assert data["tool_name"] == "Bash" and data["tool_input"] == {"command": "ls"} and "agent_id" not in data
    assert hook_input("/p", "s1", "Bash", agent_id="a1", agent_type="t", command="ls")["agent_id"] == "a1"
    assert post_message({"decision": "block", "reason": "r"}) == "r"
    assert post_message({"hookSpecificOutput": {"additionalContext": "c"}}) == "c"
    assert post_message(None) is None


__all__ = ['BashOutcome',
    'CK',
    'big_repo',
    'ck_json',
    'ck_run_cmd',
    'claimed_task_worktree',
    'commit_on_base',
    'fake_git',
    'fake_sandbox_tools',
    'guard_events',
    'hook_input',
    'new_on_branch',
    'notify_script',
    'parking_fake_claude',
    'post_message',
    'pre',
    'run_bash',
    'set_notify',
    'sleeping_fake_claude',
    'worktree_of']
