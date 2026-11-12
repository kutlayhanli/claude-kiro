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
from typing import Any, Literal, Mapping, Optional, Sequence, Tuple

from claude_kiro.hooks._shared.spec_parser import _field, parse_tasks_text
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
        subject = "gate:" + ",".join(sorted(_test_name(f) for f in claim.failing))
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

    status = raw.get("status")
    if status not in _STATUSES:
        problems.append("status is missing or is neither ruled nor needs_stronger_look")
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


# --------------------------------------------------------------------------- spec edits (Task 41)

_TASK_HEAD = re.compile(r"^### Task ?(\d+): ")
_MARK_LINE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s*(.*)$")
_FIRST_TASK = re.compile(r"^### Task ?\d", re.MULTILINE)
_LANE_FILES = ("tasks.md", "design.md", "test-plan.md")


@dataclass(frozen=True)
class SpecTextChanges:
    status_changes: Tuple[str, ...]
    mark_changes: Tuple[str, ...]


@dataclass(frozen=True)
class SpecEditFacts:
    spec: str
    changed: Tuple[str, ...]
    status_changes: Tuple[str, ...]
    mark_changes: Tuple[str, ...]
    new_cycles: Tuple[str, ...]
    new_blocking: Tuple[str, ...]
    reserved: Tuple[str, ...]


@dataclass(frozen=True)
class SpecEditDecision:
    land: bool
    reasons: Tuple[str, ...]


@dataclass(frozen=True)
class MovedTest:
    task: str
    file: str
    test: Optional[str]
    command: str


def _task_fields(text: str) -> dict:
    """task id ("header" for the file-level Status) -> (status, acceptance criteria as (mark, text)).
    Read through the project's own parser so the two cannot drift apart."""
    out: dict = {}
    first = _FIRST_TASK.search(text)
    head = text[: first.start()] if first else text
    header_status = _field(head, "Status")
    if header_status is not None:
        out["header"] = (header_status, ())
    marks: dict = {}
    cur = None
    for line in text.splitlines():
        h = _TASK_HEAD.match(line)
        if h:
            cur = h.group(1)
            continue
        k = _MARK_LINE.match(line)
        if k and cur is not None:
            marks.setdefault(cur, []).append((k.group(1) != " ", " ".join(k.group(2).split())))
    for task in parse_tasks_text(text):
        if task.num in out:
            continue
        out[task.num] = (task.status, tuple(marks.get(task.num, ())))
    return out


def _checked_by_text(criteria: Tuple[Tuple[bool, str], ...]) -> dict:
    counts: dict = {}
    for mark, text in criteria:
        counts[text] = counts.get(text, 0) + (1 if mark else 0)
    return counts


def spec_text_changes(before: str, after: str) -> SpecTextChanges:
    """Status-field and acceptance-mark changes between two tasks.md texts, by task (8.6). A removed task that
    was not Not Started counts as a Status change. Criteria are paired by text, so inserting an unchecked
    criterion changes no mark; a flipped mark, or a checked criterion added or removed, does."""
    b, a = _task_fields(before), _task_fields(after)
    statuses = []
    marks = []
    for key in sorted(set(b) | set(a), key=lambda k: (k != "header", int(k) if k.isdigit() else 0)):
        bs, bm = b.get(key, (None, ()))
        as_, am = a.get(key, (None, ()))
        if key in b and key not in a:
            if bs not in (None, "Not Started"):
                statuses.append(f"Task {key}")
        elif key in a and key not in b:
            if as_ not in (None, "Not Started"):
                statuses.append(f"Task {key}")
        elif bs != as_:
            statuses.append(f"Task {key}" if key != "header" else "header")
        bc, ac = _checked_by_text(bm), _checked_by_text(am)
        seen = []
        for _, text in list(am) + list(bm):
            if text not in seen:
                seen.append(text)
        changed = {t for t in seen if bc.get(t, 0) != ac.get(t, 0)}
        reported = set()
        for i, (_, text) in enumerate(am):
            if text in changed and text not in reported:
                reported.add(text)
                marks.append(f"Task {key} criterion {i + 1}")
        for i, (_, text) in enumerate(bm):
            if text in changed and text not in reported:
                reported.add(text)
                marks.append(f"Task {key} criterion {i + 1}")
    return SpecTextChanges(tuple(statuses), tuple(marks))


