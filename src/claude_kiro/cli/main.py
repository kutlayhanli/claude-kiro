"""Main CLI entry point for Claude Kiro.

Provides the `ck` command with subcommands for project management and a hidden
hook runner for Claude Code integration.
"""

import os
import sys
import json
import logging
import re
from pathlib import Path
from typing import Optional

import click

from .. import plugin
from ..config import initial_config, load_config
from ..paths import CONFIG_FILE, LEGACY_SPECS_DIR, SPECS_DIR, spec_roots
from .agents import agents
from .update import update
from .hooks import hook
from .run import run
from .bench import bench
from .worktree import lint, plan, waves, worktree
from .runner import HOOK_SETTINGS, configured_hooks, execute_hook, install_hook_settings


# Configure logging
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
logger = logging.getLogger(__name__)


@click.group(invoke_without_command=True)
@click.version_option(package_name="claude-kiro", prog_name="ck")
@click.option(
    "--hook",
    "hook_name",
    hidden=True,
    help="Execute a hook (for Claude Code integration)",
)
@click.option(
    "--plugin-hook",
    is_flag=True,
    hidden=True,
    help="The hook runs from the spec plugin: do nothing outside claude-kiro projects",
)
@click.option(
    "-C",
    "directory",
    type=click.Path(exists=True, file_okay=False),
    help="Run as if ck was started in DIRECTORY (like git -C), so agents never need `cd ... &&`",
)
@click.pass_context
def cli(ctx: click.Context, hook_name: Optional[str], plugin_hook: bool, directory: Optional[str]):
    """Claude Kiro - Spec-driven development methodology for use with Claude Code.

    Initialize projects with spec-driven workflow, configure Claude Code hooks,
    and verify setup with the unified `ck` command.
    """
    if directory:
        os.chdir(directory)
    if hook_name:
        # Hidden hook execution mode for Claude Code
        try:
            # Read JSON from stdin
            input_data = sys.stdin.read()
            input_json = json.loads(input_data) if input_data else {}

            # A user-scope plugin loads in every project; its hooks only act in ck projects.
            if plugin_hook and not _is_ck_project(Path(input_json.get("cwd") or os.getcwd())):
                sys.exit(0)

            # Execute the hook
            result = execute_hook(hook_name, input_json)

            # Write result to stdout
            if result is not None:
                print(json.dumps(result), file=sys.stdout, flush=True)

            sys.exit(0)
        except Exception as e:
            logger.error(f"Hook execution failed: {e}")
            sys.exit(1)
    elif ctx.invoked_subcommand is None:
        # No subcommand, show help
        click.echo(ctx.get_help())


def _is_ck_project(path: Path) -> bool:
    """A claude-kiro project: it has specs/ck.json, a ck manifest, or legacy .claude/specs/."""
    return (
        (path / CONFIG_FILE).exists()
        or (path / ".claude" / "ck-manifest.json").exists()
        or (path / LEGACY_SPECS_DIR).is_dir()
    )


# Files a vendored project gets copied into .claude/ (rendered from plugin/).
# .claude/CLAUDE.md is not here: it is created once by `ck init` and then
# belongs to the project.
MANAGED_FILES = plugin.VENDOR_MAP

RUFF_HOOK = re.compile(r"^(\s*)-\s+id:\s*(ruff|ruff-check|ruff-format)\s*$")


def _ruff_hooks_unrestricted(text: str) -> list:
    """Line indexes of ruff pre-commit hooks with no types/types_or/files restriction."""
    lines = text.splitlines()
    found = []
    for i, line in enumerate(lines):
        match = RUFF_HOOK.match(line)
        if not match:
            continue
        dash = len(match.group(1))
        restricted = False
        for following in lines[i + 1:]:
            stripped = following.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(following) - len(following.lstrip())
            if indent <= dash:
                break
            if re.match(r"(types|types_or|files):", stripped):
                restricted = True
                break
        if not restricted:
            found.append(i)
    return found


def _patch_precommit(project_dir: Path, dry_run: bool = False) -> int:
    """Limit ruff pre-commit hooks to Python files, so ruff-format leaves the
    code blocks inside spec Markdown alone. Returns how many hooks were patched."""
    config = project_dir / ".pre-commit-config.yaml"
    if not config.exists():
        return 0
    text = config.read_text()
    targets = _ruff_hooks_unrestricted(text)
    if targets and not dry_run:
        lines = text.splitlines()
        for i in reversed(targets):
            dash = len(RUFF_HOOK.match(lines[i]).group(1))
            lines.insert(i + 1, " " * (dash + 2) + "types_or: [python, pyi]")
        config.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""))
    return len(targets)


def _install_hooks(project_dir: Path) -> None:
    """Add or refresh ck hooks in .claude/settings.local.json, keeping other hooks."""
    settings_file = project_dir / ".claude" / "settings.local.json"
    settings = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
        except json.JSONDecodeError:
            backup = settings_file.with_suffix(".json.bak")
            settings_file.rename(backup)
            logger.warning(f"Backed up corrupted settings to {backup}")
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    install_hook_settings(settings)
    settings_file.write_text(json.dumps(settings, indent=2))


def _ensure_config(project_dir: Path) -> bool:
    """Create specs/ck.json if missing. Returns True when it was created."""
    config_file = project_dir / CONFIG_FILE
    if config_file.exists():
        return False
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps(initial_config(project_dir), indent=2) + "\n")
    return True


