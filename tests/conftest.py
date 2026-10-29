"""Fixtures: a real git project with a spec, a committed test suite, and a feature branch."""

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

CALC = "def add(a, b):\n    return a + b\n"


@pytest.fixture(autouse=True)
def _isolated_preferences(tmp_path_factory, monkeypatch):
    """Never read the developer's real ~/.config/claude-kiro (ck init/upgrade resolve agent settings)."""
    monkeypatch.setenv("CK_CONFIG_HOME", str(tmp_path_factory.mktemp("ck-prefs")))


@pytest.fixture(autouse=True)
def _isolated_data_home(tmp_path_factory, monkeypatch):
    """`ck run` auto-collects run analytics: never let a test touch the developer's real ~/.local/share."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path_factory.mktemp("xdg-parent") / "xdg-data"))
    monkeypatch.delenv("CK_DATA_HOME", raising=False)
    monkeypatch.delenv("CK_RUN_SANDBOXED", raising=False)
    monkeypatch.delenv("CK_STATS_DRAFTER", raising=False)


@pytest.fixture(autouse=True)
def _no_real_claude_home(tmp_path_factory, monkeypatch):
    """Default to an empty Claude home; a test that needs one sets CLAUDE_CONFIG_DIR itself afterwards."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude-parent") / "empty-claude-home"))


@pytest.fixture(autouse=True)
def _isolated_git_identity(tmp_path_factory, monkeypatch):
    """Never read the developer's git config; give every test a fixed identity."""
    cfg = tmp_path_factory.mktemp("git-parent") / "gitconfig"
    cfg.write_text("[user]\n\tname = Test User\n\temail = test@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


NETGUARD_DIR = Path(__file__).parent / "netguard"


@pytest.fixture
def no_network(tmp_path_factory, monkeypatch):
    """Block the network in this process and (through tests/netguard/sitecustomize.py) in every Python child.

    The in-process guard raises AssertionError("network"); the child guard appends a line to $CK_TEST_NET_LOG.
    Unix sockets stay allowed. Non-Python children (git, gh) get a dead proxy and file-only git protocols.
    """
    import socket

    real_connect, real_connect_ex, real_create = socket.socket.connect, socket.socket.connect_ex, socket.create_connection

    def _is_net(sock) -> bool:
        return sock.family in (socket.AF_INET, socket.AF_INET6)

    def connect(self, *a, **k):
        if _is_net(self):
            raise AssertionError("network")
        return real_connect(self, *a, **k)

    def connect_ex(self, *a, **k):
        if _is_net(self):
            raise AssertionError("network")
        return real_connect_ex(self, *a, **k)

    def create_connection(*a, **k):
        raise AssertionError("network")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    log = tmp_path_factory.mktemp("netlog") / "net.log"
    monkeypatch.setenv("CK_TEST_NET_LOG", str(log))
    existing = os.environ.get("PYTHONPATH")
    monkeypatch.setenv("PYTHONPATH", str(NETGUARD_DIR) + (os.pathsep + existing if existing else ""))
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    yield log

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
