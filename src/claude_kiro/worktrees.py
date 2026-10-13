"""Task worktrees for parallel spec implementation (`ck worktree ...`).

Every task gets `<worktree_dir>/<spec>-task-<N>` on branch `feat/<spec>-task-<N>`,
created from a base branch. worktree_dir comes from specs/ck.json (default
.ck/worktrees); a spec whose worktrees still sit in .claude/worktrees (older ck)
keeps using them until they are gone. A task agent claims its
worktree (git's own `git worktree lock`) while it runs, so a second agent or a
setup step can see it is busy. Merges go into whichever checkout has the
target branch, one task at a time, and stop at the first conflict. `land`
merges a batch instead: a `git merge-tree` precheck (no checkout touched),
one gate for the batch, and bisection by halving when the batch is red.

Git runs as a subprocess with an argument list, never through a shell, so
shell aliases and command-rewriting hooks don't interfere.
"""

import contextlib
import datetime
import os
import subprocess
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from claude_kiro.config import load_config
from claude_kiro.paths import LEGACY_WORKTREES_DIR, WORKTREES_DIR


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


def _cleanup_merged(root: Path, target: Path, path: Path, branch: str) -> str:
    """Remove a merged task's worktree and branch; best effort, the merge already happened.

    Returns "" or a note for the task's detail. A failure here must not turn a merged
    task into a failed one: its dependents would never start. (Seen in a sandboxed run:
    `git worktree remove` failing with "Device or resource busy".)
    """
    problems = []
    if path.exists():
        # Clean apart from ignored files (.venv, node_modules), checked before merging;
        # --force lets git drop those.
        rm = git(root, "worktree", "remove", "--force", str(path), check=False)
        if rm.returncode != 0:
            problems.append(f"worktree not removed ({(rm.stderr or rm.stdout).strip()})")
    if not problems:
        # -d (not -D) from the target checkout: git confirms the branch is merged into `into`.
        br = git(target, "branch", "-d", branch, check=False)
        if br.returncode != 0:
            problems.append(f"branch not deleted ({(br.stderr or br.stdout).strip()})")
    return "; cleanup: " + "; ".join(problems) + " (the merge stands; remove it later with git worktree remove + git branch -d)" if problems else ""


