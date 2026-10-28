"""Red-team of Story 2 (2.2, 2.3): the collector writes and prints nothing about a non-ck workflow.

Every case runs the real `ck stats collect` CLI over a real temp Claude home and store. A foreign run carries a unique
marker in its name, id, args, result and agent labels; after each CLI call the store, the collect log, any `ck run`
logs, stdout and stderr are scanned for those markers. The one allowed echo is a run id typed in `--run`.
"""
import json
from pathlib import Path

import pytest

from stats_helpers import AgentSpec, FakeClaudeHome, fresh_env, all_bytes, store_files, stats

FOREIGN_ID = "wf_redteam-foreign-0001"
CK_ID = "wf_redteam-ck-0001"
PAYLOAD = "REDTEAM-PAYLOAD-7f3a"  # planted in args, result and labels of every foreign run

# Names that are near-misses of the allowlist (spec-implement, spec-implement-iso, spec-create).
NEAR_MISS_NAMES = {
    "trailing-space": "spec-implement ",
    "trailing-newline": "spec-implement\n",
    "trailing-nul": "spec-implement\x00",
    "leading-bom": "﻿spec-implement",
    "zero-width-space": "spec-implement​",
    "zero-width-joiner-create": "spec-create‍",
    "rtl-override": "spec-implement‮",
    "upper-case": "Spec-Implement",
    "upper-case-create": "SPEC-CREATE",
    "iso-suffix-extra": "spec-implement-iso2",
    "underscore": "spec_implement",
    "leading-space": " spec-create",
    "trailing-slash": "spec-create/",
    "cyrillic-i": "spec-іmplement",
    "colon-prefix": "spec:spec-implement",
    "prefix-only": "spec-",
    "empty": "",
}

NON_STRING_NAMES = {
    "null": None,
    "int": 123,
    "bool": True,
    "list-of-allowlisted": ["spec-implement"],
    "dict-with-allowlisted": {"name": "spec-implement"},
    "float": 1.5,
}


def _home(env):
    home = FakeClaudeHome(env.claude)
    return home, home.session()


def _foreign_kwargs(name_label: str = "scan " + PAYLOAD) -> dict:
    return dict(args={"topic": PAYLOAD}, result={"summary": PAYLOAD}, agents=[AgentSpec(name_label)])


ALLOWLISTED = ("spec-implement", "spec-implement-iso", "spec-create")


def _forbidden_name(name: str) -> list:
    """The name must stay out of every byte, unless it is (a substring of) an allowlisted name a ck run legitimately stores."""
    core = name.strip("\x00\n ​‍‮﻿/")
    if core == "" or any(core in a for a in ALLOWLISTED):
        return []
    return [name]


def _scan(env, results, *, forbidden=(), allow_id_echo=()):
    """Nothing forbidden in the store, the log, any ck-runs log, or the outputs of `results`.

    The foreign run id may be echoed on stdout/stderr only when typed in `--run` (`allow_id_echo`); it never reaches the store.
    """
    blobs = [all_bytes(env.xdg)]
    blobs += [r.stdout.encode("utf-8", "surrogateescape") + r.stderr.encode("utf-8", "surrogateescape") for r in results]
    data = b"".join(blobs)
    for bad in (PAYLOAD, *forbidden):
        assert bad.encode("utf-8", "surrogateescape") not in data, f"{bad!r} leaked"
    assert FOREIGN_ID.encode() not in blobs[0], "foreign run id leaked into the store or logs"
    if FOREIGN_ID not in allow_id_echo:
        assert FOREIGN_ID.encode() not in b"".join(blobs[1:]), "foreign run id leaked to the console"


def _stored_run_ids(env):
    return sorted(p.stem for p in store_files(env.xdg) if p.parent.name == "runs")


# --------------------------------------------------------------------------- 2.2 --all with near-miss names


@pytest.mark.parametrize("trick", sorted(NEAR_MISS_NAMES))
def test_all_ignores_near_miss_name(trick):
    name = NEAR_MISS_NAMES[trick]
    with fresh_env() as env:
        _, s = _home(env)
        s.implement_run(CK_ID)
        s.foreign_run(FOREIGN_ID, name=name, **_foreign_kwargs())
        r = stats("collect", "--all", env=env)
        assert r.exit_code == 0, r.output
        assert _stored_run_ids(env) == [CK_ID]
        _scan(env, [r], forbidden=_forbidden_name(name))


