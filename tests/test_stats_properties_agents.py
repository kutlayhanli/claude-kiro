"""Properties P-1 to P-5, P-7 to P-12 and P-18 of agent-mode analytics (test-plan.md, Properties).

Calls the pure decision seams exported lazily from `claude_kiro.analytics` directly (no CLI, no server,
nothing mocked). Seams are imported inside the test bodies so the module always collects.
"""

import re
import string
from datetime import datetime, timedelta, timezone

from hypothesis import HealthCheck, assume, given, settings, strategies as st

from stats_helpers import OLD_DESCRIPTIONS, SPEC_TYPES, described

PROPERTIES = {
    "P-1": "test_p1_new_run_id_skips_taken_and_invalid",
    "P-2": "test_p2_run_id_validity_is_exact",
    "P-3": "test_p3_templates_parse_exactly",
    "P-4": "test_p4_non_templates_are_other",
    "P-5": ["test_p5_marker_only_at_index_zero", "test_p5_marker_negatives"],
    "P-7": ["test_p7_create_steps_match_spec_create_labels", "test_p7_random_create_text_is_other"],
    "P-8": [
        "test_p8_outcome_definition",
        "test_p8_outcome_metamorphic",
        "test_p8_summary_none_report",
        "test_p8_done_at_start_subtracts",
        "test_p8_merge_window",
        "test_p8_task_scoping",
    ],
    "P-9": ["test_p9_open_manifest_due", "test_p9_unparseable_start_is_none", "test_p9_classify_manifest_agrees"],
    "P-10": "test_p10_backfill_allowlist",
    "P-11": ["test_p11_group_structure", "test_p11_metamorphic"],
    "P-12": "test_p12_group_ids_stable",
    "P-18": "test_p18_merged_sets_disjoint",
}

SETTINGS = settings(
    derandomize=True,
    max_examples=200,
    database=None,
    deadline=None,
    suppress_health_check=list(HealthCheck),
)

HEX = "0123456789abcdef"
RUN_ID_RX = re.compile(r"ag_[0-9a-f]{8}")
valid_ids = st.text(alphabet=HEX, min_size=8, max_size=8).map(lambda h: "ag_" + h)


def _a():
    import claude_kiro.analytics as a

    return a


# ---------------------------------------------------------------------------------------------- P-1

BAD_TOKENS = ["0123ABCD", "0123abc", "0123abcde", "0123abcg", "０１２３４５６７", "", "ABCDEF01", " 123abcd"]


@st.composite
def _stream_case(draw):
    taken = draw(st.lists(valid_ids, max_size=50, unique=True))
    forced = [t[3:] for t in taken[: draw(st.integers(0, 5))]]
    junk = draw(st.lists(st.sampled_from(BAD_TOKENS), max_size=6))
    head = draw(st.permutations(forced + junk))
    free = draw(st.text(alphabet=HEX, min_size=8, max_size=8))
    assume("ag_" + free not in taken)
    return set(taken), list(head) + [free]


@given(_stream_case())
@SETTINGS
def test_p1_new_run_id_skips_taken_and_invalid(case):
    taken, stream = case
    it = iter(stream)
    got = _a().new_agent_run_id(taken, token=lambda: next(it))
    assert RUN_ID_RX.fullmatch(got), got
    assert got not in taken
    expected = next(
        "ag_" + c for c in stream if re.fullmatch(r"[0-9a-f]{8}", c) and "ag_" + c not in taken
    )
    assert got == expected


# ---------------------------------------------------------------------------------------------- P-2

NEAR_MISS_IDS = [
    "AG_0123abcd", "Ag_0123abcd", "ag_0123ABCD", "ag_0123AbCd", "ag_0123abc", "ag_0123abcde", "ag-0123abcd",
    "ag__0123abcd", " ag_0123abcd", "ag_0123abcd ", "ag_0123abcd\n", "\tag_0123abcd", "ag_0123abcd\t",
    "wf_0123abcd", "wf_0123abcdef01", "[ck:ag_0123abcd]", "ag_０１２３４５６７", "ag_٠١٢٣٤٥٦٧", "ag_", "", "ag_0123abcg",
    None, 12345, 0, b"ag_0123abcd", ["ag_0123abcd"],
]


