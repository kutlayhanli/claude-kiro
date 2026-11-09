"""Red-team of Story 5's blocking boundary (unattended-correctness 5.3, 5.4, 5.7; TC-52, TC-181).

Written from the requirements only. A wrong-reason failure must block (5.3, 5.7): an `AssertionError` whose
failing assert compares only literals or file-loaded data, a setup error, a `NameError`. An unclear one must never
block (5.4): an assert that reaches project code, a project name, or a pytest fixture argument, or a line that
cannot be parsed back. Each case below is a disguise of one side or the other: aliases, comprehensions, `with`
blocks, same-file helpers, test-helper modules, quoted CLI messages, ANSI, CRLF and reformatted tracebacks.

Seam cases (`test_tc52_redteam_seam_<slug>`) call `claude_kiro.failures.classify_not_built` and
`assert_reaches_project` with real `Failure` and `ProjectContext` values. CLI cases (`test_tc52_redteam_<slug>`)
run `ck gate demo --task 1 --json` in a real task worktree of the `attr_project` repo against real pytest. Project
modules are imported inside test bodies so this file collects before the code exists. Nothing is mocked.
"""

import json
from dataclasses import replace
from pathlib import Path

import unattended_helpers as h

TEST_FILE = "tests/test_calc.py"
CALC_TEST_FILE = "tests/test_calc_tc.py"

# =========================================================================== seam helpers

HEADER = (
    "import json\n"
    "from pathlib import Path\n"
    "import calc\n"
    "from calc import add, CONST\n"
    "from calc import add as plus\n"
    "\n"
)

DATA = 'json.loads(Path("tests/fixtures/data.json").read_text())'


def make_ctx(source="", sources=None):
    from claude_kiro.failures import ProjectContext
    return ProjectContext(
        project_modules=frozenset({"calc"}),
        owned_paths=frozenset({"src/calc/sub.py", "src/calc/cli.py"}),
        programs=frozenset({"calc"}),
        test_paths=frozenset({TEST_FILE, "tests/fixtures/data.json", "tests/helpers.py"}),
        test_sources=sources if sources is not None else {TEST_FILE: source},
    )


def make_failure(exc_type, message, detail=None, kind="error", phase="call", crash_path=TEST_FILE,
                 crash_line=None, node=f"{TEST_FILE}::test_tc3_case"):
    from claude_kiro.failures import Failure
    return Failure(node=node, file=TEST_FILE, kind=kind, message=message, truncated=False,
                   command="uv run pytest -q", exc_type=exc_type, phase=phase, crash_path=crash_path,
                   crash_line=crash_line, detail=detail if detail is not None else message, missing_command=None)


def body_source(body, params="", before=""):
    """A test file with `before` module-level code and one test whose body is `body`."""
    return HEADER + before + f"def test_tc3_case({params}):\n{body}\n"


def line_of(source, needle, last=False):
    hits = [i for i, text in enumerate(source.splitlines(), 1) if needle in text]
    assert hits, f"{needle!r} not in source"
    return hits[-1] if last else hits[0]


def assert_failure(source, needle="assert ", message="assert 1 == 3", detail=None, crash_line=None):
    line = crash_line if crash_line is not None else line_of(source, needle, last=True)
    return make_failure("AssertionError", message, detail=detail, kind="assertion", crash_line=line)


def variants(failure, source):
    """The same failure and source as other tools show them: ANSI, CRLF, indented, long traceback, all together."""
    detail = failure.detail
    ansi = "\n".join(f"\x1b[1m\x1b[31m{ln}\x1b[0m" for ln in detail.split("\n"))
    long_tb = ("============ FAILURES ============\n___________ test_tc3_case ___________\n\n"
               f"    def test_tc3_case():\n>       assert 1 == 3\nE       assert 1 == 3\n\n{TEST_FILE}:{failure.crash_line}: "
               "AssertionError\n" + detail + "\n=========== short test summary info ===========\n"
               f"FAILED {TEST_FILE}::test_tc3_case\n1 failed, 2 passed in 0.03s\n")
    crlf_src = source.replace("\n", "\r\n")
    return [
        ("plain", failure, source),
        ("ansi", replace(failure, detail=ansi, message=f"\x1b[31m{failure.message}\x1b[0m"), source),
        ("crlf detail", replace(failure, detail=detail.replace("\n", "\r\n")), source),
        ("indented", replace(failure, detail="\n".join("    E   " + ln for ln in detail.split("\n"))), source),
        ("long traceback", replace(failure, detail=long_tb), source),
        ("crlf source", failure, crlf_src),
        ("all", replace(failure, detail=ansi.replace("\n", "\r\n") + "\r\n" + long_tb), crlf_src),
    ]


