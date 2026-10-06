"""stop_verify must not treat a fresh checkout's mtimes as edits to every spec."""

import os
import time

from claude_kiro.hooks._shared.cache_manager import CacheManager
from claude_kiro.hooks._shared.session_tracker import SessionTracker
from claude_kiro.hooks.stop_verify import _candidates

from conftest import git, write_tasks


def _tracker(tmp_path, session):
    return SessionTracker(session, CacheManager(tmp_path / "cache"))


def _bump_mtime(project):
    future = time.time() + 5
    os.utime(project / "specs" / "calc" / "tasks.md", (future, future))


def test_unchanged_tasks_md_with_fresh_mtime_is_not_a_candidate(project, session, tmp_path):
    write_tasks(project, t1="Done", t2="Done", a1="x", a2="x")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "done")
    tracker = _tracker(tmp_path, session)
    _bump_mtime(project)  # what a fresh worktree checkout does to every tasks.md
    assert _candidates(project, tracker) == {}


def test_bash_edit_of_tasks_md_is_still_a_candidate(project, session, tmp_path):
    tracker = _tracker(tmp_path, session)
    write_tasks(project, t1="Done", a1="x")
    _bump_mtime(project)
    assert _candidates(project, tracker) == {project / "specs" / "calc": ["1"]}
