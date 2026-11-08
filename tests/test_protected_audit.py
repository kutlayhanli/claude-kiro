"""TC-96, TC-97, TC-98a, TC-99, TC-100: the run-end protected-file audit (`ck worktree audit demo --json`).

Real git, real landings, the real ck CLI. Bypass writes go straight to the ref file, as an agent would."""

import json
import os
import pty
import re
import select
import signal
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

import pytest

import unattended_helpers as h

# The standard git hook names. ck's hooks directory holds one script per name, beside non-script records
# (chain.json, the recorded interpreter) that the hook-tamper cases must leave alone.
GIT_HOOK_NAMES = frozenset({
    "applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit", "pre-merge-commit",
    "prepare-commit-msg", "commit-msg", "post-commit", "pre-rebase", "post-checkout", "post-merge",
    "pre-push", "pre-receive", "update", "proc-receive", "post-receive", "post-update",
    "reference-transaction", "push-to-checkout", "pre-auto-gc", "post-rewrite", "sendemail-validate",
    "fsmonitor-watchman", "p4-changelist", "p4-prepare-changelist", "p4-post-changelist", "p4-pre-submit",
    "post-index-change",
})


def common_dir(root: Path) -> Path:
    return Path(h.raw(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())


def hooks_dir(root: Path) -> Path:
    return Path(h.raw(root, "rev-parse", "--path-format=absolute", "--git-path", "hooks").stdout.strip())


def hook_files(root: Path) -> List[Path]:
    d = hooks_dir(root)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and not p.name.endswith(".sample"))


def hook_scripts(paths: List[Path]) -> List[Path]:
    return [p for p in paths if p.name in GIT_HOOK_NAMES]


def audit_repo(tmp_path: Path, *, allow: Optional[List[str]] = None, readonly_install: bool = False):
    """A repo with .claude files, .mcp.json and the integration worktree. Returns (root, ck_hooks, ck_state)
    where ck_hooks are the hook files ck installed and ck_state the files `ck worktree integration` wrote."""
    root = h.init_repo(tmp_path / "repo")
    (root / ".gitignore").write_text(".venv/\n.ck/worktrees/\n")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("A = 1\n")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text('{"permissions": {}}\n')
    (root / ".claude" / "CLAUDE.md").write_text("# base\n")
    (root / ".mcp.json").write_text('{"mcpServers": {}}\n')
    spec = root / "specs" / "demo"
    spec.mkdir(parents=True)
    (spec / "tasks.md").write_text("# Implementation Tasks: Demo\n\n## Task Breakdown\n\n" + "".join(
        f"### Task {n}: task {n}\n**Status:** Not Started\n**Track:** impl\n**Dependencies:** None\n\n"
        for n in range(1, 4)))
    if allow is not None:
        (root / "specs" / "ck.json").write_text(json.dumps({"land": {"allow_paths": allow}}))
    h.commit_all(root, "base")
    before = h.snapshot_ck_state(root)
    hooks_before = set(hook_files(root))
    if readonly_install:
        with h.readonly_git_metadata(root):
            code, _, err = h.ck_text(root, "worktree", "integration", "demo")
        assert code == 0, f"integration must still succeed when hooks cannot be installed: {err}"
    else:
        code, _, err = h.ck_text(root, "worktree", "integration", "demo")
        assert code == 0, err
    after = h.snapshot_ck_state(root)
    ck_hooks = [p for p in hook_files(root) if p not in hooks_before]
    changed = [k for k in after if before.get(k) != after[k]]
    return root, ck_hooks, changed


def audit(root: Path):
    code, obj, err = h.ck_json(root, "worktree", "audit", "demo", "--json")
    assert obj is not None, f"ck worktree audit printed no JSON (exit {code}): {err}"
    assert obj.get("schema") == "ck-audit/1", obj
    return code, obj


