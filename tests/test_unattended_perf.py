"""Performance budgets of the unattended-correctness spec: TC-169 (attribution), TC-170 (git hooks), TC-109 (graph).

All three are timing cases (`@pytest.mark.serial`): `specs/ck.json` runs them in a separate, non-parallel verify step
(`uv run pytest -q -m serial`). They run the real seams, the real ck CLI and real git with the real installed hooks.
Inputs are generated deterministically (fixed counts, no randomness). Project modules are imported inside the test
bodies, so the file collects before `attribution.py`, `graph.py` and `protect.py` exist.
"""

import json
import statistics
import subprocess
import time
from pathlib import Path

import pytest

import unattended_helpers as h

ATTRIBUTION_BUDGET_S = 2.0
GATE_BUDGET_S = 2.0
HOOK_OVERHEAD_BUDGET_S = 0.050
INTEGRATE_UPDATE_BUDGET_S = 1.0
GRAPH_BUDGET_S = 3.0

PAIRS = 50  # 50 test tasks + 50 impl tasks = a 100-task spec
TESTS_PER_FILE = 40  # 50 files x 40 tests = 2,000 tests
FAILING_PER_FILE = 4  # 50 x 4 = 200 failures


def _node(k: int, i: int) -> str:
    return f"tests/test_g{k}.py::test_tc{k}_case{i}"


def _task_blocks(runner: str = "python -m pytest -q") -> list:
    """Test task 2k-1 owns tests/test_g{k}.py and verifies impl task 2k; impl task 2k selects `-k tc{k}_`."""
    from test_lint import block

    out = []
    for k in range(1, PAIRS + 1):
        out.append(dict(num=2 * k - 1, title=f"tests {k}", track="test", files=[f"tests/test_g{k}.py"],
                        verify=f"{runner} tests/test_g{k}.py", verifies=f"Task {2 * k}",
                        status="Done" if k % 2 else "Not Started"))
        out.append(dict(num=2 * k, title=f"impl {k}", files=[f"src/mod{k}.py"], deps=f"Task {2 * k - 1}",
                        verify=f'sh stub.sh -k "tc{k}_"', vby=f"Task {2 * k - 1}",
                        status="Done" if k % 2 else "Not Started"))
    assert block  # imported only to fail loudly when the helper module is missing
    return out


def _test_files() -> dict:
    return {f"tests/test_g{k}.py": "".join(f"def test_tc{k}_case{i}():\n    assert True\n"
                                           for i in range(TESTS_PER_FILE)) for k in range(1, PAIRS + 1)}


def _failure_lines() -> list:
    return [f"FAILED {_node(k, i)} - AssertionError: assert 1 == 2"
            for k in range(1, PAIRS + 1) for i in range(FAILING_PER_FILE)]


@pytest.mark.serial
def test_tc169_attribution_seams_under_two_seconds(tmp_path):
    from claude_kiro import attribution, graph
    from claude_kiro.failures import Failure
    from claude_kiro.hooks._shared.spec_parser import parse_tasks

    root = h.spec_graph_project(tmp_path, tasks=_task_blocks(), tests=_test_files())
    spec_dir = root / "specs" / "sim"
    tasks = parse_tasks(spec_dir / "tasks.md")
    built = graph.build_graph(root, spec_dir)
    index = attribution.AttributionIndex.from_graph(built, tasks)
    done = frozenset(str(2 * k) for k in range(1, PAIRS + 1) if k % 2)
    failures = [
        Failure(node=_node(k, i), file=f"tests/test_g{k}.py", kind="assertion", message="assert 1 == 2",
                truncated=False, command="python -m pytest -q", exc_type="AssertionError", phase="call",
                crash_path=f"tests/test_g{k}.py", crash_line=2, detail="", missing_command=None)
        for k in range(1, PAIRS + 1) for i in range(FAILING_PER_FILE)
    ]
    assert len(failures) == 200 and PAIRS * TESTS_PER_FILE == 2000

    start = time.perf_counter()
    attributions = [attribution.attribute_failure(f, index, done, frozenset()) for f in failures]
    plan = attribution.rerun_plan(attributions)
    verdict = attribution.gate_verdict(attributions, {item.key: "failed" for item in plan}, True)
    elapsed = time.perf_counter() - start

    assert verdict.red is True and len(attributions) == 200
    assert elapsed < ATTRIBUTION_BUDGET_S, f"attribute + rerun_plan + gate_verdict took {elapsed:.2f}s"


