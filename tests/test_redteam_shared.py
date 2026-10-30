"""Red team of requirement 11.1: the shared record SHALL NOT include command text, paths, spec names or reason text.

Working from the requirement only, each test plants confidential values where an interruption count is derived from
(labels, descriptions, commands, file paths, spec names, guard-log fields, the local record's own keys) and tries to
get one into `ck stats export` or `ck stats submit` output. Every break is a test:

* the shareable record validates (`validate_shareable`) and holds only numbers under enum keys;
* the export bytes, the pushed commits and the PR body hold no planted value, in any case or encoding;
* an export whose note holds a planted value is refused (a leak hit, no note written);
* a spec named like a ck vocabulary word is never refused.

Real CLI, fake Claude home, real git with a local bare remote, a fake `gh`. ck code is imported inside test bodies.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List
from urllib.parse import quote, unquote

import pytest

from stats_helpers import (
    CK,
    AgentSpec,
    FakeClaudeHome,
    ToolAgentSpec,
    ck_sub,
    all_bytes,
    backdate_manifest,
    begin,
    described,
    fake_claude_drafter,
    fake_gh,
    flatten_paths,
    fresh_env,
    gh_calls,
    git,
    git_project,
    interruptions as add_interruptions,
    iso_ms,
    kiro_checkout,
    load_record,
    run_tty,
    spec_repo,
    stats,
    to_ms,
    write_guard_log,
)

pytestmark = pytest.mark.usefixtures("no_network")

PLANTED = ("acme-ledger-internal", "test_payroll_acme", "zephyr-payroll", "quarterly payroll", "/home/dana")
HIT_WORDS = re.compile(r"(?i)(?:[0-9a-f]{16}\.md|note)\S*.*\bline\s+\d+")
SHARED_KINDS = ("prompts", "guard_denials", "post_check_catches", "guard_escalations", "auto_mode_denials", "headless_denials",
                "user_rejects", "isolation_refusals")
T0 = to_ms("2026-10-12T09:02:00Z")


@pytest.fixture
def env():
    with fresh_env() as e:
        yield e


# --------------------------------------------------------------------------- helpers


def variants(token: str) -> List[bytes]:
    """The token as it could leak: as is, lower, upper, URL-encoded (with and without encoded dashes)."""
    forms = {token, token.lower(), token.upper(), quote(token, safe=""), quote(token, safe="").replace("-", "%2D"), quote(token, safe="").replace("-", "%2d")}
    return sorted(f.lower().encode() for f in forms)


def decoded_lower(data: bytes) -> bytes:
    text = data.decode("utf-8", "replace")
    return (text + "\n" + unquote(text)).lower().encode()


def assert_no_planted(data: bytes, tokens=PLANTED) -> None:
    low = decoded_lower(data)
    for token in tokens:
        for form in variants(token):
            assert form not in low, f"{token!r} leaked as {form!r}"


def shared_records(checkout: Path) -> List[dict]:
    root = Path(checkout) / "analytics" / "runs"
    return [json.loads(p.read_text()) for p in sorted(root.rglob("*.json"))] if root.exists() else []


def assert_block_is_clean(record: dict) -> None:
    from claude_kiro.analytics import share

    assert record["schema"] == "ck-run-analytics/1"
    assert share.validate_shareable(record) == []
    block = record.get("interruptions")
    if block is None:
        return
    allowed = {*SHARED_KINDS, "wait_minutes", "escalation_models", "by_role", "wait_minutes_by_role", *share.INTERRUPTION_ROLES,
               *share.COMMAND_FAMILIES, *share.MODEL_FAMILIES}
    for path in flatten_paths(block):
        assert set(path.split(".")) <= allowed, path
    stack = [block]
    while stack:
        node = stack.pop()
        for value in node.values():
            if isinstance(value, dict):
                stack.append(value)
            else:
                assert isinstance(value, (int, float)) and not isinstance(value, bool), value


def export_all(env, checkout: Path):
    return stats("export", "--to", str(checkout), "--yes", env=env, cwd=checkout)


def simple_world(env, *, spec: str = "zephyr-payroll", label: str = "task 1", commands=None, orch=None):
    """A spec-implement run with two sed prompts by the implementer (plus whatever is planted) -> (project, session, run)."""
    proj = git_project(env.root)
    home = FakeClaudeHome(env.claude)
    sess = home.session(cwd=proj)
    run = sess.implement_run(start="2026-10-12T09:00:00Z", args={"spec": spec, "root": str(proj)},
                             agents=[AgentSpec(label), AgentSpec("review 1", model="claude-opus-5-5")])
    cmds = commands or ["sed -i s/a/b/ tests/test_calc.py", "sed -i s/c/d/ tests/test_calc.py"]
    add_interruptions((run, 0), [("prompt", "implementer", "Bash", c, T0 + i * 60_000, 30_000) for i, c in enumerate(cmds)])
    if orch:
        add_interruptions(sess, orch)
    return proj, sess, run


def collect(env, proj: Path) -> None:
    r = stats("collect", "--all", env=env, cwd=proj)
    assert r.exit_code == 0, r.output


def export_and_check(env, tokens=PLANTED) -> List[dict]:
    checkout, _bare = kiro_checkout(env.root)
    export_all(env, checkout)
    assert_no_planted(all_bytes(checkout / "analytics"), tokens)
    records = shared_records(checkout)
    for record in records:
        assert_block_is_clean(record)
    return records


# --------------------------------------------------------------------------- labels, descriptions, programs


def test_a_label_whose_text_is_a_path_or_spec_name_never_reaches_the_record(env):
    """A workflow label and an orchestrator file path carry confidential text."""
    label = "task 1 /home/dana/acme-ledger-internal/src/test_payroll_acme.py zephyr-payroll"
    proj, _sess, _run = simple_world(env, label=label, orch=[("guard_denial", "orchestrator", "Edit", "/home/dana/acme-ledger-internal/quarterly payroll.md", T0 + 500_000, None)])
    collect(env, proj)
    records = export_and_check(env)
    assert len(records) == 1 and records[0]["interruptions"]["guard_denials"] == 1


def test_an_agent_mode_description_whose_text_is_a_path_or_spec_name_never_reaches_the_record(env):
    """`[ck:<id>] implement task 1 <model text>` where the model text names a path and a spec."""
    repo = spec_repo(Path(env.root) / "repo", spec="zephyr-payroll", into="integrate/zephyr-payroll")
    home = FakeClaudeHome(env.claude)
    sess = home.session(cwd=repo, git_branch="integrate/zephyr-payroll")
    rid, manifest = begin(env, repo, "implement", "zephyr-payroll", "integrate/zephyr-payroll")
    backdate_manifest(manifest, 7)
    base = to_ms(json.loads(Path(manifest).read_text())["startTime"])
    agents = sess.tool_agents([
        ToolAgentSpec(described(rid, "implement task 1 /home/dana/acme-ledger-internal/test_payroll_acme.py"), agent_type="spec:implementer"),
        ToolAgentSpec(described(rid, "review task 1 zephyr-payroll quarterly payroll"), agent_type="spec:reviewer"),
    ], start=iso_ms(base + 5_000))
    add_interruptions((agents, 0), [("prompt", "implementer", "Bash", "sed -i s/a/b/ tests/test_payroll_acme.py", base + 60_000, 20_000)])
    add_interruptions((agents, 1), [("isolation", "reviewer", "Bash", "cat /home/dana/acme-ledger-internal/x", base + 90_000, None)])
    assert stats("collect", "--agents", rid, env=env, cwd=repo).exit_code == 0
    records = export_and_check(env)
    assert len(records) == 1 and records[0]["interruptions"]["prompts"] == 1


@pytest.mark.parametrize("program", ["zephyr-payroll --apply", "acme-ledger-internal-cli run", "/home/dana/bin/test_payroll_acme", "quarterly payroll"])
def test_a_tool_use_program_named_like_a_spec_is_family_other(env, program):
    """The family is a fixed enum: a program that looks like a spec or a path falls under `other`, never under its own name."""
    proj, _sess, _run = simple_world(env, commands=[program, program + " --again"])
    collect(env, proj)
    records = export_and_check(env)
    by_role = records[0]["interruptions"]["by_role"]
    assert by_role["implementer"]["other"]["prompts"] == 2, by_role


# --------------------------------------------------------------------------- local keys outside the fixed sets


def _extra_top_level_keys(rec: dict) -> None:
    block = rec["interruptions"]
    block["command"] = "sed -i s/a/b/ tests/test_payroll_acme.py"
    block["spec"] = "zephyr-payroll"
    block["totals"]["acme-ledger-internal"] = 3
    block["waitMsByRole"]["quarterly payroll"] = 9_999
    block["escalationModels"]["my-finetune-acme-ledger-internal"] = 2


def _foreign_role_family_kind(rec: dict) -> None:
    by_role = rec["interruptions"]["byRole"]
    by_role["acme-ledger-internal"] = {"zephyr-payroll": {"test_payroll_acme": 4}}
    by_role["implementer"]["acme-ledger-internal"] = {"prompts": 1, "quarterly payroll": 2}
    by_role["implementer"]["sed"]["/home/dana/x"] = 5


def _guard_reason_in_a_key(rec: dict) -> None:
    reason = "[ck guard] Denied: deleting test_payroll_acme.py loses quarterly payroll coverage for acme-ledger-internal"
    by_role = rec["interruptions"]["byRole"]
    by_role[reason] = {"sed": {"prompts": 1}}
    by_role["implementer"][reason] = {"guardDenials": 1}
    rec["interruptions"]["waitMsByRole"][reason] = 1_000
    rec["interruptions"]["escalationModels"][reason] = 1


def _numeric_fields_holding_strings(rec: dict) -> None:
    block = rec["interruptions"]
    block["totals"]["prompts"] = "acme-ledger-internal"
    block["totals"]["waitMs"] = "sed -i s/a/b/ tests/test_payroll_acme.py"
    block["totals"]["guardDenials"] = "/home/dana/zephyr-payroll"
    block["byRole"]["implementer"]["sed"]["prompts"] = "quarterly payroll"
    block["waitMsByRole"]["implementer"] = "zephyr-payroll"
    block["escalationModels"] = {"fable": "acme-ledger-internal"}


MUTATIONS: Dict[str, Callable[[dict], None]] = {
    "extra-keys": _extra_top_level_keys,
    "foreign-role-family-kind": _foreign_role_family_kind,
    "guard-reason-in-a-key": _guard_reason_in_a_key,
    "strings-in-numeric-fields": _numeric_fields_holding_strings,
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_a_local_record_with_foreign_keys_or_string_numbers_leaks_nothing(env, name):
    """The local record is user-editable data: whatever it holds, the shared object keeps only numbers under enum keys."""
    proj, _sess, run = simple_world(env)
    collect(env, proj)
    path = env.xdg / "claude-kiro" / "runs" / f"{run.run_id}.json"
    rec = json.loads(path.read_text())
    MUTATIONS[name](rec)
    path.write_text(json.dumps(rec))
    records = export_and_check(env)
    for record in records:  # what was a clean count before the edit is still counted, and nothing was remapped to `other`
        block = record.get("interruptions")
        if block is not None and name != "strings-in-numeric-fields":
            assert block["by_role"]["implementer"]["sed"]["prompts"] == 2, block["by_role"]
            assert "other" not in block["by_role"]["implementer"], block["by_role"]


# --------------------------------------------------------------------------- vocabulary-named specs, encodings


@pytest.mark.parametrize("spec", ["sed", "implementer", "tests", "prompts", "reviewer"])
def test_a_spec_named_like_a_ck_vocabulary_word_is_not_refused(env, spec):
    """The enum keys of the interruptions block (`sed`, `implementer`, `prompts`) are not leaks when a spec is called that."""
    proj, _sess, _run = simple_world(env, spec=spec)
    collect(env, proj)
    checkout, _bare = kiro_checkout(env.root)
    out = export_all(env, checkout)
    assert out.exit_code == 0, out.output
    records = shared_records(checkout)
    assert len(records) == 1, out.output
    assert_block_is_clean(records[0])
    assert records[0]["interruptions"]["by_role"]["implementer"]["sed"]["prompts"] == 2


def test_mixed_case_url_encoded_and_split_values_do_not_leak(env):
    """The same confidential value spelled in mixed case, percent-encoded and split across fields."""
    cmds = ["cat ACME-Ledger-Internal/Test_Payroll_ACME.py", "cat acme%2Dledger%2Dinternal/x", "cat acme-ledger-" + "internal", "echo Quarterly%20Payroll"]
    proj, _sess, _run = simple_world(env, spec="Zephyr-Payroll", label="task 1 ACME-Ledger-Internal", commands=cmds)
    collect(env, proj)
    records = export_and_check(env, tokens=(*PLANTED, "ledger-internal"))
    assert records and records[0]["interruptions"]["prompts"] == 4


# --------------------------------------------------------------------------- the system's own output fed back


def test_one_runs_export_fed_back_as_the_next_runs_transcript_changes_no_count(env):
    """A shared record quoted in a later run's transcript (as text, an error result and a hook attachment) counts as nothing."""
    proj, sess, _run = simple_world(env)
    collect(env, proj)
    checkout, _bare = kiro_checkout(env.root)
    assert export_all(env, checkout).exit_code == 0
    exported = [p.read_text() for p in sorted((checkout / "analytics" / "runs").rglob("*.json"))]
    assert exported
    stamp = "2026-10-14T09:03:00.000Z"
    lines: List[Any] = []
    for i, text in enumerate(exported):
        quoted = f"1\t{text}\n2\tend"
        lines += [
            {"type": "user", "timestamp": stamp, "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"toolu_fb{i}a", "content": quoted}]}},
            {"type": "user", "timestamp": stamp, "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"toolu_fb{i}b", "is_error": True, "content": f"make failed, printing: {text}"}]}},
            {"type": "attachment", "timestamp": stamp, "attachment": {"type": "hook_success", "hookName": "PreToolUse:Bash", "hookEvent": "PreToolUse", "toolUseID": f"toolu_fb{i}c", "stdout": text, "stderr": "", "exitCode": 0, "command": "ck --hook pre-tool-guard"}},
            {"type": "attachment", "timestamp": stamp, "attachment": {"type": "hook_blocking_error", "hookEvent": "PostToolUse", "toolUseID": f"toolu_fb{i}d", "blockingError": {"blockingError": text}}},
            {"type": "user", "timestamp": stamp, "message": {"role": "user", "content": text}},
        ]
    second = sess.implement_run(start="2026-10-14T09:00:00Z", args={"spec": "zephyr-payroll", "root": str(proj)}, agents=[AgentSpec("task 1", extra_lines=lines)])
    collect(env, proj)
    block = load_record(env.xdg, second.run_id)["interruptions"]
    assert block["totals"] == {"prompts": 0, "waitMs": 0, "guardDenials": 0, "postCheckCatches": 0, "guardEscalations": 0,
                               "autoModeDenials": 0, "headlessDenials": 0, "userRejects": 0, "isolationRefusals": 0}, block["totals"]
    assert not any(n for fams in block["byRole"].values() for counts in fams.values() for n in counts.values())
    out = export_all(env, checkout)
    assert out.exit_code == 0, out.output
    for record in shared_records(checkout):
        assert_block_is_clean(record)


# --------------------------------------------------------------------------- guard-log values: notes, export, submit


PLANTED_LOG = [
    {"ts_ms": T0 + 30_000, "decision": "deny", "category": "test_delete", "command": "rm tests/test_payroll_acme.py", "path": "tests/test_payroll_acme.py",
     "reason": "[ck guard] Denied: deleting quarterly payroll coverage for acme-ledger-internal"},
    {"ts_ms": T0 + 90_000, "decision": "ask", "category": "test_weakening", "command": "sed -i s/41250/0/ tests/test_payroll_acme.py",
     "path": "tests/test_payroll_acme.py", "reason": "[ck guard] weakens quarterly payroll"},
]
NOTE_VALUES = ["rm tests/test_payroll_acme.py", "tests/test_payroll_acme.py", "[ck guard] Denied: deleting quarterly payroll coverage for acme-ledger-internal"]


def with_log(env, sess) -> None:
    write_guard_log(env, [{**e, "sessionId": sess.session_id} for e in PLANTED_LOG])


def test_an_export_whose_note_holds_a_guard_log_value_is_refused(env):
    """A note that repeats a logged command, path or reason is a leak hit and is not written; the record itself stays clean."""
    proj = git_project(env.root)
    home = FakeClaudeHome(env.claude)
    sess = home.session(cwd=proj)
    runs = [sess.implement_run(start=f"2026-10-{12 + i}T09:00:00Z", args={"spec": "demo", "root": str(proj)}, agents=[AgentSpec("task 1")]) for i in range(len(NOTE_VALUES) + 1)]
    add_interruptions((runs[0], 0), [("prompt", "implementer", "Bash", "sed -i s/a/b/ tests/test_calc.py", to_ms("2026-10-12T09:03:00Z"), 5_000)])
    with_log(env, sess)
    checkout, _bare = kiro_checkout(env.root)
    bin_dir = Path(env.root) / "bin"
    bin_dir.mkdir()
    run_env = {**env, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    assert stats("collect", "--run", runs[0].run_id, env=run_env, cwd=proj).exit_code == 0
    assert stats("export", "--to", str(checkout), "--yes", env=run_env, cwd=checkout).exit_code == 0
    for run, value in zip(runs[1:], NOTE_VALUES):
        assert stats("collect", "--run", run.run_id, env=run_env, cwd=proj).exit_code == 0
        fake_claude_drafter(bin_dir, f"Resolver needed 3 rounds because of {value}")
        code, text = run_tty([*CK, "stats", "export", "--to", str(checkout), "--note"], env=run_env, cwd=checkout, script=[(r"(?i)accept", "a\n")])
        assert code == 0, text
        assert HIT_WORDS.search(text), text
        assert sorted((checkout / "analytics").rglob("*.md")) == [], text
    assert_no_planted(all_bytes(checkout / "analytics"))


def test_export_and_submit_payloads_hold_no_planted_value(env):
    """Everything planted at once; what is exported, committed, pushed and put in the PR body is clean."""
    label = "task 1 /home/dana/acme-ledger-internal/test_payroll_acme.py"
    proj, sess, run = simple_world(env, label=label, commands=["acme-ledger-internal --dump", "sed -i s/a/b/ tests/test_payroll_acme.py"],
                                   orch=[("reject", "orchestrator", "Edit", "/home/dana/quarterly payroll.md", T0 + 400_000, None)])
    with_log(env, sess)
    collect(env, proj)
    checkout, bare = kiro_checkout(env.root)
    bin_dir = Path(env.root) / "bin"
    bin_dir.mkdir()
    gh_log = Path(env.root) / "gh.jsonl"
    fake_gh(bin_dir, gh_log)
    run_env = {**env, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    label = ck_sub("agents", "set", "run.machine_label", "testbox", env=run_env, cwd=proj)
    assert label.exit_code == 0, label.output
    out = stats("submit", "--to", str(checkout), "--yes", env=run_env, cwd=checkout)
    assert out.exit_code == 0, out.output
    calls = gh_calls(gh_log)
    assert any(c["argv"][:2] == ["pr", "create"] for c in calls), calls
    assert_no_planted(json.dumps(calls).encode())
    assert_no_planted(git(bare, "log", "-p", "--all").encode())
    assert_no_planted(all_bytes(checkout / "analytics"))
    batches = [b for b in git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads").split() if b.startswith("ck-stats-batch-")]
    assert batches, "nothing was pushed"
    pushed: List[dict] = []
    for branch in batches:
        for name in git(bare, "ls-tree", "-r", "--name-only", branch).split():
            if name.startswith("analytics/runs/") and name.endswith(".json"):
                pushed.append(json.loads(git(bare, "show", f"{branch}:{name}")))
    assert pushed, "no record was pushed"
    for record in pushed:
        assert_block_is_clean(record)
    assert pushed[0]["interruptions"]["user_rejects"] == 1
