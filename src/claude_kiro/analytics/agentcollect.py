"""The I/O side of agent-mode analytics: begin, marked-agent discovery, record building, open-manifest sweep.

This module never prices or aggregates on its own: it builds `collect._Agent` lists and calls
`collect._build_record(..., agent_run=ctx)`. The pure rules live in `agentrun` and `roles`.
"""

import json
import os
import re
import secrets
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from claude_kiro import bench, manifest
from claude_kiro.analytics import agentrun, backfill, collect, roles, store
from claude_kiro.analytics.agentrun import BeginResult, MetaView

_CONFIG_ROLES = ("implementer", "test_writer", "reviewer", "reviewer_risky", "fixer", "resolver", "orchestrator")
_GIT_TIMEOUT = 15


@dataclass(frozen=True)
class AgentFile:
    meta: MetaView
    meta_path: Path
    transcript: Path  # sibling agent-<id>.jsonl (may not exist)


@dataclass(frozen=True)
class AgentRunContext:
    run_id: str
    kind: str
    workflow_name: str
    matcher: Callable[[object, object], roles.LabelMatch]
    spec: Optional[str]
    root: Optional[str]
    into: Optional[str]
    start_ms: Optional[int]
    end_ms: Optional[int]
    roles: Optional[Mapping[str, Mapping[str, Any]]]
    done_at_start: Optional[FrozenSet[str]]
    manifest: str  # "present" | "missing"
    partial: bool
    backfill: bool
    gap_minutes: Optional[int]
    spec_agent_ids: Optional[FrozenSet[str]] = None


# ---------------------------------------------------------------- begin


def _now_ms(now: datetime) -> int:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return int(now.timestamp() * 1000)


