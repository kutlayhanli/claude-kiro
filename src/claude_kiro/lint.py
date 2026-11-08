"""Plan checks for a spec's tasks.md, run before (and during) implementation.

- Dependency cycles: no order can satisfy them.
- Verify-order cycles: impl Task X is **Verified by** test Task T, but T's
  tests import a module owned by Task Y, and Y depends (transitively) on X.
  X's gate can never pass, because its oracle needs code that can only come
  after X. Found by following T's test files' Python imports, through test
  helpers, to the task whose **Files:** list the imported module. This works
  before the module exists, because ownership comes from tasks.md. Not a
  cycle: a module X itself lists in **Files:** (X creates or extends it), or
  T's suite when T also verifies Y (the gate defers T's suite until every
  task it verifies is Done). In that case only X's own Verify commands that
  run T's files count, and a `-k` or node-ID selection there limits the
  in-test-body imports to the selected tests.
- Dependencies without a stated reason: every edge serializes work, so each
  should say why ("Task 3 (uses its parser API)"). Reported, not fatal.
- Risk paths: a task whose **Files:** match a `risk_paths` glob in specs/ck.json
  is treated as **Risk:** safety; an untagged one is reported, not fatal.
- Critical path: the longest dependency chain, the floor on wall-clock time
  however many agents run.
"""

import ast
import re
import shlex
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

from claude_kiro.config import load_config
from claude_kiro.hooks._shared.spec_parser import SpecTask, parse_tasks
from claude_kiro.hooks._shared.test_heuristics import is_test_path

IMPORT = re.compile(r"^\s*(?:from\s+(\.*[\w.]*)\s+import\s+([\w*, ()]+)|import\s+([\w., ]+))", re.MULTILINE)


def _ancestors(tasks: Dict[str, SpecTask], num: str) -> Set[str]:
    seen: Set[str] = set()
    stack = list(tasks[num].dependencies)
    while stack:
        dep = stack.pop()
        if dep in tasks and dep not in seen:
            seen.add(dep)
            stack.extend(tasks[dep].dependencies)
    return seen


def dependency_cycles(tasks: Dict[str, SpecTask]) -> List[List[str]]:
    """Each cycle as a list of task numbers, e.g. ["3", "5", "3"]."""
    cycles, state, path = [], {}, []

    def visit(num: str) -> None:
        state[num] = "active"
        path.append(num)
        for dep in tasks[num].dependencies:
            if dep not in tasks:
                continue
            if state.get(dep) == "active":
                cycle = path[path.index(dep):] + [dep]
                if sorted(cycle[:-1]) not in [sorted(c[:-1]) for c in cycles]:
                    cycles.append(cycle)
            elif dep not in state:
                visit(dep)
        path.pop()
        state[num] = "done"

    for num in tasks:
        if num not in state:
            visit(num)
    return cycles


def _module_candidates(module: str, names: List[str], base: PurePosixPath) -> List[str]:
    """Repo-relative file paths an import could refer to."""
    if module.startswith("."):
        dots = len(module) - len(module.lstrip("."))
        package = base
        for _ in range(dots - 1):
            package = package.parent
        rel = module.lstrip(".")
        prefix = str(package / rel.replace(".", "/")) if rel else str(package)
        stems = [prefix] + [f"{prefix}/{n}" for n in names]
        roots = [""]
    else:
        path = module.replace(".", "/")
        stems = [path] + [f"{path}/{n}" for n in names]
        roots = ["", "src/", "lib/"]
    out = []
    for root in roots:
        for stem in stems:
            stem = stem.lstrip("./")
            out += [f"{root}{stem}.py", f"{root}{stem}/__init__.py"]
    return out


