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
from ..analytics import agentcollect, agentrun
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
            partial = _is_partial(outcome.record)
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


def _is_partial(record: Optional[dict]) -> bool:
    return any(m.get("what") in ("runfile", "manifest") for m in (record or {}).get("missing", []))


@stats.command("begin")
@click.option("--kind", required=True, type=click.Choice(["implement", "create", "review"]), help="Kind of agent-mode run")
@click.option("--spec", default=None, help="The spec this run works on")
@click.option("--into", default=None, help="Target branch of the run")
@click.option("--session", "session_id", default=None, help="Claude Code session id (default: the newest one here)")
@click.option("--override", "overrides", multiple=True, metavar="ROLE.KEY=VALUE", help="Per-run agent setting")
@click.option("--json", "as_json", is_flag=True)
def begin_cmd(kind: str, spec: Optional[str], into: Optional[str], session_id: Optional[str], overrides: Tuple[str, ...], as_json: bool):
    """Start an agent-mode run: print its id (ag_...) and write a run manifest."""
    if not spec or not spec.strip():
        click.echo("❌ --spec is required and must not be empty (--kind is one of: implement, create, review)")
        sys.exit(2)
    res = agentcollect.begin_run(kind=kind, spec=spec.strip(), cwd=Path.cwd(), into=into or None, session_id=session_id or None,
                                 overrides=list(overrides), home=bench.claude_home(), now=_now())
    if res.warning:
        click.echo(res.warning, err=True)
    if as_json:
        click.echo(json.dumps({"run_id": res.run_id, "manifest": str(res.manifest_path) if res.manifest_path else None}))
    else:
        click.echo(res.run_id)


@stats.command("collect")
@click.option("--run", "run_id", default=None, metavar="RUN_ID", help="Collect one run")
@click.option("--all", "collect_everything", is_flag=True, help="Collect every ck run under the Claude home")
@click.option("--agents", "agents_id", default=None, metavar="RUN_ID", help="Collect one agent-mode run (ag_...)")
@click.option("--agents-backfill", "backfill_flag", is_flag=True, help="Recover old unmarked implement-mode runs")
@click.option("--dry-run", "dry_run", is_flag=True, help="With --agents-backfill: list, write nothing")
@click.option("--gap", default=None, metavar="MINUTES", help="With --agents-backfill: idle gap that splits a run")
@click.option("--auto", is_flag=True, help="Quiet mode for ck run and the skills: always exits 0")
@click.option("--json", "as_json", is_flag=True)
def collect_cmd(run_id: Optional[str], collect_everything: bool, agents_id: Optional[str], backfill_flag: bool,
                dry_run: bool, gap: Optional[str], auto: bool, as_json: bool):
    """Collect a finished run into a local record (never leaves this machine)."""
    decision = agentrun.decide_collect_args(run_id, collect_everything, agents_id, backfill_flag, auto, dry_run, gap)
    if not decision.ok:
        if decision.exit_code == 0:
            shown = agents_id if agents_id is not None else run_id
            click.echo(f"⚠ could not collect {shown}: {decision.message}; retry: ck stats collect --agents {shown}")
        else:
            click.echo(f"❌ {decision.message}")
        sys.exit(decision.exit_code)
    if decision.mode == "agents":
        if auto:
            sys.exit(collect_agents_auto(agents_id))
        home = bench.claude_home()
        if _classify(home) == "sandboxed":
            click.echo(f"❌ {_sandbox_text(home)}")
            sys.exit(1)
        _collect_agents_one(home, agents_id, as_json)
        return
    if run_id is not None and not auto and not RUN_ID_RE.match(run_id):
        click.echo("❌ RUN_ID must look like wf_ followed by letters, digits, - or _")
        sys.exit(2)
    if auto:
        sys.exit(collect_auto(run_id))
    home = bench.claude_home()
    if _classify(home) == "sandboxed":
        click.echo(f"❌ {_sandbox_text(home)}")
        sys.exit(1)
    if decision.mode == "backfill":
        _collect_backfill(home, decision.gap_minutes, dry_run, as_json)
    elif run_id is not None:
        _collect_one(home, run_id, as_json)
    else:
        _collect_all(home, as_json)


