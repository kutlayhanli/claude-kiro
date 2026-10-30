"""Bash pre-check (prompt-free-runs Story 1: TC-1..TC-8, TC-71).

Driven through the real hook entry point (`execute_hook("pre-tool-guard", ...)`) on the `project` fixture, a real git
repo on `feature` off `main` with `tests/test_calc.py` committed on `main`. A task agent is a call with `agent_id`.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import CALC_TEST, git, write_tasks
from guard_helpers import (
    claimed_task_worktree,
    ck_json,
    commit_on_base,
    decision,
    guard_events,
    new_on_branch,
    post_message,
    pre,
    run_bash,
    worktree_of,
)

AGENT = "agent-a1b2c3"
CALC_PATH = "tests/test_calc.py"


def reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def calc_untouched(root: Path) -> bool:
    return (Path(root) / CALC_PATH).read_text() == CALC_TEST


def bash(project, session, command, *, agent_id=None):
    return pre(project, session, "Bash", agent_id=agent_id, command=command)


# --------------------------------------------------------------------------- TC-1

TC1_COMMANDS = [
    "rm tests/test_calc.py",
    "rm -f tests/test_calc.py",
    "mv tests/test_calc.py {tmp}/x.py",
    "git rm -q tests/test_calc.py",
    "git mv tests/test_calc.py tests/test_moved.py",
    "unlink tests/test_calc.py",
    "truncate -s 0 tests/test_calc.py",
    "rm -rf tests",
    "rm -rf tests/",
    "git rm -r tests",
]


@pytest.mark.parametrize("command", TC1_COMMANDS)
def test_tc1_deleting_or_moving_an_existing_test_is_blocked(project, session, tmp_path, command):
    command = command.format(tmp=tmp_path)
    ck_json(project, guard={"tests": "ask"})
    assert decision(bash(project, session, command)) == "ask"
    assert calc_untouched(project)
    assert decision(bash(project, session, command, agent_id=AGENT)) == "deny"  # a task agent is never asked
    assert calc_untouched(project)
    ck_json(project, guard={"tests": "deny"})
    assert decision(bash(project, session, command)) == "deny"
    assert decision(bash(project, session, command, agent_id=AGENT)) == "deny"
    assert calc_untouched(project)


# --------------------------------------------------------------------------- TC-2

TC2_COMMANDS = [
    "sed -i 's/3/3/' tests/test_calc.py",
    "sed -i.bak 's/3/3/' tests/test_calc.py",
    "perl -pi -e 's/a/a/' tests/test_calc.py",
    "python - <<'EOF'\nopen('tests/test_calc.py','a').write('')\nEOF",
    "echo x >> tests/test_calc.py",
    "cat > tests/test_calc.py <<EOF\ndef test_add():\n    assert True\nEOF",
]


@pytest.mark.parametrize("command", TC2_COMMANDS)
def test_tc2_in_place_script_and_redirect_writes_pass(project, session, command):
    for mode in ("ask", "deny"):
        ck_json(project, guard={"tests": mode})
        assert bash(project, session, command) is None
        assert bash(project, session, command, agent_id=AGENT) is None
    assert calc_untouched(project)  # the pre-check never ran the command


# --------------------------------------------------------------------------- TC-3


def make_pycache(project: Path) -> Path:
    """A real `python -m pytest` run, so `tests/__pycache__` exists the way it does for agents."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONDONTWRITEBYTECODE", "PYTEST_ADDOPTS")}
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", CALC_PATH], cwd=project, env=env,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    cache = project / "tests" / "__pycache__"
    assert cache.is_dir() and list(cache.glob("*.pyc")), "pytest left no tests/__pycache__"
    return cache


@pytest.mark.parametrize("command", ["rm tests/test_scratch.py", "mv tests/test_scratch.py tests/test_moved.py",
                                     "git rm tests/test_scratch.py"])
def test_tc3_scratch_test_new_on_the_branch_passes(project, session, command):
    new_on_branch(project, "tests/test_scratch.py", "def test_s():\n    assert 1\n")
    assert bash(project, session, command, agent_id=AGENT) is None


@pytest.mark.parametrize("verb", ["rm", "git rm", "mv"])
def test_tc3_owned_test_of_an_in_progress_test_task_passes(project, session, verb):
    commit_on_base(project, "tests/test_subtract.py", "def test_sub():\n    assert True\n")
    write_tasks(project, t1="In Progress")
    command = f"{verb} tests/test_subtract.py" + (" tests/test_sub_moved.py" if verb == "mv" else "")
    assert bash(project, session, command, agent_id=AGENT) is None


def test_tc3_real_pycache_and_pytest_cache_pass(project, session):
    make_pycache(project)
    cache = project / "tests" / ".pytest_cache"
    cache.mkdir()
    (cache / "README.md").write_text("cache\n")
    for command in ("rm -rf tests/__pycache__", "rm tests/.pytest_cache -rf", "find tests -name '*.pyc' -delete"):
        assert bash(project, session, command, agent_id=AGENT) is None, command
    assert bash(project, session, "rm -rf tests/__pycache__") is None  # the main session too


# --------------------------------------------------------------------------- TC-4


