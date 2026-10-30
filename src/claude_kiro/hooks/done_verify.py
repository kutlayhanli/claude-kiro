"""PostToolUse hook: gate a task the moment it is marked Done.

A claim is made once, when a task's **Status:** becomes Done, so it is checked
once: right after the Edit/Write of tasks.md (or the Bash command that rewrote
it), with the task-scoped gate (that task's Verify and its verifying tests),
not the whole spec. A failing gate is fed back to Claude. Nothing re-runs on
later turns; the Stop hook only warns, and the full gate runs at merge.
"""

import os
from pathlib import Path
from typing import List, Optional, Tuple

from claude_kiro.hooks._shared import claims as cl
from claude_kiro.hooks._shared import git_utils
from claude_kiro.hooks._shared.cache_manager import CacheManager
from claude_kiro.hooks._shared.session_tracker import SessionTracker
from claude_kiro.paths import spec_roots

FILE_TOOLS = {"Edit", "Write", "MultiEdit"}


def _edited_tasks_md(cwd: Path, input_data: dict) -> Optional[Tuple[Path, Path]]:
    """(tasks.md, the checkout it belongs to) for an edit of a spec's tasks.md."""
    file_path = (input_data.get("tool_input") or {}).get("file_path", "")
    if not file_path:
        return None
    path = Path(file_path) if os.path.isabs(file_path) else cwd / file_path
    if path.name != "tasks.md":
        return None
    root = cl.checkout_of(path, cwd)
    return (path, root) if root else None


def _rewritten_by_bash(cwd: Path, seen: dict, session_start: float) -> List[Path]:
    """tasks.md files a Bash command changed and left uncommitted.

    A file is looked at only if its mtime moved since we last saw it (or, the
    first time, is newer than the session), and only if git shows a real
    change: a checkout, rebase or merge rewrites tasks.md with committed
    content, and those Done tasks are not this session's claims.
    """
    found = []
    for root in spec_roots(cwd):
        for tasks_md in root.glob("*/tasks.md"):
            mtime = cl.mtime(tasks_md)
            record = seen.get(str(tasks_md))
            if mtime is None or (record and record.get("mtime") == mtime):
                continue
            if record is None and mtime < session_start:
                continue
            if git_utils.is_modified(cwd, tasks_md) is True:
                found.append(tasks_md)
            else:
                seen[str(tasks_md)] = {"mtime": mtime, "done": sorted(cl.done_now(tasks_md))}
    return found


def hook(input_data: dict) -> Optional[dict]:
    """The gate result for a task just marked Done, and, for Bash, the post-check finding (gate report first)."""
    result = _gate_hook(input_data)
    if input_data.get("tool_name") != "Bash":
        return result
    from claude_kiro.hooks import post_bash_check

    message = post_bash_check.check(input_data)
    if not message:
        return result
    if result and result.get("decision") == "block":
        return {**result, "reason": f"{result['reason']}\n\n{message}"}
    if result and result.get("systemMessage"):
        return {"decision": "block", "reason": message, "systemMessage": result["systemMessage"]}
    return {"decision": "block", "reason": message}


def _gate_hook(input_data: dict) -> Optional[dict]:
    tool = input_data.get("tool_name", "")
    if tool not in FILE_TOOLS and tool != "Bash":
        return None
    cwd = Path(input_data.get("cwd") or os.getcwd())
    tracker = SessionTracker(input_data.get("session_id", "unknown"), CacheManager())
    state = tracker.gate_state()
    seen = cl.seen(state)

    if tool in FILE_TOOLS:
        edited = _edited_tasks_md(cwd, input_data)
        targets = [edited] if edited else []
    else:
        before_scan = dict(seen)
        targets = [(t, cwd) for t in _rewritten_by_bash(cwd, seen, tracker.session_start)]
        if not targets and seen != before_scan:
            tracker.save_gate()
    if not targets:
        return None

    # Imported here: most tool calls never reach a gate, and this hook runs on every Bash call.
    from claude_kiro.config import load_config
    from claude_kiro.gate import run_gate

    passes, failures, warnings = [], [], []
    for tasks_md, root in targets:
        now = cl.done_now(tasks_md)
        record = seen.get(str(tasks_md))
        before = set(record["done"]) if record else cl.done_at_head(root, tasks_md)
        if before is None:  # outside git with no earlier look: every Done task is a claim
            before = set()
        if tool == "Bash":
            before |= cl.done_at_head(root, tasks_md) or set()
        new = cl.task_order(now - before)

        recorded = set(now)
        if new:
            spec_dir = tasks_md.parent
            # Stamped by tree: the agent's own `ck gate --task` on the same tree reuses this pass.
            result = run_gate(root, spec_dir, new, load_config(root), reuse=True, stamp_by="done-verify")
            for num in new:
                cl.claims(state)[cl.claim_key(spec_dir, num)] = "pass" if result.ok else "fail"
            if result.ok:
                passes.append(f"{spec_dir.name}: Task {', '.join(new)}")
                if any(c.warning for c in result.checks):
                    warnings.append(result.report())
            else:
                failures.append(result.report())
                recorded -= set(new)  # re-gated the next time tasks.md changes
        seen[str(tasks_md)] = {"mtime": cl.mtime(tasks_md), "done": cl.task_order(recorded)}
    tracker.save_gate()

    if failures:
        return {
            "decision": "block",
            "reason": (
                "\n\n".join(failures) + "\n\n"
                "You marked a task Done but it does not pass the verification gate. Either fix the "
                "failures and mark it Done again, or set its **Status:** back to In Progress and tell me "
                "what is blocking it. Do not delete, skip, or weaken tests, and do not edit requirements, "
                "to get a pass. If a test contradicts the spec, report the conflict to me."
            ),
        }
    if passes:
        message = "✓ Verification gate passed for " + "; ".join(passes)
        if warnings:
            message += "\n" + "\n".join(warnings)
        return {"systemMessage": message}
    return None
