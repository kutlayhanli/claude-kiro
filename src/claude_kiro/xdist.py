"""Parallel-test support for `ck gate`, `ck doctor` and `ck init` (requirements 6.x, 7.x).

`ck gate` sets `PYTEST_XDIST_AUTO_NUM_WORKERS = max(1, cores // gates running)` for its verify
commands unless the variable is already set. Gates running are counted through a per-user
registry of flock-held files: a live gate holds LOCK_EX on its `gate-<pid>-<8 hex>.lock`, and the
kernel drops that lock however the gate ends, so a lockable file belongs to an ended gate.

`ck doctor` / `ck init` show `suggestion(verify)`: a hint to run pytest under xdist, with a
one-line check that test ids are stable.
"""

import os
import re
import secrets
import shlex
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - not POSIX
    fcntl = None  # type: ignore[assignment]

KEY = "PYTEST_XDIST_AUTO_NUM_WORKERS"
REGISTRY_ENV = "CK_GATE_REGISTRY"  # override of the registry directory (tests)


# --- gate registry -----------------------------------------------------------------------------


def registry_dir(env: Mapping[str, str]) -> Path:
    """CK_GATE_REGISTRY, else Path.home() / ".cache" / "ck" / "gates" (a fixed per-user path).

    Never TMPDIR or tempfile.gettempdir(): sandboxed commands get their own writable TMPDIR while
    /tmp is read-only, and gettempdir() can fall back to the cwd (a task worktree)."""
    override = env.get(REGISTRY_ENV)
    if override:
        return Path(override)
    return Path.home() / ".cache" / "ck" / "gates"


def _is_gate_name(name: str) -> bool:
    return name.startswith("gate-") and name.endswith(".lock")


def _reason(exc: BaseException) -> str:
    return getattr(exc, "strerror", None) or str(exc) or type(exc).__name__


@contextmanager
def gate_slot(registry: Optional[Path] = None, errors: Optional[List[str]] = None) -> Iterator[Optional[Path]]:
    """Register this gate atomically: create <registry>/.reg-<pid>-<random>, take LOCK_EX on it,
    then os.rename it to <registry>/gate-<pid>-<random>.lock (the flock stays with the inode), and
    hold the lock until exit, when it unlinks its own file. A gate-*.lock name therefore never
    exists unlocked while its gate lives. Yields None (and registers nothing) if the directory
    can't be created (mode 0700) or the file can't be created, locked or renamed; the reason is
    appended to `errors` when given."""
    if registry is None:
        registry = registry_dir(os.environ)
    fd: Optional[int] = None
    final: Optional[Path] = None
    try:
        if fcntl is None:
            raise OSError("file locking is not available on this platform")
        registry.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not registry.is_dir():
            raise NotADirectoryError(20, "Not a directory")
        token = f"{os.getpid()}-{secrets.token_hex(4)}"
        pending = registry / f".reg-{token}"
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            target = registry / f"gate-{token}.lock"
            os.rename(pending, target)
            final = target
        except OSError:
            try:
                os.unlink(pending)
            except OSError:
                pass
            raise
    except OSError as exc:
        if fd is not None:
            os.close(fd)
            fd = None
        if errors is not None:
            errors.append(_reason(exc))
    try:
        yield final
    finally:
        if fd is not None:
            if final is not None:
                try:
                    os.unlink(final)  # still holding the lock: no counter can mistake it for ended
                except OSError:
                    pass
            os.close(fd)


def running_gates(registry: Optional[Path]) -> Optional[int]:
    """Count gate-*.lock files in the registry whose flock is held (LOCK_EX|LOCK_NB fails).

    A gate-*.lock file it can lock belongs to an ended gate: it unlinks it while still holding
    that LOCK_EX, then releases. Every other name (.reg-*, stray files, directories) is ignored
    and never removed. None if the directory can't be read or isn't a directory."""
    if registry is None or fcntl is None:
        return None
    try:
        entries = list(os.scandir(registry))
    except OSError:
        return None
    count = 0
    for entry in entries:
        if not _is_gate_name(entry.name):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        try:
            fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            continue  # vanished, or not ours to read: not a live gate we can see
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                count += 1
                continue
            except OSError:
                continue
            # Lockable: its gate ended. Remove it only if the name still points at this inode.
            try:
                held, now = os.fstat(fd), os.stat(entry.path, follow_symlinks=False)
                if (held.st_dev, held.st_ino) == (now.st_dev, now.st_ino):
                    os.unlink(entry.path)
            except OSError:
                pass
        finally:
            os.close(fd)
    return count


# --- worker cap --------------------------------------------------------------------------------


def core_count() -> Optional[int]:
    """len(os.sched_getaffinity(0)) where available, else os.cpu_count()."""
    if hasattr(os, "sched_getaffinity"):
        try:
            return len(os.sched_getaffinity(0))
        except OSError:
            pass
    return os.cpu_count()


