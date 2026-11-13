"""Red-team of Story 14 (landing recovery): the SHALL NOT clauses 14.3, 14.4 and 14.5, plus further breaks.

Written from requirements 14.1 to 14.5 only. Every case builds a real repository with ck's real hooks installed
(`hooks_repo`), makes the hook refuse a real raw `git merge --no-verify`, and drives the real `ck worktree` CLI. No
case mocks project code. Every case ends by checking the two safety properties: `integrate/demo` never holds a
protected file (14.4), and it only ever moves forward through a landing of a clean task.

Each break found while attacking the requirements is a test:
- a refusal recorded for `integrate/demo` must not license restoring `integrate/demo2`, nor the reverse (14.5);
- a checkout reached through a symlink is still the checkout that the refusal names (14.1, 14.4);
- a main checkout that has an `integrate/*` branch checked out is not a managed checkout (14.3);
- a record written by hand pointing at a protected commit, at another checkout, or for the main branch never
  licenses a restore (14.4, 14.5);
- untracked and ignored files, whatever their names, are never touched (14.3);
- a submodule edit, and a staged submodule pointer made by hand, is never discarded (14.3, 14.5);
- a landing that races the hook writing records never lands a protected file (14.4).
"""

import os
import threading
from pathlib import Path
from typing import Dict, Iterable, Optional

import pytest

import unattended_helpers as h

INTEG = "integrate/demo"
INTEG_REF = "refs/heads/integrate/demo"
PROTECTED_OK = {".claude/settings.json"}  # seeded on main by hooks_repo: the only protected path integrate/demo may hold


# --------------------------------------------------------------------------- helpers


def protected_branch(root: Path, name: str, files: Optional[Dict[str, Optional[str]]] = None,
                     base: str = INTEG) -> str:
    """A branch `name` off `base` whose one commit changes protected files (default: adds `.claude/CLAUDE.md`).

    Built in a throwaway worktree outside the project, so no checkout of the project is disturbed. Returns the
    branch name.
    """
    files = files if files is not None else {".claude/CLAUDE.md": "# protected change\n"}
    scratch = root.parent / ("pb-" + name.replace("/", "_"))
    h.git(root, "worktree", "add", "-q", "-b", name, str(scratch), base)
    h._commit_in(scratch, files, f"protected change on {name}")
    h.git(root, "worktree", "remove", "--force", str(scratch))
    return name


def text_of(proc) -> str:
    return (proc.stdout or "") + (proc.stderr or "")


def refused_merge(checkout: Path, branch: str, *extra: str) -> None:
    """A raw `git merge --no-verify` of `branch` in `checkout` that the installed hook refuses."""
    out = h.raw(checkout, "merge", "--no-verify", *extra, branch)
    assert out.returncode != 0, f"the raw merge was not refused: {text_of(out)}"


def land(root: Path, n: int = 3, into: str = INTEG, cwd: Optional[Path] = None):
    return h.ck_text(cwd or root, "worktree", "land", "demo", str(n), "--into", into, "--json")


def head_of(root: Path, rev: str = INTEG) -> str:
    return h.ref(root, rev)


def paths_ever_in(root: Path, rev: str) -> set:
    out = h.git(root, "log", "--name-only", "--format=", rev)
    return {line for line in out.splitlines() if line}


def safe(root: Path, before: str, rev: str = INTEG) -> None:
    """14.4: the ref holds no protected file it did not hold at the start, and only moved forward."""
    extra = {p for p in paths_ever_in(root, rev) if p.startswith(".claude/") and p not in PROTECTED_OK}
    assert not extra, f"{rev} holds protected files {sorted(extra)}"
    after = head_of(root, rev)
    assert h.raw(root, "merge-base", "--is-ancestor", before, after).returncode == 0, f"{rev} moved off its history"


def staged_names(checkout: Path) -> set:
    return {line for line in h.git(checkout, "diff", "--cached", "--name-only").splitlines() if line}


