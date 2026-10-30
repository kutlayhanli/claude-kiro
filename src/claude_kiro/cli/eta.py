"""`ck eta`, `ck progress begin|event|end`, `ck status-line` and the hidden ETA maintainer commands.

Every command reads "now" through `progress.clock_now(env)`, so `CK_NOW` pins it. The progress commands always exit 0:
a run must never break because its progress could not be written (3.7).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import click

from .. import progress as prog
from ..analytics import store

_OUTCOMES = ("completed", "partial", "halted", "refused", "failed", "superseded")
_REASONS = ("gate", "review", "landing", "conflict", "worktree", "other")
_CLAIM_KINDS = ("task-start", "retry", "escalation", "revision", "landfix", "resolve")
_PUBLIC_KINDS = ("task-failed", "approval-start", "approval-end", "phase-start", "step-end", "park", "unpark")
_HOOK_KINDS = _CLAIM_KINDS + ("landing", "merge", "hand-landing")
_RUN_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:\-]*")
_EXIT_ERRORS = (OSError, ValueError)


# --------------------------------------------------------------------------- shared helpers


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _project_root() -> Path:
    cwd = Path.cwd()
    try:
        from ..worktrees import main_root

        return Path(main_root(cwd))
    except Exception:  # noqa: BLE001 - not a git checkout: the directory itself
        return cwd


def _service():
    from ..eta import service

    return service


def _emit_json(obj: Any) -> None:
    click.echo(json.dumps(obj, sort_keys=False))


def _line_budget(env: Mapping[str, str], default: float) -> float:
    raw = env.get("CK_ETA_LINE_TIMEOUT_S")
    if raw not in (None, ""):
        try:
            return float(raw)
        except ValueError:
            pass
    return default


# --------------------------------------------------------------------------- ck eta


def _estimate_or_exit(root: Path, spec: str, kind: Optional[str], seed: Optional[int], env: Mapping[str, str]) -> dict:
    svc = _service()
    try:
        return svc.estimate(root, spec, kind, now=prog.clock_now(env), seed=seed, env=env)
    except svc.UnknownSpec as exc:
        click.echo(f"❌ {exc}")
        sys.exit(2)
    except svc.NoEstimate as exc:
        click.echo(f"❌ no implement estimate for {spec}: {exc}")
        sys.exit(1)


def _show(view: dict, as_json: bool) -> None:
    if as_json:
        _emit_json(view)
    else:
        click.echo(_service().render(view))


def _signature(path: Optional[str]) -> Optional[tuple]:
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _spooled(path: Optional[str]) -> bool:
    if not path:
        return False
    try:
        events, _ = prog.read_events(Path(path))
    except OSError:
        return True
    start = next((e for e in events if e.get("kind") == "start"), None)
    return bool(start and start.get("spool"))


def _recheck_seconds(env: Mapping[str, str]) -> float:
    try:
        v = float(env.get("CK_ETA_RECHECK_S") or 60)
        return v if v > 0 else 60.0
    except ValueError:
        return 60.0


def _watch(root: Path, spec: str, kind: Optional[str], seed: Optional[int], as_json: bool,
           env: Mapping[str, str]) -> None:
    svc = _service()
    recheck = _recheck_seconds(env)
    last_sig: Optional[tuple] = None
    last_draw = float("-inf")
    last_check = time.monotonic()
    path: Optional[str] = None
    key: Optional[str] = None
    first_draw = True
    try:
        # The first slip check runs before the first draw: a draw refreshes the cache for this very `now`, and a
        # slip that comes from time alone is told apart by a cache computed for an earlier `now` (D14).
        try:
            key0 = prog.active_run(spec, root, env, now=prog.clock_now(env))
            if key0 and not _spooled(str(prog.run_file(key0, env))):
                svc.after_event(key0, now=prog.clock_now(env), env=env)
        except Exception:  # noqa: BLE001
            pass
        while True:
            tick = time.monotonic()
            sig = _signature(path)
            if last_draw == float("-inf") or sig != last_sig or tick - last_draw >= 60:
                view = _estimate_or_exit(root, spec, kind, seed, env)
                path, key = view.get("progressFile"), view.get("run")
                if sys.stdout.isatty():
                    click.echo("\033[H\033[J", nl=False)
                _show(view, as_json)
                click.echo("")
                sys.stdout.flush()
                last_draw, last_sig = tick, _signature(path)
                if first_draw:
                    first_draw, last_check = False, time.monotonic()  # the first check comes a full interval later
                if view.get("state") == "done":
                    return
            if key and time.monotonic() - last_check >= recheck:
                last_check = time.monotonic()
                if not _spooled(path):  # a spooled run is checked by `ck run`'s watcher
                    try:
                        svc.after_event(str(key), now=prog.clock_now(env), env=env)
                    except Exception:  # noqa: BLE001 - the check never ends the watch
                        pass
            time.sleep(1.0)
    except KeyboardInterrupt:
        return


@click.command("eta")
@click.argument("spec")
@click.option("--kind", type=click.Choice(["implement", "create"]), default=None,
              help="What to estimate (default: implement when tasks.md exists, else create)")
@click.option("--watch", "watch", is_flag=True, help="Redraw as the run's progress changes")
@click.option("--json", "as_json", is_flag=True, help="Print the ck-eta/1 view as JSON")
@click.option("--seed", type=int, default=None, help="Seed of the simulation (default: derived from the run)")
def eta(spec: str, kind: Optional[str], watch: bool, as_json: bool, seed: Optional[int]) -> None:
    """When will this run finish? Estimate from past runs and the live progress file."""
    env = os.environ
    root = _project_root()
    if watch:
        _watch(root, spec, kind, seed, as_json, env)
        return
    _show(_estimate_or_exit(root, spec, kind, seed, env), as_json)


# --------------------------------------------------------------------------- ck progress


@click.group("progress")
def progress_group() -> None:
    """Record and read the progress of a run (used by the run skills and workflows)."""


def _write_snapshot(key: str, env: Mapping[str, str], spec: str):
    """Write `<key>.model.json` (the prepared snapshot when there is one, else a fresh fit); return the Model."""
    from ..eta import history

    svc = _service()
    pdir = prog.progress_dir(env)
    model = None
    if env.get("CK_PROGRESS_DIR"):
        prepared = pdir / f"{spec}.prepared-model.json"
        try:
            # model_from_json returns None unless the snapshot's digest matches its body
            model = history.model_from_json(json.loads(prepared.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            model = None
    if model is None:
        model = svc.fit_history(env)
    svc.write_model_snapshot(pdir / f"{key}.model.json", model)
    return model


def _supersede_unfinished(root: Path, spec: str, env: Mapping[str, str], now: datetime) -> None:
    svc = _service()
    try:
        runs = svc.unfinished_runs(spec, root, env)
    except Exception:  # noqa: BLE001
        return
    for path, events in runs:
        key = path.stem
        state = prog.fold(events)
        start = next((e for e in events if e.get("kind") == "start"), {})
        merged = sum(1 for p in state.tasks.values() if p.status == "merged")
        total = start.get("total") if isinstance(start.get("total"), int) else None
        err = prog.append(key, {"kind": "end", "at": _utc_iso(now), "outcome": "superseded",
                                "merged": merged if state.run_kind != "create" else None,
                                "total": total if state.run_kind != "create" else None, "usd": None}, env)
        if err is None:
            svc.send_done_for_superseded(key, now=now, env=env, root=None if env.get("CK_PROGRESS_DIR") else root)


def _launch_estimate(root: Path, spec: str, kind: str, env: Mapping[str, str], model, settings: dict):
    """(view or None, open ids) for a launch: simulated within the line budget (4 s)."""
    from ..eta.history import CREATE_PHASES

    svc = _service()
    open_ids: List[str] = []
    if kind == "create":
        open_ids = list(CREATE_PHASES)
    else:
        try:
            open_ids = svc.open_tasks(root, spec, settings)
        except Exception:  # noqa: BLE001
            open_ids = []
    view = None
    try:
        deadline = time.monotonic() + _line_budget(env, 4.0)
        view = svc.launch_estimate(root, spec, kind, model=model, settings=settings, now=prog.clock_now(env),
                                   env=env, deadline=deadline)
    except Exception:  # noqa: BLE001 - EtaError, timeout or anything else: no estimate, the run goes on
        view = None
    return view, open_ids


def _launch_summary(view: Optional[Mapping[str, Any]]) -> dict:
    if not view:
        return {"finish": None, "remainingMin": None, "usd": None, "confidence": "none"}
    fin = view.get("finish")
    rem = view.get("remainingMinutes")
    cost = view.get("cost") or {}
    usd = [cost["p10"], cost["p90"]] if cost.get("p10") is not None and cost.get("p90") is not None else None
    return {"finish": [fin["p10"], fin["p50"], fin["p90"]] if fin else None,
            "remainingMin": [rem["p10"], rem["p50"], rem["p90"]] if rem else None, "usd": usd,
            "confidence": view.get("confidence", "none")}


@progress_group.command("begin")
@click.argument("spec")
@click.option("--kind", type=click.Choice(["implement", "create"]), required=True)
@click.option("--mode", type=click.Choice(["workflow", "agents"]), default="workflow", show_default=True)
@click.option("--json", "as_json", is_flag=True)
def progress_begin(spec: str, kind: str, mode: str, as_json: bool) -> None:
    """Start a run: write the model snapshot and the `start` event, and print the launch ETA line."""
    env = os.environ
    result: Dict[str, Any] = {"run": None, "file": None, "etaLine": None, "estimate": None}
    try:
        from ..eta import decide

        svc = _service()
        now = prog.clock_now(env)
        root = _project_root()
        _supersede_unfinished(root, spec, env, now)
        key = prog.new_run_key(spec, now)
        model = _write_snapshot(key, env, spec)
        settings = svc.run_settings(root, env)
        view, open_ids = _launch_estimate(root, spec, kind, env, model, settings)
        summary = _launch_summary(view)
        start = {"kind": "start", "at": _utc_iso(now), "run": key, "runKind": kind, "spec": spec, "root": str(root),
                 "mode": mode, "slots": settings["slots"], "slotsSource": settings["source"],
                 "maxConcurrent": settings["max_concurrent"], "open": open_ids, "total": len(open_ids),
                 "launch": summary, "model": f"{key}.model.json", "modelDigest": model.digest,
                 "spool": bool(env.get("CK_PROGRESS_DIR"))}
        err = prog.append(key, start, env)
        if err is not None:
            result["progress"] = f"failed: {err}"
        else:
            result["run"], result["file"] = key, str(prog.run_file(key, env))
            if view is not None:
                view["run"], view["progressFile"] = key, result["file"]
                result["estimate"] = view
                if view.get("finish"):
                    result["etaLine"] = decide.eta_log_line("", view, None)
    except Exception as exc:  # noqa: BLE001 - begin always exits 0
        result["run"], result["file"] = None, None
        result["progress"] = f"failed: {type(exc).__name__}: {exc}"
    if as_json:
        _emit_json(result)
        return
    if result.get("progress"):
        click.echo(f"⚠ progress: {result['progress']}")
    click.echo(result["etaLine"] or "ETA unavailable")
    if result["run"]:
        click.echo(f"run: {result['run']}")


def _split(text: Optional[str]) -> List[str]:
    return [p.strip() for p in (text or "").split(",") if p.strip()]


def _build_event(spec: str, kind: str, events: Sequence[Mapping], now: datetime, o: Mapping[str, Any],
                 hook: bool) -> Dict[str, Any]:
    """The event(s) to write for `kind`; raises ValueError with a short reason for a refused one."""
    from ..eta.history import CREATE_PHASES

    task = o.get("task")
    base: Dict[str, Any] = {"kind": kind, "at": _utc_iso(now)}

    def need_task() -> str:
        if task is None or not str(task).strip():
            raise ValueError(f"{kind} needs --task")
        return str(task)

    def need_phase() -> str:
        phase = o.get("phase")
        if phase not in CREATE_PHASES:
            raise ValueError(f"{kind} needs --phase, one of the create phases")
        return phase

    if kind not in _PUBLIC_KINDS and kind not in _HOOK_KINDS:
        raise ValueError(f"unknown event kind: {kind}")
    if kind in _HOOK_KINDS and not hook:
        raise ValueError(f"event kind {kind} is written by ck worktree claim/land, not by ck progress event")
    if kind == "task-failed":
        reason = o.get("reason")
        base.update(task=need_task(), reason=reason if reason in _REASONS else "other")
    elif kind == "approval-start":
        base.update(id=secrets.token_hex(4), reason="question")
    elif kind == "approval-end":
        opened: List[str] = []
        for ev in events:
            if ev.get("kind") == "approval-start" and isinstance(ev.get("id"), str):
                opened.append(ev["id"])
            elif ev.get("kind") == "approval-end" and ev.get("id") in opened:
                opened.remove(ev["id"])
        if not opened:
            raise ValueError("no open approval wait")
        base.update(id=opened[0])
    elif kind in ("phase-start", "step-end"):
        base.update(phase=need_phase())
    elif kind == "park":
        base.update(task=need_task(), parkKind=o.get("reason") or "needs-input", parkedAt=_utc_iso(now))
    elif kind == "unpark":
        base.update(task=need_task())
    elif kind in _CLAIM_KINDS:
        base.update(task=need_task(), attempt=o.get("attempt"), model=o.get("model"))
    elif kind in ("merge", "hand-landing"):
        base.update(task=need_task())
    elif kind == "landing":
        base.update(tasks=_split(o.get("tasks")), merged=_split(o.get("merged")), red=_split(o.get("red")),
                    conflict=_split(o.get("conflict")), protected=_split(o.get("protected")), green=o.get("green"))
    return base


def _model_readable(key: str, env: Mapping[str, str]) -> bool:
    from ..eta import history

    try:
        path = prog.progress_dir(env) / f"{key}.model.json"
        return history.model_from_json(json.loads(path.read_text(encoding="utf-8"))) is not None
    except (OSError, ValueError):
        return False


@progress_group.command("event")
@click.argument("spec")
@click.argument("kind")
@click.option("--run", "run_key", default=None, help="Run key (default: the spec's active run)")
@click.option("--task", default=None)
@click.option("--model", default=None)
@click.option("--attempt", type=int, default=None)
@click.option("--phase", default=None)
@click.option("--reason", default=None)
@click.option("--tasks", default=None, hidden=True)
@click.option("--merged", "merged_ids", default=None, hidden=True)
@click.option("--red", default=None, hidden=True)
@click.option("--conflict", default=None, hidden=True)
@click.option("--protected", default=None, hidden=True)
@click.option("--green", type=bool, default=None, hidden=True)
@click.option("--json", "as_json", is_flag=True)
def progress_event(spec, kind, run_key, task, model, attempt, phase, reason, tasks, merged_ids, red, conflict,
                   protected, green, as_json) -> None:
    """Append one event to a run's progress file and print the new ETA line."""
    env = os.environ
    out: Dict[str, Any] = {"ok": False, "eta": None, "error": None}
    try:
        now = prog.clock_now(env)
        hook = env.get("CK_PROGRESS_TEST_KINDS") == "1"
        root = _project_root()
        key = run_key or prog.active_run(spec, root, env, now=now)
        if not key:
            raise ValueError(f"no active run of {spec}")
        events, _ = prog.read_events(prog.run_file(key, env))
        opts = {"task": task, "model": model, "attempt": attempt, "phase": phase, "reason": reason, "tasks": tasks,
                "merged": merged_ids, "red": red, "conflict": conflict, "protected": protected, "green": green}
        ev = _build_event(spec, kind, events, now, opts, hook)
        if kind == "phase-start":
            state = prog.fold(events)
            if state.phase_running == ev["phase"]:
                ev = None  # already running: idempotent
            elif state.phase_running:
                err = prog.append(key, {"kind": "phase-end", "at": _utc_iso(now), "phase": state.phase_running}, env)
                if err:
                    raise OSError(err)
        if ev is not None:
            err = prog.append(key, ev, env)
            if err is not None:
                raise OSError(err)
        out["ok"] = True
        if _model_readable(key, env):
            out["eta"] = _service().after_event(key, now=prog.clock_now(env), env=env)
    except Exception as exc:  # noqa: BLE001 - always exit 0
        out["ok"] = False
        out["error"] = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
    if as_json:
        _emit_json(out)
    elif out["error"]:
        click.echo(f"⚠ progress: {out['error']}")
    elif out["eta"]:
        click.echo(out["eta"])


