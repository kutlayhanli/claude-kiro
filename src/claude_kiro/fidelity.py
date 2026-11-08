"""Row fidelity (unattended-correctness Story 8): do a test's literal values match its test-plan row and the design?

`extract_test_values(source, path)` reads a Python test with `ast` and returns the enum members, numbers, dates and
strings it asserts or feeds in, each with the field, keyword or dict key it is compared with or fed as.
`parse_row(test_plan_md, tc_id)` reads one TC row's thresholds, ranges and dates, and `parse_models(design_md)` reads
the allowed enum members per field from the design's data models. `check_values(values, row, models)` is the pure
seam: it partitions the result into checked values, mismatches, unparsed items and unmatched tests. `check_task`
gathers a test-track task's test files, test-plan.md and design.md and runs the seam.

Nothing in this module writes a file (8.6).
"""

import ast
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, FrozenSet, List, Mapping, Optional, Set, Tuple

from claude_kiro.attribution import tc_of_node

THRESHOLD_WORDS = ("threshold", "min", "max", "limit", "cutoff")
ISO_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}(?:T.*)?")
NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
NUM_RE = re.compile(r"(?<![\w.])(" + NUM + r")(\s*%)?")
RANGE_RE = re.compile(
    r"\bbetween\s+(?:" + NUM + r")\s+and\s+(?:" + NUM + r")"
    r"|(?:" + NUM + r")\s*(?:to|-|–)\s*(?:" + NUM + r")"
)
QUANTITY_AFTER = re.compile(r"\s*(?:shares?|units?|lots?|contracts?|items?|orders?)\b")
DATE_RE = re.compile(
    r"\b(?P<iso>\d{4}-\d{2}-\d{2})\b"
    r"|\b(?P<mdy_mon>[A-Za-z]{3,9})\.?\s+(?P<mdy_day>\d{1,2}),?\s+(?P<mdy_year>\d{4})\b"
    r"|\b(?P<dmy_day>\d{1,2})\s+(?P<dmy_mon>[A-Za-z]{3,9})\.?,?\s+(?P<dmy_year>\d{4})\b"
)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
PY_BLOCK = re.compile(r"```python[^\n]*\n(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class TestValue:
    test: str                 # the test function's name
    kind: str                 # "enum" | "number" | "date" | "string"
    value: str
    name: Optional[str]       # the field, keyword or dict key it is compared with or fed as
    role: str                 # "asserted" | "fed"
    line: int


@dataclass(frozen=True)
class RowFacts:
    tc_id: str
    thresholds: Tuple[Tuple[Decimal, bool], ...]   # (value, percent form) for each number in the Scenario
    is_range: bool                                  # the row states one range: thresholds are its two ends
    dates: Tuple[date, ...]


@dataclass(frozen=True)
class ModelFacts:
    allowed: Dict[str, FrozenSet[str]]              # field -> allowed enum members


@dataclass(frozen=True)
class FidelityResult:
    checked: Tuple[TestValue, ...]
    mismatches: Tuple[Mapping[str, object], ...]    # {test, value, expected | allowed, line, rule}
    unparsed: Tuple[Mapping[str, object], ...]      # items for the reviewer only (8.3)
    unmatched: Tuple[str, ...]                      # tests with no TC id or no row (8.4)


# --------------------------------------------------------------------------- extraction


def _safe_dec(text: str) -> Optional[Decimal]:
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _number_of(node: ast.AST) -> Optional[Decimal]:
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return _safe_dec(str(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _number_of(node.operand)
        return -inner if inner is not None else None
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Decimal"
            and len(node.args) == 1 and not node.keywords):
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and type(arg.value) in (str, int, float):
            return _safe_dec(str(arg.value))
    return None


def _date_iso(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and ISO_TEXT.fullmatch(node.value):
        try:
            return date.fromisoformat(node.value[:10]).isoformat()
        except ValueError:
            return None
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "date"
            and len(node.args) == 3 and not node.keywords
            and all(isinstance(a, ast.Constant) and type(a.value) is int for a in node.args)):
        try:
            return date(*(a.value for a in node.args)).isoformat()
        except ValueError:
            return None
    return None


def _classify(node: ast.AST, runtime: bool) -> Optional[Tuple[str, str]]:
    """(kind, text) for a literal-ish operand, or None. Runtime expressions only when `runtime` is set."""
    num = _number_of(node)
    if num is not None:
        return ("number", str(num))
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id[:1].isupper():
        return ("enum", node.attr)
    iso = _date_iso(node)
    if iso is not None:
        return ("date", iso)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return ("string", node.value)
    if runtime and isinstance(node, (ast.BinOp, ast.Call, ast.UnaryOp, ast.Name, ast.Subscript)):
        return ("runtime", ast.unparse(node))
    return None


def _is_threshold(name: Optional[str]) -> bool:
    return name is not None and any(word in name.lower() for word in THRESHOLD_WORDS)


def _neighbor_name(operands: List[ast.AST], i: int) -> Optional[str]:
    others = [o for j, o in enumerate(operands) if j != i]
    for o in others:
        if isinstance(o, ast.Attribute):
            return o.attr
    for o in others:
        if isinstance(o, ast.Name):
            return o.id
    return None


def _compare_values(node: ast.Compare, test: str, out: List[TestValue]) -> None:
    operands = [node.left, *node.comparators]
    for i, operand in enumerate(operands):
        c = _classify(operand, runtime=True)
        if c is None:
            continue
        kind, text = c
        name = _neighbor_name(operands, i)
        if kind == "runtime":
            if not _is_threshold(name):
                continue
            kind = "number"
        out.append(TestValue(test, kind, text, name, "asserted", operand.lineno))


def _named_value(name: str, value: ast.AST, test: str, out: List[TestValue]) -> None:
    c = _classify(value, runtime=True)
    if c is None:
        return
    kind, text = c
    if kind in ("runtime", "number"):
        if not _is_threshold(name):
            return
        kind = "number"
    out.append(TestValue(test, kind, text, name, "fed", value.lineno))


def _walk(node: ast.AST, test: Optional[str], out: List[TestValue]) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        inner = node.name if node.name.startswith("test") else test
        for child in ast.iter_child_nodes(node):
            _walk(child, inner, out)
        return
    if test is not None:
        if isinstance(node, ast.Compare):
            _compare_values(node, test, out)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            _named_value(node.arg, node.value, test, out)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str) and value is not None:
                    _named_value(key.value, value, test, out)
        elif isinstance(node, ast.Call):
            for arg in node.args:
                c = _classify(arg, runtime=False)
                if c is not None and c[0] in ("enum", "date"):
                    out.append(TestValue(test, c[0], c[1], None, "fed", arg.lineno))
    for child in ast.iter_child_nodes(node):
        _walk(child, test, out)


def extract_test_values(source: str, path: str) -> List[TestValue]:
    """The values each test function asserts or feeds in. Raises SyntaxError for an unparseable source."""
    tree = ast.parse(source, filename=path)
    out: List[TestValue] = []
    _walk(tree, None, out)
    return out


def _test_names(source: str) -> List[str]:
    tree = ast.parse(source)
    return [n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")]


# --------------------------------------------------------------------------- rows and models


def _date_of_match(m: "re.Match[str]") -> Optional[date]:
    if m.group("iso"):
        try:
            return date.fromisoformat(m.group("iso"))
        except ValueError:
            return None
    month_name = m.group("mdy_mon") or m.group("dmy_mon")
    day = int(m.group("mdy_day") or m.group("dmy_day"))
    year = int(m.group("mdy_year") or m.group("dmy_year"))
    month = MONTHS.get(month_name[:3].lower())
    if month is None:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _row_facts(tc_id: str, scenario: str) -> RowFacts:
    dates: List[date] = []

    def take(m: "re.Match[str]") -> str:
        d = _date_of_match(m)
        if d is None:
            return m.group(0)
        dates.append(d)
        return " "

    stripped = DATE_RE.sub(take, scenario)
    thresholds: List[Tuple[Decimal, bool]] = []
    for m in NUM_RE.finditer(stripped):
        if QUANTITY_AFTER.match(stripped, m.end()):
            continue  # a quantity ("for an order of 123 shares"), not a threshold
        value = _safe_dec(m.group(1).replace(",", ""))
        if value is not None:
            thresholds.append((value, m.group(2) is not None))
    is_range = len(thresholds) == 2 and RANGE_RE.search(stripped) is not None
    return RowFacts(tc_id=tc_id, thresholds=tuple(thresholds), is_range=is_range,
                    dates=tuple(sorted(set(dates))))


def parse_row(test_plan_md: str, tc_id: str) -> Optional[RowFacts]:
    """The facts of the test-plan row whose ID cell is exactly `tc_id`, read from its Scenario cell; None if absent."""
    id_col, scenario_col, header_seen = 0, 3, False
    for line in test_plan_md.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        lowered = [c.lower() for c in cells]
        if not header_seen and "id" in lowered:
            header_seen = True
            id_col = lowered.index("id")
            if "scenario" in lowered:
                scenario_col = lowered.index("scenario")
            continue
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue  # the separator row
        if id_col < len(cells) and cells[id_col] == tc_id and scenario_col < len(cells):
            return _row_facts(tc_id, cells[scenario_col])
    return None


def _base_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def parse_models(design_md: str) -> ModelFacts:
    """Allowed members per field: a dataclass field annotated with an Enum class allows that class's members."""
    enums: Dict[str, Set[str]] = {}
    annotations: Dict[str, Set[str]] = {}
    for block in PY_BLOCK.findall(design_md):
        try:
            tree = ast.parse(block)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if any(_base_name(b) == "Enum" for b in node.bases):
                members = enums.setdefault(node.name, set())
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        for target in stmt.targets:
                            if isinstance(target, ast.Name):
                                members.add(target.id)
                        if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                            members.add(stmt.value.value)
            else:
                for stmt in node.body:
                    if (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                            and isinstance(stmt.annotation, ast.Name)):
                        annotations.setdefault(stmt.target.id, set()).add(stmt.annotation.id)
    allowed: Dict[str, FrozenSet[str]] = {}
    for field_name, types in annotations.items():
        members: Set[str] = set()
        for type_name in types:
            members |= enums.get(type_name, set())
        if members:
            allowed[field_name] = frozenset(members)
    return ModelFacts(allowed=allowed)


# --------------------------------------------------------------------------- the pure seam


def _dec(text: str) -> Optional[Decimal]:
    return _safe_dec(text)


def _matches(x: Decimal, end: Tuple[Decimal, bool]) -> bool:
    target, percent = end
    return x == target or (percent and x == target / 100)


def _fmt(end: Tuple[Decimal, bool]) -> str:
    target, percent = end
    return f"{target}%" if percent else str(target)


def _item(test: str, v: TestValue, rule: str) -> Dict[str, object]:
    return {"test": test, "value": v.value, "line": v.line, "rule": rule}


def _check_numbers(test: str, items: List[TestValue], row: RowFacts, checked: List[TestValue],
                   mismatches: List[Dict[str, object]], unparsed: List[Dict[str, object]]) -> None:
    numbers = [v for v in items if v.kind == "number"]
    literal: List[Tuple[TestValue, Decimal]] = []
    had_unparsed = False
    for v in numbers:
        x = _dec(v.value)
        if x is None:
            unparsed.append(_item(test, v, "runtime expression"))
            had_unparsed = True
        elif v.name is None:
            unparsed.append(_item(test, v, "no name"))
            had_unparsed = True
        else:
            literal.append((v, x))
    if not numbers:
        return
    ends = row.thresholds
    if not ends:
        rule = "row states no threshold"
    elif len(ends) > 1 and not row.is_range:
        rule = "row states several thresholds"
    else:
        rule = None
    if rule is not None:
        for v, _ in literal:
            unparsed.append(_item(test, v, rule))
        return
    for v, x in literal:
        if any(_matches(x, e) for e in ends):
            checked.append(v)
    if had_unparsed:
        return  # a runtime or unnamed value makes the threshold undecidable: only the reviewer sees it
    for end in ends:
        if not any(_matches(x, end) for _, x in literal):
            mismatches.append({"test": test, "value": ", ".join(v.value for v, _ in literal),
                               "expected": _fmt(end), "line": literal[0][0].line if literal else 0,
                               "rule": "threshold"})


def _check_test(test: str, items: List[TestValue], row: RowFacts, models: ModelFacts,
                checked: List[TestValue], mismatches: List[Dict[str, object]],
                unparsed: List[Dict[str, object]]) -> None:
    _check_numbers(test, items, row, checked, mismatches, unparsed)
    for v in items:
        if v.kind == "date":
            if v.role != "fed":
                continue
            if v.name is None:
                unparsed.append(_item(test, v, "no name"))
            elif not row.dates:
                unparsed.append(_item(test, v, "row states no date"))
            elif date.fromisoformat(v.value[:10]) in row.dates:
                checked.append(v)
            else:
                mismatches.append({"test": test, "value": v.value,
                                   "expected": [d.isoformat() for d in sorted(row.dates)],
                                   "line": v.line, "rule": "date"})
        elif v.kind in ("enum", "string"):
            if v.name is None:
                if v.kind == "enum":
                    unparsed.append(_item(test, v, "no name"))
                continue
            if v.name not in models.allowed:
                if v.kind == "enum":
                    unparsed.append(_item(test, v, "no model for field"))
                continue
            allowed = models.allowed[v.name]
            if v.value.upper() in {m.upper() for m in allowed}:
                checked.append(v)
            else:
                mismatches.append({"test": test, "value": v.value, "allowed": sorted(allowed),
                                   "line": v.line, "rule": "enum"})


def check_values(values: List[TestValue], row: Optional[RowFacts], models: ModelFacts) -> FidelityResult:
    """Partition the values into checked, mismatches, unparsed and unmatched. A test with no TC id, or whose TC id
    is not the row's, is unmatched. Pure: the inputs are not changed and nothing is written."""
    groups: Dict[str, List[TestValue]] = {}
    for v in values:
        groups.setdefault(v.test, []).append(v)
    checked: List[TestValue] = []
    mismatches: List[Dict[str, object]] = []
    unparsed: List[Dict[str, object]] = []
    unmatched: List[str] = []
    for test, items in groups.items():
        tc = tc_of_node(test)
        if tc is None or row is None or row.tc_id != tc:
            unmatched.append(test)
            continue
        _check_test(test, items, row, models, checked, mismatches, unparsed)
    return FidelityResult(checked=tuple(checked), mismatches=tuple(mismatches), unparsed=tuple(unparsed),
                          unmatched=tuple(unmatched))


# --------------------------------------------------------------------------- the task check


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _tc_sort_key(tc: Optional[str]) -> Tuple[bool, int]:
    return (tc is None, int(tc[3:]) if tc else 0)


def check_task(project_dir, spec_dir, task: str) -> FidelityResult:
    """Run the seam over a test-track task's test files (its `owns` test files in the spec graph), against the
    spec's test-plan.md and design.md. Reads only."""
    from claude_kiro.graph import build_graph

    project_dir, spec_dir = Path(project_dir), Path(spec_dir)
    graph = build_graph(project_dir, spec_dir)
    kinds = {node.id: node.kind for node in graph.nodes}
    files = sorted({e.dst[len("file:"):] for e in graph.edges
                    if e.kind == "owns" and e.src == f"task:{task}" and kinds.get(e.dst) == "test_file"})
    plan_text = _read_text(spec_dir / "test-plan.md")
    models = parse_models(_read_text(spec_dir / "design.md"))

    values: List[TestValue] = []
    names: List[str] = []
    unparsed: List[Dict[str, object]] = []
    for rel in files:
        full = project_dir / rel
        if not full.is_file():
            continue
        if not rel.endswith(".py"):
            unparsed.append({"test": rel, "value": None, "line": 0, "rule": "non-Python test"})
            continue
        try:
            source = full.read_text(encoding="utf-8")
            values.extend(extract_test_values(source, rel))
            names.extend(_test_names(source))
        except (OSError, SyntaxError, UnicodeDecodeError):
            unparsed.append({"test": rel, "value": None, "line": 0, "rule": "unparseable test file"})

    groups: Dict[Optional[str], List[TestValue]] = {}
    for v in values:
        groups.setdefault(tc_of_node(v.test), []).append(v)
    parts = []
    for tc in sorted(groups, key=_tc_sort_key):
        row = parse_row(plan_text, tc) if tc else None
        parts.append(check_values(groups[tc], row, models))

    seen = {v.test for v in values}
    extra_unmatched: List[str] = []
    for name in names:
        if name in seen:
            continue
        tc = tc_of_node(name)
        if tc is None or parse_row(plan_text, tc) is None:
            extra_unmatched.append(name)

    return FidelityResult(
        checked=tuple(v for r in parts for v in r.checked),
        mismatches=tuple(m for r in parts for m in r.mismatches),
        unparsed=tuple(unparsed) + tuple(u for r in parts for u in r.unparsed),
        unmatched=tuple(u for r in parts for u in r.unmatched) + tuple(extra_unmatched),
    )
