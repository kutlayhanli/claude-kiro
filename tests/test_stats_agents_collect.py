"""`ck stats collect --agents`: the record of an agent-mode run (TC-13 to TC-31, TC-33, TC-34, TC-58, TC-61).

Every test runs the real CLI as a subprocess over a fake Claude home with the real transcript/meta layout, a real
store and real git repos (`ck worktree report` and `ck cost` are the real thing). Project modules are imported inside
test bodies only. Manifest open/closed state is observed through `ck doctor` and `collect --all`.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence

import pytest

from stats_helpers import (
    AgentSpec,
    FakeClaudeHome,
    FakeSession,
    FakeToolAgents,
    ToolAgentSpec,
    all_bytes,
    backdate_manifest,
    begin as do_begin,
    ck_sub,
    described,
    fail,
    fresh_env,
    git,
    git_project,
    iso,
    iso_ms,
    land,
    load_record,
    logging_git,
    mark_done,
    set_manifest,
    spec_repo,
    stats,
    store_files,
    to_ms,
)

pytestmark = pytest.mark.usefixtures("no_network")

SPEC = "demo"
INTO = "integrate/demo"
VERB_TYPES = {
    "implement": "spec:implementer", "revise": "spec:implementer", "review": "spec:reviewer",
    "fix": "spec:fixer", "resolve": "spec:resolver", "create": "general-purpose",
}
FIGURES = ("usd", "input", "output", "cacheRead", "cacheCreation")


@pytest.fixture
def env():
    with fresh_env() as e:
        yield e


@pytest.fixture(autouse=True)
def _offline(no_network):
    """Every child process of every test here must have stayed off the network (NFR no network)."""
    yield
    assert not no_network.exists() or no_network.read_text().strip() == "", no_network.read_text()


# --------------------------------------------------------------------------- helpers


@dataclass
class W:
    env: Any
    repo: Path
    home: FakeClaudeHome
    sess: FakeSession
    spec: str = SPEC
    into: str = INTO


def world(env, spec: str = SPEC, into: str = INTO, tasks: int = 4, deps: Optional[dict] = None) -> W:
    repo = spec_repo(env.root / "repo", spec=spec, into=into, tasks=tasks, deps=deps)
    home = FakeClaudeHome(env.claude)
    sess = home.session(cwd=repo, git_branch=into)
    return W(env, repo, home, sess, spec, into)


def open_run(w: W, kind: str = "implement"):
    return do_begin(w.env, w.repo, kind, w.spec, w.into)


def start_ms(manifest: Path) -> int:
    return to_ms(json.loads(Path(manifest).read_text())["startTime"])


def agent(rid: Optional[str], rest: str, agent_type: Optional[str] = None, **kw) -> ToolAgentSpec:
    """An Agent-tool subagent; `rid=None` gives the unmarked description `rest`."""
    verb = rest.split()[0]
    t = agent_type if agent_type is not None else VERB_TYPES.get(verb, "general-purpose")
    return ToolAgentSpec(described(rid, rest) if rid else rest, agent_type=t, **kw)


def add_agents(w: W, manifest: Path, specs: Sequence[ToolAgentSpec], offset_s: int = 5, sess: Optional[FakeSession] = None) -> FakeToolAgents:
    """Write the agents so that they start `offset_s` after the manifest's start."""
    return (sess or w.sess).tool_agents(list(specs), start=iso_ms(start_ms(manifest) + offset_s * 1000))


def rests(rid: str, texts: Sequence[str], **kw) -> List[ToolAgentSpec]:
    return [agent(rid, t, **kw) for t in texts]


def collect_agents(env, rid: str, *extra: str, cwd: Optional[Path] = None, as_json: bool = True, extra_env: Optional[dict] = None):
    e = {**env, **(extra_env or {})}
    return stats("collect", "--agents", rid, *extra, *(["--json"] if as_json else []), env=e, cwd=cwd or env.root)


def collected(env, rid: str, *extra: str, cwd: Optional[Path] = None, extra_env: Optional[dict] = None) -> dict:
    r = collect_agents(env, rid, *extra, cwd=cwd, extra_env=extra_env)
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data["run_id"] == rid
    return data


def record_path(env, rid: str) -> Path:
    return env.xdg / "claude-kiro" / "runs" / f"{rid}.json"


def run_cost(rec: dict) -> dict:
    figs = [c for c in rec["costs"] if c.get("scope") == "run"]
    assert len(figs) == 1, rec["costs"]
    return figs[0]


def agent_ids(rec: dict) -> set:
    return {a["agentId"] for a in rec["agents"]}


def row_by_id(rec: dict, aid: str) -> dict:
    rows = [a for a in rec["agents"] if a["agentId"] == aid]
    assert len(rows) == 1, (aid, [a["agentId"] for a in rec["agents"]])
    return rows[0]


def init_project(env, repo: Path) -> None:
    r = ck_sub("init", "--no-allow-workflow", env=env, cwd=repo)
    assert r.exit_code == 0, r.output


def doctor_open(env, repo: Path) -> int:
    """How many agent-mode runs `ck doctor` says are older than 6 h and not collected (0 when it says nothing)."""
    out = ck_sub("doctor", env=env, cwd=repo).output
    m = re.search(r"(\d+) agent-mode run", out)
    return int(m.group(1)) if m else 0


@contextlib.contextmanager
def unreadable(path: Path) -> Iterator[Path]:
    path = Path(path)
    old = stat.S_IMODE(path.stat().st_mode)
    path.chmod(0)
    try:
        yield path
    finally:
        path.chmod(old)


needs_user = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")


