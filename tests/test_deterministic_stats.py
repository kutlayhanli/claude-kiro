"""Collect-level plumbing tests for deterministic-plan stats (Task 27).

Covers the three analytics plumbing tasks at the collect / pure-function level,
over records synthesized via the Task 1 ``implement_result()`` kwargs so they
run without the workflow or show code:

- shareable counts: post-change collected runs carry ``counts.plan_fallbacks``
  equal to ``stats.planFallbacks``; pre-change records produce no key and every
  existing figure is unchanged;
- issue code: ``derive_issue_codes`` on a record with ``stats.planFallbacks > 0``
  emits ``planning_fallback`` (and not otherwise), with existing
  ``planning_failed`` behaviour unchanged; the collected shareable ``issues``
  include it;
- bench passthrough lives in ``tests/test_bench.py`` (``degraded_passthrough``).

Every test reaches the real boundary: real ``ck stats collect`` subprocesses over
``FakeClaudeHome`` stores, and real ``derive_issue_codes`` / ``build_shareable``
calls. No mocks of the project's own code. Project modules are imported inside
test bodies only so collection never depends on unimplemented code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from stats_helpers import (
    FakeClaudeHome,
    fresh_env,
    git_project,
    implement_result,
    load_record,
    stats,
)

pytestmark = pytest.mark.usefixtures("no_network")


@pytest.fixture
def env():
    with fresh_env() as e:
        yield e


def _world(env, **kwargs: Any):
    proj = git_project(env.root)
    home = FakeClaudeHome(env.claude)
    sess = home.session(cwd=proj)
    return home, sess, proj


def _collect_run(env, proj, run_id: str):
    r = stats("collect", "--run", run_id, env=env, cwd=proj)
    assert r.exit_code == 0, r.output
    return load_record(env.xdg, run_id)


def _fallback_entries() -> list:
    return [
        {"planStep": 2, "trigger": "start", "source": "deps", "ready": ["3"]},
        {"planStep": 3, "trigger": "landing", "source": "deps", "ready": ["4"]},
    ]


def _shareable(local: Dict[str, Any]) -> Dict[str, Any]:
    from claude_kiro.analytics.share import build_shareable

    return build_shareable(local)


# --------------------------------------------------------------------------- counts


def test_plan_fallbacks_counts_postchange(env):
    """Post-change collected runs carry counts.plan_fallbacks == stats.planFallbacks."""
    _, sess, proj = _world(env)
    run = sess.implement_run(result=implement_result(degraded=True, planFallbacks=_fallback_entries()))
    rec = _collect_run(env, proj, run.run_id)
    assert rec["stats"]["planFallbacks"] == 2
    shared = _shareable(rec)
    assert shared["counts"]["plan_fallbacks"] == 2


def test_plan_fallbacks_counts_prechange_absent(env):
    """Pre-change records produce no plan_fallbacks key; existing figures unchanged."""
    _, sess, proj = _world(env)
    run = sess.implement_run(result=implement_result())
    rec = _collect_run(env, proj, run.run_id)
    assert "planFallbacks" not in rec["stats"]
    assert "planFallbacks" not in rec.get("result", {})
    shared = _shareable(rec)
    assert "plan_fallbacks" not in shared["counts"]
    # Existing figures are unchanged: the standard count keys are still present.
    assert shared["counts"]["plan_steps"] == rec["stats"]["planSteps"]
    assert shared["counts"]["plan_failures"] == rec["stats"]["planFailures"]


# --------------------------------------------------------------------------- issue code


def test_planning_fallback_code_emitted(env):
    """derive_issue_codes emits planning_fallback iff stats.planFallbacks > 0."""
    from claude_kiro.analytics.issues import derive_issue_codes

    _, sess, proj = _world(env)
    run = sess.implement_run(result=implement_result(degraded=True, planFallbacks=_fallback_entries()))
    rec = _collect_run(env, proj, run.run_id)
    local = rec.get("result") or {}
    local = {**local, "stats": rec.get("stats", {})}
    assert "planning_fallback" in derive_issue_codes(local)

    run2 = sess.implement_run(result=implement_result())
    rec2 = _collect_run(env, proj, run2.run_id)
    local2 = rec2.get("result") or {}
    local2 = {**local2, "stats": rec2.get("stats", {})}
    assert "planning_fallback" not in derive_issue_codes(local2)


def test_planning_fallback_code_planning_failed_unchanged(env):
    """Existing planning_failed behaviour on planFailures is unchanged."""
    from claude_kiro.analytics.issues import derive_issue_codes

    _, sess, proj = _world(env)
    failed = implement_result()
    failed["stats"] = {**failed["stats"], "planFailures": 2}
    run = sess.implement_run(result=failed)
    rec = _collect_run(env, proj, run.run_id)
    local = {**(rec.get("result") or {}), "stats": rec.get("stats", {})}
    codes = derive_issue_codes(local)
    assert "planning_failed" in codes
    assert "planning_fallback" not in codes

    both = implement_result(degraded=True, planFallbacks=_fallback_entries())
    both["stats"] = {**both["stats"], "planFailures": 1}
    run2 = sess.implement_run(result=both)
    rec2 = _collect_run(env, proj, run2.run_id)
    local2 = {**(rec2.get("result") or {}), "stats": rec2.get("stats", {})}
    codes2 = derive_issue_codes(local2)
    assert "planning_fallback" in codes2
    assert "planning_failed" in codes2


def test_planning_fallback_code_shareable(env):
    """Collected shareable issues include planning_fallback for fallback runs."""
    _, sess, proj = _world(env)
    run = sess.implement_run(result=implement_result(degraded=True, planFallbacks=_fallback_entries()))
    rec = _collect_run(env, proj, run.run_id)
    shared = _shareable(rec)
    assert "planning_fallback" in shared["issues"]

    run2 = sess.implement_run(result=implement_result())
    rec2 = _collect_run(env, proj, run2.run_id)
    shared2 = _shareable(rec2)
    assert "planning_fallback" not in shared2["issues"]


# --------------------------------------------------------------------------- TC-23..TC-28 (Task 29)
#
# CLI examples for overhead visibility, all over real `ck stats` subprocesses on
# FakeClaudeHome stores. Fail until Tasks 28 + 33: no `degraded` /
# `plan_fallbacks` / overhead-share fields exist in records or show output yet.
# Shapes asserted here are the design.md contract: text lines
# `plan fallbacks: <n> (steps ...; sources ...)` (or `not recorded`) and
# `overhead (plan/review/land share): <pct>%` (or `not recorded`, warn suffix
# only over threshold); JSON `planFallbacks: {recorded, count, entries[]}` and
# `overhead: {recorded, share, threshold, warned}` with identical values.

TC23_ENTRIES = [
    {"planStep": 2, "trigger": "start", "source": "deps", "ready": ["3"]},
    {"planStep": 3, "trigger": "landing", "source": "last-good", "ready": ["4"]},
]


def _tc23_agents() -> list:
    """One plan / task / review / land agent with exact durations.

    byRole ms: plan 60s, implementer 120s, reviewer 60s, land 60s; total 300s,
    so the plan/review/land share is exactly 0.6 (60%, warned at 0.5).
    """
    from stats_helpers import AgentSpec

    return [
        AgentSpec("plan 1", duration_ms=60_000),
        AgentSpec("task 1", duration_ms=120_000),
        AgentSpec("review 1", duration_ms=60_000),
        AgentSpec("land 1", duration_ms=60_000),
    ]


def _collect_degraded(env, proj, sess, run_id: str, entries: list, agents: list) -> str:
    run = sess.implement_run(run_id, result=implement_result(degraded=True, planFallbacks=entries), agents=agents)
    r = stats("collect", "--run", run.run_id, env=env, cwd=proj)
    assert r.exit_code == 0, r.output
    return run.run_id


def _show_text(env, run_id: str) -> str:
    out = stats("show", run_id, env=env)
    assert out.exit_code == 0, out.output
    return out.stdout


def _show_json(env, run_id: str) -> dict:
    out = stats("show", run_id, "--json", env=env)
    assert out.exit_code == 0, out.output
    return json.loads(out.stdout)


def _find_line(text: str, *needles: str) -> Optional[str]:
    for line in text.splitlines():
        if all(n.lower() in line.lower() for n in needles):
            return line
    return None


def test_tc23_degraded_report_names_mark_count_and_triggers(env):
    """TC-23 (5.1): degraded run with 2 fallbacks at plan 2 / plan 3 -> report
    names the degraded mark, the count (2), and the triggering attempts."""
    _, sess, proj = _world(env)
    rid = _collect_degraded(env, proj, sess, "wf_tc23_degraded", TC23_ENTRIES, _tc23_agents())

    text = _show_text(env, rid)
    low = text.lower()
    assert "degraded" in low, f"no degraded mark in show output:\n{text}"
    fb = _find_line(text, "plan fallbacks")
    assert fb is not None, f"no `plan fallbacks:` line in show output:\n{text}"
    assert "2" in fb, f"fallback count 2 missing from line {fb!r}:\n{text}"
    assert "2" in fb and "3" in fb, f"triggering plan steps 2 and 3 missing from line {fb!r}:\n{text}"
    assert "deps" in fb and "last-good" in fb, f"fallback sources missing from line {fb!r}:\n{text}"

    data = _show_json(env, rid)
    pf = data.get("planFallbacks")
    assert isinstance(pf, dict) and pf.get("recorded") is True, (
        f"no recorded planFallbacks in show --json for degraded run: {json.dumps(data.get('planFallbacks'))}"
    )
    assert pf.get("count") == 2, f"fallback count != 2 in show --json: {pf}"
    steps = sorted(e.get("planStep") for e in pf.get("entries") or [])
    assert steps == [2, 3], f"triggering plan steps != [2, 3] in show --json: {pf}"
    assert data.get("degraded") is True or "degraded" in json.dumps(data).lower(), (
        "no degraded mark in show --json"
    )


def test_tc24_overhead_share_beside_fallback_count(env):
    """TC-24 (5.2): the plan/review/landing agent share of agent time appears
    next to the `plan_fallbacks` count."""
    _, sess, proj = _world(env)
    rid = _collect_degraded(env, proj, sess, "wf_tc24_overhead", TC23_ENTRIES, _tc23_agents())

    text = _show_text(env, rid)
    fb = _find_line(text, "plan fallbacks")
    assert fb is not None, f"no `plan fallbacks:` line in show output:\n{text}"
    ov = _find_line(text, "overhead", "plan/review/land share")
    assert ov is not None, f"no `overhead (plan/review/land share):` line in show output:\n{text}"
    import re

    assert re.search(r"60\s*%", ov), f"overhead share 60% missing from line {ov!r}:\n{text}"

    data = _show_json(env, rid)
    oh = data.get("overhead")
    assert isinstance(oh, dict) and oh.get("recorded") is True, (
        f"no recorded overhead in show --json: {json.dumps(data.get('overhead'))}"
    )
    assert oh.get("share") == pytest.approx(0.6), f"overhead share != 0.6 in show --json: {oh}"
    assert oh.get("threshold") == pytest.approx(0.5), f"overhead threshold != 0.5 in show --json: {oh}"


def test_tc25_json_equals_text_view_numbers(env):
    """TC-25 (5.3): `--json` fallback counts and overhead shares equal the
    human-readable values parsed from the text view."""
    import re

    _, sess, proj = _world(env)
    rid = _collect_degraded(env, proj, sess, "wf_tc25_agree", TC23_ENTRIES, _tc23_agents())

    text = _show_text(env, rid)
    fb = _find_line(text, "plan fallbacks")
    assert fb is not None, f"no `plan fallbacks:` line in show output:\n{text}"
    m_count = re.search(r"plan fallbacks:\s*(\d+)", text, re.IGNORECASE)
    assert m_count, f"no fallback count parses from {fb!r}:\n{text}"
    ov = _find_line(text, "overhead", "plan/review/land share")
    assert ov is not None, f"no overhead line in show output:\n{text}"
    m_share = re.search(r"(\d+(?:\.\d+)?)\s*%", ov)
    assert m_share, f"no overhead percent parses from {ov!r}:\n{text}"

    data = _show_json(env, rid)
    pf = data.get("planFallbacks") or {}
    oh = data.get("overhead") or {}
    assert pf.get("count") == int(m_count.group(1)), (
        f"json count {pf.get('count')} != text count {m_count.group(1)}"
    )
    assert oh.get("share") == pytest.approx(float(m_share.group(1)) / 100, abs=0.005), (
        f"json share {oh.get('share')} != text share {m_share.group(1)}%"
    )


def test_tc26_forced_overhead_warns_without_halting(env):
    """TC-26 (5.4, CLI half): a run forced above the overhead threshold still
    completes with no stop reason naming overhead; the report flags the warning."""
    from stats_helpers import AgentSpec

    _, sess, proj = _world(env)
    agents = (
        [AgentSpec(f"plan {i}", duration_ms=60_000) for i in (1, 2, 3)]
        + [AgentSpec(f"review {i}", duration_ms=60_000) for i in (1, 2, 3)]
        + [AgentSpec("land 1", duration_ms=60_000), AgentSpec("land 2", duration_ms=60_000)]
        + [AgentSpec("task 1", duration_ms=1_000)]
    )
    entries = [{"planStep": 2, "trigger": "start", "source": "deps", "ready": ["2"]}]
    rid = _collect_degraded(env, proj, sess, "wf_tc26_overhead", entries, agents)

    text = _show_text(env, rid)
    ov = _find_line(text, "overhead", "plan/review/land share")
    assert ov is not None, f"no overhead line in show output:\n{text}"
    assert "warn" in ov.lower(), f"no warn flag on over-threshold overhead line {ov!r}:\n{text}"

    data = _show_json(env, rid)
    oh = data.get("overhead") or {}
    assert oh.get("share") is not None and oh["share"] > 0.5, f"forced share not over threshold: {oh}"
    assert oh.get("warned") is True, f"over-threshold run not warned in show --json: {oh}"
    assert data.get("outcome") == "complete", f"forced-overhead run did not complete: {data.get('outcome')}"
    blob = json.dumps({k: data.get(k) for k in ("outcome", "reason", "stopReason", "halted", "failed")})
    assert "overhead" not in blob.lower(), f"stop reason names overhead (must be warn-only): {blob}"


def _plant_predet(env) -> dict:
    runs_dir = Path(env.xdg) / "claude-kiro" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    src = Path(__file__).parent / "fixtures" / "stats" / "pre-deterministic-record.json"
    raw = src.read_bytes()
    (runs_dir / "wf_predet.json").write_bytes(raw)
    return json.loads(raw)


def test_tc27_prechange_record_not_recorded_figures_unchanged(env):
    """TC-27 (5.5): the pre-change fixture (no fallback/overhead data) shows
    `not recorded` for the new sections with every existing figure unchanged."""
    rec = _plant_predet(env)

    text = stats("show", "wf_predet", env=env)
    assert text.exit_code == 0, text.output
    fb = _find_line(text.stdout, "plan fallbacks")
    assert fb is not None, f"no `plan fallbacks:` line in show output:\n{text.stdout}"
    assert "not recorded" in fb.lower(), f"pre-change fallback line must read not recorded: {fb!r}"
    ov = _find_line(text.stdout, "overhead")
    assert ov is not None, f"no overhead line in show output:\n{text.stdout}"
    assert "not recorded" in ov.lower(), f"pre-change overhead line must read not recorded: {ov!r}"
    # Existing figures print unchanged: values straight from the stored record.
    assert "wf_predet" in text.stdout and "complete" in text.stdout
    assert "2 merged" in text.stdout and "0 failed" in text.stdout and "2 total" in text.stdout
    assert "12.0 min" in text.stdout, f"wall minutes changed for pre-change record:\n{text.stdout}"

    out = stats("show", "wf_predet", "--json", env=env)
    assert out.exit_code == 0, out.output
    data = json.loads(out.stdout)
    pf = data.get("planFallbacks") or {}
    assert pf.get("recorded") is False and pf.get("count") is None, (
        f"pre-change planFallbacks must be unrecorded: {pf}"
    )
    oh = data.get("overhead") or {}
    assert oh.get("recorded") is False and oh.get("share") is None and oh.get("warned") is False, (
        f"pre-change overhead must be unrecorded: {oh}"
    )
    assert data.get("outcome") == rec["outcome"] and data.get("wallMs") == rec["wallMs"]
    assert isinstance(data.get("tasks"), list) and len(data["tasks"]) == len(rec["tasks"]) == 2
    assert (data.get("runId") or data.get("run_id")) in ("wf_predet",), "run id changed for pre-change record"


def test_tc28_old_record_bytes_identical_beside_new_run(env):
    """TC-28 (5.6): old + new records in one store; the old record reads without
    re-collection and its figures are byte-identical before/after."""
    _plant_predet(env)
    old_path = Path(env.xdg) / "claude-kiro" / "runs" / "wf_predet.json"
    before = old_path.read_bytes()

    _, sess, proj = _world(env)
    _collect_degraded(env, proj, sess, "wf_tc28_new", TC23_ENTRIES, _tc23_agents())
    r = stats("collect", "--all", env=env, cwd=proj)
    assert r.exit_code == 0, r.output

    assert old_path.read_bytes() == before, "pre-change record bytes changed by collect (no migration rewrites)"
    text = stats("show", "wf_predet", env=env)
    assert text.exit_code == 0, text.output
    assert "wf_predet" in text.stdout and "not recorded" in text.stdout.lower()
    new_text = _show_text(env, "wf_tc28_new")
    assert _find_line(new_text, "plan fallbacks") is not None
