"""`ck stats`: queue wait per agent kind and the review/landing split of the wait before merge (TC-31..TC-37, TC-59).

Everything goes through the real `ck stats collect` / `ck stats show` subprocesses over a FakeClaudeHome with an isolated
`XDG_DATA_HOME`. The only direct calls into `claude_kiro` are TC-35 and TC-59 comparing the record with the pure seams
`collect.queue_ms` and `collect.wait_split` (imported inside the test body); nothing is mocked.
"""

import json
import re
import shutil
from pathlib import Path

import pytest

from stats_helpers import (
    AgentSpec, FakeClaudeHome, TaskSpec, fresh_env, git_project, implement_result, load_record, stats, to_ms,
)

FIXTURE = Path(__file__).parent / "fixtures" / "stats" / "pre-queue-record.json"

RID = "wf_queue"
F1 = to_ms("2026-10-12T09:05:00Z")  # task 1 finishes here; it merges 300 s later
T20 = to_ms("2026-10-12T09:20:00Z")
REVIEW_WAITS_S = [10, 20, 30, 40, 50, 70, 80, 90, 100, 200]  # plus review 1 (60 s): 11 waits, total 750 s


@pytest.fixture
def env():
    with fresh_env() as e:
        yield e


def ag(label: str, started: int, wait_ms: int, queued: bool = True, dur: int = 10_000, **kw) -> AgentSpec:
    return AgentSpec(label, started_at_ms=started, duration_ms=dur, queued_at_ms=started - wait_ms if queued else None, **kw)


def queue_agents(queued: bool = True) -> list:
    """Agents of every kind. Review waits (s) are 10..100 and 200 (11 of them, 60 s median, 100 s p90, 750 s total)."""
    out = [
        ag("task 1", F1 - 300_000, 0, queued, dur=300_000),
        ag("task 2", F1 + 300_000, 5_000, queued, dur=300_000),
        # review 1: queued at task 1's finish, started 60 s later, ended 120 s after that (180 s of waiting for task 1)
        ag("review 1", F1 + 60_000, 60_000, queued, dur=120_000, model="claude-opus-5-5"),
    ]
    for i, w in enumerate(REVIEW_WAITS_S):  # reviews of tasks that are not in the report: they do not enter the split
        out.append(ag(f"review {3 + i}", T20 + w * 1000, w * 1000, queued, model="claude-opus-5-5"))
    out += [
        ag("task 9 revise 1", T20 + 300_000, 15_000, queued),
        ag("task 9 revise 2", T20 + 310_000, 25_000, queued),
        ag("plan 1", T20 + 320_000, 3_000, queued, model="claude-haiku-5-5"),
        ag("plan 2", T20 + 330_000, 7_000, queued, model="claude-haiku-5-5"),
        ag("land 1", T20 + 340_000, 120_000, queued, model="claude-haiku-5-5"),
        ag("full gate @2", T20 + 350_000, 1_000, queued, model="claude-haiku-5-5"),
    ]
    return out


def two_tasks() -> list:
    # task 1: 09:00 -> finished 09:05 -> merged 09:10 (300 s); task 2: finished 09:15 -> merged 09:17 (120 s)
    return [TaskSpec(task="1", idle_minutes=5.0), TaskSpec(task="2", wave=2, idle_minutes=2.0)]


def build(env, agents=None, tasks=None, rid=RID, **run_kw):
    home = FakeClaudeHome(env.claude)
    proj = git_project(env.root, "git@github.com:acme-corp/ledger-a.git", name="proj")
    s = home.session("-proj", cwd=proj)
    run = s.implement_run(rid, result=implement_result(tasks if tasks is not None else two_tasks()),
                          agents=agents if agents is not None else queue_agents(), **run_kw)
    return run


def collect(env, rid=RID) -> None:
    out = stats("collect", "--run", rid, env=env)
    assert out.exit_code == 0, out.output


