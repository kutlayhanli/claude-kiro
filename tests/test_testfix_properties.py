"""P-8, P-9, P-10, P-11 and P-33 (TC-174): conflict normalisation, retry digests, test-fix scope, plan and proof.

- Python seams (200 examples): `claude_kiro.testfix.scope_decision` (P-10) and `proof_decision` (P-33a).
- Workflow seams through the Node bridge (60 examples): `testConflictsOf` (P-8), `failuresForPrompt` (P-9),
  `testFixPlan` (P-11) and the whole test-fix loop of `spec-implement.js` under the fake runtime (P-33b).
Every property is derandomized, has no example database and no per-example deadline. Nothing here mocks project code.

Shapes from design.md: `testConflictsOf(result)` returns an array of TestConflict (design.md's seam table gives no other
shape and no `malformed` field); `scope_decision` reads `config["tests"]["dirs"|"files"]` (as `is_test_file` does) and
`config["fixtures"]["recorded"]`; `failuresForPrompt` returns `{shown, more: {file: count}, total}`; `testFixPlan` returns
`{groups: [{file, conflicts}], toMe: [conflict]}` and takes `alreadyFixed` as `[{node, at, fix}]`.
"""

import json
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from arbiter_helpers import arbiter_project
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
    assert isinstance(out, list), f"testConflictsOf must return TestConflict[] (design.md seam table), got {out!r}"
    return out


@given(c=conflict_st)
@settings(**BRIDGE)
def test_tc174_p8_single_conflict_equals_list_of_one(bridge, c):  # noqa: F811
    assert conflicts_of(bridge, {"testConflict": c}) == conflicts_of(bridge, {"testConflicts": [c]})
    assert [key(x) for x in conflicts_of(bridge, {"testConflict": c})] == [key(c)]


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
    out = conflicts_of(bridge, result)
    entries = ([single] if single is not None else []) + (many or [])
    good = [e for e in entries if isinstance(e, dict) and e.get("file")]
    # design.md does not say how an entry without a file is reported, so only the array of valid entries is pinned:
    # the valid entries, deduplicated by (file, test), each once. Anything else the seam adds must be a non-conflict.
    valid = [c for c in out if isinstance(c, dict) and c.get("file")]
    keys = [key(c) for c in valid]
    assert len(keys) == len(set(keys)), keys
    assert set(keys) == {key(e) for e in good}


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

# Two configs: the same default-like test globs, with and without `conftest.py` among the file globs.
FIXTURES = ["tests/fixtures/*"]
CONFIG_WITH_CONFTEST = {"tests": {"dirs": ["tests"], "files": ["test_*.py", "conftest.py"]}, "fixtures": {"recorded": FIXTURES}}
CONFIG_NO_CONFTEST = {"tests": {"dirs": ["tests"], "files": ["test_*.py"]}, "fixtures": {"recorded": FIXTURES}}
CONFIGS = [CONFIG_WITH_CONFTEST, CONFIG_NO_CONFTEST]

PATHS = ["tests/test_x.py", "tests/sub/conftest.py", "tests/fixtures/a.json", "tests/test_calc_tc.py", "tests/ä.py",
         "tests/my test.py", "tests/fixtures/sub dir/data.json", "tests/data.json", "tests/__pycache__/x.pyc",
         "src/tests.py", "tests_old/x.py", "tests_old/test_x.py", "mytests/test_x.py", "mytests/helper.py", "tests.py",
         "specs/demo/test-plan.md", "specs/demo/requirements.md", "specs/other/test-plan.md", "specs/demo/tasks.md",
         ".claude/x", "pyproject.toml", "conftest.py", "src/app.py", "src/ä.py", "my file.py", "README.md"]


def is_test_or_fixture(path, config):
    """The expected verdict for one path, derived from the config: a test file per the project's test globs, or a
    recorded fixture, and never a spec document."""
    from fnmatch import fnmatch

    from claude_kiro.hooks._shared.test_heuristics import is_test_file

    if path.startswith("specs/"):
        return False
    return is_test_file(path, config) or any(fnmatch(path, g) for g in config["fixtures"]["recorded"])


