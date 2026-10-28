"""`ck stats show`: run list, per-run breakdown, trends and text/JSON renderers.

Pure functions over local records. Records are read with `.get`, so older
schema versions show unknowns (`unknown` in text, `null` in JSON), never zeros.
"""

from dataclasses import dataclass
from statistics import median
from typing import Any, Dict, List, Mapping, Optional, Sequence
from datetime import datetime, timezone

COVERAGE: Dict[str, str] = {
    "wall": "run file durationMs: from workflow start to return; includes plan, final gate and report",
    "task_span": "first task start to last merge or finish; excludes plan, final gate and report",
    "role_phase_minutes": (
        "sum of agent durations; parallel agents overlap, so the sum can exceed wall time; "
        "excludes waits between agents"
    ),
    "idle_before_merge": "task finished to merged; waiting in the landing queue plus landing, resolve and fix time",
}

UNKNOWN = "unknown"
TOP_N = 5


@dataclass(frozen=True)
class ShowFilters:
    kind: Optional[str] = None
    since: Optional[str] = None
    until: Optional[str] = None
    project: Optional[str] = None


# ---------------------------------------------------------------- helpers


def _date(rec: Mapping[str, Any]) -> Optional[str]:
    ms = rec.get("start_ms")
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


def _implement_tasks(rec: Mapping[str, Any]) -> Optional[List[Mapping[str, Any]]]:
    impl = rec.get("implement")
    if not isinstance(impl, Mapping):
        return None
    tasks = impl.get("tasks")
    if not isinstance(tasks, list):
        return None
    return [t for t in tasks if isinstance(t, Mapping)]


def _task_counts(rec: Mapping[str, Any]) -> Dict[str, Optional[int]]:
    tasks = _implement_tasks(rec)
    if tasks is None:
        return {"total": None, "merged": None, "failed": None}
    return {
        "total": len(tasks),
        "merged": sum(1 for t in tasks if t.get("ok") is True),
        "failed": sum(1 for t in tasks if t.get("ok") is False),
    }


def _cost(rec: Mapping[str, Any]) -> Dict[str, Any]:
    c = rec.get("cost")
    if not isinstance(c, Mapping):
        return {"usd": None, "source": None}
    return {"usd": c.get("usd"), "source": c.get("source")}


def _minutes(ms: Any) -> Optional[float]:
    if isinstance(ms, (int, float)) and not isinstance(ms, bool):
        return ms / 60000.0
    return None


def _filtered(records: Sequence[Mapping[str, Any]], f: ShowFilters) -> List[Mapping[str, Any]]:
    out = []
    for rec in records:
        if f.kind and rec.get("workflow") != f.kind:
            continue
        if f.project:
            proj = rec.get("project")
            if not isinstance(proj, Mapping) or proj.get("pseudonym") != f.project:
                continue
        if f.since or f.until:
            d = _date(rec)
            if d is None:
                continue
            if f.since and d < f.since:
                continue
            if f.until and d[: len(f.until)] > f.until:
                continue
        out.append(rec)
    out.sort(key=lambda r: (r.get("start_ms") if isinstance(r.get("start_ms"), (int, float)) else -1), reverse=True)
    return out


def _share(buckets: Any, key: str) -> Dict[str, float]:
    if not isinstance(buckets, Mapping):
        return {}
    vals = {}
    for role, b in buckets.items():
        v = b.get(key) if isinstance(b, Mapping) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            vals[role] = float(v)
    total = sum(vals.values())
    if total <= 0:
        return {}
    return {r: v / total for r, v in sorted(vals.items())}


def _div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or not b:
        return None
    return a / b


# ------------------------------------------------------------------- list


