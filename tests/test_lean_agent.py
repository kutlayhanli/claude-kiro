"""The lean runner agent (Story 19): its file, its vendored copy, the skill flag and `ck doctor` (TC-160, TC-162b).

Reads the real plugin files and runs the real `ck vendor` and `ck doctor` in a temp project. TC-172 (the byte-identity
tests in tests/test_plugin.py) is run, not copied, in Task 97's Verify.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_kiro.cli.main import cli

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin"
RUNNER = PLUGIN / "agents" / "runner.md"
KEYS_RECORD = REPO / "specs" / "unattended-correctness" / "claude-code-agent-keys.md"
BASE_KEYS = {"name", "description", "tools", "model", "effort", "maxTurns"}
CLAUDE_MD_KEY = re.compile(r"`([A-Za-z_]*(?:claude|md)[A-Za-z_]*)`", re.IGNORECASE)


def frontmatter(text: str) -> dict:
    assert text.startswith("---"), "no frontmatter"
    head = text.split("---")[1]
    out = {}
    for line in head.strip().splitlines():
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            out[key.strip()] = value.strip()
    return out


def run(cwd: Path, *args: str):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(cli, list(args))
    finally:
        os.chdir(old)


@pytest.fixture
def vendored(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='p'\n")
    (root / "uv.lock").write_text("")
    out = run(root, "init", "--no-allow-workflow")
    assert out.exit_code == 0, out.output
    out = run(root, "vendor")
    assert out.exit_code == 0, out.output
    return root


def assert_lean_frontmatter(fm: dict, where: str):
    assert fm.get("tools") == "Bash", f"{where}: tools must be Bash only, got {fm.get('tools')!r}"
    assert fm.get("model") == "haiku", where
    assert fm.get("effort") == "low", where
    assert fm.get("maxTurns", "").isdigit() and 1 <= int(fm["maxTurns"]) <= 5, f"{where}: maxTurns must be <= 5"


def claude_md_state(fm: dict, where: str):
    """TC-160: exactly one of (a) the documented key set true, (b) the recorded gap and no CLAUDE.md key."""
    assert KEYS_RECORD.is_file(), "claude-code-agent-keys.md is missing"
    record = KEYS_RECORD.read_text()
    extra = set(fm) - BASE_KEYS
    named = {k for k in CLAUDE_MD_KEY.findall(record)}
    if re.search(r"\bgap\b", record, re.IGNORECASE):
        assert not extra, f"{where}: gap recorded, yet frontmatter carries {sorted(extra)}"
        assert re.search(r"no documented key", record, re.IGNORECASE), "gap record must say 'no documented key'"
        assert re.search(r"source", record, re.IGNORECASE), "gap record must name the source checked"
    else:
        assert len(extra) == 1, f"{where}: documented-key state needs exactly one CLAUDE.md key, got {sorted(extra)}"
        (key,) = extra
        assert key in named, f"{where}: {key} is not named as documented in the record"
        assert fm[key] == "true", f"{where}: {key} must be true"
        assert re.search(r"source", record, re.IGNORECASE), "documented key must have its source"


# --- TC-160 ---------------------------------------------------------------------------

def test_tc160_plugin_runner_frontmatter_is_lean():
    assert RUNNER.is_file(), "plugin/agents/runner.md does not exist"
    fm = frontmatter(RUNNER.read_text())
    assert fm.get("name") == "runner" and fm.get("description")
    assert_lean_frontmatter(fm, "plugin/agents/runner.md")


def test_tc160_claude_md_key_matches_the_record():
    assert RUNNER.is_file(), "plugin/agents/runner.md does not exist"
    claude_md_state(frontmatter(RUNNER.read_text()), "plugin/agents/runner.md")


def test_tc160_vendored_copy_has_the_same_frontmatter(vendored):
    assert RUNNER.is_file(), "plugin/agents/runner.md does not exist"
    path = vendored / ".claude/agents/ck-runner.md"
    assert path.is_file(), "ck vendor did not write .claude/agents/ck-runner.md"
    fm, source = frontmatter(path.read_text()), frontmatter(RUNNER.read_text())
    assert_lean_frontmatter(fm, "ck-runner.md")
    for key in set(source) - {"name", "description"}:
        assert fm.get(key) == source[key], f"{key}: vendored {fm.get(key)!r} != plugin {source[key]!r}"
    assert set(fm) - {"name", "description"} == set(source) - {"name", "description"}
    claude_md_state(fm, "ck-runner.md")


def test_tc160_plugin_validate_passes():
    assert RUNNER.is_file(), "plugin/agents/runner.md does not exist"
    if shutil.which("claude") is None:
        return  # the existing plugin-validate test in test_plugin.py skips the same way
    for target in (PLUGIN, REPO):
        proc = subprocess.run(["claude", "plugin", "validate", str(target)], capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr


# --- TC-162 (b) -----------------------------------------------------------------------

def test_tc162_plugin_skill_passes_the_runner_agent():
    text = (PLUGIN / "skills/implement/SKILL.md").read_text()
    assert "runnerAgent" in text and "spec:runner" in text


def test_tc162_vendored_command_passes_the_runner_agent():
    text = (REPO / ".claude/commands/spec/implement.md").read_text()
    assert "runnerAgent" in text and "ck-runner" in text


def test_tc162_vendored_rendering_passes_ck_runner(vendored):
    text = (vendored / ".claude/commands/spec/implement.md").read_text()
    assert "runnerAgent" in text and "ck-runner" in text
    assert "spec:runner" not in text


def test_tc162_doctor_reports_a_missing_lean_agent(vendored):
    agent = vendored / ".claude/agents/ck-runner.md"
    assert agent.is_file(), "ck vendor did not write ck-runner.md"
    agent.unlink()
    out = run(vendored, "doctor")
    assert "ck-runner.md" in out.output, out.output
    assert out.exit_code != 0 or "ck vendor" in out.output or "ck agents sync" in out.output
