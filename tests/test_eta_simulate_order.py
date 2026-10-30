"""Tie-break order of the simulator's slot scheduler (run-followups review round 1).

`ck plan` starts ready tasks by chain length desc, then the numeric id. The simulator must use the same order, or the
finish quantiles are biased when slots are short: ids "2" and "10" must not swap on a string comparison.
"""

from claude_kiro.eta import simulate


def test_plan_key_breaks_chain_ties_by_numeric_id():
    chain = {"2": 3, "10": 3, "1": 3}
    assert sorted(["10", "2", "1"], key=lambda t: simulate.plan_key(chain, t)) == ["1", "2", "10"]


def test_plan_key_orders_longest_chain_first_before_id():
    chain = {"2": 1, "10": 4}
    assert sorted(["2", "10"], key=lambda t: simulate.plan_key(chain, t)) == ["10", "2"]


def test_schedule_starts_id_2_before_id_10_at_equal_chain():
    # One slot, equal chain, equal durations: whichever task starts first finishes at 5, the other at 10.
    ids = ["10", "2"]
    finish = simulate._schedule(
        active=ids,
        running={},
        hand=frozenset(),
        dependents={t: [] for t in ids},
        indeg0={t: 0 for t in ids},
        chain={"2": 3, "10": 3},
        slot_of={t: 5.0 for t in ids},
        rev_of={t: 0.0 for t in ids},
        land_of={t: 0.0 for t in ids},
        cap=1,
    )
    assert finish["2"] == 5.0
    assert finish["10"] == 10.0


def test_schedule_counts_excluded_running_tasks_as_busy_slots():
    # With the only slot taken by an excluded running task, no pending task may start.
    finish = simulate._schedule(
        active=["2"],
        running={},
        hand=frozenset(),
        dependents={"2": []},
        indeg0={"2": 0},
        chain={"2": 1},
        slot_of={"2": 5.0},
        rev_of={"2": 0.0},
        land_of={"2": 0.0},
        cap=1,
        busy=1,
    )
    assert finish == {}
