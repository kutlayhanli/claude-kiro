"""The `ck arbiter` CLI and the record of rulings (run-principles Task 25).

TC-53, 88, 89, 91, 92, 93, 133, 135, 150, 152, 154. Real git repos, the real `ck` CLI as a subprocess, real files, real
file locks and real concurrent processes. Project modules are imported inside test bodies, so the file collects before
`ck arbiter` exists. Harness names map to design.md: `ck arbiter decide` is `ck arbiter open`, `ck arbiter route` is
`ck arbiter settle`, and `ck arbiter log` is `decisions_entries` over `specs/demo/decisions.md`.
"""

import fcntl
import json
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from arbiter_helpers import DEFAULT_PRINCIPLES, ArbiterProject, arbiter_project, ck_run, decisions_entries
from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

INTO = "integrate/demo"
TEST_FILE = "tests/test_greet.py"


# --------------------------------------------------------------------------- helpers


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def common_dir(p: ArbiterProject) -> Path:
    return Path(git(p.root, "rev-parse", "--path-format=absolute", "--git-common-dir"))


def ledger_path(p: ArbiterProject) -> Path:
    return common_dir(p) / "ck" / "demo" / "arbiter-ledger.json"


def seed_ledger(p: ArbiterProject, claims: dict, last_entry: int = 0) -> dict:
    ledger = {"schema": "ck-arbiter-ledger/1", "nextWorktree": 1, "lastEntry": last_entry, "claims": claims}
    path = ledger_path(p)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2) + "\n")
    return ledger


def claim_json(task: str = "2", test: str = "test_x", text: str = "the test contradicts the requirement") -> dict:
    return {"task": task, "trigger": "test-conflict", "text": text, "file": TEST_FILE, "test": test, "criterion": "1.1"}


def key_of(claim: dict) -> str:
    from claude_kiro.arbiter_policy import Claim, claim_key

    return claim_key(Claim(**claim))


def open_claim(p: ArbiterProject, claim: dict, risk: str = "normal") -> subprocess.CompletedProcess:
    payload = {"claim": claim, "task": {"risk": risk, "complexity": "Low"}}
    return p.ck("arbiter", "open", "demo", "--task", claim["task"], "--into", INTO, "--json", stdin=json.dumps(payload))


def settle(p: ArbiterProject, key: str, raw, tier: str = "normal") -> subprocess.CompletedProcess:
    stdin = raw if isinstance(raw, str) else json.dumps(raw)
    return p.ck("arbiter", "settle", "demo", key, "--tier", tier, "--json", stdin=stdin)


def parsed(done: subprocess.CompletedProcess) -> dict:
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


def ruling(key: str, outcome: str = "code", **over) -> dict:
    out = {"claim": key, "status": "ruled", "outcome": outcome, "reasoning": "the requirement says so",
           "principle": "project:test-intent"}
    if outcome == "code":
        out["code"] = "make the implementer print the greeting"
    if outcome == "spec_edit":
        out["changed"] = "tasks.md: Task 2 Verify narrowed"
    if outcome == "test_repair":
        out["testRepair"] = {"file": TEST_FILE, "test": "test_x", "change": "assert the greeting", "why": "1.1"}
    if outcome == "requirements":
        out["requirement"] = {"kind": "add", "wording": "WHEN x, THE SYSTEM SHALL y.", "reason": "missing"}
    out.update(over)
    return out


def stronger_look(key: str) -> dict:
    return {"claim": key, "status": "needs_stronger_look", "reasoning": "I cannot decide this at this tier",
            "established": "the test and the requirement disagree", "principle": "project:test-intent"}


def fresh(tmp_path: Path, name: str) -> ArbiterProject:
    return arbiter_project(tmp_path / name)


def opened(tmp_path: Path, name: str, **kw):
    """A fresh project with one claim opened at `rule`; returns (project, key, open result)."""
    p = fresh(tmp_path, name)
    claim = claim_json(**kw)
    result = parsed(open_claim(p, claim))
    assert result["action"] == "rule", result
    return p, result["key"], result


def record_payload(n: int = 1, **over) -> dict:
    out = {
        "trigger": "test-conflict", "claim_key": f"2|test:{TEST_FILE}::test_r{n}", "claim_text": f"claim number {n}",
        "task": "2", "tier": "normal", "outcome": "spec_edit", "reasoning": f"reasoning {n}",
        "principle_id": "project:test-intent", "principle_text": "a test states what the requirement says",
        "plan_layer": "absent", "changed": "tasks.md: Task 2 Verify narrowed", "moved": [],
        "commit": "abc1234", "landed": [True, ""],
    }
    out.update(over)
    return out


def record(p: ArbiterProject, payload: dict, *extra: str) -> subprocess.CompletedProcess:
    return p.ck("arbiter", "record", "demo", "--into", INTO, *extra, "--json", stdin=json.dumps(payload))


