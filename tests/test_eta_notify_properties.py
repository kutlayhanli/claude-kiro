"""P-16, P-17, P-18: `claude_kiro.eta.decide.notify_decision` and `notice_text` over generated timelines.

The seams are called directly (no server, no CLI). Holds come from the real `classify_hold` over small graphs and run
state from the real `progress.fold`, so a timeline mixes real park, failure and cycle holds. Project modules are
imported inside test bodies so the file always collects.

Input contract these tests rely on (design S6/S7/S8; the exact keyword names are this file's proposal, spelled once in
`view()` and `sent_of()`, so a rename is a one-line change):
- `decide.NotifyInput(spec, state, p50_finish_min, waited_min, launch_p90_min, hold, cause, outcome, merged, total,
  wall_min, usd, now_hhmm)`: `state` is a `decide_state` value, `outcome` the end outcome (None until the run ends).
- `decide.SentNotice(kind, key, level)`, built from a `Notice`'s own `kind`, `key` and `level`.
- A returned `Notice` has `kind` ("slip", "blocked" or "done"), `level` (the slip level, else None), `key` and `type`.
- A park hold is told apart from a failure or cycle hold by `Hold.waiting_on_you` (non-empty only for a park).
"""

import dataclasses
import re
from typing import Any, Dict, List, Optional, Tuple

from hypothesis import HealthCheck, given, settings, strategies as st

import eta_strategies as es

pytestmark = es.NO_NETWORK

PROPERTIES = {
    "P-16": "test_slip_notices_fire_exactly_at_new_levels",
    "P-17": "test_notification_dedupe_and_suppression",
    "P-18": "test_messages_hold_no_paths_tests_or_requirement_text",
}

SETTINGS = settings(derandomize=True, max_examples=200, database=None, deadline=None,
                    suppress_health_check=list(HealthCheck))

SPEC_NAMES = ("demo", "run-followups", "x1", "ledger-sync")
OUTCOMES = ("completed", "superseded", "partial", "halted", "failed", "refused")
SCENARIOS = ("none", "park-whole", "park-partial", "fail", "cycle")
# Seeded strings that must never reach a message (8.10).
DIRTY = ("/a/b.py", "C:\\x", "test_x", "The system SHALL refuse a stale lock", "CONFIDENTIAL=hunter2",
         "/home/u/specs/demo/tests/test_x.py::test_x")
# A bare "/" is allowed: the done text is "<merged>/<total> merged" (design S8). Paths are caught by their forms.
FORBIDDEN = ("\\", "test_", "SHALL", "CONFIDENTIAL", "hunter2", "C:", ".py", "/a/", "/home/", "specs/", "tests/")


# --- builders --------------------------------------------------------------------------------------------

def view(**kw: Any):
    from claude_kiro.eta import decide

    fields: Dict[str, Any] = dict(spec="demo", state="on-track", p50_finish_min=None, waited_min=0.0,
                                  launch_p90_min=None, hold=none_hold(), cause="task 1 running", outcome=None,
                                  merged=0, total=3, wall_min=0.0, usd=None, now_hhmm="15:40")
    fields.update(kw)
    return decide.NotifyInput(**fields)


def sent_of(n):
    from claude_kiro.eta import decide

    return decide.SentNotice(kind=n.kind, key=n.key, level=n.level)


def none_hold():
    from claude_kiro.eta import decide

    return decide.Hold(kind="none", reason=None, reason_key=None, held=(), waiting_on_you=(), whole_run=False)


def level_of(slip: int, threshold: int) -> int:
    """Largest whole k with slip > k x threshold (k = 0 when there is no slip)."""
    return (slip - 1) // threshold if slip >= 1 else 0


def _graph(deps: Dict[str, List[str]]):
    from claude_kiro.eta.history import Graph

    ids = tuple(deps)
    reach: Dict[str, set] = {}
    for t in ids:
        seen, stack = set(), list(deps[t])
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in deps:
                continue
            seen.add(cur)
            stack.extend(deps[cur])
        reach[t] = seen
    cycles, placed = [], set()
    for t in ids:
        if t in reach[t] and t not in placed:
            group = tuple(sorted(u for u in ids if u in reach[t] and t in reach[u]))
            placed.update(group)
            cycles.append(group)
    return Graph(order=ids, deps={t: tuple(v) for t, v in deps.items()}, done=frozenset(),
                 chain={t: 0 for t in ids}, tracks={t: "impl" for t in ids}, risk={t: "normal" for t in ids},
                 complexity={t: None for t in ids}, sizes={t: 2 for t in ids}, critical=(), cycles=tuple(cycles),
                 unknown_deps={}, slots=3, slots_source="default")


