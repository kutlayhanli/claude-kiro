"""Red team of the SHALL NOT clauses of Story 1 (1.4, 1.6, 1.9, 1.11), written from the requirements only.

Boundaries: the pure seams `plan_may_be_decided` and `effective_principles`, and the real `ck principles check`,
`ck arbiter open` and `ck lint` CLIs over real files. Project modules are imported inside test bodies.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from arbiter_helpers import arbiter_project

# --------------------------------------------------------------------------- shared helpers

BULLET = "- **Nothing to add:** the project defaults apply.\n"
CLAIM = {"task": "2", "trigger": "test-conflict", "text": "the test contradicts the requirement",
         "file": "tests/test_greet.py", "test": "test_greet", "criterion": "1.1"}
TASK = {"risk": "normal", "complexity": "Low"}

STARTER = """# Project Principles

- **priority-order:** requirements > design > test plan > task wiring.
- **test-intent:** a test keeps its intent; only selection and setup may move.
- **fail-closed:** fail closed on security.
- **remove-first:** remove before you optimize.
"""

# (id, plan text, may the plan be Decided)
PLAN_CASES = [
    ("heading-only-in-backtick-fence", "# Plan\n\n```\n## Principles\n\n" + BULLET + "```\n", False),
    ("heading-only-in-tilde-fence", "# Plan\n\n~~~\n## Principles\n" + BULLET + "~~~\n", False),
    ("heading-in-indented-fence", "# Plan\n\n   ```md\n   ## Principles\n   " + BULLET + "   ```\n", False),
    ("heading-in-unclosed-fence", "# Plan\n\n```\n## Principles\n" + BULLET, False),
    ("heading-in-longer-fence", "# Plan\n\n````\n```\n## Principles\n" + BULLET + "````\n", False),
    ("real-heading-bullet-only-in-fence", "# Plan\n\n## Principles\n\n```\n" + BULLET + "```\n", False),
    ("comment-only", "# Plan\n\n## Principles\n\n<!-- list your principles here -->\n", False),
    ("multiline-comment-only", "# Plan\n\n## Principles\n\n<!--\nlist them\n-->\n", False),
    ("bullet-inside-comment", "# Plan\n\n## Principles\n\n<!-- " + BULLET.strip() + " -->\n", False),
    ("multiline-comment-with-bullet", "# Plan\n\n## Principles\n<!--\n" + BULLET + "-->\n", False),
    ("whitespace-only", "# Plan\n\n## Principles\n   \n\t\n\n", False),
    ("heading-last-line-no-newline", "# Plan\n\n## Principles", False),
    ("next-section-holds-the-bullet", "# Plan\n\n## Principles\n\n## Scope\n\n" + BULLET, False),
    ("next-section-after-comment", "# Plan\n\n## Principles\n<!-- x -->\n## Scope\n\n- **In scope:** a\n", False),
    ("prose-only", "# Plan\n\n## Principles\n\nNothing really.\n", False),
    ("no-section", "# Plan\n\n## Scope\n\n- **In scope:** a\n", False),
    ("empty-plan", "", False),
    ("bullet-before-the-heading", "# Plan\n\n" + BULLET + "\n## Principles\n\n", False),
    ("fenced-heading-then-real-empty-heading", "# Plan\n\n```\n## Principles\n" + BULLET + "```\n\n## Principles\n", False),
    ("real-bullet", "# Plan\n\n## Principles\n\n" + BULLET, True),
    ("real-bullet-crlf", "# Plan\r\n\r\n## Principles\r\n\r\n" + BULLET.replace("\n", "\r\n"), True),
    ("real-bullet-trailing-spaces-heading", "# Plan\n\n## Principles   \n\n" + BULLET, True),
    ("real-bullet-after-comment", "# Plan\n\n## Principles\n\n<!-- hint -->\n" + BULLET, True),
    ("fenced-heading-then-real-section", "# Plan\n\n```\n## Principles\n```\n\n## Principles\n\n" + BULLET, True),
    ("bullet-then-next-section", "# Plan\n\n## Principles\n\n" + BULLET + "\n## Scope\n", True),
]


def _write_plan(project, text: str) -> None:
    for base in (project.root, project.target):
        (base / "specs" / "demo" / "PLAN.md").write_text(text, newline="")


def _check(project, text: str):
    _write_plan(project, text)
    done = project.ck("principles", "check", "demo", "--json")
    try:
        return done, json.loads(done.stdout)
    except ValueError:
        return done, None


# --------------------------------------------------------------------------- 1.4 the seam

@pytest.mark.parametrize("text,expected", [(c[1], c[2]) for c in PLAN_CASES], ids=[c[0] for c in PLAN_CASES])
def test_rt_1_4_plan_may_be_decided_seam(text, expected):
    from claude_kiro.principles import plan_may_be_decided

    ok, reason = plan_may_be_decided(text)
    assert ok is expected, f"plan_may_be_decided({text!r}) -> {(ok, reason)!r}"
    assert isinstance(reason, str)
    if not ok:
        assert reason.strip(), "a refusal says why"


def test_rt_1_4_plan_section_is_none_for_fenced_or_comment_only_headings():
    from claude_kiro.principles import plan_section

    assert plan_section("# P\n\n```\n## Principles\n" + BULLET + "```\n") is None
    assert plan_section("# P\n\n## Principles\n\n<!-- hint -->\n") is None
    assert plan_section(None) is None


# --------------------------------------------------------------------------- 1.4 through the CLI

@pytest.mark.parametrize("text,expected", [(c[1], c[2]) for c in PLAN_CASES], ids=[c[0] for c in PLAN_CASES])
def test_rt_1_4_ck_principles_check(tmp_path, text, expected):
    project = arbiter_project(tmp_path)
    done, data = _check(project, text)
    assert data is not None, f"no JSON from ck principles check: {done.stdout!r} {done.stderr!r}"
    assert data["ok"] is expected
    assert (done.returncode == 0) is expected, "exit is 1 exactly when the plan may not be Decided"
    assert isinstance(data["reason"], str)


def test_rt_1_4_check_never_edits_the_plan(tmp_path):
    project = arbiter_project(tmp_path)
    text = "# Plan\n\n**Status:** Draft\n\n## Principles\n\n<!-- hint -->\n"
    _write_plan(project, text)
    project.ck("principles", "check", "demo", "--json")
    for base in (project.root, project.target):
        assert (base / "specs" / "demo" / "PLAN.md").read_text() == text


def test_rt_1_4_check_on_an_unknown_spec_does_not_say_ok(tmp_path):
    project = arbiter_project(tmp_path)
    done = project.ck("principles", "check", "no-such-spec", "--json")
    assert done.returncode != 0
    if done.stdout.strip().startswith("{"):
        assert json.loads(done.stdout).get("ok") is not True


# --------------------------------------------------------------------------- 1.6 reasons that are only markup

DEFAULT_OVERRIDE = "- **Overrides fail-closed:** a read-only probe may fail open."
OVERRIDE_TEXT = "a read-only probe may fail open"

REASONLESS = [
    ("no-reason", DEFAULT_OVERRIDE),
    ("label-alone", DEFAULT_OVERRIDE + " **Reason:**"),
    ("label-then-spaces", DEFAULT_OVERRIDE + " **Reason:**    "),
    ("label-then-bold-markers", DEFAULT_OVERRIDE + " **Reason:** ****"),
    ("label-then-stars", DEFAULT_OVERRIDE + " **Reason:** **"),
    ("label-then-underscores", DEFAULT_OVERRIDE + " **Reason:** _ _"),
    ("label-then-double-underscores", DEFAULT_OVERRIDE + " **Reason:** __"),
    ("label-then-comment", DEFAULT_OVERRIDE + " **Reason:** <!-- why -->"),
    ("label-then-backticks", DEFAULT_OVERRIDE + " **Reason:** ``"),
    ("label-then-empty-link", DEFAULT_OVERRIDE + " **Reason:** []()"),
    ("label-then-tags", DEFAULT_OVERRIDE + " **Reason:** <b></b>"),
    ("label-then-nbsp", DEFAULT_OVERRIDE + " **Reason:**  ​"),
    ("label-then-empty-continuation", DEFAULT_OVERRIDE + " **Reason:**\n  \n"),
    ("label-then-markup-continuation", DEFAULT_OVERRIDE + " **Reason:**\n  **\n"),
    ("not-bold-label", DEFAULT_OVERRIDE + " Reason: it touches no data."),
    ("why-label", DEFAULT_OVERRIDE + " Why: it touches no data."),
    ("plain-prose", DEFAULT_OVERRIDE + " because it touches no data."),
    ("reason-only-in-a-fence", DEFAULT_OVERRIDE + "\n\n```\n**Reason:** it touches no data.\n```"),
    ("reason-on-another-bullet", DEFAULT_OVERRIDE + "\n- **Reason:** it touches no data."),
]


def _effective(plan_body: str):
    from claude_kiro.principles import effective_principles, parse_principles

    project = parse_principles(STARTER, "project")
    plan = parse_principles("## Principles\n\n" + plan_body + "\n", "plan")
    return effective_principles(project, plan)


@pytest.mark.parametrize("body", [c[1] for c in REASONLESS], ids=[c[0] for c in REASONLESS])
def test_rt_1_6_reasonless_override_never_displaces_the_default(body):
    eff = _effective(body)
    ids = [p.id for p in eff.citable]
    assert "project:fail-closed" in ids, f"the default was displaced: {ids}"
    assert not any(OVERRIDE_TEXT in p.text for p in eff.citable if p.layer == "plan"), \
        "a reasonless override is citable"


@pytest.mark.parametrize("body", [c[1] for c in REASONLESS], ids=[c[0] for c in REASONLESS])
def test_rt_1_6_reasonless_override_over_the_arbiter_open_cli(tmp_path, body):
    # The reason-on-another-bullet and fence forms aside, the CLI must agree with the seam.
    project = arbiter_project(tmp_path, principles=STARTER)
    _write_plan(project, "# Plan\n\n## Principles\n\n" + body + "\n")
    done = project.ck("arbiter", "open", "demo", "--task", "2", "--into", project.into, "--json",
                      stdin=json.dumps({"claim": CLAIM, "task": TASK}))
    data = json.loads(done.stdout)
    assert data["action"] == "rule", data
    ids = [p["id"] for p in data["principles"]]
    assert "project:fail-closed" in ids
    assert not any(OVERRIDE_TEXT in p["text"] for p in data["principles"] if p["layer"] == "plan")


def test_rt_1_6_control_a_reasoned_override_does_displace():
    eff = _effective(DEFAULT_OVERRIDE + " **Reason:** it touches no data.")
    assert "project:fail-closed" not in [p.id for p in eff.citable]
    assert any(OVERRIDE_TEXT in p.text for p in eff.citable if p.layer == "plan")


def test_rt_1_6_reasonless_override_lands_in_ignored_not_citable():
    eff = _effective(DEFAULT_OVERRIDE + " **Reason:** **")
    assert any(OVERRIDE_TEXT in p.text for p in eff.ignored)
    assert not any(OVERRIDE_TEXT in p.text for p in eff.citable)


def test_rt_1_6_one_reasoned_and_one_reasonless_override_do_not_leak_into_each_other():
    body = ("- **Overrides fail-closed:** a read-only probe may fail open. **Reason:** it touches no data.\n"
            "- **Overrides remove-first:** optimize first. **Reason:** **")
    eff = _effective(body)
    ids = [p.id for p in eff.citable]
    assert "project:fail-closed" not in ids
    assert "project:remove-first" in ids
    assert not any("optimize first" in p.text for p in eff.citable)


def test_rt_1_6_the_projects_own_text_fed_back_as_a_plan_never_overrides():
    # The project file's own bullets, put in the plan layer without a reason, displace nothing.
    from claude_kiro.principles import effective_principles, parse_principles

    project = parse_principles(STARTER, "project")
    plan = parse_principles("## Principles\n\n" + "\n".join(
        f"- **Overrides {s}:** replaced." for s in ("priority-order", "test-intent", "fail-closed", "remove-first")) + "\n",
        "plan")
    eff = effective_principles(project, plan)
    assert {p.id for p in project} <= {p.id for p in eff.citable}


# --------------------------------------------------------------------------- 1.9 the project file unusable

def _usable_open(project):
    return project.ck("arbiter", "open", "demo", "--task", "2", "--into", project.into, "--json",
                      stdin=json.dumps({"claim": CLAIM, "task": TASK}))


def _worktree_list(project) -> str:
    return subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=project.root, check=True,
                          capture_output=True, text=True).stdout


def _principles_paths(project):
    return [project.root / "specs" / "PRINCIPLES.md", project.target / "specs" / "PRINCIPLES.md"]


def _break(project, how: str) -> None:
    for path in _principles_paths(project):
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
        if how == "missing":
            continue
        if how == "directory":
            path.mkdir()
        elif how == "broken-symlink":
            path.symlink_to(path.parent / "nowhere.md")
        elif how == "symlink-loop":
            path.symlink_to(path.name)
        elif how == "fifo-free-dir-with-file":
            path.mkdir()
            (path / "PRINCIPLES.md").write_text(STARTER)
        elif how == "mode-000":
            path.write_text(STARTER)
            path.chmod(0)
        elif how.startswith("bytes:"):
            path.write_bytes(bytes.fromhex(how.split(":", 1)[1]))


BREAKS = ["missing", "directory", "broken-symlink", "symlink-loop", "fifo-free-dir-with-file",
          "bytes:fffe", "bytes:c3", "bytes:c0af", "bytes:eda080", "bytes:" + (b"- **a:** ok\n" + b"\xe9" + b" tail\n").hex(),
          "bytes:" + "# Principles\n".encode("utf-16").hex()]
if os.geteuid() != 0:
    BREAKS.append("mode-000")


def _assert_asks_and_names_file(project, before_worktrees: str):
    done = _usable_open(project)
    try:
        data = json.loads(done.stdout)
    except ValueError:
        pytest.fail(f"ck arbiter open printed no JSON: {done.stdout!r} {done.stderr!r}")
    assert data["action"] == "ask", data
    assert data.get("worktree") in (None, ""), "no arbiter worktree for an unusable principles file"
    assert "PRINCIPLES.md" in (data.get("missing") or ""), data
    assert _worktree_list(project) == before_worktrees, "an arbiter worktree was created"
    branches = subprocess.run(["git", "branch", "--list", "*arbiter*"], cwd=project.root, capture_output=True,
                              text=True).stdout
    assert branches.strip() == "", branches


@pytest.mark.parametrize("how", BREAKS)
def test_rt_1_9_an_unusable_file_is_never_given_to_the_arbiter(tmp_path, how):
    project = arbiter_project(tmp_path)
    before = _worktree_list(project)
    _break(project, how)
    try:
        _assert_asks_and_names_file(project, before)
    finally:
        for path in _principles_paths(project):
            if path.exists() and not path.is_symlink() and path.is_file():
                path.chmod(0o644)


def test_rt_1_9_file_replaced_after_a_ruling_call_is_judged_by_the_second_state(tmp_path):
    project = arbiter_project(tmp_path)
    first = json.loads(_usable_open(project).stdout)
    assert first["action"] == "rule", first
    before = _worktree_list(project)
    _break(project, "directory")
    _assert_asks_and_names_file(project, before)


@pytest.mark.parametrize("sequence", [["directory", "missing"], ["bytes:fffe", "directory"],
                                      ["missing", "bytes:c3"], ["broken-symlink", "bytes:fffe"]],
                         ids=lambda s: "->".join(x.split(":")[0] for x in s))
def test_rt_1_9_a_file_that_changes_kind_between_calls_never_rules(tmp_path, sequence):
    project = arbiter_project(tmp_path)
    before = _worktree_list(project)
    for how in sequence:
        _break(project, how)
        _assert_asks_and_names_file(project, before)


def test_rt_1_9_a_file_restored_after_being_lost_may_rule_again(tmp_path):
    project = arbiter_project(tmp_path)
    before = _worktree_list(project)
    _break(project, "bytes:fffe")
    _assert_asks_and_names_file(project, before)
    for path in _principles_paths(project):
        path.unlink()
        path.write_text(STARTER)
    assert json.loads(_usable_open(project).stdout)["action"] == "rule"


def test_rt_1_9_an_empty_but_readable_file_is_not_treated_as_missing(tmp_path):
    project = arbiter_project(tmp_path, principles="")
    data = json.loads(_usable_open(project).stdout)
    assert "PRINCIPLES.md" not in (data.get("missing") or "")


# --------------------------------------------------------------------------- 1.11 a warning never fails lint

def _lint(project, *extra):
    done = project.ck("lint", "demo", "--json", *extra)
    try:
        return done, json.loads(done.stdout)
    except ValueError:
        return done, None


def _principles_warnings(data):
    return [w for w in (data or {}).get("warnings", []) if w.get("kind") == "principles"]


LINT_PLANS = [
    ("no-section", "# Plan: Demo\n\n## Scope\n\n- **In scope:** a\n"),
    ("empty-section", "# Plan: Demo\n\n## Principles\n\n## Scope\n"),
    ("comment-only", "# Plan: Demo\n\n## Principles\n\n<!-- x -->\n"),
    ("heading-in-fence", "# Plan: Demo\n\n```\n## Principles\n" + BULLET + "```\n"),
    ("whitespace-only", "# Plan: Demo\n\n## Principles\n  \n\t\n"),
]


@pytest.mark.parametrize("plan", [c[1] for c in LINT_PLANS], ids=[c[0] for c in LINT_PLANS])
def test_rt_1_11_warning_is_reported_and_exit_is_unchanged(tmp_path, plan):
    project = arbiter_project(tmp_path)
    clean_done, clean = _lint(project)
    assert clean is not None, clean_done.stdout + clean_done.stderr
    assert _principles_warnings(clean) == []
    (project.spec_dir / "PLAN.md").write_text(plan)
    done, data = _lint(project)
    assert data is not None, done.stdout + done.stderr
    warnings = _principles_warnings(data)
    assert len(warnings) == 1, data.get("warnings")
    assert "demo" in warnings[0]["plan"] and "PLAN" in warnings[0]["plan"]
    assert warnings[0]["message"].strip()
    assert done.returncode == clean_done.returncode, "the warning changed lint's exit code"
    assert data.get("exit_nonzero", data.get("exitNonzero")) == clean.get("exit_nonzero", clean.get("exitNonzero"))


def test_rt_1_11_text_mode_exit_is_also_unchanged(tmp_path):
    project = arbiter_project(tmp_path)
    clean = project.ck("lint", "demo")
    (project.spec_dir / "PLAN.md").write_text(LINT_PLANS[1][1])
    warned = project.ck("lint", "demo")
    assert warned.returncode == clean.returncode
    assert "PLAN" in warned.stdout + warned.stderr


def test_rt_1_11_a_plan_with_a_real_bullet_has_no_warning(tmp_path):
    project = arbiter_project(tmp_path)
    (project.spec_dir / "PLAN.md").write_text("# Plan: Demo\n\n## Principles\n\n" + BULLET)
    _, data = _lint(project)
    assert _principles_warnings(data) == []


def test_rt_1_11_no_plan_file_no_warning_and_no_crash(tmp_path):
    project = arbiter_project(tmp_path, plan=None)
    done, data = _lint(project)
    assert data is not None, done.stdout + done.stderr
    assert _principles_warnings(data) == []


def test_rt_1_11_the_warning_does_not_mask_a_real_failure(tmp_path):
    # A story with no justification still fails lint, with or without the plan warning, and both findings show.
    bad = ("# Requirements: Demo\n\n## User Stories\n\n### Story 1: Greeting\n\n**As a** user **I want** a hello "
           "**So that** hi.\n\n**Acceptance Criteria:**\n\n- 1.1 WHEN the app starts, THE SYSTEM SHALL print a greeting.\n")
    project = arbiter_project(tmp_path, requirements=bad, plan="# Plan: Demo\n\n## Principles\n\n" + BULLET)
    base_done, base = _lint(project)
    (project.spec_dir / "PLAN.md").write_text(LINT_PLANS[1][1])
    done, data = _lint(project)
    assert done.returncode == base_done.returncode
    assert len(_principles_warnings(data)) == 1
