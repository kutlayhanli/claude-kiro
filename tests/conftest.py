"""Fixtures: a real git project with a spec, a committed test suite, and a feature branch."""

import json
import subprocess
import uuid
from pathlib import Path

import pytest

CALC = "def add(a, b):\n    return a + b\n"

CALC_TEST = """from calc import add


def test_add():
    assert add(1, 2) == 3


def test_add_negative():
    assert add(-1, -1) == -2
"""

TASKS = """# Implementation Tasks: Calc

**Status:** In Progress

## Task Breakdown

### Task 1: Integration tests for subtract
**Status:** {t1}
**Track:** test
**Verifies:** Task 2
**Files:**
- `tests/test_subtract.py` - subtract behaviour through the public API
**Verify:** `python -m pytest -q tests/test_subtract.py`

**Acceptance:**
- [{a1}] Tests cover 1.1 and 1.2

---

### Task 2: Implement subtract
**Status:** {t2}
**Track:** impl
**Verified by:** Task 1
**Files:**
- `calc.py` - add subtract()

**Acceptance:**
- [{a2}] subtract() implemented

## Parallel Groups
Wave 1: Task 1 · Wave 2: Task 2
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Project on branch `feature`, branched from `main` with calc + tests committed."""
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")

    (root / "calc.py").write_text(CALC)
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text(CALC_TEST)
    (root / "conftest.py").write_text("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n")
    spec = root / "specs" / "calc"
    spec.mkdir(parents=True)
    (spec / "requirements.md").write_text("# Feature: Calc\n- 1.1 WHEN ... THE SYSTEM SHALL subtract\n")
    write_tasks(root)
    (root / "specs" / "ck.json").write_text(json.dumps({"verify": ["python -m pytest -q tests/test_calc.py"]}))
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-qb", "feature")
    return root


def write_tasks(root: Path, t1="Not Started", t2="Not Started", a1=" ", a2=" ") -> None:
    (root / "specs" / "calc" / "tasks.md").write_text(TASKS.format(t1=t1, t2=t2, a1=a1, a2=a2))


@pytest.fixture
def session() -> str:
    return f"test-{uuid.uuid4().hex}"
