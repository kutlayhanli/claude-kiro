"""`ck principles` and the `ck arbiter` group (run-principles Task 31).

`ck principles init [--commit]` writes `specs/PRINCIPLES.md` from the starter text when it is missing.
An existing file is left untouched. `--commit` also commits that file alone on the checkout's branch,
and only when this call created it.

`ck principles check SPEC --json` reports whether the spec's PLAN.md may be Decided (1.4).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

from .. import worktrees as wt
from ..arbiter import (
    ArbiterError,
    claim_from_json,
    close_lane,
    open_claim,
    open_lane,
    record_ruling,
    ruling_record_from_json,
    settle_claim,
)
from ..arbiter_policy import TaskMeta
from ..principles import STARTER_TEXT, plan_may_be_decided

PRINCIPLES_REL = Path("specs") / "PRINCIPLES.md"
COMMIT_MESSAGE = "principles: add specs/PRINCIPLES.md defaults"


def _git(cwd: Path, *args: str) -> Optional[str]:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def _toplevel() -> Path:
    out = _git(Path.cwd(), "rev-parse", "--show-toplevel")
    if out is None:
        click.echo("Not in a git repository.", err=True)
        sys.exit(2)
    return Path(out.strip())


@click.group()
def principles():
    """Project principles: the defaults file and plan checks."""


@principles.command("init")
@click.option("--commit", "commit", is_flag=True, help="Commit specs/PRINCIPLES.md alone when this call created it.")
@click.option("--json", "as_json", is_flag=True)
def init(commit: bool, as_json: bool):
    """Create specs/PRINCIPLES.md from the starter text when it is missing."""
    root = _toplevel()
    path = root / PRINCIPLES_REL
    created = False
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STARTER_TEXT, encoding="utf-8")
        created = True

    commit_sha: Optional[str] = None
    if commit and created:
        rel = PRINCIPLES_REL.as_posix()
        if _git(root, "add", "--", rel) is None:
            click.echo("git add failed for specs/PRINCIPLES.md", err=True)
            sys.exit(2)
        if _git(root, "commit", "--only", "-m", COMMIT_MESSAGE, "--", rel) is None:
            click.echo("git commit failed for specs/PRINCIPLES.md", err=True)
            sys.exit(2)
        commit_sha = (_git(root, "rev-parse", "HEAD") or "").strip() or None

    result = {"created": created, "path": str(path), "commit": commit_sha}
    if as_json:
        click.echo(json.dumps(result))
    elif created:
        click.echo(f"Created {path}" + (f" (commit {commit_sha[:7]})" if commit_sha else ""))
    else:
        click.echo(f"{path} already exists; left untouched")


@principles.command("check")
@click.argument("spec")
@click.option("--json", "as_json", is_flag=True)
def check(spec: str, as_json: bool):
    """Report whether SPEC's PLAN.md carries a usable ## Principles section. Exit 1 when it may not be Decided."""
    root = _toplevel()
    plan_path = root / "specs" / spec / "PLAN.md"
    if not plan_path.is_file():
        ok, reason = False, f"no PLAN.md for spec {spec}"
    else:
        ok, reason = plan_may_be_decided(plan_path.read_text(encoding="utf-8"))
    if as_json:
        click.echo(json.dumps({"ok": ok, "reason": reason}))
    else:
        click.echo(("ok: " if ok else "not decidable: ") + reason)
    if not ok:
        sys.exit(1)


@click.group()
def arbiter():
    """The arbiter: rulings on test-conflict claims."""


@arbiter.command("open")
@click.argument("spec")
@click.option("--task", "task", required=True, help="The task number the claim belongs to.")
@click.option("--into", "into", required=True, help="The integration branch the arbiter worktree starts from.")
@click.option("--json", "as_json", is_flag=True)
def arbiter_open(spec: str, task: str, into: str, as_json: bool):
    """Decide whether CLAIM may be ruled on (stdin: {"claim": Claim, "task": TaskMeta}); start the arbiter worktree when it may."""
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise ArbiterError("stdin must be a JSON object with claim and task")
        claim = claim_from_json(payload.get("claim"), task)
        raw_task = payload.get("task") or {}
        if not isinstance(raw_task, dict) or raw_task.get("risk") not in ("safety", "normal"):
            raise ArbiterError("task.risk must be safety or normal")
        task_meta = TaskMeta(risk=raw_task["risk"], complexity=str(raw_task.get("complexity", "")))
        root = wt.main_root(_toplevel())
        result = open_claim(root, spec, into, task, claim, task_meta)
    except (ArbiterError, wt.WorktreeError, ValueError) as exc:
        click.echo(f"ck arbiter open: {exc}", err=True)
        sys.exit(2)
    click.echo(json.dumps(result))


@arbiter.command("settle")
@click.argument("spec")
@click.argument("key")
@click.option("--tier", "tier", required=True, type=click.Choice(["normal", "risky"]))
@click.option("--json", "as_json", is_flag=True)
def arbiter_settle(spec: str, key: str, tier: str, as_json: bool):
    """Validate and route the arbiter's raw answer (stdin) to claim KEY; update the ledger and the worktree."""
    try:
        root = wt.main_root(_toplevel())
        result = settle_claim(root, spec, key, sys.stdin.read(), tier)
    except (ArbiterError, wt.WorktreeError) as exc:
        click.echo(f"ck arbiter settle: {exc}", err=True)
        sys.exit(2)
    click.echo(json.dumps(result))


@arbiter.command("record")
@click.argument("spec")
@click.option("--into", "into", required=True, help="The integration branch whose checkout gets decisions.md.")
@click.option("--lock-wait", "lock_wait", type=float, default=120.0, show_default=True,
              help="Seconds to wait for the landing lock on INTO before refusing.")
@click.option("--json", "as_json", is_flag=True)
def arbiter_record(spec: str, into: str, lock_wait: float, as_json: bool):
    """Append the ruling (stdin: RulingRecord) to SPEC's decisions.md on INTO and commit that file alone."""
    try:
        record = ruling_record_from_json(json.loads(sys.stdin.read()))
        root = wt.main_root(_toplevel())
        result = record_ruling(root, spec, into, record, lock_wait)
    except (ArbiterError, wt.WorktreeError, ValueError) as exc:
        click.echo(f"ck arbiter record: {exc}", err=True)
        sys.exit(2)
    click.echo(json.dumps(result))


@arbiter.group("lane")
def lane():
    """The /spec:create lane ticket."""


@lane.command("open")
@click.argument("spec")
@click.option("--json", "as_json", is_flag=True)
def lane_open(spec: str, as_json: bool):
    """Write the lane ticket for SPEC in this checkout."""
    path = open_lane(_toplevel(), spec)
    if as_json:
        click.echo(json.dumps({"spec": spec, "open": True, "path": str(path)}))
    else:
        click.echo(f"Opened lane for {spec}: {path}")


@lane.command("close")
@click.argument("spec")
@click.option("--json", "as_json", is_flag=True)
def lane_close(spec: str, as_json: bool):
    """Remove the lane ticket for SPEC. Succeeds when there is none."""
    removed = close_lane(_toplevel(), spec)
    if as_json:
        click.echo(json.dumps({"spec": spec, "open": False, "removed": removed}))
    else:
        click.echo(f"Closed lane for {spec}" + ("" if removed else " (no ticket)"))
