"""Red-team of Story 9 (protected-file hooks): TC-90 to TC-95, TC-182 and TC-190, plus further breaks.

Written from requirements 9.2, 9.6, 9.8, 9.11, 9.12, 10.1 and 10.2 only. Every refusal checks that the ref did not
move (and, for a creation, that the ref stays absent). Every case runs real git, the real ck CLI and, for the sandbox
cases, real bwrap (the case skips only when bwrap cannot start). Nothing mocks project code.

Each break found while attacking the requirements is a test:
- git quotes non-ASCII, space, quote and tab paths in `git diff --name-only`; the check must still see them (TC-90);
- a branch that allows itself in `specs/ck.json`, in one commit or two (TC-91);
- bypasses no hook can see: `-c core.hooksPath=/dev/null`, deleted or altered hooks, an unset `core.hooksPath`,
  direct ref file writes; the audit must report the drift and the hook state (TC-92);
- deletions and renames out of `.claude/` (TC-93); a protected change buried two merges deep (TC-94);
- approval attempts without a person: piped, behind a fake TTY (`script -qc`), under a sandbox and with read-only
  git metadata and an unreadable data dir (TC-95);
- delete-then-create of an `integrate/*` ref and every other way to create one (TC-182);
- sandboxed writes to ck's hook state (TC-190).
"""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import unattended_helpers as h

INTEG = "integrate/demo"
INTEG_REF = "refs/heads/integrate/demo"
ZERO = "0" * 40


# --------------------------------------------------------------------------- helpers


def common_dir(root: Path) -> Path:
    return Path(h.raw(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())


def hooks_dir(root: Path) -> Path:
    return common_dir(root) / "ck" / "hooks"


def integ(root: Path) -> Path:
    return h.integration_worktree(root)


def text_of(proc) -> str:
    return (proc.stdout or "") + (proc.stderr or "")


def strings(obj) -> list:
    """Every string anywhere in a parsed JSON value."""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for k, v in obj.items() for s in strings(k) + strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in strings(v)]
    return []


def new_task(root: Path, n: int, files: dict, message: str = "") -> str:
    """`ck worktree create demo N` from integrate/demo, then one commit of `files` (None deletes). Returns the branch."""
    code, _, err = h.ck_text(root, "worktree", "create", "demo", str(n), "--base", INTEG)
    assert code == 0, f"ck worktree create demo {n} failed: {err}"
    h._commit_in(h.task_worktree(root, n), files, message or f"task {n}")
    return f"feat/demo-task-{n}"


def has_file(root: Path, rev: str, rel: str) -> bool:
    return h.raw(root, "cat-file", "-e", f"{rev}:{rel}").returncode == 0


def tidy(wt: Path) -> None:
    """Leave the integration worktree clean after a refused raw git command."""
    h.raw(wt, "merge", "--abort")
    h.raw(wt, "reset", "--hard", "-q")


def raw_refused(root: Path, args, *, names: str = "") -> None:
    """A raw git command in the integration worktree is refused and integrate/demo does not move."""
    wt = integ(root)
    before = h.ref(root, INTEG)
    out = h.raw(wt, *args)
    tidy(wt)
    assert out.returncode != 0, f"git {' '.join(args)} was not refused: {text_of(out)}"
    assert h.ref(root, INTEG) == before, f"git {' '.join(args)} moved {INTEG}"
    if names:
        assert names in text_of(out), f"refusal does not name {names!r}: {text_of(out)}"


def raw_forms_refused(root: Path, branch: str, names: str) -> None:
    """Every raw way to move integrate/demo onto `branch` is refused."""
    raw_refused(root, ["merge", "--no-ff", "--no-edit", branch], names=names)
    raw_refused(root, ["merge", "--no-ff", "--no-edit", "--no-verify", branch])
    raw_refused(root, ["merge", "--ff-only", branch])
    raw_refused(root, ["update-ref", INTEG_REF, h.ref(root, branch)])


