"""Story 9 (9.1 to 9.11, NFR sandbox): protected-file git hooks, landing tokens and my approval.

TC-75 to TC-88, TC-189 and TC-194, on `hooks_repo` with plain git (`raw`). Every case that runs a refused command
checks the ref value before and after, not only the exit code. Tests never write ck's hooks themselves: they only
remove, alter or chmod them, and let ck install them (`ck worktree integration`, `ck worktree start`, `ck run`).
Hooks and tokens are ck's: the project's own hooks (TC-85, TC-194) are the only hook scripts a test writes.
"""

import json
import os
import pty
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

import unattended_helpers as h

PROTECTED_REF = "refs/heads/integrate/demo"
ALLOW_COMMANDS = [".claude/commands/**"]


# --------------------------------------------------------------------------- helpers


def common_dir(root: Path) -> Path:
    return Path(h.raw(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())


def hooks_dir(root: Path) -> Path:
    return common_dir(root) / "ck" / "hooks"


def tip(root: Path, n: int) -> str:
    sha = h.ref(root, f"feat/demo-task-{n}")
    assert sha, f"feat/demo-task-{n} does not exist"
    return sha


def integ_ref(root: Path, spec: str = "demo") -> str:
    return h.ref(root, f"integrate/{spec}")


def hooks_path_config(cwd: Path) -> str:
    return h.raw(cwd, "config", "core.hooksPath").stdout.strip()


def assert_ck_hooks_installed(root: Path) -> None:
    """ck's hooks are installed: core.hooksPath points at <common>/ck/hooks and both scripts exist."""
    value = hooks_path_config(root)
    assert value, "core.hooksPath is not set: ck did not install its hooks"
    assert os.path.realpath(os.path.join(root, value)) == os.path.realpath(hooks_dir(root)), (
        f"core.hooksPath {value!r} is not ck's hooks directory {hooks_dir(root)}")
    for name in ("reference-transaction", "pre-merge-commit"):
        assert (hooks_dir(root) / name).is_file(), f"ck did not write the {name} hook"


def detach_integration(root: Path, spec: str = "demo") -> Path:
    """Detach HEAD in the integration worktree so plain git itself would allow moving the branch.

    Without it `branch -f` and `push .` are refused by git ("checked out") and never reach the hook."""
    wt = h.integration_worktree(root, spec)
    h.git(wt, "checkout", "-q", "--detach")
    return wt


def remove_ck_hooks(root: Path) -> None:
    """Delete ck's hooks (what an attacker, or a fresh clone, leaves); core.hooksPath keeps pointing at them."""
    shutil.rmtree(hooks_dir(root), ignore_errors=True)
    assert not hooks_dir(root).exists()


def reinstall_hooks(root: Path, spec: str = "demo") -> None:
    code, out, err = h.ck_text(root, "worktree", "integration", spec)
    assert code == 0, f"ck worktree integration {spec} failed ({code}): {out}{err}"
    assert_ck_hooks_installed(root)


def commit_on_integration_without_hooks(root: Path, files: Dict[str, str], spec: str = "demo") -> str:
    """Commit protected files on integrate/<spec> the only way a test can before hooks exist: with ck's hooks
    removed; then let ck reinstall them (TC-75b). Returns the new tip."""
    remove_ck_hooks(root)
    wt = h.integration_worktree(root, spec)
    for rel, content in files.items():
        target = wt / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        h.git(wt, "add", "--", rel)
    h.git(wt, "commit", "-qm", "project config")
    reinstall_hooks(root, spec)
    return integ_ref(root, spec)


def allow_paths_on_integration(root: Path, paths: List[str], spec: str = "demo") -> str:
    return commit_on_integration_without_hooks(
        root, {"specs/ck.json": json.dumps({"land": {"allow_paths": paths}}) + "\n"}, spec)


def assert_refused(root: Path, proc: subprocess.CompletedProcess, ref_name: str, before: str, what: str) -> None:
    assert proc.returncode != 0, f"{what} succeeded; it must be refused:\n{proc.stdout}{proc.stderr}"
    assert h.ref(root, ref_name) == before, f"{what} moved {ref_name} although it was refused"


def assert_hook_refusal(root: Path, run_git, ref_name: str, before: str, what: str) -> None:
    """A raw update-ref of the protected commit, refused by ck's hook (its message names .claude/CLAUDE.md)."""
    proc = run_git(["update-ref", ref_name, tip(root, 1)])
    assert_refused(root, proc, ref_name, before, what)
    assert ".claude/CLAUDE.md" in proc.stdout + proc.stderr, f"{what}: not ck's hook message:\n{proc.stdout}{proc.stderr}"


def states_of(obj: Any) -> Dict[str, str]:
    """task -> state, from `land --json` ({"tasks": [...]}) or `merge --json` ([...])."""
    items = obj["tasks"] if isinstance(obj, dict) else obj
    return {str(i["task"]): i["state"] for i in items}


def details_of(obj: Any) -> Dict[str, str]:
    items = obj["tasks"] if isinstance(obj, dict) else obj
    return {str(i["task"]): json.dumps(i) for i in items}


def find_key(obj: Any, key: str) -> List[Any]:
    """Every value stored under `key` anywhere in a JSON document."""
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


def hook_status_of(obj: Any) -> Dict[str, Any]:
    """The `hooks` object (HookStatus) of a `ck worktree start` / `integration --json` document."""
    statuses = [v for v in find_key(obj, "hooks") if isinstance(v, dict)]
    assert statuses, f"no `hooks` object in {json.dumps(obj)[:500]}"
    return statuses[0]


def approve_in_pty(root: Path, spec: str, task: str, into: str, answer: Optional[str] = None,
                   timeout: float = 90) -> Tuple[int, str]:
    """Run `ck worktree approve SPEC TASK --into INTO` on a real pseudo-terminal (`pty.fork`) and type the answer.

    `answer` None means: type the 6-character code the command printed. Returns (exit code, terminal text)."""
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["TERM"] = "dumb"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = [*h.CK_ARGV, "worktree", "approve", spec, task, "--into", into]
    pid, fd = pty.fork()
    if pid == 0:  # child: its stdin, stdout and controlling terminal are the pty
        try:
            os.chdir(root)
            os.execvpe(argv[0], argv, env)
        finally:
            os._exit(127)
    buf = b""
    sent = False
    deadline = time.time() + timeout
    status = None
    try:
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 1.0)
            if ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                buf += data
                continue
            if not sent and buf:  # quiet with output on screen: the command is waiting for me
                text = buf.decode("utf-8", "replace")
                typed = answer if answer is not None else find_code(text)
                if typed:
                    os.write(fd, typed.encode() + b"\n")
                    sent = True
        else:
            os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
    finally:
        os.close(fd)
    text = buf.decode("utf-8", "replace")
    return os.waitstatus_to_exitcode(status), text


