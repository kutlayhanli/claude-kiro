"""Property tests of the collector, driven through the real CLI over real temp homes (P-11, P-15, P-16).

Every example builds its fake Claude home, store and config inside `with fresh_env() as env:` in the `@given`
body and runs `ck` as a subprocess with that env. No property here runs `ck stats show`.
"""

import json
import re
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from stats_helpers import AgentSpec, ck_sub, fresh_env, git_project, FakeClaudeHome, load_record

PROPERTIES = {
    "P-11": "test_collect_is_idempotent",
    "P-15": "test_cost_is_conserved",
    "P-16": "test_missing_data_is_null_never_zero",
}

PROP = dict(max_examples=25, derandomize=True, database=None, deadline=None,
            suppress_health_check=list(HealthCheck))

MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
TOKEN_KEYS = ("input", "output", "cacheRead", "cacheCreation")

# Labels: P-3 templates (implementer and creator workflows) and P-4 negatives.
LABELS = [
    "task 3", "task 17 [test]", "task 3 retry 2", "task 3 retry 2 (opus)", "task 3 revise 1", "review 3",
    "review 3 round 2", "resolve 3", "resolve 3 round 1", "fix 1 (Task 3 red on landing)", "re-check 2", "land 3",
    "full gate @2", "final gate", "cross-test wave 1", "plan 1", "plan 1 retry", "report", "merge 4",
    "write requirements.md", "write design.md", "write test track", "link tracks into tasks.md", "review: fidelity",
    "refute: scope", "snapshot spec before revision", "consistency check: pass 1",
    "task", "task 3!", "Task 3", "tasks 3", "task 3 revise", "reviews 3", "land", "full gate @", "write README.md",
    " task 3", "task 3 extra", "zephyr-payroll", "Reconcile Globex invoices",
]

tokens = st.one_of(st.just(0), st.integers(0, 2_000_000))


@st.composite
def agent_plans(draw, max_agents=12):
    n = draw(st.integers(1, max_agents))
    plans = []
    for i in range(n):
        reqs = [(draw(tokens), draw(tokens), draw(tokens), draw(tokens)) for _ in range(draw(st.integers(1, 4)))]
        plans.append({"label": draw(st.sampled_from(LABELS)), "model": draw(st.sampled_from(MODELS)),
                      "split": draw(st.booleans()), "reqs": reqs})
    return plans


def build_agents(plans, tag="r"):
    out = []
    for i, p in enumerate(plans):
        requests = [(f"req_{tag}{i}_{j}", *r) for j, r in enumerate(p["reqs"])]
        out.append(AgentSpec(p["label"], model=p["model"], requests=requests, split_blocks=p["split"],
                             phase=f"Wave {1 + i % 2}", agent_id=f"a{tag}{i:02d}beef"))
    return out


def collect_run(env, run_id):
    r = ck_sub("stats", "collect", "--run", run_id, env=env, cwd=env.root)
    assert r.exit_code == 0, r.output
    return load_record(env.xdg, run_id)


def cost_json(env, run_id, allow_none=False):
    r = ck_sub("cost", "--run", run_id, "--json", env=env, cwd=env.root)
    if allow_none and r.exit_code == 1:  # no transcripts left: nothing priced
        return {"priced_usd": 0.0}
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)


def runs_dir(env) -> Path:
    return env.xdg / "claude-kiro" / "runs"


def stored(env):
    d = runs_dir(env)
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def count(out: str, label: str) -> int:
    m = re.search(rf"^{re.escape(label)}:\s*(\d+)", out, re.M)
    assert m, f"no '{label}:' line in:\n{out}"
    return int(m.group(1))


def num(v):
    return 0 if v is None else v


def run_cost(rec):
    runs = [c for c in rec["costs"] if c.get("scope") == "run"]
    assert runs, rec["costs"]
    return runs[0]


# --------------------------------------------------------------------------- P-11


@st.composite
def homes(draw):
    projects = draw(st.integers(1, 2))
    n_done = draw(st.integers(1, 4))
    n_other = draw(st.integers(0, 3))
    n = n_done + n_other
    kinds = [draw(st.sampled_from(["implement", "create"])) for _ in range(n_done)]
    kinds += [draw(st.sampled_from(["running", "foreign"])) for _ in range(n_other)]
    which = [draw(st.integers(0, projects - 1)) for _ in range(n)]
    return projects, list(zip(kinds, which))


