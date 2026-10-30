"""Properties of the shell post-check and the guard log through the real hooks and a real, module-scoped repo
(prompt-free-runs P-7, P-8, P-9, P-10, P-21).

The repo is built once per module. Examples run real commands in it (`bash -c`) after the real PreToolUse hook, then
the real PostToolUse hook, and `reset()` puts the repo back to its starting commits before the next example. Each
example uses its own session id, so the per-agent de-duplication never carries over.
"""

import hashlib
import json
import os
import shlex
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from conftest import CALC, CALC_TEST, git, write_tasks
from guard_helpers import (
    claimed_task_worktree,
    commit_on_base,
    decision,
    fake_git,
    guard_events,
    hook_input,
    new_on_branch,
    post_message,
    pre,
)
from test_guard_postcheck import AGENT, CALC_PATH, VIAS

PROPERTIES = {
    "P-7": "test_p7_post_check_reports_exactly_what_the_edit_path_would_block",
    "P-8": "test_p8_new_owned_and_already_reported_files_are_never_reported",
    "P-9": "test_p9_a_command_the_pre_filter_skips_changes_no_test_file",
    "P-10": "test_p10_forced_failures_never_change_a_decision_or_block_the_post_check",
    "P-21": "test_p21_one_event_per_blocking_decision",
}

SETTINGS = dict(derandomize=True, max_examples=40, database=None, deadline=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow, HealthCheck.data_too_large])
SCRATCH = "tests/test_scratch.py"
OWNED = "tests/test_subtract.py"


class Repo:
    root: Path
    wt: Path
    head: str
    wt_head: str


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        base = tmp_path_factory.mktemp("postcheck-prop")
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
        for template in TEMPLATES.values():
            (root / template.path).parent.mkdir(parents=True, exist_ok=True)
            (root / template.path).write_text(template.text)
        (root / "conftest.py").write_text("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n")
        (root / "specs" / "calc").mkdir(parents=True)
        (root / "specs" / "calc" / "requirements.md").write_text("# Feature: Calc\n- 1.1 WHEN asked THE SYSTEM SHALL subtract\n")
        write_tasks(root)
        (root / "specs" / "ck.json").write_text(json.dumps({"verify": ["python -m pytest -q tests/test_calc.py"]}))
        git(root, "add", "-A")
        git(root, "commit", "-qm", "base")
        git(root, "checkout", "-qb", "feature")
        commit_on_base(root, OWNED, CALC_TEST)
        new_on_branch(root, SCRATCH, CALC_TEST)
        out = Repo()
        out.root = root
        out.wt = claimed_task_worktree(root, "calc", 1)
        write_tasks(root, t1="In Progress")  # committed: reset() restores it
        git(root, "commit", "-qam", "task 1 in progress")
        out.head = git(root, "rev-parse", "HEAD").strip()
        out.wt_head = git(out.wt, "rev-parse", "HEAD").strip()
        yield out


def reset(repo: Repo) -> None:
    git(repo.root, "reset", "-q", "--hard", repo.head)
    git(repo.root, "clean", "-fdq", "-e", ".claude", "-e", ".ck")
    git(repo.wt, "reset", "-q", "--hard", repo.wt_head)
    git(repo.wt, "clean", "-fdq")
    lock = repo.root / ".git" / "index.lock"
    if lock.exists():
        lock.unlink()


def run_cycle(root: Path, session: str, command: str, *, via: str, agent_id=AGENT, post_env=None, between=None):
    """Pre hook, real `bash -c`, optional `between()`, post hook (in this process; `post_env` patches os.environ for it)."""
    from claude_kiro.cli.runner import execute_hook

    first = hook_input(root, session, "Bash", agent_id=agent_id, command=command)
    verdict = execute_hook("pre-tool-guard", first)
    if verdict and decision(verdict) in ("ask", "deny"):
        return verdict, None, False
    subprocess.run(["bash", "-c", command], cwd=str(root), capture_output=True, text=True)
    if between:
        between()
    second = hook_input(root, session, "Bash", agent_id=agent_id, tool_use_id=first["tool_use_id"], event="PostToolUse",
                        tool_response={"stdout": "", "stderr": "", "interrupted": False}, command=command)
    with pytest.MonkeyPatch.context() as mp:
        for key, value in (post_env or {}).items():
            mp.setenv(key, value)
        return verdict, execute_hook(via, second), True


