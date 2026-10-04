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


PRECOMMIT = """repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.6.9
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
      - id: ruff-check
        files: ^src/
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.6.0
    hooks:
      - id: trailing-whitespace
"""


def test_upgrade_limits_ruff_precommit_hooks_to_python(old_project):
    config = old_project / ".pre-commit-config.yaml"
    config.write_text(PRECOMMIT)
    dry = run(old_project, "upgrade", "--dry-run")
    assert "ruff limited to Python in 2 hook(s)" in dry.output
    assert config.read_text() == PRECOMMIT

    run(old_project, "upgrade")
    text = config.read_text()
    assert text.count("types_or: [python, pyi]") == 2
    assert "      - id: ruff\n        types_or: [python, pyi]\n        args: [--fix]\n" in text
    assert "      - id: ruff-format\n        types_or: [python, pyi]\n" in text
    assert "      - id: ruff-check\n        files: ^src/\n" in text  # already restricted: untouched
    assert "trailing-whitespace\n" in text

    again = run(old_project, "upgrade")
    assert "pre-commit" not in again.output  # idempotent


# --- version stamp (.claude/ck-manifest.json) ---------------------------------

from claude_kiro import manifest as ck_manifest


def stamp_of(root: Path) -> dict:
    return json.loads((root / ".claude/ck-manifest.json").read_text())


def test_upgrade_writes_version_stamp(old_project):
    run(old_project, "upgrade")
    stamp = stamp_of(old_project)
    assert stamp["version"] == ck_manifest.ck_version()
    implement = (old_project / ".claude/commands/spec/implement.md").read_text()
    assert stamp["files"][".claude/commands/spec/implement.md"] == ck_manifest.digest(implement)
    assert ".claude/workflows/spec-implement.js" in stamp["files"]


def test_older_ck_refuses_to_downgrade_newer_files(old_project):
    run(old_project, "upgrade")
    stamp = stamp_of(old_project)
    stamp["version"] = "99.0.0"
    (old_project / ".claude/ck-manifest.json").write_text(json.dumps(stamp))
    (old_project / ".claude/commands/spec/plan.md").write_text("pretend this is the newer ck's plan.md\n")

    refused = run(old_project, "upgrade")
    assert refused.exit_code == 1
    assert "written by ck 99.0.0" in refused.output and "--allow-downgrade" in refused.output
    assert (old_project / ".claude/commands/spec/plan.md").read_text() == "pretend this is the newer ck's plan.md\n"

    forced = run(old_project, "upgrade", "--allow-downgrade")
    assert forced.exit_code == 0, forced.output
    assert stamp_of(old_project)["version"] == ck_manifest.ck_version()


def test_hand_edits_to_tracked_managed_files_are_reported(old_project):
    run(old_project, "upgrade")
    git(old_project, "add", "-A")
    git(old_project, "commit", "-qm", "upgrade")
    review = old_project / ".claude/commands/spec/review.md"
    review.write_text(review.read_text() + "\nMy local tweak.\n")

    dry = run(old_project, "upgrade", "--dry-run")
    assert "Edited by hand since ck last wrote them (would replace" in dry.output
    assert ".claude/commands/spec/review.md" in dry.output
    assert "My local tweak." in review.read_text()


def test_init_stamps_fresh_projects_and_refuses_force_over_newer(tmp_path):
    result = run(tmp_path, "init")
    assert result.exit_code == 0, result.output
    assert stamp_of(tmp_path)["version"] == ck_manifest.ck_version()

    stamp = stamp_of(tmp_path)
    stamp["version"] = "99.0.0"
    (tmp_path / ".claude/ck-manifest.json").write_text(json.dumps(stamp))
    refused = run(tmp_path, "init", "--force")
    assert refused.exit_code == 1 and "99.0.0" in refused.output


def test_doctor_reports_version_mismatch(old_project):
    assert "No .claude/ck-manifest.json version stamp" in run(old_project, "doctor").output
    run(old_project, "upgrade")
    assert f"stamped by ck {ck_manifest.ck_version()}" in run(old_project, "doctor").output
    stamp = stamp_of(old_project)
    stamp["version"] = "99.0.0"
    (old_project / ".claude/ck-manifest.json").write_text(json.dumps(stamp))
    assert "reinstall ck before running 'ck upgrade'" in run(old_project, "doctor").output


def test_version_key_ordering():
    key = ck_manifest.version_key
    assert key("0.10.0") > key("0.9.9") > key("0.4.0")
    assert key("1.0.0rc1") == (1, 0, 0)
