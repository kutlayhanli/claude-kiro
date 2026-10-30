"""Property tests of shared records and `ck stats show` for interruption counts (P-22, P-27, P-29).

CLI-backed: every example builds its fake Claude home, store, kiro checkout and repository inside `with fresh_env()`
in the `@given` body and runs `ck stats` as a subprocess. ck code is imported inside test bodies only.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import List

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from stats_helpers import (
    CK,
    AgentSpec,
    FakeClaudeHome,
    all_bytes,
    fake_claude_drafter,
    flatten_paths,
    fresh_env,
    git_project,
    interruption_strategy,
    interruptions as add_interruptions,
    kiro_checkout,
    load_record,
    run_tty,
    stats,
    to_ms,
    write_guard_log,
)

PROPERTIES = {
    "P-22": "test_p22_a_planted_guard_log_value_is_refused_in_a_note_and_absent_from_the_record",
    "P-27": "test_p27_records_without_interruption_data_show_not_recorded_and_are_not_shared_or_counted",
    "P-29": "test_p29_the_shared_interruptions_object_validates_and_holds_only_numbers_and_enum_keys",
}

PROP = dict(max_examples=40, derandomize=True, database=None, deadline=None, suppress_health_check=list(HealthCheck))

HIT_WORDS = re.compile(r"(?i)(?:[0-9a-f]{16}\.md|note)\S*.*\bline\s+\d+")
SHARED_KINDS = ("prompts", "guard_denials", "post_check_catches", "guard_escalations", "auto_mode_denials", "headless_denials",
                "user_rejects", "isolation_refusals")


def shared_records(checkout: Path) -> List[dict]:
    root = checkout / "analytics" / "runs"
    return [json.loads(p.read_text()) for p in sorted(root.rglob("*.json"))] if root.exists() else []


def export_all(env):
    checkout, _bare = kiro_checkout(env.root)
    out = stats("export", "--to", str(checkout), "--yes", env=env, cwd=checkout)
    return checkout, out


# --------------------------------------------------------------------------- P-22

PLANTED_VALUES = [
    "acme-ledger-internal",
    "tests/test_payroll_acme.py",
    "quarterly payroll reconciliation for acme",
    "grep -rn acme-ledger-internal src/payroll",
    "tests/test acme payroll.py",
    "assert payroll_total(acme_ledger) == 41_250",
]
CLEAN_NOTE = "The implementer used sed on the tests and the resolver needed 3 rounds"


@settings(**PROP)
@given(value=st.sampled_from(PLANTED_VALUES), field=st.sampled_from(["command", "path", "reason"]),
       case=st.sampled_from(["same", "upper", "title"]), clean=st.booleans())
def test_p22_a_planted_guard_log_value_is_refused_in_a_note_and_absent_from_the_record(value, field, case, clean):
    """P-22: a guard-log command, path or reason that reaches a note is a leak hit; ck vocabulary words never are."""
    with fresh_env() as env:
        proj = git_project(env.root)
        home = FakeClaudeHome(env.claude)
        sess = home.session(cwd=proj)
        runs = [sess.implement_run(start=f"2026-10-{12 + i}T09:00:00Z", args={"spec": "demo", "root": str(proj)},
                                   agents=[AgentSpec("task 1")]) for i in range(2)]
        event = {"ts_ms": to_ms("2026-10-12T09:03:00Z"), "sessionId": sess.session_id, "decision": "deny", "category": "test_delete",
                 "command": "sed -i s/a/b/ tests/test_x.py", "path": "tests/test_x.py", "reason": "[ck guard] Denied: deletes a test"}
        event[field] = value
        write_guard_log(env, [event])

        checkout, _bare = kiro_checkout(env.root)
        bin_dir = Path(env.root) / "bin"
        bin_dir.mkdir()
        run_env = {**env, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        assert stats("collect", "--run", runs[0].run_id, env=run_env, cwd=proj).exit_code == 0
        first = stats("export", "--to", str(checkout), "--yes", env=run_env, cwd=checkout)
        assert first.exit_code == 0, first.output
        assert stats("collect", "--run", runs[1].run_id, env=run_env, cwd=proj).exit_code == 0
        said = CLEAN_NOTE if clean else {"same": value, "upper": value.upper(), "title": value.title()}[case]
        fake_claude_drafter(bin_dir, f"Resolver needed 3 rounds on: {said}")
        code, text = run_tty([*CK, "stats", "export", "--to", str(checkout), "--note"], env=run_env, cwd=checkout, script=[(r"(?i)accept", "a\n")])
        assert code == 0, text
        notes = sorted((checkout / "analytics").rglob("*.md"))
        if clean:
            assert not HIT_WORDS.search(text), text
            assert len(notes) == 1, text
        else:
            assert HIT_WORDS.search(text), text
            assert notes == [], text
        data = all_bytes(checkout / "analytics" / "runs").lower()
        assert value.lower().encode() not in data
        for record in shared_records(checkout):
            assert "guardLog" not in json.dumps(record)


# --------------------------------------------------------------------------- P-27


@settings(**PROP)
@given(modes=st.lists(st.sampled_from(["old", "null", "new"]), min_size=1, max_size=3))
def test_p27_records_without_interruption_data_show_not_recorded_and_are_not_shared_or_counted(modes):
    """P-27: no interruption data -> `interruptions: not recorded`, no shared block, and no aggregate adds zeros."""
    kinds = ["guard_denial", "auto", "reject"]
    with fresh_env() as env:
        proj = git_project(env.root)
        home = FakeClaudeHome(env.claude)
        sess = home.session(cwd=proj)
        run_ids = []
        for i, mode in enumerate(modes):
            start = f"2026-10-{12 + i}T09:00:00Z"
            run = sess.implement_run(start=start, args={"spec": "demo", "root": str(proj)}, agents=[AgentSpec("task 1")])
            add_interruptions(sess, [(kinds[i], "orchestrator", "Bash", "ls -la", to_ms(start) + 120_000, None)])
            run_ids.append(run.run_id)
        assert stats("collect", "--all", env=env, cwd=proj).exit_code == 0

        for rid, mode in zip(run_ids, modes):
            path = env.xdg / "claude-kiro" / "runs" / f"{rid}.json"
            rec = json.loads(path.read_text())
            if mode == "old":
                rec.pop("interruptions", None)
            elif mode == "null":
                rec["interruptions"] = None
            path.write_text(json.dumps(rec))

        for rid, mode in zip(run_ids, modes):
            shown = stats("show", rid, env=env, cwd=proj)
            assert shown.exit_code == 0, shown.output
            lines = [ln.strip() for ln in shown.stdout.splitlines() if ln.strip().startswith("interruptions:")]
            assert len(lines) == 1, shown.stdout
            assert (lines[0] == "interruptions: not recorded") == (mode != "new"), lines

        checkout, out = export_all(env)
        assert out.exit_code == 0, out.output
        shared = shared_records(checkout)
        assert len(shared) == len(modes)
        assert sum("interruptions" in rec for rec in shared) == modes.count("new")

        listing = stats("show", "--json", env=env, cwd=proj)
        assert listing.exit_code == 0, listing.output
        data = json.loads(listing.stdout)
        for entry in data["runs"]:
            mode = modes[run_ids.index(entry["runId"])]
            if mode != "new":
                assert entry.get("interruptions") is None, entry
        if modes.count("new") == 0:
            assert '"prompts": 0' not in listing.stdout and '"guardDenials": 0' not in listing.stdout


# --------------------------------------------------------------------------- P-29

PLANTED_TOKENS = ("acme-ledger-internal", "test_payroll_acme", "zephyr-payroll", "acme-ledger-core", "orion-billing", "/home/dana", "ledger.py")
PLANTED_COMMANDS = [
    "sed -i s/x/y/ tests/test_payroll_acme.py",
    "acme-ledger-internal --dump /home/dana/acme/ledger.py",
    "zephyr-payroll run",
    "git push origin zephyr-payroll",
    "python3 -c \"open('/home/dana/ledger.py','w')\"",
    "echo orion-billing > acme-ledger-core.txt",
]
SPEC_NAMES = ["zephyr-payroll", "acme-ledger-core", "orion-billing"]


@settings(**PROP)
@given(drawn=interruption_strategy(), rot=st.integers(0, 5), spec=st.sampled_from(SPEC_NAMES))
def test_p29_the_shared_interruptions_object_validates_and_holds_only_numbers_and_enum_keys(drawn, rot, spec):
    """P-29: with planted command texts, paths and spec names, the shared record validates and holds no planted string."""
    from claude_kiro.analytics import share

    events, _expected = drawn
    planted = []
    for i, e in enumerate(events):
        if e[0] == "malformed":
            planted.append(e)
        else:
            text = PLANTED_COMMANDS[(i + rot) % len(PLANTED_COMMANDS)]
            planted.append((e[0], e[1], e[2], text if e[2] == "Bash" else f"{text.split()[-1]}", *e[4:]))
    with fresh_env() as env:
        proj = git_project(env.root)
        home = FakeClaudeHome(env.claude)
        sess = home.session(cwd=proj)
        run = sess.implement_run(start="2026-10-12T09:00:00Z", duration_ms=20_000_000, args={"spec": spec, "root": str(proj)},
                                 agents=[AgentSpec("task 1"), AgentSpec("review 1", model="claude-opus-5-5")])
        by = {"implementer": [], "reviewer": [], "orchestrator": []}
        for e in planted:
            by[e[1]].append(e)
        add_interruptions((run, 0), by["implementer"])
        add_interruptions((run, 1), by["reviewer"])
        add_interruptions(sess, by["orchestrator"])
        assert stats("collect", "--all", env=env, cwd=proj).exit_code == 0
        checkout, out = export_all(env)
        assert out.exit_code == 0, out.output
        records = shared_records(checkout)
        assert len(records) == 1
        record = records[0]
        assert record["schema"] == "ck-run-analytics/1"
        assert share.validate_shareable(record) == []
        block = record["interruptions"]
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
        data = all_bytes(checkout / "analytics").lower()
        for token in PLANTED_TOKENS:
            assert token.encode() not in data, token
