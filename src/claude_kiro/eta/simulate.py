"""Seeded Monte Carlo simulator for the run ETA (run-followups design, Simulation; seam S4 `conditional_sample`).

Pure: no clock, no I/O, no click. One `random.Random(seed)` per call draws every sample, and every pass draws its
values in a fixed order, so equal inputs give an identical `SimResult`. Times are effective minutes: `now_min` is the
caller's effective now, and every start time in the run state is converted with `progress.effective_min`.
"""

from __future__ import annotations

import heapq
import itertools
import math
import random
import statistics
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from claude_kiro.eta.history import CREATE_PHASES, STEPS, Graph, Model, match_pool, size_factor, start_model
from claude_kiro.progress import RunState, effective_min

Triple = Tuple[float, float, float]


@dataclass(frozen=True)
class SimResult:
    finish_min: Optional[Triple]         # p10, p50, p90 effective minutes from launch
    remaining_min: Optional[Triple]      # p10, p50, p90 minutes from now
    usd: Optional[Tuple[float, float]]   # p10, p90; None when any open task's pool is unpriced (1.9)
    task_median_min: Dict[str, float]    # per open task, median sampled duration
    critical_median_min: Optional[float]
    widest_step: Optional[str]           # None when there is nothing to match (no open task, no phase)
    phase_median_min: Dict[str, float]   # create: per phase, median over the passes that included it
    usd_p50: Optional[float] = None      # median cost; set whenever `usd` is


# --------------------------------------------------------------------------- numeric helpers


def _index(u: float, n: int) -> int:
    return min(int(max(u, 0.0) * n), n - 1)


def _pick(values: Sequence[float], u: float) -> float:
    return values[_index(u, len(values))]


def _at(points: Sequence[float], u: float) -> float:
    """Linear interpolation over an ascending quantile list at position u in [0, 1]; 0.0 for no points."""
    n = len(points)
    if n == 0:
        return 0.0
    if n == 1:
        return float(points[0])
    pos = min(max(u, 0.0), 1.0) * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    return points[lo] + (points[hi] - points[lo]) * (pos - lo)


def _pct(values: Sequence[float], p: float) -> float:
    xs = sorted(values)
    pos = p * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _triple(values: Sequence[float]) -> Triple:
    return (_pct(values, 0.1), _pct(values, 0.5), _pct(values, 0.9))


def plan_key(chain: Mapping[str, int], task: str) -> Tuple[int, int, str]:
    """`ck plan` order: longest chain first, then the numeric id (non-numeric ids count as 0), then the id string.

    Shared with `cli.worktree._plan`'s ready sort so the simulator starts tasks in the order `/spec:implement` does.
    """
    return (-chain.get(task, 0), int(task) if task.isdigit() else 0, task)


def conditional_sample(values: Sequence[float], elapsed: float, u: float) -> float:
    """S4: uniform (by `u`) among the values above `elapsed`; with none, `elapsed` plus the pool's p10. Never below elapsed."""
    above = sorted(v for v in values if v > elapsed)
    if above:
        return above[_index(u, len(above))]
    if not values:
        return float(elapsed)
    return float(elapsed) + max(0.0, _pct(values, 0.1))


# --------------------------------------------------------------------------- per-task plans


@dataclass(frozen=True)
class _Plan:
    unknown: bool                          # no matched samples and no bundled rate: no estimate
    samples: Tuple[Tuple, ...]             # matched tuples, in model order
    slots: Tuple[float, ...]               # scaled slot minutes per sample (agentMin x size factor)
    usd: Tuple[Optional[float], ...]       # usd x size factor per sample; None when the tuple is unpriced
    rate: Optional[float]                  # median usd per agent minute of the priced samples
    rev_pool: Tuple[float, ...]            # the pool's own review waits
    land_pool: Tuple[float, ...]           # the pool's own landing waits
    bundled_slots: Tuple[float, ...]       # bundled agent-minute quantiles x size factor (no samples)
    step_index: int                        # index into STEPS of the widest step used
    priced: bool


