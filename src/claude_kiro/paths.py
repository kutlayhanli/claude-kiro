"""Project-relative locations used across the CLI, hooks, and templates."""

import fnmatch
from pathlib import Path, PurePosixPath
from typing import List, Optional

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

# Workflow that /spec:implement runs for a whole spec, and the brief its task agents read.
IMPLEMENT_WORKFLOW = ".claude/workflows/spec-implement.js"
IMPLEMENT_BRIEF = ".claude/workflows/spec-implement-brief.md"
# How the spec commands run when the Workflow tool is unavailable (Agent tool + task list).
NO_WORKFLOW_GUIDE = ".claude/workflows/without-workflow-tool.md"

# Where `ck worktree` puts task worktrees (relative to the main checkout).
WORKTREES_DIR = ".claude/worktrees"

# Repo hygiene and scaffolding: editing these is never feature work, so the
# spec-context hook stays silent about them. File-name globs, plus top-level dirs.
SCAFFOLD_FILES = [
    ".gitignore", ".gitattributes", ".gitmodules", ".editorconfig", ".env.example",
    ".pre-commit-config.yaml", ".python-version", ".nvmrc", ".tool-versions",
    ".prettierrc*", ".eslintrc*", "eslint.config.*", ".dockerignore",
    "pyproject.toml", "setup.cfg", "setup.py", "requirements*.txt", "uv.lock", "poetry.lock", "Pipfile*",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "tsconfig*.json",
    "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile*",
    "Makefile", "justfile", "Dockerfile", "docker-compose*.y*ml", "compose*.y*ml",
    "README*", "LICENSE*", "CHANGELOG*", "CONTRIBUTING*", "UPGRADING*", "SECURITY*", "CODE_OF_CONDUCT*",
    "CODEOWNERS", "CLAUDE.md", "AGENTS.md",
]
SCAFFOLD_DIRS = {".claude", ".github", ".vscode", ".idea", ".devcontainer", "worktrees"}


def is_scaffold_path(rel_path: str, extra_patterns: Optional[List[str]] = None) -> bool:
    """Whether a project-relative path is repo hygiene the spec hooks should ignore.

    `extra_patterns` (specs/ck.json "context_ignore") are globs matched against
    the whole relative path, e.g. "scripts/*" or "notebooks/**".
    """
    path = PurePosixPath(rel_path)
    if path.parts and path.parts[0] in SCAFFOLD_DIRS:
        return True
    if any(path.match(pattern) for pattern in SCAFFOLD_FILES):
        return True
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in extra_patterns or [])
