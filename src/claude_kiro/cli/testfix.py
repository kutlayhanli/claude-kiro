"""`ck testfix check|prove|record` (Story 4): the one-fix ledger, scope and proof of an automatic test fix."""

import json
import sys
from typing import Any, Tuple

import click

from .. import testfix as tf
from .. import worktrees as wt
from .worktree import _root, _spec_name


def _out(obj: Any) -> None:
    click.echo(json.dumps(obj, indent=2))


@click.group("testfix")
def testfix():
    """Test-fix ledger, scope check and proof."""


@testfix.command("check")
@click.argument("spec")
@click.option("--test", "nodes", multiple=True, required=True, help="Test node id (repeatable)")
@click.option("--json", "as_json", is_flag=True)
def check_cmd(spec: str, nodes: Tuple[str, ...], as_json: bool):
    """Which NODES may still get an automatic fix: a node is fixed once, ever."""
    root = _root()
    try:
        ledger = tf.load_ledger(tf.ledger_path(root, _spec_name(spec)))
    except tf.TestFixError as e:
        # A damaged ledger never grants: every node is reported as already fixed.
        bad = [{"node": n, "at": "", "fix": "unknown"} for n in dict.fromkeys(nodes)]
        _out({"allowed": [], "alreadyFixed": bad, "error": str(e)})
        sys.exit(3)
    result = tf.fix_allowed(list(nodes), ledger)
    if as_json:
        _out({"allowed": list(result.allowed), "alreadyFixed": [dict(a) for a in result.already]})
        return
    for node in result.allowed:
        click.echo(f"allowed {node}")
    for a in result.already:
        click.echo(f"already fixed {a['node']} by {a['fix']} at {a['at']}")


@testfix.command("prove")
@click.argument("spec")
@click.argument("fixid")
@click.option("--blocked", default=None, help="The blocked task: prove on its branch plus the fix")
@click.option("--held", multiple=True, help="A task a cross-test fix holds: prove on the integration branch plus the fix")
@click.option("--test", "nodes", multiple=True, required=True, help="Fixed test node id (repeatable)")
@click.option("--into", default=None, help="The integration branch (default integrate/SPEC)")
@click.option("--json", "as_json", is_flag=True)
def prove_cmd(spec, fixid, blocked, held, nodes, into, as_json):
    """Check the fix's scope, then (only in scope) prove the fixed tests."""
    if not blocked and not held:
        raise click.UsageError("give --blocked TASK or --held TASK")
    root = _root()
    try:
        result = tf.prove(root, _spec_name(spec), fixid, blocked, list(held), list(nodes), into)
    except (tf.TestFixError, wt.WorktreeError) as e:
        _out({"error": str(e), "scope": None, "proof": None, "changedTests": [], "against": None})
        sys.exit(3)
    if as_json:
        _out(result.as_json())
    else:
        click.echo(f"scope: {'in' if result.scope.in_scope else 'OUT'} - {result.scope.reason}")
        if result.proof:
            click.echo(f"proof against {result.against}: {'proved' if result.proof.proved else 'NOT proved'} - {result.proof.reason}")
    if not result.scope.in_scope or (result.proof and not result.proof.proved):
        sys.exit(1)


@testfix.command("record")
@click.argument("spec")
@click.argument("fixid")
@click.option("--test", "nodes", multiple=True, required=True, help="Fixed test node id (repeatable)")
@click.option("--outcome", type=click.Choice(["landed", "to-me"]), required=True)
@click.option("--json", "as_json", is_flag=True)
def record_cmd(spec, fixid, nodes, outcome, as_json):
    """Write the ledger entry of every node."""
    root = _root()
    try:
        ledger = tf.record_fix(root, _spec_name(spec), fixid, list(nodes), outcome)
    except tf.TestFixError as e:
        _out({"error": str(e)})
        sys.exit(3)
    if as_json:
        _out({"recorded": [tf.canonical_node(n) for n in nodes], "entries": len(ledger)})
    else:
        click.echo(f"recorded {len(nodes)} node(s) for {fixid}: {outcome}")