# --------------------------------------------------------------------------- mutations


def _lines_without(text: str, needle: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True) if needle not in line)


# name -> (new text from the starting content, sed script or None, perl switches+code or None)
MUTATIONS = {
    "drop_assert": (lambda t: _lines_without(t, "assert add(1, 2) == 3"), "/assert add(1, 2) == 3/d",
                    "-ni -e 'print unless /assert add\\(1, 2\\) == 3/'"),
    "drop_test": (lambda t: t[: t.index("def test_add_negative")], "/^def test_add_negative/,$d",
                  "-ni -e 'last if /^def test_add_negative/; print'"),
    "drop_all_asserts": (lambda t: _lines_without(t, "assert"), "/assert/d", "-ni -e 'print unless /assert/'"),
    "add_skip": (lambda t: t.replace("def test_add():", "@pytest.mark.skip\ndef test_add():", 1),
                 "s/^def test_add():/@pytest.mark.skip\\ndef test_add():/", "-pi -e 's/^def test_add\\(\\):/\\@pytest.mark.skip\\ndef test_add():/'"),
    "rename": (lambda t: t.replace("add(1, 2)", "add(2, 1)"), "s/add(1, 2)/add(2, 1)/", "-pi -e 's/add\\(1, 2\\)/add(2, 1)/'"),
    "comment_out": (lambda t: t.replace("    assert add(1, 2) == 3", "    # assert add(1, 2) == 3"),
                    "s/^    assert add(1, 2) == 3/    # assert add(1, 2) == 3/",
                    "-pi -e 's/^    assert add\\(1, 2\\) == 3/    # assert add(1, 2) == 3/'"),
    "skip_call": (lambda t: t.replace("def test_add():\n", "def test_add():\n    pytest.skip('x')\n", 1), None, None),
    "xfail": (lambda t: t.replace("def test_add():", "@pytest.mark.xfail\ndef test_add():", 1), None, None),
    "unittest_skip": (lambda t: "@unittest.skip('x')\n" + t, None, None),
    "only_appended": (lambda t: t + "\nit.only('x', () => {})\n", None, None),
    "add_test": (lambda t: t + "\n\ndef test_zero():\n    assert add(0, 0) == 0\n", None, None),
    "reorder": (lambda t: "from calc import add\n\n\n" + t[t.index("def test_add_negative"):] + "\n\n" + t[t.index("def test_add():"):t.index("def test_add_negative")], None, None),
    "empty": (lambda t: "", None, None),
}
TOOLS = ["sed", "perl", "python", "redirect", "find"]


def command_for(name: str, tool: str, path: str, find_root: str, *, base_text: str = CALC_TEST) -> str:
    """A shell command applying mutation `name` to `path` with `tool` (python when the tool has no form for it)."""
    new_text, sed_script, perl_code = MUTATIONS[name]
    new = new_text(base_text)
    q = shlex.quote(path)
    if tool == "sed" and sed_script:
        return f"sed -i {shlex.quote(sed_script)} {q}"
    if tool == "perl" and perl_code:
        return f"perl {perl_code} {q}"
    if tool == "find" and sed_script:
        return f"find {shlex.quote(find_root)} -name {shlex.quote(Path(path).name)} -exec sed -i {shlex.quote(sed_script)} {{}} +"
    if tool == "redirect":
        return f": > {q}" if new == "" else f"cat > {q} <<'EOF_X'\n{new}EOF_X"
    return f"python3 - <<'EOF_X'\nopen({path!r}, 'w').write({new!r})\nEOF_X"


def kind_texts(old: str, new: str):
    from claude_kiro.hooks._shared.test_heuristics import weakening

    return [r.replace("(s)", "") for r in weakening(old, new)]