@given(st.one_of(
    valid_ids.map(lambda s: (s, True)),
    st.sampled_from(NEAR_MISS_IDS).map(lambda s: (s, False)),
    st.text(max_size=14).filter(lambda s: not RUN_ID_RX.fullmatch(s)).map(lambda s: (s, False)),
))
@SETTINGS
def test_p2_run_id_validity_is_exact(case):
    value, expected = case
    assert _a().is_agent_run_id(value) is expected, repr(value)


# ---------------------------------------------------------------------------------------------- P-3

TEST_WRITER_TYPES = {"spec:test-writer", "ck-test-writer"}
task_ids = st.one_of(st.integers(1, 999).map(str), st.sampled_from(["3_a", "T1", "17", "a_b_2"]))
ks = st.integers(1, 99)


@st.composite
def _template_case(draw):
    """(rest, agent_type, expected) for one of the D4 template forms."""
    n, k, typ = draw(task_ids), draw(ks), draw(st.sampled_from(SPEC_TYPES))
    work = "test-writer" if typ in TEST_WRITER_TYPES else "implementer"
    form = draw(st.sampled_from(
        ["implement", "retry", "revise", "review", "resolve", "resolve_round", "fix", "fix_landing"]
    ))
    if form == "implement":
        return f"implement task {n}", typ, {"role": work, "task": n, "attempt": 1}
    if form == "retry":
        return f"implement task {n} retry {k}", typ, {"role": work, "task": n, "attempt": k + 1}
    if form == "revise":
        return f"revise task {n} round {k}", typ, {"role": work, "task": n, "round": k}
    if form == "review":
        return f"review task {n} round {k}", typ, {"role": "reviewer", "task": n, "round": k}
    if form == "resolve":
        return f"resolve task {n}", typ, {"role": "resolver", "task": n}
    if form == "resolve_round":
        return f"resolve task {n} round {k}", typ, {"role": "resolver", "task": n, "round": k}
    if form == "fix":
        return f"fix {k}", typ, {"role": "fixer", "attempt": k, "step": "target"}
    return f"fix task {n} attempt {k}", typ, {"role": "fixer", "task": n, "attempt": k, "step": "landing"}


@given(_template_case(), valid_ids)
@SETTINGS
def test_p3_templates_parse_exactly(case, run_id):
    rest, typ, expected = case
    got = _a().parse_agent_description(described(run_id, rest), typ)
    assert got["matched"] is True, (rest, typ, got)
    assert got["run_id"] == run_id
    for key, value in expected.items():
        assert got[key] == value, (rest, typ, key, got)
    # The same template without the marker parses the same, with no run id.
    bare = _a().parse_agent_description(rest, typ)
    assert bare["run_id"] is None
    assert bare["matched"] is True
    for key, value in expected.items():
        assert bare[key] == value


# ---------------------------------------------------------------------------------------------- P-4

NEAR_MISSES = [
    "implement demo: task 3", "implement Task 3", "implement task", "implement task 3 retry",
    "implement task 3 retry x", "review task 3 round", "review task 3 round 0x2", "fix demo: 2",
    "implement task 3 ", " implement task 3", "implement  task 3", "implement\ttask 3", "implement task 3\n",
    "implement task 3 (opus)", "implement task ３", "task 3 [test]", "land 1 2", "create bogus",
    "create requirements.md", "create write requirements.md", "create Requirements", "create requirements ",
    "create", "", "revise task 3", "Implement task 3", "review task 3", "resolve task", "fix", "fix task 3",
    "fix task 3 attempt", "implement task 3 retry 1 2", "implement demo task 3", "gate", "plan",
]
other_text = st.text(alphabet=string.ascii_letters + string.digits + " :-_[]()./\t", max_size=30).filter(
    lambda s: not re.match(r"(implement|revise|review|resolve|fix|create)\b", s)
)


@given(
    st.one_of(st.sampled_from(NEAR_MISSES), other_text),
    valid_ids,
    st.sampled_from(list(SPEC_TYPES) + ["general-purpose", "Explore", None]),
    st.booleans(),
)
@SETTINGS
def test_p4_non_templates_are_other(rest, run_id, typ, marked):
    description = described(run_id, rest) if marked else rest
    got = _a().parse_agent_description(description, typ)
    assert got["role"] == "other", (description, typ, got)
    assert got["matched"] is False
    assert got["task"] is None and got["attempt"] is None and got["round"] is None


# ---------------------------------------------------------------------------------------------- P-5

