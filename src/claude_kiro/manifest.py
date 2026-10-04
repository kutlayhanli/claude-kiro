"""Version stamp for the files ck manages in a project (.claude/ck-manifest.json).

`ck init` and `ck upgrade` record the ck version that wrote the managed files
and a hash of each. That lets a later `ck upgrade`:
- refuse to downgrade: an older ck must not replace files a newer ck wrote;
- tell when a managed file was edited by hand since ck last wrote it.

The manifest is meant to be committed with the other .claude/ files, so every
clone carries the stamp.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

MANIFEST_FILE = ".claude/ck-manifest.json"


def ck_version() -> str:
    try:
        from importlib.metadata import version

        return version("claude-kiro")
    except Exception:
        return "0.0.0"


def version_key(value: str) -> Tuple[int, ...]:
    """'0.4.1' -> (0, 4, 1); non-numeric parts are ignored ('0.5.0rc1' -> (0, 5, 0))."""
    parts = []
    for piece in str(value).split("."):
        match = re.match(r"\d+", piece)
        if not match:
            break
        parts.append(int(match.group(0)))
    return tuple(parts) or (0,)


def digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def read(project_dir: Path) -> Optional[Dict]:
    try:
        data = json.loads((project_dir / MANIFEST_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and "version" in data else None


def build(project_dir: Path, managed_paths) -> Dict:
    files = {}
    for rel in managed_paths:
        path = project_dir / rel
        if path.exists():
            files[rel] = digest(path.read_text())
    return {"version": ck_version(), "files": files}


def write(project_dir: Path, managed_paths) -> Dict:
    data = build(project_dir, managed_paths)
    path = project_dir / MANIFEST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return data


def newer_than_running(manifest: Optional[Dict]) -> bool:
    return bool(manifest) and version_key(manifest["version"]) > version_key(ck_version())


def edited_since_written(manifest: Optional[Dict], rel: str, current: str) -> bool:
    """True when ck recorded a hash for `rel` and the file no longer matches it."""
    recorded = (manifest or {}).get("files", {}).get(rel)
    return bool(recorded) and recorded != digest(current)
