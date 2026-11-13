"""Self-checks of tests/arbiter_helpers.py against today's `ck` and git (run-principles Task 1)."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings

import arbiter_helpers as ah

FIXTURE = Path(__file__).parent / "fixtures" / "hook_inputs" / "arbiter-agent-pretooluse.json"
SETTINGS = settings(max_examples=300, derandomize=True, deadline=None, database=None,
                    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def project(tmp_path):
    return ah.arbiter_project(tmp_path)


def arbiter_cli_exists(project) -> bool:
    return project.ck("arbiter", "--help").returncode == 0


# ---- the project fixture


def test_arbiter_project_is_a_real_repo_with_an_integration_worktree(project):
    assert git(project.root, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(project.target, "rev-parse", "--abbrev-ref", "HEAD") == "integrate/demo"
    assert project.target == project.root / ".ck" / "worktrees" / "demo-integration"
    assert str(project.target) in git(project.root, "worktree", "list", "--porcelain")
    for rel in ("specs/PRINCIPLES.md", "specs/ck.json", "specs/demo/PLAN.md", "specs/demo/requirements.md",
                "specs/demo/design.md", "specs/demo/test-plan.md", "specs/demo/tasks.md"):
        assert (project.root / rel).is_file(), rel
        assert (project.target / rel).is_file(), rel
    json.loads((project.root / "specs/ck.json").read_text())


def test_arbiter_project_run_args_and_exec_ck(project):
    assert project.run_args == {"root": str(project.root), "into": "integrate/demo", "targetDir": str(project.target)}
    assert project.exec_ck == {"cmd": [sys.executable, "-m", "claude_kiro.cli"], "cwd": str(project.root)}
    ran = subprocess.run([*project.exec_ck["cmd"], "lint", "demo", "--json"], cwd=project.exec_ck["cwd"], capture_output=True, text=True)
    assert ran.returncode in (0, 1), ran.stdout + ran.stderr
    json.loads(ran.stdout)


def test_arbiter_project_parameters_replace_and_omit_files(tmp_path):
    p = ah.arbiter_project(tmp_path, tasks=ah.DEFAULT_TASKS.replace("Greeting tests", "Renamed"), principles=None,
                           plan="# Plan\n", requirements="# R\n")
    assert "Renamed" in (p.spec_dir / "tasks.md").read_text()
    assert not (p.root / "specs/PRINCIPLES.md").exists()
    assert (p.spec_dir / "PLAN.md").read_text() == "# Plan\n"
    assert (p.spec_dir / "requirements.md").read_text() == "# R\n"


# ---- the arbiter caller


def test_arbiter_caller_matches_the_fixture_keys(project):
    caller = ah.arbiter_caller(project)
    fixture = json.loads(FIXTURE.read_text())
    assert fixture["agent_type"] in ah.ARBITER_AGENT_TYPES
    assert "_derivedFrom" not in fixture, "the fixture is the real capture (Task 91), not the derived placeholder"
    assert caller["agent_type"] == fixture["agent_type"]
    assert isinstance(caller["agent_id"], str) and caller["agent_id"]
    assert set(caller) <= {"agent_id", "agent_type", "cwd"}


def test_arbiter_caller_run_phase_uses_an_arbiter_worktree(project):
    caller = ah.arbiter_caller(project)
    assert Path(caller["cwd"]).name == "demo-task-arbiter1"
    assert git(caller["cwd"], "rev-parse", "--abbrev-ref", "HEAD") != "main"


def test_arbiter_caller_run_phase_takes_a_given_checkout(project):
    assert ah.arbiter_caller(project, checkout=project.target)["cwd"] == str(project.target)


def test_arbiter_caller_create_phase_needs_the_lane_command(project):
    if arbiter_cli_exists(project):
        assert ah.arbiter_caller(project, phase="create")["agent_type"] in ah.ARBITER_AGENT_TYPES
    else:
        with pytest.raises(AssertionError, match="ck arbiter lane open"):
            ah.arbiter_caller(project, phase="create")


def test_arbiter_caller_rejects_an_unknown_phase(project):
    with pytest.raises(ValueError):
        ah.arbiter_caller(project, phase="nope")


# ---- the record reader


ENTRY = """# Rulings: demo

## Ruling 1: Task 19 (test-conflict)

- **At:** 2026-11-09T17:02:57Z
- **Trigger:** test-conflict
- **Claim:** `19|test:tests/test_x.py::test_a` - the claim text
- **Ruling:** spec_edit - the reasoning
- **Landed:** no: conflict

## Ruling 2: Task 20 (guard-stop)

