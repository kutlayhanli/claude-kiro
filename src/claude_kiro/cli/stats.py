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
            if os.environ.get("CK_RUN_SANDBOXED") == "1":
                click.echo(f"ℹ️ {run_id}: ck run collects this run after the session exits")
                return 0
            reason = _sandbox_text(home)
            _log_failure(home, run_id, "sandboxed", reason)
            click.echo(f"⚠ could not collect {run_id}: {reason}; retry: {retry}")
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
            _log_failure(home, run_id, "error", msg)
        except Exception:  # noqa: BLE001
            pass
        click.echo(f"⚠ could not collect {run_id}: {msg}; retry: {retry}")
    return 0


def _log_failure(home: Optional[Path], run_id: str, kind: str, message: str) -> None:
    known = home is not None and RUN_ID_RE.match(run_id) is not None and _known_allowlisted(home, run_id)
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
    ctx.invoke(show_cmd, run_id=None, kind=None, since=None, until=None, project=None, as_json=False)


_DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


def _date_arg(name: str, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    ok = bool(_DATE_RE.match(value))
    if ok:
        try:
            datetime.strptime(value + ("-01" if len(value) == 7 else ""), "%Y-%m-%d")
        except ValueError:
            ok = False
    if not ok:
        click.echo(f"❌ {name} must look like YYYY-MM or YYYY-MM-DD")
        sys.exit(2)
    return value


@stats.command("show")
@click.argument("run_id", required=False)
@click.option("--kind", default=None, help="Only runs of this workflow kind")
@click.option("--since", default=None, help="From YYYY-MM or YYYY-MM-DD")
@click.option("--until", default=None, help="Up to YYYY-MM or YYYY-MM-DD")
@click.option("--project", default=None, help="Only runs of this project pseudonym")
@click.option("--json", "as_json", is_flag=True)
def show_cmd(run_id: Optional[str], kind: Optional[str], since: Optional[str], until: Optional[str],
             project: Optional[str], as_json: bool):
    """List collected runs, or break one run down."""
    from ..analytics import show as sh

    since = _date_arg("--since", since)
    until = _date_arg("--until", until)
    records, unreadable = store.load_records_counted()
    if run_id is not None:
        rec = next((r for r in records if r.get("runId") == run_id), None)
        if rec is None:
            click.echo(f"❌ no local record for {run_id} (ck stats collect --run {run_id})")
            sys.exit(1)
        view = sh.run_breakdown(rec)
        if as_json:
            click.echo(json.dumps(sh.run_json(view), indent=2, default=str))
        else:
            click.echo("\n".join(sh.render_run(view)))
        return
    if not records and not unreadable:
        if as_json:
            click.echo(json.dumps(sh.list_json({"runs": [], "trends": [], "unreadable": 0}), indent=2))
        else:
            click.echo("ℹ️ no runs collected yet; run ck stats collect --all")
        return
    view = sh.list_runs(records, sh.ShowFilters(kind=kind, since=since, until=until, project=project), unreadable)
    if as_json:
        click.echo(json.dumps(sh.list_json(view), indent=2, default=str))
    else:
        click.echo("\n".join(sh.render_list(view)))
        if unreadable:
            click.echo(f"⚠ {unreadable} record file(s) could not be read")


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
    if run_id is not None and not auto and not RUN_ID_RE.match(run_id):
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


# --------------------------------------------------------------------------- export

NOTE_PATH = "issues-note.md"


def _checkout_id(checkout: Path) -> str:
    import hashlib

    return hashlib.sha256(str(checkout.resolve()).encode("utf-8")).hexdigest()


def _present_files(checkout: Path, ledger: dict) -> frozenset:
    out = set()
    for entry in (ledger.get("exported") or {}).values():
        files = entry.get("files", []) if isinstance(entry, dict) else []
        for rel in files if isinstance(files, list) else []:
            if isinstance(rel, str) and (checkout / rel).is_file():
                out.add(rel)
    return frozenset(out)


def _echo_hit(path: str, hit) -> None:
    click.echo(f"{path}: {hit.field}: {hit.kind}")


def _review_note(rid: str, local: dict, codes: List[str], terms) -> Optional[str]:
    """Interactive review of one drafted note: returns the text to include, or None."""
    from ..analytics import issues, leak

    click.echo(f"\nIssues note for {rid}:")
    draft = issues.draft_note(local, codes)
    if draft.text is None:
        click.echo(f"⚠ could not draft a note: {draft.error}")
        try:
            pick = click.prompt("write one by hand or skip [w/s]", default="s", show_default=False).strip().lower()
        except click.Abort:
            return None
        if not pick.startswith("w"):
            return None
        text = click.edit("") or ""
        if not text.strip():
            return None
    else:
        text = draft.text
    while True:
        hits = leak.scan({NOTE_PATH: text}, terms)
        bad_lines = {h.field for h in hits}
        click.echo("-----")
        for n, line in enumerate(text.splitlines(), 1):
            mark = "!" if f"line {n}" in bad_lines else " "
            click.echo(f"{mark} {line}")
        click.echo("-----")
        for h in hits:
            _echo_hit(NOTE_PATH, h)
        try:
            pick = click.prompt("accept / edit / discard [a/e/d]", default="d", show_default=False).strip().lower()
        except click.Abort:
            return None
        if pick.startswith("e"):
            edited = click.edit(text)
            if edited is not None:
                text = edited
            continue
        if pick.startswith("a"):
            final = leak.scan({NOTE_PATH: text}, terms)
            decision = issues.decide_note(True, True, False, "accept", len(final))
            if decision == "include":
                return text
            for h in final:
                _echo_hit(NOTE_PATH, h)
            click.echo("⚠ note not included: it still matches the leak check")
            return None
        return None


@stats.command("export")
@click.option("--to", "to", default=None, type=click.Path(), help="The claude-kiro checkout to write into (default: here)")
@click.option("--re-export", "--reexport", "reexport", multiple=True, metavar="RUN_ID", help="Export this run again")
@click.option("--note", "--notes", "note", is_flag=True, help="Draft an issues note per record (interactive only)")
@click.option("--yes", is_flag=True, help="Do not ask")
def export_cmd(to: Optional[str], reexport: Tuple[str, ...], note: bool, yes: bool):
    """Write shareable records into a claude-kiro checkout (nothing leaves this machine)."""
    from ..analytics import export as ex
    from ..analytics import issues, leak

    checkout = Path(to) if to else Path.cwd()
    if not checkout.is_dir() or not any(ex.is_target_repo(u) for _n, u in ex.remote_urls(checkout)):
        click.echo("❌ not a kutlayhanli/claude-kiro checkout; nothing written")
        sys.exit(1)
    checkout = checkout.resolve()
    records, _bad = store.load_records_counted()
    ledger = store.load_ledger()
    cid = _checkout_id(checkout)
    plan = ex.plan_export(records, ledger, list(reexport), cid, _present_files(checkout, ledger))
    if plan.nothing_new:
        click.echo("ℹ️ nothing new to export")
        return
    if plan.restore:
        click.echo(f"ℹ️ re-exported {len(plan.restore)} record(s) whose files were missing")

    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not yes
    identity = leak.git_identity(checkout)
    non_ck = leak.non_ck_workflow_names(bench.claude_home())
    terms = leak.build_terms(records, identity, non_ck)
    notes = {}
    if note and not interactive:
        click.echo("⚠ issues note withheld: needs interactive approval")
    elif note:
        for rid in sorted(plan.records):
            local = plan.records[rid]
            text = _review_note(rid, local, issues.issue_codes(local), terms)
            if text is not None:
                notes[rid] = text
    try:
        files = ex.build_batch(plan, machine_label=store.machine_label(), notes=notes)
    except ex.SchemaViolation as e:
        click.echo(f"❌ {e}; nothing written")
        sys.exit(1)
    gate = ex.leak_gate(files, records, identity, non_ck)
    if not gate.allowed:
        for h in gate.hits:
            _echo_hit(h.file, h)
        click.echo("❌ leak check failed; nothing written or committed")
        sys.exit(1)
    try:
        ex.remove_replaced(checkout, plan)
        ex.write_batch(checkout, files)
    except (OSError, ValueError) as e:
        click.echo(f"❌ could not write: {e}")
        sys.exit(1)
    ex.record_exported(ledger, files, cid)
    store.save_ledger(ledger)
    for f in files:
        click.echo(f"✓ wrote {f.rel_path}")
