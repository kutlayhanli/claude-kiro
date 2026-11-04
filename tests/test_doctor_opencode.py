"""`ck doctor --harness opencode` delegates to the package's preflight script."""

from __future__ import annotations

import json
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
