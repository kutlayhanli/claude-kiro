"""Recorded-fixture hash lock and labels (Story 15): TC-134 to TC-138, TC-139 (a), TC-140, TC-141.

Real temporary git repos, real `ck worktree land`, the real `ck fixtures` CLI and the real `ck worktree audit`.
Nothing of the project's own is mocked.
"""

import json
from pathlib import Path
from typing import Any, List, Optional

import pytest

import unattended_helpers as h

GLOBS = ["tests/fixtures/recorded/**"]
PRICES = "tests/fixtures/recorded/prices.json"
CAPTURED = "tests/fixtures/recorded/captured.json"
LABELS = "specs/fixture-labels.json"
V1 = '{"AAPL": 4.15, "MSFT": 9.5}\n'
V2 = '{"AAPL": 4.15}\n'
V3 = '{"AAPL": 1.0}\n'


def _project(tmp_path: Path, globs: Any = GLOBS) -> Path:
    config = {} if globs is None else {"fixtures": {"recorded": globs}}
    return h.attr_project(tmp_path, config=config)


def _worktree(root: Path, n: str) -> Path:
    code, _, err = h.ck_text(root, "worktree", "create", "demo", n, "--base", "integrate/demo")
    assert code == 0, err
    return h.task_worktree(root, n)


def _commit(path: Path, files: dict, message: str) -> None:
    h._commit_in(path, files, message)


def _land(root: Path, n: str):
    code, data, err = h.ck_json(root, "worktree", "land", "demo", n, "--into", "integrate/demo", "--json")
    return code, data, err


def _task_entry(data: Any, n: str) -> dict:
    return next(t for t in data["tasks"] if str(t["task"]) == n)


def _land_file(root: Path, n: str, content: str, extra: Optional[dict] = None) -> Any:
    """Task `n` writes prices.json (plus `extra`) and lands."""
    wt = _worktree(root, n)
    files = {PRICES: content}
    files.update(extra or {})
    _commit(wt, files, f"task {n}")
    code, data, err = _land(root, n)
    assert data is not None, err
    return code, data


def _status(root: Path) -> dict:
    code, data, err = h.ck_json(root, "fixtures", "status", "demo", "--json")
    assert code == 0 and isinstance(data, dict), err
    return data


def _audit(root: Path) -> dict:
    code, data, err = h.ck_json(root, "worktree", "audit", "demo", "--json")
    assert isinstance(data, dict), (code, err)
    return data


def _paths(items: List[Any]) -> List[str]:
    return [i["path"] if isinstance(i, dict) and "path" in i else (i.get("file") if isinstance(i, dict) else i)
            for i in items]


def _as_list(locked: Any) -> List[Any]:
    if isinstance(locked, dict):
        return [{k: v} for k, v in locked.items()]
    return list(locked)


def _names_path(entry: Any, path: str) -> bool:
    if isinstance(entry, dict):
        if path in entry:
            return True
        return path in (entry.get("path"), entry.get("file"))
    return entry == path


def _sha_of(entry: Any) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    if "sha256" in entry:
        return entry["sha256"]
    for v in entry.values():
        if isinstance(v, dict) and "sha256" in v:
            return v["sha256"]
    return None


def _drift_files(items: List[Any]) -> List[str]:
    return [i["file"] if isinstance(i, dict) else i for i in items]


