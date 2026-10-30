"""Ingest a `ck-opencode-run/1` record (the OpenCode bridge's record.json) into `ck-run-local/1`.

This path never reads a Claude transcript. It validates the frozen contract, turns each agent row into
the same shape `collect._build_agent` produces, and hands the rows to `collect._build_record` through
`collect._RecordSource`: pricing, `roles.match_label`, guard/interruption aggregation, `_workflow_metrics`
and the final assembly are shared with the transcript path.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from claude_kiro import bench
from claude_kiro.analytics import collect, interruptions, roles

SCHEMA = "ck-opencode-run/1"
HARNESS = "opencode"

STATUSES = ("running", "done", "failed", "partial")
AGENT_STATUSES = ("ok", "failed", "timeout", "agent_error", "result_error", "model_error", "session_error")
WORKFLOWS = ("spec-create", "spec-implement", "other")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
FAILURE_LABELS = ("model", "port", "throttle", "spec", "env")

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class RecordError(ValueError):
    """A `ck-opencode-run/1` file that cannot be ingested; the message says which field is wrong."""


# ---------------------------------------------------------------- field checks


def _fail(where: str, message: str) -> None:
    raise RecordError(f"{where}: {message}")


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(where, "must be an object")
    return value


def _string(value: Any, where: str, *, required: bool = False, allow_empty: bool = False) -> Optional[str]:
    if value is None:
        if required:
            _fail(where, "is required")
        return None
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        _fail(where, "must be a non-empty string")
    return value


def _integer(value: Any, where: str, *, required: bool = False, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        if required:
            _fail(where, "is required")
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(where, "must be a non-negative integer")
    return value


def _number(value: Any, where: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        _fail(where, "must be a non-negative number")


def _list(value: Any, where: str) -> List[Any]:
    if not isinstance(value, list):
        _fail(where, "must be a list")
    return value


def _effort(value: Any, where: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or value not in EFFORTS:
        _fail(where, "must be one of " + ", ".join(EFFORTS) + " or null")
    return value


def _variant(value: Any, where: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        _fail(where, "must be a non-empty string or null")
    return value


# ---------------------------------------------------------------- agent rows


def _tokens(value: Any, where: str) -> Optional[Dict[str, int]]:
    if value is None:
        return None
    t = _mapping(value, where)
    out = {"input": 0, "output": 0, "cacheRead": 0, "cacheCreation": 0}
    for key in ("input", "output", "reasoning"):
        v = _integer(t.get(key), f"{where}.{key}", default=0)
        if key != "reasoning":
            out[key] = v or 0
    cache = t.get("cache")
    if cache is not None:
        c = _mapping(cache, f"{where}.cache")
        out["cacheRead"] = _integer(c.get("read"), f"{where}.cache.read", default=0) or 0
        out["cacheCreation"] = _integer(c.get("write"), f"{where}.cache.write", default=0) or 0
    return out


def _resolved_model(resolved: Mapping[str, Any], where: str) -> Optional[str]:
    provider = _string(resolved.get("providerID"), f"{where}.providerID")
    model_id = _string(resolved.get("id"), f"{where}.id")
    if provider is None or model_id is None:
        return None
    return f"{provider}/{model_id}"


def _agent_row(a: Mapping[str, Any], index: int, run_id: str, workflow: str) -> Tuple[dict, dict]:
    where = f"agents[{index}]"
    seq = _integer(a.get("seq"), f"{where}.seq", default=index + 1)
    label = _string(a.get("label"), f"{where}.label", required=True)
    phase = _string(a.get("phase"), f"{where}.phase")
    requested = _mapping(a.get("requested") or {}, f"{where}.requested")
    _string(requested.get("model"), f"{where}.requested.model", allow_empty=True)
    requested_raw_effort = _string(requested.get("effort"), f"{where}.requested.effort", allow_empty=True)
    if requested_raw_effort is not None and requested_raw_effort != "inherit" and requested_raw_effort not in EFFORTS:
        _fail(f"{where}.requested.effort", "must be a ck effort, 'inherit' or null")
    resolved = _mapping(a.get("resolved") or {}, f"{where}.resolved")
    _string(resolved.get("variant"), f"{where}.resolved.variant", allow_empty=True)
    model = _resolved_model(resolved, f"{where}.resolved")
    requested_effort = _effort(a.get("requestedEffort"), f"{where}.requestedEffort")
    resolved_effort = _variant(a.get("resolvedEffort"), f"{where}.resolvedEffort")
    variant = _variant(a.get("variant"), f"{where}.variant")
    usage = _tokens(a.get("tokens"), f"{where}.tokens")

    started = _integer(a.get("startedAt"), f"{where}.startedAt")
    ended = _integer(a.get("endedAt"), f"{where}.endedAt")
    ms = _integer(a.get("durationMs"), f"{where}.durationMs")
    if ms is None and started is not None and ended is not None and ended >= started:
        ms = ended - started
    state = _string(a.get("status"), f"{where}.status")
    if state is not None and state not in AGENT_STATUSES:
        _fail(f"{where}.status", "must be one of " + ", ".join(AGENT_STATUSES))
    for key in ("attempts", "rejections", "peakContext"):
        _integer(a.get(key), f"{where}.{key}")
    if a.get("cost") is not None:
        _number(a.get("cost"), f"{where}.cost")
    _string(a.get("error"), f"{where}.error", allow_empty=True)
    _string(a.get("submission"), f"{where}.submission", allow_empty=True)

    match = roles.match_label(label, workflow)
    session_id = _string(a.get("sessionID"), f"{where}.sessionID")
    agent_id = session_id or f"{run_id}:{seq}"
    usd = bench.price_usage(model, usage) if model is not None and usage is not None else None
    row = {
        "agentId": agent_id, "label": label, "role": match.role, "matched": match.matched, "task": match.task,
        "attempt": match.attempt, "round": match.round, "step": match.step, "escalatedTo": match.escalated_to,
        "model": model, "phase": phase, "state": state, "startedAtMs": started, "ms": ms,
        "usd": usd, "usdEstimate": None,
        "input": usage["input"] if usage else None, "output": usage["output"] if usage else None,
        "cacheRead": usage["cacheRead"] if usage else None, "cacheCreation": usage["cacheCreation"] if usage else None,
        "requestedEffort": requested_effort, "resolvedEffort": resolved_effort, "variant": variant,
        "transcript": "native", "queuedAtMs": None, "queueMs": None,
    }
    info = {
        "scan": None, "usage_by_model": ({model: usage} if model is not None and usage is not None else {}),
        "usage": usage, "match": match, "first_ts": None, "last_ts": None, "terms": set(),
    }
    return row, info


# ---------------------------------------------------------------- the record


def record_from_data(data: Any) -> collect.CollectOutcome:
    """Validate a parsed `ck-opencode-run/1` object and build its local record (no write)."""
    root = _mapping(data, "record")
    if root.get("schema") != SCHEMA:
        _fail("record.schema", f"must be {SCHEMA!r}")

    run_id = _string(root.get("runId"), "record.runId", required=True) or ""
    if not _RUN_ID_RE.fullmatch(run_id) or run_id in (".", ".."):
        _fail("record.runId", "must be a safe run id (letters, digits, '.', '_' or '-')")
    harness = _string(root.get("harness"), "record.harness", required=True)
    if harness != HARNESS:
        _fail("record.harness", f"must be {HARNESS!r}")

    workflow = _string(root.get("workflow"), "record.workflow", required=True) or ""
    if workflow not in WORKFLOWS:
        _fail("record.workflow", "must be one of " + ", ".join(WORKFLOWS))
    collector_version = _string(root.get("collectorVersion"), "record.collectorVersion", required=True)
    ck_version = _string(root.get("ckVersion"), "record.ckVersion", required=True)
    status = _string(root.get("status"), "record.status", required=True) or ""
    if status not in STATUSES:
        _fail("record.status", "must be one of " + ", ".join(STATUSES))
    started = _integer(root.get("startedAt"), "record.startedAt", required=True)
    if not started:
        _fail("record.startedAt", "must be a positive epoch in milliseconds")
    ended = _integer(root.get("endedAt"), "record.endedAt")
    if ended is not None and ended < started:
        _fail("record.endedAt", "must not be before startedAt")
    project_root = _string(root.get("projectRoot"), "record.projectRoot", required=True)
    spec = _string(root.get("spec"), "record.spec")
    into = _string(root.get("into"), "record.into")

    args_raw = root.get("args", {})
    args = dict(_mapping(args_raw, "record.args"))
    result = root.get("result")
    if result is not None:
        result = dict(_mapping(result, "record.result"))

    throttles = _integer(root.get("throttles"), "record.throttles", default=0) or 0
    retries = _integer(root.get("retries"), "record.retries", default=0) or 0
    failure_label = root.get("failureLabel")
    if failure_label is not None and failure_label not in FAILURE_LABELS:
        _fail("record.failureLabel", "must be one of " + ", ".join(FAILURE_LABELS) + " or null")
    degraded = [d for d in _list(root.get("degraded", []), "record.degraded") if isinstance(d, str) and d]
    if len(degraded) != len(root.get("degraded", [])):
        _fail("record.degraded", "must be a list of non-empty strings")

    guard = _mapping(root.get("guard") or {}, "record.guard")
    for key in ("denials", "postCheckCatches", "guardEscalations", "headlessDenials", "prompts"):
        _integer(guard.get(key), f"record.guard.{key}", default=0)
    _list(guard.get("events", []), "record.guard.events")

    agents = _list(root.get("agents"), "record.agents")
    missing: List[dict] = []
    rows: List[dict] = []
    infos: List[dict] = []
    for i, raw_agent in enumerate(agents):
        row, info = _agent_row(_mapping(raw_agent, f"agents[{i}]"), i, run_id, workflow)
        rows.append(row)
        infos.append(info)

    partial = status in ("running", "partial")
    wall_ms = (ended - started) if ended is not None else None
    extra: Dict[str, Any] = {
        "throttles": throttles, "retries": retries, "degraded": degraded, "failureLabel": failure_label,
    }
    if partial:
        extra["partial"] = True
    source = collect._RecordSource(
        rows=rows, infos=infos, missing=missing, start_ms=started, wall_ms=wall_ms, status=status,
        session_id=run_id, root=project_root, spec=spec, branch=into, ck_version=ck_version,
        collector_version=collector_version, claude_code_version=None, harness=HARNESS, source="workflow",
        partial=partial, cost_source="estimate", interruptions=interruptions.native_block(guard=guard, rows=rows),
        interruptions_from_agents=False, extra=extra,
    )
    record = collect._build_record(
        run_id=run_id, workflow_name=workflow, run_data={"args": args, "result": result},
        source=source, pricing={},
    )
    return collect.CollectOutcome(run_id, "collected", record=record)


def ingest_file(path: Path) -> collect.CollectOutcome:
    """Read and validate RECORD_FILE; raises RecordError with a field-level message on malformed input."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise RecordError(f"cannot read {p}: {e}") from None
    try:
        data = json.loads(text)
    except ValueError as e:
        raise RecordError(f"{p}: invalid JSON: {e}") from None
    try:
        return record_from_data(data)
    except RecordError as e:
        raise RecordError(f"{p}: {e}") from None