def _sync_agent_types(project_dir: Path, dry_run: bool = False) -> list:
    """Write the ck-* agent types; returns (kind, path) per change. A broken agents config is reported, not fatal."""
    from .. import agents as ag

    try:
        before = {p.name for p in (project_dir / ag.AGENTS_DIR).glob("ck-*.md")}
        result = ag.sync_agent_types(project_dir, dry_run=dry_run)
    except ag.AgentConfigError as e:
        click.echo(f"⚠️  Agent types not written: {e}")
        return []
    return [
        ("updated" if name in before else "added", f"{ag.AGENTS_DIR}/{name} (agent type for runs without the Workflow tool)")
        for name in result["changed"]
    ]


WORKFLOW_RULE = "Workflow"
SHARED_SETTINGS = ".claude/settings.json"


def _interactive() -> bool:
    return sys.stdin.isatty()


def _allow_workflow(project_dir: Path) -> str:
    """Add the Workflow allow rule to the shared .claude/settings.json, keeping every other setting.

    Returns "added", "present", or "invalid" (the file isn't a JSON object; left alone).
    """
    path = project_dir / SHARED_SETTINGS
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except json.JSONDecodeError:
        return "invalid"
    if not isinstance(data, dict) or not isinstance(data.get("permissions", {}), dict):
        return "invalid"
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    if not isinstance(allow, list):
        return "invalid"
    if WORKFLOW_RULE in allow:
        return "present"
    allow.append(WORKFLOW_RULE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return "added"


def _offer_workflow_rule(project_dir: Path, choice: Optional[bool]) -> Optional[str]:
    """Ask (or follow --allow-workflow/--no-allow-workflow) whether to allow the Workflow tool."""
    if choice is None:
        if not _interactive():
            return None
        click.echo(
            "\n/spec:create, /spec:review and /spec:implement launch multi-agent workflows. In auto mode a\n"
            "classifier may stop to ask before each launch, and an unattended session (cloud, claude -p)\n"
            f"then waits forever. Allowing the Workflow tool in {SHARED_SETTINGS} skips that question\n"
            "for everyone who uses this repository."
        )
        choice = click.confirm("Allow the Workflow tool?", default=False)
    return _allow_workflow(project_dir) if choice else None


def _edit_shared_settings(project_dir: Path, change, dry_run: bool = False) -> str:
    """Apply change(settings dict) -> bool to .claude/settings.json, keeping every other setting.

    Returns "changed", "unchanged", or "invalid" (the file isn't a JSON object; left alone).
    """
    path = project_dir / SHARED_SETTINGS
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except json.JSONDecodeError:
        return "invalid"
    if not isinstance(data, dict):
        return "invalid"
    if not change(data):
        return "unchanged"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    return "changed"


def _ensure_gitignore(project_dir: Path, dry_run: bool = False) -> Optional[str]:
    """Ignore the task worktree directory (specs/ck.json "worktree_dir") in .gitignore.

    Returns the entry added, or None when it is already ignored (by any ignore file
    git reads), lies outside the project, or this is not a git checkout.
    """
    import subprocess

    raw = str(load_config(project_dir).get("worktree_dir") or "").strip()
    if not raw or Path(os.path.expanduser(raw)).is_absolute():
        return None
    rel = raw.strip("/").removeprefix("./")
    if not rel or rel == ".." or rel.startswith("../"):
        return None
    probe = subprocess.run(
        ["git", "-C", str(project_dir), "check-ignore", "-q", "--no-index", f"{rel}/ck-probe"],
        capture_output=True,
    )
    if probe.returncode == 0:  # already ignored
        return None
    if probe.returncode != 1:  # 128: not a git checkout
        return None
    entry = f"/{rel}/"
    if not dry_run:
        path = project_dir / ".gitignore"
        text = path.read_text() if path.exists() else ""
        if text and not text.endswith("\n"):
            text += "\n"
        gap = "\n" if text else ""
        text += f"{gap}# Task worktrees made by `ck worktree` (specs/ck.json worktree_dir)\n{entry}\n"
        path.write_text(text)
    return entry


def _remove_ck_hooks(project_dir: Path, dry_run: bool = False) -> bool:
    """Drop ck's hooks from .claude/settings.local.json (the plugin brings them). Returns True if any were there."""
    path = project_dir / ".claude" / "settings.local.json"
    try:
        settings = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return False

    def ours(h) -> bool:
        cmd = h.get("command", "") if isinstance(h, dict) else ""
        return cmd.startswith("ck --hook ") or "ckh-" in cmd

    removed = False
    for event in list(hooks):
        groups = hooks[event] if isinstance(hooks[event], list) else []
        kept_groups = []
        for group in groups:
            entries = group.get("hooks", []) if isinstance(group, dict) else []
            kept = [h for h in entries if not ours(h)]
            removed |= len(kept) != len(entries)
            if kept or not entries:
                kept_groups.append({**group, "hooks": kept} if isinstance(group, dict) else group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks")
    if removed and not dry_run:
        path.write_text(json.dumps(settings, indent=2))
    return removed


def _write_vendored(project_dir: Path, overwrite: bool, dry_run: bool = False) -> dict:
    """Copy the plugin's files into .claude/ (rendered for a project without the plugin).

    overwrite=False skips existing files (ck init); True refreshes them like ck
    upgrade: untracked files that differ are backed up first, and files edited
    since ck wrote them are reported. Copies from older layouts that are no
    longer vendored are removed when unedited, else kept and listed.
    """
    from .. import manifest

    stamp = manifest.read(project_dir)
    result = {"changes": [], "skipped": [], "backups": [], "edited": [], "kept": []}
    files = plugin.vendor_files()
    for target_path, content in files.items():
        target = project_dir / target_path
        current = target.read_text() if target.exists() else None
        if current == content:
            continue
        if current is not None and not overwrite:
            result["skipped"].append(target_path)
            continue
        if current is not None and manifest.edited_since_written(stamp, target_path, current):
            result["edited"].append(target_path)
        result["changes"].append(("updated" if current is not None else "added", target_path))
        if dry_run:
            continue
        if current is not None and not _git_tracked(project_dir, target):
            backup = target.with_name(target.name + ".bak")
            backup.write_text(current)
            result["backups"].append(str(backup.relative_to(project_dir)))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    obsolete = [rel for rel in plugin.LEGACY_MANAGED if rel not in files]
    removed, kept = _remove_unedited(project_dir, obsolete, stamp, files, dry_run)
    result["changes"].extend(("removed", rel) for rel in removed)
    result["kept"].extend(kept)

    settings_file = project_dir / ".claude" / "settings.local.json"
    before = settings_file.read_text() if settings_file.exists() else ""
    try:
        merged = json.dumps(install_hook_settings(json.loads(before) if before else {}), indent=2)
    except json.JSONDecodeError:
        merged = None
    if merged != before:
        result["changes"].append(("updated" if before else "added", ".claude/settings.local.json (hooks)"))
        if not dry_run:
            _install_hooks(project_dir)

    result["changes"].extend(_sync_agent_types(project_dir, dry_run=dry_run))

    state = _edit_shared_settings(project_dir, lambda s: plugin.set_enabled(s, False), dry_run)
    if state == "changed":
        result["changes"].append(("updated", f"{SHARED_SETTINGS} (enabledPlugins: {plugin.PLUGIN_ID} off; the copies replace it here)"))
    return result


def _remove_unedited(project_dir: Path, rels, stamp, rendered: dict, dry_run: bool) -> tuple:
    """Remove copies ck wrote that nobody edited. Returns (removed, kept).

    A copy is unedited when its hash matches the manifest, or (older ck with no
    hashes) when it equals this version's rendering. Anything else is kept.
    """
    from .. import manifest

    removed, kept = [], []
    for rel in rels:
        path = project_dir / rel
        if not path.is_file():
            continue
        text = path.read_text()
        recorded = (stamp or {}).get("files", {}).get(rel)
        if (recorded and recorded == manifest.digest(text)) or text == rendered.get(rel):
            removed.append(rel)
            if not dry_run:
                path.unlink()
        else:
            kept.append(rel)
    return removed, kept


def _prune_empty_dirs(project_dir: Path, dry_run: bool) -> None:
    if dry_run:
        return
    for rel in plugin.MANAGED_DIRS:
        path = project_dir / rel
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def _migrate_to_plugin(project_dir: Path, stamp, dry_run: bool) -> dict:
    """Switch a project from managed copies to the plugin: remove unedited copies and generated agent types."""
    from .. import agents as ag

    result = {"changes": [], "kept": []}
    removed, kept = _remove_unedited(project_dir, plugin.LEGACY_MANAGED, stamp, plugin.vendor_files(), dry_run)
    result["changes"].extend(("removed", rel) for rel in removed)
    result["kept"].extend(kept)
    for path in ag.generated_agent_files(project_dir):
        result["changes"].append(("removed", str(path.relative_to(project_dir))))
        if not dry_run:
            path.unlink()
    if _remove_ck_hooks(project_dir, dry_run):
        result["changes"].append(("updated", ".claude/settings.local.json (ck hooks removed; the plugin brings them)"))
    state = _edit_shared_settings(project_dir, lambda s: plugin.set_enabled(s, True), dry_run)
    if state == "changed":
        result["changes"].append(("updated", f"{SHARED_SETTINGS} (enabledPlugins: {plugin.PLUGIN_ID})"))
    elif state == "invalid":
        click.echo(f"⚠️  {SHARED_SETTINGS} isn't a settings object; enable {plugin.PLUGIN_ID} in enabledPlugins by hand.")
    _prune_empty_dirs(project_dir, dry_run)
    return result


@cli.command()
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option(
    "--allow-workflow/--no-allow-workflow",
    default=None,
    help=f"Add (or don't) a Workflow allow rule to {SHARED_SETTINGS}, so spec workflows launch without a permission prompt. Asked interactively if not given.",
)
@click.option("--vendor", is_flag=True, help="Copy the spec commands, workflows and agents into .claude/ instead of using the plugin (for cloud sessions, which don't load plugins)")
def init(force: bool, allow_workflow: Optional[bool], vendor: bool):
    """Initialize a Claude Kiro project in the current directory.

    \b
    Default (plugin mode): enables the `spec` plugin for this project in
    .claude/settings.json (enabledPlugins + the claude-kiro marketplace) and
    writes specs/ck.json. Nothing is copied: the commands, workflows, agents
    and hooks come from the plugin, which `ck setup` installs once per machine.
    --vendor: copies them into .claude/ instead (see `ck vendor`).

    Offers to allow the Workflow tool in .claude/settings.json so unattended
    runs never wait on a prompt.
    """
    from .. import manifest
    from ..resources import ResourceLoader

    project_dir = Path.cwd()
    claude_dir = project_dir / ".claude"

    stamp = manifest.read(project_dir)
    if force and manifest.newer_than_running(stamp):
        click.echo(
            f"❌ This project's ck files were written by ck {stamp['version']}, newer than this ck "
            f"({manifest.ck_version()}); --force would replace them with older copies. Reinstall the newer ck first."
        )
        sys.exit(1)

    created = []
    skipped = []
    existing_mode = plugin.mode(project_dir)

    for directory in (claude_dir, project_dir / SPECS_DIR):
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            created.append(str(directory.relative_to(project_dir)))

    claude_md = claude_dir / "CLAUDE.md"
    if claude_md.exists() and not force:
        skipped.append(".claude/CLAUDE.md")
    else:
        claude_md.write_text(ResourceLoader().get_resource("claude_md.md"))
        created.append(".claude/CLAUDE.md")

    managed = [target for target, _ in MANAGED_FILES]
    if vendor:
        result = _write_vendored(project_dir, overwrite=force)
        created.extend(f"{item} ({kind})" if kind != "added" else item for kind, item in result["changes"])
        skipped.extend(result["skipped"])
        # Stamp only if this ck wrote every vendored file; `ck vendor` refreshes skipped ones.
        if not result["skipped"]:
            manifest.write(project_dir, managed, mode="vendor")
            created.append(f"{manifest.MANIFEST_FILE} (vendored)")
    else:
        state = _edit_shared_settings(project_dir, lambda s: plugin.set_enabled(s, True))
        if state == "changed":
            created.append(f"{SHARED_SETTINGS} (enabledPlugins: {plugin.PLUGIN_ID})")
        elif state == "invalid":
            click.echo(f"⚠️  {SHARED_SETTINGS} isn't a settings object; enable {plugin.PLUGIN_ID} in enabledPlugins by hand.")
        if existing_mode in ("legacy", "vendor"):
            click.echo("⚠️  This project has copies of the spec commands in .claude/. Run 'ck upgrade --plugin' to remove the unedited ones.")
        else:
            if _remove_ck_hooks(project_dir):
                created.append(".claude/settings.local.json (ck hooks removed; the plugin brings them)")
            manifest.write(project_dir, [], mode="plugin")
            created.append(f"{manifest.MANIFEST_FILE} (plugin mode)")

    patched = _patch_precommit(project_dir)
    if patched:
        created.append(f".pre-commit-config.yaml (ruff limited to Python in {patched} hook(s))")

    if _ensure_config(project_dir):
        created.append(CONFIG_FILE)
    else:
        skipped.append(CONFIG_FILE)

    ignored = _ensure_gitignore(project_dir)
    if ignored:
        created.append(f".gitignore ({ignored}: task worktrees)")

    rule = _offer_workflow_rule(project_dir, allow_workflow)
    if rule == "added":
        created.append(f"{SHARED_SETTINGS} (allow: {WORKFLOW_RULE})")
    elif rule == "invalid":
        click.echo(f"⚠️  {SHARED_SETTINGS} isn't a settings object; add \"{WORKFLOW_RULE}\" to permissions.allow by hand.")

    click.echo("\n✨ Claude Kiro initialized successfully!" + (" (vendored)" if vendor else ""))

    if created:
        click.echo("\n📁 Created:")
        for item in created:
            click.echo(f"  ✓ {item}")

    if skipped:
        click.echo("\n⏭️  Skipped (already exists):")
        for item in skipped:
            click.echo(f"  - {item}")
        click.echo("\n💡 Use --force to overwrite existing files")

    click.echo("\n🚀 Next steps:")
    if vendor:
        click.echo("  1. Commit .claude/ (commands, workflows, agents, output-styles, ck-manifest.json, settings.json) and specs/")
    else:
        click.echo(f"  1. Run 'ck setup' once on each machine: it installs the {plugin.PLUGIN_ID} plugin")
        click.echo(f"  2. Commit {SHARED_SETTINGS}, {manifest.MANIFEST_FILE} and {CONFIG_FILE}; teammates are offered the plugin")
    click.echo("  •  Review .claude/CLAUDE.md and customize it; run 'ck doctor' to verify the setup")
    click.echo("  •  Use /spec:plan to decide the approach, then /spec:create to write the spec")
    if allow_workflow is None and rule is None and not _interactive():
        click.echo(
            f"\n💡 Unattended runs (cloud, claude -p) can stall on a Workflow permission prompt. "
            f"Run 'ck init --allow-workflow' or add \"{WORKFLOW_RULE}\" to permissions.allow in {SHARED_SETTINGS}."
        )
    click.echo(f"\n📝 Specs are written to {SPECS_DIR}/ (outside .claude/, so no approval prompts)")
    if (project_dir / LEGACY_SPECS_DIR).is_dir():
        click.echo(f"⚠️  Found specs in {LEGACY_SPECS_DIR}/ - run 'ck migrate' to move them")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would change without writing anything")
def vendor(dry_run: bool):
    """Copy the spec plugin into this project's .claude/ (for cloud sessions).

    \b
    Claude Code on the web and other places that don't load plugins need the
    commands in the repository. This writes .claude/commands/spec/*.md (still
    /spec:plan, /spec:create, /spec:review, /spec:implement,
    /spec:spawn-worktree), .claude/workflows/, .claude/output-styles/, the
    ck-* agent types with this project's models, and the hooks in
    .claude/settings.local.json. It switches the plugin off for this project
    in .claude/settings.json, so a machine with the plugin installed doesn't
    see every command twice. Re-run (or `ck upgrade`) after updating ck.
    `ck upgrade --plugin` goes back to the plugin.
    """
    ctx = click.get_current_context()
    ctx.invoke(upgrade, dry_run=dry_run, no_migrate=True, allow_downgrade=False, to_vendor=True, to_plugin=False)


@cli.command()
@click.option("--force", is_flag=True, help="Overwrite an existing ~/.claude/CLAUDE.md")
@click.option("--diff", "show_diff", is_flag=True, help="Show how the existing global CLAUDE.md differs from this version; write nothing")
@click.option("--dry-run", is_flag=True, help="Print what would run and be written; change nothing")
@click.option(
    "--source",
    envvar="CK_MARKETPLACE",
    default=plugin.MARKETPLACE_REPO,
    show_default=True,
    help="Where the claude-kiro marketplace is: owner/repo, a git URL, or a local checkout (env: CK_MARKETPLACE)",
)
def setup(force: bool, show_diff: bool, dry_run: bool, source: str):
    """Set up this machine: install the spec plugin and the global CLAUDE.md.

    \b
    1. Installs (or updates) the `spec` Claude Code plugin at user scope:
         claude plugin marketplace add <source>
         claude plugin install spec@claude-kiro --scope user
    2. Writes ~/.claude/CLAUDE.md if it doesn't exist (never overwrites
       without --force; see changes with --diff).

    Run once per machine after installing ck, and again after `ck update`.
    """
    from ..resources import ResourceLoader
    import shlex
    import shutil
    import subprocess

    loader = ResourceLoader()
    if show_diff:
        _setup_diff(loader)
        return

    target = Path.home() / ".claude" / "CLAUDE.md"
    if target.exists() and not force:
        click.echo(f"⏭️  {target} exists; left alone (compare with 'ck setup --diff')")
    elif dry_run:
        click.echo(f"would write {target}")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(loader.get_resource("global/claude_md.md"))
        click.echo(f"✓ wrote {target}")

    status, detail = plugin.installed()
    commands = plugin.setup_commands(source, status is True)
    if dry_run:
        click.echo(f"\n{plugin.PLUGIN_ID}: {'installed, ' + detail if status else 'not installed'}. Would run:")
        for cmd in commands:
            click.echo(f"  {shlex.join(cmd)}")
        return

    claude = shutil.which("claude")
    if not claude:
        click.echo("\n❌ The claude CLI is not on PATH, so the plugin was not installed. Run these where it is:")
        for cmd in plugin.setup_commands(source, False):
            click.echo(f"  {shlex.join(cmd)}")
        click.echo("  (or inside Claude Code: /plugin marketplace add ..., then /plugin install spec@claude-kiro)")
        sys.exit(1)

    failed = False
    for i, cmd in enumerate(commands):
        click.echo(f"\n$ {shlex.join(cmd)}")
        code = subprocess.run([claude, *cmd[1:]]).returncode
        if code != 0:
            # The marketplace may already be known (added by hand): the install step decides.
            if i == 0:
                click.echo("  (continuing: the next step tells whether the plugin is available)")
                continue
            failed = True
    if failed:
        click.echo(f"\n❌ Installing {plugin.PLUGIN_ID} failed; see above. Inside Claude Code, /plugin shows the details.")
        sys.exit(1)
    click.echo(f"\n✨ {plugin.PLUGIN_ID} is installed for your user. Restart open Claude Code sessions to load it.")
    click.echo("🚀 Next: 'ck init' in each new project ('ck upgrade' in projects set up by ck 0.8 or earlier).")


GLOBAL_FILES = [
    (Path(".claude") / "CLAUDE.md", "global/claude_md.md"),
]


def _setup_diff(loader) -> None:
    """Print a unified diff between each existing global file and this version's template."""
    import difflib

    for rel, resource_path in GLOBAL_FILES:
        target = Path.home() / rel
        new = loader.get_resource(resource_path)
        if not target.exists():
            click.echo(f"\n➕ {target} does not exist; 'ck setup' would create it.")
            continue
        old = target.read_text()
        if old == new:
            click.echo(f"\n✓ {target} matches this version.")
            continue
        click.echo(f"\n✏️  {target} differs (- yours, + ck {resource_path}):")
        diff = difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), str(target), f"ck:{resource_path}"
        )
        click.echo("".join(diff))
    click.echo(
        "\n💡 If you never customized a file, 'ck setup --force' replaces it. "
        "If you did, copy the + lines you want by hand."
    )


