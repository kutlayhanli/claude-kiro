"""The leak check blocks export and submit (TC-59..TC-62), through the real `ck stats` CLI.

Real git with a local bare remote, a fake `gh` on PATH, a fake `claude` drafter for notes, a real pty for the
interactive note flow. Every scanned artifact is read from git or the gh log, never from ck internals.
ck code is imported only inside test bodies (the ISSUE_CODES check).
"""

import json
import os
import re
from pathlib import Path
from typing import List, Tuple

import pytest

from stats_helpers import (
    CK, CONFIDENTIAL, SEEDED_RUN_IDS, AgentSpec, FakeClaudeHome, TaskSpec, all_bytes, ck_sub, create_result, fake_claude_drafter,
    fake_gh, gh_calls, git, git_project, implement_result, kiro_checkout, run_tty, seeded_home,
)

pytestmark = pytest.mark.usefixtures("no_network")

REPO_ROOT = Path(__file__).resolve().parent.parent
# The per-hit line the design specifies: the file (`<record_id>.md`, or a note pseudo-path) and `line N`.
HIT_WORDS = re.compile(r"(?i)(?:[0-9a-f]{16}\.md|note)\S*.*\bline\s+\d+")
BATCH_BRANCH = re.compile(r"^ck-stats-batch-[0-9a-f]{8}$")