def _record_usd(run_id: str, env: Mapping[str, str]) -> Optional[float]:
    if not _RUN_ID.fullmatch(run_id):
        return None
    try:
        rec = json.loads(store.record_path(run_id, env).read_text(encoding="utf-8"))
        from ..analytics.show import _cost

        usd = _cost(rec).get("usd")
    except Exception:  # noqa: BLE001
        return None
    return round(float(usd), 2) if isinstance(usd, (int, float)) and not isinstance(usd, bool) else None


def _error_lines(key: str, env: Mapping[str, str]) -> List[str]:
    try:
        text = (prog.progress_dir(env) / f"{key}.errors").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [ln for ln in text.splitlines() if ln.strip()]


@progress_group.command("end")
@click.argument("spec")
@click.option("--run", "run_key", default=None)
@click.option("--outcome", type=click.Choice(list(_OUTCOMES)), required=True)
@click.option("--record", "record", default=None, help="Run id of the collected record (for the cost)")
@click.option("--json", "as_json", is_flag=True)
def progress_end(spec: str, run_key: Optional[str], outcome: str, record: Optional[str], as_json: bool) -> None:
    """End a run: append `end`, send the done notice and print the final block."""
    env = os.environ
    warnings: List[str] = []
    block = ""
    view = None
    key = None
    ok = False
    try:
        svc = _service()
        now = prog.clock_now(env)
        root = _project_root()
        key = run_key or prog.active_run(spec, root, env, now=now)
        if not key:
            warnings.append(f"⚠ progress: no active run of {spec}")
        else:
            events, _ = prog.read_events(prog.run_file(key, env))
            state = prog.fold(events)
            start = next((e for e in events if e.get("kind") == "start"), {})
            merged: Optional[int] = None
            total = None
            if state.run_kind != "create":
                merged = sum(1 for p in state.tasks.values() if p.status == "merged")
                total = start.get("total") if isinstance(start.get("total"), int) else None
            usd = _record_usd(record, env) if record else None
            err = prog.append(key, {"kind": "end", "at": _utc_iso(now), "outcome": outcome, "merged": merged,
                                    "total": total, "usd": usd}, env)
            ok = err is None
            lines = _error_lines(key, env)
            if err is not None or lines:
                first = (lines[0].split(" ", 2)[2] if lines and len(lines[0].split(" ", 2)) == 3 else err)
                n = len(lines) if lines else 1
                warnings.append(f"⚠ progress: {n} event(s) not written ({first})")
            if ok:
                svc.notify_done(key, now=now, env=env, root=None if env.get("CK_PROGRESS_DIR") else root)
                events, _ = prog.read_events(prog.run_file(key, env))
                bad = [e for e in events if e.get("kind") == "notified" and e.get("outcome") == "failed"]
                if bad:
                    warnings.append(f"⚠ notification failed: {bad[0].get('detail') or 'unknown error'}")
            try:
                view = svc.estimate(root, spec, state.run_kind or None, now=now, env=env)
                block = svc.render(view)
            except Exception:  # noqa: BLE001 - no block when the estimate cannot be made
                view = None
    except Exception as exc:  # noqa: BLE001 - always exit 0
        warnings.append(f"⚠ progress: {type(exc).__name__}: {exc}")
    if as_json:
        _emit_json({"ok": ok, "run": key, "warnings": warnings, "eta": view})
        return
    if block:
        click.echo(block)
    for w in warnings:
        click.echo(w)


