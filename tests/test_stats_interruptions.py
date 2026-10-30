"""Interruption counts in run records, shared records and `ck stats show` (TC-56..TC-69, TC-81, TC-70 collect half).

Every test runs the real `ck stats` CLI as a subprocess over a fake Claude home built with the line builders of
`tests/stats_helpers.py` (the real transcript shapes), a real store and real git repos. The local record keys are
design.md's (Harness Contract 9): `interruptions.{v, totals, byRole, waitMsByRole, escalationModels, guardLog,
skippedLines}`; the shared keys are snake_case. Project modules are imported inside test bodies only.

`ck stats export` has no `--dry-run`: TC-66 reads the shareable JSON that `ck stats export --to <checkout> --yes`
writes, which is the same bytes that leave the machine.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence

import pytest

from stats_helpers import (
    FAMILY_COMMANDS,
    AgentSpec,
    FakeClaudeHome,
    ToolAgentSpec,
    all_bytes,
    backdate_manifest,
    begin,
    ck_sub,
    described,
    expected_counts,
    fresh_env,
    git_project,
    guard_attachment,
    interruptions as add_interruptions,
    iso_ms,
    kiro_checkout,
    load_record,
    local_by_role,
    local_kinds,
    spec_repo,
    stats,
    to_ms,
    write_guard_log,
)

pytestmark = pytest.mark.usefixtures("no_network")

REPO = Path(__file__).resolve().parent.parent
RELEASE_COMMIT = "6380867"
SHOW_LINE_PREFIX = "interruptions:"
KINDS_LOCAL = ("prompts", "guardDenials", "postCheckCatches", "guardEscalations", "autoModeDenials", "headlessDenials",
               "userRejects", "isolationRefusals")


@pytest.fixture
def env():
    with fresh_env() as e:
        yield e


@pytest.fixture(autouse=True)
def _offline(no_network):
    """TC-70: every child process of every test here must have stayed off the network."""
    yield
    assert not no_network.exists() or no_network.read_text().strip() == "", no_network.read_text()


# --------------------------------------------------------------------------- the TC-56 events


def tc56_events(base_ms: int) -> Dict[str, list]:
    """TC-56's interruptions, as `stats_helpers.interruptions` events, 40 s apart from `base_ms`."""
    def t(i: int) -> int:
        return base_ms + i * 40_000

    fam = FAMILY_COMMANDS
    impl = [
        ("prompt", "implementer", "Bash", fam["sed"], t(0), 90_000),
        ("prompt", "implementer", "Bash", fam["sed"], t(4), 30_000),
        ("guard_denial", "implementer", "Bash", fam["rm"], t(6), None),
        ("post_check", "implementer", "Bash", fam["python"], t(7), None),  # index 3: the gate text comes first
        ("auto", "implementer", "Bash", fam["git"], t(8), None),
        ("isolation", "implementer", "Bash", fam["other"], t(9), None),
        ("gate_block", "implementer", "Bash", "pytest -q", t(10), None),  # near-miss: counts nothing
    ]
    review = [("headless", "reviewer", "Bash", fam["other"], t(2), None)]
    orch = [("reject", "orchestrator", "Edit", "specs/demo/requirements.md", t(3), None)]
    return {"impl": impl, "review": review, "orch": orch}


def all_events(ev: Dict[str, list]) -> list:
    return [*ev["impl"], *ev["review"], *ev["orch"]]


def nonzero(by_role: Dict[str, Any]) -> Dict[str, Any]:
    """Drop zero leaves and empty containers, so a record that spells zeros out equals one that omits them."""
    out: Dict[str, Any] = {}
    for key, value in by_role.items():
        if isinstance(value, dict):
            inner = nonzero(value)
            if inner:
                out[key] = inner
        elif value:
            out[key] = value
    return out


def local_totals(expected: dict, escalations: int = 0) -> dict:
    totals = local_kinds(expected["kinds"])
    totals["waitMs"] = expected["waitMs"]
    totals["guardEscalations"] = escalations
    return totals


