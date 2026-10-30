"""Label grammar: map an agent label of a ck workflow script to a role (design S5).

Labels are matched anchored and exactly as given (no stripping). A label that matches no pattern is
`other` with `matched=False`; the grammar never guesses.
"""

import re
from dataclasses import asdict, dataclass
from typing import Dict, FrozenSet, Optional, Tuple

ROLES = (
    "implementer", "test-writer", "reviewer", "fixer", "resolver", "plan", "land", "gate",
    "cross-test", "report", "other",
)


@dataclass(frozen=True)
class LabelMatch:
    role: str
    matched: bool
    task: Optional[str] = None
    tasks: Tuple[str, ...] = ()
    attempt: Optional[int] = None
    round: Optional[int] = None
    step: Optional[str] = None
    escalated_to: Optional[str] = None
    wave: Optional[int] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tasks"] = list(self.tasks)
        return d


_OTHER = LabelMatch(role="other", matched=False)

# A task id, or a path-shaped id (contains "/" or starts with ".") older ck versions put in labels.
_ID = r"(?:[0-9A-Za-z_]+|[^\s]*/[^\s]*|\.[^\s]*)"
_MODEL = r"[^\s()\[\]]+"
_NUM = r"[0-9]+"

_RX = re.ASCII


def _c(pattern: str) -> "re.Pattern":
    return re.compile(pattern, _RX)


_IMPL = [
    ("task", _c(rf"task (?P<n>{_ID})(?P<t> \[test\])?"
                rf"(?: (?P<kind>retry|revise|guard) (?P<k>{_NUM})(?: \((?P<m>{_MODEL})\))?)?")),
    ("park", _c(rf"park (?P<n>{_ID})")),
    ("review", _c(rf"review (?P<n>{_ID})(?: round (?P<r>{_NUM}))?")),
    ("resolve", _c(rf"resolve (?P<n>{_ID})(?: round (?P<r>{_NUM}))?")),
    ("fix_landing", _c(rf"fix (?P<k>{_NUM}) \(Task (?P<n>{_ID}) red on landing\)(?: \[(?P<m>{_MODEL})\])?")),
    ("fix", _c(rf"fix (?P<k>{_NUM}) \((?P<what>.+)\)(?: \[(?P<m>{_MODEL})\])?")),
    ("recheck", _c(rf"re-check (?P<k>{_NUM})")),
    ("full", _c(rf"full gate @{_NUM}")),
    ("final", _c(r"final gate")),
    ("land", _c(rf"land (?P<ns>{_ID}(?: {_ID})*)")),
    ("merge", _c(rf"merge (?P<n>{_ID})")),
    ("cross", _c(rf"cross-test wave (?P<w>{_NUM})")),
    ("plan", _c(rf"plan (?P<k>{_NUM})(?: retry)?")),
    ("report", _c(r"report")),
]

_CREATE_FIXED = {
    "write requirements.md": ("implementer", "requirements"),
    "write design.md": ("implementer", "design"),
    "write implementation track": ("implementer", "impl_track"),
    "link tracks into tasks.md": ("implementer", "link"),
    "write test-plan.md": ("test-writer", "test_plan"),
    "write test track": ("test-writer", "test_track"),
    "revise spec + write review.md": ("fixer", "revise"),
    "snapshot spec before revision": ("land", "snapshot"),
    "remove pre-revision snapshot": ("land", "cleanup"),
}
_LENS = r"(?:\S|\S.*\S)"
_CREATE_RX = [
    ("review", "reviewer", _c(rf"review: {_LENS}")),
    ("refute", "reviewer", _c(rf"refute: {_LENS}")),
    ("consistency", "gate", _c(rf"consistency check: pass (?P<p>{_NUM})")),
]


def _is_path(token: str) -> bool:
    return "/" in token or token.startswith(".")


def _task(token: str) -> Optional[str]:
    return None if _is_path(token) else token


