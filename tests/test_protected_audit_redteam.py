"""Red team for Story 10's SHALL NOT clause (requirement 10.5, TC-100): an audit that cannot read the approved
state SHALL NOT report the run clean.

Written from the requirements only. Every case damages, replaces or hides ck's approved-state record for
`integrate/demo` (found by diffing `snapshot_ck_state` before and after `ck worktree integration`, never by a
path the tests assume), then runs the real `ck worktree audit demo --json` on a real repo. Each case must give
`clean: false` and either `complete: false` (exit 3) or reported drift, never `clean: true`.
"""

import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

from unattended_helpers import (
    ck_json, ck_text, commit_all, git, init_repo, integration_worktree, raw, ref, snapshot_ck_state)

# 'record' = the file that holds the approved protected state of integrate/demo.


def _base_repo(root: Path) -> Path:
    init_repo(root)
    (root / ".gitignore").write_text(".venv/\n.ck/worktrees/\n")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("A = 1\n")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text('{"permissions": {}}\n')
    (root / ".mcp.json").write_text("{}\n")
    for spec in ("demo", "other"):
        d = root / "specs" / spec
        d.mkdir(parents=True)
        (d / "tasks.md").write_text(
            f"# Implementation Tasks: {spec}\n\n## Task Breakdown\n\n"
            "### Task 1: one\n**Status:** Not Started\n**Track:** impl\n**Dependencies:** None\n\n")
    commit_all(root, "base")
    return root


def _integrate(root: Path, spec: str) -> Dict[str, bytes]:
    """Run `ck worktree integration SPEC`; return the state files it added (path -> bytes)."""
    before = snapshot_ck_state(root)
    code, _, err = ck_text(root, "worktree", "integration", spec)
    if code != 0:
        raise RuntimeError(f"ck worktree integration {spec} failed ({code}): {err}")
    after = snapshot_ck_state(root)
    return {k: v for k, v in after.items() if k not in before}


def _record_of(added: Dict[str, bytes], spec: str) -> Path:
    """The approved-state record among the files `integration` added: the one named for the protected state."""
    cands = [k for k in added if "protected" in Path(k).name.lower() and f"/{spec}/" in k]
    assert cands, (f"`ck worktree integration {spec}` wrote no approved-state record "
                   f"(added files: {sorted(added)})")
    return Path(sorted(cands, key=len)[0])


@pytest.fixture
def repo(tmp_path):
    root = _base_repo(tmp_path / "repo")
    added_demo = _integrate(root, "demo")
    rec_demo = _record_of(added_demo, "demo")
    rec_other = _record_of(_integrate(root, "other"), "other")
    return {"root": root, "record": rec_demo, "other_record": rec_other, "added_demo": added_demo}


def _audit(root: Path) -> Tuple[int, Any, str]:
    return ck_json(root, "worktree", "audit", "demo", "--json")


def _never_clean(root: Path, *, expect_incomplete: bool = True) -> Dict[str, Any]:
    code, obj, err = _audit(root)
    assert isinstance(obj, dict), f"audit printed no JSON (exit {code}): {err}"
    assert obj.get("clean") is False, f"audit reported clean (exit {code}): {obj}"
    assert code != 0, f"audit exited 0 though it is not clean: {obj}"
    if obj.get("complete") is False:
        assert code == 3, f"incomplete audit must exit 3, got {code}: {obj}"
        assert obj.get("problems"), f"incomplete audit gave no reason: {obj}"
    elif expect_incomplete:
        assert obj.get("drift"), f"audit is complete, not clean, and reports no drift: {obj}"
    return obj


def test_tc100_redteam_control_untouched_state_is_readable(repo):
    code, obj, err = _audit(repo["root"])
    assert isinstance(obj, dict), (code, err)
    assert obj.get("complete") is True and obj.get("clean") is True, obj


