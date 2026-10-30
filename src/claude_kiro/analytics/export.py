"""Export planning, batch building, leak gate and writing (S19). No network call anywhere in this module."""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from claude_kiro.analytics import leak, share
from claude_kiro.analytics.submit import is_target_repo, remote_urls  # noqa: F401  (re-exported for callers)

_REL_RE = re.compile(r"^analytics/runs/\d{4}-\d{2}/[0-9a-f]{16}\.(json|md)$")


@dataclass(frozen=True)
class PendingFile:
    rel_path: str
    text: str
    run_id: str
    record_id: str
    is_note: bool


class SchemaViolation(ValueError):
    """A built shareable record violates the schema; raised before anything is written."""

    def __init__(self, paths: Sequence[str]):
        self.paths = tuple(paths)
        super().__init__("shareable record violates the schema (%s)" % ", ".join(self.paths))


@dataclass(frozen=True)
class ExportPlan:
    new: Tuple[str, ...]
    reexport: Tuple[str, ...]
    restore: Tuple[str, ...]
    nothing_new: bool
    # replace: still-present files of an unsubmitted earlier export of a re-exported or restored run; removed
    # before the new files are written (7.8, 10.10)
    replace: Tuple[str, ...] = ()
    # the local records to export, by run id (carried so build_batch needs only the plan)
    records: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)


def _run_id(rec: Mapping) -> Optional[str]:
    rid = rec.get("runId") if isinstance(rec, Mapping) else None
    return rid if isinstance(rid, str) and rid else None


def plan_export(records: Sequence[Mapping], ledger: Mapping, reexport: Sequence[str], checkout_id: str,
                present: FrozenSet[str]) -> ExportPlan:
    exported = ledger.get("exported", {}) or {}
    submitted = ledger.get("submitted", {}) or {}
    by_id: Dict[str, Mapping] = {}
    for rec in records:
        rid = _run_id(rec)
        if rid is not None:
            by_id[rid] = rec
    wanted = set(reexport)
    new: List[str] = []
    again: List[str] = []
    restore: List[str] = []
    replace: List[str] = []
    for rid in sorted(by_id):
        entry = exported.get(rid)
        if rid in wanted:
            again.append(rid)
        elif entry is None:
            new.append(rid)
            continue
        elif (isinstance(entry, Mapping) and entry.get("checkout_id") == checkout_id
              and entry.get("record_id") not in submitted
              and any(f not in present for f in entry.get("files", []))):
            restore.append(rid)
        else:
            continue
        if isinstance(entry, Mapping) and entry.get("checkout_id") == checkout_id \
                and entry.get("record_id") not in submitted:
            replace.extend(f for f in entry.get("files", []) if f in present)
    chosen = set(new) | set(again) | set(restore)
    return ExportPlan(tuple(new), tuple(again), tuple(restore), not chosen, tuple(replace),
                      {rid: by_id[rid] for rid in sorted(chosen)})


def build_batch(plan: ExportPlan, *, machine_label: Optional[str] = None,
                notes: Optional[Mapping[str, str]] = None) -> List[PendingFile]:
    notes = notes or {}
    files: List[PendingFile] = []
    for rid in list(plan.new) + list(plan.reexport) + list(plan.restore):
        local = plan.records[rid]
        record_id = share.new_record_id()
        record = share.build_shareable(local, record_id=record_id, machine_label=machine_label)
        problems = share.validate_shareable(record)
        if problems:
            raise SchemaViolation(problems)
        base = "analytics/runs/%s/%s" % (record["month"], record_id)
        text = json.dumps(record, indent=2, sort_keys=True) + "\n"
        files.append(PendingFile(base + ".json", text, rid, record_id, False))
        note = notes.get(rid)
        if isinstance(note, str) and note.strip():
            files.append(PendingFile(base + ".md", note, rid, record_id, True))
    return files


def leak_gate(files: Sequence[PendingFile], records: Sequence[Mapping], identity: Any,
              non_ck_names: Sequence[str]) -> "leak.Gate":
    from claude_kiro.analytics import guardlog

    terms = leak.build_terms(records, identity, non_ck_names, guardlog.leak_terms())
    hits = leak.scan({f.rel_path: f.text for f in files}, terms)
    return leak.decide(hits)


def _safe_dirs(checkout: Path) -> Path:
    root = Path(checkout).resolve()
    analytics = root / "analytics"
    runs = analytics / "runs"
    for d in (analytics, runs):
        if d.is_symlink():
            raise OSError("refusing to write through a symlinked directory: %s" % d.relative_to(root))
    return runs


def write_batch(checkout: Path, files: Sequence[PendingFile]) -> None:
    runs = _safe_dirs(checkout)
    root = Path(checkout).resolve()
    for f in files:
        if not _REL_RE.fullmatch(f.rel_path):
            raise ValueError("refusing path outside analytics/runs: %r" % f.rel_path)
    for f in files:
        dest = root / f.rel_path
        runs = _safe_dirs(checkout)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.parent.is_symlink() or not str(dest.parent.resolve()).startswith(str(runs.resolve())):
            raise OSError("refusing to write outside analytics/runs: %s" % f.rel_path)
        tmp = dest.with_name(".%s.tmp" % dest.name)
        tmp.write_text(f.text)
        os.replace(tmp, dest)


def remove_replaced(checkout: Path, plan: ExportPlan) -> None:
    """Remove the still-present files of the earlier unsubmitted export of re-exported or restored runs."""
    _safe_dirs(checkout)
    root = Path(checkout).resolve()
    for rel in plan.replace:
        if not _REL_RE.fullmatch(rel):
            continue
        try:
            (root / rel).unlink()
        except FileNotFoundError:
            pass


def record_exported(ledger: Dict[str, Any], files: Sequence[PendingFile], checkout_id: str,
                    now: Optional[datetime] = None) -> None:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    exported = ledger.setdefault("exported", {})
    by_run: Dict[str, List[PendingFile]] = {}
    for f in files:
        by_run.setdefault(f.run_id, []).append(f)
    for rid, group in by_run.items():
        first = group[0]
        exported[rid] = {
            "record_id": first.record_id,
            "month": first.rel_path.split("/")[2],
            "checkout_id": checkout_id,
            "files": sorted(g.rel_path for g in group),
            "note": any(g.is_note for g in group),
            "exported_at": stamp,
        }
