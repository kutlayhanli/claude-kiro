"""Contract tests over the shipped plan skill's text (run-principles Task 9).

TC-2 (1.1), TC-4 (1.3), TC-5 (1.4), TC-6 (1.5), TC-14 (2.1), TC-15 (2.2).

These read the real shipped `plugin/skills/plan/SKILL.md` and only that file: never the vendored
`.claude/commands/spec/*.md` copies. They prove the shipped text says these things, not that a session obeys it.
Where a criterion is about order, the assertion compares positions in the text.
"""

import re
from pathlib import Path
from typing import List, Optional

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "plugin" / "skills" / "plan" / "SKILL.md"


def skill_text() -> str:
    assert SKILL.is_file(), f"shipped plan skill missing: {SKILL}"
    return SKILL.read_text(encoding="utf-8")


def split_fences(text: str):
    """Return (prose, template) where template is the content of fenced blocks and prose is everything else."""
    parts = re.split(r"^```[^\n]*\n(.*?)^```[ \t]*$", text, flags=re.S | re.M)
    prose = "".join(parts[0::2])
    template = "\n".join(parts[1::2])
    return prose, template


def strip_frontmatter(text: str) -> str:
    """Drop a leading `---` YAML frontmatter block, so `allowed-tools` entries are not read as instructions."""
    m = re.match(r"\A---\n.*?\n---[ \t]*\n", text, flags=re.S)
    return text[m.end():] if m else text


def first(pattern: str, text: str, start: int = 0) -> Optional[int]:
    m = re.compile(pattern, re.I | re.S).search(text, start)
    return m.start() if m else None


def paragraphs(text: str) -> List[str]:
    return [p for p in re.split(r"\n\s*\n|\n(?=\s*(?:\d+\.|-)\s)", text) if p.strip()]


def para_with(text: str, *patterns: str) -> Optional[str]:
    """First paragraph or list item matching every pattern."""
    for p in paragraphs(text):
        if all(re.search(pat, p, re.I | re.S) for pat in patterns):
            return p
    return None


def test_tc2_init_runs_before_recording_and_reports_creation():
    text = skill_text()
    prose, _ = split_fences(strip_frontmatter(text))
    # The instruction itself (not an `allowed-tools` entry): a paragraph that runs init and reports creation.
    para = para_with(prose, r"ck principles init", r"\bcreated\b", r"\b(tell|say|let) me\b|\btell\b")
    assert para, "the skill must tell me when `ck principles init` created the file"
    init = prose.find(para)
    # Decisions are first recorded in PLAN.md: the first draft (Step 2) and each decision (Step 3).
    record = first(r"create the first draft of `?specs/\[feature-name\]/PLAN\.md", prose)
    assert record is not None, "cannot locate where the skill first records into PLAN.md"
    assert init < record, "`ck principles init` must come before any decision is recorded in PLAN.md"
    step3 = first(r"Record the outcome in PLAN\.md right away", prose)
    assert step3 is not None, "cannot locate the Step 3 recording point"
    assert init < step3, "`ck principles init` must come before the Step 3 recording point"


def test_tc4_asks_tiebreakers_overrides_and_template_has_principles_section():
    text = skill_text()
    prose, template = split_fences(text)
    assert re.search(r"tie-?breakers?", prose, re.I), "skill never asks about tie-breakers"
    assert re.search(r"overrides?", prose, re.I), "skill never asks about overrides"
    assert re.search(r"without asking", prose, re.I), "skill never asks what may change without asking"
    ask = para_with(prose, r"tie-?breakers?", r"\bask\b|AskUserQuestion")
    assert ask, "tie-breakers must be asked for directly"
    ask = para_with(prose, r"overrides?", r"\bask\b|AskUserQuestion")
    assert ask, "overrides must be asked for directly"
    ask = para_with(prose, r"without asking", r"\bask\b|AskUserQuestion")
    assert ask, "what may change without asking must be asked for directly"
    assert re.search(r"^## Principles\s*$", template, re.M), "PLAN.md template lacks a `## Principles` section"


def test_tc5_decided_step_follows_and_depends_on_nonempty_principles():
    text = skill_text()
    prose, _ = split_fences(text)
    decided = first(r"set status to `?Decided`?", prose)
    assert decided is not None, "no step sets Status to Decided"
    before = prose[:decided]
    # a Principles requirement must come before the Decided step ...
    cond = None
    for m in re.finditer(r"(non-?empty|not empty|at least one)[^\n]{0,200}Principles|Principles[^\n]{0,200}(non-?empty|not empty|at least one)", before, re.I):
        cond = m
    assert cond is not None, "nothing before the Decided step requires a non-empty `## Principles`"
    # ... and the Decided step itself must be conditional on it
    line_start = prose.rfind("\n", 0, decided) + 1
    line_end = prose.find("\n", decided)
    step = prose[line_start : line_end if line_end != -1 else len(prose)]
    assert re.search(r"\b(only|once|after|when|if|unless|until)\b", step, re.I) and re.search(r"Principles", step), (
        "the Decided step must be conditional on `## Principles`: " + step
    )
    assert para_with(prose, r"nothing to add|no principles to add", r"\bstate|\bsay|\bwrite|\brecord"), (
        "the skill must say a plan with nothing to add states so"
    )


def test_tc6_asks_reason_before_override_and_records_reason_line():
    text = skill_text()
    prose, template = split_fences(text)
    para = para_with(prose, r"overrides?", r"\breason\b", r"\bask\b|AskUserQuestion", r"before")
    assert para, "the skill must ask for a reason before recording an override"
    assert re.search(r"before[^.\n]{0,80}(record|writ)", para, re.I) or re.search(
        r"(record|writ)[^.\n]{0,80}only after", para, re.I
    ), "the reason must be asked before the override is recorded: " + para
    assert "**Reason:** <text>" in text, "the skill must record the reason as `**Reason:** <text>`"


def test_tc14_asks_what_breaks_without_each_in_scope_item_and_template_has_note():
    text = skill_text()
    prose, template = split_fences(text)
    para = para_with(prose, r"what breaks", r"without", r"in-?scope|\bIn\b|each")
    assert para, "the skill must ask what breaks without each In-scope item"
    assert re.search(r"each", para, re.I), "the question must cover each In-scope item: " + para
    assert "*Without it:*" in template, "PLAN.md template lacks the `*Without it:*` note"


def test_tc15_offers_cut_when_nothing_breaks_and_removes_only_on_acceptance():
    text = skill_text()
    prose, _ = split_fences(text)
    para = para_with(prose, r"nothing (concrete )?breaks|no concrete", r"\bcut\b", r"offer")
    assert para, "the skill must offer the cut when nothing concrete breaks"
    para = para_with(prose, r"\bremove", r"\b(accept|agree|say yes|confirm)", r"\bonly\b")
    assert para, "the item may be removed only when I accept the cut"
    assert re.search(r"never[^.\n]{0,60}remove|remove[^.\n]{0,60}only|only[^.\n]{0,60}remove", para, re.I), para
