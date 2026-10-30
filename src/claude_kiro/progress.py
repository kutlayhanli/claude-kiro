"""The run progress file (`ck-progress/1`): paths, run keys, locked append, tolerant read and the pure fold.

This module is the single owner of the file. It uses the standard library plus `analytics.store` for the data
directory, and does not import click, so the status line can use it.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from claude_kiro.analytics import store

SCHEMA = "ck-progress/1"
STALE_MIN = 120
MAX_LINE_BYTES = 4096
ERROR_LIMIT = 200

NOTICE_TYPES = {"slip": "ck_eta_slip", "blocked": "ck_eta_blocked", "done": "ck_run_done"}

_CLAIM = {"task": "task", "attempt": "attempt", "model": "model"}
_CLAIM_KINDS = ("task-start", "retry", "escalation", "revision", "landfix", "resolve")
_FIELDS: Dict[str, Tuple[str, ...]] = {
    "start": ("run", "runKind", "spec", "root", "mode", "slots", "slotsSource", "maxConcurrent", "open", "total",
              "launch", "model", "modelDigest", "spool"),
    "merge": ("task",),
    "landing": ("tasks", "merged", "red", "conflict", "protected", "green"),
    "task-failed": ("task", "reason"),
    "hand-landing": ("task",),
    "park": ("task", "parkKind", "parkedAt"),
    "unpark": ("task",),
    "approval-start": ("id", "reason"),
    "approval-end": ("id",),
    "phase-start": ("phase",),
    "phase-end": ("phase",),
    "step-end": ("phase",),
    "end": ("outcome", "merged", "total", "usd"),
    "notify-pending": ("notice", "key", "message"),
    "notified": ("notice", "key", "level", "outcome", "detail"),
}
for _k in _CLAIM_KINDS:
    _FIELDS[_k] = tuple(_CLAIM)
EVENT_KINDS = tuple(_FIELDS)
_COMMON = ("schema", "at", "kind")


# --------------------------------------------------------------------------- clock and paths

def _environ(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def _parse(at: Any) -> Optional[datetime]:
    if not isinstance(at, str):
        return None
    text = at.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clock_now(env: Optional[Mapping[str, str]] = None) -> datetime:
    """`CK_NOW` (ISO-8601 UTC) when set and valid, else the current UTC time."""
    raw = _environ(env).get("CK_NOW")
    if raw:
        dt = _parse(raw)
        if dt is not None:
            return dt
    return datetime.now(timezone.utc)


def progress_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    e = _environ(env)
    d = Path(e["CK_PROGRESS_DIR"]) if e.get("CK_PROGRESS_DIR") else store.data_dir(env) / "progress"
    store._mkdir_private(d)
    return d


def new_run_key(spec: str, now: datetime, token: Optional[str] = None) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{spec}--{stamp}-{token or secrets.token_hex(3)}"


def _check_key(key: str) -> str:
    if not key or "/" in key or "\\" in key or "\x00" in key or key.startswith("."):
        raise ValueError(f"bad run key: {key!r}")
    return key


def run_file(key: str, env: Optional[Mapping[str, str]] = None) -> Path:
    return progress_dir(env) / f"{_check_key(key)}.jsonl"


# --------------------------------------------------------------------------- append

def _record_error(key: str, kind: str, exc: BaseException, env: Optional[Mapping[str, str]]) -> None:
    try:
        path = progress_dir(env) / f"{_check_key(key)}.errors"
        msg = " ".join(str(exc).split())[:ERROR_LIMIT]
        line = f"{_iso(clock_now(env))} {kind} {type(exc).__name__}: {msg}\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 - best effort
        pass


def _append_line(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.write(fd, data)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def append(key: str, event: Mapping[str, Any], env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Append one event; return None on success, else the error text. Never raises."""
    kind = event.get("kind") if isinstance(event, Mapping) else None
    try:
        if kind not in _FIELDS:
            raise ValueError(f"unknown event kind: {kind!r}")
        row: Dict[str, Any] = {"schema": event.get("schema") or SCHEMA,
                               "at": event.get("at") or _iso(clock_now(env)), "kind": kind}
        for name in _FIELDS[kind]:
            if name in event:
                row[name] = event[name]
        data = (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")
        if len(data) > MAX_LINE_BYTES:
            raise ValueError(f"event is {len(data)} bytes, over the {MAX_LINE_BYTES} byte limit")
        _append_line(run_file(key, env), data)
        return None
    except (OSError, ValueError, TypeError) as exc:
        _record_error(key, str(kind), exc, env)
        return f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - never raises
        _record_error(key, str(kind), exc, env)
        return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- read

def read_events(path: Path) -> Tuple[List[dict], int]:
    """(valid events in order, number of lines skipped). Malformed, unknown-schema and unknown-kind lines count."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return [], 0
    events: List[dict] = []
    skipped = 0
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    for line in lines:
        try:
            obj = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            skipped += 1
            continue
        if (isinstance(obj, dict) and obj.get("schema") == SCHEMA and isinstance(obj.get("kind"), str)
                and obj["kind"] in _FIELDS):
            events.append(obj)
        else:
            skipped += 1
    return events, skipped


# --------------------------------------------------------------------------- fold

@dataclass(frozen=True)
class SentNotice:
    notice: str
    key: str
    level: Optional[int] = None


@dataclass(frozen=True)
class TaskProgress:
    task: str
    status: str  # pending | running | merged | failed | parked | hand
    step: Optional[str]
    model: Optional[str]
    attempt: Optional[int]
    first_start_min: Optional[float]
    last_start_min: Optional[float]


@dataclass(frozen=True)
class RunState:
    run: Optional[str]
    run_kind: Optional[str]
    spec: Optional[str]
    launch_at: Optional[str]
    launch: Optional[dict]
    tasks: Dict[str, TaskProgress]
    phases_done: Tuple[str, ...]
    phase_running: Optional[str]
    phase_started_min: Optional[float]
    approval_open: Optional[Tuple[str, float]]
    waits: Tuple[Tuple[float, Optional[float]], ...]
    waited_min: float
    last_event_min: Optional[float]
    last_event: Optional[Tuple[str, str]]
    cause: Optional[str]
    ended: Optional[dict]
    sent: Tuple[SentNotice, ...]
    skipped: int


def _union_length(intervals: Sequence[Tuple[float, float]]) -> float:
    total = 0.0
    cur_s: Optional[float] = None
    cur_e = 0.0
    for s, e in sorted(intervals):
        if e <= s:
            continue
        if cur_s is None:
            cur_s, cur_e = s, e
        elif s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    if cur_s is not None:
        total += cur_e - cur_s
    return total


def effective_min(waits: Sequence[Tuple[float, Optional[float]]], t: float) -> float:
    """`t` minus the wait minutes before it (open waits count up to `t`; overlaps counted once)."""
    clipped = []
    for s, e in waits:
        end = t if e is None else min(e, t)
        if end > s:
            clipped.append((s, end))
    return t - _union_length(clipped)


def _int_or_none(v: Any) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def fold(events: Sequence[Mapping[str, Any]]) -> RunState:
    run = run_kind = spec = launch_at = None
    launch: Optional[dict] = None
    base: Optional[datetime] = None
    tasks: Dict[str, TaskProgress] = {}
    phases_done: List[str] = []
    phase_running: Optional[str] = None
    phase_started: Optional[float] = None
    waits: List[List[Any]] = []  # [id, start, end|None]
    last_event_min = None
    last_event: Optional[Tuple[str, str]] = None
    cause: Optional[str] = None
    ended: Optional[dict] = None
    sent: List[SentNotice] = []
    skipped = 0

    def put(task: str, **changes: Any) -> None:
        cur = tasks.get(task) or TaskProgress(task, "pending", None, None, None, None, None)
        d = dict(cur.__dict__)
        d.update(changes)
        tasks[task] = TaskProgress(**d)

    def open_wait(wid: str, minute: float) -> None:
        if not any(w[0] == wid and w[2] is None for w in waits):
            waits.append([wid, minute, None])

    def close_wait(wid: str, minute: float) -> None:
        for w in waits:
            if w[0] == wid and w[2] is None:
                w[2] = max(minute, w[1])
                return

    for ev in events:
        if not isinstance(ev, Mapping) or ev.get("kind") not in _FIELDS:
            skipped += 1
            continue
        kind = ev["kind"]
        at = _parse(ev.get("at"))
        if base is None and at is not None:
            base = at
            if kind == "start":
                launch_at = ev.get("at")
        if kind == "start" and launch_at is None and at is not None:
            launch_at = ev.get("at")
        minute = ((at - base).total_seconds() / 60.0) if (at is not None and base is not None) else (
            last_event_min or 0.0)
        task = str(ev["task"]) if ev.get("task") is not None else None
        desc = kind

        if kind == "start":
            run, run_kind, spec = ev.get("run"), ev.get("runKind"), ev.get("spec")
            launch = ev.get("launch") if isinstance(ev.get("launch"), dict) else None
            for tid in ev.get("open") or []:
                if run_kind != "create" and isinstance(tid, (str, int)):
                    put(str(tid))
            desc = "run started"
        elif kind in _CLAIM_KINDS and task is not None:
            cur = tasks.get(task)
            put(task, status="running", step="first" if kind == "task-start" else kind,
                model=ev.get("model"), attempt=_int_or_none(ev.get("attempt")),
                first_start_min=cur.first_start_min if cur and cur.first_start_min is not None else minute,
                last_start_min=minute)
            desc = f"task {task} {'start' if kind == 'task-start' else kind}"
            if kind != "task-start" and _int_or_none(ev.get("attempt")) is not None:
                desc += f" {ev['attempt']}"
            cause = desc
        elif kind == "merge" and task is not None:
            put(task, status="merged")
            close_wait(f"hand-{task}", minute)
            desc = f"task {task} merged"
        elif kind == "task-failed" and task is not None:
            put(task, status="failed")
            desc = f"task {task} failed"
            cause = f"{desc} ({ev.get('reason')})" if ev.get("reason") else desc
        elif kind == "hand-landing" and task is not None:
            put(task, status="hand")
            open_wait(f"hand-{task}", minute)
            desc = f"task {task} waiting for hand landing"
            cause = desc
        elif kind == "park" and task is not None:
            put(task, status="parked")
            desc = f"task {task} parked"
            cause = desc
        elif kind == "unpark" and task is not None:
            cur = tasks.get(task)
            put(task, status="running" if cur and cur.first_start_min is not None else "pending")
            desc = f"task {task} unparked"
            cause = desc
        elif kind == "approval-start" and ev.get("id") is not None:
            open_wait(str(ev["id"]), minute)
            desc = "waiting for approval"
        elif kind == "approval-end" and ev.get("id") is not None:
            close_wait(str(ev["id"]), minute)
            desc = "approval given"
        elif kind == "phase-start" and isinstance(ev.get("phase"), str):
            phase_running, phase_started = ev["phase"], minute
            desc = f"phase {ev['phase']} started"
        elif kind in ("phase-end", "step-end") and isinstance(ev.get("phase"), str):
            if ev["phase"] not in phases_done:
                phases_done.append(ev["phase"])
            if phase_running == ev["phase"]:
                phase_running, phase_started = None, None
            desc = f"phase {ev['phase']} done"
        elif kind == "end":
            ended = dict(ev)
            for w in waits:
                if w[2] is None:
                    w[2] = max(minute, w[1])
            desc = f"run ended ({ev.get('outcome')})"
        elif kind in ("notified", "notify-pending"):
            sent.append(SentNotice(str(ev.get("notice")), str(ev.get("key")), _int_or_none(ev.get("level"))))
            desc = f"{ev.get('notice')} notice"

        last_event_min = minute
        last_event = (str(ev.get("at")), desc)

    closed = [(w[1], w[2]) for w in waits if w[2] is not None]
    open_waits = [w for w in waits if w[2] is None]
    approval_open = None
    if open_waits:
        first = min(open_waits, key=lambda w: w[1])
        approval_open = (first[0], first[1])
    return RunState(
        run=run, run_kind=run_kind, spec=spec, launch_at=launch_at, launch=launch, tasks=tasks,
        phases_done=tuple(phases_done), phase_running=phase_running, phase_started_min=phase_started,
        approval_open=approval_open, waits=tuple((w[1], w[2]) for w in waits), waited_min=_union_length(closed),
        last_event_min=last_event_min, last_event=last_event, cause=cause, ended=ended, sent=tuple(sent),
        skipped=skipped)


# --------------------------------------------------------------------------- staleness and run lookup

def is_stale(last_event_at: Optional[datetime], now: datetime) -> bool:
    if last_event_at is None:
        return False
    if last_event_at.tzinfo is None:
        last_event_at = last_event_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now - last_event_at > timedelta(minutes=STALE_MIN)


def _runs(env: Optional[Mapping[str, str]], prefix: str = "") -> List[Tuple[Path, List[dict]]]:
    try:
        d = progress_dir(env)
        paths = sorted(d.glob(f"{prefix}*.jsonl"))
    except OSError:
        return []
    out = []
    for p in paths:
        try:
            evs, _ = read_events(p)
        except OSError:
            continue
        out.append((p, evs))
    return out


def _start(evs: Sequence[Mapping]) -> Optional[Mapping]:
    return next((e for e in evs if e.get("kind") == "start"), None)


def _newest_first(items: List[Tuple[Path, List[dict]]]) -> List[Tuple[Path, List[dict]]]:
    def key(item: Tuple[Path, List[dict]]) -> Tuple[str, str]:
        st = _start(item[1])
        return (str(st.get("at")) if st else "", item[0].name)
    return sorted(items, key=key, reverse=True)


def active_run(spec: str, root: Optional[Path] = None, env: Optional[Mapping[str, str]] = None, *,
               now: datetime) -> Optional[str]:
    """Key of the newest run of `spec` with no `end` event (and, when given, started in `root`)."""
    for path, evs in _newest_first(_runs(env, f"{spec}--")):
        st = _start(evs)
        if st is None or st.get("spec") not in (None, spec):
            continue
        if root is not None and st.get("root") != str(root):
            continue
        if any(e.get("kind") == "end" for e in evs):
            continue
        return path.stem
    return None


def runs_for_project(root: Path, env: Optional[Mapping[str, str]] = None, *, now: datetime) -> List[Path]:
    out = []
    for path, evs in _newest_first(_runs(env)):
        st = _start(evs)
        if st is None or st.get("root") != str(root):
            continue
        if any(e.get("kind") == "end" for e in evs):
            continue
        last = _parse(evs[-1].get("at")) if evs else None
        if is_stale(last, now):
            continue
        out.append(path)
    return out


# --------------------------------------------------------------------------- spool mirror

def mergeable(line: str, seen_hashes: Set[str]) -> bool:
    """True the first time a line is seen (and records it); a spool line is mirrored once, keyed by its hash."""
    h = hashlib.sha256(line.encode("utf-8", "surrogateescape")).hexdigest()
    if h in seen_hashes:
        return False
    seen_hashes.add(h)
    return True


@contextlib.contextmanager
def notice_lock(key: str, env: Optional[Mapping[str, str]] = None) -> Iterator[None]:
    path = progress_dir(env) / f"{_check_key(key)}.lock"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _copy_if_changed(src: Path, dst: Path) -> None:
    try:
        data = src.read_bytes()
    except OSError:
        return
    try:
        if dst.exists() and dst.read_bytes() == data:
            return
        tmp = dst.with_name(dst.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dst)
    except OSError:
        pass


def mirror(spool: Path, target: Path, *, send: Callable[[str, str], Any]) -> int:
    """Copy new spool lines and sidecars into `target`, send each pending notice once; return the lines copied."""
    spool, target = Path(spool), Path(target)
    store._mkdir_private(target)
    env = {"CK_PROGRESS_DIR": str(target)}
    copied = 0
    for src in sorted(spool.glob("*.jsonl")):
        key = src.stem
        dst = target / src.name
        try:
            seen: Set[str] = set()
            if dst.exists():
                for old in dst.read_bytes().decode("utf-8", "surrogateescape").split("\n"):
                    if old:
                        seen.add(hashlib.sha256(old.encode("utf-8", "surrogateescape")).hexdigest())
            text = src.read_bytes().decode("utf-8", "surrogateescape")
        except OSError:
            continue
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        fresh = [ln for ln in lines if ln and mergeable(ln, seen)]
        if fresh:
            try:
                _append_line(dst, "".join(ln + "\n" for ln in fresh).encode("utf-8", "surrogateescape"))
                copied += len(fresh)
            except OSError:
                pass
        _copy_if_changed(spool / f"{key}.model.json", target / f"{key}.model.json")
        _copy_if_changed(spool / f"{key}.eta.json", target / f"{key}.eta.json")
        _send_pending(key, dst, env, send)
    return copied


def _send_pending(key: str, path: Path, env: Mapping[str, str], send: Callable[[str, str], Any]) -> None:
    try:
        with notice_lock(key, env):
            events, _ = read_events(path)
            done = {(e.get("notice"), e.get("key")) for e in events if e.get("kind") == "notified"}
            for ev in events:
                if ev.get("kind") != "notify-pending":
                    continue
                ident = (ev.get("notice"), ev.get("key"))
                if ident in done:
                    continue
                done.add(ident)
                ntype = NOTICE_TYPES.get(str(ev.get("notice")), "ck_needs_input")
                try:
                    outcome = send(str(ev.get("message") or ""), ntype)
                    status = getattr(outcome, "status", None) or "failed"
                    detail = getattr(outcome, "detail", None) or None
                except Exception as exc:  # noqa: BLE001
                    status, detail = "failed", f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT]
                append(key, {"kind": "notified", "notice": ev.get("notice"), "key": ev.get("key"),
                             "level": None, "outcome": status, "detail": detail}, env)
    except OSError:
        pass