def main_root(start: Path) -> Path:
    """The main checkout of the repository containing `start` (not a linked worktree)."""
    common = git(start, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    common_path = Path(common)
    return common_path.parent if common_path.name == ".git" else common_path


def _has_spec(directory: Path, spec: str) -> bool:
    return directory.is_dir() and (
        any(directory.glob(f"{spec}-task-*")) or (directory / f"{spec}-integration").exists()
    )


def configured_dir(root: Path) -> Path:
    """specs/ck.json "worktree_dir" (default .ck/worktrees) as an absolute path."""
    raw = load_config(root).get("worktree_dir") or WORKTREES_DIR
    path = Path(os.path.expanduser(str(raw)))
    return path if path.is_absolute() else root / path


def worktrees_dir(root: Path, spec: Optional[str] = None) -> Path:
    """Where SPEC's worktrees live: the configured directory, unless the spec's worktrees
    are still in the legacy .claude/worktrees and none are in the configured one yet
    (an in-flight run keeps one location)."""
    configured = configured_dir(root)
    legacy = root / LEGACY_WORKTREES_DIR
    if spec and configured != legacy and not _has_spec(configured, spec) and _has_spec(legacy, spec):
        return legacy
    return configured


def rel_path(root: Path, path: Path) -> str:
    """`path` relative to the main checkout when it is inside it, else absolute."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def task_path(root: Path, spec: str, num: str) -> Path:
    return worktrees_dir(root, spec) / f"{spec}-task-{num}"


def task_branch(spec: str, num: str) -> str:
    return f"feat/{spec}-task-{num}"


def integration_path(root: Path, spec: str) -> Path:
    return worktrees_dir(root, spec) / f"{spec}-integration"


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
        rel = rel_path(root, path)
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
    rel = rel_path(root, path)
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
    rel = rel_path(root, path)
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
    rel = rel_path(root, path)
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
        rel = rel_path(root, path)
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
        note = "" if keep else _cleanup_merged(root, target, path, branch)
        outcomes.append(Outcome(num, "MERGED", f"{ahead} commit(s) into {into}{note}", rel, branch))
    return outcomes


@dataclass
class Landed:
    task: str
    state: str  # MERGED RED CONFLICT DEFERRED PROTECTED SKIP MISSING BUSY ERROR
    detail: str = ""
    branch: str = ""
    files: List[str] = field(default_factory=list)
    checkGreen: Optional[bool] = None
    checkTail: str = ""
    mergedAt: str = ""

    def line(self) -> str:
        text = f"{self.state} {self.task} ({self.branch})"
        if self.detail:
            text += f" - {self.detail}"
        if self.files:
            text += f" [{', '.join(self.files)}]"
        if self.checkTail:
            text += "\n" + "\n".join(f"    {line}" for line in self.checkTail.splitlines())
        return text


# gate(target checkout, task numbers) -> (green, failing output)
GateFn = Callable[[Path, List[str]], Tuple[bool, str]]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _common_dir(root: Path) -> Path:
    return Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextlib.contextmanager
def land_lock(root: Path, into: str) -> Iterator[Path]:
    """One landing per target branch at a time: it may reset the target to its pre-batch commit."""
    path = _common_dir(root) / f"ck-land-{into.replace('/', '-')}.lock"
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(path.read_text().strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid and _pid_alive(pid):
                raise WorktreeError(f"another process (pid {pid}) is already landing into {into}; lock {path}")
            path.unlink(missing_ok=True)  # stale: its process is gone
            continue
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        break
    else:
        raise WorktreeError(f"could not take the landing lock {path}")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


# Files Claude Code, git or the shell run or load on their own. A task branch that changes
# one is never landed by `ck worktree land`: an unattended agent could plant a hook, an MCP
# server or a permission rule that later runs outside the sandbox. A human merges it, or the
# spec opts in with "land": {"allow_paths": [...]} in specs/ck.json (read from the target).
PROTECTED_PATHS = (
    ".claude/**", "**/.claude/**", ".mcp.json", "**/.mcp.json",
    ".githooks/**", ".husky/**", ".git-hooks/**", ".pre-commit-config.yaml", ".envrc",
    "specs/ck.json",
)


def _glob(path: str, pattern: str) -> bool:
    """Glob on a repo-relative path (`*` crosses `/`); a leading "**/" also matches at the root."""
    pattern = pattern.removeprefix("./")
    return fnmatchcase(path, pattern) or (pattern.startswith("**/") and fnmatchcase(path, pattern[3:]))


def protected_changes(root: Path, base: str, branch: str, allow: Optional[List[str]] = None) -> List[str]:
    """Files the branch changes since it forked from `base` that match PROTECTED_PATHS and no `allow` glob."""
    out = git(root, "diff", "--name-only", "--no-renames", f"{base}...{branch}", check=False).stdout.split("\n")
    allow = [a for a in (allow or []) if isinstance(a, str) and a]
    return [f for f in (line.strip() for line in out) if f
            and any(_glob(f, p) for p in PROTECTED_PATHS) and not any(_glob(f, a) for a in allow)]


def unwritable_protected(root: Path, base: str, branch: str, target: Path) -> List[str]:
    """Protected files the branch changes (allowed or not) that `target` cannot write here.

    Claude Code's Bash sandbox keeps the main checkout's .claude/ read-only even when
    land.allow_paths opts a spec in; merging then dies with git's "unable to unlink old".
    Writing, replacing or deleting a file needs its directory (or, for a new one, the
    nearest existing parent) writable; access(2) reports a read-only bind mount as not.
    """
    out = []
    for f in protected_changes(root, base, branch):
        d = (target / f).parent
        while not d.exists() and d != target:
            d = d.parent
        if not os.access(d, os.W_OK):
            out.append(f)
    return out


def land_allow_paths(root: Path) -> List[str]:
    """specs/ck.json "land": {"allow_paths": [...]} of the checkout `root` (never a task branch's copy)."""
    land_cfg = load_config(root).get("land")
    allow = land_cfg.get("allow_paths") if isinstance(land_cfg, dict) else None
    return [a for a in allow if isinstance(a, str)] if isinstance(allow, list) else []


def merge_tree(root: Path, ours: str, theirs: str) -> Tuple[bool, str, List[str]]:
    """In-memory merge (`git merge-tree --write-tree`): (clean, tree, conflicted files). Touches no checkout."""
    proc = git(root, "merge-tree", "--write-tree", "--name-only", "--no-messages", ours, theirs, check=False)
    if proc.returncode not in (0, 1):
        raise WorktreeError(f"git merge-tree {ours} {theirs}: {(proc.stderr or proc.stdout).strip()}")
    lines = proc.stdout.splitlines()
    tree = lines[0].strip() if lines else ""
    files = list(dict.fromkeys(line.strip() for line in lines[1:] if line.strip()))
    return proc.returncode == 0, tree, files


def land(
    root: Path,
    spec: str,
    nums: List[str],
    into: Optional[str] = None,
    gate: Optional[GateFn] = None,
    install: Optional[str] = None,
    keep: bool = False,
    release_claims: bool = False,
    priority: Optional[Dict[str, int]] = None,
) -> Dict:
    """Land a batch of task branches on `into` with one gate, bisecting a red batch.

    1. Precheck with `git merge-tree` (no checkout touched): a branch that conflicts with
       the target is CONFLICT; one that conflicts only with an earlier branch of this batch
       is DEFERRED to the next batch. A branch that changes a PROTECTED_PATHS file
       (not allowed by specs/ck.json "land.allow_paths") is PROTECTED: never merged,
       it needs a human. So is one whose allowed protected files the target can't write
       (a sandboxed landing): attended merge only, outside the sandbox.
    2. Merge the clean branches into the target checkout (--no-ff, longest dependency
       chain first), refresh dependencies (`install`), and gate them together.
    3. Red with more than one task: reset the target to the commit before the batch and
       land each half in turn, until the culprits are isolated. Culprits are RED and stay
       off the target, with their branch and worktree kept; the rest are MERGED.

    Only the target checkout is ever reset, and only to the commit recorded before this
    batch. Merged tasks' worktrees and branches are removed unless `keep`.
    """
    into = into or current_branch(root)
    target = checkout_of(root, into)
    if target is None:
        raise WorktreeError(f"branch {into} is not checked out anywhere; check it out or create the integration worktree")
    dirty = dirty_files(target, tracked_only=True)
    if dirty:
        raise WorktreeError(f"{target} has uncommitted changes to tracked files; refusing to land into {into}")
    priority = priority or {}
    order = sorted(dict.fromkeys(nums), key=lambda n: (-priority.get(n, 1), int(n) if n.isdigit() else 0))

    with land_lock(root, into):
        started = _now()
        base = git(target, "rev-parse", "HEAD").stdout.strip()
        results: Dict[str, Landed] = {}
        candidates: List[str] = []
        for num in order:
            path, branch = task_path(root, spec, num), task_branch(spec, num)
            if not branch_exists(root, branch):
                results[num] = Landed(num, "MISSING", "no branch", branch)
                continue
            ahead = int(git(root, "rev-list", "--count", f"{base}..{branch}").stdout.strip() or 0)
            if ahead == 0:
                results[num] = Landed(num, "SKIP", f"no commits ahead of {into}", branch)
                continue
            if path.exists():
                reason = lock_reason(root, path)
                if reason and release_claims:
                    git(root, "worktree", "unlock", str(path))
                elif reason:
                    results[num] = Landed(num, "BUSY", f"claimed: {reason}; release it first", branch)
                    continue
                if dirty_files(path):
                    results[num] = Landed(num, "ERROR", "task worktree has uncommitted changes", branch)
                    continue
            candidates.append(num)

        # Protected files first: such a branch never reaches the target, whatever else holds.
        allow = land_allow_paths(target)
        for num in list(candidates):
            touched = protected_changes(root, base, task_branch(spec, num), allow)
            if touched:
                candidates.remove(num)
                results[num] = Landed(
                    num,
                    "PROTECTED",
                    "changes files Claude Code, git or the shell run on their own; not landed, a human must "
                    "review and merge it (or list the paths in specs/ck.json \"land\": {\"allow_paths\": [...]})",
                    task_branch(spec, num),
                    touched,
                )
                continue
            # Allowed, but this checkout can't write them (a sandboxed landing): stop before git does.
            readonly = unwritable_protected(root, base, task_branch(spec, num), target)
            if readonly:
                candidates.remove(num)
                results[num] = Landed(
                    num,
                    "PROTECTED",
                    "changes protected files that land.allow_paths allows but that are read-only here (Claude Code's "
                    "Bash sandbox keeps the main checkout's .claude/ read-only); attended merge only: after the "
                    f"operator reviews it, run `ck worktree land {spec} {num}` outside the sandbox",
                    task_branch(spec, num),
                    readonly,
                )

        # Precheck: each branch against the target, then the batch in order, in memory.
        clean: List[str] = []
        for num in candidates:
            ok, _, files = merge_tree(root, base, task_branch(spec, num))
            if not ok:
                path = task_path(root, spec, num)
                results[num] = Landed(
                    num,
                    "CONFLICT",
                    f"conflicts with {into}; target untouched. Resolve inside the task worktree: "
                    f"git -C {path} merge {into}, then land again",
                    task_branch(spec, num),
                    files,
                )
            else:
                clean.append(num)
        simulated, batch = base, []
        for num in clean:
            branch = task_branch(spec, num)
            ok, tree, files = merge_tree(root, simulated, branch)
            if not ok:
                earlier = ", ".join(f"Task {n}" for n in batch)
                results[num] = Landed(num, "DEFERRED", f"conflicts with {earlier} in this batch; land it in the next batch", branch, files)
                continue
            simulated = git(
                root, "-c", "user.name=ck", "-c", "user.email=ck@localhost",
                "commit-tree", tree, "-p", simulated, "-p", branch, "-m", "ck land precheck",
            ).stdout.strip()
            batch.append(num)

        gates: List[Dict] = []

        def head() -> str:
            return git(target, "rev-parse", "HEAD").stdout.strip()

        def merge_all(group: List[str]) -> List[str]:
            merged = []
            for num in group:
                branch = task_branch(spec, num)
                proc = git(target, "merge", "--no-ff", "-m", f"Merge {branch} (task {num})", branch, check=False)
                if proc.returncode != 0:
                    files = git(target, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
                    git(target, "merge", "--abort", check=False)
                    results[num] = Landed(num, "CONFLICT", f"merge into {into} conflicted (precheck missed it); aborted", branch, files)
                    continue
                merged.append(num)
            return merged

        def check(group: List[str]) -> Tuple[bool, str]:
            t0 = datetime.datetime.now()
            ok, tail = True, ""
            if install:
                proc = subprocess.run(install, shell=True, cwd=target, capture_output=True, text=True)
                if proc.returncode != 0:
                    out = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-20:])
                    ok, tail = False, f"install step failed: `{install}`\n{out}"
            if ok:
                ok, tail = gate(target, group)
            gates.append({"tasks": list(group), "green": ok, "seconds": int((datetime.datetime.now() - t0).total_seconds())})
            return ok, tail

        def mark_merged(group: List[str], green: Optional[bool]) -> None:
            at = _now()
            for num in group:
                results[num] = Landed(num, "MERGED", f"into {into}", task_branch(spec, num), [], green, "", at)

        def land_group(group: List[str], known_red: bool = False, red_tail: str = "") -> bool:
            """Land `group` on the current target head; True if all of it landed green."""
            if not known_red:
                start = head()
                merged = merge_all(group)
                if not merged:
                    return False
                if gate is None:
                    mark_merged(merged, None)
                    return len(merged) == len(group)
                ok, tail = check(merged)
                if ok:
                    mark_merged(merged, True)
                    return len(merged) == len(group)
                git(target, "reset", "--hard", "-q", start)
                group, red_tail = merged, tail
            if len(group) == 1:
                num = group[0]
                results[num] = Landed(num, "RED", f"the gate on {into} fails with this task; it was not landed", task_branch(spec, num), [], False, red_tail)
                return False
            half = len(group) // 2
            left_green = land_group(group[:half])
            # A green left half on the same base proves the right half red: skip re-gating it whole.
            land_group(group[half:], known_red=left_green, red_tail=red_tail)
            return False

        if batch:
            land_group(batch)

        if not keep:
            for num, outcome in results.items():
                if outcome.state != "MERGED":
                    continue
                outcome.detail += _cleanup_merged(root, target, task_path(root, spec, num), task_branch(spec, num))

        return {
            "into": into,
            "base": base,
            "head": head(),
            "startedAt": started,
            "finishedAt": _now(),
            "tasks": [asdict(results[n]) for n in order if n in results],
            "gates": gates,
            "bisected": len(gates) > 1,
        }


def status(root: Path, spec: str) -> List[Outcome]:
    """Every task worktree of a spec with its claim, dirtiness, and commits ahead of the main branch."""
    base = current_branch(root)
    outcomes = []
    prefix = f"{spec}-task-"
    paths = [p for d in dict.fromkeys((configured_dir(root), root / LEGACY_WORKTREES_DIR)) for p in d.glob(f"{prefix}*") if p.is_dir()]
    for path in sorted(paths, key=lambda p: int(p.name[len(prefix):] or 0) if p.name[len(prefix):].isdigit() else 0):
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
        outcomes.append(Outcome(num, "BUSY" if reason else "EXISTS", ", ".join(bits), rel_path(root, path), branch))
    return outcomes
