"""Mechanical repair of a verify-order cycle in tasks.md (issue #6).

`ck lint` reports two shapes, both meaning impl Task X's gate can never pass before a later Task Y lands:

- verify-order: X is **Verified by** test Task T, and T's tests import a module that Y owns, Y
  depending (transitively) on X.
- verify-file: X's own **Verify** runs a test file that test Task T owns, and T verifies Y.

verify-order (the import is proven): X becomes a foundation task, and T's tests verify Y instead.
Concretely: drop T from X's **Verified by** (and X from T's **Verifies**), add T to Y's **Verified by**
(and Y to T's **Verifies**), and drop X's **Verify** commands that run T's files. It is mechanical only
when something still verifies X afterwards (a remaining Verify command or Verified-by task): with
nothing left, the gate would fall back to the full suite, which fails the same way, so the repair is
refused and a human decides.

verify-file (only the Verify line names T's file): X's Verify chose that file as its oracle, so the
missing piece is the link. T also verifies X and X is **Verified by** T: the gate then runs X's own
Verify and waits for Y only for T's whole suite, and the import-based verify-order check guards the
case where the file really needs Y once it exists.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from claude_kiro.hooks._shared.spec_parser import SpecTask, parse_tasks_text
from claude_kiro.lint import verify_order_cycles, verify_file_cycles, verify_paths

TASK_BLOCK = re.compile(r"^### Task ?(\d+): .+?(?=^### Task ?\d|^## |\Z)", re.DOTALL | re.MULTILINE)


@dataclass
class Relink:
    task: str
    fixable: bool
    reason: str
    cycles: List[Dict] = field(default_factory=list)
    verified_by: Dict[str, List[str]] = field(default_factory=dict)  # task -> new Verified by
    verifies: Dict[str, List[str]] = field(default_factory=dict)  # test task -> new Verifies
    verify: Dict[str, List[str]] = field(default_factory=dict)  # task -> new Verify commands

    def to_dict(self) -> Dict:
        return {"task": self.task, "fixable": self.fixable, "reason": self.reason,
                "cycles": [{"kind": c.get("kind", "verify-order"), "message": c.get("message")} for c in self.cycles],
                "verifiedBy": self.verified_by, "verifies": self.verifies, "verify": self.verify}


def _owned_by(t: SpecTask) -> set:
    return {f.strip().removeprefix("./") for f in t.files}


def plan_relink(tasks: Dict[str, SpecTask], project_dir: Path, config: dict, task: str) -> Relink:
    """The tasks.md edits that break every verify-order/verify-file cycle at `task`, or why not."""
    cycles = [c for c in verify_order_cycles(tasks, project_dir, config) + verify_file_cycles(tasks, config)
              if str(c.get("task")) == str(task)]
    if task not in tasks:
        return Relink(task, False, f"Task {task} is not in tasks.md")
    if not cycles:
        return Relink(task, False, f"Task {task} has no verify-order cycle", cycles)
    x = tasks[task]
    verified_by = {n: list(t.verified_by) for n, t in tasks.items()}
    verifies = {n: list(t.verifies) for n, t in tasks.items()}
    drop_files: set = set()
    for c in cycles:
        t_num = str(c.get("verifiedBy") or c.get("testTask"))
        y_num = str(c.get("needs"))
        if t_num not in tasks or y_num not in tasks:
            return Relink(task, False, f"the cycle names a task that is not in tasks.md ({c.get('message')})", cycles)
        if c.get("kind") == "verify-file":
            if t_num not in verified_by[task]:
                verified_by[task].append(t_num)
            if task not in verifies[t_num]:
                verifies[t_num].append(task)
            continue
        if t_num in verified_by[task]:
            verified_by[task].remove(t_num)
        if task in verifies[t_num]:
            verifies[t_num].remove(task)
        if t_num not in verified_by[y_num]:
            verified_by[y_num].append(t_num)
        if y_num not in verifies[t_num]:
            verifies[t_num].append(y_num)
        drop_files |= _owned_by(tasks[t_num])
    keep = [cmd for cmd in x.verify if not any(path in drop_files for path, _ in verify_paths(cmd, config))]
    if not keep and not verified_by[task]:
        return Relink(task, False, (f"after the relink nothing would verify Task {task} (its Verify only runs the "
                                     f"tests that need the later task): give it a smoke Verify by hand"), cycles)
    out = Relink(task, True, "relinked", cycles)
    out.verified_by = {n: v for n, v in verified_by.items() if v != tasks[n].verified_by}
    out.verifies = {n: v for n, v in verifies.items() if v != tasks[n].verifies}
    if keep != x.verify:
        out.verify = {task: keep}
    return out


def _refs(nums: List[str]) -> str:
    return ", ".join(f"Task {n}" for n in nums) if nums else "None"


def _set_field(block: str, name: str, value: str) -> str:
    line = re.compile(rf"^\*\*{re.escape(name)}:\*\*.*$", re.MULTILINE | re.IGNORECASE)
    if line.search(block):
        return line.sub(lambda _m: f"**{name}:** {value}", block, count=1)
    # Absent: put it after the Verify line, else after the Files list, else after the title line.
    for anchor in (r"^\*\*Verify:\*\*.*$", r"^\*\*Verified by:\*\*.*$", r"^\*\*Track:\*\*.*$"):
        m = re.search(anchor, block, re.MULTILINE | re.IGNORECASE)
        if m:
            return block[:m.end()] + f"\n**{name}:** {value}" + block[m.end():]
    first = block.index("\n") if "\n" in block else len(block)
    return block[:first] + f"\n**{name}:** {value}" + block[first:]


def apply_relink(text: str, r: Relink) -> str:
    """tasks.md text with the relink's field edits applied; other text is left byte-identical."""
    if not r.fixable:
        return text

    def edit(m: "re.Match") -> str:
        num, block = m.group(1), m.group(0)
        if num in r.verify:
            block = _set_field(block, "Verify", " ".join(f"`{c}`" for c in r.verify[num]) or "None")
        if num in r.verified_by:
            block = _set_field(block, "Verified by", _refs(r.verified_by[num]))
        if num in r.verifies:
            block = _set_field(block, "Verifies", _refs(r.verifies[num]))
        return block

    return TASK_BLOCK.sub(edit, text)


