"""Spec file parsing for Claude Kiro hooks.

Parses tasks.md files to find which files are associated with which tasks.
"""

import os
import glob
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, NamedTuple

from claude_kiro.paths import spec_roots


class TaskMatch(NamedTuple):
    """Result of matching a file to a spec task."""

    spec_name: str
    task_num: str
    task_title: str
    spec_dir: Path


class SpecParser:
    """Parser for Claude Kiro spec files."""

    def __init__(self, project_dir: Optional[Path] = None):
        """Initialize spec parser.

        Args:
            project_dir: Project root directory. Defaults to current directory.
        """
        self.project_dir = Path(project_dir) if project_dir else Path.cwd()

    @property
    def spec_roots(self) -> List[Path]:
        """Existing spec roots (specs/, then legacy .claude/specs/)."""
        return spec_roots(self.project_dir)

    def find_spec_files(self) -> List[Path]:
        """Find all tasks.md files in every spec root.

        Returns:
            List of paths to tasks.md files.
        """
        files = []
        for root in self.spec_roots:
            files.extend(Path(p) for p in glob.glob(str(root / "*" / "tasks.md")))
        return files

    def parse_task_file(self, task_file_path: Path) -> List[Tuple[str, str, List[str]]]:
        """Parse tasks.md and extract file mentions.

        Expected format:
            ### Task 11: Implement accessibility features
            **Files:**
            - `docs/index.html` - Enhance with accessibility attributes
            - `src/components/Header.tsx` - Add ARIA labels

        Args:
            task_file_path: Path to tasks.md file.

        Returns:
            List of tuples: (task_number, task_title, [files])
        """
        tasks = []

        try:
            with open(task_file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, IOError):
            return tasks

        # Split by task headers (### Task N: Title)
        task_pattern = r"### Task ?(\d+): (.+?)(?=\n### Task ?\d|\Z)"
        task_matches = re.finditer(task_pattern, content, re.DOTALL)

        for match in task_matches:
            task_num = match.group(1)
            # Extract just the title (first line, no newlines)
            task_title = match.group(2).split("\n")[0].strip()
            task_content = match.group(0)  # Use full match for file extraction

            # Extract files from **Files:** section
            files = []
            # Look for files in backticks with dash prefix
            file_pattern = r"- `([^`]+)`"
            file_matches = re.finditer(file_pattern, task_content)

            for file_match in file_matches:
                file_path = file_match.group(1)
                # Normalize the path (remove ./ prefix if present)
                file_path = file_path.lstrip("./")
                files.append(file_path)

            if files:
                tasks.append((task_num, task_title, files))

        return tasks

    def _relative(self, file_path: str) -> Optional[str]:
        """Project-relative POSIX path, or None for files outside the project."""
        path = Path(file_path)
        if not path.is_absolute():
            path = self.project_dir / path
        try:
            return path.resolve().relative_to(self.project_dir.resolve()).as_posix()
        except ValueError:
            return None

    @staticmethod
    def _lists(task_files: List[str], rel: str) -> bool:
        for listed in task_files:
            listed = listed.removeprefix("./")
            if rel == listed or (listed.endswith("/") and rel.startswith(listed)):
                return True
        return False

    def _tasks_by_recency(self):
        """(spec_dir, tasks) for every spec, most recently edited tasks.md first."""
        spec_files = sorted(self.find_spec_files(), key=lambda f: f.stat().st_mtime, reverse=True)
        return [(f.parent, parse_tasks(f)) for f in spec_files]

    def find_matching_task(self, file_path: str) -> Optional[TaskMatch]:
        """The In Progress task whose **Files:** list this exact file, if any.

        Only In Progress tasks count: naming a Not Started or Done task during an
        unrelated edit is wrong context. Paths are compared exactly, relative to
        the project root (no suffix matching, so `README.md` never matches
        `docs/README.md`).
        """
        rel = self._relative(file_path)
        if rel is None:
            return None
        for spec_dir, tasks in self._tasks_by_recency():
            for task in tasks:
                if task.in_progress and self._lists(task.files, rel):
                    return TaskMatch(spec_dir.name, task.num, task.title, spec_dir)
        return None

    def listed_by_any_task(self, file_path: str) -> bool:
        """Whether any task, in any status, lists this file."""
        rel = self._relative(file_path)
        if rel is None:
            return False
        return any(self._lists(t.files, rel) for _, tasks in self._tasks_by_recency() for t in tasks)

    def in_progress_tasks(self) -> List[TaskMatch]:
        """Every In Progress task across specs, most recently edited spec first."""
        return [
            TaskMatch(spec_dir.name, t.num, t.title, spec_dir)
            for spec_dir, tasks in self._tasks_by_recency()
            for t in tasks
            if t.in_progress
        ]

    def get_all_spec_names(self) -> List[str]:
        """Get list of all spec names in the project.

        Returns:
            List of spec directory names.
        """
        spec_names = set()
        for root in self.spec_roots:
            for spec_dir in root.iterdir():
                if spec_dir.is_dir() and (spec_dir / "tasks.md").exists():
                    spec_names.add(spec_dir.name)

        return sorted(spec_names)

    def get_spec_files(self, spec_name: str) -> dict:
        """Get all spec files for a given spec.

        Args:
            spec_name: Name of the spec directory.

        Returns:
            Dictionary mapping file type to path.
        """
        files = {}
        spec_dir = next(
            (root / spec_name for root in self.spec_roots if (root / spec_name).is_dir()),
            None,
        )

        if spec_dir:
            for file_type in ["requirements.md", "design.md", "tasks.md"]:
                file_path = spec_dir / file_type
                if file_path.exists():
                    files[file_type.replace(".md", "")] = file_path

        return files


