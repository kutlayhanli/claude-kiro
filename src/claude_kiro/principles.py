"""Principles: the two layers of rules a ruling may cite (run-principles Task 11).

The project layer is `specs/PRINCIPLES.md`. The plan layer is the `## Principles` section of a
PLAN.md. Everything here is pure: no file, clock or network. Callers read the files and pass the text.

Seams (design.md, Decision Seams):
- `parse_principles(text, layer)`: the top-level `-` bullets of a layer, as `Principle` records.
- `plan_section(plan_text)`: the body of `## Principles`, or None when absent or empty.
- `effective_principles(project, plan)`: what a ruling may cite, after overrides.
- `plan_may_be_decided(plan_text)`: whether a plan carries a usable principles section (1.4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional, Sequence, Tuple, List

STARTER_TEXT = """# Project Principles

Defaults every spec run upholds. A plan's `## Principles` section may add to these or override one with a reason.

- **priority-order:** requirements > design > test plan > task wiring.
- **test-intent:** a test keeps its intent; only selection and setup may move.
- **fail-closed:** fail closed on security.
- **remove-first:** remove before you optimize.
"""


@dataclass(frozen=True)
class Principle:
    id: str                        # "project:test-intent", "plan:2"
    layer: Literal["project", "plan"]
    text: str                      # the bullet with its continuation lines, whitespace collapsed
    overrides: Optional[str]       # slug of the project default it overrides
    reason: Optional[str]          # the text after **Reason:**
    reserves: Tuple[str, ...]      # scopes from a Reserves bullet


@dataclass(frozen=True)
class EffectivePrinciples:
    citable: Tuple[Principle, ...]  # what a ruling may cite
    ignored: Tuple[Principle, ...]  # overrides with no reason
    reserved: Tuple[str, ...]       # union of the reserves scopes
    plan_layer: Literal["present", "absent"]


_COMMENT = re.compile(r"<!--.*?-->", re.S)
_FENCE_OPEN = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_FENCE_CLOSE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*$")
_PRINCIPLES_HEADING = re.compile(r"^##[ \t]+Principles[ \t]*$")
_ANY_SECTION_HEADING = re.compile(r"^#{1,2}[ \t]")
_BULLET = re.compile(r"^-[ \t]+\S")
_OVERRIDES = re.compile(r"^\*\*Overrides[ \t]+([^*]+?):\*\*", re.I)
_RESERVES = re.compile(r"^\*\*Reserves[ \t]+([^*]+?):\*\*", re.I)
_SLUG = re.compile(r"^\*\*([A-Za-z0-9][A-Za-z0-9._-]*):\*\*")
_REASON = re.compile(r"\*\*Reason:\*\*(.*)$", re.I)


def _strip_comments(text: str) -> str:
    return _COMMENT.sub("", text)


def _fence_marker(line: str) -> Optional[str]:
    m = _FENCE_OPEN.match(line)
    return m.group(1) if m else None


def _closes_fence(line: str, marker: str) -> bool:
    m = _FENCE_CLOSE.match(line)
    return bool(m) and m.group(1)[0] == marker[0] and len(m.group(1)) >= len(marker)


def plan_section(plan_text: Optional[str]) -> Optional[str]:
    """Body of the first `## Principles` section outside a fence and comments; None when absent or empty."""
    if plan_text is None:
        return None
    body: List[str] = []
    in_section = False
    fence: Optional[str] = None
    for line in _strip_comments(plan_text).splitlines():
        if fence is not None:
            if _closes_fence(line, fence):
                fence = None
            if in_section:
                body.append(line)
            continue
        marker = _fence_marker(line)
        if marker is not None:
            fence = marker
            if in_section:
                body.append(line)
            continue
        if not in_section:
            if _PRINCIPLES_HEADING.match(line):
                in_section = True
            continue
        if _ANY_SECTION_HEADING.match(line):
            break
        body.append(line)
    text = "\n".join(body).strip()
    return text or None


def _bullets(body: str) -> List[str]:
    """Top-level `-` bullets of a body with their continuation lines, outside fences, whitespace collapsed."""
    items: List[List[str]] = []
    current: Optional[List[str]] = None
    fence: Optional[str] = None
    for line in body.splitlines():
        if fence is not None:
            if _closes_fence(line, fence):
                fence = None
            current = None
            continue
        marker = _fence_marker(line)
        if marker is not None:
            fence = marker
            current = None
            continue
        if _BULLET.match(line):
            current = [line[1:].strip()]
            items.append(current)
            continue
        if not line.strip():
            current = None
            continue
        if current is not None:
            current.append(line.strip())
    return [" ".join(" ".join(parts).split()) for parts in items]


def _reason_of(text: str) -> Optional[str]:
    m = _REASON.search(text)
    if not m:
        return None
    reason = m.group(1).strip()
    # A reason that is only markup or whitespace is no reason.
    if not re.sub(r"[\s*_`~]", "", reason):
        return None
    return reason


def _principle(text: str, layer: str, position: int) -> Principle:
    over = _OVERRIDES.match(text)
    res = _RESERVES.match(text)
    slug = _SLUG.match(text)
    if slug and not over and not res:
        pid = f"{layer}:{slug.group(1)}"
    else:
        pid = f"{layer}:{position}"
    overrides = over.group(1).strip().lower() if over else None
    reserves: Tuple[str, ...] = ()
    if res:
        reserves = tuple(s.strip() for s in res.group(1).split(",") if s.strip())
    return Principle(id=pid, layer=layer, text=text, overrides=overrides,
                     reason=_reason_of(text), reserves=reserves)


def parse_principles(text: Optional[str], layer: Literal["project", "plan"]) -> List[Principle]:
    """The principles of one layer. `project` takes the whole project file; `plan` takes a PLAN.md
    (its `## Principles` section only; no section gives no principles)."""
    if layer == "project":
        if text is None:
            return []
        body = _strip_comments(text)
    elif layer == "plan":
        body = plan_section(text)
        if body is None:
            return []
    else:
        raise ValueError(f"unknown layer: {layer!r}")
    return [_principle(t, layer, i) for i, t in enumerate(_bullets(body), start=1)]


def effective_principles(project: Sequence[Principle],
                         plan: Optional[Sequence[Principle]]) -> EffectivePrinciples:
    """What a ruling may cite. A plan override displaces its project default only with a reason;
    a reasonless override is ignored. Reserves scopes of the plan are unioned."""
    project_list = list(project or [])
    plan_list = list(plan or [])
    displaced = {p.overrides for p in plan_list if p.overrides and p.reason}
    ignored = tuple(p for p in plan_list if p.overrides and not p.reason)
    citable_plan = tuple(p for p in plan_list if not (p.overrides and not p.reason))
    citable_project = tuple(
        p for p in project_list
        if not (p.id.startswith("project:") and p.id.split(":", 1)[1].lower() in displaced)
    )
    reserved: List[str] = []
    for p in plan_list:
        for scope in p.reserves:
            if scope not in reserved:
                reserved.append(scope)
    return EffectivePrinciples(
        citable=citable_project + citable_plan,
        ignored=ignored,
        reserved=tuple(reserved),
        plan_layer="present" if plan_list else "absent",
    )


def plan_may_be_decided(plan_text: str) -> Tuple[bool, str]:
    """A plan may be Decided only with a real `## Principles` section holding a bullet (1.4).
    A `**Nothing to add:**` bullet is a bullet."""
    body = plan_section(plan_text)
    if body is None:
        return False, "no usable ## Principles section"
    if not _bullets(body):
        return False, "## Principles holds no bullet"
    return True, "ok"
