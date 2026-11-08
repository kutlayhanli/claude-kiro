"""P-8, P-9, P-10, P-11 and P-33 (TC-174): conflict normalisation, retry digests, test-fix scope, plan and proof.

- Python seams (200 examples): `claude_kiro.testfix.scope_decision` (P-10) and `proof_decision` (P-33a).
- Workflow seams through the Node bridge (60 examples): `testConflictsOf` (P-8), `failuresForPrompt` (P-9),
  `testFixPlan` (P-11) and the whole test-fix loop of `spec-implement.js` under the fake runtime (P-33b).
Every property is derandomized, has no example database and no per-example deadline. Nothing here mocks project code.

Shapes assumed from design.md: `testConflictsOf(result)` returns the conflict list (a dict with `conflicts` and
`malformed` is accepted too); `failuresForPrompt` returns `{shown, more: {file: count}, total}`; `testFixPlan` returns
`{groups: [{file, conflicts}], toMe: [conflict]}` and takes `alreadyFixed` as `[{node, at, fix}]`.
"""

import re
from collections import Counter
from typing import Any, Dict, List

from hypothesis import HealthCheck, given, settings, strategies as st

from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

PROPERTIES = {
    "P-8": "test_tc174_p8_conflicts_of_normalises_one_or_many",
    "P-9": "test_tc174_p9_failures_for_prompt_lists_first_n_and_counts_the_rest",
    "P-10": "test_tc174_p10_scope_decision_allows_only_test_paths",
    "P-11": "test_tc174_p11_test_fix_plan_partitions_conflicts",
    "P-33": "test_tc174_p33a_proof_decision_counts_only_passed_or_not_built",
}

COMMON = dict(derandomize=True, database=None, deadline=None, suppress_health_check=list(HealthCheck))
SEAM = dict(max_examples=200, **COMMON)
BRIDGE = dict(max_examples=60, **COMMON)

FILES = [f"tests/test_f{i}.py" for i in range(1, 7)]
TESTS = [f"test_tc{i}_x" for i in range(1, 6)]


def key(c: Dict[str, Any]):
    return (c.get("file"), c.get("test"))


# ----------------------------------------------------------------------------------------------- P-8

conflict_st = st.fixed_dictionaries(
    {"file": st.sampled_from(FILES), "test": st.sampled_from(TESTS)},
    optional={"criterion": st.sampled_from(["1.1", "3.7"]), "reason": st.text(max_size=5), "fix": st.text(max_size=5)},
)
no_file_st = st.fixed_dictionaries({"test": st.sampled_from(TESTS)}, optional={"reason": st.text(max_size=5)})
entry_st = st.one_of(conflict_st, conflict_st, no_file_st)


def conflicts_of(bridge, result):
    out = bridge.call("testConflictsOf", result)
    if isinstance(out, dict):
        return list(out.get("conflicts") or []), list(out.get("malformed") or [])
    return list(out), []


def split(out_conflicts, malformed):
    """Valid entries (with a file) and the malformed ones reported by the seam, either apart or flagged in the list."""
    valid = [c for c in out_conflicts if isinstance(c, dict) and c.get("file") and not c.get("malformed")]
    reported = list(malformed) + [c for c in out_conflicts if not (isinstance(c, dict) and c.get("file") and not c.get("malformed"))]
    return valid, reported


@given(c=conflict_st)
@settings(**BRIDGE)
def test_tc174_p8_single_conflict_equals_list_of_one(bridge, c):  # noqa: F811
    assert conflicts_of(bridge, {"testConflict": c}) == conflicts_of(bridge, {"testConflicts": [c]})
    assert [key(x) for x in conflicts_of(bridge, {"testConflict": c})[0]] == [key(c)]


