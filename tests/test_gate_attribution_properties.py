"""TC-173: property tests P-1 to P-6 of the unattended-correctness spec.

Decision seams: `claude_kiro.attribution` (`AttributionIndex`, `tc_of_node`, `attribute_failure`, `rerun_plan`,
`gate_verdict`) and `claude_kiro.failures` (`truncate_message`, `classify_kind`). P-5b alone runs the real CLI.
Project modules are imported inside the test bodies, so this file collects before the seams exist.

Shapes the design leaves open, fixed here (the impl task follows design.md and reports a conflict if it differs):
- `rerun_plan` and `gate_verdict` take `(Failure, Attribution)` pairs, because an `Attribution` carries no node.
  A pair's key is `failure.node`, or `"collect:<file>"` for a file-level failure.
- `gate_verdict(pairs, reruns, other_checks_ok)`: `reruns` maps key -> "passed on rerun".
  `GateVerdict.statuses` maps key -> status and `GateVerdict.red` is the verdict.
- `tc_of_node` returns one `"TC-<n>"` (any one of the name's tokens when there are several) or None.
"""

import itertools
import re
import shlex
import tempfile
import unicodedata
from pathlib import Path

from hypothesis import HealthCheck, assume, given, settings, strategies as st
from _pytest.mark.expression import Expression

from unattended_helpers import attr_project, ck_json, commit_all

PROPERTIES = {
    "P-1": ["test_tc173_p1_statuses_partition_and_excused_by", "test_tc173_p1a_tc_selection_over_coverage",
            "test_tc173_p1b_hint_never_changes_attribution"],
    "P-2": "test_tc173_p2_done_monotonicity",
    "P-3": ["test_tc173_p3_tc_tokens_function_name_only", "test_tc173_p3_verify_selection_matches_pytest_oracle"],
    "P-4": "test_tc173_p4_permutation_determinism",
    "P-5": ["test_tc173_p5a_truncation_bound_prefix_flag", "test_tc173_p5a_kind_is_one_of_five",
            "test_tc173_p5b_real_cli_kinds_and_counts"],
    "P-6": "test_tc173_p6_rerun_targets_and_verdict",
}

SETTINGS = dict(derandomize=True, max_examples=200, database=None, deadline=None,
                suppress_health_check=list(HealthCheck))
CLI_SETTINGS = dict(SETTINGS, max_examples=25)

KINDS = ("assertion", "import", "collection", "timeout", "error")
TOKEN = re.compile(r"(?<![A-Za-z0-9])tc(\d+)_", re.IGNORECASE)


def _num_sorted(tasks):
    return tuple(sorted(set(tasks), key=int))


# --------------------------------------------------------------------------- oracles


def _function_name(node_id):
    last = node_id.split("::")[-1]
    return last.split("[", 1)[0]


def _oracle_tokens(node_id):
    return sorted({int(m.group(1)) for m in TOKEN.finditer(_function_name(node_id))})


def _k_expression(command):
    """The `-k` expression pytest would use for one command: the last occurrence wins."""
    argv = shlex.split(command)
    expr = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-k" and i + 1 < len(argv):
            expr = argv[i + 1]
            i += 2
            continue
        if a.startswith("-k") and len(a) > 2:
            expr = a[2:]
        i += 1
    return expr


def _selects(command, n):
    """Pytest's own evaluator: does some name holding `tc<n>_`, built only from the expression's tokens, pass?"""
    expr = _k_expression(command)
    if expr is None:
        return False
    compiled = Expression.compile(expr)
    idents = sorted(set(re.findall(r"[A-Za-z0-9_]+", expr)) - {"and", "or", "not"})
    needle = f"tc{n}_"
    for r in range(1, len(idents) + 1):
        for combo in itertools.combinations(idents, r):
            name = "".join(combo).lower()
            if needle in name and compiled.evaluate(lambda ident, name=name: ident.lower() in name):
                return True
    return False


def _selected_by(commands, n):
    return any(_selects(c, n) for c in commands)


# --------------------------------------------------------------------------- generators

SELECTORS = [
    '-k "tc{a}_"', "-k 'tc{a}_'", "-k tc{a}_", "-ktc{a}_", '-k "tc{a}_ or tc{b}_"',
    '-k "(tc{a}_ or tc{b}_) and not slow"', '-k "not tc{a}_"', '-k "tc{a}_ and tc{b}_"',
    '-k "tc{b}_" -k "tc{a}_"', "",
]


