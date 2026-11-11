"""TC-173: the run's route for an unreproduced landing (run-principles Story 15, 15.3).

The real `plugin/workflows/spec-implement.js` runs under the fake runtime through the bridge. The fake project's landing
answers every task MERGED with an `unreproduced` key (fake_project `unreproduced: {batch: [...]}`). The attributed gate
answers are the JSON the real `ck gate demo --attribute --json` prints in a real project, red in one case and green in
the other. Project imports happen inside the test bodies.
"""

import json
from pathlib import Path
from typing import Any, Dict, List

from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)
from unattended_helpers import ALL_DONE, attr_project, ck_json, integration_worktree

LABEL = "full gate @1 (unreproduced landing)"
LANDED_FIX = "(Task 1 red on landing)"


def real_gate(tmp_path: Path, name: str, tests: Dict[str, str]) -> Dict[str, Any]:
    """The JSON of the real `ck gate demo --attribute --json` over a real project holding `tests`."""
    root = attr_project(tmp_path / name, statuses=ALL_DONE, tests=tests)
    code, obj, err = ck_json(root, "gate", "demo", "--attribute", "--json")
    assert isinstance(obj, dict) and obj.get("schema") == "ck-gate/1", (code, err[-500:])
    return {"root": root, "json": obj, "code": code}


def run(bridge, root: Path, answers: List[Dict[str, Any]]) -> Dict[str, Any]:  # noqa: F811
    project = {"deps": {"1": []}, "durations": {"1": 1}, "unreproduced": {"batch": ["1"]},
               "gateAnswers": answers}
    args = {"root": str(root), "into": "integrate/demo", "targetDir": str(integration_worktree(root)),
            "mergeBatch": 1, "fullGateEvery": 0, "deps": {"1": []}, "remaining": ["1"]}
    return bridge.run(project, args, clock="virtual")


def labels(out: Dict[str, Any]) -> List[str]:
    return [e["label"] for e in out["trace"] if e["ev"] == "start"]


def landing_entries(out: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = out["result"]
    return result["landings"] if isinstance(result, dict) and "landings" in result else []


def test_tc173_red_attributed_gate_runs_a_fixer_and_a_recheck(bridge, tmp_path):  # noqa: F811
    red = real_gate(tmp_path, "red", {"test_tc1_a": "pass", "test_tc2_b": "fail"})
    green = real_gate(tmp_path, "green", {"test_tc1_a": "pass", "test_tc2_b": "pass"})
    assert red["json"]["ok"] is False and green["json"]["ok"] is True
    out = run(bridge, red["root"], [red["json"], green["json"], green["json"]])
    started = labels(out)
    assert LABEL in started, started
    # a fixer gets the attributed entry, then a recheck runs
    fixes = [l for l in started if l.startswith("fix ")]
    assert len(fixes) == 1 and "unreproduced" not in fixes[0], started
    assert started.index(LABEL) < started.index(fixes[0]) < started.index("re-check 1"), started
    fix_prompt = out["prompts"][fixes[0]]
    assert red["json"]["failures"][0]["node"] in fix_prompt, fix_prompt
    # no task went to the landing fixer: none is RED
    assert not [l for l in started if LANDED_FIX in l], started
    # the landing entry records it
    entries = landing_entries(out)
    assert entries and entries[0].get("unreproduced") is True, entries
    assert entries[0]["red"] == [] and entries[0]["merged"] == ["1"], entries
    attributed = [c for c in out["trace"] if c.get("label") == LABEL]
    assert attributed, out["trace"][:5]


def test_tc173_green_attributed_gate_runs_no_fixer(bridge, tmp_path):  # noqa: F811
    green = real_gate(tmp_path, "green", {"test_tc1_a": "pass", "test_tc2_b": "pass"})
    out = run(bridge, green["root"], [green["json"], green["json"]])
    started = labels(out)
    assert LABEL in started, started
    assert not [l for l in started if l.startswith("fix ") or l.startswith("re-check")], started
    entries = landing_entries(out)
    assert entries and entries[0].get("unreproduced") is True, entries


def test_tc173_landing_entry_keeps_recovered(bridge, tmp_path):  # noqa: F811
    green = real_gate(tmp_path, "green", {"test_tc1_a": "pass", "test_tc2_b": "pass"})
    project = {"deps": {"1": []}, "durations": {"1": 1}, "gateAnswers": [green["json"]], "landRecovered": {"restored": True}}
    args = {"root": str(green["root"]), "into": "integrate/demo", "targetDir": str(integration_worktree(green["root"])),
            "mergeBatch": 1, "fullGateEvery": 0, "deps": {"1": []}, "remaining": ["1"]}
    out = bridge.run(project, args, clock="virtual")
    entries = landing_entries(out)
    assert entries and entries[0].get("recovered") == {"restored": True}, entries
