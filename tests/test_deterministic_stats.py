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
import re
from pathlib import Path
from typing import Any, Dict, Optional

import hypothesis.strategies as st
import pytest
from hypothesis import HealthCheck, given, settings

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


# --------------------------------------------------------------------------- P-11..P-13 (Task 30)
#
# Property tests for overhead stats (requirements 5.1-5.6; seams
# `overheadShare`/`overheadWarning` owned by Task 33). They fail until Task 33
# lands, for the right reason: `overheadShare`/`overheadWarning` do not exist
# yet (AttributeError on first seam call) and `ck stats show` prints no
# `plan fallbacks:` / `overhead (plan/review/land share):` lines. Project
# modules are imported inside test bodies so collection never depends on the
# missing code.
#
# Seam-shape note: the tests call `overheadShare(byRoleMs)` with the
# collected record's own `byRole` ms cells, and single-argument
# `overheadWarning(share)`, exactly as Task 33 / design.md specify
# (`overheadShare(byRoleMs) -> float | None`,
# `overheadWarning(share) -> bool`, threshold mirrored at 0.5). The looser
# `overheadShare(record)` phrasing in the Task 30 block and the two-argument
# `overhead_warning(share, threshold)` sketch in the test-plan Harness
# Contract are read as naming the same seam: the record's byRole ms cells
# are the record projection the seam needs, and the threshold levels
# {0%, 50%, 100%, absent} are covered as share positions against the
# published threshold (0%, exactly-at-threshold boundary, 100%, and
# absent-data runs that never warn).
#
# Cost note: every Hypothesis example calls the pure seam (microseconds) and
# exactly one cached CLI row-check per table row per test call. The CLI table
# (collect + show text + show --json over a FakeClaudeHome store) therefore
# runs once per row however many examples Hypothesis draws; the cache is keyed
# by the test call's temp root (`env` itself doubles as subprocess environ
# overrides, so the cache lives module-level, never on `env`). The shared
# `env` across examples is exactly what the cache relies on, hence
# `suppress_health_check=[HealthCheck.function_scoped_fixture]` alongside the
# mandated deterministic settings.

_OVERHEAD_ROLES = ("plan", "reviewer", "land")

_P11_FB = [
    {"planStep": 2, "trigger": "start", "source": "deps", "ready": ["3"]},
    {"planStep": 3, "trigger": "landing", "source": "last-good", "ready": ["4"]},
]


def _show_mod():
    """The stats view module owning the Task 33 seams (AttributeError until Task 33)."""
    from claude_kiro.analytics import show as mod

    return mod


def _by_role_ms(record: Dict[str, Any]) -> Dict[str, float]:
    by_role = record.get("byRole") or {}
    out: Dict[str, float] = {}
    for role, cell in by_role.items():
        if isinstance(cell, dict) and isinstance(cell.get("ms"), (int, float)) and not isinstance(cell.get("ms"), bool):
            out[role] = float(cell["ms"])
    return out


_TASK30_CLI: Dict[str, Any] = {}


def _task30_cache(env: Dict[str, Any]) -> Dict[str, Any]:
    # Module-level, keyed by the test call's temp root: `env` itself doubles
    # as subprocess environ overrides, so non-string cache values must never
    # live on it.
    return _TASK30_CLI.setdefault(str(env.root), {})


def _cached_world(env: Any):
    """One FakeClaudeHome + git project per test call (shared across rows and
    Hypothesis examples; `_world` is not idempotent)."""
    cache = _task30_cache(env)
    if "world" not in cache:
        cache["world"] = _world(env)
    return cache["world"]


def _parse_pct(line: Optional[str]) -> Optional[float]:
    if not line:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
    return float(m.group(1)) / 100 if m else None


