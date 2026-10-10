"""`ck bench`: run model configurations against specs in fresh clones, score them on hidden tests.

Every test uses a fake `ck run` (tests/bench_helpers.py); no Claude session is started.
"""

import json
from pathlib import Path

from bench_helpers import bench_setup, git, invoke

from claude_kiro import bench


def run_bench(tmp_path, reps=1, env=None, extra=()):
    cfg = bench_setup(tmp_path, reps=reps)
    out = invoke("bench", "run", str(cfg), *extra, env=env)
    assert out.exit_code == 0, out.output
    return cfg, json.loads(cfg.read_text())


def results(tmp_path) -> dict:
    return {p.stem: json.loads(p.read_text()) for p in sorted((tmp_path / "results").glob("*.json"))}


def test_plan_is_randomised_by_seed_and_covers_every_arm(tmp_path):
    cfg = json.loads(bench_setup(tmp_path, reps=3).read_text())
    cfg["specs"].append({"name": "other"})
    a, b = bench.plan_arms(cfg), bench.plan_arms(cfg)
    assert a == b  # same seed, same order
    assert sorted(x["id"] for x in a) == sorted(f"{s}--{c}--r{r}" for s in ("calc", "other") for c in ("opus", "haiku") for r in (1, 2, 3))
    cfg["seed"] = 8
    assert [x["id"] for x in bench.plan_arms(cfg)] != [x["id"] for x in a]
    assert [x["id"] for x in a] != sorted(x["id"] for x in a)  # not in config order


def test_run_clones_applies_agents_launches_and_records_results(tmp_path):
    cfg_path, cfg = run_bench(tmp_path)
    res = results(tmp_path)
    assert set(res) == {"calc--opus--r1", "calc--haiku--r1"}
    r = res["calc--opus--r1"]
    clone = Path(r["clone"])
    # Fresh clone at the base commit, on its own branch, with the agents applied and committed.
    assert clone.is_dir() and clone.parent == tmp_path / "runs"
    assert r["branch"] == "bench/opus-1"
    assert r["base_sha"] == git(Path(cfg["repo"]), "rev-parse", "main")
    settings = json.loads(git(clone, "show", "HEAD:specs/ck.json"))
    assert settings["agents"]["implementer"]["model"] == "opus" and settings["agents"]["reviewer"]["rounds"] == 2
    assert git(clone, "status", "--porcelain") == ""
    # The pinned ck version is recorded, from the ck that ran the setup.
    assert r["ck_version"] and r["ck_version_cmd"]
    # The launch carried the JSON result format, a session id and run_args.
    argv = r["argv"]
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--session-id") + 1] == r["session_id"]
    assert argv[-3:] == ["--", "--max-concurrent", "2"]
    # The workflow report, found by the session id.
    rep = r["report"]
    assert rep["merged"] == ["1", "2"] and rep["final_gate_green"] is True
    assert r["into"] == "integrate/calc" and r["final_sha"] == git(clone, "rev-parse", "integrate/calc")
    # Cost: the session's total_cost_usd, plus the transcripts priced (one request, deduped by requestId).
    assert r["cost"]["total_cost_usd"] == 3.0
    assert abs(r["cost"]["priced_usd"] - 24.2) < 1e-9  # opus: 1M in x4 + 1M out x20 + 1M cache read x0.2
    assert r["cost"]["usd"] == 3.0
    assert r["agents"][0]["label"] == "task 2" and r["agents"][0]["usage"]["output"] == 1_000_000
    assert r["fail_closed"] is False and r["exit_code"] == 0 and r["wall_minutes"] >= 0
    h = res["calc--haiku--r1"]
    assert h["report"]["escalations"] == 1 and h["report"]["review_rounds"] == 1
    assert abs(h["cost"]["priced_usd"] - 0.61) < 1e-9


def test_rerun_skips_finished_arms_unless_forced(tmp_path):
    cfg_path, _ = run_bench(tmp_path)
    before = results(tmp_path)
    out = invoke("bench", "run", str(cfg_path))
    assert out.exit_code == 0 and "skip" in out.output.lower()
    assert results(tmp_path) == before


def test_permission_denial_or_question_fails_closed(tmp_path):
    run_bench(tmp_path, env={"FAKE_DENY": "1", "FAKE_ASK": "1"})
    r = results(tmp_path)["calc--opus--r1"]
    assert r["fail_closed"] is True
    reasons = " ".join(r["fail_closed_reasons"])
    assert "permission" in reasons.lower() and "AskUserQuestion" in reasons