def moved_out_of_verify(before: Mapping[str, Any], after: Mapping[str, Any]) -> list:
    """Tests in a task's selection before the edit and not after it (8.7). A selection is a sequence of
    `(file, test_or_None, command)`; a MovedTest names each one that left."""
    moved = []
    for task in sorted(before, key=lambda t: (not str(t).isdigit(), int(t) if str(t).isdigit() else 0, str(t))):
        kept = {tuple(x) for x in after.get(task, ())}
        for file, test, command in (tuple(x) for x in before[task]):
            if (file, test, command) not in kept:
                moved.append(MovedTest(str(task), file, test, command))
    return moved


def gate_scope_of_edit(before: Mapping[str, Any], after: Mapping[str, Any], done: Any) -> list:
    """Done tasks whose verification (Verify, Verified by, Verifies) differs between the two maps (8.4)."""
    return sorted((t for t in done if before.get(t) != after.get(t)),
                  key=lambda t: (not str(t).isdigit(), int(t) if str(t).isdigit() else 0, str(t)))


def decide_spec_edit(facts: SpecEditFacts) -> SpecEditDecision:
    """The second layer behind the guard: whether a spec edit may land (8.4-8.6, 9.1-9.3, 1.7)."""
    reasons = []
    allowed = {f"specs/{facts.spec}/{name}" for name in _LANE_FILES}
    for path in facts.changed:
        if path not in allowed:
            reasons.append(f"{path} is outside the spec-edit lane")
    for s in facts.status_changes:
        reasons.append(f"Status change: {s}")
    for m in facts.mark_changes:
        reasons.append(f"acceptance mark change: {m}")
    for c in facts.new_cycles:
        reasons.append(f"new cycle: {c}")
    for b in facts.new_blocking:
        reasons.append(f"new blocking finding: {b}")
    scopes = {s.strip().lower() for s in facts.reserved}
    for path in facts.changed:
        name = path.rsplit("/", 1)[-1]
        if path.lower() in scopes or name.lower() in scopes:
            reasons.append(f"{path} is reserved by the plan")
    return SpecEditDecision(not reasons, tuple(reasons))


# --------------------------------------------------------------------------- the record of rulings (Task 42)

_ENTRY_NUMBER = re.compile(r"^## Ruling (\d+):", re.MULTILINE)


@dataclass(frozen=True)
class RulingRecord:
    """One applied ruling as `decisions.md` records it (design.md). `at` is stamped by the CLI, since the
    seams here read no clock; it is the only field beyond design.md's list."""

    trigger: str
    claim_key: str
    claim_text: str
    task: Optional[str]            # None during /spec:create
    tier: Tier
    outcome: Outcome
    reasoning: str
    principle_id: str
    principle_text: str
    plan_layer: str
    changed: str                   # what changed, or "nothing"
    moved: Tuple[Mapping, ...]     # MovedTest as dicts
    commit: Optional[str]          # None renders as "none"
    landed: Optional[Tuple[bool, str]]   # None when no landing applies
    at: str = ""


def next_number(existing: Optional[str], ledger_last: int) -> int:
    """One more than the highest ruling number in `existing` or the ledger's last entry, so numbers never repeat."""
    seen = [int(n) for n in _ENTRY_NUMBER.findall(existing or "")]
    return max([int(ledger_last), *seen]) + 1


