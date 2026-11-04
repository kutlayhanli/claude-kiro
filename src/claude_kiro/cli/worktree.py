"""`ck worktree` and `ck waves`: the stable commands parallel task agents call."""

import json
import os
import socket
import sys
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
    """Show SPEC's tasks by dependency level (display only; tasks start when their own dependencies merge)."""
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
    from ..hooks._shared.spec_parser import parse_tasks
    from ..lint import lint_spec, task_risk
    from ..waves import plan_waves

    spec_dir = _spec_dir(project_dir, spec)
    lint = lint_spec(spec_dir, project_dir)
    tasks = parse_tasks(spec_dir / "tasks.md")
    if lint["cycles"]:
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
    chain = _chain_lengths(plan["deps"], done)
    # Longest remaining chain first: when slots are short, the critical path starts first.
    ready.sort(key=lambda n: (-chain.get(n, 1), int(n) if n.isdigit() else 0))
    return {
        **plan,
        **lint,
        # Routing inputs for /spec:implement: "safety" tasks start on a stronger model
        # and get the risky reviewer; complexity feeds implementer.complexity_routing.
        "risk": task_risk({t.num: t for t in tasks}, load_config(project_dir))["risk"],
        "complexity": {t.num: t.complexity for t in tasks},
        "wave": wave_of,
        "chain": chain,
        "ready": ready,
        "remaining": [n for n in plan["deps"] if n not in done],
        # Absolute directory of this spec's task worktrees (spec-implement's worktreeDir arg).
        "worktreeDir": _worktree_dir(project_dir, spec_dir.name),
    }


def _worktree_dir(project_dir: Path, spec: str):
    try:
        return str(wt.worktrees_dir(wt.main_root(project_dir), spec))
    except wt.WorktreeError:  # not a git checkout: no worktrees to place
        return None


def _chain_lengths(deps: dict, done: set) -> dict:
    """For each open task, the number of open tasks on its longest chain of dependents, itself included."""
    dependents: dict = {n: [] for n in deps}
    for n, ds in deps.items():
        for d in ds:
            if d in dependents and n not in done:
                dependents[d].append(n)
    lengths: dict = {}

    def length(n: str, stack: frozenset) -> int:
        if n not in lengths:
            below = [length(m, stack | {m}) for m in dependents[n] if m not in stack]
            lengths[n] = 1 + max(below, default=0)
        return lengths[n]

    return {n: length(n, frozenset({n})) for n in deps if n not in done}


@click.command()
@click.argument("spec")
@click.option("--exclude", default="", help="Comma-separated tasks to leave out of `ready` (running or failed)")
@click.option("--json", "as_json", is_flag=True, hidden=True)  # output is always JSON; accepted because the docs and workflow pass it
@click.option("--progress-run", default=None, help="Run key whose progress file records --failed tasks")
@click.option("--failed", default="", help="Comma-separated tasks that failed (recorded once each in the progress file)")
def plan(spec: str, exclude: str, as_json: bool, progress_run, failed: str):
    """Machine-readable plan for SPEC (JSON): deps, done, ready-to-start tasks (longest remaining chain first), lint results, and display waves."""
    excluded = {x.strip() for x in exclude.split(",") if x.strip()}
    click.echo(json.dumps(_plan(Path.cwd(), spec, excluded), indent=2))
    ids = [x.strip() for x in failed.split(",") if x.strip()]
    if progress_run and ids:
        _record_failed(_spec_name(spec), progress_run, ids)


def _record_failed(spec: str, key: str, ids) -> None:
    try:
        from .. import progress as prog

        events, _ = prog.read_events(prog.run_file(key, os.environ))
        state = prog.fold(events)
        start = next((e for e in events if e.get("kind") == "start"), {})
        known = {str(t) for t in (start.get("open") or [])}
        todo = [t for t in dict.fromkeys(ids) if t in known and not (t in state.tasks and state.tasks[t].status == "failed")]
    except Exception:  # noqa: BLE001
        return
    if todo:
        _record_progress(spec, key, [("task-failed", {"task": t}) for t in todo])


