"""Shared harness for the `ck stats` tests (run analytics).

Test-owned code: a fake Claude home shaped like the real one (run files, orchestrator and agent transcripts),
fakes for the things ck does not own (`claude`, `gh`, `$EDITOR`, the network), a real git checkout with a local
bare remote, a real pty driver, and the seeded confidential data for the leak tests.

This module never imports `claude_kiro.cli`: every `ck` call is a subprocess (`ck_sub`, `stats`).

Layout written under `<home>/projects/<slug>/`:
    <sid>.jsonl                                       orchestrator transcript (and the `Workflow` launch)
    <sid>/workflows/<run_id>.json                     the run file
    <sid>/subagents/workflows/<run_id>/agent-<id>.jsonl      one transcript per agent
    <sid>/subagents/workflows/<run_id>/agent-<id>.meta.json  `description` (= label), `workflowPhase`, `model`
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

CK = [sys.executable, "-m", "claude_kiro.cli"]
TESTS_DIR = Path(__file__).parent

_UNSET: Any = object()

# --------------------------------------------------------------------------- time helpers


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ms(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def to_ms(value: str) -> int:
    return int(parse_iso(value).timestamp() * 1000)


def _hex(*parts: object, n: int = 16) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:n]


def _write_jsonl(path: Path, lines: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


# --------------------------------------------------------------------------- fake Claude home


@dataclass
class AgentSpec:
    """One workflow agent. `requests` = [(request_id, input, output, cache_read, cache_write)].

    `split_blocks` writes each request as two transcript lines with the same `requestId` (Claude Code writes one
    line per content block). Every line carries an ISO `timestamp`; the first is at `started_at_ms`, the last at
    `started_at_ms + duration_ms`.
    """

    label: str
    model: str = "claude-sonnet-5-5"
    phase: str = "Wave 1"
    requests: Optional[List[Tuple[str, int, int, int, int]]] = None
    split_blocks: bool = True
    duration_ms: int = 60_000
    started_at_ms: Optional[int] = None
    attempt: int = 1
    meta: bool = True
    prompt_preview: str = "Implement the task."
    result_preview: str = "Done."
    last_tool: str = "Bash: pytest -q"
    state: str = "done"
    agent_id: Optional[str] = None


@dataclass
class TaskSpec:
    """One entry of `result.tasks` of a spec-implement run (per-task rec, spec-implement.js:647-724, 830-852)."""

    task: str = "1"
    title: Optional[str] = None
    wave: int = 1
    attempts: int = 1
    ok: bool = True
    escalations: List[dict] = field(default_factory=list)
    risk_tag: str = "normal"
    start_model: str = "sonnet"
    review_model: Optional[str] = "opus"
    verdict: str = "approve"
    rounds: int = 0
    findings: List[dict] = field(default_factory=list)
    summary: str = "Implemented."
    reason: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    merged_at: Optional[str] = None
    idle_minutes: float = 1.0
    fixes: Optional[Any] = None
    landing: Optional[Any] = None
    resolves: int = 0
    wrong_tests: Optional[str] = None
    outside_files: Optional[str] = None
    deviations: Optional[str] = None
    unreviewed: bool = False


def _minutes(a: Optional[str], b: Optional[str]) -> Optional[float]:
    if not a or not b:
        return None
    return round((parse_iso(b) - parse_iso(a)).total_seconds() / 60, 1)


def implement_result(
    tasks: Optional[List[TaskSpec]] = None,
    landings: Optional[List[dict]] = None,
    full_gates: Optional[List[dict]] = None,
    final_gate: Any = _UNSET,
    cross_tests: Optional[List[dict]] = None,
    fixer_escalations: Optional[List[dict]] = None,
    phases: Optional[List[dict]] = None,
    outcome: Optional[str] = "complete",
    resolves: bool = True,
    into: str = "integrate/demo",
    base: str = "2026-10-12T09:05:00Z",
) -> dict:
    """A `spec-implement` result dict shaped like spec-implement.js:953-982.

    `resolves=False` omits the per-task `resolves` field (a pre-change run); `outcome=None` omits `outcome`
    (a pre-0.10 run, which carries only `halted`, `failed` and `notStarted`).
    """
    tasks = [TaskSpec(task="1"), TaskSpec(task="2", wave=2)] if tasks is None else tasks
    base_dt = parse_iso(base)
    report = []
    for i, t in enumerate(tasks):
        started = t.started_at or iso(base_dt + timedelta(minutes=10 * i))
        finished = t.finished_at or iso(parse_iso(started) + timedelta(minutes=5))
        merged = t.merged_at or (iso(parse_iso(finished) + timedelta(minutes=t.idle_minutes)) if t.ok else None)
        if t.unreviewed:
            review = {"verdict": "unreviewed", "rounds": t.rounds, "model": "inherited", "effort": "inherited", "riskTag": t.risk_tag}
        else:
            review = {
                "verdict": t.verdict, "rounds": t.rounds, "findings": t.findings, "summary": t.summary,
                "model": t.review_model or "inherited", "effort": "medium", "riskTag": t.risk_tag,
            }
        rec: Dict[str, Any] = {
            "task": t.task, "wave": t.wave, "title": t.title or f"Task {t.task} title", "ok": t.ok,
            "attempts": t.attempts, "startedAt": started, "finishedAt": finished, "mergedAt": merged,
            "agentMinutes": _minutes(started, finished), "toMergedMinutes": _minutes(started, merged),
            "reason": t.reason, "summary": t.summary, "wrongTests": t.wrong_tests, "outsideFiles": t.outside_files,
            "deviations": t.deviations, "fixedAfterMerge": bool(t.fixes), "review": review,
            "escalations": t.escalations, "riskTag": t.risk_tag, "startModel": t.start_model,
            "reviewModel": t.review_model,
        }
        if t.fixes is not None:
            rec["fixes"] = t.fixes
        if t.landing is not None:
            rec["landing"] = t.landing
        if resolves:
            rec["resolves"] = t.resolves
        report.append(rec)
    merged_ids = [t.task for t in tasks if t.ok]
    failed = [{"task": t.task, "reason": t.reason or "gate not passed"} for t in tasks if not t.ok]
    if landings is None:
        landings = [{
            "tasks": merged_ids, "size": len(merged_ids), "window": 2, "merged": merged_ids, "red": [], "conflict": [],
            "deferred": [], "other": [], "gates": 1, "bisected": False, "minutes": 1.0,
        }] if merged_ids else []
    starts = sorted(r["startedAt"] for r in report if r["startedAt"])
    ends = sorted((r["mergedAt"] or r["finishedAt"]) for r in report if r["mergedAt"] or r["finishedAt"])
    if phases is None:
        phases = [{"group": f"Wave {w}", "tasks": sum(1 for t in tasks if t.wave == w), "start": starts[0] if starts else None,
                   "end": ends[-1] if ends else None, "minutes": _minutes(starts[0] if starts else None, ends[-1] if ends else None)}
                  for w in sorted({t.wave for t in tasks})]
    result: Dict[str, Any] = {
        "halted": outcome == "halted",
        "reason": None if outcome in (None, "complete") else f"{outcome}: see tasks",
        "into": into,
        "reportSource": "git",
        "head": "0123456789abcdef0123456789abcdef01234567",
        "taskAgents": {"model": "sonnet", "effort": "medium"},
        "agents": {"implementer": "sonnet/medium", "test_writer": "sonnet/medium", "fixer": "opus/high", "reviewer": "opus/medium",
                   "reviewer_risky": "opus/high", "orchestrator": "haiku/low", "resolver": "sonnet/medium"},
        "targetGreen": outcome != "halted",
        "merged": merged_ids,
        "failed": failed,
        "notStarted": [],
        "blocked": [],
        "blockedOn": [],
        "escalated": [{"task": t.task, "models": [e.get("model") for e in t.escalations], "ok": t.ok} for t in tasks if t.escalations],
        "fixerEscalations": fixer_escalations or [],
        "timing": {"start": starts[0] if starts else None, "end": ends[-1] if ends else None,
                   "minutes": _minutes(starts[0] if starts else None, ends[-1] if ends else None), "phases": phases},
        "tasks": report,
        "landings": landings,
        "fullGates": full_gates or [],
        "finalGate": {"green": True} if final_gate is _UNSET else final_gate,
        "crossTests": cross_tests or [],
        "stats": {"initiallyDone": 0, "merged": len(merged_ids), "failed": len(failed), "notStarted": 0, "planSteps": 1,
                  "planFailures": 0, "fixes": 0, "landings": len(landings), "bisected": sum(1 for l in landings if l.get("bisected"))},
    }
    if outcome is not None:
        result["outcome"] = outcome
    return result


def create_result(
    *,
    fixed: int = 3,
    not_fixed: int = 1,
    escalated: int = 1,
    refuted: int = 2,
    lenses_lost: int = 1,
    consistency_passes: int = 2,
    consistency_fixed: int = 1,
    feature: str = "demo",
    mode: str = "create",
    result: str = "Ready",
) -> dict:
    """A `spec-create` result dict shaped like spec-create.js:797-817."""
    spec_dir = f"specs/{feature}"
    files = [f"{spec_dir}/{n}.md" for n in ("requirements", "design", "test-plan", "tasks", "review")]
    return {
        "mode": mode,
        "feature": feature,
        "specDir": spec_dir,
        "files": files if mode == "create" else files[-1:],
        "authored": {k: {"summary": f"wrote {k}", "assumptions": [], "concerns": []} for k in ("requirements", "design", "testPlan", "tasks")}
        if mode == "create" else None,
        "review": {
            "result": result,
            "fixed": [{"id": f"F{i}", "what": "fixed"} for i in range(fixed)],
            "notFixed": [{"id": f"N{i}", "reason": "could not"} for i in range(not_fixed)],
            "escalated": [{"id": f"E{i}", "question": "which?", "options": []} for i in range(escalated)],
            "openQuestions": [],
            "refutedCount": refuted,
            "unverified": [],
            "lensesLost": [f"lens{i}" for i in range(lenses_lost)],
        },
        "consistency": {"passes": consistency_passes, "fixed": [{"id": f"C{i}"} for i in range(consistency_fixed)], "decisions": []},
        "schedule": None,
    }


class FakeRun:
    """A written run: the run file, the agent transcripts and (optionally) the session launch line."""

    def __init__(self, session: "FakeSession", run_id: str, agent_ids: List[str]):
        self.session = session
        self.run_id = run_id
        self.agent_ids = agent_ids

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def path(self) -> Path:
        return self.session.dir / "workflows" / f"{self.run_id}.json"

    @property
    def agents_dir(self) -> Path:
        return self.session.dir / "subagents" / "workflows" / self.run_id

    def _aid(self, agent: object) -> str:
        return self.agent_ids[agent] if isinstance(agent, int) else str(agent)

    def transcript(self, agent: object) -> Path:
        return self.agents_dir / f"agent-{self._aid(agent)}.jsonl"

    def meta_path(self, agent: object) -> Path:
        return self.agents_dir / f"agent-{self._aid(agent)}.meta.json"

    def drop_transcript(self, agent: object) -> None:
        self.transcript(agent).unlink()

    def truncate_transcript(self, agent: object) -> None:
        """Cut the last line mid-JSON (the file ends without a newline)."""
        p = self.transcript(agent)
        lines = p.read_text().splitlines()
        lines[-1] = lines[-1][: max(1, len(lines[-1]) // 2)]
        p.write_text("\n".join(lines))

    def drop_meta(self, agent: object) -> None:
        self.meta_path(agent).unlink()

    def drop_run_file(self) -> None:
        """Delete only the run file; `subagents/workflows/<id>/` and the session transcript stay."""
        self.path.unlink()

    def set(self, **keys: Any) -> "FakeRun":
        d = json.loads(self.path.read_text())
        d.update(keys)
        self.path.write_text(json.dumps(d))
        return self

    def corrupt_format(self) -> "FakeRun":
        """An allowlisted run whose `workflowProgress` is an object instead of a list."""
        return self.set(workflowProgress={"not": "a list"})

    def add_budget(self, used: float, total: float = 40) -> "FakeRun":
        """Append a `budget_usd` attachment line (Claude Code's own running total) to the session transcript."""
        self.session.append([{"type": "attachment", "attachment": {"type": "budget_usd", "used": used, "total": total, "remaining": total - used}}])
        return self


class FakeSession:
    """One Claude Code session: the orchestrator transcript plus the workflow runs it launched."""

    def __init__(self, home: "FakeClaudeHome", slug: str, session_id: str, cwd: Path, git_branch: str, cc_version: str):
        self.home = home
        self.slug = slug
        self.session_id = session_id
        self.cwd = Path(cwd)
        self.git_branch = git_branch
        self.cc_version = cc_version
        self._clock = to_ms("2026-10-12T09:00:00Z")
        self.runs: List[FakeRun] = []
        self.append([self._base({"type": "user", "timestamp": iso_ms(self._clock), "message": {"role": "user", "content": "start"}})])

    @property
    def dir(self) -> Path:
        return self.home.projects / self.slug / self.session_id

    @property
    def transcript_path(self) -> Path:
        return self.home.projects / self.slug / f"{self.session_id}.jsonl"

    def _base(self, line: dict) -> dict:
        return {"cwd": str(self.cwd), "gitBranch": self.git_branch, "sessionId": self.session_id, "version": self.cc_version, **line}

    def append(self, lines: Sequence[dict]) -> None:
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        with self.transcript_path.open("a") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")

    def _launch_lines(self, run_id: str, name: str, launch: Any, start: str) -> List[dict]:
        if launch is True:
            payload = {"name": f"spec:{name}", "scriptPath": f"{self.cwd}/.claude/workflows/{name}.js"}
        else:
            payload = dict(launch)
        tool_id = "toolu_" + _hex(self.session_id, run_id, "launch")
        t0 = to_ms(start) - 5_000
        return [
            self._base({"type": "assistant", "timestamp": iso_ms(t0), "requestId": "req_" + _hex(run_id, "launch"),
                        "message": {"id": "msg_" + _hex(run_id, "launch"), "role": "assistant", "model": "claude-sonnet-5-5",
                                    "content": [{"type": "tool_use", "id": tool_id, "name": "Workflow", "input": payload}]}}),
            self._base({"type": "user", "timestamp": iso_ms(t0 + 1_000),
                        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                                                  "content": f"Workflow started in the background. Run id: {run_id}"}]}}),
        ]

    def _write_run(self, run_id: Optional[str], name: str, *, status: str, start: str, duration_ms: int, args: Optional[dict],
                   result: Any, agents: Optional[List[AgentSpec]], extra_keys: Optional[dict], launch: Any) -> FakeRun:
        n = len(self.home.run_counter)
        self.home.run_counter.append(1)
        run_id = run_id or "wf_" + _hex(self.session_id, n, n=12)
        agents = agents if agents is not None else []
        run_start_ms = to_ms(start)
        progress: List[dict] = []
        agent_ids: List[str] = []
        seen_phases: List[str] = []
        for idx, spec in enumerate(agents):
            if spec.phase not in seen_phases:
                seen_phases.append(spec.phase)
                progress.append({"type": "workflow_phase", "index": len(seen_phases), "title": spec.phase})
        for idx, spec in enumerate(agents):
            aid = spec.agent_id or "a" + _hex(run_id, idx)
            agent_ids.append(aid)
            started = spec.started_at_ms if spec.started_at_ms is not None else run_start_ms + idx * 1_000
            requests = spec.requests if spec.requests is not None else [(f"req_{aid}_1", 1_000, 500, 2_000, 100)]
            self._write_agent(run_id, aid, spec, started, requests)
            progress.append({
                "type": "workflow_agent", "index": idx + 1, "agentId": aid, "label": spec.label, "phaseTitle": spec.phase,
                "model": spec.model, "startedAt": started, "durationMs": spec.duration_ms, "attempt": spec.attempt,
                "tokens": sum(r[1] + r[2] for r in requests), "toolCalls": 3, "state": spec.state,
                "promptPreview": spec.prompt_preview, "resultPreview": spec.result_preview, "lastToolSummary": spec.last_tool,
            })
        run_file: Dict[str, Any] = {
            "runId": run_id, "workflowName": name, "status": status, "startTime": start, "durationMs": duration_ms,
            "args": args if args is not None else {"spec": "demo", "root": str(self.cwd)},
            "workflowProgress": progress,
        }
        if result is not None:
            run_file["result"] = result
        run_file.update(extra_keys or {})
        p = self.dir / "workflows" / f"{run_id}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(run_file))
        if launch is not False:
            self.append(self._launch_lines(run_id, name, launch, start))
        run = FakeRun(self, run_id, agent_ids)
        self.runs.append(run)
        return run

    def _write_agent(self, run_id: str, aid: str, spec: AgentSpec, started_ms: int, requests: Sequence[Tuple[str, int, int, int, int]]) -> None:
        d = self.dir / "subagents" / "workflows" / run_id
        base = self._base
        lines: List[dict] = [base({"type": "user", "timestamp": iso_ms(started_ms), "message": {"role": "user", "content": spec.prompt_preview}})]
        per_request = 2 if spec.split_blocks else 1
        total = max(1, len(requests) * per_request)
        k = 0
        for rid, inp, out, cread, cwrite in requests:
            usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cread, "cache_creation_input_tokens": cwrite}
            blocks: List[List[dict]] = [[{"type": "text", "text": "working"}], [{"type": "tool_use", "id": f"toolu_{_hex(rid, 'b')}", "name": "Bash", "input": {}}]]
            for content in blocks[:per_request]:
                k += 1
                ts = started_ms + round(spec.duration_ms * k / total)
                lines.append(base({"type": "assistant", "requestId": rid, "timestamp": iso_ms(ts),
                                   "message": {"id": f"msg_{_hex(rid)}", "role": "assistant", "model": spec.model, "usage": usage, "content": content}}))
        _write_jsonl(d / f"agent-{aid}.jsonl", lines)
        if spec.meta:
            (d / f"agent-{aid}.meta.json").write_text(json.dumps({"description": spec.label, "workflowPhase": spec.phase, "model": spec.model}))

    def implement_run(self, run_id: Optional[str] = None, *, name: str = "spec-implement", status: str = "completed",
                      start: str = "2026-10-12T09:00:00Z", duration_ms: int = 720_000, args: Optional[dict] = None,
                      result: Any = _UNSET, agents: Optional[List[AgentSpec]] = None, extra_keys: Optional[dict] = None,
                      launch: Any = True) -> FakeRun:
        """A spec-implement (or `name=`) run; `result=None` omits the result; `launch=False` writes no launch."""
        return self._write_run(run_id, name, status=status, start=start, duration_ms=duration_ms, args=args,
                               result=implement_result() if result is _UNSET else result,
                               agents=agents if agents is not None else _default_agents(), extra_keys=extra_keys, launch=launch)

    def create_run(self, run_id: Optional[str] = None, *, name: str = "spec-create", status: str = "completed",
                   start: str = "2026-10-12T08:00:00Z", duration_ms: int = 600_000, args: Optional[dict] = None,
                   result: Any = _UNSET, agents: Optional[List[AgentSpec]] = None, extra_keys: Optional[dict] = None,
                   launch: Any = True) -> FakeRun:
        return self._write_run(run_id, name, status=status, start=start, duration_ms=duration_ms,
                               args=args if args is not None else {"feature": "demo", "root": str(self.cwd)},
                               result=create_result() if result is _UNSET else result,
                               agents=agents if agents is not None else _default_create_agents(), extra_keys=extra_keys, launch=launch)

    def foreign_run(self, run_id: Optional[str] = None, *, name: str = "competitor-teardown-q4", status: str = "completed",
                    start: str = "2026-10-12T07:00:00Z", duration_ms: int = 300_000, args: Optional[dict] = None,
                    result: Any = _UNSET, agents: Optional[List[AgentSpec]] = None, extra_keys: Optional[dict] = None,
                    launch: Any = _UNSET) -> FakeRun:
        """A non-ck workflow with its own transcripts; its launch names it (`launch={...}` replaces the input)."""
        return self._write_run(run_id, name, status=status, start=start, duration_ms=duration_ms, args=args or {"topic": "x"},
                               result={"summary": "done"} if result is _UNSET else result,
                               agents=agents if agents is not None else [AgentSpec("scan market")],
                               extra_keys=extra_keys, launch={"name": name} if launch is _UNSET else launch)


