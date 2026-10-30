"""Properties of the run progress file (run-followups P-14, P-15).

P-14 runs `progress.read_events(path)` over valid events interleaved with every malformed-line form.
P-15 runs `progress.append(key, event, env)` with extra keys holding confidential values and checks the written line.
Both call the pure seams directly: no CLI, no server.
"""

import itertools
import json
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import eta_strategies as es

pytestmark = es.NO_NETWORK

PROPERTIES = {
    "P-14": "test_p14_read_events_returns_exactly_the_valid_events_in_order",
    "P-15": "test_p15_append_writes_only_the_typed_fields",
}

SETTINGS = settings(max_examples=200, derandomize=True, database=None, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture,
                                           HealthCheck.data_too_large, HealthCheck.large_base_example])


# --------------------------------------------------------------------------- P-14

@st.composite
def interleaved(draw):
    """(valid events, raw file bytes, malformed count): malformed lines inserted at random positions."""
    evs = draw(es.events())
    bad = draw(st.lists(es.malformed_raw(), max_size=12))
    slots = [draw(st.integers(0, len(evs))) for _ in bad]
    chunks = []
    for i, ev in enumerate(evs):
        for b, s in zip(bad, slots):
            if s == i:
                chunks.append(b)
        chunks.append(json.dumps(ev).encode("utf-8") + b"\n")
    for b, s in zip(bad, slots):
        if s == len(evs):
            chunks.append(b)
    return evs, b"".join(chunks), len(bad)


@SETTINGS
@given(data=interleaved())
def test_p14_read_events_returns_exactly_the_valid_events_in_order(tmp_path_factory, data):
    from claude_kiro import progress

    evs, raw, bad = data
    path = tmp_path_factory.mktemp("p14") / "run.jsonl"
    path.write_bytes(raw)
    got, skipped = progress.read_events(path)
    assert got == evs
    assert skipped == bad


# --------------------------------------------------------------------------- P-15

_ID = st.text("abcdef0123456789", min_size=1, max_size=8)
_TASK = st.integers(1, 30).map(str)
_TASKS = st.lists(_TASK, max_size=4)
_ATTEMPT = st.one_of(st.none(), st.integers(1, 4))
_MODEL = st.one_of(st.none(), st.sampled_from((*es.MODELS, es.MODEL_INHERIT)))
_OUTCOME = st.sampled_from(("completed", "partial", "halted", "refused", "failed", "superseded"))
_LAUNCH = st.fixed_dictionaries({"finish": st.none(), "remainingMin": st.none(), "usd": st.none(),
                                 "confidence": st.sampled_from(("low", "medium", "high"))})
_NOTICE = st.sampled_from(("slip", "blocked", "done"))

_CLAIM = {"task": _TASK, "attempt": _ATTEMPT, "model": _MODEL}
# kind -> {typed field: value strategy}, from design.md "Progress file (ck-progress/1)".
TYPED = {
    es.EV_START: {"run": _ID, "runKind": st.sampled_from(("implement", "create")), "spec": _ID, "root": st.just("/r"),
                  "mode": st.sampled_from(("workflow", "agents")), "slots": st.integers(1, 9),
                  "slotsSource": st.sampled_from(("runtime", "default")),
                  "maxConcurrent": st.one_of(st.none(), st.integers(1, 9)), "open": _TASKS,
                  "total": st.integers(0, 30), "launch": _LAUNCH, "model": st.just("m.json"),
                  "modelDigest": _ID, "spool": st.booleans()},
    **{k: _CLAIM for k in es.CLAIM_EVENTS},
    es.EV_MERGE: {"task": _TASK},
    es.EV_LANDING: {"tasks": _TASKS, "merged": _TASKS, "red": _TASKS, "conflict": _TASKS, "protected": _TASKS,
                    "green": st.one_of(st.none(), st.booleans())},
    es.EV_TASK_FAILED: {"task": _TASK,
                        "reason": st.sampled_from(("gate", "review", "landing", "conflict", "worktree", "other"))},
    es.EV_HAND_LANDING: {"task": _TASK},
    es.EV_PARK: {"task": _TASK, "parkKind": st.just(es.PARK_KIND_NEEDS_INPUT), "parkedAt": st.just(es.iso(1))},
    es.EV_UNPARK: {"task": _TASK},
    es.EV_APPROVAL_START: {"id": _ID, "reason": st.just("question")},
    es.EV_APPROVAL_END: {"id": _ID},
    es.EV_PHASE_START: {"phase": st.sampled_from(es.CREATE_PHASES)},
    es.EV_PHASE_END: {"phase": st.sampled_from(es.CREATE_PHASES)},
    es.EV_STEP_END: {"phase": st.sampled_from(es.CREATE_PHASES)},
    es.EV_END: {"outcome": _OUTCOME, "merged": st.one_of(st.none(), st.integers(0, 30)),
                "total": st.one_of(st.none(), st.integers(0, 30)), "usd": st.one_of(st.none(), st.just(1.5))},
    es.EV_NOTIFY_PENDING: {"notice": _NOTICE, "key": _ID, "message": st.just("run is slipping by 5 min")},
    es.EV_NOTIFIED: {"notice": _NOTICE, "key": _ID, "level": st.one_of(st.none(), st.integers(1, 5)),
                     "outcome": st.sampled_from(("sent", "not_configured", "failed", "deferred")),
                     "detail": st.one_of(st.none(), st.just("ok"))},
}
COMMON = ("schema", "at", "kind")

