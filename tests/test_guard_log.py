"""The private guard event log (prompt-free-runs Story 9: TC-50..TC-55, and the guard half of TC-70).

Real hooks, a real data directory and, for TC-55, the real `ck stats export` / `ck stats submit` CLIs. Events are
read through `guard_events()`, which reads `guardlog.log_path()` and the rotated `guard-events.1.jsonl`.
"""

import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import CALC_TEST, git, write_tasks
from guard_helpers import ck_json, guard_events, hook_input, post_message, pre, run_bash
from stats_helpers import SEEDED_RUN_IDS, fake_claude_drafter, seeded_home, write_guard_log
from test_guard_postcheck import AGENT, CALC_PATH, SED_WEAKEN, cli_cycle, cli_hook, cli_message
from test_stats_leak import HIT_WORDS, World

RM = "rm tests/test_calc.py"
FIELDS = {"v", "ts", "sessionId", "agentId", "agentType", "caller", "tool", "decision", "category", "reason", "path",
          "command", "family", "toolUseId"}


def log_file() -> Path:
    from claude_kiro.analytics import guardlog

    return Path(guardlog.log_path())


def three_calls(project, session):
    """A main-session ask, a task-agent deny and a post-check catch. Returns (ask, deny, post message)."""
    ck_json(project, guard={"tests": "ask"})
    ask = pre(project, session, "Bash", command=RM)
    deny = pre(project, session, "Bash", agent_id=AGENT, agent_type="spec:implementer", command=RM)
    out = run_bash(project, session, SED_WEAKEN, agent_id=AGENT, agent_type="spec:implementer")
    return ask, deny, post_message(out.post)


# --------------------------------------------------------------------------- TC-50