def _imports(text: str, rel_file: str) -> List[str]:
    base = PurePosixPath(rel_file).parent
    candidates = []
    for match in IMPORT.finditer(text):
        if match.group(3):
            for module in match.group(3).split(","):
                module = module.strip().split(" as ")[0].strip()
                if module:
                    candidates += _module_candidates(module, [], base)
        else:
            names = [n.strip().split(" as ")[0] for n in match.group(2).strip("() ").split(",") if n.strip() and n.strip() != "*"]
            candidates += _module_candidates(match.group(1), names, base)
    return candidates


def _test_files(task: SpecTask, project_dir: Path, config: dict) -> List[str]:
    files = [f for f in task.files if is_test_path(f, config)]
    for command in task.verify:
        for word in command.split():
            word = word.split("::")[0]
            if word.endswith(".py") and is_test_path(word, config):
                files.append(word)
    return list(dict.fromkeys(files))


Selector = Optional[object]  # None: the whole file runs; else a callable (test name, class name) -> bool


def _k_selector(expr: str):
    """pytest `-k EXPR` as a predicate over (test name, class name), or None if it can't be read.

    Each bare word matches as a case-insensitive substring of the test or class name; `and`, `or`,
    `not` and parentheses combine them, as in pytest.
    """
    tokens = re.findall(r"\(|\)|[^\s()]+", expr)
    if not tokens:
        return None
    words = []
    out = []
    for tok in tokens:
        low = tok.lower()
        if tok in ("(", ")") or low in ("and", "or", "not"):
            out.append(low if low in ("and", "or", "not") else tok)
        elif re.fullmatch(r"[\w.\[\]\-]+", tok):
            words.append(tok.lower())
            out.append(f"_w[{len(words) - 1}]")
        else:
            return None
    source = " ".join(out)
    try:
        code = compile(source, "<-k>", "eval")
    except SyntaxError:
        return None

    def select(name: str, cls: str = "") -> bool:
        hay = f"{cls} {name}".lower()
        try:
            return bool(eval(code, {"__builtins__": {}}, {"_w": [w in hay for w in words]}))
        except Exception:
            return True

    return select


def _node_selector(names: List[str]):
    def select(name: str, cls: str = "") -> bool:
        return any(n == name or n == cls or n.split("[")[0] == name for n in names)

    return select


def _verify_runs(commands: List[str], config: dict) -> Dict[str, Selector]:
    """{test file: selector} for the test files a task's Verify commands run.

    A file run whole (in any command) maps to None. A file run only under `-k` or as node IDs
    maps to a predicate saying which tests run; several selections of one file are OR-ed.
    """
    runs: Dict[str, list] = {}
    for command in commands:
        try:
            words = shlex.split(command)
        except ValueError:
            words = command.split()
        k_expr = None
        for i, w in enumerate(words):
            if w == "-k" and i + 1 < len(words):
                k_expr = words[i + 1]
            elif w.startswith("-k") and len(w) > 2:
                k_expr = w[2:].lstrip("=")
        whole: Set[str] = set()
        nodes: Dict[str, List[str]] = {}
        for w in words:
            path, _, node = w.partition("::")
            if not (path.endswith(".py") and is_test_path(path, config)):
                continue
            if node:
                nodes.setdefault(path, []).append(node.split("::")[-1])
            else:
                whole.add(path)
        k_select = _k_selector(k_expr) if k_expr else None
        for path in whole:
            if k_expr and k_select is not None:
                runs.setdefault(path, []).append(k_select)
            else:
                runs[path] = [None]
        for path, names in nodes.items():
            runs.setdefault(path, []).append(_node_selector(names))
    out: Dict[str, Selector] = {}
    for path, sels in runs.items():
        if any(s is None for s in sels):
            out[path] = None
        else:
            out[path] = (lambda ss: lambda name, cls="": any(s(name, cls) for s in ss))(sels)
    return out