# --------------------------------------------------------------------------- ck status-line


@click.command("status-line")
def status_line() -> None:
    """Print one status-line per active run of the project (reads Claude Code's status JSON on stdin)."""
    from .. import statusline

    statusline.main()


# --------------------------------------------------------------------------- hidden maintainer commands


@click.command("eta-defaults", hidden=True)
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False))
@click.option("--min-runs", type=int, default=None, show_default="history.MIN_DEFAULT_RUNS (20)")
def eta_defaults(out_path: str, min_runs: Optional[int]) -> None:
    """Write the bundled ETA cold-start aggregates from local history."""
    from ..eta import history

    records = store.load_records(os.environ)
    try:
        built = history.build_defaults(records, min_runs=history.MIN_DEFAULT_RUNS if min_runs is None else min_runs)
    except ValueError as exc:
        click.echo(f"❌ refusing to write defaults: {exc}")
        sys.exit(1)
    ok, problems = history.defaults_shape_ok(built.payload)
    if not ok:
        click.echo("❌ refusing to write defaults: " + "; ".join(problems))
        sys.exit(1)
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(built.payload, indent=2, sort_keys=True).replace("/", "\\u002f") + "\n", encoding="utf-8")
    click.echo(f"wrote {path} from {built.runs} runs")


def _wave_graph(rec: Mapping[str, Any]):
    from ..eta.history import Graph

    tasks = [t for t in rec.get("tasks") or [] if isinstance(t, dict) and t.get("task") is not None]
    if not tasks:
        return None
    by_wave: Dict[int, List[str]] = {}
    for t in tasks:
        w = t.get("wave") if isinstance(t.get("wave"), int) else 1
        by_wave.setdefault(w, []).append(str(t["task"]))
    waves = sorted(by_wave)
    deps: Dict[str, tuple] = {}
    chain: Dict[str, int] = {}
    for i, w in enumerate(waves):
        prev = tuple(by_wave[waves[i - 1]]) if i else ()
        for tid in by_wave[w]:
            deps[tid] = prev
            chain[tid] = len(waves) - i - 1
    order = tuple(str(t["task"]) for t in tasks)
    return Graph(
        order=order, deps=deps, done=frozenset(), chain=chain,
        tracks={str(t["task"]): t.get("track") if t.get("track") in ("impl", "test") else "impl" for t in tasks},
        risk={str(t["task"]): "safety" if t.get("riskTag") == "safety" else "normal" for t in tasks},
        complexity={}, sizes={}, critical=(), cycles=(), unknown_deps={},
        slots=max(len(v) for v in by_wave.values()), slots_source="default")


