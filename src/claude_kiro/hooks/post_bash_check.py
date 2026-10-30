"""Shell post-check: catch a Bash command that weakened an existing test after it ran.

The pre-tool guard takes a cheap snapshot before a command that may write (`take_snapshot`). After the command,
`check` compares the test files the command named against that snapshot and reports a weakening or deletion, the
way the edit path does. Everything here is best effort: any failure, or running past the 3-second budget, is
silent (the gate re-checks at Done).
"""

import glob
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from claude_kiro.hooks._shared import git_utils, guard_policy
from claude_kiro.hooks._shared.test_heuristics import is_test_file

BUDGET_S = 3.0
CONTENT_CAP = 512 * 1024
SNAPSHOT_MAX_AGE_S = 3600
MAX_TOKENS = 2000
MAX_TOKEN_LEN = 4096
MAX_CHECKOUTS = 8

_TOKEN = re.compile(r"[^\s'\"`;|&<>()]+")
_CD = re.compile(r"(?:^|[\s;&|(])cd\s+(?:--\s+)?([^\s;&|()<>]+)")
_GIT_C = re.compile(r"\bgit\s+(?:[^\s;&|]+\s+)*?-C\s+([^\s;&|()<>]+)")
_REF_WORDS = {"merge": 1, "rebase": 1, "pull": 1}


# ---------------------------------------------------------------------------------------- small helpers


def _cache_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "claude-kiro-hooks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe(text: str) -> str:
    return "".join(c if c.isalnum() or c in "_-." else "_" for c in text)[:120]


def _snapshot_path(input_data: Mapping[str, Any], command: str) -> Path:
    session = _safe(str(input_data.get("session_id") or "unknown"))
    tool_use_id = input_data.get("tool_use_id")
    key = _safe(tool_use_id) if isinstance(tool_use_id, str) and tool_use_id else hashlib.sha1(
        command.encode("utf-8", "replace")).hexdigest()
    return _cache_dir() / f"guardsnap-{session}-{key}.json"


def _dedup_path(input_data: Mapping[str, Any]) -> Path:
    session = _safe(str(input_data.get("session_id") or "unknown"))
    agent = input_data.get("agent_id")
    who = _safe(agent) if isinstance(agent, str) and agent else "main"
    return _cache_dir() / f"guarddedup-{session}-{who}.json"


def _write_atomic(path: Path, data: Any) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _toplevel(path: str) -> Optional[str]:
    top = git_utils.find_toplevel(path)
    return str(top) if top else None


def _read_text(path: Path) -> Tuple[str, Optional[str]]:
    """("ok", text) | ("absent", None) | ("skip", None) for unreadable, non-regular, binary or oversized files."""
    try:
        if not path.is_file():
            if not os.path.lexists(path):
                return "absent", None
            return "skip", None
        if path.stat().st_size > CONTENT_CAP:
            return "skip", None
        data = path.read_bytes()
    except FileNotFoundError:
        return "absent", None
    except (OSError, ValueError):
        return "skip", None
    if b"\0" in data:
        return "skip", None
    return "ok", data.decode("utf-8", errors="replace")


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def _command_text(input_data: Mapping[str, Any]) -> Optional[str]:
    tool_input = input_data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, Mapping) else None
    return command if isinstance(command, str) and command.strip() else None


def _caller(input_data: Mapping[str, Any]) -> str:
    return guard_policy.classify_caller(input_data, os.environ, False)


# ---------------------------------------------------------------------------------------- what a command names


