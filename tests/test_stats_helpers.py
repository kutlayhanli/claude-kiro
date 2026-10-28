"""Self-checks of the `ck stats` test harness (tests/stats_helpers.py). No ck stats code is exercised."""

import json
import os
import socket
import subprocess
import sys

import pytest

from claude_kiro import bench as b
from stats_helpers import (
    CK, CONFIDENTIAL, AgentSpec, FakeClaudeHome, all_bytes, ck_sub, create_result, fake_claude_drafter, fake_claude_session,
    fake_editor, fake_gh, flatten_paths, fresh_env, gh_calls, git, git_project, implement_result, kiro_checkout,
    path_without_gh, run_tty, seeded_home, seeded_local_record, stats, store_files, TaskSpec,
)


def test_a_fake_run_is_found_by_bench_and_priced_by_ck_cost(tmp_path):
    home = FakeClaudeHome(tmp_path / "claude-home")
    session = home.session(cwd=tmp_path)
    run = session.implement_run(agents=[
        AgentSpec("task 1", model="claude-sonnet-5-5", requests=[("r1", 1_000_000, 1_000_000, 0, 0)]),
        AgentSpec("review 1", model="claude-opus-5-5", requests=[("r2", 0, 100_000, 10_000_000, 0)]),
    ])
    found = b.find_workflow_run(home.root, session.session_id)
    assert found["runId"] == run.run_id and found["workflowName"] == "spec-implement" and found["status"] == "completed"
    assert set(found) >= {"runId", "workflowName", "status", "startTime", "durationMs", "args", "result", "workflowProgress"}
    agent = [e for e in found["workflowProgress"] if e["type"] == "workflow_agent"][0]
    assert set(agent) >= {"agentId", "label", "phaseTitle", "model", "startedAt", "durationMs", "attempt", "tokens", "state",
                          "promptPreview", "resultPreview", "lastToolSummary"}
    out = ck_sub("cost", "--run", run.run_id, "--json", env={"CLAUDE_CONFIG_DIR": str(home.root)})
    assert out.exit_code == 0, out.output
    data = json.loads(out.stdout)
    # sonnet 1M in + 1M out = 2 + 10; opus 100k out = 2, 10M cache reads = 2.0
    assert data["run_id"] == run.run_id and data["session_id"] == session.session_id
    assert data["priced_usd"] == pytest.approx(12.0 + 2.0 + 2.0)


def test_a_launch_is_written_to_the_session_transcript(tmp_path):
    home = FakeClaudeHome(tmp_path / "h")
    session = home.session(cwd=tmp_path)
    run = session.implement_run()
    session.create_run(launch={"scriptPath": "/x/.claude/workflows/spec-create.js"})
    session.foreign_run(launch=False)
    lines = [json.loads(l) for l in session.transcript_path.read_text().splitlines()]
    uses = [blk for l in lines if l.get("type") == "assistant" for blk in l["message"]["content"] if blk.get("name") == "Workflow"]
    assert [u["input"].get("name") for u in uses] == ["spec:spec-implement", None]
    assert uses[1]["input"] == {"scriptPath": "/x/.claude/workflows/spec-create.js"}
    result = [blk for l in lines if l.get("type") == "user" and isinstance(l["message"]["content"], list)
              for blk in l["message"]["content"] if blk.get("type") == "tool_result"]
    assert uses[0]["id"] == result[0]["tool_use_id"] and run.run_id in result[0]["content"]
    assert lines[0]["version"] == "2.1.80" and lines[0]["cwd"] == str(tmp_path) and lines[0]["gitBranch"] == "integrate/x"


def test_split_blocks_lines_share_a_request_id_and_timestamps_span_the_duration(tmp_path):
    home = FakeClaudeHome(tmp_path / "h")
    run = home.session(cwd=tmp_path).implement_run(agents=[
        AgentSpec("task 1", requests=[("rq1", 1, 2, 3, 4), ("rq2", 5, 6, 7, 8)], duration_ms=90_000, started_at_ms=1_760_000_000_000),
        AgentSpec("task 2", split_blocks=False, requests=[("rq3", 1, 1, 1, 1)]),
    ])
    lines = [json.loads(l) for l in run.transcript(0).read_text().splitlines()]
    assistants = [l for l in lines if l["type"] == "assistant"]
    assert [l["requestId"] for l in assistants] == ["rq1", "rq1", "rq2", "rq2"]
    from stats_helpers import to_ms
    assert to_ms(lines[-1]["timestamp"]) - to_ms(lines[0]["timestamp"]) == 90_000
    assert all(l["timestamp"].endswith("Z") for l in lines)
    assert len([json.loads(l) for l in run.transcript(1).read_text().splitlines() if '"assistant"' in l]) == 1
    meta = json.loads(run.meta_path(0).read_text())
    assert meta["description"] == "task 1" and meta["workflowPhase"] == "Wave 1"


