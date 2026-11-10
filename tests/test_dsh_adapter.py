"""The `dsh` adapter against the recorded API (Story 18: 18.2, 18.4; TC-155, TC-157).

The fake `dsh` in tests/fixtures/unattended/fake_dsh implements only the surface the verification record
`specs/unattended-correctness/dsh-verification.md` names. When the record says `dsh` can host both `agent()` and
`ck()`, these tests must run (a skip is a failure: `test_tc155_ran_when_the_record_allows_it`). When it says it
cannot, both are skipped with the findings report as the reason.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from bench_helpers import CK, bench_setup
from claude_kiro.bench import check_arm_result

from test_dsh_harness import load_record

FAKE_DIR = Path(__file__).resolve().parent / "fixtures" / "unattended" / "fake_dsh"
RECORD_PATH = "specs/unattended-correctness/dsh-verification.md"


def _allowed() -> bool:
    rec = load_record()
    return rec.get("canHostAgent") is True and rec.get("canHostCk") is True


def _skip_reason() -> str:
    rec = load_record()
    return f"dsh cannot host both agent() and ck() (findings report {RECORD_PATH}): missing {rec.get('missing')}"


def _require_allowed():
    if not _allowed():
        pytest.skip(_skip_reason())


def _env(tmp_path: Path, version: str = "0.2.2-rc.1", on_path: bool = True) -> dict:
    env = dict(os.environ)
    env["FAKE_DSH_VERSION"] = version
    env["FAKE_DSH_LOG"] = str(tmp_path / "dsh.log")
    if on_path:
        env["PATH"] = f"{FAKE_DIR}{os.pathsep}{env.get('PATH', '')}"
    else:
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep) if not os.path.exists(os.path.join(p, "dsh")))
    return env


def _ck(cwd: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([*CK, *args], cwd=cwd, env=env, capture_output=True, text=True)


def test_tc155_ran_when_the_record_allows_it():
    """A skip is a failure when the record allows dsh: this test is the guard, it never skips."""
    rec = load_record()
    if _allowed():
        assert (FAKE_DIR / "dsh").is_file() and os.access(FAKE_DIR / "dsh", os.X_OK)
    else:
        assert rec["canHostAgent"] is False or rec["canHostCk"] is False


def test_tc155_fake_dsh_implements_only_the_recorded_surface(tmp_path):
    env = _env(tmp_path)
    out = subprocess.run([str(FAKE_DIR / "dsh"), "--version"], env=env, capture_output=True, text=True)
    assert out.returncode == 0 and "0.2.2-rc.1" in out.stdout
    req = '{"jsonrpc":"2.0","id":1,"method":"session/exec","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"shutdown"}\n'
    proc = subprocess.run([str(FAKE_DIR / "dsh"), "--profile", "sdk"], input=req, env=env, capture_output=True, text=True)
    first = json.loads(proc.stdout.splitlines()[0])
    assert first["error"]["code"] == -32601  # no process-execution request is invented


def test_tc155_bench_arm_runs_on_dsh(tmp_path):
    """agent() is observed at the fake (initialize, session/prompt). ck() is not observable there: the record
    says the protocol has no process-execution request, so the runner's own ck() runs ck directly (design
    line 258) and never reaches dsh; a successful arm run whose result passes the 17.5 check is its evidence."""
    _require_allowed()
    env = _env(tmp_path)
    cfg = bench_setup(tmp_path)
    run = _ck(tmp_path, env, "bench", "run", str(cfg), "--harness", "dsh")
    assert run.returncode == 0, run.stdout + run.stderr
    log = Path(env["FAKE_DSH_LOG"])
    assert log.is_file(), "no request reached the fake dsh"
    methods = [json.loads(line)["method"] for line in log.read_text().splitlines()]
    assert "initialize" in methods and "session/prompt" in methods, methods  # agent() reached dsh
    results = list((tmp_path / "results").rglob("*.json"))
    assert results, "bench wrote no result"
    arms = [d for d in (json.loads(p.read_text()) for p in results) if isinstance(d, dict) and "harness" in d]
    assert arms, "no arm result carries a harness key"
    for data in arms:
        assert data["harness"] == "dsh", data
        assert check_arm_result(data) == [], data  # the 17.5 key set


def test_tc157_dsh_missing_from_path(tmp_path):
    _require_allowed()
    if shutil.which("dsh", path=_env(tmp_path, on_path=False)["PATH"]):
        pytest.fail("a real dsh is on PATH and could not be removed; the record allows dsh so this test must run")
    env = _env(tmp_path, on_path=False)
    doctor = _ck(tmp_path, env, "doctor", "--harness", "dsh")
    assert doctor.returncode != 0 and "dsh" in (doctor.stdout + doctor.stderr)
    bench = _ck(tmp_path, env, "bench", "run", str(bench_setup(tmp_path)), "--harness", "dsh")
    assert bench.returncode != 0 and re.search(r"dsh", bench.stdout + bench.stderr)


def test_tc157_dsh_unsupported_version(tmp_path):
    _require_allowed()
    env = _env(tmp_path, version="0.0.1")
    doctor = _ck(tmp_path, env, "doctor", "--harness", "dsh")
    assert doctor.returncode != 0 and "0.0.1" in (doctor.stdout + doctor.stderr), doctor.stdout + doctor.stderr
    bench = _ck(tmp_path, env, "bench", "run", str(bench_setup(tmp_path)), "--harness", "dsh")
    assert bench.returncode != 0 and "version" in (bench.stdout + bench.stderr).lower()
