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
AGENTS = {"implementer": "implementer", "test-writer": "test_writer", "fixer": "fixer", "resolver": "resolver", "reviewer": "reviewer", "arbiter": "arbiter", "auditor": "reviewer"}
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
    # A vendored project has the plugin off: trying the plugin's workflow name first only fails.
    # The skills read the recorded mode and go straight to the vendored script there.
    for workflow, text in (("spec-implement", implement), ("spec-create", create), ("spec-create", review)):
        assert '.claude/ck-manifest.json' in text and '"mode": "vendor"' in text, workflow
        assert text.index('"mode": "vendor"') < text.index(f'name: "spec:{workflow}"'), workflow
        assert f".claude/workflows/{workflow}.js" in text, workflow
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
    assert sorted(files) == sorted(f"ck-{n}.md" for n in [*AGENTS, "runner"])
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


# --- deterministic-plan skill docs (Task 23; verifies Task 24) ------------------
#
# Grep-level checks that the implement skill (and its vendored command copy)
# actually say what criteria 3.1/3.2 and 4.1/4.2 require. They read the real
# docs files: no mocks, no copies of the content under test. They fail until
# the Task 24 docs edit adds the trigger and review-batch wording.

DETERMINISTIC_DOC_PATHS = [
    PLUGIN / "skills/implement/SKILL.md",
    REPO / ".claude/commands/spec/implement.md",
]


def _deterministic_step7(path: Path, text: str) -> str:
    start = text.find("7. **Tell me it's running:**")
    assert start != -1, f"{path}: Step 7 section missing"
    end = text.find("8. **Questions", start)
    return text[start:end if end != -1 else len(text)]


def test_deterministic_docs_step6_passes_deps_and_remaining():
    """Step 6 still passes `deps`/`remaining` (3.1, 3.2)."""
    for path in DETERMINISTIC_DOC_PATHS:
        text = path.read_text()
        assert '"deps"' in text, f"{path}: Step 6 launch args must pass `deps`"
        assert '"remaining"' in text, f"{path}: Step 6 launch args must pass `remaining`"


def test_deterministic_docs_plan_triggers():
    """Step 7 documents start / after-each-landing / idle-only triggers with
    no re-read after every freed slot (3.1, 3.2)."""
    for path in DETERMINISTIC_DOC_PATHS:
        step7 = _deterministic_step7(path, path.read_text()).lower()
        assert "after each landing" in step7, f"{path}: Step 7 must document the after-each-landing trigger"
        assert "idle" in step7, f"{path}: Step 7 must document the idle-only trigger"
        assert "freed slot" in step7, f"{path}: Step 7 must say no re-read runs after every freed slot"


# --- deterministic-plan workflow-script parity (Task 25; verifies Task 26) --------
#
# TC-34 contract half: the workflow script ships on both harnesses, so any
# change to the plugin source must keep both vendored copies byte-identical.
# Reads the real files: no mocks, no copies of the content under test. Fails
# until the Task 26 refresh regenerates the OpenCode vendored copy (missing
# or stale file) — the only allowed divergence.

OPENCODE_VENDORED_SPEC_IMPLEMENT = (
    REPO / "opencode" / "vendor" / "workflows" / "spec-implement.js"
)
CLAUDE_VENDORED_SPEC_IMPLEMENT = REPO / ".claude" / "workflows" / "spec-implement.js"


def test_opencode_vendored_workflow_matches_plugin_source():
    """TC-34: `opencode/vendor/workflows/spec-implement.js` is byte-identical
    to `plugin/workflows/spec-implement.js` (1.2, 2.1, 2.2).

    `opencode/vendor/` is ignored by version control and generated by `npm run
    sync-vendor` (and on `npm pack`), so the test runs that generator first: it
    checks what the package would ship from this tree, and works in a fresh checkout."""
    import shutil
    import subprocess

    source = (PLUGIN / "workflows" / "spec-implement.js").read_bytes()
    assert len(source) > 0, "plugin source workflow script is empty"
    if shutil.which("node") is None:
        pytest.skip("node is not installed; the OpenCode vendor copy cannot be generated")
    sync = subprocess.run(["node", "scripts/sync-vendor.ts"], cwd=REPO / "opencode", capture_output=True, text=True, timeout=120)
    assert sync.returncode == 0, f"sync-vendor failed: {sync.stdout}{sync.stderr}"
    assert OPENCODE_VENDORED_SPEC_IMPLEMENT.exists(), (
        f"missing OpenCode vendored copy (run the vendor refresh): "
        f"{OPENCODE_VENDORED_SPEC_IMPLEMENT}"
    )
    assert OPENCODE_VENDORED_SPEC_IMPLEMENT.read_bytes() == source, (
        "OpenCode vendored copy diverged from plugin/workflows/spec-implement.js "
        "(refresh it from the plugin source)"
    )


def test_claude_vendored_workflow_matches_plugin_source():
    """TC-34: `.claude/workflows/spec-implement.js` is byte-identical to
    `plugin/workflows/spec-implement.js` (1.2, 2.1, 2.2)."""
    source = (PLUGIN / "workflows" / "spec-implement.js").read_bytes()
    assert len(source) > 0, "plugin source workflow script is empty"
    assert CLAUDE_VENDORED_SPEC_IMPLEMENT.exists(), (
        f"missing vendored copy (run `uv run ck vendor`): {CLAUDE_VENDORED_SPEC_IMPLEMENT}"
    )
    assert CLAUDE_VENDORED_SPEC_IMPLEMENT.read_bytes() == source, (
        ".claude vendored copy diverged from plugin/workflows/spec-implement.js "
        "(run `uv run ck vendor`)"
    )


def test_deterministic_docs_review_batching():
    """Step 7 documents wave-batched review with the risky-always-individual
    rule (4.1, 4.2)."""
    for path in DETERMINISTIC_DOC_PATHS:
        text = path.read_text()
        low = text.lower()
        assert "review-wave" in low, f"{path}: docs must describe the wave-batched `review-wave` review"
        assert ("risky" in low or "safety" in low) and "individual" in low, (
            f"{path}: docs must state the risky/safety-always-individual rule"
        )
