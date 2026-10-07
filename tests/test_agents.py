"""`ck agents`: role defaults, precedence (global < project < override), `set`, and the planning check."""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_kiro.agents import meets_planning_bar, resolve
from claude_kiro.cli.main import cli


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated global preferences and an empty project."""
    monkeypatch.setenv("CK_CONFIG_HOME", str(tmp_path / "prefs"))
    project = tmp_path / "proj"
    (project / "specs").mkdir(parents=True)
    return project


def ck(cwd: Path, *args: str):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(cli, list(args))
    finally:
        os.chdir(old)


def roles(project: Path, *args: str):
    out = ck(project, "agents", "--json", *args)
    assert out.exit_code == 0, out.output
    return json.loads(out.output)


def test_defaults_sonnet_medium_implementer_opus_high_reviewer(home):
    r = roles(home)["roles"]
    assert (r["implementer"]["model"], r["implementer"]["effort"]) == ("sonnet", "medium")
    assert (r["reviewer"]["model"], r["reviewer"]["effort"], r["reviewer"]["enabled"]) == ("opus", "high", True)
    assert (r["planning"]["min_model"], r["planning"]["min_effort"], r["planning"]["ask"]) == ("opus", "medium", True)
    # test writer and fixer follow the implementer unless set
    assert (r["test_writer"]["model"], r["fixer"]["effort"]) == ("sonnet", "medium")


def test_precedence_global_then_project_then_override(home):
    assert ck(home, "agents", "set", "implementer.model", "haiku").exit_code == 0  # global
    assert ck(home, "agents", "set", "implementer.effort", "low").exit_code == 0  # global
    assert ck(home, "agents", "set", "implementer.effort", "high", "--project").exit_code == 0
    out = roles(home, "--override", "implementer.model=opus")
    assert (out["roles"]["implementer"]["model"], out["roles"]["implementer"]["effort"]) == ("opus", "high")
    assert out["sources"]["implementer.model"] == "override"
    assert out["sources"]["implementer.effort"] == "specs/ck.json"
    # the followers pick up the effective implementer value
    assert out["roles"]["test_writer"]["model"] == "opus"
    assert out["sources"]["test_writer.model"].startswith("implementer")

    prefs = json.loads((Path(os.environ["CK_CONFIG_HOME"]) / "config.json").read_text())
    assert prefs["agents"]["implementer"] == {"model": "haiku", "effort": "low"}
    assert json.loads((home / "specs/ck.json").read_text())["agents"]["implementer"] == {"effort": "high"}


def test_set_default_removes_a_setting(home):
    ck(home, "agents", "set", "reviewer.effort", "xhigh")
    assert roles(home)["roles"]["reviewer"]["effort"] == "xhigh"
    ck(home, "agents", "set", "reviewer.effort", "default")
    assert roles(home)["roles"]["reviewer"]["effort"] == "high"


def test_inherit_and_booleans(home):
    ck(home, "agents", "set", "implementer.model", "inherit")
    ck(home, "agents", "set", "reviewer.enabled", "false")
    ck(home, "agents", "set", "reviewer.rounds", "2")
    r = roles(home)["roles"]
    assert r["implementer"]["model"] == "inherit"
    assert r["reviewer"]["enabled"] is False and r["reviewer"]["rounds"] == 2


@pytest.mark.parametrize(
    "args,message",
    [
        (("set", "implementer.model", "gpt-5"), "unknown model"),
        (("set", "reviewer.effort", "fast"), "unknown effort"),
        (("set", "planning.min_model", "inherit"), "unknown model"),
        (("set", "reviewer.colour", "red"), "unknown setting"),
        (("set", "designer.model", "opus"), "unknown agent role"),
        (("--override", "implementer=haiku"), "role.key=value"),
    ],
)
def test_bad_values_are_refused(home, args, message):
    out = ck(home, "agents", *args)
    assert out.exit_code == 2
    assert message in out.output


def test_bad_project_config_is_reported(home):
    (home / "specs/ck.json").write_text(json.dumps({"agents": {"implementer": {"model": "gpt-5"}}}))
    out = ck(home, "agents")
    assert out.exit_code == 2 and "unknown model" in out.output


@pytest.mark.parametrize(
    "model,effort,ok",
    [
        ("claude-sonnet-5-5", "high", False),  # model below opus
        ("claude-haiku-5-5", None, False),
        ("claude-opus-5-5", "low", False),  # effort below medium
        ("claude-opus-5-5", "medium", True),
        ("claude-opus-5-5", None, True),  # effort unknown: judged on model alone
        ("claude-opus-5-5[1m]", "xhigh", True),
        ("claude-fable-5-1", "low", False),  # higher model, but effort below the minimum
        ("claude-fable-5-1", "high", True),
        ("some-other-model", "high", False),
    ],
)
def test_planning_bar(model, effort, ok):
    planning = {"min_model": "opus", "min_effort": "medium", "ask": True}
    assert meets_planning_bar(model, effort, planning)[0] is ok


def test_check_command_exit_codes_follow_configured_minimum(home):
    assert ck(home, "agents", "check", "--model", "claude-sonnet-5-5").exit_code == 1
    ck(home, "agents", "set", "planning.min_model", "sonnet", "--project")
    out = ck(home, "agents", "check", "--model", "claude-sonnet-5-5", "--effort", "medium", "--json")
    assert out.exit_code == 0 and json.loads(out.output)["ok"] is True


def test_resolve_without_any_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CK_CONFIG_HOME", str(tmp_path / "nowhere"))
    assert resolve(tmp_path)["roles"]["reviewer"]["model"] == "opus"
