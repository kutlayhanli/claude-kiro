"""Run the Node control-flow tests for the Workflow scripts (stubbed agents)."""

import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).parent / "js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("script", ["spec_create.test.mjs", "spec_implement.test.mjs"])
def test_workflow_control_flow(script):
    proc = subprocess.run(["node", str(JS / script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "FAIL" not in proc.stdout
