"""Property P-8 over the ETA service (run-followups, requirements 5.5, 5.7, 8.5).

Metamorphic: inserting a closed approval wait of W minutes into a run, with every later event and `now` shifted by W,
shifts the finish p10/p50/p90 by exactly W (same explicit seed), never moves the state from `on-track` to `slipping`,
and adds no slip notification.

The property calls `claude_kiro.eta.service.estimate` in-process over a real tasks.md in a real git repo, a record
store the real collector wrote (`POOL_IMPL`) and real progress files, one per run. Project modules are imported inside
the test body so this file collects before they exist.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import eta_helpers as h
from eta_strategies import (
    EV_APPROVAL_END,
    EV_APPROVAL_START,
    EV_END,
    EV_HAND_LANDING,
    EV_MERGE,
    EV_START,
    events,
    graphs,
)

pytestmark = pytest.mark.usefixtures("no_network")

PROPERTIES = {"P-8": "test_p8_approval_wait_shifts_the_estimate"}

SEED = 7
SLOTS = "4"
THRESHOLD_MIN = 15
STALE_LIMIT_MIN = 120
SPEC = "demo"
WAIT_ID = "5a1d0c0e"
ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"

_SCRATCH: Dict[str, Path] = {}


def _scratch() -> Path:
    """One store (the real collector's POOL_IMPL output) for the whole module, removed at exit."""
    if not _SCRATCH:
        root = Path(tempfile.mkdtemp(prefix="ck-eta-p8-"))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        h.install_pool(h.POOL_IMPL, root / "xdg")
        for name in ("config", "home"):
            (root / name).mkdir()
        _SCRATCH["root"] = root
    return _SCRATCH["root"]


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, ISO_FMT).replace(tzinfo=timezone.utc)


def _shifted(event: Dict[str, Any], minutes: int) -> Dict[str, Any]:
    out = dict(event)
    for key in ("at", "parkedAt"):
        if key in out and isinstance(out[key], str):
            out[key] = (_parse(out[key]) + timedelta(minutes=minutes)).strftime(ISO_FMT)
    return out


def _env(progress_dir: Path) -> Dict[str, str]:
    root = _scratch()
    env = dict(os.environ)
    env.update({
        "XDG_DATA_HOME": str(root / "xdg"), "CK_CONFIG_HOME": str(root / "config"), "HOME": str(root / "home"),
        "CK_AGENT_SLOTS": SLOTS, h.PROGRESS_DIR_ENV: str(progress_dir), "TZ": "UTC",
    })
    env.pop(h.NOW_ENV, None)
    return env


