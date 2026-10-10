"""`ck bench`: compare agent configurations by running whole specs and scoring them on hidden tests."""

import json
import shlex
import sys
from pathlib import Path

import click

from .. import bench as b


def _fail(e: Exception) -> None:
    click.echo(f"❌ {e}")
    sys.exit(2)


@click.group()
def bench():
    """Benchmark model configurations on whole-spec runs.

    \b
      ck bench run bench.json        run every (spec, config, rep) arm, one at a time
      ck bench score bench-results   hidden-suite pass rate, cost, CIs per arm
      ck bench import out.json ...   turn an existing workflow output into a result
    See the README section "Benchmarking model configurations".
    """


@bench.command("run")
@click.argument("config", type=click.Path(exists=True, dir_okay=False))
@click.option("--dry-run", is_flag=True, help="Print the arms in run order and exit")
@click.option("--force", is_flag=True, help="Re-run arms that already have a result")
@click.option("--only", multiple=True, metavar="ARM_ID", help="Run only these arms (repeatable), e.g. calc--haiku--r1")
@click.option("--runner", default=None, help="Command used instead of `ck` to launch `run` (testing; e.g. a fake)")
def run_cmd(config: str, dry_run: bool, force: bool, only: tuple, runner: str):
    """Run every arm in CONFIG sequentially, each in a fresh clone at the pinned base.

    Arms run in a seeded random order. An arm with a results/<arm>.json is
    skipped unless --force, so an interrupted bench resumes where it stopped.
    """
    try:
        cfg = b.load_config(Path(config))
    except b.BenchError as e:
        _fail(e)
    if runner:
        cfg["runner"] = shlex.split(runner)
    arms = [a for a in b.plan_arms(cfg) if not only or a["id"] in only]
    results = Path(cfg["results_dir"])
    if dry_run:
        click.echo(f"{len(arms)} arm(s), seed {cfg['seed']}, clones in {cfg['bench_dir']}, results in {results}:")
        for i, a in enumerate(arms, 1):
            done = " (done)" if (results / f"{a['id']}.json").exists() else ""
            click.echo(f"  {i:3}. {a['id']}{done}")
        return
    failed = 0
    for i, a in enumerate(arms, 1):
        if (results / f"{a['id']}.json").exists() and not force:
            click.echo(f"⏭  skip {a['id']}: result exists (use --force to re-run)")
            continue
        click.echo(f"[{i}/{len(arms)}] {a['id']}")
        try:
            r = b.run_arm(cfg, a, echo=click.echo)
        except b.BenchError as e:
            failed += 1
            click.echo(f"❌ {a['id']}: {e}")
            continue
        rep = r["report"]
        cost = r["cost"]["usd"]
        click.echo(
            f"{'⚠ fail-closed' if r['fail_closed'] else '✓'} {a['id']}: merged {len(rep['merged'])}, failed {len(rep['failed'])}, "
            f"not started {len(rep['not_started'])}, {r['wall_minutes']:.1f} min, "
            + (f"${cost:.2f}" if cost is not None else "cost unknown")
            + ("" if rep["found"] else " — no workflow report found")
        )
        for reason in r["fail_closed_reasons"]:
            click.echo(f"    {reason}")
    if failed:
        click.echo(f"❌ {failed} arm(s) could not be set up")
        sys.exit(1)


@bench.command("score")
@click.argument("results_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--config", "config", type=click.Path(exists=True, dir_okay=False), help="Bench config (hidden suites, invariants); default: the settings recorded in each result")
@click.option("--seed", default=0, show_default=True, help="Bootstrap seed")
@click.option("--boot", default=2000, show_default=True, help="Bootstrap resamples")
@click.option("--baseline", default=None, help="Arm the others are paired against (default: the first config)")
@click.option("--cluster", type=click.Choice(["task", "spec"]), default="task", show_default=True, help="Resampling unit for paired differences")
@click.option("--out", type=click.Path(file_okay=False), default=None, help="Where to write score.json and score.md (default RESULTS_DIR)")
def score_cmd(results_dir: str, config: str, seed: int, boot: int, baseline: str, cluster: str, out: str):
    """Score every result in RESULTS_DIR on its hidden suite and invariants; write score.json and score.md."""
    try:
        cfg = b.load_config(Path(config)) if config else None
        score = b.score_results(Path(results_dir), cfg, seed=seed, n_boot=boot, baseline=baseline, cluster=cluster)
    except b.BenchError as e:
        _fail(e)
    dest = Path(out or results_dir)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "score.json").write_text(json.dumps(score, indent=2))
    md = b.score_markdown(score)
    (dest / "score.md").write_text(md)
    click.echo(md)
    click.echo(f"Wrote {dest / 'score.json'} and {dest / 'score.md'}")


@bench.command("import")
@click.argument("output", type=click.Path(exists=True, dir_okay=False))
@click.option("--spec", required=True, help="Spec name the run implemented")
@click.option("--config", "config_name", required=True, help="Arm name to file it under")
@click.option("--rep", default=1, show_default=True, help="Repetition number")
@click.option("--out", type=click.Path(file_okay=False), required=True, help="Results directory")
@click.option("--transcripts", type=click.Path(exists=True, file_okay=False), default=None,
              help="Directory with the run's agent-*.jsonl transcripts (<claude home>/projects/<dir>/<session>/subagents/workflows/<runId>), for real token usage")
@click.option("--clone", type=click.Path(exists=True, file_okay=False), default=None, help="Repository holding the run's integration branch, to score its tree")
@click.option("--ref", default=None, help="Branch or commit of the final tree (default: the report's `into`)")
@click.option("--pricing", type=click.Path(exists=True, dir_okay=False), default=None, help="JSON overriding the price table, e.g. {\"opus\": {\"input\": 5}}")
def import_cmd(output: str, spec: str, config_name: str, rep: int, out: str, transcripts: str, clone: str, ref: str, pricing: str):
    """Convert an existing workflow output file (JSON with result and workflowProgress) into a result."""
    try:
        prices = json.loads(Path(pricing).read_text()) if pricing else None
        r = b.import_output(Path(output), spec, config_name, rep, prices, Path(transcripts) if transcripts else None,
                            Path(clone) if clone else None, ref)
    except (b.BenchError, json.JSONDecodeError) as e:
        _fail(e)
    dest = Path(out)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{r['id']}.json"
    path.write_text(json.dumps(r, indent=2))
    cost = r["cost"]["usd"]
    click.echo(f"✓ {path}: merged {len(r['report']['merged'])}, failed {len(r['report']['failed'])}, "
               + (f"~${cost:.2f} ({r['cost']['basis']})" if cost is not None else "cost unknown")
               + (" — fail-closed" if r["fail_closed"] else ""))