def list_runs(records: Sequence[Mapping[str, Any]], filters: Optional[ShowFilters] = None) -> Dict[str, Any]:
    filters = filters or ShowFilters()
    rows = []
    for rec in _filtered(records, filters):
        counts = _task_counts(rec)
        cost = _cost(rec)
        rows.append(
            {
                "run_id": rec.get("run_id"),
                "start_ms": rec.get("start_ms"),
                "date": _date(rec),
                "workflow": rec.get("workflow"),
                "outcome": rec.get("outcome"),
                "wall_ms": rec.get("wall_ms"),
                "wall_minutes": _minutes(rec.get("wall_ms")),
                "cost_usd": cost["usd"],
                "cost_source": cost["source"],
                "tasks_total": counts["total"],
                "tasks_merged": counts["merged"],
                "tasks_failed": counts["failed"],
            }
        )
    return {"runs": rows, "trends": trends(records, filters), "unreadable": 0}


# ------------------------------------------------------------------ trends


def _run_trend_row(rec: Mapping[str, Any]) -> Dict[str, Any]:
    counts = _task_counts(rec)
    usd = _cost(rec)["usd"]
    wall_min = _minutes(rec.get("wall_ms"))
    cost_shares = _share(rec.get("by_role"), "cost_usd")
    time_shares = _share(rec.get("by_role"), "minutes")
    roles = sorted(set(cost_shares) | set(time_shares))
    return {
        "run_id": rec.get("run_id"),
        "cost_per_merged_task": _div(usd, counts["merged"]),
        "wall_minutes_per_task": _div(wall_min, counts["total"]),
        "role_share": {r: {"cost": cost_shares.get(r), "time": time_shares.get(r)} for r in roles},
    }


def _mean(vals: List[Optional[float]]) -> Optional[float]:
    xs = [v for v in vals if v is not None]
    return sum(xs) / len(xs) if xs else None


def trends(records: Sequence[Mapping[str, Any]], filters: Optional[ShowFilters] = None) -> List[Dict[str, Any]]:
    filters = filters or ShowFilters()
    by_kind: Dict[str, List[Mapping[str, Any]]] = {}
    for rec in _filtered(records, filters):
        kind = rec.get("workflow")
        if isinstance(kind, str):
            by_kind.setdefault(kind, []).append(rec)
    out = []
    for kind in sorted(by_kind):
        recs = by_kind[kind]
        if len(recs) < 2:
            continue
        rows = [_run_trend_row(r) for r in recs]
        roles = sorted({r for row in rows for r in row["role_share"]})
        out.append(
            {
                "workflow": kind,
                "runs": len(recs),
                "cost_per_merged_task": _mean([r["cost_per_merged_task"] for r in rows]),
                "wall_minutes_per_task": _mean([r["wall_minutes_per_task"] for r in rows]),
                "role_cost_share": {
                    r: v for r in roles
                    if (v := _mean([row["role_share"].get(r, {}).get("cost") for row in rows])) is not None
                },
                "role_time_share": {
                    r: v for r in roles
                    if (v := _mean([row["role_share"].get(r, {}).get("time") for row in rows])) is not None
                },
                "run_rows": rows,
            }
        )
    return out


# ------------------------------------------------------------- run detail