def assert_counts(rec: dict, expected: dict, *, escalations: int = 0, by_role: Optional[dict] = None) -> None:
    block = rec["interruptions"]
    assert block["v"] == 1
    assert block["totals"] == local_totals(expected, escalations), block["totals"]
    want = local_by_role(expected) if by_role is None else by_role
    assert nonzero(block["byRole"]) == nonzero(want), block["byRole"]
    assert nonzero(block["waitMsByRole"]) == nonzero(expected["waitMsByRole"]), block["waitMsByRole"]
    for kind in KINDS_LOCAL:  # totals equal the sum over byRole (conservation, P-23)
        assert block["totals"][kind] == sum(n.get(kind, 0) for fams in block["byRole"].values() for n in fams.values()), kind


# --------------------------------------------------------------------------- worlds


def workflow_world(env, *, spec: str = "demo", ev: Optional[Dict[str, list]] = None, extra_agents: Sequence[AgentSpec] = (),
                   task_extra_lines: Sequence[Any] = (), start: str = "2026-10-12T09:00:00Z", base: str = "2026-10-12T09:02:00Z") -> SimpleNamespace:
    """A spec-implement run (agents `task 1`, `review 1`, plus extras) with TC-56's events added to the transcripts."""
    ev = tc56_events(to_ms(base)) if ev is None else ev
    proj = git_project(env.root)
    home = FakeClaudeHome(env.claude)
    sess = home.session(cwd=proj)
    agents = [AgentSpec("task 1", extra_lines=list(task_extra_lines)), AgentSpec("review 1", model="claude-opus-5-5"), *extra_agents]
    run = sess.implement_run(start=start, args={"spec": spec, "root": str(proj)}, agents=agents)
    add_interruptions((run, 0), ev["impl"])
    add_interruptions((run, 1), ev["review"])
    add_interruptions(sess, ev["orch"])
    return SimpleNamespace(proj=proj, home=home, sess=sess, run=run, ev=ev, expected=expected_counts(all_events(ev)))


def collect_all(env, w) -> dict:
    r = stats("collect", "--all", env=env, cwd=w.proj)
    assert r.exit_code == 0, r.output
    return load_record(env.xdg, w.run.run_id)


def record_file(env, run_id: str) -> Path:
    return env.xdg / "claude-kiro" / "runs" / f"{run_id}.json"


def rewrite_record(env, run_id: str, fn) -> None:
    path = record_file(env, run_id)
    rec = json.loads(path.read_text())
    fn(rec)
    path.write_text(json.dumps(rec))


def export_shared(env):
    """`ck stats export --to <checkout> --yes` -> (checkout, result, shareable records)."""
    checkout, _bare = kiro_checkout(env.root)
    r = stats("export", "--to", str(checkout), "--yes", env=env, cwd=checkout)
    root = checkout / "analytics" / "runs"
    records = [json.loads(p.read_text()) for p in sorted(root.rglob("*.json"))] if root.exists() else []
    return checkout, r, records


def guard_event(w, i: int, decision: str, category: str, *, agent: Optional[int] = None, base: Optional[int] = None, **extra: Any) -> dict:
    """A guard-log event of the run's session at TC-56 time slot `i` (40 s apart)."""
    ts = (base if base is not None else to_ms("2026-10-12T09:02:00Z")) + i * 40_000
    return {"ts_ms": ts, "sessionId": w.sess.session_id, "agentId": w.run.agent_ids[agent] if agent is not None else None,
            "decision": decision, "category": category, **extra}


def tc56_log_events(w) -> List[dict]:
    """Guard-log events that agree with TC-56's transcripts: 2 asks, 1 deny, 1 post-check (all by the implementer)."""
    return [
        guard_event(w, 0, "ask", "test_weakening", agent=0),
        guard_event(w, 4, "ask", "test_weakening", agent=0),
        guard_event(w, 6, "deny", "test_delete", agent=0),
        guard_event(w, 7, "post-check", "post_check_weakening", agent=0),
    ]


# --------------------------------------------------------------------------- TC-56, TC-57


