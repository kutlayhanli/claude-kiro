"""Run ETA: history model, simulation, decisions and the service that wires them.

`estimate`, `after_event` and `render` are exported lazily so that importing `claude_kiro.eta.decide` or
`claude_kiro.eta.history` stays cheap (no click, no service).
"""

from __future__ import annotations

from typing import Any

_LAZY = {"estimate": "service", "after_event": "service", "render": "service"}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(f"{__name__}.{module}"), name)


__all__ = sorted(_LAZY)
