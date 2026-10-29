"""Red team of the SHALL NOT clauses of Story 6 (requirements 6.2 and 6.7).

* 6.2: `ck doctor` SHALL NOT report open manifests that are 6 hours old or younger.
* 6.7: `ck stats collect --all` SHALL collect only runs that have a (stale) manifest: it SHALL NOT collect marked agents
  without a manifest and SHALL NOT run the backfill heuristic.

Every case runs the real `ck` CLI (`ck doctor`, `ck stats collect --all`) over a fake Claude home and a real store.
Ages are set by backdating `startTime` (the manifest's only clock field); the clock is never mocked. Each 6.2 case ends
with a positive control (one manifest backdated past 6 h IS reported, exactly once) so a crashed or silent doctor cannot
pass. Written from requirements.md and design.md only.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List

import pytest

from stats_helpers import (
    OLD_DESCRIPTIONS,
    SPEC_TYPES,
    FakeClaudeHome,
    ToolAgentSpec,
    backdate_manifest,
    backfill_home,
    begin,
    ck_sub,
    fresh_env,
    iso,
    land,
    set_manifest,
    spec_repo,
    stats,
    store_files,
)

GHOST = "ag_deadbeef"


def _setup(env):
    """A spec repo initialised for ck (so `ck doctor` has a project), a fake Claude home with one session."""
    repo = spec_repo(env.root / "repo", "demo", "integrate/demo", tasks=3)
    for n in (1, 2, 3):
        land(repo, "demo", "integrate/demo", n)
    init = ck_sub("init", "--no-allow-workflow", cwd=repo, env=env)
    assert init.exit_code == 0, init.output
    session = FakeClaudeHome(env.claude).session(cwd=repo)
    return repo, session


def _begin(env, repo, kind: str = "create"):
    return begin(env, repo, kind=kind, spec="demo", into="integrate/demo")


def _open_dir(env) -> Path:
    return env.xdg / "claude-kiro" / "runs-open"


def _closed_dir(env) -> Path:
    return env.xdg / "claude-kiro" / "runs-closed"


def _records(env) -> List[str]:
    d = env.xdg / "claude-kiro" / "runs"
    return sorted(p.name for p in d.glob("*.json")) if d.exists() else []


def _clone(env, source: Path, run_id: str, **keys) -> Path:
    """Another open manifest with the same content as `source` (runId = file stem = `run_id`) and `keys` replaced."""
    data = json.loads(source.read_text())
    data.update({"runId": run_id, **keys})
    out = _open_dir(env) / f"{run_id}.json"
    out.write_text(json.dumps(data))
    return out


def _doctor(env, repo):
    return ck_sub("doctor", cwd=repo, env=env)


def _stale_reported(output: str) -> int:
    """How many stale agent-mode runs `ck doctor` reports (0 when the warning is absent)."""
    m = re.search(r"(\d+) agent-mode run", output)
    return int(m.group(1)) if m else 0


def _bullets(output: str, heading: str) -> List[str]:
    m = re.search(rf"{re.escape(heading)}\n((?:  - .*\n?)*)", output)
    return m.group(1).splitlines() if m else []


def _count(output: str, label: str) -> int:
    m = re.search(rf"^{re.escape(label)}:\s*(\d+)\s*$", output, re.M)
    return int(m.group(1)) if m else 0


def _assert_quiet(env, repo, baseline, *, why: str) -> None:
    out = _doctor(env, repo)
    assert _stale_reported(out.output) == 0, f"6.2: {why}: doctor reported it:\n{out.output}"
    assert "agent-mode" not in out.output, f"6.2: {why}: doctor mentions agent-mode runs:\n{out.output}"
    assert out.exit_code == baseline.exit_code
    assert _bullets(out.output, "Warnings:") == _bullets(baseline.output, "Warnings:"), f"6.2: {why}: warnings changed"
    assert _bullets(out.output, "Issues found:") == _bullets(baseline.output, "Issues found:")


def _assert_control_is_reported_once(env, repo, source: Path) -> None:
    """Positive control: one manifest 6 h 30 m old is the only one reported, and its spec/root/branch are not named."""
    stale_id = "ag_5a1e0001"
    _clone(env, source, stale_id, startTime=iso(datetime.now(timezone.utc) - timedelta(hours=6, minutes=30)))
    out = _doctor(env, repo)
    assert _stale_reported(out.output) == 1, f"6.1 control: expected exactly one stale run:\n{out.output}"
    assert "ck stats collect --all" in out.output
    assert "integrate/demo" not in out.output and str(repo) not in out.output


# --------------------------------------------------------------------------- 6.2: doctor stays quiet about young manifests


def _tz(hours: float):
    return timezone(timedelta(hours=hours))


YOUNG_START: Dict[str, Callable[[datetime], str]] = {
    "z-5h59": lambda now: (now - timedelta(hours=5, minutes=59)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "plus-02-offset-5h59": lambda now: (now - timedelta(hours=5, minutes=59)).astimezone(_tz(2)).isoformat(timespec="seconds"),
    "minus-05-offset-5h59": lambda now: (now - timedelta(hours=5, minutes=59)).astimezone(_tz(-5)).isoformat(timespec="seconds"),
    "minus-12-offset-1h": lambda now: (now - timedelta(hours=1)).astimezone(_tz(-12)).isoformat(timespec="seconds"),
    "plus-14-offset-1h": lambda now: (now - timedelta(hours=1)).astimezone(_tz(14)).isoformat(timespec="seconds"),
    "half-hour-offset-3h": lambda now: (now - timedelta(hours=3)).astimezone(_tz(5.5)).isoformat(timespec="milliseconds"),
    "zero-offset-spelled-out": lambda now: (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
    "fractional-microseconds": lambda now: (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    "fractional-milliseconds": lambda now: (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S.") + "123Z",
    "fractional-five-digits": lambda now: (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S.") + "12345Z",
    "fractional-with-offset": lambda now: (now - timedelta(hours=5)).astimezone(_tz(-3)).isoformat(timespec="microseconds"),
    "no-seconds": lambda now: (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%MZ"),
    "just-now": lambda now: now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "future-1-minute": lambda now: (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "future-1-hour": lambda now: (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "future-10-days": lambda now: (now + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "future-400-days": lambda now: (now + timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "future-with-negative-offset": lambda now: (now + timedelta(hours=2)).astimezone(_tz(-5)).isoformat(timespec="seconds"),
}


@pytest.mark.parametrize("name", sorted(YOUNG_START))
def test_62_open_manifest_6h_old_or_younger_is_not_reported(name):
    """6.2: one open manifest whose `startTime` is written in a hard surface form; quiet, then a stale control is reported."""
    with fresh_env() as env:
        repo, _session = _setup(env)
        baseline = _doctor(env, repo)
        run_id, manifest = _begin(env, repo)
        set_manifest(manifest, startTime=YOUNG_START[name](datetime.now(timezone.utc)))
        _assert_quiet(env, repo, baseline, why=name)
        _assert_control_is_reported_once(env, repo, manifest)


def test_62_many_young_manifests_at_once_are_not_reported_and_do_not_add_to_a_stale_count():
    """6.2: forty young manifests (every kind) stay quiet; with one stale manifest the count is 1, not 41."""
    with fresh_env() as env:
        repo, _session = _setup(env)
        baseline = _doctor(env, repo)
        _run_id, manifest = _begin(env, repo)
        now = datetime.now(timezone.utc)
        for i in range(40):
            minutes = (i * 8) % (5 * 60 + 58)
            _clone(env, manifest, f"ag_{i:08x}", kind=("create", "review", "implement")[i % 3],
                   startTime=iso(now - timedelta(minutes=minutes)))
        _assert_quiet(env, repo, baseline, why="forty young manifests")
        _assert_control_is_reported_once(env, repo, manifest)


def test_62_start_time_copied_from_a_closed_manifest_is_judged_on_its_own_age():
    """6.2: an open manifest sharing a closed manifest's `startTime` is young, and the closed one is not counted."""
    with fresh_env() as env:
        repo, session = _setup(env)
        baseline = _doctor(env, repo)
        closed_id, _m = _begin(env, repo)
        session.tool_agents([ToolAgentSpec(f"[ck:{closed_id}] implement task 1", agent_type="general-purpose", agent_id="aclosed00000000001")])
        assert stats("collect", "--agents", closed_id, env=env, cwd=repo).exit_code == 0
        closed = _closed_dir(env) / f"{closed_id}.json"
        assert closed.exists()
        _open_id, manifest = _begin(env, repo)
        set_manifest(manifest, startTime=json.loads(closed.read_text())["startTime"])
        _assert_quiet(env, repo, baseline, why="startTime copied from a closed manifest")
        _assert_control_is_reported_once(env, repo, manifest)


