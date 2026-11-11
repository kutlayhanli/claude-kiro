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


# --- P-9: an unset arbiter setting follows reviewer.risky_*, a set one wins by layer ---

import json  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from hypothesis import given, settings, strategies as st  # noqa: E402

PROPERTIES = {"P-9": "test_p9_unset_follows_reviewer_set_wins_by_layer_invalid_matches_reviewer"}

_ARBITER_KEYS = ("model", "effort", "risky_model", "risky_effort")
_NULLISH = ("null", "none", "default")

_VALID_MODEL = st.sampled_from(["haiku", "sonnet", "opus", "fable", "claude-opus-5-5", "claude-sonnet-4", "inherit"])
_VALID_EFFORT = st.sampled_from(["low", "medium", "high", "xhigh", "max", "inherit"])
_BAD_MODEL = st.sampled_from(["gpt-5", "Opus", "", "claude", "fast", "sonnet4", "high"])
_BAD_EFFORT = st.sampled_from(["fast", "HIGH", "", "ultra", "opus", "claude-opus-5-5"])


def _model_value(allow_int):
    bad = st.one_of(_BAD_MODEL, st.just(5)) if allow_int else _BAD_MODEL
    return st.one_of(_VALID_MODEL, _VALID_MODEL, bad)


def _effort_value(allow_int):
    bad = st.one_of(_BAD_EFFORT, st.just(5)) if allow_int else _BAD_EFFORT
    return st.one_of(_VALID_EFFORT, _VALID_EFFORT, bad)


def _arbiter_layer(allow_int):
    values = {
        "model": _model_value(allow_int),
        "effort": _effort_value(allow_int),
        "risky_model": _model_value(allow_int),
        "risky_effort": _effort_value(allow_int),
    }
    return st.dictionaries(st.sampled_from(_ARBITER_KEYS), st.just(None), max_size=4).flatmap(
        lambda keys: st.fixed_dictionaries({k: values[k] for k in keys})
    )


def _reviewer_layer():
    # Always valid: this property is about the arbiter's values; None layer = reviewer unset.
    return st.one_of(
        st.just({}),
        st.fixed_dictionaries(
            {},
            optional={
                "model": _VALID_MODEL,
                "effort": _VALID_EFFORT,
                "risky_model": _VALID_MODEL,
                "risky_effort": _VALID_EFFORT,
            },
        ),
    )


@st.composite
def _layers(draw):
    return [
        {"reviewer": draw(_reviewer_layer()), "arbiter": draw(_arbiter_layer(True))},  # global
        {"reviewer": draw(_reviewer_layer()), "arbiter": draw(_arbiter_layer(True))},  # project
        {"reviewer": draw(_reviewer_layer()), "arbiter": draw(_arbiter_layer(False))},  # override (strings)
    ]


def _is_valid(key, value):
    if not isinstance(value, str):
        return False
    if "model" in key:
        return value in ("haiku", "sonnet", "opus", "fable", "inherit") or value.startswith("claude-")
    return value in ("low", "medium", "high", "xhigh", "max", "inherit")


@settings(derandomize=True, max_examples=200, database=None, deadline=None)
@given(layers=_layers())
def test_p9_unset_follows_reviewer_set_wins_by_layer_invalid_matches_reviewer(layers):
    from claude_kiro import agents
    from claude_kiro.paths import CONFIG_FILE

    global_layer, project_layer, override_layer = layers
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "project"
        (project / CONFIG_FILE).parent.mkdir(parents=True)
        cfg_home = Path(tmp) / "cfg"
        cfg_home.mkdir()
        cfg = cfg_home / "config.json"
        cfg.write_text(json.dumps({"agents": {k: v for k, v in global_layer.items() if v}}))
        (project / CONFIG_FILE).write_text(json.dumps({"agents": {k: v for k, v in project_layer.items() if v}}))
        overrides = [f"{role}.{k}={v}" for role, vals in override_layer.items() for k, v in vals.items()]

        saved = os.environ.get("CK_CONFIG_HOME")
        os.environ["CK_CONFIG_HOME"] = str(cfg_home)
        try:
            try:
                got = agents.resolve(project, overrides)
                error = None
            except agents.AgentConfigError as exc:
                got, error = None, str(exc)
            # the reviewer's own message for the same key and value
            expected_error = None
            for source, layer in ((str(cfg), global_layer), (CONFIG_FILE, project_layer), ("override", override_layer)):
                for key, value in layer["arbiter"].items():
                    if not _is_valid(key, value):
                        try:
                            agents._coerce("reviewer", key, value if source != "override" else str(value))
                        except agents.AgentConfigError as exc:
                            expected_error = str(exc).replace(f"reviewer.{key}", f"arbiter.{key}")
                        break
                if expected_error:
                    break
        finally:
            if saved is None:
                os.environ.pop("CK_CONFIG_HOME", None)
            else:
                os.environ["CK_CONFIG_HOME"] = saved

    if expected_error is not None:
        assert error == expected_error
        return
    assert error is None, error

    named = [("override", override_layer), (CONFIG_FILE, project_layer), (str(cfg), global_layer)]

    def reviewer_risky(key):
        for _, layer in named:
            if key in layer["reviewer"]:
                return layer["reviewer"][key]
        return agents.DEFAULTS["reviewer"][key]

    arbiter = got["roles"]["arbiter"]
    for key in _ARBITER_KEYS:
        winner = next(((src, layer["arbiter"][key]) for src, layer in named if key in layer["arbiter"]), None)
        if winner is not None:
            assert arbiter[key] == winner[1], key
            assert got["sources"][f"arbiter.{key}"] == winner[0], key
        else:
            follow = reviewer_risky("risky_model" if "model" in key else "risky_effort")
            assert arbiter[key] == follow, key
            assert "reviewer.risky_" in got["sources"][f"arbiter.{key}"], key
