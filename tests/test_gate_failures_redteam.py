"""Red team for Story 3's SHALL NOT clauses, through `ck gate --json`.

3.3: `failures` SHALL never be omitted under `--json` (a passing gate has `[]`).
3.5: an over-long message SHALL never drop its entry (it is truncated and marked).

Written from the requirements only. Every case runs the real `ck gate` CLI on a real git project
(`attr_project`) with real pytest output, and validates each entry against `failure.schema.json`.
Adversarial inputs: multibyte, combining-mark, CJK and NUL messages around the 500-character limit,
messages that are only ANSI escapes or empty, messages that look like pytest's own summary lines or
a JSON `failures` array, 1,000 failing nodes, newlines in names and messages, a verify command that
prints nothing and fails, a gate whose only failing check is a config error, and the gate's own
output printed by a passing test.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

import unattended_helpers as h

LIMIT = 500
HEADER = "import pytest\n\n\n"
VERIFY_S = "python -m pytest -q -s -p no:cacheprovider tests"


def _project(tmp_path: Path, source: str, *, statuses: Optional[Dict[str, str]] = None,
             config: Optional[dict] = None) -> Path:
    root = h.attr_project(tmp_path, statuses=statuses, tests={}, config=config)
    (root / "tests" / "test_red.py").write_text(HEADER + source)
    h.commit_all(root, "red team tests")
    return root


def _gate(root: Path, *args: str) -> Tuple[int, dict]:
    code, obj, err = h.ck_json(root, "gate", "demo", "--json", *args)
    assert isinstance(obj, dict), f"`ck gate demo --json {' '.join(args)}` did not print one JSON object (exit {code}): {err[-400:]}"
    return code, obj


def _failures(obj: dict) -> List[dict]:
    assert "failures" in obj, f"`failures` omitted under --json: keys {sorted(obj)}"
    assert isinstance(obj["failures"], list), f"`failures` is not a list: {obj['failures']!r}"
    for entry in obj["failures"]:
        assert h.failure_errors(entry) == [], (entry, h.failure_errors(entry))
    return obj["failures"]


def _entry(failures: Sequence[dict], name: str) -> dict:
    hits = [f for f in failures if f.get("node") and f["node"].endswith(name)]
    assert len(hits) == 1, f"expected exactly one entry for {name}, got {len(hits)} in {[f.get('node') for f in failures]}"
    return hits[0]


def _msg_test(name: str, message_expr: str) -> str:
    return f"def {name}():\n    assert False, {message_expr}\n"


# --------------------------------------------------------------------------- 3.5 over-long messages

FILLERS = {
    "emoji": "😀",
    "combining": "é",  # e + combining acute: two code points, one grapheme
    "cjk": "世",
    "nul": "\x00",
    "mixed": "é世😀\x00ä",
}


@pytest.mark.parametrize("length", [499, 500, 501, 1500])
@pytest.mark.parametrize("kind", sorted(FILLERS))
def test_tc32_redteam_message_limit_boundary_keeps_the_entry(tmp_path, kind, length):
    filler = FILLERS[kind]
    msg = (filler * (length // len(filler) + 1))[:length]
    root = _project(tmp_path, _msg_test("test_tc2_long", repr(msg)))
    _, obj = _gate(root)
    entry = _entry(_failures(obj), "::test_tc2_long")
    message = entry["message"]
    assert len(message) <= LIMIT, f"message is {len(message)} characters"
    message.encode("utf-8")  # no lone surrogate from a split pair
    json.dumps(entry, ensure_ascii=False).encode("utf-8")
    if length > LIMIT:
        assert entry["truncated"] is True
    if entry["truncated"] is False:
        assert len(message) <= LIMIT


def test_tc32_redteam_message_of_501_ascii_characters_is_marked_truncated(tmp_path):
    root = _project(tmp_path, _msg_test("test_tc2_long", repr("x" * 501)))
    _, obj = _gate(root)
    entry = _entry(_failures(obj), "::test_tc2_long")
    assert entry["truncated"] is True
    assert len(entry["message"]) <= LIMIT


def test_tc32_redteam_long_message_with_newlines_keeps_the_entry(tmp_path):
    msg = "\n".join(f"line {i} " + "y" * 40 for i in range(60))
    root = _project(tmp_path, _msg_test("test_tc2_lines", repr(msg)))
    _, obj = _gate(root)
    entry = _entry(_failures(obj), "::test_tc2_lines")
    assert len(entry["message"]) <= LIMIT
    assert entry["truncated"] is True


def test_tc32_redteam_long_exception_class_name_and_message(tmp_path):
    src = ("class " + "E" * 600 + "(Exception):\n    pass\n\n\n"
           "def test_tc2_longclass():\n    raise " + "E" * 600 + "('z' * 700)\n")
    root = _project(tmp_path, src)
    _, obj = _gate(root)
    entry = _entry(_failures(obj), "::test_tc2_longclass")
    assert len(entry["message"]) <= LIMIT
    assert entry["truncated"] is True


def test_tc32_redteam_long_repr_in_assertion_introspection(tmp_path):
    src = "def test_tc2_introspect():\n    assert list(range(5000)) == list(range(1, 5001))\n"
    root = _project(tmp_path, src)
    _, obj = _gate(root)
    entry = _entry(_failures(obj), "::test_tc2_introspect")
    assert len(entry["message"]) <= LIMIT


def test_tc32_redteam_long_message_in_setup_and_teardown_keeps_entries(tmp_path):
    src = ("@pytest.fixture\ndef bad_setup():\n    raise RuntimeError('s' * 900)\n\n\n"
           "@pytest.fixture\ndef bad_teardown():\n    yield\n    raise RuntimeError('t' * 900)\n\n\n"
           "def test_tc2_setup(bad_setup):\n    pass\n\n\n"
           "def test_tc3_teardown(bad_teardown):\n    pass\n")
    root = _project(tmp_path, src)
    _, obj = _gate(root)
    failures = _failures(obj)
    for name in ("::test_tc2_setup", "::test_tc3_teardown"):
        entry = _entry(failures, name)
        assert len(entry["message"]) <= LIMIT
        assert entry["kind"] == "error"


def test_tc32_redteam_long_module_level_error_keeps_the_entry(tmp_path):
    root = _project(tmp_path, "raise RuntimeError('m' * 2000)\n")
    _, obj = _gate(root)
    failures = _failures(obj)
    assert failures, "a module that fails at collection must produce an entry"
    assert all(len(f["message"]) <= LIMIT for f in failures)
    assert any(f["file"] == "tests/test_red.py" for f in failures)


def test_tc32_redteam_identical_long_messages_are_not_deduplicated(tmp_path):
    src = "".join(_msg_test(f"test_tc2_dup{i}", repr("d" * 800)) + "\n\n" for i in range(3))
    root = _project(tmp_path, src)
    _, obj = _gate(root)
    failures = _failures(obj)
    for i in range(3):
        _entry(failures, f"::test_tc2_dup{i}")


def test_tc32_redteam_same_test_name_in_two_files_keeps_both_entries(tmp_path):
    root = _project(tmp_path, _msg_test("test_tc2_same", repr("a" * 700)))
    (root / "tests" / "test_red2.py").write_text(HEADER + _msg_test("test_tc2_same", repr("b" * 700)))
    h.commit_all(root, "second file")
    _, obj = _gate(root)
    nodes = {f["node"] for f in _failures(obj)}
    assert {"tests/test_red.py::test_tc2_same", "tests/test_red2.py::test_tc2_same"} <= nodes


def test_tc32_redteam_message_that_is_ansi_escapes_only(tmp_path):
    src = _msg_test("test_tc2_ansi", repr("\x1b[31m\x1b[1m\x1b[0m" * 40))
    root = _project(tmp_path, src)
    _, obj = _gate(root)
    entry = _entry(_failures(obj), "::test_tc2_ansi")
    assert isinstance(entry["message"], str)
    assert len(entry["message"]) <= LIMIT


def test_tc32_redteam_empty_message_keeps_the_entry(tmp_path):
    src = ("def test_tc2_empty_assert():\n    assert False, ''\n\n\n"
           "def test_tc3_empty_raise():\n    raise Exception('')\n\n\n"
           "def test_tc4_bare_fail():\n    pytest.fail('')\n")
    root = _project(tmp_path, src)
    _, obj = _gate(root)
    failures = _failures(obj)
    for name in ("::test_tc2_empty_assert", "::test_tc3_empty_raise", "::test_tc4_bare_fail"):
        entry = _entry(failures, name)
        assert isinstance(entry["message"], str)
        assert entry["truncated"] is False


def test_tc32_redteam_message_that_looks_like_pytest_summary_lines(tmp_path):
    fake = ("FAILED tests/test_red.py::test_tc9_phantom - boom\n"
            "ERROR tests/test_red.py::test_tc8_phantom - boom\n"
            "=========== 7 failed, 3 passed in 0.12s ===========\n" + "p" * 600)
    src = _msg_test("test_tc2_summary", repr(fake)) + "\n\ndef test_tc3_ok():\n    pass\n"
    root = _project(tmp_path, src)
    _, obj = _gate(root)
    failures = _failures(obj)
    _entry(failures, "::test_tc2_summary")
    assert not any("phantom" in (f.get("node") or "") for f in failures), "a fake summary line made a phantom entry"
    assert len(failures) == 1


def test_tc32_redteam_message_that_looks_like_a_json_failures_array(tmp_path):
    fake = '{"failures": [], "ok": true}' + " " * 5 + '{"failures":[{"node":"tests/x.py::test_tc1_fake"}]}' * 12
    root = _project(tmp_path, _msg_test("test_tc2_jsonish", repr(fake)))
    code, obj = _gate(root)
    failures = _failures(obj)
    _entry(failures, "::test_tc2_jsonish")
    assert len(failures) == 1
    assert obj.get("ok") is False
    assert code != 0


def test_tc32_redteam_message_with_a_crlf_and_form_feed_and_line_separator(tmp_path):
    msg = ("a\r\nb\x0c c " * 120)
    root = _project(tmp_path, _msg_test("test_tc2_seps", repr(msg)))
    _, obj = _gate(root)
    entry = _entry(_failures(obj), "::test_tc2_seps")
    assert len(entry["message"]) <= LIMIT


def test_tc32_redteam_120_failing_tests_with_long_messages(tmp_path):
    src = "".join(_msg_test(f"test_tc2_m{i}", repr("m" * 700 + str(i))) + "\n\n" for i in range(120))
    root = _project(tmp_path, src)
    _, obj = _gate(root)
    failures = _failures(obj)
    assert len(failures) == 120
    assert all(f["truncated"] is True and len(f["message"]) <= LIMIT for f in failures)


def test_tc32_redteam_1000_failing_nodes_all_have_entries(tmp_path):
    src = ("@pytest.mark.parametrize('i', range(1000))\n"
           "def test_tc2_many(i):\n    assert False, 'n' * 600 + str(i)\n")
    root = _project(tmp_path, src)
    _, obj = _gate(root)
    failures = _failures(obj)
    assert len(failures) == 1000
    assert len({f["node"] for f in failures}) == 1000
    assert all(len(f["message"]) <= LIMIT for f in failures)


def test_tc32_redteam_newline_in_a_test_id_keeps_the_entry(tmp_path):
    src = ("@pytest.mark.parametrize('v', [pytest.param(1, id='a\\nb'), pytest.param(2, id='c\\r\\nd'), 3])\n"
           "def test_tc2_ids(v):\n    assert False, 'q' * 700\n")
    root = _project(tmp_path, src)
    _, obj = _gate(root)
    failures = _failures(obj)
    assert len(failures) == 3
    assert all(f["node"] and len(f["message"]) <= LIMIT for f in failures)


def test_tc32_redteam_non_ascii_test_id_and_file_name(tmp_path):
    src = ("@pytest.mark.parametrize('v', ['世界', '😀', 'e\\u0301'])\n"
           "def test_tc2_uni(v):\n    assert False, v * 400\n")
    root = _project(tmp_path, src)
    (root / "tests" / "test_é世.py").write_text(HEADER + _msg_test("test_tc3_uni_file", repr("z" * 900)))
    h.commit_all(root, "unicode file")
    _, obj = _gate(root)
    failures = _failures(obj)
    assert len(failures) == 4
    assert any(f["file"] == "tests/test_é世.py" for f in failures)


def test_tc32_redteam_one_node_failing_in_call_and_teardown_is_not_dropped(tmp_path):
    src = ("@pytest.fixture\ndef both():\n    yield\n    raise RuntimeError('teardown ' + 'w' * 700)\n\n\n"
           "def test_tc2_both(both):\n    assert False, 'call ' + 'c' * 700\n")
    root = _project(tmp_path, src)
    _, obj = _gate(root)
    failures = _failures(obj)
    hits = [f for f in failures if (f["node"] or "").endswith("::test_tc2_both")]
    assert len(hits) == 1, "one entry per failing test node, even when it fails in two phases"
    assert len(hits[0]["message"]) <= LIMIT


def test_tc32_redteam_long_message_under_attribute_keeps_the_entry(tmp_path):
    root = _project(tmp_path, _msg_test("test_tc2_attr", repr("a" * 900)), statuses={"2": "Done"})
    _, obj = _gate(root, "--attribute")
    entry = _entry(_failures(obj), "::test_tc2_attr")
    assert len(entry["message"]) <= LIMIT
    assert entry["truncated"] is True
    assert entry["status"] in ("regression", "excused", "flaky", "unattributed")


def test_tc32_redteam_long_message_under_task_scope_keeps_the_entry(tmp_path):
    src = _msg_test("test_tc1_scoped", repr("s" * 900))
    root = _project(tmp_path, src, statuses={"1": "Done"})
    _, obj = _gate(root, "--task", "1")
    failures = _failures(obj)
    assert any((f["node"] or "").endswith("::test_tc1_scoped") for f in failures)
    assert all(len(f["message"]) <= LIMIT for f in failures)


# --------------------------------------------------------------------------- 3.3 never omitted


def test_tc30_redteam_passing_gate_has_an_empty_failures_list(tmp_path):
    root = _project(tmp_path, "def test_tc2_ok():\n    assert True\n")
    code, obj = _gate(root)
    assert _failures(obj) == []
    assert obj.get("ok") is True
    assert code == 0


@pytest.mark.parametrize("args", [(), ("--attribute",), ("--no-reuse",)])
def test_tc30_redteam_passing_gate_has_failures_under_every_flag(tmp_path, args):
    root = _project(tmp_path, "def test_tc2_ok():\n    assert True\n")
    _, obj = _gate(root, *args)
    assert _failures(obj) == []


def test_tc30_redteam_passing_task_scoped_gate_has_failures(tmp_path):
    root = _project(tmp_path, "def test_tc2_ok():\n    assert True\n", statuses={"1": "Done"})
    _, obj = _gate(root, "--task", "1")
    assert _failures(obj) == []


def test_tc30_redteam_passing_test_that_prints_summary_lines_and_failures_json(tmp_path):
    src = ("def test_tc2_noisy():\n"
           "    print('FAILED tests/test_red.py::test_tc9_x - boom')\n"
           "    print('=== 3 failed, 1 passed in 0.1s ===')\n"
           "    print('{\"failures\": [{\"node\": \"tests/x.py::test_tc1_fake\", \"kind\": \"error\"}], \"ok\": false}')\n"
           "    import sys\n    print('E   AssertionError: not real', file=sys.stderr)\n")
    root = _project(tmp_path, src)
    code, obj = _gate(root)
    assert _failures(obj) == []
    assert obj.get("ok") is True
    assert code == 0


def test_tc30_redteam_passing_test_that_prints_a_gate_report_of_its_own(tmp_path):
    inner = json.dumps({"schema": "ck-gate/1", "ok": False, "failures": [
        {"node": "t.py::test_tc1_x", "tcId": "TC-1", "tasks": ["1"], "attributedBy": "tc", "file": "t.py",
         "kind": "assertion", "message": "x", "truncated": False, "command": "pytest"}]})
    src = f"def test_tc2_echo():\n    print({inner!r})\n"
    root = _project(tmp_path, src)
    _, obj = _gate(root)
    assert _failures(obj) == []


def test_tc30_redteam_skips_xfail_and_warnings_are_not_failures(tmp_path):
    src = ("import warnings\n\n\n"
           "@pytest.mark.skip(reason='r')\ndef test_tc2_skip():\n    assert False\n\n\n"
           "@pytest.mark.xfail(reason='r')\ndef test_tc3_xfail():\n    assert False\n\n\n"
           "def test_tc4_warn():\n    warnings.warn('w' * 800)\n")
    root = _project(tmp_path, src)
    code, obj = _gate(root)
    assert _failures(obj) == []
    assert code == 0


def test_tc30_redteam_unchecked_acceptance_box_has_empty_failures_and_ok_false(tmp_path):
    root = _project(tmp_path, "def test_tc2_ok():\n    assert True\n", statuses={"1": "Done"})
    tasks = root / "specs" / "demo" / "tasks.md"
    text = tasks.read_text()
    assert "- [x]" in text or "- [ ]" in text
    tasks.write_text(text.replace("- [x]", "- [ ]", 1))
    h.commit_all(root, "uncheck a box")
    code, obj = _gate(root)
    assert _failures(obj) == []
    assert obj.get("ok") is False
    assert code != 0


def test_tc30_redteam_verify_command_that_prints_nothing_and_exits_1(tmp_path):
    root = _project(tmp_path, "def test_tc2_ok():\n    assert True\n",
                    config={"verify": ["python -c 'import sys; sys.exit(1)'"]})
    code, obj = _gate(root)
    _failures(obj)
    assert obj.get("ok") is False
    assert code != 0


def test_tc30_redteam_verify_command_that_does_not_exist(tmp_path):
    root = _project(tmp_path, "def test_tc2_ok():\n    assert True\n",
                    config={"verify": ["definitely-not-a-command-xyz --flag"]})
    code, obj = _gate(root)
    _failures(obj)
    assert obj.get("ok") is False
    assert code != 0


def test_tc30_redteam_verify_command_with_binary_garbage_output(tmp_path):
    cmd = "python -c \"import sys; sys.stdout.buffer.write(bytes(range(256)) * 50); sys.exit(1)\""
    root = _project(tmp_path, "def test_tc2_ok():\n    assert True\n", config={"verify": [cmd]})
    code, obj = _gate(root)
    for entry in _failures(obj):
        assert len(entry["message"]) <= LIMIT
    assert obj.get("ok") is False


def test_tc30_redteam_non_pytest_verify_printing_a_failures_array(tmp_path):
    cmd = "python -c \"print('{\\\"failures\\\": []}'); print('1 passed'); raise SystemExit(1)\""
    root = _project(tmp_path, "def test_tc2_ok():\n    assert True\n", config={"verify": [cmd]})
    code, obj = _gate(root)
    _failures(obj)
    assert obj.get("ok") is False
    assert code != 0


@pytest.mark.parametrize("config_text", [
    "{ not json",
    '{"verify": "python -m pytest"}',
    '{"verify": [1, 2]}',
    '{"verify": null}',
    "[]",
    "",
])
def test_tc30_redteam_config_error_only_still_has_failures(tmp_path, config_text):
    root = _project(tmp_path, "def test_tc2_ok():\n    assert True\n")
    (root / "specs" / "ck.json").write_text(config_text)
    h.commit_all(root, "bad config")
    code, obj = _gate(root)
    assert _failures(obj) == [] or all(h.failure_errors(f) == [] for f in obj["failures"])
    assert obj.get("ok") is False
    assert code != 0


def test_tc30_redteam_only_failing_check_is_a_missing_test_file(tmp_path):
    root = _project(tmp_path, "def test_tc2_ok():\n    assert True\n", statuses={"1": "Done"})
    (root / "tests" / "test_calc_tc.py").unlink(missing_ok=True)
    h.commit_all(root, "remove test file")
    code, obj = _gate(root)
    _failures(obj)
    assert code in (0, 1)
    assert "failures" in obj


def test_tc30_redteam_gate_run_twice_keeps_the_field(tmp_path):
    root = _project(tmp_path, "def test_tc2_ok():\n    assert True\n", statuses={"1": "Done"})
    for _ in range(2):
        _, obj = _gate(root, "--task", "1")
        assert _failures(obj) == []


def test_tc30_redteam_json_stays_one_document_when_tests_print_to_stdout(tmp_path):
    src = ("def test_tc2_loud():\n    print('x' * 100000)\n    print('}{][\"failures\"')\n"
           "    assert False, 'loud'\n")
    root = _project(tmp_path, src)
    code, obj, _ = h.ck_json(root, "gate", "demo", "--json")
    assert isinstance(obj, dict), "stdout under --json must be exactly one JSON document"
    failures = _failures(obj)
    _entry(failures, "::test_tc2_loud")
