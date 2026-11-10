"""The harness registry: one table that `ck bench run` and `ck doctor --harness` look names up in (Story 17).

A harness is a `Harness` record: its name, `run_arm(cfg, arm, echo) -> dict`, `doctor(project_dir) -> int`,
`available() -> Optional[str]` (a reason when the harness cannot run here) and `check_options(cfg, runner)`, which
raises `BenchError` for options that only apply to the other harnesses. `claude` and `opencode` are registered
below. More harnesses come from modules named in `CK_HARNESS_MODULES` (comma or colon separated) or from the
`claude_kiro.harnesses` entry point group; a module registers its harnesses when it is imported.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

import click

from . import bench as _bench

ENTRY_POINT_GROUP = "claude_kiro.harnesses"
MODULES_ENV = "CK_HARNESS_MODULES"


class HarnessError(Exception):
    """A harness name that is not registered, or a harness module that cannot be loaded."""


def _no_option_checks(cfg: Dict[str, Any], runner: Optional[str]) -> None:
    return None


@dataclass(frozen=True)
class Harness:
    name: str
    run_arm: Callable[..., Dict[str, Any]]
    doctor: Callable[[Path], int]
    available: Callable[[], Optional[str]] = lambda: None
    check_options: Callable[[Dict[str, Any], Optional[str]], None] = _no_option_checks


_REGISTRY: Dict[str, Harness] = {}
_EXTRAS_LOADED = False


def register(harness: Harness) -> None:
    """Make `harness` selectable by its name in bench and doctor. Registering a name again replaces it."""
    if not isinstance(harness, Harness):
        raise TypeError(f"register() takes a Harness, not {type(harness).__name__}")
    _REGISTRY[harness.name] = harness


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


def _split_modules(value: str) -> List[str]:
    return [part.strip() for part in value.replace(":", ",").split(",") if part.strip()]


def _load_extras() -> None:
    """Import the modules from CK_HARNESS_MODULES and the entry points, once; they register on import."""
    global _EXTRAS_LOADED
    if _EXTRAS_LOADED:
        return
    _EXTRAS_LOADED = True
    for module in _split_modules(os.environ.get(MODULES_ENV, "")):
        try:
            importlib.import_module(module)
        except Exception as e:
            raise HarnessError(f"cannot load harness module {module!r}: {e}") from e
    eps = importlib.metadata.entry_points()
    group = eps.select(group=ENTRY_POINT_GROUP) if hasattr(eps, "select") else eps.get(ENTRY_POINT_GROUP, [])
    for ep in group:
        try:
            obj = ep.load()
        except Exception as e:
            raise HarnessError(f"cannot load harness entry point {ep.name!r}: {e}") from e
        if isinstance(obj, Harness):
            register(obj)
        elif callable(obj):
            obj()


def registered() -> Dict[str, Harness]:
    """Every registered harness by name, in registration order (claude and opencode first)."""
    _load_extras()
    return dict(_REGISTRY)


def names() -> List[str]:
    return list(registered())


def select(name: str, registry: Optional[Mapping[str, Harness]] = None) -> Harness:
    """The harness called `name`; the name must match a registered name exactly."""
    reg = registered() if registry is None else registry
    if name in reg:
        return reg[name]
    raise HarnessError(f'unknown harness "{name}" (registered: {", ".join(reg)})')


def get(name: str) -> Harness:
    return select(name)


class HarnessOption(click.Option):
    """A `--harness` option whose help lists the names registered when help is shown."""

    def __init__(self, *args: Any, base_help: str = "", **kwargs: Any):
        self._base_help = base_help
        super().__init__(*args, help=base_help, **kwargs)

    def get_help_record(self, ctx: click.Context):
        self.help = f"{self._base_help} (registered: {', '.join(names())})"
        return super().get_help_record(ctx)


# --- the two base harnesses, wrapping today's code and options ---------------------------------------------------

def _claude_run_arm(cfg: Dict[str, Any], arm: Dict[str, Any], echo: Callable[[str], None] = print) -> Dict[str, Any]:
    return _bench.run_arm(cfg, arm, echo=echo)


def _claude_doctor(project_dir: Path) -> int:
    from .cli.main import doctor_claude

    return doctor_claude(project_dir)


def _opencode_run_arm(cfg: Dict[str, Any], arm: Dict[str, Any], echo: Callable[[str], None] = print) -> Dict[str, Any]:
    return _bench.run_arm_opencode(cfg, arm, echo=echo)


def _opencode_doctor(project_dir: Path) -> int:
    from .cli.doctor_opencode import doctor_opencode

    return doctor_opencode(project_dir)


def _opencode_check_options(cfg: Dict[str, Any], runner: Optional[str]) -> None:
    if runner:
        raise _bench.BenchError("--runner is only for the Claude harness; OpenCode arms use opencode.bin in the config")
    if not cfg.get("opencode", {}).get("plugin") and not os.environ.get("CK_OPENCODE_PLUGIN"):
        raise _bench.BenchError("opencode arms need opencode.plugin in the bench config (or CK_OPENCODE_PLUGIN)")


register(Harness(name="claude", run_arm=_claude_run_arm, doctor=_claude_doctor))
register(Harness(name="opencode", run_arm=_opencode_run_arm, doctor=_opencode_doctor,
                 check_options=_opencode_check_options))
