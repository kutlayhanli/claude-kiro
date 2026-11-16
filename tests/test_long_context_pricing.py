"""Haiku 5.5's long-context price tier and the per-request prompt-size stats.

Claude Haiku 5.5 is $0.10 in / $0.50 out per 1M tokens for prompts up to 100K tokens and
$0.50 / $2.50 beyond; the prompt is input + cache reads + cache writes of one request, and a
long request is priced at the long rates throughout (its output too). Every scan also counts each
model's requests, how many were over 100K and the largest prompt, so a run shows where a
cheap model stopped being cheap. Expected dollars below are worked out by hand from list rates.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_kiro import bench as b
from claude_kiro.cli.main import cli

from stats_helpers import AgentSpec, FakeClaudeHome, fresh_env, git_project, load_record, stats

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"


def line(model, req, inp=0, out=0, read=0, write=0):
    return {"type": "assistant", "requestId": req, "cwd": "/proj", "timestamp": "2026-11-16T10:00:00Z",
            "message": {"model": model, "usage": {"input_tokens": inp, "output_tokens": out,
                                                  "cache_read_input_tokens": read, "cache_creation_input_tokens": write},
                        "content": [{"type": "text", "text": "x"}]}}


def transcript(tmp_path: Path, *lines) -> Path:
    p = tmp_path / "agent.jsonl"
    p.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return p


# ------------------------------------------------------------------ pricing


def test_a_short_haiku_request_is_priced_at_the_base_rates():
    # 50K cache reads x $0.01 + 1K input x $0.10 + 2K output x $0.50
    u = {"input": 1_000, "output": 2_000, "cacheRead": 50_000, "cacheCreation": 0}
    assert b.price_usage(HAIKU, u) == pytest.approx((50_000 * 0.01 + 1_000 * 0.10 + 2_000 * 0.50) / 1e6)


def test_a_long_haiku_request_is_priced_at_the_long_rates_output_included(tmp_path):
    # 150K cache reads x $0.05 + 2K input x $0.50 + 4K output x $2.50
    s = b.scan_transcript(transcript(tmp_path, line(HAIKU, "r1", inp=2_000, out=4_000, read=150_000)))
    want = (150_000 * 0.05 + 2_000 * 0.50 + 4_000 * 2.50) / 1e6
    assert b.price_usage(HAIKU, s["usage_by_model"][HAIKU]) == pytest.approx(want)


def test_short_and_long_requests_of_one_agent_are_each_priced_at_their_own_tier(tmp_path):
    s = b.scan_transcript(transcript(
        tmp_path,
        line(HAIKU, "short", inp=1_000, out=1_000, read=60_000),
        line(HAIKU, "long", inp=1_000, out=1_000, read=120_000, write=5_000),
    ))
    u = s["usage_by_model"][HAIKU]
    assert (u["input"], u["output"], u["cacheRead"], u["cacheCreation"]) == (2_000, 2_000, 180_000, 5_000)
    assert (u["longInput"], u["longOutput"], u["longCacheRead"], u["longCacheCreation"]) == (1_000, 1_000, 120_000, 5_000)
    short = 1_000 * 0.10 + 1_000 * 0.50 + 60_000 * 0.01
    long = 1_000 * 0.50 + 1_000 * 2.50 + 120_000 * 0.05 + 5_000 * 0.50 * 1.25
    assert b.price_usage(HAIKU, u) == pytest.approx((short + long) / 1e6)
    assert b.price_usage(HAIKU, b.long_part(u)) == pytest.approx(long / 1e6)


def test_a_prompt_of_exactly_100k_tokens_is_still_the_base_tier(tmp_path):
    s = b.scan_transcript(transcript(tmp_path, line(HAIKU, "r1", inp=10_000, read=90_000, out=1_000)))
    assert "longInput" not in s["usage_by_model"][HAIKU]
    assert s["prompts_by_model"][HAIKU] == {"requests": 1, "longRequests": 0, "peakPrompt": 100_000}


def test_a_model_without_a_tier_prices_long_prompts_flat_but_still_counts_them(tmp_path):
    s = b.scan_transcript(transcript(tmp_path, line(SONNET, "r1", inp=1_000, out=1_000, read=300_000)))
    assert b.price_usage(SONNET, s["usage_by_model"][SONNET]) == pytest.approx(
        (1_000 * 2.0 + 1_000 * 10.0 + 300_000 * 0.20) / 1e6)
    assert s["prompts_by_model"][SONNET] == {"requests": 1, "longRequests": 1, "peakPrompt": 301_000}


def test_token_totals_do_not_double_count_the_long_share(tmp_path):
    s = b.scan_transcript(transcript(tmp_path, line(HAIKU, "r1", inp=1_000, out=1_000, read=200_000)))
    assert b.usage_tokens(s["usage_by_model"][HAIKU]) == 202_000


def test_aggregation_keeps_the_long_share_so_a_run_total_uses_the_tier(tmp_path):
    a = b.scan_transcript(transcript(tmp_path, line(HAIKU, "a", out=1_000, read=200_000)))
    (tmp_path / "agent.jsonl").unlink()
    c = b.scan_transcript(transcript(tmp_path, line(HAIKU, "c", out=1_000, read=20_000)))
    total = b.aggregate_usage([a, c])[HAIKU]
    assert total["longCacheRead"] == 200_000 and total["cacheRead"] == 220_000
    want = (200_000 * 0.05 + 1_000 * 2.50) + (20_000 * 0.01 + 1_000 * 0.50)
    assert b.price_usage(HAIKU, total) == pytest.approx(want / 1e6)
    assert b.aggregate_prompts([a, c])[HAIKU] == {"requests": 2, "longRequests": 1, "peakPrompt": 200_000}


def test_a_pricing_override_can_set_the_long_rates():
    u = {"input": 0, "output": 0, "cacheRead": 200_000, "cacheCreation": 0, "longInput": 0, "longOutput": 0,
         "longCacheRead": 200_000, "longCacheCreation": 0}
    over = {"haiku": {"long": {"input": 1.0, "output": 5.0, "cacheRead": 0.10}}}
    assert b.price_usage(HAIKU, u, over) == pytest.approx(200_000 * 0.10 / 1e6)


# ------------------------------------------------------------------ ck cost


def test_ck_cost_reports_the_long_prompt_share_per_model(tmp_path, monkeypatch):
    sid = "5f0e2c1a-2222-4222-8333-944455556666"
    home = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    proj = home / "projects" / "-proj"
    proj.mkdir(parents=True)
    (proj / f"{sid}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in (
        line(HAIKU, "r1", out=1_000, read=50_000),
        line(HAIKU, "r2", out=1_000, read=150_000),
        line(HAIKU, "r3", out=1_000, read=160_000),
        line(HAIKU, "r4", out=1_000, read=40_000),
    )))
    s = b.session_spend(home, sid)
    m = s["by_model"][HAIKU]
    assert (m["requests"], m["longRequests"], m["peakPrompt"]) == (4, 2, 160_000)
    long = 310_000 * 0.05 + 2_000 * 2.50
    short = 90_000 * 0.01 + 2_000 * 0.50
    assert m["longUsd"] == pytest.approx(long / 1e6)
    assert s["priced_usd"] == pytest.approx((long + short) / 1e6)

    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        out = CliRunner().invoke(cli, ["cost", "--session", sid])
    finally:
        os.chdir(old)
    assert out.exit_code == 0, out.output
    pct = round(long * 100 / (long + short))
    assert f"{HAIKU}: 50% of 4 requests had prompts over 100K tokens (peak 160K), {pct}% of its cost" in out.output


# ------------------------------------------------------------------ ck stats


@pytest.fixture
def env():
    with fresh_env() as e:
        yield e


@pytest.mark.usefixtures("no_network")
def test_stats_records_and_shows_the_long_prompt_share_by_model_and_role(env):
    proj = git_project(env.root, "git@github.com:acme-corp/acme-ledger.git")
    sess = FakeClaudeHome(env.claude).session(cwd=proj)
    agents = [
        # (request id, input, output, cache read, cache write)
        AgentSpec("task 1", model=HAIKU, phase="Wave 1",
                  requests=[("h1", 1_000, 2_000, 60_000, 0), ("h2", 1_000, 2_000, 140_000, 0)]),
        AgentSpec("review 1", model=SONNET, phase="Wave 1", requests=[("s1", 1_000, 1_000, 20_000, 0)]),
    ]
    run = sess.implement_run(agents=agents)
    assert stats("collect", "--run", run.run_id, env=env, cwd=proj).exit_code == 0
    rec = load_record(env.xdg, run.run_id)

    short = 1_000 * 0.10 + 2_000 * 0.50 + 60_000 * 0.01
    long = 1_000 * 0.50 + 2_000 * 2.50 + 140_000 * 0.05
    task = next(a for a in rec["agents"] if a["label"] == "task 1")
    assert task["usd"] == pytest.approx((short + long) / 1e6)
    assert (task["requests"], task["longRequests"], task["peakPrompt"]) == (2, 1, 141_000)
    assert task["longUsd"] == pytest.approx(long / 1e6)
    review = next(a for a in rec["agents"] if a["label"] == "review 1")
    assert (review["requests"], review["longRequests"], review["longUsd"]) == (1, 0, 0.0)

    cell = rec["byModel"][HAIKU]
    assert (cell["requests"], cell["longRequests"], cell["peakPrompt"]) == (2, 1, 141_000)
    assert cell["usd"] == pytest.approx((short + long) / 1e6)
    assert cell["longUsd"] == pytest.approx(long / 1e6)
    assert rec["byRole"]["implementer"]["longRequests"] == 1

    show = stats("show", run.run_id, env=env)
    assert show.exit_code == 0, show.output
    pct = round(long * 100 / (short + long))
    assert f"{HAIKU}: 50% of 2 requests had prompts over 100K tokens (peak 141K), {pct}% of its cost" in show.output
    assert f"role implementer: 50% of 2 requests had prompts over 100K tokens (peak 141K), {pct}% of its cost" in show.output
    assert "1 requests over 100K prompt tokens at the long-context rates (in $0.50/M, out $2.50/M)" in show.output