def expect(failure, source, allowed, ctx_sources=None):
    """Every variant classifies as one of `allowed`; `allowed` never mixes `bad` with the other classes."""
    from claude_kiro.failures import classify_not_built
    for label, fl, src in variants(failure, source):
        ctx = make_ctx(src, ctx_sources(src) if ctx_sources else None)
        got = classify_not_built(fl, ctx)
        assert got in allowed, f"{label}: got {got!r}, expected {allowed}\nfailure={fl}\nsource=\n{src}"


BAD = ("bad",)
UNCLEAR = ("unclear",)
NEVER_BAD = ("unclear", "not-built")

# =========================================================================== seam: must block (bad)


def test_tc52_redteam_seam_fixture_assert_through_alias():
    src = body_source(f'    data = {DATA}\n    alias = data\n    other = alias\n    assert other["a"] == 3')
    expect(assert_failure(src), src, BAD)


def test_tc52_redteam_seam_fixture_assert_through_comprehension():
    src = body_source(f'    rows = {DATA}\n    got = [r["v"] for r in rows["rows"] if r["v"] > 0]\n'
                      '    assert got == [1, 2]')
    expect(assert_failure(src), src, BAD)


def test_tc52_redteam_seam_fixture_assert_in_for_loop_over_file_rows():
    src = body_source('    for row in json.load(open("tests/fixtures/data.json")):\n        assert row["v"] > 3')
    expect(assert_failure(src), src, BAD)


def test_tc52_redteam_seam_fixture_assert_after_with_open_block():
    src = body_source('    with open("tests/fixtures/data.json") as fh:\n        rows = json.load(fh)\n'
                      '    total = sum(r["v"] for r in rows)\n    assert total == 3')
    expect(assert_failure(src), src, BAD)


def test_tc52_redteam_seam_fixture_assert_inside_with_block():
    src = body_source('    with open("tests/fixtures/data.json") as fh:\n        rows = json.load(fh)\n'
                      '        assert rows["a"] == 3')
    expect(assert_failure(src), src, BAD)


def test_tc52_redteam_seam_fixture_assert_through_same_file_helper():
    src = body_source('    assert load_prices()["AAPL"] == 4.15',
                      before='def load_prices():\n    return json.load(open("tests/fixtures/prices.json"))\n\n\n')
    expect(assert_failure(src), src, BAD)


def test_tc52_redteam_seam_fixture_assert_through_nested_same_file_helpers():
    before = ('def _read():\n    return Path("tests/fixtures/data.json").read_text()\n\n\n'
              'def load_prices():\n    return json.loads(_read())\n\n\n')
    src = body_source('    assert load_prices()["a"] == 3', before=before)
    expect(assert_failure(src), src, BAD)


def test_tc52_redteam_seam_literal_assert_with_quoted_no_such_command_message():
    src = body_source(f'    data = {DATA}\n    assert data["a"] == 3, "Error: No such command \'thesis\'."')
    f = assert_failure(src, message="AssertionError: Error: No such command 'thesis'.\nassert 1 == 3",
                       detail="AssertionError: Error: No such command 'thesis'.\nassert 1 == 3")
    expect(f, src, BAD)


def test_tc52_redteam_seam_literal_assert_with_quoted_invalid_choice_message():
    src = body_source('    assert 1 + 1 == 3, "calc: error: argument command: invalid choice: \'thesis\'"')
    f = assert_failure(src, message="AssertionError: calc: error: argument command: invalid choice: 'thesis'",
                       detail="AssertionError: calc: error: argument command: invalid choice: 'thesis'\nassert 2 == 3")
    expect(f, src, BAD)


def test_tc52_redteam_seam_literal_assert_with_quoted_not_implemented_message():
    src = body_source(f'    data = {DATA}\n    assert data["a"] == 3, "NotImplementedError: not yet"')
    f = assert_failure(src, message="AssertionError: NotImplementedError: not yet\nassert 1 == 3",
                       detail="AssertionError: NotImplementedError: not yet\nassert 1 == 3")
    expect(f, src, BAD)


