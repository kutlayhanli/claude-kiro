"""`ck stats` / `ck stats show`: list, per-run breakdown, trends, filters and missing data (TC-35..TC-43).

Every record is produced by the real `ck stats collect` over a fake Claude home (tests/stats_helpers.py); the only
store file written by hand is TC-42's rewrite of a collected record. Prices used for the hand-computed values
(src/claude_kiro/bench.py, USD per 1M tokens): sonnet output 10, opus output 20, haiku output 0.5.
"""

import json
import re

import pytest

from stats_helpers import (
    AgentSpec, FakeClaudeHome, TaskSpec, fresh_env, git_project, implement_result, load_record, stats,
)

ID_A1, ID_A2, ID_B, ID_C = "wf_impl_a1", "wf_impl_a2", "wf_impl_b", "wf_create_c"


@pytest.fixture
def env():
    with fresh_env() as e:
        yield e


def ag(rid: str, label: str, model: str, out_tokens: int, ms: int, phase: str = "Wave 1") -> AgentSpec:
    """An agent that only emits output tokens, so its cost is out_tokens x the model's output price."""
    return AgentSpec(label, model=model, phase=phase, duration_ms=ms, requests=[(f"rq_{rid}_{label.replace(' ', '_')}", 0, out_tokens, 0, 0)])


def impl_agents(rid: str, sonnet_out: int, opus_out: int, ms: tuple) -> list:
    return [
        ag(rid, "task 1", "claude-sonnet-5-5", sonnet_out, ms[0]),
        ag(rid, "review 1", "claude-opus-5-5", opus_out, ms[1]),
        ag(rid, "land 1", "claude-haiku-5-5", 1_000_000, ms[2]),
    ]


def collect_all(env) -> None:
    out = stats("collect", "--all", env=env)
    assert out.exit_code == 0, out.output


def build_store(env, which=("a1", "a2", "b", "c")) -> dict:
    """Project A (two implement runs, Sept and Oct), project B (one Oct run), one create run in A; then `collect --all`."""
    home = FakeClaudeHome(env.claude)
    pa = git_project(env.root, "git@github.com:acme-corp/ledger-a.git", name="proj-a")
    pb = git_project(env.root, "git@github.com:orion/ledger-b.git", name="proj-b")
    sa = home.session("-proj-a", cwd=pa)
    sb = home.session("-proj-b", cwd=pb)
    if "a1" in which:
        sa.implement_run(ID_A1, start="2026-09-20T09:00:00Z", duration_ms=720_000,
                         agents=impl_agents(ID_A1, 1_000_000, 500_000, (600_000, 300_000, 100_000)))
    if "a2" in which:
        sa.implement_run(ID_A2, start="2026-10-05T09:00:00Z", duration_ms=1_200_000,
                         agents=impl_agents(ID_A2, 2_000_000, 500_000, (800_000, 100_000, 100_000)))
    if "b" in which:
        sb.implement_run(ID_B, start="2026-10-08T09:00:00Z", duration_ms=1_800_000,
                         agents=impl_agents(ID_B, 1_000_000, 1_000_000, (600_000, 300_000, 100_000)))
    if "c" in which:
        sa.create_run(ID_C, start="2026-10-12T08:00:00Z")
    collect_all(env)
    return {"home": home, "pa": pa, "pb": pb}


def show_json(env, *args: str) -> dict:
    out = stats("show", *args, "--json", env=env)
    assert out.exit_code == 0, out.output
    return json.loads(out.stdout)


def line_of(text: str, needle: str) -> str:
    lines = [l for l in text.splitlines() if needle in l]
    assert lines, f"no line mentions {needle!r}:\n{text}"
    return lines[0]


# --------------------------------------------------------------------------- TC-35


def test_tc35_list_shows_all_runs_newest_first_with_the_listed_columns(env):
    build_store(env, ("a1", "a2", "c"))
    plain = stats(env=env)
    show = stats("show", env=env)
    assert plain.exit_code == 0 and show.exit_code == 0, plain.output + show.output
    assert plain.stdout == show.stdout
    text = plain.stdout
    positions = [text.index(r) for r in (ID_C, ID_A2, ID_A1)]
    assert positions == sorted(positions), f"not newest first:\n{text}"
    a2 = line_of(text, ID_A2)
    assert "2026-10-05" in a2 and "spec-implement" in a2 and "complete" in a2
    assert re.search(r"\b20(\.0)?\b", a2), f"wall time (20 min) missing: {a2}"
    assert "30.50" in a2 and re.search(r"transcripts|priced", a2), f"cost with source missing: {a2}"
    assert re.search(r"\b2\s*(/|of)\s*2\b", a2), f"task counts missing: {a2}"
    assert "2026-09-20" in line_of(text, ID_A1)
    c = line_of(text, ID_C)
    assert "2026-10-12" in c and "spec-create" in c


