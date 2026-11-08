"""Evidence checks for the four paid equity reruns (Stories 9 and 20).

TC-165 validates the committed bench configs (`bench-equity-reruns.json`) and runs the real
`protected_changes` on a synthetic change. TC-166, TC-167 and TC-89 validate the evidence the
reruns leave in `specs/unattended-correctness/reruns.md` (its fenced json block). These tests
validate committed files; they never generate them. Before the reruns exist the evidence file is
missing and they fail for that reason.

`validate_evidence(doc, dsh_findings=None) -> list[str]` is the validator. Task 102's red team
imports it and applies it to synthetic documents. It returns one problem string per failed bar,
each naming the criterion and the run ID (20.7); an empty list means the bar is met.

Evidence conventions (design.md, `reruns.md`):
- approvals: `[{"task": 93, "kind": "ck worktree approve"}, ...]`, task an int (not a bool, float
  or string), only Tasks 93 and 94, only through the approval command.
- finalGate: `{"state": "green", "afterLastMerge": true}`.
- hooksAtStart: true; audit: "clean" (or true, or an object with `clean: true` or `state: "clean"`).
- failedCriteria: `[]` when the bar is met.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC_DIR = ROOT / "specs" / "unattended-correctness"
RERUNS_MD = SPEC_DIR / "reruns.md"
BENCH_CONFIG = SPEC_DIR / "bench-equity-reruns.json"
DSH_VERIFICATION = SPEC_DIR / "dsh-verification.json"

SPEC_REF = "fix/mtr-spec-issues"

# The oracle for requirement 20.1: the 12 reviewed slash-command paths, and no others.
ALLOWED_COMMANDS = {
    ".claude/commands/digest.md",
    ".claude/commands/express.md",
    ".claude/commands/grade.md",
    ".claude/commands/inbox.md",
    ".claude/commands/ingest.md",
    ".claude/commands/roles.md",
    ".claude/commands/spotcheck.md",
    ".claude/commands/sweep.md",
    ".claude/commands/thesis/close.md",
    ".claude/commands/thesis/new.md",
    ".claude/commands/thesis/review.md",
    ".claude/commands/thesis/revise.md",
}

APPROVAL_KIND = "ck worktree approve"
APPROVED_TASKS = (93, 94)
GREEN_STATES = ("green", "passed", "pass")
OPENCODE_MODELS = ("opencode-go/muse-spark-1.3-contributor", "opencode-go/deepseek-v4.1-flash")

PROPERTIES: Dict[str, str] = {}


# --- the validator (imported by tests/test_unattended_e2e_evidence_redteam.py) -------------------


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _audit_clean(audit: Any) -> bool:
    if audit is True or audit == "clean":
        return True
    if isinstance(audit, dict):
        return audit.get("clean") is True or audit.get("state") == "clean"
    return False


def _validate_arm(arm: Any, baseline: Dict[str, Any], index: int) -> List[str]:
    if not isinstance(arm, dict):
        return [f"arm #{index}: not an object (criterion 20.2, run ID unknown)"]
    run = arm.get("run")
    run_label = run if isinstance(run, str) and run else "<missing run ID>"
    tag = f"arm {arm.get('name')!r} run {run_label}"
    out: List[str] = []
    failed: List[str] = []  # criteria that failed, to check against failedCriteria

    def bad(criterion: str, msg: str) -> None:
        out.append(f"{tag}: criterion {criterion}: {msg}")
        failed.append(criterion)

    if not isinstance(run, str) or not run:
        bad("2.13", "run ID missing or not a string")
    for key in ("name", "harness", "model"):
        if not isinstance(arm.get(key), str) or not arm.get(key):
            bad("20.2", f"{key} missing or not a string")

    passes = arm.get("passes")
    if not isinstance(passes, list) or not passes:
        bad("2.13", "passes missing or empty")
    else:
        for i, p in enumerate(passes):
            if not isinstance(p, dict) or not isinstance(p.get("progressRun"), str) or not p.get("progressRun"):
                bad("2.13", f"pass #{i} has no progressRun")
            elif p.get("runId") != run or not isinstance(p.get("runId"), str):
                bad("2.13", f"pass #{i} started with runId {p.get('runId')!r}, not the arm's run ID")

    done, total = arm.get("tasksDone"), arm.get("tasksTotal")
    if not (_is_int(done) and _is_int(total) and total > 0 and done == total):
        bad("20.2", f"tasks Done/total is {done!r}/{total!r}, every task must be Done")

    gate = arm.get("finalGate")
    if not (isinstance(gate, dict) and gate.get("state") in GREEN_STATES and gate.get("afterLastMerge") is True):
        bad("20.2", f"final attributed gate is {gate!r}, must be green and run after the last merge of the last pass")

    suite = arm.get("suite")
    if not (isinstance(suite, dict) and _is_int(suite.get("passed")) and suite["passed"] > 0
            and suite.get("failed") == 0 and suite.get("errors") == 0
            and _is_int(suite.get("failed")) and _is_int(suite.get("errors"))):
        bad("20.2", f"full suite is {suite!r}, must have passed > 0 and failed + errors = 0")

    hand = arm.get("handRepairs")
    if not (isinstance(hand, list) and hand == []):
        bad("20.3", f"handRepairs is {hand!r}, must be an empty list")

    approvals = arm.get("approvals")
    if not isinstance(approvals, list):
        bad("20.3", f"approvals is {approvals!r}, must be a list")
    else:
        for a in approvals:
            if not (isinstance(a, dict) and _is_int(a.get("task")) and a.get("task") in APPROVED_TASKS
                    and a.get("kind") == APPROVAL_KIND):
                bad("20.3", f"approval {a!r} is not an approval of Task 93 or 94 through `{APPROVAL_KIND}`")

    if arm.get("hooksAtStart") is not True:
        bad("9.10", f"hooksAtStart is {arm.get('hooksAtStart')!r}, hooks must be installed at run start")
    if not _audit_clean(arm.get("audit")):
        bad("9.10", f"audit is {arm.get('audit')!r}, must be clean at run end")

    for key, label in (("costPerMergedTask", "cost per merged task"), ("wallMinPerTask", "wall minutes per task")):
        v, b = arm.get(key), baseline.get(key)
        if not _is_num(v) or v < 0:
            bad("20.5", f"{label} is {v!r}, not a number")
        elif _is_num(b) and v > b:
            bad("20.5", f"{label} {v} is above the baseline {b}")

    fc = arm.get("failedCriteria")
    if not isinstance(fc, list):
        out.append(f"{tag}: criterion 20.7: failedCriteria is {fc!r}, must be a list")
    else:
        for entry in fc:
            ok = isinstance(entry, dict) and entry.get("criterion") and entry.get("runId") == run
            out.append(f"{tag}: criterion 20.7: recorded failure {entry!r}"
                       + ("" if ok else " (names no criterion or no run ID)"))
        if failed and not fc:
            out.append(f"{tag}: criterion 20.7: failed bars {sorted(set(failed))} are not recorded in failedCriteria")
    return out


def validate_evidence(doc: Any, dsh_findings: Optional[Dict[str, Any]] = None) -> List[str]:
    """Problems with the rerun evidence document; each names its criterion and the run ID."""
    if not isinstance(doc, dict):
        return ["evidence: not an object (criterion 20.2)"]
    out: List[str] = []

    baseline = doc.get("baseline")
    if not (isinstance(baseline, dict) and _is_num(baseline.get("costPerMergedTask"))
            and _is_num(baseline.get("wallMinPerTask"))):
        out.append("baseline: costPerMergedTask and wallMinPerTask must be numbers (criterion 20.5)")
        baseline = {}

    arms = doc.get("arms")
    if not isinstance(arms, list):
        return out + ["arms: not a list (criterion 20.2)"]
    names = [a.get("name") for a in arms if isinstance(a, dict)]
    runs = [a.get("run") for a in arms if isinstance(a, dict)]
    if len(set(map(str, names))) != len(names):
        out.append("arms: duplicate arm names (criterion 20.2)")
    if len(set(map(str, runs))) != len(runs):
        out.append("arms: two arms share a run ID (criterion 20.2, 2.13)")
    if len(arms) != 4:
        findings_ok = (
            len(arms) == 3
            and isinstance(dsh_findings, dict)
            and not (dsh_findings.get("canHostAgent") is True and dsh_findings.get("canHostCk") is True)
            and not any(isinstance(a, dict) and a.get("harness") == "dsh" for a in arms)
        )
        if not findings_ok:
            out.append(f"arms: {len(arms)} arms, need 4 (or 3 when the dsh findings of 18.3 apply) (criterion 20.2, 20.6)")
    for i, arm in enumerate(arms):
        out.extend(_validate_arm(arm, baseline, i))

    tac = doc.get("thinAgentContext")
    if not isinstance(tac, dict):
        out.append("thinAgentContext: missing (criterion NFR thin-agent context)")
    else:
        lean, default, runs_ = tac.get("leanCacheWrite"), tac.get("defaultCacheWrite"), tac.get("runs")
        if not (_is_num(lean) and _is_num(default) and default > 0 and lean >= 0):
            out.append(f"thinAgentContext: cache-write figures {lean!r}/{default!r} are not numbers (criterion NFR thin-agent context)")
        elif lean > 0.5 * default:
            out.append(f"thinAgentContext: lean cache write {lean} is not at least 50% below default {default} (criterion NFR thin-agent context)")
        if not isinstance(runs_, list) or not runs_:
            out.append("thinAgentContext: runs missing (criterion NFR thin-agent context)")

    sandbox = doc.get("sandboxTests")
    if not isinstance(sandbox, dict):
        out.append("sandboxTests: missing (criterion 9.11)")
    else:
        for key in ("tc88", "tc189"):
            if sandbox.get(key) != "passed":
                out.append(f"sandboxTests.{key} is {sandbox.get(key)!r}, must be 'passed' not skipped (criterion 9.11)")
    return out


# --- helpers -------------------------------------------------------------------------------------


def _evidence() -> Dict[str, Any]:
    assert RERUNS_MD.is_file(), f"{RERUNS_MD} does not exist: the equity reruns (Task 104) have not been recorded"
    m = re.search(r"```json\s*\n(.*?)\n```", RERUNS_MD.read_text(encoding="utf-8"), re.S)
    assert m, f"{RERUNS_MD} has no fenced ```json block"
    return json.loads(m.group(1))


def _dsh_findings() -> Optional[Dict[str, Any]]:
    return json.loads(DSH_VERIFICATION.read_text()) if DSH_VERIFICATION.is_file() else None


def _bench() -> Dict[str, Any]:
    assert BENCH_CONFIG.is_file(), f"{BENCH_CONFIG} does not exist: the rerun bench configs (Task 103) are not written"
    return json.loads(BENCH_CONFIG.read_text())


def _is_opencode(conf: Dict[str, Any]) -> bool:
    return conf.get("harness") == "opencode" or str(conf.get("model", "")).startswith("opencode-go/")


def _allow_paths(cfg: Dict[str, Any], conf: Dict[str, Any]) -> List[str]:
    ck_config = conf.get("ck_config", cfg.get("ck_config"))
    assert isinstance(ck_config, dict), f"config {conf.get('name')!r} has no ck_config"
    land = ck_config.get("land")
    assert isinstance(land, dict) and isinstance(land.get("allow_paths"), list), \
        f"config {conf.get('name')!r} has no ck_config.land.allow_paths"
    return land["allow_paths"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


# --- TC-165: bench configs (20.1, 20.4) ----------------------------------------------------------


def test_tc165_configs_use_the_fix_spec_ref():
    cfg = _bench()
    refs = [cfg.get("base")] + [s.get("ref") or s.get("base") or s.get("branch")
                                for s in cfg.get("specs", []) if isinstance(s, dict)]
    assert SPEC_REF in refs, f"no spec ref {SPEC_REF!r} in the bench config (found {refs})"
    for conf in cfg["configs"]:
        for key in ("base", "ref"):
            if key in conf:
                assert conf[key] == SPEC_REF, f"config {conf.get('name')!r} {key} is {conf[key]!r}"


def test_tc165_allow_paths_are_exactly_the_twelve_commands():
    cfg = _bench()
    assert len(cfg["configs"]) in (3, 4)
    for conf in cfg["configs"]:
        allow = _allow_paths(cfg, conf)
        assert len(allow) == len(set(allow)), f"config {conf.get('name')!r} lists a path twice"
        assert set(allow) == ALLOWED_COMMANDS, (
            f"config {conf.get('name')!r}: extra {sorted(set(allow) - ALLOWED_COMMANDS)}, "
            f"missing {sorted(ALLOWED_COMMANDS - set(allow))}"
        )


def test_tc165_claude_md_stays_protected_with_that_allow_list(tmp_path):
    from claude_kiro.worktrees import protected_changes

    cfg = _bench()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "task")
    (root / ".claude" / "commands" / "thesis").mkdir(parents=True)
    for p in sorted(ALLOWED_COMMANDS):
        (root / p).write_text("new command\n")
    (root / ".claude" / "CLAUDE.md").write_text("changed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "task")
    for conf in cfg["configs"]:
        allow = _allow_paths(cfg, conf)
        assert protected_changes(root, "main", "task", allow) == [".claude/CLAUDE.md"], conf.get("name")
    # a change to only the twelve commands is not protected under the same allow-list
    _git(root, "checkout", "-q", "main")
    _git(root, "checkout", "-q", "-b", "only-commands")
    (root / ".claude" / "commands" / "thesis").mkdir(parents=True)
    for p in sorted(ALLOWED_COMMANDS):
        (root / p).write_text("new command\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "commands")
    for conf in cfg["configs"]:
        assert protected_changes(root, "main", "only-commands", _allow_paths(cfg, conf)) == []


def test_tc165_opencode_arms_have_one_model_and_no_roles():
    cfg = _bench()
    opencode = [c for c in cfg["configs"] if _is_opencode(c)]
    assert len(opencode) == 2, f"expected 2 OpenCode arms, found {len(opencode)}"
    for conf in opencode:
        assert not conf.get("roles"), f"OpenCode arm {conf.get('name')!r} has a roles map (20.4)"
        assert "roles" not in (conf.get("opencode") or {}), f"OpenCode arm {conf.get('name')!r} has opencode.roles"
    for model in OPENCODE_MODELS:
        assert any(model in json.dumps(c) for c in opencode), f"no OpenCode arm uses {model}"


# --- TC-166 / TC-167 / TC-89: the committed evidence ---------------------------------------------


def test_tc166_the_evidence_file_meets_the_success_bar():
    problems = validate_evidence(_evidence(), _dsh_findings())
    assert problems == [], "\n".join(problems)


def test_tc166_evidence_arm_shapes_match_the_bench_configs():
    doc, cfg = _evidence(), _bench()
    assert len(doc["arms"]) == len(cfg["configs"])
    assert {a["harness"] for a in doc["arms"]} == {c.get("harness") or ("opencode" if _is_opencode(c) else "claude")
                                                   for c in cfg["configs"]}


def test_tc167_lean_thin_agent_cache_writes_are_half_of_default():
    tac = _evidence()["thinAgentContext"]
    assert isinstance(tac["runs"], list) and tac["runs"], "thinAgentContext.runs must name the paired runs"
    assert tac["defaultCacheWrite"] > 0
    assert tac["leanCacheWrite"] <= 0.5 * tac["defaultCacheWrite"], tac


def test_tc89_hooks_installed_at_start_and_audit_clean_at_end():
    for arm in _evidence()["arms"]:
        assert arm["hooksAtStart"] is True, f"run {arm['run']}: hooks not installed at run start (9.10)"
        assert _audit_clean(arm["audit"]), f"run {arm['run']}: run-end audit not clean (9.10)"


def test_tc89_sandbox_tests_passed_not_skipped():
    sandbox = _evidence()["sandboxTests"]
    assert sandbox["tc88"] == "passed", f"TC-88 is {sandbox['tc88']!r} (9.11)"
    assert sandbox["tc189"] == "passed", f"TC-189 is {sandbox['tc189']!r} (9.11)"


@pytest.mark.parametrize("path", [RERUNS_MD, BENCH_CONFIG])
def test_tc166_evidence_files_are_committed(path):
    assert path.is_file(), f"{path} is not committed"