def ck_refused(root: Path, verb: str, n: int, rel: str) -> None:
    """`ck worktree land|merge` refuses with state PROTECTED naming `rel` unquoted, exit 3; nothing moves."""
    before = h.ref(root, INTEG)
    code, out, err = h.ck_text(root, "worktree", verb, "demo", str(n), "--into", INTEG, "--json")
    assert code == 3, f"ck worktree {verb} demo {n}: exit {code}\n{out}\n{err}"
    obj = json.loads(out)
    items = obj["tasks"] if isinstance(obj, dict) else obj
    assert [i["state"] for i in items] == ["PROTECTED"], out
    assert any(rel in s for s in strings(obj)), f"{rel!r} not named unquoted in {out}"
    assert not any(s.startswith('"') and s.endswith('"') and "\\" in s for s in strings(obj))
    assert h.ref(root, INTEG) == before
    assert h.task_worktree(root, n).exists(), "a refused task must keep its worktree"


def all_refused(root: Path, n: int, branch: str, rel: str) -> None:
    raw_forms_refused(root, branch, rel)
    ck_refused(root, "land", n, rel)
    ck_refused(root, "merge", n, rel)


def audit(root: Path):
    return h.ck_json(root, "worktree", "audit", "demo", "--json")


def drift_map(obj) -> dict:
    return {d["file"]: d["change"] for d in obj["drift"]}


# --------------------------------------------------------------------------- TC-90


@pytest.mark.parametrize("rel", [
    None,  # task 4 of hooks_repo: .claude/commands/ä.md
    ".claude/commands/my notes.md",
    '.claude/commands/say "hi".md',
    ".claude/commands/日本語.md",
    ".claude/commands/tab\there.md",
    ".claude/commands/dir with space/é.md",
])
def test_tc90_git_quoted_paths_are_refused(tmp_path, rel):
    """9.2, 9.6: git quotes these names in `git diff --name-only`; raw merge, land and merge still refuse and
    PROTECTED names the unquoted path."""
    root = h.hooks_repo(tmp_path)
    if rel is None:
        rel, n = ".claude/commands/ä.md", 4
        branch = "feat/demo-task-4"
    else:
        n = 6
        branch = new_task(root, n, {rel: "x\n"})
    assert h.raw(root, "diff", "--name-only", f"{INTEG}...{branch}").stdout.strip() != rel or rel.isascii()
    all_refused(root, n, branch, rel)


# --------------------------------------------------------------------------- TC-91


@pytest.mark.parametrize("two_commits", [False, True])
@pytest.mark.parametrize("allow", [[".claude/**"], ["**"], [".claude/x"]])
def test_tc91_branch_cannot_allow_itself(tmp_path, two_commits, allow):
    """9.2: the allow-list is read from the ref's old value, never from the incoming commit."""
    root = h.hooks_repo(tmp_path)
    cfg = json.dumps({"land": {"allow_paths": allow}}, indent=2) + "\n"
    if two_commits:
        new_task(root, 6, {"specs/ck.json": cfg}, "allow myself")
        h._commit_in(h.task_worktree(root, 6), {".claude/x": "x\n"}, "add .claude/x")
    else:
        new_task(root, 6, {"specs/ck.json": cfg, ".claude/x": "x\n"})
    branch = "feat/demo-task-6"
    raw_forms_refused(root, branch, "")
    ck_refused(root, "land", 6, ".claude/x")
    ck_refused(root, "merge", 6, ".claude/x")


# --------------------------------------------------------------------------- TC-93


def test_tc93_deleting_a_protected_file_is_refused(tmp_path):
    """9.2: task 5 deletes .claude/settings.json (seeded on the base)."""
    root = h.hooks_repo(tmp_path)
    all_refused(root, 5, "feat/demo-task-5", ".claude/settings.json")


def test_tc93_rename_out_of_claude_is_refused(tmp_path):
    """9.2: a rename moves a protected file to notes/; the old path is a protected deletion."""
    root = h.hooks_repo(tmp_path)
    content = (root / ".claude" / "settings.json").read_text()
    branch = new_task(root, 6, {".claude/settings.json": None, "notes/settings.json": content}, "rename")
    status = h.raw(root, "diff", "--name-status", "-M", f"{INTEG}...{branch}").stdout
    assert status.startswith("R"), f"setup: git must see a rename, got {status!r}"
    all_refused(root, 6, branch, ".claude/settings.json")


