"""Real spend of a run, from its transcripts, and the budget a `ck run` gets.

The workflow's own progress counts each agent's final context size, not its usage (7-14x low in
bench v2), so the run's report takes its spend from the transcripts: Claude Code's own running
total (the `budget_usd` attachment it writes when --max-budget-usd is set) when present, and the
per-request usage priced at list prices.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_kiro import bench as b
from claude_kiro.cli.main import cli

SID = "5f0e2c1a-1111-4222-8333-944455556666"
RUN = "wf_cost-1"


def ck(cwd: Path, *args: str):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(cli, list(args))
    finally:
        os.chdir(old)


def usage_line(model, req, inp=0, out=0, read=0, write=0, cwd="/proj"):
    return {"type": "assistant", "requestId": req, "cwd": cwd, "timestamp": "2026-10-12T18:10:00Z",
            "message": {"model": model, "usage": {"input_tokens": inp, "output_tokens": out,
                                                  "cache_read_input_tokens": read, "cache_creation_input_tokens": write},
                        "content": [{"type": "text", "text": "x"}]}}


def budget_line(used, total=40):
    return {"type": "attachment", "attachment": {"type": "budget_usd", "used": used, "total": total, "remaining": total - used}}


def write_jsonl(path: Path, lines) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(l) + "\n" for l in lines))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A Claude config dir holding one session: an orchestrator transcript and a workflow run with two agents."""
    h = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(h))
    proj = h / "projects" / "-proj"
    write_jsonl(proj / f"{SID}.jsonl", [usage_line("claude-sonnet-5-5", "m1", out=1_000_000), budget_line(1.5)])
    run = proj / SID / "subagents" / "workflows" / RUN
    write_jsonl(run / "agent-a1.jsonl", [usage_line("claude-haiku-5-5", "r1", read=10_000_000), budget_line(12.25)])
    write_jsonl(run / "agent-a2.jsonl", [usage_line("claude-opus-5-5", "r2", out=100_000), budget_line(12.0)])
    (proj / SID / "workflows").mkdir(parents=True)
    (proj / SID / "workflows" / f"{RUN}.json").write_text(json.dumps({
        "runId": RUN, "workflowName": "spec-implement",
        "workflowProgress": [{"type": "workflow_agent", "agentId": "a1", "tokens": 50_000},
                             {"type": "workflow_agent", "agentId": "a2", "tokens": 50_000}],
    }))
    return h


def test_session_spend_takes_claude_codes_own_total_and_prices_every_transcript(home):
    s = b.session_spend(home, SID)
    assert s["claude_usd"] == 12.25 and s["budget_usd"] == 40  # the highest running total seen
    # sonnet 1M output = $10, haiku 10M cache reads = $0.10, opus 100k output = $2
    assert s["priced_usd"] == pytest.approx(12.10)
    assert s["transcripts"] == 3
    assert s["by_model"]["claude-haiku-5-5"]["cacheRead"] == 10_000_000
    assert s["workflow_tokens"] == 100_000 and s["workflow_transcript_tokens"] == 10_100_000
    assert s["transcript_tokens"] == 11_100_000


def test_run_spend_counts_only_that_runs_agents(home):
    s = b.session_spend(home, SID, run_id=RUN)
    assert s["transcripts"] == 2 and s["priced_usd"] == pytest.approx(2.10)


def test_a_run_id_alone_finds_its_session(home):
    assert b.find_run_session(home, RUN) == SID


def test_latest_session_is_the_newest_one_started_in_this_directory(home, tmp_path):
    other = home / "projects" / "-elsewhere" / "9e9e9e9e-0000-4000-8000-000000000000.jsonl"
    write_jsonl(other, [usage_line("claude-sonnet-5-5", "z", cwd="/elsewhere")])
    assert b.latest_session(home, Path("/proj")) == SID
    assert b.latest_session(home, Path("/nowhere")) is None


