"""Property tests of agent-mode collection, driven through the real CLI over real temp homes (P-15, P-16, P-17).

Every example builds its fake Claude home, store, config and repository inside `with fresh_env() as env:` in the
`@given` body and runs `ck` as a subprocess with that env. The decisions are read from `ck cost` and from the seam
`confidential_terms`, never re-implemented here. ck code is imported inside test bodies only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from stats_helpers import (
    CONFIDENTIAL,
    FakeClaudeHome,
    ToolAgentSpec,
    begin,
    ck_sub,
    described,
    fresh_env,
    read_only,
    spec_repo,
    stats,
)

PROPERTIES = {
    "P-15": "test_p15_run_cost_agrees_with_agent_costs_and_ck_cost",
    "P-16": "test_p16_worktree_and_manifest_values_are_confidential_terms",
    "P-17": "test_p17_auto_collection_never_fails_and_prints_one_line",
}

PROP = dict(max_examples=20, derandomize=True, database=None, deadline=None,
            suppress_health_check=list(HealthCheck))

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

SPECS = ["zephyr-payroll", "orion-billing", "acme-ledger"]
KNOWN_MODELS = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]
UNKNOWN_MODEL = "my-finetune-zephyr"


def new_run(env, spec: str = "zephyr-payroll", dirname: str = "proj"):
    """A begun implement run in a real repo -> (repo, run_id, manifest path, claude home, session)."""
    repo = spec_repo(Path(env.root) / dirname, spec=spec, into=f"integrate/{spec}", tasks=2)
    run_id, manifest = begin(env, repo, "implement", spec, f"integrate/{spec}")
    home = FakeClaudeHome(env.claude)
    session = home.session("-work-proj", cwd=repo)
    return repo, run_id, manifest, home, session


def collect_json(env, run_id: str) -> dict:
    out = stats("collect", "--agents", run_id, "--json", env=env)
    assert out.exit_code == 0, out.output
    return json.loads(out.stdout)


# --------------------------------------------------------------------------- P-15

tokens = st.integers(0, 1_000_000)


@st.composite
def marked_agents(draw):
    n = draw(st.integers(1, 6))
    out = []
    for i in range(n):
        model = draw(st.sampled_from([*KNOWN_MODELS, UNKNOWN_MODEL]))
        reqs = [(draw(tokens), draw(tokens), draw(tokens), draw(tokens)) for _ in range(draw(st.integers(1, 3)))]
        out.append((model, reqs))
    return out


@settings(**PROP)
@given(plan=marked_agents())
def test_p15_run_cost_agrees_with_agent_costs_and_ck_cost(plan):
    with fresh_env() as env:
        _repo, run_id, _manifest, _home, session = new_run(env)
        specs = []
        for i, (model, reqs) in enumerate(plan):
            requests = [(f"req_{i}_{j}", *r) for j, r in enumerate(reqs)]  # each id is split across two lines
            specs.append(ToolAgentSpec(described(run_id, f"implement task {i + 1}"), model=model, requests=requests))
        session.tool_agents(specs)

        data = collect_json(env, run_id)
        record = json.loads(Path(data["path"]).read_text())
        run_cost = next(c for c in record["costs"] if c.get("scope") == "run")
        cost = ck_sub("cost", "--session", session.session_id, "--json", env=env)
        assert cost.exit_code == 0, cost.output
        priced = json.loads(cost.stdout)["priced_usd"]

        has_unknown = any(model == UNKNOWN_MODEL for model, _ in plan)
        if has_unknown:
            assert priced is None
            assert run_cost["source"] == "estimate", run_cost
        else:
            assert priced is not None
            assert run_cost["source"] == "agent_transcripts", run_cost
            agent_sum = sum(a["usd"] for a in record["agents"] if a.get("usd") is not None)
            assert all(a.get("usd") is not None for a in record["agents"])
            assert abs(run_cost["usd"] - priced) <= 0.0101, (run_cost, priced)
            assert abs(run_cost["usd"] - agent_sum) <= 0.0101, (run_cost, agent_sum)


# --------------------------------------------------------------------------- P-16

PATH_FORMS = [
    "/home/dana/{s}/.ck/worktrees/{s}-task-1", "/{s}/wt", ".ck/worktrees/{s}-task-1", "../{s}/wt", "/srv/{s}/wt/",
    "~/{s}/wt", "/home/my user/{s} dir/wt", "/home/dâna/{s}/wtünï", "C:\\work\\{s}", "{s}",
]
BRANCH_FORMS = ["feat/{s}-task-3", "a/b/{s}", "feat/ünï-{s}", "{s}", "integrate/{s}/x/y"]
SEGMENTS = [c for c in CONFIDENTIAL if "{" not in c and "}" not in c and "\n" not in c] + ["Ünïcode-Prøject", "x"]


@st.composite
def worktree_values(draw):
    path = draw(st.sampled_from(PATH_FORMS)).replace("{s}", draw(st.sampled_from(SEGMENTS)))
    branch = draw(st.sampled_from(BRANCH_FORMS)).replace("{s}", draw(st.sampled_from(SEGMENTS)))
    return path, branch


@settings(**PROP)
@given(values=worktree_values(), spec=st.sampled_from(SPECS), which=st.sampled_from(["both", "path", "branch"]))
def test_p16_worktree_and_manifest_values_are_confidential_terms(values, spec, which):
    path, branch = values
    with fresh_env() as env:
        repo, run_id, manifest_path, _home, session = new_run(env, spec)
        specs = [
            ToolAgentSpec(described(run_id, "implement task 1"),
                          worktree_path=path if which in ("both", "path") else None,
                          worktree_branch=branch if which in ("both", "branch") else None),
            ToolAgentSpec(described(run_id, "review task 1"), agent_type="spec:reviewer"),
        ]
        session.tool_agents(specs)
        data = collect_json(env, run_id)
        record = json.loads(Path(data["path"]).read_text())
        manifest = json.loads(manifest_path.read_text())

        from claude_kiro.analytics import confidential_terms

        terms = confidential_terms([record])
        if which in ("both", "path"):
            assert path in record["terms"]
            assert confidential_terms([{"project": {"root": path}}]) <= terms
        if which in ("both", "branch"):
            assert branch in record["terms"]
            assert confidential_terms([{"project": {"branch": branch}}]) <= terms
        assert confidential_terms([{"project": {"root": path if which != "branch" else None,
                                                "branch": branch if which != "path" else None}}]) <= terms
        probe = {"project": {"spec": manifest["spec"], "root": manifest["root"], "branch": manifest["into"]},
                 "runId": run_id}
        assert confidential_terms([probe]) <= terms
        assert run_id.lower() in terms


# --------------------------------------------------------------------------- P-17

P2_POSITIVES = ["ag_0123abcd", "ag_ffffffff", "ag_00000000"]
P2_NEGATIVES = [
    "AG_0123abcd", "Ag_0123abcd", "ag_0123ABCD", "ag_0123abc", "ag_0123abcde", "ag-0123abcd", "ag__0123abcd",
    " ag_0123abcd", "ag_0123abcd ", "ag_0123abcd\n", "\tag_0123abcd", "wf_0123abcd", "wf_0123456789ab",
    "[ck:ag_0123abcd]", "ag_０１２３abcd", "ag_٠١٢٣abcd", "",
]
HOSTILE = ["$(touch pwned)", ";", "'", '"', "`id`", "a b", "ag_0123abcd; rm -rf /", "../../etc/passwd", "%s%n", "{}", "--help",
           "-x", "--", "ag_0123abcd\x1b[31m"]
ids = st.one_of(
    st.sampled_from(P2_POSITIVES + P2_NEGATIVES + HOSTILE),
    st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), max_size=24),
    st.none(),  # the real id of the begun run
)
STATES = ["no_manifest", "no_agents", "malformed_manifest", "setting_off"]
if not IS_ROOT:
    STATES += ["unreadable_projects", "read_only_store"]


def _agents_for(session, run_id: str) -> None:
    session.tool_agents([ToolAgentSpec(described(run_id, "implement task 1"))])


@settings(**PROP)
@given(agent_id=ids, state=st.sampled_from(STATES))
def test_p17_auto_collection_never_fails_and_prints_one_line(agent_id: Optional[str], state: str):
    with fresh_env() as env:
        run_id: Optional[str] = None
        if state != "no_manifest":
            _repo, run_id, manifest, _home, session = new_run(env)
            if state in ("unreadable_projects", "read_only_store"):
                _agents_for(session, run_id)
            if state == "malformed_manifest":
                manifest.write_text("{not json")
            if state == "setting_off":
                assert ck_sub("agents", "set", "run.analytics", "false", env=env, cwd=_repo).exit_code == 0
        target = agent_id if agent_id is not None else (run_id or "ag_00000000")
        args = ["collect", "--agents", target, "--auto"]

        def check(out) -> None:
            assert out.exit_code == 0, (state, target, out.output)
            assert out.stdout.endswith("\n") and out.stdout.count("\n") == 1, (state, target, out.stdout)

        if state == "unreadable_projects":
            projects = env.claude / "projects"
            projects.chmod(0)
            try:
                check(stats(*args, env=env))
            finally:
                projects.chmod(0o755)
        elif state == "read_only_store":
            store = env.xdg / "claude-kiro"
            store.mkdir(parents=True, exist_ok=True)
            with read_only(store):
                check(stats(*args, env=env))
        else:
            check(stats(*args, env=env))