@pytest.mark.parametrize("trick", sorted(NON_STRING_NAMES))
def test_all_ignores_non_string_workflow_name(trick):
    with fresh_env() as env:
        _, s = _home(env)
        s.implement_run(CK_ID)
        s.foreign_run(FOREIGN_ID, name="placeholder", **_foreign_kwargs())
        s_run = s.runs[-1]
        s_run.set(workflowName=NON_STRING_NAMES[trick])
        r = stats("collect", "--all", env=env)
        assert r.exit_code == 0, r.output
        assert _stored_run_ids(env) == [CK_ID]
        _scan(env, [r])


def test_all_ignores_run_with_missing_workflow_name():
    with fresh_env() as env:
        _, s = _home(env)
        s.implement_run(CK_ID)
        s.foreign_run(FOREIGN_ID, name="placeholder", **_foreign_kwargs())
        p = s.runs[-1].path
        d = json.loads(p.read_text())
        del d["workflowName"]
        p.write_text(json.dumps(d))
        r = stats("collect", "--all", env=env)
        assert r.exit_code == 0, r.output
        assert _stored_run_ids(env) == [CK_ID]
        _scan(env, [r])


def test_all_ignores_foreign_with_nested_allowlisted_name_in_args():
    with fresh_env() as env:
        _, s = _home(env)
        s.implement_run(CK_ID)
        s.foreign_run(FOREIGN_ID, name="competitor-teardown-q4", args={"workflowName": "spec-implement", "p": PAYLOAD},
                      result={"workflowName": "spec-create", "p": PAYLOAD}, agents=[AgentSpec("scan " + PAYLOAD)])
        r = stats("collect", "--all", env=env)
        assert r.exit_code == 0, r.output
        assert _stored_run_ids(env) == [CK_ID]
        _scan(env, [r], forbidden=["competitor-teardown-q4"])


def test_all_ignores_foreign_whose_transcripts_claim_allowlisted_phase_and_label():
    with fresh_env() as env:
        _, s = _home(env)
        s.implement_run(CK_ID)
        s.foreign_run(FOREIGN_ID, name="competitor-teardown-q4", args={"p": PAYLOAD}, result={"p": PAYLOAD},
                      agents=[AgentSpec("task 1", phase="Wave 1"), AgentSpec("review 1", phase="Wave 1"), AgentSpec("land 1", phase="Wave 1")])
        r = stats("collect", "--all", env=env)
        assert r.exit_code == 0, r.output
        assert _stored_run_ids(env) == [CK_ID]
        _scan(env, [r], forbidden=["competitor-teardown-q4"])


def test_all_ignores_foreign_whose_launch_claims_allowlisted_name():
    """The main transcript's Workflow launch says spec-implement, the run file says otherwise: the run file decides."""
    with fresh_env() as env:
        _, s = _home(env)
        s.implement_run(CK_ID)
        s.foreign_run(FOREIGN_ID, name="competitor-teardown-q4", launch={"name": "spec:spec-implement"}, **_foreign_kwargs())
        r = stats("collect", "--all", env=env)
        assert r.exit_code == 0, r.output
        assert _stored_run_ids(env) == [CK_ID]
        _scan(env, [r], forbidden=["competitor-teardown-q4"])


def test_all_foreign_name_substring_of_ck_agent_label_is_still_ignored():
    with fresh_env() as env:
        _, s = _home(env)
        s.implement_run(CK_ID)
        s.foreign_run(FOREIGN_ID, name="task", **_foreign_kwargs())
        r = stats("collect", "--all", env=env)
        assert r.exit_code == 0, r.output
        assert _stored_run_ids(env) == [CK_ID]
        _scan(env, [r])


# --------------------------------------------------------------------------- 2.2 ids and sessions


def test_foreign_run_sharing_id_with_ck_run_in_another_session_leaks_nothing():
    with fresh_env() as env:
        home = FakeClaudeHome(env.claude)
        a = home.session(slug="-work-a")
        b = home.session(slug="-work-b")
        a.implement_run(CK_ID)
        b.foreign_run(CK_ID, name="competitor-teardown-q4", **_foreign_kwargs())
        r = stats("collect", "--all", env=env)
        assert r.exit_code == 0, r.output
        assert _stored_run_ids(env) == [CK_ID]
        _scan(env, [r], forbidden=["competitor-teardown-q4"])
        r2 = stats("collect", "--run", CK_ID, env=env)
        _scan(env, [r, r2], forbidden=["competitor-teardown-q4"])


