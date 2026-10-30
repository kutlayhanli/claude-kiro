"""`ck-status-line`: one line per active run of the project, read from the progress files (stdlib only, no click).

It reads each run's `start` line, the last 8 KiB of its file and `<key>.eta.json`, and never simulates. Everything is
wrapped in `try/except BaseException`: it always exits 0 and writes nothing to stderr.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, List, Optional

_TAIL = 8192


def _current_dir(raw: str) -> str:
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    if isinstance(data, dict):
        ws = data.get("workspace")
        if isinstance(ws, dict) and isinstance(ws.get("current_dir"), str) and ws["current_dir"]:
            return ws["current_dir"]
        if isinstance(data.get("cwd"), str) and data["cwd"]:
            return data["cwd"]
    return os.getcwd()


def _first_line(path: Path) -> Optional[dict]:
    with open(path, "rb") as fh:
        line = fh.readline(65536)
    obj = json.loads(line.decode("utf-8"))
    return obj if isinstance(obj, dict) and obj.get("kind") == "start" else None


def _tail_events(path: Path) -> List[dict]:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - _TAIL))
        data = fh.read()
    lines = data.split(b"\n")
    if size > _TAIL:
        lines = lines[1:]  # the first line is probably cut
    out = []
    for ln in lines:
        try:
            obj = json.loads(ln.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _cache(path: Path) -> Optional[dict]:
    try:
        obj = json.loads(path.with_name(path.stem + ".eta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _lines(current_dir: str) -> List[str]:
    from claude_kiro import progress
    from claude_kiro.eta import decide

    now = progress.clock_now()
    inputs = []
    for path in progress.runs_for_project(Path(current_dir), now=now):
        try:
            start = _first_line(path)
            if start is None:
                continue
            tail = _tail_events(path)
            view = _cache(path)
            kind = str(start.get("runKind") or "implement")
            if kind == "create":
                phase = None
                for ev in tail:
                    if ev.get("kind") == "phase-start":
                        phase = ev.get("phase")
                    elif ev.get("kind") in ("phase-end", "step-end") and ev.get("phase") == phase:
                        phase = None
                inputs.append(decide.StatusInput(spec=str(start.get("spec")), run_kind="create",
                                                 phase=phase if isinstance(phase, str) else None, view=view))
                continue
            if view is not None and isinstance(view.get("merged"), int) and isinstance(view.get("open"), int):
                merged, total = view["merged"], view["merged"] + view["open"]
            else:
                total = start.get("total") if isinstance(start.get("total"), int) else 0
                merged = len({str(e.get("task")) for e in tail if e.get("kind") == "merge"})
            inputs.append(decide.StatusInput(spec=str(start.get("spec")), run_kind="implement", merged=merged,
                                             total=total, view=view))
        except Exception:  # noqa: BLE001 - an unreadable run is left out
            continue
    return decide.status_lines(inputs)


def main() -> int:
    try:
        try:
            raw = sys.stdin.read()
        except BaseException:  # noqa: BLE001
            raw = ""
        lines = _lines(_current_dir(raw))
        if lines:
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()
    except BaseException:  # noqa: BLE001 - always exit 0, nothing on stderr
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
