"""`ck agents`: which model and effort each spec-workflow role uses."""

import json
import sys
from pathlib import Path

import click

from .. import agents as ag
from ..paths import CONFIG_FILE


def _fail(e: Exception) -> None:
    click.echo(f"❌ {e}")
    sys.exit(2)


@click.group(invoke_without_command=True)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
@click.option("--override", "overrides", multiple=True, metavar="ROLE.KEY=VALUE", help="Override a setting for this call only")
@click.pass_context
def agents(ctx: click.Context, as_json: bool, overrides: tuple):
    """Show the model and effort each spec-workflow role uses.

    \b
    Precedence (lowest first): built-in defaults, global preferences
    (~/.config/claude-kiro/config.json), the project's specs/ck.json, then
    --override for one call. Change them with `ck agents set`.
    """
    if ctx.invoked_subcommand:
        return
    try:
        resolved = ag.resolve(Path.cwd(), list(overrides))
    except ag.AgentConfigError as e:
        _fail(e)
    if as_json:
        click.echo(json.dumps(resolved, indent=2))
        return
    roles, sources = resolved["roles"], resolved["sources"]
    click.echo("Spec workflow agents:")
    for role, settings in roles.items():
        unset = "unlimited" if role == "run" else "(same as implementer)"
        parts = ", ".join(f"{k}={unset if v is None else v}" for k, v in settings.items())
        origin = sorted({sources[f"{role}.{k}"] for k in settings} - {"default"})
        click.echo(f"  {role:12} {parts}" + (f"   [{'; '.join(origin)}]" if origin else ""))
    click.echo(f"\nGlobal preferences: {ag.global_config_path()}\nProject settings:   {CONFIG_FILE}")


@agents.command("set")
@click.argument("setting", metavar="ROLE.KEY")
@click.argument("value")
@click.option("--project", is_flag=True, help=f"Write to this project's {CONFIG_FILE} instead of the global preferences")
def set_cmd(setting: str, value: str, project: bool):
    """Save a setting, e.g. `ck agents set implementer.model haiku`.

    VALUE "default" removes the setting so the next layer down applies.
    """
    try:
        role, key, val = ag.parse_override(f"{setting}={value}")
        path = Path.cwd() / CONFIG_FILE if project else ag.global_config_path()
        ag.write_setting(path, role, key, val)
    except ag.AgentConfigError as e:
        _fail(e)
    click.echo(f"✓ {role}.{key} = {value} in {path}")
    if (Path.cwd() / ".claude").is_dir():
        _report_sync(ag.sync_agent_types(Path.cwd()))


def _report_sync(result: dict) -> None:
    for fname in result["changed"]:
        click.echo(f"  wrote {ag.AGENTS_DIR}/{fname}")
    for fname in result["skipped"]:
        click.echo(f"  ⚠ {ag.AGENTS_DIR}/{fname} was not written by ck; left alone")
    if result["changed"] and result["created_dir"]:
        click.echo(f"  {ag.AGENTS_DIR}/ is new: restart open Claude Code sessions so they see the ck-* agent types.")
    elif result["changed"]:
        click.echo("  Open Claude Code sessions pick this up within seconds.")


@agents.command("sync")
@click.option("--override", "overrides", multiple=True, metavar="ROLE.KEY=VALUE", help="Override a setting for this run")
@click.option("--dry-run", is_flag=True, help="Report what would change without writing")
@click.option("--json", "as_json", is_flag=True)
def sync(overrides: tuple, dry_run: bool, as_json: bool):
    """Write the ck-* agent types (.claude/agents/) with each role's model and effort.

    \b
    For running spec commands without the Workflow tool: the Agent tool can pick
    a model per call but not an effort, so ck-implementer, ck-test-writer,
    ck-fixer and ck-reviewer carry both. `ck agents set` and `ck upgrade` keep
    them current; run this with --override for a one-off run.
    """
    try:
        result = ag.sync_agent_types(Path.cwd(), list(overrides), dry_run=dry_run)
    except ag.AgentConfigError as e:
        _fail(e)
    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    if not result["changed"] and not result["skipped"]:
        click.echo(f"✓ {ag.AGENTS_DIR}/ck-*.md already match `ck agents`.")
    elif dry_run:
        click.echo("Would write: " + ", ".join(result["changed"]))
    else:
        _report_sync(result)


@agents.command("check")
@click.option("--model", required=True, help="The session's model, e.g. claude-opus-5-5")
@click.option("--effort", default=None, help="The session's effort level, if known")
@click.option("--json", "as_json", is_flag=True)
def check(model: str, effort: str, as_json: bool):
    """Exit 0 if MODEL/EFFORT meet the planning minimum, 1 if not (/spec:plan and /spec:create ask then)."""
    try:
        planning = ag.resolve(Path.cwd())["roles"]["planning"]
    except ag.AgentConfigError as e:
        _fail(e)
    ok, why = ag.meets_planning_bar(model, effort, planning)
    result = {"ok": ok, "reason": why, "ask": planning["ask"], "min_model": planning["min_model"], "min_effort": planning["min_effort"]}
    if as_json:
        click.echo(json.dumps(result))
    else:
        click.echo(("✓ " if ok else "⚠ ") + (f"meets the planning minimum ({planning['min_model']}, {planning['min_effort']})" if ok else why))
    sys.exit(0 if ok else 1)
