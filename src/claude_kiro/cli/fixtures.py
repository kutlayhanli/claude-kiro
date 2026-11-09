"""`ck fixtures mark` and `ck fixtures status` (Story 15)."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

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
    project = _toplevel()
    target = Path(path) if Path(path).is_absolute() else Path.cwd() / path
    if not target.is_file():
        click.echo(f"No such file: {path}", err=True)
        sys.exit(2)
    try:
        rel = target.resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        click.echo(f"{path} is outside the project {project}", err=True)
        sys.exit(2)
    config = load_config(project)
    try:
        written = fx.write_label(project, config, rel, reason, fx.sha256_text(target.read_bytes()))
    except ValueError as e:
        click.echo(str(e), err=True)
        sys.exit(2)
    click.echo(f"Labelled {rel} handcrafted in {written.relative_to(project)}")


def _git(project: Path, *args: str) -> Optional[bytes]:
    r = subprocess.run(["git", "-C", str(project), *args], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _toplevel() -> Path:
    out = _git(Path.cwd(), "rev-parse", "--show-toplevel")
    if out is None:
        click.echo("Not in a git repository.", err=True)
        sys.exit(2)
    return Path(out.decode("utf-8", "replace").strip())


@fixtures.command("status")
@click.argument("spec")
@click.option("--task", "task", default=None, help="Scope to one task: its locked files, drift attributed to it, and unlabeled fixtures it owns or has added/changed here.")
@click.option("--json", "as_json", is_flag=True)
def status(spec: str, task: Optional[str], as_json: bool):
    """Locked fixtures, drift against the lock, and fixtures without capture metadata."""
    project = _toplevel()
    _spec_dir(project, spec)  # exits 2 for an unknown spec
    name = _spec_name(spec)
    try:
        protect._spec_dir(project, name)
    except ValueError as e:
        click.echo(str(e), err=True)
        sys.exit(2)
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
    on_ref = {p.decode("utf-8", "replace") for p in listing.split(b"\0") if p}
    # Checkout files: tracked or untracked, never ignored (.ck/worktrees, node_modules, .venv, build output).
    local_listing = _git(project, "ls-files", "-z", "--cached", "--others", "--exclude-standard") or b""
    local = {p.decode("utf-8", "replace") for p in local_listing.split(b"\0") if p}
    local = {p for p in local if (project / p).is_file()}
    lock, tampered = fx.read_lock(project, name)
    changes: Dict[str, Optional[str]] = {}
    unlanded_changed = set()  # unlocked fixtures new or changed in this checkout relative to the ref
    for p in fx.recorded_files(config, sorted(on_ref | local | set(lock))):
        blob = _git(project, "show", f"{ref}:{p}")
        if p in lock:
            # A locked path missing on the ref was deleted there, whatever this checkout still holds.
            changes[p] = None if blob is None else fx.sha256_text(blob)
            continue
        local_blob = (project / p).read_bytes() if p in local else None
        shown = blob if blob is not None else local_blob
        changes[p] = None if shown is None else fx.sha256_text(shown)
        if local_blob is not None and (blob is None or blob != local_blob):
            unlanded_changed.add(p)
    label_text = _git(project, "show", f"{ref}:{fx.labels_path(config)}")
    labels = fx.parse_labels(label_text.decode("utf-8", "replace") if label_text else None)
    # The landed label governs landed (locked) fixtures; checkout labels only cover fixtures not yet landed.
    for p, lab in fx.read_labels(project, config).items():
        if p not in lock:
            labels[p] = lab
    report = fx.lock_decision(lock, changes, labels, tampered=tampered)
    drift = [d for d in report.drift if task is None or str(d["task"]) == str(task)]
    out["locked"] = [{"path": p, "sha256": e.sha256, "task": e.task, "locked_at": e.locked_at,
                      "handcrafted": e.handcrafted} for p, e in sorted(lock.items())
                     if task is None or str(e.task) == str(task)]
    out["drift"] = [dict(d) for d in drift]
    unlabeled = [p for p in report.unlabeled
                 if task is None or (p in lock and str(lock[p].task) == str(task))
                 or (p not in lock and p in unlanded_changed)]
    out["unlabeled"] = unlabeled
    out["lockTampered"] = report.lock_tampered
    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    click.echo(f"{len(out['locked'])} locked, {len(drift)} drifted, {len(unlabeled)} unlabeled")
    for d in drift:
        click.echo(f"  drift: {d['file']} ({d['change']}, task {d['task']})")
    for p in unlabeled:
        click.echo(f"  unlabeled: {p}")
    if report.lock_tampered:
        click.echo("  the fixture lock was tampered with")