def test_tc4_cd_into_a_linked_worktree_is_resolved(project, session):
    wt = worktree_of(project, "w1")
    new_on_branch(wt, "tests/test_new_scratch.py", "def test_n():\n    assert 1\n")
    for prefix in (f"cd {wt} && ", f"cd {wt}; ", f'cd "{wt}" && '):
        assert decision(bash(project, session, prefix + "rm tests/test_calc.py")) == "ask", prefix
        assert decision(bash(project, session, prefix + "rm tests/test_calc.py", agent_id=AGENT)) == "deny", prefix
        assert bash(project, session, prefix + "rm tests/test_new_scratch.py") is None, prefix
        assert bash(project, session, prefix + "rm tests/test_new_scratch.py", agent_id=AGENT) is None, prefix
    quoted = f'cd "{wt}" && git rm tests/test_calc.py'
    assert decision(bash(project, session, quoted)) == "ask"
    assert decision(bash(project, session, quoted, agent_id=AGENT)) == "deny"
    assert (wt / CALC_PATH).exists() and calc_untouched(project)


# --------------------------------------------------------------------------- TC-5


def test_tc5_task_agent_denial_reason_names_the_file_and_the_instruction(project, session):
    result = bash(project, session, "rm tests/test_calc.py", agent_id=AGENT)
    assert decision(result) == "deny"
    text = reason(result)
    lowered = text.lower()
    assert "[ck guard]" in text
    assert "tests/test_calc.py" in text
    assert "deletes the test file" in lowered
    assert "keep the test" in lowered
    assert "do not work around" in lowered
    assert "report the conflict" in lowered and "quoting the test and the requirement" in lowered


# --------------------------------------------------------------------------- TC-6


@pytest.mark.parametrize("agent_id", [None, AGENT])
def test_tc6_guard_off_means_both_hooks_stay_silent(project, session, agent_id):
    ck_json(project, guard={"tests": "off"})
    weaken = "sed -i '/assert add(1, 2)/d' tests/test_calc.py"
    for command in ("rm tests/test_calc.py", weaken):
        git(project, "checkout", "-q", "--", "tests")  # restore the tests only: specs/ck.json keeps "off"
        out = run_bash(project, session, command, agent_id=agent_id)
        assert out.pre is None and out.ran, command
        assert out.post is None and post_message(out.post) is None, command
    assert guard_events() == []


# --------------------------------------------------------------------------- TC-7


def _state_plain(tmp: Path) -> Path:
    root = tmp / "plain"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_calc.py").write_text(CALC_TEST)
    return root


def _state_missing_base(tmp: Path) -> Path:
    root = tmp / "missingbase"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text(CALC_TEST)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def _state_unborn(tmp: Path) -> Path:
    root = tmp / "unborn"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_calc.py").write_text(CALC_TEST)
    git(root, "init", "-q", "-b", "main")
    return root


def _state_garbage_head(tmp: Path) -> Path:
    root = _state_missing_base(tmp)
    (root / ".git" / "HEAD").write_text("garbage \x00 not a ref\n")
    return root


@pytest.mark.parametrize("build,base", [(_state_plain, None), (_state_missing_base, "no-such-ref"), (_state_unborn, None),
                                        (_state_garbage_head, None)])
@pytest.mark.parametrize("command", ["rm tests/test_calc.py", "rm -rf tests", "git rm -q tests/test_calc.py"])
def test_tc7_unknown_base_never_asks(tmp_path, session, build, base, command):
    root = build(tmp_path)
    ck = {"guard": {"tests": "ask", "requirements": "ask", "test_plan": "ask"}}
    if base:
        ck["base"] = base
    (root / "specs").mkdir(exist_ok=True)
    (root / "specs" / "ck.json").write_text(json.dumps(ck))
    assert pre(root, session, "Bash", command=command) is None
    assert pre(root, session, "Bash", agent_id=AGENT, command=command) is None
    assert (root / "tests" / "test_calc.py").exists()


# --------------------------------------------------------------------------- TC-8


def hook_cli(project: Path, session: str, command: str, cwd: Path = None):
    payload = {"session_id": session, "cwd": str(cwd or project), "hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": command}, "tool_use_id": "toolu_tc8", "agent_id": AGENT}
    return subprocess.run([sys.executable, "-m", "claude_kiro.cli", "--hook", "pre-tool-guard"], input=json.dumps(payload),
                          capture_output=True, text=True, cwd=cwd or project)


def test_tc8_every_ask_category_from_the_transcripts_now_passes_silently(project, session):
    new_on_branch(project, "tests/test_scratch.py", "def test_s():\n    assert 1\n")
    new_on_branch(project, "tests/test_scratch2.py", "def test_s():\n    assert 1\n")
    (project / "tests" / "__pycache__").mkdir()
    (project / "tests" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    wt = worktree_of(project, "w8")
    commands = [
        "sed -i 's/assert/#/' tests/test_calc.py",
        "rm tests/test_scratch.py",
        "python - <<'EOF'\nopen('tests/test_calc.py', 'w').write('x = 1\\n')\nEOF",
        "rm -rf tests/__pycache__",
        "git rm -q tests/test_scratch2.py",
        f"cd {wt} && sed -i 's/assert/#/' tests/test_calc.py",
    ]
    for command in commands:
        done = hook_cli(project, session, command)
        assert done.returncode == 0, (command, done.stderr)
        assert done.stdout.strip() == "", (command, done.stdout)
    assert calc_untouched(project)


# --------------------------------------------------------------------------- TC-71


def test_tc71_absolute_deletes_in_a_claimed_task_worktree(project, session):
    wt = claimed_task_worktree(project, "calc", 1)
    new_on_branch(wt, "tests/test_new_scratch.py", "def test_n():\n    assert 1\n")
    for command in (f"rm {wt}/tests/test_calc.py", f"git -C {wt} rm tests/test_calc.py", f"rm -rf {wt}/tests"):
        assert decision(bash(project, session, command, agent_id=AGENT)) == "deny", command
    assert bash(project, session, f"rm {wt}/tests/test_new_scratch.py", agent_id=AGENT) is None
    assert (wt / CALC_PATH).exists()
