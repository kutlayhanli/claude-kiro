"""`ck gate` caps pytest-xdist workers across concurrent gates (Story 6).

Real `ck gate` subprocesses run real verify commands (`python probe.py <tag>`), and the probe records the
`PYTEST_XDIST_AUTO_NUM_WORKERS` value and argv it received. Gates are started in their own process group and
always released and killed in teardown. Only the seam properties vary the core count.
"""

import fcntl
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import git

PROPERTIES = {
    "P-17": "test_worker_cap_property",
    "P-18": "test_verify_argv_is_verbatim",
}

KEY = "PYTEST_XDIST_AUTO_NUM_WORKERS"
PY_DIR = str(Path(sys.executable).parent)

PROBE = '''import json, os, sys, time
from pathlib import Path

OUT = Path({out!r})
args = sys.argv[1:]
tag = args[0]
opts = dict(a.split("=", 1) for a in args[1:] if a.split("=", 1)[0] in ("HOLD", "SLEEP", "EXIT"))
record = {{"workers": os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"), "argv": sys.argv}}
tmp = OUT / (tag + ".json.tmp")
tmp.write_text(json.dumps(record))
os.replace(tmp, OUT / (tag + ".json"))
(OUT / (tag + ".started")).touch()
if "HOLD" in opts:
    deadline = time.monotonic() + 60
    while not Path(opts["HOLD"]).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
if "SLEEP" in opts:
    time.sleep(float(opts["SLEEP"]))
sys.exit(int(opts.get("EXIT", "0")))
'''

TASKS = """# Implementation Tasks: Calc

**Status:** In Progress

## Task Breakdown

### Task 1: Probe the gate environment
**Status:** Done
**Track:** impl
**Files:**
- `probe.py` - records what a verify command receives

**Acceptance:**
- [x] probe records its environment
"""


class GateProbe:
    def __init__(self, out: Path):
        self.out = out
        self.procs: List[subprocess.Popen] = []
        self.holds: List[Path] = []

    def install(self, project: Path) -> Path:
        (project / "probe.py").write_text(PROBE.format(out=str(self.out)))
        (project / "specs" / "calc" / "tasks.md").write_text(TASKS)
        self.configure(project, ["python probe.py setup"])
        git(project, "add", "-A")
        git(project, "commit", "-qm", "probe")
        return project

    def configure(self, project: Path, verify: List[str], **extra) -> None:
        config = {"verify": verify, "collect": "", **extra}
        (project / "specs" / "ck.json").write_text(json.dumps(config))

    def hold_file(self, tag: str) -> Path:
        hold = self.out / f"release-{tag}"
        self.holds.append(hold)
        return hold

    def env(self, extra: Optional[Dict[str, str]] = None, drop: tuple = ()) -> Dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in drop}
        env["PATH"] = PY_DIR + os.pathsep + env.get("PATH", "")
        env.update(extra or {})
        return env

    def start_gate(
        self,
        project: Path,
        tag: str,
        hold: Optional[Path] = None,
        args: str = "",
        verify: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        **config,
    ) -> subprocess.Popen:
        if verify is None:
            command = f"python probe.py {tag}"
            if args:
                command += f" {args}"
            if hold is not None:
                command += f" HOLD={hold}"
            verify = [command]
        self.configure(project, verify, **config)
        proc = subprocess.Popen(
            [sys.executable, "-m", "claude_kiro.cli", "gate", "calc"],
            cwd=project,
            env=env if env is not None else self.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self.procs.append(proc)
        return proc

    def finish(self, proc: subprocess.Popen, timeout: float = 120) -> tuple:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out

    def run_gate(self, project: Path, tag: str, **kwargs) -> tuple:
        return self.finish(self.start_gate(project, tag, **kwargs))

    def wait_started(self, tag: str, proc: subprocess.Popen, timeout: float = 60) -> None:
        marker = self.out / f"{tag}.started"
        deadline = time.monotonic() + timeout
        while not marker.exists():
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"gate for {tag} ended (exit {proc.returncode}) before its probe started:\n{out}")
            if time.monotonic() > deadline:
                pytest.fail(f"probe {tag} did not start within {timeout}s")
            time.sleep(0.05)

    def release(self, hold: Path) -> None:
        hold.touch()

    def seen(self, tag: str) -> dict:
        path = self.out / f"{tag}.json"
        assert path.exists(), f"probe {tag} never ran"
        return json.loads(path.read_text())

    def cleanup(self) -> None:
        for hold in self.holds:
            try:
                hold.touch()
            except OSError:
                pass
        for proc in self.procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.communicate(timeout=30)
            except Exception:
                pass


