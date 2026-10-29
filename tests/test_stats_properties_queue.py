"""Properties of the queue-wait seams and of `ck stats show` over runs without queue data (P-13..P-16).

P-13..P-15 call the pure seams `collect.queue_ms`, `collect.wait_split` and `show.queue_summary` / `show.agent_kind`
directly (imported inside the test bodies). P-14's JSON half and P-16 are table-driven over real subprocess runs on a
FakeClaudeHome with an isolated store.
"""

import json
import math
import random
import statistics
from typing import Any, List

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from stats_helpers import AgentSpec, FakeClaudeHome, TaskSpec, fresh_env, git_project, implement_result, stats, to_ms

PROPERTIES = {
    "P-13": "test_p13_queue_ms_is_start_minus_queue_or_none",
    "P-14": "test_p14_queue_summary_matches_the_oracle",
    "P-15": "test_p15_wait_split_conserves_the_wait",
    "P-16": "test_p16_stripping_queued_at_changes_only_the_queue_sections",
}

PROP = dict(derandomize=True, max_examples=200, database=None, deadline=None, suppress_health_check=list(HealthCheck))

# --------------------------------------------------------------------------- P-13

TWO53 = 2 ** 53
SPECIAL: List[Any] = [None, "1730000000000", "x", "", True, False, float("nan"), float("inf"), float("-inf"),
                      -1, -5.5, 0, 0.0, 1, TWO53, TWO53 - 1, 1730000000000, 1730000000000.0, [], {}]
VALUES = st.one_of(st.sampled_from(SPECIAL), st.integers(-10, 10 ** 13), st.integers(0, TWO53),
                   st.floats(min_value=-(2.0 ** 60), max_value=2.0 ** 60))


def is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


@settings(**PROP)
@given(VALUES, VALUES)
def test_p13_queue_ms_is_start_minus_queue_or_none(queued, started):
    from claude_kiro.analytics.collect import queue_ms
    got = queue_ms(queued, started)
    if is_num(queued) and is_num(started) and started >= queued:
        assert isinstance(got, int) and not isinstance(got, bool) and got >= 0
        assert abs(got - (started - queued)) <= 1
        if queued == started:
            assert got == 0
    else:
        assert got is None


@settings(**PROP)
@given(st.integers(0, TWO53 - 1))
def test_p13_boundaries_equal_and_one_before(queued):
    from claude_kiro.analytics.collect import queue_ms
    assert queue_ms(queued, queued) == 0
    assert queue_ms(queued, queued + 1) == 1
    if queued > 0:
        assert queue_ms(queued, queued - 1) is None


# --------------------------------------------------------------------------- P-14

KINDS = {  # kind -> (role, round) pairs `agent_kind` must map onto it
    "task": [("implementer", None), ("test-writer", None)],
    "revise": [("implementer", 1), ("test-writer", 2)],
    "review": [("reviewer", 1), ("reviewer", None)],
    "plan": [("plan", None)],
    "land": [("land", None)],
    "gate": [("gate", None)],
    "resolve": [("resolver", 1)],
    "fix": [("fixer", None)],
    "other": [("other", None)],
    "cross-test": [("cross-test", None)],
    "report": [("report", None)],
}
WAITS = st.one_of(st.none(), st.just("x"), st.integers(0, 10 ** 7))


@st.composite
def queue_rows(draw):
    rows = []
    for kind in draw(st.lists(st.sampled_from(sorted(KINDS)), min_size=0, max_size=45)):
        role, rnd = draw(st.sampled_from(KINDS[kind]))
        rows.append((kind, {"role": role, "round": rnd, "queueMs": draw(WAITS)}))
    return rows


def oracle_summary(rows):
    by_kind = {}
    for kind, row in rows:
        v = row["queueMs"]
        if isinstance(v, int) and not isinstance(v, bool):
            by_kind.setdefault(kind, []).append(v)
    if not by_kind:
        return None
    out = {}
    for kind, vals in by_kind.items():
        s = sorted(vals)
        out[kind] = {"count": len(s), "total": sum(s), "median": statistics.median(s), "p90": s[math.ceil(0.9 * len(s)) - 1]}
    return out


@settings(**PROP)
@given(queue_rows())
def test_p14_queue_summary_matches_the_oracle(rows):
    from claude_kiro.analytics.show import agent_kind, queue_summary
    for kind, row in rows:
        assert agent_kind(row) == kind
    got = queue_summary([r for _, r in rows])
    expected = oracle_summary(rows)
    if expected is None:
        assert got is None
        return
    assert set(got) == set(expected)
    for kind, exp in expected.items():
        g = got[kind]
        assert g["count"] == exp["count"] and g["totalMs"] == exp["total"] and g["p90Ms"] == exp["p90"]
        assert isinstance(g["medianMs"], int) and abs(g["medianMs"] - exp["median"]) <= 0.5


