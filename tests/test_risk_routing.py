"""Risk routing: **Risk:** tags, risk_paths, `ck plan` risk/complexity maps, `ck lint` warnings, and the risk/complexity agent settings."""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_kiro.cli.main import cli
from claude_kiro.hooks._shared.spec_parser import parse_tasks


def ck(cwd: Path, *args: str):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(cli, list(args))
    finally:
        os.chdir(old)


def block(num, title, *, files=(), deps="None", risk=None, complexity=None, track="impl"):
    lines = [f"### Task {num}: {title}", "**Status:** Not Started", f"**Track:** {track}", "**Files:**"]
    lines += [f"- `{f}` - x" for f in files]
    if risk:
        lines.append(f"**Risk:** {risk}")
    lines.append(f"**Dependencies:** {deps}")
    if complexity:
        lines.append(f"**Complexity:** {complexity}")
    return "\n".join(lines) + "\n\n---\n\n"


def write_spec(root: Path, blocks: str, config: dict = None) -> Path:
    spec = root / "specs" / "shop"
    spec.mkdir(parents=True, exist_ok=True)
    (spec / "tasks.md").write_text("# Implementation Tasks: Shop\n\n## Task Breakdown\n\n" + blocks)
    if config is not None:
        (root / "specs" / "ck.json").write_text(json.dumps(config))
    return spec


def test_risk_and_complexity_fields_are_parsed(tmp_path):
    write_spec(
        tmp_path,
        block(1, "send", files=["src/send.py"], risk="safety (sends customer emails)", complexity="High")
        + block(2, "fmt", files=["src/fmt.py"], risk="normal", complexity="low")
        + block(3, "untagged", files=["src/x.py"])
        + block(4, "loud", risk="SAFETY - deletes records", complexity="Medium (two modules)"),
    )
    tasks = {t.num: t for t in parse_tasks(tmp_path / "specs/shop/tasks.md")}
    assert (tasks["1"].risk, tasks["1"].risk_reason, tasks["1"].complexity) == ("safety", "sends customer emails", "high")
    assert (tasks["2"].risk, tasks["2"].complexity) == ("normal", "low")
    assert (tasks["3"].risk, tasks["3"].risk_reason, tasks["3"].complexity) == ("normal", None, None)
    assert (tasks["4"].risk, tasks["4"].risk_reason, tasks["4"].complexity) == ("safety", "deletes records", "medium")


def test_plan_exposes_risk_and_complexity_maps(tmp_path):
    write_spec(
        tmp_path,
        block(1, "send", files=["src/send.py"], risk="safety (sends money)", complexity="High")
        + block(2, "fmt", files=["src/fmt.py"], complexity="Low", deps="Task 1 (formats its output)"),
    )
    plan = json.loads(ck(tmp_path, "plan", "shop", "--json").output)
    assert plan["risk"] == {"1": "safety", "2": "normal"}
    assert plan["complexity"] == {"1": "high", "2": "low"}


def test_risk_paths_make_an_untagged_task_safety_and_lint_warns(tmp_path):
    write_spec(
        tmp_path,
        block(1, "gate", files=["src/shop/gate.py"])  # untagged, matches **/gate.py
        + block(2, "sender", files=["sender_main.py"], risk="safety (sends orders)")  # tagged, matches send*.py at root
        + block(3, "fmt", files=["src/fmt.py"])
        + block(4, "root gate", files=["gate.py"]),  # **/ also matches the project root
        config={"verify": ["true"], "risk_paths": ["**/gate.py", "send*.py"]},
    )
    plan = json.loads(ck(tmp_path, "plan", "shop").output)
    assert plan["risk"] == {"1": "safety", "2": "safety", "3": "normal", "4": "safety"}
    assert [(u["task"], u["file"], u["pattern"]) for u in plan["riskUntagged"]] == [
        ("1", "src/shop/gate.py", "**/gate.py"),
        ("4", "gate.py", "**/gate.py"),
    ]

    out = ck(tmp_path, "lint", "shop")
    assert out.exit_code == 0, out.output  # a warning, not a cycle
    assert "Task 1 touches risk path src/shop/gate.py but has no **Risk:** safety" in out.output
    assert "Task 2 touches" not in out.output
    lint_json = json.loads(ck(tmp_path, "lint", "shop", "--json").output)
    assert [u["task"] for u in lint_json["riskUntagged"]] == ["1", "4"]


def test_no_risk_paths_means_no_warnings(tmp_path):
    write_spec(tmp_path, block(1, "gate", files=["src/gate.py"]))
    plan = json.loads(ck(tmp_path, "plan", "shop").output)
    assert plan["riskUntagged"] == [] and plan["risk"] == {"1": "normal"}
    assert "touches risk path" not in ck(tmp_path, "lint", "shop").output


# --- agent settings -------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CK_CONFIG_HOME", str(tmp_path / "prefs"))
    project = tmp_path / "proj"
    (project / "specs").mkdir(parents=True)
    return project


def roles(project: Path, *args: str):
    out = ck(project, "agents", "--json", *args)
    assert out.exit_code == 0, out.output
    return json.loads(out.output)["roles"]


def test_risk_defaults_leave_behaviour_unchanged(home):
    r = roles(home)
    assert (r["reviewer"]["model"], r["reviewer"]["effort"]) == ("opus", "high")
    assert (r["reviewer"]["risky_model"], r["reviewer"]["risky_effort"]) == ("opus", "high")
    assert r["implementer"]["risky_model"] is None
    assert r["implementer"]["complexity_routing"] is False


def test_recommended_tiered_config_is_accepted(home):
    for setting, value in [
        ("implementer.model", "haiku"),
        ("implementer.escalate", "sonnet,opus"),
        ("reviewer.model", "sonnet"),
        ("reviewer.risky_model", "opus"),
        ("reviewer.risky_effort", "xhigh"),
        ("implementer.risky_model", "opus"),
        ("implementer.complexity_routing", "true"),
    ]:
        out = ck(home, "agents", "set", setting, value)
        assert out.exit_code == 0, out.output
    r = roles(home)
    assert r["implementer"]["complexity_routing"] is True
    assert (r["reviewer"]["model"], r["reviewer"]["risky_model"], r["reviewer"]["risky_effort"]) == ("sonnet", "opus", "xhigh")
    assert r["implementer"]["risky_model"] == "opus"
    # the test writer follows the implementer's model, not its risk settings
    assert "risky_model" not in r["test_writer"]
    shown = ck(home, "agents").output
    assert "complexity_routing=True" in shown and "risky_model=opus" in shown


def test_unset_implementer_risky_model_is_displayed_as_the_ladder_top(home):
    assert "risky_model=(top of escalate, else model)" in ck(home, "agents").output


@pytest.mark.parametrize(
    "setting,value,message",
    [
        ("implementer.complexity_routing", "maybe", "true or false"),
        ("implementer.risky_model", "gpt-5", "unknown model"),
        ("implementer.risky_model", "inherit", "unknown model"),
        ("reviewer.risky_model", "gpt-5", "unknown model"),
        ("reviewer.risky_effort", "fast", "unknown effort"),
        ("fixer.risky_model", "opus", "unknown setting"),
    ],
)
def test_bad_risk_settings_are_refused(home, setting, value, message):
    out = ck(home, "agents", "set", setting, value)
    assert out.exit_code == 2
    assert message in out.output
