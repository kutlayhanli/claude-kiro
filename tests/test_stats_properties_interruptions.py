"""Property tests of the interruption counts in a collected record (P-23, P-25, P-26, P-28, P-30).

`ScanResult` (design S14) has no field list in design.md, so the counts are read where the contract fixes them: from
the local record that the real `ck stats collect` writes (Harness Contract 9). Every example builds its fake Claude
home, store and repository inside `with fresh_env() as env:` in the `@given` body, drawing from
`stats_helpers.interruption_strategy()` (events over roles x families x categories, mixed with near-misses and
malformed lines). ck code is imported inside test bodies only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from stats_helpers import (
    FAMILY_COMMANDS,
    AgentSpec,
    FakeClaudeHome,
    ToolAgentSpec,
    backdate_manifest,
    begin,
    described,
    expected_counts,
    fresh_env,
    git_project,
    guard_attachment,
    interruption_strategy,
    interruptions as add_interruptions,
    iso_ms,
    load_record,
    local_by_role,
    local_kinds,
    spec_repo,
    stats,
    to_ms,
    write_guard_log,
)

PROPERTIES = {
    "P-23": "test_p23_every_category_total_equals_the_generated_count_and_the_sum_over_roles_and_families",
    "P-25": "test_p25_wait_is_the_sum_of_positive_result_gaps",
    "P-26": "test_p26_inserting_malformed_lines_changes_no_count",
    "P-28": "test_p28_workflow_and_agent_mode_records_carry_equal_interruption_blocks",
    "P-30": "test_p30_the_guard_log_never_changes_the_counts_and_differences_follow_disagreement",
}

PROP = dict(max_examples=40, derandomize=True, database=None, deadline=None, suppress_health_check=list(HealthCheck))

RUN_START = "2026-10-12T09:00:00Z"
RUN_MS = 20_000_000  # ~5.5 h: every generated timestamp (and every wait) lies inside the run window
KINDS_LOCAL = ("prompts", "guardDenials", "postCheckCatches", "guardEscalations", "autoModeDenials", "headlessDenials",
               "userRejects", "isolationRefusals")
ROLE_AGENT = {"implementer": 0, "reviewer": 1}


# --------------------------------------------------------------------------- rendering


def nonzero(tree: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in tree.items():
        if isinstance(value, dict):
            inner = nonzero(value)
            if inner:
                out[key] = inner
        elif value:
            out[key] = value
    return out


def split_by_role(events: Sequence[tuple]) -> Dict[str, list]:
    out: Dict[str, list] = {"implementer": [], "reviewer": [], "orchestrator": []}
    for event in events:
        out[event[1]].append(event)
    return out


def shift(events: Sequence[tuple], delta_ms: int) -> list:
    return [(*e[:4], e[4] + delta_ms, *e[5:]) for e in events]


def render_workflow(env, events: Sequence[tuple], *, task_extra_lines: Sequence[Any] = (), duplicate: bool = False, spec: str = "demo") -> Tuple[Path, str, Any]:
    """A spec-implement run (`task 1`, `review 1`) holding the events -> (project, run id, session)."""
    proj = git_project(env.root)
    home = FakeClaudeHome(env.claude)
    sess = home.session(cwd=proj)
    run = sess.implement_run(start=RUN_START, duration_ms=RUN_MS, args={"spec": spec, "root": str(proj)},
                             agents=[AgentSpec("task 1", extra_lines=list(task_extra_lines)), AgentSpec("review 1", model="claude-opus-5-5")])
    by = split_by_role(events)
    for _ in range(2 if duplicate else 1):
        add_interruptions((run, 0), by["implementer"])
        add_interruptions((run, 1), by["reviewer"])
        add_interruptions(sess, by["orchestrator"])
    run.session_obj = sess  # type: ignore[attr-defined]
    return proj, run.run_id, run


def collect_workflow(env, proj: Path, run_id: str) -> dict:
    r = stats("collect", "--all", env=env, cwd=proj)
    assert r.exit_code == 0, r.output
    return load_record(env.xdg, run_id)


def render_agent_mode(env, events: Sequence[tuple]) -> dict:
    """The same events as Agent-tool subagents described `[ck:<id>] implement task 1` / `review task 1`; collected with --agents."""
    repo = spec_repo(Path(env.root) / "repo")
    home = FakeClaudeHome(env.claude)
    sess = home.session(cwd=repo, git_branch="integrate/demo")
    rid, manifest = begin(env, repo, "implement", "demo", "integrate/demo")
    backdate_manifest(manifest, 7)
    base = to_ms(json.loads(Path(manifest).read_text())["startTime"])
    agents = sess.tool_agents([
        ToolAgentSpec(described(rid, "implement task 1"), agent_type="spec:implementer"),
        ToolAgentSpec(described(rid, "review task 1"), agent_type="spec:reviewer", model="claude-opus-5-5"),
    ], start=iso_ms(base + 5_000))
    by = split_by_role(shift(events, base + 60_000 - to_ms("2026-10-12T09:05:00Z")))
    add_interruptions((agents, 0), by["implementer"])
    add_interruptions((agents, 1), by["reviewer"])
    add_interruptions(sess, by["orchestrator"])
    r = stats("collect", "--agents", rid, env=env, cwd=repo)
    assert r.exit_code == 0, r.output
    return load_record(env.xdg, rid)


def check_counts(rec: dict, expected: dict) -> None:
    """The record's totals equal the generated counts and the sums over byRole (by role, and by family)."""
    block = rec["interruptions"]
    totals = local_kinds(expected["kinds"])
    totals["waitMs"] = expected["waitMs"]
    totals["guardEscalations"] = 0
    assert block["totals"] == totals, (block["totals"], totals)
    assert nonzero(block["byRole"]) == nonzero(local_by_role(expected)), block["byRole"]
    assert nonzero(block["waitMsByRole"]) == nonzero(expected["waitMsByRole"]), block["waitMsByRole"]
    for kind in KINDS_LOCAL:
        by_role = sum(counts.get(kind, 0) for fams in block["byRole"].values() for counts in fams.values())
        by_family: Dict[str, int] = {}
        for fams in block["byRole"].values():
            for family, counts in fams.items():
                by_family[family] = by_family.get(family, 0) + counts.get(kind, 0)
        assert by_role == sum(by_family.values()) == block["totals"][kind], kind
    assert sum(block["waitMsByRole"].values()) == block["totals"]["waitMs"]


