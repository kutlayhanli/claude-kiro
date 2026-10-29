"""Red-team of Story 7's SHALL NOT clauses (7.3, 7.4, 7.5), through the real `ck doctor` / `ck init`."""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HINT_VERIFY = ["uv run pytest -q"]


def ck(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "claude_kiro.cli", *args], cwd=cwd, capture_output=True, text=True)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def make_repo(tmp_path: Path, name: str = "repo", pyproject: bool = True) -> Path:
    root = tmp_path / name
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    if pyproject:
        (root / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
    return root


def set_verify(root: Path, verify) -> None:
    (root / "specs").mkdir(exist_ok=True)
    (root / "specs" / "ck.json").write_text(json.dumps({"verify": verify}))


@pytest.fixture
def inited(tmp_path: Path) -> Path:
    root = make_repo(tmp_path)
    res = ck(root, "init", "--no-allow-workflow")
    assert res.returncode == 0, res.stdout + res.stderr
    return root


def doctor_out(root: Path) -> subprocess.CompletedProcess:
    return ck(root, "doctor")


def has_hint(res: subprocess.CompletedProcess) -> bool:
    return "xdist" in (res.stdout + res.stderr).lower()


NO_HINT_VERIFY = [
    # already parallel, in odd forms
    ["uv run pytest -q -n auto"],
    ["uv run pytest -q -n0"],
    ["uv run pytest -q -n=auto"],
    ["uv run pytest -q -nauto"],
    ["pytest -n 4"],
    ["pytest --numprocesses auto"],
    ["pytest --numprocesses=4"],
    ["python -m pytest -q -n auto tests"],
    ["PYTEST_ADDOPTS='-n auto' pytest"],
    ["PYTEST_ADDOPTS=-n auto pytest"],
    # only mention pytest, never run it
    ["pip install pytest"],
    ["echo pytest"],
    ["ruff check tests/test_pytest_x.py"],
    ["ruff check tests/test_pytest_plugin.py"],
    ["pytest-free"],
    ["npm test --silent"],
    ["make pytest-docs"],
    ["cat pytest.ini"],
    [],
    [""],
    ["   "],
    # a second command already parallel, the first not pytest
    ["ruff check .", "pytest -n auto"],
]


@pytest.mark.parametrize("verify", NO_HINT_VERIFY, ids=[repr(v) for v in NO_HINT_VERIFY])
def test_no_hint_for_near_miss_verify(inited: Path, verify):
    set_verify(inited, verify)
    res = doctor_out(inited)
    assert res.returncode == 0, res.stdout + res.stderr
    assert not has_hint(res), res.stdout


def test_unbalanced_quotes_do_not_crash_or_hint_wrongly(inited: Path):
    set_verify(inited, ["pytest -n auto 'unterminated"])
    res = doctor_out(inited)
    assert "Traceback" not in res.stderr, res.stderr
    assert not has_hint(res), res.stdout
    set_verify(inited, ["echo 'pytest"])
    res = doctor_out(inited)
    assert "Traceback" not in res.stderr, res.stderr
    assert not has_hint(res), res.stdout


def test_hint_check_command_fed_back_as_verify_does_not_crash_or_change_exit(inited: Path):
    set_verify(inited, HINT_VERIFY)
    res = doctor_out(inited)
    assert has_hint(res), "precondition: the hint is shown for plain pytest"
    cmds = [part for line in res.stdout.splitlines() for part in line.split("`")[1::2] if "PYTHONHASHSEED" in part]
    assert cmds, res.stdout
    set_verify(inited, [cmds[0]])
    res2 = doctor_out(inited)
    assert res2.returncode == res.returncode
    assert "Traceback" not in res2.stderr


def test_whole_hint_text_as_a_verify_command_does_not_crash(inited: Path):
    set_verify(inited, HINT_VERIFY)
    first = doctor_out(inited)
    lines = [ln.strip("- ").strip() for ln in first.stdout.splitlines() if "xdist" in ln.lower()]
    assert lines
    set_verify(inited, lines)
    res = doctor_out(inited)
    assert res.returncode == first.returncode
    assert "Traceback" not in res.stderr


# Verify values with non-string items (7, [1, 2], ["pytest", None]) already crash `ck doctor` at its
# "Verify commands:" line today, independent of the hint, so they cannot be compared with a baseline.
@pytest.mark.parametrize("verify", ["uv run pytest -q", {"cmd": "pytest"}, None],
                         ids=["str", "dict", "null"])
def test_malformed_verify_never_adds_a_hint_failure(inited: Path, verify):
    """Whatever the shape of `verify`, the hint code must not change doctor's exit status."""
    set_verify(inited, verify)
    with_shape = doctor_out(inited)
    set_verify(inited, ["true"])
    baseline = doctor_out(inited)
    assert with_shape.returncode == baseline.returncode, with_shape.stdout + with_shape.stderr
    assert "Traceback" not in with_shape.stderr, with_shape.stderr


def test_hint_never_appears_as_an_issue(inited: Path):
    set_verify(inited, HINT_VERIFY)
    res = doctor_out(inited)
    assert has_hint(res)
    assert "Issues found" not in res.stdout
    assert "Everything looks good" in res.stdout or "Warnings" in res.stdout
    issues_part = res.stdout.split("Issues found")[1] if "Issues found" in res.stdout else ""
    assert "xdist" not in issues_part.lower()


def test_doctor_exit_codes_equal_across_states_with_and_without_hint(tmp_path: Path):
    codes = {}
    for state in ("healthy", "no_claude", "no_verify"):
        for label, verify in (("hint", HINT_VERIFY), ("nohint", ["uv run pytest -q -n auto"])):
            root = make_repo(tmp_path, f"{state}-{label}")
            assert ck(root, "init", "--no-allow-workflow").returncode == 0
            set_verify(root, verify)
            if state == "no_claude":
                shutil.rmtree(root / ".claude")
            elif state == "no_verify":
                (root / "specs" / "ck.json").write_text("{}")
            res = doctor_out(root)
            codes[(state, label)] = res.returncode
            assert "Traceback" not in res.stderr, res.stderr
    for state in ("healthy", "no_claude", "no_verify"):
        assert codes[(state, "hint")] == codes[(state, "nohint")], codes


def test_doctor_with_hint_does_not_modify_the_project(inited: Path):
    set_verify(inited, HINT_VERIFY)
    before = snapshot(inited)
    doctor_out(inited)
    assert snapshot(inited) == before


def snapshot(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if ".git" in p.relative_to(root).parts or not p.is_file():
            continue
        out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_init_changes_nothing_about_deps_or_verify(tmp_path: Path):
    root = make_repo(tmp_path)
    (root / "uv.lock").write_text("version = 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    pyproject, lock = root / "pyproject.toml", root / "uv.lock"
    before = (pyproject.read_bytes(), lock.read_bytes())
    res = ck(root, "init", "--no-allow-workflow")
    assert res.returncode == 0, res.stdout + res.stderr
    assert (pyproject.read_bytes(), lock.read_bytes()) == before
    cfg = (root / "specs" / "ck.json").read_text()
    assert not any(" -n" in v or "numprocesses" in v for v in json.loads(cfg).get("verify", []))
    assert "xdist" not in pyproject.read_text()


def test_init_twice_and_on_existing_ck_json_is_byte_identical(tmp_path: Path):
    root = make_repo(tmp_path)
    (root / "uv.lock").write_text("version = 1\n")
    (root / "specs").mkdir()
    (root / "specs" / "ck.json").write_text('{\n  "verify": ["uv run pytest -q"]\n}\n')
    first = ck(root, "init", "--no-allow-workflow")
    assert first.returncode == 0, first.stdout + first.stderr
    watched = [root / "specs" / "ck.json", root / "pyproject.toml", root / "uv.lock"]
    before = [digest(p) for p in watched]
    assert (root / "specs" / "ck.json").read_text() == '{\n  "verify": ["uv run pytest -q"]\n}\n'
    second = ck(root, "init", "--no-allow-workflow")
    assert second.returncode == 0, second.stdout + second.stderr
    assert [digest(p) for p in watched] == before
    assert "xdist" in (first.stdout + first.stderr).lower(), "existing pytest verify still earns the hint"


def test_init_with_force_still_leaves_pyproject_and_lock_alone(tmp_path: Path):
    root = make_repo(tmp_path)
    (root / "uv.lock").write_text("version = 1\n")
    assert ck(root, "init", "--no-allow-workflow").returncode == 0
    watched = [root / "pyproject.toml", root / "uv.lock"]
    before = [digest(p) for p in watched]
    res = ck(root, "init", "--force", "--no-allow-workflow")
    assert res.returncode == 0, res.stdout + res.stderr
    assert [digest(p) for p in watched] == before


@pytest.mark.parametrize("verify", [["uv run pytest -q -n auto"], ["pytest --numprocesses=4"], ["pytest -n0"], ["echo pytest"], []])
def test_init_prints_no_hint_for_near_miss_existing_verify(tmp_path: Path, verify):
    root = make_repo(tmp_path)
    set_verify(root, verify)
    res = ck(root, "init", "--no-allow-workflow")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "xdist" not in (res.stdout + res.stderr).lower()
    assert json.loads((root / "specs" / "ck.json").read_text())["verify"] == verify


def test_init_package_json_only_project_has_no_hint(tmp_path: Path):
    root = make_repo(tmp_path, pyproject=False)
    (root / "package.json").write_text('{"name": "x", "scripts": {"test": "jest"}}\n')
    before = digest(root / "package.json")
    res = ck(root, "init", "--no-allow-workflow")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "xdist" not in (res.stdout + res.stderr).lower()
    assert digest(root / "package.json") == before
    verify = json.loads((root / "specs" / "ck.json").read_text()).get("verify", [])
    assert not any("pytest" in v for v in verify)


def test_init_hint_matches_doctor_hint(tmp_path: Path):
    """The system's own output fed back: init's suggestion text equals what doctor shows for the same verify."""
    root = make_repo(tmp_path)
    set_verify(root, HINT_VERIFY)
    init_res = ck(root, "init", "--no-allow-workflow")
    doc = doctor_out(root)
    init_lines = [ln.strip() for ln in init_res.stdout.splitlines() if "xdist" in ln.lower()]
    doc_lines = [ln.strip().lstrip("- ").strip() for ln in doc.stdout.splitlines() if "xdist" in ln.lower()]
    assert init_lines and doc_lines
    for d in doc_lines:
        assert any(d == i or i.endswith(d) for i in init_lines), (init_lines, doc_lines)
