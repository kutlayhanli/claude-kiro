"""Properties of the test-track classifiers (unattended-correctness TC-175: P-12, P-14, P-15, P-16).

P-12 runs the pure seam `claude_kiro.failures.classify_not_built(failure, ctx)` over generated failures.
P-14 to P-16 run the fidelity seams (`extract_test_values`, `parse_row`, `parse_models`, `check_values` of
`claude_kiro.fidelity`) over generated test sources, test-plan rows and design model blocks. Nothing here starts
a server. Project modules are imported inside the test bodies so the file collects before the code exists.
"""

import copy
import os
import re
import tempfile
from datetime import date, timedelta
from decimal import Decimal

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

PROPERTIES = {
    "P-12": "test_tc175_p12_classify_not_built_right_reason",
    "P-14": "test_tc175_p14_fidelity_equal_values_match_different_mismatch",
    "P-15": "test_tc175_p15_fidelity_partition",
    "P-16": "test_tc175_p16_fidelity_inputs_unchanged",
}

SETTINGS = dict(max_examples=200, derandomize=True, database=None, deadline=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture,
                                       HealthCheck.data_too_large, HealthCheck.filter_too_much])

# =========================================================================== P-12

TEST_FILE = "tests/test_calc.py"
IDENT = st.sampled_from(["x", "sub", "mul", "ratio", "total", "parse", "Widget", "helper_fn"])

HEADER = (
    "import json\n"
    "from pathlib import Path\n"
    "import calc\n"
    "from calc import add, CONST\n"
    "\n"
)


def _ctx():
    from claude_kiro.failures import ProjectContext
    return ProjectContext


def make_ctx(source=""):
    return _ctx()(
        project_modules=frozenset({"calc"}),
        owned_paths=frozenset({"src/calc/sub.py", "src/calc/cli.py"}),
        programs=frozenset({"calc"}),
        test_paths=frozenset({TEST_FILE, "tests/fixtures/data.json", "tests/helpers.py"}),
        test_sources={TEST_FILE: source},
    )


def make_failure(exc_type, message, detail=None, kind="error", phase="call", crash_path=TEST_FILE,
                 crash_line=None, node=f"{TEST_FILE}::test_tc3_case"):
    from claude_kiro.failures import Failure
    return Failure(node=node, file=TEST_FILE, kind=kind, message=message, truncated=False,
                   command="uv run pytest -q", exc_type=exc_type, phase=phase, crash_path=crash_path,
                   crash_line=crash_line, detail=detail if detail is not None else message, missing_command=None)


def assert_source(body, params=""):
    """A test file whose failing assert is the last `assert` line of `body`; returns (source, line)."""
    source = HEADER + f"def test_tc3_case({params}):\n{body}\n"
    lines = source.splitlines()
    line = max(i for i, text in enumerate(lines, 1) if text.strip().startswith("assert"))
    return source, line


# (template, params, kind of V placeholder fills). V is the operand the dependency form substitutes.
ASSERT_TEMPLATES = [
    ('    assert {V} == 4', ""),
    ('    data = json.loads(Path("tests/fixtures/data.json").read_text())\n'
     '    got = data["a"] + {V}\n    assert got == 4', ""),
    ('    for row in json.load(open("tests/fixtures/data.json")):\n        assert row["v"] > {V}', ""),
    ('    with open("tests/fixtures/data.json") as fh:\n        rows = json.load(fh)\n'
     '    total = len(rows) + {V}\n    assert total == 3', ""),
]
# dependency form -> (V text, extra params, class)
DEPENDENCIES = {
    "literal": ("3", "", "bad"),
    "project_import": ("CONST", "", "unclear"),
    "project_call": ("add(1, 2)", "", "unclear"),
    "project_module_attr": ("calc.VERSION", "", "unclear"),
    "fixture_arg": ("client.value", "client", "unclear"),
}

ASSERT_ONLY = [  # a stand-alone literal assert and a file-loaded one, both bad
    ('    assert 1 + 1 == 3', ""),
    ('    expected = [1, 2, 3]\n    assert 4 in expected', ""),
    ('    text = Path("tests/fixtures/data.json").read_text()\n    assert "ok" in text', ""),
]


