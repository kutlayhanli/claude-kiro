"""Landing recovery (run-principles Story 14): TC-162 to TC-168, TC-196, TC-197 and P-47.

Every case is built on a real repository with ck's git hooks installed (`hooks_repo`) and a real integration
worktree. The refused merge is a real `git merge --no-verify` refused by the real installed `reference-transaction`
hook, so the residue in the checkout is exactly what git leaves. The refusal record is read only through
`protect.refusal_for`, the landing result only through `ck worktree land --json`. Project imports happen inside the
test bodies, so this file collects before the implementation exists.

`tc168_` tests are the hook side only: none of them runs `ck worktree merge` or `ck worktree land` after a refused
merge, so they do not depend on the recovery.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

import unattended_helpers as h

REF = "refs/heads/integrate/demo"
PROTECTED = ".claude/CLAUDE.md"


# --------------------------------------------------------------------------- scenario builders


def protected_branch(root: Path, name: str = "feat/demo-protected") -> str:
    """A branch off integrate/demo whose only change adds `.claude/CLAUDE.md`; returns its name."""
    wt = root.parent / f"{name.replace('/', '-')}-wt"
    h.git(root, "worktree", "add", "-q", "-b", name, str(wt), "integrate/demo")
    (wt / ".claude").mkdir(exist_ok=True)
    (wt / ".claude" / PROTECTED.split("/")[1]).write_text("# changed behind ck's back\n")
    h.commit_all(wt, "protected change")
    h.git(root, "worktree", "remove", "--force", str(wt))
    return name


def diverge(integ: Path) -> None:
    """One ordinary commit on integrate/demo, so a merge of an older branch is not a fast-forward."""
    (integ / "src" / "extra.py").write_text("EXTRA = 1\n")
    h.commit_all(integ, "extra work on integration")


def refused_merge(tmp_path: Path, ff: bool) -> Dict[str, Any]:
    """The repo, a refused raw `git merge --no-verify` of a protected branch into the integration checkout."""
    root = h.hooks_repo(tmp_path)
    integ = h.integration_worktree(root)
    branch = protected_branch(root)
    if not ff:
        diverge(integ)
    head = h.ref(root, "integrate/demo")
    out = h.raw(integ, "merge", "--no-verify", "--no-edit", branch)
    assert out.returncode != 0, f"the raw merge was not refused: {out.stdout} {out.stderr}"
    assert h.ref(root, "integrate/demo") == head, "the refused merge moved integrate/demo"
    return {"root": root, "integ": integ, "branch": branch, "head": head}


def git_path(checkout: Path, name: str) -> Path:
    return Path(h.git(checkout, "rev-parse", "--path-format=absolute", "--git-path", name).strip())


def status(checkout: Path) -> List[str]:
    return [line for line in h.git(checkout, "status", "--porcelain").splitlines() if line]


def tree_paths(root: Path, branch: str = "integrate/demo") -> List[str]:
    return h.git(root, "ls-tree", "-r", "--name-only", branch).splitlines()


def land(root: Path, *tasks: str, into: Optional[str] = None, gate: bool = False) -> Tuple[int, Any, str]:
    args = ["worktree", "land", "demo", *tasks]
    if into:
        args += ["--into", into]
    if gate:
        args.append("--gate")
    return h.ck_json(root, *args, "--json")


def recovered_of(obj: Any) -> Optional[Dict[str, Any]]:
    """`recovered` on the land result, or on one of its entries."""
    if not isinstance(obj, dict):
        return None
    if obj.get("recovered"):
        return obj["recovered"]
    for entry in obj.get("tasks", []):
        if isinstance(entry, dict) and entry.get("recovered"):
            return entry["recovered"]
    return None


def refusal(root: Path):
    from claude_kiro import protect

    return protect.refusal_for(root, REF)


# --------------------------------------------------------------------------- TC-162, TC-163


def test_tc162_fast_forward_refusal_is_recovered_by_the_next_landing(tmp_path):
    s = refused_merge(tmp_path, ff=True)
    root, integ, head = s["root"], s["integ"], s["head"]
    assert f"A  {PROTECTED}" in status(integ), status(integ)
    code, obj, err = land(root, "3")
    assert code == 0 and obj is not None, f"landing failed ({code}): {err}"
    assert [t["state"] for t in obj["tasks"]] == ["MERGED"], obj
    rec = recovered_of(obj)
    assert rec is not None, f"no `recovered` in the landing result: {obj}"
    assert PROTECTED in rec["paths"], rec
    assert rec["mergeInProgress"] is False, rec
    assert rec["head"] == head, rec
    assert PROTECTED not in tree_paths(root)
    assert not (integ / PROTECTED).exists()


def test_tc163_merge_commit_refusal_is_recovered_with_the_merge_state_cleared(tmp_path):
    s = refused_merge(tmp_path, ff=False)
    root, integ, head = s["root"], s["integ"], s["head"]
    merge_head = git_path(integ, "MERGE_HEAD")
    assert merge_head.exists(), "the refused merge left no MERGE_HEAD, so there is nothing to recover from"
    code, obj, err = land(root, "3")
    assert not code and obj is not None, f"landing failed ({code}): {err}"
    assert [t["state"] for t in obj["tasks"]] == ["MERGED"], obj
    rec = recovered_of(obj)
    assert rec is not None, f"no `recovered` in the landing result: {obj}"
    assert rec["mergeInProgress"] is True, rec
    assert rec["head"] == head, rec
    for name in ("MERGE_HEAD", "MERGE_MSG", "AUTO_MERGE"):
        assert not git_path(integ, name).exists(), f"{name} is still there after the recovery"
    assert PROTECTED not in tree_paths(root)


# --------------------------------------------------------------------------- TC-164, TC-165


def test_tc164_untracked_and_ignored_files_survive_the_recovery(tmp_path):
    root = h.hooks_repo(tmp_path)
    integ = h.integration_worktree(root)
    branch = protected_branch(root)
    untracked = integ / "notes.txt"
    untracked.write_bytes(b"untracked \xc3\xa4 bytes\n\x00\x01")
    ignored = integ / ".venv" / "lib" / "ignored.bin"
    ignored.parent.mkdir(parents=True)
    ignored.write_bytes(b"ignored\x00\xff bytes\n")
    assert h.raw(integ, "check-ignore", "-q", ".venv/lib/ignored.bin").returncode == 0, "the file is not ignored"
    before = {p: p.read_bytes() for p in (untracked, ignored)}
    assert h.raw(integ, "merge", "--no-verify", "--no-edit", branch).returncode != 0
    code, obj, err = land(root, "3")
    assert code == 0 and recovered_of(obj), f"no recovering landing ({code}): {obj} {err}"
    for path, data in before.items():
        assert path.exists() and path.read_bytes() == data, f"{path} changed"


def test_tc165_a_dirty_main_checkout_still_refuses_a_merge(tmp_path):
    root = h.hooks_repo(tmp_path)
    (root / "shared.txt").write_text("edited by hand\n")
    out = h.ck_run(root, "worktree", "merge", "demo", "3")
    assert out.returncode != 0, f"the merge into a dirty main checkout succeeded: {out.stdout}"
    assert "has uncommitted changes to tracked files" in out.stdout + out.stderr, out.stdout + out.stderr
    assert (root / "shared.txt").read_text() == "edited by hand\n"
    assert h.ref(root, "feat/demo-task-3"), "the task branch is gone: the merge ran"


# --------------------------------------------------------------------------- TC-166

NO_RECORD = "no refused ref update is recorded at this head"


def hand_edit_with_open_merge(root: Path, integ: Path) -> Tuple[str, str]:
    """A hand edit to a tracked file and a merge in progress made without any refusal (`--no-commit`)."""
    h.git(root, "branch", "feat/demo-side", "integrate/demo")
    side = root.parent / "side-wt"
    h.git(root, "worktree", "add", "-q", str(side), "feat/demo-side")
    (side / "side.txt").write_text("side\n")
    h.commit_all(side, "side change")
    h.git(root, "worktree", "remove", "--force", str(side))
    return "feat/demo-side", "src/a.py"


def open_merge_and_edit(root: Path, integ: Path) -> None:
    side, edited = hand_edit_with_open_merge(root, integ)
    out = h.raw(integ, "merge", "--no-ff", "--no-commit", side)
    assert out.returncode == 0, out.stdout + out.stderr
    assert git_path(integ, "MERGE_HEAD").exists()
    (integ / edited).write_text("A = 'hand edit'\n")


def assert_land_refuses_untouched(root: Path, integ: Path, head: str) -> None:
    code, out, err = h.ck_text(root, "worktree", "land", "demo", "3", "--json")
    assert code != 0, f"the landing went ahead: {out}"
    assert NO_RECORD in out + err, f"expected {NO_RECORD!r} in: {out} {err}"
    assert (integ / "src" / "a.py").read_text() == "A = 'hand edit'\n", "the hand edit was discarded"
    assert git_path(integ, "MERGE_HEAD").exists(), "MERGE_HEAD was discarded"
    assert h.ref(root, "integrate/demo") == head, "integrate/demo moved"


def test_tc166_no_refusal_recorded_is_not_discarded(tmp_path):
    root = h.hooks_repo(tmp_path)
    integ = h.integration_worktree(root)
    open_merge_and_edit(root, integ)
    assert refusal(root) is None
    assert_land_refuses_untouched(root, integ, h.ref(root, "integrate/demo"))


def test_tc166_a_refusal_at_an_older_head_is_not_discarded(tmp_path):
    s = refused_merge(tmp_path, ff=True)
    root, integ = s["root"], s["integ"]
    assert refusal(root) is not None, "the refused merge left no record, so there is no older record to test"
    # the branch moves on (an allowed update), then the checkout gets the hand edit and an open merge
    h.git(integ, "reset", "-q", "--hard", "HEAD")
    new = h.git(integ, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "moved on").strip()
    h.git(root, "update-ref", REF, new)
    open_merge_and_edit(root, integ)
    rec = refusal(root)
    assert rec is None or rec.head != h.ref(root, "integrate/demo"), "the record still names the current head"
    assert_land_refuses_untouched(root, integ, new)


def test_tc166_a_refusal_from_another_checkout_is_not_discarded(tmp_path):
    root = h.hooks_repo(tmp_path)
    integ = h.integration_worktree(root)
    branch = protected_branch(root)
    head = h.ref(root, "integrate/demo")
    # a protected update of the ref from the main checkout, refused by the hook there
    out = h.raw(root, "update-ref", REF, h.ref(root, branch))
    assert out.returncode != 0, "the protected ref update was accepted"
    rec = refusal(root)
    assert rec is not None and rec.head == head, f"no refusal recorded at the current head: {rec}"
    assert os.path.realpath(rec.checkout) != os.path.realpath(integ), rec
    open_merge_and_edit(root, integ)
    assert_land_refuses_untouched(root, integ, head)


# --------------------------------------------------------------------------- TC-167


def test_tc167_a_held_index_lock_fails_the_recovery_and_keeps_the_record(tmp_path):
    s = refused_merge(tmp_path, ff=True)
    root, integ, head = s["root"], s["integ"], s["head"]
    lock = git_path(integ, "index.lock")
    lock.write_text("")
    try:
        code, out, err = h.ck_text(root, "worktree", "land", "demo", "3", "--json")
        assert code != 0, f"the landing went ahead with the index locked: {out}"
        text = out + err
        assert str(integ) in text, f"the target {integ} is not named: {text}"
        assert "index.lock" in text, f"the git error is not named: {text}"
        assert h.ref(root, "integrate/demo") == head, "integrate/demo moved"
        rec = refusal(root)
        assert rec is not None and rec.head == head, "the refusal record is gone"
    finally:
        lock.unlink()
    code, obj, err = land(root, "3")
    assert code == 0 and recovered_of(obj), f"the next landing did not recover ({code}): {obj} {err}"
    assert PROTECTED not in tree_paths(root)


# --------------------------------------------------------------------------- TC-168 (hook side only)


@pytest.mark.parametrize("ff", [True, False], ids=["fast-forward", "merge-commit"])
def test_tc168_refused_raw_merge_leaves_a_record_naming_head_and_checkout(tmp_path, ff):
    s = refused_merge(tmp_path, ff=ff)
    rec = refusal(s["root"])
    assert rec is not None, "the refused merge left no refusal record"
    assert rec.ref == REF
    assert rec.head == s["head"], rec
    assert rec.checkout == os.path.realpath(s["integ"]), rec


def test_tc168_a_tokened_landing_leaves_no_record(tmp_path):
    root = h.hooks_repo(tmp_path)
    assert refusal(root) is None
    code, obj, err = land(root, "3")
    assert code == 0 and [t["state"] for t in obj["tasks"]] == ["MERGED"], f"{code} {obj} {err}"
    assert refusal(root) is None, "an allowed, tokened update left a refusal record"


# --------------------------------------------------------------------------- TC-196


@pytest.mark.parametrize("ff", [True, False], ids=["fast-forward", "merge-commit"])
def test_tc196_ck_worktree_merge_restores_and_says_so_first(tmp_path, ff):
    s = refused_merge(tmp_path, ff=ff)
    root, integ, head = s["root"], s["integ"], s["head"]
    code, out, err = h.ck_text(root, "worktree", "merge", "demo", "3", "--into", "integrate/demo")
    assert code == 0, f"the merge failed ({code}): {out} {err}"
    lines = [line for line in out.splitlines() if line.strip()]
    expected = f"restored {integ} to {head}: discarded {PROTECTED}"
    assert lines and expected in lines[0], f"first line is not the restore line {expected!r}: {out}"
    if ff:
        assert "a merge was in progress" not in lines[0], lines[0]
    else:
        assert lines[0].endswith("; a merge was in progress"), lines[0]
    assert refusal(root) is None, "the refusal record is still there after the recovery"
    assert PROTECTED not in tree_paths(root)


# --------------------------------------------------------------------------- TC-197

CLAIM = {"task": "3", "trigger": "test-conflict", "text": "the test contradicts criterion 1.1",
         "file": "tests/test_a.py", "test": "test_a", "criterion": "1.1"}


def spec_edit_through_arbiter(root: Path, integ: Path) -> str:
    """`ck arbiter open` and `settle` for a spec-edit ruling, then a commit on the arbiter branch. Returns `arbiter<k>`."""
    code, obj, err = h.ck_json(root, "principles", "init", "--commit", "--json")
    assert code == 0, f"ck principles init failed ({code}): {err}"
    # the principles file is on the main branch; bring it to integration through an ordinary landing-free merge
    h.git(integ, "merge", "-q", "--no-edit", "main")
    code, opened, err = h.ck_json(root, "arbiter", "open", "demo", "--task", "3", "--into", "integrate/demo", "--json",
                                  input=json.dumps({"claim": CLAIM, "task": {"risk": "normal", "complexity": "Low"}}))
    assert code == 0 and opened is not None, f"ck arbiter open failed ({code}): {err}"
    assert opened["action"] == "rule", opened
    ruling = {"claim": opened["key"], "status": "ruled", "outcome": "spec_edit", "reasoning": "the spec is unclear",
              "principle": opened["principles"][0]["id"], "changed": "design.md note"}
    code, settled, err = h.ck_json(root, "arbiter", "settle", "demo", opened["key"], "--tier", "normal", "--json",
                                   input=json.dumps(ruling))
    assert code == 0 and settled is not None and settled["route"] == "spec_edit", f"{code} {settled} {err}"
    worktree = Path(opened["worktree"])
    worktree = worktree if worktree.is_absolute() else root / worktree
    (worktree / "specs" / "demo" / "design.md").write_text("# Design\n\nA clarified note.\n")
    h.commit_all(worktree, "spec edit")
    return opened["number"]


def test_tc197_spec_edit_landing_and_arbiter_record_restore_the_checkout(tmp_path):
    s = refused_merge(tmp_path, ff=True)
    root, integ, branch = s["root"], s["integ"], s["branch"]
    # the checkout is dirty from the refused merge before the arbiter opens
    number = spec_edit_through_arbiter(root, integ)
    assert any(PROTECTED in line for line in status(integ)), "the refused merge left the checkout clean"
    code, obj, err = land(root, number, into="integrate/demo", gate=True)
    assert code == 0 and obj is not None, f"the arbiter landing failed ({code}): {err} {obj}"
    rec = recovered_of(obj)
    assert rec is not None and PROTECTED in rec["paths"], f"no `recovered` naming {PROTECTED}: {obj}"
    assert PROTECTED not in tree_paths(root)
    assert "A clarified note." in h.git(root, "show", "integrate/demo:specs/demo/design.md")

    out = h.raw(integ, "merge", "--no-verify", "--no-edit", branch)
    assert out.returncode != 0, "the second raw merge was not refused"
    record = {"trigger": "test-conflict", "claim_key": "3|test-conflict|tests/test_a.py", "claim_text": CLAIM["text"],
              "task": "3", "tier": "normal", "outcome": "spec_edit", "reasoning": "the spec is unclear",
              "principle_id": "P1", "principle_text": "text", "plan_layer": "absent", "changed": "design.md note",
              "moved": [], "commit": None, "landed": None}
    code, rec_out, err = h.ck_json(root, "arbiter", "record", "demo", "--into", "integrate/demo", "--json",
                                   input=json.dumps(record))
    assert code == 0 and rec_out is not None, f"ck arbiter record failed ({code}): {err}"
    restored = rec_out.get("recovered")
    assert restored and PROTECTED in restored["paths"], f"record did not report the recovery: {rec_out}"
    assert "specs/demo/decisions.md" in tree_paths(root), "decisions.md was not committed on integrate/demo"
    assert PROTECTED not in tree_paths(root)
    assert status(integ) == [], status(integ)


# --------------------------------------------------------------------------- P-47

PROPERTIES = {"P-47": "test_p47_recovery_only_for_a_managed_checkout_with_a_matching_refusal"}


def test_p47_recovery_only_for_a_managed_checkout_with_a_matching_refusal():
    from hypothesis import given, settings
    from hypothesis import strategies as st

    from claude_kiro.protect import Refusal
    from claude_kiro.worktrees import RecoveryFacts, decide_recovery

    into, target, head = "integrate/demo", "/work/demo-integration", "a" * 40

    def make(kind: str):
        if kind == "absent":
            return None
        ref = "refs/heads/integrate/other" if kind == "other-ref" else f"refs/heads/{into}"
        rhead = "b" * 40 if kind == "other-head" else head
        checkout = "/elsewhere/checkout" if kind == "other-checkout" else target
        return Refusal(ref=ref, head=rhead, checkout=checkout, at="2026-11-11T00:00:00Z")

    @settings(derandomize=True, max_examples=200, database=None, deadline=None)
    @given(managed=st.booleans(), dirty=st.booleans(), merging=st.booleans(),
           kind=st.sampled_from(["absent", "other-ref", "other-head", "other-checkout", "matching"]))
    def check(managed, dirty, merging, kind):
        facts = RecoveryFacts(into=into, target=target, managed=managed,
                              dirty=("src/a.py",) if dirty else (), merge_in_progress=merging,
                              head=head, refusal=make(kind))
        verdict, reason = decide_recovery(facts)
        if not dirty and not merging:
            assert verdict == "clean", (facts, verdict)
        elif managed and kind == "matching":
            assert verdict == "recover", (facts, verdict)
        else:
            assert verdict == "refuse", (facts, verdict)
            assert reason, "a refusal carries a reason"

    check()