mutation_names = st.sampled_from(sorted(MUTATIONS))
tools = st.sampled_from(TOOLS)
vias = st.sampled_from(VIAS)

# --------------------------------------------------------------------------- P-7 templates (pytest, jest/vitest, go, rspec)


class Template:
    def __init__(self, path, text, assert_key, first, second, comment, extra, skips):
        self.path, self.text, self.assert_key = path, text, assert_key
        self.first, self.second, self.comment, self.extra, self.skips = first, second, comment, extra, skips


JEST_TEST = """const { add } = require('../calc')

it('adds', () => {
  expect(add(1, 2)).toBe(3)
})

it('adds negatives', () => {
  expect(add(-1, -1)).toBe(-2)
})
"""
GO_TEST = """package calc

import "testing"

func TestAdd(t *testing.T) {
\tif add(1, 2) != 3 {
\t\tt.Fatal("x")
\t}
}

func TestAddNegative(t *testing.T) {
\tif add(-1, -1) != -2 {
\t\tt.Fatal("y")
\t}
}
"""
RSPEC_TEST = """require_relative '../calc'

it 'adds' do
  expect(add(1, 2)).to eq(3)
end

it 'adds negatives' do
  add(-1, -1).should == -2
end
"""

TEMPLATES = {
    "pytest": Template(
        "tests/test_calc.py", CALC_TEST, "assert add(1, 2) == 3", "def test_add():", "def test_add_negative", "# ",
        "\n\ndef test_zero():\n    assert add(0, 0) == 0\n",
        [lambda t: t.replace("def test_add():", "@pytest.mark.skip\ndef test_add():", 1),
         lambda t: t.replace("def test_add():\n", "def test_add():\n    pytest.skip('x')\n", 1),
         lambda t: t.replace("def test_add():", "@pytest.mark.xfail\ndef test_add():", 1),
         lambda t: "@unittest.skip('x')\n" + t,
         lambda t: t + "\nit.only('x', () => {})\n"]),
    "jest": Template(
        "tests/calc.test.js", JEST_TEST, "expect(add(1, 2))", "it('adds',", "it('adds negatives'", "// ",
        "\nit('zero', () => {\n  expect(add(0, 0)).toBe(0)\n})\n",
        [lambda t: t.replace("it('adds',", "it.skip('adds',", 1),
         lambda t: t.replace("it('adds',", "xit('adds',", 1),
         lambda t: t.replace("it('adds',", "it.only('adds',", 1),
         lambda t: t.replace("it('adds',", "test.todo('adds',", 1),
         lambda t: t.replace("it('adds negatives',", "describe.skip('adds negatives',", 1)]),
    "go": Template(
        "calc_test.go", GO_TEST, 't.Fatal("x")', "func TestAdd(", "func TestAddNegative", "// ",
        "\nfunc TestZero(t *testing.T) {\n\tif add(0, 0) != 0 {\n\t\tt.Fatal(\"z\")\n\t}\n}\n",
        [lambda t: t.replace("func TestAdd(t *testing.T) {\n", "func TestAdd(t *testing.T) {\n\tt.Skip(\"x\")\n", 1),
         lambda t: t.replace("func TestAddNegative(t *testing.T) {\n", "func TestAddNegative(t *testing.T) {\n\tt.SkipNow()\n", 1)]),
    "rspec": Template(
        "spec/calc_spec.rb", RSPEC_TEST, "expect(add(1, 2))", "it 'adds' do", "it 'adds negatives'", "# ",
        "\nit 'zero' do\n  expect(add(0, 0)).to eq(0)\nend\n",
        [lambda t: t.replace("it 'adds' do", "xit('adds') do", 1),
         lambda t: t.replace("it 'adds' do", "it.skip 'adds' do", 1),
         lambda t: t.replace("it 'adds negatives' do", "describe.skip 'adds negatives' do", 1)]),
}


