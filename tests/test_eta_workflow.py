"""TC-25, TC-29(b), TC-51, TC-54(b), TC-60(b), TC-87 and P-24: the two shipped Workflow scripts write the progress events.

The real `plugin/workflows/spec-implement.js` and `spec-create.js` run under the stub runtime (the Node bridge, on its
virtual clock). Agents are the only fakes: with `exec_ck` they parse the progress flags out of the claim and land
commands in their prompts and record the events through the real `ck progress event` subprocess, and landing results
come from the fake project. The test runs `ck progress begin` before the run and `ck progress end` after it, as the
skill would. P-24 inspects the prompts the script builds (no execCk).
"""

import copy
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from hypothesis import given, settings, strategies as st

from eta_strategies import NO_NETWORK, CREATE_PHASES, progress_lines
from guard_helpers import notify_script
from landing_bridge import bridge, scenarios  # noqa: F401  (bridge is a module-scoped fixture)
from stats_helpers import ck_sub, spec_repo
from test_landing_scheduler_properties import (  # noqa: F401
    LAND_FIX, RETRY, REVISE, SETTINGS, events, kind, land_tasks, prepare, tasks_named, with_finish_first,
)

pytestmark = [NO_NETWORK, pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")]

PROPERTIES = {"P-24": "test_p24_claim_and_land_prompts_carry_the_progress_flags"}

SPEC = "demo"
KEY = "demo--20261030T150205Z-3f9a1c"
ETA_LINE_RE = re.compile(r"^ETA \d\d:\d\d \(p90 \d\d:\d\d\), critical \d+(→\d+)*$")
LANDED_RE = re.compile(r"^landed [\d,]+ · ETA \d\d:\d\d( \([+-]\d+\))?$")
UNAVAILABLE = "ETA unavailable"
AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
LADDER_MODEL = "opus"
IMPLEMENTER = {"model": "sonnet", "effort": "medium", "escalate": [LADDER_MODEL]}
CLAIM_KINDS = {"retry": ("retry", "escalation"), "revise": ("revision", "escalation"),
               "landfix": ("landfix", "escalation"), "resolve": ("resolve", "escalation")}


# --------------------------------------------------------------------------- the world: repo, env, exec_ck

def exec_ck(repo: Path) -> Dict[str, Any]:
    """Fake agents run the real CLI as a subprocess in `repo`, with the test's (isolated) environment."""
    return {"cmd": [sys.executable, "-m", "claude_kiro.cli"], "env": dict(os.environ), "cwd": str(repo)}


def ck(repo: Path, *args: str):
    return ck_sub(*args, cwd=repo)


def begin(repo: Path, spec: str, kind_: str) -> Dict[str, Any]:
    """The skill's step: `ck progress begin <spec> --kind <kind> --mode workflow --json`."""
    res = ck(repo, "progress", "begin", spec, "--kind", kind_, "--mode", "workflow", "--json")
    assert res.exit_code == 0, res.stderr
    return json.loads(res.stdout)


def end(repo: Path, spec: str, outcome: str = "completed"):
    """The skill's step after the run: `ck progress end <spec> --outcome <outcome>`."""
    res = ck(repo, "progress", "end", spec, "--outcome", outcome)
    assert res.exit_code == 0, res.stdout + res.stderr
    return res


def world(tmp_path: Path, name: str = "repo") -> Path:
    return spec_repo(tmp_path / name, SPEC, tasks=4)


def project_25() -> Dict[str, Any]:
    """Tasks 1-4. Task 2 fails twice (a retry, then an escalation to opus); task 3 is red on its first landing."""
    deps = {"1": [], "2": ["1"], "3": [], "4": ["3"]}
    return {"deps": deps, "durations": {"1": 5, "2": 5, "3": 8, "4": 3}, "failUntil": {"2": 2}, "checkRed": ["3"],
            "planDurations": {str(k): 1 for k in range(1, 60)}}


def args_25(run: Optional[str], eta_line: Optional[str]) -> Dict[str, Any]:
    deps = project_25()["deps"]
    return {"agents": {"implementer": dict(IMPLEMENTER)}, "deps": deps, "remaining": list(deps), "mergeBatch": 2,
            "progressRun": run, "etaLine": eta_line}


def started(out: Dict[str, Any]) -> List[str]:
    return [e["label"] for e in out["trace"] if e["ev"] == "start"]


def trace_events(out: Dict[str, Any]) -> List[tuple]:
    return events(out)


def run_implement(bridge, repo: Path, *, eta_answer: Optional[str] = None, use_eta_line: bool = True, progress: bool = True):  # noqa: F811
    """begin -> bridge run (spec-implement.js, execCk) -> end. Returns the pieces the tests look at."""
    began = begin(repo, SPEC, "implement")
    project = project_25()
    if eta_answer:
        project["etaAnswer"] = eta_answer
    run = began.get("run") if progress else None
    out = bridge.run(project, args_25(run, began.get("etaLine") if use_eta_line else None), slots=4, exec_ck=exec_ck(repo))
    finished = end(repo, SPEC)
    return began, out, finished


def exec_etas(out: Dict[str, Any], kinds=("landing", "merge", "hand-landing")) -> List[Optional[str]]:
    found = []
    for res in out["execResults"]:
        argv = res["argv"]
        if argv[:2] == ["progress", "event"] and argv[3] in kinds:
            try:
                found.append(json.loads(res["stdout"]).get("eta"))
            except ValueError:
                pass
    return found


# --------------------------------------------------------------------------- TC-25

def test_tc25_implement_run_writes_the_events_to_the_real_progress_file(bridge, tmp_path):  # noqa: F811
    repo = world(tmp_path)
    began, out, finished = run_implement(bridge, repo)
    assert began["run"], began
    assert all(r["code"] == 0 for r in out["execResults"]), [r for r in out["execResults"] if r["code"] != 0]
    rows = progress_lines(began["file"])
    kinds = [r["kind"] for r in rows]
    assert kinds[0] == "start" and kinds[-1] == "end", kinds
    assert rows[0]["run"] == began["run"]

    # a task-start for each first attempt
    first_attempts = sorted(l.split()[1] for l in started(out) if re.match(r"^task \w+$", l))
    assert sorted(r["task"] for r in rows if r["kind"] == "task-start") == first_attempts

    # retry 2, then escalation 2 with its model
    assert [(r["task"], r["attempt"]) for r in rows if r["kind"] == "retry"] == [("2", 2)]
    esc = [r for r in rows if r["kind"] == "escalation"]
    assert [(r["task"], r["attempt"], r["model"]) for r in esc] == [("2", 3, LADDER_MODEL)]
    # no retry or escalation comes before that task's task-start
    first_start = {r["task"]: i for i, r in enumerate(rows) if r["kind"] == "task-start"}
    for i, r in enumerate(rows):
        if r["kind"] in ("retry", "escalation"):
            assert first_start[r["task"]] < i

    # one landing per land agent, with exactly its tasks and the fake project's red/merged sets
    lands = [l for l in started(out) if l.startswith("land ")]
    landings = [r for r in rows if r["kind"] == "landing"]
    assert len(landings) == len(lands) >= 3
    red_left = {"3": 1}
    for label, row in zip(lands, landings):
        tasks = land_tasks(label)
        red = [t for t in tasks if red_left.get(t)]
        for t in red:
            red_left[t] -= 1
        assert row["tasks"] == tasks
        assert row["red"] == red
        assert row["merged"] == [t for t in tasks if t not in red]
    assert any(r["red"] == ["3"] for r in landings)

    # merges equal the result's merged
    assert sorted(r["task"] for r in rows if r["kind"] == "merge") == sorted(out["result"]["merged"]) == ["1", "2", "3", "4"]
    # the test's own end closes the file, and the privacy rule holds for what the run wrote
    assert rows[-1]["kind"] == "end"
    text = Path(began["file"]).read_text()
    assert "MANDATE" not in text and "Mechanical planning step" not in text


def test_tc25_create_run_writes_phase_events(bridge, tmp_path):  # noqa: F811
    repo = world(tmp_path)
    began = begin(repo, "orbit", "create")
    args = {"feature": "orbit", "progressRun": began["run"], "etaLine": began.get("etaLine")}
    out = bridge.create(args, exec_ck=exec_ck(repo))
    end(repo, "orbit")
    assert all(r["code"] == 0 for r in out["execResults"]), out["execResults"]
    rows = progress_lines(began["file"])
    ran = []
    for res in out["execResults"]:
        argv = res["argv"]
        if len(argv) > 3 and argv[3] == "step-end":
            title = argv[argv.index("--phase") + 1]
            if title not in ran:
                ran.append(title)
    assert ran and set(ran) <= set(CREATE_PHASES)
    starts = []
    for r in rows:
        if r["kind"] == "phase-start" and r["phase"] not in starts:
            starts.append(r["phase"])
    assert starts == ran, (starts, ran)
    ends = {r["phase"] for r in rows if r["kind"] in ("phase-end", "step-end")}
    assert set(ran) <= ends
    assert rows[0]["kind"] == "start" and rows[-1]["kind"] == "end"


# --------------------------------------------------------------------------- TC-51

def test_tc51_implement_logs_the_start_line_and_one_eta_line_per_landing(bridge, tmp_path):  # noqa: F811
    repo = world(tmp_path)
    began, out, _ = run_implement(bridge, repo)
    eta_line = began["etaLine"]
    assert eta_line and ETA_LINE_RE.match(eta_line), eta_line
    logs = out["logs"]
    assert logs.count(eta_line) == 1
    at = logs.index(eta_line)
    assert re.match(r"^\d+ task\(s\) remaining", logs[at - 1]), logs[:at + 1]  # right after that line, before any task ends
    assert not any("done and merged" in l for l in logs[:at])

    lands = [l for l in started(out) if l.startswith("land ")]
    landed = [l for l in logs if LANDED_RE.match(l)]
    assert len(landed) == len(lands) >= 3
    etas = exec_etas(out)
    assert all(l in etas for l in landed), (landed, etas)


def test_tc51_create_logs_the_start_line_and_an_eta_line_per_phase_end(bridge, tmp_path):  # noqa: F811
    repo = world(tmp_path)
    began = begin(repo, "orbit", "create")
    out = bridge.create({"feature": "orbit", "progressRun": began["run"], "etaLine": began.get("etaLine")}, exec_ck=exec_ck(repo))
    end(repo, "orbit")
    assert began["etaLine"] and out["logs"].count(began["etaLine"]) == 1
    by_phase: Dict[str, List[str]] = {}
    for res in out["execResults"]:
        argv = res["argv"]
        if len(argv) > 3 and argv[3] == "step-end" and "--json" in argv:
            eta = json.loads(res["stdout"]).get("eta")
            if eta:
                by_phase.setdefault(argv[argv.index("--phase") + 1], []).append(eta)
    assert by_phase
    for title, etas in by_phase.items():
        assert any(l in etas for l in out["logs"]), (title, etas, out["logs"])


# --------------------------------------------------------------------------- TC-54(b)

@pytest.mark.parametrize("answer", ["null", "number", "missing"])
def test_tc54b_a_bad_eta_answer_gives_unavailable_lines_and_changes_nothing_else(bridge, tmp_path, answer):  # noqa: F811
    normal = run_implement(bridge, world(tmp_path, "normal"))[1]
    odd = run_implement(bridge, world(tmp_path, "odd"), eta_answer=answer, use_eta_line=False)[1]
    lands = [l for l in started(odd) if l.startswith("land ")]
    assert odd["logs"].count(UNAVAILABLE) == 1 + len(lands)  # the start line and one per landing
    assert not any(LANDED_RE.match(l) for l in odd["logs"])
    assert trace_events(odd) == trace_events(normal)
    assert odd["result"]["merged"] == normal["result"]["merged"]
    assert odd["result"] == normal["result"]


# --------------------------------------------------------------------------- TC-29(b), TC-60(b), TC-87

@pytest.mark.skipif(AS_ROOT, reason="directory permissions do not bind root")
def test_tc87_unwritable_progress_dir_and_failing_notify_leave_the_run_unchanged(bridge, tmp_path):  # noqa: F811
    healthy_repo = world(tmp_path, "healthy")
    began_a, a, end_a = run_implement(bridge, healthy_repo)

    notify = notify_script(tmp_path / "bin", "fail", tmp_path / "notify.out")
    res = ck_sub("agents", "set", "run.notify_command", notify, cwd=healthy_repo)
    assert res.exit_code == 0, res.stderr

    broken_repo = world(tmp_path, "broken")
    began_b = begin(broken_repo, SPEC, "implement")
    file_b = Path(began_b["file"])
    os.chmod(file_b, 0o400)
    os.chmod(file_b.parent, 0o500)
    try:
        project = project_25()
        project["etaAnswer"] = "null"
        b = bridge.run(project, args_25(began_b["run"], None), slots=4, exec_ck=exec_ck(broken_repo))
        end_b = end(broken_repo, SPEC)
    finally:
        os.chmod(file_b.parent, 0o700)
        os.chmod(file_b, 0o600)

    # the run itself is unchanged: same labels in order, same merges, landings and outcome
    assert trace_events(b) == trace_events(a)
    assert b["result"]["merged"] == a["result"]["merged"]
    assert b["result"]["landings"] == a["result"]["landings"]
    assert b["result"]["outcome"] == a["result"]["outcome"]
    assert b["result"] == a["result"]
    assert set(b["result"]) == set(a["result"])
    assert not [k for k in b["result"] if re.search(r"progress|notif.*fail|eta", k, re.I)]

    # the failures are named exactly once, by the warning lines of the test's own `ck progress end`
    lines = end_b.stdout.splitlines()
    assert len([l for l in lines if "notification failed" in l]) == 1, end_b.stdout
    assert len([l for l in lines if l.startswith("⚠") and "progress" in l]) == 1, end_b.stdout
    assert not any("⚠" in l for l in end_a.stdout.splitlines())
    everything = json.dumps(b["execResults"]) + json.dumps(b["result"]) + "\n".join(b["logs"])
    assert "notification failed" not in everything
    assert "not written" not in everything
    assert all(r["code"] == 0 for r in b["execResults"]), [r for r in b["execResults"] if r["code"] != 0]
    assert b["logs"].count(UNAVAILABLE) == 1 + len([l for l in started(b) if l.startswith("land ")])


# --------------------------------------------------------------------------- P-24

def claim_command(prompt: str) -> Optional[Dict[str, Any]]:
    """`ck ... worktree claim <spec> <n> <flags>` in a prompt: {'task', 'flags'} (flags: name -> value or True)."""
    m = re.search(r"worktree claim " + SPEC + r" (\w+)([^\n\"`]*)", prompt)
    return {"task": m.group(1), "flags": flag_map(m.group(2))} if m else None


def land_command(prompt: str) -> Optional[Dict[str, Any]]:
    m = re.search(r"worktree land " + SPEC + r" ([^\n\"`]*)", prompt)
    if not m:
        return None
    tasks: List[str] = []
    for tok in m.group(1).split():
        if not tok.isdigit():
            break
        tasks.append(tok)
    return {"tasks": tasks, "flags": flag_map(m.group(1))}


def flag_map(text: str) -> Dict[str, Any]:
    toks = text.split()
    flags: Dict[str, Any] = {}
    for i, tok in enumerate(toks):
        if tok.startswith("--"):
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            flags[tok] = nxt if nxt is not None and not nxt.startswith("--") else True
    return flags


def with_agents(sc: Dict[str, Any], conflict: bool) -> Dict[str, Any]:
    sc = copy.deepcopy(sc)
    sc["args"]["agents"] = {**sc["args"].get("agents", {}), "implementer": dict(IMPLEMENTER)}
    if conflict:
        sc["project"]["conflicts"] = [list(sc["project"]["deps"])[-1]]
    return sc


def claim_kind_of(label: str) -> Optional[str]:
    k = kind(label)
    if k == "fix":
        return "landfix" if LAND_FIX.match(label.split(" (", 1)[1].rstrip(")") if " (" in label else "") else None
    return k if k in ("task", "retry", "revise", "resolve") else None


@SETTINGS
@given(sc=scenarios(), slots=st.sampled_from([1, 2, 3, 4, 5, 6]), finish_first=st.booleans(), conflict=st.booleans())
def test_p24_claim_and_land_prompts_carry_the_progress_flags(bridge, sc, slots, finish_first, conflict):  # noqa: F811
    sc = with_agents(with_finish_first(prepare(sc, slots=slots), finish_first), conflict)
    with_run = bridge.run(sc["project"], {**sc["args"], "progressRun": KEY, "etaLine": None}, slots=sc["slots"])
    without = bridge.run(sc["project"], sc["args"], slots=sc["slots"])

    # without progressRun no prompt carries a progress flag
    for label, prompt in without["prompts"].items():
        assert "--progress-run" not in prompt and "--progress-event" not in prompt, label

    order = [l for l in with_run["events"] if not l.endswith(" end")]
    first_idx: Dict[str, int] = {}
    for i, label in enumerate(order):
        if kind(label) == "task":
            first_idx.setdefault(label.split()[1], i)
    for i, label in enumerate(order):
        prompt = with_run["prompts"][label]
        ck_kind = claim_kind_of(label)
        if ck_kind:
            claim = claim_command(prompt)
            assert claim, (label, prompt)
            f = claim["flags"]
            assert f.get("--progress-run") == KEY, label
            model = with_run["opts"][label].get("model") or "inherit"
            assert f.get("--progress-model") == model, (label, f)
            assert str(f.get("--attempt", "")).isdigit() and int(f["--attempt"]) >= 1, (label, f)
            event = f.get("--progress-event")
            if ck_kind == "task":
                assert event == "task-start" and f["--attempt"] == "1", (label, f)
            else:
                assert event == ("escalation" if model == LADDER_MODEL else CLAIM_KINDS[ck_kind][0]), (label, f, model)
                n = claim["task"]
                assert n in first_idx and first_idx[n] < i, (label, "retry/escalation before the task-start")
                if ck_kind == "retry":
                    attempt = re.search(r"attempt (\d+)\)", prompt)
                    assert attempt and f["--attempt"] == attempt.group(1), (label, f)
        elif kind(label) == "land":
            land = land_command(prompt)
            assert land, (label, prompt)
            assert land["flags"].get("--progress-run") == KEY, label
            assert land["tasks"] == land_tasks(label), (label, land)
