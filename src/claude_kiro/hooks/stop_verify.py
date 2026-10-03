"""Stop / SubagentStop hook: block "done" claims that fail the verification gate.

Fires at the end of every turn but only does work when a task this session
touched (edited one of its files, or edited its spec's tasks.md) is marked
Done. Stopping is always allowed; claiming Done is not, unless the gate passes.
A task whose state was already verified is not re-run.
"""

import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from claude_kiro.config import load_config
from claude_kiro.gate import run_gate
from claude_kiro.hooks._shared import git_utils
from claude_kiro.hooks._shared.cache_manager import CacheManager
from claude_kiro.hooks._shared.session_tracker import SessionTracker
from claude_kiro.hooks._shared.spec_parser import parse_tasks
from claude_kiro.paths import spec_roots

# After this many blocks for the same unchanged failure, let the stop through
# and warn me instead, so a stuck agent can't loop forever.
MAX_BLOCKS = 3


def _candidates(project_dir: Path, tracker: SessionTracker) -> Dict[Path, List[str]]:
    """Done tasks this session touched, grouped by spec directory."""
    specs: Dict[Path, set] = {}
    by_name = {d.name: d for root in spec_roots(project_dir) for d in root.iterdir() if d.is_dir()}

    for spec_name, task_num in tracker.touched_tasks():
        if spec_name in by_name:
            specs.setdefault(by_name[spec_name], set()).add(task_num)

    edited = {Path(p).resolve() for p in tracker.spec_edits()}
    for spec_dir in by_name.values():
        tasks_md = spec_dir / "tasks.md"
        try:
            touched_by_bash = tasks_md.stat().st_mtime >= tracker.session_start
        except OSError:
            continue
        if spec_dir.resolve() in edited or touched_by_bash:
            specs.setdefault(spec_dir, set()).add("*")

    result = {}
    for spec_dir, nums in specs.items():
        tasks = parse_tasks(spec_dir / "tasks.md")
        done = [t.num for t in tasks if t.done and ("*" in nums or t.num in nums)]
        if done:
            result[spec_dir] = done
    return result


def hook(input_data: dict) -> Optional[dict]:
    cwd = Path(input_data.get("cwd") or os.getcwd())
    session_id = input_data.get("session_id", "unknown")
    agent_id = input_data.get("agent_id")
    tracker = SessionTracker(session_id, CacheManager())

    candidates = _candidates(cwd, tracker)
    if not candidates:
        return None

    state = tracker.gate_state()
    verified = state.setdefault("verified", {})
    blocks = state.setdefault("blocks", {})
    fingerprint = git_utils.worktree_fingerprint(cwd) if git_utils.is_repo(cwd) else str(time.time())
    config = load_config(cwd)

    failures, passes, warnings = [], [], []
    for spec_dir, task_nums in sorted(candidates.items()):
        pending = [n for n in task_nums if verified.get(f"{spec_dir}:{n}") != fingerprint]
        if not pending:
            continue
        result = run_gate(cwd, spec_dir, pending, config)
        if result.ok:
            for n in pending:
                verified[f"{spec_dir}:{n}"] = fingerprint
            passes.append(f"{spec_dir.name}: Task {', '.join(pending)}")
            if any(c.warning for c in result.checks):
                warnings.append(result.report())
        else:
            failures.append(result.report())

    if not failures:
        tracker.save()
        if not passes:
            return None
        message = "✓ Verification gate passed for " + "; ".join(passes)
        if warnings:
            message += "\n" + "\n".join(warnings)
        return {"systemMessage": message}

    report = "\n\n".join(failures)
    key = f"{agent_id or 'main'}:{fingerprint}"
    blocks[key] = blocks.get(key, 0) + 1
    tracker.save()

    if blocks[key] > MAX_BLOCKS:
        return {
            "systemMessage": (
                f"⚠️ Verification gate still failing after {MAX_BLOCKS} attempts; letting the stop "
                f"through. A task is marked Done that does not pass:\n\n{report}"
            )
        }

    instructions = (
        f"{report}\n\n"
        "A task is marked Done but does not pass the verification gate. Either fix the "
        "failures, or set the task's **Status:** back to In Progress and tell me what is "
        "blocking it. Do not delete, skip, or weaken tests, and do not edit requirements, "
        "to get a pass. If a test contradicts the spec, report the conflict to me."
    )
    return {"decision": "block", "reason": instructions}
