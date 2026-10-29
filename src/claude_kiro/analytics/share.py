"""The shareable record: the one place that decides what leaves the machine (requirements 7.2-7.7).

`build_shareable` builds a NEW dict field by field from named local-record fields. It has no input for, and never
reads, the pseudonym, remote, spec name, titles, labels, paths or ids. Every string it emits is a member of a fixed
set, a regex-checked version or month, or a random record id; everything else is a count or a number. Anything
outside a fixed set is written as `other`, never as the original text.

`SHAREABLE_SCHEMA` lists every allowed dotted path of `ck-run-analytics/1` with its type, enum or format.
Pattern syntax (same as `analytics/SCHEMA.md`): `*` = a segment whose value comes from a fixed enum, `[]` = list items.
"""

import math
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from claude_kiro.analytics import SCHEMA_SHARED
from claude_kiro.analytics.issues import ISSUE_CODES, derive_issue_codes
from claude_kiro.analytics.roles import ROLES

# ---------------------------------------------------------------- fixed sets

MODEL_FAMILIES = ("haiku", "sonnet", "opus", "fable", "inherit", "other")
OUTCOMES = ("complete", "blocked", "halted", "refused", "ready", "ready_after_decisions", "needs_work", "failed", "other")
VERDICTS = ("approve", "changes", "unreviewed", "other")
EFFORTS = ("low", "medium", "high", "xhigh", "max", "inherit")
STACK_HINTS = ("python", "node", "go", "rust", "java", "ruby", "dotnet", "other")
WORKFLOWS = ("spec-implement", "spec-create", "spec-plan", "other")
VARIANTS = ("default", "iso", "other")
CONFIG_ROLES = ("implementer", "test_writer", "reviewer", "reviewer_risky", "fixer", "orchestrator", "resolver")
TRACKS = ("impl", "test", "unknown", "other")
RISKS = ("normal", "safety", "other")
COST_SOURCES = ("priced_from_transcripts", "estimate", "agent_transcripts")
SOURCES = ("workflow", "agents")
MISSING_KINDS = ("result", "runfile", "agent_entries", "transcripts", "meta", "timing", "ck_version")
PHASES = ("plan", "wave", "tasks", "final_gate", "report", "requirements", "design_test_plan", "task_tracks", "link",
          "adversarial_review", "revise", "consistency_check", "other")

COUNT_KEYS = (
    "tasks", "tasks_merged", "tasks_failed", "tasks_not_started", "waves", "agents", "attempts", "retries",
    "escalations", "review_rounds", "conflicts", "conflicts_inferred", "fixes", "landings", "bisects",
    "gates_green", "gates_red", "cross_tests", "cross_test_blocking", "plan_steps", "plan_failures",
    "review_fixed", "review_not_fixed", "review_escalated", "refuted", "lenses_lost",
    "consistency_passes", "consistency_fixed", "open_questions", "agents_other",
)
USAGE_KEYS = ("input", "output", "cacheRead", "cacheCreation")

_FIELD_SETS = {
    "outcome": frozenset(OUTCOMES),
    "verdict": frozenset(VERDICTS),
    "role": frozenset(ROLES),
    "effort": frozenset(EFFORTS),
    "stack": frozenset(STACK_HINTS),
    "issue": frozenset(ISSUE_CODES),
}

_MODEL_RE = re.compile(r"(?:claude-)?(haiku|sonnet|opus|fable)(?:-[0-9][0-9a-z-]*)?(?:\[1m\])?")
_VERSION_RE = re.compile(
    r"[0-9]+\.[0-9]+\.[0-9]+(?:\.?(?:a|b|rc|dev|post)[0-9]+)?(?:-(?:alpha|beta|rc|dev|pre)(?:\.?[0-9]+)?)?")
_ID_RE = re.compile(r"[0-9a-f]{16}")
_MONTH_RE = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")
_MACHINE_RE = re.compile(r"[a-z0-9-]{1,32}")
_PRICING_RE = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")
_WAVE_RE = re.compile(r"Wave ([0-9]{1,3})")

# ---------------------------------------------------------------- enum mapping (S11, S12)


def to_enum(value: Any, allowed: Iterable[str]) -> str:
    """S11: `value` when it is a string member of `allowed`, else `other`. The original is never copied."""
    if isinstance(value, str) and value in allowed:
        return value
    return "other"


