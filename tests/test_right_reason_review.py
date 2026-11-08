"""Seam cases for the review round 1 findings on the right-reason classifier (unattended-correctness Story 5).

Each case calls `claude_kiro.failures.classify_not_built`, `right_reason_verdict` or `assert_reaches_project` with
real `Failure` and `ProjectContext` values, as the red-team seam cases do. Nothing is mocked.
"""

import pytest

TEST_FILE = "tests/test_calc.py"
HELPER_FILE = "tests/helpers.py"


def make_ctx(source="", sources=None):
    from claude_kiro.failures import ProjectContext
    return ProjectContext(
        project_modules=frozenset({"calc"}),
        owned_paths=frozenset({"src/calc/sub.py", "calc.py"}),
        programs=frozenset({"calc"}),
        test_paths=frozenset({TEST_FILE, "tests/fixtures/data.json", HELPER_FILE}),
        test_sources=sources if sources is not None else {TEST_FILE: source},
    )


def make_failure(exc_type, message, detail=None, kind="assertion", phase="call", crash_path=TEST_FILE,
                 crash_line=None, file=TEST_FILE):
    from claude_kiro.failures import Failure
    return Failure(node=f"{file}::test_tc3_case", file=file, kind=kind, message=message, truncated=False,
                   command="uv run pytest -q", exc_type=exc_type, phase=phase, crash_path=crash_path,
                   crash_line=crash_line, detail=detail if detail is not None else message, missing_command=None)


def body(text, header="import json\nfrom pathlib import Path\n"):
    """A test file with one test whose body is `text`; returns (source, line of its last assert)."""
    src = header + "\n\ndef test_tc3_case():\n" + "".join(f"    {ln}\n" for ln in text.splitlines())
    lines = src.splitlines()
    line = max(i for i, ln in enumerate(lines, 1) if ln.strip().startswith("assert"))
    return src, line


def classify_body(text, header="import json\nfrom pathlib import Path\n"):
    from claude_kiro.failures import classify_not_built
    src, line = body(text, header)
    f = make_failure("AssertionError", "assert 1 == 2", crash_line=line)
    return classify_not_built(f, make_ctx(src))


# --- finding 1: names the parse cannot bind reach project code (unclear, never bad) ----------------

@pytest.mark.parametrize("text,header", [
    ("assert f() == 2", "from mypkg import *\n"),                    # wildcard import of a project module
    ("assert undefined_global == 2", ""),                            # unbound non-builtin name
    ("assert __import__('mypkg').value == 2", ""),                   # code-running builtin
    ("assert eval('2') == 3", ""),                                   # code-running builtin
])
def test_review_seam_unbound_or_code_running_names_are_unclear(text, header):
    assert classify_body(text, header) == "unclear"


def test_review_seam_module_dunder_file_stays_data_only():
    # `Path(__file__)` is the test file's own path: a literal-only assert on file data is still bad
    text = 'data = json.loads(Path(__file__).read_text())\nassert data == 3'
    assert classify_body(text, "import json\nfrom pathlib import Path\n") == "bad"


def test_review_seam_lambda_parameters_are_values_not_fixtures():
    # a lambda's own parameter is a local value: the assert uses no project code, so it is bad
    assert classify_body("assert all(map(lambda x: x == 1, [2])) is False", "") == "bad"


# --- finding 1: program-running modules reach project code -----------------------------------------

@pytest.mark.parametrize("text,header", [
    ("assert subprocess.run(['mtr', 'x']).returncode == 0", "import subprocess\n"),
    ("assert run(['mtr', 'x']).returncode == 0", "from subprocess import run\n"),
    ("assert importlib.import_module('mypkg').value == 2", "import importlib\n"),
    ("assert os.system('mtr x') == 0", "import os\n"),
    ("assert system('mtr x') == 0", "from os import system\n"),
    ("assert runpy.run_module('mypkg').get('x') == 2", "import runpy\n"),
])
def test_review_seam_program_running_modules_are_unclear(text, header):
    assert classify_body(text, header) == "unclear"


def test_review_seam_os_path_alone_stays_bad():
    assert classify_body("assert os.path.basename('a/b') == 'c'", "import os\n") == "bad"


# --- finding 4: installed third-party data readers do not count ------------------------------------

def test_review_seam_pytest_approx_on_literal_is_bad():
    assert classify_body("assert json.loads('2') == pytest.approx(3)", "import json\nimport pytest\n") == "bad"


