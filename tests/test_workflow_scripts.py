"""Run the Node control-flow tests for the Workflow scripts (stubbed agents)."""

import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).parent / "js"
# Scripts whose scenarios each build a real git project and run the real `ck` CLI (about 20-30 s idle); the
# whole suite under -n auto can more than double that. Every other script keeps the 60 s limit.
SLOW_TIMEOUTS = {"guard_ladder.test.mjs": 300, "test_fix.test.mjs": 300}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("script", [p.name for p in sorted(JS.glob("*.test.mjs"))])
def test_workflow_control_flow(script):
    proc = subprocess.run(["node", str(JS / script)], capture_output=True, text=True, timeout=SLOW_TIMEOUTS.get(script, 60))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "FAIL" not in proc.stdout
