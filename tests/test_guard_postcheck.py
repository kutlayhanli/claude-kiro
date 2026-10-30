"""Shell post-check (prompt-free-runs Story 2: TC-9..TC-19, TC-73, TC-75, TC-76, TC-77, TC-82).

Every case is driven the way Claude Code drives it: the PreToolUse hook (which takes the snapshot), the real command
in `bash -c`, then the PostToolUse hook with the same `tool_use_id`. The post-check runs through both of its paths:
`post-bash-check` itself and `done-verify` (the installed path, which carries the post-check finding for Bash calls).
A task agent is a call with `agent_id`. The `project` fixture is a real git repo on `feature` off `main`, with
`tests/test_calc.py` committed on `main`.

Project modules are imported inside test bodies, so this file collects before the implementation exists.
"""

import json
import os
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import CALC_TEST, git, write_tasks
from guard_helpers import (
    CK,
    big_repo,
    ck_json,
    ck_run_cmd,
    claimed_task_worktree,
    commit_on_base,
    fake_git,
    guard_events,
    hook_input,
    new_on_branch,
    post_message,
    pre,
    run_bash,
)

AGENT = "agent-a1b2c3"
CALC_PATH = "tests/test_calc.py"
VIAS = ["post-bash-check", "done-verify"]
SED_WEAKEN = "sed -i '/assert add(1, 2) == 3/d' tests/test_calc.py"
ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hook_inputs"


# --------------------------------------------------------------------------- shared helpers (also used by the red-team and property files)


def py_heredoc(body: str) -> str:
    """A `python` heredoc command running `body` (the interpreter running this suite)."""
    return f"{sys.executable} - <<'EOF'\n{body}\nEOF"


def cli_hook(name: str, payload=None, *, env=None, cwd=None, raw=None, timeout: float = 60) -> SimpleNamespace:
    """Run `python -m claude_kiro.cli --hook <name>` with JSON (or `raw` bytes) on stdin."""
    data = raw if raw is not None else json.dumps(payload).encode("utf-8", "surrogatepass")
    start = time.monotonic()
    done = subprocess.run([*CK, "--hook", name], input=data, capture_output=True, cwd=str(cwd) if cwd else None,
                          env=env if env is not None else os.environ.copy(), timeout=timeout)
    return SimpleNamespace(returncode=done.returncode, stdout=done.stdout.decode("utf-8", "replace"),
                           stderr=done.stderr.decode("utf-8", "replace"), seconds=time.monotonic() - start)


def cli_message(done) -> "str | None":
    """`post_message` of a CLI hook run's stdout (None for empty output)."""
    text = done.stdout.strip()
    return post_message(json.loads(text)) if text else None


def cli_cycle(project, session, command, *, agent_id=AGENT, via="post-bash-check", post_env=None, cwd=None,
              between=None, post_command=None, post_cwd=None) -> SimpleNamespace:
    """Pre hook, real `bash -c`, optional `between()`, then the post hook, all as CLI subprocesses."""
    first = hook_input(project, session, "Bash", agent_id=agent_id, command=command)
    first["cwd"] = str(cwd or project)
    pre_done = cli_hook("pre-tool-guard", first, cwd=cwd or project)
    ran = subprocess.run(["bash", "-c", command], cwd=str(cwd or project), capture_output=True, text=True)
    if between:
        between()
    second = hook_input(project, session, "Bash", agent_id=agent_id, tool_use_id=first["tool_use_id"], event="PostToolUse",
                        tool_response={"stdout": ran.stdout, "stderr": ran.stderr, "interrupted": False},
                        command=post_command if post_command is not None else command)
    second["cwd"] = str(post_cwd or cwd or project)
    post_done = cli_hook(via, second, env=post_env, cwd=cwd or project)
    return SimpleNamespace(pre=pre_done, ran=ran, post=post_done)


# --------------------------------------------------------------------------- TC-9, TC-10


