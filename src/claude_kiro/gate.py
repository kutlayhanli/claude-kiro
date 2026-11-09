"""Verification gate: the definition of done for spec tasks.

A task may be marked Done only when:
- every acceptance checkbox in its tasks.md block is checked,
- the project checks pass: `verify_always`, the test-collection check, and
  either the full `verify` suite or, while a test-first spec is in progress,
  only the test suites that should already pass (see specs/ck.json verify_mode),
- the task's own **Verify:** commands pass, plus those of the test tasks that
  verify it,
- no existing test was deleted or weakened on this branch unless a test-track
  task owns that file,
- every correctness property (P-n in test-plan.md's ## Properties section) is
  claimed by a test file's PROPERTIES map: a warning by default, a failure
  with `"properties": "required"` in specs/ck.json.

Test-track tasks are written before the code they test, so their tests are
expected to fail until the implementation lands. For them the gate requires
the test files to exist and contain test cases, and only requires their Verify
commands to pass once every task they verify is Done.

Used by `ck gate` and by the done-verify hook when a task is marked Done.
"""

import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from claude_kiro import properties as props
from claude_kiro import verified
from claude_kiro import xdist
from claude_kiro.attribution import Attribution, AttributionIndex, attribute_failure, failure_key
from claude_kiro.config import collect_command, load_config
from claude_kiro.failures import Failure, failing_files, parse_short_summary, read_report
from claude_kiro.graph import build_graph
from claude_kiro.hooks._shared import git_utils
from claude_kiro.hooks._shared.spec_parser import SpecTask, parse_tasks
from claude_kiro.hooks._shared.test_heuristics import is_test_path, measure, weakening

OUTPUT_TAIL = 40  # lines of failing command output to include in the report
PLUGIN_DIR = Path(__file__).parent / "resources" / "pytest_plugin"  # ck_pytest_report.py, loaded with -p
PLUGIN_NAME = "ck_pytest_report"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    warning: bool = False  # passed, but worth telling me about


@dataclass
class CommandRun:
    check: Check
    failures: List[Failure] = field(default_factory=list)


@dataclass
class GateResult:
    spec_dir: Path
    tasks: List[str]
    checks: List[Check] = field(default_factory=list)
    failures: List[Failure] = field(default_factory=list)  # one per failing node or file, never truncated (3.3)
    attributions: List[Attribution] = field(default_factory=list)  # parallel to `failures` when attributed
    command_providers: Dict[str, List[str]] = field(default_factory=dict)  # known command -> providing tasks
    attribute: bool = False
    warnings: List[str] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def _missing_command(self, failure: Failure) -> Optional[Dict[str, Any]]:
        command = failure.missing_command
        if not command or command not in self.command_providers:
            return None
        return {"command": command, "providers": list(self.command_providers[command])}

    def to_json(self) -> Dict[str, Any]:
        """The `ck-gate/1` object (`ck gate --json`). Failure entries are never truncated or dropped."""
        entries = []
        for i, failure in enumerate(self.failures):
            attr = self.attributions[i] if i < len(self.attributions) else None
            entry = failure.as_json(
                tc_id=attr.tc_id if attr else None,
                tasks=attr.tasks if attr else (),
                attributed_by=attr.by if attr else None,
            )
            entry["missingCommand"] = self._missing_command(failure)
            entries.append(entry)
        return {
            "schema": "ck-gate/1",
            "spec": self.spec_dir.name,
            "tasks": list(self.tasks),
            "ok": self.ok,
            "attribute": self.attribute,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail, "warning": c.warning} for c in self.checks],
            "failures": entries,
            "summary": {
                "checks": len(self.checks),
                "failedChecks": sum(1 for c in self.checks if not c.ok),
                "failures": len(entries),
            },
            "warnings": list(self.warnings),
            "durationS": round(self.duration_s, 3),
        }

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


def _direct_pytest(command: str) -> bool:
    """True when the command is one pytest invocation (possibly behind uv or env): the only kind that gets the plugin.

    Wrappers such as tox, make or shell scripts are not direct: they get nothing injected, and their failures come
    from the output (pytest's short summary, else the failing file names).
    """
    try:
        segments = xdist._segments(xdist._tokens(command))
    except ValueError:
        return False
    return len(segments) == 1 and xdist._segment_pytest(segments[0]) is not None


def _known_files(project_dir: Path) -> List[str]:
    return git_utils.project_files(project_dir) or []


