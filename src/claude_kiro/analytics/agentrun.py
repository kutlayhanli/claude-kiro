"""Pure rules of agent-mode analytics (design A1-A4, A7-A11, A14).

No I/O, no clock, no network: every input is an argument. The one exception is `new_agent_run_id` without a
`token`, which draws from `secrets` (the design's callers pass their own draw in tests).
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AbstractSet, Callable, Dict, FrozenSet, Mapping, Optional, Sequence, Tuple

from . import roles

RUN_ID_RE = re.compile(r"ag_[0-9a-f]{8}")
MARKER_RE = re.compile(r"\[ck:(ag_[0-9a-f]{8})\]")
KINDS = ("implement", "create", "review")
STALE_MS = 6 * 60 * 60 * 1000
MERGE_SLACK_MS = 30 * 60 * 1000
MANIFEST_SCHEMA = "ck-run-manifest/1"
ALLOWED_PLACEHOLDERS = frozenset({"run_id", "N", "k"})
DEFAULT_GAP_MINUTES = 120

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_HEX8 = re.compile(r"[0-9a-f]{8}")
_PLACEHOLDER = re.compile(r"<[^<>]*>")
_ISO_RX = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2})"
    r"(?::([0-9]{2})(?:\.([0-9]{1,9}))?)?"
    r"(Z|[+-][0-9]{2}:[0-9]{2})"
)
_GAP_RX = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class FormatCheck:
    ok: bool
    start_ms: Optional[int]   # parsed from startTime; None when ok is False
    reason: Optional[str]     # short code, never the file's contents


@dataclass(frozen=True)
class Marker:
    run_id: str   # ag_<8 hex>
    rest: str     # the description after "[ck:<id>] " ("" when nothing follows)


@dataclass(frozen=True)
class MetaView:
    agent_id: str
    session_id: str
    agent_type: Optional[str]
    description: Optional[str]
    under_workflow: bool


@dataclass(frozen=True)
class ReportSummary:
    outcome: Optional[str]
    merged: Optional[Tuple[str, ...]]
    failed: Optional[Tuple[str, ...]]
    blocked: Optional[Tuple[str, ...]]
    not_started: Optional[Tuple[str, ...]]
    head: Optional[str]


@dataclass(frozen=True)
class ArgsDecision:
    ok: bool
    mode: Optional[str]
    exit_code: int
    message: Optional[str]
    gap_minutes: Optional[int]


@dataclass(frozen=True)
class BeginResult:
    run_id: str
    manifest_path: Optional[Path]
    warning: Optional[str]


# ------------------------------------------------------------------------------------------- A1-A4 run ids, markers

def is_agent_run_id(value: object) -> bool:
    """A1: True only for a str that fully matches `ag_[0-9a-f]{8}`."""
    return isinstance(value, str) and RUN_ID_RE.fullmatch(value) is not None


valid_run_id = is_agent_run_id


def new_run_id(taken: AbstractSet[str], draw: Callable[[], str], attempts: int = 64) -> str:
    """A2: `ag_` + the first draw that is 8 lowercase hex digits and not in `taken`."""
    for _ in range(attempts):
        token = draw()
        if isinstance(token, str) and _HEX8.fullmatch(token) and "ag_" + token not in taken:
            return "ag_" + token
    raise RuntimeError("no free agent-mode run id after %d draws" % attempts)


def new_agent_run_id(taken: AbstractSet[str], *, token: Optional[Callable[[], str]] = None) -> str:
    """Harness form of A2. Without `token` the draw is `secrets.token_hex(4)`."""
    if token is None:
        import secrets

        token = lambda: secrets.token_hex(4)  # noqa: E731
    return new_run_id(taken, token)


def parse_marker(description: object) -> Optional[Marker]:
    """A3: `Marker` when the description starts (index 0, no stripping) with `[ck:<id>]`, then nothing or one space."""
    if not isinstance(description, str):
        return None
    found = MARKER_RE.match(description)
    if found is None:
        return None
    end = found.end()
    if end == len(description):
        return Marker(run_id=found.group(1), rest="")
    if description[end] == " ":
        return Marker(run_id=found.group(1), rest=description[end + 1:])
    return None


def select_marked(metas: Sequence[MetaView], run_id: str) -> Tuple[MetaView, ...]:
    """A4: the metas whose marker is `run_id`, in input order, whatever their agent type or session."""
    return tuple(m for m in metas if (parse_marker(m.description) or _NO_MARK).run_id == run_id)


_NO_MARK = Marker(run_id="", rest="")


def parse_agent_description(description: object, agent_type: object) -> Dict[str, object]:
    """Harness form: the marker (if any) plus the A5 grammar on the rest. Keys run_id, role, matched, task,
    attempt, round, step; a description that matches no template is `other` with `matched` False.
    """
    marker = parse_marker(description)
    if marker is None:
        run_id, rest = None, description
    else:
        run_id, rest = marker.run_id, marker.rest
    found = roles.match_agent(rest, agent_type)
    return {
        "run_id": run_id,
        "role": found.role,
        "matched": found.matched,
        "task": found.task,
        "attempt": found.attempt,
        "round": found.round,
        "step": found.step,
    }


# ------------------------------------------------------------------------------------------- A7, A8 templates

def template_violations(template: str) -> Tuple[str, ...]:
    """A7: placeholders outside ALLOWED_PLACEHOLDERS, plus a template not starting with the marker prefix."""
    found = []
    for token in _PLACEHOLDER.findall(template):
        if token[1:-1] not in ALLOWED_PLACEHOLDERS:
            found.append(token)
    if not template.startswith("[ck:<run_id>] "):
        found.append("marker")
    return tuple(found)


def render_template(template: str, samples: Mapping[str, str]) -> str:
    """A8: substitute each allowed placeholder that has a sample; others stay as written."""

    def sub(match: "re.Match") -> str:
        name = match.group(0)[1:-1]
        if name in ALLOWED_PLACEHOLDERS and name in samples:
            return str(samples[name])
        return match.group(0)

    return _PLACEHOLDER.sub(sub, template)


def description_templates(markdown: str) -> Tuple[str, ...]:
    """A8: every backtick span in `markdown` that starts with the `[ck:<run_id>] ` marker prefix."""
    return tuple(re.findall(r"`(\[ck:<run_id>\][^`]*)`", markdown))


# ------------------------------------------------------------------------------------------- A9 implement outcome

_REPORT_LISTS = ("merged", "done", "failed", "blocked", "unstarted")


def parse_iso_ms(value: object) -> Optional[int]:
    """ISO-8601 with a `Z` or `±hh:mm` offset (fractional seconds and missing seconds allowed) -> epoch ms.

    A naive time, a non-string or an unparseable string gives None.
    """
    if not isinstance(value, str):
        return None
    found = _ISO_RX.fullmatch(value)
    if found is None:
        return None
    year, month, day, hour, minute, second, frac, offset = found.groups()
    try:
        if offset == "Z":
            tz = timezone.utc
        else:
            sign = -1 if offset[0] == "-" else 1
            hh, mm = int(offset[1:3]), int(offset[4:6])
            if hh > 23 or mm > 59:
                return None
            tz = timezone(sign * timedelta(hours=hh, minutes=mm))
        micro = int((frac or "0").ljust(6, "0")[:6])
        moment = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0), micro,
                          tzinfo=tz)
    except ValueError:
        return None
    return (moment - _EPOCH) // timedelta(milliseconds=1)


def _task_ids(entries: object) -> Tuple[str, ...]:
    seen = []
    for entry in entries if isinstance(entries, (list, tuple)) else ():
        task = entry.get("task") if isinstance(entry, Mapping) else None
        if isinstance(task, str) and task not in seen:
            seen.append(task)
    return tuple(seen)


def report_summary(
    report: object,
    done_at_start: Optional[AbstractSet[str]] = None,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    run_tasks: Optional[AbstractSet[str]] = None,
) -> ReportSummary:
    """A9 in full. An unusable report gives every field None. `merged` excludes `done_at_start` when given,
    else keeps the `merged` entries whose `mergedAt` lies in [start_ms, end_ms]. Failed, blocked and not-started
    are the report's lists minus merged, limited to `run_tasks` when given (an empty set gives outcome None).
    """
    if not isinstance(report, Mapping) or not all(isinstance(report.get(k), (list, tuple)) for k in _REPORT_LISTS):
        return ReportSummary(None, None, None, None, None, None)

    if done_at_start is not None:
        merged_all = _task_ids(report["merged"]) + _task_ids(report["done"])
        merged = tuple(t for t in dict.fromkeys(merged_all) if t not in done_at_start)
    else:
        in_window = []
        for entry in report["merged"]:
            at = parse_iso_ms(entry.get("mergedAt")) if isinstance(entry, Mapping) else None
            if at is None or (start_ms is not None and at < start_ms) or (end_ms is not None and at > end_ms):
                continue
            in_window.append(entry)
        merged = _task_ids(in_window)

    merged_set = set(merged)
    failed = [t for t in _task_ids(report["failed"]) if t not in merged_set]
    blocked = [t for t in _task_ids(report["blocked"]) if t not in merged_set]
    not_started = [t for t in dict.fromkeys(blocked + list(_task_ids(report["unstarted"])))
                   if t not in merged_set]
    if run_tasks is not None:
        scope = set(run_tasks)
        failed = [t for t in failed if t in scope]
        blocked = [t for t in blocked if t in scope]
        not_started = [t for t in not_started if t in scope]

    head = report.get("head")
    if run_tasks is not None and not run_tasks:
        outcome = None
    elif failed:
        outcome = "failed"
    elif blocked or not_started:
        outcome = "blocked"
    else:
        outcome = "complete"
    return ReportSummary(
        outcome=outcome, merged=merged, failed=tuple(failed), blocked=tuple(blocked),
        not_started=tuple(not_started), head=head if isinstance(head, str) else None,
    )


def implement_outcome(report: object) -> Optional[str]:
    """Harness one-argument form of A9: the outcome string, or None for an unusable report."""
    return report_summary(report).outcome


def implement_summary(
    report: object,
    *,
    done_at_start: Optional[AbstractSet[str]] = None,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    run_tasks: Optional[AbstractSet[str]] = None,
) -> Dict[str, Optional[object]]:
    """Harness dict view of A9: keys outcome, merged, failed, blocked, not_started (all None when unusable)."""
    s = report_summary(report, done_at_start=done_at_start, start_ms=start_ms, end_ms=end_ms, run_tasks=run_tasks)
    return {
        "outcome": s.outcome,
        "merged": None if s.merged is None else list(s.merged),
        "failed": None if s.failed is None else list(s.failed),
        "blocked": None if s.blocked is None else list(s.blocked),
        "not_started": None if s.not_started is None else list(s.not_started),
    }


# ------------------------------------------------------------------------------------------- A10 manifests

def manifest_start_ms(value: object) -> Optional[int]:
    """A10: the manifest `startTime` as epoch ms, or None (naive, non-string, unparseable)."""
    return parse_iso_ms(value)


def check_manifest(data: object, stem: Optional[str] = None) -> FormatCheck:
    """A10: the manifest format. Fails on a wrong schema, a bad runId (or one not equal to `stem`), an unknown
    kind, a missing or unparseable startTime, or an empty spec or root. Other keys are tolerated.
    """
    if not isinstance(data, Mapping):
        return FormatCheck(False, None, "not-an-object")
    if data.get("schema") != MANIFEST_SCHEMA:
        return FormatCheck(False, None, "schema")
    run_id = data.get("runId")
    if not is_agent_run_id(run_id) or (stem is not None and run_id != stem):
        return FormatCheck(False, None, "runId")
    if data.get("kind") not in KINDS:
        return FormatCheck(False, None, "kind")
    start = manifest_start_ms(data.get("startTime"))
    if start is None:
        return FormatCheck(False, None, "startTime")
    for key in ("spec", "root"):
        value = data.get(key)
        if not isinstance(value, str) or not value:
            return FormatCheck(False, None, key)
    return FormatCheck(True, start, None)


def classify_open_manifest(check: FormatCheck, now_ms: int, threshold_ms: int = STALE_MS) -> str:
    """A10: "malformed" on a failed check, "collect" iff strictly older than the threshold, else "young"."""
    if not check.ok or check.start_ms is None:
        return "malformed"
    return "collect" if now_ms - check.start_ms > threshold_ms else "young"


def _moment_ms(now: datetime) -> int:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - _EPOCH) // timedelta(milliseconds=1)


def open_manifest_due(start: object, now: datetime, *, hours: int = 6) -> Optional[bool]:
    """True when an open manifest starting at `start` is strictly older than `hours` at `now`; None when unparseable."""
    start_ms = manifest_start_ms(start)
    if start_ms is None:
        return None
    check = FormatCheck(True, start_ms, None)
    return classify_open_manifest(check, _moment_ms(now), threshold_ms=hours * 60 * 60 * 1000) == "collect"


def classify_manifest(manifest: object, now: datetime) -> str:
    """Harness form: "collect", "young" or "malformed" for a whole manifest dict at `now`."""
    return classify_open_manifest(check_manifest(manifest), _moment_ms(now))


# ------------------------------------------------------------------------------------------- A11 collect arguments

def _refuse(message: str, auto: bool) -> ArgsDecision:
    return ArgsDecision(ok=False, mode=None, exit_code=0 if auto else 2, message=message, gap_minutes=None)


def decide_collect_args(
    run_id: Optional[str],
    all_: bool,
    agents: Optional[str],
    backfill: bool,
    auto: bool,
    dry_run: bool,
    gap: Optional[str],
) -> ArgsDecision:
    """A11: the `ck stats collect` argument rules. Id-format failures exit 0 under `auto`."""
    chosen = [run_id is not None, bool(all_), agents is not None, bool(backfill)]
    if sum(chosen) != 1:
        return ArgsDecision(False, None, 2, "give exactly one of --run, --all, --agents or --agents-backfill", None)
    if auto and (all_ or backfill):
        return ArgsDecision(False, None, 2, "--auto cannot be combined with --all or --agents-backfill", None)
    if (dry_run or gap is not None) and not backfill:
        return ArgsDecision(False, None, 2, "--dry-run and --gap need --agents-backfill", None)

    gap_minutes: Optional[int] = None
    if backfill:
        gap_minutes = DEFAULT_GAP_MINUTES
        if gap is not None:
            parsed = None
            if _GAP_RX.fullmatch(gap):
                try:
                    parsed = int(gap)
                except ValueError:  # more digits than int() takes
                    parsed = None
            if parsed is None or parsed < 1:
                return ArgsDecision(False, None, 2, "--gap must be a positive whole number of minutes", None)
            gap_minutes = parsed

    if agents is not None and not is_agent_run_id(agents):
        return _refuse("RUN_ID must look like ag_ followed by 8 lowercase hex digits", auto)
    if run_id is not None and is_agent_run_id(run_id):
        return _refuse(f"{run_id} is an agent-mode run id: use ck stats collect --agents {run_id}", auto)

    if run_id is not None:
        mode = "run"
    elif all_:
        mode = "all"
    elif agents is not None:
        mode = "agents"
    else:
        mode = "backfill"
    return ArgsDecision(True, mode, 0, None, gap_minutes)


# ------------------------------------------------------------------------------------------- A14 cost label

def agent_cost_label(transcript_agents: int, estimated_agents: int, unpriced_agents: int) -> str:
    """A14: "agent_transcripts" iff the run cost is priced from transcripts alone, else "estimate"."""
    from .collect import cost_label

    if cost_label(transcript_agents, estimated_agents, unpriced_agents) == "priced_from_transcripts":
        return "agent_transcripts"
    return "estimate"
