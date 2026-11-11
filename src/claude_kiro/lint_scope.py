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

from dataclasses import dataclass
from typing import Any, Mapping, Set, Tuple

from claude_kiro.hooks._shared.spec_parser import SpecTask


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
