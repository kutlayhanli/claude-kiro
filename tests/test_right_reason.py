"""Right-reason classification on test-track gates (unattended-correctness Story 5).

TC-50 to TC-54, TC-56, TC-57, TC-181. Every case builds a real project (`attr_project`, spec `demo`), adds the
fixture click and argparse CLIs and a `calc.py` to `integrate/demo`, branches test task 1's worktree from it,
commits real failing pytest files there and runs `ck gate demo --task 1 --json` through the real CLI.
"""

import json
import textwrap
from pathlib import Path

import pytest

import unattended_helpers as h

TEST_FILE = "tests/test_calc_tc.py"

CALC = '''\
def add(a, b):
    return a + b


def rate_floor(prices):
    return 0.05  # Task 2's stub: a wrong value


def thesis_report():
    raise NotImplementedError("thesis report is not built yet")


def validate_trade(trade):
    if trade.get("side") not in ("BUY", "SELL"):
        raise ValueError("invalid enum %r" % trade.get("side"))
    return trade
'''

CONFTEST = ("import sys, pathlib\n"
            "_root = pathlib.Path(__file__).parent\n"
            "sys.path.insert(0, str(_root))\n"
            "sys.path.insert(0, str(_root / 'src'))\n")

DESIGN = ("# Design: Demo\n\n## Components\n\n- `calc.py`: add and subtract.\n\n## CLI\n\n"
          + h.CLI_TABLE_HEADER + "| `mtr thesis new` | `src/mtr/cli.py` |\n| `mtr2 report` | `src/mtr2/cli.py` |\n")

HEADER = '''\
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, PYTHONPATH=str(ROOT / "src"))


def run(*argv):
    return subprocess.run([sys.executable, *argv], cwd=ROOT, env=ENV, capture_output=True, text=True)


'''

PRICES = '{"AAPL": 4.25, "MSFT": 3.1}\n'


def build(tmp_path, tests: str, statuses=None):
    """Project + task 1 worktree holding the committed test file. Returns the task worktree."""
    root = h.attr_project(tmp_path, statuses=statuses if statuses is not None else {})
    integ = h.integration_worktree(root)
    h._commit_in(integ, {
        "calc.py": CALC, "conftest.py": CONFTEST, "specs/demo/design.md": DESIGN,
        "src/mtr/__init__.py": "", "src/mtr/cli.py": h.MTR_CLI,
        "src/mtr2/__init__.py": "", "src/mtr2/cli.py": h.MTR2_CLI,
        "tests/fixtures/prices.json": PRICES,
        "tests/fixtures/trade.json": '{"side": "Buy"}\n',
    }, "sources")
    code, _, err = h.ck_text(root, "worktree", "create", "demo", "1", "--base", "integrate/demo")
    assert code == 0, err
    wt = h.task_worktree(root, 1)
    (wt / "tests").mkdir(exist_ok=True)
    (wt / TEST_FILE).write_text(HEADER + textwrap.dedent(tests))
    h.commit_all(wt, "task [1]: tests")
    return wt


def gate(wt):
    code, obj, err = h.ck_json(wt, "gate", "demo", "--task", "1", "--json")
    assert isinstance(obj, dict), f"gate printed no JSON (exit {code}): {err}"
    return code, obj


def entries(obj):
    return [f for f in obj["failures"] if (f.get("file") or "").endswith("test_calc_tc.py")]


def rr_checks(obj):
    return [c for c in obj["checks"] if "right-reason" in c["name"].lower().replace(" ", "-")]


def classes(obj):
    return sorted(f.get("class") for f in entries(obj))


# --- TC-50 ---------------------------------------------------------------------------------------


def test_tc50_import_error_of_unbuilt_name_is_not_built(tmp_path):
    wt = build(tmp_path, '''
        def test_tc3_subtract():
            from calc import subtract
            assert subtract(3, 1) == 2
    ''')
    code, obj = gate(wt)
    es = entries(obj)
    assert len(es) == 1, obj["failures"]
    assert es[0]["class"] == "not-built"
    assert code == 0 and obj["ok"] is True
    assert all(c["ok"] for c in rr_checks(obj)), obj["checks"]


# --- TC-51 ---------------------------------------------------------------------------------------

TC51_CASES = {
    "click-no-such-command": '''
        def test_tc3_cli():
            from mtr.cli import main
            main(["thesis", "report"])
    ''',
    "argparse-invalid-choice": '''
        def test_tc3_cli():
            out = run("-m", "mtr2.cli", "thesis")
            assert out.returncode == 0, out.stderr
    ''',
    "argparse-unrecognized-arguments": '''
        def test_tc3_cli():
            out = run("-m", "mtr2.cli", "report", "--since", "2020")
            assert out.returncode == 0, out.stderr
    ''',
    "click-no-such-option": '''
        def test_tc3_cli():
            out = run("-m", "mtr.cli", "version", "--since", "2020")
            assert out.returncode == 0, out.stderr
    ''',
    "not-implemented-error": '''
        def test_tc3_calc():
            import calc
            calc.thesis_report()
    ''',
    "attribute-error-on-calc": '''
        def test_tc3_calc():
            import calc
            calc.subtract(3, 1)
    ''',
}


