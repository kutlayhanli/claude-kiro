"""Submit a batch of exported run records as a draft PR to the one allowed target repo."""

import os
import re
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

TARGET_REPO = "kutlayhanli/claude-kiro"
TARGET_BASE = "master"

# runner(argv, cwd) -> object with .returncode, .stdout, .stderr (a subprocess.CompletedProcess fits)
Runner = Callable[[Sequence[str], Path], Any]

_TARGET_RE = re.compile(
    r"(?:https?://(?:[^@/\s]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
    + re.escape(TARGET_REPO)
    + r"(?:\.git)?/?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BatchSummary:
    records: int
    kinds: Dict[str, int]
    months: Tuple[str, ...]
    machine: Optional[str]


@dataclass(frozen=True)
class Preflight:
    ok: bool
    reason: Optional[str]


@dataclass(frozen=True)
class SubmitResult:
    ok: bool
    pr_url: Optional[str]
    branch: Optional[str]
    error: Optional[str]
    rolled_back: bool


def subprocess_runner(argv: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess":
    try:
        return subprocess.run(list(argv), cwd=str(cwd), capture_output=True, text=True)
    except OSError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


# --------------------------------------------------------------------------- target check (S20)


def is_target_repo(url: str) -> bool:
    return isinstance(url, str) and _TARGET_RE.fullmatch(url.strip()) is not None


def target_remote(remotes: Sequence[Tuple[str, str]]) -> Optional[str]:
    for name, url in remotes:
        if is_target_repo(url):
            return name
    return None


def remote_urls(checkout: Path) -> List[Tuple[str, str]]:
    """(name, configured url) pairs from local git config, so an insteadOf rewrite never hides the URL."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(checkout), "config", "--get-regexp", r"^remote\..*\.url$"],
            capture_output=True, text=True,
        )
    except OSError:
        return []
    out: List[Tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key.startswith("remote.") and key.endswith(".url"):
            out.append((key[len("remote."):-len(".url")], value.strip()))
    return out


# --------------------------------------------------------------------------- preflight (S21)


def _porcelain_paths(line: str) -> List[str]:
    body = line[3:] if len(line) > 3 else ""
    if " -> " in body:
        parts = body.split(" -> ")
    else:
        parts = [body]
    paths = []
    for p in parts:
        p = p.strip()
        if len(p) >= 2 and p.startswith('"') and p.endswith('"'):
            p = p[1:-1]
        if p:
            paths.append(p)
    return paths


def decide_preflight(porcelain: Sequence[str], own_pending: FrozenSet[str],
                     tracked_destinations: FrozenSet[str], target_ok: bool) -> Preflight:
    if not target_ok:
        return Preflight(False, "not a kutlayhanli/claude-kiro checkout")
    foreign = set()
    for line in porcelain:
        if not line.strip():
            continue
        for path in _porcelain_paths(line):
            if path not in own_pending:
                foreign.add(path)
    if foreign:
        return Preflight(False, f"uncommitted changes ({len(foreign)} paths)")
    for dest in sorted(tracked_destinations):
        if dest not in own_pending:
            return Preflight(False, f"destination {dest} already exists")
    return Preflight(True, None)


# --------------------------------------------------------------------------- batch and confirmation (S22, S23)


def select_batch(ledger: dict, checkout_id: str) -> List[str]:
    submitted = ledger.get("submitted") or {}
    ids: List[str] = []
    for entry in (ledger.get("exported") or {}).values():
        if entry.get("checkout_id") != checkout_id:
            continue
        rid = entry.get("record_id")
        if rid and rid not in submitted and rid not in ids:
            ids.append(rid)
    return ids


def decide_confirmation(yes: bool, interactive: bool, has_free_text: bool) -> str:
    if interactive:
        return "skip" if yes and not has_free_text else "ask"
    if not yes:
        return "refuse_noninteractive"
    return "skip_withhold_notes" if has_free_text else "skip"


# --------------------------------------------------------------------------- PR text (S24)


def branch_name(token: str) -> str:
    return f"ck-stats-batch-{token}"


def _kinds_text(summary: BatchSummary) -> str:
    return ", ".join(f"{k} {n}" for k, n in sorted(summary.kinds.items()))


def commit_message(summary: BatchSummary) -> str:
    text = f"ck-stats-batch: {summary.records} ({_kinds_text(summary)})"
    if summary.months:
        text += " " + " ".join(summary.months)
    if summary.machine:
        text += f" {summary.machine}"
    return text


def pr_title(summary: BatchSummary) -> str:
    return commit_message(summary)


def pr_body(summary: BatchSummary) -> str:
    lines = [
        f"count: {summary.records}",
        f"kinds: {_kinds_text(summary)}",
        f"months: {' '.join(summary.months)}",
    ]
    if summary.machine:
        lines.append(f"machine: {summary.machine}")
    return "\n".join(lines) + "\n"


def summarize(records: Sequence[dict], machine_label: Optional[str]) -> BatchSummary:
    kinds: Dict[str, int] = {}
    months = set()
    for rec in records:
        kind = rec.get("workflow")
        kind = kind if isinstance(kind, str) else "other"
        kinds[kind] = kinds.get(kind, 0) + 1
        month = rec.get("month")
        if isinstance(month, str):
            months.add(month)
    return BatchSummary(len(records), kinds, tuple(sorted(months)), machine_label or None)


def pr_text(records: Sequence[dict], machine_label: Optional[str], token: Optional[str] = None) -> dict:
    summary = summarize(records, machine_label)
    if token is None:
        token = secrets.token_hex(4)
    return {
        "branch": branch_name(token),
        "commit": commit_message(summary),
        "title": pr_title(summary),
        "body": pr_body(summary),
    }


# --------------------------------------------------------------------------- git / gh flow


def _fail(runner_result: Any) -> str:
    text = (getattr(runner_result, "stderr", "") or getattr(runner_result, "stdout", "") or "").strip()
    return text.splitlines()[-1] if text else f"exit {getattr(runner_result, 'returncode', '?')}"


def submit_batch(checkout: Path, remote: str, files: Sequence[Any], summary: BatchSummary,
                 runner: Runner, token: Optional[str] = None) -> SubmitResult:
    checkout = Path(checkout)
    if shutil.which("gh") is None:
        return SubmitResult(False, None, None, "gh is not installed (needed to open the PR); nothing changed", False)
    auth = runner(["gh", "auth", "status"], checkout)
    if auth.returncode != 0:
        return SubmitResult(False, None, None, f"gh is not authenticated: {_fail(auth)}; nothing changed", False)

    head = runner(["git", "rev-parse", "--abbrev-ref", "HEAD"], checkout)
    original = (head.stdout or "").strip() if head.returncode == 0 else ""
    if original == "HEAD" or not original:
        sha = runner(["git", "rev-parse", "HEAD"], checkout)
        original = (sha.stdout or "").strip()
    if not original:
        return SubmitResult(False, None, None, "cannot determine the current branch; nothing changed", False)

    branch = branch_name(token or secrets.token_hex(4))
    paths = [f.rel_path for f in files]
    state = {"switched": False, "committed": False, "pushed": False}

    def rollback(message: str) -> SubmitResult:
        if state["switched"]:
            if not state["committed"]:
                runner(["git", "reset", "-q", "--", *paths], checkout)
            runner(["git", "switch", original], checkout)
            runner(["git", "branch", "-D", branch], checkout)
        for f in files:
            target = checkout / f.rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.text, encoding="utf-8")
        if state["pushed"]:
            message += f"; branch {branch} was already pushed to {remote} and was left there"
        return SubmitResult(False, None, branch, message, True)

    fetch = runner(["git", "fetch", remote, TARGET_BASE], checkout)
    if fetch.returncode != 0:
        return SubmitResult(False, None, None, f"git fetch {remote} {TARGET_BASE} failed: {_fail(fetch)}", False)

    sw = runner(["git", "switch", "-c", branch, f"{remote}/{TARGET_BASE}"], checkout)
    if sw.returncode != 0:
        return rollback(f"git switch failed: {_fail(sw)}")
    state["switched"] = True

    step = runner(["git", "add", "--", *paths], checkout)
    if step.returncode != 0:
        return rollback(f"git add failed: {_fail(step)}")
    step = runner(["git", "commit", "-q", "-m", commit_message(summary)], checkout)
    if step.returncode != 0:
        return rollback(f"git commit failed: {_fail(step)}")
    state["committed"] = True

    step = runner(["git", "push", remote, branch], checkout)
    if step.returncode != 0:
        return rollback(f"git push failed: {_fail(step)}")
    state["pushed"] = True

    fd, body_path = tempfile.mkstemp(prefix="ck-pr-body-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(pr_body(summary))
        pr = runner(
            ["gh", "pr", "create", "--draft", "--repo", TARGET_REPO, "--base", TARGET_BASE,
             "--head", branch, "--title", pr_title(summary), "--body-file", body_path],
            checkout,
        )
    finally:
        try:
            os.unlink(body_path)
        except OSError:
            pass
    if pr.returncode != 0:
        return rollback(f"gh pr create failed: {_fail(pr)}")
    out_lines = [l.strip() for l in (pr.stdout or "").splitlines() if l.strip()]
    pr_url = out_lines[-1] if out_lines else None

    back = runner(["git", "switch", original], checkout)
    if back.returncode != 0:
        return SubmitResult(True, pr_url, branch,
                            f"PR opened but switching back to {original} failed: {_fail(back)}", False)
    return SubmitResult(True, pr_url, branch, None, False)
