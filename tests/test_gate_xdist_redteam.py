"""Red team for Story 6's SHALL NOT clause (6.5): a gate whose process no longer exists is never counted.

Every case runs real `ck gate` subprocesses (each in its own process group) against a real git
project whose verify command is a probe script. The probe records the
`PYTEST_XDIST_AUTO_NUM_WORKERS` value it was given, so a fresh gate started after an adversarial
setup shows whether a gone gate (or a file that only looks like one) was counted: with no other
live gate, the fresh gate must see the machine's full core count (6.4) and still run its verify.

Each test sets its own `CK_GATE_REGISTRY` (a per-test directory) and clears any inherited
`PYTEST_XDIST_AUTO_NUM_WORKERS`, so tests never count each other or the outer gate. Teardown
kills every process group the test started, including the detached children the probe forks.
"""

import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from conftest import git

CK = [sys.executable, "-m", "claude_kiro.cli"]
VAR = "PYTEST_XDIST_AUTO_NUM_WORKERS"
WAIT_S = 60

PROBE = r'''
import json, os, sys, time

args = sys.argv[1:]
tag = args[0]
opts = args[1:]
out_dir = os.environ["PROBE_OUT"]
with open(os.path.join(out_dir, tag + ".json"), "w") as fh:
    json.dump({"value": os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"), "argv": sys.argv, "pid": os.getpid()}, fh)
if "--fork" in opts:
    # A long-lived child that outlives the verify command and the gate: forked (not exec'd),
    # detached into its own session, stdio closed so the gate is not kept waiting on its pipes.
    pid = os.fork()
    if pid == 0:
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        with open(os.path.join(out_dir, tag + ".child"), "w") as fh:
            fh.write(str(os.getpid()))
        time.sleep(120)
        os._exit(0)
    deadline = time.time() + 30
    while not os.path.exists(os.path.join(out_dir, tag + ".child")) and time.time() < deadline:
        time.sleep(0.05)
if "--hold" in opts:
    hold = os.path.join(out_dir, opts[opts.index("--hold") + 1])
    open(os.path.join(out_dir, tag + ".started"), "w").close()
    deadline = time.time() + 90
    while not os.path.exists(hold) and time.time() < deadline:
        time.sleep(0.05)
sys.exit(0)
'''

TASKS = """# Implementation Tasks: Demo

**Status:** In Progress

## Task Breakdown

### Task 1: Implement demo
**Status:** Done
**Track:** impl
**Files:**
- `demo.py` - the demo

**Acceptance:**
- [x] demo implemented

## Parallel Groups
Wave 1: Task 1
"""


def core_count() -> int:
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 1


def wait_for(path: Path, timeout: float = WAIT_S) -> None:
    deadline = time.time() + timeout
    while not path.exists():
        if time.time() > deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.05)


