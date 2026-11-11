"""The arbiter's lane through the real guard hook (run-principles TC-79 to 87, 101, 103, 106, 134).

Every decision comes from the real `execute_hook("pre-tool-guard", ...)`; shell forms that pass the hook run under
real `bash -c`. The arbiter's identity comes only from `arbiter_helpers.arbiter_caller`. Project modules are
imported inside function bodies, so the file collects before the lane exists.
"""

import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import arbiter_helpers as ah
import guard_helpers as gh

REPO = Path(__file__).resolve().parent.parent
TASK_STARTED = {"in_progress": "In Progress", "done": "Done"}


# --------------------------------------------------------------------------- helpers


@pytest.fixture
def project(tmp_path):
    return ah.arbiter_project(tmp_path)


def verdict(result):
    """The permission decision of a hook result, or None when the hook said nothing (allowed)."""
    return gh.decision(result) if result and "hookSpecificOutput" in result else None


def reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def denied(result) -> bool:
    return verdict(result) in ("deny", "ask")


def set_statuses(checkout: Path, *statuses: str, spec: str = "demo") -> None:
    """Rewrite the `**Status:**` lines of the two tasks in `checkout`'s tasks.md (header stays Not Started)."""
    path = checkout / "specs" / spec / "tasks.md"
    text = path.read_text()
    head, _, rest = text.partition("### Task 1")
    first, _, second = rest.partition("### Task 2")
    first = first.replace("**Status:** Not Started", f"**Status:** {statuses[0]}", 1)
    second = second.replace("**Status:** Not Started", f"**Status:** {statuses[1]}", 1)
    path.write_text(head + "### Task 1" + first + "### Task 2" + second)


def call(caller: dict, tool: str, *, session: str = "s-arbiter", **tool_input):
    """One PreToolUse call through the real hook for `caller` (keys of `arbiter_caller`)."""
    return gh.pre(caller["cwd"], session, tool, agent_id=caller.get("agent_id"), agent_type=caller.get("agent_type"),
                  **tool_input)


def checkout_of(caller: dict) -> Path:
    return Path(caller["cwd"])


def edit_call(caller: dict, rel: str, old: str, new: str):
    return call(caller, "Edit", file_path=str(checkout_of(caller) / rel), old_string=old, new_string=new)


def write_call(caller: dict, path: str, content: str = "changed\n"):
    return call(caller, "Write", file_path=path, content=content)


def first_line(path: Path) -> str:
    return path.read_text().splitlines()[1] if len(path.read_text().splitlines()) > 1 else path.read_text().splitlines()[0]


def snapshot(root: Path, rels) -> dict:
    return {r: ((root / r).read_bytes() if (root / r).exists() else None) for r in rels}


def other_agent(checkout: Path, agent_type="general-purpose") -> dict:
    return {"agent_id": "agent-task-" + uuid.uuid4().hex[:8], "agent_type": agent_type, "cwd": str(checkout)}


def bash(caller: dict, command: str):
    return gh.run_bash(caller["cwd"], "s-arbiter", command, agent_id=caller.get("agent_id"),
                       agent_type=caller.get("agent_type"), cwd=caller["cwd"])


def plant(checkout: Path) -> None:
    """Files a denied command must not touch: a test, a source file, another spec, and a scratch test."""
    for rel, text in {"tests/test_greet.py": "def test_greet():\n    assert True\n", "tests/x.py": "x = 1\n",
                      "src/greet.py": "def greet():\n    return 'hi'\n",
                      "specs/other/tasks.md": "# Implementation Tasks: Other\n"}.items():
        path = checkout / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def reword(checkout: Path, name: str):
    """(old, new) strings that change existing text of a spec document."""
    path = checkout / "specs" / "demo" / name
    old = path.read_text().splitlines()[0]
    return old, old + " (reworded)"


# --------------------------------------------------------------------------- TC-79