def model_alias(model: Any) -> str:
    """S12: the model family for canonical ids and bare aliases; `inherit` for inherited; else `other`."""
    if not isinstance(model, str) or not model.isascii():
        return "other"
    low = model.lower()
    if low in ("inherit", "inherited"):
        return "inherit"
    m = _MODEL_RE.fullmatch(low)
    return m.group(1) if m else "other"


def model_family(model: Any) -> str:
    return model_alias(model)


def enum_value(field: str, value: Any) -> str:
    if field == "model":
        return model_alias(value)
    allowed = _FIELD_SETS.get(field)
    if allowed is None:
        raise ValueError(f"unknown enumerated field: {field}")
    return to_enum(value, allowed)


def new_record_id() -> str:
    """16 random hex digits, never holding a run of 10 or more digits (it would read as an epoch timestamp)."""
    while True:
        rid = secrets.token_hex(8)
        if not re.search(r"[0-9]{10}", rid):
            return rid


def month_of(start: Any) -> str:
    """UTC `yyyy-mm` of an ISO string or an epoch (seconds or milliseconds). Raises ValueError when unreadable."""
    if isinstance(start, bool):
        raise ValueError("start time is not readable")
    if isinstance(start, (int, float)):
        if not math.isfinite(start):
            raise ValueError("start time is not readable")
        seconds = start / 1000 if abs(start) >= 1e11 else start
        try:
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise ValueError("start time is not readable") from None
    elif isinstance(start, str):
        text = start.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError("start time is not readable") from None
        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    else:
        raise ValueError("start time is not readable")
    if not 1970 <= dt.year <= 9999:
        raise ValueError("start time is out of range")
    return f"{dt.year:04d}-{dt.month:02d}"


# ---------------------------------------------------------------- schema

_INT = ("int",)
_NUM = ("num",)
_BOOL = ("bool",)


def _enum(values: Iterable[str]) -> tuple:
    return ("enum", frozenset(values))


def _fmt(name: str, rx: "re.Pattern") -> tuple:
    return ("fmt", name, rx)


def _obj(fields: Mapping[str, tuple], required: Iterable[str] = ()) -> tuple:
    return ("obj", dict(fields), frozenset(required))


def _map(keys: Iterable[str], node: tuple) -> tuple:
    return ("map", frozenset(keys), node)


def _list(node: tuple) -> tuple:
    return ("list", node)


_WITH_OTHER = lambda vals: tuple(vals) + (("other",) if "other" not in vals else ())  # noqa: E731
_MODEL_KEYS = MODEL_FAMILIES
_USAGE = _obj({k: _INT for k in USAGE_KEYS})

_TREE = _obj(
    {
        "schema": _enum([SCHEMA_SHARED]),
        "id": _fmt("16 lowercase hex characters", _ID_RE),
        "ck_version": _fmt("version or other", re.compile(_VERSION_RE.pattern + "|other")),
        "claude_code_version": _fmt("version or other", re.compile(_VERSION_RE.pattern + "|other")),
        "workflow": _enum(WORKFLOWS),
        "source": _enum(SOURCES),
        "variant": _enum(VARIANTS),
        "outcome": _enum(OUTCOMES),
        "month": _fmt("yyyy-mm", _MONTH_RE),
        "machine": _fmt("slug of 1-32 lowercase letters, digits, hyphens", _MACHINE_RE),
        "stack": _enum(STACK_HINTS),
        "config": _map(CONFIG_ROLES, _obj({"model": _enum(_MODEL_KEYS), "effort": _enum(_WITH_OTHER(EFFORTS))})),
        "counts": _obj({k: _INT for k in COUNT_KEYS}),
        "agents_by_role": _map(ROLES, _INT),
        "verdicts": _map(VERDICTS, _INT),
        "durations": _obj({
            "wall_minutes": _NUM,
            "task_span_minutes": _NUM,
            "phases": _list(_obj({"phase": _enum(PHASES), "wave": _INT, "minutes": _NUM})),
            "by_role": _map(ROLES, _NUM),
        }),
        "tasks": _list(_obj({
            "index": _INT, "track": _enum(TRACKS), "ok": _BOOL, "attempts": _INT, "review_rounds": _INT,
            "verdict": _enum(VERDICTS), "resolves": _INT, "landing_fixes": _INT, "escalations": _INT,
            "risk": _enum(RISKS), "agent_minutes": _NUM, "to_merged_minutes": _NUM, "idle_minutes": _NUM,
        })),
        "tokens": _obj({
            "by_model": _map(_MODEL_KEYS, _USAGE),
            "by_role": _map(ROLES, _USAGE),
        }),
        "cost": _obj({
            "usd": _NUM,
            "source": _enum(COST_SOURCES),
            "pricing_date": _fmt("yyyy-mm", _PRICING_RE),
            "estimated": _BOOL,
            "by_model": _map(_MODEL_KEYS, _NUM),
            "by_role": _map(ROLES, _NUM),
        }),
        "issues": _list(_enum(ISSUE_CODES)),
        "missing": _list(_enum(MISSING_KINDS)),
    },
    required=("schema", "id", "ck_version", "workflow", "outcome", "month"),
)


