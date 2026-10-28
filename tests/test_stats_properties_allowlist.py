"""P-1: the workflow allowlist seam `is_ck_workflow` (requirement 2.1). Pure: no CLI."""

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from stats_helpers import CONFIDENTIAL

PROPERTIES = {"P-1": "test_p1_is_ck_workflow_true_only_for_the_three_names"}

ALLOWED = ("spec-implement", "spec-implement-iso", "spec-create")

NEAR_MISSES = (
    "spec-implement-v2", "Spec-Implement", " spec-create", "spec-create\n", "spec_create", "spec-plan",
    "spec-implement-iso2", "my-spec-create", "spec-implement.js", "spec-implement ", "SPEC-CREATE",
    "spec-create\r\n", "spec-create\x00", "spec-review", "", "spec-implement-iso/", "spec-implement/",
    "\tspec-implement", "spec–create", "spec-implement-ISO",
)
FOREIGN = ("competitor-teardown-q4", "tax-review-2026")


def _confidential_forms(t):
    esc = json.dumps(t)[1:-1].replace("/", "\\/")
    return [
        t, t.upper(), t.lower(), t.title(), esc, f"see {t}, ok", f"({t}).", f"/x/{t}/y", f"{t}/y", f"./{t}",
        f"../{t}", f"~/{t}", f"C:\\{t}\\y", f"integrate/{t}", f"feat/{t}-1", f"https://github.com/o/{t}",
        f"git@github.com:o/{t}.git", f"Name <{t}@b.c>", f"a+tag@{t}",
    ]


def confidential():
    return st.sampled_from(CONFIDENTIAL).flatmap(lambda t: st.sampled_from(_confidential_forms(t)))


non_strings = st.sampled_from([None, 1, 0, 1.5, True, False, [], ["spec-create"], {}, {"spec-create": 1}, b"spec-create", ("spec-create",)])

# Every allowed name is a legal confidential/other string only if it is not exactly allowed.
names = st.one_of(
    st.sampled_from(ALLOWED),
    st.sampled_from(NEAR_MISSES),
    st.sampled_from(FOREIGN),
    confidential(),
    non_strings,
    st.text(max_size=30),
)


@settings(derandomize=True, max_examples=200, database=None, deadline=None)
@given(names)
def test_p1_is_ck_workflow_true_only_for_the_three_names(name):
    from claude_kiro.analytics import is_ck_workflow

    expected = isinstance(name, str) and name in ALLOWED
    assert is_ck_workflow(name) is expected, repr(name)


def test_p1_every_listed_name_is_decided():
    from claude_kiro.analytics import is_ck_workflow

    for n in ALLOWED:
        assert is_ck_workflow(n) is True
    for n in (*NEAR_MISSES, *FOREIGN, *CONFIDENTIAL):
        assert is_ck_workflow(n) is False, repr(n)
    for n in (None, 1, [], ["spec-create"]):
        assert is_ck_workflow(n) is False


def test_p1_allowlist_constant_is_exactly_the_three_names():
    from claude_kiro.analytics import ALLOWED_WORKFLOWS

    assert set(ALLOWED_WORKFLOWS) == set(ALLOWED)
    assert len(ALLOWED_WORKFLOWS) == 3
