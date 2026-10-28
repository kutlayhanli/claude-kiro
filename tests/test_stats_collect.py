"""Integration tests for `ck stats collect`: the local record of a run (TC-1..TC-17, TC-79, TC-83, contract check).

Every test builds a fake `$CLAUDE_CONFIG_DIR` with `stats_helpers`, runs the real `ck stats collect` as a subprocess and
reads the file under `$XDG_DATA_HOME/claude-kiro/runs/`. Project modules are imported inside test bodies only.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from stats_helpers import (
    AgentSpec,
    FakeClaudeHome,
    TaskSpec,
    ck_sub,
    create_result,
    fresh_env,
    git_project,
    implement_result,
    load_record,
    seeded_home,
    seeded_local_record,
    stats,
    store_files,
    to_ms,
)

pytestmark = pytest.mark.usefixtures("no_network")


# --------------------------------------------------------------------------- helpers


@pytest.fixture
def env():
    with fresh_env() as e:
        yield e


def world(env, remote: Optional[str] = "git@github.com:acme-corp/acme-ledger.git", **session_kw):
    proj = git_project(env.root, remote)
    home = FakeClaudeHome(env.claude)
    sess = home.session(cwd=proj, **session_kw)
    return home, sess, proj


def collect(env, *args: str, cwd: Optional[Path] = None):
    return stats("collect", *args, env=env, cwd=cwd)


def runs_dir(env) -> Path:
    return env.xdg / "claude-kiro" / "runs"


def ck_version() -> str:
    from importlib.metadata import version

    return version("claude-kiro")


def mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def three_model_agents() -> List[AgentSpec]:
    return [
        AgentSpec("task 1", model="claude-sonnet-5-5", phase="Wave 1",
                  requests=[("r1", 1000, 500, 2000, 100), ("r2", 3000, 700, 9000, 400)]),
        AgentSpec("review 1", model="claude-opus-5-5", phase="Wave 1", requests=[("r3", 2500, 900, 4000, 0)]),
        AgentSpec("task 2", model="claude-sonnet-5-5", phase="Wave 2", requests=[("r4", 800, 200, 100, 50)]),
        AgentSpec("land 1 2", model="claude-haiku-5-5", phase="Wave 2", requests=[("r5", 400, 100, 300, 10)]),
    ]


def usd(model: str, i: int, o: int, cr: int, cw: int) -> float:
    from claude_kiro import bench

    return bench.price_usage(model, {"input": i, "output": o, "cacheRead": cr, "cacheCreation": cw})


def priced_figure(rec: dict) -> dict:
    figs = [c for c in rec["costs"] if c.get("scope") == "run"]
    assert len(figs) == 1, rec["costs"]
    return figs[0]


# --------------------------------------------------------------------------- TC-1, TC-2


def test_tc1_collect_writes_exactly_one_record_and_prints_its_path(env):
    """TC-1."""
    _, sess, proj = world(env)
    run = sess.implement_run()
    r = collect(env, "--run", run.run_id, cwd=proj)
    assert r.exit_code == 0, r.output
    files = store_files(env.xdg)
    record = runs_dir(env) / f"{run.run_id}.json"
    assert files == [record]
    assert str(record) in r.stdout
    assert load_record(env.xdg, run.run_id)["runId"] == run.run_id


def test_tc2_default_data_home_is_under_home_local_share(env):
    """TC-2."""
    from stats_helpers import CK

    _, sess, proj = world(env)
    run = sess.implement_run()
    e = {**os.environ, **env}
    e.pop("XDG_DATA_HOME", None)
    r = subprocess.run([*CK, "stats", "collect", "--run", run.run_id], cwd=proj, env=e, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (env.home / ".local" / "share" / "claude-kiro" / "runs" / f"{run.run_id}.json").is_file()


# --------------------------------------------------------------------------- TC-3


def test_tc3_fresh_auto_collect_records_versions_and_run_header(env):
    """TC-3."""
    _, sess, proj = world(env, cc_version="2.1.80")
    res = implement_result()
    run = sess.implement_run(result=res, duration_ms=654_321)
    r = collect(env, "--run", run.run_id, "--auto", cwd=proj)
    assert r.exit_code == 0, r.output
    rec = load_record(env.xdg, run.run_id)
    assert rec["schema"].startswith("ck-run-local/")
    assert rec["ckVersion"] == ck_version()
    assert rec["collectorVersion"] == ck_version()
    assert rec["claudeCodeVersion"] == "2.1.80"
    assert rec["workflow"] == "spec-implement"
    assert rec["outcome"] == "complete"
    assert rec["startTime"].startswith("2026-10-12T09:00:00")
    assert rec["wallMs"] == 654_321
    assert rec["agentsConfig"] == res["agents"]


def test_tc3_no_version_in_transcripts_gives_null_claude_code_version(env):
    """TC-3."""
    _, sess, proj = world(env, cc_version="2.1.80")
    run = sess.implement_run()
    for p in [sess.transcript_path, *run.agents_dir.glob("agent-*.jsonl")]:
        lines = [json.loads(l) for l in p.read_text().splitlines()]
        for l in lines:
            l.pop("version", None)
        p.write_text("".join(json.dumps(l) + "\n" for l in lines))
    assert collect(env, "--run", run.run_id, cwd=proj).exit_code == 0
    assert load_record(env.xdg, run.run_id)["claudeCodeVersion"] is None


def _manifest(proj: Path, version: str, when: str) -> None:
    p = proj / ".claude" / "ck-manifest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"version": version}))
    t = to_ms(when) / 1000
    os.utime(p, (t, t))


def test_tc3_backfill_credits_manifest_version_only_when_it_predates_the_run(env):
    """TC-3."""
    _, sess, proj = world(env)
    run = sess.implement_run()
    _manifest(proj, "0.8.0", "2026-09-01T00:00:00Z")
    r = collect(env, "--all", cwd=proj)
    assert r.exit_code == 0, r.output
    rec = load_record(env.xdg, run.run_id)
    assert rec["ckVersion"] == "0.8.0"
    assert rec["collectorVersion"] == ck_version()


def test_tc3_backfill_with_newer_or_absent_manifest_gives_null_ck_version(env):
    """TC-3."""
    _, sess, proj = world(env)
    newer = sess.implement_run()
    _manifest(proj, "0.8.0", "2026-11-01T00:00:00Z")
    assert collect(env, "--all", cwd=proj).exit_code == 0
    rec = load_record(env.xdg, newer.run_id)
    assert rec["ckVersion"] is None
    assert any(m["what"] == "ckVersion" for m in rec["missing"])
    assert rec["collectorVersion"] == ck_version()
    # absent manifest
    (proj / ".claude" / "ck-manifest.json").unlink()
    (runs_dir(env) / f"{newer.run_id}.json").unlink()
    assert collect(env, "--all", cwd=proj).exit_code == 0
    rec = load_record(env.xdg, newer.run_id)
    assert rec["ckVersion"] is None
    assert any(m["what"] == "ckVersion" for m in rec["missing"])


def test_tc3_recollect_by_all_keeps_ck_version_of_an_auto_collected_run(env):
    """TC-3."""
    _, sess, proj = world(env)
    run = sess.implement_run()
    assert collect(env, "--run", run.run_id, "--auto", cwd=proj).exit_code == 0
    assert load_record(env.xdg, run.run_id)["ckVersion"] == ck_version()
    # a transcript gains a request
    t = run.transcript(0)
    line = json.loads(t.read_text().splitlines()[-1])
    line["requestId"] = "req_new"
    line["message"] = {**line["message"], "id": "msg_new"}
    with t.open("a") as fh:
        fh.write(json.dumps(line) + "\n")
    assert collect(env, "--all", cwd=proj).exit_code == 0
    assert load_record(env.xdg, run.run_id)["ckVersion"] == ck_version()


# --------------------------------------------------------------------------- TC-4, TC-5, TC-6


def test_tc4_tokens_and_usd_match_hand_computed_values_with_requests_counted_once(env):
    """TC-4."""
    _, sess, proj = world(env)
    agents = three_model_agents()
    run = sess.implement_run(agents=agents)
    assert collect(env, "--run", run.run_id, cwd=proj).exit_code == 0
    rec = load_record(env.xdg, run.run_id)

    expected_usd = {
        "task 1": usd("claude-sonnet-5-5", 1000, 500, 2000, 100) + usd("claude-sonnet-5-5", 3000, 700, 9000, 400),
        "review 1": usd("claude-opus-5-5", 2500, 900, 4000, 0),
        "task 2": usd("claude-sonnet-5-5", 800, 200, 100, 50),
        "land 1 2": usd("claude-haiku-5-5", 400, 100, 300, 10),
    }
    expected_tok = {
        "task 1": (4000, 1200, 11000, 500),
        "review 1": (2500, 900, 4000, 0),
        "task 2": (800, 200, 100, 50),
        "land 1 2": (400, 100, 300, 10),
    }
    by_label = {a["label"]: a for a in rec["agents"]}
    for label, want in expected_usd.items():
        a = by_label[label]
        assert a["usd"] == pytest.approx(want, abs=1e-9)
        assert (a["input"], a["output"], a["cacheRead"], a["cacheCreation"]) == expected_tok[label]

    def cell_sum(labels):
        return (sum(expected_usd[l] for l in labels), [sum(expected_tok[l][i] for l in labels) for i in range(4)])

    groups = {
        "byModel": {"claude-sonnet-5-5": ["task 1", "task 2"], "claude-opus-5-5": ["review 1"], "claude-haiku-5-5": ["land 1 2"]},
        "byPhase": {"Wave 1": ["task 1", "review 1"], "Wave 2": ["task 2", "land 1 2"]},
        "byRole": {"implementer": ["task 1", "task 2"], "reviewer": ["review 1"], "land": ["land 1 2"]},
        "byTask": {"1": ["task 1", "review 1"], "2": ["task 2"]},
    }
    for key, mapping in groups.items():
        for name, labels in mapping.items():
            cell = rec[key][name]
            u, toks = cell_sum(labels)
            assert cell["usd"] == pytest.approx(u, abs=1e-9), (key, name)
            assert [cell["input"], cell["output"], cell["cacheRead"], cell["cacheCreation"]] == toks, (key, name)


def test_tc5_priced_total_matches_ck_cost_to_the_cent(env):
    """TC-5."""
    _, sess, proj = world(env)
    run = sess.implement_run(agents=three_model_agents())
    assert collect(env, "--run", run.run_id, cwd=proj).exit_code == 0
    cost = ck_sub("cost", "--run", run.run_id, "--json", env=env, cwd=proj)
    assert cost.exit_code == 0, cost.output
    want = json.loads(cost.stdout)["priced_usd"]
    got = priced_figure(load_record(env.xdg, run.run_id))["usd"]
    assert round(got, 2) == round(want, 2)


def test_tc6_cost_source_without_and_with_budget_attachment(env):
    """TC-6."""
    from claude_kiro import bench

    _, sess, proj = world(env)
    plain = sess.implement_run(agents=three_model_agents())
    assert collect(env, "--run", plain.run_id, cwd=proj).exit_code == 0
    fig = priced_figure(load_record(env.xdg, plain.run_id))
    assert fig["source"] == "priced_from_transcripts"
    assert fig["pricingDate"] == bench.PRICING_DATE

    budget = sess.implement_run(agents=three_model_agents())
    budget.add_budget(12.5)
    assert collect(env, "--run", budget.run_id, cwd=proj).exit_code == 0
    rec = load_record(env.xdg, budget.run_id)
    run_fig = priced_figure(rec)
    assert run_fig["source"] == "priced_from_transcripts"
    assert run_fig["usd"] == pytest.approx(fig["usd"], abs=1e-6)
    session_figs = [c for c in rec["costs"] if c["scope"] == "session"]
    assert len(session_figs) == 1
    assert session_figs[0]["source"] == "claude_total"


# --------------------------------------------------------------------------- TC-7


def test_tc7_per_task_and_per_run_fields_are_recorded(env):
    """TC-7."""
    tasks = [
        TaskSpec(task="1", attempts=3, escalations=[{"from": "sonnet", "model": "opus"}], rounds=2, verdict="approve",
                 risk_tag="risky", fixes=[{"what": "lint"}], started_at="2026-10-12T09:00:00Z",
                 finished_at="2026-10-12T09:05:00Z", merged_at="2026-10-12T09:12:00Z", findings=[{"severity": "minor"}]),
        TaskSpec(task="2", wave=2, attempts=1, rounds=0, verdict="approve"),
    ]
    landings = [{"tasks": ["1", "2"], "size": 2, "window": 2, "merged": ["1", "2"], "red": [], "conflict": [], "deferred": [],
                 "other": [], "gates": 1, "bisected": False, "minutes": 1.5}]
    full_gates = [{"afterMerges": 2, "green": True}]
    cross = [{"wave": 1, "tasks": ["1"], "verdict": "ok", "findings": [], "summary": "fine"}]
    fixers = [{"what": "landing", "model": "opus", "green": True}]
    res = implement_result(tasks, landings=landings, full_gates=full_gates, final_gate={"green": True},
                           cross_tests=cross, fixer_escalations=fixers)
    _, sess, proj = world(env)
    run = sess.implement_run(result=res)
    assert collect(env, "--run", run.run_id, cwd=proj).exit_code == 0
    rec = load_record(env.xdg, run.run_id)
    t1 = next(t for t in rec["tasks"] if str(t["task"]) == "1")
    assert t1["attempts"] == 3 and t1["retries"] == 2
    assert t1["escalations"] and t1["reviewRounds"] == 2 and t1["verdict"] == "approve"
    assert t1["riskTag"] == "risky"
    assert t1["fixes"]
    assert t1["idleBeforeMergeMs"] == 7 * 60_000
    assert t1["agentMinutes"] is not None and t1["toMergedMinutes"] is not None
    t2 = next(t for t in rec["tasks"] if str(t["task"]) == "2")
    assert t2["retries"] == 0
    assert rec["landings"][0]["size"] == 2 and rec["landings"][0]["bisected"] is False
    assert rec["fullGates"] and rec["fullGates"][0]["green"] is True
    assert rec["finalGate"]["green"] is True
    assert rec["crossTests"]
    assert rec["fixerEscalations"] and rec["fixerEscalations"][0]["model"] == "opus"
    assert rec["phases"]


# --------------------------------------------------------------------------- TC-8


def test_tc8_spec_create_counts_and_phase_durations(env):
    """TC-8."""
    _, sess, proj = world(env)
    run = sess.create_run(
        result=create_result(fixed=3, not_fixed=1, escalated=1, refuted=2, lenses_lost=1, consistency_passes=2, consistency_fixed=1),
        agents=[
            AgentSpec("write requirements.md", phase="Requirements", started_at_ms=to_ms("2026-10-12T08:00:00Z"), duration_ms=60_000),
            AgentSpec("write design.md", phase="Design + Test Plan", started_at_ms=to_ms("2026-10-12T08:02:00Z"), duration_ms=120_000),
            AgentSpec("review: fidelity", phase="Adversarial Review", model="claude-opus-5-5",
                      started_at_ms=to_ms("2026-10-12T08:05:00Z"), duration_ms=180_000),
        ])
    assert collect(env, "--run", run.run_id, cwd=proj).exit_code == 0
    rec = load_record(env.xdg, run.run_id)
    assert rec["workflow"] == "spec-create"
    assert rec["review"] == {"fixed": 3, "notFixed": 1, "escalated": 1, "refuted": 2, "lensesLost": 1}
    assert rec["consistency"]["passes"] == 2 and rec["consistency"]["fixed"] == 1
    phases = rec["phases"]
    assert set(phases) == {"Requirements", "Design + Test Plan", "Adversarial Review"}
    assert all(isinstance(v, (int, float)) and v > 0 for v in phases.values())


# --------------------------------------------------------------------------- TC-9


def test_tc9_resolves_recorded_or_inferred_from_labels(env):
    """TC-9."""
    _, sess, proj = world(env)
    recorded = sess.implement_run(
        result=implement_result([TaskSpec(task="4", resolves=2)]),
        agents=[AgentSpec("task 4"), AgentSpec("resolve 4")])
    assert collect(env, "--run", recorded.run_id, cwd=proj).exit_code == 0
    t = load_record(env.xdg, recorded.run_id)["tasks"][0]
    assert t["resolves"] == 2 and t["resolvesInferred"] is False

    old = sess.implement_run(
        result=implement_result([TaskSpec(task="4"), TaskSpec(task="5", wave=2)], resolves=False),
        agents=[AgentSpec("task 4"), AgentSpec("resolve 4"), AgentSpec("resolve 4 round 2"), AgentSpec("task 5", phase="Wave 2")])
    assert collect(env, "--run", old.run_id, cwd=proj).exit_code == 0
    tasks = {str(x["task"]): x for x in load_record(env.xdg, old.run_id)["tasks"]}
    assert tasks["4"]["resolves"] == 2 and tasks["4"]["resolvesInferred"] is True
    assert tasks["5"]["resolves"] == 0 and tasks["5"]["resolvesInferred"] is True


# --------------------------------------------------------------------------- TC-10


def test_tc10_pseudonym_is_stable_opaque_and_salted(env):
    """TC-10."""
    _, sess, proj = world(env, "git@github.com:acme-corp/acme-ledger.git")
    r1 = sess.implement_run()
    r2 = sess.implement_run(start="2026-10-13T09:00:00Z")
    assert collect(env, "--run", r1.run_id, cwd=proj).exit_code == 0
    cfg_path = env.config / "config.json"
    salt1 = json.loads(cfg_path.read_text())["stats_salt"]
    p1 = load_record(env.xdg, r1.run_id)["projectPseudonym"]
    assert p1
    assert collect(env, "--run", r1.run_id, cwd=proj).exit_code == 0
    assert collect(env, "--run", r2.run_id, cwd=proj).exit_code == 0
    assert json.loads(cfg_path.read_text())["stats_salt"] == salt1
    assert load_record(env.xdg, r1.run_id)["projectPseudonym"] == p1
    assert load_record(env.xdg, r2.run_id)["projectPseudonym"] == p1
    for part in ("acme-corp", "acme-ledger", "acme", "ledger", "github", "git@"):
        assert part not in p1.lower()

    # a fresh salt gives a different pseudonym
    cfg_path.unlink()
    assert collect(env, "--run", r1.run_id, cwd=proj).exit_code == 0
    assert load_record(env.xdg, r1.run_id)["projectPseudonym"] != p1


def test_tc10_project_without_remote_has_null_pseudonym(env):
    """TC-10."""
    _, sess, proj = world(env, None)
    run = sess.implement_run()
    assert collect(env, "--run", run.run_id, cwd=proj).exit_code == 0
    assert load_record(env.xdg, run.run_id)["projectPseudonym"] is None


# --------------------------------------------------------------------------- TC-11, TC-12, TC-13


def test_tc11_recollect_replaces_the_record(env):
    """TC-11."""
    _, sess, proj = world(env)
    run = sess.implement_run(agents=[AgentSpec("task 1", requests=[("r1", 1000, 500, 0, 0)])])
    assert collect(env, "--run", run.run_id, cwd=proj).exit_code == 0
    before = priced_figure(load_record(env.xdg, run.run_id))["usd"]
    t = run.transcript(0)
    line = json.loads(t.read_text().splitlines()[-1])
    line["requestId"] = "req_more"
    line["message"] = {**line["message"], "id": "msg_more", "usage": {"input_tokens": 50_000, "output_tokens": 0,
                                                                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}
    with t.open("a") as fh:
        fh.write(json.dumps(line) + "\n")
    assert collect(env, "--run", run.run_id, cwd=proj).exit_code == 0
    assert [p for p in store_files(env.xdg) if p.name == f"{run.run_id}.json"] == [runs_dir(env) / f"{run.run_id}.json"]
    assert len(list(runs_dir(env).glob("*.json"))) == 1
    after = priced_figure(load_record(env.xdg, run.run_id))["usd"]
    assert after > before


def test_tc12_unknown_run_is_not_found_and_names_the_searched_home(env):
    """TC-12."""
    world(env)
    r = collect(env, "--run", "wf_nope")
    assert r.exit_code != 0
    assert "not found" in r.output.lower()
    assert str(env.claude) in r.output
    assert store_files(env.xdg) == []


def test_tc13_running_run_is_not_collected(env):
    """TC-13."""
    _, sess, proj = world(env)
    run = sess.implement_run(status="running")
    r = collect(env, "--run", run.run_id, cwd=proj)
    assert r.exit_code == 0, r.output
    assert "not finished" in r.output.lower()
    assert store_files(env.xdg) == []


# --------------------------------------------------------------------------- TC-14


def test_tc14_missing_data_gives_nulls_and_missing_entries(env):
    """TC-14."""
    _, sess, proj = world(env)
    run = sess.implement_run(agents=[
        AgentSpec("task 1 SECRETLABEL-a"), AgentSpec("review 1 SECRETLABEL-b", model="claude-opus-5-5"),
        AgentSpec("land 1 2 SECRETLABEL-c", model="claude-haiku-5-5"), AgentSpec("task 2 ok", phase="Wave 2")])
    run.drop_transcript(0)
    run.truncate_transcript(1)
    run.drop_meta(2)
    r = collect(env, "--run", run.run_id, cwd=proj)
    assert r.exit_code == 0, r.output
    rec = load_record(env.xdg, run.run_id)
    flagged = {m["agentId"] for m in rec["missing"]}
    assert run.agent_ids[0] in flagged or 0 in flagged
    assert len(rec["missing"]) >= 3
    dropped = next(a for a in rec["agents"] if a["agentId"] == run.agent_ids[0])
    assert dropped["usd"] is None and dropped["input"] is None
    for a in rec["agents"]:
        if a["agentId"] in (run.agent_ids[0], run.agent_ids[1]):
            assert a["usd"] != 0
    estimates = [c for c in rec["costs"] if c["source"] == "estimate"]
    assert estimates or all(c["source"] != "priced_from_transcripts" or c["usd"] is not None for c in rec["costs"])
    assert "SECRETLABEL" not in json.dumps(rec["missing"])
    assert "SECRETLABEL" not in r.output


# --------------------------------------------------------------------------- TC-15, TC-16, TC-17


def _timed_agents() -> List[AgentSpec]:
    t0 = to_ms("2026-10-12T09:00:00Z")
    return [
        AgentSpec("task 1", phase="Wave 1", started_at_ms=t0, duration_ms=60_000),
        AgentSpec("review 1", phase="Wave 1", model="claude-opus-5-5", started_at_ms=t0 + 30_000, duration_ms=120_000),
        AgentSpec("land 1", phase="Wave 2", model="claude-haiku-5-5", started_at_ms=t0 + 200_000, duration_ms=40_000),
    ]


def test_tc15_run_file_gone_recovers_a_partial_record_from_the_session(env):
    """TC-15."""
    _, sess, proj = world(env)
    run = sess.implement_run(agents=_timed_agents())
    run.drop_run_file()
    r = collect(env, "--run", run.run_id, cwd=proj)
    assert r.exit_code == 0, r.output
    rec = load_record(env.xdg, run.run_id)
    assert rec["workflow"] == "spec-implement"
    whats = {m["what"] for m in rec["missing"]}
    assert {"runfile", "result"} <= whats
    roles = {a["label"]: a["role"] for a in rec["agents"]}
    assert roles == {"task 1": "implementer", "review 1": "reviewer", "land 1": "land"}
    phases = {a["label"]: a["phase"] for a in rec["agents"]}
    assert phases == {"task 1": "Wave 1", "review 1": "Wave 1", "land 1": "Wave 2"}
    by = {a["label"]: a for a in rec["agents"]}
    assert by["task 1"]["ms"] == 60_000
    assert by["review 1"]["ms"] == 120_000
    assert rec["wallMs"] == 240_000
    cost = ck_sub("cost", "--run", run.run_id, "--json", env=env, cwd=proj)
    assert cost.exit_code == 0, cost.output
    assert round(priced_figure(rec)["usd"], 2) == round(json.loads(cost.stdout)["priced_usd"], 2)


def test_tc15_spec_create_recognised_from_script_path_alone(env):
    """TC-15."""
    _, sess, proj = world(env)
    run = sess.create_run(launch={"scriptPath": f"{proj}/.claude/workflows/spec-create.js"})
    run.drop_run_file()
    r = collect(env, "--run", run.run_id, cwd=proj)
    assert r.exit_code == 0, r.output
    assert load_record(env.xdg, run.run_id)["workflow"] == "spec-create"


@pytest.mark.parametrize("case", ["foreign_launch", "no_launch", "other_id_launch", "no_session"])
def test_tc15_unrecoverable_run_file_gone_is_not_found(env, case):
    """TC-15."""
    _, sess, proj = world(env)
    kw: Dict[str, Any] = {}
    if case == "foreign_launch":
        kw["launch"] = {"name": "tax-review-2026"}
    elif case == "no_launch":
        kw["launch"] = False
    run = sess.implement_run(**kw)
    if case == "other_id_launch":
        sess.append(sess._launch_lines("wf_someone_else", "spec-implement", True, "2026-10-12T09:00:00Z"))
        text = sess.transcript_path.read_text().replace(run.run_id, "wf_zzz_other")
        sess.transcript_path.write_text(text)
        sess.append(sess._launch_lines("wf_someone_else", "spec-implement", True, "2026-10-12T09:00:00Z"))
    run.drop_run_file()
    if case == "no_session":
        sess.transcript_path.unlink()
    r = collect(env, "--run", run.run_id, cwd=proj)
    assert r.exit_code != 0
    assert "not found" in r.output.lower()
    assert str(env.claude) in r.output
    assert store_files(env.xdg) == []
    assert "tax-review-2026" not in r.output


def test_tc16_corrupt_allowlisted_run_file_is_unrecognised(env):
    """TC-16."""
    _, sess, proj = world(env)
    obj = sess.implement_run().corrupt_format()
    absent = sess.implement_run(start="2026-10-12T10:00:00Z")
    d = json.loads(absent.path.read_text())
    del d["workflowProgress"]
    absent.path.write_text(json.dumps(d))
    for run in (obj, absent):
        r = collect(env, "--run", run.run_id, cwd=proj)
        assert r.exit_code != 0
        assert "unrecognised" in r.output.lower() or "unrecognized" in r.output.lower()
        assert run.run_id in r.output
    assert store_files(env.xdg) == []


def test_tc16_corrupt_foreign_run_file_reveals_nothing(env):
    """TC-16."""
    _, sess, proj = world(env)
    foreign = sess.foreign_run(name="tax-review-2026", result={"summary": "SECRET-CONTENT-42"}).corrupt_format()
    r = collect(env, "--run", foreign.run_id, cwd=proj)
    assert r.exit_code != 0
    assert "tax-review-2026" not in r.output and "SECRET-CONTENT-42" not in r.output
    assert store_files(env.xdg) == []


def test_tc16_workflow_progress_with_odd_entries_still_gives_a_record(env):
    """TC-16."""
    _, sess, proj = world(env)
    run = sess.implement_run(result="a string result")
    d = json.loads(run.path.read_text())
    d["workflowProgress"] = [42, *d["workflowProgress"],
                             {"type": "workflow_agent", "agentId": "aodd", "durationMs": "abc", "label": 7, "tokens": "x"}]
    run.path.write_text(json.dumps(d))
    r = collect(env, "--run", run.run_id, cwd=proj)
    assert r.exit_code == 0, r.output
    rec = load_record(env.xdg, run.run_id)
    assert {"task 1", "review 1", "land 1"} <= {a["label"] for a in rec["agents"]}
    whats = {m["what"] for m in rec["missing"]}
    assert "agent_entry" in whats and "result" in whats
    odd = [a for a in rec["agents"] if a["agentId"] == "aodd"]
    for a in odd:
        assert a["ms"] is None or a["ms"] != 0


def test_tc16_invalid_json_run_file_under_run_and_all(env):
    """TC-16."""
    _, sess, proj = world(env)
    run = sess.implement_run()
    run.path.write_text("{ not json at all")
    r = collect(env, "--run", run.run_id, cwd=proj)
    assert r.exit_code != 0
    assert run.run_id in r.output
    assert "unrecognised" in r.output.lower() or "unrecognized" in r.output.lower()
    assert store_files(env.xdg) == []

    foreign = sess.foreign_run("wf_foreignbad", name="tax-review-2026")
    foreign.path.write_text('{ "workflowName": "tax-review-2026", broken')
    r = collect(env, "--all", cwd=proj)
    assert "tax-review-2026" not in r.output and "wf_foreignbad" not in r.output
    assert run.run_id not in r.output
    assert "skipped non-ck: 2" in r.output
    assert "wf_foreignbad" not in r.output


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod 000 does not restrict root")
@pytest.mark.parametrize("flag", ["--run", "--all"])
def test_tc17_unreadable_projects_dir(env, flag):
    """TC-17."""
    _, sess, proj = world(env)
    run = sess.implement_run()
    projects = env.claude / "projects"
    projects.chmod(0)
    try:
        args = ["--run", run.run_id] if flag == "--run" else ["--all"]
        r = collect(env, *args, cwd=proj)
    finally:
        projects.chmod(0o755)
    assert r.exit_code != 0
    assert "outside the sandbox" in r.output.lower()
    assert store_files(env.xdg) == []


# --------------------------------------------------------------------------- TC-79


def test_tc79_modes_of_records_directory_and_config(env):
    """TC-79."""
    _, sess, proj = world(env)
    run = sess.implement_run()
    assert collect(env, "--run", run.run_id, cwd=proj).exit_code == 0
    assert mode(runs_dir(env) / f"{run.run_id}.json") == 0o600
    assert mode(runs_dir(env)) == 0o700
    assert mode(env.config / "config.json") == 0o600


def test_tc79_existing_world_readable_config_is_tightened_and_stays_so(env):
    """TC-79."""
    _, sess, proj = world(env)
    run = sess.implement_run()
    assert ck_sub("agents", "set", "implementer.model", "sonnet", env=env, cwd=proj).exit_code == 0
    cfg = env.config / "config.json"
    before = json.loads(cfg.read_text())
    cfg.chmod(0o644)
    assert collect(env, "--run", run.run_id, cwd=proj).exit_code == 0
    assert mode(cfg) == 0o600
    after = json.loads(cfg.read_text())
    assert {k: v for k, v in after.items() if k != "stats_salt"} == before
    assert "stats_salt" in after
    assert ck_sub("agents", "set", "implementer.effort", "high", env=env, cwd=proj).exit_code == 0
    assert mode(cfg) == 0o600


# --------------------------------------------------------------------------- TC-83


@pytest.mark.parametrize("case,fields,want", [
    ("a", {"halted": False, "failed": [], "notStarted": []}, "complete"),
    ("b", {"halted": False, "failed": [{"task": "1", "reason": "x"}], "notStarted": []}, "blocked"),
    ("c", {"halted": True, "failed": [], "notStarted": ["2"]}, "halted"),
    ("d", {"refused": True, "halted": False, "failed": [], "notStarted": []}, "refused"),
])
def test_tc83_pre_010_results_get_an_inferred_outcome(env, case, fields, want):
    """TC-83."""
    _, sess, proj = world(env)
    res = implement_result(outcome=None)
    res.update(fields)
    run = sess.implement_run(result=res)
    r = collect(env, "--all", cwd=proj)
    assert r.exit_code == 0, r.output
    rec = load_record(env.xdg, run.run_id)
    assert rec["outcome"] == want
    assert rec["outcomeInferred"] is True


def test_tc83_current_result_outcome_is_taken_as_recorded(env):
    """TC-83."""
    _, sess, proj = world(env)
    run = sess.implement_run(result=implement_result(outcome="blocked"))
    assert collect(env, "--all", cwd=proj).exit_code == 0
    rec = load_record(env.xdg, run.run_id)
    assert rec["outcome"] == "blocked" and rec["outcomeInferred"] is False


# --------------------------------------------------------------------------- contract check


@pytest.mark.parametrize("kind", ["spec-implement", "spec-create"])
def test_seeded_local_record_has_the_key_set_of_a_collected_record(tmp_path, env, kind):
    """Contract check: seeded_local_record() keys equal a really collected record's keys."""
    home, terms = seeded_home(tmp_path)
    e = dict(env)
    e["CLAUDE_CONFIG_DIR"] = str(home.root)
    proj = home.projects_by_name["acme-ledger"]
    run_id = "wf_zephyr-impl-1" if kind == "spec-implement" else "wf_zephyr-create-1"
    r = stats("collect", "--run", run_id, env=e, cwd=proj)
    assert r.exit_code == 0, r.output
    rec = json.loads((env.xdg / "claude-kiro" / "runs" / f"{run_id}.json").read_text())
    assert set(seeded_local_record(kind)) == set(rec)
