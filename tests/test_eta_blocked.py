"""`ck eta` blocked, waiting-on-me and slipping views (run-followups Task 20: TC-39..TC-45).

Setup events are written with `eta_helpers.append_events` at pinned times (the approval of TC-42(a) goes through the real
`ck progress event`); `ck eta` runs at a pinned `CK_NOW`.
"""

from __future__ import annotations

import re

import pytest

import eta_helpers as h
from test_eta_live import (CHAIN3, TASKS6, Scenario, approval_end, approval_start, at, ev, merge, parse, park,
                           start_task, when)

pytestmark = pytest.mark.usefixtures("no_network")

SEED = ("--seed", "4")


def finish_lines(text: str):
    return [line for line in text.splitlines() if re.match(r"\s*finish\b", line)]


def two_chains():
    return [h.TaskDef(1), h.TaskDef(2, deps=(1,)), h.TaskDef(3), h.TaskDef(4, deps=(3,))]


# --------------------------------------------------------------------------- TC-39


def test_tc39_whole_run_park(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    s.add(start_task(0, 1), park(5, 1))
    text = s.text(10)
    view = s.view(10)
    assert "blocked: task 1 parked (needs input)" in text
    assert re.search(r"\b2\b", text) and re.search(r"\b3\b", text)
    assert finish_lines(text) == []
    assert "remaining once unblocked" in text
    assert view["state"] == "blocked" and view["finish"] is None and view["runnableDone"] is None
    for p in ("p10", "p50", "p90"):
        assert view["remainingMinutes"][p] is not None
    assert set(view["blocked"]["held"]) >= {"1", "2", "3"}


# --------------------------------------------------------------------------- TC-40


def test_tc40_branch_park(tmp_path):
    s = Scenario(tmp_path, two_chains(), name="branch")
    s.begin()
    s.add(start_task(0, 1), start_task(0, 3), park(5, 3))
    view = s.view(10, *SEED)
    text = s.text(10, *SEED)
    assert view["state"] == "blocked" and view["finish"] is None
    assert "task 3 parked (needs input)" in view["blocked"]["reason"]
    assert set(view["waitingOnYou"]) == {"3", "4"}
    assert set(view["blocked"]["held"]) >= {"3", "4"}

    only12 = Scenario(tmp_path, [h.TaskDef(1), h.TaskDef(2, deps=(1,))], name="only12")
    only12.begin()
    only12.add(start_task(0, 1))
    ref = only12.view(10, *SEED)
    assert view["remainingMinutes"]["p50"] >= ref["remainingMinutes"]["p50"]
    assert view["runnableDone"] is not None
    assert parse(view["runnableDone"]) == parse(ref["finish"]["p50"])
    assert re.search(r"runnable work done ~\d\d:\d\d", text)
    assert finish_lines(text) == []


# --------------------------------------------------------------------------- TC-41


def test_tc41a_failed_task_with_dependents_blocks(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    s.add(start_task(0, 1), ev("task-failed", 5, task="1", reason="gate"))
    view = s.view(8)
    text = s.text(8)
    assert view["state"] == "blocked" and view["finish"] is None
    assert "1" in view["blocked"]["reason"] and {"2", "3"} <= set(view["blocked"]["held"])
    assert "blocked:" in text and "remaining once unblocked" in text
    assert view["remainingMinutes"]["p50"] is not None
    assert finish_lines(text) == []


def test_tc41b_hand_landing_waits_and_merge_closes_it(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    s.add(start_task(0, 1), merge(10, 1), start_task(10, 2), ev("hand-landing", 20, task="2"))
    view = s.view(27)
    assert view["state"] == "waiting" and view["blocked"] is None
    assert view["waitingOnYouMinutes"] == 7
    assert "waiting on you 7 min" in s.text(27)
    s.add(merge(30, 2))
    after = s.view(31)
    assert after["state"] != "waiting" and after["waitingOnYouMinutes"] is None


def test_tc41c_failed_leaf_is_not_blocked(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    s.add(start_task(0, 1), merge(4, 1), start_task(4, 2), merge(8, 2), start_task(8, 3),
          ev("task-failed", 12, task="3", reason="gate"))
    view = s.view(14)
    assert view["state"] != "blocked" and view["blocked"] is None


# --------------------------------------------------------------------------- TC-42


@pytest.mark.parametrize("through_cli", [True, False], ids=["cli-writes-cache", "append-no-cache"])
def test_tc42_open_approval_freezes_remaining(tmp_path, through_cli):
    s = Scenario(tmp_path, CHAIN3, name="cli" if through_cli else "raw")
    s.begin()
    s.add(start_task(0, 1))
    if through_cli:
        out = h.event(s.env, s.repo, SPEC_NAME, "approval-start", now=at(20), reason="question")
        assert out.exit_code == 0, out.output
    else:
        s.add(approval_start(20))
    early, late = s.view(27, *SEED), s.view(40, *SEED)
    assert early["state"] == late["state"] == "waiting"
    assert early["waitingOnYouMinutes"] == 7 and late["waitingOnYouMinutes"] == 20
    assert "waiting on you 7 min" in s.text(27, *SEED) and "waiting on you 20 min" in s.text(40, *SEED)
    assert early["remainingMinutes"] == late["remainingMinutes"]


SPEC_NAME = "demo"


# --------------------------------------------------------------------------- TC-43


def test_tc43_approval_end_shifts_finish_by_the_wait(tmp_path):
    a = Scenario(tmp_path, CHAIN3, name="with-wait")
    a.begin()
    a.add(start_task(0, 1), approval_start(20), approval_end(40), merge(41, 1))
    b = Scenario(tmp_path, CHAIN3, name="without-wait")
    b.begin()
    b.add(start_task(0, 1), merge(21, 1))
    va, vb = a.view(45, *SEED), b.view(25, *SEED)
    assert va["state"] != "slipping"
    for p in ("p10", "p50", "p90"):
        assert parse(va["finish"][p]) == parse(vb["finish"][p]) + (when(20) - when(0))


# --------------------------------------------------------------------------- TC-44


def test_tc44_unpark_returns_to_estimating(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    s.add(start_task(0, 1), park(10, 1), ev("unpark", 30, task="1"))
    view = s.view(32)
    assert view["state"] in ("on-track", "slipping")
    assert parse(view["finish"]["p10"]) >= when(30)


def test_tc44_failed_then_fixed_and_merged_returns_to_estimating(tmp_path):
    s = Scenario(tmp_path, CHAIN3, name="fixed")
    s.begin()
    s.add(start_task(0, 1), ev("task-failed", 10, task="1", reason="gate"), start_task(20, 1, "retry", attempt=2),
          merge(30, 1))
    view = s.view(32)
    assert view["state"] in ("on-track", "slipping")
    assert parse(view["finish"]["p10"]) >= when(30)


# --------------------------------------------------------------------------- TC-45


def test_tc45_slipping_has_new_time_and_cause(tmp_path):
    s = Scenario(tmp_path, TASKS6)
    s.begin()
    late = int(s.launch_p90_minute()) + 40
    s.add(start_task(0, 1), start_task(late - 3, 1, "retry", attempt=2))
    view = s.view(late)
    text = s.text(late)
    assert view["state"] == "slipping"
    assert "slipping" in text
    assert re.search(r"finish \d\d:\d\d", text)
    assert "task 1 retry 1" in text and view["cause"] == "task 1 retry 1"


def test_tc45_on_track_has_no_slip_wording(tmp_path):
    s = Scenario(tmp_path, TASKS6)
    s.begin()
    s.add(start_task(0, 1))
    view = s.view(3)
    text = s.text(3)
    assert view["state"] == "on-track"
    assert re.search(r"finish \d\d:\d\d\W+\d\d:\d\d\W+\d\d:\d\d", text)
    assert "slip" not in text.lower()
