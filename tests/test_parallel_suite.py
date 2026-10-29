"""This repo's own suite runs under pytest-xdist (Story 5): declared, configured, stable ids, same results."""

import json
import os
import re
import shlex
import subprocess
import sys
import time
import tomllib
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _child_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("CK_COMPARE_PARALLEL", "PYTEST_ADDOPTS", "PYTEST_XDIST_AUTO_NUM_WORKERS")}
    env.update(extra)
    return env


# --- TC-38 (5.1) --------------------------------------------------------------


def test_dev_dependency_group_declares_pytest_xdist():
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    dev = data.get("dependency-groups", {}).get("dev", [])
    names = [re.split(r"[\s<>=!~;\[]", req, maxsplit=1)[0].strip().lower() for req in dev if isinstance(req, str)]
    assert "pytest-xdist" in names, f"dependency-groups.dev is {dev!r}"


# --- TC-39 (5.2) --------------------------------------------------------------


def _runs_pytest_with_n_auto(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    invokes_pytest = any(t == "pytest" or t.endswith("/pytest") for t in tokens)
    n_auto = any(a == "-n" and b == "auto" for a, b in zip(tokens, tokens[1:])) or "-nauto" in tokens or "-n=auto" in tokens
    return invokes_pytest and n_auto


def test_ck_json_verify_runs_pytest_with_n_auto():
    config = json.loads((REPO / "specs" / "ck.json").read_text())
    verify = config.get("verify", [])
    if isinstance(verify, str):
        verify = [verify]
    assert any(_runs_pytest_with_n_auto(c) for c in verify), f"verify is {verify!r}"


# --- TC-40 (5.3) --------------------------------------------------------------


def _collect_ids(seed: str) -> list:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=REPO,
        env=_child_env(PYTHONHASHSEED=seed),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, f"collection failed with PYTHONHASHSEED={seed}:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
    return sorted(line.strip() for line in proc.stdout.splitlines() if "::" in line)


def test_collected_test_ids_are_stable_across_processes_and_hash_seeds():
    seeds = ["0", "1", "4242"]
    with ThreadPoolExecutor(max_workers=len(seeds)) as pool:
        runs = list(pool.map(_collect_ids, seeds))
    assert runs[0], "collection found no tests"
    for seed, ids in zip(seeds[1:], runs[1:]):
        if ids != runs[0]:
            only_first = sorted(set(runs[0]) - set(ids))[:20]
            only_this = sorted(set(ids) - set(runs[0]))[:20]
            pytest.fail(f"ids differ between PYTHONHASHSEED=0 and {seed}: only in 0: {only_first}; only in {seed}: {only_this}")


# --- TC-41 (5.4, NFR-4), opt-in ----------------------------------------------


def _junit_outcomes(path: Path) -> set:
    outcomes = set()
    for case in ET.parse(path).getroot().iter("testcase"):
        nodeid = f"{case.get('classname')}::{case.get('name')}"
        outcome = "passed"
        for child in case:
            if child.tag in ("failure", "error", "skipped"):
                outcome = child.tag
                break
        outcomes.add((nodeid, outcome))
    return outcomes


def _run_suite(extra: list, junit: Path) -> tuple:
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}", *extra, "tests"],
        cwd=REPO,
        env=_child_env(),
        capture_output=True,
        text=True,
    )
    wall = time.monotonic() - started
    assert junit.exists(), f"no junit report from {extra}:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
    return _junit_outcomes(junit), wall, proc.stdout


def _deselected(output: str) -> int:
    match = re.search(r"(\d+) deselected", output)
    return int(match.group(1)) if match else 0


@pytest.mark.skipif(os.environ.get("CK_COMPARE_PARALLEL") != "1", reason="opt-in: set CK_COMPARE_PARALLEL=1")
def test_compare_serial_and_parallel_runs_agree(tmp_path):
    serial, serial_wall, serial_out = _run_suite(["-p", "no:xdist"], tmp_path / "serial.xml")
    parallel, parallel_wall, parallel_out = _run_suite(["-n", "auto"], tmp_path / "parallel.xml")
    assert serial, "serial run reported no tests"
    assert serial == parallel, (
        f"only serial: {sorted(serial - parallel)[:20]}\nonly parallel: {sorted(parallel - serial)[:20]}"
    )
    assert _deselected(serial_out) == _deselected(parallel_out)
    cores = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    if cores >= 8:
        assert parallel_wall <= 0.5 * serial_wall, f"parallel {parallel_wall:.1f}s vs serial {serial_wall:.1f}s on {cores} cores"
