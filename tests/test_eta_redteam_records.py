"""Red team of 6.2 (size counts stay local) and 7.2 (bundled defaults are aggregates only).

Written from the requirements: try to push a size count into shareable output, and to push record-level data (epoch
timestamps, hex-like ids, number-like titles, this repository's own names) into the defaults file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Dict, List

import pytest

import eta_helpers as h
from eta_history import T, assert_aggregates_only, custom_history
from eta_strategies import SIZE_KEYS
from stats_helpers import ck_sub, flatten_paths, kiro_checkout, stats

pytestmark = pytest.mark.usefixtures("no_network")

REPO = Path(__file__).resolve().parent.parent
SECRET = "ZEPHYR-PAYROLL-SECRET"


# --------------------------------------------------------------------------- 6.2


def exported_texts(checkout: Path) -> Dict[Path, str]:
    return {p: p.read_text() for p in sorted((checkout / "analytics").rglob("*")) if p.is_file()}


def assert_no_size(checkout: Path) -> None:
    files = exported_texts(checkout)
    assert any(p.suffix == ".json" for p in files), "nothing was exported"
    for path, text in files.items():
        for key in SIZE_KEYS:
            assert key not in text, (path, key)
        if path.suffix == ".json":
            for p in flatten_paths(json.loads(text)):
                assert not re.search(r"(?i)(files|criteria)_?count", p), (path, p)


def assert_local_counts(store: Path) -> None:
    """The counts exist locally (so the absence from an export is a decision, not a missing feature)."""
    recs = [json.loads(p.read_text()) for p in (store / "claude-kiro" / "runs").glob("*.json")]
    assert recs and all(k in t for r in recs for t in r["tasks"] for k in SIZE_KEYS)


def build(tmp_path: Path, runs: int = 2):
    store = custom_history(tmp_path / "hist", lambda i: [T(files=3, criteria=4), T(files=5, criteria=2)], runs=runs)
    return store, h._pool_env(tmp_path / "hist")


def test_62_re_collect_after_tasks_md_edits_still_exports_no_counts(tmp_path):
    store, env = build(tmp_path)
    checkout, _bare = kiro_checkout(tmp_path)
    assert stats("export", "--to", str(checkout), "--yes", env=env).exit_code == 0
    assert_no_size(checkout)
    for tasks_md in sorted((tmp_path / "hist" / "projects").glob("*/specs/*/tasks.md")):
        tasks_md.write_text(tasks_md.read_text().replace("**Files:**\n", "**Files:**\n- `src/extra_one.py` - more\n- `src/extra_two.py` - more\n"))
    run_ids = []
    for rec in sorted((store / "claude-kiro" / "runs").glob("*.json")):
        run_ids.append(json.loads(rec.read_text())["runId"])
        rec.unlink()
    again = ck_sub(*h.COLLECT_ALL, env=env, cwd=tmp_path)
    assert again.exit_code == 0, again.output
    (tmp_path / "second").mkdir()
    checkout2, _ = kiro_checkout(tmp_path / "second")
    for run_id in run_ids:
        assert stats("export", "--to", str(checkout2), "--yes", "--re-export", run_id, env=env).exit_code == 0
    assert_no_size(checkout2)
    runs = sorted((store / "claude-kiro" / "runs").glob("*.json"))
    assert len(runs) == 2
    assert [t["filesCount"] for t in json.loads(runs[0].read_text())["tasks"]] == [5, 7]


def test_62_re_export_of_a_run_carries_no_counts(tmp_path):
    store, env = build(tmp_path, runs=1)
    checkout, _bare = kiro_checkout(tmp_path)
    assert stats("export", "--to", str(checkout), "--yes", env=env).exit_code == 0
    run_id = json.loads(next((store / "claude-kiro" / "runs").glob("*.json")).read_text())["runId"]
    out = stats("export", "--to", str(checkout), "--yes", "--re-export", run_id, env=env)
    assert out.exit_code == 0, out.output
    assert_no_size(checkout)
    assert_local_counts(store)
    assert not [k for k in SIZE_KEYS if k in out.output]


def test_62_hostile_file_names_in_tasks_md_do_not_reach_an_export(tmp_path):
    store, env = build(tmp_path, runs=1)
    for tasks_md in (tmp_path / "hist" / "projects").glob("*/specs/*/tasks.md"):
        tasks_md.write_text(tasks_md.read_text().replace("src/ledger/part", f"src/{SECRET}/part").replace("part1_1", f"part1_{SECRET}"))
    for rec in (store / "claude-kiro" / "runs").glob("*.json"):
        rec.unlink()
    assert ck_sub(*h.COLLECT_ALL, env=env, cwd=tmp_path).exit_code == 0
    checkout, _bare = kiro_checkout(tmp_path)
    assert stats("export", "--to", str(checkout), "--yes", env=env).exit_code == 0
    assert_no_size(checkout)
    assert_local_counts(store)
    assert SECRET not in "".join(exported_texts(checkout).values())


# --------------------------------------------------------------------------- 7.2


EPOCH_MS = 1_767_225_600_123
EPOCH_S = 1_767_225_600
HEX_INT = 0xDEADBEEFCAFE
HEX_ID = "deadbeefcafe0123"
NUMBER_TITLE = "1234567890123"


def real_names() -> List[str]:
    names = []
    for d in sorted((REPO / "specs").iterdir()):
        if d.is_dir():
            names.append(d.name)
            tm = d / "tasks.md"
            if tm.exists():
                names += re.findall(r"^### Task [^:\n]+: (.+)$", tm.read_text(), flags=re.M)
    return names


def defaults_over(tmp_path: Path, mutate: Callable[[dict], None]):
    xdg = h.install_pool(h.POOL_IMPL, tmp_path / "xdg")
    h.install_pool(h.POOL_CREATE, xdg)
    for path in sorted((xdg / "claude-kiro" / "runs").glob("*.json")):
        rec = json.loads(path.read_text())
        mutate(rec)
        path.write_text(json.dumps(rec, indent=2))
    target = tmp_path / "defaults.json"
    out = ck_sub(*h.DEFAULTS_CMD, "--out", str(target), env={"XDG_DATA_HOME": str(xdg)}, cwd=tmp_path)
    return out, target


def leaves(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from leaves(v)
    elif isinstance(node, list):
        for v in node:
            yield from leaves(v)
    else:
        yield node


def assert_clean(out, target: Path, forbidden: List[str]) -> None:
    assert "No such command" not in out.output
    if out.exit_code != 0:
        assert not target.exists()
        return
    text = target.read_text()
    payload = json.loads(text)
    assert_aggregates_only(payload)
    for value in leaves(payload):
        if isinstance(value, (int, float)):
            assert abs(value) < 1e6, f"record-sized number {value} in the defaults"
    for needle in forbidden:
        assert needle not in text, needle


def test_72_epoch_timestamps_in_numeric_fields_do_not_become_numbers(tmp_path):
    """Requirement 7.2: no timestamp or record-level value reaches the bundled defaults.

    Half the implement tasks carry an epoch (about 1.77e12 ms or 1.77e9 s) in a duration field. q100 is the max, so a
    build that takes quantiles over raw values copies the timestamp into the file. `assert_clean` takes 1e6 as the bound
    for any number in the defaults (no aggregate of minutes, ms waits, USD per minute or sizes comes near it); the build
    must drop such implausible values first, or refuse.
    """

    def mutate(rec):
        if rec["workflow"] != "spec-implement":
            return
        for k, task in enumerate(rec.get("tasks") or []):
            if k % 2 == 0:
                task["agentMinutes"] = EPOCH_MS
                task["reviewWaitMs"] = EPOCH_MS
                task["landingWaitMs"] = EPOCH_S
                task["toMergedMinutes"] = EPOCH_S
        rec["wallMs"] = EPOCH_MS

    out, target = defaults_over(tmp_path, mutate)
    assert_clean(out, target, ["1767225600", "176722560"])


def test_72_hex_like_ids_and_number_like_titles_stay_out(tmp_path):
    def mutate(rec):
        rec["runId"] = f"wf_{HEX_ID}"
        rec["sessionId"] = HEX_ID * 2
        for task in rec.get("tasks") or []:
            task["title"] = NUMBER_TITLE
            task["task"] = str(HEX_INT)
            task["fixes"] = HEX_INT

    out, target = defaults_over(tmp_path, mutate)
    assert_clean(out, target, [HEX_ID, str(HEX_INT), NUMBER_TITLE, "deadbeef"])


def test_72_this_repositorys_names_and_titles_stay_out(tmp_path):
    names = real_names()
    assert len(names) > 20

    def mutate(rec):
        for i, task in enumerate(rec.get("tasks") or []):
            task["title"] = names[i % len(names)]
        rec["project"] = {"name": names[0], "root": f"/work/{names[1]}"} if isinstance(rec.get("project"), dict) else names[0]

    out, target = defaults_over(tmp_path, mutate)
    assert_clean(out, target, [n for n in names if len(n) > 12])


def test_72_defaults_are_a_fixed_point_of_their_own_output(tmp_path):
    out, target = defaults_over(tmp_path, lambda rec: None)
    assert out.exit_code == 0, out.output
    first = target.read_text()
    again = ck_sub(*h.DEFAULTS_CMD, "--out", str(tmp_path / "again.json"), env={"XDG_DATA_HOME": str(tmp_path / "xdg")}, cwd=tmp_path)
    assert again.exit_code == 0, again.output
    assert (tmp_path / "again.json").read_text() == first
    assert_aggregates_only(json.loads(first))