def not_built_forms(name):
    nm = f"'{name}'"
    click_usage = "Usage: calc [OPTIONS] COMMAND [ARGS]...\nTry 'calc --help' for help.\n\n"
    argparse_usage = "usage: calc [-h] {run,list} ...\n"
    return [
        make_failure("ModuleNotFoundError", f"ModuleNotFoundError: No module named 'calc.{name}'", kind="import",
                     phase="collect", crash_path=TEST_FILE),
        make_failure("ImportError", f"ImportError: cannot import name {nm} from 'calc' (/w/src/calc/__init__.py)",
                     kind="import", phase="collect"),
        make_failure("AttributeError", f"AttributeError: module 'calc' has no attribute {nm}"),
        make_failure("AssertionError", "assert 2 == 0",
                     detail=f"assert 2 == 0\n  + where 2 = <Result SystemExit(2)>.exit_code\n{click_usage}"
                            f"Error: No such command {nm}.", kind="assertion"),
        make_failure("AssertionError", "assert 2 == 0",
                     detail=f"assert 2 == 0\n{click_usage}Error: No such command “{name}”.",
                     kind="assertion"),
        make_failure("SystemExit", "2",
                     detail=f"{argparse_usage}calc: error: argument command: invalid choice: {nm} "
                            f"(choose from 'run', 'list')"),
        make_failure("SystemExit", "2", detail=f"{argparse_usage}calc: error: unrecognized arguments: --{name}"),
        make_failure("AssertionError", "assert 2 == 0",
                     detail=f"assert 2 == 0\n{click_usage}Error: No such option: --{name}", kind="assertion"),
        make_failure("NotImplementedError", "NotImplementedError", crash_path="src/calc/ops.py"),
    ]


def wrong_reason_failures(name):
    data_assert, line1 = assert_source('    data = json.loads(Path("tests/fixtures/data.json").read_text())\n'
                                       '    assert data["a"] == 3')
    out = [
        ("AssertionError from a file-loaded assert", data_assert,
         make_failure("AssertionError", "assert 1 == 3", kind="assertion", crash_line=line1)),
        ("ValueError in fixture setup", "",
         make_failure("ValueError", f"ValueError: bad fixture {name}", phase="setup", crash_path="tests/conftest.py")),
        ("ValidationError in fixture setup", "",
         make_failure("ValidationError", f"ValidationError: 1 validation error for Row {name}", phase="setup",
                      crash_path="tests/conftest.py")),
        ("ModuleNotFoundError for a module of the task's own Files", "",
         make_failure("ModuleNotFoundError", "ModuleNotFoundError: No module named 'tests.helpers'", kind="import",
                      phase="collect")),
    ]
    for text, params in ASSERT_ONLY:
        src, line = assert_source(text, params)
        out.append((f"literal assert {text!r}", src,
                    make_failure("AssertionError", "assert False", kind="assertion", crash_line=line)))
    return out


def unclear_failures(name):
    out = []
    for text in ("from numpy import array", "import numpy as np"):
        out.append(("", make_failure("ModuleNotFoundError", "ModuleNotFoundError: No module named 'numpy'",
                                     kind="import", phase="collect")))
    out.append(("", make_failure("KeyError", f"KeyError: '{name}'")))
    out.append(("", make_failure("TypeError", "TypeError: unsupported operand type(s) for +: 'int' and 'str'")))
    out.append(("", make_failure("NotImplementedError", "NotImplementedError", crash_path=TEST_FILE)))
    # an assert line that cannot be parsed: the source is broken, or the line is not in the file
    out.append(("def test_tc3_case(:\n    assert (\n", make_failure("AssertionError", "assert", kind="assertion",
                                                                       crash_line=2)))
    src, _ = assert_source("    assert 1 == 2")
    out.append((src, make_failure("AssertionError", "assert", kind="assertion", crash_line=9999)))
    out.append((src, make_failure("AssertionError", "assert", kind="assertion", crash_line=None)))
    return out


