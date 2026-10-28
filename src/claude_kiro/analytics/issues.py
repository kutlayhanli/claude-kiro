"""Issue codes for a run, and the optional free-text issues note (requirements 8.1-8.5).

`derive_issue_codes` is a pure function of the local record's result fields (never of free text other than the
structured failure `reason`). `draft_note` is the one subprocess here; it never raises. No terminal prompt lives
here: the CLI owns that.
"""

import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from claude_kiro import bench

ISSUE_CODES = frozenset({
    # spec-implement
    "gate_red_after_merge",
    "landing_red",
    "landing_bisected",
    "resolver_rounds_exceeded",
    "conflict_not_resolved",
    "review_blocking_unresolved",
    "review_contract_mismatch",
    "cross_test_blocking",
    "planning_failed",
    "plan_cycle",
    "retry_no_commits",
    "escalated_to_opus",
    "escalated",
    "red_on_landing_fixed",
    "worktree_busy",
    "unreviewed_merge",
    "report_from_agents",
    "permission_refusal",
    "run_halted", "run_blocked",
    # spec-create
    "review_needs_work", "review_lenses_lost", "consistency_conflicts", "review_escalated",
    # collection quality
    "unknown_roles",
    "transcripts_missing",
})

DRAFTER_ARGV = ["claude", "-p", "--model", "sonnet", "--max-turns", "1", "--output-format", "text"]


@dataclass(frozen=True)
class NoteDraft:
    text: Optional[str]
    error: Optional[str]


def _dicts(v: Any) -> List[Mapping]:
    return [x for x in v if isinstance(x, Mapping)] if isinstance(v, list) else []


def _size(v: Any) -> int:
    """A count from a list (its length) or an int; anything else is unknown (0)."""
    if isinstance(v, list):
        return len(v)
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    return 0