def _plan(graph: Graph, model: Model, task: str, roles: Mapping) -> _Plan:
    track = graph.tracks.get(task, "unknown")
    risk = graph.risk.get(task, "normal")
    sm = start_model(track, risk, graph.complexity.get(task), roles)
    match = match_pool(model, track, sm, risk)
    step = STEPS.index(match.step)
    task_size = graph.sizes.get(task)
    if match.samples:
        samples = tuple(match.samples)
        factors = [size_factor(model.size, task_size, s[6]) for s in samples]
        slots = tuple(s[0] * f for s, f in zip(samples, factors))
        usd = tuple(None if s[5] is None else s[5] * f for s, f in zip(samples, factors))
        rates = [s[5] / s[0] for s in samples if s[5] is not None and s[0] > 0]
        return _Plan(
            unknown=False, samples=samples, slots=slots, usd=usd,
            rate=statistics.median(rates) if rates else None,
            rev_pool=tuple(s[3] for s in samples if s[3] is not None),
            land_pool=tuple(s[4] for s in samples if s[4] is not None),
            bundled_slots=(), step_index=step,
            priced=any(u is not None for u in usd) or bool(model.bundled_usd),
        )
    if match.quantiles is not None:
        factor = size_factor(model.size, task_size, None)
        bundled = tuple(v * factor for v in match.quantiles["agentMinutes"])
        return _Plan(unknown=False, samples=(), slots=(), usd=(), rate=None, rev_pool=(), land_pool=(),
                     bundled_slots=bundled, step_index=step, priced=bool(model.bundled_usd))
    return _Plan(unknown=True, samples=(), slots=(), usd=(), rate=None, rev_pool=(), land_pool=(),
                 bundled_slots=(), step_index=step, priced=False)


def _wait(pool: Sequence[float], bundled: Sequence[float], u: float) -> float:
    if pool:
        return _pick(pool, u)
    if bundled:
        return _at(bundled, u)
    return 0.0


def _draw(plan: _Plan, model: Model, up: float, ur: float, ul: float, uc: float):
    """One joint tuple draw: (slot minutes, review wait, landing wait, cost or None)."""
    bw_rev = model.bundled_waits.get("reviewMinutes", ())
    bw_land = model.bundled_waits.get("landingMinutes", ())
    if plan.samples:
        i = _index(up, len(plan.samples))
        s = plan.samples[i]
        slot = plan.slots[i]
        rev = s[3] if s[3] is not None else _wait(plan.rev_pool, bw_rev, ur)
        land = s[4] if s[4] is not None else _wait(plan.land_pool, bw_land, ul)
        if plan.usd[i] is not None:
            cost: Optional[float] = plan.usd[i]
        elif plan.rate is not None:
            cost = slot * plan.rate
        elif model.bundled_usd:
            cost = slot * _at(model.bundled_usd, uc)
        else:
            cost = None
        return slot, rev, land, cost
    slot = _at(plan.bundled_slots, up)
    rev = _wait((), bw_rev, ur)
    land = _wait((), bw_land, ul)
    cost = slot * _at(model.bundled_usd, uc) if model.bundled_usd else None
    return slot, rev, land, cost


# --------------------------------------------------------------------------- implement


def _schedule(active: Sequence[str], running: Mapping[str, float], hand: frozenset, dependents: Mapping[str, List[str]],
              indeg0: Mapping[str, int], chain: Mapping[str, int], slot_of: Mapping[str, float],
              rev_of: Mapping[str, float], land_of: Mapping[str, float], cap: int, busy: int = 0) -> Dict[str, float]:
    """One pass of the slot scheduler in effective minutes from now. Returns each finished task's finish time.

    Running tasks already hold a slot; hand-landing tasks hold none. `busy` counts excluded running tasks, which hold
    a slot for the whole pass. Pending tasks start in `ck plan` order (`plan_key`) while fewer than `cap` slots are
    busy. Review and landing waits follow the slot and hold none. A task on a dependency cycle is never released, so
    it is missing from the result.
    """
    free = cap - len(running) - busy
    indeg = dict(indeg0)
    finish: Dict[str, float] = {}
    events: List[Tuple[float, int, int, str]] = []  # (time, seq, kind 0 slot-free | 1 finished, task)
    seq = itertools.count()
    ready: List[Tuple[Tuple[int, int, str], str]] = []
    for t in running:
        heapq.heappush(events, (slot_of[t], next(seq), 0, t))
        heapq.heappush(events, (slot_of[t] + rev_of[t] + land_of[t], next(seq), 1, t))
    for t in hand:
        heapq.heappush(events, (land_of[t], next(seq), 1, t))
    for t in active:
        if t not in running and t not in hand and indeg[t] == 0:
            heapq.heappush(ready, (plan_key(chain, t), t))
    now = 0.0
    while True:
        while free > 0 and ready:
            _, t = heapq.heappop(ready)
            free -= 1
            end = now + slot_of[t]
            heapq.heappush(events, (end, next(seq), 0, t))
            heapq.heappush(events, (end + rev_of[t] + land_of[t], next(seq), 1, t))
        if not events:
            break
        now, _, kind, t = heapq.heappop(events)
        if kind == 0:
            free += 1
            continue
        finish[t] = now
        for d in dependents[t]:
            indeg[d] -= 1
            if indeg[d] == 0 and d not in running and d not in hand:
                heapq.heappush(ready, (plan_key(chain, d), d))
    return finish


