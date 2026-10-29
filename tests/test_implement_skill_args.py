"""TC-1 (requirements 1.1, 3.1, 3.5): the implement skill passes the dependency graph and the
finish-first inputs to the spec-implement workflow, and the real `ck agents --json` emits the key
paths the skill tells the orchestrator to copy.

Reads the real `plugin/skills/implement/SKILL.md` (and its vendored copy) and runs the real
`ck agents --json` as a subprocess under an isolated CK_CONFIG_HOME.
"""

import json
import re
from pathlib import Path

import pytest

import stats_helpers

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "plugin" / "skills" / "implement" / "SKILL.md"
VENDORED = ROOT / ".claude" / "commands" / "spec" / "implement.md"

EXISTING = ("wave", "titles", "tracks", "risk", "complexity")


def launch_args(path: Path) -> dict:
    """The JSON args block of the whole-spec step 6 ("Launch") in a skill file."""
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^6\. \*\*Launch\.\*\*", text, re.M)
    assert m, f"{path}: step 6 (Launch) not found"
    block = re.search(r"```json\s*\n(.*?)\n\s*```", text[m.end():], re.S)
    assert block, f"{path}: no json args block after step 6"
    return json.loads(block.group(1))


@pytest.mark.parametrize("path", [SKILL, VENDORED], ids=["plugin", "vendored"])
def test_tc1_launch_args_carry_deps_remaining_and_finish_first_inputs(path):
    args = launch_args(path)
    for key in EXISTING:
        assert key in args, f"{path.name}: {key} missing from the args block"
    for key in ("deps", "remaining"):
        assert key in args, f"{path.name}: {key!r} missing next to {', '.join(EXISTING)}"
        assert "ck plan" in str(args[key]), f"{path.name}: {key} must be sourced from ck plan --json, got {args[key]!r}"
    assert "agentSlots" in args, f"{path.name}: agentSlots missing"
    assert "runtime.agentSlots" in str(args["agentSlots"]) and "ck agents" in str(args["agentSlots"]), args["agentSlots"]
    assert "finishFirst" in args, f"{path.name}: finishFirst missing"
    assert "run.finish_first" in str(args["finishFirst"]) and "ck agents" in str(args["finishFirst"]), args["finishFirst"]


def test_tc1_vendored_copy_names_the_same_sources():
    plugin, vendored = launch_args(SKILL), launch_args(VENDORED)
    for key in ("deps", "remaining", "agentSlots", "finishFirst"):
        assert vendored.get(key) == plugin.get(key), key


def test_tc1_real_ck_agents_json_has_the_key_paths_the_skill_names(tmp_path):
    env = {
        "CK_CONFIG_HOME": str(tmp_path / "prefs"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-home"),
        "HOME": str(tmp_path / "home"),
    }
    (tmp_path / "home").mkdir()
    project = tmp_path / "proj"
    (project / "specs").mkdir(parents=True)
    r = stats_helpers.ck_sub("agents", "--json", env=env, cwd=project)
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    slots = data["runtime"]["agentSlots"]
    assert isinstance(slots, int) and not isinstance(slots, bool) and slots >= 1
    assert isinstance(data["roles"]["run"]["finish_first"], bool)
