"""Red team of Story 1.3: an excused failure never turns the gate red, and nothing outside 1.3 is excused.

Written from requirements.md only (1.1 to 1.8 as they bear on 1.3), against the real gate CLI
(`ck gate demo --attribute --json`) on real `attr_project` repos with real pytest. A failure is excused only when
at least one of its attributed tasks is not Done. Every case below tries to push a failure across that line, in
one direction (an excuse it does not deserve) or the other (a red gate it does not deserve).

`attr_project` map: test task 1 owns `tests/test_calc_tc.py` (Verifies 2, 3); impl task 2 Verify selects
tc1_/tc2_; impl task 3 selects tc3_; test task 6 owns `tests/test_more.py` (Verifies 7, 8); tc4 and up are
written to `tests/test_more.py`. Project imports happen only inside helpers: this module always collects.
"""

import textwrap
from pathlib import Path

import unattended_helpers as h

ALL_DONE = {"1": "Done", "2": "Done", "3": "Done", "6": "Done", "7": "Done", "8": "Done"}


def done_except(*open_tasks, **other):
    st = {k: v for k, v in ALL_DONE.items() if k not in open_tasks}
    st.update({k: v for k, v in other.items()})
    return st


def extra_impl(num, verify=None, status="Not Started", verified_by="Task 1"):
    verify_line = f"**Verify:** `{verify}`\n" if verify else ""
    return (f"### Task {num}: Extra {num}\n**Status:** {status}\n**Track:** impl\n**Requirements:** 1.1\n"
            f"**Files:**\n- `extra{num}.py` - extra\n{verify_line}**Verified by:** {verified_by}\n"
            f"**Dependencies:** Task 1\n\n**Acceptance:**\n- [ ] works\n\n---\n\n")


def extra_test_task(num, files, verifies, status="Done"):
    bullets = "".join(f"- `{f}` - tests\n" for f in files)
    return (f"### Task {num}: Extra tests {num}\n**Status:** {status}\n**Track:** test\n**Requirements:** 1.1\n"
            f"**Files:**\n{bullets}**Verifies:** {verifies}\n**Dependencies:** None\n\n"
            f"**Acceptance:**\n- [ ] written\n\n---\n\n")


