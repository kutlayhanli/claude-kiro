"""The spec graph: the tasks, TCs, test files and source files of one spec, and how they connect.

`build_graph(project_dir, spec_dir)` reads the spec's tasks.md, design.md and test-plan.md and the files they
name, and returns a `SpecGraph`. Nothing is cached on disk. Each file's scan is memoised in-process under the
file's content hash, so an edit that keeps size and mtime is still seen. A malformed source adds a
`GraphProblem` and the rest of the graph is still built.

Scan only: no project code is imported and no network is used.

Scope of `imports` edges: an edge is added only when both ends are non-glob Files-owned paths of the spec and
the importing file was scanned. An import of a repo file no task owns adds no edge. A file that exists but
cannot be read (a directory in place of a file, a permission error) adds a `GraphProblem`; a missing file adds
nothing.
"""

import ast
import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .hooks._shared.spec_parser import SpecTask, parse_tasks_text
from .lint import _imports

SCHEMA = "ck-graph/1"
NODE_KINDS = ("task", "tc", "test_file", "source_file", "command")
EDGE_KINDS = ("depends", "verifies", "owns", "provides", "invokes", "imports")


@dataclass(frozen=True)
class Node:
    id: str  # "task:12", "tc:TC-12", "file:tests/test_x.py", "cmd:mtr thesis new"
    kind: str
    label: str


@dataclass(frozen=True)
class Edge:
    kind: str
    src: str
    dst: str
    source: str  # "tasks.md", "Verify -k", "Verifies", "scan"


@dataclass(frozen=True)
class GraphProblem:
    source: str  # a file path, or "specs/x/tasks.md#Task 12"
    problem: str


@dataclass(frozen=True)
class MissingEdge:
    """A test file of a test task verifies `lacking` and invokes `command`, which `provider` provides, while `lacking`
    does not depend on `provider` (12.1)."""

    test_file: str  # "file:tests/test_x.py"
    command: str  # "mtr thesis new"
    provider: str  # "task:5"
    lacking: str  # "task:2"


@dataclass(frozen=True)
class UnknownCommand:
    """A command a test invokes that no task provides and no non-test source defines (12.3)."""

    command: str  # "mtr bogus"
    test_files: Tuple[str, ...]  # ("file:tests/test_x.py",)


@dataclass
class SpecGraph:
    spec: str
    nodes: List[Node]
    edges: List[Edge]
    problems: List[GraphProblem]
    programs: List[str] = field(default_factory=list)
    # test file id -> the task ids its test task verifies. Not part of the ck-graph/1 document; it feeds
    # `missing_command_edges`.
    test_verifies: Dict[str, List[str]] = field(default_factory=dict)

    def to_json(self) -> dict:
        nodes = sorted(self.nodes, key=lambda n: (n.kind, n.id))
        edges = sorted(self.edges, key=lambda e: (e.kind, e.src, e.dst, e.source))
        return {
            "schema": SCHEMA,
            "spec": self.spec,
            "nodes": [{"id": n.id, "kind": n.kind, "label": n.label} for n in nodes],
            "edges": [{"kind": e.kind, "src": e.src, "dst": e.dst, "source": e.source} for e in edges],
            "problems": [{"source": p.source, "problem": p.problem} for p in self.problems],
            "programs": list(self.programs),
        }


# --------------------------------------------------------------------------- paths and globs


def _norm_entry(raw: str) -> str:
    """A Files entry as written -> the path or glob it names: backticks, a leading "./" and " - reason" are dropped,
    and a trailing "/" means everything under that directory ("dir/**")."""
    s = raw.strip()
    if s.startswith("`"):
        end = s.find("`", 1)
        s = s[1:end] if end > 0 else s[1:]
    else:
        s = s.split(" - ")[0].strip()
    if s.startswith("./"):
        s = s[2:]
    if s.endswith("/"):
        s += "**"
    return s


def _is_glob(path: str) -> bool:
    return any(c in path for c in "*?")


def _glob_tokens(pattern: str) -> List[Tuple[str, str]]:
    """Tokens: ("c", char) literal, ("q", "") any one non-"/" char, ("s", "") any run without "/", ("g", "") any run."""
    tokens: List[Tuple[str, str]] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            tokens.append(("g", ""))
            i += 2
        elif pattern[i] == "*":
            tokens.append(("s", ""))
            i += 1
        elif pattern[i] == "?":
            tokens.append(("q", ""))
            i += 1
        else:
            tokens.append(("c", pattern[i]))
            i += 1
    return tokens