def find_code(text: str) -> Optional[str]:
    """The 6-character confirmation code `ck worktree approve` printed."""
    clean = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text).replace("\r", "")
    m = re.findall(r"(?i)\bcode\b\W{1,8}([A-Za-z0-9]{6})\b", clean)
    if m:
        return m[-1]
    tokens = [t for t in re.findall(r"\b[A-Za-z0-9]{6}\b", clean) if t.isupper() or any(c.isdigit() for c in t)]
    return tokens[-1] if tokens else None


# --------------------------------------------------------------------------- TC-75


def test_tc75a_after_integration_creation_a_raw_protected_update_is_refused(tmp_path):
    root = h.hooks_repo(tmp_path)  # `ck worktree integration demo` (CREATED) ran in the fixture
    assert_ck_hooks_installed(root)
    detach_integration(root)
    before = integ_ref(root)
    out = h.raw(root, "update-ref", PROTECTED_REF, tip(root, 1))
    assert_refused(root, out, PROTECTED_REF, before, "raw update-ref of integrate/demo to a protected commit")


def test_tc75b_removed_hooks_are_reinstalled_when_the_integration_worktree_is_reused(tmp_path):
    root = h.hooks_repo(tmp_path)
    remove_ck_hooks(root)
    code, out, err = h.ck_text(root, "worktree", "integration", "demo")
    assert code == 0, out + err
    assert out.startswith("EXISTS"), f"the second `ck worktree integration demo` must report EXISTS: {out}"
    assert_ck_hooks_installed(root)
    detach_integration(root)
    before = integ_ref(root)
    proc = h.raw(root, "update-ref", PROTECTED_REF, tip(root, 1))
    assert_refused(root, proc, PROTECTED_REF, before, "raw update-ref after the hooks were reinstalled")


def test_tc75c_run_start_also_installs_the_hooks(tmp_path):
    root = h.hooks_repo(tmp_path)
    remove_ck_hooks(root)
    code, obj, err = h.ck_json(root, "worktree", "start", "demo", "--json")
    assert code == 0 and obj is not None, f"ck worktree start demo --json failed ({code}): {err}"
    status = hook_status_of(obj)
    assert status["installed"] is True and status["active"] is True, status
    assert_ck_hooks_installed(root)
    detach_integration(root)
    before = integ_ref(root)
    proc = h.raw(root, "update-ref", PROTECTED_REF, tip(root, 1))
    assert_refused(root, proc, PROTECTED_REF, before, "raw update-ref after run start")


# --------------------------------------------------------------------------- TC-76, TC-77


def test_tc76_no_verify_merge_of_a_protected_branch_is_refused(tmp_path):
    root = h.hooks_repo(tmp_path)
    wt = h.integration_worktree(root)
    before = integ_ref(root)
    head_before = h.ref(wt, "HEAD")
    proc = h.raw(wt, "merge", "--no-ff", "--no-verify", "--no-edit", "feat/demo-task-1")
    assert_refused(root, proc, PROTECTED_REF, before, "git merge --no-ff --no-verify of the protected task 1")
    h.raw(wt, "merge", "--abort")
    assert h.ref(wt, "HEAD") == head_before
    assert not (wt / ".claude" / "CLAUDE.md").exists(), ".claude/CLAUDE.md is in the integration worktree"
    tree = h.raw(root, "ls-tree", "-r", "--name-only", PROTECTED_REF).stdout.splitlines()
    assert ".claude/CLAUDE.md" not in tree


