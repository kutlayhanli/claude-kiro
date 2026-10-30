"""Agent-mode analytics hardening (run-followups D7): TC-62 to TC-72.

Every test runs the real `ck stats collect` as a subprocess over a fake Claude home, a real store, real permissions and
real collect.log bytes. Project modules are not imported. TC-70 and TC-72 are regression guards that pass before and
after the fix; TC-71 observes `open` audit events through tests/openaudit/sitecustomize.py (it only observes).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterator, List

import pytest

import test_stats_agents_backfill as B
import test_stats_agents_collect as C
import test_stats_agents_open as O
from stats_helpers import AgentSpec, FakeClaudeHome, ToolAgentSpec, fresh_env, land, load_record, spec_repo, stats

pytestmark = pytest.mark.usefixtures("no_network")

OPENAUDIT = Path(__file__).parent / "openaudit"
needs_user = C.needs_user
unreadable = C.unreadable


@pytest.fixture
def env():
    with fresh_env() as e:
        yield e


# --------------------------------------------------------------------------- helpers


def data_dir(env) -> Path:
    return env.xdg / "claude-kiro"


def snapshot(env) -> Dict[str, bytes]:
    """Relative path -> bytes of every file in the data directory (call it with the permissions in place)."""
    root = data_dir(env)
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()} if root.exists() else {}


def pending_store(env):
    """A store holding one backfill record (session 1) and a second session whose group is still to be written."""
    h = B.Home(env)
    s1 = h.session()
    s1.tool_agents([B.impl("demo", 1, B.t(9, 0)), B.impl("demo", 2, B.t(9, 15))])
    first = B.backfill(env)
    assert first.exit_code == 0, first.output
    assert len(B.records(env)) == 1
    s2 = h.session()
    s2.tool_agents([B.impl("ledger", 1, B.t(9, 0, day=3))])
    return s1, s2


@contextlib.contextmanager
def broken_store(env, how: str) -> Iterator[None]:
    runs = data_dir(env) / "runs"
    if how == "runs_chmod":
        with unreadable(runs):
            yield
    elif how == "data_chmod":
        with unreadable(data_dir(env)):
            yield
    elif how == "runs_file":
        aside = env.root / "runs-aside"
        runs.rename(aside)
        runs.write_text("not a directory")
        try:
            yield
        finally:
            runs.unlink()
            aside.rename(runs)
    else:  # pragma: no cover
        raise AssertionError(how)


BREAKS = [
    pytest.param("runs_chmod", marks=needs_user),
    "runs_file",
    pytest.param("data_chmod", marks=needs_user),
]


# --------------------------------------------------------------------------- TC-62, TC-63


@pytest.mark.parametrize("how", BREAKS)
def test_tc62_an_unreadable_store_refuses_backfill_and_writes_nothing(env, how):
    """TC-62 (9.1)."""
    pending_store(env)
    before = snapshot(env)
    with broken_store(env, how):
        r = B.backfill(env)
    assert r.exit_code == 1, r.output
    assert "cannot check backfill overlaps: record store" in r.output, r.output
    assert "unreadable" in r.output, r.output
    assert str(data_dir(env)) in r.output, r.output
    assert snapshot(env) == before


@pytest.mark.parametrize("how", BREAKS)
def test_tc63_dry_run_lists_the_groups_and_marks_overlaps_unchecked_text(env, how):
    """TC-63 (9.2)."""
    _s1, s2 = pending_store(env)
    before = snapshot(env)
    with broken_store(env, how):
        r = B.backfill(env, "--dry-run")
    assert r.exit_code == 1, r.output
    assert f"session {s2.session_id}" in r.output, r.output
    assert "overlaps not checked" in r.output, r.output
    assert snapshot(env) == before


@pytest.mark.parametrize("how", BREAKS)
def test_tc63_dry_run_json_marks_the_overlap_check_unavailable(env, how):
    """TC-63 (9.2)."""
    _s1, s2 = pending_store(env)
    before = snapshot(env)
    with broken_store(env, how):
        r = B.backfill(env, "--dry-run", "--json")
    assert r.exit_code == 1, r.output
    data = json.loads(r.stdout)
    assert data["overlapCheck"] == "unavailable", data
    assert s2.session_id in [g["session"] for g in data["groups"]], data
    assert snapshot(env) == before


# --------------------------------------------------------------------------- TC-64


def _record_text(env) -> str:
    (p,) = sorted((data_dir(env) / "runs").glob("*.json"))
    return p.read_text()


MALFORMED = {
    "invalid_json": lambda text: "{this is not json",
    "json_list": lambda text: "[1, 2, 3]",
    "wrong_schema": lambda text: json.dumps({"unexpected": True}),
    "empty": lambda text: "",
    "truncated": lambda text: text[: len(text) // 2],
}
BAD_NAME = "broken-record.json"


def narrow_gap_store(env):
    h = B.Home(env)
    s = h.session()
    s.tool_agents([B.impl("demo", 1, B.t(9, 0), 15), B.impl("demo", 2, B.t(9, 15), 15),
                   B.impl("demo", 3, B.t(10, 15), 15), B.impl("demo", 4, B.t(10, 30), 15)])
    first = B.backfill(env)
    assert first.exit_code == 0, first.output
    assert len(B.records(env)) == 1
    return s


def test_tc64_control_without_a_malformed_file_exits_0(env):
    """TC-64 (9.3): the same scenario without the malformed file exits 0 as today."""
    narrow_gap_store(env)
    before = snapshot(env)
    r = B.backfill(env, "--gap", "30")
    assert r.exit_code == 0, r.output
    assert snapshot(env) == before


@pytest.mark.parametrize("kind", sorted(MALFORMED))
def test_tc64_a_malformed_record_refuses_backfill_and_names_the_file(env, kind):
    """TC-64 (9.3)."""
    narrow_gap_store(env)
    (data_dir(env) / "runs" / BAD_NAME).write_text(MALFORMED[kind](_record_text(env)))
    before = snapshot(env)
    r = B.backfill(env, "--gap", "30")
    assert r.exit_code == 1, r.output
    assert "malformed record" in r.output and BAD_NAME in r.output, r.output
    assert snapshot(env) == before


@pytest.mark.parametrize("kind", sorted(MALFORMED))
def test_tc64_dry_run_lists_groups_unchecked_and_names_the_file(env, kind):
    """TC-64 (9.3), --dry-run as text and as JSON."""
    s = narrow_gap_store(env)
    (data_dir(env) / "runs" / BAD_NAME).write_text(MALFORMED[kind](_record_text(env)))
    before = snapshot(env)

    text = B.backfill(env, "--gap", "30", "--dry-run")
    assert text.exit_code == 1, text.output
    assert text.output.count(f"session {s.session_id}") == 2, text.output
    assert "overlaps not checked" in text.output and BAD_NAME in text.output, text.output

    js = B.backfill(env, "--gap", "30", "--dry-run", "--json")
    assert js.exit_code == 1, js.output
    data = json.loads(js.stdout)
    assert len(data["groups"]) == 2, data
    assert BAD_NAME in json.dumps(data["malformedRecords"]), data
    assert snapshot(env) == before


def test_tc64_json_backfill_reports_the_malformed_record(env):
    """TC-64 (9.3), write mode with --json."""
    narrow_gap_store(env)
    (data_dir(env) / "runs" / BAD_NAME).write_text("{nope")
    before = snapshot(env)
    r = B.backfill(env, "--gap", "30", "--json")
    assert r.exit_code == 1, r.output
    assert BAD_NAME in r.output, r.output
    assert snapshot(env) == before


# --------------------------------------------------------------------------- TC-65


@pytest.mark.parametrize("how", ["deleted", pytest.param("chmod000", marks=needs_user)])
def test_tc65_a_missing_or_unreadable_session_transcript_leaves_the_outcome_unknown(env, how):
    """TC-65 (9.4)."""
    repo = spec_repo(env.root / "repo", "demo", "integrate/demo", tasks=3)
    now = datetime.now(timezone.utc)
    start = int((now - timedelta(minutes=40)).timestamp() * 1000)
    s = B.Home(env).session(cwd=repo, branch="integrate/demo")
    s.tool_agents([B.impl("demo", n, start + (n - 1) * 600_000, 9) for n in (1, 2, 3)])
    for n in (1, 2, 3):
        land(repo, "demo", "integrate/demo", n)

    ctx = contextlib.nullcontext()
    if how == "deleted":
        s.transcript_path.unlink()
    else:
        ctx = unreadable(s.transcript_path)
    with ctx:
        r = B.backfill(env, "--json")
    assert r.exit_code == 0, r.output
    recs = B.records(env)
    assert len(recs) == 1, r.output
    (rec,) = recs.values()
    assert rec["outcome"] is None, rec["outcome"]
    assert {"agentId": None, "what": "outcome"} in [
        {"agentId": m.get("agentId"), "what": m.get("what")} for m in rec["missing"]], rec["missing"]


# --------------------------------------------------------------------------- TC-66


@needs_user
def test_tc66_sandboxed_agents_json_collect_prints_one_json_failure(env):
    """TC-66 (10.1)."""
    w = C.world(env)
    C.init_project(env, w.repo)
    rid, mp = C.open_run(w)
    C.add_agents(w, mp, C.rests(rid, ["implement task 1"]))
    before = snapshot(env)
    with unreadable(env.claude / "projects"):
        r = C.collect_agents(env, rid, cwd=w.repo)
    assert r.exit_code == 1, r.output
    lines = r.stdout.splitlines()
    assert len(lines) == 1, r.stdout
    data = json.loads(lines[0])
    assert data["run_id"] == rid and data["result"] == "failed", data
    assert "sandbox" in data["error"].lower(), data
    assert snapshot(env) == before


# --------------------------------------------------------------------------- TC-67, TC-68


def collect_log(env) -> List[str]:
    p = data_dir(env) / "collect.log"
    return p.read_text().splitlines() if p.exists() else []


@pytest.mark.parametrize("rid,reason", [("bogus", "hex"), ("ag_0a1b2c3d", "not found")])
def test_tc67_auto_logs_one_line_and_prints_the_retry_hint(env, rid, reason):
    """TC-67 (10.2)."""
    FakeClaudeHome(env.claude)
    r = stats("collect", "--agents", rid, "--auto", env=env, cwd=env.root)
    assert r.exit_code == 0, r.output
    assert f"retry: ck stats collect --agents {rid}" in r.stdout, r.output
    lines = collect_log(env)
    assert len(lines) == 1, lines
    assert rid in lines[0] and reason in lines[0].lower(), lines


def test_tc67_a_long_id_with_a_newline_is_logged_as_one_line_cut_to_64_characters(env):
    """TC-67 (10.2)."""
    FakeClaudeHome(env.claude)
    rid = "bad\n" + "y" * 196
    assert len(rid) == 200
    r = stats("collect", "--agents", rid, "--auto", env=env, cwd=env.root)
    assert r.exit_code == 0, r.output
    lines = collect_log(env)
    assert len(lines) == 1, lines
    assert "y" * 60 in lines[0] and "y" * 61 not in lines[0], lines


def test_tc68_a_malformed_id_without_auto_is_an_error_as_text(env):
    """TC-68 (10.3)."""
    before = snapshot(env)
    r = stats("collect", "--agents", "bogus", env=env, cwd=env.root)
    assert r.exit_code in (1, 2) and r.exit_code != 0, r.output
    assert "RUN_ID must look like ag_" in r.output
    assert snapshot(env) == before


def test_tc68_a_malformed_id_without_auto_is_one_json_error_with_json(env):
    """TC-68 (10.3)."""
    r = stats("collect", "--agents", "bogus", "--json", env=env, cwd=env.root)
    assert r.exit_code in (1, 2) and r.exit_code != 0, r.output
    data = json.loads(r.stdout)
    assert "RUN_ID must look like ag_" in json.dumps(data), data
    assert len(r.stdout.splitlines()) == 1, r.stdout


# --------------------------------------------------------------------------- TC-69


@needs_user
@pytest.mark.parametrize("as_json", [False, True])
def test_tc69_a_sweep_failure_is_reported_and_workflow_runs_still_are(env, as_json):
    """TC-69 (11.1)."""
    s = FakeClaudeHome(env.claude).session()
    s.implement_run(args={"spec": "demo", "root": str(s.cwd)})
    open_dir = data_dir(env) / "runs-open"
    open_dir.mkdir(parents=True)
    with unreadable(open_dir):
        r = stats("collect", "--all", *(["--json"] if as_json else []), env=env, cwd=env.root)
    assert r.exit_code == 1, r.output
    if as_json:
        data = json.loads(r.stdout)
        assert any(f["run_id"] == "open-manifests" and f["reason"] for f in data["failures"]), data
        assert data["failed"] >= 1 and data["collected"] == 1, data
    else:
        assert "open-manifests" in r.output, r.output
        assert B.count(r.output, "collected") == 1, r.output
        assert (B.count(r.output, "failed") or 0) >= 1, r.output


# --------------------------------------------------------------------------- TC-70 (regression guard)


def test_tc70_collect_all_writes_each_record_by_its_source(env):
    """TC-70 (11.2): passes before and after."""
    w = O.world(env)
    wf_id = O.workflow_run(w)
    rid, manifest = O.open_run(w, 7)
    O.add_marked(w, rid, manifest)
    r = O.collect_all(w)
    assert r.exit_code == 0, r.output
    assert load_record(env.xdg, wf_id).get("source") != "agents"
    assert load_record(env.xdg, rid)["source"] == "agents"
    assert O.closed_manifest(env, rid).exists() and not O.open_manifest(env, rid).exists()


# --------------------------------------------------------------------------- TC-71


def _opens(log: Path) -> Counter:
    return Counter(log.read_text().splitlines()) if log.exists() else Counter()


def test_tc71_agent_mode_reads_each_transcript_as_often_as_a_workflow_collect(env):
    """TC-71 (11.3): the open count of every transcript equals its count in the equivalent workflow collect."""
    n = 6
    w = O.world(env)
    run = w.sess.implement_run(args={"spec": w.spec, "root": str(w.repo)},
                               agents=[AgentSpec(f"task {i}", phase="Wave 1") for i in range(n)])
    rid, manifest = O.open_run(w)
    O.add_marked(w, rid, manifest, rests=[f"implement task {i}" for i in range(n)])

    def run_with_log(name: str, *args: str) -> Counter:
        log = env.root / f"open-{name}.log"
        e = {**env, "PYTHONPATH": str(OPENAUDIT), "CK_TEST_OPEN_LOG": str(log)}
        r = stats("collect", *args, env=e, cwd=w.repo)
        assert r.exit_code == 0, r.output
        return _opens(log)

    wf = run_with_log("workflow", "--run", run.run_id)
    ag = run_with_log("agents", "--agents", rid)
    assert len(wf) == n, wf
    assert len(ag) == n, ag
    assert sorted(ag.values()) == sorted(wf.values()), (dict(ag), dict(wf))


# --------------------------------------------------------------------------- TC-72 (regression guard)


def test_tc72_records_are_byte_identical_across_fresh_stores_and_recollect_is_unchanged(env):
    """TC-72 (11.4): passes before and after."""
    w = O.world(env)
    wf_id = O.workflow_run(w)
    rid, manifest = O.open_run(w)
    O.add_marked(w, rid, manifest)
    store_b = env.root / "xdg-b"
    (store_b / "claude-kiro" / "runs-open").mkdir(parents=True)
    shutil.copy(manifest, store_b / "claude-kiro" / "runs-open" / manifest.name)
    env_b = {**env, "XDG_DATA_HOME": str(store_b)}

    for e in (env, env_b):
        for args in (("--run", wf_id), ("--agents", rid)):
            r = stats("collect", *args, "--json", env=e, cwd=w.repo)
            assert r.exit_code == 0, r.output
    for name in (wf_id, rid):
        a = (data_dir(env) / "runs" / f"{name}.json").read_bytes()
        b = (store_b / "claude-kiro" / "runs" / f"{name}.json").read_bytes()
        assert a == b, name

    again = stats("collect", "--run", wf_id, "--json", env=env, cwd=w.repo)
    assert json.loads(again.stdout)["result"] == "unchanged", again.output
    again = stats("collect", "--agents", rid, "--json", env=env, cwd=w.repo)
    assert json.loads(again.stdout)["result"] == "unchanged", again.output
