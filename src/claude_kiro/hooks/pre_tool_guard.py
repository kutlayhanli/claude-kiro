"""PreToolUse hook: guard tests and requirements against being bent to fit the code.

Asks me (or denies, per specs/ck.json "guard") before Claude:
- removes test cases or assertions from an existing test file, or adds
  skip/xfail/only markers to one, unless an In Progress test-track task owns it
  or the file is new on this branch;
- deletes, moves, or rewrites test files through Bash;
- edits a spec's requirements.md while its implementation is under way.

Everything else passes through untouched. The gate repeats the test check at
completion time, so anything this misses is still caught before Done.
"""

import os
import re
import shlex
from pathlib import Path
from typing import List, Optional

from claude_kiro.config import guard_mode, load_config
from claude_kiro.hooks._shared import git_utils
from claude_kiro.hooks._shared.cache_manager import CacheManager
from claude_kiro.hooks._shared.session_tracker import SessionTracker
from claude_kiro.hooks._shared.spec_parser import parse_tasks
from claude_kiro.hooks._shared.test_heuristics import is_test_path, weakening
from claude_kiro.paths import spec_roots

DESTRUCTIVE_BASH = re.compile(r"(?:^|[\s;&|(])(rm|mv|git\s+rm|git\s+mv|truncate|unlink)\s")
IN_PLACE_BASH = re.compile(r"(?:^|[\s;&|(])(sed|perl)\s+(?:-\w*\s+)*-i")
REDIRECT = re.compile(r">\s*([^\s;&|]+)")


def _relative(path: str, project_dir: Path) -> Optional[str]:
    p = Path(path)
    if not p.is_absolute():
        p = project_dir / p
    try:
        return p.resolve().relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        return None


def _decision(mode: str, reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": mode,
            "permissionDecisionReason": reason,
        }
    }


def _in_progress_test_files(project_dir: Path) -> set:
    owned = set()
    for root in spec_roots(project_dir):
        for tasks_md in root.glob("*/tasks.md"):
            for task in parse_tasks(tasks_md):
                if task.in_progress and task.track == "test":
                    owned.update(task.files)
    return owned


def _implementation_started(spec_dir: Path) -> bool:
    tasks = parse_tasks(spec_dir / "tasks.md")
    return any(t.in_progress or t.done for t in tasks)


def _old_and_new(tool_name: str, tool_input: dict, path: Path) -> Optional[tuple]:
    if tool_name == "Edit":
        return tool_input.get("old_string", ""), tool_input.get("new_string", "")
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits", [])
        return "".join(e.get("old_string", "") for e in edits), "".join(e.get("new_string", "") for e in edits)
    if tool_name == "Write":
        try:
            old = path.read_text(errors="replace")
        except OSError:
            return None
        return old, tool_input.get("content", tool_input.get("file_text", ""))
    return None


def _check_file_edit(tool_name: str, tool_input: dict, project_dir: Path, config: dict) -> Optional[dict]:
    rel = _relative(tool_input.get("file_path", ""), project_dir)
    if rel is None:
        return None
    path = project_dir / rel

    # requirements.md is frozen once implementation starts.
    if path.name == "requirements.md":
        mode = guard_mode(config, "requirements")
        spec_dir = path.parent
        if mode and spec_dir.parent in spec_roots(project_dir) and _implementation_started(spec_dir):
            return _decision(
                mode,
                f"{rel} is the contract for a spec whose implementation has started. Changing "
                "requirements to match the code hides failures. If a requirement is wrong, stop "
                "and raise it with me; approve this edit only if I asked for the change.",
            )
        return None

    mode = guard_mode(config, "tests")
    if not mode or not path.is_file() or not is_test_path(rel, config):
        return None
    texts = _old_and_new(tool_name, tool_input, path)
    if texts is None:
        return None
    reasons = weakening(*texts)
    if not reasons:
        return None
    if rel in _in_progress_test_files(project_dir):
        return None
    base = git_utils.base_ref(project_dir, config.get("base")) if git_utils.is_repo(project_dir) else None
    if base and not git_utils.exists_at(project_dir, base, rel):
        return None  # new on this branch; still being written

    return _decision(
        mode,
        f"This edit {', '.join(reasons)} in {rel}, an existing test that no In Progress "
        "test-track task owns. Tests are the oracle: weakening them to get a pass is not "
        "allowed. If the test is wrong or contradicts the spec, stop and tell me instead.",
    )


def _bash_targets(command: str) -> List[str]:
    """Paths a command could destroy: args of rm/mv/sed -i/..., and redirect targets."""
    targets = []
    for segment in re.split(r"&&|\|\||[;|\n]", command):
        targets += REDIRECT.findall(segment)
        if DESTRUCTIVE_BASH.search(segment) or IN_PLACE_BASH.search(segment):
            try:
                tokens = shlex.split(segment)
            except ValueError:
                tokens = segment.split()
            targets += [t for t in tokens[1:] if not t.startswith("-")]
    return targets


def _check_bash(command: str, project_dir: Path, config: dict) -> Optional[dict]:
    mode = guard_mode(config, "tests")
    if not mode:
        return None
    hits = []
    for token in _bash_targets(command):
        rel = _relative(token, project_dir)
        if not rel or rel == ".":
            continue
        path = project_dir / rel
        if path.is_file() and is_test_path(rel, config):
            hits.append(rel)
        elif path.is_dir() and is_test_path(f"{rel}/x", config):
            hits.append(rel + "/")
    hits = sorted(set(hits) - _in_progress_test_files(project_dir))
    if not hits:
        return None
    return _decision(
        mode,
        f"This command deletes, moves, or rewrites test files ({', '.join(hits)}). Tests are the "
        "oracle for spec tasks; if one is wrong, stop and tell me instead of removing it.",
    )


def hook(input_data: dict) -> Optional[dict]:
    project_dir = Path(input_data.get("cwd") or os.getcwd())
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {}) or {}

    # Start the session clock at the first tool call, so the Stop hook knows
    # which tasks.md edits happened in this session.
    SessionTracker(input_data.get("session_id", "unknown"), CacheManager())

    config = load_config(project_dir)
    if tool_name in ("Edit", "Write", "MultiEdit"):
        return _check_file_edit(tool_name, tool_input, project_dir, config)
    if tool_name == "Bash":
        return _check_bash(tool_input.get("command", ""), project_dir, config)
    return None