def bypass_commit(root: Path, edits: Dict[str, Optional[str]]) -> None:
    """Move integrate/demo to a new commit by writing the ref file directly (no git ref command, no hook)."""
    head = h.ref(root, "integrate/demo")
    idx = root.parent / "bypass.index"
    if idx.exists():
        idx.unlink()
    env = dict(os.environ, GIT_INDEX_FILE=str(idx), GIT_AUTHOR_NAME="x", GIT_AUTHOR_EMAIL="x@x",
               GIT_COMMITTER_NAME="x", GIT_COMMITTER_EMAIL="x@x")

    def g(*args: str, stdin: Optional[str] = None) -> str:
        return subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True, text=True,
                              input=stdin).stdout.strip()

    g("read-tree", head)
    for rel, content in edits.items():
        if content is None:
            g("update-index", "--force-remove", rel)
        else:
            oid = g("hash-object", "-w", "--stdin", stdin=content)
            g("update-index", "--add", "--cacheinfo", f"100644,{oid},{rel}")
    tree = g("write-tree")
    new = g("commit-tree", tree, "-p", head, "-m", "bypass")
    ref_file = common_dir(root) / "refs" / "heads" / "integrate" / "demo"
    ref_file.parent.mkdir(parents=True, exist_ok=True)
    ref_file.write_text(new + "\n")
    assert h.ref(root, "integrate/demo") == new


def drift_map(obj) -> Dict[str, str]:
    return {d["file"]: d["change"] for d in obj["drift"]}


# --- TC-98a ---------------------------------------------------------------------


def test_tc98a_clean_run_reports_no_drift(tmp_path):
    root, _, _ = audit_repo(tmp_path)
    code, obj = audit(root)
    assert code == 0, obj
    assert obj["clean"] is True and obj["complete"] is True
    assert obj["drift"] == []
    assert obj["hooks"]["installed"] is True and obj["hooks"]["active"] is True and obj["hooks"]["intact"] is True


# --- TC-96 ----------------------------------------------------------------------


def test_tc96_ref_file_bypass_lists_exactly_the_three_changes(tmp_path):
    root, _, _ = audit_repo(tmp_path)
    bypass_commit(root, {".claude/a": "new\n", ".mcp.json": '{"mcpServers": {"evil": {}}}\n',
                         ".claude/settings.json": None})
    code, obj = audit(root)
    assert code == 1, obj
    assert obj["clean"] is False
    assert drift_map(obj) == {".claude/a": "added", ".mcp.json": "changed", ".claude/settings.json": "deleted"}
    assert len(obj["drift"]) == 3


# --- TC-97 ----------------------------------------------------------------------


def _assert_hooks_reported(code, obj):
    hooks = obj["hooks"]
    assert code == 1, obj
    assert obj["clean"] is False
    assert not (hooks["installed"] and hooks["active"] and hooks["intact"]), hooks
    assert hooks["problems"], hooks


def test_tc97_hooks_deleted(tmp_path):
    root, ck_hooks, _ = audit_repo(tmp_path)
    scripts = hook_scripts(ck_hooks)
    assert scripts, f"ck worktree integration installed no hook scripts: {ck_hooks}"
    for p in scripts:
        p.unlink()
    _assert_hooks_reported(*audit(root))


def test_tc97_hook_content_altered(tmp_path):
    root, ck_hooks, _ = audit_repo(tmp_path)
    target = [p for p in ck_hooks if p.name == "reference-transaction"]
    assert target, f"ck worktree integration installed no reference-transaction hook: {ck_hooks}"
    others = {p: p.read_bytes() for p in ck_hooks if p != target[0]}
    target[0].write_text(target[0].read_text() + "\n# tampered\nexit 0\n")
    assert all(p.read_bytes() == b for p, b in others.items())  # only the one script changed
    code, obj = audit(root)
    _assert_hooks_reported(code, obj)
    assert obj["hooks"]["intact"] is False


def test_tc97_hooks_path_repointed(tmp_path):
    root, _, _ = audit_repo(tmp_path)
    h.git(root, "config", "core.hooksPath", ".husky/_")
    code, obj = audit(root)
    _assert_hooks_reported(code, obj)
    assert obj["hooks"]["active"] is False
    assert any("hooksPath" in p for p in obj["hooks"]["problems"]), obj["hooks"]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="chmod a-w does not stop root")
