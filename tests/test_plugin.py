"""The `spec` Claude Code plugin in plugin/: structure, single source of truth, the bin/ck shim."""

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from claude_kiro import agents as ag
from claude_kiro import manifest
from claude_kiro import plugin as pl
from claude_kiro.cli.runner import HOOK_SETTINGS

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin"
SKILLS = ["plan", "create", "implement", "review", "spawn-worktree"]
AGENTS = {"implementer": "implementer", "test-writer": "test_writer", "fixer": "fixer", "resolver": "resolver", "reviewer": "reviewer"}
WORKFLOWS = ["spec-create", "spec-implement"]


def frontmatter(text: str) -> dict:
    head = text.split("---")[1]
    out = {}
    for line in head.strip().splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            out[key.strip()] = value.strip()
    return out


# --- structure ---------------------------------------------------------------

def test_plugin_manifest_names_the_plugin_spec_at_the_package_version():
    data = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    assert data["name"] == "spec"
    assert data["version"] == manifest.ck_version()
    assert data["description"] and data["author"]["name"]


def test_marketplace_lists_the_plugin_at_plugin_dir():
    data = json.loads((REPO / ".claude-plugin/marketplace.json").read_text())
    assert data["name"] == "claude-kiro"
    entry = {p["name"]: p for p in data["plugins"]}["spec"]
    assert entry["source"] == "./plugin"
    assert pl.PLUGIN_ID == "spec@claude-kiro"


def test_every_skill_agent_workflow_resource_and_hook_is_present():
    for name in SKILLS:
        fm = frontmatter((PLUGIN / "skills" / name / "SKILL.md").read_text())
        assert fm.get("description"), name
    for name in AGENTS:
        fm = frontmatter((PLUGIN / "agents" / f"{name}.md").read_text())
        assert fm["name"] == name and fm["description"]
    for name in WORKFLOWS:
        text = (PLUGIN / "workflows" / f"{name}.js").read_text()
        assert text.startswith("export const meta") and f"name: '{name}'" in text
    # Only workflow scripts in workflows/: Markdown there would be parsed as a workflow.
    assert sorted(p.name for p in (PLUGIN / "workflows").iterdir()) == ["spec-create.js", "spec-implement.js"]
    assert (PLUGIN / "resources/spec-implement-brief.md").exists()
    assert (PLUGIN / "resources/without-workflow-tool.md").exists()
    assert frontmatter((PLUGIN / "output-styles/spec-driven.md").read_text())["name"]
    assert os.access(PLUGIN / "bin/ck", os.X_OK)


def test_skill_frontmatter_keeps_argument_hints_and_tools():
    implement = frontmatter((PLUGIN / "skills/implement/SKILL.md").read_text())
    assert "Workflow" in implement["allowed-tools"] and "--no-workflow" in implement["argument-hint"]
    assert "$ARGUMENTS" in (PLUGIN / "skills/review/SKILL.md").read_text()


def test_agent_defaults_match_ck_agents_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("CK_CONFIG_HOME", str(tmp_path / "none"))
    roles = ag.resolve(tmp_path)["roles"]
    for name, role in AGENTS.items():
        fm = frontmatter((PLUGIN / "agents" / f"{name}.md").read_text())
        assert (fm["model"], fm["effort"]) == (roles[role]["model"], roles[role]["effort"]), name
    reviewer = frontmatter((PLUGIN / "agents/reviewer.md").read_text())
    assert "Edit" not in reviewer["tools"] and "Write" not in reviewer["tools"]


def test_hooks_json_reproduces_the_hooks_ck_init_configured():
    hooks = json.loads((PLUGIN / "hooks/hooks.json").read_text())["hooks"]
    found = []
    for event, groups in hooks.items():
        for group in groups:
            for h in group["hooks"]:
                assert h["command"].startswith('"${CLAUDE_PLUGIN_ROOT}/bin/ck" --hook ')
                found.append({"event": event, "matcher": group.get("matcher"), "hook": h["command"].split()[2], "timeout": h["timeout"]})
    assert sorted(found, key=json.dumps) == sorted(HOOK_SETTINGS, key=json.dumps)


