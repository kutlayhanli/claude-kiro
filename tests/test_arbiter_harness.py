"""Self-checks of the run-principles fake runtime additions (Task 2): `rulings`, `auditFindings`, `arbiterEdits`,
`arbiterStepFailures`, `ledgerFail`, the `arbiter-` thin steps, `throwingDate`, the new create answers and labels, and
that the existing node suites still pass with the extended fake. Every check runs the real fake project, bridge or
workflow script under node; a missing label is named in the failure, never a broken harness."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from landing_bridge import bridge  # noqa: F401  (module-scoped fixture)

JS = Path(__file__).parent / "js"
pytestmark = [pytest.mark.usefixtures("no_network"), pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")]

# A stand-in `ck` that echoes its argv and stdin as JSON, so the thin steps' command line and stdin are observable.
ECHO = "import sys, json; print(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}))"
ECHO_CK = {"cmd": [sys.executable, "-c", ECHO], "env": {}}


def node(script: str, check: bool = True):
    """Run an ES-module snippet in tests/js; return the parsed JSON it prints (or the process when check is False)."""
    proc = subprocess.run(["node", "--input-type=module", "-e", script], cwd=JS, capture_output=True, text=True, timeout=120)
    if not check:
        return proc
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def with_project(options: dict, body: str):
    """Build a fake project with `options` (JSON) and run `body` (async JS using p); print the JSON of `out`."""
    return node(
        "import { project } from './fake_project.mjs'\n"
        f"const p = project({{ deps: {{ '1': [], '2': ['1'] }}, ...{json.dumps(options)} }})\n"
        "const out = {}\n" + body + "\nconsole.log(JSON.stringify(out))"
    )


def test_rulings_answer_in_order_and_the_last_repeats():
    out = with_project(
        {"rulings": {"1": [{"status": "ruled", "outcome": "code"}, None, {"status": "ruled", "outcome": "spec_edit"}]}},
        "out.a = await p.agent('p', { label: 'arbiter task 1' })\n"
        "out.b = await p.agent('p', { label: 'arbiter task 1 risky' })\n"
        "out.c = await p.agent('p', { label: 'arbiter task 1' })\n"
        "out.d = await p.agent('p', { label: 'arbiter task 1' })",
    )
    assert out["a"]["outcome"] == "code"
    assert out["b"] is None
    assert out["c"]["outcome"] == "spec_edit" and out["d"]["outcome"] == "spec_edit"


def test_ruling_throw_and_an_unscripted_task_name_the_label():
    out = with_project(
        {"rulings": {"1": ["throw"]}},
        "try { await p.agent('p', { label: 'arbiter task 1' }) } catch (e) { out.thrown = e.message }\n"
        "try { await p.agent('p', { label: 'arbiter task 2' }) } catch (e) { out.unscripted = e.message }",
    )
    assert "arbiter agent failed" in out["thrown"]
    assert 'no ruling scripted for "arbiter task 2"' in out["unscripted"]


def test_audit_findings_default_to_no_problems():
    out = with_project(
        {"auditFindings": {"2": {"problems": [{"file": "tests/t.py", "test": "test_x", "criterion": "1.1", "reason": "r", "needs": "n"}], "summary": "one"}}},
        "out.none = await p.agent('p', { label: 'audit task 1' })\nout.some = await p.agent('p', { label: 'audit task 2' })",
    )
    assert out["none"]["problems"] == []
    assert out["some"]["problems"][0]["test"] == "test_x"


def test_a_review_entry_can_carry_a_serves_no_requirement_finding():
    out = with_project(
        {"reviews": {"1": [{"verdict": "changes", "finding": "serves-no-requirement"}]}},
        "out.r = await p.agent('review 1', { label: 'review 1' })",
    )
    assert out["r"]["verdict"] == "changes"
    assert out["r"]["findings"][0]["noRequirement"] is True


def test_an_arbiter_step_without_execck_throws_and_never_skips_the_real_cli():
    prompt = "Run:\nck -C /repo arbiter open demo --task 1 --into integrate/demo --json"
    for opts in ({"label": "arbiter-open"}, {"label": "arbiter-open", "agentType": "ck-runner"}):
        proc = node(
            "import { project } from './fake_project.mjs'\n"
            "const p = project({ deps: { '1': [] } })\n"
            f"await p.agent({json.dumps(prompt)}, {json.dumps(opts)})",
            check=False,
        )
        assert proc.returncode != 0
        assert "arbiter-open" in proc.stderr and "execCk" in proc.stderr


def test_an_arbiter_step_runs_its_one_ck_line_with_the_fenced_json_block_as_stdin():
    prompt = (
        "Run this one command and print its output:\n"
        "ck -C /repo arbiter open demo --task 1 --into integrate/demo --json\n"
        'with this on stdin:\n```json\n{"claim": {"task": "1"}, "task": {"risk": "normal"}}\n```\n'
    )
    out = node(
        "import { project } from './fake_project.mjs'\n"
        f"const p = project({{ deps: {{ '1': [] }}, execCk: {json.dumps(ECHO_CK)} }})\n"
        f"const r = await p.agent({json.dumps(prompt)}, {{ label: 'arbiter-open', agentType: 'ck-runner' }})\n"
        "console.log(JSON.stringify({ r, results: p.state.execResults.length }))"
    )
    assert out["results"] == 1
    assert out["r"]["exitCode"] == 0
    assert out["r"]["json"]["argv"] == ["-C", "/repo", "arbiter", "open", "demo", "--task", "1", "--into", "integrate/demo", "--json"]
    assert json.loads(out["r"]["json"]["stdin"]) == {"claim": {"task": "1"}, "task": {"risk": "normal"}}


# The thin-step prompt exactly as spec-implement.js's thinCk writes it: its shellWord and its prompt template are read
# from the script, so the fake follows the producer, not a hand-made prompt.
THINCK_PROMPT = r"""
import fs from 'node:fs'
const src = fs.readFileSync('../../plugin/workflows/spec-implement.js', 'utf8')
const shellWord = src.match(/^const shellWord = .*$/m)[0]
const start = src.indexOf('  const cmd = `ck ${argv.map(shellWord)')
const end = src.indexOf('  const opts = () =>', start)
if (start < 0 || end < 0) throw new Error('thinCk template not found in spec-implement.js')
const body = src.slice(start, end)
export const thinPrompt = new Function('argv', 'o', 'GIT_NOTE', 'GATE_NOTE', `${shellWord}\n${body}\nreturn prompt`)
"""


def thin_prompt(argv: list, stdin: str | None, cwd: str) -> str:
    out = node(
        THINCK_PROMPT.replace("export const", "const")
        + f"console.log(JSON.stringify(thinPrompt({json.dumps(argv)}, {{ stdin: {json.dumps(stdin)}, cwd: {json.dumps(cwd)}, what: 'Arbiter step' }}, 'Use git as you were told.', '')))"
    )
    return out


@pytest.mark.parametrize(
    "checkout",
    ["/tmp/demo-task-arbiter1", "/tmp/my demo/x-task-arbiter1", "/tmp/it's/x-task-arbiter1"],
)
def test_an_arbiter_step_reads_the_argv_and_heredoc_stdin_thinck_writes(checkout):
    argv = ["-C", checkout, "arbiter", "open", "demo", "--task", "1", "--into", "integrate/demo", "--json"]
    stdin = json.dumps({"claim": {"task": "1", "text": "the test's assert contradicts 1.1"}, "task": {"risk": "normal"}})
    prompt = thin_prompt(argv, stdin, checkout)
    assert "<<'CK_PARK'" in prompt  # the real producer's here-document shape
    out = node(
        "import { project, arbiterArgv, arbiterStdin } from './fake_project.mjs'\n"
        f"const p = project({{ deps: {{ '1': [] }}, execCk: {json.dumps(ECHO_CK)} }})\n"
        f"const prompt = {json.dumps(prompt)}\n"
        "const r = await p.agent(prompt, { label: 'arbiter-open', agentType: 'spec:runner' })\n"
        "console.log(JSON.stringify({ r, argv: arbiterArgv(prompt), stdin: arbiterStdin(prompt) }))"
    )
    assert out["argv"] == argv
    assert out["stdin"] == stdin
    assert out["r"]["exitCode"] == 0
    assert out["r"]["json"]["argv"] == argv  # the real command got exactly thinCk's words
    assert json.loads(out["r"]["json"]["stdin"]) == json.loads(stdin)


def test_an_arbiter_step_without_stdin_reads_the_bare_thinck_line():
    argv = ["-C", "/repo", "arbiter", "settle", "demo", "1|gate failure", "--tier", "risky", "--json"]
    prompt = thin_prompt(argv, None, "/repo")
    out = node(
        "import { arbiterArgv, arbiterStdin } from './fake_project.mjs'\n"
        f"console.log(JSON.stringify({{ argv: arbiterArgv({json.dumps(prompt)}), stdin: arbiterStdin({json.dumps(prompt)}) }}))"
    )
    assert out == {"argv": argv, "stdin": None}


def test_a_failed_record_step_reports_exit_1_after_the_real_command_ran():
    record = {"trigger": "gate-failure", "claim_key": "1|gate", "claim_text": "t", "task": "1", "tier": "normal", "outcome": "code", "reasoning": "r", "principle_id": "P1"}
    prompt = f"ck -C /repo arbiter record demo --into integrate/demo --json\n```json\n{json.dumps(record)}\n```"
    out = node(
        "import { project } from './fake_project.mjs'\n"
        f"const p = project({{ deps: {{ '1': [] }}, arbiterStepFailures: {{ record: true }}, execCk: {json.dumps(ECHO_CK)} }})\n"
        f"const rec = await p.agent({json.dumps(prompt)}, {{ label: 'arbiter-record' }})\n"
        f"const open = await p.agent({json.dumps(prompt.replace('record', 'open'))}, {{ label: 'arbiter-open' }})\n"
        "console.log(JSON.stringify({ rec, open, ran: p.state.execResults.map(r => r.argv[3]) }))"
    )
    assert out["rec"]["exitCode"] == 1 and "error" in out["rec"]["json"]
    assert out["open"]["exitCode"] == 0  # only the named step fails
    assert out["ran"] == ["record", "open"]  # the real command ran before the failure was reported


def test_ledger_fail_answers_exit_1_with_an_error():
    schema = {"type": "object", "properties": {"exitCode": {}, "json": {}}}
    out = with_project(
        {"ledgerFail": True},
        f"out.r = await p.agent('ck testfix check demo --test tests/t.py::test_x', {{ label: 'testfix ledger 1', schema: {json.dumps(schema)} }})",
    )
    assert out["r"]["exitCode"] == 1
    assert out["r"]["json"]["error"]


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def test_arbiter_edits_commit_with_real_git_in_the_checkout_the_prompt_names(tmp_path):
    repo = tmp_path / "demo-task-arbiter1"
    repo.mkdir()
    git("init", "-q", "-b", "work", cwd=repo)
    (repo / "seed.txt").write_text("seed")
    git("add", "seed.txt", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "seed", cwd=repo)
    before = git("rev-parse", "HEAD", cwd=repo).strip()
    edits = {"1": {"files": {"specs/demo/tasks.md": "# changed\n", "specs/demo/design.md": "design\n"}}}
    ruling = {"status": "ruled", "outcome": "spec_edit", "reasoning": "r", "principle": "P1"}
    out = with_project(
        {"rulings": {"1": [ruling]}, "arbiterEdits": edits},
        f"out.r = await p.agent('Work only in {repo} and rule.', {{ label: 'arbiter task 1' }})",
    )
    assert out["r"]["outcome"] == "spec_edit"
    after = git("rev-parse", "HEAD", cwd=repo).strip()
    assert after != before
    assert sorted(git("show", "--name-only", "--format=", "HEAD", cwd=repo).split()) == ["specs/demo/design.md", "specs/demo/tasks.md"]
    assert (repo / "specs/demo/tasks.md").read_text() == "# changed\n"
    assert git("status", "--short", cwd=repo) == ""


def test_arbiter_edits_without_a_named_checkout_fail_loudly():
    proc = node(
        "import { project } from './fake_project.mjs'\n"
        "const p = project({ deps: { '1': [] }, rulings: { 1: [{ status: 'ruled' }] }, arbiterEdits: { 1: { files: { 'a.md': 'x' } } } })\n"
        "await p.agent('no checkout here', { label: 'arbiter task 1' })",
        check=False,
    )
    assert proc.returncode != 0 and "names no arbiter checkout" in proc.stderr


def test_the_bridge_call_op_reaches_a_decisions_block_function(bridge):  # noqa: F811
    assert bridge.call("readyTasks", {"order": ["1", "2"], "deps": {"1": [], "2": ["1"]}, "done": [], "exclude": [], "blocked": []}) == ["1"]
    with pytest.raises(Exception, match="noSuchDecision"):
        bridge.call("noSuchDecision")


def test_throwing_date_makes_date_and_math_random_throw():
    out = node(
        "import { runWorkflow, throwingGlobals } from './load_workflow.mjs'\n"
        "import fs from 'node:fs'\nimport os from 'node:os'\nimport path from 'node:path'\n"
        "const f = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tw-')), 's.js')\n"
        "fs.writeFileSync(f, `const r = {}\n"
        "for (const [k, fn] of Object.entries({ now: () => Date.now(), made: () => new Date(), random: () => Math.random() })) { try { fn(); r[k] = 'ok' } catch (e) { r[k] = 'threw' } }\n"
        "r.fixed = new Date('2026-01-01T00:00:00Z').getTime(); r.floor = Math.floor(2.5)\nreturn r`)\n"
        "const { result } = await runWorkflow(f, { agent: async () => null, args: {}, ...throwingGlobals() })\n"
        "console.log(JSON.stringify(result))"
    )
    assert out["now"] == out["made"] == out["random"] == "threw"
    assert out["fixed"] == 1767225600000 and out["floor"] == 2  # parsing a stamp and other Math stay usable


SCENARIO = {"project": {"deps": {"1": [], "2": ["1"]}}, "args": {"agents": {}}}


def test_run_with_throwing_date_completes_because_the_script_reads_no_clock(bridge):  # noqa: F811
    plain = bridge.run(**SCENARIO, clock="virtual")
    thrown = bridge.run(**SCENARIO, clock="virtual", throwing_date=True)
    # the run result's `merged` list (timing fields such as `overhead.share` read the clock, which throws here)
    assert "merged" in plain["result"]
    assert plain["result"]["merged"]
    assert thrown["result"]["merged"] == plain["result"]["merged"]


def test_create_with_throwing_date_completes(bridge):  # noqa: F811
    out = bridge.create(throwing_date=True)
    assert out["result"] is not None


def test_create_stub_answers_the_new_arbiter_labels_and_overrides_win():
    out = node(
        "import { stub } from './fake_create.mjs'\n"
        "const s = stub({ 'arbiter drift': { rulings: [{ claim: 'D1' }] } })\n"
        "const out = {}\n"
        "for (const l of ['arbiter refuted', 'arbiter cuts', 'arbiter cuts risky', 'arbiter drift', 'apply-cuts', 'review: should-exist']) out[l] = await s.agent('p', { label: l })\n"
        "console.log(JSON.stringify(out))"
    )
    assert isinstance(out["arbiter refuted"], dict) and isinstance(out["arbiter cuts risky"], dict)
    assert out["arbiter drift"]["rulings"][0]["claim"] == "D1"
    assert out["review: should-exist"] == {"findings": []}
    assert "applied" in out["apply-cuts"]


def test_emit_labels_has_scenarios_for_the_new_paths():
    # The labels themselves must come from the real scripts (TC-137, Task 45); here the harness only
    # proves its scenarios exist: args that reach the arbiter/auditor paths and answers for their labels.
    src = (JS / "emit_labels.mjs").read_text()
    for needle in ["arbiterAgent", "auditorAgent", "runnerAgent", "'arbiter task'", "'audit task'", "'arbiter-'", "'apply-cuts'"]:
        assert needle in src, needle
    # agent type names, not flags (design.md "Workflow args"); a claim trigger, a due audit and the create subjects
    for needle in ["'spec:arbiter'", "'spec:auditor'", "'spec:runner'", "verifiedBy", "failUntilRuled", "guardUntilRuled",
                   "conflictUntilRuled", "needs_stronger_look", "'rerun'", "problems: []", "mode: 'apply-cuts'", "'review: should-exist'"]:
        assert needle in src, needle
    assert "arbiterAgent: true" not in src and "auditorAgent: true" not in src and "runnerAgent: true" not in src
    assert "labelCorpus.push" not in src
    proc = subprocess.run(["node", str(JS / "emit_labels.mjs")], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    labels = {c["label"] for c in json.loads(proc.stdout) if c["workflow"] == "spec-implement"}
    # the implement scenarios really run (thin steps get the CK_CALL envelope), so they reach the task, landing and report steps
    assert {"task 1", "land 1", "report", "run start"} <= labels, sorted(labels)


def test_emit_labels_answers_match_the_design_schemas():
    # emit_labels imports fake_project.mjs's helpers; its answers for the new labels follow design.md's RULING, AUDIT,
    # OpenResult and SettleResult, the same shapes fake_project.mjs uses.
    src = (JS / "emit_labels.mjs").read_text()
    assert "{ rulings: [] }" not in src.split("function createAgent")[0]  # a per-task ruling is one RULING
    assert "findings: [], summary: 'clean'" not in src  # AUDIT has problems, not findings
    for field in ["action: 'rule'", "tier: 'normal'", "planLayer:", "earlier: []", "missing: null", "route", "record:", "problems: []", "number:", "commit:"]:
        assert field in src, field


SUITES = [p.name for p in sorted(JS.glob("*.test.mjs")) if "spec_create" not in p.name and "spec_review_frozen" not in p.name]


@pytest.mark.parametrize("script", SUITES)
def test_existing_node_suites_still_pass_with_the_extended_fake(script):
    proc = subprocess.run(["node", str(JS / script)], capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "FAIL" not in proc.stdout