def _by_update_ref(root: Path, wt: Path, target: str) -> subprocess.CompletedProcess:
    h.git(wt, "checkout", "-q", "--detach")
    return h.raw(root, "update-ref", PROTECTED_REF, target)


def _by_branch_f(root: Path, wt: Path, target: str) -> subprocess.CompletedProcess:
    h.git(wt, "checkout", "-q", "--detach")
    return h.raw(root, "branch", "-f", "integrate/demo", target)


def _by_reset_hard(root: Path, wt: Path, target: str) -> subprocess.CompletedProcess:
    return h.raw(wt, "reset", "--hard", target)


def _by_push_dot(root: Path, wt: Path, target: str) -> subprocess.CompletedProcess:
    h.git(wt, "checkout", "-q", "--detach")
    return h.raw(root, "push", ".", f"{target}:{PROTECTED_REF}")


def _by_commit(root: Path, wt: Path, target: str) -> subprocess.CompletedProcess:
    (wt / ".claude").mkdir(exist_ok=True)
    (wt / ".claude" / "x").write_text("x\n")
    h.git(wt, "add", "--", ".claude/x")
    return h.raw(wt, "commit", "-qm", "add .claude/x")


def _by_cherry_pick(root: Path, wt: Path, target: str) -> subprocess.CompletedProcess:
    return h.raw(wt, "cherry-pick", target)


def _by_merge_ff_only(root: Path, wt: Path, target: str) -> subprocess.CompletedProcess:
    return h.raw(wt, "merge", "--ff-only", target)


MOVERS = {
    "update-ref": _by_update_ref,
    "branch -f": _by_branch_f,
    "reset --hard": _by_reset_hard,
    "push .": _by_push_dot,
    "commit": _by_commit,
    "cherry-pick": _by_cherry_pick,
    "merge --ff-only": _by_merge_ff_only,
}


@pytest.mark.parametrize("how", list(MOVERS))
def test_tc77_every_plain_git_way_to_move_the_integration_ref_to_a_protected_commit_is_refused(tmp_path, how):
    root = h.hooks_repo(tmp_path)
    assert_ck_hooks_installed(root)
    wt = h.integration_worktree(root)
    before = integ_ref(root)
    proc = MOVERS[how](root, wt, tip(root, 1))
    assert_refused(root, proc, PROTECTED_REF, before, f"git {how}")


def test_tc77_control_the_same_movers_pass_for_a_commit_without_protected_changes(tmp_path):
    """The refusals above come from the hook: with hooks installed, a plain update to a non-protected commit passes."""
    root = h.hooks_repo(tmp_path)
    assert_ck_hooks_installed(root)
    detach_integration(root)
    target = tip(root, 3)
    proc = h.raw(root, "update-ref", PROTECTED_REF, target)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert integ_ref(root) == target


# --------------------------------------------------------------------------- TC-78, TC-79, TC-80, TC-81


def test_tc78_the_branchs_own_allow_list_covers_commands_but_not_claude_md(tmp_path):
    root = h.hooks_repo(tmp_path)
    allow_paths_on_integration(root, ALLOW_COMMANDS)
    wt = h.integration_worktree(root)
    before = integ_ref(root)
    proc = h.raw(wt, "merge", "--no-ff", "--no-edit", "feat/demo-task-1")
    assert_refused(root, proc, PROTECTED_REF, before, "raw merge of Task 1 (.claude/CLAUDE.md)")
    h.raw(wt, "merge", "--abort")
    ok = h.raw(wt, "merge", "--no-ff", "--no-edit", "feat/demo-task-2")
    assert ok.returncode == 0, f"raw merge of Task 2 (only .claude/commands/x.md) must pass:\n{ok.stdout}{ok.stderr}"
    assert integ_ref(root) != before
    assert h.raw(root, "cat-file", "-e", f"{PROTECTED_REF}:.claude/commands/x.md").returncode == 0


def test_tc79a_replaying_ck_state_after_an_allowed_landing_lets_no_other_update_through(tmp_path):
    root = h.hooks_repo(tmp_path)
    allow_paths_on_integration(root, ALLOW_COMMANDS)
    code, obj, err = h.ck_json(root, "worktree", "land", "demo", "2", "--into", "integrate/demo", "--keep", "--json")
    assert code == 0 and states_of(obj)["2"] == "MERGED", f"{code} {obj} {err}"
    landed = integ_ref(root)
    after = h.snapshot_ck_state(root)
    h.restore_ck_state(root, after)
    detach_integration(root)
    proc = h.raw(root, "update-ref", PROTECTED_REF, tip(root, 1))
    assert_refused(root, proc, PROTECTED_REF, landed, "raw update to another protected commit after the landing")
    h.restore_ck_state(root, after)
    detach_integration(root)
    again = h.raw(root, "update-ref", PROTECTED_REF, tip(root, 1))
    assert_refused(root, again, PROTECTED_REF, landed, "the same raw update with ck's state replayed")