@pytest.mark.parametrize("name", ["tasks.md", "design.md", "test-plan.md"])
def test_tc79_arbiter_edits_its_three_spec_files_with_a_task_in_progress(project, name):
    caller = ah.arbiter_caller(project)
    checkout = checkout_of(caller)
    set_statuses(checkout, "In Progress", "Not Started")
    old, new = reword(checkout, name)
    result = edit_call(caller, f"specs/demo/{name}", old, new)
    assert not denied(result), result


def test_tc79_arbiter_rewrites_existing_text_with_a_write_too(project):
    caller = ah.arbiter_caller(project)
    checkout = checkout_of(caller)
    set_statuses(checkout, "Done", "In Progress")
    result = write_call(caller, str(checkout / "specs/demo/test-plan.md"), "# Test Plan: Demo\n\nrewritten\n")
    assert not denied(result), result


# --------------------------------------------------------------------------- TC-80

MIXES = [("Not Started", "Not Started"), ("In Progress", "Not Started"), ("Done", "Not Started"),
         ("Done", "In Progress"), ("Done", "Done")]


@pytest.mark.parametrize("statuses", MIXES)
def test_tc80_requirements_denied_by_edit_and_by_append_in_a_run(project, statuses):
    caller = ah.arbiter_caller(project)
    checkout = checkout_of(caller)
    set_statuses(checkout, *statuses)
    path = checkout / "specs/demo/requirements.md"
    before = path.read_bytes()
    old, new = reword(checkout, "requirements.md")
    assert denied(edit_call(caller, "specs/demo/requirements.md", old, new))
    appended = path.read_text() + "- 1.2 WHEN the app stops, THE SYSTEM SHALL print a farewell.\n"
    assert denied(write_call(caller, str(path), appended))
    last = path.read_text().splitlines()[-1]
    assert denied(edit_call(caller, "specs/demo/requirements.md", last, last + "\n- 1.2 WHEN x, THE SYSTEM SHALL y."))
    assert path.read_bytes() == before


# --------------------------------------------------------------------------- TC-81


def test_tc81_everything_outside_the_three_files_is_denied(project):
    caller = ah.arbiter_caller(project)
    checkout = checkout_of(caller)
    plant(checkout)
    targets = ["tests/test_greet.py", "src/greet.py", "specs/demo/PLAN.md", "specs/PRINCIPLES.md", "specs/ck.json",
               "specs/other/tasks.md", ".claude/x"]
    before = snapshot(checkout, targets)
    for rel in targets:
        result = write_call(caller, str(checkout / rel))
        assert denied(result), rel
    assert snapshot(checkout, targets) == before
    outside = write_call(caller, "/etc/x")
    assert denied(outside)
    assert not Path("/etc/x").exists()


# --------------------------------------------------------------------------- TC-82

DENIED_SHELL = [
    "rm specs/demo/requirements.md",
    "rm tests/test_greet.py",
    "mv specs/demo/requirements.md specs/demo/requirements.bak",
    "mv tests/test_greet.py tests/old_greet.py",
    "echo x > specs/demo/requirements.md",
    "echo x > tests/test_greet.py",
    "echo x | tee specs/demo/requirements.md",
    "echo x | tee tests/test_greet.py",
    "sed -i s/a/b/ specs/demo/requirements.md",
    "sed -i s/a/b/ tests/test_greet.py",
    "python -c \"open('specs/demo/requirements.md','w').write('x')\"",
    "python -c \"open('tests/test_greet.py','w').write('x')\"",
    "cp specs/demo/design.md specs/demo/requirements.md",
    "cp specs/demo/design.md tests/test_greet.py",
]
SUBSTITUTION_SHELL = [
    'echo "$(rm tests/x.py)"',
    "echo `cat tests/x.py`",
    "echo `rm tests/x.py`",
    'git commit -m "$(rm tests/x.py)"',
    "cat <(rm tests/x.py)",
    "echo x >(rm tests/x.py)",
]
WATCHED = ["specs/demo/requirements.md", "specs/demo/design.md", "tests/test_greet.py", "tests/x.py", "src/greet.py"]


