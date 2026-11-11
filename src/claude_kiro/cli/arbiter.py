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

from ..arbiter import close_lane, open_lane
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
