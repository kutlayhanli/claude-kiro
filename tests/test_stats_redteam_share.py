"""Red-team of the shareable-record exclusions (Story 7: 7.3, 7.4, 7.5).

Written from requirements.md and the design's public interfaces only. Each case plants a hostile value in a local
run (a confidential string in an enum slot, a custom model id, a version holding text, ...), drives the real
`ck stats collect` and `ck stats export --to <kiro checkout>` as subprocesses (7.1) and scans every byte written
under the checkout's `analytics/` area: no planted string may appear, and every enumerated field must hold a
member of its fixed set or `other`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from stats_helpers import (
    CONFIDENTIAL,
    AgentSpec,
    TaskSpec,
    all_bytes,
    fresh_env,
    git_project,
    implement_result,
    create_result,
    kiro_checkout,
    stats,
)

MODEL_ALIASES = {"haiku", "sonnet", "opus", "fable", "inherit", "other"}
EFFORTS = {"low", "medium", "high", "xhigh", "max", "inherit", "other"}
WORKFLOWS = {"spec-implement", "spec-create", "spec-plan", "other"}
VARIANTS = {"default", "iso", "other"}
OUTCOMES = {"complete", "blocked", "halted", "refused", "ready", "ready_after_decisions", "needs_work", "failed", "other"}
STACKS = {"python", "node", "go", "rust", "java", "ruby", "dotnet", "other"}
CONFIG_ROLES = {"implementer", "test_writer", "reviewer", "reviewer_risky", "fixer", "orchestrator", "resolver"}
ROLES = {"implementer", "test-writer", "test_writer", "reviewer", "fixer", "resolver", "plan", "land", "gate", "cross-test",
         "cross_test", "report", "other"}
VERDICTS = {"approve", "changes", "unreviewed", "other"}
COST_SOURCES = {"priced_from_transcripts", "estimate"}
MISSING = {"result", "runfile", "agent_entries", "transcripts", "meta", "timing", "ck_version"}
PHASES = {"plan", "wave", "tasks", "final_gate", "report", "requirements", "design_test_plan", "task_tracks", "link",
          "adversarial_review", "revise", "consistency_check", "other"}
ISSUE_CODES = {
    "gate_red_after_merge", "landing_red", "landing_bisected", "resolver_rounds_exceeded", "conflict_not_resolved",
    "review_blocking_unresolved", "review_contract_mismatch", "cross_test_blocking", "planning_failed", "plan_cycle",
    "retry_no_commits", "escalated_to_opus", "escalated", "red_on_landing_fixed", "worktree_busy", "unreviewed_merge",
    "report_from_agents", "permission_refusal", "run_halted", "run_blocked", "review_needs_work", "review_lenses_lost",
    "consistency_conflicts", "review_escalated", "unknown_roles", "transcripts_missing",
}
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([.+-][0-9A-Za-z.]+)?$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

SECRET = "ZEPHYR-PAYROLL-SECRET"  # a marker no seeded term shares; every case plants it or a CONFIDENTIAL string


# --------------------------------------------------------------------------- driver


class Exported:
    def __init__(self, checkout: Path, bare: Path, env: dict, records: List[Tuple[Path, dict]], exit_code: int, output: str):
        self.checkout, self.bare, self.env = checkout, bare, env
        self.records, self.exit_code, self.output = records, exit_code, output

    @property
    def analytics(self) -> Path:
        return self.checkout / "analytics"

    def blob(self) -> str:
        return all_bytes(self.analytics).decode("utf-8", errors="replace")


def _read_records(checkout: Path) -> List[Tuple[Path, dict]]:
    root = checkout / "analytics" / "runs"
    out = []
    for p in sorted(root.rglob("*.json")) if root.exists() else []:
        out.append((p, json.loads(p.read_text())))
    return out


def run_pipeline(
    tmp: Path,
    env: dict,
    *,
    result: Any = None,
    agents: Optional[List[AgentSpec]] = None,
    cc_version: str = "2.1.80",
    manifest_version: Optional[str] = None,
    project_files: Optional[Dict[str, str]] = None,
    extra_keys: Optional[dict] = None,
    start: str = "2026-10-12T09:00:00Z",
    kind: str = "implement",
    args: Optional[dict] = None,
) -> Exported:
    """Fake home with one hostile run -> `ck stats collect --all` -> `ck stats export --to <checkout> --yes`."""
    from stats_helpers import FakeClaudeHome  # the harness is test-owned, not a ck module

    home = FakeClaudeHome(Path(env["CLAUDE_CONFIG_DIR"]))
    proj = git_project(tmp, "git@github.com:acme-corp/acme-ledger.git", name="acme-ledger")
    for rel, text in (project_files or {}).items():
        f = proj / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    if manifest_version is not None:
        m = proj / ".claude" / "ck-manifest.json"
        m.parent.mkdir(parents=True, exist_ok=True)
        m.write_text(json.dumps({"version": manifest_version}))
    session = home.session("-home-dana-acme-ledger", cwd=proj, git_branch="integrate/zephyr-payroll", cc_version=cc_version)
    kw: Dict[str, Any] = {"start": start}
    if agents is not None:
        kw["agents"] = agents
    if extra_keys is not None:
        kw["extra_keys"] = extra_keys
    if kind == "implement":
        kw["args"] = args or {"spec": "zephyr-payroll", "root": str(proj)}
        kw["result"] = result if result is not None else implement_result([TaskSpec(task="1"), TaskSpec(task="2", wave=2)])
        session.implement_run("wf_redteam1", **kw)
    else:
        kw["args"] = args or {"feature": "zephyr-payroll", "root": str(proj)}
        kw["result"] = result if result is not None else create_result(feature="zephyr-payroll")
        session.create_run("wf_redteam1", **kw)
    collected = stats("collect", "--all", env=env, cwd=proj)
    assert collected.exit_code == 0, collected.output
    checkout, bare = kiro_checkout(tmp)
    exported = stats("export", "--to", str(checkout), "--yes", env=env, cwd=proj)
    return Exported(checkout, bare, env, _read_records(checkout), exported.exit_code, exported.output)


def folded(text: str) -> str:
    return text.replace("\x00", "").casefold()


def assert_no_leak(ex: Exported, planted: List[str]) -> None:
    blob = folded(ex.blob())
    for term in [*planted, *CONFIDENTIAL, "wf_redteam1", ex.env["CLAUDE_CONFIG_DIR"]]:
        assert folded(term) not in blob, f"planted string {term!r} reached the shareable files"
    assert not re.search(r"\d{4}-\d{2}-\d{2}", ex.blob()), "an exact date reached the shareable files"
    assert not re.search(r"(?<![\d.])\d{10,13}(?![\d.])", ex.blob()), "an epoch timestamp reached the shareable files"


def _enum(path: str, value: Any, allowed: set) -> None:
    assert isinstance(value, str) and value in allowed, f"{path} = {value!r} is not in {sorted(allowed)}"


def assert_enums(rec: dict) -> None:
    _enum("workflow", rec.get("workflow"), WORKFLOWS)
    if "variant" in rec:
        _enum("variant", rec["variant"], VARIANTS)
    _enum("outcome", rec.get("outcome"), OUTCOMES)
    assert MONTH_RE.match(str(rec.get("month"))), rec.get("month")
    assert re.fullmatch(r"[0-9a-f]{16}", str(rec.get("id", rec.get("record_id", "")))), rec
    if "stack" in rec:
        _enum("stack", rec["stack"], STACKS)
    for key in ("ck_version", "claude_code_version"):
        v = rec.get(key)
        if v is not None:
            assert v == "other" or VERSION_RE.match(v), f"{key} = {v!r}"
    for role, cfg in (rec.get("config") or {}).items():
        _enum(f"config.{role}", role, CONFIG_ROLES)
        _enum(f"config.{role}.model", cfg.get("model"), MODEL_ALIASES)
        _enum(f"config.{role}.effort", cfg.get("effort"), EFFORTS)
    for role in rec.get("agents_by_role") or {}:
        _enum("agents_by_role key", role, ROLES)
    for v in rec.get("verdicts") or {}:
        _enum("verdicts key", v, VERDICTS)
    tokens = rec.get("tokens") or {}
    for m in tokens.get("by_model") or {}:
        _enum("tokens.by_model key", m, MODEL_ALIASES)
    for r in tokens.get("by_role") or {}:
        _enum("tokens.by_role key", r, ROLES)
    cost = rec.get("cost") or {}
    for m in cost.get("by_model") or {}:
        _enum("cost.by_model key", m, MODEL_ALIASES)
    for r in cost.get("by_role") or {}:
        _enum("cost.by_role key", r, ROLES)
    if "source" in cost:
        _enum("cost.source", cost["source"], COST_SOURCES)
    for code in rec.get("issues") or []:
        _enum("issues[]", code, ISSUE_CODES)
    for item in rec.get("missing") or []:
        _enum("missing[]", item, MISSING)
    durations = rec.get("durations") or {}
    for ph in durations.get("phases") or []:
        _enum("durations.phases[].phase", ph.get("phase"), PHASES)
    for r in durations.get("by_role") or {}:
        _enum("durations.by_role key", r, ROLES)
    for t in rec.get("tasks") or []:
        if "verdict" in t:
            _enum("tasks[].verdict", t["verdict"], VERDICTS)
        assert not isinstance(t.get("index"), str), "task index must be anonymous"


def check(ex: Exported, planted: List[str]) -> None:
    assert ex.exit_code == 0, ex.output
    assert len(ex.records) == 1, f"expected one shareable record, got {len(ex.records)}\n{ex.output}"
    assert_no_leak(ex, planted)
    for _, rec in ex.records:
        assert_enums(rec)


def _with(result: dict, mutate: Callable[[dict], None]) -> dict:
    mutate(result)
    return result


def _impl(mutate: Callable[[dict], None], tasks: Optional[List[TaskSpec]] = None) -> dict:
    return _with(implement_result(tasks or [TaskSpec(task="1"), TaskSpec(task="2", wave=2)]), mutate)


# --------------------------------------------------------------------------- 7.4: enum values outside their set

OUTCOME_VALUES = {
    "confidential-string": "zephyr-payroll",
    "client-name-title-case": "Reconcile Globex invoices",
    "complete-plus-space": "complete ",
    "complete-plus-newline": "complete\n",
    "complete-with-nul": "comp\x00lete",
    "complete-uppercase": "COMPLETE",
    "turkish-dotted-i": "İncomplete",
    "sharp-s-fold": "straße-complete",
    "complete-with-suffix-secret": "complete: acme-corp",
    "number": 7,
    "float": 1.5,
    "list-with-secret": ["complete", "acme-corp"],
    "dict-nested-text": {"outcome": "complete", "note": "Initech SSN column"},
    "bool": True,
    "empty-string": "",
}


@pytest.mark.parametrize("value", list(OUTCOME_VALUES.values()), ids=list(OUTCOME_VALUES))
def test_outcome_outside_set_is_not_copied(tmp_path: Path, value: Any) -> None:
    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, result=_impl(lambda r: r.update(outcome=value)))
        check(ex, ["Initech SSN column", "acme-corp", "Reconcile Globex invoices", "straße-complete", "İncomplete"])


@pytest.mark.parametrize("value", ["Ready ", "Ready after decisions ", "READY", "Ready\x00", "Needs work: Globex", "ßready", "ready_after_decisions; acme-corp", {"r": "Globex"}, 3],
                         ids=["ready-space", "rad-space", "upper", "nul", "suffix-secret", "sharp-s", "suffix-acme", "dict", "number"])
def test_create_review_result_outside_set_is_not_copied(tmp_path: Path, value: Any) -> None:
    result = create_result(feature="zephyr-payroll")
    result["review"]["result"] = value
    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, kind="create", result=result)
        check(ex, ["Globex", "acme-corp", "ßready"])


VERDICT_VALUES = {
    "confidential": "Initech SSN column",
    "approve-plus-space": "approve ",
    "approve-upper": "APPROVE",
    "approve-nul": "app\x00rove",
    "turkish-i": "İnitech-approve",
    "number": 42,
    "list": ["approve", "Initech"],
    "dict-nested-text": {"verdict": "approve", "text": "Reconcile Globex invoices"},
    "null": None,
}


@pytest.mark.parametrize("value", list(VERDICT_VALUES.values()), ids=list(VERDICT_VALUES))
def test_review_verdict_outside_set_is_not_copied(tmp_path: Path, value: Any) -> None:
    def mutate(r: dict) -> None:
        r["tasks"][0]["review"]["verdict"] = value

    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, result=_impl(mutate))
        check(ex, ["Initech", "Globex", "İnitech-approve"])


@pytest.mark.parametrize("value", ["zephyr-payroll", "normal ", "CRITICAL", "İ-risk", "acme-corp\x00", 9, ["x", "acme-corp"], {"a": "acme-corp"}],
                         ids=["confidential", "normal-space", "upper", "turkish-i", "nul-secret", "number", "list", "dict"])
def test_risk_tag_outside_set_is_not_copied(tmp_path: Path, value: Any) -> None:
    def mutate(r: dict) -> None:
        r["tasks"][0]["riskTag"] = value
        r["tasks"][0]["review"]["riskTag"] = value

    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, result=_impl(mutate))
        check(ex, ["acme-corp", "İ-risk"])


@pytest.mark.parametrize("value", ["zephyr-payroll", "high ", "HIGH", "hıgh", "max\x00acme-corp", 5, ["high"], {"effort": "acme-corp"}],
                         ids=["confidential", "high-space", "upper", "dotless-i", "nul", "number", "list", "dict"])
def test_effort_outside_set_is_other(tmp_path: Path, value: Any) -> None:
    def mutate(r: dict) -> None:
        r["agents"]["implementer"] = f"sonnet/{value}" if isinstance(value, str) else value
        r["taskAgents"] = {"model": "sonnet", "effort": value}

    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, result=_impl(mutate))
        check(ex, ["acme-corp", "hıgh"])


@pytest.mark.parametrize("value", ["python-acme-corp", "Python", "python ", "pythоn", "rust\x00", "ßtack", 3, ["python"], {"x": "acme-corp"}],
                         ids=["suffix", "capitalised", "space", "cyrillic-o", "nul", "sharp-s", "number", "list", "dict"])
def test_stack_hint_outside_set_is_other(tmp_path: Path, value: Any) -> None:
    # the run file carries a hostile stack hint in an unknown key; the collector must never promote it
    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, extra_keys={"stack": value, "args2": {"stack": value}})
        check(ex, ["acme-corp", "ßtack", "pythоn"])


@pytest.mark.parametrize("fname", ["acme-corp.toml", "zephyr-payroll.gemspec", "Initech SSN column.csproj", "acme-ledger.pyproject", "acme-corp/Cargo.toml.bak",
                                  "go.mod.acme-corp", "package.json.zephyr-payroll"],
                         ids=["toml", "gemspec", "csproj-with-spaces", "pyproject-lookalike", "dir-named-after-client", "gomod-lookalike", "package-lookalike"])
def test_stack_hint_derived_from_client_named_file(tmp_path: Path, fname: str) -> None:
    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, project_files={fname: "x = 1\n"})
        check(ex, ["acme-corp", "zephyr-payroll", "Initech SSN column", "acme-ledger"])


def test_stack_hint_from_known_file_is_enum_member(tmp_path: Path) -> None:
    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, project_files={"pyproject.toml": '[project]\nname = "acme-ledger"\n', "package.json": '{"name": "acme-corp"}'})
        check(ex, ["acme-corp", "acme-ledger"])


# --------------------------------------------------------------------------- 7.5: model identifiers

MODEL_IDS = {
    "custom-suffix-acme-corp": "claude-opus-5-5-acme-corp",
    "slash-family": "opus/acme-ledger",
    "bracket-secret": "claude-sonnet-5-5[zephyr-payroll]",
    "bedrock-arn": "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-opus-5-5-v1:0",
    "bedrock-prefixed": "us.anthropic.claude-opus-5-5-v1:0",
    "vertex-at": "claude-opus-5-5@20261001",
    "provider-slash": "anthropic/claude-opus-5-5",
    "finetune": "my-finetune-acme-corp",
    "family-in-middle": "acme-corp-opus-ledger",
    "uppercase-secret": "CLAUDE-OPUS-5-5-ACME-CORP",
    "trailing-space": "claude-opus-5-5 acme-corp",
    "nul": "claude-opus-5-5\x00acme-corp",
    "turkish-dotted-i": "claude-opus-5-5-İnitech",
    "sharp-s": "claude-opus-5-5-straße",
    "empty": "",
}

#: ids the requirement calls custom or provider-specific: they must be exactly `other`
STRICTLY_OTHER = {"slash-family", "bracket-secret", "bedrock-arn", "bedrock-prefixed", "vertex-at", "provider-slash", "finetune",
                  "family-in-middle", "trailing-space", "nul", "turkish-dotted-i", "sharp-s"}


def _model_case(tmp_path: Path, model_id: str) -> Exported:
    def mutate(r: dict) -> None:
        r["agents"]["implementer"] = f"{model_id}/medium"
        r["agents"]["reviewer"] = f"{model_id}/high"
        r["taskAgents"] = {"model": model_id, "effort": "medium"}
        r["tasks"][0]["startModel"] = model_id
        r["tasks"][0]["reviewModel"] = model_id
        r["tasks"][0]["review"]["model"] = model_id
        r["tasks"][0]["escalations"] = [{"model": model_id, "reason": "retry"}]
        r["fixerEscalations"] = [{"what": "x", "model": model_id, "green": True}]

    agents = [AgentSpec("task 1", model=model_id or "unknown"), AgentSpec("review 1", model=model_id or "unknown")]
    with fresh_env() as env:
        return run_pipeline(tmp_path, env, result=_impl(mutate), agents=agents)


@pytest.mark.parametrize("name", list(MODEL_IDS))
def test_custom_model_id_is_not_copied(tmp_path: Path, name: str) -> None:
    ex = _model_case(tmp_path, MODEL_IDS[name])
    planted = ["acme-corp", "acme-ledger", "zephyr-payroll", "İnitech", "straße", "123456789012", "20261001", "finetune"]
    check(ex, planted)
    if name in STRICTLY_OTHER:
        for _, rec in ex.records:
            for m in (rec.get("tokens") or {}).get("by_model") or {}:
                assert m == "other", f"{name}: model key {m!r} should be 'other'"
            for role, cfg in (rec.get("config") or {}).items():
                assert cfg["model"] == "other", f"{name}: config.{role}.model = {cfg['model']!r}"


@pytest.mark.parametrize("model_id,alias", [("claude-sonnet-5-5", "sonnet"), ("claude-opus-5-5", "opus"), ("claude-haiku-5-5", "haiku"), ("opus", "opus"),
                                           ("sonnet[1m]", "sonnet")],
                         ids=["sonnet", "opus", "haiku", "bare-alias", "one-m"])
def test_known_model_ids_still_map_to_alias(tmp_path: Path, model_id: str, alias: str) -> None:
    ex = _model_case(tmp_path, model_id)
    check(ex, [])
    for _, rec in ex.records:
        assert set((rec.get("tokens") or {}).get("by_model") or {}) == {alias}


# --------------------------------------------------------------------------- 7.3: versions and free text

VERSIONS = {
    "text-suffix": "2.1.80 acme-corp",
    "text-prefix": "acme-corp 2.1.80",
    "dash-secret": "2.1.80-zephyr-payroll",
    "paren": "2.1.80 (Initech SSN column)",
    "newline": "2.1.80\nacme-corp",
    "nul": "2.1.80\x00acme-corp",
    "only-secret": "acme-corp",
    "unicode-digits": "２.１.８０",
    "v-prefixed": "v2.1.80",
}


@pytest.mark.parametrize("version", list(VERSIONS.values()), ids=list(VERSIONS))
def test_claude_code_version_holding_text(tmp_path: Path, version: str) -> None:
    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, cc_version=version)
        check(ex, ["acme-corp", "zephyr-payroll", "Initech SSN column"])


@pytest.mark.parametrize("version", list(VERSIONS.values()), ids=list(VERSIONS))
def test_ck_version_holding_text(tmp_path: Path, version: str) -> None:
    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, manifest_version=version)
        check(ex, ["acme-corp", "zephyr-payroll", "Initech SSN column"])


def test_task_id_that_is_a_title(tmp_path: Path) -> None:
    tasks = [TaskSpec(task="Reconcile Globex invoices", title="Initech SSN column"), TaskSpec(task="../../etc/acme-corp", wave=2, title="x")]
    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, result=implement_result(tasks))
        check(ex, ["Reconcile Globex invoices", "Initech SSN column", "acme-corp"])
        for _, rec in ex.records:
            assert all(isinstance(t.get("index"), int) for t in rec.get("tasks") or [])


def test_agent_labels_and_phases_never_copied(tmp_path: Path) -> None:
    agents = [
        AgentSpec("task Reconcile Globex invoices", phase="Wave acme-corp"),
        AgentSpec("review: zephyr-payroll", phase="Initech SSN column", model="claude-opus-5-5"),
        AgentSpec("land acme-ledger/src/ledger_globex.py", phase="Wave 1", model="claude-haiku-5-5"),
    ]
    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, agents=agents)
        check(ex, ["Reconcile Globex invoices", "Initech SSN column", "acme-corp", "ledger_globex"])


def test_unknown_result_keys_holding_secrets(tmp_path: Path) -> None:
    def mutate(r: dict) -> None:
        r["secretNotes"] = "Initech SSN column in /srv/acme/ledger.py"
        r["tasks"][0]["customer"] = "Reconcile Globex invoices"
        r["tasks"][0]["review"]["extra"] = {"deep": ["dana.scully@acme-corp.com"]}
        r["landings"][0]["branch"] = "integrate/zephyr-payroll"
        r["timing"]["phases"][0]["group"] = "Wave acme-corp"
        r["finalGate"]["summary"] = "Dana Scully broke it"
        r["stats"]["note"] = "acme-ledger"

    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, result=_impl(mutate), extra_keys={"owner": "Dana Scully", "logs": ["zephyr-payroll"]})
        check(ex, ["Initech SSN column", "/srv/acme/ledger.py", "Reconcile Globex invoices", "dana.scully@acme-corp.com", "integrate/zephyr-payroll",
                   "Wave acme-corp", "Dana Scully", "acme-ledger", "zephyr-payroll"])


def test_failure_reason_text_only_becomes_issue_codes(tmp_path: Path) -> None:
    tasks = [TaskSpec(task="1", ok=False, reason="merge failed: conflict not resolved in /srv/acme/ledger.py for acme-corp"),
             TaskSpec(task="2", ok=False, reason="permission denied: /home/dana/acme-ledger/src/ledger.py")]
    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, result=implement_result(tasks, outcome="blocked"))
        check(ex, ["/srv/acme/ledger.py", "acme-corp", "/home/dana/acme-ledger/src/ledger.py"])


def test_no_issues_note_in_non_interactive_export(tmp_path: Path) -> None:
    tasks = [TaskSpec(task="1", ok=False, reason="merge failed: conflict not resolved")]
    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, result=implement_result(tasks, outcome="blocked"))
        check(ex, [])
        assert not list(ex.analytics.rglob("*.md")), "a free-text note was written without an interactive approval"


# --------------------------------------------------------------------------- timestamps near boundaries


@pytest.mark.parametrize("start", [
    "2026-09-30T23:59:59Z", "2026-10-01T00:00:00Z", "2026-12-31T23:59:59.999Z", "2027-01-01T00:00:00+00:00",
    "2026-10-31T23:59:59-08:00", "2026-02-29T00:00:00Z",
], ids=["month-end", "month-start", "year-end-ms", "year-start", "negative-offset", "invalid-leap-day"])
def test_exact_dates_never_reach_the_record(tmp_path: Path, start: str) -> None:
    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, start=start)
        if ex.exit_code == 0:
            check(ex, [start])
        else:
            assert not ex.records, "a failed export must write nothing"


@pytest.mark.parametrize("start_time", [1759276799, 1759276799000, 1759276800, 1759276800000, 253402300800, -1, 0, 1.7592768e12],
                         ids=["sec-month-end", "ms-month-end", "sec-month-start", "ms-month-start", "year-10000", "negative", "zero", "float-ms"])
def test_epoch_start_time_in_seconds_or_ms(tmp_path: Path, start_time: Any) -> None:
    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, extra_keys={"startTime": start_time})
        if ex.exit_code == 0 and ex.records:
            check(ex, [str(start_time)])
        else:
            assert not ex.records, "a failed export must write nothing"


# --------------------------------------------------------------------------- the system's own output fed back


def test_exported_record_fed_back_as_local_record(tmp_path: Path) -> None:
    with fresh_env() as env:
        first = run_pipeline(tmp_path, env)
        check(first, [])
        runs_dir = Path(env["XDG_DATA_HOME"]) / "claude-kiro" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        for i, (_, rec) in enumerate(first.records):
            planted = dict(rec)
            planted["stack"] = "acme-corp"
            planted["outcome"] = "Initech SSN column"
            planted["workflow"] = "zephyr-payroll"
            (runs_dir / f"wf_fedback{i}.json").write_text(json.dumps(planted))
        (tmp_path / "second").mkdir()
        checkout2, _ = kiro_checkout(tmp_path / "second")
        res = stats("export", "--to", str(checkout2), "--yes", env=env)
        ex2 = Exported(checkout2, checkout2, env, _read_records(checkout2), res.exit_code, res.output)
        assert_no_leak(ex2, ["acme-corp", "Initech SSN column", "zephyr-payroll"])
        for _, rec in ex2.records:
            assert_enums(rec)


def test_export_twice_never_duplicates_or_widens(tmp_path: Path) -> None:
    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env)
        check(ex, [])
        again = stats("export", "--to", str(ex.checkout), "--yes", env=env)
        assert again.exit_code == 0, again.output
        records = _read_records(ex.checkout)
        assert len(records) == 1
        assert_no_leak(Exported(ex.checkout, ex.bare, env, records, 0, ""), [])


def test_planted_strings_in_every_slot_at_once(tmp_path: Path) -> None:
    """Kitchen sink: confidential text in every enum slot and every unknown key of one run."""
    c = list(CONFIDENTIAL)

    def mutate(r: dict) -> None:
        r["outcome"] = c[0]
        r["agents"] = {k: f"{c[i]}/{c[i + 1]}" for i, k in enumerate(r["agents"])}
        r["taskAgents"] = {"model": c[2], "effort": c[3]}
        r["into"] = c[6]
        for i, t in enumerate(r["tasks"]):
            t["task"] = c[i + 1]
            t["title"] = c[i + 2]
            t["riskTag"] = c[i + 3]
            t["startModel"] = c[i + 4]
            t["review"]["verdict"] = c[i + 5]
            t["review"]["model"] = c[i + 6]
        r["reportSource"] = c[7]

    agents = [AgentSpec(f"task {c[3]}", model=c[4], phase=c[5]), AgentSpec(f"review {c[6]}", model=c[7], phase=c[8])]
    with fresh_env() as env:
        ex = run_pipeline(tmp_path, env, result=_impl(mutate), agents=agents, cc_version=c[1], manifest_version=c[2],
                          extra_keys={"stack": c[9], "owner": c[11]})
        check(ex, [])
