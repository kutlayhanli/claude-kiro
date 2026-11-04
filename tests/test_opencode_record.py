"""Native ingest of the OpenCode bridge's `ck-opencode-run/1` record into `ck-run-local/1` (D7).

The contract is `notes/opencode-record-contract.md`: the bridge writes record.json; `ck stats record`
validates it, builds the same local record `ck stats collect` writes, and never reads a Claude transcript.
"""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_kiro import bench, manifest
from claude_kiro.analytics import store
from claude_kiro.analytics.share import build_shareable, validate_shareable
from claude_kiro.cli.main import cli

RUN_ID = "wf-20261030192320-56aaa6"
FREE = "opencode/deepseek-v4.1-flash-free"
FREE_MUSE = "opencode/muse-spark-1.3-contributor-free"

RESULT = {
    "outcome": "complete", "tasks": [], "landings": [], "fullGates": [],
    "finalGate": {"green": True}, "crossTests": [], "fixerEscalations": [], "timing": {},
}


def agent(seq, label, *, session="ses_1", provider="opencode", model="deepseek-v4.1-flash-free",
          requested="medium", resolved="high", variant="high", tokens=(1000, 200, 500, 100), **extra):
    row = {
        "seq": seq, "label": label, "phase": "Wave 1", "role": None, "agent": "ck-worker",
        "parentSessionID": "ses_parent", "sessionID": session,
        "requested": {"model": "sonnet", "effort": requested},
        "resolved": {"providerID": provider, "id": model, "variant": variant},
        "requestedEffort": requested, "resolvedEffort": resolved, "variant": variant,
        "startedAt": 1793388200000 + seq * 1000, "endedAt": 1793388200000 + seq * 1000 + 1000,
        "durationMs": 1000, "status": "ok", "submission": "tool", "attempts": 1, "rejections": 0,
        "tokens": {"input": tokens[0], "output": tokens[1], "reasoning": 0,
                   "cache": {"read": tokens[2], "write": tokens[3]}},
        "cost": 0, "peakContext": tokens[0] + tokens[2], "error": None,
    }
    row.update(extra)
    return row


def record(tmp_path, **overrides):
    data = {
        "schema": "ck-opencode-run/1", "runId": RUN_ID, "harness": "opencode", "workflow": "spec-implement",
        "collectorVersion": "0.10.3", "ckVersion": "0.10.3", "claudeCodeVersion": None,
        "status": "done", "startedAt": 1793388200665, "endedAt": 1793388233000,
        "projectRoot": str(tmp_path / "proj"), "spec": "auth", "into": "integrate/auth",
        "args": {"spec": "auth"}, "result": RESULT,
        "throttles": 4, "retries": 1, "failureLabel": None, "degraded": ["compaction"],
        "guard": {"denials": 2, "postCheckCatches": 1, "guardEscalations": 1,
                  "headlessDenials": 0, "prompts": 3},
        "agents": [
            agent(1, "task 3", session="ses_1"),
            agent(2, "task 3", session="ses_2", tokens=(2000, 300, 700, 100), attempts=2),
            agent(3, "review 3", session="ses_3", provider="opencode", model="muse-spark-1.3-contributor-free",
                  requested="high", tokens=(3000, 400, 800, 100)),
            agent(4, "land 3", session="ses_4", requested="low", resolved=None, variant=None,
                  tokens=(500, 50, 100, 0)),
        ],
    }
    data.update(overrides)
    return data


def ck(*args: str):
    old = os.getcwd()
    try:
        return CliRunner().invoke(cli, list(args))
    finally:
        os.chdir(old)


