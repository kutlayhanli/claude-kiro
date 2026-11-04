"""Shared harness of the ETA CLI tests (run-followups; test-plan Test Infrastructure item 1).

Test-owned. Real boundaries only: the real `ck` CLI as a subprocess, real git repos, and history pools that the real
`ck stats collect --all` produced over fake Claude homes. This module never imports `claude_kiro.cli`; it imports
`claude_kiro.progress` (setup events for reader tests) only inside a function, so every test file collects before
that module exists. It does not import `tests/eta_strategies.py`.

Every command and environment name is spelled once, as a constant below.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from stats_helpers import (
    AgentSpec,
    FakeClaudeHome,
    Result,
    TaskSpec,
    ck_sub,
    create_result,
    git,
    implement_result,
    iso,
    to_ms,
)

# --------------------------------------------------------------------------- command and environment names

PROGRESS_BEGIN = ("progress", "begin")
PROGRESS_EVENT = ("progress", "event")
PROGRESS_END = ("progress", "end")
STATUSLINE_CMD = ("status-line",)
STATUSLINE_BIN = "ck-status-line"
DEFAULTS_CMD = ("stats", "eta-defaults")
BACKTEST_CMD = ("stats", "eta-backtest")
SLIP_KEY = "run.eta_slip_minutes"
PROGRESS_DIR_ENV = "CK_PROGRESS_DIR"
TEST_KINDS_ENV = "CK_PROGRESS_TEST_KINDS"
NOW_ENV = "CK_NOW"
DEFAULTS_ENV = "CK_ETA_DEFAULTS"
LINE_TIMEOUT_ENV = "CK_ETA_LINE_TIMEOUT_S"
RECHECK_ENV = "CK_ETA_RECHECK_S"
NOTIFY_TYPE_ENV = "CK_NOTIFY_TYPE"

ETA_CMD = "eta"
WORKTREE_CMD = "worktree"
VERSION_FLAG = "--version"
COLLECT_ALL = ("stats", "collect", "--all")
TZ_ENV = ("TZ", "UTC")
PROGRESS_RUN_FLAG = "--progress-run"
PROGRESS_EVENT_FLAG = "--progress-event"
PROGRESS_MODEL_FLAG = "--progress-model"
ATTEMPT_FLAG = "--attempt"


def _env_with(env: Optional[Dict[str, str]], now: Optional[str] = None) -> Dict[str, str]:
    out = dict(env or {})
    out[TZ_ENV[0]] = TZ_ENV[1]
    if now is not None:
        out[NOW_ENV] = now
    return out


def _flag_args(flags: Dict[str, Any]) -> List[str]:
    """`run="k", task=3, reason=None, force=True` -> `--run k --task 3 --force` (underscores become dashes)."""
    out: List[str] = []
    for key, value in flags.items():
        if value is None or value is False:
            continue
        out.append("--" + key.replace("_", "-"))
        if value is not True:
            out.append(str(value))
    return out


# --------------------------------------------------------------------------- command wrappers


def eta(env: Dict[str, str], cwd: Path, spec: str, *args: str, now: Optional[str] = None) -> Result:
    """`ck eta <spec> <args>` as a subprocess with TZ=UTC and CK_NOW=now."""
    return ck_sub(ETA_CMD, spec, *args, env=_env_with(env, now), cwd=cwd)


def eta_json(env: Dict[str, str], cwd: Path, spec: str, *args: str, now: Optional[str] = None) -> dict:
    out = eta(env, cwd, spec, *args, "--json", now=now)
    return json.loads(out.stdout)


def begin(env: Dict[str, str], cwd: Path, spec: str, kind: str, now: Optional[str] = None) -> dict:
    """`ck progress begin <spec> --kind <kind> --json` -> the parsed `{run, file, etaLine, estimate}`."""
    out = ck_sub(*PROGRESS_BEGIN, spec, "--kind", kind, "--json", env=_env_with(env, now), cwd=cwd)
    return json.loads(out.stdout)


def event(env: Dict[str, str], cwd: Path, spec: str, kind: str, now: Optional[str] = None, **flags: Any) -> Result:
    """`ck progress event <spec> <kind> [--flag value ...]`."""
    return ck_sub(*PROGRESS_EVENT, spec, kind, *_flag_args(flags), env=_env_with(env, now), cwd=cwd)


def end(env: Dict[str, str], cwd: Path, spec: str, outcome: str, now: Optional[str] = None) -> Result:
    """`ck progress end <spec> --outcome <outcome>`."""
    return ck_sub(*PROGRESS_END, spec, "--outcome", outcome, env=_env_with(env, now), cwd=cwd)


def claim_event(env: Dict[str, str], repo: Path, spec: str, task: int | str, *, run: str, kind: str = "task-start",
                model: Optional[str] = None, attempt: Optional[int] = None, now: Optional[str] = None) -> Result:
    """`ck worktree claim <spec> <task> --progress-run ... --progress-event ...` in a `stats_helpers.spec_repo`."""
    extra: List[str] = []
    if model is not None:
        extra += [PROGRESS_MODEL_FLAG, model]
    if attempt is not None:
        extra += [ATTEMPT_FLAG, str(attempt)]
    return ck_sub(WORKTREE_CMD, "claim", spec, str(task), PROGRESS_RUN_FLAG, run, PROGRESS_EVENT_FLAG, kind, *extra,
                  env=_env_with(env, now), cwd=repo)


def land_event(env: Dict[str, str], repo: Path, spec: str, tasks: Sequence[int | str], *, run: str,
               into: str = "integrate/demo", now: Optional[str] = None) -> Result:
    """`ck worktree land <spec> <tasks> --into ... --progress-run ...` in a `stats_helpers.spec_repo`."""
    return ck_sub(WORKTREE_CMD, "land", spec, *[str(t) for t in tasks], "--into", into, PROGRESS_RUN_FLAG, run,
                  env=_env_with(env, now), cwd=repo)


def status_line(env: Dict[str, str], stdin_json: Any, cwd: Optional[Path] = None) -> Result:
    """Run the `ck-status-line` entry point (next to the interpreter) with `stdin_json` (a str, or a value to dump)."""
    text = stdin_json if isinstance(stdin_json, str) else json.dumps(stdin_json)
    exe = Path(sys.executable).parent / STATUSLINE_BIN
    p = subprocess.run([str(exe)], input=text, cwd=cwd, env={**os.environ, **_env_with(env)}, capture_output=True,
                       text=True, timeout=60)
    return Result(p.returncode, p.stdout, p.stderr)


# --------------------------------------------------------------------------- readers and setup


def append_events(env: Dict[str, str], key: str, events: Sequence[dict]) -> List[Optional[str]]:
    """Write setup events through `claude_kiro.progress.append` (for tests whose boundary is the reader)."""
    from claude_kiro import progress

    merged = {**os.environ, **(env or {})}
    return [progress.append(key, ev, merged) for ev in events]


def progress_file_of(env: Dict[str, str], spec: str, cwd: Optional[Path] = None) -> Path:
    """The run's progress file, from `ck eta <spec> --json` `progressFile`."""
    return Path(eta_json(env, cwd or Path.cwd(), spec)["progressFile"])


