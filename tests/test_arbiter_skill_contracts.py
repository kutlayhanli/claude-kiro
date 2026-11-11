"""Contract cases over the shipped implement, create and review skill text (and the manual-path
resource): TC-139, TC-147, TC-159 and the report/launch rules of Stories 4, 10 and 12.

Reads only the real `plugin/skills/*/SKILL.md` and `plugin/resources/without-workflow-tool.md`,
never the vendored `.claude/commands/spec/*.md`.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMPLEMENT = ROOT / "plugin" / "skills" / "implement" / "SKILL.md"
CREATE = ROOT / "plugin" / "skills" / "create" / "SKILL.md"
REVIEW = ROOT / "plugin" / "skills" / "review" / "SKILL.md"
MANUAL = ROOT / "plugin" / "resources" / "without-workflow-tool.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def near(text: str, first: str, second: str, window: int = 400) -> bool:
    """`first` and `second` (regexes) both occur within `window` characters, in either order."""
    return bool(
        re.search(f"(?:{first}).{{0,{window}}}(?:{second})", text, re.S | re.I)
        or re.search(f"(?:{second}).{{0,{window}}}(?:{first})", text, re.S | re.I)
    )


# ---- implement skill -------------------------------------------------------------------------


def test_implement_skill_passes_the_four_new_args():
    text = read(IMPLEMENT)
    for name in ("arbiterAgent", "auditorAgent", "verifiedBy", "agents.arbiter"):
        assert name in text, f"implement skill never mentions {name}"
    assert near(text, "arbiterAgent", "spec:arbiter"), "arbiterAgent has no agent type named"
    assert near(text, "auditorAgent", "spec:auditor"), "auditorAgent has no agent type named"
    assert near(text, "agents\\.arbiter", "ck agents"), "agents.arbiter is not tied to ck agents"


def test_implement_skill_verify_scope_launches_whole_spec_and_refuses_named_single_task():
    text = read(IMPLEMENT)
    assert "verify-scope" in text, "implement skill never mentions a verify-scope finding"
    assert near(text, "verify-scope", "whole-spec", 600), "no launch rule for a whole-spec run"
    assert near(text, "verify-scope", "refus", 600), "no refusal for a single-task run"
    assert near(text, "refus", "single-task", 300)
    assert near(text, "verify-scope", "verify-file", 800), "finding is not quoted as a verify-file entry is"


def test_implement_skill_reports_this_runs_rulings_and_proposed_cuts():
    text = read(IMPLEMENT)
    assert re.search(r"rulings", text, re.I), "report does not list rulings"
    assert near(text, "rulings", "this run", 300), "rulings are not scoped to this run"
    assert "proposedCuts" in text, "report does not list proposed cuts"


def test_implement_skill_tc139_principles_init_on_target_checkout_with_commit():
    text = read(IMPLEMENT)
    assert re.search(r"ck -C <targetDir> principles init[^\n`]*--commit", text), (
        "implement skill does not run `ck -C <targetDir> principles init --commit`"
    )
    assert near(text, "principles init", "created the file|tell me", 500), (
        "implement skill does not tell me when it created the file"
    )


def test_implement_skill_tc147_audit_before_first_attempt_and_claims_are_needs_input():
    text = read(IMPLEMENT)
    assert near(text, "audit", "before the first attempt", 300), "no read-only audit before the first attempt"
    assert "spec:auditor" in text and "ck-auditor" in text
    assert near(text, "single-task", "needs-input", 600), "single-task claims are not needs-input entries"
    assert re.search(r"no guard ladder", text, re.I), "does not say there is no guard ladder"
    assert re.search(r"no confirmation agent", text, re.I), "does not say there is no confirmation agent"
    assert near(text, "audit problem", "needs-input", 300), "an audit problem is not a needs-input entry"


def test_implement_skill_tc147_manual_path_resource_says_the_same():
    text = read(MANUAL)
    assert near(text, "audit", "before the first attempt", 300), "manual path has no audit before the first attempt"
    assert near(text, "needs-input", "claim", 400), "manual path claims are not needs-input entries"
    assert re.search(r"no guard ladder", text, re.I)
    assert re.search(r"no confirmation agent", text, re.I)
    assert near(text, "audit problem", "needs-input", 300)


# ---- create skill ----------------------------------------------------------------------------


def test_create_skill_reports_proposed_cuts_with_story_weakness_and_savings():
    text = read(CREATE)
    assert "review.proposedCuts" in text, "create skill does not report review.proposedCuts"
    assert near(text, "proposedCuts", "story", 500)
    assert near(text, "proposedCuts", "weakness", 500)
    assert near(text, "proposedCuts", "saves", 500), "does not say what the cut saves"


def test_create_skill_approved_cut_before_any_task_started_goes_through_apply_cuts_and_lint():
    text = read(CREATE)
    assert "apply-cuts" in text, "create skill never runs apply-cuts"
    assert near(text, "apply-cuts", "no task (has )?started|not started|before .{0,20}task", 500)
    assert near(text, "apply-cuts", "ck lint", 600), "does not run ck lint after the cut"
    assert near(text, "ck lint", "new .{0,20}fail", 400), "does not report a new ck lint failure"


def test_create_skill_approved_cut_after_a_task_started_says_requirements_frozen():
    text = read(CREATE)
    assert near(text, "frozen", "task", 400), "does not tell me the requirements are frozen"
    assert near(text, "frozen", "mine|my change|my to make", 300), "does not say the change is mine"


def test_create_skill_tc139_principles_init_and_tells_me():
    text = read(CREATE)
    assert "ck principles init" in text
    assert near(text, "principles init", "created the file|tell me", 500)


def test_create_skill_tc159_runs_ck_agents_json_and_passes_arbiter_args():
    text = read(CREATE)
    assert "ck agents --json" in text, "create skill does not run `ck agents --json`"
    for name in ("arbiterAgent", "runnerAgent", "agents.arbiter"):
        assert name in text, f"create skill does not pass {name}"


# ---- review skill ----------------------------------------------------------------------------


def test_review_skill_reports_proposed_cuts_with_story_weakness_and_savings():
    text = read(REVIEW)
    assert "review.proposedCuts" in text, "review skill does not report review.proposedCuts"
    assert near(text, "proposedCuts", "story", 500)
    assert near(text, "proposedCuts", "weakness", 500)
    assert near(text, "proposedCuts", "saves", 500), "does not say what the cut saves"


def test_review_skill_tc139_principles_init_and_tells_me():
    text = read(REVIEW)
    assert "ck principles init" in text
    assert near(text, "principles init", "created the file|tell me", 500)


def test_review_skill_tc159_runs_ck_agents_json_and_passes_arbiter_args():
    text = read(REVIEW)
    assert "ck agents --json" in text, "review skill does not run `ck agents --json`"
    for name in ("arbiterAgent", "runnerAgent", "agents.arbiter"):
        assert name in text, f"review skill does not pass {name}"