def test_review_seam_third_party_reader_on_fixture_data_is_bad():
    # `packaging` is installed (a data reader of file text): the assert uses no project code
    text = 'v = Version(Path("tests/fixtures/data.json").read_text())\nassert v == Version("1.0")'
    assert classify_body(text, "from packaging.version import Version\nfrom pathlib import Path\n") == "bad"


def test_review_seam_helper_module_call_is_unclear():
    # a test helper file in the repository may run project code: the parse cannot see it (5.7)
    assert classify_body("assert check(1) == 2", "from tests.helpers import check\n") == "unclear"


# --- finding 2: a wrapped NotImplementedError anywhere in the chain ---------------------------------

def test_review_seam_chained_not_implemented_is_not_built():
    from claude_kiro.failures import classify_not_built
    detail = ("Traceback (most recent call last):\n  File \"calc.py\", line 9, in thesis_report\n"
              "NotImplementedError: thesis report is not built yet\n\n"
              "The above exception was the direct cause of the following exception:\n\n"
              "Traceback (most recent call last):\n  File \"tests/test_calc.py\", line 4\n"
              "RuntimeError: wrapped")
    f = make_failure("RuntimeError", "RuntimeError: wrapped", detail=detail, kind="error", phase="call",
                     crash_path=TEST_FILE)
    assert classify_not_built(f, make_ctx("")) == "not-built"


def test_review_seam_not_implemented_quoted_in_a_message_is_not_chained():
    from claude_kiro.failures import classify_not_built
    src, line = body('data = json.loads(Path("tests/fixtures/data.json").read_text())\nassert data == 3')
    f = make_failure("AssertionError", "AssertionError: NotImplementedError: later", crash_line=line,
                     detail="AssertionError: NotImplementedError: later\nassert 1 == 3")
    assert classify_not_built(f, make_ctx(src)) == "bad"


# --- finding 5: a project source file under a fixtures directory is not test-side -----------------

def test_review_seam_project_fixtures_module_assert_is_not_bad():
    from claude_kiro.failures import classify_not_built
    f = make_failure("AssertionError", "assert 1 == 2", crash_path="src/pkg/fixtures/loader.py", crash_line=3,
                     file=TEST_FILE)
    assert classify_not_built(f, make_ctx("")) == "unclear"


def test_review_seam_test_fixture_module_assert_is_bad():
    from claude_kiro.failures import classify_not_built
    f = make_failure("AssertionError", "assert 1 == 2", crash_path="tests/fixtures/helper.py", crash_line=3,
                     file=TEST_FILE)
    assert classify_not_built(f, make_ctx("")) == "bad"


# --- finding 3: right_reason_verdict needs its context, and its own verdict shape is pinned --------

def test_review_seam_verdict_without_context_refuses_to_fail_open():
    from claude_kiro.failures import TestResult, right_reason_verdict
    res = [TestResult(node="n", passed=False, failure=make_failure("AssertionError", "x"))]
    with pytest.raises(ValueError):
        right_reason_verdict(res, all_verified_done=False)


def test_review_seam_verdict_all_done_requires_pass_without_context():
    from claude_kiro.failures import right_reason_verdict
    v = right_reason_verdict([], all_verified_done=True)
    assert v.require_pass is True and v.blocking == () and v.unclear == () and v.already_passing == ()


def test_review_seam_verdict_splits_blocking_unclear_and_passing():
    from claude_kiro.failures import TestResult, right_reason_verdict
    src, line = body('data = json.loads(Path("tests/fixtures/data.json").read_text())\nassert data == 3')
    bad = make_failure("AssertionError", "assert 1 == 3", crash_line=line)
    unclear = make_failure("KeyError", "KeyError: 'b'", kind="error", phase="call")
    results = [TestResult(node="a", passed=True, failure=None),
               TestResult(node="b", passed=False, failure=bad),
               TestResult(node="c", passed=False, failure=unclear)]
    v = right_reason_verdict(results, all_verified_done=False, ctx=make_ctx(src))
    assert v.require_pass is False
    assert v.blocking == (bad,)
    assert v.unclear == (unclear,)
    assert v.already_passing == ("a",)


# --- the reviewer's cases stay as they are: a repository helper error is a wrong reason --------------

def test_review_seam_helper_import_error_is_bad():
    from claude_kiro.failures import classify_not_built
    f = make_failure("ModuleNotFoundError", "No module named 'tests.helpers'", kind="import",
                     detail="ModuleNotFoundError: No module named 'tests.helpers'", phase="call")
    assert classify_not_built(f, make_ctx("")) == "bad"