def notify_messages(out: Path) -> List[str]:
    """The `message` of what `guard_helpers.notify_script(..., "ok", out)` recorded (it keeps the last call)."""
    out = Path(out)
    if not out.exists():
        return []
    stdin = json.loads(out.read_text())["stdin"]
    return [json.loads(stdin)["message"]]


_TYPED_NOTIFY = """import json, os, sys
payload = json.loads(sys.stdin.read() or "{{}}")
with open({out!r}, "a") as fh:
    fh.write(json.dumps({{"message": payload.get("message"), "type": os.environ.get({env!r})}}) + "\\n")
"""


def typed_notify_script(bin_dir: Path, out: Path) -> str:
    """A notify command appending `{"message", "type"}` per call, `type` being the CK_NOTIFY_TYPE it ran with."""
    path = Path(bin_dir) / "notify-typed"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + _TYPED_NOTIFY.format(out=str(out), env=NOTIFY_TYPE_ENV))
    path.chmod(0o755)
    return str(path)


def typed_notices(out: Path) -> List[Tuple[str, Optional[str]]]:
    out = Path(out)
    if not out.exists():
        return []
    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    return [(r["message"], r["type"]) for r in rows]


def _median_s(argv: Sequence[str], runs: int = 5) -> float:
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        subprocess.run(list(argv), capture_output=True, stdin=subprocess.DEVNULL, timeout=120, env=dict(os.environ))
        times.append(time.perf_counter() - t0)
    return statistics.median(times)


