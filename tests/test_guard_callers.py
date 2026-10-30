"""Caller classification through the real pre-tool guard (prompt-free-runs TC-20..TC-28, TC-72, TC-74, TC-86, TC-89).

Inputs are the recorded real Claude Code hook inputs of tests/fixtures/hook_inputs (fields rewritten to the `project`
repo). Environments are the recorded headless env and the env `ck run --dry-run --json` reports for its child. The hook
is the real `execute_hook("pre-tool-guard", ...)`; project modules are imported inside test bodies.
"""

import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import git, write_tasks
from guard_helpers import CK, ck_json, ck_run_cmd, claimed_task_worktree, decision, fake_sandbox_tools
from test_guard import REQ_OLD, TP_OLD, write_test_plan

FIXTURES = Path(__file__).parent / "fixtures" / "hook_inputs"
SUMMARY = json.loads((FIXTURES / "capture-summary.json").read_text())
WORKFLOW_HAS_AGENT_ID = SUMMARY["workflowAgentHasAgentId"]
NO_MARKER_REASON = "capture-summary.json: Workflow agents carry agent_id, so the fallback marker is not built"
needs_marker = pytest.mark.skipif(WORKFLOW_HAS_AGENT_ID, reason=NO_MARKER_REASON)

ASSERTION = "    assert add(1, 2) == 3\n"
WEAKEN = dict(old_string=ASSERTION, new_string="    pass\n")
ENV_KEYS = ("CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_EFFORT")


# --------------------------------------------------------------------------- helpers


def recorded(name, project, session, tool, **tool_input):
    """A recorded real hook input with its fields rewritten to this repo, session and tool call."""
    data = json.loads((FIXTURES / f"{name}-pretooluse.json").read_text())
    data = copy.deepcopy(data)
    data["cwd"] = str(project)
    data["session_id"] = session
    data["transcript_path"] = str(Path(str(project)) / ".claude" / "transcript.jsonl")
    data["tool_name"] = tool
    data["tool_input"] = tool_input
    data.pop("scratchpad_dir", None)
    return data


def fire(data):
    from claude_kiro.cli.runner import execute_hook

    return execute_hook("pre-tool-guard", data)


def headless_env(monkeypatch):
    """The recorded `claude -p` hook environment (only the Claude Code markers; placeholders and paths are not applied)."""
    env = json.loads((FIXTURES / "headless-env.json").read_text())
    for key in ENV_KEYS:
        if key in env and env[key] != "<redacted>":
            monkeypatch.setenv(key, env[key])
    assert os.environ["CLAUDE_CODE_ENTRYPOINT"].startswith("sdk")


