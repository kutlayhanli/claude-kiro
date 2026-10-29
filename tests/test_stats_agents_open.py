"""Open agent-mode manifests: `ck doctor` and `ck stats collect --all` (TC-35 to TC-38).

Every test runs the real CLI as a subprocess over a fake Claude home, a real store and real git repos. The clock is
never faked: a manifest's age is set by backdating its `startTime` by whole hours (7 h is stale, 5 h is young).
Project modules are not imported at all.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

import pytest

from stats_helpers import (
    CONFIDENTIAL,
    AgentSpec,
    FakeClaudeHome,
    FakeSession,
    ToolAgentSpec,
    backdate_manifest,
    begin,
    ck_sub,
    described,
    fresh_env,
    iso_ms,
    land,
    load_record,
    set_manifest,
    spec_repo,
    stats,
    store_files,
    to_ms,
)

pytestmark = pytest.mark.usefixtures("no_network")


@pytest.fixture
def env():
    with fresh_env() as e:
        yield e


@dataclass
class W:
    env: object
    repo: Path
    sess: FakeSession
    spec: str
    into: str


def world(env, spec: str = "demo", into: str = "integrate/demo") -> W:
    """A spec repo with every task landed, initialised for ck (so `ck doctor` has a project), and a fake session."""
    repo = spec_repo(env.root / "repo", spec, into, tasks=3)
    for n in (1, 2, 3):
        land(repo, spec, into, n)
    init = ck_sub("init", "--no-allow-workflow", cwd=repo, env=env)
    assert init.exit_code == 0, init.output
    sess = FakeClaudeHome(env.claude).session(cwd=repo, git_branch=into)
    return W(env, repo, sess, spec, into)


def open_run(w: W, backdate_hours: Optional[float] = None):
    rid, manifest = begin(w.env, w.repo, "implement", w.spec, w.into)
    if backdate_hours is not None:
        backdate_manifest(manifest, backdate_hours)
    return rid, manifest


def start_ms(manifest: Path) -> int:
    return to_ms(json.loads(Path(manifest).read_text())["startTime"])


def add_marked(w: W, rid: str, manifest: Path, rests: Sequence[str] = ("implement task 1", "review task 1")) -> None:
    specs = [ToolAgentSpec(described(rid, r), agent_type="spec:reviewer" if r.startswith("review") else "spec:implementer") for r in rests]
    w.sess.tool_agents(specs, start=iso_ms(start_ms(manifest) + 5_000))


def doctor(w: W):
    return ck_sub("doctor", cwd=w.repo, env=w.env)


def collect_all(w: W, *extra: str):
    return stats("collect", "--all", *extra, env=w.env, cwd=w.repo)


def count(output: str, label: str) -> Optional[int]:
    m = re.search(rf"^\s*{re.escape(label)}:\s*(\d+)\s*$", output, re.M)
    return int(m.group(1)) if m else None


def open_runs_reported(output: str) -> int:
    m = re.search(r"(\d+) agent-mode run", output)
    return int(m.group(1)) if m else 0


def record_names(env) -> List[str]:
    d = env.xdg / "claude-kiro" / "runs"
    return sorted(p.name for p in d.glob("*.json")) if d.exists() else []


def open_manifest(env, rid: str) -> Path:
    return env.xdg / "claude-kiro" / "runs-open" / f"{rid}.json"


def closed_manifest(env, rid: str) -> Path:
    return env.xdg / "claude-kiro" / "runs-closed" / f"{rid}.json"


def workflow_run(w: W) -> str:
    run = w.sess.implement_run(args={"spec": w.spec, "root": str(w.repo)})
    return run.run_id


# --------------------------------------------------------------------------- TC-35


def test_tc35_doctor_counts_stale_open_runs_without_naming_them(env):
    """TC-35 (6.1, 6.2)."""
    spec, into = "zephyr-payroll", "integrate/zephyr-payroll"
    w = world(env, spec, into)
    for hours in (7, 7, 5):
        _, manifest = open_run(w, hours)
        set_manifest(manifest, root="/home/dana/acme-ledger/src/ledger.py")
    out = doctor(w)
    assert open_runs_reported(out.output) == 2, out.output
    assert "ck stats collect --all" in out.output
    for term in CONFIDENTIAL:
        assert term not in out.output, term


def test_tc35_doctor_is_quiet_with_only_a_young_open_run(env):
    """TC-35 (6.2): the 5 h manifest alone gives no such warning."""
    w = world(env)
    open_run(w, 5)
    out = doctor(w)
    assert open_runs_reported(out.output) == 0, out.output
    assert "agent-mode" not in out.output
    assert "ck stats collect --all" not in out.output


# --------------------------------------------------------------------------- TC-36


def test_tc36_collect_all_closes_stale_manifests_and_leaves_young_ones(env):
    """TC-36 (6.3, 6.4, 6.7)."""
    w = world(env)
    wf_id = workflow_run(w)
    stale_id, stale = open_run(w, 7)
    young_id, young = open_run(w, 5)
    add_marked(w, stale_id, stale)
    add_marked(w, young_id, young)
    ghost = "ag_deadbeef"
    w.sess.tool_agents([ToolAgentSpec(described(ghost, "implement task 2"))], start=iso_ms(start_ms(stale) + 60_000))
    w.sess.tool_agents([ToolAgentSpec("implement demo: task 3"), ToolAgentSpec("fix demo: task 3 red", agent_type="spec:fixer")],
                       start=iso_ms(start_ms(stale) + 120_000))

    out = collect_all(w)
    assert out.exit_code == 0, out.output
    assert count(out.output, "collected") == 2, out.output
    assert count(out.output, "unfinished") == 1, out.output
    assert count(out.output, "failed") == 0, out.output

    assert record_names(env) == sorted([f"{wf_id}.json", f"{stale_id}.json"])
    rec = load_record(env.xdg, stale_id)
    assert rec["source"] == "agents" and rec["partial"] is True
    assert closed_manifest(env, stale_id).exists() and not open_manifest(env, stale_id).exists()
    assert open_manifest(env, young_id).exists() and not closed_manifest(env, young_id).exists()
    assert not (env.xdg / "claude-kiro" / "runs" / f"{ghost}.json").exists()

    again = collect_all(w, "--json")
    assert again.exit_code == 0, again.output
    data = json.loads(again.stdout)
    assert data["collected"] == 0 and data["failed"] == 0 and data["unfinished"] == 1
    assert data["already_had"] >= 1
    assert record_names(env) == sorted([f"{wf_id}.json", f"{stale_id}.json"])

    text = collect_all(w)
    assert re.search(r"^\s*already had:\s*\d+\s*$", text.output, re.M), text.output


# --------------------------------------------------------------------------- TC-37


def test_tc37_stale_manifest_without_marked_agents_is_abandoned_once(env):
    """TC-37 (6.5)."""
    w = world(env)
    wf_id = workflow_run(w)
    rid, manifest = open_run(w, 7)

    out = collect_all(w)
    assert out.exit_code == 0, out.output
    assert out.output.count(rid) == 1, out.output
    assert "abandoned" in out.output and "no agents carry the marker" in out.output
    assert count(out.output, "abandoned") == 1, out.output
    assert count(out.output, "failed") == 0, out.output
    assert record_names(env) == [f"{wf_id}.json"]
    assert closed_manifest(env, rid).exists() and not open_manifest(env, rid).exists()

    second = collect_all(w)
    assert second.exit_code == 0, second.output
    assert rid not in second.output and "abandoned" not in second.output
    after = doctor(w)
    assert rid not in after.output
    assert open_runs_reported(after.output) == 0, after.output
    assert "agent-mode" not in after.output


# --------------------------------------------------------------------------- TC-38


def test_tc38_unreadable_manifests_fail_quietly_and_doctor_counts_them(env):
    """TC-38 (6.6, 3.3)."""
    w = world(env, "zephyr-payroll", "integrate/zephyr-payroll")
    bad_json_id, bad_json = open_run(w)
    bad_list_id, bad_list = open_run(w)
    add_marked(w, bad_json_id, bad_json)
    bad_json.write_text('{"spec": "zephyr-payroll"')
    bad_list.write_text('["zephyr-payroll"]')

    out = collect_all(w)
    assert out.exit_code == 1, out.output
    assert count(out.output, "failed") == 2, out.output
    assert "zephyr-payroll" not in out.output

    doc = doctor(w)
    assert re.search(r"\b2 agent-mode run manifest", doc.output), doc.output
    assert "could not be read" in doc.output
    assert "zephyr-payroll" not in doc.output

    before = bad_json.read_bytes()
    one = stats("collect", "--agents", bad_json_id, env=w.env, cwd=w.repo)
    assert one.exit_code == 1, one.output
    assert "run manifest not recognised" in one.output
    assert "zephyr-payroll" not in one.output
    assert record_names(env) == []
    assert bad_json.read_bytes() == before

    auto = stats("collect", "--agents", bad_json_id, "--auto", env=w.env, cwd=w.repo)
    assert auto.exit_code == 0, auto.output
    assert len([ln for ln in auto.output.splitlines() if ln.strip()]) == 1, auto.output
    assert "run manifest not recognised" in auto.output
    assert "zephyr-payroll" not in auto.output
    assert record_names(env) == []
    assert bad_json.read_bytes() == before
    assert store_files(env.xdg)  # the store holds the manifests only, no record
