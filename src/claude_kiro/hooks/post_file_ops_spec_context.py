"""PostToolUse Hook: Spec Context Provider with Session Tracking.

Provides spec context when editing files, showing messages only once per file per session.
"""

import os
from pathlib import Path

from claude_kiro.hooks._shared.cache_manager import CacheManager
from claude_kiro.hooks._shared.session_tracker import SessionTracker
from claude_kiro.hooks._shared.spec_parser import SpecParser
from claude_kiro.config import load_config
from claude_kiro.paths import is_scaffold_path, is_spec_path


def generate_spec_context_message(
    spec_name: str, task_num: str, task_title: str, spec_dir: str
) -> str:
    """Generate context message for a file in a spec.

    Args:
        spec_name: Name of the spec.
        task_num: Task number.
        task_title: Task title.
        spec_dir: Spec directory, relative to the project root.

    Returns:
        Formatted context message.
    """
    return f"""
📋 **Spec Task Context: {spec_name}**

You are working on Task {task_num}: {task_title}

Before proceeding:
1. Read `{spec_dir}/requirements.md` - verify acceptance criteria
2. Read `{spec_dir}/design.md` - follow architectural decisions
3. Read `{spec_dir}/test-plan.md` (if present) - the tests that define "working"
4. Check `{spec_dir}/tasks.md` - the task's Track, Verify commands, and acceptance checklist

Your responsibilities:
- As you complete acceptance criteria, update the checkboxes in tasks.md to [x]
- Mark the task `**Status:** Done` only after `ck gate {spec_name} --task {task_num}` passes; a Stop hook enforces this
- Never delete, skip, or weaken existing tests or edit requirements.md to get a pass; if a test looks wrong, tell me

Ensure your changes align with the spec requirements and design.
"""


def _relative(path: Path, cwd: str) -> str:
    """Display a path relative to the project root when possible."""
    try:
        return str(path.resolve().relative_to(Path(cwd).resolve()))
    except ValueError:
        return str(path)


def generate_no_spec_message(file_path: str) -> str:
    """Generate message for a file not in any spec.

    Args:
        file_path: Path to the file.

    Returns:
        Formatted suggestion message.
    """
    # Extract just the filename for cleaner display
    file_name = os.path.basename(file_path)

    return f"""
⚠️ **File Not in Specification**

The file `{file_name}` is not part of any existing specification.

WHEN we work on new feature code, you should suggest to me creating a specification first.

Your task:
1. Infer what feature this file relates to based on file path and context
2. Suggest to me: "You can run `/spec:plan [inferred-feature-description]` to decide the approach, then `/spec:create`"

Example: "You can run `/spec:plan Add user authentication system`"

If this is a quick fix or non-feature work, acknowledge and proceed without requiring a spec.
"""


def generate_outside_task_message(rel_path: str, spec_name: str, task_num: str) -> str:
    """Note for a file the In Progress task does not list."""
    return f"""
📎 `{rel_path}` is not listed in Task {task_num} of {spec_name} (In Progress).

Other tasks may run in parallel and touch nearby files, so keep changes outside your task's
**Files:** minimal, and mention `{rel_path}` in your report as a file touched outside the task.
"""


def hook(input_data: dict) -> dict | None:
    """Process post-file-ops hook.

    Args:
        input_data: Hook input from Claude Code

    Returns:
        Hook output dict or None if no output needed
    """

    # Extract relevant fields
    tool_name = input_data.get("tool_name", "")
    session_id = input_data.get("session_id", "unknown")

    # Only process file modification tools
    if tool_name not in ["Edit", "Write", "MultiEdit"]:
        return None

    # Get file path from tool input
    tool_input = input_data.get("tool_input", {})
    file_path = tool_input.get("file_path", "")

    if not file_path:
        return None

    # Get project directory
    cwd = input_data.get("cwd", os.getcwd())

    # Initialize components
    cache_manager = CacheManager()
    session_tracker = SessionTracker(session_id, cache_manager)

    # Spec files get no context message, but tasks.md edits are recorded so the
    # Stop hook can verify tasks marked Done in this session.
    if is_spec_path(file_path, Path(cwd)):
        path = Path(file_path) if os.path.isabs(file_path) else Path(cwd) / file_path
        if path.name == "tasks.md":
            session_tracker.mark_spec_edit(str(path.parent))
        return None

    spec_parser = SpecParser(cwd)

    # Clean up old sessions on first file of a new session
    if session_tracker.get_notification_count() == 0:
        session_tracker.cleanup_old_sessions(session_id)

    # Check if we've already notified about this file in this session
    if session_tracker.has_notified(file_path):
        return None

    # Repo hygiene, scaffolding, and files outside the project never get spec talk.
    rel = spec_parser._relative(file_path)
    if rel is None or is_scaffold_path(rel, load_config(Path(cwd)).get("context_ignore", [])):
        return None

    task_match = spec_parser.find_matching_task(file_path)
    if task_match:
        # File belongs to the task In Progress -> remind of its context.
        context = generate_spec_context_message(
            task_match.spec_name,
            task_match.task_num,
            task_match.task_title,
            _relative(task_match.spec_dir, cwd),
        )
        session_tracker.mark_notified(
            file_path,
            in_spec=True,
            spec_name=task_match.spec_name,
            task_num=task_match.task_num,
        )
    elif spec_parser.listed_by_any_task(file_path):
        # Planned work of a task that is not In Progress: nothing useful to say.
        session_tracker.mark_notified(file_path, in_spec=False, context="other-task")
        return None
    elif spec_parser.in_progress_tasks():
        # Implementing a task, editing a file it doesn't list.
        current = spec_parser.in_progress_tasks()[0]
        context = generate_outside_task_message(rel, current.spec_name, current.task_num)
        session_tracker.mark_notified(file_path, in_spec=False, context="outside-task")
    else:
        # No spec work under way and the file is in no spec -> suggest one.
        context = generate_no_spec_message(file_path)
        session_tracker.mark_notified(file_path, in_spec=False)

    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    }
