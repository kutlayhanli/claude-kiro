"""Landing bisection blames only a task whose own task-scoped gate fails (Story 15; TC-170-172, 175, 198; P-48).

Real temporary git repositories and the real `ck worktree land --gate --json`. Task branches commit their own files
and their own `**Status:** Done` in `tasks.md`, as real implementers do, so a gate after an earlier half has MERGED
sees those tasks Done on the target.
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pytest
from click.testing import CliRunner
from hypothesis import given, settings
from hypothesis import strategies as st

from claude_kiro.cli.main import cli

from conftest import git

PROPERTIES = {"P-48": "test_p48_a_task_is_red_only_on_its_own_task_scoped_red_gate"}

OTHER_BAD = "other_bad.txt"


def ck(cwd: Path, *args: str):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(cli, list(args))
    finally:
        os.chdir(old)


def block(num: int, status: str = "Not Started") -> str:
    return (
        f"### Task {num}: task {num}\n"
        f"**Status:** {status}\n"
        "**Track:** impl\n"
        "**Description:** one of the demo tasks.\n"
        "**Files:**\n"
        f"- `f{num}.txt`\n"
        f"**Verify:** `test -f f{num}.txt`\n\n"
    )


def tasks_md(total: int, done: Sequence[int], with_test_task: bool) -> str:
    out = "# Implementation Tasks: Demo\n\n"
    if with_test_task:
        out += (
            "### Task 100: tests\n**Status:** Done\n**Track:** test\n**Verifies:** Task 1\n"
            "**Description:** the suite only the full run executes.\n**Files:**\n- `tests/test_other.py`\n"
            "**Verify:** `python -m pytest -q tests/test_other.py`\n\n"
        )
    for i in range(1, total + 1):
        out += block(i, "Done" if i in done else "Not Started")
    return out


def make_repo(
    root: Path,
    total: int,
    done: Sequence[int] = (),
    with_test_task: bool = True,
    log: Optional[Path] = None,
    other_bad_on_base: bool = False,
    flaky_marker: Optional[Path] = None,
) -> None:
    """A repository whose `verify` runs tests/test_other.py: red as soon as `other_bad.txt` exists."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    (root / ".gitignore").write_text(".venv/\n.ck/worktrees/\n__pycache__/\n.pytest_cache/\n")
    (root / "tests").mkdir()
    logging = f"Path({str(log)!r}).open('a').write('ran\\n')\n" if log else ""
    (root / "tests/test_other.py").write_text(
        "from pathlib import Path\n\n\n"
        "def test_other():\n"
        f"    {logging.strip() or 'pass'}\n"
        f"    assert not (Path(__file__).resolve().parent.parent / {OTHER_BAD!r}).exists(), 'an earlier task broke me'\n"
    )
    if flaky_marker:  # fails on its first run, keyed by a marker it writes outside the tree, then passes
        (root / "tests/test_other.py").write_text(
            "from pathlib import Path\n\n\n"
            "def test_other():\n"
            f"    marker = Path({str(flaky_marker)!r})\n"
            "    first = not marker.exists()\n"
            "    marker.write_text('ran')\n"
            "    assert not first, 'fails on the first run only'\n"
        )
    (root / "specs/demo").mkdir(parents=True)
    (root / "specs/demo/tasks.md").write_text(tasks_md(total, done, with_test_task))
    (root / "specs/ck.json").write_text(
        json.dumps({"verify": [f"{sys.executable} -m pytest -q -p no:cacheprovider tests/test_other.py"]})
    )
    for i in done:
        (root / f"f{i}.txt").write_text("done\n")
    if other_bad_on_base:
        (root / OTHER_BAD).write_text("earlier regression\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")


def wt(root: Path, n) -> Path:
    return root / ".ck/worktrees" / f"demo-task-{n}"


def implement(root: Path, num: int, bad: bool = False, breaks_other: bool = False) -> None:
    """Commit what a real implementer does: its own file (not for a task whose own Verify must fail) and its Done."""
    path = wt(root, num)
    if bad:
        (path / f"unrelated{num}.txt").write_text("not what the Verify wants\n")
    else:
        (path / f"f{num}.txt").write_text("ok\n")
    if breaks_other:
        (path / OTHER_BAD).write_text("regression only the full suite sees\n")
    tasks = path / "specs/demo/tasks.md"
    text = tasks.read_text()
    pattern = re.compile(rf"(### Task {num}: .*?\n\*\*Status:\*\* )Not Started")
    assert pattern.search(text)
    tasks.write_text(pattern.sub(r"\1Done", text, count=1))
    git(path, "add", "-A")
    git(path, "commit", "-qm", f"task {num}")