def relink_text(text: str, project_dir: Path, config: dict, task: str) -> "tuple[Optional[str], Relink]":
    tasks = {t.num: t for t in parse_tasks_text(text)}
    r = plan_relink(tasks, project_dir, config, str(task))
    return (apply_relink(text, r) if r.fixable else None), r


# --------------------------------------------------------------------------- command edges (12.2)


@dataclass
class CommandRelink:
    """Dependency edges that make a test task's command work: `adds` maps a task to the (provider task, command)
    pairs to add to its **Dependencies:**; `refused` lists the edges that would close a cycle."""

    adds: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    refused: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "adds": [{"task": t, "dependsOn": p, "command": c}
                     for t, pairs in sorted(self.adds.items(), key=lambda kv: int(kv[0]))
                     for p, c in pairs],
            "refused": self.refused,
        }


def _node_num(node: str) -> str:
    return node.split(":", 1)[-1]


def _reaches(deps: Dict[str, List[str]], src: str, dst: str) -> bool:
    seen: set = set()
    stack = list(deps.get(src, []))
    while stack:
        node = stack.pop()
        if node == dst:
            return True
        if node not in seen:
            seen.add(node)
            stack.extend(deps.get(node, []))
    return False


def plan_command_relink(graph, tasks: Dict[str, SpecTask]) -> CommandRelink:
    """The **Dependencies:** edges that `graph.missing_command_edges` calls for (12.1), one per (task, provider) pair.

    Edges are added one at a time against the dependencies already accepted, so a set of edges that only closes a
    cycle together is refused too. Refused edges are reported, not added.
    """
    from claude_kiro.graph import missing_command_edges

    deps: Dict[str, List[str]] = {n: [d for d in t.dependencies if d in tasks] for n, t in tasks.items()}
    wanted: Dict[Tuple[str, str], List[str]] = {}
    for m in missing_command_edges(graph):
        task, provider = _node_num(m.lacking), _node_num(m.provider)
        if task in tasks and provider in tasks and task != provider:
            wanted.setdefault((task, provider), []).append(m.command)
    out = CommandRelink()
    for (task, provider) in sorted(wanted, key=lambda k: (int(k[0]), int(k[1]))):
        command = sorted(set(wanted[(task, provider)]))[0]
        if _reaches(deps, provider, task):
            out.refused.append({
                "task": task,
                "dependsOn": provider,
                "command": command,
                "message": (f"Task {task} needs `{command}` from Task {provider}, but Task {provider} already depends "
                            f"on Task {task}: the edge would close a cycle"),
            })
            continue
        deps.setdefault(task, []).append(provider)
        out.adds.setdefault(task, []).append((provider, command))
    return out


def _add_dependencies(block: str, refs: List[str]) -> str:
    """The block with `refs` appended to its **Dependencies:** field (replacing None/n/a/-)."""
    match = re.search(r"^\*\*Dependencies:\*\*[ \t]*(.*)$", block, re.MULTILINE | re.IGNORECASE)
    if match is None:
        return _set_field(block, "Dependencies", ", ".join(refs))
    value = match.group(1).strip()
    if not value or value.lower().startswith(("none", "n/a", "-")):
        new = ", ".join(refs)
    else:
        new = f"{value}, {', '.join(refs)}"
    return block[:match.start()] + f"**Dependencies:** {new}" + block[match.end():]


def apply_command_relink(text: str, r: CommandRelink) -> str:
    """tasks.md text with the command edges added; other text is left byte-identical."""
    if not r.adds:
        return text

    def edit(m: "re.Match") -> str:
        num, block = m.group(1), m.group(0)
        pairs = r.adds.get(num)
        if not pairs:
            return block
        return _add_dependencies(block, [f"Task {p} (provides `{c}`)" for p, c in pairs])

    return TASK_BLOCK.sub(edit, text)
