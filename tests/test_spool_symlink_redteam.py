"""prepare_spool never writes through a symlink a sandboxed child planted in the spool (Story 16, Security NFR).

The spool (`<worktrees>/.ck-progress`) is writable by sandboxed children, and prepare_spool runs unsandboxed. A
symlink named like a run of the spec, pointing at a file with no `end` line (a hook script, the key), must be left
alone: its target's bytes and mode stay as they were. Real ck code, real files; nothing is mocked.
"""

import os
import stat
from pathlib import Path

from unattended_helpers import commit_all, init_repo

SPEC = "demo"


def _repo(tmp_path: Path) -> Path:
    root = init_repo(tmp_path / "repo")
    (root / ".gitignore").write_text(".venv/\n.ck/worktrees/\n")
    (root / "specs" / SPEC).mkdir(parents=True)
    (root / "specs" / SPEC / "tasks.md").write_text("### Task 1: t1\n**Status:** Not Started\n\n")
    commit_all(root, "base")
    return root


def _spool(root: Path) -> Path:
    from claude_kiro import worktrees

    spool = Path(worktrees.worktrees_dir(worktrees.main_root(root), SPEC)) / ".ck-progress"
    spool.mkdir(parents=True, exist_ok=True)
    return spool


def test_a_symlinked_spool_run_file_target_is_unchanged_after_prepare_spool(tmp_path):
    from claude_kiro.eta.service import prepare_spool

    root = _repo(tmp_path)
    spool = _spool(root)
    target = tmp_path / "hook-script"
    target.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(target, 0o755)
    (spool / f"{SPEC}--20260101T000000Z-abcdef.jsonl").symlink_to(target)
    (spool / f"{SPEC}--x.jsonl").symlink_to(target)

    prepare_spool(root, SPEC, dict(os.environ))

    assert target.read_text() == "#!/bin/sh\nexit 0\n", "prepare_spool wrote through a planted symlink"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755, "prepare_spool changed a symlink target's mode"


def test_a_spool_file_not_named_like_a_run_key_is_left_alone(tmp_path):
    from claude_kiro.eta.service import prepare_spool

    root = _repo(tmp_path)
    spool = _spool(root)
    odd = spool / f"{SPEC}--not-a-run.jsonl"
    odd.write_text("")
    os.chmod(odd, 0o644)

    prepare_spool(root, SPEC, dict(os.environ))

    assert odd.read_text() == "", "prepare_spool appended to a file that is not one of ck's run keys"
    assert stat.S_IMODE(odd.stat().st_mode) == 0o644


def test_a_forced_append_error_never_writes_through_a_planted_errors_symlink(tmp_path):
    # Review finding (Task 36): a child makes the supersede append fail every time (a run file it made read-only, no
    # `end` line), and plants `<key>.errors` as a symlink to a file outside the sandbox. The unsandboxed parent's
    # error record must not follow it: the target's bytes and mode stay as they were.
    from claude_kiro.eta.service import prepare_spool

    root = _repo(tmp_path)
    spool = _spool(root)
    key = f"{SPEC}--20260101T000000Z-x"
    run = spool / f"{key}.jsonl"
    run.write_text('{"schema":"ck-progress/1","at":"2026-01-01T00:00:00Z","kind":"start"}\n')
    os.chmod(run, 0o400)
    target = tmp_path / "bashrc"
    target.write_text("export PATH=$PATH\n")
    os.chmod(target, 0o644)
    (spool / f"{key}.errors").symlink_to(target)

    try:
        prepare_spool(root, SPEC, dict(os.environ))
    finally:
        os.chmod(run, 0o600)

    assert target.read_text() == "export PATH=$PATH\n", "the error record wrote through a planted .errors symlink"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644, "the error record changed a symlink target's mode"