def land(root: Path, *nums: str):
    result = ck(root, "worktree", "land", "demo", *nums, "--gate", "--json")
    try:
        data = json.loads(result.output)
    except ValueError:
        raise AssertionError(result.output)
    return result, data, {t["task"]: t for t in data["tasks"]}


def gate_for(data: Dict, tasks: Sequence[str]) -> List[Dict]:
    return [g for g in data["gates"] if sorted(g["tasks"]) == sorted(tasks)]


def log_lines(log: Path) -> int:
    return len(log.read_text().splitlines()) if log.exists() else 0


def test_tc170_only_the_task_whose_own_gate_fails_is_red(tmp_path):
    """TC-170: three tasks, only the third fails its own Verify."""
    root = tmp_path / "repo"
    make_repo(root, 3, with_test_task=False)
    assert ck(root, "worktree", "create", "demo", "1", "2", "3").exit_code == 0
    implement(root, 1)
    implement(root, 2)
    implement(root, 3, bad=True)

    result, data, by = land(root, "1", "2", "3")
    assert result.exit_code == 1, result.output
    assert [by[n]["state"] for n in ("1", "2", "3")] == ["MERGED", "MERGED", "RED"]
    assert data["bisected"] is True
    assert gate_for(data, ["1", "2", "3"])[0]["green"] is False
    assert gate_for(data, ["2", "3"]), data["gates"]  # the right half is gated, not inferred red
    alone = gate_for(data, ["3"])
    assert len(alone) == 1 and alone[0]["green"] is False  # the culprit's own gate
    for n in ("1", "2"):
        assert [g["green"] for g in gate_for(data, [n])] == [True]
    # The RED task carries the tail and failures of its own gate: its Verify, no other task's.
    own = alone[0]
    assert by["3"]["checkTail"] == own["checkTail"]
    if "failures" in own:
        assert by["3"].get("failures") == own["failures"]
    assert "f3.txt" in by["3"]["checkTail"]
    assert "f1.txt" not in by["3"]["checkTail"] and "f2.txt" not in by["3"]["checkTail"]
    assert "unreproduced" not in data


def test_tc171_a_batch_only_failure_blames_no_task_and_no_gate_below_widens(tmp_path):
    """TC-171: the observed case. Two tasks complete the spec; the first breaks only the full suite."""
    root, log = tmp_path / "repo", tmp_path / "verify.log"
    make_repo(root, 2, log=log)
    assert ck(root, "worktree", "create", "demo", "1", "2").exit_code == 0
    implement(root, 1, breaks_other=True)
    implement(root, 2)

    result, data, by = land(root, "1", "2")
    assert [by[n]["state"] for n in ("1", "2")] == ["MERGED", "MERGED"], result.output
    assert not [n for n, t in by.items() if t["state"] == "RED"]
    batch = data["gates"][0]
    assert sorted(batch["tasks"]) == ["1", "2"] and batch["green"] is False
    unreproduced = data["unreproduced"]
    assert sorted(unreproduced["tasks"]) == ["1", "2"]
    assert "tests/test_other.py" in json.dumps(unreproduced["failures"]) + unreproduced["tail"]
    # The gate on the last single task sees the spec complete on the target, and still ran task-scoped.
    last = data["gates"][-1]
    assert last["tasks"] == ["2"] and last["green"] is True
    assert log_lines(log) == 1  # `verify` ran once: the batch gate, no gate after it
    assert (root / OTHER_BAD).exists()  # the target holds what the batch gate saw


def test_tc172_a_batch_gate_that_fails_once_blames_no_task(tmp_path):
    """TC-172: the batch gate fails on its first run only; nothing is RED, both tasks merge, `unreproduced` is set."""
    root, marker = tmp_path / "repo", tmp_path / "flaky.marker"
    make_repo(root, 2, flaky_marker=marker)
    assert ck(root, "worktree", "create", "demo", "1", "2").exit_code == 0
    implement(root, 1)
    implement(root, 2)

    result, data, by = land(root, "1", "2")
    assert [by[n]["state"] for n in ("1", "2")] == ["MERGED", "MERGED"], result.output
    assert not [n for n, t in by.items() if t["state"] == "RED"]
    assert sorted(data["gates"][0]["tasks"]) == ["1", "2"] and data["gates"][0]["green"] is False
    assert marker.exists()
    assert sorted(data["unreproduced"]["tasks"]) == ["1", "2"]