_SECRET_TAILS = st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), max_size=30)
_EXTRA_VALUE = st.one_of(
    _SECRET_TAILS.map(lambda t: "CONFIDENTIAL " + t),
    _SECRET_TAILS.map(lambda t: "/home/dana/CONFIDENTIAL/src/ledger.py" + t),
    _SECRET_TAILS.map(lambda t: "C:\\CONFIDENTIAL\\ledger.py" + t),
    _SECRET_TAILS.map(lambda t: "line one CONFIDENTIAL\nline two\r\n" + t + "\nline three"),
    _SECRET_TAILS.map(lambda t: "def test_x():\n    assert CONFIDENTIAL" + t),
)
_EXTRA_KEY = st.one_of(
    st.sampled_from(("prompt", "transcript", "contents", "diff", "text", "note", "path", "content", "reason2")),
    st.text("abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12),
)
_COUNTER = itertools.count()


@st.composite
def event_with_extras(draw):
    kind = draw(st.sampled_from(es.ALL_EVENTS))
    allowed = {name: draw(strategy) for name, strategy in TYPED[kind].items()}
    present = {n: v for n, v in allowed.items() if draw(st.booleans()) or n == "task"}
    extras = draw(st.dictionaries(_EXTRA_KEY, _EXTRA_VALUE, min_size=1, max_size=6))
    extras = {k: v for k, v in extras.items() if k not in TYPED[kind] and k not in COMMON}
    nested = draw(st.booleans())
    if nested and extras:
        extras["nested"] = {"contents": list(extras.values())}
    event = {"schema": es.PROGRESS_SCHEMA, "at": es.iso(draw(es.times())), "kind": kind, **present, **extras}
    return kind, present, extras, event


def _flatten(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _flatten(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _flatten(v)


@SETTINGS
@given(case=event_with_extras())
def test_p15_append_writes_only_the_typed_fields(tmp_path_factory, case):
    from claude_kiro import progress

    kind, present, extras, event = case
    directory = tmp_path_factory.mktemp("p15")
    env = {"CK_PROGRESS_DIR": str(directory / "progress")}
    key = "demo--20261030T150205Z-%06d" % next(_COUNTER)
    assert progress.append(key, event, env) is None
    path = progress.run_file(key, env)
    assert Path(path).parent == directory / "progress"
    text = Path(path).read_text(encoding="utf-8")
    assert text.endswith("\n") and text.count("\n") == 1
    assert len(text.encode("utf-8")) <= 4096
    row = json.loads(text)
    # only the kind's typed fields (plus schema, at, kind), and every typed field given is kept
    assert set(row) <= set(COMMON) | set(TYPED[kind])
    assert row["kind"] == kind and row["schema"] == es.PROGRESS_SCHEMA
    for name, value in present.items():
        assert row.get(name) == value
    # none of the extra values appear, raw or JSON-escaped
    assert "CONFIDENTIAL" not in text
    for value in _flatten(extras):
        assert value not in text
        assert json.dumps(value)[1:-1] not in text
    for key_name in extras:
        if key_name not in TYPED[kind]:
            assert key_name not in row
