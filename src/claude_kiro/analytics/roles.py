"""Label grammar: map an agent label of a ck workflow script to a role (design S5).

Labels are matched anchored and exactly as given (no stripping). A label that matches no pattern is
`other` with `matched=False`; the grammar never guesses.
"""

import re
from dataclasses import asdict, dataclass
from typing import Optional, Tuple

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
                rf"(?: (?P<kind>retry|revise) (?P<k>{_NUM})(?: \((?P<m>{_MODEL})\))?)?")),
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
            return LabelMatch(**kw)
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
