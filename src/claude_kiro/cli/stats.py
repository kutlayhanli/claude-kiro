"""`ck stats`: collect and look at run analytics. The CLI only does I/O; rules live in `claude_kiro.analytics`."""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import click

from .. import agents as ag
from .. import bench
from ..analytics import collect as col
from ..analytics import runfile, store

RUN_ID_RE = re.compile(r"^wf_[A-Za-z0-9_-]{1,128}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _classify(home: Path) -> str:
    exists = home.is_dir()
    listable = True
    if exists:
        try:
            projects = home / "projects"
            os.listdir(projects if projects.exists() else home)
        except OSError:
            listable = False
    return runfile.classify_home(exists, listable, os.environ.get("CK_RUN_SANDBOXED") == "1")


def _sandbox_text(home: Path) -> str:
    return f"ck stats collect must run outside the sandbox: {home} is not readable here"


def _known_allowlisted(home: Path, run_id: str) -> bool:
    try:
        p = runfile.find_run_file(home, run_id)
        if p is not None:
            return runfile.is_allowlisted(runfile.peek_workflow_name(p))
        orphan = runfile.find_orphan_run(home, run_id)
        if orphan is not None and orphan.session_jsonl is not None:
            name = runfile.recover_workflow_name(runfile.workflow_launches(orphan.session_jsonl), run_id)
            return runfile.is_allowlisted(name)
    except Exception:  # noqa: BLE001
        return False
    return False


def _failure_text(run_id: str, outcome: col.CollectOutcome, home: Path) -> str:
    """Console text of one failed single-run collect. Never names a workflow."""
    err = outcome.error
    if err == "not_allowlisted":
        return f"{run_id} is not a ck spec workflow run; not collected"
    if err == "unrecognised":
        detail = outcome.reason or "run file format not recognised"
        if detail.startswith("run file format not recognised"):
            tail = detail.split(": ", 1)[1] if ": " in detail else ""
            return f"{run_id}: run-file format not recognised" + (f" ({tail})" if tail else "")
        return f"{run_id}: run-file format not recognised"
    if err == "not_found":
        return f"run {run_id} not found under {home}"
    return f"{run_id}: {outcome.reason or 'could not be collected'}"


def _write(outcome: col.CollectOutcome) -> Tuple[Optional[Path], str]:
    return col.write_outcome_result(outcome)


def collect_auto(run_id: str, *, home: Optional[Path] = None) -> int:
    """`collect --run RUN_ID --auto`: always 0; a failure is logged and the retry command printed."""
    retry = f"ck stats collect --run {run_id}"
    try:
        try:
            enabled = col.auto_collect_enabled(ag.resolve(Path.cwd())["roles"]["run"])
        except ag.AgentConfigError:
            enabled = True
        if not enabled:
            click.echo("ℹ️ auto-collect is off (ck agents set run.analytics true)")
            return 0
        home = home or bench.claude_home()
        if not RUN_ID_RE.match(run_id):
            raise ValueError("run id is not valid")
        if _classify(home) == "sandboxed":
            click.echo(f"ℹ️ {run_id}: ck run collects this run after the session exits")
            return 0
        outcome = col.collect_run(home, run_id, now=_now(), fresh=True)
        if outcome.kind == "unfinished":
            click.echo(f"ℹ️ {run_id} has not finished; nothing written")
            return 0
        if outcome.kind == "collected":
            path, _kind = _write(outcome)
            partial = any(m.get("what") == "runfile" for m in (outcome.record or {}).get("missing", []))
            click.echo(f"✓ {run_id}: wrote {path}" + (" (partial: run file missing)" if partial else ""))
            return 0
        reason = _failure_text(run_id, outcome, home)
        kind = outcome.error or "failed"
        _log_failure(home, run_id, kind, reason)
        click.echo(f"⚠ could not collect {run_id}: {reason}; retry: {retry}")
    except Exception as e:  # noqa: BLE001 - auto collection must never change the caller's result
        msg = f"{type(e).__name__}: {str(e)[:200]}"
        try:
            _log_failure(home or Path(""), run_id, "error", msg)
        except Exception:  # noqa: BLE001
            pass
        click.echo(f"⚠ could not collect {run_id}: {msg}; retry: {retry}")
    return 0


def _log_failure(home: Path, run_id: str, kind: str, message: str) -> None:
    known = _known_allowlisted(home, run_id) if str(home) else False
    if known:
        line = f"{_utc_iso()} {run_id} {kind}: {message}"
    else:
        line = f"{_utc_iso()} - {kind}"
    store.append_collect_log(line)


@click.group(invoke_without_command=True)
@click.pass_context
def stats(ctx: click.Context):
    """Run analytics: what each ck run cost and how it went."""
    if ctx.invoked_subcommand:
        return
    click.echo(ctx.get_help())


@stats.command("collect")
@click.option("--run", "run_id", default=None, metavar="RUN_ID", help="Collect one run")
@click.option("--all", "collect_everything", is_flag=True, help="Collect every ck run under the Claude home")
@click.option("--auto", is_flag=True, help="Quiet mode for ck run and the skills: always exits 0")
@click.option("--json", "as_json", is_flag=True)
def collect_cmd(run_id: Optional[str], collect_everything: bool, auto: bool, as_json: bool):
    """Collect a finished run into a local record (never leaves this machine)."""
    if bool(run_id) == bool(collect_everything):
        click.echo("❌ give exactly one of --run RUN_ID or --all")
        sys.exit(2)
    if run_id is not None and not RUN_ID_RE.match(run_id):
        click.echo("❌ RUN_ID must look like wf_ followed by letters, digits, - or _")
        sys.exit(2)
    if auto:
        if run_id is None:
            click.echo("❌ --auto needs --run RUN_ID")
            sys.exit(2)
        sys.exit(collect_auto(run_id))
    home = bench.claude_home()
    if _classify(home) == "sandboxed":
        click.echo(f"❌ {_sandbox_text(home)}")
        sys.exit(1)
    if run_id is not None:
        _collect_one(home, run_id, as_json)
    else:
        _collect_all(home, as_json)


def _collect_one(home: Path, run_id: str, as_json: bool) -> None:
    outcome = col.collect_run(home, run_id, now=_now())
    if outcome.kind == "unfinished":
        click.echo(json.dumps({"run_id": run_id, "result": "unfinished"}) if as_json else f"ℹ️ {run_id} has not finished; nothing written")
        return
    if outcome.kind == "collected":
        path, kind = _write(outcome)
        partial = any(m.get("what") == "runfile" for m in (outcome.record or {}).get("missing", []))
        if as_json:
            click.echo(json.dumps({"run_id": run_id, "result": kind, "path": str(path), "partial": partial}))
        else:
            click.echo(f"✓ {run_id}: wrote {path}" + (" (partial: run file missing)" if partial else ""))
        return
    click.echo(json.dumps({"run_id": run_id, "result": "failed", "error": outcome.error}) if as_json
               else f"❌ {_failure_text(run_id, outcome, home)}")
    sys.exit(1)


def _collect_all(home: Path, as_json: bool) -> None:
    summary = col.collect_all(home, now=_now())
    kinds: List[str] = []
    failures: List[Tuple[str, str]] = []
    skipped = 0
    for o in summary.outcomes:
        if o.kind == "skipped_non_ck":
            skipped += 1
            continue
        if o.kind == "collected":
            try:
                _path, k = _write(o)
            except Exception as e:  # noqa: BLE001
                kinds.append("failed")
                failures.append((o.run_id, f"{type(e).__name__}: {str(e)[:200]}"))
                continue
            kinds.append(k)
        else:
            kinds.append(o.kind)
            if o.kind == "failed":
                failures.append((o.run_id, o.reason or "failed"))
    counts, code = col.summarize_outcomes(kinds, skipped)
    if as_json:
        click.echo(json.dumps({"collected": counts.collected, "already_had": counts.unchanged, "updated": counts.updated,
                               "unfinished": counts.unfinished, "failed": counts.failed,
                               "failures": [{"run_id": r, "reason": m} for r, m in failures],
                               "skipped_non_ck": counts.skipped_non_ck}))
        sys.exit(code)
    if not kinds:
        click.echo(f"ℹ️ no ck spec runs found under {home} ({skipped} other workflow runs skipped)")
        click.echo(f"skipped non-ck: {skipped}")
        sys.exit(0)
    click.echo(f"collected: {counts.collected}")
    click.echo(f"already had: {counts.unchanged}")
    click.echo(f"updated: {counts.updated}")
    click.echo(f"unfinished: {counts.unfinished}")
    click.echo(f"failed: {counts.failed}")
    for rid, reason in failures:
        click.echo(f"  {rid}: {reason}")
    click.echo(f"skipped non-ck: {counts.skipped_non_ck}")
    sys.exit(code)


@stats.command("label")
@click.argument("slug", required=False)
@click.option("--clear", is_flag=True, help="Remove the machine label")
def label_cmd(slug: Optional[str], clear: bool):
    """Show, set or clear the machine label (alias of `ck agents set run.machine_label`)."""
    if slug is not None and clear:
        click.echo("❌ give a SLUG or --clear, not both")
        sys.exit(2)
    if slug is None and not clear:
        try:
            current = ag.resolve(Path.cwd())["roles"]["run"].get("machine_label")
        except ag.AgentConfigError as e:
            click.echo(f"❌ {e}")
            sys.exit(2)
        click.echo(current or "(none)")
        return
    value = "none" if clear else slug
    try:
        ag.write_setting(ag.global_config_path(), "run", "machine_label", value)
    except ag.AgentConfigError as e:
        click.echo(f"❌ {e}")
        sys.exit(2)
    click.echo("✓ machine label cleared" if clear else f"✓ machine label = {slug}")
