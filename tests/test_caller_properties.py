"""Properties of caller classification (prompt-free-runs P-11, P-32).

P-11 runs the pure seam `guard_policy.classify_caller(input, env, workflow_marker)` over generated inputs, with the
recorded hook inputs of tests/fixtures/hook_inputs as `@example` seeds. P-32 runs `run_marker` with an injected clock
and is skipped unless the fallback marker is built (capture-summary.json says Workflow agents lack `agent_id`).
"""

import copy
import json
import time
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

PROPERTIES = {
    "P-11": "test_p11_classify_caller_matches_the_rule",
    "P-32": "test_p32_marker_lifecycle",
}

FIXTURES = Path(__file__).parent / "fixtures" / "hook_inputs"
SUMMARY = json.loads((FIXTURES / "capture-summary.json").read_text())
MARKER_BUILT = not SUMMARY["workflowAgentHasAgentId"]

SETTINGS = dict(max_examples=200, derandomize=True, database=None, deadline=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


def fixture_input(name):
    return json.loads((FIXTURES / f"{name}-pretooluse.json").read_text())


# --------------------------------------------------------------------------- P-11 oracle

_JSON = st.recursive(
    st.none() | st.booleans() | st.integers(-5, 5) | st.text(max_size=8),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=5), inner, max_size=3),
    max_leaves=6,
)
_AGENT_IDS = st.sampled_from([None, "", "  ", "a1b2", "agent-0001", 42, [], {"id": "x"}, True])
_MISSING = object()


def expected(hook_input, env, marker):
    agent_id = hook_input.get("agent_id") if isinstance(hook_input, dict) else None
    if isinstance(agent_id, str) and agent_id != "":
        return "task_agent"
    if env.get("CK_RUN_HEADLESS") == "1" or str(env.get("CLAUDE_CODE_ENTRYPOINT", "")).startswith("sdk"):
        return "task_agent"
    return "task_agent" if marker else "main_session"


@st.composite
def hook_inputs(draw):
    data = {}
    for key, strat in (("session_id", st.text(max_size=8)), ("cwd", st.text(max_size=12)), ("hook_event_name", st.text(max_size=8)),
                       ("tool_name", st.sampled_from(["Edit", "Bash", "Write", "MultiEdit"])), ("tool_input", _JSON)):
        value = draw(st.one_of(st.just(_MISSING), strat, _JSON))
        if value is not _MISSING:
            data[key] = value
    agent = draw(st.one_of(st.just(_MISSING), _AGENT_IDS))
    if agent is not _MISSING:
        data["agent_id"] = agent
    for key, value in draw(st.dictionaries(st.text(min_size=1, max_size=6).filter(lambda k: k != "agent_id"), _JSON, max_size=3)).items():
        data.setdefault(key, value)
    return data


_ENVS = st.fixed_dictionaries({}, optional={
    "CK_RUN_HEADLESS": st.sampled_from(["1", "0", "", "true", "yes"]),
    "CLAUDE_CODE_ENTRYPOINT": st.sampled_from(["sdk-cli", "sdk-ts", "sdk", "cli", "", "xsdk", "SDK-cli"]),
    "OTHER": st.text(max_size=4),
})


@settings(**SETTINGS)
@given(hook_inputs(), _ENVS, st.booleans())
@example(fixture_input("main-session"), {}, False)
@example(fixture_input("agent-tool-subagent"), {}, False)
@example(fixture_input("workflow-agent"), {}, False)
@example(fixture_input("headless"), {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, False)
@example(fixture_input("main-session"), {"CK_RUN_HEADLESS": "1"}, False)
@example(fixture_input("main-session"), {"CK_RUN_HEADLESS": "0", "CLAUDE_CODE_ENTRYPOINT": "cli"}, False)
@example(fixture_input("main-session"), {}, True)
@example({}, {}, False)
@example({"agent_id": 42}, {}, True)
def test_p11_classify_caller_matches_the_rule(hook_input, env, marker):
    from claude_kiro.hooks._shared.guard_policy import classify_caller

    before = copy.deepcopy(hook_input)
    result = classify_caller(hook_input, env, marker)
    assert result == expected(hook_input, env, marker)
    assert hook_input == before  # the input is never modified

    # Metamorphic: unknown keys, a different cwd and different tool paths never change the result.
    if isinstance(hook_input, dict):
        changed = dict(hook_input)
        changed["zz_unknown"] = {"foo": [1, {"bar": None}]}
        changed["cwd"] = "/some/task/worktree"
        ti = changed.get("tool_input")
        if isinstance(ti, dict):
            changed["tool_input"] = {**ti, "file_path": "/some/task/worktree/tests/test_calc.py"}
        assert classify_caller(changed, env, marker) == result
        removed = {k: v for k, v in hook_input.items() if k not in ("cwd", "transcript_path", "prompt_id", "effort", "permission_mode")}
        assert classify_caller(removed, env, marker) == result


@settings(**SETTINGS)
@given(st.one_of(st.none(), st.integers(), st.text(max_size=5), st.lists(_JSON, max_size=3)), _ENVS, st.booleans())
def test_p11_non_mapping_input_never_raises(hook_input, env, marker):
    from claude_kiro.hooks._shared.guard_policy import classify_caller

    assert classify_caller(hook_input, env, marker) == expected(hook_input, env, marker)


# --------------------------------------------------------------------------- P-32

_SESSIONS = st.sampled_from(["s1", "s2"])
_EVENTS = st.lists(st.tuples(st.sampled_from(["mark", "refresh", "unmark", "clear"]), _SESSIONS, st.integers(0, 3600)), max_size=12)


def model(events, session, now):
    """active iff the last `mark` of the session is not followed by an `unmark`/`marker-clear`, and `now` is within
    MARKER_IDLE_S of the latest `mark`/`refresh` after it (a refresh with no mark in force changes nothing)."""
    last = None
    for kind, sess, at in events:
        if sess != session:
            continue
        if kind == "mark":
            last = at
        elif kind in ("unmark", "clear"):
            last = None
        elif kind == "refresh" and last is not None:
            last = at
    return last is not None and now - last < 900


@pytest.mark.skipif(not MARKER_BUILT, reason="capture-summary.json: Workflow agents carry agent_id, so the fallback marker is not built")
@settings(**SETTINGS)
@given(events=_EVENTS, gap=st.integers(0, 3600))
def test_p32_marker_lifecycle(events, gap, tmp_path_factory):
    from claude_kiro.cli.runner import execute_hook
    from claude_kiro.hooks._shared import run_marker

    assert run_marker.MARKER_IDLE_S == 900
    base = tmp_path_factory.mktemp("marker")
    env = {"XDG_DATA_HOME": str(base), "CK_DATA_HOME": str(base)}
    clock = {"now": 0.0}
    events = sorted(events, key=lambda e: e[2])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(time, "time", lambda: clock["now"])
        for kind, sess, at in events:
            clock["now"] = float(at)
            if kind == "mark":
                run_marker.mark(sess, "calc", env=env)
            elif kind == "refresh":
                run_marker.refresh(sess, env=env, now=float(at))
            elif kind == "unmark":
                run_marker.unmark(sess, env=env)
            else:
                mp.setenv("CK_DATA_HOME", str(base))
                assert execute_hook("marker-clear", {"session_id": sess, "hook_event_name": "UserPromptSubmit", "cwd": str(base)}) is None
        now = (events[-1][2] if events else 0) + gap
        for sess in ("s1", "s2"):
            assert run_marker.active(sess, env=env, now=float(now)) == model(events, sess, now)