def test_foreign_run_file_overwriting_a_ck_run_file_in_the_same_session_leaks_nothing():
    """Same id, same session: the foreign run file replaces the ck one (one path), so no run is allowlisted any more."""
    with fresh_env() as env:
        _, s = _home(env)
        s.implement_run(CK_ID)
        s.foreign_run(CK_ID, name="competitor-teardown-q4", **_foreign_kwargs())
        r = stats("collect", "--all", env=env)
        assert r.exit_code == 0, r.output
        assert _stored_run_ids(env) == []
        _scan(env, [r], forbidden=["competitor-teardown-q4"])


def test_all_twice_feeding_store_back_stays_clean():
    with fresh_env() as env:
        _, s = _home(env)
        s.implement_run(CK_ID)
        s.foreign_run(FOREIGN_ID, name="competitor-teardown-q4", **_foreign_kwargs())
        r1 = stats("collect", "--all", env=env)
        r2 = stats("collect", "--all", env=env)
        assert r1.exit_code == 0 and r2.exit_code == 0
        assert _stored_run_ids(env) == [CK_ID]
        _scan(env, [r1, r2], forbidden=["competitor-teardown-q4"])


def test_all_skipped_foreign_runs_are_only_counted():
    with fresh_env() as env:
        _, s = _home(env)
        s.implement_run(CK_ID)
        for i in range(3):
            s.foreign_run(f"wf_redteam-foreign-{i:04d}", name=f"tax-review-{i}", **_foreign_kwargs())
        r = stats("collect", "--all", env=env)
        assert r.exit_code == 0, r.output
        for i in range(3):
            assert f"wf_redteam-foreign-{i:04d}".encode() not in all_bytes(env.xdg) + r.output.encode()
            assert f"tax-review-{i}" not in r.output
        _scan(env, [r])


# --------------------------------------------------------------------------- 2.2 corrupt runs in --all


def test_all_corrupt_foreign_next_to_corrupt_ck_names_only_the_ck_run():
    with fresh_env() as env:
        _, s = _home(env)
        s.implement_run(CK_ID).corrupt_format()
        s.foreign_run(FOREIGN_ID, name="competitor-teardown-q4", **_foreign_kwargs())
        s.runs[-1].set(workflowProgress={"not": "a list"}, result=["garbage", PAYLOAD])
        r = stats("collect", "--all", env=env)
        assert CK_ID in r.output  # the failed ck run is named (4.2)
        _scan(env, [r], forbidden=["competitor-teardown-q4"])


def test_all_unparseable_foreign_run_file_names_nothing():
    with fresh_env() as env:
        _, s = _home(env)
        s.implement_run(CK_ID)
        s.foreign_run(FOREIGN_ID, name="competitor-teardown-q4", **_foreign_kwargs())
        s.runs[-1].path.write_text('{"workflowName": "competitor-teardown-q4", "runId": "' + FOREIGN_ID + '", "x": ' + PAYLOAD)
        r = stats("collect", "--all", env=env)
        _scan(env, [r], forbidden=["competitor-teardown-q4"])


def test_all_empty_and_binary_foreign_run_files_name_nothing():
    with fresh_env() as env:
        _, s = _home(env)
        s.implement_run(CK_ID)
        d = s.dir / "workflows"
        (d / (FOREIGN_ID + ".json")).write_bytes(b"")
        (d / "wf_redteam-binary.json").write_bytes(b"\xff\xfe\x00" + PAYLOAD.encode())
        r = stats("collect", "--all", env=env)
        assert "wf_redteam-binary" not in r.output
        _scan(env, [r])


def test_all_foreign_run_in_unreadable_transcript_dir_reports_no_error_naming_it():
    with fresh_env() as env:
        _, s = _home(env)
        s.implement_run(CK_ID)
        f = s.foreign_run(FOREIGN_ID, name="competitor-teardown-q4", **_foreign_kwargs())
        f.agents_dir.chmod(0)
        try:
            r = stats("collect", "--all", env=env)
        finally:
            f.agents_dir.chmod(0o755)
        assert r.exit_code == 0, r.output
        _scan(env, [r], forbidden=["competitor-teardown-q4"])


# --------------------------------------------------------------------------- 2.3 --run refusal


@pytest.mark.parametrize("trick", sorted(NEAR_MISS_NAMES))
def test_run_refuses_near_miss_name_without_naming_it(trick):
    name = NEAR_MISS_NAMES[trick]
    with fresh_env() as env:
        _, s = _home(env)
        s.foreign_run(FOREIGN_ID, name=name, **_foreign_kwargs())
        r = stats("collect", "--run", FOREIGN_ID, env=env)
        assert r.exit_code != 0, r.output
        assert store_files(env.xdg) == [] or _stored_run_ids(env) == []
        _scan(env, [r], allow_id_echo=[FOREIGN_ID], forbidden=_forbidden_name(name))