def _flatten(node: tuple, prefix: str, out: Dict[str, tuple]) -> None:
    kind = node[0]
    if kind == "obj":
        for key, child in node[1].items():
            _flatten(child, f"{prefix}.{key}" if prefix else key, out)
    elif kind == "map":
        _flatten(node[2], f"{prefix}.*", out)
    elif kind == "list":
        _flatten(node[1], f"{prefix}[]", out)
    elif kind == "enum":
        out[prefix] = ("enum", tuple(sorted(node[1])))
    elif kind == "fmt":
        out[prefix] = ("format", node[1])
    else:
        out[prefix] = (kind,)


def _build_schema() -> Dict[str, tuple]:
    out: Dict[str, tuple] = {}
    _flatten(_TREE, "", out)
    return out


SHAREABLE_SCHEMA: Dict[str, tuple] = _build_schema()
SHAREABLE_FIELDS: Tuple[str, ...] = tuple(sorted(SHAREABLE_SCHEMA))


def schema_field_paths() -> List[str]:
    return list(SHAREABLE_FIELDS)


def _check(node: tuple, value: Any, path: str, errors: List[str]) -> None:
    kind = node[0]
    if kind == "obj":
        if not isinstance(value, Mapping):
            errors.append(f"{path or '<record>'}: expected an object")
            return
        for key in node[2]:
            if key not in value:
                errors.append(f"{path + '.' if path else ''}{key}: required")
        for key, child_value in value.items():
            child = node[1].get(key) if isinstance(key, str) else None
            if child is None:
                errors.append(f"{path + '.' if path else ''}{_safe_key(key)}: not an allowed field")
                continue
            _check(child, child_value, f"{path}.{key}" if path else key, errors)
    elif kind == "map":
        if not isinstance(value, Mapping):
            errors.append(f"{path}: expected an object")
            return
        for key, child_value in value.items():
            if not isinstance(key, str) or key not in node[1]:
                errors.append(f"{path}.*: key not in the fixed set")
                continue
            _check(node[2], child_value, f"{path}.{key}", errors)
    elif kind == "list":
        if not isinstance(value, list):
            errors.append(f"{path}: expected a list")
            return
        for item in value:
            _check(node[1], item, f"{path}[]", errors)
    elif kind == "enum":
        if not (isinstance(value, str) and value in node[1]):
            errors.append(f"{path}: not in the fixed set")
    elif kind == "fmt":
        if not (isinstance(value, str) and node[2].fullmatch(value)):
            errors.append(f"{path}: not in the {node[1]} format")
    elif kind == "int":
        if not (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
            errors.append(f"{path}: expected a non-negative integer")
    elif kind == "num":
        if not (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0):
            errors.append(f"{path}: expected a non-negative number")
    elif kind == "bool":
        if not isinstance(value, bool):
            errors.append(f"{path}: expected true or false")


def _safe_key(key: Any) -> str:
    return "<key>"


def validate_shareable(record: Any) -> List[str]:
    """[] when every path, type, enum and format of `record` is in SHAREABLE_SCHEMA. Messages never echo values."""
    errors: List[str] = []
    _check(_TREE, record, "", errors)
    return errors


# ---------------------------------------------------------------- the builder (S14)


def _int(v: Any) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _num(v: Any, digits: int = 2) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        return None
    return round(float(v), digits)


def _dicts(v: Any) -> List[Mapping]:
    return [x for x in v if isinstance(x, Mapping)] if isinstance(v, list) else []


def _sum(values: Iterable[Optional[int]]) -> Optional[int]:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _version(v: Any) -> str:
    return v if isinstance(v, str) and v.isascii() and _VERSION_RE.fullmatch(v) else "other"


_CREATE_OUTCOMES = {"Ready": "ready", "Ready after decisions": "ready_after_decisions", "Needs work": "needs_work"}


def _outcome(local: Mapping) -> str:
    raw = local.get("outcome")
    if isinstance(raw, str) and raw in _CREATE_OUTCOMES:
        raw = _CREATE_OUTCOMES[raw]
    if raw is None and local.get("status") == "failed":
        return "failed"
    return enum_value("outcome", raw)


_PHASE_NAMES = {
    "plan": "plan", "tasks": "tasks", "final_gate": "final_gate", "report": "report",
    "requirements": "requirements", "design_test_plan": "design_test_plan", "task_tracks": "task_tracks",
    "link": "link", "adversarial_review": "adversarial_review", "revise": "revise",
    "consistency_check": "consistency_check",
}


def _phase(name: Any) -> Tuple[str, Optional[int]]:
    if not isinstance(name, str) or not name.isascii():
        return "other", None
    m = _WAVE_RE.fullmatch(name)
    if m:
        return "wave", int(m.group(1))
    key = re.sub(r"[^a-z]+", "_", name.lower()).strip("_")
    return _PHASE_NAMES.get(key, "other"), None


def _phases(local: Mapping) -> List[dict]:
    raw = local.get("phases")
    items: List[Tuple[Any, Any]] = []
    if isinstance(raw, list):
        items = [(p.get("group"), p.get("minutes")) for p in _dicts(raw)]
    elif isinstance(raw, Mapping):
        items = list(raw.items())
    out = []
    for name, minutes in items:
        phase, wave = _phase(name)
        m = _num(minutes)
        if m is None:
            continue
        row: Dict[str, Any] = {"phase": phase, "minutes": m}
        if wave is not None:
            row["wave"] = wave
        out.append(row)
    return out


def _merge_add(table: Dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    table[key] = table.get(key, 0) + value


def _usage_tables(local: Mapping, key: str, enum_field: str) -> Tuple[Dict[str, dict], Dict[str, float], Dict[str, float]]:
    """Merge a local `by*` table into enum-keyed usage, usd and minutes tables."""
    usage: Dict[str, dict] = {}
    usd: Dict[str, float] = {}
    minutes: Dict[str, float] = {}
    table = local.get(key)
    if not isinstance(table, Mapping):
        return usage, usd, minutes
    for raw_key, cell in table.items():
        if not isinstance(cell, Mapping):
            continue
        k = enum_value(enum_field, raw_key)
        for field in USAGE_KEYS:
            v = _int(cell.get(field))
            if v is not None:
                row = usage.setdefault(k, {})
                row[field] = row.get(field, 0) + v
        c = _num(cell.get("usd"), 6)
        if c is not None:
            usd[k] = usd.get(k, 0.0) + c
        ms = cell.get("ms")
        if _num(ms) is not None:
            minutes[k] = minutes.get(k, 0.0) + ms / 60000
    return usage, usd, minutes


def _tasks(local: Mapping) -> List[dict]:
    out = []
    for i, t in enumerate(_dicts(local.get("tasks"))):
        row: Dict[str, Any] = {"index": i + 1, "track": to_enum(t.get("track"), TRACKS)}
        if isinstance(t.get("ok"), bool):
            row["ok"] = t["ok"]
        for out_key, key in (("attempts", "attempts"), ("review_rounds", "reviewRounds"), ("resolves", "resolves"),
                             ("landing_fixes", "fixes")):
            v = _int(t.get(key))
            if v is not None:
                row[out_key] = v
        if t.get("verdict") is not None:
            row["verdict"] = enum_value("verdict", t["verdict"])
        if isinstance(t.get("escalations"), list):
            row["escalations"] = len(t["escalations"])
        if t.get("riskTag") is not None:
            row["risk"] = to_enum(t["riskTag"], RISKS)
        for out_key, key in (("agent_minutes", "agentMinutes"), ("to_merged_minutes", "toMergedMinutes")):
            v = _num(t.get(key))
            if v is not None:
                row[out_key] = v
        idle = t.get("idleBeforeMergeMs")
        if _num(idle) is not None:
            row["idle_minutes"] = round(idle / 60000, 2)
        out.append(row)
    return out


def _counts(local: Mapping, tasks: List[Mapping], by_role: Dict[str, int]) -> Dict[str, int]:
    c: Dict[str, Any] = {}
    stats = local.get("stats") if isinstance(local.get("stats"), Mapping) else {}
    raw_tasks = _dicts(local.get("tasks"))
    if isinstance(local.get("tasks"), list):
        c["tasks"] = len(local["tasks"])
        c["tasks_merged"] = sum(1 for t in raw_tasks if t.get("ok") is True)
        c["tasks_failed"] = sum(1 for t in raw_tasks if t.get("ok") is False)
        waves = {t.get("wave") for t in raw_tasks if _int(t.get("wave")) is not None}
        if waves:
            c["waves"] = len(waves)
        c["attempts"] = _sum(_int(t.get("attempts")) for t in raw_tasks)
        c["retries"] = _sum(_int(t.get("retries")) for t in raw_tasks)
        esc = [len(t["escalations"]) for t in raw_tasks if isinstance(t.get("escalations"), list)]
        c["escalations"] = sum(esc) if esc else None
        c["review_rounds"] = _sum(_int(t.get("reviewRounds")) for t in raw_tasks)
        c["conflicts"] = _sum(_int(t.get("resolves")) for t in raw_tasks)
        c["conflicts_inferred"] = sum(1 for t in raw_tasks if t.get("resolvesInferred") is True)
        c["fixes"] = _sum(_int(t.get("fixes")) for t in raw_tasks)
    c["tasks_not_started"] = _int(stats.get("notStarted"))
    c["plan_steps"] = _int(stats.get("planSteps"))
    c["plan_failures"] = _int(stats.get("planFailures"))
    if isinstance(local.get("landings"), list):
        lands = _dicts(local["landings"])
        c["landings"] = len(local["landings"])
        c["bisects"] = sum(1 for l in lands if l.get("bisected") is True)
    gates = [g.get("green") for g in _dicts(local.get("fullGates"))]
    fg = local.get("finalGate")
    if isinstance(fg, Mapping):
        gates.append(fg.get("green"))
    if isinstance(local.get("fullGates"), list) or isinstance(fg, Mapping):
        c["gates_green"] = sum(1 for g in gates if g is True)
        c["gates_red"] = sum(1 for g in gates if g is False)
    if isinstance(local.get("crossTests"), list):
        c["cross_tests"] = len(local["crossTests"])
        c["cross_test_blocking"] = _sum(_int(x.get("blocking")) for x in _dicts(local["crossTests"]))
    review = local.get("review") if isinstance(local.get("review"), Mapping) else None
    if review is not None:
        c["review_fixed"] = _int(review.get("fixed"))
        c["review_not_fixed"] = _int(review.get("notFixed"))
        c["review_escalated"] = _int(review.get("escalated"))
        c["refuted"] = _int(review.get("refuted"))
        c["lenses_lost"] = _int(review.get("lensesLost"))
    cons = local.get("consistency") if isinstance(local.get("consistency"), Mapping) else None
    if cons is not None:
        c["consistency_passes"] = _int(cons.get("passes"))
        c["consistency_fixed"] = _int(cons.get("fixed"))
    c["open_questions"] = _int(local.get("openQuestions"))
    if isinstance(local.get("agents"), list):
        c["agents"] = len(local["agents"])
        c["agents_other"] = by_role.get("other", 0)
    return {k: v for k, v in c.items() if v is not None and k in COUNT_KEYS}


def _config(local: Mapping) -> Dict[str, dict]:
    raw = local.get("agentsConfig")
    out: Dict[str, dict] = {}
    if not isinstance(raw, Mapping):
        return out
    for role, cfg in raw.items():
        if not isinstance(role, str):
            continue
        key = role.replace("-", "_")
        if key not in CONFIG_ROLES or not isinstance(cfg, Mapping):
            continue
        # an unset model or effort is inherited from the session
        model, effort = cfg.get("model"), cfg.get("effort")
        out[key] = {
            "model": model_alias(model) if model is not None else "inherit",
            "effort": to_enum(effort, EFFORTS) if effort is not None else "inherit",
        }
    return out


_MISSING_MAP = {
    "transcript": "transcripts", "transcript-unparseable": "transcripts", "transcript-truncated": "transcripts",
    "meta": "meta", "ckVersion": "ck_version", "result": "result", "runfile": "runfile",
    "agent_entry": "agent_entries", "timing": "timing",
    "manifest": "runfile", "outcome": "result",
}


def source_of(local: Mapping) -> str:
    """A15: "agents" iff the local record says so, else "workflow"."""
    return "agents" if isinstance(local, Mapping) and local.get("source") == "agents" else "workflow"


def _missing(local: Mapping) -> List[str]:
    found = set()
    for item in local.get("missing") if isinstance(local.get("missing"), list) else []:
        what = item.get("what") if isinstance(item, Mapping) else item
        if isinstance(what, str) and what in _MISSING_MAP:
            found.add(_MISSING_MAP[what])
    return sorted(found)


def _cost(local: Mapping, usd_by_model: Dict[str, float], usd_by_role: Dict[str, float]) -> Dict[str, Any]:
    cost: Dict[str, Any] = {}
    run = next((c for c in _dicts(local.get("costs")) if c.get("scope") == "run"), None)
    if run is not None:
        # the source is read from the run figure only, never from the session-wide `claude_total` figure
        source = to_enum(run.get("source"), COST_SOURCES)
        cost["source"] = "estimate" if source == "other" else source
        usd = _num(run.get("usd"), 9)
        if usd is not None:
            cost["usd"] = usd
        pd = run.get("pricingDate")
        if isinstance(pd, str) and _PRICING_RE.fullmatch(pd):
            cost["pricing_date"] = pd
        cost["estimated"] = cost["source"] == "estimate"
    if usd_by_model:
        cost["by_model"] = {k: round(v, 4) for k, v in usd_by_model.items()}
    if usd_by_role:
        cost["by_role"] = {k: round(v, 4) for k, v in usd_by_role.items()}
    return cost


def build_shareable(local: Mapping, *, record_id: Optional[str] = None, machine_label: Optional[str] = None,
                    issue_codes: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """S14: a new shareable record from named fields of a local record. Never mutates `local`."""
    if not isinstance(local, Mapping):
        raise TypeError("a local record is a mapping")
    if record_id is None:
        record_id = new_record_id()
    elif not (isinstance(record_id, str) and _ID_RE.fullmatch(record_id)):
        raise ValueError("record_id must be 16 lowercase hex characters")
    codes = derive_issue_codes(local) if issue_codes is None else issue_codes

    out: Dict[str, Any] = {
        "schema": SCHEMA_SHARED,
        "id": record_id,
        "ck_version": _version(local.get("ckVersion")),
        "workflow": to_enum(local.get("workflow"), WORKFLOWS),
        "source": source_of(local),
        "outcome": _outcome(local),
        "month": month_of(local.get("startTime")),
    }
    ccv = local.get("claudeCodeVersion")
    if ccv is not None:
        out["claude_code_version"] = _version(ccv)
    name = local.get("workflowName")
    out["variant"] = ("iso" if name == "spec-implement-iso" else "default" if name in ("spec-implement", "spec-create")
                      else "other")
    from claude_kiro.agents import valid_machine_label

    if valid_machine_label(machine_label):
        out["machine"] = machine_label
    project = local.get("project")
    stack = project.get("stack") if isinstance(project, Mapping) else None
    if stack is not None:
        out["stack"] = enum_value("stack", stack)

    config = _config(local)
    if config:
        out["config"] = config

    by_role_agents: Dict[str, int] = {}
    for a in _dicts(local.get("agents")):
        _merge_add(by_role_agents, enum_value("role", a.get("role")), 1)
    if by_role_agents:
        out["agents_by_role"] = by_role_agents

    tasks = _tasks(local)
    counts = _counts(local, tasks, by_role_agents)
    if counts:
        out["counts"] = counts
    if tasks:
        out["tasks"] = tasks
        verdicts: Dict[str, int] = {}
        for t in tasks:
            if "verdict" in t:
                _merge_add(verdicts, t["verdict"], 1)
        if verdicts:
            out["verdicts"] = verdicts

    model_usage, model_usd, _ = _usage_tables(local, "byModel", "model")
    role_usage, role_usd, role_minutes = _usage_tables(local, "byRole", "role")
    durations: Dict[str, Any] = {}
    wall = _num(local.get("wallMs"))
    if wall is not None:
        durations["wall_minutes"] = round(wall / 60000, 2)
    span = _num(local.get("taskSpanMinutes"))
    if span is not None:
        durations["task_span_minutes"] = span
    phases = _phases(local)
    if phases:
        durations["phases"] = phases
    if role_minutes:
        durations["by_role"] = {k: round(v, 2) for k, v in role_minutes.items()}
    if durations:
        out["durations"] = durations

    tokens: Dict[str, Any] = {}
    if model_usage:
        tokens["by_model"] = model_usage
    if role_usage:
        tokens["by_role"] = role_usage
    if tokens:
        out["tokens"] = tokens
    cost = _cost(local, model_usd, role_usd)
    if cost:
        out["cost"] = cost

    out["issues"] = sorted({c for c in codes if isinstance(c, str) and c in ISSUE_CODES})
    out["missing"] = _missing(local)
    return out