LABEL_POOL = ["task 1", "task 2", "task 3 revise 1", "review 1", "review 2 round 2", "plan 1", "land 1", "full gate @2",
              "resolve 1", "fix 1 (Task 1 red on landing)", "report", "cross-test wave 1"]


def test_p14_json_equals_the_summary_on_20_collected_runs():
    from claude_kiro.analytics.show import queue_summary
    with fresh_env() as env:
        home = FakeClaudeHome(env.claude)
        proj = git_project(env.root, "git@github.com:acme-corp/ledger-a.git", name="proj")
        session = home.session("-proj", cwd=proj)
        base = to_ms("2026-10-12T09:00:00Z")
        ids = []
        for n in range(20):
            rng = random.Random(1000 + n)
            agents = []
            for i in range(rng.randint(1, 12)):
                started = base + i * 1000
                spec = AgentSpec(rng.choice(LABEL_POOL), started_at_ms=started, duration_ms=5_000)
                if rng.random() < 0.8:
                    spec.queued_at_ms = started - rng.choice([0, 0, 1_000, 7_000, 60_000, 123_456])
                agents.append(spec)
            rid = f"wf_p14_{n:02d}"
            ids.append(rid)
            session.implement_run(rid, start=f"2026-10-{1 + n:02d}T09:00:00Z", agents=agents)
        out = stats("collect", "--all", env=env)
        assert out.exit_code == 0, out.output
        for rid in ids:
            rec = json.loads((env.xdg / "claude-kiro" / "runs" / f"{rid}.json").read_text())
            summary = queue_summary(rec["agents"])
            shown = stats("show", rid, "--json", env=env)
            assert shown.exit_code == 0, shown.output
            data = json.loads(shown.stdout)
            if summary is None:
                assert data["queueWait"] == {"recorded": False, "byKind": {}}
            else:
                assert data["queueWait"] == {"recorded": True, "byKind": summary}


# --------------------------------------------------------------------------- P-15

TIME = st.integers(0, 10 ** 7)


