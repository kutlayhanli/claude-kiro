"""A selecting task excuses a failing node only if its `-k` can actually run that node (1.1, 1.2; Task 20 decision).

The graph's TC-level `Verify -k` edges stay as they are. `-k "tc3_ and tc4_"` is about TC-3 and TC-4 but never runs a
test carrying only one of the tokens, so it must not excuse that test's failure. Pure seam tests, no mocks.
"""

import pytest

from claude_kiro.attribution import AttributionIndex, attribute_failure, could_select
from claude_kiro.failures import Failure


def failure(node, file="tests/test_x.py"):
    return Failure(node=node, file=file, kind="assertion", message="m", truncated=False, command="pytest", exc_type=None,
                   phase="call", crash_path=None, crash_line=None, detail="", missing_command=None)


@pytest.mark.parametrize("expr,node,expected", [
    ("tc3_ and tc4_", "tests/test_x.py::test_tc3_sub", False),
    ("tc3_ and tc4_", "tests/test_x.py::test_tc3_tc4_combo", True),
    ("tc3_", "tests/test_x.py::test_tc3_sub", True),
    ("tc1_ or tc3_", "tests/test_x.py::test_tc3_sub", True),
    ("tc3_", "tests/test_x.py::test_TC03_upper", True),
    ("tc3_", "tests/test_x.py::test_tc13_other", False),
    ("not tc3_", "tests/test_x.py::test_tc3_sub", False),
    ("(tc3_ or tc4_) and not slow", "tests/test_x.py::test_tc3_sub", True),
    ("tc3_ and smoke", "tests/test_x.py::test_tc3_sub", True),
    ("tc3_ and", "tests/test_x.py::test_tc1_other", True),
    ("tc3_ $bad", "tests/test_x.py::test_tc1_other", True),
])
def test_could_select(expr, node, expected):
    assert could_select(expr, node) is expected


def index(selectors, coverage=("2",), tasks=("3", "9"), **extra):
    return AttributionIndex(tc_tasks={"TC-3": tasks}, task_selectors=selectors, selected_tc_coverage={"TC-3": coverage}, **extra)


def test_a_selecting_task_that_cannot_run_the_node_does_not_excuse_it():
    idx = index({"3": ("tc3_",), "9": ("tc3_ and tc4_",)})
    a = attribute_failure(failure("tests/test_x.py::test_tc3_sub"), idx, {"3"}, set())
    assert a.tasks == ("3",) and a.by == "tc" and a.candidate and a.excused_by == ()


def test_a_test_carrying_both_tokens_stays_with_both_tasks():
    idx = index({"3": ("tc3_",), "9": ("tc3_ and tc4_",)})
    a = attribute_failure(failure("tests/test_x.py::test_tc3_tc4_combo"), idx, {"3"}, set())
    assert a.tasks == ("3", "9") and a.excused_by == ("9",)


def test_when_no_selecting_task_can_run_the_node_coverage_is_used():
    idx = index({"9": ("tc3_ and tc4_",)}, coverage=("2",), tasks=("9",))
    a = attribute_failure(failure("tests/test_x.py::test_tc3_sub"), idx, {"2"}, set())
    assert a.tasks == ("2",) and a.by == "tc" and a.candidate


def test_with_no_coverage_either_it_falls_to_the_file_and_warns():
    idx = index({"9": ("tc3_ and tc4_",)}, coverage=(), tasks=("9",),
                file_owner={"tests/test_x.py": "1"}, test_task_verifies={"1": ("5",)})
    a = attribute_failure(failure("tests/test_x.py::test_tc3_sub"), idx, {"5"}, set())
    assert a.by == "file" and a.tasks == ("5",) and any("can run" in w for w in a.warnings)


def test_a_tc_attributed_through_coverage_is_never_filtered():
    # Coverage tasks' selectors are about other TCs: TC-3 has no selecting task, so none is removed.
    idx = AttributionIndex(tc_tasks={"TC-3": ("5",)}, task_selectors={"5": ("tc5_",)})
    a = attribute_failure(failure("tests/test_x.py::test_tc3_sub"), idx, set(), set())
    assert a.tasks == ("5",) and a.excused_by == ("5",)