@click.command()
@click.argument("spec")
@click.option("--json", "as_json", is_flag=True)
@click.option("--contracts", is_flag=True, help="Instead: flag keys and status values the spec's test files use that design.md never names")
@click.option("--task", "tasks", multiple=True, help="With --contracts: only these test tasks' files (repeatable)")
@click.option("--base", help="With --contracts: check the test files changed since this git ref instead")
@click.option("--strict", is_flag=True, help="With --contracts: exit 1 when anything is flagged (default: warn)")
def lint(spec: str, as_json: bool, contracts: bool, tasks, base, strict: bool):
    """Check SPEC's task plan: dependency cycles, verify-order cycles, reasonless dependencies, untagged risk-path tasks, critical path.

    Exits 1 if there is a cycle (the plan can't be executed as written).
    With --contracts, checks test files against design.md's data models instead (a heuristic: it warns unless --strict).
    """
    if contracts:
        _lint_contracts(spec, as_json, tasks, base, strict)
        return
    result = _plan(Path.cwd(), spec, set())
    if as_json:
        click.echo(json.dumps({k: result[k] for k in ("cycles", "verifyCycles", "reasonless", "criticalPath", "waves", "riskUntagged")}, indent=2))
    else:
        click.echo(f"Plan lint for {result['spec']}:")
        if result["cycles"]:
            for cycle in result["cycles"]:
                click.echo(f"  ✗ dependency cycle: {' -> '.join('Task ' + n for n in cycle)}")
        else:
            click.echo("  ✓ no dependency cycles")
        from ..hooks._shared.spec_parser import parse_tasks

        parsed = parse_tasks(_spec_dir(Path.cwd(), spec) / "tasks.md")
        known = {t.num for t in parsed}
        for t in parsed:
            unknown = [d for d in t.dependencies if d not in known]
            if unknown:
                click.echo(f"  ⚠ Task {t.num} depends on unknown Task {', '.join(unknown)}")
        for item in result["verifyCycles"]:
            if item.get("kind") == "verify-file":
                click.echo(f"  ✗ verify-file cycle: {item['message']}")
            else:
                click.echo(f"  ✗ verify-order cycle: {item['message']}")
        if not result["verifyCycles"] and not result["cycles"]:
            click.echo("  ✓ no verify-order cycles found (checked Python imports of existing test files and Verify test files owned by test tasks)")
        if result["reasonless"]:
            click.echo(f"  ⚠ {len(result['reasonless'])} dependenc{'y' if len(result['reasonless']) == 1 else 'ies'} without a stated reason (each one serializes work):")
            for item in result["reasonless"][:25]:
                hint = f"; shares {', '.join(item['sharedFiles'])}" if item["sharedFiles"] else "; no shared files"
                click.echo(f"      Task {item['task']} -> Task {item['dependsOn']}{hint}")
            if len(result["reasonless"]) > 25:
                click.echo(f"      ... and {len(result['reasonless']) - 25} more (see --json)")
            click.echo('      Add a reason in parentheses, e.g. "Task 3 (calls parse_config)", or drop the edge.'
                       " Specs written before ck 0.4 have no reasons; review the edges on the critical path first.")
        for item in result["riskUntagged"]:
            click.echo(f"  ⚠ Task {item['task']} touches risk path {item['file']} but has no **Risk:** safety (matches {item['pattern']} in risk_paths)")
        if result["riskUntagged"]:
            click.echo("      It is routed as safety anyway. Add \"**Risk:** safety (one-line reason)\" so the spec says so.")
        path = result["criticalPath"]
        if path:
            click.echo(f"  ℹ critical path ({len(path)} of {len(result['remaining'])} open tasks): {' -> '.join(path)}")
            click.echo(f"  ℹ ready now: {', '.join(result['ready']) or 'none'}")
    if result["cycles"] or result["verifyCycles"]:
        sys.exit(1)


