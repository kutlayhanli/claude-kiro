"""Small read-only git helpers for the gate and guard."""

import hashlib
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple


def _git(project_dir: Path, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args], cwd=project_dir, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def is_repo(project_dir: Path) -> bool:
    return _git(project_dir, "rev-parse", "--is-inside-work-tree") is not None


def base_ref(project_dir: Path, configured: Optional[str] = None) -> Optional[str]:
    """Commit to diff against: merge-base of HEAD with the configured or default branch."""
    candidates = [configured] if configured else ["origin/HEAD", "origin/main", "origin/master", "main", "master"]
    for ref in candidates:
        if ref and _git(project_dir, "rev-parse", "--verify", "--quiet", ref):
            merge_base = _git(project_dir, "merge-base", "HEAD", ref)
            if merge_base:
                return merge_base.strip()
    head = _git(project_dir, "rev-parse", "HEAD")
    return head.strip() if head else None


def changed_files(project_dir: Path, base: str) -> List[Tuple[str, str]]:
    """(status, path) for files changed since `base`, including uncommitted and untracked."""
    changes = {}
    out = _git(project_dir, "diff", "--name-status", "--no-renames", base, "--") or ""
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            changes[parts[-1]] = parts[0][0]
    untracked = _git(project_dir, "ls-files", "--others", "--exclude-standard") or ""
    for path in untracked.splitlines():
        changes.setdefault(path, "A")
    return sorted((status, path) for path, status in changes.items())


def project_files(project_dir: Path) -> Optional[List[str]]:
    """Tracked and untracked (not ignored) files, project-relative; None outside git."""
    out = _git(project_dir, "ls-files", "--cached", "--others", "--exclude-standard")
    return None if out is None else out.splitlines()


def is_modified(project_dir: Path, path: Path) -> Optional[bool]:
    """Whether `path` differs from HEAD (modified, staged, or untracked).

    None when git cannot tell (not a repo), so callers can fall back to mtime.
    """
    out = _git(project_dir, "status", "--porcelain", "--", str(path))
    return None if out is None else bool(out.strip())


def show(project_dir: Path, ref: str, rel_path: str) -> Optional[str]:
    """File content at `ref`, or None if it did not exist there."""
    return _git(project_dir, "show", f"{ref}:{rel_path}")


def exists_at(project_dir: Path, ref: str, rel_path: str) -> bool:
    return _git(project_dir, "cat-file", "-e", f"{ref}:{rel_path}") is not None


# Untracked artifacts that running a test suite creates; they must not make
# the tree look changed, or every verified state would be re-verified.
_CACHE_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".coverage", "coverage", ".nyc_output", "target", ".tox"}


def worktree_fingerprint(project_dir: Path) -> str:
    """Changes whenever HEAD, tracked content, or untracked source files change."""
    digest = hashlib.sha256()
    digest.update((_git(project_dir, "rev-parse", "HEAD") or "").encode())
    digest.update((_git(project_dir, "diff", "HEAD") or "").encode())
    untracked = _git(project_dir, "ls-files", "--others", "--exclude-standard") or ""
    for rel in sorted(untracked.splitlines()):
        if _CACHE_PARTS.intersection(Path(rel).parts) or rel.endswith((".pyc", ".pyo")):
            continue
        digest.update(rel.encode())
        try:
            digest.update((project_dir / rel).read_bytes())
        except OSError:
            pass
    return digest.hexdigest()[:16]