@pytest.mark.parametrize("via", VIAS)
def test_tc9_sed_deleting_an_assertion_is_reported_to_a_task_agent(project, session, via):
    out = run_bash(project, session, SED_WEAKEN, agent_id=AGENT, via=via)
    assert out.pre is None and out.ran and out.returncode == 0
    text = post_message(out.post)
    assert text and CALC_PATH in text
    assert "removed assertion" in text or "removes 1 assertion" in text
    lowered = text.lower()
    assert "restore" in lowered and "report the conflict" in lowered
    assert "[ck post-check]" in text


@pytest.mark.parametrize("via", VIAS)
def test_tc10_python_heredoc_adding_a_skip_names_skip(project, session, via):
    body = ("p = 'tests/test_calc.py'\n"
            "s = open(p).read().replace('def test_add():', '@pytest.mark.skip\\ndef test_add():', 1)\n"
            "open(p, 'w').write(s)")
    out = run_bash(project, session, py_heredoc(body), agent_id=AGENT, via=via)
    assert out.ran and out.returncode == 0
    text = post_message(out.post)
    assert text and CALC_PATH in text and "skip" in text.lower()


@pytest.mark.parametrize("via", VIAS)
def test_tc10_python_c_dropping_a_test_names_test_case(project, session, via):
    code = ("s = open('tests/test_calc.py').read(); "
            "open('tests/test_calc.py', 'w').write(s[:s.index('def test_add_negative')])")
    out = run_bash(project, session, f'{sys.executable} -c "{code}"', agent_id=AGENT, via=via)
    assert out.ran and out.returncode == 0
    text = post_message(out.post)
    assert text and CALC_PATH in text and "test case" in text


# --------------------------------------------------------------------------- TC-11


@pytest.mark.parametrize("via", VIAS)
def test_tc11_main_session_ask_wording(project, session, via):
    ck_json(project, guard={"tests": "ask"})
    out = run_bash(project, session, SED_WEAKEN, via=via)
    assert out.ran
    text = post_message(out.post)
    assert text and CALC_PATH in text
    lowered = text.lower()
    assert "ask me" in lowered
    assert "report the conflict" not in lowered and "guardstop" not in lowered


@pytest.mark.parametrize("via", VIAS)
def test_tc11_main_session_deny_wording(project, session, via):
    ck_json(project, guard={"tests": "deny"})
    out = run_bash(project, session, SED_WEAKEN, via=via)
    assert out.ran
    text = post_message(out.post)
    assert text and CALC_PATH in text and "removes 1 assertion" in text
    lowered = text.lower()
    assert "restore" in lowered
    assert "report the conflict" not in lowered and "guardstop" not in lowered
    assert "ask me" not in lowered


# --------------------------------------------------------------------------- TC-12


def _case_rename(project):
    return "sed -i 's/add(1, 2)/add(2, 1)/' tests/test_calc.py"


def _case_append_test(project):
    body = ("open('tests/test_calc.py', 'a').write('\\n\\ndef test_zero():\\n    assert add(0, 0) == 0\\n')")
    return py_heredoc(body)


def _case_branch_only(project):
    new_on_branch(project, "tests/test_scratch.py", "def test_s():\n    assert 1\n    assert 2\n")
    return "sed -i '/assert 2/d' tests/test_scratch.py"


def _case_owned(project):
    commit_on_base(project, "tests/test_subtract.py", "def test_sub():\n    assert 1\n    assert 2\n")
    write_tasks(project, t1="In Progress")
    return "sed -i '/assert 2/d' tests/test_subtract.py"


@pytest.mark.parametrize("via", VIAS)
@pytest.mark.parametrize("case", [_case_rename, _case_append_test, _case_branch_only, _case_owned])
def test_tc12_non_weakening_new_and_owned_changes_report_nothing(project, session, via, case):
    command = case(project)
    out = run_bash(project, session, command, agent_id=AGENT, via=via)
    assert out.ran and out.returncode == 0
    assert out.post is None


# --------------------------------------------------------------------------- TC-13


READ_ONLY = ["ls tests", "cat tests/test_calc.py", "git status", "git branch", "find tests -name '*.py'",
             "grep -n assert tests/test_calc.py"]


