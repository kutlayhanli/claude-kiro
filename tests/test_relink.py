"""Issue #6: `ck relink` breaks a verify-order cycle the way `ck lint` suggests.

The shapes are the equity Task 32 cycle (an impl task Verified by a test task whose tests import a module
a later task owns) and the verify-file cycle (an impl task's own Verify runs a later task's test file).
Everything drives the real CLI over real tasks.md files in a git repo.
"""

import json
import subprocess
from pathlib import Path

import pytest

from claude_kiro.lint import lint_spec
from test_lint import block, ck, write_spec

pytestmark = pytest.mark.usefixtures("no_network")


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def equity(tmp_path: Path) -> Path:
    """Task 32 (scaffold) Verified by Task 1 (harness tests), whose harness imports src/mtr/cli.py, owned by
    Task 33, which depends on Task 32. Task 32's Verify also runs Task 1's own test file."""
    (tmp_path / "tests/harness").mkdir(parents=True)
    (tmp_path / "tests/__init__.py").write_text("")
    (tmp_path / "tests/harness/__init__.py").write_text("")
    (tmp_path / "tests/harness/cli.py").write_text("def run_cli(argv):\n    from mtr.cli import main\n    return main(argv)\n")
    (tmp_path / "tests/test_harness_selftest.py").write_text("from tests.harness.cli import run_cli\n\n\ndef test_x():\n    assert run_cli([]) == 0\n")
    blocks = (
        block(1, "Harness", track="test", status="Done", files=["tests/harness/cli.py", "tests/test_harness_selftest.py"], verifies="Task 32")
        + block(32, "Scaffold", files=["pyproject.toml"], verify="uv sync` `uv run pytest -q tests/test_harness_selftest.py", vby="Task 1")
        + block(33, "CLI", files=["src/mtr/cli.py"], deps="Task 32 (needs the package)")
    )
    write_spec(tmp_path, blocks)
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return tmp_path


def cycles_at(root: Path, task: str):
    return [c for c in lint_spec(root / "specs/sim", root)["verifyCycles"] if c["task"] == task]


def test_relink_dry_run_names_the_edits_and_writes_nothing(equity):
    before = (equity / "specs/sim/tasks.md").read_text()
    assert cycles_at(equity, "32"), "the fixture has the cycle"
    res = ck(equity, "relink", "sim", "32", "--json")
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["fixable"] is True and out["applied"] is False
    assert out["verifiedBy"] == {"32": [], "33": ["1"]}
    assert out["verifies"] == {"1": ["33"]}
    assert out["verify"] == {"32": ["uv sync"]}
    assert (equity / "specs/sim/tasks.md").read_text() == before


def test_relink_apply_commit_breaks_the_cycle_like_the_hand_fix(equity):
    res = ck(equity, "relink", "sim", "32", "--apply", "--commit", "--json")
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["applied"] is True and out["committed"] is True, out
    text = (equity / "specs/sim/tasks.md").read_text()
    assert "**Verify:** `uv sync`\n" in text
    assert "**Verified by:** None" in text  # Task 32
    assert "**Verified by:** Task 1" in text  # Task 33 now
    assert "**Verifies:** Task 33" in text
    assert cycles_at(equity, "32") == []
    assert "relink: break Task 32" in git(equity, "log", "-1", "--format=%s")
    assert git(equity, "status", "--porcelain") == ""


def test_relink_leaves_the_rest_of_tasks_md_byte_identical(equity):
    before = (equity / "specs/sim/tasks.md").read_text()
    assert ck(equity, "relink", "sim", "32", "--apply").exit_code == 0
    after = (equity / "specs/sim/tasks.md").read_text()
    fields = ("**Verify:**", "**Verified by:**", "**Verifies:**")
    rest = lambda t: [line for line in t.splitlines() if not line.startswith(fields)]  # noqa: E731
    assert rest(before) == rest(after)  # only the three relinked fields change (a missing one may be added)


def test_relink_refuses_when_nothing_would_verify_the_task(equity):
    tasks = equity / "specs/sim/tasks.md"
    tasks.write_text(tasks.read_text().replace("**Verify:** `uv sync` `uv run pytest -q tests/test_harness_selftest.py`",
                                               "**Verify:** `uv run pytest -q tests/test_harness_selftest.py`"))
    before = tasks.read_text()
    res = ck(equity, "relink", "sim", "32", "--apply", "--json")
    assert res.exit_code == 1, res.output
    out = json.loads(res.output)
    assert out["fixable"] is False and "nothing would verify Task 32" in out["reason"]
    assert tasks.read_text() == before


def test_relink_of_a_task_without_a_cycle_is_a_no_op(equity):
    res = ck(equity, "relink", "sim", "33", "--apply", "--json")
    assert res.exit_code == 1
    assert "no verify-order cycle" in json.loads(res.output)["reason"]
    unknown = ck(equity, "relink", "sim", "99", "--json")
    assert unknown.exit_code == 1 and "not in tasks.md" in json.loads(unknown.output)["reason"]


def test_relink_fixes_a_verify_file_cycle_by_linking_the_oracle(tmp_path):
    """TC-77 shape: Task 8's Verify runs Task 9's file, Task 9 verifies Task 13, 13 depends on 8."""
    write_spec(tmp_path, block(8, "Perf core", files=["src/perf.py"], verify="uv run pytest -q tests/test_core.py` `uv run pytest -q tests/test_perf.py")
               + block(9, "Perf tests", track="test", files=["tests/test_perf.py"], verifies="Task 13")
               + block(10, "Core tests", track="test", files=["tests/test_core.py"], verifies="Task 8")
               + block(13, "Report", files=["src/report.py"], deps="Task 8 (uses perf)"))
    assert cycles_at(tmp_path, "8")
    res = ck(tmp_path, "relink", "sim", "8", "--apply", "--json")
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    # Task 8's Verify chose Task 9's file as its oracle: the missing piece is the link, not the command.
    assert out["verify"] == {}
    assert out["verifiedBy"] == {"8": ["9"]}
    assert out["verifies"] == {"9": ["13", "8"]}
    assert cycles_at(tmp_path, "8") == []


def test_relink_links_an_oracle_even_when_it_is_the_only_verify(tmp_path):
    """The equity Task 75 shape: X's whole Verify is T's files; linking keeps it verified."""
    write_spec(tmp_path, block(75, "Decay", files=["src/decay.py"], verify="uv run pytest -q tests/test_decay_golden.py", deps="Task 23 (tests first)")
               + block(23, "Decay and option tests", track="test", files=["tests/test_decay_golden.py", "tests/test_option_golden.py"], verifies="Task 76")
               + block(76, "Options", files=["src/options.py"], deps="Task 75 (uses decay)", vby="Task 23"))
    res = ck(tmp_path, "relink", "sim", "75", "--apply", "--json")
    assert res.exit_code == 0, res.output
    text = (tmp_path / "specs/sim/tasks.md").read_text()
    assert "**Verify:** `uv run pytest -q tests/test_decay_golden.py`" in text
    assert "**Verifies:** Task 76, Task 75" in text
    assert cycles_at(tmp_path, "75") == []