def baseline_startup_s() -> float:
    """Median of 5 timings of `ck --version`."""
    return _median_s([sys.executable, "-m", "claude_kiro.cli", VERSION_FLAG])


def bare_startup_s() -> float:
    """Median of 5 timings of `python -I -c "import claude_kiro"`."""
    return _median_s([sys.executable, "-I", "-c", "import claude_kiro"])


# --------------------------------------------------------------------------- spec builder


@dataclass
class TaskDef:
    id: int | str
    track: str = "impl"
    model: Optional[str] = None
    risk: Optional[str] = None
    files: int = 1
    criteria: int = 1
    deps: Sequence[int | str] = ()
    verifies: Sequence[int | str] = ()
    verify: Optional[str] = None


def _task_block(t: TaskDef, done: bool) -> str:
    n = t.id
    lines = [f"### Task {n}: {'Write tests for' if t.track == 'test' else 'Build'} part {n}",
             f"**Status:** {'Done' if done else 'Not Started'}",
             f"**Track:** {t.track}"]
    if t.model:
        lines.append(f"**Model:** {t.model}")
    if t.risk:
        lines.append(f"**Risk:** {t.risk}")
    lines.append("**Requirements:** " + (", ".join(f"{n}.{k}" for k in range(1, t.criteria + 1)) if t.criteria else "None"))
    lines.append(f"**Description:** Part {n} of the demo ledger orbit.")
    lines.append("**Files:**")
    base = "tests/test_part" if t.track == "test" else "src/ledger/part"
    for k in range(1, t.files + 1):
        lines.append(f"- `{base}{n}_{k}.py` - part {n} file {k}")
    lines.append(f"**Verify:** `{t.verify or f'test -f part{n}.txt'}`")
    if t.verifies:
        lines.append("**Verifies:** " + ", ".join(f"Task {v}" for v in t.verifies))
    lines.append("**Dependencies:** " + (", ".join(f"Task {d}" for d in t.deps) if t.deps else "None"))
    lines.append("**Complexity:** Low")
    return "\n".join(lines) + "\n\n---\n"