def _lock_files(root: Path) -> List[Path]:
    common = Path(h.raw(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    return sorted((common / "ck").rglob("fixture-lock.json"))


# --------------------------------------------------------------------------- TC-134


def test_tc134_first_landing_locks_the_sha256(tmp_path):
    import hashlib

    root = _project(tmp_path)
    code, data = _land_file(root, "1", V1)
    assert code == 0, data
    status = _status(root)
    digest = hashlib.sha256(V1.encode()).hexdigest()
    assert any(_names_path(e, PRICES) and _sha_of(e) == digest for e in _as_list(status["locked"])), status["locked"]
    assert len(_as_list(status["locked"])) == 1
    assert status["drift"] == []
    assert len(_lock_files(root)) == 1


# --------------------------------------------------------------------------- TC-135


def test_tc135_later_landing_reports_drift_in_land_and_audit(tmp_path):
    root = _project(tmp_path)
    _land_file(root, "1", V1)
    code, data = _land_file(root, "2", V2)
    entry = _task_entry(data, "2")
    drift = entry["fixtureDrift"]
    assert drift, entry
    assert [d["file"] for d in drift] == [PRICES]
    assert all(str(d["task"]) == "2" for d in drift)
    audit = _audit(root)
    assert PRICES in _drift_files(audit["fixtures"]["drift"])
    assert audit["clean"] is False


# --------------------------------------------------------------------------- TC-136


def test_tc136_handcrafted_mark_with_reason_is_not_drift(tmp_path):
    root = _project(tmp_path)
    _land_file(root, "1", V1)
    wt = _worktree(root, "2")
    (wt / PRICES).write_text(V2)
    code, out, err = h.ck_text(wt, "fixtures", "mark", PRICES, "--handcrafted", "trimmed to 3 rows")
    assert code == 0, err
    h.commit_all(wt, "task 2")
    code, data, err = _land(root, "2")
    assert data is not None, err
    assert _task_entry(data, "2").get("fixtureDrift", []) == []
    assert "trimmed to 3 rows" in json.dumps(_status(root))
    assert _drift_files(_audit(root)["fixtures"]["drift"]) == []


# --------------------------------------------------------------------------- TC-137


@pytest.mark.parametrize("reason", ["", "   ", "\n"], ids=["empty", "spaces", "newline"])
def test_tc137_blank_reasons_stay_drift(tmp_path, reason):
    root = _project(tmp_path)
    _land_file(root, "1", V1)
    wt = _worktree(root, "2")
    (wt / PRICES).write_text(V2)
    h.ck_text(wt, "fixtures", "mark", PRICES, "--handcrafted", reason)  # may refuse; the change is drift either way
    h.commit_all(wt, "task 2")
    code, data, err = _land(root, "2")
    assert data is not None, err
    assert [d["file"] for d in _task_entry(data, "2")["fixtureDrift"]] == [PRICES]
    assert PRICES in _drift_files(_audit(root)["fixtures"]["drift"])


# --------------------------------------------------------------------------- TC-138


def _edit_lock(root: Path, mutate) -> None:
    (lock,) = _lock_files(root)
    obj = json.loads(lock.read_text())
    mutate(obj["entries"])
    lock.write_text(json.dumps(obj))


def test_tc138_edited_lock_hash_is_reported_by_audit(tmp_path):
    root = _project(tmp_path)
    _land_file(root, "1", V1)

    def edit(entries):
        entries[PRICES]["sha256"] = "0" * 64

    _edit_lock(root, edit)
    audit = _audit(root)
    assert audit["fixtures"]["lockTampered"] is True
    assert audit["clean"] is False


def test_tc138_deleted_lock_entry_is_reported_by_audit(tmp_path):
    root = _project(tmp_path)
    _land_file(root, "1", V1)
    _edit_lock(root, lambda entries: entries.pop(PRICES))
    audit = _audit(root)
    assert audit["fixtures"]["lockTampered"] is True
    assert audit["clean"] is False


# --------------------------------------------------------------------------- TC-139 (a)


def test_tc139a_unlabeled_lists_fixtures_without_capture_or_handcrafted_label(tmp_path):
    root = _project(tmp_path)
    (root / "tests" / "fixtures" / "recorded").mkdir(parents=True)
    for name, text in (("prices.json", V1), ("captured.json", V2), ("hand.json", V3), ("bare.json", V1 + V2)):
        (root / "tests" / "fixtures" / "recorded" / name).write_text(text)
    # captured.json has capture metadata; hand.json is marked handcrafted; prices.json and bare.json have neither.
    code, _, err = h.ck_text(root, "fixtures", "mark", "tests/fixtures/recorded/hand.json",
                             "--handcrafted", "typed by hand")
    assert code == 0, err
    labels = json.loads((root / LABELS).read_text()) if (root / LABELS).exists() else {}
    labels[CAPTURED] = {"source": "captured", "capture": {"tool": "curl", "at": "2026-01-01T00:00:00Z",
                                                          "source": "https://example.test/prices"}}
    (root / LABELS).parent.mkdir(exist_ok=True)
    (root / LABELS).write_text(json.dumps(labels, indent=2) + "\n")
    # Commit on main and put integrate/demo at the same commit, so status passes whichever tree it reads.
    h.commit_all(root, "fixtures and labels")
    h.raw(root, "branch", "-f", "integrate/demo", "HEAD")
    status = _status(root)
    assert sorted(_paths(status["unlabeled"])) == ["tests/fixtures/recorded/bare.json", PRICES]


# --------------------------------------------------------------------------- TC-140


def test_tc140_no_globs_means_no_lock_and_no_fixture_items(tmp_path):
    root = _project(tmp_path, globs=None)
    before = h.snapshot_ck_state(root)
    wt = _worktree(root, "1")
    _commit(wt, {PRICES: V1}, "task 1")
    code, data, err = _land(root, "1")
    assert data is not None, err
    assert not _task_entry(data, "1").get("fixtureDrift")
    assert _lock_files(root) == []
    after = h.snapshot_ck_state(root)
    assert not [k for k in after if "fixture-lock" in k]
    assert not [k for k in set(after) - set(before) if "fixture" in k]
    audit = _audit(root)
    fixtures = audit.get("fixtures") or {}
    assert not fixtures.get("drift") and not fixtures.get("lockTampered") and not fixtures.get("handcrafted")
    assert PRICES not in json.dumps(audit)


def test_tc140_land_adds_no_fixture_state_file_beyond_what_landing_always_writes(tmp_path):
    with_globs = _project(tmp_path / "a")
    without = _project(tmp_path / "b", globs=None)
    for root in (with_globs, without):
        wt = _worktree(root, "1")
        _commit(wt, {PRICES: V1}, "task 1")
        _land(root, "1")
    locks_with = [k for k in h.snapshot_ck_state(with_globs) if k.endswith("fixture-lock.json")]
    locks_without = [k for k in h.snapshot_ck_state(without) if k.endswith("fixture-lock.json")]
    assert len(locks_with) == 1 and locks_without == []


# --------------------------------------------------------------------------- TC-141


@pytest.mark.parametrize("globs", [42, None, ["a", 1], {"x": "y"}], ids=["int", "null", "mixed", "object"])
def test_tc141_non_string_globs_are_a_config_error_from_land_and_status(tmp_path, globs):
    root = h.attr_project(tmp_path, config={"fixtures": {"recorded": globs}})
    wt = _worktree(root, "1")
    _commit(wt, {PRICES: V1}, "task 1")
    land = h.ck_run(root, "worktree", "land", "demo", "1", "--into", "integrate/demo", "--json")
    assert "fixtures.recorded" in land.stdout + land.stderr
    assert _lock_files(root) == []
    status = h.ck_run(root, "fixtures", "status", "demo", "--json")
    assert status.returncode != 0
    assert "fixtures.recorded" in status.stdout + status.stderr