class Gates:
    """Starts real `ck gate demo` processes, each in its own process group, and kills them all at teardown."""

    def __init__(self, tmp_path: Path, registry: Path):
        self.tmp_path = tmp_path
        self.registry = registry
        self.out = tmp_path / "out"
        self.out.mkdir()
        self.procs: List[subprocess.Popen] = []
        self.projects = 0

    def project(self, verify: List[str]) -> Path:
        self.projects += 1
        root = self.tmp_path / f"proj{self.projects}"
        (root / "specs" / "demo").mkdir(parents=True)
        git(root, "init", "-q", "-b", "main")
        (root / "probe.py").write_text(PROBE)
        (root / "demo.py").write_text("X = 1\n")
        (root / "specs" / "demo" / "requirements.md").write_text("# Feature: Demo\n- 1.1 THE SYSTEM SHALL demo\n")
        (root / "specs" / "demo" / "tasks.md").write_text(TASKS)
        (root / "specs" / "ck.json").write_text(json.dumps({"verify": verify, "verify_timeout": 120}))
        git(root, "add", "-A")
        git(root, "commit", "-qm", "base")
        return root

    def probe_cmd(self, tag: str, *opts: str) -> str:
        # The output directory goes through the environment: a tmp path contains "pytest", which
        # would make the gate treat the probe as a pytest command.
        parts = [sys.executable, "probe.py", tag, *opts]
        return " ".join(shlex.quote(p) for p in parts)

    def start(self, tag: str, *opts: str) -> subprocess.Popen:
        root = self.project([self.probe_cmd(tag, *opts)])
        proc = subprocess.Popen(
            [*CK, "gate", "demo"],
            cwd=root,
            env={**os.environ, "PROBE_OUT": str(self.out)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self.procs.append(proc)
        return proc

    def start_holding(self, tag: str) -> Path:
        """Start a gate whose verify command waits until the returned hold file exists."""
        hold = self.out / f"{tag}.release"
        self.start(tag, "--hold", hold.name)
        wait_for(self.out / f"{tag}.started")
        return hold

    def finish(self, proc: subprocess.Popen) -> str:
        output, _ = proc.communicate(timeout=180)
        return output

    def run(self, tag: str, *opts: str) -> Dict:
        """Run a gate to completion; it must pass and its verify command must have run."""
        proc = self.start(tag, *opts)
        output = self.finish(proc)
        assert proc.returncode == 0, output
        record = self.out / f"{tag}.json"
        assert record.exists(), f"the gate did not run its verify command:\n{output}"
        return json.loads(record.read_text())

    def seen(self, tag: str) -> Optional[str]:
        return json.loads((self.out / f"{tag}.json").read_text())["value"]

    def kill_all(self) -> None:
        for proc in self.procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                proc.communicate(timeout=10)
            except Exception:
                pass
        for child in self.out.glob("*.child"):
            try:
                pid = int(child.read_text())
                os.killpg(pid, signal.SIGKILL)
            except (ValueError, ProcessLookupError, PermissionError):
                pass


@pytest.fixture
def registry(tmp_path, monkeypatch) -> Path:
    reg = tmp_path / "registry"
    reg.mkdir()
    monkeypatch.setenv("CK_GATE_REGISTRY", str(reg))
    monkeypatch.delenv(VAR, raising=False)
    return reg


@pytest.fixture
def gates(tmp_path, registry):
    g = Gates(tmp_path, registry)
    yield g
    g.kill_all()


def full() -> str:
    return str(core_count())


def shared(n: int) -> str:
    return str(max(1, core_count() // n))


def snapshot(directory: Path) -> Dict[str, Optional[bytes]]:
    """Relative path -> bytes (None for directories) for everything under `directory`."""
    result: Dict[str, Optional[bytes]] = {}
    for path in sorted(directory.rglob("*")):
        rel = str(path.relative_to(directory))
        if path.is_symlink():
            result[rel] = b"symlink:" + os.readlink(path).encode()
        elif path.is_dir():
            result[rel] = None
        else:
            result[rel] = path.read_bytes()
    return result


# --- a gate that no longer exists ----------------------------------------------------------------


def test_gate_sigkilled_mid_verify_is_not_counted(gates):
    """Gate and verify SIGKILLed together, three times over: no corpse is counted."""
    for i in range(3):
        gates.start_holding(f"victim{i}")
        victim = gates.procs[-1]
        os.killpg(victim.pid, signal.SIGKILL)
        victim.communicate(timeout=30)
    record = gates.run("fresh")
    assert record["value"] == full()


def test_gate_sigkilled_alone_while_its_verify_child_lives_is_not_counted(gates):
    """Only the gate process is killed; its verify command keeps running. The gate is gone."""
    gates.start_holding("victim")
    victim = gates.procs[-1]
    # Precondition: the registry works, so the victim is visible while it is alive.
    assert gates.run("during")["value"] == shared(2)
    os.kill(victim.pid, signal.SIGKILL)
    victim.communicate(timeout=30)
    assert gates.run("fresh")["value"] == full()


def test_verify_forking_a_long_lived_child_does_not_keep_the_gate_counted(gates):
    """A verify command leaves a detached child behind (it outlives the gate): the ended gate is not counted."""
    first = gates.run("forker", "--fork")
    assert first["value"] == full()
    child = int((gates.out / "forker.child").read_text())
    os.kill(child, 0)  # still alive after its gate ended
    assert gates.run("fresh")["value"] == full()


def test_two_gates_started_in_the_same_instant_are_not_counted_after_they_end(gates):
    a = gates.start("twin-a")
    b = gates.start("twin-b")
    for proc in (a, b):
        output = gates.finish(proc)
        assert proc.returncode == 0, output
    assert gates.seen("twin-a") is not None and gates.seen("twin-b") is not None
    assert gates.run("fresh")["value"] == full()


def test_many_simultaneous_gates_killed_together_are_not_counted(gates):
    for i in range(4):
        gates.start_holding(f"crowd{i}")
    for proc in list(gates.procs):
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=30)
    assert gates.run("fresh")["value"] == full()


# --- the system's own registry entries fed back --------------------------------------------------


def test_registry_entries_of_an_ended_gate_restored_afterwards_are_not_counted(gates, registry):
    """Copy a live gate's registry entries, let it end, put the copies back: they belong to a gone gate."""
    hold = gates.start_holding("live")
    assert gates.run("during")["value"] == shared(2)
    copies = {p.name: p.read_bytes() for p in registry.iterdir() if p.is_file()}
    assert copies, "a live gate left no entry in its registry"
    hold.touch()
    gates.finish(gates.procs[0])
    for name, data in copies.items():
        (registry / name).write_bytes(data)
    assert gates.run("fresh")["value"] == full()
    # And again: whatever the fresh gate left behind is fed back once more.
    assert gates.run("fresh-again")["value"] == full()


def test_entries_named_after_live_non_gate_processes_are_not_counted(gates, registry):
    """Gate-shaped names carrying PIDs of processes that exist (this test, pid 1) but are not gates."""
    for pid in (os.getpid(), 1, os.getppid()):
        (registry / f"gate-{pid}-deadbeef.lock").write_bytes(b"")
    (registry / f"gate-{os.getpid()}-0123abcd.lock").write_bytes(b"leftover")
    assert gates.run("fresh")["value"] == full()


def test_read_only_leftover_entry_is_not_counted(gates, registry):
    entry = registry / "gate-99999-cafef00d.lock"
    entry.write_bytes(b"")
    entry.chmod(stat.S_IRUSR)
    assert gates.run("fresh")["value"] == full()


def test_dangling_symlink_entry_is_not_counted(gates, registry):
    (registry / "gate-4242-0badf00d.lock").symlink_to(registry / "missing" / "gate-4242-0badf00d.lock")
    assert gates.run("fresh")["value"] == full()


# --- stray files are not gates, and survive -----------------------------------------------------


def test_stray_files_and_directories_survive_and_are_not_counted(gates, registry):
    (registry / "notes.txt").write_text("keep me\n")
    (registry / "gate-notes.txt").write_text("near miss\n")
    (registry / "gate-1-deadbeef.lock.bak").write_bytes(b"backup")
    (registry / "Gate-2-deadbeef.LOCK").write_bytes(b"case")
    (registry / ".reg-3-deadbeef").write_bytes(b"")
    (registry / "gate-5-deadbeef.lock ").write_bytes(b"trailing space")
    sub = registry / "sub dir"
    sub.mkdir()
    (sub / "gate-6-deadbeef.lock").write_bytes(b"nested")
    gate_named_dir = registry / "gate-7-deadbeef.lock"
    gate_named_dir.mkdir()
    (gate_named_dir / "inside").write_text("x")
    before = snapshot(registry)

    assert gates.run("fresh")["value"] == full()

    after = snapshot(registry)
    for rel, content in before.items():
        assert rel in after, f"stray registry entry {rel!r} was removed"
        assert after[rel] == content, f"stray registry entry {rel!r} was changed"


# --- unusual registry paths ---------------------------------------------------------------------


def test_registry_path_with_spaces_and_unicode(gates, tmp_path, monkeypatch):
    reg = tmp_path / "reg dir ü 日本 'q' $HOME"
    reg.mkdir()
    monkeypatch.setenv("CK_GATE_REGISTRY", str(reg))
    hold = gates.start_holding("live")
    assert gates.run("during")["value"] == shared(2), "the registry at this path must count the live gate"
    hold.touch()
    gates.finish(gates.procs[0])
    assert gates.run("fresh")["value"] == full()