def template_mutation(tpl: Template, name: str, index: int) -> str:
    """The new text for mutation `name` applied to the template's starting body."""
    from claude_kiro.hooks._shared.test_heuristics import ASSERTION

    t = tpl.text
    if name == "drop_assert":
        return _lines_without(t, tpl.assert_key)
    if name == "drop_test":
        return t[: t.index(tpl.second)]
    if name == "drop_all_asserts":
        return "".join(line for line in t.splitlines(keepends=True) if not ASSERTION.search(line))
    if name == "skip_marker":
        return tpl.skips[index % len(tpl.skips)](t)
    if name == "rename":
        return t.replace("add(1, 2)", "add(2, 1)")
    if name == "comment_out":
        return "".join(tpl.comment + line if tpl.assert_key in line else line for line in t.splitlines(keepends=True))
    if name == "add_test":
        return t + tpl.extra
    if name == "reorder":
        head, first, second = t[: t.index(tpl.first)], t[t.index(tpl.first): t.index(tpl.second)], t[t.index(tpl.second):]
        return head + second.rstrip("\n") + "\n\n" + first.rstrip("\n") + "\n"
    if name == "empty":
        return ""
    raise KeyError(name)


TEMPLATE_MUTATIONS = ["drop_assert", "drop_test", "drop_all_asserts", "skip_marker", "rename", "comment_out", "add_test", "reorder", "empty"]


def template_command(tpl: Template, name: str, tool: str, path: str, find_root: str, new: str) -> str:
    q = shlex.quote(path)
    if name == "drop_assert" and tool == "sed":
        return f"sed -i {shlex.quote('/' + tpl.assert_key + '/d')} {q}"
    if name == "drop_assert" and tool == "perl":
        return f"perl -ni -e {shlex.quote('print unless /\\Q' + tpl.assert_key + '\\E/')} {q}"
    if name == "drop_assert" and tool == "find":
        return f"find {shlex.quote(find_root)} -name {shlex.quote(Path(path).name)} -exec sed -i {shlex.quote('/' + tpl.assert_key + '/d')} {{}} +"
    if tool == "redirect":
        return f": > {q}" if new == "" else f"cat > {q} <<'EOF_X'\n{new}EOF_X"
    return f"python3 - <<'EOF_X'\nopen({path!r}, 'w').write({new!r})\nEOF_X"


# --------------------------------------------------------------------------- P-7


@settings(**SETTINGS)
@given(lang=st.sampled_from(sorted(TEMPLATES)), name=st.sampled_from(TEMPLATE_MUTATIONS), index=st.integers(0, 6), tool=tools,
       in_worktree=st.booleans(), commit=st.booleans(), via=vias)
def test_p7_post_check_reports_exactly_what_the_edit_path_would_block(repo, lang, name, index, tool, in_worktree, commit, via):
    reset(repo)
    tpl = TEMPLATES[lang]
    new = template_mutation(tpl, name, index)
    root = repo.wt if in_worktree else repo.root
    target = root / tpl.path
    path = str(target) if in_worktree else tpl.path
    command = template_command(tpl, name, tool, path, f"{root}/{Path(tpl.path).parent}" if in_worktree else str(Path(tpl.path).parent), new)
    if commit:
        sep = "\n" if "\n" in command else " && "  # a heredoc terminator must stay alone on its line
        command += f"{sep}git -C {shlex.quote(str(root))} commit -qam x"
    session = f"p7-{uuid.uuid4().hex}"

    write_blocks = decision(pre(repo.root, session, "Write", agent_id=AGENT, file_path=str(target), content=new)) == "deny"
    verdict, post, ran = run_cycle(repo.root, session, command, via=via)
    assert ran, verdict
    message = post_message(post)
    if new == tpl.text:
        return  # D19: the base ref's own content is exempt
    assert (message is not None) == write_blocks, (lang, name, tool, command, message)
    if write_blocks:
        assert tpl.path in message
        for kind in kind_texts(tpl.text, new):
            assert kind in message, (kind, message)


# --------------------------------------------------------------------------- P-8

READ_ONLY = ["ls tests", "cat tests/test_calc.py", "git status", "git branch", "find tests -name '*.py'", "grep -n assert tests/test_calc.py",
             "git diff", "git log --oneline", "head -n 3 calc.py", "wc -l calc.py", "pwd", "true"]
