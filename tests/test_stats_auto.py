"""Automatic collection after `ck run` (run analytics, Story 3): TC-21..TC-25, TC-27, TC-84.

`ck run` is a real subprocess with a fake `claude` (and fake `bwrap`/`socat`, so the sandbox check passes) on PATH.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stats_helpers import (
    AgentSpec,
    FakeClaudeHome,
    _script,
    ck_sub,
    fake_claude_session,
    fresh_env,
    git_project,
    load_record,
    store_files,
)

pytestmark = pytest.mark.usefixtures("no_network")

CK_RUN_A = "wf_ck_auto_a"
CK_RUN_B = "wf_ck_auto_b"
FOREIGN = "wf_foreign_auto"
SID = "5e55a10e-0000-4000-8000-0000000000c3"


def soon() -> str:
    """A start time a little ahead of now, so the run falls inside ck's `since` window."""
    return (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_project(env) -> Path:
    root = git_project(env.root, None, name="proj")
    (root / "specs" / "demo").mkdir(parents=True)
    (root / "specs" / "demo" / "tasks.md").write_text("# Tasks\n\n### Task 1: a\n**Status:** Not Started\n**Dependencies:** None\n")
    return root


def implement(run_id: str) -> dict:
    return {"kind": "implement", "kwargs": {"run_id": run_id, "start": soon()}}


def foreign(run_id: str) -> dict:
    return {"kind": "foreign", "kwargs": {"run_id": run_id, "start": soon()}}


def install_fakes(env, runs, exit_code: int = 0) -> dict:
    """Fake claude (writing `runs` while `ck run` waits), bwrap and socat on PATH; returns the env for ck_sub."""
    bin_dir = env.root / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake_claude_session(bin_dir, runs, exit_code=exit_code)
    for tool in ("bwrap", "socat"):
        _script(bin_dir / tool, "", shebang="/bin/sh")
    return {**env, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def ck_run(project: Path, env, log: Path):
    return ck_sub("run", "demo", "--session-id", SID, "--log", str(log), env=env, cwd=project)


def normalise(text: str, project: Path, *logs: Path) -> str:
    for i, log in enumerate(logs):
        text = text.replace(str(log), "<LOG>")
    return text.replace(str(project), "<PROJECT>")


def without_stats_lines(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.startswith("stats:") and "ck stats collect" not in l)


def stats_lines(text: str):
    return [l for l in text.splitlines() if l.startswith("stats:")]


def test_tc21_two_ck_runs_are_collected_and_the_foreign_run_is_not():
    with fresh_env() as env:
        project = make_project(env)
        full = install_fakes(env, [implement(CK_RUN_A), implement(CK_RUN_B), foreign(FOREIGN)])
        log = env.root / "run.log"
        out = ck_run(project, full, log)
        assert out.exit_code == 0, out.output + log.read_text()
        assert load_record(env.xdg, CK_RUN_A)["runId"] == CK_RUN_A
        assert load_record(env.xdg, CK_RUN_B)["runId"] == CK_RUN_B
        text = log.read_text()
        for rid in (CK_RUN_A, CK_RUN_B):
            lines = [l for l in text.splitlines() if f"stats: {rid} collected -> " in l]
            assert len(lines) == 1, (rid, text)
        assert FOREIGN not in text and FOREIGN not in out.output
        assert not any(FOREIGN in p.name for p in store_files(env.xdg))


@pytest.mark.parametrize("code", [0, 7])
def test_tc22_a_failing_collection_changes_nothing_but_its_own_lines(code):
    with fresh_env() as env:
        project = make_project(env)
        full = install_fakes(env, [implement(CK_RUN_A)], exit_code=code)
        blocker = env.root / "not-a-dir"
        blocker.write_text("x")
        full["XDG_DATA_HOME"] = str(blocker)
        a_log, b_log = env.root / "a.log", env.root / "b.log"
        a = ck_run(project, full, a_log)
        assert a.exit_code == code, a.output
        assert ck_sub("agents", "set", "run.analytics", "false", env=full, cwd=project).exit_code == 0
        b = ck_run(project, full, b_log)
        assert b.exit_code == code, b.output
        # the failing run reports the failure and the retry command; the disabled run says nothing about stats
        assert f"ck stats collect --run {CK_RUN_A}" in a_log.read_text()
        assert "ck stats collect" not in b_log.read_text() and "ck stats collect" not in b.output
        assert "Traceback" not in a.output
        # everything else is identical
        assert without_stats_lines(normalise(a.stdout, project, a_log)) == without_stats_lines(normalise(b.stdout, project, b_log))
        assert without_stats_lines(normalise(a_log.read_text(), project, a_log)) == without_stats_lines(normalise(b_log.read_text(), project, b_log))


def test_tc23_turned_off_stores_nothing_but_manual_collect_still_works():
    with fresh_env() as env:
        project = make_project(env)
        full = install_fakes(env, [implement(CK_RUN_A), implement(CK_RUN_B)])
        assert ck_sub("agents", "set", "run.analytics", "false", env=full, cwd=project).exit_code == 0
        log = env.root / "run.log"
        out = ck_run(project, full, log)
        assert out.exit_code == 0, out.output
        assert store_files(env.xdg) == []
        assert not stats_lines(log.read_text()) and "collected" not in log.read_text()
        manual = ck_sub("stats", "collect", "--run", CK_RUN_A, env=full, cwd=project)
        assert manual.exit_code == 0, manual.output
        assert load_record(env.xdg, CK_RUN_A)["runId"] == CK_RUN_A
        before = store_files(env.xdg)
        auto = ck_sub("stats", "collect", "--run", CK_RUN_B, "--auto", env=full, cwd=project)
        assert auto.exit_code == 0, auto.output
        assert "auto-collect is off" in auto.output
        assert store_files(env.xdg) == before
        assert not (env.xdg / "claude-kiro" / "runs" / f"{CK_RUN_B}.json").exists()


def test_tc24_collection_is_on_when_nothing_is_set():
    with fresh_env() as env:
        project = make_project(env)
        full = install_fakes(env, [implement(CK_RUN_A)])
        shown = ck_sub("agents", "--json", env=full, cwd=project)
        assert shown.exit_code == 0, shown.output
        assert json.loads(shown.stdout)["roles"]["run"]["analytics"] is True
        out = ck_run(project, full, env.root / "run.log")
        assert out.exit_code == 0, out.output
        assert load_record(env.xdg, CK_RUN_A)["runId"] == CK_RUN_A


def test_tc25_a_non_boolean_setting_is_rejected_and_the_config_is_untouched():
    with fresh_env() as env:
        project = make_project(env)
        assert ck_sub("agents", "set", "run.analytics", "true", env=env, cwd=project).exit_code == 0
        assert ck_sub("agents", "set", "run.analytics", "true", "--project", env=env, cwd=project).exit_code == 0
        glob, proj = env.config / "config.json", project / "specs" / "ck.json"
        before = (glob.read_bytes(), proj.read_bytes())
        for extra in ((), ("--project",)):
            out = ck_sub("agents", "set", "run.analytics", "maybe", *extra, env=env, cwd=project)
            assert out.exit_code != 0, out.output
            assert "true" in out.output and "false" in out.output
            assert "Traceback" not in out.output
        assert (glob.read_bytes(), proj.read_bytes()) == before


def test_tc27_auto_collect_failure_exits_zero_and_prints_the_retry_command():
    with fresh_env() as env:
        project = make_project(env)
        home = FakeClaudeHome(env.claude)
        home.session(cwd=project).implement_run(CK_RUN_A, start="2026-10-12T09:00:00Z", agents=[AgentSpec("task 1")])
        blocker = env.root / "not-a-dir"
        blocker.write_text("x")
        out = ck_sub("stats", "collect", "--run", CK_RUN_A, "--auto", env={**env, "XDG_DATA_HOME": str(blocker)}, cwd=project)
        assert out.exit_code == 0, out.output
        assert f"ck stats collect --run {CK_RUN_A}" in out.output
        assert f"could not collect {CK_RUN_A}" in out.output
        assert "Traceback" not in out.output


def test_tc84_the_data_dir_is_denied_inside_the_sandbox():
    with fresh_env() as env:
        project = make_project(env)
        full = install_fakes(env, [])
        out = ck_sub("run", "demo", "--dry-run", "--json", env=full, cwd=project)
        assert out.exit_code == 0, out.output
        files = json.loads(out.stdout)["settings"]["sandbox"]["credentials"]["files"]
        data_dir = env.xdg / "claude-kiro"
        modes = {f["path"]: f["mode"] for f in files}
        assert modes.get(str(data_dir)) == "deny" or modes.get(str(data_dir.resolve())) == "deny", modes
        assert modes.get("~/.ssh") == "deny"
