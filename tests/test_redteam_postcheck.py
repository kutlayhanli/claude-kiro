"""Red team for requirement 2.7: the post-check SHALL NOT block or delay the run (prompt-free-runs).

Working from the requirement alone, each case feeds the post-check something hostile and expects: no output (or a
correct catch that names a test file with the `[ck post-check]` prefix), exit 0, done within 5 s, the repository
untouched by the hook, and the gate's verdict unchanged. Every case runs through both hooks (`post-bash-check` and the
installed `done-verify`) as real CLI subprocesses, after a real PreToolUse hook and a real `bash -c`.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import git
from guard_helpers import ck_run_cmd, commit_on_base, fake_git, hook_input
from test_guard_postcheck import AGENT, CALC_PATH, SED_WEAKEN, VIAS, cli_cycle, cli_hook, cli_message

pytestmark = pytest.mark.parametrize("via", VIAS)


@pytest.fixture(autouse=True)
def private_tmp(tmp_path, monkeypatch):
    """Snapshots go to the temp dir; give each test its own so a leftover can be found and corrupted."""
    tmp = tmp_path / "hook-tmp"
    tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp))
    return tmp


def snapshots(tmp: Path):
    return sorted(tmp.glob("**/guardsnap-*"))


def gate_signature(project: Path):
    done = ck_run_cmd(project, "gate", "calc")
    return done.returncode, re.sub(r"\d+(?:\.\d+)?", "N", done.stdout)


def repo_state(project: Path):
    status = subprocess.run(["git", "status", "--porcelain"], cwd=project, capture_output=True, text=True).stdout
    return status


def check_quiet(done, *, strict: bool = False):
    """Exit 0, within 5 s, and either no output or a well-formed catch of a test file."""
    assert done.returncode == 0, done.stderr
    assert done.seconds < 5, done.seconds
    if strict:
        assert done.stdout.strip() == "", done.stdout
        return
    if done.stdout.strip():
        message = cli_message(done)
        assert message and "[ck post-check]" in message and "tests/test_" in message, done.stdout


def exercise(project, session, command, via, *, strict=False, gate=True, between=None, **kwargs):
    """cli_cycle with the gate verdict and the repository compared around the post hook."""
    holder = {}

    def before_post():
        if between:
            between()
        holder["gate"] = gate_signature(project) if gate else None
        holder["state"] = repo_state(project)

    out = cli_cycle(project, session, command, via=via, between=before_post, **kwargs)
    check_quiet(out.post, strict=strict)
    assert repo_state(project) == holder["state"]
    if gate:
        assert gate_signature(project) == holder["gate"]
    return out


# --------------------------------------------------------------------------- no, corrupt or huge snapshots


def test_post_with_no_snapshot_gives_no_output(project, session, via):
    command = SED_WEAKEN
    subprocess.run(["bash", "-c", command], cwd=project, check=True)
    payload = hook_input(project, session, "Bash", agent_id=AGENT, event="PostToolUse",
                         tool_response={"stdout": "", "stderr": "", "interrupted": False}, command=command)
    done = cli_hook(via, payload, cwd=project)
    check_quiet(done, strict=True)


@pytest.mark.parametrize("damage", ["garbage", "truncated", "empty", "wrong-shape", "huge"])
def test_corrupt_truncated_or_huge_snapshot_gives_no_output(project, session, private_tmp, via, damage):
    first = hook_input(project, session, "Bash", agent_id=AGENT, command=SED_WEAKEN)
    assert cli_hook("pre-tool-guard", first, cwd=project).returncode == 0
    found = snapshots(private_tmp)
    assert found, "the pre hook took no snapshot for a command that may write"
    for path in found:
        original = path.read_bytes()
        if damage == "garbage":
            path.write_bytes(b"\xff\xfe\x00 not json {{{")
        elif damage == "truncated":
            path.write_bytes(original[: max(1, len(original) // 2)])
        elif damage == "empty":
            path.write_bytes(b"")
        elif damage == "wrong-shape":
            path.write_text(json.dumps({"createdAt": "yesterday", "checkouts": [1, 2, 3], "caller": 7}))
        else:
            path.write_text(json.dumps({"createdAt": 0, "checkouts": {str(project): {"head": None, "paths": None,
                                                                                    "dirty": {"tests/test_calc.py": {"sha": "x", "content": "a" * 40_000_000}}}}}))
    subprocess.run(["bash", "-c", SED_WEAKEN], cwd=project, check=True)
    gate_before = gate_signature(project)
    payload = hook_input(project, session, "Bash", agent_id=AGENT, tool_use_id=first["tool_use_id"], event="PostToolUse",
                         tool_response={"stdout": "", "stderr": "", "interrupted": False}, command=SED_WEAKEN)
    check_quiet(cli_hook(via, payload, cwd=project))
    assert gate_signature(project) == gate_before


# --------------------------------------------------------------------------- hostile command text


def test_a_megabyte_command_gives_no_output(project, session, via):
    huge = "echo hi > notes.txt # " + "A" * 1_000_000
    first = hook_input(project, session, "Bash", agent_id=AGENT, command=huge)
    pre_done = cli_hook("pre-tool-guard", first, cwd=project)
    assert pre_done.returncode == 0 and pre_done.seconds < 30
    subprocess.run(["bash", "-c", "echo hi > notes.txt"], cwd=project, check=True)
    payload = hook_input(project, session, "Bash", agent_id=AGENT, tool_use_id=first["tool_use_id"], event="PostToolUse",
                         tool_response={"stdout": "", "stderr": "", "interrupted": False}, command=huge)
    check_quiet(cli_hook(via, payload, cwd=project), strict=True)


@pytest.mark.parametrize("text", [
    "echo hi > notes.txt # \u0000 NUL \u0000",
    "sed -i 's/x/x/' tests/test_calc.py \ud800 lone surrogate",
    "echo '‮​' > notes.txt",
    "tests/" * 5000 + "test_calc.py",
])
def test_nul_and_odd_characters_in_the_command_give_no_output(project, session, via, text):
    first = hook_input(project, session, "Bash", agent_id=AGENT, command=text)
    assert cli_hook("pre-tool-guard", first, cwd=project).returncode == 0
    payload = hook_input(project, session, "Bash", agent_id=AGENT, tool_use_id=first["tool_use_id"], event="PostToolUse",
                         tool_response={"stdout": "", "stderr": "", "interrupted": False}, command=text)
    check_quiet(cli_hook(via, payload, cwd=project), strict=True)


def test_invalid_utf8_inside_the_command_gives_no_output(project, via):
    raw = (b'{"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "s", "cwd": "' + str(project).encode()
           + b'", "tool_input": {"command": "sed -i \xff\xfe tests/test_calc.py"}}')
    env = {**os.environ, "LC_ALL": "C.UTF-8", "PYTHONUTF8": "0"}  # pinned: the stdin decoding must not depend on the machine's locale
    check_quiet(cli_hook(via, raw=raw, env=env, cwd=project), strict=True)


@pytest.mark.parametrize("tool_input", [{"command": 7}, None, {}, {"command": ["sed", "-i"]}, {"command": ""}])
def test_wrong_typed_command_gives_no_output(project, via, tool_input):
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "s", "cwd": str(project),
               "tool_use_id": "toolu_x", "agent_id": AGENT, "tool_input": tool_input,
               "tool_response": {"stdout": "", "stderr": "", "interrupted": False}}
    check_quiet(cli_hook(via, payload, cwd=project), strict=True)


def test_the_post_checks_own_message_fed_back_as_a_command_gives_no_output(project, session, via):
    first = exercise(project, session, SED_WEAKEN, via)
    message = cli_message(first.post)
    assert message and "[ck post-check]" in message
    for command in (message, f"echo {json.dumps(message)}", json.dumps({"decision": "block", "reason": message})):
        exercise(project, session, command, via, strict=True)


# --------------------------------------------------------------------------- hostile files


def test_a_binary_test_file_gives_no_output(project, session, via):
    commit_on_base(project, "tests/test_bin.py", "x")
    (project / "tests" / "test_bin.py").write_bytes(os.urandom(200_000))
    git(project, "commit", "-qam", "binary")
    exercise(project, session, "echo hi > notes.txt", via, strict=True)


def test_a_50_mb_test_file_is_handled_within_five_seconds(project, session, via):
    commit_on_base(project, "tests/test_huge.py", "# x\n" * 12_500_000)
    exercise(project, session, "echo '# more' >> tests/test_huge.py", via, gate=False)


def test_a_symlink_loop_among_the_test_files_gives_no_output(project, session, via):
    os.symlink("test_loop.py", project / "tests" / "test_loop.py")
    exercise(project, session, "echo hi > notes.txt", via, strict=True)


def test_a_fifo_among_the_test_files_does_not_block_the_hook(project, session, via):
    os.mkfifo(project / "tests" / "test_fifo.py")
    exercise(project, session, "echo hi > notes.txt", via, strict=True, gate=False)


def test_a_test_file_deleted_between_pre_and_post_is_a_clean_catch_or_silence(project, session, via):
    exercise(project, session, SED_WEAKEN, via, between=lambda: (project / CALC_PATH).unlink(), gate=False)


def test_a_cd_into_a_vanished_directory_gives_no_output(project, session, via):
    (project / "gone").mkdir()
    out = cli_cycle(project, session, "cd gone && echo hi > x.txt", via=via, between=lambda: shutil.rmtree(project / "gone"),
                    post_cwd=project / "gone")
    check_quiet(out.post, strict=True)


def test_hundreds_of_changed_test_files_stay_within_five_seconds(project, session, via):
    for n in range(300):
        (project / "tests" / f"test_many_{n:03d}.py").write_text("def test_x():\n    assert 1\n    assert 2\n")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "many")
    git(project, "checkout", "-q", "main")
    git(project, "merge", "-q", "--no-edit", "feature")
    git(project, "checkout", "-q", "feature")
    exercise(project, session, "sed -i '/assert 2/d' tests/test_many_*.py", via, gate=False)


# --------------------------------------------------------------------------- hostile repository states


def test_a_repo_in_the_middle_of_a_rebase_never_blocks(project, session, via):
    (project / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n")
    git(project, "commit", "-qam", "feature side")
    git(project, "checkout", "-q", "main")
    (project / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    git(project, "commit", "-qam", "main side")
    git(project, "checkout", "-q", "feature")
    stopped = subprocess.run(["git", "rebase", "main"], cwd=project, capture_output=True, text=True)
    assert stopped.returncode != 0 and (project / ".git" / "rebase-merge").exists()
    exercise(project, session, SED_WEAKEN, via, gate=False)


def test_an_index_lock_never_blocks(project, session, via):
    lock = project / ".git" / "index.lock"
    exercise(project, session, SED_WEAKEN, via, between=lambda: lock.write_text(""), gate=False)
    assert lock.exists()


@pytest.mark.parametrize("mode", ["sleep", "fail", "garbage"])
def test_a_git_that_hangs_fails_or_prints_garbage_never_blocks(project, session, tmp_path, via, mode):
    bin_dir = tmp_path / f"bin-{mode}"
    bin_dir.mkdir()
    if mode == "garbage":
        script = bin_dir / "git"
        script.write_text("#!/bin/sh\nprintf '\\377\\376garbage\\0\\0 \\n%s\\n' '{\"not\": \"git\"}'\nexit 0\n")
        script.chmod(0o755)
    else:
        fake_git(bin_dir, mode)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    out = cli_cycle(project, session, SED_WEAKEN, via=via, post_env=env)
    check_quiet(out.post, strict=mode != "garbage")
    assert gate_signature(project)[0] in (0, 1)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_an_unreadable_directory_of_tests_never_blocks(project, session, via):
    sub = project / "tests" / "sealed"
    sub.mkdir()
    (sub / "test_sealed.py").write_text("def test_s():\n    assert 1\n")
    try:
        out = cli_cycle(project, session, "echo hi > notes.txt", via=via, between=lambda: sub.chmod(0))
        check_quiet(out.post, strict=True)
    finally:
        sub.chmod(stat.S_IRWXU)