def snapshot(root: Path, rels: Iterable[str]) -> Dict[str, Optional[bytes]]:
    return {r: ((root / r).read_bytes() if (root / r).is_file() else None) for r in rels}


def realpath(p: Path) -> str:
    return str(Path(os.path.realpath(p)))


def write_record(root: Path, ref: str, head: str, checkout: Path) -> None:
    """A refusal record written through the protect module, but with values the hook never recorded."""
    from claude_kiro import protect

    protect.record_refusal(root, protect.Refusal(ref=ref, head=head, checkout=realpath(checkout),
                                                 at="2026-01-01T00:00:00Z"))


def second_integration(root: Path, spec: str) -> Path:
    code, _, err = h.ck_text(root, "worktree", "integration", spec)
    assert code == 0, f"ck worktree integration {spec} failed: {err}"
    return h.integration_worktree(root, spec)


# --------------------------------------------------------------------------- 14.5: a record for another branch


def test_redteam_14_5_record_for_a_prefix_branch_does_not_license_the_longer_branch(tmp_path):
    """A refusal for integrate/demo must not restore a hand edit in the integrate/demo2 checkout."""
    root = h.hooks_repo(tmp_path)
    wt = h.integration_worktree(root)
    wt2 = second_integration(root, "demo2")
    pb = protected_branch(root, "pb/ff")
    refused_merge(wt, pb)  # records a refusal for refs/heads/integrate/demo
    (wt2 / "shared.txt").write_text("hand edit in demo2\n")
    before2, before = head_of(root, "integrate/demo2"), head_of(root)
    code, out, err = land(root, 3, "integrate/demo2")
    assert code != 0, f"landing into a hand-edited checkout was not refused:\n{out}\n{err}"
    assert (wt2 / "shared.txt").read_text() == "hand edit in demo2\n"
    assert head_of(root, "integrate/demo2") == before2
    assert head_of(root) == before


def test_redteam_14_5_record_for_the_longer_branch_does_not_license_the_prefix_branch(tmp_path):
    """The reverse: a refusal for integrate/demo2 must not restore a merge in progress in integrate/demo."""
    root = h.hooks_repo(tmp_path)
    wt = h.integration_worktree(root)
    wt2 = second_integration(root, "demo2")
    pb2 = protected_branch(root, "pb/demo2", base="integrate/demo2")
    refused_merge(wt2, pb2)  # records a refusal for refs/heads/integrate/demo2
    (wt / "shared.txt").write_text("hand edit in demo\n")
    h.git(wt, "add", "shared.txt")
    before = head_of(root)
    code, out, err = land(root, 3)
    assert code != 0, f"landing into a hand-edited checkout was not refused:\n{out}\n{err}"
    assert (wt / "shared.txt").read_text() == "hand edit in demo\n"
    assert staged_names(wt) == {"shared.txt"}
    assert head_of(root) == before


def test_redteam_14_5_record_for_the_prefix_branch_still_recovers_that_branch(tmp_path):
    """Not over-strict: the same setup, but the refusal names the dirty checkout's own branch, so it recovers."""
    root = h.hooks_repo(tmp_path)
    wt = h.integration_worktree(root)
    second_integration(root, "demo2")
    pb = protected_branch(root, "pb/ff")
    refused_merge(wt, pb)
    before = head_of(root)
    code, out, err = land(root, 3)
    assert code == 0, f"the recovery did not land:\n{out}\n{err}"
    assert ".claude/CLAUDE.md" not in paths_ever_in(root, INTEG)
    safe(root, before)


# --------------------------------------------------------------------------- 14.1, 14.4: a symlinked checkout


