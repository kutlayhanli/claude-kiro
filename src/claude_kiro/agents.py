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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claude_kiro.paths import CONFIG_FILE

MODELS = ("haiku", "sonnet", "opus", "fable")  # ascending capability
EFFORTS = ("low", "medium", "high", "xhigh", "max")  # ascending
INHERIT = "inherit"

DEFAULTS: Dict[str, Dict[str, Any]] = {
    # /spec:plan and /spec:create: if the session runs below this, ask me whether to switch.
    "planning": {"min_model": "opus", "min_effort": "medium", "ask": True},
    # Agents that write code for impl-track tasks (and their retries).
    "implementer": {"model": "sonnet", "effort": "medium"},
    # Agents that write test-track tasks. null = same as implementer.
    "test_writer": {"model": None, "effort": None},
    # Reviews each task's diff against design.md and requirements.md before it merges.
    # rounds: how many times the implementer may revise after blocking findings.
    "reviewer": {"model": "opus", "effort": "high", "enabled": True, "rounds": 1},
    # Repairs a red target branch. null = same as implementer.
    "fixer": {"model": None, "effort": None},
}

ROLE_KEYS = {
    "planning": {"min_model", "min_effort", "ask"},
    "implementer": {"model", "effort"},
    "test_writer": {"model", "effort"},
    "reviewer": {"model", "effort", "enabled", "rounds"},
    "fixer": {"model", "effort"},
}


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
        for key in ("model", "effort"):
            if roles[role][key] is None:
                roles[role][key] = roles["implementer"][key]
                sources[f"{role}.{key}"] = f"implementer ({sources['implementer.' + key]})"
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
