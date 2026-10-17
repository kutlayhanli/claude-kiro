"""`ck graph`: the spec's traceability graph, checked for completeness and against an inferred graph.

The lists stay the source of truth (requirements.md, design.md, test-plan.md, tasks.md). This
module reads the links they already declare into one graph:

    design element --designs--> criterion      (design.md Requirements Traceability table)
    TC-n / P-n     --covers---> criterion      (test-plan.md Criterion / Requirement columns)
    impl task      --implements-> criterion    (tasks.md **Requirements:**)
    test task      --tests----> criterion      (tasks.md **Requirements:**)
    test task      --contains-> TC-n / P-n     (TC ids in the block, **Properties:**)
    impl task      --verified_by-> test task   (**Verified by:**; **Verifies:** the other way)
    task           --depends_on-> task         (**Dependencies:**)

and checks it for completeness (every criterion designed, covered by a case or property, implemented
and tested by a task; no dangling references; Verified by / Verifies symmetric).

The spec-create workflow's Graph Gate also has an agent build an *inferred* graph from the content
alone, with the declared links stripped out (`blind_dossier`), and `diff_graphs` compares the two:
a declared link the content doesn't support is likely wrong, an inferred link nobody declared is
likely missing traceability. `graph.json` is derived: rebuilt by each gate, never edited by hand.
`ck lint` warns when the lists changed after it was written.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from claude_kiro.hooks._shared.spec_parser import _field, parse_tasks_text

GRAPH_FILE = "graph.json"
SOURCES = ("requirements.md", "design.md", "test-plan.md", "tasks.md")

STORY = re.compile(r"^###\s+Story\s+(\d+)\s*[:.-]?\s*(.*)$", re.MULTILINE)
CRITERION_LINE = re.compile(r"^\s*[-*]\s+(?:\*\*)?(\d+\.\d+)(?:\*\*)?[.:)]?\s+(.+)$", re.MULTILINE)
TASK_BLOCK = re.compile(r"^### Task ?(\d+): (.+?)(?=^### Task ?\d|^## |\Z)", re.DOTALL | re.MULTILINE)
RANGE = re.compile(r"\b(\d+)\.(\d+)\s*(?:-|–|—|to)\s*(?:(\d+)\.)?(\d+)\b")
SINGLE = re.compile(r"\b(\d+)\.(\d+)\b")
STORY_REF = re.compile(r"\bStory\s+(\d+)\b|\b(\d+)\.[xX*]\b", re.IGNORECASE)
CASE_ID = re.compile(r"\bTC-\d+\b")
PROP_ID = re.compile(r"\bP-\d+\b")
REF_FIELDS = ("Requirements", "Verified by", "Verifies", "Dependencies", "Properties", "Status")

# Edge kinds whose target is a criterion: the links the inferred graph is compared on.
CRITERION_EDGES = ("designs", "covers", "implements", "tests")


def _sha(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def story_id(num: str) -> str:
    return f"Story {num}"


def task_id(num: str) -> str:
    return f"Task {num}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_requirements(text: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """({story number: title}, {criterion id: text}) from requirements.md."""
    stories = {m.group(1): m.group(2).strip() for m in STORY.finditer(text)}
    criteria: Dict[str, str] = {}
    for m in CRITERION_LINE.finditer(text):
        criteria.setdefault(m.group(1), m.group(2).strip())
    for cid in criteria:
        stories.setdefault(cid.split(".")[0], "")
    return stories, criteria


def criterion_refs(text: str, known: Iterable[str]) -> Tuple[List[str], List[str]]:
    """(criterion ids, dangling ids) referenced in a cell or field.

    Understands "1.1, 1.2", ranges "2.1-2.3" / "2.1–3", and story-level references "Story 4" or
    "4.x", which expand to every known criterion of that story. A number like 3.10 that no story
    has is dangling; one whose story exists but criterion doesn't is dangling too. Parenthesized
    text is a reason, as in tasks.md reference fields, so "1.1 (within 0.5 s)" cites 1.1 only.
    """
    known = list(known)
    known_set = set(known)
    found: List[str] = []
    dangling: List[str] = []
    text = re.sub(r"\([^)]*\)", " ", text or "")

    def add(cid: str) -> None:
        if cid in known_set:
            found.append(cid)
        else:
            dangling.append(cid)

    consumed = text
    for m in RANGE.finditer(text):
        story, start, end_story, end = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
        if end_story and end_story != story:
            continue
        for n in range(start, end + 1):
            add(f"{story}.{n}")
        consumed = consumed.replace(m.group(0), " ", 1)
    for m in STORY_REF.finditer(consumed):
        story = m.group(1) or m.group(2)
        members = [c for c in known if c.split(".")[0] == story]
        if members:
            found.extend(members)
        else:
            dangling.append(story_id(story))
        consumed = consumed.replace(m.group(0), " ", 1)
    for m in SINGLE.finditer(consumed):
        add(f"{m.group(1)}.{m.group(2)}")
    return list(dict.fromkeys(found)), list(dict.fromkeys(dangling))


def parse_tables(text: str) -> List[Tuple[Optional[str], List[str], List[List[str]]]]:
    """[(nearest heading above, header cells, rows)] for each markdown table."""
    tables = []
    heading: Optional[str] = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
        if line.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            header = _cells(line)
            rows = []
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            tables.append((heading, header, rows))
            continue
        i += 1
    return tables


def _cells(line: str) -> List[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", line)]


def _column(header: List[str], *patterns: str, exclude: Iterable[int] = ()) -> Optional[int]:
    for pattern in patterns:
        for i, name in enumerate(header):
            if i not in exclude and re.search(pattern, name, re.IGNORECASE):
                return i
    return None


def _cell(row: List[str], i: Optional[int]) -> str:
    return row[i] if i is not None and i < len(row) else ""


def _element_name(cell: str) -> str:
    return re.sub(r"\s+", " ", cell.replace("`", "").replace("**", "")).strip()


def parse_traceability(text: str) -> Optional[List[Tuple[str, str]]]:
    """[(criterion cell, design element)] from design.md's Requirements Traceability table, or None if absent."""
    for heading, header, rows in parse_tables(text):
        if not heading or "traceab" not in heading.lower():
            continue
        crit = _column(header, r"criter", r"req")
        elem = _column(header, r"design", r"element", r"component", r".", exclude=[crit] if crit is not None else [])
        if crit is None or elem is None:
            return []
        return [(_cell(r, crit), _element_name(_cell(r, elem))) for r in rows if _element_name(_cell(r, elem))]
    return None


