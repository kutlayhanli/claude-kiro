"""Integration tests: `ck stats collect` collects only ck workflows (TC-18..TC-20, requirements 2.1-2.3).

Every `ck` call is a real subprocess over a real fake Claude home with real file permissions.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from stats_helpers import (
    AgentSpec,
    FakeClaudeHome,
    all_bytes,
    fresh_env,
    load_record,
    stats,
    store_files,
)

FOREIGN = {
    "wf_foreign_a1": "competitor-teardown-q4",
    "wf_foreign_b2": "spec-implement-v2",
    "wf_foreign_c3": "Spec-Create",
    "wf_foreign_d4": "spec-plan",
}


def _leaks(blob: bytes, extra: tuple = ()) -> list:
    text = blob.decode(errors="replace")
    return [t for t in (*FOREIGN.values(), *FOREIGN.keys(), *extra) if t in text]


def test_tc18_only_ck_runs_are_stored_and_foreign_names_never_appear():
    """TC-18: two ck runs plus four foreign runs -> only the ck runs are stored; no name or id of a foreign run leaks."""
    with fresh_env() as env:
        home = FakeClaudeHome(env.claude)
        session = home.session()
        ck1 = session.implement_run("wf_ck_impl_1")
        ck2 = session.create_run("wf_ck_create_1")
        for rid, name in FOREIGN.items():
            session.foreign_run(rid, name=name)

        res = stats("collect", "--all", env=env)

        assert res.exit_code == 0, res.output
        stored = {p.name for p in store_files(env.xdg)}
        assert "wf_ck_impl_1.json" in stored and "wf_ck_create_1.json" in stored
        assert load_record(env.xdg, ck1.run_id)["runId"] == ck1.run_id
        assert load_record(env.xdg, ck2.run_id)["runId"] == ck2.run_id
        for rid in FOREIGN:
            assert f"{rid}.json" not in stored
        blob = all_bytes(env.xdg) + res.stdout.encode() + res.stderr.encode()
        assert _leaks(blob) == []


def test_tc19_foreign_run_is_not_read_past_its_name():
    """TC-19: a foreign run with malformed progress and unreadable transcripts gives exit 0 and no error about it."""
    with fresh_env() as env:
        home = FakeClaudeHome(env.claude)
        session = home.session()
        session.implement_run("wf_ck_impl_1")
        foreign = session.foreign_run("wf_foreign_a1", name="competitor-teardown-q4")
        foreign.set(workflowProgress={"not": "a list"})
        locked = [p for p in foreign.agents_dir.rglob("*") if p.is_file()]
        assert locked, "fixture wrote no foreign transcripts"
        for p in locked:
            p.chmod(0)
        try:
            res = stats("collect", "--all", env=env)
        finally:
            for p in locked:
                p.chmod(stat.S_IRUSR | stat.S_IWUSR)

        assert res.exit_code == 0, res.output
        assert (env.xdg / "claude-kiro" / "runs" / "wf_ck_impl_1.json").exists()
        out = res.output
        assert "wf_foreign_a1" not in out
        assert "competitor-teardown-q4" not in out
        assert "permission" not in out.lower()
        assert "denied" not in out.lower()
        assert "workflowProgress" not in out
        assert _leaks(all_bytes(env.xdg)) == []


def test_tc20_collect_run_refuses_a_foreign_run_without_naming_it():
    """TC-20: `collect --run <foreign id>` exits non-zero, refuses, does not print the name, writes nothing."""
    with fresh_env() as env:
        home = FakeClaudeHome(env.claude)
        session = home.session()
        session.foreign_run("wf_f1", name="tax-review-2026")

        res = stats("collect", "--run", "wf_f1", env=env)

        assert res.exit_code != 0, res.output
        assert res.exit_code != 2, f"expected a refusal, got a usage error:\n{res.output}"
        assert "tax-review-2026" not in res.output
        assert "wf_f1" in res.output  # the id I typed is echoed back
        assert store_files(env.xdg) == []
        assert "tax-review-2026" not in all_bytes(env.xdg).decode(errors="replace")
