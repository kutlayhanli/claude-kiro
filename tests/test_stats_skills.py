"""TC-26: the implement, create and review skills (and their vendored renderings) end with the collect step."""

import re
from pathlib import Path

import pytest

from claude_kiro import plugin as pl

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin"

COLLECT = re.compile(r"ck stats collect --run\s+\S+\s+--auto")

# name -> (skill path, vendored path, regex that starts the reporting section, regex that ends it)
CASES = {
    "implement": ("skills/implement/SKILL.md", ".claude/commands/spec/implement.md", r"^9\. \*\*When it returns", r"^(10\. |## )"),
    "create": ("skills/create/SKILL.md", ".claude/commands/spec/create.md", r"^## Step 4: Close the loop", r"^## (?!Step 4)"),
    "review": ("skills/review/SKILL.md", ".claude/commands/spec/review.md", r"^4\. When it returns", r"^(5\. |## )"),
}


def section(text: str, start: str, end: str) -> str:
    lines = text.splitlines()
    begin = next((i for i, line in enumerate(lines) if re.search(start, line)), None)
    assert begin is not None, f"reporting section {start!r} not found"
    stop = len(lines)
    for i in range(begin + 1, len(lines)):
        if re.search(end, lines[i]):
            stop = i
            break
    return "\n".join(lines[begin:stop])


def sources(name: str):
    skill, vendored, start, end = CASES[name]
    packaged = (PLUGIN / skill).read_text()
    rendered = pl.vendor_files()[vendored]
    return {"packaged": section(packaged, start, end), "vendored": section(rendered, start, end)}


@pytest.mark.parametrize("kind", ["packaged", "vendored"])
@pytest.mark.parametrize("name", sorted(CASES))
def test_collect_step_present(name, kind):
    text = sources(name)[kind]
    assert COLLECT.search(text), f"{name} ({kind}): no `ck stats collect --run <id> --auto` step"
    assert "outside the sandbox" in text, f"{name} ({kind}): collect must run outside the sandbox"
    low = text.lower()
    assert "where" in low and ("record" in low), f"{name} ({kind}): must report where the record went"
    assert "retry" in low, f"{name} ({kind}): failure must be reported with the retry command"
    assert "never let" in low or "without changing" in low or "do not change" in low, (
        f"{name} ({kind}): the result must not change the run's report"
    )


@pytest.mark.parametrize("kind", ["packaged", "vendored"])
@pytest.mark.parametrize("name", sorted(CASES))
def test_collect_step_is_last_reporting_step(name, kind):
    text = sources(name)[kind]
    at = COLLECT.search(text)
    assert at, f"{name} ({kind}): no collect step"
    if name == "implement":
        spend = text.find("ck cost --run")
        assert 0 <= spend < at.start(), "collect step must come after the spend line"
    else:
        questions = text.lower().find("open questions")
        assert questions < 0 or at.start() < questions, f"{name} ({kind}): collect must come before the open questions"


@pytest.mark.parametrize("name", sorted(CASES))
def test_vendored_matches_packaged_collect_line(name):
    parts = sources(name)
    packaged = next(line for line in parts["packaged"].splitlines() if COLLECT.search(line))
    assert packaged.strip() in parts["vendored"], f"{name}: vendored rendering lacks the packaged collect text"
