"""Attribute gate failures to spec tasks through the spec graph (unattended-correctness Stories 1 and 2).

The index is built once per gate from the `SpecGraph` edges (`verifies` for TC -> task, `owns` for test task ->
file) plus the parsed task blocks for the `Verifies` fields and the Verify commands. Attribution never reads a
failure's message: only the node id's function name (TC ids), the node's file and its parameter ID (hint only).

Pure seams: `tc_of_node`, `param_owned_file`, `attribute_failure`, `rerun_plan`, `rerun_command`, `gate_verdict`.
Every output is canonical (sorted by command, then failure key; tasks sorted numerically), so the same inputs in
any order give the same output (1.10).
"""

import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from claude_kiro.failures import Failure

if TYPE_CHECKING:  # pragma: no cover
    from claude_kiro.graph import SpecGraph
    from claude_kiro.hooks._shared.spec_parser import SpecTask

RERUN_ARG_LIMIT = 24000  # characters of node ids per rerun command line, well under the OS argument limit

TC_TOKEN = re.compile(r"(?<![A-Za-z0-9])tc(\d+)_", re.IGNORECASE)


@dataclass(frozen=True)
class Attribution:
    tc_id: Optional[str]                 # "TC-12" when attributed by a TC id, else None
    tasks: Tuple[str, ...]               # attributed impl tasks, sorted numerically
    by: str                              # "tc" | "file" | "command" | "none"
    candidate: bool                      # regression candidate (not excused)
    excused_by: Tuple[str, ...]          # attributed tasks not in done ∪ gated, sorted numerically
    warnings: Tuple[str, ...]            # unknown TC id, attribution unavailable
    hint: Optional[str] = None           # by="none" only: the test task owning the file a parameter ID names


@dataclass(frozen=True)
class AttributionIndex:
    tc_tasks: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)        # "TC-n" -> impl tasks
    file_owner: Mapping[str, str] = field(default_factory=dict)                  # test file -> test task
    test_task_verifies: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)  # test task -> impl tasks
    command_owner: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)   # Verify command -> tasks

    @classmethod
    def from_graph(cls, graph: "SpecGraph", tasks: "Mapping[str, SpecTask]",
                   known_tcs: Optional[Iterable[str]] = None) -> "AttributionIndex":
        """`known_tcs` (e.g. "TC-3" for each test-plan row) limits TC attribution: any other TC id is unknown (1.1).

        `tasks` is the task-number map, or the parsed task list (`parse_tasks`) which is keyed here.
        """
        if not isinstance(tasks, Mapping):
            tasks = {task.num: task for task in tasks}
        selecting: Dict[str, Set[str]] = {}
        covering: Dict[str, Set[str]] = {}
        owners: Dict[str, Set[str]] = {}
        for edge in graph.edges:
            if edge.kind == "verifies" and edge.src.startswith("tc:") and edge.dst.startswith("task:"):
                bucket = selecting if edge.source == "Verify -k" else covering if edge.source == "Verifies" else None
                if bucket is not None:
                    bucket.setdefault(edge.src[len("tc:"):], set()).add(edge.dst[len("task:"):])
            elif edge.kind == "owns" and edge.src.startswith("task:") and edge.dst.startswith("file:"):
                num = edge.src[len("task:"):]
                task = tasks.get(num)
                if task is not None and task.track == "test":
                    owners.setdefault(edge.dst[len("file:"):], set()).add(num)

        # A TC is attributed to the tasks whose Verify -k selects it; only a TC no Verify selects falls back to
        # its test task's coverage (the Verifies edges), never the union of the two (1.1).
        known = None if known_tcs is None else set(known_tcs)
        tc_tasks: Dict[str, Tuple[str, ...]] = {}
        for tc in sorted(set(selecting) | set(covering)):
            chosen = selecting.get(tc) or covering.get(tc)
            if chosen and (known is None or tc in known):
                tc_tasks[tc] = _nums(chosen)
        file_owner = {path: min(nums, key=int) for path, nums in owners.items() if nums}
        test_task_verifies = {
            num: _nums(v for v in task.verifies if v in tasks)
            for num, task in tasks.items() if task.track == "test"
        }
        commands: Dict[str, Set[str]] = {}
        for num, task in tasks.items():
            for command in task.verify:
                commands.setdefault(command, set()).add(num)
        command_owner = {command: _nums(nums) for command, nums in commands.items()}
        return cls(tc_tasks=tc_tasks, file_owner=file_owner, test_task_verifies=test_task_verifies,
                   command_owner=command_owner)


