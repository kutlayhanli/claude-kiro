"""Shareable `source` / `agent_transcripts` schema and leak vocabulary: TC-49, TC-50, TC-51, TC-55 (vocabulary half), P-13, P-14.

They call the seams `build_shareable`, `validate_shareable`, `SOURCES` and `leak.VOCABULARY` directly, and run the real
`ck stats export` against a real git kiro checkout. Project modules are imported inside the test bodies so the suite
stays collectable before the implementation exists.
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from stats_helpers import (
    CONFIDENTIAL,
    FakeClaudeHome,
    fake_gh,
    fresh_env,
    git,
    git_project,
    kiro_checkout,
    seeded_local_record,
    stats,
)

PROPERTIES = {
    "P-13": "test_p13_agent_mode_records_build_a_valid_leak_free_shareable",
    "P-14": "test_p14_validator_accepts_and_rejects_source_and_cost_source_as_documented",
}

SETTINGS = dict(derandomize=True, max_examples=200, database=None, deadline=None, suppress_health_check=list(HealthCheck))

REPO = Path(__file__).resolve().parent.parent
SCHEMA_MD = REPO / "analytics" / "SCHEMA.md"
FIXTURE = Path(__file__).parent / "fixtures" / "stats" / "shared_0_10_1.json"

AG_ID = "ag_1a2b3c4d"
WORKTREE_TERMS = ("/home/dana/acme-ledger/.ck/worktrees/zephyr-payroll-task-3", "feat/zephyr-payroll-task-3")
DESCRIPTIONS = (
    "Implement task 3 of zephyr-payroll in worktree zephyr-payroll-task-3",
    "Review the spec for tax-review-2026 at /srv/acme/ledger.py",
)


def fixture_record() -> dict:
    return json.loads(FIXTURE.read_text())


def _shareable():
    from claude_kiro.analytics import share

    return share


# --------------------------------------------------------------------------- the 0.10.1 fixture


def test_fixture_is_a_valid_0_10_1_record_today():
    """Guards the fixture itself: a later failure points at a regression, not at the fixture."""
    rec = fixture_record()
    assert "source" not in rec
    assert rec["ck_version"] == "0.10.1"
    assert _shareable().validate_shareable(rec) == []


# --------------------------------------------------------------------------- TC-49


@pytest.mark.parametrize("source", ["workflow", "agents"])
def test_tc49_accepts_source_workflow_and_agents(source):
    rec = fixture_record()
    rec["source"] = source
    assert _shareable().validate_shareable(rec) == []


def test_tc49_accepts_a_record_without_source():
    assert _shareable().validate_shareable(fixture_record()) == []


@pytest.mark.parametrize("bad", ["Agents", "agent", "workflows", "", None, 1])
def test_tc49_rejects_other_source_values_without_echoing_them(bad):
    rec = fixture_record()
    rec["source"] = bad
    errors = _shareable().validate_shareable(rec)
    assert errors, f"source {bad!r} was accepted"
    if isinstance(bad, str) and bad:
        assert not any(bad in e for e in errors), errors


@pytest.mark.parametrize("bad", ["claude_total", "agent-transcripts"])
def test_tc49_rejects_other_cost_source_values_without_echoing_them(bad):
    rec = fixture_record()
    rec["cost"]["source"] = bad
    errors = _shareable().validate_shareable(rec)
    assert errors, f"cost.source {bad!r} was accepted"
    assert not any(bad in e for e in errors), errors


def test_tc49_accepts_cost_source_agent_transcripts():
    rec = fixture_record()
    rec["source"] = "agents"
    rec["cost"]["source"] = "agent_transcripts"
    assert _shareable().validate_shareable(rec) == []


def test_tc49_sources_constant_holds_exactly_the_two_values():
    assert tuple(_shareable().SOURCES) == ("workflow", "agents")


# --------------------------------------------------------------------------- TC-50


def _schema_section(name: str) -> str:
    text = SCHEMA_MD.read_text()
    m = re.search(rf"^##\s+{re.escape(name)}\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    assert m, f"analytics/SCHEMA.md has no '## {name}' section"
    return m.group(1)


def _field_lines(path: str) -> List[str]:
    lines = []
    for line in _schema_section("Fields").splitlines():
        if line.strip().startswith(("-", "*", "|")):
            m = re.search(r"`([^`]+)`", line)
            if m and m.group(1) == path:
                lines.append(line)
    return lines


def test_tc50_schema_md_documents_source():
    lines = _field_lines("source")
    assert lines, "analytics/SCHEMA.md '## Fields' has no line for `source`"
    text = " ".join(lines)
    assert "workflow" in text and "agents" in text


def test_tc50_schema_md_lists_agent_transcripts_as_a_cost_source():
    lines = _field_lines("cost.source")
    assert lines, "analytics/SCHEMA.md '## Fields' has no line for `cost.source`"
    assert "agent_transcripts" in " ".join(lines)


def test_tc50_schema_id_is_unchanged_and_still_named():
    from claude_kiro.analytics import SCHEMA_SHARED

    assert SCHEMA_SHARED == "ck-run-analytics/1"
    assert SCHEMA_SHARED in SCHEMA_MD.read_text()


# --------------------------------------------------------------------------- TC-51


@pytest.fixture
def w():
    with fresh_env() as env:
        home = FakeClaudeHome(env.claude)
        proj = git_project(env.root)
        checkout, bare = kiro_checkout(env.root)
        bin_dir = env.root / "bin"
        fake_gh(bin_dir, env.root / "gh.log")
        env_vars = {k: str(v) for k, v in env.items()}
        env_vars["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        yield SimpleNamespace(env=env, vars=env_vars, home=home, proj=proj, checkout=checkout, bare=bare)


def test_tc51_export_leaves_a_0_10_1_record_in_the_checkout_unchanged(w):
    rec = fixture_record()
    assert _shareable().validate_shareable(rec) == []
    old = w.checkout / "analytics" / "runs" / rec["month"] / f"{rec['id']}.json"
    old.parent.mkdir(parents=True)
    old.write_bytes(FIXTURE.read_bytes())
    before = old.read_bytes()
    git(w.checkout, "add", "-A")
    git(w.checkout, "commit", "-qm", "analytics: a ck 0.10.1 record")
    git(w.checkout, "push", "-q", "origin", "HEAD")

    s = w.home.session("-work-acme", cwd=w.proj)
    s.implement_run(start="2026-10-12T09:00:00Z", args={"spec": "demo", "root": str(w.proj)})
    r = stats("collect", "--all", env=w.vars, cwd=w.proj)
    assert r.exit_code == 0, r.output
    r = stats("export", "--to", str(w.checkout), env=w.vars, cwd=w.checkout)
    assert r.exit_code == 0, r.output

    assert old.read_bytes() == before
    exported = sorted((w.checkout / "analytics" / "runs").rglob("*.json"))
    assert old in exported and len(exported) == 2
    share = _shareable()
    for path in exported:
        data = json.loads(path.read_text())
        assert share.validate_shareable(data) == [], path
        if path != old:
            assert data["source"] == "workflow"


# --------------------------------------------------------------------------- TC-55 (vocabulary half)


def test_tc55_leak_vocabulary_holds_the_source_and_cost_source_words():
    from claude_kiro.analytics import leak

    for word in ("workflow", "agents", "agent_transcripts"):
        assert word in leak.VOCABULARY, word


# --------------------------------------------------------------------------- generators


@st.composite
def agent_mode_record(draw):
    """A local record of an agent-mode (or older workflow) run, with agent-mode extras in the free-text slots."""
    kind = draw(st.sampled_from(["spec-implement", "spec-create"]))
    mode = draw(st.sampled_from(["implement", "create", "review"]))
    rec = copy.deepcopy(seeded_local_record("spec-implement" if mode == "implement" else "spec-create"))
    if mode != "implement":
        rec["mode"] = mode
    assert kind
    run_id = AG_ID[:-1] + draw(st.sampled_from("0123456789abcdef"))
    rec["runId"] = run_id
    rec["workflow"] = "spec-implement" if mode == "implement" else "spec-create"
    rec["workflowName"] = rec["workflow"]
    rec["terms"] = list(rec["terms"]) + list(WORKTREE_TERMS) + [run_id]
    rec["project"] = {**rec["project"], "root": WORKTREE_TERMS[0], "branch": WORKTREE_TERMS[1]}
    for i, a in enumerate(rec["agents"]):
        a["label"] = DESCRIPTIONS[i % len(DESCRIPTIONS)]
    cost_source = draw(st.sampled_from(["agent_transcripts", "estimate"]))
    rec["costs"] = [
        {"usd": 3.75, "source": cost_source, "pricingDate": "2026-10", "scope": "run"},
        {"usd": 7.5, "source": "claude_total", "pricingDate": None, "scope": "session"},
    ]
    source = draw(st.sampled_from(["agents", "absent", "none"]))
    rec.pop("source", None)
    if source == "agents":
        rec["source"] = "agents"
    elif source == "none":
        rec["source"] = None
    return rec, source, cost_source, run_id


def _json_forms(obj: Any) -> List[str]:
    return [json.dumps(obj), json.dumps(obj, ensure_ascii=False), json.dumps(obj).replace("/", "\\/")]


def _local_secrets(rec: dict, run_id: str) -> List[str]:
    secrets = list(CONFIDENTIAL) + list(WORKTREE_TERMS) + list(DESCRIPTIONS) + [run_id, rec["sessionId"]]
    secrets += [a["agentId"] for a in rec["agents"]] + [a["label"] for a in rec["agents"]]
    return sorted({s for s in secrets if s})


# --------------------------------------------------------------------------- P-13


@settings(**SETTINGS)
@given(agent_mode_record())
def test_p13_agent_mode_records_build_a_valid_leak_free_shareable(case):
    rec, source, cost_source, run_id = case
    share = _shareable()
    before = copy.deepcopy(rec)
    out = share.build_shareable(rec, record_id="0123456789abcdef")
    assert rec == before, "build_shareable mutated its input"

    assert out["source"] == ("agents" if source == "agents" else "workflow")
    assert out["cost"]["source"] == ("agent_transcripts" if cost_source == "agent_transcripts" else "estimate")
    assert share.validate_shareable(out) == []

    for text in _json_forms(out):
        low = text.lower()
        for secret in _local_secrets(rec, run_id):
            assert secret.lower() not in low, f"shareable record leaks {secret!r}"
        assert not re.search(r"\bag_[0-9a-f]{8}\b", low)


# --------------------------------------------------------------------------- P-14

_BAD_SOURCES = ["Agents", "agent", "agents ", "workflows", "wf", "transcripts", "agent_transcripts", "", None, 0, []]
_BAD_COST_SOURCES = ["claude_total", "agent-transcripts", "Agent_Transcripts", "priced_from_transcript"]


@st.composite
def valid_shareable(draw):
    rec, _source, _cost, _run_id = draw(agent_mode_record())
    out = _shareable().build_shareable(rec, record_id="0123456789abcdef")
    out.pop("source", None)
    return out


@settings(**SETTINGS)
@given(valid_shareable(), st.sampled_from(["absent", "workflow", "agents"]), st.sampled_from(_BAD_SOURCES),
       st.sampled_from(_BAD_COST_SOURCES))
def test_p14_validator_accepts_and_rejects_source_and_cost_source_as_documented(base, good, bad_source, bad_cost):
    share = _shareable()
    assert share.validate_shareable(base) == [], "generator produced an invalid record"

    ok = copy.deepcopy(base)
    if good != "absent":
        ok["source"] = good
    assert share.validate_shareable(ok) == []
    # metamorphic: deleting `source` from a valid record keeps it valid
    ok.pop("source", None)
    assert share.validate_shareable(ok) == []

    bad = copy.deepcopy(base)
    bad["source"] = bad_source
    errors = share.validate_shareable(bad)
    assert errors, f"source {bad_source!r} was accepted"
    if isinstance(bad_source, str) and len(bad_source) >= 4:
        assert not any(bad_source in e for e in errors), errors

    bad = copy.deepcopy(base)
    bad["cost"]["source"] = bad_cost
    errors = share.validate_shareable(bad)
    assert errors, f"cost.source {bad_cost!r} was accepted"
    assert not any(bad_cost in e for e in errors), errors