@settings(**PROP)
@given(home=homes(), k=st.integers(1, 3), interleave=st.booleans())
def test_collect_is_idempotent(home, k, interleave):
    n_projects, runs = home
    with fresh_env() as env:
        h = FakeClaudeHome(env.claude)
        projs = [git_project(env.root, name=f"proj{i}") for i in range(n_projects)]
        sessions = [h.session(slug=f"-work-p{i}", cwd=projs[i]) for i in range(n_projects)]
        expected, collected_ids = 0, []
        for kind, pi in runs:
            s = sessions[pi]
            if kind == "implement":
                collected_ids.append(s.implement_run().run_id)
                expected += 1
            elif kind == "create":
                collected_ids.append(s.create_run().run_id)
                expected += 1
            elif kind == "running":
                s.implement_run(status="running", result=None)
            else:
                s.foreign_run()
        for i in range(k):
            r = ck_sub("stats", "collect", "--all", env=env, cwd=env.root)
            assert r.exit_code == 0, r.output
            if i == 0:
                assert count(r.stdout, "collected") == expected, r.output
            else:
                assert count(r.stdout, "collected") == 0, r.output
                assert count(r.stdout, "already had") == expected, r.output
            if interleave and collected_ids:
                one = ck_sub("stats", "collect", "--run", collected_ids[i % len(collected_ids)], env=env, cwd=env.root)
                assert one.exit_code == 0, one.output
            assert stored(env) == sorted(collected_ids)


# --------------------------------------------------------------------------- P-15


def expected_totals(plan):
    """Token sums per kind for a plan: one count per requestId (split lines carry the same usage)."""
    tot = dict.fromkeys(TOKEN_KEYS, 0)
    for r in plan["reqs"]:
        for key, v in zip(TOKEN_KEYS, r):
            tot[key] += v
    return tot


def sum_cells(cells, key):
    return sum(num(c.get(key)) for c in cells.values())


@settings(**PROP)
@given(plans=agent_plans(), dup=st.integers(0, 50))
def test_cost_is_conserved(plans, dup):
    with fresh_env() as env:
        h = FakeClaudeHome(env.claude)
        s = h.session(slug="-work-p0", cwd=git_project(env.root))
        run = s.implement_run(agents=build_agents(plans))
        rec = collect_run(env, run.run_id)

        agents = rec["agents"]
        assert len(agents) == len(plans)
        # every transcript exists, so nothing is unknown: no null usd on an agent or a bucket
        for a in agents:
            assert a["usd"] is not None, a
        for group in ("byModel", "byRole", "byPhase", "byTask"):
            for name, cell in rec[group].items():
                assert cell.get("usd") is not None, (group, name)
        assert run_cost(rec)["usd"] is not None
        total = {k: sum(num(a[k]) for a in agents) for k in TOKEN_KEYS}
        total_usd = sum(num(a["usd"]) for a in agents)
        # each token kind agrees with what the transcripts hold, counting a request once
        for key in TOKEN_KEYS:
            assert total[key] == sum(expected_totals(p)[key] for p in plans), key
        untasked = [a for a in agents if a["task"] is None]
        for group in ("byModel", "byRole", "byPhase"):
            cells = rec[group]
            assert sum_cells(cells, "usd") == pytest.approx(total_usd, abs=1e-6), group
            for key in TOKEN_KEYS:
                assert sum_cells(cells, key) == total[key], (group, key)
        by_task = rec["byTask"]
        assert sum_cells(by_task, "usd") + sum(num(a["usd"]) for a in untasked) == pytest.approx(total_usd, abs=1e-6)
        for key in TOKEN_KEYS:
            assert sum_cells(by_task, key) + sum(num(a[key]) for a in untasked) == total[key], key
        # the run total, and `ck cost`
        assert abs(num(run_cost(rec)["usd"]) - total_usd) <= 0.005
        priced = cost_json(env, run.run_id)["priced_usd"]
        assert abs(priced - total_usd) <= 0.005

        # metamorphic: duplicate transcript lines with the same requestId; nothing changes
        for i, aid in enumerate(run.agent_ids):
            path = run.transcript(aid)
            lines = path.read_text().splitlines()
            assistant = [l for l in lines if '"requestId"' in l]
            extra = [assistant[(dup + i) % len(assistant)]]
            path.write_text("\n".join(lines + extra) + "\n")
        again = collect_run(env, run.run_id)
        for key in ("agents", "byModel", "byRole", "byPhase", "byTask"):
            assert again[key] == rec[key], key
        assert abs(cost_json(env, run.run_id)["priced_usd"] - priced) <= 1e-9


# --------------------------------------------------------------------------- P-16