def test_62_closed_manifest_backdated_after_closing_is_not_reported():
    """6.2/6.5: closing is what removes a manifest from the open set; its age afterwards is irrelevant."""
    with fresh_env() as env:
        repo, session = _setup(env)
        baseline = _doctor(env, repo)
        run_id, manifest = _begin(env, repo)
        session.tool_agents([ToolAgentSpec(f"[ck:{run_id}] implement task 1", agent_type="general-purpose", agent_id="aclosed00000000001")])
        assert stats("collect", "--agents", run_id, env=env, cwd=repo).exit_code == 0
        closed = _closed_dir(env) / f"{run_id}.json"
        backdate_manifest(closed, 30)
        _assert_quiet(env, repo, baseline, why="a closed manifest backdated after closing")
        fresh_id, fresh_manifest = _begin(env, repo)
        _assert_control_is_reported_once(env, repo, fresh_manifest)


def test_62_a_young_manifest_is_still_not_reported_after_a_failed_collect_leaves_it_open():
    """6.2/3.12: a manual collect on a young run with no agents exits 1 and leaves the manifest open; doctor stays quiet."""
    with fresh_env() as env:
        repo, _session = _setup(env)
        baseline = _doctor(env, repo)
        run_id, manifest = _begin(env, repo)
        out = stats("collect", "--agents", run_id, env=env, cwd=repo)
        assert out.exit_code == 1 and manifest.exists()
        _assert_quiet(env, repo, baseline, why="young manifest left open by a failed collect")
        _assert_control_is_reported_once(env, repo, manifest)


