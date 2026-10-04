"""`ck worktree` and `ck waves`: the stable commands parallel task agents call."""

import json
import os
import socket
import sys
import time
from pathlib import Path

import click

from ..config import install_command, load_config
from ..paths import LEGACY_SPECS_DIR, SPECS_DIR, spec_roots
from .. import worktrees as wt


def _spec_dir(project_dir: Path, spec: str) -> Path:
    candidate = Path(spec) if Path(spec).is_absolute() else project_dir / spec
    if (candidate / "tasks.md").exists():
        return candidate
    for root in spec_roots(project_dir):
        if (root / spec / "tasks.md").exists():
            return root / spec
    click.echo(f"❌ No tasks.md found for '{spec}' (looked in {SPECS_DIR}/ and {LEGACY_SPECS_DIR}/)")
    sys.exit(2)


def _spec_name(spec: str) -> str:
    return Path(spec.rstrip("/")).name


def _root() -> Path:
    try:
        return wt.main_root(Path.cwd())
    except wt.WorktreeError as e:
        click.echo(f"❌ Not in a git repository: {e}")
        sys.exit(2)


def _emit(outcomes, as_json: bool, bad_states=("BUSY", "ERROR", "CONFLICT", "MISSING")) -> None:
    if as_json:
        click.echo(json.dumps([o.as_dict() for o in outcomes], indent=2))
    else:
        for outcome in outcomes:
            click.echo(outcome.line())
    if any(o.state == "CONFLICT" for o in outcomes):
        sys.exit(2)
    if any(o.state in bad_states for o in outcomes):
        sys.exit(3)


@click.command()
@click.argument("spec")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
def waves(spec: str, as_json: bool):
    """Show the wave plan for SPEC: Parallel Groups from tasks.md, or computed from Dependencies."""
    from ..waves import plan_waves

    plan = plan_waves(_spec_dir(Path.cwd(), spec))
    if as_json:
        click.echo(json.dumps(plan, indent=2))
        return
    done = set(plan["done"])
    click.echo(f"Waves for {plan['spec']} (from {plan['source']}):")
    for index, wave in enumerate(plan["waves"], 1):
        items = [f"{n}{'✓' if n in done else ''}{' [test]' if plan['tracks'].get(n) == 'test' else ''}" for n in wave]
        click.echo(f"  Wave {index}: {', '.join(items)}")
    for warning in plan["warnings"]:
        click.echo(f"  ⚠️  {warning}")


@click.group()
def worktree():
    """Create, claim, merge, and inspect per-task git worktrees.

    \b
    Layout: .claude/worktrees/<spec>-task-<N> on branch feat/<spec>-task-<N>.
    Exit codes: 0 ok, 2 merge conflict, 3 something busy/missing/failed.
    """


@worktree.command("create")
@click.argument("spec")
@click.argument("tasks", nargs=-1, required=True)
@click.option("--base", help="Branch to create from (default: the main checkout's branch)")
@click.option("--install", "do_install", is_flag=True, help="Run the install step (specs/ck.json \"install\", or detected) in each worktree")
@click.option("--reuse-dirty", is_flag=True, help="Reuse an existing worktree even if it has uncommitted changes")
@click.option("--json", "as_json", is_flag=True)
def create_cmd(spec, tasks, base, do_install, reuse_dirty, as_json):
    """Create (or reuse) worktrees for TASKS of SPEC. Claimed or dirty worktrees are reported BUSY."""
    root = _root()
    install = install_command(load_config(root), root) if do_install else None
    try:
        outcomes = wt.create(root, _spec_name(spec), list(tasks), base=base, install=install, reuse_dirty=reuse_dirty)
    except wt.WorktreeError as e:
        click.echo(f"❌ {e}")
        sys.exit(3)
    _emit(outcomes, as_json)


@worktree.command("integration")
@click.argument("spec")
@click.option("--base", help="Branch to create from (default: the main checkout's branch)")
def integration_cmd(spec, base):
    """Create the integrate/<SPEC> worktree that task merges can go into instead of main."""
    root = _root()
    try:
        outcome = wt.create_integration(root, _spec_name(spec), base=base)
    except wt.WorktreeError as e:
        click.echo(f"❌ {e}")
        sys.exit(3)
    click.echo(outcome.line())


@worktree.command("claim")
@click.argument("spec")
@click.argument("task")
@click.option("--owner", default=None, help="Who holds the claim (default: host, pid, time)")
@click.option("--takeover", is_flag=True, help="Take over a claim left by an agent that died")
def claim_cmd(spec, task, owner, takeover):
    """Mark a task worktree as in use (git worktree lock). Fails with BUSY if already claimed."""
    root = _root()
    owner = owner or f"ck task agent on {socket.gethostname()} pid {os.getppid()} at {time.strftime('%Y-%m-%dT%H:%M:%S')}"
    _emit([wt.claim(root, _spec_name(spec), task, owner, takeover=takeover)], False)


@worktree.command("release")
@click.argument("spec")
@click.argument("task")
def release_cmd(spec, task):
    """Release a task worktree claim."""
    _emit([wt.release(_root(), _spec_name(spec), task)], False)


@worktree.command("merge")
@click.argument("spec")
@click.argument("tasks", nargs=-1, required=True)
@click.option("--into", help="Target branch (default: the main checkout's branch)")
@click.option("--keep", is_flag=True, help="Keep the worktree and branch after merging")
@click.option("--json", "as_json", is_flag=True)
def merge_cmd(spec, tasks, into, keep, as_json):
    """Merge TASKS' branches into the target one at a time; stop at the first conflict.

    A merged task's worktree and branch are removed. On CONFLICT the merge is
    aborted and nothing is left half-merged: resolve inside the task worktree
    (git merge <target> there), commit, and run this again.
    """
    root = _root()
    try:
        outcomes = wt.merge(root, _spec_name(spec), list(tasks), into=into, keep=keep)
    except wt.WorktreeError as e:
        click.echo(f"❌ {e}")
        sys.exit(3)
    _emit(outcomes, as_json, bad_states=("CONFLICT", "ERROR", "BUSY"))


@worktree.command("status")
@click.argument("spec")
@click.option("--json", "as_json", is_flag=True)
def status_cmd(spec, as_json):
    """List SPEC's task worktrees: claimed, uncommitted changes, commits ahead."""
    outcomes = wt.status(_root(), _spec_name(spec))
    if not outcomes and not as_json:
        click.echo("No task worktrees.")
        return
    _emit(outcomes, as_json, bad_states=())
