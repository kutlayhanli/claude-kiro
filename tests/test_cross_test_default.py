"""reviewer.cross_test default and opt-out in agent config (TC-64a, TC-66a; criteria 7.1, 7.3).

Runs the real `ck agents` CLI against an isolated CK_CONFIG_HOME and a real temp project.
"""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_kiro.cli.main import cli


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("CK_CONFIG_HOME", str(tmp_path / "prefs"))
    proj = tmp_path / "proj"
    (proj / "specs").mkdir(parents=True)
    return proj


def ck(cwd: Path, *args: str):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(cli, list(args))
    finally:
        os.chdir(old)


def reviewer(project: Path) -> dict:
    out = ck(project, "agents", "--json")
    assert out.exit_code == 0, out.output
    return json.loads(out.output)["roles"]["reviewer"]


def test_tc64_cross_test_defaults_true(project):
    assert reviewer(project)["cross_test"] is True


def test_tc66_cross_test_opt_out_global_then_project(project):
    assert reviewer(project)["cross_test"] is True
    out = ck(project, "agents", "set", "reviewer.cross_test", "false")
    assert out.exit_code == 0, out.output
    assert reviewer(project)["cross_test"] is False

    # a project value overrides the global one: turn it back on globally, off per project
    assert ck(project, "agents", "set", "reviewer.cross_test", "true").exit_code == 0
    assert reviewer(project)["cross_test"] is True
    out = ck(project, "agents", "set", "reviewer.cross_test", "false", "--project")
    assert out.exit_code == 0, out.output
    assert reviewer(project)["cross_test"] is False
