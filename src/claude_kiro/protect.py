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
import sys
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
    head: str = ""


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
        problems.append("the approved protected state is missing or unreadable; when it is missing, `ck worktree "
                        "integration` records it again; when it is damaged, review the protected files and delete "
                        "it first, since ck never reseals a state it cannot verify")
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
        path = _spec_dir(root, spec) / "protected.json"
        if not path.is_file():  # a directory or a FIFO counts as unreadable, without blocking on it
            return None
        data = json.loads(path.read_text())
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


def approved_state_file(root: Path, spec: str) -> Optional[Path]:
    """The path of `spec`'s state file when something is there, verified or not; None when nothing is."""
    try:
        path = _spec_dir(root, spec) / "protected.json"
    except ValueError:
        return None
    return path if os.path.lexists(path) else None


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


# --------------------------------------------------------------------------- git hooks

def hook_status_json(status: HookStatus) -> Dict[str, Any]:
    """`status` as JSON consumers read it: the one place that names `chainedTo`."""
    return {"installed": status.installed, "active": status.active, "intact": status.intact,
            "chainedTo": status.chained_to, "problems": list(status.problems)}


# Every client-side hook name git runs; each gets a script in <common>/ck/hooks that chains to the project's own.
HOOK_NAMES = (
    "applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit", "pre-merge-commit", "prepare-commit-msg",
    "commit-msg", "post-commit", "pre-rebase", "post-checkout", "post-merge", "pre-push", "pre-auto-gc",
    "post-rewrite", "sendemail-validate", "fsmonitor-watchman", "post-index-change", "reference-transaction",
    "push-to-checkout", "pre-receive", "update", "proc-receive", "post-receive", "post-update",
)
CK_HOOKS = ("reference-transaction", "pre-merge-commit")
# Known gap: `git symbolic-ref` and the new name of `git branch -m` / `-c` reach no reference-transaction hook
# (git 2.43; the hook sees only the delete of the old name), so an integrate/* ref re-created that way is not
# refused here. The run-end audit reports it as drift (design, Security).
# Hooks whose mere presence changes what git does (push-to-checkout replaces the updateInstead worktree update,
# proc-receive takes over receive-pack ref updates): installed only when the chain target provides them.
_PRESENCE_HOOKS = ("push-to-checkout", "proc-receive")
_CHAIN_FILE = "chain.json"


def _hook_names(chain: str) -> Tuple[str, ...]:
    return tuple(n for n in HOOK_NAMES
                 if n not in _PRESENCE_HOOKS or os.access(os.path.join(chain, n), os.X_OK))


def _chain_of(meta: Optional[Mapping[str, Any]]) -> Optional[str]:
    value = (meta.get("previous") or meta.get("chain")) if meta else None
    return str(value) if value else None


def hooks_dir(root: Path) -> Path:
    return ck_dir(root) / "hooks"


def _sh(value: str) -> str:
    import shlex

    return shlex.quote(value)


def _script(name: str, chain: str, python: str) -> str:
    head = ("#!/bin/sh\n# Installed by ck: refuses unapproved protected changes to integrate/* and chains to the "
            "project's own hooks. Reinstall with `ck worktree integration SPEC`.\n"
            f"chain={_sh(chain)}\n")
    if name not in CK_HOOKS:
        return head + f'if [ -x "$chain/{name}" ]; then exec "$chain/{name}" "$@"; fi\nexit 0\n'
    runner = (
        "run_ck() {\n"
        "  if command -v ck >/dev/null 2>&1; then ck git-hook \"$@\"; return $?; fi\n"
        f"  if [ -x {_sh(python)} ]; then {_sh(python)} -m claude_kiro.cli git-hook \"$@\"; return $?; fi\n"
        "  return 127\n}\n"
        "no_ck() {\n"
        "  echo \"ck: refused: ck is not available to check this change to an integrate/* branch; "
        "reinstall the hooks with ck worktree integration SPEC\" >&2\n  exit 1\n}\n")
    if name == "reference-transaction":
        return head + runner + (
            "input=''\n"
            "while IFS= read -r line || [ -n \"$line\" ]; do input=\"$input$line\n\"; done\n"
            f"if [ -x \"$chain/{name}\" ]; then printf '%s' \"$input\" | \"$chain/{name}\" \"$@\" || exit $?; fi\n"
            "[ \"$1\" = prepared ] || exit 0\n"
            "case \"$input\" in *' refs/heads/integrate/'*) ;; *) exit 0 ;; esac\n"
            "printf '%s' \"$input\" | run_ck reference-transaction \"$1\"\n"
            "rc=$?\n[ $rc -eq 127 ] && no_ck\nexit $rc\n")
    return head + runner + (
        f"if [ -x \"$chain/{name}\" ]; then \"$chain/{name}\" \"$@\" || exit $?; fi\n"
        "case \"$(git symbolic-ref -q HEAD)\" in refs/heads/integrate/*) ;; *) exit 0 ;; esac\n"
        "run_ck pre-merge-commit\nrc=$?\n[ $rc -eq 127 ] && no_ck\nexit $rc\n")


