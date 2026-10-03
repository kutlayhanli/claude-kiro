"""`ck upgrade` on a project set up by ck 0.1.x, and `ck setup --diff` on an existing machine."""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_kiro.cli.main import cli

from conftest import git

OLD_SETTINGS = {
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]}],
        "PostToolUse": [
            {"matcher": "Edit|Write|MultiEdit", "hooks": [{"type": "command", "command": "ck --hook post-file-ops", "timeout": 5000}]}
        ],
    },
    "permissions": {"allow": ["Bash(ls:*)"]},
}


def run(cwd: Path, *args: str):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(cli, list(args))
    finally:
        os.chdir(old)


@pytest.fixture
def old_project(tmp_path: Path) -> Path:
    """A repo as ck 0.1.x left it: specs in .claude/specs, ms hook timeout, no workflow."""
    root = tmp_path / "legacy"
    (root / ".claude/commands/spec").mkdir(parents=True)
    (root / ".claude/output-styles").mkdir()
    (root / ".claude/CLAUDE.md").write_text("# My project\nCustom notes I wrote.\n")
    (root / ".claude/commands/spec/create.md").write_text("old create command\n")
    (root / ".claude/commands/spec/plan.md").write_text("old plan command\n")
    (root / ".claude/settings.local.json").write_text(json.dumps(OLD_SETTINGS))
    (root / ".claude/specs/auth").mkdir(parents=True)
    (root / ".claude/specs/auth/tasks.md").write_text("### Task 1: x\nSee .claude/specs/auth/design.md\n")
    (root / "pyproject.toml").write_text("[project]\nname='legacy'\n")
    (root / "uv.lock").write_text("")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "ck 0.1 project")
    return root


def test_dry_run_reports_plan_and_writes_nothing(old_project):
    before = git(old_project, "status", "--porcelain")
    result = run(old_project, "upgrade", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "updated  .claude/commands/spec/create.md" in result.output
    assert "added  .claude/workflows/spec-create.js" in result.output
    assert "moved  .claude/specs/auth -> specs/auth" in result.output
    assert git(old_project, "status", "--porcelain") == before
    assert not (old_project / "specs").exists()


def test_upgrade_refreshes_managed_files_and_keeps_project_files(old_project):
    result = run(old_project, "upgrade")
    assert result.exit_code == 0, result.output

    # project-owned file untouched
    assert (old_project / ".claude/CLAUDE.md").read_text() == "# My project\nCustom notes I wrote.\n"
    # managed files refreshed / added
    assert "ck gate" in (old_project / ".claude/commands/spec/implement.md").read_text()
    assert "Workflow" in (old_project / ".claude/commands/spec/create.md").read_text()
    assert (old_project / ".claude/workflows/spec-create.js").read_text().startswith("export const meta")

    # hooks merged: foreign hook and other settings kept, ms timeout fixed, new hooks added
    settings = json.loads((old_project / ".claude/settings.local.json").read_text())
    assert settings["permissions"] == {"allow": ["Bash(ls:*)"]}
    pre = [h["command"] for g in settings["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert "my-linter" in pre and "ck --hook pre-tool-guard" in pre
    post = [h for g in settings["hooks"]["PostToolUse"] for h in g["hooks"]]
    assert post == [{"type": "command", "command": "ck --hook post-file-ops", "timeout": 10}]
    assert "Stop" in settings["hooks"] and "SubagentStop" in settings["hooks"]

    # config created with detected verify command
    assert json.loads((old_project / "specs/ck.json").read_text())["verify"] == ["uv run pytest -q"]

    # specs moved with history, references rewritten
    assert not (old_project / ".claude/specs").exists()
    assert "See specs/auth/design.md" in (old_project / "specs/auth/tasks.md").read_text()
    assert "R  .claude/specs/auth/tasks.md -> specs/auth/tasks.md" in git(old_project, "status", "--porcelain").replace("RM", "R ")


def test_upgrade_is_idempotent(old_project):
    run(old_project, "upgrade")
    result = run(old_project, "upgrade")
    assert result.exit_code == 0
    assert "Already up to date" in result.output


def test_untracked_customized_command_is_backed_up(old_project):
    custom = old_project / ".claude/commands/spawn-worktree.md"
    custom.write_text("my own spawn-worktree tweaks\n")  # untracked
    run(old_project, "upgrade")
    assert (old_project / ".claude/commands/spawn-worktree.md.bak").read_text() == "my own spawn-worktree tweaks\n"
    assert "Wave Gate" in custom.read_text()


def test_no_migrate_leaves_specs(old_project):
    result = run(old_project, "upgrade", "--no-migrate")
    assert (old_project / ".claude/specs/auth/tasks.md").exists()
    assert "run 'ck migrate' when ready" in result.output


def test_upgrade_refuses_uninitialized_dir(tmp_path):
    result = run(tmp_path, "upgrade")
    assert result.exit_code == 1
    assert "ck init" in result.output


def test_setup_diff_shows_changes_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    claude_md = tmp_path / ".claude/CLAUDE.md"
    claude_md.parent.mkdir()
    claude_md.write_text("# My global notes\n- Specs live in `.claude/specs/[feature-name]/`\n")
    result = run(tmp_path, "setup", "--diff")
    assert result.exit_code == 0, result.output
    assert "-- Specs live in `.claude/specs/[feature-name]/`" in result.output
    assert "+- Specs live in `specs/[feature-name]/`" in result.output
    assert "does not exist" in result.output  # skill not installed in this fake HOME
    assert claude_md.read_text().startswith("# My global notes")
    assert not (tmp_path / ".claude/skills").exists()
