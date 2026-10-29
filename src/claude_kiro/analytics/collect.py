"""Collector core: turn a Claude Code run file and its agent transcripts into a local run record.

Seams owned here: S7 `cost_label`, S8 `summarize_outcomes`, S10 `auto_collect_enabled`, S27
`tie_ck_version` and `pseudonym`. The record is written with the Harness Contract key names. Numbers
that cannot be computed are `None`, never 0. A run that is not an allowlisted ck workflow is dropped
at once: its name and id are kept nowhere.
"""

import hashlib
import hmac
import json
import re
import subprocess
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from claude_kiro import bench, manifest
from claude_kiro.analytics import SCHEMA_LOCAL, roles, runfile, store

CostSource = str
OutcomeKind = str

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_HEAD_LINES = 50
_TAIL_BYTES = 1 << 20
_USAGE_KEYS = bench.USAGE_KEYS

_salt_lock = threading.Lock()


@dataclass(frozen=True)
class CollectOutcome:
    run_id: str
    kind: OutcomeKind
    record: Optional[dict] = None
    reason: Optional[str] = None
    error: Optional[str] = None  # not_found | not_allowlisted | unrecognised | sandboxed | unreadable


@dataclass(frozen=True)
class CollectCounts:
    collected: int = 0
    unchanged: int = 0
    updated: int = 0
    unfinished: int = 0
    failed: int = 0
    skipped_non_ck: int = 0
    failures: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CollectSummary:
    counts: CollectCounts
    exit_code: int
    outcomes: Tuple[CollectOutcome, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------- seams


def cost_label(transcript_agents: int, estimated_agents: int, unpriced_agents: int) -> CostSource:
    """S7: the run figure is `priced_from_transcripts` or `estimate`, never `claude_total`."""
    if transcript_agents > 0 and estimated_agents == 0 and unpriced_agents == 0:
        return "priced_from_transcripts"
    return "estimate"


def summarize_outcomes(outcomes: Sequence[OutcomeKind], skipped_non_ck: int) -> Tuple[CollectCounts, int]:
    """S8: counts of the allowlisted outcomes plus the skipped count, and the exit code (1 iff any failed)."""
    c = Counter(outcomes)
    counts = CollectCounts(
        collected=c.get("collected", 0), unchanged=c.get("unchanged", 0), updated=c.get("updated", 0),
        unfinished=c.get("unfinished", 0), failed=c.get("failed", 0), skipped_non_ck=skipped_non_ck,
    )
    return counts, 1 if counts.failed > 0 else 0


def auto_collect_enabled(run_role: Mapping) -> bool:
    """S10: unset means on."""
    return run_role.get("analytics", True) is not False


def tie_ck_version(manifest_version: Optional[str], manifest_mtime_ms: Optional[int], installed: str,
                   run_start_ms: Optional[int], fresh: bool, previous: Optional[str] = None) -> Optional[str]:
    """S27: the ck version the run used, only when it can be tied to the run."""
    mv = manifest_version if isinstance(manifest_version, str) and manifest_version else None
    if (mv is not None and manifest_mtime_ms is not None and run_start_ms is not None
            and manifest_mtime_ms < run_start_ms):
        return mv
    if fresh:
        return mv or installed
    if isinstance(previous, str) and previous:
        return previous
    return None


def _normalise_remote(remote: str) -> str:
    r = remote.strip().lower()
    r = re.sub(r"^[a-z][a-z0-9+.-]*://", "", r)
    r = re.sub(r"^[^@/]*@", "", r)
    r = re.sub(r"^([^/:]+):(?!\d+/)", r"\1/", r)
    r = r.rstrip("/")
    if r.endswith(".git"):
        r = r[:-4]
    return r.rstrip("/")


def pseudonym(remote: str, salt: bytes) -> str:
    """HMAC-SHA256 (keyed by the salt) of the normalised remote, first 16 hex characters."""
    return hmac.new(bytes(salt), _normalise_remote(remote).encode("utf-8"), hashlib.sha256).hexdigest()[:16]


# ---------------------------------------------------------------- small helpers


def _int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v == v and abs(v) != float("inf"):
        return int(v)
    return None


def _to_ms(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return _int(v)
    if isinstance(v, str):
        s = v.strip()
        try:
            return int(float(s))
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(round(dt.timestamp() * 1000))
    return None


def _iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _kind_of(name: str) -> str:
    return "spec-create" if name == "spec-create" else "spec-implement"


def _head(path: Path) -> Dict[str, Optional[str]]:
    """cwd, gitBranch and version from the first lines of a transcript."""
    out: Dict[str, Optional[str]] = {"cwd": None, "gitBranch": None, "version": None}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(_HEAD_LINES), fh):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                for k in out:
                    if out[k] is None and isinstance(d.get(k), str) and d[k]:
                        out[k] = d[k]
                if all(out.values()):
                    break
    except OSError:
        pass
    return out


def _is_truncated(path: Path) -> bool:
    """The file ends mid-line with text that is not complete JSON."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
            data = fh.read()
    except OSError:
        return False
    text = data.decode("utf-8", errors="replace")
    if not text or text.endswith("\n"):
        return False
    if "\n" not in text and size > _TAIL_BYTES:
        return False
    last = text.rsplit("\n", 1)[-1]
    try:
        json.loads(last)
    except ValueError:
        return True
    return False


def _timestamps(path: Path) -> Tuple[Optional[int], Optional[int]]:
    first = last = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                ms = _to_ms(d.get("timestamp")) if isinstance(d, dict) else None
                if ms is None:
                    continue
                first = ms if first is None else min(first, ms)
                last = ms if last is None else max(last, ms)
    except OSError:
        pass
    return first, last


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _git_remote(root: Optional[str]) -> Optional[str]:
    if not root or not Path(root).is_dir():
        return None
    for args in (["remote", "get-url", "origin"], None):
        try:
            if args is None:
                r = subprocess.run(["git", "-C", root, "remote"], capture_output=True, text=True, timeout=10)
                names = r.stdout.split() if r.returncode == 0 else []
                if not names:
                    return None
                args = ["remote", "get-url", names[0]]
            r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[0]
    return None


_STACKS = (
    ("python", ("pyproject.toml", "setup.py", "requirements.txt", "Pipfile")),
    ("node", ("package.json",)),
    ("go", ("go.mod",)),
    ("rust", ("Cargo.toml",)),
    ("java", ("pom.xml", "build.gradle", "build.gradle.kts")),
    ("ruby", ("Gemfile",)),
)


def _stack_hint(root: Optional[str]) -> Optional[str]:
    if not root or not Path(root).is_dir():
        return None
    p = Path(root)
    try:
        for name, files in _STACKS:
            if any((p / f).exists() for f in files):
                return name
        if any(p.glob("*.csproj")) or any(p.glob("*.sln")):
            return "dotnet"
    except OSError:
        return None
    return None


def _get_salt() -> bytes:
    with _salt_lock:
        return store.ensure_salt()


# ---------------------------------------------------------------- buckets


def _new_cell() -> dict:
    return {"usd": None, "input": None, "output": None, "cacheRead": None, "cacheCreation": None, "agents": 0, "ms": None}


def _add(cell: dict, usd: Optional[float], usage: Optional[Mapping[str, int]], ms: Optional[int], count: int = 1) -> None:
    cell["agents"] += count
    if usd is not None:
        cell["usd"] = (cell["usd"] or 0.0) + usd
    if usage is not None:
        for k in _USAGE_KEYS:
            cell[k] = (cell[k] or 0) + usage.get(k, 0)
    if ms is not None:
        cell["ms"] = (cell["ms"] or 0) + ms


# ---------------------------------------------------------------- agents


@dataclass
class _Agent:
    agent_id: Optional[str]
    label: Any
    phase: Optional[str]
    model_hint: Optional[str]
    state: Optional[str]
    started_ms: Optional[int]
    duration_ms: Optional[int]
    tokens: Optional[int]
    transcript: Optional[Path]
    meta_path: Optional[Path]


def _build_agent(a: _Agent, workflow_name: str, pricing: dict, missing: List[dict],
                 versions: Counter, timing_from_transcript: bool) -> Tuple[dict, dict]:
    """-> (disk row, internal info with usage_by_model and the contribution to the run cost)."""
    aid = a.agent_id
    meta = _read_json(a.meta_path) if a.meta_path is not None else None
    if a.meta_path is None or not isinstance(meta, dict):
        missing.append({"agentId": aid, "what": "meta"})
        meta = {}
    label = a.label if isinstance(a.label, str) else (meta.get("description") if isinstance(meta.get("description"), str) else None)
    phase = a.phase if isinstance(a.phase, str) and a.phase else (
        meta.get("workflowPhase") if isinstance(meta.get("workflowPhase"), str) else None)
    match = roles.match_label(label, workflow_name)

    status = "missing"
    scan: Optional[dict] = None
    first_ts = last_ts = None
    if a.transcript is not None and a.transcript.is_file():
        try:
            scan = bench.scan_transcript(a.transcript)
        except OSError:
            scan = None
            status = "unparseable"
        else:
            if scan["requests"] == 0:
                scan = None
                status = "truncated" if _is_truncated(a.transcript) else "unparseable"
            else:
                status = "truncated" if _is_truncated(a.transcript) else "ok"
        v = _head(a.transcript)["version"]
        if v:
            versions[v] += 1
        if timing_from_transcript:
            first_ts, last_ts = _timestamps(a.transcript)
    if status == "missing":
        missing.append({"agentId": aid, "what": "transcript"})
    elif status == "unparseable":
        missing.append({"agentId": aid, "what": "transcript-unparseable"})
    elif status == "truncated":
        missing.append({"agentId": aid, "what": "transcript-truncated"})

    usage_by_model: Dict[str, Dict[str, int]] = scan["usage_by_model"] if scan else {}
    dominant = None
    if usage_by_model:
        dominant = max(usage_by_model, key=lambda m: (sum(usage_by_model[m].values()), m))
    hint = meta.get("model") if isinstance(meta.get("model"), str) else None
    raw_model = a.model_hint if isinstance(a.model_hint, str) and a.model_hint else hint
    model = bench.normalize_model(raw_model) if raw_model else dominant

    started = a.started_ms if a.started_ms is not None else first_ts
    ms = a.duration_ms
    if timing_from_transcript:
        ms = (last_ts - first_ts) if first_ts is not None and last_ts is not None and last_ts > first_ts else None

    usage: Optional[Dict[str, int]] = None
    usd: Optional[float] = None
    estimate: Optional[float] = None
    if scan:
        usage = dict.fromkeys(_USAGE_KEYS, 0)
        for u in usage_by_model.values():
            for k in _USAGE_KEYS:
                usage[k] += u[k]
        usd = bench._price_by_model(usage_by_model, pricing)
    elif a.tokens is not None and a.tokens >= 0 and model:
        estimate = bench.price_usage(model, {"input": a.tokens}, pricing)

    row = {
        "agentId": aid, "label": label, "role": match.role, "matched": match.matched, "task": match.task,
        "attempt": match.attempt, "round": match.round, "step": match.step, "escalatedTo": match.escalated_to,
        "model": model, "phase": phase, "state": a.state, "startedAtMs": started, "ms": ms,
        "usd": usd, "usdEstimate": estimate,
        "input": usage["input"] if usage else None, "output": usage["output"] if usage else None,
        "cacheRead": usage["cacheRead"] if usage else None, "cacheCreation": usage["cacheCreation"] if usage else None,
        "transcript": status,
    }
    if status == "truncated":
        # a cut file cannot show that a figure is really 0: report it as unknown
        for k in ("usd", "input", "output", "cacheRead", "cacheCreation"):
            if row[k] == 0:
                row[k] = None
    info = {"scan": scan, "usage_by_model": usage_by_model, "usage": usage, "match": match,
            "first_ts": first_ts, "last_ts": last_ts}
    return row, info


def _aggregate(rows: List[dict], infos: List[dict], pricing: dict) -> Dict[str, Any]:
    by_model: Dict[str, dict] = {}
    by_role: Dict[str, dict] = {}
    by_phase: Dict[str, dict] = {}
    by_task: Dict[str, dict] = {}
    for row, info in zip(rows, infos):
        contribution = row["usd"] if row["usd"] is not None else row["usdEstimate"]
        ubm = info["usage_by_model"]
        if ubm:
            target = row["model"] if row["model"] in ubm else next(iter(ubm))
            for m, u in ubm.items():
                _add(by_model.setdefault(m, _new_cell()), bench.price_usage(m, u, pricing), u, row["ms"] if m == target else None)
        else:
            _add(by_model.setdefault(row["model"] or "unknown", _new_cell()), contribution, None, row["ms"])
        for table, key in ((by_role, row["role"]), (by_phase, row["phase"]), (by_task, row["task"])):
            if key is None:
                continue
            _add(table.setdefault(key, _new_cell()), contribution, info["usage"], row["ms"])
    return {"byModel": by_model, "byRole": by_role, "byPhase": by_phase, "byTask": by_task}


def _costs(rows: List[dict], infos: List[dict], pricing: dict, session_usd: Optional[float]) -> List[dict]:
    scans = [i["scan"] for i in infos if i["scan"]]
    transcript_agents = len(scans)
    estimated_agents = sum(1 for i in infos if not i["scan"])
    unpriced = sum(1 for r in rows if r["usd"] is None and r["usdEstimate"] is None)
    total: Optional[float] = None
    if scans:
        total = bench._price_by_model(bench.aggregate_usage(scans), pricing)
        if total is None:
            known = [r["usd"] for r in rows if r["usd"] is not None]
            total = sum(known) if known else None
    estimates = [r["usdEstimate"] for r in rows if r["usd"] is None and r["usdEstimate"] is not None]
    if estimates:
        total = (total or 0.0) + sum(estimates)
    costs = [{"usd": total, "source": cost_label(transcript_agents, estimated_agents, unpriced),
              "pricingDate": bench.PRICING_DATE, "scope": "run"}]
    if session_usd is not None:
        costs.append({"usd": session_usd, "source": "claude_total", "pricingDate": bench.PRICING_DATE, "scope": "session"})
    return costs


# ---------------------------------------------------------------- the workflow-specific hook


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) == float("inf"):
        return None
    return v


def _lst(v: Any) -> list:
    return v if isinstance(v, list) else []


def _dicts(v: Any) -> List[dict]:
    return [x for x in _lst(v) if isinstance(x, dict)]


def conflict_count(result_task: Mapping, resolver_labels: Sequence[roles.LabelMatch]) -> Tuple[Optional[int], bool]:
    """S6: `(count, inferred)`. `resolves` from the result is used as is; otherwise the resolver agents
    that name the task are counted and the figure is marked inferred (1.7)."""
    given = _int(result_task.get("resolves")) if isinstance(result_task, Mapping) else None
    if given is not None:
        return given, False
    task = result_task.get("task") if isinstance(result_task, Mapping) else None
    if task is None:
        return None, True
    key = str(task)
    n = sum(1 for m in resolver_labels if m.role == "resolver" and m.task is not None and str(m.task) == key)
    return n, True


def derive_outcome(result: object) -> Tuple[Optional[str], bool]:
    """S26: `(outcome, inferred)`. `result.outcome` when present, else the rule spec-implement.js applies."""
    if not isinstance(result, Mapping):
        return None, False
    given = result.get("outcome")
    if isinstance(given, str) and given:
        return given, False
    if result.get("refused"):
        return "refused", True
    if result.get("halted"):
        return "halted", True
    for k in ("failed", "notStarted"):
        v = result.get(k)
        if isinstance(v, list) and v:
            return "blocked", True
    return "complete", True


def _split_config(value: Any) -> Optional[dict]:
    if not isinstance(value, str) or not value or value == "off":
        return None
    # the effort is the last segment; a model id may itself hold slashes
    model, sep, effort = value.rpartition("/")
    if not sep:
        model, effort = value, ""
    return {"model": model or None, "effort": effort or None}


def _count(v: Any) -> Optional[int]:
    """Length of a list; None when the source is not a list (absent is never 0)."""
    return len(v) if isinstance(v, list) else None


def _implement_metrics(result: Mapping, rows: List[dict], workflow_name: str, tracks: Mapping) -> dict:
    resolver_labels = [roles.match_label(r["label"], workflow_name) for r in rows if r["role"] == "resolver"]
    tasks: List[dict] = []
    for t in _dicts(result.get("tasks")):
        key = str(t.get("task"))
        attempts = _int(t.get("attempts"))
        esc_raw = t.get("escalations")
        escs = _lst(esc_raw)
        esc_models = ([e.get("model") if isinstance(e, dict) else e for e in escs
                       if isinstance(e, str) or (isinstance(e, dict) and isinstance(e.get("model"), str))]
                      if isinstance(esc_raw, list) else None)
        if esc_models is not None:
            esc_models = [m for m in esc_models if isinstance(m, str)]
        track = tracks.get(key) if isinstance(tracks.get(key), str) else None
        if track not in ("impl", "test"):
            roles_seen = {r["role"] for r in rows if r.get("task") is not None and str(r["task"]) == key}
            track = "test" if "test-writer" in roles_seen else ("impl" if "implementer" in roles_seen else "unknown")
        retry_esc = sum(1 for e in escs if isinstance(e, dict) and e.get("stage") == "retry")
        review = t.get("review") if isinstance(t.get("review"), dict) else {}
        resolves, inferred = conflict_count(t, resolver_labels)
        merged_ms, finished_ms = _to_ms(t.get("mergedAt")), _to_ms(t.get("finishedAt"))
        tasks.append({
            "task": t.get("task"), "title": t.get("title") if isinstance(t.get("title"), str) else None,
            "track": track, "reason": t.get("reason") if isinstance(t.get("reason"), str) else None,
            "wave": _int(t.get("wave")), "ok": t.get("ok") if isinstance(t.get("ok"), bool) else None,
            "attempts": attempts,
            "retries": max(0, attempts - 1 - retry_esc) if attempts is not None else None,
            "escalations": esc_models,
            "reviewRounds": _int(review.get("rounds")),
            "verdict": review.get("verdict") if isinstance(review.get("verdict"), str) else None,
            "resolves": resolves, "resolvesInferred": inferred,
            "fixes": sum(1 for r in rows if r["role"] == "fixer" and r["step"] == "landing" and r["task"] is not None
                         and str(r["task"]) == key),
            "agentMinutes": _num(t.get("agentMinutes")), "toMergedMinutes": _num(t.get("toMergedMinutes")),
            "riskTag": t.get("riskTag") if isinstance(t.get("riskTag"), str) else None,
            "idleBeforeMergeMs": (merged_ms - finished_ms) if merged_ms is not None and finished_ms is not None else None,
        })
    landings = [{"size": _int(l.get("size")), "merged": _count(l.get("merged")), "red": _count(l.get("red")),
                 "conflict": _count(l.get("conflict")), "bisected": bool(l.get("bisected")),
                 "minutes": _num(l.get("minutes"))} for l in _dicts(result.get("landings"))]
    full_gates = [{"afterMerges": _int(g.get("afterMerges")), "green": g.get("green") if isinstance(g.get("green"), bool) else None}
                  for g in _dicts(result.get("fullGates"))]
    fg = result.get("finalGate")
    final_gate = None
    if isinstance(fg, dict):
        fix = fg.get("fix")
        # fixAttempted: a fix agent ran; fixed: the fix made the gate green (None when no fix ran).
        green = fg.get("green") if isinstance(fg.get("green"), bool) else None
        final_gate = {"green": green, "fixAttempted": bool(fix),
                      "fixed": (green if green is not None else None) if fix else None}
    cross = [{"wave": _int(c.get("wave")),
              "tasks": len(_lst(c.get("tasks"))) if isinstance(c.get("tasks"), list) else _int(c.get("tasks")),
              "verdict": c.get("verdict") if isinstance(c.get("verdict"), str) else None,
              "blocking": sum(1 for f in _dicts(c.get("findings")) if f.get("severity") == "blocking")}
             for c in _dicts(result.get("crossTests"))]
    fixer_esc = [{"model": f.get("model") if isinstance(f.get("model"), str) else None,
                  "green": f.get("green") if isinstance(f.get("green"), bool) else None}
                 for f in _dicts(result.get("fixerEscalations"))]
    timing = result.get("timing") if isinstance(result.get("timing"), dict) else {}
    phases = [{"group": p.get("group") if isinstance(p.get("group"), str) else None, "tasks": _int(p.get("tasks")),
               "minutes": _num(p.get("minutes"))} for p in _dicts(timing.get("phases"))]
    stats = result.get("stats")
    agents = result.get("agents") if isinstance(result.get("agents"), dict) else None
    config = None
    if agents is not None:
        config = {k: c for k, c in ((k, _split_config(v)) for k, v in agents.items()) if c is not None}
    return {
        "reportSource": result.get("reportSource") if isinstance(result.get("reportSource"), str) else None,
        "taskSpanMinutes": _num(timing.get("minutes")),
        "tasks": tasks, "landings": landings, "fullGates": full_gates, "finalGate": final_gate, "crossTests": cross,
        "fixerEscalations": fixer_esc, "phases": phases,
        "stats": {k: _int(v) for k, v in stats.items()} if isinstance(stats, dict) else None,
        "agentsConfig": config,
    }


def _create_metrics(result: Mapping, rows: List[dict]) -> dict:
    review = result.get("review") if isinstance(result.get("review"), dict) else None
    cons = result.get("consistency") if isinstance(result.get("consistency"), dict) else None
    spans: Dict[str, Optional[List[int]]] = {}
    for r in rows:
        ph, s, ms = r["phase"], r["startedAtMs"], r["ms"]
        if not isinstance(ph, str):
            continue
        spans.setdefault(ph, None)
        if s is None or ms is None:
            continue
        end = s + ms
        cur = spans[ph]
        spans[ph] = [s, end] if cur is None else [min(cur[0], s), max(cur[1], end)]
    by_role: Dict[str, Dict[str, int]] = {}
    for r in rows:
        if r["model"]:
            by_role.setdefault(r["role"], Counter())[r["model"]] += 1
    config = {role: {"model": c.most_common(1)[0][0], "effort": None} for role, c in by_role.items()} or None
    rv = review or {}
    res = rv.get("result")
    return {
        "mode": result.get("mode") if isinstance(result.get("mode"), str) else None,
        "reviewResult": res if isinstance(res, str) and res else None,
        "openQuestions": _count(rv.get("openQuestions")),
        "review": None if review is None else {
            "fixed": _count(review.get("fixed")), "notFixed": _count(review.get("notFixed")),
            "escalated": _count(review.get("escalated")), "refuted": _int(review.get("refutedCount")),
            "lensesLost": _int(review.get("lensesLost"))},
        "consistency": None if cons is None else {"passes": _int(cons.get("passes")), "fixed": _count(cons.get("fixed"))},
        "phases": {p: (None if sp is None else (sp[1] - sp[0]) / 60000) for p, sp in spans.items()} or None,
        "agentsConfig": config,
    }


def _result_terms(result: Any) -> set:
    """Task titles, reasons and summaries stay local; the leak guard needs them as terms."""
    out: set = set()
    if not isinstance(result, Mapping):
        return out
    for t in _dicts(result.get("tasks")):
        for k in ("title", "reason", "summary"):
            if isinstance(t.get(k), str) and t[k]:
                out.add(t[k])
    for k in ("reason",):
        if isinstance(result.get(k), str) and result[k]:
            out.add(result[k])
    for f in _dicts(result.get("failed")):
        if isinstance(f.get("reason"), str) and f["reason"]:
            out.add(f["reason"])

    def add(v: Any) -> None:
        if isinstance(v, str) and v:
            out.add(v)

    def add_findings(v: Any) -> None:
        for f in _dicts(v):
            add(f.get("issue"))
            add(f.get("summary"))
            add(f.get("what"))
            add(f.get("question"))

    for t in _dicts(result.get("tasks")):
        rv = t.get("review")
        if isinstance(rv, dict):
            add(rv.get("summary"))
            add_findings(rv.get("findings"))
    for c in _dicts(result.get("crossTests")):
        add(c.get("summary"))
        add_findings(c.get("findings"))
    fg = result.get("finalGate")
    if isinstance(fg, dict):
        add(fg.get("summary"))
    for f in _dicts(result.get("fixerEscalations")):
        add(f.get("what"))
    authored = result.get("authored")
    if isinstance(authored, dict):
        for a in authored.values():
            if isinstance(a, dict):
                add(a.get("summary"))
    rv = result.get("review")
    if isinstance(rv, dict):
        for k in ("fixed", "notFixed", "escalated", "openQuestions", "unverified"):
            for it in _lst(rv.get(k)):
                if isinstance(it, str):
                    add(it)
                elif isinstance(it, dict):
                    for kk in ("what", "reason", "question", "issue", "summary"):
                        add(it.get(kk))
    return out


def _workflow_metrics(kind: str, workflow_name: str, result: Any, rows: List[dict], run_data: Optional[Mapping]) -> dict:
    """The spec-implement / spec-create metrics, `outcome` and `agentsConfig` (1.2, 1.5, 1.6, 1.7)."""
    out: Dict[str, Any] = {"outcome": None, "outcomeInferred": False, "agentsConfig": None}
    if kind == "spec-create":
        out.update({"mode": None, "reviewResult": None, "openQuestions": None, "review": None,
                    "consistency": None, "phases": None})
    else:
        out.update({"reportSource": None, "taskSpanMinutes": None, "tasks": None, "landings": None, "fullGates": None, "finalGate": None, "crossTests": None,
                    "fixerEscalations": None, "phases": None, "stats": None})
    if not isinstance(result, Mapping):
        return out
    if kind == "spec-create":
        out.update(_create_metrics(result, rows))
        out["outcome"] = out["reviewResult"]
        out["outcomeInferred"] = False
    else:
        out["outcome"], out["outcomeInferred"] = derive_outcome(result)
        args = run_data.get("args") if run_data else None
        tracks = args.get("tracks") if isinstance(args, dict) and isinstance(args.get("tracks"), dict) else {}
        out.update(_implement_metrics(result, rows, workflow_name, {str(k): v for k, v in tracks.items()}))
    return out


# ---------------------------------------------------------------- record


def _previous_ck_version(run_id: str) -> Optional[str]:
    data = _read_json(store.record_path(run_id))
    v = data.get("ckVersion") if isinstance(data, dict) else None
    return v if isinstance(v, str) and v else None


def _build_record(*, run_id: str, workflow_name: str, run_data: Optional[Mapping], session_dir: Path,
                  agent_dir: Path, session_jsonl: Optional[Path], agents: List[_Agent], missing: List[dict],
                  start_ms: Optional[int], wall_ms: Optional[int], status: Optional[str], pricing: dict,
                  fresh: bool, partial: bool) -> dict:
    kind = _kind_of(workflow_name)
    versions: Counter = Counter()
    rows: List[dict] = []
    infos: List[dict] = []
    for a in agents:
        row, info = _build_agent(a, workflow_name, pricing, missing, versions, partial)
        rows.append(row)
        infos.append(info)

    if partial:
        firsts = [i["first_ts"] for i in infos if i["first_ts"] is not None]
        lasts = [i["last_ts"] for i in infos if i["last_ts"] is not None]
        if firsts and lasts:
            start_ms = min(firsts)
            wall_ms = max(lasts) - min(firsts)

    head = _head(session_jsonl) if session_jsonl is not None and session_jsonl.is_file() else {}
    if head.get("version"):
        versions[head["version"]] += 1
    session_usd: Optional[float] = None
    if session_jsonl is not None and session_jsonl.is_file():
        try:
            session_usd = bench.scan_transcript(session_jsonl)["budget_used"]
        except OSError:
            session_usd = None

    args = run_data.get("args") if run_data else None
    args = args if isinstance(args, dict) else {}
    root = args.get("root") if isinstance(args.get("root"), str) and args.get("root") else head.get("cwd")
    spec = next((args[k] for k in ("spec", "feature") if isinstance(args.get(k), str) and args[k]), None)
    branch = head.get("gitBranch")
    remote = _git_remote(root)
    pseudo = pseudonym(remote, _get_salt()) if remote else None

    installed = manifest.ck_version()
    mv: Optional[str] = None
    mmtime: Optional[int] = None
    if root:
        mf = manifest.read(Path(root))
        if mf is not None and isinstance(mf.get("version"), str):
            mv = mf["version"]
            try:
                mmtime = int((Path(root) / manifest.MANIFEST_FILE).stat().st_mtime * 1000)
            except OSError:
                mmtime = None
    ck_version = tie_ck_version(mv, mmtime, installed, start_ms, fresh, _previous_ck_version(run_id))
    if ck_version is None:
        missing.append({"agentId": None, "what": "ckVersion"})

    result = run_data.get("result") if run_data else None
    if not partial and not isinstance(result, dict):
        missing.append({"agentId": None, "what": "result"})

    terms = {t for t in (root, spec, branch, remote) if isinstance(t, str) and t}
    if remote:
        terms.update(s for s in re.split(r"[/:@]", _normalise_remote(remote)) if len(s) > 2)
    terms.update(r["label"] for r in rows if isinstance(r["label"], str) and r["label"])
    terms.update(r["phase"] for r in rows if isinstance(r["phase"], str) and r["phase"])
    terms.update(_result_terms(result))

    record: Dict[str, Any] = {
        "schema": SCHEMA_LOCAL,
        "runId": run_id,
        "sessionId": session_dir.name,
        "workflow": kind,
        "workflowName": workflow_name,
        "status": status,
        "ckVersion": ck_version,
        "collectorVersion": installed,
        "claudeCodeVersion": versions.most_common(1)[0][0] if versions else None,
        "startTime": _iso(start_ms),
        "wallMs": wall_ms,
        "project": {"root": root, "spec": spec, "branch": branch, "remote": remote, "pseudonym": pseudo,
                    "stack": _stack_hint(root)},
        "projectPseudonym": pseudo,
        "terms": sorted(terms),
        "costs": _costs(rows, infos, pricing, session_usd),
        **_aggregate(rows, infos, pricing),
        "agents": rows,
        "missing": missing,
    }
    record.update(_workflow_metrics(kind, workflow_name, result, rows, run_data))
    return record


def _agent_dir_files(agent_dir: Path, aid: Optional[str]) -> Tuple[Optional[Path], Optional[Path]]:
    if not aid or not _SAFE_ID.match(aid):
        return None, None
    return agent_dir / f"agent-{aid}.jsonl", agent_dir / f"agent-{aid}.meta.json"


def _collect_file(path: Path, run_id: str, name: Optional[str], pricing: dict, fresh: bool) -> CollectOutcome:
    if name is None:
        return CollectOutcome(run_id, "skipped_non_ck", reason="run file could not be read", error="unrecognised")
    if not runfile.is_allowlisted(name):
        return CollectOutcome(run_id, "skipped_non_ck", reason="not a ck spec workflow run", error="not_allowlisted")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return CollectOutcome(run_id, "failed", reason="run file format not recognised: unreadable", error="unrecognised")
    fc = runfile.check_format(data)
    if not fc.ok:
        return CollectOutcome(run_id, "failed", reason="run file format not recognised: " + "; ".join(fc.problems),
                              error="unrecognised")
    if runfile.run_state(data) == "unfinished":
        return CollectOutcome(run_id, "unfinished", reason="run is not finished yet")

    session_dir = path.parent.parent
    agent_dir = session_dir / "subagents" / "workflows" / run_id
    session_jsonl = session_dir.parent / f"{session_dir.name}.jsonl"
    missing: List[dict] = []
    agents: List[_Agent] = []
    for entry in data["workflowProgress"]:
        if not isinstance(entry, dict):
            missing.append({"agentId": None, "what": "agent_entry"})
            continue
        if entry.get("type") != "workflow_agent":
            continue
        aid = entry.get("agentId") if isinstance(entry.get("agentId"), str) and entry.get("agentId") else None
        tx, meta = _agent_dir_files(agent_dir, aid)
        agents.append(_Agent(
            agent_id=aid, label=entry.get("label"), phase=entry.get("phaseTitle"), model_hint=entry.get("model"),
            state=entry.get("state") if isinstance(entry.get("state"), str) else None,
            started_ms=_int(entry.get("startedAt")), duration_ms=_int(entry.get("durationMs")),
            tokens=_int(entry.get("tokens")), transcript=tx, meta_path=meta,
        ))
    record = _build_record(
        run_id=run_id, workflow_name=name, run_data=data, session_dir=session_dir, agent_dir=agent_dir,
        session_jsonl=session_jsonl, agents=agents, missing=missing, start_ms=_to_ms(data.get("startTime")),
        wall_ms=_int(data.get("durationMs")), status=data.get("status") if isinstance(data.get("status"), str) else None,
        pricing=pricing, fresh=fresh, partial=False)
    return CollectOutcome(run_id, "collected", record=record)


def _collect_orphan(home: Path, run_id: str, pricing: dict, fresh: bool) -> CollectOutcome:
    not_found = CollectOutcome(run_id, "failed", reason="run not found", error="not_found")
    orphan = runfile.find_orphan_run(home, run_id)
    if orphan is None or orphan.session_jsonl is None:
        return not_found
    name = runfile.recover_workflow_name(runfile.workflow_launches(orphan.session_jsonl), run_id)
    if not runfile.is_allowlisted(name):
        return not_found
    agents: List[_Agent] = []
    for tx in sorted(orphan.agent_dir.glob("agent-*.jsonl")):
        aid = tx.name[len("agent-"):-len(".jsonl")]
        agents.append(_Agent(agent_id=aid, label=None, phase=None, model_hint=None, state=None, started_ms=None,
                             duration_ms=None, tokens=None, transcript=tx,
                             meta_path=orphan.agent_dir / f"agent-{aid}.meta.json"))
    missing = [{"agentId": None, "what": "runfile"}, {"agentId": None, "what": "result"}]
    session_dir = orphan.agent_dir.parent.parent.parent
    record = _build_record(
        run_id=run_id, workflow_name=name, run_data=None, session_dir=session_dir, agent_dir=orphan.agent_dir,
        session_jsonl=orphan.session_jsonl, agents=agents, missing=missing, start_ms=None, wall_ms=None,
        status=None, pricing=pricing, fresh=fresh, partial=True)
    return CollectOutcome(run_id, "collected", record=record)


def collect_run(home: Path, run_id: str, *, now: datetime, pricing: Optional[dict] = None,
                fresh: bool = False) -> CollectOutcome:
    """Build the local record of one run. Writing is left to `write_outcome`."""
    pricing = pricing or {}
    path = runfile.find_run_file(Path(home), run_id)
    if path is None:
        return _collect_orphan(Path(home), run_id, pricing, fresh)
    try:
        name = runfile.peek_workflow_name(path)
    except Exception:
        name = None
    return _collect_file(path, run_id, name, pricing, fresh)


def collect_all(home: Path, *, now: datetime, workers: int = 8, pricing: Optional[dict] = None) -> CollectSummary:
    """Collect every run file under the home concurrently; results come back in input order."""
    pricing = pricing or {}
    files = runfile.iter_run_files(Path(home))

    def work(p: Path) -> CollectOutcome:
        try:
            name = runfile.peek_workflow_name(p)
        except Exception:
            name = None
        if name is None or not runfile.is_allowlisted(name):
            return CollectOutcome("", "skipped_non_ck", error="unrecognised" if name is None else "not_allowlisted")
        try:
            return _collect_file(p, p.stem, name, pricing, False)
        except Exception as e:  # noqa: BLE001 - one run must never stop the others
            return CollectOutcome(p.stem, "failed", reason=f"{type(e).__name__}: {str(e)[:200]}")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        outcomes = tuple(pool.map(work, files))
    skipped = sum(1 for o in outcomes if o.kind == "skipped_non_ck")
    counts, code = summarize_outcomes([o.kind for o in outcomes if o.kind != "skipped_non_ck"], skipped)
    failures = tuple((o.run_id, o.reason or "") for o in outcomes if o.kind == "failed")
    return CollectSummary(replace(counts, failures=failures), code, outcomes)


def write_outcome_result(outcome: CollectOutcome, env: Optional[Mapping[str, str]] = None) -> Tuple[Optional[Path], OutcomeKind]:
    """Replace the record of the run; -> (path, collected | unchanged | updated). No record: (None, outcome.kind)."""
    rec = outcome.record
    if rec is None:
        return None, outcome.kind
    path = store.record_path(rec["runId"], env)
    old = _read_json(path)
    if old is None and not path.exists():
        kind = "collected"
    elif old == rec:
        return path, "unchanged"
    else:
        kind = "updated"
    store.write_private_json(path, rec)
    return path, kind


def write_outcome(outcome: CollectOutcome, env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """Replace-by-run-id (1.9); the path written, or None when the outcome holds no record."""
    return write_outcome_result(outcome, env)[0]