def test_tc56_every_interruption_kind_is_counted_by_role_and_family(env):
    """TC-56."""
    w = workflow_world(env)
    rec = collect_all(env, w)
    assert w.expected["waitMs"] == 120_000
    assert rec["interruptions"]["totals"] == {
        "prompts": 2, "waitMs": 120_000, "guardDenials": 1, "postCheckCatches": 1, "guardEscalations": 0,
        "autoModeDenials": 1, "headlessDenials": 1, "userRejects": 1, "isolationRefusals": 1,
    }
    assert_counts(rec, w.expected)
    by_role = nonzero(rec["interruptions"]["byRole"])
    assert by_role["implementer"]["sed"] == {"prompts": 2}
    assert by_role["implementer"]["rm"] == {"guardDenials": 1}
    assert by_role["implementer"]["python"] == {"postCheckCatches": 1}
    assert by_role["implementer"]["git"] == {"autoModeDenials": 1}
    assert by_role["implementer"]["other"] == {"isolationRefusals": 1}
    assert by_role["reviewer"] == {"other": {"headlessDenials": 1}}
    assert by_role["orchestrator"] == {"other": {"userRejects": 1}}
    assert nonzero(rec["interruptions"]["waitMsByRole"]) == {"implementer": 120_000}


def test_tc57_guard_escalations_come_from_the_guard_agent_rows(env):
    """TC-57."""
    extra = [
        AgentSpec("task 1 guard 1 (opus)", model="claude-opus-5-5"),
        AgentSpec("task 1 guard 2 (fable)", model="claude-fable-5-5"),
        AgentSpec("park 1", model="claude-haiku-5-5"),
    ]
    w = workflow_world(env, extra_agents=extra)
    rec = collect_all(env, w)
    block = rec["interruptions"]
    assert block["totals"]["guardEscalations"] == 2
    assert block["escalationModels"] == {"opus": 1, "fable": 1}
    park = [a for a in rec["agents"] if a["label"] == "park 1"]
    assert len(park) == 1 and park[0]["role"] == "orchestrator", park
    guards = [a for a in rec["agents"] if " guard " in a["label"]]
    assert len(guards) == 2 and all(a["role"] == "implementer" for a in guards), guards
    assert sum(n.get("guardEscalations", 0) for fams in block["byRole"]["implementer"].values() for n in [fams]) == 2


# --------------------------------------------------------------------------- TC-58


@pytest.mark.parametrize("mode", ["create", "review"])
def test_tc58_create_and_review_runs_use_the_same_keys_and_roles(env, mode):
    """TC-58."""
    from stats_helpers import create_result

    proj = git_project(env.root)
    home = FakeClaudeHome(env.claude)
    sess = home.session(cwd=proj)
    writer = "write requirements.md" if mode == "create" else "revise spec + write review.md"
    run = sess.create_run(start="2026-10-12T08:00:00Z", args={"feature": "demo", "root": str(proj), "mode": mode},
                          result=create_result(mode=mode), agents=[AgentSpec(writer, phase="Requirements")])
    base = to_ms("2026-10-12T08:02:00Z")
    add_interruptions(sess, [("prompt", "orchestrator", "Edit", "specs/demo/requirements.md", base, 20_000)])
    add_interruptions((run, 0), [("isolation", "orchestrator", "Write", "specs/demo/design.md", base + 60_000, None)])
    r = stats("collect", "--all", env=env, cwd=proj)
    assert r.exit_code == 0, r.output
    rec = load_record(env.xdg, run.run_id)
    row = [a for a in rec["agents"] if a["label"] == writer]
    assert len(row) == 1, rec["agents"]
    writer_role = row[0]["role"] if row[0]["role"] in ("implementer", "test-writer", "reviewer", "fixer", "resolver") else "orchestrator"
    want: Dict[str, Any] = {"orchestrator": {"other": {"prompts": 1}}}
    want.setdefault(writer_role, {}).setdefault("other", {})["isolationRefusals"] = 1
    block = rec["interruptions"]
    assert nonzero(block["byRole"]) == want, block["byRole"]
    assert block["totals"]["prompts"] == 1 and block["totals"]["isolationRefusals"] == 1
    assert block["totals"]["waitMs"] == 20_000


