"""Shared Python harness for the unattended-correctness spec (test-plan "To add" items 1 and 2).

Every helper drives real git or the real ck CLI (`python -m claude_kiro.cli` in a subprocess). Nothing here mocks
a `claude_kiro` module, git, the filesystem or a test runner. Importing this module never imports project code that
may not exist yet: project imports happen inside the helpers that need them.

Helpers
-------
- `ck_json`, `ck_text`: run ck, return `(code, obj, stderr)` / `(code, stdout, stderr)`.
- `attr_project`, `pytest_body`, `node_test_file`, `counter_lines`: the attribution scenario repo.
- `hooks_repo`, `ref`, `raw`, `snapshot_ck_state`, `restore_ck_state`, `readonly_git_metadata`.
- `sandboxed`: real `bwrap`, binds derived from `sandbox_settings`.
- `progress_run`: a real run record through `ck progress begin/end` and `CK_NOW`.
- `spec_graph_project`, `fake_claude`, `equity_sized_repo`.
"""

import contextlib
import json
import os
import shlex
import stat
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

FIXTURES = Path(__file__).parent / "fixtures" / "unattended"

CK_ARGV = [sys.executable, "-m", "claude_kiro.cli"]

# --------------------------------------------------------------------------- git and ck


def git(cwd: Path, *args: str) -> str:
    """Plain git that raises on failure (setup code)."""
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def raw(cwd: Path, *git_args: str) -> subprocess.CompletedProcess:
    """Plain git, never through ck, never raising: the caller reads returncode, stdout and stderr."""
    return subprocess.run(["git", *git_args], cwd=cwd, capture_output=True, text=True)


def ref(root: Path, name: str) -> str:
    """The commit a ref points at (full sha), or '' when the ref does not exist."""
    out = raw(root, "rev-parse", "--verify", "-q", f"{name}^{{commit}}")
    return out.stdout.strip() if out.returncode == 0 else ""


def _ck_env(cwd: Path, env: Optional[Mapping[str, str]]) -> Dict[str, str]:
    merged = dict(os.environ)
    # Generated tests append to $CK_TEST_COUNTERS/<name>; default it next to the project.
    merged.setdefault("CK_TEST_COUNTERS", str(Path(cwd).resolve().parent / "counters"))
    merged.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    # Verify commands say `python`: make it this interpreter, as `uv run` does.
    merged["PATH"] = str(Path(sys.executable).parent) + os.pathsep + merged.get("PATH", "")
    if env:
        merged.update(env)
    return merged


def ck_run(cwd: Path, *args: str, env: Optional[Mapping[str, str]] = None, input: Optional[str] = None,
           timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run([*CK_ARGV, *args], cwd=cwd, env=_ck_env(cwd, env), input=input, capture_output=True,
                          text=True, timeout=timeout)


def ck_json(cwd: Path, *args: str, env: Optional[Mapping[str, str]] = None,
            input: Optional[str] = None) -> Tuple[int, Any, str]:
    """Run `python -m claude_kiro.cli ARGS` in `cwd`: (exit code, stdout parsed as JSON or None, stderr)."""
    out = ck_run(cwd, *args, env=env, input=input)
    try:
        obj = json.loads(out.stdout)
    except ValueError:
        obj = None
    return out.returncode, obj, out.stderr


def ck_text(cwd: Path, *args: str, env: Optional[Mapping[str, str]] = None,
            input: Optional[str] = None) -> Tuple[int, str, str]:
    out = ck_run(cwd, *args, env=env, input=input)
    return out.returncode, out.stdout, out.stderr


def init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    return root


def commit_all(root: Path, message: str) -> None:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)


# --------------------------------------------------------------------------- attribution project

OUTCOMES = ("pass", "fail", "flaky", "import-error", "syntax-error", "setup-error")
_MB = "é世😀"  # multibyte filler for assert-msg:<n>

_TEST_HEADER = '''\
import os
import time
from pathlib import Path

import pytest


def _count(name):
    """One line per run, so a test can count how many times a node ran."""
    base = Path(os.environ.get("CK_TEST_COUNTERS") or Path(__file__).resolve().parents[2] / "counters")
    base.mkdir(parents=True, exist_ok=True)
    path = base / name
    runs = path.read_text().count("\\n") if path.exists() else 0
    with path.open("a") as fh:
        fh.write("run\\n")
    return runs


'''


