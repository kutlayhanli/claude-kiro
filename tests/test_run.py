"""`ck run`: start a whole-spec run headless and unattended, from the main checkout."""

import json
import os
import subprocess
from pathlib import Path

from click.testing import CliRunner

from claude_kiro.cli.main import cli


def ck(cwd: Path, *args: str):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(cli, list(args))
    finally:
        os.chdir(old)


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "specs" / "demo").mkdir(parents=True)
    (root / "specs" / "demo" / "tasks.md").write_text("# Tasks\n\n### Task 1: a\n**Status:** Not Started\n**Dependencies:** None\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    return root


def test_dry_run_prints_an_unattended_headless_command(tmp_path):
    root = repo(tmp_path)
    out = ck(root, "run", "demo", "--dry-run", "--json")
    assert out.exit_code == 0, out.output
    plan = json.loads(out.output)
    argv = plan["argv"]
    assert argv[:2] == ["claude", "-p"]
    assert argv[2] == "/spec:implement demo all"
    for flag in (["--permission-mode", "auto"], ["--permission-prompts", "none"], ["--model", "sonnet"], ["--effort", "low"]):
        i = argv.index(flag[0])
        assert argv[i + 1] == flag[1]
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert "Workflow" in allowed and "Bash(ck *)" in allowed
    assert plan["env"]["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "0"
    assert plan["cwd"] == str(root)
    assert plan["log"].startswith(str(root / ".claude" / "ck-runs" / "demo-"))


def test_extra_arguments_reach_spec_implement_and_session_model_is_settable(tmp_path):
    root = repo(tmp_path)
    out = ck(root, "run", "demo", "--dry-run", "--json", "--model", "haiku", "--", "--escalate", "sonnet,opus", "--max-concurrent", "6")
    argv = json.loads(out.output)["argv"]
    assert argv[2] == "/spec:implement demo all --escalate sonnet,opus --max-concurrent 6"
    assert argv[argv.index("--model") + 1] == "haiku"


def test_refuses_inside_a_linked_worktree(tmp_path):
    root = repo(tmp_path)
    wt = root / ".claude" / "worktrees" / "demo-task-1"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "-b", "t1", str(wt)], check=True)
    out = ck(wt, "run", "demo", "--dry-run")
    assert out.exit_code == 2
    assert "main checkout" in out.output


def test_unknown_spec_is_refused(tmp_path):
    root = repo(tmp_path)
    out = ck(root, "run", "nope", "--dry-run")
    assert out.exit_code == 2 and "nope" in out.output


# --- Safety guards for unattended runs --------------------------------------

import shutil  # noqa: E402

import pytest  # noqa: E402

REAL_WHICH = shutil.which


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A HOME with nothing in it, so the developer's real ~/.ssh, ~/.claude.json etc. never leak into a test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def sandbox_tools(monkeypatch, present: bool):
    def which(name, *a, **k):
        if name in ("bwrap", "socat"):
            return f"/usr/bin/{name}" if present else None
        return REAL_WHICH(name, *a, **k)

    monkeypatch.setattr(shutil, "which", which)


def plan_of(root, *args):
    out = ck(root, "run", "demo", "--dry-run", "--json", *args)
    assert out.exit_code == 0, out.output
    return json.loads(out.output)


def flag(argv, name):
    return argv[argv.index(name) + 1]


def test_dry_run_caps_budget_and_turns(tmp_path, fake_home, monkeypatch):
    sandbox_tools(monkeypatch, present=False)
    root = repo(tmp_path)
    argv = plan_of(root)["argv"]
    assert flag(argv, "--max-budget-usd") == "40"
    assert flag(argv, "--max-turns") == "400"
    argv = plan_of(root, "--budget", "12.5", "--max-turns", "50")["argv"]
    assert flag(argv, "--max-budget-usd") == "12.5"
    assert flag(argv, "--max-turns") == "50"