@click.command("eta-backtest", hidden=True)
@click.option("--json", "as_json", is_flag=True)
def eta_backtest(as_json: bool) -> None:
    """How often did a past run's wall time fall inside the launch p10-p90 estimated from the runs before it?"""
    from ..eta import history, simulate

    records = [r for r in store.load_records(os.environ) if isinstance(r, dict)]
    records.sort(key=lambda r: (str(r.get("startTime") or ""), str(r.get("runId") or "")))
    defaults = history.load_defaults(os.environ)
    empty = prog.fold([])
    runs = inside = 0
    for i, rec in enumerate(records):
        is_create = rec.get("workflow") == "spec-create"
        if not is_create and not isinstance(rec.get("tasks"), list):
            continue
        wall_ms = rec.get("wallMs")
        if not isinstance(wall_ms, (int, float)) or isinstance(wall_ms, bool) or wall_ms <= 0:
            continue
        counted = False
        try:
            model = history.fit_model(records[:i], defaults, tasks_md_reader=lambda root, spec: None)
            if is_create:
                sim = simulate.simulate_create(model, empty, now_min=0.0, n=400, seed=1)
            else:
                graph = _wave_graph(rec)
                if graph is None:
                    continue
                sim = simulate.simulate_implement(graph, model, empty, now_min=0.0, n=400, seed=1)
            if sim.remaining_min is None:
                continue  # nothing was re-estimated: not counted
            runs += 1
            counted = True
            wall = wall_ms / 60000.0
            if sim.remaining_min[0] <= wall <= sim.remaining_min[2]:
                inside += 1
        except Exception:  # noqa: BLE001 - a run that cannot be re-estimated counts as outside
            if not counted:
                runs += 1
    rate = (inside / runs) if runs else 0.0
    body = {"runs": runs, "inside": inside, "rate": round(rate, 4)}
    if as_json:
        _emit_json(body)
    else:
        click.echo(f"{inside}/{runs} past runs finished inside the launch p10-p90 ({rate:.0%})")