def render_entry(record: RulingRecord, number: int) -> str:
    """One `## Ruling <n>` block. Every value is collapsed onto its field line, so no value can start a heading."""
    title = f"Task {record.task} ({record.trigger})" if record.task is not None else record.trigger
    what = _collapse(record.changed)
    for m in record.moved:
        where = _collapse(m.get("test") or m.get("command") or "")
        what += f". Moved out of Verify: {_collapse(m.get('file', ''))}::{where} (Task {_collapse(m.get('task', ''))})"
    commit = _collapse(record.commit) if record.commit else "none"
    lines = [
        f"## Ruling {number}: {_collapse(title)}",
        "",
        f"- **At:** {_collapse(record.at)}",
        f"- **Trigger:** {_collapse(record.trigger)}",
        f"- **Claim:** {_collapse(record.claim_key)} - {_collapse(record.claim_text)}",
        f"- **Tier:** {_collapse(record.tier)}",
        f"- **Ruling:** {_collapse(record.outcome)} - {_collapse(record.reasoning)}",
        f"- **Principle cited:** {_collapse(record.principle_id)} - {_collapse(record.principle_text)}",
        f"- **Plan layer:** {_collapse(record.plan_layer)}",
        f"- **What changed:** {what}",
        f"- **Commit:** {commit}",
    ]
    if record.landed is not None:
        ok, why = record.landed
        lines.append("- **Landed:** yes" if ok else f"- **Landed:** no: {_collapse(why) or 'no reason given'}")
    return "\n".join(lines) + "\n"


def append_entry(existing: Optional[str], entry: RulingRecord, number: int) -> str:
    """`existing` with one entry appended; the output always starts with `existing` (10.1-10.4, 10.6).
    With no `existing`, the file starts with a bare heading (the CLI passes the spec's own heading)."""
    base = existing if existing is not None else "# Rulings\n\n"
    return base + render_entry(entry, number) + "\n"


def rulings_summary(rulings: Sequence[Mapping]) -> Mapping:
    """Counts of a run's rulings by trigger, outcome and tier (10.8)."""
    def count(field: str) -> dict:
        counts: dict = {}
        for r in rulings:
            key = str(r.get(field))
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))

    return {"total": len(rulings), "byTrigger": count("trigger"), "byOutcome": count("outcome"),
            "byTier": count("tier")}


# --------------------------------------------------------------------------- /spec:create rules (Task 43)


@dataclass(frozen=True)
class CreateRoute:
    """What `/spec:create` does with one checked ruling (design.md). `confirm` and `keep-refuted` are the
    refuted step (a finding that stands, a refutation that stands); `show-cut` and `reject-cut` are the cuts
    step (a sound cut, an unsound one); `apply` is an allowed drift wording correction; `report` goes to me and
    is never applied; `unchecked` means the check did not run or did not hold. Every route but `unchecked` is
    recorded as a numbered ruling. `reason` is "" | "invalid-ruling" | "stronger-look" | "requirements" |
    "frozen" | "not-a-create-outcome"."""

    action: Literal["confirm", "keep-refuted", "show-cut", "reject-cut", "apply", "report", "unchecked"]
    reason: str


def route_create_ruling(check: RulingCheck, step: Step, started: bool) -> CreateRoute:
    """Sends a checked `/spec:create` ruling to one action. Anything that adds, removes or changes what PLAN.md
    decided is `requirements` and is reported, never applied (11.6, 11.10). A wording correction is allowed only
    while implementation has not started (11.5, 11.7). A ruling that asks for a stronger look is `unchecked`
    with reason `stronger-look`; the caller re-runs it once at the risky tier."""
    if not check.valid:
        return CreateRoute("unchecked", "invalid-ruling")
    if check.stronger_look:
        return CreateRoute("unchecked", "stronger-look")
    if check.outcome == "requirements":
        return CreateRoute("report", "requirements")
    if check.outcome == "spec_edit":
        verdict = str(check.raw.get("verdict", "")).strip().lower() if isinstance(check.raw, Mapping) else ""
        if step == "drift":
            return CreateRoute("report", "frozen") if started else CreateRoute("apply", "")
        if step == "cuts":
            return CreateRoute("show-cut" if verdict == "sound" else "reject-cut", "")
        return CreateRoute("confirm" if verdict == "finding stands" else "keep-refuted", "")
    # code and test_repair do not exist during /spec:create: nothing is applied
    return CreateRoute("report", "not-a-create-outcome")


def decide_cut(approved: bool, started: bool) -> Literal["apply", "hold", "frozen"]:
    """`apply` only when I approved the cut and implementation has not started; `frozen` when I approved it
    after it started (the change is mine to make); else `hold` (4.3-4.5)."""
    if not approved:
        return "hold"
    return "frozen" if started else "apply"
