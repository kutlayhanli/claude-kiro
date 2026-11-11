"""Gate simulation helpers shared by the gate and `ck lint`.

- `suite_due`: whether a test task's suite must already pass when a gate
  runs. `gate.py` calls this same function, so the gate and its simulation
  cannot disagree.
- `done_at_gate`: the tasks treated as Done when a gate for one task runs
  (the Done tasks, the task itself, and its transitive dependencies).
- `lint_verdict`: the exit rule of `ck lint`. Cycles, verify cycles, verify
  scope findings and justification findings set `exit_nonzero`; warnings do
  not. Only verify-scope findings are `launch_blockers`.

Pure functions over parsed tasks and plain result mappings: no project code
is imported, and no file is read (NFR: lint imports no project code).
"""

import ast
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from claude_kiro.hooks._shared.spec_parser import SpecTask, parse_tasks
from claude_kiro.hooks._shared.test_heuristics import is_test_path


@dataclass(frozen=True)
class SimTest:  # one test function as source text sees it
    name: str
    cls: str
    source: str


@dataclass(frozen=True)
class SimInput:  # built by load_sim_input from tasks.md and test files
    tasks: Mapping[str, SpecTask]
    runs: Mapping[str, Mapping[str, Any]]  # task -> file -> selector (None = whole file)
    unresolved: Mapping[str, Sequence[Tuple[str, str]]]  # task -> [(command, reason)]
    tests: Mapping[str, Sequence[SimTest]]  # file -> tests
    module_source: Mapping[str, str]  # file -> code outside test functions
    programs: Sequence[str]


@dataclass(frozen=True)
class ScopeFinding:  # sorted by (int task, kind, suite, testTask)
    kind: str  # the only kind: "unfiltered-shared-suite"
    task: str  # the task whose gate is simulated
    suite: str  # the test file
    testTask: str  # the test task that makes the suite shared
    message: str


@dataclass(frozen=True)
class ScopeResult:
    findings: Tuple[ScopeFinding, ...]
    not_checked: Tuple[Tuple[str, str, str], ...]  # (task, command, reason)


@dataclass(frozen=True)
class LintVerdict:
    exit_nonzero: bool  # cycles, verify cycles, verify scope, justifications
    launch_blockers: Tuple[Mapping, ...]  # verify-scope findings only; justifications never block a launch


def suite_due(test_task: SpecTask, tasks: Mapping[str, SpecTask], assumed_done: Set[str]) -> bool:
    """A test task's suite is due when every task it verifies is assumed Done."""
    targets = [n for n in test_task.verifies if n in tasks]
    return bool(targets) and all(n in assumed_done for n in targets)


def _transitive_dependencies(tasks: Mapping[str, SpecTask], num: str) -> Set[str]:
    seen: Set[str] = set()
    stack = list(tasks[num].dependencies) if num in tasks else []
    while stack:
        dep = stack.pop()
        if dep in tasks and dep not in seen:
            seen.add(dep)
            stack.extend(tasks[dep].dependencies)
    return seen


def done_at_gate(tasks: Mapping[str, SpecTask], num: str) -> Set[str]:
    """Tasks counted as Done for a gate of `num`: every Done task, `num`, and its transitive dependencies."""
    done = {n for n, t in tasks.items() if t.done}
    return done | {num} | _transitive_dependencies(tasks, num)


def lint_verdict(result: Mapping[str, Any]) -> LintVerdict:
    """The exit rule of `ck lint` over a lint result mapping.

    Missing keys count as empty, so a result from an older lint run still
    gives a verdict. Warnings (`warnings`, `notChecked`) never set the exit.
    """
    verify_scope: Tuple[Mapping, ...] = tuple(result.get("verifyScope") or ())
    exit_nonzero = bool(
        result.get("cycles")
        or result.get("verifyCycles")
        or verify_scope
        or result.get("justifications")
    )
    return LintVerdict(exit_nonzero=exit_nonzero, launch_blockers=verify_scope)


