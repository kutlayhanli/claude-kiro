"""D3 failure model: the `Failure` record, its classification, truncation and the readers the gate uses.

Pure seams (`classify_kind`, `truncate_message`, `missing_command`) take plain values so the properties can call
them directly. The readers turn a gate command's output into `Failure` entries: the plugin's JSON-lines report
(`read_report`), pytest's short summary (`parse_short_summary`) or, for other runners, the known test file names
named in the output (`failing_files`).
"""

import ast
import builtins
import importlib.util
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Literal, Mapping, Optional, Sequence, Set, Tuple

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
    if name in words:  # the invocation echoes the unknown name (click: `mtr thesis new: ... 'thesis'`) before the marker
        return " ".join([prog, *words])
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


# --------------------------------------------------------------------------- right-reason classification (5.2-5.7)

DATA_ERRORS = ("ValidationError", "JSONDecodeError", "YAMLError", "UnicodeDecodeError")
IMPORT_FAMILY = ("ImportError", "ModuleNotFoundError", "AttributeError")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_QUOTE_MAP = str.maketrans({"‘": "'", "’": "'", "“": "'", "”": "'"})
_NAMED_MODULE = (re.compile(r"No module named '([^']+)'"),
                 re.compile(r"cannot import name '[^']*' from '([^']+)'"),
                 re.compile(r"module '([^']+)' has no attribute"))
_NO_SUCH_OPTION = re.compile(r"No such option: (\S+)")
_USAGE = re.compile(r"(?i)\busage:")
_PYTEST_E = re.compile(r"(?m)^[ \t]*E(?=[ \t]{3})[ \t]*")
# An exception line of a traceback (also a chained one, `E   NotImplementedError: x`), not a message that quotes it
_NOT_IMPLEMENTED = re.compile(r"(?m)^\s*(?:E\s+)?(?:[\w.]+\.)?NotImplementedError\b")
# Builtins that run the code they are given: a use of one reaches project code.
_CODE_BUILTINS = frozenset({"__import__", "eval", "exec", "compile"})
_BUILTIN_NAMES = frozenset(dir(builtins))
# Modules whose every use starts a program or runs project code (the CLI through `subprocess`, a module by name).
_EXEC_MODULES = frozenset({"subprocess", "runpy", "importlib", "pty", "multiprocessing"})
# `os` functions that start a program: `os.system`, `os.popen`, `os.execv`, `os.spawnl`, `os.posix_spawn`, ...
_OS_EXEC = re.compile(r"^(system|popen|exec[lv]p?e?|spawn[lv]p?e?|posix_spawnp?)$")
# Attributes every module has; `__file__` is the test file's own path, not code.
_MODULE_ATTRS = frozenset({"__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__",
                           "__builtins__", "__annotations__"})


@dataclass(frozen=True)
class ProjectContext:
    """What the classifier needs to know about the project; the gate gathers it."""
    project_modules: FrozenSet[str]   # top-level packages/modules of the project (src/ layout aware)
    owned_paths: FrozenSet[str]       # files owned by impl tasks, existing or not
    programs: FrozenSet[str]          # the project's CLI programs
    test_paths: FrozenSet[str]        # this task's test files and fixtures
    test_sources: Mapping[str, str]   # source text of this task's test files, keyed by repo-relative path


@dataclass(frozen=True)
class TestResult:
    node: str
    passed: bool
    failure: Optional[Failure]


@dataclass(frozen=True)
class RightReasonVerdict:
    blocking: Tuple[Failure, ...]     # class "bad" (5.3)
    unclear: Tuple[Failure, ...]      # reported, not blocking (5.4)
    already_passing: Tuple[str, ...]  # passing nodes (5.5)
    require_pass: bool                # all verified tasks Done: today's rule (5.6)


def _clean(text: Optional[str]) -> str:
    """ANSI colour off, CRLF and CR to LF, curly quotes to straight ones."""
    text = _ANSI.sub("", text or "")
    return text.replace("\r\n", "\n").replace("\r", "\n").translate(_QUOTE_MAP)


def _flat(text: str) -> str:
    """One line: a message wrapped by a tool still matches its markers."""
    return re.sub(r"\s+", " ", text)


def _module_candidates(mod: str) -> Tuple[str, str]:
    base = mod.replace(".", "/")
    return base + ".py", base + "/__init__.py"


