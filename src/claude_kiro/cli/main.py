"""Main CLI entry point for Claude Kiro.

Provides the `ck` command with subcommands for project management and a hidden
hook runner for Claude Code integration.
"""

import sys
import json
import logging
from pathlib import Path
from typing import Optional

import click

from ..config import initial_config, load_config
from ..paths import CONFIG_FILE, LEGACY_SPECS_DIR, SPECS_DIR, SPEC_WORKFLOW, spec_roots
from .hooks import hook
from .runner import HOOK_SETTINGS, configured_hooks, execute_hook, install_hook_settings


# Configure logging
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
logger = logging.getLogger(__name__)


@click.group(invoke_without_command=True)
@click.option(
    "--hook",
    "hook_name",
    hidden=True,
    help="Execute a hook (for Claude Code integration)",
)
@click.pass_context
def cli(ctx: click.Context, hook_name: Optional[str]):
    """Claude Kiro - Spec-driven development methodology for use with Claude Code.

    Initialize projects with spec-driven workflow, configure Claude Code hooks,
    and verify setup with the unified `ck` command.
    """
    if hook_name:
        # Hidden hook execution mode for Claude Code
        try:
            # Read JSON from stdin
            input_data = sys.stdin.read()
            input_json = json.loads(input_data) if input_data else {}

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


# Files ck owns and refreshes on upgrade. .claude/CLAUDE.md is not here: it is
# created once by `ck init` and then belongs to the project.
MANAGED_FILES = [
    (".claude/output-styles/spec-driven.md", "output_styles/spec_driven.md"),
    (".claude/commands/spec/plan.md", "commands/spec/plan.md"),
    (".claude/commands/spec/create.md", "commands/spec/create.md"),
    (".claude/commands/spec/implement.md", "commands/spec/implement.md"),
    (".claude/commands/spec/review.md", "commands/spec/review.md"),
    (".claude/commands/spawn-worktree.md", "commands/spawn-worktree.md"),
    (SPEC_WORKFLOW, "workflows/spec_create.js"),
]


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