def test_tc93_rename_into_claude_is_refused(tmp_path):
    """9.2: the other direction, a clean file renamed into .claude/."""
    root = h.hooks_repo(tmp_path)
    branch = new_task(root, 6, {"src/a.py": None, ".claude/commands/a.py": "A = 1\n"}, "rename into claude")
    all_refused(root, 6, branch, ".claude/commands/a.py")


# --------------------------------------------------------------------------- TC-94


def test_tc94_protected_change_buried_two_merges_deep(tmp_path):
    """9.2: the tip commit is clean; the protected change came in through a merge of a merge."""
    root = h.hooks_repo(tmp_path)
    branch = new_task(root, 6, {"src/b.py": "B = 1\n"}, "task 6 own work")
    wt = h.task_worktree(root, 6)
    h.git(wt, "checkout", "-q", "-b", "side1", INTEG)
    h._commit_in(wt, {".claude/x": "buried\n"}, "side1 adds .claude/x")
    h.git(wt, "checkout", "-q", "-b", "side2", INTEG)
    h.git(wt, "merge", "--no-ff", "-q", "-m", "merge side1", "side1")
    h._commit_in(wt, {"src/side2.py": "S = 2\n"}, "side2 clean")
    h.git(wt, "checkout", "-q", branch)
    h.git(wt, "merge", "--no-ff", "-q", "-m", "merge side2", "side2")
    h._commit_in(wt, {"src/c.py": "C = 3\n"}, "clean tip")
    tip = h.git(wt, "diff", "--name-only", "HEAD~1", "HEAD").split()
    assert tip == ["src/c.py"], f"setup: the tip commit must be clean, got {tip}"
    all_refused(root, 6, branch, ".claude/x")


def test_tc94_protected_change_reverted_in_a_side_merge_still_visible_net(tmp_path):
    """9.2: a protected file added and then edited again in a later commit is one net change, refused."""
    root = h.hooks_repo(tmp_path)
    branch = new_task(root, 6, {".claude/x": "one\n"}, "add")
    h._commit_in(h.task_worktree(root, 6), {".claude/x": "two\n", "src/d.py": "D = 4\n"}, "edit and clean")
    all_refused(root, 6, branch, ".claude/x")


# --------------------------------------------------------------------------- TC-92 (bypasses and the audit)


def test_tc92_hooks_path_override_lands_and_the_audit_reports_drift(tmp_path):
    """10.1, 10.2: `git -c core.hooksPath=/dev/null merge` cannot be stopped, but the audit reports it."""
    root = h.hooks_repo(tmp_path)
    before = h.ref(root, INTEG)
    out = h.raw(integ(root), "-c", "core.hooksPath=/dev/null", "merge", "--no-ff", "--no-edit", "feat/demo-task-1")
    assert out.returncode == 0, text_of(out)
    assert h.ref(root, INTEG) != before, "setup: the bypass must land"
    code, obj, err = audit(root)
    assert code == 1, (code, obj, err)
    assert obj["schema"] == "ck-audit/1" and obj["complete"] is True and obj["clean"] is False
    assert drift_map(obj) == {".claude/CLAUDE.md": "added"}
    hooks = obj["hooks"]
    assert hooks["installed"] is True and hooks["active"] is True and hooks["intact"] is True, hooks


def test_tc92_deleted_hooks_land_and_the_audit_reports_both(tmp_path):
    """10.1, 10.2: delete ck's hook directory, land a protected change with plain git."""
    root = h.hooks_repo(tmp_path)
    assert hooks_dir(root).is_dir(), "hooks must be installed (9.1) before they can be deleted"
    shutil.rmtree(hooks_dir(root))
    before = h.ref(root, INTEG)
    out = h.raw(integ(root), "merge", "--no-ff", "--no-edit", "feat/demo-task-1")
    assert out.returncode == 0, text_of(out)
    assert h.ref(root, INTEG) != before, "setup: deleted hooks cannot refuse"
    code, obj, err = audit(root)
    assert code == 1, (code, obj, err)
    assert obj["clean"] is False and drift_map(obj) == {".claude/CLAUDE.md": "added"}
    hooks = obj["hooks"]
    assert (hooks["installed"] is False or hooks["intact"] is False) and hooks["problems"], hooks


