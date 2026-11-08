"""Protected-change decisions, landing tokens and the sealed approved state.

Pure decision seams (`ref_update_decision`, `token_matches`, `merge_commit_decision`, `approval_decision`,
`audit_decision`) plus the state ck keeps in `<git-common-dir>/ck/`: an HMAC key, one-shot landing tokens and the
sealed approved protected state of each integration ref. No network.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import os
import re
import secrets
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

INTEGRATE_PREFIX = "refs/heads/integrate/"
ZEROS = "0" * 40

# Files Claude Code, git or the shell run or load on their own (same set as worktrees.PROTECTED_PATHS).
PROTECTED_PATHS = (
    ".claude/**", "**/.claude/**", ".mcp.json", "**/.mcp.json",
    ".githooks/**", ".husky/**", ".git-hooks/**", ".pre-commit-config.yaml", ".envrc",
    "specs/ck.json",
)


# --------------------------------------------------------------------------- models

@dataclass(frozen=True)
class RefUpdate:
    ref: str
    old: str
    new: str
    new_tree: Optional[str] = None
    new_parents: Tuple[str, ...] = ()
    ancestor_of_old: bool = False

    @property
    def is_create(self) -> bool:
        return self.old == ZEROS

    @property
    def is_delete(self) -> bool:
        return self.new == ZEROS


@dataclass(frozen=True)
class TokenRecord:
    id: str
    ref: str
    old: str
    tree: str
    parents: Tuple[str, ...]
    issued_at: str
    reason: str
    files: Tuple[str, ...]
    mac: str


@dataclass(frozen=True)
class RefDecision:
    allow: bool
    files: Tuple[str, ...]
    message: str


@dataclass(frozen=True)
class HookStatus:
    installed: bool
    active: bool
    intact: bool
    chained_to: Optional[str] = None
    problems: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ApprovedState:
    ref: str
    commit: str
    files: Mapping[str, str]
    approvals: Tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class AuditReport:
    complete: bool
    clean: bool
    drift: Tuple[Mapping[str, str], ...]
    hooks: HookStatus
    fixtures: Mapping[str, object]
    problems: Tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- path filter

def git_unquote(p: str) -> str:
    """The path a git-quoted name (`"\\303\\244.md"`) denotes; any other string is returned unchanged."""
    if not (len(p) >= 2 and p[0] == '"' and p[-1] == '"'):
        return p
    inner, out, i = p[1:-1], bytearray(), 0
    simple = {"n": 10, "t": 9, '"': 34, "\\": 92, "a": 7, "b": 8, "f": 12, "r": 13, "v": 11}
    while i < len(inner):
        c = inner[i]
        if c == "\\" and i + 1 < len(inner):
            if re.fullmatch(r"[0-3][0-7]{2}", inner[i + 1:i + 4]):
                out.append(int(inner[i + 1:i + 4], 8))
                i += 4
                continue
            if inner[i + 1] in simple:
                out.append(simple[inner[i + 1]])
                i += 2
                continue
        out += c.encode("utf-8", "replace")
        i += 1
    return bytes(out).decode("utf-8", "replace")


def _glob(path: str, pattern: str) -> bool:
    pattern = pattern.removeprefix("./")
    return fnmatchcase(path, pattern) or (pattern.startswith("**/") and fnmatchcase(path, pattern[3:]))


def is_protected(path: str) -> bool:
    n = git_unquote(path)
    return any(_glob(n, p) for p in PROTECTED_PATHS)


def _allow_globs(allow: Optional[Sequence[str]]) -> List[str]:
    return [a for a in (allow or []) if isinstance(a, str) and a]


def refuse_paths(ref: str, changed: Sequence[str], allow: Sequence[str]) -> List[str]:
    """Protected, not allow-listed paths (unquoted) among `changed` for an `integrate/*` ref; else none."""
    if not ref.startswith(INTEGRATE_PREFIX):
        return []
    globs = _allow_globs(allow)
    out: List[str] = []
    for p in changed:
        n = git_unquote(p)
        if is_protected(n) and not any(_glob(n, a) for a in globs) and n not in out:
            out.append(n)
    return out


# --------------------------------------------------------------------------- decision seams

def _refusal_message(files: Sequence[str], what: str) -> str:
    listed = ", ".join(files)
    return (f"refused: {what} carries protected files not covered by land.allow_paths: {listed}. "
            "Review them, then approve in your own terminal with `ck worktree approve SPEC TASK`.")


def token_matches(token: TokenRecord, update: RefUpdate, used: bool, mac_ok: bool) -> bool:
    """A token is one-shot and bound to this ref, this old value and this new tree and parents."""
    return (not used and bool(mac_ok) and token.ref == update.ref and token.old == update.old
            and update.new_tree is not None and token.tree == update.new_tree
            and tuple(token.parents) == tuple(update.new_parents))


def ref_update_decision(update: RefUpdate, protected_changed: Any, allow: Sequence[str],
                        token: Any, approved: Optional[Mapping[str, str]]) -> RefDecision:
    """Allow or refuse one ref update (9.2-9.4, 9.12).

    `token` is truthy when a token that `token_matches` was presented. For a create, `protected_changed` is the new
    commit's protected files as a mapping path -> blob oid (compared with `approved`).
    """
    if not update.ref.startswith(INTEGRATE_PREFIX) or update.is_delete:
        return RefDecision(True, (), "")
    if update.is_create:
        files_map = dict(protected_changed) if isinstance(protected_changed, Mapping) else None
        if approved is not None and isinstance(approved, Mapping) and files_map is not None \
                and {git_unquote(k): v for k, v in files_map.items()} == dict(approved):
            return RefDecision(True, (), "")
        if token:
            return RefDecision(True, (), "")
        names = sorted(files_map) if files_map is not None else [git_unquote(p) for p in (protected_changed or [])]
        return RefDecision(False, tuple(names), _refusal_message(
            names or ["(no approved state recorded for this ref)"], f"creating {update.ref}"))
    files = tuple(refuse_paths(update.ref, list(protected_changed or []), allow))
    if not files or token:
        return RefDecision(True, (), "")
    return RefDecision(False, files, _refusal_message(files, f"the update of {update.ref}"))


def merge_commit_decision(head_ref: str, changed: Sequence[str], allow: Sequence[str],
                          token_present: bool) -> RefDecision:
    files = tuple(refuse_paths(head_ref, list(changed), allow))
    if not files or token_present:
        return RefDecision(True, (), "")
    return RefDecision(False, files, _refusal_message(files, f"this merge into {head_ref}"))


def approval_decision(stdin_tty: bool, stdout_tty: bool, typed: str, expected: str) -> bool:
    """Approval needs a real terminal on stdin and stdout and the exact code (one trailing newline allowed)."""
    if not (stdin_tty and stdout_tty):
        return False
    stripped = typed[:-1] if typed.endswith("\n") else typed
    return bool(expected) and stripped == expected


def audit_decision(approved: Optional[ApprovedState], current: Mapping[str, str], allow: Sequence[str],
                   hooks: HookStatus, fixtures: Mapping[str, object]) -> AuditReport:
    problems: List[str] = []
    files = getattr(approved, "files", None) if approved is not None else None
    complete = isinstance(files, Mapping)
    drift: List[Dict[str, str]] = []
    if not complete:
        problems.append("the approved protected state is missing or unreadable; run `ck worktree integration` "
                        "or approve the integration again")
    else:
        for path in sorted(set(files) | set(current)):
            a, c = files.get(path), current.get(path)
            if a is None and c is not None:
                drift.append({"file": path, "change": "added"})
            elif a is not None and c is None:
                drift.append({"file": path, "change": "deleted"})
            elif a != c:
                drift.append({"file": path, "change": "changed"})
        if drift:
            problems.append("protected files differ from the approved state: "
                            + ", ".join(f"{d['file']} ({d['change']})" for d in drift))
    hooks_ok = bool(hooks.installed and hooks.active and hooks.intact)
    if not hooks_ok:
        why = [n for n, ok in (("not installed", hooks.installed), ("not active", hooks.active),
                               ("modified", hooks.intact)) if not ok]
        problems.append("git hooks " + ", ".join(why))
    problems.extend(hooks.problems)
    fixture_drift = list(fixtures.get("drift") or []) if isinstance(fixtures, Mapping) else []
    tampered = bool(fixtures.get("lock_tampered")) if isinstance(fixtures, Mapping) else False
    if fixture_drift:
        problems.append(f"{len(fixture_drift)} recorded fixture(s) changed since their lock")
    if tampered:
        problems.append("the fixture lock was tampered with")
    clean = complete and not drift and hooks_ok and not fixture_drift and not tampered
    return AuditReport(complete=complete, clean=clean, drift=tuple(drift), hooks=hooks,
                       fixtures=fixtures, problems=tuple(problems))


# --------------------------------------------------------------------------- state in <common>/ck

def common_dir(root: Path) -> Path:
    out = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return Path(out)


def ck_dir(root: Path) -> Path:
    return common_dir(root) / "ck"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_key(root: Path) -> bytes:
    """The HMAC key, created (mode 0600) on first use."""
    d = ck_dir(root)
    path = d / "key"
    if not path.exists():
        d.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(32))
    return path.read_text().strip().encode()


def _read_key(root: Path) -> Optional[bytes]:
    try:
        return (ck_dir(root) / "key").read_text().strip().encode() or None
    except OSError:
        return None


def _seal(key: bytes, payload: Mapping[str, object]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def _write_json(path: Path, data: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


_TOKEN_FIELDS = ("id", "ref", "old", "tree", "parents", "issued_at", "reason", "files")


def _token_payload(t: TokenRecord) -> Dict[str, object]:
    return {"id": t.id, "ref": t.ref, "old": t.old, "tree": t.tree, "parents": list(t.parents),
            "issued_at": t.issued_at, "reason": t.reason, "files": list(t.files)}


def issue_token(root: Path, ref: str, old: str, tree: str, parents: Sequence[str], reason: str,
                files: Sequence[str] = ()) -> TokenRecord:
    key = ensure_key(root)
    rec = TokenRecord(id=secrets.token_hex(8), ref=ref, old=old, tree=tree, parents=tuple(parents),
                      issued_at=_now(), reason=reason, files=tuple(files), mac="")
    mac = _seal(key, _token_payload(rec))
    rec = TokenRecord(**{**rec.__dict__, "mac": mac})
    _write_json(ck_dir(root) / "tokens" / f"{rec.id}.json", {**_token_payload(rec), "mac": mac})
    return rec


def _safe_id(token_id: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{16}", token_id or ""))


def load_token(root: Path, token_id: str) -> Optional[Tuple[TokenRecord, bool, bool]]:
    """(record, used, mac_ok) for a token id, or None when it does not exist or cannot be read."""
    if not _safe_id(token_id):
        return None
    d = ck_dir(root)
    used = (d / "used" / f"{token_id}.json").exists()
    path = d / ("used" if used else "tokens") / f"{token_id}.json"
    try:
        data = json.loads(path.read_text())
        rec = TokenRecord(id=str(data["id"]), ref=str(data["ref"]), old=str(data["old"]), tree=str(data["tree"]),
                          parents=tuple(data["parents"]), issued_at=str(data["issued_at"]),
                          reason=str(data["reason"]), files=tuple(data["files"]), mac=str(data["mac"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    key = _read_key(root)
    mac_ok = key is not None and hmac.compare_digest(_seal(key, _token_payload(rec)), rec.mac) \
        and rec.id == token_id
    return rec, used, mac_ok


def check_token(root: Path, token_id: str, update: RefUpdate) -> bool:
    loaded = load_token(root, token_id)
    if loaded is None:
        return False
    rec, used, mac_ok = loaded
    return token_matches(rec, update, used, mac_ok)


def consume_token(root: Path, token_id: str, update: RefUpdate) -> bool:
    """True when the token matches `update`; it is then moved to `used/` (one-shot).

    A token is bound to `old`, so one whose rename failed still cannot match a later update.
    """
    if not check_token(root, token_id, update):
        return False
    d = ck_dir(root)
    try:
        (d / "used").mkdir(parents=True, exist_ok=True)
        os.rename(d / "tokens" / f"{token_id}.json", d / "used" / f"{token_id}.json")
    except OSError:
        pass
    return True


# --------------------------------------------------------------------------- approved state

def _spec_dir(root: Path, spec: str) -> Path:
    if not spec or "/" in spec or "\\" in spec or spec in (".", "..") or spec in ("tokens", "used", "hooks"):
        raise ValueError(f"invalid spec name {spec!r}")
    return ck_dir(root) / spec


def _state_payload(spec: str, state: ApprovedState) -> Dict[str, object]:
    return {"spec": spec, "ref": state.ref, "commit": state.commit, "files": dict(state.files),
            "approvals": [dict(a) for a in state.approvals]}


def read_approved_state(root: Path, spec: str, ref: Optional[str] = None) -> Optional[ApprovedState]:
    """The sealed state of `spec`, or None when missing, unreadable, unsealed, or of another spec or ref."""
    try:
        data = json.loads((_spec_dir(root, spec) / "protected.json").read_text())
        state = ApprovedState(ref=str(data["ref"]), commit=str(data["commit"]),
                              files={str(k): str(v) for k, v in dict(data["files"]).items()},
                              approvals=tuple(dict(a) for a in data["approvals"]))
        mac = str(data["mac"])
        recorded_spec = data["spec"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    key = _read_key(root)
    if key is None or recorded_spec != spec or (ref is not None and state.ref != ref):
        return None
    if not hmac.compare_digest(_seal(key, _state_payload(spec, state)), mac):
        return None
    return state


def read_refs_index(root: Path) -> Dict[str, str]:
    try:
        data = json.loads((ck_dir(root) / "refs.json").read_text())
        return {str(k): str(v) for k, v in data.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def approved_state_for_ref(root: Path, ref: str) -> Optional[ApprovedState]:
    spec = read_refs_index(root).get(ref)
    return read_approved_state(root, spec, ref) if spec else None


def record_approved_state(root: Path, spec: str, ref: str, commit: str, files: Mapping[str, Optional[str]],
                          approval: Optional[Mapping[str, object]] = None) -> ApprovedState:
    """Record `files` (path -> blob oid; None deletes the path) as approved; the other paths are kept.

    A missing, unreadable or other-ref state starts fresh. Also updates the ref -> spec index.
    """
    key = ensure_key(root)
    prev = read_approved_state(root, spec, ref)
    merged: Dict[str, str] = dict(prev.files) if prev else {}
    for path, oid in files.items():
        if oid is None:
            merged.pop(path, None)
        else:
            merged[path] = oid
    approvals = list(prev.approvals) if prev else []
    if approval is not None:
        approvals.append({"at": _now(), "commit": commit, **dict(approval)})
    state = ApprovedState(ref=ref, commit=commit, files=merged, approvals=tuple(approvals))
    payload = _state_payload(spec, state)
    _write_json(_spec_dir(root, spec) / "protected.json", {**payload, "mac": _seal(key, payload)})
    index = read_refs_index(root)
    index[ref] = spec
    _write_json(ck_dir(root) / "refs.json", index)
    return state