@pytest.mark.parametrize("trick", sorted(NON_STRING_NAMES))
def test_run_refuses_non_string_name(trick):
    with fresh_env() as env:
        _, s = _home(env)
        s.foreign_run(FOREIGN_ID, name="placeholder", **_foreign_kwargs()).set(workflowName=NON_STRING_NAMES[trick])
        r = stats("collect", "--run", FOREIGN_ID, env=env)
        assert r.exit_code != 0, r.output
        assert _stored_run_ids(env) == []
        _scan(env, [r], allow_id_echo=[FOREIGN_ID])


def test_run_refuses_foreign_with_nested_allowlisted_name():
    with fresh_env() as env:
        _, s = _home(env)
        s.foreign_run(FOREIGN_ID, name="competitor-teardown-q4", args={"workflowName": "spec-implement", "p": PAYLOAD},
                      result={"p": PAYLOAD}, agents=[AgentSpec("scan " + PAYLOAD)])
        r = stats("collect", "--run", FOREIGN_ID, env=env)
        assert r.exit_code != 0, r.output
        assert _stored_run_ids(env) == []
        _scan(env, [r], allow_id_echo=[FOREIGN_ID], forbidden=["competitor-teardown-q4"])


def test_run_refuses_foreign_whose_launch_claims_allowlisted_name():
    with fresh_env() as env:
        _, s = _home(env)
        s.foreign_run(FOREIGN_ID, name="competitor-teardown-q4", launch={"name": "spec:spec-implement"}, **_foreign_kwargs())
        r = stats("collect", "--run", FOREIGN_ID, env=env)
        assert r.exit_code != 0, r.output
        assert _stored_run_ids(env) == []
        _scan(env, [r], allow_id_echo=[FOREIGN_ID], forbidden=["competitor-teardown-q4"])


def test_run_refuses_foreign_with_dropped_run_file_but_launch_naming_foreign_workflow():
    """1.15 recovery path: the recovered name is not allowlisted, so not found and the name is never printed."""
    with fresh_env() as env:
        _, s = _home(env)
        f = s.foreign_run(FOREIGN_ID, name="competitor-teardown-q4", **_foreign_kwargs())
        f.drop_run_file()
        r = stats("collect", "--run", FOREIGN_ID, env=env)
        assert r.exit_code != 0, r.output
        assert _stored_run_ids(env) == []
        _scan(env, [r], allow_id_echo=[FOREIGN_ID], forbidden=["competitor-teardown-q4"])


def test_run_refuses_foreign_with_dropped_run_file_when_transcript_meta_claims_allowlisted_phase():
    with fresh_env() as env:
        _, s = _home(env)
        f = s.foreign_run(FOREIGN_ID, name="competitor-teardown-q4", args={"p": PAYLOAD}, result={"p": PAYLOAD},
                          agents=[AgentSpec("task 1", phase="Wave 1")])
        f.drop_run_file()
        r = stats("collect", "--run", FOREIGN_ID, env=env)
        assert r.exit_code != 0, r.output
        assert _stored_run_ids(env) == []
        _scan(env, [r], allow_id_echo=[FOREIGN_ID], forbidden=["competitor-teardown-q4"])


def test_run_dropped_run_file_launch_with_allowlisted_name_in_other_field_is_not_trusted():
    """Launch input names a foreign workflow but mentions spec-implement in a prompt field."""
    with fresh_env() as env:
        _, s = _home(env)
        f = s.foreign_run(FOREIGN_ID, name="competitor-teardown-q4", launch={"name": "competitor-teardown-q4", "args": {"workflowName": "spec-implement"}},
                          **_foreign_kwargs())
        f.drop_run_file()
        r = stats("collect", "--run", FOREIGN_ID, env=env)
        assert r.exit_code != 0, r.output
        assert _stored_run_ids(env) == []
        _scan(env, [r], allow_id_echo=[FOREIGN_ID], forbidden=["competitor-teardown-q4"])