# --------------------------------------------------------------------------- P-23


@settings(**PROP)
@given(drawn=interruption_strategy())
def test_p23_every_category_total_equals_the_generated_count_and_the_sum_over_roles_and_families(drawn):
    """P-23: conservation. Near-misses (RTK allow, gate-only block, quoted phrases, mid-text errors) count nothing."""
    events, expected = drawn
    with fresh_env() as env:
        proj, run_id, _ = render_workflow(env, events)
        check_counts(collect_workflow(env, proj, run_id), expected)


# --------------------------------------------------------------------------- P-25


@st.composite
def ask_gaps(draw):
    """(events, duplicate): n asks by the orchestrator, each with a result gap in seconds, a missing result, or a result before the ask."""
    n = draw(st.integers(0, 6))
    base = to_ms("2026-10-12T09:05:00Z")
    events = []
    for i in range(n):
        gap = draw(st.one_of(st.none(), st.integers(-30, 3_600)))
        wait = None if gap is None else gap * 1000
        events.append(("prompt", "orchestrator", "Bash", FAMILY_COMMANDS["sed"], base + i * 4_000_000 // 10, wait))
    return events, draw(st.booleans())


@settings(**PROP)
@given(drawn=ask_gaps())
def test_p25_wait_is_the_sum_of_positive_result_gaps(drawn):
    """P-25: waitMs = sum of max(0, result - ask) over matched pairs, never negative; a duplicated attachment counts once."""
    events, duplicate = drawn
    want = sum(max(0, e[5]) for e in events if e[5] is not None)
    with fresh_env() as env:
        proj, run_id, _ = render_workflow(env, events, duplicate=duplicate)
        block = collect_workflow(env, proj, run_id)["interruptions"]
    assert block["totals"]["prompts"] == len(events)
    assert block["totals"]["waitMs"] == want >= 0
    assert nonzero(block["waitMsByRole"]) == nonzero({"orchestrator": want})


# --------------------------------------------------------------------------- P-26

INSERTIONS: List[Any] = [
    "{not json",
    {},
    {"type": "attachment"},
    {"type": "mystery", "timestamp": "2026-10-12T09:07:00.000Z"},
    {"type": "attachment", "timestamp": "2026-10-12T09:07:30.000Z", "attachment": {"type": "hook_success", "hookName": "PreToolUse:Bash", "toolUseID": "toolu_nostdout"}},
    {"type": "attachment", "timestamp": "2026-10-12T09:08:00.000Z", "attachment": {"type": "hook_blocking_error", "hookEvent": "PostToolUse", "toolUseID": "toolu_noerr"}},
    "[1, 2, 3]",
    '"just a string"',
]


@settings(**PROP)
@given(drawn=interruption_strategy(), extra=st.lists(st.sampled_from(range(len(INSERTIONS))), min_size=1, max_size=6), tail=st.booleans())
def test_p26_inserting_malformed_lines_changes_no_count(drawn, extra, tail):
    """P-26: malformed or unknown lines, anywhere in a transcript (including a cut last line), change no count."""
    events, expected = drawn
    with fresh_env() as env:
        proj, run_id, run = render_workflow(env, events, task_extra_lines=[INSERTIONS[i] for i in extra])
        if tail:
            with run.transcript(0).open("a") as fh:
                fh.write('{"type": "attachment", "attachment": {"type": "hook_succ')
        check_counts(collect_workflow(env, proj, run_id), expected)


# --------------------------------------------------------------------------- P-28


@settings(**PROP)
@given(drawn=interruption_strategy())
def test_p28_workflow_and_agent_mode_records_carry_equal_interruption_blocks(drawn):
    """P-28: the same events in the same roles give equal blocks in a workflow run and an agent-mode run."""
    events, expected = drawn
    with fresh_env() as env:
        proj, run_id, _ = render_workflow(env, events)
        workflow = collect_workflow(env, proj, run_id)["interruptions"]
    with fresh_env() as env:
        agent = render_agent_mode(env, events)["interruptions"]
    for key in ("v", "totals", "escalationModels"):
        assert workflow[key] == agent[key], key
    assert nonzero(workflow["byRole"]) == nonzero(agent["byRole"])
    assert nonzero(workflow["waitMsByRole"]) == nonzero(agent["waitMsByRole"])
    assert workflow["guardLog"]["state"] == agent["guardLog"]["state"] == "ok"
    assert workflow["totals"]["waitMs"] == expected["waitMs"]


# --------------------------------------------------------------------------- P-30


@st.composite
def log_shapes(draw):
    """(events, expected, mode, delta): the guard log equals the transcripts, misses events, has extras, or holds other sessions' events."""
    events, expected = draw(interruption_strategy())
    mode = draw(st.sampled_from(["equal", "missing", "extra", "other_session"]))
    delta = draw(st.integers(1, 3))
    return events, expected, mode, delta


LOG_FIELDS = {"prompts": ("ask", "test_weakening"), "guardDenials": ("deny", "test_delete"), "postCheckCatches": ("post-check", "post_check_weakening")}


@settings(**PROP)
@given(drawn=log_shapes())
def test_p30_the_guard_log_never_changes_the_counts_and_differences_follow_disagreement(drawn):
    """P-30: the transcript wins; `differences` is noted iff the log and the transcripts disagree."""
    events, expected, mode, delta = drawn
    transcript = {"prompts": expected["kinds"]["prompts"], "guardDenials": expected["kinds"]["guard_denials"],
                  "postCheckCatches": expected["kinds"]["post_check_catches"]}
    in_log = dict(transcript)
    if mode == "missing":
        in_log = {k: max(0, v - delta) for k, v in in_log.items()}
    elif mode == "extra":
        in_log = {k: v + delta for k, v in in_log.items()}
    with fresh_env() as env:
        proj, run_id, run = render_workflow(env, events)
        sess = run.session_obj
        base = to_ms("2026-10-12T09:05:00Z")
        log: List[dict] = []
        for kind, n in in_log.items():
            decision, category = LOG_FIELDS[kind]
            log += [{"ts_ms": base + 1_000 * (len(log) + 1), "sessionId": sess.session_id, "agentId": None, "decision": decision, "category": category}
                    for _ in range(n)]
        if mode == "other_session":
            log += [{"ts_ms": base + 500_000, "sessionId": "some-other-session", "agentId": None, "decision": "deny", "category": "test_delete"} for _ in range(delta)]
        if log:
            write_guard_log(env, log)
        rec = collect_workflow(env, proj, run_id)
    check_counts(rec, expected)
    glog = rec["interruptions"]["guardLog"]
    run_events = sum(in_log.values())
    if run_events == 0:
        return  # nothing in the log for this run: whether an empty log is a "difference" is not fixed by the contract
    want = {k: {"log": in_log[k], "transcripts": transcript[k]} for k in transcript if in_log[k] != transcript[k]}
    assert (glog.get("differences") or {}) == want, (glog, want)
    assert glog["events"] == run_events
