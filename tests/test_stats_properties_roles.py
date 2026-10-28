"""Properties P-3 and P-4: the `parse_label` grammar (requirements 5.1, 5.2).

Calls the decision seam `claude_kiro.analytics.parse_label(label, workflow) -> dict`
(keys `role, task, attempt, round`) directly; no CLI, no server.
"""

import string

from hypothesis import HealthCheck, given, settings, strategies as st

PROPERTIES = {
    "P-3": "test_p3_template_labels_parse_exactly",
    "P-4": "test_p4_non_template_labels_are_other",
}

SETTINGS = settings(
    derandomize=True,
    max_examples=200,
    database=None,
    deadline=None,
    suppress_health_check=list(HealthCheck),
)

IMPL = "spec-implement"
CREATE = "spec-create"

ID_CHARS = string.digits + string.ascii_letters + "_"
task_ids = st.one_of(
    st.integers(min_value=1, max_value=999).map(str),
    st.sampled_from(["3", "17", "3_a", "x", "T1", "a_b_2"]),
    st.text(alphabet=ID_CHARS, min_size=1, max_size=8),
)
counts = st.integers(min_value=1, max_value=999)
models = st.sampled_from(["opus", "sonnet", "haiku", "claude-opus-5-5"])
workflows_impl = st.sampled_from([IMPL, "spec-implement-iso"])
lens_text = st.text(alphabet=string.ascii_lowercase + " -", min_size=1, max_size=12).filter(
    lambda s: s.strip() == s and s
)
what_text = st.text(alphabet=string.ascii_lowercase + " -", min_size=1, max_size=12).filter(
    lambda s: s.strip() == s and s and not s.startswith("Task")
)


def _parse(label, workflow):
    from claude_kiro.analytics import parse_label

    return parse_label(label, workflow)


def _impl_case():
    """Returns strategy of (label, workflow, expected) for implement templates."""
    N, K, R, M = task_ids, counts, counts, models
    role_tag = st.sampled_from([("", "implementer"), (" [test]", "test-writer")])
    opt_model = st.one_of(st.none(), M)

    task_plain = st.builds(
        lambda n, rt: (f"task {n}{rt[0]}", {"role": rt[1], "task": n, "attempt": 1}),
        N, role_tag,
    )
    task_retry = st.builds(
        lambda n, rt, k, m: (
            f"task {n}{rt[0]} retry {k}" + (f" ({m})" if m else ""),
            {"role": rt[1], "task": n, "attempt": k + 1},
        ),
        N, role_tag, K, opt_model,
    )
    task_revise = st.builds(
        lambda n, rt, r, m: (
            f"task {n}{rt[0]} revise {r}" + (f" ({m})" if m else ""),
            {"role": rt[1], "task": n, "round": r},
        ),
        N, role_tag, R, opt_model,
    )
    review = st.builds(
        lambda n, r: (
            f"review {n}" + (f" round {r}" if r else ""),
            {"role": "reviewer", "task": n, "round": r or 1},
        ),
        N, st.one_of(st.none(), R),
    )
    resolve = st.builds(
        lambda n, r: (
            f"resolve {n}" + (f" round {r}" if r else ""),
            {"role": "resolver", "task": n, "round": r or 1},
        ),
        N, st.one_of(st.none(), R),
    )
    fix_landing = st.builds(
        lambda k, n, m: (
            f"fix {k} (Task {n} red on landing)" + (f" [{m}]" if m else ""),
            {"role": "fixer", "task": n, "attempt": k},
        ),
        K, N, opt_model,
    )
    fix_target = st.builds(
        lambda k, w, m: (
            f"fix {k} ({w})" + (f" [{m}]" if m else ""),
            {"role": "fixer", "task": None, "attempt": k},
        ),
        K, what_text, opt_model,
    )
    recheck = st.builds(lambda k: (f"re-check {k}", {"role": "gate", "attempt": k}), K)
    full_gate = st.builds(lambda k: (f"full gate @{k}", {"role": "gate"}), K)
    final_gate = st.just(("final gate", {"role": "gate"}))
    land = st.builds(
        lambda ns: ("land " + " ".join(ns), {"role": "land"}),
        st.lists(N, min_size=1, max_size=4),
    )
    merge = st.builds(lambda n: (f"merge {n}", {"role": "land", "task": n}), N)
    cross = st.builds(lambda w: (f"cross-test wave {w}", {"role": "cross-test"}), K)
    plan = st.builds(
        lambda k, retry: (
            f"plan {k}" + (" retry" if retry else ""),
            {"role": "plan", "attempt": k},
        ),
        K, st.booleans(),
    )
    report = st.just(("report", {"role": "report"}))
    return st.one_of(
        task_plain, task_retry, task_revise, review, resolve, fix_landing, fix_target,
        recheck, full_gate, final_gate, land, merge, cross, plan, report,
    )