def parse_test_plan(text: str) -> Tuple[Dict[str, Dict], Dict[str, Dict]]:
    """({TC id: {criteria cell, text}}, {P id: {criteria cell, text}}) from test-plan.md's tables."""
    cases: Dict[str, Dict] = {}
    props: Dict[str, Dict] = {}
    for _, header, rows in parse_tables(text):
        crit = _column(header, r"criter", r"req")
        for row in rows:
            first = row[0] if row else ""
            target = cases if CASE_ID.fullmatch(first) else props if PROP_ID.fullmatch(first) else None
            if target is None or first in target:
                continue
            content = [c for i, c in enumerate(row) if i not in (0, crit)]
            target[first] = {"criteria": _cell(row, crit), "text": " | ".join(c for c in content if c)}
    return cases, props


def _task_blocks(text: str) -> Dict[str, str]:
    return {m.group(1): m.group(0) for m in TASK_BLOCK.finditer(text)}


# ---------------------------------------------------------------------------
# The declared graph
# ---------------------------------------------------------------------------


def build_graph(spec_dir: Path) -> Dict:
    """The declared graph and its completeness issues. Never raises on a malformed spec: problems become issues."""
    spec_dir = Path(spec_dir)
    texts = {name: _read(spec_dir / name) for name in SOURCES}
    nodes: Dict[str, Dict] = {}
    edges: List[Dict] = []
    issues: List[Dict] = []

    def issue(code: str, severity: str, message: str, nodes_: Iterable[str] = ()) -> None:
        issues.append({"code": code, "severity": severity, "nodes": list(nodes_), "message": message})

    def edge(src: str, dst: str, kind: str, file: str) -> None:
        if not any(e["from"] == src and e["to"] == dst and e["kind"] == kind for e in edges):
            edges.append({"from": src, "to": dst, "kind": kind, "file": file})

    for name, text in texts.items():
        if text is None:
            issue("missing-file", "error", f"{name} is missing", [])

    stories, criteria = parse_requirements(texts["requirements.md"] or "")
    for num, title in stories.items():
        nodes[story_id(num)] = {"id": story_id(num), "kind": "story", "text": title}
    for cid, text in criteria.items():
        nodes[cid] = {"id": cid, "kind": "criterion", "story": story_id(cid.split(".")[0]), "text": text}
    if texts["requirements.md"] is not None and not criteria:
        issue("no-criteria", "error", "requirements.md has no numbered criteria (\"- N.M WHEN ..., THE SYSTEM SHALL ...\")")

    def link(src: str, cell: str, kind: str, file: str) -> List[str]:
        found, dangling = criterion_refs(cell, criteria)
        for cid in found:
            edge(src, cid, kind, file)
        for bad in dangling:
            issue("dangling-criterion", "error", f"{src} ({file}) cites {bad}, which requirements.md does not define", [src])
        return found

    # design.md
    trace = parse_traceability(texts["design.md"] or "") if texts["design.md"] is not None else None
    if texts["design.md"] is not None and trace is None:
        issue("no-traceability", "error", "design.md has no Requirements Traceability table")
    for cell, name in trace or []:
        nid = f"D:{name}"
        nodes.setdefault(nid, {"id": nid, "kind": "design", "text": name})
        link(nid, cell, "designs", "design.md")

    # test-plan.md
    cases, props = parse_test_plan(texts["test-plan.md"] or "")
    for kind, table in (("case", cases), ("property", props)):
        for nid, row in table.items():
            nodes[nid] = {"id": nid, "kind": kind, "text": row["text"]}
            if not link(nid, row["criteria"], "covers", "test-plan.md") and not criterion_refs(row["criteria"], criteria)[1]:
                issue(f"{kind}-no-criterion", "error", f"{nid} names no governing criterion", [nid])

    # tasks.md
    tasks_text = texts["tasks.md"] or ""
    tasks = {t.num: t for t in parse_tasks_text(tasks_text)}
    blocks = _task_blocks(tasks_text)
    for num, task in tasks.items():
        tid = task_id(num)
        nodes[tid] = {"id": tid, "kind": "task", "track": task.track, "text": task.title}
    for num, task in tasks.items():
        tid = task_id(num)
        block = blocks.get(num, "")
        reqs = link(tid, _field(block, "Requirements") or "", "tests" if task.track == "test" else "implements", "tasks.md")
        if not reqs and task.track != "test" and (task.verified_by or task.verifies):
            issue("task-no-requirements", "warning", f"{tid} has no **Requirements:** but takes part in verification", [tid])
        if task.track == "test":
            for ref in list(dict.fromkeys(CASE_ID.findall(block) + task.properties)):
                if ref in nodes:
                    edge(tid, ref, "contains", "tasks.md")
                else:
                    issue("dangling-test-ref", "error", f"{tid} lists {ref}, which test-plan.md does not define", [tid])
        for other, kind in [(n, "verified_by") for n in task.verified_by] + [(n, "verifies") for n in task.verifies] + [(n, "depends_on") for n in task.dependencies]:
            if other in tasks:
                edge(tid, task_id(other), kind, "tasks.md")
            else:
                issue("dangling-task", "error", f"{tid} **{kind.replace('_', ' ').capitalize()}:** Task {other}, which tasks.md does not define", [tid])

    _completeness(nodes, edges, tasks, criteria, texts, trace, issue)
    return {
        "spec": spec_dir.name,
        "nodes": list(nodes.values()),
        "edges": edges,
        "issues": issues,
        "sources": {name: _sha(spec_dir / name) for name in SOURCES},
    }