def test_tc35_json_list_has_the_contract_fields(env):
    build_store(env, ("a1", "a2", "c"))
    data = show_json(env)
    runs = data["runs"]
    assert [r["runId"] for r in runs] == [ID_C, ID_A2, ID_A1]
    by_id = {r["runId"]: r for r in runs}
    assert by_id[ID_A2]["workflow"] == "spec-implement" and by_id[ID_A2]["outcome"] == "complete"
    assert by_id[ID_A2]["startTime"].startswith("2026-10-05")
    assert by_id[ID_A2]["wallMs"] == 1_200_000 and by_id[ID_A1]["wallMs"] == 720_000
    assert by_id[ID_A1]["cost"]["usd"] == pytest.approx(20.5) and by_id[ID_A2]["cost"]["usd"] == pytest.approx(30.5)
    assert all(r["cost"]["source"] == "priced_from_transcripts" for r in runs)
    assert by_id[ID_A2]["tasks"] == {"total": 2, "merged": 2, "failed": 0}
    assert by_id[ID_C]["workflow"] == "spec-create"


# --------------------------------------------------------------------------- TC-36

SLOW, COSTLY = "2", "3"


def breakdown_store(env) -> str:
    home = FakeClaudeHome(env.claude)
    proj = git_project(env.root, "git@github.com:acme-corp/ledger-a.git", name="proj-a")
    rid = "wf_breakdown"
    agents = [
        ag(rid, "task 1", "claude-sonnet-5-5", 100_000, 60_000),
        ag(rid, "task 1 retry 1", "claude-sonnet-5-5", 100_000, 60_000),
        ag(rid, "task 2", "claude-sonnet-5-5", 200_000, 300_000),
        ag(rid, "task 3", "claude-sonnet-5-5", 1_000_000, 120_000),
        ag(rid, "review 1", "claude-opus-5-5", 50_000, 30_000),
        ag(rid, "review 2", "claude-opus-5-5", 50_000, 30_000),
        ag(rid, "review 3", "claude-opus-5-5", 50_000, 30_000),
        ag(rid, "land 1", "claude-haiku-5-5", 1_000_000, 40_000),
    ]
    tasks = [
        TaskSpec(task="1", attempts=2, rounds=1, resolves=1, idle_minutes=1),
        TaskSpec(task="2", wave=1, escalations=[{"model": "opus"}], idle_minutes=2, fixes=1),
        TaskSpec(task="3", wave=1, idle_minutes=3),
    ]
    landings = [
        {"tasks": ["1", "2"], "size": 2, "window": 2, "merged": ["1", "2"], "red": [], "conflict": [], "deferred": [],
         "other": [], "gates": 1, "bisected": False, "minutes": 1.0},
        {"tasks": ["3"], "size": 1, "window": 2, "merged": ["3"], "red": ["3"], "conflict": [], "deferred": [],
         "other": [], "gates": 2, "bisected": True, "minutes": 2.0},
    ]
    home.session("-proj-a", cwd=proj).implement_run(rid, agents=agents, result=implement_result(tasks, landings=landings))
    collect_all(env)
    return rid


def test_tc36_breakdown_text_names_every_section(env):
    rid = breakdown_store(env)
    out = stats("show", rid, env=env)
    assert out.exit_code == 0, out.output
    low = out.stdout.lower()
    for word in ("implementer", "reviewer", "wave 1", "sonnet", "opus", "slowest", "idle", "landing"):
        assert word in low, f"{word!r} missing:\n{out.stdout}"
    assert re.search(r"costliest|most expensive", low)
    for word in ("retry", "escalation", "review round|review-round|reviewround", "conflict", "fix"):
        assert re.search(word, low), f"rate {word!r} missing:\n{out.stdout}"


def test_tc36_breakdown_json_has_the_known_numbers(env):
    rid = breakdown_store(env)
    d = show_json(env, rid)
    assert d["runId"] == rid
    assert d["byRole"]["implementer"]["usd"] == pytest.approx(14.0)
    assert d["byRole"]["reviewer"]["usd"] == pytest.approx(3.0)
    assert d["byRole"]["land"]["usd"] == pytest.approx(0.5)
    assert sum(c["usd"] for c in d["byModel"].values()) == pytest.approx(17.5)
    assert len(d["byModel"]) == 3
    assert d["byPhase"]["Wave 1"]["usd"] == pytest.approx(17.5)
    assert str(d["slowestTasks"][0]["task"]) == SLOW
    assert str(d["costliestTasks"][0]["task"]) == COSTLY
    rates = d["rates"]
    assert set(rates) >= {"retry", "escalation", "reviewRound", "conflict", "fix"}
    for key in ("retry", "escalation", "reviewRound", "conflict", "fix"):
        assert rates[key] == pytest.approx(1 / 3), key
    assert "180000" in json.dumps(d["idleBeforeMergeMs"])
    summary = d["landingSummary"]
    assert isinstance(summary, dict)
    values = list(summary.values())
    assert 2 in values and 1.5 in values, summary
    assert set(d["other"]) >= {"agents", "minutes", "usd"} and d["other"]["agents"] == 0


