"""Stateful side of the arbiter (run-principles Tasks 32 and 37): lane tickets, the claim ledger, `ck arbiter open`.

A lane ticket `<checkout>/.ck/arbiter-lane/<spec>.json` holds `{"spec", "openedAt"}`. While it exists
the guard lets the /spec:create arbiter edit within its lane (design.md, 11.5, 11.7).

The ledger `<git common dir>/ck/<spec>/arbiter-ledger.json` (`ck-arbiter-ledger/1`) records every claim by its
key, with the task, status, call count, ruling number and starting commit. It is written under a per-spec file
lock, so limits and repeats survive across processes and runs. `open_claim` is the one place that reads both
principle layers, decides with the pure seams in `arbiter_policy`, starts the `arbiter<k>` worktree and updates
the ledger (design.md, `ck arbiter open`).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from . import worktrees as wt
from .arbiter_policy import (
    Claim,
    ClaimDecision,
    TaskMeta,
    claim_facts,
    claim_key,
    decide_claim,
)
from .principles import effective_principles, parse_principles

LANE_DIR_REL = Path(".ck") / "arbiter-lane"
LEDGER_SCHEMA = "ck-arbiter-ledger/1"
PRINCIPLES_REL = Path("specs") / "PRINCIPLES.md"


class ArbiterError(Exception):
    """The arbiter cannot be opened. No decision is made, so the CLI exits non-zero."""


# --------------------------------------------------------------------------- lane tickets (Task 32)


def lane_ticket_path(checkout: Path, spec: str) -> Path:
    return Path(checkout) / LANE_DIR_REL / f"{spec}.json"


def open_lane(checkout: Path, spec: str, now: Optional[datetime] = None) -> Path:
    """Write the lane ticket for SPEC in CHECKOUT and return its path."""
    path = lane_ticket_path(checkout, spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps({"spec": spec, "openedAt": stamp}), encoding="utf-8")
    return path


def close_lane(checkout: Path, spec: str) -> bool:
    """Remove the lane ticket. True when one was removed; a missing ticket is not an error."""
    path = lane_ticket_path(checkout, spec)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


# --------------------------------------------------------------------------- the claim ledger (Task 37)


def ledger_path(root: Path, spec: str) -> Path:
    return wt._common_dir(root) / "ck" / spec / "arbiter-ledger.json"


def _empty_ledger() -> Dict[str, Any]:
    return {"schema": LEDGER_SCHEMA, "nextWorktree": 1, "lastEntry": 0, "claims": {}}


def load_ledger(path: Path) -> Dict[str, Any]:
    """The ledger; a missing file is empty. Anything unreadable or malformed is an error, never a grant."""
    if not path.exists():
        return _empty_ledger()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArbiterError(f"the arbiter ledger {path} is damaged: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("claims"), dict):
        raise ArbiterError(f"the arbiter ledger {path} is damaged: not a ledger object")
    data.setdefault("nextWorktree", 1)
    data.setdefault("lastEntry", 0)
    return data


def write_ledger(path: Path, ledger: Mapping[str, Any]) -> None:
    """Replace the ledger atomically. Callers hold `ledger_lock`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(dict(ledger), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def ledger_lock(root: Path, spec: str) -> Iterator[Path]:
    """A blocking flock per spec on `<git common dir>/ck/<spec>/arbiter-ledger.lock`."""
    path = ledger_path(root, spec).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# --------------------------------------------------------------------------- reading the two layers


def read_principles(path: Path) -> Tuple[str, Optional[str]]:
    """(PrinciplesRead, text). Only `ok` may rule (P-4): missing, directory, denied and decode-error do not."""
    if not path.exists():  # a broken symlink does not exist either
        return "missing", None
    if path.is_dir():
        return "directory", None
    try:
        return "ok", path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "decode-error", None
    except OSError:
        return "denied", None


def _plan_text(checkout: Path, spec: str) -> Optional[str]:
    path = checkout / "specs" / spec / "PLAN.md"
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def claim_from_json(raw: Any, task: str) -> Claim:
    """The `Claim` of the stdin object. JSON lists become the tuples the dataclass holds."""
    if not isinstance(raw, dict):
        raise ArbiterError("the claim must be a JSON object")
    names = {f.name for f in fields(Claim)}
    kw = {k: v for k, v in raw.items() if k in names}
    try:
        findings = tuple((str(pair[0]), str(pair[1])) for pair in kw.get("findings", []) or [])
    except (TypeError, IndexError, KeyError):
        raise ArbiterError("each finding must be a [file, issue] pair")
    kw["findings"] = findings
    for k in ("failing", "tests", "cycle", "models_tried"):
        kw[k] = tuple(str(x) for x in (kw.get(k) or []))
    kw["task"] = str(task)
    try:
        return Claim(**kw)
    except TypeError as exc:
        raise ArbiterError(f"the claim is missing a required field: {exc}")


def _earlier(ledger: Mapping[str, Any], task: str) -> List[Dict[str, Any]]:
    """This task's applied rulings, by number, each once."""
    seen: Dict[int, Dict[str, Any]] = {}
    for key, entry in ledger["claims"].items():
        if str(entry.get("task")) != task or entry.get("status") != "ruled" or entry.get("ruling") is None:
            continue
        n = int(entry["ruling"])
        seen.setdefault(n, {"ruling": n, "claim": key, "outcome": entry.get("outcome"),
                            "reasoning": entry.get("reasoning")})
    return [seen[n] for n in sorted(seen)]


def _per_finding(claim: Claim) -> List[Tuple[Tuple[str, str], Claim]]:
    """Each finding of a review deadlock as a claim of its own, whose key is its identity (7.4)."""
    return [((f, i), replace(claim, findings=((f, i),))) for f, i in claim.findings]


def _split_deadlock(claim: Claim, ledger: Mapping[str, Any]) -> Tuple[Claim, bool]:
    """Findings already covered by an applied ruling are left out; (the claim with the uncovered ones, all_covered)."""
    if claim.trigger != "review-deadlock" or not claim.findings:
        return claim, False
    uncovered = []
    covered = 0
    for finding, single in _per_finding(claim):
        entry = ledger["claims"].get(claim_key(single))
        if entry is not None and entry.get("status") == "ruled":
            covered += 1
        else:
            uncovered.append(finding)
    if not covered:
        return claim, False
    if not uncovered:
        return claim, True
    return replace(claim, findings=tuple(uncovered)), False


def _start_worktree(root: Path, spec: str, into: str, number: int) -> Tuple[Path, str, str]:
    """The `arbiter<k>` worktree from INTO, on its own branch: (path, branch, starting commit)."""
    label = f"arbiter{number}"
    path = wt.task_path(root, spec, label)
    branch = wt.task_branch(spec, label)
    if path.exists() or wt.branch_exists(root, branch):
        raise ArbiterError(f"the arbiter worktree {label} already exists at {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    wt.git(root, "worktree", "add", "-b", branch, str(path), into)
    base = wt.git(path, "rev-parse", "HEAD").stdout.strip()
    return path, branch, base


def _discard_worktree(root: Path, path: Path, branch: str) -> None:
    """Undo `_start_worktree` when the ledger was not written, so the next open can use the same number."""
    wt.git(root, "worktree", "remove", "--force", str(path), check=False)
    wt.git(root, "branch", "-D", branch, check=False)


def open_claim(root: Path, spec: str, into: str, task: str, claim: Claim, task_meta: TaskMeta) -> Dict[str, Any]:
    """`OpenResult` for one claim (design.md). Reads both principle layers, decides, and when it rules starts the
    `arbiter<k>` worktree and records the claim in the ledger. Holds the spec's ledger lock for the whole call."""
    with ledger_lock(root, spec):
        ledger = load_ledger(ledger_path(root, spec))
        checkout = wt.checkout_of(root, into)
        if checkout is None:
            raise ArbiterError(f"{into} is not checked out anywhere, so its principles and plan cannot be read")
        state, project_text = read_principles(checkout / PRINCIPLES_REL)
        plan_text = _plan_text(checkout, spec)
        project = parse_principles(project_text, "project") if state == "ok" else []
        plan = parse_principles(plan_text, "plan") if plan_text is not None else None
        effective = effective_principles(project, plan)

        narrowed, all_covered = _split_deadlock(claim, ledger)
        facts = claim_facts(narrowed, ledger, state, task_meta)
        decision: ClaimDecision = decide_claim(facts)
        if all_covered and decision.action == "rule":
            decision = ClaimDecision("ask", None, "repeat-after-ruling")
        key = claim_key(narrowed)
        earlier = _earlier(ledger, str(claim.task))

        result: Dict[str, Any] = {
            "action": decision.action,
            "key": key,
            "tier": decision.tier,
            "reason": decision.reason,
            "principles": [{"id": p.id, "layer": p.layer, "text": p.text} for p in effective.citable],
            "planLayer": effective.plan_layer,
            "worktree": None,
            "branch": None,
            "number": None,
            "earlier": earlier,
            "missing": str(PRINCIPLES_REL) if state != "ok" else None,
        }
        if decision.action != "rule":
            return result

        number = int(ledger["nextWorktree"])
        path, branch, base = _start_worktree(root, spec, into, number)
        rel = wt.rel_path(root, path)
        ledger["nextWorktree"] = number + 1
        # One call is one count: the combined entry carries the call, and a deadlock's per-finding entries are
        # aliases that carry the same worktree and ruling but add no calls (claim_facts sums calls over the task).
        keys = {key: 1}
        if narrowed.trigger == "review-deadlock":
            for _, single in _per_finding(narrowed):
                keys.setdefault(claim_key(single), 0)
        for k, added in keys.items():
            prior = ledger["claims"].get(k, {})
            ledger["claims"][k] = {
                "task": str(claim.task), "status": "open", "calls": int(prior.get("calls", 0)) + added,
                "ruling": None, "base": base, "worktree": rel, "branch": branch,
            }
        try:
            write_ledger(ledger_path(root, spec), ledger)
        except BaseException:
            _discard_worktree(root, path, branch)
            raise

        result.update({"worktree": rel, "branch": branch, "number": str(number)})
        return result