def decisions_text(p: ArbiterProject) -> str:
    path = p.target / "specs" / "demo" / "decisions.md"
    return path.read_text() if path.exists() else ""


def branch_exists(p: ArbiterProject, branch: str) -> bool:
    return subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=p.root).returncode == 0


# --------------------------------------------------------------------------- TC-53


@pytest.mark.parametrize("outcome", ["spec_edit", "test_repair", "code", "requirements"])
def test_tc53_a_ruling_with_each_outcome_passes_settle(tmp_path, outcome):
    p, key, _ = opened(tmp_path, outcome)
    result = parsed(settle(p, key, ruling(key, outcome)))
    assert result["problems"] == [], result
    assert result["reason"] != "invalid-ruling", result
    assert result["principle"]["id"] == "project:test-intent", result


def test_tc53_a_ruling_asking_for_a_stronger_look_passes_settle(tmp_path):
    p, key, _ = opened(tmp_path, "stronger")
    result = parsed(settle(p, key, stronger_look(key)))
    assert result["problems"] == [], result
    assert result["route"] == "rerun", result


@pytest.mark.parametrize("lacking", ["claim", "reasoning", "principle"])
def test_tc53_a_ruling_lacking_claim_reasoning_or_principle_fails(tmp_path, lacking):
    p, key, _ = opened(tmp_path, "lacking-" + lacking)
    raw = ruling(key, "code")
    del raw[lacking]
    result = parsed(settle(p, key, raw))
    assert result["route"] == "needs_input" and result["reason"] == "invalid-ruling", result
    assert result["record"] is False, result
    assert result["problems"], result


# --------------------------------------------------------------------------- TC-88


def test_tc88_record_appends_one_numbered_entry_with_every_field(tmp_path):
    p = fresh(tmp_path, "p")
    done = parsed(record(p, record_payload(1)))
    assert done["number"] == 1 and done["commit"], done
    [entry] = decisions_entries(p)
    assert entry["number"] == 1
    assert entry["Trigger"] == "test-conflict"
    assert f"2|test:{TEST_FILE}::test_r1" in entry["Claim"] and "claim number 1" in entry["Claim"]
    assert entry["Ruling"].startswith("spec_edit") and "reasoning 1" in entry["Ruling"]
    assert "project:test-intent" in entry["Principle cited"]
    assert "tasks.md: Task 2 Verify narrowed" in entry["What changed"]
    assert entry["Commit"] == "abc1234"
    # committed alone on the integration branch, so a landing's clean-target check passes
    assert decisions_entries(p, "branch") == decisions_entries(p)
    assert git(p.target, "status", "--porcelain") == ""
    assert git(p.target, "show", "--name-only", "--format=", "HEAD").splitlines() == ["specs/demo/decisions.md"]


# --------------------------------------------------------------------------- TC-89


def test_tc89_a_ruling_with_no_file_changed_records_commit_none(tmp_path):
    p = fresh(tmp_path, "p")
    parsed(record(p, record_payload(1, outcome="code", changed="nothing", commit=None, landed=None)))
    [entry] = decisions_entries(p)
    assert entry["Commit"] == "none"
    assert entry["What changed"].lower().startswith("nothing")


def test_tc89_a_create_ruling_records_commit_none(tmp_path):
    p = fresh(tmp_path, "p")
    payload = record_payload(1, trigger="create:refuted", task=None, claim_key="refuted|finding-1",
                             changed="requirements.md: wording corrected", commit=None, landed=None)
    parsed(record(p, payload))
    [entry] = decisions_entries(p)
    assert entry["Commit"] == "none"
    assert entry["Trigger"] == "create:refuted"


# --------------------------------------------------------------------------- TC-91


def test_tc91_earlier_entries_stay_byte_identical_and_numbers_keep_rising(tmp_path):
    p = fresh(tmp_path, "p")
    seen = ""
    for n in (1, 2, 3):
        done = parsed(record(p, record_payload(n)))  # each `ck` call is a new process
        assert done["number"] == n, done
        now = decisions_text(p)
        assert now.startswith(seen), f"entries before number {n} changed"
        assert len(now) > len(seen)
        seen = now
    assert [e["number"] for e in decisions_entries(p)] == [1, 2, 3]


# --------------------------------------------------------------------------- TC-92