@pytest.fixture
def gate_probe(project, tmp_path):
    out = tmp_path / "probe-out"
    out.mkdir()
    probe = GateProbe(out)
    probe.install(project)
    probe.project = project
    try:
        yield probe
    finally:
        probe.cleanup()


@pytest.fixture
def project_b(gate_probe, tmp_path):
    """A second, independent project with the same probe (the cross-project case)."""
    other = tmp_path / "proj_b"
    shutil.copytree(gate_probe.project, other, symlinks=True)
    return other


def _cores() -> int:
    from claude_kiro.xdist import core_count

    return core_count()


def _two_gates(probe: GateProbe, x: Path, y: Path) -> str:
    """Gate A holds in project X; gate B runs in project Y. Returns what B's probe saw."""
    hold = probe.hold_file("a")
    a = probe.start_gate(x, "a", hold=hold)
    probe.wait_started("a", a)
    code, out = probe.run_gate(y, "b")
    assert code == 0, out
    seen = probe.seen("b")["workers"]
    probe.release(hold)
    code, out = probe.finish(a)
    assert code == 0, out
    return seen


# --- TC-42 (6.1) --------------------------------------------------------------


def test_concurrent_gate_in_another_project_halves_the_workers(gate_probe, project_b):
    cores = _cores()
    seen_b = _two_gates(gate_probe, gate_probe.project, project_b)
    assert seen_b == str(max(1, cores // 2))
    code, out = gate_probe.run_gate(gate_probe.project, "c")
    assert code == 0, out
    assert gate_probe.seen("c")["workers"] == str(cores)


# --- TC-56 (6.1-6.4) ----------------------------------------------------------


def test_gate_uses_the_worker_cap_seam(gate_probe, project_b):
    from claude_kiro.xdist import core_count, worker_cap

    seen_b = _two_gates(gate_probe, gate_probe.project, project_b)
    assert seen_b == worker_cap(None, core_count(), 2)


# --- TC-43 (6.2) --------------------------------------------------------------


@pytest.mark.parametrize("preset", ["3", ""], ids=["three", "empty"])
def test_preset_worker_value_passes_through(gate_probe, project_b, preset):
    hold = gate_probe.hold_file("a")
    a = gate_probe.start_gate(gate_probe.project, "a", hold=hold)
    gate_probe.wait_started("a", a)
    code, out = gate_probe.run_gate(project_b, "b", env=gate_probe.env({KEY: preset}))
    assert code == 0, out
    assert gate_probe.seen("b")["workers"] == preset
    gate_probe.release(hold)
    gate_probe.finish(a)


# --- TC-44 (6.3) --------------------------------------------------------------


@pytest.mark.parametrize("gates", [5, 9, 100])
def test_more_gates_than_cores_gives_one_worker(gates):
    from claude_kiro.xdist import worker_cap

    assert worker_cap(None, 4, gates) == "1"


# --- TC-45 (6.4) --------------------------------------------------------------


def test_single_gate_gets_every_core(gate_probe):
    cores = _cores()
    code, out = gate_probe.run_gate(gate_probe.project, "solo")
    assert code == 0, out
    assert gate_probe.seen("solo")["workers"] == str(cores)


# --- TC-46 (6.5) --------------------------------------------------------------


@pytest.mark.parametrize("ending", ["pass", "fail", "timeout", "sigkill"])
def test_ended_gate_is_no_longer_counted(gate_probe, ending):
    cores = _cores()
    project = gate_probe.project
    if ending == "pass":
        code, out = gate_probe.run_gate(project, "a")
        assert code == 0, out
    elif ending == "fail":
        code, out = gate_probe.run_gate(project, "a", args="EXIT=1")
        assert code != 0, out
    elif ending == "timeout":
        code, out = gate_probe.run_gate(project, "a", args="SLEEP=30", verify_timeout=1)
        assert code != 0, out
        assert "timed out" in out
    else:
        a = gate_probe.start_gate(project, "a", hold=gate_probe.hold_file("a"))
        gate_probe.wait_started("a", a)
        os.killpg(a.pid, signal.SIGKILL)
        a.communicate(timeout=30)
        assert a.returncode == -signal.SIGKILL
    code, out = gate_probe.run_gate(project, "b")
    assert code == 0, out
    assert gate_probe.seen("b")["workers"] == str(cores)


# --- TC-47 (6.6) --------------------------------------------------------------


def _unusable_lines(output: str, registry: Path) -> List[str]:
    return [line for line in output.splitlines() if "unusable" in line and str(registry) in line]


def _check_unusable_registry(probe: GateProbe, registry: Path) -> None:
    cores = _cores()
    env = probe.env({"CK_GATE_REGISTRY": str(registry)})
    code, out = probe.run_gate(
        probe.project, "a", verify=["python probe.py a", "python probe.py b"], env=env
    )
    assert code == 0, out
    assert probe.seen("a")["workers"] == str(cores)
    assert probe.seen("b")["workers"] == str(cores)
    assert len(_unusable_lines(out, registry)) == 1, out
    assert len([line for line in out.splitlines() if "unusable" in line]) == 1, out

    code, out = probe.run_gate(probe.project, "f", args="EXIT=1", env=env)
    assert code == 1, out
    assert "FAIL" in out
    assert probe.seen("f")["workers"] == str(cores)
    assert len(_unusable_lines(out, registry)) == 1, out


def test_registry_path_that_is_a_file_still_runs_the_gate(gate_probe, tmp_path):
    registry = tmp_path / "registry-file"
    registry.write_text("not a directory\n")
    _check_unusable_registry(gate_probe, registry)
    assert registry.read_text() == "not a directory\n"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_unreadable_registry_directory_still_runs_the_gate(gate_probe, tmp_path):
    registry = tmp_path / "registry-000"
    registry.mkdir()
    registry.chmod(0)
    try:
        _check_unusable_registry(gate_probe, registry)
    finally:
        registry.chmod(0o700)


# --- TC-60 (6.1) --------------------------------------------------------------


def _registry_files(root: Path) -> List[str]:
    return sorted(
        str(p) for p in root.rglob("*") if p.name.startswith("gate-") and p.name.endswith(".lock") or p.name.startswith(".reg-")
    )


def test_gates_with_different_tmpdirs_share_the_default_registry(gate_probe, project_b, tmp_path):
    cores = _cores()
    home = tmp_path / "home"
    home.mkdir()
    dir1, dir2 = tmp_path / "tmp1", tmp_path / "tmp2"
    dir1.mkdir()
    dir2.mkdir()
    drop = ("CK_GATE_REGISTRY",)
    env_a = gate_probe.env({"HOME": str(home), "TMPDIR": str(dir1)}, drop=drop)
    env_b = gate_probe.env({"HOME": str(home), "TMPDIR": str(dir2)}, drop=drop)

    hold = gate_probe.hold_file("a")
    a = gate_probe.start_gate(gate_probe.project, "a", hold=hold, env=env_a)
    gate_probe.wait_started("a", a)
    registry_seen_while_running = {d: _registry_files(d) for d in (dir1, dir2, gate_probe.project, project_b)}
    code, out = gate_probe.run_gate(project_b, "b", env=env_b)
    assert code == 0, out
    assert gate_probe.seen("b")["workers"] == str(max(1, cores // 2))
    gate_probe.release(hold)
    code, out = gate_probe.finish(a)
    assert code == 0, out

    assert registry_seen_while_running == {d: [] for d in registry_seen_while_running}
    for d in (dir1, dir2, gate_probe.project, project_b):
        assert _registry_files(d) == [], d


# --- TC-61 (6.1, 6.5) ---------------------------------------------------------

HOLDER = """import fcntl, sys
f = open(sys.argv[1], "w")
fcntl.flock(f, fcntl.LOCK_EX)
print("locked", flush=True)
sys.stdin.read()
"""

COUNTER = """import sys
from pathlib import Path
from claude_kiro.xdist import running_gates
reg, stop = Path(sys.argv[1]), Path(sys.argv[2])
n = 0
seen = set()
while not stop.exists():
    seen.add(running_gates(reg))
    n += 1
print(n, sorted(x for x in seen if x is not None), flush=True)
"""


def test_counter_never_removes_live_or_in_progress_registrations(tmp_path):
    from claude_kiro.xdist import gate_slot, running_gates

    registry = tmp_path / "registry"
    registry.mkdir()
    in_progress = registry / ".reg-999998-0badf00d"
    in_progress.write_text("")
    live = registry / "gate-999999-deadbeef.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(live)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    stop = tmp_path / "stop-counter"
    counter = None
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert running_gates(registry) == 1
        assert in_progress.exists() and live.exists()

        counter = subprocess.Popen(
            [sys.executable, "-c", COUNTER, str(registry), str(stop)], stdout=subprocess.PIPE, text=True
        )
        for _ in range(30):
            with gate_slot(registry) as slot:
                assert slot is not None
                for _ in range(10):
                    assert running_gates(registry) == 2
                    assert len(list(registry.glob("gate-*.lock"))) == 2
            assert running_gates(registry) == 1
        stop.touch()
        out, _ = counter.communicate(timeout=60)
        assert counter.returncode == 0, out
        assert int(out.split()[0]) > 0
        assert in_progress.exists() and live.exists()
        assert sorted(p.name for p in registry.iterdir()) == sorted([in_progress.name, live.name])
    finally:
        stop.touch()
        if counter is not None and counter.poll() is None:
            counter.kill()
            counter.wait()
        holder.stdin.close()
        holder.wait(timeout=30)

    # Once the holder is gone its file is lockable: an ended gate, no longer counted.
    assert running_gates(registry) == 0
    assert in_progress.exists()


# --- TC-48 (6.7) and P-18 -----------------------------------------------------


def test_verify_commands_receive_their_argv_verbatim(gate_probe):
    verify = ["python probe.py a", "python probe.py b -n 2"]
    code, out = gate_probe.run_gate(gate_probe.project, "a", verify=verify)
    assert code == 0, out
    assert gate_probe.seen("a")["argv"] == ["probe.py", "a"]
    assert gate_probe.seen("b")["argv"] == ["probe.py", "b", "-n", "2"]
    for command in verify:
        assert command in out


@pytest.mark.parametrize(
    "form",
    ["", "-n 2", "-n auto", "--numprocesses=3", "-p no:xdist", "-nauto"],
    ids=["plain", "n-2", "n-auto", "numprocesses-3", "no-xdist", "nauto"],
)
def test_verify_argv_is_verbatim(gate_probe, form):
    command = f"python probe.py x {form}".strip()
    code, out = gate_probe.run_gate(gate_probe.project, "x", verify=[command])
    assert code == 0, out
    assert gate_probe.seen("x")["argv"] == ["probe.py", "x", *shlex.split(form)]
    assert f"`{command}`" in out


# --- P-17 (6.1-6.4, 6.6) at the seam -----------------------------------------

PRESETS = st.sampled_from([None, "", "0", "1", "auto", "16", " 4"])
CORES = st.one_of(st.none(), st.integers(1, 256))
RUNNING = st.one_of(st.none(), st.sampled_from([-1, 0]), st.integers(1, 300))


@settings(derandomize=True, max_examples=200, database=None, deadline=None)
@given(preset=PRESETS, cores=CORES, running=RUNNING)
def test_worker_cap_property(preset, cores, running):
    from claude_kiro.xdist import worker_cap

    value = worker_cap(preset, cores, running)
    if preset is not None:
        assert value is None
    else:
        c = cores if cores is not None and cores >= 1 else 1
        r = running if running is not None and running >= 1 else 1
        assert value == str(max(1, c // r))


UNRELATED = st.dictionaries(
    st.sampled_from(["PYTEST_ADDOPTS", "PATH", "HOME", "PYTEST_XDIST_WORKER", "CK_X"]),
    st.sampled_from(["-n 3", "", "x", "/usr/bin", "auto"]),
    max_size=4,
)


@settings(derandomize=True, max_examples=200, database=None, deadline=None)
@given(preset=PRESETS, unrelated=UNRELATED)
def test_verify_env_touches_only_the_worker_key(preset, unrelated):
    from claude_kiro.xdist import core_count, verify_env

    base = dict(unrelated)
    if preset is not None:
        base[KEY] = preset
    before = dict(base)
    with tempfile.TemporaryDirectory() as registry:
        env = verify_env(base, Path(registry))
    assert base == before
    assert {k: v for k, v in env.items() if k != KEY} == {k: v for k, v in base.items() if k != KEY}
    if preset is not None:
        assert env[KEY] == preset
    else:
        assert env[KEY] == str(max(1, core_count() or 1))


def test_verify_env_leaves_pytest_addopts_untouched():
    from claude_kiro.xdist import core_count, verify_env

    with tempfile.TemporaryDirectory() as registry:
        env = verify_env({"PYTEST_ADDOPTS": "-n 3"}, Path(registry))
    assert env == {"PYTEST_ADDOPTS": "-n 3", KEY: str(core_count())}
