"""`ck update`: reinstall ck, add any missing global files, and upgrade this project, in one step."""

import shutil
import subprocess
import sys
from pathlib import Path

import click

DEFAULT_SOURCE = "git+https://github.com/kutlayhanli/claude-kiro"


def _run(cmd, cwd=None) -> int:
    click.echo(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd).returncode


def _is_ck_project(path: Path) -> bool:
    from ..plugin import mode

    return (path / ".claude" / "ck-manifest.json").exists() or mode(path) is not None


@click.command()
@click.option("--ref", default=None, help="Branch, tag, or commit to install (default: the repository's default branch)")
@click.option("--source", envvar="CK_SOURCE", default=DEFAULT_SOURCE, show_default=True, help="Where to install ck from (env: CK_SOURCE)")
@click.option("--project/--no-project", default=True, help="Also run `ck upgrade` here if this is a claude-kiro project")
@click.option("--dry-run", is_flag=True, help="Print the commands without running them")
def update(ref, source, project, dry_run):
    """Update ck and this project in one step.

    \b
    1. Reinstall ck with uv from SOURCE (at --ref, if given)
    2. `ck setup`: install or update the spec plugin, and add ~/.claude/CLAUDE.md
       if missing (never overwrites; review changes with `ck setup --diff`)
    3. `ck upgrade` in the current directory, if it is a claude-kiro project
       (a project with copies from ck 0.8 or earlier moves to the plugin)

    Wait until any running /spec:implement workflow has finished: its agents call ck.
    """
    spec = f"{source}@{ref}" if ref else source
    install = ["uv", "tool", "install", "--force", "--reinstall", spec]
    here = Path.cwd()
    upgrade_here = project and _is_ck_project(here)

    if dry_run:
        click.echo(" ".join(install))
        click.echo("ck setup")
        if upgrade_here:
            click.echo(f"ck upgrade   (in {here})")
        return

    if not shutil.which("uv"):
        click.echo("❌ uv is not on PATH. Install it (https://docs.astral.sh/uv/), or reinstall ck by hand.")
        sys.exit(2)
    if _run(install) != 0:
        click.echo("❌ Reinstalling ck failed; nothing else was changed.")
        sys.exit(1)

    ck = shutil.which("ck") or "ck"  # the freshly installed one
    _run([ck, "setup"])
    click.echo("  A changed ~/.claude/CLAUDE.md is not overwritten. Review it with: ck setup --diff")

    if upgrade_here:
        if _run([ck, "upgrade"], cwd=here) != 0:
            click.echo("❌ ck upgrade failed in this project; see above.")
            sys.exit(1)
        click.echo("  Commit what changed: git status && git add -A .claude specs")
    elif project:
        click.echo("  Not a claude-kiro project here; run `ck upgrade` in each project set up by ck 0.8 or earlier.")
    click.echo("✓ Updated. Restart open Claude Code sessions so they load the new plugin version.")