MARKER_NEGATIVES = [
    lambda i, r: f"word [ck:{i}] {r}",
    lambda i, r: f"[CK:{i}] {r}",
    lambda i, r: f"[Ck:{i}] {r}",
    lambda i, r: f"[ck: {i}] {r}",
    lambda i, r: f"[ck:{i.upper()}] {r}",
    lambda i, r: f"[ck:{i}x] {r}",
    lambda i, r: f"[ck:ag_X] {r}",
    lambda i, r: f"(ck:{i}) {r}",
    lambda i, r: f"[ck:{i}]x{r}",
    lambda i, r: f" [ck:{i}] {r}",
    lambda i, r: f"[ck:{i}",
    lambda i, r: f"[ck:{i[3:]}] {r}",
    lambda i, r: f"[ck:wf_{i[3:]}] {r}",
    lambda i, r: f"ck:{i} {r}",
]
P5_TYPES = st.sampled_from(list(SPEC_TYPES) + ["general-purpose", "Explore", None])


@given(valid_ids, st.text(max_size=20), st.booleans(), P5_TYPES)
@SETTINGS
def test_p5_marker_only_at_index_zero(run_id, rest, with_rest, typ):
    d = f"[ck:{run_id}]" + (" " + rest if with_rest else "")
    assert _a().parse_agent_description(d, typ)["run_id"] == run_id
    # Independent of the agent type.
    assert _a().parse_agent_description(d, None)["run_id"] == run_id
    # Two markers: only the first counts.
    other = "ag_" + "f" * 8
    assert _a().parse_agent_description(f"[ck:{run_id}] [ck:{other}] {rest}", typ)["run_id"] == run_id


@given(
    valid_ids,
    st.text(max_size=20),
    st.sampled_from(range(len(MARKER_NEGATIVES))),
    st.text(max_size=8).filter(lambda s: s != "" and not s.startswith("[ck:")),
    P5_TYPES,
    st.one_of(st.none(), st.integers(), st.sampled_from(OLD_DESCRIPTIONS).map(lambda s: s.format(spec="demo", n=3))),
)
@SETTINGS
def test_p5_marker_negatives(run_id, rest, which, prefix, typ, oddball):
    a = _a()
    assert a.parse_agent_description(MARKER_NEGATIVES[which](run_id, rest), typ)["run_id"] is None
    # Metamorphic: any text in front of an exact marker destroys it.
    assert a.parse_agent_description(prefix + f"[ck:{run_id}] {rest}", typ)["run_id"] is None
    # Old-style descriptions and non-strings never yield an id.
    assert a.parse_agent_description(oddball, typ)["run_id"] is None




# ---------------------------------------------------------------------------------------------- P-7


@given(st.data(), valid_ids)
@SETTINGS
def test_p7_create_steps_match_spec_create_labels(data, run_id):
    a = _a()
    steps = a.AGENT_CREATE_STEPS
    assert steps
    step = data.draw(st.sampled_from(sorted(steps)))
    got = a.parse_agent_description(described(run_id, "create " + step), "general-purpose")
    want = a.parse_label(steps[step], "spec-create")
    assert got["matched"] is True
    assert got["role"] == want["role"] and got["step"] == want["step"], (step, got, want)

    # Negatives: case changes, suffixes, the workflow label itself, empty.
    negatives = [
        step.upper(), step.title(), step + ".md", step + " ", " " + step, "",
        steps[step], "write requirements.md", "requirements.md",
    ]
    bad = data.draw(st.sampled_from(negatives))
    assume(bad not in steps)
    other = a.parse_agent_description(described(run_id, "create " + bad), "general-purpose")
    assert other["role"] == "other" and other["matched"] is False, (bad, other)


@given(st.text(max_size=25), valid_ids)
@SETTINGS
def test_p7_random_create_text_is_other(s, run_id):
    a = _a()
    assume(s not in a.AGENT_CREATE_STEPS)
    got = a.parse_agent_description(described(run_id, "create " + s), "spec:planner")
    assert got["role"] == "other" and got["matched"] is False




# ---------------------------------------------------------------------------------------------- P-8

LISTS = ("merged", "done", "failed", "blocked", "unstarted")
BASE_MS = 1_800_000_000_000  # an arbitrary instant