def _completeness(nodes, edges, tasks, criteria, texts, trace, issue) -> None:
    incoming: Dict[str, Set[str]] = {}
    for e in edges:
        incoming.setdefault(e["to"], set()).add(e["kind"] + ":" + nodes.get(e["from"], {}).get("kind", "?"))
    out: Dict[Tuple[str, str], Set[str]] = {}
    for e in edges:
        out.setdefault((e["from"], e["kind"]), set()).add(e["to"])

    for cid in criteria:
        kinds = incoming.get(cid, set())
        if trace and "designs:design" not in kinds:
            issue("criterion-no-design", "error", f"{cid} has no row in design.md's Requirements Traceability table", [cid])
        if texts["test-plan.md"] is not None and not ({"covers:case", "covers:property"} & kinds):
            issue("criterion-no-case", "error", f"{cid} is covered by no test case or property in test-plan.md", [cid])
        if texts["tasks.md"] is not None and "implements:task" not in kinds:
            issue("criterion-no-impl-task", "error", f"{cid} is implemented by no impl task (**Requirements:**)", [cid])
        if texts["tasks.md"] is not None and "tests:task" not in kinds:
            issue("criterion-no-test-task", "error", f"{cid} is tested by no test task (**Requirements:**)", [cid])

    contained: Dict[str, List[str]] = {}
    for e in edges:
        if e["kind"] == "contains":
            contained.setdefault(e["to"], []).append(e["from"])
    for node in nodes.values():
        if node["kind"] == "case" and texts["tasks.md"] is not None and not contained.get(node["id"]):
            issue("case-no-task", "error", f"{node['id']} is in no test task", [node["id"]])
        if node["kind"] == "property" and texts["tasks.md"] is not None and len(contained.get(node["id"], [])) != 1:
            owners = contained.get(node["id"], [])
            issue("property-task-count", "error", f"{node['id']} must be listed under **Properties:** by exactly one test task, not {len(owners)}" + (f" ({', '.join(owners)})" if owners else ""), [node["id"], *owners])

    for num, task in tasks.items():
        tid = task_id(num)
        for test_num in task.verified_by:
            if test_num in tasks and num not in tasks[test_num].verifies:
                issue("verify-asymmetry", "error", f"{tid} is **Verified by:** Task {test_num}, but Task {test_num} does not list it under **Verifies:**", [tid, task_id(test_num)])
        for impl_num in task.verifies:
            if impl_num in tasks and num not in tasks[impl_num].verified_by:
                issue("verify-asymmetry", "error", f"{tid} **Verifies:** Task {impl_num}, but Task {impl_num} does not list it under **Verified by:**", [tid, task_id(impl_num)])
        mine = out.get((tid, "implements" if task.track != "test" else "tests"), set())
        if task.track != "test" and mine and not task.verified_by:
            issue("impl-unverified", "error", f"{tid} implements {', '.join(sorted(mine))} but no test task verifies it", [tid])
        if task.track == "test" and mine and not task.verifies:
            issue("test-verifies-nothing", "error", f"{tid} tests {', '.join(sorted(mine))} but verifies no impl task", [tid])
        if task.track == "test":
            for item in sorted(out.get((tid, "contains"), set())):
                missing = sorted(out.get((item, "covers"), set()) - mine)
                if missing:
                    issue("task-misses-criterion", "warning", f"{tid} contains {item}, which covers {', '.join(missing)}, but its **Requirements:** omit it", [tid, item])
        else:
            for test_num in task.verified_by:
                theirs = out.get((task_id(test_num), "tests"), set())
                if mine and theirs and not (mine & theirs):
                    issue("verify-no-overlap", "warning", f"{tid} is verified by Task {test_num}, but they share no criterion (fine only under the verify-order rule)", [tid, task_id(test_num)])


