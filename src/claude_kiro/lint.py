"""Plan checks for a spec's tasks.md, run before (and during) implementation.

- Dependency cycles: no order can satisfy them.
- Verify-order cycles: impl Task X is **Verified by** test Task T, but T's
  tests import a module owned by Task Y, and Y depends (transitively) on X.
  X's gate can never pass, because its oracle needs code that can only come
  after X. Found by following T's test files' Python imports, through test
  helpers, to the task whose **Files:** list the imported module. This works
  before the module exists, because ownership comes from tasks.md. The gate
  runs T's suite only once every task T verifies is Done, so not a cycle:
  Y Done by then (Y is, or is a dependency of, a task T verifies; only X's
  own Verify commands that run T's files count then, and a `-k` or node-ID
  selection there limits the in-test-body imports to the selected tests); a
  module that a task Done by then also lists in **Files:** (X itself, a task
  X depends on, or one the suite waits for); or a module that already exists
  and defines every imported name (later owners only extend it).
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


Candidate = Tuple[str, Tuple[str, ...]]  # (repo-relative file an import could refer to, names imported from it)


def _module_candidates(module: str, names: List[str], base: PurePosixPath) -> List[Candidate]:
    """Repo-relative file paths an import could refer to, each with the names it must provide.

    The module's own file must provide `names`; a `names[i]` submodule file must merely exist.
    """
    if module.startswith("."):
        dots = len(module) - len(module.lstrip("."))
        package = base
        for _ in range(dots - 1):
            package = package.parent
        rel = module.lstrip(".")
        prefix = str(package / rel.replace(".", "/")) if rel else str(package)
        roots = [""]
    else:
        prefix = module.replace(".", "/")
        roots = ["", "src/", "lib/"]
    stems = [(prefix, tuple(names))] + [(f"{prefix}/{n}", ()) for n in names]
    out: List[Candidate] = []
    for root in roots:
        for stem, wanted in stems:
            stem = stem.lstrip("./")
            out += [(f"{root}{stem}.py", wanted), (f"{root}{stem}/__init__.py", wanted)]
    return out


def _imports(text: str, rel_file: str) -> List[Candidate]:
    base = PurePosixPath(rel_file).parent
    candidates: List[Candidate] = []
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


def _defined_names(path: Path) -> Optional[Set[str]]:
    """Names a Python file defines at module level (None if it does not exist or does not parse)."""
    if not path.is_file():
        return None
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return None
    names: Set[str] = set()
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                names.update(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            stack += [n for field in ("body", "orelse", "finalbody") for n in getattr(node, field, [])]
            stack += [n for h in getattr(node, "handlers", []) for n in h.body]
    return names


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


def _selected_imports(text: str, rel: str, select) -> List[Candidate]:
    """The import candidates of a test file when only the tests `select` accepts run.

    Module-level imports always run (at collection), and so may any import in a non-test helper.
    An import inside a test function (or test method) counts only if that test is selected.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _imports(text, rel)
    base = PurePosixPath(rel).parent
    candidates: List[Candidate] = []

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
    provided: Dict[str, Optional[Set[str]]] = {}
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
        for candidate, wanted in (_selected_imports(text, rel, select) if select is not None else _imports(text, rel)):
            if candidate in owner_of:
                # An owned file that already exists and defines every imported name can't fail the
                # import whatever its later owners add (a task extending an existing module).
                if candidate not in provided:
                    provided[candidate] = _defined_names(project_dir / candidate)
                have = provided[candidate]
                if have is not None and all(n in have for n in wanted):
                    continue
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

    ancestors: Dict[str, Set[str]] = {}

    def anc(n: str) -> Set[str]:
        if n not in ancestors:
            ancestors[n] = _ancestors(tasks, n)
        return ancestors[n]

    found = []
    for num, task in tasks.items():
        if task.track == "test" or task.done:
            continue
        # Done whenever X's gate runs: X itself (its own work) and everything X depends on.
        before_gate = {num} | anc(num)
        own_runs = None  # X's own Verify commands' test files, read once on first need
        for test_num in task.verified_by:
            test_task = tasks.get(test_num)
            if not test_task:
                continue
            # The gate runs T's suite only once every task T verifies is Done, so by then those
            # tasks and everything they depend on are Done too.
            verified = {v for v in test_task.verifies if v in tasks} | {v for v, t in tasks.items() if test_num in t.verified_by}
            before_suite = set(before_gate)
            for v in verified:
                before_suite |= {v} | anc(v)
            test_files = _test_files(test_task, project_dir, config)
            suite = _owners_reached(test_files, project_dir, owner_of)
            selected = None
            for owner in sorted(suite):
                if owner == num or owner not in tasks or num not in anc(owner):
                    continue
                if owner in before_suite:
                    # T's suite never runs at X's gate before Y lands. Only X's own Verify commands
                    # that run T's files can reach Y's module there, under their own selection.
                    if own_runs is None:
                        own_runs = _verify_runs(task.verify, config)
                    if selected is None:
                        selected = _owners_reached({f: s for f, s in own_runs.items() if f in test_files}, project_dir, owner_of)
                    modules, done_by_then = selected.get(owner, []), before_gate
                else:
                    modules, done_by_then = suite[owner], before_suite
                # A module that a task Done by then also lists in **Files:** (X itself, a task X
                # depends on, or one the suite waits for) exists by then: not a cycle.
                modules = [m for m in modules if not (set(owner_of.get(m, [])) & done_by_then)]
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

    # Sorted, so a tie between equally long chains picks the same one in every process (set order varies by run).
    paths = [chain(n, frozenset({n})) for n in sorted(open_tasks)]
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


