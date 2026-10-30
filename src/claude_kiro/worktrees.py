"""Task worktrees for parallel spec implementation (`ck worktree ...`).

Every task gets `<worktree_dir>/<spec>-task-<N>` on branch `feat/<spec>-task-<N>`,
created from a base branch. worktree_dir comes from specs/ck.json (default
.ck/worktrees); a spec whose worktrees still sit in .claude/worktrees (older ck)
keeps using them until they are gone. A task agent claims its
worktree (git's own `git worktree lock`) while it runs, so a second agent or a
setup step can see it is busy. A claim holds while it shows activity (its lock,
a gate run, a commit) within claim_stale_minutes; process IDs are never consulted,
since every sandboxed command has its own PID namespace. Merges go into whichever checkout has the
target branch, one task at a time, and stop at the first conflict. `land`
merges a batch instead: a `git merge-tree` precheck (no checkout touched),
one gate for the batch, and bisection by halving when the batch is red.

Git runs as a subprocess with an argument list, never through a shell, so
shell aliases and command-rewriting hooks don't interfere.
"""

import contextlib
import datetime
import os
import secrets
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Tuple

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
    with contextlib.suppress(OSError):
        path.with_name(path.name + ".decision.md").unlink()
        path.with_name(path.name + ".needs-input.json").unlink()
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


CLAIM_STALE_MINUTES = 60


def _lock_file(path: Path) -> Optional[Path]:
    proc = git(path, "rev-parse", "--path-format=absolute", "--git-dir", check=False)
    return Path(proc.stdout.strip()) / "locked" if proc.returncode == 0 else None


def claim_age(path: Path) -> float:
    """Seconds since the claim on `path` last showed activity: the lock file's mtime
    (set when claimed, refreshed by heartbeat()) or the branch tip's commit time,
    whichever is newer. Works for claims of any format, including ck 0.9's "pid N" ones."""
    times = []
    lock = _lock_file(path)
    with contextlib.suppress(OSError):
        if lock:
            times.append(lock.stat().st_mtime)
    with contextlib.suppress(ValueError):
        times.append(float(git(path, "log", "-1", "--format=%ct", check=False).stdout.strip()))
    return time.time() - max(times) if times else float("inf")


def stale_claim(root: Path, path: Path) -> Optional[str]:
    """The lock reason when the claim on `path` is stale (no activity for
    claim_stale_minutes from specs/ck.json), else None."""
    reason = lock_reason(root, path)
    if not reason:
        return None
    minutes = load_config(root).get("claim_stale_minutes", CLAIM_STALE_MINUTES)
    return reason if claim_age(path) > float(minutes) * 60 else None


def heartbeat(path: Path) -> None:
    """Mark a claimed worktree as still in use (best effort; `ck gate` calls it)."""
    lock = _lock_file(path)
    with contextlib.suppress(OSError):
        if lock and lock.exists():
            os.utime(lock)


