"""The `spec` Claude Code plugin: where its files are, and how a project uses it.

plugin/ at the repository root is the one editable source of the spec skills,
workflows, agents, hooks and output style. Wheels carry it as package data
(claude_kiro/_plugin); a source checkout (editable install) reads plugin/
directly.

A project uses the plugin in one of two modes:
- plugin (default): .claude/settings.json enables `spec@claude-kiro`; nothing
  is copied. Each machine installs the plugin once (`ck setup`).
- vendored (`ck vendor`): the plugin's files are copied into .claude/ for
  places that don't load plugins (cloud sessions). The copies are rendered from
  the plugin (vendor_files) so they keep the /spec:* names and need no plugin.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .paths import IMPLEMENT_BRIEF, IMPLEMENT_WORKFLOW, NO_WORKFLOW_GUIDE, SPEC_WORKFLOW

PLUGIN_NAME = "spec"
MARKETPLACE_NAME = "claude-kiro"
PLUGIN_ID = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
MARKETPLACE_REPO = "kutlayhanli/claude-kiro"
MARKETPLACE_ENTRY = {"source": {"source": "github", "repo": MARKETPLACE_REPO}}

SKILLS = ("plan", "create", "implement", "review", "spawn-worktree")
# plugin agent file name -> `ck agents` role
AGENT_ROLES = {
    "implementer": "implementer",
    "test-writer": "test_writer",
    "fixer": "fixer",
    "resolver": "resolver",
    "reviewer": "reviewer",
}

# Vendored copy in a project -> source file in the plugin.
VENDOR_MAP: List[Tuple[str, str]] = [
    *[(f".claude/commands/spec/{name}.md", f"skills/{name}/SKILL.md") for name in SKILLS],
    (".claude/output-styles/spec-driven.md", "output-styles/spec-driven.md"),
    (SPEC_WORKFLOW, "workflows/spec-create.js"),
    (IMPLEMENT_WORKFLOW, "workflows/spec-implement.js"),
    (IMPLEMENT_BRIEF, "resources/spec-implement-brief.md"),
    (NO_WORKFLOW_GUIDE, "resources/without-workflow-tool.md"),
]

# Every path a ck version ever copied into a project (0.8 and earlier, plus
# today's vendored set). `ck upgrade` removes unedited ones when migrating.
LEGACY_MANAGED = sorted(
    {target for target, _ in VENDOR_MAP}
    | {".claude/commands/spawn-worktree.md"}
)
MANAGED_DIRS = (".claude/commands/spec", ".claude/commands", ".claude/workflows", ".claude/output-styles", ".claude/agents")

PLUGIN_ONLY = "<!-- plugin-only -->"


def root() -> Path:
    """The plugin directory: package data in a wheel, plugin/ in a source checkout."""
    packaged = Path(__file__).resolve().parent / "_plugin"
    if (packaged / ".claude-plugin" / "plugin.json").exists():
        return packaged
    checkout = Path(__file__).resolve().parents[2] / "plugin"
    if (checkout / ".claude-plugin" / "plugin.json").exists():
        return checkout
    raise FileNotFoundError("claude-kiro plugin files not found (reinstall claude-kiro)")


def read(rel: str) -> str:
    return (root() / rel).read_text()


def version() -> str:
    return json.loads(read(".claude-plugin/plugin.json"))["version"]


# --- vendored rendering ----------------------------------------------------------

_AGENT_REF = re.compile(r"(?<![\w/:-])spec:(" + "|".join(map(re.escape, AGENT_ROLES)) + r")\b")
_SKILL_REF = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/skills/([\w-]+)/SKILL\.md")
_DIR_REF = re.compile(r"(?:\$\{CLAUDE_PLUGIN_ROOT\}|<plugin-root>)/(?:resources|workflows)/")


def vendored(text: str) -> str:
    """A plugin Markdown file as a project copy: project paths, ck-* agent types, no plugin-only lines."""
    lines = [line for line in text.splitlines(keepends=True) if PLUGIN_ONLY not in line]
    text = "".join(lines)
    text = _SKILL_REF.sub(lambda m: f".claude/commands/spec/{m.group(1)}.md", text)
    text = _DIR_REF.sub(".claude/workflows/", text)
    return _AGENT_REF.sub(lambda m: f"ck-{m.group(1)}", text)


def vendor_files() -> Dict[str, str]:
    """Project path -> content of every vendored file. Scripts are copied verbatim."""
    files = {}
    for target, source in VENDOR_MAP:
        text = read(source)
        files[target] = vendored(text) if source.endswith(".md") else text
    return files


def agent_sources() -> Dict[str, Tuple[Dict[str, str], str]]:
    """Plugin agent name -> (frontmatter, body)."""
    out = {}
    for name in AGENT_ROLES:
        text = read(f"agents/{name}.md")
        _, head, body = text.split("---", 2)
        meta = {}
        for line in head.strip().splitlines():
            key, value = line.split(":", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            meta[key.strip()] = value
        out[name] = (meta, body.strip())
    return out


# --- project settings ---------------------------------------------------------------

def set_enabled(settings: dict, enabled: bool) -> bool:
    """Enable (plugin mode) or disable (vendored mode) the plugin in a settings dict. Returns True if it changed."""
    before = json.dumps(settings, sort_keys=True)
    plugins = settings.get("enabledPlugins")
    if not isinstance(plugins, dict):
        plugins = settings["enabledPlugins"] = {}
    plugins[PLUGIN_ID] = enabled
    if enabled:
        markets = settings.get("extraKnownMarketplaces")
        if not isinstance(markets, dict):
            markets = settings["extraKnownMarketplaces"] = {}
        markets.setdefault(MARKETPLACE_NAME, MARKETPLACE_ENTRY)
    return json.dumps(settings, sort_keys=True) != before


def enabled_in(settings: dict) -> Optional[bool]:
    plugins = settings.get("enabledPlugins")
    if isinstance(plugins, dict) and PLUGIN_ID in plugins:
        return bool(plugins[PLUGIN_ID])
    return None


# --- the plugin on this machine (read-only) ------------------------------------------

def installed(home: Optional[Path] = None) -> Tuple[Optional[bool], str]:
    """Whether the plugin is installed: (True/False, detail), or (None, why unknown).

    Asks `claude plugin list --json` when the CLI is on PATH, else reads
    ~/.claude/plugins/installed_plugins.json. Never changes anything.
    """
    claude = shutil.which("claude")
    if claude:
        try:
            proc = subprocess.run([claude, "plugin", "list", "--json"], capture_output=True, text=True, timeout=30)
            if proc.returncode == 0:
                for item in json.loads(proc.stdout or "[]"):
                    if isinstance(item, dict) and item.get("id") == PLUGIN_ID:
                        state = "" if item.get("enabled", True) else ", disabled"
                        return True, f"{item.get('version', '?')} ({item.get('scope', '?')} scope{state})"
                return False, "not in `claude plugin list`"
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError):
            pass
    listing = (home or Path.home()) / ".claude" / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(listing.read_text())
    except (OSError, json.JSONDecodeError):
        return (False, f"no {listing}") if listing.parent.is_dir() else (None, "claude CLI not found and no ~/.claude/plugins/")
    entries = (data.get("plugins") or {}).get(PLUGIN_ID) if isinstance(data, dict) else None
    if entries:
        first = entries[0] if isinstance(entries, list) else entries
        return True, f"{first.get('version', '?')} ({first.get('scope', '?')} scope)"
    return False, f"not in {listing}"


def setup_commands(source: str, already_installed: bool) -> List[List[str]]:
    """The `claude plugin` commands that install (or update) the plugin at user scope."""
    if already_installed:
        return [
            ["claude", "plugin", "marketplace", "update", MARKETPLACE_NAME],
            ["claude", "plugin", "update", PLUGIN_ID, "--scope", "user"],
        ]
    return [
        ["claude", "plugin", "marketplace", "add", source],
        ["claude", "plugin", "install", PLUGIN_ID, "--scope", "user"],
    ]


def mode(project_dir: Path) -> Optional[str]:
    """How a project gets the spec commands.

    "plugin" or "vendor" (recorded in .claude/ck-manifest.json by ck 0.9+),
    "legacy" (managed copies from ck 0.8 or earlier, no mode recorded), or None
    (not set up).
    """
    from . import manifest
    from .agents import generated_agent_files

    stamp = manifest.read(project_dir)
    if stamp and stamp.get("mode") in ("plugin", "vendor"):
        return stamp["mode"]
    if any((project_dir / rel).exists() for rel in LEGACY_MANAGED) or generated_agent_files(project_dir):
        return "legacy"
    try:
        settings = json.loads((project_dir / ".claude" / "settings.json").read_text())
    except (OSError, json.JSONDecodeError):
        settings = {}
    if isinstance(settings, dict) and enabled_in(settings):
        return "plugin"
    return None
