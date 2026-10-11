"""Projects in plugin mode (default) and vendored mode: ck init, ck vendor, ck upgrade migration, ck setup, ck doctor."""

import json
import os
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_kiro import agents as ag
from claude_kiro import manifest
from claude_kiro import plugin as pl
from claude_kiro.cli.main import cli

from conftest import git

MARKETPLACE = {"claude-kiro": {"source": {"source": "github", "repo": "kutlayhanli/claude-kiro"}}}


def run(cwd: Path, *args: str, input: str = None):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(cli, list(args), input=input)
    finally:
        os.chdir(old)


def shared_settings(root: Path) -> dict:
    path = root / ".claude/settings.json"
    return json.loads(path.read_text()) if path.exists() else {}


def local_hooks(root: Path) -> list:
    path = root / ".claude/settings.local.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [h["command"] for groups in data.get("hooks", {}).values() for g in groups for h in g["hooks"]]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='p'\n")
    (root / "uv.lock").write_text("")
    return root


# --- ck init -----------------------------------------------------------------------

def test_init_default_enables_the_plugin_and_copies_nothing(project):
    out = run(project, "init", "--no-allow-workflow")
    assert out.exit_code == 0, out.output
    settings = shared_settings(project)
    assert settings["enabledPlugins"] == {"spec@claude-kiro": True}
    assert settings["extraKnownMarketplaces"] == MARKETPLACE
    for managed in (".claude/commands", ".claude/workflows", ".claude/agents", ".claude/output-styles"):
        assert not (project / managed).exists(), managed
    assert json.loads((project / "specs/ck.json").read_text())["verify"] == ["uv run pytest -q"]
    assert not any("ck --hook" in c for c in local_hooks(project))  # the plugin brings the hooks
    assert manifest.read(project)["mode"] == "plugin"
    assert "ck setup" in out.output  # tells me to install the plugin once per machine


def test_init_keeps_other_settings(project):
    (project / ".claude").mkdir()
    (project / ".claude/settings.json").write_text(json.dumps({"model": "opus", "enabledPlugins": {"other@x": True}}))
    run(project, "init", "--allow-workflow")
    settings = shared_settings(project)
    assert settings["model"] == "opus"
    assert settings["enabledPlugins"] == {"other@x": True, "spec@claude-kiro": True}
    assert settings["permissions"]["allow"] == ["Workflow"]


def test_init_vendor_copies_the_plugin_into_claude_dir(project):
    out = run(project, "init", "--vendor", "--no-allow-workflow")
    assert out.exit_code == 0, out.output
    for target, content in pl.vendor_files().items():
        assert (project / target).read_text() == content, target
    impl = (project / ".claude/agents/ck-implementer.md").read_text()
    assert "model: sonnet" in impl and ag.GENERATED_MARK in impl
    assert "ck --hook pre-tool-guard" in local_hooks(project)
    stamp = manifest.read(project)
    assert stamp["mode"] == "vendor" and ".claude/commands/spec/implement.md" in stamp["files"]
    # vendored copies replace the plugin here, so a user-installed plugin can't duplicate /spec:*
    assert shared_settings(project)["enabledPlugins"] == {"spec@claude-kiro": False}


def test_vendor_command_switches_a_plugin_project_to_vendored(project):
    run(project, "init", "--no-allow-workflow")
    out = run(project, "vendor")
    assert out.exit_code == 0, out.output
    assert (project / ".claude/commands/spec/plan.md").exists()
    assert manifest.read(project)["mode"] == "vendor"
    assert shared_settings(project)["enabledPlugins"]["spec@claude-kiro"] is False


def test_agents_set_in_a_plugin_project_writes_no_agent_files(project):
    run(project, "init", "--no-allow-workflow")
    out = run(project, "agents", "set", "implementer.model", "haiku", "--project")
    assert out.exit_code == 0, out.output
    assert not (project / ".claude/agents").exists()