def _implement(label: str) -> LabelMatch:
    for name, rx in _IMPL:
        m = rx.fullmatch(label)
        if not m:
            continue
        g = m.groupdict()
        if name == "task":
            n = g["n"]
            role = "test-writer" if g["t"] else "implementer"
            step = "path_id" if _is_path(n) else None
            kw = dict(role=role, matched=True, task=_task(n), attempt=1, step=step)
            if g["kind"] == "retry":
                kw.update(attempt=int(g["k"]) + 1, escalated_to=g["m"])
            elif g["kind"] == "revise":
                kw.update(attempt=None, round=int(g["k"]), escalated_to=g["m"])
            elif g["kind"] == "guard":
                kw.update(attempt=None, step="guard", escalated_to=g["m"])
            return LabelMatch(**kw)
        if name == "park":
            n = g["n"]
            return LabelMatch(role="orchestrator", matched=True, task=_task(n),
                              step="path_id" if _is_path(n) else None)
        if name in ("review", "resolve"):
            n = g["n"]
            return LabelMatch(
                role="reviewer" if name == "review" else "resolver", matched=True, task=_task(n),
                round=int(g["r"]) if g["r"] else 1, step="path_id" if _is_path(n) else None,
            )
        if name == "fix_landing":
            return LabelMatch(role="fixer", matched=True, task=_task(g["n"]), attempt=int(g["k"]),
                              escalated_to=g["m"], step="landing")
        if name == "fix":
            return LabelMatch(role="fixer", matched=True, attempt=int(g["k"]),
                              escalated_to=g["m"], step="target")
        if name == "recheck":
            return LabelMatch(role="gate", matched=True, attempt=int(g["k"]), step="recheck")
        if name == "full":
            return LabelMatch(role="gate", matched=True, step="full")
        if name == "final":
            return LabelMatch(role="gate", matched=True, step="final")
        if name == "land":
            ids = tuple(g["ns"].split(" "))
            paths = any(_is_path(i) for i in ids)
            return LabelMatch(
                role="land", matched=True, task=None if paths or len(ids) != 1 else ids[0],
                tasks=() if paths else ids, step="path_id" if paths else None,
            )
        if name == "merge":
            return LabelMatch(role="land", matched=True, task=_task(g["n"]), step="merge")
        if name == "cross":
            return LabelMatch(role="cross-test", matched=True, wave=int(g["w"]))
        if name == "plan":
            return LabelMatch(role="plan", matched=True, attempt=int(g["k"]))
        if name == "report":
            return LabelMatch(role="report", matched=True)
    return _OTHER


def _create(label: str) -> LabelMatch:
    fixed = _CREATE_FIXED.get(label)
    if fixed:
        return LabelMatch(role=fixed[0], matched=True, step=fixed[1])
    for step, role, rx in _CREATE_RX:
        m = rx.fullmatch(label)
        if m:
            rnd = int(m.group("p")) if step == "consistency" else None
            return LabelMatch(role=role, matched=True, step=step, round=rnd)
    return _OTHER


def match_label(label: object, workflow: str) -> LabelMatch:
    """Parse `label` for `workflow` into a LabelMatch; `other` / `matched=False` when nothing fits."""
    if not isinstance(label, str):
        return _OTHER
    if workflow in ("spec-implement", "spec-implement-iso"):
        return _implement(label)
    if workflow == "spec-create":
        return _create(label)
    return _OTHER


def parse_label(label: object, workflow: str) -> dict:
    """Harness-contract form of `match_label`: a dict with keys role, task, attempt, round (and more)."""
    return match_label(label, workflow).to_dict()


# ---------------------------------------------------------------------------------------------- agent mode
# Agent-mode grammar (design A5, A6): the description templates of design.md "Description templates" and the
# free-form descriptions of old implement-mode runs. `match_label` above is unchanged.