def add_file(root: Path, rel: str, source: str) -> None:
    """Write a test file into the repo, commit it, and bring `integrate/demo` up to the same commit."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source))
    h.commit_all(root, f"add {rel}")
    h.git(h.integration_worktree(root), "merge", "--ff-only", "-q", "main")


def attributed_gate(root: Path, *extra):
    code, obj, err = h.ck_json(root, "gate", "demo", *extra, "--attribute", "--json")
    assert obj is not None, f"`ck gate demo --attribute --json` printed no JSON (exit {code}): {err[-400:]}"
    assert isinstance(obj.get("failures"), list), f"no failures list in {sorted(obj)}"
    return code, obj


def entry(obj, name):
    hits = [f for f in obj["failures"] if (f.get("node") or "").endswith(f"::{name}")]
    assert len(hits) == 1, f"expected one failure for {name}, got {[f.get('node') for f in obj['failures']]}"
    return hits[0]


def assert_red(code, obj):
    assert obj["ok"] is False and code == 1, f"gate must be red: ok={obj['ok']} exit={code}"


def assert_not_red_for_excused(code, obj):
    assert obj["ok"] is True and code == 0, f"excused failures must not make the gate red: ok={obj['ok']} exit={code}"


# --- near-miss TC tokens -------------------------------------------------------------------------------------


def test_tc4_redteam_token_inside_a_word_is_not_a_tc(tmp_path):
    """`test_xtc3_inword` contains the text `tc3_` but no `tc3_` token: Task 3 (Not Started) cannot excuse it."""
    root = h.attr_project(tmp_path, statuses=done_except("3"),
                          tests={"tests/test_more.py::test_xtc3_inword": "fail"})
    code, obj = attributed_gate(root)
    e = entry(obj, "test_xtc3_inword")
    assert e.get("tc") != "TC-3"
    assert "3" not in (e.get("excusedBy") or [])
    assert e["status"] == "regression", f"owning test task 6 verifies Done tasks 7 and 8: {e}"
    assert_red(code, obj)


def test_tc4_redteam_longer_tc_number_does_not_borrow_a_shorter_one(tmp_path):
    """`tc33_` is TC-33, not TC-3: Task 3 (the only unfinished task) cannot excuse it."""
    root = h.attr_project(tmp_path, statuses=done_except("3"),
                          tests={"tests/test_more.py::test_tc33_big": "fail"})
    code, obj = attributed_gate(root)
    e = entry(obj, "test_tc33_big")
    assert e.get("tc") == "TC-33"
    assert e["status"] == "regression" and "3" not in (e.get("excusedBy") or []), e
    assert_red(code, obj)


def test_tc4_redteam_tc12_is_not_tc1(tmp_path):
    """`tc12_` must not be read as TC-1. Task 2 is Done, tasks 7 and 8 (file owner's Verifies) are not."""
    root = h.attr_project(tmp_path, statuses=done_except("7", "8"),
                          tests={"tests/test_more.py::test_tc12_x": "fail"})
    code, obj = attributed_gate(root)
    e = entry(obj, "test_tc12_x")
    assert e.get("tc") == "TC-12"
    assert e["tasks"] != ["2"], "tc12_ was attributed to the task that selects tc1_"
    assert e["status"] == "excused" and e["excusedBy"] == ["7", "8"], e
    assert_not_red_for_excused(code, obj)


def test_tc4_redteam_padded_and_uppercase_tokens_are_the_same_tc(tmp_path):
    """`TC03_` and `tc003_` are TC-3: all excused by unfinished Task 3, so the gate stays green."""
    root = h.attr_project(tmp_path, statuses=done_except("3"),
                          tests={"test_TC03_upper": "fail", "test_tc003_padded": "fail", "test_tc3_plain": "fail"})
    code, obj = attributed_gate(root)
    for name in ("test_TC03_upper", "test_tc003_padded", "test_tc3_plain"):
        e = entry(obj, name)
        assert e["tc"] == "TC-3" and e["tasks"] == ["3"] and e["status"] == "excused" and e["excusedBy"] == ["3"], e
    assert_not_red_for_excused(code, obj)


def test_tc4_redteam_token_only_in_param_id_excuses_nothing(tmp_path):
    """A `tc3_` that appears only in the parameter ID is not the test's TC ID."""
    root = h.attr_project(tmp_path, statuses=done_except("3"))
    add_file(root, "tests/test_more.py", '''
        import pytest


        @pytest.mark.parametrize("case", ["tc3_sub"])
        def test_wrapper(case):
            assert False, "boom"
        ''')
    code, obj = attributed_gate(root)
    e = entry(obj, "test_wrapper[tc3_sub]")
    assert e.get("tc") in (None, "") and "3" not in (e.get("excusedBy") or []), e
    assert e["status"] == "regression", e
    assert_red(code, obj)


def test_tc4_redteam_token_only_in_class_name_excuses_nothing(tmp_path):
    root = h.attr_project(tmp_path, statuses=done_except("3"))
    add_file(root, "tests/test_more.py", '''
        class Testtc3_Sub:
            def test_plain(self):
                assert False, "boom"
        ''')
    code, obj = attributed_gate(root)
    e = [f for f in obj["failures"] if (f.get("node") or "").endswith("test_plain")][0]
    assert e.get("tc") in (None, "") and "3" not in (e.get("excusedBy") or []), e
    assert e["status"] == "regression", e
    assert_red(code, obj)


def test_tc4_redteam_token_in_file_or_directory_name_excuses_nothing(tmp_path):
    """The file `tests/test_tc3_file.py` is owned by no task: its plain test is unattributed, however named."""
    root = h.attr_project(tmp_path, statuses=done_except("3"), tests={
        "tests/test_tc3_file.py::test_plain": "fail", "tests/tc3_dir/test_x.py::test_plain": "fail"})
    code, obj = attributed_gate(root)
    assert len(obj["failures"]) == 2
    for e in obj["failures"]:
        assert e["status"] == "unattributed" and e["tasks"] == [] and not e.get("excusedBy"), e
    assert_red(code, obj)


def test_tc4_redteam_message_text_naming_a_tc_excuses_nothing(tmp_path):
    """The crash message of an unowned test names `tc3_` and Task 3: messages are never read for attribution."""
    root = h.attr_project(tmp_path, statuses=done_except("3"))
    add_file(root, "tests/test_orphan.py", '''
        def test_plain():
            """test_tc3_sub is excused by Task 3"""
            assert False, "tests/test_calc_tc.py::test_tc3_sub excused by 3 (tc3_)"
        ''')
    code, obj = attributed_gate(root)
    e = entry(obj, "test_plain")
    assert e["status"] == "unattributed" and e["tasks"] == [] and not e.get("excusedBy"), e
    assert_red(code, obj)


# --- unowned files and unknown TCs ---------------------------------------------------------------------------


def test_tc4_redteam_lookalike_paths_are_not_owned(tmp_path):
    """Owned: `tests/test_calc_tc.py`. A same-named file in a subdirectory, or a longer name, is not owned."""
    root = h.attr_project(tmp_path, statuses={}, tests={
        "tests/sub/test_calc_tc.py::test_plain": "fail", "tests/test_calc_tc2.py::test_plain": "fail",
        "tests/test_more_extra.py::test_plain": "fail"})
    code, obj = attributed_gate(root)
    assert len(obj["failures"]) == 3
    for e in obj["failures"]:
        assert e["status"] == "unattributed" and e["tasks"] == [] and not e.get("excusedBy"), e
    assert_red(code, obj)


def test_tc4_redteam_unknown_tc_in_unowned_file_cannot_be_excused(tmp_path):
    """TC-99 is in no selector and no coverage, and the file has no owner: unattributed, red, even with all open."""
    root = h.attr_project(tmp_path, statuses={}, tests={"tests/test_orphan.py::test_tc99_x": "fail"})
    code, obj = attributed_gate(root)
    e = entry(obj, "test_tc99_x")
    assert e["status"] == "unattributed" and not e.get("excusedBy"), e
    assert_red(code, obj)


def test_tc4_redteam_unowned_import_error_cannot_be_excused(tmp_path):
    root = h.attr_project(tmp_path, statuses={}, tests={"tests/test_orphan_imp.py::test_x": "import-error"})
    code, obj = attributed_gate(root)
    assert obj["failures"], "the import error must be reported"
    for e in obj["failures"]:
        assert e["status"] == "unattributed" and not e.get("excusedBy"), e
    assert_red(code, obj)


def test_tc4_redteam_unowned_file_with_symlink_twin_in_owned_dir(tmp_path):
    """A file in an owned directory is not owned by being near an owned file: only the path decides."""
    root = h.attr_project(tmp_path, statuses=done_except("3"))
    add_file(root, "tests/test_calc_tc_twin.py", '''
        def test_plain():
            assert False
        ''')
    code, obj = attributed_gate(root)
    e = entry(obj, "test_plain")
    assert e["status"] == "unattributed", e
    assert_red(code, obj)


# --- Verify selector forms -----------------------------------------------------------------------------------


def test_tc4_redteam_not_selector_does_not_select(tmp_path):
    """Task 9 (Not Started) Verifies with `-k "not tc3_"`: that is not a selection of tc3 (or of any TC)."""
    task9 = extra_impl("9", verify='python -m pytest -q tests/test_calc_tc.py -k "not tc3_"')
    root = h.attr_project(tmp_path, statuses=done_except(), extra_tasks=task9,
                          tests={"test_tc3_sub": "fail", "test_tc1_add": "fail"})
    code, obj = attributed_gate(root)
    e3, e1 = entry(obj, "test_tc3_sub"), entry(obj, "test_tc1_add")
    assert e3["tasks"] == ["3"] and e3["status"] == "regression", e3
    assert e1["tasks"] == ["2"] and e1["status"] == "regression", e1
    assert_red(code, obj)


def test_tc4_redteam_last_of_two_k_options_wins(tmp_path):
    """pytest keeps the last `-k`: `-k "tc3_" -k "tc1_"` selects tc1 only, so Task 9 never excuses tc3."""
    task9 = extra_impl("9", verify='python -m pytest -q tests/test_calc_tc.py -k "tc3_" -k "tc1_"')
    root = h.attr_project(tmp_path, statuses=done_except(), extra_tasks=task9,
                          tests={"test_tc3_sub": "fail", "test_tc1_add": "pass"})
    code, obj = attributed_gate(root)
    e3 = entry(obj, "test_tc3_sub")
    assert e3["tasks"] == ["3"] and e3["status"] == "regression", e3
    assert_red(code, obj)


def test_tc4_redteam_last_of_two_k_options_still_selects(tmp_path):
    """The converse: `-k "tc1_" -k "tc3_"` selects tc3, so unfinished Task 9 excuses it (with Done Task 3)."""
    task9 = extra_impl("9", verify='python -m pytest -q tests/test_calc_tc.py -k "tc1_" -k "tc3_"')
    root = h.attr_project(tmp_path, statuses=done_except(), extra_tasks=task9, tests={"test_tc3_sub": "fail"})
    code, obj = attributed_gate(root)
    e3 = entry(obj, "test_tc3_sub")
    assert e3["tasks"] == ["3", "9"] and e3["status"] == "excused" and e3["excusedBy"] == ["9"], e3
    assert_not_red_for_excused(code, obj)


def test_tc4_redteam_and_selector_selects_nothing_extra(tmp_path):
    """`-k "tc3_ and tc4_"` accepts no test name built from one TC, so Task 9 selects neither tc3 nor tc4."""
    task9 = extra_impl("9", verify='python -m pytest -q tests -k "tc3_ and tc4_"')
    root = h.attr_project(tmp_path, statuses=done_except(), extra_tasks=task9, tests={"test_tc3_sub": "fail"})
    code, obj = attributed_gate(root)
    e3 = entry(obj, "test_tc3_sub")
    assert e3["tasks"] == ["3"] and e3["status"] == "regression", e3
    assert_red(code, obj)


def test_tc4_redteam_excused_by_lists_only_unfinished_tasks(tmp_path):
    """Tasks 3 (Done) and 9 (Not Started) both select tc3_: only 9 excuses, and `excusedBy` names only 9."""
    task9 = extra_impl("9", verify='python -m pytest -q tests -k "tc3_"')
    root = h.attr_project(tmp_path, statuses=done_except(), extra_tasks=task9, tests={"test_tc3_sub": "fail"})
    code, obj = attributed_gate(root)
    e3 = entry(obj, "test_tc3_sub")
    assert e3["status"] == "excused" and e3["excusedBy"] == ["9"], e3
    assert_not_red_for_excused(code, obj)


def test_tc4_redteam_test_task_coverage_never_added_to_a_verify_selection(tmp_path):
    """tc3 is selected by Task 3's Verify, so test task 1's Verifies (Task 2) is never unioned in: with Task 3
    Done and Task 2 Not Started, the failure is a regression, not excused by Task 2."""
    root = h.attr_project(tmp_path, statuses=done_except("2"), tests={"test_tc3_sub": "fail"})
    code, obj = attributed_gate(root)
    e3 = entry(obj, "test_tc3_sub")
    assert e3["tasks"] == ["3"] and e3["status"] == "regression", e3
    assert_red(code, obj)


# --- test tasks verifying tasks that do not exist ------------------------------------------------------------


def test_tc4_redteam_phantom_verified_task_is_not_an_excuse(tmp_path):
    """Test task 10 owns `tests/test_ghost.py` and Verifies Task 2 (Done) and Task 99 (not in tasks.md). The
    failure is attributed to the tasks that exist; a task that is not in tasks.md is not 'unfinished'."""
    ghost = extra_test_task("10", ["tests/test_ghost.py"], "Task 2, Task 99")
    root = h.attr_project(tmp_path, statuses=done_except(), extra_tasks=ghost,
                          tests={"tests/test_ghost.py::test_plain": "fail"})
    code, obj = attributed_gate(root)
    e = entry(obj, "test_plain")
    assert "99" not in (e.get("excusedBy") or []), e
    assert e["status"] == "regression", e
    assert_red(code, obj)


def test_tc4_redteam_only_phantom_verified_task_excuses_nothing(tmp_path):
    ghost = extra_test_task("10", ["tests/test_ghost.py"], "Task 99")
    root = h.attr_project(tmp_path, statuses=done_except(), extra_tasks=ghost,
                          tests={"tests/test_ghost.py::test_plain": "fail"})
    code, obj = attributed_gate(root)
    e = entry(obj, "test_plain")
    assert "99" not in (e.get("excusedBy") or []), e
    assert e["status"] != "excused", f"a failure was excused by a task that is not in tasks.md: {e}"
    assert_red(code, obj)


# --- unusual status forms ------------------------------------------------------------------------------------


def test_tc4_redteam_not_done_status_forms_are_unfinished(tmp_path):
    """None of these is Done, so each excuses its task's failure: Not Done, Undone, In Progress (Done soon)."""
    forms = ["Not Done", "Undone", "In Progress (Done soon)", "Blocked", "not started"]
    for i, status in enumerate(forms):
        root = h.attr_project(tmp_path / f"p{i}", statuses=done_except("3", **{"3": status}),
                              tests={"test_tc3_sub": "fail"})
        code, obj = attributed_gate(root)
        e3 = entry(obj, "test_tc3_sub")
        assert e3["status"] == "excused" and e3["excusedBy"] == ["3"], (status, e3)
        assert_not_red_for_excused(code, obj)


def test_tc4_redteam_done_status_forms_are_done(tmp_path):
    """Spellings the task parser reads as Done count as Done: nothing is excused, so the failure is red."""
    forms = ["✅ Done", "**Done**", "done", "DONE", "Completed", "Done (merged)"]
    for i, status in enumerate(forms):
        root = h.attr_project(tmp_path / f"p{i}", statuses=done_except("3", **{"3": status}),
                              tests={"test_tc3_sub": "fail"})
        code, obj = attributed_gate(root)
        e3 = entry(obj, "test_tc3_sub")
        assert e3["status"] == "regression" and not e3.get("excusedBy"), (status, e3)
        assert_red(code, obj)


def test_tc4_redteam_missing_status_line_is_unfinished(tmp_path):
    """A task with no Status line is Not Started (the parser's default), so it excuses."""
    root = h.attr_project(tmp_path, statuses=done_except("3"), tests={"test_tc3_sub": "fail"})
    spec = root / "specs" / "demo" / "tasks.md"
    text = spec.read_text().replace("**Status:** Not Started\n**Track:** impl\n**Requirements:** 2.1\n**Files:**\n"
                                    "- `calc.py` - subtract", "**Track:** impl\n**Requirements:** 2.1\n**Files:**\n"
                                    "- `calc.py` - subtract")
    assert text != spec.read_text()
    add_file(root, "specs/demo/tasks.md", text)
    code, obj = attributed_gate(root)
    e3 = entry(obj, "test_tc3_sub")
    assert e3["status"] == "excused" and e3["excusedBy"] == ["3"], e3
    assert_not_red_for_excused(code, obj)


# --- excused beside a real regression, and --task boundaries -------------------------------------------------


def test_tc4_redteam_excused_does_not_hide_or_become_a_regression(tmp_path):
    """One excused failure and one real regression in the same run: red from the regression alone, and the
    excused entry stays excused (the regression does not drag it along, nor it the regression)."""
    root = h.attr_project(tmp_path, statuses=done_except("3"),
                          tests={"test_tc3_sub": "fail", "test_tc1_add": "fail",
                                 "tests/test_orphan.py::test_plain": "fail"})
    code, obj = attributed_gate(root)
    assert entry(obj, "test_tc3_sub")["status"] == "excused"
    assert entry(obj, "test_tc1_add")["status"] == "regression"
    assert entry(obj, "test_plain")["status"] == "unattributed"
    assert_red(code, obj)


def test_tc4_redteam_gating_one_task_does_not_finish_its_neighbours(tmp_path):
    """`--task 3` gates only Task 3 as Done: tc3 is a regression, tc1 is still excused by unfinished Task 2."""
    root = h.attr_project(tmp_path, statuses={"1": "Done"}, tests={"test_tc3_sub": "fail", "test_tc1_add": "fail"})
    code, obj = attributed_gate(root, "--task", "3")
    assert entry(obj, "test_tc3_sub")["status"] == "regression"
    e1 = entry(obj, "test_tc1_add")
    assert e1["status"] == "excused" and e1["excusedBy"] == ["2"], e1
    assert_red(code, obj)


def test_tc4_redteam_gating_task_with_number_prefix_of_another(tmp_path):
    """`--task 3` must not match task 30 or 13 by prefix or substring: add Task 30 (Not Started, selects tc3_
    as well). Gating Task 3 as Done leaves Task 30 unfinished, so tc3 stays excused by 30 alone."""
    task30 = extra_impl("30", verify='python -m pytest -q tests -k "tc3_"')
    root = h.attr_project(tmp_path, statuses=done_except("3"), extra_tasks=task30, tests={"test_tc3_sub": "fail"})
    code, obj = attributed_gate(root, "--task", "3")
    e3 = entry(obj, "test_tc3_sub")
    assert e3["status"] == "excused" and e3["excusedBy"] == ["30"], e3
    assert_not_red_for_excused(code, obj)


# --- the gate's own output fed back --------------------------------------------------------------------------


def test_tc4_redteam_excuse_is_not_remembered_after_the_task_is_done(tmp_path):
    """Run the gate (tc3 excused, green), write Task 3 as Done, run again: the report the first run wrote must
    not carry the excuse over, so the same failing test is now a regression and the gate is red."""
    root = h.attr_project(tmp_path, statuses=done_except("3"), tests={"test_tc3_sub": "fail"})
    code, obj = attributed_gate(root)
    assert entry(obj, "test_tc3_sub")["status"] == "excused"
    assert_not_red_for_excused(code, obj)
    spec = root / "specs" / "demo" / "tasks.md"
    text = spec.read_text()
    assert "**Status:** Not Started" in text
    add_file(root, "specs/demo/tasks.md", text.replace("**Status:** Not Started", "**Status:** Done"))
    code, obj = attributed_gate(root)
    e3 = entry(obj, "test_tc3_sub")
    assert e3["status"] == "regression" and not e3.get("excusedBy"), e3
    assert_red(code, obj)


def test_tc4_redteam_failure_node_ids_fed_back_as_test_names_are_ordinary_names(tmp_path):
    """A test named like the previous run's node ID text (`tests_test_calc_tc_py__test_tc3_sub`) is the TC-3
    test it reads as; one named with the status word (`test_excused_regression`) carries no TC and no excuse."""
    root = h.attr_project(tmp_path, statuses=done_except("3"), tests={"test_tc3_sub": "fail"})
    _, first = attributed_gate(root)
    node = entry(first, "test_tc3_sub")["node"]
    fed = "test_" + node.replace("/", "_").replace(".", "_").replace("::", "__").replace("test_test", "test")
    add_file(root, "tests/test_orphan.py", f'''
        def test_excused_regression():
            assert False


        def {fed}():
            assert False
        ''')
    code, obj = attributed_gate(root)
    e = entry(obj, "test_excused_regression")
    assert e["status"] == "unattributed" and not e.get("excusedBy"), e
    assert_red(code, obj)