def test_skills_use_the_plugin_root_and_namespaced_workflows():
    implement = (PLUGIN / "skills/implement/SKILL.md").read_text()
    create = (PLUGIN / "skills/create/SKILL.md").read_text()
    review = (PLUGIN / "skills/review/SKILL.md").read_text()
    assert 'name: "spec:spec-implement"' in implement
    assert 'name: "spec:spec-create"' in create and 'name: "spec:spec-create"' in review
    assert '"brief": "${CLAUDE_PLUGIN_ROOT}/resources/spec-implement-brief.md"' in implement
    for text in (implement, create, review):
        assert "${CLAUDE_PLUGIN_ROOT}/resources/without-workflow-tool.md" in text
        assert "scriptPath" in text  # fallback for vendored projects
    for name in SKILLS:
        text = (PLUGIN / "skills" / name / "SKILL.md").read_text()
        assert "ck-implementer" not in text and "ck-reviewer" not in text, name
        assert "/spawn-worktree" not in text.replace("/spec:spawn-worktree", ""), name
    guide = (PLUGIN / "resources/without-workflow-tool.md").read_text()
    for agent in AGENTS:
        assert f"`spec:{agent}`" in guide


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
@pytest.mark.parametrize("target", [PLUGIN, REPO])
def test_claude_plugin_validate(target):
    proc = subprocess.run(["claude", "plugin", "validate", str(target)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- one source of truth -------------------------------------------------------

def test_package_finds_the_plugin_source():
    assert pl.root().resolve() == PLUGIN.resolve() or (pl.root() / ".claude-plugin/plugin.json").exists()
    assert pl.version() == manifest.ck_version()


def test_no_copy_of_plugin_content_lives_in_the_python_package():
    templates = REPO / "src/claude_kiro/resources/templates"
    plugin_texts = {p.read_text() for p in PLUGIN.rglob("*") if p.is_file()}
    for path in templates.rglob("*"):
        if path.is_file() and path.suffix in (".md", ".js"):
            assert path.read_text() not in plugin_texts, path
    assert not (templates / "commands").exists() and not (templates / "workflows").exists()


def test_vendored_rendering_has_no_plugin_placeholders_and_local_agent_types():
    files = pl.vendor_files()
    assert set(files) == {target for target, _ in pl.VENDOR_MAP}
    for target, text in files.items():
        if target.endswith(".md"):
            assert "${CLAUDE_PLUGIN_ROOT}" not in text, target
            assert "<plugin-root>" not in text and "plugin-only" not in text, target
            assert not re.search(r"`spec:(implementer|test-writer|fixer|resolver|reviewer)`", text), target
    implement = files[".claude/commands/spec/implement.md"]
    assert '"brief": ".claude/workflows/spec-implement-brief.md"' in implement
    assert ".claude/commands/spec/implement.md" in implement
    assert "`ck-implementer`" in files[".claude/workflows/without-workflow-tool.md"]
    # scripts are copied verbatim
    assert files[".claude/workflows/spec-implement.js"] == (PLUGIN / "workflows/spec-implement.js").read_text()


def test_dogfood_copies_in_this_repo_match_the_plugin_source():
    """This repo's .claude/ is a vendored copy (`ck vendor`). Edit plugin/, then run `uv run ck vendor`."""
    drift = []
    for target, content in pl.vendor_files().items():
        path = REPO / target
        if not path.exists() or path.read_text() != content:
            drift.append(target)
    for name, content in ag.agent_type_files(ag.resolve(REPO)["roles"]).items():
        path = REPO / ag.AGENTS_DIR / name
        if not path.exists() or path.read_text() != content:
            drift.append(f"{ag.AGENTS_DIR}/{name}")
    assert not drift, f"out of date with plugin/ (run `uv run ck vendor`): {drift}"
    stale = (REPO / ".claude/commands/spawn-worktree.md")
    assert not stale.exists()


def test_generated_agent_types_take_their_prompts_from_the_plugin_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("CK_CONFIG_HOME", str(tmp_path / "none"))
    files = ag.agent_type_files(ag.resolve(tmp_path)["roles"])
    assert sorted(files) == sorted(f"ck-{n}.md" for n in AGENTS)
    for name in AGENTS:
        body = (PLUGIN / "agents" / f"{name}.md").read_text().split("---", 2)[2].strip()
        assert body in files[f"ck-{name}.md"]
        assert ag.GENERATED_MARK in files[f"ck-{name}.md"]


# --- bin/ck shim -----------------------------------------------------------------

def _fake_ck(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fake = directory / "ck"
    fake.write_text('#!/bin/sh\necho "real ck $*"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def _shim(args, path_dirs, home):
    env = {"PATH": os.pathsep.join(str(d) for d in path_dirs), "HOME": str(home)}
    return subprocess.run(["/bin/sh", str(PLUGIN / "bin/ck"), *args], capture_output=True, text=True, env=env, timeout=20)


def test_shim_runs_the_installed_ck_and_skips_itself(tmp_path):
    real = tmp_path / "real-bin"
    _fake_ck(real)
    out = _shim(["gate", "demo"], [PLUGIN / "bin", "/usr/bin", "/bin", real], tmp_path)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "real ck gate demo"


def test_shim_explains_how_to_install_ck_when_missing(tmp_path):
    out = _shim(["doctor"], [PLUGIN / "bin", "/usr/bin", "/bin"], tmp_path)
    assert out.returncode == 127
    assert "uv tool install" in out.stderr and "claude-kiro" in out.stderr
    hook = _shim(["--hook", "stop-verify"], [PLUGIN / "bin", "/usr/bin", "/bin"], tmp_path)
    assert hook.returncode == 0  # a missing ck never blocks a hook


def test_shim_falls_back_to_uv_tool_bin(tmp_path):
    _fake_ck(tmp_path / ".local/bin")
    out = _shim(["--version"], [PLUGIN / "bin", "/usr/bin", "/bin"], tmp_path)
    assert out.stdout.strip() == "real ck --version"
