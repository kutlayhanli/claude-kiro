"""Pure guard rules: who is calling, what a command destroys, and what to block.

Every rule here (design seams S1-S4 and S6-S10) is a pure function over facts
the hooks gather. No file, git, clock or environment access happens in this
module; the hooks call it and never repeat a rule.
"""

import posixpath
import re
from dataclasses import dataclass
from typing import Any, List, Literal, Mapping, Optional, Sequence, Tuple

from claude_kiro.handoff import is_headless
from claude_kiro.hooks._shared.test_heuristics import weakening

Caller = Literal["task_agent", "main_session"]

DELETES_KIND = "deletes the test file"
MOVES_KIND = "moves the test file"


@dataclass(frozen=True)
class DestroyTarget:
    path: str  # absolute path, resolved against the cwd in force at its segment
    op: Literal["delete", "move"]
    program: str  # "rm" | "mv" | "git rm" | "git mv" | "unlink" | "truncate"


@dataclass(frozen=True)
class TargetFacts:
    rel: str  # path relative to the target's own checkout
    op: Literal["delete", "move"]
    is_test: bool
    exists_on_base: Optional[bool]  # None = could not tell
    owned: bool  # listed by an In Progress test-track task in that checkout


@dataclass(frozen=True)
class EditFacts:
    rel: str
    target: Literal["test", "requirements", "test_plan", "other"]
    weakening: Tuple[str, ...]
    owned: bool
    exists_on_base: Optional[bool]
    implementation_started: bool  # frozen-spec files only
    only_adds: bool  # frozen-spec files only


@dataclass(frozen=True)
class GuardDecision:
    block: bool
    mode: Optional[Literal["ask", "deny"]] = None  # None when block is False
    paths: Tuple[str, ...] = ()
    kinds: Tuple[str, ...] = ()
    category: Optional[str] = None  # "test_delete" | "test_move" | "test_weakening" | "spec_edit"


@dataclass(frozen=True)
class FileChange:
    rel: str
    is_test: bool
    exists_on_base: Optional[bool]
    owned: bool
    changed_by_command: bool
    before: Optional[str]  # content before the command (None: did not exist)
    after: Optional[str]  # content after (None: removed)
    matches_incoming: bool = False  # after equals the base ref's or an incoming ref's content (D19)


@dataclass(frozen=True)
class PostCheckFinding:
    caller: Caller
    mode: Literal["ask", "deny"]
    files: Tuple[Tuple[str, Tuple[str, ...]], ...]  # (rel, weakening kinds)


# --- shell scanning -------------------------------------------------------------------------------

@dataclass
class _Segment:
    words: List[str]
    writes: List[str]  # targets of write redirects (not /dev/null, not fd duplication)
    tee: bool = False


_SEP_CHARS = ";&|()\n"
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _scan(command: str, strict: bool = False) -> List[_Segment]:
    """Split a command into segments of words with write redirects noted. Never raises."""
    segments: List[_Segment] = []
    words: List[str] = []
    writes: List[str] = []
    buf: List[str] = []
    have = False  # a word is in progress (so '' counts)
    pending_redirect = False
    heredocs: List[str] = []

    def end_word() -> None:
        nonlocal have, pending_redirect
        if not have:
            return
        word = "".join(buf)
        buf.clear()
        have = False
        if pending_redirect:
            pending_redirect = False
            if word != "/dev/null" and not word.startswith("&"):
                writes.append(word)
            return
        words.append(word)

    def end_segment() -> None:
        nonlocal words, writes, pending_redirect
        end_word()
        pending_redirect = False
        if words or writes:
            segments.append(_Segment(words, writes))
        words, writes = [], []

    text = command if isinstance(command, str) else ""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            if i + 1 < n and text[i + 1] == "\n":
                i += 2
                continue
            if i + 1 < n:
                buf.append(text[i + 1])
                have = True
            i += 2
            continue
        if c == "'":
            j = text.find("'", i + 1)
            if j < 0 and strict:
                raise ValueError("unterminated quote")
            j = n if j < 0 else j
            buf.append(text[i + 1 : j])
            have = True
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n and strict:
                raise ValueError("unterminated quote")
            have = True
            i = j + 1
            continue
        if c == "<" and text.startswith("<<", i) and not text.startswith("<<<", i):
            j = i + 2
            if j < n and text[j] == "-":
                j += 1
            while j < n and text[j] in " \t":
                j += 1
            m = re.match(r"""(['"]?)([A-Za-z0-9_.\-]+)\1""", text[j:])
            if m:
                heredocs.append(m.group(2))
                i = j + m.end()
                end_word()
                continue
        if c == ">":
            # fd prefix such as 2> or &> belongs to the redirect, not to a word
            if have and "".join(buf).isdigit():
                buf.clear()
                have = False
            j = i + 1
            if j < n and text[j] in ">|":
                j += 1
            end_word()
            if j < n and text[j] == "&":
                j += 1
                while j < n and text[j] in " \t":
                    j += 1
                k = j
                while k < n and text[k] not in " \t\n;&|()":
                    k += 1
                i = k  # >&2, >&- : duplication, not a file write
                continue
            pending_redirect = True
            i = j
            continue
        if c in " \t\r":
            end_word()
            i += 1
            continue
        if c in _SEP_CHARS:
            if c == "&" and i + 1 < n and text[i + 1] == ">":
                # &> file
                end_word()
                pending_redirect = True
                i += 2
                if i < n and text[i] == ">":
                    i += 1
                continue
            end_segment()
            i += 1
            if c == "\n" and heredocs:
                for delim in heredocs:
                    while i < n:
                        eol = text.find("\n", i)
                        eol = n if eol < 0 else eol
                        line = text[i:eol].strip()
                        i = eol + 1
                        if line == delim:
                            break
                heredocs = []
            continue
        buf.append(c)
        have = True
        i += 1
    end_segment()
    for seg in segments:
        seg.tee = any(w == "tee" for w in seg.words[:1])
    return segments


