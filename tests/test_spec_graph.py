"""`ck graph SPEC --json` (`ck-graph/1`): nodes, edges, freshness and problems.

TC-102 to TC-105, TC-14, TC-107 and TC-108 of specs/unattended-correctness. Every test runs the real CLI
(`python -m claude_kiro.cli`) on real files in `tmp_path`: `spec_graph_project` (spec `sim`) and `attr_project`
(spec `demo`). Nothing of the project is mocked. Before `ck graph` exists these fail because the command is
unknown, never because of a broken fixture.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple

import pytest

import unattended_helpers as h

SPEC = "sim"

DESIGN_CLI = {
    "mtr thesis new": "src/mtr/thesis.py",
    "mtr thesis": "src/mtr/thesis.py",
    "mtr version": "src/mtr/cli.py",
}


# --------------------------------------------------------------------------- helpers


def graph(root: Path, spec: str = SPEC) -> Dict[str, Any]:
    """Run `ck graph SPEC --json`; the object must be a ck-graph/1 document."""
    code, obj, err = h.ck_json(root, "graph", spec, "--json")
    assert code == 0 and isinstance(obj, dict), f"ck graph {spec} --json failed ({code}): {err}"
    assert obj.get("spec") == spec
    assert set(obj) >= {"nodes", "edges", "problems"}
    return obj


def graph_stdout(root: Path, spec: str = SPEC) -> str:
    code, out, err = h.ck_text(root, "graph", spec, "--json")
    assert code == 0, f"ck graph {spec} --json failed ({code}): {err}"
    return out


def node_set(g: Dict[str, Any]) -> Set[Tuple[str, str]]:
    return {(n["kind"], n["id"]) for n in g["nodes"]}


def edge_set(g: Dict[str, Any], kind: str = "") -> Set[Tuple[str, str, str]]:
    return {(e["kind"], e["src"], e["dst"]) for e in g["edges"] if not kind or e["kind"] == kind}


def with_provides(block_text: str, value: str) -> str:
    """Add a `**Provides:**` field to a `test_lint.block` text (before `**Dependencies:**`)."""
    assert "**Dependencies:**" in block_text
    return block_text.replace("**Dependencies:**", f"**Provides:** {value}\n**Dependencies:**", 1)


def base_tasks(*, test_file: str = "tests/test_cli.py", provides_version: bool = True) -> str:
    """Tasks 1 (test), 2 and 3 (impl) of the TC-102 project."""
    from test_lint import block

    task3 = block(3, "Version command", files=["src/mtr/cli.py"], verify='python -m pytest -q tests/test_cli.py -k "tc2_ and not tc9_"',
                  vby="Task 1", deps="Task 1")
    if provides_version:
        task3 = with_provides(task3, "mtr version")
    return (
        block(1, "CLI tests", track="test", files=[test_file], verify=f"python -m pytest -q {test_file}",
              verifies="Task 2, Task 3")
        + block(2, "Thesis command", files=["src/mtr/thesis.py"], verify='python -m pytest -q tests/test_cli.py -k "tc1_"',
                vby="Task 1", deps="Task 1")
        + task3
    )


CLI_TEST = '''\
from mtr.thesis import make


def test_tc1_thesis_new():
    run_cli(["thesis", "new"])


def test_tc2_version():
    subprocess.run(["mtr", "version"])
'''

THESIS_SRC = "def make():\n    return 1\n"


def tc102_project(tmp_path: Path) -> Path:
    return h.spec_graph_project(tmp_path, tasks=base_tasks(), design_cli=DESIGN_CLI,
                                tests={"tests/test_cli.py": CLI_TEST}, sources={"src/mtr/thesis.py": THESIS_SRC})


# --------------------------------------------------------------------------- TC-102


def test_tc102_graph_nodes_and_edges_equal_the_expected_sets(tmp_path):
    root = tc102_project(tmp_path)
    g = graph(root)

    assert node_set(g) == {
        ("task", "task:1"), ("task", "task:2"), ("task", "task:3"),
        ("tc", "tc:TC-1"), ("tc", "tc:TC-2"),
        ("test_file", "file:tests/test_cli.py"),
        ("source_file", "file:src/mtr/thesis.py"), ("source_file", "file:src/mtr/cli.py"),
        ("command", "cmd:mtr thesis new"), ("command", "cmd:mtr thesis"), ("command", "cmd:mtr version"),
    }
    assert {n["kind"] for n in g["nodes"]} == {"task", "tc", "test_file", "source_file", "command"}

    assert edge_set(g, "depends") == {("depends", "task:2", "task:1"), ("depends", "task:3", "task:1")}
    # `tc2_ and not tc9_`: the term after `not` is not an edge, so there is no TC-9 anywhere.
    assert edge_set(g, "verifies") == {
        ("verifies", "tc:TC-1", "task:2"), ("verifies", "tc:TC-1", "task:3"),
        ("verifies", "tc:TC-2", "task:2"), ("verifies", "tc:TC-2", "task:3"),
    }
    assert not any("TC-9" in n["id"] for n in g["nodes"])
    assert edge_set(g, "owns") == {
        ("owns", "task:1", "file:tests/test_cli.py"), ("owns", "task:2", "file:src/mtr/thesis.py"),
        ("owns", "task:3", "file:src/mtr/cli.py"),
    }
    assert edge_set(g, "provides") == {
        ("provides", "task:2", "cmd:mtr thesis new"),  # design table row names a file Task 2 owns
        ("provides", "task:2", "cmd:mtr thesis"),      # its design table row also names a file Task 2 owns
        ("provides", "task:3", "cmd:mtr version"),     # **Provides:** field
    }
    assert edge_set(g, "invokes") == {
        ("invokes", "file:tests/test_cli.py", "cmd:mtr thesis new"),  # longest path, not `mtr thesis`
        ("invokes", "file:tests/test_cli.py", "cmd:mtr version"),
    }
    assert edge_set(g, "imports") == {("imports", "file:tests/test_cli.py", "file:src/mtr/thesis.py")}
    assert {e["kind"] for e in g["edges"]} == {"depends", "verifies", "owns", "provides", "invokes", "imports"}
    assert g["problems"] == []

    by_id = {n["id"]: n for n in g["nodes"]}
    assert by_id["task:2"]["label"] == "Thesis command"
    assert by_id["file:src/mtr/thesis.py"]["label"] == "src/mtr/thesis.py"
    assert by_id["cmd:mtr thesis new"]["label"] == "mtr thesis new"
    # every verifies edge keeps its origin
    sources = {(e["src"], e["dst"]): set() for e in g["edges"] if e["kind"] == "verifies"}
    for e in g["edges"]:
        if e["kind"] == "verifies":
            sources[(e["src"], e["dst"])].add(e["source"])
    assert "Verify -k" in sources[("tc:TC-1", "task:2")] and "Verify -k" in sources[("tc:TC-2", "task:3")]
    assert "Verifies" in sources[("tc:TC-1", "task:3")]


# --------------------------------------------------------------------------- TC-103


def test_tc103_design_table_infers_the_provider_from_the_file_a_task_owns(tmp_path):
    from test_lint import block

    tasks = (block(71, "Thesis module", files=["src/mtr/thesis.py"])
             + block(82, "Other work", files=["src/mtr/other.py"]))
    root = h.spec_graph_project(tmp_path / "a", tasks=tasks, design_cli={"mtr thesis new": "src/mtr/thesis.py"},
                                sources={"src/mtr/thesis.py": THESIS_SRC, "src/mtr/other.py": "X = 1\n"})
    g = graph(root)
    assert edge_set(g, "provides") == {("provides", "task:71", "cmd:mtr thesis new")}


def test_tc103_provides_field_wins_over_the_design_table(tmp_path):
    from test_lint import block

    tasks = (block(71, "Thesis module", files=["src/mtr/thesis.py"])
             + with_provides(block(82, "Thesis command", files=["src/mtr/other.py"]), "mtr thesis new"))
    root = h.spec_graph_project(tmp_path / "b", tasks=tasks, design_cli={"mtr thesis new": "src/mtr/thesis.py"},
                                sources={"src/mtr/thesis.py": THESIS_SRC, "src/mtr/other.py": "X = 1\n"})
    g = graph(root)
    assert edge_set(g, "provides") == {("provides", "task:82", "cmd:mtr thesis new")}
    assert g["problems"] == []


# --------------------------------------------------------------------------- TC-104


def test_tc104_every_invocation_form_gives_an_invokes_edge_and_near_misses_none(tmp_path):
    from test_lint import block

    forms = h.invocation_forms("thesis new")
    version_forms = h.invocation_forms("version")
    misses = list(h.NEAR_MISS_FORMS)

    def body(snippets: Iterable[str]) -> str:
        return "".join(f"def test_tc1_case{i}():\n    {s}\n\n\n" for i, s in enumerate(snippets))

    tests: Dict[str, str] = {"tests/test_all_forms.py": body(forms + version_forms + misses)}
    for i, form in enumerate(forms):
        tests[f"tests/test_form_{i}.py"] = body([form])
    for i, form in enumerate(version_forms):
        tests[f"tests/test_version_form_{i}.py"] = body([form])
    for i, miss in enumerate(misses):
        tests[f"tests/test_miss_{i}.py"] = body([miss])

    task = block(1, "Form tests", track="test", files=sorted(tests), verifies="Task 2")
    root = h.spec_graph_project(tmp_path, tasks=task + block(2, "Impl", files=["src/mtr/thesis.py"], vby="Task 1", deps="Task 1"),
                                design_cli=DESIGN_CLI, tests=tests, sources={"src/mtr/thesis.py": THESIS_SRC})
    g = graph(root)
    assert g["problems"] == []

    expected = {("invokes", "file:tests/test_all_forms.py", "cmd:mtr thesis new"),
                ("invokes", "file:tests/test_all_forms.py", "cmd:mtr version")}
    expected |= {("invokes", f"file:tests/test_form_{i}.py", "cmd:mtr thesis new") for i in range(len(forms))}
    expected |= {("invokes", f"file:tests/test_version_form_{i}.py", "cmd:mtr version") for i in range(len(version_forms))}
    got = edge_set(g, "invokes")
    assert got == expected, f"missing {sorted(expected - got)}; unexpected {sorted(got - expected)}"
    # never the shorter path `mtr thesis`, whatever the form
    assert not any(dst == "cmd:mtr thesis" for _, _, dst in got)


# --------------------------------------------------------------------------- TC-105


def test_tc105_two_runs_are_byte_identical_and_sorted(tmp_path):
    root = tc102_project(tmp_path)
    first = graph_stdout(root)
    second = graph_stdout(root)
    assert first == second
    g = json.loads(first)
    assert g["nodes"] == sorted(g["nodes"], key=lambda n: (n["kind"], n["id"]))
    assert g["edges"] == sorted(g["edges"], key=lambda e: (e["kind"], e["src"], e["dst"]))
    assert len(g["nodes"]) > 5 and len(g["edges"]) > 5


# --------------------------------------------------------------------------- TC-14


def _rewrite_tasks(root: Path, edit) -> None:
    path = root / "specs" / "demo" / "tasks.md"
    path.write_text(edit(path.read_text()))
    h.commit_all(root, "move tc3_ verify")


def _tc3_verify_dsts(g: Dict[str, Any]) -> Set[str]:
    return {e["dst"] for e in g["edges"] if e["kind"] == "verifies" and e["src"] == "tc:TC-3" and e["source"] == "Verify -k"}


def _gate_tc3_tasks(root: Path) -> List[str]:
    code, obj, err = h.ck_json(root, "gate", "demo", "--attribute", "--json")
    assert isinstance(obj, dict), f"ck gate demo --attribute --json printed no JSON ({code}): {err}"
    entries = [f for f in obj.get("failures", []) if f.get("tcId") == "TC-3"]
    assert len(entries) == 1, obj.get("failures")
    return entries[0]["tasks"]


def test_tc14_moving_a_verify_term_changes_graph_and_gate_together(tmp_path):
    root = h.attr_project(tmp_path, tests={"test_tc1_a": "pass", "test_tc2_b": "pass", "test_tc3_c": "fail"})

    assert _tc3_verify_dsts(graph(root, "demo")) == {"task:3"}
    assert _gate_tc3_tasks(root) == ["3"]

    def move(text: str) -> str:
        assert text.count('-k "tc3_"') == 1
        text = text.replace('-k "tc3_"', '-k "tc1_"')  # Task 3's Verify no longer selects tc3_
        # Task 8 gains a Verify with tc3_ (it had none).
        eight = "**Files:**\n- `eight.py` - eight\n"
        assert eight in text
        return text.replace(eight, eight + '**Verify:** `python -m pytest -q tests/test_calc_tc.py -k "tc3_"`\n')

    _rewrite_tasks(root, move)

    assert _tc3_verify_dsts(graph(root, "demo")) == {"task:8"}
    assert _gate_tc3_tasks(root) == ["8"]


# --------------------------------------------------------------------------- TC-107


def test_tc107_edits_with_restored_mtimes_are_reflected(tmp_path):
    from test_lint import block

    tasks = (
        block(1, "CLI tests", track="test", files=["tests/test_x.py"], verifies="Task 2, Task 3")
        + block(2, "Thesis", files=["src/mtr/thesis.py"], vby="Task 1", deps="Task 1")
        + block(3, "Cli", files=["src/mtr/cli.py"], vby="Task 1", deps="Task 1")
    )
    test_src = 'def test_tc1_x():\n    subprocess.run(["mtr2", "report"])\n'
    root = h.spec_graph_project(
        tmp_path, tasks=tasks, design_cli={"mtr2 report": "src/mtr2/cli.py", "mtr2 cancel": "src/mtr2/cli.py"},
        tests={"tests/test_x.py": test_src}, sources={"src/mtr/thesis.py": "import mtr.xyz\n"})
    before = graph(root)
    assert ("depends", "task:3", "task:1") in edge_set(before, "depends")
    assert edge_set(before, "invokes") == {("invokes", "file:tests/test_x.py", "cmd:mtr2 report")}
    assert edge_set(before, "imports") == set()

    def edit(rel: str, old: str, new: str, *, from_marker: str = "") -> None:
        path = root / rel
        stat = path.stat()
        text = path.read_text()
        start = text.index(from_marker) if from_marker else 0
        assert old in text[start:] and len(old) == len(new)
        path.write_text(text[:start] + text[start:].replace(old, new, 1))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert path.stat().st_mtime_ns == stat.st_mtime_ns and path.stat().st_size == stat.st_size

    # Task 3 now depends on Task 2, the test invokes another command, a source imports another module.
    edit(f"specs/{SPEC}/tasks.md", "**Dependencies:** Task 1", "**Dependencies:** Task 2", from_marker="### Task 3:")
    edit("tests/test_x.py", '"report"', '"cancel"')
    edit("src/mtr/thesis.py", "import mtr.xyz", "import mtr.cli")

    after = graph(root)
    assert edge_set(after, "depends") == {("depends", "task:2", "task:1"), ("depends", "task:3", "task:2")}
    assert edge_set(after, "invokes") == {("invokes", "file:tests/test_x.py", "cmd:mtr2 cancel")}
    assert edge_set(after, "imports") == {("imports", "file:src/mtr/thesis.py", "file:src/mtr/cli.py")}


# --------------------------------------------------------------------------- TC-108


def test_tc108_malformed_sources_become_problems_and_the_rest_is_built(tmp_path):
    from test_lint import block

    good = base_tasks()
    bad_blocks = ("### Task x: Broken header\n**Status:** Not Started\n**Track:** impl\n**Dependencies:** None\n\n---\n\n"
                  + "### Task 5: Dangling dependency\n**Status:** Not Started\n**Track:** impl\n**Files:**\n- `src/mtr/five.py` - x\n"
                    "**Dependencies:** Task\n\n---\n\n")
    bad_test = "def test_tc3_broken(:\n    pass\n"
    bad_owner = block(6, "Owns the broken test", track="test", files=["tests/test_bad.py"], verifies="Task 2")

    clean = h.spec_graph_project(tmp_path / "clean", tasks=good + block(6, "Owns nothing", track="test", verifies="Task 2"),
                                 design_cli=DESIGN_CLI, tests={"tests/test_cli.py": CLI_TEST},
                                 sources={"src/mtr/thesis.py": THESIS_SRC})
    dirty = h.spec_graph_project(tmp_path / "dirty", tasks=good + bad_blocks + bad_owner, design_cli=DESIGN_CLI,
                                 tests={"tests/test_cli.py": CLI_TEST, "tests/test_bad.py": bad_test},
                                 sources={"src/mtr/thesis.py": THESIS_SRC})

    clean_g = graph(clean)
    assert clean_g["problems"] == []
    g = graph(dirty)

    problems = g["problems"]
    assert len(problems) == 3, problems
    assert all(set(p) >= {"source", "problem"} and p["problem"] for p in problems)
    by_source = {p["source"]: p["problem"] for p in problems}
    assert len(by_source) == 3, "each bad source is named once"
    header = [s for s in by_source if "tasks.md" in s and "Task x" in s]
    dangling = [s for s in by_source if "tasks.md" in s and "Task 5" in s]
    broken = [s for s in by_source if s.endswith("tests/test_bad.py")]
    assert len(header) == 1 and len(dangling) == 1 and len(broken) == 1, by_source
    assert "SyntaxError" in by_source[broken[0]]

    # Everything else is the clean subset's graph: the bad test file and the task that owns it are all that differ.
    bad_node = "file:tests/test_bad.py"
    nodes = {n for n in node_set(g) if n[1] not in (bad_node, "task:6")}
    edges = {e for e in edge_set(g) if bad_node not in e[1:] and "task:6" not in e[1:]}
    clean_nodes = {n for n in node_set(clean_g) if n[1] != "task:6"}
    clean_edges = {e for e in edge_set(clean_g) if "task:6" not in e[1:]}
    assert nodes == clean_nodes
    assert edges == clean_edges
    assert not any(n[1] in ("task:5", "task:x") for n in node_set(g)), "a malformed block adds no task node"