# --- ck upgrade: migrating a 0.8 project (managed copies) to the plugin -------------

LEGACY = {
    ".claude/commands/spec/plan.md": "plan as ck 0.8 wrote it\n",
    ".claude/commands/spec/create.md": "create as ck 0.8 wrote it\n",
    ".claude/commands/spec/review.md": "review as ck 0.8 wrote it\n",
    ".claude/commands/spawn-worktree.md": "spawn as ck 0.8 wrote it\n",
    ".claude/workflows/spec-create.js": "export const meta = {}\n",
    ".claude/workflows/spec-implement-brief.md": "brief\n",
    ".claude/output-styles/spec-driven.md": "style\n",
}


@pytest.fixture
def legacy(project: Path) -> Path:
    """A project as ck 0.8 left it: managed copies stamped in the manifest, ck-* agents, hooks in settings.local.json."""
    for rel, text in LEGACY.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(text)
    stamp = {"version": "0.8.0", "files": {rel: manifest.digest(text) for rel, text in LEGACY.items()}}
    (project / ".claude/ck-manifest.json").write_text(json.dumps(stamp))
    # edited by hand after ck wrote it
    (project / ".claude/commands/spec/review.md").write_text("review as ck 0.8 wrote it\nplus my tweak\n")
    agents = project / ".claude/agents"
    agents.mkdir()
    for name, content in ag.agent_type_files(ag.resolve(project)["roles"]).items():
        (agents / name).write_text(content)
    (agents / "ck-fixer.md").write_text("---\nname: ck-fixer\n---\nmy own fixer\n")  # hand-written: no mark
    (agents / "mine.md").write_text("---\nname: mine\n---\nunrelated\n")
    settings = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]}]}}
    (project / ".claude/settings.local.json").write_text(json.dumps(settings))
    (project / ".claude/CLAUDE.md").write_text("# mine\n")
    (project / "specs").mkdir()
    (project / "specs/ck.json").write_text(json.dumps({"verify": ["uv run pytest -q"]}))
    run(project, "agents", "sync")  # no-op for files present; keeps the fixture honest
    from claude_kiro.cli.main import _install_hooks

    _install_hooks(project)
    git(project, "init", "-q", "-b", "main")
    git(project, "config", "user.email", "t@example.com")
    git(project, "config", "user.name", "Test")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "ck 0.8 project")
    return project


def test_upgrade_dry_run_lists_the_migration_and_writes_nothing(legacy):
    before = git(legacy, "status", "--porcelain")
    out = run(legacy, "upgrade", "--dry-run")
    assert out.exit_code == 0, out.output
    assert "removed  .claude/commands/spec/plan.md" in out.output
    assert "removed  .claude/agents/ck-implementer.md" in out.output
    assert ".claude/commands/spec/review.md" in out.output.split("Kept")[1]
    assert git(legacy, "status", "--porcelain") == before
    assert (legacy / ".claude/commands/spec/plan.md").exists()


def test_upgrade_migrates_to_the_plugin_removing_only_unedited_copies(legacy):
    out = run(legacy, "upgrade")
    assert out.exit_code == 0, out.output
    for rel in LEGACY:
        if rel != ".claude/commands/spec/review.md":
            assert not (legacy / rel).exists(), rel
    # edited by hand: kept and listed
    assert (legacy / ".claude/commands/spec/review.md").read_text().endswith("plus my tweak\n")
    assert "Kept" in out.output and ".claude/commands/spec/review.md" in out.output
    # generated agent types removed, hand-written and unrelated ones kept
    agents = legacy / ".claude/agents"
    assert not (agents / "ck-implementer.md").exists() and not (agents / "ck-reviewer.md").exists()
    assert (agents / "ck-fixer.md").read_text().endswith("my own fixer\n")
    assert (agents / "mine.md").exists()
    # the plugin's hooks replace ck's settings.local.json hooks; foreign hooks stay
    assert local_hooks(legacy) == ["my-linter"]
    settings = shared_settings(legacy)
    assert settings["enabledPlugins"] == {"spec@claude-kiro": True}
    assert settings["extraKnownMarketplaces"] == MARKETPLACE
    assert manifest.read(legacy)["mode"] == "plugin"
    assert (legacy / ".claude/CLAUDE.md").read_text() == "# mine\n"
    assert not (legacy / ".claude/workflows").exists()  # emptied directories go too
    assert "git add -A .claude" in out.output or "commit" in out.output

    again = run(legacy, "upgrade")
    assert again.exit_code == 0, again.output
    assert "Already up to date" in again.output


