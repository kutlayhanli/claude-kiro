"""Task worktrees for parallel spec implementation (`ck worktree ...`).

Every task gets `.claude/worktrees/<spec>-task-<N>` on branch
`feat/<spec>-task-<N>`, created from a base branch. A task agent claims its
worktree (git's own `git worktree lock`) while it runs, so a second agent or a
setup step can see it is busy. Merges go into whichever checkout has the
target branch, one task at a time, and stop at the first conflict.

Git runs as a subprocess with an argument list, never through a shell, so
shell aliases and command-rewriting hooks don't interfere.
"""

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

from claude_kiro.paths import WORKTREES_DIR


class WorktreeError(Exception):
    pass


@dataclass
class Outcome:
    task: str
    state: str  # CREATED EXISTS BUSY CLAIMED RELEASED MERGED SKIP CONFLICT MISSING ERROR
    detail: str = ""
    path: str = ""
    branch: str = ""

    def line(self) -> str:
        parts = [self.state, self.task]
        if self.path:
            parts.append(self.path)
        if self.branch:
            parts.append(f"({self.branch})")
        if self.detail:
            parts.append(f"- {self.detail}")
        return " ".join(parts)

    def as_dict(self) -> Dict[str, str]:
        return asdict(self)


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()}")
    return proc


def main_root(start: Path) -> Path:
    """The main checkout of the repository containing `start` (not a linked worktree)."""
    common = git(start, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    common_path = Path(common)
    return common_path.parent if common_path.name == ".git" else common_path


def task_path(root: Path, spec: str, num: str) -> Path:
    return root / WORKTREES_DIR / f"{spec}-task-{num}"


def task_branch(spec: str, num: str) -> str:
    return f"feat/{spec}-task-{num}"


def integration_path(root: Path, spec: str) -> Path:
    return root / WORKTREES_DIR / f"{spec}-integration"


def integration_branch(spec: str) -> str:
    return f"integrate/{spec}"


def worktree_table(root: Path) -> Dict[str, Dict[str, str]]:
    """path -> {branch, locked} from `git worktree list --porcelain`."""
    table: Dict[str, Dict[str, str]] = {}
    current: Dict[str, str] = {}
    for line in git(root, "worktree", "list", "--porcelain").stdout.splitlines() + [""]:
        if not line:
            if "worktree" in current:
                table[str(Path(current["worktree"]).resolve())] = current
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value if value else "yes"
    return table


def checkout_of(root: Path, branch: str) -> Optional[Path]:
    """Checkout (main or linked worktree) that has `branch` checked out."""
    for path, info in worktree_table(root).items():
        if info.get("branch") == f"refs/heads/{branch}":
            return Path(path)
    return None


def current_branch(path: Path) -> str:
    return git(path, "branch", "--show-current").stdout.strip()


def branch_exists(root: Path, branch: str) -> bool:
    return git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0


def dirty_files(path: Path, tracked_only: bool = False) -> List[str]:
    args = ["status", "--porcelain"] + (["--untracked-files=no"] if tracked_only else [])
    return [line for line in git(path, *args).stdout.splitlines() if line.strip()]


def lock_reason(root: Path, path: Path) -> Optional[str]:
    info = worktree_table(root).get(str(path.resolve()))
    if info and "locked" in info:
        return info["locked"] if info["locked"] != "yes" else "locked"
    return None


def create(
    root: Path,
    spec: str,
    nums: List[str],
    base: Optional[str] = None,
    install: Optional[str] = None,
    reuse_dirty: bool = False,
) -> List[Outcome]:
    """Create or reuse a worktree per task. Busy worktrees (claimed, or with
    uncommitted changes unless reuse_dirty) are reported, not touched."""
    base = base or current_branch(root)
    if not base:
        raise WorktreeError("main checkout is on a detached HEAD; pass --base")
    outcomes = []
    for num in nums:
        path, branch = task_path(root, spec, num), task_branch(spec, num)
        rel = str(path.relative_to(root))
        if path.exists():
            reason = lock_reason(root, path)
            dirty = dirty_files(path)
            if reason:
                outcomes.append(Outcome(num, "BUSY", f"claimed: {reason}", rel, branch))
                continue
            if dirty and not reuse_dirty:
                outcomes.append(Outcome(num, "BUSY", f"{len(dirty)} uncommitted change(s); commit or clean, or pass --reuse-dirty", rel, branch))
                continue
            outcome = Outcome(num, "EXISTS", "reused", rel, branch)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            if branch_exists(root, branch):
                git(root, "worktree", "add", str(path), branch)
                outcome = Outcome(num, "CREATED", "re-attached existing branch", rel, branch)
            else:
                git(root, "worktree", "add", "-b", branch, str(path), base)
                outcome = Outcome(num, "CREATED", f"from {base}", rel, branch)
        if install:
            proc = subprocess.run(install, shell=True, cwd=path, capture_output=True, text=True)
            if proc.returncode != 0:
                tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-10:])
                outcome = Outcome(num, "ERROR", f"install failed: `{install}`\n{tail}", rel, branch)
        outcomes.append(outcome)
    return outcomes