def test_tc92_sixteen_concurrent_records_give_sixteen_entries_and_numbers(tmp_path):
    p = fresh(tmp_path, "p")
    from arbiter_helpers import CK

    procs = []
    for n in range(1, 17):
        proc = subprocess.Popen([*CK, "arbiter", "record", "demo", "--into", INTO, "--json"], cwd=p.root,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        procs.append((n, proc))
    for n, proc in procs:  # all 16 are started before any one is fed, so they overlap
        proc.stdin.write(json.dumps(record_payload(n)))
        proc.stdin.close()
    outs = []
    for n, proc in procs:
        out = proc.stdout.read()
        err = proc.stderr.read()
        assert proc.wait(timeout=300) == 0, f"record {n} failed: {out}{err}"
        outs.append(json.loads(out))
    entries = decisions_entries(p)
    assert len(entries) == 16, [e["number"] for e in entries]
    assert sorted(e["number"] for e in entries) == list(range(1, 17))
    assert sorted(o["number"] for o in outs) == list(range(1, 17))
    assert sorted(e["Claim"].split("claim number ")[1] for e in entries) == sorted(str(n) for n in range(1, 17))
    assert git(p.target, "status", "--porcelain") == ""


# --------------------------------------------------------------------------- TC-93


def test_tc93_no_file_before_the_first_ruling_and_the_first_ruling_creates_it(tmp_path):
    p = fresh(tmp_path, "p")
    for where in ("target", "root", "branch"):
        assert decisions_entries(p, where) == []
    assert not (p.target / "specs" / "demo" / "decisions.md").exists()
    assert not (p.root / "specs" / "demo" / "decisions.md").exists()
    parsed(record(p, record_payload(1)))
    assert (p.target / "specs" / "demo" / "decisions.md").is_file()
    assert len(decisions_entries(p)) == 1


def test_tc93_a_workflow_run_with_no_rulings_leaves_no_file(tmp_path, bridge):
    p = fresh(tmp_path, "p")
    out = bridge.run({"deps": {"1": [], "2": ["1"]}}, dict(p.run_args), exec_ck=p.exec_ck)
    assert out is not None
    assert not (p.target / "specs" / "demo" / "decisions.md").exists()
    assert not (p.root / "specs" / "demo" / "decisions.md").exists()
    assert decisions_entries(p, "branch") == []


# --------------------------------------------------------------------------- TC-133


def test_tc133_open_equals_decide_claim_and_only_a_recorded_history_changes_the_answer(tmp_path):
    from claude_kiro.arbiter_policy import Claim, TaskMeta, claim_facts, claim_key, decide_claim

    claim = claim_json()
    key = claim_key(Claim(**claim))
    task = TaskMeta(risk="normal", complexity="Low")

    def seam(ledger: dict):
        d = decide_claim(claim_facts(Claim(**claim), ledger, "ok", task))
        return d.action, d.tier, d.reason

    empty = {"schema": "ck-arbiter-ledger/1", "nextWorktree": 1, "lastEntry": 0, "claims": {}}
    a = fresh(tmp_path, "empty")
    got_a = parsed(open_claim(a, claim))
    assert (got_a["action"], got_a["tier"], got_a["reason"]) == seam(empty)
    assert got_a["key"] == key

    history = {key: {"task": "2", "status": "ruled", "calls": 1, "ruling": 1, "base": "abc1234"}}
    b = fresh(tmp_path, "history")
    ledger = seed_ledger(b, history, last_entry=1)
    got_b = parsed(open_claim(b, claim))
    assert (got_b["action"], got_b["tier"], got_b["reason"]) == seam(ledger)
    assert got_b["action"] == "ask"
    assert got_a["action"] != got_b["action"], "the fixture must be one where only the recorded history differs"


# --------------------------------------------------------------------------- TC-135


def _seam_route(raw: dict, key: str, tier: str):
    from claude_kiro.arbiter_policy import route_ruling, validate_ruling
    from claude_kiro.principles import effective_principles, parse_principles

    project = parse_principles(DEFAULT_PRINCIPLES, "project")
    effective = effective_principles(project, None)
    citable = {pr.id: pr for pr in effective.citable}
    check = validate_ruling(raw, key, citable, "run")
    return check, route_ruling(check, tier, effective)


@pytest.mark.parametrize("form", ["valid", "invalid"])
def test_tc135_settle_equals_validate_ruling_plus_route_ruling(tmp_path, form):
    p, key, _ = opened(tmp_path, form)
    raw = ruling(key, "code") if form == "valid" else ruling(key, "code", principle="project:made-up")
    check, route = _seam_route(raw, key, "normal")
    got = parsed(settle(p, key, raw))
    assert got["route"] == route.route
    assert got["reason"] == route.reason
    assert got["record"] == route.record
    assert got["problems"] == list(check.problems)
    assert check.valid == (form == "valid")


# --------------------------------------------------------------------------- TC-150


def _start_commit(p: ArbiterProject, opened_result: dict) -> str:
    return git(p.root, "rev-parse", INTO)


def _partial_commit(worktree: Path) -> str:
    (worktree / "specs" / "demo" / "tasks.md").write_text("# partial edit from the first attempt\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "partial")
    return git(worktree, "rev-parse", "HEAD")


def test_tc150_rerun_keeps_the_worktree_and_resets_its_branch_to_the_starting_commit(tmp_path):
    p, key, result = opened(tmp_path, "rerun")
    start = _start_commit(p, result)
    worktree, branch = Path(result["worktree"]), result["branch"]
    if not worktree.is_absolute():
        worktree = p.root / worktree
    assert worktree.is_dir() and git(worktree, "rev-parse", "HEAD") == start
    partial = _partial_commit(worktree)
    assert partial != start

    got = parsed(settle(p, key, stronger_look(key)))
    assert got["route"] == "rerun", got
    assert worktree.is_dir(), "a re-run keeps the worktree"
    assert branch_exists(p, branch)
    assert git(worktree, "rev-parse", "HEAD") == start, "the partial commit must be gone"
    assert git(p.root, "rev-parse", branch) == start
    assert git(worktree, "status", "--porcelain") == ""


@pytest.mark.parametrize("outcome", ["test_repair", "code", "needs_input"])
def test_tc150_test_repair_code_and_needs_input_remove_the_worktree_and_branch(tmp_path, outcome):
    p, key, result = opened(tmp_path, outcome)
    worktree, branch = Path(result["worktree"]), result["branch"]
    if not worktree.is_absolute():
        worktree = p.root / worktree
    assert worktree.is_dir() and branch_exists(p, branch)
    if outcome == "needs_input":
        raw = ruling(key, "code", principle="project:made-up")  # invalid: needs input
    else:
        raw = ruling(key, outcome)
    got = parsed(settle(p, key, raw))
    assert got["route"] == outcome, got
    assert not worktree.exists(), "the worktree must be removed"
    assert not branch_exists(p, branch), "the branch must be removed"
    assert str(worktree) not in git(p.root, "worktree", "list", "--porcelain")


def test_tc150_spec_edit_keeps_the_worktree_and_branch_for_the_landing(tmp_path):
    p, key, result = opened(tmp_path, "spec-edit")
    worktree, branch = Path(result["worktree"]), result["branch"]
    if not worktree.is_absolute():
        worktree = p.root / worktree
    tip = _partial_commit(worktree)
    got = parsed(settle(p, key, ruling(key, "spec_edit")))
    assert got["route"] == "spec_edit", got
    assert worktree.is_dir() and branch_exists(p, branch)
    assert git(p.root, "rev-parse", branch) == tip, "the edit commit stays for the landing"


# --------------------------------------------------------------------------- TC-152


def test_tc152_record_with_the_landing_lock_held_past_lock_wait_exits_nonzero_and_writes_nothing(tmp_path):
    p = fresh(tmp_path, "p")
    before = git(p.root, "rev-parse", INTO)
    lock = common_dir(p) / "ck-land-integrate-demo.lock"
    with open(lock, "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the real landing lock, held by this test
        done = record(p, record_payload(1), "--lock-wait", "1")
        assert done.returncode != 0, done.stdout + done.stderr
    assert not (p.target / "specs" / "demo" / "decisions.md").exists()
    assert decisions_entries(p) == [] and decisions_entries(p, "branch") == []
    assert git(p.root, "rev-parse", INTO) == before
    assert git(p.target, "status", "--porcelain") == ""
    assert "lock" in (done.stdout + done.stderr).lower() or "landing" in (done.stdout + done.stderr).lower()
    parsed(record(p, record_payload(1)))  # the same record succeeds once the lock is free
    assert len(decisions_entries(p)) == 1


# --------------------------------------------------------------------------- TC-154


def _ruled(task: str, names: str, first: int) -> dict:
    return {
        f"{task}|test:{TEST_FILE}::test_{n}": {"task": task, "status": "ruled", "calls": 1, "ruling": first + i, "base": "abc1234"}
        for i, n in enumerate(names)
    }


def test_tc154_three_rulings_on_the_task_make_open_ask_and_list_them(tmp_path):
    p = fresh(tmp_path, "p")
    seed_ledger(p, _ruled("2", "abc", 1), last_entry=3)
    got = parsed(open_claim(p, claim_json(task="2", test="test_new")))
    assert got["action"] == "ask", got
    assert got["reason"] == "task-ruling-limit", got
    assert [e["ruling"] for e in got["earlier"]] == [1, 2, 3], got
    assert got["worktree"] is None and got["branch"] is None


def test_tc154_three_rulings_on_another_task_do_not_count(tmp_path):
    p = fresh(tmp_path, "p")
    seed_ledger(p, _ruled("1", "abc", 1), last_entry=3)
    got = parsed(open_claim(p, claim_json(task="2", test="test_new")))
    assert got["action"] == "rule", got
    assert got["reason"] == "", got
    assert got["earlier"] == [] or got["earlier"] is None or all(e["ruling"] not in (1, 2, 3) for e in got["earlier"])
