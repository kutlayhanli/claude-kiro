"""Pure rules for arbiter claims (run-principles Task 29).

Decides whether a claim may be ruled on, and at which tier. Hooks and the CLI gather facts;
these functions decide. Nothing here reads files, git, or the clock.

Seams (design.md, `src/claude_kiro/arbiter_policy.py`):
- `claim_key(claim)`: the claim's identity, `<task>|<subject>`.
- `claim_facts(claim, ledger, principles_read, task)`: the facts a decision needs.
- `decide_claim(facts)`: `ask` with a reason, or `rule` with a tier.
"""

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional, Tuple

from claude_kiro.principles import EffectivePrinciples

Tier = Literal["normal", "risky"]
Trigger = Literal["test-conflict", "guard-stop", "review-deadlock", "command-cycle", "gate-failure", "audit", "lint"]
Outcome = Literal["spec_edit", "test_repair", "code", "requirements"]
Step = Literal["refuted", "cuts", "drift"]
PrinciplesRead = Literal["ok", "missing", "directory", "denied", "decode-error"]

# Bounded arbiter cost (design.md non-functional rows): at most 2 calls per claim, 6 per task,
# and 3 rulings per task.
CLAIM_CALL_LIMIT = 2
TASK_CALL_LIMIT = 6
TASK_RULING_LIMIT = 3


@dataclass(frozen=True)
class Claim:
    task: str
    trigger: Trigger
    text: str
    file: str = ""
    test: str = ""
    criterion: str = ""
    findings: Tuple[Tuple[str, str], ...] = ()
    failing: Tuple[str, ...] = ()
    rule: str = ""
    subject: str = ""
    tests: Tuple[str, ...] = ()
    command: str = ""
    provider: str = ""
    cycle: Tuple[str, ...] = ()
    models_tried: Tuple[str, ...] = ()
    evidence: str = ""


@dataclass(frozen=True)
class PriorClaim:
    status: Literal["open", "failed", "exhausted", "ruled"]
    calls: int
    ruling: Optional[int]


@dataclass(frozen=True)
class TaskMeta:
    risk: Literal["safety", "normal"]
    complexity: str  # carried, never read (5.5)


@dataclass(frozen=True)
class ClaimFacts:
    key: str
    trigger: Trigger
    safety: bool
    principles_readable: bool
    prior: Optional[PriorClaim]
    task_rulings: int
    task_calls: int


@dataclass(frozen=True)
class ClaimDecision:
    action: Literal["rule", "ask"]
    tier: Optional[Tier]
    reason: str  # "" | "principles-missing" | "repeat-after-ruling" | "exhausted" | "claim-call-limit"
    #              | "task-ruling-limit" | "task-call-limit"


def _path(p: str) -> str:
    """Paths drop a leading `./`."""
    out = p.strip()
    while out.startswith("./"):
        out = out[2:]
    return out


def _collapse(s: str) -> str:
    return " ".join(str(s).split())


def _finding_identity(file: str, issue: str) -> str:
    """`<file>#<issue>`, the match `settleFindings` uses: issue lowercased, whitespace collapsed."""
    return f"{_path(file)}#{_collapse(issue).lower()}"


def _test_name(test: str) -> str:
    """Keeps what follows the last `::`, drops `[...]` parameters, collapses whitespace, lowercases."""
    name = str(test).rsplit("::", 1)[-1]
    name = re.sub(r"\[.*?\]", "", name)
    return _collapse(name).lower()


def claim_key(claim: Claim) -> str:
    """The claim's identity: `<task>|<subject>`. Evidence and claim text never count."""
    t = claim.trigger
    if t == "review-deadlock":
        subject = "review:" + ",".join(sorted({_finding_identity(f, i) for f, i in claim.findings}))
    elif t == "gate-failure":
        subject = "gate:" + ",".join(sorted(claim.failing))
    elif t == "lint":
        subject = f"lint:{claim.rule}:{claim.subject}"
    elif t == "command-cycle":
        subject = f"command:{claim.provider}:{claim.command}"
    elif claim.test:
        subject = f"test:{_path(claim.file)}::{_test_name(claim.test)}"
    else:
        subject = f"file:{_path(claim.file)}:{claim.criterion}"
    return f"{claim.task}|{subject}"


def claim_facts(claim: Claim, ledger: Mapping[str, Any], principles_read: str, task: TaskMeta) -> ClaimFacts:
    """Builds the facts for one claim. Counts this task's rulings and calls only, whatever the ledger's order."""
    key = claim_key(claim)
    entries: Mapping[str, Any] = ledger.get("claims", {}) or {}
    task_id = str(claim.task)

    prior = None
    if key in entries:
        e = entries[key]
        prior = PriorClaim(status=e["status"], calls=int(e.get("calls", 0)), ruling=e.get("ruling"))

    task_rulings = 0
    task_calls = 0
    for e in entries.values():
        if str(e.get("task")) != task_id:
            continue
        task_calls += int(e.get("calls", 0))
        if e.get("status") == "ruled":
            task_rulings += 1

    return ClaimFacts(
        key=key,
        trigger=claim.trigger,
        safety=task.risk == "safety",
        principles_readable=principles_read == "ok",
        prior=prior,
        task_rulings=task_rulings,
        task_calls=task_calls,
    )