def test_tc79b_a_used_token_does_not_let_the_same_update_be_replayed_with_raw_git(tmp_path):
    root = h.hooks_repo(tmp_path)
    before = integ_ref(root)
    code, text = approve_in_pty(root, "demo", "1", "integrate/demo")
    assert code == 0, f"approval through a pseudo-terminal failed ({code}):\n{text}"
    approved = integ_ref(root)
    assert approved != before and h.raw(root, "cat-file", "-e", f"{approved}:.claude/CLAUDE.md").returncode == 0
    state = h.snapshot_ck_state(root)  # right after the landing: the used token and ck's records
    # Replay ck's state first (it also holds the ref and the worktree HEAD), then put the ref back the way ck would
    # (a direct ref write) and detach, so only the used token and ck's records remain from the landing.
    h.restore_ck_state(root, state)
    (common_dir(root) / "refs" / "heads" / "integrate" / "demo").write_text(before + "\n")
    assert integ_ref(root) == before
    detach_integration(root)
    proc = h.raw(root, "update-ref", PROTECTED_REF, approved)
    assert_refused(root, proc, PROTECTED_REF, before, "replaying the approved update with plain git")


def test_tc79c_a_landing_into_one_integration_branch_is_no_token_for_another(tmp_path):
    root = h.hooks_repo(tmp_path)
    for name in ("a", "b"):
        code, out, err = h.ck_text(root, "worktree", "integration", name, "--base", "main")
        assert code == 0, out + err
    allow_paths_on_integration(root, ALLOW_COMMANDS, spec="a")
    code, obj, err = h.ck_json(root, "worktree", "land", "demo", "2", "--into", "integrate/a", "--keep", "--json")
    assert code == 0 and states_of(obj)["2"] == "MERGED", f"{code} {obj} {err}"
    detach_integration(root, "b")
    before = integ_ref(root, "b")
    proc = h.raw(root, "update-ref", "refs/heads/integrate/b", tip(root, 1))
    assert_refused(root, proc, "refs/heads/integrate/b", before, "raw update of integrate/b after a landing into integrate/a")


@pytest.mark.parametrize("name", ["refs/heads/feature", "refs/tags/integrate/x", "refs/heads/integrated/x",
                                  "refs/heads/feat/integrate/x"])
def test_tc80_refs_outside_integrate_pass_without_the_protected_check(tmp_path, name):
    root = h.hooks_repo(tmp_path)
    assert_ck_hooks_installed(root)
    target = tip(root, 1)
    proc = h.raw(root, "update-ref", name, target)
    assert proc.returncode == 0, f"update-ref {name} must pass:\n{proc.stdout}{proc.stderr}"
    assert h.raw(root, "rev-parse", name).stdout.strip() == target


def test_tc81_a_protected_merge_is_refused_before_the_commit_exists_with_a_readable_message(tmp_path):
    root = h.hooks_repo(tmp_path)
    wt = h.integration_worktree(root)
    before = integ_ref(root)
    head_before = h.ref(wt, "HEAD")
    proc = h.raw(wt, "merge", "--no-ff", "--no-edit", "feat/demo-task-1")
    assert_refused(root, proc, PROTECTED_REF, before, "git merge --no-ff of the protected task 1 (hooks enabled)")
    assert h.ref(wt, "HEAD") == head_before, "a commit exists"
    err = proc.stdout + proc.stderr
    assert ".claude/CLAUDE.md" in err, err
    assert "land.allow_paths" in err or "approve" in err, f"the message must say how to approve: {err}"


# --------------------------------------------------------------------------- TC-82, TC-83


def test_tc82_ck_worktree_merge_gives_protected_with_exit_3(tmp_path):
    root = h.hooks_repo(tmp_path)
    before = integ_ref(root)
    code, obj, err = h.ck_json(root, "worktree", "merge", "demo", "1", "--into", "integrate/demo", "--json")
    assert obj is not None, f"no JSON: {err}"
    item = obj[0] if isinstance(obj, list) else obj["tasks"][0]
    assert item["state"] == "PROTECTED", item
    assert ".claude/CLAUDE.md" in item["detail"], item
    assert code == 3, f"exit {code}"
    assert integ_ref(root) == before


@pytest.mark.parametrize("cmd", ["land", "merge"])
@pytest.mark.parametrize("task,allow", [("2", True), ("3", False)])
def test_tc83_allowed_and_plain_landings_succeed_with_the_hooks_installed(tmp_path, cmd, task, allow):
    root = h.hooks_repo(tmp_path)
    if allow:
        allow_paths_on_integration(root, ALLOW_COMMANDS)
    assert_ck_hooks_installed(root)
    before = integ_ref(root)
    code, obj, err = h.ck_json(root, "worktree", cmd, "demo", task, "--into", "integrate/demo", "--json")
    assert obj is not None, f"no JSON ({code}): {err}"
    assert states_of(obj)[task] == "MERGED", f"{cmd} task {task}: {states_of(obj)} {err}"
    assert code == 0
    after = integ_ref(root)
    assert after != before, "the ref did not move"
    assert h.raw(root, "merge-base", "--is-ancestor", before, after).returncode == 0