OTHER_WRITES = ["echo hi > notes.txt", "touch other.txt", "sed -i 's/a + b/b + a/' calc.py", "echo '# n' >> calc.py", "mkdir -p out && echo x > out/y"]


@settings(**SETTINGS)
@given(name=mutation_names, tool=tools, which=st.sampled_from([SCRATCH, OWNED]), via=vias)
def test_p8_new_owned_and_already_reported_files_are_never_reported(repo, name, tool, which, via):
    reset(repo)
    session = f"p8-{uuid.uuid4().hex}"
    command = command_for(name, tool, which, "tests")
    _, post, ran = run_cycle(repo.root, session, command, via=via)
    assert ran and post is None, (which, name, tool, post_message(post))


@settings(**SETTINGS)
@given(follow=st.lists(st.sampled_from(READ_ONLY + OTHER_WRITES), min_size=1, max_size=3), via=vias)
def test_p8_a_reported_file_is_never_reported_again_by_commands_that_do_not_touch_it(repo, follow, via):
    reset(repo)
    session = f"p8b-{uuid.uuid4().hex}"
    _, first, ran = run_cycle(repo.root, session, command_for("drop_assert", "sed", CALC_PATH, "tests"), via=via)
    assert ran and CALC_PATH in (post_message(first) or "")
    for command in follow:
        _, post, ran = run_cycle(repo.root, session, command, via=via)
        assert ran and post is None, (command, post_message(post))


# --------------------------------------------------------------------------- P-9

WRITE_FORMS = [
    "sed -i s/a/b/ tests/test_calc.py", "sed -i.bak s/a/b/ tests/test_calc.py", "perl -pi -e s/a/b/ tests/test_calc.py",
    "python3 - <<'EOF'\nopen('tests/test_calc.py','w').write('')\nEOF", "python3 script.py", "echo x > tests/test_calc.py",
    "echo x >> tests/test_calc.py", "tee tests/test_calc.py", "cp calc.py tests/test_calc.py", "mv calc.py calc2.py",
    "dd of=tests/test_calc.py if=calc.py", "install calc.py tests/test_calc.py", "git checkout -- tests/test_calc.py",
    "git restore tests/test_calc.py", "patch -p1 < d.patch", "ed tests/test_calc.py", "awk -i inplace 1 tests/test_calc.py",
    "npm run test", "./tool", "rm tests/test_calc.py", "truncate -s 0 tests/test_calc.py",
    "find tests -delete", "find tests -name x -exec rm {} +", "find tests -name x -execdir rm {} +", "find tests -ok rm {} \\;",
    "find tests -fprint out.txt", "git branch -D x", "git branch -m a b", "git commit -qam x", "git merge main", "git reset --hard",
]
READ_ONLY_RUN = ["ls", "ls tests", "cat tests/test_calc.py", "grep -n assert tests/test_calc.py", "git status", "git diff", "git log --oneline",
                 "git show HEAD --stat", "git branch", "git branch -a", "git branch --show-current", "find tests -name '*.py'",
                 "find tests -type f", "head -n 2 tests/test_calc.py", "wc -l tests/test_calc.py", "pwd", "true", "echo hi",
                 "python -m pytest -q --co -p no:cacheprovider tests/test_calc.py", "git rev-parse HEAD", "git ls-files tests"]
PREFIXES = ["", "cd . && ", "cd tests && cd .. && ", "FOO=1 ", "command ", "FOO=1 BAR=2 "]


