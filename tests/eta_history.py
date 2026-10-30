"""Custom collected histories for the ETA CLI tests (run-followups Task 19).

Builds on `eta_helpers` (fake Claude homes, the spec builder and the real `ck stats collect --all`). Where the
shared pools do not have the shape a case needs (one dimension changed at a time), `custom_history` writes runs from
an explicit per-task description and lets the real collector produce the store. Test-owned; imports no project code.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Callable, List, Optional, Sequence

import eta_helpers as h
from stats_helpers import AgentSpec, FakeClaudeHome, implement_result, iso, to_ms


@dataclass
class T:
    """One past task: what it was and how it went."""

    track: str = "impl"
    model: str = "sonnet"
    risk: str = "normal"
    minutes: float = 3.0
    attempts: int = 1
    wait: float = 0.0  # minutes of review wait and of landing wait each
    files: int = 1
    criteria: int = 1


def _agents(plan: Sequence[h.PlanTask], waits: Sequence[float]) -> List[AgentSpec]:
    agents: List[AgentSpec] = []
    for p, w in zip(plan, waits):
        n = p.defn.id
        total_ms = (p.finished - p.started).total_seconds() * 1000
        # The collector reads a test-track task from the " [test]" label suffix (the test-writer role), as in eta_helpers.
        suffix = " [test]" if p.defn.track == "test" else ""
        for k in range(p.attempts):
            agents.append(AgentSpec(f"task {n}{suffix}" if k == 0 else f"task {n} retry {k}", model=f"claude-{p.start_model}-5-5",
                                    phase="Wave 1", duration_ms=int(total_ms / p.attempts), attempt=k + 1,
                                    started_at_ms=to_ms(iso(p.started)) + int(k * total_ms / p.attempts)))
        fin = to_ms(iso(p.finished))
        wm = int(w * 60_000)
        agents.append(AgentSpec(f"review {n}", model="claude-opus-5-5", phase="Wave 1", duration_ms=120_000,
                                started_at_ms=fin + wm, queued_at_ms=fin if wm else None))
        land_q = fin + wm + 120_000
        agents.append(AgentSpec(f"land {n}", model="claude-haiku-5-5", phase="Wave 1", duration_ms=60_000,
                                started_at_ms=land_q + wm, queued_at_ms=land_q if wm else None))
    return agents


def custom_history(root: Path, make_run: Callable[[int], Sequence[T]], runs: int = 12) -> Path:
    """Collect `runs` implement runs whose tasks are `make_run(i)`; returns the store's `xdg-data` directory."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    env = h._pool_env(root)
    home = FakeClaudeHome(Path(env["CLAUDE_CONFIG_DIR"]))
    wanted = {}
    for i in range(runs):
        specs = list(make_run(i))
        wanted[f"cust-{i:02d}"] = {str(j + 1): t.track for j, t in enumerate(specs)}
        start = h._RUN0 + timedelta(days=i)
        plan: List[h.PlanTask] = []
        cursor = start
        for j, t in enumerate(specs):
            fin = cursor + timedelta(minutes=t.minutes * t.attempts)
            merged = fin + timedelta(minutes=2 * t.wait + 3)
            d = h.TaskDef(id=j + 1, track=t.track, model=t.model, risk=None if t.risk == "normal" else t.risk,
                          files=t.files, criteria=t.criteria)
            plan.append(h.PlanTask(d, 1, t.model, t.risk, t.minutes, t.attempts, False, cursor, fin, merged, t.wait > 0))
            cursor = merged + timedelta(minutes=1)
        spec = f"cust-{i:02d}"
        proj = h.eta_spec(root / "projects" / spec, [p.defn for p in plan], spec=spec, into=f"integrate/{spec}")
        session = home.session(f"-work-{spec}", cwd=proj, git_branch=f"integrate/{spec}")
        session.implement_run(
            f"wf_cust-{i:02d}", start=iso(start - timedelta(minutes=1)),
            duration_ms=int((cursor - start).total_seconds() * 1000) + 120_000, args={"spec": spec, "root": str(proj), "tracks": wanted[spec]},
            result=implement_result([h._task_spec(p) for p in plan], into=f"integrate/{spec}", base=iso(start)),
            agents=_agents(plan, [t.wait for t in specs]))
    h._collect(root, env)
    store = root / "xdg-data"
    # Sanity: the collected records carry the track each task was written with (the ladder keys on tasks[].track).
    got = {(r.get("project") or {}).get("spec"): {str(t.get("task")): t.get("track") for t in r.get("tasks") or []} for r in records(store)}
    for spec, tracks in wanted.items():
        assert got.get(spec) == tracks, (spec, got.get(spec), tracks)
    return store