def _collect_agents_one(home: Path, run_id: str, as_json: bool) -> None:
    try:
        outcome = agentcollect.collect_agent_run(home, run_id, now=_now())
    except Exception as e:  # noqa: BLE001
        outcome = col.CollectOutcome(run_id, "failed", reason=f"{type(e).__name__}: {str(e)[:200]}", error="error")
    if outcome.kind == "collected":
        try:
            path, kind = agentcollect.finish_collected(outcome)
        except Exception as e:  # noqa: BLE001
            reason = f"{type(e).__name__}: {str(e)[:200]}"
            click.echo(json.dumps({"run_id": run_id, "result": "failed", "error": reason}) if as_json else f"❌ {reason}")
            sys.exit(1)
        partial = _is_partial(outcome.record)
        why = _partial_why(outcome.record)
        if as_json:
            click.echo(json.dumps({"run_id": run_id, "result": kind, "path": str(path), "partial": partial}))
        else:
            click.echo(f"✓ {run_id}: wrote {path}" + (f" (partial: {why})" if partial else ""))
        return
    reason = outcome.reason or "could not be collected"
    click.echo(json.dumps({"run_id": run_id, "result": "failed", "error": outcome.error or reason}) if as_json
               else f"❌ {reason}")
    sys.exit(1)


def _partial_why(record: Optional[dict]) -> str:
    whats = [m.get("what") for m in (record or {}).get("missing", [])]
    if "manifest" in whats:
        return "run manifest missing"
    if "runfile" in whats:
        return "run file missing"
    return "incomplete"


def collect_agents_auto(run_id: str, home: Optional[Path] = None) -> int:
    """`collect --agents RUN_ID --auto`: always 0; a failure is logged and the retry command printed."""
    retry = f"ck stats collect --agents {run_id}"
    try:
        try:
            enabled = col.auto_collect_enabled(ag.resolve(Path.cwd())["roles"]["run"])
        except ag.AgentConfigError:
            enabled = True
        if not enabled:
            click.echo("ℹ️ auto-collect is off (ck agents set run.analytics true)")
            return 0
        home = home or bench.claude_home()
        if _classify(home) == "sandboxed":
            if os.environ.get("CK_RUN_SANDBOXED") == "1":
                click.echo(f"ℹ️ {run_id}: ck run collects this run after the session exits "
                           f"(without a run manifest: {retry})")
                return 0
            reason = _sandbox_text(home)
            store.append_collect_log(f"{_utc_iso()} {run_id} sandboxed: {reason}")
            click.echo(f"⚠ could not collect {run_id}: {reason}; retry: {retry}")
            return 0
        outcome = agentcollect.collect_agent_run(home, run_id, now=_now(), fresh=True)
        if outcome.kind == "collected":
            path, _kind = agentcollect.finish_collected(outcome)
            click.echo(f"✓ {run_id}: wrote {path}")
            return 0
        reason = outcome.reason or "could not be collected"
        store.append_collect_log(f"{_utc_iso()} {run_id} {outcome.error or 'failed'}: {reason}")
        click.echo(f"⚠ could not collect {run_id}: {reason}; retry: {retry}")
    except Exception as e:  # noqa: BLE001 - auto collection must never change the caller's result
        msg = f"{type(e).__name__}: {str(e)[:200]}"
        try:
            store.append_collect_log(f"{_utc_iso()} {run_id} error: {msg}")
        except Exception:  # noqa: BLE001
            pass
        click.echo(f"⚠ could not collect {run_id}: {msg}; retry: {retry}")
    return 0