@pytest.mark.parametrize("via", VIAS)
@pytest.mark.parametrize("command", READ_ONLY)
def test_tc13_read_only_commands_report_nothing_and_are_filtered_out(project, session, via, command):
    from claude_kiro.hooks._shared import guard_policy

    assert guard_policy.may_write(command) is False
    out = run_bash(project, session, command, agent_id=AGENT, via=via)
    assert out.ran and out.post is None


@pytest.mark.parametrize("via", VIAS)
def test_tc13_writing_a_non_test_file_leaves_no_changed_test(project, session, via):
    from claude_kiro.hooks._shared import guard_policy

    command = "sed -i 's/x/x/' calc.py"
    assert guard_policy.may_write(command) is True
    out = run_bash(project, session, command, agent_id=AGENT, via=via)
    assert out.ran and out.returncode == 0
    assert out.post is None


# --------------------------------------------------------------------------- TC-14


@pytest.mark.parametrize("via", VIAS)
def test_tc14_a_weakened_file_is_reported_once_and_again_only_when_changed_further(project, session, via):
    first = run_bash(project, session, SED_WEAKEN, agent_id=AGENT, via=via)
    assert CALC_PATH in (post_message(first.post) or "")

    unrelated = run_bash(project, session, "echo hi > notes.txt", agent_id=AGENT, via=via)
    assert unrelated.ran and unrelated.post is None

    second = run_bash(project, session, "sed -i '/assert add(-1, -1) == -2/d' tests/test_calc.py", agent_id=AGENT, via=via)
    text = post_message(second.post)
    assert text and CALC_PATH in text
    assert "removes 1 assertion" in text  # compared from the already weakened content: one more, not two
    # One catch for this command: the "<rel>: <kinds>" part appears once. The S10 text names
    # <rel> a second time in its restore hint, so the bare path count is not the measure.
    assert text.count(f"{CALC_PATH}:") == 1


# --------------------------------------------------------------------------- TC-15


def _git_env(tmp_path: Path, mode: str) -> dict:
    bin_dir = tmp_path / f"bin-{mode}"
    bin_dir.mkdir()
    fake_git(bin_dir, mode)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    return env


@pytest.mark.parametrize("via", VIAS)
@pytest.mark.parametrize("mode", ["fail", "sleep"])
def test_tc15_a_failing_or_sleeping_git_gives_no_output_within_five_seconds(project, session, tmp_path, via, mode):
    out = cli_cycle(project, session, SED_WEAKEN, via=via, post_env=_git_env(tmp_path, mode))
    assert out.ran.returncode == 0
    assert out.post.returncode == 0, out.post.stderr
    assert out.post.stdout.strip() == ""
    assert out.post.seconds < 5, out.post.seconds


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod 000 does not stop root from reading the file")
@pytest.mark.parametrize("via", VIAS)
def test_tc15_an_unreadable_test_file_gives_no_output(project, session, via):
    path = project / CALC_PATH
    try:
        out = cli_cycle(project, session, SED_WEAKEN, via=via, between=lambda: path.chmod(0))
    finally:
        path.chmod(0o644)
    assert out.post.returncode == 0, out.post.stderr
    assert out.post.stdout.strip() == ""
    assert out.post.seconds < 5


# --------------------------------------------------------------------------- TC-16


def test_tc16_an_ignored_catch_still_fails_the_gate(project, session):
    (project / "tests" / "test_subtract.py").write_text(
        "def test_subtract():\n    from calc import subtract\n\n    assert subtract(5, 3) == 2\n")
    (project / "calc.py").write_text((project / "calc.py").read_text() + "\n\ndef subtract(a, b):\n    return a - b\n")
    write_tasks(project, t1="Done", t2="Done", a1="x", a2="x")
    out = run_bash(project, session, SED_WEAKEN + " && git commit -qam sneaky", agent_id=AGENT)
    assert out.ran and out.returncode == 0
    assert CALC_PATH in (post_message(out.post) or "")  # the catch is ignored below
    gate = ck_run_cmd(project, "gate", "calc", "--task", "2")
    assert gate.returncode != 0, gate.stdout
    assert "No existing tests deleted or weakened" in gate.stdout + gate.stderr
    assert "tests/test_calc.py: removes 1 assertion" in gate.stdout + gate.stderr