@cli.command()
@click.option("--unattended", is_flag=True, help="Check this machine for unattended `ck run`s: sandbox tools, credentials, risky allow rules")
def doctor(unattended: bool):
    """Check Claude Kiro setup health.

    Reports how the project gets the spec commands (the plugin, or vendored
    copies), whether the plugin is installed on this machine, and checks
    files, hooks, the version stamp and verify commands.
    With --unattended: what an unattended `ck run` is exposed to here, with
    recommendations (always exits 0).
    """
    import shutil
    from .. import manifest

    project_dir = Path.cwd()
    if unattended:
        from .run import doctor_unattended

        doctor_unattended(project_dir)
        return
    claude_dir = project_dir / ".claude"
    issues = []
    warnings = []

    click.echo("🔍 Checking Claude Kiro setup...\n")

    if shutil.which("ck"):
        click.echo("✓ ck command is installed")
    else:
        issues.append("ck command not found in PATH")

    if not claude_dir.exists():
        issues.append(".claude directory not found - run 'ck init'")
        _report_doctor_results(issues, warnings)
        return

    stamp = manifest.read(project_dir)
    mode = plugin.mode(project_dir)
    if mode == "plugin":
        click.echo(f"✓ Mode: plugin ({plugin.PLUGIN_ID} enabled in {SHARED_SETTINGS})")
        try:
            shared = json.loads((project_dir / SHARED_SETTINGS).read_text())
        except (OSError, json.JSONDecodeError):
            shared = {}
        if not (isinstance(shared, dict) and plugin.enabled_in(shared)):
            issues.append(f"{plugin.PLUGIN_ID} is not enabled in {SHARED_SETTINGS} - run 'ck upgrade'")
    elif mode == "vendor":
        click.echo("✓ Mode: vendored (spec commands copied into .claude/ by 'ck vendor')")
    elif mode == "legacy":
        source = f"ck {stamp['version']}" if stamp else "an older ck"
        warnings.append(
            f"Mode: legacy (managed copies from {source} in .claude/) - run 'ck upgrade' to switch to the plugin, "
            "or 'ck vendor' to keep copies"
        )
    else:
        issues.append("Not set up: run 'ck init'")

    status, detail = plugin.installed()
    if status:
        click.echo(f"✓ Plugin {plugin.PLUGIN_ID} installed: {detail}")
        installed_version = detail.split(" ", 1)[0]
        if mode == "plugin" and installed_version not in ("?", manifest.ck_version()):
            warnings.append(
                f"Plugin {plugin.PLUGIN_ID} is {installed_version} but ck is {manifest.ck_version()} - "
                "run 'ck update' so the commands and the CLI match"
            )
    elif mode == "plugin":
        if status is False:
            warnings.append(f"Plugin {plugin.PLUGIN_ID} is not installed on this machine ({detail}) - run 'ck setup'")
        else:
            warnings.append(f"Can't tell whether {plugin.PLUGIN_ID} is installed ({detail}) - run 'ck setup' if /spec:plan is missing")

    if mode == "vendor":
        missing_files = [target for target, _ in MANAGED_FILES if not (project_dir / target).exists()]
        if missing_files:
            issues.append(f"Missing files: {', '.join(missing_files)} - run 'ck vendor'")
        else:
            click.echo("✓ All vendored files present")

    settings_file = claude_dir / "settings.local.json"
    if mode in ("vendor", "legacy"):
        if settings_file.exists():
            try:
                settings = json.loads(settings_file.read_text())
                found = configured_hooks(settings)
                for spec in HOOK_SETTINGS:
                    if spec["hook"] in found.get(spec["event"], []):
                        click.echo(f"✓ {spec['event']} hook configured: ck --hook {spec['hook']}")
                    else:
                        issues.append(
                            f"{spec['event']} hook 'ck --hook {spec['hook']}' not configured - run 'ck upgrade' to fix"
                        )
                for event, groups in settings.get("hooks", {}).items():
                    for group in groups if isinstance(groups, list) else []:
                        for h in group.get("hooks", []) if isinstance(group, dict) else []:
                            if isinstance(h, dict) and str(h.get("command", "")).startswith("ck --hook") and h.get("timeout", 0) > 3600:
                                warnings.append(
                                    f"{event} hook timeout {h['timeout']} looks like milliseconds; Claude Code uses seconds - run 'ck upgrade' to fix"
                                )
            except json.JSONDecodeError:
                issues.append("settings.local.json is corrupted")
        else:
            warnings.append("settings.local.json not found - hooks may not be configured")
    elif mode == "plugin" and _remove_ck_hooks(project_dir, dry_run=True):
        warnings.append("ck hooks in .claude/settings.local.json run twice with the plugin's - run 'ck upgrade' to remove them")
    elif mode == "plugin":
        click.echo("✓ Hooks come from the plugin")

    global_claude_md = Path.home() / ".claude" / "CLAUDE.md"
    if global_claude_md.exists():
        click.echo("✓ Global ~/.claude/CLAUDE.md found")
    else:
        warnings.append("Global ~/.claude/CLAUDE.md not found - run 'ck setup'")

    roots = spec_roots(project_dir)
    spec_count = sum(len(list(root.glob("*/requirements.md"))) for root in roots)
    if spec_count > 0:
        click.echo(f"✓ Found {spec_count} spec(s)")
    else:
        click.echo(f"ℹ️  No specs created yet (they go in {SPECS_DIR}/)")

    running = manifest.ck_version()
    if not stamp:
        warnings.append(f"No {manifest.MANIFEST_FILE} version stamp - run 'ck upgrade' to record it")
    elif manifest.newer_than_running(stamp):
        issues.append(
            f"Project files were written by ck {stamp['version']} but this ck is {running} - reinstall ck before running 'ck upgrade' (see UPGRADING.md)"
        )
    elif manifest.version_key(stamp["version"]) < manifest.version_key(running):
        warnings.append(f"Project files are from ck {stamp['version']}; this ck is {running} - run 'ck upgrade'")
    else:
        click.echo(f"✓ Project stamped by ck {stamp['version']} (this ck)")

    if _patch_precommit(project_dir, dry_run=True):
        warnings.append("ruff pre-commit hooks also run on Markdown code blocks in specs - run 'ck upgrade' to limit them to Python")

    if not (project_dir / CONFIG_FILE).exists():
        warnings.append(f"{CONFIG_FILE} not found - run 'ck init' to create verification config")
    elif not load_config(project_dir).get("verify"):
        warnings.append(f'No "verify" commands in {CONFIG_FILE} - the gate can only check per-task **Verify:** lines')
    else:
        click.echo(f"✓ Verify commands: {', '.join(load_config(project_dir)['verify'])}")

    if (project_dir / LEGACY_SPECS_DIR).is_dir():
        warnings.append(
            f"Specs found in {LEGACY_SPECS_DIR}/ - run 'ck migrate' to move them to {SPECS_DIR}/"
        )

    _report_doctor_results(issues, warnings)


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would move without changing anything")
def migrate(dry_run: bool):
    """Move specs from .claude/specs/ to specs/.

    Uses `git mv` for tracked specs so history follows the files, and rewrites
    `.claude/specs/` references inside the moved Markdown files.
    """
    project_dir = Path.cwd()
    if not (project_dir / LEGACY_SPECS_DIR).is_dir():
        click.echo(f"Nothing to migrate: {LEGACY_SPECS_DIR}/ not found.")
        return

    moved, skipped = _migrate_specs(project_dir, dry_run)

    verb = "Would move" if dry_run else "Moved"
    if moved:
        click.echo(f"\n📦 {verb} to {SPECS_DIR}/:")
        for name in moved:
            click.echo(f"  ✓ {name}")
    if skipped:
        click.echo("\n⏭️  Skipped:")
        for item in skipped:
            click.echo(f"  - {item}")
    if not moved and not skipped:
        click.echo(f"No spec directories found in {LEGACY_SPECS_DIR}/.")
    if moved and not dry_run:
        click.echo("\n💡 Review with 'git status' and commit the move.")