def test_tc52_redteam_seam_literal_assert_with_quoted_module_not_found_message():
    src = body_source('    assert 2 + 2 == 5, "ModuleNotFoundError: No module named \'calc.sub\'"')
    f = assert_failure(src, message="AssertionError: ModuleNotFoundError: No module named 'calc.sub'",
                       detail="AssertionError: ModuleNotFoundError: No module named 'calc.sub'\nassert 4 == 5")
    expect(f, src, BAD)


def test_tc52_redteam_seam_multiline_fixture_assert_with_crash_line_on_a_continuation():
    src = body_source(f'    data = {DATA}\n    assert (\n        data["a"]\n        == 3\n    )')
    f = assert_failure(src, crash_line=line_of(src, '== 3'))
    expect(f, src, BAD)


def test_tc52_redteam_seam_name_error_for_a_missing_import_in_the_test_file():
    f = make_failure("NameError", "NameError: name 'subtract' is not defined",
                     detail="    def test_tc3_case():\n>       assert subtract(3, 1) == 2\nE       NameError: name "
                            "'subtract' is not defined", crash_line=9)
    expect(f, "", BAD)


def test_tc52_redteam_seam_name_error_naming_a_project_module():
    f = make_failure("NameError", "NameError: name 'calc' is not defined", crash_line=9)
    expect(f, "", BAD)


def test_tc52_redteam_seam_syntax_error_in_the_test_file():
    f = make_failure("SyntaxError", "SyntaxError: invalid syntax", kind="import", phase="collect",
                     detail="E     File \"tests/test_calc.py\", line 7\nE       def test_tc3_case(:\nE   SyntaxError: "
                            "invalid syntax")
    expect(f, "", BAD)


def test_tc52_redteam_seam_validation_error_in_fixture_setup():
    f = make_failure("ValueError", "ValueError: invalid enum 'Buy'", phase="setup", crash_path="tests/conftest.py")
    expect(f, "", BAD)


def test_tc52_redteam_seam_assert_in_conftest_helper():
    f = make_failure("AssertionError", "assert 0.05 == 0.04", kind="assertion", phase="setup",
                     crash_path="tests/conftest.py", crash_line=12)
    expect(f, "", BAD)


def test_tc52_redteam_seam_assert_in_test_helper_module_is_not_project_code():
    f = make_failure("AssertionError", "assert 1 == 2", kind="assertion", crash_path="tests/helpers.py",
                     crash_line=5)
    expect(f, "", BAD)


def test_tc52_redteam_seam_json_decode_error_loading_a_fixture():
    f = make_failure("JSONDecodeError", "json.decoder.JSONDecodeError: Expecting ',' delimiter: line 3 column 5",
                     phase="call", crash_path="tests/fixtures/data.json")
    expect(f, "", BAD)


def test_tc52_redteam_seam_missing_fixture_file_the_task_owns():
    f = make_failure("FileNotFoundError", "FileNotFoundError: [Errno 2] No such file or directory: "
                     "'tests/fixtures/data.json'")
    expect(f, "", BAD)


# =========================================================================== seam: must never block (unclear)


def test_tc52_redteam_seam_project_call_hidden_behind_local_variable():
    src = body_source('    result = calc.add(1, 1)\n    alias = result\n    assert alias == 3')
    expect(assert_failure(src), src, UNCLEAR)


def test_tc52_redteam_seam_project_name_through_from_import_alias():
    src = body_source('    assert plus(1, 1) == 3')
    expect(assert_failure(src), src, UNCLEAR)


def test_tc52_redteam_seam_project_constant_copied_to_a_local():
    src = body_source(f'    base = CONST\n    data = {DATA}\n    assert data["a"] + base == 3')
    expect(assert_failure(src), src, UNCLEAR)


def test_tc52_redteam_seam_project_call_in_comprehension():
    src = body_source('    got = [add(n, 1) for n in range(3)]\n    assert got == [0, 0, 0]')
    expect(assert_failure(src), src, UNCLEAR)


def test_tc52_redteam_seam_project_call_in_loop_body_feeding_the_assert():
    src = body_source('    total = 0\n    for n in range(3):\n        total += calc.add(n, 1)\n    assert total == 1')
    expect(assert_failure(src), src, UNCLEAR)


def test_tc52_redteam_seam_project_value_in_walrus():
    src = body_source('    assert (r := calc.add(1, 1)) == 3')
    expect(assert_failure(src), src, UNCLEAR)


