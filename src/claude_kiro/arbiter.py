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
import re
import os
from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from . import worktrees as wt
from .arbiter_policy import (
    Claim,
    ClaimDecision,
    RulingRecord,
    TaskMeta,
    append_entry,
    claim_facts,
    claim_key,
    decide_claim,
    next_number,
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

        result.update({"worktree": str(path), "branch": branch, "number": f"arbiter{number}"})  # design.md OpenResult: the `ck worktree land` label
        return result


# --------------------------------------------------------------------------- settle (Task 38)


def _principles_at(root: Path, base: str, spec: str) -> Any:
    """The effective principles as they stood at the arbiter worktree's starting commit."""
    def show(rel: Path) -> Optional[str]:
        done = wt.git(root, "show", f"{base}:{rel.as_posix()}", check=False)
        return done.stdout if done.returncode == 0 else None

    project_text = show(PRINCIPLES_REL)
    plan_text = show(Path("specs") / spec / "PLAN.md")
    project = parse_principles(project_text, "project") if project_text is not None else []
    plan = parse_principles(plan_text, "plan") if plan_text is not None else None
    return effective_principles(project, plan)


def _remove_worktree(root: Path, rel: Optional[str], branch: Optional[str]) -> None:
    if rel:
        wt.git(root, "worktree", "remove", "--force", str(root / rel), check=False)
    if branch:
        wt.git(root, "branch", "-D", branch, check=False)


def settle_claim(root: Path, spec: str, key: str, raw_text: str, tier: str) -> Dict[str, Any]:
    """`SettleResult` for the arbiter's raw answer to claim KEY: validate it, route it, update the ledger and
    deal with the arbiter's worktree by route (design.md, `ck arbiter settle`)."""
    if tier not in ("normal", "risky"):
        raise ArbiterError("tier must be normal or risky")
    from .arbiter_policy import route_ruling, validate_ruling

    with ledger_lock(root, spec):
        ledger = load_ledger(ledger_path(root, spec))
        entry = ledger["claims"].get(key)
        if entry is None:
            raise ArbiterError(f"no claim {key!r} in the arbiter ledger; it was never opened")
        base = entry.get("base")
        if not base:
            raise ArbiterError(f"the ledger entry for {key!r} has no starting commit")
        effective = _principles_at(root, base, spec)
        citable = {p.id: p for p in effective.citable}
        try:
            raw: Any = json.loads(raw_text)
        except ValueError:
            raw = raw_text
        check = validate_ruling(raw, key, citable, "run")
        route = route_ruling(check, tier, effective)  # type: ignore[arg-type]

        rel, branch = entry.get("worktree"), entry.get("branch")
        if not check.valid:
            status = "failed"
        elif route.reason == "exhausted":
            status = "exhausted"
        else:
            status = entry.get("status", "open")
        for other in ledger["claims"].values():
            if other.get("worktree") == rel and other.get("branch") == branch and other.get("status") == "open":
                other["status"] = status if status in ("failed", "exhausted") else "open"
        entry["status"] = status
        if route.route == "rerun":
            # The risky re-run is a second real call on this claim (one ruling, two calls).
            entry["calls"] = int(entry.get("calls", 0)) + 1
        write_ledger(ledger_path(root, spec), ledger)

        if route.route in ("test_repair", "code", "needs_input"):
            _remove_worktree(root, rel, branch)
        elif route.route == "rerun" and rel:
            path = root / rel
            wt.git(path, "reset", "--hard", base)
            wt.git(path, "clean", "-fdq")

        principle = citable.get(check.principle) if check.principle else None
        return {
            "route": route.route,
            "reason": route.reason,
            "record": route.record,
            "principle": {"id": principle.id, "text": principle.text} if principle is not None else None,
            "problems": list(check.problems),
        }


# --------------------------------------------------------------------------- the record of rulings (Task 42)


def decisions_rel(spec: str) -> Path:
    return Path("specs") / spec / "decisions.md"


def ruling_record_from_json(raw: Any) -> RulingRecord:
    """The `RulingRecord` of the stdin object. `moved` is a list of objects, `landed` a [bool, why] pair or null."""
    if not isinstance(raw, dict):
        raise ArbiterError("the ruling record must be a JSON object")
    names = {f.name for f in fields(RulingRecord)}
    kw = {k: v for k, v in raw.items() if k in names}
    try:
        kw["moved"] = tuple(dict(m) for m in (raw.get("moved") or []))
        landed = raw.get("landed")
        kw["landed"] = None if landed is None else (bool(landed[0]), str(landed[1]))
    except (TypeError, ValueError, IndexError, KeyError):
        raise ArbiterError("moved must be a list of objects and landed a [bool, why] pair or null")
    try:
        return RulingRecord(**kw)
    except TypeError as exc:
        raise ArbiterError(f"the ruling record is missing a field: {exc}")


def record_ruling(root: Path, spec: str, into: str, record: RulingRecord, lock_wait: float) -> Dict[str, Any]:
    """`{number, commit}` for one ruling (design.md, `ck arbiter record`).

    Takes the landing lock on INTO with a bounded wait, so concurrent records serialize; appends the entry to
    `specs/<spec>/decisions.md` on INTO's checkout and commits that file alone. The first ruling creates the file.
    A lock not acquired in time, or a failed write or commit, raises and leaves the file as it was.
    """
    checkout = wt.checkout_of(root, into)
    if checkout is None:
        raise ArbiterError(f"{into} is not checked out anywhere, so decisions.md cannot be written")
    path = checkout / decisions_rel(spec)
    rel = decisions_rel(spec).as_posix()
    with wt.land_lock(root, into, wait_s=lock_wait):
        recovered = None
        if into.startswith("integrate/"):
            try:
                recovered = wt.recover_target(root, checkout, into)
            except wt.WorktreeError:
                recovered = None  # refused (14.5): leave the checkout as it is and commit decisions.md alone
        existing = path.read_text(encoding="utf-8") if path.is_file() else None
        ledger_last = int(load_ledger(ledger_path(root, spec)).get("lastEntry", 0))
        number = next_number(existing, ledger_last)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        head = existing if existing is not None else f"# Rulings: {spec}\n\n"
        text = append_entry(head, replace(record, at=record.at or stamp), number)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(text, encoding="utf-8")
            wt.git(checkout, "add", "--", rel)
            wt.git(checkout, "commit", "--only", "-m", f"arbiter: record ruling {number}", "--", rel)
            sha = wt.git(checkout, "rev-parse", "HEAD").stdout.strip()
        except BaseException:
            wt.git(checkout, "reset", "-q", "--", rel, check=False)
            if existing is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(existing, encoding="utf-8")
            raise
        ledger_warning = None
        try:
            _mark_ruled(root, spec, record, number)
        except Exception as exc:  # the entry is committed: a ledger failure is a warning, not a failed record
            ledger_warning = f"the ruling is committed but the ledger was not updated: {exc}"
    return {"number": number, "commit": sha, **({"recovered": recovered} if recovered else {}), **({"warning": ledger_warning} if ledger_warning else {})}


def _mark_ruled(root: Path, spec: str, record: RulingRecord, number: int) -> None:
    """The ledger learns the ruling: the claim (and a deadlock's per-finding aliases, which share its worktree) is
    `ruled` with this number, so a later `open` of the same claim is a repeat and the task's limits count it."""
    with ledger_lock(root, spec):
        ledger = load_ledger(ledger_path(root, spec))
        entry = ledger["claims"].get(record.claim_key)
        if entry is not None:
            rel, branch = entry.get("worktree"), entry.get("branch")
            for other in ledger["claims"].values():
                if other is entry or (rel and other.get("worktree") == rel and other.get("branch") == branch):
                    other.update({"status": "ruled", "ruling": number, "outcome": record.outcome, "reasoning": record.reasoning})
        ledger["lastEntry"] = max(int(ledger.get("lastEntry", 0)), number)
        write_ledger(ledger_path(root, spec), ledger)


# --------------------------------------------------------------------------- /spec:create review (Task 43)

_RESULT_LINE = re.compile(r"^\*\*Result:\*\*.*$", re.MULTILINE)
_DECISION_HEAD = re.compile(r"^## Needs Your Decision[ \t]*$", re.MULTILINE)
_NEXT_HEAD = re.compile(r"^## ", re.MULTILINE)


def implementation_started(checkout: Path, spec: str) -> bool:
    """Any task of the spec In Progress or Done (the guard's own rule)."""
    from .hooks._shared.spec_parser import parse_tasks

    return any(t.in_progress or t.done for t in parse_tasks(Path(checkout) / "specs" / spec / "tasks.md"))


def create_context(checkout: Path, spec: str, approved_cut: bool) -> Dict[str, Any]:
    """`ck arbiter context` for /spec:create: both principle layers, whether implementation has started,
    the unusable principles file when there is one, and `decide_cut` for the approval flag."""
    from .arbiter_policy import decide_cut

    state, project_text = read_principles(checkout / PRINCIPLES_REL)
    plan_text = _plan_text(checkout, spec)
    project = parse_principles(project_text, "project") if state == "ok" else []
    plan = parse_principles(plan_text, "plan") if plan_text is not None else None
    effective = effective_principles(project, plan)
    started = implementation_started(checkout, spec)
    return {
        "principles": [{"id": p.id, "layer": p.layer, "text": p.text} for p in effective.citable],
        "planLayer": effective.plan_layer,
        "plan": "present" if plan_text is not None else "absent",
        "implementationStarted": started,
        "missing": str(PRINCIPLES_REL) if state != "ok" else None,
        "cut": decide_cut(bool(approved_cut), started),
    }


def _subject_id(subject: Any) -> Optional[str]:
    if isinstance(subject, dict):
        value = subject.get("id")
        return str(value) if isinstance(value, (str, int)) and str(value) else None
    if isinstance(subject, (str, int)) and str(subject):
        return str(subject)
    return None


def _subject_text(subject: Any) -> str:
    if isinstance(subject, dict):
        for k in ("problem", "title", "text", "claim", "weakness"):
            if isinstance(subject.get(k), str) and subject[k].strip():
                return subject[k]
    return str(subject if not isinstance(subject, dict) else subject.get("id", ""))


def _entries_of(output: Any) -> Optional[List[Any]]:
    """The list of per-subject entries in the arbiter's output, or None when there is none to read."""
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            return None
    if isinstance(output, dict) and isinstance(output.get("rulings"), list):
        output = output["rulings"]
    return output if isinstance(output, list) else None


_DRIFT_HEAD = re.compile(r"^## Drift[ \t]*$", re.MULTILINE)
_PLACEHOLDER = re.compile(r"^\(?\s*(none|n/a|nothing)\s*\)?\.?$", re.IGNORECASE)


def _is_separator(line: str) -> bool:
    return line.strip().startswith("|") and set(line.replace("|", "").strip()) <= set("-: ")


def _is_header(line: str) -> bool:
    cells = [c.strip().lower() for c in line.strip().strip("|").split("|")]
    return line.strip().startswith("|") and bool(cells) and cells[0] == "id"


def _add_rows(text: str, head_re: "re.Pattern[str]", title: str, header: List[str], rows: List[str]) -> str:
    """Adds table rows under `## title`, creating the section when it is missing. An existing section keeps its
    rows and gets a header when it has none; a `(none)` placeholder is dropped."""
    head = head_re.search(text)
    if head is None:
        return text.rstrip("\n") + f"\n\n## {title}\n" + "\n".join(header + rows) + "\n"
    nxt = _NEXT_HEAD.search(text, head.end())
    end = nxt.start() if nxt else len(text)
    kept = [l for l in text[head.end():end].splitlines() if l.strip() and not _PLACEHOLDER.match(l.strip())]
    prose = [l for l in kept if not l.strip().startswith("|")]
    table = [l for l in kept if l.strip().startswith("|")]
    if not any(_is_header(l) for l in table):
        table = header + [l for l in table if not _is_separator(l)]
    section = "\n" + ("\n".join(prose) + "\n\n" if prose else "") + "\n".join(table + rows) + "\n"
    return text[:head.end()] + section + ("\n" if nxt else "") + text[end:]


def _append_drift(text: str, rows: List[Dict[str, Any]], decisions: List[Dict[str, Any]], result: Optional[str]) -> str:
    """review.md with the drift rows, the `requirements` items in Needs Your Decision, and the Result line.
    A section that is already there is extended, not repeated."""
    def cell(s: Any) -> str:
        return " ".join(str(s).split()).replace("|", "\\|")

    if decisions:
        lines = [
            f"| {cell(d['id'])} | {cell(d['question'])} | (a) Change PLAN.md to match; (b) leave the spec as it is | "
            f"{cell(d['conflicts'])} | arbiter drift check |"
            for d in decisions
        ]
        text = _add_rows(text, _DECISION_HEAD, "Needs Your Decision",
                         ["| ID | Question | Options | Conflicts with | Raised by |", "|---|---|---|---|---|"], lines)
    if rows:
        body = [
            f"| {cell(r['id'])} | {cell(r['action'])} | {cell(r['outcome'])} | {cell(r['principle'])} | {cell(r['reasoning'])} |"
            for r in rows
        ]
        text = _add_rows(text, _DRIFT_HEAD, "Drift",
                         ["| ID | Action | Outcome | Principle | Reasoning |", "|---|---|---|---|---|"], body)
    if result is not None:
        line = f"**Result:** {result}"
        current = _RESULT_LINE.search(text)
        if current is not None:
            # Only upgrade: `Needs work` is never replaced by a weaker result (design.md, 11.12).
            if _result_rank(current.group(0)) < _result_rank(line):
                text = text[:current.start()] + line + text[current.end():]
        else:
            text = _insert_result(text, line)
    return text


_RESULT_RANK = {"ready": 0, "ready after decisions": 1, "needs work": 2}


def _result_rank(line: str) -> int:
    """How strong a `**Result:**` line is; anything unrecognized (a template placeholder) counts as weakest."""
    # Emphasis marks are dropped and the value is matched by its start, longest name first, so an annotated value such as
    # "Needs work (2 blocking findings remain)", "Needs work." or "**Needs work**" keeps its rank.
    value = re.sub(r"[*_`]", "", line.split("**Result:**", 1)[-1]).strip().lower()
    for name in sorted(_RESULT_RANK, key=len, reverse=True):
        if value.startswith(name):
            return _RESULT_RANK[name]
    return -1


_H1 = re.compile(r"^# .*$", re.MULTILINE)
_META_LINE = re.compile(r"^\*\*(Date|Mode):\*\*.*$", re.MULTILINE)


def _insert_result(text: str, line: str) -> str:
    """`text` with the Result line placed as in the template: after Date/Mode when present, else after the H1."""
    h1 = _H1.search(text)
    if h1 is None:
        return line + "\n\n" + text
    pos = h1.end()
    nxt = _NEXT_HEAD.search(text, pos)
    limit = nxt.start() if nxt else len(text)
    for meta in _META_LINE.finditer(text, pos, limit):
        pos = meta.end()
    if pos == h1.end():
        rest = text[pos:].lstrip("\n")
        return text[:pos] + "\n\n" + line + "\n\n" + rest
    return text[:pos] + "\n" + line + text[pos:]


def _decision_rows(text: str) -> bool:
    """Whether Needs Your Decision has a data row (not the header, the separator or a placeholder)."""
    head = _DECISION_HEAD.search(text)
    if head is None:
        return False
    nxt = _NEXT_HEAD.search(text, head.end())
    body = text[head.end(): nxt.start() if nxt else len(text)]
    return any(l.strip().startswith("|") and not _is_separator(l) and not _is_header(l) for l in body.splitlines())


def _requirement_text(raw: Mapping[str, Any]) -> Tuple[str, str]:
    """(what is proposed, why) from a ruling's `requirement` object; the reasoning stands in when it is absent."""
    req = raw.get("requirement")
    reasoning = str(raw.get("reasoning", ""))
    if isinstance(req, dict) and any(isinstance(req.get(k), str) and req[k].strip() for k in ("kind", "wording", "reason")):
        kind = str(req.get("kind") or "change").strip()
        wording = str(req.get("wording") or "").strip()
        why = str(req.get("reason") or "").strip() or reasoning
        return (f"Proposed requirement ({kind}): {wording}" if wording else f"Proposed requirement ({kind})"), why
    return reasoning, reasoning


def _is_lost(r: Mapping[str, Any]) -> bool:
    """A result whose subject went unchecked for good (a stronger-look ask still has its risky re-run)."""
    return r["action"] == "lost" or (r["action"] == "unchecked" and not (r["stronger"] and r["reason"] != "exhausted"))


def _recorded_text(root: Path, spec: str, into: str) -> str:
    try:
        checkout = wt.checkout_of(root, into)
    except (wt.WorktreeError, OSError):
        return ""
    path = checkout / decisions_rel(spec) if checkout is not None else None
    try:
        return path.read_text(encoding="utf-8") if path is not None and path.is_file() else ""
    except OSError:
        return ""


def _has_ruling(recorded: str, key: str) -> bool:
    return re.search(rf"^- \*\*Claim:\*\* {re.escape(key)} - ", recorded, re.MULTILINE) is not None


def review_step(root: Path, checkout: Path, spec: str, into: str, step: str, tier: str,
                payload: Any, lock_wait: float = 120.0) -> Dict[str, Any]:
    """`ReviewResult` for one /spec:create step. Each entry is validated and routed on its own, so a malformed
    entry loses only its subject. Every applied ruling is recorded like a run's, with the commit as none."""
    from .arbiter_policy import route_create_ruling, validate_ruling

    if step not in ("refuted", "cuts", "drift"):
        raise ArbiterError("step must be refuted, cuts or drift")
    if tier not in ("normal", "risky"):
        raise ArbiterError("tier must be normal or risky")
    if not isinstance(payload, dict):
        raise ArbiterError("stdin must be a JSON object with subjects, output and lostSteps")
    subjects = payload.get("subjects") or []
    lost_steps = payload.get("lostSteps") or []
    if not isinstance(subjects, list) or not isinstance(lost_steps, list):
        raise ArbiterError("subjects and lostSteps must be lists")

    state, project_text = read_principles(checkout / PRINCIPLES_REL)
    plan_text = _plan_text(checkout, spec)
    project = parse_principles(project_text, "project") if state == "ok" else []
    plan = parse_principles(plan_text, "plan") if plan_text is not None else None
    effective = effective_principles(project, plan)
    citable = {p.id: p for p in effective.citable}
    started = implementation_started(checkout, spec)

    # A principles file that cannot be read means the arbiter was never usable: nothing is ruled (1.12).
    entries = _entries_of(payload.get("output")) if state == "ok" else None

    by_id: Dict[str, Tuple[Any, Any]] = {}
    unkeyed = 0  # entries with no usable claim/id: each loses its (unknown) finding
    if entries is not None:
        for entry in entries:
            ident = entry.get("claim", entry.get("id")) if isinstance(entry, dict) else None
            if isinstance(ident, (str, int)) and not isinstance(ident, bool) and str(ident).strip():
                if str(ident) not in by_id:
                    by_id[str(ident)] = entry
            else:
                unkeyed += 1
    # With no subjects named (the drift step), the arbiter names its own items.
    items: List[Tuple[str, Any]] = []
    for s in subjects:
        ident = _subject_id(s)
        if ident is not None:
            items.append((ident, s))
    named = bool(items)
    if not items:
        items = [(i, {"id": i}) for i in by_id]
        # Contract for the workflow (Task 84): the risky re-run passes the stronger-look ids as `subjects`. When it
        # sends the whole output instead, an item whose claim key already has a ruling in decisions.md was settled
        # at the normal tier and is not recorded or appended again.
        if tier == "risky":
            recorded = _recorded_text(root, spec, into)
            items = [(i, s) for i, s in items if not _has_ruling(recorded, f"{spec}|{step}:{i}")]

    results: List[Dict[str, Any]] = []
    if not named:
        for n in range(unkeyed):
            results.append({"id": f"entry-{n + 1}", "action": "unchecked", "reason": "invalid-ruling",
                            "stronger": False, "ruling": None})
    drift_rows: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    for ident, subject in items:
        entry = by_id.get(ident)
        key = f"{spec}|{step}:{ident}"
        if entry is None:
            results.append({"id": ident, "action": "unchecked", "reason": "no-ruling", "stronger": False, "ruling": None})
            continue
        raw = dict(entry)
        raw["claim"] = key
        check = validate_ruling(raw, key, citable, "create")
        # A drift ruling that names a requirement item changes what PLAN.md decided, whatever its outcome says (11.6).
        if step == "drift" and check.valid and check.outcome == "spec_edit" and isinstance(raw.get("requirement"), dict) and raw["requirement"]:
            check = replace(check, outcome="requirements")
        route = route_create_ruling(check, step, started)  # type: ignore[arg-type]
        action, reason = route.action, route.reason
        # A stronger-look ask is re-run once at the risky tier. When it asks again here it is not recorded
        # (the run path records it as needs-input; /spec:create has no needs-input entry), and it is lost.
        if reason == "stronger-look" and tier == "risky":
            reason = "exhausted"
        ruling_no: Optional[int] = None
        if action != "unchecked":
            principle = citable[check.principle] if check.principle else None
            changed = "nothing"
            if action == "apply":
                changed = str(entry.get("changed") or "requirements.md wording")
            record = RulingRecord(
                trigger=f"create-{step}", claim_key=key, claim_text=_subject_text(subject), task=None, tier=tier,  # type: ignore[arg-type]
                outcome=check.outcome or "spec_edit", reasoning=str(raw.get("reasoning", "")),
                principle_id=check.principle or "", principle_text=principle.text if principle else "",
                plan_layer=effective.plan_layer, changed=changed, moved=(), commit=None, landed=None,
            )
            try:
                ruling_no = int(record_ruling(root, spec, into, record, lock_wait)["number"])
            except (ArbiterError, wt.WorktreeError, OSError) as exc:
                action, reason, ruling_no = "unchecked", f"record-failed: {exc}", None
        results.append({
            "id": ident, "action": action, "reason": reason,
            "stronger": check.stronger_look, "ruling": ruling_no,
        })
        if step == "drift" and action in ("apply", "report"):
            reasoning = str(raw.get("reasoning", ""))
            if check.outcome == "requirements":
                question, why = _requirement_text(raw)
                reasoning = f"{question}. Why: {why}" if question != reasoning else reasoning
                decisions.append({"id": ident, "question": question, "conflicts": f"PLAN.md: {why}"})
            drift_rows.append({"id": ident, "action": action, "outcome": check.outcome or "",
                               "principle": check.principle or "", "reasoning": reasoning})

    lost = (not items and step != "drift") or any(_is_lost(r) for r in results)
    if payload.get("output") is None or entries is None:
        lost = True

    if step == "drift":
        review = checkout / "specs" / spec / "review.md"
        text = review.read_text(encoding="utf-8") if review.is_file() else f"# Spec Review: {spec}\n\n"
        if state != "ok":
            drift_rows.append({"id": "principles", "action": "lost", "outcome": "", "principle": "",
                               "reasoning": f"{PRINCIPLES_REL} is missing or unreadable; run `ck principles init`. The check did not run."})
        else:
            for r in results:
                if r["action"] == "unchecked" and _is_lost(r):
                    drift_rows.append({"id": r["id"], "action": "lost", "outcome": "", "principle": "",
                                       "reasoning": f"not checked ({r['reason']}); the drift check did not run for this item"})
            if lost and not drift_rows:
                drift_rows.append({"id": "drift", "action": "lost", "outcome": "", "principle": "",
                                   "reasoning": "the drift check did not run or returned nothing valid"})
        new_text = _append_drift(text, drift_rows, decisions, None)
        if lost or lost_steps:
            result = "Needs work"
        elif _decision_rows(new_text):
            result = "Ready after decisions"
        else:
            result = None
        new_text = _append_drift(new_text, [], [], result) if result else new_text
        review.parent.mkdir(parents=True, exist_ok=True)
        review.write_text(new_text, encoding="utf-8")

    return {"results": results, "lost": lost}
