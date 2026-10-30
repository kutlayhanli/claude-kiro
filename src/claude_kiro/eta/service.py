"""The run ETA service: the I/O layer that wires history, simulate and decide (run-followups design, eta/service.py).

It never re-implements a rule: holds, state, slip and notice decisions, line formats and the cache check are the pure
seams in `eta.decide`; the Monte Carlo is `eta.simulate`; the model is `eta.history`; the progress file is `progress`.
The `EtaView` (`ck-eta/1`) is a plain dict, so it is also the JSON `ck eta --json` prints and the cache body.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import tempfile
import time
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from claude_kiro import agents as ag
from claude_kiro import notify as notify_mod
from claude_kiro import progress
from claude_kiro.analytics import store
from claude_kiro.eta import decide, history, simulate
from claude_kiro.eta.history import CREATE_PHASES, N_CREATE, N_IMPLEMENT, Graph, Model

SCHEMA = "ck-eta/1"
DEFAULT_LINE_BUDGET_S = 3.0
BATCH = 100
_NOTICE_KINDS = ("notified", "notify-pending")
_SECRET_KEYS = ("digest", "modelDigest")


class EtaError(Exception):
    """Base of the typed errors the CLI maps to exit codes."""

    exit_code = 1


class UnknownSpec(EtaError):
    exit_code = 2


class NoEstimate(EtaError):
    exit_code = 1


class _Timeout(Exception):
    pass


# --------------------------------------------------------------------------- small helpers


def _environ(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def _tz(env: Mapping[str, str]) -> Optional[tzinfo]:
    name = env.get("TZ")
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - fall back to the process zone
            return None
    return None


def _local(dt: datetime, env: Mapping[str, str]) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    tz = _tz(env)
    return dt.astimezone(tz) if tz is not None else dt.astimezone()


def _iso_local(dt: datetime, env: Mapping[str, str]) -> str:
    return _local(dt, env).replace(microsecond=0).isoformat()


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _secs(minutes: float) -> int:
    """Whole seconds of `minutes`, quantised first so float noise cannot flip a rounding (exact wait shifts)."""
    return int(round(round(minutes, 6) * 60))


def _at(launch: datetime, eff_min: float, waited_min: float, env: Mapping[str, str]) -> str:
    return _iso_local(launch + timedelta(seconds=_secs(eff_min) + _secs(waited_min)), env)


def _write_private(path: Path, text: str) -> None:
    """Write `text` to `path` (0600) through a fresh, exclusively created temp file in the same directory.

    The directory may be the spool, which a sandboxed child can write: a planted `<path>.tmp` symlink or a symlinked
    `path` must never make this (possibly unsandboxed) process write through to another file."""
    if path.is_symlink():
        raise OSError("refusing to write through a symlink: %s" % path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        try:
            data = text.encode("utf-8")
            while data:
                data = data[os.write(fd, data):]
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _EnvOverlay:
    """Make `agents.resolve` (which reads os.environ for the config path) see the caller's config location."""

    _KEYS = ("CK_CONFIG_HOME", "XDG_CONFIG_HOME", "HOME")

    def __init__(self, env: Mapping[str, str]):
        self.env = env
        self.saved: Dict[str, Optional[str]] = {}

    def __enter__(self) -> "_EnvOverlay":
        if self.env is os.environ:
            return self
        for k in self._KEYS:
            if k in self.env and self.env.get(k) != os.environ.get(k):
                self.saved[k] = os.environ.get(k)
                os.environ[k] = str(self.env[k])
        return self

    def __exit__(self, *exc: Any) -> None:
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _settings(project_dir: Path, env: Mapping[str, str]) -> Dict[str, Any]:
    """Slots, roles and the run settings; an unreadable config uses the default slot cap and says so."""
    cpus = ag.core_count() or 1
    notes: List[str] = []
    roles: Mapping[str, Any] = {}
    max_concurrent = None
    notify_command = None
    threshold = 15
    source = "runtime"
    try:
        slots = int(ag.runtime_info(env)["agentSlots"])
        with _EnvOverlay(env):
            resolved = ag.resolve(project_dir)
        roles = resolved["roles"]
        run = roles.get("run", {})
        max_concurrent = run.get("max_concurrent")
        notify_command = run.get("notify_command")
        threshold = int(run.get("eta_slip_minutes") or 15)
    except ag.AgentConfigError:
        slots = ag.runtime_agent_slots(cpus, env)
        source = "default"
        notes.append("slot cap unreadable: using the default %d" % slots)
    return {"slots": slots, "source": source, "roles": roles, "max_concurrent": max_concurrent,
            "notify_command": notify_command, "threshold": threshold, "notes": notes}


# --------------------------------------------------------------------------- spec and run lookup


def _spec_dir(project_dir: Path, spec: str) -> Optional[Path]:
    from claude_kiro.paths import spec_roots

    for root in spec_roots(project_dir):
        if (root / spec).is_dir():
            return root / spec
    return None


def _same_root(start: Optional[Mapping], project_dir: Path) -> bool:
    root = (start or {}).get("root")
    if not root:
        return True
    try:
        return Path(str(root)).resolve() == Path(project_dir).resolve()
    except OSError:
        return str(root) == str(project_dir)


def _start_of(events: Sequence[Mapping]) -> Optional[Mapping]:
    return next((e for e in events if e.get("kind") == "start"), None)


def _find_runs(spec: str, project_dir: Path, env: Mapping[str, str]) -> Tuple[List[Tuple[Path, List[dict], int]],
                                                                               List[Tuple[Path, str]]]:
    """(readable runs of `spec` newest first as (path, events, skipped), unreadable files as (path, reason))."""
    runs: List[Tuple[Path, List[dict], int]] = []
    bad: List[Tuple[Path, str]] = []
    try:
        paths = sorted(progress.progress_dir(env).glob(f"{spec}--*.jsonl"))
    except OSError:
        return runs, bad
    for p in paths:
        try:
            evs, skipped = progress.read_events(p)
        except OSError as exc:
            bad.append((p, exc.strerror or type(exc).__name__))
            continue
        st = _start_of(evs)
        if st is None or st.get("spec") not in (None, spec) or not _same_root(st, project_dir):
            continue
        runs.append((p, evs, skipped))
    runs.sort(key=lambda r: (str(_start_of(r[1]).get("at")), r[0].name), reverse=True)  # type: ignore[union-attr]
    return runs, bad


