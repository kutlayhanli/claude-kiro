"""Collection health for `ck doctor`: format drift, auto-collect state, uncollected runs."""

import json
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Literal, Mapping, Optional, Sequence

from . import runfile


@dataclass(frozen=True)
class DoctorFinding:
    level: Literal["warning", "info"]
    message: str


def decide(newest: Optional[runfile.FormatCheck], auto_collect: bool, uncollected: int) -> List[DoctorFinding]:
    out: List[DoctorFinding] = []
    if newest is not None and not newest.ok:
        detail = "; ".join(newest.problems)
        out.append(DoctorFinding(
            "warning",
            "Run-file format of the newest ck run is not recognised"
            + (f" ({detail})" if detail else "")
            + ": ck stats cannot collect it - update ck"))
    if not auto_collect:
        out.append(DoctorFinding(
            "info", "Automatic run analytics is off (ck agents set run.analytics true)"))
    if uncollected > 0:
        out.append(DoctorFinding(
            "warning", f"{uncollected} finished ck run(s) not collected - run ck stats collect --all"))
    return out


def _start_ms(data: Any, path: Path) -> float:
    """Run start as epoch ms; falls back to the file's mtime when startTime is unusable."""
    st = data.get("startTime") if isinstance(data, dict) else None
    if isinstance(st, (int, float)) and not isinstance(st, bool):
        return float(st)
    if isinstance(st, str):
        text = st.strip()
        try:
            return float(text)
        except ValueError:
            pass
        try:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return when.timestamp() * 1000.0
        except ValueError:
            pass
    try:
        return path.stat().st_mtime * 1000.0
    except OSError:
        return 0.0


def doctor_findings(home: Path, auto_collect: bool, records: Sequence[Mapping]) -> List[DoctorFinding]:
    """Never raises: an unreadable Claude home just gives no run-based findings."""
    newest_check: Optional[runfile.FormatCheck] = None
    uncollected = 0
    try:
        have = {r.get("runId") for r in records if isinstance(r, Mapping)}
        newest_key = None
        newest_data: Any = None
        for p in runfile.iter_run_files(Path(home)):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            if not isinstance(data, dict) or not runfile.is_allowlisted(data.get("workflowName")):
                continue
            key = (_start_ms(data, p), p.stem)
            if newest_key is None or key > newest_key:
                newest_key, newest_data = key, data
            if p.stem not in have and runfile.check_format(data).ok and runfile.run_state(data) == "finished":
                uncollected += 1
        if newest_key is not None:
            newest_check = runfile.check_format(newest_data)
    except Exception:  # noqa: BLE001 - doctor must not fail on an unreadable home
        pass
    return decide(newest_check, auto_collect, uncollected)