def test_run_refuses_foreign_with_unparseable_run_file_without_naming_it():
    with fresh_env() as env:
        _, s = _home(env)
        s.foreign_run(FOREIGN_ID, name="competitor-teardown-q4", **_foreign_kwargs())
        s.runs[-1].path.write_text('{"workflowName": "competitor-teardown-q4", "x": ' + PAYLOAD)
        r = stats("collect", "--run", FOREIGN_ID, env=env)
        assert r.exit_code != 0, r.output
        assert _stored_run_ids(env) == []
        _scan(env, [r], allow_id_echo=[FOREIGN_ID], forbidden=["competitor-teardown-q4"])


def test_run_dropped_run_file_is_not_tied_to_a_ck_launch_whose_run_id_merely_contains_it():
    """1.15: the launch whose result names the id. Run id `<foreign>2` of a ck launch must not name the foreign run."""
    with fresh_env() as env:
        _, s = _home(env)
        s.implement_run(FOREIGN_ID + "2")
        f = s.foreign_run(FOREIGN_ID, name="competitor-teardown-q4", launch=False, **_foreign_kwargs())
        f.drop_run_file()
        r = stats("collect", "--run", FOREIGN_ID, env=env)
        assert r.exit_code != 0, r.output
        assert _stored_run_ids(env) == []
        _scan(env, [r], allow_id_echo=[FOREIGN_ID], forbidden=["competitor-teardown-q4"])


def test_run_with_id_shared_across_sessions_never_stores_the_foreign_one():
    with fresh_env() as env:
        home = FakeClaudeHome(env.claude)
        a = home.session(slug="-work-a")
        b = home.session(slug="-work-b")
        b.foreign_run(CK_ID, name="competitor-teardown-q4", **_foreign_kwargs())
        a.implement_run(CK_ID)
        r = stats("collect", "--run", CK_ID, env=env)
        _scan(env, [r], forbidden=["competitor-teardown-q4"])


def test_run_refusal_message_is_identical_for_two_different_foreign_names():
    """The refusal carries nothing derived from the name (same text up to the typed id)."""
    outs = []
    for name in ("competitor-teardown-q4", "tax-review-2026"):
        with fresh_env() as env:
            _, s = _home(env)
            s.foreign_run(FOREIGN_ID, name=name, **_foreign_kwargs())
            r = stats("collect", "--run", FOREIGN_ID, env=env)
            assert r.exit_code != 0
            outs.append((r.stdout, r.stderr.replace(str(env.claude), "<home>")))
    assert outs[0] == outs[1]


# --------------------------------------------------------------------------- 2.2 / 2.3 --auto


@pytest.mark.parametrize("trick", ["competitor-teardown-q4", "spec-implement ", "spec-implement​", "Spec-Create"])
def test_auto_on_foreign_run_exits_zero_and_names_nothing(trick):
    with fresh_env() as env:
        _, s = _home(env)
        s.foreign_run(FOREIGN_ID, name=trick, **_foreign_kwargs())
        r = stats("collect", "--run", FOREIGN_ID, "--auto", env=env)
        assert r.exit_code == 0, r.output
        assert _stored_run_ids(env) == []
        _scan(env, [r], allow_id_echo=[FOREIGN_ID], forbidden=[trick] if trick.startswith("competitor") else [])


def test_auto_on_foreign_run_does_not_write_its_name_or_contents_to_collect_log():
    with fresh_env() as env:
        _, s = _home(env)
        s.foreign_run(FOREIGN_ID, name="competitor-teardown-q4", **_foreign_kwargs())
        r = stats("collect", "--run", FOREIGN_ID, "--auto", env=env)
        log = Path(env.xdg) / "claude-kiro" / "collect.log"
        text = log.read_text() if log.exists() else ""
        assert "competitor-teardown-q4" not in text and PAYLOAD not in text
        assert FOREIGN_ID not in text  # 2.2: a typed id is echoed to the console only, never logged
        _scan(env, [r], allow_id_echo=[FOREIGN_ID], forbidden=["competitor-teardown-q4"])


def test_auto_foreign_run_with_unwritable_store_still_names_nothing():
    """Even on the failure path (log line plus retry command) a foreign run's name stays out."""
    with fresh_env() as env:
        _, s = _home(env)
        s.foreign_run(FOREIGN_ID, name="competitor-teardown-q4", **_foreign_kwargs())
        store = Path(env.xdg) / "claude-kiro"
        store.mkdir(parents=True)
        store.chmod(0o500)
        try:
            r = stats("collect", "--run", FOREIGN_ID, "--auto", env=env)
        finally:
            store.chmod(0o755)
        assert r.exit_code == 0, r.output
        _scan(env, [r], allow_id_echo=[FOREIGN_ID], forbidden=["competitor-teardown-q4"])