def _named(command: str, cwd: str) -> Tuple[List[str], List[str]]:
    """(checkouts, absolute paths that exist) the command names. Order-preserving, deduplicated."""
    dirs = [cwd]
    for pattern in (_CD, _GIT_C):
        for match in pattern.findall(command)[:20]:
            raw = match.strip("'\"")
            dirs.append(raw if os.path.isabs(raw) else os.path.join(cwd, raw))
    paths: List[str] = []
    abs_tokens: List[str] = []
    for token in _TOKEN.findall(command)[:MAX_TOKENS]:
        if len(token) > MAX_TOKEN_LEN:
            continue
        options = [token] if os.path.isabs(token) else [os.path.join(d, token) for d in dirs]
        if os.path.isabs(token):
            abs_tokens.append(token)
        for option in options:
            try:
                found = glob.glob(option) if any(c in option for c in "*?[") else ([option] if os.path.lexists(option) else [])
            except (OSError, ValueError):
                found = []
            for hit in found[:1000]:
                paths.append(os.path.abspath(hit))
    checkouts: List[str] = []
    for source in [*dirs, *abs_tokens]:
        top = _toplevel(source)
        if top and top not in checkouts:
            checkouts.append(top)
        if len(checkouts) >= MAX_CHECKOUTS:
            break
    seen = set()
    unique = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return checkouts, unique


def _rel_paths(checkout: str, paths: List[str]) -> Optional[List[str]]:
    """The named paths inside `checkout` (relative, posix); None when it names none (all test files)."""
    out: List[str] = []
    prefix = checkout.rstrip(os.sep) + os.sep
    for p in paths:
        if p == checkout:
            out.append("")
        elif p.startswith(prefix):
            out.append(p[len(prefix):].replace(os.sep, "/"))
    return out or None


def _test_scope(named: Optional[List[str]], tests: List[str], config: Mapping[str, Any]) -> Optional[List[str]]:
    """The named paths that can hold a test: a test file, a directory with test files, or the checkout root.

    A command that names only other files (a bare `python rewrite.py`) names no test, so every test file of the
    checkout is a candidate (None), as design.md "Pre-command snapshot" says.
    """
    if named is None:
        return None
    kept = [n for n in named
            if n == "" or is_test_file(n, config) or any(t.startswith(n.rstrip("/") + "/") for t in tests)]
    return kept or None


def _candidates(files: List[str], named: Optional[List[str]]) -> List[str]:
    if named is None:
        return files
    picked = set()
    for rel in files:
        for n in named:
            if n == "" or rel == n or rel.startswith(n.rstrip("/") + "/"):
                picked.add(rel)
                break
    return sorted(picked)


def _owned(checkout: Path) -> set:
    from claude_kiro.hooks.pre_tool_guard import in_progress_test_files

    return in_progress_test_files(checkout)


# ---------------------------------------------------------------------------------------- pre-command snapshot


def take_snapshot(input_data: Mapping[str, Any], command: str, caller: str) -> Optional[Path]:
    """Write the snapshot for one Bash call; None when nothing is covered or it could not be written."""
    git_utils.set_deadline(BUDGET_S)
    try:
        from claude_kiro.config import load_config
        from claude_kiro.hooks.pre_tool_guard import session_mode

        cwd = str(input_data.get("cwd") or os.getcwd())
        checkouts, paths = _named(command, cwd)
        record: Dict[str, Any] = {}
        for checkout in checkouts:
            root = Path(checkout)
            config = load_config(root)
            if guard_policy.effective_mode(caller, session_mode(cwd, "tests")) is None:
                continue
            head = git_utils.head_sha(root)
            status = git_utils.status_paths(root)
            files = git_utils.project_files(root)
            if status is None or files is None:
                continue
            tests = [f for f in files if is_test_file(f, config)]
            named = _test_scope(_rel_paths(checkout, paths), tests, config)
            wanted = set(_candidates(tests, named))
            dirty: Dict[str, Any] = {}
            for rel in status:
                if rel not in wanted:
                    continue
                kind, text = _read_text(root / rel)
                if kind == "ok":
                    dirty[rel] = {"sha": _sha(text or ""), "content": text}
                elif kind == "absent":
                    dirty[rel] = {"sha": "absent", "content": None}
                else:
                    dirty[rel] = {"sha": "unknown", "content": None}
            record[checkout] = {"head": head, "paths": named, "dirty": dirty}
        if not record:
            return None
        path = _snapshot_path(input_data, command)
        _sweep(path.parent)
        _write_atomic(path, {"createdAt": time.time(), "caller": caller, "checkouts": record})
        return path
    except Exception:
        return None
    finally:
        git_utils.set_deadline(None)


