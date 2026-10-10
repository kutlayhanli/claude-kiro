"""Pass stamps: `ck gate` records a passing full suite by tree hash, for safe-merge to reuse.

Stamp contract (shared with ~/.claude/skills/safe-merge): <git-common-dir>/ck-verified/<tree>.json is a
list of entries {commands, sha, by, at, duration_s, host, commit}; `sha` is sha256 of the commands joined
with a trailing newline each (what `printf '%s\\n' cmd...` produces).
"""

import hashlib
import json
import subprocess
from pathlib import Path

from claude_kiro.gate import run_gate
from claude_kiro.verified import commands_sha, lookup_stamp, stamp_dir

from conftest import git, write_tasks
from test_gate import implement_subtract, write_subtract_tests

VERIFY = ["python -m pytest -q tests/test_calc.py"]


def finish_spec(project: Path) -> None:
    """Both tasks Done and committed: the gate runs the full `verify` suite on a clean tree."""
    write_subtract_tests(project)
    implement_subtract(project)
    write_tasks(project, t1="Done", t2="Done", a1="x", a2="x")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "done")


def head_tree(project: Path) -> str:
    return git(project, "rev-parse", "HEAD^{tree}").strip()


def stamp_entries(project: Path) -> list:
    path = stamp_dir(project) / f"{head_tree(project)}.json"
    return json.loads(path.read_text()) if path.exists() else []


def test_sha_matches_printf_joined_commands():
    cmds = ["uv run pytest -q", "echo two words"]
    shell = subprocess.run(
        ["bash", "-c", 'printf "%s\\n" "$@" | sha256sum', "_", *cmds], capture_output=True, text=True, check=True
    ).stdout.split()[0]
    assert commands_sha(cmds) == shell == hashlib.sha256("".join(c + "\n" for c in cmds).encode()).hexdigest()


def test_passing_full_gate_on_clean_tree_stamps_the_tree(project):
    finish_spec(project)
    result = run_gate(project, project / "specs/calc")
    assert result.ok, result.report()
    entries = stamp_entries(project)
    assert len(entries) == 1
    e = entries[0]
    assert e["commands"] == VERIFY and e["sha"] == commands_sha(VERIFY)
    assert e["by"] == "ck gate" and e["commit"] == git(project, "rev-parse", "HEAD").strip()
    assert lookup_stamp(project, head_tree(project), [VERIFY]) is not None


def test_regating_the_same_tree_keeps_one_entry_per_command_set(project):
    finish_spec(project)
    assert run_gate(project, project / "specs/calc").ok
    assert run_gate(project, project / "specs/calc").ok
    assert len(stamp_entries(project)) == 1


def test_uncommitted_change_is_not_stamped(project):
    finish_spec(project)
    (project / "calc.py").write_text((project / "calc.py").read_text() + "\n# local edit\n")
    assert run_gate(project, project / "specs/calc").ok
    assert stamp_entries(project) == []


def test_untracked_file_is_not_stamped(project):
    finish_spec(project)
    (project / "scratch.py").write_text("x = 1\n")
    assert run_gate(project, project / "specs/calc").ok
    assert stamp_entries(project) == []


def test_failing_gate_is_not_stamped(project):
    finish_spec(project)
    (project / "calc.py").write_text((project / "calc.py").read_text().replace("return a + b", "return a * b"))
    git(project, "commit", "-qam", "break add")
    assert not run_gate(project, project / "specs/calc").ok
    assert stamp_entries(project) == []


def test_gate_that_skips_the_full_suite_is_not_stamped(project):
    # Test task Done, impl not: test-first mode runs only the suites that should pass, not `verify`.
    write_subtract_tests(project)
    write_tasks(project, t1="Done", a1="x")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "tests first")
    assert run_gate(project, project / "specs/calc", ["1"]).ok
    assert stamp_entries(project) == []


def test_stamp_from_a_linked_worktree_lands_in_the_shared_git_dir(project, tmp_path):
    finish_spec(project)
    wt = tmp_path / "wt"
    git(project, "worktree", "add", "-q", str(wt), "-b", "other")
    assert run_gate(wt, wt / "specs/calc").ok
    assert stamp_dir(wt) == stamp_dir(project) == (project / ".git" / "ck-verified").resolve()
    assert stamp_entries(project)[0]["by"] == "ck gate"


def test_existing_entries_from_safe_merge_are_preserved(project):
    finish_spec(project)
    d = stamp_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    light = ["python -m pytest -q tests/meta"]
    (d / f"{head_tree(project)}.json").write_text(
        json.dumps([{"commands": light, "sha": commands_sha(light), "by": "safe-merge"}])
    )
    assert run_gate(project, project / "specs/calc").ok
    assert [e["by"] for e in stamp_entries(project)] == ["safe-merge", "ck gate"]
