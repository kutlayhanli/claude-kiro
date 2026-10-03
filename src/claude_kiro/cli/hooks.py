"""Hook management subcommands for the Claude Kiro CLI."""

import json
import sys
from pathlib import Path
from typing import Optional, Dict, Any

import click

from .runner import HOOK_SETTINGS, HookRegistry, configured_hooks, install_hook_settings


@click.group()
def hook():
    """Manage hooks for Claude Code integration with Claude Kiro specs."""
    pass


@hook.command()
def list():
    """List all available hook modules."""
    registry = HookRegistry()
    click.echo("Available hooks:")
    for hook_name in sorted(registry.list_hooks()):
        click.echo(f"\n  📎 {hook_name}")
        click.echo(f"     Module: {registry.get_hook(hook_name)}")
        description = registry.descriptions.get(hook_name)
        if description:
            click.echo(f"     Description: {description}")


@hook.command()
def status():
    """Show which Claude Kiro hooks are configured in settings.local.json."""
    settings_file = Path.cwd() / ".claude" / "settings.local.json"

    if not settings_file.exists():
        click.echo("❌ No settings.local.json found")
        click.echo("\n💡 Run 'ck init' to create it")
        return

    try:
        settings = json.loads(settings_file.read_text())
    except json.JSONDecodeError as e:
        click.echo(f"❌ Failed to parse settings.local.json: {e}")
        return

    found = configured_hooks(settings)
    click.echo("Claude Kiro hooks:")
    for spec in HOOK_SETTINGS:
        ok = spec["hook"] in found.get(spec["event"], [])
        click.echo(f"  {'✓' if ok else '✗'} {spec['event']:<13} ck --hook {spec['hook']}")
    if not all(spec["hook"] in found.get(spec["event"], []) for spec in HOOK_SETTINGS):
        click.echo("\n💡 Run 'ck init' to install missing hooks (other hooks are kept)")


@hook.command()
@click.argument("name")
@click.argument("json_file", type=click.Path(exists=True), required=False)
def test(name: str, json_file: Optional[str]):
    """Test a hook with sample JSON data.

    \b
    NAME: Hook name to test (e.g., post-file-ops)
    JSON_FILE: Path to JSON file with test data (optional, uses empty JSON if not provided)
    """
    registry = HookRegistry()

    # Check if hook exists
    if name not in registry.list_hooks():
        click.echo(f"❌ Hook '{name}' not found")
        click.echo("\nAvailable hooks:")
        for hook_name in registry.list_hooks():
            click.echo(f"  - {hook_name}")
        sys.exit(1)

    # Load test data
    test_data: Dict[str, Any] = {}
    if json_file:
        try:
            with open(json_file, "r") as f:
                test_data = json.load(f)
            click.echo(f"📄 Loaded test data from {json_file}")
        except json.JSONDecodeError as e:
            click.echo(f"❌ Failed to parse JSON: {e}")
            sys.exit(1)
    else:
        click.echo("📄 Using empty JSON input")

    # Execute the hook
    click.echo(f"\n🔧 Executing hook: {name}")
    click.echo("=" * 40)

    try:
        from .runner import execute_hook

        result = execute_hook(name, test_data)

        if result:
            click.echo("\n📤 Output:")
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo("\n✓ Hook executed successfully (no output)")

    except Exception as e:
        click.echo(f"\n❌ Hook execution failed: {e}")
        sys.exit(1)


@hook.command()
def config():
    """Generate settings.json snippet for hook configuration."""
    click.echo("📋 Add this to your .claude/settings.local.json:\n")

    config_snippet = install_hook_settings({})

    click.echo(json.dumps(config_snippet, indent=2))

    click.echo("\n💡 Tips:")
    click.echo("  - settings.local.json is for local overrides")
    click.echo("  - settings.json is for shared project settings")
    click.echo("  - Claude Code merges both, with local taking precedence")
    click.echo(
        "\n📚 Learn more about hooks: https://docs.claude.com/en/docs/claude-code/hooks"
    )
