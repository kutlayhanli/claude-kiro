"""TC-7 to TC-12, TC-32, the README half of TC-47 and P-6: the agent-mode instruction documents.

Static reads of the packaged sources and of the `claude_kiro.plugin.vendor_files()` renderings (as
`test_stats_skills.py` does). The template extractor and checker below are written from the requirements' template
rules (2.1 to 2.3: inline-code templates that start with `[ck:<run_id>]`, only the placeholders `<run_id>`, `<N>`,
`<k>`), not from any implementation helper. Project seams (`parse_agent_description`, `AGENT_CREATE_STEPS`) are imported
inside test bodies.
"""

import re
from pathlib import Path
from typing import Dict, List

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from claude_kiro import plugin as pl
from stats_helpers import CONFIDENTIAL, SPEC_TYPES, described

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin"

PROPERTIES = {"P-6": "test_p6_rendered_templates_are_clean_and_parse"}

SETTINGS = settings(
    derandomize=True,
    max_examples=200,
    database=None,
    deadline=None,
    suppress_health_check=list(HealthCheck),
)

# name -> (packaged path under plugin/, vendored path)
SOURCES = {
    "implement": ("skills/implement/SKILL.md", ".claude/commands/spec/implement.md"),
    "create": ("skills/create/SKILL.md", ".claude/commands/spec/create.md"),
    "review": ("skills/review/SKILL.md", ".claude/commands/spec/review.md"),
    "spawn-worktree": ("skills/spawn-worktree/SKILL.md", ".claude/commands/spec/spawn-worktree.md"),
    "without-workflow-tool": ("resources/without-workflow-tool.md", pl.NO_WORKFLOW_GUIDE),
}
KINDS = ("packaged", "vendored")

# Skills whose reporting section holds the collect step (same sections as test_stats_skills.py).
REPORT_SECTIONS = {
    "implement": (r"^9\. \*\*When it returns", r"^(10\. |## )"),
    "create": (r"^## Step 4: Close the loop", r"^## (?!Step 4)"),
    "review": (r"^4\. When it returns", r"^(5\. |## )"),
}

MARK = "[ck:<run_id>] "
ALLOWED = {"<run_id>", "<N>", "<k>"}
FAILED_LINE = "⚠ ck stats begin failed: this run is not recorded in ck stats"
COLLECT_AGENTS = re.compile(r"ck stats collect --agents\s+<run_id>\s+--auto")

# Agent types the doc's table pairs with each kind of work (design.md "Description templates"), plus vendored ck-*.
TYPES_BY_VERB = {
    "implement": ("spec:implementer", "spec:test-writer", "ck-implementer", "ck-test-writer"),
    "revise": ("spec:implementer", "spec:test-writer", "ck-implementer", "ck-test-writer"),
    "review": ("spec:reviewer", "ck-reviewer"),
    "fix": ("spec:fixer", "ck-fixer"),
    "resolve": ("spec:resolver", "ck-resolver"),
    "create": ("general-purpose",),
}

REQUIRED_FORMS = [
    "implement task <N>",
    "implement task <N> retry <k>",
    "revise task <N> round <k>",
    "review task <N> round <k>",
    "resolve task <N>",
    "fix <k>",
    "fix task <N> attempt <k>",
]


def text_of(name: str, kind: str) -> str:
    packaged, vendored = SOURCES[name]
    if kind == "packaged":
        return (PLUGIN / packaged).read_text()
    return pl.vendor_files()[vendored]


def all_cases():
    return [(n, k) for n in SOURCES for k in KINDS]


def ids(case):
    return f"{case[0]}-{case[1]}"


def extract_templates(text: str) -> List[str]:
    """Inline-code templates that start with the marker; a `... x` span stands for the marker plus x."""
    out = []
    for span in re.findall(r"`([^`\n]+)`", text):
        if span.startswith("[ck:<run_id>]") and span.strip() != "[ck:<run_id>]":
            out.append(span)
        elif span.startswith("... "):
            out.append(MARK + span[4:])
    return out