def test_tc92_altered_hook_script_is_reported(tmp_path):
    """10.2: every hook script is replaced by `exit 0`; the protected merge lands; intact is false."""
    root = h.hooks_repo(tmp_path)
    scripts = [p for p in sorted(hooks_dir(root).iterdir()) if p.is_file() and p.suffix == ""]
    assert scripts, "ck must have installed hook scripts"
    for path in scripts:
        mode = path.stat().st_mode
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(mode)
    before = h.ref(root, INTEG)
    out = h.raw(integ(root), "merge", "--no-ff", "--no-edit", "feat/demo-task-1")
    assert out.returncode == 0 and h.ref(root, INTEG) != before, text_of(out)
    code, obj, _ = audit(root)
    assert code == 1 and obj["clean"] is False
    assert obj["hooks"]["intact"] is False and obj["hooks"]["problems"], obj["hooks"]
    assert drift_map(obj) == {".claude/CLAUDE.md": "added"}


def test_tc92_unset_hooks_path_is_reported_inactive(tmp_path):
    """10.2: `git config --unset core.hooksPath` leaves ck's scripts intact but not active."""
    root = h.hooks_repo(tmp_path)
    assert h.raw(root, "config", "--local", "core.hooksPath").stdout.strip(), "hooks must be installed (9.1)"
    assert h.raw(root, "config", "--local", "--unset", "core.hooksPath").returncode == 0
    before = h.ref(root, INTEG)
    out = h.raw(integ(root), "merge", "--no-ff", "--no-edit", "feat/demo-task-2")
    assert out.returncode == 0 and h.ref(root, INTEG) != before, text_of(out)
    code, obj, _ = audit(root)
    assert code == 1 and obj["clean"] is False
    assert obj["hooks"]["active"] is False and obj["hooks"]["problems"], obj["hooks"]
    assert drift_map(obj) == {".claude/commands/x.md": "added"}


@pytest.mark.parametrize("task, expected", [
    (1, {".claude/CLAUDE.md": "added"}),
    (4, {".claude/commands/ä.md": "added"}),
    (5, {".claude/settings.json": "deleted"}),
])
def test_tc92_ref_file_write_lands_and_the_audit_reports_drift(tmp_path, task, expected):
    """10.1: writing `refs/heads/integrate/demo` directly touches no hook; the audit names each file unquoted."""
    root = h.hooks_repo(tmp_path)
    target = h.ref(root, f"feat/demo-task-{task}")
    ref_file = common_dir(root) / "refs" / "heads" / "integrate" / "demo"
    ref_file.parent.mkdir(parents=True, exist_ok=True)
    ref_file.write_text(target + "\n")
    assert h.ref(root, INTEG) == target, "setup: the ref file write must move the branch"
    code, obj, err = audit(root)
    assert code == 1, (code, obj, err)
    assert obj["complete"] is True and obj["clean"] is False
    assert drift_map(obj) == expected
    hooks = obj["hooks"]
    assert hooks["installed"] is True and hooks["active"] is True and hooks["intact"] is True, hooks


def test_tc92_audit_reports_nothing_for_ck_own_clean_landing(tmp_path):
    """10.1 negative control: a clean `ck worktree land` is not drift, so the audit says clean and exits 0."""
    root = h.hooks_repo(tmp_path)
    code, out, err = h.ck_text(root, "worktree", "land", "demo", "3", "--into", INTEG, "--json")
    assert code == 0, (out, err)
    code, obj, err = audit(root)
    assert code == 0 and obj["clean"] is True and obj["drift"] == [], (code, obj, err)


# --------------------------------------------------------------------------- TC-95 (approval without a person)

APPROVE = ["worktree", "approve", "demo", "1"]


def assert_not_approved(root: Path, before: str, proc, *, label: str) -> None:
    text = text_of(proc)
    assert "No such command" not in text and "No module named" not in text, f"{label}: approve is not runnable: {text}"
    assert proc.returncode != 0, f"{label}: approval did not refuse: {text}"
    assert h.ref(root, INTEG) == before, f"{label}: {INTEG} moved"
    assert not has_file(root, INTEG, ".claude/CLAUDE.md"), f"{label}: the protected file landed"
    assert h.task_worktree(root, 1).exists(), f"{label}: the task was consumed"


