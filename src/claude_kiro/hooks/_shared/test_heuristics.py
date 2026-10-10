"""Language-agnostic heuristics for spotting tests being weakened.

These are deliberately simple regex counts. They do not need to be exact: the
guard asks before a suspicious edit and the gate reports evidence, so a false
positive costs one confirmation, while a missed deletion costs a silently
passing suite.
"""

import re
from pathlib import PurePosixPath
from typing import Any, Dict, List, NamedTuple

from claude_kiro.properties import map_ids

TEST_DEF = re.compile(
    r"^\s*(?:async\s+)?def\s+test\w*\s*\("  # pytest / unittest
    r"|^\s*(?:it|test|specify)(?:\.each\s*\(.*?\))?\s*\("  # jest / vitest / mocha
    r"|^\s*it\s+['\"]"  # rspec
    r"|^\s*func\s+Test\w+\s*\("  # go
    r"|#\[(?:tokio::)?test\]"  # rust
    r"|@(?:Test|ParameterizedTest)\b",  # junit
    re.MULTILINE,
)

ASSERTION = re.compile(
    r"\bassert\w*\b"
    r"|\bself\.assert\w+"
    r"|\bexpect\s*\("
    r"|\.should\b"
    r"|\bpytest\.raises\b"
    r"|\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\b"
    r"|\brequire\.\w+\s*\("
    r"|\bAssert\.\w+"
)

# Markers that switch tests off, or (.only) switch every other test off.
SKIP_MARKER = re.compile(
    r"@pytest\.mark\.(?:skip|skipif|xfail)\b"
    r"|\bpytest\.(?:skip|xfail)\s*\("
    r"|@unittest\.skip"
    r"|\b(?:it|test|describe|context)\.(?:skip|todo|only)\b"
    r"|\bx(?:it|describe|test|context)\s*\("
    r"|\bf(?:it|describe)\s*\("
    r"|\bt\.Skip\w*\s*\("
    r"|#\[ignore\]"
    r"|@(?:Disabled|Ignore)\b"
)


# Property-test example budgets (Hypothesis max_examples, fast-check numRuns).
EXAMPLE_BUDGET = re.compile(r"\b(max_examples|numRuns)\b\s*[=:]\s*(\d+)")


def _budgets(text: str) -> Dict[str, int]:
    budgets: Dict[str, int] = {}
    for name, value in EXAMPLE_BUDGET.findall(text):
        budgets[name] = max(budgets.get(name, 0), int(value))
    return budgets


class TestMetrics(NamedTuple):
    tests: int
    assertions: int
    skips: int


def measure(text: str) -> TestMetrics:
    return TestMetrics(
        tests=len(TEST_DEF.findall(text)),
        assertions=len(ASSERTION.findall(text)),
        skips=len(SKIP_MARKER.findall(text)),
    )


def weakening(old: str, new: str) -> List[str]:
    """Reasons `new` is weaker than `old`; empty when it is not."""
    before, after = measure(old), measure(new)
    reasons = []
    if after.tests < before.tests:
        reasons.append(f"removes {before.tests - after.tests} test case(s)")
    if after.assertions < before.assertions:
        reasons.append(f"removes {before.assertions - after.assertions} assertion(s)")
    if after.skips > before.skips:
        reasons.append(f"adds {after.skips - before.skips} skip/xfail/only marker(s)")
    dropped = sorted(map_ids(old) - map_ids(new), key=lambda p: int(p[2:]))
    if dropped and "PROPERTIES" in new:
        reasons.append(f"drops property {', '.join(dropped)} from PROPERTIES")
    elif dropped:
        reasons.append(f"drops property {', '.join(dropped)} (PROPERTIES map removed)")
    old_budget, new_budget = _budgets(old), _budgets(new)
    for name, value in old_budget.items():
        if name in new_budget and new_budget[name] < value:
            reasons.append(f"lowers {name} from {value} to {new_budget[name]}")
    return reasons


def is_test_path(rel_path: str, config: Dict[str, Any]) -> bool:
    """Whether a project-relative path is a test file per the config patterns."""
    path = PurePosixPath(rel_path.replace("\\", "/"))
    tests = config.get("tests", {})
    dirs = set(tests.get("dirs", []))
    if any(part in dirs for part in path.parts[:-1]):
        return True
    return any(path.match(pattern) for pattern in tests.get("files", []))