@pytest.mark.serial
def test_tc169_attributed_gate_cli_minus_stub_under_two_seconds(tmp_path):
    # Every command the gate can run (suite, test-task Verify, impl Verify, reruns) goes through the stub, which
    # prints only the failures its arguments select: the gate sees 200 unique failures and no real test time.
    root = h.spec_graph_project(tmp_path, tasks=_task_blocks("sh stub.sh"), tests=_test_files())
    recorded = "\n".join(["=== short test summary info ==="] + _failure_lines()
                         + [f"{PAIRS * FAILING_PER_FILE} failed, {PAIRS * (TESTS_PER_FILE - FAILING_PER_FILE)} passed in 3.20s"])
    (root / "recorded.txt").write_text(recorded + "\n")
    (root / "stub.sh").write_text(
        '#!/bin/sh\n'
        'd="$(dirname "$0")"\n'
        'echo run >> "$d/stub-count.txt"\n'
        'pat=""\n'
        'while [ $# -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    -k) shift; pat="test_$1";;\n'
        '    tests/*) pat="$1::";;\n'
        '  esac\n'
        '  shift\n'
        'done\n'
        'if [ -z "$pat" ]; then cat "$d/recorded.txt"; exit 1; fi\n'
        'echo "=== short test summary info ==="\n'
        'grep -F -- "$pat" "$d/recorded.txt" | grep "^FAILED"\n'
        'exit 1\n')
    (root / "specs" / "ck.json").write_text(json.dumps({"verify": ["sh stub.sh"]}) + "\n")
    (root / ".gitignore").write_text("stub-count.txt\n")
    h.commit_all(root, "stub verify")

    # What one stub run costs on this machine, measured the way the gate starts it.
    runs = []
    for _ in range(5):
        t = time.perf_counter()
        subprocess.run(["sh", "stub.sh"], cwd=root, capture_output=True, text=True)
        runs.append(time.perf_counter() - t)
    per_run = statistics.median(runs)
    (root / "stub-count.txt").unlink()

    start = time.perf_counter()
    code, obj, err = h.ck_json(root, "gate", "sim", "--attribute", "--json")
    wall = time.perf_counter() - start

    assert isinstance(obj, dict) and obj.get("schema") == "ck-gate/1", err
    assert code == 1 and obj["failures"], "the recorded failures must reach the gate's verdict"
    stub_runs = (root / "stub-count.txt").read_text().count("\n")
    own = wall - stub_runs * per_run
    assert own < GATE_BUDGET_S, f"gate took {wall:.2f}s with {stub_runs} stub runs of {per_run * 1000:.0f} ms: {own:.2f}s"


def _median_update_ref(root: Path, runs: int = 20) -> float:
    samples = []
    for _ in range(runs):
        t = time.perf_counter()
        out = h.raw(root, "update-ref", "refs/heads/x", "HEAD")
        samples.append(time.perf_counter() - t)
        assert out.returncode == 0, out.stderr
        h.git(root, "update-ref", "-d", "refs/heads/x")
    return statistics.median(samples)


@pytest.mark.serial
def test_tc170_hook_overhead_on_equity_sized_repo(tmp_path):
    from claude_kiro import protect

    root = h.equity_sized_repo(tmp_path)
    _median_update_ref(root, 3)  # warm the file cache for both measurements
    without = _median_update_ref(root)

    status = protect.install_hooks(root)
    assert not status.problems, status.problems
    assert h.git(root, "config", "core.hooksPath").strip(), "the hooks must be active for the timed runs"
    with_hooks = _median_update_ref(root)

    overhead = with_hooks - without
    assert overhead < HOOK_OVERHEAD_BUDGET_S, (
        f"median update-ref: {with_hooks * 1000:.1f} ms with hooks, {without * 1000:.1f} ms without")


@pytest.mark.serial
def test_tc170_integrate_branch_allowed_change_under_one_second(tmp_path):
    from claude_kiro import protect

    root = h.equity_sized_repo(tmp_path)
    base = h.ref(root, "main")
    h.git(root, "checkout", "-q", "-b", "side")
    (root / "src" / "pkg0" / "mod0.py").write_text("file 0 changed\n")
    h.commit_all(root, "allowed change")
    child = h.ref(root, "side")
    h.git(root, "checkout", "-q", "main")
    assert child and child != base

    # Created before the hooks exist: a creation needs a landing token or an approved state once they do.
    created = h.raw(root, "update-ref", "refs/heads/integrate/demo", base)
    assert created.returncode == 0, created.stderr
    status = protect.install_hooks(root)
    assert not status.problems, status.problems

    start = time.perf_counter()
    out = h.raw(root, "update-ref", "refs/heads/integrate/demo", child, base)
    elapsed = time.perf_counter() - start

    assert out.returncode == 0, f"an allowed change to integrate/demo must pass: {out.stderr}"
    assert h.ref(root, "integrate/demo") == child
    assert elapsed < INTEGRATE_UPDATE_BUDGET_S, f"update-ref on integrate/demo took {elapsed:.2f}s"


@pytest.mark.serial
def test_tc109_graph_build_100_tasks_200_test_files_under_three_seconds(tmp_path):
    blocks = []
    tests = {}
    for n in range(1, 101):
        blocks.append(dict(num=n, title=f"task {n}", files=[f"src/m{n}.py"], deps="None" if n == 1 else f"Task {n - 1}",
                           verify=f'python -m pytest -q -k "tc{n}_"'))
    for f in range(200):
        tests[f"tests/test_p{f}.py"] = (f"from src.m{f // 2 + 1} import x\n\n\n"
                                        f"def test_tc{f // 2 + 1}_case{f}():\n    assert x\n")
    root = h.spec_graph_project(tmp_path, tasks=blocks, tests=tests, sources={f"src/m{n}.py": "x = 1\n" for n in range(1, 101)})

    def wall(*args: str) -> float:
        t = time.perf_counter()
        h.ck_run(root, *args)
        return time.perf_counter() - t

    baseline = statistics.median(wall("--version") for _ in range(3))
    start = time.perf_counter()
    code, obj, err = h.ck_json(root, "graph", "sim", "--json")
    graph_wall = time.perf_counter() - start

    assert code == 0 and isinstance(obj, dict), err
    assert len([n for n in obj["nodes"] if n["id"].startswith("task:")]) == 100
    assert graph_wall - baseline < GRAPH_BUDGET_S, f"ck graph {graph_wall:.2f}s vs ck --version {baseline:.2f}s"
