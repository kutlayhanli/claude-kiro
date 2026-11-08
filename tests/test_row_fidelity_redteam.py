"""Red team for Story 8's SHALL NOT clauses (8.3, 8.6), written from the requirements only.

8.3: values the script checked SHALL NOT be sent to the reviewer; only unparsed items and unmatched tests go.
8.6: the check SHALL NOT modify the test plan, the design or the tests.

Every case runs the real CLI (`ck lint demo --fidelity --task 1 --json`, or `ck gate demo --task 1 --json` for the
`ongate` cases) in a subprocess on real files in a real git project. Output shapes are only read where the spec
names them (`mismatches`, `unparsed`, `unmatched`, a test's name inside an item); the 8.3 checks use distinctive
test names and literals, so they hold whatever the item shape is.
"""

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Union

import pytest

from conftest import git

CK = [sys.executable, "-m", "claude_kiro.cli"]
TEST_FILE = "tests/test_calc_tc.py"

DESIGN = """# Design: Demo

## Data Models

```python
class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"
```
"""

TASKS = f"""# Implementation Tasks: Demo

**Status:** In Progress

## Task Breakdown

### Task 1: Calc tests
**Status:** Not Started
**Track:** test
**Requirements:** 1.1
**Description:** tests
**Files:**
- `{TEST_FILE}` - calc tests
**Verify:** `python3 --version`
**Verifies:** Task 2

**Acceptance:**
- [ ] done

**Dependencies:** None

---

### Task 2: Calc
**Status:** Not Started
**Track:** impl
**Requirements:** 1.1
**Files:**
- `src/calc.py` - calc
**Verify:** `python3 --version`
**Verified by:** Task 1

**Dependencies:** Task 1 (its tests are the oracle)

## Parallel Groups
Wave 1: Task 1
Wave 2: Task 2
"""

PLAN_HEAD = "# Test Plan: Demo\n\n| ID | Req | Type | Scenario | Notes | File |\n|---|---|---|---|---|---|\n"


def row(tc: int, scenario: str) -> str:
    return f"| TC-{tc} | 1.1 | int-cli | {scenario} | real | `{TEST_FILE}` |\n"


