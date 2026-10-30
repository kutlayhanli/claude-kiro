"""Red team of the estimate clauses 1.9, 2.5, 5.2 and 5.5 (run-followups Task 20), worked from the requirements only.

1.9 cost is never `$0` when unknown. 2.5 untimed create records never become zero-length phases. 5.2 a parked run never
shows a finish range. 5.5 waited minutes never count toward slipping.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import eta_helpers as h
from test_eta_live import (CHAIN3, SPEC, T0, Scenario, approval_end, approval_start, at, ev, merge, parse, park,
                           start_task, when)

pytestmark = pytest.mark.usefixtures("no_network")

SEED = ("--seed", "4")
ZERO_DOLLARS = re.compile(r"\$-?0(\.0+)?(?![\d.])")


def _records_dir(s: Scenario) -> Path:
    return Path(s.env["XDG_DATA_HOME"]) / "claude-kiro" / "runs"


def _rewrite_records(s: Scenario, edit) -> None:
    for path in sorted(_records_dir(s).glob("*.json")):
        rec = json.loads(path.read_text())
        edit(rec)
        path.write_text(json.dumps(rec))


# `costs` below is a field of stored run records, not an EtaView key.
def _set_usd(value):
    def edit(rec):
        for c in rec.get("costs", []):
            c["usd"] = value
        for a in rec.get("agents", []):
            a["usd"] = value
    return edit


def _blank_first_agent(rec):
    if rec.get("agents"):
        rec["agents"][0]["usd"] = None


def assert_cost_unknown_or_positive(view: dict, text: str) -> None:
    cost = view["cost"]
    if cost is not None:
        known = [v for v in cost.values() if v is not None]
        assert all(v > 0 for v in known), cost
    # A priced estimate may legitimately round to whole dollars; only an unknown cost must never print $0.
    if cost is None or any(v is None for v in cost.values()):
        assert not ZERO_DOLLARS.search(text), text


# --------------------------------------------------------------------------- 1.9


@pytest.mark.parametrize("value", [0.0, -0.0, None], ids=["zero", "negative-zero", "null"])
def test_19_records_priced_at_zero_or_null_never_show_zero_dollars(tmp_path, value):
    s = Scenario(tmp_path, CHAIN3, pool=h.POOL_IMPL)
    _rewrite_records(s, _set_usd(value))
    view = s.view(0)
    assert_cost_unknown_or_positive(view, s.text(0))


def test_19_one_unpriced_agent_per_task_never_yields_zero(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    _rewrite_records(s, _blank_first_agent)
    assert_cost_unknown_or_positive(s.view(0), s.text(0))


def test_19_costless_pool_says_unknown_in_text_and_live(tmp_path):
    s = Scenario(tmp_path, CHAIN3, pool=h.POOL_COSTLESS)
    s.begin()
    s.add(start_task(0, 1))
    view = s.view(5)
    text = s.text(5)
    assert_cost_unknown_or_positive(view, text)
    assert "cost unknown" in text


# --------------------------------------------------------------------------- 2.5


def _create_scenario(tmp_path, edit, name):
    s = Scenario(tmp_path, [h.TaskDef(1)], pool=h.POOL_CREATE, name=name)
    (s.repo / "specs" / SPEC / "tasks.md").unlink()
    _rewrite_records(s, edit)
    return s


def _phase_medians(view: dict):
    return {p["phase"]: p["medianMinutes"] for p in view["phases"]}


def _check_create(s: Scenario):
    out = h.eta(s.env, s.repo, SPEC, "--kind", "create", "--json", now=at(0))
    assert out.exit_code == 0, out.output
    view = json.loads(out.stdout)
    assert view["runKind"] == "create"
    for phase, median in _phase_medians(view).items():
        assert median is None or median > 0, (phase, median)
    assert view["remainingMinutes"]["p50"] > 0
    return view


def test_25_zero_minute_phase_is_not_sampled(tmp_path):
    def edit(rec):
        rec["phases"]["Link"] = 0

    view = _check_create(_create_scenario(tmp_path, edit, "zero"))
    assert _phase_medians(view).get("Link") != 0


def test_25_null_minute_phase_is_not_sampled(tmp_path):
    def edit(rec):
        rec["phases"]["Link"] = None
        rec["phases"]["Revise"] = None

    view = _check_create(_create_scenario(tmp_path, edit, "null"))
    assert _phase_medians(view).get("Link") != 0


def test_25_missing_phase_in_half_the_records_does_not_drag_the_median_to_zero(tmp_path):
    counter = {"n": 0}

    def edit(rec):
        counter["n"] += 1
        if counter["n"] % 2:
            rec["phases"].pop("Link", None)
            rec["phases"].pop("Revise", None)
        else:
            rec["phases"]["Link"] = 0

    s = _create_scenario(tmp_path, edit, "missing")
    view = _check_create(s)
    medians = _phase_medians(view)
    assert medians.get("Revise") is None or medians["Revise"] >= 8


# --------------------------------------------------------------------------- 5.2


def _finish_lines(text: str):
    return [line for line in text.splitlines() if re.match(r"\s*finish\b", line)]


def test_52_park_on_a_diamond(tmp_path):
    diamond = [h.TaskDef(1), h.TaskDef(2, deps=(1,)), h.TaskDef(3, deps=(1,)), h.TaskDef(4, deps=(2, 3))]
    s = Scenario(tmp_path, diamond)
    s.begin()
    s.add(start_task(0, 1), merge(5, 1), start_task(5, 2), start_task(5, 3), park(10, 2))
    view, text = s.view(12), s.text(12)
    assert view["state"] == "blocked" and view["finish"] is None
    assert _finish_lines(text) == []
    assert set(view["waitingOnYou"]) >= {"2", "4"}
    assert "task 2 parked (needs input)" in view["blocked"]["reason"]
    assert set(view["blocked"]["held"]) >= {"2", "4"}


def test_52_runnable_time_is_never_the_finish(tmp_path):
    s = Scenario(tmp_path, [h.TaskDef(1), h.TaskDef(2, deps=(1,)), h.TaskDef(3), h.TaskDef(4)])
    s.begin()
    s.add(start_task(0, 1), start_task(0, 3), park(5, 1))
    view, text = s.view(10, *SEED), s.text(10, *SEED)
    assert view["finish"] is None and view["runnableDone"] is not None
    assert "task 1 parked (needs input)" in view["blocked"]["reason"]
    assert set(view["blocked"]["held"]) >= {"1", "2"}
    hhmm = parse(view["runnableDone"]).strftime("%H:%M")
    for line in _finish_lines(text):
        assert hhmm not in line
    assert all(hhmm not in line for line in text.splitlines() if "runnable work done" not in line and line.startswith("finish"))


def test_52_park_and_approval_open_together_still_show_no_finish(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    s.add(start_task(0, 1), park(5, 1), approval_start(8))
    view, text = s.view(12), s.text(12)
    assert view["state"] == "blocked"
    assert view["finish"] is None
    assert _finish_lines(text) == []
    assert view["blocked"] is not None


def test_52_an_etaview_fed_back_as_a_progress_line_changes_nothing(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    s.add(start_task(0, 1), park(5, 1))
    before = s.view(10)
    path = Path(before["progressFile"])
    with path.open("a") as fh:
        fh.write(json.dumps(before) + "\n")
    out = h.eta(s.env, s.repo, SPEC, "--json", now=at(10))
    assert out.exit_code == 0, out.output
    after = json.loads(out.stdout)
    assert after["state"] == "blocked" and after["finish"] is None


# --------------------------------------------------------------------------- 5.5


def _not_slipping_after_wait(s: Scenario, minute: float) -> dict:
    view = s.view(minute, *SEED)
    assert view["state"] != "slipping", view["state"]
    return view


def test_55_a_long_wait_is_not_a_slip(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    # The wait is longer than the launch range is wide, so after it the p50 is past the launch p90 by about the
    # waited minutes alone: the slip check must still subtract the wait (5.5) and so not report slipping.
    launch_p50 = (parse(s.launch["finish"]["p50"]) - T0).total_seconds() / 60
    wait = max(100, int(s.launch_p90_minute() - launch_p50) + 30)
    s.add(start_task(0, 1), approval_start(5), approval_end(5 + wait))
    view = _not_slipping_after_wait(s, 5 + wait + 2)
    p50_minute = (parse(view["finish"]["p50"]) - T0).total_seconds() / 60
    assert p50_minute > s.launch_p90_minute(), (p50_minute, s.launch_p90_minute())


def test_55_overlapping_approvals_count_their_union_once(tmp_path):
    two = Scenario(tmp_path, CHAIN3, name="two")
    two.begin()
    two.add(start_task(0, 1), approval_start(5, "aaaaaaaa"), approval_start(20, "bbbbbbbb"), approval_end(50, "aaaaaaaa"),
            approval_end(70, "bbbbbbbb"))
    one = Scenario(tmp_path, CHAIN3, name="one")
    one.begin()
    one.add(start_task(0, 1), approval_start(5, "cccccccc"), approval_end(70, "cccccccc"))
    v2 = _not_slipping_after_wait(two, 72)
    v1 = _not_slipping_after_wait(one, 72)
    assert v2["finish"] == v1["finish"]


def test_55_approval_end_without_a_start_or_with_another_id_waits_nothing(tmp_path):
    base = Scenario(tmp_path, CHAIN3, name="base")
    base.begin()
    base.add(start_task(0, 1))
    odd = Scenario(tmp_path, CHAIN3, name="odd")
    odd.begin()
    odd.add(start_task(0, 1), approval_end(10, "deadbeef"))
    vo = odd.view(12, *SEED)
    vb = base.view(12, *SEED)
    assert vo["state"] in ("on-track", "slipping") and vo["waitingOnYouMinutes"] is None
    assert vo["finish"] == vb["finish"]


def test_55_end_of_another_id_leaves_the_wait_open(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    s.add(start_task(0, 1), approval_start(10, "aaaaaaaa"), approval_end(15, "bbbbbbbb"))
    view = s.view(20)
    assert view["state"] == "waiting" and view["waitingOnYouMinutes"] == 10


def test_55_a_hand_landing_and_a_question_open_together(tmp_path):
    s = Scenario(tmp_path, CHAIN3)
    s.begin()
    s.add(start_task(0, 1), merge(5, 1), start_task(5, 2), ev("hand-landing", 10, task="2"),
          approval_start(15), approval_end(25))
    view = s.view(30)
    assert view["state"] == "waiting"
    assert view["waitingOnYouMinutes"] == 20
