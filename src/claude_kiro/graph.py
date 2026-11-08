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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

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


@dataclass
class SpecGraph:
    spec: str
    nodes: List[Node]
    edges: List[Edge]
    problems: List[GraphProblem]
    programs: List[str] = field(default_factory=list)

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
        found.setdefault(num, []).extend(parse_tasks_text(chunk))
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
    """TC numbers of `tcN_` terms in each `-k` expression that are not preceded by `not`."""
    out: List[int] = []
    for command in commands:
        for match in re.finditer(r"-k(?:\s+|=)(?:\"([^\"]*)\"|'([^']*)'|(\S+))", command):
            expr = next(g for g in match.groups() if g is not None)
            for term in re.finditer(r"(\bnot\s+)?\btc(\d+)_", expr):
                if term.group(1) is None:
                    out.append(int(term.group(2)))
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
    if problem:
        problems.append(GraphProblem(f"{spec_rel}/design.md", problem))
    elif design_bytes is not None:
        try:
            _check_design(design_bytes.decode("utf-8"), f"{spec_rel}/design.md", problems)
        except UnicodeDecodeError as err:
            problems.append(GraphProblem(f"{spec_rel}/design.md", f"not UTF-8 text (byte {err.start})"))

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

    scans: Dict[str, _Scan] = {}
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
    )
