"""Stop hook: warn about Done claims that never passed the gate. Runs no tests.

The done-verify PostToolUse hook gates each task when it is marked Done, and
the full gate runs at merge. This hook only reads what is already known, so
ending a turn stays instant: it lists tasks still marked Done whose gate
failed, and uncommitted Done tasks no gate ran for (e.g. tasks.md written by a
script the PostToolUse hook does not see). It never blocks. In a subagent's
task worktree, which shares my session id, only failed gates are reported.
"""

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from claude_kiro.hooks._shared import claims as cl
from claude_kiro.hooks._shared.cache_manager import CacheManager
from claude_kiro.hooks._shared.session_tracker import SessionTracker
from claude_kiro.paths import spec_roots


def _unverified(project_dir: Path, tracker: SessionTracker) -> Dict[Path, Tuple[Path, List[str]]]:
    """Spec dir -> (its checkout, Done tasks without a passing gate this session)."""
    state = tracker.gate_state()
    results = cl.claims(state)
    seen = cl.seen(state)

    spec_dirs = {Path(p) for p in tracker.spec_edits()}
    spec_dirs |= {Path(k).parent for k, record in seen.items()}
    spec_dirs |= {Path(k.rsplit(":", 1)[0]) for k in results}
    for root in spec_roots(project_dir):
        for tasks_md in root.glob("*/tasks.md"):
            mtime = cl.mtime(tasks_md)
            if mtime is not None and mtime >= tracker.session_start:
                spec_dirs.add(tasks_md.parent)

    found: Dict[Path, Tuple[Path, List[str]]] = {}
    for spec_dir in spec_dirs:
        tasks_md = spec_dir / "tasks.md"
        root = cl.checkout_of(tasks_md, project_dir)
        now = cl.done_now(tasks_md) if root else set()
        if not now:
            continue
        claimed = {n for n in now if cl.claim_key(spec_dir, n) in results}
        if root == project_dir:
            head = cl.done_at_head(root, tasks_md)
            uncommitted = now - head if head is not None else set()
            missing = [n for n in uncommitted | claimed if results.get(cl.claim_key(spec_dir, n)) != "pass"]
        else:
            # A subagent's task worktree (it shares my session id): its ungated Done tasks
            # are work in flight, so only a gate that ran and failed is worth a warning.
            missing = [n for n in claimed if results.get(cl.claim_key(spec_dir, n)) == "fail"]
        if missing:
            found[spec_dir] = (root, cl.task_order(missing))
    return found


def hook(input_data: dict) -> Optional[dict]:
    cwd = Path(input_data.get("cwd") or os.getcwd())
    tracker = SessionTracker(input_data.get("session_id", "unknown"), CacheManager())
    unverified = _unverified(cwd, tracker)
    if not unverified:
        return None

    results = cl.claims(tracker.gate_state())
    lines = []
    for spec_dir, (root, nums) in sorted(unverified.items()):
        name = spec_dir.name if root == cwd else f"{spec_dir.name} (worktree {root})"
        ck = "ck" if root == cwd else f"ck -C {root}"
        failed = [n for n in nums if results.get(cl.claim_key(spec_dir, n)) == "fail"]
        unchecked = [n for n in nums if n not in failed]
        if failed:
            lines.append(f"- {name}: Task {', '.join(failed)} failed the gate")
        if unchecked:
            lines.append(f"- {name}: Task {', '.join(unchecked)} not gated yet")
        lines.append(f"  check with `{ck} gate {spec_dir.name} " + " ".join(f"--task {n}" for n in nums) + "`")
    return {
        "systemMessage": (
            "⚠️ Marked Done without a passing verification gate:\n"
            + "\n".join(lines)
            + "\nThe full gate runs at merge; fix these or set them back to In Progress before landing."
        )
    }
