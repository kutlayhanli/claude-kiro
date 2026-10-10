"""Correctness properties: the P-n rows of test-plan.md and the PROPERTIES maps in test files.

test-plan.md has a `## Properties` section with one row per universally
quantified criterion:

    | P-3 | 9.12 | for any amount not in the price sheet ... | "$x", "x USD", "x to y" | held |

Each property test file declares which properties it checks, by test name,
in a module-level map (any language; the gate reads it as text):

    PROPERTIES = {"P-3": "test_unquoted_amount_holds", "P-4": ["test_a", "test_b"]}
    export const PROPERTIES = { 'P-3': 'unquoted amount holds' }
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Set

PROPERTY_ID = re.compile(r"\bP-\d+\b")
_SECTION = re.compile(r"^##\s+Properties\b.*?$(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
_ROW_ID = re.compile(r"^\s*(?:\|\s*|[-*]\s+)\**`?(P-\d+)`?\**\s*(?:\||:|\s)", re.MULTILINE)
_MAP_START = re.compile(r"\bPROPERTIES\b[^=\n]*=")
_KEY = re.compile(r"""["'`]?(P-\d+)["'`]?\s*:""")
_STRING = re.compile(r""""((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'|`([^`\n]*)`""")


def plan_properties(test_plan: Path) -> Optional[List[str]]:
    """P-n ids of test-plan.md's ## Properties section, in order.

    None when the file or the section is missing (specs written before
    properties existed); [] when the section exists but lists none.
    """
    try:
        text = test_plan.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _SECTION.search(text)
    if not match:
        return None
    return list(dict.fromkeys(_ROW_ID.findall(match.group(1))))


def _map_body(text: str) -> Optional[tuple]:
    """(start, end) of the braces of the PROPERTIES map literal."""
    start = _MAP_START.search(text)
    if not start:
        return None
    open_at = text.find("{", start.end())
    if open_at < 0:
        return None
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return open_at, i + 1
    return None


def map_entries(text: str) -> Dict[str, List[str]]:
    """{"P-3": ["test_name", ...]} from a file's PROPERTIES map; {} if it has none."""
    span = _map_body(text)
    if not span:
        return {}
    body = text[span[0] + 1 : span[1] - 1]
    keys = list(_KEY.finditer(body))
    entries: Dict[str, List[str]] = {}
    for i, key in enumerate(keys):
        value = body[key.end() : keys[i + 1].start() if i + 1 < len(keys) else len(body)]
        names = [next(g for g in m.groups() if g is not None) for m in _STRING.finditer(value)]
        entries.setdefault(key.group(1), []).extend(name for name in names if name)
    return entries


def map_ids(text: str) -> Set[str]:
    return set(map_entries(text))


def covered(text: str) -> Dict[str, List[str]]:
    """{P-id: [test names the map gives that do not appear elsewhere in the file]}.

    A property is covered by this file when its list is empty.
    """
    span = _map_body(text)
    if not span:
        return {}
    rest = text[: span[0]] + text[span[1] :]
    return {pid: [n for n in names if n not in rest] for pid, names in map_entries(text).items()}
