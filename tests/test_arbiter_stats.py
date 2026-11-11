"""TC-95 (10.8): a workflow result's `rulings` reach `ck stats show --json` as counts by trigger, outcome and tier.

The run file is a real one written by tests/stats_helpers.py, collected by the real `ck stats collect`.
"""

import json

import pytest

from stats_helpers import FakeClaudeHome, fresh_env, git_project, implement_result, stats

RUN_ID = "wf_impl_rulings"


def _ruling(number, task, trigger, outcome, tier):
    return {"number": number, "task": task, "trigger": trigger, "outcome": outcome, "tier": tier,
            "principle": "P1", "commit": f"c{number}" if number else None, "landed": True if number else None}


RULINGS = [
    _ruling(1, "2", "gate-failure", "code", "normal"),
    _ruling(2, "2", "gate-failure", "code", "risky"),
    _ruling(3, "3", "test-conflict", "test_repair", "normal"),
    _ruling(4, "4", "guard-stop", "spec_edit", "risky"),
    _ruling(None, "5", "test-conflict", "test_repair", "normal"),
]


@pytest.fixture
def env():
    with fresh_env() as e:
        yield e


def test_tc95_rulings_reach_stats_show_as_counts_by_trigger_outcome_and_tier(env):
    home = FakeClaudeHome(env.claude)
    proj = git_project(env.root, "git@github.com:acme-corp/ledger-a.git", name="proj-a")
    result = implement_result()
    result["rulings"] = RULINGS
    home.session("-proj-a", cwd=proj).implement_run(RUN_ID, start="2026-10-05T09:00:00Z", result=result)
    out = stats("collect", "--all", env=env)
    assert out.exit_code == 0, out.output

    shown = stats("show", RUN_ID, "--json", env=env)
    assert shown.exit_code == 0, shown.output
    data = json.loads(shown.stdout)

    assert "rulings" in data, f"no rulings key in {sorted(data)}"
    r = data["rulings"]
    assert r["byTrigger"] == {"gate-failure": 2, "test-conflict": 2, "guard-stop": 1}
    assert r["byOutcome"] == {"code": 2, "test_repair": 2, "spec_edit": 1}
    assert r["byTier"] == {"normal": 3, "risky": 2}