def apply_noise(failure, flags):
    """Metamorphic transform: ANSI colour, CRLF, indented traceback, surrounding pytest noise."""
    from dataclasses import replace
    detail, message = failure.detail, failure.message
    if "indent" in flags:
        detail = "\n".join("    E   " + ln for ln in detail.split("\n"))
    if "ansi" in flags:
        detail = "\n".join(f"\x1b[1m\x1b[31m{ln}\x1b[0m" for ln in detail.split("\n"))
        message = f"\x1b[31m{message}\x1b[0m"
    if "noise" in flags:
        detail = ("============ FAILURES ============\n___________ test_tc3_case ___________\n"
                  "tests/test_calc.py:7: in test_tc3_case\n" + detail +
                  "\n=========== short test summary info ===========\nFAILED tests/test_calc.py::test_tc3_case\n"
                  "1 failed, 2 passed in 0.03s")
    if "crlf" in flags:
        detail = detail.replace("\n", "\r\n")
    return replace(failure, detail=detail, message=message)


NOISE = st.lists(st.sampled_from(["ansi", "crlf", "indent", "noise"]), unique=True, max_size=4)


@settings(**SETTINGS)
@given(name=IDENT, idx=st.integers(0, 10_000), flags=NOISE)
def test_tc175_p12_classify_not_built_right_reason(name, idx, flags):
    """P-12: not-built forms, wrong-reason forms and the rest, unchanged by ANSI, CRLF, indentation and noise."""
    from claude_kiro.failures import classify_not_built

    # not-built-yet forms
    forms = not_built_forms(name)
    f = forms[idx % len(forms)]
    ctx = make_ctx()
    assert classify_not_built(f, ctx) == "not-built", f
    assert classify_not_built(apply_noise(f, flags), ctx) == "not-built", (f, flags)

    # wrong-reason forms
    cases = wrong_reason_failures(name)
    label, src, f = cases[idx % len(cases)]
    ctx = make_ctx(src)
    assert classify_not_built(f, ctx) == "bad", (label, f)
    assert classify_not_built(apply_noise(f, flags), ctx) == "bad", (label, f, flags)

    # unclear forms: never bad, and unclear except NotImplementedError raised from test code (never bad)
    cases = unclear_failures(name)
    src, f = cases[idx % len(cases)]
    ctx = make_ctx(src)
    got = classify_not_built(f, ctx)
    assert got != "bad", f
    if f.exc_type != "NotImplementedError":
        assert got == "unclear", f
    assert classify_not_built(apply_noise(f, flags), ctx) == got, (f, flags)


@settings(**SETTINGS)
@given(template=st.sampled_from(ASSERT_TEMPLATES), dep=st.sampled_from(sorted(DEPENDENCIES)), flags=NOISE)
def test_tc175_p12_assert_reaches_project(template, dep, flags):
    """P-12: an assert that runs no project code and uses no project name is bad, adding either makes it unclear."""
    from claude_kiro.failures import classify_not_built

    text, _ = template
    value, params, expected = DEPENDENCIES[dep]
    src, line = assert_source(text.format(V=value), params)
    f = make_failure("AssertionError", "assert 3 == 4", kind="assertion", crash_line=line)
    ctx = make_ctx(src)
    assert classify_not_built(f, ctx) == expected, (dep, src)
    assert classify_not_built(apply_noise(f, flags), ctx) == expected, (dep, src, flags)
    if dep != "literal":
        lit_src, lit_line = assert_source(text.format(V="3"))
        lit = make_failure("AssertionError", "assert 3 == 4", kind="assertion", crash_line=lit_line)
        assert classify_not_built(lit, make_ctx(lit_src)) == "bad"
        assert classify_not_built(f, ctx) == "unclear"


UNRELATED_EXC = st.sampled_from(["RuntimeError", "KeyError", "TypeError", "ZeroDivisionError", "OSError",
                                 "RecursionError", "IndexError"])
PLAIN_TEXT = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789 .,", min_size=0, max_size=60)


@settings(**SETTINGS)
@given(exc=UNRELATED_EXC, text=PLAIN_TEXT, crash=st.sampled_from([TEST_FILE, "src/calc/ops.py", None]),
       flags=NOISE)
def test_tc175_p12_everything_else_never_bad(exc, text, crash, flags):
    """P-12: any other body failure is unclear or not-built, never bad (only a wrong reason blocks)."""
    from claude_kiro.failures import classify_not_built

    f = make_failure(exc, f"{exc}: {text}", crash_path=crash)
    ctx = make_ctx("")
    assert classify_not_built(f, ctx) in ("unclear", "not-built"), f
    assert classify_not_built(apply_noise(f, flags), ctx) in ("unclear", "not-built"), (f, flags)


