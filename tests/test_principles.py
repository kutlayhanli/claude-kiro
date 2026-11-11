"""The principles file and the two layers, through the real CLI (run-principles Task 7).

TC-1, TC-3, TC-140 (`ck principles init`), TC-8, TC-10, TC-153 (`ck arbiter open` / `settle`).
Real git repos, real files, real subprocesses. Project modules are never imported here.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from arbiter_helpers import DEFAULT_PLAN, arbiter_project, ck_run

DEFAULTS = ("priority-order", "test-intent", "fail-closed", "remove-first")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _json(proc: subprocess.CompletedProcess) -> dict:
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def _claim(task: str = "1") -> str:
    claim = {"task": task, "trigger": "test-conflict", "text": "the greeting test contradicts the criterion",
             "file": "tests/test_greet.py", "test": "test_greets", "criterion": "1.1"}
    return json.dumps({"claim": claim, "task": {"risk": "normal", "complexity": "Low"}})


def _open(project, task: str = "1") -> subprocess.CompletedProcess:
    return project.ck("arbiter", "open", project.spec, "--task", task, "--into", project.into, "--json", stdin=_claim(task))


def _plan(*bullets: str) -> str:
    return "# Plan: Demo\n\n## Principles\n\n" + "\n".join(bullets) + "\n"


# --------------------------------------------------------------------------- TC-1, TC-3, TC-140


def test_tc1_init_creates_the_file_with_all_four_defaults(tmp_path):
    project = arbiter_project(tmp_path, principles=None)
    path = project.root / "specs" / "PRINCIPLES.md"
    assert not path.exists()

    out = _json(project.ck("principles", "init", "--json"))

    assert out["created"] is True
    assert Path(out["path"]).resolve() == path.resolve()
    text = path.read_text()
    for slug in DEFAULTS:
        assert f"**{slug}:**" in text
    lowered = text.lower()
    assert "requirements > design > test plan > task wiring" in lowered
    assert "security" in lowered


def test_tc3_init_leaves_an_edited_file_untouched(tmp_path):
    project = arbiter_project(tmp_path, principles=None)
    path = project.root / "specs" / "PRINCIPLES.md"
    edited = b"# Project Principles\n\n- **mine:** my own edited rule.\n"
    path.write_bytes(edited)
    os.utime(path, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    before = path.stat().st_mtime_ns

    out = _json(project.ck("principles", "init", "--json"))

    assert out["created"] is False
    assert path.read_bytes() == edited
    assert path.stat().st_mtime_ns == before


def test_tc140_init_commit_commits_the_file_alone_and_only_when_it_created_it(tmp_path):
    project = arbiter_project(tmp_path, principles=None)
    target = project.target
    path = target / "specs" / "PRINCIPLES.md"
    assert not path.exists()
    before = _git(target, "rev-parse", "HEAD").strip()
    branch = _git(target, "rev-parse", "--abbrev-ref", "HEAD").strip()
    assert branch == project.into

    out = _json(project.ck("principles", "init", "--commit", "--json", cwd=target))

    assert out["created"] is True
    assert out["commit"]
    head = _git(target, "rev-parse", "HEAD").strip()
    assert head != before and head.startswith(out["commit"][:7])
    assert _git(target, "rev-parse", "HEAD~1").strip() == before
    assert _git(target, "show", "--name-only", "--format=", "HEAD").split() == ["specs/PRINCIPLES.md"]
    assert _git(target, "rev-parse", "--abbrev-ref", "HEAD").strip() == branch
    assert _git(target, "status", "--porcelain").strip() == ""

    again = _json(project.ck("principles", "init", "--commit", "--json", cwd=target))

    assert again["created"] is False
    assert again["commit"] is None
    assert _git(target, "rev-parse", "HEAD").strip() == head


# --------------------------------------------------------------------------- TC-8


def _principle_texts(opened: dict) -> list:
    return [p["text"] for p in opened["principles"]]


def test_tc8_override_with_a_reason_takes_precedence_and_one_without_does_not(tmp_path):
    reasoned = arbiter_project(
        tmp_path / "a",
        plan=_plan("- **Overrides remove-first:** keep the legacy path, REASONED-OVERRIDE. **Reason:** a migration needs it."))
    got = _json(_open(reasoned))
    assert got["action"] == "rule", got
    ids = [p["id"] for p in got["principles"]]
    assert "project:remove-first" not in ids
    plan_layer = [p for p in got["principles"] if p["layer"] == "plan"]
    assert any("REASONED-OVERRIDE" in p["text"] for p in plan_layer)
    assert got["planLayer"] == "present"

    unreasoned = arbiter_project(
        tmp_path / "b", plan=_plan("- **Overrides remove-first:** keep the legacy path, UNREASONED-OVERRIDE."))
    got = _json(_open(unreasoned))
    assert got["action"] == "rule", got
    ids = [p["id"] for p in got["principles"]]
    assert "project:remove-first" in ids
    project_layer = [p for p in got["principles"] if p["id"] == "project:remove-first"]
    assert project_layer[0]["layer"] == "project"
    assert "remove before you optimize" in project_layer[0]["text"]


# --------------------------------------------------------------------------- TC-10


def test_tc10_a_plan_grant_over_requirements_changes_nothing(tmp_path):
    plan = _plan("- **Arbiter may change requirements:** the arbiter may add or change requirements without asking me.")
    project = arbiter_project(tmp_path, plan=plan)
    requirements = project.spec_dir / "requirements.md"
    before = requirements.read_bytes()

    opened = _json(_open(project))
    assert opened["action"] == "rule", opened
    ruling = {
        "claim": opened["key"], "status": "ruled", "reasoning": "the criterion is missing a case",
        "outcome": "requirements", "principle": "project:test-intent",
        "requirement": {"kind": "add", "wording": "1.2 WHEN x, THE SYSTEM SHALL y.", "reason": "gap"},
    }
    settled = _json(project.ck("arbiter", "settle", project.spec, opened["key"], "--tier", opened["tier"] or "normal",
                               "--json", stdin=json.dumps(ruling)))

    assert settled["route"] == "needs_input"
    assert requirements.read_bytes() == before


# --------------------------------------------------------------------------- TC-153


def _worktree_count(project) -> int:
    return len(_git(project.root, "worktree", "list", "--porcelain").split("worktree ")) - 1


def _break_missing(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()


def _break_directory(path: Path) -> None:
    _break_missing(path)
    path.mkdir()


def _break_symlink(path: Path) -> None:
    _break_missing(path)
    path.symlink_to(path.parent / "no-such-target.md")


def _break_denied(path: Path) -> None:
    path.chmod(0o000)


def _break_binary(path: Path) -> None:
    path.write_bytes(b"# Principles\n\n- **x:** \xff\xfe\x80 not utf-8\n")


@pytest.mark.parametrize("breaker", [_break_missing, _break_directory, _break_symlink, _break_denied, _break_binary],
                         ids=["missing", "directory", "broken-symlink", "mode-000", "non-utf8"])
def test_tc153_an_unusable_principles_file_means_ask_and_no_worktree(tmp_path, breaker):
    project = arbiter_project(tmp_path)
    for checkout in (project.target, project.root):
        breaker(checkout / "specs" / "PRINCIPLES.md")
    before = _worktree_count(project)
    try:
        proc = _open(project)
        got = _json(proc)
    finally:
        for checkout in (project.target, project.root):
            p = checkout / "specs" / "PRINCIPLES.md"
            if p.exists() and not p.is_symlink():
                p.chmod(0o644)

    assert got["action"] == "ask", got
    assert got["reason"] == "principles-missing"
    assert "PRINCIPLES.md" in (got["missing"] or "")
    assert got["worktree"] is None and got["branch"] is None
    assert _worktree_count(project) == before
    assert not list((project.root / ".ck" / "worktrees").glob("*arbiter*"))


@pytest.mark.parametrize("text", ["", "# Project Principles\n\n- **only:** one rule.\n"], ids=["empty", "readable"])
def test_tc153_a_readable_or_empty_file_may_rule(tmp_path, text):
    project = arbiter_project(tmp_path, principles=text)
    got = _json(_open(project))
    assert got["missing"] is None
    assert got["reason"] != "principles-missing"
    assert got["action"] == "rule", got