@st.composite
def damage(draw):
    plans = draw(agent_plans(max_agents=6))
    n = len(plans)
    chosen = draw(st.lists(st.integers(0, n - 1), min_size=1, max_size=n, unique=True))
    ops = {i: draw(st.sampled_from(["delete", "truncate"])) for i in chosen}
    cuts = {i: draw(st.floats(0, 1)) for i in chosen}
    return plans, ops, cuts


@settings(**PROP)
@given(case=damage())
def test_missing_data_is_null_never_zero(case):
    plans, ops, cuts = case
    with fresh_env() as env:
        h = FakeClaudeHome(env.claude)
        s = h.session(slug="-work-p0", cwd=git_project(env.root))
        run = s.implement_run(agents=build_agents(plans))
        before = collect_run(env, run.run_id)
        base_priced = cost_json(env, run.run_id)["priced_usd"]
        orig = {a["agentId"]: a for a in before["agents"]}

        deleted = set()
        for i, op in ops.items():
            aid = run.agent_ids[i]
            if op == "delete":
                run.drop_transcript(aid)
                deleted.add(aid)
            else:
                path = run.transcript(aid)
                data = path.read_bytes()
                lines = data.rstrip(b"\n").split(b"\n")
                last_start = len(data.rstrip(b"\n")) - len(lines[-1])
                # strictly inside the last line, so the file ends in partial JSON
                cut = last_start + max(1, min(len(lines[-1]) - 1, int(cuts[i] * len(lines[-1]))))
                path.write_bytes(data[:cut])

        after = collect_run(env, run.run_id)
        missing_ids = {m.get("agentId") for m in after["missing"]}
        idx = {aid: i for i, aid in enumerate(run.agent_ids)}

        def run_tokens(aid):  # the run file's own context-token count for the agent (input + output)
            return sum(r[0] + r[1] for r in plans[idx[aid]]["reqs"])

        for a in after["agents"]:
            aid = a["agentId"]
            if aid in deleted:
                # design 1.12 / AgentRow: no transcript means no usage (null); the cost is an estimate from the
                # run-file context tokens at the input rate, so usd is null or a positive estimate, never 0
                for key in TOKEN_KEYS:
                    assert a[key] is None, (aid, key, a[key])
                assert a["usd"] is None or a["usd"] > 0 or run_tokens(aid) == 0, (aid, a["usd"])
                assert aid in missing_ids, (aid, after["missing"])
            elif aid in orig:
                for key in (*TOKEN_KEYS, "usd"):
                    if a[key] == 0 and orig[aid][key] not in (0, None):
                        raise AssertionError(f"{aid}.{key} went from {orig[aid][key]} to 0 after truncation")
                    if a[key] is None:
                        assert aid in missing_ids, (aid, key, after["missing"])
                # an unsplit agent's only usage-bearing line was cut: the record says the transcript is damaged
                if ops.get(idx[aid]) == "truncate" and not plans[idx[aid]]["split"]:
                    assert aid in missing_ids or a.get("transcript") == "truncated", (aid, after["missing"])

        # buckets whose every agent lost its transcript hold null, never 0 (1.12)
        for group, field in (("byModel", "model"), ("byRole", "role"), ("byPhase", "phase"), ("byTask", "task")):
            members = {}
            for a in after["agents"]:
                if a.get(field) is not None:
                    members.setdefault(str(a[field]), []).append(a)
            for name, cell in after[group].items():
                group_agents = members.get(str(name), [])
                if group_agents and all(a["agentId"] in deleted for a in group_agents):
                    for key in TOKEN_KEYS:
                        assert cell.get(key) is None, (group, name, key, cell.get(key))
                    zero_run = all(run_tokens(a["agentId"]) == 0 for a in group_agents)
                    assert cell.get("usd") is None or cell["usd"] > 0 or zero_run, (group, name, cell.get("usd"))

        # the run cost is an estimate once any transcript is gone (1.12, S7), and never rises or drops to 0
        if deleted:
            assert run_cost(after)["source"] == "estimate", run_cost(after)
        usd_before, usd_after = run_cost(before)["usd"], run_cost(after)["usd"]
        if usd_after is not None:
            assert usd_after <= num(usd_before) + 1e-9, (usd_before, usd_after)
            survivors = sum(num(a["usd"]) for a in after["agents"] if a["agentId"] not in deleted)
            if survivors > 0:
                assert usd_after > 0, (usd_before, usd_after)
        # metamorphic: deleting transcripts never raises the priced total (also through `ck cost`)
        assert cost_json(env, run.run_id, allow_none=True)["priced_usd"] <= base_priced + 1e-9
