"""Name constants, Hypothesis strategies and small helpers for the ETA tests (run-followups).

Test-owned and project-free: nothing here imports a `claude_kiro` module. Command and environment names live in
`tests/eta_helpers.py`. Every event, schema and model name is spelled once, as a constant below.
"""

import contextlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest
from hypothesis import strategies as st

from landing_bridge import dags

# --- schema and size names --------------------------------------------------------------------

PROGRESS_SCHEMA = "ck-progress/1"
ETA_SCHEMA_RE = r"^ck-eta/\d+$"
DEFAULTS_SCHEMA = "ck-eta-defaults/1"
SIZE_KEYS = ("filesCount", "criteriaCount")
STALE_MIN = 120

# --- models, tracks, risks --------------------------------------------------------------------

MODEL_SONNET = "sonnet"
MODEL_OPUS = "opus"
MODEL_HAIKU = "haiku"
MODEL_INHERIT = "inherit"
MODELS = (MODEL_SONNET, MODEL_OPUS, MODEL_HAIKU)
TRACKS = ("impl", "test")
RISKS = ("normal", "safety")

# --- match ladder and bundled parts -----------------------------------------------------------

MATCH_STEPS = ("track+model+risk", "track+risk", "track", "all", "bundled", "none")
# Design 7.3 names only "review wait" and "size factor" (design.md example). Task 12 must reconcile the full set of
# part names it reports (e.g. landing wait, task durations) with this constant.
BUNDLED_PARTS = ("review wait", "size factor")

# --- progress event kinds ---------------------------------------------------------------------

EV_START = "start"
EV_TASK_START = "task-start"
EV_RETRY = "retry"
EV_ESCALATION = "escalation"
EV_REVISION = "revision"
EV_LANDFIX = "landfix"
EV_RESOLVE = "resolve"
EV_MERGE = "merge"
EV_LANDING = "landing"
EV_TASK_FAILED = "task-failed"
EV_HAND_LANDING = "hand-landing"
EV_PARK = "park"
EV_UNPARK = "unpark"
EV_APPROVAL_START = "approval-start"
EV_APPROVAL_END = "approval-end"
EV_PHASE_START = "phase-start"
EV_PHASE_END = "phase-end"
EV_STEP_END = "step-end"
EV_END = "end"
EV_NOTIFY_PENDING = "notify-pending"
EV_NOTIFIED = "notified"

CLAIM_EVENTS = (EV_TASK_START, EV_RETRY, EV_ESCALATION, EV_REVISION, EV_LANDFIX, EV_RESOLVE)
IMPLEMENT_EVENTS = (
    EV_START, *CLAIM_EVENTS, EV_MERGE, EV_LANDING, EV_TASK_FAILED, EV_HAND_LANDING, EV_PARK, EV_UNPARK,
    EV_APPROVAL_START, EV_APPROVAL_END, EV_END, EV_NOTIFY_PENDING, EV_NOTIFIED,
)
CREATE_EVENTS = (
    EV_START, EV_PHASE_START, EV_PHASE_END, EV_STEP_END, EV_APPROVAL_START, EV_APPROVAL_END, EV_END,
    EV_NOTIFY_PENDING, EV_NOTIFIED,
)

RUN_KIND_IMPLEMENT = "implement"
PARK_KIND_NEEDS_INPUT = "needs-input"
EV_UNKNOWN = "warp"
ALL_EVENTS = tuple(dict.fromkeys((*IMPLEMENT_EVENTS, *CREATE_EVENTS)))

NO_NETWORK = pytest.mark.usefixtures("no_network")

_SPEC_CREATE_JS = Path(__file__).resolve().parent.parent / "plugin" / "workflows" / "spec-create.js"


def _read_create_phases() -> tuple:
    text = _SPEC_CREATE_JS.read_text(encoding="utf-8")
    block = text[text.index("phases:"):]
    block = block[:block.index("\n  ],")]
    return tuple(re.findall(r"title:\s*'([^']+)'", block))