def test_tc52_redteam_seam_project_call_in_with_target():
    src = body_source('    with calc.session() as s:\n        value = s.read()\n    assert value == 3')
    expect(assert_failure(src), src, UNCLEAR)


def test_tc52_redteam_seam_project_import_inside_the_test_function():
    src = body_source('    from calc import subtract\n    assert subtract(3, 1) == 3')
    expect(assert_failure(src), src, UNCLEAR)


def test_tc52_redteam_seam_standard_library_wrapping_a_project_call():
    src = body_source('    assert sorted(calc.items()) == [1, 2]')
    expect(assert_failure(src), src, UNCLEAR)


def test_tc52_redteam_seam_fixture_argument_that_only_returns_a_literal():
    src = body_source('    assert prices["AAPL"] == 4.15', params="prices")
    expect(assert_failure(src), src, UNCLEAR)


def test_tc52_redteam_seam_fixture_argument_reached_through_alias():
    src = body_source('    local = prices\n    value = local["AAPL"]\n    assert value == 4.15', params="prices")
    expect(assert_failure(src), src, UNCLEAR)


def test_tc52_redteam_seam_fixture_argument_beside_fixture_data():
    src = body_source(f'    data = {DATA}\n    assert data["a"] == client.value', params="client")
    expect(assert_failure(src), src, UNCLEAR)


def test_tc52_redteam_seam_same_file_helper_that_calls_project_code():
    src = body_source('    assert compute() == 3',
                      before='def compute():\n    return calc.add(1, 1)\n\n\n')
    expect(assert_failure(src), src, UNCLEAR)


def test_tc52_redteam_seam_same_file_helper_two_levels_above_project_code():
    before = ('def inner():\n    return calc.add(1, 1)\n\n\n'
              'def middle():\n    return inner() + 0\n\n\n')
    src = body_source('    assert middle() == 3', before=before)
    expect(assert_failure(src), src, UNCLEAR)


def test_tc52_redteam_seam_same_file_helper_with_a_project_default_argument():
    before = 'def compute(x=CONST):\n    return x\n\n\n'
    src = body_source('    assert compute() == 3', before=before)
    expect(assert_failure(src), src, UNCLEAR)


def test_tc52_redteam_seam_helper_imported_from_a_test_helper_module():
    src = ("import json\nfrom helpers import compute\n\n\n"
           "def test_tc3_case():\n    value = compute()\n    assert value == 3\n")
    expect(assert_failure(src), src, UNCLEAR)


def test_tc52_redteam_seam_helper_imported_with_a_relative_import():
    src = ("from .helpers import compute\n\n\n"
           "def test_tc3_case():\n    assert compute() == 3\n")
    expect(assert_failure(src), src, UNCLEAR)


def test_tc52_redteam_seam_multiline_project_assert_with_crash_line_on_a_continuation():
    src = body_source('    assert (\n        calc.add(1, 1)\n        == 3\n    )')
    f = assert_failure(src, crash_line=line_of(src, 'calc.add(1, 1)'))
    expect(f, src, UNCLEAR)


def test_tc52_redteam_seam_crash_line_that_is_not_an_assert():
    src = body_source('    x = 1\n    assert x == 3')
    expect(assert_failure(src, crash_line=line_of(src, "x = 1")), src, UNCLEAR)


def test_tc52_redteam_seam_crash_line_past_the_end_of_the_source():
    src = body_source('    assert 1 == 3')
    expect(assert_failure(src, crash_line=9999), src, UNCLEAR)


def test_tc52_redteam_seam_crash_line_missing():
    src = body_source('    assert 1 == 3')
    f = make_failure("AssertionError", "assert 1 == 3", kind="assertion", crash_line=None)
    expect(f, src, UNCLEAR)


def test_tc52_redteam_seam_test_source_not_available():
    f = make_failure("AssertionError", "assert 1 == 3", kind="assertion", crash_line=7)
    expect(f, "", UNCLEAR, ctx_sources=lambda src: {})


def test_tc52_redteam_seam_test_source_changed_into_something_unparseable():
    src = "def test_tc3_case(:\n    assert (\n"
    expect(make_failure("AssertionError", "assert", kind="assertion", crash_line=2), src, UNCLEAR)


def test_tc52_redteam_seam_test_source_that_is_empty():
    expect(make_failure("AssertionError", "assert", kind="assertion", crash_line=1), "", UNCLEAR)


