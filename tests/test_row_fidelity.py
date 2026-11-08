"""Row fidelity (Story 8): TC-69, TC-70, TC-71a, TC-72, TC-73a, TC-74.

Every case runs the real CLI (`ck lint demo --fidelity --task 1 --json`, or `ck gate demo --task 1 --json` for the
`ongate` cases) on real spec and test files in a real git repo. Nothing of the project's own code is mocked.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

import unattended_helpers as h

HEADER = "| ID | Criteria | Level | Scenario | Real boundary | File |\n|----|----------|-------|----------|---------------|------|\n"
TEST_FILE = "tests/test_calc_tc.py"


def row(n: int, scenario: str) -> str:
    return f"| TC-{n} | 1.1 | int | {scenario} | python | {TEST_FILE} |\n"


DESIGN = """# Design: Demo

## Data Models

| Model | Field | Type | Values |
|---|---|---|---|
| Order | side | `Side` | BUY, SELL |
| Order | qty | int | any |

```python
class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"
```
"""

DESIGN_NO_ENUM = "# Design: Demo\n\n## Components\n\n- `calc.py`: add and subtract.\n"


def fid_project(tmp_path: Path, *, rows: str, tests: str, design: str = DESIGN) -> Path:
    """The attribution demo repo with its test plan, design and task 1's test file replaced, all committed."""
    root = h.attr_project(tmp_path)
    (root / "specs/demo/test-plan.md").write_text("# Test Plan: Demo\n\n" + HEADER + rows)
    (root / "specs/demo/design.md").write_text(design)
    (root / TEST_FILE).write_text("from datetime import date\n\n\ndef make_order(side, qty=1):\n"
                                  "    return {\"side\": side, \"qty\": qty}\n\n\n" + tests)
    h.commit_all(root, "fidelity fixtures")
    return root


def fidelity(root: Path) -> Dict[str, Any]:
    code, obj, err = h.ck_json(root, "lint", "demo", "--fidelity", "--task", "1", "--json")
    assert obj is not None, f"lint --fidelity printed no JSON (exit {code}): {err}"
    obj["_code"] = code
    for key in ("checked", "mismatches", "unparsed", "unmatched"):
        assert key in obj, f"{key} missing from {sorted(obj)}"
    return obj


def for_test(items: List[Any], name: str) -> List[Any]:
    return [m for m in items if name in json.dumps(m)]


def num(v: Any) -> float:
    return float(str(v).strip("'\""))


def digests(root: Path) -> Dict[str, str]:
    files = [root / "specs/demo/test-plan.md", root / "specs/demo/design.md"] + sorted((root / "tests").glob("*.py"))
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


THRESHOLD_AND_SIDE_ROWS = row(4, "The fee threshold is 4.50 and an order carries a side")
THRESHOLD_AND_SIDE_TESTS = '''def test_tc4_fee_threshold():
    fee_threshold = 4.15
    assert fee_threshold > 0


def test_tc4_order_side():
    order = make_order(side="Buy")
    assert order["side"] == "Buy"
'''


# --- TC-69 ---------------------------------------------------------------------------


def test_tc69_threshold_and_enum_mismatches(tmp_path):
    root = fid_project(tmp_path, rows=THRESHOLD_AND_SIDE_ROWS, tests=THRESHOLD_AND_SIDE_TESTS)
    res = fidelity(root)
    assert res["_code"] == 1
    thr = for_test(res["mismatches"], "test_tc4_fee_threshold")
    assert len(thr) == 1, res["mismatches"]
    assert num(thr[0]["value"]) == 4.15 and num(thr[0]["expected"]) == 4.50
    enum = for_test(res["mismatches"], "test_tc4_order_side")
    assert enum, res["mismatches"]
    assert str(enum[0]["value"]).strip("'\"") == "Buy" and sorted(enum[0]["allowed"]) == ["BUY", "SELL"]


def test_tc69_ongate_row_fidelity_failures(tmp_path):
    root = fid_project(tmp_path, rows=THRESHOLD_AND_SIDE_ROWS, tests=THRESHOLD_AND_SIDE_TESTS)
    code, obj, err = h.ck_json(root, "gate", "demo", "--task", "1", "--json")
    assert obj is not None, f"gate printed no JSON (exit {code}): {err}"
    assert code != 0
    fid = [f for f in obj["failures"] if f.get("check") == "row-fidelity"]
    assert len(fid) >= 2, obj["failures"]
    text = json.dumps(fid)
    assert "4.15" in text and "Buy" in text


# --- TC-70 ---------------------------------------------------------------------------