def check_template(template: str) -> List[str]:
    """Problems with one template; each names the template."""
    problems = []
    if not template.startswith(MARK):
        problems.append(f"{template!r}: does not start with {MARK!r}")
        rest = template
    else:
        rest = template[len(MARK):]
    for ph in re.findall(r"<[^<>]*>", rest):
        if ph not in ALLOWED:
            problems.append(f"{template!r}: placeholder {ph} is not allowed (only <run_id>, <N>, <k>)")
    stripped = re.sub(r"<(run_id|N|k)>", "", rest)
    if "<" in stripped or ">" in stripped:
        problems.append(f"{template!r}: stray angle bracket")
    if rest != rest.strip() or not rest:
        problems.append(f"{template!r}: empty or padded body")
    return problems


def description_values(text: str) -> List[str]:
    values = re.findall(r'"description"\s*:\s*"([^"]*)"', text)
    values += re.findall(r"description\s+`([^`\n]+)`", text, flags=re.IGNORECASE)
    return values


def verb_of(template: str) -> str:
    rest = template[len(MARK):] if template.startswith(MARK) else template
    return rest.split(" ", 1)[0]


def render(template: str, *, run_id: str, n, k, marked: bool) -> str:
    rest = template[len(MARK):]
    rest = rest.replace("<N>", str(n)).replace("<k>", str(k)).replace("<run_id>", run_id)
    return described(run_id, rest) if marked else rest


def all_templates() -> Dict[str, List[str]]:
    seen: Dict[str, List[str]] = {}
    for case in all_cases():
        for t in extract_templates(text_of(*case)):
            seen.setdefault(t, []).append(ids(case))
    return seen


def section(text: str, start: str, end: str) -> str:
    lines = text.splitlines()
    begin = next((i for i, line in enumerate(lines) if re.search(start, line)), None)
    assert begin is not None, f"section {start!r} not found"
    stop = len(lines)
    for i in range(begin + 1, len(lines)):
        if re.search(end, lines[i]):
            stop = i
            break
    return "\n".join(lines[begin:stop])