def create_integration(root: Path, spec: str, base: Optional[str] = None) -> Outcome:
    """Worktree on integrate/<spec> that task merges go into instead of the base branch."""
    base = base or current_branch(root)
    path, branch = integration_path(root, spec), integration_branch(spec)
    rel = str(path.relative_to(root))
    if path.exists():
        return Outcome("integration", "EXISTS", "reused", rel, branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(root, branch):
        git(root, "worktree", "add", str(path), branch)
    else:
        git(root, "worktree", "add", "-b", branch, str(path), base)
    return Outcome("integration", "CREATED", f"from {base}", rel, branch)


def claim(root: Path, spec: str, num: str, owner: str, takeover: bool = False) -> Outcome:
    path, branch = task_path(root, spec, num), task_branch(spec, num)
    rel = str(path.relative_to(root))
    if not path.exists():
        return Outcome(num, "MISSING", "no worktree; run `ck worktree create` first", rel, branch)
    reason = lock_reason(root, path)
    if reason and not takeover:
        return Outcome(num, "BUSY", f"claimed: {reason}", rel, branch)
    if reason:
        git(root, "worktree", "unlock", str(path))
    git(root, "worktree", "lock", "--reason", owner, str(path))
    return Outcome(num, "CLAIMED", owner + (" (took over)" if reason else ""), rel, branch)


def release(root: Path, spec: str, num: str) -> Outcome:
    path, branch = task_path(root, spec, num), task_branch(spec, num)
    rel = str(path.relative_to(root))
    if path.exists() and lock_reason(root, path):
        git(root, "worktree", "unlock", str(path))
    return Outcome(num, "RELEASED", "", rel, branch)


def merge(root: Path, spec: str, nums: List[str], into: Optional[str] = None, keep: bool = False) -> List[Outcome]:
    """Merge task branches into `into` one at a time (--no-ff); stop at the first conflict.

    The merge runs in the checkout that has `into` checked out. A merged task's
    worktree and branch are removed unless `keep`.
    """
    into = into or current_branch(root)
    target = checkout_of(root, into)
    if target is None:
        raise WorktreeError(f"branch {into} is not checked out anywhere; check it out or create the integration worktree")
    dirty = dirty_files(target, tracked_only=True)
    if dirty:
        raise WorktreeError(f"{target} has uncommitted changes to tracked files; refusing to merge into {into}")

    outcomes = []
    for num in nums:
        path, branch = task_path(root, spec, num), task_branch(spec, num)
        rel = str(path.relative_to(root))
        if not branch_exists(root, branch):
            outcomes.append(Outcome(num, "MISSING", "no branch", rel, branch))
            continue
        ahead = int(git(root, "rev-list", "--count", f"{into}..{branch}").stdout.strip() or 0)
        if ahead == 0:
            outcomes.append(Outcome(num, "SKIP", f"no commits ahead of {into}", rel, branch))
            continue
        if path.exists():
            reason = lock_reason(root, path)
            if reason:
                outcomes.append(Outcome(num, "BUSY", f"claimed: {reason}; release it first", rel, branch))
                continue
            if dirty_files(path):
                outcomes.append(Outcome(num, "ERROR", "task worktree has uncommitted changes", rel, branch))
                continue
        proc = git(target, "merge", "--no-ff", "-m", f"Merge {branch} (task {num})", branch, check=False)
        if proc.returncode != 0:
            conflicted = git(target, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
            git(target, "merge", "--abort", check=False)
            outcomes.append(
                Outcome(
                    num,
                    "CONFLICT",
                    f"merge aborted; conflicts in {', '.join(conflicted) or 'unknown files'}. "
                    f"Resolve inside the task worktree: git -C {path} merge {into}, then merge again",
                    rel,
                    branch,
                )
            )
            break
        if not keep:
            if path.exists():
                # Clean apart from ignored files (.venv, node_modules), checked above;
                # --force lets git drop those.
                git(root, "worktree", "remove", "--force", str(path))
            # -d (not -D) from the target checkout: git confirms the branch is merged into `into`.
            git(target, "branch", "-d", branch)
        outcomes.append(Outcome(num, "MERGED", f"{ahead} commit(s) into {into}", rel, branch))
    return outcomes


def status(root: Path, spec: str) -> List[Outcome]:
    """Every task worktree of a spec with its claim, dirtiness, and commits ahead of the main branch."""
    base = current_branch(root)
    outcomes = []
    prefix = f"{spec}-task-"
    for path in sorted((root / WORKTREES_DIR).glob(f"{prefix}*"), key=lambda p: int(p.name[len(prefix):] or 0) if p.name[len(prefix):].isdigit() else 0):
        num = path.name[len(prefix):]
        branch = task_branch(spec, num)
        bits = []
        reason = lock_reason(root, path)
        if reason:
            bits.append(f"claimed: {reason}")
        dirty = dirty_files(path)
        if dirty:
            bits.append(f"{len(dirty)} uncommitted")
        if branch_exists(root, branch):
            ahead = git(root, "rev-list", "--count", f"{base}..{branch}").stdout.strip()
            bits.append(f"{ahead} ahead of {base}")
        outcomes.append(Outcome(num, "BUSY" if reason else "EXISTS", ", ".join(bits), str(path.relative_to(root)), branch))
    return outcomes