def test_unstamped_copies_are_removed_only_when_identical_to_this_version(project):
    """ck 0.1-era projects have no hashes: a copy equal to this version's rendering is safe to remove."""
    files = pl.vendor_files()
    (project / ".claude/commands/spec").mkdir(parents=True)
    (project / ".claude/commands/spec/plan.md").write_text(files[".claude/commands/spec/plan.md"])
    (project / ".claude/commands/spec/create.md").write_text("old create command\n")
    out = run(project, "upgrade")
    assert out.exit_code == 0, out.output
    assert not (project / ".claude/commands/spec/plan.md").exists()
    assert (project / ".claude/commands/spec/create.md").exists()
    assert ".claude/commands/spec/create.md" in out.output.split("Kept")[1]


def test_upgrade_keeps_a_vendored_project_vendored_and_refreshes_it(project):
    run(project, "init", "--vendor", "--no-allow-workflow")
    plan = project / ".claude/commands/spec/plan.md"
    stamp = manifest.read(project)
    plan.write_text("stale copy from an older ck\n")
    stamp["files"][".claude/commands/spec/plan.md"] = manifest.digest("stale copy from an older ck\n")
    stamp["files"][".claude/commands/spawn-worktree.md"] = manifest.digest("old spawn\n")
    (project / ".claude/commands/spawn-worktree.md").write_text("old spawn\n")
    (project / ".claude/ck-manifest.json").write_text(json.dumps(stamp))

    out = run(project, "upgrade")
    assert out.exit_code == 0, out.output
    assert plan.read_text() == pl.vendor_files()[".claude/commands/spec/plan.md"]
    assert not (project / ".claude/commands/spawn-worktree.md").exists()  # moved to /spec:spawn-worktree
    assert manifest.read(project)["mode"] == "vendor"
    assert "Already up to date" in run(project, "upgrade").output


def test_upgrade_plugin_flag_migrates_a_vendored_project(project):
    run(project, "init", "--vendor", "--no-allow-workflow")
    out = run(project, "upgrade", "--plugin")
    assert out.exit_code == 0, out.output
    assert not (project / ".claude/commands").exists()
    assert not (project / ".claude/agents").exists()
    assert shared_settings(project)["enabledPlugins"] == {"spec@claude-kiro": True}
    assert manifest.read(project)["mode"] == "plugin"


def test_upgrade_vendor_flag_vendors_a_legacy_project(legacy):
    out = run(legacy, "upgrade", "--vendor")
    assert out.exit_code == 0, out.output
    assert (legacy / ".claude/commands/spec/implement.md").read_text() == pl.vendor_files()[".claude/commands/spec/implement.md"]
    assert manifest.read(legacy)["mode"] == "vendor"
    assert "ck --hook pre-tool-guard" in local_hooks(legacy)


# --- ck setup ------------------------------------------------------------------------

