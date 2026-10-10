"""Benchmark harness: compare agent configurations by running whole specs and scoring them on hidden tests.

Protocol (see README, "Benchmarking model configurations"):
- every (spec, config, rep) arm runs sequentially, in a randomised but seeded
  order, in a fresh `git clone --no-hardlinks` of the repo at a pinned base;
- the arm's agent settings are written with `ck agents set ... --project` and
  committed, then `ck run <spec> --output-format json --session-id <uuid>`;
- results come from what the run leaves behind, found by the session id:
  the `claude -p` JSON result (total_cost_usd, permission_denials), the
  workflow's persisted run file
  (<claude home>/projects/*/<session>/workflows/wf_*.json, its `result` is the
  spec-implement report) and the session and subagent transcripts (per-request
  token usage, AskUserQuestion calls, permission decisions);
- scoring overlays a hidden acceptance suite and safety invariants (pytest
  files kept outside the repo, never shown to agents) on each arm's final
  integration tree and reports per-arm metrics with clustered bootstrap CIs.

Pure stdlib: no numpy, no YAML unless PyYAML happens to be installed.
"""

import json
import math
import os
import random
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA = "ck-bench-result/1"

# List prices in USD per 1M tokens, dated 2026-10. Override per family with
# "pricing" in the bench config, e.g. {"opus": {"input": 5, "output": 25}}.
# Cache writes (5-minute TTL) cost 1.25x input unless "cacheCreation" is set.
PRICING_DATE = "2026-10"
PRICING: Dict[str, Dict[str, float]] = {
    "haiku": {"input": 0.10, "output": 0.50, "cacheRead": 0.01},
    "sonnet": {"input": 2.0, "output": 10.0, "cacheRead": 0.20},
    "opus": {"input": 4.0, "output": 20.0, "cacheRead": 0.20},
}
CACHE_WRITE_FACTOR = 1.25
USAGE_KEYS = ("input", "output", "cacheRead", "cacheCreation")

# Tool-result text that means the permission layer refused a call.
DENIAL_PHRASES = ("Permission for this action was denied", "auto mode classifier")
# A failed task whose reason cites the permission layer: the run needed a human.
REFUSAL_RE = re.compile(r"refused by the permission|permission (layer|check|denied)|was denied|needs? (my|your|a human|the user'?s?) (answer|approval|decision)", re.I)
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
TASK_RE = re.compile(r"task[_-]?(\d+)", re.I)


class BenchError(Exception):
    pass


# ---------------------------------------------------------------- pricing


def normalize_model(model: Optional[str]) -> str:
    return re.sub(r"\[[^\]]*\]$", "", model or "?")


def model_family(model: Optional[str], pricing: Optional[dict] = None) -> Optional[str]:
    m = (model or "").lower()
    for fam in list((pricing or {}).keys()) + list(PRICING):
        if fam in m:
            return fam
    return None


def rates(model: Optional[str], pricing: Optional[dict] = None) -> Optional[Dict[str, float]]:
    fam = model_family(model, pricing)
    if fam is None:
        return None
    r = {**PRICING.get(fam, {}), **((pricing or {}).get(fam) or {})}
    if "input" not in r:
        return None
    r.setdefault("output", 0.0)
    r.setdefault("cacheRead", 0.0)
    r.setdefault("cacheCreation", r["input"] * CACHE_WRITE_FACTOR)
    return r


def price_usage(model: Optional[str], usage: Dict[str, int], pricing: Optional[dict] = None) -> Optional[float]:
    """USD for a usage dict {input, output, cacheRead, cacheCreation}; None for an unknown model."""
    r = rates(model, pricing)
    if r is None:
        return None
    return sum((usage.get(k) or 0) * r[k] for k in USAGE_KEYS) / 1e6


# ---------------------------------------------------------------- config


def _argv(value: Any, default: Optional[List[str]]) -> Optional[List[str]]:
    if value is None:
        return default
    return shlex.split(value) if isinstance(value, str) else [str(v) for v in value]


def load_config(path: Path) -> Dict[str, Any]:
    path = Path(path).resolve()
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError:
            raise BenchError(f"{path}: YAML needs PyYAML installed; or write the config as JSON")
        cfg = yaml.safe_load(text)
    else:
        try:
            cfg = json.loads(text)
        except json.JSONDecodeError as e:
            raise BenchError(f"{path}: {e}")
    return normalize_config(cfg, path.parent)


