"""pytest-xdist hint in `ck doctor` / `ck init` (TC-49..TC-53, TC-57, P-20): the real CLI, real shells, real pytest projects."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from stats_helpers import ck_sub

PYTEST_CMD = f"{sys.executable} -m pytest -q"
HINT_VERIFY = ["uv run pytest -q"]
NO_HINT_VERIFIES = [
    ["uv run pytest -q -n auto"],
    ["pytest --numprocesses=4"],
    ["npm test --silent"],
    [],
    ["ruff check tests/test_pytest_plugin.py"],
]


def _bullets(output, heading):
    """The bullet lines under a doctor section heading (`Warnings:` or `Issues found:`)."""
    m = re.search(rf"{re.escape(heading)}\n((?:  - .*\n?)*)", output)
    return m.group(1).splitlines() if m else []


def _git(path, *args):
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    return path


@pytest.fixture
def inited(tmp_path):
    """A fresh git repo with a pyproject.toml, set up by `ck init --no-allow-workflow`."""
    project = _repo(tmp_path / "proj")
    (project / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0"\n')
    r = ck_sub("init", "--no-allow-workflow", cwd=project)
    assert r.exit_code == 0, r.output
    return project


def set_verify(project, verify):
    path = project / "specs" / "ck.json"
    cfg = json.loads(path.read_text()) if path.exists() else {}
    if verify is None:
        cfg.pop("verify", None)
    else:
        cfg["verify"] = verify
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n")


def doctor(project):
    r = ck_sub("doctor", cwd=project)
    return r.exit_code, _bullets(r.output, "Warnings:"), _bullets(r.output, "Issues found:")


def xdist_bullets(bullets):
    return [b for b in bullets if "xdist" in b.lower()]


@pytest.fixture
def stable_suite(tmp_path):
    d = tmp_path / "stable"
    d.mkdir()
    (d / "test_stable.py").write_text(
        "import pytest\n\n@pytest.mark.parametrize('p', [1, 2, 3])\ndef test_a(p):\n    assert p\n")
    return d


@pytest.fixture
def pid_suite(tmp_path):
    d = tmp_path / "pid"
    d.mkdir()
    (d / "test_pid.py").write_text(
        "import os\nimport pytest\n\n@pytest.mark.parametrize('p', [os.getpid()])\ndef test_a(p):\n    assert p\n")
    return d


# TC-49 (7.1)
def test_doctor_warns_about_xdist_for_serial_pytest_verify(inited):
    set_verify(inited, HINT_VERIFY)
    _, warnings, issues = doctor(inited)
    hint = xdist_bullets(warnings)
    assert len(hint) == 1, warnings
    assert "pytest-xdist" in hint[0] and "-n auto" in hint[0]
    assert xdist_bullets(issues) == []


def _check_command(hint):
    ticks = re.findall(r"`([^`]+)`", hint)
    if ticks:
        cmds = [t for t in ticks if "PYTHONHASHSEED" in t]
        assert len(cmds) == 1, hint
        return cmds[0]
    return hint.split("One-line check:", 1)[1].strip()


# TC-50 (7.2)
def test_hint_names_the_pitfall_and_its_check_command_discriminates(inited, stable_suite, pid_suite, tmp_path):
    set_verify(inited, [PYTEST_CMD])
    _, warnings, _ = doctor(inited)
    hint = xdist_bullets(warnings)[0]
    assert "unstable" in hint.lower()
    assert "different tests were collected" in hint.lower()
    cmd = _check_command(hint)
    assert cmd.count("PYTHONHASHSEED") == 2 and "--collect-only" in cmd
    cmd = cmd.replace("/tmp/", str(tmp_path) + "/")  # /tmp may be read-only in a sandbox

    ok = subprocess.run(["sh", "-c", cmd], cwd=stable_suite, capture_output=True, text=True, timeout=300)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    bad = subprocess.run(["sh", "-c", cmd], cwd=pid_suite, capture_output=True, text=True, timeout=300)
    assert bad.returncode != 0 or bad.stdout.strip(), "pid-parametrized suite should show a difference"


# TC-51 (7.3)
@pytest.mark.parametrize("verify", NO_HINT_VERIFIES, ids=lambda v: " ".join(v) or "empty")
def test_no_hint_when_verify_is_not_serial_pytest(inited, verify):
    set_verify(inited, verify)
    _, warnings, issues = doctor(inited)
    assert xdist_bullets(warnings) == [] and xdist_bullets(issues) == []


# TC-52 (7.4)
def test_init_prints_the_same_suggestion_and_changes_nothing(tmp_path):
    project = _repo(tmp_path / "fresh")
    (project / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0"\n')
    before = (project / "pyproject.toml").read_bytes()
    r = ck_sub("init", "--no-allow-workflow", cwd=project)
    assert r.exit_code == 0, r.output
    cfg = json.loads((project / "specs" / "ck.json").read_text())
    assert not any(re.search(r"(^|\s)-n(\s|=|\d|$)|--numprocesses", c) for c in cfg["verify"])
    assert (project / "pyproject.toml").read_bytes() == before
    _, warnings, _ = doctor(project)
    hint = xdist_bullets(warnings)
    assert len(hint) == 1, (cfg, warnings)
    assert hint[0].strip()[2:].strip() in r.output


def test_init_gives_no_suggestion_for_a_package_json_only_repo(tmp_path):
    project = _repo(tmp_path / "node")
    (project / "package.json").write_text(json.dumps({"name": "x", "scripts": {"test": "node -e 0"}}))
    r = ck_sub("init", "--no-allow-workflow", cwd=project)
    assert r.exit_code == 0, r.output
    assert "xdist" not in r.output.lower()



# TC-53 (7.5)
@pytest.mark.parametrize("break_it", [False, True], ids=["healthy", "issue"])
def test_doctor_exit_code_is_unchanged_by_the_hint(inited, break_it):
    if break_it:
        shutil.rmtree(inited / ".claude")
    set_verify(inited, HINT_VERIFY)
    with_hint = doctor(inited)
    set_verify(inited, ["uv run pytest -q -n auto"])
    without = doctor(inited)
    assert xdist_bullets(with_hint[1]) and not xdist_bullets(without[1])
    assert with_hint[0] == without[0]


# TC-57 (7.1, 7.3)
@pytest.mark.parametrize("verify", [HINT_VERIFY, *NO_HINT_VERIFIES], ids=lambda v: " ".join(v) or "empty")
def test_doctor_hint_iff_seam_suggests(inited, verify):
    from claude_kiro import xdist

    set_verify(inited, verify)
    _, warnings, _ = doctor(inited)
    got = xdist_bullets(warnings)
    expected = xdist.suggestion(verify)
    if expected is None:
        assert got == []
    else:
        assert got == [f"  - {expected}"]


# P-20 (7.5)
def _state_healthy(p):
    pass


def _state_no_claude(p):
    shutil.rmtree(p / ".claude")


def _state_no_verify(p):
    pass  # verify is removed by the case itself


STATES = {"healthy": _state_healthy, "no-claude": _state_no_claude, "no-verify": _state_no_verify}


@pytest.mark.parametrize("state", sorted(STATES))
def test_p20_doctor_exit_status_ignores_the_hint(inited, state):
    codes = {}
    for name, verify in (("hint", HINT_VERIFY), ("no-hint", ["uv run pytest -q -n auto"])):
        set_verify(inited, None if state == "no-verify" else verify)
        if not (inited / ".claude").exists() and state != "no-claude":
            pytest.fail("fixture lost .claude")
        if state == "no-claude" and (inited / ".claude").exists():
            STATES[state](inited)
        code, warnings, _ = doctor(inited)
        if state != "no-verify":
            assert bool(xdist_bullets(warnings)) == (name == "hint")
        codes[name] = code
    assert codes["hint"] == codes["no-hint"]


PROPERTIES = {"P-20": "test_p20_doctor_exit_status_ignores_the_hint"}
