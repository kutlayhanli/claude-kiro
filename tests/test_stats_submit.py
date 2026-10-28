"""End-to-end tests for `ck stats submit` (run analytics, requirement 10): TC-63..TC-71, TC-82.

Branch, commit and push are real git against a local bare repo (`kiro_checkout`); only `gh` is faked. Prompts run
under a real pty. Every `ck` call is a subprocess; nothing here imports the project's own code.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from stats_helpers import (
    ACME_REMOTE,
    CK,
    CONFIDENTIAL,
    TARGET_REPO,
    TARGET_URL,
    FakeClaudeHome,
    AgentSpec,
    TaskSpec,
    ck_sub,
    fake_claude_drafter,
    fake_gh,
    fresh_env,
    gh_calls,
    git,
    git_project,
    implement_result,
    kiro_checkout,
    path_without_gh,
    run_tty,
)

PROMPT = r"\[y/N\]"
LABEL = "testbox"
NOTE_TEXT = "Resolver needed 3 rounds on a lockfile conflict"


class World:
    """One fresh store, a kiro checkout with a bare remote, a fake gh, and collected runs."""

    def __init__(self, env, checkout: Path, bare: Path, bin_dir: Path, ghlog: Path, session, project: Path):
        self.env = env
        self.checkout = checkout
        self.bare = bare
        self.bin = bin_dir
        self.ghlog = ghlog
        self.session = session
        self.project = project
        self.runs = 0
        self.root: Path = env.root

    def vars(self, **over: str) -> Dict[str, str]:
        base = {k: str(v) for k, v in self.env.items()}
        base["PATH"] = f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"
        base.update(over)
        return base

    def add_run(self, collect: bool = True) -> str:
        self.runs += 1
        run_id = f"wf_sub-{self.runs}"
        day = 10 + self.runs
        self.session.implement_run(
            run_id, start=f"2026-10-{day:02d}T09:00:00Z",
            args={"spec": "zephyr-payroll", "root": str(self.project)},
            result=implement_result([TaskSpec(task="1", title="Reconcile Globex invoices")], into="integrate/zephyr-payroll"),
            agents=[AgentSpec("task 1", agent_id=f"a5eed{self.runs:011d}")],
        )
        if collect:
            self.collect()
        return run_id

    def collect(self) -> None:
        r = ck_sub("stats", "collect", "--all", env=self.vars(), cwd=self.project)
        assert r.exit_code == 0, r.output

    def submit(self, *extra: str, **env_over: str):
        return ck_sub("stats", "submit", "--to", str(self.checkout), *extra, env=self.vars(**env_over), cwd=self.checkout)

    def export(self, *extra: str):
        return ck_sub("stats", "export", "--to", str(self.checkout), *extra, env=self.vars(), cwd=self.checkout)

    def pty(self, sub: str, script, *extra: str):
        return run_tty([*CK, "stats", sub, "--to", str(self.checkout), *extra], env=self.vars(), cwd=self.checkout,
                       script=script)

    def pty_submit(self, reply: str, *extra: str):
        return self.pty("submit", [(PROMPT, reply)], *extra)

    # --- observers
    def local_branches(self) -> List[str]:
        return sorted(git(self.checkout, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())

    def bare_branches(self) -> List[str]:
        return sorted(git(self.bare, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())

    def batch_branches(self, repo: Optional[Path] = None) -> List[str]:
        return [b for b in git(repo or self.bare, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
                if b.startswith("ck-stats-batch-")]

    def current(self) -> str:
        return git(self.checkout, "rev-parse", "--abbrev-ref", "HEAD")

    def untracked(self) -> List[str]:
        out = git(self.checkout, "status", "--porcelain", "--untracked-files=all")
        return [l[3:] for l in out.splitlines() if l.startswith("??")]

    def exported_files(self) -> List[Path]:
        return sorted(p for p in (self.checkout / "analytics").rglob("*") if p.is_file()) if (self.checkout / "analytics").exists() else []

    def calls(self) -> List[dict]:
        return gh_calls(self.ghlog)

    def pr_calls(self) -> List[dict]:
        return [c for c in self.calls() if c["argv"][:2] == ["pr", "create"]]

    def snapshot(self) -> dict:
        files = {}
        for p in sorted(self.checkout.rglob("*")):
            if p.is_file() and ".git" not in p.relative_to(self.checkout).parts:
                files[str(p.relative_to(self.checkout))] = p.read_bytes()
        return {
            "head": git(self.checkout, "rev-parse", "HEAD"),
            "status": git(self.checkout, "status", "--porcelain", "--untracked-files=all"),
            "branches": self.local_branches(),
            "bare": self.bare_branches(),
            "files": files,
        }

    def branch_changes(self, branch: str) -> List[str]:
        return [l for l in git(self.bare, "diff", "--name-status", "master", branch).splitlines() if l]


def head_of(call: dict) -> str:
    return call["argv"][call["argv"].index("--head") + 1]


def pr_text(call: dict) -> str:
    argv = call["argv"]
    title = argv[argv.index("--title") + 1] if "--title" in argv else ""
    return title + "\n" + (call.get("body") or "") + "\n" + " ".join(argv)


def assert_no_confidential(text: str) -> None:
    for term in CONFIDENTIAL:
        assert term not in text, f"confidential term {term!r} leaked into: {text!r}"


@pytest.fixture
def make_world():
    stack = contextlib.ExitStack()

    def build(runs: int = 2, **kiro_kw) -> World:
        env = stack.enter_context(fresh_env())
        root = env.root
        project = git_project(root, ACME_REMOTE, name="acme-ledger")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        ghlog = root / "gh-calls.jsonl"
        fake_gh(bin_dir, ghlog)
        checkout, bare = kiro_checkout(root, **kiro_kw)
        home = FakeClaudeHome(env.claude)
        session = home.session("-home-dana-acme-ledger", cwd=project, git_branch="integrate/zephyr-payroll")
        w = World(env, checkout, bare, bin_dir, ghlog, session, project)
        r = ck_sub("agents", "set", "run.machine_label", LABEL, env=w.vars(), cwd=project)
        assert r.exit_code == 0, r.output
        for _ in range(runs):
            w.add_run(collect=False)
        if runs:
            w.collect()
        return w

    with stack:
        yield build


def record_files(w: World, branch: str) -> List[str]:
    return [l for l in w.branch_changes(branch)]


# --------------------------------------------------------------------------- TC-63


def test_tc63_diff_and_leak_check_shown_before_prompt_and_no_branch(make_world):
    w = make_world(runs=2)
    before_branches = w.local_branches()
    code, out = w.pty_submit("n\n")
    idx = out.index("[y/N]")
    before = out[:idx]
    files = w.exported_files()
    assert len(files) >= 2, out
    for f in files:
        for line in f.read_text().splitlines():
            if line.strip():
                assert line.strip() in before, f"{f}: {line!r} not shown before the prompt:\n{before}"
    leak_lines = [l for l in before.splitlines() if "leak" in l.lower() and "check" in l.lower()]
    assert leak_lines, before
    assert any("✓" in l for l in leak_lines), leak_lines
    assert w.local_branches() == before_branches
    assert w.batch_branches() == []
    assert w.pr_calls() == []


# --------------------------------------------------------------------------- TC-64


def _assert_pr(w: World, call: dict, n: int, kinds_months=("spec-implement", "2026-10")) -> str:
    argv = call["argv"]
    assert argv[:2] == ["pr", "create"]
    assert "--draft" in argv
    assert argv[argv.index("--repo") + 1] == TARGET_REPO
    assert argv[argv.index("--base") + 1] == "master"
    branch = head_of(call)
    import re
    assert re.fullmatch(r"ck-stats-batch-[0-9a-f]{8}", branch), branch
    text = pr_text(call)
    assert str(n) in text
    for needle in (*kinds_months, LABEL):
        assert needle in text, (needle, text)
    assert_no_confidential(text)
    return branch


def _assert_batch_branch(w: World, branch: str, n: int) -> None:
    assert branch in w.bare_branches()
    assert git(w.bare, "rev-list", "--count", f"master..{branch}") == "1"
    assert git(w.bare, "rev-parse", f"{branch}^") == git(w.bare, "rev-parse", "master")
    changes = w.branch_changes(branch)
    assert len(changes) >= n
    for line in changes:
        status, path = line.split("\t", 1)
        assert status == "A", line
        assert path.startswith("analytics/runs/"), line
    assert_no_confidential(git(w.bare, "log", "-1", "--format=%B", branch))


def test_tc64_confirm_creates_branch_commit_and_draft_pr(make_world):
    w = make_world(runs=2)
    code, out = w.pty_submit("y\n")
    assert code == 0, out
    calls = w.pr_calls()
    assert len(calls) == 1, w.calls()
    branch = _assert_pr(w, calls[0], 2)
    _assert_batch_branch(w, branch, 2)
    assert "https://github.com/kutlayhanli/claude-kiro/pull/101" in out
    assert w.current() == "master"
    assert w.untracked() == []


def test_tc64_unpushed_commit_is_not_in_pr_branch(make_world):
    w = make_world(runs=1, unpushed_commit=True)
    unpushed = git(w.checkout, "rev-parse", "HEAD")
    assert w.current() == "local-work"
    code, out = w.pty_submit("y\n")
    assert code == 0, out
    calls = w.pr_calls()
    assert len(calls) == 1
    branch = head_of(calls[0])
    _assert_batch_branch(w, branch, 1)
    assert w.current() == "local-work"
    assert git(w.checkout, "rev-parse", "HEAD") == unpushed
    r = subprocess.run(["git", "merge-base", "--is-ancestor", unpushed, branch], cwd=w.bare, capture_output=True)
    assert r.returncode != 0


def test_tc64_fork_origin_branch_goes_to_upstream_only(make_world):
    w = make_world(runs=1, remote="https://github.com/someone/claude-kiro.git", extra_remotes={"upstream": TARGET_URL})
    code, out = w.pty_submit("y\n")
    assert code == 0, out
    calls = w.pr_calls()
    assert len(calls) == 1
    branch = head_of(calls[0])
    assert branch in w.bare_branches()
    origin_store = w.root / "other-bare-origin.git"
    assert origin_store.exists()
    assert w.batch_branches(origin_store) == []


# --------------------------------------------------------------------------- TC-65


def test_tc65_second_submit_commits_only_new_record_third_says_nothing_new(make_world):
    w = make_world(runs=2)
    code, out = w.pty_submit("y\n")
    assert code == 0, out
    first = head_of(w.pr_calls()[0])
    first_files = {l.split("\t", 1)[1] for l in w.branch_changes(first)}
    w.add_run()
    r = w.submit("--yes")
    assert r.exit_code == 0, r.output
    calls = w.pr_calls()
    assert len(calls) == 2, r.output
    second = head_of(calls[1])
    assert second != first
    second_files = {l.split("\t", 1)[1] for l in w.branch_changes(second)}
    assert git(w.bare, "rev-list", "--count", f"master..{second}") == "1"
    assert len(second_files) >= 1
    assert second_files.isdisjoint(first_files)
    assert len(second_files) < len(first_files)
    r3 = w.submit("--yes")
    assert r3.exit_code == 0, r3.output
    assert "nothing new" in r3.output.lower()
    assert len(w.pr_calls()) == 2


# --------------------------------------------------------------------------- TC-66


def test_tc66_decline_leaves_no_trace_and_later_submit_includes_records(make_world):
    w = make_world(runs=2)
    local, bare, head = w.local_branches(), w.bare_branches(), git(w.checkout, "rev-parse", "HEAD")
    code, out = w.pty_submit("n\n")
    assert code == 0, out
    assert w.local_branches() == local
    assert w.bare_branches() == bare
    assert git(w.checkout, "rev-parse", "HEAD") == head
    assert w.calls() == []
    exported = len(w.untracked())
    assert exported >= 2
    r = w.submit("--yes")
    assert r.exit_code == 0, r.output
    calls = w.pr_calls()
    assert len(calls) == 1
    changes = w.branch_changes(head_of(calls[0]))
    assert len(changes) == exported


# --------------------------------------------------------------------------- TC-67


def test_tc67_yes_without_tty_prints_diff_and_leak_check_and_opens_pr(make_world):
    w = make_world(runs=2)
    r = w.submit("--yes")
    assert r.exit_code == 0, r.output
    assert "[y/N]" not in r.output
    assert "analytics/runs/" in r.output
    leak_lines = [l for l in r.output.splitlines() if "leak" in l.lower() and "check" in l.lower()]
    assert leak_lines and any("✓" in l for l in leak_lines), r.output
    assert len(w.pr_calls()) == 1
    assert "https://github.com/kutlayhanli/claude-kiro/pull/101" in r.output


def test_tc67_note_yes_without_tty_starts_no_drafter_and_submits_the_rest(make_world):
    w = make_world(runs=2)
    drafter = fake_claude_drafter(w.bin, NOTE_TEXT)
    r = w.submit("--note", "--yes")
    assert r.exit_code == 0, r.output
    assert drafter.calls() == []
    assert len(w.pr_calls()) == 1
    changes = w.branch_changes(head_of(w.pr_calls()[0]))
    assert len(changes) >= 2
    assert not any(l.endswith(".md") for l in changes)
    assert NOTE_TEXT not in r.output + "".join(p.read_text() for p in w.exported_files() if p.exists())


# --------------------------------------------------------------------------- TC-68


@pytest.mark.parametrize("case", ["no_gh", "auth_fails", "pr_create_fails"])
def test_tc68_gh_failures_leave_files_in_checkout_and_later_submit_includes_them(make_world, case):
    w = make_world(runs=2)
    original_branches = w.local_branches()
    over: Dict[str, str] = {}
    if case == "no_gh":
        over["PATH"] = path_without_gh(w.root)
    elif case == "auth_fails":
        over["FAKE_GH_AUTH_RC"] = "1"
    else:
        over["FAKE_GH_FAIL_RC"] = "1"
    r = w.submit("--yes", **over)
    assert r.exit_code != 0, r.output
    assert r.output.strip()
    assert "gh" in r.output.lower() or "pull request" in r.output.lower()
    exported = w.untracked()
    assert len([p for p in exported if p.startswith("analytics/runs/")]) >= 2, exported
    assert w.local_branches() == original_branches
    assert w.current() == "master"
    shutil.rmtree(w.checkout / "analytics")
    assert w.untracked() == []
    before_calls = len(w.pr_calls())
    ok = w.submit("--yes")
    assert ok.exit_code == 0, ok.output
    calls = w.pr_calls()
    assert len(calls) == before_calls + 1
    changes = w.branch_changes(head_of(calls[-1]))
    assert len(changes) >= len(exported)


# --------------------------------------------------------------------------- TC-69


def _assert_refused_unchanged(w: World, snap: dict, r) -> None:
    assert r.exit_code != 0, r.output
    assert r.output.strip()
    assert w.snapshot() == snap
    assert w.calls() == []
    assert w.batch_branches() == []


def test_tc69a_uncommitted_change_is_refused_and_nothing_changes(make_world):
    w = make_world(runs=2)
    (w.checkout / "README.md").write_text("edited locally\n")
    snap = w.snapshot()
    r = w.submit("--yes")
    _assert_refused_unchanged(w, snap, r)
    assert "uncommitted" in r.output.lower()


def test_tc69b_existing_destination_file_is_refused_and_nothing_changes(make_world):
    w = make_world(runs=1)
    r0 = w.export()
    assert r0.exit_code == 0, r0.output
    files = w.untracked()
    assert files
    git(w.checkout, "add", "-A")
    git(w.checkout, "commit", "-qm", "oops tracked the exported files")
    snap = w.snapshot()
    r = w.submit("--yes")
    _assert_refused_unchanged(w, snap, r)


def test_tc69c_own_exported_untracked_files_do_not_block_submit(make_world):
    w = make_world(runs=2)
    code, out = w.pty_submit("n\n")
    assert code == 0, out
    exported = w.untracked()
    assert exported
    r = w.submit("--yes")
    assert r.exit_code == 0, r.output
    calls = w.pr_calls()
    assert len(calls) == 1
    changes = {l.split("\t", 1)[1] for l in w.branch_changes(head_of(calls[0]))}
    assert changes == set(exported)


# --------------------------------------------------------------------------- TC-70


def test_tc70_nothing_new_says_so_and_does_nothing(make_world):
    w = make_world(runs=0)
    snap = w.snapshot()
    r = w.submit("--yes")
    assert r.exit_code == 0, r.output
    assert "nothing new" in r.output.lower()
    assert w.snapshot() == snap
    assert w.calls() == []

    w.add_run()
    assert w.submit("--yes").exit_code == 0
    calls_before = len(w.calls())
    branches = w.bare_branches()
    r2 = w.submit("--yes")
    assert r2.exit_code == 0, r2.output
    assert "nothing new" in r2.output.lower()
    assert len(w.calls()) == calls_before
    assert w.bare_branches() == branches


# --------------------------------------------------------------------------- TC-71


@pytest.mark.parametrize("origin", [
    "https://github.com/angelsen/claude-kiro.git",
    "https://github.com/kutlayhanli/claude-kiro-fork.git",
    "https://gitlab.com/kutlayhanli/claude-kiro.git",
])
def test_tc71_wrong_origin_is_refused_and_nothing_is_written(make_world, origin):
    w = make_world(runs=2, remote=origin)
    snap = w.snapshot()
    store = w.root / "other-bare-origin.git"
    other_before = sorted(git(store, "for-each-ref", "--format=%(refname:short)").split()) if store.exists() else []
    r = w.submit("--yes")
    assert r.exit_code != 0, r.output
    assert r.output.strip()
    assert w.snapshot() == snap
    assert w.exported_files() == []
    assert w.calls() == []
    other_after = sorted(git(store, "for-each-ref", "--format=%(refname:short)").split()) if store.exists() else []
    assert other_after == other_before


# --------------------------------------------------------------------------- TC-82


@pytest.fixture
def note_world(make_world):
    """An approved note from an earlier interactive `export --note`, plus one more collected, unexported run."""
    w = make_world(runs=1)
    drafter = fake_claude_drafter(w.bin, NOTE_TEXT)
    code, out = w.pty("export", [(r"(?i)accept", "accept\n")], "--note")
    assert code == 0, out
    notes = [p for p in w.exported_files() if p.suffix == ".md"]
    assert len(notes) == 1, (out, w.exported_files())
    assert notes[0].read_text().strip() == NOTE_TEXT
    w.add_run()
    w.note = notes[0]
    w.note_bytes = notes[0].read_bytes()
    w.drafter = drafter
    w.drafter_calls = len(drafter.calls())
    return w


def test_tc82_yes_without_tty_withholds_note_and_submits_records(note_world):
    w = note_world
    r = w.submit("--yes")
    assert r.exit_code == 0, r.output
    assert "[y/N]" not in r.output
    low = r.output.lower()
    assert "withheld" in low and "interactive" in low, r.output
    calls = w.pr_calls()
    assert len(calls) == 1
    branch = head_of(calls[0])
    assert git(w.bare, "rev-list", "--count", f"master..{branch}") == "1"
    changes = w.branch_changes(branch)
    assert len(changes) == 2, changes
    assert not any(l.endswith(".md") for l in changes)
    assert w.note.exists() and w.note.read_bytes() == w.note_bytes
    assert str(w.note.relative_to(w.checkout)) in w.untracked()
    assert len(w.drafter.calls()) == w.drafter_calls
    again = w.submit("--yes")
    assert again.exit_code == 0, again.output
    assert "nothing new" in again.output.lower()
    assert len(w.pr_calls()) == 1


def test_tc82_yes_under_pty_still_prompts_and_commits_the_note(note_world):
    w = note_world
    code, out = w.pty_submit("y\n", "--yes")
    assert code == 0, out
    calls = w.pr_calls()
    assert len(calls) == 1
    changes = w.branch_changes(head_of(calls[0]))
    assert len(changes) == 3, changes
    md = [l for l in changes if l.endswith(".md")]
    assert len(md) == 1
    shown = git(w.bare, "show", f"{head_of(calls[0])}:{md[0].split(chr(9), 1)[1]}")
    assert shown.strip() == NOTE_TEXT


def test_tc82_no_yes_and_no_tty_refuses_before_touching_anything(note_world):
    w = note_world
    snap = w.snapshot()
    r = w.submit()
    assert r.exit_code != 0, r.output
    assert r.output.strip()
    assert w.snapshot() == snap
    assert w.calls() == []
    assert w.batch_branches() == []