# --------------------------------------------------------------------------- TC-59, TC-60


def test_tc59_non_shell_interruptions_are_family_other_and_shell_families_follow_the_command(env):
    """TC-59."""
    proj = git_project(env.root)
    home = FakeClaudeHome(env.claude)
    sess = home.session(cwd=proj)
    run = sess.implement_run(args={"spec": "demo", "root": str(proj)})
    base = to_ms("2026-10-12T09:02:00Z")
    events = [
        ("guard_denial", "orchestrator", "Edit", "specs/demo/requirements.md", base, None),
        ("prompt", "orchestrator", "Write", "specs/demo/tasks.md", base + 40_000, None),
        ("prompt", "orchestrator", "Bash", "cd /work/demo && sed -i 's/a/b/' tests/test_x.py", base + 80_000, None),
        ("prompt", "orchestrator", "Bash", "uv run python x.py", base + 120_000, None),
        ("prompt", "orchestrator", "Bash", "echo hi > f.txt", base + 160_000, None),
    ]
    add_interruptions(sess, events)
    r = stats("collect", "--all", env=env, cwd=proj)
    assert r.exit_code == 0, r.output
    rec = load_record(env.xdg, run.run_id)
    assert nonzero(rec["interruptions"]["byRole"]) == {"orchestrator": {
        "other": {"guardDenials": 1, "prompts": 1}, "sed": {"prompts": 1}, "python": {"prompts": 1}, "redirect": {"prompts": 1}}}


def test_tc60_wait_is_the_gap_to_the_result_and_never_negative(env):
    """TC-60."""
    proj = git_project(env.root)
    home = FakeClaudeHome(env.claude)
    sess = home.session(cwd=proj)
    run = sess.implement_run(args={"spec": "demo", "root": str(proj)})
    base = to_ms("2026-10-12T09:02:00Z")
    cmd = FAMILY_COMMANDS["sed"]
    events = [
        ("prompt", "orchestrator", "Bash", cmd, base, 150_000),  # answered 2.5 min later
        ("prompt", "orchestrator", "Bash", cmd, base + 200_000, None),  # no result line
        ("prompt", "orchestrator", "Bash", cmd, base + 300_000, -5_000),  # result stamped before the ask
    ]
    add_interruptions(sess, events)
    r = stats("collect", "--all", env=env, cwd=proj)
    assert r.exit_code == 0, r.output
    block = load_record(env.xdg, run.run_id)["interruptions"]
    assert block["totals"]["prompts"] == 3
    assert block["totals"]["waitMs"] == 150_000
    assert nonzero(block["waitMsByRole"]) == {"orchestrator": 150_000}


# --------------------------------------------------------------------------- TC-61, TC-62, TC-81


def test_tc61_a_log_that_agrees_adds_nothing_and_a_disagreement_is_noted(env):
    """TC-61."""
    w = workflow_world(env)
    write_guard_log(env, tc56_log_events(w))
    rec = collect_all(env, w)
    log = rec["interruptions"]["guardLog"]
    assert log["state"] == "ok" and log["events"] == 4, log
    assert not log.get("differences"), log
    assert_counts(rec, w.expected)

    write_guard_log(env, [guard_event(w, 8, "deny", "test_delete", agent=0)])
    rec = collect_all(env, w)
    log = rec["interruptions"]["guardLog"]
    assert log["differences"] == {"guardDenials": {"log": 2, "transcripts": 1}}, log
    assert_counts(rec, w.expected)  # the transcript wins


def has_guard_log_missing(rec: dict) -> bool:
    return any(isinstance(m, dict) and m.get("what") == "guard_log" for m in rec.get("missing", []))


def log_file(env) -> Path:
    return env.xdg / "claude-kiro" / "guard-events.jsonl"