@contextlib.contextmanager
def committer_date(when: datetime) -> Iterator[None]:
    """Commits made inside get this committer (and author) date: `ck worktree report` reads it as `mergedAt`."""
    stamp = f"{int(when.timestamp())} +0000"
    old = {k: os.environ.get(k) for k in ("GIT_COMMITTER_DATE", "GIT_AUTHOR_DATE")}
    os.environ["GIT_COMMITTER_DATE"] = os.environ["GIT_AUTHOR_DATE"] = stamp
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def counts_of(rec: dict) -> dict:
    from claude_kiro.analytics.share import build_shareable

    return build_shareable(rec)["counts"]


def net_log_empty(log: Path) -> bool:
    return not log.exists() or log.read_text().strip() == ""


# --------------------------------------------------------------------------- TC-13


def test_tc13_marked_agents_of_five_roles_are_collected(env):
    """TC-13."""
    w = world(env, spec="zephyr-payroll", into="integrate/zephyr-payroll")
    rid, mp = open_run(w)
    texts = ["implement task 1", "implement task 2", "review task 1 round 1", "fix 1", "resolve task 3"]
    types = ["spec:implementer", "spec:test-writer", "spec:reviewer", "spec:fixer", "spec:resolver"]
    marked = [agent(rid, t, ty) for t, ty in zip(texts, types)]
    noise = [
        agent(None, "create requirements", "general-purpose"),
        agent("ag_0badc0de", "implement task 1", "spec:implementer"),
    ]
    ta = add_agents(w, mp, marked + noise)
    w.sess.implement_run()  # a workflow run in the same session
    out = collect_agents(env, rid, cwd=w.repo)
    assert out.exit_code == 0, out.output
    data = json.loads(out.stdout)
    assert set(data) == {"run_id", "result", "path", "partial"}
    assert data["run_id"] == rid and data["result"] == "collected" and data["partial"] is False
    rec = load_record(env.xdg, rid)
    assert Path(data["path"]) == record_path(env, rid)
    assert rec["source"] == "agents" and rec["workflow"] == "spec-implement"
    assert agent_ids(rec) == {ta._aid(i) for i in range(5)}
    assert rec["project"]["spec"] == "zephyr-payroll"
    assert Path(rec["project"]["root"]).resolve() == w.repo.resolve()
    assert rec["project"]["branch"] == "integrate/zephyr-payroll"
    assert "zephyr-payroll" not in str(rec.get("description", ""))
    assert run_cost(rec)["source"] == "agent_transcripts"
    assert not [m for m in rec["missing"] if m.get("what") == "result"]
    assert isinstance(rec["wallMs"], int) and rec["wallMs"] > 0
    first_start = min(a["startedAtMs"] for a in rec["agents"])
    assert first_start > start_ms(mp)
    text = collect_agents(env, rid, cwd=w.repo, as_json=False)
    assert text.exit_code == 0 and str(record_path(env, rid)) in text.stdout


def test_tc13_collected_run_is_closed_for_doctor_and_collect_all(env):
    """TC-13 (3.8): with the manifest backdated 7 h, collecting it closes it."""
    w = world(env)
    init_project(env, w.repo)
    rid, mp = open_run(w)
    add_agents(w, mp, rests(rid, ["implement task 1", "review task 1 round 1"]))
    backdate_manifest(mp, 7)
    assert doctor_open(env, w.repo) == 1
    collected(env, rid, cwd=w.repo)
    assert doctor_open(env, w.repo) == 0
    r = stats("collect", "--all", "--json", env=env, cwd=w.repo)
    assert r.exit_code == 0, r.output
    assert rid not in r.output
    summary = json.loads(r.stdout)
    assert summary["collected"] == 0


# --------------------------------------------------------------------------- TC-14


def test_tc14_per_agent_figures_and_agreement_with_ck_cost(env):
    """TC-14."""
    from claude_kiro import bench

    w = world(env)
    rid, mp = open_run(w)
    reqs = {
        "claude-sonnet-5-5": [("rq_s1", 1000, 500, 2000, 100), ("rq_s2", 3000, 700, 9000, 400)],
        "claude-opus-5-5": [("rq_o1", 2500, 900, 4000, 0)],
        "claude-haiku-5-5": [("rq_h1", 400, 100, 300, 10)],
    }
    durations = {"claude-sonnet-5-5": 60_000, "claude-opus-5-5": 45_000, "claude-haiku-5-5": 30_000}
    specs = [
        agent(rid, f"implement task {i + 1}", model=m, requests=r, duration_ms=durations[m])
        for i, (m, r) in enumerate(reqs.items())
    ]
    ta = add_agents(w, mp, specs)
    collected(env, rid, cwd=w.repo)
    rec = load_record(env.xdg, rid)
    total = 0.0
    for i, (model, r) in enumerate(reqs.items()):
        row = row_by_id(rec, ta._aid(i))
        assert bench.normalize_model(row["model"]) == bench.normalize_model(model)
        assert row["input"] == sum(x[1] for x in r)
        assert row["output"] == sum(x[2] for x in r)
        assert row["cacheRead"] == sum(x[3] for x in r)
        assert row["cacheCreation"] == sum(x[4] for x in r)
        assert row["ms"] == durations[model]
        assert row["usd"] > 0
        total += row["usd"]
    assert run_cost(rec)["usd"] == pytest.approx(total)
    cost = ck_sub("cost", "--session", w.sess.session_id, "--json", env=env, cwd=w.repo)
    assert cost.exit_code == 0, cost.output
    priced = json.loads(cost.stdout)["priced_usd"]
    assert round(run_cost(rec)["usd"], 2) == round(priced, 2)
    assert round(total, 2) == round(priced, 2)