@given(
    single=st.one_of(st.none(), entry_st),
    many=st.one_of(st.none(), st.lists(st.one_of(entry_st, st.integers(), st.text(max_size=3), st.none()), max_size=6)),
)
@settings(**BRIDGE)
def test_tc174_p8_conflicts_of_normalises_one_or_many(bridge, single, many):  # noqa: F811
    result: Dict[str, Any] = {"task": "1", "status": "blocked"}
    if single is not None:
        result["testConflict"] = single
    if many is not None:
        result["testConflicts"] = many
    valid, reported = split(*conflicts_of(bridge, result))
    entries = ([single] if single is not None else []) + (many or [])
    objects = [e for e in entries if isinstance(e, dict)]
    good = [e for e in objects if e.get("file")]
    bad = [e for e in entries if not (isinstance(e, dict) and e.get("file"))]
    # union, deduplicated by (file, test)
    keys = [key(c) for c in valid]
    assert len(keys) == len(set(keys)), keys
    assert set(keys) == {key(e) for e in good}
    # entries without a file are reported, never silently dropped
    if bad:
        assert reported, f"malformed entries {bad} were dropped silently"
    else:
        assert not reported


# ----------------------------------------------------------------------------------------------- P-9

failure_st = st.builds(lambda f, t: {"node": f"{f}::{t}", "file": f, "tcId": "TC-1", "kind": "assertion"},
                       st.sampled_from([f"tests/test_g{i}.py" for i in range(10)]), st.sampled_from(["test_a", "test_b", "test_c"]))


@given(failures=st.lists(failure_st, max_size=300), limit=st.sampled_from([50, 50, 50, 1, 7]))
@settings(**BRIDGE)
def test_tc174_p9_failures_for_prompt_lists_first_n_and_counts_the_rest(bridge, failures, limit):  # noqa: F811
    n = len(failures)
    out = bridge.call("failuresForPrompt", failures, limit)
    assert out["total"] == n
    assert out["shown"] == failures[: min(n, limit)]
    rest = Counter(f["file"] for f in failures[limit:])
    assert dict(out["more"]) == dict(rest)
    assert sum(out["more"].values()) == max(0, n - limit)
    assert len(out["shown"]) + sum(out["more"].values()) == n  # no entry is both listed and counted


@given(failures=st.lists(failure_st, max_size=300))
@settings(**BRIDGE)
def test_tc174_p9_default_limit_is_fifty(bridge, failures):  # noqa: F811
    out = bridge.call("failuresForPrompt", failures)
    assert out["shown"] == failures[:50]
    assert sum(out["more"].values()) == max(0, len(failures) - 50)


# ---------------------------------------------------------------------------------------------- P-10

TEST_PATHS = ["tests/test_x.py", "tests/sub/conftest.py", "tests/fixtures/a.json", "tests/test_calc_tc.py", "tests/ä.py",
              "tests/my test.py", "tests/fixtures/sub dir/data.json", '"tests/\\303\\244.py"']
NON_TEST = ["src/tests.py", "tests_old/x.py", "src/tests/fixtures.py", "specs/demo/test-plan.md", "specs/demo/requirements.md",
            "specs/other/test-plan.md", ".claude/x", "pyproject.toml", "conftest.py", "src/app.py", "src/ä.py", "my file.py",
            '"src/\\303\\244.py"', "README.md", "specs/demo/tasks.md", "mytests/test_x.py", "tests.py"]
PLAN = "# Test Plan\n\n| ID | Criterion |\n| TC-1 | 1.1 |\n"
PLAN_NEW_ROW = PLAN + "| TC-2 | 1.2 |\n"


def decide(paths, before=PLAN, after=PLAN, statuses=None):
    from claude_kiro.testfix import Change, scope_decision

    statuses = statuses or ["M"] * len(paths)
    return scope_decision([Change(p, s) for p, s in zip(paths, statuses)], before, after, {})


path_st = st.sampled_from(TEST_PATHS + NON_TEST)
status_st = st.sampled_from(["A", "M", "D", "R"])


@given(paths=st.lists(path_st, min_size=1, max_size=6), statuses=st.lists(status_st, min_size=6, max_size=6))
@settings(**SEAM)
def test_tc174_p10_scope_decision_allows_only_test_paths(paths, statuses):
    want = all(p in TEST_PATHS for p in paths)
    got = decide(paths, statuses=statuses[: len(paths)])
    assert got.in_scope is want, (paths, got)
    if not want:
        assert got.outside, got  # the files that are not test files or fixtures are named