def make_hold(scenario: str, base: int):
    """A real hold from `classify_hold` for the scenario, with task ids shifted by `base` (distinct reason keys)."""
    from claude_kiro import progress
    from claude_kiro.eta import decide

    a, b, c, d = (str(base + i) for i in (1, 2, 3, 4))
    start = {"schema": es.PROGRESS_SCHEMA, "at": es.iso(0), "kind": es.EV_START, "run": "r000001",
             "runKind": es.RUN_KIND_IMPLEMENT, "spec": "demo"}
    if scenario in ("park-whole", "park-partial", "fail"):
        deps = {a: [], b: [a], c: [b]}
        if scenario == "park-partial":
            deps[d] = []
        ids = list(deps)
        extra = ({"schema": es.PROGRESS_SCHEMA, "at": es.iso(5), "kind": es.EV_TASK_FAILED, "task": a,
                  "reason": "gate"} if scenario == "fail" else
                 {"schema": es.PROGRESS_SCHEMA, "at": es.iso(5), "kind": es.EV_PARK, "task": a,
                  "parkKind": es.PARK_KIND_NEEDS_INPUT, "parkedAt": es.iso(5)})
        events = [dict(start, open=ids, total=len(ids)), extra]
    else:  # cycle
        deps = {a: [b], b: [a], c: []}
        events = [dict(start, open=list(deps), total=3)]
    hold = decide.classify_hold(_graph(deps), progress.fold(events))
    assert hold.kind == "blocked", (scenario, hold)
    return hold


# --- timelines -------------------------------------------------------------------------------------------

@st.composite
def entries(draw) -> Dict[str, Any]:
    """One timeline entry: a state, the hold behind it, a slip (p50 - waited - launch p90) and a repeat count."""
    state = draw(st.sampled_from(("track", "track", "waiting", "stale", "blocked")))
    if state == "track":
        scenario = "none"
    elif state == "blocked":
        scenario = draw(st.sampled_from(SCENARIOS[1:]))
    else:
        scenario = draw(st.sampled_from(SCENARIOS))
    return {"state": state, "scenario": scenario, "base": draw(st.integers(0, 3)) * 10,
            "slip": draw(st.integers(-60, 400)), "waited": draw(st.integers(0, 120)),
            "p90": draw(st.integers(60, 300)), "repeat": draw(st.integers(1, 3))}


@st.composite
def timelines(draw) -> Dict[str, Any]:
    ents = draw(st.lists(entries(), min_size=1, max_size=14))
    end = draw(st.one_of(st.none(), st.sampled_from(OUTCOMES)))
    return {"entries": ents, "end": end, "threshold": draw(st.integers(1, 60)),
            "spec": draw(st.sampled_from(SPEC_NAMES))}


def entry_views(tl: Dict[str, Any], dirty: Optional[str] = None) -> List[Tuple[Dict[str, Any], Any]]:
    """[(entry-or-end marker, NotifyInput)] with repeats expanded; `dirty` is seeded into cause and hold reason."""
    out = []
    for e in tl["entries"]:
        hold = make_hold(e["scenario"], e["base"]) if e["scenario"] != "none" else none_hold()
        if dirty and hold.kind == "blocked":
            hold = dataclasses.replace(hold, reason="%s %s" % (hold.reason, dirty))
        p50 = None if e["state"] == "blocked" else float(e["slip"] + e["waited"] + e["p90"])
        state = {"track": "slipping" if e["slip"] > 0 else "on-track"}.get(e["state"], e["state"])
        v = view(spec=tl["spec"], state=state, p50_finish_min=p50, waited_min=float(e["waited"]),
                 launch_p90_min=float(e["p90"]), hold=hold, cause=dirty or "task 1 running")
        out.extend([(e, v)] * e["repeat"])
    if tl["end"] is not None:
        v = view(spec=tl["spec"], state="done", p50_finish_min=100.0, waited_min=0.0, launch_p90_min=100.0,
                 outcome=tl["end"], merged=2, total=6, wall_min=40.0, usd=3.5, cause=dirty or "task 1 running")
        out.extend([({"state": "done", "scenario": "none"}, v)] * 2)
    return out


def replay(views, threshold: int):
    """Call `notify_decision` after each view, feeding every notice back as sent. Returns [(index, Notice)]."""
    from claude_kiro.eta import decide

    sent: list = []
    got = []
    for i, (_, v) in enumerate(views):
        for n in decide.notify_decision(v, tuple(sent), threshold):
            got.append((i, n))
            sent.append(sent_of(n))
    return got, sent


# --- P-16 ------------------------------------------------------------------------------------------------

@SETTINGS
@given(moves=st.lists(st.tuples(st.integers(-60, 120), st.integers(0, 120)), min_size=1, max_size=40),
       p90=st.integers(60, 300), start_slip=st.integers(-100, 20), threshold=st.integers(1, 60),
       data=st.data())