def fake_tty(argv) -> list:
    return ["script", "-qec", " ".join(shlex.quote(a) for a in argv), "/dev/null"]


TYPED = ["yes\n", "yes\nyes\nyes\n", "YES\n", "y\n", "\n", "approve\n", "000000\n", "123456\n", "1\n", "\x04"]


@pytest.mark.parametrize("typed", TYPED)
def test_tc95_piped_input_cannot_approve(tmp_path, typed):
    """9.8: stdin is a pipe, not a TTY."""
    root = h.hooks_repo(tmp_path)
    before = h.ref(root, INTEG)
    proc = h.ck_run(root, *APPROVE, input=typed)
    assert_not_approved(root, before, proc, label=f"pipe {typed!r}")


@pytest.mark.parametrize("typed", TYPED)
def test_tc95_fake_tty_with_guessed_answers_cannot_approve(tmp_path, typed):
    """9.8: `script -qc` gives ck a TTY; the agent still cannot type a code it has not seen."""
    if shutil.which("script") is None:
        pytest.skip("script(1) is not installed")
    root = h.hooks_repo(tmp_path)
    before = h.ref(root, INTEG)
    argv = fake_tty([sys.executable, "-m", "claude_kiro.cli", *APPROVE])
    proc = subprocess.run(argv, cwd=root, env=h._ck_env(root, None), input=typed, capture_output=True, text=True,
                          timeout=120)
    assert "No such command" not in text_of(proc) and "No module named" not in text_of(proc), text_of(proc)
    assert h.ref(root, INTEG) == before
    assert not has_file(root, INTEG, ".claude/CLAUDE.md")
    assert h.task_worktree(root, 1).exists()


@pytest.mark.parametrize("extra", [["--yes"], ["-y"], ["--code", "123456"], ["--json"], ["--force"], ["123456"]])
def test_tc95_no_command_line_bypass(tmp_path, extra):
    """9.8: approve never takes a code, --yes or --json on the command line."""
    root = h.hooks_repo(tmp_path)
    before = h.ref(root, INTEG)
    proc = h.ck_run(root, *APPROVE, *extra, input="yes\n")
    assert proc.returncode != 0, text_of(proc)
    assert h.ref(root, INTEG) == before and not has_file(root, INTEG, ".claude/CLAUDE.md")
    assert h.task_worktree(root, 1).exists()


def test_tc95_readonly_git_metadata_and_unreadable_data_dir(tmp_path):
    """9.8: with read-only git metadata and the ck data dir unreadable, neither a pipe nor a fake TTY approves."""
    root = h.hooks_repo(tmp_path)
    data = tmp_path / "ck-data-unreadable"
    data.mkdir()
    data.chmod(0)
    env = {"CK_DATA_HOME": str(data)}
    before = h.ref(root, INTEG)
    try:
        with h.readonly_git_metadata(root):
            proc = h.ck_run(root, *APPROVE, input="yes\n", env=env)
            assert_not_approved(root, before, proc, label="readonly pipe")
            if shutil.which("script"):
                argv = fake_tty([sys.executable, "-m", "claude_kiro.cli", *APPROVE])
                proc = subprocess.run(argv, cwd=root, env=h._ck_env(root, env), input="yes\n",
                                      capture_output=True, text=True, timeout=120)
                assert "No such command" not in text_of(proc), text_of(proc)
                assert h.ref(root, INTEG) == before
    finally:
        data.chmod(0o700)
    assert not has_file(root, INTEG, ".claude/CLAUDE.md")


