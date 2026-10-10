"""Which model and effort each spec-workflow role uses.

Resolved from, lowest to highest precedence:
  1. built-in defaults (DEFAULTS below)
  2. global preferences: ~/.config/claude-kiro/config.json, key "agents"
     ($CK_CONFIG_HOME overrides the directory)
  3. the project's specs/ck.json, key "agents"
  4. overrides for one run (`ck agents --override role.key=value`, or the
     --model/--effort flags of /spec:implement)

Model values: an alias (sonnet, opus, haiku, fable; each means the newest model
of that family), a full claude-* ID, or "inherit" (use the session's model).
Effort values: low, medium, high, xhigh, max, or "inherit".
"""

import copy
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claude_kiro.paths import CONFIG_FILE

MODELS = ("haiku", "sonnet", "opus", "fable")  # ascending capability
EFFORTS = ("low", "medium", "high", "xhigh", "max")  # ascending
INHERIT = "inherit"

DEFAULT_ALLOWED_DOMAINS = (
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
)
_DOMAIN = re.compile(r"^(\*\.)?[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*(:\d+)?$")

DEFAULTS: Dict[str, Dict[str, Any]] = {
    # /spec:plan and /spec:create: if the session runs below this, ask me whether to switch.
    "planning": {"min_model": "opus", "min_effort": "medium", "ask": True},
    # Agents that write code for impl-track tasks (and their retries).
    # escalate: bigger models to try, in order, once a task's retries or review
    # rounds run out on the role's model (one extra attempt each). [] = off.
    "implementer": {"model": "sonnet", "effort": "medium", "escalate": []},
    # Agents that write test-track tasks. null = same as implementer.
    "test_writer": {"model": None, "effort": None, "escalate": None},
    # Reviews each task's diff against design.md and requirements.md before it merges.
    # rounds: how many times the implementer may revise after blocking findings.
    "reviewer": {"model": "opus", "effort": "high", "enabled": True, "rounds": 1},
    # Repairs a red target branch: reads the failing gate, decides environment vs code, fixes.
    # Judgment-heavy and rare (about once a run). null = same as implementer.
    # escalate: null = the implementer's list (models not above the fixer's are skipped).
    "fixer": {"model": "opus", "effort": "medium", "escalate": None},
    # Mechanical workflow steps: plan (`ck plan` + worktrees), conflict-free merges,
    # gates, re-checks. Their decisions are coded in the workflow; the agent runs a
    # command and reports.
    "orchestrator": {"model": "sonnet", "effort": "low"},
    # Resolves a merge conflict in the task worktree. A bad resolution lands broken
    # or weakened code on the target, so it gets a strong model.
    "resolver": {"model": "opus", "effort": "high"},
    # How many tasks may have an implementing agent at work at once (review and the
    # merge queue don't count). null = unlimited; lower it for memory-heavy test
    # suites or a tight usage budget.
    # allowed_domains: the only hosts shell commands may reach when `ck run` sandboxes
    # the session (package registries and GitHub). [] = no network at all.
    "run": {"max_concurrent": None, "allowed_domains": list(DEFAULT_ALLOWED_DOMAINS)},
}

ROLE_KEYS = {
    "planning": {"min_model", "min_effort", "ask"},
    "implementer": {"model", "effort", "escalate"},
    "test_writer": {"model", "effort", "escalate"},
    "reviewer": {"model", "effort", "enabled", "rounds"},
    "fixer": {"model", "effort", "escalate"},
    "orchestrator": {"model", "effort"},
    "resolver": {"model", "effort"},
    "run": {"max_concurrent", "allowed_domains"},
}
UNLIMITED = "unlimited"


class AgentConfigError(ValueError):
    pass


def global_config_path() -> Path:
    base = os.environ.get("CK_CONFIG_HOME") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "claude-kiro"
    )
    return Path(base) / "config.json"


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _valid_model(value: Any, allow_inherit: bool = True) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    return value in MODELS or value.startswith("claude-") or (allow_inherit and value == INHERIT)