def _git_out(cwd: Path, *args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=_GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def _project_dir_name(cwd: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def _detect_session(home: Path, cwd: Path) -> Optional[str]:
    """Newest session transcript in the project directory Claude Code uses for `cwd`."""
    try:
        best: Optional[Tuple[float, str]] = None
        for name in {_project_dir_name(cwd), _project_dir_name(cwd.resolve())}:
            for f in (Path(home) / "projects" / name).glob("*.jsonl"):
                try:
                    mt = f.stat().st_mtime
                except OSError:
                    continue
                if best is None or (mt, f.stem) > best:
                    best = (mt, f.stem)
        return best[1] if best else None
    except OSError:
        return None


def _project_roles(cwd: Path, overrides: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {r: {"model": None, "effort": None, "enabled": None} for r in _CONFIG_ROLES}
    try:
        from claude_kiro import agents as ag

        resolved = ag.resolve(cwd, list(overrides))["roles"]
    except Exception:  # noqa: BLE001 - every lookup degrades to null
        return out

    def pick(d: Any, mk: str, ek: str, nk: str) -> Dict[str, Any]:
        d = d if isinstance(d, Mapping) else {}
        model, effort, enabled = d.get(mk), d.get(ek), d.get(nk)
        return {"model": model if isinstance(model, str) else None,
                "effort": effort if isinstance(effort, str) else None,
                "enabled": enabled if isinstance(enabled, bool) else None}

    for role in _CONFIG_ROLES:
        if role == "reviewer_risky":
            out[role] = pick(resolved.get("reviewer"), "risky_model", "risky_effort", "enabled")
        else:
            out[role] = pick(resolved.get(role), "model", "effort", "enabled")
    return out


def _done_at_start(root: Path, into: Optional[str], spec: str) -> Optional[List[str]]:
    if not into:
        return None
    try:
        from claude_kiro.hooks._shared.spec_parser import parse_tasks_text
        from claude_kiro.paths import SPECS_DIR

        text = _git_out(root, "show", f"{into}:{SPECS_DIR}/{spec}/tasks.md")
        if text is None:
            return None
        return [str(t.num) for t in parse_tasks_text(text) if t.done]
    except Exception:  # noqa: BLE001
        return None


def begin_run(*, kind: str, spec: str, cwd: Path, into: Optional[str], session_id: Optional[str],
              overrides: Sequence[str], home: Path, now: datetime,
              env: Optional[Mapping[str, str]] = None) -> BeginResult:
    """Make a run id and write the 0600 run manifest. Best effort: a failed write still returns the id."""
    environ = os.environ if env is None else env
    cwd = Path(cwd)
    spec = Path(str(spec).rstrip("/\\")).name or str(spec)

    top = _git_out(cwd, "rev-parse", "--show-toplevel")
    root_path = Path(top.strip()) if top and top.strip() else cwd.resolve()
    target = into
    if not target:
        branch = _git_out(root_path, "branch", "--show-current")
        target = branch.strip() if branch and branch.strip() else None
    session = session_id or _detect_session(Path(home), cwd)
    done = _done_at_start(root_path, target, spec) if kind == "implement" else None

    draws = iter([d.strip() for d in (environ.get("CK_TEST_RUN_ID_DRAWS") or "").split(",") if d.strip()])

    def draw() -> str:
        try:
            return next(draws)
        except StopIteration:
            return secrets.token_hex(4)

    try:
        taken = store.taken_run_ids(env)
    except OSError:
        taken = set()
    run_id = agentrun.new_run_id(taken, draw)

    try:
        version = manifest.ck_version()
    except Exception:  # noqa: BLE001
        version = None
    data = {
        "schema": agentrun.MANIFEST_SCHEMA, "runId": run_id, "kind": kind,
        "startTime": collect._iso(_now_ms(now)), "sessionId": session, "ckVersion": version,
        "spec": spec, "root": str(root_path), "into": target, "doneAtStart": done,
        "agents": _project_roles(cwd, overrides),
    }
    try:
        path = store.write_manifest(data, env)
    except OSError as e:
        reason = e.strerror or type(e).__name__
        return BeginResult(run_id, None,
                           f"⚠ could not write the run manifest ({reason}); this run will be collected without it")
    return BeginResult(run_id, path, None)


# ---------------------------------------------------------------- discovery


def _read_meta(path: Path) -> Optional[MetaView]:
    name = path.name
    aid = name[len("agent-"):-len(".meta.json")]
    if not aid or not collect._SAFE_ID.match(aid):
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    desc, atype = data.get("description"), data.get("agentType")
    return MetaView(agent_id=aid, session_id=path.parent.parent.name,
                    agent_type=atype if isinstance(atype, str) else None,
                    description=desc if isinstance(desc, str) else None, under_workflow=False)


def _search(dirs: List[Path], run_id: str, workers: int) -> Tuple[List[AgentFile], int]:
    paths: List[Path] = []
    for d in dirs:
        try:
            paths.extend(sorted(d.glob("agent-*.meta.json")))
        except OSError:
            continue
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        views = list(pool.map(_read_meta, paths))
    bad = sum(1 for v in views if v is None)
    pairs = [(v, p) for v, p in zip(views, paths) if v is not None]
    chosen = agentrun.select_marked([v for v, _ in pairs], run_id)
    by_view = {id(v): p for v, p in pairs}
    files = [AgentFile(v, by_view[id(v)], by_view[id(v)].with_name(f"agent-{v.agent_id}.jsonl")) for v in chosen]
    return files, bad


def _find(home: Path, run_id: str, session_id: Optional[str], workers: int
          ) -> Tuple[List[AgentFile], List[Path], int]:
    projects = Path(home) / "projects"
    searched: List[Path] = []
    if isinstance(session_id, str) and session_id and collect._SAFE_ID.match(session_id):
        dirs = sorted(projects.glob(f"*/{session_id}/subagents"))
        searched.extend(dirs)
        files, bad = _search(dirs, run_id, workers)
        if files:
            return files, searched, bad
    dirs = sorted(projects.glob("*/*/subagents"))
    searched = list(dict.fromkeys(searched + dirs))
    files, bad = _search(dirs, run_id, workers)
    return files, searched, bad


def find_marked_agents(home: Path, run_id: str, session_id: Optional[str], workers: int = 8
                       ) -> Tuple[List[AgentFile], List[Path]]:
    """The agents whose description carries the run's marker: the manifest session first, else every session."""
    files, searched, _ = _find(home, run_id, session_id, workers)
    return files, searched


# ---------------------------------------------------------------- collect


def _marker_matcher(label: object, agent_type: object) -> roles.LabelMatch:
    marker = agentrun.parse_marker(label)
    return roles.match_agent(marker.rest if marker else None, agent_type)


def _is_create_step(description: Optional[str]) -> bool:
    marker = agentrun.parse_marker(description)
    return marker is not None and marker.rest.startswith("create ") and \
        marker.rest[len("create "):] in roles.CREATE_STEPS


def _phase_of(description: Optional[str]) -> Optional[str]:
    marker = agentrun.parse_marker(description)
    if marker is not None and marker.rest.startswith("create "):
        return roles.CREATE_STEP_PHASES.get(marker.rest[len("create "):])
    return None


def _to_agent(af: AgentFile) -> collect._Agent:
    return collect._Agent(
        agent_id=af.meta.agent_id, label=af.meta.description, phase=_phase_of(af.meta.description),
        model_hint=None, state=None, started_ms=None, duration_ms=None, tokens=None,
        transcript=af.transcript, meta_path=af.meta_path, agent_type=af.meta.agent_type)


def _failed(run_id: str, error: str, reason: str) -> collect.CollectOutcome:
    return collect.CollectOutcome(run_id, "failed", reason=reason, error=error)


def collect_agent_run(home: Path, run_id: str, *, now: datetime, pricing: Optional[dict] = None,
                      fresh: bool = False, partial: bool = False,
                      env: Optional[Mapping[str, str]] = None) -> collect.CollectOutcome:
    """Build the local record of one agent-mode run. Writing and closing the manifest are `finish_collected`."""
    pricing = pricing or {}
    home = Path(home)
    data, state = store.read_manifest(run_id, env)
    check = None
    if state == "malformed" or (data is not None and not (check := agentrun.check_manifest(data, run_id)).ok):
        return _failed(run_id, "malformed", "run manifest not recognised")
    has_manifest = data is not None

    session = data.get("sessionId") if has_manifest and isinstance(data.get("sessionId"), str) else None
    files, searched, bad = _find(home, run_id, session, 8)
    if not files:
        if not has_manifest:
            dirs = f"{store.open_manifests_dir(env)}, {store.closed_manifests_dir(env)}"
            return _failed(run_id, "not_found",
                           f"run {run_id} not found: no manifest in {dirs} and no agent carries "
                           f"[ck:{run_id}] under {home / 'projects'}")
        return _failed(run_id, "no_agents", f"no agents carry the marker [ck:{run_id}]")

    def _started(af: AgentFile) -> Tuple[int, str]:
        try:
            first_ts = collect._timestamps(af.transcript)[0] if af.transcript.is_file() else None
        except OSError:
            first_ts = None
        return (first_ts if first_ts is not None else 1 << 62, af.meta.agent_id)

    files = sorted(files, key=_started)  # rows run in start order, like a workflow run's progress list
    first = next((f for f in files if f.meta.session_id == session), files[0])
    agent_dir = first.meta_path.parent
    session_dir = agent_dir.parent
    session_jsonl = session_dir.parent / f"{session_dir.name}.jsonl"
    head = collect._head(session_jsonl) if session_jsonl.is_file() else {}

    if has_manifest:
        kind = data["kind"]
        spec, root, into = data["spec"], data["root"], data.get("into") if isinstance(data.get("into"), str) else None
        start_ms = check.start_ms if check is not None else None
        agents_cfg = data.get("agents") if isinstance(data.get("agents"), dict) else None
        das = data.get("doneAtStart")
        done = frozenset(str(x) for x in das) if isinstance(das, list) else None
    else:
        kind = "create" if all(_is_create_step(f.meta.description) for f in files) else "implement"
        spec, root, into = None, head.get("cwd"), head.get("gitBranch")
        start_ms, agents_cfg, done = None, None, None

    ctx = AgentRunContext(
        run_id=run_id, kind=kind, workflow_name="spec-implement" if kind == "implement" else "spec-create",
        matcher=_marker_matcher, spec=spec, root=root, into=into, start_ms=start_ms, end_ms=None,
        roles=agents_cfg, done_at_start=done, manifest="present" if has_manifest else "missing",
        partial=bool(partial or not has_manifest), backfill=False, gap_minutes=None)
    missing: List[dict] = []
    if not has_manifest:
        missing.append({"agentId": None, "what": "manifest"})
    if bad:
        missing.append({"agentId": None, "what": "meta"})
    record = collect._build_record(
        run_id=run_id, workflow_name=ctx.workflow_name, run_data=None, session_dir=session_dir,
        agent_dir=agent_dir, session_jsonl=session_jsonl, agents=[_to_agent(f) for f in files], missing=missing,
        start_ms=None, wall_ms=None, status=None, pricing=pricing, fresh=fresh, partial=ctx.partial,
        agent_run=ctx)
    return collect.CollectOutcome(run_id, "collected", record=record)


def finish_collected(outcome: collect.CollectOutcome, env: Optional[Mapping[str, str]] = None
                     ) -> Tuple[Optional[Path], str]:
    """Write the record, then close the run manifest (only after a successful write)."""
    path, kind = collect.write_outcome_result(outcome, env)
    if outcome.record is not None:
        try:
            store.close_manifest(outcome.run_id, collect._iso(_now_ms(datetime.now(timezone.utc))) or "", env)
        except (OSError, ValueError):
            pass  # no open manifest (missing or already closed): nothing to close
    return path, kind


# ---------------------------------------------------------------- git report and metrics


def _git_report(root: Optional[str], spec: Optional[str], into: Optional[str]) -> Optional[dict]:
    """`ck worktree report` in-process; None on any failure."""
    if not root or not spec or not Path(root).is_dir():
        return None
    try:
        from claude_kiro import worktrees

        report = worktrees.report(Path(root), spec, into=into)
    except Exception:  # noqa: BLE001
        return None
    return report if isinstance(report, dict) else None


def _agents_config(roles_cfg: Optional[Mapping[str, Mapping[str, Any]]]) -> Optional[dict]:
    if not isinstance(roles_cfg, Mapping):
        return None
    out = {}
    for role, cfg in roles_cfg.items():
        if isinstance(cfg, Mapping) and (cfg.get("model") or cfg.get("effort")):
            out[role] = {"model": cfg.get("model"), "effort": cfg.get("effort")}
    return out or None


def _task_rows(rows: List[dict], merged: Sequence[str], failed: Sequence[str], report: Mapping) -> List[dict]:
    merged_at = {e.get("task"): e.get("mergedAt") for e in report.get("merged", []) if isinstance(e, Mapping)}
    out = []
    for task, ok in [(t, True) for t in merged] + [(t, False) for t in failed]:
        mine = [r for r in rows if r.get("task") is not None and str(r["task"]) == task]
        attempts = [r["attempt"] for r in mine if r.get("attempt") is not None]
        rounds = [r["round"] for r in mine if r["role"] == "reviewer" and r.get("round") is not None]
        at = merged_at.get(task)
        out.append({"task": task, "ok": ok, "attempts": max(attempts) if attempts else None,
                    "reviewRounds": max(rounds) if rounds else None,
                    "resolves": sum(1 for r in mine if r["role"] == "resolver"),
                    "mergedAt": at if isinstance(at, str) and at else None})
    return out


def agent_metrics(ctx: AgentRunContext, rows: List[dict]) -> dict:
    """The kind-specific record fields. Implement runs read the git report; create/review never do."""
    config = _agents_config(ctx.roles)
    if ctx.kind != "implement":
        out: Dict[str, Any] = {"outcome": None, "outcomeInferred": False, "agentsConfig": None, "mode": None,
                               "reviewResult": None, "openQuestions": None, "review": None,
                               "consistency": None, "phases": None}
        out.update(collect._create_metrics({}, rows))
        out["outcome"], out["outcomeInferred"], out["mode"] = None, False, \
            (ctx.kind if ctx.manifest == "present" else None)
        if config is not None:
            out["agentsConfig"] = config
        return out

    out = {"outcome": None, "outcomeInferred": False, "agentsConfig": config, "reportSource": None,
           "taskSpanMinutes": None, "tasks": None, "landings": None, "fullGates": None, "finalGate": None,
           "crossTests": None, "fixerEscalations": None, "phases": None, "stats": None}
    if not ctx.spec or not ctx.root:
        return out  # no manifest: no spec, so the report is never called
    report = _git_report(ctx.root, ctx.spec, ctx.into)
    run_tasks = frozenset(str(r["task"]) for r in rows if r.get("task") is not None
                          and (ctx.spec_agent_ids is None or r.get("agentId") in ctx.spec_agent_ids))
    summary = agentrun.report_summary(report, done_at_start=ctx.done_at_start, start_ms=ctx.start_ms,
                                      end_ms=ctx.end_ms, run_tasks=run_tasks)
    if summary.merged is None:
        out["outcomeMissing"] = True
        return out
    out["outcome"] = summary.outcome
    out["reportSource"] = "git"
    out["tasks"] = _task_rows(rows, summary.merged, summary.failed or (), report or {})
    out["stats"] = {"notStarted": len(summary.not_started or ()), "blocked": len(summary.blocked or ())}
    return out


# ---------------------------------------------------------------- backfill (7.1-7.6)


@dataclass(frozen=True)
class BackfillPlan:
    plan: backfill.GroupPlan
    est_usd: Dict[str, Optional[float]]        # record id -> priced cost of its agents (None: no transcript)
    overlaps: Dict[str, str]                   # record id of a left-out group -> the backfill record it clashes with
    files: Dict[str, AgentFile]                # agent id -> files, for every candidate
    gap_minutes: int = agentrun.DEFAULT_GAP_MINUTES


def _backfill_metas(home: Path, workers: int) -> List[AgentFile]:
    """Every direct subagent meta of every session (workflow-tool agents live one level deeper and are not read)."""
    paths = sorted((Path(home) / "projects").glob("*/*/subagents/agent-*.meta.json"))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        views = list(pool.map(_read_meta, paths))
    return [AgentFile(v, p, p.with_name(f"agent-{v.agent_id}.jsonl"))
            for v, p in zip(views, paths) if v is not None]


def _probe(af: AgentFile, pricing: dict) -> Tuple[Optional[int], Optional[int], Optional[float]]:
    """(first ts, last ts, priced usd) from the transcript; all None when it is missing or unreadable."""
    if not af.transcript.is_file():
        return None, None, None
    first, last = collect._timestamps(af.transcript)
    usd: Optional[float] = None
    try:
        scan = bench.scan_transcript(af.transcript)
    except OSError:
        scan = None
    if scan and scan.get("requests"):
        usd = bench._price_by_model(scan["usage_by_model"], pricing)
    return first, last, usd


def _existing_backfill(env: Optional[Mapping[str, str]]) -> List[Tuple[str, FrozenSet[str]]]:
    """(record id, agent ids) of every backfill record already written."""
    try:
        records = store.load_records(env)
    except OSError:
        return []
    out: List[Tuple[str, FrozenSet[str]]] = []
    for rec in records:
        run = rec.get("agentRun")
        if not isinstance(run, dict) or not run.get("backfill"):
            continue
        ids = frozenset(a["agentId"] for a in rec.get("agents") or []
                        if isinstance(a, dict) and isinstance(a.get("agentId"), str))
        out.append((str(rec.get("runId")), ids))
    return out


def plan_backfill(home: Path, *, gap_minutes: int = agentrun.DEFAULT_GAP_MINUTES, pricing: Optional[dict] = None,
                  env: Optional[Mapping[str, str]] = None) -> BackfillPlan:
    """Group the unmarked spec-agent runs (A12, A13, A6) and price them. Writes nothing."""
    pricing = pricing or {}
    gap_ms = gap_minutes * 60_000
    excluded: List[Tuple[str, str, str]] = []
    candidates: List[AgentFile] = []
    for af in _backfill_metas(Path(home), 8):
        reason = backfill.candidate_reason(af.meta)
        if reason is None:
            candidates.append(af)
        else:
            excluded.append((af.meta.agent_id, af.meta.session_id, reason))

    with ThreadPoolExecutor(max_workers=8) as pool:
        probes = list(pool.map(lambda af: _probe(af, pricing), candidates))

    agents: List[backfill.BackfillAgent] = []
    files: Dict[str, AgentFile] = {}
    usd_of: Dict[str, Optional[float]] = {}
    for af, (start, end, usd) in zip(candidates, probes):
        legacy = roles.match_legacy(af.meta.description, af.meta.agent_type)
        agents.append(backfill.BackfillAgent(
            agent_id=af.meta.agent_id, session_id=af.meta.session_id, spec=legacy.spec,
            start_ms=start, end_ms=end, role=legacy.role))
        files[af.meta.agent_id] = af
        usd_of[af.meta.agent_id] = usd

    grouped = backfill.group_agents(agents, gap_ms)
    excluded.extend(grouped.excluded)

    existing = _existing_backfill(env)
    kept: List[backfill.BackfillGroup] = []
    overlaps: Dict[str, str] = {}
    for g in grouped.groups:
        members = frozenset(g.agent_ids)
        clash = next((rid for rid, ids in existing if ids & members and ids != members), None)
        if clash is None:
            kept.append(g)
        else:
            overlaps[g.record_id] = clash
            excluded.extend((aid, g.session_id, f"overlaps backfill record {clash}") for aid in g.agent_ids)

    est: Dict[str, Optional[float]] = {}
    for g in kept:
        priced = [usd_of[a] for a in g.agent_ids if usd_of.get(a) is not None]
        est[g.record_id] = sum(priced) if priced else None

    plan = backfill.GroupPlan(groups=tuple(kept),
                              excluded=tuple(sorted(excluded, key=lambda e: (e[1], e[0]))))
    return BackfillPlan(plan=plan, est_usd=est, overlaps=overlaps, files=files, gap_minutes=gap_minutes)


def _legacy_matcher(label: object, agent_type: object) -> roles.LabelMatch:
    m = roles.match_legacy(label, agent_type)
    return roles.LabelMatch(role=m.role, matched=m.matched, task=m.task)


def _collect_group(g: backfill.BackfillGroup, plan: BackfillPlan, pricing: dict,
                   env: Optional[Mapping[str, str]]) -> collect.CollectOutcome:
    files = [plan.files[a] for a in g.agent_ids]
    agent_dir = files[0].meta_path.parent
    session_dir = agent_dir.parent
    session_jsonl = session_dir.parent / f"{session_dir.name}.jsonl"
    head = collect._head(session_jsonl) if session_jsonl.is_file() else {}
    gap_ms = plan.gap_minutes * 60_000
    ctx = AgentRunContext(
        run_id=g.record_id, kind="implement", workflow_name="spec-implement", matcher=_legacy_matcher,
        spec=g.spec, root=head.get("cwd"), into=head.get("gitBranch"), start_ms=g.start_ms,
        end_ms=g.end_ms + min(agentrun.MERGE_SLACK_MS, gap_ms), roles=None, done_at_start=None,
        manifest="missing", partial=True, backfill=True, gap_minutes=plan.gap_minutes,
        spec_agent_ids=frozenset(g.spec_agent_ids))
    missing: List[dict] = [{"agentId": None, "what": "manifest"}]
    record = collect._build_record(
        run_id=g.record_id, workflow_name="spec-implement", run_data=None, session_dir=session_dir,
        agent_dir=agent_dir, session_jsonl=session_jsonl, agents=[_to_agent(f) for f in files], missing=missing,
        start_ms=None, wall_ms=None, status=None, pricing=pricing, fresh=False, partial=True, agent_run=ctx)
    _, kind = collect.write_outcome_result(collect.CollectOutcome(g.record_id, "collected", record=record), env)
    return collect.CollectOutcome(g.record_id, kind, record=record)


def collect_backfill(home: Path, plan: BackfillPlan, *, now: datetime, pricing: Optional[dict] = None,
                     env: Optional[Mapping[str, str]] = None) -> Tuple[collect.CollectOutcome, ...]:
    """Write one partial record per planned group; one failing group never stops the others."""
    pricing = pricing or {}
    out: List[collect.CollectOutcome] = []
    for g in plan.plan.groups:
        try:
            out.append(_collect_group(g, plan, pricing, env))
        except Exception as e:  # noqa: BLE001 - recorded per group
            out.append(collect.CollectOutcome(g.record_id, "failed", reason=f"{type(e).__name__}: {str(e)[:200]}"))
    return tuple(out)


# ---------------------------------------------------------------- open-manifest sweep


def _sweep_one(home: Path, path: Path, now: datetime, pricing: Optional[dict],
               env: Optional[Mapping[str, str]]) -> collect.CollectOutcome:
    run_id = path.stem
    try:
        data, state = store.read_manifest(run_id, env)
        check = agentrun.check_manifest(data, run_id) if data is not None else agentrun.FormatCheck(False, None, "read")
        verdict = agentrun.classify_open_manifest(check, _now_ms(now))
        if state == "malformed" or verdict == "malformed":
            return _failed(run_id, "malformed", "run manifest not recognised")
        if verdict == "young":
            return collect.CollectOutcome(run_id, "unfinished", reason="run is not finished yet")
        outcome = collect_agent_run(home, run_id, now=now, pricing=pricing, partial=True, env=env)
        if outcome.error == "no_agents":
            data["abandoned"] = True
            store.write_private_json(store.open_manifests_dir(env) / f"{run_id}.json", data)
            store.close_manifest(run_id, collect._iso(_now_ms(now)) or "", env)
            return collect.CollectOutcome(run_id, "abandoned", reason="no agents carry the marker")
        return outcome
    except Exception as e:  # noqa: BLE001 - one manifest must never stop the others
        return collect.CollectOutcome(run_id, "failed", reason=f"{type(e).__name__}: {str(e)[:200]}")


def collect_open_manifests(home: Path, *, now: datetime, workers: int = 8, pricing: Optional[dict] = None,
                           env: Optional[Mapping[str, str]] = None) -> Tuple[collect.CollectOutcome, ...]:
    """Stale open manifests become partial records; young ones are unfinished; bad ones failed."""
    paths = store.iter_open_manifests(env)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return tuple(pool.map(lambda p: _sweep_one(Path(home), p, now, pricing, env), paths))


def session_agent_runs(home: Path, session_id: str, since_ms: int) -> Tuple[str, ...]:
    """Run ids of open manifests of this session, or session-less ones started at or after `since_ms`."""
    found: List[str] = []
    for path in store.iter_open_manifests():
        data, state = store.read_manifest(path.stem)
        if data is None or state == "malformed":
            continue
        check = agentrun.check_manifest(data, path.stem)
        if not check.ok or check.start_ms is None:
            continue
        sid = data.get("sessionId")
        if sid == session_id or (sid is None and check.start_ms >= since_ms):
            found.append(path.stem)
    return tuple(found)