# --------------------------------------------------------------------------- TC-15


def test_tc15_a_session_less_manifest_finds_agents_in_any_session(env):
    """TC-15 (a)."""
    w = world(env)
    rid, mp = open_run(w)
    set_manifest(mp, sessionId=None)
    sess_b = w.home.session(slug="-work-b", cwd=w.repo)
    ta = add_agents(w, mp, rests(rid, ["implement task 1"]), sess=sess_b)
    collected(env, rid, cwd=w.repo)
    assert agent_ids(load_record(env.xdg, rid)) == {ta._aid(0)}


def test_tc15_a_manifest_session_limits_the_search_to_that_session(env):
    """TC-15 (b)."""
    w = world(env)
    rid, mp = open_run(w)
    sess_a = w.sess
    sess_b = w.home.session(slug="-work-b", cwd=w.repo)
    set_manifest(mp, sessionId=sess_a.session_id)
    ta_a = add_agents(w, mp, rests(rid, ["implement task 1"]), sess=sess_a)
    add_agents(w, mp, rests(rid, ["implement task 2"]), sess=sess_b, offset_s=20)
    collected(env, rid, cwd=w.repo)
    assert agent_ids(load_record(env.xdg, rid)) == {ta_a._aid(0)}


def test_tc15_without_marked_agents_in_the_manifest_session_every_session_is_searched(env):
    """TC-15 (c)."""
    w = world(env)
    rid, mp = open_run(w)
    sess_b = w.home.session(slug="-work-b", cwd=w.repo)
    set_manifest(mp, sessionId=w.sess.session_id)
    ta_b = add_agents(w, mp, rests(rid, ["implement task 1"]), sess=sess_b)
    collected(env, rid, cwd=w.repo)
    assert agent_ids(load_record(env.xdg, rid)) == {ta_b._aid(0)}


# --------------------------------------------------------------------------- TC-16

FOUR = [f"implement task {n}" for n in (1, 2, 3, 4)]


def _all_merged(w):
    for n in (1, 2, 3, 4):
        land(w.repo, w.spec, w.into, n)


def _mixed(w):
    land(w.repo, w.spec, w.into, 1)
    mark_done(w.repo, w.spec, w.into, 2)
    fail(w.repo, w.spec, 3, w.into)


def _one_failed(w):
    land(w.repo, w.spec, w.into, 1)
    fail(w.repo, w.spec, 2, w.into)


def _rest_unstarted(w):
    land(w.repo, w.spec, w.into, 1)


STATES = [
    pytest.param(_all_merged, None, "complete", (4, 0, 0), id="all-merged"),
    pytest.param(_mixed, {"4": ["3"]}, "failed", (2, 1, 1), id="merged-done-failed-blocked"),
    pytest.param(_one_failed, None, "failed", (1, 1, 2), id="one-merged-one-failed"),
    pytest.param(_rest_unstarted, None, "blocked", (1, 0, 3), id="one-merged-rest-unstarted"),
]


@pytest.mark.parametrize("apply,deps,outcome,expected", STATES)
def test_tc16_outcome_and_counts_come_from_the_git_report(env, apply, deps, outcome, expected):
    """TC-16."""
    w = world(env, deps=deps)
    rid, mp = open_run(w)
    add_agents(w, mp, rests(rid, FOUR))
    apply(w)
    collected(env, rid, cwd=w.repo)
    rec = load_record(env.xdg, rid)
    assert rec["outcome"] == outcome
    c = counts_of(rec)
    assert (c["tasks_merged"], c["tasks_failed"], c["tasks_not_started"]) == expected


def test_tc16_partial_run_judges_only_the_tasks_its_agents_name_complete(env):
    """TC-16 (D11): task 2 failed before `begin`, 3-4 unstarted; the run names only task 1."""
    w = world(env)
    fail(w.repo, w.spec, 2, w.into)
    rid, mp = open_run(w)
    add_agents(w, mp, rests(rid, ["implement task 1"]))
    land(w.repo, w.spec, w.into, 1)
    collected(env, rid, cwd=w.repo)
    rec = load_record(env.xdg, rid)
    assert rec["outcome"] == "complete"
    c = counts_of(rec)
    assert c["tasks_merged"] == 1 and c["tasks_failed"] == 0 and c["tasks_not_started"] == 0


def test_tc16_partial_run_fails_by_its_one_named_task(env):
    """TC-16 (D11): the named task's branch failed."""
    w = world(env)
    fail(w.repo, w.spec, 2, w.into)
    rid, mp = open_run(w)
    add_agents(w, mp, rests(rid, ["implement task 1"]))
    fail(w.repo, w.spec, 1, w.into)
    collected(env, rid, cwd=w.repo)
    assert load_record(env.xdg, rid)["outcome"] == "failed"


def test_tc16_partial_run_naming_a_task_left_unstarted_is_blocked(env):
    """TC-16 (D11): tasks 1 and 3 named, 1 landed, 3 unstarted."""
    w = world(env)
    fail(w.repo, w.spec, 2, w.into)
    rid, mp = open_run(w)
    add_agents(w, mp, rests(rid, ["implement task 1", "implement task 3"]))
    land(w.repo, w.spec, w.into, 1)
    collected(env, rid, cwd=w.repo)
    rec = load_record(env.xdg, rid)
    assert rec["outcome"] == "blocked"
    assert counts_of(rec)["tasks_not_started"] == 1


def test_tc16_a_run_naming_no_task_has_no_outcome(env):
    """TC-16 (D11)."""
    w = world(env)
    rid, mp = open_run(w)
    add_agents(w, mp, rests(rid, ["fix 1"]))
    land(w.repo, w.spec, w.into, 1)
    collected(env, rid, cwd=w.repo)
    assert load_record(env.xdg, rid)["outcome"] is None