@pytest.mark.parametrize(
    "case, bad, breaks_other",
    [("one culprit", {3}, None), ("two culprits", {2, 4}, None), ("batch only", set(), 1)],
)
def test_tc175_a_batch_of_four_costs_at_most_seven_gates(tmp_path, case, bad, breaks_other):
    root = tmp_path / "repo"
    make_repo(root, 4)
    nums = ["1", "2", "3", "4"]
    assert ck(root, "worktree", "create", "demo", *nums).exit_code == 0
    for n in range(1, 5):
        implement(root, n, bad=n in bad, breaks_other=n == breaks_other)

    result, data, by = land(root, *nums)
    assert len(data["gates"]) <= 2 * 4 - 1, data["gates"]
    red = {n for n, t in by.items() if t["state"] == "RED"}
    assert red == {str(n) for n in bad}, case
    if not bad:
        assert all(t["state"] == "MERGED" for t in by.values())
        assert "unreproduced" in data
    else:
        assert all(by[n]["state"] == "MERGED" for n in nums if n not in red)
        assert "unreproduced" not in data


def test_tc198_a_lone_task_completing_the_spec_is_gated_task_scoped(tmp_path):
    """TC-198: an earlier task on the target broke the full suite; the lone last task is not blamed for it."""
    root, log = tmp_path / "repo", tmp_path / "verify.log"
    make_repo(root, 2, done=[1], log=log, other_bad_on_base=True)
    assert ck(root, "worktree", "create", "demo", "2").exit_code == 0
    implement(root, 2)

    result, data, by = land(root, "2")
    assert result.exit_code == 0, result.output
    assert by["2"]["state"] == "MERGED"
    assert [g["tasks"] for g in data["gates"]] == [["2"]] and data["gates"][0]["green"] is True
    assert log_lines(log) == 0  # no `verify` run, so tests/test_other.py never ran
    assert "unreproduced" not in data


# --- P-48 ------------------------------------------------------------------------------


class ScopedGate:
    """An in-process gate modelling gate.py's scope rule: the full suite runs when called with `whole` true and the
    tasks Done on the target plus the group cover the spec."""

    def __init__(self, bad: set, full_suite_fails: bool):
        self.bad, self.full_suite_fails = bad, full_suite_fails
        self.calls: List[Dict] = []

    def __call__(self, target: Path, group: List[str], whole: bool = True):
        text = (target / "specs/demo/tasks.md").read_text()
        every = set(re.findall(r"^### Task (\d+):", text, re.M))
        done = {m.group(1) for m in re.finditer(r"^### Task (\d+):.*\n\*\*Status:\*\* Done", text, re.M)}
        full = bool(whole) and every <= (done | set(group))
        red = bool(set(group) & self.bad) or (self.full_suite_fails and full)
        self.calls.append({"group": list(group), "whole": bool(whole), "full": full, "red": red})
        return (not red), (f"red {group}" if red else ""), []


@st.composite
def scenarios(draw):
    n = draw(st.integers(min_value=1, max_value=6))
    bad = {str(i) for i in range(1, n + 1) if draw(st.booleans())}
    return n, bad, draw(st.booleans()), draw(st.sampled_from(["none", "done", "pending"]))


@settings(derandomize=True, max_examples=30, database=None, deadline=None)
@given(scenarios())
def test_p48_a_task_is_red_only_on_its_own_task_scoped_red_gate(scenario):
    from claude_kiro import worktrees

    n, bad, flag, extra = scenario
    nums = [str(i) for i in range(1, n + 1)]
    with tempfile.TemporaryDirectory(prefix="p48-") as tmp:
        root = Path(tmp) / "repo"
        total = n + (0 if extra == "none" else 1)
        make_repo(root, total, done=[n + 1] if extra == "done" else [], with_test_task=False)
        worktrees.create(root, "demo", nums, base="main")
        for i in range(1, n + 1):
            implement(root, i, bad=str(i) in bad)
        gate = ScopedGate(bad, flag)

        data = worktrees.land(root, "demo", nums, into="main", gate=gate)

    by = {t["task"]: t for t in data["tasks"]}
    red = {k for k, t in by.items() if t["state"] == "RED"}
    calls = gate.calls
    assert 1 <= len(calls) <= 2 * n - 1
    assert all(not c["whole"] for c in calls[1:]), calls  # only the batch gate may widen
    if n == 1:
        assert calls[0]["whole"] is False  # a lone task is never gated with the full suite
    for task in red:
        assert task in bad
        assert any(c["group"] == [task] and not c["whole"] and c["red"] for c in calls), (task, calls)
    if bad:
        assert red == bad
        assert all(by[k]["state"] == "MERGED" for k in nums if k not in bad)
        assert "unreproduced" not in data
    else:
        assert not red and all(by[k]["state"] == "MERGED" for k in nums)
        ran_full = calls[0]["full"]
        assert ("unreproduced" in data) == (flag and ran_full)
