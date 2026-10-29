"""`ck stats collect --agents-backfill`: recovering old unmarked implement-mode runs (TC-39 to TC-46, TC-47 CLI half, TC-60).

Every test runs the real CLI as a subprocess over a fake Claude home with the real transcript/meta layout and a real
store; the git-outcome test uses a real `spec_repo`. Ages and gaps are written into the transcripts; the clock is not
faked. Project modules are not imported.
"""

from __future__ import annotations

import itertools
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from stats_helpers import (
    AgentSpec,
    FakeSession,
    FakeClaudeHome,
    ToolAgentSpec,
    described,
    fresh_env,
    iso_ms,
    land,
    spec_repo,
    stats,
    store_files,
    to_ms,
)

pytestmark = pytest.mark.usefixtures("no_network")

_ids = itertools.count(1)


@pytest.fixture
def env():
    with fresh_env() as e:
        yield e


# --------------------------------------------------------------------------- helpers


def t(hh: int, mm: int = 0, day: int = 1) -> int:
    return to_ms(f"2026-09-{day:02d}T{hh:02d}:{mm:02d}:00Z")


def ag(desc: Optional[str], typ: Optional[str], start_ms: int, minutes: float = 10, aid: Optional[str] = None) -> ToolAgentSpec:
    aid = aid or f"a{next(_ids):015x}"
    return ToolAgentSpec(desc, agent_type=typ, started_at_ms=start_ms, duration_ms=int(minutes * 60_000), agent_id=aid)


def impl(spec: str, n: int, start_ms: int, minutes: float = 10) -> ToolAgentSpec:
    return ag(f"implement {spec}: task {n}", "spec:implementer", start_ms, minutes)


def fix(spec: str, n: int, start_ms: int, minutes: float = 10) -> ToolAgentSpec:
    return ag(f"fix {spec}: task {n} red", "spec:fixer", start_ms, minutes)


class Home:
    """One fake Claude home per env, so several sessions can share it."""

    def __init__(self, env):
        self.env = env
        self.home = FakeClaudeHome(env.claude)

    def session(self, cwd: Optional[Path] = None, branch: str = "integrate/demo") -> FakeSession:
        return self.home.session(cwd=cwd or self.env.root / "project", git_branch=branch)


def backfill(env, *args: str):
    return stats("collect", "--agents-backfill", *args, env=env, cwd=env.root)


def plan(env, *args: str) -> Dict[str, Any]:
    r = backfill(env, "--dry-run", "--json", *args)
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)


def shape(data: Dict[str, Any]) -> List[tuple]:
    return sorted((g["spec"], g["agents"]) for g in data["groups"])


def reasons(data: Dict[str, Any]) -> Dict[str, str]:
    return {e["agentId"]: e["reason"] for e in data["excluded"]}


def runs_dir(env) -> Path:
    return env.xdg / "claude-kiro" / "runs"


def records(env) -> Dict[str, dict]:
    d = runs_dir(env)
    return {p.name: json.loads(p.read_text()) for p in sorted(d.glob("*.json"))} if d.exists() else {}


def count(output: str, label: str) -> Optional[int]:
    m = re.search(rf"^\s*{re.escape(label)}:\s*(\d+)\s*$", output, re.M)
    return int(m.group(1)) if m else None


def run_usd(rec: dict) -> float:
    figs = [c for c in rec["costs"] if c.get("scope") == "run"]
    assert len(figs) == 1, rec["costs"]
    return figs[0]["usd"]


# --------------------------------------------------------------------------- TC-39