def test_tc62a_a_log_never_created_is_ok_with_zero_events(env):
    """TC-62 (a)."""
    w = workflow_world(env)
    assert not log_file(env).exists()
    rec = collect_all(env, w)
    log = rec["interruptions"]["guardLog"]
    assert log["state"] == "ok" and log["events"] == 0 and not log.get("differences"), log
    assert not has_guard_log_missing(rec), rec.get("missing")
    assert_counts(rec, w.expected)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_tc62b_an_unreadable_log_is_listed_as_missing(env):
    """TC-62 (b)."""
    w = workflow_world(env)
    path = write_guard_log(env, tc56_log_events(w))
    path.chmod(0)
    try:
        rec = collect_all(env, w)
    finally:
        path.chmod(0o600)
    assert rec["interruptions"]["guardLog"]["state"] == "unreadable"
    assert has_guard_log_missing(rec), rec.get("missing")
    assert_counts(rec, w.expected)


@pytest.mark.parametrize("rotated_ts, state", [("2026-10-12T09:03:00Z", "rotated"), ("2026-10-12T08:00:00Z", "ok")])
def test_tc62c_a_rotated_log_is_missing_only_when_its_oldest_event_is_newer_than_the_run_start(env, rotated_ts, state):
    """TC-62 (c)."""
    w = workflow_world(env)
    path = write_guard_log(env, [guard_event(w, 0, "ask", "test_weakening", agent=0, ts_ms=to_ms(rotated_ts))])
    path.rename(path.with_name("guard-events.1.jsonl"))
    write_guard_log(env, [guard_event(w, 5, "deny", "test_delete", agent=0)])  # 09:05:20, newer than the run start
    rec = collect_all(env, w)
    assert rec["interruptions"]["guardLog"]["state"] == state, rec["interruptions"]["guardLog"]
    assert has_guard_log_missing(rec) == (state == "rotated"), rec.get("missing")
    assert_counts(rec, w.expected)


def test_tc81_two_runs_in_one_session_keep_their_own_log_events(env):
    """TC-81."""
    from stats_helpers import create_result

    proj = git_project(env.root)
    home = FakeClaudeHome(env.claude)
    sess = home.session(cwd=proj)
    create = sess.create_run(start="2026-10-12T08:00:00Z", args={"feature": "demo", "root": str(proj)}, result=create_result())
    ev = tc56_events(to_ms("2026-10-12T09:02:00Z"))
    impl = sess.implement_run(start="2026-10-12T09:00:00Z", args={"spec": "demo", "root": str(proj)},
                              agents=[AgentSpec("task 1"), AgentSpec("review 1", model="claude-opus-5-5")])
    add_interruptions((impl, 0), ev["impl"])
    add_interruptions((impl, 1), ev["review"])
    add_interruptions(sess, ev["orch"])
    add_interruptions(sess, [("guard_denial", "orchestrator", "Edit", "specs/demo/requirements.md", to_ms("2026-10-12T08:03:00Z"), None)])
    w = SimpleNamespace(sess=sess, run=impl)
    log = tc56_log_events(w)
    log.append({"ts_ms": to_ms("2026-10-12T08:03:00Z"), "sessionId": sess.session_id, "agentId": None, "decision": "deny", "category": "spec_edit"})  # the create run's own
    log.append({"ts_ms": to_ms("2026-10-12T09:04:00Z"), "sessionId": sess.session_id, "agentId": "a-not-in-this-run", "decision": "deny", "category": "test_delete"})
    write_guard_log(env, log)
    r = stats("collect", "--all", env=env, cwd=proj)
    assert r.exit_code == 0, r.output
    rec = load_record(env.xdg, impl.run_id)
    glog = rec["interruptions"]["guardLog"]
    assert glog["events"] == 4, glog
    assert not glog.get("differences"), glog
    assert_counts(rec, expected_counts(all_events(ev)))
    crec = load_record(env.xdg, create.run_id)
    assert crec["interruptions"]["guardLog"]["events"] == 1, crec["interruptions"]["guardLog"]
    assert crec["interruptions"]["totals"]["guardDenials"] == 1


# --------------------------------------------------------------------------- TC-63