CREATE_FIXED = [
    ("write requirements.md", "implementer"),
    ("write design.md", "implementer"),
    ("write implementation track", "implementer"),
    ("link tracks into tasks.md", "implementer"),
    ("write test-plan.md", "test-writer"),
    ("write test track", "test-writer"),
    ("revise spec + write review.md", "fixer"),
    ("snapshot spec before revision", "land"),
    ("remove pre-revision snapshot", "land"),
]


def _create_case():
    fixed = st.sampled_from(CREATE_FIXED).map(lambda t: (t[0], {"role": t[1]}))
    review = st.builds(lambda l: (f"review: {l}", {"role": "reviewer"}), lens_text)
    refute = st.builds(lambda l: (f"refute: {l}", {"role": "reviewer"}), lens_text)
    consistency = st.builds(
        lambda p: (f"consistency check: pass {p}", {"role": "gate", "round": p}), counts
    )
    return st.one_of(fixed, review, refute, consistency)


@SETTINGS
@given(case=_impl_case(), wf=workflows_impl)
def test_p3_template_labels_parse_exactly(case, wf):
    label, expected = case
    got = _parse(label, wf)
    for key, want in expected.items():
        assert got[key] == want, (label, key, got)


@SETTINGS
@given(case=_create_case())
def test_p3_create_templates_parse_exactly(case):
    label, expected = case
    got = _parse(label, CREATE)
    for key, want in expected.items():
        assert got[key] == want, (label, key, got)


NEAR_MISSES_IMPL = [
    "task", "task 3!", "task 3 retry x", "Task 3", "tasks 3", "task 3 revise", "task 3 retry",
    "review", "reviews 3", "land", "fix (x)", "resolve", "full gate", "full gate @", "plan",
    "", " ", "confidential()", "task 3 extra", "task  3", "task 3.1", "review 3 round",
    "review 3 round x", "fix x (y)", "cross-test wave", "re-check", "final  gate", "report now",
]
NEAR_MISSES_CREATE = [
    "review:", "refute:", "consistency check", "consistency check: pass", "write README.md",
    "write design", "confidential()", "", "Write design.md", "write design.md extra",
    "consistency check: pass x",
]

CLOSED_IMPL = [
    "task 3", "task 3 [test]", "task 3 retry 1", "task 3 retry 1 (opus)", "task 3 revise 2",
    "task 3_a revise 2 (sonnet)", "review 3", "review 3 round 2", "resolve 3",
    "resolve 3 round 2", "fix 1 (Task 3 red on landing)", "fix 1 (Task 3 red on landing) [opus]",
    "fix 2 (x)", "re-check 2", "full gate @2", "final gate", "cross-test wave 1", "plan 1",
    "plan 1 retry", "report", "merge 3",
]
OPEN_IMPL = ["land 3", "land 3 4"]
CLOSED_CREATE = [t[0] for t in CREATE_FIXED] + ["consistency check: pass 1"]
OPEN_CREATE = ["review: security", "refute: scope"]


def _decorated(closed, open_):
    plain = st.one_of(
        st.sampled_from(NEAR_MISSES_IMPL if closed is CLOSED_IMPL else NEAR_MISSES_CREATE),
    )
    lead = st.builds(
        lambda l, ws: ws + l,
        st.sampled_from(closed + open_), st.sampled_from([" ", "  ", "\t", "\n"]),
    )
    trail = st.builds(
        lambda l, ws: l + ws,
        st.sampled_from(closed), st.sampled_from([" ", "  ", "\t", "\n"]),
    )
    suffix = st.builds(
        lambda l, s: l + s,
        st.sampled_from(closed), st.sampled_from([" extra", " 1", "!", " [test]x", "."]),
    )
    return st.one_of(plain, lead, trail, suffix)


def _assert_other(label, wf):
    got = _parse(label, wf)
    assert got["role"] == "other", (label, got)
    assert got["task"] is None, (label, got)
    assert got["attempt"] is None, (label, got)
    assert got["round"] is None, (label, got)


@SETTINGS
@given(label=_decorated(CLOSED_IMPL, OPEN_IMPL), wf=workflows_impl)
def test_p4_non_template_labels_are_other(label, wf):
    _assert_other(label, wf)


@SETTINGS
@given(label=_decorated(CLOSED_CREATE, OPEN_CREATE))
def test_p4_create_non_template_labels_are_other(label):
    _assert_other(label, CREATE)