def normalize_config(cfg: Dict[str, Any], base_dir: Path) -> Dict[str, Any]:
    if not isinstance(cfg, dict):
        raise BenchError("bench config must be an object")
    for key in ("repo", "base", "specs", "configs"):
        if not cfg.get(key):
            raise BenchError(f"bench config needs {key!r}")

    def rel(p: Optional[str]) -> Optional[str]:
        if p is None:
            return None
        q = Path(os.path.expanduser(p))
        return str(q if q.is_absolute() else (base_dir / q).resolve())

    out = dict(cfg)
    repo = str(cfg["repo"])
    out["repo"] = repo if re.match(r"^[a-z]+://|^[^/]+@[^:]+:", repo) else rel(repo)
    out["seed"] = int(cfg.get("seed", 0))
    out["reps"] = int(cfg.get("reps", 1))
    if out["reps"] < 1:
        raise BenchError("reps must be at least 1")
    out["ck"] = _argv(cfg.get("ck"), ["ck"])
    out["runner"] = _argv(cfg.get("runner"), None)
    out["env"] = {str(k): str(v) for k, v in (cfg.get("env") or {}).items()}
    out["bench_dir"] = rel(cfg.get("bench_dir") or "bench-runs")
    out["results_dir"] = rel(cfg.get("results_dir") or "bench-results")
    out["pricing"] = cfg.get("pricing") or {}
    out["timeout_minutes"] = cfg.get("timeout_minutes")
    specs = []
    for s in cfg["specs"]:
        s = {"name": s} if isinstance(s, str) else dict(s)
        if not NAME_RE.match(str(s.get("name", ""))):
            raise BenchError(f"spec name {s.get('name')!r} must match {NAME_RE.pattern}")
        s["hidden_tests"] = rel(s.get("hidden_tests"))
        s["invariants"] = rel(s.get("invariants"))
        s["hidden_dest"] = s.get("hidden_dest") or "tests/ck_bench_hidden"
        s["invariants_dest"] = s.get("invariants_dest") or "tests/ck_bench_invariants"
        s["test_cmd"] = _argv(s.get("test_cmd"), ["python", "-m", "pytest"])
        s["task_map"] = s.get("task_map") or {}
        specs.append(s)
    configs, seen = [], set()
    for c in cfg["configs"]:
        c = dict(c)
        name = str(c.get("name", ""))
        if not NAME_RE.match(name) or name in seen:
            raise BenchError(f"config name {name!r} must be unique and match {NAME_RE.pattern}")
        seen.add(name)
        c["agents"] = {str(k): v for k, v in (c.get("agents") or {}).items()}
        c["run_args"] = _argv(c.get("run_args"), [])
        configs.append(c)
    out["specs"], out["configs"] = specs, configs
    return out


def arm_id(spec: str, config: str, rep: int) -> str:
    return f"{spec}--{config}--r{rep}"