@given(allowed=st.lists(st.sampled_from(TEST_PATHS), min_size=1, max_size=4), bad=st.sampled_from(NON_TEST), pos=st.integers(0, 4))
@settings(**SEAM)
def test_tc174_p10_complement_a_non_test_path_makes_it_refused(allowed, bad, pos):
    assert decide(allowed).in_scope is True
    mixed = list(allowed)
    mixed.insert(min(pos, len(mixed)), bad)
    assert decide(mixed).in_scope is False


@given(refused=st.lists(st.sampled_from(NON_TEST), min_size=1, max_size=3), extra=st.lists(st.sampled_from(TEST_PATHS), min_size=1, max_size=3))
@settings(**SEAM)
def test_tc174_p10_monotone_adding_a_test_path_never_allows(refused, extra):
    assert decide(refused).in_scope is False
    assert decide(refused + extra).in_scope is False
    assert decide(extra + refused).in_scope is False


@given(paths=st.lists(st.sampled_from(TEST_PATHS), min_size=0, max_size=3), row=st.sampled_from([PLAN_NEW_ROW, PLAN.replace("1.1", "9.9"), PLAN + "\n"]))
@settings(**SEAM)
def test_tc174_p10_any_change_to_the_test_plan_is_out_of_scope(paths, row):
    # Only a new row (or any edit at all) in the plan, with every changed path a test file: still refused.
    got = decide(paths, PLAN, row)
    assert got.in_scope is False, got
    assert decide(paths + ["specs/demo/test-plan.md"], PLAN, PLAN).in_scope is False


# ---------------------------------------------------------------------------------------------- P-11

fix_conflict_st = st.builds(lambda f, t: {"file": f, "test": t, "criterion": "1.1", "reason": "r", "fix": "f"},
                            st.sampled_from(FILES[:5]), st.sampled_from(TESTS))


def node_of(c):
    return f"{c['file']}::{c['test']}"


def plan_of(bridge, conflicts, prior_nodes):
    already = [{"node": n, "at": "abc1234", "fix": "testfix1"} for n in prior_nodes]
    out = bridge.call("testFixPlan", {"conflicts": conflicts, "alreadyFixed": already})
    return out["groups"], out["toMe"]


@given(conflicts=st.lists(fix_conflict_st, max_size=20), data=st.data())
@settings(**BRIDGE)
def test_tc174_p11_test_fix_plan_partitions_conflicts(bridge, conflicts, data):  # noqa: F811
    nodes = sorted({node_of(c) for c in conflicts})
    prior = data.draw(st.lists(st.sampled_from(nodes + ["tests/test_other.py::test_unrelated"]) if nodes else st.just("tests/test_other.py::test_unrelated"), max_size=8))
    groups, to_me = plan_of(bridge, conflicts, prior)
    in_groups = [key(c) for g in groups for c in g["conflicts"]]
    in_me = [key(c) for c in to_me]
    distinct = {key(c) for c in conflicts}
    # every conflict appears exactly once, in a group or in toMe
    assert sorted(in_groups + in_me) == sorted(distinct)
    # prior-fixed nodes are never in a group; and they are exactly toMe
    assert not any(f"{f}::{t}" in set(prior) for f, t in in_groups)
    assert {f"{f}::{t}" for f, t in in_me} == {n for n in nodes if n in set(prior)}
    # exactly one group per file, each holding only conflicts of that file
    files = [g["file"] for g in groups]
    assert len(files) == len(set(files))
    assert set(files) == {f for f, _ in in_groups}
    for g in groups:
        assert g["conflicts"] and all(c["file"] == g["file"] for c in g["conflicts"])


@given(conflicts=st.lists(fix_conflict_st, min_size=1, max_size=20), data=st.data())
@settings(**BRIDGE)
def test_tc174_p11_removing_a_prior_fix_never_moves_another_to_me(bridge, conflicts, data):  # noqa: F811
    nodes = sorted({node_of(c) for c in conflicts})
    prior = data.draw(st.lists(st.sampled_from(nodes), unique=True, max_size=8))
    _, full_me = plan_of(bridge, conflicts, prior)
    for removed in prior:
        _, less_me = plan_of(bridge, conflicts, [n for n in prior if n != removed])
        assert {key(c) for c in less_me} <= {key(c) for c in full_me}
        assert {key(c) for c in full_me} - {key(c) for c in less_me} <= {tuple(removed.split("::"))}