def _write_view(repo: Path, key: str, evs: List[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    """Write `evs` through the real appender into a fresh progress dir, then ask the service for the view."""
    from claude_kiro.eta import service

    progress_dir = Path(tempfile.mkdtemp(prefix="progress-", dir=_scratch()))
    try:
        env = _env(progress_dir)
        errors = h.append_events(env, key, evs)
        assert all(e is None for e in errors), errors
        view = service.estimate(repo, SPEC, now=now, seed=SEED, env=env)
        return view if isinstance(view, dict) else _as_dict(view)
    finally:
        shutil.rmtree(progress_dir, ignore_errors=True)


def _as_dict(view: Any) -> Dict[str, Any]:
    import dataclasses

    if dataclasses.is_dataclass(view):
        return dataclasses.asdict(view)
    return dict(view)


def _finish(view: Dict[str, Any]) -> Optional[Dict[str, datetime]]:
    fin = view.get("finish")
    if fin is None:
        return None
    return {q: datetime.fromisoformat(fin[q]) for q in ("p10", "p50", "p90")}


def _closed_wait_minutes(evs: List[Dict[str, Any]], launch: datetime) -> float:
    """Minutes during which at least one wait was open and has closed (overlaps merged), from the events alone.

    A wait is an approval-start/approval-end pair, or a `hand-landing` (id `hand-<task>`) closed by that task's `merge`.
    """
    opened: Dict[str, datetime] = {}
    spans = []
    for ev in evs:
        at = _parse(ev["at"])
        kind = ev["kind"]
        if kind == EV_APPROVAL_START:
            opened.setdefault(ev["id"], at)
        elif kind == EV_APPROVAL_END and ev["id"] in opened:
            spans.append((opened.pop(ev["id"]), at))
        elif kind == EV_HAND_LANDING:
            opened.setdefault("hand-" + str(ev["task"]), at)
        elif kind == EV_MERGE and "hand-" + str(ev.get("task")) in opened:
            wid = "hand-" + str(ev["task"])
            spans.append((opened.pop(wid), at))
    total, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(spans):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += (cur_e - cur_s).total_seconds() / 60
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += (cur_e - cur_s).total_seconds() / 60
    return total


def _no_hold() -> Any:
    from claude_kiro.eta import decide

    hold_cls = getattr(decide, "Hold", None)
    if hold_cls is not None:
        try:
            return hold_cls(kind="none", reason=None, reason_key=None, held=(), waiting_on_you=(), whole_run=False)
        except TypeError:
            pass
    return SimpleNamespace(kind="none", reason=None, reason_key=None, held=(), waiting_on_you=(), whole_run=False)


def _is_slip(notice: Any) -> bool:
    """True when the notice is a slip notice, by its key or by its NOTICE_TYPES type, whatever the field is named."""
    from claude_kiro.eta import decide

    slip_type = getattr(decide, "NOTICE_TYPES", {}).get("slip")
    for name in ("kind", "notice", "type", "notice_type", "notification_type"):
        value = notice.get(name) if isinstance(notice, dict) else getattr(notice, name, None)
        if value is not None and (value == "slip" or (slip_type is not None and value == slip_type)):
            return True
    return False


def _slip_notices(view: Dict[str, Any], evs: List[Dict[str, Any]], launch_p90_min: Optional[float],
                  spec: str = SPEC) -> List[Any]:
    """The slip notices `decide.notify_decision` would send for this view with nothing sent before."""
    from claude_kiro.eta import decide

    fin = _finish(view)
    launch = _parse(evs[0]["at"])
    p50_min = None if fin is None else (fin["p50"] - launch).total_seconds() / 60
    waited = _closed_wait_minutes(evs, launch)
    ni = SimpleNamespace(
        spec=spec, state=view["state"], p50_finish_min=p50_min, p50=p50_min, waited_min=waited, waited=waited,
        launch_p90_min=launch_p90_min, launch_p90=launch_p90_min, hold=_no_hold(), ended=view.get("ended"),
        view=view, kind=view.get("runKind"),
    )
    return [n for n in decide.notify_decision(ni, (), THRESHOLD_MIN) if _is_slip(n)]


def test_slip_helper_can_see_a_slip():
    """Guard: the helper must report a slip when one is plain, so an unexpected notice shape fails loudly here."""
    start = "2026-01-01T00:00:00Z"
    view = {"state": "slipping", "ended": False, "runKind": "implement",
            "finish": {q: (_parse(start) + timedelta(minutes=m)).strftime(ISO_FMT)
                       for q, m in (("p10", 300), ("p50", 400), ("p90", 500))}}
    evs = [{"at": start, "kind": EV_START}]
    slips = _slip_notices(view, evs, launch_p90_min=100.0)
    assert slips, "notify_decision returned no slip notice for p50 400 vs launch p90 100: notice shape unrecognised"


def _launch_summary(view: Dict[str, Any]) -> Dict[str, Any]:
    return {"finish": [view["finish"][q] for q in ("p10", "p50", "p90")] if view.get("finish") else None,
            "remainingMin": view.get("remainingMin"), "usd": view.get("usd"),
            "confidence": view.get("confidence", "low")}


@st.composite
def shifted_runs(draw):
    graph = draw(graphs(max_tasks=8))
    evs = [e for e in draw(events(graph, max_events=25)) if e["kind"] != EV_END]
    wait = draw(st.integers(1, 240))
    insert_at = draw(st.integers(1, len(evs)))           # the wait opens at the time of evs[insert_at - 1]
    since_last = draw(st.integers(0, STALE_LIMIT_MIN))   # `now` within 120 minutes of the last event (no stale run)
    return graph, evs, wait, insert_at, since_last


@settings(derandomize=True, max_examples=50, database=None, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.filter_too_much])
@given(shifted_runs())
def test_p8_approval_wait_shifts_the_estimate(case):
    graph, evs, wait, insert_at, since_last = case
    root = Path(tempfile.mkdtemp(prefix="repo-", dir=_scratch()))
    try:
        tasks = [h.TaskDef(id=tid, track=g["track"], model=g["model"], risk=None if g["risk"] == "normal" else g["risk"],
                           files=g["files"], criteria=g["criteria"], deps=tuple(g["deps"]))
                 for tid, g in graph.items()]
        repo = h.eta_spec(root / "project", tasks, spec=SPEC)
        key = "demo--20260101T000000Z-" + evs[0]["run"]
        start0 = dict(evs[0], run=key, root=str(repo), mode="workflow", slots=int(SLOTS), slotsSource="runtime",
                      spool=True)
        launch_view = _write_view(repo, key, [start0], _parse(start0["at"]))
        launch_fin = _finish(launch_view)
        launch_p90 = None if launch_fin is None else (launch_fin["p90"] - _parse(start0["at"])).total_seconds() / 60
        start = dict(start0, launch=_launch_summary(launch_view))
        base = [start, *evs[1:]]
        anchor = _parse(base[insert_at - 1]["at"])
        opened = {"schema": start["schema"], "at": anchor.strftime(ISO_FMT), "kind": EV_APPROVAL_START,
                  "id": WAIT_ID, "reason": "question"}
        closed = {"schema": start["schema"], "at": (anchor + timedelta(minutes=wait)).strftime(ISO_FMT),
                  "kind": EV_APPROVAL_END, "id": WAIT_ID}
        with_wait = [*base[:insert_at], opened, closed, *[_shifted(e, wait) for e in base[insert_at:]]]
        now_a = _parse(base[-1]["at"]) + timedelta(minutes=since_last)
        now_b = now_a + timedelta(minutes=wait)

        view_a = _write_view(repo, key, base, now_a)
        view_b = _write_view(repo, key, with_wait, now_b)

        fin_a, fin_b = _finish(view_a), _finish(view_b)
        if fin_a is None or fin_b is None:
            assert fin_a is None and fin_b is None, (view_a["state"], view_b["state"])
        else:
            for q in ("p10", "p50", "p90"):
                assert fin_b[q] - fin_a[q] == timedelta(minutes=wait), (q, fin_a[q], fin_b[q], wait)

        assert not (view_a["state"] == "on-track" and view_b["state"] == "slipping"), (view_a["state"], view_b["state"])

        slips_a = _slip_notices(view_a, base, launch_p90)
        slips_b = _slip_notices(view_b, with_wait, launch_p90)
        assert len(slips_b) <= len(slips_a), (len(slips_a), len(slips_b))
    finally:
        shutil.rmtree(root, ignore_errors=True)