# --------------------------------------------------------------------------- TC-37 / TC-38


def trend_runs(d: dict, kind: str) -> dict:
    return {r["runId"]: r for r in d["trends"][kind]["runs"]}


def test_tc37_trends_carry_the_hand_computed_values(env):
    build_store(env)
    d = show_json(env)
    assert set(d["trends"]) == {"spec-implement"}
    runs = trend_runs(d, "spec-implement")
    assert set(runs) == {ID_A1, ID_A2, ID_B}
    # cost per merged task = total / 2 merged; wall minutes per task = wall minutes / 2 tasks
    assert runs[ID_A1]["costPerMergedTask"] == pytest.approx(20.5 / 2)
    assert runs[ID_A2]["costPerMergedTask"] == pytest.approx(30.5 / 2)
    assert runs[ID_B]["costPerMergedTask"] == pytest.approx(30.5 / 2)
    assert runs[ID_A1]["wallMinutesPerTask"] == pytest.approx(6.0)
    assert runs[ID_A2]["wallMinutesPerTask"] == pytest.approx(10.0)
    assert runs[ID_B]["wallMinutesPerTask"] == pytest.approx(15.0)
    cost = {ID_A1: (10, 10, 0.5, 20.5), ID_A2: (20, 10, 0.5, 30.5), ID_B: (10, 20, 0.5, 30.5)}
    time = {ID_A1: (0.6, 0.3, 0.1), ID_A2: (0.8, 0.1, 0.1), ID_B: (0.6, 0.3, 0.1)}
    for rid, (i, r, l, total) in cost.items():
        share = runs[rid]["roleShare"]
        assert share["implementer"]["cost"] == pytest.approx(i / total)
        assert share["reviewer"]["cost"] == pytest.approx(r / total)
        assert share["land"]["cost"] == pytest.approx(l / total)
        assert share["implementer"]["time"] == pytest.approx(time[rid][0])
        assert share["reviewer"]["time"] == pytest.approx(time[rid][1])
        assert share["land"]["time"] == pytest.approx(time[rid][2])


def test_tc37_text_prints_a_trend_block(env):
    build_store(env)
    out = stats("show", env=env)
    assert out.exit_code == 0, out.output
    assert "trend" in out.stdout.lower()


def test_tc37_kind_filter_excludes_the_other_kind(env):
    build_store(env)
    d = show_json(env, "--kind", "spec-create")
    assert [r["runId"] for r in d["runs"]] == [ID_C]
    assert d["trends"] == {}
    d = show_json(env, "--kind", "spec-implement")
    assert {r["runId"] for r in d["runs"]} == {ID_A1, ID_A2, ID_B}


def test_tc37_since_filter_excludes_september(env):
    build_store(env)
    d = show_json(env, "--since", "2026-10-01")
    assert {r["runId"] for r in d["runs"]} == {ID_A2, ID_B, ID_C}
    assert set(trend_runs(d, "spec-implement")) == {ID_A2, ID_B}


def test_tc37_project_filter_keeps_only_that_project(env):
    build_store(env)
    pa = load_record(env.xdg, ID_A1)["projectPseudonym"]
    pb = load_record(env.xdg, ID_B)["projectPseudonym"]
    assert pa and pb and pa != pb and load_record(env.xdg, ID_A2)["projectPseudonym"] == pa
    d = show_json(env, "--project", pa)
    assert {r["runId"] for r in d["runs"]} == {ID_A1, ID_A2, ID_C}
    assert set(trend_runs(d, "spec-implement")) == {ID_A1, ID_A2}
    d = show_json(env, "--project", pb)
    assert [r["runId"] for r in d["runs"]] == [ID_B]
    assert "spec-implement" not in d["trends"]


def test_tc38_one_run_per_kind_gives_no_trend(env):
    build_store(env, ("a2", "c"))
    d = show_json(env)
    assert len(d["runs"]) == 2 and d["trends"] == {}
    out = stats("show", env=env)
    assert out.exit_code == 0, out.output
    assert "trend" not in out.stdout.lower()


# --------------------------------------------------------------------------- TC-39


def test_tc39_json_matches_the_text_list(env):
    build_store(env, ("a1", "a2", "c"))
    text = stats("show", env=env).stdout
    for r in show_json(env)["runs"]:
        line = line_of(text, r["runId"])
        assert f"{r['cost']['usd']:.2f}" in line, line