# =========================================================================== P-14 to P-16

PLAN_HEAD = ("# Test plan\n\n| ID | Criterion | Level | Scenario | Real boundary | Test file |\n"
             "|---|---|---|---|---|---|\n")
DESIGN = """# Design

## Data Models

```python
class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"

class Status(Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"

@dataclass
class Order:
    side: Side
    status: Status
    due_date: date
    min_price: Decimal
```
"""
SRC_HEAD = "from datetime import date\nfrom decimal import Decimal\nfrom shop.models import Order, Side, Status\n\n"
TEST_PATH = "tests/test_orders.py"


def cents(c):
    return f"{c // 100}.{c % 100:02d}"


def plan_row(tc, scenario):
    return f"| TC-{tc} | 8.1 | unit | {scenario} | the order check | `tests/test_orders.py` |\n"


def fn_src(tc, slug, body):
    return f"def test_tc{tc}_{slug}():\n{body}\n\n"


def plan_of(*rows):
    return PLAN_HEAD + "".join(rows)


def run_check(source, plan, tc, design=DESIGN):
    from claude_kiro.fidelity import check_values, extract_test_values, parse_models, parse_row
    values = extract_test_values(source, TEST_PATH)
    return check_values(values, parse_row(plan, f"TC-{tc}"), parse_models(design))


def mentions(entries, name):
    out = []
    for e in entries:
        text = e["test"] if isinstance(e, dict) else getattr(e, "test", e)
        if name in str(text):
            out.append(e)
    return out


def verdict(result, name):
    """Where the result puts test `name`: 'mismatch', 'unparsed', 'unmatched' or 'matched' (checked, no problem)."""
    where = []
    if mentions(result.mismatches, name):
        where.append("mismatch")
    if mentions(result.unparsed, name):
        where.append("unparsed")
    if mentions(result.unmatched, name):
        where.append("unmatched")
    if not where and mentions(result.checked, name):
        where.append("matched")
    return where


# Each case: (row sentence, test body, expected 'matched'|'mismatch'), built by a strategy from one drawn value.
CENTS = st.integers(100, 99_999)
QTY = st.integers(10, 900)
SINGLE_FORMS = ["{v}", "${v}", "{v} USD", "USD {v}", "{v}/share", "{v}0"]
TEST_NUM_FORMS = ["{v}", "{v}0", "Decimal(\"{v}\")", "{v}"]
ROW_SUFFIX = "for an order of {q} shares"


def cmp_body(literal_expr):
    return f"    quote = Order(side=Side.BUY, status=Status.OPEN)\n    assert quote.min_price >= {literal_expr}"


@st.composite
def number_cases(draw):
    c = draw(CENTS)
    q = draw(QTY)
    assume(q * 100 != c)
    form = draw(st.sampled_from(SINGLE_FORMS))
    row_text = form.format(v=cents(c)) if form != "{v}0" else cents(c) + "0"
    equal_expr = draw(st.sampled_from(TEST_NUM_FORMS)).format(v=cents(c))
    scenario = f"The minimum price is {row_text} {ROW_SUFFIX.format(q=q)}."
    diff = draw(st.sampled_from(["cent_up", "cent_down", "quantity"]))
    if diff == "cent_up":
        other = cents(c + 1)
    elif diff == "cent_down":
        other = cents(c - 1)
    else:
        other = str(q)
    return scenario, equal_expr, other, diff


@st.composite
def thousands_cases(draw):
    n = draw(st.integers(1000, 99_999))
    scenario = f"The minimum price is {n:,} USD."
    equal_expr = draw(st.sampled_from([str(n), f"{n}.0", f"{n:_}"]))
    return scenario, equal_expr, f"{n}.01"


@st.composite
def range_cases(draw):
    lo = draw(st.integers(100, 50_000))
    hi = lo + draw(st.integers(1, 5_000))
    a, b = cents(lo), cents(hi)
    form = draw(st.sampled_from([f"{a} to {b}", f"{a}-{b}", f"between {a} and {b}"]))
    scenario = f"The accepted price is {form}."
    return scenario, lo, hi