def pytest_body(name: str, outcome: str) -> str:
    """The pytest source of one test named `name` with the given outcome (no file header)."""
    fn = name if name.startswith("test_") else f"test_{name}"
    key = fn
    if outcome == "pass":
        return f"def {fn}():\n    _count({key!r})\n    assert True\n"
    if outcome == "fail":
        return f"def {fn}():\n    _count({key!r})\n    assert 1 == 2\n"
    if outcome == "flaky":
        return f"def {fn}():\n    runs = _count({key!r})\n    assert runs >= 1, 'first run fails'\n"
    if outcome == "import-error":
        return f"def {fn}():\n    _count({key!r})\n    assert True\n"  # the file-level import is added by the writer
    if outcome == "syntax-error":
        return f"def {fn}(:\n    pass\n"
    if outcome == "setup-error":
        return (f"@pytest.fixture\ndef boom_{fn}():\n    raise RuntimeError('setup boom')\n\n\n"
                f"def {fn}(boom_{fn}):\n    _count({key!r})\n    assert True\n")
    if outcome.startswith("sleep:"):
        seconds = float(outcome.split(":", 1)[1])
        return f"def {fn}():\n    _count({key!r})\n    time.sleep({seconds!r})\n"
    if outcome.startswith("assert-msg:"):
        n = int(outcome.split(":", 1)[1])
        msg = (_MB * (n // len(_MB) + 1))[:n]
        return f"def {fn}():\n    _count({key!r})\n    assert False, {msg!r}\n"
    raise ValueError(f"unknown outcome {outcome!r}")


def node_test_file(failing: bool) -> str:
    """A node:test file with one test, failing or passing."""
    check = "assert.equal(1, 2)" if failing else "assert.equal(1, 1)"
    return ("import test from 'node:test';\nimport assert from 'node:assert/strict';\n\n"
            f"test('node check', () => {{\n  {check};\n}});\n")


def _tc_number(name: str) -> Optional[int]:
    import re

    m = re.search(r"tc0*(\d+)_", name, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _target_file(name: str, outcome: str) -> str:
    if "::" in name:
        return name.split("::", 1)[0]
    if outcome in ("import-error", "syntax-error"):
        return f"tests/test_{name.removeprefix('test_')}_file.py"  # a file-level failure gets its own file
    n = _tc_number(name)
    return "tests/test_more.py" if n is not None and n >= 4 else "tests/test_calc_tc.py"


def write_test_files(root: Path, tests: Mapping[str, str]) -> List[str]:
    files: Dict[str, List[str]] = {}
    prefix: Dict[str, str] = {}
    for key, outcome in tests.items():
        name = key.split("::", 1)[1] if "::" in key else key
        target = _target_file(key, outcome)
        files.setdefault(target, []).append(pytest_body(name, outcome))
        if outcome == "import-error":
            prefix[target] = "import module_that_does_not_exist_anywhere  # noqa: F401\n\n\n"
    for target, bodies in files.items():
        path = root / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_TEST_HEADER + prefix.get(target, "") + "\n\n".join(bodies))
    return sorted(files)


def counter_lines(root: Path, name: str, counters: Optional[Path] = None) -> int:
    """How many times the generated test `name` ran (lines in its counter file)."""
    base = counters or (Path(root).resolve().parent / "counters")
    fn = name if name.startswith("test_") else f"test_{name}"
    path = base / fn
    return path.read_text().count("\n") if path.exists() else 0


_DEMO_REQUIREMENTS = """# Feature: Demo

- 1.1 WHEN a is added to b THE SYSTEM SHALL return the sum
- 2.1 WHEN a is subtracted from b THE SYSTEM SHALL return the difference
"""

_DEMO_TEST_PLAN = """# Test Plan: Demo

| ID | Criteria | Level | Scenario | Real boundary | File |
|----|----------|-------|----------|---------------|------|
| TC-1 | 1.1 | int | add | python | tests/test_calc_tc.py |
| TC-2 | 1.1 | int | add negative | python | tests/test_calc_tc.py |
| TC-3 | 2.1 | int | subtract | python | tests/test_calc_tc.py |
| TC-5 | 2.1 | int | more | python | tests/test_more.py |
"""

_DEMO_DESIGN = "# Design: Demo\n\n## Components\n\n- `calc.py`: add and subtract.\n"

_TASKS_HEAD = "# Implementation Tasks: Demo\n\n**Status:** In Progress\n\n## Task Breakdown\n\n"


def attr_tasks_md(statuses: Optional[Mapping[str, str]] = None, extra_tasks: str = "") -> str:
    """The `attr_project` tasks.md: test tasks 1 and 6, impl tasks 2, 3, 7 and 8."""
    st = {str(k): v for k, v in (statuses or {}).items()}

    def status(n: str) -> str:
        return st.get(n, "Not Started")

    def box(n: str) -> str:
        return "x" if status(n) == "Done" else " "

    return _TASKS_HEAD + f"""\
### Task 1: Calc tests
**Status:** {status('1')}
**Track:** test
**Requirements:** 1.1, 2.1
**Files:**
- `tests/test_calc_tc.py` - calc behaviour
**Verify:** `python -m pytest -q tests/test_calc_tc.py`
**Verifies:** Task 2, Task 3
**Dependencies:** None

**Acceptance:**
- [{box('1')}] Tests cover 1.1 and 2.1

---

### Task 2: Implement add
**Status:** {status('2')}
**Track:** impl
**Requirements:** 1.1
**Files:**
- `calc.py` - add
**Verify:** `python -m pytest -q tests/test_calc_tc.py -k "tc1_ or tc2_"`
**Verified by:** Task 1
**Dependencies:** Task 1

**Acceptance:**
- [{box('2')}] add works

---

### Task 3: Implement subtract
**Status:** {status('3')}
**Track:** impl
**Requirements:** 2.1
**Files:**
- `calc.py` - subtract
**Verify:** `python -m pytest -q tests/test_calc_tc.py -k "tc3_"`
**Verified by:** Task 1
**Dependencies:** Task 1

**Acceptance:**
- [{box('3')}] subtract works

---

### Task 6: More tests
**Status:** {status('6')}
**Track:** test
**Requirements:** 2.1
**Files:**
- `tests/test_more.py` - more behaviour
**Verify:** `python -m pytest -q tests/test_more.py -k "tc5_"`
**Verifies:** Task 7, Task 8
**Dependencies:** None

**Acceptance:**
- [{box('6')}] Tests written

---

### Task 7: Implement seven
**Status:** {status('7')}
**Track:** impl
**Requirements:** 2.1
**Files:**
- `seven.py` - seven
**Verified by:** Task 6
**Dependencies:** Task 6

**Acceptance:**
- [{box('7')}] works

---

### Task 8: Implement eight
**Status:** {status('8')}
**Track:** impl
**Requirements:** 2.1
**Files:**
- `eight.py` - eight
**Verified by:** Task 6
**Dependencies:** Task 6

**Acceptance:**
- [{box('8')}] works

---

""" + extra_tasks


def attr_project(tmp_path: Path, *, statuses: Optional[Mapping[str, str]] = None,
                 tests: Optional[Mapping[str, str]] = None, extra_tasks: str = "",
                 config: Optional[Mapping[str, Any]] = None) -> Path:
    """A git repo on `main` (then `integrate/demo` from `ck worktree integration demo`) with the demo spec.

    `statuses` maps a task number to its status (default Not Started). `tests` maps a generated test name to an
    outcome (see OUTCOMES, plus `sleep:<s>` and `assert-msg:<n>`); a key `path::name` picks the file. Returns the
    project root (the `main` checkout). Counter files go to `<tmp_path>/counters` (or `$CK_TEST_COUNTERS`).
    """
    root = init_repo(tmp_path / "proj")
    (tmp_path / "counters").mkdir(exist_ok=True)
    (root / ".gitignore").write_text(".venv/\n.ck/worktrees/\n__pycache__/\n.pytest_cache/\n")
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n")
    (root / "conftest.py").write_text("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n")
    spec = root / "specs" / "demo"
    spec.mkdir(parents=True)
    (spec / "requirements.md").write_text(_DEMO_REQUIREMENTS)
    (spec / "test-plan.md").write_text(_DEMO_TEST_PLAN)
    (spec / "design.md").write_text(_DEMO_DESIGN)
    (spec / "tasks.md").write_text(attr_tasks_md(statuses, extra_tasks))
    cfg: Dict[str, Any] = {"verify": ["python -m pytest -q -p no:cacheprovider tests"]}
    cfg.update(config or {})
    (root / "specs" / "ck.json").write_text(json.dumps(cfg, indent=2) + "\n")
    (root / "tests").mkdir()
    write_test_files(root, tests or {})
    commit_all(root, "base")
    code, _, err = ck_text(root, "worktree", "integration", "demo")
    if code != 0:
        raise RuntimeError(f"ck worktree integration demo failed ({code}): {err}")
    return root


# --------------------------------------------------------------------------- hooks repo


def _commit_in(path: Path, files: Mapping[str, Optional[str]], message: str) -> None:
    for rel, content in files.items():
        target = path / rel
        if content is None:
            git(path, "rm", "-q", rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    commit_all(path, message)


def hooks_repo(tmp_path: Path) -> Path:
    """A repo with the integration worktree (`ck worktree integration demo`) and five task branches.

    - `feat/demo-task-1` adds `.claude/CLAUDE.md`; task 2 adds `.claude/commands/x.md`; task 3 changes only
      `src/a.py`; task 4 adds `.claude/commands/ä.md`; task 5 deletes `.claude/settings.json` (seeded on main).
    Each branch has its own task worktree (`ck worktree create demo N`). Returns the project root.
    """
    root = init_repo(tmp_path / "repo")
    (root / ".gitignore").write_text(".venv/\n.ck/worktrees/\n")
    (root / "shared.txt").write_text("base\n")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("A = 1\n")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text('{"permissions": {}}\n')
    spec = root / "specs" / "demo"
    spec.mkdir(parents=True)
    tasks = "# Implementation Tasks: Demo\n\n## Task Breakdown\n\n" + "".join(
        f"### Task {n}: task {n}\n**Status:** Not Started\n**Track:** impl\n**Dependencies:** None\n\n" for n in range(1, 6))
    (spec / "tasks.md").write_text(tasks)
    commit_all(root, "base")
    code, _, err = ck_text(root, "worktree", "integration", "demo")
    if code != 0:
        raise RuntimeError(f"ck worktree integration demo failed ({code}): {err}")
    code, _, err = ck_text(root, "worktree", "create", "demo", "1", "2", "3", "4", "5", "--base", "integrate/demo")
    if code != 0:
        raise RuntimeError(f"ck worktree create failed ({code}): {err}")
    changes: Dict[str, Dict[str, Optional[str]]] = {
        "1": {".claude/CLAUDE.md": "# changed by task 1\n"},
        "2": {".claude/commands/x.md": "x\n"},
        "3": {"src/a.py": "A = 2\n"},
        "4": {".claude/commands/ä.md": "umlaut\n"},
        "5": {".claude/settings.json": None},
    }
    for n, files in changes.items():
        _commit_in(task_worktree(root, n), files, f"task {n}")
    return root


def task_worktree(root: Path, n: Union[int, str], spec: str = "demo") -> Path:
    return root / ".ck" / "worktrees" / f"{spec}-task-{n}"


def integration_worktree(root: Path, spec: str = "demo") -> Path:
    """The worktree where `integrate/<spec>` is checked out (from `git worktree list`)."""
    out = git(root, "worktree", "list", "--porcelain")
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line.split(" ", 1)[1]
        elif line == f"branch refs/heads/integrate/{spec}" and path:
            return Path(path)
    raise RuntimeError(f"no worktree has integrate/{spec} checked out")


class Snapshot(dict):
    """path -> bytes, plus the permission bits (`modes`) so a restore keeps executable hooks executable."""

    def __init__(self) -> None:
        super().__init__()
        self.modes: Dict[str, int] = {}


def _state_roots(root: Path) -> List[Path]:
    from claude_kiro.analytics import store

    common = Path(raw(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    roots = [common, store.data_dir(), root / ".ck" / "worktrees"]
    seen: List[Path] = []
    for p in roots:
        if p not in seen:
            seen.append(p)
    return seen


def snapshot_ck_state(root: Path) -> Snapshot:
    """Every regular file under the git common dir, the ck data dir and the worktree dir: path -> bytes."""
    snap = Snapshot()
    for base in _state_roots(root):
        if not base.exists():
            continue
        for dirpath, _dirs, names in os.walk(base):
            for name in names:
                path = Path(dirpath) / name
                if path.is_symlink() or not path.is_file():
                    continue
                key = str(path)
                try:
                    snap[key] = path.read_bytes()
                    snap.modes[key] = stat.S_IMODE(path.stat().st_mode)
                except OSError:
                    continue
    return snap


def restore_ck_state(root: Path, snap: Snapshot) -> None:
    """Put the snapshotted files back: rewrite changed ones, recreate deleted ones, remove ones added since."""
    current = snapshot_ck_state(root)
    for key in set(current) - set(snap):
        try:
            Path(key).unlink()
        except OSError:
            pass
    for key, data in snap.items():
        path = Path(key)
        if key in current and current[key] == data:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        mode = getattr(snap, "modes", {}).get(key)
        if mode is not None:
            path.chmod(mode)


@contextlib.contextmanager
def readonly_git_metadata(root: Path) -> Iterator[None]:
    """`chmod a-w` on `.git/worktrees/*`, the hooks location and `.git/config`; restored afterwards."""
    common = Path(raw(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    hooks = Path(raw(root, "rev-parse", "--path-format=absolute", "--git-path", "hooks").stdout.strip())
    targets = [common / "config", hooks]
    wts = common / "worktrees"
    if wts.is_dir():
        targets += sorted(p for p in wts.iterdir())
    saved: List[Tuple[Path, int]] = []
    for path in targets:
        if path.exists():
            mode = stat.S_IMODE(path.stat().st_mode)
            saved.append((path, mode))
            path.chmod(mode & ~0o222)
    try:
        yield
    finally:
        for path, mode in reversed(saved):
            with contextlib.suppress(OSError):
                path.chmod(mode)


# --------------------------------------------------------------------------- bwrap


def _bwrap_probe() -> Optional[str]:
    """None when bwrap starts here, else the reason (the bwrap error text)."""
    try:
        out = subprocess.run(["bwrap", "--ro-bind", "/", "/", "true"], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return "bwrap is not installed"
    except subprocess.TimeoutExpired:
        return "bwrap --ro-bind / / true timed out"
    if out.returncode != 0:
        return (out.stderr or out.stdout).strip() or f"bwrap exited {out.returncode}"
    return None


def _home_path(entry: str) -> Path:
    return Path(entry.replace("~", str(Path.home()), 1)) if entry.startswith("~") else Path(entry)


def bwrap_argv(argv: Sequence[str], project: Path, worktree_dir: Path) -> List[str]:
    """The bwrap command line for `argv`, with binds derived from `sandbox_settings(...)["sandbox"]["filesystem"]`."""
    from claude_kiro.cli.run import sandbox_settings

    fs = sandbox_settings(project, [], worktree_dir)["sandbox"]["filesystem"]
    home = Path.home()
    cmd = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--die-with-parent"]
    # `~` is hidden (denyRead) except what the settings re-open.
    if any(_home_path(p) == home for p in fs["denyRead"]):
        cmd += ["--tmpfs", str(home)]
    for entry in fs["allowRead"]:
        path = _home_path(entry)
        if path.exists():
            cmd += ["--ro-bind", str(path), str(path)]
    writable = [project.resolve(), worktree_dir.resolve(), *(_home_path(p) for p in fs["allowWrite"])]
    for path in writable:
        if path.exists():
            cmd += ["--bind", str(path), str(path)]
    # The ck data dir stays hidden even where it is not under `~`.
    from claude_kiro.analytics import store

    data = store.data_dir()
    if data.exists() and not str(data).startswith(str(home)):
        cmd += ["--tmpfs", str(data)]
    # Git metadata is read-only, as observed in October.
    common = Path(raw(project, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    for sub in ("hooks", "config", "worktrees"):
        target = common / sub
        if target.exists():
            cmd += ["--ro-bind", str(target), str(target)]
    return [*cmd, "--chdir", str(project), "--", *argv]


def sandboxed(argv: Sequence[str], project: Path, worktree_dir: Path,
              env: Optional[Mapping[str, str]] = None) -> subprocess.CompletedProcess:
    """Run argv under real bwrap. Skips (with bwrap's error) only when `bwrap --ro-bind / / true` fails."""
    import pytest

    reason = _bwrap_probe()
    if reason is not None:
        pytest.skip(f"bwrap unavailable: {reason}")
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(bwrap_argv(argv, project, worktree_dir), capture_output=True, text=True, env=merged,
                          timeout=300)


# --------------------------------------------------------------------------- progress runs


def progress_run(spec: str, key: str, *, ended: bool, last_event_age_min: float,
                 cwd: Optional[Path] = None, env: Optional[Mapping[str, str]] = None) -> str:
    """A real run record: `ck progress begin` (and `end` when `ended`) with `CK_NOW` set `last_event_age_min`
    minutes in the past, so the run's last event is that old. `key` labels the run for the caller; ck chooses the
    real run key, which is returned (pass it to `--progress-run`)."""
    cwd = Path(cwd) if cwd else Path.cwd()
    then = datetime.now(timezone.utc) - timedelta(minutes=last_event_age_min)
    pinned = {"CK_NOW": then.strftime("%Y-%m-%dT%H:%M:%SZ"), **(env or {})}
    code, obj, err = ck_json(cwd, "progress", "begin", spec, "--kind", "implement", "--json", env=pinned)
    run = (obj or {}).get("run") if isinstance(obj, dict) else None
    if code != 0 or not run:
        raise RuntimeError(f"ck progress begin failed for {key!r}: code={code} out={obj!r} err={err}")
    if ended:
        code, obj, err = ck_json(cwd, "progress", "end", spec, "--run", run, "--outcome", "completed", "--json", env=pinned)
        if code != 0:
            raise RuntimeError(f"ck progress end failed for {key!r}: code={code} out={obj!r} err={err}")
    return run


# --------------------------------------------------------------------------- spec graph project

CLI_TABLE_HEADER = "| Command | File |\n|---------|------|\n"

MTR_CLI = '''\
import click


@click.group()
@click.option("--verbose", is_flag=True)
def cli(verbose):
    """mtr: a click CLI."""


@cli.command()
def version():
    """Print the version."""
    click.echo("mtr 1.0")


@cli.group()
def thesis():
    """thesis commands"""


@thesis.command("new")
@click.option("--name", default="x")
def thesis_new(name):
    click.echo(f"thesis new {name}")


def main(argv=None):
    cli(args=argv, standalone_mode=False)



if __name__ == "__main__":
    cli()
'''

MTR2_CLI = '''\
import argparse


def build():
    parser = argparse.ArgumentParser(prog="mtr2")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("version")
    sub.add_parser("report")
    return parser


def main(argv=None):
    args = build().parse_args(argv)
    if args.cmd == "version":
        print("mtr2 1.0")
    elif args.cmd == "report":
        print("mtr2 report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

# Invocation forms of P-20 (`{cmd}` is the command words, e.g. `thesis new`) and its near-misses.
INVOCATION_FORMS = (
    'run_cli({argv})', 'main({argv})', 'CliRunner().invoke(cli, {argv})', 'subprocess.run(["mtr", {words}])',
    'subprocess.run("mtr {cmd} --x 1", shell=True)', 'shlex.split("mtr {cmd}")', '["--verbose", {words}]',
)
NEAR_MISS_FORMS = (
    'run_cli(["thesis", "newer"])', 'run_cli(["thesis-new"])', 'run_cli(["express", "--name", "thesis"])',
    'assert ok, "thesis new failed"', '# mtr thesis new', 'subprocess.run(["git", "thesis", "new"])',
)


def invocation_forms(cmd: str) -> List[str]:
    """P-20's invocation forms for the command `cmd` (a space-separated command path)."""
    words = cmd.split()
    argv = json.dumps(words)
    return [f.format(argv=argv, words=", ".join(json.dumps(w) for w in words), cmd=cmd) for f in INVOCATION_FORMS]


def spec_graph_project(tmp_path: Path, *, tasks: Union[str, Sequence[Mapping[str, Any]]] = (),
                       design_cli: Union[Mapping[str, str], Sequence[Tuple[str, str]]] = (),
                       tests: Optional[Mapping[str, str]] = None,
                       sources: Optional[Mapping[str, str]] = None) -> Path:
    """A project for `ck graph` / `ck lint`: design.md with a `| Command | File |` table, tasks.md built with
    `tests/test_lint.py::block`, the given test and source files, a click CLI `src/mtr/cli.py` (`mtr version`
    defined) and an argparse CLI `src/mtr2/cli.py`. `tasks` is tasks.md block text, or a list of `block(**kw)` kwargs.
    Returns the project root; the spec is `specs/sim`."""
    from test_lint import block  # imported here: it is a test module

    root = init_repo(tmp_path / "graph")
    spec = root / "specs" / "sim"
    spec.mkdir(parents=True)
    rows = design_cli.items() if isinstance(design_cli, Mapping) else design_cli
    table = CLI_TABLE_HEADER + "".join(f"| `{c}` | `{f}` |\n" for c, f in rows)
    (spec / "design.md").write_text(f"# Design: Sim\n\n## CLI\n\n{table}")
    (spec / "requirements.md").write_text("# Feature: Sim\n\n- 1.1 WHEN a THE SYSTEM SHALL b\n")
    (spec / "test-plan.md").write_text("# Test Plan: Sim\n")
    body = tasks if isinstance(tasks, str) else "".join(block(**dict(kw)) for kw in tasks)
    (spec / "tasks.md").write_text("# Implementation Tasks: Sim\n\n## Task Breakdown\n\n" + body)
    files: Dict[str, str] = {"src/mtr/__init__.py": "", "src/mtr/cli.py": MTR_CLI, "src/mtr2/__init__.py": "",
                             "src/mtr2/cli.py": MTR2_CLI}
    files.update(sources or {})
    files.update(tests or {})
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    commit_all(root, "base")
    return root


# --------------------------------------------------------------------------- fake claude


def fake_claude(tmp_path: Path) -> Path:
    """A `claude` executable (put its directory first on PATH). At launch it records argv, the `--settings` JSON
    and `git config core.hooksPath` into `<dir>/record.json`, then exits 0. Returns the directory."""
    bin_dir = tmp_path / "fake-claude-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    record = bin_dir / "record.json"
    script.write_text(textwrap.dedent(f'''\
        #!{sys.executable}
        import json, subprocess, sys

        argv = sys.argv[1:]
        settings = None
        for i, a in enumerate(argv):
            if a == "--settings" and i + 1 < len(argv):
                raw = argv[i + 1]
                try:
                    settings = json.loads(raw)
                except ValueError:
                    try:
                        settings = json.load(open(raw))
                    except (OSError, ValueError):
                        settings = None
        hp = subprocess.run(["git", "config", "core.hooksPath"], capture_output=True, text=True)
        with open({str(record)!r}, "a") as fh:
            fh.write(json.dumps({{"argv": argv, "settings": settings, "hooksPath": hp.stdout.strip() or None,
                                  "cwd": __import__("os").getcwd()}}) + "\\n")
        sys.exit(0)
    '''))
    script.chmod(0o755)
    return bin_dir


def fake_claude_records(bin_dir: Path) -> List[dict]:
    path = bin_dir / "record.json"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


# --------------------------------------------------------------------------- equity-sized repo


def equity_sized_repo(tmp_path: Path, files: int = 3000, commits: int = 500) -> Path:
    """A repo with `files` tracked files and `commits` commits, built with one `git fast-import` stream."""
    root = init_repo(tmp_path / "equity")
    per_commit = max(1, files // commits)
    lines: List[str] = []
    mark = 0
    written = 0
    for c in range(commits):
        lines.append("commit refs/heads/main")
        lines.append(f"committer Test <t@example.com> {1_700_000_000 + c} +0000")
        msg = f"commit {c}"
        lines.append(f"data {len(msg)}")
        lines.append(msg)
        count = per_commit if c < commits - 1 else max(files - written, 0)
        for _ in range(count):
            body = f"file {written}"
            lines.append(f"M 100644 inline src/pkg{written % 50}/mod{written}.py")
            lines.append(f"data {len(body)}")
            lines.append(body)
            written += 1
            mark += 1
        lines.append("")
    stream = "\n".join(lines) + "\n"
    subprocess.run(["git", "fast-import", "--quiet"], cwd=root, input=stream, text=True, check=True,
                   capture_output=True)
    git(root, "reset", "-q", "--hard", "main")
    return root


def quote(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


# --------------------------------------------------------------------------- captured baselines

ALL_DONE = {n: "Done" for n in ("1", "2", "3", "6", "7", "8")}

# `ck gate demo` scenarios whose text report is captured in fixtures/unattended/gate-text-baseline/<name>.txt (TC-13).
GATE_BASELINE_SCENARIOS: Dict[str, Dict[str, Any]] = {
    "green-all-done": {"statuses": ALL_DONE, "tests": {"test_tc1_a": "pass", "test_tc2_b": "pass", "test_tc3_c": "pass",
                                                       "test_tc5_d": "pass"}, "args": []},
    "regression-done-tasks": {"statuses": ALL_DONE, "tests": {"test_tc1_a": "pass", "test_tc2_b": "fail",
                                                              "test_tc3_c": "pass", "test_tc5_d": "pass"}, "args": []},
    "excused-unfinished": {"statuses": {"1": "Done"}, "tests": {"test_tc1_a": "pass", "test_tc2_b": "fail",
                                                                "test_tc3_c": "fail", "test_tc5_d": "pass"}, "args": []},
    "collection-error": {"statuses": ALL_DONE, "tests": {"test_tc1_a": "pass", "broken": "import-error"}, "args": []},
    "setup-error-task-scoped": {"statuses": ALL_DONE, "tests": {"test_tc1_a": "pass", "test_tc3_c": "setup-error"},
                                "args": ["--task", "3"]},
}


def normalize_gate_text(text: str, root: Path) -> str:
    """Strip what differs between runs: the project path, timings and the temp directory."""
    import re

    text = text.replace(str(root.resolve()), "<root>").replace(str(root), "<root>")
    text = re.sub(r"\b\d+(\.\d+)? ?s\b", "<t>s", text)
    text = re.sub(r"in <t>s", "in <t>s", text)
    return text


def capture_gate_text(tmp_path: Path, name: str) -> str:
    """Run `ck gate demo` for the scenario `name`: the normalised text plus a first line `exit: <code>`."""
    sc = GATE_BASELINE_SCENARIOS[name]
    root = attr_project(tmp_path, statuses=sc["statuses"], tests=sc["tests"])
    code, out, err = ck_text(root, "gate", "demo", *sc["args"])
    return f"exit: {code}\n" + normalize_gate_text(out, root)


def json_key_paths(obj: Any, prefix: str = "") -> List[str]:
    """Every key path of a JSON value: dict keys joined with `.`, list items as `[]`."""
    paths: List[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            paths.append(p)
            paths.extend(json_key_paths(v, p))
    elif isinstance(obj, list):
        for v in obj:
            paths.extend(json_key_paths(v, f"{prefix}[]"))
    return sorted(set(paths))


_LAND_TASKS = """# Implementation Tasks: Demo

### Task 1: one
**Status:** Not Started
**Verify:** `test -f one.txt`

### Task 2: two
**Status:** Not Started
**Verify:** `test ! -f bad.txt`

### Task 3: three
**Status:** Not Started
**Verify:** `test -f three.txt`
"""


def land_project(tmp_path: Path) -> Path:
    """A repo with task worktrees 1, 2 and 3 for `ck worktree land` (the test_worktree_land shape)."""
    root = init_repo(tmp_path / "landrepo")
    (root / ".gitignore").write_text(".venv/\n.ck/worktrees/\n")
    (root / "shared.txt").write_text("base\n")
    (root / "specs" / "demo").mkdir(parents=True)
    (root / "specs" / "demo" / "tasks.md").write_text(_LAND_TASKS)
    commit_all(root, "base")
    code, _, err = ck_text(root, "worktree", "create", "demo", "1", "2", "3")
    if code != 0:
        raise RuntimeError(f"ck worktree create failed: {err}")
    _commit_in(task_worktree(root, 1), {"one.txt": "1\n"}, "task 1")
    _commit_in(task_worktree(root, 2), {"shared.txt": "from task 2\n"}, "task 2")
    _commit_in(task_worktree(root, 3), {"three.txt": "3\n", "shared.txt": "from task 3\n"}, "task 3")
    return root


def capture_land_keys(tmp_path: Path) -> Dict[str, List[str]]:
    """Key paths of `ck worktree land --json` for a clean gated landing and for a conflict."""
    root = land_project(tmp_path)
    code, obj, err = ck_json(root, "worktree", "land", "demo", "1", "--gate", "--json")
    if not isinstance(obj, dict):
        raise RuntimeError(f"land --json did not print JSON ({code}): {err}")
    clean = json_key_paths(obj)
    ck_text(root, "worktree", "merge", "demo", "2")
    code, obj, err = ck_json(root, "worktree", "land", "demo", "3", "--gate", "--json")
    if not isinstance(obj, dict):
        raise RuntimeError(f"land --json (conflict) did not print JSON ({code}): {err}")
    return {"clean": clean, "conflict": json_key_paths(obj)}


def capture_baselines(tmp: Path, out: Path = FIXTURES) -> None:
    """Regenerate land-json-keys.json and gate-text-baseline/*.txt from the ck of this checkout.

    Run once, from the unchanged base commit: `python tests/unattended_helpers.py capture`."""
    (out / "gate-text-baseline").mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(GATE_BASELINE_SCENARIOS):
        d = tmp / f"gate{i}"
        d.mkdir()
        (out / "gate-text-baseline" / f"{name}.txt").write_text(capture_gate_text(d, name))
    d = tmp / "land"
    d.mkdir()
    scenarios = capture_land_keys(d)
    union = sorted({p for paths in scenarios.values() for p in paths})
    (out / "land-json-keys.json").write_text(json.dumps({"paths": union, "scenarios": scenarios}, indent=2) + "\n")


# --------------------------------------------------------------------------- failure schema

_JSON_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
               "integer": int, "number": (int, float)}


def _is_type(value: Any, name: str) -> bool:
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _JSON_TYPES[name])


def schema_errors(instance: Any, schema: Mapping[str, Any], path: str = "$") -> List[str]:
    """Validate against the JSON Schema subset the fixtures use (type, enum, required, properties, items,
    pattern, maxLength). `jsonschema` is not a project dependency, so this is the one validator for Python tests."""
    import re

    errors: List[str] = []
    types = schema.get("type")
    if types is not None:
        names = [types] if isinstance(types, str) else list(types)
        if not any(_is_type(instance, n) for n in names):
            return [f"{path}: expected {names}, got {type(instance).__name__}"]
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in {schema['enum']}")
    if isinstance(instance, str):
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: length {len(instance)} over maxLength {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: {instance!r} does not match {schema['pattern']}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required key {key!r}")
        for key, sub in schema.get("properties", {}).items():
            if key in instance:
                errors.extend(schema_errors(instance[key], sub, f"{path}.{key}"))
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errors.extend(schema_errors(item, schema["items"], f"{path}[{i}]"))
    return errors


def failure_schema() -> Dict[str, Any]:
    return json.loads((FIXTURES / "failure.schema.json").read_text())


def failure_errors(entry: Any) -> List[str]:
    """Schema violations of one `ck-gate/1` failure entry (empty when valid)."""
    return schema_errors(entry, failure_schema())


if __name__ == "__main__":
    import tempfile

    if sys.argv[1:] == ["capture"]:
        with tempfile.TemporaryDirectory() as t:
            capture_baselines(Path(t))
    else:
        raise SystemExit("usage: python tests/unattended_helpers.py capture")