def install(store: Path, xdg: Path) -> Path:
    shutil.copytree(store, xdg, dirs_exist_ok=True)
    return Path(xdg)


def records(store: Path) -> List[dict]:
    return [json.loads(p.read_text()) for p in sorted((Path(store) / "claude-kiro" / "runs").glob("*.json"))]


def create_history(root: Path, second: Callable[[int], bool], runs: int = 8) -> Path:
    """`runs` collected create runs; a second consistency pass (5% of the base time, in the same phase) where `second(i)`."""
    from stats_helpers import create_result

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    env = h._pool_env(root)
    home = FakeClaudeHome(Path(env["CLAUDE_CONFIG_DIR"]))
    for i in range(runs):
        base_ms = h.create_total_minutes(i) * 60_000
        two = second(i)
        steps = list(h._CREATE_STEPS) + [("consistency check: pass 1", "Consistency Check", 0.10)]
        if two:
            steps.append(("consistency check: pass 2", "Consistency Check", 0.05))
        total_ms = int(base_ms * (1.05 if two else 1.0))
        proj = h.eta_spec(root / "projects" / f"orbit-{i:02d}", [h.TaskDef(1)], spec=f"orbit-{i:02d}")
        session = home.session(f"-work-orbit-{i:02d}", cwd=proj, git_branch="main")
        start = h._RUN0 + timedelta(days=i, hours=-1)
        cursor = to_ms(iso(start))
        agents = []
        for label, phase, share in steps:
            dur = int(base_ms * share)
            agents.append(AgentSpec(label, model="claude-opus-5-5" if label.startswith("review") else "claude-sonnet-5-5",
                                    phase=phase, duration_ms=dur, started_at_ms=cursor))
            cursor += dur
        session.create_run(f"wf_cvar-{i:02d}", start=iso(start), duration_ms=total_ms,
                           args={"feature": f"orbit-{i:02d}", "root": str(proj)},
                           result=create_result(feature=f"orbit-{i:02d}", consistency_passes=2 if two else 1), agents=agents)
    h._collect(root, env)
    return root / "xdg-data"


# --------------------------------------------------------------------------- bundled defaults: aggregates only (7.2)

DEFAULTS_FAMILIES = {"schema", "quantiles", "tasks", "waits", "usdPerAgentMinute", "phases", "size"}
_ID_PATTERNS = (r"wf_", r"ag_", r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", r"[0-9a-fA-F]{12,}",
                r"\d{4}-\d{2}-\d{2}", r"\d{2}:\d{2}:\d{2}", r"/")


def walk_defaults(payload, schema: str, path: str = ""):
    """Yield (path, key-or-None, value) for every key and leaf; fails the caller's assert on a non-numeric leaf."""
    import math
    import re

    if isinstance(payload, dict):
        for key, value in payload.items():
            assert isinstance(key, str) and key, (path, key)
            for pattern in _ID_PATTERNS:
                assert not re.search(pattern, key), f"key {key!r} at {path!r} looks like {pattern}"
            yield from walk_defaults(value, schema, f"{path}.{key}" if path else key)
    elif isinstance(payload, list):
        for i, value in enumerate(payload):
            yield from walk_defaults(value, schema, f"{path}[{i}]")
    elif isinstance(payload, str):
        assert path == "schema" and payload == schema, f"string value {payload!r} at {path!r}"
    else:
        assert isinstance(payload, (int, float)) and not isinstance(payload, bool), (path, payload)
        assert math.isfinite(payload), (path, payload)
    yield path, payload


def assert_aggregates_only(payload, schema: str = "ck-eta-defaults/1") -> None:
    assert isinstance(payload, dict) and payload.get("schema") == schema
    assert set(payload) <= DEFAULTS_FAMILIES, set(payload) - DEFAULTS_FAMILIES
    list(walk_defaults(payload, schema))