def sandbox_run(argv, root: Path, *, input=None, writable_worktrees=True) -> subprocess.CompletedProcess:
    """`argv` under real bwrap with the binds `sandbox_settings` implies, plus what the helper leaves out:
    `denyWrite` entries become read-only binds, the interpreter and the sources are readable (the helper hides
    `~`), and `.git/worktrees` stays writable so a landing can move a worktree. Skips only without namespaces."""
    reason = h._bwrap_probe()
    if reason is not None:
        pytest.skip(f"bwrap unavailable: {reason}")
    import claude_kiro
    from claude_kiro.cli.run import sandbox_settings

    wt_dir = root / ".ck" / "worktrees"
    cmd = h.bwrap_argv(argv, root, wt_dir)
    cut = cmd.index("--chdir")
    extra = []
    for p in {Path(sys.prefix), Path(sys.base_prefix), Path(claude_kiro.__file__).resolve().parents[1]}:
        extra += ["--ro-bind", str(p), str(p)]
    if writable_worktrees:
        wts = common_dir(root) / "worktrees"
        extra += ["--bind", str(wts), str(wts)]
    deny = sandbox_settings(root, [], wt_dir)["sandbox"]["filesystem"].get("denyWrite", [])
    for entry in deny:
        path = h._home_path(entry)
        if path.exists():
            extra += ["--ro-bind", str(path), str(path)]
    return subprocess.run([*cmd[:cut], *extra, *cmd[cut:]], capture_output=True, text=True, input=input,
                          env=h._ck_env(root, None), timeout=300)


def test_tc95_sandboxed_approval_is_refused(tmp_path):
    """9.8, 9.11: approval inside the run sandbox, piped and behind `script -qc` with "yes" piped in."""
    root = h.hooks_repo(tmp_path)
    before = h.ref(root, INTEG)
    ck = [sys.executable, "-m", "claude_kiro.cli", *APPROVE]
    proc = sandbox_run(ck, root, input="yes\n")
    assert_not_approved(root, before, proc, label="sandbox pipe")
    if shutil.which("script"):
        proc = sandbox_run(fake_tty(ck), root, input="yes\n")
        text = text_of(proc)
        assert "No such command" not in text and "No module named" not in text, text
        assert h.ref(root, INTEG) == before
    assert not has_file(root, INTEG, ".claude/CLAUDE.md")
    assert h.task_worktree(root, 1).exists()


# --------------------------------------------------------------------------- TC-182


def test_tc182_delete_then_recreate(tmp_path):
    """9.12: delete passes; re-creating at a protected commit is refused (ref stays absent); at the last approved
    commit it passes."""
    root = h.hooks_repo(tmp_path)
    approved = h.ref(root, INTEG)
    protected = h.ref(root, "feat/demo-task-1")
    deleted = h.raw(root, "update-ref", "-d", INTEG_REF)
    assert deleted.returncode == 0, text_of(deleted)
    assert h.ref(root, INTEG) == ""
    out = h.raw(root, "update-ref", INTEG_REF, protected)
    assert out.returncode != 0 and h.ref(root, INTEG) == "", text_of(out)
    assert ".claude/CLAUDE.md" in text_of(out), text_of(out)
    out = h.raw(root, "update-ref", INTEG_REF, approved)
    assert out.returncode == 0 and h.ref(root, INTEG) == approved, text_of(out)


def test_tc182_recreate_at_commit_with_unchanged_protected_files_passes(tmp_path):
    """9.12: equal protected files means nothing new lands, so the re-creation passes."""
    root = h.hooks_repo(tmp_path)
    clean = h.ref(root, "feat/demo-task-3")
    assert h.raw(root, "update-ref", "-d", INTEG_REF).returncode == 0
    out = h.raw(root, "update-ref", INTEG_REF, clean)
    assert out.returncode == 0 and h.ref(root, INTEG) == clean, text_of(out)


FORMS = ["update-ref", "update-ref-zero-old", "update-ref-stdin", "branch", "branch-new-name", "branch-nested",
         "push-dot", "fetch-dot", "worktree-add", "worktree-add-detached-then-branch"]