def test_tc39_dry_run_lists_groups_and_left_out_agents_and_writes_nothing(env):
    """TC-39 (7.1)."""
    h = Home(env)
    s1 = h.session()
    s1.tool_agents([impl("demo", 1, t(9, 0), 20), fix("demo", 1, t(9, 30), 10)])
    s2 = h.session()
    s2.tool_agents([impl("ledger", 2, t(14, 0), 30)])
    marked = ag(described("ag_1234abcd", "implement task 1"), "spec:implementer", t(9, 50))
    spec_less = ag("review task 4 diff", "spec:reviewer", t(21, 0))
    s1.tool_agents([marked, spec_less])
    before = store_files(env.xdg)

    text = backfill(env, "--dry-run")
    assert text.exit_code == 0, text.output
    assert re.search(rf"session {s1.session_id}\s+spec demo\s+agents 2", text.output), text.output
    assert re.search(rf"session {s2.session_id}\s+spec ledger\s+agents 1", text.output), text.output
    assert "start" in text.output and "wall" in text.output and "est. $" in text.output
    assert "left out" in text.output
    assert "carries a marker" in text.output and "no spec" in text.output

    data = plan(env)
    groups = {g["session"]: g for g in data["groups"]}
    assert set(groups) == {s1.session_id, s2.session_id}
    g1 = groups[s1.session_id]
    assert g1["spec"] == "demo" and g1["agents"] == 2 and g1["wallMinutes"] == 40
    assert "09:00" in g1["start"] and isinstance(g1["usd"], (int, float)) and g1["usd"] > 0 and g1["id"]
    assert reasons(data)[marked.agent_id] == "carries a marker"
    assert reasons(data)[spec_less.agent_id] == "no spec"
    assert store_files(env.xdg) == before == []


# --------------------------------------------------------------------------- TC-40


def test_tc40_only_unmarked_spec_type_agents_are_candidates(env):
    """TC-40 (7.2)."""
    h = Home(env)
    s = h.session()
    spec_agents = [impl("demo", 1, t(9, 0)), fix("demo", 1, t(9, 15))]
    s.tool_agents(spec_agents)
    gp = ag("implement demo: task 1", "general-purpose", t(9, 30))
    marked = ag(described("ag_1234abcd", "implement task 2"), "spec:implementer", t(9, 40))
    s.tool_agents([gp, marked])
    wf = s.implement_run(start="2026-09-01T09:50:00Z", args={"spec": "demo", "root": str(s.cwd)},
                         agents=[AgentSpec("implement demo: task 1"), AgentSpec("review task 1"), AgentSpec("fix demo: task 1 red")])

    data = plan(env)
    assert shape(data) == [("demo", 2)]
    blob = json.dumps(data)
    for aid in wf.agent_ids:
        assert aid not in blob, "a workflow agent is not even a candidate"
    assert gp.agent_id not in json.dumps(data["groups"]) and marked.agent_id not in json.dumps(data["groups"])


# --------------------------------------------------------------------------- TC-41


def test_tc41_grouping_by_session_spec_and_gap(env):
    """TC-41 (7.3)."""
    h = Home(env)
    s = h.session()
    s.tool_agents([
        impl("demo", 1, t(9, 0), 15), impl("demo", 2, t(9, 15), 15),
        impl("demo", 3, t(10, 15), 15), impl("demo", 4, t(10, 30), 15),   # a 45-minute hole after 09:30
        impl("ledger", 1, t(9, 30), 5), impl("ledger", 2, t(9, 35), 5),
        impl("demo", 1, t(13, 0), 15),
    ])
    assert shape(plan(env)) == [("demo", 1), ("demo", 4), ("ledger", 2)]
    assert shape(plan(env, "--gap", "300")) == [("demo", 5), ("ledger", 2)]
    assert shape(plan(env, "--gap", "30")) == [("demo", 1), ("demo", 2), ("demo", 2), ("ledger", 2)]


def test_tc41_the_gap_runs_from_an_agents_end_to_the_next_start(env):
    """TC-41: a 150-minute implementer followed 10 minutes after its end by a fixer is one group."""
    h = Home(env)
    s = h.session()
    s.tool_agents([impl("demo", 1, t(9, 0), 150), fix("demo", 1, t(11, 40), 10)])
    assert shape(plan(env)) == [("demo", 2)]


# --------------------------------------------------------------------------- TC-42


def _joiner_world(env, with_joiner: bool = True):
    h = Home(env)
    s = h.session()
    s.tool_agents([impl("demo", 1, t(9, 0), 30), impl("demo", 2, t(9, 30), 30)])
    s.tool_agents([impl("demo", 3, t(12, 30), 15), impl("demo", 4, t(12, 45), 15)])
    joiner = ag("review task 4 diff", "spec:reviewer", t(10, 5), 135)
    if with_joiner:
        s.tool_agents([joiner])
    return joiner