@st.composite
def verify_commands(draw, tcs):
    """One Verify line: 1-2 commands, each with a selector form."""
    out = []
    for _ in range(draw(st.integers(1, 2))):
        a, b = draw(st.sampled_from(tcs)), draw(st.sampled_from(tcs))
        sel = draw(st.sampled_from(SELECTORS)).format(a=a, b=b)
        out.append(f"uv run pytest -q tests/test_cover.py {sel}".strip())
    return out


@st.composite
def spec_models(draw):
    ids = draw(st.sets(st.integers(1, 30), min_size=4, max_size=14))
    ids = sorted(ids)
    n_test = draw(st.integers(1, max(1, len(ids) // 3)))
    test_tasks = [str(i) for i in draw(st.permutations(ids))[:n_test]]
    impl = [str(i) for i in ids if str(i) not in test_tasks]
    assume(impl)
    files, verifies = {}, {}
    for t in test_tasks:
        for k in range(draw(st.integers(1, 3))):
            files[f"tests/test_t{t}_{k}.py"] = t
        verifies[t] = _num_sorted(draw(st.lists(st.sampled_from(impl), max_size=4)))
    tcs = sorted(draw(st.sets(st.integers(1, 120), min_size=2, max_size=12)))
    covering = {n: draw(st.sampled_from(test_tasks + [None])) for n in tcs}
    selecting = {n: _num_sorted(draw(st.lists(st.sampled_from(impl), max_size=3))) for n in tcs}
    tc_tasks = {}
    for n in tcs:
        if selecting[n]:
            tc_tasks[f"TC-{n}"] = selecting[n]  # P-1a (selected): only the selecting tasks
        elif covering[n] is not None:
            tc_tasks[f"TC-{n}"] = verifies[covering[n]]  # P-1a (fallback): the covering test task's Verifies
    done = frozenset(draw(st.sets(st.sampled_from(ids).map(str))))
    gated = frozenset(draw(st.sets(st.sampled_from(ids).map(str))))
    return dict(impl=impl, test_tasks=test_tasks, files=files, verifies=verifies, tcs=tcs, covering=covering,
                selecting=selecting, tc_tasks=tc_tasks, done=done, gated=gated)


def _failure(**kw):
    from claude_kiro.failures import Failure

    base = dict(node=None, file=None, kind="assertion", message="boom", truncated=False, command="pytest",
                exc_type="AssertionError", phase="call", crash_path=None, crash_line=None, detail="boom",
                missing_command=None)
    base.update(kw)
    return Failure(**base)


def _index(model):
    from claude_kiro.attribution import AttributionIndex

    return AttributionIndex(tc_tasks=dict(model["tc_tasks"]), file_owner=dict(model["files"]),
                            test_task_verifies=dict(model["verifies"]), command_owner={})


@st.composite
def failure_specs(draw, model):
    """Failure descriptors: (node, file) built from owned, unowned, wrapper and file-level forms."""
    owned = sorted(model["files"])
    kind = draw(st.sampled_from(["tc-owned", "tc-unowned", "plain-owned", "plain-unowned", "file-owned",
                                 "file-unowned", "wrapper"]))
    n = draw(st.one_of(st.sampled_from(model["tcs"]), st.integers(121, 140)))  # 121+: unknown TC
    fn = f"test_tc{n:03d}_x" if draw(st.booleans()) else f"test_tc{n}_x"
    owned_file = draw(st.sampled_from(owned))
    unowned = f"tests/test_unowned_{draw(st.integers(0, 3))}.py"
    if kind == "tc-owned":
        return (f"{owned_file}::{fn}", owned_file)
    if kind == "tc-unowned":
        return (f"{unowned}::{fn}", unowned)
    if kind == "plain-owned":
        return (f"{owned_file}::test_plain_{draw(st.integers(0, 3))}", owned_file)
    if kind == "plain-unowned":
        return (f"{unowned}::test_plain_{draw(st.integers(0, 3))}", unowned)
    if kind == "file-owned":
        return (None, owned_file)
    if kind == "file-unowned":
        return (None, unowned)
    return (f"tests/test_wrapper.py::test_wrap[{owned_file}]", "tests/test_wrapper.py")


def _key(failure):
    return failure.node if failure.node else f"collect:{failure.file}"


def _status(attr):
    if not attr.tasks:
        return "unattributed"
    return "regression-candidate" if attr.candidate else "excused"


def _oracle_attribution(model, node, file, done, gated):
    tokens = _oracle_tokens(node) if node else []
    tasks = ()
    if tokens and f"TC-{tokens[0]}" in model["tc_tasks"]:
        tasks = model["tc_tasks"][f"TC-{tokens[0]}"]
    elif file in model["files"]:
        tasks = model["verifies"][model["files"][file]]
    excused_by = _num_sorted(t for t in tasks if t not in (done | gated))
    return _num_sorted(tasks), excused_by


# --------------------------------------------------------------------------- P-1


@settings(**SETTINGS)
@given(data=st.data())
def test_tc173_p1_statuses_partition_and_excused_by(data):
    """P-1: tasks, candidate, excusedBy follow the oracle; the three statuses partition the failures."""
    from claude_kiro.attribution import attribute_failure

    model = data.draw(spec_models())
    index = _index(model)
    for node, file in data.draw(st.lists(failure_specs(model), min_size=1, max_size=8)):
        attr = attribute_failure(_failure(node=node, file=file), index, model["done"], model["gated"])
        tasks, excused_by = _oracle_attribution(model, node, file, model["done"], model["gated"])
        assert _num_sorted(attr.tasks) == tasks
        assert _num_sorted(attr.excused_by) == excused_by
        status = _status(attr)
        if not tasks:
            assert status == "unattributed" and attr.candidate is True and not attr.excused_by
        elif excused_by:
            assert status == "excused" and attr.candidate is False
        else:
            assert status == "regression-candidate" and attr.candidate is True


@settings(**SETTINGS)
@given(data=st.data())
def test_tc173_p1a_tc_selection_over_coverage(data):
    """P-1a: a TC some Verify selects gets exactly the selectors; otherwise the covering test task's Verifies."""
    from claude_kiro.attribution import attribute_failure

    model = data.draw(spec_models())
    index = _index(model)
    n = data.draw(st.sampled_from(model["tcs"]))
    owned_file = data.draw(st.sampled_from(sorted(model["files"])))
    attr = attribute_failure(_failure(node=f"{owned_file}::test_tc{n}_x", file=owned_file), index,
                             model["done"], model["gated"])
    if model["selecting"][n]:
        assert _num_sorted(attr.tasks) == model["selecting"][n]
        extra = set(model["verifies"].get(model["covering"][n], ())) - set(model["selecting"][n])
        assert not extra & set(attr.tasks)
    elif model["covering"][n] is not None:
        assert _num_sorted(attr.tasks) == model["verifies"][model["covering"][n]]
    else:  # unknown TC: file attribution, never an invented task
        assert _num_sorted(attr.tasks) == model["verifies"][model["files"][owned_file]]


@settings(**SETTINGS)
@given(data=st.data())
def test_tc173_p1b_hint_never_changes_attribution(data):
    """P-1b: a wrapper whose parameter ID names an owned file never gains tasks, status or excusedBy."""
    from claude_kiro.attribution import attribute_failure

    model = data.draw(spec_models())
    index = _index(model)
    owned_file = data.draw(st.sampled_from(sorted(model["files"])))
    with_hint = _failure(node=f"tests/test_wrapper.py::test_wrap[{owned_file}]", file="tests/test_wrapper.py")
    without = _failure(node="tests/test_wrapper.py::test_wrap[nothing]", file="tests/test_wrapper.py")
    a = attribute_failure(with_hint, index, model["done"], model["gated"])
    b = attribute_failure(without, index, model["done"], model["gated"])
    assert (a.tasks, a.candidate, a.excused_by, a.by) == (b.tasks, b.candidate, b.excused_by, b.by)
    assert a.tasks == () and a.excused_by == ()
    assert _status(a) == "unattributed"


# --------------------------------------------------------------------------- P-2


@settings(**SETTINGS)
@given(data=st.data())
def test_tc173_p2_done_monotonicity(data):
    """P-2: more Done tasks only move excused -> candidate; unattributed never changes."""
    from claude_kiro.attribution import attribute_failure

    model = data.draw(spec_models())
    index = _index(model)
    more = frozenset(data.draw(st.sets(st.sampled_from(model["impl"]))))
    bigger = model["done"] | more
    for node, file in data.draw(st.lists(failure_specs(model), min_size=1, max_size=8)):
        f = _failure(node=node, file=file)
        small = _status(attribute_failure(f, index, model["done"], model["gated"]))
        big = _status(attribute_failure(f, index, bigger, model["gated"]))
        if small == "unattributed" or big == "unattributed":
            assert small == big
        if small == "regression-candidate":
            assert big == "regression-candidate"
        if big == "excused":
            assert small == "excused"


# --------------------------------------------------------------------------- P-3

FUNC_PIECES = ["test_", "tc7_", "TC7_", "tc012_", "tc12_", "tc12x_", "atc1_", "xtc1_", "tc3_", "tc4_", "foo", "_bar",
               "Tc9_", "tc1_", "1tc5_"]


@st.composite
def node_ids(draw):
    func = "test_" + "".join(draw(st.lists(st.sampled_from(FUNC_PIECES), min_size=0, max_size=3))) + "x"
    param_tc = draw(st.sampled_from(["", "[tc7_]", "[a-tc2_]", "[case]"]))
    cls = draw(st.sampled_from(["", "TestTc5_::", "Test::"]))
    path = draw(st.sampled_from(["tests/test_a.py", "sub/dir/test_tc3_file.py", "tc4_dir/test_b.py"]))
    return f"{path}::{cls}{func}{param_tc}"


@settings(**SETTINGS)
@given(node=node_ids())
def test_tc173_p3_tc_tokens_function_name_only(node):
    """P-3: TC ids are the `tc<digits>_` tokens of the function name only, padded numbers normalised."""
    from claude_kiro.attribution import tc_of_node

    expected = _oracle_tokens(node)
    got = tc_of_node(node)
    if not expected:
        assert got is None
    else:
        assert got in {f"TC-{n}" for n in expected}


def _p3_spec(root, tcs, verifies):
    spec = root / "specs" / "demo"
    spec.mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "tests" / "test_cover.py").write_text("".join(f"def test_tc{n}_x():\n    pass\n\n" for n in tcs))
    (spec / "requirements.md").write_text("# Feature\n\n- 1.1 WHEN a THE SYSTEM SHALL b\n")
    rows = "".join(f"| TC-{n} | 1.1 | int | s | python | tests/test_cover.py |\n" for n in tcs)
    (spec / "test-plan.md").write_text(
        "# Test Plan\n\n| ID | Criteria | Level | Scenario | Real boundary | File |\n"
        "|----|----------|-------|----------|---------------|------|\n" + rows)
    (spec / "design.md").write_text("# Design\n\n## Components\n\n- `calc.py`: calc.\n")
    blocks = ["# Implementation Tasks: Demo\n\n**Status:** In Progress\n\n## Task Breakdown\n\n"
              "### Task 1: Tests\n**Status:** Done\n**Track:** test\n**Requirements:** 1.1\n**Files:**\n"
              "- `tests/test_cover.py` - tests\n**Verify:** `uv run pytest -q tests/test_cover.py`\n"
              + "**Verifies:** " + ", ".join(f"Task {t}" for t in verifies) + "\n**Dependencies:** None\n\n"
              "**Acceptance:**\n- [x] ok\n\n---\n\n"]
    for t, cmds in verifies.items():
        blocks.append(
            f"### Task {t}: Impl {t}\n**Status:** Not Started\n**Track:** impl\n**Requirements:** 1.1\n**Files:**\n"
            f"- `src{t}.py` - impl\n**Verify:** " + " ".join(f"`{c}`" for c in cmds)
            + "\n**Verified by:** Task 1\n**Dependencies:** Task 1\n\n**Acceptance:**\n- [ ] ok\n\n---\n\n")
    (spec / "tasks.md").write_text("".join(blocks))
    return spec


@settings(**SETTINGS)
@given(data=st.data())
def test_tc173_p3_verify_selection_matches_pytest_oracle(data):
    """P-3: the graph's `Verify -k` edges agree with pytest's `-k` evaluator for every selector form."""
    from claude_kiro.graph import build_graph

    tcs = sorted(data.draw(st.sets(st.integers(1, 9), min_size=3, max_size=6)))
    task_ids = [2, 3, 4]
    verifies = {t: data.draw(verify_commands(tcs)) for t in task_ids}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        spec = _p3_spec(root, tcs, verifies)
        graph = build_graph(root, spec).to_json()
        edges = {(e["src"], e["dst"]) for e in graph["edges"]
                 if e["kind"] == "verifies" and e.get("source") == "Verify -k"}
    for t, cmds in verifies.items():
        for n in tcs:
            assert ((f"tc:TC-{n}", f"task:{t}") in edges) == _selected_by(cmds, n), (t, n, cmds)


# --------------------------------------------------------------------------- failure lists (P-4, P-6)


@st.composite
def pair_lists(draw):
    model = draw(spec_models())
    index = _index(model)
    pool = draw(st.lists(failure_specs(model), min_size=1, max_size=8))
    picks = draw(st.lists(st.sampled_from(pool), min_size=1, max_size=12))  # duplicates allowed
    from claude_kiro.attribution import attribute_failure

    pairs = []
    for node, file in picks:
        f = _failure(node=node, file=file, kind="collection" if node is None else "assertion")
        pairs.append((f, attribute_failure(f, index, model["done"], model["gated"])))
    return pairs


def _keys(items):
    return [getattr(i, "key", i) for i in items]


# --------------------------------------------------------------------------- P-4


@settings(**SETTINGS)
@given(pairs=pair_lists(), data=st.data())
def test_tc173_p4_permutation_determinism(pairs, data):
    """P-4: two calls, and any permutation of the failure order, give identical output in the same order."""
    from claude_kiro.attribution import gate_verdict, rerun_plan

    shuffled = data.draw(st.permutations(pairs))
    reruns = {_key(f): data.draw(st.booleans()) for f, _ in pairs}
    assert rerun_plan(pairs) == rerun_plan(pairs)
    assert rerun_plan(pairs) == rerun_plan(list(shuffled))
    v1, v2 = gate_verdict(pairs, reruns, True), gate_verdict(list(shuffled), reruns, True)
    assert v1 == v2 and gate_verdict(pairs, reruns, True) == v1


# --------------------------------------------------------------------------- P-5a


CHARS = ["a", "b", " ", "\n", "\r", "\x00", "\x1b", "[", "3", "1", "m", "é", "世", "\U0001f600", "́",
         "‍", "️", "\U0001f1fa", "\U0001f1f8"]
LENGTHS = [0, 1, 2, 499, 500, 501, 5000]


@st.composite
def messages(draw):
    chunk = "".join(draw(st.lists(st.sampled_from(CHARS), min_size=1, max_size=40)))
    target = draw(st.one_of(st.sampled_from(LENGTHS), st.integers(0, 1200)))
    return (chunk * (target // len(chunk) + 1))[:target]


def _extends(ch):
    return unicodedata.category(ch) in ("Mn", "Me", "Mc") or ch in ("‍", "️")


@settings(**SETTINGS)
@given(text=messages())
def test_tc173_p5a_truncation_bound_prefix_flag(text):
    """P-5a: <= 500 chars, flag iff over 500, unchanged when not truncated, never empty, no split grapheme."""
    from claude_kiro.failures import truncate_message

    out, truncated = truncate_message(text)
    assert len(out) <= 500
    assert truncated == (len(text) > 500)
    if not truncated:
        assert out == text
        return
    assert out
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in out)
    common = len(list(itertools.takewhile(lambda p: p[0] == p[1], zip(out, text))))
    assert common >= len(out) - 3  # at most a short marker after the kept prefix
    assert common > 0
    if common < len(text):
        assert not _extends(text[common]), "cut before a combining mark or joiner"
        assert text[common - 1] != "‍", "cut after a zero-width joiner"
        assert not (text[common - 1] == "\r" and text[common] == "\n"), "cut inside CRLF"


EXC_TYPES = ["AssertionError", "MyAssertionError", "AssertionErrorX", "ImportError", "ModuleNotFoundError",
             "ValueError", "KeyError", "RuntimeError", "TimeoutError", "Weird", "", "SyntaxError"]


@settings(**SETTINGS)
@given(exc=st.sampled_from(EXC_TYPES), phase=st.sampled_from(["collect", "setup", "call", "teardown", "", "other"]),
       message=st.text(max_size=40), timed_out=st.booleans())
def test_tc173_p5a_kind_is_one_of_five(exc, phase, message, timed_out):
    """P-5a (3.2): always one of the five kinds; near-miss class names and unknowns are `error`."""
    from claude_kiro.failures import classify_kind

    kind = classify_kind(exc, phase, message, timed_out)
    assert kind in KINDS
    if timed_out:
        assert kind == "timeout"
    elif phase == "collect":
        assert kind == ("import" if exc in ("ImportError", "ModuleNotFoundError") else "collection")
    elif exc == "AssertionError":
        assert kind == "assertion"
    elif exc in ("MyAssertionError", "AssertionErrorX", "Weird", "", "ValueError", "KeyError"):
        assert kind == "error"


# --------------------------------------------------------------------------- P-5b

EXC_CLASSES = [("ValueError", ""), ("KeyError", ""), ("AssertionError", ""), ("RuntimeError", ""),
               ("MyAssertionError", "class MyAssertionError(AssertionError):\n    pass\n"),
               ("MyAssertionError2", "class MyAssertionError2(Exception):\n    pass\n"),
               ("AssertionErrorX", "class AssertionErrorX(Exception):\n    pass\n"),
               ("OSError", "")]


@st.composite
def cli_scenarios(draw):
    if draw(st.booleans()):  # collect scenario: every extra file fails at collection (pytest stops there)
        files = []
        for i in range(draw(st.integers(1, 3))):
            files.append((i, draw(st.sampled_from(["import", "raise", "syntax"]))))
        return ("collect", files)
    tests = [(draw(st.sampled_from(EXC_CLASSES)), draw(st.sampled_from(["call", "setup", "teardown"])))
             for _ in range(draw(st.integers(1, 6)))]
    return ("run", tests)


def _write_gen_files(root, scenario):
    mode, items = scenario
    tests = root / "tests"
    if mode == "collect":
        for i, how in items:
            body = {"import": "import module_that_does_not_exist_anywhere\n",
                    "raise": "raise RuntimeError('collect boom')\n",
                    "syntax": "def broken(:\n    pass\n"}[how]
            (tests / f"test_gen_{i}.py").write_text(body + "\n\ndef test_never():\n    pass\n")
        return len(items)
    src = "import pytest\n\n"
    for i, ((name, defn), phase) in enumerate(items):
        src += defn + "\n"
        if phase == "call":
            src += f"def test_gen_{i}():\n    raise {name}('boom {i}')\n\n"
        elif phase == "setup":
            src += (f"@pytest.fixture\ndef fx_{i}():\n    raise {name}('setup {i}')\n\n"
                    f"def test_gen_{i}(fx_{i}):\n    pass\n\n")
        else:
            src += (f"@pytest.fixture\ndef fx_{i}():\n    yield\n    raise {name}('teardown {i}')\n\n"
                    f"def test_gen_{i}(fx_{i}):\n    pass\n\n")
    (tests / "test_gen.py").write_text(src)
    return len(items)


@settings(**CLI_SETTINGS)
@given(scenario=cli_scenarios())
def test_tc173_p5b_real_cli_kinds_and_counts(scenario):
    """P-5b: through the real `ck gate --attribute --json`, every kind is one of the five and none is dropped."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        done = {k: "Done" for k in ("1", "2", "3", "6", "7", "8")}
        root = attr_project(tmp_path, statuses=done,
                            tests={"tc1_a": "pass", "tc3_a": "pass", "tc5_a": "pass"})
        expected = _write_gen_files(root, scenario)
        commit_all(root, "generated failing tests")
        code, obj, err = ck_json(root, "gate", "demo", "--attribute", "--json")
    assert obj is not None, err
    failures = [f for f in obj["failures"]]
    assert code != 0
    assert all(f["kind"] in KINDS for f in failures)
    assert len(failures) == expected, failures


# --------------------------------------------------------------------------- P-6


@settings(**SETTINGS)
@given(pairs=pair_lists(), data=st.data())
def test_tc173_p6_rerun_targets_and_verdict(pairs, data):
    """P-6: targets are exactly the candidates (each once, excused never); red iff regression or unattributed."""
    from claude_kiro.attribution import gate_verdict, rerun_plan

    keys = {_key(f) for f, _ in pairs}
    candidates = {_key(f) for f, a in pairs if a.candidate}
    targets = _keys(rerun_plan(pairs))
    assert sorted(targets) == sorted(candidates)  # each once, none excused
    assert not (set(targets) & {_key(f) for f, a in pairs if not a.candidate})
    passed = {k: data.draw(st.booleans()) for k in keys}
    other_ok = data.draw(st.booleans())
    verdict = gate_verdict(pairs, passed, other_ok)
    expect = {}
    for f, a in pairs:
        k = _key(f)
        if not a.candidate:
            expect[k] = "excused"
        elif passed[k]:
            expect[k] = "flaky"
        else:
            expect[k] = "unattributed" if not a.tasks else "regression"
    assert dict(verdict.statuses) == expect
    bad = any(s in ("regression", "unattributed") for s in expect.values())
    assert verdict.red == (bad or not other_ok)
