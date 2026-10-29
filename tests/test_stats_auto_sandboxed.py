"""`ck stats collect --auto` on a home this process cannot read: info line only under ck run (CK_RUN_SANDBOXED=1)."""

import pytest

from claude_kiro.cli import stats as st


@pytest.fixture
def unreadable(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "_classify", lambda home: "sandboxed")
    monkeypatch.setattr(st.col, "auto_collect_enabled", lambda role: True)
    logged = []
    monkeypatch.setattr(st.store, "append_collect_log", logged.append)
    return tmp_path / "home", logged


def test_sandboxed_under_ck_run_prints_info_line_and_exits_zero(unreadable, monkeypatch, capsys):
    home, logged = unreadable
    monkeypatch.setenv("CK_RUN_SANDBOXED", "1")
    assert st.collect_auto("wf_abc", home=home) == 0
    out = capsys.readouterr().out
    assert "ck run collects this run after the session exits" in out
    assert "retry" not in out
    assert logged == []


def test_sandboxed_without_flag_logs_failure_and_prints_retry(unreadable, monkeypatch, capsys):
    home, logged = unreadable
    monkeypatch.delenv("CK_RUN_SANDBOXED", raising=False)
    assert st.collect_auto("wf_abc", home=home) == 0
    out = capsys.readouterr().out
    assert "ck run collects" not in out
    assert "retry: ck stats collect --run wf_abc" in out
    assert len(logged) == 1 and "sandboxed" in logged[0]


def test_invalid_id_under_auto_is_logged_without_the_id(unreadable, monkeypatch, capsys):
    home, logged = unreadable
    monkeypatch.delenv("CK_RUN_SANDBOXED", raising=False)
    assert st.collect_auto("wf_*", home=home) == 0
    assert "wf_*" not in "".join(logged)
