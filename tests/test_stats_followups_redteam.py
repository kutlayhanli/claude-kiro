"""Red team for run-followups 9.1, 9.3 and 10.1 (the SHALL NOT clauses of the D7 hardening).

Working from the requirements only: 9.1 backfill SHALL never treat an unreadable store as empty (so it writes no
records); 9.3 backfill SHALL write no records when a record file cannot be parsed; 10.1 a sandboxed
`collect --agents <id> --json` SHALL NOT print plain text. Every case compares the store bytes before and after.
Permission cases skip as root.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

import pytest

import test_stats_agents_backfill as B
import test_stats_agents_collect as C
from stats_helpers import FakeClaudeHome, ck_sub, fresh_env, stats
from test_stats_followups import BAD_NAME, data_dir, pending_store, snapshot

pytestmark = pytest.mark.usefixtures("no_network")
needs_user = C.needs_user
unreadable = C.unreadable


@pytest.fixture
def env():
    with fresh_env() as e:
        yield e


def backfill_t(env, *args: str, extra_env=None, timeout: float = 60):
    """Backfill with a timeout, so a hang on a FIFO or device fails the test instead of the suite."""
    return ck_sub("stats", "collect", "--agents-backfill", *args, env={**env, **(extra_env or {})}, cwd=env.root, timeout=timeout)


def assert_refused(r, before, env):
    assert r.exit_code == 1, r.output
    assert "Traceback" not in r.output, r.output
    assert snapshot(env) == before


# --------------------------------------------------------------------------- 9.1: unreadable store is not an empty store


@contextlib.contextmanager
def runs_replaced(env, kind: str):
    runs = data_dir(env) / "runs"
    aside = env.root / "runs-aside"
    runs.rename(aside)
    if kind == "dangling":
        runs.symlink_to(env.root / "nowhere")
    elif kind == "to_file":
        target = env.root / "plain-file"
        target.write_text("x")
        runs.symlink_to(target)
    try:
        yield
    finally:
        runs.unlink()
        aside.rename(runs)


@pytest.mark.parametrize("kind", ["dangling", "to_file"])
def test_91_a_runs_symlink_that_does_not_lead_to_a_directory_refuses_backfill(env, kind):
    pending_store(env)
    before = snapshot(env)
    with runs_replaced(env, kind):
        r = backfill_t(env)
    assert_refused(r, before, env)
    assert "cannot check backfill overlaps" in r.output, r.output


def test_91_a_dangling_runs_symlink_with_dry_run_json_is_one_object_and_unchecked(env):
    _s1, s2 = pending_store(env)
    before = snapshot(env)
    with runs_replaced(env, "dangling"):
        r = backfill_t(env, "--dry-run", "--json")
    assert r.exit_code == 1, r.output
    assert len(r.stdout.splitlines()) == 1, r.stdout
    data = json.loads(r.stdout)
    assert data["overlapCheck"] == "unavailable" and s2.session_id in [g["session"] for g in data["groups"]], data
    assert snapshot(env) == before


@needs_user
def test_91_a_runs_directory_that_is_readable_but_not_searchable_refuses_backfill(env):
    pending_store(env)
    before = snapshot(env)
    runs = data_dir(env) / "runs"
    runs.chmod(0o400)
    try:
        r = backfill_t(env)
    finally:
        runs.chmod(0o700)
    assert_refused(r, before, env)


@needs_user
def test_91_an_unreadable_data_home_refuses_backfill_and_writes_nothing(env):
    pending_store(env)
    before = snapshot(env)
    with unreadable(env.xdg):
        r = backfill_t(env)
    assert r.exit_code == 1, r.output
    assert "Traceback" not in r.output, r.output
    assert snapshot(env) == before


def test_91_a_data_home_that_is_dev_null_refuses_backfill(env):
    B.Home(env).session().tool_agents([B.impl("demo", 1, B.t(9, 0))])
    before = snapshot(env)
    r = backfill_t(env, extra_env={"CK_DATA_HOME": "/dev/null"})
    assert_refused(r, before, env)


def test_91_an_unreadable_store_with_json_does_not_print_a_results_object_that_counts_success(env):
    """The refusal may not look like a successful empty run on stdout."""
    pending_store(env)
    before = snapshot(env)
    with runs_replaced(env, "dangling"):
        r = backfill_t(env, "--json")
    assert r.exit_code == 1, r.output
    if r.stdout.strip():
        data = json.loads(r.stdout)
        assert data.get("collected", 0) == 0
    assert snapshot(env) == before


# --------------------------------------------------------------------------- 9.3: an unparsable record blocks the write


def plant(env, name: str = BAD_NAME) -> Path:
    return data_dir(env) / "runs" / name


def test_93_a_record_that_is_a_directory_refuses_backfill(env):
    pending_store(env)
    plant(env).mkdir()
    before = snapshot(env)
    r = backfill_t(env)
    assert_refused(r, before, env)
    assert BAD_NAME in r.output, r.output


def test_93_a_record_that_is_a_fifo_refuses_backfill_instead_of_hanging(env):
    pending_store(env)
    os.mkfifo(plant(env))
    before = snapshot(env)
    r = backfill_t(env, timeout=30)
    assert_refused(r, before, env)
    assert BAD_NAME in r.output, r.output


@needs_user
def test_93_an_unreadable_record_refuses_backfill(env):
    pending_store(env)
    p = plant(env)
    p.write_text("{}")
    before = snapshot(env)
    with unreadable(p):
        r = backfill_t(env)
    assert_refused(r, before, env)
    assert BAD_NAME in r.output, r.output


# ids=: a 200 KB payload in the test id overflows PYTEST_CURRENT_TEST for the ck subprocess (exec arg limit)
@pytest.mark.parametrize("name,payload", [
    ("binary", b"\xff\xfe\x00\x01 not text"),
    ("nul_bytes", b"\x00" * 64),
    ("deeply_nested", b"[" * 200_000),
    ("one_byte_short", None),
    ("own_dry_run_output", None),
], ids=["binary", "nul_bytes", "deeply_nested", "one_byte_short", "own_dry_run_output"])
def test_93_near_miss_and_hostile_record_bytes_refuse_backfill(env, name, payload):
    pending_store(env)
    if name == "one_byte_short":
        (existing,) = sorted((data_dir(env) / "runs").glob("*.json"))
        payload = existing.read_bytes().rstrip()[:-1]
    elif name == "own_dry_run_output":
        payload = backfill_t(env, "--dry-run", "--json").stdout.encode()
    plant(env).write_bytes(payload)
    before = snapshot(env)
    r = backfill_t(env)
    assert_refused(r, before, env)
    assert BAD_NAME in r.output, r.output


# --------------------------------------------------------------------------- 10.1: no plain text from a sandboxed --json collect


def one_json_failure(r, rid):
    assert r.exit_code == 1, r.output
    lines = r.stdout.splitlines()
    assert len(lines) == 1, r.output
    data = json.loads(lines[0])
    assert data["run_id"] == rid and data["result"] == "failed" and "sandbox" in data["error"].lower(), data
    assert "❌" not in r.stdout


@needs_user
def test_101_an_unknown_id_in_a_sandbox_is_still_one_json_failure(env):
    FakeClaudeHome(env.claude)
    before = snapshot(env)
    with unreadable(env.claude / "projects"):
        r = stats("collect", "--agents", "ag_0a1b2c3d", "--json", env=env, cwd=env.root)
    one_json_failure(r, "ag_0a1b2c3d")
    assert snapshot(env) == before


@needs_user
def test_101_an_unreadable_claude_home_in_a_sandbox_is_one_json_failure(env):
    w = C.world(env)
    C.init_project(env, w.repo)
    rid, mp = C.open_run(w)
    C.add_agents(w, mp, C.rests(rid, ["implement task 1"]))
    before = snapshot(env)
    with unreadable(env.claude):
        r = C.collect_agents(env, rid, cwd=w.repo)
    one_json_failure(r, rid)
    assert snapshot(env) == before


@needs_user
def test_101_the_sandbox_failure_is_json_from_any_directory_and_with_several_agents(env):
    w = C.world(env)
    C.init_project(env, w.repo)
    rid, mp = C.open_run(w)
    C.add_agents(w, mp, C.rests(rid, ["implement task 1", "review task 1", "fix task 1 red"]))
    other = env.root / "elsewhere"
    other.mkdir()
    with unreadable(env.claude / "projects"):
        r = C.collect_agents(env, rid, cwd=other)
    one_json_failure(r, rid)


@needs_user
def test_101_one_unreadable_agent_transcript_never_prints_plain_text_with_json(env):
    w = C.world(env)
    C.init_project(env, w.repo)
    rid, mp = C.open_run(w)
    agents = C.add_agents(w, mp, C.rests(rid, ["implement task 1", "review task 1"]))
    with unreadable(agents.transcript(0)):
        r = C.collect_agents(env, rid, cwd=w.repo)
    lines = r.stdout.splitlines()
    assert len(lines) == 1, r.output
    data = json.loads(lines[0])
    assert data["run_id"] == rid
    assert (r.exit_code == 1) == (data["result"] == "failed"), (r.exit_code, data)