def _valid_effort(value: Any, allow_inherit: bool = True) -> bool:
    return value is None or value in EFFORTS or (allow_inherit and value == INHERIT)


def _coerce(role: str, key: str, value: Any) -> Any:
    """Validate one setting; strings from the command line are converted."""
    if key not in ROLE_KEYS.get(role, set()):
        raise AgentConfigError(f"unknown setting {role}.{key} (known: {', '.join(sorted(ROLE_KEYS.get(role, ())) or '-')})")
    if key == "escalate" and isinstance(value, str) and value.strip().lower() in ("none", "off"):
        value = []  # an explicit "no escalation", so it can switch off a list saved in a lower layer
    if key == "allowed_domains" and isinstance(value, str) and value.strip().lower() in ("none", "off", "[]", ""):
        value = []  # no network for sandboxed commands
    if isinstance(value, str) and value.lower() in ("null", "none", "default"):
        value = None
    if key in ("ask", "enabled"):
        if isinstance(value, str):
            if value.lower() not in ("true", "false", "yes", "no", "on", "off"):
                raise AgentConfigError(f"{role}.{key} must be true or false")
            value = value.lower() in ("true", "yes", "on")
        if not isinstance(value, bool):
            raise AgentConfigError(f"{role}.{key} must be true or false")
        return value
    if key == "escalate":
        if value is None:
            return None
        if isinstance(value, str):
            value = [] if value.strip().lower() in ("", "off", "[]") else [m.strip() for m in value.split(",")]
        if not isinstance(value, list) or any(not isinstance(m, str) or not m or not _valid_model(m, allow_inherit=False) for m in value):
            raise AgentConfigError(f"{role}.escalate must be a comma-separated list of models ({', '.join(MODELS)} or claude-* IDs), or none")
        return value
    if key == "allowed_domains":
        if value is None:
            return None
        if isinstance(value, str):
            value = [d.strip() for d in value.split(",")]
        if not isinstance(value, list) or any(not isinstance(d, str) or not _DOMAIN.match(d) for d in value):
            raise AgentConfigError(f"{role}.allowed_domains must be a comma-separated list of host names (e.g. pypi.org,*.github.com), or none")
        return value
    if key == "max_concurrent":
        if value is None or value == UNLIMITED:
            return value
        try:
            number = int(str(value))
        except ValueError:
            number = 0
        if number < 1:
            raise AgentConfigError(f"{role}.max_concurrent must be a whole number of 1 or more, or {UNLIMITED}")
        return number
    if key == "rounds":
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise AgentConfigError(f"{role}.rounds must be a whole number")
        if value < 0:
            raise AgentConfigError(f"{role}.rounds must be 0 or more")
        return value
    if key in ("model", "min_model"):
        if not _valid_model(value, allow_inherit=key == "model"):
            raise AgentConfigError(f"{role}.{key}: unknown model {value!r} (use {', '.join(MODELS)}, a claude-* ID, or inherit)")
        return value
    if key in ("effort", "min_effort"):
        if not _valid_effort(value, allow_inherit=key == "effort"):
            raise AgentConfigError(f"{role}.{key}: unknown effort {value!r} (use {', '.join(EFFORTS)} or inherit)")
        return value
    return value


def _merge(into: Dict[str, Dict[str, Any]], layer: Any, source: str, sources: Dict[str, str]) -> None:
    if not isinstance(layer, dict):
        return
    for role, settings in layer.items():
        if role not in DEFAULTS or not isinstance(settings, dict):
            raise AgentConfigError(f"{source}: unknown agent role {role!r} (known: {', '.join(DEFAULTS)})")
        for key, value in settings.items():
            into[role][key] = _coerce(role, key, value)
            sources[f"{role}.{key}"] = source


