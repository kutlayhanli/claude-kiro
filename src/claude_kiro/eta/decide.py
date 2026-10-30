"""Pure ETA decision seams (design S5-S12): holds, run state, notifications, line formats, cache and effective time.

No I/O, no clock, no click: every input is an argument, so `statusline.py` can import this module cheaply.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from claude_kiro.progress import effective_min

NOTICE_TYPES = {"slip": "ck_eta_slip", "blocked": "ck_eta_blocked", "done": "ck_run_done"}

STALE_MIN = 120
_FAIL_REASONS = ("gate", "review", "landing", "conflict", "worktree", "other")
_OUTCOMES = ("completed", "partial", "halted", "refused", "failed", "superseded")


# --------------------------------------------------------------------------- types

@dataclass(frozen=True)
class Hold:
    kind: str  # "none" | "blocked"
    reason: Optional[str]
    reason_key: Optional[str]
    held: Tuple[str, ...]
    waiting_on_you: Tuple[str, ...]
    whole_run: bool


@dataclass(frozen=True)
class SentNotice:
    kind: str  # "slip" | "blocked" | "done"
    key: str
    level: Optional[int] = None


@dataclass(frozen=True)
class NotifyInput:
    spec: str
    state: str
    p50_finish_min: Optional[float]
    waited_min: float
    launch_p90_min: Optional[float]
    hold: Hold
    cause: Optional[str]
    outcome: Optional[str]
    merged: Optional[int]
    total: Optional[int]
    wall_min: Optional[float]
    usd: Optional[float]
    now_hhmm: str


@dataclass(frozen=True)
class Notice:
    kind: str
    level: Optional[int]
    key: str
    type: str
    source: NotifyInput


@dataclass(frozen=True)
class StatusInput:
    spec: str
    run_kind: str  # "implement" | "create"
    merged: int = 0
    total: int = 0
    phase: Optional[str] = None
    view: Optional[Mapping[str, Any]] = None  # the cached EtaView, None when there is no cache


@dataclass(frozen=True)
class EventsDigest:
    size: int
    lines: int
    last_hash: str

    def as_dict(self) -> Dict[str, Any]:
        return {"size": self.size, "lines": self.lines, "lastHash": self.last_hash}


# --------------------------------------------------------------------------- S5 classify_hold

def _idkey(t: str) -> Tuple[int, int, str]:
    return (0, int(t), t) if t.isdigit() else (1, 0, t)


def _sorted(ids: Any) -> List[str]:
    return sorted(set(ids), key=_idkey)


def _dependents(deps: Mapping[str, Sequence[str]], seeds: Set[str]) -> Set[str]:
    out = set(seeds)
    changed = True
    while changed:
        changed = False
        for t, ds in deps.items():
            if t not in out and any(d in out for d in ds):
                out.add(t)
                changed = True
    return out


def classify_hold(graph: Any, state: Any) -> Hold:
    deps = {t: tuple(graph.deps.get(t, ())) for t in graph.order}
    merged = {t for t, p in state.tasks.items() if p.status == "merged"}
    finished = set(graph.done) | merged
    parked = [t for t in graph.order if t in state.tasks and state.tasks[t].status == "parked"]
    failed = [t for t in graph.order if t in state.tasks and state.tasks[t].status == "failed"]

    segments: List[str] = []
    keys: List[str] = []
    held: Set[str] = set()
    waiting: Set[str] = set()

    if parked:
        dep = _dependents(deps, set(parked))
        held |= dep
        waiting |= dep
        for t in parked:
            segments.append("task %s parked (needs input)" % t)
            keys.append("park:%s" % t)

    seeds: Set[str] = set()
    for group in graph.cycles:
        ids = _sorted(group)
        seeds |= set(ids)
        segments.append("dependency cycle %s" % ", ".join(ids))
        keys.append("cycle:%s" % ",".join(ids))
    for t, unknown in sorted(graph.unknown_deps.items(), key=lambda kv: _idkey(kv[0])):
        for u in unknown:
            seeds.add(t)
            segments.append("task %s depends on unknown task %s" % (t, u))
            keys.append("unknown:%s>%s" % (t, u))
    if seeds:
        held |= _dependents(deps, seeds)

    if failed:
        remaining = [t for t in graph.order if t not in finished and t not in failed]
        blocked_by_fail = _dependents(deps, set(failed)) - set(failed)
        if remaining and all(t in blocked_by_fail for t in remaining):
            held |= set(remaining)
            for t in failed:
                segments.append("task %s failed" % t)
                keys.append("failed:%s" % t)

    held -= finished
    waiting -= finished
    if not segments:
        return Hold("none", None, None, (), (), False)

    reason_tasks = {t for t in parked} | seeds | set(failed)
    extra = [t for t in _sorted(held) if t not in reason_tasks]
    reason = "; ".join(segments)
    if extra:
        reason += "; holds %s" % ", ".join(extra)
    held_sorted = _sorted(held)
    runnable = [t for t in graph.order if t not in held and t not in finished and t not in failed]
    key = "|".join(sorted(keys)) + "|held:" + ",".join(held_sorted)
    return Hold("blocked", reason, key, tuple(held_sorted), tuple(_sorted(waiting)), not runnable)


# --------------------------------------------------------------------------- S6 decide_state

def decide_state(*, ended: bool, started: bool, stale: bool, hold: Hold, wait_open: bool,
                 p50_finish_min: Optional[float], waited_min: float, launch_p90_min: Optional[float]) -> str:
    if ended:
        return "done"
    if stale:
        return "stale"
    if hold.kind == "blocked":
        return "blocked"
    if not started:
        return "not-started"
    if wait_open:
        return "waiting"
    if p50_finish_min is not None and launch_p90_min is not None and p50_finish_min - waited_min > launch_p90_min:
        return "slipping"
    return "on-track"


# --------------------------------------------------------------------------- S7 notify_decision

def _slip_level(view: NotifyInput, threshold_min: int) -> int:
    if view.p50_finish_min is None or view.launch_p90_min is None or threshold_min <= 0:
        return 0
    slip = view.p50_finish_min - view.waited_min - view.launch_p90_min
    if slip <= 0:
        return 0
    return max(0, int(math.ceil(slip / threshold_min)) - 1)


def _sent_kind(s: Any) -> Optional[str]:
    return getattr(s, "kind", None) or getattr(s, "notice", None)


def notify_decision(view: NotifyInput, sent: Sequence[Any], threshold_min: int) -> List[Notice]:
    out: List[Notice] = []
    if view.state == "done" or view.outcome is not None:
        if not any(_sent_kind(s) == "done" for s in sent):
            out.append(Notice("done", None, "done", NOTICE_TYPES["done"], view))
        return out
    if view.state in ("waiting", "stale", "not-started"):
        return out
    if view.state == "blocked":
        hold = view.hold
        if hold.kind == "blocked" and not hold.waiting_on_you and hold.reason_key:
            if not any(_sent_kind(s) == "blocked" and s.key == hold.reason_key for s in sent):
                out.append(Notice("blocked", None, hold.reason_key, NOTICE_TYPES["blocked"], view))
        return out
    level = _slip_level(view, threshold_min)
    best = max([s.level or 0 for s in sent if _sent_kind(s) == "slip"] or [0])
    if level >= 1 and level > best:
        out.append(Notice("slip", level, "slip", NOTICE_TYPES["slip"], view))
    return out


# --------------------------------------------------------------------------- S8 notice_text

_ID = r"[0-9A-Za-z]{1,16}"
_IDS = r"%s(?:, %s)*" % (_ID, _ID)
_REASON_PART = re.compile(
    r"task %(i)s parked \(needs input\)"
    r"|dependency cycle %(l)s"
    r"|task %(i)s depends on unknown task %(i)s"
    r"|task %(i)s failed(?: \((?:%(f)s)\))?"
    r"|holds %(l)s" % {"i": _ID, "l": _IDS, "f": "|".join(_FAIL_REASONS)})
_CAUSE = re.compile(
    r"task %s(?: (?:running|start|merged|failed|parked|unparked|waiting for hand landing"
    r"|(?:retry|escalation|revision|landfix|resolve)(?: \d{1,3})?))?(?: \((?:%s)\))?"
    % (_ID, "|".join(_FAIL_REASONS)))


def _clean_reason(reason: Optional[str]) -> str:
    parts = [m.group(0) for m in _REASON_PART.finditer(reason or "")]
    return "; ".join(parts) if parts else "run held"


def _clean_cause(cause: Optional[str]) -> str:
    if cause and _CAUSE.fullmatch(cause):
        return cause
    return "progress behind the launch estimate"


def _add_minutes(hhmm: str, minutes: float) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", hhmm or "")
    if not m:
        return "?"
    total = (int(m.group(1)) * 60 + int(m.group(2)) + int(round(minutes))) % (24 * 60)
    return "%02d:%02d" % (total // 60, total % 60)


def notice_text(n: Notice) -> str:
    v = n.source
    spec = v.spec
    if n.kind == "blocked":
        return "ck: %s blocked: %s" % (spec, _clean_reason(v.hold.reason))
    if n.kind == "done":
        word = v.outcome if v.outcome in _OUTCOMES and v.outcome != "completed" else "done"
        if v.merged is not None and v.total is not None:
            body = "%d/%d merged" % (v.merged, v.total)
        else:
            body = "spec written"
        text = "ck: %s %s %s: %s, %d min" % (spec, word, v.now_hhmm, body, int(round(v.wall_min or 0)))
        if v.usd is not None:
            text += ", $%.2f" % v.usd
        return text
    p50 = v.p50_finish_min or 0.0
    slip = p50 - v.waited_min - (v.launch_p90_min or 0.0)
    eta = _add_minutes(v.now_hhmm, p50 - (v.wall_min or 0.0))
    return "ck: %s ETA %s (+%d min, %s)" % (spec, eta, int(round(slip)), _clean_cause(v.cause))


# --------------------------------------------------------------------------- S9 status_lines

def _hhmm(iso: Any) -> Optional[str]:
    if not isinstance(iso, str):
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        return None


def _p50_time(view: Optional[Mapping[str, Any]]) -> Optional[str]:
    fin = (view or {}).get("finish")
    return _hhmm(fin.get("p50")) if isinstance(fin, Mapping) else None


def _waiting_min(view: Mapping[str, Any]) -> Optional[int]:
    w = view.get("waitingOnYouMinutes")
    return int(round(w)) if isinstance(w, (int, float)) and not isinstance(w, bool) else None


def status_lines(runs: Sequence[StatusInput]) -> List[str]:
    lines = []
    for r in runs:
        view = r.view
        if view is None:
            eta = "ETA ?"
        elif view.get("state") == "blocked":
            eta = "blocked"
        elif view.get("state") == "waiting":
            w = _waiting_min(view)
            eta = "waiting on you %d min" % (w or 0)
        else:
            t = _p50_time(view)
            eta = "ETA %s" % t if t else "ETA ?"
        if r.run_kind == "create":
            lines.append("ck %s create %s %s" % (r.spec, r.phase or "?", eta))
            continue
        line = "ck %s %d/%d %s" % (r.spec, r.merged, r.total, eta)
        cost = (view or {}).get("cost")
        p50 = cost.get("p50") if isinstance(cost, Mapping) else None
        if isinstance(p50, (int, float)) and not isinstance(p50, bool):
            line += " ~$%d" % int(round(p50))
        lines.append(line)
    return lines


# --------------------------------------------------------------------------- S10 eta_log_line

def _parse_iso(s: Any) -> Optional[datetime]:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def eta_log_line(what: str, view: Mapping[str, Any], prev_p50: Optional[str]) -> str:
    fin = view.get("finish") if isinstance(view.get("finish"), Mapping) else None
    state = view.get("state")
    if what in ("", "start", "launch", None):
        t50 = _hhmm(fin.get("p50")) if fin else None
        t90 = _hhmm(fin.get("p90")) if fin else None
        head = "ETA %s (p90 %s)" % (t50, t90) if t50 and t90 else "ETA ?"
        if view.get("runKind") == "create":
            return "%s, %d phases" % (head, len(view.get("phases") or []))
        crit = (view.get("criticalPath") or {}).get("tasks") or []
        return "%s, critical %s" % (head, "→".join(str(c) for c in crit)) if crit else head
    if state == "blocked":
        return "%s · blocked" % what
    w = _waiting_min(view)
    if state == "waiting" or w is not None:
        return "%s · waiting on you %d min" % (what, w or 0)
    t50 = _hhmm(fin.get("p50")) if fin else None
    if t50 is None and view.get("open") == 0 and view.get("runKind") != "create":
        t50 = _hhmm(view.get("now"))  # nothing left to run: it finishes now
    line = "%s · ETA %s" % (what, t50 or "?")
    cur, prev = _parse_iso(fin.get("p50")) if fin else None, _parse_iso(prev_p50)
    if cur and prev:
        try:
            delta = (cur - prev).total_seconds() / 60.0
        except TypeError:
            delta = 0.0
        if abs(delta) >= 1:
            line += " (%s%d)" % ("+" if delta > 0 else "-", int(round(abs(delta))))
    return line


# --------------------------------------------------------------------------- S11 cache_fresh

def cache_fresh(cache: Optional[Mapping[str, Any]], digest: EventsDigest, model_digest: str) -> bool:
    if not isinstance(cache, Mapping):
        return False
    d = cache.get("digest")
    if isinstance(d, EventsDigest):
        d = d.as_dict()
    if not isinstance(d, Mapping):
        return False
    return dict(d) == digest.as_dict() and cache.get("modelDigest") == model_digest


# --------------------------------------------------------------------------- S12 effective_now

def effective_now(state: Any, now_min: float) -> Tuple[float, float]:
    """`(now_eff, waited_min)`: frozen while a wait is open (the open wait counts up to `now`)."""
    now_eff = effective_min(state.waits, now_min)
    return now_eff, now_min - now_eff