def show(env, rid=RID) -> str:
    out = stats("show", rid, env=env)
    assert out.exit_code == 0, out.output
    return out.stdout


def show_json(env, rid=RID) -> dict:
    out = stats("show", rid, "--json", env=env)
    assert out.exit_code == 0, out.output
    return json.loads(out.stdout)


def rows_by_label(env, rid=RID) -> dict:
    return {a["label"]: a for a in load_record(env.xdg, rid)["agents"]}


def queue_section(text: str) -> dict:
    """{kind: (count, median_s, p90_s, total_min)} parsed from the `queue wait (runtime slot):` section."""
    lines = text.splitlines()
    idx = [i for i, l in enumerate(lines) if l.startswith("queue wait (runtime slot):")]
    assert idx, f"no queue wait section:\n{text}"
    out = {}
    for line in lines[idx[0] + 1:]:
        if not line.startswith("  "):
            break
        m = re.match(r"\s+([\w-]+): (\d+) agents?, median ([\d.]+) s, p90 ([\d.]+) s, total ([\d.]+) min", line)
        assert m, f"unparseable queue line {line!r}"
        out[m.group(1)] = (int(m.group(2)), float(m.group(3)), float(m.group(4)), float(m.group(5)))
    return out


SPLIT_RX = re.compile(r"wait before merge split: review median ([\d.]+) min, total ([\d.]+) min; "
                      r"landing median ([\d.]+) min, total ([\d.]+) min")


def split_line(text: str) -> tuple:
    m = SPLIT_RX.search(text)
    assert m, f"no wait before merge split line:\n{text}"
    return tuple(float(g) for g in m.groups())


# --------------------------------------------------------------------------- TC-31


def test_tc31_queue_ms_is_started_minus_queued_and_zero_when_equal(env):
    build(env, agents=[
        ag("review 1", F1 + 77_000, 77_000, model="claude-opus-5-5"),
        ag("task 1", F1, 0),
    ])
    collect(env)
    rows = rows_by_label(env)
    assert rows["review 1"]["queueMs"] == 77_000
    assert rows["review 1"]["queuedAtMs"] == F1
    assert rows["task 1"]["queueMs"] == 0
    assert rows["task 1"]["queuedAtMs"] == F1


# --------------------------------------------------------------------------- TC-32


def hostile_agents(hostile: bool) -> list:
    base = F1
    out = [
        ag("review 1", base + 77_000, 77_000, model="claude-opus-5-5"),
        ag("task 6", base + 5_000, 0),
        ag("task 1", base + 10_000, 0, queued=False),  # queuedAt missing
        AgentSpec("task 2", started_at_ms=base + 20_000, queued_at_ms=base if hostile else None, omit_started=hostile),
        AgentSpec("task 3", started_at_ms=base + 30_000, queued_at_ms=base + 40_000 if hostile else None),  # start < queued
        AgentSpec("task 4", started_at_ms=base + 40_000, raw_progress={"queuedAt": None} if hostile else None),
        AgentSpec("task 5", started_at_ms=base + 50_000, raw_progress={"queuedAt": "x"} if hostile else None),
    ]
    return out


def test_tc32_unknown_queue_times_are_null_and_nothing_else_changes(env):
    build(env, agents=hostile_agents(True))
    collect(env)
    rows = rows_by_label(env)
    assert rows["review 1"]["queueMs"] == 77_000 and rows["task 6"]["queueMs"] == 0
    for label in ("task 1", "task 2", "task 3", "task 4", "task 5"):
        assert rows[label]["queueMs"] is None, f"{label}: {rows[label]}"
        assert "queueMs" in rows[label]
    with fresh_env() as base_env:
        build(base_env, agents=hostile_agents(False))
        collect(base_env)
        base = rows_by_label(base_env)
    for label, row in rows.items():
        assert row["ms"] == base[label]["ms"] and row["usd"] == base[label]["usd"], label


# --------------------------------------------------------------------------- TC-33