def parse_override(text: str) -> Tuple[str, str, str]:
    """'reviewer.effort=xhigh' -> ('reviewer', 'effort', 'xhigh')."""
    if "=" not in text or "." not in text.split("=", 1)[0]:
        raise AgentConfigError(f"override {text!r} must look like role.key=value, e.g. implementer.model=haiku")
    path, value = text.split("=", 1)
    role, key = path.split(".", 1)
    return role.strip(), key.strip(), value.strip()


def resolve(project_dir: Path, overrides: Optional[List[str]] = None) -> Dict[str, Any]:
    """Effective settings per role, with where each value came from."""
    roles = copy.deepcopy(DEFAULTS)
    sources = {f"{r}.{k}": "default" for r, s in DEFAULTS.items() for k in s}
    _merge(roles, _read_json(global_config_path()).get("agents"), str(global_config_path()), sources)
    _merge(roles, _read_json(project_dir / CONFIG_FILE).get("agents"), CONFIG_FILE, sources)
    layer: Dict[str, Dict[str, Any]] = {}
    for text in overrides or []:
        role, key, value = parse_override(text)
        layer.setdefault(role, {})[key] = value
    _merge(roles, layer, "override", sources)

    # Roles that follow the implementer unless set.
    for role in ("test_writer", "fixer"):
        for key in ("model", "effort", "escalate"):
            if roles[role][key] is None:
                roles[role][key] = roles["implementer"][key]
                sources[f"{role}.{key}"] = f"implementer ({sources['implementer.' + key]})"
    if roles["run"]["allowed_domains"] is None:
        roles["run"]["allowed_domains"] = list(DEFAULT_ALLOWED_DOMAINS)
    if roles["run"]["max_concurrent"] == UNLIMITED:
        roles["run"]["max_concurrent"] = None
    return {"roles": roles, "sources": sources}


def write_setting(path: Path, role: str, key: str, value: str) -> Dict[str, Any]:
    """Set agents.<role>.<key> in a JSON config file (global preferences or specs/ck.json)."""
    if role not in DEFAULTS:
        raise AgentConfigError(f"unknown agent role {role!r} (known: {', '.join(DEFAULTS)})")
    coerced = _coerce(role, key, value)
    data = _read_json(path)
    agents = data.setdefault("agents", {})
    if not isinstance(agents, dict):
        raise AgentConfigError(f"{path}: \"agents\" must be an object")
    settings = agents.setdefault(role, {})
    if coerced is None:
        settings.pop(key, None)
        if not settings:
            agents.pop(role, None)
    else:
        settings[key] = coerced
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return data


def _rank(value: str, order: Tuple[str, ...]) -> Optional[int]:
    return order.index(value) if value in order else None


def model_family(model: str) -> Optional[str]:
    """'claude-opus-5-5' or 'opus' -> 'opus'; None for anything else."""
    text = (model or "").lower()
    for family in MODELS:
        if family in text:
            return family
    return None


def meets_planning_bar(model: str, effort: Optional[str], planning: Dict[str, Any]) -> Tuple[bool, str]:
    """Whether a session's model/effort is at least the planning minimum, and why not."""
    family = model_family(model)
    need_model, need_effort = planning.get("min_model"), planning.get("min_effort")
    if need_model and family and _rank(family, MODELS) < _rank(model_family(need_model) or need_model, MODELS):
        return False, f"model {model} is below {need_model}"
    if need_model and not family:
        return False, f"model {model!r} is not a known family; expected {need_model} or higher"
    if need_effort and effort and effort in EFFORTS and _rank(effort, EFFORTS) < _rank(need_effort, EFFORTS):
        return False, f"effort {effort} is below {need_effort}"
    return True, "ok"


# --- Agent types for running without the Workflow tool -----------------------
# The Agent tool can set a model per call but not an effort, so each role gets a
# Claude Code agent type (.claude/agents/ck-<name>.md) carrying both.