def test_tc42_a_spec_less_agent_joins_a_group_within_the_gap_else_is_left_out(env):
    """TC-42 (7.4)."""
    h = Home(env)
    s = h.session()
    s.tool_agents([impl("demo", 1, t(9, 0), 30), impl("demo", 2, t(9, 30), 30)])
    near = ag("review task 4 diff", "spec:reviewer", t(9, 40), 5)
    far = ag("review task 4 diff", "spec:reviewer", t(18, 0), 5)
    s.tool_agents([near, far])
    data = plan(env)
    assert shape(data) == [("demo", 3)]
    assert reasons(data) == {far.agent_id: "no spec"}


def test_tc42_a_joiner_changes_agents_and_cost_but_not_start_or_wall_time(env):
    """TC-42 (D15)."""
    with fresh_env() as env2:
        _joiner_world(env2, with_joiner=False)
        base = plan(env2)
    joiner = _joiner_world(env)
    data = plan(env)
    assert data["excluded"] == []
    first, second = sorted(data["groups"], key=lambda g: g["start"])
    assert "09:00" in first["start"] and first["wallMinutes"] == 60 and first["agents"] == 3
    assert "12:30" in second["start"] and second["agents"] == 2
    base_first, base_second = sorted(base["groups"], key=lambda g: g["start"])
    assert base_first["agents"] == 2 and base_first["wallMinutes"] == 60
    assert first["usd"] > base_first["usd"]
    assert second["usd"] == base_second["usd"]
    assert joiner.agent_id  # written


# --------------------------------------------------------------------------- TC-43


def test_tc43_write_creates_one_partial_record_per_group_with_the_git_outcome(env):
    """TC-43 (7.5)."""
    repo = spec_repo(env.root / "repo", "demo", "integrate/demo", tasks=3)
    now = datetime.now(timezone.utc)
    start = int((now - timedelta(minutes=40)).timestamp() * 1000)
    h = Home(env)
    s = h.session(cwd=repo, branch="integrate/demo")
    s.tool_agents([impl("demo", n, start + (n - 1) * 600_000, 9) for n in (1, 2, 3)])
    gone = env.root / "removed-checkout"
    g = h.session(cwd=gone, branch="integrate/gone")
    g.tool_agents([impl("gone", 1, start, 9)])
    for n in (1, 2, 3):
        land(repo, "demo", "integrate/demo", n)

    out = backfill(env, "--json")
    assert out.exit_code == 0, out.output
    recs = records(env)
    assert len(recs) == 2
    by_spec = {r["project"]["spec"]: r for r in recs.values()}
    for r in recs.values():
        assert r["partial"] is True and r["source"] == "agents"
        assert any(m.get("what") == "manifest" for m in r["missing"])
    demo = by_spec["demo"]
    assert Path(demo["project"]["root"]).resolve() == repo.resolve()
    assert demo["project"]["branch"] == "integrate/demo"
    assert demo["outcome"] == "complete"
    absent = by_spec["gone"]
    assert absent["project"]["root"] == str(gone) and absent["project"]["branch"] == "integrate/gone"
    assert absent["outcome"] is None
    assert any(m.get("what") == "outcome" for m in absent["missing"])


# --------------------------------------------------------------------------- TC-44


def test_tc44_a_rerun_is_idempotent(env):
    """TC-44 (7.6)."""
    h = Home(env)
    s = h.session()
    s.tool_agents([impl("demo", 1, t(9, 0)), impl("demo", 2, t(9, 10))])
    s.tool_agents([impl("ledger", 1, t(15, 0))])
    first = backfill(env)
    assert first.exit_code == 0, first.output
    assert count(first.output, "collected") == 2, first.output
    names = sorted(records(env))
    files = store_files(env.xdg)
    assert len(names) == 2

    second = backfill(env)
    assert second.exit_code == 0, second.output
    assert count(second.output, "already had") == 2, second.output
    assert count(second.output, "collected") == 0
    assert sorted(records(env)) == names and store_files(env.xdg) == files

    third = backfill(env, "--json")
    results = json.loads(third.stdout)["results"]
    assert len(results) == 2 and all(r["result"] == "unchanged" for r in results)
    assert sorted(records(env)) == names


