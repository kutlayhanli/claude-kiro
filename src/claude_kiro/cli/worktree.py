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


def _plan(project_dir: Path, spec: str, exclude: set) -> dict:
    from ..lint import lint_spec
    from ..waves import plan_waves

    spec_dir = _spec_dir(project_dir, spec)
    lint = lint_spec(spec_dir, project_dir)
    if lint["cycles"]:
        from ..hooks._shared.spec_parser import parse_tasks

        tasks = parse_tasks(spec_dir / "tasks.md")
        plan = {
            "spec": spec_dir.name,
            "waves": [],
            "deps": {t.num: t.dependencies for t in tasks},
            "done": [t.num for t in tasks if t.done],
            "titles": {t.num: t.title for t in tasks},
            "tracks": {t.num: t.track for t in tasks},
            "source": "none (dependency cycle)",
            "warnings": [],
        }
    else:
        plan = plan_waves(spec_dir)
    wave_of = {n: i + 1 for i, wave in enumerate(plan["waves"]) for n in wave}
    done = set(plan["done"])
    blocked = {c["task"] for c in lint["verifyCycles"]} | {n for cycle in lint["cycles"] for n in cycle}
    ready = [
        n
        for n, deps in plan["deps"].items()
        if n not in done and n not in exclude and n not in blocked and all(d in done for d in deps)
    ]
    ready.sort(key=lambda n: (wave_of.get(n, 0), int(n) if n.isdigit() else 0))
    return {**plan, **lint, "wave": wave_of, "ready": ready, "remaining": [n for n in plan["deps"] if n not in done]}


@click.command()
@click.argument("spec")
@click.option("--exclude", default="", help="Comma-separated tasks to leave out of `ready` (running or failed)")
@click.option("--json", "as_json", is_flag=True, hidden=True)  # output is always JSON; accepted because the docs and workflow pass it
def plan(spec: str, exclude: str, as_json: bool):
    """Machine-readable plan for SPEC (JSON): waves, deps, done, ready-to-start tasks, and lint results."""
    excluded = {x.strip() for x in exclude.split(",") if x.strip()}
    click.echo(json.dumps(_plan(Path.cwd(), spec, excluded), indent=2))


@click.command()
@click.argument("spec")
@click.option("--json", "as_json", is_flag=True)
def lint(spec: str, as_json: bool):
    """Check SPEC's task plan: dependency cycles, verify-order cycles, reasonless dependencies, critical path.

    Exits 1 if there is a cycle (the plan can't be executed as written).
    """
    result = _plan(Path.cwd(), spec, set())
    if as_json:
        click.echo(json.dumps({k: result[k] for k in ("cycles", "verifyCycles", "reasonless", "criticalPath", "waves")}, indent=2))
    else:
        click.echo(f"Plan lint for {result['spec']}:")
        if result["cycles"]:
            for cycle in result["cycles"]:
                click.echo(f"  ✗ dependency cycle: {' -> '.join('Task ' + n for n in cycle)}")
        else:
            click.echo("  ✓ no dependency cycles")
        for item in result["verifyCycles"]:
            click.echo(f"  ✗ verify-order cycle: {item['message']}")
        if not result["verifyCycles"] and not result["cycles"]:
            click.echo("  ✓ no verify-order cycles found (checked Python imports of existing test files)")
        if result["reasonless"]:
            click.echo(f"  ⚠ {len(result['reasonless'])} dependenc{'y' if len(result['reasonless']) == 1 else 'ies'} without a stated reason (each one serializes work):")
            for item in result["reasonless"][:25]:
                hint = f"; shares {', '.join(item['sharedFiles'])}" if item["sharedFiles"] else "; no shared files"
                click.echo(f"      Task {item['task']} -> Task {item['dependsOn']}{hint}")
            if len(result["reasonless"]) > 25:
                click.echo(f"      ... and {len(result['reasonless']) - 25} more (see --json)")
            click.echo('      Add a reason in parentheses, e.g. "Task 3 (calls parse_config)", or drop the edge.'
                       " Specs written before ck 0.4 have no reasons; review the edges on the critical path first.")
        path = result["criticalPath"]
        open_waves = sum(1 for wave in result["waves"] if any(n in result["remaining"] for n in wave))
        if path:
            click.echo(f"  ℹ critical path ({len(path)} open tasks): {' -> '.join(path)}; open waves: {open_waves}")
    if result["cycles"] or result["verifyCycles"]:
        sys.exit(1)


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