def _closure(tokens: List[Tuple[str, str]], i: int) -> Set[int]:
    """States reachable from i without consuming a character (skipping star-like tokens)."""
    states = {i}
    stack = [i]
    while stack:
        s = stack.pop()
        if s < len(tokens) and tokens[s][0] in ("s", "g") and s + 1 not in states:
            states.add(s + 1)
            stack.append(s + 1)
    return states


def _step(tokens: List[Tuple[str, str]], i: int, char: str) -> Set[int]:
    """States after consuming `char` in state i."""
    if i >= len(tokens):
        return set()
    kind, value = tokens[i]
    if kind == "c":
        return {i + 1} if char == value else set()
    if kind == "q":
        return {i + 1} if char != "/" else set()
    if kind == "s":
        return {i} if char != "/" else set()
    return {i}  # "g" takes any character and stays


def _globs_intersect(a: str, b: str) -> bool:
    """True when some path matches both patterns. Characters are abstracted to the literals of both patterns, "/",
    and one character that is neither, so the search is finite and exact."""
    ta, tb = _glob_tokens(a), _glob_tokens(b)
    alphabet = {v for k, v in ta + tb if k == "c"} | {"/"}
    alphabet.add(next(chr(c) for c in range(1, 0x110000) if chr(c) not in alphabet))
    seen = {(0, 0)}
    stack = [(0, 0)]
    while stack:
        i, j = stack.pop()
        ca, cb = _closure(ta, i), _closure(tb, j)
        if len(ta) in ca and len(tb) in cb:
            return True
        for x in ca:
            for y in cb:
                for char in alphabet:
                    for nx in _step(ta, x, char):
                        for ny in _step(tb, y, char):
                            if (nx, ny) not in seen:
                                seen.add((nx, ny))
                                stack.append((nx, ny))
    return False


def paths_overlap(a: str, b: str) -> bool:
    """Whether two Files entries can name a common path. Globs (`*`, `?`, `**`) match as in a path pattern:
    `src/*.py` overlaps `src/a.py`, `src/**` overlaps `src/x/y.py`, and `src/a.py` does not overlap `src/ab.py`."""
    return _globs_intersect(_norm_entry(a), _norm_entry(b))


