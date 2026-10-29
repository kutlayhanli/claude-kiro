"""Red team of the SHALL NOT clauses of Story 3 (requirements 3.6 and 3.17, with 3.3, 3.4 and 3.9 as the inverses).

* 3.6: a `create` or `review` run SHALL NOT run `ck worktree report`. The report runs in-process and reads
  `git show <into>:specs/<spec>/tasks.md`; a `git` shim on PATH (`logging_git`) logs every call, so that call is the
  observable. Manifests with a near-miss `kind` are malformed (3.3), never "create-ish", and an `implement` manifest
  does run the report (3.4).
* 3.17: a `general-purpose` agent whose description carries no marker, or the marker of a run with neither a manifest
  nor the id I named, SHALL never be included in any agent-mode record (D7).

Written from requirements.md and design.md only. Every case runs the real `ck` CLI over a fake Claude home, a real store
and real git. Project modules are imported inside test bodies (none is needed here).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from stats_helpers import (
    FakeClaudeHome,
    ToolAgentSpec,
    backdate_manifest,
    begin,
    fresh_env,
    land,
    load_record,
    logging_git,
    set_manifest,
    spec_repo,
    stats,
    store_files,
)

LEGIT = "alegit00000000001"
GHOST = "ag_deadbeef"  # a well-formed run id that has neither a manifest nor is ever named


def _runs(env) -> List[str]:
    d = env.xdg / "claude-kiro" / "runs"
    return sorted(p.name for p in d.glob("*.json")) if d.exists() else []


def _setup(env, kind: str = "create", *, landed: bool = True, logging: bool = True):
    """A spec repo (every task landed), a fake Claude home with one session, a begun run and a logging git on PATH."""
    repo = spec_repo(env.root / "repo", "demo", "integrate/demo", tasks=4)
    if landed:
        for n in range(1, 5):
            land(repo, "demo", "integrate/demo", n)
    session = FakeClaudeHome(env.claude).session(cwd=repo)
    run_id, manifest = begin(env, repo, kind=kind, spec="demo", into="integrate/demo")
    log = env.root / "git-calls.jsonl"
    envp = dict(env)
    if logging:
        bin_dir = env.root / "bin"
        logging_git(bin_dir, log)
        envp["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    return repo, session, run_id, manifest, envp, log


def _calls(log: Path) -> List[List[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _report_calls(log: Path) -> List[List[str]]:
    """Every git call that reads a tasks.md: only `ck worktree report` (and `begin`'s doneAtStart) does that."""
    return [c for c in _calls(log) if any("tasks.md" in a for a in c)]


def _collect(envp, run_id: str, repo: Path, *extra: str):
    return stats("collect", "--agents", run_id, "--json", *extra, env=envp, cwd=repo)


def _legit(run_id: str, rest: str = "implement task 1") -> ToolAgentSpec:
    return ToolAgentSpec(f"[ck:{run_id}] {rest}", agent_type="general-purpose", agent_id=LEGIT)


def _agent_ids(rec: dict) -> List[str]:
    return sorted(a["agentId"] for a in rec["agents"])


# --------------------------------------------------------------------------- 3.6: create / review never run the report


@pytest.mark.parametrize("kind", ["create", "review"])
def test_36_create_and_review_runs_never_run_the_git_report(kind):
    """3.6: the manifest says create/review; the spec is perfect and fully merged; no tasks.md read, outcome unknown."""
    with fresh_env() as env:
        repo, session, run_id, manifest, envp, log = _setup(env, kind)
        session.tool_agents([_legit(run_id)])
        log.unlink(missing_ok=True)
        out = _collect(envp, run_id, repo)
        assert out.exit_code == 0, out.output
        assert json.loads(out.stdout)["result"] == "collected"
        assert _report_calls(log) == [], f"3.6: `ck worktree report` ran for a {kind} run: {_report_calls(log)}"
        rec = load_record(env.xdg, run_id)
        assert rec["outcome"] is None
        assert rec.get("tasks") is None and rec.get("stats") is None
        assert rec.get("reportSource") != "git"
        assert "outcome" not in [m.get("what") for m in rec.get("missing", [])]
        assert rec["workflow"] == "spec-create"  # kind from the manifest (3.3)


@pytest.mark.parametrize("kind", ["create", "review"])
def test_36_agent_descriptions_that_look_like_implement_do_not_turn_a_create_run_into_an_implement_run(kind):
    """3.3/3.6: the kind comes from the manifest, never from `implement task N` descriptions."""
    with fresh_env() as env:
        repo, session, run_id, manifest, envp, log = _setup(env, kind)
        session.tool_agents([
            _legit(run_id, "implement task 1"),
            ToolAgentSpec(f"[ck:{run_id}] review task 1 round 1", agent_type="spec:reviewer", agent_id="areview0000000001"),
            ToolAgentSpec(f"[ck:{run_id}] resolve task 2", agent_type="spec:resolver", agent_id="aresolv0000000001"),
        ])
        log.unlink(missing_ok=True)
        out = _collect(envp, run_id, repo)
        assert out.exit_code == 0, out.output
        assert _report_calls(log) == []
        rec = load_record(env.xdg, run_id)
        assert rec["outcome"] is None and rec.get("tasks") is None
        assert rec["workflow"] == "spec-create"


def test_36_recollect_auto_and_all_paths_never_run_the_report_either():
    """3.6 through every entry point: `--auto`, a second (unchanged) collect, and `collect --all` on a stale manifest."""
    with fresh_env() as env:
        repo, session, run_id, manifest, envp, log = _setup(env, "create")
        session.tool_agents([_legit(run_id)])
        log.unlink(missing_ok=True)
        auto = stats("collect", "--agents", run_id, "--auto", env=envp, cwd=repo)
        assert auto.exit_code == 0, auto.output
        again = _collect(envp, run_id, repo)
        assert again.exit_code == 0 and json.loads(again.stdout)["result"] == "unchanged", again.output
        assert _report_calls(log) == []

    with fresh_env() as env:
        repo, session, run_id, manifest, envp, log = _setup(env, "review")
        session.tool_agents([_legit(run_id)])
        backdate_manifest(manifest, 7)
        log.unlink(missing_ok=True)
        out = stats("collect", "--all", env=envp, cwd=repo)
        assert out.exit_code == 0, out.output
        assert run_id + ".json" in _runs(env)
        assert _report_calls(log) == [], "3.6: `collect --all` ran the report for a stale review manifest"
        assert load_record(env.xdg, run_id)["outcome"] is None


def test_36_without_a_manifest_the_report_does_not_run_either():
    """3.10/3.6: a run with no manifest has no spec; marked create-step agents give a partial record and no report call."""
    with fresh_env() as env:
        repo, session, run_id, manifest, envp, log = _setup(env, "create")
        manifest.unlink()
        session.tool_agents([ToolAgentSpec(f"[ck:{run_id}] create requirements", agent_type="general-purpose", agent_id=LEGIT)])
        log.unlink(missing_ok=True)
        out = _collect(envp, run_id, repo)
        assert out.exit_code == 0, out.output
        assert _report_calls(log) == []
        assert load_record(env.xdg, run_id)["outcome"] is None


MALFORMED_KINDS: List[Tuple[str, object]] = [
    ("capital-create", "Create"),
    ("upper-create", "CREATE"),
    ("trailing-space-review", "review "),
    ("leading-space-create", " create"),
    ("trailing-newline-create", "create\n"),
    ("capital-review", "Review"),
    ("capital-implement", "Implement"),
    ("trailing-space-implement", "implement "),
    ("empty", ""),
    ("null", None),
    ("number", 5),
    ("list", ["create"]),
    ("zero-width-space-create", "create​"),
    ("cyrillic-e-create", "crеate"),
    ("fullwidth-review", "ｒeview"),
    ("nul-implement", "implement\x00"),
    ("create-and-implement", "create implement"),
    ("plural-review", "reviews"),
]


@pytest.mark.parametrize("kind", [pytest.param(k, id=i) for i, k in MALFORMED_KINDS])
def test_36_near_miss_kind_is_a_malformed_manifest_not_a_guess(kind):
    """3.3/3.6: `Create`, `review ` ... are not kinds. Exit 1, `run manifest not recognised`, no record, no report call."""
    with fresh_env() as env:
        repo, session, run_id, manifest, envp, log = _setup(env, "create")
        session.tool_agents([_legit(run_id)])
        set_manifest(manifest, kind=kind)
        before = manifest.read_bytes()
        log.unlink(missing_ok=True)
        out = _collect(envp, run_id, repo)
        assert out.exit_code == 1, out.output
        assert json.loads(out.stdout) == {"run_id": run_id, "result": "failed", "error": "malformed"}, out.output
        plain = stats("collect", "--agents", run_id, env=envp, cwd=repo)
        assert plain.exit_code == 1, plain.output
        assert "run manifest not recognised" in plain.output, plain.output
        assert _runs(env) == []
        assert _report_calls(log) == [], "a malformed kind must not reach the report"
        assert manifest.read_bytes() == before, "a malformed manifest is left untouched"
        assert str(repo) not in out.output  # the manifest's contents are never echoed


def test_34_inverse_a_manifest_edited_to_implement_is_an_implement_run_and_the_report_runs():
    """3.4: the kind is the manifest's `implement`; the report is read from `<into>:specs/<spec>/tasks.md`."""
    with fresh_env() as env:
        repo, session, run_id, manifest, envp, log = _setup(env, "create")
        session.tool_agents([_legit(run_id, "implement task 1")])
        set_manifest(manifest, kind="implement")
        log.unlink(missing_ok=True)
        out = _collect(envp, run_id, repo)
        assert out.exit_code == 0, out.output
        assert _report_calls(log), "3.4: an implement manifest must run the git report"
        assert load_record(env.xdg, run_id)["workflow"] == "spec-implement"


def test_34_inverse_create_step_descriptions_do_not_stop_an_implement_run_from_running_the_report():
    """3.3: descriptions never decide the kind, in either direction."""
    with fresh_env() as env:
        repo, session, run_id, manifest, envp, log = _setup(env, "implement")
        session.tool_agents([ToolAgentSpec(f"[ck:{run_id}] create requirements", agent_type="general-purpose", agent_id=LEGIT)])
        log.unlink(missing_ok=True)
        out = _collect(envp, run_id, repo)
        assert out.exit_code == 0, out.output
        assert _report_calls(log), "an implement manifest runs the report whatever the agents are called"


# --------------------------------------------------------------------------- 3.17: only the named run's marker counts


def _adversarial(run_id: str, other_id: str, extras: Dict[str, str]) -> List[Tuple[str, Optional[object], Dict[str, object]]]:
    """(name, meta description, extra_meta) triples; none of them carries a valid marker of `run_id` at index 0."""
    m = f"[ck:{run_id}]"
    swap = lambda s, a, b: s.replace(a, b, 1)  # noqa: E731
    return [
        # surface forms of the marker
        ("zero-width-space-in-ck", (swap(m, "ck", "c​k") + " implement task 1"), {}),
        ("zero-width-space-before-bracket", (swap(m, "]", "​]") + " implement task 1"), {}),
        ("zero-width-joiner-in-id", (swap(m, "ag_", "ag‍_") + " implement task 1"), {}),
        ("fullwidth-brackets", f"［ck:{run_id}］ implement task 1", {}),
        ("fullwidth-letters-ck", f"[ｃｋ:{run_id}] implement task 1", {}),
        ("fullwidth-underscore-in-id", f"[ck:{run_id.replace('_', chr(0xff3f))}] implement task 1", {}),
        ("cyrillic-es-for-c", f"[сk:{run_id}] implement task 1", {}),
        ("cyrillic-ka-for-k", f"[cк:{run_id}] implement task 1", {}),
        ("cyrillic-a-in-ag", f"[ck:{run_id.replace('a', chr(0x430), 1)}] implement task 1", {}),
        ("upper-case-ck", f"[CK:{run_id}] implement task 1", {}),
        ("upper-case-id", f"[ck:{run_id.upper()}] implement task 1", {}),
        ("space-after-colon", f"[ck: {run_id}] implement task 1", {}),
        ("tab-after-colon", f"[ck:\t{run_id}] implement task 1", {}),
        ("unclosed-marker", f"[ck:{run_id} implement task 1", {}),
        ("id-with-extra-hex-digit", f"[ck:{run_id}0] implement task 1", {}),
        ("id-truncated-one-digit", f"[ck:{run_id[:-1]}] implement task 1", {}),
        ("round-brackets", f"(ck:{run_id}) implement task 1", {}),
        ("no-brackets-id-only", f"ck:{run_id} implement task 1", {}),
        # position: the marker must be at index 0, with no stripping
        ("bom-before-marker", f"﻿{m} implement task 1", {}),
        ("leading-newline", f"\n{m} implement task 1", {}),
        ("leading-space", f" {m} implement task 1", {}),
        ("leading-tab", f"\t{m} implement task 1", {}),
        ("leading-nbsp", f" {m} implement task 1", {}),
        ("marker-at-end", f"implement task 1 {m}", {}),
        ("marker-in-the-middle", f"implement {m} task 1", {}),
        ("marker-after-another-word", f"x {m}", {}),
        ("quoted-marker", f"\"{m}\" implement task 1", {}),
        # other run ids
        ("ghost-run-marker", f"[ck:{other_id}] implement task 1", {}),
        ("ghost-run-marker-after-real-marker-text", f"[ck:{other_id}] {m} implement task 1", {}),
        ("no-marker", "implement task 1", {}),
        ("empty-description", "", {}),
        ("missing-description", None, {}),
        # the system's own output fed back
        ("description-is-the-run-id", run_id, {}),
        ("description-is-the-record-path", extras["path"], {}),
        ("description-is-the-wrote-line", f"✓ {run_id}: wrote {extras['path']}", {}),
        ("description-is-the-json-line", extras["json"], {}),
        ("description-is-the-manifest-path", extras["manifest"], {}),
        # the marker in places other than the meta description
        ("marker-in-extra-meta-key", "implement task 1", {m: "x"}),
        ("marker-in-extra-meta-title", "implement task 1", {"title": f"{m} implement task 1"}),
        ("marker-in-extra-meta-prompt", "implement task 1", {"prompt": f"{m} implement task 1"}),
        ("marker-in-tool-use-id-not-description", "implement task 1", {"toolUseId": m}),
        ("marker-in-nested-meta", "implement task 1", {"nested": {"description": f"{m} implement task 1"}}),
        ("marker-in-worktree-branch", "implement task 1", {"worktreeBranch": m}),
        # non-string descriptions
        ("list-description", [f"{m} implement task 1"], {}),
        ("number-description", 7, {}),
        ("dict-description", {"description": f"{m} implement task 1"}, {}),
        ("bool-description", True, {}),
    ]


def _write_adversarial(session, run_id: str, other_id: str, extras: Dict[str, str], agent_type: str = "general-purpose") -> List[str]:
    cases = _adversarial(run_id, other_id, extras)
    specs = []
    for idx, (_name, desc, extra) in enumerate(cases):
        specs.append(ToolAgentSpec(desc, agent_type=agent_type, agent_id=f"aadv{idx:010d}", extra_meta=extra))
    session.tool_agents(specs, start="2026-10-12T09:10:00Z")
    return [f"aadv{idx:010d}" for idx in range(len(cases))]


def _rewrite_first_user_message(path: Path, text: str) -> None:
    lines = path.read_text().splitlines()
    first = json.loads(lines[0])
    first["message"]["content"] = text
    lines[0] = json.dumps(first)
    path.write_text("\n".join(lines) + "\n")


@pytest.mark.parametrize("kind", ["create", "implement"])
def test_317_unmarked_and_foreign_marked_general_purpose_agents_are_in_no_record(kind):
    """3.17: every adversarial description is left out of the record of the named run, across `--agents`, `show`, `--all`."""
    with fresh_env() as env:
        repo, session, run_id, manifest, envp, log = _setup(env, kind)
        first = _collect_prior(envp, repo, session, run_id)  # one legit record, so we can feed its output back
        prior_missing = load_record(env.xdg, run_id).get("missing", [])
        extras = {"path": first["path"], "json": json.dumps(first), "manifest": str(manifest)}
        ids = _write_adversarial(session, run_id, GHOST, extras)
        # a marker only in the first user message of an otherwise unmarked agent
        transcript = session.dir / "subagents" / f"agent-{ids[0]}.jsonl"
        _rewrite_first_user_message(transcript, f"[ck:{run_id}] implement task 1")
        log.unlink(missing_ok=True)

        out = _collect(envp, run_id, repo)
        assert out.exit_code == 0, out.output
        rec = load_record(env.xdg, run_id)
        _assert_only_meta_gaps_added(out, rec, prior_missing)
        assert _agent_ids(rec) == [LEGIT], f"3.17: foreign agents in the record: {_agent_ids(rec)}"
        if kind == "create":
            assert _report_calls(log) == []

        shown = stats("show", run_id, "--json", env=envp)
        assert shown.exit_code == 0, shown.output
        for aid in ids:
            assert aid not in shown.stdout, f"3.17: {aid} appears in `ck stats show`"

        every = stats("collect", "--all", env=envp, cwd=repo)
        assert every.exit_code == 0, every.output
        assert _runs(env) == [f"{run_id}.json"], "3.17: `collect --all` wrote a record for a foreign agent"
        assert _agent_ids(load_record(env.xdg, run_id)) == [LEGIT]


def _assert_only_meta_gaps_added(out, rec: dict, prior_missing: list) -> None:
    """3.17 with 3.15: a recollect over adversarial agents may only add `{agentId: None, what: "meta"}` gaps
    to the `missing` entries the first collect wrote; with nothing added the result is `unchanged`."""
    result = json.loads(out.stdout)["result"]
    missing = rec.get("missing", [])
    remaining = list(missing)
    for entry in prior_missing:
        assert entry in remaining, f"3.17: a gap of the first collect was dropped: {entry} not in {missing}"
        remaining.remove(entry)
    assert all(m == {"agentId": None, "what": "meta"} for m in remaining), f"3.17: an adversarial agent added a gap: {remaining}"
    if remaining:
        assert result == "updated", f"3.17: new meta gaps but result {result}: {out.output}"
    else:
        assert result == "unchanged", f"3.17: an adversarial agent changed the record: {out.output}"


def _collect_prior(envp, repo: Path, session, run_id: str) -> dict:
    session.tool_agents([_legit(run_id)])
    out = _collect(envp, run_id, repo)
    assert out.exit_code == 0, out.output
    data = json.loads(out.stdout)
    assert data["result"] == "collected", out.output
    return data


def test_317_run_with_only_adversarial_agents_has_no_agents_and_writes_nothing():
    """3.17/3.12: with no valid marker anywhere, `--agents` exits 1, writes nothing and leaves the manifest open."""
    with fresh_env() as env:
        repo, session, run_id, manifest, envp, log = _setup(env, "create")
        _write_adversarial(session, run_id, GHOST, {"path": str(env.root / "x.json"), "json": "{}", "manifest": str(manifest)})
        before = manifest.read_bytes()
        out = _collect(envp, run_id, repo)
        assert out.exit_code == 1, out.output
        assert json.loads(out.stdout) == {"run_id": run_id, "result": "failed", "error": "no_agents"}, out.output
        plain = stats("collect", "--agents", run_id, env=envp, cwd=repo)
        assert plain.exit_code == 1, plain.output
        assert "no agents carry the marker" in plain.output, plain.output
        assert _runs(env) == []
        assert manifest.read_bytes() == before and manifest.parent.name == "runs-open"


def test_317_stale_manifest_with_only_adversarial_agents_is_abandoned_and_collects_nothing():
    """3.17/6.5: `--all` finds no valid marked agent for a stale manifest: abandoned, no record, exit 0."""
    with fresh_env() as env:
        repo, session, run_id, manifest, envp, log = _setup(env, "create")
        _write_adversarial(session, run_id, GHOST, {"path": str(env.root / "x.json"), "json": "{}", "manifest": str(manifest)})
        backdate_manifest(manifest, 8)
        out = stats("collect", "--all", env=envp, cwd=repo)
        assert out.exit_code == 0, out.output
        assert re.search(r"abandoned:\s*1", out.output), out.output
        assert _runs(env) == []
        assert not [p for p in store_files(env.xdg) if "runs/" in str(p) and p.suffix == ".json"]


@pytest.mark.parametrize("agent_type", ["spec:implementer", "ck-reviewer", "Explore", None])
def test_317_other_agent_types_with_foreign_markers_are_not_included_either(agent_type):
    """3.17 (D7): the type plays no part: a foreign or missing marker excludes the agent whatever its type."""
    with fresh_env() as env:
        repo, session, run_id, manifest, envp, log = _setup(env, "create")
        _collect_prior(envp, repo, session, run_id)
        prior_missing = load_record(env.xdg, run_id).get("missing", [])
        _write_adversarial(session, run_id, GHOST, {"path": "p", "json": "{}", "manifest": str(manifest)}, agent_type=agent_type)
        out = _collect(envp, run_id, repo)
        assert out.exit_code == 0, out.output
        rec = load_record(env.xdg, run_id)
        _assert_only_meta_gaps_added(out, rec, prior_missing)
        assert _agent_ids(rec) == [LEGIT]


def test_317_two_runs_with_manifests_never_share_agents():
    """3.17: agents marked with run B's id are in B's record only, even when B collects after A."""
    with fresh_env() as env:
        repo, session, run_a, manifest_a, envp, log = _setup(env, "create")
        run_b, manifest_b = begin(env, repo, kind="create", spec="demo", into="integrate/demo")
        assert run_a != run_b
        session.tool_agents([
            ToolAgentSpec(f"[ck:{run_a}] implement task 1", agent_type="general-purpose", agent_id="aaaaaaaa00000001"),
            ToolAgentSpec(f"[ck:{run_b}] implement task 1", agent_type="general-purpose", agent_id="abbbbbbb00000001"),
            ToolAgentSpec(f"[ck:{run_b}] review task 1 round 1", agent_type="general-purpose", agent_id="abbbbbbb00000002"),
        ])
        assert _collect(envp, run_b, repo).exit_code == 0
        assert _collect(envp, run_a, repo).exit_code == 0
        assert _agent_ids(load_record(env.xdg, run_a)) == ["aaaaaaaa00000001"]
        assert _agent_ids(load_record(env.xdg, run_b)) == ["abbbbbbb00000001", "abbbbbbb00000002"]


def test_317_same_agent_id_in_two_sessions_only_the_marked_one_counts():
    """3.17: an unmarked agent with the same id in another session adds nothing to the record."""
    with fresh_env() as env:
        repo, session, run_id, manifest, envp, log = _setup(env, "create")
        other = FakeClaudeHome(env.claude).session(slug="-work-other", session_id="5e55a10e-0000-4000-8000-0000000000ee", cwd=repo)
        session.tool_agents([_legit(run_id)])
        huge = [("req_dup", 9_000_000, 9_000_000, 0, 0)]
        other.tool_agents([ToolAgentSpec("implement task 1", agent_type="general-purpose", agent_id=LEGIT, requests=huge)])
        other.tool_agents([ToolAgentSpec(f"[ck:{GHOST}] implement task 1", agent_type="general-purpose", agent_id="aother0000000001", requests=huge)])
        out = _collect(envp, run_id, repo)
        assert out.exit_code == 0, out.output
        rec = load_record(env.xdg, run_id)
        assert _agent_ids(rec) == [LEGIT]
        assert rec["agents"][0]["input"] == 1000, "3.17: the other session's same-id agent was read instead"


def test_317_marked_agent_of_another_session_is_taken_only_when_the_manifest_session_has_none():
    """3.1/3.17: a same-id lookalike in the manifest's session must not hide or duplicate the marked one elsewhere."""
    with fresh_env() as env:
        repo, session, run_id, manifest, envp, log = _setup(env, "create")
        other = FakeClaudeHome(env.claude).session(slug="-work-other", session_id="5e55a10e-0000-4000-8000-0000000000ef", cwd=repo)
        other.tool_agents([_legit(run_id)])
        session.tool_agents([ToolAgentSpec("implement task 1", agent_type="general-purpose", agent_id="alook000000000001")])
        out = _collect(envp, run_id, repo)
        assert out.exit_code == 0, out.output
        assert _agent_ids(load_record(env.xdg, run_id)) == [LEGIT]


def test_317_adversarial_agent_added_later_does_not_change_an_existing_record():
    """3.9/3.17: recollecting after foreign agents appear is `unchanged`, byte for byte."""
    with fresh_env() as env:
        repo, session, run_id, manifest, envp, log = _setup(env, "create")
        first = _collect_prior(envp, repo, session, run_id)
        record_path = Path(first["path"])
        before = record_path.read_bytes()
        session.tool_agents([
            ToolAgentSpec(run_id, agent_type="general-purpose", agent_id="afeed00000000001"),
            ToolAgentSpec(first["path"], agent_type="general-purpose", agent_id="afeed00000000002"),
            ToolAgentSpec(f"[ck:{GHOST}] implement task 2", agent_type="general-purpose", agent_id="afeed00000000003"),
            ToolAgentSpec("implement task 3", agent_type="general-purpose", agent_id="afeed00000000004"),
        ], start="2026-10-12T09:30:00Z")
        again = _collect(envp, run_id, repo)
        assert again.exit_code == 0, again.output
        assert json.loads(again.stdout)["result"] == "unchanged"
        assert record_path.read_bytes() == before


def test_39_closed_manifest_marker_is_still_collected_by_its_own_id_and_by_no_other_run():
    """3.9/3.17: a marked agent of an already closed run is included by `--agents <that id>` (`updated`), in no other record."""
    with fresh_env() as env:
        repo, session, run_a, manifest_a, envp, log = _setup(env, "create")
        run_b, manifest_b = begin(env, repo, kind="create", spec="demo", into="integrate/demo")
        session.tool_agents([
            ToolAgentSpec(f"[ck:{run_a}] implement task 1", agent_type="general-purpose", agent_id="aaaaaaaa00000001"),
            ToolAgentSpec(f"[ck:{run_b}] implement task 1", agent_type="general-purpose", agent_id="abbbbbbb00000001"),
        ])
        first = _collect(envp, run_a, repo)
        assert first.exit_code == 0 and json.loads(first.stdout)["result"] == "collected", first.output
        assert not manifest_a.exists() and (env.xdg / "claude-kiro" / "runs-closed" / f"{run_a}.json").exists()
        session.tool_agents([ToolAgentSpec(f"[ck:{run_a}] review task 1 round 1", agent_type="general-purpose", agent_id="aaaaaaaa00000002")],
                            start="2026-10-12T09:45:00Z")
        second = _collect(envp, run_a, repo)
        assert second.exit_code == 0, second.output
        assert json.loads(second.stdout)["result"] == "updated"
        assert _agent_ids(load_record(env.xdg, run_a)) == ["aaaaaaaa00000001", "aaaaaaaa00000002"]
        assert _collect(envp, run_b, repo).exit_code == 0
        assert _agent_ids(load_record(env.xdg, run_b)) == ["abbbbbbb00000001"]
        third = _collect(envp, run_a, repo)
        assert json.loads(third.stdout)["result"] == "unchanged"


def test_39_closed_run_agents_are_not_swept_into_a_new_run_by_collect_all():
    """3.17/3.9: `collect --all` after a run closed never re-collects or re-assigns that run's agents."""
    with fresh_env() as env:
        repo, session, run_a, manifest_a, envp, log = _setup(env, "create")
        run_b, manifest_b = begin(env, repo, kind="create", spec="demo", into="integrate/demo")
        session.tool_agents([
            ToolAgentSpec(f"[ck:{run_a}] implement task 1", agent_type="general-purpose", agent_id="aaaaaaaa00000001"),
            ToolAgentSpec(f"[ck:{run_b}] implement task 1", agent_type="general-purpose", agent_id="abbbbbbb00000001"),
        ])
        assert _collect(envp, run_a, repo).exit_code == 0
        backdate_manifest(manifest_b, 9)
        out = stats("collect", "--all", env=envp, cwd=repo)
        assert out.exit_code == 0, out.output
        assert _agent_ids(load_record(env.xdg, run_a)) == ["aaaaaaaa00000001"]
        assert _agent_ids(load_record(env.xdg, run_b)) == ["abbbbbbb00000001"]