@st.composite
def percent_cases(draw):
    p = draw(st.integers(1, 98))
    scenario = f"The fee threshold is {p}%."
    equal_expr = draw(st.sampled_from([f"{p / 100:.2f}", f"{p}", f"{p}.0"]))
    return scenario, p, equal_expr


def body_of(expr):
    return f"    quote = Order(side=Side.BUY, status=Status.OPEN)\n    assert quote.fee_threshold >= {expr}"


DATE_ROW_FORMS = [lambda d: d.isoformat(), lambda d: f"{d.strftime('%b')} {d.day}, {d.year}",
                  lambda d: f"{d.day} {d.strftime('%B')} {d.year}"]
DATE_TEST_FORMS = [lambda d: f'"{d.isoformat()}"', lambda d: f"date({d.year}, {d.month}, {d.day})",
                   lambda d: f'"{d.isoformat()}T00:00:00Z"']


@st.composite
def date_cases(draw):
    d = date(2026, 1, 1) + timedelta(days=draw(st.integers(0, 700)))
    row_form = draw(st.sampled_from(DATE_ROW_FORMS))(d)
    test_form = draw(st.sampled_from(DATE_TEST_FORMS))
    return f"The order is due {row_form}.", d, test_form


ENUM_MEMBERS = ["BUY", "SELL"]
SIBLINGS = ["OPEN", "CLOSED"]


@st.composite
def enum_cases(draw):
    member = draw(st.sampled_from(ENUM_MEMBERS))
    form = draw(st.sampled_from([f"Side.{member}", f'"{member}"', f'"{member.lower()}"', f'"{member.title()}"']))
    sib = draw(st.sampled_from(SIBLINGS))
    bad = draw(st.sampled_from([f"Status.{sib}", f'"{sib}"', f"Status.{sib}", f'"{sib.lower()}"']))
    return f"The order side is {member}.", form, bad


def enum_body(expr):
    return f"    order = Order(side={expr}, status=Status.OPEN)\n    assert order.ok"


def date_body(expr):
    return f"    order = Order(side=Side.BUY, due_date={expr})\n    assert order.ok"


def check_one(scenario, body, tc=7):
    plan = plan_of(plan_row(tc, scenario))
    source = SRC_HEAD + fn_src(tc, "case", body)
    return run_check(source, plan, tc), source, plan


def assert_matches(result):
    assert result.mismatches == (), result
    assert result.unparsed == (), result
    assert result.unmatched == (), result
    assert len(result.checked) >= 1, result


def assert_mismatches(result):
    assert len(result.mismatches) >= 1, result
    assert result.unmatched == (), result


@settings(**SETTINGS)
@given(case=number_cases())
def test_tc175_p14_numbers(case):
    scenario, equal_expr, other, diff = case
    assert_matches(check_one(scenario, cmp_body(equal_expr))[0])
    assert_mismatches(check_one(scenario, cmp_body(other))[0])


@settings(**SETTINGS)
@given(case=thousands_cases())
def test_tc175_p14_thousands(case):
    scenario, equal_expr, off = case
    assert_matches(check_one(scenario, cmp_body(equal_expr))[0])
    assert_mismatches(check_one(scenario, cmp_body(off))[0])


@settings(**SETTINGS)
@given(case=range_cases())
def test_tc175_p14_ranges(case):
    scenario, lo, hi = case

    def body(a, b):
        return f"    quote = Order(side=Side.BUY, status=Status.OPEN)\n    assert {cents(a)} <= quote.price <= {cents(b)}"

    assert_matches(check_one(scenario, body(lo, hi))[0])
    assert_mismatches(check_one(scenario, body(lo, hi + 1))[0])   # the upper end is one cent off
    assert_mismatches(check_one(scenario, body(lo - 1, hi))[0])   # the lower end is one cent off
    assert_mismatches(check_one(scenario, body(hi, hi + 5))[0])   # the other end of the range, used as the lower