def _strip_prefix(words: Sequence[str]) -> List[str]:
    """Drop env assignments and `command`/`rtk` wrappers from the front of a segment."""
    out = list(words)
    while out:
        if _ASSIGN.match(out[0]) or out[0] in ("command", "rtk"):
            out.pop(0)
            continue
        break
    return out


def _git_split(words: Sequence[str]) -> Tuple[List[str], Optional[str], List[str]]:
    """For `git ...` words (git removed): (-C dirs in order, subcommand, args after it)."""
    dirs: List[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w == "-C" and i + 1 < len(words):
            dirs.append(words[i + 1])
            i += 2
        elif w == "-c" and i + 1 < len(words):
            i += 2
        elif w.startswith("-"):
            i += 1
        else:
            return dirs, w, list(words[i + 1 :])
    return dirs, None, []


def _abs(base: str, path: str) -> str:
    return posixpath.normpath(path if path.startswith("/") else posixpath.join(base, path))


def _operands(args: Sequence[str], value_flags: Sequence[str] = ()) -> List[str]:
    out: List[str] = []
    i = 0
    only = False
    while i < len(args):
        a = args[i]
        if only:
            out.append(a)
        elif a == "--":
            only = True
        elif a in value_flags:
            i += 1
        elif a.startswith("-") and a != "-":
            pass
        else:
            out.append(a)
        i += 1
    return out


def destroy_targets(command: str, cwd: str) -> List[DestroyTarget]:
    """S1: paths a command deletes or moves. Redirects, sed/perl -i and find yield none."""
    try:
        segments = _scan(command, strict=True)
    except Exception:  # defensive: fall back to whitespace split
        segments = [_Segment(s.split(), []) for s in re.split(r"&&|\|\||[;|\n]", command or "")]
    cur = cwd
    targets: List[DestroyTarget] = []
    for seg in segments:
        words = _strip_prefix(seg.words)
        if not words:
            continue
        prog, args = words[0], words[1:]
        if prog == "cd":
            ops = _operands(args)
            if ops and ops[0] not in ("-", "~"):
                cur = _abs(cur, ops[0])
            continue
        base = cur
        name = prog
        if prog == "git":
            dirs, sub, args = _git_split(args)
            for d in dirs:
                base = _abs(base, d)
            if sub not in ("rm", "mv"):
                continue
            name = f"git {sub}"
        paths: List[str] = []
        op: Literal["delete", "move"] = "delete"
        if name in ("rm", "git rm", "unlink"):
            paths = _operands(args)
        elif name == "truncate":
            paths = _operands(args, ("-s", "--size", "-r", "--reference"))
        elif name in ("mv", "git mv"):
            op = "move"
            if any(a in ("-t",) or a.startswith("--target-directory") for a in args):
                paths = _operands(args, ("-t", "--target-directory", "-S", "--suffix"))
            else:
                ops = _operands(args, ("-S", "--suffix"))
                paths = ops[:-1] if len(ops) >= 2 else []
        else:
            continue
        for p in paths:
            if not p:
                continue
            targets.append(DestroyTarget(_abs(base, p), op, name))
    return targets


_READ_ONLY = frozenset(
    "ls cat head tail grep rg wc echo pwd which sleep true test [ date diff stat file du jq cd".split()
)
_FIND_WRITE = ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls")
_GIT_READ = frozenset("status log diff show rev-parse ls-files cat-file merge-base".split())
_BRANCH_LIST = frozenset("-a -r -v -vv --list --show-current --contains --merged --no-merged".split())
_CK_READ = frozenset(["gate", "plan", "lint"])


def _segment_read_only(seg: _Segment) -> bool:
    if seg.writes or seg.tee:
        return False
    words = _strip_prefix(seg.words)
    if not words:
        return True
    prog, args = words[0], words[1:]
    if prog == "find":
        return not any(a in _FIND_WRITE or a.startswith("-fprint") for a in args)
    if prog == "git":
        _, sub, rest = _git_split(args)
        if sub in _GIT_READ:
            return True
        if sub == "worktree":
            return bool(rest) and rest[0] == "list"
        if sub == "branch":
            return all(a in _BRANCH_LIST for a in rest)
        return False
    if prog == "ck":
        i, rest = 0, list(args)
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] == "-C" else 1
        rest = rest[i:]
        if not rest:
            return False
        if rest[0] in _CK_READ:
            return True
        if rest[0] == "worktree":
            return len(rest) > 1 and rest[1] in ("status", "report")
        if rest[0] == "stats":
            return len(rest) > 1 and rest[1] == "show"
        return False
    return prog in _READ_ONLY


