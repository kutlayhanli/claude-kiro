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
from hypothesis import given, settings, strategies as st

from arbiter_helpers import DEFAULT_PLAN, arbiter_project, ck_run, claims, seam

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


# --------------------------------------------------------------------------- properties (Task 8)
# Pure seams, reached by import inside the test body so the file stays collectable before the code exists.

PROPERTIES = {
    "P-1": "test_p1_a_reasoned_override_takes_precedence_and_a_reasonless_one_never_does",
    "P-2": "test_p2_a_requirements_ruling_never_reaches_a_spec_edit_lane",
    "P-3": "test_p3_adding_a_reservation_only_moves_applied_to_needs_input",
    "P-4": "test_p4_an_unusable_principles_read_means_ask_and_ok_may_rule",
    "P-40": "test_p40_a_plan_may_be_decided_exactly_when_the_real_section_holds_a_bullet",
}

PROP = settings(derandomize=True, max_examples=200, database=None, deadline=None)

_words = st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=1, max_size=20).map(str.strip).filter(bool)

_SLUGS = list(DEFAULTS)


def _project_layer():
    parse_principles = seam("arbiter.effective_principles", "parse_principles")
    return parse_principles(
        "# Project Principles\n\n" + "\n".join(f"- **{s}:** the {s} rule." for s in _SLUGS) + "\n", "project")


def _effective(plan_bullets):
    parse_principles, effective_principles = seam("arbiter.effective_principles")
    project = _project_layer()
    plan = parse_principles("## Principles\n\n" + "\n".join(plan_bullets) + "\n", "plan") if plan_bullets else None
    return project, effective_principles(project, plan)


_NO_REASON = ["", " ", "   ", " **Reason:**", " **Reason:** ", " **Reason:**   ", " **", " Why: {t}", " Reason: {t}",
              " because {t}", " {t}"]


@st.composite
def _override(draw):
    """(bullet, slug, reasoned, targets_default_exactly)."""
    slug = draw(st.sampled_from(_SLUGS))
    form = draw(st.sampled_from(["exact", "case", "unknown"]))
    target = {"exact": slug, "case": draw(st.sampled_from([slug.upper(), slug.title(), slug.swapcase()])),
              "unknown": draw(_words.map(lambda w: "zz-" + w.replace(" ", "-")))}[form]
    text = draw(_words)
    reasoned = draw(st.booleans())
    if reasoned:
        tail = f" **Reason:** {draw(_words)}"
    else:
        tail = draw(st.sampled_from(_NO_REASON)).format(t=draw(_words))
    return (f"- **Overrides {target}:** OVR-{text.replace(' ', '-')}.{tail}", slug, reasoned, form)


@PROP
@given(_override())
def test_p1_a_reasoned_override_takes_precedence_and_a_reasonless_one_never_does(case):
    bullet, slug, reasoned, form = case
    project, eff = _effective([bullet])
    ids = [p.id for p in eff.citable]
    default_id = f"project:{slug}"
    assert default_id in [p.id for p in project]
    plan_texts = [p.text for p in eff.citable if p.layer == "plan"]
    if reasoned:
        assert any("OVR-" in t for t in plan_texts), (bullet, eff)
        if form == "exact":
            assert default_id not in ids, (bullet, eff)
    else:
        assert default_id in ids, (bullet, eff)
        assert not any("OVR-" in t for t in plan_texts), (bullet, eff)
        assert not any("OVR-" in p.text for p in eff.citable)
    if form == "unknown":
        assert all(f"project:{s}" in ids for s in _SLUGS), (bullet, eff)


def _check(**kw):
    from claude_kiro.arbiter_policy import RulingCheck

    base = dict(valid=True, stronger_look=False, outcome="code", principle="project:test-intent", reserved_by=None,
                problems=(), raw={})
    base.update(kw)
    return RulingCheck(**base)


_GRANTS = [
    "- **Arbiter may edit requirements:** the arbiter may edit requirements.",
    "- ARBITER MAY EDIT REQUIREMENTS WITHOUT ASKING.",
    "- requirements.md may change without asking",
    "- **requirements.md may change without asking:** go ahead.",
    "| arbiter may edit requirements | yes |",
    "- **Arbiter may change requirements:** the arbiter may add or change requirements without asking me.",
]