AGENTS_DIR = ".claude/agents"
GENERATED_MARK = "<!-- generated by `ck agents sync`; edits are overwritten. Change settings with `ck agents set`. -->"

# name -> (role, description, tools or None for all, system prompt)
AGENT_TYPES = {
    "ck-implementer": (
        "implementer",
        "claude-kiro spec task agent for impl-track tasks, their retries and revisions",
        None,
        "You implement one impl-track task of a claude-kiro spec, in the worktree or checkout your prompt names. "
        "Follow the operating brief your prompt points to exactly, and return the report it asks for.",
    ),
    "ck-test-writer": (
        "test_writer",
        "claude-kiro spec task agent for test-track tasks, their retries and revisions",
        None,
        "You implement one test-track task of a claude-kiro spec: tests against real boundaries, written before the code "
        "they verify. Follow the operating brief your prompt points to exactly, and return the report it asks for.",
    ),
    "ck-fixer": (
        "fixer",
        "claude-kiro agent that resolves a task's merge conflict or repairs a red target branch",
        None,
        "You repair a claude-kiro spec run: a merge conflict in a task worktree, or a target branch whose gate is red. "
        "Never weaken a test or edit requirements.md to get a pass; if that is the only way, stop and explain.",
    ),
    "ck-resolver": (
        "resolver",
        "claude-kiro agent that resolves a task branch's merge conflict in the task worktree",
        None,
        "You resolve one merge conflict in a claude-kiro spec run, inside the task worktree your prompt names, never on "
        "the target checkout. Keep both sides' intent: both sides' task sections in tasks.md, the union of dependency "
        "manifests (then re-lock), and every test and assertion from both sides. Read design.md and requirements.md "
        "when the sides disagree. The task's gate must pass in the worktree before you commit the merge.",
    ),
    "ck-reviewer": (
        "reviewer",
        "claude-kiro reviewer: checks a task's diff against design.md and requirements.md before it merges; never edits",
        "Read, Grep, Glob, Bash",
        "You review one task of a claude-kiro spec before it merges. Read the diff and the spec; never edit, commit, or "
        "run the test suite. Mark a finding blocking only if it should stop the merge, and return the verdict and "
        "findings your prompt asks for.",
    ),
}


def agent_type_files(roles: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """File name -> content of each ck agent type, for the resolved roles. `inherit` leaves the field out."""
    files = {}
    for name, (role, description, tools, body) in AGENT_TYPES.items():
        settings = roles[role]
        lines = ["---", f"name: {name}", f"description: {description}"]
        for key in ("model", "effort"):
            if settings.get(key) and settings[key] != INHERIT:
                lines.append(f"{key}: {settings[key]}")
        if tools:
            lines.append(f"tools: {tools}")
        lines += ["---", "", GENERATED_MARK, "", body, ""]
        files[f"{name}.md"] = "\n".join(lines)
    return files


def sync_agent_types(project_dir: Path, overrides: Optional[List[str]] = None, dry_run: bool = False) -> Dict[str, Any]:
    """Write .claude/agents/ck-*.md from the resolved roles.

    A file without GENERATED_MARK is the user's own and is left alone ("skipped").
    "created_dir" matters: Claude Code watches an existing agents directory, but
    a session started before the directory existed needs a restart to see it.
    """
    roles = resolve(project_dir, overrides)["roles"]
    target = project_dir / AGENTS_DIR
    result: Dict[str, Any] = {
        "dir": str(target),
        "created_dir": not target.is_dir(),
        "changed": [],
        "unchanged": [],
        "skipped": [],
    }
    for fname, content in agent_type_files(roles).items():
        path = target / fname
        current = path.read_text() if path.exists() else None
        if current == content:
            result["unchanged"].append(fname)
        elif current is not None and GENERATED_MARK not in current:
            result["skipped"].append(fname)
        else:
            result["changed"].append(fname)
            if not dry_run:
                target.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
    return result
