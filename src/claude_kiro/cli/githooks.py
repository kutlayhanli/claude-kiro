"""Hidden `ck git-hook ...` commands that ck's git hooks call (see protect.install_hooks)."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import click

from .. import protect


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True)


def _text(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.decode("utf-8", "replace").strip() if proc.returncode == 0 else ""


def _allow_from(root: Path, rev: str) -> List[str]:
    """land.allow_paths of specs/ck.json as it is at `rev` (the ref's old value, never the incoming commit)."""
    proc = _git(root, "show", f"{rev}:specs/ck.json")
    if proc.returncode != 0:
        return []
    try:
        land = json.loads(proc.stdout.decode("utf-8", "replace")).get("land")
        allow = land.get("allow_paths") if isinstance(land, dict) else None
    except (ValueError, AttributeError):
        return []
    return [a for a in allow if isinstance(a, str)] if isinstance(allow, list) else []


def _names(out: bytes) -> List[str]:
    return [n.decode("utf-8", "replace") for n in out.split(b"\0") if n]


def _norm(oid: str) -> str:
    return protect.ZEROS if oid and set(oid) == {"0"} else oid


def _shape(root: Path, commit: str) -> Tuple[Optional[str], Tuple[str, ...]]:
    tree = _text(_git(root, "rev-parse", "--verify", "-q", f"{commit}^{{tree}}")) or None
    parents = tuple(_text(_git(root, "show", "-s", "--format=%P", commit)).split())
    return tree, parents


@click.group("git-hook", hidden=True)
def git_hook():
    """Called by ck's git hooks; not for direct use."""


@git_hook.command("reference-transaction")
@click.argument("state")
def reference_transaction(state):
    if state != "prepared":
        return
    root = Path.cwd()
    token_id = os.environ.get("CK_LANDING_TOKEN", "")
    failed = False
    used: List[Tuple[str, protect.RefUpdate]] = []
    for line in sys.stdin.read().splitlines():
        parts = line.split(" ")
        if len(parts) != 3 or not parts[2].startswith(protect.INTEGRATE_PREFIX):
            continue
        old, new, ref = _norm(parts[0]), _norm(parts[1]), parts[2]
        if not (re.fullmatch(r"[0-9a-f]{40,64}", old) and re.fullmatch(r"[0-9a-f]{40,64}", new)):
            continue
        if new == protect.ZEROS:
            continue
        tree, parents = _shape(root, new)
        update = protect.RefUpdate(ref, old, new, tree, parents)
        token_ok = bool(token_id) and protect.check_token(root, token_id, update)
        if update.is_create:
            approved = protect.approved_state_for_ref(root, ref)
            decision = protect.ref_update_decision(
                update, protect.protected_files_at(root, new), [], token_ok,
                dict(approved.files) if approved is not None else None)
        else:
            changed = _names(_git(root, "diff", "--name-only", "--no-renames", "-z", old, new).stdout)
            decision = protect.ref_update_decision(update, changed, _allow_from(root, old), token_ok, None)
        if not decision.allow:
            failed = True
            click.echo(decision.message, err=True)
        elif token_ok:
            used.append((token_id, update))
    if failed:
        sys.exit(1)
    for tid, update in used:
        protect.consume_token(root, tid, update)


@git_hook.command("pre-merge-commit")
def pre_merge_commit():
    root = Path.cwd()
    head_ref = _text(_git(root, "symbolic-ref", "-q", "HEAD"))
    if not head_ref.startswith(protect.INTEGRATE_PREFIX):
        return
    head = _text(_git(root, "rev-parse", "HEAD"))
    changed = _names(_git(root, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD").stdout)
    present = False
    loaded = protect.load_token(root, os.environ.get("CK_LANDING_TOKEN", ""))
    if loaded is not None:
        rec, used, mac_ok = loaded
        present = mac_ok and not used and rec.ref == head_ref and rec.old == head
    decision = protect.merge_commit_decision(head_ref, changed, _allow_from(root, "HEAD"), present)
    if not decision.allow:
        click.echo(decision.message, err=True)
        sys.exit(1)