def test_tc52_redteam_seam_assert_outside_any_test_function():
    src = HEADER + "assert calc.add(1, 1) == 3\n"
    expect(assert_failure(src), src, UNCLEAR)


# =========================================================================== seam: not-built forms stay not-built


def test_tc52_redteam_seam_not_implemented_error_raised_by_the_test_itself_is_never_bad():
    f = make_failure("NotImplementedError", "NotImplementedError", crash_path=TEST_FILE, crash_line=7)
    expect(f, body_source("    raise NotImplementedError"), NEVER_BAD)


def test_tc52_redteam_seam_not_implemented_error_from_project_code_is_not_built():
    f = make_failure("NotImplementedError", "NotImplementedError: add is a stub", crash_path="calc.py",
                     crash_line=4)
    expect(f, "", ("not-built",))


def test_tc52_redteam_seam_import_typo_of_a_project_module_is_never_not_built():
    f = make_failure("ModuleNotFoundError", "ModuleNotFoundError: No module named 'calcc'", kind="import",
                     phase="collect", crash_line=3)
    expect(f, "from calcc import x\n", ("bad", "unclear"))


def test_tc52_redteam_seam_import_of_a_missing_project_submodule_is_not_built():
    f = make_failure("ModuleNotFoundError", "ModuleNotFoundError: No module named 'calc.sub'", kind="import",
                     phase="collect", crash_line=3)
    expect(f, "from calc.sub import x\n", ("not-built",))


def test_tc52_redteam_seam_cannot_import_name_with_curly_quotes_and_ansi():
    f = make_failure("ImportError", "ImportError: cannot import name \u2018subtract2\u2019 from \u2018calc\u2019",
                     kind="import", phase="collect", crash_line=3)
    expect(f, "from calc import subtract2\n", ("not-built",))


def test_tc52_redteam_seam_click_no_such_command_with_ansi_inside_the_message():
    detail = ("assert 2 == 0\n  + where 2 = <Result SystemExit(2)>.exit_code\nUsage: calc [OPTIONS] COMMAND "
              "[ARGS]...\nTry 'calc --help' for help.\n\n\x1b[31mError:\x1b[0m No such command \u201cthesis\u201d.")
    src = body_source('    result = runner.invoke(calc_cli, ["thesis"])\n    assert result.exit_code == 0', params="runner")
    f = assert_failure(src, message="assert 2 == 0", detail=detail)
    expect(f, src, ("not-built",))


def test_tc52_redteam_seam_argparse_invalid_choice_wrapped_over_two_lines():
    detail = ("usage: calc [-h] {run,list} ...\ncalc: error: argument command: invalid choice:\r\n"
              "    'thesis' (choose from 'run', 'list')")
    f = make_failure("SystemExit", "2", detail=detail)
    expect(f, "", ("not-built",))


def test_tc52_redteam_seam_another_programs_missing_command_is_not_not_built():
    detail = ("assert 2 == 0\nUsage: othertool [OPTIONS] COMMAND [ARGS]...\nTry 'othertool --help' for help.\n\n"
              "Error: No such command 'thesis'.")
    src = body_source('    result = runner.invoke(other_cli, ["thesis"])\n    assert result.exit_code == 0',
                      params="runner")
    f = assert_failure(src, message="assert 2 == 0", detail=detail)
    from claude_kiro.failures import classify_not_built
    for label, fl, s in variants(f, src):
        assert classify_not_built(fl, make_ctx(s)) != "not-built", label


def test_tc52_redteam_seam_third_party_module_missing_is_not_not_built():
    f = make_failure("ModuleNotFoundError", "ModuleNotFoundError: No module named 'numpy'", kind="import",
                     phase="collect", crash_line=1)
    expect(f, "import numpy\n", UNCLEAR)


# =========================================================================== assert_reaches_project (5.7)


def reaches(src, line, sources=None):
    from claude_kiro.failures import assert_reaches_project
    return assert_reaches_project(src, line, make_ctx(src, sources))


def test_tc52_redteam_seam_reaches_false_for_alias_of_file_data():
    src = body_source(f'    data = {DATA}\n    alias = data\n    assert alias["a"] == 3')
    assert reaches(src, line_of(src, "assert ", last=True)) is False
    assert reaches(src.replace("\n", "\r\n"), line_of(src, "assert ", last=True)) is False


