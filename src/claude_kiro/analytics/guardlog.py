"""The private guard event log: one JSON line per guard ask, deny or post-check catch.

The log holds command text and paths, so it lives only under the ck data directory
(directory 0700, file 0600), never in a project. Appending never raises: a log that
cannot be written changes nothing the agent sees.
"""

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Dict, List, Literal, Mapping, Optional, Tuple

from claude_kiro.analytics import store

try:
    import fcntl
except ImportError:  # pragma: no cover - not POSIX
    fcntl = None  # type: ignore[assignment]

GUARD_LOG_LIMIT = 1_048_576
LOG_NAME = "guard-events.jsonl"
ROTATED_NAME = "guard-events.1.jsonl"
LOCK_NAME = "guard-events.lock"
LOCK_WAIT_S = 0.5


@dataclass(frozen=True)
class GuardEvent:
    ts: str  # ISO-8601 UTC
    session_id: str
    agent_id: Optional[str]
    agent_type: Optional[str]
    caller: str
    tool: str
    decision: Literal["ask", "deny", "post-check"]
    category: str
    reason: str
    path: str
    command: Optional[str]
    family: str
    tool_use_id: Optional[str]


@dataclass(frozen=True)
class GuardLogRead:
    state: Literal["ok", "rotated", "unreadable"]
    events: Tuple[dict, ...]
    skipped: int = 0


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def log_path(env: Optional[Mapping[str, str]] = None) -> Path:
    return store.data_dir(env) / LOG_NAME


def _rotated_path(env: Optional[Mapping[str, str]] = None) -> Path:
    return store.data_dir(env) / ROTATED_NAME


def _to_json(event: GuardEvent) -> Dict[str, Any]:
    return {
        "v": 1,
        "ts": event.ts,
        "sessionId": event.session_id,
        "agentId": event.agent_id,
        "agentType": event.agent_type,
        "caller": event.caller,
        "tool": event.tool,
        "decision": event.decision,
        "category": event.category,
        "reason": event.reason,
        "path": event.path,
        "command": event.command,
        "family": event.family,
        "toolUseId": event.tool_use_id,
    }


def _lock(directory: Path) -> Optional[int]:
    """An exclusive lock on the data dir's guard lock file (fd), or None when it cannot be had within LOCK_WAIT_S.

    Without the lock the event is still appended, only rotation is skipped (a later append rotates).
    """
    if fcntl is None:
        return None
    try:
        fd = os.open(directory / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.01)


def _unlock(fd: Optional[int]) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[union-attr]
    except OSError:
        pass
    finally:
        os.close(fd)


def append_event(event: GuardEvent, env: Optional[Mapping[str, str]] = None) -> bool:
    """Append one event; rotate first when the file is at the limit. False when it could not be written."""
    try:
        line = json.dumps(_to_json(event), ensure_ascii=True) + "\n"
        path = log_path(env)
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        lock = _lock(directory)
        try:
            if lock is not None:
                # Re-checked under the lock: two hooks that both saw the limit rotate once, not twice.
                try:
                    if path.stat().st_size >= GUARD_LOG_LIMIT:
                        os.replace(path, _rotated_path(env))
                except FileNotFoundError:
                    pass
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        finally:
            _unlock(lock)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except Exception:
        return False


def _parse_ms(ts: Any) -> Optional[int]:
    if not isinstance(ts, str):
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _read_file(path: Path) -> Tuple[List[dict], int, bool]:
    """(events, skipped lines, readable)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return [], 0, True
    except OSError:
        return [], 0, False
    events: List[dict] = []
    skipped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(row, dict):
            events.append(row)
        else:
            skipped += 1
    return events, skipped, True


def read_events(
    session_ids: Collection[str],
    *,
    window: Tuple[Optional[int], Optional[int]] = (None, None),
    agent_ids: Optional[Collection[str]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> GuardLogRead:
    """Events of the given sessions inside the run window (ms), for the run's agents or no agent."""
    current, skipped_a, ok_a = _read_file(log_path(env))
    older, skipped_b, ok_b = _read_file(_rotated_path(env))
    skipped = skipped_a + skipped_b
    if not (ok_a and ok_b):
        return GuardLogRead("unreadable", (), skipped)
    allrows = older + current
    start, end = window
    state: Literal["ok", "rotated", "unreadable"] = "ok"
    if start is not None and _rotated_path(env).exists():
        stamps = [m for m in (_parse_ms(r.get("ts")) for r in allrows) if m is not None]
        if stamps and min(stamps) > start:
            state = "rotated"
    sessions = set(session_ids)
    agents = set(agent_ids) if agent_ids is not None else None
    kept: List[dict] = []
    for row in allrows:
        if row.get("sessionId") not in sessions:
            continue
        ms = _parse_ms(row.get("ts"))
        if ms is None:
            skipped += 1
            continue
        if (start is not None and ms < start) or (end is not None and ms > end):
            continue
        if agents is not None:
            aid = row.get("agentId")
            if aid and aid not in agents:
                continue
        kept.append(row)
    return GuardLogRead(state, tuple(kept), skipped)


def leak_terms(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """Every command, path and reason text in the log (current and rotated file)."""
    out: List[str] = []
    seen = set()
    for path in (_rotated_path(env), log_path(env)):
        rows, _skipped, _ok = _read_file(path)
        for row in rows:
            for key in ("command", "path", "reason"):
                value = row.get(key)
                if isinstance(value, str) and value.strip() and value not in seen:
                    seen.add(value)
                    out.append(value)
    return out