def test_62_collect_all_leaves_young_manifests_open_and_unreported():
    """6.2/6.3: `--all` never closes a young manifest, even when marked agents exist for it; doctor stays quiet afterwards."""
    with fresh_env() as env:
        repo, session = _setup(env)
        baseline = _doctor(env, repo)
        run_id, manifest = _begin(env, repo)
        session.tool_agents([ToolAgentSpec(f"[ck:{run_id}] implement task 1", agent_type="general-purpose", agent_id="ayoung00000000001")])
        set_manifest(manifest, startTime=YOUNG_START["plus-02-offset-5h59"](datetime.now(timezone.utc)))
        out = stats("collect", "--all", env=env, cwd=repo)
        assert out.exit_code == 0, out.output
        assert manifest.exists() and _records(env) == []
        assert _count(out.output, "unfinished") == 1, out.output
        _assert_quiet(env, repo, baseline, why="young manifest after collect --all")


# --------------------------------------------------------------------------- 6.7: --all collects only runs with a manifest


def _flip_last_hex(run_id: str) -> str:
    return run_id[:-1] + ("0" if run_id[-1] != "0" else "1")


def _marked(run_id: str, agent_id: str, rest: str = "implement task 1") -> ToolAgentSpec:
    return ToolAgentSpec(f"[ck:{run_id}] {rest}", agent_type="general-purpose", agent_id=agent_id)


def test_67_marked_agents_without_any_manifest_are_not_collected_by_all_but_are_by_their_id():
    """6.7: a well-formed marker with no manifest is left alone by `--all`; `--agents <id>` still collects it (control)."""
    with fresh_env() as env:
        repo, session = _setup(env)
        session.tool_agents([_marked(GHOST, "aghost00000000001"), _marked(GHOST, "aghost00000000002", "review task 1 round 1")])
        out = stats("collect", "--all", env=env, cwd=repo)
        assert out.exit_code == 0, out.output
        assert _records(env) == [], "6.7: `--all` collected marked agents that have no manifest"
        by_id = stats("collect", "--agents", GHOST, "--json", env=env, cwd=repo)
        assert by_id.exit_code == 0, by_id.output
        assert _records(env) == [f"{GHOST}.json"]


