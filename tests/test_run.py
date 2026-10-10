"""`ck run`: start a whole-spec run headless and unattended, from the main checkout."""

import json
import os
import subprocess
from pathlib import Path

from click.testing import CliRunner

from claude_kiro.cli.main import cli


def ck(cwd: Path, *args: str):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(cli, list(args))
    finally:
        os.chdir(old)


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "specs" / "demo").mkdir(parents=True)
    (root / "specs" / "demo" / "tasks.md").write_text("# Tasks\n\n### Task 1: a\n**Status:** Not Started\n**Dependencies:** None\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    return root


def test_dry_run_prints_an_unattended_headless_command(tmp_path):
    root = repo(tmp_path)
    out = ck(root, "run", "demo", "--dry-run", "--json")
    assert out.exit_code == 0, out.output
    plan = json.loads(out.output)
    argv = plan["argv"]
    assert argv[:2] == ["claude", "-p"]
    assert argv[2] == "/spec:implement demo all"
    for flag in (["--permission-mode", "auto"], ["--permission-prompts", "none"], ["--model", "sonnet"], ["--effort", "low"]):
        i = argv.index(flag[0])
        assert argv[i + 1] == flag[1]
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert "Workflow" in allowed and "Bash(ck *)" in allowed
    assert plan["env"]["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "0"
    assert plan["cwd"] == str(root)
    assert plan["log"].startswith(str(root / ".claude" / "ck-runs" / "demo-"))


def test_extra_arguments_reach_spec_implement_and_session_model_is_settable(tmp_path):
    root = repo(tmp_path)
    out = ck(root, "run", "demo", "--dry-run", "--json", "--model", "haiku", "--", "--escalate", "sonnet,opus", "--max-concurrent", "6")
    argv = json.loads(out.output)["argv"]
    assert argv[2] == "/spec:implement demo all --escalate sonnet,opus --max-concurrent 6"
    assert argv[argv.index("--model") + 1] == "haiku"


def test_refuses_inside_a_linked_worktree(tmp_path):
    root = repo(tmp_path)
    wt = root / ".claude" / "worktrees" / "demo-task-1"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "-b", "t1", str(wt)], check=True)
    out = ck(wt, "run", "demo", "--dry-run")
    assert out.exit_code == 2
    assert "main checkout" in out.output


def test_unknown_spec_is_refused(tmp_path):
    root = repo(tmp_path)
    out = ck(root, "run", "nope", "--dry-run")
    assert out.exit_code == 2 and "nope" in out.output