# --------------------------------------------------------------------------- TC-17


def _timed_post(project, command, via, *, agent_id=AGENT):
    """Pre hook, real command, then the time of one in-process post-check call."""
    from claude_kiro.cli.runner import execute_hook

    session = f"timing-{time.monotonic_ns()}"
    first = hook_input(project, session, "Bash", agent_id=agent_id, command=command)
    execute_hook("pre-tool-guard", first)
    done = subprocess.run(["bash", "-c", command], cwd=str(project), capture_output=True, text=True)
    second = hook_input(project, session, "Bash", agent_id=agent_id, tool_use_id=first["tool_use_id"], event="PostToolUse",
                        tool_response={"stdout": done.stdout, "stderr": done.stderr, "interrupted": False}, command=command)
    if via == "check":
        from claude_kiro.hooks import post_bash_check

        call = lambda: post_bash_check.check(second)  # noqa: E731
    else:
        call = lambda: execute_hook("done-verify", second)  # noqa: E731
    start = time.perf_counter()
    result = call()
    return time.perf_counter() - start, result


@pytest.fixture
def large(tmp_path):
    root = big_repo(tmp_path)
    (root / "specs").mkdir()
    (root / "specs" / "ck.json").write_text(json.dumps({"verify": []}))
    git(root, "add", "-A")
    git(root, "commit", "-qm", "ck.json")
    git(root, "checkout", "-qb", "feature")
    return root


@pytest.mark.parametrize("via", ["check", "done-verify"])
def test_tc17_skipped_command_costs_under_100_ms(large, via):
    _timed_post(large, "ls", via)  # warm-up: imports
    times = [_timed_post(large, "ls", via)[0] for _ in range(5)]
    assert statistics.median(times) < 0.1, times


@pytest.mark.parametrize("via", ["check", "done-verify"])
def test_tc17_sed_over_five_test_files_costs_under_one_second(large, via):
    command = "sed -i '/assert/d' tests/test_mod_000.py tests/test_mod_001.py tests/test_mod_002.py tests/test_mod_003.py tests/test_mod_004.py"
    _timed_post(large, "ls", via)  # warm-up
    times = []
    for _ in range(5):
        git(large, "checkout", "-q", "--", "tests")
        seconds, result = _timed_post(large, command, via)
        assert result is not None  # the weakenings are found, not skipped
        times.append(seconds)
    assert statistics.median(times) < 1.0, times


# --------------------------------------------------------------------------- TC-18


def test_tc18_end_to_end_through_the_cli_hooks(project, session):
    out = cli_cycle(project, session, SED_WEAKEN)
    assert out.pre.returncode == 0 and out.pre.stdout.strip() == ""
    assert out.post.returncode == 0, out.post.stderr
    text = cli_message(out.post)
    assert text and CALC_PATH in text and "removes 1 assertion" in text
    diff = git(project, "diff", "--", CALC_PATH)
    assert "-    assert add(1, 2) == 3" in diff


# --------------------------------------------------------------------------- TC-19


def test_tc19_registration():
    from claude_kiro.cli.runner import HOOK_SETTINGS, HookRegistry

    assert "post-bash-check" in HookRegistry._hooks
    summary = json.loads((FIXTURES / "capture-summary.json").read_text())
    marker_built = not summary["workflowAgentHasAgentId"]

    base = {("PreToolUse", "Edit|Write|MultiEdit|Bash", "pre-tool-guard"),
            ("PostToolUse", "Edit|Write|MultiEdit", "post-file-ops"),
            ("PostToolUse", "Edit|Write|MultiEdit|Bash", "done-verify"),
            ("Stop", None, "stop-verify")}
    got = {(s["event"], s["matcher"], s["hook"]) for s in HOOK_SETTINGS}
    expected = set(base)
    if marker_built:
        marker = [s for s in HOOK_SETTINGS if s["hook"] == "marker-clear"]
        assert len(marker) == 1 and marker[0]["event"] == "UserPromptSubmit"
        expected |= {(marker[0]["event"], marker[0]["matcher"], "marker-clear")}
    assert got == expected
    verify = [s for s in HOOK_SETTINGS if s["hook"] == "done-verify"]
    assert len(verify) == 1 and verify[0]["event"] == "PostToolUse" and "Bash" in verify[0]["matcher"]

    hooks_json = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text())["hooks"]
    seen = set()
    for event, groups in hooks_json.items():
        for group in groups:
            for entry in group["hooks"]:
                name = entry["command"].split("--hook ")[1].split()[0]
                seen.add((event, group.get("matcher"), name))
    assert seen == expected