def _lint_contracts(spec: str, as_json: bool, tasks, base, strict: bool) -> None:
    from ..contracts import check_contracts

    project_dir = Path.cwd()
    spec_dir = _spec_dir(project_dir, spec)
    try:
        result = check_contracts(spec_dir, project_dir, tasks=tasks, base=base)
    except ValueError as e:
        click.echo(f"❌ {e}")
        sys.exit(2)
    if as_json:
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(f"Contract lint for {spec_dir.name} ({len(result['checkedFiles'])} test file(s) against {result['designNames']} design.md names):")
        if result["skipped"]:
            click.echo(f"  ℹ skipped: {result['skipped']}")
        elif not result["unknown"]:
            click.echo("  ✓ no unknown keys or status values")
        else:
            for item in result["unknown"]:
                what = "key" if item["kind"] == "key" else "compared value"
                hint = f" (did you mean '{item['suggest']}'?)" if item["suggest"] else ""
                click.echo(f"  ⚠ {item['file']}:{item['line']}: {what} '{item['key']}' is not in design.md{hint}")
            click.echo("      A heuristic: each is a string literal the tests use that no design.md code block, table or")
            click.echo("      backticked name defines. Fix the test or the design, or list deliberate ones in")
            click.echo("      specs/ck.json \"contracts_ignore\".")
    if strict and result["unknown"]:
        sys.exit(1)


# --------------------------------------------------------------------------- progress events


def _record_progress(spec: str, key, builds, *, eta: bool = False):
    """Append events to a run's progress file, then recompute. Returns (eta line or None, failure text or None).

    `key` is a run key, or None to use the spec's active run (park/resolve). `builds` is a list of
    (kind, options) pairs. A progress failure never reaches the caller's output or exit code."""
    try:
        from .. import progress as prog
        from . import eta as ce

        env = os.environ
        now = prog.clock_now(env)
        if key is None:
            key = prog.active_run(spec, _root(), env, now=now)
            if not key:
                return None, None
        events, _ = prog.read_events(prog.run_file(key, env))
        failure = None
        for kind, opts in builds:  # one bad event does not drop the others
            try:
                ev = ce._build_event(spec, kind, events, now, opts, True)
            except Exception as exc:  # noqa: BLE001
                failure = failure or f"failed: {type(exc).__name__}: {exc}"
                continue
            err = prog.append(key, ev, env)
            if err is not None:
                return None, f"failed: {err}"
            events.append(ev)
        from ..eta import service

        line = service.after_event(key, now=prog.clock_now(env), env=env)
        return (line if eta else None), failure
    except Exception as exc:  # noqa: BLE001 - progress never changes a command
        return None, f"failed: {type(exc).__name__}: {exc}"