@settings(**SETTINGS)
@given(case=percent_cases())
def test_tc175_p14_percentages(case):
    scenario, p, equal_expr = case
    assert_matches(check_one(scenario, body_of(equal_expr))[0])
    assert_mismatches(check_one(scenario, body_of(f"{(p + 1) / 100:.2f}"))[0])
    assert_mismatches(check_one(scenario, body_of(str(p + 1)))[0])


@settings(**SETTINGS)
@given(case=date_cases())
def test_tc175_p14_dates(case):
    scenario, d, test_form = case
    assert_matches(check_one(scenario, date_body(test_form(d)))[0])
    for other in (d + timedelta(days=1), d - timedelta(days=1)):
        assert_mismatches(check_one(scenario, date_body(test_form(other)))[0])


@settings(**SETTINGS)
@given(case=enum_cases())
def test_tc175_p14_enums(case):
    scenario, form, bad = case
    assert_matches(check_one(scenario, enum_body(form))[0])
    assert_mismatches(check_one(scenario, enum_body(bad))[0])


@settings(**SETTINGS)
@given(case=number_cases(), seed=st.randoms(use_true_random=False))
def test_tc175_p14_fidelity_equal_values_match_different_mismatch(case, seed):
    """P-14: equal value matches, different value mismatches, and test order in the file does not matter."""
    scenario, equal_expr, other, _ = case
    plan = plan_of(plan_row(1, scenario), plan_row(2, scenario), plan_row(3, scenario))
    fns = [fn_src(1, "good", cmp_body(equal_expr)), fn_src(2, "bad", cmp_body(other)),
           fn_src(3, "good_again", cmp_body(equal_expr))]
    outcomes = []
    for order in ([0, 1, 2], [2, 1, 0], seed.sample([0, 1, 2], 3)):
        source = SRC_HEAD + "".join(fns[i] for i in order)
        from claude_kiro.fidelity import check_values, extract_test_values, parse_models, parse_row
        values = extract_test_values(source, TEST_PATH)
        merged_mismatch, merged_checked = set(), set()
        for tc in (1, 2, 3):
            mine = [v for v in values if f"test_tc{tc}_" in v.test]
            res = check_values(mine, parse_row(plan, f"TC-{tc}"), parse_models(DESIGN))
            merged_mismatch |= {(f"tc{tc}", str(m["value"])) for m in res.mismatches}
            merged_checked |= {(f"tc{tc}", str(v.value)) for v in res.checked}
            assert res.unmatched == ()
        assert {t for t, _ in merged_mismatch} == {"tc2"}, merged_mismatch
        assert {t for t, _ in merged_checked} >= {"tc1", "tc3"}
        outcomes.append((merged_mismatch, merged_checked))
    assert outcomes[0] == outcomes[1] == outcomes[2]


# ---------------------------------------------------------------- P-15

TIERS_ROW = "The price tiers are 4.15, 5.25 and 6.50."
KINDS = ["match", "mismatch", "tiers", "runtime_mul", "runtime_call", "no_tc", "no_row"]


@st.composite
def partition_cases(draw):
    kinds = draw(st.lists(st.sampled_from(KINDS), min_size=1, max_size=8))
    c = draw(CENTS)
    return kinds, c


def build_partition(kinds, c):
    """Return (source, plan, tests) where tests is a list of (name, tc or None, expected category)."""
    rows, fns, tests = [], [], []
    for i, kind in enumerate(kinds):
        tc = 100 + i
        slug = f"k{i}"
        v = cents(c)
        if kind == "no_tc":
            name = f"test_plain_{slug}"
            fns.append(f"def {name}():\n{cmp_body(v)}\n\n")
            tests.append((name, None, "unmatched"))
            continue
        name = f"test_tc{tc}_{slug}"
        if kind == "no_row":
            fns.append(f"def {name}():\n{cmp_body(v)}\n\n")
            tests.append((name, tc, "unmatched"))
            continue
        if kind == "match":
            rows.append(plan_row(tc, f"The minimum price is {v}."))
            body, exp = cmp_body(v), "matched"
        elif kind == "mismatch":
            rows.append(plan_row(tc, f"The minimum price is {v}."))
            body, exp = cmp_body(cents(c + 1)), "mismatch"
        elif kind == "tiers":
            rows.append(plan_row(tc, TIERS_ROW))
            body, exp = cmp_body("4.15"), "unparsed"
        elif kind == "runtime_mul":
            rows.append(plan_row(tc, f"The minimum price is {v}."))
            body, exp = cmp_body("base * 1.1"), "unparsed"
        else:
            rows.append(plan_row(tc, f"The minimum price is {v}."))
            body, exp = cmp_body("f(y)"), "unparsed"
        fns.append(f"def {name}():\n{body}\n\n")
        tests.append((name, tc, exp))
    return SRC_HEAD + "".join(fns), plan_of(*rows), tests