@pytest.mark.parametrize("case", sorted(TC51_CASES))
def test_tc51_unbuilt_feature_failures_are_not_built(tmp_path, case):
    wt = build(tmp_path, TC51_CASES[case])
    code, obj = gate(wt)
    es = entries(obj)
    assert len(es) == 1, obj["failures"]
    assert es[0]["class"] == "not-built", es[0]
    assert code == 0 and obj["ok"] is True, obj["checks"]


# --- TC-52 ---------------------------------------------------------------------------------------


def test_tc52_assert_on_fixture_data_only_is_bad(tmp_path):
    wt = build(tmp_path, '''
        def test_tc3_prices():
            prices = json.load(open(ROOT / "tests" / "fixtures" / "prices.json"))
            assert prices["AAPL"] == 4.15
    ''')
    code, obj = gate(wt)
    es = entries(obj)
    assert len(es) == 1, obj["failures"]
    assert es[0]["class"] == "bad", es[0]
    assert es[0]["check"] == "right-reason"
    assert code != 0 and obj["ok"] is False
    assert any(not c["ok"] and not c.get("warning") for c in rr_checks(obj)), obj["checks"]


# --- TC-53 ---------------------------------------------------------------------------------------


def test_tc53_validation_error_loading_fixture_is_wrong_reason(tmp_path):
    wt = build(tmp_path, '''
        @pytest.fixture
        def trade():
            import calc
            return calc.validate_trade(json.load(open(ROOT / "tests" / "fixtures" / "trade.json")))


        def test_tc3_trade(trade):
            assert trade
    ''')
    code, obj = gate(wt)
    es = entries(obj)
    assert len(es) == 1, obj["failures"]
    assert es[0]["class"] == "bad", es[0]
    assert es[0]["check"] == "right-reason"
    assert "invalid enum" in es[0]["message"]
    assert code != 0 and obj["ok"] is False


# --- TC-54 ---------------------------------------------------------------------------------------


def test_tc54_third_party_import_and_test_built_keyerror_are_unclear(tmp_path):
    wt = build(tmp_path, '''
        def test_tc3_third_party():
            import numpy_not_installed_anywhere  # noqa: F401


        def test_tc4_keyerror():
            table = {"a": 1}
            return table["b"]
    ''')
    code, obj = gate(wt)
    assert classes(obj) == ["unclear", "unclear"], obj["failures"]
    assert code == 0 and obj["ok"] is True
    assert rr_checks(obj), obj["checks"]
    assert all(c["ok"] or c.get("warning") for c in rr_checks(obj))


# --- TC-56 ---------------------------------------------------------------------------------------


def test_tc56_already_passing_test_is_a_warning_and_gate_passes(tmp_path):
    wt = build(tmp_path, '''
        def test_tc1_already_passes():
            import calc
            assert calc.add(1, 2) == 3
    ''')
    code, obj = gate(wt)
    assert code == 0 and obj["ok"] is True, obj["checks"]
    warns = [c for c in obj["checks"] if c.get("warning")
             and "test_tc1_already_passes" in (c["name"] + " " + c.get("detail", ""))]
    assert warns, obj["checks"]


# --- TC-57 ---------------------------------------------------------------------------------------


def test_tc57_all_verified_tasks_done_requires_pass(tmp_path):
    wt = build(tmp_path, '''
        def test_tc3_subtract():
            from calc import subtract
            assert subtract(3, 1) == 2
    ''', statuses={"2": "Done", "3": "Done"})
    code, obj = gate(wt)
    assert code != 0 and obj["ok"] is False
    assert not rr_checks(obj), obj["checks"]
    assert all(f.get("class") is None for f in entries(obj)), obj["failures"]


# --- TC-181 --------------------------------------------------------------------------------------

TC181_CASES = {
    "assert-on-project-call": '''
        def test_tc3_rate_floor():
            import calc
            prices = json.load(open(ROOT / "tests" / "fixtures" / "prices.json"))
            assert calc.rate_floor(prices) == 0.04
    ''',
    "assert-on-pytest-fixture-argument": '''
        @pytest.fixture
        def value():
            return 3


        def test_tc3_value(value):
            assert value == 4
    ''',
    "assert-line-cannot-be-parsed-back": '''
        def test_tc3_source_changes():
            Path(__file__).write_text("# the source changed after the run\\n")
            assert 1 == 2
    ''',
}


@pytest.mark.parametrize("case", sorted(TC181_CASES))
def test_tc181_assert_reaching_project_code_or_unparsable_is_unclear(tmp_path, case):
    wt = build(tmp_path, TC181_CASES[case])
    code, obj = gate(wt)
    es = entries(obj)
    assert len(es) == 1, obj["failures"]
    assert es[0]["kind"] == "assertion"
    assert es[0]["class"] == "unclear", es[0]
    assert code == 0 and obj["ok"] is True, obj["checks"]
    assert rr_checks(obj), obj["checks"]