SPEC_AGENT_TYPES: FrozenSet[str] = frozenset({
    "spec:implementer", "spec:test-writer", "spec:reviewer", "spec:fixer", "spec:resolver",
    "ck-implementer", "ck-test-writer", "ck-reviewer", "ck-fixer", "ck-resolver",
})
_TEST_WRITER_TYPES: FrozenSet[str] = frozenset({"spec:test-writer", "ck-test-writer"})
_TYPE_ROLE: Dict[str, str] = {
    "spec:implementer": "implementer", "ck-implementer": "implementer",
    "spec:test-writer": "test-writer", "ck-test-writer": "test-writer",
    "spec:reviewer": "reviewer", "ck-reviewer": "reviewer",
    "spec:fixer": "fixer", "ck-fixer": "fixer",
    "spec:resolver": "resolver", "ck-resolver": "resolver",
}

MAX_CONSISTENCY_PASSES = 2
CREATE_LENSES: Tuple[str, ...] = ("fidelity", "grounding", "implementability", "verification")
CREATE_STEPS: Tuple[str, ...] = (
    "requirements", "design", "test-plan", "impl-track", "test-track", "link",
    *(f"review {lens}" for lens in CREATE_LENSES),
    *(f"refute {lens}" for lens in CREATE_LENSES),
    "snapshot", "revise",
    *(f"consistency {p}" for p in range(1, MAX_CONSISTENCY_PASSES + 1)),
    "cleanup",
)
# Literal agent step -> the spec-create.js label whose role and phase it must share (design step table).
AGENT_CREATE_STEPS: Dict[str, str] = {
    "requirements": "write requirements.md",
    "design": "write design.md",
    "test-plan": "write test-plan.md",
    "impl-track": "write implementation track",
    "test-track": "write test track",
    "link": "link tracks into tasks.md",
    **{f"review {lens}": f"review: {lens}" for lens in CREATE_LENSES},
    **{f"refute {lens}": f"refute: {lens}" for lens in CREATE_LENSES},
    "snapshot": "snapshot spec before revision",
    "revise": "revise spec + write review.md",
    **{f"consistency {p}": f"consistency check: pass {p}" for p in range(1, MAX_CONSISTENCY_PASSES + 1)},
    "cleanup": "remove pre-revision snapshot",
}
CREATE_STEP_PHASES: Dict[str, str] = {
    "requirements": "Requirements",
    "design": "Design + Test Plan",
    "test-plan": "Design + Test Plan",
    "impl-track": "Task Tracks",
    "test-track": "Task Tracks",
    "link": "Link",
    **{f"review {lens}": "Adversarial Review" for lens in CREATE_LENSES},
    **{f"refute {lens}": "Adversarial Review" for lens in CREATE_LENSES},
    "snapshot": "Revise",
    "revise": "Revise",
    **{f"consistency {p}": "Consistency Check" for p in range(1, MAX_CONSISTENCY_PASSES + 1)},
    "cleanup": "Consistency Check",
}

_TASK_ID = r"[0-9A-Za-z_]+"
_COUNT = r"[1-9][0-9]*"  # <k> is a positive integer (design "Description templates")
_AGENT_FORMS = [
    ("implement", _c(rf"implement task (?P<n>{_TASK_ID})")),
    ("retry", _c(rf"implement task (?P<n>{_TASK_ID}) retry (?P<k>{_COUNT})")),
    ("guard", _c(rf"implement task (?P<n>{_TASK_ID}) guard (?P<k>{_COUNT})")),
    ("revise", _c(rf"revise task (?P<n>{_TASK_ID}) round (?P<k>{_COUNT})")),
    ("review", _c(rf"review task (?P<n>{_TASK_ID}) round (?P<k>{_COUNT})")),
    ("resolve", _c(rf"resolve task (?P<n>{_TASK_ID})")),
    ("resolve_round", _c(rf"resolve task (?P<n>{_TASK_ID}) round (?P<k>{_COUNT})")),
    ("fix", _c(rf"fix (?P<k>{_COUNT})")),
    ("fix_landing", _c(rf"fix task (?P<n>{_TASK_ID}) attempt (?P<k>{_COUNT})")),
]


def _is_test_writer(agent_type: object) -> bool:
    return isinstance(agent_type, str) and agent_type in _TEST_WRITER_TYPES


