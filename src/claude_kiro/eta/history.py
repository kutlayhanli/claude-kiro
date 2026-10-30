"""History model for the run ETA: records in, a numbers-only model snapshot out (run-followups, design Data Models).

Pure except `load_defaults` (reads the bundled or overridden defaults file) and the default `tasks_md_reader`
(reads a spec's tasks.md; tests and `build_defaults` pass their own or none).

Bundled parts. `Model.bundled_parts` names, over the whole model, every distribution that fell back to the bundled
defaults: `durations`-class names are `durations`, `retries`, `escalations`, `review wait`, `landing wait`, `cost`,
`size` (implement runs) and `phases` (create runs). The model cannot know which kind of run a view is for, so the
service keeps the parts that belong to its kind (`phases` for create, the others for implement) when it reports
`basis.bundledParts` and `basis.source`. Every part depends only on a sample count that can only shrink when
records are removed, so removing records never removes a part (P-11).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, FrozenSet, List, Literal, Mapping, Optional, Sequence, Tuple

MIN_MATCH = 8
MIN_SIZED = 30
MIN_DEFAULT_RUNS = 20
N_IMPLEMENT = 1000
N_CREATE = 4000

CREATE_PHASES = (
    "Requirements",
    "Design + Test Plan",
    "Task Tracks",
    "Link",
    "Adversarial Review",
    "Revise",
    "Consistency Check",
)

MODEL_SCHEMA = "ck-eta-model/1"
DEFAULTS_SCHEMA = "ck-eta-defaults/1"
QUANTILE_POINTS = tuple(range(0, 101, 5))
TASK_KEYS = ("impl|normal", "impl|safety", "test|normal", "test|safety")
WAIT_KEYS = ("reviewMinutes", "landingMinutes")
DEFAULTS_FAMILIES = ("schema", "quantiles", "tasks", "waits", "usdPerAgentMinute", "phases", "size")
FAMILIES = ("haiku", "sonnet", "opus", "fable")
MAX_EXPONENT = 1.5

TRACKS = ("impl", "test")
STEPS = ("track+model+risk", "track+risk", "track", "all", "bundled", "none")

# A task tuple: [agentMin, retries, escalations, reviewWaitMin, landingWaitMin, usd, size]
Tuple7 = Tuple[float, int, int, Optional[float], Optional[float], Optional[float], Optional[int]]


# --------------------------------------------------------------------------- types


@dataclass(frozen=True)
class Graph:  # from cli.worktree._plan (ck plan --json)
    order: Tuple[str, ...]
    deps: Dict[str, Tuple[str, ...]]
    done: FrozenSet[str]
    chain: Dict[str, int]
    tracks: Dict[str, str]
    risk: Dict[str, str]
    complexity: Dict[str, Optional[str]]
    sizes: Dict[str, Optional[int]]
    critical: Tuple[str, ...]
    cycles: Tuple[Tuple[str, ...], ...]
    unknown_deps: Dict[str, Tuple[str, ...]]
    slots: int
    slots_source: Literal["runtime", "default"]


@dataclass(frozen=True)
class PoolMatch:
    samples: Tuple[Tuple, ...]
    quantiles: Optional[dict]
    step: Literal["track+model+risk", "track+risk", "track", "all", "bundled", "none"]


@dataclass(frozen=True)
class SizeFit:
    exponent: float
    min_size: int
    median_size: int
    source: Literal["local", "bundled", "none"]


@dataclass(frozen=True)
class Model:
    basis: Dict[str, Any]
    bundled_parts: Tuple[str, ...]
    task_pools: Dict[str, Tuple[Tuple, ...]]
    bundled_tasks: Dict[str, dict]
    bundled_waits: Dict[str, Tuple[float, ...]]
    bundled_usd: Tuple[float, ...]
    size: SizeFit
    phases: Dict[str, dict]
    phase_source: str
    create_totals: Tuple[float, ...]
    rates: Dict[str, Optional[float]]
    digest: str = ""


@dataclass(frozen=True)
class DefaultsResult:
    payload: dict
    runs: int


# --------------------------------------------------------------------------- small helpers


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _is_num(v: Any) -> bool:
    return _num(v) is not None


def _int(v: Any) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _r(x: float) -> float:
    return round(float(x), 6)


def _sort_key(t: Sequence) -> Tuple:
    return tuple((x is None, 0 if x is None else x) for x in t)


def family_of(text: Any) -> str:
    """'claude-opus-5-5' -> 'opus'; 'inherit' -> 'inherit'; anything else (or no model) -> 'unknown'."""
    low = text.lower() if isinstance(text, str) else ""
    if "inherit" in low:
        return "inherit"
    for fam in FAMILIES:
        if fam in low:
            return fam
    return "unknown"


def quantiles(values: Sequence[float]) -> Optional[List[float]]:
    """21 points (0, 5, ..., 100) by linear interpolation of the sorted values; None for no values."""
    xs = sorted(values)
    if not xs:
        return None
    n = len(xs)
    out = []
    for p in QUANTILE_POINTS:
        pos = p / 100 * (n - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, n - 1)
        out.append(_r(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)))
    return out


# --------------------------------------------------------------------------- size (S2, S3)


def size_factor(fit: SizeFit, task_size: Optional[int], sample_size: Optional[int]) -> float:
    """S2: (max(task, min) / max(sample or median, min)) ** exponent; 1.0 for no fit or no size; never 0."""
    if fit.source == "none" or task_size is None:
        return 1.0
    lo = max(int(fit.min_size), 1)
    num = max(task_size, lo)
    den = max(sample_size or fit.median_size, lo)
    value = (num / den) ** fit.exponent
    return value if value > 0 else 1.0


def fit_size(samples: Sequence[Tuple[str, float, Optional[int]]], bundled: Optional[SizeFit], min_sized: int = MIN_SIZED) -> SizeFit:
    """S3: a local fit only with >= min_sized sized samples (group-centred log-log least squares, clamped), else bundled, else none."""
    sized = [(g, m, s) for g, m, s in samples if s is not None and m is not None and m > 0 and s >= 0]
    if len(sized) < min_sized:
        return bundled if bundled is not None else SizeFit(0.0, 1, 1, "none")
    positive = [s for _, _, s in sized if s > 0]
    min_size = max(1, min(positive)) if positive else 1
    eff = sorted(max(s, min_size) for _, _, s in sized)
    median_size = max(min_size, int(round(eff[len(eff) // 2] if len(eff) % 2 else (eff[len(eff) // 2 - 1] + eff[len(eff) // 2]) / 2)))
    groups: Dict[str, List[Tuple[float, float]]] = {}
    for g, m, s in sized:
        groups.setdefault(g, []).append((math.log(max(s, min_size)), math.log(m)))
    sxx = sxy = 0.0
    for pts in groups.values():
        if len(pts) < 2:
            continue
        mx = sum(x for x, _ in pts) / len(pts)
        my = sum(y for _, y in pts) / len(pts)
        for x, y in pts:
            sxx += (x - mx) ** 2
            sxy += (x - mx) * (y - my)
    b = sxy / sxx if sxx > 1e-12 else 0.0
    b = min(max(b, 0.0), MAX_EXPONENT)
    return SizeFit(_r(b), min_size, median_size, "local")


# --------------------------------------------------------------------------- defaults shape (S18)


def _check_q(v: Any, path: str, problems: List[str]) -> None:
    if not isinstance(v, list) or len(v) != len(QUANTILE_POINTS) or not all(_is_num(x) for x in v):
        problems.append(f"{path}: expected {len(QUANTILE_POINTS)} finite numbers")


def _check_rate(v: Any, path: str, problems: List[str]) -> None:
    if not _is_num(v) or not 0 <= v <= 1:
        problems.append(f"{path}: expected a finite number from 0 to 1")


def _check_part(name: str, value: Any, problems: List[str]) -> None:
    if name == "schema":
        if value != DEFAULTS_SCHEMA:
            problems.append("schema: unknown schema id")
    elif name == "quantiles":
        if value != list(QUANTILE_POINTS) or any(isinstance(x, bool) for x in value):
            problems.append("quantiles: expected the 21 fixed points")
    elif name == "tasks":
        if not isinstance(value, dict):
            problems.append("tasks: expected an object")
            return
        for k, v in value.items():
            if k not in TASK_KEYS:
                problems.append(f"tasks.{k}: unknown key")
            elif not isinstance(v, dict) or set(v) != {"agentMinutes", "retryRate", "escalationRate"}:
                problems.append(f"tasks.{k}: expected agentMinutes, retryRate, escalationRate")
            else:
                _check_q(v["agentMinutes"], f"tasks.{k}.agentMinutes", problems)
                _check_rate(v["retryRate"], f"tasks.{k}.retryRate", problems)
                _check_rate(v["escalationRate"], f"tasks.{k}.escalationRate", problems)
    elif name == "waits":
        if not isinstance(value, dict):
            problems.append("waits: expected an object")
            return
        for k, v in value.items():
            if k not in WAIT_KEYS:
                problems.append(f"waits.{k}: unknown key")
            else:
                _check_q(v, f"waits.{k}", problems)
    elif name == "usdPerAgentMinute":
        _check_q(value, "usdPerAgentMinute", problems)
    elif name == "phases":
        if not isinstance(value, dict):
            problems.append("phases: expected an object")
            return
        for k, v in value.items():
            if k not in CREATE_PHASES:
                problems.append(f"phases.{k}: unknown phase")
            elif not isinstance(v, dict) or set(v) != {"presence", "minutes"}:
                problems.append(f"phases.{k}: expected presence, minutes")
            else:
                _check_rate(v["presence"], f"phases.{k}.presence", problems)
                _check_q(v["minutes"], f"phases.{k}.minutes", problems)
    elif name == "size":
        if not isinstance(value, dict) or set(value) != {"exponent", "minSize", "medianSize"}:
            problems.append("size: expected exponent, minSize, medianSize")
            return
        e, lo, med = value["exponent"], _int(value["minSize"]), _int(value["medianSize"])
        if not _is_num(e) or not 0 <= e <= MAX_EXPONENT:
            problems.append("size.exponent: expected a finite number from 0 to 1.5")
        if lo is None or lo < 1:
            problems.append("size.minSize: expected a whole number >= 1")
        if med is None or (lo is not None and med < lo):
            problems.append("size.medianSize: expected a whole number >= minSize")


def defaults_shape_ok(payload: Any) -> Tuple[bool, List[str]]:
    """S18: only the `ck-eta-defaults/1` tree with finite numbers in every quantile and rate position."""
    if not isinstance(payload, dict):
        return False, ["payload: expected an object"]
    problems: List[str] = []
    if "schema" not in payload:
        problems.append("schema: missing")
    for key, value in payload.items():
        if key not in DEFAULTS_FAMILIES:
            problems.append(f"{key if isinstance(key, str) and len(key) <= 40 else '?'}: unknown key")
        else:
            _check_part(key, value, problems)
    return (not problems), problems


def _clean_defaults(defaults: Any) -> Optional[dict]:
    """The parts of `defaults` that pass the shape check (None for no defaults or a wrong schema)."""
    if not isinstance(defaults, Mapping) or defaults.get("schema") != DEFAULTS_SCHEMA:
        return None
    out: dict = {"schema": DEFAULTS_SCHEMA}
    for key in DEFAULTS_FAMILIES:
        if key == "schema" or key not in defaults:
            continue
        problems: List[str] = []
        _check_part(key, defaults[key], problems)
        if not problems:
            out[key] = defaults[key]
    return out


def load_defaults(env: Optional[Mapping[str, str]] = None) -> Optional[dict]:
    """The bundled defaults (or the file `CK_ETA_DEFAULTS` names): the parts present; None when missing, unreadable or malformed."""
    e = os.environ if env is None else env
    override = e.get("CK_ETA_DEFAULTS")
    try:
        if override:
            with open(override, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            from importlib import resources

            data = json.loads(resources.files("claude_kiro.resources").joinpath("eta_defaults.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, ModuleNotFoundError, UnicodeError):
        return None
    return _clean_defaults(data)


# --------------------------------------------------------------------------- start model (S19)


def _rank(model: Any) -> int:
    text = model.lower() if isinstance(model, str) else ""
    for i, fam in enumerate(FAMILIES):
        if fam in text:
            return i
    return -1


def start_model(track: str, risk: str, complexity: Optional[str], roles: Mapping) -> str:
    """S19: the model family a task starts on (port of the workflow's `codeOpts`)."""
    roles = roles if isinstance(roles, Mapping) else {}
    impl = roles.get("implementer") if isinstance(roles.get("implementer"), Mapping) else {}
    if track == "test":
        tw = roles.get("test_writer")
        role = tw if isinstance(tw, Mapping) else impl
    else:
        role = impl
    base = role.get("model") if isinstance(role.get("model"), str) and role.get("model") else None
    esc_role = role if isinstance(role.get("escalate"), list) else impl
    esc = esc_role.get("escalate") if isinstance(esc_role.get("escalate"), list) else []
    esc = [m for m in esc if isinstance(m, str)]
    ladder = esc if _rank(base) < 0 else [m for m in esc if _rank(m) > _rank(base)]
    target: Optional[str] = None
    if risk == "safety":
        risky = impl.get("risky_model") if isinstance(impl.get("risky_model"), str) and impl.get("risky_model") else None
        target = risky or (ladder[-1] if ladder else None)
    elif impl.get("complexity_routing") is True and str(complexity or "").lower() == "high":
        target = ladder[0] if ladder else None
    chosen = base
    if target and (_rank(base) < 0 or _rank(target) > _rank(base)):
        chosen = target
    return family_of(chosen) if chosen else "inherit"


# --------------------------------------------------------------------------- extraction from records


def _default_reader(root: str, spec: str) -> Optional[Dict[str, Tuple[int, int]]]:
    from claude_kiro.analytics.collect import _task_sizes

    return _task_sizes(root, spec)


@dataclass
class _Extract:
    tuples: List[Tuple[str, Tuple7]] = field(default_factory=list)
    impl_runs: int = 0
    create_timed: List[Dict[str, Tuple[float, Optional[float]]]] = field(default_factory=list)
    create_totals: List[float] = field(default_factory=list)
    create_runs: int = 0


def _rows_of(rec: Mapping, task: Any) -> List[dict]:
    agents = rec.get("agents")
    if not isinstance(agents, list):
        return []
    key = str(task)
    return [a for a in agents if isinstance(a, dict) and a.get("task") is not None and str(a["task"]) == key]


def _usd(rows: Sequence[Mapping], pick: Callable[[Mapping], bool] = lambda a: True) -> Optional[float]:
    """Sum of the rows' usd; None when there are no rows or any row is unpriced (missing, null, zero or negative)."""
    rows = [a for a in rows if pick(a)]
    if not rows:
        return None
    total = 0.0
    for a in rows:
        v = _num(a.get("usd"))
        if v is None or v <= 0:
            return None
        total += v
    return _r(total)


def _extract(records: Sequence[Mapping], reader: Optional[Callable[[str, str], Optional[Dict[str, Tuple[int, int]]]]]) -> _Extract:
    ex = _Extract()
    sizes_cache: Dict[Tuple[str, str], Optional[Dict[str, Tuple[int, int]]]] = {}

    def sizes_for(rec: Mapping) -> Optional[Dict[str, Tuple[int, int]]]:
        project = rec.get("project") if isinstance(rec.get("project"), Mapping) else {}
        root, spec = project.get("root"), project.get("spec")
        if reader is None or not isinstance(root, str) or not isinstance(spec, str):
            return None
        if (root, spec) not in sizes_cache:
            try:
                sizes_cache[(root, spec)] = reader(root, spec)
            except Exception:  # noqa: BLE001 - an unreadable spec just leaves its tasks unsized
                sizes_cache[(root, spec)] = None
        return sizes_cache[(root, spec)]

    for rec in records:
        if not isinstance(rec, Mapping):
            continue
        if rec.get("workflow") == "spec-create":
            ex.create_runs += 1
            phases = rec.get("phases")
            timed = {}
            if isinstance(phases, dict):
                for title, minutes in phases.items():
                    m = _num(minutes)
                    if m is not None and m > 0:
                        timed[title] = m
            if not timed:
                wall = _num(rec.get("wallMs"))
                if wall is not None and wall > 0:
                    ex.create_totals.append(_r(wall / 60000))
                continue
            sample: Dict[str, Tuple[float, Optional[float]]] = {}
            agents = rec.get("agents") if isinstance(rec.get("agents"), list) else []
            for title, minutes in timed.items():
                if title in CREATE_PHASES:
                    rows = [a for a in agents if isinstance(a, dict) and a.get("phase") == title]
                    sample[title] = (_r(minutes), _usd(rows))
            ex.create_timed.append(sample)
            continue
        tasks = rec.get("tasks")
        if not isinstance(tasks, list):
            continue
        ex.impl_runs += 1
        sizes: Any = False  # not read yet
        for t in tasks:
            if not isinstance(t, dict) or t.get("ok") is not True:
                continue
            minutes = _num(t.get("agentMinutes"))
            if minutes is None or minutes < 0:
                continue
            rows = _rows_of(rec, t.get("task"))
            first = next((a for a in rows if a.get("role") in ("implementer", "test-writer")), None)
            fam = family_of(first.get("model")) if first else "unknown"
            track = t.get("track") if t.get("track") in TRACKS else "unknown"
            risk = "safety" if t.get("riskTag") == "safety" else "normal"
            attempts = _int(t.get("attempts"))
            retries = attempts - 1 if attempts is not None and attempts >= 1 else 0
            esc = t.get("escalations")
            escalations = len(esc) if isinstance(esc, list) else 0
            files, crit = _int(t.get("filesCount")), _int(t.get("criteriaCount"))
            size: Optional[int] = files + crit if files is not None and crit is not None else None
            if size is None:
                if sizes is False:
                    sizes = sizes_for(rec)
                got = sizes.get(str(t.get("task"))) if isinstance(sizes, dict) else None
                if got is not None:
                    size = int(got[0]) + int(got[1])
            review = _num(t.get("reviewWaitMs"))
            landing = _num(t.get("landingWaitMs"))
            tup: Tuple7 = (_r(minutes), retries, escalations, None if review is None else _r(review / 60000),
                           None if landing is None else _r(landing / 60000), _usd(rows), size)
            ex.tuples.append((f"{track}|{fam}|{risk}", tup))
    return ex


# --------------------------------------------------------------------------- fit_model


def fit_model(records: Sequence[Mapping], defaults: Optional[Mapping], *,
              tasks_md_reader: Optional[Callable[[str, str], Optional[Dict[str, Tuple[int, int]]]]] = _default_reader) -> Model:
    ex = _extract(records, tasks_md_reader)
    d = _clean_defaults(defaults) or {}

    pools: Dict[str, List[Tuple7]] = {}
    for key, tup in ex.tuples:
        pools.setdefault(key, []).append(tup)
    task_pools = {k: tuple(sorted(v, key=_sort_key)) for k, v in sorted(pools.items())}
    flat = [t for v in task_pools.values() for t in v]

    bundled_tasks = {k: dict(v) for k, v in sorted((d.get("tasks") or {}).items())}
    bw = d.get("waits") or {}
    bundled_waits = {k: tuple(bw[k]) for k in WAIT_KEYS if k in bw}
    bundled_usd = tuple(d.get("usdPerAgentMinute") or ())

    parts: List[str] = []
    n_tasks = len(flat)
    local_enough = n_tasks >= MIN_MATCH
    if not local_enough and bundled_tasks:
        parts += ["durations", "retries", "escalations"]
    n_review = sum(1 for t in flat if t[3] is not None and t[3] > 0)  # zero waits are no wait data
    n_landing = sum(1 for t in flat if t[4] is not None and t[4] > 0)
    n_priced = sum(1 for t in flat if t[5] is not None)
    if n_review < MIN_MATCH and "reviewMinutes" in bundled_waits:
        parts.append("review wait")
    if n_landing < MIN_MATCH and "landingMinutes" in bundled_waits:
        parts.append("landing wait")
    if n_priced < MIN_MATCH and bundled_usd:
        parts.append("cost")

    bsz = d.get("size")
    bundled_fit = SizeFit(float(bsz["exponent"]), int(bsz["minSize"]), int(bsz["medianSize"]), "bundled") if bsz else None
    samples = [(k, t[0], t[6]) for k, v in task_pools.items() for t in v if t[6] is not None]
    size = fit_size(samples, bundled_fit)
    if size.source == "bundled":
        parts.append("size")

    # retry / escalation rates
    if local_enough:
        rates: Dict[str, Optional[float]] = {"retry": _r(sum(1 for t in flat if t[1] > 0) / n_tasks),
                                             "escalation": _r(sum(1 for t in flat if t[2] > 0) / n_tasks)}
    elif bundled_tasks:
        vals = list(bundled_tasks.values())
        rates = {"retry": _r(sum(v["retryRate"] for v in vals) / len(vals)),
                 "escalation": _r(sum(v["escalationRate"] for v in vals) / len(vals))}
    else:
        rates = {"retry": None, "escalation": None}

    # create phases
    local_phases: Dict[str, dict] = {}
    timed = ex.create_timed
    for title in CREATE_PHASES:
        got = sorted(((s[title][0], s[title][1]) for s in timed if title in s), key=_sort_key)
        if got:
            local_phases[title] = {"presence": _r(len(got) / len(timed)), "minutes": [g[0] for g in got], "usd": [g[1] for g in got]}
    bp = d.get("phases") or {}
    if len(timed) >= MIN_MATCH or (local_phases and not bp):
        phases, phase_source = local_phases, "local"
    elif bp:
        phases = {k: {"presence": float(v["presence"]), "minutes": list(v["minutes"]), "usd": [None] * len(v["minutes"])}
                  for k, v in sorted(bp.items())}
        phase_source = "bundled"
        parts.append("phases")
    else:
        phases, phase_source = {}, "none"

    n_local = n_tasks + len(timed) + len(ex.create_totals)
    if n_local == 0 and ex.impl_runs == 0 and ex.create_runs == 0:
        source = "bundled" if d else "none"
    else:
        source = "mixed" if parts else "local"
    basis = {"tasks": n_tasks, "runs": ex.impl_runs, "createRuns": ex.create_runs, "source": source}
    model = Model(basis=basis, bundled_parts=tuple(parts), task_pools=task_pools, bundled_tasks=bundled_tasks,
                  bundled_waits=bundled_waits, bundled_usd=bundled_usd, size=size, phases=phases,
                  phase_source=phase_source, create_totals=tuple(sorted(ex.create_totals)), rates=rates)
    return replace(model, digest=_digest(_body(model)))


# --------------------------------------------------------------------------- match (S1)


def match_pool(model: Model, track: str, start_model: str, risk: str, min_n: int = MIN_MATCH) -> PoolMatch:
    """S1: (track, model, risk) -> (track, risk) -> (track) -> (all) -> bundled -> none; a step needs >= min_n samples."""
    pools = model.task_pools
    if track in TRACKS:
        exact = pools.get(f"{track}|{start_model}|{risk}", ())
        if len(exact) >= min_n:
            return PoolMatch(tuple(exact), None, "track+model+risk")
        by_risk = tuple(t for k, v in pools.items() for t in v if k.split("|")[0] == track and k.split("|")[2] == risk)
        if len(by_risk) >= min_n:
            return PoolMatch(tuple(sorted(by_risk, key=_sort_key)), None, "track+risk")
        by_track = tuple(t for k, v in pools.items() for t in v if k.split("|")[0] == track)
        if len(by_track) >= min_n:
            return PoolMatch(tuple(sorted(by_track, key=_sort_key)), None, "track")
    every = tuple(t for v in pools.values() for t in v)
    if len(every) >= min_n:
        return PoolMatch(tuple(sorted(every, key=_sort_key)), None, "all")
    bt = model.bundled_tasks
    q = bt.get(f"{track}|{risk}") or bt.get(f"impl|{risk}") or bt.get(f"{track}|normal") or bt.get("impl|normal") \
        or (next(iter(bt.values())) if bt else None)
    if q is not None:
        return PoolMatch((), q, "bundled")
    return PoolMatch((), None, "none")


# --------------------------------------------------------------------------- JSON


def _body(m: Model) -> dict:
    return {
        "schema": MODEL_SCHEMA,
        "basis": dict(m.basis),
        "bundledParts": list(m.bundled_parts),
        "taskPools": {k: [list(t) for t in v] for k, v in sorted(m.task_pools.items())},
        "bundledTasks": {k: dict(v) for k, v in sorted(m.bundled_tasks.items())},
        "bundledWaits": {k: list(v) for k, v in sorted(m.bundled_waits.items())},
        "bundledUsd": list(m.bundled_usd),
        "size": {"exponent": m.size.exponent, "minSize": m.size.min_size, "medianSize": m.size.median_size, "source": m.size.source},
        "phases": {k: {"presence": v["presence"], "minutes": list(v["minutes"]), "usd": list(v["usd"])} for k, v in m.phases.items()},
        "phaseSource": m.phase_source,
        "createTotals": list(m.create_totals),
        "rates": dict(m.rates),
    }


def _digest(body: Mapping) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def model_to_json(m: Model) -> dict:
    body = _body(m)
    return {"schema": MODEL_SCHEMA, "digest": _digest(body), **{k: v for k, v in body.items() if k != "schema"}}


def model_from_json(d: Mapping) -> Optional[Model]:
    """The model a snapshot holds; None when it is malformed or its digest does not match."""
    try:
        if not isinstance(d, Mapping) or d.get("schema") != MODEL_SCHEMA:
            return None
        body = {k: v for k, v in d.items() if k != "digest"}
        if _digest(body) != d.get("digest"):
            return None
        size = body["size"]
        fit = SizeFit(float(size["exponent"]), int(size["minSize"]), int(size["medianSize"]), size["source"])
        if fit.source not in ("local", "bundled", "none"):
            return None
        return Model(
            basis=dict(body["basis"]), bundled_parts=tuple(body["bundledParts"]),
            task_pools={k: tuple(tuple(t) for t in v) for k, v in body["taskPools"].items()},
            bundled_tasks={k: dict(v) for k, v in body["bundledTasks"].items()},
            bundled_waits={k: tuple(v) for k, v in body["bundledWaits"].items()},
            bundled_usd=tuple(body["bundledUsd"]), size=fit,
            phases={k: {"presence": v["presence"], "minutes": list(v["minutes"]), "usd": list(v["usd"])} for k, v in body["phases"].items()},
            phase_source=body["phaseSource"], create_totals=tuple(body["createTotals"]), rates=dict(body["rates"]), digest=d["digest"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------- build_defaults


MAX_PLAUSIBLE = 1e5  # minutes (about 70 days) or any other aggregate input: a bigger value is a timestamp or junk


def _plausible(ex: "_Extract") -> "_Extract":
    """Drop record-sized values (epoch timestamps in a duration field) before they can reach a bundled aggregate."""
    def ok(v: Any) -> bool:
        return v is None or (isinstance(v, (int, float)) and abs(v) < MAX_PLAUSIBLE)

    tuples = []
    for k, t in ex.tuples:
        if not ok(t[0]):
            continue
        t = (t[0], t[1], t[2], t[3] if ok(t[3]) else None, t[4] if ok(t[4]) else None,
             t[5] if ok(t[5]) else None, t[6] if ok(t[6]) else None)
        tuples.append((k, t))
    timed = [{title: v for title, v in s.items() if ok(v[0]) and ok(v[1])} for s in ex.create_timed]
    return _Extract(tuples=tuples, impl_runs=ex.impl_runs, create_timed=[s for s in timed if s],
                    create_totals=[v for v in ex.create_totals if ok(v)], create_runs=ex.create_runs)


def build_defaults(records: Sequence[Mapping], *, min_runs: int = MIN_DEFAULT_RUNS,
                   tasks_md_reader: Optional[Callable[[str, str], Optional[Dict[str, Tuple[int, int]]]]] = None) -> DefaultsResult:
    """Aggregate `ck-eta-defaults/1` from records: quantiles and rates only. Raises ValueError below `min_runs` runs."""
    n = len(records)
    if n < min_runs:
        raise ValueError(f"{n} runs in history, need {min_runs}")
    ex = _extract(records, tasks_md_reader)
    ex = _plausible(ex)
    tasks: Dict[str, dict] = {}
    for track in TRACKS:
        for risk in ("normal", "safety"):
            tups = [t for k, t in ex.tuples if k.split("|")[0] == track and k.split("|")[2] == risk]
            if tups:
                tasks[f"{track}|{risk}"] = {
                    "agentMinutes": quantiles([t[0] for t in tups]),
                    "retryRate": _r(sum(1 for t in tups if t[1] > 0) / len(tups)),
                    "escalationRate": _r(sum(1 for t in tups if t[2] > 0) / len(tups)),
                }
    waits: Dict[str, list] = {}
    for name, idx in (("reviewMinutes", 3), ("landingMinutes", 4)):
        q = quantiles([t[idx] for _, t in ex.tuples if t[idx] is not None])
        if q:
            waits[name] = q
    per_min = [t[5] / t[0] for _, t in ex.tuples if t[5] is not None and t[0] > 0]
    phases: Dict[str, dict] = {}
    for title in CREATE_PHASES:
        mins = [s[title][0] for s in ex.create_timed if title in s]
        if mins:
            phases[title] = {"presence": _r(len(mins) / len(ex.create_timed)), "minutes": quantiles(mins)}
    payload: dict = {
        "schema": DEFAULTS_SCHEMA,
        "quantiles": list(QUANTILE_POINTS),
        "tasks": tasks,
        "waits": waits,
        "usdPerAgentMinute": quantiles(per_min) if per_min else [],
        "phases": phases,
    }
    if not payload["usdPerAgentMinute"]:
        del payload["usdPerAgentMinute"]
    fit = fit_size([(k, t[0], t[6]) for k, t in ex.tuples if t[6] is not None], None)
    if fit.source == "local":
        payload["size"] = {"exponent": fit.exponent, "minSize": fit.min_size, "medianSize": fit.median_size}
    ok, problems = defaults_shape_ok(payload)
    if not ok:
        raise ValueError("built defaults fail the shape check: " + "; ".join(problems))
    return DefaultsResult(payload, n)