def test_tc63_malformed_and_unknown_lines_change_no_count(env):
    """TC-63."""
    base = to_ms("2026-10-12T09:02:00Z")
    bad_attachment = guard_attachment("toolu_notjson", "ask", "x", ts_ms=base + 5_000)
    bad_attachment["attachment"]["stdout"] = "not json"
    extra = ["{not json", {}, {"type": "attachment"}, {"type": "mystery", "timestamp": iso_ms(base + 1_000)}, bad_attachment]
    ev = tc56_events(base)
    ev["orch"] = [*ev["orch"], ("malformed", "orchestrator", "Bash", "{not json", base + 11, None),
                  ("malformed", "orchestrator", "Bash", {"type": "attachment"}, base + 12, None)]
    w = workflow_world(env, ev=ev, task_extra_lines=extra)
    with w.run.transcript(0).open("a") as fh:  # a half line at EOF
        fh.write('{"type": "assistant", "timestamp": "2026-10-12T09:09')
    rec = collect_all(env, w)
    assert_counts(rec, w.expected)
    assert rec["interruptions"]["skippedLines"] >= 1


# --------------------------------------------------------------------------- TC-64


def test_tc64_a_record_from_before_the_feature_shows_not_recorded_and_is_not_shared(env):
    """TC-64."""
    w = workflow_world(env)
    collect_all(env, w)
    rewrite_record(env, w.run.run_id, lambda rec: rec.pop("interruptions", None))
    shown = stats("show", w.run.run_id, env=env, cwd=w.proj)
    assert shown.exit_code == 0, shown.output
    lines = [ln.strip() for ln in shown.stdout.splitlines() if ln.strip().startswith(SHOW_LINE_PREFIX)]
    assert lines == ["interruptions: not recorded"], shown.stdout
    _checkout, r, records = export_shared(env)
    assert r.exit_code == 0, r.output
    assert len(records) == 1 and "interruptions" not in records[0]
    listing = stats("show", env=env, cwd=w.proj)
    assert listing.exit_code == 0 and "0 prompts" not in listing.stdout, listing.stdout
    as_json = stats("show", "--json", env=env, cwd=w.proj)
    assert as_json.exit_code == 0, as_json.output
    assert '"prompts": 0' not in as_json.stdout and '"prompts":0' not in as_json.stdout


# --------------------------------------------------------------------------- TC-65 (agent mode)


def start_ms_of(manifest: Path) -> int:
    return to_ms(json.loads(Path(manifest).read_text())["startTime"])


def agent_world(env) -> SimpleNamespace:
    """An agent-mode run (manifest backdated 7 h so `--all` closes it) holding TC-56's events and one guard-ladder agent."""
    repo = spec_repo(env.root / "repo")
    home = FakeClaudeHome(env.claude)
    sess = home.session(cwd=repo, git_branch="integrate/demo")
    rid, manifest = begin(env, repo, "implement", "demo", "integrate/demo")
    backdate_manifest(manifest, 7)
    base = start_ms_of(manifest)
    agents = sess.tool_agents([
        ToolAgentSpec(described(rid, "implement task 1"), agent_type="spec:implementer"),
        ToolAgentSpec(described(rid, "review task 1"), agent_type="spec:reviewer", model="claude-opus-5-5"),
        ToolAgentSpec(described(rid, "implement task 1 guard 1"), agent_type="spec:implementer", model="claude-fable-5-5"),
    ], start=iso_ms(base + 5_000))
    ev = tc56_events(base + 60_000)
    add_interruptions((agents, 0), ev["impl"])
    add_interruptions((agents, 1), ev["review"])
    add_interruptions(sess, ev["orch"])
    return SimpleNamespace(repo=repo, rid=rid, ev=ev, expected=expected_counts(all_events(ev)), sess=sess)


def agent_by_role(expected: dict) -> dict:
    by_role = local_by_role(expected)
    by_role.setdefault("implementer", {}).setdefault("other", {})["guardEscalations"] = 1
    return by_role