def sentences(text: str) -> List[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n\s*\n|\n[-*] ", text) if s.strip()]


# --- TC-7 -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", [(n, k) for n in ("without-workflow-tool", "spawn-worktree") for k in KINDS], ids=ids)
def test_tc7_begin_failure_continues_without_marker(case):
    text = text_of(*case)
    assert re.search(r"ck stats begin\b", text), f"{ids(case)}: no `ck stats begin` step"
    low = text.lower()
    assert re.search(r"never stop or retry", low), f"{ids(case)}: must say never stop or retry the run because of a begin failure"
    assert "without the" in low and "prefix" in low, f"{ids(case)}: must describe the marker-less form (without the prefix)"
    assert FAILED_LINE in text, f"{ids(case)}: missing the begin-failed report line"
    for s in sentences(text):
        if re.search(r"backfill|recover", s, flags=re.IGNORECASE):
            assert re.search(r"\b(not|never|cannot|can't|no)\b|n't", s, flags=re.IGNORECASE), (
                f"{ids(case)}: claims backfill or a later command recovers a run: {s!r}"
            )


@pytest.mark.parametrize("case", [(n, k) for n in ("without-workflow-tool", "spawn-worktree") for k in KINDS], ids=ids)
def test_tc7_failed_line_replaces_the_collect_step(case):
    text = text_of(*case)
    at = text.find(FAILED_LINE)
    assert at >= 0, f"{ids(case)}: missing the begin-failed report line"
    window = text[max(0, at - 400): at + 400].lower()
    assert "in place of" in window or "instead of" in window, f"{ids(case)}: the line must stand in place of the collect step"


# --- TC-8 -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_tc8_required_forms_present(kind):
    found = set()
    for name in SOURCES:
        found |= {t[len(MARK):] for t in extract_templates(text_of(name, kind)) if t.startswith(MARK)}
    missing = [f for f in REQUIRED_FORMS if f not in found]
    assert not missing, f"{kind}: templates missing from the docs: {missing}"


@pytest.mark.parametrize("kind", KINDS)
def test_tc8_create_steps_equal_agent_create_steps(kind):
    from claude_kiro.analytics import AGENT_CREATE_STEPS

    steps = set()
    for t in extract_templates(text_of("without-workflow-tool", kind)):
        rest = t[len(MARK):]
        if rest.startswith("create "):
            steps.add(rest[len("create "):])
    assert steps == set(AGENT_CREATE_STEPS), (
        f"{kind}: create steps written out differ from AGENT_CREATE_STEPS: "
        f"missing {sorted(set(AGENT_CREATE_STEPS) - steps)}, extra {sorted(steps - set(AGENT_CREATE_STEPS))}"
    )


# --- TC-9 -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", all_cases(), ids=ids)
def test_tc9_templates_use_only_allowed_placeholders(case):
    problems = []
    for t in extract_templates(text_of(*case)):
        problems += check_template(t)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("case", all_cases(), ids=ids)
def test_tc9_descriptions_are_marker_templates_and_old_forms_gone(case):
    text = text_of(*case)
    # Only these two documents carry Agent-call descriptions; the other skills' "description" fields are command args.
    values = description_values(text) if case[0] in ("spawn-worktree", "without-workflow-tool") else []
    for value in values:
        problems = check_template(value)
        assert not problems, f"{ids(case)}: description is not a marker template: {problems}"
    for old in ("implement <spec>: task <N>", "implement <feature>: <task-summary>"):
        assert old not in text, f"{ids(case)}: old description form {old!r} still present"


def test_tc9_the_checker_catches_a_planted_spec_placeholder():
    planted = "[ck:<run_id>] implement <spec>: task <N>"
    problems = check_template(planted)
    assert problems and all(planted in p for p in problems), "checker must fail naming the planted template"
    for bad in ("[ck:<run_id>] create <step>", "[ck:<run_id>] review <lens>", "[ck:<run_id>] fix <p>",
                "implement task <N>", "[ck:ag_3f9c01ab] implement task <N>"):
        assert check_template(bad), f"checker passed {bad!r}"
    for good in ("[ck:<run_id>] implement task <N> retry <k>", "[ck:<run_id>] create review fidelity"):
        assert not check_template(good), f"checker rejected {good!r}"


# --- TC-10 / P-6 ------------------------------------------------------------------------------------------


def check_render(template: str, run_id: str, n, k) -> None:
    from claude_kiro.analytics import parse_agent_description

    verb = verb_of(template)
    assert verb in TYPES_BY_VERB, f"{template!r}: no agent type is paired with verb {verb!r}"
    for marked in (True, False):
        desc = render(template, run_id=run_id, n=n, k=k, marked=marked)
        assert "<" not in desc and ">" not in desc, f"{desc!r}: placeholder left"
        for term in CONFIDENTIAL:
            assert term not in desc, f"{desc!r} carries confidential text {term!r}"
        for agent_type in TYPES_BY_VERB[verb]:
            assert agent_type in SPEC_TYPES or agent_type == "general-purpose"
            got = parse_agent_description(desc, agent_type)
            assert got["role"] != "other", f"{desc!r} ({agent_type}) parses to role other"
            assert got["run_id"] == (run_id if marked else None)


def test_tc10_every_template_renders_and_parses():
    templates = all_templates()
    assert templates, "no [ck:<run_id>] templates found in the agent-mode docs"
    for t in templates:
        check_render(t, "ag_3f9c01ab", 3, 2)


@settings(parent=SETTINGS)
@given(data=st.data())
def test_p6_rendered_templates_are_clean_and_parse(data):
    templates = sorted(all_templates())
    assert templates, "no [ck:<run_id>] templates found in the agent-mode docs"
    t = data.draw(st.sampled_from(templates))
    run_id = "ag_" + data.draw(st.text(alphabet="0123456789abcdef", min_size=8, max_size=8))
    n = data.draw(st.integers(1, 999))
    k = data.draw(st.integers(1, 99))
    check_render(t, run_id, n, k)


# --- TC-11 ------------------------------------------------------------------------------------------------


def bullet(text: str, start: str) -> str:
    lines = text.splitlines()
    begin = next((i for i, line in enumerate(lines) if line.startswith(start)), None)
    assert begin is not None, f"bullet {start!r} not found"
    stop = len(lines)
    for i in range(begin + 1, len(lines)):
        if lines[i].startswith("- **") or lines[i].startswith("#") or not lines[i].strip():
            stop = i
            break
    return "\n".join(lines[begin:stop])


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("start", ["- **No Workflow tool**", "- **Everything `inherit`, review off:**"])
def test_tc11_spawn_worktree_agent_paths_start_with_begin(kind, start):
    path = bullet(text_of("spawn-worktree", kind), start)
    assert "ck stats begin --kind implement" in path, f"{kind}: {start} path does not say to run `ck stats begin --kind implement`"
    assert "[ck:<run_id>]" in path, f"{kind}: {start} path does not use the marker templates"


# --- TC-12 ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_tc12_vendored_contains_each_packaged_agent_line(name):
    packaged = text_of(name, "packaged")
    vendored = text_of(name, "vendored")
    lines = [
        ln for ln in packaged.splitlines()
        if (re.search(r"ck stats begin\b", ln) or "collect --agents" in ln) and pl.PLUGIN_ONLY not in ln
    ]
    assert lines, f"{name}: no `ck stats begin` / `collect --agents` line in the packaged source"
    for ln in lines:
        assert pl.vendored(ln).strip() in vendored, f"{name}: vendored rendering lacks: {ln.strip()!r}"


# --- TC-32 ------------------------------------------------------------------------------------------------


def agent_paths():
    out = {}
    for name in SOURCES:
        for kind in KINDS:
            text = text_of(name, kind)
            if name in REPORT_SECTIONS:
                text = section(text, *REPORT_SECTIONS[name])
            elif name == "spawn-worktree":
                text = section(text, r"^### Phase 5", r"^## ")
            out[(name, kind)] = text
    return out


@pytest.mark.parametrize("case", all_cases(), ids=ids)
def test_tc32_agent_mode_path_ends_with_collect_agents(case):
    text = agent_paths()[case]
    at = COLLECT_AGENTS.search(text)
    assert at, f"{ids(case)}: no `ck stats collect --agents <run_id> --auto` step"
    low = text.lower()
    assert "outside the sandbox" in low, f"{ids(case)}: collect must run outside the sandbox"
    assert "record" in low and "where" in low, f"{ids(case)}: must report where the record went"
    assert "retry" in low, f"{ids(case)}: a failure must be reported with the retry command"
    assert "never let" in low or "without changing" in low or "do not change" in low, (
        f"{ids(case)}: the result must never change the run's report"
    )
    name = case[0]
    if name == "implement":
        spend = text.find("ck cost --run")
        assert 0 <= spend < at.start(), f"{ids(case)}: collect must come after the spend line"
    elif name in ("create", "review"):
        questions = low.find("open questions")
        assert questions < 0 or at.start() < questions, f"{ids(case)}: collect must come before the open questions"


# --- TC-47 (doc half) -------------------------------------------------------------------------------------


def test_tc47_readme_states_what_backfill_cannot_recover():
    readme = (REPO / "README.md").read_text()
    body = section(readme, r"^## Run analytics", r"^## (?!Run analytics)")
    low = " ".join(body.lower().split())
    assert "ck stats begin" in low and "collect --agents" in low, "README run-analytics section lacks agent-mode usage"
    assert re.search(r"(create|review)[^.]*(before|older than|predate)[^.]*(cannot|can't|can not|not) be recovered", low) or re.search(
        r"(cannot|can't|can not) be recovered[^.]*(create|review)", low
    ), "README must say create and review runs from before this feature cannot be recovered"
    assert re.search(r"begin[^.]*failed[^.]*(cannot|can't|can not|not) be recovered", low) or re.search(
        r"(cannot|can't|can not) be recovered[^.]*begin[^.]*failed", low
    ) or re.search(r"failed[^.]*begin[^.]*(cannot|can't|can not|not) be recovered", low), (
        "README must say runs whose `ck stats begin` failed cannot be recovered"
    )