def _work_role(agent_type: object) -> str:
    return "test-writer" if _is_test_writer(agent_type) else "implementer"


def _agent_create(step: str) -> LabelMatch:
    label = AGENT_CREATE_STEPS.get(step)
    if label is None:
        return _OTHER
    match = _create(label)
    return match if match.matched else _OTHER


def _agent_form(name: str, groups: dict, agent_type: object) -> LabelMatch:
    n = groups.get("n")
    k = groups.get("k")
    if name == "implement":
        return LabelMatch(role=_work_role(agent_type), matched=True, task=n, attempt=1)
    if name == "retry":
        return LabelMatch(role=_work_role(agent_type), matched=True, task=n, attempt=int(k) + 1)
    if name == "guard":
        return LabelMatch(role=_work_role(agent_type), matched=True, task=n, step="guard")
    if name == "revise":
        return LabelMatch(role=_work_role(agent_type), matched=True, task=n, round=int(k))
    if name == "review":
        return LabelMatch(role="reviewer", matched=True, task=n, round=int(k))
    if name == "resolve":
        return LabelMatch(role="resolver", matched=True, task=n)
    if name == "resolve_round":
        return LabelMatch(role="resolver", matched=True, task=n, round=int(k))
    if name == "fix":
        return LabelMatch(role="fixer", matched=True, attempt=int(k), step="target")
    return LabelMatch(role="fixer", matched=True, task=n, attempt=int(k), step="landing")


def match_agent(rest: object, agent_type: object) -> LabelMatch:
    """A5: the description-template grammar (text after the marker), anchored and exact.

    The verb sets the role family; `implement`/`revise` give test-writer for a test-writer type and implementer
    otherwise. Types outside SPEC_AGENT_TYPES take their role from the verb alone. Anything else is `other`.
    """
    if not isinstance(rest, str):
        return _OTHER
    if rest.startswith("create "):
        return _agent_create(rest[len("create "):])
    for name, rx in _AGENT_FORMS:
        found = rx.fullmatch(rest)
        if found:
            try:
                return _agent_form(name, found.groupdict(), agent_type)
            except ValueError:  # a counter too long for int(); never a guessed role
                return _OTHER
    return _OTHER


@dataclass(frozen=True)
class LegacyMatch:
    role: str
    matched: bool
    spec: Optional[str] = None
    task: Optional[str] = None


_LEGACY_FORMS = [
    ("implement", _c(rf"implement (?P<spec>[^\s:]+): task (?P<n>{_TASK_ID})(?:[ :].*)?")),
    ("fix", _c(rf"fix (?P<spec>[^\s:]+): (?:task (?P<n>{_TASK_ID})(?:[ :].*)?|.*)")),
    ("revise", _c(rf"revise (?P<spec>[^\s:]+) task (?P<n>{_TASK_ID})(?:[ :].*)?")),
    ("review", _c(rf"review task (?P<n>{_TASK_ID})(?:[ :].*)?")),
]


def match_legacy(description: object, agent_type: object) -> LegacyMatch:
    """A6: free-form descriptions of old implement-mode runs (`implement <spec>: task <N>` and friends).

    The role comes from the type and is refined by the verb. `spec` is the token before `:` (or after
    `revise `), None when absent. Unparseable: the type's role, `matched=False`, no spec.
    """
    if isinstance(description, str):
        for verb, rx in _LEGACY_FORMS:
            found = rx.fullmatch(description)
            if not found:
                continue
            groups = found.groupdict()
            spec, task = groups.get("spec"), groups.get("n")
            if verb == "implement" or verb == "revise":
                role = _work_role(agent_type)
            elif verb == "fix":
                role = "fixer"
            else:
                role = "reviewer"
            return LegacyMatch(role=role, matched=True, spec=spec, task=task)
    role = _TYPE_ROLE.get(agent_type, "other") if isinstance(agent_type, str) else "other"
    return LegacyMatch(role=role, matched=False)