def test_ck_cost_reports_real_spend_and_the_undercount(home, tmp_path):
    out = ck(tmp_path, "cost", "--session", SID)
    assert out.exit_code == 0, out.output
    # the workflow's agents used 10.1M tokens by their transcripts; its progress counted 100k
    assert "$12.25" in out.output and "$12.10" in out.output and "101x" in out.output
    data = json.loads(ck(tmp_path, "cost", "--run", RUN, "--json").output)
    assert data["session_id"] == SID and data["run_id"] == RUN and data["priced_usd"] == pytest.approx(2.10)


def test_ck_cost_without_transcripts_says_so(home, tmp_path):
    out = ck(tmp_path, "cost", "--session", "00000000-0000-4000-8000-000000000000")
    assert out.exit_code == 1 and "no transcripts" in out.output.lower()


# --- the budget `ck run` gets -----------------------------------------------------


def repo(tmp_path: Path, tasks: int = 3, done: int = 1) -> Path:
    root = tmp_path / "proj"
    (root / "specs" / "demo").mkdir(parents=True)
    blocks = [f"### Task {i}: t{i}\n**Status:** {'Done' if i <= done else 'Not Started'}\n**Dependencies:** None\n"
              for i in range(1, tasks + 1)]
    (root / "specs" / "demo" / "tasks.md").write_text("# Tasks\n\n" + "\n".join(blocks))
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    return root


@pytest.fixture
def sandboxed(monkeypatch, tmp_path):
    import shutil

    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: f"/usr/bin/{n}" if n in ("bwrap", "socat") else real(n, *a, **k))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def budget_of(root: Path, *args: str) -> str:
    out = ck(root, "run", "demo", "--dry-run", "--json", *args)
    assert out.exit_code == 0, out.output
    argv = json.loads(out.output)["argv"]
    return argv[argv.index("--max-budget-usd") + 1]


def test_budget_default_is_unchanged(tmp_path, sandboxed):
    assert budget_of(repo(tmp_path)) == "40"


def test_budget_is_settable_per_project_and_per_run(tmp_path, sandboxed):
    root = repo(tmp_path)
    assert ck(root, "agents", "set", "run.budget_usd", "75", "--project").exit_code == 0
    assert budget_of(root) == "75"
    assert budget_of(root, "--budget", "20") == "20"  # one run's flag wins


def test_per_task_budget_scales_with_the_tasks_left(tmp_path, sandboxed):
    root = repo(tmp_path, tasks=5, done=1)
    assert ck(root, "agents", "set", "run.budget_per_task_usd", "1.5", "--project").exit_code == 0
    assert budget_of(root) == "6"  # 4 tasks not Done x $1.5
    assert ck(root, "agents", "set", "run.budget_usd", "50", "--project").exit_code == 0
    assert budget_of(root) == "50"  # a fixed cap wins over the per-task one


def test_bad_budget_settings_are_refused(tmp_path, sandboxed):
    root = repo(tmp_path)
    assert ck(root, "agents", "set", "run.budget_usd", "-3", "--project").exit_code != 0
    assert ck(root, "agents", "set", "run.budget_per_task_usd", "lots", "--project").exit_code != 0


def test_a_finished_run_appends_its_real_spend_to_the_log(tmp_path, sandboxed, monkeypatch):
    """The fake claude writes a session transcript the way Claude Code does; `ck run` finds it by cwd."""
    root = repo(tmp_path)
    h = tmp_path / "claude-home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(h))
    line = json.dumps(usage_line("claude-sonnet-5-5", "q1", out=500_000, cwd=str(root))) + "\n" + json.dumps(budget_line(7.5)) + "\n"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(f"#!/bin/sh\nmkdir -p '{h}/projects/-x'\nprintf '%s' '{line}' > '{h}/projects/-x/{SID}.jsonl'\necho fake-claude done\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    out = ck(root, "run", "demo")
    assert out.exit_code == 0, out.output
    log = next((root / ".claude" / "ck-runs").glob("demo-*.log")).read_text()
    assert "Spend" in log and "$7.50" in log and "$5.00" in log
    assert "$7.50" in out.output