def _norm(path: str) -> str:
    return path.removeprefix("./")


def _num_key(num: str) -> Tuple[int, int, str]:
    return (0, int(num), num) if num.isdigit() else (1, 0, num)


def _selection(runs: Mapping[str, Any], path: str, tests: Sequence[SimTest]) -> Set[str]:
    """Names of the tests of `path` that a task's runs select (empty when the task runs no part of it)."""
    if path not in runs:
        return set()
    selector = runs[path]
    return {t.name for t in tests if selector is None or selector(t.name, t.cls)}


def scope_findings(sim: SimInput) -> ScopeResult:
    """Simulate the gate of each task that is not Done and report unfiltered shared suites (12.1 to 12.4, 12.9).

    A finding (task, suite, testTask) exists when the gate of `task` selects a test of a shared suite that
    belongs to `testTask`, and `testTask`'s suite is not due at that gate. Nothing else is a finding: a
    selected call to a later command is handled by the live command edges, not here (12.3).
    """
    tasks = sim.tasks
    owners: Dict[str, List[str]] = {}
    for num, task in tasks.items():
        if task.track == "test":
            for path in dict.fromkeys(_norm(f) for f in task.files):
                owners.setdefault(path, []).append(num)
    shared = {path: nums for path, nums in owners.items() if len(set(nums)) >= 2}

    found: Set[Tuple[str, str, str]] = set()
    for num, task in tasks.items():
        if task.done or not shared:
            continue
        assumed = done_at_gate(tasks, num)
        ran: List[Mapping[str, Any]] = []
        if task.track == "test":
            if suite_due(task, tasks, assumed):
                ran.append(sim.runs.get(num, {}))
        else:
            ran.append(sim.runs.get(num, {}))
            for test_num in task.verified_by:
                test_task = tasks.get(test_num)
                if test_task is not None and test_task.done and suite_due(test_task, tasks, assumed):
                    ran.append(sim.runs.get(test_num, {}))
        for path, test_nums in shared.items():
            tests = sim.tests.get(path, ())
            chosen: Set[str] = set()
            for runs in ran:
                chosen |= _selection(_norm_runs(runs), path, tests)
            if not chosen:
                continue
            for test_num in test_nums:
                if suite_due(tasks[test_num], tasks, assumed):
                    continue
                mine = _selection(_norm_runs(sim.runs.get(test_num, {})), path, tests)
                if chosen & mine:
                    found.add((num, path, test_num))

    findings = tuple(
        ScopeFinding(
            kind="unfiltered-shared-suite",
            task=num,
            suite=path,
            testTask=test_num,
            message=(
                f"Task {num}'s gate selects tests of {path} that belong to Task {test_num}, whose suite is not due "
                f"at Task {num}'s gate. Narrow Task {num}'s Verify to its own tests in {path}."
            ),
        )
        for num, path, test_num in sorted(found, key=lambda f: (_num_key(f[0]), "unfiltered-shared-suite", f[1], f[2]))
    )

    not_checked = tuple(
        (num, command, reason)
        for num in sorted(sim.unresolved, key=_num_key)
        if not (num in tasks and tasks[num].done)
        for command, reason in sim.unresolved[num]
    )
    return ScopeResult(findings=findings, not_checked=not_checked)


def _norm_runs(runs: Mapping[str, Any]) -> Mapping[str, Any]:
    return {_norm(p): s for p, s in runs.items()}