def test_tc70_date_and_range_upper_bound(tmp_path):
    rows = (row(6, "Given the cutoff date 2026-01-31 an order is closed") +
            row(7, "The fee stays in the range 4.15 to 4.50"))
    tests = '''def test_tc6_cutoff_date():
    opened = date(2026, 1, 30)
    assert opened.day == 30


def test_tc7_fee_range():
    fee = 4.0
    assert fee <= 4.15
'''
    root = fid_project(tmp_path, rows=rows, tests=tests, design=DESIGN_NO_ENUM)
    res = fidelity(root)
    dates = for_test(res["mismatches"], "test_tc6_cutoff_date")
    assert dates and "2026-01-30" in json.dumps(dates[0]) and "2026-01-31" in json.dumps(dates[0])
    rng = for_test(res["mismatches"], "test_tc7_fee_range")
    assert rng and num(rng[0]["value"]) == 4.15 and num(rng[0]["expected"]) == 4.50


# --- TC-71a --------------------------------------------------------------------------


def test_tc71_runtime_value_is_unparsed_checked_are_not(tmp_path):
    rows = row(4, "The fee threshold is 4.50") + row(8, "The fee threshold is 2.00")
    tests = '''def test_tc4_fee_threshold():
    fee_threshold = 4.50
    assert fee_threshold > 0


def test_tc8_computed_fee():
    base = 2.0
    fee_threshold = base * 1.1
    assert fee_threshold > 0
'''
    root = fid_project(tmp_path, rows=rows, tests=tests, design=DESIGN_NO_ENUM)
    res = fidelity(root)
    assert for_test(res["unparsed"], "1.1"), res
    assert not for_test(res["unparsed"], "test_tc4_fee_threshold")
    assert not for_test(res["mismatches"], "test_tc4_fee_threshold")
    assert res["checked"], "the matching 4.50 is checked"


# --- TC-72 ---------------------------------------------------------------------------


def test_tc72_no_tc_id_and_row_less_tc_are_unmatched(tmp_path):
    tests = '''def test_plain_name_without_id():
    assert make_order(side="BUY")["side"] == "BUY"


def test_tc77_has_no_row():
    assert make_order(side="SELL")["side"] == "SELL"


def test_tc4_has_a_row():
    assert make_order(side="BUY")["qty"] == 1
'''
    root = fid_project(tmp_path, rows=row(4, "An order carries a side"), tests=tests)
    res = fidelity(root)
    text = json.dumps(res["unmatched"])
    assert "test_plain_name_without_id" in text, res["unmatched"]
    assert "test_tc77_has_no_row" in text, res["unmatched"]
    assert "test_tc4_has_a_row" not in text


# --- TC-73a --------------------------------------------------------------------------


def test_tc73_everything_matches_all_lists_empty(tmp_path):
    tests = '''def test_tc4_fee_threshold():
    fee_threshold = 4.50
    assert fee_threshold > 0


def test_tc4_order_side():
    order = make_order(side="BUY")
    assert order["side"] == "BUY"
'''
    root = fid_project(tmp_path, rows=THRESHOLD_AND_SIDE_ROWS, tests=tests)
    res = fidelity(root)
    assert res["_code"] == 0
    assert res["mismatches"] == [] and res["unparsed"] == [] and list(res["unmatched"]) == []
    assert res["checked"], "values were extracted and checked"


# --- TC-74 ---------------------------------------------------------------------------


def test_tc74_lint_leaves_files_and_git_clean(tmp_path):
    root = fid_project(tmp_path, rows=THRESHOLD_AND_SIDE_ROWS, tests=THRESHOLD_AND_SIDE_TESTS)
    before = digests(root)
    assert h.raw(root, "status", "--porcelain").stdout.strip() == ""
    res = fidelity(root)
    assert res["mismatches"], "the check really ran on the mismatching fixtures"
    assert digests(root) == before
    assert h.raw(root, "status", "--porcelain").stdout.strip() == ""


def test_tc74_ongate_files_and_git_clean_after_gate(tmp_path):
    root = fid_project(tmp_path, rows=THRESHOLD_AND_SIDE_ROWS, tests=THRESHOLD_AND_SIDE_TESTS)
    before = digests(root)
    code, obj, err = h.ck_json(root, "gate", "demo", "--task", "1", "--json")
    assert obj is not None, f"gate printed no JSON (exit {code}): {err}"
    assert any(f.get("check") == "row-fidelity" for f in obj["failures"]), "the fidelity check ran in the gate"
    assert digests(root) == before
    assert h.raw(root, "status", "--porcelain").stdout.strip() == ""
