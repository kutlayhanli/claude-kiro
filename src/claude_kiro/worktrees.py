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
from typing import TYPE_CHECKING, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from claude_kiro.config import load_config
from claude_kiro.paths import LEGACY_WORKTREES_DIR, WORKTREES_DIR

if TYPE_CHECKING:
    from claude_kiro.protect import Refusal
    from claude_kiro.progress import Liveness


class WorktreeError(Exception):
    pass


@dataclass
class Outcome:
    task: str
    state: str  # CREATED EXISTS BUSY CLAIMED RELEASED MERGED SKIP CONFLICT MISSING ERROR
    detail: str = ""
    path: str = ""
    branch: str = ""
    run: Optional[str] = None  # progress run recorded by the claim (status only)
    hooks: Optional[Dict] = None  # HookStatus of the integration worktree's hooks (create_integration only)

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
        out = asdict(self)
        if out.get("run") is None:
            out.pop("run", None)
        if out.get("hooks") is None:
            out.pop("hooks", None)
        return out


def git(cwd: Path, *args: str, check: bool = True, env: Optional[Mapping[str, str]] = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=dict(env) if env else None)
    if check and proc.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()}")
    return proc


def _commit_shape(root: Path, commit: str) -> Tuple[str, List[str]]:
    """(tree, parents) of `commit`, which may name an annotated tag (`git show` would print the tag header first)."""
    tree, _, parents = git(root, "log", "-1", "--format=%T%n%P", commit).stdout.strip().partition("\n")
    return tree.strip(), parents.split()


def _tree_protected(root: Path, old: str, tree: str) -> List[str]:
    """Protected paths (unquoted) that differ between commit `old` and tree `tree`."""
    from claude_kiro import protect

    out = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "--no-renames", "-z", old, tree],
                         capture_output=True).stdout
    names = [n.decode("utf-8", "replace") for n in out.split(b"\0") if n]
    return [n for n in names if protect.is_protected(n)]


def _token_env(root: Path, into: str, old: str, tree: str, parents: List[str], reason: str,
               files: Sequence[str] = ()) -> Optional[Dict[str, str]]:
    """A process environment carrying a fresh one-shot landing token for this exact update of `integrate/*`
    (the hooks let an update through only with it, 9.7); None for any other branch or when none can be issued."""
    from claude_kiro import protect

    if not into.startswith("integrate/"):
        return None
    try:
        rec = protect.issue_token(root, f"refs/heads/{into}", old, tree, parents, reason, files)
    except (OSError, subprocess.CalledProcessError):
        return None
    return {**os.environ, "CK_LANDING_TOKEN": rec.id}


def _git_merge(root: Path, target: Path, into: str, branch: str, message: str) -> subprocess.CompletedProcess:
    """`git merge --no-ff` of `branch` in `target`, with a landing token when `into` is an integrate/* branch."""
    env = None
    if into.startswith("integrate/"):
        old = git(target, "rev-parse", "HEAD").stdout.strip()
        ok, tree, _ = merge_tree(root, old, branch)
        if ok and tree:
            other = git(root, "rev-parse", branch).stdout.strip()
            env = _token_env(root, into, old, tree, [old, other], f"land {branch} into {into} ({other[:12]})",
                             _tree_protected(root, old, tree))
    extra = {"env": env} if env else {}
    return git(target, "merge", "--no-ff", "-m", message, branch, check=False, **extra)


@dataclass(frozen=True)
class RecoveryFacts:
    into: str
    target: str                  # realpath of the checkout of `into`
    managed: bool                # integrate/* checkout under the worktree directory, not the main checkout
    dirty: Tuple[str, ...]       # tracked dirty paths plus unmerged paths, sorted
    merge_in_progress: bool      # MERGE_HEAD exists in the target
    head: str
    refusal: Optional["Refusal"]


def decide_recovery(facts: RecoveryFacts) -> Tuple[str, str]:
    """("clean" | "recover" | "refuse", reason). Pure (14.1, 14.3, 14.5)."""
    if not facts.dirty and not facts.merge_in_progress:
        return "clean", ""
    base = f"{facts.target} has uncommitted changes to tracked files; refusing to land into {facts.into}"
    if not facts.managed:
        return "refuse", base
    refusal = facts.refusal
    if not (refusal is not None and refusal.ref == f"refs/heads/{facts.into}" and refusal.head == facts.head
            and refusal.checkout == facts.target):
        return "refuse", base + ": no refused ref update is recorded at this head; ck will not discard it"
    return "recover", ""


def _is_managed_checkout(root: Path, target: Path, into: str) -> bool:
    if not into.startswith("integrate/"):
        return False
    real = os.path.realpath(target)
    if real == os.path.realpath(root):
        return False
    for directory in (configured_dir(root), root / LEGACY_WORKTREES_DIR):
        base = os.path.realpath(directory)
        if real == base or real.startswith(base + os.sep):
            return True
    return False


def _git_dir_of(target: Path) -> Path:
    raw = git(target, "rev-parse", "--git-dir").stdout.strip()
    path = Path(raw)
    return path if path.is_absolute() else target / path


