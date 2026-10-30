"""TC-48 follow-up: when `bundledParts` names a wait, the estimate really draws that wait from the bundled defaults.

POOL_IMPL_NOWAIT records every review and landing wait as 0 ms (collect writes 0 for a task that merged with no
wait). Those zeros are no wait data, so the label names both waits as bundled; this checks the simulation agrees:
the same history with bundled waits set to zero gives a shorter estimate than with the packaged bundled waits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import eta_helpers as h

pytestmark = pytest.mark.usefixtures("no_network")

NOW = "2026-10-30T15:00:00Z"
PACKAGED = Path(__file__).resolve().parent.parent / "src" / "claude_kiro" / "resources" / "eta_defaults.json"


def _view(env, repo):
    out = h.eta(env, repo, "demo", "--json", "--seed", "2", now=NOW)
    assert out.exit_code == 0, out.output
    return json.loads(out.stdout)


def test_tc48_zero_waits_are_sampled_from_the_bundled_waits(tmp_path):
    xdg = h.install_pool(h.POOL_IMPL_NOWAIT, tmp_path / "xdg")
    repo = h.eta_spec(tmp_path / "ledger", [h.TaskDef(1, model="sonnet"), h.TaskDef(2, model="sonnet", deps=(1,)),
                                            h.TaskDef(3, model="sonnet", deps=(2,))])
    defaults = json.loads(PACKAGED.read_text())
    assert any(v > 0 for vals in defaults["waits"].values() for v in vals)
    defaults["waits"] = {k: [0.0] * len(v) for k, v in defaults["waits"].items()}
    zero = tmp_path / "zero-waits.json"
    zero.write_text(json.dumps(defaults))

    base = {"XDG_DATA_HOME": str(xdg), "CK_AGENT_SLOTS": "4"}
    bundled = _view(base, repo)
    no_wait = _view({**base, h.DEFAULTS_ENV: str(zero)}, repo)
    for view in (bundled, no_wait):
        assert set(view["basis"]["bundledParts"]) >= {"review wait", "landing wait"}
    assert bundled["remainingMinutes"]["p50"] > no_wait["remainingMinutes"]["p50"]