def test_tc97_hooks_never_installed(tmp_path):
    root, ck_hooks, _ = audit_repo(tmp_path, readonly_install=True)
    assert not ck_hooks
    _assert_hooks_reported(*audit(root))


# --- TC-99 ----------------------------------------------------------------------


def _task_branch(root: Path, n: int, files: Dict[str, Optional[str]]) -> None:
    code, _, err = h.ck_text(root, "worktree", "create", "demo", str(n), "--base", "integrate/demo")
    assert code == 0, err
    h._commit_in(h.task_worktree(root, n), files, f"task {n}")


def test_tc99_allow_list_landing_is_not_drift(tmp_path):
    root, _, _ = audit_repo(tmp_path, allow=[".claude/commands/**"])
    _task_branch(root, 2, {".claude/commands/x.md": "x\n"})
    before = h.ref(root, "integrate/demo")
    code, out, err = h.ck_text(root, "worktree", "land", "demo", "2", "--json")
    assert code == 0, (out, err)
    assert h.ref(root, "integrate/demo") != before, "the allow-listed landing did not move the ref"
    assert h.raw(root, "cat-file", "-e", "integrate/demo:.claude/commands/x.md").returncode == 0
    code, obj = audit(root)
    assert code == 0, obj
    assert obj["clean"] is True and obj["complete"] is True
    assert obj["drift"] == []


def _approve_in_pty(root: Path) -> int:
    """`ck worktree approve demo 1` at a real terminal: read the 6-character code, type it back."""
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(root)
            os.execvp(h.CK_ARGV[0], [*h.CK_ARGV, "worktree", "approve", "demo", "1"])
        finally:
            os._exit(127)
    seen = b""
    typed = False
    deadline = time.time() + 120
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 1.0)
        if ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                break
            seen += chunk
            if not typed:
                # the first 6-character token after the word "code", on the same line or a following one
                m = re.search(rb"code\W+(?:\S*\W+)??\b([A-Za-z0-9]{6})\b", seen, re.IGNORECASE)
                if m and m.end() < len(seen):  # the token is complete only once more output follows it
                    os.write(fd, m.group(1) + b"\n")
                    typed = True
    if not typed:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        os.waitpid(pid, 0)
        os.close(fd)
        pytest.fail(f"no code prompt seen: {seen!r}")
    # the code is typed; give the child the rest of the deadline, then stop it rather than hang
    while time.time() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            os.close(fd)
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.2)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    os.close(fd)
    pytest.fail(f"ck worktree approve did not finish: {seen!r}")


def test_tc99_approval_landing_is_not_drift(tmp_path):
    root, _, _ = audit_repo(tmp_path)
    _task_branch(root, 1, {".claude/CLAUDE.md": "# changed by task 1\n"})
    before = h.ref(root, "integrate/demo")
    assert _approve_in_pty(root) == 0
    assert h.ref(root, "integrate/demo") != before, "the approved landing did not move the ref"
    code, obj = audit(root)
    assert code == 0, obj
    assert obj["clean"] is True and obj["complete"] is True
    assert obj["drift"] == []


# --- TC-100 ---------------------------------------------------------------------


def _approved_record_candidates(root: Path, changed: List[str]) -> List[Path]:
    """Files `ck worktree integration` wrote that are neither git internals nor worktree checkouts."""
    common = common_dir(root)
    return [Path(key) for key in changed if (common / "ck" / "demo") in Path(key).parents]


@pytest.mark.parametrize("mode", ["deleted", "corrupt"])
def test_tc100_unreadable_approved_state_is_incomplete(tmp_path, mode):
    root, _, changed = audit_repo(tmp_path)
    records = _approved_record_candidates(root, changed)
    assert records, f"ck worktree integration wrote no approved-state record; changed: {changed}"
    code, obj = audit(root)
    assert code == 0 and obj["clean"] is True, obj
    for p in records:
        if mode == "deleted":
            p.unlink()
        else:
            p.write_bytes(b'{"truncated": [1, 2')
    code, obj = audit(root)
    assert code == 3, obj
    assert obj["complete"] is False and obj["clean"] is False
    assert any(str(x).startswith("approved state unreadable") for x in obj["problems"]), obj