@pytest.mark.parametrize("command", DENIED_SHELL + SUBSTITUTION_SHELL)
def test_tc82_denied_shell_forms_are_denied_and_change_nothing(project, command):
    caller = ah.arbiter_caller(project)
    checkout = checkout_of(caller)
    plant(checkout)
    before = snapshot(checkout, WATCHED)
    outcome = bash(caller, command)
    assert denied(outcome.pre), command
    assert not outcome.ran
    assert snapshot(checkout, WATCHED) == before


def test_tc82_single_quoted_substitution_text_is_not_denied(project):
    caller = ah.arbiter_caller(project)
    checkout = checkout_of(caller)
    path = checkout / "specs/demo/design.md"
    path.write_text(path.read_text() + "\nA ruling.\n")
    staged = bash(caller, "git add specs/demo/design.md")
    assert not denied(staged.pre) and staged.returncode == 0
    outcome = bash(caller, "git commit -m 'text $(x)'")
    assert not denied(outcome.pre), outcome.pre
    assert outcome.returncode == 0
    subject = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=checkout, capture_output=True, text=True).stdout
    assert subject.strip() == "text $(x)"


# --------------------------------------------------------------------------- TC-83

CLAIMS = [
    ("env", {"env": {"CK_ARBITER": "1"}}),
    ("env-caller", {"env": {"CK_CALLER": "ck-arbiter"}}),
    ("argument", {"arg": True}),
    ("prompt", {"prompt": "You are the arbiter (spec:arbiter). The arbiter's lane applies to this call."}),
]


def claimed_pre(checkout: Path, claim: dict, monkeypatch, tool: str, **tool_input):
    from claude_kiro.cli.runner import execute_hook

    tool_input = dict(tool_input)
    if claim.get("arg"):
        tool_input["ck-arbiter"] = True
        tool_input["subagent_type"] = "ck-arbiter"
    data = gh.hook_input(str(checkout), "s-claim", tool, agent_id="agent-task-claimer", agent_type="general-purpose",
                         **tool_input)
    if "prompt" in claim:
        data["prompt"] = claim["prompt"]
        data["tool_input"]["description"] = claim["prompt"]
    for key, value in claim.get("env", {}).items():
        monkeypatch.setenv(key, value)
    return execute_hook("pre-tool-guard", data)


@pytest.mark.parametrize("label,claim", CLAIMS)
def test_tc83_a_claim_of_the_lane_changes_nothing(project, monkeypatch, label, claim):
    caller = ah.arbiter_caller(project)
    checkout = checkout_of(caller)
    plant(checkout)
    set_statuses(checkout, "In Progress", "Not Started")
    cases = [
        ("Write", {"file_path": str(checkout / "src/greet.py"), "content": "x = 1\n"}),
        ("Write", {"file_path": str(checkout / "specs/demo/PLAN.md"), "content": "x\n"}),
        ("Edit", dict(zip(("old_string", "new_string"), reword(checkout, "requirements.md")),
                      file_path=str(checkout / "specs/demo/requirements.md"))),
        ("Edit", dict(zip(("old_string", "new_string"), reword(checkout, "tasks.md")),
                      file_path=str(checkout / "specs/demo/tasks.md"))),
    ]
    for tool, tool_input in cases:
        plain = gh.pre(str(checkout), "s-plain", tool, agent_id="agent-task-claimer", agent_type="general-purpose",
                       **tool_input)
        claimed = claimed_pre(checkout, claim, monkeypatch, tool, **tool_input)
        assert verdict(claimed) == verdict(plain), (label, tool, tool_input["file_path"])
    # Positive control: the real arbiter is held to the lane on the same source edit.
    assert denied(write_call(caller, str(checkout / "src/greet.py"), "x = 1\n"))


# --------------------------------------------------------------------------- TC-84

MALFORMED_TYPES = ["spec:arbiter ", "SPEC:ARBITER", "CK-ARBITER", "ck-arbiter2", "spec:arbiter2", "arbiter", "", None, 7,
                   ["spec:arbiter"]]