def _parse_fb_count(line: Optional[str]) -> Optional[int]:
    if not line:
        return None
    m = re.search(r"plan fallbacks:\s*(\d+)", line, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _p11_rows() -> list:
    from stats_helpers import AgentSpec, TaskSpec

    full = [
        AgentSpec("plan 1", duration_ms=60_000),
        AgentSpec("task 1", duration_ms=120_000),
        AgentSpec("review 1", duration_ms=60_000),
        AgentSpec("land 1", duration_ms=60_000),
    ]
    return [
        {"name": "mix_full", "agents": full,
         "result": lambda: implement_result(degraded=True, planFallbacks=list(_P11_FB))},
        {"name": "plan_heavy", "agents": [AgentSpec("plan 1", duration_ms=90_000), AgentSpec("plan 2", duration_ms=90_000),
                                          AgentSpec("task 1", duration_ms=60_000)],
         "result": lambda: implement_result(degraded=True, planFallbacks=[dict(_P11_FB[0])])},
        {"name": "review_land_only", "agents": [AgentSpec("task 1", duration_ms=60_000), AgentSpec("review 1", duration_ms=60_000),
                                                AgentSpec("land 1", duration_ms=60_000)],
         "result": lambda: implement_result(degraded=True, planFallbacks=[dict(_P11_FB[1])])},
        {"name": "clean_zero", "agents": full,
         "result": lambda: implement_result(degraded=False, planFallbacks=[])},
        {"name": "no_fields", "agents": full, "result": lambda: implement_result()},
        {"name": "zero_total", "agents": [AgentSpec("plan 1", duration_ms=0), AgentSpec("task 1", duration_ms=0)],
         "result": lambda: implement_result(degraded=True, planFallbacks=[dict(_P11_FB[0])])},
        {"name": "zero_overhead_share",
         "agents": [AgentSpec("plan 1", duration_ms=0), AgentSpec("review 1", duration_ms=0),
                    AgentSpec("land 1", duration_ms=0), AgentSpec("task 1", duration_ms=60_000)],
         "result": lambda: implement_result(degraded=True, planFallbacks=[dict(_P11_FB[0])])},
        {"name": "missing_started", "agents": [AgentSpec("plan 1", omit_started=True), AgentSpec("task 1", omit_started=True),
                                               AgentSpec("review 1", omit_started=True), AgentSpec("land 1", omit_started=True)],
         "result": lambda: implement_result(degraded=True, planFallbacks=list(_P11_FB))},
        {"name": "no_meta", "agents": [AgentSpec("plan 1", meta=False), AgentSpec("task 1", meta=False),
                                       AgentSpec("review 1", meta=False), AgentSpec("land 1", meta=False)],
         "result": lambda: implement_result(degraded=True, planFallbacks=list(_P11_FB))},
        {"name": "failed_task",
         "agents": [AgentSpec("plan 1", duration_ms=60_000), AgentSpec("task 1", duration_ms=60_000),
                    AgentSpec("task 2", duration_ms=60_000)],
         "result": lambda: implement_result(tasks=[TaskSpec(task="1", ok=True), TaskSpec(task="2", ok=False, reason="gate red")],
                                            degraded=True, planFallbacks=[dict(_P11_FB[0])])},
    ]


def _p11_check_row(env: Any, row: dict) -> None:
    """One table row: collect, then text/JSON/seam three-way agreement (cached per test call)."""
    from claude_kiro.analytics.show import overheadShare, overheadWarning

    cache = _task30_cache(env)
    key = ("p11", row["name"])
    if key not in cache:
        _, sess, proj = _cached_world(env)
        rid = f"wf_p11_{row['name']}"
        run = sess.implement_run(rid, result=row["result"](), agents=row["agents"])
        r = stats("collect", "--run", run.run_id, env=env, cwd=proj)
        assert r.exit_code == 0, r.output
        cache[key] = (load_record(env.xdg, run.run_id), _show_text(env, run.run_id), _show_json(env, run.run_id))
    rec, text, data = cache[key]

    by_ms = _by_role_ms(rec)
    expected = overheadShare(by_ms)
    assert expected is None or 0.0 <= expected <= 1.0, f"seam share out of range for {row['name']}: {expected}"

    entries = (rec.get("result") or {}).get("planFallbacks")
    recorded_fb = entries is not None
    pf = data.get("planFallbacks") or {}
    assert pf.get("recorded") is recorded_fb, f"row {row['name']}: json recorded={pf.get('recorded')} != fields-present={recorded_fb}"
    fb_line = _find_line(text, "plan fallbacks")
    assert fb_line is not None, f"row {row['name']}: no `plan fallbacks:` line in show output:\n{text}"
    if recorded_fb:
        assert pf.get("count") == len(entries) == _parse_fb_count(fb_line), (
            f"row {row['name']}: json count {pf.get('count')} != text count {_parse_fb_count(fb_line)} != entries {len(entries)}"
        )
        assert [e.get("planStep") for e in pf.get("entries") or []] == [e.get("planStep") for e in entries]
    else:
        assert pf.get("count") is None, f"row {row['name']}: unrecorded json count must be null: {pf}"
        assert "not recorded" in fb_line.lower(), f"row {row['name']}: unrecorded line must read not recorded: {fb_line!r}"

    oh = data.get("overhead") or {}
    ov_line = _find_line(text, "overhead", "plan/review/land share")
    assert ov_line is not None, f"row {row['name']}: no `overhead (plan/review/land share):` line in show output:\n{text}"
    if recorded_fb and expected is not None:
        assert oh.get("recorded") is True, f"row {row['name']}: overhead must be recorded: {oh}"
        assert oh.get("share") == pytest.approx(expected), f"row {row['name']}: json share {oh.get('share')} != seam {expected}"
        assert oh.get("threshold") == pytest.approx(0.5), f"row {row['name']}: threshold != 0.5: {oh}"
        text_share = _parse_pct(ov_line)
        assert text_share is not None and text_share == pytest.approx(oh["share"], abs=0.01), (
            f"row {row['name']}: text share {text_share} != json share {oh.get('share')}: {ov_line!r}"
        )
        assert oh.get("warned") == overheadWarning(oh.get("share")), (
            f"row {row['name']}: json warned {oh.get('warned')} != seam on json share {oh.get('share')}"
        )
        if all(role in by_ms for role in _OVERHEAD_ROLES) and sum(by_ms.values()) > 0:
            assert oh.get("share") == pytest.approx(
                sum(by_ms[r] for r in _OVERHEAD_ROLES) / sum(by_ms.values())), (
                    f"row {row['name']}: plan/review/land-present run must show the combined three-role share")
    else:
        assert oh.get("share") is None, f"row {row['name']}: unrecorded/empty overhead share must be null: {oh}"
        assert oh.get("warned") is False, f"row {row['name']}: unrecorded/empty overhead must not warn: {oh}"
        assert "not recorded" in ov_line.lower() or _parse_pct(ov_line) is None, (
            f"row {row['name']}: unrecorded overhead line must read not recorded: {ov_line!r}")


@given(
    ms=st.dictionaries(st.sampled_from(["plan", "reviewer", "land", "implementer", "fixer", "resolver", "other"]),
                       st.integers(0, 7_200_000), min_size=1, max_size=7),
    idx=st.integers(0, 9),
)
@settings(derandomize=True, max_examples=200, database=None,
         suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_stats_text_json_agree(env, ms, idx):
    """P-11 (5.2, 5.3): any collected run -> `show --json` fallback counts +
    overhead shares equal the text-view parsed numbers and `overheadShare`
    on the same record's byRole ms; plan/review/land-present runs show the
    combined three-role share. Table-driven FakeClaudeHome rows (agent mixes,
    zero-duration, missing `startedAt`, meta-less agents, failed tasks) x
    generated byRole ms maps for the pure seam."""
    from claude_kiro.analytics.show import overheadShare

    total = sum(ms.values())
    share = overheadShare(dict(ms))
    if total <= 0:
        assert share is None, f"empty byRole ms must give None, got {share} for {ms}"
    else:
        want = sum(ms.get(r, 0) for r in _OVERHEAD_ROLES) / total
        assert share == pytest.approx(want), f"seam {share} != manual {want} for {ms}"
        assert 0.0 <= share <= 1.0, f"share out of range: {share} for {ms}"
    _p11_check_row(env, _p11_rows()[idx])


def _p12_pair_rows() -> list:
    from stats_helpers import AgentSpec

    labels = ["plan 1", "task 1", "review 1", "land 1"]
    over = [AgentSpec("plan 1", duration_ms=60_000), AgentSpec("task 1", duration_ms=120_000),
            AgentSpec("review 1", duration_ms=60_000), AgentSpec("land 1", duration_ms=60_000)]
    under = [AgentSpec("plan 1", duration_ms=10_000), AgentSpec("task 1", duration_ms=300_000),
             AgentSpec("review 1", duration_ms=10_000), AgentSpec("land 1", duration_ms=10_000)]
    entries = [dict(_P11_FB[0])]
    return [
        {"name": "over", "agents": over, "labels": labels,
         "result": lambda: implement_result(degraded=True, planFallbacks=list(entries)), "warned": True},
        {"name": "under", "agents": under, "labels": labels,
         "result": lambda: implement_result(degraded=True, planFallbacks=list(entries)), "warned": False},
        {"name": "zero", "agents": [AgentSpec("task 1", duration_ms=60_000), AgentSpec("task 2", duration_ms=60_000)],
         "labels": ["task 1", "task 2"], "result": lambda: implement_result(degraded=True, planFallbacks=list(entries)),
         "warned": False},
        {"name": "half", "agents": [AgentSpec("plan 1", duration_ms=60_000), AgentSpec("review 1", duration_ms=30_000),
                                   AgentSpec("land 1", duration_ms=30_000), AgentSpec("task 1", duration_ms=120_000)],
         "labels": ["plan 1", "review 1", "land 1", "task 1"],
         "result": lambda: implement_result(degraded=True, planFallbacks=list(entries)), "warned": False},
        {"name": "full", "agents": [AgentSpec("plan 1", duration_ms=60_000), AgentSpec("review 1", duration_ms=60_000),
                                   AgentSpec("land 1", duration_ms=60_000)],
         "labels": ["plan 1", "review 1", "land 1"],
         "result": lambda: implement_result(degraded=True, planFallbacks=list(entries)), "warned": True},
    ]


def _p12_collect_pair(env: Any) -> dict:
    """Collect the warn-only pair/table once per test call (cached on env)."""
    from claude_kiro.analytics.show import overheadWarning

    cache = _task30_cache(env)
    if "p12" not in cache:
        _, sess, proj = _cached_world(env)
        out = {}
        for row in _p12_pair_rows():
            rid = f"wf_p12_{row['name']}"
            run = sess.implement_run(rid, result=row["result"](), agents=row["agents"])
            r = stats("collect", "--run", run.run_id, env=env, cwd=proj)
            assert r.exit_code == 0, r.output
            out[row["name"]] = (load_record(env.xdg, run.run_id), _show_text(env, run.run_id), _show_json(env, run.run_id),
                                row)
        src = Path(__file__).parent / "fixtures" / "stats" / "pre-deterministic-record.json"
        runs_dir = Path(env.xdg) / "claude-kiro" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        raw = src.read_bytes()
        (runs_dir / "wf_p12_absent.json").write_bytes(raw)
        out["absent"] = (json.loads(raw), _show_text(env, "wf_p12_absent"), _show_json(env, "wf_p12_absent"), None)
        cache["p12"] = out
    return cache["p12"]


@given(
    s=st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
                st.sampled_from([0.0, 0.5, 1.0])),
    name=st.sampled_from(["over", "under", "zero", "half", "full", "absent"]),
)
@settings(derandomize=True, max_examples=200, database=None,
         suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_warn_only_metamorphic(env, s, name):
    """P-12 (5.4, metamorphic warn-only): warning iff share > threshold, and
    flagging never changes scheduling: the flagged run's merged set, stop
    reason, and agent sequence equal the threshold-disabled (unwarned) run.
    Generated shares 0-100% + None x table rows (over-threshold, boundary
    50%, zero, 100%, absent-data) over real `ck stats` CLI + direct
    `overheadWarning` calls."""
    from claude_kiro.analytics.show import overheadWarning

    assert overheadWarning(s) == (s is not None and s > 0.5), f"warn-only threshold compare wrong for share={s}"

    table = _p12_collect_pair(env)
    rec, text, data, row = table[name]
    oh = data.get("overhead") or {}
    assert oh.get("threshold", 0.5) == pytest.approx(0.5), f"row {name}: published threshold moved: {oh}"
    if name == "absent":
        assert oh.get("recorded") is False and oh.get("share") is None and oh.get("warned") is False, (
            f"absent-data row must never warn: {oh}")
        ov = _find_line(text, "overhead")
        assert ov is not None and "not recorded" in ov.lower(), f"absent-data overhead line: {ov!r}"
    else:
        assert row is not None
        assert oh.get("recorded") is True and oh.get("share") is not None, f"row {name}: overhead must be recorded: {oh}"
        assert oh.get("warned") is row["warned"] == overheadWarning(oh.get("share")), (
            f"row {name}: json warned {oh.get('warned')} != expected {row['warned']}")
        assert [a.get("label") for a in rec.get("agents", [])] == row["labels"], (
            f"row {name}: agent sequence changed")

    over_rec, _, over_data, _ = table["over"]
    under_rec, _, under_data, _ = table["under"]
    for key in ("merged", "failed", "notStarted"):
        assert (over_rec.get("result") or {}).get(key) == (under_rec.get("result") or {}).get(key), (
            f"warn-only violation: merged set differs: over={(over_rec.get('result') or {}).get(key)} "
            f"under={(under_rec.get('result') or {}).get(key)}")
    assert over_rec.get("outcome") == under_rec.get("outcome"), "warn-only violation: outcome differs"
    assert (over_rec.get("result") or {}).get("reason") == (under_rec.get("result") or {}).get("reason"), (
        "warn-only violation: stop reason differs")
    assert [a.get("label") for a in over_rec.get("agents", [])] == [a.get("label") for a in under_rec.get("agents", [])], (
        "warn-only violation: agent sequence differs")
    assert (over_data.get("overhead") or {}).get("warned") is True, "over-threshold pair member must warn"
    assert (under_data.get("overhead") or {}).get("warned") is False, "under-threshold pair member must not warn"
    assert "overhead" not in json.dumps({"o": over_data.get("outcome"), "r": over_data.get("reason")}).lower(), (
        "warn-only violation: stop reason names overhead")


def _p13_rows() -> list:
    from stats_helpers import AgentSpec, TaskSpec

    agents = [AgentSpec("plan 1", duration_ms=60_000), AgentSpec("task 1", duration_ms=60_000),
              AgentSpec("review 1", duration_ms=60_000), AgentSpec("land 1", duration_ms=60_000)]
    red_landings = [{"tasks": ["1", "2"], "size": 2, "window": 2, "merged": ["1"], "red": ["2"], "conflict": [],
                     "deferred": [], "other": [], "gates": 1, "bisected": False, "minutes": 1.0}]
    return [
        {"name": "failed-task", "agents": agents,
         "result": lambda: implement_result(tasks=[TaskSpec(task="1", ok=True), TaskSpec(task="2", ok=False, reason="gate red")],
                                            degraded=True, planFallbacks=[dict(_P11_FB[0])])},
        {"name": "revise", "agents": agents,
         "result": lambda: implement_result(tasks=[TaskSpec(task="1", rounds=2, findings=[{"severity": "blocking", "issue": "x"}]),
                                                   TaskSpec(task="2")],
                                            degraded=True, planFallbacks=[dict(_P11_FB[0])])},
        {"name": "red-landing",
         "agents": [AgentSpec("plan 1", duration_ms=60_000), AgentSpec("task 1", duration_ms=60_000),
                    AgentSpec("task 2", duration_ms=60_000), AgentSpec("land 1", duration_ms=60_000)],
         "result": lambda: implement_result(tasks=[TaskSpec(task="1", ok=True), TaskSpec(task="2", ok=False, reason="gate red")],
                                            landings=[dict(l) for l in red_landings],
                                            degraded=True, planFallbacks=[dict(_P11_FB[0])])},
        {"name": "halted", "agents": agents,
         "result": lambda: implement_result(tasks=[TaskSpec(task="1", ok=False, reason="gate red"),
                                                   TaskSpec(task="2", ok=False, reason="gate red")],
                                            outcome="halted", degraded=True, planFallbacks=list(_P11_FB))},
        {"name": "no-meta", "agents": [AgentSpec("plan 1", meta=False), AgentSpec("task 1", meta=False)],
         "result": lambda: implement_result(degraded=True, planFallbacks=[dict(_P11_FB[0])])},
        {"name": "zero-duration", "agents": [AgentSpec("plan 1", duration_ms=0), AgentSpec("task 1", duration_ms=0)],
         "result": lambda: implement_result(degraded=True, planFallbacks=[dict(_P11_FB[0])])},
        {"name": "missing-started", "agents": [AgentSpec("plan 1", omit_started=True), AgentSpec("task 1", omit_started=True)],
         "result": lambda: implement_result(degraded=True, planFallbacks=[dict(_P11_FB[0])])},
        {"name": "fixes",
         "agents": agents,
         "result": lambda: implement_result(tasks=[TaskSpec(task="1", fixes=[{"round": 1, "ok": True}]), TaskSpec(task="2")],
                                            degraded=True, planFallbacks=[dict(_P11_FB[0])])},
        {"name": "multi-wave",
         "agents": agents + [AgentSpec("task 3", duration_ms=60_000)],
         "result": lambda: implement_result(tasks=[TaskSpec(task="1", wave=1), TaskSpec(task="2", wave=1), TaskSpec(task="3", wave=2)],
                                            degraded=True, planFallbacks=list(_P11_FB))},
    ]


def _strip_to_prechange(record: Dict[str, Any]) -> Dict[str, Any]:
    import copy

    rec = copy.deepcopy(record)
    if isinstance(rec.get("stats"), dict):
        rec["stats"].pop("planFallbacks", None)
    res = rec.get("result")
    if isinstance(res, dict):
        res.pop("degraded", None)
        res.pop("planFallbacks", None)
    rec.pop("degraded", None)
    rec.pop("planFallbacks", None)
    return rec


def _p13_check_row(env: Any, row: dict) -> None:
    """One table row: degraded runs always show {degraded, count, triggers}; record bytes never rewritten."""
    cache = _task30_cache(env)
    key = ("p13", row["name"])
    if key not in cache:
        _, sess, proj = _cached_world(env)
        rid = f"wf_p13_{row['name']}"
        run = sess.implement_run(rid, result=row["result"](), agents=row["agents"])
        r = stats("collect", "--run", run.run_id, env=env, cwd=proj)
        assert r.exit_code == 0, r.output
        path = Path(env.xdg) / "claude-kiro" / "runs" / f"{run.run_id}.json"
        before = path.read_bytes()
        cache[key] = (run.run_id, path, before, row)
    run_id, path, before, _ = cache[key]

    text = _show_text(env, run_id)
    data = _show_json(env, run_id)
    assert "degraded" in text.lower(), f"row {row['name']}: no degraded mark in show output:\n{text}"
    fb_line = _find_line(text, "plan fallbacks")
    assert fb_line is not None, f"row {row['name']}: no `plan fallbacks:` line in show output:\n{text}"
    entries = (load_record(env.xdg, run_id).get("result") or {}).get("planFallbacks") or []
    count = len(entries)
    assert _parse_fb_count(fb_line) == count, f"row {row['name']}: text count {_parse_fb_count(fb_line)} != {count}"
    for e in entries:
        assert str(e.get("planStep")) in fb_line, f"row {row['name']}: trigger plan step {e.get('planStep')} missing: {fb_line!r}"
        assert str(e.get("source")) in fb_line, f"row {row['name']}: trigger source {e.get('source')} missing: {fb_line!r}"
    pf = data.get("planFallbacks") or {}
    assert pf.get("recorded") is True and pf.get("count") == count, f"row {row['name']}: json planFallbacks: {pf}"
    assert [e.get("planStep") for e in pf.get("entries") or []] == [e.get("planStep") for e in entries]
    assert [e.get("trigger") for e in pf.get("entries") or []] == [e.get("trigger") for e in entries]
    assert data.get("degraded") is True, f"row {row['name']}: json degraded must be true"

    r = stats("collect", "--all", env=env, cwd=Path(env.root) / "project")
    assert r.exit_code == 0, r.output
    assert path.read_bytes() == before, f"row {row['name']}: re-collection rewrote stored record bytes"


@given(idx=st.integers(0, 8))
@settings(derandomize=True, max_examples=200, database=None,
         suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_prechange_not_recorded(env, idx):
    """P-13 (5.1, 5.5, 5.6): any pre-change record -> `not recorded` with
    every pre-existing key unchanged and re-collection never rewriting bytes;
    complement: any post-change degraded run always shows all three of
    {degraded, count, triggers}. Table-driven FakeClaudeHome runs (failed
    task, revise, red landing, halted, meta-less, zero-duration, missing
    timestamps, fixes, multi-wave) x the committed
    `pre-deterministic-record.json` plus stripped-field variants of collected
    post-change records."""
    _p13_check_row(env, _p13_rows()[idx])

    cache = _task30_cache(env)
    if "p13-pre" not in cache:
        _plant_predet(env)
        _, sess, proj = _cached_world(env)
        rid = "wf_p13_stripsrc"
        run = sess.implement_run(rid, result=implement_result(degraded=True, planFallbacks=list(_P11_FB)))
        r = stats("collect", "--run", run.run_id, env=env, cwd=proj)
        assert r.exit_code == 0, r.output
        stripped = _strip_to_prechange(load_record(env.xdg, run.run_id))
        (Path(env.xdg) / "claude-kiro" / "runs" / "wf_p13_stripped.json").write_text(json.dumps(stripped))
        cache["p13-pre"] = True

    for planted_id in ("wf_predet", "wf_p13_stripped"):
        path = Path(env.xdg) / "claude-kiro" / "runs" / f"{planted_id}.json"
        before = path.read_bytes()
        stored = json.loads(before)
        text = _show_text(env, planted_id)
        fb = _find_line(text, "plan fallbacks")
        assert fb is not None and "not recorded" in fb.lower(), f"{planted_id}: {fb!r}"
        ov = _find_line(text, "overhead")
        assert ov is not None and "not recorded" in ov.lower(), f"{planted_id}: {ov!r}"
        data = _show_json(env, planted_id)
        pf = data.get("planFallbacks") or {}
        assert pf.get("recorded") is False and pf.get("count") is None, f"{planted_id}: {pf}"
        oh = data.get("overhead") or {}
        assert oh.get("recorded") is False and oh.get("share") is None and oh.get("warned") is False, (
            f"{planted_id}: {oh}")
        assert data.get("outcome") == stored["outcome"] and data.get("wallMs") == stored["wallMs"], (
            f"{planted_id}: pre-existing keys changed")
        assert isinstance(data.get("tasks"), list) and len(data["tasks"]) == len(stored["tasks"])
        r = stats("collect", "--all", env=env, cwd=Path(env.root) / "project")
        assert r.exit_code == 0, r.output
        assert path.read_bytes() == before, f"{planted_id}: re-collection rewrote stored bytes"
        _show_text(env, planted_id)
        assert path.read_bytes() == before, f"{planted_id}: show rewrote stored bytes"


PROPERTIES = {
    "P-11": "test_stats_text_json_agree",
    "P-12": "test_warn_only_metamorphic",
    "P-13": "test_prechange_not_recorded",
}


# --------------------------------------------------------------------------- Task 31: red-team warn-only (5.4)
#
# Adversarial cases for the warn-only budget (requirement 5.4: SHALL NOT halt,
# fail or retry because of overhead). Every case reaches the real boundary:
# real `ck stats collect`/`show` subprocesses over FakeClaudeHome stores plus
# direct calls to the Task 33 seams (`overheadShare`/`overheadWarning`). No
# mocks of the project's own code; project modules are imported inside test
# bodies so collection never depends on the missing code.
#
# They fail until Task 33 lands, for the right reason: `overheadShare` /
# `overheadWarning` do not exist yet (AttributeError on first seam call) and
# `ck stats show` prints no `overhead (plan/review/land share):` line.


_RT_FB = [
    {"planStep": 2, "trigger": "start", "source": "deps", "ready": ["3"]},
]


def test_redteam_warn_only_exact_threshold_no_warn(env):
    """Share exactly at the published threshold (0.5) warns nothing and the
    run completes with no stop reason naming overhead."""
    from claude_kiro.analytics.show import overheadShare, overheadWarning

    assert overheadWarning(0.5) is False, "share exactly at threshold must not warn"
    assert overheadWarning(0.5001) is True, "share just above threshold must warn"

    from stats_helpers import AgentSpec

    agents = [
        AgentSpec("plan 1", duration_ms=60_000),
        AgentSpec("review 1", duration_ms=30_000),
        AgentSpec("land 1", duration_ms=30_000),
        AgentSpec("task 1", duration_ms=120_000),
    ]
    _, sess, proj = _world(env)
    run = sess.implement_run(
        "wf_rt_exact", result=implement_result(degraded=True, planFallbacks=list(_RT_FB)), agents=agents)
    rec = _collect_run(env, proj, run.run_id)
    assert overheadShare(_by_role_ms(rec)) == pytest.approx(0.5)

    text = _show_text(env, run.run_id)
    data = _show_json(env, run.run_id)
    oh = data.get("overhead") or {}
    assert oh.get("share") == pytest.approx(0.5), f"exact-threshold share != 0.5: {oh}"
    assert oh.get("warned") is False, f"exact-threshold share must not warn: {oh}"
    ov = _find_line(text, "overhead", "plan/review/land share")
    assert ov is not None, f"no overhead line in show output:\n{text}"
    assert "warn" not in ov.lower(), f"exact-threshold line must carry no warn flag: {ov!r}"
    assert data.get("outcome") == "complete"
    blob = json.dumps({k: data.get(k) for k in ("outcome", "reason", "stopReason", "halted", "failed")})
    assert "overhead" not in blob.lower(), f"stop reason names overhead (must be warn-only): {blob}"


def test_redteam_warn_only_all_plan_share_warns_without_halting(env):
    """Share 100% with all-plan agents warns, yet the run still completes
    with its agent sequence intact and no stop reason naming overhead."""
    from claude_kiro.analytics.show import overheadShare, overheadWarning

    assert overheadWarning(1.0) is True, "100% overhead share must warn"

    from stats_helpers import AgentSpec

    agents = [AgentSpec("plan 1", duration_ms=60_000), AgentSpec("plan 2", duration_ms=60_000)]
    _, sess, proj = _world(env)
    run = sess.implement_run(
        "wf_rt_allplan", result=implement_result(degraded=True, planFallbacks=list(_RT_FB)), agents=agents)
    rec = _collect_run(env, proj, run.run_id)
    assert overheadShare(_by_role_ms(rec)) == pytest.approx(1.0)

    text = _show_text(env, run.run_id)
    data = _show_json(env, run.run_id)
    oh = data.get("overhead") or {}
    assert oh.get("warned") is True, f"100% share must warn: {oh}"
    ov = _find_line(text, "overhead", "plan/review/land share")
    assert ov is not None, f"no overhead line in show output:\n{text}"
    assert "warn" in ov.lower(), f"100% share line must carry the warn flag: {ov!r}"
    assert data.get("outcome") == "complete", "all-plan run must complete (warn-only)"
    assert [a.get("label") for a in rec.get("agents", [])] == ["plan 1", "plan 2"], "agent sequence changed"
    blob = json.dumps({k: data.get(k) for k in ("outcome", "reason", "stopReason", "halted", "failed")})
    assert "overhead" not in blob.lower(), f"stop reason names overhead (must be warn-only): {blob}"


def test_redteam_warn_only_missing_share_never_warns(env):
    """A None/missing share never warns — not even at the most permissive
    reading of the threshold — and the run completes with no overhead stop."""
    from claude_kiro.analytics.show import overheadShare, overheadWarning

    assert overheadWarning(None) is False, "missing share must never warn"
    assert overheadShare({}) is None, "empty byRole ms must give None"

    from stats_helpers import AgentSpec

    agents = [AgentSpec("plan 1", duration_ms=0), AgentSpec("task 1", duration_ms=0)]
    _, sess, proj = _world(env)
    run = sess.implement_run(
        "wf_rt_missing", result=implement_result(degraded=True, planFallbacks=list(_RT_FB)), agents=agents)
    rec = _collect_run(env, proj, run.run_id)
    assert overheadShare(_by_role_ms(rec)) is None, "zero-total ms must give None"

    text = _show_text(env, run.run_id)
    data = _show_json(env, run.run_id)
    oh = data.get("overhead") or {}
    assert oh.get("share") is None, f"missing share must stay null: {oh}"
    assert oh.get("warned") is False, f"missing share must never warn: {oh}"
    ov = _find_line(text, "overhead", "plan/review/land share")
    assert ov is not None, f"no overhead line in show output:\n{text}"
    assert "warn" not in ov.lower(), f"missing-share line must carry no warn flag: {ov!r}"
    assert data.get("outcome") == "complete"


def test_redteam_warn_only_threshold_shaped_strings_never_warn(env):
    """Threshold-shaped strings fed back as share values (the system's own
    report wording, percents, bare numbers, bools) must not warn, crash, or
    halt the run: only a real numeric share above 0.5 warns."""
    from claude_kiro.analytics.show import overheadShare, overheadWarning

    for shaped in ("0.5", "50%", "above 50% threshold", "0.61", "1.0", True, False, float("nan")):
        assert overheadWarning(shaped) is False, f"threshold-shaped {shaped!r} must never warn"
    assert overheadShare(
        {"plan": "60000", "reviewer": "60000", "land": "60000", "implementer": "120000"}) is None, (
        "string ms cells must be ignored (share None), not coerced into a warning share")

    _, sess, proj = _world(env)
    run = sess.implement_run("wf_rt_strings", result=implement_result(), agents=_tc23_agents())
    rec = _collect_run(env, proj, run.run_id)
    assert overheadShare(_by_role_ms(rec)) == pytest.approx(0.6)
    data = _show_json(env, run.run_id)
    assert data.get("outcome") == "complete"
    blob = json.dumps({k: data.get(k) for k in ("outcome", "reason", "stopReason", "halted", "failed")})
    assert "overhead" not in blob.lower(), f"stop reason names overhead (must be warn-only): {blob}"


def test_redteam_warn_only_warning_preserves_real_failure(env):
    """An overhead warning coinciding with a real failure must not mask or
    rename it: the warning flags while the outcome stays halted, the failed
    task and its gate-red reason stay visible, and nothing names overhead as
    the stop reason."""
    from claude_kiro.analytics.show import overheadWarning

    from stats_helpers import AgentSpec, TaskSpec

    agents = [
        AgentSpec("plan 1", duration_ms=60_000),
        AgentSpec("task 1", duration_ms=120_000),
        AgentSpec("review 1", duration_ms=60_000),
        AgentSpec("land 1", duration_ms=60_000),
    ]
    tasks = [TaskSpec(task="1", ok=True), TaskSpec(task="2", ok=False, reason="gate red")]
    result = implement_result(tasks=tasks, outcome="halted", degraded=True, planFallbacks=list(_RT_FB))
    _, sess, proj = _world(env)
    run = sess.implement_run("wf_rt_coincident", result=result, agents=agents)
    rec = _collect_run(env, proj, run.run_id)

    from claude_kiro.analytics.show import overheadShare

    share = overheadShare(_by_role_ms(rec))
    assert share is not None and share > 0.5, f"coincident run must be over threshold: {share}"
    assert overheadWarning(share) is True, "over-threshold coincident run must warn"

    text = _show_text(env, run.run_id)
    data = _show_json(env, run.run_id)
    oh = data.get("overhead") or {}
    assert oh.get("warned") is True, f"coincident over-threshold run must warn: {oh}"
    ov = _find_line(text, "overhead", "plan/review/land share")
    assert ov is not None and "warn" in ov.lower(), f"coincident overhead line must warn: {ov!r}"
    assert data.get("outcome") == "halted", "warning must not rename the real halted outcome"
    assert "halted" in text.lower(), "warning must not mask the halted outcome in the text view"
    assert "1 failed" in text, f"real failed task must stay visible beside the warning:\n{text}"
    assert "gate red" in text, f"real gate-red reason must stay visible beside the warning:\n{text}"
    blob = json.dumps({k: data.get(k) for k in ("outcome", "reason", "stopReason", "halted", "failed")})
    assert "overhead" not in blob.lower(), f"stop reason renamed to overhead: {blob}"
