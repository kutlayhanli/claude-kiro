"""PreToolUse hook: guard tests, requirements and test plans against being bent to fit the code.

Who is calling decides the answer (`guard_policy.classify_caller`): a task agent is denied, the main
session gets the mode set in specs/ck.json "guard". The guard blocks, before it runs:
- an edit that removes test cases or assertions from an existing test file, or adds
  skip/xfail/only markers to one, unless an In Progress test-track task owns it
  or the file is new on this branch;
- a Bash command that deletes or moves an existing test file (also through a directory);
- for a task agent, a change or removal of existing text in a spec's requirements.md or
  test-plan.md while its implementation is under way (adding new text passes).

For a Bash command that may write, it also takes a snapshot of the dirty test files, so the
post-check (hooks/post_bash_check.py) can tell afterwards what the command changed.

Every target is judged in its own checkout: the guard mode comes from the session's own checkout,
while the patterns, base and owned files come from the target's checkout (so an absolute path into
a task worktree uses that worktree's). Every ask or deny is appended to the private guard log.
"""

import difflib
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from claude_kiro.config import guard_mode, load_config
from claude_kiro.hooks._shared import git_utils, guard_policy
from claude_kiro.hooks._shared.cache_manager import CacheManager
from claude_kiro.hooks._shared.session_tracker import SessionTracker
from claude_kiro.hooks._shared.spec_parser import parse_tasks
from claude_kiro.hooks._shared.test_heuristics import is_test_file, weakening
from claude_kiro.paths import spec_roots

FILE_TOOLS = ("Edit", "Write", "MultiEdit")

# Spec files whose existing text is frozen once implementation starts: file name -> guard key.
FROZEN_SPEC_FILES = {
    "requirements.md": "requirements",
    "test-plan.md": "test_plan",
}


def _decision(mode: str, reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": mode,
            "permissionDecisionReason": reason,
        }
    }


def in_progress_test_files(project_dir: Path) -> set:
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


def _only_adds(old: str, new: str) -> bool:
    """Whether new keeps every non-blank line of old, unchanged and in order.

    Inserting a requirement or test case cannot bend an existing one to fit the code;
    rewording or deleting one can.
    """
    old_lines = [line.rstrip() for line in old.splitlines() if line.strip()]
    new_lines = [line.rstrip() for line in new.splitlines() if line.strip()]
    ops = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()
    return all(tag in ("equal", "insert") for tag, *_ in ops)


def _old_and_new(tool_name: str, tool_input: Mapping[str, Any], path: Path, checkout: Optional[Path] = None) -> Optional[tuple]:
    if tool_name == "Edit":
        return str(tool_input.get("old_string", "")), str(tool_input.get("new_string", ""))
    if tool_name == "MultiEdit":
        edits = [e for e in (tool_input.get("edits") or []) if isinstance(e, Mapping)]
        return "".join(str(e.get("old_string", "")) for e in edits), "".join(str(e.get("new_string", "")) for e in edits)
    if tool_name == "Write":
        try:
            old = path.read_text(errors="replace")
        except OSError:
            # Unreadable now: the committed version is the best we have of what is being replaced.
            old = git_utils.content_at(checkout, "HEAD", _rel(path, checkout) or "") if checkout else None
            if old is None:
                return None
        return old, str(tool_input.get("content", tool_input.get("file_text", "")))
    return None


def session_mode(cwd: Any, kind: str) -> Optional[str]:
    """The configured guard mode for `kind` in the session's own checkout (the one `cwd` is in)."""
    base = Path(str(cwd))
    return guard_mode(load_config(git_utils.find_toplevel(base) or base), kind)


def _checkout_of(path: Path, cwd: Path) -> Optional[Path]:
    """The checkout holding `path` (its own, for a task worktree), else `cwd` when the path is inside it."""
    top = git_utils.find_toplevel(path)
    if top is not None:
        return top
    try:
        path.resolve().relative_to(cwd.resolve())
        return cwd
    except ValueError:
        return None


def _rel(path: Path, checkout: Path) -> Optional[str]:
    try:
        return path.resolve().relative_to(checkout.resolve()).as_posix()
    except ValueError:
        return None


class _Block:
    def __init__(self, decision: guard_policy.GuardDecision, reason: str):
        self.decision = decision
        self.reason = reason

    @property
    def path(self) -> str:
        return self.decision.paths[0] if self.decision.paths else ""


def _check_file_edit(tool_name: str, tool_input: Mapping[str, Any], cwd: Path, caller: str) -> Optional[_Block]:
    raw = tool_input.get("file_path")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(os.path.abspath(raw if os.path.isabs(raw) else cwd / raw))
    checkout = _checkout_of(path, cwd)
    if checkout is None:
        return None
    rel = _rel(path, checkout)
    if rel is None:
        return None
    config = load_config(checkout)

    # requirements.md and test-plan.md are frozen once implementation starts.
    # Judged on the resolved path (`..` and symlinks gone), as the test-file check below is.
    real = checkout / rel
    if real.name in FROZEN_SPEC_FILES:
        kind = FROZEN_SPEC_FILES[real.name]
        mode = session_mode(cwd, kind)
        spec_dir = real.parent
        if mode is None or spec_dir.parent not in spec_roots(checkout):
            return None
        started = _implementation_started(spec_dir)
        only_adds = True
        if started and caller == "task_agent":
            texts = _old_and_new(tool_name, tool_input, real)
            only_adds = texts is not None and _only_adds(*texts)
        facts = guard_policy.EditFacts(rel, kind, (), False, None, started, only_adds)  # type: ignore[arg-type]
        decision = guard_policy.decide_edit(facts, caller, mode)  # type: ignore[arg-type]
        if not decision.block:
            return None
        return _Block(decision, guard_policy.block_reason(caller, kind, rel, decision.kinds, tool_name))  # type: ignore[arg-type]

    mode = session_mode(cwd, "tests")
    if mode is None or not path.is_file() or not is_test_file(rel, config):
        return None
    texts = _old_and_new(tool_name, tool_input, path, checkout)
    if texts is None:
        return None
    reasons = tuple(weakening(*texts))
    if not reasons:
        return None
    owned = rel in in_progress_test_files(checkout)
    base = git_utils.base_ref(checkout, config.get("base"), fallback_head=False) if git_utils.is_repo(checkout) else None
    exists_on_base = git_utils.exists_at(checkout, base, rel) if base else None
    facts = guard_policy.EditFacts(rel, "test", reasons, owned, exists_on_base, False, False)
    decision = guard_policy.decide_edit(facts, caller, mode)  # type: ignore[arg-type]
    if not decision.block:
        return None
    return _Block(decision, guard_policy.block_reason(caller, "test", rel, decision.kinds, tool_name))  # type: ignore[arg-type]