@pytest.mark.parametrize("form", FORMS)
def test_tc182_every_way_to_create_an_integration_ref_is_refused(tmp_path, form):
    root = h.hooks_repo(tmp_path)
    protected = h.ref(root, "feat/demo-task-1")
    name = "integrate/demo"
    if form in ("update-ref", "update-ref-zero-old", "update-ref-stdin", "branch"):
        if form == "branch":
            # `branch -D` needs the branch not checked out.
            assert h.raw(integ(root), "checkout", "--detach", "-q").returncode == 0
            out = h.raw(root, "branch", "-D", INTEG)
            assert out.returncode == 0, text_of(out)
        else:
            assert h.raw(root, "update-ref", "-d", INTEG_REF).returncode == 0
    assert h.ref(root, name) == "" or form not in ("update-ref", "update-ref-zero-old", "update-ref-stdin", "branch")
    if form == "update-ref":
        out = h.raw(root, "update-ref", INTEG_REF, protected)
    elif form == "update-ref-zero-old":
        out = h.raw(root, "update-ref", INTEG_REF, protected, ZERO)
    elif form == "update-ref-stdin":
        out = subprocess.run(["git", "update-ref", "--stdin"], cwd=root, capture_output=True, text=True,
                             input=f"start\ncreate {INTEG_REF} {protected}\ncommit\n")
    elif form == "branch":
        out = h.raw(root, "branch", INTEG, protected)
    elif form == "branch-new-name":
        name = "integrate/new2"
        out = h.raw(root, "branch", name, protected)
    elif form == "branch-nested":
        name = "integrate/deep/er"
        out = h.raw(root, "branch", name, protected)
    elif form == "push-dot":
        name = "integrate/new"
        out = h.raw(root, "push", ".", f"{protected}:refs/heads/{name}")
    elif form == "fetch-dot":
        name = "integrate/f"
        out = h.raw(root, "fetch", ".", f"{protected}:refs/heads/{name}")
    elif form == "worktree-add":
        name = "integrate/x"
        out = h.raw(root, "worktree", "add", "-b", name, str(tmp_path / "wt-x"), protected)
    else:  # a detached worktree, then a branch created from inside it
        name = "integrate/y"
        wt = tmp_path / "wt-y"
        assert h.raw(root, "worktree", "add", "--detach", str(wt), protected).returncode == 0
        out = h.raw(wt, "checkout", "-b", name)
    assert out.returncode != 0, f"{form} created {name} at a protected commit: {text_of(out)}"
    assert h.ref(root, name) == "", f"{form}: {name} exists after the refusal"


def test_tc182_branch_delete_then_branch_at_approved_commit_passes(tmp_path):
    """9.12: after `branch -D`, `git branch integrate/demo <approved>` passes."""
    root = h.hooks_repo(tmp_path)
    approved = h.ref(root, INTEG)
    assert h.raw(integ(root), "checkout", "--detach", "-q").returncode == 0
    assert h.raw(root, "branch", "-D", INTEG).returncode == 0
    out = h.raw(root, "branch", INTEG, approved)
    assert out.returncode == 0 and h.ref(root, INTEG) == approved, text_of(out)


def test_tc182_ck_creates_its_own_integration_ref(tmp_path):
    """9.12, 9.7: `ck worktree integration` creating integrate/other with hooks installed succeeds (token)."""
    root = h.hooks_repo(tmp_path)
    assert h.raw(root, "config", "--local", "core.hooksPath").stdout.strip(), "hooks must be installed (9.1)"
    code, out, err = h.ck_text(root, "worktree", "integration", "other")
    assert code == 0, (out, err)
    assert h.ref(root, "integrate/other") == h.ref(root, "main")


def test_tc182_ck_integration_from_a_protected_base_after_delete_is_refused(tmp_path):
    """9.12, 10.1: delete the worktree and branch (deletes pass), then `ck worktree integration --base <protected
    change>` must not recreate the ref nor reseal the approved state from that base."""
    root = h.hooks_repo(tmp_path)
    protected = h.ref(root, "feat/demo-task-1")
    wt = integ(root)
    assert h.raw(root, "worktree", "remove", "--force", str(wt)).returncode == 0
    assert h.raw(root, "branch", "-D", INTEG).returncode == 0
    assert h.ref(root, INTEG) == ""
    code, out, err = h.ck_text(root, "worktree", "integration", "demo", "--base", protected)
    assert h.ref(root, INTEG) == "", f"{INTEG} was created: {out} {err}"
    assert code != 0 or "CREATED" not in out, (out, err)
    assert ".claude/CLAUDE.md" in out + err, (out, err)
    assert "ck worktree approve" in out + err, (out, err)


# --------------------------------------------------------------------------- TC-190


