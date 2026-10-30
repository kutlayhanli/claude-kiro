"""Pure-seam properties of the guard policy (prompt-free-runs P-2, P-12, P-13, P-24).

The seams are `guard_policy.destroy_targets`, `decide_bash`, `decide_edit`, `effective_mode`, `block_reason`,
`post_check_reason` and `command_family`. Pure: no repo, no hook.
"""

import string

from hypothesis import given, settings
from hypothesis import strategies as st

PROPERTIES = {
    "P-2": "test_p2_writes_are_never_destroy_targets_and_never_block",
    "P-12": "test_p12_task_agent_is_never_asked",
    "P-13": "test_p13_denial_and_catch_reasons_carry_path_kind_and_instruction",
    "P-24": "test_p24_command_family_follows_the_rule",
}

SETTINGS = dict(derandomize=True, max_examples=200, database=None)


def policy():
    from claude_kiro.hooks._shared import guard_policy

    return guard_policy


# --------------------------------------------------------------------------- generators

word = st.text(alphabet=string.ascii_lowercase + string.digits + "_", min_size=1, max_size=8)
test_paths = st.builds(lambda d, n: f"{d}/test_{n}.py", st.sampled_from(["tests", "tests/sub", "src/tests", "spec"]), word)
spaced_paths = st.builds(lambda n: f"tests/test {n}.py", word)
any_test_path = st.one_of(test_paths, spaced_paths, st.just("tests/test_calc.py"))


def q(path: str) -> str:
    """Shell-quote a path only when it has a space."""
    return f"'{path}'" if " " in path else path


WRITE_FORMS = [
    "sed -i 's/a/b/' {f}", "sed -i.bak 's/a/b/' {f}", "sed -i '' 's/a/b/' {f}", "sed --in-place=x 's/a/b/' {f}",
    "perl -pi -e 's/a/a/' {f}", "perl -i -pe 's/a/a/' {f}",
    "python - <<'EOF'\nopen({f!r},'w').write('')\nEOF", "python3 -c \"open('{f}','w').write('')\"", "uv run python s.py {f}",
    "> {f}", ">> {f}", "echo x > {f}", "echo x >> {f}", "cat > {f} <<EOF\nx\nEOF", "tee {f}", "tee -a {f}", "cp x.py {f}",
    "printf 'a' > {f}", "xargs sed -i 's/a/b/' {f}", "find tests -name '*.py' -exec sed -i 's/a/b/' {{}} +",
]
cd_prefix = st.sampled_from(["", "cd /srv/proj && ", "cd /srv/proj; ", 'cd "/srv/proj" && ', "cd tests && "])
writes = st.builds(lambda form, f, pre: pre + form.format(f=q(f)), st.sampled_from(WRITE_FORMS), any_test_path, cd_prefix)
callers = st.sampled_from(["task_agent", "main_session"])
modes = st.sampled_from(["ask", "deny", None])


# --------------------------------------------------------------------------- P-2


@settings(**SETTINGS)
@given(command=writes, caller=callers, configured=st.sampled_from(["ask", "deny", "off"]))
def test_p2_writes_are_never_destroy_targets_and_never_block(command, caller, configured):
    gp = policy()
    assert gp.destroy_targets(command, "/srv/proj") == [], command
    mode = gp.effective_mode(caller, None if configured == "off" else configured)
    decided = gp.decide_bash([], caller, mode)
    assert decided.block is False and decided.mode is None


# --------------------------------------------------------------------------- P-12 and P-13

rels = st.one_of(any_test_path, st.just("specs/calc/requirements.md"), st.just("specs/calc/test-plan.md"))
kinds_text = st.lists(st.sampled_from(["deletes the test file", "moves the test file", "removes 1 assertion(s)",
                                       "adds 2 skip/xfail/only marker(s)", "removes 3 test case(s)"]), min_size=1, max_size=3,
                      unique=True)
configured_modes = st.sampled_from(["ask", "deny", "off"])


def blocking_bash_facts(rel, op):
    from claude_kiro.hooks._shared.guard_policy import TargetFacts

    return [TargetFacts(rel=rel, op=op, is_test=True, exists_on_base=True, owned=False)]


def blocking_edit_facts(rel, target, kinds):
    from claude_kiro.hooks._shared.guard_policy import EditFacts

    if target == "test":
        return EditFacts(rel=rel, target="test", weakening=tuple(kinds), owned=False, exists_on_base=True,
                         implementation_started=False, only_adds=False)
    return EditFacts(rel=rel, target=target, weakening=(), owned=False, exists_on_base=None, implementation_started=True,
                     only_adds=False)