def _bucket_view(buckets: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(buckets, Mapping):
        return {}
    out = {}
    for k in sorted(buckets):
        b = buckets[k]
        if not isinstance(b, Mapping):
            continue
        out[k] = {
            "agents": b.get("agents"),
            "minutes": b.get("minutes"),
            "cost_usd": b.get("cost_usd"),
            "estimated": b.get("estimated"),
        }
    return out


def _rate(tasks: Optional[List[Mapping[str, Any]]], fn) -> Optional[float]:
    if not tasks:
        return None
    vals = []
    for t in tasks:
        v = fn(t)
        if v is None:
            continue
        vals.append(v)
    if not vals:
        return None
    return sum(vals) / len(tasks)


def _escalations(t: Mapping[str, Any]) -> Optional[int]:
    e = t.get("escalations")
    return len(e) if isinstance(e, list) else None


def run_breakdown(record: Mapping[str, Any]) -> Dict[str, Any]:
    tasks = _implement_tasks(record)
    impl = record.get("implement") if isinstance(record.get("implement"), Mapping) else {}
    by_task = record.get("by_task") if isinstance(record.get("by_task"), Mapping) else {}

    slowest: List[Dict[str, Any]] = []
    costliest: List[Dict[str, Any]] = []
    if tasks:
        timed = [t for t in tasks if isinstance(t.get("agent_minutes"), (int, float))]
        timed.sort(key=lambda t: t["agent_minutes"], reverse=True)
        slowest = [{"task": t.get("task"), "minutes": t["agent_minutes"]} for t in timed[:TOP_N]]
        priced = []
        for t in tasks:
            b = by_task.get(t.get("task"))
            usd = b.get("cost_usd") if isinstance(b, Mapping) else None
            if isinstance(usd, (int, float)):
                priced.append({"task": t.get("task"), "cost_usd": usd})
        priced.sort(key=lambda x: x["cost_usd"], reverse=True)
        costliest = priced[:TOP_N]

    idles = [
        t["idle_before_merge_minutes"]
        for t in (tasks or [])
        if isinstance(t.get("idle_before_merge_minutes"), (int, float))
    ]
    idle = {
        "total": sum(idles) if idles else None,
        "median": median(idles) if idles else None,
        "max": max(idles) if idles else None,
    }

    landings_raw = impl.get("landings") if isinstance(impl, Mapping) else None
    if isinstance(landings_raw, list):
        ls = [x for x in landings_raw if isinstance(x, Mapping)]
        sizes = [x["size"] for x in ls if isinstance(x.get("size"), (int, float))]

        def _sum(key):
            return sum(int(x[key]) for x in ls if isinstance(x.get(key), (int, float)))

        landings = {
            "count": len(ls),
            "mean_size": (sum(sizes) / len(sizes)) if sizes else None,
            "bisected": _sum("bisected"),
            "red": _sum("red"),
        }
    else:
        landings = {"count": None, "mean_size": None, "bisected": None, "red": None}

    other = record.get("other_role") if isinstance(record.get("other_role"), Mapping) else {}
    counts = _task_counts(record)
    cost = _cost(record)
    return {
        "run": {
            "run_id": record.get("run_id"),
            "date": _date(record),
            "start_ms": record.get("start_ms"),
            "workflow": record.get("workflow"),
            "workflow_name": record.get("workflow_name"),
            "outcome": record.get("outcome"),
            "wall_minutes": _minutes(record.get("wall_ms")),
            "task_span_minutes": impl.get("task_span_minutes") if isinstance(impl, Mapping) else None,
            "cost_usd": cost["usd"],
            "cost_source": cost["source"],
            "tasks_total": counts["total"],
            "tasks_merged": counts["merged"],
            "tasks_failed": counts["failed"],
        },
        "by_role": _bucket_view(record.get("by_role")),
        "by_phase": _bucket_view(record.get("by_phase")),
        "by_model": _bucket_view(record.get("by_model")),
        "slowest_tasks": slowest,
        "costliest_tasks": costliest,
        "rates": {
            "retry": _rate(tasks, lambda t: t.get("retries")),
            "escalation": _rate(tasks, _escalations),
            "review_round": _rate(tasks, lambda t: t.get("review_rounds")),
            "conflict": _rate(tasks, lambda t: t.get("resolves")),
            "fix": _rate(tasks, lambda t: t.get("landing_fixes")),
        },
        "idle_before_merge": idle,
        "landings": landings,
        "other_role": {
            "agents": other.get("agents"),
            "minutes": other.get("minutes"),
            "cost_usd": other.get("cost_usd"),
        },
        "coverage": dict(COVERAGE),
        "missing": list(record.get("missing") or []),
    }


# --------------------------------------------------------------- renderers


def _fmt(v: Any, spec: str = "") -> str:
    if v is None:
        return UNKNOWN
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, float) and spec:
        return format(v, spec)
    return str(v)


def _usd(v: Any) -> str:
    return UNKNOWN if v is None else f"${v:.2f}"


def _min(v: Any) -> str:
    return UNKNOWN if v is None else f"{v:.1f} min"