def test_tc174_p10_the_two_configs_disagree_exactly_on_root_conftest():
    # Root conftest.py is allowed only when the test globs match it: both ways, the expectation comes from the config.
    assert is_test_or_fixture("conftest.py", CONFIG_WITH_CONFTEST) is True
    assert is_test_or_fixture("conftest.py", CONFIG_NO_CONFTEST) is False
    from claude_kiro.testfix import Change, scope_decision

    assert scope_decision([Change("conftest.py", "M")], PLAN, PLAN, CONFIG_WITH_CONFTEST).in_scope is True
    assert scope_decision([Change("conftest.py", "M")], PLAN, PLAN, CONFIG_NO_CONFTEST).in_scope is False


PLAN = "# Test Plan\n\n| ID | Criterion |\n| TC-1 | 1.1 |\n"
PLAN_NEW_ROW = PLAN + "| TC-2 | 1.2 |\n"


def decide(paths, before=PLAN, after=PLAN, statuses=None, config=CONFIG_WITH_CONFTEST):
    from claude_kiro.testfix import Change, scope_decision

    statuses = statuses or ["M"] * len(paths)
    return scope_decision([Change(p, s) for p, s in zip(paths, statuses)], before, after, config)


path_st = st.sampled_from(PATHS)
status_st = st.sampled_from(["A", "M", "D", "R"])
config_st = st.sampled_from(CONFIGS)


@given(paths=st.lists(path_st, min_size=1, max_size=6), statuses=st.lists(status_st, min_size=6, max_size=6), config=config_st)
@settings(**SEAM)
def test_tc174_p10_scope_decision_allows_only_test_paths(paths, statuses, config):
    want = all(is_test_or_fixture(p, config) for p in paths)
    got = decide(paths, statuses=statuses[: len(paths)], config=config)
    assert got.in_scope is want, (paths, got)
    if not want:
        assert got.outside, got  # the files that are not test files or fixtures are named


@given(data=st.data(), config=config_st)
@settings(**SEAM)
def test_tc174_p10_complement_a_non_test_path_makes_it_refused(data, config):
    allowed_pool = [p for p in PATHS if is_test_or_fixture(p, config)]
    bad_pool = [p for p in PATHS if not is_test_or_fixture(p, config)]
    allowed = data.draw(st.lists(st.sampled_from(allowed_pool), min_size=1, max_size=4))
    bad = data.draw(st.sampled_from(bad_pool))
    pos = data.draw(st.integers(0, 4))
    assert decide(allowed, config=config).in_scope is True
    mixed = list(allowed)
    mixed.insert(min(pos, len(mixed)), bad)
    assert decide(mixed, config=config).in_scope is False


@given(data=st.data(), config=config_st)
@settings(**SEAM)
def test_tc174_p10_monotone_adding_a_test_path_never_allows(data, config):
    allowed_pool = [p for p in PATHS if is_test_or_fixture(p, config)]
    bad_pool = [p for p in PATHS if not is_test_or_fixture(p, config)]
    refused = data.draw(st.lists(st.sampled_from(bad_pool), min_size=1, max_size=3))
    extra = data.draw(st.lists(st.sampled_from(allowed_pool), min_size=1, max_size=3))
    assert decide(refused, config=config).in_scope is False
    assert decide(refused + extra, config=config).in_scope is False
    assert decide(extra + refused, config=config).in_scope is False


@given(data=st.data(), config=config_st, row=st.sampled_from([PLAN_NEW_ROW, PLAN.replace("1.1", "9.9"), PLAN + "\n"]))
@settings(**SEAM)
def test_tc174_p10_any_change_to_the_test_plan_is_out_of_scope(data, config, row):
    # Only a new row (or any edit at all) in the plan, with every changed path a test file: still refused.
    paths = data.draw(st.lists(st.sampled_from([p for p in PATHS if is_test_or_fixture(p, config)]), min_size=0, max_size=3))
    got = decide(paths, PLAN, row, config=config)
    assert got.in_scope is False, got
    assert decide(paths + ["specs/demo/test-plan.md"], PLAN, PLAN, config=config).in_scope is False


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

