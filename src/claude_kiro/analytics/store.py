"""Local analytics data store: paths, private writes, records, ledger, collect log, salt, label."""

import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

_SCHEMA_RE = re.compile(r"^ck-run-local/\d+$")

# Number of unreadable record files skipped by the most recent load_records() call.
unreadable_count = 0


def _environ(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def data_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    e = _environ(env)
    if e.get("CK_DATA_HOME"):
        return Path(e["CK_DATA_HOME"])
    if e.get("XDG_DATA_HOME"):
        return Path(e["XDG_DATA_HOME"]) / "claude-kiro"
    return Path.home() / ".local" / "share" / "claude-kiro"


def runs_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    return data_dir(env) / "runs"


def record_path(run_id: str, env: Optional[Mapping[str, str]] = None) -> Path:
    return runs_dir(env) / f"{run_id}.json"


def open_manifests_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    return data_dir(env) / "runs-open"


def closed_manifests_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    return data_dir(env) / "runs-closed"


def manifest_path(run_id: str, env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    open_path = open_manifests_dir(env) / f"{run_id}.json"
    if open_path.is_file():
        return open_path
    closed_path = closed_manifests_dir(env) / f"{run_id}.json"
    if closed_path.is_file():
        return closed_path
    return None


def write_manifest(manifest: Mapping[str, Any], env: Optional[Mapping[str, str]] = None) -> Path:
    path = open_manifests_dir(env) / f"{manifest['runId']}.json"
    write_private_json(path, dict(manifest))
    return path


def read_manifest(run_id: str, env: Optional[Mapping[str, str]] = None) -> Tuple[Optional[dict], str]:
    path = manifest_path(run_id, env)
    if path is None:
        return None, "missing"
    where = "open" if path.parent == open_manifests_dir(env) else "closed"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, "malformed"
    if not isinstance(data, dict):
        return None, "malformed"
    return data, where


def iter_open_manifests(env: Optional[Mapping[str, str]] = None) -> List[Path]:
    d = open_manifests_dir(env)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.json"))


def close_manifest(run_id: str, closed_at: str, env: Optional[Mapping[str, str]] = None) -> None:
    open_path = open_manifests_dir(env) / f"{run_id}.json"
    data, _ = read_manifest(run_id, env)
    if data is None or manifest_path(run_id, env) != open_path:
        raise FileNotFoundError(f"no open manifest for run {run_id}")
    data["closedAt"] = closed_at
    write_private_json(open_path, data)
    closed_dir = closed_manifests_dir(env)
    _mkdir_private(closed_dir)
    os.replace(open_path, closed_dir / open_path.name)


def taken_run_ids(env: Optional[Mapping[str, str]] = None) -> set:
    taken = set()
    for d in (open_manifests_dir(env), closed_manifests_dir(env), runs_dir(env)):
        if d.is_dir():
            taken.update(p.stem for p in d.glob("*.json"))
    return taken


def _mkdir_private(path: Path) -> None:
    missing: List[Path] = []
    p = path
    while not p.exists() and p != p.parent:
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            continue
        os.chmod(d, 0o700)


def _write_private_text(path: Path, text: str) -> None:
    path = Path(path)
    _mkdir_private(path.parent)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    os.chmod(path, 0o600)


def write_private_json(path: Path, obj: Any) -> None:
    _write_private_text(Path(path), json.dumps(obj, indent=2, sort_keys=True) + "\n")


def load_records_counted(env: Optional[Mapping[str, str]] = None) -> Tuple[List[dict], int]:
    records: List[dict] = []
    bad = 0
    d = runs_dir(env)
    if not d.is_dir():
        return records, 0
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            bad += 1
            continue
        schema = data.get("schema") if isinstance(data, dict) else None
        if not isinstance(schema, str) or not _SCHEMA_RE.match(schema):
            bad += 1
            continue
        records.append(data)
    return records, bad


@dataclass(frozen=True)
class StoreRead:
    records: List[dict]
    malformed: List[str]      # file names of unreadable, non-object or schema-less record files
    error: Optional[str]      # "<path>: <OSError text>" when the store could not be listed


def _unreadable(d: Path, e: OSError) -> str:
    return f"record store unreadable: {d}: {e.strerror or e}"


def load_records_checked(env: Optional[Mapping[str, str]] = None) -> StoreRead:
    """Like load_records_counted, but an unreadable store is an error, never an empty store."""
    d = runs_dir(env)
    try:
        os.stat(d)
    except FileNotFoundError:
        try:
            os.lstat(d)
        except FileNotFoundError:
            return StoreRead([], [], None)
        except OSError as e:
            return StoreRead([], [], _unreadable(d, e))
        return StoreRead([], [], _unreadable(d, FileNotFoundError("dangling link")))
    except OSError as e:
        return StoreRead([], [], _unreadable(d, e))
    try:
        with os.scandir(d) as it:
            names = sorted(e.name for e in it if e.name.endswith(".json"))
    except OSError as e:
        return StoreRead([], [], _unreadable(d, e))
    records: List[dict] = []
    malformed: List[str] = []
    for name in names:
        p = d / name
        try:
            if not os.path.isfile(p):
                raise OSError("not a regular file")
            data = json.loads(p.read_text())
        except (OSError, ValueError, RecursionError, UnicodeDecodeError):
            malformed.append(name)
            continue
        schema = data.get("schema") if isinstance(data, dict) else None
        if not isinstance(schema, str) or not _SCHEMA_RE.match(schema):
            malformed.append(name)
            continue
        records.append(data)
    return StoreRead(records, malformed, None)


def load_records(env: Optional[Mapping[str, str]] = None) -> List[dict]:
    global unreadable_count
    records, unreadable_count = load_records_counted(env)
    return records


def _ledger_path(env: Optional[Mapping[str, str]] = None) -> Path:
    return data_dir(env) / "state.json"


def load_ledger(env: Optional[Mapping[str, str]] = None) -> Dict[str, dict]:
    ledger: Dict[str, dict] = {"exported": {}, "submitted": {}}
    try:
        data = json.loads(_ledger_path(env).read_text())
    except (OSError, ValueError):
        return ledger
    if isinstance(data, dict):
        for key in ("exported", "submitted"):
            if isinstance(data.get(key), dict):
                ledger[key] = data[key]
    return ledger


def save_ledger(ledger: Mapping[str, Any], env: Optional[Mapping[str, str]] = None) -> None:
    write_private_json(_ledger_path(env), ledger)


def append_collect_log(line: str, env: Optional[Mapping[str, str]] = None) -> None:
    path = data_dir(env) / "collect.log"
    _mkdir_private(path.parent)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(line.rstrip("\n") + "\n")
    os.chmod(path, 0o600)


def ensure_salt() -> bytes:
    from claude_kiro import agents

    return agents.ensure_salt()


def machine_label() -> Optional[str]:
    from claude_kiro import agents

    label = agents.resolve(Path.cwd())["roles"]["run"].get("machine_label")
    return label if isinstance(label, str) and label else None
