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
    # Where `ck worktree` creates task and integration worktrees, relative to the
    # main checkout (or absolute). Keep it out of .claude/: Claude Code's Bash
    # sandbox makes .claude/ read-only. Add it to .gitignore (`ck init` does).
    "worktree_dir": ".ck/worktrees",
    # Minutes a task worktree claim holds without activity (the claim itself, a
    # `ck gate` run in it, a commit on its branch). After that it is stale: `ck
    # worktree create` releases it and `claim` takes it over; commits stay put.
    "claim_stale_minutes": 60,
    # Dependency install step for fresh worktrees (`ck worktree create --install`).
    # null = detected from lockfiles.
    "install": None,
    # Glob patterns for files whose bugs are costly and silent (send paths,
    # payment code, price tripwires, auth), e.g. ["**/gate.py", "**/send*.py"].
    # A task whose **Files:** match one is treated as **Risk:** safety (stronger
    # starting model, risky reviewer) even without the tag; `ck lint` warns.
    "risk_paths": [],
    # `ck worktree land` refuses a task branch that changes files Claude Code or git
    # run on their own (.claude/**, .mcp.json, git hook directories, specs/ck.json):
    # an unattended agent could otherwise plant a hook or a permission rule that runs
    # outside the sandbox. List globs here for a spec that legitimately edits them.
    "land": {"allow_paths": []},
    # Keys and values `ck lint --contracts` never flags in test files, though
    # design.md doesn't name them (fixture or framework fields, e.g. ["csrf_token"]).
    "contracts_ignore": [],
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
    "guard": {"tests": "ask", "requirements": "ask", "test_plan": "ask"},
    # Git ref the tamper check diffs against. null = merge-base with origin/HEAD,
    # main, or master, whichever exists.
    "base": None,
    # Test-track validation command(s), run on every test-track gate (Story 6).
    # Absent (no key) = no check; an explicit null is a config error.
    "test_track": {"validate": None},
    # Recorded-fixture globs to hash-lock (Story 15) and the label file.
    "fixtures": {"recorded": [], "labels": "specs/fixture-labels.json"},
}


class ConfigError(Exception):
    """A specs/ck.json value the strict readers refuse. `key` names the setting."""

    def __init__(self, key: str, message: Optional[str] = None):
        self.key = key
        super().__init__(message or f"specs/ck.json: {key} is invalid")


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
    # The default test_track.validate is None, which the strict reader treats as
    # an explicit null. Drop it unless the file declares the key, so an absent
    # key still reads as "no check".
    declared = user.get("test_track")
    if not (isinstance(declared, dict) and "validate" in declared):
        config["test_track"].pop("validate", None)
    return config


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def test_track_validate(config: Dict[str, Any]) -> List[str]:
    """The test-track validation command(s): [] when absent, else a list of strings.

    Raises ConfigError naming test_track.validate for any other value, null included.
    """
    section = config.get("test_track", {})
    if not isinstance(section, dict) or "validate" not in section:
        return []
    value = section["validate"]
    if isinstance(value, str):
        return [value]
    if _is_str_list(value):
        return list(value)
    raise ConfigError(
        "test_track.validate",
        "specs/ck.json: test_track.validate must be a string or a list of strings",
    )


def fixture_globs(config: Dict[str, Any]) -> List[str]:
    """The recorded-fixture globs: [] when absent, else a list of strings.

    Raises ConfigError naming fixtures.recorded for any other value, including a
    non-string member.
    """
    section = config.get("fixtures", {})
    if not isinstance(section, dict) or "recorded" not in section:
        return []
    value = section["recorded"]
    if _is_str_list(value):
        return list(value)
    raise ConfigError(
        "fixtures.recorded",
        "specs/ck.json: fixtures.recorded must be a list of glob strings",
    )


def _present(path: Path) -> bool:
    """A real file, not the empty read-only stub Claude Code's sandbox leaves at
    package.json, package-lock.json, yarn.lock... while a sandboxed session runs."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def detect_verify_commands(project_dir: Path) -> List[str]:
    """Best-guess test command for a new project's specs/ck.json."""
    if (project_dir / "pyproject.toml").exists() or (project_dir / "pytest.ini").exists():
        return ["uv run pytest -q"] if (project_dir / "uv.lock").exists() else ["pytest -q"]
    package_json = project_dir / "package.json"
    if _present(package_json):
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
    if _present(project_dir / "uv.lock"):
        return "uv sync --frozen -q"
    if _present(project_dir / "package-lock.json"):
        return "npm ci --silent"
    if _present(project_dir / "pnpm-lock.yaml"):
        return "pnpm install --frozen-lockfile"
    if _present(project_dir / "yarn.lock"):
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