def test_tc19_ck_init_vendor_installs_the_same_set(tmp_path):
    from claude_kiro.cli.runner import configured_hooks

    root = tmp_path / "initproj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    done = ck_run_cmd(root, "init", "--vendor", "--no-allow-workflow")
    assert done.returncode == 0, done.stdout + done.stderr
    settings = json.loads((root / ".claude" / "settings.json").read_text())
    found = configured_hooks(settings)
    assert "done-verify" in found.get("PostToolUse", [])
    summary = json.loads((FIXTURES / "capture-summary.json").read_text())
    assert ("marker-clear" in found.get("UserPromptSubmit", [])) == (not summary["workflowAgentHasAgentId"])
    assert "post-bash-check" not in sum(found.values(), [])  # it runs through done-verify when installed


# --------------------------------------------------------------------------- TC-73


@pytest.mark.parametrize("via", VIAS)
def test_tc73_absolute_path_into_a_claimed_task_worktree(project, session, via):
    wt = claimed_task_worktree(project, "calc", 1)
    sed = f"sed -i '/assert add(1, 2) == 3/d' {wt}/tests/test_calc.py"
    out = run_bash(project, session, sed, agent_id=AGENT, via=via)
    assert out.ran and out.returncode == 0
    text = post_message(out.post)
    assert text and CALC_PATH in text and "removes 1 assertion" in text


@pytest.mark.parametrize("via", VIAS)
def test_tc73_python_heredoc_by_absolute_path_dropping_a_test(project, session, via):
    wt = claimed_task_worktree(project, "calc", 1)
    body = (f"p = '{wt}/tests/test_calc.py'\n"
            "s = open(p).read()\n"
            "open(p, 'w').write(s[:s.index('def test_add_negative')])")
    out = run_bash(project, session, py_heredoc(body), agent_id=AGENT, via=via)
    assert out.ran and out.returncode == 0
    text = post_message(out.post)
    assert text and CALC_PATH in text and "test case" in text


# --------------------------------------------------------------------------- TC-75


@pytest.mark.parametrize("via", VIAS)
def test_tc75_sed_then_commit_is_still_compared(project, session, via):
    out = run_bash(project, session, SED_WEAKEN + " && git commit -qam wip", agent_id=AGENT, via=via)
    assert out.ran and out.returncode == 0
    assert git(project, "status", "--porcelain", CALC_PATH).strip() == ""  # clean afterwards
    text = post_message(out.post)
    assert text and CALC_PATH in text and "removes 1 assertion" in text


@pytest.mark.parametrize("via", VIAS)
def test_tc75_checkout_of_another_version_then_commit_is_caught(project, session, via):
    # TC-75(b) adapted: the plan checks out `main`'s version, but that version equals the base ref's content, which
    # D19 exempts. To keep the case about HEAD moving, `main` first moves on with a weaker version (no longer the
    # merge-base's), and `feature` has added an assertion to the file.
    path = project / CALC_PATH
    path.write_text(CALC_TEST + "\n\ndef test_zero():\n    assert add(0, 0) == 0\n")
    git(project, "commit", "-qam", "feature adds a test")
    git(project, "checkout", "-q", "main")
    (project / CALC_PATH).write_text("from calc import add\n\n\ndef test_add():\n    pass\n")
    git(project, "commit", "-qam", "main moves on, weaker")
    git(project, "checkout", "-q", "feature")
    out = run_bash(project, session, "git checkout main -- tests/test_calc.py && git commit -qm x", agent_id=AGENT, via=via)
    assert out.ran and out.returncode == 0
    text = post_message(out.post)
    assert text and CALC_PATH in text


