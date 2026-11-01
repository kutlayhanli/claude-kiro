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