def ck_run_child_env(project, tmp_path, monkeypatch):
    """Apply the environment `ck run --dry-run --json` reports for its child (CK_*/CLAUDE_* markers, not directories)."""
    bin_dir = fake_sandbox_tools(tmp_path / "bin")
    env = {k: v for k, v in os.environ.items() if k not in ("CK_RUN_HEADLESS", "CLAUDE_CODE_ENTRYPOINT")}
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    done = ck_run_cmd(project, "run", "calc", "--dry-run", "--json", env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    child = json.loads(done.stdout)["env"]
    assert child.get("CK_RUN_HEADLESS") == "1", child
    for key, value in child.items():
        if key.startswith(("CK_", "CLAUDE_")) and not key.endswith(("_HOME", "_DIR")) and isinstance(value, str):
            monkeypatch.setenv(key, value)
    assert os.environ["CK_RUN_HEADLESS"] == "1"


def reason_of(result):
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def started(project, **guard):
    """Implementation started (Task 2 In Progress), a requirements.md and a test-plan.md with real text, committed."""
    write_tasks(project, t2="In Progress")
    (project / "specs/calc/requirements.md").write_text("# Feature: Calc\n\n" + REQ_OLD)
    write_test_plan(project)
    ck_json(project, guard={"tests": "ask", "requirements": "ask", "test_plan": "ask", **guard})
    git(project, "add", "-A")
    git(project, "commit", "-qm", "started")
    return project


def weaken_edit(project, session, name="agent-tool-subagent", path=None, tool="Edit"):
    path = str(path or project / "tests/test_calc.py")
    if tool == "Edit":
        return recorded(name, project, session, "Edit", file_path=path, **WEAKEN)
    if tool == "MultiEdit":
        return recorded(name, project, session, "MultiEdit", file_path=path, edits=[WEAKEN])
    return recorded(name, project, session, "Write", file_path=path, content="from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")


def begin_marker(project, session):
    """Main session runs the real Bash call `ck guard workflow-begin calc` through the hook (marker-built path only)."""
    data = recorded("main-session", project, session, "Bash", command="ck guard workflow-begin calc")
    assert fire(data) is None


def end_marker(project, session):
    assert fire(recorded("main-session", project, session, "Bash", command="ck guard workflow-end")) is None


def workflow_input(project, session, path, **kw):
    """The recorded Workflow-agent input; with the marker built it has no agent_id (as the capture recorded)."""
    data = recorded("workflow-agent", project, session, "Edit", file_path=str(path), **WEAKEN)
    if not WORKFLOW_HAS_AGENT_ID:
        data.pop("agent_id", None)
    return data


# --------------------------------------------------------------------------- TC-20..TC-22


def test_tc20_agent_tool_subagent_is_denied(project, session):
    ck_json(project, guard={"tests": "ask"})
    result = fire(weaken_edit(project, session))
    assert decision(result) == "deny"


def test_tc21_headless_env_is_denied(project, session, monkeypatch):
    ck_json(project, guard={"tests": "ask"})
    data = weaken_edit(project, session, name="main-session")
    assert "agent_id" not in data
    headless_env(monkeypatch)
    assert decision(fire(data)) == "deny"


def test_tc21_ck_run_headless_flag_is_denied(project, session, monkeypatch):
    ck_json(project, guard={"tests": "ask"})
    data = weaken_edit(project, session, name="main-session")
    monkeypatch.setenv("CK_RUN_HEADLESS", "1")
    assert decision(fire(data)) == "deny"


def test_tc21_ck_run_dry_run_child_env_is_denied(project, session, tmp_path, monkeypatch):
    ck_json(project, guard={"tests": "ask"})
    git(project, "add", "-A")
    git(project, "commit", "-qm", "ck")
    data = weaken_edit(project, session, name="main-session")
    ck_run_child_env(project, tmp_path, monkeypatch)
    assert decision(fire(data)) == "deny"


def test_tc21_near_miss_env_is_the_main_session(project, session, monkeypatch):
    ck_json(project, guard={"tests": "ask"})
    monkeypatch.setenv("CK_RUN_HEADLESS", "0")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    assert decision(fire(weaken_edit(project, session, name="main-session"))) == "ask"


@pytest.mark.parametrize("mode", ["ask", "deny"])
def test_tc22_main_session_gets_the_configured_mode(project, session, mode):
    ck_json(project, guard={"tests": mode})
    result = fire(weaken_edit(project, session, name="main-session"))
    assert decision(result) == mode
    assert "removes 1 assertion" in reason_of(result)


# --------------------------------------------------------------------------- TC-23


def test_tc23_capture_summary_records_the_answer():
    assert isinstance(SUMMARY["workflowAgentHasAgentId"], bool)
    assert isinstance(SUMMARY["agentToolSubagentHasAgentId"], bool)
    recorded_input = json.loads((FIXTURES / "workflow-agent-pretooluse.json").read_text())
    assert ("agent_id" in recorded_input) == SUMMARY["workflowAgentHasAgentId"]
    agent_input = json.loads((FIXTURES / "agent-tool-subagent-pretooluse.json").read_text())
    assert ("agent_id" in agent_input) == SUMMARY["agentToolSubagentHasAgentId"]


@pytest.mark.skipif(not WORKFLOW_HAS_AGENT_ID, reason="capture-summary.json: Workflow agents lack agent_id, the marker path applies")
def test_tc23_workflow_agent_with_agent_id_is_denied(project, session):
    from guard_helpers import claimed_task_worktree

    started(project)
    wt = claimed_task_worktree(project, "calc", 1)
    data = weaken_edit(project, session, name="workflow-agent", path=wt / "tests/test_calc.py")
    assert data["cwd"] == str(project)
    assert decision(fire(data)) == "deny"


@needs_marker
def test_tc23_marker_active_denies_then_ends_then_mode(project, session):
    started(project)
    wt = claimed_task_worktree(project, "calc", 1)
    target = wt / "tests/test_calc.py"
    begin_marker(project, session)
    assert decision(fire(workflow_input(project, session, target))) == "deny"
    end_marker(project, session)
    assert decision(fire(workflow_input(project, session, target))) == "ask"


@needs_marker
def test_tc23_marker_of_another_session_never_applies(project, session):
    started(project)
    begin_marker(project, session)
    other = workflow_input(project, session + "-other", project / "tests/test_calc.py")
    assert decision(fire(other)) == "ask"


@pytest.mark.skipif(os.environ.get("CK_LIVE_CLAUDE") != "1", reason="live capture needs CK_LIVE_CLAUDE=1 (real, paid Claude Code session)")
def test_tc23_live_workflow_agent_agent_id(tmp_path):
    """Re-capture a one-agent Workflow under `claude -p` and compare agent_id presence with capture-summary.json."""
    root = tmp_path / "live"
    out = tmp_path / "recorded"
    (root / ".claude").mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    hook = f"{sys.executable} {FIXTURES / 'record_hook.py'} {out}"
    (root / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Edit|Write|MultiEdit|Bash", "hooks": [{"type": "command", "command": hook}]}]}}))
    prompt = ("Run a Workflow with exactly one agent whose only job is to run the Bash command `echo CAPTURE_WORKFLOW`. "
              "Then finish.")
    done = subprocess.run(["claude", "-p", "--permission-mode", "bypassPermissions", prompt], cwd=root,
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout + done.stderr
    calls = [json.loads(p.read_text())["input"] for p in sorted(out.glob("PreToolUse-*.json"))]
    agent_calls = [c for c in calls if "CAPTURE_WORKFLOW" in json.dumps(c.get("tool_input")) and c.get("agent_type") != "main"]
    assert agent_calls, calls
    assert all(("agent_id" in c) == WORKFLOW_HAS_AGENT_ID for c in agent_calls)


# --------------------------------------------------------------------------- TC-24


@pytest.mark.parametrize("agent_id", ["", None, 42, [], {"id": 1}])
def test_tc24_unusable_agent_id_is_the_main_session(project, session, agent_id):
    ck_json(project, guard={"tests": "ask"})
    data = weaken_edit(project, session, name="main-session")
    data["agent_id"] = agent_id
    assert decision(fire(data)) == "ask"


def test_tc24_malformed_inputs_never_raise(project, session):
    ck_json(project, guard={"tests": "ask"})
    base = weaken_edit(project, session, name="agent-tool-subagent")
    cases = []
    no_cwd = copy.deepcopy(base)
    del no_cwd["cwd"]
    cases.append(no_cwd)
    null_input = copy.deepcopy(base)
    null_input["tool_input"] = None
    cases.append(null_input)
    unknown = copy.deepcopy(base)
    unknown["foo"] = {"bar": [1, 2, {"baz": None}]}
    cases.append(unknown)
    for case in cases:
        result = fire(case)
        assert result is None or decision(result) in ("ask", "deny")
    assert fire(null_input) is None
    assert decision(fire(unknown)) == "deny"


@pytest.mark.parametrize("stdin", ["{}", "not json", "", "[]", "null"])
def test_tc24_cli_hook_exits_zero(project, stdin):
    done = subprocess.run([*CK, "--hook", "pre-tool-guard"], input=stdin, capture_output=True, text=True, cwd=project)
    assert done.returncode == 0, done.stdout + done.stderr


# --------------------------------------------------------------------------- TC-25, TC-26


@pytest.mark.parametrize("tool", ["Edit", "MultiEdit", "Write"])
def test_tc25_tc26_task_agent_test_weakening_is_denied(project, session, tool):
    started(project)
    result = fire(weaken_edit(project, session, tool=tool))
    assert decision(result) == "deny"
    reason = reason_of(result)
    assert "tests/test_calc.py" in reason
    assert ("removes 1 assertion" in reason) if tool != "Write" else ("test" in reason)


def test_tc25_tc26_task_agent_requirement_reword_is_denied(project, session):
    started(project)
    path = project / "specs/calc/requirements.md"
    data = recorded("agent-tool-subagent", project, session, "Edit", file_path=str(path), old_string=REQ_OLD,
                    new_string=REQ_OLD.replace("subtract", "subtract or return 0"))
    result = fire(data)
    assert decision(result) == "deny"
    assert "specs/calc/requirements.md" in reason_of(result)
    assert "requirement" in reason_of(result)


def test_tc25_tc26_redteam_dotdot_and_symlink_paths_to_frozen_spec_are_denied(project, session):
    started(project)
    reword = dict(old_string=REQ_OLD, new_string=REQ_OLD.replace("subtract", "subtract or return 0"))
    dotdot = project / "specs" / "calc" / ".." / "calc" / "requirements.md"
    result = fire(recorded("agent-tool-subagent", project, session, "Edit", file_path=str(dotdot), **reword))
    assert decision(result) == "deny"
    (project / "specs_link").symlink_to(project / "specs")
    linked = project / "specs_link" / "calc" / "requirements.md"
    result = fire(recorded("agent-tool-subagent", project, session, "Edit", file_path=str(linked), **reword))
    assert decision(result) == "deny"


def test_tc25_tc26_redteam_symlink_into_another_checkout_is_denied(project, session, tmp_path):
    started(project)
    other = tmp_path / "other-checkout"
    git(project, "worktree", "add", "-q", "-b", "other-side", str(other))
    (other / "r.md").symlink_to(project / "specs" / "calc" / "requirements.md")
    reword = dict(old_string=REQ_OLD, new_string=REQ_OLD.replace("subtract", "subtract or return 0"))
    result = fire(recorded("agent-tool-subagent", other, session, "Edit", file_path=str(other / "r.md"), **reword))
    assert decision(result) == "deny"
    assert "requirements.md" in reason_of(result)


def test_tc25_tc26_task_agent_test_plan_row_deletion_is_denied(project, session):
    started(project)
    path = project / "specs/calc/test-plan.md"
    deleted = TP_OLD.replace("| TC-2 | 1.1 | add(-1, 1) returns 0 |\n", "")
    data = recorded("agent-tool-subagent", project, session, "Edit", file_path=str(path), old_string=TP_OLD, new_string=deleted)
    result = fire(data)
    assert decision(result) == "deny"
    assert "specs/calc/test-plan.md" in reason_of(result)
    assert "test case" in reason_of(result)


def test_tc25_kind_off_is_silent(project, session):
    started(project, tests="off", requirements="off", test_plan="off")
    assert fire(weaken_edit(project, session)) is None
    req = recorded("agent-tool-subagent", project, session, "Edit", file_path=str(project / "specs/calc/requirements.md"),
                   old_string=REQ_OLD, new_string=REQ_OLD.replace("subtract", "multiply"))
    assert fire(req) is None
    plan = recorded("agent-tool-subagent", project, session, "Edit", file_path=str(project / "specs/calc/test-plan.md"),
                    old_string=TP_OLD, new_string=TP_OLD.replace("returns 3", "returns 4"))
    assert fire(plan) is None


# --------------------------------------------------------------------------- TC-27, TC-28


def spec_edits(project, session, name):
    """Rewording a requirement, deleting the Decided line, replacing the whole requirements.md, rewording and deleting a
    test-plan row: the edits a main session makes to a started spec."""
    req = str(project / "specs/calc/requirements.md")
    plan = str(project / "specs/calc/test-plan.md")
    return [
        recorded(name, project, session, "Edit", file_path=req, old_string=REQ_OLD, new_string=REQ_OLD.replace("subtract", "subtract or return 0")),
        recorded(name, project, session, "Edit", file_path=req, old_string=REQ_OLD, new_string=REQ_OLD.replace("  (Decided 2026-10-12.)\n", "")),
        recorded(name, project, session, "Write", file_path=req, content="# Feature: Calc\n- 1.1 anything\n"),
        recorded(name, project, session, "Edit", file_path=plan, old_string=TP_OLD, new_string=TP_OLD.replace("returns 3", "returns a number")),
        recorded(name, project, session, "Edit", file_path=plan, old_string=TP_OLD,
                 new_string=TP_OLD.replace("| TC-2 | 1.1 | add(-1, 1) returns 0 |\n", "")),
    ]


def test_tc27_main_session_spec_edits_pass_with_no_ask(project, session):
    started(project)
    for data in spec_edits(project, session, "main-session"):
        assert fire(data) is None, data["tool_input"]


def test_tc28_task_agent_spec_edits_are_denied(project, session):
    started(project)
    for data in spec_edits(project, session, "agent-tool-subagent"):
        assert decision(fire(data)) == "deny", data["tool_input"]


def test_tc28_add_only_edits_from_a_task_agent_pass(project, session):
    started(project)
    req = project / "specs/calc/requirements.md"
    plan = project / "specs/calc/test-plan.md"
    added_req = REQ_OLD.replace("\n\n### Story 2", "\n- 1.2 WHEN asked THE SYSTEM SHALL\n  multiply.\n\n### Story 2")
    added_row = TP_OLD + "| TC-3 | 1.2 | add(0, 0) returns 0 |\n"
    assert fire(recorded("agent-tool-subagent", project, session, "Edit", file_path=str(req), old_string=REQ_OLD, new_string=added_req)) is None
    assert fire(recorded("agent-tool-subagent", project, session, "Edit", file_path=str(plan), old_string=TP_OLD, new_string=added_row)) is None
    assert fire(recorded("agent-tool-subagent", project, session, "Write", file_path=str(req), content=req.read_text() + "- 1.3 SHALL divide.\n")) is None
    assert fire(recorded("agent-tool-subagent", project, session, "Write", file_path=str(plan), content=plan.read_text() + "\n## Notes\nNew.\n")) is None


# --------------------------------------------------------------------------- TC-72, TC-74


@pytest.mark.parametrize("mode", ["ask", "deny"])
@pytest.mark.parametrize("hook_cwd", ["main", "worktree"])
def test_tc72_main_session_in_a_task_worktree_gets_the_configured_mode(project, session, mode, hook_cwd):
    started(project, tests=mode)
    wt = claimed_task_worktree(project, "calc", 1)
    target = wt / "tests/test_calc.py"
    assert target.exists()
    cwd = project if hook_cwd == "main" else wt
    edit = recorded("main-session", cwd, session, "Edit", file_path=str(target), **WEAKEN)
    assert decision(fire(edit)) == mode
    rm = recorded("main-session", cwd, session, "Bash", command=f"rm {target}")
    assert decision(fire(rm)) == mode


@pytest.mark.parametrize("tool", ["Edit", "MultiEdit", "Write"])
def test_tc74_task_agent_absolute_path_into_a_task_worktree_is_denied(project, session, tool):
    started(project)
    wt = claimed_task_worktree(project, "calc", 1)
    data = weaken_edit(project, session, path=wt / "tests/test_calc.py", tool=tool)
    assert data["cwd"] == str(project)
    result = fire(data)
    assert decision(result) == "deny"
    assert "tests/test_calc.py" in reason_of(result)
    if tool != "Write":
        assert "removes 1 assertion" in reason_of(result)


# --------------------------------------------------------------------------- TC-86


def revise_edits(project, session, name, agent_id_removed=False):
    req = str(project / "specs/calc/requirements.md")
    plan = str(project / "specs/calc/test-plan.md")
    out = [
        recorded(name, project, session, "Edit", file_path=req, old_string=REQ_OLD, new_string=REQ_OLD.replace("subtract", "subtract or return 0")),
        recorded(name, project, session, "Edit", file_path=plan, old_string=TP_OLD,
                 new_string=TP_OLD.replace("| TC-2 | 1.1 | add(-1, 1) returns 0 |\n", "")),
    ]
    if agent_id_removed:
        for item in out:
            item.pop("agent_id", None)
    return out


def add_only_edits(project, session, name, agent_id_removed=False):
    req = str(project / "specs/calc/requirements.md")
    plan = str(project / "specs/calc/test-plan.md")
    added_req = REQ_OLD.replace("\n\n### Story 2", "\n- 1.2 WHEN asked THE SYSTEM SHALL\n  multiply.\n\n### Story 2")
    out = [
        recorded(name, project, session, "Edit", file_path=req, old_string=REQ_OLD, new_string=added_req),
        recorded(name, project, session, "Edit", file_path=plan, old_string=TP_OLD, new_string=TP_OLD + "| TC-3 | 1.2 | add(0, 0) returns 0 |\n"),
    ]
    if agent_id_removed:
        for item in out:
            item.pop("agent_id", None)
    return out


def assert_returned_as_decision(result):
    assert decision(result) == "deny"
    reason = reason_of(result)
    assert "escalated" in reason and "decision" in reason, reason
    assert "Do not apply" in reason, reason


def test_tc86_workflow_revise_agent_is_denied_with_decision_reason(project, session):
    started(project)
    if not WORKFLOW_HAS_AGENT_ID:
        begin_marker(project, session)
    for data in revise_edits(project, session, "workflow-agent", agent_id_removed=not WORKFLOW_HAS_AGENT_ID):
        assert_returned_as_decision(fire(data))
    for data in add_only_edits(project, session, "workflow-agent", agent_id_removed=not WORKFLOW_HAS_AGENT_ID):
        assert fire(data) is None


def test_tc86_agent_mode_revise_agent_is_denied_with_decision_reason(project, session):
    started(project)
    for data in revise_edits(project, session, "agent-tool-subagent"):
        assert_returned_as_decision(fire(data))
    for data in add_only_edits(project, session, "agent-tool-subagent"):
        assert fire(data) is None


def test_tc86_handed_off_ck_run_revise_agent_is_denied_with_decision_reason(project, session, tmp_path, monkeypatch):
    started(project)
    ck_run_child_env(project, tmp_path, monkeypatch)
    for data in revise_edits(project, session, "main-session"):
        assert "agent_id" not in data
        assert_returned_as_decision(fire(data))
    for data in add_only_edits(project, session, "main-session"):
        assert fire(data) is None


# --------------------------------------------------------------------------- TC-89 (marker built only)


def marker_file(session):
    from claude_kiro.hooks._shared import run_marker

    data_dir = Path(os.environ["XDG_DATA_HOME"])
    found = sorted(data_dir.rglob(f"workflow-runs/{session}.json"))
    assert found, f"no marker file for {session} under {data_dir} ({run_marker.__name__})"
    return found[0]


def replant(session, age_s):
    """Rewrite the marker's `refreshedAt` to be `age_s` seconds old, in the format the marker already uses."""
    path = marker_file(session)
    data = json.loads(path.read_text())
    old = data["refreshedAt"]
    if isinstance(old, (int, float)):
        data["refreshedAt"] = time.time() - age_s
    else:
        from datetime import datetime, timedelta, timezone

        data["refreshedAt"] = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(data))


@needs_marker
def test_tc89_no_agent_id_call_is_a_task_agent_while_active(project, session):
    started(project)
    begin_marker(project, session)
    assert decision(fire(workflow_input(project, session, project / "tests/test_calc.py"))) == "deny"


@needs_marker
def test_tc89_marker_expiry_after_15_minutes(project, session):
    started(project)
    begin_marker(project, session)
    target = project / "tests/test_calc.py"
    replant(session, 16 * 60)
    assert decision(fire(workflow_input(project, session, target))) == "ask"
    begin_marker(project, session)
    replant(session, 14 * 60)
    assert decision(fire(workflow_input(project, session, target))) == "deny"


@needs_marker
def test_tc89_marker_clear_on_user_prompt_submit(project, session):
    from claude_kiro.cli.runner import execute_hook

    started(project)
    begin_marker(project, session)
    target = project / "tests/test_calc.py"
    assert decision(fire(workflow_input(project, session, target))) == "deny"
    prompt = {"session_id": session, "cwd": str(project), "hook_event_name": "UserPromptSubmit", "prompt": "hello"}
    assert execute_hook("marker-clear", prompt) is None
    assert decision(fire(workflow_input(project, session, target))) == "ask"


@needs_marker
def test_tc89_re_begin_replaces_a_stale_marker(project, session):
    started(project)
    begin_marker(project, session)
    replant(session, 16 * 60)
    begin_marker(project, session)
    assert decision(fire(workflow_input(project, session, project / "tests/test_calc.py"))) == "deny"


@needs_marker
def test_tc89_marker_of_another_session_is_never_cleared_or_read(project, session):
    from claude_kiro.cli.runner import execute_hook

    started(project)
    begin_marker(project, session)
    other = session + "-other"
    prompt = {"session_id": other, "cwd": str(project), "hook_event_name": "UserPromptSubmit", "prompt": "hello"}
    assert execute_hook("marker-clear", prompt) is None
    target = project / "tests/test_calc.py"
    assert decision(fire(workflow_input(project, session, target))) == "deny"
    assert decision(fire(workflow_input(project, other, target))) == "ask"
