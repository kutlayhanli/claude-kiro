"""`ck handoff KIND [ARGS...]`: run a whole-spec skill from the main checkout, not from an isolated session.

A skill started in a `.claude/worktrees/` session would run in a sandbox that refuses what the workflow needs.
This gathers the git facts, asks `handoff.decide_handoff`, and either does nothing, launches a detached `ck run` in
the main checkout, or exits 3 with the exact commands to run by hand. See design.md, "Hand-off flow".
"""

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from .. import handoff as ho
from ..paths import SPECS_DIR

EARLY_EXIT_SECONDS = 3.0


def _git(cwd: Path, *args: str) -> Optional[str]:
    """Stdout of a git command, or None when it fails."""
    try:
        done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    except OSError:
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _linked_main_root(cwd: Path) -> Tuple[bool, Optional[Path]]:
    """(is a linked worktree, its main checkout), read from the `.git` file so a damaged worktree still resolves."""
    for directory in (cwd, *cwd.parents):
        entry = directory / ".git"
        if entry.is_dir():
            return False, directory
        if entry.is_file():
            try:
                text = entry.read_text().strip()
            except OSError:
                return True, None
            if not text.startswith("gitdir:"):
                return True, None
            gitdir = Path(text[len("gitdir:"):].strip())
            if not gitdir.is_absolute():
                gitdir = (directory / gitdir)
            if gitdir.parent.name != "worktrees" or not gitdir.is_dir():
                return True, None
            common = gitdir.parent.parent
            root = common.parent if common.name == ".git" else common
            return True, root.resolve() if root.is_dir() else None
    return False, None


def _tree(cwd: Path, spec: str) -> Optional[str]:
    return _git(cwd, "rev-parse", "--verify", "-q", f"HEAD:{SPECS_DIR}/{spec}")


def gather_facts(kind: str, args: Tuple[str, ...], cwd: Path) -> ho.HandoffFacts:
    linked, main = _linked_main_root(cwd)
    if not linked or main is None:
        return ho.HandoffFacts(kind=kind, args=args, cwd=str(cwd), is_linked_worktree=linked)
    base = dict(kind=kind, args=args, cwd=str(cwd), is_linked_worktree=True, main_root=str(main))
    spec = ho._spec_of(ho.HandoffFacts(kind=kind, args=args))
    if spec is None or "/" in spec or spec in (".", ".."):
        return ho.HandoffFacts(**base)
    wt_tree, main_tree = _tree(cwd, spec), _tree(main, spec)
    wt_head, main_head = _git(cwd, "rev-parse", "HEAD"), _git(main, "rev-parse", "HEAD")
    status = _git(main, "status", "--porcelain", "--untracked-files=no")
    dirty = _git(cwd, "status", "--porcelain", "--", f"{SPECS_DIR}/{spec}")
    same: Optional[bool] = None
    if wt_head and main_head:
        same = wt_tree == main_tree
    return ho.HandoffFacts(
        **base,
        spec_in_main=main_tree is not None,
        spec_in_worktree=wt_tree is not None,
        worktree_head=wt_head,
        main_head=main_head,
        main_branch=_git(main, "branch", "--show-current"),
        main_clean=None if status is None else status == "",
        spec_same=same,
        worktree_spec_dirty=None if dirty is None else dirty != "",
        run_branch_at=_git(main, "rev-parse", "--verify", "-q", f"refs/heads/{spec}-run"),
    )


def _manual(main: str, spec: str, run_args: Tuple[str, ...], rest: Tuple[str, ...], switch: Optional[str] = None) -> str:
    run = "ck run " + " ".join(shlex.quote(a) for a in run_args)
    if rest:
        run += " -- " + " ".join(shlex.quote(a) for a in rest)
    steps = ([switch] if switch else []) + [run]
    return f"cd {shlex.quote(main)} && " + " && ".join(steps)


