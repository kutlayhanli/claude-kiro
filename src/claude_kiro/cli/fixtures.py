"""`ck fixtures mark` and `ck fixtures status` (Story 15)."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import click

from .. import fixtures as fx
from .. import protect
from ..config import ConfigError, load_config
from .worktree import _spec_dir, _spec_name


@click.group("fixtures")
def fixtures():
    """Recorded-fixture lock: label handcrafted fixtures, show lock status."""


@fixtures.command("mark")
@click.argument("path")
@click.option("--handcrafted", "reason", required=True, help="Why this fixture is handcrafted, not captured.")
def mark(path: str, reason: str):
    """Label PATH handcrafted: records the reason and the sha256 of its current content."""
    if not reason.strip():
        click.echo("A handcrafted label needs a non-blank reason.", err=True)
        sys.exit(2)
    project = Path.cwd()
    target = project / path
    if not target.is_file():
        click.echo(f"No such file: {path}", err=True)
        sys.exit(2)
    rel = target.resolve().relative_to(project.resolve()).as_posix()
    config = load_config(project)
    written = fx.write_label(project, config, rel, reason, fx.sha256_text(target.read_bytes()))
    click.echo(f"Labelled {rel} handcrafted in {written.relative_to(project)}")


def _git(project: Path, *args: str) -> Optional[bytes]:
    r = subprocess.run(["git", "-C", str(project), *args], capture_output=True)
    return r.stdout if r.returncode == 0 else None


@fixtures.command("status")
@click.argument("spec")
@click.option("--task", "task", default=None, help="Only report drift attributed to this task.")
@click.option("--json", "as_json", is_flag=True)
def status(spec: str, task: Optional[str], as_json: bool):
    """Locked fixtures, drift against the lock, and fixtures without capture metadata."""
    project = Path.cwd()
    name = _spec_name(spec)
    config = load_config(project)
    out: Dict[str, object] = {"locked": [], "drift": [], "unlabeled": [], "configError": None}
    try:
        globs_on = bool(fx.fixture_globs(config))
    except ConfigError as e:
        out["configError"] = str(e)
        if as_json:
            click.echo(json.dumps(out, indent=2))
        click.echo(str(e), err=True)
        sys.exit(1)
    if not globs_on:
        click.echo(json.dumps(out, indent=2) if as_json else "No fixtures.recorded globs: nothing locked.")
        return
    ref = f"integrate/{name}"
    if _git(project, "rev-parse", "--verify", "--quiet", ref) is None:
        ref = "HEAD"
    listing = _git(project, "ls-tree", "-r", "-z", "--name-only", ref) or b""
    paths = {p.decode("utf-8", "replace") for p in listing.split(b"\0") if p}
    # The integration branch is the truth for landed content; files only in this checkout count too.
    paths |= {p.relative_to(project).as_posix() for p in project.rglob("*")
              if p.is_file() and ".git" not in p.relative_to(project).parts}
    changes: Dict[str, Optional[str]] = {}
    for p in fx.recorded_files(config, sorted(paths)):
        blob = _git(project, "show", f"{ref}:{p}")
        if blob is None and (project / p).is_file():
            blob = (project / p).read_bytes()
        changes[p] = None if blob is None else fx.sha256_text(blob)
    lock, tampered = fx.read_lock(project, name)
    for p in lock:
        changes.setdefault(p, None)
    label_text = _git(project, "show", f"{ref}:{fx.labels_path(config)}")
    labels = fx.parse_labels(label_text.decode("utf-8", "replace") if label_text else None)
    labels.update(fx.read_labels(project, config))
    report = fx.lock_decision(lock, changes, labels, tampered=tampered)
    drift = [d for d in report.drift if task is None or str(d["task"]) == str(task)]
    out["locked"] = [{"path": p, "sha256": e.sha256, "task": e.task, "locked_at": e.locked_at,
                      "handcrafted": e.handcrafted} for p, e in sorted(lock.items())
                     if task is None or str(e.task) == str(task)]
    out["drift"] = [dict(d) for d in drift]
    out["unlabeled"] = list(report.unlabeled)
    out["lockTampered"] = report.lock_tampered
    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    click.echo(f"{len(out['locked'])} locked, {len(drift)} drifted, {len(report.unlabeled)} unlabeled")
    for d in drift:
        click.echo(f"  drift: {d['file']} ({d['change']}, task {d['task']})")
    for p in report.unlabeled:
        click.echo(f"  unlabeled: {p}")
    if report.lock_tampered:
        click.echo("  the fixture lock was tampered with")
