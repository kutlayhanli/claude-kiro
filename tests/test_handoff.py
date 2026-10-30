"""`ck handoff` and `ck run --command`: whole-spec runs started from an isolated session are handed to `ck run`
(TC-44..TC-48, TC-83..TC-85; requirements 3.2, 8.1-8.9).

Real git worktrees, the real CLI in a subprocess, a fake `claude` and fake sandbox tools on the PATH the child
inherits. No project code is mocked.
"""

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from guard_helpers import CK, fake_sandbox_tools, sleeping_fake_claude, worktree_of
from test_run import repo

SPEC_FILES = ("requirements.md", "design.md", "test-plan.md", "tasks.md")
TASKS = "# Tasks\n\n### Task 1: a\n**Status:** Not Started\n**Dependencies:** None\n"
IDENTITY = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com"}
HEADLESS_VARS = ("CK_RUN_HEADLESS", "CLAUDE_CODE_ENTRYPOINT")


def git(cwd, *args, check=True):
    env = {**os.environ, **IDENTITY}
    done = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True)
    if check:
        assert done.returncode == 0, (args, done.stdout, done.stderr)
    return done.stdout.strip()


def project(base: Path) -> Path:
    """`repo()` from test_run, with `demo` committed on `main` and the run directories ignored."""
    root = repo(base)
    git(root, "branch", "-M", "main")
    (root / ".gitignore").write_text(".claude/worktrees/\n.claude/ck-runs/\n.ck/\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "demo spec")
    return root


def write_spec(where: Path, name: str, extra: str = "") -> None:
    d = where / "specs" / name
    d.mkdir(parents=True, exist_ok=True)
    for f in SPEC_FILES:
        (d / f).write_text(TASKS + extra if f == "tasks.md" else f"# {f}\n{extra}")


def commit_all(where: Path, msg: str) -> str:
    git(where, "add", "-A")
    git(where, "commit", "-qm", msg)
    return git(where, "rev-parse", "HEAD")


def tool_env(tmp: Path, *, tools: bool = True) -> dict:
    """The environment a child inherits. With `tools` the PATH starts with fake bwrap/socat; without, the PATH holds
    only git and the fake claude, so the sandbox check cannot be satisfied by this machine's tools."""
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in HEADLESS_VARS}
    env.update(IDENTITY)
    env["HOME"] = str(home)
    if tools:
        fake_sandbox_tools(bin_dir)
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    else:
        for tool in ("bwrap", "socat"):
            (bin_dir / tool).unlink(missing_ok=True)
        link = bin_dir / "git"
        if not link.exists():
            link.symlink_to(shutil.which("git"))
        env["PATH"] = str(bin_dir)
    return env


def fake_claude(tmp: Path, name: str = "rec") -> Path:
    record = tmp / f"{name}.json"
    record.unlink(missing_ok=True)
    sleeping_fake_claude(tmp / "bin", record)
    return record


def early_exit_claude(tmp: Path, record: Path) -> None:
    body = (f"#!{sys.executable}\nimport json, os, sys\n"
            f"open({str(record)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'cwd': os.getcwd()}}))\nsys.exit(1)\n")
    path = tmp / "bin" / "claude"
    path.write_text(body)
    path.chmod(0o755 | stat.S_IXUSR)


