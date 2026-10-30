"""Properties of the Bash pre-check through the real hook and a real, module-scoped repo
(prompt-free-runs P-1, P-3, P-4, P-5, P-6).

The repo is built once per module and only ever read by the examples: the pre-check never runs the command, and the
commands generated here are never executed. `guard.tests` is rewritten per example (ck_json), which is the only change.
"""

import json
import shlex
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from conftest import CALC, CALC_TEST, git, write_tasks
from guard_helpers import (
    ck_json,
    claimed_task_worktree,
    commit_on_base,
    decision,
    fake_git,
    guard_events,
    new_on_branch,
    pre,
    worktree_of,
)

PROPERTIES = {
    "P-1": "test_p1_deleting_or_moving_an_existing_test_is_blocked",
    "P-3": "test_p3_near_miss_targets_pass",
    "P-4": "test_p4_cd_prefix_is_resolved",
    "P-5": "test_p5_guard_off_means_silence",
    "P-6": "test_p6_unknown_base_never_asks",
}

SETTINGS = dict(derandomize=True, max_examples=40, database=None, deadline=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
AGENT = "agent-prop"
SESSION = "prop-session"


class Repo:
    """Paths of the module-scoped repo."""

    root: Path
    wt: Path  # a linked worktree under .claude/worktrees
    wt_space: Path  # a linked worktree whose path has a space
    task_wt: Path  # a claimed task worktree (hook cwd stays at root)
    states: dict  # P-6: name -> repo path


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        base = tmp_path_factory.mktemp("guard-prop")
        mp.setenv("CK_CONFIG_HOME", str(base / "prefs"))
        mp.setenv("XDG_DATA_HOME", str(base / "xdg"))
        cfg = base / "gitconfig"
        cfg.write_text("[user]\n\tname = Test User\n\temail = test@example.com\n")
        mp.setenv("GIT_CONFIG_GLOBAL", str(cfg))
        mp.setenv("GIT_CONFIG_NOSYSTEM", "1")
        root = base / "proj"
        root.mkdir()
        git(root, "init", "-q", "-b", "main")
        (root / "calc.py").write_text(CALC)
        (root / "tests").mkdir()
        (root / "tests" / "test_calc.py").write_text(CALC_TEST)
        (root / "tests" / "test calc.py").write_text(CALC_TEST)
        (root / "conftest.py").write_text("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n")
        (root / "specs" / "calc").mkdir(parents=True)
        (root / "specs" / "calc" / "requirements.md").write_text("# Feature: Calc\n- 1.1 WHEN asked THE SYSTEM SHALL subtract\n")
        write_tasks(root)
        (root / "specs" / "ck.json").write_text(json.dumps({"verify": ["python -m pytest -q"], "guard": {"tests": "ask"}}))
        git(root, "add", "-A")
        git(root, "commit", "-qm", "base")
        git(root, "checkout", "-qb", "feature")
        commit_on_base(root, "tests/test_subtract.py", "def test_sub():\n    assert True\n")
        new_on_branch(root, "tests/test_scratch.py", "def test_s():\n    assert 1\n")
        for rel in ("tests/__pycache__/test_calc.cpython-311.pyc", "tests/sub/__pycache__/m.pyc", "tests/.pytest_cache/README.md",
                    "tests/tmp_x.py", "specs/calc/notes.md"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("x\n")
        (root / "build").mkdir()
        # Worktrees come before Task 1 is marked In Progress, so only the main checkout owns test_subtract.py.
        out = Repo()
        out.root = root
        out.wt = worktree_of(root, "w1")
        out.wt_space = root / ".claude" / "worktrees" / "w 2"  # worktree_of() derives the branch from the name: build it here
        git(root, "worktree", "add", "-q", "-b", "wt-w-two", str(out.wt_space))
        out.task_wt = claimed_task_worktree(root, "calc", 1)
        write_tasks(root, t1="In Progress")
        out.states = _unknown_base_states(base)
        yield out


def _unknown_base_states(base: Path) -> dict:
    states = {}
    plain = base / "s-plain"
    (plain / "tests").mkdir(parents=True)
    (plain / "tests" / "test_calc.py").write_text(CALC_TEST)
    states["no-git"] = plain

    def committed(name):
        root = base / name
        (root / "tests").mkdir(parents=True)
        (root / "tests" / "test_calc.py").write_text(CALC_TEST)
        git(root, "init", "-q", "-b", "main")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "base")
        return root

    missing = committed("s-missing-base")
    (missing / "specs").mkdir()
    (missing / "specs" / "ck.json").write_text(json.dumps({"base": "no-such-ref"}))
    states["missing-base"] = missing

    unborn = base / "s-unborn"
    (unborn / "tests").mkdir(parents=True)
    (unborn / "tests" / "test_calc.py").write_text(CALC_TEST)
    git(unborn, "init", "-q", "-b", "main")
    states["unborn"] = unborn

    garbage = committed("s-garbage-head")
    (garbage / ".git" / "HEAD").write_text("garbage \x00 not a ref\n")
    states["garbage-head"] = garbage

    failing = committed("s-git-fails")
    states["git-fails"] = failing
    for path in states.values():
        (path / "specs").mkdir(exist_ok=True)
        ck = path / "specs" / "ck.json"
        if not ck.exists():
            ck.write_text("{}")
    return states


# --------------------------------------------------------------------------- generators

VERBS = ["rm {t}", "rm -f {t}", "rm -rf {t}", "rm -- {t}", "unlink {t}", "truncate -s 0 {t}", "mv {t} /tmp/ck-dst.py",
         "mv {t} /tmp/ck-dstdir/", "git rm {t}", "git rm -q --cached {t}", "git mv {t} moved.py", "rm -f scratch.tmp {t}",
         "mv a.py b.py {t} /tmp/ck-dstdir/"]
FILE_ONLY = ("unlink", "truncate")
caller_st = st.sampled_from(["main", "agent", "headless"])
mode_st = st.sampled_from(["ask", "deny"])


def prefixes(root: Path):
    return st.sampled_from(["", f"cd {root} && ", f"cd {root}; ", f'cd "{root}" && ', "pytest -q && ", "true || ", "x; ", "echo hi\n"])


def apply_verb(verb: str, target: str) -> str:
    return verb.format(t=target)


def call(root: Path, command: str, caller: str, mp):
    if caller == "headless":
        mp.setenv("CK_RUN_HEADLESS", "1")
        return pre(root, SESSION, "Bash", command=command)
    return pre(root, SESSION, "Bash", agent_id=AGENT if caller == "agent" else None, command=command)


def expected(caller: str, mode: str) -> str:
    return "deny" if caller in ("agent", "headless") else mode


def quoted(path: str) -> str:
    return shlex.quote(path) if " " in path else path


def test_the_module_repo_is_what_the_properties_assume(repo):
    assert (repo.root / "tests" / "test calc.py").exists()
    assert (repo.task_wt / "tests" / "test_calc.py").exists()
    assert (repo.wt / "tests" / "test_calc.py").exists()
    assert (repo.wt_space / "tests" / "test_calc.py").exists()


# --------------------------------------------------------------------------- P-1


@settings(**SETTINGS)
@given(data=st.data(), verb=st.sampled_from(VERBS), caller=caller_st, mode=mode_st)
def test_p1_deleting_or_moving_an_existing_test_is_blocked(repo, data, verb, caller, mode):
    root = repo.root
    targets = [
        "tests/test_calc.py", "./tests/test_calc.py", "tests/../tests/test_calc.py", f"{root}/tests/test_calc.py",
        "'tests/test_calc.py'", '"tests/test_calc.py"', quoted("tests/test calc.py"), f"{repo.task_wt}/tests/test_calc.py",
        "tests", "tests/", "./tests",
    ]
    target = data.draw(st.sampled_from(targets))
    if verb.split()[0] in FILE_ONLY and not target.rstrip("'\"").endswith(".py"):
        target = "tests/test_calc.py"
    command = data.draw(prefixes(root)) + apply_verb(verb, target)
    ck_json(root, guard={"tests": mode})
    before = (root / "tests" / "test_calc.py").read_text()
    with pytest.MonkeyPatch.context() as mp:
        result = call(root, command, caller, mp)
    assert decision(result) == expected(caller, mode), command
    assert (root / "tests" / "test_calc.py").read_text() == before
    assert (repo.task_wt / "tests" / "test_calc.py").exists()


@settings(**SETTINGS)
@given(prefix=st.sampled_from(["", "cd {r} && "]), caller=caller_st, mode=mode_st)
def test_p1_git_dash_c_forms(repo, prefix, caller, mode):
    root = repo.root
    ck_json(root, guard={"tests": mode})
    command = prefix.format(r=root) + f"git -C {root} rm tests/test_calc.py"
    with pytest.MonkeyPatch.context() as mp:
        assert decision(call(root, command, caller, mp)) == expected(caller, mode), command
    command = f"git -C {repo.task_wt} rm tests/test_calc.py"
    with pytest.MonkeyPatch.context() as mp:
        assert decision(call(root, command, caller, mp)) == expected(caller, mode), command


# --------------------------------------------------------------------------- P-3

NEAR_MISS = ["tests/test_scratch.py", "tests/test_subtract.py", "tests/__pycache__/test_calc.cpython-311.pyc", "tests/__pycache__",
             "tests/sub/__pycache__", "tests/.pytest_cache", "tests/tmp_x.py", "calc.py", "build/", "specs/calc/notes.md"]


@settings(**SETTINGS)
@given(verb=st.sampled_from(VERBS), target=st.sampled_from(NEAR_MISS), caller=caller_st, mode=mode_st,
       prefix=st.sampled_from(["", "cd {r} && ", "cd {r}; ", "pytest -q && "]))
def test_p3_near_miss_targets_pass(repo, verb, target, caller, mode, prefix):
    root = repo.root
    if verb.split()[0] in FILE_ONLY and target.endswith(("/", "cache", "__pycache__")):
        target = "calc.py"
    command = prefix.format(r=root) + apply_verb(verb, target)
    ck_json(root, guard={"tests": mode})
    with pytest.MonkeyPatch.context() as mp:
        assert call(root, command, caller, mp) is None, command


# --------------------------------------------------------------------------- P-4

C_FORMS = ["rm {r}", "git rm {r}", "mv {r} /tmp/ck-dst.py"]


def d_and_rel(repo):
    here = ["tests/test_calc.py", "tests/test_scratch.py", "calc.py"]
    return st.one_of(
        st.tuples(st.sampled_from(["."]), st.sampled_from(here)),
        st.tuples(st.just("tests"), st.sampled_from(["test_calc.py", "test_scratch.py"])),
        st.tuples(st.just(str(repo.root)), st.sampled_from(here)),
        st.tuples(st.just(str(repo.wt)), st.sampled_from(here)),
        st.tuples(st.just(str(repo.task_wt)), st.sampled_from(here)),
        st.tuples(st.just(str(repo.wt_space)), st.sampled_from(here)),
    )


@settings(**SETTINGS)
@given(data=st.data(), form=st.sampled_from(C_FORMS), caller=st.sampled_from(["main", "agent"]), mode=mode_st)
def test_p4_cd_prefix_is_resolved(repo, data, form, caller, mode):
    root = repo.root
    d, rel = data.draw(d_and_rel(repo))
    ck_json(root, guard={"tests": mode})
    qd = shlex.quote(d) if " " in d else d
    joined = form.format(r=shlex.quote(f"{d}/{rel}") if " " in d else f"{d}/{rel}")
    commands = [f"cd {qd} && " + form.format(r=rel), f"cd {qd}; " + form.format(r=rel), joined]
    with pytest.MonkeyPatch.context() as mp:
        results = [decision(call(root, c, caller, mp)) for c in commands]
    assert results[0] == results[1] == results[2], (commands, results)


# --------------------------------------------------------------------------- P-5

OFF_COMMANDS = [
    "rm tests/test_calc.py", "rm -rf tests", "git rm -q tests/test_calc.py", "mv tests/test_calc.py /tmp/x.py",
    "sed -i 's/a/b/' tests/test_calc.py", "sed -i '/assert add(1, 2)/d' tests/test_calc.py", "perl -pi -e 's/assert//' tests/test_calc.py",
    "python - <<'EOF'\nopen('tests/test_calc.py','w').write('')\nEOF", "echo '' > tests/test_calc.py",
    "cat > tests/test_calc.py <<EOF\ndef test_a():\n    pass\nEOF",
    "find tests -name '*.py' -exec sed -i 's/assert//' {} +", "sed -i 's/def test_add/def xtest_add/' tests/test_calc.py",
]


@settings(**SETTINGS)
@given(command=st.sampled_from(OFF_COMMANDS), caller=caller_st)
def test_p5_guard_off_means_silence(repo, command, caller):
    from claude_kiro.cli.runner import execute_hook
    from guard_helpers import hook_input

    root = repo.root
    ck_json(root, guard={"tests": "off"})
    with pytest.MonkeyPatch.context() as mp:
        assert call(root, command, caller, mp) is None
        first = hook_input(root, SESSION, "Bash", agent_id=None if caller == "main" else AGENT, command=command)
        post = hook_input(root, SESSION, "Bash", agent_id=None if caller == "main" else AGENT, tool_use_id=first["tool_use_id"],
                          event="PostToolUse", tool_response={"stdout": "", "stderr": "", "interrupted": False}, command=command)
        assert execute_hook("post-bash-check", post) is None
    assert guard_events() == []


# --------------------------------------------------------------------------- P-6


@settings(**SETTINGS)
@given(state=st.sampled_from(["no-git", "missing-base", "unborn", "garbage-head", "git-fails"]), verb=st.sampled_from(VERBS),
       target=st.sampled_from(["tests/test_calc.py", "tests", "tests/"]), caller=caller_st, mode=mode_st)
def test_p6_unknown_base_never_asks(repo, tmp_path_factory, state, verb, target, caller, mode):
    root = repo.states[state]
    if verb.split()[0] in FILE_ONLY and target != "tests/test_calc.py":
        target = "tests/test_calc.py"
    ck_json(root, guard={"tests": mode})
    command = apply_verb(verb, target)
    with pytest.MonkeyPatch.context() as mp:
        if state == "git-fails":
            bin_dir = tmp_path_factory.mktemp("failing-git-bin")
            fake_git(bin_dir, "fail")
            mp.setenv("PATH", f"{bin_dir}:" + __import__("os").environ["PATH"])
        result = call(root, command, caller, mp)
    assert result is None, (state, command, result)