@pytest.mark.parametrize("path", ["agents", "all", "auto"])
def test_tc65_agent_mode_runs_carry_the_same_interruptions(env, path):
    """TC-65."""
    w = agent_world(env)
    args = {"agents": ["--agents", w.rid], "all": ["--all"], "auto": ["--agents", w.rid, "--auto"]}[path]
    r = stats("collect", *args, env=env, cwd=w.repo)
    assert r.exit_code == 0, r.output
    rec = load_record(env.xdg, w.rid)
    assert_counts(rec, w.expected, escalations=1, by_role=agent_by_role(w.expected))
    assert rec["interruptions"]["escalationModels"] == {"fable": 1}


# --------------------------------------------------------------------------- TC-66, TC-67


PLANTED_IN_TC56 = ("sed -i", "git push", "rm -rf", "tests/test_calc.py", "zephyr-payroll", "destroys a tracked file", "weakens a test",
                   "edited a test file", "specs/demo/requirements.md")
SHARED_KINDS = ("prompts", "guard_denials", "post_check_catches", "guard_escalations", "auto_mode_denials", "headless_denials",
                "user_rejects", "isolation_refusals")


def assert_enum_numbers_only(block: dict) -> None:
    from claude_kiro.analytics import share

    assert set(block) == {*SHARED_KINDS, "wait_minutes", "escalation_models", "by_role", "wait_minutes_by_role"}, set(block)
    for kind in SHARED_KINDS:
        assert type(block[kind]) is int, kind
    assert isinstance(block["wait_minutes"], (int, float)) and not isinstance(block["wait_minutes"], bool)
    assert set(block["escalation_models"]) <= set(share.MODEL_FAMILIES)
    assert all(type(n) is int for n in block["escalation_models"].values())
    assert set(block["by_role"]) <= set(share.INTERRUPTION_ROLES)
    for fams in block["by_role"].values():
        assert set(fams) <= set(share.COMMAND_FAMILIES)
        for counts in fams.values():
            assert set(counts) <= set(SHARED_KINDS) and all(type(n) is int for n in counts.values())
    assert set(block["wait_minutes_by_role"]) <= set(share.INTERRUPTION_ROLES)
    assert all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in block["wait_minutes_by_role"].values())


def test_tc66_the_shared_record_holds_only_enum_keyed_numbers(env):
    """TC-66."""
    from claude_kiro.analytics import share

    w = workflow_world(env, spec="zephyr-payroll")
    collect_all(env, w)
    checkout, r, records = export_shared(env)
    assert r.exit_code == 0, r.output
    assert len(records) == 1
    rec = records[0]
    assert rec["schema"] == "ck-run-analytics/1"
    assert share.validate_shareable(rec) == []
    block = rec["interruptions"]
    assert_enum_numbers_only(block)
    assert {k: block[k] for k in SHARED_KINDS} == {
        "prompts": 2, "guard_denials": 1, "post_check_catches": 1, "guard_escalations": 0, "auto_mode_denials": 1,
        "headless_denials": 1, "user_rejects": 1, "isolation_refusals": 1}
    assert block["wait_minutes"] == pytest.approx(2.0)
    assert block["wait_minutes_by_role"]["implementer"] == pytest.approx(2.0)
    assert block["by_role"]["implementer"]["sed"]["prompts"] == 2
    data = all_bytes(checkout / "analytics")
    for planted in PLANTED_IN_TC56:
        assert planted.encode() not in data, planted
    assert b"guardLog" not in data and b"skippedLines" not in data and b"differences" not in data


def test_tc67_schema_md_documents_the_new_paths_and_keeps_the_old_ones(env, tmp_path):
    """TC-67."""
    from claude_kiro.analytics import share
    from test_stats_export import assert_documented

    w = workflow_world(env, extra_agents=[AgentSpec("task 1 guard 1 (fable)", model="claude-fable-5-5")])
    collect_all(env, w)
    _checkout, r, records = export_shared(env)
    assert r.exit_code == 0, r.output
    assert "interruptions" in records[0]
    assert_documented(records)
    new_paths = {p for p in share.SHAREABLE_FIELDS if p.startswith("interruptions")}
    assert new_paths, "SHAREABLE_SCHEMA has no interruptions paths"
    old = old_release(tmp_path)
    listed = subprocess.run([sys.executable, "-c", "import json; from claude_kiro.analytics.share import SHAREABLE_FIELDS as F; print(json.dumps(sorted(F)))"],
                            env=old_env(old), capture_output=True, text=True, timeout=120)
    assert listed.returncode == 0, listed.stderr
    old_paths = set(json.loads(listed.stdout))
    assert old_paths and old_paths <= set(share.SHAREABLE_FIELDS), sorted(old_paths - set(share.SHAREABLE_FIELDS))


