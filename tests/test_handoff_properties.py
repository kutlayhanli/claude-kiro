"""Properties P-19, P-20 and P-31: the pure hand-off decision `handoff.decide_handoff` (requirements 8.1, 8.5-8.9).

No server and no mocks: the seam is called directly with generated `HandoffFacts`. The real-CLI sample of P-19 is
TC-47 in `tests/test_handoff.py`. Project modules are imported inside the test bodies so the file always collects.
"""

import re
import shlex
import string

from hypothesis import HealthCheck, assume, given, settings, strategies as st

PROPERTIES = {
    "P-19": "test_p19_handoff_fires_only_from_a_claude_worktree_for_whole_spec_runs",
    "P-20": "test_p20_rest_args_round_trip_through_shlex",
    "P-31": "test_p31_spec_state_decides_handoff_run_branch_or_refusal",
}

SETTINGS = settings(
    derandomize=True,
    max_examples=200,
    database=None,
    deadline=None,
    suppress_health_check=list(HealthCheck),
)

ROOT = "/work/proj"
TASK_TOKEN = re.compile(r"^[0-9][0-9A-Za-z_]*$")

# cwd form -> (cwd, is_linked_worktree, main_root, fires_when_whole_spec)
CWD_FORMS = {
    "main_root": (ROOT, False, ROOT, False),
    "main_subdir": (ROOT + "/src/pkg", False, ROOT, False),
    "claude_worktree_root": (ROOT + "/.claude/worktrees/bg-1", True, ROOT, True),
    "claude_worktree_subdir": (ROOT + "/.claude/worktrees/bg-1/src/pkg", True, ROOT, True),
    "ck_worktree": (ROOT + "/.ck/worktrees/demo-task-1", True, ROOT, False),
    "outside_worktree": ("/elsewhere/wt", True, ROOT, False),
    "non_git": ("/tmp/not-a-repo", False, None, False),
    "near_miss_prefix": (ROOT + "/.claude/worktrees-old/bg-1", True, ROOT, False),
    "worktrees_dir_itself": (ROOT + "/.claude/worktrees", True, ROOT, False),
}

SHA_A = "a" * 40
SHA_B = "b" * 40

slugs = st.from_regex(r"[a-z][a-z0-9]{0,6}(-[a-z0-9]{1,6}){0,3}", fullmatch=True)
kinds = st.sampled_from(["implement", "create", "review"])
task_ids = st.one_of(st.integers(min_value=1, max_value=999).map(str), st.sampled_from(["3", "17", "3_a", "2b", "1x"]))
rests = st.one_of(
    st.just(()),
    task_ids.map(lambda n: (n,)),
    st.just(("all",)),
    st.just(("--escalate", "opus")),
    task_ids.map(lambda n: (n, "--x")),
)


def _facts(**over):
    from claude_kiro.handoff import HandoffFacts

    base = dict(
        kind="implement", args=("demo",), cwd=ROOT, is_linked_worktree=False, main_root=ROOT,
        spec_in_main=True, spec_in_worktree=True, worktree_head=SHA_A, main_head=SHA_B, main_branch="main",
        main_clean=True, spec_same=True, worktree_spec_dirty=False, run_branch_at=None,
    )
    base.update(over)
    return HandoffFacts(**base)


def _single_task(kind, args):
    """The first two args hold a task-id token other than `all` (a single-task implement run)."""
    return kind == "implement" and any(TASK_TOKEN.match(a) and a != "all" for a in args[:2])


@SETTINGS
@given(form=st.sampled_from(sorted(CWD_FORMS)), kind=kinds, spec=slugs, rest=rests)
def test_p19_handoff_fires_only_from_a_claude_worktree_for_whole_spec_runs(form, kind, spec, rest):
    from claude_kiro.handoff import decide_handoff

    cwd, linked, main_root, fires_form = CWD_FORMS[form]
    args = (spec, *rest)
    decision = decide_handoff(_facts(kind=kind, args=args, cwd=cwd, is_linked_worktree=linked, main_root=main_root))
    expected = fires_form and not _single_task(kind, args)
    assert (decision.action == "handoff") is expected, (form, kind, args, decision)
    if not expected:
        assert decision.action == "none", (form, kind, args, decision)
    if form in ("main_root", "main_subdir"):
        assert decision.action == "none"  # a handed-off run cannot hand off again


