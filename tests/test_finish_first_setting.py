"""run.finish_first setting (TC-28 CLI part, TC-30, P-12) and the runtime agent-slot block of `ck agents --json` (3.1).

The CLI tests run the real `ck agents` as a subprocess on real config files in an isolated CK_CONFIG_HOME.
P-12 calls the coercion seam `claude_kiro.agents._coerce("run", "finish_first", v)`.
"""

import json
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

import stats_helpers

PROPERTIES = {"P-12": "test_p12_finish_first_accepts_only_boolean_words"}

_WORDS = {"true": True, "false": False, "yes": True, "no": False, "on": True, "off": False}
_RESETS = ("null", "none", "default")


@pytest.fixture
def env_project(tmp_path):
    env = {
        "CK_CONFIG_HOME": str(tmp_path / "prefs"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-home"),
        "HOME": str(tmp_path / "home"),
    }
    (tmp_path / "home").mkdir()
    project = tmp_path / "proj"
    (project / "specs").mkdir(parents=True)
    return env, project, tmp_path


def _ck(env, project, *args, extra_env=None):
    return stats_helpers.ck_sub("agents", *args, env={**env, **(extra_env or {})}, cwd=project)


def _json(env, project, extra_env=None):
    r = _ck(env, project, "--json", extra_env=extra_env)
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)


def _paths(env, project: Path):
    return [Path(env["CK_CONFIG_HOME"]) / "config.json", project / "specs" / "ck.json"]


def _snapshot(paths):
    return {p: (p.read_bytes() if p.exists() else None) for p in paths}


def test_tc28_default_finish_first_true_next_to_other_run_settings(env_project):
    env, project, _ = env_project
    run = _json(env, project)["roles"]["run"]
    assert run["finish_first"] is True
    assert "analytics" in run  # same place as the other run settings


@pytest.mark.parametrize("extra", [[], ["--project"]], ids=["global", "project"])
def test_tc30_off_is_stored_and_maybe_is_refused(env_project, extra):
    env, project, _ = env_project
    r = _ck(env, project, "set", *extra, "run.finish_first", "off")
    assert r.exit_code == 0, r.output
    assert _json(env, project)["roles"]["run"]["finish_first"] is False
    before = _snapshot(_paths(env, project))
    r = _ck(env, project, "set", *extra, "run.finish_first", "maybe")
    assert r.exit_code != 0
    out = r.output.lower()
    assert "true or false" in out and "yes/no" in out and "on/off" in out
    assert _snapshot(_paths(env, project)) == before
    assert _json(env, project)["roles"]["run"]["finish_first"] is False


def test_reset_words_remove_the_key_and_restore_true(env_project):
    env, project, _ = env_project
    cfg = Path(env["CK_CONFIG_HOME"]) / "config.json"
    for word in _RESETS:
        assert _ck(env, project, "set", "run.finish_first", "off").exit_code == 0
        assert _json(env, project)["roles"]["run"]["finish_first"] is False
        r = _ck(env, project, "set", "run.finish_first", word)
        assert r.exit_code == 0, (word, r.output)
        assert "finish_first" not in json.dumps(json.loads(cfg.read_text()).get("agents", {}).get("run", {}))
        assert _json(env, project)["roles"]["run"]["finish_first"] is True


def test_runtime_block_has_integer_cpus_and_agent_slots(env_project):
    env, project, _ = env_project
    rt = _json(env, project)["runtime"]
    assert type(rt["cpus"]) is int and rt["cpus"] >= 1
    assert type(rt["agentSlots"]) is int and rt["agentSlots"] >= 1
    assert rt["agentSlots"] <= 16


def test_ck_agent_slots_env_overrides(env_project):
    env, project, _ = env_project
    assert _json(env, project, extra_env={"CK_AGENT_SLOTS": "5"})["runtime"]["agentSlots"] == 5


def _variants(w):
    return [w, w.upper(), w.capitalize(), w.swapcase()]


value_strategy = st.one_of(
    st.sampled_from(sorted(_WORDS)).flatmap(lambda w: st.sampled_from(_variants(w))),
    st.sampled_from(_RESETS).flatmap(lambda w: st.sampled_from(_variants(w))),
    st.sampled_from(["TRUE", "On", "nO", " true", "true ", "1", "0", "t", "y", "enabled", "disabled", "ture", "",
                     "[]", "true,false", "trüe", "true\n", "тrue"]),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz ,[]01", max_size=10),
)


@settings(derandomize=True, max_examples=200, database=None, deadline=None)
@given(value_strategy)
def test_p12_finish_first_accepts_only_boolean_words(v):
    from claude_kiro.agents import AgentConfigError, _coerce

    low = v.lower()
    if low in _WORDS:
        assert _coerce("run", "finish_first", v) is _WORDS[low]
    elif low in _RESETS:
        assert _coerce("run", "finish_first", v) is None
    else:
        with pytest.raises(AgentConfigError) as exc:
            _coerce("run", "finish_first", v)
        assert "true or false" in str(exc.value)


_CLI_CASES = [
    ("TRUE", True), ("nO", False), ("On", True),  # accepted
    ("1", None), ("ture", None), ("enabled", None),  # refused
]


@pytest.mark.parametrize("value,expected", _CLI_CASES, ids=[c[0] or "empty" for c in _CLI_CASES])
def test_p12_real_cli_cases(env_project, value, expected):
    env, project, _ = env_project
    before = _snapshot(_paths(env, project))
    r = stats_helpers.ck_sub("agents", "set", "--", "run.finish_first", value, env=env, cwd=project)
    if expected is None:
        assert r.exit_code != 0, r.output
        assert "true or false" in r.output
        assert _snapshot(_paths(env, project)) == before
        assert _json(env, project)["roles"]["run"]["finish_first"] is True
    else:
        assert r.exit_code == 0, r.output
        assert _json(env, project)["roles"]["run"]["finish_first"] is expected