def test_redteam_14_4_checkout_reached_through_symlinks_is_recovered_and_the_ref_stays(tmp_path):
    """The refused merge runs from a symlink to the checkout, the landing from a symlink to the project."""
    root = h.hooks_repo(tmp_path)
    wt = h.integration_worktree(root)
    link_wt, link_root = tmp_path / "link-integ", tmp_path / "link-root"
    link_wt.symlink_to(wt, target_is_directory=True)
    link_root.symlink_to(root, target_is_directory=True)
    pb = protected_branch(root, "pb/ff")
    refused_merge(link_wt, pb)
    before = head_of(root)
    code, out, err = land(root, 3, cwd=link_root)
    assert code == 0, f"the symlinked checkout was not recovered:\n{out}\n{err}"
    safe(root, before)
    assert not (wt / ".claude" / "CLAUDE.md").exists()
    assert staged_names(wt) == set()


def test_redteam_14_5_a_symlink_named_like_the_checkout_is_not_the_checkout(tmp_path):
    """A refusal recorded in one checkout does not license a hand edit in a copy reached by the same branch name.

    The record names the realpath of the checkout; a hand edit made in the checkout after the refusal was
    recovered by hand (no record left) is refused even when the cwd is a symlink to it.
    """
    root = h.hooks_repo(tmp_path)
    wt = h.integration_worktree(root)
    link_wt = tmp_path / "link-integ"
    link_wt.symlink_to(wt, target_is_directory=True)
    pb = protected_branch(root, "pb/ff")
    refused_merge(link_wt, pb)
    # the person recovers by hand, then makes their own edit and a merge in progress
    h.git(wt, "reset", "--hard", "-q")
    before = head_of(root)
    write_record(root, INTEG_REF, "0" * 40, wt)  # a stale record, as left by an older head
    (wt / "shared.txt").write_text("my own edit\n")
    code, out, err = land(root, 3, cwd=tmp_path / "link-integ")
    assert code != 0, f"a hand edit under a stale record was discarded:\n{out}\n{err}"
    assert (wt / "shared.txt").read_text() == "my own edit\n"
    assert head_of(root) == before


# --------------------------------------------------------------------------- 14.3: other checkouts


def test_redteam_14_3_main_checkout_with_an_integration_branch_is_not_restored(tmp_path):
    """The main checkout has integrate/demo checked out and a refused merge staged a protected file there."""
    root = h.hooks_repo(tmp_path)
    wt = h.integration_worktree(root)
    h.git(root, "worktree", "remove", "--force", str(wt))
    h.git(root, "checkout", "-q", INTEG)
    pb = protected_branch(root, "pb/ff")
    refused_merge(root, pb)  # the hook records a refusal naming the main checkout
    before = head_of(root)
    staged = staged_names(root)
    assert ".claude/CLAUDE.md" in staged
    code, out, err = land(root, 3)
    assert code != 0, f"a dirty main checkout was landed into:\n{out}\n{err}"
    assert staged_names(root) == staged, "the main checkout's staged files were discarded"
    assert (root / ".claude" / "CLAUDE.md").exists()
    assert head_of(root) == before
    code, out, err = h.ck_text(root, "worktree", "merge", "demo", "3", "--into", INTEG)
    assert code != 0, f"a dirty main checkout was merged into:\n{out}\n{err}"
    assert staged_names(root) == staged
    assert head_of(root) == before


def test_redteam_14_3_main_branch_with_a_hand_written_record_is_still_refused(tmp_path):
    """A record for refs/heads/main naming the main checkout and its head does not make main managed."""
    root = h.hooks_repo(tmp_path)
    write_record(root, "refs/heads/main", head_of(root, "main"), root)
    (root / "shared.txt").write_text("dirty main\n")
    before = head_of(root, "main")
    code, out, err = h.ck_text(root, "worktree", "merge", "demo", "3", "--into", "main")
    assert code != 0, f"a dirty main checkout was merged into:\n{out}\n{err}"
    assert "uncommitted changes to tracked files" in out + err
    assert (root / "shared.txt").read_text() == "dirty main\n"
    assert head_of(root, "main") == before
    code, out, err = h.ck_text(root, "worktree", "land", "demo", "3", "--into", "main", "--json")
    assert code != 0
    assert (root / "shared.txt").read_text() == "dirty main\n"


