"""`ck doctor --harness opencode` delegates to the package's preflight script."""

from __future__ import annotations

import json
import re
from pathlib import Path

from bench_helpers import invoke


def _package(tmp_path: Path, *, exit_code: int = 0, body: str = "preflight ok") -> Path:
    package = tmp_path / "opencode-package"
    scripts = package / "scripts"
    scripts.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"name": "opencode-claude-kiro"}))
    script = scripts / "preflight.mjs"
    script.write_text(
        "import { stdout, exit } from 'node:process';\n"
        f"stdout.write({json.dumps(body + chr(10))});\n"
        "stdout.write(JSON.stringify({ok: true}) + '\\n');\n"
        f"exit({exit_code});\n"
    )
    return package


def test_doctor_opencode_runs_the_configured_package(tmp_path, monkeypatch):
    package = _package(tmp_path)
    monkeypatch.setenv("CK_OPENCODE_PLUGIN", str(package))
    monkeypatch.chdir(tmp_path)

    result = invoke("doctor", "--harness", "opencode")

    assert result.exit_code == 0
    assert "preflight ok" in result.output
    assert '{"ok": true}' not in result.output  # the JSON tail is for tooling, not the human report


def test_doctor_opencode_returns_the_preflight_failure(tmp_path, monkeypatch):
    package = _package(tmp_path, exit_code=1, body="preflight failed: node is too old")
    monkeypatch.setenv("CK_OPENCODE_PLUGIN", str(package))
    monkeypatch.chdir(tmp_path)

    result = invoke("doctor", "--harness", "opencode")

    assert result.exit_code == 1
    assert "preflight failed" in result.output


def test_doctor_opencode_names_the_fix_when_the_plugin_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("CK_OPENCODE_PLUGIN", raising=False)
    monkeypatch.chdir(tmp_path)

    result = invoke("doctor", "--harness", "opencode")

    assert result.exit_code == 1
    assert "CK_OPENCODE_PLUGIN" in result.output


ROLE_NAMES = ["implementer", "test_writer", "reviewer", "reviewer_risky", "fixer", "orchestrator", "resolver"]


def test_tc133_doctor_prints_each_roles_model_and_source(tmp_path, monkeypatch):
    package = Path(__file__).resolve().parents[1] / "opencode"
    project = tmp_path / "project"
    (project / "specs").mkdir(parents=True)
    (project / "specs" / "ck.json").write_text(json.dumps(
        {"opencode": {"roles": {"implementer": "opencode/claude-opus-5-5#high"}}}
    ))
    monkeypatch.setenv("CK_OPENCODE_PLUGIN", str(package))
    monkeypatch.setenv("CK_CONFIG_HOME", str(tmp_path / "global-config"))
    monkeypatch.chdir(project)

    result = invoke("doctor", "--harness", "opencode")

    # The exit code depends on the host (ck, vendor/ and agent files); the role lines do not.
    assert "ConfigError" not in result.output, result.output  # `roles` is a known opencode key
    lines = {}
    for role in ROLE_NAMES:
        found = [ln for ln in result.output.splitlines() if re.search(rf"\b{role}\b", ln)]
        assert found, f"{role} is not listed:\n{result.output}"
        lines[role] = found[0]
    assert "opencode/claude-opus-5-5#high" in lines["implementer"]
    assert re.search(r"\broles\b", lines["implementer"]), lines["implementer"]
    for role in ROLE_NAMES[1:]:
        assert not re.search(r"\broles\b", lines[role]), lines[role]
        assert re.search(r"\b(alias|inherit)\b", lines[role]), lines[role]
