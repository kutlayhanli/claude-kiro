"""`ck stats begin`: the run manifest an agent-mode run opens (TC-1 to TC-6, TC-59).

Every test runs the real CLI as a subprocess over a real git repo and a fresh `XDG_DATA_HOME`; project modules are
imported inside test bodies only.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from stats_helpers import (
    ToolAgentSpec,
    FakeClaudeHome,
    ck_sub,
    described,
    fresh_env,
    git,
    git_project,
    parse_iso,
    read_only,
    spec_repo,
    stats,
    store_files,
    to_ms,
)

pytestmark = pytest.mark.usefixtures("no_network")

RUN_ID = re.compile(r"^ag_[0-9a-f]{8}$")
CONFIDENTIAL_LABEL = "acme-ledger"
CONFIDENTIAL_DOMAIN = "acme-corp.com"


@pytest.fixture
def env():
    with fresh_env() as e:
        yield e


def mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def net_log_empty(log: Path) -> bool:
    return not log.exists() or log.read_text().strip() == ""


def demo_repo(env, name: str = "repo") -> Path:
    """A `spec_repo` (spec demo, target integrate/demo) with a subdirectory, checked out on `integrate/demo`."""
    repo = spec_repo(env.root / name)
    git(repo, "checkout", "-q", "integrate/demo")
    (repo / "sub" / "deeper").mkdir(parents=True)
    return repo


def begin_json(env, cwd: Path, *args: str):
    r = stats("begin", "--kind", "implement", "--spec", "demo", *args, "--json", env=env, cwd=cwd)
    return r


# --------------------------------------------------------------------------- TC-1


def test_tc1_begin_json_manifest_shape(env):
    """TC-1."""
    from importlib.metadata import version

    repo = demo_repo(env)
    # Plant confidential configuration that must never reach the manifest.
    for key, value in (("run.machine_label", CONFIDENTIAL_LABEL), ("run.allowed_domains", CONFIDENTIAL_DOMAIN)):
        r = ck_sub("agents", "set", key, value, env=env, cwd=repo)
        assert r.exit_code == 0, r.output
    sub = repo / "sub" / "deeper"
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    r = begin_json(env, sub)
    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert set(data) == {"run_id", "manifest"}
    assert RUN_ID.match(data["run_id"])
    path = Path(data["manifest"])
    assert path.is_file()
    m = json.loads(path.read_text())
    assert m["kind"] == "implement" and m["spec"] == "demo"
    assert Path(m["root"]).resolve() == repo.resolve()
    assert m["into"] == "integrate/demo"
    assert before <= parse_iso(m["startTime"]) <= after
    assert m["ckVersion"] == version("claude-kiro")
    assert m["sessionId"] is None or isinstance(m["sessionId"], str)
    assert m["doneAtStart"] == []
    assert m["runId"] == data["run_id"]

    roles = json.loads(ck_sub("agents", "--json", env=env, cwd=sub).stdout)["roles"]

    def proj(role: dict) -> dict:
        return {"model": role.get("model"), "effort": role.get("effort"), "enabled": role.get("enabled")}

    expected = {name: proj(roles[name]) for name in ("implementer", "test_writer", "reviewer", "fixer", "resolver", "orchestrator")}
    rev = roles["reviewer"]
    expected["reviewer_risky"] = {"model": rev.get("risky_model"), "effort": rev.get("risky_effort"), "enabled": rev.get("enabled")}
    assert m["agents"] == expected
    for banned in ("allowed_domains", "machine_label", "sources", "runtime"):
        assert banned not in m
    raw = path.read_bytes()
    assert CONFIDENTIAL_LABEL.encode() not in raw
    assert CONFIDENTIAL_DOMAIN.encode() not in raw
    assert mode(path) == 0o600
    assert mode(path.parent) == 0o700


def test_tc1_done_at_start_is_null_without_a_tasks_file(env):
    """TC-1 (1.2): a plain project has no specs/demo/tasks.md on the target."""
    proj = git_project(env.root, None, name="plain")
    r = begin_json(env, proj)
    assert r.exit_code == 0, r.output
    m = json.loads(Path(json.loads(r.stdout)["manifest"]).read_text())
    assert m["doneAtStart"] is None
    assert m["into"] == "main"


# --------------------------------------------------------------------------- TC-2


def test_tc2_text_mode_into_and_kind(env):
    """TC-2."""
    repo = demo_repo(env)
    r = stats("begin", "--kind", "review", "--spec", "demo", "--into", "main", env=env, cwd=repo)
    assert r.exit_code == 0, r.output
    run_id = r.stdout.splitlines()[0].strip()
    assert RUN_ID.match(run_id)
    files = [p for p in store_files(env.xdg) if p.name == f"{run_id}.json"]
    assert len(files) == 1
    m = json.loads(files[0].read_text())
    assert m["into"] == "main" and m["kind"] == "review"


# --------------------------------------------------------------------------- TC-3


def test_tc3_begin_skips_every_taken_id(env):
    """TC-3."""
    repo = demo_repo(env)
    base = env.xdg / "claude-kiro"
    (base / "runs").mkdir(parents=True)
    (base / "runs-open").mkdir()
    (base / "runs-closed").mkdir()
    (base / "runs" / "ag_0a1b2c3d.json").write_text("{}")
    (base / "runs-open" / "ag_0a1b2c3e.json").write_text("{}")
    (base / "runs-closed" / "ag_0a1b2c3f.json").write_text("{}")
    e = {**env, "CK_TEST_RUN_ID_DRAWS": "0a1b2c3d,0a1b2c3e,0a1b2c3f,0a1b2c40"}
    r = stats("begin", "--kind", "implement", "--spec", "demo", env=e, cwd=repo)
    assert r.exit_code == 0, r.output
    assert r.stdout.splitlines()[0].strip() == "ag_0a1b2c40"


def test_tc3_thirty_runs_give_thirty_distinct_ids(env):
    """TC-3 (without the hook)."""
    repo = demo_repo(env)
    ids = set()
    for _ in range(30):
        r = stats("begin", "--kind", "implement", "--spec", "demo", env=env, cwd=repo)
        assert r.exit_code == 0, r.output
        ids.add(r.stdout.splitlines()[0].strip())
    assert len(ids) == 30 and all(RUN_ID.match(i) for i in ids)


# --------------------------------------------------------------------------- TC-4

BAD_ARGS = [
    pytest.param(["--spec", "demo"], id="no-kind"),
    pytest.param(["--kind", "plan", "--spec", "demo"], id="kind-plan"),
    pytest.param(["--kind", "Implement", "--spec", "demo"], id="kind-capitalised"),
    pytest.param(["--kind", "", "--spec", "demo"], id="kind-empty"),
    pytest.param(["--kind", "implement"], id="no-spec"),
    pytest.param(["--kind", "implement", "--spec", ""], id="spec-empty"),
]


@pytest.mark.parametrize("args", BAD_ARGS)
def test_tc4_bad_kind_or_spec_is_a_usage_error(env, args):
    """TC-4."""
    repo = demo_repo(env)
    r = stats("begin", *args, env=env, cwd=repo)
    assert r.exit_code == 2, r.output
    for kind in ("implement", "create", "review"):
        assert kind in r.output
    assert store_files(env.xdg) == []


# --------------------------------------------------------------------------- TC-5


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.parametrize("as_json", [False, True])
def test_tc5_unwritable_store_still_prints_an_id(env, as_json):
    """TC-5."""
    repo = demo_repo(env)
    store = env.xdg / "claude-kiro"
    store.mkdir()
    args = ["begin", "--kind", "implement", "--spec", "demo", *(["--json"] if as_json else [])]
    with read_only(store):
        r = stats(*args, env=env, cwd=repo)
    assert r.exit_code == 0, r.output
    if as_json:
        data = json.loads(r.stdout)
        assert RUN_ID.match(data["run_id"]) and data["manifest"] is None
    else:
        assert RUN_ID.match(r.stdout.splitlines()[0].strip())
    assert "without" in r.stderr and "manifest" in r.stderr
    assert store_files(env.xdg) == []


# --------------------------------------------------------------------------- TC-6


def test_tc6_begin_is_fast_and_offline(env, no_network):
    """TC-6."""
    repo = demo_repo(env)
    ck_sub("--version", env=env, cwd=repo)  # warm the import caches

    def timed(*args: str) -> float:
        t0 = time.monotonic()
        r = ck_sub(*args, env=env, cwd=repo)
        assert r.exit_code == 0, r.output
        return time.monotonic() - t0

    baseline = min(timed("--version") for _ in range(3))
    begin_time = min(timed("stats", "begin", "--kind", "implement", "--spec", "demo") for _ in range(3))
    assert begin_time - baseline < 1.0, (begin_time, baseline)
    assert net_log_empty(no_network)


# --------------------------------------------------------------------------- TC-59


def test_tc59_private_modes_for_manifest_and_record(env):
    """TC-59."""
    repo = demo_repo(env)
    assert not (env.xdg / "claude-kiro").exists()
    r = begin_json(env, repo)
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    manifest = Path(data["manifest"])
    assert mode(manifest) == 0o600
    d = manifest.parent
    while d != env.xdg:
        assert mode(d) == 0o700, d
        d = d.parent
    # A marked agent, then the record.
    home = FakeClaudeHome(env.claude)
    sess = home.session(cwd=repo)
    start = to_ms(json.loads(manifest.read_text())["startTime"]) + 5_000
    from stats_helpers import iso_ms

    sess.tool_agents([ToolAgentSpec(described(data["run_id"], "implement task 1"), duration_ms=30_000)], start=iso_ms(start))
    c = stats("collect", "--agents", data["run_id"], env=env, cwd=repo)
    assert c.exit_code == 0, c.output
    record = env.xdg / "claude-kiro" / "runs" / f"{data['run_id']}.json"
    assert record.is_file() and mode(record) == 0o600
    assert mode(record.parent) == 0o700