# --------------------------------------------------------------------------- TC-45


@pytest.mark.parametrize("dry", [False, True])
def test_tc45_no_candidates_says_so_and_exits_0(env, dry):
    """TC-45 (7.7)."""
    h = Home(env)
    s = h.session()
    s.implement_run(args={"spec": "demo", "root": str(s.cwd)})
    s.tool_agents([ag("implement demo: task 1", "general-purpose", t(9, 0))])
    out = backfill(env, *(["--dry-run"] if dry else []))
    assert out.exit_code == 0, out.output
    assert "no unmarked spec-agent runs to backfill" in out.output
    assert store_files(env.xdg) == []


# --------------------------------------------------------------------------- TC-46


@pytest.mark.parametrize("gap", ["0", "-5", "abc", "1.5", ""])
@pytest.mark.parametrize("dry", [False, True])
def test_tc46_a_bad_gap_exits_2_and_writes_nothing(env, gap, dry):
    """TC-46 (7.8)."""
    h = Home(env)
    h.session().tool_agents([impl("demo", 1, t(9, 0))])
    out = backfill(env, *(["--dry-run"] if dry else []), "--gap", gap)
    assert out.exit_code == 2, out.output
    assert "No such option" not in out.output, "the option must exist and reject the value"
    assert store_files(env.xdg) == []


# --------------------------------------------------------------------------- TC-47 (CLI half)


def test_tc47_summary_lines_and_json_counts(env):
    """TC-47 (7.9)."""
    h = Home(env)
    s = h.session()
    s.tool_agents([impl("demo", 1, t(9, 0)), impl("ledger", 1, t(15, 0))])
    s.tool_agents([ag(described("ag_1234abcd", "implement task 3"), "spec:implementer", t(9, 20))])

    out = backfill(env)
    assert out.exit_code == 0, out.output
    assert [count(out.output, k) for k in ("collected", "already had", "updated", "failed", "left out")] == [2, 0, 0, 0, 1], out.output

    data = json.loads(backfill(env, "--json").stdout)
    assert (data["collected"], data["already_had"], data["updated"], data["failed"], data["excluded"]) == (0, 2, 0, 0, 1)
    assert len(data["results"]) == 2


# --------------------------------------------------------------------------- TC-60


def test_tc60_a_narrower_gap_never_double_counts_agents(env):
    """TC-60 (7.6)."""
    h = Home(env)
    s = h.session()
    ta = s.tool_agents([impl("demo", 1, t(9, 0), 15), impl("demo", 2, t(9, 15), 15),
                        impl("demo", 3, t(10, 15), 15), impl("demo", 4, t(10, 30), 15)])
    agent_ids = set(ta.agent_ids)
    assert shape(plan(env)) == [("demo", 4)] and shape(plan(env, "--gap", "30")) == [("demo", 2), ("demo", 2)]

    first = backfill(env, "--json")
    assert first.exit_code == 0, first.output
    recs1 = records(env)
    assert len(recs1) == 1
    (first_name, first_rec), = recs1.items()
    first_id = first_name[: -len(".json")]
    bytes1 = (runs_dir(env) / first_name).read_bytes()
    usd1 = run_usd(first_rec)

    dry = plan(env, "--gap", "30")
    assert dry["groups"] == []
    got = reasons(dry)
    assert set(got) == agent_ids
    assert all(r == f"overlaps backfill record {first_id}" for r in got.values()), got

    third = backfill(env, "--gap", "30")
    assert third.exit_code == 0, third.output
    recs3 = records(env)
    assert sorted(recs3) == [first_name]
    assert (runs_dir(env) / first_name).read_bytes() == bytes1
    seen: List[str] = []
    for rec in recs3.values():
        seen += [a["agentId"] for a in rec["agents"]]
    assert sorted(seen) == sorted(set(seen)) and set(seen) == agent_ids
    assert sum(run_usd(r) for r in recs3.values()) == usd1