def render_list(view: Mapping[str, Any]) -> List[str]:
    lines: List[str] = []
    runs = view.get("runs") or []
    if not runs:
        return ["no runs match; try: ck stats collect --all"]
    lines.append("date        workflow        outcome   wall (run file)  cost            tasks (merged/failed/total)  run")
    for r in runs:
        src = r.get("cost_source")
        cost = _usd(r.get("cost_usd")) + (f" ({src})" if src else "")
        tasks = f"{_fmt(r.get('tasks_merged'))}/{_fmt(r.get('tasks_failed'))}/{_fmt(r.get('tasks_total'))}"
        lines.append(
            f"{_fmt(r.get('date'))}  {_fmt(r.get('workflow'))}  {_fmt(r.get('outcome'))}  "
            f"{_min(r.get('wall_minutes'))}  {cost}  {tasks}  {_fmt(r.get('run_id'))}"
        )
    lines.append(f"wall: {COVERAGE['wall']}")
    tr = view.get("trends") or []
    if tr:
        lines.append("")
        lines.append("trends (mean per run):")
        for t in tr:
            lines.append(
                f"  {t['workflow']} ({t['runs']} runs): cost per merged task {_usd(t.get('cost_per_merged_task'))}, "
                f"wall minutes per task {_fmt(t.get('wall_minutes_per_task'), '.1f')}"
            )
            for label, key in (("cost share", "role_cost_share"), ("time share", "role_time_share")):
                sh = t.get(key) or {}
                if sh:
                    lines.append("    " + label + ": " + ", ".join(f"{r} {v * 100:.0f}%" for r, v in sh.items()))
        lines.append(f"  role time shares: {COVERAGE['role_phase_minutes']}")
    return lines


def _bucket_lines(title: str, buckets: Mapping[str, Mapping[str, Any]]) -> List[str]:
    if not buckets:
        return [f"{title}: {UNKNOWN}"]
    lines = [f"{title}:"]
    for k, b in buckets.items():
        est = " (estimated)" if b.get("estimated") else ""
        lines.append(f"  {k}: {_fmt(b.get('agents'))} agents, {_min(b.get('minutes'))}, {_usd(b.get('cost_usd'))}{est}")
    return lines


def render_run(view: Mapping[str, Any]) -> List[str]:
    run = view.get("run") or {}
    src = run.get("cost_source")
    lines = [
        f"run {_fmt(run.get('run_id'))}: {_fmt(run.get('workflow'))}, outcome {_fmt(run.get('outcome'))}, "
        f"started {_fmt(run.get('date'))}",
        f"wall: {_min(run.get('wall_minutes'))}  [{COVERAGE['wall']}]",
        f"task span: {_min(run.get('task_span_minutes'))}  [{COVERAGE['task_span']}]",
        f"cost: {_usd(run.get('cost_usd'))}" + (f" ({src})" if src else f" ({UNKNOWN} source)"),
        f"tasks: {_fmt(run.get('tasks_merged'))} merged, {_fmt(run.get('tasks_failed'))} failed, "
        f"{_fmt(run.get('tasks_total'))} total",
        "",
    ]
    for title, key in (("by role", "by_role"), ("by phase", "by_phase"), ("by model", "by_model")):
        lines += _bucket_lines(title, view.get(key) or {})
    lines.append(f"  minutes: {COVERAGE['role_phase_minutes']}")
    lines.append("")
    sl = view.get("slowest_tasks") or []
    lines.append("slowest tasks:" if sl else f"slowest tasks: {UNKNOWN}")
    lines += [f"  {t['task']}: {_min(t['minutes'])}" for t in sl]
    co = view.get("costliest_tasks") or []
    lines.append("costliest tasks:" if co else f"costliest tasks: {UNKNOWN}")
    lines += [f"  {t['task']}: {_usd(t['cost_usd'])}" for t in co]
    rates = view.get("rates") or {}
    lines.append(
        "rates per task: "
        + ", ".join(f"{k.replace('_', ' ')} {_fmt(rates.get(k), '.2f')}" for k in ("retry", "escalation", "review_round", "conflict", "fix"))
    )
    idle = view.get("idle_before_merge") or {}
    lines.append(
        f"idle before merge: total {_min(idle.get('total'))}, median {_min(idle.get('median'))}, "
        f"max {_min(idle.get('max'))}  [{COVERAGE['idle_before_merge']}]"
    )
    ld = view.get("landings") or {}
    lines.append(
        f"landings: {_fmt(ld.get('count'))}, mean size {_fmt(ld.get('mean_size'), '.1f')}, "
        f"bisected {_fmt(ld.get('bisected'))}, red {_fmt(ld.get('red'))}"
    )
    o = view.get("other_role") or {}
    lines.append(f"other: {_fmt(o.get('agents'))} agents, {_min(o.get('minutes'))}, {_usd(o.get('cost_usd'))}")
    miss = view.get("missing") or []
    if miss:
        lines.append("missing: " + ", ".join(str(m) for m in miss))
    return lines


