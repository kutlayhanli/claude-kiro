"""`ck graph`: the declared traceability graph, its completeness checks, the blind dossier, the diff, staleness."""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_kiro.cli.main import cli
from claude_kiro.graph import blind_dossier, build_graph, criterion_refs, diff_graphs, graph_staleness, write_graph
from claude_kiro.lint import lint_spec

REQUIREMENTS = """# Feature: Notes

## User Stories
### Story 1: Save a note
**As a** user **I want** to save notes **So that** I keep them

**Acceptance Criteria:**
- 1.1 WHEN I save a note, THE SYSTEM SHALL store it on disk
- 1.2 WHEN the disk is full, THE SYSTEM SHALL report an error

### Story 2: List notes
**Acceptance Criteria:**
- 2.1 WHEN I list notes, THE SYSTEM SHALL print every saved note, newest first
"""

DESIGN = """# Design: Notes

## Components
### NoteStore
Writes `notes/<id>.md`, raises DiskFull when the write fails (see 1.2).

### ListCommand
Reads the notes directory and prints titles by mtime.

## Requirements Traceability
| Criterion | Design element |
|-----------|----------------|
| 1.1, 1.2 | `NoteStore` |
| 2.1 | ListCommand |
"""

TEST_PLAN = """# Test Plan: Notes

## Test Cases
| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|----|-----------|-------|------|------|------|
| TC-1 | 1.1 | integration | Given a temp dir, when I save, then the file exists | filesystem | tests/test_save.py |
| TC-2 | 1.2 | integration | Given a full disk, when I save, then an error is shown | filesystem | tests/test_save.py |
| TC-3 | 2.1 | e2e | Given two notes, when I list, then both print newest first | CLI | tests/test_list.py |

## Properties
| ID | Requirement | Property | Generator domain | Expected decision |
|----|-------------|----------|------------------|-------------------|
| P-1 | 2.1 | for any set of notes, list order is by mtime descending | lists of notes | sorted |
"""


def task(num, title, *, track="impl", reqs="", desc="", vby="", verifies="", deps="None", props=""):
    lines = [f"### Task {num}: {title}", "**Status:** Not Started", f"**Track:** {track}"]
    if reqs:
        lines.append(f"**Requirements:** {reqs}")
    lines.append(f"**Description:** {desc or title}")
    if props:
        lines.append(f"**Properties:** {props}")
    if vby:
        lines.append(f"**Verified by:** {vby}")
    if verifies:
        lines.append(f"**Verifies:** {verifies}")
    lines.append(f"**Dependencies:** {deps}")
    return "\n".join(lines) + "\n\n---\n\n"


TASKS = "# Implementation Tasks: Notes\n\n## Task Breakdown\n\n" + "".join([
    task(1, "Save tests", track="test", reqs="1.1, 1.2", desc="TC-1, TC-2", verifies="Task 3"),
    task(2, "List tests", track="test", reqs="2.1", desc="TC-3 and the ordering property", props="P-1", verifies="Task 4"),
    task(3, "NoteStore", reqs="1.1, 1.2", vby="Task 1", deps="Task 1 (its tests are the oracle)"),
    task(4, "List command", reqs="2.1", vby="Task 2", deps="Task 2 (its tests are the oracle), Task 3 (reads its files)"),
]) + "## Dependency Graph\n"


def write_spec(root: Path, **overrides) -> Path:
    spec = root / "specs" / "notes"
    spec.mkdir(parents=True, exist_ok=True)
    files = {"requirements.md": REQUIREMENTS, "design.md": DESIGN, "test-plan.md": TEST_PLAN, "tasks.md": TASKS, **overrides}
    for name, text in files.items():
        if text is not None:
            (spec / name).write_text(text)
    return spec


def ck(cwd: Path, *args: str):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(cli, list(args))
    finally:
        os.chdir(old)


def codes(graph):
    return sorted({i["code"] for i in graph["issues"]})


def links(graph, kind):
    return sorted((e["from"], e["to"]) for e in graph["edges"] if e["kind"] == kind)