def test_67_marked_agents_with_the_id_of_a_closed_manifest_are_not_collected_by_all():
    """6.7/6.3: new agents appear for a run whose manifest is closed; `--all` (open manifests only) leaves its record as is."""
    with fresh_env() as env:
        repo, session = _setup(env)
        run_id, manifest = _begin(env, repo)
        session.tool_agents([_marked(run_id, "aclosed00000000001")])
        assert stats("collect", "--agents", run_id, env=env, cwd=repo).exit_code == 0
        record = env.xdg / "claude-kiro" / "runs" / f"{run_id}.json"
        before = record.read_bytes()
        session.tool_agents([_marked(run_id, "aclosed00000000002", "review task 1 round 1")], start="2026-10-12T09:40:00Z")
        out = stats("collect", "--all", env=env, cwd=repo)
        assert out.exit_code == 0, out.output
        assert record.read_bytes() == before, "6.7: `--all` re-collected a run whose manifest was closed"
        assert (_closed_dir(env) / f"{run_id}.json").exists() and not (_open_dir(env) / f"{run_id}.json").exists()
        assert _records(env) == [f"{run_id}.json"]


def test_67_marker_differing_from_a_stale_manifest_by_one_hex_digit_is_not_collected():
    """6.7: a stale manifest with no agents next to agents marked with a near-miss id: abandoned, nothing collected."""
    with fresh_env() as env:
        repo, session = _setup(env)
        run_id, manifest = _begin(env, repo)
        near = _flip_last_hex(run_id)
        assert near != run_id
        session.tool_agents([_marked(near, "anear000000000001"), _marked(near, "anear000000000002", "review task 1 round 1")])
        backdate_manifest(manifest, 7)
        out = stats("collect", "--all", env=env, cwd=repo)
        assert out.exit_code == 0, out.output
        assert _count(out.output, "abandoned") == 1, out.output
        assert _records(env) == [], "6.7: agents marked with a near-miss id were collected"
        assert not manifest.exists() and (_closed_dir(env) / f"{run_id}.json").exists()
        assert stats("collect", "--agents", near, env=env, cwd=repo).exit_code == 0  # control: collectable by its own id


def test_67_near_miss_marked_agents_are_not_mixed_into_a_stale_manifests_record():
    """6.7/3.17: the stale run has its own marked agent; agents with a one-digit-off id stay out of its record."""
    with fresh_env() as env:
        repo, session = _setup(env)
        run_id, manifest = _begin(env, repo)
        near = _flip_last_hex(run_id)
        session.tool_agents([_marked(run_id, "aown0000000000001"), _marked(near, "anear000000000001")])
        backdate_manifest(manifest, 7)
        out = stats("collect", "--all", env=env, cwd=repo)
        assert out.exit_code == 0, out.output
        assert _records(env) == [f"{run_id}.json"]
        rec = json.loads((env.xdg / "claude-kiro" / "runs" / f"{run_id}.json").read_text())
        assert sorted(a["agentId"] for a in rec["agents"]) == ["aown0000000000001"]


def test_67_stale_manifest_whose_run_id_disagrees_with_its_file_name_collects_nothing():
    """6.7/6.6: runs-open/<A>.json holding runId <B> is malformed; neither A's nor B's marked agents are collected."""
    with fresh_env() as env:
        repo, session = _setup(env)
        run_id, manifest = _begin(env, repo)
        other = "ag_0b0b0b0b"
        data = json.loads(manifest.read_text())
        data["runId"] = other
        manifest.write_text(json.dumps(data))
        session.tool_agents([_marked(run_id, "afile00000000001"), _marked(other, "aid000000000001")])
        backdate_manifest(manifest, 9)
        out = stats("collect", "--all", env=env, cwd=repo)
        assert out.exit_code == 1, out.output
        assert "run manifest not recognised" in out.output, out.output
        assert _records(env) == [], "6.7: marked agents were collected through a manifest that names another run"
        assert "integrate/demo" not in out.output and str(repo) not in out.output
        assert manifest.exists(), "a malformed manifest is not closed"


