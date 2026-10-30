"""`ck doctor --harness opencode`: run the plugin's own preflight without a server session."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

import click

PACKAGE = "opencode-claude-kiro"


def _plugins(config: Path) -> list[Any]:
    try:
        data = json.loads(config.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    plugins = data.get("plugins") if isinstance(data, dict) else None
    return plugins if isinstance(plugins, list) else []


def find_plugin(project_dir: Path) -> Optional[Path]:
    """A local package path or an installed copy; the environment wins over the config."""
    from_env = os.environ.get("CK_OPENCODE_PLUGIN")
    if from_env:
        candidate = Path(from_env).expanduser()
        if (candidate / "package.json").is_file():
            return candidate.resolve()

    for name in ("opencode.json", "opencode.jsonc"):
        config = project_dir / name
        for entry in _plugins(config):
            package = entry if isinstance(entry, str) else entry.get("package") if isinstance(entry, dict) else None
            if not isinstance(package, str) or not package:
                continue
            candidate = Path(package).expanduser()
            if not candidate.is_absolute():
                candidate = project_dir / candidate
            if (candidate / "package.json").is_file():
                return candidate.resolve()
            local = project_dir / "node_modules" / package
            if (local / "package.json").is_file():
                return local.resolve()

    local = project_dir / "node_modules" / PACKAGE
    if (local / "package.json").is_file():
        return local.resolve()

    npm = shutil.which("npm")
    if npm:
        try:
            root = subprocess.run(
                [npm, "root", "-g"], capture_output=True, text=True, check=False, timeout=15
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            root = ""
        if root:
            global_copy = Path(root) / PACKAGE
            if (global_copy / "package.json").is_file():
                return global_copy.resolve()
    return None


def doctor_opencode(project_dir: Optional[Path] = None) -> int:
    project = (project_dir or Path.cwd()).resolve()
    click.echo("🔍 Checking OpenCode / claude-kiro setup...\n")

    package = find_plugin(project)
    if package is None:
        click.echo("✗ opencode-claude-kiro is not installed or not configured in this project.")
        click.echo("  Add it to opencode.json plugins, or set CK_OPENCODE_PLUGIN to its directory.")
        return 1

    node = shutil.which("node")
    if not node:
        click.echo("✗ node is not on PATH; the workflow runner needs Node.js 20 or newer.")
        return 1

    script = package / "scripts" / "preflight.mjs"
    if not script.is_file():
        click.echo(f"✗ {script} is missing; install a complete opencode-claude-kiro package.")
        return 1

    try:
        result = subprocess.run(
            [node, str(script), str(project)],
            cwd=project,
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        click.echo(f"✗ could not run the preflight: {error}")
        return 1

    for line in result.stdout.splitlines():
        if line.strip().startswith("{"):
            continue  # the JSON tail is for tooling; the human line is above it
        click.echo(line)
    if result.stderr.strip():
        click.echo(result.stderr.rstrip(), err=True)
    return result.returncode
