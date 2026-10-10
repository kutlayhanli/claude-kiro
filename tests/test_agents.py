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


def test_max_concurrent_defaults_to_unlimited_and_is_settable(home):
    assert roles(home)["roles"]["run"]["max_concurrent"] is None  # no cap
    assert "max_concurrent=unlimited" in ck(home, "agents").output
    assert ck(home, "agents", "set", "run.max_concurrent", "8").exit_code == 0
    assert roles(home)["roles"]["run"]["max_concurrent"] == 8
    assert ck(home, "agents", "set", "run.max_concurrent", "3", "--project").exit_code == 0
    assert roles(home, "--override", "run.max_concurrent=2")["roles"]["run"]["max_concurrent"] == 2
    assert roles(home)["roles"]["run"]["max_concurrent"] == 3
    # "unlimited" is a value, so it can lift a cap set in a lower layer
    assert roles(home, "--override", "run.max_concurrent=unlimited")["roles"]["run"]["max_concurrent"] is None
    ck(home, "agents", "set", "run.max_concurrent", "unlimited", "--project")
    assert roles(home)["roles"]["run"]["max_concurrent"] is None


@pytest.mark.parametrize("value", ["0", "0.5", "many"])
def test_max_concurrent_must_be_a_positive_number(home, value):
    out = ck(home, "agents", "set", "run.max_concurrent", value)
    assert out.exit_code == 2 and "run.max_concurrent" in out.output


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


# --- agent types for running without the Workflow tool -----------------------

def frontmatter(path: Path) -> dict:
    head = path.read_text().split("---")[1]
    return dict(line.split(": ", 1) for line in head.strip().splitlines())


def test_sync_writes_one_agent_type_per_role_with_model_and_effort(home):
    out = ck(home, "agents", "sync")
    assert out.exit_code == 0, out.output
    agents_dir = home / ".claude" / "agents"
    impl = frontmatter(agents_dir / "ck-implementer.md")
    assert (impl["name"], impl["model"], impl["effort"]) == ("ck-implementer", "sonnet", "medium")
    assert "tools" not in impl
    review = frontmatter(agents_dir / "ck-reviewer.md")
    assert (review["model"], review["effort"]) == ("opus", "high")
    assert "Edit" not in review["tools"] and "Write" not in review["tools"]
    assert frontmatter(agents_dir / "ck-test-writer.md")["model"] == "sonnet"  # follows the implementer
    assert (agents_dir / "ck-fixer.md").exists()
    assert "restart" in out.output  # the agents directory is new


def test_sync_is_idempotent_and_follows_settings_and_overrides(home):
    ck(home, "agents", "sync")
    assert "already match" in ck(home, "agents", "sync").output
    ck(home, "agents", "sync", "--override", "implementer.effort=low")
    assert frontmatter(home / ".claude/agents/ck-implementer.md")["effort"] == "low"
    ck(home, "agents", "sync")
    assert frontmatter(home / ".claude/agents/ck-implementer.md")["effort"] == "medium"


def test_inherit_leaves_the_field_out(home):
    ck(home, "agents", "sync", "--override", "implementer.model=inherit", "--override", "implementer.effort=inherit")
    fm = frontmatter(home / ".claude/agents/ck-implementer.md")
    assert "model" not in fm and "effort" not in fm


def test_set_resyncs_agent_types_in_a_project(home):
    (home / ".claude").mkdir()
    out = ck(home, "agents", "set", "reviewer.model", "fable", "--project")
    assert out.exit_code == 0, out.output
    assert frontmatter(home / ".claude/agents/ck-reviewer.md")["model"] == "fable"


def test_set_outside_a_project_writes_no_agent_types(home):
    ck(home, "agents", "set", "implementer.model", "haiku")
    assert not (home / ".claude").exists()


def test_sync_leaves_hand_written_agent_files_alone(home):
    agents_dir = home / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "ck-reviewer.md").write_text("---\nname: ck-reviewer\nmodel: haiku\n---\nmine\n")
    out = ck(home, "agents", "sync", "--json")
    result = json.loads(out.output)
    assert result["skipped"] == ["ck-reviewer.md"] and result["created_dir"] is False
    assert (agents_dir / "ck-reviewer.md").read_text().endswith("mine\n")


def test_sync_dry_run_writes_nothing(home):
    out = ck(home, "agents", "sync", "--dry-run")
    assert "Would write" in out.output and "ck-implementer.md" in out.output
    assert not (home / ".claude" / "agents").exists()