def plan_arms(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every (spec, config, rep), shuffled with the config's seed so order effects spread across arms."""
    arms = [
        {"id": arm_id(s["name"], c["name"], r), "spec": s["name"], "config": c["name"], "rep": r}
        for s in cfg["specs"] for c in cfg["configs"] for r in range(1, cfg["reps"] + 1)
    ]
    random.Random(cfg.get("seed", 0)).shuffle(arms)
    return arms


# ---------------------------------------------------------------- collecting a run


def claude_home(env: Optional[Dict[str, str]] = None) -> Path:
    env = env if env is not None else dict(os.environ)
    return Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def parse_result_log(path: Path) -> Optional[Dict[str, Any]]:
    """The `claude -p --output-format json` result object; tolerates other lines around it."""
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "result":
                return obj
    return None


def find_workflow_run(home: Path, session_id: str) -> Optional[Dict[str, Any]]:
    """The spec-implement run the session persisted (latest if it ran more than one)."""
    found = []
    for f in home.glob(f"projects/*/{session_id}/workflows/*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d, dict):
            found.append((d.get("workflowName") == "spec-implement", str(d.get("timestamp") or ""), f.stat().st_mtime, d, f))
    if not found:
        return None
    found.sort(key=lambda x: x[:3])
    d, f = found[-1][3], found[-1][4]
    return {**d, "_path": str(f)}


def _blocks(content: Any) -> List[dict]:
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def scan_transcript(path: Path) -> Dict[str, Any]:
    """Per-model token usage (one count per API request), AskUserQuestion calls and permission refusals."""
    requests: Dict[str, Tuple[str, Dict[str, int]]] = {}
    asked, denials = 0, []
    for i, line in enumerate(Path(path).read_text(errors="replace").splitlines()):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        decision = (d.get("permissionDecision") or {}).get("decision") if isinstance(d.get("permissionDecision"), dict) else None
        if decision and decision not in ("accept", "allow"):
            denials.append(f"permission decision {decision!r}")
        m = d.get("message")
        if not isinstance(m, dict):
            continue
        if d.get("type") == "assistant":
            u = m.get("usage")
            if isinstance(u, dict):
                key = d.get("requestId") or m.get("id") or f"line{i}"
                cur = {
                    "input": u.get("input_tokens") or 0,
                    "output": u.get("output_tokens") or 0,
                    "cacheRead": u.get("cache_read_input_tokens") or 0,
                    "cacheCreation": u.get("cache_creation_input_tokens") or 0,
                }
                model, prev = requests.get(key, (normalize_model(m.get("model")), {}))
                requests[key] = (model, {k: max(cur[k], prev.get(k, 0)) for k in USAGE_KEYS})
            for b in _blocks(m.get("content")):
                if b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion":
                    asked += 1
        elif d.get("type") == "user":
            for b in _blocks(m.get("content")):
                if b.get("type") == "tool_result" and b.get("is_error"):
                    c = b.get("content")
                    text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in _blocks(c))
                    if any(p in text for p in DENIAL_PHRASES):
                        denials.append("tool call refused by the permission layer")
    by_model: Dict[str, Dict[str, int]] = {}
    for model, u in requests.values():
        acc = by_model.setdefault(model, dict.fromkeys(USAGE_KEYS, 0))
        for k in USAGE_KEYS:
            acc[k] += u[k]
    return {"usage_by_model": by_model, "requests": len(requests), "asked": asked, "denials": denials}


def _price_by_model(by_model: Dict[str, Dict[str, int]], pricing: dict) -> Optional[float]:
    total = 0.0
    for model, u in by_model.items():
        p = price_usage(model, u, pricing)
        if p is None:
            return None
        total += p
    return total


def summarize_report(report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    r = report if isinstance(report, dict) else {}
    tasks = []
    for t in r.get("tasks") or []:
        review = t.get("review") or {}
        tasks.append({
            "task": str(t.get("task")),
            "ok": bool(t.get("ok")),
            "attempts": t.get("attempts"),
            "review_rounds": review.get("rounds") or 0,
            "verdict": review.get("verdict"),
            "escalations": [e.get("model") for e in t.get("escalations") or [] if isinstance(e, dict)],
            "agent_minutes": t.get("agentMinutes"),
        })
    failed = r.get("failed") or []
    final_gate = r.get("finalGate")
    return {
        "found": bool(r),
        "into": r.get("into"),
        "merged": [str(x) for x in r.get("merged") or []],
        "failed": [str(f.get("task")) if isinstance(f, dict) else str(f) for f in failed],
        "failed_reasons": {str(f.get("task")): f.get("reason") for f in failed if isinstance(f, dict)},
        "not_started": [str(x) for x in r.get("notStarted") or []],
        "halted": r.get("halted"),
        "halt_reason": r.get("reason"),
        "target_green": r.get("targetGreen"),
        "final_gate_green": final_gate.get("green") if isinstance(final_gate, dict) else None,
        "escalations": sum(len(t["escalations"]) for t in tasks) + len(r.get("fixerEscalations") or []),
        "escalated": r.get("escalated") or [],
        "review_rounds": sum(t["review_rounds"] for t in tasks),
        "workflow_minutes": (r.get("timing") or {}).get("minutes"),
        "agents": r.get("agents"),
        "tasks": tasks,
    }


def collect_agents(progress: Iterable[dict], transcripts: Dict[str, Path], pricing: dict) -> Tuple[List[dict], List[str], int]:
    """Per-agent rows. Usage comes from the agent's transcript when there is one; otherwise the
    workflow's `tokens` (the agent's final context size, not its cumulative usage) priced at the
    input rate, marked as an estimate. Returns (rows, refusals, AskUserQuestion count)."""
    rows, refusals, asked = [], [], 0
    for a in progress or []:
        if not isinstance(a, dict) or a.get("type") != "workflow_agent":
            continue
        aid = a.get("agentId")
        model = normalize_model(a.get("model"))
        row = {"agent_id": aid, "label": a.get("label"), "model": model, "tokens": a.get("tokens"),
               "duration_ms": a.get("durationMs"), "state": a.get("state"), "usage": None, "cost_usd": None, "cost_basis": None}
        tx = transcripts.get(aid) if aid else None
        if tx:
            s = scan_transcript(tx)
            usage = dict.fromkeys(USAGE_KEYS, 0)
            for u in s["usage_by_model"].values():
                for k in USAGE_KEYS:
                    usage[k] += u[k]
            row.update(usage=usage, usage_by_model=s["usage_by_model"], cost_usd=_price_by_model(s["usage_by_model"], pricing), cost_basis="transcript")
            refusals += [f"{a.get('label')}: {x}" for x in s["denials"]]
            asked += s["asked"]
        elif a.get("tokens") is not None:
            row.update(cost_usd=price_usage(model, {"input": a.get("tokens") or 0}, pricing), cost_basis="context-tokens-as-input")
        rows.append(row)
    return rows, refusals, asked


def _cost_summary(agents: List[dict], total_cost_usd: Optional[float], extra: Optional[float] = None) -> Dict[str, Any]:
    bases = {a["cost_basis"] for a in agents if a["cost_basis"]}
    priced = [a["cost_usd"] for a in agents if a["cost_usd"] is not None]
    priced_usd = (sum(priced) + (extra or 0.0)) if (priced or extra is not None) else None
    estimated = "context-tokens-as-input" in bases or any(a["cost_usd"] is None for a in agents)
    basis = ("transcripts" if bases <= {"transcript"} else
             "workflowProgress context tokens priced at the input rate (no usage breakdown; a low estimate)" if bases == {"context-tokens-as-input"}
             else "mixed: transcripts where present, else context tokens priced at the input rate")
    out = {
        "total_cost_usd": total_cost_usd,
        "priced_usd": priced_usd,
        "usd": total_cost_usd if total_cost_usd is not None else priced_usd,
        "source": "claude total_cost_usd" if total_cost_usd is not None else "priced tokens",
        "basis": basis,
        "estimated": estimated if total_cost_usd is None else False,
        "pricing_date": PRICING_DATE,
    }
    if total_cost_usd and priced_usd and not estimated and abs(priced_usd - total_cost_usd) / total_cost_usd > 0.25:
        out["discrepancy"] = f"priced transcripts ${priced_usd:.2f} vs claude total_cost_usd ${total_cost_usd:.2f}"
    return out


def fail_closed_reasons(summary: Dict[str, Any], session: Optional[dict], refusals: List[str], asked: int) -> List[str]:
    reasons = []
    denials = (session or {}).get("permission_denials") or []
    if denials:
        tools = sorted({str(d.get("tool_name")) for d in denials if isinstance(d, dict)})
        reasons.append(f"{len(denials)} permission denial(s) in the session ({', '.join(tools)})")
    if asked:
        reasons.append(f"AskUserQuestion called {asked} time(s): the run needed a human answer")
    if refusals:
        reasons.append(f"{len(refusals)} permission refusal(s) in agent transcripts, e.g. {refusals[0]}")
    for task, reason in (summary.get("failed_reasons") or {}).items():
        if reason and REFUSAL_RE.search(reason):
            reasons.append(f"task {task} failed citing a permission refusal or a needed human decision")
    return reasons


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise BenchError(f"git {' '.join(args)} failed in {cwd}: {p.stderr.strip()}")
    return p.stdout.strip() if p.returncode == 0 else ""


def collect_session(session_id: str, home: Path, log: Path, pricing: dict) -> Dict[str, Any]:
    session = parse_result_log(log)
    wf = find_workflow_run(home, session_id)
    tx: Dict[str, Path] = {}
    main_tx = None
    for d in home.glob(f"projects/*/{session_id}"):
        for f in d.glob("subagents/**/agent-*.jsonl"):
            tx[f.stem[len("agent-"):]] = f
    for f in home.glob(f"projects/*/{session_id}.jsonl"):
        main_tx = f
    summary = summarize_report((wf or {}).get("result"))
    agents, refusals, asked = collect_agents((wf or {}).get("workflowProgress") or [], tx, pricing)
    main_cost = None
    if main_tx:
        s = scan_transcript(main_tx)
        usage = {k: sum(u[k] for u in s["usage_by_model"].values()) for k in USAGE_KEYS}
        main_cost = _price_by_model(s["usage_by_model"], pricing)
        agents.append({"agent_id": None, "label": "orchestrator session", "model": ", ".join(s["usage_by_model"]) or "?",
                       "tokens": None, "duration_ms": None, "state": None, "usage": usage, "usage_by_model": s["usage_by_model"],
                       "cost_usd": main_cost, "cost_basis": "transcript"})
        refusals += [f"session: {x}" for x in s["denials"]]
        asked += s["asked"]
    cost = _cost_summary(agents, (session or {}).get("total_cost_usd"))
    reasons = fail_closed_reasons(summary, session, refusals, asked)
    tokens_by_model: Dict[str, Dict[str, int]] = {}
    for a in agents:
        for model, u in (a.get("usage_by_model") or {}).items():
            acc = tokens_by_model.setdefault(model, dict.fromkeys(USAGE_KEYS, 0))
            for k in USAGE_KEYS:
                acc[k] += u[k]
    return {
        "session": {k: (session or {}).get(k) for k in ("subtype", "is_error", "num_turns", "duration_ms", "total_cost_usd", "modelUsage", "permission_denials")},
        "workflow_file": (wf or {}).get("_path"),
        "workflow_status": (wf or {}).get("status"),
        "report": summary,
        "agents": agents,
        "tokens_by_model": tokens_by_model,
        "cost": cost,
        "fail_closed": bool(reasons),
        "fail_closed_reasons": reasons,
    }


# ---------------------------------------------------------------- running


def _sh(argv: Sequence[str], cwd: Path, env: Dict[str, str], timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def run_arm(cfg: Dict[str, Any], arm: Dict[str, Any], echo: Callable[[str], None] = print) -> Dict[str, Any]:
    """Clone, set up, launch and collect one arm; writes and returns results/<id>.json."""
    spec = next(s for s in cfg["specs"] if s["name"] == arm["spec"])
    conf = next(c for c in cfg["configs"] if c["name"] == arm["config"])
    results_dir, bench_dir = Path(cfg["results_dir"]), Path(cfg["bench_dir"])
    clone = bench_dir / arm["id"]
    env = {**os.environ, **cfg["env"]}
    ck = cfg["ck"]
    if clone.exists():
        shutil.rmtree(clone)  # a half-finished earlier attempt
    bench_dir.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(["git", "clone", "-q", "--no-hardlinks", cfg["repo"], str(clone)], capture_output=True, text=True)
    if p.returncode != 0:
        raise BenchError(f"git clone failed: {p.stderr.strip()}")
    base_sha = _git(clone, "rev-parse", "--verify", "-q", f"{cfg['base']}^{{commit}}", check=False) or \
        _git(clone, "rev-parse", "--verify", f"origin/{cfg['base']}^{{commit}}")
    branch = f"bench/{arm['config']}-{arm['rep']}"
    _git(clone, "checkout", "-q", "-b", branch, base_sha)

    version = _sh([*ck, "--version"], clone, env)
    ck_version = (version.stdout or version.stderr).strip().splitlines()[-1] if (version.stdout or version.stderr).strip() else None
    up = _sh([*ck, "upgrade"], clone, env)
    if up.returncode != 0:
        raise BenchError(f"ck upgrade failed in {clone}:\n{up.stdout}{up.stderr}")
    for key, value in conf["agents"].items():
        s = _sh([*ck, "agents", "set", key, str(value), "--project"], clone, env)
        if s.returncode != 0:
            raise BenchError(f"ck agents set {key} {value} failed:\n{s.stdout}{s.stderr}")
    if _git(clone, "status", "--porcelain"):
        _git(clone, "add", "-A")
        _git(clone, "-c", "user.name=ck-bench", "-c", "user.email=ck-bench@localhost", "commit", "-qm", f"bench: set up {arm['config']}")
    setup_sha = _git(clone, "rev-parse", "HEAD")

    session_id = str(uuid.uuid4())
    logs = results_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{arm['id']}.json"
    argv = [*(cfg["runner"] or ck), "run", arm["spec"], "--output-format", "json", "--session-id", session_id, "--log", str(log), *conf["run_args"]]
    echo(f"▶ {arm['id']}: {' '.join(shlex.quote(a) for a in argv)}  (in {clone})")
    timeout = float(cfg["timeout_minutes"]) * 60 if cfg.get("timeout_minutes") else None
    started = time.time()
    try:
        launched = _sh(argv, clone, env, timeout)
        exit_code, launch_out = launched.returncode, launched.stdout + launched.stderr
    except subprocess.TimeoutExpired as e:
        exit_code, launch_out = None, f"timed out after {timeout}s\n{e.stdout or ''}"
    finished = time.time()
    (logs / f"{arm['id']}.ck-run.txt").write_text(launch_out if isinstance(launch_out, str) else launch_out.decode(errors="replace"))

    collected = collect_session(session_id, claude_home(env), log, cfg["pricing"])
    into = collected["report"].get("into") or f"integrate/{arm['spec']}"
    final_sha = _git(clone, "rev-parse", "--verify", "-q", f"{into}^{{commit}}", check=False) or None
    result = {
        "schema": SCHEMA,
        "source": "run",
        **arm,
        "config_order": [c["name"] for c in cfg["configs"]],
        "spec_settings": {k: spec.get(k) for k in ("hidden_tests", "invariants", "hidden_dest", "invariants_dest", "test_cmd", "task_map")},
        "agents_settings": conf["agents"],
        "run_args": conf["run_args"],
        "ck_version": ck_version,
        "ck_version_cmd": [*ck, "--version"],
        "repo": cfg["repo"],
        "base": cfg["base"],
        "base_sha": base_sha,
        "setup_sha": setup_sha,
        "clone": str(clone),
        "branch": branch,
        "into": into,
        "final_sha": final_sha,
        "argv": argv,
        "session_id": session_id,
        "log": str(log),
        "exit_code": exit_code,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(finished)),
        "wall_minutes": round((finished - started) / 60, 3),
        **collected,
    }
    if exit_code is None:
        result["fail_closed"] = True
        result["fail_closed_reasons"].append("timed out")
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / f"{arm['id']}.json").write_text(json.dumps(result, indent=2))
    return result


# ---------------------------------------------------------------- import


def import_output(path: Path, spec: str, config: str, rep: int, pricing: Optional[dict] = None,
                  transcripts: Optional[Path] = None, clone: Optional[Path] = None, ref: Optional[str] = None) -> Dict[str, Any]:
    """Results JSON from an existing workflow output file ({result, workflowProgress, ...})."""
    d = json.loads(Path(path).read_text())
    if not isinstance(d, dict) or "result" not in d:
        raise BenchError(f"{path}: expected a workflow output object with a 'result' key")
    report = d["result"]
    if isinstance(report, str):
        report = json.loads(report)
    summary = summarize_report(report)
    tx = {f.stem[len("agent-"):]: f for f in Path(transcripts).glob("**/agent-*.jsonl")} if transcripts else {}
    agents, refusals, asked = collect_agents(d.get("workflowProgress") or [], tx, pricing or {})
    into = ref or (report or {}).get("into")
    final_sha = None
    if clone and into:
        final_sha = _git(Path(clone), "rev-parse", "--verify", "-q", f"{into}^{{commit}}", check=False) or None
    wall = summary["workflow_minutes"]
    if wall is None and d.get("durationMs"):
        wall = round(d["durationMs"] / 60000, 3)
    reasons = fail_closed_reasons(summary, None, refusals, asked)
    return {
        "schema": SCHEMA,
        "source": "import",
        "imported_from": str(Path(path).resolve()),
        "id": arm_id(spec, config, rep), "spec": spec, "config": config, "rep": rep,
        "config_order": None,
        "spec_settings": None,
        "ck_version": None,
        "clone": str(Path(clone).resolve()) if clone else None,
        "into": into,
        "final_sha": final_sha,
        "exit_code": None,
        "wall_minutes": wall,
        "session": None,
        "workflow_file": str(Path(path).resolve()),
        "workflow_status": d.get("status"),
        "report": summary,
        "agents": agents,
        "tokens_by_model": {},
        "cost": _cost_summary(agents, None),
        "fail_closed": bool(reasons),
        "fail_closed_reasons": reasons,
        "notes": ["imported: not a controlled run (no pinned base, clone or ck version recorded)"],
    }


# ---------------------------------------------------------------- scoring


def bootstrap_ci(clusters: Sequence[Any], stat: Callable[[List[Any]], Optional[float]], n: int = 2000,
                 seed: int = 0, alpha: float = 0.05) -> Tuple[Optional[float], Optional[float]]:
    """Percentile bootstrap resampling whole clusters with replacement."""
    clusters = list(clusters)
    if not clusters:
        return (None, None)
    rng = random.Random(seed)
    k = len(clusters)
    vals = []
    for _ in range(n):
        v = stat([clusters[rng.randrange(k)] for _ in range(k)])
        if v is not None:
            vals.append(v)
    if not vals:
        return (None, None)
    vals.sort()
    lo = vals[int(math.floor(alpha / 2 * (len(vals) - 1)))]
    hi = vals[int(math.ceil((1 - alpha / 2) * (len(vals) - 1)))]
    return (lo, hi)


def run_suite(tree: Path, src: Optional[str], dest: str, cmd: List[str]) -> Optional[Dict[str, Any]]:
    """Copy a pytest suite into the tree at DEST and run it; per-test pass/fail/skip from JUnit XML."""
    if not src:
        return None
    src_p = Path(src)
    if not src_p.exists():
        return {"tests": {}, "error": f"{src} does not exist"}
    target = tree / dest
    if src_p.is_dir():
        shutil.copytree(src_p, target, dirs_exist_ok=True)
    else:
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_p, target / src_p.name)
    with tempfile.TemporaryDirectory(prefix="ck-bench-junit-") as tmp:
        xml = Path(tmp) / "junit.xml"
        p = subprocess.run([*cmd, dest, "-q", "-p", "no:cacheprovider", f"--junitxml={xml}"], cwd=tree, capture_output=True, text=True)
        tests: Dict[str, str] = {}
        if xml.exists():
            for case in ET.parse(xml).getroot().iter("testcase"):
                tid = f"{case.get('classname', '')}::{case.get('name', '')}"
                tags = {c.tag for c in case}
                tests[tid] = "fail" if tags & {"failure", "error"} else "skip" if "skipped" in tags else "pass"
    out: Dict[str, Any] = {"tests": tests, "exit_code": p.returncode}
    if p.returncode not in (0, 1) or not tests:
        out["error"] = (p.stdout + p.stderr)[-2000:]
    return out


