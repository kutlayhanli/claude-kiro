"""Red team of Story 11's SHALL NOT clauses (written from requirements 11.6 and 11.7 only).

11.6: attribution and dependency detection SHALL NOT use separate mappings: `ck graph`, `ck lint`, `ck plan`
and `ck gate --attribute` must all reflect a Verify selector (or import) change in one step.
11.7: ck SHALL NEVER serve a graph older than the files it reads: edits that keep size and mtime, edits through a
symlink, a file replaced by rename, same-second edits across in-process calls.

Every test runs the real CLI on real files in `tmp_path`. Nothing of the project is mocked.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

import pytest

import unattended_helpers as h

SPEC = "sim"


# --------------------------------------------------------------------------- helpers


def graph(root: Path, spec: str = SPEC) -> Dict[str, Any]:
    code, obj, err = h.ck_json(root, "graph", spec, "--json")
    assert code == 0 and isinstance(obj, dict), f"ck graph {spec} --json failed ({code}): {err}"
    return obj


def graph_inproc(root: Path, spec: str = SPEC) -> Dict[str, Any]:
    """`ck graph` through the click entry point in this process: the in-process memo survives between calls."""
    from click.testing import CliRunner

    from claude_kiro.cli.main import cli

    old = os.getcwd()
    os.chdir(root)
    try:
        res = CliRunner().invoke(cli, ["graph", spec, "--json"])
    finally:
        os.chdir(old)
    assert res.exit_code == 0, f"ck graph failed ({res.exit_code}): {res.output}"
    return json.loads(res.output)


def edges(obj: Dict[str, Any], kind: str, source: Optional[str] = None) -> Set[Tuple[str, str]]:
    return {(e["src"], e["dst"]) for e in obj["edges"] if e["kind"] == kind and (source is None or e["source"] == source)}


def node_ids(obj: Dict[str, Any], kind: Optional[str] = None) -> Set[str]:
    return {n["id"] for n in obj["nodes"] if kind is None or n["kind"] == kind}


def replace_keep_stat(path: Path, old: str, new: str, *, same_size: bool = True) -> None:
    """Edit `path` in place, then restore its atime and mtime to the nanosecond."""
    st = os.stat(path)
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    out = text.replace(old, new, 1)
    if same_size:
        assert len(out.encode("utf-8")) == len(text.encode("utf-8")), "edit must keep the byte size"
    path.write_text(out, encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    after = os.stat(path)
    assert after.st_mtime_ns == st.st_mtime_ns
    if same_size:
        assert after.st_size == st.st_size


def set_verify(tasks_md: Path, task: str, verify: Optional[str]) -> None:
    """Rewrite (or add, or remove with None) the `**Verify:**` line of one `### Task N` block."""
    text = tasks_md.read_text(encoding="utf-8")
    m = re.search(rf"^### Task {re.escape(task)}:.*?(?=^### Task |\Z)", text, re.S | re.M)
    assert m, f"task {task} not in tasks.md"
    blk = m.group(0)
    blk = re.sub(r"^\*\*Verify:\*\*.*\n", "", blk, flags=re.M)
    if verify is not None:
        blk = re.sub(r"^(\*\*Verified by:\*\*|\*\*Verifies:\*\*|\*\*Dependencies:\*\*)", lambda mm: f"**Verify:** `{verify}`\n{mm.group(1)}", blk, count=1, flags=re.M)
    tasks_md.write_text(text[: m.start()] + blk + text[m.end():], encoding="utf-8")


ALL_DONE = {n: "Done" for n in ("1", "2", "3", "6", "7", "8")}
TESTS = {"test_tc1_a": "pass", "test_tc2_b": "pass", "test_tc3_c": "fail", "test_tc5_d": "pass",
         "test_tc13_e": "pass", "test_tc30_f": "pass"}


def attr(tmp_path: Path) -> Path:
    return h.attr_project(tmp_path, statuses=ALL_DONE, tests=TESTS)


def gate_tasks(root: Path, tc: str = "TC-3") -> Any:
    """The attributed tasks of the gate failure for `tc` (None when the gate has no such failure)."""
    code, obj, err = h.ck_json(root, "gate", "demo", "--attribute", "--json")
    assert isinstance(obj, dict) and "failures" in obj, f"ck gate --attribute --json gave no failures ({code}): {err}"
    found = [f for f in obj["failures"] if f.get("tcId") == tc]
    return found[0]["tasks"] if found else None


# =========================================================================== 11.6: single mapping


def test_tc14_redteam_selector_move_reflected_by_graph_and_gate_in_one_step(tmp_path):
    root = attr(tmp_path)
    tasks_md = root / "specs/demo/tasks.md"
    before = graph(root, "demo")
    assert ("tc:TC-3", "task:3") in edges(before, "verifies", "Verify -k")
    assert gate_tasks(root) == ["3"]

    set_verify(tasks_md, "3", 'python -m pytest -q tests/test_calc_tc.py -k "tc1_"')
    set_verify(tasks_md, "8", 'python -m pytest -q tests/test_calc_tc.py -k "tc3_"')

    after = graph(root, "demo")
    assert ("tc:TC-3", "task:8") in edges(after, "verifies", "Verify -k")
    assert ("tc:TC-3", "task:3") not in edges(after, "verifies", "Verify -k")
    assert gate_tasks(root) == ["8"]


def test_tc14_redteam_selector_move_same_size_same_mtime(tmp_path):
    root = attr(tmp_path)
    tasks_md = root / "specs/demo/tasks.md"
    assert gate_tasks(root) == ["3"]
    assert ("tc:TC-3", "task:3") in edges(graph(root, "demo"), "verifies", "Verify -k")

    # Same byte length: Task 3 now selects tc2_, Task 8 selects tc3_ (added with a same-size placeholder first).
    set_verify(tasks_md, "8", 'python -m pytest -q tests/test_calc_tc.py -k "tc9_"')
    assert gate_tasks(root) == ["3"]
    replace_keep_stat(tasks_md, '-k "tc9_"', '-k "tc3_"')
    replace_keep_stat(tasks_md, '-k "tc3_"', '-k "tc1_"')  # first match is Task 3's selector

    after = graph(root, "demo")
    assert ("tc:TC-3", "task:8") in edges(after, "verifies", "Verify -k")
    assert ("tc:TC-3", "task:3") not in edges(after, "verifies", "Verify -k")
    assert gate_tasks(root) == ["8"]


def test_tc14_redteam_not_expression_adds_no_edge(tmp_path):
    root = attr(tmp_path)
    tasks_md = root / "specs/demo/tasks.md"
    set_verify(tasks_md, "8", 'python -m pytest -q tests/test_calc_tc.py -k "not tc3_"')
    obj = graph(root, "demo")
    assert ("tc:TC-3", "task:8") not in edges(obj, "verifies")
    assert gate_tasks(root) == ["3"]

    set_verify(tasks_md, "8", 'python -m pytest -q tests/test_calc_tc.py -k "tc2_ and not tc3_"')
    obj = graph(root, "demo")
    assert ("tc:TC-3", "task:8") not in edges(obj, "verifies")
    assert ("tc:TC-2", "task:8") in edges(obj, "verifies", "Verify -k")
    assert gate_tasks(root) == ["3"]


def test_tc14_redteam_selector_substring_near_misses(tmp_path):
    root = attr(tmp_path)
    tasks_md = root / "specs/demo/tasks.md"
    set_verify(tasks_md, "8", 'python -m pytest -q tests/test_more.py -k "tc13_ or tc30_"')
    obj = graph(root, "demo")
    verifies = edges(obj, "verifies", "Verify -k")
    assert ("tc:TC-13", "task:8") in verifies and ("tc:TC-30", "task:8") in verifies
    assert ("tc:TC-3", "task:8") not in verifies and ("tc:TC-1", "task:8") not in verifies
    assert gate_tasks(root) == ["3"]


def test_tc14_redteam_verifies_fallback_is_never_unioned(tmp_path):
    root = attr(tmp_path)
    obj = graph(root, "demo")
    # Task 1 lists `Verifies: Task 2, Task 3`, so TC-3 may carry a "Verifies" edge to Task 2 as well; attribution
    # must use the "Verify -k" edge only while one exists.
    assert ("tc:TC-3", "task:3") in edges(obj, "verifies", "Verify -k")
    assert gate_tasks(root) == ["3"]


def test_tc14_redteam_import_edit_reaches_graph_lint_and_plan_together(tmp_path):
    from test_lint import block

    tasks = (block(1, "tests", track="test", files=["tests/test_t.py"], verify="python -m pytest -q tests/test_t.py",
                   verifies="Task 2")
             + block(2, "impl a", files=["src/mtr/a.py"], vby="Task 1", deps="Task 1")
             + block(3, "impl b", files=["src/mtr/b.py"], deps="Task 2"))
    test_file = "from mtr.b import f\n\n\ndef test_tc1_x():\n    assert f()\n"
    root = h.spec_graph_project(tmp_path, tasks=tasks, tests={"tests/test_t.py": test_file},
                                sources={"src/mtr/a.py": "def f():\n    return 1\n", "src/mtr/b.py": "def g():\n    return 2\n"})

    def views():
        g = graph(root)
        _, lint, _ = h.ck_json(root, "lint", SPEC, "--json")
        _, plan, _ = h.ck_json(root, "plan", SPEC, "--json")
        assert isinstance(lint, dict) and isinstance(plan, dict)
        return g, [c["task"] for c in lint["verifyCycles"]], plan.get("verifyCycles")

    g, lint_cycles, plan_cycles = views()
    assert ("file:tests/test_t.py", "file:src/mtr/b.py") in edges(g, "imports")
    # Task 2 is verified by tests that import Task 3's module, and Task 3 depends on Task 2: a cycle in every view.
    assert lint_cycles == ["2"], lint_cycles
    assert plan_cycles and any(("2" in json.dumps(c)) for c in plan_cycles), plan_cycles

    replace_keep_stat(root / "tests/test_t.py", "from mtr.b import f", "from mtr.a import f")
    g, lint_cycles, plan_cycles = views()
    assert ("file:tests/test_t.py", "file:src/mtr/a.py") in edges(g, "imports")
    assert ("file:tests/test_t.py", "file:src/mtr/b.py") not in edges(g, "imports")
    assert lint_cycles == [] and not plan_cycles


def test_tc14_redteam_test_plan_row_moves_edge_for_every_consumer(tmp_path):
    root, _ = _plan_row_project(tmp_path)
    plan_before = h.ck_json(root, "plan", SPEC, "--json")[1]
    assert isinstance(plan_before, dict)
    assert ("tc:TC-9", "task:3") in edges(graph(root), "verifies", "Verifies")

    replace_keep_stat(root / f"specs/{SPEC}/test-plan.md", "tests/test_a.py", "tests/test_b.py")

    after = graph(root)
    assert ("tc:TC-9", "task:4") in edges(after, "verifies", "Verifies")
    assert ("tc:TC-9", "task:3") not in edges(after, "verifies")
    plan_after = h.ck_json(root, "plan", SPEC, "--json")[1]
    assert isinstance(plan_after, dict)
    # `testVerifies` (plan) is derived from the same edges: the test-plan row moved test_a's TC to Task 4's file.
    assert "4" in plan_after["testVerifies"].get("tests/test_b.py", [])


# =========================================================================== 11.7: freshness


def _plan_row_project(tmp_path: Path):
    from test_lint import block

    tasks = (block(1, "tests a", track="test", files=["tests/test_a.py"], verify="python -m pytest -q tests/test_a.py", verifies="Task 3")
             + block(2, "tests b", track="test", files=["tests/test_b.py"], verify="python -m pytest -q tests/test_b.py", verifies="Task 4")
             + block(3, "impl three", files=["src/mtr/three.py"], vby="Task 1", deps="Task 1")
             + block(4, "impl four", files=["src/mtr/four.py"], vby="Task 2", deps="Task 2"))
    body = "def test_plain():\n    assert True\n"
    root = h.spec_graph_project(tmp_path, tasks=tasks, tests={"tests/test_a.py": body, "tests/test_b.py": body})
    row = "| TC-9 | 1.1 | int-cli | a row | real | `tests/test_a.py` |\n"
    header = "# Test Plan: Sim\n\n| TC | Req | Kind | Description | Setup | Test file |\n|----|-----|------|-------------|-------|-----------|\n"
    (root / f"specs/{SPEC}/test-plan.md").write_text(header + row)
    h.commit_all(root, "plan row")
    return root, row


def _tc_project(tmp_path: Path, test_text: str, *, title: str = "tests", extra_files: Optional[Dict[str, str]] = None,
                design_cli=()) -> Path:
    from test_lint import block

    tasks = (block(1, title, track="test", files=["tests/test_cli_a.py"], verify="python -m pytest -q tests/test_cli_a.py",
                   verifies="Task 2")
             + block(2, "impl", files=["src/mtr/impl.py"], vby="Task 1", deps="Task 1",
                     verify='python -m pytest -q tests/test_cli_a.py -k "tc3_"'))
    tests = {"tests/test_cli_a.py": test_text}
    tests.update(extra_files or {})
    return h.spec_graph_project(tmp_path, tasks=tasks, tests=tests, design_cli=design_cli)


TC3_TEST = "def test_tc3_alpha():\n    assert True\n"
TC8_TEST = "def test_tc8_alpha():\n    assert True\n"


def test_tc107_redteam_tasks_md_same_size_same_mtime(tmp_path):
    root = _tc_project(tmp_path, TC3_TEST)
    tasks_md = root / "specs/sim/tasks.md"
    assert ("tc:TC-3", "task:2") in edges(graph(root), "verifies", "Verify -k")
    replace_keep_stat(tasks_md, '-k "tc3_"', '-k "tc8_"')
    after = graph(root)
    assert ("tc:TC-8", "task:2") in edges(after, "verifies", "Verify -k")
    assert ("tc:TC-3", "task:2") not in edges(after, "verifies", "Verify -k")


def test_tc107_redteam_test_file_same_size_same_mtime(tmp_path):
    root = _tc_project(tmp_path, TC3_TEST)
    assert "tc:TC-3" in node_ids(graph(root), "tc") and "tc:TC-8" not in node_ids(graph(root), "tc")
    replace_keep_stat(root / "tests/test_cli_a.py", "test_tc3_alpha", "test_tc8_alpha")
    after = graph(root)
    assert "tc:TC-8" in node_ids(after, "tc") and "tc:TC-3" not in node_ids(after, "tc")
    assert ("tc:TC-8", "task:2") in edges(after, "verifies", "Verifies")


def test_tc107_redteam_source_file_same_size_same_mtime(tmp_path):
    test_text = 'import subprocess\n\n\ndef test_tc3_alpha():\n    subprocess.run(["mtr2", "reprot"])\n'
    root = _tc_project(tmp_path, test_text, design_cli={"mtr2 version": "src/mtr2/cli.py"})
    _, before, _ = h.ck_json(root, "lint", SPEC, "--json")
    assert isinstance(before, dict) and "mtr2 reprot" in json.dumps(before["unknownCommands"])
    replace_keep_stat(root / "src/mtr2/cli.py", 'add_parser("report")', 'add_parser("reprot")')
    _, after, _ = h.ck_json(root, "lint", SPEC, "--json")
    assert isinstance(after, dict) and "mtr2 reprot" not in json.dumps(after["unknownCommands"])


def test_tc107_redteam_file_replaced_by_rename_with_old_mtime(tmp_path):
    root = _tc_project(tmp_path, TC3_TEST)
    path = root / "tests/test_cli_a.py"
    assert "tc:TC-3" in node_ids(graph(root), "tc")
    st = os.stat(path)
    new = path.with_name("replacement.tmp")
    new.write_text(TC8_TEST)
    assert len(TC8_TEST) == len(TC3_TEST)
    os.utime(new, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(new, path)  # new inode, same size, same mtime_ns
    after_stat = os.stat(path)
    assert (after_stat.st_size, after_stat.st_mtime_ns) == (st.st_size, st.st_mtime_ns)
    after = graph(root)
    assert "tc:TC-8" in node_ids(after, "tc") and "tc:TC-3" not in node_ids(after, "tc")


def test_tc107_redteam_tasks_md_replaced_by_rename_with_old_mtime(tmp_path):
    root = _tc_project(tmp_path, TC3_TEST)
    path = root / "specs/sim/tasks.md"
    assert graph(root)
    st = os.stat(path)
    new = path.with_name("tasks.new")
    new.write_text(path.read_text().replace('-k "tc3_"', '-k "tc8_"'))
    os.utime(new, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(new, path)
    after = graph(root)
    assert ("tc:TC-8", "task:2") in edges(after, "verifies", "Verify -k")


def test_tc107_redteam_edit_through_symlink(tmp_path):
    root = _tc_project(tmp_path, TC3_TEST)
    link = root / "specs/sim/tasks.md"
    real = root / "real_tasks.md"
    real.write_text(link.read_text())
    link.unlink()
    link.symlink_to(real)
    assert ("tc:TC-3", "task:2") in edges(graph(root), "verifies", "Verify -k")
    replace_keep_stat(link, '-k "tc3_"', '-k "tc8_"')  # written through the link; the target keeps its stat
    after = graph(root)
    assert ("tc:TC-8", "task:2") in edges(after, "verifies", "Verify -k")
    assert ("tc:TC-3", "task:2") not in edges(after, "verifies", "Verify -k")


def test_tc107_redteam_symlink_retargeted_to_equal_stat_file(tmp_path):
    root = _tc_project(tmp_path, TC3_TEST)
    link = root / "tests/test_cli_a.py"
    one, two = root / "shared_one.py", root / "shared_two.py"
    one.write_text(TC3_TEST)
    two.write_text(TC8_TEST)
    stamp = (1_700_000_000_000_000_000, 1_700_000_000_000_000_000)
    os.utime(one, ns=stamp)
    os.utime(two, ns=stamp)
    link.unlink()
    link.symlink_to(one)
    assert "tc:TC-3" in node_ids(graph(root), "tc")
    link.unlink()
    link.symlink_to(two)  # same size, same mtime_ns through the link, different file
    after = graph(root)
    assert "tc:TC-8" in node_ids(after, "tc") and "tc:TC-3" not in node_ids(after, "tc")


def test_tc107_redteam_same_second_in_process_flips(tmp_path):
    root = _tc_project(tmp_path, TC3_TEST)
    path = root / "tests/test_cli_a.py"
    st = os.stat(path)
    seen = []
    for i in range(6):
        want = "tc8" if i % 2 == 0 else "tc3"
        text = TC8_TEST if want == "tc8" else TC3_TEST
        path.write_text(text)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        obj = graph_inproc(root)
        tcs = node_ids(obj, "tc")
        seen.append(sorted(tcs))
        assert (f"tc:TC-{want[2:]}" in tcs) and (("tc:TC-3" in tcs) != (want == "tc8")), (i, tcs)


def test_tc107_redteam_same_second_in_process_tasks_md(tmp_path):
    root = _tc_project(tmp_path, TC3_TEST)
    path = root / "specs/sim/tasks.md"
    st = os.stat(path)
    graph_inproc(root)
    for i, (old, new) in enumerate([("tc3_", "tc7_"), ("tc7_", "tc3_"), ("tc3_", "tc7_")]):
        text = path.read_text()
        path.write_text(text.replace(f'-k "{old}"', f'-k "{new}"'))
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        obj = graph_inproc(root)
        want = f"tc:TC-{new[2]}"
        assert (want, "task:2") in edges(obj, "verifies", "Verify -k"), (i, new)
        assert (f"tc:TC-{old[2]}", "task:2") not in edges(obj, "verifies", "Verify -k"), (i, old)


def test_tc107_redteam_multibyte_title_same_byte_length(tmp_path):
    root = _tc_project(tmp_path, TC3_TEST, title="ee")
    labels = {n["id"]: n["label"] for n in graph(root)["nodes"]}
    assert labels["task:1"] == "ee"
    replace_keep_stat(root / "specs/sim/tasks.md", "### Task 1: ee", "### Task 1: é")
    labels = {n["id"]: n["label"] for n in graph(root)["nodes"]}
    assert labels["task:1"] == "é"


def test_tc107_redteam_test_plan_row_moves_edge_tasks_md_untouched(tmp_path):
    root, _ = _plan_row_project(tmp_path)
    tasks_md = root / f"specs/{SPEC}/tasks.md"
    tasks_stat = os.stat(tasks_md)
    before = graph(root)
    assert ("tc:TC-9", "task:3") in edges(before, "verifies")
    replace_keep_stat(root / f"specs/{SPEC}/test-plan.md", "tests/test_a.py", "tests/test_b.py")
    assert os.stat(tasks_md).st_mtime_ns == tasks_stat.st_mtime_ns
    after = graph(root)
    assert ("tc:TC-9", "task:4") in edges(after, "verifies")
    assert ("tc:TC-9", "task:3") not in edges(after, "verifies")


def test_tc107_redteam_deleted_test_file_drops_its_edges(tmp_path):
    root = _tc_project(tmp_path, TC3_TEST)
    assert "tc:TC-3" in node_ids(graph(root), "tc")
    (root / "tests/test_cli_a.py").unlink()
    after = graph(root)
    assert ("tc:TC-3", "task:2") not in edges(after, "verifies", "Verifies")
    assert "file:tests/test_cli_a.py" not in {s for s, _ in edges(after, "imports")}


def test_tc107_redteam_backdated_mtime_and_different_size(tmp_path):
    root = _tc_project(tmp_path, TC3_TEST)
    path = root / "tests/test_cli_a.py"
    assert "tc:TC-3" in node_ids(graph(root), "tc")
    path.write_text(TC8_TEST + "\n\n# padding\n")
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))  # far older than the cached build
    after = graph(root)
    assert "tc:TC-8" in node_ids(after, "tc") and "tc:TC-3" not in node_ids(after, "tc")


def test_tc107_redteam_graph_output_stable_when_nothing_changed(tmp_path):
    root = _tc_project(tmp_path, TC3_TEST)
    first = h.ck_run(root, "graph", SPEC, "--json").stdout
    second = h.ck_run(root, "graph", SPEC, "--json").stdout
    assert first and first == second