@settings(**SETTINGS)
@given(rel=any_test_path, op=st.sampled_from(["delete", "move"]), caller=callers, configured=configured_modes)
def test_p12_bash_blocks_follow_the_caller_and_mode(rel, op, caller, configured):
    gp = policy()
    mode = gp.effective_mode(caller, None if configured == "off" else configured)
    decided = gp.decide_bash(blocking_bash_facts(rel, op), caller, mode)
    if configured == "off":
        assert mode is None and decided.block is False
    elif caller == "task_agent":
        assert mode == "deny" and decided.block and decided.mode == "deny"  # never ask
    else:
        assert mode == configured and decided.block and decided.mode == configured


@settings(**SETTINGS)
@given(rel=any_test_path, kinds=kinds_text, target=st.sampled_from(["test", "requirements", "test_plan"]), caller=callers,
       configured=configured_modes)
def test_p12_task_agent_is_never_asked(rel, kinds, target, caller, configured):
    gp = policy()
    facts = blocking_edit_facts(rel, target, kinds)
    mode = gp.effective_mode(caller, None if configured == "off" else configured)
    decided = gp.decide_edit(facts, caller, mode)
    assert decided.mode != "ask" or caller == "main_session"
    if configured == "off":
        assert decided.block is False
    elif target == "test":
        assert decided.block
        assert decided.mode == ("deny" if caller == "task_agent" else configured)
    elif caller == "task_agent":
        assert decided.block and decided.mode == "deny"  # frozen spec text: a task agent is denied (5.2)
    else:
        assert decided.block is False  # the main session may change the spec (5.1)


@settings(**SETTINGS)
@given(rel=any_test_path, kinds=kinds_text, tool=st.sampled_from(["Bash", "Edit", "Write", "MultiEdit"]))
def test_p13_denial_and_catch_reasons_carry_path_kind_and_instruction(rel, kinds, tool):
    gp = policy()
    text = gp.block_reason("task_agent", "test", rel, kinds, tool)
    lowered = text.lower()
    assert text.startswith("[ck guard]") and rel in text
    assert all(kind in text for kind in kinds), (kinds, text)
    assert "keep the test" in lowered and "do not work around" in lowered and "report the conflict" in lowered

    spec = gp.block_reason("task_agent", "requirements", "specs/calc/requirements.md", [], tool)
    assert "specs/calc/requirements.md" in spec and "report the conflict" in spec.lower() and "do not" in spec.lower()

    finding = gp.PostCheckFinding(caller="task_agent", mode="deny", files=((rel, tuple(kinds)),))
    caught = gp.post_check_reason(finding)
    assert caught.startswith("[ck post-check]") and rel in caught
    assert all(kind in caught for kind in kinds), (kinds, caught)
    assert "restore" in caught.lower() and "keep" in caught.lower() and "report the conflict" in caught.lower()


# --------------------------------------------------------------------------- P-24

FAMILY_FORMS = {
    "sed": ["sed -i 's/a/b/' f", "sed -n 1p f", "perl -i -pe 's/a/b/' f", "perl -pi -e 's/a/b/' f"],
    "rm": ["rm f", "rm -rf d", "unlink f", "truncate -s 0 f"],
    "mv": ["mv a b"],
    "git": ["git status", "git -C d rm f", "git rm -q f", "command git log", "git mv a b"],
    "python": ["python s.py", "python3 -c 'pass'", "uv run python s.py", "python -m pytest -q", "python - <<'EOF'\npass\nEOF"],
    "redirect": ["echo x > f", "printf a >> f", "cat > f <<EOF\nx\nEOF", "ls | tee f > /dev/null && echo x > f"],
    "other": ["pytest -q", "grep sed f", "rmdir d", "gitk", "pythonx s.py", "ls", "echo hi 2>&1", "ls > /dev/null",
              "pytest -q > /dev/null 2>&1", "cat f"],
}
family_cases = [(form, family) for family, forms in FAMILY_FORMS.items() for form in forms]
prefixes = st.sampled_from(["", " ", "\t", "  \t", "cd d && ", "cd d; ", "FOO=1 ", "FOO=1 BAR=2 ", "cd d && FOO=1 "])


@settings(**SETTINGS)
@given(case=st.sampled_from(family_cases), prefix=prefixes)
def test_p24_command_family_follows_the_rule(case, prefix):
    form, family = case
    gp = policy()
    assert gp.command_family(prefix + form) == family, (prefix + form)
    assert gp.command_family(None) == "other"
    if " " in form and not form.startswith(("python -", "cat >")) and "\n" not in form:
        head, tail = form.split(" ", 1)
        assert gp.command_family(prefix + head + " \\\n  " + tail) == family  # backslash continuation


def test_p24_family_names_are_the_documented_set():
    from claude_kiro.analytics import share

    assert set(share.COMMAND_FAMILIES) == {"sed", "rm", "mv", "git", "python", "redirect", "other"}
    gp = policy()
    for _form, family in family_cases:
        assert family in share.COMMAND_FAMILIES
    assert gp.command_family("") == "other"