# --------------------------------------------------------------------------- pure seams


def _nums(tasks: Iterable[str]) -> Tuple[str, ...]:
    return tuple(sorted(set(tasks), key=int))


def _function_name(node_id: str) -> str:
    # The parameter ID is cut first: it may itself hold "::" (`test_x[a::1]`). The file part (before the first
    # "::") may hold "[" (`odd dir [1]/test_odd.py`), so the cut is made after it.
    head, sep, rest = node_id.partition("::")
    if not sep:
        return head.split("[", 1)[0]
    return rest.split("[", 1)[0].split("::")[-1]


def tc_of_node(node_id: Optional[str]) -> Optional[str]:
    """The TC id named by the node's function name (`tcNNN_`, case-insensitive, number normalised), or None.

    Parameter IDs, class names, files and directories are never read (P-3). When several tokens appear, the
    smallest number is used.
    """
    if not node_id:
        return None
    numbers = [int(m.group(1)) for m in TC_TOKEN.finditer(_function_name(node_id))]
    return f"TC-{min(numbers)}" if numbers else None


def param_owned_file(node_id: Optional[str], file_owner: Mapping[str, str]) -> Optional[str]:
    """The owned test file a wrapper's parameter ID names: an exact path, or a unique basename, as one `/` part.

    Used for a hint only. It never gives tasks, a `by`, a status or an excuse (1.5).
    """
    if not node_id:
        return None
    last = node_id.split("::")[-1]
    if "[" not in last:
        return None
    start = last.index("[") + 1
    end = last.rfind("]")
    param = last[start:end] if end > start else last[start:]
    parts = [param] + [p for p in param.split("/") if p and p != param]
    for part in parts:
        if part in file_owner:
            return part
    for part in parts:
        matches = sorted(p for p in file_owner if p.rsplit("/", 1)[-1] == part)
        if len(matches) == 1:
            return matches[0]
    return None


def _finish(tc_id: Optional[str], tasks: Tuple[str, ...], by: str, finished: frozenset,
            warnings: List[str]) -> Attribution:
    excused = tuple(t for t in tasks if t not in finished)
    return Attribution(tc_id=tc_id, tasks=tasks, by=by, candidate=not excused, excused_by=excused,
                       warnings=tuple(warnings))


def attribute_failure(failure: Failure, index: AttributionIndex, done: Iterable[str],
                      gated: Iterable[str]) -> Attribution:
    """TC id -> its tasks (`by="tc"`); else the node's own file's test task (`by="file"`); else a command-level
    failure's command owners (`by="command"`); else unattributed (`by="none"`, a candidate, with a hint).
    """
    finished = frozenset(done) | frozenset(gated)
    warnings: List[str] = []
    node, file = failure.node, failure.file

    tc = tc_of_node(node)
    if tc is not None and tc in index.tc_tasks:
        return _finish(tc, _nums(index.tc_tasks[tc]), "tc", finished, warnings)
    if tc is not None:
        warnings.append(f"unknown TC id {tc} in {node}")

    if node is None and file is None:
        warnings.append(f"attribution unavailable for `{failure.command}`")
        tasks = _nums(index.command_owner.get(failure.command, ()))
        return _finish(None, tasks, "command", finished, warnings)

    owner = index.file_owner.get(file) if file else None
    if owner is not None:
        tasks = _nums(index.test_task_verifies.get(owner, ()))
        return _finish(tc, tasks, "file", finished, warnings)  # an unknown TC keeps its id; only `by` says file (1.8)

    hint = None
    path = param_owned_file(node, index.file_owner)
    if path is not None:
        hint = f"parameter names {path}, owned by test Task {index.file_owner[path]}"
    return Attribution(tc_id=None, tasks=(), by="none", candidate=True, excused_by=(), warnings=tuple(warnings),
                       hint=hint)