- **Trigger:** guard-stop
- **Commit:** none
"""


def test_decisions_entries_is_empty_without_a_file(project):
    assert ah.decisions_entries(project) == []


def test_decisions_entries_parses_each_entry_and_field(project):
    (project.target / "specs/demo/decisions.md").write_text(ENTRY)
    entries = ah.decisions_entries(project)
    assert [e["number"] for e in entries] == [1, 2]
    assert entries[0]["title"] == "Task 19 (test-conflict)"
    assert entries[0]["Claim"] == "`19|test:tests/test_x.py::test_a` - the claim text"
    assert entries[0]["Landed"] == "no: conflict"
    assert entries[1]["Commit"] == "none"


def test_decisions_entries_reads_the_branch_tip_through_git(project):
    (project.target / "specs/demo/decisions.md").write_text(ENTRY)
    assert ah.decisions_entries(project, where="branch") == []
    git(project.target, "add", "specs/demo/decisions.md")
    git(project.target, "commit", "-qm", "record")
    assert len(ah.decisions_entries(project, where="branch")) == 2


# ---- the interface map


def test_interface_map_keeps_the_harness_contract_names():
    for name in ("arbiter.claim_id", "arbiter.decide_call", "arbiter.validate_ruling", "arbiter.route_ruling",
                 "arbiter.spec_edit_refusals", "arbiter.effective_principles", "guard_policy.arbiter_may_edit",
                 "lint.justification_findings", "lint.verify_scope"):
        assert name in ah.INTERFACE_MAP
    assert set(ah.CLI_MAP) == {"ck arbiter decide", "ck arbiter route", "ck arbiter record", "ck arbiter land"}
    assert ah.INTERFACE_MAP["arbiter.claim_id"] == ("claude_kiro.arbiter_policy", "claim_key")


def test_seam_reports_the_missing_module_not_a_broken_helper():
    try:
        got = ah.seam("arbiter.claim_id")
    except ModuleNotFoundError as err:
        assert "arbiter_policy" in str(err)
    else:
        assert callable(got)


# ---- the strategies


@SETTINGS
@given(ah.claims())
def test_claims_cover_the_claim_fields(c):
    assert c["trigger"] in ah.TRIGGERS and c["task"] and c["text"]


def test_claims_generate_every_trigger():
    seen = set()

    @SETTINGS
    @given(ah.claims())
    def run(c):
        seen.add(c["trigger"])

    run()
    assert seen == set(ah.TRIGGERS)


def test_rulings_generate_every_outcome_and_malformed_ones():
    seen, principles = [], ["project:test-intent", {"id": "plan:x"}]

    @SETTINGS
    @given(ah.rulings(principles))
    def run(r):
        seen.append(r)

    run()
    outcomes = {r["outcome"] for r in seen}
    assert set(ah.OUTCOMES) <= outcomes
    assert any(o not in ah.OUTCOMES for o in outcomes)
    assert {r["status"] for r in seen} == {"ruled", "needs_stronger_look"}
    assert any(r["principle"] == "plan:x" for r in seen)


def test_histories_generate_every_status():
    seen = set()

    @SETTINGS
    @given(ah.histories())
    def run(h):
        seen.update(row["status"] for row in h)

    run()
    assert seen == {"open", "failed", "exhausted", "ruled"}


def test_spec_paths_generate_in_spec_and_near_miss_forms():
    seen = []

    @SETTINGS
    @given(ah.spec_paths("demo"))
    def run(p):
        seen.append(p)

    run()
    assert any(p == "specs/demo/tasks.md" for p in seen)
    assert any(p.startswith("./") for p in seen)
    assert any("/../" in p for p in seen)
    assert any(p.startswith("/") for p in seen)
    assert any("demo-other" in p or "demox" in p for p in seen)
    assert any(p.startswith("SPECS/") for p in seen)


def test_justification_lines_generate_counting_and_empty_forms():
    seen = []

    @SETTINGS
    @given(ah.justification_lines())
    def run(x):
        seen.append(x)

    run()
    assert all("Without it" in line for line, _ in seen)
    assert {counts for _, counts in seen} == {True, False}
    for line, counts in seen:
        content = re.sub(r"^\*\*Without it:?\*\*:?", "", line)
        if not counts:
            assert not re.sub(r"[\s*_`>~<!\-]|<!--|-->", "", content)


def test_requirements_docs_generate_each_story_form():
    seen = []

    @SETTINGS
    @given(ah.requirements_docs())
    def run(x):
        seen.append(x)

    run()
    assert any(not m for _, m in seen) and any(m for _, m in seen)
    assert any("```" in t for t, _ in seen) and any("## Notes" in t for t, _ in seen)
    assert any("### Story" not in t for t, _ in seen)
    for text, missing in seen:
        assert all(f"### Story {n}:" in text for n in missing)


def test_arbiter_failures_generate_every_failure_form():
    seen = []

    @SETTINGS
    @given(ah.arbiter_failures())
    def run(f):
        seen.append(f)

    run()
    assert "throw" in seen and None in seen and {} in seen and [] in seen and "a string" in seen
    assert any(isinstance(f, int) for f in seen)
    assert any(isinstance(f, dict) and f for f in seen)