_TC_ID = re.compile(r"(?:^|[_\s])tc\d+_")
_JS_TEST_NAME = re.compile(r"\btest\(\s*['\"`]([^'\"`]+)")
_QUOTED = re.compile(r"""(['"`])((?:\\.|(?!\1).)*)\1""")
_WORD = re.compile(r"[A-Za-z0-9_][\w\-]*")


def _task_ref(node: str) -> str:
    """A graph node id ("task:5") as the task reference lint reports ("Task 5")."""
    return f"Task {node.split(':', 1)[-1]}"


def _source_words(project_dir: Path, sources: List[str]) -> Set[str]:
    """Words of the string literals in the non-test source files (command names a source defines)."""
    words: Set[str] = set()
    for rel in sources:
        path = project_dir / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        literals: List[str] = []
        if rel.endswith(".py"):
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                tree = None
            if tree is not None:
                literals = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        else:
            literals = [m.group(2) for m in _QUOTED.finditer(text)]
        for literal in literals:
            words.update(_WORD.findall(literal))
    return words


def _untagged_tests(graph, project_dir: Path, owners: Dict[str, str]) -> List[Dict[str, str]]:
    """Test functions in the spec's test files whose name carries no TC id, with the test task that owns the file."""
    out: List[Dict[str, str]] = []
    for node in sorted(graph.nodes, key=lambda n: n.id):
        if node.kind != "test_file":
            continue
        path = project_dir / node.label
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if node.label.endswith(".py"):
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            names = [n.name for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")]
        else:
            names = _JS_TEST_NAME.findall(text)
        owner = owners.get(node.label, "")
        for name in names:
            if _TC_ID.search(name) is None:
                out.append({
                    "file": node.label,
                    "test": name,
                    "task": f"Task {owner}" if owner else "",
                    "message": f"{node.label}: {name} has no TC id (owned by Task {owner})" if owner
                    else f"{node.label}: {name} has no TC id",
                })
    return out


def _graph_findings(spec_dir: Path, project_dir: Path, tasks: Dict[str, SpecTask], config: dict) -> Dict[str, List]:
    """The keys the spec graph adds to lint (11.8, 12.1, 12.3, 1.9, 13.4). Reported, never fatal."""
    from claude_kiro.graph import _cli_rows, build_graph, missing_command_edges, unknown_commands

    graph = build_graph(project_dir, spec_dir)
    command_edges = []
    for m in missing_command_edges(graph):
        provider, lacking = _task_ref(m.provider), _task_ref(m.lacking)
        test_file = m.test_file.split(":", 1)[-1]
        command_edges.append({
            "testFile": test_file,
            "command": m.command,
            "provider": provider,
            "task": lacking,
            "message": (f"{test_file} invokes `{m.command}`, which {provider} provides, but {lacking} "
                        f"does not depend on {provider}"),
        })

    design = spec_dir / "design.md"
    design_text = design.read_text(errors="replace") if design.is_file() else ""
    cli_rows = _cli_rows(design_text)
    design_commands = {cmd for cmd, _ in cli_rows}
    sources = sorted({n.label for n in graph.nodes if n.kind == "source_file"} | {f for _, f in cli_rows})
    sources = [s for s in sources if not is_test_path(s, config)]
    unknown = []
    for u in unknown_commands(graph, _source_words(project_dir, sources)):
        if u.command in design_commands:
            continue  # the design's CLI table declares it
        unknown.append({
            "command": u.command,
            "testFiles": [f.split(":", 1)[-1] for f in u.test_files],
            "message": f"`{u.command}` is invoked by {', '.join(f.split(':', 1)[-1] for f in u.test_files)} "
                       "but no task provides it and no source file defines it",
        })

    owners = {f.removeprefix("./"): num for num, t in tasks.items() if t.track == "test" for f in t.files}
    no_write_set = [
        {"task": f"Task {num}", "message": f"Task {num} ({t.title}) has no **Files:**, so its write set is unknown"}
        for num, t in sorted(tasks.items(), key=lambda kv: int(kv[0]))
        if t.track != "test" and not t.files and not t.done
    ]
    return {
        "commandEdges": command_edges,
        "unknownCommands": unknown,
        "untaggedTests": _untagged_tests(graph, project_dir, owners),
        "noWriteSet": no_write_set,
        "graphProblems": [{"source": p.source, "problem": p.problem} for p in graph.problems],
    }


def _justification_list(spec_dir: Path) -> List[Dict[str, str]]:
    """Stories of requirements.md with no `**Without it:**` line in their own section (3.6). Never fatal alone."""
    from claude_kiro.justifications import justification_findings

    req = spec_dir / "requirements.md"
    text = req.read_text(errors="replace") if req.is_file() else None
    return [
        {
            "story": f.story,
            "title": f.title,
            "message": f"Story {f.story} ({f.title}) has no **Without it:** line in requirements.md",
        }
        for f in justification_findings(text)
    ]


def _principles_warnings(spec_dir: Path) -> List[Dict[str, str]]:
    """A warning when PLAN.md exists and its `## Principles` section is missing or empty (3.7). Never fatal."""
    from claude_kiro.principles import plan_section

    plan = spec_dir / "PLAN.md"
    if not plan.is_file():
        return []
    # plan_section returns None for both a missing and an empty section, so None means "warn".
    if plan_section(plan.read_text(errors="replace")) is not None:
        return []
    return [{
        "kind": "principles",
        "plan": str(plan),
        "message": f"{spec_dir.name}/PLAN.md has an empty or missing ## Principles section",
    }]


def _scope_entry(finding) -> Dict[str, str]:
    return {"kind": finding.kind, "task": finding.task, "suite": finding.suite,
            "testTask": finding.testTask, "message": finding.message}


def _not_checked_entry(num: str, command: str, reason: str) -> Dict[str, str]:
    return {"task": num, "command": command, "reason": reason,
            "message": f"Task {num}'s Verify `{command}` is not checked ({reason}); lint does not simulate its selection"}


def lint_spec(spec_dir: Path, project_dir: Path) -> Dict:
    from claude_kiro.lint_scope import dedupe_scope, load_sim_input, scope_findings

    tasks = {t.num: t for t in parse_tasks(spec_dir / "tasks.md")}
    cycles = dependency_cycles(tasks)
    config = load_config(project_dir)
    risk = task_risk(tasks, config)
    result = {
        "riskUntagged": risk["riskUntagged"],
        "cycles": cycles,
        "verifyCycles": ([] if cycles else verify_order_cycles(tasks, project_dir, config)) + verify_file_cycles(tasks, config),
        "reasonless": reasonless_dependencies(tasks),
        "criticalPath": [] if cycles else critical_path(tasks),
    }
    result.update(_graph_findings(spec_dir, project_dir, tasks, config))
    result["justifications"] = _justification_list(spec_dir)
    result["warnings"] = _principles_warnings(spec_dir)
    # Verify scope (12.1 to 12.10): a problem verifyCycles already reports is not reported twice (12.10).
    scope = scope_findings(load_sim_input(spec_dir / "tasks.md", project_dir, config))
    scope, _ = dedupe_scope(scope, {"verifyCycles": result["verifyCycles"]})
    # A Verify whose file a verify-order cycle already names is reported there, not as not checked.
    reported = {(str(e.get("task")), str(e.get(k))) for e in result["verifyCycles"] for k in ("file", "module") if e.get(k)}
    unchecked = [
        (num, command, reason)
        for num, command, reason in scope.not_checked
        if not any(num == task and path in command for task, path in reported)
    ]
    result["verifyScope"] = [_scope_entry(f) for f in scope.findings]
    result["notChecked"] = [_not_checked_entry(*entry) for entry in unchecked]
    return result


def simulate_gate(spec_dir: Path, project_dir: Path, num: str) -> Dict:
    """`ck lint --simulate --task N`: the suites due at N's gate and the tests they select (12.1, 12.2).

    Tasks the gate treats as Done (`done_at_gate`) count as Done here. A test task's own selection is its Verify
    runs; an impl task's gate runs its own Verify and the Verify of each test task that verifies it and is due.
    """
    from claude_kiro.lint_scope import _norm, _norm_runs, _selection, done_at_gate, load_sim_input, suite_due

    sim = load_sim_input(spec_dir / "tasks.md", project_dir, load_config(project_dir))
    tasks = sim.tasks
    task = tasks.get(num)
    if task is None or task.done:
        return {"task": num, "suites": [], "selected": []}
    assumed = done_at_gate(tasks, num)
    if task.track == "test":
        due = [task] if suite_due(task, tasks, assumed) else []
    else:
        due = [tasks[n] for n in task.verified_by if n in tasks and suite_due(tasks[n], tasks, assumed)]
    suites = sorted(
        ({"file": path, "testTask": t.num} for t in due for path in dict.fromkeys(_norm(f) for f in t.files)),
        key=lambda s: (_num_key(s["testTask"]), s["file"]),
    )
    ran = [sim.runs.get(num, {})] + [sim.runs.get(t.num, {}) for t in due]
    selected = []
    for path in sorted({p for runs in ran for p in runs}):
        names: set = set()
        for runs in ran:
            names |= _selection(_norm_runs(runs), path, sim.tests.get(path, ()))
        if names:
            selected.append({"file": path, "tests": sorted(names)})
    return {"task": num, "suites": suites, "selected": selected}


def _num_key(num: str) -> Tuple[int, int, str]:
    return (0, int(num), num) if num.isdigit() else (1, 0, num)