def _collect_backfill(home: Path, gap_minutes: Optional[int], dry_run: bool, as_json: bool) -> None:
    from ..analytics import roles as _roles

    gap = gap_minutes or agentrun.DEFAULT_GAP_MINUTES
    plan = agentcollect.plan_backfill(home, gap_minutes=gap)
    groups, excluded = plan.plan.groups, plan.plan.excluded
    if not groups:
        if as_json:
            if dry_run:
                click.echo(json.dumps({"groups": [], "excluded": [{"agentId": a, "reason": r} for a, _s, r in excluded]}))
            else:
                click.echo(json.dumps({"results": [], "collected": 0, "already_had": 0, "updated": 0, "failed": 0,
                                       "excluded": len(excluded)}))
            return
        click.echo("ℹ️ no unmarked spec-agent runs to backfill")
        if dry_run:
            for aid, _sid, reason in excluded:
                click.echo(f"left out: {aid}  {reason}")
        else:
            click.echo(f"left out: {len(excluded)}")
        return

    def wall(g) -> int:
        return int(round((g.end_ms - g.start_ms) / 60000))

    def iso(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if dry_run:
        if as_json:
            click.echo(json.dumps({
                "groups": [{"id": g.record_id, "session": g.session_id, "spec": g.spec, "agents": len(g.agent_ids),
                            "start": iso(g.start_ms), "wallMinutes": wall(g), "usd": plan.est_usd.get(g.record_id)}
                           for g in groups],
                "excluded": [{"agentId": a, "reason": r} for a, _s, r in excluded]}))
            return
        for g in groups:
            usd = plan.est_usd.get(g.record_id)
            click.echo(f"group {g.record_id}  session {g.session_id}  spec {g.spec}  agents {len(g.agent_ids)}  "
                       f"start {iso(g.start_ms)}  wall {wall(g)} min  est. " + (f"${usd:.2f}" if usd is not None else "$?"))
            for aid in g.agent_ids:
                af = plan.files.get(aid)
                role = _roles.match_legacy(af.meta.description, af.meta.agent_type).role if af else "?"
                click.echo(f"  {aid}  {role}")
        for aid, _sid, reason in excluded:
            click.echo(f"left out: {aid}  {reason}")
        return

    outcomes = agentcollect.collect_backfill(home, plan, now=_now())
    counts = {"collected": 0, "unchanged": 0, "updated": 0, "failed": 0}
    for o in outcomes:
        counts[o.kind if o.kind in counts else "failed"] += 1
    if as_json:
        click.echo(json.dumps({"results": [{"id": o.run_id, "result": o.kind if o.kind in counts else "failed"} for o in outcomes],
                               "collected": counts["collected"], "already_had": counts["unchanged"],
                               "updated": counts["updated"], "failed": counts["failed"], "excluded": len(excluded)}))
    else:
        click.echo(f"collected: {counts['collected']}")
        click.echo(f"already had: {counts['unchanged']}")
        click.echo(f"updated: {counts['updated']}")
        click.echo(f"failed: {counts['failed']}")
        for o in outcomes:
            if o.kind == "failed":
                click.echo(f"  {o.run_id}: {o.reason or 'failed'}")
        click.echo(f"left out: {len(excluded)}")
    sys.exit(1 if counts["failed"] else 0)


def _collect_one(home: Path, run_id: str, as_json: bool) -> None:
    outcome = col.collect_run(home, run_id, now=_now())
    if outcome.kind == "unfinished":
        click.echo(json.dumps({"run_id": run_id, "result": "unfinished"}) if as_json else f"ℹ️ {run_id} has not finished; nothing written")
        return
    if outcome.kind == "collected":
        path, kind = _write(outcome)
        partial = _is_partial(outcome.record)
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
    try:
        agent_outcomes = agentcollect.collect_open_manifests(home, now=_now())
    except Exception:  # noqa: BLE001
        agent_outcomes = ()
    kinds: List[str] = []
    failures: List[Tuple[str, str]] = []
    skipped = 0
    abandoned: List[str] = []
    for o in list(summary.outcomes) + list(agent_outcomes):
        if o.kind == "skipped_non_ck":
            skipped += 1
            continue
        if o.kind == "abandoned":
            abandoned.append(o.run_id)
            continue
        if o.kind == "collected":
            try:
                if o.run_id.startswith("ag_"):
                    _path, k = agentcollect.finish_collected(o)
                else:
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
        payload = {"collected": counts.collected, "already_had": counts.unchanged, "updated": counts.updated,
                   "unfinished": counts.unfinished, "failed": counts.failed,
                   "failures": [{"run_id": r, "reason": m} for r, m in failures],
                   "skipped_non_ck": counts.skipped_non_ck}
        if abandoned:
            payload["abandoned"] = len(abandoned)
        click.echo(json.dumps(payload))
        sys.exit(code)
    if not kinds and not abandoned:
        click.echo(f"ℹ️ no ck spec runs found under {home} ({skipped} other workflow runs skipped)")
        click.echo(f"skipped non-ck: {skipped}")
        sys.exit(0)
    for rid in abandoned:
        click.echo(f"ℹ️ {rid}: abandoned (no agents carry the marker)")
    click.echo(f"collected: {counts.collected}")
    click.echo(f"already had: {counts.unchanged}")
    click.echo(f"updated: {counts.updated}")
    click.echo(f"unfinished: {counts.unfinished}")
    click.echo(f"failed: {counts.failed}")
    if abandoned:
        click.echo(f"abandoned: {len(abandoned)}")
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

    checkout = Path(to) if to else Path.cwd()
    if not checkout.is_dir() or not any(ex.is_target_repo(u) for _n, u in ex.remote_urls(checkout)):
        click.echo("❌ not a kutlayhanli/claude-kiro checkout; nothing written")
        sys.exit(1)
    _export_into(checkout.resolve(), reexport, note, yes)


def _export_into(checkout: Path, reexport, note: bool, yes: bool, announce_nothing: bool = True) -> bool:
    """The export step shared with `submit`: True when something was planned and written."""
    from ..analytics import export as ex
    from ..analytics import issues, leak

    records, _bad = store.load_records_counted()
    ledger = store.load_ledger()
    cid = _checkout_id(checkout)
    plan = ex.plan_export(records, ledger, list(reexport), cid, _present_files(checkout, ledger))
    if plan.nothing_new:
        if announce_nothing:
            click.echo("ℹ️ nothing new to export")
        return False
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
    return True


# --------------------------------------------------------------------------- submit


def _git_lines(checkout: Path, *args: str) -> List[str]:
    import subprocess

    proc = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True)
    return [l for l in proc.stdout.split("\n") if l]


