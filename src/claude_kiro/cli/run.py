"""`ck run`: start a whole-spec /spec:implement run headless and unattended.

A spec run takes hours; it should not stall on a permission prompt. This starts
`claude -p` in the main checkout with auto permissions and no prompts (anything
that would have asked is denied and shows up in the run's report), allows the
Workflow launch and the ck/git commands the agents run, and keeps the process
open until the workflow finishes. Run it from the main checkout, not from a
worktree: worktree-isolated sessions refuse commands they cannot prove stay
inside that worktree, and the workflow makes its own task worktrees anyway.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import click

from .worktree import _spec_dir

ALLOWED_TOOLS = ["Workflow", "Bash(ck *)", "Bash(command git *)", "Bash(git *)"]


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


def _linked_worktree(cwd: Path) -> bool:
    git_dir, common = _git(cwd, "rev-parse", "--absolute-git-dir"), _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return bool(git_dir and common) and Path(git_dir).resolve() != Path(common).resolve()


@click.command(context_settings={"ignore_unknown_options": True})
@click.argument("spec")
@click.argument("extra", nargs=-1, type=click.UNPROCESSED)
@click.option("--model", default="sonnet", show_default=True, help="Model of the orchestrating session (it only launches and reports)")
@click.option("--effort", default="low", show_default=True, help="Effort of the orchestrating session")
@click.option("--dry-run", is_flag=True, help="Print the command instead of running it")
@click.option("--json", "as_json", is_flag=True, help="With --dry-run: print the command as JSON")
def run(spec: str, extra: tuple, model: str, effort: str, dry_run: bool, as_json: bool):
    """Run every task of SPEC unattended: headless, no permission prompts.

    \b
    Arguments after `--` go to /spec:implement, e.g.
      ck run auth -- --escalate sonnet,opus
    Agent models come from `ck agents` as usual. Output goes to
    .claude/ck-runs/<spec>-<time>.log; follow it with `tail -f`.
    """
    cwd = Path.cwd()
    if _linked_worktree(cwd):
        click.echo("❌ Run `ck run` from the main checkout, not from a worktree: a worktree-isolated session refuses "
                   "the commands the workflow needs, and the workflow creates its own task worktrees.")
        sys.exit(2)
    name = _spec_dir(cwd, spec).name  # exits 2 if the spec has no tasks.md
    prompt = " ".join([f"/spec:implement {name} all", *extra])
    argv = [
        "claude", "-p", prompt,
        "--permission-mode", "auto",
        "--permission-prompts", "none",
        "--allowedTools", ",".join(ALLOWED_TOOLS),
        "--model", model,
        "--effort", effort,
    ]
    env = {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"}
    log = cwd / ".claude" / "ck-runs" / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    if dry_run:
        if as_json:
            click.echo(json.dumps({"argv": argv, "env": env, "cwd": str(cwd), "log": str(log)}, indent=2))
        else:
            click.echo(" ".join(f"{k}={v}" for k, v in env.items()) + " " + " ".join(argv[:2]) + f" {json.dumps(prompt)} " + " ".join(argv[3:]))
            click.echo(f"log: {log}")
        return
    if not shutil.which("claude"):
        click.echo("❌ `claude` is not on PATH")
        sys.exit(2)
    log.parent.mkdir(parents=True, exist_ok=True)
    click.echo(f"Running {name} unattended (orchestrator session {model}/{effort}); log: {log}")
    with log.open("w") as out:
        code = subprocess.call(argv, cwd=cwd, env={**os.environ, **env}, stdout=out, stderr=subprocess.STDOUT)
    click.echo(f"{'✓' if code == 0 else '❌'} claude exited {code}; log: {log}")
    sys.exit(code)
