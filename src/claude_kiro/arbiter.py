"""Stateful side of the arbiter (run-principles Task 32): lane tickets.

A lane ticket `<checkout>/.ck/arbiter-lane/<spec>.json` holds `{"spec", "openedAt"}`. While it exists
the guard lets the /spec:create arbiter edit within its lane (design.md, 11.5, 11.7).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

LANE_DIR_REL = Path(".ck") / "arbiter-lane"


def lane_ticket_path(checkout: Path, spec: str) -> Path:
    return Path(checkout) / LANE_DIR_REL / f"{spec}.json"


def open_lane(checkout: Path, spec: str, now: Optional[datetime] = None) -> Path:
    """Write the lane ticket for SPEC in CHECKOUT and return its path."""
    path = lane_ticket_path(checkout, spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps({"spec": spec, "openedAt": stamp}), encoding="utf-8")
    return path


def close_lane(checkout: Path, spec: str) -> bool:
    """Remove the lane ticket. True when one was removed; a missing ticket is not an error."""
    path = lane_ticket_path(checkout, spec)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