def test_tc39_json_matches_the_text_breakdown(env):
    rid = breakdown_store(env)
    text = stats("show", rid, env=env).stdout
    d = show_json(env, rid)
    assert rid in text
    for role, cell in d["byRole"].items():
        amount = f"{cell['usd']:.2f}"
        assert any(role in l and amount in l for l in text.splitlines()), f"{role} {amount} not on one line:\n{text}"


# --------------------------------------------------------------------------- TC-40 / TC-41


def test_tc40_empty_store_suggests_collect_all(env):
    for args in ((), ("show",)):
        out = stats(*args, env=env)
        assert out.exit_code == 0, out.output
        low = out.output.lower()
        assert re.search(r"no runs|no collected|no records", low), out.output
        assert "ck stats collect --all" in out.output


def test_tc41_unknown_run_id_is_an_error(env):
    build_store(env, ("a2",))
    out = stats("show", "wf_missing", env=env)
    assert out.exit_code != 0
    assert "no local record" in out.output.lower()


# --------------------------------------------------------------------------- TC-42


def never_zero(value, where: str) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            never_zero(v, f"{where}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            never_zero(v, f"{where}[{i}]")
    else:
        assert not (isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0), f"{where} is 0"


def test_tc42_fields_added_later_read_unknown_not_zero(env):
    home = FakeClaudeHome(env.claude)
    proj = git_project(env.root, "git@github.com:acme-corp/ledger-a.git", name="proj-a")
    home.session("-proj-a", cwd=proj).implement_run("wf_old", agents=impl_agents("wf_old", 1_000_000, 500_000, (60_000, 30_000, 10_000)))
    collect_all(env)
    path = env.xdg / "claude-kiro" / "runs" / "wf_old.json"
    rec = json.loads(path.read_text())
    for key in ("claudeCodeVersion", "crossTests"):
        rec.pop(key, None)
    for t in rec.get("tasks", []):
        for key in ("resolves", "resolvesInferred", "idleBeforeMergeMs"):
            t.pop(key, None)
    path.write_text(json.dumps(rec))

    listing = stats(env=env)
    assert listing.exit_code == 0, listing.output
    assert "wf_old" in listing.stdout
    text = stats("show", "wf_old", env=env)
    assert text.exit_code == 0, text.output
    assert "unknown" in text.stdout.lower()
    assert "unknown" in line_of(text.stdout.lower(), "idle")
    d = show_json(env, "wf_old")
    assert d.get("claudeCodeVersion") is None
    assert d.get("crossTests") is None
    assert d.get("idleBeforeMergeMs") is None or not isinstance(d["idleBeforeMergeMs"], (int, float)) or d["idleBeforeMergeMs"] != 0
    never_zero(d.get("idleBeforeMergeMs"), "idleBeforeMergeMs")
    for t in d.get("tasks", []):
        assert t.get("resolves") is None and t.get("idleBeforeMergeMs") is None
    assert show_json(env)["runs"][0]["runId"] == "wf_old"


def test_tc42_partial_run_prints_unknown_and_null_never_zero(env):
    home = FakeClaudeHome(env.claude)
    proj = git_project(env.root, "git@github.com:acme-corp/ledger-a.git", name="proj-a")
    rid = "wf_partial"
    run = home.session("-proj-a", cwd=proj).implement_run(rid, agents=impl_agents(rid, 1_000_000, 500_000, (60_000, 30_000, 10_000)))
    run.drop_transcript(0)
    run.truncate_transcript(1)
    run.drop_meta(2)
    collect_all(env)
    gone = run.agent_ids[0]

    rec = json.loads((env.xdg / "claude-kiro" / "runs" / f"{rid}.json").read_text())
    assert {m["agentId"] for m in rec["missing"]} >= set(run.agent_ids)

    text = stats("show", rid, env=env)
    assert text.exit_code == 0, text.output
    assert "unknown" in text.stdout.lower()
    d = show_json(env, rid)
    dropped = [a for a in d["agents"] if a["agentId"] == gone]
    assert dropped and dropped[0]["usd"] is None
    assert dropped[0]["input"] is None and dropped[0]["output"] is None
    listed = show_json(env)["runs"][0]
    assert listed["cost"]["source"] == "estimate"
    assert "estimate" in stats("show", env=env).stdout.lower()


# --------------------------------------------------------------------------- TC-43


def test_tc43_durations_state_what_they_cover_and_exclude(env):
    rid = breakdown_store(env)
    out = stats("show", rid, env=env)
    assert out.exit_code == 0, out.output
    wall = line_of(out.stdout.lower(), "wall")
    assert "plan" in wall and "final gate" in wall, wall
    assert re.search(r"exclud|includ", wall), wall
    low = out.stdout.lower()
    assert "overlap" in low and "wait" in low, "role/phase duration basis missing"
    assert re.search(r"exclud", low)
    assert "merge" in line_of(low, "idle")