def decide_claim(facts: ClaimFacts) -> ClaimDecision:
    """`ask` with a reason, or `rule` with a tier. Complexity is never an input."""
    if not facts.principles_readable:
        return ClaimDecision("ask", None, "principles-missing")
    prior = facts.prior
    if prior is not None:
        if prior.status == "ruled":
            return ClaimDecision("ask", None, "repeat-after-ruling")
        if prior.status == "exhausted":
            return ClaimDecision("ask", None, "exhausted")
        if prior.calls >= CLAIM_CALL_LIMIT:
            return ClaimDecision("ask", None, "claim-call-limit")
    if facts.task_calls >= TASK_CALL_LIMIT:
        return ClaimDecision("ask", None, "task-call-limit")
    if facts.task_rulings >= TASK_RULING_LIMIT:
        return ClaimDecision("ask", None, "task-ruling-limit")

    repeat = prior is not None and prior.status in ("open", "failed")
    risky = facts.safety or facts.trigger == "guard-stop" or repeat
    return ClaimDecision("rule", "risky" if risky else "normal", "")


# --------------------------------------------------------------------------- rulings (Task 30)

OUTCOMES = ("spec_edit", "test_repair", "code", "requirements")
_STATUSES = ("ruled", "needs_stronger_look")
_LANES = {"spec_edit": "spec_edit", "test_repair": "test_repair", "code": "code"}
# A plan reservation names the scope a ruling's outcome would change: `code` or `test repairs`.
_RESERVABLE = {"code": "code", "test_repair": "test repairs"}


@dataclass(frozen=True)
class RulingCheck:
    valid: bool
    stronger_look: bool
    outcome: Optional[Outcome]
    principle: Optional[str]
    reserved_by: Optional[str]
    problems: Tuple[str, ...]
    raw: Any  # kept for the needs-input entry (7.2)


@dataclass(frozen=True)
class RulingRoute:
    route: Literal["spec_edit", "test_repair", "code", "needs_input", "rerun"]
    reason: str  # "" | "invalid-ruling" | "exhausted" | "reserved" | "requirements" | "missing-requirement"
    record: bool  # whether this answer becomes a numbered ruling


def validate_ruling(raw: Any, key: str, citable: Mapping[str, Any], mode: str) -> RulingCheck:
    """Checks one arbiter answer against the validity rules (7.1, 7.2). `mode` is "run" or "create";
    both apply the same rules (11.9). `citable` maps principle ids to principles; the cited id must be one of them."""
    if mode not in ("run", "create"):
        raise ValueError(f"unknown mode: {mode!r}")
    if not isinstance(raw, dict):
        return RulingCheck(False, False, None, None, None, ("the answer is not an object",), raw)

    problems: list = []
    if raw.get("claim") != key:
        problems.append("claim is missing or is not the claim this ruling answers")

    status = raw.get("status", "ruled")
    if status not in _STATUSES:
        problems.append("status is neither ruled nor needs_stronger_look")
    stronger = status == "needs_stronger_look"

    reasoning = raw.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        problems.append("reasoning is missing or not text")

    principle = raw.get("principle")
    if not isinstance(principle, str) or not principle:
        problems.append("principle is missing or not text")
        principle = None
    elif principle not in citable:
        problems.append(f"principle {principle!r} is not a citable principle of either layer")

    outcome = None
    if not stronger:
        value = raw.get("outcome")
        if isinstance(value, str) and value in OUTCOMES:
            outcome = value
        else:
            problems.append("outcome must be exactly one of spec_edit, test_repair, code, requirements")

    reserved_by = raw.get("reservedBy")
    if reserved_by in (None, ""):
        reserved_by = None
    elif not isinstance(reserved_by, str):
        problems.append("reservedBy is not text")
        reserved_by = None

    return RulingCheck(
        valid=not problems,
        stronger_look=stronger,
        outcome=outcome,
        principle=principle,
        reserved_by=reserved_by,
        problems=tuple(problems),
        raw=raw,
    )


def _reserved(check: RulingCheck, effective: EffectivePrinciples) -> bool:
    """A reservation applies when the ruling names a reserving plan principle, or when the plan reserves the
    scope the outcome would change. Grants are never read here (1.8)."""
    if check.reserved_by is not None:
        if any(p.id == check.reserved_by and p.reserves for p in effective.citable):
            return True
    scope = _RESERVABLE.get(check.outcome or "")
    return scope is not None and any(s.lower() == scope for s in effective.reserved)


def route_ruling(check: RulingCheck, tier: Tier, effective: EffectivePrinciples) -> RulingRoute:
    """Sends a checked ruling to exactly one lane. A reservation only moves a ruling toward needs_input (1.7)."""
    if not check.valid:
        return RulingRoute("needs_input", "invalid-ruling", False)
    if check.stronger_look:
        if tier == "normal":
            return RulingRoute("rerun", "", False)  # one re-run; the two calls count as one ruling (7.7)
        return RulingRoute("needs_input", "exhausted", True)
    if _reserved(check, effective):
        return RulingRoute("needs_input", "reserved", True)
    if check.outcome == "requirements":
        req = check.raw.get("requirement") if isinstance(check.raw, dict) else None
        kind = req.get("kind") if isinstance(req, dict) else None
        return RulingRoute("needs_input", "missing-requirement" if kind == "add" else "requirements", True)
    return RulingRoute(_LANES[check.outcome], "", True)