# --------------------------------------------------------------------------- TC-68


def interruption_lines(text: str) -> List[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith(SHOW_LINE_PREFIX)]


def test_tc68_show_prints_one_interruptions_line_in_the_given_format(env):
    """TC-68."""
    base = to_ms("2026-10-12T09:02:00Z")
    rm = FAMILY_COMMANDS["rm"]
    ev = {"impl": [("guard_denial", "implementer", "Bash", rm, base + i * 40_000, None) for i in range(3)], "review": [], "orch": []}
    w = workflow_world(env, ev=ev, extra_agents=[AgentSpec("task 1 guard 1 (fable)", model="claude-fable-5-5")])
    collect_all(env, w)
    shown = stats("show", w.run.run_id, env=env, cwd=w.proj)
    assert shown.exit_code == 0, shown.output
    assert interruption_lines(shown.stdout) == ["interruptions: 0 prompts (0 min waiting) · 3 guard denials → 1 escalation (fable) · 0 refusals"], shown.stdout


def test_tc68_other_nonzero_counts_join_the_same_line(env):
    """TC-68 (post-check, auto-mode, headless and rejection counts)."""
    w = workflow_world(env)
    collect_all(env, w)
    shown = stats("show", w.run.run_id, env=env, cwd=w.proj)
    assert shown.exit_code == 0, shown.output
    lines = interruption_lines(shown.stdout)
    assert len(lines) == 1, shown.stdout
    line = lines[0]
    assert line.startswith("interruptions: 2 prompts (2 min waiting) · 1 guard denial") or line.startswith("interruptions: 2 prompts (2 min waiting) · 1 guard denials"), line
    assert re.search(r"\b1 refusal", line), line
    assert re.search(r"\b1 post-check catch", line), line
    assert re.search(r"\b1 auto-mode denial", line), line
    assert re.search(r"\b1 headless denial", line), line
    assert re.search(r"\b1 rejection", line), line


# --------------------------------------------------------------------------- TC-69


def old_release(tmp: Path) -> Path:
    probe = subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"{RELEASE_COMMIT}^{{commit}}"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip(f"release commit {RELEASE_COMMIT} is not in this clone (shallow clone)")
    tar_path = Path(tmp) / "release.tar"
    out = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", "-o", str(tar_path), RELEASE_COMMIT], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    dest = Path(tmp) / "release-0.10.2"
    dest.mkdir()
    with tarfile.open(tar_path) as tar:
        tar.extractall(dest)
    return dest


def old_env(release: Path) -> Dict[str, str]:
    path = str(release / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")
    return {**os.environ, "PYTHONPATH": path.rstrip(os.pathsep)}


def test_tc69_release_0_10_2_still_lists_a_record_with_interruptions(env, tmp_path):
    """TC-69."""
    release = old_release(tmp_path)
    w = workflow_world(env)
    rec = collect_all(env, w)
    assert "interruptions" in rec
    out = subprocess.run([sys.executable, "-m", "claude_kiro.cli", "stats", "show"], cwd=w.proj, env={**old_env(release), **env},
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    assert w.run.run_id in out.stdout, out.stdout


# --------------------------------------------------------------------------- TC-70 (collect half)


def test_tc70_collect_with_interruptions_makes_no_network_call(env, no_network):
    """TC-70: `ck stats collect` over TC-56's run, with a guard log, stays off the network (net log empty)."""
    w = workflow_world(env)
    write_guard_log(env, tc56_log_events(w))
    rec = collect_all(env, w)
    assert_counts(rec, w.expected)
    assert not no_network.exists() or no_network.read_text().strip() == "", no_network.read_text()
