"""Backfill tests for `ck stats collect --all` (TC-28..TC-31, requirements 4.1-4.4).

Every test drives the real CLI as a subprocess over a fake Claude home with several projects.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from stats_helpers import (
    AgentSpec,
    FakeClaudeHome,
    fresh_env,
    git_project,
    load_record,
    stats,
    store_files,
)


@pytest.fixture
def env():
    with fresh_env() as e:
        yield e


def _home(env) -> FakeClaudeHome:
    return FakeClaudeHome(env.claude)


def _projects(env):
    return git_project(env.root, name="proj-a"), git_project(env.root, remote="git@github.com:other/proj-b.git", name="proj-b")


def _collect_all(env):
    return stats("collect", "--all", env=env)


def _stored_ids(env):
    d = Path(env.xdg) / "claude-kiro" / "runs"
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def _count(out: str, label: str) -> int:
    m = re.search(rf"^{re.escape(label)}: (\d+)\s*$", out, re.M)
    assert m, f"no '{label}: N' line in:\n{out}"
    return int(m.group(1))


def test_tc28_all_ck_runs_across_projects_are_stored(env):
    home = _home(env)
    pa, pb = _projects(env)
    sa = home.session("-work-proj-a", cwd=pa)
    r1 = sa.implement_run("wf_impl0000001")
    r2 = sa.implement_run("wf_iso00000001", name="spec-implement-iso", start="2026-10-12T10:00:00Z")
    r3 = sa.create_run("wf_create000001")
    sb = home.session("-work-proj-b", cwd=pb)
    r4 = sb.implement_run("wf_impl0000002", start="2026-10-13T09:00:00Z")
    r5 = sb.implement_run("wf_iso00000002", name="spec-implement-iso", start="2026-10-13T10:00:00Z")
    r6 = sb.create_run("wf_create000002", start="2026-10-13T08:00:00Z")
    res = _collect_all(env)
    assert res.exit_code == 0, res.output
    ids = [r.run_id for r in (r1, r2, r3, r4, r5, r6)]
    assert _stored_ids(env) == sorted(ids)
    assert _count(res.stdout, "collected") == 6
    for rid in ids:
        assert load_record(env.xdg, rid)["runId"] == rid
    assert load_record(env.xdg, "wf_iso00000001")["workflow"] == "spec-implement-iso"
    assert load_record(env.xdg, "wf_create000001")["workflow"] == "spec-create"


def _tc29_tree(env):
    """2 new, 1 unchanged-collected, 1 collected then changed, 1 running, 1 corrupt, 2 foreign."""
    home = _home(env)
    pa, pb = _projects(env)
    sa = home.session("-work-proj-a", cwd=pa)
    sb = home.session("-work-proj-b", cwd=pb)
    unchanged = sa.implement_run("wf_unchanged001", start="2026-10-10T09:00:00Z")
    changed = sb.implement_run("wf_changed00001", start="2026-10-10T10:00:00Z")
    assert stats("collect", "--run", unchanged.run_id, env=env).exit_code == 0
    assert stats("collect", "--run", changed.run_id, env=env).exit_code == 0
    # a new request appears in one transcript after the first collection
    t = changed.transcript(0)
    last = json.loads(t.read_text().splitlines()[-1])
    last["requestId"] = "req_late_1"
    last["message"] = {**last["message"], "id": "msg_late_1"}
    with t.open("a") as fh:
        fh.write(json.dumps(last) + "\n")
    new1 = sa.create_run("wf_new000000001", start="2026-10-11T09:00:00Z")
    new2 = sb.implement_run("wf_new000000002", name="spec-implement-iso", start="2026-10-11T10:00:00Z")
    running = sa.implement_run("wf_running00001", status="running", result=None, start="2026-10-12T09:00:00Z")
    corrupt = sb.implement_run("wf_corrupt00001", start="2026-10-12T10:00:00Z").corrupt_format()
    sa.foreign_run("wf_foreign00001", name="competitor-teardown-q4")
    sb.foreign_run("wf_foreign00002", name="tax-review-2026")
    return {"unchanged": unchanged, "changed": changed, "new": [new1, new2], "running": running, "corrupt": corrupt}


def test_tc29_summary_counts_and_failure_line(env):
    t = _tc29_tree(env)
    res = _collect_all(env)
    out = res.stdout
    assert _count(out, "collected") == 2
    assert _count(out, "already had") == 1
    assert _count(out, "updated") == 1
    assert _count(out, "unfinished") == 1
    assert _count(out, "failed") == 1
    assert _count(out, "skipped non-ck") == 2
    lines = out.splitlines()
    i = lines.index(next(l for l in lines if re.match(r"^failed: 1\s*$", l)))
    assert re.match(rf"^  {re.escape(t['corrupt'].run_id)}: \S.*$", lines[i + 1]), out
    # foreign runs: count only, no names or ids
    for needle in ("competitor-teardown-q4", "tax-review-2026", "wf_foreign00001", "wf_foreign00002"):
        assert needle not in res.output
    assert "wf_running00001" not in "\n".join(l for l in lines if l.startswith("  "))


def test_tc30_other_runs_stored_and_exit_nonzero_then_zero_after_removal(env):
    t = _tc29_tree(env)
    res = _collect_all(env)
    assert res.exit_code != 0, res.output
    stored = _stored_ids(env)
    for r in [t["unchanged"], t["changed"], *t["new"]]:
        assert r.run_id in stored
    assert t["corrupt"].run_id not in stored
    assert t["running"].run_id not in stored
    t["corrupt"].path.unlink()
    res2 = _collect_all(env)
    assert res2.exit_code == 0, res2.output
    assert _count(res2.stdout, "failed") == 0
    assert t["corrupt"].run_id not in _stored_ids(env)


def test_tc31_only_foreign_runs_says_none_found(env):
    home = _home(env)
    pa, pb = _projects(env)
    home.session("-work-proj-a", cwd=pa).foreign_run("wf_foreign00001", name="competitor-teardown-q4")
    home.session("-work-proj-b", cwd=pb).foreign_run("wf_foreign00002", name="tax-review-2026")
    res = _collect_all(env)
    assert res.exit_code == 0, res.output
    assert re.search(r"no ck runs (were )?found", res.output, re.I), res.output
    assert _stored_ids(env) == []
    for needle in ("competitor-teardown-q4", "tax-review-2026", "wf_foreign00001"):
        assert needle not in res.output


def test_tc31_empty_home_says_none_found(env):
    res = _collect_all(env)
    assert res.exit_code == 0, res.output
    assert re.search(r"no ck runs (were )?found", res.output, re.I), res.output
    assert _stored_ids(env) == []