def _run_command(command: str, project_dir: Path, timeout: int, env: Optional[Dict[str, str]],
                 report_dir: Path) -> CommandRun:
    """Run one verify command and parse its failures. The command text is never rewritten.

    For a direct pytest invocation the child environment (only) gets the plugin: CK_GATE_REPORT names a fresh
    report file in `report_dir`, CK_GATE_ROOT is the gate's checkout, PYTHONPATH finds the plugin and
    PYTEST_ADDOPTS loads it. The plugin removes these for nested runs (TC-191).
    """
    name = f"`{command}`"
    run_env = dict(os.environ if env is None else env)
    report: Optional[Path] = None
    if _direct_pytest(command):
        report = report_dir / f"run-{os.getpid()}-{time.monotonic_ns()}.jsonl"
        run_env["CK_GATE_REPORT"] = str(report)
        run_env["CK_GATE_ROOT"] = str(project_dir)
        run_env["PYTHONPATH"] = os.pathsep.join(p for p in (str(PLUGIN_DIR), run_env.get("PYTHONPATH", "")) if p)
        run_env["PYTEST_ADDOPTS"] = " ".join(p for p in (f"-p {PLUGIN_NAME}", run_env.get("PYTEST_ADDOPTS", "")) if p)
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=project_dir,
            env=run_env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",  # a command that prints binary must not crash the gate
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        timeout_failure = Failure(
            node=None, file=None, kind="timeout", message=f"timed out after {timeout}s", truncated=False,
            command=command, exc_type=None, phase=None, crash_path=None, crash_line=None, detail="",
            missing_command=None,
        )
        return CommandRun(Check(name, False, f"timed out after {timeout}s"), [timeout_failure])
    if proc.returncode == 0:
        return CommandRun(Check(name, True), [])
    output = proc.stdout + proc.stderr
    tail = "\n".join(output.strip().splitlines()[-OUTPUT_TAIL:])
    found: List[Failure] = read_report(report, command) if report is not None else []
    if not found:
        found = parse_short_summary(output, command)
    if not found:
        found = failing_files(output, _known_files(project_dir), command)
    return CommandRun(Check(name, False, f"exit {proc.returncode}\n{tail}"), found)


def _attribute(result: GateResult, project_dir: Path, spec_dir: Path, tasks: Dict[str, SpecTask]) -> None:
    """Fill each failure's TC id and attributed tasks from the spec graph, and the known commands' providers.

    Without `--attribute` no status is given: that is the attributed gate's job (Task 20).
    """
    if not result.failures:
        return
    try:
        graph = build_graph(project_dir, spec_dir)
        index = AttributionIndex.from_graph(graph, tasks)
    except Exception as exc:  # attribution is best effort; the failure list itself stays complete
        result.warnings.append(f"attribution unavailable: {exc}")
        result.attributions = [Attribution(None, (), "none", True, (), ())] * len(result.failures)
        return
    providers: Dict[str, List[str]] = {}
    known = {n.label for n in graph.nodes if n.kind == "command"}
    for edge in graph.edges:
        if edge.kind == "provides" and edge.dst.startswith("cmd:"):
            providers.setdefault(edge.dst[len("cmd:"):], []).append(edge.src[len("task:"):])
    result.command_providers = {cmd: sorted(providers.get(cmd, []), key=int) for cmd in known}
    done = {n for n, t in tasks.items() if t.done}
    gated = list(result.tasks)
    attributions = [attribute_failure(f, index, done, gated) for f in result.failures]
    result.attributions = attributions
    for attr in attributions:
        for warning in attr.warnings:
            if warning not in result.warnings:
                result.warnings.append(warning)


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
    empty = []  # helpers (harnesses, fakes, plugins) under a test dir hold no test cases themselves
    for rel in test_files:
        path = project_dir / rel
        if not path.is_file():
            problems.append(f"{rel}: missing")
        elif rel.endswith("conftest.py") or "fixtures" in rel:
            continue
        elif measure(path.read_text(errors="replace")).tests == 0:
            empty.append(rel)
    if empty and len(empty) == len([f for f in test_files if not (f.endswith("conftest.py") or "fixtures" in f)]):
        problems += [f"{rel}: no test cases found" for rel in empty]
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


MAX_SCAN_BYTES = 2_000_000
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target", ".tox"}


def _test_files(project_dir: Path, config: Dict[str, Any], listed: Iterable[str]) -> List[str]:
    """Test files to scan for PROPERTIES maps: those tasks list, plus every test file in the project."""
    files = git_utils.project_files(project_dir)
    if files is None:  # not a git repository
        files = []
        for root, dirs, names in os.walk(project_dir):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            files.extend((Path(root) / n).relative_to(project_dir).as_posix() for n in names)
    candidates = dict.fromkeys(list(listed) + [f for f in files if not _SKIP_DIRS.intersection(Path(f).parts[:-1])])
    return [f for f in candidates if is_test_path(f, config)]