def _git_tracked(project_dir: Path, path: Path) -> bool:
    import subprocess

    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(path)],
        cwd=project_dir,
        capture_output=True,
    )
    return result.returncode == 0


def _migrate_specs(project_dir: Path, dry_run: bool) -> tuple:
    """Move .claude/specs/* to specs/. Returns (moved names, skipped reasons)."""
    import shutil
    import subprocess

    legacy = project_dir / LEGACY_SPECS_DIR
    target_root = project_dir / SPECS_DIR
    if not legacy.is_dir():
        return [], []

    moved, skipped = [], []
    for spec_dir in sorted(p for p in legacy.iterdir() if p.is_dir()):
        dest = target_root / spec_dir.name
        if dest.exists():
            skipped.append(f"{spec_dir.name} ({SPECS_DIR}/{spec_dir.name} already exists)")
            continue
        moved.append(spec_dir.name)
        if dry_run:
            continue

        target_root.mkdir(parents=True, exist_ok=True)
        if any(_git_tracked(project_dir, f) for f in spec_dir.rglob("*") if f.is_file()):
            subprocess.run(["git", "mv", str(spec_dir), str(dest)], cwd=project_dir, check=True)
        else:
            shutil.move(str(spec_dir), str(dest))

        for md in dest.rglob("*.md"):
            text = md.read_text()
            updated = text.replace(f"{LEGACY_SPECS_DIR}/", f"{SPECS_DIR}/")
            if updated != text:
                md.write_text(updated)

    if not dry_run and legacy.is_dir() and not any(legacy.iterdir()):
        legacy.rmdir()
    return moved, skipped


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would change without writing anything")
@click.option("--no-migrate", is_flag=True, help=f"Leave specs in {LEGACY_SPECS_DIR}/")
@click.option("--allow-downgrade", is_flag=True, help="Proceed even if a newer ck wrote this project's files")
@click.option("--vendor", "to_vendor", is_flag=True, help="Keep (or switch to) vendored copies in .claude/ and refresh them")
@click.option("--plugin", "to_plugin", is_flag=True, help="Switch a vendored project to the plugin")
def upgrade(dry_run: bool, no_migrate: bool, allow_downgrade: bool, to_vendor: bool, to_plugin: bool):
    """Bring an existing Claude Kiro project up to date with this ck version.

    \b
    Projects set up by ck 0.8 or earlier carry copies of the spec commands in
    .claude/. By default this migrates them to the `spec` plugin:
    - removes each copy ck wrote that nobody edited (its hash matches
      .claude/ck-manifest.json); edited copies are kept and listed
    - removes the generated .claude/agents/ck-*.md agent types
    - removes ck's hooks from .claude/settings.local.json (the plugin has them)
    - enables spec@claude-kiro in .claude/settings.json
    A vendored project (`ck vendor`) stays vendored and gets fresh copies;
    --plugin switches it to the plugin, --vendor vendors a legacy project.
    In every mode:
    - creates specs/ck.json (verify commands, guard settings) if missing
    - adds the task worktree directory (.ck/worktrees) to .gitignore
    - moves specs from .claude/specs/ to specs/ (unless --no-migrate)

    Never touches .claude/CLAUDE.md or the content of your specs. Records the
    ck version and mode in .claude/ck-manifest.json. An older ck refuses to
    upgrade a project stamped by a newer one unless --allow-downgrade is given.
    """
    from .. import manifest

    project_dir = Path.cwd()
    if not (project_dir / ".claude").is_dir():
        click.echo("❌ No .claude/ directory here. Run 'ck init' for a new project.")
        sys.exit(1)
    if to_vendor and to_plugin:
        click.echo("❌ Pick one of --vendor and --plugin.")
        sys.exit(2)

    stamp = manifest.read(project_dir)
    if manifest.newer_than_running(stamp) and not allow_downgrade:
        click.echo(
            f"❌ This project's ck files were written by ck {stamp['version']}, but this ck is "
            f"{manifest.ck_version()}. Upgrading would replace them with older copies.\n"
            "   Reinstall the newer ck first (see UPGRADING.md), or pass --allow-downgrade if you really mean it."
        )
        sys.exit(1)

    current = plugin.mode(project_dir)
    target_mode = "vendor" if to_vendor else "plugin" if to_plugin else ("vendor" if current == "vendor" else "plugin")
    migrating = target_mode == "plugin" and current in ("legacy", "vendor")

    changes, backups, edited, kept = [], [], [], []
    if target_mode == "vendor":
        result = _write_vendored(project_dir, overwrite=True, dry_run=dry_run)
        changes += result["changes"]
        backups += result["backups"]
        edited += result["edited"]
        kept += result["kept"]
        managed = [target for target, _ in MANAGED_FILES]
    else:
        result = _migrate_to_plugin(project_dir, stamp, dry_run)
        changes += result["changes"]
        kept += result["kept"]
        managed = []

    if not (project_dir / CONFIG_FILE).exists():
        changes.append(("added", f"{CONFIG_FILE} (verify: {', '.join(initial_config(project_dir)['verify']) or 'none detected'})"))
        if not dry_run:
            _ensure_config(project_dir)

    patched = _patch_precommit(project_dir, dry_run=dry_run)
    if patched:
        changes.append(("updated", f".pre-commit-config.yaml (ruff limited to Python in {patched} hook(s))"))

    ignored = _ensure_gitignore(project_dir, dry_run=dry_run)
    if ignored:
        changes.append(("updated", f".gitignore ({ignored}: task worktrees)"))

    moved, skipped = ([], []) if no_migrate else _migrate_specs(project_dir, dry_run)
    for name in moved:
        changes.append(("moved", f"{LEGACY_SPECS_DIR}/{name} -> {SPECS_DIR}/{name}"))

    if dry_run:
        stale = not stamp or stamp.get("version") != manifest.ck_version() or stamp.get("mode") != target_mode or bool(changes)
    else:
        stale = manifest.build(project_dir, managed, target_mode) != stamp
    if stale:
        changes.append(("updated" if stamp else "added", f"{manifest.MANIFEST_FILE} (ck {manifest.ck_version()}, {target_mode} mode)"))
        if not dry_run:
            manifest.write(project_dir, managed, mode=target_mode)

    if dry_run:
        title = "Upgrade plan (dry run, nothing written)"
    elif migrating:
        title = f"Claude Kiro project moved to the {plugin.PLUGIN_ID} plugin"
    else:
        title = "Claude Kiro project upgraded" + (" (vendored)" if target_mode == "vendor" else "")
    click.echo(f"\n✨ {title}")
    if migrating:
        click.echo("   The spec commands, workflows, agents and hooks now come from the plugin; ck's copies go.")
    if not changes:
        click.echo("\n✓ Already up to date.")
    for kind, item in changes:
        click.echo(f"  {kind:>7}  {item}")
    for item in skipped:
        click.echo(f"  skipped  {item}")
    if backups:
        click.echo("\n🗂️  Backed up untracked files you may have customized:")
        for item in backups:
            click.echo(f"  - {item}")
    if edited:
        verb = "would replace" if dry_run else "replaced"
        click.echo(f"\n✋ Edited by hand since ck last wrote them ({verb}; see git diff and re-apply what you need):")
        for item in edited:
            click.echo(f"  - {item}")
    if kept:
        why = "they shadow the plugin's commands" if target_mode == "plugin" else "this ck no longer uses them"
        click.echo(
            "\n✋ Kept (edited by hand, or written by a ck too old to tell; delete them yourself once you've moved "
            f"anything you need, since {why}):"
        )
        for item in kept:
            click.echo(f"  - {item}")
    if no_migrate and (project_dir / LEGACY_SPECS_DIR).is_dir():
        click.echo(f"\n⚠️  Specs are still in {LEGACY_SPECS_DIR}/; run 'ck migrate' when ready.")

    if changes and not dry_run:
        click.echo("\n🚀 Next steps:")
        if target_mode == "plugin":
            click.echo(f"  1. Run 'ck setup' once on this machine if you haven't: it installs {plugin.PLUGIN_ID}")
            click.echo("  2. Review and commit, removals included: git status && git add -A .claude specs && git commit")
        else:
            click.echo("  1. Review: git status && git diff .claude specs")
            click.echo("  2. Commit .claude/commands, .claude/workflows, .claude/agents, .claude/output-styles, .claude/settings.json, .claude/ck-manifest.json and specs/")
        click.echo(f"  3. Check the \"verify\" command in {CONFIG_FILE} runs your test suite")
        click.echo("  4. Restart open Claude Code sessions in this project so they load the change")
        click.echo("  5. Run 'ck doctor'")


