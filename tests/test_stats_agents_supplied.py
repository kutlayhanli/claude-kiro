"""`ck stats collect --agents --from-json`: the record of an agent-mode run from
orchestrator-supplied agent rows (no transcripts).

Every test runs the real CLI as a subprocess over a fake store with a real manifest;
no Claude transcript home is needed. Project modules are imported inside test bodies only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stats_helpers import (
    begin as do_begin,
    fresh_env,
    load_record,
    spec_repo,
    stats,
)

pytestmark = pytest.mark.usefixtures("no_network")

SPEC = "demo"
INTO = "integrate/demo"


@pytest.fixture
def env():
    with fresh_env() as e:
        yield e


@pytest.fixture(autouse=True)
def _offline(no_network):
    """Every child process of every test here must have stayed off the network."""
    yield
    assert not no_network.exists() or no_network.read_text().strip() == "", no_network.read_text()


def open_run(env, kind: str = "create"):
    repo = spec_repo(env.root / "repo", spec=SPEC, into=INTO, tasks=2)
    rid, manifest = do_begin(env, repo, kind, SPEC, INTO)
    return rid, manifest


def rows_file(env, rows: Any) -> Path:
    path = Path(env.root) / "rows.json"
    path.write_text(json.dumps(rows))
    return path


def record_path(env, rid: str) -> Path:
    return env.xdg / "claude-kiro" / "runs" / f"{rid}.json"


def collect(env, rid: str, path: Path, *extra: str):
    return stats("collect", "--agents", rid, "--from-json", str(path), "--json", *extra, env=env)


def test_supplied_create_steps_are_collected_with_roles_and_phases(env):
    rid, _manifest = open_run(env, "create")
    rows = [
        {"label": f"[ck:{rid}] create requirements", "model": "opus", "tokens": 1000, "ms": 5000},
        {"label": f"[ck:{rid}] create design", "model": "opus", "tokens": 2000, "ms": 9000},
        {"label": f"[ck:{rid}] create review fidelity", "model": "sonnet", "tokens": 500, "ms": 1000},
    ]
    out = collect(env, rid, rows_file(env, rows))
    assert out.exit_code == 0, out.output
    rec = load_record(env.xdg, rid)
    assert rec["workflow"] == "spec-create"
    assert rec["partial"] is True
    labels = [a["label"] for a in rec["agents"]]
    assert labels == [r["label"] for r in rows]
    by_label = {a["label"]: a for a in rec["agents"]}
    assert by_label[f"[ck:{rid}] create requirements"]["phase"] == "Requirements"
    assert by_label[f"[ck:{rid}] create design"]["phase"] == "Design + Test Plan"
    assert by_label[f"[ck:{rid}] create review fidelity"]["phase"] == "Adversarial Review"
    assert by_label[f"[ck:{rid}] create requirements"]["transcript"] == "missing"
    assert by_label[f"[ck:{rid}] create requirements"]["usd"] is None
    assert by_label[f"[ck:{rid}] create requirements"]["usdEstimate"] is not None
    assert by_label[f"[ck:{rid}] create requirements"]["usdEstimate"] > 0
    run_costs = [c for c in rec["costs"] if c.get("scope") == "run"]
    assert len(run_costs) == 1
    assert run_costs[0]["source"] == "estimate"
    whats = [m["what"] for m in rec["missing"]]
    assert "transcripts-supplied" in whats
    assert run_costs[0]["usd"] == pytest.approx(sum(a["usdEstimate"] for a in rec["agents"]))


def test_supplied_implement_labels_match_roles_and_unknown_models_stay_unknown(env):
    rid, _manifest = open_run(env, "implement")
    rows = [
        {"label": f"[ck:{rid}] implement task 1", "model": "mystery-model-9", "tokens": 1000, "ms": 3000,
         "agentType": "spec:implementer"},
        {"label": f"[ck:{rid}] review task 1 round 1", "agentType": "spec:reviewer"},
    ]
    out = collect(env, rid, rows_file(env, rows))
    assert out.exit_code == 0, out.output
    rec = load_record(env.xdg, rid)
    assert rec["workflow"] == "spec-implement"
    by_label = {a["label"]: a for a in rec["agents"]}
    assert by_label[f"[ck:{rid}] implement task 1"]["role"] == "implementer"
    assert by_label[f"[ck:{rid}] review task 1 round 1"]["role"] == "reviewer"
    assert by_label[f"[ck:{rid}] implement task 1"]["usdEstimate"] is None
    assert by_label[f"[ck:{rid}] review task 1 round 1"]["usd"] is None
    run_costs = [c for c in rec["costs"] if c.get("scope") == "run"]
    assert len(run_costs) == 1 and run_costs[0]["usd"] is None


def test_supplied_timing_lands_on_rows_and_wall_stays_unknown(env):
    rid, _manifest = open_run(env, "create")
    rows = [
        {"label": f"[ck:{rid}] create requirements", "startedMs": 1700000000000, "ms": 7000},
    ]
    out = collect(env, rid, rows_file(env, rows))
    assert out.exit_code == 0, out.output
    rec = load_record(env.xdg, rid)
    assert rec["agents"][0]["startedAtMs"] == 1700000000000
    assert rec["agents"][0]["ms"] == 7000


def test_supplied_rows_without_the_marker_fail_closed(env):
    rid, _manifest = open_run(env, "create")
    rows = [{"label": "[ck:ag_wrong] create requirements", "model": "opus", "tokens": 10}]
    out = collect(env, rid, rows_file(env, rows))
    assert out.exit_code == 1, out.output
    assert not record_path(env, rid).exists()


def test_supplied_empty_and_malformed_files_fail_closed(env):
    rid, _manifest = open_run(env, "create")
    for bad in ([], {}, "not a list", [{"model": "opus"}],
                [{"label": f"[ck:{rid}] create design", "tokens": -1}],
                [{"label": f"[ck:{rid}] create design", "tokens": "lots"}]):
        out = collect(env, rid, rows_file(env, bad))
        assert out.exit_code == 1, out.output
    assert not record_path(env, rid).exists()
    out = stats("collect", "--agents", rid, "--from-json", str(Path(env.root) / "missing.json"),
                "--json", env=env)
    assert out.exit_code == 1, out.output


def test_supplied_without_a_manifest_gives_a_create_record(env):
    repo = spec_repo(env.root / "repo", spec=SPEC, into=INTO, tasks=2)
    void_env = dict(env)
    rid = "ag_00000000"
    rows = [{"label": f"[ck:{rid}] create link", "model": "opus", "tokens": 100, "ms": 500}]
    out = stats("collect", "--agents", rid, "--from-json", str(rows_file(env, rows)),
                "--json", env=void_env, cwd=repo)
    assert out.exit_code == 0, out.output
    rec = load_record(env.xdg, rid)
    assert rec["workflow"] == "spec-create"
    assert "manifest" in [m["what"] for m in rec["missing"]]