def may_write(command: str) -> bool:
    """S2: False only when the whole command is read-only (the post-check pre-filter)."""
    try:
        segments = _scan(command)
    except Exception:
        return True
    return not all(_segment_read_only(s) for s in segments)


_PYTHON = re.compile(r"^python(\d+(\.\d+)?)?$")


def command_family(command: Optional[str]) -> str:
    """S3: one of sed rm mv git python redirect other."""
    if not isinstance(command, str):
        return "other"
    try:
        segments = _scan(command)
    except Exception:
        return "other"
    first: Optional[List[str]] = None
    for seg in segments:
        words = _strip_prefix(seg.words)
        if words and words[0] == "cd":
            continue
        if words:
            first = words
            break
    if first:
        prog, args = first[0], first[1:]
        if prog == "sed":
            return "sed"
        if prog == "perl" and any(a.startswith("-") and not a.startswith("--") and "i" in a[1:] for a in args):
            return "sed"
        if prog in ("rm", "unlink", "truncate"):
            return "rm"
        if prog == "mv":
            return "mv"
        if prog == "git":
            return "git"
        if _PYTHON.match(prog):
            return "python"
        if prog == "uv" and args[:1] == ["run"]:
            rest = [a for a in args[1:] if not a.startswith("-")]
            if rest and _PYTHON.match(rest[0]):
                return "python"
    if any(seg.writes for seg in segments):
        return "redirect"
    return "other"


# --- caller and mode ------------------------------------------------------------------------------

def classify_caller(hook_input: Any, env: Mapping[str, str], workflow_marker: bool) -> Caller:
    """S4: task agent or main session. Paths never enter the decision."""
    agent_id = hook_input.get("agent_id") if isinstance(hook_input, Mapping) else None
    has_agent_id = isinstance(agent_id, str) and agent_id != ""
    if has_agent_id:
        return "task_agent"
    if is_headless(env if isinstance(env, Mapping) else {}):
        return "task_agent"
    if workflow_marker is True:
        return "task_agent"
    return "main_session"


def effective_mode(caller: Caller, configured: Optional[str]) -> Optional[Literal["ask", "deny"]]:
    """S6: None when the kind is off; deny for task agents; the configured mode for the main session."""
    if configured not in ("ask", "deny"):
        return None
    return "deny" if caller == "task_agent" else configured  # type: ignore[return-value]


# --- decisions ------------------------------------------------------------------------------------

def decide_bash(targets: Sequence[TargetFacts], caller: Caller, mode: Optional[str]) -> GuardDecision:
    """S7: block only deleting or moving an existing, unowned test file."""
    eff = effective_mode(caller, mode)
    if eff is None:
        return GuardDecision(False)
    hits = [t for t in targets if t.is_test and t.exists_on_base is True and not t.owned]
    if not hits:
        return GuardDecision(False)
    paths: List[str] = []
    for t in hits:
        if t.rel not in paths:
            paths.append(t.rel)
    kinds: List[str] = []
    for t in hits:
        kind = DELETES_KIND if t.op == "delete" else MOVES_KIND
        if kind not in kinds:
            kinds.append(kind)
    category = "test_delete" if any(t.op == "delete" for t in hits) else "test_move"
    return GuardDecision(True, eff, tuple(paths[:4]), tuple(kinds), category)