def _denied(root: Path) -> list:
    from claude_kiro.cli.run import sandbox_settings

    fs = sandbox_settings(root, [], root / ".ck" / "worktrees")["sandbox"]["filesystem"]
    return [os.path.realpath(h._home_path(p)) for p in fs.get("denyWrite", [])]


def test_tc190_sandbox_settings_deny_write_hooks_and_key_but_not_tokens(tmp_path):
    """9.11 (a): `denyWrite` lists ck/hooks and ck/key and leaves ck/tokens (and per-spec state) writable."""
    root = h.hooks_repo(tmp_path)
    common = os.path.realpath(common_dir(root))
    denied = _denied(root)
    assert os.path.join(common, "ck", "hooks") in denied, denied
    assert os.path.join(common, "ck", "key") in denied, denied
    for writable in (os.path.join(common, "ck", "tokens"), os.path.join(common, "ck", "demo")):
        for entry in denied:
            assert writable != entry and not writable.startswith(entry + os.sep), \
                f"{entry} denies writes to {writable}, which landings need"


def _sha_tree(paths) -> dict:
    out = {}
    for base in paths:
        base = Path(base)
        files = [base] if base.is_file() else [p for p in sorted(base.rglob("*")) if p.is_file()]
        for p in files:
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


_ATTACK = r'''
import json, os, sys
hooks, key = sys.argv[1], sys.argv[2]
worked = []
def attempt(label, fn):
    try:
        fn()
        worked.append(label)
    except OSError:
        pass
def append(p):
    with open(p, "ab") as f:
        f.write(b"\n# planted\n")
def truncate(p):
    with open(p, "wb") as f:
        f.write(b"")
for dirpath, _dirs, names in os.walk(hooks):
    for name in names:
        p = os.path.join(dirpath, name)
        attempt("append " + p, lambda p=p: append(p))
        attempt("truncate " + p, lambda p=p: truncate(p))
        attempt("unlink " + p, lambda p=p: os.unlink(p))
        attempt("replace " + p, lambda p=p: (open(p + ".new", "w").write("x"), os.replace(p + ".new", p)))
chain = os.path.join(hooks, "chain.json")
attempt("repoint chain.json", lambda: open(chain, "w").write(json.dumps({"previous": "/tmp/attacker-hooks"})))
attempt("new hook", lambda: open(os.path.join(hooks, "pre-commit"), "w").write("#!/bin/sh\nexit 0\n"))
attempt("rename hooks dir", lambda: os.rename(hooks, hooks + ".bak"))
attempt("write key", lambda: truncate(key))
attempt("unlink key", lambda: os.unlink(key))
attempt("replace key", lambda: (open(key + ".new", "w").write("k"), os.replace(key + ".new", key)))
print(json.dumps(worked))
'''


def test_tc190_sandboxed_writes_to_hook_state_fail_and_an_allowed_land_still_works(tmp_path):
    """9.11 (b): inside the run sandbox the hook scripts, chain.json, the interpreter record and the key cannot
    be written, replaced, unlinked or renamed (sha256s unchanged); a sandboxed land of an allowed change still
    succeeds, because tokens and per-spec state stay writable."""
    root = h.hooks_repo(tmp_path)
    hooks, key = hooks_dir(root), common_dir(root) / "ck" / "key"
    assert (hooks / "reference-transaction").is_file(), "ck must have installed its hooks (9.1)"
    assert (hooks / "chain.json").is_file() and key.is_file()
    before = _sha_tree([hooks, key])
    proc = sandbox_run([sys.executable, "-c", _ATTACK, str(hooks), str(key)], root)
    assert proc.returncode == 0, text_of(proc)
    assert json.loads(proc.stdout) == [], f"the sandbox let these writes through: {proc.stdout}"
    assert _sha_tree([hooks, key]) == before
    ref_before = h.ref(root, INTEG)
    land = sandbox_run([sys.executable, "-m", "claude_kiro.cli", "worktree", "land", "demo", "3", "--into", INTEG,
                        "--json"], root)
    assert land.returncode == 0, text_of(land)
    assert [t["state"] for t in json.loads(land.stdout)["tasks"]] == ["MERGED"], land.stdout
    assert h.ref(root, INTEG) != ref_before
    assert _sha_tree([hooks, key]) == before, "a sandboxed landing changed ck's hook state"