def test_dry_run_denies_dangerous_commands_and_credential_reads(tmp_path, fake_home, monkeypatch):
    sandbox_tools(monkeypatch, present=False)
    root = repo(tmp_path)
    plan = plan_of(root)
    denied = flag(plan["argv"], "--disallowedTools").split(",")
    for rule in (
        "Bash(git push *)", "Bash(git -C * push *)", "Bash(git reset --hard *)", "Bash(git -C * reset --hard *)",
        "Bash(git clean *)", "Bash(git worktree remove --force *)", "Bash(git -c *)", "Bash(git -C * -c *)",
        "Bash(curl *)", "Bash(wget *)", "Bash(ssh *)", "Bash(scp *)", "Bash(rsync *)", "Bash(nc *)",
        "Read(~/.ssh/**)", "Edit(~/.ssh/**)", "Read(~/.aws/**)", "Read(~/.config/gcloud/**)", "Read(**/.env)",
        "Edit(**/.claude/settings*.json)", "WebFetch",
    ):
        assert rule in denied, rule
    assert plan["env"]["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "1"
    assert plan["env"]["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "0"


def test_git_allow_rules_are_narrow(tmp_path, fake_home, monkeypatch):
    sandbox_tools(monkeypatch, present=False)
    root = repo(tmp_path)
    allowed = flag(plan_of(root)["argv"], "--allowedTools").split(",")
    assert "Bash(git *)" not in allowed and "Bash(command git *)" not in allowed
    for rule in ("Bash(git -C * merge *)", "Bash(git -C * commit *)", "Bash(git -C * status *)", "Bash(git -C * log *)",
                 "Bash(git -C * diff *)", "Bash(git -C * add *)", "Bash(git status *)", "Bash(git log *)", "Bash(git diff *)"):
        assert rule in allowed, rule
    assert not any("push" in r or "reset" in r or "clean" in r for r in allowed)


def test_isolation_auto_runs_unsandboxed_with_a_warning_when_bwrap_is_missing(tmp_path, fake_home, monkeypatch):
    sandbox_tools(monkeypatch, present=False)
    root = repo(tmp_path)
    plan = plan_of(root)
    assert plan["isolation"] == "none"
    assert "--settings" not in plan["argv"]
    assert any("bwrap" in w or "bubblewrap" in w for w in plan["warnings"])


def test_isolation_auto_enables_a_strict_sandbox_when_bwrap_and_socat_exist(tmp_path, fake_home, monkeypatch):
    sandbox_tools(monkeypatch, present=True)
    root = repo(tmp_path)
    plan = plan_of(root)
    assert plan["isolation"] == "sandbox"
    settings = json.loads(flag(plan["argv"], "--settings"))
    sb = settings["sandbox"]
    assert sb["enabled"] is True and sb["failIfUnavailable"] is True
    assert sb["allowUnsandboxedCommands"] is False and sb["autoAllowBashIfSandboxed"] is True
    assert "~/" in sb["filesystem"]["denyRead"]
    assert str(root) in sb["filesystem"]["allowRead"]
    assert str(root / ".claude" / "worktrees") in sb["filesystem"]["allowRead"]
    assert "pypi.org" in sb["network"]["allowedDomains"] and "registry.npmjs.org" in sb["network"]["allowedDomains"]
    assert sb["network"]["strictAllowlist"] is True
    assert plan["settings"] == settings


def test_isolation_sandbox_refuses_when_bwrap_or_socat_is_missing(tmp_path, fake_home, monkeypatch):
    sandbox_tools(monkeypatch, present=False)
    root = repo(tmp_path)
    out = ck(root, "run", "demo", "--dry-run", "--json", "--isolation", "sandbox")
    assert out.exit_code == 2
    assert "bwrap" in out.output and "socat" in out.output


def test_isolation_none_is_explicit_and_warns_about_credential_dirs(tmp_path, fake_home, monkeypatch):
    sandbox_tools(monkeypatch, present=True)
    (fake_home / ".ssh").mkdir()
    root = repo(tmp_path)
    plan = plan_of(root, "--isolation", "none")
    assert plan["isolation"] == "none" and "--settings" not in plan["argv"]
    assert any("~/.ssh" in w for w in plan["warnings"])


def test_allowed_domains_come_from_ck_agents(tmp_path, fake_home, monkeypatch):
    sandbox_tools(monkeypatch, present=True)
    root = repo(tmp_path)
    assert ck(root, "agents", "set", "run.allowed_domains", "example.com,pypi.org", "--project").exit_code == 0
    settings = json.loads(flag(plan_of(root)["argv"], "--settings"))
    assert settings["sandbox"]["network"]["allowedDomains"] == ["example.com", "pypi.org"]


def test_untrusted_workspace_is_warned_about(tmp_path, fake_home, monkeypatch):
    sandbox_tools(monkeypatch, present=True)
    root = repo(tmp_path)
    claude_json = fake_home / ".claude.json"
    claude_json.write_text(json.dumps({"projects": {str(root): {"hasTrustDialogAccepted": False}}}))
    assert any("trust" in w.lower() for w in plan_of(root)["warnings"])
    claude_json.write_text(json.dumps({"projects": {str(root): {"hasTrustDialogAccepted": True}}}))
    assert not any("trust" in w.lower() for w in plan_of(root)["warnings"])


def test_a_real_launch_records_the_isolation_warning_in_the_log(tmp_path, fake_home, monkeypatch):
    sandbox_tools(monkeypatch, present=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text("#!/bin/sh\necho fake-claude \"$@\"\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    root = repo(tmp_path)
    out = ck(root, "run", "demo")
    assert out.exit_code == 0, out.output
    assert "UNSANDBOXED" in out.output
    logs = list((root / ".claude" / "ck-runs").glob("demo-*.log"))
    assert len(logs) == 1
    text = logs[0].read_text()
    assert "UNSANDBOXED" in text and "isolation: none" in text
    assert "fake-claude" in text and "--max-budget-usd" in text


def test_doctor_unattended_reports_sandbox_tools_and_risky_settings(tmp_path, fake_home, monkeypatch):
    sandbox_tools(monkeypatch, present=False)
    (fake_home / ".ssh").mkdir()
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash", "Read"]}}))
    root = repo(tmp_path)
    out = ck(root, "doctor", "--unattended")
    assert out.exit_code == 0, out.output
    assert "bwrap" in out.output and "socat" in out.output
    assert "apt-get install bubblewrap socat" in out.output
    assert "~/.ssh" in out.output
    assert "Bash" in out.output and "settings.json" in out.output
    assert "Recommendations" in out.output


def test_doctor_unattended_is_quiet_when_sandbox_tools_exist(tmp_path, fake_home, monkeypatch):
    sandbox_tools(monkeypatch, present=True)
    root = repo(tmp_path)
    out = ck(root, "doctor", "--unattended")
    assert out.exit_code == 0, out.output
    assert "apt-get install" not in out.output
    assert "sandbox" in out.output.lower()


def test_a_trusted_parent_directory_counts_as_trusted(tmp_path, fake_home, monkeypatch):
    sandbox_tools(monkeypatch, present=True)
    root = repo(tmp_path)
    (fake_home / ".claude.json").write_text(json.dumps({"projects": {str(tmp_path): {"hasTrustDialogAccepted": True}}}))
    assert not any("trust" in w.lower() for w in plan_of(root)["warnings"])