def test_complete_spec_has_no_issues_and_every_link(tmp_path):
    graph = build_graph(write_spec(tmp_path))
    assert graph["issues"] == []
    assert links(graph, "designs") == [("D:ListCommand", "2.1"), ("D:NoteStore", "1.1"), ("D:NoteStore", "1.2")]
    assert links(graph, "covers") == [("P-1", "2.1"), ("TC-1", "1.1"), ("TC-2", "1.2"), ("TC-3", "2.1")]
    assert links(graph, "implements") == [("Task 3", "1.1"), ("Task 3", "1.2"), ("Task 4", "2.1")]
    assert links(graph, "tests") == [("Task 1", "1.1"), ("Task 1", "1.2"), ("Task 2", "2.1")]
    assert links(graph, "contains") == [("Task 1", "TC-1"), ("Task 1", "TC-2"), ("Task 2", "P-1"), ("Task 2", "TC-3")]
    assert ("Task 4", "Task 3") in links(graph, "depends_on")


@pytest.mark.parametrize(
    "text, expected, dangling",
    [
        ("1.1, 1.2", ["1.1", "1.2"], []),
        ("1.1-1.2", ["1.1", "1.2"], []),
        ("1.1–2", ["1.1", "1.2"], []),
        ("Story 1", ["1.1", "1.2"], []),
        ("1.x", ["1.1", "1.2"], []),
        ("1.3; 4.1", [], ["1.3", "4.1"]),
        ("Story 9", [], ["Story 9"]),
        ("2.1 (ordering)", ["2.1"], []),
        ("1.1 (saves within 0.5 s)", ["1.1"], []),
    ],
)
def test_criterion_refs(text, expected, dangling):
    assert criterion_refs(text, ["1.1", "1.2", "2.1"]) == (expected, dangling)


def test_uncovered_criterion_is_reported_on_every_axis(tmp_path):
    requirements = REQUIREMENTS + "- 2.2 WHEN there are no notes, THE SYSTEM SHALL print nothing\n"
    graph = build_graph(write_spec(tmp_path, **{"requirements.md": requirements}))
    on_22 = sorted(i["code"] for i in graph["issues"] if "2.2" in i["nodes"])
    assert on_22 == ["criterion-no-case", "criterion-no-design", "criterion-no-impl-task", "criterion-no-test-task"]


def test_dangling_references(tmp_path):
    design = DESIGN.replace("| 2.1 | ListCommand |", "| 2.1, 3.4 | ListCommand |")
    tasks = TASKS.replace("**Description:** TC-1, TC-2", "**Description:** TC-1, TC-2, TC-9").replace("**Verified by:** Task 2", "**Verified by:** Task 2, Task 8")
    graph = build_graph(write_spec(tmp_path, **{"design.md": design, "tasks.md": tasks}))
    assert {"dangling-criterion", "dangling-test-ref", "dangling-task"} <= set(codes(graph))


def test_verify_asymmetry_and_property_ownership(tmp_path):
    tasks = TASKS.replace("**Verifies:** Task 4", "**Verifies:** None").replace("**Properties:** P-1", "")
    graph = build_graph(write_spec(tmp_path, **{"tasks.md": tasks}))
    assert {"verify-asymmetry", "property-task-count", "test-verifies-nothing"} <= set(codes(graph))


def test_case_without_criterion_and_task_missing_criterion(tmp_path):
    plan = TEST_PLAN.replace("| TC-2 | 1.2 |", "| TC-2 |  |")
    tasks = TASKS.replace("**Requirements:** 1.1, 1.2\n**Description:** TC-1", "**Requirements:** 1.1\n**Description:** TC-1")
    graph = build_graph(write_spec(tmp_path, **{"test-plan.md": plan}))
    assert "case-no-criterion" in codes(graph)
    graph = build_graph(write_spec(tmp_path, **{"test-plan.md": TEST_PLAN, "tasks.md": tasks}))
    assert "task-misses-criterion" in codes(graph)


def test_missing_files_and_legacy_specs_are_issues_not_crashes(tmp_path):
    graph = build_graph(write_spec(tmp_path, **{"test-plan.md": None}))
    assert "missing-file" in codes(graph)
    legacy = "### Story 1: Old\n- WHEN x, THE SYSTEM SHALL y\n"
    graph = build_graph(write_spec(tmp_path / "old", **{"requirements.md": legacy, "design.md": "# Design\n"}))
    assert {"no-criteria", "no-traceability"} <= set(codes(graph))