@st.composite
def split_case(draw):
    f = draw(TIME)
    m = f + draw(st.one_of(st.just(0), st.integers(0, 10 ** 6), st.integers(0, 10 ** 7 - f if f < 10 ** 7 else 0)))
    ivs = []
    for _ in range(draw(st.integers(0, 6))):
        shape = draw(st.sampled_from(["inside", "straddle_f", "straddle_m", "outside_before", "outside_after", "zero", "wide", "any"]))
        span = max(1, m - f)
        if shape == "inside":
            a = f + draw(st.integers(0, span)); b = a + draw(st.integers(0, max(0, m - a)))
        elif shape == "straddle_f":
            a = max(0, f - draw(st.integers(1, 1000))); b = f + draw(st.integers(0, span))
        elif shape == "straddle_m":
            a = f + draw(st.integers(0, span)); b = m + draw(st.integers(1, 1000))
        elif shape == "outside_before":
            b = max(0, f - draw(st.integers(0, 1000))); a = max(0, b - draw(st.integers(0, 1000)))
        elif shape == "outside_after":
            a = m + draw(st.integers(0, 1000)); b = a + draw(st.integers(0, 1000))
        elif shape == "zero":
            a = b = draw(st.integers(0, 10 ** 7))
        elif shape == "wide":
            a = draw(st.integers(0, f)); b = m + draw(st.integers(0, 1000))
        else:
            a = draw(TIME); b = a + draw(st.integers(0, 10 ** 6))
        ivs.append((a, b))
    if ivs and draw(st.booleans()):
        ivs.append(draw(st.sampled_from(ivs)))  # identical
    if ivs and draw(st.booleans()):
        a, b = draw(st.sampled_from(ivs))
        ivs.append((a + (b - a) // 4, b - (b - a) // 4))  # nested
    return f, m, ivs


def oracle_union(f, m, ivs):
    clipped = sorted((max(a, f), min(b, m)) for a, b in ivs if min(b, m) > max(a, f))
    total, cur_s, cur_e = 0, None, None
    for a, b in clipped:
        if cur_e is None or a > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = a, b
        else:
            cur_e = max(cur_e, b)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


@settings(**PROP)
@given(split_case(), st.tuples(TIME, st.integers(0, 10 ** 6)))
def test_p15_wait_split_conserves_the_wait(case, extra):
    from claude_kiro.analytics.collect import wait_split
    f, m, ivs = case
    review, landing = wait_split(f, m, ivs)
    assert review + landing == m - f
    assert 0 <= review <= m - f
    assert review == oracle_union(f, m, ivs)
    # metamorphic: adding an interval never lowers review; order and duplicates do not matter
    a, length = extra
    more_review, more_landing = wait_split(f, m, ivs + [(a, a + length)])
    assert more_review >= review and more_review + more_landing == m - f
    assert wait_split(f, m, list(reversed(ivs)) + ivs) == (review, landing)


@settings(**PROP)
@given(TIME, TIME, st.lists(st.tuples(TIME, TIME), max_size=3))
def test_p15_unknown_or_inverted_gives_none(f, m, ivs):
    from claude_kiro.analytics.collect import wait_split
    ivs = [(min(a, b), max(a, b)) for a, b in ivs]
    assert wait_split(None, m, ivs) == (None, None)
    assert wait_split(f, None, ivs) == (None, None)
    assert wait_split(None, None, ivs) == (None, None)
    if m < f:
        assert wait_split(f, m, ivs) == (None, None)
    assert wait_split(f, f, ivs) == (0, 0)
    assert wait_split(f, m, ivs + [(f, None)]) == (None, None)


# --------------------------------------------------------------------------- P-16

F = to_ms("2026-10-12T09:05:00Z")


def _agents(labels, queued=True):
    out = []
    for i, label in enumerate(labels):
        started = F - 300_000 + i * 20_000
        out.append(AgentSpec(label, started_at_ms=started, duration_ms=15_000, queued_at_ms=started - (i + 1) * 1000 if queued else None))
    return out


SCENARIOS = {
    "with_reviews": dict(agents=["task 1", "review 1", "task 2", "review 2", "land 1"]),
    "without_reviews": dict(agents=["task 1", "task 2", "land 1"]),
    "failed_task": dict(agents=["task 1", "review 1", "task 2"], tasks=[TaskSpec(task="1"), TaskSpec(task="2", ok=False, reason="gate not passed")]),
    "revise": dict(agents=["task 1", "review 1", "task 1 revise 1", "review 1 round 2", "land 1"]),
    "red_landing": dict(agents=["task 1", "review 1", "land 1", "fix 1 (Task 1 red on landing)", "re-check 1"],
                        landings=[{"tasks": ["1"], "size": 1, "window": 2, "merged": [], "red": ["1"], "conflict": [], "deferred": [],
                                   "other": [], "gates": 1, "bisected": False, "minutes": 1.0}]),
    "unfinished": dict(agents=["task 1", "review 1"], run=dict(status="completed", result=None)),
    "no_meta": dict(agents=["task 1", "review 1", "land 1"], meta=False),
    "two_review_rounds": dict(agents=["task 1", "review 1", "review 1 round 2", "resolve 1", "land 1"]),
}


def _build(env, name, queued):
    sc = SCENARIOS[name]
    home = FakeClaudeHome(env.claude)
    proj = git_project(env.root, "git@github.com:acme-corp/ledger-a.git", name="proj")
    agents = _agents(sc["agents"], queued)
    if sc.get("meta") is False:
        for a in agents:
            a.meta = False
    kw = dict(sc.get("run", {}))
    if "result" not in kw:
        kw["result"] = implement_result(sc.get("tasks", [TaskSpec(task="1"), TaskSpec(task="2", wave=2)]), landings=sc.get("landings"))
    return home.session("-proj", cwd=proj).implement_run("wf_p16", agents=agents, **kw)


def _show(env):
    out = stats("show", "wf_p16", "--json", env=env)
    assert out.exit_code == 0, out.output
    text = stats("show", "wf_p16", env=env)
    assert text.exit_code == 0, text.output
    return json.loads(out.stdout), text.stdout


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_p16_stripping_queued_at_changes_only_the_queue_sections(name):
    with fresh_env() as env:
        run = _build(env, name, queued=True)
        assert stats("collect", "--all", env=env).exit_code == 0
        with_q, _ = _show(env)
        d = json.loads(run.path.read_text())
        for entry in d["workflowProgress"]:
            entry.pop("queuedAt", None)
        run.path.write_text(json.dumps(d))
        (env.xdg / "claude-kiro" / "runs" / "wf_p16.json").unlink()
        assert stats("collect", "--all", env=env).exit_code == 0
        without, text = _show(env)
    assert set(without) == set(with_q)
    for key in with_q:
        if key not in ("queueWait", "waitSplit"):
            assert without[key] == with_q[key], key
    assert without["queueWait"] == {"recorded": False, "byKind": {}}
    assert "queue wait (runtime slot): not recorded" in text