def _fake_claude(bin_dir: Path, plugins: list) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    script.write_text("#!/bin/sh\n" f"[ \"$1 $2\" = 'plugin list' ] && echo '{json.dumps(plugins)}' && exit 0\n" "echo \"unexpected: $*\" >&2; exit 3\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


def test_setup_dry_run_prints_the_plugin_install_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _fake_claude(tmp_path / "bin", [])
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin")
    out = run(tmp_path, "setup", "--dry-run")
    assert out.exit_code == 0, out.output
    assert "claude plugin marketplace add kutlayhanli/claude-kiro" in out.output
    assert "claude plugin install spec@claude-kiro --scope user" in out.output
    assert not (tmp_path / ".claude").exists()


def test_setup_dry_run_with_a_local_source_and_an_installed_plugin_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _fake_claude(tmp_path / "bin", [{"id": "spec@claude-kiro", "version": "0.8.9", "enabled": True, "scope": "user"}])
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin")
    out = run(tmp_path, "setup", "--dry-run", "--source", "/src/claude-kiro")
    assert out.exit_code == 0, out.output
    assert "claude plugin marketplace update claude-kiro" in out.output
    assert "claude plugin update spec@claude-kiro --scope user" in out.output
    assert "marketplace add" not in out.output


def test_setup_without_claude_on_path_says_what_to_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", f"{tmp_path / 'empty'}")
    out = run(tmp_path, "setup")
    assert out.exit_code == 1
    assert "claude plugin install spec@claude-kiro" in out.output
    assert (tmp_path / ".claude/CLAUDE.md").exists()  # the global CLAUDE.md is still installed


# --- ck doctor -----------------------------------------------------------------------

def test_doctor_reports_plugin_mode_and_the_installed_plugin(project, tmp_path, monkeypatch):
    run(project, "init", "--no-allow-workflow")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _fake_claude(tmp_path / "bin", [{"id": "spec@claude-kiro", "version": manifest.ck_version(), "enabled": True, "scope": "user"}])
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    out = run(project, "doctor")
    assert "Mode: plugin" in out.output
    assert "spec@claude-kiro installed" in out.output
    assert "Missing files" not in out.output and "settings.local.json not found" not in out.output


def test_doctor_falls_back_to_the_plugins_directory(project, tmp_path, monkeypatch):
    run(project, "init", "--no-allow-workflow")
    home = tmp_path / "home"
    (home / ".claude/plugins").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{tmp_path / 'nothing'}")
    out = run(project, "doctor")
    assert "spec@claude-kiro is not installed" in out.output and "ck setup" in out.output
    (home / ".claude/plugins/installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {"spec@claude-kiro": [{"scope": "user", "version": "0.9.0"}]}})
    )
    assert "spec@claude-kiro installed" in run(project, "doctor").output


def test_doctor_reports_vendored_and_legacy_modes(project, legacy):
    assert "managed copies from ck 0.8.0" in run(legacy, "doctor").output
    vendored = project.parent / "vendored"
    vendored.mkdir()
    run(vendored, "init", "--vendor", "--no-allow-workflow")
    out = run(vendored, "doctor")
    assert "Mode: vendored" in out.output and "Missing files" not in out.output


# --- plugin hooks only act in ck projects ---------------------------------------------

def test_plugin_hooks_stay_silent_outside_ck_projects(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_x.py").write_text("def test_x():\n    assert True\n")
    payload = json.dumps({"cwd": str(tmp_path), "tool_name": "Bash", "tool_input": {"command": "rm tests/test_x.py"}, "session_id": "s"})
    quiet = CliRunner().invoke(cli, ["--hook", "pre-tool-guard", "--plugin-hook"], input=payload)
    assert quiet.exit_code == 0 and quiet.output.strip() == ""
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs/ck.json").write_text("{}")
    guarded = CliRunner().invoke(cli, ["--hook", "pre-tool-guard", "--plugin-hook"], input=payload)
    assert guarded.exit_code == 0 and "permissionDecision" in guarded.output


def test_init_default_removes_ck_hooks_that_would_run_twice(project):
    (project / ".claude").mkdir()
    old = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "ck --hook stop-verify", "timeout": 600}]}],
                     "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]}]}}
    (project / ".claude/settings.local.json").write_text(json.dumps(old))
    out = run(project, "init", "--no-allow-workflow")
    assert out.exit_code == 0, out.output
    assert local_hooks(project) == ["my-linter"]
