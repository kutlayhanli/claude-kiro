"""The xdist hint's decision seam (Story 7): `xdist.has_pytest_without_n` and `xdist.suggestion`.

`ck doctor` / `ck init` show `suggestion(verify)`; their own tests are in test_doctor_xdist.py.
"""

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

PROPERTIES = {"P-19": "test_hint_iff_some_pytest_command_lacks_n"}

INVOCATIONS = [
    "pytest",
    "pytest -q",
    "python -m pytest tests",
    "python3 -m pytest",
    "uv run pytest -q",
    "uv run --with x pytest",
    "poetry run pytest",
    ".venv/bin/pytest",
]
N_FORMS = ["-n auto", "-n 4", "-n=4", "-nauto", "-n4", "--numprocesses auto", "--numprocesses=4", "-n 0"]
NEAR_MISSES = ["--no-header", "--tb=no", "-p no:xdist", '-k "not n"', "--strict-markers"]
NOT_PYTEST = [
    "npm test",
    "cargo test",
    "ruff check tests/test_pytest_plugin.py",
    "pip install pytest-xdist",
    "uv run mypy",
    "echo pytest-free",
]

# Commands that earn the hint: a pytest invocation with no -n option (near-misses included, chained too).
WITHOUT_N = (
    INVOCATIONS
    + [f"{inv} {opt}" for inv in INVOCATIONS for opt in NEAR_MISSES]
    + ["pytest -q && ruff check .", "uv run pytest -q --tb=no; npm test"]
)
# Commands that do not: pytest already parallelised, in every -n form (near-misses alongside), chained too.
WITH_N = (
    [f"{inv} {n}" for inv in INVOCATIONS for n in N_FORMS]
    + [f"{inv} {opt} {n}" for inv in INVOCATIONS[:3] for opt in NEAR_MISSES for n in N_FORMS[:3]]
    + ["pytest -q -n auto && ruff check .", "uv run pytest -n 4; npm test"]
)


def _kind(command: str) -> str:
    if command in WITHOUT_N:
        return "without"
    if command in WITH_N:
        return "with"
    return "other"


commands = st.one_of(st.sampled_from(WITHOUT_N), st.sampled_from(WITH_N), st.sampled_from(NOT_PYTEST))
verify_lists = st.lists(commands, max_size=6)


@settings(derandomize=True, max_examples=200, database=None, deadline=None)
@given(verify=verify_lists, data=st.data())
def test_hint_iff_some_pytest_command_lacks_n(verify, data):
    from claude_kiro.xdist import has_pytest_without_n, suggestion

    expected = next((c for c in verify if _kind(c) == "without"), None)
    assert has_pytest_without_n(verify) == expected
    hint = suggestion(verify)
    assert (hint is not None) == (expected is not None)
    permuted = data.draw(st.permutations(verify))
    assert (suggestion(permuted) is not None) == (expected is not None)


@pytest.mark.parametrize("command", WITHOUT_N)
def test_each_serial_pytest_form_earns_the_hint(command):
    from claude_kiro.xdist import has_pytest_without_n, suggestion

    assert has_pytest_without_n([command]) == command
    assert suggestion([command]) is not None


@pytest.mark.parametrize("command", WITH_N + NOT_PYTEST)
def test_parallel_or_non_pytest_commands_get_no_hint(command):
    from claude_kiro.xdist import has_pytest_without_n, suggestion

    assert has_pytest_without_n([command]) is None
    assert suggestion([command]) is None


def test_empty_verify_gets_no_hint():
    from claude_kiro.xdist import suggestion

    assert suggestion([]) is None


def _check_command(hint: str) -> str:
    """The hint's one-line check: a backticked command, else the trailing text from the first PYTHONHASHSEED."""
    for quoted in re.findall(r"`([^`]*)`", hint):
        if "PYTHONHASHSEED" in quoted:
            return quoted
    start = hint.find("PYTHONHASHSEED")
    assert start >= 0, hint
    return hint[start:]


@pytest.mark.parametrize("verify,prefix", [(["uv run pytest -q"], "uv run pytest"), (["pytest"], "pytest")])
def test_hint_text_explains_xdist_and_the_stable_id_check(verify, prefix):
    from claude_kiro.xdist import suggestion

    hint = suggestion(verify)
    assert hint is not None
    assert "pytest-xdist" in hint
    assert "-n auto" in hint
    lower = hint.lower()
    assert "unstable" in lower and "ids" in lower
    assert "different tests were collected" in lower
    check = _check_command(hint)
    assert "\n" not in check.strip()
    assert check.count("PYTHONHASHSEED") == 2
    assert "--collect-only" in check
    assert prefix in check
