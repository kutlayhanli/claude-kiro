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

    # Calls add up over the task's entries; a ruling counts once by its number, since a deadlock's
    # per-finding entries share the ruling of its combined entry (arbiter.open_claim, settle).
    ruling_ids = set()
    task_calls = 0
    for entry_key, e in entries.items():
        if str(e.get("task")) != task_id:
            continue
        task_calls += int(e.get("calls", 0))
        if e.get("status") == "ruled":
            ruling_ids.add(int(e["ruling"]) if e.get("ruling") is not None else ("entry", entry_key))
    task_rulings = len(ruling_ids)

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