def _load_snapshot(path: Path) -> Optional[Model]:
    try:
        return history.model_from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def _fit(env: Mapping[str, str]) -> Model:
    try:
        records = store.load_records(env)
    except Exception:  # noqa: BLE001 - an unreadable store is an empty history
        records = []
    return history.fit_model(records, history.load_defaults(env))


def _load_model(key: Optional[str], spec: str, env: Mapping[str, str]) -> Model:
    pdir = progress.progress_dir(env)
    if key:
        m = _load_snapshot(pdir / f"{key}.model.json")
        if m is not None:
            return m
    if env.get("CK_PROGRESS_DIR"):
        m = _load_snapshot(pdir / f"{spec}.prepared-model.json")
        if m is not None:
            return m
    return _fit(env)


# --------------------------------------------------------------------------- events digest and cache


def _events_digest(path: Optional[Path]) -> decide.EventsDigest:
    """Size, line count and last-line hash of the run's events, leaving out the notice lines (they follow a decision)."""
    if path is None:
        return decide.EventsDigest(0, 0, "")
    try:
        raw = path.read_bytes()
    except OSError:
        return decide.EventsDigest(0, 0, "")
    size = lines = 0
    last = b""
    for line in raw.split(b"\n"):
        if not line:
            continue
        try:
            kind = json.loads(line.decode("utf-8")).get("kind")
        except (ValueError, UnicodeDecodeError, AttributeError):
            kind = None
        if kind in _NOTICE_KINDS:
            continue
        size += len(line) + 1
        lines += 1
        last = line
    return decide.EventsDigest(size, lines, hashlib.sha256(last).hexdigest() if lines else "")


def _default_seed(spec: str, model: Model, digest: decide.EventsDigest) -> int:
    text = json.dumps([spec, model.digest, digest.as_dict()], sort_keys=True)
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def _cache_path(key: str, env: Mapping[str, str]) -> Path:
    return progress.progress_dir(env) / f"{key}.eta.json"