def _tests_fingerprint(root: Path):
    digest = hashlib.sha1()
    for path in sorted((root / "tests").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    status = subprocess.run(["git", "status", "--porcelain", "tests"], cwd=root, capture_output=True, text=True).stdout
    return digest.hexdigest(), status


@settings(**SETTINGS)
@given(command=st.sampled_from(READ_ONLY_RUN + WRITE_FORMS), prefix=st.sampled_from(PREFIXES))
def test_p9_a_command_the_pre_filter_skips_changes_no_test_file(repo, command, prefix):
    from claude_kiro.hooks._shared import guard_policy

    reset(repo)
    full = prefix + command
    may = guard_policy.may_write(full)
    if command in WRITE_FORMS:
        assert may is True, full
        return
    if may:
        return  # a conservative answer is allowed for a read-only command; the safety side is below
    before = _tests_fingerprint(repo.root)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run(["bash", "-c", full], cwd=repo.root, capture_output=True, text=True, env=env, timeout=60)
    assert _tests_fingerprint(repo.root) == before, full


def test_p9_the_plain_read_only_commands_are_actually_skipped():
    from claude_kiro.hooks._shared import guard_policy

    for command in ("ls tests", "cat tests/test_calc.py", "git status", "git branch", "git branch -a", "find tests -name '*.py'",
                    "grep -n assert tests/test_calc.py"):
        assert guard_policy.may_write(command) is False, command
        assert guard_policy.may_write("FOO=1 " + command) is False, command
        assert guard_policy.may_write("cd . && " + command) is False, command


# --------------------------------------------------------------------------- P-10

FAILURES = ["data_dir", "git_fail", "git_sleep", "unreadable", "read_only"]
IS_ROOT = os.geteuid() == 0


def pre_inputs(repo):
    path = str(repo.root / CALC_PATH)
    return [
        ("Bash", {"command": "rm tests/test_calc.py"}),
        ("Bash", {"command": "git rm -q tests/test_calc.py"}),
        ("Bash", {"command": f"rm {repo.wt}/tests/test_calc.py"}),
        ("Edit", {"file_path": path, "old_string": "    assert add(1, 2) == 3\n", "new_string": "    pass\n"}),
        ("Write", {"file_path": path, "content": "def test_add():\n    pass\n"}),
        ("Bash", {"command": "sed -i '/assert add(1, 2) == 3/d' tests/test_calc.py"}),
        ("Bash", {"command": "python3 -c \"open('tests/test_calc.py','a').write('')\""}),
    ]


@settings(**{**SETTINGS, "max_examples": 40})
@given(index=st.integers(0, 6), failure=st.sampled_from([f for f in FAILURES if not (IS_ROOT and f in ("unreadable", "read_only"))]),
       agent=st.booleans())
def test_p10_forced_failures_never_change_a_decision_or_block_the_post_check(repo, tmp_path_factory, index, failure, agent):
    reset(repo)
    tool, tool_input = pre_inputs(repo)[index]
    agent_id = AGENT if agent else None
    blocker = tmp_path_factory.mktemp("p10") / "not-a-dir"
    blocker.write_text("x")
    bin_dir = blocker.parent / "bin"
    bin_dir.mkdir()
    target = repo.root / CALC_PATH

    baseline = pre(repo.root, f"p10a-{uuid.uuid4().hex}", tool, agent_id=agent_id, **tool_input)

    with pytest.MonkeyPatch.context() as mp:
        if failure == "data_dir":
            mp.setenv("CK_DATA_HOME", str(blocker))
        elif failure == "unreadable":
            target.chmod(0)
        elif failure == "read_only":
            (repo.root / "tests").chmod(0o555)
        try:
            forced = pre(repo.root, f"p10b-{uuid.uuid4().hex}", tool, agent_id=agent_id, **tool_input)
        finally:
            target.chmod(0o644)
            (repo.root / "tests").chmod(0o755)
    assert forced == baseline, (tool, tool_input, failure)

    if tool != "Bash" or decision(baseline) in ("ask", "deny"):
        return
    command = tool_input["command"]
    via = "post-bash-check"
    reset(repo)
    _, base_post, ran = run_cycle(repo.root, f"p10c-{uuid.uuid4().hex}", command, via=via, agent_id=agent_id)
    assert ran
    reset(repo)
    post_env = {}
    between = None
    if failure == "git_fail":
        fake_git(bin_dir, "fail")
        post_env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    elif failure == "git_sleep":
        fake_git(bin_dir, "sleep")
        post_env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    elif failure == "data_dir":
        post_env = {"CK_DATA_HOME": str(blocker)}
    elif failure == "unreadable":
        between = lambda: target.chmod(0)  # noqa: E731
    elif failure == "read_only":
        between = lambda: (repo.root / "tests").chmod(0o555)  # noqa: E731
    start = time.monotonic()
    try:
        _, forced_post, ran = run_cycle(repo.root, f"p10d-{uuid.uuid4().hex}", command, via=via, agent_id=agent_id,
                                        post_env=post_env, between=between)
    finally:
        target.chmod(0o644)
        (repo.root / "tests").chmod(0o755)
    assert ran
    assert time.monotonic() - start < 20
    if failure in ("git_fail", "git_sleep", "unreadable"):
        assert forced_post is None, (command, failure, post_message(forced_post))
    else:
        assert post_message(forced_post) == post_message(base_post)


# --------------------------------------------------------------------------- P-21

EVENT_FIELDS = {"v", "ts", "sessionId", "agentId", "agentType", "caller", "tool", "decision", "category", "reason", "path",
                "command", "family", "toolUseId"}
REQ = "- 1.1 WHEN asked THE SYSTEM SHALL subtract\n"


def call_menu(repo):
    """(label, runner(session, agent_id) -> number of blocking decisions it should produce)."""
    path = str(repo.root / CALC_PATH)

    def bash_pre(command):
        return lambda session, agent: 1 if decision(pre(repo.root, session, "Bash", agent_id=agent, command=command)) else 0

    def bash_cycle(command, expect_block):
        def run(session, agent):
            verdict, post, ran = run_cycle(repo.root, session, command, via="post-bash-check", agent_id=agent)
            blocked = 1 if (verdict and decision(verdict)) else 0
            caught = 1 if post_message(post) else 0
            if expect_block is not None:
                assert bool(blocked or caught) == expect_block, (command, verdict, post)
            return blocked + caught
        return run

    def edit(old, new, file_path, expect_main):
        def run(session, agent):
            result = pre(repo.root, session, "Edit", agent_id=agent, file_path=file_path, old_string=old, new_string=new)
            return 1 if result else 0
        return run

    return [
        bash_pre("rm tests/test_calc.py"),
        bash_pre("rm tests/test_scratch.py"),
        bash_pre("rm -rf tests/__pycache__"),
        bash_cycle("sed -i 's/add(1, 2)/add(2, 1)/' tests/test_calc.py", False),
        bash_cycle("sed -i '/assert add(1, 2) == 3/d' tests/test_calc.py", True),
        bash_cycle("sed -i '/assert/d' tests/test_scratch.py", False),
        bash_cycle("echo hi > notes.txt", False),
        bash_cycle("python3 -c \"open('tests/test_calc.py','a').write('\\n# c\\n')\"", False),
        edit("    assert add(1, 2) == 3\n", "    pass\n", path, True),
        edit("a + b", "a + b + 0", str(repo.root / "calc.py"), False),
        edit(REQ, "- 1.1 WHEN asked THE SYSTEM SHALL do something else\n", str(repo.root / "specs/calc/requirements.md"), False),
        edit(REQ, REQ + "- 1.2 WHEN more THE SYSTEM SHALL add\n", str(repo.root / "specs/calc/requirements.md"), False),
    ]


@settings(**SETTINGS)
@given(picks=st.lists(st.integers(0, 11), min_size=1, max_size=3), agent=st.booleans())
def test_p21_one_event_per_blocking_decision(repo, picks, agent):
    reset(repo)
    menu = call_menu(repo)
    session = f"p21-{uuid.uuid4().hex}"
    agent_id = AGENT if agent else None
    for pick in picks:
        before = guard_events()
        blocking = menu[pick](session, agent_id)
        reset(repo)
        after = guard_events()
        assert len(after) - len(before) == blocking, (pick, agent, after[len(before):])
        for event in after[len(before):]:
            assert EVENT_FIELDS <= set(event), EVENT_FIELDS - set(event)
            assert event["v"] == 1 and event["decision"] in ("ask", "deny", "post-check")
            assert event["sessionId"] == session
            assert event["caller"] == ("task_agent" if agent else "main_session")