@cli.command()
@click.argument("spec")
@click.option("--task", "-t", "tasks", multiple=True, help="Task number to verify (repeatable). Default: every Done task.")
def gate(spec: str, tasks: tuple):
    """Run the verification gate for SPEC (a name in specs/ or a path).

    Checks acceptance boxes, runs the verify commands from specs/ck.json and the
    tasks' **Verify:** lines, and checks no existing test was deleted or
    weakened on this branch. Exits 1 if anything fails.
    """
    from ..gate import run_gate

    project_dir = Path.cwd()
    candidate = Path(spec)
    if not candidate.is_absolute():
        candidate = project_dir / spec
    if not (candidate / "tasks.md").exists():
        matches = [root / spec for root in spec_roots(project_dir) if (root / spec / "tasks.md").exists()]
        if not matches:
            click.echo(f"❌ No tasks.md found for '{spec}' (looked in {SPECS_DIR}/ and {LEGACY_SPECS_DIR}/)")
            sys.exit(2)
        candidate = matches[0]

    result = run_gate(project_dir, candidate, [t.removeprefix("Task ").strip() for t in tasks] or None)
    if not result.tasks and not result.checks:
        click.echo("ℹ️  No Done tasks to verify. Pass --task N to verify a specific task.")
        return
    click.echo(result.report())
    sys.exit(0 if result.ok else 1)


def _report_doctor_results(issues: list, warnings: list):
    """Report the results of doctor command."""
    if warnings:
        click.echo("\n⚠️  Warnings:")
        for warning in warnings:
            click.echo(f"  - {warning}")

    if issues:
        click.echo("\n❌ Issues found:")
        for issue in issues:
            click.echo(f"  - {issue}")

        click.echo("\n💡 To fix:")
        click.echo("  1. Run 'ck init' to set up project")
        click.echo("  2. Run 'ck hook config' for settings snippet")
        click.echo(
            "  3. Claude Code settings docs: https://docs.claude.com/en/docs/claude-code/settings"
        )
    else:
        click.echo("\n✅ Everything looks good!")
        click.echo("\n🎉 Your Claude Kiro setup is healthy and ready to use!")


# Add hook subcommand group
cli.add_command(hook)
cli.add_command(worktree)
cli.add_command(waves)
cli.add_command(plan)
cli.add_command(lint)
cli.add_command(agents)
cli.add_command(update)
cli.add_command(run)
cli.add_command(bench)


if __name__ == "__main__":
    cli()