def test_tc33_queue_wait_section_lists_every_kind_present(env):
    build(env)
    collect(env)
    section = queue_section(show(env))
    assert set(section) >= {"task", "review", "revise", "plan", "land", "gate"}
    count, median_s, p90_s, total_min = section["review"]
    assert count == 11 and median_s == pytest.approx(60) and p90_s == pytest.approx(100)
    assert total_min * 60 == pytest.approx(750, abs=3)
    assert section["task"][0] == 2 and section["revise"][0] == 2 and section["plan"][0] == 2
    assert section["land"][0] == 1 and section["gate"][0] == 1
    assert section["land"][1] == pytest.approx(120)


# --------------------------------------------------------------------------- TC-34


def test_tc34_wait_before_merge_splits_into_review_and_landing(env):
    build(env)
    collect(env)
    tasks = {str(t["task"]): t for t in load_record(env.xdg, RID)["tasks"]}
    assert (tasks["1"]["reviewWaitMs"], tasks["1"]["landingWaitMs"]) == (180_000, 120_000)
    assert (tasks["2"]["reviewWaitMs"], tasks["2"]["landingWaitMs"]) == (0, 120_000)
    text = show(env)
    r_med, r_tot, l_med, l_tot = split_line(text)
    assert (r_med, r_tot, l_med, l_tot) == (pytest.approx(1.5), pytest.approx(3.0), pytest.approx(2.0), pytest.approx(4.0))
    data = show_json(env)
    assert data["idleBeforeMergeMs"] == {"total": 420_000, "median": 210_000, "max": 300_000}
    lines = text.splitlines()
    idle_at = next(i for i, l in enumerate(lines) if l.startswith("idle before merge:"))
    split_at = next(i for i, l in enumerate(lines) if l.startswith("wait before merge split:"))
    assert abs(idle_at - split_at) <= 2, "the split line sits next to idle before merge"


# --------------------------------------------------------------------------- TC-35


def strip_queued(run) -> None:
    d = json.loads(run.path.read_text())
    for entry in d["workflowProgress"]:
        entry.pop("queuedAt", None)
    run.path.write_text(json.dumps(d))


def test_tc35_no_queue_data_reads_not_recorded_and_changes_nothing_else(env):
    run = build(env)
    collect(env)
    with_q = show_json(env)
    assert with_q["queueWait"]["recorded"] is True
    strip_queued(run)
    (env.xdg / "claude-kiro" / "runs" / f"{RID}.json").unlink()
    collect(env)
    text = show(env)
    assert "queue wait (runtime slot): not recorded" in text
    without = show_json(env)
    assert without["queueWait"]["recorded"] is False
    assert without["waitSplit"]["recorded"] is True
    assert set(without) == set(with_q)
    for key in with_q:
        if key not in ("queueWait", "waitSplit"):
            assert without[key] == with_q[key], key
    from claude_kiro.analytics import collect as collector
    # review intervals now start at startedAt: review 1 is (F1+60 s, F1+180 s)
    review1 = (F1 + 60_000, F1 + 180_000)
    r1 = collector.wait_split(F1, F1 + 300_000, [review1])
    r2 = collector.wait_split(F1 + 600_000, F1 + 720_000, [])
    assert r1 == (120_000, 180_000) and r2 == (0, 120_000)
    split = without["waitSplit"]
    assert split["review"]["totalMs"] == r1[0] + r2[0]
    assert split["landing"]["totalMs"] == r1[1] + r2[1]
    assert split["review"]["medianMs"] == (r1[0] + r2[0]) / 2
    assert split["landing"]["medianMs"] == (r1[1] + r2[1]) / 2


# --------------------------------------------------------------------------- TC-36