def _target_facts(target: guard_policy.DestroyTarget, cwd: Path) -> Optional[tuple]:
    """(checkout, config, [TargetFacts]) for one destroy target, judged in its own checkout."""
    path = Path(target.path)
    checkout = _checkout_of(path, cwd)
    if checkout is None:
        return None
    rel = _rel(path, checkout)
    if not rel or rel == ".":
        return None
    config = load_config(checkout)
    if session_mode(cwd, "tests") is None:
        return None
    base = git_utils.base_ref(checkout, config.get("base"), fallback_head=False) if git_utils.is_repo(checkout) else None
    owned = in_progress_test_files(checkout)
    if path.is_dir():
        given = rel.rstrip("/") + "/"
        files = git_utils.tree_files(checkout, base, rel) if base else None
        if files is None:
            return checkout, config, [guard_policy.TargetFacts(given, target.op, False, None, False)]  # type: ignore[arg-type]
        return checkout, config, [
            guard_policy.TargetFacts(f, target.op, is_test_file(f, config), True, f in owned, given)  # type: ignore[arg-type]
            for f in files
        ]
    is_test = is_test_file(rel, config)
    exists = git_utils.exists_at(checkout, base, rel) if (is_test and base) else None
    return checkout, config, [guard_policy.TargetFacts(rel, target.op, is_test, exists, rel in owned)]  # type: ignore[arg-type]


def _check_bash(command: str, cwd: Path, caller: str) -> Optional[_Block]:
    targets = guard_policy.destroy_targets(command, str(cwd))
    mode = session_mode(cwd, "tests")
    groups: Dict[str, tuple] = {}
    for target in targets:
        got = _target_facts(target, cwd)
        if got is None:
            continue
        checkout, config, facts = got
        groups.setdefault(str(checkout), (config, []))[1].extend(facts)
    for _config, facts in groups.values():
        decision = guard_policy.decide_bash(facts, caller, mode)  # type: ignore[arg-type]
        if decision.block:
            rel = ", ".join(decision.paths)
            return _Block(decision, guard_policy.block_reason(caller, "test", rel, decision.kinds, "Bash"))  # type: ignore[arg-type]
    return None


def _log(input_data: Mapping[str, Any], tool: str, caller: str, block: _Block, command: Optional[str]) -> None:
    from claude_kiro.analytics import guardlog

    def text(key: str) -> Optional[str]:
        value = input_data.get(key)
        return value if isinstance(value, str) and value else None

    guardlog.append_event(guardlog.GuardEvent(
        ts=guardlog.now_iso(), session_id=text("session_id") or "unknown", agent_id=text("agent_id"),
        agent_type=text("agent_type"), caller=caller, tool=tool, decision=block.decision.mode or "deny",  # type: ignore[arg-type]
        category=block.decision.category or "", reason=block.reason, path=block.path, command=command,
        family=guard_policy.command_family(command), tool_use_id=text("tool_use_id")))


def _hook(input_data: dict) -> Optional[dict]:
    if not isinstance(input_data, dict):
        return None
    cwd_raw = input_data.get("cwd")
    cwd = Path(cwd_raw if isinstance(cwd_raw, str) and cwd_raw else os.getcwd())
    tool_name = input_data.get("tool_name")
    tool_input = input_data.get("tool_input")
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        return None
    session_id = input_data.get("session_id")

    # Start the session clock at the first tool call, so the Stop hook knows
    # which tasks.md edits happened in this session.
    try:
        SessionTracker(session_id if isinstance(session_id, str) and session_id else "unknown", CacheManager())
    except Exception:
        pass

    caller = guard_policy.classify_caller(input_data, os.environ, False)
    command: Optional[str] = None
    if tool_name in FILE_TOOLS:
        block = _check_file_edit(tool_name, tool_input, cwd, caller)
    elif tool_name == "Bash":
        raw = tool_input.get("command")
        command = raw if isinstance(raw, str) else None
        if command is None:
            return None
        block = _check_bash(command, cwd, caller)
    else:
        return None

    if block is not None:
        _log(input_data, tool_name, caller, block, command)
        return _decision(block.decision.mode or "deny", block.reason)
    if command is not None and guard_policy.may_write(command):
        from claude_kiro.hooks import post_bash_check

        post_bash_check.take_snapshot(input_data, command, caller)
    return None


def hook(input_data: dict) -> Optional[dict]:
    """The hook entry point. Odd input never fails it: an error here passes the call (the gate re-checks)."""
    try:
        return _hook(input_data)
    except Exception:
        return None
    finally:
        git_utils.set_deadline(None)