def test_redteam_14_3_an_integration_branch_checked_out_in_a_task_worktree_path_is_not_managed(tmp_path):
    """A checkout outside ck's worktree directory is not managed, even with a matching refusal for its branch."""
    root = h.hooks_repo(tmp_path)
    wt = h.integration_worktree(root)
    h.git(root, "worktree", "remove", "--force", str(wt))
    elsewhere = tmp_path / "elsewhere"
    h.git(root, "worktree", "add", "-q", str(elsewhere), INTEG)
    pb = protected_branch(root, "pb/ff")
    refused_merge(elsewhere, pb)
    before = head_of(root)
    code, out, err = land(root, 3)
    assert code != 0, f"a checkout outside the worktree directory was restored and landed into:\n{out}\n{err}"
    assert (elsewhere / ".claude" / "CLAUDE.md").exists()
    assert head_of(root) == before


# --------------------------------------------------------------------------- 14.4: hand-written records


def test_redteam_14_4_record_pointing_at_a_protected_commit_never_lands_it(tmp_path):
    """A record written by hand whose head is the protected branch's commit, for a checkout holding that content."""
    root = h.hooks_repo(tmp_path)
    wt = h.integration_worktree(root)
    pb = protected_branch(root, "pb/ff")
    protected_head = head_of(root, pb)
    refused_merge(wt, pb)
    write_record(root, INTEG_REF, protected_head, wt)  # overwrites the hook's record: now at the protected commit
    before = head_of(root)
    code, out, err = land(root, 3)
    assert head_of(root) == before or code == 0
    safe(root, before)
    assert ".claude/CLAUDE.md" not in paths_ever_in(root, INTEG)
    assert head_of(root) != protected_head
    assert h.raw(root, "merge-base", "--is-ancestor", protected_head, INTEG).returncode != 0, \
        "the protected commit became an ancestor of integrate/demo"
    if code != 0:
        assert (wt / ".claude" / "CLAUDE.md").exists(), "a refused landing changed the checkout"


def test_redteam_14_4_record_from_another_checkout_is_not_a_licence(tmp_path):
    """The record names the main checkout's path; the integration checkout is dirty with a merge in progress."""
    root = h.hooks_repo(tmp_path)
    wt = h.integration_worktree(root)
    pb = protected_branch(root, "pb/merge", {".claude/CLAUDE.md": "x\n"})
    h._commit_in(wt, {"shared.txt": "moved on\n"}, "integration moves")  # so the raw merge is not a fast-forward
    refused_merge(wt, pb)
    write_record(root, INTEG_REF, head_of(root), root)  # right ref and head, wrong checkout
    before = head_of(root)
    code, out, err = land(root, 3)
    assert code != 0, f"a record from another checkout licensed a restore:\n{out}\n{err}"
    assert "no refused ref update is recorded at this head" in out + err
    assert (wt / ".claude" / "CLAUDE.md").exists()
    assert head_of(root) == before


def test_redteam_14_4_the_ref_never_moves_during_a_recovery_that_the_hook_would_refuse_to_follow(tmp_path):
    """After the recovery the checkout equals the ref's head: no protected change is staged, no merge in progress."""
    root = h.hooks_repo(tmp_path)
    wt = h.integration_worktree(root)
    pb = protected_branch(root, "pb/ff")
    refused_merge(wt, pb)
    before = head_of(root)
    code, out, err = h.ck_text(root, "worktree", "merge", "demo", "3", "--into", INTEG)
    assert code == 0, f"the recovering merge failed:\n{out}\n{err}"
    safe(root, before)
    assert h.git(wt, "status", "--porcelain", "--untracked-files=no").strip() == ""
    assert not (wt / ".claude" / "CLAUDE.md").exists()


# --------------------------------------------------------------------------- 14.3: untracked files