@click.group()
def worktree():
    """Create, claim, merge, and inspect per-task git worktrees.

    \b
    Layout: <worktree_dir>/<spec>-task-<N> on branch feat/<spec>-task-<N>;
    worktree_dir is set in specs/ck.json (default .ck/worktrees). Worktrees made by
    older ck in .claude/worktrees are still found.
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
@click.option("--owner", default=None, help="Who holds the claim (default: ck task agent on <host>); a token and the time are appended")
@click.option("--takeover", is_flag=True, help="Take over a claim left by an agent that is gone, however recent")
@click.option("--progress-run", default=None, help="Run key to record a progress event in (only when the claim succeeds)")
@click.option("--progress-event", type=click.Choice(["task-start", "retry", "escalation", "revision", "landfix", "resolve"]),
              default=None, help="Kind of progress event for this claim")
@click.option("--progress-model", default=None, help="Model of the agent that will work on the task")
@click.option("--attempt", type=int, default=None, help="Attempt number for the progress event")
def claim_cmd(spec, task, owner, takeover, progress_run, progress_event, progress_model, attempt):
    """Mark a task worktree as in use (git worktree lock). Fails with BUSY if already claimed.

    A claim with no activity (claim, `ck gate` run, commit) for claim_stale_minutes
    in specs/ck.json (default 60) is stale and taken over without --takeover.
    """
    root = _root()
    owner = owner or f"ck task agent on {socket.gethostname()}"
    outcome = wt.claim(root, _spec_name(spec), task, owner, takeover=takeover)
    if outcome.state == "CLAIMED" and progress_run and progress_event:
        _record_progress(_spec_name(spec), progress_run,
                         [(progress_event, {"task": task, "model": progress_model, "attempt": attempt})])
    _emit([outcome], False)


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


def _gate_fn(spec: str):
    """Run the task gate in-process in the target checkout: (green, failing part of the report)."""
    from ..gate import run_gate

    def gate(target: Path, nums):
        spec_dir = next((r / spec for r in spec_roots(target) if (r / spec / "tasks.md").exists()), None)
        if spec_dir is None:
            return False, f"no tasks.md for {spec} in {target}"
        result = run_gate(target, spec_dir, list(nums))
        if result.ok:
            return True, ""
        lines = []
        for check in result.checks:
            if not check.ok:
                lines.append(f"✗ {check.name}")
                lines.extend(f"    {line}" for line in check.detail.rstrip().splitlines())
        return False, "\n".join(lines[-80:])

    return gate


def _record_landing(spec: str, key: str, result: dict) -> dict:
    """Append the landing, merge and hand-landing events; return `result` with `eta` (and `progress` on failure)."""
    states = {}
    for item in result["tasks"]:
        states.setdefault(item["state"], []).append(str(item["task"]))
    gates = result.get("gates") or []
    green = all(bool(g.get("green")) for g in gates) if gates else not states.get("RED")
    builds = [("landing", {"tasks": ",".join(str(i["task"]) for i in result["tasks"]),
                           "merged": ",".join(states.get("MERGED", [])), "red": ",".join(states.get("RED", [])),
                           "conflict": ",".join(states.get("CONFLICT", [])),
                           "protected": ",".join(states.get("PROTECTED", [])), "green": green})]
    builds += [("merge", {"task": t}) for t in states.get("MERGED", [])]
    builds += [("hand-landing", {"task": t}) for t in states.get("PROTECTED", [])]
    line, failure = _record_progress(spec, key, builds, eta=True)
    result = {**result, "eta": line}
    if failure:
        result["progress"] = failure
    return result


def _priority(target: Path, spec: str) -> dict:
    """Longest remaining dependency chain per task, from tasks.md on the target (critical path lands first)."""
    from ..waves import plan_waves

    for r in spec_roots(target):
        if (r / spec / "tasks.md").exists():
            try:
                plan = plan_waves(r / spec)
                return _chain_lengths(plan["deps"], set(plan["done"]))
            except Exception:  # an unparsable plan only loses the ordering
                return {}
    return {}


@worktree.command("land")
@click.argument("spec")
@click.argument("tasks", nargs=-1, required=True)
@click.option("--into", help="Target branch (default: the main checkout's branch)")
@click.option("--gate", "do_gate", is_flag=True, help="Gate the batch (ck gate --task for every landed task) and bisect a red batch")
@click.option("--install", "do_install", is_flag=True, help="Refresh dependencies in the target (specs/ck.json \"install\", or detected) before each gate")
@click.option("--release", "release_claims", is_flag=True, help="Release claims left on the task worktrees (their agents have finished)")
@click.option("--keep", is_flag=True, help="Keep merged tasks' worktrees and branches")
@click.option("--progress-run", default=None, help="Run key to record the landing in; adds an `eta` line to the JSON")
@click.option("--json", "as_json", is_flag=True)
def land_cmd(spec, tasks, into, do_gate, do_install, release_claims, keep, progress_run, as_json):
    """Land a batch of TASKS on the target: precheck, merge, one gate, bisect if red.

    \b
    Each branch is checked with `git merge-tree` first, so a CONFLICT never touches
    the target; a branch that only conflicts with an earlier one in the batch is
    DEFERRED to the next batch. The clean ones merge (--no-ff, critical path first)
    and, with --gate, are gated together. A red batch is bisected by halving:
    the target is reset to its pre-batch commit and the halves re-landed until the
    culprits are found. Culprits are RED and stay off the target (branch kept).
    A branch that changes .claude/**, .mcp.json, git hook directories or
    specs/ck.json is PROTECTED and never merged (a human reviews it), unless
    specs/ck.json "land": {"allow_paths": [...]} lists the paths. Allowed paths
    the target can't write (Claude Code's Bash sandbox keeps the main checkout's
    .claude/ read-only) are PROTECTED too: land those attended, outside the sandbox.
    Exit codes: 0 ok, 1 a task RED, 2 a CONFLICT, 3 busy/missing/protected/failed.
    """
    root = _root()
    name = _spec_name(spec)
    try:
        target_branch = into or wt.current_branch(root)
        target = wt.checkout_of(root, target_branch)
        result = wt.land(
            root,
            name,
            list(tasks),
            into=target_branch,
            gate=_gate_fn(name) if do_gate else None,
            install=install_command(load_config(target or root), target or root) if do_install else None,
            keep=keep,
            release_claims=release_claims,
            priority=_priority(target, name) if target else {},
        )
    except wt.WorktreeError as e:
        if as_json:
            click.echo(json.dumps({"error": str(e), "tasks": []}, indent=2))
        else:
            click.echo(f"❌ {e}")
        sys.exit(3)
    if progress_run:
        result = _record_landing(name, progress_run, result)
    if as_json:
        click.echo(json.dumps(result, indent=2))
    else:
        for item in result["tasks"]:
            click.echo(wt.Landed(**item).line())
        gates = result["gates"]
        if gates:
            click.echo(f"{len(gates)} gate run(s){' (bisected)' if result['bisected'] else ''}")
    states = {t["state"] for t in result["tasks"]}
    if states & {"BUSY", "ERROR", "MISSING", "PROTECTED"}:
        sys.exit(3)
    if "CONFLICT" in states:
        sys.exit(2)
    if "RED" in states:
        sys.exit(1)


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


@worktree.command("report")
@click.argument("spec")
@click.option("--into", help="Target branch (default: the main checkout's branch)")
@click.option("--json", "as_json", is_flag=True)
def report_cmd(spec, into, as_json):
    """What landed, from git: merged / done / failed / blocked / unstarted tasks of SPEC.

    \b
    merged:    a merge commit on the target names the task branch
    done:      Done in tasks.md on the target, no merge commit (earlier run)
    failed:    the task branch has commits that never landed
    blocked:   a dependency is not Done on the target ("on")
    unstarted: ready, but nothing was committed for it
    needs input: parked with `ck worktree park`; waits for `ck worktree resolve`
    Each task is in exactly one list. Agent claims are not consulted; an
    orchestrator adds its agents' reasons to this as annotations.
    """
    try:
        result = wt.report(_root(), _spec_name(spec), into=into)
    except wt.WorktreeError as e:
        if as_json:
            click.echo(json.dumps({"error": str(e)}, indent=2))
        else:
            click.echo(f"❌ {e}")
        sys.exit(3)
    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    click.echo(f"Report for {result['spec']} on {result['into']} ({result['head'][:10]}):")
    for key in ("merged", "done", "failed", "blocked", "unstarted"):
        items = result[key]
        shown = ", ".join(f"{e['task']} (on {', '.join(e['on'])})" if key == "blocked" else e["task"] for e in items)
        click.echo(f"  {key} {len(items)}{': ' + shown if shown else ''}")
    waiting = result.get("needsInput", [])
    if waiting:
        # Unindented on purpose: a notification message and this line must not share a 12-character window.
        click.echo(f"needs input {len(waiting)}: " + ", ".join(f"{e['task']} ({e['decision']})" for e in waiting))
        for e in waiting:
            click.echo(f"    task {e['task']} ({e['file']}, {e['kind']}):")
            click.echo(f"      test: {e['test']}")
            click.echo(f"      requirement: {e['requirement']}")
        click.echo("    Answer with `ck worktree resolve <spec> <N>` (answer JSON on stdin), then run the spec again.")


def _read_stdin_object(what: str) -> dict:
    """The JSON object on stdin, or exit 3 with a message."""
    try:
        data = json.loads(sys.stdin.read())
    except ValueError as e:
        click.echo(f"❌ {what}: stdin is not valid JSON ({e})")
        sys.exit(3)
    if not isinstance(data, dict):
        click.echo(f"❌ {what}: stdin must be a JSON object")
        sys.exit(3)
    return data


@worktree.command("park")
@click.argument("spec")
@click.argument("task")
@click.option("--no-notify", is_flag=True, help="Write the park note without sending a notification")
@click.option("--json", "as_json", is_flag=True)
def park_cmd(spec, task, no_notify, as_json):
    """Park TASK as needing my decision: write the note, send one notification.

    \b
    Reads {"file", "kind", "test", "requirement", "decision"} as JSON on stdin.
    The notification (run.notify_command) names the spec and task only, never
    the quoted text. Exits 0 once the note is written, even if the notification
    failed; exits 3 when the note cannot be written or stdin is not valid JSON.
    Inside `ck run` (CK_RUN_NOTIFY=parent) ck run's own process sends it.
    """
    from .. import agents as ag
    from .. import notify

    root = _root()
    name = _spec_name(spec)
    note = _read_stdin_object("park")
    try:
        path = wt.park(root, name, task, note)
    except wt.WorktreeError as e:
        click.echo(f"❌ {e}")
        sys.exit(3)
    if no_notify:
        status, detail = "skipped", "--no-notify"
    elif os.environ.get("CK_RUN_NOTIFY") == "parent":
        status, detail = "deferred", "ck run sends it"
    else:
        try:
            command = ag.resolve(root)["roles"]["run"]["notify_command"]
        except ag.AgentConfigError as e:
            command = None
            outcome = notify.NotifyOutcome("failed", f"cannot read run.notify_command: {e}")
        else:
            outcome = notify.send(command, notify.notify_message(name, task), root)
        status, detail = outcome.status, outcome.detail
    _record_progress(name, None, [("park", {"task": task})])
    result = {"parked": True, "note": str(path), "notification": status, "detail": detail}
    if as_json:
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(f"parked {name} task {task}: {path}")
        click.echo(f"notification: {status}" + (f" ({detail})" if detail else ""))


@worktree.command("resolve")
@click.argument("spec")
@click.argument("task")
@click.option("--json", "as_json", is_flag=True)
def resolve_cmd(spec, task, as_json):
    """Record my answer for a parked TASK, clear its park note.

    Reads {"answer": "..."} as JSON on stdin. Every later attempt of the task
    reads the decision note first.
    """
    root = _root()
    name = _spec_name(spec)
    data = _read_stdin_object("resolve")
    answer = data.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        click.echo("❌ resolve: stdin must be {\"answer\": \"<text>\"}")
        sys.exit(3)
    try:
        had = task in wt.parked(root, name)
        path = wt.resolve(root, name, task, answer)
    except wt.WorktreeError as e:
        click.echo(f"❌ {e}")
        sys.exit(3)
    if had:
        _record_progress(name, None, [("unpark", {"task": task})])
    result = {"resolved": True, "decisionNote": str(path), "hadParkNote": bool(had)}
    if as_json:
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(f"resolved {name} task {task}: {path}")
