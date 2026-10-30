"""Interruption counts of one run, read from its transcripts (design S13-S16, the local `interruptions` record).

An interruption is a moment a run stopped for a person or a guard: a prompt the guard asked about, a guard denial,
a post-check catch, a guard-ladder escalation, or a refusal by the permission system. The counts come from the
transcripts. The private guard log is only compared with them; the transcripts win (10.4).
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Collection, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from claude_kiro.analytics import guardlog
from claude_kiro.hooks._shared.guard_policy import command_family

SCHEMA_VERSION = 1

ROLES = ("implementer", "test-writer", "reviewer", "fixer", "resolver", "orchestrator")

# Local (camelCase) kind names, in the record's order.
KINDS = ("prompts", "guardDenials", "postCheckCatches", "guardEscalations", "autoModeDenials", "headlessDenials",
         "userRejects", "isolationRefusals")

_RESULT_KINDS = {
    "auto_mode_denial": "autoModeDenials",
    "headless_denial": "headlessDenials",
    "user_reject": "userRejects",
    "isolation_refusal": "isolationRefusals",
    "guard_denial": "guardDenials",
}
_ISOLATION_PREFIXES = ("This session is isolated in the worktree", "This agent is isolated in the worktree")
_LOG_KINDS = {"ask": "prompts", "deny": "guardDenials", "post-check": "postCheckCatches"}
_COMPARED = ("prompts", "guardDenials", "postCheckCatches")


@dataclass
class ScanResult:
    """Counts of one transcript: `counts[(family, kind)]`, the wait in ms and the number of skipped lines."""
    role: str = "orchestrator"
    counts: Dict[Tuple[str, str], int] = field(default_factory=dict)
    wait_ms: int = 0
    skipped: int = 0


# ---------------------------------------------------------------- S13, S15, S16


def classify_result_text(text: str) -> Optional[str]:
    """S13: the denial kind of an error tool-result text, or None."""
    if not isinstance(text, str):
        return None
    if text.startswith(_ISOLATION_PREFIXES):
        return "isolation_refusal"
    if "[ck guard]" in text and "Denied" in text:
        return "guard_denial"
    if "denied by the Claude Code auto mode classifier" in text:
        return "auto_mode_denial"
    if "requires approval, and this session has no approval surface" in text:
        return "headless_denial"
    if "The user doesn't want to proceed" in text:
        return "user_reject"
    return None


def interruption_role(row_role: Optional[str]) -> str:
    """S15: the role an interruption is counted under."""
    if row_role in ("implementer", "test-writer", "reviewer", "fixer", "resolver"):
        return row_role  # type: ignore[return-value]
    return "orchestrator"


def reconcile(transcript: Mapping[str, int], log: Mapping[str, int]) -> Tuple[Dict[str, int], Dict[str, Dict[str, int]]]:
    """S16: the transcript counts unchanged, and where the log disagrees `{kind: {"log": n, "transcripts": m}}`."""
    differences: Dict[str, Dict[str, int]] = {}
    for kind in _COMPARED:
        a, b = int(log.get(kind, 0)), int(transcript.get(kind, 0))
        if a != b:
            differences[kind] = {"log": a, "transcripts": b}
    return dict(transcript), differences


# ---------------------------------------------------------------- S14


def _ms(ts: Any) -> Optional[int]:
    if not isinstance(ts, str):
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content if isinstance(b, Mapping) and isinstance(b.get("text"), str))
    return ""


def _decision(attachment: Mapping) -> Optional[str]:
    """`ask` or `deny` of a ck PreToolUse guard hook_success attachment, else None (an RTK allow, no decision)."""
    stdout = attachment.get("stdout")
    command = attachment.get("command")
    if not isinstance(stdout, str):
        return None
    is_guard = (isinstance(command, str) and "pre-tool-guard" in command) or "[ck guard]" in stdout
    if not is_guard:
        return None
    try:
        out = json.loads(stdout)
    except ValueError:
        return None
    spec = out.get("hookSpecificOutput") if isinstance(out, Mapping) else None
    decision = spec.get("permissionDecision") if isinstance(spec, Mapping) else None
    return decision if decision in ("ask", "deny") else None


def scan_lines(lines: Iterable[Any], role: str, window: Tuple[Optional[int], Optional[int]] = (None, None)) -> ScanResult:
    """S14: counts for one transcript, each interruption once per `toolUseID` and kind."""
    start, end = window
    tool_input: Dict[str, Tuple[str, Any]] = {}
    seen: Dict[Tuple[str, str], bool] = {}
    kinds: List[Tuple[str, str]] = []  # (tool_use_id, kind) in first-seen order
    ask_ts: Dict[str, int] = {}
    result_ts: Dict[str, int] = {}
    skipped = 0

    def note(tid: str, kind: str) -> None:
        if (tid, kind) not in seen:
            seen[(tid, kind)] = True
            kinds.append((tid, kind))

    for line in lines:
        if not isinstance(line, Mapping):
            skipped += 1
            continue
        ltype = line.get("type")
        if not isinstance(ltype, str):
            skipped += 1
            continue
        ts = _ms(line.get("timestamp"))
        message = line.get("message")
        if ltype == "assistant" and isinstance(message, Mapping) and isinstance(message.get("content"), list):
            for block in message["content"]:
                if isinstance(block, Mapping) and block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                    tool_input.setdefault(block["id"], (str(block.get("name")), block.get("input")))
            continue
        if ltype not in ("attachment", "user"):
            continue
        if ts is not None and ((start is not None and ts < start) or (end is not None and ts > end)):
            continue  # another run's line in a shared session: not malformed, so not counted as skipped
        if ltype == "attachment":
            att = line.get("attachment")
            if not isinstance(att, Mapping):
                skipped += 1
                continue
            atype = att.get("type")
            tid = att.get("toolUseID")
            if atype == "hook_success":
                decision = _decision(att)
                if decision is None:
                    if not isinstance(att.get("stdout"), str):
                        skipped += 1
                    continue
                if not isinstance(tid, str):
                    skipped += 1
                    continue
                if decision == "ask":
                    note(tid, "prompts")
                    if ts is not None:
                        ask_ts.setdefault(tid, ts)
                else:
                    note(tid, "guardDenials")
            elif atype == "hook_blocking_error" and att.get("hookEvent") == "PostToolUse":
                err = att.get("blockingError")
                text = err.get("blockingError") if isinstance(err, Mapping) else None
                if not isinstance(text, str):
                    skipped += 1
                    continue
                if "[ck post-check]" in text and isinstance(tid, str):
                    note(tid, "postCheckCatches")
            continue
        # a user line: tool results
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, Mapping) and block.get("type") == "tool_result"):
                continue
            tid = block.get("tool_use_id")
            if not isinstance(tid, str):
                continue
            if ts is not None:
                result_ts.setdefault(tid, ts)
            if block.get("is_error") is True:
                kind = classify_result_text(_result_text(block.get("content")))
                if kind is not None:
                    note(tid, _RESULT_KINDS[kind])

    result = ScanResult(role=role, skipped=skipped)
    for tid, kind in kinds:
        name, tinput = tool_input.get(tid, ("", None))
        command = tinput.get("command") if name == "Bash" and isinstance(tinput, Mapping) else None
        family = command_family(command) if name == "Bash" else "other"
        key = (family, kind)
        result.counts[key] = result.counts.get(key, 0) + 1
        if kind == "prompts" and tid in ask_ts and tid in result_ts:
            gap = result_ts[tid] - ask_ts[tid]
            if gap > 0:
                result.wait_ms += gap
    return result


# ---------------------------------------------------------------- the record builder


def _scan_file(path: Optional[Path], role: str, window: Tuple[Optional[int], Optional[int]]) -> Optional[ScanResult]:
    if path is None:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None  # an unreadable transcript adds nothing; `missing` already lists it
    parsed: List[Any] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            parsed.append(json.loads(raw))
        except ValueError:
            parsed.append(raw)  # a non-mapping: scan_lines counts it as skipped
    return scan_lines(parsed, role, window)


def _empty_kinds() -> Dict[str, int]:
    return {k: 0 for k in KINDS}


def collect_run_interruptions(*, agents: Sequence[Any], rows: Sequence[Mapping], session_jsonl: Optional[Path],
                              start_ms: Optional[int], wall_ms: Optional[int], session_ids: Collection[str],
                              open_end: bool = False,
                              env: Optional[Mapping[str, str]] = None) -> Tuple[dict, bool]:
    """The local `interruptions` object and whether the guard log belongs in the record's `missing`."""
    from claude_kiro.analytics import share

    # An agent-mode run has no end of its own (its orchestrator can talk long after the last subagent), so its window
    # is open at the end; a Workflow run ends where its run file says.
    end_ms = start_ms + wall_ms if not open_end and start_ms is not None and wall_ms is not None else None
    window = (start_ms, end_ms)

    scans: List[Tuple[str, ScanResult]] = []
    session_scan = _scan_file(session_jsonl, "orchestrator", window)
    if session_scan is not None:
        scans.append(("orchestrator", session_scan))
    for agent, row in zip(agents, rows):
        role = interruption_role(row.get("role"))
        if role == "orchestrator" and not row.get("matched"):
            # a description the grammar does not know still has an agent type that names its role
            from claude_kiro.analytics import roles as _roles

            role = _roles._TYPE_ROLE.get(getattr(agent, "agent_type", None), "orchestrator")
        scan = _scan_file(getattr(agent, "transcript", None), role, (None, None))
        if scan is not None:
            scans.append((scan.role, scan))

    totals = _empty_kinds()
    totals["waitMs"] = 0
    by_role: Dict[str, Dict[str, Dict[str, int]]] = {}
    wait_by_role: Dict[str, int] = {}
    skipped = 0
    for role, scan in scans:
        skipped += scan.skipped
        for (family, kind), n in scan.counts.items():
            cell = by_role.setdefault(role, {}).setdefault(family, {})
            cell[kind] = cell.get(kind, 0) + n
            totals[kind] += n
        if scan.wait_ms:
            wait_by_role[role] = wait_by_role.get(role, 0) + scan.wait_ms
            totals["waitMs"] += scan.wait_ms

    escalation_models: Dict[str, int] = {}
    for row in rows:
        if row.get("step") != "guard":
            continue
        role = interruption_role(row.get("role"))
        cell = by_role.setdefault(role, {}).setdefault("other", {})
        cell["guardEscalations"] = cell.get("guardEscalations", 0) + 1
        totals["guardEscalations"] += 1
        family = share.model_family(row.get("escalatedTo") or row.get("model"))
        escalation_models[family] = escalation_models.get(family, 0) + 1

    agent_ids = [r["agentId"] for r in rows if isinstance(r.get("agentId"), str) and r["agentId"]]
    log = guardlog.read_events(session_ids, window=window, agent_ids=agent_ids, env=env)
    log_counts: Dict[str, int] = {}
    for event in log.events:
        kind = _LOG_KINDS.get(event.get("decision"))
        if kind:
            log_counts[kind] = log_counts.get(kind, 0) + 1
    differences: Dict[str, Dict[str, int]] = {}
    if log.events:
        _, differences = reconcile({k: totals[k] for k in _COMPARED}, log_counts)

    block = {
        "v": SCHEMA_VERSION,
        "totals": totals,
        "byRole": by_role,
        "waitMsByRole": wait_by_role,
        "escalationModels": escalation_models,
        "guardLog": {"state": log.state, "events": len(log.events), "differences": differences},
        "skippedLines": skipped,
    }
    return block, log.state in ("unreadable", "rotated")
