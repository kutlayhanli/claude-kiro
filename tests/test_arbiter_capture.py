"""TC-160: the arbiter's identity, checked against what Claude Code really sends (Requirements 9.5, 9.6).

The fixtures are written by `tests/fixtures/hook_inputs/capture.sh --arbiter`, a paid `claude -p` run made by hand
(Task 91): a one-agent Workflow whose agent is started with `agentType` `spec:arbiter` (plugin), and one with the
vendored `ck-arbiter`. A derived placeholder carries `_derivedFrom`; a real capture does not.
"""
import json
from pathlib import Path

import pytest

HOOK_INPUTS = Path(__file__).parent / "fixtures" / "hook_inputs"

CAPTURES = {
    "spec:arbiter": HOOK_INPUTS / "arbiter-agent-pretooluse.json",
    "ck-arbiter": HOOK_INPUTS / "arbiter-vendored-agent-pretooluse.json",
}


@pytest.mark.parametrize("agent_type", sorted(CAPTURES))
def test_tc160_captured_arbiter_hook_input_is_the_arbiter(agent_type):
    from claude_kiro.hooks._shared import guard_policy

    path = CAPTURES[agent_type]
    assert path.is_file(), f"{path.name} not captured: run capture.sh --arbiter (Task 91)"
    hook_input = json.loads(path.read_text())

    assert "_derivedFrom" not in hook_input, f"{path.name} is a derived placeholder, not a real capture"
    assert hook_input.get("hook_event_name") == "PreToolUse"
    assert hook_input.get("tool_name") == "Bash"
    assert hook_input.get("agent_type") == agent_type
    assert isinstance(hook_input.get("agent_id"), str) and hook_input["agent_id"]
    assert guard_policy.is_arbiter_caller(hook_input) is True