def test_tc52_redteam_seam_reaches_true_for_project_call_behind_local():
    src = body_source('    r = calc.add(1, 1)\n    s = r\n    assert s == 3')
    assert reaches(src, line_of(src, "assert ", last=True)) is True


def test_tc52_redteam_seam_reaches_true_for_fixture_argument():
    src = body_source('    assert prices["A"] == 3', params="prices")
    assert reaches(src, line_of(src, "assert ", last=True)) is True


def test_tc52_redteam_seam_reaches_true_for_helper_calling_project_in_same_file():
    src = body_source('    assert helper() == 3', before='def helper():\n    return add(1, 1)\n\n\n')
    assert reaches(src, line_of(src, "assert helper", last=True)) is True


def test_tc52_redteam_seam_reaches_true_for_helper_from_a_test_helper_module():
    src = "from helpers import helper\n\n\ndef test_tc3_case():\n    assert helper() == 3\n"
    assert reaches(src, 5) is True


def test_tc52_redteam_seam_reaches_none_for_a_line_that_is_not_an_assert():
    src = body_source('    x = 1\n    assert x == 3')
    assert reaches(src, line_of(src, "x = 1")) is None


def test_tc52_redteam_seam_reaches_none_for_unparseable_source():
    assert reaches("def test_tc3_case(:\n    assert (\n", 2) is None


def test_tc52_redteam_seam_reaches_none_for_a_line_past_the_end():
    assert reaches(body_source('    assert 1 == 3'), 500) is None


# =========================================================================== CLI: ck gate demo --task 1 --json

PRICES = {"AAPL": 4.1, "MSFT": 9.5}
DATA_JSON = {"a": 1, "rows": [{"v": 1}, {"v": 2}]}

CLI_HEADER = (
    "import json\n"
    "from pathlib import Path\n"
    "\n"
    "import calc\n"
    "import pytest\n"
    "from calc import add\n"
    "\n"
    "FIX = Path(__file__).parent / 'fixtures'\n"
    "\n"
    "\n"
)

HELPERS_MODULE = "import calc\n\n\ndef compute():\n    return calc.add(1, 1)\n"


def gate_project(tmp_path, test_source, extra_files=None, newline="\n"):
    """`attr_project` plus a real task-1 worktree holding `test_source` as the task's test file."""
    root = h.attr_project(tmp_path, statuses={})
    code, _, err = h.ck_text(root, "worktree", "create", "demo", "1", "--base", "integrate/demo")
    assert code == 0, err
    wt = h.task_worktree(root, "1")
    tasks_md = wt / "specs" / "demo" / "tasks.md"
    tasks_md.write_text(tasks_md.read_text().replace("- [ ]", "- [x]"))  # unchecked boxes would fail every gate
    files = {
        CALC_TEST_FILE: CLI_HEADER + test_source,
        "tests/fixtures/prices.json": json.dumps(PRICES),
        "tests/fixtures/data.json": json.dumps(DATA_JSON),
    }
    files.update(extra_files or {})
    for rel, content in files.items():
        path = wt / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".py") and newline != "\n":
            content = content.replace("\n", newline)
        path.write_bytes(content.encode())
    h.commit_all(wt, "task 1: tests")
    return wt


def run_gate(wt, env=None):
    code, obj, err = h.ck_json(wt, "gate", "demo", "--task", "1", "--json", env=env)
    assert obj is not None, f"gate printed no JSON (exit {code}): {err}"
    return code, obj


def classes(obj):
    return {f["node"].split("::")[-1]: f.get("class") for f in obj["failures"] if f.get("node")}


def right_reason(obj):
    hits = [c for c in obj["checks"] if "right reason" in c["name"].lower()]
    assert len(hits) == 1, f"expected one right-reason check, got {[c['name'] for c in obj['checks']]}"
    return hits[0]


def assert_blocks(code, obj, bad_nodes, unclear_nodes=()):
    got = classes(obj)
    for n in bad_nodes:
        assert got.get(n) == "bad", f"{n}: class {got.get(n)!r}, expected 'bad'; classes={got}"
    for n in unclear_nodes:
        assert got.get(n) == "unclear", f"{n}: class {got.get(n)!r}, expected 'unclear'; classes={got}"
    check = right_reason(obj)
    assert check["ok"] is False, check
    assert code != 0 and obj["ok"] is False