def _create_one(
    root: Path, num: str, path: Path, branch: str, rel: str, base: str, reuse_dirty: bool
) -> Outcome:
    """The git side of `create` for one task; the caller holds `create_lock`.

    Returns CREATED/EXISTS (install still to run), or BUSY/ERROR (done with this task).
    A failed `git worktree add` is an ERROR outcome carrying git's message, so
    `--json` callers can read it and the other tasks still get their own outcome.
    """
    if path.exists():
        reason = lock_reason(root, path)
        stale = stale_claim(root, path) if reason else None
        if stale:  # its agent is gone; its commits stay on the branch for the next one
            git(root, "worktree", "unlock", str(path))
            reason = None
        dirty = dirty_files(path)
        if reason:
            return Outcome(num, "BUSY", f"claimed: {reason}", rel, branch)
        if dirty and not reuse_dirty:
            return Outcome(num, "BUSY", f"{len(dirty)} uncommitted change(s); commit or clean, or pass --reuse-dirty", rel, branch)
        return Outcome(num, "EXISTS", f"reused; released stale claim: {stale}" if stale else "reused", rel, branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if branch_exists(root, branch):
            git(root, "worktree", "add", str(path), branch)
            return Outcome(num, "CREATED", "re-attached existing branch", rel, branch)
        git(root, "worktree", "add", "-b", branch, str(path), base)
        return Outcome(num, "CREATED", f"from {base}", rel, branch)
    except WorktreeError as e:
        return Outcome(num, "ERROR", str(e), rel, branch)


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
        # Serialize the git side (worktree add, unlock) across concurrent `ck worktree
        # create` runs; the install below runs outside the lock so installs overlap.
        with create_lock(root):
            outcome = _create_one(root, num, path, branch, rel, base, reuse_dirty)
        if outcome.state not in ("CREATED", "EXISTS"):
            outcomes.append(outcome)
            continue
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
    stale = stale_claim(root, path) if reason else None
    if reason and not takeover and not stale:
        return Outcome(num, "BUSY", f"claimed: {reason}", rel, branch)
    if reason:
        git(root, "worktree", "unlock", str(path))
    owner = f"{owner} (claim {secrets.token_hex(4)} at {_now()})"
    git(root, "worktree", "lock", "--reason", owner, str(path))
    unpark(root, spec, num)  # a fresh attempt clears the park note; the decision note stays
    took = f" (took over stale claim: {stale})" if stale else " (took over)" if reason else ""
    return Outcome(num, "CLAIMED", owner + took, rel, branch)


def _private_write(path: Path, text: str, append: bool = False) -> None:
    """Write `text` to `path` with mode 0600 (created private, then re-chmodded)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(str(path), flags, 0o600)
    with os.fdopen(fd, "a" if append else "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


_SAFE_ID = re.compile(r"[0-9A-Za-z_][0-9A-Za-z_.\-]*")


def _check_ids(spec: str, num: str) -> None:
    for what, value in (("spec", spec), ("task", num)):
        if not isinstance(value, str) or not _SAFE_ID.fullmatch(value) or ".." in value:
            raise WorktreeError(f"invalid {what} name {value!r}")


def park_path(root: Path, spec: str, num: str) -> Path:
    return worktrees_dir(root, spec) / f"{spec}-task-{num}.needs-input.json"


def decision_path(root: Path, spec: str, num: str) -> Path:
    return worktrees_dir(root, spec) / f"{spec}-task-{num}.decision.md"


def park(root: Path, spec: str, num: str, note: Mapping) -> Path:
    """Write the park note (0600) for a task that needs my decision; returns its path.

    `priorDecision` is true when a decision note already exists for the task.
    """
    import json

    _check_ids(spec, num)
    body = {
        "schema": "ck-park/1",
        "spec": spec,
        "task": num,
        "file": str(note.get("file", "")),
        "kind": str(note.get("kind", "")),
        "test": str(note.get("test", "")),
        "requirement": str(note.get("requirement", "")),
        "decision": str(note.get("decision", "")),
        "parkedAt": _now(),
        "priorDecision": decision_path(root, spec, num).exists(),
    }
    path = park_path(root, spec, num)
    try:
        _private_write(path, json.dumps(body, indent=2) + "\n")
    except OSError as e:
        raise WorktreeError(f"cannot write {path}: {e}")
    return path


def parked(root: Path, spec: str) -> Dict[str, dict]:
    """task -> park note for every readable park note of the spec."""
    import json

    found: Dict[str, dict] = {}
    prefix, suffix = f"{spec}-task-", ".needs-input.json"
    for directory in dict.fromkeys((configured_dir(root), root / LEGACY_WORKTREES_DIR)):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob(f"{prefix}*{suffix}")):
            num = path.name[len(prefix):-len(suffix)]
            if num in found:
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                data.setdefault("task", num)
                found[num] = data
    return found


def unpark(root: Path, spec: str, num: str) -> None:
    for directory in dict.fromkeys((worktrees_dir(root, spec), configured_dir(root), root / LEGACY_WORKTREES_DIR)):
        with contextlib.suppress(OSError):
            (directory / f"{spec}-task-{num}.needs-input.json").unlink()


DECISION_FOOTER = ("If this says the test or requirement is wrong, it has already been changed in the main checkout; "
                   "do not change it yourself.")


def resolve(root: Path, spec: str, num: str, answer: str) -> Path:
    """Store my answer in the task's decision note (0600, appended), clear the park note; returns the note."""
    _check_ids(spec, num)
    path = decision_path(root, spec, num)
    existing = ""
    with contextlib.suppress(OSError):
        existing = path.read_text()
    if existing:
        existing = existing.replace("\n" + DECISION_FOOTER + "\n", "\n").rstrip("\n") + "\n\n"
    text = existing + f"## {_now()}\n\n{answer}\n\n{DECISION_FOOTER}\n"
    try:
        _private_write(path, text)
    except OSError as e:
        raise WorktreeError(f"cannot write {path}: {e}")
    unpark(root, spec, num)
    return path


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
            if reason and stale_claim(root, path):
                git(root, "worktree", "unlock", str(path))
            elif reason:
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


@contextlib.contextmanager
def create_lock(root: Path) -> Iterator[Path]:
    """Serialize `git worktree add`/unlock across concurrent `ck worktree create` runs.

    Blocking flock on <git common dir>/ck-create.lock (like `land_lock`, but it waits):
    many agents creating their own worktrees at once would otherwise collide on git's
    own lock files. The kernel drops the flock when the holder exits.
    """
    import fcntl

    path = _common_dir(root) / "ck-create.lock"
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@contextlib.contextmanager
def land_lock(root: Path, into: str) -> Iterator[Path]:
    """One landing per target branch at a time: it may reset the target to its pre-batch commit.

    An flock on the lock file, which the kernel drops when the holder exits, however it
    dies. Not a recorded PID: each sandboxed command has its own PID namespace, so a PID
    from another command can't be checked. The file stays; only the flock counts.
    """
    import fcntl

    path = _common_dir(root) / f"ck-land-{into.replace('/', '-')}.lock"
    with open(path, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise WorktreeError(f"another process is already landing into {into}; lock {path}") from None
        f.truncate(0)
        f.write(f"landing since {_now()}\n")  # for a human reading it; nothing parses it
        f.flush()
        try:
            yield path
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


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
                if reason and (release_claims or stale_claim(root, path)):
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


def report(root: Path, spec: str, into: Optional[str] = None) -> Dict:
    """What a run actually achieved, from git: the source for an orchestrator's final report.

    - merged: tasks with a merge commit made ON `into` (first-parent history) whose subject
      names the task branch (`ck worktree land/merge` write "Merge feat/<spec>-task-N (task N)";
      git's default "Merge branch 'feat/<spec>-task-N'" counts too). Merges of the target
      into a task branch are not on the first-parent line and don't count.
    - done: Done in tasks.md on `into` but with no merge commit (an earlier run, a direct commit).
    - failed: not merged or Done, and the task branch has commits that are not on `into`.
    - blocked: none of the above, and a dependency is not Done on `into` ("on" lists them).
    - needsInput: a park note exists (`ck worktree park`) and the task is neither merged nor Done.
    - unstarted: the rest: ready to start, but nothing was committed for it.

    Every task of tasks.md is in exactly one list. Agent results are not consulted.
    """
    from claude_kiro.hooks._shared.spec_parser import parse_tasks_text
    from claude_kiro.paths import LEGACY_SPECS_DIR, SPECS_DIR

    into = into or current_branch(root)
    if not into or not branch_exists(root, into):
        raise WorktreeError(f"branch {into or '(none)'} does not exist")
    head = git(root, "rev-parse", into).stdout.strip()
    content = None
    for base_dir in (SPECS_DIR, LEGACY_SPECS_DIR):
        shown = git(root, "show", f"{into}:{base_dir}/{spec}/tasks.md", check=False)
        if shown.returncode == 0:
            content = shown.stdout
            break
    if content is None:
        raise WorktreeError(f"no {SPECS_DIR}/{spec}/tasks.md on {into}")
    tasks = parse_tasks_text(content)
    known = {t.num for t in tasks}
    done = {t.num for t in tasks if t.done}

    pattern = re.compile(rf"feat/{re.escape(spec)}-task-([0-9A-Za-z_]+)")
    merges: Dict[str, Dict] = {}
    log = git(root, "log", "--merges", "--first-parent", "--reverse", "--format=%H%x09%cI%x09%s", into).stdout
    for line in log.splitlines():
        sha, at, subject = (line.split("\t", 2) + ["", ""])[:3]
        match = pattern.search(subject)
        if match and match.group(1) in known and match.group(1) not in merges:
            merges[match.group(1)] = {"commit": sha, "mergedAt": at, "subject": subject}

    out: Dict[str, List[Dict]] = {"merged": [], "done": [], "failed": [], "blocked": [], "unstarted": [], "needsInput": []}
    notes = parked(root, spec)
    for task in tasks:
        n = task.num
        if n in merges:
            out["merged"].append({"task": n, **merges[n], "done": n in done})
            continue
        if n in done:
            out["done"].append({"task": n})
            continue
        if n in notes:
            note = notes[n]
            out["needsInput"].append({"task": n, "file": note.get("file", ""), "kind": note.get("kind", ""),
                                      "test": note.get("test", ""), "requirement": note.get("requirement", ""),
                                      "decision": note.get("decision", ""), "parkedAt": note.get("parkedAt", "")})
            continue
        branch = task_branch(spec, n)
        ahead = int(git(root, "rev-list", "--count", f"{into}..{branch}").stdout.strip() or 0) if branch_exists(root, branch) else 0
        if ahead:
            path = task_path(root, spec, n)
            entry = {"task": n, "branch": branch, "ahead": ahead}
            claim = lock_reason(root, path) if path.exists() else None
            if claim:
                entry["claim"] = claim
            out["failed"].append(entry)
            continue
        waiting = [d for d in task.dependencies if d not in done]
        if waiting:
            out["blocked"].append({"task": n, "on": waiting})
        else:
            out["unstarted"].append({"task": n})
    return {"spec": spec, "into": into, "head": head, **out}
