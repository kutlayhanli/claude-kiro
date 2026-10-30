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
BUNDLED_PARTS = ("review wait", "size factor", "phase durations", "cost rate")

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
    (duplicated deps, dangling multi-digit ids), then with some probability a back edge (cycle) or a self-loop."""
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
    if len(ids) > 1 and draw(st.integers(0, 3)) == 0:
        lo, hi = sorted(draw(st.lists(st.sampled_from(ids), min_size=2, max_size=2, unique=True)),
                        key=lambda x: int(x))
        graph[lo]["deps"].append(hi)  # back edge: lower id depends on a higher one
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

    add(EV_START, run="r" + draw(st.text("0123456789abcdef", min_size=6, max_size=6)), runKind="implement",
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
            add(kind, task=task, parkKind="needs-input", parkedAt=iso(clock))
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
               '{"schema": "ck-progress/9", "kind": "start"}', '{"schema": "ck-progress/1"}',
               '{"schema": "ck-progress/1", "kind": 7}', '{"kind": "merge", "task": "1"}')


@st.composite
def malformed_lines(draw) -> str:
    """One progress-file line that is not a valid event: junk, wrong schema, non-object JSON, truncated JSON,
    or an oversized object."""
    choice = draw(st.integers(0, 3))
    if choice == 0:
        return draw(st.sampled_from(_JUNK_LINES))
    if choice == 1:
        good = json.dumps({"schema": PROGRESS_SCHEMA, "at": iso(0), "kind": EV_MERGE, "task": "1"})
        return good[: draw(st.integers(1, len(good) - 1))]
    if choice == 2:
        return draw(st.text(alphabet=st.characters(blacklist_characters="\n\r"), max_size=60))
    pad = "x" * draw(st.integers(4097, 6000))
    return json.dumps({"schema": PROGRESS_SCHEMA, "at": iso(0), "kind": EV_MERGE, "task": "1", "pad": pad})


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
