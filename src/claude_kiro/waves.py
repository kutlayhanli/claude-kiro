"""Wave labels for a spec's tasks, for progress display and timing groups.

Waves don't schedule anything: /spec:implement and /spawn-worktree start a task
as soon as its own Dependencies are merged (`ck plan` "ready"). Specs written
before 0.7 have a "## Parallel Groups" section; it is used for the labels when
it is consistent (every task appears exactly once, every dependency sits in an
earlier wave), and an inconsistency is reported. Otherwise waves are computed
from **Dependencies:** (topological levels), splitting any level whose tasks
edit the same file.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from claude_kiro.hooks._shared.spec_parser import SpecTask, parse_tasks

WAVE_LABEL = re.compile(r"\bWave\s*(\d+)\b", re.IGNORECASE)


def _section(text: str, title: str) -> Optional[str]:
    match = re.search(rf"^##\s+{title}\s*$(.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return match.group(1) if match else None


def _numbers(segment: str) -> List[str]:
    # drop dependency notes like "(after 1)" or "(depends on Task 2)"
    segment = re.sub(r"\((?:after|depends?|needs?|requires?|once|blocked)\b[^)]*\)", " ", segment, flags=re.IGNORECASE)
    numbers: List[str] = []
    for start, end, single in re.findall(r"(\d+)\s*[-–]\s*(\d+)|(\d+)", segment):
        if single:
            numbers.append(single)
        else:
            numbers.extend(str(n) for n in range(int(start), int(end) + 1))
    return numbers


def parse_parallel_groups(text: str) -> Optional[List[List[str]]]:
    """Waves as listed in "## Parallel Groups", or None if absent or unreadable."""
    section = _section(text, "Parallel Groups")
    if not section:
        return None
    labels = list(WAVE_LABEL.finditer(section))
    if not labels:
        return None
    waves: Dict[int, List[str]] = {}
    for i, label in enumerate(labels):
        end = labels[i + 1].start() if i + 1 < len(labels) else len(section)
        waves.setdefault(int(label.group(1)), []).extend(_numbers(section[label.end():end]))
    return [waves[k] for k in sorted(waves)]


def _problems(waves: List[List[str]], tasks: Dict[str, SpecTask]) -> List[str]:
    problems = []
    seen: Dict[str, int] = {}
    for index, wave in enumerate(waves):
        for num in wave:
            if num not in tasks:
                problems.append(f"Task {num} is in Wave {index + 1} but not in the task list")
            elif num in seen:
                problems.append(f"Task {num} is listed in Wave {seen[num] + 1} and Wave {index + 1}")
            else:
                seen[num] = index
    missing = [n for n in tasks if n not in seen]
    if missing:
        problems.append(f"Tasks {', '.join(missing)} are in no wave")
    for num, index in seen.items():
        for dep in tasks[num].dependencies:
            if dep in seen and seen[dep] >= index:
                problems.append(f"Task {num} (Wave {index + 1}) depends on Task {dep} (Wave {seen[dep] + 1})")
    return problems


def compute_waves(tasks: Dict[str, SpecTask]) -> Tuple[List[List[str]], List[str]]:
    """Topological levels from Dependencies, split so no wave edits a file twice."""
    warnings = []
    level: Dict[str, int] = {}
    visiting = set()

    def depth(num: str) -> int:
        if num in level:
            return level[num]
        if num in visiting:
            raise ValueError(f"dependency cycle through Task {num}")
        visiting.add(num)
        deps = [d for d in tasks[num].dependencies if d in tasks]
        unknown = [d for d in tasks[num].dependencies if d not in tasks]
        if unknown:
            warnings.append(f"Task {num} depends on unknown Task {', '.join(unknown)}")
        level[num] = 1 + max((depth(d) for d in deps), default=-1)
        visiting.discard(num)
        return level[num]

    for num in tasks:
        depth(num)

    waves: List[List[str]] = []
    for lvl in range(max(level.values(), default=-1) + 1):
        pending = sorted((n for n, l in level.items() if l == lvl), key=int)
        while pending:  # greedy split on shared files
            wave, files, rest = [], set(), []
            for num in pending:
                task_files = set(tasks[num].files)
                if task_files & files:
                    rest.append(num)
                else:
                    wave.append(num)
                    files |= task_files
            waves.append(wave)
            pending = rest
    return waves, warnings


def plan_waves(spec_dir: Path) -> Dict:
    """{waves, deps, done, titles, tracks, source, warnings} for a spec directory."""
    tasks_md = spec_dir / "tasks.md"
    text = tasks_md.read_text(encoding="utf-8")
    tasks = {t.num: t for t in parse_tasks(tasks_md)}
    warnings: List[str] = []

    listed = parse_parallel_groups(text)
    if listed is not None and not _problems(listed, tasks):
        waves, source = listed, "parallel-groups"
    else:
        if listed is not None:
            warnings.extend(_problems(listed, tasks))
            warnings.append("Parallel Groups is inconsistent; waves computed from Dependencies instead")
        waves, more = compute_waves(tasks)
        warnings.extend(more)
        source = "dependencies"

    return {
        "spec": spec_dir.name,
        "waves": waves,
        "deps": {n: [d for d in t.dependencies if d in tasks] for n, t in tasks.items()},
        "done": [n for n, t in tasks.items() if t.done],
        "titles": {n: t.title for n, t in tasks.items()},
        "tracks": {n: t.track for n, t in tasks.items()},
        "source": source,
        "warnings": warnings,
    }
