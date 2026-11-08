"""D3 failure model: the `Failure` record, its classification, truncation and the readers the gate uses.

Pure seams (`classify_kind`, `truncate_message`, `missing_command`) take plain values so the properties can call
them directly. The readers turn a gate command's output into `Failure` entries: the plugin's JSON-lines report
(`read_report`), pytest's short summary (`parse_short_summary`) or, for other runners, the known test file names
named in the output (`failing_files`).
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Literal, Optional, Sequence, Tuple

Kind = Literal["assertion", "import", "collection", "timeout", "error"]
KINDS: Tuple[str, ...] = ("assertion", "import", "collection", "timeout", "error")
MESSAGE_LIMIT = 500
DETAIL_LIMIT = 4000

IMPORT_ERRORS = ("ImportError", "ModuleNotFoundError")


@dataclass(frozen=True)
class Failure:
    node: Optional[str]          # pytest node id; None for file- or command-level entries
    file: Optional[str]          # repo-relative test file; None when only the command is known
    kind: Kind
    message: str                 # first line of the crash message, at most MESSAGE_LIMIT characters
    truncated: bool              # the message was cut (3.5)
    command: str                 # the Verify command that produced it
    exc_type: Optional[str]      # "AssertionError", "ModuleNotFoundError", ...
    phase: Optional[str]         # "setup" | "call" | "teardown" | "collect" | None
    crash_path: Optional[str]    # file of the innermost frame, repo-relative when inside the repo
    crash_line: Optional[int]    # line of the innermost frame in crash_path
    detail: str                  # longer text kept for classification, at most DETAIL_LIMIT; never emitted
    missing_command: Optional[str]  # command path from missing_command(), e.g. "mtr thesis new"

    def as_json(self, tc_id: Optional[str] = None, tasks: Iterable[str] = (),
                attributed_by: Optional[str] = None) -> Dict[str, object]:
        """The ck-gate/1 failure entry. `detail` is never emitted.

        `missingCommand` carries the command path here; the gate replaces it with `{command, providers}`
        when the graph knows the command.
        """
        return {
            "node": self.node,
            "tcId": tc_id,
            "tasks": list(tasks),
            "attributedBy": attributed_by,
            "file": self.file,
            "kind": self.kind,
            "message": self.message,
            "truncated": self.truncated,
            "command": self.command,
            "missingCommand": self.missing_command,
        }


# --------------------------------------------------------------------------- classification (3.2)

def classify_kind(exc_type: Optional[str], phase: Optional[str], message: str, timed_out: bool) -> Kind:
    """One of the five kinds. Exact class names only: `MyAssertionError` is `error`, not `assertion`."""
    if timed_out:
        return "timeout"
    exc = exc_type or ""
    if phase == "collect":
        return "import" if exc in IMPORT_ERRORS else "collection"
    if exc == "AssertionError":
        return "assertion"
    return "error"


# --------------------------------------------------------------------------- truncation (3.5)

_RI_LO, _RI_HI = 0x1F1E6, 0x1F1FF


def _extends(ch: str) -> bool:
    """Marks and joiners that attach to the character before them."""
    return unicodedata.category(ch) in ("Mn", "Me", "Mc") or ch in ("‍", "️")


def _legal_cut(text: str, i: int) -> bool:
    """True when cutting `text` before index i splits no grapheme (mark, ZWJ, CRLF, regional-indicator pair)."""
    if _extends(text[i]) or text[i - 1] == "‍":
        return False
    if text[i - 1] == "\r" and text[i] == "\n":
        return False
    ri = lambda c: _RI_LO <= ord(c) <= _RI_HI
    if ri(text[i]) and ri(text[i - 1]):
        run = 0
        while i - 1 - run >= 0 and ri(text[i - 1 - run]):
            run += 1
        if run % 2 == 1:
            return False
    return True


def truncate_message(text: str, limit: int = MESSAGE_LIMIT) -> Tuple[str, bool]:
    """Keep at most `limit` characters, cut on a grapheme boundary; the entry itself is never dropped."""
    if len(text) <= limit:
        return text, False
    for i in range(limit, 0, -1):
        if _legal_cut(text, i):
            return text[:i], True
    return text[:limit], True


truncate = truncate_message  # the name the harness contract uses (failures.truncate)


# --------------------------------------------------------------------------- missing commands (12.5)

_CLICK_NO_SUCH = re.compile(r"No such command '([^']*)'")
_ARGPARSE_CHOICE = re.compile(r"invalid choice: '([^']*)'")
_ARGPARSE_UNRECOGNIZED = re.compile(r"unrecognized arguments:\s*(\S+)")
_WORD = r"[A-Za-z][\w-]*"
# Words that name a message's own vocabulary, not a program.
_NOT_PROGRAMS = {"Usage", "usage", "Error", "error", "Try", "argument", "unrecognized", "No", "Traceback",
                 "Exception", "for", "help", "to", "the", "from", "choose", "is", "a", "an", "not"}


def _prefix(text: str, programs: Sequence[str]) -> Optional[Tuple[str, List[str]]]:
    """The program and the command words named just before a marker: the run with the most words wins."""
    prog_re = ("(?:" + "|".join(re.escape(p) for p in programs) + ")") if programs else r"[A-Za-z][\w.-]*"
    best: Optional[Tuple[str, List[str]]] = None
    for m in re.finditer(rf"(?<![\w.-])({prog_re})((?:[ \t]+{_WORD})*)", text):
        prog = m.group(1)
        if not programs and prog in _NOT_PROGRAMS:
            continue
        if m.start() > 0 and text[m.start() - 1] in "'\"":  # quoted mention, such as a `Try 'mtr ... --help'` hint
            continue
        words = m.group(2).split()
        if best is None or len(words) >= len(best[1]):
            best = (prog, words)
    return best


def missing_command(text: str, programs: Sequence[str] = ()) -> Optional[str]:
    """The command path an invocation failed on, e.g. "mtr thesis new", or None.

    Matches click's `No such command 'x'`, argparse's `invalid choice: 'x'` and `unrecognized arguments: x`.
    The path is the program and command words named before the marker, plus the unknown name. When `programs`
    is given, only those program names start a path.
    """
    if not text:
        return None
    m = _CLICK_NO_SUCH.search(text) or _ARGPARSE_CHOICE.search(text) or _ARGPARSE_UNRECOGNIZED.search(text)
    if m is None:
        return None
    name = m.group(1)
    found = _prefix(text[:m.start()], programs)
    if found is None:
        return None
    prog, words = found
    if words and words[-1] == name:  # the invocation echoes the unknown name before the marker
        words = words[:-1]
    return " ".join([prog, *words, name])


# --------------------------------------------------------------------------- readers

def _split_exc(text: str) -> Tuple[Optional[str], str]:
    """`AssertionError: boom` -> ("AssertionError", "boom"); no exception name -> (None, text)."""
    m = re.match(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt))(?::\s?(.*))?$", text)
    if m is None:
        return None, text
    return m.group(1).split(".")[-1], (m.group(2) or "")


def _failure(node: Optional[str], file: Optional[str], exc: Optional[str], phase: Optional[str], message: str,
             crash_path: Optional[str], crash_line: Optional[int], detail: str, command: str) -> Failure:
    kind = classify_kind(exc, phase, message, False)
    short, truncated = truncate_message(message)
    detail = detail[:DETAIL_LIMIT]
    return Failure(node=node, file=file, kind=kind, message=short, truncated=truncated, command=command,
                   exc_type=exc, phase=phase, crash_path=crash_path, crash_line=crash_line, detail=detail,
                   missing_command=missing_command(detail or message, ()))


def read_report(path, command: str) -> List[Failure]:
    """The failures the plugin wrote to `$CK_GATE_REPORT`: one `kind: report` JSON line per failed report."""
    report = Path(path)
    if not report.is_file():
        return []
    out: List[Failure] = []
    for raw in report.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("kind") != "report":
            continue
        node = obj.get("node") or None
        if node and "::" in node:
            file, node_out = node.split("::", 1)[0], node
        else:  # collection failures name the file itself
            file, node_out = node, None
        line = obj.get("crash_line")
        out.append(_failure(
            node=node_out, file=file, exc=obj.get("exc_type") or None, phase=obj.get("phase") or None,
            message=str(obj.get("message") or ""), crash_path=obj.get("crash_path") or None,
            crash_line=line if isinstance(line, int) else None, detail=str(obj.get("detail") or ""),
            command=command))
    return out


def read_collected(path) -> List[str]:
    """The node IDs the plugin recorded under `CK_GATE_COLLECT`, in the order pytest collected them."""
    report = Path(path)
    if not report.is_file():
        return []
    nodes: List[str] = []
    for raw in report.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(raw) if raw.strip() else None
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("kind") == "collect" and obj.get("node"):
            nodes.append(str(obj["node"]))
    return nodes


_SUMMARY = re.compile(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$")
_COLLECTING = re.compile(r"^ERROR collecting (\S+)")
_BLOCK = re.compile(r"^_+ ERROR collecting (.+?) _+$")
_E_LINE = re.compile(r"^E\s+(.*)$")


def _collect_blocks(lines: List[str]) -> Dict[str, str]:
    """File -> the first `E` line of its `ERROR collecting` traceback block (the exception it raised)."""
    found: Dict[str, str] = {}
    current: Optional[str] = None
    for line in lines:
        block = _BLOCK.match(line)
        if block:
            current = block.group(1).strip()
            continue
        if current is None:
            continue
        if line.startswith("=") or (line.startswith("_") and not block):
            current = None
            continue
        e = _E_LINE.match(line)
        if e and current not in found:
            found[current] = e.group(1).strip()
    return found


def parse_short_summary(output: str, command: str) -> List[Failure]:
    """Failures from pytest's short summary: `FAILED node - msg`, `ERROR node - msg`, `ERROR collecting file`."""
    lines = output.splitlines()
    blocks = _collect_blocks(lines)
    out: List[Failure] = []
    seen = set()
    for line in lines:
        target: Optional[str] = None
        rest = ""
        phase: Optional[str] = None
        status = _SUMMARY.match(line)
        collecting = _COLLECTING.match(line)
        if status:
            target, rest = status.group(2), status.group(3) or ""
            if "::" not in target:  # a file that failed to collect (pytest 8 summary form)
                phase = "collect"
            elif status.group(1) == "FAILED":
                phase = "call"
        elif collecting:
            target, phase = collecting.group(1), "collect"
        else:
            continue
        if target in seen:
            continue
        seen.add(target)
        if "::" in target:
            file, node = target.split("::", 1)[0], target
        else:
            file, node = target, None
        exc, message = _split_exc(rest)
        if exc is None and rest.startswith("assert"):
            exc = "AssertionError"
        if node is None and not message:
            message = blocks.get(file, "")
            if message and exc is None:
                exc, message = _split_exc(message)
        out.append(_failure(node=node, file=file, exc=exc, phase=phase, message=message, crash_path=None,
                            crash_line=None, detail=line, command=command))
    return out


_FAIL_MARK = re.compile(r"fail|error|traceback|✗|✘|×", re.IGNORECASE)


def failing_files(output: str, known_files: Iterable[str], command: str) -> List[Failure]:
    """File-level failures for a non-pytest runner: failure lines that name a test file the graph knows."""
    known = sorted(set(known_files))
    out: List[Failure] = []
    named = set()
    for line in output.splitlines():
        if not _FAIL_MARK.search(line):
            continue
        for path in known:
            if path in named:
                continue
            pat = rf"(?<![\w./-]){re.escape(path)}(?![\w./-])"
            if re.search(pat, line):
                named.add(path)
                out.append(_failure(node=None, file=path, exc=None, phase=None, message=line.strip(),
                                    crash_path=None, crash_line=None, detail=line, command=command))
    return out