# ---------------------------------------------------------------------------
# The blind dossier and the diff against an inferred graph
# ---------------------------------------------------------------------------


def _redact(text: str, criteria: Iterable[str]) -> str:
    """Replace criterion and story references with [ref], so the reader can't copy declared links."""
    ids = sorted(criteria, key=len, reverse=True)
    text = RANGE.sub(lambda m: "[ref]" if f"{m.group(1)}.{m.group(2)}" in ids else m.group(0), text)
    text = STORY_REF.sub("[ref]", text)
    if ids:
        text = re.sub(r"\b(?:" + "|".join(re.escape(i) for i in ids) + r")\b", "[ref]", text)
    return text


def _strip_traceability(design: str) -> str:
    """design.md without its Requirements Traceability section."""
    out, skipping, level = [], False, 0
    for line in design.splitlines():
        m = re.match(r"^(#+)\s+(.*)$", line)
        if m:
            if skipping and len(m.group(1)) <= level:
                skipping = False
            if not skipping and "traceab" in m.group(2).lower():
                skipping, level = True, len(m.group(1))
                continue
        if not skipping:
            out.append(line)
    return "\n".join(out)


def _strip_fields(block: str) -> str:
    pattern = r"^\*\*(?:" + "|".join(re.escape(f) for f in REF_FIELDS) + r"):\*\*.*$\n?"
    return re.sub(pattern, "", block, flags=re.MULTILINE | re.IGNORECASE)