def assert_passes(code, obj, unclear_nodes):
    got = classes(obj)
    for n in unclear_nodes:
        assert got.get(n) == "unclear", f"{n}: class {got.get(n)!r}, expected 'unclear'; classes={got}"
    assert "bad" not in got.values(), got
    check = right_reason(obj)
    assert check["ok"] is True and check.get("warning") is True, check
    assert code == 0 and obj["ok"] is True, obj["checks"]


FIXTURE_ONLY_TESTS = '''\
def test_tc1_alias():
    prices = json.load(open(FIX / "prices.json"))
    alias = prices
    assert alias["AAPL"] == 4.15


def test_tc1_comprehension():
    prices = json.loads((FIX / "prices.json").read_text())
    got = [prices[k] for k in sorted(prices)]
    assert got == [4.15, 9.5]


def test_tc1_with_open():
    with open(FIX / "data.json") as fh:
        rows = json.load(fh)
    assert len(rows["rows"]) == 3


def test_tc1_helper():
    assert _load()["AAPL"] == 4.15


def _load():
    return json.load(open(FIX / "prices.json"))
'''
FIXTURE_ONLY_NODES = ["test_tc1_alias", "test_tc1_comprehension", "test_tc1_with_open", "test_tc1_helper"]


def test_tc52_redteam_fixture_data_asserts_in_disguise_all_block(tmp_path):
    wt = gate_project(tmp_path, FIXTURE_ONLY_TESTS)
    code, obj = run_gate(wt)
    assert_blocks(code, obj, FIXTURE_ONLY_NODES)


def test_tc52_redteam_fixture_data_assert_blocks_under_every_traceback_style(tmp_path):
    wt = gate_project(tmp_path, FIXTURE_ONLY_TESTS)
    for opts in ("--tb=native", "--tb=long", "--tb=short", "--color=yes"):
        code, obj = run_gate(wt, env={"PYTEST_ADDOPTS": opts, "PY_COLORS": "1"})
        assert_blocks(code, obj, FIXTURE_ONLY_NODES)


def test_tc52_redteam_fixture_data_assert_blocks_in_a_crlf_test_file(tmp_path):
    wt = gate_project(tmp_path, FIXTURE_ONLY_TESTS, newline="\r\n")
    code, obj = run_gate(wt)
    assert_blocks(code, obj, FIXTURE_ONLY_NODES)


def test_tc52_redteam_quoted_cli_message_in_a_fixture_assert_still_blocks(tmp_path):
    src = ('def test_tc1_quoted_no_such_command():\n'
           '    prices = json.load(open(FIX / "prices.json"))\n'
           '    assert prices["AAPL"] == 4.15, "Error: No such command \'thesis\'."\n\n\n'
           'def test_tc1_quoted_invalid_choice():\n'
           '    assert 1 + 1 == 3, "calc: error: argument command: invalid choice: \'thesis\'"\n\n\n'
           'def test_tc1_quoted_not_implemented():\n'
           '    assert len("ab") == 3, "NotImplementedError: not yet"\n')
    wt = gate_project(tmp_path, src)
    code, obj = run_gate(wt)
    assert_blocks(code, obj, ["test_tc1_quoted_no_such_command", "test_tc1_quoted_invalid_choice",
                              "test_tc1_quoted_not_implemented"])


def test_tc52_redteam_name_error_and_setup_error_block(tmp_path):
    src = ('def test_tc1_forgot_import():\n    assert subtract(3, 1) == 2\n\n\n'
           '@pytest.fixture\ndef broken():\n    raise ValueError("invalid enum \'Buy\'")\n\n\n'
           'def test_tc1_setup_error(broken):\n    assert True\n')
    wt = gate_project(tmp_path, src)
    code, obj = run_gate(wt)
    assert_blocks(code, obj, ["test_tc1_forgot_import", "test_tc1_setup_error"])


def test_tc52_redteam_one_wrong_reason_failure_blocks_among_unclear_ones(tmp_path):
    src = ('def test_tc1_unclear_project_value():\n    assert add(1, 1) == 3\n\n\n'
           'def test_tc1_bad_fixture_value():\n    prices = json.load(open(FIX / "prices.json"))\n'
           '    assert prices["AAPL"] == 4.15\n\n\n'
           'def test_tc1_not_built():\n    from calc import subtract_all\n    assert subtract_all([3, 1]) == 2\n')
    wt = gate_project(tmp_path, src)
    code, obj = run_gate(wt)
    assert_blocks(code, obj, ["test_tc1_bad_fixture_value"], ["test_tc1_unclear_project_value"])
    assert classes(obj).get("test_tc1_not_built") in ("not-built", None)