@dataclass
class SpecTask:
    """One "### Task N: Title" block from tasks.md, with the fields the gate uses."""

    num: str
    title: str
    status: str = "Not Started"
    track: str = "impl"  # "impl" or "test"
    files: List[str] = field(default_factory=list)
    verify: List[str] = field(default_factory=list)  # **Verify:** `cmd` `cmd`
    verified_by: List[str] = field(default_factory=list)  # **Verified by:** Task 7
    verifies: List[str] = field(default_factory=list)  # **Verifies:** Task 2
    dependencies: List[str] = field(default_factory=list)  # **Dependencies:** Task 1, Task 3
    acceptance: List[Tuple[bool, str]] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return _status_key(self.status) in ("done", "complete", "completed")

    @property
    def in_progress(self) -> bool:
        return _status_key(self.status) == "in progress"


def _status_key(status: str) -> str:
    """Normalize "✅ Done", "**Done**", "In Progress (blocked)" style statuses."""
    words = re.sub(r"[^a-z ]", " ", status.lower()).split()
    joined = " ".join(words)
    for key in ("in progress", "not started", "done", "completed", "complete", "evolved", "blocked"):
        if joined.startswith(key):
            return key
    return joined


def _field(block: str, name: str) -> Optional[str]:
    match = re.search(rf"^\*\*{name}:\*\*\s*(.*)$", block, re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else None


def _task_refs(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return re.findall(r"(?:Task\s*)?(\d+)", value, re.IGNORECASE)


def parse_tasks(task_file_path: Path) -> List[SpecTask]:
    """Parse every task block in tasks.md, including tasks with no files."""
    try:
        content = Path(task_file_path).read_text(encoding="utf-8")
    except (OSError, IOError):
        return []

    tasks = []
    for match in re.finditer(r"^### Task ?(\d+): (.+?)(?=^### Task ?\d|^## |\Z)", content, re.DOTALL | re.MULTILINE):
        block = match.group(0)
        task = SpecTask(num=match.group(1), title=match.group(2).split("\n")[0].strip())

        status = _field(block, "Status")
        if status:
            task.status = status
        elif re.search(r"✅|\b(?:COMPLETE|DONE)\b", task.title):
            task.status = "Done"  # older specs put the status in the title
        track = (_field(block, "Track") or "").lower()
        if track.startswith("test"):
            task.track = "test"

        task.files = [f.removeprefix("./") for f in re.findall(r"^- `([^`]+)`", block, re.MULTILINE)]
        task.verify = re.findall(r"`([^`]+)`", _field(block, "Verify") or "")
        task.verified_by = _task_refs(_field(block, "Verified by"))
        task.verifies = _task_refs(_field(block, "Verifies"))
        task.dependencies = _task_refs(_field(block, "Dependencies"))
        task.acceptance = [
            (mark.lower() == "x", text.strip())
            for mark, text in re.findall(r"^\s*- \[([ xX])\]\s*(.+)$", block, re.MULTILINE)
        ]
        tasks.append(task)
    return tasks
