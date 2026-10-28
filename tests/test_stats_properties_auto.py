"""P-23: a failing collection never changes what `ck run` does (run analytics, 3.3)."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from stats_helpers import (
    AgentSpec,
    _script,
    ck_sub,
    fake_claude_session,
    fresh_env,
    git_project,
    load_record,
)

pytestmark = pytest.mark.usefixtures("no_network")

RUN_ID = "wf_ck_prop"
SID = "5e55a10e-0000-4000-8000-0000000000d4"

# Injected by patching the fake claude (it runs after the session is written, before claude exits).
CLAUDE_FAULTS = {
    "truncated": "p = session.runs[0].path; t = p.read_text(); p.write_text(t[: len(t) // 2])",
    "progress_object": "session.runs[0].corrupt_format()",
    "transcript_chmod": "session.runs[0].transcript(0).chmod(0)",
    "meta_not_json": "session.runs[0].meta_path(0).write_text('not json {')",
    "session_deleted": "import shutil; shutil.rmtree(session.dir)",
}
ENV_FAULTS = ("store_is_file", "config_unwritable")
FAULTS = [*CLAUDE_FAULTS, *ENV_FAULTS]

PROPERTIES = {"P-23": "test_p23_a_failing_collection_never_changes_ck_run"}

COUNT_LINE = "stats: 1 run file in this session could not be read; retry: ck stats collect --all"


def build(env, fault: str, code: int, disable: bool):
    root = git_project(env.root, None, name="proj")
    (root / "specs" / "demo").mkdir(parents=True)
    (root / "specs" / "demo" / "tasks.md").write_text("# Tasks\n\n### Task 1: a\n**Status:** Not Started\n**Dependencies:** None\n")
    bin_dir = env.root / "bin"
    bin_dir.mkdir()
    start = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    claude = fake_claude_session(bin_dir, [{"kind": "implement", "kwargs": {"run_id": RUN_ID, "start": start}}],
                                 exit_code=code)
    if fault in CLAUDE_FAULTS:
        text = claude.read_text()
        assert 'print("fake claude done")' in text
        claude.write_text(text.replace('print("fake claude done")', CLAUDE_FAULTS[fault] + '\nprint("fake claude done")'))
    for tool in ("bwrap", "socat"):
        _script(bin_dir / tool, "", shebang="/bin/sh")
    full = {**env, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    if fault == "store_is_file":
        (env.xdg / "claude-kiro").write_text("x")
    if fault == "config_unwritable":
        (env.root / "afile").write_text("x")
        full["CK_CONFIG_HOME"] = str(env.root / "afile" / "sub")
    if disable:
        assert ck_sub("agents", "set", "run.analytics", "false", "--project", env=full, cwd=root).exit_code == 0
    return root, full


def run_once(env, fault: str, code: int, disable: bool):
    root, full = build(env, fault, code, disable)
    log = env.root / "run.log"
    out = ck_sub("run", "demo", "--session-id", SID, "--log", str(log), env=full, cwd=root)
    return root, log, out


def norm(text: str, root: Path, log: Path, env) -> str:
    text = text.replace(str(log), "<LOG>")
    for r in (root.resolve(), root):
        text = text.replace(str(r), "<PROJECT>")
    for r in (Path(env.root).resolve(), Path(env.root)):
        text = text.replace(str(r), "<ROOT>")
    return "\n".join(l for l in text.splitlines() if not l.startswith("stats:") and "ck stats collect" not in l)


@settings(max_examples=25, derandomize=True, database=None, deadline=None, suppress_health_check=list(HealthCheck))
@given(fault=st.sampled_from(FAULTS), code=st.sampled_from([0, 7]))
def test_p23_a_failing_collection_never_changes_ck_run(fault, code):
    with fresh_env() as env_a, fresh_env() as env_b:
        root_a, log_a, a = run_once(env_a, fault, code, disable=False)
        root_b, log_b, b = run_once(env_b, fault, code, disable=True)
        assert a.exit_code == code and b.exit_code == code, (a.output, b.output)
        assert "Traceback" not in a.output and "Traceback" not in log_a.read_text()
        assert norm(a.stdout, root_a, log_a, env_a) == norm(b.stdout, root_b, log_b, env_b)
        assert norm(log_a.read_text(), root_a, log_a, env_a) == norm(log_b.read_text(), root_b, log_b, env_b)
        a_log_text = log_a.read_text()
        assert not any("stats" in l for l in b.stdout.splitlines()) and "stats:" not in log_b.read_text()
        if fault == "truncated":
            assert [l for l in a.stdout.splitlines() if l.startswith("stats:")] == [COUNT_LINE]
            assert [l for l in a_log_text.splitlines() if l.startswith("stats:")] == [COUNT_LINE]
            assert RUN_ID not in "".join(l for l in a.stdout.splitlines() if l.startswith("stats:"))
            collect_log = (env_a.xdg / "claude-kiro" / "collect.log").read_text()
            assert re.search(r"^\S+ - unrecognised$", collect_log, re.M), collect_log
        elif fault in ("transcript_chmod", "meta_not_json"):
            assert any(RUN_ID in l and "collected" in l for l in a_log_text.splitlines()), a_log_text
            assert load_record(env_a.xdg, RUN_ID)["runId"] == RUN_ID
        elif fault in ("progress_object", "store_is_file"):
            assert f"ck stats collect --run {RUN_ID}" in a_log_text