def test_blind_dossier_hides_every_declared_link(tmp_path):
    dossier = blind_dossier(write_spec(tmp_path))
    criteria_section, rest = dossier.split("## Design elements", 1)
    assert "- 1.1 WHEN I save a note" in criteria_section
    for leak in ("1.1", "1.2", "2.1", "Traceability", "**Requirements:**", "**Verified by:**", "**Verifies:**", "**Dependencies:**"):
        assert leak not in rest, leak
    assert "- D:NoteStore" in rest and "TC-3:" in rest and "### Task 4: List command" in rest
    assert "raises DiskFull when the write fails (see [ref])" in rest


def test_diff_finds_unsupported_undeclared_and_orphans(tmp_path):
    requirements = REQUIREMENTS + "- 2.2 WHEN there are no notes, THE SYSTEM SHALL print nothing\n"
    graph = build_graph(write_spec(tmp_path, **{"requirements.md": requirements}))
    inferred = {"edges": [
        {"from": "D:NoteStore", "to": "Story 1", "evidence": "writes notes, raises DiskFull"},  # story level: matches 1.1 and 1.2
        {"from": "D:ListCommand", "to": "2.1"},
        {"from": "TC-1", "to": "1.1"}, {"from": "TC-2", "to": "1.2"}, {"from": "TC-3", "to": "2.1"},
        {"from": "P-1", "to": "1.1", "evidence": "ordering of saved notes"},  # wrong: declared 2.1
        {"from": "Task 1", "to": "Story 1"}, {"from": "Task 2", "to": "2.1"},
        {"from": "Task 3", "to": "1.1"}, {"from": "Task 3", "to": "1.2"}, {"from": "Task 4", "to": "2.1"},
        {"from": "Task 9", "to": "2.1"},
    ]}
    diff = diff_graphs(graph, inferred)
    assert diff["declaredOnly"] == [{"from": "P-1", "to": "2.1"}]
    assert diff["inferredOnly"] == [{"from": "P-1", "to": "1.1", "evidence": "ordering of saved notes"}]
    assert diff["orphans"] == ["2.2"]
    assert diff["unknown"] == [{"from": "Task 9", "to": "2.1", "evidence": None}]


def test_graph_json_is_derived_and_staleness_is_reported(tmp_path):
    spec = write_spec(tmp_path)
    assert graph_staleness(spec) is None
    path = write_graph(spec, build_graph(spec), {"edges": []}, {"declaredOnly": []})
    data = json.loads(path.read_text())
    assert data["derived"] is True and "Do not edit" in data["note"] and data["inferred"] == []
    assert graph_staleness(spec) == []
    assert lint_spec(spec, tmp_path)["graphStale"] == []
    (spec / "tasks.md").write_text(TASKS + "\n")
    assert graph_staleness(spec) == ["tasks.md"]
    assert lint_spec(spec, tmp_path)["graphStale"] == ["tasks.md"]
    out = ck(tmp_path, "lint", "notes")
    assert "graph.json is stale: tasks.md" in out.output


def test_cli(tmp_path):
    write_spec(tmp_path)
    out = ck(tmp_path, "graph", "notes", "--write")
    assert out.exit_code == 0, out.output
    assert "✓ complete" in out.output and (tmp_path / "specs/notes/graph.json").is_file()

    inferred = tmp_path / "inferred.json"
    inferred.write_text(json.dumps({"edges": [{"from": "TC-1", "to": "1.1"}]}))
    out = ck(tmp_path, "graph", "notes", "--diff", str(inferred), "--json")
    assert out.exit_code == 0, out.output
    result = json.loads(out.output)
    assert result["issues"] == [] and {"from": "TC-1", "to": "1.1"} not in result["diff"]["declaredOnly"]

    out = ck(tmp_path, "graph", "notes", "--blind")
    assert out.exit_code == 0 and out.output.startswith("# Blind dossier: notes")

    (tmp_path / "specs/notes/tasks.md").write_text(TASKS.replace("**Requirements:** 2.1\n**Description:** List command", "**Description:** List command"))
    out = ck(tmp_path, "graph", "notes")
    assert out.exit_code == 1 and "2.1 is implemented by no impl task" in out.output