def check_partition(source, plan, tests):
    from claude_kiro.fidelity import check_values, extract_test_values, parse_models, parse_row
    values = extract_test_values(source, TEST_PATH)
    models = parse_models(DESIGN)
    results = {}
    for name, tc, _ in tests:
        mine = [v for v in values if name in v.test]
        row = parse_row(plan, f"TC-{tc}") if tc is not None else None
        results[name] = check_values(mine, row, models)
    return results


@settings(**SETTINGS)
@given(case=partition_cases())
def test_tc175_p15_fidelity_partition(case):
    """P-15: each test lands in exactly one of matched, mismatches, unparsed, unmatched; the reviewer gets the last two."""
    kinds, c = case
    source, plan, tests = build_partition(kinds, c)
    results = check_partition(source, plan, tests)
    reviewer, everything_ok = set(), True
    for name, _, expected in tests:
        res = results[name]
        where = verdict(res, name)
        assert where == [expected], (name, expected, where, res)
        if where[0] in ("unparsed", "unmatched"):
            reviewer.add(name)
        if where[0] in ("matched",):
            assert not mentions(res.unparsed, name) and not mentions(res.unmatched, name)
        else:
            everything_ok = everything_ok and where[0] != "mismatch"
    expected_reviewer = {n for n, _, e in tests if e in ("unparsed", "unmatched")}
    assert reviewer == expected_reviewer
    reviewer_items = {str(u["test"]) if isinstance(u, dict) else str(u)
                      for r in results.values() for u in (*r.unparsed, *r.unmatched)}
    assert all(any(n in item for n in expected_reviewer) for item in reviewer_items)
    assert len(reviewer_items) == len(expected_reviewer)
    if all(e == "matched" for _, _, e in tests):
        assert all(r.unparsed == () and r.unmatched == () and r.mismatches == () for r in results.values())


# ---------------------------------------------------------------- P-16


@settings(**SETTINGS)
@given(case=number_cases(), date_case=date_cases(), enum_case=enum_cases(), fed=st.booleans())
def test_tc175_p16_fidelity_inputs_unchanged(case, date_case, enum_case, fed):
    """P-16: the seam takes strings and returns a value, leaves its inputs unchanged and writes no file."""
    from claude_kiro.fidelity import check_values, extract_test_values, parse_models, parse_row

    scenario, equal_expr, other, _ = case
    _, d, dform = date_case
    _, eform, ebad = enum_case
    body = cmp_body(other if fed else equal_expr)
    source = (SRC_HEAD + fn_src(7, "price", body) + fn_src(8, "date", date_body(dform(d)))
              + fn_src(9, "side", enum_body(ebad if fed else eform)))
    plan = plan_of(plan_row(7, scenario), plan_row(8, f"The order is due {d.isoformat()}."),
                   plan_row(9, "The order side is BUY."))
    design = DESIGN
    copies = (source[:], plan[:], design[:])
    ids = (id(source), id(plan), id(design))
    with tempfile.TemporaryDirectory() as tmp:
        before = os.getcwd()
        os.chdir(tmp)
        try:
            values = extract_test_values(source, TEST_PATH)
            row = parse_row(plan, "TC-7")
            models = parse_models(design)
            values_before, row_before, models_before = list(values), copy.deepcopy(row), copy.deepcopy(models)
            result = check_values(values, row, models)
            assert os.listdir(tmp) == []
        finally:
            os.chdir(before)
    assert (source, plan, design) == copies
    assert (id(source), id(plan), id(design)) == ids
    assert values == values_before and row == row_before and models == models_before
    assert result is not None
    assert not isinstance(result, str)
    # the same call twice gives an equal value (a pure function of its inputs)
    assert check_values(values, row, models) == result
