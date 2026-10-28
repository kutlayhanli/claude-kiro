"""Collection health in `ck doctor` (TC-74..TC-76): the real CLI over a fake Claude home, through subprocesses."""

import re

import pytest

from stats_helpers import AgentSpec, FakeClaudeHome, ck_sub, implement_result, TaskSpec

FOREIGN_NAME = "competitor-teardown-q4"


@pytest.fixture
def ck_project(project, tmp_path, monkeypatch):
    """The `project` fixture set up by `ck init`, with a fake Claude home as CLAUDE_CONFIG_DIR."""
    assert ck_sub("init", "--no-allow-workflow", cwd=project).exit_code == 0
    home = FakeClaudeHome(tmp_path / "claude-home")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home.root))
    home.project = project  # type: ignore[attr-defined]
    return home


def _session(home, sid="5e55a10e-0000-4000-8000-0000000000c3"):
    return home.session("-proj", sid, cwd=home.project)


def _implement(session, run_id, start):
    return session.implement_run(
        run_id, start=start, result=implement_result([TaskSpec(task="1")]),
        agents=[AgentSpec("task 1"), AgentSpec("review 1", model="claude-opus-5-5")],
    )


def doctor(home):
    return ck_sub("doctor", cwd=home.project)


def _bullets(output, heading):
    """The bullet lines under a doctor section heading (`Warnings:` or `Issues found:`)."""
    m = re.search(rf"{re.escape(heading)}\n((?:  - .*\n?)*)", output)
    return m.group(1).splitlines() if m else []


# TC-74 (12.1)
def test_doctor_warns_when_the_newest_allowlisted_run_has_an_unrecognised_format(ck_project):
    s = _session(ck_project)
    _implement(s, "wf_older-ok", "2026-10-12T09:00:00Z")
    _implement(s, "wf_newest-bad", "2026-10-14T09:00:00Z").corrupt_format()
    # a still newer run of a non-ck workflow with a bad format: skipped by name, never named
    s.foreign_run("wf_foreign-bad", name=FOREIGN_NAME, start="2026-10-15T09:00:00Z").set(workflowProgress={"not": "a list"})
    out = doctor(ck_project).output
    warnings = "\n".join(_bullets(out, "Warnings:"))
    assert "not recognised" in warnings, out
    assert "ck stats cannot collect it" in warnings, out
    assert FOREIGN_NAME not in out


def test_doctor_is_quiet_about_format_when_the_newest_allowlisted_run_is_fine(ck_project):
    s = _session(ck_project)
    _implement(s, "wf_older-bad", "2026-10-12T09:00:00Z").corrupt_format()
    _implement(s, "wf_newest-ok", "2026-10-14T09:00:00Z")
    assert "not recognised" not in doctor(ck_project).output


# TC-75 (12.2)
def test_doctor_mentions_auto_collect_off_as_information_not_a_warning(ck_project):
    s = _session(ck_project)
    _implement(s, "wf_one", "2026-10-12T09:00:00Z")
    _implement(s, "wf_two", "2026-10-13T09:00:00Z")
    assert ck_sub("stats", "collect", "--all", cwd=ck_project.project).exit_code == 0
    on = doctor(ck_project)
    assert ck_sub("agents", "set", "run.analytics", "false", cwd=ck_project.project).exit_code == 0
    off = doctor(ck_project)
    off_text = off.output.lower()
    assert "automatic run analytics is off" in off_text, off.output
    assert "ck agents set run.analytics true" in off.output
    assert "automatic run analytics is off" not in on.output.lower()
    assert off.exit_code == on.exit_code
    assert _bullets(off.output, "Issues found:") == _bullets(on.output, "Issues found:")
    off_warnings = _bullets(off.output, "Warnings:")
    assert off_warnings == _bullets(on.output, "Warnings:")
    assert not any("analytics" in w.lower() for w in off_warnings)


# TC-76 (12.3)
def test_doctor_counts_finished_runs_not_collected_and_suggests_collect_all(ck_project):
    s = _session(ck_project)
    _implement(s, "wf_a", "2026-10-12T09:00:00Z")
    _implement(s, "wf_b", "2026-10-13T09:00:00Z")
    _implement(s, "wf_c", "2026-10-14T09:00:00Z")
    assert ck_sub("stats", "collect", "--run", "wf_a", cwd=ck_project.project).exit_code == 0
    out = doctor(ck_project).output
    warnings = "\n".join(_bullets(out, "Warnings:"))
    assert re.search(r"\b2 finished ck run(?:s|\(s\))? not collected", warnings), out
    assert "ck stats collect --all" in warnings, out
    assert ck_sub("stats", "collect", "--all", cwd=ck_project.project).exit_code == 0
    after = doctor(ck_project).output
    assert "not collected" not in after
    assert "ck stats collect --all" not in after