def test_tc50_each_blocking_call_appends_one_complete_event(project, session):
    assert guard_events() == []
    ask, deny, caught = three_calls(project, session)
    assert ask["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert deny["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert caught
    events = guard_events()
    assert len(events) == 3, events
    by_decision = {e["decision"]: e for e in events}
    assert set(by_decision) == {"ask", "deny", "post-check"}

    for event in events:
        assert event["v"] == 1
        assert FIELDS <= set(event), FIELDS - set(event)
        stamp = datetime.fromisoformat(event["ts"].replace("Z", "+00:00"))
        assert stamp.utcoffset() == timezone.utc.utcoffset(None)
        assert event["sessionId"] == session
        assert event["tool"] == "Bash"
        assert event["path"] == CALC_PATH
        assert isinstance(event["toolUseId"], str) and event["toolUseId"]
        assert event["reason"]

    ask_event, deny_event, post_event = by_decision["ask"], by_decision["deny"], by_decision["post-check"]
    assert ask_event["caller"] == "main_session" and not ask_event.get("agentId") and not ask_event.get("agentType")
    assert ask_event["reason"] == ask["hookSpecificOutput"]["permissionDecisionReason"]
    assert ask_event["command"] == RM and ask_event["family"] == "rm" and ask_event["category"] == "test_delete"

    assert deny_event["caller"] == "task_agent"
    assert deny_event["agentId"] == AGENT and deny_event["agentType"] == "spec:implementer"
    assert deny_event["reason"] == deny["hookSpecificOutput"]["permissionDecisionReason"]
    assert deny_event["command"] == RM and deny_event["family"] == "rm"

    assert post_event["caller"] == "task_agent" and post_event["agentId"] == AGENT
    assert post_event["category"] == "post_check_weakening"
    assert post_event["command"] == SED_WEAKEN and post_event["family"] == "sed"
    assert post_event["reason"] in caught or caught in post_event["reason"]


def test_tc50_file_tool_events_have_no_command(project, session):
    ck_json(project, guard={"tests": "ask"})
    result = pre(project, session, "Edit", agent_id=AGENT, file_path=str(project / CALC_PATH),
                 old_string="    assert add(1, 2) == 3\n", new_string="    pass\n")
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    (event,) = guard_events()
    assert event["tool"] == "Edit" and event["path"] == CALC_PATH and event["category"] == "test_weakening"
    assert event.get("command") is None


# --------------------------------------------------------------------------- TC-51


def test_tc51_twenty_passing_calls_write_nothing(project, session):
    write_tasks(project, t1="In Progress")
    requirements = project / "specs" / "calc" / "requirements.md"
    line = "- 1.1 WHEN ... THE SYSTEM SHALL subtract\n"
    for n in range(5):
        assert pre(project, session, "Edit", agent_id=AGENT, file_path=str(project / "calc.py"),
                   old_string="a + b", new_string=f"a + b  # {n}") is None
        assert run_bash(project, session, "sed -i 's/add(1, 2)/add(2, 1)/' tests/test_calc.py", agent_id=AGENT).post is None
        git(project, "checkout", "-q", "--", CALC_PATH)
        assert pre(project, session, "Edit", agent_id=AGENT, file_path=str(requirements),
                   old_string=line, new_string=line + f"- 1.{n + 2} WHEN more THE SYSTEM SHALL add\n") is None
        git(project, "checkout", "-q", "--", "specs")
        assert run_bash(project, session, f"echo '# {n}' >> calc.py", agent_id=AGENT).post is None
    assert guard_events() == []
    assert not log_file().exists()


# --------------------------------------------------------------------------- TC-52


def test_tc52_the_log_is_private_and_outside_the_project(project, session):
    from claude_kiro.analytics import store

    before = git(project, "status", "--porcelain", "--ignored")
    pre(project, session, "Bash", command=RM)
    pre(project, session, "Bash", agent_id=AGENT, command=RM)
    path = log_file()
    assert path.exists()
    assert path.parent == store.data_dir()
    assert project not in path.parents
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert git(project, "status", "--porcelain", "--ignored") == before
    assert not list(project.rglob("guard-events*"))


# --------------------------------------------------------------------------- TC-53


def test_tc53_a_log_near_the_limit_rotates_once(project, session):
    from claude_kiro.analytics import guardlog

    limit = guardlog.GUARD_LOG_LIMIT
    path = log_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    skeleton = {"v": 1, "ts": "2026-10-12T09:00:00Z", "sessionId": "old", "agentId": None, "agentType": None,
                "caller": "task_agent", "tool": "Bash", "decision": "deny", "category": "test_delete", "reason": "",
                "path": "tests/old.py", "command": "rm tests/old.py", "family": "rm", "toolUseId": "toolu_old"}
    filler = limit - 50 - len(json.dumps(skeleton)) - 1
    planted = json.dumps({**skeleton, "reason": "x" * filler}) + "\n"
    assert len(planted.encode()) == limit - 50
    path.write_text(planted)

    for _ in range(2):
        assert pre(project, session, "Bash", agent_id=AGENT, command=RM) is not None
    rotated = path.with_name("guard-events.1.jsonl")
    assert rotated.exists()
    assert rotated.read_text().startswith(planted)  # the old content, plus at most the first new event
    assert path.exists() and "toolu_old" not in path.read_text()
    newest = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert newest and newest[-1]["sessionId"] == session
    largest_event = max(len(line) for line in path.read_text().splitlines())
    assert path.stat().st_size + rotated.stat().st_size <= 2 * limit + largest_event + 1
    assert path.stat().st_size < limit


# --------------------------------------------------------------------------- TC-54


def test_tc54_an_unwritable_data_dir_changes_nothing_the_agent_sees(project, session, tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a regular file where the data dir should be")
    monkeypatch.setenv("CK_DATA_HOME", str(blocker))
    broken = three_calls(project, session)
    assert guard_events_safe() == []
    assert blocker.read_text() == "a regular file where the data dir should be"

    git(project, "checkout", "-q", "--", ".")
    monkeypatch.delenv("CK_DATA_HOME")
    working = three_calls(project, session + "-b")
    assert len(guard_events()) == 3
    assert broken == working


def guard_events_safe():
    """guard_events() when the log path cannot exist: an empty list, not an error."""
    try:
        return guard_events()
    except (OSError, NotADirectoryError):
        return []


def test_tc54_the_cli_output_is_identical_with_an_unwritable_data_dir(project, session, tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    payload = hook_input(project, session, "Bash", agent_id=AGENT, tool_use_id="toolu_tc54", command=RM)
    good = os.environ.copy()
    bad = {**good, "CK_DATA_HOME": str(blocker)}
    one = cli_hook("pre-tool-guard", payload, env=good, cwd=project)
    two = cli_hook("pre-tool-guard", payload, env=bad, cwd=project)
    assert one.returncode == 0 and two.returncode == 0, two.stderr
    assert one.stdout and one.stdout == two.stdout

    out = cli_cycle(project, session, SED_WEAKEN, post_env=bad)
    assert out.post.returncode == 0, out.post.stderr
    assert cli_message(out.post)


# --------------------------------------------------------------------------- TC-55

PLANTED = {
    "command": "rm vortexion-ledger-internal/notes.py",
    "path": "tests/test_payroll_vortexion.py",
    "reason": "quarterly payroll vortexion reconciliation failed",
}


def planted_world(tmp_path):
    home, _terms = seeded_home(tmp_path)
    world = World(tmp_path, home)
    write_guard_log(world.env, [{"command": PLANTED["command"], "path": PLANTED["path"], "reason": PLANTED["reason"]}])
    return world


@pytest.mark.parametrize("key", ["command", "path", "reason"])
def test_tc55_a_note_naming_a_guard_log_value_is_refused(tmp_path, key):
    world = planted_world(tmp_path)
    world.collect("--run", SEEDED_RUN_IDS[2])
    assert world.export("--yes").exit_code == 0
    world.collect("--run", SEEDED_RUN_IDS[0])
    fake_claude_drafter(world.bin, f"Resolver needed 3 rounds on {PLANTED[key]} lockfile")
    code, text = world.export_tty("--note", script=[(r"(?i)accept", "a\n")])
    assert code == 0, text
    assert HIT_WORDS.search(text), text
    assert world.exported_notes() == [], text


def test_tc55_the_same_flow_without_a_planted_value_passes(tmp_path):
    world = planted_world(tmp_path)
    world.collect("--run", SEEDED_RUN_IDS[2])
    assert world.export("--yes").exit_code == 0
    world.collect("--run", SEEDED_RUN_IDS[0])
    fake_claude_drafter(world.bin, "Resolver needed 3 rounds on a lockfile")
    code, text = world.export_tty("--note", script=[(r"(?i)accept", "a\n")])
    assert code == 0, text
    assert len(world.exported_notes()) == 1, text


def test_tc55_export_and_submit_refuse_a_record_carrying_a_guard_log_value(tmp_path):
    # The machine label is the free-text slot a record carries; a slug-shaped confidential value in the log must block it.
    world = planted_world(tmp_path)
    write_guard_log(world.env, [{"command": "vortexion-ledger-internal", "path": "vortexion-ledger-internal",
                                 "reason": "vortexion-ledger-internal"}])
    world.collect("--all")
    set_label = world.ck("agents", "set", "run.machine_label", "vortexion-ledger-internal")
    assert set_label.exit_code == 0, set_label.output
    out = world.export("--yes")
    assert out.exit_code != 0, out.output
    assert world.exported_json() == [] and world.exported_notes() == []
    sub = world.submit("--yes")
    assert sub.exit_code != 0, sub.output
    assert world.pushed_batches() == []


def test_tc55_ck_vocabulary_in_the_log_does_not_refuse(tmp_path):
    world = planted_world(tmp_path)
    write_guard_log(world.env, [{"command": "sed -i s/a/b/ tests/x.py", "path": "tests/x.py", "reason": "[ck guard] Denied: tests"}])
    world.collect("--all")
    out = world.export("--yes")
    assert out.exit_code == 0, out.output
    assert world.exported_json()


# --------------------------------------------------------------------------- TC-70 (guard half)


def test_tc70_guard_post_check_and_log_make_no_network_call(project, session, no_network):
    three_calls(project, session)
    git(project, "checkout", "-q", "--", ".")
    cli_cycle(project, session + "-cli", SED_WEAKEN)
    pre(project, session, "Bash", command=RM)
    assert len(guard_events()) >= 4
    assert not Path(no_network).exists() or Path(no_network).read_text() == ""
