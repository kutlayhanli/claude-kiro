"""Pure backfill rules for unmarked implement-mode runs (design A12, A13). No I/O, no clock, no network.

`group_backfill` is a dict view over `candidate_reason`, `roles.match_legacy` and `group_agents`: it has no
grouping logic of its own, so the properties check the functions the CLI runs.
"""

import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from . import roles
from .agentrun import DEFAULT_GAP_MINUTES, MetaView, parse_marker


@dataclass(frozen=True)
class BackfillAgent:
    agent_id: str
    session_id: str
    spec: Optional[str]
    start_ms: Optional[int]
    end_ms: Optional[int]
    role: str


@dataclass(frozen=True)
class BackfillGroup:
    record_id: str
    session_id: str
    spec: str
    agent_ids: Tuple[str, ...]
    spec_agent_ids: Tuple[str, ...]
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class GroupPlan:
    groups: Tuple[BackfillGroup, ...]
    excluded: Tuple[Tuple[str, str, str], ...]   # (agent_id, session_id, reason)


def group_id(session_id: str, first_agent_id: str) -> str:
    """`ag_` + the first 8 hex digits of sha256("backfill\\0" + session + "\\0" + first agent)."""
    digest = hashlib.sha256(("backfill\0" + session_id + "\0" + first_agent_id).encode("utf-8")).hexdigest()
    return "ag_" + digest[:8]


def candidate_reason(meta: MetaView) -> Optional[str]:
    """A12: None for a candidate; otherwise the first reason that excludes it, in the Harness order."""
    if meta.under_workflow:
        return "workflow agent"
    if parse_marker(meta.description) is not None:
        return "carries a marker"
    if not (isinstance(meta.agent_type, str) and meta.agent_type in roles.SPEC_AGENT_TYPES):
        return "not a spec agent type"
    return None


def group_agents(agents: Sequence[BackfillAgent], gap_ms: int) -> GroupPlan:
    """A13: spec-named agents form (session, spec) groups split where the gap from the running maximum end to
    the next start exceeds `gap_ms`; spec-less agents then join the nearest group of their session within
    `gap_ms` without moving its bounds; the rest are left out as `no spec`. Agents without timing: `no timing`.
    """
    excluded: List[Tuple[str, str, str]] = []
    timed: List[BackfillAgent] = []
    for agent in agents:
        if agent.start_ms is None or agent.end_ms is None:
            excluded.append((agent.agent_id, agent.session_id, "no timing"))
        else:
            timed.append(agent)

    keyed: Dict[Tuple[str, str], List[BackfillAgent]] = {}
    for agent in timed:
        if agent.spec is not None:
            keyed.setdefault((agent.session_id, agent.spec), []).append(agent)

    # Each raw group: (session_id, spec, members sorted by (start, id), running-max end).
    raw: List[Tuple[str, str, List[BackfillAgent], int]] = []
    for session_id, spec in sorted(keyed):
        members = sorted(keyed[(session_id, spec)], key=lambda a: (a.start_ms, a.agent_id))
        current = [members[0]]
        top = members[0].end_ms
        for agent in members[1:]:
            if agent.start_ms - top <= gap_ms:
                current.append(agent)
                top = max(top, agent.end_ms)
            else:
                raw.append((session_id, spec, current, top))
                current, top = [agent], agent.end_ms
        raw.append((session_id, spec, current, top))

    joined: Dict[int, List[BackfillAgent]] = {}
    for agent in sorted((a for a in timed if a.spec is None), key=lambda a: (a.start_ms, a.agent_id)):
        best: Optional[Tuple[int, int, int]] = None
        for index, (session_id, _spec, members, end) in enumerate(raw):
            if session_id != agent.session_id:
                continue
            start = members[0].start_ms
            if agent.start_ms <= end + gap_ms and agent.end_ms >= start - gap_ms:
                distance = max(0, start - agent.end_ms, agent.start_ms - end)
                rank = (distance, start, index)
                if best is None or rank < best:
                    best = rank
        if best is None:
            excluded.append((agent.agent_id, agent.session_id, "no spec"))
        else:
            joined.setdefault(best[2], []).append(agent)

    groups: List[BackfillGroup] = []
    for index, (session_id, spec, members, end) in enumerate(raw):
        extra = joined.get(index, [])
        everyone = sorted(members + extra, key=lambda a: (a.start_ms, a.agent_id))
        groups.append(BackfillGroup(
            record_id=group_id(session_id, members[0].agent_id),
            session_id=session_id,
            spec=spec,
            agent_ids=tuple(a.agent_id for a in everyone),
            spec_agent_ids=tuple(a.agent_id for a in members),
            start_ms=members[0].start_ms,
            end_ms=end,
        ))
    groups.sort(key=lambda g: (g.session_id, g.start_ms, g.record_id))
    return GroupPlan(groups=tuple(groups), excluded=tuple(sorted(excluded, key=lambda e: (e[1], e[0]))))


def group_backfill(agents: Sequence[Dict[str, object]], gap_minutes: int = DEFAULT_GAP_MINUTES) -> Dict[str, list]:
    """Harness dict view: {"groups": [...], "excluded": [{"agentId", "reason"}]} over A12 then A13.

    Input agents are dicts with agentId, sessionId, agentType, description, startMs, endMs and workflow.
    """
    excluded: List[Dict[str, str]] = []
    candidates: List[BackfillAgent] = []
    for raw_agent in agents:
        meta = MetaView(
            agent_id=raw_agent["agentId"],
            session_id=raw_agent["sessionId"],
            agent_type=raw_agent.get("agentType"),
            description=raw_agent.get("description"),
            under_workflow=bool(raw_agent.get("workflow")),
        )
        reason = candidate_reason(meta)
        if reason is not None:
            excluded.append({"agentId": meta.agent_id, "reason": reason})
            continue
        legacy = roles.match_legacy(meta.description, meta.agent_type)
        candidates.append(BackfillAgent(
            agent_id=meta.agent_id,
            session_id=meta.session_id,
            spec=legacy.spec,
            start_ms=raw_agent.get("startMs"),
            end_ms=raw_agent.get("endMs"),
            role=legacy.role,
        ))

    plan = group_agents(candidates, gap_minutes * 60_000)
    excluded.extend({"agentId": agent_id, "reason": reason} for agent_id, _sid, reason in plan.excluded)
    groups = [
        {
            "id": g.record_id,
            "sessionId": g.session_id,
            "spec": g.spec,
            "agentIds": list(g.agent_ids),
            "specAgentIds": list(g.spec_agent_ids),
            "startMs": g.start_ms,
            "endMs": g.end_ms,
        }
        for g in plan.groups
    ]
    return {"groups": groups, "excluded": sorted(excluded, key=lambda e: e["agentId"])}
