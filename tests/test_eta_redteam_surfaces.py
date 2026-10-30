"""Red team for the status line: requirement 8.3 (reads only progress files and the cached estimate, never simulates) and 8.4
(prints nothing for no active run, drops an unreadable run, and never prints an error).

Working from the requirements only, each clause is attacked with hostile stdin, hostile working directories, hostile
progress files (a directory, a FIFO, garbage) and hostile caches (deleted, truncated, another schema, poisoned values).
Every case runs the real `ck-status-line` entry point; the FIFO case carries a test-level timeout so a blocking read can never
hang the suite.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Union

import pytest

import eta_helpers as h
from eta_strategies import NO_NETWORK
from test_eta_surfaces import STATUS_NOW_MIN, World, base_env, data_home, needs_user, stamp

pytestmark = NO_NETWORK

BAD_WORDS = ("error", "Traceback", "❌")
HHMM = r"\d\d:\d\d"
TIMEOUT_S = 20


@pytest.fixture
def world(tmp_path) -> World:
    h.install_pool(h.POOL_IMPL, data_home())
    h.install_pool(h.POOL_CREATE, data_home())
    return World(tmp_path)


def run_status(env: Dict[str, str], payload: Union[str, bytes], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    """The real entry point with raw bytes on stdin; fails the test (rather than hanging) after TIMEOUT_S."""
    data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    exe = Path(sys.executable).parent / h.STATUSLINE_BIN
    try:
        return subprocess.run([str(exe)], input=data, cwd=cwd, env={**os.environ, **env}, capture_output=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        pytest.fail(f"ck-status-line did not return within {TIMEOUT_S} s")


def clean(done: subprocess.CompletedProcess) -> List[str]:
    """The exit code is 0, stderr is empty and no line holds an error word; returns the output lines."""
    assert done.returncode == 0, done.stderr
    assert done.stderr == b"", done.stderr
    text = done.stdout.decode("utf-8", errors="replace")
    for word in BAD_WORDS:
        assert word not in text, text
    return [ln for ln in text.splitlines() if ln.strip()]


def stdin_for(root: Path) -> str:
    return json.dumps({"workspace": {"current_dir": str(root)}})


def env_for(**extra: str) -> Dict[str, str]:
    return {**base_env(), h.NOW_ENV: stamp(STATUS_NOW_MIN), **extra}


def cache_path(world: World, name: str) -> Path:
    progress = world.files[name]
    return progress.with_name(progress.stem + ".eta.json")


def demo_line(lines: List[str]) -> Optional[str]:
    return next((ln for ln in lines if ln.startswith("ck demo ")), None)


# --------------------------------------------------------------------------- 8.3: reads files only, never simulates


def test_83_a_poisoned_cache_is_shown_as_it_is_not_recomputed(world):
    path = cache_path(world, "demo")
    body = json.loads(path.read_text())
    body["finish"] = {k: "2026-10-30T23:59:00+00:00" for k in ("p10", "p50", "p90")}
    body["cost"] = {"p10": 1.0, "p50": 12345.4, "p90": 99999.0}
    path.write_text(json.dumps(body))
    line = demo_line(clean(run_status(env_for(), stdin_for(world.a))))
    assert line == "ck demo 3/6 ETA 23:59 ~$12345", line


def test_83_a_deleted_cache_gives_a_question_mark_not_a_simulated_time(world):
    cache_path(world, "demo").unlink()
    line = demo_line(clean(run_status(env_for(), stdin_for(world.a))))
    assert line is not None and "ETA ?" in line, line
    assert re.search(HHMM, line) is None, line


def test_83_a_deleted_cache_and_model_snapshot_leave_the_other_runs_unchanged(world):
    before = clean(run_status(env_for(), stdin_for(world.a)))
    cache_path(world, "demo").unlink()
    world.files["demo"].with_name(world.files["demo"].stem + ".model.json").unlink()
    after = clean(run_status(env_for(), stdin_for(world.a)))
    assert sorted(ln for ln in after if not ln.startswith("ck demo ")) == sorted(ln for ln in before if not ln.startswith("ck demo "))


def test_83_a_garbage_model_snapshot_cannot_change_the_output(world):
    before = run_status(env_for(), stdin_for(world.a)).stdout
    snapshot = world.files["demo"].with_name(world.files["demo"].stem + ".model.json")
    snapshot.write_text("{ not a model")
    assert run_status(env_for(), stdin_for(world.a)).stdout == before


def test_83_the_line_does_not_depend_on_the_history_store(world):
    before = run_status(env_for(), stdin_for(world.a)).stdout
    runs = data_home() / "claude-kiro" / "runs"
    for path in runs.glob("*.json"):
        path.write_text("[]")
    assert run_status(env_for(), stdin_for(world.a)).stdout == before


# --------------------------------------------------------------------------- 8.4: never an error, drops what it cannot read


def test_84_a_one_megabyte_payload_is_harmless(world):
    padding = "x" * (1024 * 1024)
    payload = json.dumps({"workspace": {"current_dir": str(world.a)}, "padding": padding})
    lines = clean(run_status(env_for(), payload))
    assert len(lines) == 4


def test_84_a_one_megabyte_of_junk_is_harmless(world):
    assert clean(run_status(env_for(), "{" * (1024 * 1024), cwd=world.a)) is not None


def test_84_non_utf8_bytes_on_stdin_are_harmless(world):
    junk = b'{"workspace": {"current_dir": "\xff\xfe\xfa"}, "x": "\x80\x81"}'
    clean(run_status(env_for(), junk, cwd=world.a))
    clean(run_status(env_for(), b"\xff\xfe\x00\x01\x02", cwd=world.a))


def test_84_a_current_dir_that_is_a_file_prints_nothing_for_it(tmp_path, world):
    stray = tmp_path / "stray.txt"  # outside every project root, so no run matches its path
    stray.write_text("not a directory\n")
    assert clean(run_status(env_for(), stdin_for(stray))) == []
    clean(run_status(env_for(), stdin_for(world.a / "specs" / "demo" / "tasks.md")))  # inside A: only clean behaviour


def test_84_a_current_dir_in_a_symlink_loop_is_harmless(tmp_path, world):
    one, two = tmp_path / "loop-one", tmp_path / "loop-two"
    one.symlink_to(two)
    two.symlink_to(one)
    clean(run_status(env_for(), stdin_for(one)))
    clean(run_status(env_for(), stdin_for(one / "deeper" / "still")))


def test_84_a_current_dir_that_does_not_exist_prints_nothing(tmp_path, world):
    assert clean(run_status(env_for(), stdin_for(tmp_path / "no" / "such" / "dir"))) == []


def test_84_a_progress_file_replaced_by_a_directory_drops_only_that_run(world):
    victim = world.files["blk"]
    victim.unlink()
    victim.mkdir()
    lines = clean(run_status(env_for(), stdin_for(world.a)))
    assert not any(ln.startswith("ck blk ") for ln in lines)
    assert demo_line(lines) is not None and len(lines) == 3


def test_84_a_progress_file_replaced_by_a_fifo_never_hangs_the_status_line(world):
    victim = world.files["blk"]
    victim.unlink()
    os.mkfifo(victim)
    assert stat.S_ISFIFO(victim.stat().st_mode)
    lines = clean(run_status(env_for(), stdin_for(world.a)))  # test-level timeout: pytest.fail after TIMEOUT_S
    assert not any(ln.startswith("ck blk ") for ln in lines)
    assert demo_line(lines) is not None


@needs_user
def test_84_an_unreadable_progress_file_drops_only_that_run(world):
    victim = world.files["wait"]
    victim.chmod(0)
    try:
        lines = clean(run_status(env_for(), stdin_for(world.a)))
    finally:
        victim.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert not any(ln.startswith("ck wait ") for ln in lines)
    assert len(lines) == 3


def test_84_a_truncated_progress_file_is_harmless(world):
    victim = world.files["demo"]
    data = victim.read_bytes()
    victim.write_bytes(data[: len(data) // 2 + 3])
    clean(run_status(env_for(), stdin_for(world.a)))


def test_84_a_truncated_cache_is_harmless(world):
    path = cache_path(world, "demo")
    path.write_bytes(path.read_bytes()[:40])
    lines = clean(run_status(env_for(), stdin_for(world.a)))
    assert len(lines) in (3, 4)
    assert not any("{" in ln for ln in lines)


@pytest.mark.parametrize("body", ['{"schema": "ck-eta/999", "finish": 5, "cost": "much"}', '{"schema": "other/1"}', "[]", "null", '"text"',
                                  '{"finish": {"p50": 7}, "cost": {"p50": "NaN"}}', '{"cost": {"p50": -3}}', ""])
def test_84_a_cache_with_a_different_schema_is_harmless(world, body):
    cache_path(world, "demo").write_text(body)
    lines = clean(run_status(env_for(), stdin_for(world.a)))
    assert len(lines) in (3, 4)
    line = demo_line(lines)
    assert line is None or line.startswith("ck demo ")


def test_84_no_active_run_prints_nothing_even_with_hostile_input(tmp_path):
    repo = h.eta_spec(tmp_path / "empty", [h.TaskDef(1)])
    for payload in ("", "garbage", b"\xff\xfe", "{" * 5000, json.dumps({"workspace": {"current_dir": str(repo)}})):
        done = run_status(env_for(), payload, cwd=repo)
        assert clean(done) == []
        assert done.stdout == b""