def write_set_overlaps(owns: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """{task: the other tasks whose Files overlap its own}. A task with no Files has no entry."""
    names = sorted(owns)
    result: Dict[str, List[str]] = {}
    for name in names:
        entries = owns[name]
        if not entries:
            continue
        overlaps = []
        for other in names:
            if other == name or not owns[other]:
                continue
            if any(paths_overlap(x, y) for x in entries for y in owns[other]):
                overlaps.append(other)
        result[name] = overlaps
    return result


write_overlaps = write_set_overlaps  # the name the harness contract uses


# --------------------------------------------------------------------------- file scans (memoised by content)


@dataclass(frozen=True)
class _Scan:
    problem: Optional[str]
    tcs: Tuple[int, ...]  # TC numbers defined by the file (`def test_tcN_`, `test('tcN_`)
    imports: Tuple[str, ...]  # repo-relative files the file imports (lint._imports candidates)


_SCANS: Dict[Tuple[str, str], _Scan] = {}
_SCAN_MEMO_LIMIT = 4096


def _scan_uncached(rel: str, data: bytes) -> _Scan:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as err:
        return _Scan(f"not UTF-8 text (byte {err.start}): {err.reason}", (), ())
    if rel.endswith(".py"):
        try:
            ast.parse(text, filename=rel)
        except (SyntaxError, ValueError) as err:
            line = getattr(err, "lineno", None)
            return _Scan(f"SyntaxError line {line}: {getattr(err, 'msg', err)}", (), ())
        tcs = tuple(int(n) for n in re.findall(r"\bdef\s+test_tc(\d+)_", text))
        imports = tuple(dict.fromkeys(path for path, _ in _imports(text, rel)))
        return _Scan(None, tcs, imports)
    tcs = tuple(int(n) for n in re.findall(r"\btest\(\s*['\"`]tc(\d+)_", text))
    return _Scan(None, tcs, ())


def _scan(rel: str, data: bytes) -> _Scan:
    key = (rel, hashlib.sha256(data).hexdigest())
    hit = _SCANS.get(key)
    if hit is None:
        if len(_SCANS) >= _SCAN_MEMO_LIMIT:
            _SCANS.clear()
        hit = _scan_uncached(rel, data)
        _SCANS[key] = hit
    return hit


def _read(path: Path) -> Tuple[Optional[bytes], Optional[str]]:
    """(bytes, None) when the file is read; (None, None) when it does not exist (silent: a task may not have
    written its file yet); (None, problem) when it exists but cannot be read (11.8: never silent)."""
    try:
        if not path.exists() and not path.is_symlink():
            return None, None
        if not path.is_file():
            return None, "unreadable: exists but is not a regular file"
        return path.read_bytes(), None
    except OSError as err:
        return None, f"unreadable: {err.strerror or err}"


def _rel(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


# --------------------------------------------------------------------------- spec documents


def _cells(line: str) -> List[str]:
    return [c.strip().strip("`").strip() for c in line.strip().strip("|").split("|")]


def _parse_tasks(text: str, source: str, problems: List[GraphProblem]) -> Dict[str, SpecTask]:
    """Task blocks by number. A block that does not parse, or a duplicate number, adds a problem and is left out."""
    found: Dict[str, List[SpecTask]] = {}
    for chunk in re.split(r"(?m)^(?=#{2,3} )", text):
        if not re.match(r"### Task\b", chunk):
            continue
        header = chunk.split("\n", 1)[0]
        match = re.match(r"### Task ?(\d+): ", chunk)
        if not match:
            problems.append(GraphProblem(f"{source}#{header[4:].strip()}", "task header has no task number"))
            continue
        num = match.group(1)
        dependencies = re.search(r"^\*\*Dependencies:\*\*\s*(.*)$", chunk, re.MULTILINE)
        value = dependencies.group(1).strip() if dependencies else ""
        if value and not value.lower().startswith(("none", "n/a", "-")) and not re.search(r"\d", value):
            problems.append(GraphProblem(f"{source}#Task {num}", f"Dependencies names no task number: {value!r}"))
            continue
        parsed = parse_tasks_text(chunk)
        for item in parsed:
            # A Provides value without backticks is still a command path (`**Provides:** mtr thesis new`).
            item.provides = _provides_values(chunk)
        found.setdefault(num, []).extend(parsed)
    tasks: Dict[str, SpecTask] = {}
    for num, items in found.items():
        if len(items) > 1:
            problems.append(GraphProblem(f"{source}#Task {num}", f"task number {num} is defined {len(items)} times"))
            continue
        if items:
            tasks[num] = items[0]
    return tasks


def _check_design(text: str, source: str, problems: List[GraphProblem]) -> None:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("|") and _cells(line)[:1] == ["Command"]:
            rows = 0
            for row in lines[i + 1:]:
                if not row.strip().startswith("|"):
                    break
                if "`" in row:
                    rows += 1
            if rows == 0:
                problems.append(GraphProblem(source, "CLI table (| Command | File |) has no command rows"))


def _plan_rows(text: str) -> List[Tuple[int, Tuple[str, ...], str]]:
    """(TC number, the other cells of the row, description) for each test-plan row whose first cell is TC-N."""
    rows = []
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = _cells(line)
        match = re.match(r"\W*TC-(\d+)\b", cells[0]) if cells else None
        if match:
            rest = tuple(c for c in cells[1:] if c)
            desc = cells[3] if len(cells) > 3 else ""
            rows.append((int(match.group(1)), rest, desc))
    return rows


def _verify_tcs(commands: List[str]) -> List[int]:
    """TC numbers of `tcN_` terms in each command's `-k` expression that are not preceded by `not`.

    `-k` may be attached to its value (`-ktc3_`); pytest keeps the last `-k`, so only that one selects.
    """
    out: List[int] = []
    for command in commands:
        matches = list(re.finditer(r"(?<!\S)-k(?:\s+|=)?(?:\"([^\"]*)\"|'([^']*)'|(\S+))", command))
        if not matches:
            continue
        expr = next(g for g in matches[-1].groups() if g is not None)
        positive = [int(term.group(2)) for term in re.finditer(r"(\bnot\s+)?\btc(\d+)_", expr)
                    if term.group(1) is None]
        # `tc3_ and tc4_`: no one test name carries both TC ids, so the selector names no TC (1.1 red team).
        if re.search(r"\band\b", expr) and len(set(positive)) > 1:
            continue
        out.extend(positive)
    return out


# --------------------------------------------------------------------------- commands (11.1 to 11.4, 12.1, 12.3)


def _provides_values(chunk: str) -> List[str]:
    """The command paths of a task block's `**Provides:**` field: backticked spans if it has any, else the
    comma-separated plain values."""
    match = re.search(r"^\*\*Provides:\*\*\s*(.*)$", chunk, re.MULTILINE)
    if not match:
        return []
    spans = re.findall(r"`([^`]+)`", match.group(1))
    if spans:
        return [" ".join(s.split()) for s in spans if s.strip()]
    return [" ".join(p.split()) for p in match.group(1).split(",") if p.strip()]


def _cli_rows(text: str) -> List[Tuple[str, str]]:
    """(command path, file) for each row of the design's `| Command | File |` table."""
    rows: List[Tuple[str, str]] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("|") and _cells(line)[:1] == ["Command"]:
            for row in lines[i + 1:]:
                if not row.strip().startswith("|"):
                    break
                cells = _cells(row)
                if len(cells) >= 2 and cells[0] and not cells[0].startswith("-") and cells[1]:
                    rows.append((" ".join(cells[0].split()), _norm_entry(cells[1])))
    return rows


def _programs(project_dir: Path, cli_rows: List[Tuple[str, str]], problems: List[GraphProblem]) -> List[str]:
    """Known programs: the first word of each design CLI row, the `[project.scripts]` keys of pyproject.toml and the
    `bin` keys of package.json."""
    found = {cmd.split()[0] for cmd, _ in cli_rows if cmd.split()}
    pyproject, problem = _read(project_dir / "pyproject.toml")
    if problem:
        problems.append(GraphProblem("pyproject.toml", problem))
    elif pyproject is not None:
        try:
            data = tomllib.loads(pyproject.decode("utf-8"))
            found |= set(((data.get("project") or {}).get("scripts") or {}).keys())
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
            problems.append(GraphProblem("pyproject.toml", f"not valid TOML: {err}"))
    package, problem = _read(project_dir / "package.json")
    if problem:
        problems.append(GraphProblem("package.json", problem))
    elif package is not None:
        try:
            data = json.loads(package.decode("utf-8"))
            bins = data.get("bin") if isinstance(data, dict) else None
            if isinstance(bins, dict):
                found |= set(bins.keys())
        except (UnicodeDecodeError, ValueError) as err:
            problems.append(GraphProblem("package.json", f"not valid JSON: {err}"))
    return sorted(p for p in found if p)


def _provides_problem(value: str, programs: Set[str]) -> Optional[str]:
    """Why a **Provides:** value is malformed (11.8), or None when it is a command path (program and command).

    The program need not be one discovery knows: **Provides:** wins over inference (11.3), and an unreadable
    design.md (whose CLI table names the programs) must not turn every Provides field into a problem (TC-108)."""
    del programs  # kept in the signature for callers; an unknown program is not a malformed value
    words = value.split()
    if not words:
        return "Provides names no command"
    if len(words) < 2:
        return f"Provides names no command path: {value!r}"
    return None


def _add_command_node(nodes: Dict[str, Node], cmd: str) -> None:
    nodes.setdefault(f"cmd:{cmd}", Node(f"cmd:{cmd}", "command", cmd))


def invoked_commands(source: str, commands: Iterable[str], programs: Iterable[str], lang: str = "py") -> List[str]:
    """Command paths (program included, e.g. "mtr thesis new") that a test source invokes. A word sequence starting
    with a known program is a program form (`["mtr", "thesis", "new"]`, `"mtr thesis new --x"`) and goes to the
    longest known command path it starts with; an unknown path under the program is returned as itself. A list
    without the program (`CliRunner().invoke(cli, ["thesis", "new"])`) goes to the longest known command path it
    starts with, never to a group (a path that prefixes a longer known path). Leading options are skipped. Pure:
    depends only on the source text, the command paths and the programs. Python is scanned with the AST (string
    constants and list/tuple literals), anything else with regexes."""
    known = sorted({" ".join(c.split()) for c in commands if len(c.split()) >= 2})
    progs = {p for p in programs if p}
    groups = {c for c in known if any(o != c and o.startswith(c + " ") for o in known)}
    found: Set[str] = set()

    def skip_options(words: List[str]) -> List[str]:
        i = 0
        while i < len(words) and words[i].startswith("-"):
            i += 1
        return words[i:]

    def program_match(prog: str, rest: List[str]) -> None:
        words = skip_options(rest)
        best: Optional[str] = None
        for c in known:
            cw = c.split()
            if cw[0] == prog and words[:len(cw) - 1] == cw[1:] and (best is None or len(cw) > len(best.split())):
                best = c
        if best is not None:
            found.add(best)
            return
        path: List[str] = []
        for w in words:
            if w.startswith("-"):
                break
            path.append(w)
        if path:
            found.add(f"{prog} {' '.join(path)}")

    def bare_match(items: List[str]) -> None:
        words = skip_options(items)
        best: Optional[str] = None
        for c in known:
            if c in groups:
                continue
            cw = c.split()
            if words[:len(cw) - 1] == cw[1:] and (best is None or len(cw) > len(best.split())):
                best = c
        if best is not None:
            found.add(best)

    def text_match(text: str) -> None:
        words = text.split()
        if len(words) >= 2 and words[0] in progs:
            program_match(words[0], words[1:])

    def list_match(items: List[str]) -> None:
        if not items:
            return
        if items[0] in progs:
            program_match(items[0], items[1:])
        else:
            bare_match(items)

    if lang == "py":
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text_match(node.value)
            elif (isinstance(node, (ast.List, ast.Tuple)) and node.elts
                  and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts)):
                list_match([e.value for e in node.elts])
    else:
        for m in re.finditer(r"""(['"`])((?:\\.|(?!\1).)*)\1""", source):
            text_match(m.group(2))
        for m in re.finditer(r"\[([^\[\]]*)\]", source):
            items = [s for _, s in re.findall(r"""(['"])((?:\\.|(?!\1).)*)\1""", m.group(1))]
            list_match(items)
    return sorted(found)


def _transitive(start: str, deps: Dict[str, Set[str]]) -> Set[str]:
    seen: Set[str] = set()
    stack = list(deps.get(start, ()))
    while stack:
        node = stack.pop()
        if node not in seen:
            seen.add(node)
            stack.extend(deps.get(node, ()))
    return seen


def missing_command_edges(graph: SpecGraph) -> List[MissingEdge]:
    """The (test file, command, provider, lacking task) tuples where a test task's test file verifies task V and
    invokes a command that task P provides, P is not V, and P is not among V's transitive dependencies (12.1)."""
    deps: Dict[str, Set[str]] = {}
    providers: Dict[str, Set[str]] = {}
    invokes: Dict[str, Set[str]] = {}
    for e in graph.edges:
        if e.kind == "depends":
            deps.setdefault(e.src, set()).add(e.dst)
        elif e.kind == "provides":
            providers.setdefault(e.dst, set()).add(e.src)
        elif e.kind == "invokes":
            invokes.setdefault(e.src, set()).add(e.dst)
    out: Set[MissingEdge] = set()
    for test_file, verified_tasks in graph.test_verifies.items():
        for verified in verified_tasks:
            closure = _transitive(verified, deps)
            for cmd in invokes.get(test_file, ()):
                for provider in providers.get(cmd, ()):
                    if provider != verified and provider not in closure:
                        out.add(MissingEdge(test_file, cmd, provider, verified))
    return sorted(out, key=lambda m: (m.test_file, m.command, m.provider, m.lacking))


def unknown_commands(graph: SpecGraph, source_words: Iterable[str]) -> List[UnknownCommand]:
    """Command nodes no task provides whose path (program excluded) has a word that is not a string literal of a
    non-test source file (12.3)."""
    defined = set(source_words)
    provided = {e.dst for e in graph.edges if e.kind == "provides"}
    out: List[UnknownCommand] = []
    for node in sorted(graph.nodes, key=lambda n: n.id):
        if node.kind != "command" or node.id in provided:
            continue
        if all(w in defined for w in node.label.split()[1:]):
            continue
        files = tuple(sorted({e.src for e in graph.edges if e.kind == "invokes" and e.dst == node.id}))
        out.append(UnknownCommand(node.label, files))
    return out


# --------------------------------------------------------------------------- build


def build_graph(project_dir: Path, spec_dir: Path) -> SpecGraph:
    project_dir, spec_dir = Path(project_dir), Path(spec_dir)
    spec_rel = _rel(spec_dir, project_dir)
    problems: List[GraphProblem] = []

    tasks_bytes, problem = _read(spec_dir / "tasks.md")
    tasks: Dict[str, SpecTask] = {}
    if problem:
        problems.append(GraphProblem(f"{spec_rel}/tasks.md", problem))
    elif tasks_bytes is not None:
        try:
            tasks = _parse_tasks(tasks_bytes.decode("utf-8"), f"{spec_rel}/tasks.md", problems)
        except UnicodeDecodeError as err:
            problems.append(GraphProblem(f"{spec_rel}/tasks.md", f"not UTF-8 text (byte {err.start})"))

    design_bytes, problem = _read(spec_dir / "design.md")
    cli_rows: List[Tuple[str, str]] = []
    if problem:
        problems.append(GraphProblem(f"{spec_rel}/design.md", problem))
    elif design_bytes is not None:
        try:
            design_text = design_bytes.decode("utf-8")
            _check_design(design_text, f"{spec_rel}/design.md", problems)
            cli_rows = _cli_rows(design_text)
        except UnicodeDecodeError as err:
            problems.append(GraphProblem(f"{spec_rel}/design.md", f"not UTF-8 text (byte {err.start})"))
    programs = _programs(project_dir, cli_rows, problems)
    prog_set = set(programs)

    plan: List[Tuple[int, Tuple[str, ...], str]] = []
    plan_bytes, problem = _read(spec_dir / "test-plan.md")
    if problem:
        problems.append(GraphProblem(f"{spec_rel}/test-plan.md", problem))
    elif plan_bytes is not None:
        try:
            plan = _plan_rows(plan_bytes.decode("utf-8"))
        except UnicodeDecodeError as err:
            problems.append(GraphProblem(f"{spec_rel}/test-plan.md", f"not UTF-8 text (byte {err.start})"))

    nodes: Dict[str, Node] = {}
    edges: Set[Edge] = set()
    norm_files: Dict[str, List[str]] = {}
    for num in sorted(tasks, key=int):
        task = tasks[num]
        nodes[f"task:{num}"] = Node(f"task:{num}", "task", task.title)
        norm_files[num] = [_norm_entry(f) for f in task.files]
        kind = "test_file" if task.track == "test" else "source_file"
        for path in norm_files[num]:
            fid = f"file:{path}"
            previous = nodes.get(fid)
            if previous is None or kind == "test_file":
                nodes[fid] = Node(fid, kind, path)
            edges.add(Edge("owns", f"task:{num}", fid, "tasks.md"))
        for dep in task.dependencies:
            if dep in tasks and dep != num:
                edges.add(Edge("depends", f"task:{num}", f"task:{dep}", "tasks.md"))

    test_verifies: Dict[str, List[str]] = {}
    for num in sorted(tasks, key=int):
        task = tasks[num]
        if task.track == "test":
            verified = [f"task:{v}" for v in task.verifies if v in tasks]
            for path in norm_files[num]:
                if not _is_glob(path):
                    test_verifies.setdefault(f"file:{path}", []).extend(verified)
    test_paths = {p for num, task in tasks.items() if task.track == "test" for p in norm_files[num] if not _is_glob(p)}

    scans: Dict[str, _Scan] = {}
    test_texts: Dict[str, str] = {}
    for path in sorted({p for ps in norm_files.values() for p in ps}):
        if _is_glob(path):
            continue
        data, problem = _read(project_dir / path)
        if problem:
            problems.append(GraphProblem(path, problem))
            continue
        if data is None:
            continue
        scan = _scan(path, data)
        if scan.problem:
            problems.append(GraphProblem(path, scan.problem))
        scans[path] = scan
        if path in test_paths and scan.problem is None:
            test_texts[path] = data.decode("utf-8")

    # A tc node exists for each TC some test file or test-plan row defines. Its label is the test-plan description
    # when one exists, else "TC-n". A TC that only a Verify -k names has a verifies edge and no node (dangling).
    tc_labels: Dict[int, str] = {}
    for num in sorted(tasks, key=int):
        task = tasks[num]
        if task.track == "test":
            files = set(norm_files[num])
            tcs: Set[int] = set()
            for path in files:
                if path in scans:
                    tcs.update(scans[path].tcs)
            for tc, row_files, desc in plan:
                if files & set(row_files):
                    tcs.add(tc)
                    if desc and not tc_labels.get(tc):
                        tc_labels[tc] = desc
            for tc in tcs:
                tc_labels.setdefault(tc, "")
            for verified in task.verifies:
                if verified not in tasks:
                    continue
                for tc in tcs:
                    edges.add(Edge("verifies", f"tc:TC-{tc}", f"task:{verified}", "Verifies"))
        else:
            for tc in _verify_tcs(task.verify):
                # No tc node for a TC only this -k names: a dangling verifies source (design 11.2, see Task 12).
                edges.add(Edge("verifies", f"tc:TC-{tc}", f"task:{num}", "Verify -k"))

    # Provides (11.3): a task's **Provides:** field wins over inference for that task, and a command some task
    # provides explicitly is never inferred for another task. Otherwise a design CLI row naming a file the task owns,
    # else a backticked command path with a known program in the task's title.
    explicit: Set[str] = set()
    for num in sorted(tasks, key=int):
        for value in tasks[num].provides:
            problem = _provides_problem(value, prog_set)
            if problem:
                problems.append(GraphProblem(f"{spec_rel}/tasks.md#Task {num}", problem))
                continue
            cmd = " ".join(value.split())
            explicit.add(cmd)
            _add_command_node(nodes, cmd)
            edges.add(Edge("provides", f"task:{num}", f"cmd:{cmd}", "Provides"))

    inferred: Dict[str, Set[str]] = {}
    for num in sorted(tasks, key=int):
        if tasks[num].provides:
            continue
        found: List[Tuple[str, str]] = [
            (cmd, "design.md") for cmd, path in cli_rows if any(paths_overlap(path, owned) for owned in norm_files[num])
        ]
        if not found:
            for span in re.findall(r"`([^`]+)`", tasks[num].title):
                words = span.split()
                if len(words) >= 2 and words[0] in prog_set:
                    path_words: List[str] = []
                    for w in words:
                        if w.startswith("-"):
                            break
                        path_words.append(w)
                    if len(path_words) >= 2:
                        found.append((" ".join(path_words), "tasks.md"))
        for cmd, source in found:
            if cmd in explicit:
                continue
            inferred.setdefault(cmd, set()).add(num)
            _add_command_node(nodes, cmd)
            edges.add(Edge("provides", f"task:{num}", f"cmd:{cmd}", source))
    for cmd, providers in sorted(inferred.items()):
        if len(providers) > 1:
            problems.append(GraphProblem(
                f"{spec_rel}/design.md",
                f"command {cmd!r} is inferred as provided by tasks {', '.join(sorted(providers, key=int))}",
            ))

    # Invokes (11.4): each test file's command invocations, against the design table and the provided commands.
    known_cmds = sorted({cmd for cmd, _ in cli_rows} | {e.dst[len("cmd:"):] for e in edges if e.kind == "provides"})
    for path in sorted(test_texts):
        lang = "py" if path.endswith(".py") else "js"
        for cmd in invoked_commands(test_texts[path], known_cmds, programs, lang):
            _add_command_node(nodes, cmd)
            edges.add(Edge("invokes", f"file:{path}", f"cmd:{cmd}", "scan"))

    for src_path, scan in scans.items():
        for target in scan.imports:
            if target != src_path and target in scans:
                edges.add(Edge("imports", f"file:{src_path}", f"file:{target}", "scan"))

    for tc, desc in sorted(tc_labels.items()):
        nodes[f"tc:TC-{tc}"] = Node(f"tc:TC-{tc}", "tc", desc or f"TC-{tc}")

    problems.sort(key=lambda p: (p.source, p.problem))
    return SpecGraph(
        spec=spec_dir.name,
        nodes=list(nodes.values()),
        edges=sorted(edges, key=lambda e: (e.kind, e.src, e.dst, e.source)),
        problems=problems,
        programs=programs,
        test_verifies={k: sorted(set(v)) for k, v in sorted(test_verifies.items())},
    )