@pytest.mark.parametrize("name,payload", [
    ("empty", b""),
    ("one-byte", b"{"),
    ("whitespace", b"  \n\t\n"),
    ("null", b"null"),
    ("list", b"[]"),
    ("string", b'"approved"'),
    ("empty-object", b"{}"),
    ("number", b"0"),
    ("bom-only", b"\xef\xbb\xbf"),
    ("nul-bytes", b"\x00" * 64),
    ("not-utf8", b"\xff\xfe\xfa{\"files\": {}}"),
    ("nan", b'{"files": NaN}'),
    ("deep-nesting", b"[" * 5000 + b"]" * 5000),
    ("trailing-garbage", None),
    ("files-not-mapping", None),
    ("files-null", None),
])
def test_tc100_redteam_corrupt_record(repo, name, payload):
    rec: Path = repo["record"]
    if name == "trailing-garbage":
        payload = rec.read_bytes() + b"\n}{garbage"
    elif name in ("files-not-mapping", "files-null"):
        data = json.loads(rec.read_text())
        data["files"] = ["x"] if name == "files-not-mapping" else None
        payload = json.dumps(data).encode()
    rec.write_bytes(payload)
    obj = _never_clean(repo["root"])
    assert obj.get("complete") is False, obj


def test_tc100_redteam_truncated_every_prefix(repo):
    rec: Path = repo["record"]
    full = rec.read_bytes()
    assert len(full) > 4
    for cut in sorted({1, len(full) // 4, len(full) // 2, len(full) - 2, len(full) - 1}):
        rec.write_bytes(full[:cut])
        obj = _never_clean(repo["root"])
        assert obj.get("complete") is False, (cut, obj)


def test_tc100_redteam_record_deleted(repo):
    repo["record"].unlink()
    obj = _never_clean(repo["root"])
    assert obj.get("complete") is False, obj


def test_tc100_redteam_state_dir_moved(repo):
    state_dir = repo["record"].parent
    moved = state_dir.with_name(state_dir.name + "-moved")
    state_dir.rename(moved)
    obj = _never_clean(repo["root"])
    assert obj.get("complete") is False, obj


def test_tc100_redteam_state_dir_deleted_with_other_specs_left(repo):
    shutil.rmtree(repo["record"].parent)
    assert repo["other_record"].exists()
    obj = _never_clean(repo["root"])
    assert obj.get("complete") is False, obj


def test_tc100_redteam_every_added_state_file_removed(repo, tmp_path):
    """Every state file `integration demo` added under the spec's own directory: delete each one in turn."""
    root = repo["root"]
    victims = [Path(k) for k in repo["added_demo"] if Path(k).parent == repo["record"].parent]
    assert repo["record"] in victims
    for victim in victims:
        data = victim.read_bytes()
        victim.unlink()
        obj = _never_clean(root)
        assert obj.get("complete") is False, (str(victim), obj)
        victim.write_bytes(data)


def test_tc100_redteam_other_spec_record(repo):
    repo["record"].write_bytes(repo["other_record"].read_bytes())
    obj = _never_clean(repo["root"])
    assert obj.get("complete") is False, obj


def test_tc100_redteam_other_ref_rewritten_in_place(repo):
    text = repo["record"].read_text()
    assert "integrate/demo" in text
    repo["record"].write_text(text.replace("integrate/demo", "integrate/other"))
    obj = _never_clean(repo["root"])
    assert obj.get("complete") is False, obj


def test_tc100_redteam_other_spec_name_rewritten_in_place(repo):
    data = json.loads(repo["record"].read_text())
    swapped = json.loads(json.dumps(data).replace('"demo"', '"other"'))
    repo["record"].write_text(json.dumps(swapped))
    if swapped == data:
        # The record holds no spec-name value: this only reserialised it, which must not break the audit.
        code, obj, err = _audit(repo["root"])
        assert isinstance(obj, dict), (code, err)
        assert obj.get("complete") is True and obj.get("clean") is True, obj
        return
    obj = _never_clean(repo["root"])
    assert obj.get("complete") is False, obj


def test_tc100_redteam_unreadable_by_permissions(repo):
    rec: Path = repo["record"]
    rec.chmod(0)
    try:
        if os.access(rec, os.R_OK):
            # Running as a user the mode does not bind (root): the file really is readable, so
            # the honest outcome is a clean audit; nothing to attack here. Check the audit still runs.
            _, obj, _ = _audit(repo["root"])
            assert isinstance(obj, dict)
            return
        obj = _never_clean(repo["root"])
        assert obj.get("complete") is False, obj
    finally:
        rec.chmod(0o644)


def test_tc100_redteam_unreadable_state_dir(repo):
    d: Path = repo["record"].parent
    mode = stat.S_IMODE(d.stat().st_mode)
    d.chmod(0)
    try:
        if os.access(d, os.X_OK):
            _, obj, _ = _audit(repo["root"])
            assert isinstance(obj, dict)
            return
        obj = _never_clean(repo["root"])
        assert obj.get("complete") is False, obj
    finally:
        d.chmod(mode)


def test_tc100_redteam_record_is_a_directory(repo):
    rec: Path = repo["record"]
    rec.unlink()
    rec.mkdir()
    (rec / "protected.json").write_text("{}")
    obj = _never_clean(repo["root"])
    assert obj.get("complete") is False, obj


@pytest.mark.parametrize("kind", ["dangling", "loop", "dev-null", "directory", "non-json-file"])
def test_tc100_redteam_record_is_a_symlink(repo, tmp_path, kind):
    rec: Path = repo["record"]
    rec.unlink()
    if kind == "dangling":
        target = tmp_path / "does-not-exist"
    elif kind == "loop":
        target = rec
    elif kind == "dev-null":
        target = Path("/dev/null")
    elif kind == "directory":
        target = tmp_path
    else:
        target = tmp_path / "plain.txt"
        target.write_text("not json at all\n")
    os.symlink(target, rec)
    obj = _never_clean(repo["root"])
    assert obj.get("complete") is False, obj


def test_tc100_redteam_record_is_a_fifo(repo):
    rec: Path = repo["record"]
    rec.unlink()
    os.mkfifo(rec)
    # A reader that opens a FIFO blocks forever: the audit must give up with a verdict, not hang.
    import subprocess
    from unattended_helpers import CK_ARGV, _ck_env
    try:
        out = subprocess.run([*CK_ARGV, "worktree", "audit", "demo", "--json"], cwd=repo["root"],
                             env=_ck_env(repo["root"], None), capture_output=True, text=True, timeout=60,
                             stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pytest.fail("audit hung reading a FIFO as the approved state")
    obj = json.loads(out.stdout)
    assert obj.get("clean") is False and obj.get("complete") is False, obj
    assert out.returncode == 3


def _bypass_add(root: Path, rel: str, content: str) -> str:
    """Add a protected file to integrate/demo with hooks bypassed (a deliberate circumvention); returns the blob oid."""
    wt = integration_worktree(root)
    target = wt / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(wt, "add", "-A")
    git(wt, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "bypass: add protected file")
    return git(wt, "rev-parse", f"HEAD:{rel}").strip()


def test_tc100_redteam_hidden_drift_control(repo):
    """Control: the bypass is reported as drift while the record is untouched."""
    _bypass_add(repo["root"], ".claude/evil.md", "evil\n")
    code, obj, err = _audit(repo["root"])
    assert isinstance(obj, dict), (code, err)
    assert obj.get("clean") is False
    assert {"file": ".claude/evil.md", "change": "added"} in obj.get("drift", []), obj


def test_tc100_redteam_mac_matches_old_content_new_files(repo):
    """Bypass adds a protected file, then the record is edited to approve it but keeps its old seal."""
    root = repo["root"]
    oid = _bypass_add(root, ".claude/evil.md", "evil\n")
    data = json.loads(repo["record"].read_text())
    assert isinstance(data.get("files"), dict), data
    data["files"][".claude/evil.md"] = oid
    data["commit"] = ref(root, "integrate/demo")
    repo["record"].write_text(json.dumps(data, indent=2))
    obj = _never_clean(root, expect_incomplete=False)
    assert obj.get("complete") is False or obj.get("drift"), obj


def test_tc100_redteam_mac_matches_old_content_one_byte_flip(repo):
    data = json.loads(repo["record"].read_text())
    macs = [k for k in data if "mac" in k.lower() or "seal" in k.lower() or "sig" in k.lower()]
    assert macs, f"the approved state carries no mac field: {sorted(data)}"
    # same mac, whitespace-only reserialisation is fine; a changed commit with the old mac is not
    data["commit"] = "0" * 40
    repo["record"].write_text(json.dumps(data, indent=2))
    obj = _never_clean(repo["root"])
    assert obj.get("complete") is False, obj


def test_tc100_redteam_mac_field_removed_or_emptied(repo):
    data = json.loads(repo["record"].read_text())
    macs = [k for k in data if "mac" in k.lower() or "seal" in k.lower() or "sig" in k.lower()]
    assert macs, f"the approved state carries no mac field: {sorted(data)}"
    for mode in ("removed", "empty", "null", "number"):
        d = dict(data)
        for k in macs:
            if mode == "removed":
                del d[k]
            else:
                d[k] = {"empty": "", "null": None, "number": 0}[mode]
        repo["record"].write_text(json.dumps(d, indent=2))
        obj = _never_clean(repo["root"])
        assert obj.get("complete") is False, (mode, obj)


def test_tc100_redteam_key_replaced_and_record_resealed_unknown(repo):
    """The seal key is replaced (as if an agent re-keyed): the old record must no longer verify."""
    root = repo["root"]
    keys = [Path(k) for k in snapshot_ck_state(root) if Path(k).name == "key" and "/ck/" in k]
    assert keys, "ck wrote no seal key under its state directory"
    for key in keys:
        key.write_bytes(b"\x01" * max(len(key.read_bytes()), 32))
    obj = _never_clean(root)
    assert obj.get("complete") is False, obj


def test_tc100_redteam_older_snapshot_restored(repo):
    """After the approved state moves on (a landing), the record is rolled back to the older one."""
    root = repo["root"]
    old = repo["record"].read_bytes()
    # A landing through ck's own path moves the approved state: a plain task landing is enough.
    code, _, err = ck_text(root, "worktree", "create", "demo", "1", "--base", "integrate/demo")
    assert code == 0, err
    task_wt = root / ".ck" / "worktrees" / "demo-task-1"
    (task_wt / "src").mkdir(exist_ok=True)
    (task_wt / "src" / "a.py").write_text("A = 2\n")
    commit_all(task_wt, "task 1")
    code, out, err = ck_text(root, "worktree", "land", "demo", "1", "--json")
    assert code == 0, (out, err)
    assert ref(root, "integrate/demo") != ref(root, "main")
    after_landing = repo["record"].read_bytes()
    repo["record"].write_bytes(old)
    code, obj, err = _audit(root)
    assert isinstance(obj, dict), (code, err)
    if old == after_landing:
        return  # the landing changed no approved protected state, so there is nothing older to restore
    # An older, validly sealed state: the head moved past it, so it must not give a clean verdict
    # unless the state is the same protected content (no protected file differs).
    assert obj.get("drift") == [], obj


def test_tc100_redteam_older_snapshot_with_new_protected_drift(repo):
    """Protected drift is bypass-landed, then the record is replaced by the older snapshot taken before it."""
    root = repo["root"]
    old = repo["record"].read_bytes()
    _bypass_add(root, ".claude/evil.md", "evil\n")
    repo["record"].write_bytes(old)
    obj = _never_clean(root, expect_incomplete=False)
    assert {"file": ".claude/evil.md", "change": "added"} in obj.get("drift", []) or obj.get("complete") is False


def test_tc100_redteam_protected_file_changed_with_record_deleted(repo):
    root = repo["root"]
    _bypass_add(root, ".mcp.json", '{"mcpServers": {"x": {}}}\n')
    repo["record"].unlink()
    obj = _never_clean(root)
    assert obj.get("complete") is False, obj


def _delete_integration_branch(root: Path) -> str:
    sha = ref(root, "integrate/demo")
    wt = integration_worktree(root)
    out = raw(root, "worktree", "remove", "--force", str(wt))
    assert out.returncode == 0, out.stderr
    out = raw(root, "branch", "-D", "integrate/demo")
    assert out.returncode == 0, out.stderr
    return sha


def test_tc100_redteam_integration_branch_deleted(repo):
    _delete_integration_branch(repo["root"])
    code, obj, err = _audit(repo["root"])
    assert isinstance(obj, dict), (code, err)
    assert obj.get("clean") is False, obj
    assert code != 0, obj


def test_tc100_redteam_integration_branch_recreated_with_protected_file(repo):
    root = repo["root"]
    _delete_integration_branch(root)
    (root / ".claude" / "evil.md").write_text("evil\n")
    git(root, "add", "-A")
    git(root, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "main gets a protected file")
    out = raw(root, "-c", "core.hooksPath=/dev/null", "branch", "integrate/demo", "main")
    assert out.returncode == 0, out.stderr
    obj = _never_clean(root, expect_incomplete=False)
    assert obj.get("complete") is False or {"file": ".claude/evil.md", "change": "added"} in obj.get("drift", []), obj


def test_tc100_redteam_integration_branch_recreated_and_record_deleted(repo):
    root = repo["root"]
    _delete_integration_branch(root)
    (root / ".claude" / "settings.json").write_text('{"permissions": {"allow": ["*"]}}\n')
    git(root, "add", "-A")
    git(root, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "main weakens settings")
    raw(root, "-c", "core.hooksPath=/dev/null", "branch", "integrate/demo", "main")
    repo["record"].unlink()
    obj = _never_clean(root)
    assert obj.get("complete") is False, obj


def test_tc100_redteam_integration_branch_recreated_same_commit_record_deleted(repo):
    root = repo["root"]
    sha = _delete_integration_branch(root)
    raw(root, "-c", "core.hooksPath=/dev/null", "branch", "integrate/demo", sha)
    repo["record"].unlink()
    obj = _never_clean(root)
    assert obj.get("complete") is False, obj


def test_tc100_redteam_refs_index_deleted(repo):
    """Deleting ck/refs.json (the hook's ref -> spec index) must not hide a bypass-landed protected file."""
    root = repo["root"]
    idx = [Path(k) for k in snapshot_ck_state(root) if Path(k).name == "refs.json"]
    if not idx:
        pytest.fail("ck wrote no refs.json under its state directory")
    _bypass_add(root, ".claude/evil.md", "evil\n")
    for p in idx:
        p.unlink()
    obj = _never_clean(root, expect_incomplete=False)
    assert obj.get("complete") is False or obj.get("drift"), obj


def test_tc100_redteam_audit_of_unknown_spec_is_not_clean(repo):
    code, obj, err = ck_json(repo["root"], "worktree", "audit", "nosuchspec", "--json")
    assert code != 0, (obj, err)
    if isinstance(obj, dict):
        assert obj.get("clean") is not True, obj


def test_tc100_redteam_audit_output_fed_back_as_record(repo):
    """The system's own audit output, written over the record, is not a sealed approved state."""
    code, obj, err = _audit(repo["root"])
    assert isinstance(obj, dict), (code, err)
    repo["record"].write_text(json.dumps(obj))
    after = _never_clean(repo["root"])
    assert after.get("complete") is False, after


def test_tc100_redteam_into_other_branch_has_no_record(repo):
    """`--into` another branch has no approved state of its own: never clean."""
    root = repo["root"]
    code, obj, err = ck_json(root, "worktree", "audit", "demo", "--into", "main", "--json")
    assert isinstance(obj, dict), (code, err)
    assert obj.get("clean") is False, obj
    assert code != 0