@pytest.mark.parametrize("via", VIAS)
def test_tc75_commit_after_a_non_test_edit_reports_nothing(project, session, via):
    first = run_bash(project, session, "echo '# note' >> calc.py", agent_id=AGENT, via=via)
    assert first.post is None
    out = run_bash(project, session, "git commit -qam y", agent_id=AGENT, via=via)
    assert out.ran and out.returncode == 0
    assert out.post is None


# --------------------------------------------------------------------------- TC-76


@pytest.mark.parametrize("via", VIAS)
def test_tc76_find_exec_sed_passes_pre_and_is_caught_after(project, session, via):
    out = run_bash(project, session, "find tests -name test_calc.py -exec sed -i '/assert/d' {} +", agent_id=AGENT, via=via)
    assert out.pre is None and out.ran and out.returncode == 0
    text = post_message(out.post)
    assert text and CALC_PATH in text and "assertion" in text


@pytest.mark.parametrize("via", VIAS)
def test_tc76_find_delete_of_a_test_is_caught_as_a_deletion(project, session, via):
    out = run_bash(project, session, "find tests -name test_calc.py -delete", agent_id=AGENT, via=via)
    assert out.pre is None and out.ran and out.returncode == 0
    assert not (project / CALC_PATH).exists()
    text = post_message(out.post)
    assert text and CALC_PATH in text and "deletes the test file" in text


@pytest.mark.parametrize("via", VIAS)
def test_tc76_find_delete_of_pyc_files_reports_nothing(project, session, via):
    cache = project / "tests" / "__pycache__"
    cache.mkdir()
    (cache / "test_calc.cpython-311.pyc").write_bytes(b"\0\0")
    out = run_bash(project, session, "find tests -name '*.pyc' -delete", agent_id=AGENT, via=via)
    assert out.pre is None and out.ran and out.returncode == 0
    assert out.post is None


@pytest.mark.parametrize("command", [
    "find tests -name x -exec echo {} \\;",
    "find tests -name x -execdir echo {} \\;",
    "find tests -name x -ok echo {} \\;",
    "find tests -name x -delete",
    "find tests -name x -fprint out.txt",
    "find tests -name x -exec sed -i s/a/b/ {} +",
])
def test_tc76_find_write_forms_may_write(command):
    from claude_kiro.hooks._shared import guard_policy

    assert guard_policy.may_write(command) is True


def test_tc76_plain_find_does_not_write():
    from claude_kiro.hooks._shared import guard_policy

    assert guard_policy.may_write("find tests -name '*.py'") is False


# --------------------------------------------------------------------------- TC-77


def _pair(project, session, n, env):
    """One pre/command/post triple as three CLI subprocesses: a task agent weakening its own test file."""
    rel = f"tests/test_c{n}.py"
    command = f"sed -i '/assert 2/d' {rel}"
    agent = f"agent-{n}"
    first = hook_input(project, session, "Bash", agent_id=agent, tool_use_id=f"toolu_pair{n}", command=command)
    pre_done = cli_hook("pre-tool-guard", first, env=env, cwd=project)
    ran = subprocess.run(["bash", "-c", command], cwd=str(project), capture_output=True, text=True)
    second = hook_input(project, session, "Bash", agent_id=agent, tool_use_id=f"toolu_pair{n}", event="PostToolUse",
                        tool_response={"stdout": "", "stderr": "", "interrupted": False}, command=command)
    post_done = cli_hook("post-bash-check", second, env=env, cwd=project)
    return n, rel, pre_done, ran, post_done