def _pos(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def _norm(v: Any) -> str:
    return v.strip().lower().replace(" ", "_") if isinstance(v, str) else ""


def derive_issue_codes(local: Mapping) -> Tuple[str, ...]:
    """S15: sorted, de-duplicated subset of ISSUE_CODES, a pure function of the record's result fields."""
    get = local.get if isinstance(local, Mapping) else (lambda k, d=None: d)
    codes = set()
    outcome = _norm(get("outcome"))

    tasks = _dicts(get("tasks"))
    for t in tasks:
        reason = t.get("reason") if isinstance(t.get("reason"), str) else None
        ok = t.get("ok")
        if reason is not None:
            if reason.startswith("merge failed: still conflicting after"):
                codes.add("resolver_rounds_exceeded")
            if reason.startswith("merge failed: conflict not resolved"):
                codes.add("conflict_not_resolved")
            if reason == "merge failed: no commits":
                codes.add("retry_no_commits")
            if reason.startswith("review: blocking findings remain"):
                codes.add("review_contract_mismatch" if t.get("track") == "test" else "review_blocking_unresolved")
            if reason == "planning step failed":
                codes.add("planning_failed")
            if "worktree busy" in reason.lower():
                codes.add("worktree_busy")
            if bench.REFUSAL_RE.search(reason):
                codes.add("permission_refusal")
        escs = t.get("escalations")
        if isinstance(escs, list) and escs:
            codes.add("escalated")
            if any(isinstance(e, str) and "opus" in e.lower() for e in escs):
                codes.add("escalated_to_opus")
        if ok is True and _pos(t.get("fixes")):
            codes.add("red_on_landing_fixed")
        if ok is True and t.get("verdict") == "unreviewed":
            codes.add("unreviewed_merge")

    for f in _dicts(get("fixerEscalations")):
        if isinstance(f.get("model"), str):
            codes.add("escalated")
            if "opus" in f["model"].lower():
                codes.add("escalated_to_opus")

    for l in _dicts(get("landings")):
        if _size(l.get("red")) > 0:
            codes.add("landing_red")
        if l.get("bisected") is True:
            codes.add("landing_bisected")

    if any(g.get("green") is False for g in _dicts(get("fullGates"))):
        codes.add("gate_red_after_merge")
    fg = get("finalGate")
    if isinstance(fg, Mapping) and (fg.get("green") is False or fg.get("fixAttempted") is True):
        codes.add("gate_red_after_merge")

    if any(_pos(c.get("blocking")) for c in _dicts(get("crossTests"))):
        codes.add("cross_test_blocking")

    stats = get("stats")
    if isinstance(stats, Mapping) and _pos(stats.get("planFailures")):
        codes.add("planning_failed")
    if outcome == "refused":
        codes.add("plan_cycle")
    if outcome == "halted":
        codes.add("run_halted")
    if outcome == "blocked":
        codes.add("run_blocked")
    if outcome == "needs_work":
        codes.add("review_needs_work")
    if get("reportSource") == "agents":
        codes.add("report_from_agents")

    review = get("review")
    if isinstance(review, Mapping):
        if _pos(review.get("lensesLost")):
            codes.add("review_lenses_lost")
        if _pos(review.get("escalated")):
            codes.add("review_escalated")
    cons = get("consistency")
    if isinstance(cons, Mapping) and _pos(cons.get("fixed")):
        codes.add("consistency_conflicts")

    by_role = get("byRole")
    other = by_role.get("other") if isinstance(by_role, Mapping) else None
    if (isinstance(other, Mapping) and _pos(other.get("agents"))) or any(
            a.get("role") == "other" for a in _dicts(get("agents"))):
        codes.add("unknown_roles")
    if any(m.get("what") in ("transcript", "transcripts") for m in _dicts(get("missing"))):
        codes.add("transcripts_missing")

    return tuple(sorted(codes & ISSUE_CODES))


def issue_codes(local: Mapping) -> List[str]:
    """Harness Contract wrapper over `derive_issue_codes`."""
    return list(derive_issue_codes(local))


def decide_note(has_note: bool, interactive: bool, yes: bool, user_choice: Optional[str], final_hits: int) -> str:
    """S16: include only when interactive, not `yes`, the user chose `accept`, and the final leak check found 0 hits."""
    if not has_note:
        return "none"
    if not interactive or yes:
        return "withhold_noninteractive"
    if user_choice != "accept":
        return "discarded"
    if final_hits > 0:
        return "blocked_by_leak"
    return "include"


def _build_prompt(local: Mapping, codes: Sequence[str]) -> str:
    lines = [
        "You are helping write a short issues note about one run of the ck spec workflow.",
        "Write a few plain sentences in Markdown describing what went wrong and what to look at.",
        "Do not include file paths, names, repository details or any quoted code.",
        "",
        f"Workflow: {local.get('workflow')}",
        f"Outcome: {local.get('outcome')}",
        f"Issue codes: {', '.join(codes) if codes else 'none'}",
    ]
    tasks = _dicts(local.get("tasks"))
    if tasks:
        failed = [t for t in tasks if t.get("ok") is False]
        lines.append(f"Tasks: {len(tasks)}, failed: {len(failed)}")
        for t in failed:
            if isinstance(t.get("reason"), str):
                lines.append(f"Failure reason: {t['reason']}")
    lines.append(f"Landings: {len(_dicts(local.get('landings')))}")
    lines.append(f"Full gates: {len(_dicts(local.get('fullGates')))}")
    return "\n".join(lines) + "\n"


def draft_note(local: Mapping, codes: Sequence[str], argv: Optional[List[str]] = None, timeout: float = 120) -> NoteDraft:
    """Run the drafter with a prompt on stdin; return its stdout or an error, never raising."""
    try:
        if argv is None:
            env = os.environ.get("CK_STATS_DRAFTER")
            argv = shlex.split(env) if env else list(DRAFTER_ARGV)
        if not argv:
            return NoteDraft(None, "no drafter command")
        prompt = _build_prompt(local, codes)
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return NoteDraft(None, "drafter not found")
    except subprocess.TimeoutExpired:
        return NoteDraft(None, f"drafter timed out after {timeout:g}s")
    except Exception as e:  # never raise
        return NoteDraft(None, f"drafter failed: {type(e).__name__}")
    if proc.returncode != 0:
        return NoteDraft(None, f"drafter exited with status {proc.returncode}")
    text = (proc.stdout or "").strip()
    if not text:
        return NoteDraft(None, "drafter produced no text")
    return NoteDraft(text, None)