UNCLEAR_TESTS = '''\
def test_tc1_project_call_behind_local():
    result = calc.add(1, 1)
    alias = result
    assert alias == 3


def test_tc1_project_name_in_comprehension():
    got = [add(n, 1) for n in range(3)]
    assert got == [0, 0, 0]


@pytest.fixture
def prices():
    return {"AAPL": 4.1}


def test_tc1_fixture_argument_returning_a_literal(prices):
    assert prices["AAPL"] == 4.15


def test_tc1_same_file_helper_calling_project():
    assert _compute() == 3


def _compute():
    return calc.add(1, 1)


def test_tc1_helper_module_calling_project():
    from helpers import compute
    assert compute() == 3
'''
UNCLEAR_NODES = ["test_tc1_project_call_behind_local", "test_tc1_project_name_in_comprehension",
                 "test_tc1_fixture_argument_returning_a_literal", "test_tc1_same_file_helper_calling_project",
                 "test_tc1_helper_module_calling_project"]


def test_tc52_redteam_asserts_that_reach_project_code_never_block(tmp_path):
    wt = gate_project(tmp_path, UNCLEAR_TESTS, extra_files={"tests/helpers.py": HELPERS_MODULE})
    code, obj = run_gate(wt)
    assert_passes(code, obj, UNCLEAR_NODES)


def test_tc52_redteam_asserts_that_reach_project_code_never_block_in_color_crlf_and_native_tracebacks(tmp_path):
    wt = gate_project(tmp_path, UNCLEAR_TESTS, extra_files={"tests/helpers.py": HELPERS_MODULE}, newline="\r\n")
    for opts in ("--tb=native", "--tb=long", "--color=yes"):
        code, obj = run_gate(wt, env={"PYTEST_ADDOPTS": opts, "PY_COLORS": "1"})
        assert_passes(code, obj, UNCLEAR_NODES)


def test_tc52_redteam_multiline_assert_on_project_code_never_blocks(tmp_path):
    src = ('def test_tc1_multiline_project_assert():\n    assert (\n        calc.add(1, 1)\n        == 3\n    )\n')
    wt = gate_project(tmp_path, src)
    code, obj = run_gate(wt)
    assert_passes(code, obj, ["test_tc1_multiline_project_assert"])


def test_tc52_redteam_not_implemented_error_from_the_test_itself_never_blocks_as_bad(tmp_path):
    src = 'def test_tc1_raises_itself():\n    raise NotImplementedError("later")\n'
    wt = gate_project(tmp_path, src)
    code, obj = run_gate(wt)
    assert classes(obj).get("test_tc1_raises_itself") in ("not-built", "unclear"), classes(obj)
    assert "bad" not in classes(obj).values()
    assert right_reason(obj)["ok"] is True


def test_tc52_redteam_import_typo_of_a_project_module_is_not_counted_as_not_built(tmp_path):
    src = 'from calcc import add2\n\n\ndef test_tc1_typo():\n    assert add2(1, 1) == 2\n'
    wt = gate_project(tmp_path, src)
    _, obj = run_gate(wt)
    entries = [f for f in obj["failures"] if (f.get("file") or "").endswith("test_calc_tc.py")]
    assert entries, obj["failures"]
    assert all(f.get("class") in ("bad", "unclear") for f in entries), entries


def test_tc52_redteam_missing_project_name_stays_not_built_under_ansi_color(tmp_path):
    src = ('def test_tc1_missing_name():\n    from calc import subtract_all\n    assert subtract_all([3, 1]) == 2\n\n\n'
           'def test_tc1_missing_attribute():\n    assert calc.multiply(2, 3) == 6\n')
    wt = gate_project(tmp_path, src, newline="\r\n")
    for opts in ("--color=yes", "--tb=native", "--tb=long"):
        code, obj = run_gate(wt, env={"PYTEST_ADDOPTS": opts, "PY_COLORS": "1"})
        got = classes(obj)
        assert got.get("test_tc1_missing_name") == "not-built", got
        assert got.get("test_tc1_missing_attribute") == "not-built", got
        assert right_reason(obj)["ok"] is True and code == 0, obj["checks"]