@PROP
@given(st.lists(st.sampled_from(_GRANTS), max_size=3), st.sampled_from(["normal", "risky"]), st.booleans())
def test_p2_a_requirements_ruling_never_reaches_a_spec_edit_lane(grants, tier, stronger):
    route_ruling = seam("arbiter.route_ruling", "route_ruling")
    check = _check(outcome="requirements", stronger_look=stronger,
                   raw={"requirement": {"kind": "add", "wording": "1.2 x", "reason": "gap"}})
    _, none = _effective([])
    _, granted = _effective(grants)
    before = route_ruling(check, tier, none)
    after = route_ruling(check, tier, granted)
    assert after.route != "spec_edit" and before.route != "spec_edit"
    if not stronger:
        assert after.route == "needs_input" and before.route == "needs_input"
    assert after == before


_APPLIED = {"spec_edit": "spec_edit", "test_repair": "test_repair", "code": "code"}


@PROP
@given(st.sampled_from(["spec_edit", "test_repair", "code"]),
       st.sampled_from(["code", "test repairs", "tasks.md", "design.md", "test-plan.md", "reported"]),
       st.sampled_from(["normal", "risky"]))
def test_p3_adding_a_reservation_only_moves_applied_to_needs_input(outcome, scope, tier):
    route_ruling = seam("arbiter.route_ruling", "route_ruling")
    parse_principles = seam("arbiter.effective_principles", "parse_principles")
    named = "design.md" if scope == "reported" else scope
    bullet = f"- **Reserves {named}:** that is mine to approve."
    reserved_by = None
    if scope == "reported":
        plan = parse_principles("## Principles\n\n" + bullet + "\n", "plan")
        reserved_by = next(p.id for p in plan if p.reserves)
    check = _check(outcome=outcome, reserved_by=reserved_by)
    _, without = _effective([])
    _, with_ = _effective([bullet])
    before = route_ruling(check, tier, without)
    after = route_ruling(check, tier, with_)
    assert before.route == _APPLIED[outcome]
    names_area = ((scope == "code" and outcome == "code") or (scope == "test repairs" and outcome == "test_repair")
                  or scope == "reported")
    if names_area:
        assert after.route == "needs_input" and after.reason == "reserved", (outcome, scope, after)
    else:
        assert after == before, (outcome, scope, before, after)
    # never the reverse: a reservation cannot turn needs-input into an applied route
    assert not (before.route == "needs_input" and after.route != "needs_input")


@PROP
@given(claims().filter(lambda c: c["trigger"] != "audit"),
       st.sampled_from(["ok", "missing", "directory", "denied", "decode-error"]),
       st.sampled_from(["normal", "safety"]))
def test_p4_an_unusable_principles_read_means_ask_and_ok_may_rule(raw, read, risk):
    import claude_kiro.arbiter_policy as ap

    claim = ap.Claim(**{k: (tuple(v) if isinstance(v, list) else v) for k, v in raw.items()})
    task = ap.TaskMeta(risk=risk, complexity="Low")
    ledger = {"schema": "ck-arbiter-ledger/1", "nextWorktree": 1, "lastEntry": 0, "claims": {}}
    facts = ap.claim_facts(claim, ledger, read, task)
    decision = ap.decide_claim(facts)
    if read == "ok":
        assert decision.action == "rule", (read, decision)
        assert decision.reason != "principles-missing"
    else:
        assert decision.action == "ask", (read, decision)
        assert decision.reason == "principles-missing"


@st.composite
def _plan_text(draw):
    parts = ["# Plan: Demo\n"]
    if draw(st.booleans()):
        parts.append("## Goal\n\n- a goal bullet\n")
    fenced = draw(st.sampled_from(["", "```", "~~~", "  ```"]))
    if fenced:
        parts.append(f"{fenced.strip()}\n## Principles\n\n- fenced bullet\n{fenced.strip()}\n")
    kind = draw(st.sampled_from(["none", "empty", "whitespace", "comment", "bullet", "nothing", "many", "prose"]))
    body = {"none": None, "empty": "", "whitespace": " \n\t\n   \n", "comment": "<!-- add principles here -->\n",
            "bullet": f"- **{draw(st.sampled_from(_SLUGS))}:** {draw(_words)}\n",
            "nothing": "- **Nothing to add:** the project defaults apply.\n",
            "many": "- one\n- two\n", "prose": "just some prose, no bullet\n"}[kind]
    if body is not None:
        parts.append("## Principles\n\n" + body)
    if draw(st.booleans()):
        parts.append("\n## Out of scope\n\n- a bullet under a later heading\n")
    return "\n".join(parts), kind in ("bullet", "nothing", "many")


@PROP
@given(_plan_text())
def test_p40_a_plan_may_be_decided_exactly_when_the_real_section_holds_a_bullet(case):
    from claude_kiro.principles import plan_may_be_decided

    text, expected = case
    ok, reason = plan_may_be_decided(text)
    assert ok is expected, (text, ok, reason)