def _unsubmitted_entries(ledger: dict, cid: str) -> dict:
    submitted = ledger.get("submitted") or {}
    out = {}
    for entry in (ledger.get("exported") or {}).values():
        if isinstance(entry, dict) and entry.get("checkout_id") == cid and entry.get("record_id") not in submitted:
            out[entry.get("record_id")] = entry
    return out


def _echo_diff(files) -> None:
    import difflib

    for f in files:
        lines = f.text.splitlines()
        for l in difflib.unified_diff([], lines, fromfile="/dev/null", tofile=f.rel_path, lineterm=""):
            click.echo(l)


@stats.command("submit")
@click.option("--to", "to", default=None, type=click.Path(), help="The claude-kiro checkout to use (default: here)")
@click.option("--note", "--notes", "note", is_flag=True, help="Draft an issues note per record (interactive only)")
@click.option("--yes", is_flag=True, help="Do not ask (a note still needs a terminal to be approved)")
def submit_cmd(to: Optional[str], note: bool, yes: bool):
    """Open a draft PR with the exported run records against kutlayhanli/claude-kiro."""
    import secrets as _secrets

    from ..analytics import export as ex
    from ..analytics import leak, submit as sb

    checkout = Path(to) if to else Path.cwd()
    remotes = ex.remote_urls(checkout) if checkout.is_dir() else []
    remote = sb.target_remote(remotes)
    if remote is None:
        click.echo("❌ not a kutlayhanli/claude-kiro checkout; nothing written")
        sys.exit(1)
    checkout = checkout.resolve()
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    if sb.decide_confirmation(yes, tty, False) == "refuse_noninteractive":
        click.echo("❌ no terminal to confirm on: pass --yes to submit without asking; nothing written")
        sys.exit(1)

    cid = _checkout_id(checkout)
    ledger = store.load_ledger()
    pending = _unsubmitted_entries(ledger, cid)
    candidates = set()
    for entry in pending.values():
        candidates.update(f for f in entry.get("files", []) if isinstance(f, str))
    for sub in (ledger.get("submitted") or {}).values():
        if isinstance(sub, dict):
            candidates.update(f for f in sub.get("withheld", []) if isinstance(f, str))
    tracked = frozenset(_git_lines(checkout, "ls-files", "--", *sorted(candidates))) if candidates else frozenset()
    own = frozenset(f for f in candidates if f not in tracked and (checkout / f).is_file())
    tracked_dest = frozenset(f for f in tracked
                             if any(f in e.get("files", []) for e in pending.values()))
    porcelain = _git_lines(checkout, "status", "--porcelain", "--untracked-files=all")
    pre = sb.decide_preflight(porcelain, own, tracked_dest, True)
    if not pre.ok:
        click.echo(f"❌ {checkout}: {pre.reason}; nothing changed")
        sys.exit(1)

    _export_into(checkout, (), note, yes, announce_nothing=False)

    ledger = store.load_ledger()
    ids = sb.select_batch(ledger, cid)
    if not ids:
        click.echo("ℹ️ nothing new to submit")
        return
    by_record = _unsubmitted_entries(ledger, cid)
    files = []
    shareables = []
    for rid in ids:
        entry = by_record[rid]
        for rel in sorted(entry.get("files", [])):
            path = checkout / rel
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as e:
                click.echo(f"❌ cannot read {rel}: {e}; nothing submitted")
                sys.exit(1)
            files.append(ex.PendingFile(rel, text, "", rid, rel.endswith(".md")))
            if rel.endswith(".json"):
                try:
                    shareables.append(json.loads(text))
                except ValueError:
                    pass
    has_notes = any(f.is_note for f in files)
    decision = sb.decide_confirmation(yes, tty, has_notes)
    if decision == "refuse_noninteractive":
        click.echo("❌ no terminal to confirm on: pass --yes to submit without asking; nothing submitted")
        sys.exit(1)
    withheld = []
    if decision == "skip_withhold_notes":
        withheld = [f.rel_path for f in files if f.is_note]
        files = [f for f in files if not f.is_note]
        click.echo("⚠ issues note withheld: needs interactive approval")
    summary = sb.summarize(shareables, store.machine_label())
    token = _secrets.token_hex(4)
    text = {"<branch>": sb.branch_name(token), "<commit>": sb.commit_message(summary),
            "<pr-title>": sb.pr_title(summary), "<pr-body>": sb.pr_body(summary)}

    records, _bad = store.load_records_counted()
    identity = leak.git_identity(checkout)
    terms = leak.build_terms(records, identity, leak.non_ck_workflow_names(bench.claude_home()))
    hits = leak.scan({**{f.rel_path: f.text for f in files}, **text}, terms)
    if hits:
        for h in hits:
            _echo_hit(h.file, h)
        click.echo("❌ leak check failed; nothing committed")
        sys.exit(1)
    _echo_diff(files)
    click.echo(f"✓ leak check passed ({len(files)} file(s))")

    if decision == "ask":
        try:
            ok = click.confirm("Open a draft PR with these files?", default=False)
        except click.Abort:
            ok = False
        if not ok:
            click.echo("ℹ️ not submitted")
            return
    result = sb.submit_batch(checkout, remote, files, summary, sb.subprocess_runner, token=token)
    if not result.ok:
        click.echo(f"❌ {result.error}")
        click.echo("files are left in the checkout for manual commit; nothing marked submitted")
        sys.exit(1)
    stamp = _utc_iso()
    submitted = ledger.setdefault("submitted", {})
    for rid in ids:
        submitted[rid] = {"pr_url": result.pr_url, "branch": result.branch, "submitted_at": stamp,
                          "withheld": [w for w in withheld if by_record[rid].get("files") and w in by_record[rid]["files"]]}
    store.save_ledger(ledger)
    if result.error:
        click.echo(f"⚠ {result.error}")
    click.echo(f"✓ opened draft PR: {result.pr_url}")
