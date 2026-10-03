"""Hook execution runner for Claude Code integration."""

import importlib
from typing import Any, Dict, List, Optional


class HookRegistry:
    """Registry of available hooks for Claude Kiro."""

    _hooks: Dict[str, str] = {
        "post-file-ops": "claude_kiro.hooks.post_file_ops_spec_context:hook",
        "pre-tool-guard": "claude_kiro.hooks.pre_tool_guard:hook",
        "stop-verify": "claude_kiro.hooks.stop_verify:hook",
    }

    descriptions: Dict[str, str] = {
        "post-file-ops": "Injects spec task context after file edits; records tasks.md edits",
        "pre-tool-guard": "Asks before tests are weakened/deleted or requirements edited mid-implementation",
        "stop-verify": "Blocks stopping while a task marked Done fails the verification gate",
    }

    def get_hook(self, name: str) -> Optional[str]:
        """Get hook module path by name.

        Args:
            name: Hook name (e.g., 'post-file-ops')

        Returns:
            Module path string if hook exists, None otherwise
        """
        return self._hooks.get(name)

    def list_hooks(self) -> List[str]:
        """List all available hook names.

        Returns:
            List of hook names
        """
        return list(self._hooks.keys())



# Claude Code settings entries that `ck init` installs. Timeouts are in seconds.
HOOK_SETTINGS: List[Dict[str, Any]] = [
    {"event": "PreToolUse", "matcher": "Edit|Write|MultiEdit|Bash", "hook": "pre-tool-guard", "timeout": 30},
    {"event": "PostToolUse", "matcher": "Edit|Write|MultiEdit", "hook": "post-file-ops", "timeout": 10},
    {"event": "Stop", "matcher": None, "hook": "stop-verify", "timeout": 600},
    {"event": "SubagentStop", "matcher": None, "hook": "stop-verify", "timeout": 600},
]


def install_hook_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Add Claude Kiro hooks to a settings dict, replacing older ck entries only.

    Hooks from other tools are left untouched.
    """
    hooks = settings.setdefault("hooks", {})
    for spec in HOOK_SETTINGS:
        command = f"ck --hook {spec['hook']}"
        groups = [
            g
            for g in hooks.get(spec["event"], [])
            if not any(
                isinstance(h, dict)
                and (h.get("command", "") == command or "ckh-" in h.get("command", ""))
                for h in g.get("hooks", [])
            )
        ]
        group: Dict[str, Any] = {"hooks": [{"type": "command", "command": command, "timeout": spec["timeout"]}]}
        if spec["matcher"]:
            group = {"matcher": spec["matcher"], **group}
        groups.append(group)
        hooks[spec["event"]] = groups
    return settings


def configured_hooks(settings: Dict[str, Any]) -> Dict[str, List[str]]:
    """Map event -> ck hook names configured for it."""
    found: Dict[str, List[str]] = {}
    for event, groups in settings.get("hooks", {}).items():
        for group in groups if isinstance(groups, list) else []:
            for h in group.get("hooks", []) if isinstance(group, dict) else []:
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                if cmd.startswith("ck --hook "):
                    found.setdefault(event, []).append(cmd.split()[-1])
    return found



def execute_hook(
    hook_name: str, input_data: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Execute a hook by name with the provided input data.

    Args:
        hook_name: Name of the hook to execute
        input_data: JSON data to pass to the hook

    Returns:
        Hook output as dict, or None if no output

    Raises:
        ValueError: If hook is not found
        Exception: If hook execution fails
    """
    registry = HookRegistry()
    hook_path = registry.get_hook(hook_name)

    if not hook_path:
        raise ValueError(
            f"Hook '{hook_name}' not found. Available: {', '.join(registry.list_hooks())}"
        )

    # Parse module and function name
    if ":" in hook_path:
        module_path, func_name = hook_path.rsplit(":", 1)
    else:
        module_path = hook_path
        func_name = "hook"

    try:
        # Import the hook module
        module = importlib.import_module(module_path)

        # Get the hook function
        if not hasattr(module, func_name):
            raise ValueError(
                f"Hook module {module_path} does not have function '{func_name}'"
            )

        hook_func = getattr(module, func_name)

        # Execute the hook - it's now a clean function that takes dict and returns dict
        return hook_func(input_data)

    except ImportError as e:
        raise ImportError(f"Failed to import hook module {module_path}: {e}")
    except Exception as e:
        raise Exception(f"Hook execution failed: {e}")