def decide_edit(facts: EditFacts, caller: Caller, mode: Optional[str]) -> GuardDecision:
    """S8: block a test weakening, or (task agents only) a change to frozen spec text."""
    eff = effective_mode(caller, mode)
    if eff is None:
        return GuardDecision(False)
    if facts.target == "test":
        if facts.weakening and not facts.owned and facts.exists_on_base is not False:
            return GuardDecision(True, eff, (facts.rel,), tuple(facts.weakening), "test_weakening")
        return GuardDecision(False)
    if facts.target in ("requirements", "test_plan"):
        if not facts.implementation_started or facts.only_adds or caller == "main_session":
            return GuardDecision(False)
        return GuardDecision(True, eff, (facts.rel,), (), "spec_edit")
    return GuardDecision(False)


def decide_post_check(
    changes: Sequence[FileChange], caller: Caller, mode: Optional[str]
) -> Optional[PostCheckFinding]:
    """S9: the test files a command weakened, or None."""
    eff = effective_mode(caller, mode)
    if eff is None:
        return None
    files: List[Tuple[str, Tuple[str, ...]]] = []
    for ch in changes:
        if not (ch.changed_by_command and ch.is_test and ch.exists_on_base is True and not ch.owned):
            continue
        if ch.matches_incoming:
            continue
        if ch.after is None:
            reasons = [DELETES_KIND]
        elif ch.before is None:
            continue
        else:
            reasons = weakening(ch.before, ch.after)
        if reasons:
            files.append((ch.rel, tuple(reasons)))
    if not files:
        return None
    return PostCheckFinding(caller, eff, tuple(files))


# --- reason texts ---------------------------------------------------------------------------------

_ORACLE = (
    "Tests are the oracle: weakening them to get a pass is not allowed. If the test is wrong or "
    "contradicts the spec, stop and tell me instead."
)


def block_reason(caller: Caller, target_kind: str, rel: str, kinds: Sequence[str], tool: str) -> str:
    """S10: the text given to Claude when a call is blocked. `target_kind` is test, requirements or test_plan."""
    is_bash = tool == "Bash"
    if target_kind in ("requirements", "test_plan"):
        if caller == "task_agent":
            return (
                f"[ck guard] Denied: {rel} is the contract of a spec whose implementation has started; "
                "existing text cannot be changed or removed by an agent (adding is allowed). Do not apply "
                "this change. If you are revising the spec in /spec:review, return it under \"escalated\" "
                "as a decision for me, with the proposed text; otherwise report the conflict."
            )
        what = "requirement" if target_kind == "requirements" else "test case"
        return (
            f"[ck guard] {rel} is the contract for a spec whose implementation has started. Changing "
            f"{what}s to match the code hides failures (adding new ones is fine). If a {what} "
            "is wrong, stop and raise it with me; approve this edit only if I asked for the change."
        )
    kind_text = ", ".join(kinds) if kinds else "changes"
    if caller == "task_agent":
        what = "command" if is_bash else "edit"
        where = f"{kind_text} {rel}" if kind_text.endswith("the test file") else f"{kind_text} in {rel}"
        return (
            f"[ck guard] Denied: this {what} {where}, an existing test that no In Progress test-track "
            "task owns. Keep the test. Do not work around this block (no other command, script or tool to "
            "get the same change). Leave the task In Progress and report the conflict in guardStop, "
            "quoting the test and the requirement it contradicts."
        )
    if is_bash:
        return (
            f"[ck guard] This command deletes, moves, or rewrites test files ({rel}). Tests are the "
            "oracle for spec tasks; if one is wrong, stop and tell me instead of removing it."
        )
    return (
        f"[ck guard] This edit {kind_text} in {rel}, an existing test that no In Progress "
        f"test-track task owns. {_ORACLE}"
    )


def post_check_reason(finding: PostCheckFinding) -> str:
    """S10: the post-check message, by caller and mode."""
    parts = [f"{rel}: {', '.join(kinds)}" for rel, kinds in finding.files]
    head = f"[ck post-check] Your last command weakened {'; '.join(parts)}."
    first = finding.files[0][0] if finding.files else "<file>"
    restore = f'Restore the test now (for example "command git -C <checkout> checkout -- {first}" if it had no other changes)'
    if finding.caller == "task_agent":
        return (
            f"{head} {restore}, keep it, and report the conflict in guardStop quoting the test and "
            "the requirement."
        )
    if finding.mode == "ask":
        return f"{head} Ask me before keeping this change."
    return f"{head} {restore}."
