"""Verification gate: the definition of done for spec tasks.

A task may be marked Done only when:
- every acceptance checkbox in its tasks.md block is checked,
- the project checks pass: `verify_always`, the test-collection check, and
  either the full `verify` suite or, while a test-first spec is in progress,
  only the test suites that should already pass (see specs/ck.json verify_mode),
- the task's own **Verify:** commands pass, plus those of the test tasks that
  verify it,
- no existing test was deleted or weakened on this branch unless a test-track
  task owns that file.

Test-track tasks are written before the code they test, so their tests are
expected to fail until the implementation lands. For them the gate requires
the test files to exist and contain test cases, and only requires their Verify
commands to pass once every task they verify is Done.

Used by `ck gate` and by the Stop/SubagentStop hook.
"""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from claude_kiro.config import collect_command, load_config
from claude_kiro.hooks._shared import git_utils
from claude_kiro.hooks._shared.spec_parser import SpecTask, parse_tasks
from claude_kiro.hooks._shared.test_heuristics import is_test_path, measure, weakening

OUTPUT_TAIL = 40  # lines of failing command output to include in the report


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    warning: bool = False  # passed, but worth telling me about


@dataclass
class GateResult:
    spec_dir: Path
    tasks: List[str]
    checks: List[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def report(self) -> str:
        label = ", ".join(f"Task {n}" for n in self.tasks) or "spec"
        lines = [f"Verification gate for {self.spec_dir.name} ({label}): {'PASS' if self.ok else 'FAIL'}"]
        for check in self.checks:
            mark = "✓" if check.ok else "✗"
            if check.ok and check.warning:
                mark = "⚠"
            lines.append(f"{mark} {check.name}")
            if check.detail and (not check.ok or check.warning):
                lines.extend(f"    {line}" for line in check.detail.rstrip().splitlines())
        return "\n".join(lines)


def _run(command: str, project_dir: Path, timeout: int) -> Check:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return Check(f"`{command}`", False, f"timed out after {timeout}s")
    if proc.returncode == 0:
        return Check(f"`{command}`", True)
    output = (proc.stdout + proc.stderr).strip().splitlines()
    tail = "\n".join(output[-OUTPUT_TAIL:])
    return Check(f"`{command}`", False, f"exit {proc.returncode}\n{tail}")


def _acceptance(task: SpecTask) -> Check:
    open_items = [text for checked, text in task.acceptance if not checked]
    if not task.acceptance:
        return Check(f"Task {task.num} acceptance criteria", True, "no checkboxes listed", warning=True)
    if open_items:
        return Check(
            f"Task {task.num} acceptance criteria",
            False,
            "unchecked:\n" + "\n".join(f"- [ ] {t}" for t in open_items),
        )
    return Check(f"Task {task.num} acceptance criteria", True)


def _test_files_present(task: SpecTask, project_dir: Path, config: Dict[str, Any]) -> Check:
    name = f"Task {task.num} test files exist and contain tests"
    test_files = [f for f in task.files if is_test_path(f, config)]
    if not test_files:
        return Check(name, False, "a test-track task must list its test files under **Files:**")
    problems = []
    for rel in test_files:
        path = project_dir / rel
        if not path.is_file():
            problems.append(f"{rel}: missing")
        elif rel.endswith("conftest.py") or "fixtures" in rel:
            continue
        elif measure(path.read_text(errors="replace")).tests == 0:
            problems.append(f"{rel}: no test cases found")
    return Check(name, not problems, "\n".join(problems))


def _tamper(project_dir: Path, config: Dict[str, Any], owned: Iterable[str]) -> Check:
    name = "No existing tests deleted or weakened"
    if not git_utils.is_repo(project_dir):
        return Check(name, True, "not a git repository; tamper check skipped", warning=True)
    base = git_utils.base_ref(project_dir, config.get("base"))
    if not base:
        return Check(name, True, "no base commit; tamper check skipped", warning=True)

    owned = set(owned)
    problems = []
    for status, rel in git_utils.changed_files(project_dir, base):
        if status == "A" or not is_test_path(rel, config) or rel in owned:
            continue
        old = git_utils.show(project_dir, base, rel)
        if old is None:
            continue
        path = project_dir / rel
        if status == "D" or not path.exists():
            problems.append(f"{rel}: deleted")
            continue
        reasons = weakening(old, path.read_text(errors="replace"))
        if reasons:
            problems.append(f"{rel}: {', '.join(reasons)}")
    detail = "\n".join(problems)
    if problems:
        detail += (
            f"\n(compared with {base[:10]}. If a test is genuinely wrong, stop and tell me; "
            "don't change it to make the task pass.)"
        )
    return Check(name, not problems, detail)


def run_gate(
    project_dir: Path,
    spec_dir: Path,
    task_nums: Optional[List[str]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> GateResult:
    """Verify the given tasks (default: every Done task in the spec)."""
    config = config or load_config(project_dir)
    tasks = {t.num: t for t in parse_tasks(spec_dir / "tasks.md")}
    if task_nums is None:
        selected = [t for t in tasks.values() if t.done]
    else:
        selected = [tasks[n] for n in task_nums if n in tasks]

    result = GateResult(spec_dir, [t.num for t in selected])
    missing = [n for n in (task_nums or []) if n not in tasks]
    if missing:
        result.checks.append(Check("Tasks exist", False, f"not found in tasks.md: {', '.join(missing)}"))
    if not selected:
        return result

    commands: List[str] = []
    impl_selected = [t for t in selected if t.track != "test"]

    # Gating a task means claiming it is Done, so count it as Done when
    # deciding which test suites must already pass.
    assumed_done = {n for n, t in tasks.items() if t.done} | {t.num for t in selected}

    def suite_due(test_task: SpecTask) -> bool:
        targets = [n for n in test_task.verifies if n in tasks]
        return bool(targets) and all(n in assumed_done for n in targets)

    has_test_track = any(t.track == "test" for t in tasks.values())
    spec_complete = all(n in assumed_done for n in tasks)
    green_only = str(config.get("verify_mode", "auto")).lower() == "auto" and has_test_track and not spec_complete

    commands.extend(config.get("verify_always", []))
    # A task gate (--task) checks that task: its own Verify and its verifying
    # tests (added below). Re-running every other finished suite on every task
    # is the whole-spec gate's job; on a large spec that sweep made each task
    # gate run dozens of suites. task_regression: true restores it.
    task_scoped = task_nums is not None and not config.get("task_regression", False)
    if impl_selected:
        if green_only and task_scoped:
            if config.get("verify"):
                result.checks.append(
                    Check(
                        "Verify mode: this task's tests",
                        True,
                        "test-first spec in progress: running this task's Verify and its verifying tests; "
                        f"`ck gate {spec_dir.name}` (no --task) re-runs every suite that should already pass, "
                        f"and the full suite ({'; '.join(config['verify'])}) runs once every task is Done.",
                        warning=True,
                    )
                )
        elif green_only:
            due = [t for t in tasks.values() if t.track == "test" and suite_due(t)]
            for test_task in due:
                commands.extend(test_task.verify)
            pending = [t.num for t in tasks.values() if t.track == "test" and not suite_due(t)]
            if config.get("verify"):
                result.checks.append(
                    Check(
                        "Verify mode: tests that should already pass",
                        True,
                        f"test-first spec in progress: running the suites of {len(due)} test task(s) whose "
                        f"tasks are all Done; skipping {len(pending)} still waiting on unfinished tasks "
                        f"(Tasks {', '.join(pending) or '-'}). The full suite ({'; '.join(config['verify'])}) "
                        "runs once every task is Done.",
                        warning=True,
                    )
                )
        else:
            commands.extend(config.get("verify", []))

    for task in selected:
        result.checks.append(_acceptance(task))
        if task.track == "test":
            result.checks.append(_test_files_present(task, project_dir, config))
            if suite_due(task):
                commands.extend(task.verify)
            elif task.verify:
                result.checks.append(
                    Check(
                        f"Task {task.num} tests may still fail",
                        True,
                        "they verify tasks that are not Done yet; they must pass when those are",
                        warning=True,
                    )
                )
        else:
            commands.extend(task.verify)
            for num in task.verified_by:
                linked = tasks.get(num)
                if linked is None:
                    result.checks.append(Check(f"Task {task.num} verifying task {num}", False, "not found in tasks.md"))
                elif not linked.done:
                    result.checks.append(
                        Check(
                            f"Task {task.num} verifying task {num}",
                            False,
                            f"Task {num} ({linked.title}) must be Done first: its tests are this task's oracle",
                        )
                    )
                elif suite_due(linked):
                    commands.extend(linked.verify)
                else:
                    waiting = [n for n in linked.verifies if n in tasks and n not in assumed_done]
                    result.checks.append(
                        Check(
                            f"Task {num} tests not required yet",
                            True,
                            f"they also verify Task {', '.join(waiting)}, not Done yet; required once it is",
                            warning=True,
                        )
                    )

    if impl_selected and not commands and not config.get("verify"):
        result.checks.append(
            Check(
                "Verify commands configured",
                True,
                'nothing to run: add "verify" commands to specs/ck.json or **Verify:** lines to tasks',
                warning=True,
            )
        )

    timeout = int(config.get("verify_timeout", 540))
    collect = collect_command(config)
    if collect:
        check = _run(collect, project_dir, timeout)
        check.name = f"Test collection succeeds (`{collect}`)"
        if not check.ok:
            check.detail += "\n(a test file that cannot be imported breaks the suite for every task; import project modules inside test bodies)"
        result.checks.append(check)
    for command in dict.fromkeys(commands):  # dedupe, keep order
        if command != collect:
            result.checks.append(_run(command, project_dir, timeout))

    owned = {f for t in tasks.values() if t.track == "test" for f in t.files}
    result.checks.append(_tamper(project_dir, config, owned))
    return result