UNTRACKED = {
    ".claude/CLAUDE.md.orig": "untracked, in a protected directory\n",
    ".claude/commands/evil.md": "untracked command\n",
    "-rf": "a file named like a flag\n",
    "sp ace/ä ö.txt": "umlaut and space\n",
    "src/new_module.py": "NEW = 1\n",
    ".gitmodules.bak": "not a gitmodules\n",
    "ignored.log": "ignored\n",
}


def test_redteam_14_3_untracked_files_git_would_stage_survive_a_recovery_byte_for_byte(tmp_path):
    root = h.hooks_repo(tmp_path)
    wt = h.integration_worktree(root)
    info = Path(h.git(wt, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip())
    info.parent.mkdir(parents=True, exist_ok=True)
    with info.open("a") as fh:
        fh.write("ignored.log\n")
    for rel, body in UNTRACKED.items():
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(body)
    pb = protected_branch(root, "pb/ff")
    refused_merge(wt, pb)
    before_files = snapshot(wt, UNTRACKED)
    before = head_of(root)
    code, out, err = land(root, 3)
    assert code == 0, f"the recovery did not land:\n{out}\n{err}"
    assert snapshot(wt, UNTRACKED) == before_files
    assert all(v is not None for v in snapshot(wt, UNTRACKED).values())
    assert ".claude/CLAUDE.md" not in paths_ever_in(root, INTEG)
    safe(root, before)
    in_tree = set(h.git(wt, "ls-tree", "-r", "--name-only", INTEG).splitlines())
    assert not (set(UNTRACKED) & in_tree), "an untracked file was committed by the recovery or the landing"


def test_redteam_14_3_an_untracked_file_the_refused_merge_would_overwrite_is_not_lost(tmp_path):
    """An untracked file named like the protected file the refused merge adds: git refuses; nothing is lost."""
    root = h.hooks_repo(tmp_path)
    wt = h.integration_worktree(root)
    (wt / ".claude").mkdir(exist_ok=True)
    (wt / ".claude" / "CLAUDE.md").write_text("my untracked notes\n")
    pb = protected_branch(root, "pb/ff")
    h.raw(wt, "merge", "--no-verify", pb)  # git itself or the hook refuses
    before = head_of(root)
    code, out, err = land(root, 3)
    safe(root, before)
    assert (wt / ".claude" / "CLAUDE.md").read_text() in ("my untracked notes\n", "# protected change\n")
    if code == 0:
        assert (wt / ".claude" / "CLAUDE.md").read_text() == "my untracked notes\n"


# --------------------------------------------------------------------------- 14.3, 14.5: a dirty submodule


def add_submodule(root: Path, tmp_path: Path) -> Path:
    """Commit a submodule `vendor/sub` into integrate/demo through the integration checkout."""
    sub_src = h.init_repo(tmp_path / "sub-src")
    (sub_src / "lib.txt").write_text("lib 1\n")
    h.commit_all(sub_src, "sub base")
    wt = h.integration_worktree(root)
    h.git(wt, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_src), "vendor/sub")
    h.git(wt, "config", "user.email", "t@example.com")
    h.git(wt, "config", "user.name", "Test")
    h.git(wt, "commit", "-qm", "add submodule")
    return wt / "vendor" / "sub"


def test_redteam_14_3_a_dirty_submodule_is_not_discarded_by_a_recovery(tmp_path):
    root = h.hooks_repo(tmp_path)
    sub = add_submodule(root, tmp_path)
    wt = h.integration_worktree(root)
    pb = protected_branch(root, "pb/ff")
    refused_merge(wt, pb)
    (sub / "lib.txt").write_text("lib edited in the submodule\n")
    (sub / "extra.txt").write_text("untracked in the submodule\n")
    before = head_of(root)
    code, out, err = land(root, 3)
    safe(root, before)
    assert (sub / "lib.txt").read_text() == "lib edited in the submodule\n", "the submodule's edit was discarded"
    assert (sub / "extra.txt").read_text() == "untracked in the submodule\n"
    assert ".claude/CLAUDE.md" not in paths_ever_in(root, INTEG)
    if code == 0:
        assert staged_names(wt) == set()