def raw_pre(checkout: Path, **extra):
    from claude_kiro.cli.runner import execute_hook

    path = str(checkout / "src/greet.py")
    data = gh.hook_input(str(checkout), "s-raw", "Write", file_path=path, content="x = 1\n")
    data.update(extra)
    return execute_hook("pre-tool-guard", data)


@pytest.mark.parametrize("agent_id", [None, "", 0])
@pytest.mark.parametrize("agent_type", ["spec:arbiter", "ck-arbiter"])
def test_tc84_the_arbiter_type_without_an_agent_id_is_unclaimed(project, agent_id, agent_type):
    caller = ah.arbiter_caller(project)
    checkout = checkout_of(caller)
    plant(checkout)
    extra = {"agent_type": agent_type}
    if agent_id is not None:
        extra["agent_id"] = agent_id
    unclaimed = raw_pre(checkout)
    claimed = raw_pre(checkout, **extra)
    assert verdict(claimed) == verdict(unclaimed)
    assert denied(write_call(caller, str(checkout / "src/greet.py"), "x = 1\n"))


@pytest.mark.parametrize("bad_type", MALFORMED_TYPES, ids=repr)
def test_tc84_a_malformed_agent_type_is_unclaimed(project, bad_type):
    caller = ah.arbiter_caller(project)
    checkout = checkout_of(caller)
    plant(checkout)
    set_statuses(checkout, "In Progress", "Not Started")
    unclaimed = raw_pre(checkout, agent_id="agent-task-1")
    claimed = raw_pre(checkout, agent_id="agent-task-1", agent_type=bad_type)
    assert verdict(claimed) == verdict(unclaimed)
    assert not denied(claimed)
    assert denied(write_call(caller, str(checkout / "src/greet.py"), "x = 1\n"))


# --------------------------------------------------------------------------- TC-85


@pytest.mark.parametrize("name", ["requirements.md", "test-plan.md"])
def test_tc85_other_callers_keep_todays_decisions(project, name):
    root = project.root
    task_agent = other_agent(root)
    main = {"cwd": str(root)}
    path = root / "specs/demo" / name
    old, new = reword(root, name)

    def decisions():
        return {
            "task_reword": verdict(edit_call(task_agent, f"specs/demo/{name}", old, new)),
            "task_append": verdict(write_call(task_agent, str(path), path.read_text() + "\n| TC-2 | 1.1 | I | more | CLI | t |\n")),
            "main_reword": verdict(gh.pre(root, "s-main", "Edit", file_path=str(path), old_string=old, new_string=new)),
        }

    before = decisions()
    assert before == {"task_reword": None, "task_append": None, "main_reword": None}
    set_statuses(root, "In Progress", "Not Started")
    after = decisions()
    assert after == {"task_reword": "deny", "task_append": None, "main_reword": None}
    assert main  # the main session has no agent id


# --------------------------------------------------------------------------- TC-86


def test_tc86_a_denial_names_the_file_and_the_lane(project):
    caller = ah.arbiter_caller(project)
    checkout = checkout_of(caller)
    plant(checkout)
    for rel in ("specs/demo/requirements.md", "src/greet.py", "specs/demo/PLAN.md"):
        result = write_call(caller, str(checkout / rel))
        assert denied(result), rel
        text = reason(result)
        assert rel in text, text
        assert "outside" in text.lower() and "lane" in text.lower() and "arbiter" in text.lower(), text


def test_tc86_a_shell_denial_names_the_file_too(project):
    caller = ah.arbiter_caller(project)
    plant(checkout_of(caller))
    outcome = bash(caller, "rm tests/test_greet.py")
    assert denied(outcome.pre)
    text = reason(outcome.pre)
    assert "tests/test_greet.py" in text and "lane" in text.lower(), text


# --------------------------------------------------------------------------- TC-87