def eta_spec(root: Path, tasks: Sequence[TaskDef], *, done: Sequence[int | str] = (), spec: str = "demo",
             into: Optional[str] = None) -> Path:
    """A real git repo with `specs/<spec>/tasks.md` built from `tasks` (Files bullets and Requirements ids are real)."""
    repo = Path(root)
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(".ck/worktrees/\n")
    (repo / "specs" / spec).mkdir(parents=True)
    done_ids = {str(d) for d in done}
    body = "\n".join(_task_block(t, str(t.id) in done_ids) for t in tasks)
    (repo / "specs" / spec / "tasks.md").write_text(f"# Implementation Tasks: {spec}\n\n## Task Breakdown\n\n{body}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "branch", into or f"integrate/{spec}")
    return repo


# --------------------------------------------------------------------------- history pools

POOL_IMPL = "POOL_IMPL"
POOL_IMPL_NOWAIT = "POOL_IMPL_NOWAIT"
POOL_CREATE = "POOL_CREATE"
POOL_COSTLESS = "POOL_COSTLESS"
POOL_SIZED_FEW = "POOL_SIZED_FEW"
POOLS = (POOL_IMPL, POOL_IMPL_NOWAIT, POOL_CREATE, POOL_COSTLESS, POOL_SIZED_FEW)

IMPL_RUNS = 48
CREATE_RUNS = 10
COSTLESS_RUNS = 10
SIZED_RUNS = 4
UNSIZED_RUNS = 40

_BASE_MIN = {("impl", "normal"): 3.0, ("test", "normal"): 2.0, ("impl", "safety"): 4.5, ("test", "safety"): 3.0}
_JITTER = (0.9, 1.0, 1.1, 0.95, 1.05)
_RUN0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


@dataclass
class PlanTask:
    defn: TaskDef
    wave: int
    start_model: str
    risk: str
    minutes: float
    attempts: int
    escalated: bool
    started: datetime
    finished: datetime
    merged: datetime
    review_wait: bool


def impl_plan(i: int, tag: str = "impl", waits: Optional[bool] = None) -> Tuple[str, str, List[PlanTask]]:
    """(run id, spec name, tasks) of implement run `i`: a pure function of the index, shared by the builder and the smoke test."""
    run_id = f"wf_{tag}-{i:02d}"
    spec = f"{tag}-{i:02d}"
    waiting = (i % 2 == 0) if waits is None else waits
    count = 4 + i % 5
    start = _RUN0 + timedelta(days=i)
    wave_start = start
    out: List[PlanTask] = []
    for j in range(count):
        track = "test" if j % 3 == 1 else "impl"
        model = "opus" if (i + j) % 2 else "sonnet"
        risk = "safety" if (i * 3 + j) % 4 == 3 else "normal"
        files = 1 + (i + j) % 4
        criteria = 1 + (i * 3 + 2 * j) % 5
        key = (i * 13 + j * 7) % 20
        attempts = 2 if key < 3 else 1
        escalated = key == 3
        minutes = round(_BASE_MIN[(track, risk)] * (1 + 0.5 * (files + criteria)) * _JITTER[(i + j) % 5], 2)
        wave = 1 + j // 3
        if j % 3 == 0 and j:
            wave_start = max(p.merged for p in out) + timedelta(minutes=1)
        started = wave_start + timedelta(minutes=j % 3)
        finished = started + timedelta(minutes=minutes * (attempts + (1 if escalated else 0)))
        merged = finished + timedelta(minutes=4 if waiting else 0)
        d = TaskDef(id=j + 1, track=track, model=model, risk=None if risk == "normal" else risk, files=files, criteria=criteria,
                    deps=(j,) if j % 3 and j else ())
        out.append(PlanTask(d, wave, model, risk, minutes, attempts, escalated, started, finished, merged, waiting))
    return run_id, spec, out


def _impl_agents(plan: Sequence[PlanTask], costless: bool) -> List[AgentSpec]:
    requests = [] if costless else None
    agents: List[AgentSpec] = []
    for p in plan:
        n = p.defn.id
        suffix = " [test]" if p.defn.track == "test" else ""
        phase = f"Wave {p.wave}"
        model_id = f"claude-{p.start_model}-5-5"
        total_ms = (p.finished - p.started).total_seconds() * 1000
        runs = p.attempts + (1 if p.escalated else 0)
        for k in range(runs):
            label = f"task {n}{suffix}" if k == 0 else f"task {n} retry {k}"
            model = "claude-opus-5-5" if (p.escalated and k == runs - 1) else model_id
            agents.append(AgentSpec(label, model=model, phase=phase, duration_ms=int(total_ms / runs), attempt=k + 1,
                                    started_at_ms=to_ms(iso(p.started)) + int(k * total_ms / runs), requests=requests))
        fin = to_ms(iso(p.finished))
        if p.review_wait:
            agents.append(AgentSpec(f"review {n}", model="claude-opus-5-5", phase=phase, duration_ms=120_000,
                                    started_at_ms=fin + 30_000, queued_at_ms=fin + 5_000, requests=requests))
            agents.append(AgentSpec(f"land {n}", model="claude-haiku-5-5", phase=phase, duration_ms=60_000,
                                    started_at_ms=fin + 180_000, queued_at_ms=fin + 150_000, requests=requests))
        else:
            agents.append(AgentSpec(f"review {n}", model="claude-opus-5-5", phase=phase, duration_ms=120_000,
                                    started_at_ms=fin, requests=requests))
            agents.append(AgentSpec(f"land {n}", model="claude-haiku-5-5", phase=phase, duration_ms=60_000,
                                    started_at_ms=fin, requests=requests))
    return agents


def _task_spec(p: PlanTask) -> TaskSpec:
    escalations = [{"stage": "retry", "model": "opus", "from": "sonnet"}] if p.escalated else []
    return TaskSpec(task=str(p.defn.id), title=f"Part {p.defn.id}", wave=p.wave, attempts=p.attempts + (1 if p.escalated else 0),
                    escalations=escalations, risk_tag=p.risk, start_model=p.start_model,
                    started_at=iso(p.started), finished_at=iso(p.finished), merged_at=iso(p.merged),
                    landing={"waited": True} if p.review_wait else None)


def _pool_env(root: Path) -> Dict[str, str]:
    cfg = root / "gitconfig"
    cfg.write_text("[user]\n\tname = Test User\n\temail = test@example.com\n")
    env = {
        "CLAUDE_CONFIG_DIR": str(root / "claude-home"), "XDG_DATA_HOME": str(root / "xdg-data"),
        "CK_CONFIG_HOME": str(root / "ck-config"), "GIT_CONFIG_GLOBAL": str(cfg), "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(root / "home"), TZ_ENV[0]: TZ_ENV[1],
    }
    for key in ("CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "CK_CONFIG_HOME", "HOME"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    return env


def _collect(root: Path, env: Dict[str, str]) -> None:
    out = ck_sub(*COLLECT_ALL, env=env, cwd=root)
    assert out.exit_code == 0, out.output


def _build_impl(root: Path, runs: int, tag: str, *, costless: bool = False, delete_specs_from: Optional[int] = None) -> None:
    env = _pool_env(root)
    home = FakeClaudeHome(Path(env["CLAUDE_CONFIG_DIR"]))
    for i in range(runs):
        run_id, spec, plan = impl_plan(i, tag)
        proj = eta_spec(root / "projects" / f"{tag}-{i:02d}", [p.defn for p in plan], spec=spec, into=f"integrate/{spec}")
        session = home.session(f"-work-{tag}-{i:02d}", cwd=proj, git_branch=f"integrate/{spec}")
        session.implement_run(
            run_id, start=iso(plan[0].started - timedelta(minutes=1)),
            duration_ms=int((max(p.merged for p in plan) - plan[0].started).total_seconds() * 1000) + 120_000,
            args={"spec": spec, "root": str(proj)},
            result=implement_result([_task_spec(p) for p in plan], into=f"integrate/{spec}", base=iso(plan[0].started)),
            agents=_impl_agents(plan, costless))
        if delete_specs_from is not None and i >= delete_specs_from:
            shutil.rmtree(proj / "specs")
    _collect(root, env)


# The create phases and the share of the run each agent takes (sums to 1.0)
_CREATE_STEPS = (
    ("write requirements.md", "Requirements", 0.10), ("write design.md", "Design + Test Plan", 0.10),
    ("write test-plan.md", "Design + Test Plan", 0.10), ("write implementation track", "Task Tracks", 0.10),
    ("write test track", "Task Tracks", 0.10), ("link tracks into tasks.md", "Link", 0.05),
    ("review: fidelity", "Adversarial Review", 0.05), ("review: grounding", "Adversarial Review", 0.05),
    ("review: implementability", "Adversarial Review", 0.05), ("review: verification", "Adversarial Review", 0.05),
    ("revise spec + write review.md", "Revise", 0.15),
)


def create_total_minutes(i: int) -> float:
    return round(58 + i * 38 / (CREATE_RUNS - 1), 2)


def _build_create(root: Path) -> None:
    env = _pool_env(root)
    home = FakeClaudeHome(Path(env["CLAUDE_CONFIG_DIR"]))
    for i in range(CREATE_RUNS):
        total_ms = int(create_total_minutes(i) * 60_000)
        second = i in (1, 4, 7)
        untimed = i in (3, 8)
        steps = list(_CREATE_STEPS)
        steps += [("consistency check: pass 1", "Consistency Check", 0.05 if second else 0.10)]
        if second:
            steps += [("consistency check: pass 2", "Consistency Check", 0.05)]
        proj = eta_spec(root / "projects" / f"orbit-{i:02d}", [TaskDef(1)], spec=f"orbit-{i:02d}")
        session = home.session(f"-work-orbit-{i:02d}", cwd=proj, git_branch="main")
        start = _RUN0 + timedelta(days=i, hours=-1)
        cursor = to_ms(iso(start))
        agents = []
        for label, phase, share in steps:
            dur = int(total_ms * share)
            agents.append(AgentSpec(label, model="claude-opus-5-5" if label.startswith("review") else "claude-sonnet-5-5",
                                    phase=phase, duration_ms=dur, started_at_ms=cursor,
                                    omit_started=untimed and label.startswith(("link", "revise"))))
            cursor += dur
        session.create_run(f"wf_create-{i:02d}", start=iso(start), duration_ms=total_ms,
                           args={"feature": f"orbit-{i:02d}", "root": str(proj)},
                           result=create_result(feature=f"orbit-{i:02d}", consistency_passes=2 if second else 1), agents=agents)
    _collect(root, env)


def _build(name: str, root: Path) -> None:
    if name == POOL_IMPL:
        _build_impl(root, IMPL_RUNS, "impl")
    elif name == POOL_CREATE:
        _build_create(root)
    elif name == POOL_COSTLESS:
        _build_impl(root, COSTLESS_RUNS, "costless", costless=True)
    elif name == POOL_SIZED_FEW:
        _build_impl(root, SIZED_RUNS + UNSIZED_RUNS, "sized", delete_specs_from=SIZED_RUNS)
    elif name == POOL_IMPL_NOWAIT:
        base = build_pool(POOL_IMPL)
        shutil.copytree(base / "xdg-data", root / "xdg-data", dirs_exist_ok=True)
        keep = {impl_plan(i)[0] for i in range(IMPL_RUNS) if i % 2 == 1}
        runs_dir = root / "xdg-data" / "claude-kiro" / "runs"
        for path in sorted(runs_dir.glob("*.json")):
            if path.stem not in keep:
                path.unlink()
    else:
        raise ValueError(name)


_POOL_CACHE: Dict[str, Path] = {}
_DONE = ".built"
_DIGEST = ".digest"


def _source_key() -> str:
    """A hash of everything a pool's bytes depend on: this harness, the fake-home builders and the collector."""
    here = Path(__file__).resolve()
    src = Path(__import__("claude_kiro").__file__).resolve().parent
    digest = hashlib.sha1()
    for path in [here, here.with_name("stats_helpers.py"), *sorted(src.rglob("*.py"))]:
        digest.update(str(path.name).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def pool_root() -> Path:
    """The machine-wide directory that holds the pools of the current sources.

    The path depends only on the sources, so a record's project root and session id (both derived from it) are the same
    on every xdist worker: `-n auto` gives the same pool bytes everywhere."""
    return Path(tempfile.gettempdir()) / f"ck-eta-pools-{_source_key()}"


def _digest_of(root: Path) -> str:
    digest = hashlib.sha1()
    for path in sorted((root / "xdg-data" / "claude-kiro").rglob("*.json")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def build_pool(name: str) -> Path:
    """The pool's directory (holds `xdg-data/`). Built once per machine run under a file lock; every worker reads it."""
    import fcntl

    if name in _POOL_CACHE:
        return _POOL_CACHE[name]
    top = pool_root()
    top.mkdir(parents=True, exist_ok=True)
    root = top / name
    with open(top / f"{name}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (root / _DONE).exists():
            shutil.rmtree(root, ignore_errors=True)
            root.mkdir()
            _build(name, root)
            (root / _DIGEST).write_text(_digest_of(root))
            (root / _DONE).write_text("ok")
    _POOL_CACHE[name] = root
    return root


def install_pool(name: str, xdg: Path) -> Path:
    """Copy the pool's store into a test's `XDG_DATA_HOME`; returns `xdg`."""
    shutil.copytree(build_pool(name) / "xdg-data", xdg, dirs_exist_ok=True)
    return Path(xdg)


def history_records(pool: str | Path) -> List[dict]:
    """The stored `runs/*.json` of a pool (by name, or the pool directory) as dicts, in file-name order."""
    base = build_pool(pool) if isinstance(pool, str) and pool in POOLS else Path(pool)
    runs = base / "xdg-data" / "claude-kiro" / "runs"
    return [json.loads(p.read_text()) for p in sorted(runs.glob("*.json"))]