def test_tc16_resumed_spec_counts_only_merges_after_begin(env):
    """TC-16 (1.2): task 1 landed before `begin`, task 2 after."""
    w = world(env)
    land(w.repo, w.spec, w.into, 1)
    rid, mp = open_run(w)
    assert json.loads(mp.read_text())["doneAtStart"] == ["1"]
    add_agents(w, mp, rests(rid, ["implement task 1", "implement task 2"]))
    land(w.repo, w.spec, w.into, 2)
    collected(env, rid, cwd=w.repo)
    rec = load_record(env.xdg, rid)
    assert counts_of(rec)["tasks_merged"] == 1
    assert [str(t["task"]) for t in rec["tasks"]] == ["2"]


def test_tc16_unknown_done_at_start_uses_the_time_window(env):
    """TC-16: `doneAtStart` null counts only merges from the start to 30 minutes after the last agent."""
    w = world(env)
    rid, mp = open_run(w)
    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    set_manifest(mp, doneAtStart=None, startTime=iso(t0))
    add_agents(w, mp, rests(rid, FOUR), offset_s=60)
    with committer_date(t0 - timedelta(minutes=30)):
        land(w.repo, w.spec, w.into, 1)
    with committer_date(t0 + timedelta(minutes=5)):
        land(w.repo, w.spec, w.into, 2)
    with committer_date(t0 + timedelta(minutes=150)):
        land(w.repo, w.spec, w.into, 3)
    collected(env, rid, cwd=w.repo)
    assert counts_of(load_record(env.xdg, rid))["tasks_merged"] == 1


# --------------------------------------------------------------------------- TC-17


def _root_deleted(w, mp):
    set_manifest(mp, root=str(w.env.root / "gone"))


def _plain_dir(w, mp):
    plain = w.env.root / "plain"
    plain.mkdir()
    set_manifest(mp, root=str(plain))


def _into_deleted(w, mp):
    git(w.repo, "branch", "-D", w.into)


def _no_tasks_md(w, mp):
    set_manifest(mp, spec="no-such-spec")


@pytest.mark.parametrize("break_it", [_root_deleted, _plain_dir, _into_deleted, _no_tasks_md],
                         ids=["root-deleted", "plain-dir", "into-deleted", "no-tasks-md"])
def test_tc17_an_unavailable_report_gives_an_unknown_outcome(env, break_it):
    """TC-17."""
    w = world(env)
    rid, mp = open_run(w)
    add_agents(w, mp, rests(rid, ["implement task 1"]))
    if break_it is not _into_deleted:
        land(w.repo, w.spec, w.into, 1)
    break_it(w, mp)
    r = collect_agents(env, rid, cwd=env.root)
    assert r.exit_code == 0, r.output
    rec = load_record(env.xdg, rid)
    assert rec["outcome"] is None
    assert {"agentId": None, "what": "outcome"} in rec["missing"] or any(m.get("what") == "outcome" for m in rec["missing"])
    c = counts_of(rec)
    for key in ("tasks_merged", "tasks_failed", "tasks_not_started"):
        assert key not in c


# --------------------------------------------------------------------------- TC-18


@pytest.mark.parametrize("kind", ["create", "review"])
def test_tc18_create_and_review_runs_never_read_the_report(env, kind):
    """TC-18."""
    w = world(env)
    rid, mp = open_run(w, kind)
    add_agents(w, mp, [agent(rid, "create requirements"), agent(rid, "create design")])
    bin_dir = env.root / "bin"
    bin_dir.mkdir()
    log = env.root / "git.log"
    logging_git(bin_dir, log)
    e = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    out = collect_agents(env, rid, cwd=w.repo, extra_env=e)
    assert out.exit_code == 0, out.output
    rec = load_record(env.xdg, rid)
    assert rec["outcome"] is None
    assert rec["workflow"] == "spec-create" and rec["mode"] == kind
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    assert not [c for c in calls if "show" in c and any("tasks.md" in a for a in c)], calls


# --------------------------------------------------------------------------- TC-19


def test_tc19_collecting_again_is_unchanged_then_updated_by_new_usage(env):
    """TC-19."""
    w = world(env)
    rid, mp = open_run(w)
    ta = add_agents(w, mp, rests(rid, ["implement task 1"]))
    first = collected(env, rid, cwd=w.repo)
    assert first["result"] == "collected"
    before = record_path(env, rid).read_bytes()
    second = collected(env, rid, cwd=w.repo)
    assert second["result"] == "unchanged"
    assert record_path(env, rid).read_bytes() == before
    # a new request in the transcript
    tx = ta.transcript(0)
    last = json.loads(tx.read_text().splitlines()[-1])
    last["requestId"] = "rq_extra"
    last["message"] = {**last["message"], "id": "msg_extra"}
    with tx.open("a") as fh:
        fh.write(json.dumps(last) + "\n")
    third = collected(env, rid, cwd=w.repo)
    assert third["result"] == "updated"
    assert record_path(env, rid).read_bytes() != before
    assert json.loads(record_path(env, rid).read_text())["agents"][0]["output"] > json.loads(before)["agents"][0]["output"]
    assert [p for p in store_files(env.xdg) if p.parent.name == "runs"] == [record_path(env, rid)]


# --------------------------------------------------------------------------- TC-20