@SETTINGS
@given(kind=st.sampled_from(["plan", "", "Implement", "implement2", "ck run"]), spec=slugs)
def test_p19_other_kinds_never_fire(kind, spec):
    from claude_kiro.handoff import decide_handoff

    cwd, linked, main_root, _ = CWD_FORMS["claude_worktree_root"]
    assert decide_handoff(_facts(kind=kind, args=(spec,), cwd=cwd, is_linked_worktree=linked, main_root=main_root)).action == "none"


WORDS = st.one_of(
    st.just(""),
    st.sampled_from(["--", "a b", "it's", 'say "hi"', "$HOME", "$(touch PWNED)", "`id`", "x=y", "--k=v w", "é ü 日本", "a\\b", "; rm -rf x", "*", "a\tb"]),
    st.text(alphabet=string.ascii_letters + string.digits + " '\"$=-_\\`;&|<>*?()é日", max_size=12),
)


@SETTINGS
@given(kind=kinds, spec=slugs, rest=st.lists(WORDS, max_size=6))
def test_p20_rest_args_round_trip_through_shlex(kind, spec, rest):
    from claude_kiro.handoff import decide_handoff

    args = (spec, *rest)
    assume(not _single_task(kind, args))
    cwd, linked, main_root, _ = CWD_FORMS["claude_worktree_root"]
    decision = decide_handoff(_facts(kind=kind, args=args, cwd=cwd, is_linked_worktree=linked, main_root=main_root))
    assert decision.action == "handoff", decision
    assert tuple(decision.run_args) == (spec, "--command", kind)
    assert tuple(decision.rest) == tuple(rest)
    assert tuple(shlex.split(shlex.join(decision.rest))) == tuple(rest)


tri = st.sampled_from([True, False, None])
shas = st.one_of(st.none(), st.text(alphabet="0123456789abcdef", min_size=40, max_size=40))


@SETTINGS
@given(
    spec=slugs, spec_same=tri, main_clean=tri, dirty=tri, in_main=st.booleans(), in_wt=st.booleans(),
    head=shas, other=st.text(alphabet="0123456789abcdef", min_size=40, max_size=40),
    run_at_kind=st.sampled_from(["none", "head", "other"]), kind=kinds,
)
def test_p31_spec_state_decides_handoff_run_branch_or_refusal(spec, spec_same, main_clean, dirty, in_main, in_wt, head, other,
                                                              run_at_kind, kind):
    from claude_kiro.handoff import decide_handoff

    run_at = None if run_at_kind == "none" else (head if run_at_kind == "head" else other)
    if run_at_kind == "head":
        assume(head is not None)
    cwd, linked, main_root, _ = CWD_FORMS["claude_worktree_root"]
    facts = _facts(kind=kind, args=(spec,), cwd=cwd, is_linked_worktree=linked, main_root=main_root, spec_in_main=in_main,
                   spec_in_worktree=in_wt, worktree_head=head, spec_same=spec_same, main_clean=main_clean,
                   worktree_spec_dirty=dirty, run_branch_at=run_at)
    d = decide_handoff(facts)  # never raises
    if not (in_main or in_wt):
        assert d.action == "refuse" and d.recovery, d
        return
    if spec_same is True:
        assert d.action == "handoff" and d.run_branch is None and d.start_point is None, d
        return
    branch_ok = spec_same is False and main_clean is True and dirty is False and head is not None and run_at in (None, head)
    if branch_ok:
        assert d.action == "handoff", d
        assert d.run_branch == f"{spec}-run" and d.start_point == head, d
    else:
        assert d.action == "refuse", d
        assert d.recovery, d
        text = "\n".join(d.recovery)
        assert f"ck run {spec}" in text, d
        if head is not None:
            assert f"git switch -c {spec}-run {head}" in text, d
