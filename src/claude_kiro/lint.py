"""Plan checks for a spec's tasks.md, run before (and during) implementation.

- Dependency cycles: no order can satisfy them.
- Verify-order cycles: impl Task X is **Verified by** test Task T, but T's
  tests import a module owned by Task Y, and Y depends (transitively) on X.
  X's gate can never pass, because its oracle needs code that can only come
  after X. Found by following T's test files' Python imports, through test
  helpers, to the task whose **Files:** list the imported module. This works
  before the module exists, because ownership comes from tasks.md.
- Dependencies without a stated reason: every edge serializes work, so each
  should say why ("Task 3 (uses its parser API)"). Reported, not fatal.
- Critical path vs. wave count: how much of the wave count is forced by the
  dependency chain.
"""

import re
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set

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


def _owners_reached(start: List[str], project_dir: Path, owner_of: Dict[str, List[str]], max_files: int = 400) -> Dict[str, str]:
    """{owner task: imported file} for project modules reachable from `start`.

    Expands through files nobody owns (test helpers, harness modules); stops at
    owned files, since their own imports are covered by their tasks' dependencies.
    """
    reached: Dict[str, str] = {}
    seen: Set[str] = set()
    queue = list(start)
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
        for candidate in _imports(text, rel):
            if candidate in owner_of:
                for owner in owner_of[candidate]:
                    reached.setdefault(owner, candidate)
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
        for test_num in task.verified_by:
            test_task = tasks.get(test_num)
            if not test_task:
                continue
            reached = _owners_reached(_test_files(test_task, project_dir, config), project_dir, owner_of)
            for owner, module in sorted(reached.items()):
                if owner != num and owner in tasks and num in _ancestors(tasks, owner):
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


def lint_spec(spec_dir: Path, project_dir: Path) -> Dict:
    tasks = {t.num: t for t in parse_tasks(spec_dir / "tasks.md")}
    cycles = dependency_cycles(tasks)
    return {
        "cycles": cycles,
        "verifyCycles": [] if cycles else verify_order_cycles(tasks, project_dir),
        "reasonless": reasonless_dependencies(tasks),
        "criticalPath": [] if cycles else critical_path(tasks),
    }