def _path_matches(mod: str, paths: Iterable[str]) -> bool:
    cands = _module_candidates(mod)
    return any(p == c or p.endswith("/" + c) for p in paths for c in cands)


def _project_module(mod: str, ctx: ProjectContext) -> bool:
    return mod.split(".")[0] in ctx.project_modules or _path_matches(mod, ctx.owned_paths)


def _test_module(mod: str, ctx: ProjectContext) -> bool:
    return _path_matches(mod, ctx.test_paths)


def _named_module(text: str) -> Optional[str]:
    for pat in _NAMED_MODULE:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


def _test_side(path: str, ctx: ProjectContext) -> bool:
    """A test helper, conftest or fixture file: not project code. A project's own source is never test-side."""
    parts = path.split("/")
    if path in ctx.test_paths or parts[-1].startswith("conftest"):
        return True
    if path in ctx.owned_paths or parts[0] == "src":
        return False
    return "tests" in parts[:-1] or "fixtures" in parts[:-1]


def _cli_not_built(f: Failure, ctx: ProjectContext) -> bool:
    """A missing command or option of one of the project's programs, printed by the CLI itself (rule 3)."""
    if not ctx.programs:
        return False
    raw = _clean(f.detail) + "\n" + _clean(f.message)
    # an assertion's own message can quote a CLI error; only a CLI's usage banner makes it the CLI's output
    if f.exc_type == "AssertionError" and not _USAGE.search(raw):
        return False
    flat = _flat(_PYTEST_E.sub("", raw))  # pytest's "E   " prefix on each traceback line
    progs = tuple(sorted(ctx.programs))
    if missing_command(flat, progs) is not None:
        return True
    m = _NO_SUCH_OPTION.search(flat)
    return m is not None and _prefix(flat[:m.start()], progs) is not None


# --------------------------------------------------------------------------- reaching project code (5.7)

class _Scope:
    """Bindings of one scope: name -> [(kind, payload)]. Kinds: expr (value), import (module), param, def, def."""

    def __init__(self, parent: Optional["_Scope"]):
        self.parent = parent
        self.names: Dict[str, List[Tuple[str, object]]] = {}

    def add(self, name: str, kind: str, payload: object = None) -> None:
        self.names.setdefault(name, []).append((kind, payload))


def _store_names(target: ast.AST) -> List[str]:
    return [n.id for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]


def _all_args(args: ast.arguments) -> List[ast.arg]:
    out = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    if args.vararg:
        out.append(args.vararg)
    if args.kwarg:
        out.append(args.kwarg)
    return out


