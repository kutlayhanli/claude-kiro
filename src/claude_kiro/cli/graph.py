"""`ck graph SPEC [--json]`: the spec graph (tasks, TCs, test and source files and their edges)."""

import json
from collections import Counter
from pathlib import Path

import click

from ..graph import EDGE_KINDS, NODE_KINDS, build_graph
from .worktree import _spec_dir


@click.command("graph")
@click.argument("spec")
@click.option("--json", "as_json", is_flag=True, help="Print the ck-graph/1 document.")
def graph(spec: str, as_json: bool):
    """Build SPEC's graph from its files (fresh on every call) and print it.

    Malformed sources are listed as problems; the rest of the graph is still printed.
    """
    project = Path.cwd()
    spec_dir = _spec_dir(project, spec)
    result = build_graph(project, spec_dir)
    if as_json:
        click.echo(json.dumps(result.to_json(), indent=2))
        return
    nodes = Counter(n.kind for n in result.nodes)
    edges = Counter(e.kind for e in result.edges)
    click.echo(f"Spec graph for {result.spec}:")
    click.echo("  nodes: " + ", ".join(f"{k} {nodes.get(k, 0)}" for k in NODE_KINDS))
    click.echo("  edges: " + ", ".join(f"{k} {edges.get(k, 0)}" for k in EDGE_KINDS))
    if result.problems:
        click.echo(f"  problems ({len(result.problems)}):")
        for problem in result.problems:
            click.echo(f"    - {problem.source}: {problem.problem}")
    else:
        click.echo("  no problems")