def _property_coverage(project_dir: Path, files: Iterable[str]) -> tuple:
    """(ids covered, {id: [dangling test names]}) across the PROPERTIES maps of `files`."""
    ok, dangling = set(), {}
    for rel in files:
        path = project_dir / rel
        try:
            if not path.is_file() or path.stat().st_size > MAX_SCAN_BYTES:
                continue
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if "PROPERTIES" not in text:
            continue
        for pid, missing_names in props.covered(text).items():
            if missing_names:
                dangling.setdefault(pid, []).extend(f"{n} (named in {rel}, no such test there)" for n in missing_names)
            else:
                ok.add(pid)
    return ok, {pid: names for pid, names in dangling.items() if pid not in ok}


MAP_HINT = (
    'Add each to the PROPERTIES map of the test file that checks it, e.g. '
    'PROPERTIES = {"P-3": "test_name"}. Never drop a property from test-plan.md to get a pass.'
)


def _properties_checks(
    project_dir: Path,
    spec_dir: Path,
    tasks: Dict[str, SpecTask],
    selected: List[SpecTask],
    whole_spec: bool,
    assumed_done: set,
    config: Dict[str, Any],
) -> List[Check]:
    mode = str(config.get("properties", "warn")).lower()
    if mode == "off":
        return []
    required = mode == "required"
    ids = props.plan_properties(spec_dir / "test-plan.md")
    checks: List[Check] = []

    # A test task claiming Done must map the properties it lists, in its own files.
    for task in selected:
        if task.track != "test" or not task.properties:
            continue
        files = [f for f in task.files if is_test_path(f, config)]
        ok, dangling = _property_coverage(project_dir, files)
        missing = [p for p in task.properties if p not in ok]
        detail = ""
        if missing:
            detail = f"not in a PROPERTIES map of {', '.join(files) or 'its listed test files'}: {', '.join(missing)}"
            detail += "".join(f"\n{pid} -> {n}" for pid in missing for n in dangling.get(pid, []))
            detail += "\n" + MAP_HINT
        checks.append(Check(f"Task {task.num} properties mapped", not (missing and required), detail, warning=bool(missing)))

    if not whole_spec:
        return checks
    if ids is None:
        if required and (spec_dir / "test-plan.md").exists():
            checks.append(
                Check(
                    "Properties covered",
                    False,
                    'test-plan.md has no ## Properties section, and specs/ck.json sets "properties": "required". '
                    "Add one (a P-n row per universally quantified criterion, or \"None\" with the reason).",
                )
            )
        return checks
    if not ids:
        checks.append(Check("Properties covered", True))
        return checks

    listed = [f for t in tasks.values() for f in t.files]
    ok, dangling = _property_coverage(project_dir, _test_files(project_dir, config, listed))
    owners: Dict[str, List[str]] = {}
    for t in tasks.values():
        for pid in t.properties:
            owners.setdefault(pid, []).append(t.num)
    missing, pending = [], []
    for pid in ids:
        if pid in ok:
            continue
        owner = owners.get(pid, [])
        if owner and not any(n in assumed_done for n in owner):
            pending.append(f"{pid} (Task {', '.join(owner)})")
        else:
            missing.append(pid)
    lines = []
    if missing:
        lines.append("in no test file's PROPERTIES map: " + ", ".join(missing))
        lines.extend(f"{pid} -> {n}" for pid in missing for n in dangling.get(pid, []))
        unowned = [pid for pid in missing if pid not in owners]
        if unowned:
            lines.append(f"no test task lists {', '.join(unowned)} under **Properties:**")
        lines.append(MAP_HINT)
    if pending:
        lines.append("not written yet, owned by unfinished test tasks: " + ", ".join(pending))
    checks.append(
        Check(
            f"Properties covered ({len(ids) - len(missing) - len(pending)}/{len(ids)})",
            not (missing and required),
            "\n".join(lines),
            warning=bool(missing or pending),
        )
    )
    return checks