def test_tc36_json_carries_the_same_numbers_as_the_text(env):
    build(env)
    collect(env)
    data = show_json(env)
    assert data["queueWait"]["recorded"] is True
    assert data["queueWait"]["byKind"]["review"] == {"count": 11, "medianMs": 60_000, "p90Ms": 100_000, "totalMs": 750_000}
    split = data["waitSplit"]
    assert split["recorded"] is True
    assert split["review"] == {"medianMs": 90_000, "totalMs": 180_000}
    assert split["landing"] == {"medianMs": 120_000, "totalMs": 240_000}
    text = show(env)
    section = queue_section(text)
    assert set(section) == set(data["queueWait"]["byKind"])
    for kind, (count, median_s, p90_s, total_min) in section.items():
        j = data["queueWait"]["byKind"][kind]
        assert count == j["count"]
        assert median_s * 1000 == pytest.approx(j["medianMs"], abs=500)
        assert p90_s * 1000 == pytest.approx(j["p90Ms"], abs=500)
        assert total_min * 60_000 == pytest.approx(j["totalMs"], abs=3_000)
    r_med, r_tot, l_med, l_tot = split_line(text)
    assert r_med * 60_000 == pytest.approx(split["review"]["medianMs"], abs=3_000)
    assert r_tot * 60_000 == pytest.approx(split["review"]["totalMs"], abs=3_000)
    assert l_med * 60_000 == pytest.approx(split["landing"]["medianMs"], abs=3_000)
    assert l_tot * 60_000 == pytest.approx(split["landing"]["totalMs"], abs=3_000)


# --------------------------------------------------------------------------- TC-37


def test_tc37_a_record_collected_before_the_change_still_shows(env):
    fixture = json.loads(FIXTURE.read_text())
    assert not any("queueMs" in a for a in fixture["agents"]), "the fixture must come from the pre-change collector"
    runs = env.xdg / "claude-kiro" / "runs"
    runs.mkdir(parents=True)
    shutil.copy(FIXTURE, runs / f"{fixture['runId']}.json")
    text = show(env, fixture["runId"])
    assert "idle before merge:" in text and "landings:" in text
    assert "queue wait (runtime slot): not recorded" in text
    assert "wait before merge split: not recorded" in text
    data = show_json(env, fixture["runId"])
    idle_total = sum(t["idleBeforeMergeMs"] for t in fixture["tasks"])
    assert data["idleBeforeMergeMs"]["total"] == idle_total
    assert data["queueWait"] == {"recorded": False, "byKind": {}}
    assert data["waitSplit"] == {"recorded": False, "review": None, "landing": None}
    listing = stats("show", env=env)
    assert listing.exit_code == 0 and fixture["runId"] in listing.stdout


# --------------------------------------------------------------------------- TC-59


def test_tc59_record_values_equal_the_collector_seams(env):
    run = build(env)
    collect(env)
    from claude_kiro.analytics import collect as collector
    entries = {e["agentId"]: e for e in json.loads(run.path.read_text())["workflowProgress"] if e.get("type") == "workflow_agent"}
    rec = load_record(env.xdg, RID)
    assert len(rec["agents"]) == len(entries)
    for row in rec["agents"]:
        e = entries[row["agentId"]]
        assert row["queueMs"] == collector.queue_ms(e.get("queuedAt"), e.get("startedAt")), row["label"]
    result = json.loads(run.path.read_text())["result"]
    for t in result["tasks"]:
        key = str(t["task"])
        ivs = []
        for row in rec["agents"]:
            if row["task"] is not None and str(row["task"]) == key and row["role"] in ("reviewer", "implementer", "test-writer") \
                    and (row["role"] == "reviewer" or row["round"] is not None):
                start = row["queuedAtMs"] if row["queuedAtMs"] is not None else row["startedAtMs"]
                ivs.append((start, row["startedAtMs"] + row["ms"]))
        expected = collector.wait_split(to_ms(t["finishedAt"]), to_ms(t["mergedAt"]), ivs)
        got = next(x for x in rec["tasks"] if str(x["task"]) == key)
        assert (got["reviewWaitMs"], got["landingWaitMs"]) == expected, key
