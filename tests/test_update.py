"""`ck update`: reinstall ck, add missing global files, upgrade the project, in that order."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from claude_kiro.cli import update as upd
from claude_kiro.cli.main import cli


@pytest.fixture
def calls(monkeypatch):
    """Record subprocess calls instead of running them; `fail` lists commands that exit 1."""
    log = {"cmds": [], "fail": set()}

    def fake_run(cmd, cwd=None):
        log["cmds"].append((cmd, cwd))
        return SimpleNamespace(returncode=1 if cmd[1] in log["fail"] or cmd[0] in log["fail"] else 0)

    monkeypatch.setattr(upd.subprocess, "run", fake_run)
    monkeypatch.setattr(upd.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.delenv("CK_SOURCE", raising=False)
    return log


def run(cwd: Path, *args: str):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(cli, ["update", *args])
    finally:
        os.chdir(old)


def project(tmp_path: Path) -> Path:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "ck-manifest.json").write_text("{}")
    return tmp_path


def test_dry_run_prints_the_plan_and_runs_nothing(tmp_path, calls):
    out = run(project(tmp_path), "--dry-run")
    assert out.exit_code == 0
    assert "uv tool install --force --reinstall git+https://github.com/kutlayhanli/claude-kiro" in out.output
    assert "ck setup" in out.output and "ck upgrade" in out.output
    assert calls["cmds"] == []


def test_in_a_project_reinstalls_then_sets_up_then_upgrades(tmp_path, calls):
    p = project(tmp_path)
    out = run(p)
    assert out.exit_code == 0, out.output
    cmds = [c for c, _ in calls["cmds"]]
    assert cmds[0] == ["uv", "tool", "install", "--force", "--reinstall", upd.DEFAULT_SOURCE]
    assert cmds[1] == ["/bin/ck", "setup"]
    assert cmds[2] == ["/bin/ck", "upgrade"] and calls["cmds"][2][1] == p
    assert "Restart open Claude Code sessions" in out.output


def test_outside_a_project_skips_upgrade(tmp_path, calls):
    out = run(tmp_path)
    assert out.exit_code == 0
    assert [c[1] for c, _ in calls["cmds"]] == ["tool", "setup"]
    assert "run `ck upgrade` in each project" in out.output


def test_no_project_flag_skips_upgrade(tmp_path, calls):
    run(project(tmp_path), "--no-project")
    assert all(c[1] != "upgrade" for c, _ in calls["cmds"])


def test_failed_reinstall_stops_before_touching_anything(tmp_path, calls):
    calls["fail"].add("tool")
    out = run(project(tmp_path))
    assert out.exit_code == 1
    assert len(calls["cmds"]) == 1
    assert "nothing else was changed" in out.output


def test_ref_and_source(tmp_path, calls, monkeypatch):
    monkeypatch.setenv("CK_SOURCE", "git+https://example.com/fork/claude-kiro")
    run(tmp_path, "--ref", "v0.5.0")
    assert calls["cmds"][0][0][-1] == "git+https://example.com/fork/claude-kiro@v0.5.0"


def test_missing_uv_is_reported(tmp_path, calls, monkeypatch):
    monkeypatch.setattr(upd.shutil, "which", lambda name: None)
    out = run(tmp_path)
    assert out.exit_code == 2 and "uv is not on PATH" in out.output
    assert calls["cmds"] == []
