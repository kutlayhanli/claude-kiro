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
"""

import datetime
import hashlib
import json
import os
import socket
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


def lookup_stamp(project_dir: Path, tree: str, command_sets: Iterable[List[str]]) -> Optional[Dict[str, Any]]:
    """The first stamp entry for `tree` matching any of `command_sets`, or None."""
    entries = _read(stamp_dir(project_dir) / f"{tree}.json")
    for commands in command_sets:
        sha = commands_sha(commands)
        for e in entries:
            if e.get("sha") == sha:
                return e
    return None


def write_stamp(project_dir: Path, tree: str, commands: List[str], by: str, duration_s: int) -> Optional[Path]:
    """Record that `tree` passed `commands`; replaces an earlier entry for the same commands."""
    directory = stamp_dir(project_dir)
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
            }
        )
        fd, tmp = tempfile.mkstemp(dir=directory)
        with os.fdopen(fd, "w") as f:
            json.dump(entries, f, indent=1)
        os.replace(tmp, path)
        return path
    except OSError:
        return None