# --------------------------------------------------------------------------- TC-84


def test_tc84a_approval_with_a_pipe_as_stdin_is_refused(tmp_path):
    root = h.hooks_repo(tmp_path)
    before = integ_ref(root)
    for typed in ("yes\n", "ABC123\n", "\n"):
        out = subprocess.run([*h.CK_ARGV, "worktree", "approve", "demo", "1", "--into", "integrate/demo"], cwd=root,
                             env=h._ck_env(root, None), input=typed, capture_output=True, text=True, timeout=120,
                             start_new_session=True)  # no controlling terminal to fall back to
        assert "No such command" not in out.stderr, "ck worktree approve does not exist"
        assert out.returncode == 2, f"approval through a pipe succeeded:\n{out.stdout}{out.stderr}"
        assert integ_ref(root) == before, "approval through a pipe moved the ref"
        assert h.raw(root, "cat-file", "-e", f"{PROTECTED_REF}:.claude/CLAUDE.md").returncode != 0


def test_tc84b_approval_typed_at_a_pseudo_terminal_lands_task_1_with_a_token(tmp_path):
    root = h.hooks_repo(tmp_path)
    before = integ_ref(root)
    code, text = approve_in_pty(root, "demo", "1", "integrate/demo")
    assert code == 0, f"approval at a pseudo-terminal failed ({code}):\n{text}"
    after = integ_ref(root)
    assert after != before, f"the ref did not move:\n{text}"
    assert h.raw(root, "cat-file", "-e", f"{PROTECTED_REF}:.claude/CLAUDE.md").returncode == 0
    assert h.raw(root, "merge-base", "--is-ancestor", before, after).returncode == 0


def test_tc84b_a_wrong_code_at_the_terminal_lands_nothing(tmp_path):
    root = h.hooks_repo(tmp_path)
    before = integ_ref(root)
    code, text = approve_in_pty(root, "demo", "1", "integrate/demo", answer="WRONG1")
    assert code != 0, f"a wrong code was accepted:\n{text}"
    assert integ_ref(root) == before


# --------------------------------------------------------------------------- TC-85

SCENARIOS = ["git-hooks-dir", "hooks-path", "pre-commit-framework"]