# Gap in design.md: ProofOutcome(node, passed, cls) has no status for skipped, xfailed, deselected or missing nodes
# (`cls` is classify_not_built's "not-built" | "bad" | "unclear"). These four stand for "not passed, not not-built";
# task 57 or a design amendment must settle how they are represented (a status field, or the requested nodes passed in).
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
COMBOS = [(scope_ok, proved, approve) for scope_ok in (True, False) for proved in (True, False) for approve in (True, False)]


def landed_testfix(run) -> bool:
    # Only the thin `land ...` request labels count; prompt text that merely mentions a land is not a land.
    labels = [e["label"] for e in run["trace"] if e.get("ev") == "request"]
    return any(LAND_RE.search(label) for label in labels)


@pytest.mark.parametrize("scope_ok,proved,approve", COMBOS)
@given(delay=st.integers(0, 3), contradictory=st.booleans())
@settings(**BRIDGE)
def test_tc174_p33b_land_iff_scope_proof_and_review_all_hold(bridge, scope_ok, proved, approve, delay, contradictory):  # noqa: F811
    f = CONFLICT["file"]
    node = f"{f}::{CONFLICT['test']}"
    proof: Dict[str, Any] = {"proved": proved, "reason": "PROOFREASON"}
    if (proved and contradictory) or not proved:
        # a proof that says proved with a failing outcome is not a proof
        proof["outcomes"] = [{"node": node, "passed": False, "cls": "bad"}]
    review = {"verdict": "approve", "findings": [], "summary": "ok"} if approve else {"verdict": "changes", "findings": [{"severity": "blocking", "test": CONFLICT["test"], "issue": "REVIEWFINDING"}], "summary": "REVIEWSUMMARY"}
    scope = {"in_scope": True} if scope_ok else {"in_scope": False, "outside": ["src/app.py"], "reason": "SCOPEREASON source changed"}
    project = {
        "deps": {"1": []},
        "testConflicts": {"1": {"conflict": CONFLICT}},
        "testFix": {f: {"verdict": "fixed", "proof": proof, "scope": scope, "review": review}},
        "durations": {"1": 1 + delay},
        "mergeDurations": {"1": 1 + delay},
        "reviewDurations": {"1": delay},
    }
    # the conflict reaches the fixer only through the arbiter's test_repair ruling (design D31-D33); the arbiter's thin
    # steps (`ck arbiter open`, `settle`, `record`) run the real CLI on a real git project
    project["rulings"] = {"1": [{"claim": "@key", "status": "ruled", "outcome": "test_repair", "principle": "project:test-intent",
                                 "reasoning": "the test asserts a value no correct code returns",
                                 "testRepair": {"file": f, "test": CONFLICT["test"], "change": CONFLICT["fix"], "why": CONFLICT["reason"]}}]}
    with tempfile.TemporaryDirectory() as tmp:
        repo = arbiter_project(Path(tmp))
        args = {"onFailure": "continue", **repo.run_args, "spec": "demo", "arbiterAgent": "spec:arbiter"}
        run = bridge.run(project, args=args, clock="virtual", exec_ck=repo.exec_ck)
    effective_proved = proved and not contradictory
    should_land = scope_ok and effective_proved and approve
    assert landed_testfix(run) is should_land, (scope_ok, proved, approve, contradictory, run["result"].get("needsInput"))
    if not should_land:
        entries = run["result"].get("needsInput") or []
        assert entries, "a refused or unproven fix must end in needs-input"
        entry = next((e for e in entries if e.get("kind") == "test-fix"), None)
        assert entry is not None, f"needs-input must be the test-fix entry (kind 'test-fix'): {entries}"
        text = json.dumps(entry)
        if not scope_ok:
            assert "src/app.py" in text, entry  # the files that are out of scope (4.5)
        elif not effective_proved:
            assert "PROOFREASON" in text or node in text, entry  # carries the proof result (4.6)
        else:
            assert "REVIEWFINDING" in text or "REVIEWSUMMARY" in text, entry  # carries the review findings (4.6)