def _sweep(directory: Path) -> None:
    now = time.time()
    try:
        for old in directory.glob("guardsnap-*"):
            try:
                if now - old.stat().st_mtime > SNAPSHOT_MAX_AGE_S:
                    old.unlink()
            except OSError:
                pass
    except OSError:
        pass


# ---------------------------------------------------------------------------------------- the check


def _load_snapshot(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if not isinstance(data, dict) or not isinstance(data.get("checkouts"), dict):
        return None
    return data


def _merge_refs(command: str) -> List[str]:
    """Refs a `git merge|rebase|pull` in the command brings in (`@{u}` for a bare pull)."""
    refs: List[str] = []
    words = _TOKEN.findall(command)
    for i, word in enumerate(words):
        if word != "git":
            continue
        j = i + 1
        while j < len(words) and (words[j].startswith("-") or words[j - 1] == "-C"):
            j += 1
        if j >= len(words) or words[j] not in _REF_WORDS:
            continue
        verb = words[j]
        args = [w for w in words[j + 1:j + 8] if not w.startswith("-")]
        if verb == "pull":
            # `git pull <remote> <branch>` brings in the fetched commit, not the local `<branch>`.
            args = ["FETCH_HEAD", f"{args[0]}/{args[1]}", args[1]] if len(args) >= 2 else ["@{u}", "FETCH_HEAD"]
        refs.extend(args[:3])
    return refs


def _incoming_refs(root: Path, old_head: Optional[str], head: Optional[str], command: str) -> List[str]:
    refs = _merge_refs(command)
    if old_head and head and head != old_head:
        out = git_utils._git(root, "rev-list", "--parents", "-n", "1", head)
        if out:
            parents = out.split()[1:]
            refs.extend(p for p in parents if p != old_head)
    return refs


def _check_checkout(checkout: str, snap: Mapping[str, Any], caller: str, command: str, deadline: float,
                    cwd: str) -> Optional[Any]:
    from claude_kiro.config import load_config
    from claude_kiro.hooks.pre_tool_guard import session_mode

    root = Path(checkout)
    config = load_config(root)
    mode = session_mode(cwd, "tests")
    if guard_policy.effective_mode(caller, mode) is None:
        return None
    old_head = snap.get("head") if isinstance(snap.get("head"), str) else None
    named = snap.get("paths") if isinstance(snap.get("paths"), list) else None
    dirty = snap.get("dirty") if isinstance(snap.get("dirty"), dict) else {}
    head = git_utils.head_sha(root)
    if head is None:
        return None
    files = git_utils.project_files(root)
    if files is None:
        return None
    tests = [f for f in files if is_test_file(f, config)]
    candidates = set(_candidates(tests, [str(p) for p in named] if named is not None else None))
    candidates.update(rel for rel in dirty if is_test_file(rel, config))
    moved = bool(old_head) and head != old_head
    if moved:
        for rel in git_utils.changed_since(root, old_head) or []:
            if is_test_file(rel, config):
                candidates.add(rel)
    if not candidates:
        return None
    base = git_utils.base_ref(root, config.get("base"), fallback_head=False)
    owned = _owned(root)
    incoming: Optional[List[str]] = None
    changes = []
    entry_clean: Dict[str, bool] = {}
    for rel in sorted(candidates):
        if time.monotonic() > deadline:
            return None
        kind, after = _read_text(root / rel)
        if kind == "skip":
            continue
        entry = dirty.get(rel) if isinstance(dirty.get(rel), dict) else None
        if entry is not None:
            if entry.get("sha") == "unknown":
                continue
            before = entry.get("content") if isinstance(entry.get("content"), str) else None
        else:
            before = git_utils.content_at(root, old_head if moved else head, rel)
        if after == before:
            continue
        entry_clean[rel] = entry is None and not moved
        exists_on_base: Optional[bool] = git_utils.exists_at(root, base, rel) if base else None
        matches = False
        if after is not None:
            if base and git_utils.content_at(root, base, rel) == after:
                matches = True
            else:
                if incoming is None:
                    incoming = _incoming_refs(root, old_head, head, command)
                matches = any(git_utils.content_at(root, ref, rel) == after for ref in incoming)
        changes.append(guard_policy.FileChange(rel, True, exists_on_base, rel in owned, True, before, after, matches))
    return guard_policy.decide_post_check(changes, caller, mode), checkout, {c.rel: (c.after, entry_clean.get(c.rel, False)) for c in changes}


def _dedup(finding: Any, afters: Dict[str, Dict[str, Tuple[Optional[str], bool]]], input_data: Mapping[str, Any]) -> Any:
    """Drop files already reported with the same content; remember the rest."""
    path = _dedup_path(input_data)
    try:
        seen = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(seen, dict):
            seen = {}
    except (OSError, ValueError):
        seen = {}
    keep = []
    for checkout, rel, kinds in finding:
        after, was_clean = afters.get(checkout, {}).get(rel, (None, False))
        mark = _sha(after) if after is not None else "absent"
        key = f"{checkout}:{rel}"
        if seen.get(key) == mark and not was_clean:  # a file that was clean before the command was restored since
            continue
        seen[key] = mark
        keep.append((checkout, rel, kinds))
    if keep:
        try:
            _write_atomic(path, seen)
        except OSError:
            pass
    return keep


def check(input_data: Dict[str, Any]) -> Optional[str]:
    """The post-check message for a finished Bash call, or None. Never raises."""
    try:
        if not isinstance(input_data, dict) or input_data.get("tool_name") not in (None, "Bash"):
            return None
        command = _command_text(input_data)
        if command is None or not guard_policy.may_write(command):
            return None
        path = _snapshot_path(input_data, command)
        if not path.exists():
            return None
        snapshot = _load_snapshot(path)
        if snapshot is None:
            return None
        git_utils.set_deadline(BUDGET_S)
        deadline = time.monotonic() + BUDGET_S
        caller = _caller(input_data)
        found: List[Tuple[str, str, Tuple[str, ...]]] = []
        afters: Dict[str, Dict[str, Tuple[Optional[str], bool]]] = {}
        mode = None
        for checkout, snap in snapshot["checkouts"].items():
            if not isinstance(checkout, str) or not isinstance(snap, dict):
                continue
            try:
                result = _check_checkout(checkout, snap, caller, command, deadline, str(input_data.get("cwd") or os.getcwd()))
            except Exception:
                continue
            if not result:
                continue
            finding, _root, after_map = result
            if finding is None:
                continue
            mode = mode or finding.mode
            afters[checkout] = after_map
            found.extend((checkout, rel, kinds) for rel, kinds in finding.files)
        if time.monotonic() > deadline or not found or mode is None:
            return None
        found = _dedup(found, afters, input_data)
        if not found:
            return None
        finding = guard_policy.PostCheckFinding(caller, mode, tuple((rel, kinds) for _c, rel, kinds in found))  # type: ignore[arg-type]
        message = guard_policy.post_check_reason(finding)
        _log(input_data, command, caller, message, found[0][1])
        return message
    except Exception:
        return None
    finally:
        git_utils.set_deadline(None)


def _log(input_data: Mapping[str, Any], command: str, caller: str, message: str, rel: str) -> None:
    from claude_kiro.analytics import guardlog

    def text(key: str) -> Optional[str]:
        value = input_data.get(key)
        return value if isinstance(value, str) and value else None

    guardlog.append_event(guardlog.GuardEvent(
        ts=guardlog.now_iso(), session_id=text("session_id") or "unknown", agent_id=text("agent_id"),
        agent_type=text("agent_type"), caller=caller, tool="Bash", decision="post-check",
        category="post_check_weakening", reason=message, path=rel, command=command,
        family=guard_policy.command_family(command), tool_use_id=text("tool_use_id")))


def hook(input_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    message = check(input_data)
    return {"decision": "block", "reason": message} if message else None
