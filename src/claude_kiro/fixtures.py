"""Recorded-fixture hash lock and labels (Story 15).

`lock_decision` is the pure seam. The sealed lock lives in `<git-common-dir>/ck/<spec>/fixture-lock.json`
(`{"entries": {...}, "mac": ...}`, sealed with protect's HMAC key); labels live in the tree
(`fixtures.labels`, default `specs/fixture-labels.json`). No network.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import protect
from .config import ConfigError, fixture_globs

DEFAULT_LABELS = "specs/fixture-labels.json"
LOCK_NAME = "fixture-lock.json"


@dataclass(frozen=True)
class LockEntry:
    sha256: str
    task: str
    locked_at: str
    handcrafted: Optional[str] = None


@dataclass(frozen=True)
class Label:
    source: str  # "captured" | "handcrafted"
    reason: Optional[str] = None
    sha256: Optional[str] = None
    capture: Optional[Mapping[str, object]] = None


@dataclass(frozen=True)
class FixtureReport:
    new_locks: Tuple[str, ...] = ()
    drift: Tuple[Mapping[str, str], ...] = ()
    handcrafted: Tuple[Mapping[str, str], ...] = ()
    lock_tampered: bool = False
    unlabeled: Tuple[str, ...] = ()
    config_error: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "newLocks": list(self.new_locks),
            "drift": [dict(d) for d in self.drift],
            "handcrafted": [dict(h) for h in self.handcrafted],
            "lockTampered": self.lock_tampered,
            "unlabeled": list(self.unlabeled),
            "configError": self.config_error,
        }


def sha256_text(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- decision

def _is_handcrafted(label: Optional[Label]) -> bool:
    return label is not None and label.source == "handcrafted" and bool((label.reason or "").strip())


def lock_decision(lock: Mapping[str, LockEntry], changes: Mapping[str, Optional[str]],
                  labels: Mapping[str, Label], *, tampered: bool = False) -> FixtureReport:
    """`changes` maps each recorded-fixture path to its new sha256 (None: deleted).

    A drift entry's `task` is the lock entry's task (the recorder); the land path overrides it with the
    landing task (15.2).

    No entry -> new lock. A locked file whose content differs (or that was deleted) is drift, unless its
    label is handcrafted with a non-blank reason and a sha256 equal to the new content.
    """
    new_locks: List[str] = []
    drift: List[Dict[str, str]] = []
    handcrafted: List[Dict[str, str]] = []
    for path in sorted(changes):
        sha = changes[path]
        entry = lock.get(path)
        if entry is None:
            if sha is not None:
                new_locks.append(path)
            continue
        if sha == entry.sha256:
            continue
        label = labels.get(path)
        if sha is not None and _is_handcrafted(label) and label.sha256 == sha:
            handcrafted.append({"file": path, "task": entry.task, "reason": str(label.reason).strip()})
        else:
            drift.append({"file": path, "task": entry.task, "change": "deleted" if sha is None else "changed"})
    present = {p for p, s in changes.items() if s is not None} | {p for p in lock if p not in changes}
    unlabeled = sorted(
        p for p in present
        if not (labels.get(p) and labels[p].capture) and not _is_handcrafted(labels.get(p))
    )
    return FixtureReport(tuple(new_locks), tuple(drift), tuple(handcrafted), tampered, tuple(unlabeled))


# --------------------------------------------------------------------------- labels

def parse_labels(text: Optional[str]) -> Dict[str, Label]:
    """The label file's content -> {path: Label}; missing, blank or malformed entries are skipped."""
    if not text or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Label] = {}
    for path, raw in data.items():
        if not isinstance(raw, dict):
            continue
        source = raw.get("source")
        if source not in ("captured", "handcrafted"):
            source = "handcrafted" if raw.get("reason") is not None else "captured"
        reason, sha, capture = raw.get("reason"), raw.get("sha256"), raw.get("capture")
        out[str(path)] = Label(
            source=source,
            reason=reason if isinstance(reason, str) else None,
            sha256=sha if isinstance(sha, str) else None,
            capture=capture if isinstance(capture, dict) and capture else None,
        )
    return out


def labels_path(config: Mapping[str, Any]) -> str:
    section = config.get("fixtures")
    value = section.get("labels") if isinstance(section, dict) else None
    return value if isinstance(value, str) and value else DEFAULT_LABELS


def read_labels(project: Path, config: Mapping[str, Any]) -> Dict[str, Label]:
    try:
        return parse_labels((project / labels_path(config)).read_text())
    except OSError:
        return {}


def write_label(project: Path, config: Mapping[str, Any], path: str, reason: str, sha256: str) -> Path:
    """Record a handcrafted label. Raises ValueError when the existing label file is malformed (nothing is overwritten)."""
    target = project / labels_path(config)
    data: Dict[str, Any] = {}
    if target.exists():
        try:
            loaded = json.loads(target.read_text())
        except (OSError, ValueError) as e:
            raise ValueError(f"{target} is not valid JSON ({e}); fix it before labelling") from e
        if not isinstance(loaded, dict):
            raise ValueError(f"{target} is not a JSON object; fix it before labelling")
        data = loaded
    data[path] = {"source": "handcrafted", "reason": reason.strip(), "sha256": sha256}
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, target)
    return target


# --------------------------------------------------------------------------- sealed lock

def lock_path(root: Path, spec: str) -> Path:
    return protect._spec_dir(root, spec) / LOCK_NAME


def _entries_json(entries: Mapping[str, LockEntry]) -> Dict[str, Dict[str, object]]:
    return {p: {"sha256": e.sha256, "task": e.task, "locked_at": e.locked_at, "handcrafted": e.handcrafted}
            for p, e in entries.items()}


def read_lock(root: Path, spec: str) -> Tuple[Dict[str, LockEntry], bool]:
    """(entries, tampered). A missing lock file is empty and not tampered; an unreadable or unsealed one is tampered."""
    path = lock_path(root, spec)
    if not path.exists():
        return {}, False
    try:
        data = json.loads(path.read_text())
        raw = data["entries"]
        entries = {str(p): LockEntry(sha256=str(e["sha256"]), task=str(e["task"]),
                                     locked_at=str(e["locked_at"]),
                                     handcrafted=None if e.get("handcrafted") is None else str(e["handcrafted"]))
                   for p, e in dict(raw).items()}
        mac = str(data["mac"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}, True
    key = protect._read_key(root)
    if key is None:
        return entries, True
    ok = hmac.compare_digest(protect._seal(key, {"spec": spec, "entries": _entries_json(entries)}), mac)
    return entries, not ok


def write_lock(root: Path, spec: str, entries: Mapping[str, LockEntry]) -> None:
    key = protect.ensure_key(root)
    body = _entries_json(entries)
    protect._write_json(lock_path(root, spec),
                        {"entries": body, "mac": protect._seal(key, {"spec": spec, "entries": body})})


# --------------------------------------------------------------------------- globs

def recorded_files(config: Mapping[str, Any], paths: List[str]) -> List[str]:
    """The paths matching `fixtures.recorded`; [] when no globs. Raises ConfigError for a bad value."""
    globs = fixture_globs(dict(config))
    return sorted(p for p in paths if any(protect._glob(p, g) for g in globs))


__all__ = ["ConfigError", "FixtureReport", "Label", "LockEntry", "lock_decision", "parse_labels",
           "read_labels", "write_label", "read_lock", "write_lock", "recorded_files", "sha256_text"]