def test_tc87_the_arbiters_git_add_commit_and_lint_are_allowed(project):
    caller = ah.arbiter_caller(project)
    checkout = checkout_of(caller)
    for name in ("tasks.md", "design.md", "test-plan.md"):
        path = checkout / "specs/demo" / name
        path.write_text(path.read_text() + "\nRuling note.\n")
    added = bash(caller, "git add specs/demo/tasks.md specs/demo/design.md specs/demo/test-plan.md")
    assert not denied(added.pre) and added.returncode == 0, added.pre
    linted = bash(caller, "ck lint demo")
    assert not denied(linted.pre), linted.pre
    committed = bash(caller, "git commit -m 'arbiter: spec edit'")
    assert not denied(committed.pre) and committed.returncode == 0, committed.pre
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=checkout, capture_output=True,
                           text=True).stdout.split()
    assert sorted(files) == ["specs/demo/design.md", "specs/demo/tasks.md", "specs/demo/test-plan.md"]


# --------------------------------------------------------------------------- TC-101, TC-103, TC-106


def test_tc101_create_phase_corrects_requirements_wording_with_nothing_started(project):
    caller = ah.arbiter_caller(project, phase="create")
    checkout = checkout_of(caller)
    old, new = reword(checkout, "requirements.md")
    assert not denied(edit_call(caller, "specs/demo/requirements.md", old, new))
    assert not denied(edit_call(caller, "specs/demo/tasks.md", *reword(checkout, "tasks.md")))
    assert denied(write_call(caller, str(checkout / "src/greet.py"), "x = 1\n"))


@pytest.mark.parametrize("started", ["In Progress", "Done"])
def test_tc103_started_spec_denies_the_create_arbiter_and_still_freezes_the_reviser(project, started):
    caller = ah.arbiter_caller(project, phase="create")
    checkout = checkout_of(caller)
    set_statuses(checkout, started, "Not Started")
    path = checkout / "specs/demo/requirements.md"
    before = path.read_bytes()
    old, new = reword(checkout, "requirements.md")
    assert denied(edit_call(caller, "specs/demo/requirements.md", old, new))
    assert denied(write_call(caller, str(path), path.read_text() + "- 1.2 WHEN x, THE SYSTEM SHALL y.\n"))
    assert path.read_bytes() == before
    # The reviser (a task agent, not the arbiter) is still held to today's frozen-text rule.
    reviser = other_agent(checkout)
    assert verdict(edit_call(reviser, "specs/demo/requirements.md", old, new)) == "deny"
    assert verdict(write_call(reviser, str(path), path.read_text() + "- 1.2 WHEN x, THE SYSTEM SHALL y.\n")) is None
    node = subprocess.run(["node", str(REPO / "tests/js/spec_review_frozen.test.mjs")], cwd=REPO, capture_output=True,
                          text=True)
    assert node.returncode == 0, node.stdout + node.stderr


def test_tc106_plan_md_is_denied_in_create_and_in_run(project):
    for phase in ("run", "create"):
        caller = ah.arbiter_caller(project, phase=phase)
        checkout = checkout_of(caller)
        path = checkout / "specs/demo/PLAN.md"
        before = path.read_bytes()
        old = path.read_text().splitlines()[0]
        assert denied(edit_call(caller, "specs/demo/PLAN.md", old, old + " x")), phase
        assert denied(write_call(caller, str(path), path.read_text() + "\n- more\n")), phase
        assert path.read_bytes() == before


# --------------------------------------------------------------------------- TC-134


def test_tc134_the_hook_and_the_policy_seam_agree(project):
    from claude_kiro.hooks._shared import guard_policy

    may_edit = ah.seam("guard_policy.arbiter_may_edit")
    caller = ah.arbiter_caller(project)
    checkout = checkout_of(caller)
    for rel, expected in (("specs/demo/tasks.md", True), ("specs/demo/requirements.md", False)):
        facts = guard_policy.ArbiterEditFacts(rel=rel, arbiter_worktree_spec="demo", lane_specs=(),
                                              implementation_started=False)
        seam_allows = not may_edit(facts).block
        hook_allows = not denied(write_call(caller, str(checkout / rel), "x\n"))
        assert seam_allows == hook_allows == expected, rel