def _sha(text: bytes) -> str:
    return hashlib.sha256(text).hexdigest()


def _read_chain(root: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads((hooks_dir(root) / _CHAIN_FILE).read_text())
        return data if isinstance(data, dict) and isinstance(data.get("scripts"), dict) else None
    except (OSError, ValueError):
        return None


def _git_out(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _same_path(cwd: Path, value: str, target: Path) -> bool:
    return os.path.realpath(os.path.join(str(cwd), os.path.expanduser(value))) == os.path.realpath(str(target))


def hook_status(root: Path, worktree: Optional[Path] = None) -> HookStatus:
    """Whether ck's hooks are installed, active for `worktree` and unmodified. Writes nothing."""
    worktree = worktree or root
    problems: List[str] = []
    try:
        d = hooks_dir(root)
        meta = _read_chain(root)
        recorded: Mapping[str, str] = meta["scripts"] if meta else {}
        names = tuple(recorded) if meta else ()
        installed = bool(meta) and all(n in recorded for n in HOOK_NAMES if n not in _PRESENCE_HOOKS) \
            and all((d / n).is_file() for n in names)
        value = _git_out(worktree, "config", "--get", "core.hooksPath")
        active = bool(value) and _same_path(worktree, value, d)
        intact = installed
        if installed:
            for n in names:
                try:
                    ok = _sha((d / n).read_bytes()) == recorded.get(n) and os.access(d / n, os.X_OK)
                except OSError:
                    ok = False
                if not ok:
                    intact = False
                    problems.append(f"hook {n} was modified")
                    break
        elif not meta:
            problems.append("ck's git hooks are not installed")
        else:
            problems.append("ck's git hook scripts are missing")
        if value and not active:
            problems.append(f"core.hooksPath is {value!r}, not ck's hooks directory")
        elif not value:
            problems.append("core.hooksPath is not set")
        chained = _chain_of(meta)
        return HookStatus(installed, active, intact, chained, tuple(problems))
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        return HookStatus(False, False, False, None, (f"cannot read the hook state: {exc}",))


def _writable_target(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def install_hooks(root: Path) -> HookStatus:
    """Install ck's hooks (idempotent); an OS error is returned in `problems`, never raised; never half-installs."""
    try:
        common = common_dir(root)
        d = common / "ck" / "hooks"
        refuse = [f"{p} is not writable (permission denied)"
                  for p in (_writable_target(d), common / "config", _writable_target(common / "ck"))
                  if not os.access(p, os.W_OK)]
        if refuse:
            status = hook_status(root)
            return HookStatus(status.installed, status.active, status.intact, status.chained_to,
                              tuple(refuse) + status.problems)
        ensure_key(root)
        current = _git_out(root, "config", "--get", "core.hooksPath")
        meta = _read_chain(root)
        if current and not _same_path(root, current, d):
            chain = current
        elif _chain_of(meta):
            chain = str(_chain_of(meta))
        else:
            chain = str(common / "hooks")
        python = sys.executable or "python3"
        d.mkdir(parents=True, exist_ok=True)
        shas: Dict[str, str] = {}
        names = _hook_names(chain)
        for name in names:
            text = _script(name, chain, python).encode()
            shas[name] = _sha(text)
            path = d / name
            if not (path.is_file() and path.read_bytes() == text and os.access(path, os.X_OK)):
                tmp = d / f".{name}.{secrets.token_hex(4)}.tmp"
                tmp.write_bytes(text)
                os.chmod(tmp, 0o755)
                os.replace(tmp, path)
        _write_json(d / _CHAIN_FILE, {"previous": chain, "python": python, "scripts": shas})
        if not (current and _same_path(root, current, d)):
            subprocess.run(["git", "-C", str(root), "config", "core.hooksPath", str(d)], check=True,
                           capture_output=True, text=True)
        return hook_status(root)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = (getattr(exc, "stderr", "") or "").strip() or str(exc)
        status = hook_status(root)
        return HookStatus(status.installed, status.active, status.intact, status.chained_to,
                          (f"cannot install ck's git hooks: {detail}",) + status.problems)


def ensure_hooks(root: Path, worktree: Optional[Path] = None) -> HookStatus:
    """Nothing when the hooks are installed and active (an altered script stays `intact: false` for the run-end
    audit to report, 10.2); otherwise (re)install them, chaining to a moved `core.hooksPath`."""
    status = hook_status(root, worktree)
    if status.installed and status.active:
        return status
    return install_hooks(root)


def hooks_managed(root: Path) -> bool:
    """True when ck ever installed its hooks in this repository (its key exists)."""
    try:
        return (ck_dir(root) / "key").exists()
    except (OSError, subprocess.CalledProcessError):
        return False


def protected_files_at(root: Path, commit: str) -> Dict[str, str]:
    """Protected paths (unquoted) of `commit` -> blob oid."""
    out = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "-z", commit], capture_output=True).stdout
    files: Dict[str, str] = {}
    for entry in out.split(b"\0"):
        meta, _, name = entry.partition(b"\t")
        path = name.decode("utf-8", "replace")
        if path and is_protected(path):
            files[path] = meta.split()[2].decode()
    return files


# --------------------------------------------------------------------------- run-end audit

def _fixture_audit(root: Path, spec: str, head: str, checkout: Path) -> Dict[str, Any]:
    """Lock drift and tampering of recorded fixtures at `head` (15.2, 15.5), as the audit reports them."""
    from . import fixtures
    from .config import load_config

    config = load_config(checkout)
    out: Dict[str, Any] = {"drift": [], "lockTampered": False, "handcrafted": []}
    try:
        fixtures.fixture_globs(dict(config))
    except fixtures.ConfigError as exc:
        out["configError"] = str(exc)
        return out
    try:
        entries, tampered = fixtures.read_lock(root, spec)
    except OSError:
        entries, tampered = {}, True  # a lock that cannot be read cannot be trusted
    out["lockTampered"] = tampered
    changes: Dict[str, Optional[str]] = {}
    for path in sorted(entries):
        proc = subprocess.run(["git", "-C", str(root), "show", f"{head}:{path}"], capture_output=True)
        changes[path] = fixtures.sha256_text(proc.stdout) if proc.returncode == 0 else None
    report = fixtures.lock_decision(entries, changes, fixtures.read_labels(checkout, config), tampered=tampered)
    deleted_hand = [{"file": h["file"], "task": h["task"], "change": "deleted"}
                    for h in report.handcrafted if changes[h["file"]] is None]
    out["drift"] = [dict(d) for d in sorted([*report.drift, *deleted_hand], key=lambda d: d["file"])]
    out["handcrafted"] = [dict(h) for h in report.handcrafted if changes[h["file"]] is not None]
    return out


def audit(root: Path, spec: str, into: Optional[str] = None) -> AuditReport:
    """Gather the inputs of `audit_decision` for `into` (default integrate/<spec>) and decide (10.1-10.5)."""
    from . import worktrees as wt

    branch = into or wt.integration_branch(spec)
    ref = f"refs/heads/{branch}"
    checkout = wt.checkout_of(root, branch) or root
    proc = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
                          capture_output=True, text=True)
    head = proc.stdout.strip() if proc.returncode == 0 else ""
    approved = read_approved_state(root, spec, ref)
    current = protected_files_at(root, head) if head else {}
    hooks = hook_status(root, checkout)
    try:
        allow = wt.land_allow_paths(checkout)
    except Exception:  # a config the audit cannot read must not hide the drift
        allow = []
    fixture_report = _fixture_audit(root, spec, head, checkout) if head else \
        {"drift": [], "lockTampered": False, "handcrafted": []}
    report = audit_decision(approved, current, allow, hooks,
                            {**fixture_report, "lock_tampered": fixture_report.get("lockTampered")})
    problems = list(report.problems)
    if not report.complete:
        if approved_state_file(root, spec) is None:
            reason = "no approved state is recorded for this spec"
        else:
            reason = "it is damaged, sealed for another spec or ref, or fails its mac"
        problems = [f"approved state unreadable: {reason}"] + problems[1:]
    if not head:
        problems.append(f"branch {branch} does not exist")
    if fixture_report.get("configError"):
        problems.append(f"fixtures config error: {fixture_report['configError']}")
    clean = report.clean and bool(head) and not fixture_report.get("configError")
    return AuditReport(complete=report.complete, clean=clean, drift=report.drift, hooks=hooks,
                       fixtures=fixture_report, problems=tuple(problems), head=head)
