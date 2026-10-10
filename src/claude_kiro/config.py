"""Project configuration for verification and guards, stored in specs/ck.json."""

import copy
import re
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from claude_kiro.paths import CONFIG_FILE

DEFAULT_CONFIG: Dict[str, Any] = {
    # The project's full test suite. While a test-first spec is in progress its
    # test tasks land before the code they verify, so the full suite is
    # expected to be red; see verify_mode.
    "verify": [],
    # Checks that must pass at every gate, whatever the spec's progress
    # (lint, type check, smoke tests).
    "verify_always": [],
    # "auto": while the spec has unfinished test-track tasks, run only the
    # tests that should already pass (verify_always, the collection check, and
    # each test task's Verify commands once every task it Verifies is Done);
    # run `verify` once every task is Done. "full": always run `verify`.
    "verify_mode": "auto",
    # While a test-first spec is in progress, `ck gate <spec> --task N` runs only
    # that task's Verify and its verifying tests; `ck gate <spec>` re-runs every
    # suite that should already pass. true: every task gate re-runs them too.
    "task_regression": False,
    # Command that fails when any test file cannot be collected/imported.
    # null = derived from a pytest command in `verify`; "" disables the check.
    "collect": None,
    # Properties coverage: every P-n in a spec's test-plan.md ## Properties
    # section must be a key of some test file's PROPERTIES map.
    # "warn": report missing ones; "required": fail the gate; "off": skip.
    "properties": "warn",
    # Seconds allowed per verify command.
    "verify_timeout": 540,
    # Dependency install step for fresh worktrees (`ck worktree create --install`).
    # null = detected from lockfiles.
    "install": None,
    # Glob patterns for files whose bugs are costly and silent (send paths,
    # payment code, price tripwires, auth), e.g. ["**/gate.py", "**/send*.py"].
    # A task whose **Files:** match one is treated as **Risk:** safety (stronger
    # starting model, risky reviewer) even without the tag; `ck lint` warns.
    "risk_paths": [],
    # Extra glob patterns the spec-context hook never comments on.
    "context_ignore": [],
    # How test files are recognized. A path is a test file if any directory
    # segment is in `dirs` or its file name matches one of `files`.
    "tests": {
        "dirs": ["tests", "test", "__tests__", "spec", "e2e", "integration_tests"],
        "files": [
            "test_*.py",
            "*_test.py",
            "conftest.py",
            "*_test.go",
            "*.test.*",
            "*.spec.*",
            "*_spec.rb",
            "*Test.java",
            "*Tests.cs",
        ],
    },
    # Guard behavior: "ask" (prompt me), "deny" (block and tell Claude why), "off".
    "guard": {"tests": "ask", "requirements": "ask"},
    # Git ref the tamper check diffs against. null = merge-base with origin/HEAD,
    # main, or master, whichever exists.
    "base": None,
}


def load_config(project_dir: Path) -> Dict[str, Any]:
    """Load specs/ck.json merged over defaults. Missing or invalid file -> defaults."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    path = project_dir / CONFIG_FILE
    try:
        user = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return config
    if not isinstance(user, dict):
        return config

    for key, value in user.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(value)
        else:
            config[key] = value
    if isinstance(config["verify"], str):
        config["verify"] = [config["verify"]]
    return config


def detect_verify_commands(project_dir: Path) -> List[str]:
    """Best-guess test command for a new project's specs/ck.json."""
    if (project_dir / "pyproject.toml").exists() or (project_dir / "pytest.ini").exists():
        return ["uv run pytest -q"] if (project_dir / "uv.lock").exists() else ["pytest -q"]
    package_json = project_dir / "package.json"
    if package_json.exists():
        try:
            scripts = json.loads(package_json.read_text()).get("scripts", {})
        except (OSError, json.JSONDecodeError):
            scripts = {}
        if "test" in scripts:
            return ["npm test --silent"]
    if (project_dir / "go.mod").exists():
        return ["go test ./..."]
    if (project_dir / "Cargo.toml").exists():
        return ["cargo test"]
    return []


def collect_command(config: Dict[str, Any]) -> Optional[str]:
    """The test-collection check: configured, or derived from a pytest verify command."""
    configured = config.get("collect")
    if configured is not None:
        return configured or None
    for command in config.get("verify", []):
        match = re.match(r"^(.*?\bpytest)\b", command)
        if match:
            return f"{match.group(1)} --collect-only -q"
    return None


def install_command(config: Dict[str, Any], project_dir: Path) -> Optional[str]:
    """Dependency install step for a fresh worktree."""
    configured = config.get("install")
    if configured is not None:
        return configured or None
    if (project_dir / "uv.lock").exists():
        return "uv sync --frozen -q"
    if (project_dir / "package-lock.json").exists():
        return "npm ci --silent"
    if (project_dir / "pnpm-lock.yaml").exists():
        return "pnpm install --frozen-lockfile"
    if (project_dir / "yarn.lock").exists():
        return "yarn install --frozen-lockfile"
    return None


def initial_config(project_dir: Path) -> Dict[str, Any]:
    """The specs/ck.json that `ck init` writes."""
    return {
        "verify": detect_verify_commands(project_dir),
        "guard": copy.deepcopy(DEFAULT_CONFIG["guard"]),
        "base": None,
    }


def guard_mode(config: Dict[str, Any], kind: str) -> Optional[str]:
    """Return "ask", "deny", or None when the guard is off."""
    mode = str(config.get("guard", {}).get(kind, "ask")).lower()
    return mode if mode in ("ask", "deny") else None