def test_tc20_marked_agents_without_a_manifest_give_a_partial_record(env):
    """TC-20."""
    w = world(env)
    rid = "ag_0a1b2c3d"
    ta = w.sess.tool_agents(rests(rid, ["implement task 1", "review task 1 round 1"]))
    out = collect_agents(env, rid, cwd=w.repo)
    assert out.exit_code == 0, out.output
    assert json.loads(out.stdout)["partial"] is True
    rec = load_record(env.xdg, rid)
    assert any(m.get("what") == "manifest" for m in rec["missing"])
    assert Path(rec["project"]["root"]).resolve() == w.repo.resolve()
    assert rec["project"].get("spec") is None
    assert rec["outcome"] is None
    assert rec["workflow"] == "spec-implement"
    assert agent_ids(rec) == {ta._aid(0), ta._aid(1)}


def test_tc20_all_create_step_agents_give_a_create_record_without_a_report(env):
    """TC-20 (variant)."""
    w = world(env)
    rid = "ag_0a1b2c3d"
    w.sess.tool_agents([agent(rid, "create requirements", "general-purpose"), agent(rid, "create design", "general-purpose")])
    bin_dir = env.root / "bin"
    bin_dir.mkdir()
    log = env.root / "git.log"
    logging_git(bin_dir, log)
    out = collect_agents(env, rid, cwd=w.repo, extra_env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
    assert out.exit_code == 0, out.output
    rec = load_record(env.xdg, rid)
    assert rec["workflow"] == "spec-create" and rec.get("mode") is None
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    assert not [c for c in calls if "show" in c], calls


# --------------------------------------------------------------------------- TC-21, TC-22


def test_tc21_an_unknown_run_is_not_found(env):
    """TC-21."""
    FakeClaudeHome(env.claude)
    before = store_files(env.xdg)
    r = collect_agents(env, "ag_0a1b2c3d", as_json=False)
    assert r.exit_code != 0
    assert "not found" in r.output
    assert str(env.claude / "projects") in r.output
    assert "runs-open" in r.output
    assert store_files(env.xdg) == before


def test_tc22_a_manifest_without_marked_agents_stays_open(env):
    """TC-22."""
    w = world(env)
    init_project(env, w.repo)
    rid, mp = open_run(w)
    w.sess.tool_agents([agent(None, "create requirements", "general-purpose")])
    r = collect_agents(env, rid, cwd=w.repo, as_json=False)
    assert r.exit_code != 0
    assert "no agents carry the marker" in r.output
    assert not record_path(env, rid).exists()
    backdate_manifest(mp, 7)
    assert doctor_open(env, w.repo) == 1


# --------------------------------------------------------------------------- TC-23, TC-24

BAD_IDS = ["ag_0123ABCD", "ag_0123abc", "ag_0123abcde", "AG_0123abcd", "wf_0123abcd", "ag-0123abcd", " ag_0123abcd",
           "[ck:ag_0123abcd]"]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_tc23_a_malformed_agent_run_id_is_a_usage_error(env, bad):
    """TC-23."""
    r = stats("collect", "--agents", bad, env=env, cwd=env.root)
    assert r.exit_code == 2, r.output
    assert "ag_" in r.output and "hex" in r.output
    assert store_files(env.xdg) == []


def test_tc23_run_with_an_agent_mode_id_points_to_agents(env):
    """TC-23."""
    r = stats("collect", "--run", "ag_0123abcd", env=env, cwd=env.root)
    assert r.exit_code == 2, r.output
    assert "--agents" in r.output
    assert store_files(env.xdg) == []


@pytest.mark.parametrize("other", [["--run", "wf_a"], ["--all"], ["--agents-backfill"]])
def test_tc24_agents_cannot_be_combined(env, other):
    """TC-24."""
    r = stats("collect", "--agents", "ag_0123abcd", *other, env=env, cwd=env.root)
    assert r.exit_code == 2, r.output
    assert store_files(env.xdg) == []


# --------------------------------------------------------------------------- TC-25


def test_tc25_faulty_transcripts_and_metas_never_make_a_zero_row(env):
    """TC-25."""
    w = world(env)
    rid, mp = open_run(w)
    two = lambda tag: [(f"rq_{tag}_1", 1000, 500, 2000, 100), (f"rq_{tag}_2", 2000, 300, 1000, 50)]  # noqa: E731
    specs = [agent(rid, f"implement task {i + 1}", requests=two(i)) for i in range(5)]
    ta = add_agents(w, mp, specs)
    ids = [ta._aid(i) for i in range(5)]
    ta.drop_transcript(0)
    ta.truncate_transcript(1)
    ta.corrupt_meta(2)
    ta.drop_meta(3)
    r = collect_agents(env, rid, cwd=w.repo)
    assert r.exit_code == 0, r.output
    rec = load_record(env.xdg, rid)
    missing = rec["missing"]
    assert {"agentId": ids[0], "what": "transcript"} in missing
    assert {"agentId": ids[1], "what": "transcript-truncated"} in missing
    assert run_cost(rec)["source"] == "estimate"
    dropped = row_by_id(rec, ids[0])
    assert dropped["ms"] is None and all(dropped[k] is None for k in FIGURES)
    truncated = row_by_id(rec, ids[1])
    assert truncated["transcript"] == "truncated"
    assert all(truncated[k] != 0 for k in FIGURES)
    intact = row_by_id(rec, ids[4])
    assert intact["usd"] > 0
    for aid in (ids[2], ids[3]):
        if aid in agent_ids(rec):
            assert {"agentId": aid, "what": "meta"} in missing
            row = row_by_id(rec, aid)
            assert not all(row[k] == 0 for k in FIGURES)


# --------------------------------------------------------------------------- TC-26


@needs_user
def test_tc26_unreadable_projects_directory_is_refused_and_the_manifest_stays_open(env):
    """TC-26."""
    w = world(env)
    init_project(env, w.repo)
    rid, mp = open_run(w)
    add_agents(w, mp, rests(rid, ["implement task 1"]))
    with unreadable(env.claude / "projects"):
        r = collect_agents(env, rid, cwd=w.repo, as_json=False)
    assert r.exit_code != 0
    assert "outside the sandbox" in r.output
    assert not record_path(env, rid).exists()
    backdate_manifest(mp, 7)
    assert doctor_open(env, w.repo) == 1


# --------------------------------------------------------------------------- TC-27


@pytest.mark.parametrize("via", ["agents", "all"])
def test_tc27_near_miss_markers_are_never_collected(env, via):
    """TC-27."""
    w = world(env)
    rid, mp = open_run(w)
    good = rests(rid, ["implement task 1"])
    near = [
        agent(None, "create requirements", "general-purpose"),
        agent("ag_0badc0de", "create design", "general-purpose"),
        ToolAgentSpec(f"x [ck:{rid}] create design", agent_type="general-purpose"),
        ToolAgentSpec(f"[CK:{rid}] create design", agent_type="general-purpose"),
        ToolAgentSpec(f"[ck: {rid}] create design", agent_type="general-purpose"),
        ToolAgentSpec(f" [ck:{rid}] create design", agent_type="general-purpose"),
    ]
    ta = add_agents(w, mp, good + near)
    if via == "agents":
        collected(env, rid, cwd=w.repo)
    else:
        backdate_manifest(mp, 7)
        r = stats("collect", "--all", env=env, cwd=w.repo)
        assert r.exit_code == 0, r.output
    rec = load_record(env.xdg, rid)
    assert agent_ids(rec) == {ta._aid(0)}
    blob = all_bytes(env.xdg)
    for i in range(1, 1 + len(near)):
        assert ta._aid(i).encode() not in blob
    assert not record_path(env, "ag_0badc0de").exists()


# --------------------------------------------------------------------------- TC-28

SID_28 = "5e55a10e-0000-4000-8000-0000000000e8"
WT_PATH = "/home/dana/acme-ledger/.ck/worktrees/zephyr-payroll-task-3"
WT_BRANCH = "feat/zephyr-payroll-task-3"


def test_tc28_the_workflow_record_is_independent_of_agent_mode_data():
    """TC-28 (metamorphic)."""
    with fresh_env() as e1, fresh_env() as e2:
        proj = git_project(e1.root, None, name="proj")
        outputs = []
        for e, extra in ((e1, False), (e2, True)):
            home = FakeClaudeHome(e.claude)
            s = home.session(cwd=proj, session_id=SID_28)
            s.implement_run("wf_meta_1", args={"spec": "demo", "root": str(proj)})
            if extra:
                rid, mp = do_begin(e, proj, "implement", "demo", "main")
                other = home.session(slug="-work-other", cwd=proj)
                other.tool_agents(rests(rid, ["implement task 1"]), start=iso_ms(start_ms(mp) + 5000))
            r = stats("collect", "--run", "wf_meta_1", env=e, cwd=proj)
            assert r.exit_code == 0, r.output
            outputs.append(r.stdout.replace(str(e.xdg), "<xdg>"))
        assert record_path(e1, "wf_meta_1").read_bytes() == record_path(e2, "wf_meta_1").read_bytes()
        assert outputs[0] == outputs[1]
        again = stats("collect", "--run", "wf_meta_1", "--json", env=e2, cwd=proj)
        assert json.loads(again.stdout)["result"] == "unchanged"


def test_tc28_worktree_keys_in_workflow_metas_change_only_terms(env):
    """TC-28 (variant)."""
    proj = git_project(env.root, None, name="proj")
    home = FakeClaudeHome(env.claude)
    run = home.session(cwd=proj).implement_run("wf_meta_2", args={"spec": "demo", "root": str(proj)})
    first = stats("collect", "--run", "wf_meta_2", "--json", env=env, cwd=proj)
    assert json.loads(first.stdout)["result"] == "collected"
    old = load_record(env.xdg, "wf_meta_2")
    for i in range(len(run.agent_ids)):
        p = run.meta_path(i)
        m = json.loads(p.read_text())
        m.update({"worktreePath": WT_PATH, "worktreeBranch": WT_BRANCH})
        p.write_text(json.dumps(m))
    second = stats("collect", "--run", "wf_meta_2", "--json", env=env, cwd=proj)
    assert json.loads(second.stdout)["result"] == "updated"
    new = load_record(env.xdg, "wf_meta_2")
    assert {k for k in set(old) | set(new) if old.get(k) != new.get(k)} == {"terms"}
    assert WT_PATH in new["terms"] and WT_BRANCH in new["terms"]
    third = stats("collect", "--run", "wf_meta_2", "--json", env=env, cwd=proj)
    assert json.loads(third.stdout)["result"] == "unchanged"


# --------------------------------------------------------------------------- TC-29

LONG_ID = "a5eedc0ffee{:04x}"
# (agent type, description rest, expected role, task, attempt, round); None = the table gives no value
TABLE = [
    ("spec:implementer", "implement task 3", "implementer", "3", 1, None),
    ("ck-implementer", "implement task 3 retry 2", "implementer", "3", 3, None),
    ("spec:test-writer", "implement task 1", "test-writer", "1", 1, None),
    ("spec:implementer", "review task 3 round 2", "reviewer", "3", None, 2),
    ("ck-reviewer", "review task 3 round 1", "reviewer", "3", None, 1),
    ("spec:fixer", "fix 1", "fixer", None, 1, None),
    ("spec:resolver", "resolve task 4", "resolver", "4", None, None),
    ("general-purpose", "implement task 3", "implementer", "3", 1, None),
    ("general-purpose", "review task 3 round 1", "reviewer", "3", None, 1),
    ("general-purpose", "resolve task 4", "resolver", "4", None, None),
    ("general-purpose", "fix 2", "fixer", None, 2, None),
]


def test_tc29_role_task_attempt_round_follow_the_template_grammar(env):
    """TC-29."""
    from claude_kiro.analytics import parse_agent_description

    w = world(env)
    rid, mp = open_run(w)
    specs = [ToolAgentSpec(described(rid, rest), agent_type=ty, agent_id=LONG_ID.format(i)) for i, (ty, rest, *_r) in enumerate(TABLE)]
    specs.append(ToolAgentSpec(described(rid, "implement task 2"), agent_type="spec:implementer", model="claude-sonnet-5-5",
                               meta_model="claude-opus-5-5", agent_id=LONG_ID.format(len(TABLE))))
    specs.append(ToolAgentSpec(described(rid, "implement task 2 retry 1"), agent_type="spec:implementer", model="claude-sonnet-5-5",
                               agent_id=LONG_ID.format(len(TABLE) + 1)))
    add_agents(w, mp, specs)
    collected(env, rid, cwd=w.repo)
    rec = load_record(env.xdg, rid)
    for i, (ty, rest, role, task, attempt, rnd) in enumerate(TABLE):
        row = row_by_id(rec, LONG_ID.format(i))
        assert row["role"] == role, (rest, ty)
        assert (str(row["task"]) if row["task"] is not None else None) == task
        if attempt is not None:
            assert row["attempt"] == attempt
        if rnd is not None:
            assert row["round"] == rnd
        parsed = parse_agent_description(described(rid, rest), ty)
        assert row["role"] == parsed["role"] and row["round"] == parsed["round"] and row["attempt"] == parsed["attempt"]
        assert (str(row["task"]) if row["task"] is not None else None) == (str(parsed["task"]) if parsed["task"] is not None else None)
    assert "opus" in row_by_id(rec, LONG_ID.format(len(TABLE)))["model"]
    assert "sonnet" in row_by_id(rec, LONG_ID.format(len(TABLE) + 1))["model"]


# --------------------------------------------------------------------------- TC-30

PHASES = {
    "write requirements.md": "Requirements", "write design.md": "Design + Test Plan", "write test-plan.md": "Design + Test Plan",
    "write implementation track": "Task Tracks", "write test track": "Task Tracks", "link tracks into tasks.md": "Link",
    "snapshot spec before revision": "Revise", "revise spec + write review.md": "Revise",
    "remove pre-revision snapshot": "Consistency Check",
}


def phase_of(label: str) -> str:
    if label in PHASES:
        return PHASES[label]
    if label.startswith(("review:", "refute:")):
        return "Adversarial Review"
    if label.startswith("consistency check"):
        return "Consistency Check"
    raise AssertionError(f"no phase for {label}")


def test_tc30_create_steps_match_the_workflow_roles_and_phases(env):
    """TC-30 (metamorphic)."""
    from claude_kiro.analytics import AGENT_CREATE_STEPS

    w = world(env)
    rid, mp = open_run(w, "create")
    steps = list(AGENT_CREATE_STEPS)
    add_agents(w, mp, [agent(rid, f"create {s}", "general-purpose") for s in steps])
    wf_session = w.home.session(slug="-work-wf", cwd=w.repo)
    run = wf_session.create_run("wf_create_30", agents=[AgentSpec(AGENT_CREATE_STEPS[s], phase=phase_of(AGENT_CREATE_STEPS[s])) for s in steps])
    collected(env, rid, cwd=w.repo)
    r = stats("collect", "--run", run.run_id, env=env, cwd=w.repo)
    assert r.exit_code == 0, r.output
    a, b = load_record(env.xdg, rid), load_record(env.xdg, run.run_id)
    assert [x["role"] for x in a["agents"]] == [x["role"] for x in b["agents"]]
    assert [x["phase"] for x in a["agents"]] == [x["phase"] for x in b["agents"]]


# --------------------------------------------------------------------------- TC-31


def test_tc31_non_template_descriptions_are_other_and_shown_in_stats(env):
    """TC-31."""
    w = world(env)
    rid, mp = open_run(w)
    texts = ["implement Task 3", "implement demo: task 3", "review task 3", "deploy"]
    add_agents(w, mp, [ToolAgentSpec(described(rid, t), agent_type="spec:implementer") for t in texts])
    collected(env, rid, cwd=w.repo)
    rec = load_record(env.xdg, rid)
    assert [a["role"] for a in rec["agents"]] == ["other"] * 4
    assert rec["byRole"]["other"]["agents"] == 4
    out = stats("show", env=env, cwd=w.repo)
    assert out.exit_code == 0, out.output
    lines = [ln for ln in out.stdout.splitlines() if ln.startswith("other:")]
    assert len(lines) == 1 and "4 agents" in lines[0], out.stdout


# --------------------------------------------------------------------------- TC-33


def _case_collected(w, rid, mp):
    add_agents(w, mp, rests(rid, ["implement task 1"]))
    return rid, str(record_path(w.env, rid))


def _case_invalid(w, rid, mp):
    return "bogus", "ck stats collect --agents bogus"


def _case_not_found(w, rid, mp):
    return "ag_0a1b2c3d", "ck stats collect --agents ag_0a1b2c3d"


def _case_no_agents(w, rid, mp):
    return rid, f"ck stats collect --agents {rid}"


def _case_unreadable(w, rid, mp):
    add_agents(w, mp, rests(rid, ["implement task 1"]))
    return rid, f"ck stats collect --agents {rid}"


@pytest.mark.parametrize("case", [_case_collected, _case_invalid, _case_not_found, _case_no_agents, _case_unreadable],
                         ids=["collected", "invalid-id", "not-found", "no-marked-agents", "projects-unreadable"])
def test_tc33_auto_prints_one_line_and_exits_zero(env, case):
    """TC-33."""
    if case is _case_unreadable and os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    w = world(env)
    rid, mp = open_run(w)
    target, expect = case(w, rid, mp)
    cm = unreadable(env.claude / "projects") if case is _case_unreadable else contextlib.nullcontext()
    with cm:
        r = stats("collect", "--agents", target, "--auto", env=env, cwd=w.repo)
    assert r.exit_code == 0, r.output
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, r.stdout
    assert expect in lines[0], lines[0]


# --------------------------------------------------------------------------- TC-34


def test_tc34_auto_collect_off_skips_but_a_manual_collect_works(env):
    """TC-34."""
    w = world(env)
    init_project(env, w.repo)
    rid, mp = open_run(w)
    add_agents(w, mp, rests(rid, ["implement task 1"]))
    backdate_manifest(mp, 7)
    off = ck_sub("agents", "set", "run.analytics", "false", env=env, cwd=w.repo)
    assert off.exit_code == 0, off.output
    r = stats("collect", "--agents", rid, "--auto", env=env, cwd=w.repo)
    assert r.exit_code == 0, r.output
    assert "auto-collect is off" in r.stdout
    assert not record_path(env, rid).exists()
    assert doctor_open(env, w.repo) == 1
    manual = collected(env, rid, cwd=w.repo)
    assert manual["result"] == "collected" and record_path(env, rid).is_file()


# --------------------------------------------------------------------------- TC-58


@needs_user
def test_tc58_begin_and_auto_never_block_on_an_unwritable_store_and_unreadable_home(env):
    """TC-58."""
    w = world(env)
    store = env.xdg / "claude-kiro"
    store.mkdir()
    old = stat.S_IMODE(store.stat().st_mode)
    with contextlib.ExitStack() as stack:
        store.chmod(0o500)
        stack.callback(store.chmod, old)
        stack.enter_context(unreadable(env.claude / "projects"))
        b = stats("begin", "--kind", "implement", "--spec", w.spec, "--json", env=env, cwd=w.repo)
        assert b.exit_code == 0, b.output
        rid = json.loads(b.stdout)["run_id"]
        assert re.fullmatch(r"ag_[0-9a-f]{8}", rid)
        a = stats("collect", "--agents", rid, "--auto", env=env, cwd=w.repo)
        assert a.exit_code == 0, a.output
        assert len([ln for ln in a.stdout.splitlines() if ln.strip()]) == 1


# --------------------------------------------------------------------------- TC-61


@needs_user
def test_tc61_sandboxed_auto_defers_to_ck_run(env):
    """TC-61 (a)."""
    w = world(env)
    init_project(env, w.repo)
    rid, mp = open_run(w)
    add_agents(w, mp, rests(rid, ["implement task 1"]))
    backdate_manifest(mp, 7)
    sandboxed = {"CK_RUN_SANDBOXED": "1"}
    with unreadable(env.claude / "projects"):
        r = stats("collect", "--agents", rid, "--auto", env={**env, **sandboxed}, cwd=w.repo)
        orphan = stats("collect", "--agents", "ag_0a1b2c3d", "--auto", env={**env, **sandboxed}, cwd=w.repo)
    assert r.exit_code == 0 and orphan.exit_code == 0
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1 and "ck run collects" in lines[0], r.stdout
    olines = [ln for ln in orphan.stdout.splitlines() if ln.strip()]
    assert len(olines) == 1 and "ck stats collect --agents ag_0a1b2c3d" in olines[0], orphan.stdout
    assert doctor_open(env, w.repo) == 1


def test_tc61_ck_run_collects_only_its_own_session_runs(env, monkeypatch):
    """TC-61 (b): `auto_collect_after_run` in-process."""
    from claude_kiro.cli.run import auto_collect_after_run

    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.delenv("CK_RUN_SANDBOXED", raising=False)
    w = world(env)
    init_project(env, w.repo)
    other = w.home.session(slug="-work-other", cwd=w.repo)
    started = time.time()
    own, mp_own = open_run(w)
    free, mp_free = open_run(w)
    foreign, mp_foreign = open_run(w)
    stray = "ag_0badc0de"
    set_manifest(mp_own, sessionId=w.sess.session_id)
    set_manifest(mp_free, sessionId=None)
    set_manifest(mp_foreign, sessionId=other.session_id)
    add_agents(w, mp_own, rests(own, ["implement task 1"]))
    add_agents(w, mp_free, rests(free, ["implement task 2"]), offset_s=30)
    add_agents(w, mp_foreign, rests(foreign, ["implement task 3"]), sess=other)
    w.sess.tool_agents(rests(stray, ["implement task 4"]), start=iso_ms(start_ms(mp_own) + 60_000))
    soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    wf = w.sess.implement_run("wf_tc61", start=soon)
    lines = auto_collect_after_run(w.repo, w.sess.session_id, started)
    stats_lines = [ln for ln in lines if ln.startswith("stats:")]
    for rid in (own, free):
        assert len([ln for ln in stats_lines if rid in ln]) == 1, lines
        assert record_path(env, rid).is_file()
    for rid in (foreign, stray):
        assert not [ln for ln in lines if rid in ln], lines
        assert not record_path(env, rid).exists()
    assert record_path(env, wf.run_id).is_file()
    backdate_manifest(mp_foreign, 7)
    assert doctor_open(env, w.repo) == 1  # only the other session's manifest is still open