def test_fake_run_helpers_damage_exactly_what_they_name(tmp_path):
    home = FakeClaudeHome(tmp_path / "h")
    session = home.session(cwd=tmp_path)
    run = session.implement_run()
    run.drop_transcript(0)
    run.truncate_transcript(1)
    run.drop_meta(2)
    assert not run.transcript(0).exists() and run.meta_path(0).exists()
    with pytest.raises(json.JSONDecodeError):
        json.loads(run.transcript(1).read_text().splitlines()[-1])
    assert not run.meta_path(2).exists() and run.transcript(2).exists()
    run.set(status="running")
    assert json.loads(run.path.read_text())["status"] == "running"
    run.corrupt_format()
    assert isinstance(json.loads(run.path.read_text())["workflowProgress"], dict)
    run.add_budget(12.5)
    assert b.session_spend(home.root, session.session_id)["claude_usd"] == 12.5
    run.drop_run_file()
    assert not run.path.exists() and run.agents_dir.exists() and session.transcript_path.exists()


def test_result_shapes(tmp_path):
    r = implement_result([TaskSpec(task="1", ok=True, idle_minutes=7), TaskSpec(task="2", ok=False, reason="x")])
    assert r["outcome"] == "complete" and r["merged"] == ["1"] and r["failed"][0]["task"] == "2"
    t1 = r["tasks"][0]
    assert t1["resolves"] == 0 and t1["toMergedMinutes"] - t1["agentMinutes"] == pytest.approx(7)
    assert {"task", "wave", "title", "attempts", "ok", "escalations", "riskTag", "startModel", "reviewModel", "review",
            "reason", "startedAt", "finishedAt", "mergedAt"} <= set(t1)
    assert {"verdict", "rounds", "findings", "summary"} <= set(t1["review"])
    assert "resolves" not in implement_result(resolves=False)["tasks"][0]
    assert "outcome" not in implement_result(outcome=None)
    assert {"mode", "feature", "specDir", "files", "authored", "review", "consistency", "schedule"} <= set(create_result())
    assert create_result()["review"]["refutedCount"] == 2


def test_kiro_checkout_fetch_and_push_reach_the_bare_repo_offline(tmp_path, no_network):
    checkout, bare = kiro_checkout(tmp_path)
    assert git(checkout, "config", "remote.origin.url") == "https://github.com/kutlayhanli/claude-kiro.git"
    git(checkout, "fetch", "origin")
    git(checkout, "checkout", "-q", "-b", "ck-stats-batch-1", "origin/master")
    (checkout / "f.txt").write_text("x\n")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-qm", "add")
    git(checkout, "push", "-q", "origin", "ck-stats-batch-1")
    assert "ck-stats-batch-1" in git(bare, "branch", "--list")
    assert not os.path.exists(os.environ["CK_TEST_NET_LOG"])