# ------------------------------------------------------------ JSON builders


def list_json(view: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "runs": [
            {
                "runId": r.get("run_id"),
                "startTime": r.get("start_ms"),
                "workflow": r.get("workflow"),
                "outcome": r.get("outcome"),
                "wallMs": r.get("wall_ms"),
                "cost": {"usd": r.get("cost_usd"), "source": r.get("cost_source")},
                "tasks": {"total": r.get("tasks_total"), "merged": r.get("tasks_merged"), "failed": r.get("tasks_failed")},
            }
            for r in view.get("runs") or []
        ],
        "trends": trends_json(view.get("trends") or []),
        "unreadable": view.get("unreadable", 0),
    }


def trends_json(items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    return {
        t["workflow"]: {
            "runs": [
                {
                    "runId": r["run_id"],
                    "costPerMergedTask": r["cost_per_merged_task"],
                    "wallMinutesPerTask": r["wall_minutes_per_task"],
                    "roleShare": r["role_share"],
                }
                for r in t.get("run_rows") or []
            ]
        }
        for t in items
    }


def run_json(view: Mapping[str, Any]) -> Dict[str, Any]:
    run = view.get("run") or {}
    o = view.get("other_role") or {}
    ld = view.get("landings") or {}
    rates = view.get("rates") or {}
    return {
        "runId": run.get("run_id"),
        "startTime": run.get("start_ms"),
        "workflow": run.get("workflow"),
        "outcome": run.get("outcome"),
        "wallMinutes": run.get("wall_minutes"),
        "taskSpanMinutes": run.get("task_span_minutes"),
        "cost": {"usd": run.get("cost_usd"), "source": run.get("cost_source")},
        "tasks": {"total": run.get("tasks_total"), "merged": run.get("tasks_merged"), "failed": run.get("tasks_failed")},
        "byRole": view.get("by_role") or {},
        "byPhase": view.get("by_phase") or {},
        "byModel": view.get("by_model") or {},
        "slowestTasks": view.get("slowest_tasks") or [],
        "costliestTasks": view.get("costliest_tasks") or [],
        "rates": {
            "retry": rates.get("retry"),
            "escalation": rates.get("escalation"),
            "reviewRound": rates.get("review_round"),
            "conflict": rates.get("conflict"),
            "fix": rates.get("fix"),
        },
        "idleBeforeMergeMs": {
            k: (v * 60000 if isinstance(v, (int, float)) else None)
            for k, v in (view.get("idle_before_merge") or {}).items()
        },
        "landingSummary": {
            "count": ld.get("count"),
            "meanSize": ld.get("mean_size"),
            "bisected": ld.get("bisected"),
            "red": ld.get("red"),
        },
        "other": {"agents": o.get("agents"), "minutes": o.get("minutes"), "usd": o.get("cost_usd")},
        "coverage": view.get("coverage") or dict(COVERAGE),
        "missing": view.get("missing") or [],
    }