def worker_cap(preset: Optional[str], cores: Optional[int], running: Optional[int]) -> Optional[str]:
    """The PYTEST_XDIST_AUTO_NUM_WORKERS value to set; None when a value is already present."""
    if preset is not None:
        return None
    c = cores if cores is not None and cores >= 1 else 1
    r = running if running is not None and running >= 1 else 1
    return str(max(1, c // r))


def verify_env(base: Mapping[str, str], registry: Optional[Path]) -> Dict[str, str]:
    """dict(base) plus PYTEST_XDIST_AUTO_NUM_WORKERS from worker_cap(base.get(...), core_count(), running_gates(registry))."""
    env = dict(base)
    value = worker_cap(base.get(KEY), core_count(), running_gates(registry))
    if value is not None:
        env[KEY] = value
    return env


# --- doctor / init hint --------------------------------------------------------------------------

SEPARATORS = {"&&", ";", "||", "|", "&"}
WRAPPERS = {"uv": "run", "poetry": "run", "pipenv": "run", "hatch": "run", "pdm": "run", "rye": "run"}
# `uv run` options that take a separate value (`--with x`); others are flags.
UV_VALUE_OPTIONS = {
    "--with", "--with-editable", "--with-requirements", "--python", "-p", "--extra", "--group",
    "--only-group", "--no-group", "--package", "--directory", "--project", "--env-file", "--index",
    "--default-index", "--index-url", "--extra-index-url", "--find-links", "-f", "--cache-dir",
    "--config-file", "--index-strategy", "--keyring-provider", "--resolution", "--prerelease",
    "--exclude-newer", "--link-mode", "--refresh-package", "--reinstall-package", "--upgrade-package",
    "-P", "--no-binary-package", "--no-build-package", "--color", "--python-platform", "-w",
}
ENV_VALUE_OPTIONS = {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON = re.compile(r"^python[0-9.]*$")
HINT = (
    'pytest runs serially in "verify": pytest-xdist can run it in parallel (add pytest-xdist as a dev '
    'dependency and "-n auto" to the command). Check first that test ids are stable: unstable ids make '
    'xdist fail with "Different tests were collected between gw0 and gw1". One-line check: '
    "PYTHONHASHSEED=1 {cmd} --collect-only -q | grep :: > /tmp/ids1 && "
    "PYTHONHASHSEED=2 {cmd} --collect-only -q | grep :: > /tmp/ids2 && diff /tmp/ids1 /tmp/ids2"
)


def _tokens(command: str) -> List[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        tokens: List[str] = []
        for word in command.split():
            core = word.rstrip(";")
            if core:
                tokens.append(core)
            if core != word:
                tokens.append(";")
        return tokens


def _segments(tokens: List[str]) -> List[List[str]]:
    segments: List[List[str]] = [[]]
    for token in tokens:
        if token in SEPARATORS or (token and set(token) <= set(";&|")):
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def _is_n_form(token: str) -> bool:
    return (token.startswith("-n") and not token.startswith("--")) or token == "--numprocesses" or token.startswith(
        "--numprocesses="
    )


def _is_pytest(token: str) -> bool:
    return token == "pytest" or token.endswith("/pytest")


def _segment_pytest(segment: List[str]) -> Optional[bool]:
    """None if the segment does not invoke pytest; else True if it runs pytest in parallel."""
    i, n = 0, len(segment)
    parallel_env = False
    # Leading assignments (PYTEST_ADDOPTS with an -n form counts as parallel).
    while i < n and _ASSIGN.match(segment[i]):
        name, value = segment[i].split("=", 1)
        if name == "PYTEST_ADDOPTS":
            try:
                words = shlex.split(value)
            except ValueError:
                words = value.split()
            if any(_is_n_form(w) for w in words):
                parallel_env = True
        i += 1
    # Wrappers, possibly nested (`env X=1 uv run python -m pytest`).
    while i < n:
        word = segment[i]
        base = word.rsplit("/", 1)[-1]
        if base in WRAPPERS and i + 1 < n and segment[i + 1] == WRAPPERS[base]:
            i += 2
            while i < n and segment[i].startswith("-") and not _is_pytest(segment[i]):
                option = segment[i]
                i += 2 if (base == "uv" and option in UV_VALUE_OPTIONS) else 1
            continue
        if base == "env":
            i += 1
            while i < n and (segment[i].startswith("-") or _ASSIGN.match(segment[i])):
                if segment[i] in ENV_VALUE_OPTIONS:
                    i += 1
                i += 1
            continue
        if _PYTHON.match(base):
            i += 1
            while i < n and segment[i].startswith("-") and segment[i] != "-m":
                i += 1
            if i + 1 < n and segment[i] == "-m" and segment[i + 1] == "pytest":
                rest = segment[i + 2:]
                return parallel_env or any(_is_n_form(w) for w in rest)
            return None
        break
    if i < n and _is_pytest(segment[i]):
        return parallel_env or any(_is_n_form(w) for w in segment[i + 1:])
    return None


def has_pytest_without_n(verify: Sequence[str]) -> Optional[str]:
    """The first verify command that invokes pytest without an -n / --numprocesses option, or None."""
    if not isinstance(verify, (list, tuple)):
        return None
    for command in verify:
        if not isinstance(command, str):
            continue
        for segment in _segments(_tokens(command)):
            if _segment_pytest(segment) is False:
                return command
    return None


def suggestion(verify: Sequence[str]) -> Optional[str]:
    """The doctor/init hint text, or None (see has_pytest_without_n)."""
    command = has_pytest_without_n(verify)
    if command is None:
        return None
    match = re.match(r"^(.*?\bpytest)\b", command)  # the pytest prefix, as config.collect_command derives it
    prefix = match.group(1).strip() if match else "pytest"
    return HINT.format(cmd=prefix)