def dedupe_scope(result: ScopeResult, existing: Mapping[str, Any]) -> Tuple[ScopeResult, List[dict]]:
    """Drop findings whose (task, suite) an existing lint key already reports (12.10).

    `existing` maps lint keys to lists of entries; an entry names its task and its suite (`module`, or `file`).
    Returns the remaining result and the dropped findings, each with the key that already reports it.
    """
    taken: Dict[Tuple[str, str], str] = {}
    for key, entries in existing.items():
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            task = str(entry.get("task", ""))
            suite = entry.get("module", entry.get("file"))
            if task and suite:
                taken.setdefault((task, str(suite)), key)
    kept: List[ScopeFinding] = []
    extra: List[dict] = []
    for finding in result.findings:
        key = (finding.task, finding.suite)
        if key in taken:
            extra.append({"task": finding.task, "kind": finding.kind, "suite": finding.suite,
                          "testTask": finding.testTask, "existing": taken[key]})
        else:
            kept.append(finding)
    return ScopeResult(findings=tuple(kept), not_checked=result.not_checked), extra


# --------------------------------------------------------------------------- building the simulation input


def _is_test_function(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")


def _parse_test_file(path: Path) -> Tuple[List[SimTest], str]:
    """(test functions, code outside them) of one test file, parsed once with `ast` (no import, no execution)."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return [], ""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return [], ""
    tests: List[SimTest] = []
    outside: List[str] = []
    for node in tree.body:
        if _is_test_function(node):
            tests.append(SimTest(name=node.name, cls="", source=ast.get_source_segment(text, node) or ""))
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if _is_test_function(item):
                    tests.append(SimTest(name=item.name, cls=node.name, source=ast.get_source_segment(text, item) or ""))
                else:
                    outside.append(ast.get_source_segment(text, item) or "")
        else:
            outside.append(ast.get_source_segment(text, node) or "")
    return tests, "\n".join(s for s in outside if s)


def _unresolved_reason(command: str, project_dir: Path, config: dict) -> Optional[str]:
    """Why a Verify command's test selection cannot be resolved statically, or None (12.9)."""
    from claude_kiro.lint import _k_selector, verify_paths

    if "$" in command:
        return "shell variable or command substitution"
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    pytest_at = next((i for i, w in enumerate(words) if PurePosixPath(w).name == "pytest"), None)
    if pytest_at is None:
        return None
    for i, w in enumerate(words):
        if w == "-k" and i + 1 < len(words) and _k_selector(words[i + 1]) is None:
            return "unreadable -k"
    for i, w in enumerate(words[pytest_at + 1:], start=pytest_at + 1):
        if w.startswith("-") or words[i - 1] == "-k":
            continue
        target = w.split("::")[0]
        if any(c in target for c in ("*", "?", "[")):
            return "glob"
        if (project_dir / target).is_dir():
            return "directory"
    paths = [p for p, _ in verify_paths(command, config)]
    if not paths:
        return "bare pytest selects every test"
    for path in paths:
        if not (project_dir / path).is_file():
            return "not on disk yet"
    return None


def load_sim_input(tasks_md: Path, project_dir: Path, config: dict) -> SimInput:
    """Build the simulation input from tasks.md and the test files it names (ast and text only)."""
    from claude_kiro.lint import _verify_runs

    tasks = {t.num: t for t in parse_tasks(tasks_md)}
    runs: Dict[str, Dict[str, Any]] = {}
    unresolved: Dict[str, List[Tuple[str, str]]] = {}
    test_paths: Set[str] = set()
    for num, task in tasks.items():
        runs[num] = {_norm(p): s for p, s in _verify_runs(task.verify, config).items()} if task.verify else {}
        test_paths.update(runs[num])
        if task.track == "test":
            test_paths.update(_norm(f) for f in task.files if is_test_path(f, config))
        unresolved[num] = [] if task.done else [
            (command, reason)
            for command in task.verify
            for reason in [_unresolved_reason(command, project_dir, config)]
            if reason
        ]
    tests: Dict[str, List[SimTest]] = {}
    module_source: Dict[str, str] = {}
    for path in sorted(test_paths):
        tests[path], module_source[path] = _parse_test_file(project_dir / path)
    return SimInput(tasks=tasks, runs=runs, unresolved=unresolved, tests=tests,
                    module_source=module_source, programs=("ck",))