def test_67_unmarked_spec_agents_with_old_descriptions_next_to_a_stale_manifest_are_not_backfilled():
    """6.7: the backfill heuristic never runs under `--all`: old free-form descriptions of the manifest's own spec stay out."""
    with fresh_env() as env:
        repo, _session = _setup(env)
        backfill_home(env, 2, 4)
        run_id, manifest = _begin(env, repo)
        backdate_manifest(manifest, 7)
        out = stats("collect", "--all", env=env, cwd=repo)
        assert out.exit_code == 0, out.output
        assert _count(out.output, "abandoned") == 1, out.output
        assert _records(env) == [], "6.7: `--all` ran the backfill heuristic"
        assert not any("backfill" in p.name for p in store_files(env.xdg))
        dry = stats("collect", "--agents-backfill", "--dry-run", "--json", env=env, cwd=repo)  # control: the groups exist
        assert dry.exit_code == 0, dry.output
        assert json.loads(dry.stdout)["groups"], dry.output


def test_67_backfill_shaped_group_in_the_session_of_a_workflow_run_is_not_collected_by_all():
    """6.7: the workflow run is collected; the unmarked spec agents beside it (backfill-shaped) are not."""
    with fresh_env() as env:
        repo, session = _setup(env)
        session.implement_run("wf_redteam_open_1", start="2026-10-12T09:00:00Z")
        specs = [ToolAgentSpec(OLD_DESCRIPTIONS[i].format(spec="demo", n=i + 1), agent_type=SPEC_TYPES[i], agent_id=f"aold{i:012d}") for i in range(4)]
        session.tool_agents(specs, start="2026-10-12T09:20:00Z")
        out = stats("collect", "--all", env=env, cwd=repo)
        assert out.exit_code == 0, out.output
        assert _records(env) == ["wf_redteam_open_1.json"], "6.7: `--all` wrote more than the workflow run's record"
        assert _count(out.output, "collected") == 1, out.output


def test_67_backfill_record_id_fed_into_a_fake_manifest_does_not_collect_or_rewrite_the_backfill_record():
    """6.7: a manifest named after an existing backfill record has no marked agents: abandoned; the record is untouched."""
    with fresh_env() as env:
        repo, _session = _setup(env)
        backfill_home(env, 1, 4)
        wrote = stats("collect", "--agents-backfill", "--json", env=env, cwd=repo)
        assert wrote.exit_code == 0, wrote.output
        results = json.loads(wrote.stdout)["results"]
        assert results, wrote.output
        backfill_id = results[0]["id"]
        record = env.xdg / "claude-kiro" / "runs" / f"{backfill_id}.json"
        assert record.exists()
        before = record.read_bytes()
        _rid, manifest = _begin(env, repo)
        fake = _clone(env, manifest, backfill_id, startTime=iso(datetime.now(timezone.utc) - timedelta(hours=8)))
        manifest.unlink()
        out = stats("collect", "--all", env=env, cwd=repo)
        assert out.exit_code == 0, out.output
        assert _count(out.output, "abandoned") == 1, out.output
        assert record.read_bytes() == before, "6.7: `--all` rewrote a backfill record through a fake manifest"
        assert not fake.exists() and (_closed_dir(env) / f"{backfill_id}.json").exists()
        assert len(_records(env)) == len(results)


def test_67_stale_and_young_manifests_with_marked_strangers_collect_only_the_stale_one():
    """6.7/6.3: a stale run with agents is collected, a young one is left unfinished, a stranger's marker is ignored."""
    with fresh_env() as env:
        repo, session = _setup(env)
        stale_id, stale = _begin(env, repo)
        young_id, young = _begin(env, repo)
        session.tool_agents([_marked(stale_id, "astale00000000001"), _marked(young_id, "ayoung00000000001"), _marked(GHOST, "aghost00000000001")])
        backdate_manifest(stale, 7)
        out = stats("collect", "--all", env=env, cwd=repo)
        assert out.exit_code == 0, out.output
        assert _records(env) == [f"{stale_id}.json"]
        assert young.exists() and _count(out.output, "unfinished") == 1, out.output
        assert _count(out.output, "collected") == 1, out.output
