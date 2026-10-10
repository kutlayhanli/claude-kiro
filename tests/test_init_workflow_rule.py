"""`ck init` offers a Workflow allow rule in .claude/settings.json, so unattended runs never wait on a prompt."""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_kiro.cli import main as ck_main
from claude_kiro.cli.main import cli


def init(cwd: Path, *args: str, input: str = None):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(cli, ["init", *args], input=input)
    finally:
        os.chdir(old)


def settings(project: Path):
    path = project / ".claude" / "settings.json"
    return json.loads(path.read_text()) if path.exists() else None


@pytest.fixture
def project(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='p'\n")
    return tmp_path


def test_flag_adds_the_rule(project):
    out = init(project, "--allow-workflow")
    assert out.exit_code == 0, out.output
    assert settings(project) == {"permissions": {"allow": ["Workflow"]}}
    assert ".claude/settings.json (allow: Workflow)" in out.output


def test_existing_settings_are_kept_and_the_rule_is_not_duplicated(project):
    (project / ".claude").mkdir()
    (project / ".claude/settings.json").write_text(
        json.dumps({"model": "opus", "permissions": {"defaultMode": "auto", "allow": ["Bash(ls:*)"]}})
    )
    init(project, "--allow-workflow")
    init(project, "--allow-workflow")
    data = settings(project)
    assert data["model"] == "opus" and data["permissions"]["defaultMode"] == "auto"
    assert data["permissions"]["allow"] == ["Bash(ls:*)", "Workflow"]


def test_no_flag_writes_nothing(project):
    out = init(project, "--no-allow-workflow")
    assert out.exit_code == 0
    assert settings(project) is None
    assert "ck init --allow-workflow" not in out.output  # an explicit no is not nagged


def test_unattended_without_a_flag_skips_the_question_and_says_how(project, monkeypatch):
    monkeypatch.setattr(ck_main, "_interactive", lambda: False)
    out = init(project)
    assert out.exit_code == 0
    assert settings(project) is None
    assert "ck init --allow-workflow" in out.output


@pytest.mark.parametrize("answer, expected", [("y\n", ["Workflow"]), ("n\n", None), ("\n", None)])
def test_interactive_asks_and_defaults_to_no(project, monkeypatch, answer, expected):
    monkeypatch.setattr(ck_main, "_interactive", lambda: True)
    out = init(project, input=answer)
    assert out.exit_code == 0, out.output
    assert "Allow the Workflow tool?" in out.output
    data = settings(project)
    assert (data["permissions"]["allow"] if data else None) == expected


def test_settings_that_are_not_an_object_are_left_alone(project):
    (project / ".claude").mkdir()
    (project / ".claude/settings.json").write_text("[1, 2]")
    out = init(project, "--allow-workflow")
    assert out.exit_code == 0
    assert (project / ".claude/settings.json").read_text() == "[1, 2]"
    assert "add \"Workflow\" to permissions.allow by hand" in out.output