def test_kiro_checkout_options(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    checkout, bare = kiro_checkout(a, unpushed_commit=True)
    assert git(checkout, "branch", "--show-current") == "local-work"
    assert git(checkout, "rev-parse", "HEAD") not in git(bare, "log", "--all", "--format=%H")
    d = tmp_path / "b"
    d.mkdir()
    fork, bare2 = kiro_checkout(d, "https://github.com/someone/claude-kiro.git",
                                extra_remotes={"upstream": "https://github.com/kutlayhanli/claude-kiro.git"})
    git(fork, "push", "-q", "upstream", "HEAD:refs/heads/probe")
    assert "probe" in git(bare2, "branch", "--list")
    assert "probe" not in git(d / "other-bare-origin.git", "branch", "--list")


def test_fake_gh_logs_argv_and_body(tmp_path):
    bin_dir = tmp_path / "bin"
    log = tmp_path / "gh.log"
    fake_gh(bin_dir, log)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    ok = subprocess.run(["gh", "pr", "create", "--draft", "--title", "T", "--body", "B"], env=env, capture_output=True, text=True)
    assert ok.returncode == 0 and ok.stdout.strip().endswith("/pull/101")
    assert subprocess.run(["gh", "auth", "status"], env={**env, "FAKE_GH_AUTH_RC": "1"}).returncode == 1
    assert subprocess.run(["gh", "pr", "create"], env={**env, "FAKE_GH_FAIL_RC": "4"}, capture_output=True).returncode == 4
    calls = gh_calls(log)
    assert calls[0] == {"argv": ["pr", "create", "--draft", "--title", "T", "--body", "B"], "body": "B"}
    assert [c["argv"][:2] for c in calls] == [["pr", "create"], ["auth", "status"], ["pr", "create"]]


def test_path_without_gh_hides_the_real_gh(tmp_path):
    p = path_without_gh(tmp_path)
    assert subprocess.run(["sh", "-c", "command -v gh"], env={"PATH": p}, capture_output=True).returncode != 0
    assert subprocess.run(["sh", "-c", "command -v git"], env={"PATH": p}, capture_output=True).returncode == 0


def test_drafter_and_editor_fakes(tmp_path):
    d = fake_claude_drafter(tmp_path / "bin", "the note")
    assert d.calls() == []
    out = subprocess.run([str(d.path), "-p", "prompt"], input="in", capture_output=True, text=True)
    assert out.stdout == "the note" and d.calls() == [{"argv": ["-p", "prompt"], "stdin": "in"}]
    f = tmp_path / "note.md"
    f.write_text("old")
    subprocess.run([str(fake_editor(tmp_path / "bin", "new text")), str(f)], check=True)
    assert f.read_text() == "new text"


def test_run_tty_drives_a_prompt():
    code = "import sys; sys.stdout.write('name? '); sys.stdout.flush(); n = sys.stdin.readline().strip(); print('hi ' + n); sys.exit(3)"
    status, text = run_tty([sys.executable, "-c", code], env={}, cwd=None, script=[(r"name\? ", "ada\n")])
    assert status == 3 and "hi ada" in text


def test_run_tty_reports_a_prompt_that_never_comes():
    with pytest.raises(AssertionError, match="never|ended before"):
        run_tty([sys.executable, "-c", "print('bye')"], env={}, cwd=None, script=[(r"never", "x\n")])


def test_no_network_raises_in_process_and_reaches_children(no_network):
    with pytest.raises(AssertionError, match="network"):
        socket.create_connection(("1.1.1.1", 80))
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(AssertionError, match="network"):
        s.connect(("1.1.1.1", 80))
    s.close()
    child = subprocess.run([sys.executable, "-c", "import socket; socket.create_connection(('1.1.1.1', 80))"], capture_output=True, text=True)
    assert child.returncode != 0
    lines = open(os.environ["CK_TEST_NET_LOG"]).read().splitlines()
    assert len(lines) == 1 and "1.1.1.1" in lines[0]


def test_two_fresh_envs_are_disjoint():
    with fresh_env() as a:
        assert len({a.claude, a.xdg, a.config, a.home}) == 4 and a.root.is_dir()
        assert all(d.is_dir() and d.parent == a.root for d in (a.claude, a.xdg, a.config, a.home))
        (a.xdg / "marker").write_text("x")
        with fresh_env() as b2:
            assert not (b2.xdg / "marker").exists() and b2.root != a.root
            assert {v for v in a.values() if v.startswith('/')}.isdisjoint({v for v in b2.values() if v.startswith('/')})
        assert (a.xdg / "marker").exists()
    assert not a.root.exists()
    assert set(a) >= {"CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "CK_CONFIG_HOME", "GIT_CONFIG_GLOBAL", "HOME"}


def test_stats_and_ck_sub_run_the_cli_as_a_subprocess(tmp_path):
    out = ck_sub("--help")
    assert out.exit_code == 0 and "Usage" in out.output
    assert CK[:2] == [sys.executable, "-m"]
    assert stats("--help").exit_code in (0, 2)  # `ck stats` may not exist yet


def test_fake_claude_session_writes_runs_for_the_session_id(tmp_path):
    bin_dir = tmp_path / "bin"
    fake_claude_session(bin_dir, [
        {"kind": "implement", "kwargs": {"run_id": "wf_one"}},
        {"kind": "foreign", "kwargs": {"run_id": "wf_two", "name": "tax-review-2026"}},
    ], exit_code=7)
    home = tmp_path / "claude-home"
    proj = tmp_path / "proj"
    proj.mkdir()
    out = subprocess.run(["claude", "--session-id", "sid-1", "-p", "x"], cwd=proj, capture_output=True, text=True,
                         env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "CLAUDE_CONFIG_DIR": str(home)})
    assert out.returncode == 7, out.stderr
    assert (home / "projects" / "-fake-ck-run" / "sid-1" / "workflows" / "wf_one.json").exists()
    assert json.loads(next((home / "projects").glob("*/sid-1/workflows/wf_two.json")).read_text())["workflowName"] == "tax-review-2026"
    assert b.find_run_session(home, "wf_one") == "sid-1"


def test_store_readers(tmp_path):
    xdg = tmp_path / "xdg"
    assert store_files(xdg) == []
    (xdg / "claude-kiro" / "runs").mkdir(parents=True)
    (xdg / "claude-kiro" / "runs" / "wf_1.json").write_text('{"a": 1}')
    assert [p.name for p in store_files(xdg)] == ["wf_1.json"]
    from stats_helpers import load_record
    assert load_record(xdg, "wf_1") == {"a": 1}
    assert b"a" in all_bytes(xdg) and all_bytes(tmp_path / "missing") == b""
    assert flatten_paths({"a": [{"b": 1, "c": [2]}], "d": {}, "e": None}) == {"a[].b", "a[].c[]", "d", "e"}


def test_seeded_home_plants_every_confidential_string(tmp_path):
    home, terms = seeded_home(tmp_path)
    assert set(terms) == set(CONFIDENTIAL)
    blob = all_bytes(tmp_path).decode(errors="replace")  # run files, transcripts and the projects' git config
    missing = [t for t in CONFIDENTIAL if t not in blob]
    assert not missing, f"not planted anywhere: {missing}"
    # the git identity and remotes live in the project repos
    a = home.projects_by_name["acme-ledger"]
    assert git(a, "config", "user.name") == "Dana Scully" and git(a, "config", "remote.origin.url") == "git@github.com:acme-corp/acme-ledger.git"
    assert git(home.projects_by_name["orion-billing"], "config", "remote.origin.url") == "https://gitlab.example.com/orion/billing"
    for t in ("Reconcile Globex invoices", "Initech SSN column", "/srv/acme/ledger.py", "/home/dana/acme-ledger/src/ledger.py",
              "src/ledger_globex.py", "integrate/zephyr-payroll", "dana.scully@acme-corp.com", "2026-10-12T09:13:27Z"):
        assert t in blob
    for sid in ("tax-review-2026", "competitor-teardown-q4"):
        assert sid in blob
    # ck-only runs and foreign runs are both findable
    names = {json.loads(p.read_text())["workflowName"] for p in home.root.glob("projects/*/*/workflows/*.json")}
    assert names == {"spec-implement", "spec-create", "competitor-teardown-q4", "tax-review-2026"}


@pytest.mark.parametrize("kind", ["spec-implement", "spec-create"])
def test_seeded_local_record_has_the_contract_keys_and_plants_strings(kind):
    rec = seeded_local_record(kind)
    common = {"schema", "runId", "workflow", "outcome", "ckVersion", "collectorVersion", "claudeCodeVersion", "startTime", "wallMs",
              "agentsConfig", "projectPseudonym", "outcomeInferred", "sessionId", "workflowName", "status", "project", "terms", "costs", "byModel", "byRole", "byPhase", "byTask", "agents", "missing"}
    extra = ({"tasks", "landings", "fullGates", "finalGate", "crossTests", "fixerEscalations", "phases"} if kind == "spec-implement"
             else {"review", "consistency", "phases"})
    assert set(rec) == common | extra and rec["workflow"] == kind
    assert set(rec["agents"][0]) == {"agentId", "label", "role", "task", "attempt", "round", "model", "phase", "ms", "usd", "input",
                                     "output", "cacheRead", "cacheCreation"}
    assert set(rec["costs"][0]) == {"usd", "source", "pricingDate", "scope"}
    blob = json.dumps(rec)
    assert [t for t in CONFIDENTIAL if t not in blob] == []
    if kind == "spec-implement":
        assert set(rec["tasks"][0]) == {"task", "title", "reason", "attempts", "retries", "escalations", "reviewRounds", "verdict", "resolves",
                                        "resolvesInferred", "fixes", "agentMinutes", "toMergedMinutes", "riskTag", "idleBeforeMergeMs"}


def test_the_helpers_module_never_imports_the_cli():
    from pathlib import Path
    text = (Path(__file__).parent / "stats_helpers.py").read_text()
    assert "import claude_kiro.cli" not in text and "from claude_kiro.cli" not in text and "from claude_kiro import cli" not in text