def _collect(nodes: Sequence[ast.AST], scope: _Scope) -> None:
    """Record the bindings a body makes in `scope`; nested function and class bodies are their own scopes."""
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope.add(node.name, "def", node)
            continue
        if isinstance(node, ast.ClassDef):
            scope.add(node.name, "expr", node)
            continue
        if isinstance(node, ast.Import):
            for a in node.names:
                scope.add(a.asname or a.name.split(".")[0], "import", a.name)
        elif isinstance(node, ast.ImportFrom):
            mod = "." * node.level + (node.module or "")
            for a in node.names:  # `from X import *` binds "*"; names it supplies are unbound here (see _name_reaches)
                exec_name = mod == "os" and bool(_OS_EXEC.match(a.name))
                scope.add(a.asname or a.name, "import", "os." + a.name if exec_name else mod)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for n in _store_names(t):
                    scope.add(n, "expr", node.value)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)) and node.value is not None:
            for n in _store_names(node.target):
                scope.add(n, "expr", node.value)
        elif isinstance(node, ast.NamedExpr):
            for n in _store_names(node.target):
                scope.add(n, "expr", node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for n in _store_names(node.target):
                scope.add(n, "expr", node.iter)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    for n in _store_names(item.optional_vars):
                        scope.add(n, "expr", item.context_expr)
        elif isinstance(node, ast.comprehension):
            for n in _store_names(node.target):
                scope.add(n, "expr", node.iter)
        elif isinstance(node, ast.ExceptHandler) and node.name and node.type is not None:
            scope.add(node.name, "expr", node.type)
        elif isinstance(node, ast.Lambda):  # a lambda's parameters are its own locals: values, not fixtures
            for a in _all_args(node.args):
                scope.add(a.arg, "expr", ast.Constant(value=None))
        stack.extend(ast.iter_child_nodes(node))


def _reaches(expr: ast.AST, scope: _Scope, visiting: Set[tuple], ctx: ProjectContext) -> bool:
    for node in ast.walk(expr):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if _name_reaches(node.id, scope, visiting, ctx):
                return True
        elif isinstance(node, ast.Attribute) and _OS_EXEC.match(node.attr):  # `os.system(...)`, `os.popen(...)`
            return True
    return False


def _name_reaches(name: str, scope: _Scope, visiting: Set[tuple], ctx: ProjectContext) -> bool:
    s: Optional[_Scope] = scope
    while s is not None:
        if name in s.names:
            key = ("name", id(s), name)
            if key in visiting:
                return False
            visiting.add(key)
            try:
                return any(_binding_reaches(kind, payload, s, visiting, ctx) for kind, payload in s.names[name])
            finally:
                visiting.discard(key)
        s = s.parent
    # Unbound: a builtin is not project code, but a code-running builtin is, and so is any other name, which may
    # come from a `from X import *` the parse cannot expand (every ambiguity is unclear, never bad).
    if name in _MODULE_ATTRS:
        return False
    if name in _CODE_BUILTINS:
        return True
    return name not in _BUILTIN_NAMES


def _import_reaches(mod: str, ctx: ProjectContext) -> bool:
    """Project, helper, unknown and program-running modules reach project code; the standard library does not,
    nor does a third-party package installed outside the repository (a data reader such as `yaml` or `pytest`)."""
    if mod.startswith("."):
        return True
    top = mod.split(".")[0]
    if top in ctx.project_modules or _path_matches(mod, ctx.owned_paths) or _path_matches(mod, ctx.test_paths):
        return True
    if top in _EXEC_MODULES or (mod.startswith("os.") and _OS_EXEC.match(mod[3:])):
        return True
    if top in sys.stdlib_module_names:
        return False
    try:  # only the top-level name: find_spec of a dotted name would import its parent package
        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError):
        spec = None
    origin = (spec.origin or "") if spec is not None else ""
    if origin and ("site-packages" in origin or "dist-packages" in origin):
        return False
    return True  # a repository file, a namespace or an unknown module: cannot tell what it runs


def _binding_reaches(kind: str, payload: object, s: _Scope, visiting: Set[tuple], ctx: ProjectContext) -> bool:
    if kind == "param":  # a test-function parameter is a pytest fixture, which may call project code
        return True
    if kind == "import":
        return _import_reaches(str(payload), ctx)
    key = ("binding", id(payload))
    if key in visiting:
        return False
    visiting.add(key)
    try:
        if kind == "expr":
            return _reaches(payload, s, visiting, ctx)  # type: ignore[arg-type]
        return _def_reaches(payload, s, visiting, ctx)  # type: ignore[arg-type]
    finally:
        visiting.discard(key)


def _def_reaches(node: ast.AST, s: _Scope, visiting: Set[tuple], ctx: ProjectContext) -> bool:
    """A same-file function reaches project code when its defaults or its body do."""
    args = node.args  # type: ignore[attr-defined]
    for d in list(args.defaults) + [k for k in args.kw_defaults if k is not None]:
        if _reaches(d, s, visiting, ctx):
            return True
    fscope = _Scope(s)
    for a in _all_args(args):
        fscope.add(a.arg, "param")
    _collect(node.body, fscope)  # type: ignore[attr-defined]
    return any(_reaches(stmt, fscope, visiting, ctx) for stmt in node.body)  # type: ignore[attr-defined]


