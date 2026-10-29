"""Run-file reader: allowlist, format check, run state and run-file discovery."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Literal, Mapping, Optional, Sequence, Tuple

ALLOWLISTED_WORKFLOWS = frozenset({"spec-implement", "spec-implement-iso", "spec-create"})
ALLOWED_WORKFLOWS = ALLOWLISTED_WORKFLOWS

RunState = Literal["finished", "unfinished"]
HomeAccess = Literal["ok", "sandboxed", "missing"]

_UNFINISHED = frozenset({"running", "pending", "queued", "starting"})
_TERMINAL = frozenset({"completed", "failed", "killed", "cancelled", "error", "stopped"})


@dataclass(frozen=True)
class FormatCheck:
    ok: bool
    problems: Tuple[str, ...] = ()


@dataclass(frozen=True)
class OrphanRun:
    agent_dir: Path
    session_jsonl: Optional[Path]


def is_allowlisted(name: object) -> bool:
    return isinstance(name, str) and name in ALLOWLISTED_WORKFLOWS


is_ck_workflow = is_allowlisted


def check_format(data: object) -> FormatCheck:
    if not isinstance(data, dict):
        return FormatCheck(False, ("run file is not an object",))
    problems: List[str] = []
    if not isinstance(data.get("workflowName"), str):
        problems.append("workflowName is missing or not a string")
    if not isinstance(data.get("status"), str):
        problems.append("status is missing or not a string")
    st = data.get("startTime")
    if isinstance(st, bool) or not isinstance(st, (int, str)):
        problems.append("startTime is missing or not an int or string")
    if not isinstance(data.get("workflowProgress"), list):
        problems.append("workflowProgress is not a list")
    return FormatCheck(not problems, tuple(problems))


def run_state(data: Mapping) -> RunState:
    status = data.get("status")
    if status in _UNFINISHED:
        return "unfinished"
    if status not in _TERMINAL and data.get("durationMs") is None:
        return "unfinished"
    return "finished"


def classify_home(exists: bool, listable: bool, sandbox_flag: bool) -> HomeAccess:
    if not listable:
        return "sandboxed"
    if not exists:
        return "sandboxed" if sandbox_flag else "missing"
    return "ok"


def find_run_file(home: Path, run_id: str) -> Optional[Path]:
    if not run_id or "/" in run_id or "\\" in run_id or run_id in (".", ".."):
        return None
    try:
        for p in sorted(Path(home).glob(f"projects/*/*/workflows/{run_id}.json")):
            if p.is_file():
                return p
    except OSError:
        return None
    return None


def iter_run_files(home: Path) -> List[Path]:
    try:
        return sorted(p for p in Path(home).glob("projects/*/*/workflows/wf_*.json") if p.is_file())
    except OSError:
        return []


def peek_workflow_name(path: Path) -> Optional[str]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        name = data.get("workflowName")
        if isinstance(name, str):
            return name
    return None


def session_run_ids(home: Path, session_id: str, since_ms: int) -> Tuple[List[str], int]:
    ids: List[str] = []
    unreadable = 0
    for p in iter_run_files(home):
        if p.parent.parent.name != session_id:
            continue
        try:
            if p.stat().st_mtime * 1000 < since_ms:
                continue
        except OSError:
            continue
        name = peek_workflow_name(p)
        if name is None:
            unreadable += 1
        elif is_allowlisted(name):
            ids.append(p.stem)
    return ids, unreadable


def find_orphan_run(home: Path, run_id: str) -> Optional[OrphanRun]:
    if not run_id or "/" in run_id or "\\" in run_id or run_id in (".", ".."):
        return None
    if find_run_file(home, run_id) is not None:
        return None
    try:
        for d in sorted(Path(home).glob(f"projects/*/*/subagents/workflows/{run_id}")):
            if d.is_dir():
                sid_dir = d.parent.parent.parent
                jsonl = sid_dir.parent / f"{sid_dir.name}.jsonl"
                return OrphanRun(d, jsonl if jsonl.is_file() else None)
    except OSError:
        return None
    return None


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and isinstance(c.get("text"), str):
                parts.append(c["text"])
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    return ""


def workflow_launches(session_jsonl: Path) -> List[Tuple[Mapping, str]]:
    uses: dict = {}
    results: dict = {}
    order: List[str] = []
    try:
        with open(session_jsonl, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                msg = obj.get("message") if isinstance(obj, dict) else None
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use" and c.get("name") == "Workflow":
                        tid = c.get("id")
                        if isinstance(tid, str) and isinstance(c.get("input"), dict):
                            uses[tid] = c["input"]
                            order.append(tid)
                    elif c.get("type") == "tool_result":
                        tid = c.get("tool_use_id")
                        if isinstance(tid, str):
                            results[tid] = _result_text(c.get("content"))
    except OSError:
        return []
    return [(uses[t], results[t]) for t in order if t in results]


def recover_workflow_name(launches: Sequence[Tuple[Mapping, str]], run_id: str) -> Optional[str]:
    if not run_id:
        return None
    for inp, text in launches:
        if not isinstance(text, str) or not re.search(
                r"(?<![A-Za-z0-9_-])" + re.escape(run_id) + r"(?![A-Za-z0-9_-])", text):
            continue
        name: Optional[str] = None
        raw = inp.get("name") if isinstance(inp, Mapping) else None
        if isinstance(raw, str) and raw:
            name = raw[len("spec:"):] if raw.startswith("spec:") else raw
        else:
            sp = inp.get("scriptPath") if isinstance(inp, Mapping) else None
            if isinstance(sp, str):
                base = sp.replace("\\", "/").rsplit("/", 1)[-1]
                if base in ("spec-implement.js", "spec-create.js"):
                    name = base[:-3]
        return name if is_allowlisted(name) else None
    return None
