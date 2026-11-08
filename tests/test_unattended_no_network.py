"""TC-168 (NFR, no network): the unattended-correctness commands never touch the network.

Every test runs under the `no_network` fixture: the in-process socket guard, plus tests/netguard/sitecustomize.py
for every Python child (it appends a line to $CK_TEST_NET_LOG) and a dead proxy for non-Python children. Each test
runs the real CLI or real git in a real temporary repository and then checks that the network log is empty and that
the command exists (a missing command or option is how these tests fail before the implementation).
Project modules are never imported at module level.
"""

import json
from pathlib import Path
from typing import Sequence

import pytest

from unattended_helpers import (
    attr_project,
    ck_run,
    commit_all,
    hooks_repo,
    integration_worktree,
    raw,
    spec_graph_project,
    task_worktree,
)

pytestmark = pytest.mark.usefixtures("no_network")

PROPERTIES: dict = {}

_MISSING = ("No such command", "No such option", "Usage:")


def _log_text(log: Path) -> str:
    return log.read_text().strip() if log.exists() else ""


def _ck(no_network: Path, cwd: Path, *args: str, ok: Sequence[int] = (0, 1)):
    """Run the real ck; the command must exist, exit in `ok`, and leave the network log empty."""
    out = ck_run(cwd, *args)
    assert not any(m in out.stderr for m in _MISSING), f"ck {' '.join(args)} is not implemented: {out.stderr}"
    assert out.returncode in ok, f"ck {' '.join(args)} exited {out.returncode}: {out.stdout}\n{out.stderr}"
    assert _log_text(no_network) == "", _log_text(no_network)
    return out


def _graph_project(tmp_path: Path) -> Path:
    tasks = [
        dict(num=1, title="Tests", track="test", files=["tests/test_a.py"], verifies="Task 2", deps="None",
             verify="python -m pytest -q tests/test_a.py"),
        dict(num=2, title="Impl", track="impl", files=["src/mtr/a.py"], vby="Task 1", deps="Task 1"),
        dict(num=3, title="New command", track="impl", files=["src/mtr/cli.py"], deps="None"),
    ]
    tests = {"tests/test_a.py": "import subprocess\n\n\ndef test_tc1_cli():\n    subprocess.run(['mtr', 'version'])\n"}
    return spec_graph_project(tmp_path, tasks=tasks, design_cli={"mtr version": "src/mtr/cli.py"}, tests=tests)


def test_tc168_gate(no_network, tmp_path):
    root = attr_project(tmp_path, tests={"tc1_add": "pass", "tc2_add": "fail"})
    out = _ck(no_network, root, "gate", "demo", "--attribute", "--json", ok=(0, 1, 2, 3))
    assert json.loads(out.stdout), "gate --json printed no JSON"


def test_tc168_graph(no_network, tmp_path):
    root = _graph_project(tmp_path)
    out = _ck(no_network, root, "graph", "sim", "--json")
    assert json.loads(out.stdout)["spec"] == "sim"


def test_tc168_lint(no_network, tmp_path):
    root = _graph_project(tmp_path)
    out = _ck(no_network, root, "lint", "sim", "--json")
    assert isinstance(json.loads(out.stdout), dict)


def test_tc168_plan(no_network, tmp_path):
    root = _graph_project(tmp_path)
    tasks = root / "specs" / "sim" / "tasks.md"
    before = tasks.read_bytes()
    out = _ck(no_network, root, "plan", "sim", "--json")
    assert isinstance(json.loads(out.stdout), dict)
    assert tasks.read_bytes() == before


def test_tc168_relink(no_network, tmp_path):
    root = _graph_project(tmp_path)
    _ck(no_network, root, "relink", "sim", "--commands", "--apply")
    commit_all(root, "after relink")


def test_tc168_lint_fidelity(no_network, tmp_path):
    root = attr_project(tmp_path, tests={"tc1_add": "pass"})
    out = _ck(no_network, root, "lint", "demo", "--fidelity", "--json")
    assert isinstance(json.loads(out.stdout), dict)


def test_tc168_start(no_network, tmp_path):
    root = hooks_repo(tmp_path)
    out = _ck(no_network, root, "worktree", "start", "demo", "--json", ok=(0,))
    assert isinstance(json.loads(out.stdout), dict)


def test_tc168_hooked_git_merge(no_network, tmp_path):
    """A raw `git merge` runs the installed hooks, which call ck (a Python child the netguard watches)."""
    root = hooks_repo(tmp_path)
    _ck(no_network, root, "worktree", "start", "demo", "--json", ok=(0,))
    integ = integration_worktree(root)
    for n in ("3", "1"):
        raw(integ, "merge", "--no-edit", f"feat/demo-task-{n}")
    assert task_worktree(root, "3").exists()
    assert _log_text(no_network) == "", _log_text(no_network)


def test_tc168_land(no_network, tmp_path):
    root = hooks_repo(tmp_path)
    out = _ck(no_network, root, "worktree", "land", "demo", "3", "--json", ok=(0, 1, 2, 3))
    assert json.loads(out.stdout)


def test_tc168_audit(no_network, tmp_path):
    root = hooks_repo(tmp_path)
    out = _ck(no_network, root, "worktree", "audit", "demo", "--json", ok=(0, 1, 3))
    assert isinstance(json.loads(out.stdout), dict)


def test_tc168_fixtures_status(no_network, tmp_path):
    root = attr_project(tmp_path, tests={"tc1_add": "pass"})
    out = _ck(no_network, root, "fixtures", "status", "demo", "--json")
    assert json.loads(out.stdout) is not None


def test_tc168_testfix_check(no_network, tmp_path):
    root = attr_project(tmp_path, tests={"tc1_add": "pass"})
    out = _ck(no_network, root, "testfix", "check", "demo", "--test", "tests/test_calc_tc.py::test_tc1_add", "--json")
    assert "allowed" in json.loads(out.stdout)
