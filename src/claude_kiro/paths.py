"""Project-relative locations used across the CLI, hooks, and templates."""

from pathlib import Path
from typing import List

# Specs live at the project root, outside .claude/, so Claude can write them
# without the per-edit approval Claude Code requires for .claude/.
SPECS_DIR = "specs"

# Where specs lived before 0.2. Still read so older projects keep working;
# `ck migrate` moves them to SPECS_DIR.
LEGACY_SPECS_DIR = ".claude/specs"

# Workflow script that /spec:create and /spec:review run.
SPEC_WORKFLOW = ".claude/workflows/spec-create.js"


def spec_roots(project_dir: Path) -> List[Path]:
    """Spec root directories that exist in a project, current location first."""
    roots = [project_dir / SPECS_DIR, project_dir / LEGACY_SPECS_DIR]
    return [root for root in roots if root.is_dir()]


def is_spec_path(file_path: str, project_dir: Path) -> bool:
    """Whether a path points inside a spec root (current or legacy)."""
    path = Path(file_path)
    if not path.is_absolute():
        path = project_dir / path
    for root in (SPECS_DIR, LEGACY_SPECS_DIR):
        try:
            path.resolve().relative_to((project_dir / root).resolve())
            return True
        except ValueError:
            continue
    return False


# Verification and guard settings (verify commands, test patterns, guard modes).
CONFIG_FILE = f"{SPECS_DIR}/ck.json"