def write(tmp_path: Path, data, name: str = "record.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


def ingest(tmp_path: Path, data):
    return ck("stats", "record", str(write(tmp_path, data)))


def load(run_id: str = RUN_ID) -> dict:
    return json.loads(store.record_path(run_id).read_text())


def runs() -> list:
    d = store.runs_dir()
    return sorted(d.glob("*.json")) if d.is_dir() else []


# --------------------------------------------------------------------------- the happy path


def test_a_contract_record_becomes_a_local_record(tmp_path):
    out = ingest(tmp_path, record(tmp_path))
    assert out.exit_code == 0, out.output
    assert "✓" in out.output and RUN_ID in out.output and "wrote" in out.output
    rec = load()

    assert rec["schema"] == "ck-run-local/1"
    assert rec["runId"] == RUN_ID
    assert rec["harness"] == "opencode"
    assert rec["claudeCodeVersion"] is None
    assert rec["collectorVersion"] == "0.10.3"
    assert rec["ckVersion"] == "0.10.3"
    assert rec["source"] == "workflow"
    assert rec["workflow"] == "spec-implement" and rec["workflowName"] == "spec-implement"
    assert rec["status"] == "done"
    assert rec["project"]["root"] == str(tmp_path / "proj")
    assert rec["project"]["spec"] == "auth" and rec["project"]["branch"] == "integrate/auth"
    assert rec["throttles"] == 4 and rec["retries"] == 1
    assert rec["degraded"] == ["compaction"] and rec["failureLabel"] is None
    assert rec["missing"] == []
    assert "partial" not in rec

    assert [a["role"] for a in rec["agents"]] == ["implementer", "implementer", "reviewer", "land"]
    assert [a["task"] for a in rec["agents"]] == ["3", "3", "3", "3"]
    assert [a["requestedEffort"] for a in rec["agents"]] == ["medium", "medium", "high", "low"]
    assert [a["resolvedEffort"] for a in rec["agents"]] == ["high", "high", "high", None]
    assert [a["variant"] for a in rec["agents"]] == ["high", "high", "high", None]
    assert rec["byRole"]["implementer"]["agents"] == 2
    assert rec["byRole"]["reviewer"]["agents"] == 1

    assert rec["costs"][0]["usd"] == 0.0 and rec["costs"][0]["usd"] is not None
    assert rec["costs"][0]["source"] == "estimate"
    assert FREE in rec["byModel"] and FREE_MUSE in rec["byModel"]

    totals = rec["interruptions"]["totals"]
    assert totals["guardDenials"] == 2 and totals["postCheckCatches"] == 1
    assert totals["guardEscalations"] == 1 and totals["prompts"] == 3

    shown = ck("stats", "show", RUN_ID)
    assert shown.exit_code == 0, shown.output
    assert RUN_ID in shown.output


def test_a_null_ck_version_is_filled_from_the_installed_cli(tmp_path):
    result = ingest(tmp_path, record(tmp_path, ckVersion=None))
    assert result.exit_code == 0
    assert load()["ckVersion"] == manifest.ck_version()


def test_the_beam_free_arm_is_priced_at_zero():
    assert bench.price_usage(
        "opencode-go/beam-free", {"input": 1000, "output": 100, "cacheRead": 500, "cacheCreation": 0}
    ) == 0.0


def test_a_free_run_reports_zero_dollars_not_unknown(tmp_path):
    usage = {"input": 1_000_000, "output": 1_000_000, "cacheRead": 1_000_000, "cacheCreation": 1_000_000}
    assert bench.rates(FREE)["free"] is True
    assert bench.price_usage(FREE, usage) == 0.0

    out = ingest(tmp_path, record(tmp_path))
    assert out.exit_code == 0, out.output
    rec = load()
    assert rec["costs"][0]["usd"] == 0.0
    assert all(a["usd"] == 0.0 for a in rec["agents"])

    cost = ck("cost", "--run", RUN_ID)
    assert cost.exit_code == 0, cost.output
    assert "$0.00" in cost.output and "unknown" not in cost.output.lower()


def test_agent_rows_keep_native_identity_and_do_not_claim_transcripts(tmp_path):
    out = ingest(tmp_path, record(tmp_path))
    assert out.exit_code == 0, out.output
    rows = load()["agents"]
    assert [a["agentId"] for a in rows] == ["ses_1", "ses_2", "ses_3", "ses_4"]
    assert all(a["transcript"] == "native" for a in rows)
    assert all(a["state"] == "ok" for a in rows)
    assert all(a["model"] in (FREE, FREE_MUSE) for a in rows)
    missing = load()["missing"]
    assert not any(m.get("what") in ("transcript", "meta") for m in missing)


def test_shareable_record_carries_the_new_fields_and_validates(tmp_path):
    out = ingest(tmp_path, record(tmp_path, failureLabel="model"))
    assert out.exit_code == 0, out.output
    rec = load()
    shared = build_shareable(rec, record_id="0123456789abcdef")
    assert validate_shareable(shared) == []
    assert shared["harness"] == "opencode"
    assert shared["throttles"] == 4 and shared["retries"] == 1
    assert shared["degraded"] == ["compaction"]
    assert shared["failure_label"] == "model"
    assert shared["agents"][0] == {
        "role": "implementer", "model": "other", "requested_effort": "medium",
        "resolved_effort": "high", "variant": "high",
    }
    text = json.dumps(shared)
    assert "task 3" not in text and "ses_1" not in text


# --------------------------------------------------------------------------- partial records


@pytest.mark.parametrize("status", ["running", "partial"])
def test_a_running_record_is_ingested_as_partial(tmp_path, status):
    out = ingest(tmp_path, record(tmp_path, status=status, endedAt=None, result=None,
                                agents=[agent(1, "task 3")]))
    assert out.exit_code == 0, out.output
    assert "partial" in out.output
    rec = load()
    assert rec["status"] == status
    assert rec["partial"] is True
    assert rec["wallMs"] is None
    assert not any(m.get("what") == "result" for m in rec["missing"])
    assert len(rec["agents"]) == 1

    shown = ck("stats", "show", RUN_ID)
    assert shown.exit_code == 0, shown.output


# --------------------------------------------------------------------------- malformed input


@pytest.mark.parametrize("data, needle", [
    ("{not json", "invalid JSON"),
    ({"schema": "wrong", "runId": RUN_ID}, "schema"),
    ({"schema": "ck-opencode-run/1", "harness": "opencode"}, "runId"),
])
def test_malformed_input_fails_and_writes_nothing(tmp_path, data, needle):
    path = tmp_path / "record.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    out = ck("stats", "record", str(path))
    assert out.exit_code == 1
    assert needle in out.output
    assert runs() == []


# --------------------------------------------------------------------------- unknown paid model


def test_an_unknown_paid_model_stays_unknown(tmp_path):
    unknown = "opencode-go/unknown-paid-9"
    data = record(tmp_path, status="failed", result=None, failureLabel="model",
                  agents=[agent(1, "task 3", provider="opencode-go", model="unknown-paid-9",
                                requested=None, resolved=None, variant=None)])
    out = ingest(tmp_path, data)
    assert out.exit_code == 0, out.output
    rec = load()
    assert rec["agents"][0]["model"] == unknown
    assert rec["agents"][0]["usd"] is None
    assert rec["costs"][0]["usd"] is None

    cost = ck("cost", "--run", RUN_ID)
    assert cost.exit_code == 0, cost.output
    assert "cost unknown" in cost.output.lower() and "$0.00" not in cost.output

    shared = build_shareable(rec, record_id="0123456789abcdef")
    assert validate_shareable(shared) == []
    assert "usd" not in shared["cost"]
    assert shared["failure_label"] == "model"


# --------------------------------------------------------------------------- idempotence


def test_re_ingesting_the_same_record_rewrites_nothing(tmp_path):
    data = record(tmp_path)
    first = ingest(tmp_path, data)
    assert first.exit_code == 0, first.output
    path = store.record_path(RUN_ID)
    before = path.read_bytes()

    second = ingest(tmp_path, data)
    assert second.exit_code == 0, second.output
    assert "unchanged" in second.output
    assert path.read_bytes() == before