def assert_reaches_project(test_source: Optional[str], line: Optional[int], ctx: ProjectContext) -> Optional[bool]:
    """Does the failing assert at `line` run project code or use a project name? None when it cannot be parsed.

    Finds the `assert` statement covering `line` and its enclosing function, then follows every name in the
    assert back through the function's bindings (assignments, `for`/`with`/comprehension targets, walrus, imports,
    same-file helpers). `False` means only literals, builtins, the standard library and file-loaded data.
    """
    if test_source is None or line is None:
        return None
    try:
        tree = ast.parse(test_source)
    except (SyntaxError, ValueError):
        return None
    target: Optional[ast.Assert] = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert) and node.lineno <= line <= (node.end_lineno or node.lineno):
            if target is None or (node.end_lineno or node.lineno) - node.lineno < \
                    (target.end_lineno or target.lineno) - target.lineno:
                target = node
    if target is None:
        return None
    func: Optional[ast.AST] = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.lineno <= target.lineno <= (node.end_lineno or node.lineno):
            if func is None or node.lineno > func.lineno:  # the innermost enclosing function
                func = node
    module = _Scope(None)
    _collect(tree.body, module)
    scope = module
    if func is not None:
        scope = _Scope(module)
        for a in _all_args(func.args):  # type: ignore[attr-defined]
            scope.add(a.arg, "param")
        _collect(func.body, scope)  # type: ignore[attr-defined]
    roots: List[ast.AST] = [target.test] + ([target.msg] if target.msg is not None else [])
    return any(_reaches(r, scope, set(), ctx) for r in roots)


# --------------------------------------------------------------------------- the classifier (5.2-5.4, 5.7)

def classify_not_built(failure: Failure, ctx: ProjectContext) -> str:
    """"not-built" (the feature is not built yet), "bad" (a wrong reason: blocks) or "unclear" (reported only).

    First match wins: NotImplementedError; an import or attribute error naming a project module or owned path
    (or a test-owned module, which is bad); a missing command or option of a program; data-loading errors, setup
    errors, an assert failing in a helper or conftest file, and SyntaxError or NameError in the test file (bad);
    an AssertionError in the test file, bad only when its assert reaches no project code (5.7); otherwise unclear.
    """
    exc = failure.exc_type
    # anywhere in the chain: a wrapping exception's text still carries the stub's NotImplementedError (rule 1)
    if exc == "NotImplementedError" or _NOT_IMPLEMENTED.search(_clean(failure.message)) \
            or _NOT_IMPLEMENTED.search(_clean(failure.detail)):
        return "not-built"
    if exc in IMPORT_FAMILY:
        mod = _named_module(_clean(failure.message)) or _named_module(_clean(failure.detail))
        if mod is not None:
            if _project_module(mod, ctx):
                return "not-built"
            if exc != "AttributeError" and _test_module(mod, ctx):
                return "bad"
            return "unclear"  # a third-party module
    if _cli_not_built(failure, ctx):
        return "not-built"
    text = _clean(failure.message) + "\n" + _clean(failure.detail)
    if exc in DATA_ERRORS:
        return "bad"
    if exc == "FileNotFoundError" and any(p and p in text for p in ctx.test_paths):
        return "bad"
    if failure.phase == "setup":
        return "bad"
    if exc == "AssertionError" and failure.crash_path is not None and failure.crash_path != failure.file \
            and _test_side(failure.crash_path, ctx):
        return "bad"
    in_test_file = failure.file is not None and failure.crash_path == failure.file
    if exc in ("SyntaxError", "NameError") and in_test_file:
        return "bad"
    if exc == "AssertionError" and in_test_file:
        src = ctx.test_sources.get(failure.file or "")
        reaches = assert_reaches_project(src, failure.crash_line, ctx)
        return "bad" if reaches is False else "unclear"
    return "unclear"


def right_reason_verdict(results: Sequence[TestResult], all_verified_done: bool,
                         ctx: Optional[ProjectContext] = None) -> RightReasonVerdict:
    """The right-reason verdict of a test-track gate. With every verified task Done only a pass is required (5.6).

    Otherwise the failing results are classified with `classify_not_built` (`ctx` is needed for that; without it
    a failure is unclear and does not block). Passing nodes are listed as already passing (5.5).
    """
    if all_verified_done:
        return RightReasonVerdict(blocking=(), unclear=(), already_passing=(), require_pass=True)
    if ctx is None:  # without the project context no failure can be classified: refuse rather than fail open
        raise ValueError("right_reason_verdict needs a ProjectContext unless all verified tasks are Done")
    blocking: List[Failure] = []
    unclear: List[Failure] = []
    passing: List[str] = []
    for r in results:
        if r.passed:
            passing.append(r.node)
            continue
        if r.failure is None:
            continue
        cls = classify_not_built(r.failure, ctx)
        if cls == "bad":
            blocking.append(r.failure)
        elif cls == "unclear":
            unclear.append(r.failure)
    return RightReasonVerdict(blocking=tuple(blocking), unclear=tuple(unclear),
                              already_passing=tuple(passing), require_pass=False)
