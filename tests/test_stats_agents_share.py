"""Agent-mode analytics: the CLI half of the privacy boundary (TC-48, TC-52 to TC-54, the export half of TC-55).

Everything goes through the real `ck stats begin`, `collect --agents` / `--run` and `export` as subprocesses, over a
fake Claude home, a real git checkout (local bare remote), a fake `gh` and, for the note flow, a real pty with a fake
`claude` drafter. Leak assertions scan the raw bytes of every exported file. The decision is checked against the
seams `confidential_terms` / `leak_scan` themselves, never re-implemented here. ck code is imported in test bodies only.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest

from stats_helpers import (
    CONFIDENTIAL,
    AgentSpec,
    FakeClaudeHome,
    ToolAgentSpec,
    all_bytes,
    begin,
    described,
    fake_claude_drafter,
    fresh_env,
    git_project,
    implement_result,
    kiro_checkout,
    land,
    load_record,
    spec_repo,
    stats,
    TaskSpec,
)

pytestmark = pytest.mark.usefixtures("no_network")

SPEC = "zephyr-payroll"
INTO = "integrate/zephyr-payroll"
WT_PATH = "/home/dana/acme-ledger/.ck/worktrees/zephyr-payroll-task-3"
WT_BRANCH = "feat/zephyr-payroll-task-3"
DESCRIPTION_TEXT = "Reconcile Globex invoices"


# --------------------------------------------------------------------------- builders


def agent_mode_run(env: Dict[str, str], home: FakeClaudeHome, tmp: Path, *, name: str = "acme-ledger",
                   wt_path: str = WT_PATH, wt_branch: str = WT_BRANCH, session_start: str = "2026-10-12T09:00:00Z"
                   ) -> SimpleNamespace:
    """A begun agent-mode implement run of `SPEC` with three marked Agent-tool subagents, one task landed."""
    repo = spec_repo(Path(tmp) / name, spec=SPEC, into=INTO, tasks=2)
    run_id, manifest = begin(env, repo, "implement", SPEC, INTO)
    land(repo, SPEC, INTO, 1)
    session = home.session("-work-" + name, cwd=repo)
    specs = [
        ToolAgentSpec(described(run_id, "implement task 1"), worktree_path=wt_path, worktree_branch=wt_branch),
        ToolAgentSpec(described(run_id, "review task 1"), agent_type="spec:reviewer", model="claude-opus-5-5",
                      worktree_path=wt_path, worktree_branch=wt_branch),
        ToolAgentSpec(described(run_id, DESCRIPTION_TEXT), agent_type="spec:fixer", worktree_path=wt_path),
    ]
    agents = session.tool_agents(specs, start=session_start)
    return SimpleNamespace(repo=repo, run_id=run_id, manifest=manifest, session=session, agents=agents,
                           descriptions=[s.description for s in specs])


def workflow_run(home: FakeClaudeHome, tmp: Path, *, name: str = "wf-proj", wt_path: str = WT_PATH,
                 wt_branch: str = WT_BRANCH, start: str = "2026-10-13T10:00:00Z") -> Tuple[str, Any, Path]:
    """A spec-implement workflow run whose agent metas carry worktree keys -> (run id, FakeRun, project)."""
    proj = git_project(Path(tmp), "git@github.com:acme-corp/acme-ledger.git", name=name)
    session = home.session("-work-" + name, cwd=proj)
    extra = {"worktreePath": wt_path, "worktreeBranch": wt_branch}
    run = session.implement_run(
        "wf_zephyr-impl-1", start=start, args={"spec": SPEC, "root": str(proj)},
        result=implement_result([TaskSpec(task="1", title=DESCRIPTION_TEXT)], into=INTO),
        agents=[AgentSpec("task 1", extra_meta=extra), AgentSpec("review 1", model="claude-opus-5-5", extra_meta=extra)])
    return run.run_id, run, proj


def collect_agents(env: Dict[str, str], run_id: str) -> dict:
    out = stats("collect", "--agents", run_id, "--json", env=env)
    assert out.exit_code == 0, out.output
    data = json.loads(out.stdout)
    assert data["result"] in ("collected", "updated", "unchanged"), out.output
    return data


def collect_workflow(env: Dict[str, str], run_id: str) -> None:
    out = stats("collect", "--run", run_id, env=env)
    assert out.exit_code == 0, out.output


def exported_json(checkout: Path) -> List[Path]:
    root = Path(checkout) / "analytics" / "runs"
    return sorted(root.rglob("*.json")) if root.exists() else []


def documented_paths_check(records: List[dict]) -> None:
    from test_stats_export import assert_documented

    assert_documented(records)


@pytest.fixture
def w():
    with fresh_env() as env:
        home = FakeClaudeHome(env.claude)
        checkout, bare = kiro_checkout(env.root)
        yield SimpleNamespace(env=env, home=home, checkout=checkout, bare=bare, xdg=env.xdg, tmp=env.root)


# --------------------------------------------------------------------------- TC-48


def test_tc48_an_exported_agent_mode_run_is_content_free(w):
    am = agent_mode_run(w.env, w.home, w.tmp)
    wf_id, wf_run, _proj = workflow_run(w.home, w.tmp)
    collect_agents(w.env, am.run_id)
    collect_workflow(w.env, wf_id)

    out = stats("export", "--to", str(w.checkout), "--yes", env=w.env)
    assert out.exit_code == 0, out.output
    files = exported_json(w.checkout)
    assert len(files) == 2, [str(f) for f in files]
    records = [json.loads(f.read_text()) for f in files]

    by_source = {r.get("source"): r for r in records}
    assert sorted(by_source, key=str) == ["agents", "workflow"], [r.get("source") for r in records]
    assert by_source["agents"]["cost"]["source"] == "agent_transcripts"
    assert by_source["workflow"]["cost"]["source"] != "agent_transcripts"

    from claude_kiro.analytics.share import validate_shareable

    for rec in records:
        assert validate_shareable(rec) == []
    documented_paths_check(records)

    raw = all_bytes(w.checkout / "analytics")
    low = raw.lower()
    banned: List[str] = [*CONFIDENTIAL, am.run_id, am.session.session_id, WT_PATH, WT_BRANCH, *am.descriptions,
                         DESCRIPTION_TEXT, wf_id, wf_run.session_id, *am.agents.agent_ids, *wf_run.agent_ids]
    for term in banned:
        assert term.encode() not in raw, f"exported bytes hold {term!r}"
        assert term.lower().encode() not in low, f"exported bytes hold {term!r} (case-folded)"
    assert am.run_id[3:].encode() not in low, "the hex part of the run id leaked"


# --------------------------------------------------------------------------- TC-52


@pytest.mark.parametrize("mode", ["workflow", "agents"])
def test_tc52_worktree_values_are_terms_and_cover_the_probe(w, mode):
    if mode == "agents":
        am = agent_mode_run(w.env, w.home, w.tmp)
        data = collect_agents(w.env, am.run_id)
        record = json.loads(Path(data["path"]).read_text())
    else:
        wf_id, _run, _proj = workflow_run(w.home, w.tmp)
        collect_workflow(w.env, wf_id)
        record = load_record(w.xdg, wf_id)

    assert WT_PATH in record["terms"]
    assert WT_BRANCH in record["terms"]

    from claude_kiro.analytics import confidential_terms

    probe = {"project": {"root": WT_PATH, "branch": WT_BRANCH}}
    probe_terms = confidential_terms([probe])
    assert probe_terms, "the probe must yield terms"
    assert probe_terms <= confidential_terms([record])


# --------------------------------------------------------------------------- TC-53


def test_tc53_manifest_values_and_the_run_id_are_terms(w):
    am = agent_mode_run(w.env, w.home, w.tmp)
    data = collect_agents(w.env, am.run_id)
    record = json.loads(Path(data["path"]).read_text())
    manifest = json.loads(am.manifest.read_text())
    assert manifest["spec"] == SPEC and manifest["into"] == INTO

    from claude_kiro.analytics import confidential_terms

    terms = confidential_terms([record])
    for value in (manifest["spec"], manifest["root"], manifest["into"], am.run_id):
        assert value.lower() in terms, f"{value!r} is not a confidential term of the agent-mode record"


# --------------------------------------------------------------------------- TC-54


@pytest.mark.parametrize("which", ["worktree path", "worktree branch", "run id"])
def test_tc54_a_note_holding_agent_mode_values_is_a_hit_and_excluded(tmp_path, which):
    from test_stats_leak import HIT_WORDS, World

    home = FakeClaudeHome(tmp_path / "claude-home")
    world = World(tmp_path, home)
    wf_id, _run, _proj = workflow_run(home, tmp_path)
    am = agent_mode_run(world.env, home, tmp_path)
    needle = {"worktree path": WT_PATH, "worktree branch": WT_BRANCH, "run id": am.run_id}[which]

    world.collect("--run", wf_id)
    first = world.export("--yes")
    assert first.exit_code == 0, first.output
    world.collect("--agents", am.run_id)
    fake_claude_drafter(world.bin, f"Resolver needed 3 rounds near {needle} lockfile")
    code, text = world.export_tty("--note", script=[(r"(?i)accept", "a\n")])
    assert code == 0, text
    assert HIT_WORDS.search(text), text
    assert world.exported_notes() == [], text
    assert len(world.exported_json()) == 2
    assert needle.encode() not in all_bytes(world.checkout / "analytics")


# --------------------------------------------------------------------------- TC-55 (export half)


def test_tc55_vocabulary_and_an_exported_record_has_no_leak_hits_against_its_own_terms(w):
    from claude_kiro.analytics import confidential_terms, leak_scan
    from claude_kiro.analytics.leak import VOCABULARY

    assert {"workflow", "agents", "agent_transcripts"} <= set(VOCABULARY)

    am = agent_mode_run(w.env, w.home, w.tmp)
    data = collect_agents(w.env, am.run_id)
    local = json.loads(Path(data["path"]).read_text())
    out = stats("export", "--to", str(w.checkout), "--yes", env=w.env)
    assert out.exit_code == 0, out.output
    files = exported_json(w.checkout)
    assert len(files) == 1
    text = files[0].read_text()
    assert json.loads(text)["source"] == "agents"
    assert leak_scan({files[0].name: text}, confidential_terms([local])) == []
