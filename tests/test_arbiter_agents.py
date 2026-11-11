"""The arbiter's role settings: `ck agents set` and `ck agents --json` (TC-35, TC-36)."""

import pytest

from test_agents import ck, home, roles  # noqa: F401  (home is a fixture)


@pytest.mark.parametrize(
    "key,value",
    [
        ("model", "fable"),
        ("effort", "xhigh"),
        ("risky_model", "opus"),
        ("risky_effort", "max"),
    ],
)
def test_tc35_each_arbiter_key_is_settable_and_shown(home, key, value):
    out = ck(home, "agents", "set", f"arbiter.{key}", value)
    assert out.exit_code == 0, out.output
    shown = roles(home)
    assert shown["roles"]["arbiter"][key] == value
    assert shown["sources"][f"arbiter.{key}"] != "default"


def test_tc35_all_four_keys_show_together(home):
    values = {"model": "fable", "effort": "low", "risky_model": "sonnet", "risky_effort": "medium"}
    for key, value in values.items():
        assert ck(home, "agents", "set", f"arbiter.{key}", value).exit_code == 0
    arbiter = roles(home)["roles"]["arbiter"]
    assert {k: arbiter[k] for k in values} == values


@pytest.mark.parametrize(
    "key,bad",
    [
        ("model", "gpt-5"),
        ("risky_model", "gpt-5"),
        ("effort", "fast"),
        ("risky_effort", "fast"),
    ],
)
def test_tc35_invalid_value_gets_the_reviewers_message(home, key, bad):
    mine = ck(home, "agents", "set", f"arbiter.{key}", bad)
    theirs = ck(home, "agents", "set", f"reviewer.{key}", bad)
    assert theirs.exit_code == 2
    assert mine.exit_code == 2, mine.output
    assert mine.output == theirs.output.replace(f"reviewer.{key}", f"arbiter.{key}")
    assert "unknown" in mine.output


def test_tc36_unset_arbiter_follows_the_reviewers_risky_settings(home):
    r = roles(home)["roles"]
    assert (r["arbiter"]["model"], r["arbiter"]["effort"]) == (r["reviewer"]["risky_model"], r["reviewer"]["risky_effort"])
    assert (r["arbiter"]["risky_model"], r["arbiter"]["risky_effort"]) == (
        r["reviewer"]["risky_model"],
        r["reviewer"]["risky_effort"],
    )


def test_tc36_follows_reviewer_risky_settings_after_they_change(home):
    assert ck(home, "agents", "set", "reviewer.risky_model", "sonnet").exit_code == 0
    assert ck(home, "agents", "set", "reviewer.risky_effort", "xhigh").exit_code == 0
    out = roles(home)
    arbiter = out["roles"]["arbiter"]
    for key in ("model", "effort", "risky_model", "risky_effort"):
        want = out["roles"]["reviewer"]["risky_" + ("model" if "model" in key else "effort")]
        assert arbiter[key] == want, key
    assert arbiter["model"] == "sonnet" and arbiter["effort"] == "xhigh"
    assert "reviewer.risky" in out["sources"]["arbiter.model"]


def test_tc36_a_set_key_wins_and_the_rest_still_follow(home):
    assert ck(home, "agents", "set", "arbiter.model", "fable").exit_code == 0
    assert ck(home, "agents", "set", "reviewer.risky_effort", "max").exit_code == 0
    arbiter = roles(home)["roles"]["arbiter"]
    assert arbiter["model"] == "fable"
    assert arbiter["effort"] == "max"
