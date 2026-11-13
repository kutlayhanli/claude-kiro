"""Justification rule for user stories (run-principles Story 2 and Story 3).

`justification_findings(text)` is pure: it takes the text of a requirements
document and returns one finding per user story that has no justification.

A story's justification is a `**Without it:**` line inside the story's own
section. The section runs from its `### Story N: Title` heading to the next
heading of any level. A line counts only when its content, after removing
HTML comments, the markup characters (asterisk, underscore, backtick, tilde,
hash, greater-than, colon, hyphen) and whitespace, is non-empty.
Lines inside fenced code blocks count for no story. Criteria are never
required to carry a line.
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class JustificationFinding:
    story: str
    title: str


_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*$")
_STORY = re.compile(r"^### Story (\d+): (.*)$")
_HEADING = re.compile(r"^#{1,6} ")
_MARKER = re.compile(r"^\*\*Without it:\*\*(.*)$")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_MARKUP = re.compile(r"[\s*_`~#>:\-]")


def _has_justification(line: str) -> bool:
    m = _MARKER.match(line)
    if not m:
        return False
    content = _COMMENT.sub("", m.group(1))
    return bool(_MARKUP.sub("", content))


def justification_findings(text: Optional[str]) -> List[JustificationFinding]:
    """The stories with no justification line in their own section, in document order."""
    if not text:
        return []
    # every story heading in document order, and whether each story number has a justification line
    order: List[Tuple[str, str]] = []
    has: Dict[str, bool] = {}
    current: Optional[str] = None
    fence: Optional[str] = None
    # an HTML comment, one line or many, counts for nothing: drop it before reading lines
    text = _COMMENT.sub("", text)
    for line in text.splitlines():
        if fence is not None:
            close = _FENCE_CLOSE.match(line)
            if close and close.group(1)[0] == fence[0] and len(close.group(1)) >= len(fence):
                fence = None
            continue
        opened = _FENCE_OPEN.match(line)
        if opened:
            fence = opened.group(1)
            continue
        story_heading = _STORY.match(line)
        if story_heading:
            current = story_heading.group(1)
            order.append((current, story_heading.group(2)))
            has[current] = False
        elif _HEADING.match(line):
            current = None
        elif current is not None and _has_justification(line):
            has[current] = True
    return [JustificationFinding(story=num, title=title) for num, title in order if not has[num]]