def failure_key(failure: Failure) -> str:
    """The node id; `collect:<file>` for a file-level failure; `command:<command>` for a command-level one."""
    if failure.node:
        return failure.node
    if failure.file:
        return f"collect:{failure.file}"
    return f"command:{failure.command}"


def _launcher(command: str) -> str:
    try:
        argv = shlex.split(command)
    except ValueError:
        return command
    for i, arg in enumerate(argv):
        if arg == "pytest" or arg.endswith("/pytest"):
            return " ".join(shlex.quote(a) for a in argv[: i + 1])
    return command


def rerun_command(prefix: str, nodes: Sequence[str], limit: int = RERUN_ARG_LIMIT) -> List[str]:
    """Rerun command lines: `prefix -q -p no:cacheprovider` plus quoted node ids, chunked under `limit`."""
    base = f"{prefix} -q -p no:cacheprovider"
    chunks: List[str] = []
    current: List[str] = []
    length = len(base)
    for node in nodes:
        quoted = shlex.quote(node)
        if current and length + 1 + len(quoted) > limit:
            chunks.append(base + " " + " ".join(current))
            current, length = [], len(base)
        current.append(quoted)
        length += 1 + len(quoted)
    if current:
        chunks.append(base + " " + " ".join(current))
    return chunks


@dataclass(frozen=True)
class RerunItem:
    key: str        # node id, or "collect:<file>" for a file-level failure
    command: str


def rerun_plan(pairs: Sequence[Tuple[Failure, Attribution]]) -> List[RerunItem]:
    """Each regression candidate exactly once, in canonical order. Excused failures are never rerun."""
    best: Dict[str, Tuple[Tuple[str, str], Failure]] = {}
    for failure, attr in pairs:
        if not attr.candidate:
            continue
        key = failure_key(failure)
        order = (failure.command, key)
        if key not in best or order < best[key][0]:
            best[key] = (order, failure)
    items: List[RerunItem] = []
    for key, (order, failure) in sorted(best.items(), key=lambda kv: kv[1][0]):
        if failure.node or failure.file:
            target = failure.node if failure.node else failure.file
            command = rerun_command(_launcher(failure.command), [target])[0]
        else:
            command = failure.command
        items.append(RerunItem(key=key, command=command))
    return items


@dataclass(frozen=True)
class GateVerdict:
    red: bool
    statuses: Mapping[str, str]   # failure key -> regression | excused | flaky | unattributed
    counts: Mapping[str, int]     # regressions, excused, flaky, unattributed


def gate_verdict(pairs: Sequence[Tuple[Failure, Attribution]], reruns: Mapping[str, bool],
                 other_checks_ok: bool) -> GateVerdict:
    """`reruns` maps a failure key to whether its rerun passed. Red iff a regression, an unattributed failure or
    another check failed."""
    statuses: Dict[str, str] = {}
    for failure, attr in pairs:
        key = failure_key(failure)
        if not attr.candidate:
            status = "excused"
        elif reruns.get(key, False):
            status = "flaky"
        else:
            status = "regression" if attr.tasks else "unattributed"
        statuses[key] = status
    statuses = dict(sorted(statuses.items()))
    values = list(statuses.values())
    counts = {
        "regressions": values.count("regression"),
        "excused": values.count("excused"),
        "flaky": values.count("flaky"),
        "unattributed": values.count("unattributed"),
    }
    red = counts["regressions"] + counts["unattributed"] > 0 or not other_checks_ok
    return GateVerdict(red=red, statuses=statuses, counts=counts)
