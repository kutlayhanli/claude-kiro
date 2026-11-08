"""Self-checks of tests/unattended_helpers.py against today's ck (they test the harness, not the feature).

Every check runs real git and the real ck CLI; none mocks a `claude_kiro` module. xdist-safe: own tmp_path repos."""

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import unattended_helpers as h


def run_pytest(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, CK_TEST_COUNTERS=str(root.parent / "counters"), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args], cwd=root,
                          env=env, capture_output=True, text=True)


# --- ck_json --------------------------------------------------------------------


def test_ck_json_parses_json_and_returns_none_for_text(tmp_path):
    root = h.attr_project(tmp_path, statuses={})
    code, obj, _ = h.ck_json(root, "plan", "demo", "--json")
    assert code == 0 and isinstance(obj, dict)
    code, obj, _ = h.ck_json(root, "gate", "demo")  # text report
    assert obj is None and code in (0, 1)


# --- attr_project ---------------------------------------------------------------


def test_attr_project_shape(tmp_path):
    root = h.attr_project(tmp_path, statuses={"1": "Done"}, tests={"test_tc1_a": "pass", "test_tc5_b": "pass"})
    branches = h.raw(root, "branch", "--format=%(refname:short)").stdout.split()
    assert {"main", "integrate/demo"} <= set(branches)
    assert h.raw(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    for rel in ("calc.py", "specs/demo/requirements.md", "specs/demo/test-plan.md", "specs/demo/design.md",
                "specs/demo/tasks.md", "specs/ck.json", "tests/test_calc_tc.py", "tests/test_more.py"):
        assert (root / rel).exists(), rel
    cfg = json.loads((root / "specs/ck.json").read_text())
    assert cfg["verify"] == ["python -m pytest -q -p no:cacheprovider tests"]
    assert h.integration_worktree(root).exists()
    assert h.raw(root, "status", "--porcelain").stdout.strip() == ""


def test_attr_project_tasks_parse_through_ck(tmp_path):
    root = h.attr_project(tmp_path, statuses={"1": "Done"})
    code, plan, err = h.ck_json(root, "plan", "demo", "--json")
    assert code == 0, err
    assert "6" in plan["ready"] and "2" in plan["ready"] and "7" not in plan["ready"]
    tasks = (root / "specs/demo/tasks.md").read_text()
    assert re.search(r"### Task 1:.*?\*\*Status:\*\* Done", tasks, re.S)
    assert "**Verifies:** Task 2, Task 3" in tasks and "**Verifies:** Task 7, Task 8" in tasks
    assert '-k "tc1_ or tc2_"' in tasks and '-k "tc3_"' in tasks and '-k "tc5_"' in tasks


def test_attr_project_extra_tasks_and_config(tmp_path):
    extra = "### Task 9: nine\n**Status:** Not Started\n**Track:** impl\n**Dependencies:** None\n\n---\n\n"
    root = h.attr_project(tmp_path, extra_tasks=extra, config={"claim_stale_minutes": 5})
    assert "### Task 9: nine" in (root / "specs/demo/tasks.md").read_text()
    assert json.loads((root / "specs/ck.json").read_text())["claim_stale_minutes"] == 5


@pytest.mark.parametrize("outcome,expect_fail", [("pass", False), ("fail", True), ("setup-error", True),
                                                 ("sleep:0.05", False)])
def test_generated_outcomes_run_as_named(tmp_path, outcome, expect_fail):
    root = h.attr_project(tmp_path, tests={"test_tc1_x": outcome})
    out = run_pytest(root, "tests/test_calc_tc.py")
    assert (out.returncode != 0) is expect_fail, out.stdout
    if outcome == "setup-error":
        assert "setup boom" in out.stdout and "1 error" in out.stdout


def test_file_level_outcomes_break_collection_of_their_own_file(tmp_path):
    root = h.attr_project(tmp_path, tests={"test_tc1_ok": "pass", "imp": "import-error", "syn": "syntax-error"})
    out = run_pytest(root, "tests/test_calc_tc.py")
    assert out.returncode == 0, out.stdout  # the good file is not affected
    assert "ModuleNotFoundError" in run_pytest(root, "tests/test_imp_file.py").stdout
    assert "SyntaxError" in run_pytest(root, "tests/test_syn_file.py").stdout


def test_flaky_fails_first_run_passes_next_and_counts_runs(tmp_path):
    root = h.attr_project(tmp_path, tests={"test_tc1_flaky": "flaky"})
    assert run_pytest(root, "tests/test_calc_tc.py").returncode != 0
    assert run_pytest(root, "tests/test_calc_tc.py").returncode == 0
    assert run_pytest(root, "tests/test_calc_tc.py").returncode == 0
    assert h.counter_lines(root, "test_tc1_flaky") == 3
    assert (tmp_path / "counters" / "test_tc1_flaky").read_text() == "run\n" * 3


def test_every_generated_test_appends_one_line_per_run(tmp_path):
    root = h.attr_project(tmp_path, tests={"test_tc1_a": "pass", "test_tc2_b": "fail", "test_tc5_c": "pass"})
    run_pytest(root, "tests")
    run_pytest(root, "tests")
    assert [h.counter_lines(root, n) for n in ("test_tc1_a", "test_tc2_b", "test_tc5_c")] == [2, 2, 2]
    assert h.counter_lines(root, "test_never_ran") == 0


@pytest.mark.parametrize("n", [0, 1, 499, 500, 501, 5000])
def test_assert_msg_has_n_characters_multibyte_included(tmp_path, n):
    root = h.attr_project(tmp_path, tests={"test_tc1_msg": f"assert-msg:{n}"})
    src = (root / "tests/test_calc_tc.py").read_text()
    ns: dict = {}
    body = h.pytest_body("test_tc1_msg", f"assert-msg:{n}")
    exec(compile("def _count(name):\n    return 0\n" + body, "x", "exec"), ns)
    with pytest.raises(AssertionError) as exc:
        ns["test_tc1_msg"]()
    assert len(str(exc.value)) == n and (n < 3 or any(ord(c) > 127 for c in str(exc.value)))
    assert "assert False" in src


def test_pytest_body_rejects_unknown_outcome():
    with pytest.raises(ValueError):
        h.pytest_body("test_x", "explode")


def test_node_test_file_passes_and_fails_under_node(tmp_path):
    node = subprocess.run(["node", "--version"], capture_output=True, text=True)
    if node.returncode != 0:
        pytest.fail(f"node is required by the JS harness: {node.stderr}")
    for failing in (False, True):
        f = tmp_path / f"t{int(failing)}.test.mjs"
        f.write_text(h.node_test_file(failing))
        out = subprocess.run(["node", "--test", str(f)], capture_output=True, text=True)
        assert (out.returncode != 0) is failing, out.stdout


# --- hooks_repo and state snapshots ---------------------------------------------


def test_hooks_repo_branches_and_diffs(tmp_path):
    root = h.hooks_repo(tmp_path)
    assert (root / ".claude/settings.json").exists()
    expected = {"1": ["A:.claude/CLAUDE.md"], "2": ["A:.claude/commands/x.md"], "3": ["M:src/a.py"],
                "4": ["A:.claude/commands/ä.md"], "5": ["D:.claude/settings.json"]}
    for n, want in expected.items():
        branch = f"feat/demo-task-{n}"
        assert h.ref(root, branch) and h.ref(root, branch) != h.ref(root, "integrate/demo")
        out = h.raw(root, "-c", "core.quotepath=off", "diff", "--name-status", f"integrate/demo...{branch}")
        got = [f"{ln.split(chr(9))[0]}:{ln.split(chr(9))[1]}" for ln in out.stdout.splitlines()]
        assert got == want, (n, out.stdout)
    assert h.ref(root, "no-such-branch") == ""
    assert h.raw(root, "rev-parse", "nope").returncode != 0


def test_snapshot_restore_round_trip(tmp_path):
    root = h.hooks_repo(tmp_path)
    snap = h.snapshot_ck_state(root)
    assert snap and all(isinstance(v, bytes) for v in snap.values())
    common = Path(h.raw(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    hook_files = [Path(k) for k in snap if k.startswith(str(common / "hooks")) and "sample" not in k]
    victim = next(iter(sorted(snap)))
    victim_path = Path(victim)
    original = snap[victim]
    victim_path.write_bytes(original + b"junk")
    added = common / "ck-added-by-test"
    added.write_text("new")
    gone_path = Path(sorted(snap)[-1])
    gone_bytes = snap[str(gone_path)]
    gone_mode = snap.modes[str(gone_path)]
    gone_path.unlink()
    h.restore_ck_state(root, snap)
    assert victim_path.read_bytes() == original
    assert not added.exists()
    assert gone_path.read_bytes() == gone_bytes and stat.S_IMODE(gone_path.stat().st_mode) == gone_mode
    assert h.snapshot_ck_state(root) == snap
    del hook_files


def test_snapshot_covers_worktree_dir_and_ck_state_written_by_ck(tmp_path):
    root = h.hooks_repo(tmp_path)
    before = h.snapshot_ck_state(root)
    code, _, err = h.ck_text(root, "worktree", "claim", "demo", "1")
    assert code == 0, err
    after = h.snapshot_ck_state(root)
    assert after != before  # the claim wrote something under the common dir
    assert any(k.startswith(str(root / ".ck" / "worktrees")) for k in after)
    h.restore_ck_state(root, before)
    assert h.snapshot_ck_state(root) == before


# --- readonly_git_metadata --------------------------------------------------------


def test_readonly_git_metadata_blocks_writes_and_restores_modes(tmp_path):
    root = h.hooks_repo(tmp_path)
    common = Path(h.raw(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    paths = [common / "config", common / "hooks", *sorted((common / "worktrees").iterdir())]
    modes = {p: stat.S_IMODE(p.stat().st_mode) for p in paths}
    with h.readonly_git_metadata(root):
        for p in paths:
            assert not stat.S_IMODE(p.stat().st_mode) & 0o222, p
        if os.geteuid() != 0:
            with pytest.raises(PermissionError):
                (common / "config").open("a").write("x")
    assert {p: stat.S_IMODE(p.stat().st_mode) for p in paths} == modes
    assert h.raw(root, "config", "ck.test", "1").returncode == 0


def test_readonly_git_metadata_restores_after_an_exception(tmp_path):
    root = h.hooks_repo(tmp_path)
    cfg = Path(h.raw(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()) / "config"
    mode = stat.S_IMODE(cfg.stat().st_mode)
    with pytest.raises(RuntimeError):
        with h.readonly_git_metadata(root):
            raise RuntimeError("boom")
    assert stat.S_IMODE(cfg.stat().st_mode) == mode


# --- sandboxed --------------------------------------------------------------------


def test_sandboxed_skips_only_with_the_bwrap_error_as_reason(tmp_path):
    root = h.hooks_repo(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    stub = bin_dir / "bwrap"
    stub.write_text("#!/bin/sh\necho 'bwrap: No permissions to create new namespace' >&2\nexit 1\n")
    stub.chmod(0o755)
    old = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old}"
    try:
        with pytest.raises(pytest.skip.Exception) as exc:
            h.sandboxed(["true"], root, h.integration_worktree(root))
    finally:
        os.environ["PATH"] = old
    assert "No permissions to create new namespace" in str(exc.value)


def test_sandboxed_command_line_uses_sandbox_settings_binds(tmp_path):
    root = h.hooks_repo(tmp_path)
    wt = h.integration_worktree(root)
    argv = h.bwrap_argv(["git", "status"], root, wt)
    assert argv[0] == "bwrap" and argv[-2:] == ["git", "status"]
    assert argv[1:4] == ["--ro-bind", "/", "/"]
    binds = [(argv[i + 1]) for i, a in enumerate(argv) if a == "--bind"]
    assert str(root.resolve()) in binds
    ro = [(argv[i + 1]) for i, a in enumerate(argv) if a == "--ro-bind"]
    common = Path(h.raw(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    for sub in ("hooks", "config", "worktrees"):
        assert str(common / sub) in ro


def test_sandboxed_runs_where_bwrap_starts_else_the_probe_gives_a_reason(tmp_path):
    root = h.hooks_repo(tmp_path)
    reason = h._bwrap_probe()
    if reason is not None:
        assert reason.strip()  # a host without namespaces: the reason is what a skip would report
        return
    out = h.sandboxed(["git", "merge", "--no-verify", "feat/demo-task-1"], root, h.integration_worktree(root))
    assert isinstance(out, subprocess.CompletedProcess)


# --- progress_run -----------------------------------------------------------------


def _events(run: str) -> list:
    from claude_kiro import progress

    return progress.read_events(progress.run_file(run))[0]


def test_progress_run_ended_and_open_runs(tmp_path):
    root = h.attr_project(tmp_path, statuses={})
    open_run = h.progress_run("demo", "k1", ended=False, last_event_age_min=90, cwd=root)
    kinds = [e["kind"] for e in _events(open_run)]
    assert kinds == ["start"]
    done = h.progress_run("demo", "k2", ended=True, last_event_age_min=5, cwd=root)
    assert [e["kind"] for e in _events(done)] == ["start", "end"]
    assert done != open_run and done.startswith("demo--")


def test_progress_run_last_event_age(tmp_path):
    from datetime import datetime, timezone

    root = h.attr_project(tmp_path, statuses={})
    run = h.progress_run("demo", "k", ended=False, last_event_age_min=120, cwd=root)
    at = _events(run)[-1]["at"]
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(at.replace("Z", "+00:00"))).total_seconds() / 60
    assert 119 <= age <= 125


# --- spec_graph_project -----------------------------------------------------------


def test_spec_graph_project_writes_design_table_tasks_and_runnable_clis(tmp_path):
    from test_lint import block

    forms = h.invocation_forms("thesis new")
    assert 'main(["thesis", "new"])' in forms and 'shlex.split("mtr thesis new")' in forms
    tests = {"tests/test_cli.py": "def test_tc1_x():\n    pass  # " + "; ".join(forms) + "\n"}
    root = h.spec_graph_project(
        tmp_path, tasks=[dict(num=1, title="cli", track="test", files=["tests/test_cli.py"]),
                         dict(num=2, title="impl", files=["src/mtr/cli.py"], deps="Task 1")],
        design_cli={"mtr thesis new": "src/mtr/cli.py", "mtr version": "src/mtr/cli.py"}, tests=tests, sources={})
    design = (root / "specs/sim/design.md").read_text()
    assert "| Command | File |" in design and "`mtr thesis new`" in design
    assert (root / "specs/sim/tasks.md").read_text().count("### Task ") == 2
    assert block(1, "cli", track="test", files=["tests/test_cli.py"]) in (root / "specs/sim/tasks.md").read_text()
    env = dict(os.environ, PYTHONPATH=str(root / "src"))
    out = subprocess.run([sys.executable, "-m", "mtr.cli", "version"], cwd=root, env=env, capture_output=True, text=True)
    assert out.returncode == 0 and "mtr 1.0" in out.stdout, out.stderr
    out = subprocess.run([sys.executable, "-m", "mtr2.cli", "version"], cwd=root, env=env, capture_output=True, text=True)
    assert out.returncode == 0 and "mtr2 1.0" in out.stdout, out.stderr
    assert (root / "tests/test_cli.py").exists()
    code, obj, err = h.ck_json(root, "lint", "sim", "--json")
    assert obj is not None or code in (0, 1), err


def test_spec_graph_project_accepts_block_text(tmp_path):
    from test_lint import block

    root = h.spec_graph_project(tmp_path, tasks=block(1, "one"), design_cli=[("mtr2 report", "src/mtr2/cli.py")])
    assert "### Task 1: one" in (root / "specs/sim/tasks.md").read_text()


# --- fake_claude ------------------------------------------------------------------


def test_fake_claude_records_argv_settings_and_hooks_path(tmp_path):
    root = h.hooks_repo(tmp_path)
    h.git(root, "config", "core.hooksPath", ".husky")
    bin_dir = h.fake_claude(tmp_path)
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    settings = json.dumps({"sandbox": {"enabled": True}})
    out = subprocess.run(["claude", "-p", "hi", "--settings", settings], cwd=root, env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    [rec] = h.fake_claude_records(bin_dir)
    assert rec["argv"][:2] == ["-p", "hi"] and rec["settings"] == {"sandbox": {"enabled": True}}
    assert rec["hooksPath"] == ".husky"


# --- equity_sized_repo ------------------------------------------------------------


def test_equity_sized_repo_counts(tmp_path):
    root = h.equity_sized_repo(tmp_path, files=60, commits=12)
    assert int(h.git(root, "rev-list", "--count", "HEAD").strip()) == 12
    assert len(h.git(root, "ls-files").splitlines()) == 60
    assert h.raw(root, "status", "--porcelain").stdout.strip() == ""


# --- failure.schema.json ----------------------------------------------------------

EXAMPLE_FAILURE = {
    "node": "tests/test_rates.py::test_tc012_rate_floor", "tcId": "TC-12", "tasks": ["31", "40"],
    "attributedBy": "tc", "file": "tests/test_rates.py", "kind": "assertion", "message": "assert 0.05 == 0.04",
    "truncated": False, "command": "uv run pytest -q", "status": "regression", "excusedBy": [], "rerun": "failed",
    "class": None, "check": None, "missingCommand": None,
}


def test_failure_schema_accepts_the_design_example_and_minimal_entry():
    assert h.failure_errors(EXAMPLE_FAILURE) == []
    minimal = {k: EXAMPLE_FAILURE[k] for k in ("node", "tcId", "tasks", "attributedBy", "file", "kind", "message",
                                               "truncated", "command")}
    assert h.failure_errors(minimal) == []
    assert h.failure_errors({**minimal, "node": None, "tcId": None, "hint": "owned by Task 3",
                             "missingCommand": {"command": "mtr x", "providers": ["4"]}}) == []


def test_failure_schema_accepts_command_attribution_and_a_null_file():
    assert h.failure_errors({**EXAMPLE_FAILURE, "attributedBy": "command", "file": None}) == []
    assert h.failure_errors({**EXAMPLE_FAILURE, "file": 3})


@pytest.mark.parametrize("kind", ["assertion", "import", "collection", "timeout", "error"])
def test_failure_schema_accepts_the_five_kinds(kind):
    assert h.failure_errors({**EXAMPLE_FAILURE, "kind": kind}) == []


@pytest.mark.parametrize("kind", ["failure", "Assertion", "", None, 3])
def test_failure_schema_rejects_a_kind_outside_the_five(kind):
    assert h.failure_errors({**EXAMPLE_FAILURE, "kind": kind})


def test_failure_schema_message_limit_is_500_characters_not_bytes():
    assert h.failure_errors({**EXAMPLE_FAILURE, "message": "x" * 500}) == []
    assert h.failure_errors({**EXAMPLE_FAILURE, "message": "x" * 501})
    assert h.failure_errors({**EXAMPLE_FAILURE, "message": "é" * 500}) == []  # 1000 bytes, 500 characters
    assert h.failure_errors({**EXAMPLE_FAILURE, "message": "\U0001F600" * 501})


@pytest.mark.parametrize("key", ["node", "tcId", "tasks", "attributedBy", "file", "kind", "message", "truncated",
                                 "command"])
def test_failure_schema_requires_each_core_field(key):
    entry = dict(EXAMPLE_FAILURE)
    del entry[key]
    assert any(key in e for e in h.failure_errors(entry))


@pytest.mark.parametrize("patch", [{"status": "ok"}, {"attributedBy": "magic"}, {"truncated": "no"}, {"tasks": "3"},
                                   {"tcId": "tc-12"}, {"rerun": "maybe"}, {"class": "worse"}, {"check": "x"}])
def test_failure_schema_rejects_bad_optional_values(patch):
    assert h.failure_errors({**EXAMPLE_FAILURE, **patch})


# --- captured baselines -----------------------------------------------------------


def test_land_json_keys_baseline_is_captured_and_consistent():
    data = json.loads((h.FIXTURES / "land-json-keys.json").read_text())
    assert set(data) == {"paths", "scenarios"} and data["paths"] == sorted(data["paths"])
    for key in ("tasks", "tasks[].task", "tasks[].state", "gates", "gates[].tasks", "gates[].green", "bisected",
                "into", "head", "base", "startedAt", "finishedAt"):
        assert key in data["paths"], key
    assert data["paths"] == sorted({p for ps in data["scenarios"].values() for p in ps})
    assert "tasks[].files" in data["scenarios"]["conflict"]


def test_land_key_paths_helper_flattens_dicts_and_lists():
    assert h.json_key_paths({"a": [{"b": 1}, {"c": {"d": 2}}], "e": []}) == ["a", "a[].b", "a[].c", "a[].c.d", "e"]


def test_gate_text_baselines_exist_for_every_scenario():
    files = {p.stem for p in (h.FIXTURES / "gate-text-baseline").glob("*.txt")}
    assert files == set(h.GATE_BASELINE_SCENARIOS)
    for name in files:
        text = (h.FIXTURES / "gate-text-baseline" / f"{name}.txt").read_text()
        assert re.match(r"exit: [01]\nVerification gate for demo", text), name
        assert "/tmp/" not in text and "pytest-of" not in text  # normalised


def test_readme_names_the_capture_commit():
    readme = (h.FIXTURES / "README.md").read_text()
    sha = re.search(r"\b[0-9a-f]{40}\b", readme)
    assert sha
    assert subprocess.run(["git", "cat-file", "-e", f"{sha.group(0)}^{{commit}}"], cwd=Path(__file__).parent).returncode == 0


def test_land_project_lands_a_task_with_the_real_cli(tmp_path):
    root = h.land_project(tmp_path)
    code, obj, err = h.ck_json(root, "worktree", "land", "demo", "1", "--gate", "--json")
    assert code == 0, err
    assert obj["tasks"][0]["state"] == "MERGED"