def _fmt(ms, style):
    """Render epoch ms as a `%cI`-like string in one of several offset forms."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    if style == "Z":
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if style == "utc":
        return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    tz = timezone(timedelta(hours=2))
    return dt.astimezone(tz).strftime("%Y-%m-%dT%H:%M:%S+02:00")


def _entry(name, lst, mergedAt="2000-01-01T00:00:00+00:00"):
    if lst == "merged":
        return {"task": name, "commit": "abc", "mergedAt": mergedAt, "subject": "m", "done": True}
    if lst == "failed":
        return {"task": name, "branch": "b", "ahead": 1}
    if lst == "blocked":
        return {"task": name, "on": ["1"]}
    return {"task": name}


def _report(placement, times=None):
    rep = {"spec": "demo", "into": "integrate/demo", "head": "h", **{k: [] for k in LISTS}}
    for i, lst in enumerate(placement):
        name = str(i + 1)
        at = times[i] if times else "2000-01-01T00:00:00+00:00"
        rep[lst].append(_entry(name, lst, at))
    return rep


placements = st.lists(st.sampled_from(LISTS), max_size=20)
RANK = {"complete": 2, "blocked": 1, "failed": 0}


def _tasks_of(rep, *lists):
    return {e["task"] for k in (lists or LISTS) for e in rep[k]}


@given(placements, st.randoms(use_true_random=False))
@SETTINGS
def test_p8_outcome_definition(placement, rnd):
    a = _a()
    rep = _report(placement)
    got = a.implement_outcome(rep)
    if any(p == "failed" for p in placement):
        assert got == "failed"
    elif all(p in ("merged", "done") for p in placement):
        assert got == "complete"
    else:
        assert got == "blocked"
    assert a.implement_outcome(None) is None
    shuffled = {**rep, **{k: rnd.sample(rep[k], len(rep[k])) for k in LISTS}}
    assert a.implement_outcome(shuffled) == got


@given(placements.filter(bool), st.data())
@SETTINGS
def test_p8_outcome_metamorphic(placement, data):
    a = _a()
    merged_ix = [i for i, p in enumerate(placement) if p == "merged"]
    assume(merged_ix)
    before = a.implement_outcome(_report(placement))
    i = data.draw(st.sampled_from(merged_ix))
    moved = list(placement)
    moved[i] = "failed"
    after = a.implement_outcome(_report(moved))
    assert RANK[after] <= RANK[before]
    assert after == "failed"


@given(st.one_of(st.none(), st.just({})), st.one_of(st.none(), st.sets(st.sampled_from("12345"))),
       st.one_of(st.none(), st.integers(0, 10**12)), st.one_of(st.none(), st.sets(st.sampled_from("12345"))))
@SETTINGS
def test_p8_summary_none_report(report, done_at_start, start, run_tasks):
    got = _a().implement_summary(
        report, done_at_start=done_at_start, start_ms=start, end_ms=start, run_tasks=run_tasks
    )
    for key in ("outcome", "merged", "failed", "blocked", "not_started"):
        assert got[key] is None, (key, got)


@given(placements, st.data())
@SETTINGS
def test_p8_done_at_start_subtracts(placement, data):
    rep = _report(placement)
    all_ids = sorted(_tasks_of(rep))
    D = set(data.draw(st.lists(st.sampled_from(all_ids), unique=True))) if all_ids else set()
    # Also cover the empty and the full subset explicitly.
    D = data.draw(st.sampled_from([D, set(), set(all_ids)]))
    got = _a().implement_summary(rep, done_at_start=D)
    assert set(got["merged"]) == (_tasks_of(rep, "merged", "done")) - D
    assert not (set(got["merged"]) & D)
    assert len(got["merged"]) == len(set(got["merged"]))


@given(
    st.lists(st.tuples(st.sampled_from(["merged", "done", "failed", "unstarted"]),
                       st.sampled_from(["before", "start", "inside", "end", "after", "bad"]),
                       st.sampled_from(["Z", "utc", "+02"]),
                       st.integers(1, 3000)), max_size=20),
    st.integers(0, 10**6), st.integers(0, 10**6),
)
@SETTINGS
def test_p8_merge_window(rows, start_s, span_s):
    start_ms = BASE_MS + start_s * 1000
    end_ms = start_ms + span_s * 1000
    placement, times, inside = [], [], set()
    for i, (lst, where, style, off) in enumerate(rows):
        placement.append(lst)
        ms = {
            "before": start_ms - 1000, "start": start_ms, "end": end_ms, "after": end_ms + 1000,
            "inside": start_ms + (off * 1000) % (span_s * 1000 + 1000) // 1000 * 1000,
        }.get(where)
        times.append("not a date" if where == "bad" else _fmt(ms, style))
        if lst == "merged" and where in ("start", "end", "inside"):
            inside.add(str(i + 1))
    rep = _report(placement, times)
    got = _a().implement_summary(rep, done_at_start=None, start_ms=start_ms, end_ms=end_ms)
    assert set(got["merged"]) == inside, (rows, got)


@given(placements, st.data())
@SETTINGS
def test_p8_task_scoping(placement, data):
    a = _a()
    rep = _report(placement)
    ids = sorted(_tasks_of(rep))
    T = set(data.draw(st.lists(st.sampled_from(ids), unique=True))) if ids else set()
    T = data.draw(st.sampled_from([T, set(), set(ids), set(ids[:1])]))
    T = T | set(data.draw(st.lists(st.sampled_from(["900", "901"]), unique=True)))  # ids absent from the report
    got = a.implement_summary(rep, run_tasks=T)
    for key in ("failed", "blocked", "not_started"):
        assert set(got[key]) <= T, (key, got, T)
    if not T:
        assert got["outcome"] is None
    elif T & set(ids):
        restricted = {**rep, **{k: [e for e in rep[k] if e["task"] in T] for k in LISTS}}
        assert got["outcome"] == a.implement_outcome(restricted)
    # Tasks outside T never change the outcome or the lists.
    extra = data.draw(st.lists(st.sampled_from(["failed", "blocked", "unstarted"]), min_size=1, max_size=5))
    bigger = {**rep, **{k: list(rep[k]) for k in LISTS}}
    for j, lst in enumerate(extra):
        bigger[lst].append(_entry(f"out_{j}", lst))
    again = a.implement_summary(bigger, run_tasks=T)
    assert again["outcome"] == got["outcome"]
    for key in ("failed", "blocked", "not_started"):
        assert set(again[key]) == set(got[key])
    # run_tasks None is the whole-spec form.
    whole = a.implement_summary(rep, run_tasks=None)
    assert whole == a.implement_summary(rep)
    assert whole["outcome"] == a.implement_outcome(rep)


# ---------------------------------------------------------------------------------------------- P-9

SIX_H_MS = 6 * 3600 * 1000
OFFSETS = {"Z": timedelta(0), "+00:00": timedelta(0), "+02:00": timedelta(hours=2), "-05:30": -timedelta(hours=5, minutes=30)}


def _iso(ms, form, fractional, seconds=True):
    off = OFFSETS[form]
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) + off
    suffix = "Z" if form == "Z" else form
    if not seconds:
        return dt.strftime("%Y-%m-%dT%H:%M") + suffix
    s = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if fractional:
        s += ".%03d" % (ms % 1000)
    return s + suffix


deltas = st.one_of(
    st.sampled_from([-1000, -1, 0, 1, 1000, -SIX_H_MS - 5000, 10**9, -10**8, 10**11]),
    st.integers(-10**8, 10**8),
)


@given(st.integers(0, 4 * 10**9).map(lambda x: BASE_MS + x), deltas, st.sampled_from(sorted(OFFSETS)),
       st.sampled_from(["frac", "plain", "noseconds"]), st.sampled_from(sorted(OFFSETS)))
@SETTINGS
def test_p9_open_manifest_due(start_ms, delta, form, shape, now_form):
    a = _a()
    if shape == "noseconds":
        start_ms -= start_ms % 60000
    elif shape == "plain":
        start_ms -= start_ms % 1000
    now_ms = start_ms + SIX_H_MS + delta
    start = _iso(start_ms, form, shape == "frac", seconds=shape != "noseconds")
    now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(timezone(OFFSETS[now_form]))
    got = a.open_manifest_due(start, now)
    assert got is (delta > 0), (start, now, delta)
    # Same instant, another offset spelling: same answer.
    again = a.open_manifest_due(_iso(start_ms, "Z", shape == "frac", seconds=shape != "noseconds"), now)
    assert again is got


UNPARSEABLE = ["", "yesterday", 12345, 1.5, None, "2026-10-29T10:00:00", "2026-10-29", [], {}, "not-a-time", "T10:00:00Z"]


@given(st.sampled_from(UNPARSEABLE))
@SETTINGS
def test_p9_unparseable_start_is_none(start):
    assert _a().open_manifest_due(start, datetime(2026, 10, 29, 12, tzinfo=timezone.utc)) is None


def _manifest(start):
    role = {"model": "sonnet", "effort": "medium", "enabled": True}
    return {
        "schema": "ck-run-manifest/1", "runId": "ag_0123abcd", "kind": "implement", "startTime": start,
        "sessionId": "s1", "ckVersion": "0.0.0", "spec": "demo", "root": "/work/demo", "into": "integrate/demo",
        "doneAtStart": [],
        "agents": {k: dict(role) for k in (
            "implementer", "test_writer", "reviewer", "reviewer_risky", "fixer", "resolver", "orchestrator"
        )},
    }


@given(
    st.one_of(
        st.tuples(st.integers(0, 4 * 10**9).map(lambda x: BASE_MS + x - x % 1000), deltas,
                  st.sampled_from(sorted(OFFSETS))).map(lambda t: ("ok",) + t),
        st.sampled_from(UNPARSEABLE).map(lambda s: ("bad", s)),
    )
)
@SETTINGS
def test_p9_classify_manifest_agrees(case):
    a = _a()
    now = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
    if case[0] == "bad":
        start = case[1]
    else:
        _, start_ms, delta, form = case
        now = datetime.fromtimestamp((start_ms + SIX_H_MS + delta) / 1000, tz=timezone.utc)
        start = _iso(start_ms, form, False)
    due = a.open_manifest_due(start, now)
    got = a.classify_manifest(_manifest(start), now)
    assert got == {True: "collect", False: "young", None: "malformed"}[due], (start, now, got, due)



# ------------------------------------------------------------------------------- P-10, P-11, P-12, P-18

DAY_MS = 86_400_000
MIN_MS = 60_000
OTHER_TYPES = ["general-purpose", "Explore", "spec:planner", None, "spec:implementer-x", "SPEC:IMPLEMENTER"]
SPEC_NAMES = ["demo", "ledger", "zephyr-payroll"]
SPECLESS = [None, "review task 3 diff", "reviewing things"]


def _desc(spec, n):
    if spec is None:
        return SPECLESS[n % len(SPECLESS)] if n % 3 else OLD_DESCRIPTIONS[3].format(spec="x", n=n)
    return OLD_DESCRIPTIONS[n % 3].format(spec=spec, n=n)


@st.composite
def _agents_case(draw, mixed=True, one_spec_per_session=False, min_size=1):
    """(agents, spec_of) where spec_of maps agentId -> the spec the description names (or None)."""
    n_sessions = draw(st.integers(1, 4))
    sessions = ["s%d" % i for i in range(n_sessions)]
    pool = draw(st.lists(st.sampled_from(SPEC_NAMES), max_size=3, unique=True))
    share = draw(st.integers(0, 50))
    session_spec = {s: draw(st.sampled_from(SPEC_NAMES)) for s in sessions}
    n = draw(st.integers(min_size, 40))
    agents, spec_of = [], {}
    for i in range(n):
        sid = draw(st.sampled_from(sessions))
        spec = None
        if one_spec_per_session:
            spec = session_spec[sid]
        elif pool:
            spec = draw(st.sampled_from(pool))
        if draw(st.integers(0, 99)) < share:
            spec = None
        start = draw(st.integers(0, DAY_MS // 1000)) * 1000
        dur = draw(st.integers(1, 4 * 3600)) * 1000
        typ = draw(st.sampled_from(SPEC_TYPES))
        workflow = False
        desc = _desc(spec, i)
        if mixed:
            typ = draw(st.sampled_from(list(SPEC_TYPES) * 2 + OTHER_TYPES))
            workflow = draw(st.integers(0, 9)) == 0
            marker_kind = draw(st.sampled_from(["none"] * 5 + ["valid", "invalid"]))
            if marker_kind == "valid":
                desc = described("ag_%08x" % i, "implement task %d" % i)
                spec = None
            elif marker_kind == "invalid":
                desc = "[ck:ag_X] " + (desc or "implement task 1")
                spec = None  # the prefixed description no longer parses to a spec
        agent = {
            "agentId": "a%04d" % i, "sessionId": sid, "agentType": typ, "description": desc,
            "startMs": start, "endMs": start + dur, "workflow": workflow,
        }
        agents.append(agent)
        spec_of[agent["agentId"]] = spec
    return agents, spec_of


def _marked(desc):
    return isinstance(desc, str) and re.match(r"\[ck:ag_[0-9a-f]{8}\]( |\Z)", desc) is not None


def _is_candidate(a):
    return a["agentType"] in SPEC_TYPES and not _marked(a["description"]) and not a["workflow"]


gaps = st.integers(1, 600)


@given(_agents_case(mixed=True), gaps)
@SETTINGS
def test_p10_backfill_allowlist(case, gap):
    agents, _ = case
    out = _a().group_backfill(agents, gap_minutes=gap)
    by_id = {a["agentId"]: a for a in agents}
    grouped = [i for g in out["groups"] for i in g["agentIds"]]
    assert len(grouped) == len(set(grouped))
    excluded = {e["agentId"]: e["reason"] for e in out["excluded"]}
    assert not (set(grouped) & set(excluded))
    for i in grouped:
        assert _is_candidate(by_id[i]), by_id[i]
    for a in agents:
        i = a["agentId"]
        if a["workflow"]:
            assert i not in grouped
            assert excluded.get(i, "workflow agent") == "workflow agent"
        elif _marked(a["description"]):
            assert i not in grouped and excluded.get(i) == "carries a marker", (a, excluded.get(i))
        elif a["agentType"] not in SPEC_TYPES:
            assert i not in grouped and excluded.get(i) == "not a spec agent type", (a, excluded.get(i))
        else:
            # A candidate is grouped or left out for lack of a spec or timing; never silently dropped.
            assert (i in grouped) != (i in excluded)
            if i in excluded:
                assert excluded[i] in ("no spec", "no timing")


def _spec_named(case, group):
    agents, spec_of = case
    return [i for i in group["agentIds"] if spec_of[i] is not None]


def _check_groups(case, out, gap_minutes):
    agents, spec_of = case
    gap = gap_minutes * MIN_MS
    by_id = {a["agentId"]: a for a in agents}
    cands = {a["agentId"] for a in agents if _is_candidate(a)}
    grouped = [i for g in out["groups"] for i in g["agentIds"]]
    excluded = {e["agentId"]: e["reason"] for e in out["excluded"]}
    # Partition: each candidate is in exactly one group or excluded.
    assert sorted(grouped) == sorted(set(grouped))
    assert set(grouped) | {i for i in excluded if i in cands} == cands
    assert not (set(grouped) & set(excluded))
    for g in out["groups"]:
        members = [by_id[i] for i in g["agentIds"]]
        assert {m["sessionId"] for m in members} == {g["sessionId"]}
        named = [m for m in members if spec_of[m["agentId"]] is not None]
        assert named, g
        assert {spec_of[m["agentId"]] for m in named} == {g["spec"]}
        # Ordered by start.
        starts = [m["startMs"] for m in members]
        assert starts == sorted(starts), g
        # Spec-named members chain within the gap, using the running maximum end.
        top = None
        for m in sorted(named, key=lambda m: (m["startMs"], m["agentId"])):
            if top is not None:
                assert m["startMs"] - top <= gap, (g, m)
            top = m["endMs"] if top is None else max(top, m["endMs"])
        assert g["startMs"] == min(m["startMs"] for m in named)
        assert g["endMs"] == max(m["endMs"] for m in named)
        for m in members:
            if spec_of[m["agentId"]] is None:
                assert m["startMs"] <= g["endMs"] + gap and m["endMs"] >= g["startMs"] - gap, (g, m)
    # Consecutive groups of one session and spec are more than `gap` apart.
    by_key = {}
    for g in out["groups"]:
        by_key.setdefault((g["sessionId"], g["spec"]), []).append(g)
    for gs in by_key.values():
        gs.sort(key=lambda g: g["startMs"])
        for prev, nxt in zip(gs, gs[1:]):
            assert nxt["startMs"] - prev["endMs"] > gap, (prev, nxt)
    # Every spec-named candidate with timing is grouped; spec-less leftovers have no group within the gap.
    for i in cands:
        if spec_of[i] is not None:
            assert i in grouped, i
        elif i in excluded:
            assert excluded[i] == "no spec"
            m = by_id[i]
            for g in out["groups"]:
                if g["sessionId"] == m["sessionId"]:
                    assert not (m["startMs"] <= g["endMs"] + gap and m["endMs"] >= g["startMs"] - gap), (g, m)


@given(_agents_case(mixed=True), gaps)
@SETTINGS
def test_p11_group_structure(case, gap):
    out = _a().group_backfill(case[0], gap_minutes=gap)
    _check_groups(case, out, gap)


@given(_agents_case(mixed=False), gaps, gaps, st.randoms(use_true_random=False))
@SETTINGS
def test_p11_metamorphic(case, gap1, gap2, rnd):
    a = _a()
    agents = case[0]
    lo, hi = sorted((gap1, gap2))
    out_lo = a.group_backfill(agents, gap_minutes=lo)
    out_hi = a.group_backfill(agents, gap_minutes=hi)
    assert len(out_hi["groups"]) <= len(out_lo["groups"])
    shuffled = rnd.sample(agents, len(agents))
    again = a.group_backfill(shuffled, gap_minutes=lo)
    assert again["groups"] == out_lo["groups"]
    key = lambda e: e["agentId"]
    assert sorted(again["excluded"], key=key) == sorted(out_lo["excluded"], key=key)




@given(_agents_case(mixed=True), gaps, st.randoms(use_true_random=False))
@SETTINGS
def test_p12_group_ids_stable(case, gap, rnd):
    a = _a()
    agents = case[0]
    out = a.group_backfill(agents, gap_minutes=gap)
    ids = [g["id"] for g in out["groups"]]
    assert all(a.is_agent_run_id(i) for i in ids)
    assert len(set(ids)) == len(ids)
    members = {frozenset(g["agentIds"]): g["id"] for g in out["groups"]}
    # Same input, same ids.
    assert [g["id"] for g in a.group_backfill(agents, gap_minutes=gap)["groups"]] == ids
    # Any permutation, same ids per member set.
    shuffled = a.group_backfill(rnd.sample(agents, len(agents)), gap_minutes=gap)
    assert {frozenset(g["agentIds"]): g["id"] for g in shuffled["groups"]} == members
    # An agent added to another session leaves this session's ids unchanged.
    extra = {
        "agentId": "zz_extra", "sessionId": "s_other", "agentType": "spec:implementer",
        "description": OLD_DESCRIPTIONS[0].format(spec="demo", n=1), "startMs": 5_000, "endMs": 9_000,
        "workflow": False,
    }
    grown = a.group_backfill(agents + [extra], gap_minutes=gap)
    kept = {frozenset(g["agentIds"]): g["id"] for g in grown["groups"] if g["sessionId"] != "s_other"}
    assert kept == members


@given(
    _agents_case(mixed=False, one_spec_per_session=True),
    gaps,
    st.lists(st.tuples(st.integers(-3600, DAY_MS // 1000 + 3 * 3600), st.sampled_from(["Z", "utc", "+02"])), max_size=20),
)
@SETTINGS
def test_p18_merged_sets_disjoint(case, gap, merges):
    a = _a()
    agents = case[0]
    placement = ["merged"] * len(merges)
    times = [_fmt(BASE_MS + s * 1000, style) for s, style in merges]
    # Re-base the agents onto the same clock as the merge times.
    shifted = [{**x, "startMs": x["startMs"] + BASE_MS, "endMs": x["endMs"] + BASE_MS} for x in agents]
    out = a.group_backfill(shifted, gap_minutes=gap)
    # Add merges sitting exactly on and just around each group's window edges.
    extra_times = []
    for g in out["groups"]:
        end = g["endMs"] + min(30 * MIN_MS, gap * MIN_MS)
        for ms in (g["startMs"] - 1000, g["startMs"], g["endMs"], end, end + 1000):
            extra_times.append(_fmt(ms, "Z"))
    rep = _report(placement + ["merged"] * len(extra_times), times + extra_times)
    window = {}
    for g in out["groups"]:
        got = a.implement_summary(
            rep, done_at_start=None, start_ms=g["startMs"], end_ms=g["endMs"] + min(30 * MIN_MS, gap * MIN_MS)
        )["merged"]
        window[g["id"]] = (g["sessionId"], g["spec"], set(got))
    groups = list(window.values())
    for i, (s1, sp1, m1) in enumerate(groups):
        for s2, sp2, m2 in groups[i + 1:]:
            if (s1, sp1) == (s2, sp2):
                assert not (m1 & m2), (s1, sp1, m1 & m2)