class World:
    def __init__(self, tmp: Path, home: FakeClaudeHome):
        self.tmp = Path(tmp)
        self.home = home
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.gh_log = self.tmp / "gh.jsonl"
        fake_gh(self.bin, self.gh_log)
        self.checkout, self.bare = kiro_checkout(self.tmp)
        # Each world gets its own local store: two worlds in one test reuse run ids, and the export ledger is keyed
        # by run id, so a shared store would make the second world's export find nothing new.
        self.env = {"CLAUDE_CONFIG_DIR": str(home.root), "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                    "XDG_DATA_HOME": str(self.tmp / "xdg-data")}

    def ck(self, *args: str):
        return ck_sub(*args, env=self.env, cwd=self.checkout)

    def collect(self, *args: str):
        out = self.ck("stats", "collect", *args)
        assert out.exit_code == 0, out.output
        return out

    def export(self, *args: str):
        return self.ck("stats", "export", "--to", str(self.checkout), *args)

    def submit(self, *args: str):
        return self.ck("stats", "submit", "--to", str(self.checkout), *args)

    def export_tty(self, *args: str, script=()):
        return run_tty([*CK, "stats", "export", "--to", str(self.checkout), *args], env=self.env, cwd=self.checkout,
                       script=list(script))

    def exported_json(self) -> List[Path]:
        return sorted((self.checkout / "analytics" / "runs").rglob("*.json")) if (self.checkout / "analytics").exists() else []

    def exported_notes(self) -> List[Path]:
        return sorted((self.checkout / "analytics").rglob("*.md")) if (self.checkout / "analytics").exists() else []

    def porcelain(self) -> str:
        return git(self.checkout, "status", "--porcelain", "--untracked-files=all")

    def bare_branches(self) -> List[str]:
        return git(self.bare, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()

    def pushed_batches(self) -> List[str]:
        return [b for b in self.bare_branches() if b != "master"]

    def local_branches(self) -> List[str]:
        return git(self.checkout, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()


# --------------------------------------------------------------------------- worlds


def _rich_tasks() -> List[TaskSpec]:
    return [
        TaskSpec(task="1", ok=False, attempts=2, reason="merge failed: still conflicting after 3 rounds",
                 outside_files="src/claude_kiro/analytics/export.py transcripts/x.py permission/y.py"),
        TaskSpec(task="2", wave=2, ok=False, reason="merge failed: conflict not resolved"),
        TaskSpec(task="3", wave=2, escalations=[{"model": "opus"}], unreviewed=True),
        TaskSpec(task="4", wave=3, ok=False, attempts=2, reason="merge failed: no commits"),
    ]


def _segment_world(tmp: Path, segs_a: List[str], spec_a: str = "ui", spec_b: str = "review", rich: bool = True):
    """Project A (spec `spec_a`, root path segments `segs_a`) and project B (spec `spec_b`) -> (home, a_run_id, b_run_id)."""
    home = FakeClaudeHome(Path(tmp) / "claude-home")
    root_a = git_project(tmp, "git@github.com:acme-corp/acme-ledger.git", name="/".join(["a", *segs_a, "app"]))
    root_b = git_project(tmp, "https://gitlab.example.com/orion/billing", name="b/app")
    sa = home.session("-proj-a", "5e55a10e-0000-4000-8000-0000000000c1", cwd=root_a)
    ra = sa.implement_run("wf_seg-a-1", args={"spec": spec_a, "root": str(root_a)},
                          result=implement_result(_rich_tasks() if rich else [TaskSpec(task="1")],
                                                  full_gates=[{"afterMerges": 2, "green": False}] if rich else None,
                                                  into=f"integrate/{spec_a}"))
    sb = home.session("-proj-b", "5e55a10e-0000-4000-8000-0000000000c2", cwd=root_b)
    rb = sb.implement_run("wf_seg-b-1", start="2026-10-13T10:00:00Z", args={"spec": spec_b, "root": str(root_b)},
                          result=implement_result([TaskSpec(task="1")], into=f"integrate/{spec_b}"))
    return home, ra.run_id, rb.run_id


def _export_b_then_note_a(world: World, a_run: str, b_run: str, note: str) -> Tuple[int, str]:
    """Export B (non-interactive), then export A with `--note` under a pty whose fake drafter writes `note`.

    Returns (exit_code, transcript). A answers `accept` at the first (and only) prompt.
    """
    world.collect("--run", b_run)
    first = world.export("--yes")
    assert first.exit_code == 0, first.output
    world.collect("--run", a_run)
    fake_claude_drafter(world.bin, note)
    return world.export_tty("--note", script=[(r"(?i)accept", "a\n")])


# --------------------------------------------------------------------------- TC-59

NOTE_CASES = [
    "orion-billing",
    "Dana Scully",
    "dana.scully@acme-corp.com",
    "sk-ant-api03-AbCdEf0123456789xyzXYZ",
    "/home/dana/x",
    "123e4567-e89b-12d3-a456-426614174000",
    "2026-10-12T09:13:27Z",
]


@pytest.mark.parametrize("hit", NOTE_CASES)
def test_a_note_naming_confidential_data_is_a_hit_and_excluded(tmp_path, hit):
    home, _terms = seeded_home(tmp_path)
    world = World(tmp_path, home)
    if hit in ("Dana Scully", "dana.scully@acme-corp.com"):
        # the hit must come from the git identity of the checkout the design names, not from a path segment
        git(world.checkout, "config", "user.name", "Dana Scully")
        git(world.checkout, "config", "user.email", "dana.scully@acme-corp.com")
    note = f"Resolver needed 3 rounds on {hit} lockfile"
    world.collect("--run", SEEDED_RUN_IDS[2])
    assert world.export("--yes").exit_code == 0
    world.collect("--run", SEEDED_RUN_IDS[0])
    fake_claude_drafter(world.bin, note)
    code, text = world.export_tty("--note", script=[(r"(?i)accept", "a\n")])
    assert code == 0, text
    assert HIT_WORDS.search(text), text
    assert world.exported_notes() == [], text
    assert len(world.exported_json()) == 2
    assert hit.encode() not in all_bytes(world.checkout / "analytics")


def test_a_clean_note_is_included_under_the_same_flow(tmp_path):
    home, _terms = seeded_home(tmp_path)
    world = World(tmp_path, home)
    world.collect("--run", SEEDED_RUN_IDS[2])
    assert world.export("--yes").exit_code == 0
    world.collect("--run", SEEDED_RUN_IDS[0])
    fake_claude_drafter(world.bin, "Resolver needed 3 rounds on a lockfile")
    code, text = world.export_tty("--note", script=[(r"(?i)accept", "a\n")])
    assert code == 0, text
    assert len(world.exported_notes()) == 1, text
    assert len(world.exported_json()) == 2


# --------------------------------------------------------------------------- TC-60


def test_machine_label_that_is_a_confidential_term_blocks_export_and_submit(tmp_path):
    home, _terms = seeded_home(tmp_path)
    world = World(tmp_path, home)
    world.collect("--all")
    set_label = world.ck("agents", "set", "run.machine_label", "zephyr-payroll")
    assert set_label.exit_code == 0, set_label.output
    head = git(world.checkout, "rev-parse", "HEAD")
    commits = git(world.checkout, "rev-list", "--all", "--count")
    bare_refs = git(world.bare, "for-each-ref")

    out = world.export("--yes")
    assert out.exit_code != 0, out.output
    assert re.search(r"\.json: machine\b", out.output), out.output
    assert world.exported_json() == [] and world.exported_notes() == []
    assert world.porcelain() == ""

    sub = world.submit("--yes")
    assert sub.exit_code != 0, sub.output
    assert re.search(r"\.json: machine\b", sub.output), sub.output
    assert world.local_branches() == ["master"]
    assert world.pushed_batches() == []
    assert git(world.bare, "for-each-ref") == bare_refs
    assert git(world.checkout, "rev-parse", "HEAD") == head
    assert git(world.checkout, "rev-list", "--all", "--count") == commits
    assert world.porcelain() == ""
    assert gh_calls(world.gh_log) == []


# --------------------------------------------------------------------------- TC-61


def test_whole_word_rule_normal_export_has_no_hit_but_a_note_naming_the_spec_does(tmp_path):
    home, _, _ = _segment_world(tmp_path / "w1", ["analytics", "transcripts", "permission", "1", "80", "2026", "cafe"])
    world = World(tmp_path / "w1", home)
    world.collect("--all")
    out = world.export("--yes")
    assert out.exit_code == 0, out.output
    assert len(world.exported_json()) == 2
    assert world.exported_notes() == []
    assert any("2026-10" in str(p) for p in world.exported_json())

    home2, a2, b2 = _segment_world(tmp_path / "w2", ["analytics", "transcripts", "permission"])
    world2 = World(tmp_path / "w2", home2)
    code, text = _export_b_then_note_a(world2, a2, b2, "the ui spec")
    assert code == 0, text
    assert HIT_WORDS.search(text), text
    assert world2.exported_notes() == []
    assert len(world2.exported_json()) == 2


# Codes the fixtures cannot force: the design gives no derivation rule a run file can trigger.
CANNOT_BE_PRODUCED = {
    "worktree_busy",          # a worktree-claim condition, never recorded in a run result or label
    "consistency_conflicts",  # the design states no rule for it that a fixture can force
}


def _issue_world_runs(home: FakeClaudeHome) -> None:
    """Add runs to project A that force the outcome, review, permission, landing, gate and collection codes."""
    sess = home.sessions[0]
    tasks = [
        TaskSpec(task="5", wave=1, ok=False, reason="review: blocking findings remain",
                 findings=[{"severity": "blocking", "issue": "x", "file": "f.py"}], verdict="changes"),
        TaskSpec(task="6", wave=1, ok=False, reason="review: blocking findings remain",
                 findings=[{"severity": "blocking", "issue": "x", "file": "f.py"}], verdict="changes"),
        TaskSpec(task="7", wave=1, ok=False, reason="refused by the permission layer"),
        TaskSpec(task="8", wave=1, unreviewed=True, attempts=2, escalations=[{"stage": "retry", "model": "opus"}]),
        TaskSpec(task="9", wave=1),
    ]
    landings = [{"tasks": ["8"], "size": 1, "window": 2, "merged": ["8"], "red": ["8"], "conflict": [], "deferred": [],
                 "other": [], "gates": 2, "bisected": True, "minutes": 1.0}]
    result = implement_result(
        tasks, landings=landings, full_gates=[{"afterMerges": 1, "green": False}], final_gate={"green": False},
        cross_tests=[{"wave": 1, "tasks": 2, "verdict": "changes", "summary": "s",
                      "findings": [{"severity": "blocking", "issue": "x", "file": "f.py"}]}],
        fixer_escalations=[{"model": "opus"}], outcome="blocked")
    result["reportSource"] = "agents"
    result["stats"]["planFailures"] = 1
    agents = [
        AgentSpec("task 5", phase="Wave 1"),
        AgentSpec("task 6 [test]", phase="Wave 1"),
        AgentSpec("task 7", phase="Wave 1"),
        AgentSpec("task 8", phase="Wave 1"),
        AgentSpec("fix 1 (Task 8 red on landing)", phase="Wave 1", model="claude-opus-5-5"),
        AgentSpec("mystery step", phase="Wave 1"),
        AgentSpec("task 9", phase="Wave 1"),
    ]
    run = sess.implement_run("wf_seg-a-issues", start="2026-10-14T09:00:00Z", args={"spec": "ui", "root": str(sess.cwd)},
                             result=result, agents=agents)
    run.drop_transcript(len(agents) - 1)  # an agent with no transcript
    sess.implement_run("wf_seg-a-halted", start="2026-10-15T09:00:00Z", args={"spec": "ui", "root": str(sess.cwd)},
                       result=implement_result([TaskSpec(task="1", ok=False, reason="planning step failed")], outcome="halted"))
    refused = implement_result([TaskSpec(task="1")], outcome="refused")
    refused["refused"] = True
    sess.implement_run("wf_seg-a-refused", start="2026-10-16T09:00:00Z", args={"spec": "ui", "root": str(sess.cwd)}, result=refused)
    create = create_result(feature="ui", result="Needs work", lenses_lost=1, escalated=1, consistency_passes=2)
    create["consistency"]["decisions"] = [{"id": "D1"}]
    sess.create_run("wf_seg-a-create", start="2026-10-17T09:00:00Z", args={"feature": "ui", "root": str(sess.cwd)}, result=create)


def test_issue_codes_never_trip_the_leak_check(tmp_path):
    from claude_kiro.analytics.issues import ISSUE_CODES

    home, _, _ = _segment_world(tmp_path, ["analytics", "transcripts", "permission"])
    _issue_world_runs(home)
    world = World(tmp_path, home)
    world.collect("--all")
    out = world.export("--yes")
    assert out.exit_code == 0, out.output
    assert not HIT_WORDS.search(out.output), out.output
    codes = set()
    for p in world.exported_json():
        rec = json.loads(p.read_text())
        codes |= set(rec.get("issues", []))
    assert codes <= set(ISSUE_CODES)
    # these three (permission_refusal collides with the `permission` path segment) must be exported, not skipped
    assert {"review_contract_mismatch", "permission_refusal", "review_blocking_unresolved"} <= codes, codes
    # superset: the design defines no rule that keeps consistency_conflicts out, so it may or may not be exported
    assert codes >= set(ISSUE_CODES) - CANNOT_BE_PRODUCED, sorted(set(ISSUE_CODES) - CANNOT_BE_PRODUCED - codes)


def test_digit_and_hex_segments_export_next_to_versions_months_and_ids(tmp_path):
    home, _a, _b = _segment_world(tmp_path, ["1", "80", "2026", "cafe"], rich=False)
    world = World(tmp_path, home)
    world.collect("--all")
    out = world.export("--yes")
    assert out.exit_code == 0, out.output
    files = world.exported_json()
    assert len(files) == 2
    for p in files:
        rec = json.loads(p.read_text())
        assert rec["claude_code_version"] == "2.1.80", p
        assert rec["month"] == "2026-10", p
        assert p.parent.name == "2026-10", p
        assert re.fullmatch(r"[0-9a-f]{16}", rec["id"]) and p.stem == rec["id"], p


def test_submit_passes_the_leak_check_with_ck_words_in_the_records_and_opens_the_pr(tmp_path):
    home, _a, _b = _segment_world(tmp_path, ["analytics", "transcripts", "permission", "1", "80", "2026", "cafe"])
    world = World(tmp_path, home)
    world.collect("--all")
    out = world.submit("--yes")
    assert out.exit_code == 0, out.output
    branches = world.pushed_batches()
    assert len(branches) == 1 and BATCH_BRANCH.match(branches[0]), branches
    creates = [c for c in gh_calls(world.gh_log) if c["argv"][:2] == ["pr", "create"]]
    assert len(creates) == 1


def test_ck_vocabulary_segments_are_not_terms_but_other_segments_still_are(tmp_path):
    segs = ["spec", "ck", "stats", "batch", "orchard"]
    home, _, _ = _segment_world(tmp_path / "w1", segs)
    world = World(tmp_path / "w1", home)
    world.collect("--all")
    out = world.submit("--yes")
    assert out.exit_code == 0, out.output
    branches = world.pushed_batches()
    assert len(branches) == 1 and BATCH_BRANCH.match(branches[0]), branches
    assert [c for c in gh_calls(world.gh_log) if c["argv"][:2] == ["pr", "create"]]

    home2, a2, b2 = _segment_world(tmp_path / "w2", segs)
    world2 = World(tmp_path / "w2", home2)
    code, text = _export_b_then_note_a(world2, a2, b2, "touched files under orchard today")
    assert code == 0, text
    assert HIT_WORDS.search(text), text
    assert world2.exported_notes() == []


def test_schema_doc_documents_the_matching_rules():
    doc = REPO_ROOT / "analytics" / "SCHEMA.md"
    text = doc.read_text().lower()
    for pattern in (r"whole[- ]word", r"digit[- ]only|only (?:of )?(?:decimal|hex)", r"ck[- ]vocabulary|vocabulary term", r"fixed[- ]token"):
        assert re.search(pattern, text), f"analytics/SCHEMA.md does not document {pattern!r}"


# --------------------------------------------------------------------------- TC-62


def _lower_blob(*parts: object) -> str:
    return "\n".join(p if isinstance(p, str) else json.dumps(p) for p in parts).lower()


def test_nothing_seeded_reaches_files_branch_commit_or_pr(tmp_path):
    home, terms = seeded_home(tmp_path)
    assert list(terms) == list(CONFIDENTIAL)
    world = World(tmp_path, home)
    world.collect("--all")
    exported = world.export("--yes")
    assert exported.exit_code == 0, exported.output
    export_blob = _lower_blob(all_bytes(world.checkout / "analytics").decode(errors="replace"))

    sub = world.submit("--yes")
    assert sub.exit_code == 0, sub.output
    branches = world.pushed_batches()
    assert len(branches) == 1 and BATCH_BRANCH.match(branches[0]), branches
    branch = branches[0]
    message = git(world.bare, "log", "-1", "--format=%B", branch)
    names = git(world.bare, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", branch)
    assert names.strip(), "the commit should add the batch files"
    contents = [git(world.bare, "show", f"{branch}:{n}") for n in names.splitlines()]
    creates = [c for c in gh_calls(world.gh_log) if c["argv"][:2] == ["pr", "create"]]
    assert len(creates) == 1

    blob = _lower_blob(export_blob, branch, message, names, *contents, creates)
    leaked = [t for t in terms if t.lower() in blob]
    assert leaked == []
