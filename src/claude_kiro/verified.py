"""Pass stamps: a record that a git tree passed a set of check commands.

Shared with the safe-merge skill (~/.claude/skills/safe-merge), which reads them to skip re-running
checks on a tree that already passed, and writes its own. One JSON file per tree in the repository's
common git dir, so every worktree sees it:

    <git-common-dir>/ck-verified/<tree-hash>.json
    [{"commands": [...], "sha": "<sha256>", "by": "ck gate", "commit": "...", "at": "...",
      "duration_s": 12, "host": "..."}]

``sha`` is sha256 of the commands joined with a trailing newline each (``printf '%s\\n' cmd...``). A
tree hash covers every tracked byte, so a stamp is only written for a worktree with no uncommitted
or untracked changes when the checks started, and no tracked changes after.

A task gate (`ck gate --task`, the done-verify hook) stamps the tree of the working tree as it ran,
tracked and untracked files alike (`worktree_tree`), with `"scope": "task"` and the commands it ran,
in `<git-common-dir>/ck-verified/task/<tree-hash>.json` (same entry format; safe-merge never reads it).
That is the tree `git add -A && git commit` then records, so a task gate after the commit can reuse it.
"""

import datetime
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from claude_kiro.hooks._shared.git_utils import _git


def commands_sha(commands: Iterable[str]) -> str:
    return hashlib.sha256("".join(c + "\n" for c in commands).encode()).hexdigest()


def stamp_dir(project_dir: Path) -> Path:
    common = (_git(project_dir, "rev-parse", "--git-common-dir") or "").strip()
    path = Path(common)
    if not path.is_absolute():
        path = Path(project_dir) / path
    return path.resolve() / "ck-verified"


def head_tree(project_dir: Path) -> Optional[str]:
    out = _git(project_dir, "rev-parse", "HEAD^{tree}")
    return out.strip() if out else None


def clean_tree(project_dir: Path) -> Optional[str]:
    """HEAD's tree when the worktree has no tracked or untracked changes, else None."""
    status = _git(project_dir, "status", "--porcelain", "--untracked-files=normal")
    if status is None or status.strip():
        return None
    return head_tree(project_dir)


# Untracked artifacts a test run leaves behind; they must not change a task gate's tree.
_CACHE_EXCLUDES = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis", ".nyc_output", ".tox")


def worktree_tree(project_dir: Path) -> Optional[str]:
    """Tree hash of the working tree: HEAD plus every tracked change and untracked, not ignored file.

    Built in a throwaway copy of the index (the copy keeps git's stat cache, so only changed files are
    hashed); the real index is never touched. Test caches are left out. None outside git.
    """
    index = (_git(project_dir, "rev-parse", "--git-path", "index") or "").strip()
    if not index:
        return None
    index_path = Path(index) if Path(index).is_absolute() else Path(project_dir) / index
    with tempfile.TemporaryDirectory(prefix="ck-tree-") as tmp:
        temp_index = Path(tmp) / "index"
        env = {**os.environ, "GIT_INDEX_FILE": str(temp_index)}

        def git(*args: str) -> str:
            return subprocess.run(["git", *args], cwd=project_dir, env=env, capture_output=True, text=True,
                                  timeout=300, check=True).stdout

        try:
            if index_path.exists():
                shutil.copyfile(index_path, temp_index)
            else:
                git("read-tree", "HEAD")
            excludes = [f":(exclude,glob)**/{d}/**" for d in _CACHE_EXCLUDES] + [":(exclude,glob)**/*.py[co]"]
            git("add", "-A", "--", ".", *excludes)
            return git("write-tree").strip() or None
        except (OSError, subprocess.SubprocessError):
            return None


def tracked_unchanged(project_dir: Path, tree: str) -> bool:
    """HEAD still has `tree` and no tracked file was modified (checks may leave untracked caches)."""
    if head_tree(project_dir) != tree:
        return False
    status = _git(project_dir, "status", "--porcelain", "--untracked-files=no")
    return status is not None and not status.strip()


def _read(path: Path) -> List[Dict[str, Any]]:
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def _scope_dir(project_dir: Path, scope: Optional[str]) -> Path:
    # Task stamps live in a subdirectory: the top level is the full-suite contract safe-merge reads.
    return stamp_dir(project_dir) / scope if scope else stamp_dir(project_dir)


def lookup_stamp(project_dir: Path, tree: str, command_sets: Iterable[List[str]],
                 scope: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The first stamp entry for `tree` matching any of `command_sets`, or None."""
    entries = _read(_scope_dir(project_dir, scope) / f"{tree}.json")
    for commands in command_sets:
        sha = commands_sha(commands)
        for e in entries:
            if e.get("sha") == sha:
                return e
    return None


def write_stamp(project_dir: Path, tree: str, commands: List[str], by: str, duration_s: int,
                scope: Optional[str] = None) -> Optional[Path]:
    """Record that `tree` passed `commands`; replaces an earlier entry for the same commands."""
    directory = _scope_dir(project_dir, scope)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{tree}.json"
        sha = commands_sha(commands)
        entries = [e for e in _read(path) if e.get("sha") != sha]
        commit = (_git(project_dir, "rev-parse", "HEAD") or "").strip()
        entries.append(
            {
                "commands": list(commands),
                "sha": sha,
                "by": by,
                "commit": commit,
                "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "duration_s": int(duration_s),
                "host": socket.gethostname(),
                **({"scope": scope} if scope else {}),
            }
        )
        fd, tmp = tempfile.mkstemp(dir=directory)
        with os.fdopen(fd, "w") as f:
            json.dump(entries, f, indent=1)
        os.replace(tmp, path)
        return path
    except OSError:
        return None