def test_dry_run_prints_the_plan_and_runs_nothing(tmp_path):
    cfg = bench_setup(tmp_path)
    out = invoke("bench", "run", str(cfg), "--dry-run")
    assert out.exit_code == 0, out.output
    assert "calc--opus--r1" in out.output and "calc--haiku--r1" in out.output
    assert not (tmp_path / "runs").exists() and not (tmp_path / "results").exists()


def test_score_hidden_suite_separates_the_arms(tmp_path):
    cfg_path, _ = run_bench(tmp_path)
    out = invoke("bench", "score", str(tmp_path / "results"), "--config", str(cfg_path), "--seed", "3")
    assert out.exit_code == 0, out.output
    score = json.loads((tmp_path / "results" / "score.json").read_text())
    arms = score["arms"]
    assert arms["opus"]["hidden_pass_rate"]["value"] == 1.0
    assert arms["haiku"]["hidden_pass_rate"]["value"] == 0.0
    assert arms["opus"]["invariants_pass_rate"]["value"] == 1.0 and arms["haiku"]["invariants_pass_rate"]["value"] == 1.0
    assert arms["opus"]["tasks_passing_hidden"] == 1 and arms["haiku"]["tasks_passing_hidden"] == 0
    assert arms["opus"]["usd_per_passing_task"]["value"] == 3.0 and arms["haiku"]["usd_per_passing_task"]["value"] is None
    assert arms["opus"]["merged_rate"]["value"] == 1.0
    assert arms["haiku"]["escalations_per_run"]["value"] == 1.0
    # Paired by task where both arms merged; baseline is the first config.
    pair = score["paired"]["haiku"]
    assert pair["baseline"] == "opus" and pair["units"] == 1 and pair["hidden_pass_diff"]["value"] == -1.0
    md = (tmp_path / "results" / "score.md").read_text()
    assert "| opus |" in md and "| haiku |" in md and "hidden" in md.lower()
    # The hidden tests never reached the arms' own branches.
    clone = Path(results(tmp_path)["calc--opus--r1"]["clone"])
    assert "test_task2_subtract" not in git(clone, "log", "--all", "--name-only", "--format=")


def test_fail_closed_run_scores_zero(tmp_path):
    cfg_path, _ = run_bench(tmp_path, env={"FAKE_DENY": "1"})
    invoke("bench", "score", str(tmp_path / "results"), "--config", str(cfg_path))
    arms = json.loads((tmp_path / "results" / "score.json").read_text())["arms"]
    assert arms["opus"]["fail_closed_runs"] == 1 and arms["opus"]["hidden_pass_rate"]["value"] == 0.0


def test_bootstrap_is_deterministic_with_a_seed():
    clusters = [[1.0, 0.0], [1.0, 1.0], [0.0], [1.0, 1.0, 0.0], [0.5]]
    mean = lambda cs: (lambda xs: sum(xs) / len(xs))([x for c in cs for x in c])
    a = bench.bootstrap_ci(clusters, mean, seed=11, n=500)
    b = bench.bootstrap_ci(clusters, mean, seed=11, n=500)
    c = bench.bootstrap_ci(clusters, mean, seed=12, n=500)
    assert a == b and a != c
    lo, hi = a
    assert 0.0 <= lo <= mean(clusters) <= hi <= 1.0
    assert bench.bootstrap_ci([[1.0]], mean, seed=1) == (1.0, 1.0)
    assert bench.bootstrap_ci([], mean, seed=1) == (None, None)


def test_price_usage_uses_list_prices_and_overrides():
    u = {"input": 2_000_000, "output": 1_000_000, "cacheRead": 10_000_000, "cacheCreation": 0}
    assert abs(bench.price_usage("claude-sonnet-5-5", u) - (4 + 10 + 2)) < 1e-9
    assert abs(bench.price_usage("claude-haiku-5-5", u, {"haiku": {"input": 1, "output": 1, "cacheRead": 0}}) - 3) < 1e-9
    assert bench.price_usage("gpt-x", u) is None