def _read_cache(key: str, env: Mapping[str, str]) -> Optional[dict]:
    try:
        body = json.loads(_cache_path(key, env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _write_cache(key: str, view: Mapping[str, Any], digest: decide.EventsDigest, model_digest: str,
                 env: Mapping[str, str]) -> None:
    try:
        body = dict(view)
        body["cached"] = False
        body["digest"] = digest.as_dict()
        body["modelDigest"] = model_digest
        _write_private(_cache_path(key, env), json.dumps(body, sort_keys=True))
    except Exception:  # noqa: BLE001 - the cache is an optimisation
        pass


def _from_cache(cache: Mapping[str, Any], path: Path, now_iso: str) -> dict:
    view = {k: v for k, v in cache.items() if k not in _SECRET_KEYS}
    view["cached"] = True
    view["progressFile"] = str(path)
    view["now"] = now_iso
    return view


# --------------------------------------------------------------------------- graph


def _build_graph(project_dir: Path, spec: str, merged: Sequence[str], slots: int, slots_source: str) -> Graph:
    from claude_kiro.cli import worktree as wtcli
    from claude_kiro.hooks._shared.spec_parser import parse_tasks

    sdir = _spec_dir(project_dir, spec)
    tasks_md = (sdir / "tasks.md") if sdir is not None else None
    if tasks_md is None or not tasks_md.exists():
        raise NoEstimate("tasks.md missing")
    try:
        tasks = parse_tasks(tasks_md)
        plan = wtcli._plan(project_dir, spec, set())
    except (OSError, ValueError, SystemExit) as exc:
        raise NoEstimate("tasks.md unreadable (%s)" % (exc,))
    if not tasks:
        raise NoEstimate("tasks.md has no tasks")
    ids = [t.num for t in tasks]
    by_id = {t.num: t for t in tasks}
    md_done = {t.num for t in tasks if t.done}
    order = tuple(n for n in ids if n not in md_done)
    done = frozenset(md_done | {m for m in merged if m in by_id})
    deps = {n: tuple(by_id[n].dependencies) for n in ids}
    chain = wtcli._chain_lengths({n: list(plan["deps"].get(n, ())) for n in ids}, set(done))
    cycles = tuple(tuple(c[:-1]) if len(c) > 1 and c[0] == c[-1] else tuple(c) for c in plan.get("cycles") or [])
    known = set(ids)
    unknown = {n: tuple(d for d in by_id[n].dependencies if d not in known) for n in ids
               if any(d not in known for d in by_id[n].dependencies)}
    return Graph(
        order=order, deps=deps, done=done, chain=chain,
        tracks={n: by_id[n].track for n in ids},
        risk={n: str(plan.get("risk", {}).get(n, "normal")) for n in ids},
        complexity={n: by_id[n].complexity for n in ids},
        sizes={n: len(by_id[n].files) + len(by_id[n].requirements) for n in ids},
        critical=tuple(plan.get("criticalPath") or ()), cycles=cycles, unknown_deps=unknown,
        slots=slots, slots_source=slots_source)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- the view


def _empty_view(kind: str, spec: str, env: Mapping[str, str], now: datetime) -> Dict[str, Any]:
    return {
        "schema": SCHEMA, "runKind": kind, "spec": spec, "run": None, "state": "not-started", "progressFile": None,
        "cached": False, "launchAt": None, "now": _iso_local(now, env), "finish": None, "runnableDone": None,
        "remainingMinutes": None, "deltaVsLaunchMinutes": None, "cost": {"p10": None, "p50": None, "p90": None},
        "confidence": "none", "basis": {"source": "none", "tasks": 0, "runs": 0, "widestMatch": "none",
                                        "bundledParts": [], "sizeSignal": "off"},
        "slots": 0, "slotsSource": "runtime", "open": 0, "merged": 0, "running": 0, "waiting": 0,
        "criticalPath": {"tasks": [], "medianMinutes": None}, "slowest": [], "safetyTagged": 0, "retryRate": None,
        "escalationRate": None, "cause": None, "lastEvent": None, "tasks": [], "phases": [], "blocked": None,
        "waitingOnYouMinutes": None, "waitingOnYou": [], "ended": None, "staleSince": None, "notes": []}


def _triple(t: Optional[Sequence[float]]) -> Optional[Dict[str, float]]:
    return None if t is None else {"p10": float(t[0]), "p50": float(t[1]), "p90": float(t[2])}


def _round2(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x), 2)


class _Aux:
    """Decision inputs the view alone does not carry."""

    def __init__(self) -> None:
        self.hold: decide.Hold = decide.Hold("none", None, None, (), (), False)
        self.p50_finish_min: Optional[float] = None
        self.waited_min: float = 0.0
        self.launch_p90_min: Optional[float] = None
        self.state: Optional[progress.RunState] = None
        self.launch: Optional[datetime] = None
        self.now: Optional[datetime] = None


def _launch_p90_min(state: progress.RunState, launch: datetime) -> Optional[float]:
    summary = state.launch if isinstance(state.launch, dict) else None
    if not summary:
        return None
    fin = summary.get("finish")
    if isinstance(fin, list) and len(fin) == 3 and isinstance(fin[2], str):
        try:
            return (_aware(datetime.fromisoformat(fin[2].replace("Z", "+00:00"))) - launch).total_seconds() / 60.0
        except ValueError:
            pass
    rem = summary.get("remainingMin")
    if isinstance(rem, list) and len(rem) == 3 and isinstance(rem[2], (int, float)) and not isinstance(rem[2], bool):
        return float(rem[2])
    return None


_SPEC_NAME = re.compile(r"[0-9A-Za-z_][0-9A-Za-z_.\-]*")
_UNSET: Any = object()


def _valid_spec(spec: str) -> bool:
    return bool(_SPEC_NAME.fullmatch(spec)) and ".." not in spec


def _check_deadline(deadline: Optional[float]) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise _Timeout()


def _run_sim(fn: Any, deadline: Optional[float], n_full: int, **kwargs: Any) -> Any:
    """Run `fn(n=...)`. With a deadline, a first batch of 100 passes times the work: past the budget, raise."""
    if deadline is None:
        return fn(n=n_full, **kwargs)
    t0 = time.monotonic()
    fn(n=min(BATCH, n_full), **kwargs)
    took = time.monotonic() - t0
    _check_deadline(deadline)
    if n_full > BATCH and time.monotonic() + took * (n_full / BATCH - 1) > deadline:
        raise _Timeout()
    result = fn(n=n_full, **kwargs)
    _check_deadline(deadline)
    return result


def _core(project_dir: Path, spec: str, kind: str, *, key: Optional[str], events: Sequence[Mapping[str, Any]],
          skipped: int, progress_file: Optional[Path], now: datetime, seed: Optional[int], env: Mapping[str, str],
          model: Model, settings: Mapping[str, Any], deadline: Optional[float] = None,
          extra_notes: Sequence[str] = ()) -> Tuple[Dict[str, Any], _Aux]:
    now = _aware(now)
    aux = _Aux()
    state = progress.fold(events)
    aux.state = state
    started = state.run is not None or any(e.get("kind") == "start" for e in events)
    launch = progress._parse(state.launch_at) if state.launch_at else None
    if launch is None:
        launch = now
    aux.launch, aux.now = launch, now
    now_min = max(0.0, (now - launch).total_seconds() / 60.0)
    notes: List[str] = list(extra_notes) + list(settings["notes"])

    last_at = progress._parse(state.last_event[0]) if state.last_event else None
    stale = state.ended is None and started and progress.is_stale(last_at, now)

    graph: Optional[Graph] = None
    open_ids: List[str] = []
    if kind == "implement":
        merged_ids = [t for t, p in state.tasks.items() if p.status == "merged"]
        graph = _build_graph(project_dir, spec, merged_ids, settings["slots"], settings["source"])
        open_ids = [t for t in graph.order if t not in graph.done]
        hold = decide.classify_hold(graph, state)
    else:
        hold = decide.Hold("none", None, None, (), (), False)
    aux.hold = hold

    now_eff, waited_total = decide.effective_now(state, now_min)
    waited_total = round(waited_total, 6)
    wait_open = state.approval_open is not None and state.ended is None

    view = _empty_view(kind, spec, env, now)
    view.update({"run": key, "progressFile": str(progress_file) if progress_file else None,
                 "launchAt": state.launch_at if started else _utc_iso(now)})
    view["slots"], view["slotsSource"] = settings["slots"], settings["source"]
    if state.last_event:
        view["lastEvent"] = {"at": state.last_event[0], "what": state.last_event[1]}
    view["cause"] = state.cause
    if skipped:
        notes.append("%d malformed progress line(s) skipped" % skipped)

    # ---- basis and confidence
    basis_src = str(model.basis.get("source", "none"))
    parts = [p for p in model.bundled_parts if (p == "phases") == (kind == "create")]
    view["basis"] = {"source": basis_src, "tasks": int(model.basis.get("tasks", 0)),
                     "runs": int(model.basis.get("runs" if kind == "implement" else "createRuns", 0)),
                     "widestMatch": "none", "bundledParts": parts,
                     "sizeSignal": "off" if model.size.source == "none" else model.size.source}
    view["retryRate"], view["escalationRate"] = model.rates.get("retry"), model.rates.get("escalation")
    if basis_src == "bundled":
        notes.append("low confidence: no local history")
    elif parts:
        notes.append("bundled defaults used for: %s" % ", ".join(parts))
    if kind == "implement" and model.size.source == "none":
        notes.append("size signal not used")

    # ---- counts and per-task rows
    merged_count = running = 0
    if graph is not None:
        held = set(hold.held)
        merged_set = {t for t, p in state.tasks.items() if p.status == "merged" and t in graph.order}
        merged_count = len(merged_set)
        rows = []
        for t in graph.order:
            tp = state.tasks.get(t)
            status = tp.status if tp else "pending"
            if status == "pending" and t in held:
                status = "held"
            minutes = None
            if status == "running" and tp is not None and tp.first_start_min is not None:
                minutes = round(max(0.0, now_eff - progress.effective_min(state.waits, tp.first_start_min)), 1)
            rows.append({"task": t, "state": status, "step": tp.step if tp else None,
                         "model": tp.model if tp else None, "minutes": minutes, "medianMinutes": None,
                         "waitsOn": [d for d in graph.deps.get(t, ()) if d not in graph.done]})
        view["tasks"] = rows
        running = sum(1 for r in rows if r["state"] == "running")
        view.update({"open": len(open_ids), "merged": merged_count, "running": running,
                     "waiting": max(0, len(open_ids) - running),
                     "safetyTagged": sum(1 for t in open_ids if graph.risk.get(t) == "safety")})
        view["criticalPath"] = {"tasks": [t for t in graph.critical if t not in graph.done], "medianMinutes": None}
    else:
        phases_known = [t for t in CREATE_PHASES if t in model.phases or t in state.phases_done]
        view["open"] = sum(1 for t in phases_known if t not in state.phases_done)
        view["merged"] = len(state.phases_done)

    # ---- ended / stale / not simulated forms
    if state.ended is not None:
        view["state"] = "done"
        view["ended"] = _ended_block(project_dir, spec, kind, state, launch, model, settings, env, now, graph, events)
        if hold.kind == "blocked":
            hold = decide.Hold("none", None, None, (), (), False)
            aux.hold = hold
        _fill_phases(view, kind, model, state, None)
        usd = (state.launch or {}).get("usd") if isinstance(state.launch, dict) else None
        if isinstance(usd, list) and len(usd) == 2 and all(isinstance(x, (int, float)) for x in usd):
            view["cost"] = {"p10": _round2(usd[0]), "p50": None, "p90": _round2(usd[1])}
        return view, aux
    if stale:
        view["state"] = "stale"
        view["staleSince"] = state.last_event[0] if state.last_event else None
        _fill_phases(view, kind, model, state, None)
        return view, aux

    # ---- simulate
    seed_v = seed if seed is not None else _default_seed(spec, model, _events_digest(progress_file))
    sim = None
    run_sim = False
    if kind == "implement":
        assert graph is not None
        if not open_ids:
            notes.append("nothing left to run")
        elif not (model.basis.get("source") == "none" and not model.bundled_tasks and not model.task_pools):
            run_sim = True
    else:
        run_sim = bool(model.phases)
    if kind == "implement" and run_sim:
        sim = _run_sim(simulate.simulate_implement, deadline, N_IMPLEMENT, graph=graph, model=model, state=state,
                       now_min=now_eff, seed=seed_v, roles=settings["roles"], max_concurrent=settings["max_concurrent"])
    elif kind == "create" and run_sim:
        sim = _run_sim(simulate.simulate_create, deadline, N_CREATE, model=model, state=state, now_min=now_eff,
                       seed=seed_v)
    elif kind == "implement" and not run_sim and open_ids:
        notes.append("no estimate: no history and no bundled defaults")

    view["confidence"] = "none"
    runnable_sim = None
    if sim is not None and sim.finish_min is not None:
        view["confidence"] = "low" if basis_src == "bundled" else "normal"
        view["basis"]["widestMatch"] = sim.widest_step or "none"
        view["remainingMinutes"] = _triple(tuple(_round2(x) for x in sim.remaining_min))  # type: ignore[arg-type]
        if sim.usd is not None:
            view["cost"] = {"p10": _round2(sim.usd[0]), "p50": _round2(sim.usd_p50), "p90": _round2(sim.usd[1])}
        view["criticalPath"]["medianMinutes"] = _round2(sim.critical_median_min)
        slow = sorted(sim.task_median_min.items(), key=lambda kv: (-kv[1], decide._idkey(kv[0])))[:2]
        view["slowest"] = [{"task": t, "medianMinutes": _round2(m)} for t, m in slow]
        for row in view["tasks"]:
            if row["task"] in sim.task_median_min:
                row["medianMinutes"] = _round2(sim.task_median_min[row["task"]])
        if hold.kind != "blocked":
            fin = tuple(_at(launch, v, waited_total, env) for v in sim.finish_min)
            view["finish"] = {"p10": fin[0], "p50": fin[1], "p90": fin[2]}
            aux.p50_finish_min = (_secs(sim.finish_min[1]) + _secs(waited_total)) / 60.0
        elif not hold.whole_run and graph is not None:
            runnable_sim = _run_sim(simulate.simulate_implement, deadline, N_IMPLEMENT, graph=graph, model=model,
                                    state=state, now_min=now_eff, seed=seed_v, roles=settings["roles"],
                                    max_concurrent=settings["max_concurrent"], exclude=frozenset(hold.held))
            if runnable_sim.finish_min is not None:
                view["runnableDone"] = _at(launch, runnable_sim.finish_min[1], waited_total, env)
    elif sim is not None and kind == "implement" and sim.widest_step:
        view["basis"]["widestMatch"] = sim.widest_step
    _fill_phases(view, kind, model, state, sim)

    # ---- launch comparison
    if view["finish"] is not None and state.launch and isinstance(state.launch.get("finish"), list) \
            and len(state.launch["finish"]) == 3:
        try:
            l50 = _aware(datetime.fromisoformat(str(state.launch["finish"][1]).replace("Z", "+00:00")))
            f50 = _aware(datetime.fromisoformat(view["finish"]["p50"]))
            view["deltaVsLaunchMinutes"] = round((f50 - l50).total_seconds() / 60.0, 1)
        except ValueError:
            pass

    # ---- state, waits
    aux.waited_min = state.waited_min
    aux.launch_p90_min = _launch_p90_min(state, launch)
    view["state"] = decide.decide_state(
        ended=False, started=started, stale=False, hold=hold, wait_open=wait_open,
        p50_finish_min=aux.p50_finish_min, waited_min=state.waited_min, launch_p90_min=aux.launch_p90_min)
    if hold.kind == "blocked":
        view["blocked"] = {"reason": hold.reason, "held": list(hold.held)}
        view["waitingOnYou"] = list(hold.waiting_on_you)
    if view["state"] == "waiting" and state.approval_open is not None:
        view["waitingOnYouMinutes"] = round(max(0.0, now_min - state.approval_open[1]), 1)
        view["waitingOnYou"] = sorted((t for t, p in state.tasks.items() if p.status == "hand"), key=decide._idkey)
    view["notes"] = notes
    return view, aux


def _fill_phases(view: Dict[str, Any], kind: str, model: Model, state: progress.RunState, sim: Any) -> None:
    if kind != "create":
        return
    out = []
    for t in CREATE_PHASES:
        if t not in model.phases and t not in state.phases_done and t != state.phase_running:
            continue
        st = "done" if t in state.phases_done else "running" if t == state.phase_running else "pending"
        med = None
        if sim is not None and t in sim.phase_median_min:
            med = sim.phase_median_min[t]
        elif t in model.phases and model.phases[t]["minutes"]:
            med = statistics.median(model.phases[t]["minutes"])
        out.append({"phase": t, "state": st, "medianMinutes": _round2(med)})
    view["phases"] = out


def _ended_block(project_dir: Path, spec: str, kind: str, state: progress.RunState, launch: datetime, model: Model,
                 settings: Mapping[str, Any], env: Mapping[str, str], now: datetime, graph: Optional[Graph],
                 events: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    end = state.ended or {}
    at = progress._parse(end.get("at")) or now
    wall = max(0.0, (at - launch).total_seconds() / 60.0)
    place = "within p10-p90"
    rem = (state.launch or {}).get("remainingMin") if isinstance(state.launch, dict) else None
    if isinstance(rem, list) and len(rem) == 3 and all(isinstance(x, (int, float)) for x in rem):
        place = "below p10" if wall < rem[0] else "above p90" if wall > rem[2] else "within p10-p90"
    block: Dict[str, Any] = {"outcome": end.get("outcome"), "at": end.get("at"), "wallMinutes": round(wall, 1),
                             "merged": end.get("merged"), "total": end.get("total"), "usd": end.get("usd"),
                             "vsLaunch": place, "next": None, "nothingLeft": False}
    if kind == "implement":
        # "Open tasks that remain" (4.8) are the tasks tasks.md does not mark Done, not the tasks this run did not
        # merge: the ended view is read from main after the run, where a merge that did not land on main (or was
        # reverted) is still open work. TC-38 fixes this: a run that merged all six tasks while tasks.md ticks
        # four still shows two open tasks with their launch estimate. So the run's merges are deliberately not
        # passed in, and the launch estimate below starts from no events.
        try:
            fresh = _build_graph(project_dir, spec, [], settings["slots"], settings["source"])
        except NoEstimate:
            fresh = None
        remaining = [t for t in fresh.order if t not in fresh.done] if fresh is not None else []
        if not remaining:
            block["nothingLeft"] = True
        else:
            try:
                nxt, _ = _core(project_dir, spec, kind, key=None, events=[], skipped=0, progress_file=None,
                               now=now, seed=None, env=env, model=model, settings=settings)
                block["next"] = nxt
            except (EtaError, _Timeout):
                block["next"] = None
    return block


# --------------------------------------------------------------------------- estimate


def estimate(project_dir: Path, spec: str, kind: Optional[str] = None, *, now: datetime, seed: Optional[int] = None,
             env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The `EtaView` (a dict) for `spec`. Raises `UnknownSpec` (exit 2) or `NoEstimate` (exit 1)."""
    e = _environ(env)
    project_dir = Path(project_dir)
    now = _aware(now)
    sdir = _spec_dir(project_dir, spec)
    runs, bad = _find_runs(spec, project_dir, e)
    has_tasks = sdir is not None and (sdir / "tasks.md").exists()
    if kind is None:
        if has_tasks:
            kind = "implement"
        elif sdir is not None:
            kind = "create"
        elif runs:
            kind = str(_start_of(runs[0][1]).get("runKind") or "implement")  # type: ignore[union-attr]
        elif bad:
            kind = "implement"
        else:
            raise UnknownSpec("no spec named %s (looked in specs/ and .claude/specs/)" % spec)
    if sdir is None and not runs and not bad:
        raise UnknownSpec("no spec named %s (looked in specs/ and .claude/specs/)" % spec)
    if kind == "implement" and not has_tasks:
        raise NoEstimate("tasks.md missing")

    settings = _settings(project_dir, e)
    run = next((r for r in runs if _start_of(r[1]).get("runKind") in (None, kind)), None)  # type: ignore[union-attr]
    extra: List[str] = []
    if run is None and bad:
        extra.append("live progress unavailable: %s" % bad[0][1])
    key = run[0].stem if run else None
    path = run[0] if run else None
    events = run[1] if run else []
    skipped = run[2] if run else 0
    model = _load_model(key, spec, e)

    digest = _events_digest(path)
    now_iso = _iso_local(now, e)
    if key and seed is None:
        # Reuse only a view computed for this same `now`: state, the stale check, waiting minutes, running minutes and a
        # slip from time alone all move with the clock, so a cache from an earlier `now` is never served (3.10, D16).
        cache = _read_cache(key, e)
        if cache is not None and cache.get("now") == now_iso and decide.cache_fresh(cache, digest, model.digest):
            return _from_cache(cache, path, now_iso)  # type: ignore[arg-type]
    view, _ = _core(project_dir, spec, kind, key=key, events=events, skipped=skipped, progress_file=path, now=now,
                    seed=seed, env=e, model=model, settings=settings, extra_notes=extra)
    if key and seed is None:
        _write_cache(key, view, digest, model.digest, e)
    return view


# --------------------------------------------------------------------------- notifications


def _what(events: Sequence[Mapping[str, Any]]) -> str:
    real = [ev for ev in events if ev.get("kind") not in _NOTICE_KINDS]
    if not real:
        return ""
    last = real[-1]
    kind = last.get("kind")
    if kind == "start":
        return ""
    if kind == "landing":
        merged = [str(x) for x in (last.get("merged") or [])]
        return "landed %s" % ",".join(merged) if merged else "landing"
    state = progress.fold(real)
    return state.last_event[1] if state.last_event else str(kind)


def _notify_input(view: Mapping[str, Any], aux: _Aux, spec: str, env: Mapping[str, str],
                  end_event: Optional[Mapping[str, Any]] = None) -> decide.NotifyInput:
    state = aux.state
    assert state is not None and aux.launch is not None and aux.now is not None
    when = aux.now
    outcome = None
    merged = total = None
    usd = None
    wall = (when - aux.launch).total_seconds() / 60.0
    if end_event is not None:
        outcome = end_event.get("outcome")
        merged, total, usd = end_event.get("merged"), end_event.get("total"), end_event.get("usd")
        at = progress._parse(end_event.get("at"))
        if at is not None:
            wall = max(0.0, (at - aux.launch).total_seconds() / 60.0)
    else:
        merged, total = view.get("merged"), view.get("open", 0) + view.get("merged", 0)
        usd = (view.get("cost") or {}).get("p50")
    cause = view.get("cause")
    if view.get("_timeOnly"):
        running = [(p.last_start_min if p.last_start_min is not None else 0.0, t)
                   for t, p in state.tasks.items() if p.status == "running"]
        if running:
            cause = "task %s running" % min(running, key=lambda r: (r[0], decide._idkey(r[1])))[1]
    return decide.NotifyInput(
        spec=spec, state=str(view["state"]), p50_finish_min=aux.p50_finish_min, waited_min=aux.waited_min,
        launch_p90_min=aux.launch_p90_min, hold=aux.hold, cause=cause, outcome=outcome, merged=merged, total=total,
        wall_min=wall, usd=usd, now_hhmm=_local(when, env).strftime("%H:%M"))


def _start_untrusted(start: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    """True when the run's `start` line may have been written by a sandboxed child: read from the spool
    (`CK_PROGRESS_DIR`), mirrored from one (`spool: true`), or with no `root` at all. Its root then names neither the
    notify command nor the working directory of a direct send."""
    return bool(env.get("CK_PROGRESS_DIR")) or bool(start.get("spool")) or not start.get("root")


def _queues_for_parent(env: Mapping[str, str]) -> bool:
    return env.get("CK_RUN_NOTIFY") == "parent"


def _dispatch(key: str, notices: Sequence[decide.Notice], cwd: Path, command: Optional[str],
              env: Mapping[str, str], direct: bool) -> None:
    """Send (or queue for `ck run`) each due notice and record it. Called under `notice_lock`; never raises."""
    parent = env.get("CK_RUN_NOTIFY") == "parent" and not direct
    for n in notices:
        text = decide.notice_text(n)
        try:
            if parent:
                progress.append(key, {"kind": "notify-pending", "notice": n.kind, "key": n.key, "message": text}, env)
                continue
            outcome = notify_mod.send(command, text, cwd, timeout=10, notification_type=decide.NOTICE_TYPES[n.kind])
            status, detail = outcome.status, (outcome.detail or None)
        except Exception as exc:  # noqa: BLE001 - failures are recorded, not raised
            status, detail = "failed", ("%s: %s" % (type(exc).__name__, exc))[:200]
        progress.append(key, {"kind": "notified", "notice": n.kind, "key": n.key, "level": n.level,
                              "outcome": status, "detail": detail}, env)


def after_event(key: str, *, now: datetime, budget_s: Optional[float] = None,
                env: Optional[Mapping[str, str]] = None, root: Optional[Path] = None,
                notify_command: Any = _UNSET) -> Optional[str]:
    """Recompute after an event (or on time alone), cache, notify, and return the log line; None past the budget or on error.

    The progress file may be written by a sandboxed child (the spool), so its `start` event's root is not trusted for
    the notify command or the working directory: a caller outside the sandbox passes its own `root` and
    `notify_command`. Under `CK_PROGRESS_DIR` without them nothing is sent directly (outcomes are recorded)."""
    e = _environ(env)
    try:
        override = e.get("CK_ETA_LINE_TIMEOUT_S")
        budget = float(override) if override not in (None, "") else (
            float(budget_s) if budget_s is not None else DEFAULT_LINE_BUDGET_S)
    except ValueError:
        budget = DEFAULT_LINE_BUDGET_S
    deadline = time.monotonic() + budget
    try:
        now = _aware(now)
        with progress.notice_lock(key, e):
            path = progress.run_file(key, e)
            events, skipped = progress.read_events(path)
            start = _start_of(events)
            if start is None:
                return None
            state = progress.fold(events)
            last_at = progress._parse(state.last_event[0]) if state.last_event else None
            if state.ended is not None or progress.is_stale(last_at, now):
                return None
            project_dir, spec = Path(str(start.get("root"))), str(start.get("spec"))
            if not _valid_spec(spec):
                return None
            untrusted = root is None and _start_untrusted(start, e)
            if root is not None:
                project_dir = Path(root)
            kind = str(start.get("runKind") or "implement")
            settings = _settings(project_dir, e)
            if notify_command is not _UNSET:
                settings["notify_command"] = notify_command
            # An untrusted call that would send directly decides nothing and records nothing, so it can never mark a
            # notice as sent (S7 dedupe) without sending it; queueing for `ck run` (CK_RUN_NOTIFY=parent) is unaffected.
            can_notify = not (untrusted and notify_command is _UNSET and not _queues_for_parent(e))
            model = _load_model(key, spec, e)
            digest = _events_digest(path)
            now_iso = _iso_local(now, e)
            cache = _read_cache(key, e)
            fresh_events = cache is not None and decide.cache_fresh(cache, digest, model.digest)
            prev_p50 = ((cache or {}).get("finish") or {}).get("p50") if cache else None
            _check_deadline(deadline)
            view, aux = _core(project_dir, spec, kind, key=key, events=events, skipped=skipped, progress_file=path,
                              now=now, seed=None, env=e, model=model, settings=settings, deadline=deadline)
            _check_deadline(deadline)
            _write_cache(key, view, digest, model.digest, e)
            view["_timeOnly"] = bool(fresh_events and cache.get("now") != now_iso)  # type: ignore[union-attr]
            if can_notify:
                ni = _notify_input(view, aux, spec, e)
                notices = decide.notify_decision(ni, state.sent, settings["threshold"])
                _dispatch(key, notices, project_dir, settings["notify_command"], e, direct=False)
            view.pop("_timeOnly", None)
            return decide.eta_log_line(_what(events), view, prev_p50)
    except Exception:  # noqa: BLE001 - never breaks the writer
        return None


def _send_done(key: str, *, now: datetime, env: Mapping[str, str], direct: bool,
               root: Optional[Path] = None) -> None:
    """Dispatch the done notice of an ended run (once: S7 skips a run that already has one).

    `root` is the caller's trusted project root. Without it, a run whose `start` may come from a sandboxed child
    (`_start_untrusted`) is only queued for `ck run` (CK_RUN_NOTIFY=parent); a direct send is skipped and nothing is
    recorded, so the notice is never marked sent without being sent."""
    now = _aware(now)
    with progress.notice_lock(key, env):
        events, _skipped = progress.read_events(progress.run_file(key, env))
        start = _start_of(events)
        state = progress.fold(events)
        if start is None or state.ended is None:
            return
        spec = str(start.get("spec"))
        if root is not None:
            project_dir = Path(root)
        elif _start_untrusted(start, env):
            if direct or not _queues_for_parent(env):
                return
            project_dir = Path(str(start.get("root")))
        else:
            project_dir = Path(str(start.get("root")))
        settings = _settings(project_dir, env)
        launch = progress._parse(state.launch_at) or now
        aux = _Aux()
        aux.state, aux.launch = state, launch
        aux.now = progress._parse(state.ended.get("at")) or now
        aux.waited_min = state.waited_min
        view = {"state": "done", "cause": state.cause, "merged": state.ended.get("merged"),
                "open": 0, "cost": {}}
        ni = _notify_input(view, aux, spec, env, end_event=state.ended)
        ni = decide.NotifyInput(**{**ni.__dict__, "now_hhmm": _local(now, env).strftime("%H:%M")})
        notices = decide.notify_decision(ni, state.sent, settings["threshold"])
        _dispatch(key, notices, project_dir, settings["notify_command"], env, direct=direct)


def notify_done(key: str, *, now: datetime, env: Optional[Mapping[str, str]] = None,
                root: Optional[Path] = None) -> None:
    """After `end` is appended: send the run's done notice (or queue it under `CK_RUN_NOTIFY=parent`). Never raises.

    Pass `root` (the caller's own project root) when the run may have been spooled: see `_send_done`."""
    try:
        _send_done(key, now=now, env=_environ(env), direct=False, root=root)
    except Exception:  # noqa: BLE001
        pass


def send_done_for_superseded(key: str, *, now: datetime, env: Optional[Mapping[str, str]] = None,
                             root: Optional[Path] = None) -> None:
    """After a run's `end` outcome `superseded` is appended: its done notice (D18), stale runs included."""
    notify_done(key, now=now, env=env, root=root)


# --------------------------------------------------------------------------- spool


def prepare_spool(main_root: Path, spec: str, env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """Supersede unfinished data-dir runs of `spec` (sending their done notice), then make the git-ignored spool."""
    e = dict(_environ(env))
    data_env = dict(e)
    data_env.pop("CK_PROGRESS_DIR", None)
    data_env.pop("CK_RUN_NOTIFY", None)
    now = progress.clock_now(e)
    main_root = Path(main_root)
    try:
        runs, _ = _find_runs(spec, main_root, data_env)
        for path, evs, _skipped in runs:
            if any(ev.get("kind") == "end" for ev in evs):
                continue
            key = path.stem
            err = progress.append(key, {"kind": "end", "at": _utc_iso(now), "outcome": "superseded", "merged": None,
                                        "total": None, "usd": None}, data_env)
            if err is None:
                try:
                    _send_done(key, now=now, env=data_env, direct=True, root=main_root)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001 - spool preparation continues
        pass
    try:
        from claude_kiro import worktrees as wt

        wdir = wt.worktrees_dir(wt.main_root(main_root), spec)
    except Exception:  # noqa: BLE001
        return None
    spool = Path(wdir) / ".ck-progress"
    try:
        if spool.is_symlink():
            return None
        spool.mkdir(parents=True, exist_ok=True)
        if spool.is_symlink() or not spool.is_dir():
            return None
        os.chmod(spool, 0o700)
        ignore = spool / ".gitignore"
        if ignore.is_symlink():
            return None
        if not ignore.exists() or ignore.read_text(encoding="utf-8") != "*\n":
            ignore.write_text("*\n", encoding="utf-8")
        model = _fit(data_env)
        _write_private(spool / f"{spec}.prepared-model.json", json.dumps(history.model_to_json(model), sort_keys=True))
    except OSError:
        return None
    return spool


# --------------------------------------------------------------------------- run-start operations (used by `ck progress begin`)


def fit_history(env: Optional[Mapping[str, str]] = None) -> Model:
    """A fresh model fit from the recorded run history and the defaults (what a run snapshots at launch)."""
    return _fit(_environ(env))


def write_model_snapshot(path: Path, model: Model) -> None:
    """Write `model` as an owner-only JSON snapshot at `path`."""
    _write_private(Path(path), json.dumps(history.model_to_json(model), sort_keys=True))


def run_settings(project_dir: Path, env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The run settings an estimate uses: slots (and where they came from) and max concurrency."""
    return _settings(Path(project_dir), _environ(env))


def unfinished_runs(spec: str, project_dir: Path, env: Optional[Mapping[str, str]] = None) -> List[Tuple[Path, List[dict]]]:
    """(progress file, events) for each run of `spec` in `project_dir` that has no end event."""
    runs, _bad = _find_runs(spec, Path(project_dir), _environ(env))
    return [(path, events) for path, events, _skipped in runs if not any(e.get("kind") == "end" for e in events)]


def open_tasks(project_dir: Path, spec: str, settings: Mapping[str, Any]) -> List[str]:
    """The tasks of `spec` not marked Done in its tasks.md, in plan order. Raises EtaError when there is no graph."""
    graph = _build_graph(Path(project_dir), spec, [], settings["slots"], settings["source"])
    return [t for t in graph.order if t not in graph.done]


def launch_estimate(project_dir: Path, spec: str, kind: str, *, model: Model, settings: Mapping[str, Any],
                    now: datetime, env: Optional[Mapping[str, str]] = None,
                    deadline: Optional[float] = None) -> Dict[str, Any]:
    """The launch view of a run that has no events yet. Raises EtaError (or a timeout past `deadline`)."""
    view, _aux = _core(Path(project_dir), spec, kind, key=None, events=[], skipped=0, progress_file=None, now=now,
                       seed=None, env=_environ(env), model=model, settings=settings, deadline=deadline)
    return view


# --------------------------------------------------------------------------- text


def _hhmm(iso: Any) -> str:
    if not isinstance(iso, str):
        return "?"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        return "?"


def _minutes_ago(view: Mapping[str, Any]) -> str:
    try:
        a = datetime.fromisoformat(str(view["launchAt"]).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(view["now"]))
        return "%d min ago" % int(round((_aware(b) - _aware(a)).total_seconds() / 60.0))
    except (KeyError, ValueError, TypeError):
        return "?"


def _cost_text(view: Mapping[str, Any]) -> str:
    c = view.get("cost") or {}
    if c.get("p10") is None or c.get("p90") is None:
        return "cost unknown"
    return "cost $%d – $%d (p10–p90)" % (round(c["p10"]), round(c["p90"]))


def _basis_text(view: Mapping[str, Any]) -> str:
    b = view.get("basis") or {}
    size = {"local": "on", "bundled": "bundled", "off": "off"}.get(b.get("sizeSignal"), "off")
    if view.get("runKind") == "create":
        return "basis: %s create runs · confidence %s" % (b.get("runs", 0), view.get("confidence"))
    match = b.get("widestMatch")
    text = "basis: %s past tasks, %s runs%s · size signal %s · confidence %s" % (
        b.get("tasks", 0), b.get("runs", 0), " · matched at %s" % match if match and match != "none" else "", size,
        view.get("confidence"))
    if b.get("bundledParts"):
        text += " · bundled: %s" % ", ".join(b["bundledParts"])
    return text


def _finish_text(view: Mapping[str, Any]) -> str:
    fin = view.get("finish")
    if not fin:
        return "no estimate: no history and no bundled defaults" if view.get("confidence") == "none" else "no finish estimate"
    text = "finish %s – %s – %s (p10–p50–p90)" % (_hhmm(fin["p10"]), _hhmm(fin["p50"]), _hhmm(fin["p90"]))
    d = view.get("deltaVsLaunchMinutes")
    if isinstance(d, (int, float)):
        text += " · %+d min vs launch" % round(d)
    return text if view.get("state") == "not-started" else text + " · " + str(view.get("state"))


def _graph_lines(view: Mapping[str, Any]) -> List[str]:
    out = []
    crit = view.get("criticalPath") or {}
    if crit.get("tasks"):
        text = "critical: " + " → ".join(crit["tasks"])
        if crit.get("medianMinutes") is not None:
            text += " (median %d min)" % round(crit["medianMinutes"])
        out.append(text)
    rows = view.get("tasks") or []
    running = [r for r in rows if r["state"] == "running"]
    if running:
        parts = []
        for r in running:
            step = r.get("step") or "first"
            label = "%s %s" % (r["task"], step if step != "first" else "first")
            parts.append("%s %s %s min" % (label, r.get("model") or "inherit", round(r.get("minutes") or 0)))
        out.append("running: " + " · ".join(parts))
    waiting = [r for r in rows if r["state"] in ("pending", "held") and r.get("waitsOn")]
    if waiting:
        out.append("waiting: " + " · ".join("%s on %s" % (r["task"], ",".join(r["waitsOn"])) for r in waiting))
    return out


def render(view: Mapping[str, Any]) -> str:
    """The text block for an `EtaView` (design CLI section)."""
    kind = view.get("runKind")
    state = view.get("state")
    lines: List[str] = []
    if kind == "create":
        head = "ck eta %s · create" % view.get("spec")
    else:
        head = "ck eta %s · implement · %s open · %s slots" % (view.get("spec"), view.get("open"), view.get("slots"))
    lines.append(head)
    if view.get("launchAt") and state != "not-started":
        last = view.get("lastEvent")
        text = "launched %s (%s)" % (_hhmm(view["launchAt"]), _minutes_ago(view))
        if last:
            text += " · last event %s %s" % (_hhmm(last["at"]), last["what"])
        lines.append(text)
    if state == "done" and view.get("ended"):
        end = view["ended"]
        text = "%s %s: %s min" % (end.get("outcome"), _hhmm(end.get("at")), round(end.get("wallMinutes") or 0))
        if end.get("merged") is not None and end.get("total") is not None:
            text = "%s %s: %s/%s merged, %s min" % (end.get("outcome"), _hhmm(end.get("at")), end["merged"],
                                                     end["total"], round(end.get("wallMinutes") or 0))
        if end.get("usd") is not None:
            text += ", $%.2f" % end["usd"]
        lines.append(text)
        if end.get("usd") is None and (view.get("cost") or {}).get("p10") is not None:
            lines.append(_cost_text(view).replace("(p10–p90)", "(launch estimate)"))
        lines.append("vs launch estimate: %s" % {"below p10": "before p10", "above p90": "after p90",
                                                  "within p10-p90": "inside p10-p90"}.get(end.get("vsLaunch"), ""))
        if end.get("nothingLeft"):
            lines.append("nothing left to run")
        elif end.get("next"):
            lines.append("remaining work:")
            lines.extend("  " + ln for ln in render(end["next"]).splitlines()[1:] if ln != "not started")
        return "\n".join(lines)
    if kind == "implement" and view.get("open") == 0 and state == "not-started":
        lines.append("nothing left to run")
        return "\n".join(lines)
    if state == "not-started":
        lines.append("not started")
    if kind == "implement":
        lines.append("merged %s · running %s · waiting %s" % (view.get("merged"), view.get("running"), view.get("waiting")))
    if state == "stale":
        lines.append("stale: no progress since %s" % _hhmm(view.get("staleSince")))
    elif state == "blocked":
        blocked = view.get("blocked") or {}
        lines.append("blocked: %s" % blocked.get("reason"))
        rem = view.get("remainingMinutes")
        if rem:
            lines.append("remaining once unblocked: %d min" % round(rem["p50"]))
        if view.get("runnableDone"):
            lines.append("runnable work done ~%s" % _hhmm(view["runnableDone"]))
    else:
        lines.append(_finish_text(view))
        if view.get("waitingOnYouMinutes") is not None:
            lines.append("waiting on you %d min" % round(view["waitingOnYouMinutes"]))
    if state != "stale":
        lines.append(_cost_text(view))
    if view.get("cause"):
        lines.append("cause: %s" % view["cause"])
    if kind == "create":
        for ph in view.get("phases") or []:
            mark = {"done": "✓", "running": "▶"}.get(ph["state"], " ")
            med = "" if ph.get("medianMinutes") is None else " (median %d min)" % round(ph["medianMinutes"])
            lines.append("  %s %s%s" % (mark, ph["phase"], med))
    else:
        lines.extend(_graph_lines(view))
        if state == "not-started":
            slow = view.get("slowest") or []
            if slow:
                lines.append("slowest: " + " · ".join("%s %d min" % (s["task"], round(s["medianMinutes"])) for s in slow))
            lines.append("safety-tagged: %s" % view.get("safetyTagged"))
            rr, er = view.get("retryRate"), view.get("escalationRate")
            lines.append("retry rate: %s" % ("?" if rr is None else "%d%%" % round(rr * 100)))
            lines.append("escalation rate: %s" % ("?" if er is None else "%d%%" % round(er * 100)))
    lines.append(_basis_text(view))
    for note in view.get("notes") or []:
        lines.append("note: %s" % note)
    return "\n".join(lines)