def _selected_imports(text: str, rel: str, select) -> List[str]:
    """The import candidates of a test file when only the tests `select` accepts run.

    Module-level imports always run (at collection), and so may any import in a non-test helper.
    An import inside a test function (or test method) counts only if that test is selected.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _imports(text, rel)
    base = PurePosixPath(rel).parent
    candidates: List[str] = []

    def take(node: ast.AST) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Import):
                for alias in sub.names:
                    candidates.extend(_module_candidates(alias.name, [], base))
            elif isinstance(sub, ast.ImportFrom):
                module = "." * sub.level + (sub.module or "")
                names = [a.name for a in sub.names if a.name != "*"]
                candidates.extend(_module_candidates(module, names, base))

    is_test = lambda n: isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")  # noqa: E731
    for node in tree.body:
        if is_test(node):
            if select(node.name):
                take(node)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if is_test(item):
                    if select(item.name, node.name):
                        take(item)
                else:
                    take(item)
        else:
            take(node)
    return candidates


def _owners_reached(start, project_dir: Path, owner_of: Dict[str, List[str]], max_files: int = 400) -> Dict[str, List[str]]:
    """{owner task: imported files} for project modules reachable from `start`.

    `start` is a list of files (each run whole) or a {file: selector} map (see `_verify_runs`):
    a selector limits that file's in-test imports to the selected tests. Expands through files
    nobody owns (test helpers, harness modules) in full; stops at owned files, since their own
    imports are covered by their tasks' dependencies.
    """
    selectors: Dict[str, Selector] = dict(start) if isinstance(start, dict) else {f: None for f in start}
    reached: Dict[str, List[str]] = {}
    seen: Set[str] = set()
    queue = list(selectors)
    while queue and len(seen) < max_files:
        rel = queue.pop(0)
        if rel in seen:
            continue
        seen.add(rel)
        path = project_dir / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        select = selectors.get(rel)
        for candidate in (_selected_imports(text, rel, select) if select is not None else _imports(text, rel)):
            if candidate in owner_of:
                for owner in owner_of[candidate]:
                    mods = reached.setdefault(owner, [])
                    if candidate not in mods:
                        mods.append(candidate)
            elif candidate not in seen and (project_dir / candidate).is_file():
                queue.append(candidate)
    return reached


def verify_order_cycles(tasks: Dict[str, SpecTask], project_dir: Path, config: Optional[dict] = None) -> List[Dict[str, str]]:
    config = config or load_config(project_dir)
    owner_of: Dict[str, List[str]] = {}
    for num, task in tasks.items():
        if task.track != "test":
            for f in task.files:
                owner_of.setdefault(f.removeprefix("./"), []).append(num)

    found = []
    for num, task in tasks.items():
        if task.track == "test" or task.done:
            continue
        own_runs = None  # X's own Verify commands' test files, read once on first need
        for test_num in task.verified_by:
            test_task = tasks.get(test_num)
            if not test_task:
                continue
            test_files = _test_files(test_task, project_dir, config)
            suite = _owners_reached(test_files, project_dir, owner_of)
            selected = None
            for owner in sorted(suite):
                if owner == num or owner not in tasks or num not in _ancestors(tasks, owner):
                    continue
                if owner in test_task.verifies or test_num in tasks[owner].verified_by:
                    # The gate runs T's suite only once every task T verifies is Done, so it is
                    # never required at X's gate before Y lands. Only X's own Verify commands that
                    # run T's files can reach Y's module there, under their own selection.
                    if own_runs is None:
                        own_runs = _verify_runs(task.verify, config)
                    if selected is None:
                        selected = _owners_reached({f: s for f, s in own_runs.items() if f in test_files}, project_dir, owner_of)
                    modules = selected.get(owner, [])
                else:
                    modules = suite[owner]
                # A module X itself lists in **Files:** is X's to create or extend: not a cycle.
                modules = [m for m in modules if num not in owner_of.get(m, [])]
                for module in modules[:1]:
                    found.append(
                        {
                            "task": num,
                            "verifiedBy": test_num,
                            "needs": owner,
                            "module": module,
                            "message": (
                                f"Task {num} is verified by Task {test_num}, whose tests import {module} "
                                f"(owned by Task {owner}), but Task {owner} depends on Task {num}: "
                                f"Task {num}'s gate can't pass before Task {owner} exists. Make Task {num} a "
                                f"foundation task (smoke Verify) and have Task {test_num} verify Task {owner}."
                            ),
                        }
                    )
    return found


_GLOB_CHARS = ("*", "?", "[")


def verify_paths(command: str, config: dict) -> List[Tuple[str, bool]]:
    """(path, narrowed) pairs for the test-file tokens of one Verify command.

    narrowed is True when the token carried a `::` selector, when the command
    holds a `-k` option (which narrows every file in it), or when a `node <test
    file>` command names test cases after the file (`node t.test.mjs TC-1 TC-3`,
    the JS harness's own selector)."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    command_narrowed = any(tok == "-k" or (tok.startswith("-k") and len(tok) > 2) for tok in tokens)
    node_selected = False
    if tokens and PurePosixPath(tokens[0]).name == "node":
        rest = [t for t in tokens[1:] if not t.startswith("-")]
        node_selected = len(rest) > 1 and all("/" not in t and "." not in t for t in rest[1:])
    out: List[Tuple[str, bool]] = []
    for tok in tokens:
        selected = "::" in tok or node_selected
        path = tok.split("::")[0].removeprefix("./")
        if not path or path.endswith("/") or any(c in path for c in _GLOB_CHARS):
            continue
        if not is_test_path(path, config):
            continue
        out.append((path, selected or command_narrowed))
    return out


def _shortest_chain(tasks: Dict[str, SpecTask], start: str, target: str) -> Optional[List[str]]:
    """Shortest dependency path start -> ... -> target (BFS over dependencies), or None."""
    parents: Dict[str, Optional[str]] = {start: None}
    queue = [start]
    while queue:
        node = queue.pop(0)
        if node == target:
            chain = []
            cur: Optional[str] = node
            while cur is not None:
                chain.append(cur)
                cur = parents[cur]
            return chain[::-1]  # start first: [start, ..., target]
        for dep in tasks[node].dependencies:
            if dep in tasks and dep not in parents:
                parents[dep] = node
                queue.append(dep)
    return None


def verify_file_cycles(tasks: Dict[str, SpecTask], config: dict) -> List[Dict]:
    """Impl task X whose Verify runs test file F, where test task T owns F, T verifies impl task Y,
    and Y depends on X (directly or transitively): X's gate can't pass before Y lands."""
    found: List[Dict] = []
    for x_num, x in tasks.items():
        if x.track == "test" or x.done:
            continue
        files: List[str] = []
        for command in x.verify:
            for path, narrowed in verify_paths(command, config):
                if not narrowed and path not in files:
                    files.append(path)
        for path in files:
            for t_num, t in tasks.items():
                if t.track != "test" or path not in {f.strip() for f in t.files}:
                    continue
                if x_num in t.verifies or t_num in x.verified_by:
                    continue
                ys = [y for y in t.verifies if y in tasks]
                ys += [y for y, task in tasks.items() if t_num in task.verified_by and y not in ys]
                for y_num in ys:
                    if y_num == x_num or tasks[y_num].track == "test":
                        continue
                    chain = _shortest_chain(tasks, y_num, x_num)
                    if chain is None:
                        continue
                    found.append(
                        {
                            "kind": "verify-file",
                            "task": x_num,
                            "file": path,
                            "testTask": t_num,
                            "needs": y_num,
                            "chain": chain,
                            "message": (
                                f"Task {x_num}'s Verify runs {path}, which Task {t_num} (test track) owns and which "
                                f"verifies Task {y_num}; Task {y_num} depends on Task {x_num} ({' -> '.join(chain)}), "
                                f"so Task {x_num}'s gate can't pass before Task {y_num} lands. "
                                f"Narrow Task {x_num}'s Verify to test files that don't need Task {y_num}, or, if "
                                f"{path} is Task {x_num}'s own oracle, link it: ck relink <spec> {x_num} --apply."
                            ),
                        }
                    )
    return found


def reasonless_dependencies(tasks: Dict[str, SpecTask]) -> List[Dict[str, str]]:
    """Dependencies with no stated reason. Depending on your own verifying test
    task is implicitly justified (its tests are the oracle), so it isn't listed.
    Edges between tasks with no shared files come first: the likeliest to be spurious."""
    out = []
    for num, task in tasks.items():
        for dep in task.dependencies:
            if dep in tasks and dep not in task.verified_by and not task.dependency_reasons.get(dep):
                shared = set(task.files) & set(tasks[dep].files)
                out.append({"task": num, "dependsOn": dep, "sharedFiles": sorted(shared)})
    return sorted(out, key=lambda e: (bool(e["sharedFiles"]), int(e["task"]) if e["task"].isdigit() else 0))


def critical_path(tasks: Dict[str, SpecTask], only_open: bool = True) -> List[str]:
    """Longest dependency chain (by task count), ignoring Done tasks when only_open."""
    open_tasks = {n for n, t in tasks.items() if not (only_open and t.done)}
    best: Dict[str, List[str]] = {}

    def chain(num: str, stack: frozenset) -> List[str]:
        if num in best:
            return best[num]
        longest: List[str] = []
        for dep in tasks[num].dependencies:
            if dep in open_tasks and dep not in stack:
                candidate = chain(dep, stack | {dep})
                if len(candidate) > len(longest):
                    longest = candidate
        best[num] = longest + [num]
        return best[num]

    paths = [chain(n, frozenset({n})) for n in open_tasks]
    return max(paths, key=len, default=[])


def _matches(path: str, pattern: str) -> bool:
    """Glob match on a project-relative path; a leading "**/" also matches at the root."""
    path, pattern = path.removeprefix("./"), pattern.removeprefix("./")
    return fnmatchcase(path, pattern) or (pattern.startswith("**/") and fnmatchcase(path, pattern[3:]))


def risk_path_hits(tasks: Dict[str, SpecTask], patterns: List[str]) -> Dict[str, List[Dict[str, str]]]:
    """{task: [{file, pattern}]} for tasks whose **Files:** match a risk_paths glob."""
    hits: Dict[str, List[Dict[str, str]]] = {}
    for num, task in tasks.items():
        for f in task.files:
            pattern = next((p for p in patterns if isinstance(p, str) and _matches(f, p)), None)
            if pattern:
                hits.setdefault(num, []).append({"file": f, "pattern": pattern})
    return hits


def task_risk(tasks: Dict[str, SpecTask], config: dict) -> Dict:
    """Effective risk per task (a **Risk:** safety tag, or a file on a risk path) and the untagged risk-path tasks."""
    patterns = config.get("risk_paths") or []
    hits = risk_path_hits(tasks, patterns if isinstance(patterns, list) else [patterns])
    risk = {n: "safety" if t.risk == "safety" or n in hits else "normal" for n, t in tasks.items()}
    untagged = [
        {"task": n, "file": hit["file"], "pattern": hit["pattern"]}
        for n, t in tasks.items()
        if t.risk != "safety" and not t.done
        for hit in hits.get(n, [])[:1]
    ]
    return {"risk": risk, "riskUntagged": untagged}


def lint_spec(spec_dir: Path, project_dir: Path) -> Dict:
    tasks = {t.num: t for t in parse_tasks(spec_dir / "tasks.md")}
    cycles = dependency_cycles(tasks)
    config = load_config(project_dir)
    risk = task_risk(tasks, config)
    return {
        "riskUntagged": risk["riskUntagged"],
        "cycles": cycles,
        "verifyCycles": ([] if cycles else verify_order_cycles(tasks, project_dir, config)) + verify_file_cycles(tasks, config),
        "reasonless": reasonless_dependencies(tasks),
        "criticalPath": [] if cycles else critical_path(tasks),
    }