def blind_dossier(spec_dir: Path) -> str:
    """The spec's content with every declared criterion link removed, as markdown for the inferring agent."""
    spec_dir = Path(spec_dir)
    texts = {name: _read(spec_dir / name) or "" for name in SOURCES}
    graph = build_graph(spec_dir)
    criteria = [n["id"] for n in graph["nodes"] if n["kind"] == "criterion"]
    parts = ["# Blind dossier: " + spec_dir.name, "",
             "Generated by `ck graph --blind`. Every link between criteria and the rest of the spec has been removed",
             "(traceability table, criterion columns, **Requirements:** fields, criterion ids in prose are shown as [ref]).",
             "", "## Criteria (link targets)", ""]
    stories = {n["id"]: n["text"] for n in graph["nodes"] if n["kind"] == "story"}
    for sid, title in stories.items():
        parts.append(f"### {sid}: {title}")
        for n in graph["nodes"]:
            if n["kind"] == "criterion" and n["story"] == sid:
                parts.append(f"- {n['id']} {n['text']}")
        parts.append("")
    design_nodes = [n["id"] for n in graph["nodes"] if n["kind"] == "design"]
    parts += ["## Design elements (link sources)", ""] + [f"- {d}" for d in design_nodes] + [""]
    parts += ["### design.md, without its traceability section", "", _redact(_strip_traceability(texts["design.md"]), criteria), ""]
    parts += ["## Test cases and properties (link sources)", ""]
    for n in graph["nodes"]:
        if n["kind"] in ("case", "property"):
            parts.append(f"- {n['id']}: {_redact(n['text'], criteria)}")
    parts += ["", "## Tasks (link sources)", ""]
    for num, block in _task_blocks(texts["tasks.md"]).items():
        parts.append(_redact(_strip_fields(block), criteria).strip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _declared_links(graph: Dict) -> Set[Tuple[str, str]]:
    return {(e["from"], e["to"]) for e in graph["edges"] if e["kind"] in CRITERION_EDGES}


def diff_graphs(graph: Dict, inferred: Dict) -> Dict:
    """Compare declared criterion links with an inferred graph {"edges": [{"from", "to", "evidence"}]}.

    An inferred edge may target a story ("Story 3"): it then matches a declared link to any of that
    story's criteria, and a declared link to one of them isn't reported as unsupported. Granularity
    differences are not findings; only links one side has and the other has nowhere.
    """
    nodes = {n["id"]: n for n in graph["nodes"]}
    criteria = {nid for nid, n in nodes.items() if n["kind"] == "criterion"}
    story_of = {cid: nodes[cid]["story"] for cid in criteria}
    sources = {nid for nid, n in nodes.items() if n["kind"] in ("design", "case", "property", "task")}
    targets = criteria | {nid for nid, n in nodes.items() if n["kind"] == "story"}
    declared = _declared_links(graph)

    inferred_links: Dict[Tuple[str, str], str] = {}
    unknown = []
    for e in (inferred or {}).get("edges", []):
        src, dst = str(e.get("from", "")).strip(), str(e.get("to", "")).strip()
        if src not in sources or dst not in targets:
            unknown.append({"from": src, "to": dst, "evidence": e.get("evidence")})
            continue
        inferred_links.setdefault((src, dst), e.get("evidence") or "")

    def inferred_supports(src: str, cid: str) -> bool:
        return (src, cid) in inferred_links or (src, story_of[cid]) in inferred_links

    def declared_has(src: str, dst: str) -> bool:
        if dst in criteria:
            return (src, dst) in declared
        return any(s == src and story_of.get(c) == dst for s, c in declared)

    unsupported = [{"from": s, "to": c} for s, c in sorted(declared) if not inferred_supports(s, c)]
    undeclared = [{"from": s, "to": d, "evidence": ev} for (s, d), ev in sorted(inferred_links.items()) if not declared_has(s, d)]
    linked_inferred = {d for _, d in inferred_links}
    linked_declared = {c for _, c in declared}
    orphans = sorted(c for c in criteria if c not in linked_declared and c not in linked_inferred and story_of[c] not in linked_inferred)
    return {
        "declaredOnly": unsupported,
        "inferredOnly": undeclared,
        "orphans": orphans,
        "unknown": unknown,
        "counts": {"declared": len(declared), "inferred": len(inferred_links)},
    }


# ---------------------------------------------------------------------------
# graph.json
# ---------------------------------------------------------------------------


def write_graph(spec_dir: Path, graph: Dict, inferred: Optional[Dict] = None, diff: Optional[Dict] = None) -> Path:
    data = {
        "derived": True,
        "note": "Generated by `ck graph --write` (the spec-create Graph Gate). Do not edit: the lists are the source; rerun to refresh.",
        **graph,
    }
    if inferred is not None:
        data["inferred"] = inferred.get("edges", [])
    if diff is not None:
        data["diff"] = diff
    path = Path(spec_dir) / GRAPH_FILE
    path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    return path


def graph_staleness(spec_dir: Path) -> Optional[List[str]]:
    """Source files changed since graph.json was written; None when there is no readable graph.json."""
    try:
        recorded = json.loads((Path(spec_dir) / GRAPH_FILE).read_text()).get("sources") or {}
    except (OSError, ValueError, AttributeError):
        return None
    return [name for name in SOURCES if recorded.get(name) != _sha(Path(spec_dir) / name)]