def score_tree(result: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay the hidden suite and the invariants on the arm's final tree, in a throwaway checkout."""
    clone, sha = result.get("clone"), result.get("final_sha")
    if not clone or not sha or not Path(clone).is_dir():
        return {"hidden": None, "invariants": None, "error": "no final tree (clone or final_sha missing)"}
    with tempfile.TemporaryDirectory(prefix="ck-bench-score-") as tmp:
        tree = Path(tmp) / "tree"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", "--no-checkout", clone, str(tree)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(tree), "checkout", "-q", "--detach", sha], check=True, capture_output=True)
        hidden = run_suite(tree, spec.get("hidden_tests"), spec.get("hidden_dest") or "tests/ck_bench_hidden", spec.get("test_cmd") or ["python", "-m", "pytest"])
        inv = run_suite(tree, spec.get("invariants"), spec.get("invariants_dest") or "tests/ck_bench_invariants", spec.get("test_cmd") or ["python", "-m", "pytest"])
    return {"hidden": hidden, "invariants": inv}


def task_of(test_id: str, task_map: Dict[str, Any]) -> str:
    """Which task a hidden test belongs to: task_map (substring of the test id, paths may use / and .py),
    else `task<N>` in the test's module or name, else "*" (the spec as a whole)."""
    for pattern, task in (task_map or {}).items():
        norm = pattern.replace("/", ".").removesuffix(".py")
        if pattern in test_id or norm in test_id:
            return str(task)
    m = TASK_RE.search(test_id)
    return m.group(1) if m else "*"


def _mean(xs: List[Optional[float]]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _ratio(num: List[Optional[float]], den: List[Optional[float]]) -> Optional[float]:
    pairs = [(a, b) for a, b in zip(num, den) if a is not None and b is not None]
    d = sum(b for _, b in pairs)
    return sum(a for a, _ in pairs) / d if d else None


def score_results(results_dir: Path, cfg: Optional[Dict[str, Any]] = None, seed: int = 0, n_boot: int = 2000,
                  baseline: Optional[str] = None, cluster: str = "task") -> Dict[str, Any]:
    results = []
    for f in sorted(Path(results_dir).glob("*.json")):
        try:
            r = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict) and r.get("schema") == SCHEMA:
            results.append(r)
    if not results:
        raise BenchError(f"no results in {results_dir}")
    spec_cfgs = {s["name"]: s for s in (cfg or {}).get("specs", [])}
    warnings: List[str] = []

    runs = []
    for r in results:
        spec = spec_cfgs.get(r["spec"]) or r.get("spec_settings") or {}
        tree = score_tree(r, spec) if (spec.get("hidden_tests") or spec.get("invariants")) else {"hidden": None, "invariants": None, "error": "no hidden suite configured"}
        if tree.get("error"):
            warnings.append(f"{r['id']}: {tree['error']}")
        for kind in ("hidden", "invariants"):
            if tree.get(kind) and tree[kind].get("error"):
                warnings.append(f"{r['id']} {kind}: {tree[kind]['error'][-300:]}")
        rep = r.get("report") or {}
        runs.append({"result": r, "tree": tree, "spec": r["spec"], "config": r["config"], "task_map": spec.get("task_map") or {},
                     "hidden_configured": bool(spec.get("hidden_tests")), "merged": set(rep.get("merged") or [])})

    # Universe of hidden tests per spec: every test any arm's run reported. A test missing from a run counts as failed.
    universe: Dict[str, set] = {}
    for x in runs:
        if x["tree"].get("hidden"):
            universe.setdefault(x["spec"], set()).update(x["tree"]["hidden"]["tests"])
    for x in runs:
        tests = universe.get(x["spec"]) or set()
        got = (x["tree"].get("hidden") or {}).get("tests") or {}
        closed = x["result"].get("fail_closed")
        status = {t: ("fail" if closed else got.get(t, "fail")) for t in tests}
        counted = [t for t in tests if status[t] != "skip"]
        x["hidden_pass"] = (sum(status[t] == "pass" for t in counted) / len(counted)) if counted and x["hidden_configured"] else None
        units: Dict[str, List[str]] = {}
        for t in counted:
            units.setdefault(task_of(t, x["task_map"]), []).append(t)
        x["units"] = {u: all(status[t] == "pass" for t in ts) for u, ts in units.items()} if x["hidden_configured"] else {}
        x["unit_merged"] = {u: (u in x["merged"]) if u != "*" else bool(x["result"].get("final_sha")) for u in x["units"]}
        x["tasks_passing"] = sum(x["units"].values()) if x["hidden_configured"] else None
        inv = x["tree"].get("invariants")
        x["inv_ok"] = (bool(inv["tests"]) and not inv.get("error") and all(v != "fail" for v in inv["tests"].values())) if inv else None
        x["usd"] = (x["result"].get("cost") or {}).get("usd")
        rep = x["result"].get("report") or {}
        x["planned"] = len(rep.get("merged") or []) + len(rep.get("failed") or []) + len(rep.get("not_started") or [])
        x["n_merged"] = len(rep.get("merged") or [])
        x["escalations"] = rep.get("escalations")
        x["review_rounds"] = rep.get("review_rounds")
        x["wall"] = x["result"].get("wall_minutes")

    order = next((r.get("config_order") for r in results if r.get("config_order")), None) or \
        ([c["name"] for c in cfg["configs"]] if cfg else sorted({r["config"] for r in results}))
    arms_names = [a for a in order if any(x["config"] == a for x in runs)] + sorted({x["config"] for x in runs} - set(order))
    baseline = baseline or arms_names[0]

    def metric(clusters: List[List[dict]], fn: Callable[[List[dict]], Optional[float]]) -> Dict[str, Any]:
        flat = lambda cs: [x for c in cs for x in c]
        lo, hi = bootstrap_ci(clusters, lambda cs: fn(flat(cs)), n=n_boot, seed=seed)
        return {"value": fn(flat(clusters)), "ci": [lo, hi]}

    arms: Dict[str, Any] = {}
    for a in arms_names:
        rs = [x for x in runs if x["config"] == a]
        by_spec: Dict[str, List[dict]] = {}
        for x in rs:
            by_spec.setdefault(x["spec"], []).append(x)
        cl = list(by_spec.values())
        bases = sorted({(x["result"].get("cost") or {}).get("source", "?") + (" (estimated)" if (x["result"].get("cost") or {}).get("estimated") else "") for x in rs})
        arms[a] = {
            "runs": len(rs),
            "specs": len(cl),
            "fail_closed_runs": sum(bool(x["result"].get("fail_closed")) for x in rs),
            "tasks_passing_hidden": sum(x["tasks_passing"] or 0 for x in rs),
            "hidden_pass_rate": metric(cl, lambda xs: _mean([x["hidden_pass"] for x in xs])),
            "invariants_pass_rate": metric(cl, lambda xs: _mean([None if x["inv_ok"] is None else float(x["inv_ok"]) for x in xs])),
            "usd_per_passing_task": metric(cl, lambda xs: _ratio([x["usd"] for x in xs], [x["tasks_passing"] for x in xs])),
            "usd_per_run": metric(cl, lambda xs: _mean([x["usd"] for x in xs])),
            "merged_rate": metric(cl, lambda xs: _ratio([x["n_merged"] for x in xs], [x["planned"] for x in xs])),
            "escalations_per_run": metric(cl, lambda xs: _mean([x["escalations"] for x in xs])),
            "review_rounds_per_run": metric(cl, lambda xs: _mean([x["review_rounds"] for x in xs])),
            "wall_minutes": metric(cl, lambda xs: _mean([x["wall"] for x in xs])),
            "cost_sources": bases,
        }
    all_bases = {b for a in arms.values() for b in a["cost_sources"]}
    if len(all_bases) > 1:
        warnings.append(f"cost comes from different sources across runs ({'; '.join(sorted(all_bases))}): USD comparisons are not like for like")

    paired: Dict[str, Any] = {}
    for a in arms_names:
        if a == baseline:
            continue
        units = []
        for spec in sorted({x["spec"] for x in runs}):
            def per_unit(arm: str) -> Dict[str, List[float]]:
                acc: Dict[str, List[float]] = {}
                for x in runs:
                    if x["config"] == arm and x["spec"] == spec:
                        for u, ok in x["units"].items():
                            if x["unit_merged"].get(u):
                                acc.setdefault(u, []).append(float(ok))
                return acc
            mine, base = per_unit(a), per_unit(baseline)
            for u in sorted(set(mine) & set(base)):
                units.append({"spec": spec, "task": u, "diff": _mean(mine[u]) - _mean(base[u])})
        if cluster == "spec":
            groups: Dict[str, List[dict]] = {}
            for u in units:
                groups.setdefault(u["spec"], []).append(u)
            ucl = list(groups.values())
        else:
            ucl = [[u] for u in units]
        flat_mean = lambda cs: _mean([u["diff"] for c in cs for u in c])
        lo, hi = bootstrap_ci(ucl, flat_mean, n=n_boot, seed=seed)
        cost_pairs = []
        for spec in sorted({x["spec"] for x in runs}):
            ca = _mean([x["usd"] for x in runs if x["config"] == a and x["spec"] == spec])
            cb = _mean([x["usd"] for x in runs if x["config"] == baseline and x["spec"] == spec])
            if ca is not None and cb is not None:
                cost_pairs.append([ca - cb])
        clo, chi = bootstrap_ci(cost_pairs, lambda cs: _mean([c[0] for c in cs]), n=n_boot, seed=seed)
        paired[a] = {
            "baseline": baseline,
            "units": len(units),
            "cluster": cluster,
            "hidden_pass_diff": {"value": flat_mean(ucl), "ci": [lo, hi]},
            "usd_per_run_diff_by_spec": {"value": _mean([c[0] for c in cost_pairs]), "ci": [clo, chi], "specs": len(cost_pairs)},
            "per_task": units,
        }

    per_run = [{
        "id": x["result"]["id"], "spec": x["spec"], "config": x["config"], "rep": x["result"].get("rep"),
        "fail_closed": bool(x["result"].get("fail_closed")), "hidden_pass": x["hidden_pass"], "tasks_passing": x["tasks_passing"],
        "units": x["units"], "invariants_ok": x["inv_ok"], "usd": x["usd"], "merged": sorted(x["merged"]), "wall_minutes": x["wall"],
        "hidden_tests": (x["tree"].get("hidden") or {}).get("tests"), "invariant_tests": (x["tree"].get("invariants") or {}).get("tests"),
    } for x in runs]
    return {"seed": seed, "bootstrap": n_boot, "baseline": baseline, "arms": arms, "paired": paired, "runs": per_run, "warnings": warnings}


def _fmt(m: Any, kind: str = "num") -> str:
    def one(v: Optional[float]) -> str:
        if v is None:
            return "–"
        return f"{v * 100:.0f}%" if kind == "pct" else f"${v:.2f}" if kind == "usd" else f"{v:.2f}"
    if isinstance(m, dict):
        lo, hi = m.get("ci") or [None, None]
        ci = f" [{one(lo)}, {one(hi)}]" if lo is not None and (lo != m.get("value") or hi != m.get("value")) else ""
        return one(m.get("value")) + ci
    return str(m)


def score_markdown(score: Dict[str, Any]) -> str:
    lines = [
        "# ck bench score", "",
        f"95% percentile bootstrap CIs ({score['bootstrap']} resamples, seed {score['seed']}); arm metrics resample specs, "
        "paired differences resample tasks. Fail-closed runs score 0 on the hidden suite.", "",
        "| arm | runs | specs | fail-closed | hidden pass rate | tasks passing hidden | USD / passing task | USD / run | merged rate | escalations / run | review rounds / run | wall min | invariants |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, a in score["arms"].items():
        lines.append(" | ".join([
            f"| {name}", str(a["runs"]), str(a["specs"]), str(a["fail_closed_runs"]),
            _fmt(a["hidden_pass_rate"], "pct"), str(a["tasks_passing_hidden"]), _fmt(a["usd_per_passing_task"], "usd"),
            _fmt(a["usd_per_run"], "usd"), _fmt(a["merged_rate"], "pct"), _fmt(a["escalations_per_run"]),
            _fmt(a["review_rounds_per_run"]), _fmt(a["wall_minutes"]), _fmt(a["invariants_pass_rate"], "pct"),
        ]) + " |")
    if score["paired"]:
        lines += ["", f"## Paired against {score['baseline']} (tasks both arms merged)", "",
                  "| arm | tasks | hidden pass diff | USD / run diff (by spec) |", "|---|---|---|---|"]
        for name, p in score["paired"].items():
            lines.append(f"| {name} | {p['units']} | {_fmt(p['hidden_pass_diff'])} | {_fmt(p['usd_per_run_diff_by_spec'], 'usd')} |")
    if score["warnings"]:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in score["warnings"]]
    return "\n".join(lines) + "\n"