def _emit(data: Dict[str, Any], as_json: bool, code: int) -> None:
    if as_json:
        click.echo(json.dumps(data, indent=2))
    else:
        for key, value in data.items():
            click.echo(f"{key}: {value}")
    sys.exit(code)


def _fail(error: str, manual: str, as_json: bool) -> None:
    _emit({"handoff": True, "launched": False, "error": error, "manualCommand": manual}, as_json, 3)


@click.command(context_settings={"ignore_unknown_options": True})
@click.argument("kind", type=click.Choice(list(ho.HANDOFF_KINDS)))
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.option("--json", "as_json", is_flag=True, help="Print JSON")
def handoff(kind: str, args: tuple, as_json: bool):
    """Hand a whole-spec skill run to `ck run` in the main checkout when this is an isolated worktree session.

    \b
    ARGS are the skill's arguments, the spec name first. Exit 0: nothing to hand off, or the run launched.
    Exit 3: a hand-off is needed but could not start; `manualCommand` says what to run by hand.
    """
    cwd = Path.cwd().resolve()
    facts = gather_facts(kind, tuple(args), cwd)
    decision = ho.decide_handoff(facts)
    if decision.action == "none":
        _emit({"handoff": False, "interactive": not ho.is_headless(os.environ)}, as_json, 0)
    if decision.action == "refuse":
        _fail(decision.reason, decision.recovery[0] if decision.recovery else "", as_json)

    from .run import RunRefused, preflight

    main = Path(decision.main_checkout or "")
    run_args, rest = decision.run_args, decision.rest
    spec = run_args[0]
    switch_cmd = f"git switch -c {shlex.quote(spec + '-run')} {shlex.quote(facts.worktree_head or '')}"
    manual_switch = _manual(str(main), spec, run_args, rest, switch_cmd)
    run_branch: Optional[str] = None
    if decision.run_branch:
        if facts.run_branch_at is not None:
            switched = subprocess.run(["git", "-C", str(main), "switch", decision.run_branch], capture_output=True, text=True)
        else:
            switched = subprocess.run(["git", "-C", str(main), "switch", "-c", decision.run_branch, decision.start_point or ""],
                                      capture_output=True, text=True)
        if switched.returncode != 0:
            _fail(f"could not switch the main checkout to {decision.run_branch}: "
                  f"{(switched.stderr or switched.stdout).strip()}", manual_switch, as_json)
        run_branch = decision.run_branch
    manual = _manual(str(main), spec, run_args, rest)

    try:
        plan = preflight(main, spec, kind, False)
    except RunRefused as e:
        _fail(e.reason + (f" (the main checkout is now on branch {run_branch})" if run_branch else ""), manual, as_json)
    log = main / ".claude" / "ck-runs" / f"{plan.name}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    launch_log = Path(f"{log}.launch")
    argv = [sys.executable, "-m", "claude_kiro.cli", "run", *run_args, "--log", str(log), "--", *rest]
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with launch_log.open("w") as out:
            proc = subprocess.Popen(argv, cwd=str(main), start_new_session=True, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT)
    except OSError as e:
        _fail(f"could not start ck run: {e}", manual, as_json)
    try:
        code = proc.wait(timeout=EARLY_EXIT_SECONDS)
    except subprocess.TimeoutExpired:
        code = None
    if code is not None:
        try:
            tail = " ".join(launch_log.read_text().split())[-300:]
        except OSError:
            tail = ""
        _fail(f"ck run exited with code {code} within {EARLY_EXIT_SECONDS:g} s" + (f": {tail}" if tail else ""), manual, as_json)
    command = "ck run " + " ".join(shlex.quote(a) for a in (spec, "--command", kind)) + (
        " -- " + " ".join(shlex.quote(a) for a in rest) if rest else "")
    _emit({
        "handoff": True, "launched": True, "mainCheckout": str(main), "log": str(log), "follow": f"tail -f {log}",
        "report": f"ck worktree report {plan.name} (task states) and ck stats show (run analytics), run in {main}",
        "command": command, "runBranch": run_branch,
    }, as_json, 0)
