"""Project configuration for verification and guards, stored in specs/ck.json."""

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from claude_kiro.paths import CONFIG_FILE

DEFAULT_CONFIG: Dict[str, Any] = {
    # Commands that must pass before any implementation task can be marked Done.
    "verify": [],
    # Seconds allowed per verify command.
    "verify_timeout": 540,
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