def simulate_implement(graph: Graph, model: Model, state: RunState, *, now_min: float, n: int, seed: int,
                       exclude: frozenset = frozenset(), roles: Optional[Mapping] = None,
                       max_concurrent: Optional[int] = None) -> SimResult:
    """Finish and cost quantiles for the open tasks of `graph` (not done, not in `exclude`), over `n` passes.

    `roles` feeds the start model (S19); without it every task starts on the `inherit` family. `max_concurrent`
    caps the slots together with `graph.slots`. Callers pass `exclude=held` for the runnable-only pass.
    """
    rng = random.Random(seed)
    now = float(now_min)
    roles = roles if isinstance(roles, Mapping) else {}
    active = [t for t in graph.order if t not in graph.done and t not in exclude]
    if not active:
        return SimResult((now, now, now), (0.0, 0.0, 0.0), None, {}, None, None, {})
    plans = {t: _plan(graph, model, t, roles) for t in active}
    widest = STEPS[max(p.step_index for p in plans.values())]
    if any(p.unknown for p in plans.values()):
        return SimResult(None, None, None, {}, None, widest, {})
    usd_possible = all(p.priced for p in plans.values())

    cap = max(1, graph.slots if max_concurrent is None else min(graph.slots, max_concurrent))
    running: Dict[str, float] = {}
    hand = set()
    for t in active:
        tp = state.tasks.get(t)
        if tp is None:
            continue
        if tp.status == "running":
            running[t] = effective_min(state.waits, tp.first_start_min) if tp.first_start_min is not None else now
        elif tp.status == "hand":
            hand.add(t)
    hand_set = frozenset(hand)

    aset = set(active)
    dependents: Dict[str, List[str]] = {t: [] for t in active}
    indeg: Dict[str, int] = {t: 0 for t in active}
    for t in active:
        for d in graph.deps.get(t, ()):
            if d in aset:
                indeg[t] += 1
                dependents[d].append(t)
    plan_order = sorted(active, key=lambda t: plan_key(graph.chain, t))
    # Excluded running tasks (held) still occupy their slot; they are not simulated, so they hold it for the whole pass.
    busy = sum(1 for t in exclude if t not in graph.done and state.tasks.get(t) is not None
               and state.tasks[t].status == "running")

    spans: List[float] = []
    crits: List[float] = []
    costs: List[float] = []
    per_task: Dict[str, List[float]] = {t: [] for t in active}
    unpriced = False
    for _ in range(max(1, n)):
        draws = {t: (rng.random(), rng.random(), rng.random(), rng.random()) for t in active}
        slot_of: Dict[str, float] = {}
        rev_of: Dict[str, float] = {}
        land_of: Dict[str, float] = {}
        dur: Dict[str, float] = {}
        cost = 0.0
        for t in active:
            up, ur, ul, uc = draws[t]
            plan = plans[t]
            slot, rev, land, base = _draw(plan, model, up, ur, ul, uc)
            frac = 1.0  # share of the tuple's cost still to be spent
            if t in running:
                elapsed = max(0.0, now - running[t])
                values = plan.slots if plan.samples else plan.bundled_slots
                full = slot
                slot = conditional_sample(values, elapsed, up) - elapsed
                frac = min(1.0, slot / full) if full > 0 else 0.0
            elif t in hand_set:
                slot, rev = 0.0, 0.0
                frac = 0.0  # the agent work is already spent
            slot_of[t], rev_of[t], land_of[t] = slot, rev, land
            dur[t] = slot + rev + land
            if base is None:
                unpriced = True
            else:
                cost += base * frac
        finish = _schedule(active, running, hand_set, dependents, indeg, graph.chain, slot_of, rev_of, land_of, cap,
                           busy)
        if len(finish) < len(active):
            # Some open task never became ready: it sits on a dependency cycle or behind one. Its time is unknown.
            return SimResult(None, None, None, {}, None, widest, {})
        spans.append(max(finish.values()) if finish else 0.0)
        best: Dict[str, float] = {}
        for t in plan_order:
            pre = max((best[d] for d in graph.deps.get(t, ()) if d in best), default=0.0)
            best[t] = dur[t] + pre
        crits.append(max(best.values()))
        costs.append(cost)
        for t in active:
            per_task[t].append(dur[t])

    span_triple = _triple(spans)
    usd = None if (not usd_possible or unpriced) else (_pct(costs, 0.1), _pct(costs, 0.9))
    return SimResult(
        finish_min=tuple(now + v for v in span_triple),
        remaining_min=span_triple,
        usd=usd,
        task_median_min={t: statistics.median(v) for t, v in per_task.items()},
        critical_median_min=statistics.median(crits),
        widest_step=widest,
        phase_median_min={},
        usd_p50=None if usd is None else _pct(costs, 0.5),
    )