def test_slip_notices_fire_exactly_at_new_levels(moves, p90, start_slip, threshold, data):
    """P-16: a slip notice at exactly the estimates whose level k first exceeds every earlier level."""
    from claude_kiro.eta import decide

    spec = data.draw(st.sampled_from(SPEC_NAMES))
    p50 = p90 + start_slip
    sent: list = []
    best = 0
    seen_levels = []
    for move, waited in moves:
        p50 += move
        slip = p50 - waited - p90
        level = level_of(slip, threshold)
        v = view(spec=spec, state="slipping" if slip > 0 else "on-track", p50_finish_min=float(p50),
                 waited_min=float(waited), launch_p90_min=float(p90))
        notices = [n for n in decide.notify_decision(v, tuple(sent), threshold) if n.kind == "slip"]
        if level >= 1 and level > best:
            assert len(notices) == 1 and notices[0].level == level, (slip, level, best, notices)
            best = level
            seen_levels.append(level)
        else:
            assert notices == [], (slip, level, best, notices)
        sent.extend(sent_of(n) for n in notices)
        # A time-only re-check of the same estimate sends nothing more (D14).
        assert [n for n in decide.notify_decision(v, tuple(sent), threshold) if n.kind == "slip"] == []
    assert seen_levels == sorted(set(seen_levels))
    if best == 0:  # complement: never more than one threshold over launch p90 means no slip notice at all
        assert not sent


# --- P-17 ------------------------------------------------------------------------------------------------

@SETTINGS
@given(tl=timelines())
def test_notification_dedupe_and_suppression(tl):
    """P-17: one done, one blocked per reason (never for a park), nothing while waiting or stale, pending counts."""
    from claude_kiro.eta import decide

    views = entry_views(tl)
    got, sent = replay(views, tl["threshold"])

    # Expected notices, derived from the timeline alone.
    best = 0
    seen_keys: set = set()
    expected: List[Tuple[int, str]] = []
    done_seen = False
    for i, (e, v) in enumerate(views):
        if v.state in ("slipping", "on-track"):
            slip = int(v.p50_finish_min - v.waited_min - v.launch_p90_min)
            level = level_of(slip, tl["threshold"])
            if level >= 1 and level > best:
                expected.append((i, "slip"))
                best = level
        elif v.state == "blocked" and e["scenario"] in ("fail", "cycle"):
            if v.hold.reason_key not in seen_keys:
                seen_keys.add(v.hold.reason_key)
                expected.append((i, "blocked"))
        elif v.state == "done" and not done_seen:
            done_seen = True
            expected.append((i, "done"))
    assert [(i, n.kind) for i, n in got] == expected, (tl, [(i, n.kind) for i, n in got], expected)

    assert sum(n.kind == "done" for _, n in got) == (1 if tl["end"] is not None else 0)
    blocked_keys = [n.key for _, n in got if n.kind == "blocked"]
    assert len(blocked_keys) == len(set(blocked_keys))
    for i, n in got:
        e, v = views[i]
        assert n.type == decide.NOTICE_TYPES[n.kind] == {
            "slip": "ck_eta_slip", "blocked": "ck_eta_blocked", "done": "ck_run_done"}[n.kind]
        if n.kind in ("slip", "blocked"):
            assert v.state not in ("waiting", "stale"), (i, v.state)
        if n.kind == "blocked":
            assert not v.hold.waiting_on_you, "a park hold must never send a blocked notice"

    # Everything already sent (or pending) is counted: replaying the whole timeline sends nothing new.
    for _, v in views:
        assert decide.notify_decision(v, tuple(sent), tl["threshold"]) == []


# --- P-18 ------------------------------------------------------------------------------------------------

@SETTINGS
@given(tl=timelines(), dirty=st.sampled_from(DIRTY), extra=st.sampled_from(DIRTY))
def test_messages_hold_no_paths_tests_or_requirement_text(tl, dirty, extra):
    """P-18: with paths, test names, requirement text and secrets seeded into reasons and causes, no message has them."""
    from claude_kiro.eta import decide

    views = entry_views(tl, dirty="%s %s" % (dirty, extra))
    got, _ = replay(views, tl["threshold"])
    for _, n in got:
        text = decide.notice_text(n)
        assert text.startswith("ck: %s" % tl["spec"]), text
        for bad in (*FORBIDDEN, dirty, extra):
            assert bad not in text, (bad, text)
        assert not re.search(r"/[A-Za-z_.~]", text), text
        if n.kind == "done":
            assert re.search(r"\d+/\d+ merged", text) or "spec written" in text, text