ROW4 = row(4, "Given a score threshold 4.50. Then the check passes.")
CHECKED = "def test_tc4_checked_alpha():\n    assert score() >= 4.50\n"
RUNTIME = "def test_tc5_runtime_beta(base):\n    assert score() == base * 1.1\n"
ROW5 = row(5, "Given a runtime base. Then the score is derived.")
MISMATCH = "def test_tc4_wrong_gamma():\n    assert score() >= 4.15\n"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def make(tmp_path: Path, test_src: Union[str, bytes], rows: str = ROW4, design: str = DESIGN, plan_link: bool = False) -> Path:
    root = tmp_path / "proj"
    spec = root / "specs" / "demo"
    (root / "tests").mkdir(parents=True)
    spec.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    (spec / "requirements.md").write_text("# Feature: Demo\n- 1.1 THE SYSTEM SHALL demo\n")
    (spec / "tasks.md").write_text(TASKS)
    (spec / "design.md").write_text(design)
    plan = PLAN_HEAD + rows
    if plan_link:
        (root / "elsewhere").mkdir()
        (root / "elsewhere" / "plan-real.md").write_text(plan)
        (spec / "test-plan.md").symlink_to("../../elsewhere/plan-real.md")
    else:
        (spec / "test-plan.md").write_text(plan)
    data = test_src.encode() if isinstance(test_src, str) else test_src
    (root / TEST_FILE).write_bytes(data)
    (root / "specs" / "ck.json").write_text(json.dumps({"verify": ["python3 --version"], "verify_timeout": 60}))
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def run_ck(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([*CK, *args], cwd=root, capture_output=True, text=True, timeout=300)


def fid(root: Path) -> Dict:
    out = run_ck(root, "lint", "demo", "--fidelity", "--task", "1", "--json")
    assert out.returncode in (0, 1), f"`ck lint --fidelity` did not run (exit {out.returncode}): {out.stdout}{out.stderr}"
    assert "Traceback" not in out.stderr, out.stderr
    data = json.loads(out.stdout)
    for key in ("checked", "mismatches", "unparsed", "unmatched"):
        assert key in data, data
    return data


def gate(root: Path) -> Dict:
    out = run_ck(root, "gate", "demo", "--task", "1", "--json")
    assert "Traceback" not in out.stderr, out.stderr
    return json.loads(out.stdout)


def text(items) -> str:
    return json.dumps(items, sort_keys=True)


def tree_state(root: Path) -> Dict[str, tuple]:
    """Bytes (sha), mode and link target of every non-git, non-cache file."""
    state = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if ".git" in rel.parts or "__pycache__" in rel.parts or ".pytest_cache" in rel.parts or p.is_dir() and not p.is_symlink():
            continue
        st = p.lstat()
        link = os.readlink(p) if p.is_symlink() else None
        state[str(rel)] = (None if p.is_symlink() and not p.exists() else (sha(p) if not p.is_dir() else None), stat.S_IMODE(st.st_mode), link)
    return state


def assert_untouched(root: Path, before: Dict[str, tuple]) -> None:
    assert tree_state(root) == before
    assert git(root, "status", "--porcelain").strip() == ""


# --------------------------------------------------------------------------- 8.3: checked values never reach the reviewer

def test_tc71_redteam_checked_test_is_not_passed_on_beside_an_unparsed_one(tmp_path):
    root = make(tmp_path, CHECKED + "\n\n" + RUNTIME, ROW4 + ROW5)
    data = fid(root)
    assert data["mismatches"] == []
    assert "test_tc5_runtime_beta" in text(data["unparsed"]), data
    for items in (data["unparsed"], data["unmatched"]):
        assert "test_tc4_checked_alpha" not in text(items), data


def test_tc71_redteam_same_literal_checked_and_in_a_runtime_expression(tmp_path):
    """4.50 is checked against the row in one assert and used in `base * 4.50` in another: the runtime part is
    unparsed, the checked assert is not a second, unparsed copy."""
    src = "def test_tc4_mixed_delta(base):\n    assert score() >= 4.50\n    assert other() == base * 4.50\n"
    root = make(tmp_path, src + "\n\n" + CHECKED.replace("checked_alpha", "pure_alpha"))
    data = fid(root)
    assert data["mismatches"] == []
    assert "test_tc4_mixed_delta" in text(data["unparsed"]), data
    assert "test_tc4_pure_alpha" not in text(data["unparsed"]) + text(data["unmatched"]), data
    runtime_items = [i for i in data["unparsed"] if "test_tc4_mixed_delta" in text(i)]
    assert all("score()" not in text(i) for i in runtime_items), "the checked assert leaked into an unparsed item"


@pytest.mark.parametrize(
    "scenario",
    [
        "Given a score threshold `4.50`. Then the check passes.",
        "Given a score threshold 4.50[^1]. Then the check passes.\n\n[^1]: it was 3.00 before",
        "Given a score threshold 4.50 (was 3.00). Then the check passes.",
        "Given a pipe a \\| b and a score threshold 4.50. Then the check passes.",
        "Given a score threshold **4.50**. Then the check passes.",
    ],
    ids=["backticks", "footnote", "parenthetical", "escaped-pipe", "bold"],
)
def test_tc71_redteam_row_surface_forms_never_split_a_checked_value(tmp_path, scenario):
    """The value 4.50 is in the row however it is dressed. A test using it is matched or unparsed, never both and
    never a mismatch; the TC is found (not unmatched)."""
    root = make(tmp_path, CHECKED, row(4, scenario))
    data = fid(root)
    assert data["mismatches"] == [], data
    assert data["unmatched"] == [], data
    if "test_tc4_checked_alpha" in text(data["unparsed"]):
        assert data["checked"] == [] or "4.50" not in text(data["checked"]), "one value both checked and passed on"


def test_tc71_redteam_row_with_a_pipe_in_a_cell_keeps_later_columns(tmp_path):
    """An escaped pipe in the scenario must not shift columns so that the test file column parses as the row."""
    root = make(tmp_path, CHECKED + "\n\n" + MISMATCH.replace("def test_tc4_", "def test_tc6_"),
                ROW4.replace("Given a", "Given a x \\| y and a") + row(6, "Given threshold 4.50"))
    data = fid(root)
    assert [m for m in data["mismatches"] if "test_tc6_wrong_gamma" in text(m)], data
    assert "test_tc4_checked_alpha" not in text(data["unparsed"]) + text(data["unmatched"]), data


@pytest.mark.parametrize("variant", ["crlf", "bom", "bom-crlf"])
def test_tc71_redteam_encoding_forms_do_not_hide_a_mismatch_or_leak_a_match(tmp_path, variant):
    src = CHECKED + "\n\n" + MISMATCH
    if "crlf" in variant:
        src = src.replace("\n", "\r\n")
    data_bytes = src.encode()
    if "bom" in variant:
        data_bytes = b"\xef\xbb\xbf" + data_bytes
    root = make(tmp_path, data_bytes, ROW4 + row(7, "unused"))
    data = fid(root)
    assert [m for m in data["mismatches"] if "test_tc4_wrong_gamma" in text(m)], data
    assert "test_tc4_checked_alpha" not in text(data["unparsed"]) + text(data["unmatched"]), data


def test_tc71_redteam_non_utf8_byte_is_never_silently_clean(tmp_path):
    """A test file with a stray latin-1 byte: no traceback, and the wrong value is reported, or the test is handed
    to the reviewer as unparsed/unmatched. Reporting nothing would let a bad value through unseen."""
    src = ("# caf\xe9\n" + MISMATCH).encode("latin-1")
    root = make(tmp_path, src)
    data = fid(root)
    assert data["mismatches"] or data["unparsed"] or data["unmatched"], data


def test_tc71_redteam_fidelity_output_fed_back_as_a_row_is_inert(tmp_path):
    """The system's own output pasted into the test plan as a row must not turn checked values into reviewer
    items, nor crash the parser."""
    root = make(tmp_path, CHECKED + "\n\n" + RUNTIME, ROW4 + ROW5)
    first = fid(root)
    blob = json.dumps(first).replace("|", "/").replace("\n", " ")
    plan = root / "specs/demo/test-plan.md"
    plan.write_text(plan.read_text() + row(8, blob) + f"| TC-4 | 1.1 | int-cli | {blob} | real | x |\n")
    data = fid(root)
    assert data["mismatches"] == [], data
    assert "test_tc4_checked_alpha" not in text(data["unparsed"]) + text(data["unmatched"]), data


def test_tc71_redteam_unparsed_output_fed_back_as_a_test_literal(tmp_path):
    """An unparsed item's text used as a string literal in a test is one more value, not a reason to resend the
    checked ones."""
    root = make(tmp_path, CHECKED + "\n\n" + RUNTIME, ROW4 + ROW5)
    first = fid(root)
    src = CHECKED + f"\n\ndef test_tc5_echo_epsilon():\n    assert reply == {text(first['unparsed'])!r}\n"
    (root / TEST_FILE).write_text(src)
    data = fid(root)
    assert "test_tc4_checked_alpha" not in text(data["unparsed"]) + text(data["unmatched"]), data


def test_tc71_redteam_ongate_unparsed_items_are_not_gate_failures(tmp_path):
    """Items the script could not parse go to the reviewer: they are not mismatches, so the gate must not turn them
    into row-fidelity failures."""
    root = make(tmp_path, CHECKED + "\n\n" + RUNTIME, ROW4 + ROW5)
    assert fid(root)["unparsed"], "control: the runtime value is unparsed"
    result = gate(root)
    assert [f for f in result["failures"] if f.get("check") == "row-fidelity"] == [], result["failures"]
    assert not [c for c in result["checks"] if "fidelity" in c["name"].lower() and not c["ok"]], result["checks"]


# --------------------------------------------------------------------------- 8.6: nothing is modified

SHAPES = {
    "plain": dict(src=CHECKED + "\n\n" + MISMATCH + "\n\n" + RUNTIME, rows=ROW4 + ROW5),
    "crlf": dict(src=(CHECKED + "\n\n" + MISMATCH).replace("\n", "\r\n"), rows=ROW4),
    "bom": dict(src=b"\xef\xbb\xbf" + (CHECKED + "\n\n" + MISMATCH).encode(), rows=ROW4),
    "non-utf8": dict(src=("# caf\xe9\n" + MISMATCH).encode("latin-1"), rows=ROW4),
    "no-trailing-newline": dict(src=(CHECKED + MISMATCH).rstrip("\n"), rows=ROW4.rstrip("\n")),
    "pipe-row": dict(src=CHECKED, rows=ROW4.replace("Given a", "Given a x \\| y and a")),
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_tc74_redteam_files_byte_identical_after_fidelity(tmp_path, shape):
    root = make(tmp_path, SHAPES[shape]["src"], SHAPES[shape]["rows"])
    before = tree_state(root)
    fid(root)
    fid(root)
    assert_untouched(root, before)


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_tc74_redteam_ongate_files_byte_identical_after_gate(tmp_path, shape):
    root = make(tmp_path, SHAPES[shape]["src"], SHAPES[shape]["rows"])
    before = tree_state(root)
    gate(root)
    assert_untouched(root, before)


def test_tc74_redteam_read_only_spec_files_and_tests_keep_bytes_and_modes(tmp_path):
    root = make(tmp_path, CHECKED + "\n\n" + MISMATCH + "\n\n" + RUNTIME, ROW4 + ROW5)
    spec = root / "specs/demo"
    for p in (spec / "test-plan.md", spec / "design.md", root / TEST_FILE):
        p.chmod(0o444)
    spec.chmod(0o555)
    (root / "tests").chmod(0o555)
    try:
        git(root, "add", "-A")
        before = tree_state(root)
        out = run_ck(root, "lint", "demo", "--fidelity", "--task", "1", "--json")
        assert out.returncode in (0, 1), out.stdout + out.stderr
        assert "Traceback" not in out.stderr and "ermission" not in out.stderr, out.stderr
        data = json.loads(out.stdout)
        assert data["mismatches"], "the read-only files must still be checked"
        assert tree_state(root) == before
    finally:
        spec.chmod(0o755)
        (root / "tests").chmod(0o755)


def test_tc74_redteam_ongate_read_only_files_keep_bytes_and_modes(tmp_path):
    root = make(tmp_path, CHECKED + "\n\n" + MISMATCH, ROW4)
    spec = root / "specs/demo"
    for p in (spec / "test-plan.md", spec / "design.md", root / TEST_FILE):
        p.chmod(0o444)
    before = tree_state(root)
    gate(root)
    assert tree_state(root) == before
    assert git(root, "status", "--porcelain").strip() == ""


def test_tc74_redteam_symlinked_test_plan_stays_a_link_and_its_target_is_unchanged(tmp_path):
    root = make(tmp_path, CHECKED + "\n\n" + MISMATCH, ROW4, plan_link=True)
    link = root / "specs/demo/test-plan.md"
    target_before = os.readlink(link)
    before = tree_state(root)
    data = fid(root)
    assert [m for m in data["mismatches"] if "test_tc4_wrong_gamma" in text(m)], "the linked plan must be read"
    assert link.is_symlink() and os.readlink(link) == target_before
    assert_untouched(root, before)


def test_tc74_redteam_ongate_symlinked_test_plan_stays_a_link(tmp_path):
    root = make(tmp_path, CHECKED + "\n\n" + MISMATCH, ROW4, plan_link=True)
    link = root / "specs/demo/test-plan.md"
    before = tree_state(root)
    gate(root)
    assert link.is_symlink()
    assert_untouched(root, before)


def test_tc74_redteam_symlinked_test_file_and_design_are_not_replaced(tmp_path):
    root = make(tmp_path, CHECKED + "\n\n" + MISMATCH)
    real = root / "elsewhere"
    real.mkdir()
    (real / "t.py").write_bytes((root / TEST_FILE).read_bytes())
    (real / "design.md").write_bytes((root / "specs/demo/design.md").read_bytes())
    (root / TEST_FILE).unlink()
    (root / "specs/demo/design.md").unlink()
    (root / TEST_FILE).symlink_to("../elsewhere/t.py")
    (root / "specs/demo/design.md").symlink_to("../../elsewhere/design.md")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "links")
    before = tree_state(root)
    fid(root)
    assert (root / TEST_FILE).is_symlink() and (root / "specs/demo/design.md").is_symlink()
    assert_untouched(root, before)


def test_tc74_redteam_fidelity_output_is_never_written_into_the_plan(tmp_path):
    """Run with output redirected next to the spec: the check itself writes nothing, not even a report file."""
    root = make(tmp_path, CHECKED + "\n\n" + MISMATCH + "\n\n" + RUNTIME, ROW4 + ROW5)
    before = tree_state(root)
    names_before = sorted(str(p.relative_to(root)) for p in root.rglob("*") if ".git" not in p.parts and "__pycache__" not in p.parts)
    fid(root)
    names_after = sorted(str(p.relative_to(root)) for p in root.rglob("*") if ".git" not in p.parts and "__pycache__" not in p.parts)
    assert names_after == names_before
    assert_untouched(root, before)


def test_tc74_redteam_a_task_that_is_not_selected_is_not_touched_either(tmp_path):
    """No `--task`: the whole spec is checked, and still nothing is written."""
    root = make(tmp_path, CHECKED + "\n\n" + MISMATCH, ROW4)
    before = tree_state(root)
    out = run_ck(root, "lint", "demo", "--fidelity", "--json")
    assert out.returncode in (0, 1), out.stdout + out.stderr
    assert "Traceback" not in out.stderr
    json.loads(out.stdout)
    assert_untouched(root, before)
