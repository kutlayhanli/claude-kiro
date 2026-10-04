"""PostToolUse spec-context hook: names only the In Progress task, ignores scaffolding."""

import json
from pathlib import Path

import pytest

from claude_kiro.cli.runner import execute_hook

TASKS = """# Implementation Tasks: Demo

### Task 1: Build the parser
**Status:** {s1}
**Files:**
- `src/parser.py` - parser
- `src/config.py` - settings

### Task 2: Docs and config
**Status:** {s2}
**Files:**
- `CLAUDE.md` - conventions
- `config.py` - root config
- `src/report.py` - report
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "specs/demo").mkdir(parents=True)
    (tmp_path / "src").mkdir()
    write(tmp_path, "In Progress", "Not Started")
    return tmp_path


def write(root: Path, s1: str, s2: str):
    (root / "specs/demo/tasks.md").write_text(TASKS.format(s1=s1, s2=s2))


def edit(project: Path, session: str, path: str):
    result = execute_hook(
        "post-file-ops",
        {"session_id": session, "cwd": str(project), "tool_name": "Edit", "tool_input": {"file_path": path}},
    )
    return result and result["hookSpecificOutput"]["additionalContext"]


def test_names_the_in_progress_task(project, session):
    context = edit(project, session, str(project / "src/parser.py"))
    assert "Task 1: Build the parser" in context


def test_silent_for_files_of_tasks_not_in_progress(project, session):
    assert edit(project, session, "src/report.py") is None


def test_no_suffix_misattribution(project, session):
    # `.claude/CLAUDE.md` used to become `claude/CLAUDE.md` and match Task 2's `CLAUDE.md`.
    assert edit(project, session, str(project / ".claude/CLAUDE.md")) is None
    # `lib/config.py` must not match Task 2's root `config.py`; with Task 1 In Progress
    # it gets the outside-task note naming Task 1, never Task 2.
    context = edit(project, session, "lib/config.py")
    assert "not listed in Task 1" in context and "Task 2" not in context


def test_no_in_progress_task_means_no_task_named(project, session):
    write(project, "Done", "Not Started")
    assert edit(project, session, "src/parser.py") is None  # listed by a Done task
    context = edit(project, session, "src/new_feature.py")
    assert "File Not in Specification" in context


@pytest.mark.parametrize(
    "path",
    [".gitignore", "pyproject.toml", "uv.lock", "README.md", "docs/README.md", "Makefile",
     ".pre-commit-config.yaml", ".github/workflows/ci.yml", ".claude/settings.json", "LICENSE"],
)
def test_scaffold_files_are_ignored(project, session, path):
    write(project, "Done", "Done")
    assert edit(project, session, path) is None


def test_files_outside_the_project_are_ignored(project, session, tmp_path_factory):
    outside = tmp_path_factory.mktemp("scratch") / "helper.sh"
    assert edit(project, session, str(outside)) is None


def test_context_ignore_patterns(project, session):
    write(project, "Done", "Done")
    (project / "specs/ck.json").write_text(json.dumps({"context_ignore": ["scripts/*", "notebooks/**"]}))
    assert edit(project, session, "scripts/migrate.py") is None
    assert edit(project, session, "notebooks/a/b.ipynb") is None
    assert "File Not in Specification" in edit(project, session, "src/other.py")
