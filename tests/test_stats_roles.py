"""TC-32, TC-33: role attribution from agent labels, through the real `ck stats collect` and `ck stats show`.

Requirements 5.1, 5.2. Every call is a `ck` subprocess over a fake Claude home; nothing of ck is imported or mocked.
"""

import json
from collections import Counter

import pytest

from stats_helpers import AgentSpec, FakeClaudeHome, fresh_env, load_record, stats

# label -> expected per-agent fields. Only fields the grammar table of design.md fixes are listed.
IMPLEMENT_LABELS = {
    "task 3": dict(role="implementer", task="3", attempt=1),
    "task 3 [test]": dict(role="test-writer", task="3", attempt=1),
    "task 3 retry 1": dict(role="implementer", task="3", attempt=2),
    "task 3 retry 2 (opus)": dict(role="implementer", task="3", attempt=3),
    "task 3 revise 1": dict(role="implementer", task="3", round=1),
    "review 3": dict(role="reviewer", task="3", round=1),
    "review 3 round 2": dict(role="reviewer", task="3", round=2),
    "resolve 3": dict(role="resolver", task="3", round=1),
    "resolve 3 round 2": dict(role="resolver", task="3", round=2),
    "fix 1 (Task 3 red on landing)": dict(role="fixer", task="3", attempt=1),
    "fix 2 (full gate) [opus]": dict(role="fixer", task=None, attempt=2),
    "re-check 2": dict(role="gate", attempt=2),
    "land 3 4": dict(role="land"),
    "full gate @4": dict(role="gate"),
    "final gate": dict(role="gate"),
    "cross-test wave 2": dict(role="cross-test"),
    "plan 1": dict(role="plan", attempt=1),
    "plan 2 retry": dict(role="plan"),
    "report": dict(role="report"),
}

CREATE_LABELS = {
    "write requirements.md": dict(role="implementer"),
    "write design.md": dict(role="implementer"),
    "write test-plan.md": dict(role="test-writer"),
    "write implementation track": dict(role="implementer"),
    "write test track": dict(role="test-writer"),
    "link tracks into tasks.md": dict(role="implementer"),
    "review: fidelity": dict(role="reviewer"),
    "refute: fidelity": dict(role="reviewer"),
    "snapshot spec before revision": dict(role="land"),
    "revise spec + write review.md": dict(role="fixer"),
    "consistency check: pass 1": dict(role="gate", round=1),
    "remove pre-revision snapshot": dict(role="land"),
}


def _collect(env, run_id):
    out = stats("collect", "--run", run_id, env=env)
    assert out.exit_code == 0, out.output
    return load_record(env.xdg, run_id)


def _check_labels(env, run_id, expected):
    rec = _collect(env, run_id)
    by_label = {a["label"]: a for a in rec["agents"]}
    assert set(by_label) == set(expected)
    for label, want in expected.items():
        for key, value in want.items():
            assert by_label[label][key] == value, f"{label!r}: {key} is {by_label[label][key]!r}, expected {value!r}"
    counts = Counter(w["role"] for w in expected.values())
    assert {k: v["agents"] for k, v in rec["byRole"].items()} == dict(counts)
    assert "other" not in rec["byRole"]
    assert all(a["role"] != "other" for a in rec["agents"])


def test_tc32_every_spec_implement_template_is_attributed_by_its_meaning():
    with fresh_env() as env:
        home = FakeClaudeHome(env.claude)
        run = home.session(cwd=env.root).implement_run(agents=[AgentSpec(label) for label in IMPLEMENT_LABELS])
        _check_labels(env, run.run_id, IMPLEMENT_LABELS)


def test_tc32_every_spec_create_template_is_attributed_by_its_meaning():
    with fresh_env() as env:
        home = FakeClaudeHome(env.claude)
        run = home.session(cwd=env.root).create_run(agents=[AgentSpec(label, phase="Requirements") for label in CREATE_LABELS])
        _check_labels(env, run.run_id, CREATE_LABELS)


def test_tc33_labels_no_template_produces_are_role_other_and_show_reports_them():
    with fresh_env() as env:
        home = FakeClaudeHome(env.claude)
        run = home.session(cwd=env.root).implement_run(agents=[
            AgentSpec("triage invoices", duration_ms=120_000, requests=[("q1", 1_000_000, 100_000, 0, 0)]),
            AgentSpec("tasks 3", duration_ms=180_000, requests=[("q2", 1_000_000, 100_000, 0, 0)]),
        ])
        rec = _collect(env, run.run_id)
        assert [a["role"] for a in rec["agents"]] == ["other", "other"]
        assert {a["label"] for a in rec["agents"]} == {"triage invoices", "tasks 3"}
        assert all(a["task"] is None for a in rec["agents"])
        assert set(rec["byRole"]) == {"other"}
        assert rec["byRole"]["other"]["agents"] == 2

        out = stats("show", run.run_id, "--json", env=env)
        assert out.exit_code == 0, out.output
        data = json.loads(out.stdout)
        assert data["other"]["agents"] == 2
        assert data["other"]["minutes"] == pytest.approx(5.0, abs=0.01)
        assert data["other"]["usd"] > 0
        assert data["other"]["usd"] == pytest.approx(data["byRole"]["other"]["usd"])
        assert data["other"]["usd"] == pytest.approx(rec["byRole"]["other"]["usd"])
