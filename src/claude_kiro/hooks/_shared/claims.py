"""Done claims: which tasks were just marked Done, and what their gate said.

Shared by the done-verify hook (gates a claim once, when it is made) and the
stop-verify hook (warns about claims that never passed). State lives in the
session cache under `gate`:

- `seen`: tasks.md path -> {"mtime", "done"}: the Done set last observed, so
  a later change can be told apart from tasks that were already Done.
- `claims`: "<spec_dir>:<task>" -> "pass" | "fail": the gate result per claim.
"""

import os
from pathlib import Path
from typing import Dict, Optional, Set

from claude_kiro.hooks._shared import git_utils
from claude_kiro.hooks._shared.spec_parser import parse_tasks, parse_tasks_text


def done_now(tasks_md: Path) -> Set[str]:
    return {t.num for t in parse_tasks(tasks_md) if t.done}


def done_at_head(project_dir: Path, tasks_md: Path) -> Optional[Set[str]]:
    """Done tasks in HEAD's version of tasks.md; empty if HEAD lacks the file, None outside git."""
    if not git_utils.is_repo(project_dir):
        return None
    rel = os.path.relpath(tasks_md, project_dir)
    content = git_utils.show(project_dir, "HEAD", f"./{rel}")
    return {t.num for t in parse_tasks_text(content) if t.done} if content else set()


def mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def claim_key(spec_dir: Path, task_num: str) -> str:
    return f"{spec_dir}:{task_num}"


def seen(state: Dict) -> Dict[str, Dict]:
    return state.setdefault("seen", {})


def claims(state: Dict) -> Dict[str, str]:
    return state.setdefault("claims", {})


def task_order(nums) -> list:
    return sorted(nums, key=lambda n: (len(n), n))
