"""Smoke test of tests/eta_strategies.py: import hygiene, constants and that each strategy draws examples."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings

import eta_strategies as es

pytestmark = es.NO_NETWORK

TESTS = Path(__file__).parent
FAST = settings(max_examples=25, derandomize=True, database=None, deadline=None,
                suppress_health_check=list(HealthCheck))


def test_module_imports_no_claude_kiro_module():
    code = (
        "import sys; sys.path.insert(0, %r); import eta_strategies; "
        "print([m for m in sys.modules if m == 'claude_kiro' or m.startswith('claude_kiro.')])" % str(TESTS)
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def test_create_phases_has_seven_titles():
    assert len(es.CREATE_PHASES) == 7
    assert es.CREATE_PHASES[0] == "Requirements"


def test_constants_shape():
    assert re.match(es.ETA_SCHEMA_RE, "ck-eta/1")
    assert es.PROGRESS_SCHEMA == "ck-progress/1" and es.DEFAULTS_SCHEMA == "ck-eta-defaults/1"
    assert es.CLAIM_EVENTS == ("task-start", "retry", "escalation", "revision", "landfix", "resolve")
    assert es.MATCH_STEPS[0] == "track+model+risk" and es.MATCH_STEPS[-1] == "none"


def test_progress_lines_reads_objects_only(tmp_path):
    f = tmp_path / "p.jsonl"
    f.write_text('{"a": 1}\nnot json\n\n[1]\n{"b": 2}\n')
    assert es.progress_lines(f) == [{"a": 1}, {"b": 2}]
    assert es.progress_lines(tmp_path / "missing.jsonl") == []


@FAST
@given(es.graphs())
def test_graphs_draw(graph):
    assert graph
    for task in graph.values():
        assert task["track"] in es.TRACKS and task["model"] in es.MODELS and task["risk"] in es.RISKS


@FAST
@given(es.events())
def test_events_draw(evs):
    assert evs[0]["kind"] == es.EV_START
    assert all(e["schema"] == es.PROGRESS_SCHEMA for e in evs)
    ats = [e["at"] for e in evs]
    assert ats == sorted(ats)
    json.dumps(evs)


@FAST
@given(es.times())
def test_times_draw(t):
    assert 0 <= t <= 600


@FAST
@given(es.malformed_lines())
def test_malformed_lines_are_not_valid_events(line):
    try:
        obj = json.loads(line)
    except ValueError:
        return
    ok = (isinstance(obj, dict) and obj.get("schema") == es.PROGRESS_SCHEMA and isinstance(obj.get("kind"), str)
          and "at" in obj and len(line) <= 4096)
    assert not ok


def test_busy_starts_and_kills_children():
    with es.busy(2) as procs:
        assert all(p.poll() is None for p in procs)
    assert all(p.poll() is not None for p in procs)
