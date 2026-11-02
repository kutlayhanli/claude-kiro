"""Fixture-spec e2e gate: fallback completion plus degraded marking (TC-31/TC-32).

Covers requirements 6.1, 6.2 (Story 6) against the fixture scaffold from Task 1
(``specs/deterministic-plan/fixtures/fallback-spec/``, 3 tasks ``1 -> 2, 1 -> 3``):

- TC-31 (6.1, NFR-1): fixture spec with the planning agent forced to return a
  ready list contradicting its dependencies runs to completion degraded, with
  fallback count >= 1 and the triggering plan attempt named.
- TC-32 (6.2): the same fixture with a healthy planning agent completes with
  zero fallbacks and no degraded mark.
- TC-33 (6.3, NFR-2) is manual-only by design: the equity re-run costs a full
  workload run, so CI does NOT assert a numeric saving. This module records the
  manual procedure and only asserts the record exists
  (``test_tc33_manual_equity_rerun_documented``).

Hermetic and fast on purpose (unlike the task blurb's "real paid agents"
shape): every assertion runs the REAL workflow script
(``plugin/workflows/spec-implement.js``) under the fake runtime through the
Node JSON-lines bridge, with the fixture graph and the shim's two forced-
inconsistent answers reproduced as ``planSeq`` (``ready: ['999']``, an unknown
id, deps staying truthful). No paid agents, no network, no fixture-branch
mutation, no manual re-run in CI.

Before Task 22 exists, TC-31/TC-32 fail for the right reason: the script halts
the twice-inconsistent run with ``planning step failed`` instead of
continuing, and carries no ``degraded``/``planFallbacks`` fields. Project
modules are imported inside test bodies/helpers only, so collection never
depends on unimplemented code.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

import pytest

from landing_bridge import bridge  # noqa: F401  (pytest fixture)

pytestmark = pytest.mark.usefixtures("no_network")

FIXTURE_DIR = Path(__file__).parent.parent / "specs" / "deterministic-plan" / "fixtures" / "fallback-spec"
FIXTURE_DEPS: Dict[str, List[str]] = {"1": [], "2": ["1"], "3": ["1"]}
FIXTURE_ORDER = ["1", "2", "3"]

# The plan-agent shim's forced-inconsistent answers: a ready list holding an id
# absent from the graph (max id + 1) while deps stay truthful, so the scheduler
# must retry once, fall back deterministically, and report degraded. The shim
# forces exactly the first two plan answers; afterwards answers are healthy.
SHIM_BAD_READY = ["999"]
FORCED_PLAN_SEQ = [{"ready": list(SHIM_BAD_READY)}, {"ready": list(SHIM_BAD_READY)}]

PLAN_LABEL = re.compile(r"^plan (\d+)( retry)?$")


def _fixture_args(extra: Dict | None = None) -> Dict:
    args: Dict = {"deps": {t: list(ds) for t, ds in FIXTURE_DEPS.items()}, "remaining": list(FIXTURE_ORDER)}
    if extra:
        args.update(extra)
    return args


def _fallback_entries(result: Dict) -> List[Dict]:
    """One accessor for the Task 22 result shape (design: planFallbacks list)."""
    fb = result.get("planFallbacks", [])
    if isinstance(fb, dict):
        return [{"trigger": t} for t in fb.get("triggers", [])]
    return list(fb or [])


def _fallback_count(result: Dict) -> int:
    entries = _fallback_entries(result)
    stats = result.get("stats") or {}
    return stats.get("planFallbacks", len(entries))


def _plan_labels(trace) -> List[str]:
    return [e["label"] for e in trace if e["ev"] == "request" and PLAN_LABEL.match(e["label"])]


def _fixture_shim_modes() -> List[str]:
    """Modes the fixture shim + ck.json contract exposes (healthy + forced)."""
    text = (FIXTURE_DIR / "plan_shim.mjs").read_text()
    assert "forced-inconsistent" in text and "healthy" in text
    cfg = json.loads((FIXTURE_DIR / "ck.json").read_text())
    return list(cfg.get("planShim", {}).get("modes", []))


def _fixture_plan_via_cli() -> Dict:
    """Run the real `ck plan` CLI over the real fixture tasks.md (copied as a
    plannable spec into a throwaway git repo): the data path TC-31 names."""
    import os
    import subprocess
    import tempfile

    from click.testing import CliRunner

    from claude_kiro.cli.main import cli

    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com"}
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q", tmp], check=True, env=env, capture_output=True)
        spec_dir = Path(tmp) / "specs" / "fallback-spec"
        spec_dir.mkdir(parents=True)
        (spec_dir / "tasks.md").write_text((FIXTURE_DIR / "tasks.md").read_text())
        subprocess.run(["git", "-C", tmp, "add", "-A"], check=True, env=env, capture_output=True)
        subprocess.run(["git", "-C", tmp, "commit", "-q", "-m", "init"], check=True, env=env, capture_output=True)
        old = os.getcwd()
        os.chdir(tmp)
        try:
            res = CliRunner().invoke(cli, ["plan", "fallback-spec", "--json"])
        finally:
            os.chdir(old)
    assert res.exit_code == 0, res.output
    return json.loads(res.output)


# ------------------------------------------------------------------ TC-31 (6.1)


def test_tc31_forced_inconsistent_completes_degraded(bridge):
    """TC-31 (6.1, NFR-1): forced-inconsistent fixture retries once, falls back,
    completes, and the report shows degraded with fallback count >= 1."""
    assert set(_fixture_shim_modes()) >= {"healthy", "forced-inconsistent"}
    # Real `ck plan` data path on the fixture repo: the 3-task graph plans
    # with task 1 ready first, matching the deterministic oracle.
    plan = _fixture_plan_via_cli()
    assert plan["ready"] == ["1"], plan["ready"]
    assert plan["deps"] == {"1": [], "2": ["1"], "3": ["1"]}, plan["deps"]
    project = {"deps": dict(FIXTURE_DEPS), "planSeq": [dict(e) for e in FORCED_PLAN_SEQ]}
    out = bridge.run(project, _fixture_args(), slots=3)
    result = out["result"]
    trace = out["trace"]
    plans = _plan_labels(trace)

    # Retry-once shape: the start plan ran, exactly one retry followed it, and
    # no third plan-1 attempt was ever requested.
    assert "plan 1" in plans, plans[:6]
    assert "plan 1 retry" in plans, plans[:6]
    assert [p for p in plans if p in ("plan 1", "plan 1 retry")] == ["plan 1", "plan 1 retry"], plans

    # Completion from the deterministic list: every fixture task merges.
    assert result.get("halted") is False, result.get("reason")
    assert result.get("outcome") == "complete", (result.get("outcome"), result.get("reason"))
    assert sorted(result.get("merged", []), key=int) == ["1", "2", "3"], result.get("merged")

    # Degraded marking plus fallback count/triggers (Task 22 fields).
    assert result.get("degraded") is True, (result.get("degraded"), result.get("reason"))
    entries = _fallback_entries(result)
    assert len(entries) >= 1, entries
    assert _fallback_count(result) >= 1, result.get("stats")
    labels = set(plans)
    for entry in entries:
        trigger = entry.get("trigger")
        step = entry.get("planStep")
        label = trigger if (isinstance(trigger, str) and PLAN_LABEL.match(trigger)) else f"plan {step}"
        assert label in labels, (entry, sorted(labels))


# ------------------------------------------------------------------ TC-32 (6.2)


def test_tc32_healthy_completes_clean(bridge):
    """TC-32 (6.2): healthy fixture completes with zero fallbacks, no degraded."""
    assert set(_fixture_shim_modes()) >= {"healthy", "forced-inconsistent"}
    out = bridge.run({"deps": dict(FIXTURE_DEPS)}, _fixture_args(), slots=3)
    result = out["result"]

    assert result.get("halted") is False, result.get("reason")
    assert result.get("outcome") == "complete", (result.get("outcome"), result.get("reason"))
    assert sorted(result.get("merged", []), key=int) == ["1", "2", "3"], result.get("merged")

    assert result.get("degraded") is False, result.get("degraded")
    assert _fallback_entries(result) == [], result.get("planFallbacks")
    assert _fallback_count(result) == 0, result.get("stats")


# --------------------------------------------- TC-33 (6.3, manual-only record)


def test_tc33_manual_equity_rerun_documented():
    """TC-33 (6.3, NFR-2, manual not CI): the equity re-run procedure and its
    comparison record live in the fixture README. This asserts the record
    exists and names the comparison (lower plan/review/land share, no planning
    halt) without asserting any numeric saving in CI."""
    doc = FIXTURE_DIR / "README.md"
    assert doc.exists(), f"missing manual re-run record: {doc}"
    text = doc.read_text()
    assert "TC-33" in text
    assert "baseline" in text.lower()
    assert "no planning halt" in text.lower()