def ck(cwd: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([*CK, *args], cwd=str(cwd), env=env, capture_output=True, text=True)


def handoff(cwd: Path, env: dict, *args: str):
    done = ck(cwd, env, "handoff", *args, "--json")
    try:
        data = json.loads(done.stdout)
    except ValueError:
        pytest.fail(f"not JSON (exit {done.returncode}): {done.stdout!r} {done.stderr!r}")
    return done, data


def wait_record(record: Path, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if record.exists():
            try:
                return json.loads(record.read_text())
            except ValueError:
                pass  # half-written
        time.sleep(0.1)
    pytest.fail(f"fake claude never recorded a run in {timeout} s")


def prompt_of(rec: dict) -> str:
    argv = rec["argv"]
    return argv[argv.index("-p") + 1]


def never_ran_in(record: Path, worktree: Path) -> None:
    time.sleep(0.5)
    if record.exists():
        assert Path(json.loads(record.read_text())["cwd"]).resolve() != worktree.resolve()


def run_processes_with(log: str):
    """Command lines of live `ck run` processes that carry `log`."""
    found = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            args = (p / "cmdline").read_bytes().decode(errors="replace").rstrip("\0").split("\0")
        except OSError:
            continue
        if log in args and "claude_kiro.cli" in " ".join(args):
            found.append(args)
    return found


@pytest.fixture
def setup(tmp_path):
    root = project(tmp_path)
    wt = worktree_of(root, "bg-1")
    env = tool_env(tmp_path)
    record = fake_claude(tmp_path)
    return root, wt, env, record


# --------------------------------------------------------------------------- TC-44


def test_tc44_whole_spec_run_is_launched_from_the_main_checkout(setup):
    root, wt, env, record = setup
    done, data = handoff(wt, env, "implement", "demo")
    assert done.returncode == 0, done.stdout + done.stderr
    assert data["handoff"] is True and data["launched"] is True
    assert Path(data["mainCheckout"]).resolve() == root.resolve()
    rec = wait_record(record)
    assert Path(rec["cwd"]).resolve() == root.resolve()
    prompt = prompt_of(rec)
    assert prompt == "/spec:implement demo all"
    assert "--log" not in prompt and "--log" not in rec["argv"]
    log = Path(data["log"])
    assert log.exists(), data
    assert log.parent.resolve() == (root / ".claude" / "ck-runs").resolve() and log.name.startswith("demo-")
    assert data["follow"] == f"tail -f {data['log']}"
    assert "ck worktree report demo" in data["report"] and "ck stats show" in data["report"]


# --------------------------------------------------------------------------- TC-45


def dry(cwd, env, kind, spec="demo"):
    return ck(cwd, env, "run", spec, "--command", kind, "--dry-run", "--json")


def test_tc45_create_and_review_runs_match_implement(setup):
    root, wt, env, _ = setup
    plans = {}
    for kind in ("implement", "create", "review"):
        done = dry(root, env, kind)
        assert done.returncode == 0, done.stdout + done.stderr
        plans[kind] = json.loads(done.stdout)
    base = plans["implement"]
    assert base["argv"][2] == "/spec:implement demo all"
    assert base["env"].get("CK_RUN_HEADLESS") == "1"
    for kind in ("create", "review"):
        plan = plans[kind]
        assert plan["argv"][2] == f"/spec:{kind} demo"
        assert plan["argv"][:2] == base["argv"][:2] and plan["argv"][3:] == base["argv"][3:]
        assert plan["env"] == base["env"] and plan["env"].get("CK_RUN_HEADLESS") == "1"
    argv = base["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "auto"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert "Workflow" in argv[argv.index("--allowedTools") + 1].split(",")
    assert "--max-budget-usd" in argv and "--max-turns" in argv


def test_tc45_create_and_review_hand_off_with_their_own_prompt(setup):
    root, wt, env, record = setup
    for kind in ("create", "review"):
        record.unlink(missing_ok=True)
        done, data = handoff(wt, env, kind, "demo")
        assert done.returncode == 0 and data["launched"] is True, done.stdout + done.stderr
        assert prompt_of(wait_record(record)) == f"/spec:{kind} demo"
        time.sleep(3.5)  # let this fake claude finish before the next launch


def test_tc45_refusals_apply_to_create_and_review(setup, tmp_path):
    root, wt, env, _ = setup
    no_tools = tool_env(tmp_path, tools=False)
    for kind in ("create", "review"):
        cases = [
            (lambda k: dry(root, env, k, "nope"), lambda k: dry(root, env, "implement", "nope")),
            (lambda k: dry(wt, env, k), lambda k: dry(wt, env, "implement")),
            (lambda k: dry(root, no_tools, k), lambda k: dry(root, no_tools, "implement")),
        ]
        for mine, reference in cases:
            a, b = mine(kind), reference(kind)
            assert b.returncode != 0, b.stdout + b.stderr
            assert a.returncode == b.returncode, (kind, a.stdout + a.stderr)


# --------------------------------------------------------------------------- TC-46


def assert_failed(done, data, root, wt, record):
    assert done.returncode == 3, done.stdout + done.stderr
    assert data["handoff"] is True and data["launched"] is False
    assert data["error"]
    assert "ck run demo --command implement" in data["manualCommand"], data
    never_ran_in(record, wt)


def test_tc46_sandbox_tools_missing(setup, tmp_path):
    root, wt, _, record = setup
    env = tool_env(tmp_path, tools=False)
    sleeping_fake_claude(tmp_path / "bin", record)
    done, data = handoff(wt, env, "implement", "demo")
    assert_failed(done, data, root, wt, record)
    assert f"cd {root}" in data["manualCommand"]


def test_tc46_ck_run_refuses_an_unknown_spec(setup):
    root, wt, env, record = setup
    done, data = handoff(wt, env, "implement", "nope")
    assert done.returncode == 3, done.stdout + done.stderr
    assert data["launched"] is False and data["error"] and "ck run nope --command implement" in data["manualCommand"]


def test_tc46_main_checkout_not_found(setup):
    root, wt, env, record = setup
    shutil.rmtree(root / ".git" / "worktrees" / "bg-1")
    done, data = handoff(wt, env, "implement", "demo")
    assert done.returncode == 3, done.stdout + done.stderr
    assert data["handoff"] is True and data["launched"] is False and data["error"]
    assert "ck run demo --command implement" in data["manualCommand"]
    never_ran_in(record, wt)


def test_tc46_child_exits_at_once(setup, tmp_path):
    root, wt, env, record = setup
    early_exit_claude(tmp_path, record)
    done, data = handoff(wt, env, "implement", "demo")
    assert_failed(done, data, root, wt, record)
    assert Path(json.loads(record.read_text())["cwd"]).resolve() == root.resolve()


# --------------------------------------------------------------------------- TC-47


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("handoff-shared")
    root = project(tmp)
    return {
        "root": root,
        "sub": root / "specs",
        "claude": worktree_of(root, "bg-1"),
        "ck": worktree_of(root, "demo-task-1", under=".ck/worktrees"),
        "outside": worktree_of(root, "elsewhere", under=str(tmp / "outside")),
        "tmp": tmp,
    }


@pytest.mark.parametrize("where, extra", [("root", ()), ("sub", ()), ("ck", ()), ("outside", ()), ("claude", ("3",))])
def test_tc47_no_handoff_when_not_needed(shared, where, extra):
    env = tool_env(shared["tmp"])
    fake_claude(shared["tmp"], "never")
    done, data = handoff(shared[where], env, "implement", "demo", *extra)
    assert done.returncode == 0, done.stdout + done.stderr
    assert data["handoff"] is False
    assert not (shared["root"] / ".claude" / "ck-runs").exists()


# --------------------------------------------------------------------------- TC-48


def test_tc48_options_stay_before_the_separator(setup):
    root, wt, env, record = setup
    proc = subprocess.Popen([*CK, "handoff", "implement", "demo", "--escalate", "sonnet,opus", "--max-concurrent", "6", "--json"],
                            cwd=str(wt), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    argv = None
    deadline = time.monotonic() + 20
    log = None
    while time.monotonic() < deadline and argv is None and proc.poll() is None:
        for p in (root / ".claude" / "ck-runs").glob("demo-*.log"):
            log = str(p)
        if log:
            found = run_processes_with(log)
            argv = found[0] if found else None
        time.sleep(0.05)
    out, err = proc.communicate(timeout=30)
    data = json.loads(out)
    assert data["launched"] is True, out + err
    assert argv is not None, "never saw the launched ck run"
    assert prompt_of(wait_record(record)) == "/spec:implement demo all --escalate sonnet,opus --max-concurrent 6"
    sep = argv.index("--")
    before, after = argv[:sep], argv[sep + 1:]
    assert before[before.index("--command") + 1] == "implement"
    assert before[before.index("--log") + 1] == data["log"]
    assert after == ["--escalate", "sonnet,opus", "--max-concurrent", "6"]


def test_tc48_argument_with_spaces_and_quotes_survives(setup):
    root, wt, env, record = setup
    tricky = 'say "hi" it\'s'
    done, data = handoff(wt, env, "implement", "demo", "--note", tricky)
    assert done.returncode == 0 and data["launched"] is True, done.stdout + done.stderr
    prompt = prompt_of(wait_record(record))
    assert shlex.split(prompt) == ["/spec:implement", "demo", "all", "--note", tricky]


# --------------------------------------------------------------------------- TC-83..TC-85


def head_of(where: Path) -> str:
    return git(where, "rev-parse", "HEAD")


@pytest.fixture
def branch_only_spec(setup):
    root, wt, env, record = setup
    write_spec(wt, "newspec")
    sha = commit_all(wt, "add newspec")
    return root, wt, env, record, sha


def test_tc83_spec_only_on_the_worktree_branch_runs_on_a_run_branch(branch_only_spec):
    root, wt, env, record, sha = branch_only_spec
    main_head = head_of(root)
    done, data = handoff(wt, env, "implement", "newspec")
    assert done.returncode == 0, done.stdout + done.stderr
    assert data["launched"] is True and data["runBranch"] == "newspec-run"
    assert git(root, "branch", "--show-current") == "newspec-run"
    assert head_of(root) == sha != main_head
    rec = wait_record(record)
    assert Path(rec["cwd"]).resolve() == root.resolve()
    assert prompt_of(rec) == "/spec:implement newspec all"


def test_tc83_a_differing_file_also_gets_a_run_branch(setup):
    root, wt, env, record = setup
    (wt / "specs" / "demo" / "tasks.md").write_text(TASKS + "\nchanged in the worktree\n")
    sha = commit_all(wt, "edit demo")
    done, data = handoff(wt, env, "implement", "demo")
    assert done.returncode == 0 and data["runBranch"] == "demo-run", done.stdout + done.stderr
    assert git(root, "branch", "--show-current") == "demo-run" and head_of(root) == sha
    assert prompt_of(wait_record(record)) == "/spec:implement demo all"


# --------------------------------------------------------------------------- TC-84


def snapshot(root: Path):
    return (git(root, "branch", "--show-current"), head_of(root), git(root, "status", "--porcelain"))


def refusal(branch_only_spec, expected_error_words):
    root, wt, env, record, sha = branch_only_spec
    before = snapshot(root)
    wt_before = snapshot(wt)
    done, data = handoff(wt, env, "implement", "newspec")
    assert done.returncode == 3, done.stdout + done.stderr
    assert data["handoff"] is True and data["launched"] is False
    assert data["error"] and any(w in data["error"].lower() for w in expected_error_words), data["error"]
    assert f"git switch -c newspec-run {sha}" in data["manualCommand"], data
    assert "ck run newspec --command implement" in data["manualCommand"], data
    assert snapshot(root) == before
    assert snapshot(wt) == wt_before
    never_ran_in(record, wt)
    assert not record.exists()


def test_tc84_dirty_main_checkout(branch_only_spec):
    root = branch_only_spec[0]
    (root / ".gitignore").write_text((root / ".gitignore").read_text() + "# local edit\n")
    refusal(branch_only_spec, ("main", "uncommitted", "commit", "stash"))


def test_tc84_dirty_worktree_spec(branch_only_spec):
    wt = branch_only_spec[1]
    (wt / "specs" / "newspec" / "design.md").write_text("# edited, not committed\n")
    refusal(branch_only_spec, ("worktree", "uncommitted", "commit"))


def test_tc84_run_branch_already_taken(branch_only_spec):
    root = branch_only_spec[0]
    git(root, "branch", "newspec-run")  # at the main checkout's HEAD, not the worktree's
    refusal(branch_only_spec, ("newspec-run", "branch", "delete", "rename"))


# --------------------------------------------------------------------------- TC-85


def test_tc85_commits_outside_the_spec_run_on_the_current_branch(setup):
    root, wt, env, record = setup
    (wt / "other.txt").write_text("not the spec\n")
    commit_all(wt, "other work")
    before = snapshot(root)
    done, data = handoff(wt, env, "implement", "demo")
    assert done.returncode == 0 and data["launched"] is True and data["runBranch"] is None, done.stdout + done.stderr
    assert snapshot(root) == before and before[0] == "main"
    assert Path(wait_record(record)["cwd"]).resolve() == root.resolve()


def test_tc85_worktree_at_the_main_head(setup):
    root, wt, env, record = setup
    assert head_of(wt) == head_of(root)
    before = snapshot(root)
    done, data = handoff(wt, env, "implement", "demo")
    assert done.returncode == 0 and data["launched"] is True and data["runBranch"] is None, done.stdout + done.stderr
    assert snapshot(root) == before and before[0] == "main"