@cli.command()
@click.option("--force", is_flag=True, help="Overwrite existing files")
def init(force: bool):
    """Initialize a Claude Kiro project in the current directory.

    Creates .claude directory structure with output styles, slash commands,
    the spec workflow, a specs/ directory, and configures hooks in
    settings.local.json.
    """
    from ..resources import ResourceLoader
    import json

    project_dir = Path.cwd()
    claude_dir = project_dir / ".claude"

    # Track what we create/skip
    created = []
    skipped = []

    # Create directory structure
    directories = [
        claude_dir,
        claude_dir / "output-styles",
        claude_dir / "commands",
        claude_dir / "commands" / "spec",
        claude_dir / "workflows",
        project_dir / SPECS_DIR,
    ]

    for directory in directories:
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            created.append(str(directory.relative_to(project_dir)))

    # Load resources and create files
    loader = ResourceLoader()

    files_to_create = [(".claude/CLAUDE.md", "claude_md.md"), *MANAGED_FILES]

    for target_path, resource_path in files_to_create:
        target = project_dir / target_path

        if target.exists() and not force:
            skipped.append(target_path)
            continue

        try:
            content = loader.get_resource(resource_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            created.append(target_path)
        except Exception as e:
            logger.error(f"Failed to create {target_path}: {e}")

    _install_hooks(project_dir)
    created.append(".claude/settings.local.json")

    if _ensure_config(project_dir):
        created.append(CONFIG_FILE)
    else:
        skipped.append(CONFIG_FILE)

    # Report results
    click.echo("\n✨ Claude Kiro initialized successfully!")

    if created:
        click.echo("\n📁 Created:")
        for item in created:
            click.echo(f"  ✓ {item}")

    if skipped:
        click.echo("\n⏭️  Skipped (already exists):")
        for item in skipped:
            click.echo(f"  - {item}")
        click.echo("\n💡 Use --force to overwrite existing files")

    # Show next steps
    click.echo("\n🚀 Next steps:")
    click.echo("  1. Review .claude/CLAUDE.md and customize for your project")
    click.echo("  2. Run 'ck doctor' to verify setup")
    click.echo("  3. Use /spec:plan to decide the approach, then /spec:create to write the spec")
    click.echo(f"\n📝 Specs are written to {SPECS_DIR}/ (outside .claude/, so no approval prompts)")
    if (project_dir / LEGACY_SPECS_DIR).is_dir():
        click.echo(f"⚠️  Found specs in {LEGACY_SPECS_DIR}/ - run 'ck migrate' to move them")
    click.echo(
        "\n📚 Claude Code hooks docs: https://docs.claude.com/en/docs/claude-code/hooks"
    )


@cli.command()
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option("--diff", "show_diff", is_flag=True, help="Show how existing global files differ from this version; write nothing")
def setup(force: bool, show_diff: bool):
    """Set up global Claude Kiro configuration in ~/.claude/.

    Installs global CLAUDE.md and skills that apply across all projects.
    Run this once per machine after installing claude-kiro. On an existing
    machine, use --diff to see what changed before deciding on --force.
    """
    from ..resources import ResourceLoader

    if show_diff:
        _setup_diff(ResourceLoader())
        return

    home_claude = Path.home() / ".claude"
    skills_dir = home_claude / "skills" / "spawn-worktree"

    # Track what we create/skip
    created = []
    skipped = []

    loader = ResourceLoader()

    # Global files to install
    global_files = [
        (home_claude / "CLAUDE.md", "global/claude_md.md"),
        (skills_dir / "SKILL.md", "global/spawn_worktree_skill.md"),
    ]

    for target, resource_path in global_files:
        if target.exists() and not force:
            skipped.append(str(target))
            continue

        try:
            content = loader.get_resource(resource_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            created.append(str(target))
        except Exception as e:
            logger.error(f"Failed to create {target}: {e}")

    # Report results
    click.echo("\n✨ Claude Kiro global setup complete!")

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
    click.echo("  1. Review ~/.claude/CLAUDE.md and customize")
    click.echo("  2. Run 'ck init' in each project to set up project-level config")


GLOBAL_FILES = [
    (Path(".claude") / "CLAUDE.md", "global/claude_md.md"),
    (Path(".claude") / "skills" / "spawn-worktree" / "SKILL.md", "global/spawn_worktree_skill.md"),
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
def doctor():
    """Check Claude Kiro setup health.

    Verifies installation, directory structure, file presence, and hook configuration.
    """
    import shutil
    import json

    project_dir = Path.cwd()
    claude_dir = project_dir / ".claude"
    issues = []
    warnings = []

    click.echo("🔍 Checking Claude Kiro setup...\n")

    # Check 1: ck command installed
    if shutil.which("ck"):
        click.echo("✓ ck command is installed")
    else:
        issues.append("ck command not found in PATH")

    # Check 2: .claude directory exists
    if claude_dir.exists():
        click.echo("✓ .claude directory exists")
    else:
        issues.append(".claude directory not found - run 'ck init'")
        # Can't continue other checks without .claude
        _report_doctor_results(issues, warnings)
        return

    # Check 3: Required files present
    required_files = [
        ".claude/output-styles/spec-driven.md",
        ".claude/commands/spec/plan.md",
        ".claude/commands/spec/create.md",
        ".claude/commands/spec/implement.md",
        ".claude/commands/spec/review.md",
        ".claude/commands/spawn-worktree.md",
        SPEC_WORKFLOW,
    ]

    missing_files = []
    for file_path in required_files:
        if not (project_dir / file_path).exists():
            missing_files.append(file_path)

    if missing_files:
        issues.append(f"Missing files: {', '.join(missing_files)}")
    else:
        click.echo("✓ All required files present")

    # Check 4: Hooks configured
    settings_file = claude_dir / "settings.local.json"
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text())
            found = configured_hooks(settings)
            for spec in HOOK_SETTINGS:
                if spec["hook"] in found.get(spec["event"], []):
                    click.echo(f"✓ {spec['event']} hook configured: ck --hook {spec['hook']}")
                else:
                    issues.append(
                        f"{spec['event']} hook 'ck --hook {spec['hook']}' not configured - run 'ck init' to fix"
                    )
            for event, groups in settings.get("hooks", {}).items():
                for group in groups if isinstance(groups, list) else []:
                    for h in group.get("hooks", []) if isinstance(group, dict) else []:
                        if isinstance(h, dict) and str(h.get("command", "")).startswith("ck --hook") and h.get("timeout", 0) > 3600:
                            warnings.append(
                                f"{event} hook timeout {h['timeout']} looks like milliseconds; Claude Code uses seconds - run 'ck init' to fix"
                            )
        except json.JSONDecodeError:
            issues.append("settings.local.json is corrupted")
    else:
        warnings.append("settings.local.json not found - hooks may not be configured")

    # Check 5: Global setup
    home_claude = Path.home() / ".claude"
    global_claude_md = home_claude / "CLAUDE.md"
    spawn_skill = home_claude / "skills" / "spawn-worktree" / "SKILL.md"

    if global_claude_md.exists():
        click.echo("✓ Global ~/.claude/CLAUDE.md found")
    else:
        warnings.append("Global ~/.claude/CLAUDE.md not found - run 'ck setup'")

    if spawn_skill.exists():
        click.echo("✓ Global spawn-worktree skill found")
    else:
        warnings.append("spawn-worktree skill not found - run 'ck setup'")

    # Check 6: Count existing specs
    roots = spec_roots(project_dir)
    spec_count = sum(len(list(root.glob("*/requirements.md"))) for root in roots)
    if spec_count > 0:
        click.echo(f"✓ Found {spec_count} spec(s)")
    else:
        click.echo(f"ℹ️  No specs created yet (they go in {SPECS_DIR}/)")

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
def upgrade(dry_run: bool, no_migrate: bool):
    """Bring an existing Claude Kiro project up to date with this ck version.

    \b
    - Refreshes ck-managed files: spec commands, /spawn-worktree, the output
      style, and the spec-create workflow
    - Merges the current hooks into .claude/settings.local.json, keeping hooks
      from other tools and fixing old millisecond timeouts
    - Creates specs/ck.json (verify commands, guard settings) if missing
    - Moves specs from .claude/specs/ to specs/ (unless --no-migrate)

    Never touches .claude/CLAUDE.md or the content of your specs. Managed files
    that differ from the new version and are not tracked by git are backed up
    as <file>.bak first.
    """
    from ..resources import ResourceLoader

    project_dir = Path.cwd()
    if not (project_dir / ".claude").is_dir():
        click.echo("❌ No .claude/ directory here. Run 'ck init' for a new project.")
        sys.exit(1)

    loader = ResourceLoader()
    changes, backups = [], []
    for target_path, resource_path in MANAGED_FILES:
        target = project_dir / target_path
        content = loader.get_resource(resource_path)
        if target.exists() and target.read_text() == content:
            continue
        changes.append(("updated" if target.exists() else "added", target_path))
        if dry_run:
            continue
        if target.exists() and not _git_tracked(project_dir, target):
            backup = target.with_name(target.name + ".bak")
            backup.write_text(target.read_text())
            backups.append(str(backup.relative_to(project_dir)))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    settings_file = project_dir / ".claude" / "settings.local.json"
    before = settings_file.read_text() if settings_file.exists() else ""
    try:
        merged = json.dumps(install_hook_settings(json.loads(before) if before else {}), indent=2)
    except json.JSONDecodeError:
        merged = None
    if merged != before:
        changes.append(("updated" if before else "added", ".claude/settings.local.json (hooks)"))
        if not dry_run:
            _install_hooks(project_dir)

    if not (project_dir / CONFIG_FILE).exists():
        changes.append(("added", f"{CONFIG_FILE} (verify: {', '.join(initial_config(project_dir)['verify']) or 'none detected'})"))
        if not dry_run:
            _ensure_config(project_dir)

    moved, skipped = ([], []) if no_migrate else _migrate_specs(project_dir, dry_run)
    for name in moved:
        changes.append(("moved", f"{LEGACY_SPECS_DIR}/{name} -> {SPECS_DIR}/{name}"))

    title = "Upgrade plan (dry run, nothing written)" if dry_run else "Claude Kiro project upgraded"
    click.echo(f"\n✨ {title}")
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
    if no_migrate and (project_dir / LEGACY_SPECS_DIR).is_dir():
        click.echo(f"\n⚠️  Specs are still in {LEGACY_SPECS_DIR}/; run 'ck migrate' when ready.")

    if changes and not dry_run:
        click.echo("\n🚀 Next steps:")
        click.echo("  1. Review: git status && git diff .claude specs")
        click.echo(f"  2. Check the \"verify\" command in {CONFIG_FILE} runs your test suite")
        click.echo("  3. Commit .claude/commands, .claude/workflows, .claude/output-styles and specs/")
        click.echo("  4. Restart open Claude Code sessions in this project so the new hooks load")
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


if __name__ == "__main__":
    cli()