def test_tc77_eight_concurrent_pairs_each_catch_their_own_file_once(project, session, tmp_path, monkeypatch):
    tmp = tmp_path / "tmpdir"
    tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp))
    for n in range(8):
        commit_on_base(project, f"tests/test_c{n}.py", "def test_x():\n    assert 1\n    assert 2\n")
    probe_command = "sed -i '/assert 2/d' tests/test_c0.py"
    probe = hook_input(project, session, "Bash", agent_id="agent-probe", tool_use_id="toolu_probe", command=probe_command)
    assert cli_hook("pre-tool-guard", probe, env=os.environ.copy(), cwd=project).returncode == 0
    assert list(tmp.glob("**/guardsnap-*")) != []  # a pre hook alone leaves a snapshot, so "none left" below means something
    done = hook_input(project, session, "Bash", agent_id="agent-probe", tool_use_id="toolu_probe", event="PostToolUse",
                      tool_response={"stdout": "", "stderr": "", "interrupted": False}, command=probe_command)
    assert cli_hook("post-bash-check", done, env=os.environ.copy(), cwd=project).returncode == 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda n: _pair(project, session, n, os.environ.copy()), range(8)))
    for n, rel, pre_done, ran, post_done in results:
        assert pre_done.returncode == 0 and ran.returncode == 0 and post_done.returncode == 0, post_done.stderr
        text = cli_message(post_done)
        assert text, f"pair {n} caught nothing"
        assert text.count(rel) >= 1
        others = [f"tests/test_c{m}.py" for m in range(8) if m != n]
        assert not any(o in text for o in others), f"pair {n} reported another pair's file: {text}"
    events = [e for e in guard_events() if e.get("decision") == "post-check"]
    assert sorted(e["path"] for e in events) == sorted(f"tests/test_c{n}.py" for n in range(8))
    assert list(tmp.glob("**/guardsnap-*")) == []  # no snapshot is left behind


def test_tc77_a_script_built_path_into_another_worktree_is_left_to_the_gate(project, session):
    w1 = claimed_task_worktree(project, "calc", 1)
    w2 = claimed_task_worktree(project, "calc", 2)
    code = ("import os; "
            f"p = os.path.join({str(w2.parent)!r}, {w2.name!r}, 'tests', 'test_calc.py'); "
            "s = open(p).read(); open(p, 'w').write(s.replace('    assert add(1, 2) == 3', '    pass'))")
    command = f"{sys.executable} -c \"{code}\""
    assert str(w2) not in command
    out = run_bash(project, session, command, agent_id=AGENT)
    assert out.ran and out.returncode == 0
    assert "pass" in (w2 / CALC_PATH).read_text()
    assert out.post is None  # D20: worktrees the command does not name are not covered
    assert w1.exists()
    gate = ck_run_cmd(w2, "gate", "calc", "--task", "2")
    assert gate.returncode != 0, gate.stdout
    assert "No existing tests deleted or weakened" in gate.stdout + gate.stderr


# --------------------------------------------------------------------------- TC-82

MERGED = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n\n\ndef test_add_negative():\n    pass\n"


def _legit_change(project: Path, via_branch: str) -> str:
    """A commit that removes an assertion from the test on `main` (or on a branch `integ` cut from it)."""
    git(project, "checkout", "-q", "main")
    if via_branch != "main":
        git(project, "checkout", "-q", "-b", via_branch)
    (project / CALC_PATH).write_text(MERGED)
    git(project, "commit", "-qam", "a legitimate change to the test")
    git(project, "checkout", "-q", "feature")
    return via_branch


@pytest.mark.parametrize("via", VIAS)
@pytest.mark.parametrize("ref", ["main", "integ"])
def test_tc82_merging_in_a_legitimate_test_change_reports_nothing_then_later_weakening_is_caught(project, session, via, ref):
    wt = claimed_task_worktree(project, "calc", 1)
    _legit_change(project, ref)
    merged = run_bash(project, session, f"git -C {wt} merge -q --no-edit {ref}", agent_id=AGENT, via=via)
    assert merged.ran and merged.returncode == 0, merged
    assert (wt / CALC_PATH).read_text() == MERGED
    assert merged.post is None

    out = run_bash(project, session, f"sed -i '/assert add(2, 2)/d' {wt}/tests/test_calc.py", agent_id=AGENT, via=via)
    assert out.ran and out.returncode == 0
    text = post_message(out.post)
    assert text and CALC_PATH in text and "removes 1 assertion" in text