def _default_agents() -> List[AgentSpec]:
    return [
        AgentSpec("task 1", phase="Wave 1"),
        AgentSpec("review 1", phase="Wave 1", model="claude-opus-5-5"),
        AgentSpec("land 1", phase="Wave 1", model="claude-haiku-5-5"),
    ]


def _default_create_agents() -> List[AgentSpec]:
    return [
        AgentSpec("write requirements.md", phase="Requirements"),
        AgentSpec("write design.md", phase="Design + Test Plan"),
        AgentSpec("review: fidelity", phase="Adversarial Review", model="claude-opus-5-5"),
    ]


class FakeClaudeHome:
    """A `$CLAUDE_CONFIG_DIR` tree in a temp directory."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.projects = self.root / "projects"
        self.projects.mkdir(parents=True, exist_ok=True)
        self.run_counter: List[int] = []
        self.sessions: List[FakeSession] = []

    def session(self, slug: str = "-work-acme", session_id: Optional[str] = None, *, cwd: Optional[Path] = None,
                git_branch: str = "integrate/x", cc_version: str = "2.1.80") -> FakeSession:
        sid = session_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.root}/{len(self.sessions)}"))
        s = FakeSession(self, slug, sid, Path(cwd) if cwd else self.root / "project", git_branch, cc_version)
        self.sessions.append(s)
        return s


# --------------------------------------------------------------------------- git, gh, claude, editor


def _git_env() -> Dict[str, str]:
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": "Test User", "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test User", "GIT_COMMITTER_EMAIL": "test@example.com"})
    return env


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=_git_env()).stdout.strip()


def git_project(tmp: Path, remote: Optional[str] = "git@github.com:acme-corp/acme-ledger.git", *, name: str = "project") -> Path:
    """A real repo with one commit on `main` and (unless remote is None) an `origin`; session cwd and args.root point at it."""
    root = Path(tmp) / name
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("project\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    if remote:
        git(root, "remote", "add", "origin", remote)
    return root


TARGET_REPO = "kutlayhanli/claude-kiro"
TARGET_URL = "https://github.com/kutlayhanli/claude-kiro.git"


def _is_target(url: str) -> bool:
    return re.search(r"github\.com[/:]kutlayhanli/claude-kiro(\.git)?$", url) is not None


def kiro_checkout(tmp: Path, remote: str = TARGET_URL, *, extra_remotes: Optional[Dict[str, str]] = None,
                  unpushed_commit: bool = False) -> Tuple[Path, Path]:
    """A real clone of a local bare repo (one commit on `master`) -> (checkout, bare).

    `origin` is `remote` (and every extra remote its URL). `url.<bare>.insteadOf=<url>` makes fetch and push of the
    target URL reach the bare repo offline; any other URL (a fork, `angelsen/claude-kiro`) gets its own copy of the
    bare repo at `<tmp>/other-bare-<remote name>.git`, so a push to it is observable and never reaches `bare`.
    `unpushed_commit=True` leaves HEAD on branch `local-work` holding one commit that is not in the bare repo.
    """
    tmp = Path(tmp)
    bare = tmp / "kiro-bare.git"
    seed = tmp / "kiro-seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "master")
    (seed / "README.md").write_text("claude-kiro\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "seed")
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True, capture_output=True, env=_git_env())
    checkout = tmp / "kiro-checkout"
    subprocess.run(["git", "clone", "-q", str(bare), str(checkout)], check=True, capture_output=True, env=_git_env())
    remotes = {"origin": remote, **(extra_remotes or {})}
    for rname, url in remotes.items():
        if _is_target(url):
            store = bare
        else:
            store = tmp / f"other-bare-{rname}.git"
            if not store.exists():
                subprocess.run(["git", "clone", "-q", "--bare", str(bare), str(store)], check=True, capture_output=True, env=_git_env())
        if rname in git(checkout, "remote").split():
            git(checkout, "remote", "set-url", rname, url)
        else:
            git(checkout, "remote", "add", rname, url)
        git(checkout, "config", "--add", f"url.{store}.insteadOf", url)
        git(checkout, "fetch", "-q", rname)
    if unpushed_commit:
        git(checkout, "checkout", "-q", "-b", "local-work")
        (checkout / "local.txt").write_text("unpushed\n")
        git(checkout, "add", "-A")
        git(checkout, "commit", "-qm", "local work")
    return checkout, bare


def _script(path: Path, body: str, shebang: Optional[str] = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{shebang or sys.executable}\n{body}")
    path.chmod(0o755)
    return path


FAKE_GH = r'''
import json, os, sys
argv = sys.argv[1:]
body = None
for i, a in enumerate(argv):
    if a == "--body" and i + 1 < len(argv):
        body = argv[i + 1]
    elif a == "--body-file" and i + 1 < len(argv):
        try:
            body = sys.stdin.read() if argv[i + 1] == "-" else open(argv[i + 1]).read()
        except OSError:
            body = None
with open(LOG, "a") as fh:
    fh.write(json.dumps({"argv": argv, "body": body}) + "\n")
if argv[:2] == ["auth", "status"]:
    sys.exit(int(os.environ.get("FAKE_GH_AUTH_RC", "0")))
if argv[:2] == ["pr", "create"]:
    rc = int(os.environ.get("FAKE_GH_FAIL_RC", "0"))
    if rc:
        sys.stderr.write("gh: pull request create failed\n")
        sys.exit(rc)
    print("https://github.com/kutlayhanli/claude-kiro/pull/101")
'''


def fake_gh(bin_dir: Path, log: Path) -> Path:
    """`gh` on PATH: appends {"argv": [...], "body": ...} JSON lines to `log`.

    `gh auth status` exits $FAKE_GH_AUTH_RC (default 0); `gh pr create` prints a PR URL or exits $FAKE_GH_FAIL_RC.
    """
    return _script(Path(bin_dir) / "gh", f"LOG = {str(log)!r}\n{FAKE_GH}")


def gh_calls(log: Path) -> List[dict]:
    log = Path(log)
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def path_without_gh(tmp: Path) -> str:
    """A PATH holding only symlinks to git, sh and python (the real gh must not be found)."""
    d = Path(tmp) / "bin-no-gh"
    d.mkdir(parents=True, exist_ok=True)
    for name in ("git", "sh"):
        real = shutil.which(name)
        if real and not (d / name).exists():
            (d / name).symlink_to(real)
    for name in ("python", "python3"):
        if not (d / name).exists():
            (d / name).symlink_to(sys.executable)
    return str(d)


FAKE_DRAFTER = r'''
import json, sys
argv = sys.argv[1:]
if "-p" in argv or "--print" in argv:
    data = "" if sys.stdin.isatty() else sys.stdin.read()
    with open(LOG, "a") as fh:
        fh.write(json.dumps({"argv": argv, "stdin": data}) + "\n")
    sys.stdout.write(NOTE)
'''


class Drafter:
    def __init__(self, path: Path, log: Path):
        self.path = path
        self.log = log

    def calls(self) -> List[dict]:
        """Every recorded `claude -p` invocation: {"argv": [...], "stdin": "..."}."""
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []


def fake_claude_drafter(bin_dir: Path, note_text: str) -> Drafter:
    """`claude` that, called with `-p`, prints `note_text` and records its argv and stdin."""
    log = Path(bin_dir) / "drafter-calls.jsonl"
    path = _script(Path(bin_dir) / "claude", f"LOG = {str(log)!r}\nNOTE = {note_text!r}\n{FAKE_DRAFTER}")
    return Drafter(path, log)


def fake_editor(bin_dir: Path, replacement: str) -> Path:
    """`$EDITOR` script that overwrites the file it is given with `replacement`."""
    return _script(Path(bin_dir) / "fake-editor", f"import sys\nopen(sys.argv[1], 'w').write({replacement!r})\n")


FAKE_CLAUDE_SESSION = r'''
import json, os, sys, pathlib
sys.path.insert(0, TESTS)
from stats_helpers import FakeClaudeHome, AgentSpec, implement_result, create_result, TaskSpec

args = sys.argv[1:]
sid = args[args.index("--session-id") + 1] if "--session-id" in args else None
home = FakeClaudeHome(pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]))
session = home.session(slug="-fake-ck-run", session_id=sid, cwd=pathlib.Path.cwd())
for r in RUNS:
    kw = dict(r.get("kwargs", {}))
    kw["agents"] = [AgentSpec(**a) for a in kw["agents"]] if kw.get("agents") is not None else None
    getattr(session, {"implement": "implement_run", "create": "create_run", "foreign": "foreign_run"}[r["kind"]])(**kw)
print("fake claude done")
sys.exit(EXIT)
'''


def fake_claude_session(bin_dir: Path, runs: Sequence[dict], *, exit_code: int = 0) -> Path:
    """A fake `claude` for `ck run`: writes one session (cwd = the project) holding `runs`, then exits `exit_code`.

    `runs` = [{"kind": "implement" | "create" | "foreign", "kwargs": {...}}]: `kwargs` are the keyword arguments of
    `FakeSession.implement_run` / `create_run` / `foreign_run` (JSON-able; `agents` as a list of AgentSpec field dicts).
    The session id is the `--session-id` ck passes. Expects `$CLAUDE_CONFIG_DIR` in the environment.
    """
    body = f"TESTS = {str(TESTS_DIR)!r}\nRUNS = {json.dumps(list(runs))}\nEXIT = {int(exit_code)}\n{FAKE_CLAUDE_SESSION}"
    return _script(Path(bin_dir) / "claude", body)


def agent_dict(spec: AgentSpec) -> dict:
    """An AgentSpec as the JSON-able dict `fake_claude_session` expects."""
    return dataclasses.asdict(spec)


# --------------------------------------------------------------------------- environments and ck subprocesses


class Env(dict):
    """The env overrides of one `fresh_env()` store, with its directories as attributes."""

    root: Path
    claude: Path
    xdg: Path
    config: Path
    home: Path


@contextlib.contextmanager
def fresh_env() -> Iterator[Env]:
    """A per-call temp store: its own CLAUDE_CONFIG_DIR, XDG_DATA_HOME, CK_CONFIG_HOME, GIT_CONFIG_GLOBAL and HOME.

    Use inside a Hypothesis `@given` body instead of `tmp_path` (all examples share one test call).
    """
    with tempfile.TemporaryDirectory(prefix="ck-fresh-") as td:
        root = Path(td)
        env = Env()
        env.root, env.claude, env.xdg, env.config, env.home = (root / "claude-home", root / "xdg-data", root / "ck-config", root / "home", root / "home")
        env.claude.mkdir()
        env.config.mkdir()
        env.home.mkdir(exist_ok=True)
        gitconfig = root / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test User\n\temail = test@example.com\n")
        env.update({
            "CLAUDE_CONFIG_DIR": str(env.claude), "XDG_DATA_HOME": str(env.xdg), "CK_CONFIG_HOME": str(env.config),
            "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(env.home),
        })
        yield env


@dataclass
class Result:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


def ck_sub(*args: str, env: Optional[Dict[str, str]] = None, cwd: Optional[Path] = None, stdin: Any = subprocess.DEVNULL,
           input: Optional[str] = None, timeout: float = 300) -> Result:
    """Run `python -m claude_kiro.cli <args>` as a subprocess; `env` overrides are merged over os.environ."""
    p = subprocess.run([*CK, *args], cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True, text=True, timeout=timeout,
                       **({"input": input} if input is not None else {"stdin": stdin}))
    return Result(p.returncode, p.stdout, p.stderr)


def stats(*args: str, env: Optional[Dict[str, str]] = None, input: Optional[str] = None, cwd: Optional[Path] = None) -> Result:
    """`ck stats <args>` as a subprocess."""
    return ck_sub("stats", *args, env=env, cwd=cwd, input=input)


def run_tty(argv: Sequence[str], *, env: Optional[Dict[str, str]] = None, cwd: Optional[Path] = None,
            script: Sequence[Tuple[str, str]] = (), timeout: float = 30) -> Tuple[int, str]:
    """Run `argv` under a real pty (`pty.fork()`); wait for each regex in `script` in order and send its reply.

    Returns (exit_code, transcript) with CRLF normalised to LF. Build argv as `[*CK, "stats", ...]`.
    """
    full_env = {**os.environ, **(env or {})}
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            if cwd:
                os.chdir(cwd)
            os.execvpe(argv[0], list(argv), full_env)
        finally:
            os._exit(127)
    buf = ""
    pending = list(script)
    pos = 0
    deadline = time.monotonic() + timeout
    eof = False
    try:
        while not eof:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"run_tty timed out waiting for {pending[0][0] if pending else 'exit'!r}; transcript so far:\n{buf}")
            ready, _, _ = select.select([fd], [], [], min(left, 0.5))
            if ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    data = b""
                if not data:
                    eof = True
                else:
                    buf += data.decode(errors="replace").replace("\r\n", "\n")
            while pending:
                m = re.search(pending[0][0], buf[pos:])
                if not m:
                    break
                pos += m.end()
                os.write(fd, pending.pop(0)[1].encode())
        if pending:
            raise AssertionError(f"run_tty: the process ended before {pending[0][0]!r} appeared; transcript:\n{buf}")
        _, status = os.waitpid(pid, 0)
        pid = 0
        return os.waitstatus_to_exitcode(status), buf
    finally:
        if pid:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
        with contextlib.suppress(OSError):
            os.close(fd)


# --------------------------------------------------------------------------- store readers


def store_files(xdg: Path) -> List[Path]:
    """Every file under `<xdg>/claude-kiro` (records and bookkeeping), sorted."""
    root = Path(xdg) / "claude-kiro"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def load_record(xdg: Path, run_id: str) -> dict:
    return json.loads((Path(xdg) / "claude-kiro" / "runs" / f"{run_id}.json").read_text())


def all_bytes(*roots: Path) -> bytes:
    """The bytes of every file under the roots (a root may be a file), for leak scans."""
    out = b""
    for root in roots:
        root = Path(root)
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []
        for f in files:
            out += f.read_bytes()
    return out


def flatten_paths(obj: Any, prefix: str = "") -> set:
    """Dotted JSON paths of the leaves of `obj`; list items are `[]` (`a[].b`); empty containers count as leaves."""
    if isinstance(obj, dict) and obj:
        out: set = set()
        for k, v in obj.items():
            out |= flatten_paths(v, f"{prefix}.{k}" if prefix else str(k))
        return out
    if isinstance(obj, list) and obj:
        out = set()
        for v in obj:
            out |= flatten_paths(v, f"{prefix}[]")
        return out
    return {prefix}


# --------------------------------------------------------------------------- seeded confidential data

ACME_REMOTE = "git@github.com:acme-corp/acme-ledger.git"
ORION_REMOTE = "https://gitlab.example.com/orion/billing"
SEEDED_SESSION_IDS = ("5e55a10e-0000-4000-8000-0000000000a1", "5e55a10e-0000-4000-8000-0000000000b2")
SEEDED_RUN_IDS = ("wf_zephyr-impl-1", "wf_zephyr-create-1", "wf_orion-impl-1", "wf_foreign-teardown-1", "wf_foreign-tax-1")
SEEDED_AGENT_IDS = ("a5eedc0ffee0001a", "a5eedc0ffee0002b")
SEEDED_TIMESTAMP = "2026-10-12T09:13:27Z"

CONFIDENTIAL: Tuple[str, ...] = (
    "zephyr-payroll",
    "Reconcile Globex invoices",
    "Initech SSN column",
    "/srv/acme/ledger.py",
    "/home/dana/acme-ledger/src/ledger.py",
    "src/ledger_globex.py",
    "integrate/zephyr-payroll",
    ACME_REMOTE,
    "acme-corp",
    "acme-ledger",
    "dana.scully@acme-corp.com",
    "Dana Scully",
    "competitor-teardown-q4",
    "tax-review-2026",
    SEEDED_TIMESTAMP,
    "orion-billing",
    ORION_REMOTE,
    *SEEDED_SESSION_IDS,
    *SEEDED_RUN_IDS,
    *SEEDED_AGENT_IDS,
)

_PREVIEW = "Reconcile Globex invoices in /home/dana/acme-ledger/src/ledger.py for zephyr-payroll; owner dana.scully@acme-corp.com"
_SUMMARY = "Initech SSN column handled in /srv/acme/ledger.py and src/ledger_globex.py on integrate/zephyr-payroll"


def seeded_home(tmp: Path) -> Tuple[FakeClaudeHome, List[str]]:
    """Plant every CONFIDENTIAL string in every free-text slot of a fake home -> (home, terms).

    Project A (`acme-ledger`, remote ACME_REMOTE, local git identity Dana Scully <dana.scully@acme-corp.com>) has a
    spec-implement run on `integrate/zephyr-payroll`, a spec-create run and two non-ck runs; project B
    (`orion-billing`, remote ORION_REMOTE) has a spec-implement run. `home.projects_by_name` maps the project names
    to their repo directories; the home is `home.root` (use it as CLAUDE_CONFIG_DIR).
    """
    tmp = Path(tmp)
    home = FakeClaudeHome(tmp / "claude-home")
    proj_a = git_project(tmp, ACME_REMOTE, name="acme-ledger")
    git(proj_a, "config", "user.name", "Dana Scully")
    git(proj_a, "config", "user.email", "dana.scully@acme-corp.com")
    proj_b = git_project(tmp, ORION_REMOTE, name="orion-billing")
    home.projects_by_name = {"acme-ledger": proj_a, "orion-billing": proj_b}  # type: ignore[attr-defined]

    def agents(extra_label: str) -> List[AgentSpec]:
        return [
            AgentSpec(f"task 1 {extra_label}", agent_id=SEEDED_AGENT_IDS[0], prompt_preview=_PREVIEW, result_preview=_SUMMARY, last_tool=f"Edit {_SUMMARY}"),
            AgentSpec("review 1", model="claude-opus-5-5", agent_id=SEEDED_AGENT_IDS[1], prompt_preview=_PREVIEW, result_preview=_SUMMARY, last_tool="Read /srv/acme/ledger.py"),
            AgentSpec("land 1", model="claude-haiku-5-5", prompt_preview=_PREVIEW, result_preview=_SUMMARY, last_tool="Bash: git merge integrate/zephyr-payroll"),
        ]

    tasks = [
        TaskSpec(task="1", title="Reconcile Globex invoices", started_at=SEEDED_TIMESTAMP, summary=_SUMMARY,
                 reason=f"retry: {_PREVIEW}", wrong_tests=_SUMMARY, outside_files="/home/dana/acme-ledger/src/ledger.py",
                 deviations="src/ledger_globex.py", findings=[{"severity": "blocking", "issue": _SUMMARY, "file": "src/ledger_globex.py"}],
                 verdict="approve", attempts=2),
        TaskSpec(task="2", wave=2, title="Initech SSN column", summary=_SUMMARY, reason=None),
    ]
    sa = home.session("-home-dana-acme-ledger", SEEDED_SESSION_IDS[0], cwd=proj_a, git_branch="integrate/zephyr-payroll")
    sa.implement_run(
        SEEDED_RUN_IDS[0], args={"spec": "zephyr-payroll", "root": str(proj_a)},
        result=implement_result(tasks, into="integrate/zephyr-payroll",
                                cross_tests=[{"wave": 1, "tasks": ["1"], "verdict": "ok", "findings": [], "summary": _SUMMARY}],
                                fixer_escalations=[{"what": "landing Task 1 in /srv/acme/ledger.py", "model": "opus", "green": True}],
                                final_gate={"green": True, "summary": _SUMMARY}),
        agents=agents("Reconcile Globex invoices"),
        extra_keys={"logs": [_SUMMARY, _PREVIEW]},
    )
    sa.create_run(SEEDED_RUN_IDS[1], args={"feature": "zephyr-payroll", "root": str(proj_a)},
                  result=create_result(feature="zephyr-payroll"),
                  agents=[AgentSpec("write requirements.md", phase="Requirements", prompt_preview=_PREVIEW, result_preview=_SUMMARY)])
    sa.foreign_run(SEEDED_RUN_IDS[3], name="competitor-teardown-q4", agents=[AgentSpec("scan Globex", prompt_preview=_PREVIEW)])
    sa.foreign_run(SEEDED_RUN_IDS[4], name="tax-review-2026", agents=[AgentSpec("review Initech SSN column", prompt_preview=_PREVIEW)])
    sb = home.session("-home-dana-orion-billing", SEEDED_SESSION_IDS[1], cwd=proj_b, git_branch="integrate/orion-billing")
    sb.implement_run(SEEDED_RUN_IDS[2], start="2026-10-13T10:00:00Z", args={"spec": "orion-billing", "root": str(proj_b)},
                     result=implement_result([TaskSpec(task="1", title="Bill orion-billing accounts", summary="orion-billing ledger")],
                                             into="integrate/orion-billing"),
                     agents=agents("orion-billing"))
    return home, list(CONFIDENTIAL)


def seeded_local_record(kind: str) -> dict:
    """A local record (Harness Contract shape) for `spec-implement` or `spec-create` with CONFIDENTIAL strings in every free-text slot.

    Free-text slots: agent ids and labels, phase names, task ids, model names, the verdict and risk tag, config values,
    `missing` entries, gate / landing / cross-test text, the project pseudonym and the run id. Property tests of pure
    seams start their `local_record()` generator from it; Task 26 checks that its key set matches a collected record.
    """
    if kind not in ("spec-implement", "spec-create"):
        raise ValueError(kind)
    c = list(CONFIDENTIAL)

    def pick(i: int) -> str:
        return c[i % len(c)]

    def cell(**kw: Any) -> dict:
        return {"usd": 1.25, "input": 1000, "output": 500, "cacheRead": 2000, "cacheCreation": 100, "agents": 1, "ms": 60_000, **kw}

    model = f"my-finetune-{pick(0)}"
    agents = [
        {"agentId": pick(20 + i), "label": f"task {pick(i)}", "role": "implementer", "task": pick(1 + i), "attempt": 1, "round": None,
         "model": model if i else "claude-sonnet-5-5", "phase": pick(2 + i), "ms": 60_000, "usd": 1.25, "input": 1000, "output": 500,
         "cacheRead": 2000, "cacheCreation": 100}
        for i in range(3)
    ]
    agents.append({**agents[0], "agentId": pick(30), "label": pick(3), "role": "other", "task": None, "usd": None, "ms": None})
    rec: Dict[str, Any] = {
        "schema": "ck-run-local/1",
        "runId": pick(17),
        "workflow": kind,
        "outcome": "complete",
        "outcomeInferred": False,
        "ckVersion": "0.10.0",
        "collectorVersion": "0.10.0",
        "claudeCodeVersion": "2.1.80",
        "startTime": SEEDED_TIMESTAMP,
        "wallMs": 720_000,
        "agentsConfig": {"implementer": f"sonnet/{pick(0)}", "reviewer": pick(1), **{f"setting{i}": t for i, t in enumerate(c)}},
        "projectPseudonym": "p" + _hex(pick(7)),
        "costs": [
            {"usd": 3.75, "source": "priced_from_transcripts", "pricingDate": "2026-10", "scope": "run"},
            {"usd": 7.5, "source": "claude_total", "pricingDate": None, "scope": "session"},
        ],
        "byModel": {"claude-sonnet-5-5": cell(), model: cell()},
        "byRole": {"implementer": cell(agents=3), "other": cell(usd=None)},
        "byPhase": {pick(2): cell(), pick(3): cell()},
        "byTask": {pick(1): cell(), pick(4): cell()},
        "agents": agents,
        "missing": [{"agentId": pick(21), "what": "transcript"}, {"agentId": pick(22), "what": pick(5)}],
    }
    if kind == "spec-implement":
        rec.update({
            "tasks": [
                {"task": pick(1 + i), "attempts": 2, "retries": 1, "escalations": 1, "reviewRounds": 1, "verdict": pick(3 + i),
                 "resolves": 1, "resolvesInferred": False, "fixes": 0, "agentMinutes": 5.0, "toMergedMinutes": 6.0,
                 "riskTag": pick(6 + i), "idleBeforeMergeMs": 60_000}
                for i in range(2)
            ],
            "landings": [{"tasks": [pick(1), pick(4)], "size": 2, "window": 2, "merged": [pick(1)], "red": [pick(4)], "conflict": [],
                          "deferred": [], "other": [], "gates": 1, "bisected": True, "minutes": 1.0}],
            "fullGates": [{"afterMerges": 2, "green": False, "summary": pick(8)}],
            "finalGate": {"green": True, "summary": f"{pick(8)} {_SUMMARY}"},
            "crossTests": [{"wave": 1, "tasks": [pick(1)], "verdict": pick(9), "findings": [{"issue": _SUMMARY}], "summary": _PREVIEW}],
            "fixerEscalations": [{"what": f"landing Task {pick(1)}", "model": model, "green": True}],
            "phases": [{"group": pick(2), "tasks": 2, "start": SEEDED_TIMESTAMP, "end": SEEDED_TIMESTAMP, "minutes": 12.0}],
        })
    else:
        rec.update({
            "review": {"fixed": 3, "notFixed": 1, "escalated": 1, "refuted": 2, "lensesLost": 1},
            "consistency": {"passes": 2, "fixed": 1},
            "phases": [{"group": pick(2), "ms": 100_000}, {"group": pick(3), "ms": 200_000}],
        })
    return rec