def _write_hook(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _prepare_project_hooks(root: Path, scenario: str, marker: Path, nope: Path) -> None:
    """The repository's own hooks, in place before ck installs its own (ck's are removed first)."""
    remove_ck_hooks(root)
    h.raw(root, "config", "--unset", "core.hooksPath")
    wt = h.integration_worktree(root)
    if scenario == "git-hooks-dir":
        _write_hook(common_dir(root) / "hooks" / "pre-merge-commit",
                    f'if [ -e {nope} ]; then echo "project says no" >&2; exit 1; fi\n')
        _write_hook(common_dir(root) / "hooks" / "reference-transaction", f"cat >/dev/null; echo ref >> {marker}\n")
    elif scenario == "hooks-path":
        for base in (root, wt):  # a relative hooks path resolves in whichever worktree git runs
            _write_hook(base / ".githooks" / "reference-transaction", f"cat >/dev/null; echo ref >> {marker}\n")
        h.git(root, "config", "core.hooksPath", ".githooks")
    else:
        _write_hook(common_dir(root) / "hooks" / "pre-commit",
                    "# File generated by pre-commit: https://pre-commit.com\n"
                    f"echo pre-commit >> {marker}\nexit 0\n")


def _marker_lines(marker: Path) -> int:
    return len(marker.read_text().splitlines()) if marker.exists() else 0


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_tc85_ck_chains_to_the_repositorys_own_hooks(tmp_path, scenario):
    root = h.hooks_repo(tmp_path)
    marker, nope = tmp_path / "marker.txt", tmp_path / "NOPE"
    _prepare_project_hooks(root, scenario, marker, nope)
    reinstall_hooks(root)
    wt = h.integration_worktree(root)
    before = integ_ref(root)

    # A non-protected merge: the project's hooks run, and its refusal still refuses.
    if scenario == "git-hooks-dir":
        nope.write_text("")
        blocked = h.raw(wt, "merge", "--no-ff", "--no-edit", "feat/demo-task-3")
        assert_refused(root, blocked, PROTECTED_REF, before, "a merge the project's own pre-merge-commit refuses")
        assert "project says no" in blocked.stderr + blocked.stdout
        h.raw(wt, "merge", "--abort")
        nope.unlink()
    runs_before = _marker_lines(marker)
    plain = h.raw(wt, "merge", "--no-ff", "--no-edit", "feat/demo-task-3")
    assert plain.returncode == 0, f"a non-protected merge must pass:\n{plain.stdout}{plain.stderr}"
    assert integ_ref(root) != before
    if scenario == "hooks-path":
        assert _marker_lines(marker) > runs_before, "the project's reference-transaction hook no longer runs"

    # A protected merge is still refused by ck.
    after_plain = integ_ref(root)
    protected = h.raw(wt, "merge", "--no-ff", "--no-verify", "--no-edit", "feat/demo-task-1")
    assert_refused(root, protected, PROTECTED_REF, after_plain, "a protected merge with the project's hooks chained")
    h.raw(wt, "merge", "--abort")

    # pre-commit still runs on commit (and the project's hook marker grows).
    if scenario == "pre-commit-framework":
        runs = _marker_lines(marker)
        (wt / "src" / "b.py").write_text("B = 1\n")
        h.git(wt, "add", "--", "src/b.py")
        done = h.raw(wt, "commit", "-qm", "b")
        assert done.returncode == 0, done.stdout + done.stderr
        assert _marker_lines(marker) > runs, "the pre-commit hook did not run"


# --------------------------------------------------------------------------- TC-86


@pytest.fixture
def unwritable_hooks_location(tmp_path):
    """The hooks location made read-only before install: no hooks yet, ck's directory and the config read-only."""
    if os.geteuid() == 0:
        pytest.skip("chmod read-only does not bind root")
    root = h.hooks_repo(tmp_path)
    remove_ck_hooks(root)
    h.raw(root, "config", "--unset", "core.hooksPath")
    common = common_dir(root)
    for sub in ("ck", "ck/demo", "ck/tokens"):  # writable records stay writable; only the hooks location is locked
        (common / sub).mkdir(parents=True, exist_ok=True)
    (common / "ck" / "hooks").mkdir(exist_ok=True)
    targets = [common / "ck" / "hooks", common / "config"]
    saved = [(p, stat.S_IMODE(p.stat().st_mode)) for p in targets]
    for path, mode in saved:
        path.chmod(mode & ~0o222)
    try:
        yield root
    finally:
        for path, mode in saved:
            path.chmod(mode)


REASON = re.compile(r"(?i)permission denied|read-only|not writable|cannot write|can't write|operation not permitted")


def test_tc86_integration_reports_that_hooks_are_not_installed_with_the_reason(unwritable_hooks_location):
    root = unwritable_hooks_location
    code, out, err = h.ck_text(root, "worktree", "integration", "demo", "--json")
    assert out.strip(), f"no output ({code}): {err}"
    doc = json.loads(out)
    status = hook_status_of(doc)
    assert status["installed"] is False, status
    assert status["problems"], f"no reason for the failed install: {status}"
    assert REASON.search(json.dumps(doc)), f"the report must name the reason: {doc}"
    assert not (hooks_dir(root) / "reference-transaction").exists(), "no hook may be written (never half-install)"
    assert not any(hooks_dir(root).iterdir()), "no hook may be written (never half-install)"
    assert h.raw(root, "config", "--get", "core.hooksPath").stdout.strip() == "", "core.hooksPath must stay unset"


def test_tc86_run_start_reports_that_hooks_are_not_installed_with_the_reason(unwritable_hooks_location):
    root = unwritable_hooks_location
    code, obj, err = h.ck_json(root, "worktree", "start", "demo", "--json")
    assert code == 0, f"run start must exit 0 when only the hooks cannot be installed ({code}): {err}"
    status = hook_status_of(obj)
    assert status["installed"] is False, status
    assert status["problems"], status
    assert REASON.search(json.dumps(obj)), f"the report must name the reason: {obj}"


# --------------------------------------------------------------------------- TC-87, TC-88


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod read-only does not bind root")
def test_tc87_hooks_stay_effective_and_tokens_work_with_read_only_git_metadata(tmp_path):
    root = h.hooks_repo(tmp_path)
    allow_paths_on_integration(root, ALLOW_COMMANDS)
    wt = h.integration_worktree(root)
    before = integ_ref(root)
    with h.readonly_git_metadata(root):
        merge = h.raw(wt, "merge", "--no-ff", "--no-verify", "--no-edit", "feat/demo-task-1")
        assert_refused(root, merge, PROTECTED_REF, before, "raw merge --no-verify of Task 1 under read-only metadata")
        # The merge can die on read-only worktree metadata before any hook runs; the ref update reaches the hook.
        assert_hook_refusal(root, lambda a: h.raw(root, *a), PROTECTED_REF, before,
                            "raw update-ref under read-only metadata")
        # ck's own landing needs the integration worktree's gitdir (ORIG_HEAD, index) writable: that gitdir is
        # unlocked for the landing only; the hooks, config and the other worktrees stay read-only.
        gitdir = Path(h.raw(wt, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip())
        gitdir.chmod(gitdir.stat().st_mode | 0o200)
        code, obj, err = h.ck_json(root, "worktree", "land", "demo", "2", "--into", "integrate/demo", "--keep", "--json")
        assert obj is not None, f"no JSON ({code}): {err}"
        assert states_of(obj)["2"] == "MERGED", f"an allowed landing must succeed under read-only metadata: {obj} {err}"
        assert code == 0
    assert integ_ref(root) != before


def sandbox_ck(tmp_path: Path, root: Path) -> Dict[str, str]:
    """Make `ck` runnable under the real sandbox, where the checkout's venv (under ~) is hidden.

    A copy of claude_kiro and click goes inside the project (readable there) and a `ck` shim on PATH runs them with
    the system python, so ck's hooks find `ck` on PATH as they do in a real run. Returns the env for `sandboxed`."""
    import click
    import claude_kiro

    lib = root / ".ck" / "sandbox-lib"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(Path(claude_kiro.__file__).parent, lib / "claude_kiro", ignore=ignore)
    shutil.copytree(Path(click.__file__).parent, lib / "click", ignore=ignore)
    shim = root / ".ck" / "sandbox-bin"
    shim.mkdir(parents=True, exist_ok=True)
    python = sys._base_executable
    (shim / "ck").write_text(f'#!/bin/sh\nPYTHONPATH={lib} exec {python} -m claude_kiro.cli "$@"\n')
    (shim / "ck").chmod(0o755)
    return {"PATH": str(shim) + os.pathsep + "/usr/bin:/bin"}


def test_tc88_a_raw_protected_merge_is_refused_inside_the_real_sandbox(tmp_path):
    root = h.hooks_repo(tmp_path)
    assert_ck_hooks_installed(root)
    wt = h.integration_worktree(root)
    before = integ_ref(root)
    env = sandbox_ck(tmp_path, root)  # one copy of the ck library per repo: copytree refuses an existing target
    proc = h.sandboxed(["git", "-C", str(wt), "merge", "--no-ff", "--no-verify", "--no-edit", "feat/demo-task-1"],
                       root, root / ".ck" / "worktrees", env=env)
    assert_refused(root, proc, PROTECTED_REF, before, "sandboxed git merge --no-verify of Task 1")
    run = lambda a: h.sandboxed(["git", "-C", str(root), *a], root, root / ".ck" / "worktrees", env=env)
    assert_hook_refusal(root, run, PROTECTED_REF, before, "sandboxed raw update-ref of the protected commit")
    ok = run(["update-ref", "refs/heads/feature", tip(root, 3)])  # control: not protected, so the hook lets it pass
    assert ok.returncode == 0, f"a sandboxed update to a non-protected ref must pass:\n{ok.stdout}{ok.stderr}"


# --------------------------------------------------------------------------- TC-189


def _run_env(tmp_path: Path, claude_bin: Path) -> Dict[str, str]:
    """PATH with a fake claude plus stand-ins for the sandbox tools `ck run` checks for, and an empty HOME."""
    tools = tmp_path / "sandbox-tools"
    tools.mkdir()
    for tool in ("bwrap", "socat"):
        (tools / tool).write_text("#!/bin/sh\nexit 0\n")
        (tools / tool).chmod(0o755)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {"PATH": os.pathsep.join([str(claude_bin), str(tools), os.environ.get("PATH", "")]), "HOME": str(home)}


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod read-only does not bind root")
def test_tc189_ck_run_installs_the_hooks_before_launch_and_they_hold_in_read_only_metadata(tmp_path):
    root = h.hooks_repo(tmp_path)
    remove_ck_hooks(root)
    h.raw(root, "config", "--unset", "core.hooksPath")
    claude_bin = h.fake_claude(tmp_path)
    env = _run_env(tmp_path, claude_bin)
    out = h.ck_run(root, "run", "demo", env=env)
    assert out.returncode == 0, f"ck run demo failed ({out.returncode}):\n{out.stdout}{out.stderr}"
    records = h.fake_claude_records(claude_bin)
    assert records, f"the fake claude never started:\n{out.stdout}{out.stderr}"
    first = records[0]
    assert first["hooksPath"], "core.hooksPath was not set when claude started: the hooks were installed too late"
    assert os.path.realpath(os.path.join(root, first["hooksPath"])) == os.path.realpath(hooks_dir(root))
    deny = first["settings"]["sandbox"]["filesystem"]["denyWrite"]
    common = common_dir(root)
    resolved = {os.path.realpath(os.path.expanduser(p)) for p in deny}
    assert os.path.realpath(common / "ck" / "hooks") in resolved, deny
    assert os.path.realpath(common / "ck" / "key") in resolved, deny
    assert not any(p.rstrip("/").endswith("ck/tokens") for p in deny), "tokens must stay writable"
    assert (common / "ck" / "key").is_file()

    wt = h.integration_worktree(root)
    before = integ_ref(root)
    with h.readonly_git_metadata(root):
        code, obj, err = h.ck_json(root, "worktree", "start", "demo", "--json")
        assert code == 0 and obj is not None, f"ck worktree start failed in read-only metadata ({code}): {err}"
        status = hook_status_of(obj)
        assert status["installed"] is True and status["active"] is True, status
        proc = h.raw(wt, "merge", "--no-ff", "--no-verify", "--no-edit", "feat/demo-task-1")
        assert_refused(root, proc, PROTECTED_REF, before, "raw merge --no-verify under read-only metadata after ck run")
        assert_hook_refusal(root, lambda a: h.raw(root, *a), PROTECTED_REF, before,
                            "raw update-ref under read-only metadata after ck run")
        ok = h.raw(root, "update-ref", "refs/heads/feature", tip(root, 3))  # control: the refusal is the hook's
        assert ok.returncode == 0, ok.stdout + ok.stderr


def test_tc189_the_same_start_and_merge_inside_the_real_sandbox(tmp_path):
    root = h.hooks_repo(tmp_path)
    remove_ck_hooks(root)
    h.raw(root, "config", "--unset", "core.hooksPath")
    claude_bin = h.fake_claude(tmp_path)
    out = h.ck_run(root, "run", "demo", env=_run_env(tmp_path, claude_bin))
    assert out.returncode == 0, f"ck run demo failed ({out.returncode}):\n{out.stdout}{out.stderr}"
    worktrees = root / ".ck" / "worktrees"
    wt = h.integration_worktree(root)
    before = integ_ref(root)
    sb_env = sandbox_ck(tmp_path, root)
    start = h.sandboxed(["ck", "worktree", "start", "demo", "--json"], root, worktrees, env=sb_env)
    assert start.returncode == 0, start.stdout + start.stderr
    status = hook_status_of(json.loads(start.stdout))
    assert status["installed"] is True and status["active"] is True, status
    proc = h.sandboxed(["git", "-C", str(wt), "merge", "--no-ff", "--no-verify", "--no-edit", "feat/demo-task-1"],
                       root, worktrees, env=sb_env)
    assert_refused(root, proc, PROTECTED_REF, before, "sandboxed raw merge --no-verify after ck run")
    run = lambda a: h.sandboxed(["git", "-C", str(root), *a], root, worktrees, env=sb_env)
    assert_hook_refusal(root, run, PROTECTED_REF, before, "sandboxed raw update-ref after ck run")
    ok = run(["update-ref", "refs/heads/feature", tip(root, 3)])
    assert ok.returncode == 0, ok.stdout + ok.stderr


# --------------------------------------------------------------------------- TC-194


def test_tc194_a_husky_prepare_that_resets_hooks_path_is_repaired_and_chained(tmp_path):
    root = h.hooks_repo(tmp_path)
    marker = tmp_path / "husky-marker.txt"
    wt = h.integration_worktree(root)
    # The project's husky hook lives, untracked as husky's own `_` directory is, inside the integration worktree.
    _write_hook(wt / ".husky" / "_" / "pre-merge-commit", f"echo husky >> {marker}\n")
    config = {"install": "git config core.hooksPath .husky/_"}
    commit_on_integration_without_hooks(root, {"specs/ck.json": json.dumps(config) + "\n"})
    assert_ck_hooks_installed(root)
    before = integ_ref(root)

    code, obj, err = h.ck_json(root, "worktree", "land", "demo", "3", "--into", "integrate/demo", "--gate",
                               "--install", "--json")
    assert obj is not None, f"no JSON ({code}): {err}"
    assert states_of(obj).get("3") == "MERGED", f"land of Task 3 did not merge: {obj} {err}"

    assert_ck_hooks_installed(root)  # core.hooksPath points at ck's hooks again
    chain = json.loads((hooks_dir(root) / "chain.json").read_text())
    assert ".husky/_" in json.dumps(chain), f"chain.json does not name .husky/_: {chain}"
    reports = [v for v in find_key(obj, "hooks") if v]
    assert reports and ".husky/_" in json.dumps(reports), f"the land result does not report the re-install: {obj}"

    after_land = integ_ref(root)
    assert after_land != before
    runs = _marker_lines(marker)
    proc = h.raw(wt, "merge", "--no-ff", "--no-edit", "feat/demo-task-1")
    assert_refused(root, proc, PROTECTED_REF, after_land, "a raw protected merge after the husky reset")
    h.raw(wt, "merge", "--abort")
    assert _marker_lines(marker) > runs, "the husky hook no longer runs through ck's chain"
    nv = h.raw(wt, "merge", "--no-ff", "--no-verify", "--no-edit", "feat/demo-task-1")
    assert_refused(root, nv, PROTECTED_REF, after_land, "a raw --no-verify protected merge after the husky reset")
    h.raw(wt, "merge", "--abort")


def test_tc85_integration_json_names_the_chain_target_chained_to_in_camel_case(tmp_path):
    """The design's HookStatus JSON key, the one `run start` and `ck-audit/1` consumers read."""
    root = h.hooks_repo(tmp_path)
    code, obj, err = h.ck_json(root, "worktree", "integration", "demo", "--json")
    assert obj is not None, f"no JSON ({code}): {err}"
    status = hook_status_of(obj)
    assert "chainedTo" in status and "chained_to" not in status, status