# --------------------------------------------------------------------------- create


def simulate_create(model: Model, state: RunState, *, now_min: float, n: int, seed: int) -> SimResult:
    """Finish quantiles for a create run: the running phase by S4, each pending phase included with its presence.

    Phases run in order, so the finish is now plus the sum of the included phase durations. Cost is not simulated for
    create runs (`usd` is None).
    """
    rng = random.Random(seed)
    now = float(now_min)
    phases = model.phases
    unknown = SimResult(None, None, None, {}, None, None, {})
    if not phases:
        return unknown

    run_name = state.phase_running if state.phase_running in phases else None
    if run_name is not None and not phases[run_name]["minutes"]:
        return unknown  # a running phase with no samples has no estimate; it must not read as 0 minutes left
    elapsed = 0.0
    if run_name is not None and state.phase_started_min is not None:
        elapsed = max(0.0, now - effective_min(state.waits, state.phase_started_min))
    pending = []
    for t in CREATE_PHASES:
        if t not in phases or t in state.phases_done or t == state.phase_running or float(phases[t]["presence"]) <= 0:
            continue
        if not phases[t]["minutes"]:
            return unknown  # a phase that may run but has no samples cannot be dropped silently
        pending.append(t)
    minutes = {t: tuple(float(x) for x in phases[t]["minutes"]) for t in ([run_name] if run_name else []) + pending}
    presence = {t: float(phases[t]["presence"]) for t in pending}
    candidates = ([run_name] if run_name else []) + pending
    cost_ok = bool(candidates) and all(
        phases[t]["usd"] and len(phases[t]["usd"]) == len(phases[t]["minutes"])
        and all(u is not None for u in phases[t]["usd"]) for t in candidates)

    totals: List[float] = []
    costs: List[float] = []
    per_phase: Dict[str, List[float]] = {}
    for _ in range(max(1, n)):
        total = 0.0
        cost = 0.0
        included: Dict[str, float] = {}
        if run_name is not None:
            u = rng.random()
            remaining = conditional_sample(minutes[run_name], elapsed, u) - elapsed
            included[run_name] = remaining
            total += remaining
            if cost_ok:
                cost += phases[run_name]["usd"][_index(u, len(minutes[run_name]))]
        for t in pending:
            u_inc = rng.random()
            u_val = rng.random()
            if u_inc < presence[t]:
                i = _index(u_val, len(minutes[t]))
                included[t] = minutes[t][i]
                total += minutes[t][i]
                if cost_ok:
                    cost += phases[t]["usd"][i]
        totals.append(total)
        costs.append(cost)
        for t, v in included.items():
            per_phase.setdefault(t, []).append(v)

    total_triple = _triple(totals)
    usd = (_pct(costs, 0.1), _pct(costs, 0.9)) if cost_ok else None
    return SimResult(
        finish_min=tuple(now + v for v in total_triple),
        remaining_min=total_triple,
        usd=usd,
        task_median_min={},
        critical_median_min=None,
        widest_step=None,  # create has no match ladder; None says so instead of a placeholder step
        phase_median_min={t: statistics.median(v) for t, v in per_phase.items()},
        usd_p50=_pct(costs, 0.5) if cost_ok else None,
    )
