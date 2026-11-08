"""The guarded DeepSeek Harness (`dsh`) verification record (Story 18: 18.1, 18.3, 18.5).

The record `specs/unattended-correctness/dsh-verification.md` carries a fenced json block with
`{repository, version, checkedAt, canHostAgent, canHostCk, missing, evidence}`. These tests only read it;
they never write it. TC-156 runs the real CLI with only the `dsh` binary faked.
"""

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from bench_helpers import CK, bench_setup

RECORD = Path(__file__).resolve().parent.parent / "specs" / "unattended-correctness" / "dsh-verification.md"
KEYS = ("repository", "version", "checkedAt", "canHostAgent", "canHostCk", "missing", "evidence")


def load_record() -> dict:
    """The record's fenced json block; fails (not skips) when the record or block is missing."""
    assert RECORD.is_file(), f"the verification record does not exist: {RECORD}"
    blocks = re.findall(r"```json\s*\n(.*?)\n```", RECORD.read_text(), flags=re.S)
    assert blocks, "the verification record has no fenced json block"
    data = json.loads(blocks[0])
    assert isinstance(data, dict), "the json block is not an object"
    return data


def fake_dsh(directory: Path) -> Path:
    """A `dsh` binary on PATH: it exists and reports a version, so only registration can refuse it."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "dsh"
    path.write_text("#!/bin/sh\necho 'dsh 1.0.0'\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def run_ck(cwd: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([*CK, *args], cwd=cwd, env=env, capture_output=True, text=True)


def test_tc154_record_has_the_verification_fields():
    data = load_record()
    for key in KEYS:
        assert key in data, f"missing key {key!r}"
    assert "github.com/deepseek-ai/" in data["repository"]
    assert isinstance(data["version"], str) and data["version"].strip()
    assert isinstance(data["checkedAt"], str) and data["checkedAt"].strip()
    assert isinstance(data["canHostAgent"], bool)
    assert isinstance(data["canHostCk"], bool)
    assert isinstance(data["missing"], list)
    assert isinstance(data["evidence"], list) and data["evidence"], "evidence must list what was checked"


def test_tc156_findings_report_and_dsh_not_registered(tmp_path):
    data = load_record()
    if data.get("canHostAgent") is True and data.get("canHostCk") is True:
        pytest.skip("dsh can host agent() and ck(): 18.2 applies, so no findings report (TC-155 and TC-157 cover it)")

    # The findings report (18.3): what was checked, the version, and what is missing.
    assert data.get("checkedAt") or data.get("checked"), "findings report lacks when/what was checked"
    assert isinstance(data.get("version"), str) and data["version"].strip()
    assert isinstance(data.get("missing"), list) and data["missing"], "findings report must say what is missing"

    # dsh is not registered: even with a dsh binary on PATH, the CLI refuses it and lists the registered ones.
    bin_dir = tmp_path / "bin"
    fake_dsh(bin_dir)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    cfg = bench_setup(tmp_path)

    bench = run_ck(tmp_path, env, "bench", "run", str(cfg), "--dry-run", "--harness", "dsh")
    assert bench.returncode != 0, bench.stdout + bench.stderr
    text = bench.stdout + bench.stderr
    assert "claude" in text and "opencode" in text, text
    assert not re.search(r"registered:[^)\n]*dsh", text), text

    doctor = run_ck(tmp_path, env, "doctor", "--harness", "dsh")
    assert doctor.returncode != 0, doctor.stdout + doctor.stderr
    text = doctor.stdout + doctor.stderr
    assert "claude" in text and "opencode" in text, text
    assert not re.search(r"registered:[^)\n]*dsh", text), text

    # The registry module, when it exists, does not list dsh either.
    code = "from claude_kiro import harnesses as h\nprint(sorted(getattr(h, 'REGISTRY', getattr(h, 'HARNESSES', {}))))\n"
    reg = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    if reg.returncode == 0:
        assert "dsh" not in reg.stdout, reg.stdout