CREATE_PHASES = _read_create_phases()

# --- strategies -------------------------------------------------------------------------------

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def iso(minutes: float) -> str:
    """ISO-8601 UTC second-precision stamp `minutes` after a fixed epoch."""
    return (_EPOCH + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def times():
    """Minute offsets in [0, 600]."""
    return st.integers(0, 600)


@st.composite
def graphs(draw, max_tasks: int = 15) -> Dict[str, Dict[str, Any]]:
    """{id: {deps, track, model, risk, files, criteria}} in tasks.md order. Built on `landing_bridge.dags()`
    (duplicated deps, dangling multi-digit ids), then with some probability a cycle (the reverse of an existing edge) or a self-loop."""
    deps = draw(dags(max_tasks=max_tasks))
    ids = list(deps)
    graph: Dict[str, Dict[str, Any]] = {}
    for tid in ids:
        graph[tid] = {
            "deps": list(deps[tid]),
            "track": draw(st.sampled_from(TRACKS)),
            "model": draw(st.sampled_from(MODELS)),
            "risk": draw(st.sampled_from(RISKS)),
            "files": draw(st.integers(0, 12)),
            "criteria": draw(st.integers(0, 12)),
        }
    edges = [(t, d) for t in ids for d in graph[t]["deps"] if d in graph and d != t]
    if edges and draw(st.integers(0, 3)) == 0:
        t, d = draw(st.sampled_from(edges))
        graph[d]["deps"].append(t)  # reverse of an existing edge: a guaranteed 2-cycle
    if draw(st.integers(0, 5)) == 0:
        tid = draw(st.sampled_from(ids))
        graph[tid]["deps"].append(tid)  # self-loop
    return graph


@st.composite
def events(draw, graph: Optional[Dict[str, Dict[str, Any]]] = None, max_events: int = 40) -> List[Dict[str, Any]]:
    """A time-ordered progress-event sequence consistent with `graph`: a `start`, then claims, parks,
    `hand-landing`, approval waits, merges and `task-failed`, ending with an optional `end`."""
    if graph is None:
        graph = draw(graphs(max_tasks=8))
    ids = list(graph)
    out: List[Dict[str, Any]] = []
    clock = draw(st.integers(0, 60))

    def add(kind: str, **fields: Any) -> None:
        nonlocal clock
        clock += draw(st.integers(0, 30))
        out.append({"schema": PROGRESS_SCHEMA, "at": iso(clock), "kind": kind, **fields})

    add(EV_START, run="r" + draw(st.text("0123456789abcdef", min_size=6, max_size=6)), runKind=RUN_KIND_IMPLEMENT,
        spec="demo", open=ids, total=len(ids))
    started: List[str] = []
    waits: List[str] = []
    for _ in range(draw(st.integers(0, max_events))):
        kind = draw(st.sampled_from((*CLAIM_EVENTS, EV_MERGE, EV_PARK, EV_UNPARK, EV_HAND_LANDING, EV_TASK_FAILED,
                                     EV_APPROVAL_START, EV_APPROVAL_END)))
        task = draw(st.sampled_from(ids))
        if kind in CLAIM_EVENTS:
            started.append(task)
            add(kind, task=task, attempt=draw(st.one_of(st.none(), st.integers(1, 4))),
                model=draw(st.one_of(st.none(), st.sampled_from((*MODELS, MODEL_INHERIT)))))
        elif kind == EV_MERGE:
            add(kind, task=task)
        elif kind == EV_PARK:
            add(kind, task=task, parkKind=PARK_KIND_NEEDS_INPUT, parkedAt=iso(clock))
        elif kind == EV_UNPARK:
            add(kind, task=task)
        elif kind == EV_HAND_LANDING:
            add(kind, task=task)
        elif kind == EV_TASK_FAILED:
            add(kind, task=task, reason=draw(st.sampled_from(("gate", "review", "landing", "conflict", "other"))))
        elif kind == EV_APPROVAL_START:
            wid = draw(st.text("0123456789abcdef", min_size=8, max_size=8))
            waits.append(wid)
            add(kind, id=wid, reason="question")
        elif waits:
            add(kind, id=waits.pop())
    if draw(st.booleans()):
        add(EV_END, outcome=draw(st.sampled_from(("completed", "partial", "halted", "failed"))),
            merged=None, total=len(ids), usd=None)
    return out


_JUNK_LINES = ("", "   ", "{", "}", "[]", "null", "42", '"text"', "{\"kind\": }", "not json", "\x00\x01",
               json.dumps({"schema": "ck-progress/9", "kind": EV_START}),
               json.dumps({"schema": PROGRESS_SCHEMA}),
               json.dumps({"schema": PROGRESS_SCHEMA, "kind": 7}),
               json.dumps({"kind": EV_MERGE, "task": "1"}),
               json.dumps({"schema": PROGRESS_SCHEMA, "at": iso(0), "kind": EV_UNKNOWN, "task": "1"}))


@st.composite
def malformed_lines(draw) -> str:
    """One progress-file line (no line ending) that is not a valid event: junk, wrong schema, unknown kind,
    non-object JSON, truncated JSON, or an oversized (over 4 KiB) run of non-JSON / truncated JSON."""
    choice = draw(st.integers(0, 4))
    if choice == 0:
        return draw(st.sampled_from(_JUNK_LINES))
    if choice == 1:
        good = json.dumps({"schema": PROGRESS_SCHEMA, "at": iso(0), "kind": EV_MERGE, "task": "1"})
        return good[: draw(st.integers(1, len(good) - 1))]
    if choice == 2:
        return draw(st.text(alphabet=st.characters(blacklist_characters="\n\r"), max_size=60))
    if choice == 3:
        return "x" * draw(st.integers(4097, 6000))
    head = json.dumps({"schema": PROGRESS_SCHEMA, "at": iso(0), "kind": EV_MERGE, "task": "1"})[:-1]
    return head + ', "text": "' + "x" * draw(st.integers(4097, 6000))  # cut off mid-string: never closes


@st.composite
def malformed_raw(draw) -> bytes:
    """One malformed progress-file line as raw bytes, including a line terminator: every `malformed_lines()`
    form plus NUL bytes, invalid UTF-8, and CRLF endings. Write these to the file in binary mode."""
    kind = draw(st.integers(0, 3))
    if kind == 0:
        return draw(malformed_lines()).encode("utf-8") + b"\n"
    if kind == 1:
        return draw(malformed_lines()).encode("utf-8") + b"\r\n"
    if kind == 2:
        return b"\x00" * draw(st.integers(1, 8)) + draw(st.binary(max_size=20)).replace(b"\n", b"") + b"\n"
    bad = draw(st.sampled_from((b"\xff\xfe", b"\xc3\x28", b"\x80", b"\xf0\x28\x8c\x28")))
    return b'{"schema": "' + PROGRESS_SCHEMA.encode() + b'", "note": "' + bad + b'"}\n'


# --- helpers ----------------------------------------------------------------------------------

def progress_lines(path) -> List[Dict[str, Any]]:
    """The test's own JSON-lines reader (not `progress.read_events`): every non-blank line that parses to an
    object, in file order."""
    p = Path(path)
    if not p.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


@contextlib.contextmanager
def busy(n: int) -> Iterator[List[subprocess.Popen]]:
    """Start `n` CPU-bound child processes; kill them on exit."""
    procs = [subprocess.Popen([sys.executable, "-c", "while True: pass"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL) for _ in range(n)]
    try:
        yield procs
    finally:
        for p in procs:
            with contextlib.suppress(Exception):
                p.kill()
        for p in procs:
            with contextlib.suppress(Exception):
                p.wait(timeout=5)