def run_gate(
    project_dir: Path,
    spec_dir: Path,
    task_nums: Optional[List[str]] = None,
    config: Optional[Dict[str, Any]] = None,
    reuse: bool = False,
    stamp_by: str = "ck gate",
) -> GateResult:
    """Verify the given tasks (default: every Done task in the spec).

    A task-scoped gate that passes stamps the working tree it checked (see verified.py). With
    `reuse`, a task-scoped gate whose tree already passed the same commands skips running them;
    acceptance, test-file, property and tamper checks always run. Whole-spec gates never reuse,
    and callers that gate merged trees (landing) leave `reuse` off.

    The gate registers itself in the per-user gate registry for its whole run, and each verify
    command gets PYTEST_XDIST_AUTO_NUM_WORKERS = cores // gates running unless it is already set
    (see xdist.py). Commands are never rewritten.
    """
    started = time.monotonic()
    registry = xdist.registry_dir(os.environ)
    errors: List[str] = []
    with tempfile.TemporaryDirectory(prefix="ck-gate-") as report_dir, xdist.gate_slot(registry, errors) as slot:
        result = _run_gate(project_dir, spec_dir, task_nums, config, reuse, stamp_by,
                           registry if slot else None, errors, Path(report_dir))
    result.duration_s = time.monotonic() - started
    return result


def _run_gate(
    project_dir: Path,
    spec_dir: Path,
    task_nums: Optional[List[str]],
    config: Optional[Dict[str, Any]],
    reuse: bool,
    stamp_by: str,
    registry: Optional[Path],
    errors: List[str],
    report_dir: Path,
) -> GateResult:
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
    # A pass of the full suite on a clean tree is stamped for safe-merge to reuse.
    full_suite = list(config.get("verify") or [])
    clean = verified.clean_tree(project_dir) if full_suite and all(c in commands for c in full_suite) else None
    started = time.monotonic()
    collect = collect_command(config)
    to_run = ([collect] if collect else []) + [c for c in dict.fromkeys(commands) if c != collect]  # dedupe, keep order
    # A task gate's pass is stamped by the working tree it checked, so the next task gate on the
    # same tree (the agent's own `ck gate --task` right after the done-verify hook's) can skip the run.
    task_tree = verified.worktree_tree(project_dir) if task_scoped and to_run else None
    reused = verified.lookup_stamp(project_dir, task_tree, [to_run], scope="task") if reuse and task_tree else None
    if reused:
        result.checks.append(
            Check(
                f"Checks reused: this tree passed the same {len(to_run)} command(s)",
                True,
                f"tree {task_tree[:12]} passed them at {reused.get('at', '?')} ({reused.get('by', '?')}, "
                f"{reused.get('duration_s', '?')}s); nothing changed since. `ck gate --no-reuse` runs them again.",
                warning=True,
            )
        )
        for command in to_run:
            name = f"Test collection succeeds (`{command}`)" if command == collect else f"`{command}`"
            result.checks.append(Check(name, True))
    else:
        noted = [False]

        def note_unusable(reason: str) -> None:
            if not noted[0]:
                noted[0] = True
                print(
                    f"ck gate: gate registry {xdist.registry_dir(os.environ)} unusable ({reason}); "
                    "counting only this gate for xdist workers",
                    file=sys.stderr,
                    flush=True,
                )

        if registry is None:
            note_unusable(errors[0] if errors else "could not register this gate")
        failure_keys: set = set()
        for command in to_run:
            if registry is not None and xdist.running_gates(registry) is None:
                note_unusable("could not read it")
                registry = None
            run = _run_command(command, project_dir, timeout, xdist.verify_env(os.environ, registry), report_dir)
            check = run.check
            if command == collect:
                check.name = f"Test collection succeeds (`{collect}`)"
                if not check.ok:
                    check.detail += "\n(a test file that cannot be imported breaks the suite for every task; import project modules inside test bodies)"
            result.checks.append(check)
            for failure in run.failures:  # one entry per failing node, however many commands ran it
                if failure_key(failure) not in failure_keys:
                    failure_keys.add(failure_key(failure))
                    result.failures.append(failure)

    result.checks.extend(
        _properties_checks(project_dir, spec_dir, tasks, selected, task_nums is None, assumed_done, config)
    )

    owned = {f for t in tasks.values() if t.track == "test" for f in t.files}
    result.checks.append(_tamper(project_dir, config, owned))
    if clean and result.ok and verified.tracked_unchanged(project_dir, clean):
        verified.write_stamp(project_dir, clean, full_suite, "ck gate", int(time.monotonic() - started))
    if task_tree and not reused and result.ok and verified.worktree_tree(project_dir) == task_tree:
        verified.write_stamp(project_dir, task_tree, to_run, stamp_by, int(time.monotonic() - started), scope="task")
    _attribute(result, project_dir, spec_dir, tasks)
    return result