IMPORT_FIXTURE = {
    "summary": "Implement a spec",
    "agentCount": 3,
    "logs": ["Agents: impl haiku/medium", "Task 3: reviewer asked for changes, revision 1/2"],
    "totalTokens": 300000,
    "result": {
        "halted": False, "reason": None, "into": "integrate/demo", "targetGreen": True,
        "merged": ["3", "5"],
        "failed": [{"task": "4", "reason": "Both attempts were refused by the permission layer"}],
        "notStarted": ["6"],
        "escalated": [{"task": "5", "models": ["sonnet"], "ok": True}],
        "timing": {"start": "2026-10-10T20:59:05Z", "end": "2026-10-10T21:15:29Z", "minutes": 16.4, "phases": []},
        "tasks": [
            {"task": "3", "ok": True, "attempts": 1, "escalations": [], "review": {"verdict": "approve", "rounds": 1}},
            {"task": "4", "ok": False, "attempts": 2, "escalations": []},
            {"task": "5", "ok": True, "attempts": 2, "escalations": [{"kind": "retry", "model": "sonnet"}], "review": {"verdict": "approve", "rounds": 0}},
        ],
        "finalGate": {"green": True},
    },
    "workflowProgress": [
        {"type": "workflow_phase", "index": 1, "title": "Plan"},
        {"type": "workflow_agent", "label": "plan 1", "agentId": "p1", "model": "claude-haiku-5-5", "tokens": 100000, "durationMs": 5000},
        {"type": "workflow_agent", "label": "task 3", "agentId": "t3", "model": "claude-haiku-5-5", "tokens": 100000, "durationMs": 60000},
        {"type": "workflow_agent", "label": "task 5 retry 1 (sonnet)", "agentId": "t5", "model": "claude-sonnet-5-5[1m]", "tokens": 100000, "durationMs": 90000},
    ],
}


def test_import_converts_a_workflow_output_file(tmp_path):
    src = tmp_path / "haiku_pass1_result.txt"
    src.write_text(json.dumps(IMPORT_FIXTURE))
    out_dir = tmp_path / "results"
    out = invoke("bench", "import", str(src), "--spec", "demo", "--config", "haiku", "--rep", "1", "--out", str(out_dir))
    assert out.exit_code == 0, out.output
    r = json.loads((out_dir / "demo--haiku--r1.json").read_text())
    assert r["source"] == "import" and r["spec"] == "demo" and r["config"] == "haiku"
    rep = r["report"]
    assert rep["merged"] == ["3", "5"] and rep["failed"] == ["4"] and rep["not_started"] == ["6"]
    assert rep["escalations"] == 1 and rep["review_rounds"] == 1
    assert r["wall_minutes"] == 16.4
    # No usage breakdown: per-agent tokens (final context size) priced at the input rate, marked as estimated.
    assert [a["model"] for a in r["agents"]] == ["claude-haiku-5-5", "claude-haiku-5-5", "claude-sonnet-5-5"]
    expected = 0.1 * 0.10 + 0.1 * 0.10 + 0.1 * 2.0
    assert abs(r["cost"]["priced_usd"] - expected) < 1e-9 and r["cost"]["usd"] == r["cost"]["priced_usd"]
    assert r["cost"]["estimated"] is True and "input" in r["cost"]["basis"]
    assert r["cost"]["pricing_date"] == "2026-10"
    # A task that failed on a permission refusal makes the run fail closed.
    assert r["fail_closed"] is True


def test_import_prices_transcripts_when_given(tmp_path):
    src = tmp_path / "run.json"
    src.write_text(json.dumps(IMPORT_FIXTURE))
    tx = tmp_path / "wf"
    tx.mkdir()
    line = {"type": "assistant", "requestId": "q", "message": {"model": "claude-haiku-5-5", "usage": {
        "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}}}
    (tx / "agent-t3.jsonl").write_text(json.dumps(line) + "\n")
    out = invoke("bench", "import", str(src), "--spec", "demo", "--config", "haiku", "--rep", "2",
                 "--out", str(tmp_path / "res"), "--transcripts", str(tx))
    assert out.exit_code == 0, out.output
    r = json.loads((tmp_path / "res" / "demo--haiku--r2.json").read_text())
    by = {a["agent_id"]: a for a in r["agents"]}
    # cache read 0.01 + cache write 1.25 x input 0.10 per 1M
    assert abs(by["t3"]["cost_usd"] - (0.01 + 0.125)) < 1e-9 and by["t3"]["cost_basis"] == "transcript"
    assert by["p1"]["cost_basis"] == "context-tokens-as-input"