# ---------------------------------------------------------------------------------------------- P-33a

outcome_st = st.sampled_from([
    (True, None), (False, "not-built"), (False, "bad"), (False, "unclear"),
    (False, "skipped"), (False, "xfailed"), (False, "deselected"), (False, "missing"),
])


@given(outs=st.lists(outcome_st, min_size=1, max_size=8))
@settings(**SEAM)
def test_tc174_p33a_proof_decision_counts_only_passed_or_not_built(outs):
    from claude_kiro.testfix import ProofOutcome, proof_decision

    outcomes = [ProofOutcome(f"tests/test_t.py::test_{i}", passed, cls) for i, (passed, cls) in enumerate(outs)]
    verdict = proof_decision(outcomes)
    want = all(p or c == "not-built" for p, c in outs)
    assert verdict.proved is want, outs


@given(kind=st.sampled_from(["skipped", "xfailed", "deselected", "missing"]), others=st.lists(st.sampled_from([(True, None), (False, "not-built")]), max_size=5), pos=st.integers(0, 5))
@settings(**SEAM)
def test_tc174_p33a_a_skipped_xfailed_deselected_or_missing_node_is_never_proved(kind, others, pos):
    from claude_kiro.testfix import ProofOutcome, proof_decision

    outs = list(others)
    outs.insert(min(pos, len(outs)), (False, kind))
    outcomes = [ProofOutcome(f"tests/test_t.py::test_{i}", p, c) for i, (p, c) in enumerate(outs)]
    assert proof_decision(outcomes).proved is False


# ---------------------------------------------------------------------------------------------- P-33b

CONFLICT = {"file": "tests/test_x.py", "test": "test_tc1_x", "criterion": "1.1", "reason": "can never pass", "fix": "fix it"}
LAND_RE = re.compile(r"worktree land \S+ testfix\d+|^land\b.*testfix", re.S)


def landed_testfix(run) -> bool:
    labels = [e["label"] for e in run["trace"] if e.get("ev") == "request"]
    return any(LAND_RE.search(label) for label in labels) or any(LAND_RE.search(p) for p in run["prompts"].values())


@given(scope_ok=st.booleans(), proved=st.booleans(), approve=st.booleans(), delay=st.integers(0, 3), contradictory=st.booleans())
@settings(**BRIDGE)
def test_tc174_p33b_land_iff_scope_proof_and_review_all_hold(bridge, scope_ok, proved, approve, delay, contradictory):  # noqa: F811
    f = CONFLICT["file"]
    node = f"{f}::{CONFLICT['test']}"
    proof: Dict[str, Any] = {"proved": proved, "reason": "decided"}
    if proved and contradictory:
        # a proof that says proved with a failing outcome is not a proof
        proof["outcomes"] = [{"node": node, "passed": False, "cls": "bad"}]
    if not proved:
        proof["outcomes"] = [{"node": node, "passed": False, "cls": "bad"}]
    review = {"verdict": "approve", "findings": [], "summary": "ok"} if approve else {"verdict": "changes", "findings": [{"severity": "blocking", "test": CONFLICT["test"]}], "summary": "no"}
    scope = {"in_scope": True} if scope_ok else {"in_scope": False, "outside": ["src/app.py"], "reason": "source changed"}
    project = {
        "deps": {"1": []},
        "testConflicts": {"1": {"conflict": CONFLICT}},
        "testFix": {f: {"verdict": "fixed", "proof": proof, "scope": scope, "review": review}},
        "durations": {"1": 1 + delay},
        "mergeDurations": {"1": 1 + delay},
    }
    run = bridge.run(project, args={"onFailure": "continue"}, clock="virtual")
    effective_proved = proved and not contradictory
    should_land = scope_ok and effective_proved and approve
    assert landed_testfix(run) is should_land, (scope_ok, proved, approve, contradictory, run["result"].get("needsInput"))
    if not should_land:
        assert run["result"].get("needsInput"), "a refused or unproven fix must end in needs-input"