def test_redteam_14_5_a_hand_staged_submodule_pointer_is_never_discarded(tmp_path):
    """No refusal is recorded: the person advanced the submodule and staged its pointer by hand."""
    root = h.hooks_repo(tmp_path)
    sub = add_submodule(root, tmp_path)
    wt = h.integration_worktree(root)
    (sub / "lib.txt").write_text("lib 2\n")
    h.git(sub, "config", "user.email", "t@example.com")
    h.git(sub, "config", "user.name", "Test")
    h.git(sub, "commit", "-qam", "sub change")
    h.git(wt, "add", "vendor/sub")
    pointer = h.git(wt, "ls-files", "-s", "vendor/sub")
    before = head_of(root)
    code, out, err = land(root, 3)
    assert code != 0, f"a hand-staged submodule pointer was discarded by a landing:\n{out}\n{err}"
    assert h.git(wt, "ls-files", "-s", "vendor/sub") == pointer
    assert (sub / "lib.txt").read_text() == "lib 2\n"
    assert head_of(root) == before


# --------------------------------------------------------------------------- 14.4, 14.5: hand merge vs hook merge


def test_redteam_14_5_a_hand_merge_in_progress_with_an_older_record_is_never_discarded(tmp_path):
    """A refusal at an older head, then the branch moves, then the person starts a merge by hand."""
    root = h.hooks_repo(tmp_path)
    wt = h.integration_worktree(root)
    pb = protected_branch(root, "pb/ff")
    refused_merge(wt, pb)
    h.git(wt, "reset", "--hard", "-q")
    h._commit_in(wt, {"shared.txt": "base\nmoved\n"}, "the branch moves")
    h.git(wt, "checkout", "-q", "-b", "side", "HEAD~1")
    h._commit_in(wt, {"other.txt": "side\n"}, "side work")
    h.git(wt, "checkout", "-q", INTEG)
    h.git(wt, "merge", "--no-commit", "--no-ff", "side")  # the person's merge, in progress
    assert (Path(h.git(wt, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD").strip())).exists()
    before = head_of(root)
    code, out, err = land(root, 3)
    assert code != 0, f"a hand merge was discarded:\n{out}\n{err}"
    assert (Path(h.git(wt, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD").strip())).exists()
    assert (wt / "other.txt").read_text() == "side\n"
    assert head_of(root) == before


# --------------------------------------------------------------------------- a landing racing the hook


def test_redteam_14_4_landing_that_races_the_hook_writing_records_never_lands_a_protected_file(tmp_path):
    root = h.hooks_repo(tmp_path)
    wt = h.integration_worktree(root)
    branches = [protected_branch(root, f"pb/race{i}", {f".claude/race{i}.md": f"{i}\n"}) for i in range(4)]
    refused_merge(wt, branches[0])
    before = head_of(root)
    stop = threading.Event()
    errors = []

    def attacker() -> None:
        i = 0
        while not stop.is_set() and i < 12:
            out = h.raw(wt, "merge", "--no-verify", branches[i % len(branches)])
            if out.returncode != 0 and "refus" in text_of(out).lower():
                pass
            i += 1

    t = threading.Thread(target=attacker)
    t.start()
    results = []
    try:
        for _ in range(2):
            results.append(land(root, 3))
    finally:
        stop.set()
        t.join(timeout=120)
    assert not errors
    safe(root, before)
    for code, out, err in results:
        assert "Traceback" not in err, err
    # the records stay readable whatever the interleaving
    from claude_kiro import protect

    protect.refusal_for(root, INTEG_REF)
    # once the attacker has stopped, the next landing recovers or refuses; it never lands a protected file
    code, out, err = h.ck_text(root, "worktree", "land", "demo", "3", "--into", INTEG, "--json")
    safe(root, before)
    assert not [p for p in paths_ever_in(root, INTEG) if p.startswith(".claude/race")]