def _recovery_state(target: Path) -> Tuple[List[str], bool, str]:
    dirty = set(dirty_files(target, tracked_only=True))
    paths = set()
    for line in dirty:
        paths.add(line[3:].split(" -> ")[-1].strip().strip('"') if len(line) > 3 else line)
    unmerged = git(target, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split("\n")
    paths.update(p for p in unmerged if p)
    merging = (_git_dir_of(target) / "MERGE_HEAD").exists()
    head = git(target, "rev-parse", "HEAD").stdout.strip()
    return sorted(paths), merging, head


def recover_target(root: Path, target: Path, into: str) -> Optional[Dict]:
    """Restore a refused integration checkout before a write into it (Story 14).

    None when clean; WorktreeError when it must be refused; else resets to the checkout's own head and returns
    {"paths", "mergeInProgress", "head"}."""
    from claude_kiro.protect import clear_refusal, refusal_for

    paths, merging, head = _recovery_state(target)
    facts = RecoveryFacts(
        into=into, target=os.path.realpath(target), managed=_is_managed_checkout(root, target, into),
        dirty=tuple(paths), merge_in_progress=merging, head=head,
        refusal=refusal_for(root, f"refs/heads/{into}") if (paths or merging) else None)
    verdict, reason = decide_recovery(facts)
    if verdict == "clean":
        return None
    if verdict == "refuse":
        raise WorktreeError(reason)
    try:
        _git_reset(root, target, into, head)
    except (WorktreeError, subprocess.CalledProcessError, OSError) as exc:
        raise WorktreeError(f"could not restore {target} to {head[:12]}: {exc}; the refusal record is kept") from exc
    after_paths, after_merging, after_head = _recovery_state(target)
    if after_head != head or after_merging or after_paths:
        raise WorktreeError(f"restoring {target} to {head[:12]} did not leave it clean; the refusal record is kept")
    clear_refusal(root, f"refs/heads/{into}", head)
    return {"paths": paths, "mergeInProgress": merging, "head": head}


def _git_reset(root: Path, target: Path, into: str, commit: str) -> None:
    """`git reset --hard` of `target` to `commit`, with a landing token when `into` is an integrate/* branch.

    Raises WorktreeError when git refuses: land() must not carry on over commits it failed to take back."""
    env = None
    if into.startswith("integrate/"):
        old = git(target, "rev-parse", "HEAD").stdout.strip()
        tree, parents = _commit_shape(root, commit)
        env = _token_env(root, into, old, tree, parents, f"reset {into} to {commit[:12]}",
                         _tree_protected(root, old, tree))
    extra = {"env": env} if env else {}
    git(target, "reset", "--hard", "-q", commit, **extra)


def _ensure_hooks(root: Path, worktree: Path, notes: Optional[List[Dict]] = None, managed_only: bool = False) -> None:
    """`protect.ensure_hooks`; a re-install or a problem is appended to `notes` (never raises)."""
    from claude_kiro import protect

    try:
        if managed_only and not protect.hooks_managed(root):
            return
        before = protect.hook_status(root, worktree)
        if before.installed and before.active and before.intact:
            return
        status = protect.ensure_hooks(root, worktree)
        if notes is not None:
            notes.append({**protect.hook_status_json(status),
                          "reinstalled": bool(status.installed and status.active and status.intact)})
    except Exception as exc:  # noqa: BLE001 - hooks never turn a landing into a crash
        if notes is not None:
            notes.append({"installed": False, "active": False, "intact": False, "chainedTo": None,
                          "problems": [str(exc)]})


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


# Generated artifacts by whole path segment: these directory names anywhere in the path, `target`
# only as the top-level directory (Rust/Maven build output; a `src/.../target/` package is source).
GENERATED_DIRS = frozenset({"__pycache__", ".pytest_cache", "node_modules", ".venv", ".mypy_cache", ".ruff_cache"})
GENERATED_ROOT_DIRS = frozenset({"target"})
GENERATED_PATH_SUFFIXES = (".pyc", ".pyo")


def _is_generated_status_path(path: str) -> bool:
    """True when an untracked `git status` path is a generated artifact, not real work."""
    p = path.strip().strip('"').rstrip("/")
    parts = [part for part in p.split("/") if part]
    if not parts:
        return False
    return (
        any(part in GENERATED_DIRS for part in parts)
        or (len(parts) >= 1 and parts[0] in GENERATED_ROOT_DIRS and (len(parts) > 1 or path.rstrip().endswith("/")))
        or parts[-1].endswith(GENERATED_PATH_SUFFIXES)
    )


def _is_generated_status_line(line: str) -> bool:
    """A porcelain line that is only a generated artifact. Tracked changes always count as work."""
    if not line.startswith("?? "):
        return False
    return _is_generated_status_path(line[3:])


def dirty_files(path: Path, tracked_only: bool = False) -> List[str]:
    args = ["status", "--porcelain"] + (["--untracked-files=no"] if tracked_only else [])
    lines = [line for line in git(path, *args).stdout.splitlines() if line.strip()]
    # Untracked generated artifacts (__pycache__/, *.pyc, .pytest_cache/, a top-level target/, ...)
    # never count as real work: a task worktree containing only those merges cleanly.
    return [line for line in lines if not _is_generated_status_line(line)]


def _unquote_c(text: str) -> str:
    """Undo git's C-style quoting of porcelain values (non-ASCII bytes become octal escapes inside quotes)."""
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return text
    body, out, i = text[1:-1], bytearray(), 0
    simple = {"n": 10, "t": 9, "r": 13, "a": 7, "b": 8, "f": 12, "v": 11, "\\": 92, '"': 34}
    while i < len(body):
        ch = body[i]
        if ch != "\\" or i + 1 >= len(body):
            out += ch.encode("utf-8")
            i += 1
        elif body[i + 1] in "01234567":
            j = i + 1
            while j < len(body) and j < i + 4 and body[j] in "01234567":
                j += 1
            out.append(int(body[i + 1:j], 8) & 255)
            i = j
        else:
            out.append(simple.get(body[i + 1], ord(body[i + 1])))
            i += 2
    return out.decode("utf-8", "replace")


def lock_reason(root: Path, path: Path) -> Optional[str]:
    info = worktree_table(root).get(str(path.resolve()))
    if info and "locked" in info:
        return _unquote_c(info["locked"]) if info["locked"] != "yes" else "locked"
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
    claim_stale_minutes from specs/ck.json), else None. A claim whose recorded
    run is still running is never stale, however old it is (16.3)."""
    from claude_kiro import progress

    reason = lock_reason(root, path)
    if not reason:
        return None
    run = claim_info(reason).run
    if run and progress.run_liveness(run, None, progress.clock_now()) == "running":
        return None
    minutes = load_config(root).get("claim_stale_minutes", CLAIM_STALE_MINUTES)
    return reason if claim_age(path) > float(minutes) * 60 else None


@dataclass(frozen=True)
class ClaimInfo:
    owner: str
    token: Optional[str]
    at: Optional[str]
    run: Optional[str]  # progress run key; None for older claims and hand-made locks


_CLAIM_SUFFIX = re.compile(r"^(?P<owner>.*) \(claim (?P<token>\S+) at (?P<at>\S+)(?: run (?P<run>.*))?\)$", re.DOTALL)


def claim_info(reason: Optional[str]) -> ClaimInfo:
    """Parse a lock reason: only the trailing `(claim TOKEN at TIME[ run KEY])` ck wrote counts, never owner text."""
    text = reason or ""
    m = _CLAIM_SUFFIX.match(text)
    if not m:
        return ClaimInfo(text, None, None, None)
    return ClaimInfo(m.group("owner"), m.group("token"), m.group("at"), m.group("run") or None)


def claim_release_decision(claim: ClaimInfo, starting_run: Optional[str], liveness: Optional["Liveness"], age_s: float,
                           stale_minutes: float = CLAIM_STALE_MINUTES) -> str:
    """"own" (the starting run's claim), "keep" or "release" (16.2-16.5, 16.7)."""
    if claim.run is not None:
        if starting_run is not None and claim.run == starting_run:
            return "own"
        return "keep" if liveness == "running" else "release"
    return "release" if age_s > float(stale_minutes) * 60 else "keep"


@dataclass
class RunStartClaims:
    # `run` is None for a claim with no run ID (16.4). A kept entry whose release was attempted and failed carries
    # `error` (git's message), so a caller can tell "live run, kept on purpose" from "unlock failed".
    released: List[Dict[str, Optional[str]]] = field(default_factory=list)  # {task, run, reason}
    kept: List[Dict[str, Optional[str]]] = field(default_factory=list)      # {task, run[, error]}


def release_for_run_start(root: Path, spec: str, run: Optional[str], env: Optional[Mapping[str, str]] = None,
                          now: Optional[datetime.datetime] = None) -> RunStartClaims:
    """Unlock the claims of runs that are over (commits stay); keep live runs' and the starting run's own."""
    from claude_kiro import progress

    now = now or datetime.datetime.now(datetime.timezone.utc)
    minutes = float(load_config(root).get("claim_stale_minutes", CLAIM_STALE_MINUTES))
    out = RunStartClaims()
    prefix = f"{spec}-task-"
    paths = [p for d in dict.fromkeys((configured_dir(root), root / LEGACY_WORKTREES_DIR)) for p in d.glob(f"{prefix}*") if p.is_dir()]
    for path in sorted(paths, key=lambda p: p.name):
        num = path.name[len(prefix):]
        reason = lock_reason(root, path)
        if not reason:
            continue
        info = claim_info(reason)
        liveness = progress.run_liveness(info.run, env, now) if info.run and info.run != run else None
        decision = claim_release_decision(info, run, liveness, claim_age(path), minutes)
        if decision == "release":
            try:
                git(root, "worktree", "unlock", str(path))
            except WorktreeError as exc:
                out.kept.append({"task": num, "run": info.run, "error": str(exc) or "git worktree unlock failed"})
                continue
            out.released.append({"task": num, "run": info.run, "reason": reason})
        else:
            out.kept.append({"task": num, "run": info.run})
    return out


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
            notes: List[Dict] = []
            _ensure_hooks(root, path, notes, managed_only=True)
            if notes and outcome.state in ("CREATED", "EXISTS"):
                outcome.detail += _hook_detail(notes)
        outcomes.append(outcome)
    return outcomes


def _hook_detail(notes: List[Dict]) -> str:
    if not notes:
        return ""
    last = notes[-1]
    if last.get("problems") and not last.get("reinstalled"):
        return f"; git hooks not installed: {'; '.join(last['problems'])}"
    return "; git hooks reinstalled" + (f" (chained to {last['chainedTo']})" if last.get("chainedTo") else "")


def _record_baseline(root: Path, spec: str, branch: str) -> None:
    """The approved protected state of an integration branch that has none: what its tip carries (10.4); best effort.

    Only when no state file exists: one that fails to verify is left alone for the audit to report as incomplete
    (10.5), since resealing it would launder whatever damaged it."""
    from claude_kiro import protect

    ref = f"refs/heads/{branch}"
    try:
        if protect.approved_state_file(root, spec) is not None:
            return
        tip = git(root, "rev-parse", ref).stdout.strip()
        protect.record_approved_state(root, spec, ref, tip, protect.protected_files_at(root, tip), {"by": "baseline"})
    except (OSError, ValueError, subprocess.CalledProcessError, WorktreeError):
        pass


def create_integration(root: Path, spec: str, base: Optional[str] = None) -> Outcome:
    """Worktree on integrate/<spec> that task merges go into instead of the base branch.

    The git hooks are installed (or repaired) first, so the creation of the ref already needs its token (9.1).
    """
    base = base or current_branch(root)
    path, branch = integration_path(root, spec), integration_branch(spec)
    rel = rel_path(root, path)
    notes: List[Dict] = []
    _ensure_hooks(root, path if path.exists() else root, notes)
    from claude_kiro import protect

    ref = f"refs/heads/{branch}"
    state_file = protect.approved_state_file(root, spec)  # by spec: a ref lost from refs.json still has its state
    approved = protect.read_approved_state(root, spec, ref) if state_file is not None else None
    unverified = state_file is not None and approved is None
    recover = (f"delete {state_file} in your own terminal and run `ck worktree integration {spec} --base {base}` again")

    def status() -> Dict:
        st = protect.hook_status(root, path if path.exists() else root)
        out = protect.hook_status_json(st)
        out["problems"] = list(dict.fromkeys([*(notes[-1]["problems"] if notes else []), *st.problems]))
        return out

    def detail(text: str) -> str:
        note = (f"; the approved protected state {state_file} cannot be verified and was left as it is, so the audit "
                "will report incomplete" if unverified else "")
        return text + _hook_detail(notes) + note

    if path.exists():
        _record_baseline(root, spec, branch)
        return Outcome("integration", "EXISTS", detail("reused"), rel, branch, hooks=status())
    path.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(root, branch):
        git(root, "worktree", "add", str(path), branch)
        _record_baseline(root, spec, branch)
    else:
        if unverified:
            raise WorktreeError(
                f"refused: creating {ref}: the approved protected state {state_file} cannot be verified, and ck will "
                f"not reseal it from {base}. To accept {base} as the new approved state, review its protected files, "
                f"then {recover}.")
        tree, parents = _commit_shape(root, base)
        base_files = protect.protected_files_at(root, base)
        if approved is not None and dict(approved.files) != base_files:
            # 9.12: the approved state exists and the base differs from it; never reseal it from the base
            drift = sorted(k for k in {*base_files, *approved.files} if base_files.get(k) != approved.files.get(k))
            raise WorktreeError(
                f"refused: creating {ref} from {base} carries protected files that differ from the approved state: "
                f"{', '.join(drift)}. `ck worktree approve SPEC TASK` lands a task into an existing integrate branch, "
                f"so it cannot re-create this one. To accept {base} as the new approved state, review those files, "
                f"then {recover}.")
        env = _token_env(root, branch, protect.ZEROS, tree, parents, f"create {branch} from {base}",
                         sorted(base_files))
        git(root, "worktree", "add", "-b", branch, str(path), base, **({"env": env} if env else {}))
        _record_baseline(root, spec, branch)
    return Outcome("integration", "CREATED", detail(f"from {base}"), rel, branch, hooks=status())


def claim(root: Path, spec: str, num: str, owner: str, takeover: bool = False, run: Optional[str] = None) -> Outcome:
    path, branch = task_path(root, spec, num), task_branch(spec, num)
    rel = rel_path(root, path)
    if not path.exists():
        return Outcome(num, "MISSING", "no worktree; run `ck worktree create` first", rel, branch)
    reason = lock_reason(root, path)
    stale = stale_claim(root, path) if reason else None
    own = bool(reason and run and claim_info(reason).run == run)  # the same run's claim is its own (16.7)
    if reason and not takeover and not stale and not own:
        return Outcome(num, "BUSY", f"claimed: {reason}", rel, branch)
    if reason:
        git(root, "worktree", "unlock", str(path))
    owner = f"{owner} (claim {secrets.token_hex(4)} at {_now()}{f' run {run}' if run else ''})"
    git(root, "worktree", "lock", "--reason", owner, str(path))
    if unpark(root, spec, num):  # a fresh attempt clears the park note; the decision note stays
        _record_unpark(root, spec, num)
    took = f" (took over stale claim: {stale})" if stale else " (took over)" if reason and not own else ""
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


def unpark(root: Path, spec: str, num: str) -> bool:
    """Remove the park note; True when one was removed."""
    removed = False
    for directory in dict.fromkeys((worktrees_dir(root, spec), configured_dir(root), root / LEGACY_WORKTREES_DIR)):
        try:
            (directory / f"{spec}-task-{num}.needs-input.json").unlink()
            removed = True
        except OSError:
            pass
    return removed


def _record_unpark(root: Path, spec: str, num: str) -> None:
    """Append `unpark` to the spec's active run. Never raises: progress does not change a claim."""
    try:
        from . import progress

        env = os.environ
        now = progress.clock_now(env)
        key = progress.active_run(spec, root, env, now=now)
        if key:
            progress.append(key, {"kind": "unpark", "at": now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                  "task": num}, env)
    except Exception:  # noqa: BLE001
        pass


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


def _record_landed(root: Path, spec: str, into: str, landed: Dict[str, List[str]], approved: Dict[str, List[str]]) -> None:
    """Record in the approved protected state only the protected paths the landed tasks changed (10.4).

    Never the whole protected state: a bypass write elsewhere must not be laundered. `landed` maps task -> its
    protected paths; those listed in `approved[task]` are recorded `by: "approval"`, the rest `by: "allow-list"`.
    Best effort: a state that cannot be written is left for the audit to report."""
    from claude_kiro import protect

    if not into.startswith("integrate/") or not landed:
        return
    ref = f"refs/heads/{into}"
    try:
        tip = git(root, "rev-parse", ref).stdout.strip()
        at_tip = protect.protected_files_at(root, tip)
        if protect.read_approved_state(root, spec, ref) is None:
            return  # never reseal a state that is absent or fails to verify: the audit reports it incomplete
        for n in sorted(landed):
            for by in ("allow-list", "approval"):
                paths = sorted(f for f in landed[n] if (f in approved.get(n, ())) == (by == "approval"))
                if paths:
                    protect.record_approved_state(root, spec, ref, tip, {f: at_tip.get(f) for f in paths},
                                                  {"by": by, "task": n, "files": paths})
    except (OSError, ValueError, subprocess.CalledProcessError, WorktreeError):
        pass


def _merge_commit_of(root: Path, base: str, tip: str, branch: str) -> Optional[str]:
    """The first-parent merge commit on `tip` (after `base`) whose second parent is the tip of `branch`."""
    try:
        want = git(root, "rev-parse", f"refs/heads/{branch}").stdout.strip()
        lines = git(root, "rev-list", "--first-parent", "--parents", f"{base}..{tip}").stdout.splitlines()
    except (subprocess.CalledProcessError, WorktreeError):
        return None
    for line in lines:
        ids = line.split()
        if len(ids) >= 3 and ids[2] == want:
            return ids[0]
    return None


def _record_fixtures(root: Path, target: Path, spec: str, base: str, merged: List[str],
                     results: Dict[str, "Landed"]) -> Optional[str]:
    """Lock recorded fixtures of the tasks that merged (15.1, 15.2); returns a config error text or None.

    Per merged task, in landing order: new files get a lock entry (task, time); changed or deleted locked files
    are `fixtureDrift` unless their label is handcrafted with a reason covering the new content, which updates
    the entry's hash and records the reason. Landing is never refused by drift. No globs: nothing is written."""
    from claude_kiro import fixtures

    if not merged:
        return None
    config = load_config(target)
    try:
        globs = fixtures.fixture_globs(dict(config))
    except fixtures.ConfigError as exc:
        return str(exc)
    if not globs:
        return None
    entries, tampered = fixtures.read_lock(root, spec)
    labels = fixtures.read_labels(target, config)
    changed = False
    tip_ref = git(target, "rev-parse", "HEAD").stdout.strip()
    for num in merged:
        branch = task_branch(spec, num)
        diff = git(root, "diff", "--name-status", "--no-renames", "-z", f"{base}...{branch}").stdout
        paths: Dict[str, str] = {}
        parts = diff.split("\0")
        for i in range(0, len(parts) - 1, 2):
            if parts[i + 1]:
                paths[parts[i + 1]] = parts[i].strip()
        recorded = fixtures.recorded_files(config, sorted(paths))
        changes: Dict[str, Optional[str]] = {}
        source = _merge_commit_of(root, base, tip_ref, branch) or branch
        for path in recorded:
            if paths[path].startswith("D"):
                changes[path] = None
                continue
            proc = subprocess.run(["git", "-C", str(root), "show", f"{source}:{path}"], capture_output=True)
            if proc.returncode != 0:
                raise WorktreeError(f"cannot read recorded fixture {path} of task {num} at {source}")
            changes[path] = fixtures.sha256_text(proc.stdout)
        if not changes:
            continue
        report = fixtures.lock_decision(entries, changes, labels, tampered=tampered)
        # A deletion has no content to seal, so one that lock_decision accepts as handcrafted is still reported.
        accepted = [h_ for h_ in report.handcrafted if changes[h_["file"]] is not None]
        deleted = [{"file": h_["file"], "change": "deleted"} for h_ in report.handcrafted if changes[h_["file"]] is None]
        results[num].fixtureDrift = [{"file": d["file"], "task": num, "change": d["change"]}
                                     for d in sorted([*report.drift, *deleted], key=lambda d: d["file"])]
        if tampered:
            continue  # never reseal a lock that does not verify: the audit reports it
        at = _now()
        for path in report.new_locks:
            entries[path] = fixtures.LockEntry(sha256=str(changes[path]), task=num, locked_at=at)
            changed = True
        for h_ in accepted:
            old = entries[h_["file"]]
            entries[h_["file"]] = fixtures.LockEntry(sha256=str(changes[h_["file"]]), task=old.task,
                                                     locked_at=old.locked_at, handcrafted=h_["reason"])
            changed = True
    if changed:
        fixtures.write_lock(root, spec, entries)
    return None


def merge(root: Path, spec: str, nums: List[str], into: Optional[str] = None, keep: bool = False,
          on_recovered: Optional[Callable[[Path, Dict], None]] = None) -> List[Outcome]:
    """Merge task branches into `into` one at a time (--no-ff); stop at the first conflict.

    The merge runs in the checkout that has `into` checked out. A merged task's
    worktree and branch are removed unless `keep`.
    """
    into = into or current_branch(root)
    target = checkout_of(root, into)
    if target is None:
        raise WorktreeError(f"branch {into} is not checked out anywhere; check it out or create the integration worktree")
    with land_lock(root, into):
        recovered = recover_target(root, target, into)
    if recovered and on_recovered:
        on_recovered(target, recovered)

    outcomes = []
    allow = land_allow_paths(target)
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
        touched = protected_changes(root, into, branch, allow)
        landing_paths = protected_changes(root, into, branch)
        if touched:
            outcomes.append(Outcome(num, "PROTECTED", f"changes protected files not covered by land.allow_paths: "
                                    f"{', '.join(touched)}; not merged, a human must review and approve it", rel, branch))
            break
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
        proc = _git_merge(root, target, into, branch, f"Merge {branch} (task {num})")
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
        _record_landed(root, spec, into, {num: landing_paths}, {})
        note = "" if keep else _cleanup_merged(root, target, path, branch)
        outcomes.append(Outcome(num, "MERGED", f"{ahead} commit(s) into {into}{note}", rel, branch))
    return outcomes


@dataclass
class Landed:
    task: str
    state: str  # MERGED RED CONFLICT DEFERRED PROTECTED REFUSED SKIP MISSING BUSY ERROR
    detail: str = ""
    branch: str = ""
    files: List[str] = field(default_factory=list)
    checkGreen: Optional[bool] = None
    checkTail: str = ""
    mergedAt: str = ""
    failures: List[Dict] = field(default_factory=list)  # the gate's failure entries (ck-gate/1), RED only
    fixtureDrift: List[Dict] = field(default_factory=list)  # [{file, task, change}] recorded fixtures changed (15.2)
    commit: Optional[str] = None  # the target's HEAD after a spec-edit landing (MERGED only)
    moved: List[Dict] = field(default_factory=list)  # tests a spec edit moved out of a Verify selection (8.7)
    recovered: Optional[Dict] = None  # the checkout was restored before this landing (14.2)

    def line(self) -> str:
        text = f"{self.state} {self.task} ({self.branch})"
        if self.detail:
            text += f" - {self.detail}"
        if self.files:
            text += f" [{', '.join(self.files)}]"
        if self.checkTail:
            text += "\n" + "\n".join(f"    {line}" for line in self.checkTail.splitlines())
        return text


# gate(target checkout, task numbers) -> (green, failing output, failure entries)
GateFn = Callable[[Path, List[str]], Tuple[bool, str, List[Dict]]]


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
def land_lock(root: Path, into: str, wait_s: float = 0) -> Iterator[Path]:
    """One landing per target branch at a time: it may reset the target to its pre-batch commit.

    An flock on the lock file, which the kernel drops when the holder exits, however it
    dies. Not a recorded PID: each sandboxed command has its own PID namespace, so a PID
    from another command can't be checked. The file stays; only the flock counts.
    `wait_s` is how long to wait for a holder to finish before refusing (0: refuse at once).
    """
    import fcntl

    path = _common_dir(root) / f"ck-land-{into.replace('/', '-')}.lock"
    with open(path, "a+") as f:
        deadline = time.monotonic() + max(0.0, float(wait_s))
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise WorktreeError(f"another process is already landing into {into}; lock {path}") from None
                time.sleep(0.1)
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
    out = git(root, "diff", "--name-only", "--no-renames", "-z", f"{base}...{branch}", check=False).stdout.split("\0")
    allow = [a for a in (allow or []) if isinstance(a, str) and a]
    return [f for f in out if f
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
    approved: Optional[Dict[str, List[str]]] = None,
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

    `approved` (task -> protected files; only `ck worktree approve` passes it) lets exactly those files through
    for those tasks; they are recorded in the approved state as `by: "approval"`.
    """
    approved = {k: list(v) for k, v in (approved or {}).items()}
    into = into or current_branch(root)
    target = checkout_of(root, into)
    if target is None:
        raise WorktreeError(f"branch {into} is not checked out anywhere; check it out or create the integration worktree")
    priority = priority or {}
    order = sorted(dict.fromkeys(nums), key=lambda n: (-priority.get(n, 1), int(n) if n.isdigit() else 0))

    with land_lock(root, into):
        recovered = recover_target(root, target, into)
        started = _now()
        hook_notes: List[Dict] = []
        _ensure_hooks(root, target, hook_notes)
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
            touched = [f for f in touched if f not in approved.get(num, ())]
            if touched:
                candidates.remove(num)
                results[num] = Landed(
                    num,
                    "PROTECTED",
                    "changes files Claude Code, git or the shell run on their own; not landed, a human must "
                    "review and merge it (or list the paths in specs/ck.json \"land\": {\"allow_paths\": [...]}); "
                    "an agent stops here and reports it, and never merges it by another route",
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

        landed_paths = {n: protected_changes(root, base, task_branch(spec, n)) for n in batch}
        gates: List[Dict] = []

        def head() -> str:
            return git(target, "rev-parse", "HEAD").stdout.strip()

        def merge_all(group: List[str]) -> List[str]:
            merged = []
            for num in group:
                branch = task_branch(spec, num)
                proc = _git_merge(root, target, into, branch, f"Merge {branch} (task {num})")
                if proc.returncode != 0:
                    files = git(target, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
                    git(target, "merge", "--abort", check=False)
                    results[num] = Landed(num, "CONFLICT", f"merge into {into} conflicted (precheck missed it); aborted", branch, files)
                    continue
                merged.append(num)
            return merged

        def check(group: List[str]) -> Tuple[bool, str, List[Dict]]:
            t0 = datetime.datetime.now()
            ok, tail, failures = True, "", []
            if install:
                proc = subprocess.run(install, shell=True, cwd=target, capture_output=True, text=True)
                if proc.returncode != 0:
                    out = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-20:])
                    ok, tail = False, f"install step failed: `{install}`\n{out}"
                _ensure_hooks(root, target, hook_notes)  # an install step may move core.hooksPath (husky prepare)
            if ok:
                ok, tail, failures = gate(target, group)
            gates.append({"tasks": list(group), "green": ok, "seconds": int((datetime.datetime.now() - t0).total_seconds())})
            return ok, tail, failures

        def mark_merged(group: List[str], green: Optional[bool]) -> None:
            at = _now()
            for num in group:
                results[num] = Landed(num, "MERGED", f"into {into}", task_branch(spec, num), [], green, "", at)

        def land_group(group: List[str], known_red: bool = False, red_tail: str = "", red_failures: Optional[List[Dict]] = None) -> bool:
            """Land `group` on the current target head; True if all of it landed green."""
            red_failures = red_failures or []
            if not known_red:
                start = head()
                merged = merge_all(group)
                if not merged:
                    return False
                if gate is None:
                    mark_merged(merged, None)
                    return len(merged) == len(group)
                ok, tail, failures = check(merged)
                if ok:
                    mark_merged(merged, True)
                    return len(merged) == len(group)
                _git_reset(root, target, into, start)
                group, red_tail, red_failures = merged, tail, failures
            if len(group) == 1:
                num = group[0]
                results[num] = Landed(num, "RED", f"the gate on {into} fails with this task; it was not landed", task_branch(spec, num), [], False, red_tail, failures=red_failures)
                return False
            half = len(group) // 2
            left_green = land_group(group[:half])
            # A green left half on the same base proves the right half red: skip re-gating it whole.
            land_group(group[half:], known_red=left_green, red_tail=red_tail, red_failures=red_failures)
            return False

        if batch:
            land_group(batch)

        _record_landed(
            root, spec, into,
            {n: landed_paths[n] for n, o in results.items() if o.state == "MERGED" and n in landed_paths}, approved)

        fixture_error: Optional[str] = None
        try:
            fixture_error = _record_fixtures(
                root, target, spec, base, [n for n in order if n in results and results[n].state == "MERGED"], results)
        except (OSError, ValueError, subprocess.CalledProcessError, WorktreeError) as exc:
            fixture_error = f"fixture lock not recorded: {exc}"

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
            **({"recovered": recovered} if recovered else {}),
            **({"hooks": hook_notes[-1]} if hook_notes else {}),
            **({"fixtureError": fixture_error} if fixture_error else {}),
        }


SPEC_EDIT_LOCK_WAIT_S = 120.0


def _spec_dir_of(checkout: Path, spec: str) -> Optional[Path]:
    from claude_kiro.paths import LEGACY_SPECS_DIR, SPECS_DIR

    for base_dir in (SPECS_DIR, LEGACY_SPECS_DIR):
        d = checkout / base_dir / spec
        if (d / "tasks.md").is_file():
            return d
    return None


def _finding_keys(lint: Mapping) -> set:
    """Identities of the blocking findings of a lint result: cycles, verify cycles, verify scope, justifications."""
    import json

    keys = set()
    for name in ("cycles", "verifyCycles", "verifyScope", "justifications"):
        for entry in lint.get(name) or ():
            if isinstance(entry, Mapping):
                pick = {k: entry[k] for k in ("task", "kind", "file", "module", "suite", "testTask") if k in entry}
                keys.add(f"{name}:{json.dumps(pick or dict(entry), sort_keys=True, default=str)}")
            else:
                keys.add(f"{name}:{json.dumps(entry, sort_keys=True, default=str)}")
    return keys


def _program_of(command: str) -> str:
    import shlex

    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    at = next((i for i, w in enumerate(words) if Path(w).name == "pytest"), None)
    return " ".join(words[: at + 1]) if at is not None else (words[0] if words else "")


def _spec_facts(checkout: Path, spec: str) -> Dict:
    """What a landing compares before and after the merge: tasks.md text, lint findings, test selections per
    task, each task's verification fields, and the Done tasks."""
    from claude_kiro.hooks._shared.spec_parser import parse_tasks_text
    from claude_kiro.lint import _verify_runs, lint_spec
    from claude_kiro.lint_scope import _norm, _selection, load_sim_input

    spec_dir = _spec_dir_of(checkout, spec)
    if spec_dir is None:
        return {"text": "", "findings": set(), "selections": {}, "verification": {}, "done": set()}
    text = (spec_dir / "tasks.md").read_text(encoding="utf-8", errors="replace")
    config = load_config(checkout)
    findings = _finding_keys(lint_spec(spec_dir, checkout))
    sim = load_sim_input(spec_dir / "tasks.md", checkout, config)
    selections: Dict[str, List[Tuple[str, Optional[str], str]]] = {}
    for num, task in sim.tasks.items():
        chosen = []
        for command in task.verify:
            for path, selector in _verify_runs([command], config).items():
                path = _norm(path)
                tests = sim.tests.get(path, ())
                names = sorted(_selection({path: selector}, path, tests))
                program = _program_of(command)
                chosen.extend((path, name, program) for name in names) if names else chosen.append((path, None, program))
        selections[num] = chosen
    tasks = parse_tasks_text(text)
    return {
        "text": text, "findings": findings, "selections": selections,
        "verification": {t.num: (tuple(t.verify), tuple(t.verified_by), tuple(t.verifies)) for t in tasks},
        "done": {t.num for t in tasks if t.done},
    }


def land_spec_edit(
    root: Path,
    spec: str,
    num: str,
    into: Optional[str] = None,
    gate: Optional[GateFn] = None,
    wait_s: float = SPEC_EDIT_LOCK_WAIT_S,
) -> Landed:
    """Land one arbiter branch (`arbiter<k>`) on `into` under the merge-queue lock (8.1, 8.4-8.7, 1.7).

    Refuses a conflict (`git merge-tree`); takes lint findings and test selections on the target, merges, takes
    them again, and asks `decide_spec_edit` about the difference. Gates only the Done tasks whose verification
    the edit changed. The target goes back to its earlier commit on any refusal or red gate.
    """
    from claude_kiro import arbiter_policy as policy
    from claude_kiro.arbiter import _principles_at

    into = into or current_branch(root)
    branch = task_branch(spec, num)
    path = task_path(root, spec, num)
    target = checkout_of(root, into)
    if target is None:
        raise WorktreeError(f"branch {into} is not checked out anywhere; check it out or create the integration worktree")
    with land_lock(root, into, wait_s=wait_s):
        if not branch_exists(root, branch):
            return Landed(num, "MISSING", "no branch", branch)
        try:
            recovered = recover_target(root, target, into)
        except WorktreeError as exc:
            return Landed(num, "ERROR", str(exc), branch)
        start = git(target, "rev-parse", "HEAD").stdout.strip()
        if int(git(root, "rev-list", "--count", f"{start}..{branch}").stdout.strip() or 0) == 0:
            return Landed(num, "SKIP", f"no commits ahead of {into}", branch)

        ok, _, files = merge_tree(root, start, branch)
        if not ok:
            return Landed(num, "CONFLICT", f"conflicts with {into}; target untouched", branch, files)
        touched = git(root, "diff", "--name-only", "--no-renames", "-z", f"{start}...{branch}").stdout.split("\0")
        touched = [f for f in touched if f]
        lane = {f"specs/{spec}/{n}" for n in policy._LANE_FILES}
        outside = [f for f in touched if f not in lane]
        if outside:
            return Landed(num, "REFUSED", "; ".join(f"{f} is outside the spec-edit lane" for f in outside), branch, outside)

        before = _spec_facts(target, spec)
        effective = _principles_at(root, start, spec)
        proc = _git_merge(root, target, into, branch, f"Merge {branch} (task {num})")
        if proc.returncode != 0:
            conflicted = git(target, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
            git(target, "merge", "--abort", check=False)
            return Landed(num, "CONFLICT", f"merge into {into} conflicted (precheck missed it); aborted", branch, conflicted)

        def undo() -> None:
            _git_reset(root, target, into, start)

        try:
            after = _spec_facts(target, spec)
            changes = policy.spec_text_changes(before["text"], after["text"])
            changed = git(target, "diff", "--name-only", "--no-renames", start, "HEAD").stdout.split()
            facts = policy.SpecEditFacts(
                spec=spec, changed=tuple(changed), status_changes=changes.status_changes,
                mark_changes=changes.mark_changes,
                new_cycles=tuple(sorted(k for k in after["findings"] - before["findings"] if k.startswith("cycles:"))),
                new_blocking=tuple(sorted(k for k in after["findings"] - before["findings"] if not k.startswith("cycles:"))),
                reserved=tuple(effective.reserved),
            )
            decision = policy.decide_spec_edit(facts)
            if not decision.land:
                undo()
                return Landed(num, "REFUSED", "; ".join(decision.reasons), branch, list(changed))
            moved = [asdict(m) for m in policy.moved_out_of_verify(before["selections"], after["selections"])]
            scope = policy.gate_scope_of_edit(before["verification"], after["verification"], before["done"] & after["done"])
            if gate is not None and scope:
                green, tail, failures = gate(target, [str(n) for n in scope])
                if not green:
                    undo()
                    return Landed(num, "RED", f"the gate on {into} fails with this edit; it was not landed", branch,
                                  list(changed), False, tail, failures=failures)
        except BaseException:
            with contextlib.suppress(Exception):
                undo()
            raise
        commit = git(target, "rev-parse", "HEAD").stdout.strip()
        note = _cleanup_merged(root, target, path, branch)
        return Landed(num, "MERGED", f"into {into}{note}", branch, list(changed), True if gate is not None and scope else None,
                      "", _now(), commit=commit, moved=moved, recovered=recovered)


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
        outcomes.append(Outcome(num, "BUSY" if reason else "EXISTS", ", ".join(bits), rel_path(root, path), branch,
                                run=(claim_info(reason).run or None) if reason else None))
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
