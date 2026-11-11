"""Shared helpers for the arbiter and principles tests (run-principles). Test-owned.

Real boundaries only: a real git repo, a real integration worktree, the real `ck` CLI, the real hook entry point.
Project modules are imported inside function bodies, so every test file collects before the feature exists.
This file is the one place that knows (a) how the arbiter is identified, (b) how `decisions.md` is laid out, and
(c) how the test plan's Harness Contract names map to design.md's names (INTERFACE_MAP).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from hypothesis import strategies as st

CK = [sys.executable, "-m", "claude_kiro.cli"]
ARBITER_AGENT_TYPES = ("spec:arbiter", "ck-arbiter")

DEFAULT = object()  # "use the helper's stock text"

DEFAULT_PRINCIPLES = """# Principles

## Principles

- **test-intent:** a test states what the requirement says; repair the test, never bend the requirement to it.
- **remove-first:** remove before you optimize.
- **ask-on-reserved:** a reserved scope is decided by me, not by the arbiter.
"""

DEFAULT_PLAN = """# Plan: Demo

## Principles

- **Nothing to add:** the project defaults apply.
"""

DEFAULT_REQUIREMENTS = """# Requirements: Demo

## User Stories

### Story 1: Greeting

**As a** user **I want** a greeting **So that** I feel welcome.

**Without it:** the app is silent and users cannot tell it started.

**Acceptance Criteria:**

- 1.1 WHEN the app starts, THE SYSTEM SHALL print a greeting.
"""

DEFAULT_TASKS = """# Implementation Tasks: Demo

**Status:** Not Started

## Task Breakdown

### Task 1: Greeting tests
**Status:** Not Started
**Track:** test
**Requirements:** 1.1
**Description:** Test the greeting.
**Files:**
- `tests/test_greet.py` - the test
**Verify:** `uv run pytest -q tests/test_greet.py`
**Verifies:** Task 2 (its tests are the oracle)
**Dependencies:** None

---

### Task 2: Greeting
**Status:** Not Started
**Track:** impl
**Requirements:** 1.1
**Description:** Print the greeting.
**Files:**
- `src/greet.py` - the greeting
**Verify:** `uv run pytest -q tests/test_greet.py`
**Verified by:** Task 1 (tests/test_greet.py)
**Dependencies:** Task 1

---
"""

DEFAULT_CK_JSON = {
    "verify": ["python -c pass"],
    "guard": {"tests": "ask", "requirements": "ask", "test_plan": "ask"},
    "base": None,
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def ck_run(cwd: Path, *args: str, stdin: Optional[str] = None, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return subprocess.run([*CK, *args], cwd=str(cwd), input=stdin, capture_output=True, text=True,
                          env=env if env is not None else os.environ.copy())


# --------------------------------------------------------------------------- the project fixture


@dataclass
class ArbiterProject:
    root: Path                     # the main checkout, on `main`
    target: Path                   # the integration checkout, on integrate/demo
    spec_dir: Path                 # <root>/specs/demo
    into: str = "integrate/demo"
    spec: str = "demo"
    exec_ck: Dict[str, Any] = field(default_factory=dict)

    @property
    def run_args(self) -> Dict[str, str]:
        return {"root": str(self.root), "into": self.into, "targetDir": str(self.target)}

    def ck(self, *args: str, stdin: Optional[str] = None, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
        return ck_run(cwd or self.root, *args, stdin=stdin)


def arbiter_project(tmp_path: Path, *, tasks: Optional[str] = None, principles: Any = DEFAULT, plan: Any = DEFAULT,
                    requirements: Any = DEFAULT) -> ArbiterProject:
    """A real git repo on `main`, with `integrate/demo` checked out at `.ck/worktrees/demo-integration`.

    `principles`, `plan` and `requirements` take text, `DEFAULT`, or None to leave the file out.
    """
    root = Path(tmp_path) / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.com")
    specs = root / "specs"
    spec_dir = specs / "demo"
    spec_dir.mkdir(parents=True)
    (specs / "ck.json").write_text(json.dumps(DEFAULT_CK_JSON, indent=2) + "\n")
    texts = {
        "PRINCIPLES.md": (DEFAULT_PRINCIPLES if principles is DEFAULT else principles, specs),
        "PLAN.md": (DEFAULT_PLAN if plan is DEFAULT else plan, spec_dir),
        "requirements.md": (DEFAULT_REQUIREMENTS if requirements is DEFAULT else requirements, spec_dir),
        "design.md": ("# Design: Demo\n\nA greeting.\n", spec_dir),
        "test-plan.md": ("# Test Plan: Demo\n\n| TC-1 | 1.1 | I | greeting printed | CLI | tests/test_greet.py |\n", spec_dir),
        "tasks.md": (DEFAULT_TASKS if tasks is None else tasks, spec_dir),
    }
    for name, (text, folder) in texts.items():
        if text is not None:
            (folder / name).write_text(text)
    (root / ".gitignore").write_text(".ck/\n.claude/worktrees/\n__pycache__/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    made = ck_run(root, "worktree", "integration", "demo", "--base", "main")
    assert made.returncode == 0, made.stdout + made.stderr
    target = root / ".ck" / "worktrees" / "demo-integration"
    assert target.is_dir(), made.stdout + made.stderr
    return ArbiterProject(root=root, target=target, spec_dir=spec_dir,
                          exec_ck={"cmd": [sys.executable, "-m", "claude_kiro.cli"], "cwd": str(root)})


# --------------------------------------------------------------------------- the arbiter's identity (the only place)


def arbiter_caller(project: ArbiterProject, spec: str = "demo", phase: str = "run", monkeypatch=None,
                   checkout: Optional[Path] = None) -> dict:
    """Hook-input keyword arguments (`agent_id`, `agent_type`, `cwd`) that make the real hook see the real arbiter.

    run: the checkout is the worktree `demo-task-arbiter1` (or `checkout`). create: a lane ticket is opened with the
    real `ck arbiter lane open`, and the checkout is the project root.
    """
    identity = {"agent_id": "agent-arbiter-" + uuid.uuid4().hex[:8], "agent_type": ARBITER_AGENT_TYPES[0]}
    if phase == "run":
        if checkout is None:
            path = project.root / ".ck" / "worktrees" / f"{spec}-task-arbiter1"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                _git(project.root, "worktree", "add", "-q", "-b", f"feat/{spec}-task-arbiter1", str(path), project.into)
            checkout = path
        return {**identity, "cwd": str(checkout)}
    if phase == "create":
        opened = ck_run(project.root, "arbiter", "lane", "open", spec, "--json")
        assert opened.returncode == 0, "ck arbiter lane open failed: " + opened.stdout + opened.stderr
        return {**identity, "cwd": str(checkout or project.root)}
    raise ValueError(f"unknown phase {phase!r}")


# --------------------------------------------------------------------------- the record reader (the only parser)

_ENTRY = re.compile(r"^## Ruling (\d+): (.*)$")
_FIELD = re.compile(r"^- \*\*(.+?):\*\* ?(.*)$")


def decisions_entries(project: ArbiterProject, where: str = "target") -> List[dict]:
    """Every entry of `specs/demo/decisions.md`, in file order, as `{number, title, <field name>: text}`.

    Reads the file on the integration checkout (`where="target"`), the main checkout (`"root"`) or the branch tip
    (`"branch"`, through git). The entry format is design.md's `## Ruling <n>: ...` and its `- **Field:**` lines.
    """
    if where == "branch":
        shown = subprocess.run(["git", "show", f"{project.into}:specs/demo/decisions.md"], cwd=project.root,
                               capture_output=True, text=True)
        text = shown.stdout if shown.returncode == 0 else ""
    else:
        path = (project.target if where == "target" else project.root) / "specs" / "demo" / "decisions.md"
        text = path.read_text() if path.exists() else ""
    entries: List[dict] = []
    for line in text.splitlines():
        m = _ENTRY.match(line)
        if m:
            entries.append({"number": int(m.group(1)), "title": m.group(2).strip()})
            continue
        f = _FIELD.match(line)
        if f and entries:
            entries[-1][f.group(1).strip()] = f.group(2).strip()
    return entries


# --------------------------------------------------------------------------- interface map (Harness Contract -> design.md)

INTERFACE_MAP = {
    "arbiter.claim_id": ("claude_kiro.arbiter_policy", "claim_key"),
    "arbiter.decide_call": ("claude_kiro.arbiter_policy", "claim_facts", "decide_claim"),
    "arbiter.validate_ruling": ("claude_kiro.arbiter_policy", "validate_ruling"),
    "arbiter.route_ruling": ("claude_kiro.arbiter_policy", "route_ruling"),
    "arbiter.spec_edit_refusals": ("claude_kiro.arbiter_policy", "spec_text_changes", "decide_spec_edit"),
    "arbiter.effective_principles": ("claude_kiro.principles", "parse_principles", "effective_principles"),
    "guard_policy.arbiter_may_edit": ("claude_kiro.hooks._shared.guard_policy", "decide_arbiter_edit"),
    "guard_policy.is_arbiter_caller": ("claude_kiro.hooks._shared.guard_policy", "is_arbiter_caller"),
    "guard_policy.decide_arbiter_bash": ("claude_kiro.hooks._shared.guard_policy", "decide_arbiter_bash"),
    "guard_policy.arbiter_block_reason": ("claude_kiro.hooks._shared.guard_policy", "arbiter_block_reason"),
    "lint.justification_findings": ("claude_kiro.justifications", "justification_findings"),
    "lint.verify_scope": ("claude_kiro.lint_scope", "scope_findings", "dedupe_scope", "lint_verdict"),
}

CLI_MAP = {
    "ck arbiter decide": "ck arbiter open SPEC --task N --into BRANCH --json",
    "ck arbiter route": "ck arbiter settle SPEC KEY --tier ... --json",
    "ck arbiter record": "ck arbiter record SPEC --into BRANCH --json",
    "ck arbiter land": "ck worktree land SPEC arbiter<k> --into BRANCH --gate --json",
}

LINT_JSON_MAP = {
    "planPrinciples": "warnings entries of kind `principles`",
    "verifyScope.notChecked": "top-level `notChecked`",
    "verifyScope.checked": "ck lint SPEC --simulate --task N --json",
}


def seam(name: str, *parts: str):
    """Import a design.md seam by its Harness Contract name (inside the test body). `parts` picks entries of the map."""
    import importlib

    entry = INTERFACE_MAP[name]
    module = importlib.import_module(entry[0])
    names = parts or entry[1:]
    got = [getattr(module, n) for n in names]
    return got[0] if len(got) == 1 else tuple(got)


# --------------------------------------------------------------------------- strategies

TRIGGERS = ["test-conflict", "guard-stop", "review-deadlock", "command-cycle", "gate-failure", "audit", "lint"]
OUTCOMES = ["spec_edit", "test_repair", "code", "requirements"]

_word = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8)
_sentence = st.lists(_word, min_size=1, max_size=6).map(" ".join)
_test_ids = st.builds(lambda f, t: f"tests/test_{f}.py::test_{t}", _word, _word)


@st.composite
def claims(draw) -> dict:
    """A claim as the workflow reports it (the JSON `Claim` is built from): every trigger, every optional field."""
    trigger = draw(st.sampled_from(TRIGGERS))
    out: Dict[str, Any] = {"task": str(draw(st.integers(1, 120))), "trigger": trigger, "text": draw(_sentence)}
    if trigger in ("test-conflict", "guard-stop"):
        out.update(file=draw(_test_ids).split("::")[0], test=draw(_test_ids).split("::")[1], criterion=draw(_sentence))
    if trigger == "review-deadlock":
        out["findings"] = draw(st.lists(st.tuples(_word.map(lambda w: f"src/{w}.py"), _sentence), min_size=1, max_size=4))
    if trigger == "gate-failure":
        out["failing"] = draw(st.lists(_test_ids, min_size=1, max_size=4))
        out["models_tried"] = draw(st.lists(st.sampled_from(["haiku", "sonnet", "opus"]), max_size=3))
    if trigger == "lint":
        out.update(rule=draw(st.just("unfiltered-shared-suite")), subject=draw(_word),
                   tests=draw(st.lists(st.integers(1, 120).map(str), max_size=3)))
    if trigger == "command-cycle":
        provider = draw(_word)
        out.update(command=draw(_sentence), provider=provider, cycle=[str(draw(st.integers(1, 9))), provider])
    if draw(st.booleans()):
        out["evidence"] = draw(_sentence)
    return out


@st.composite
def rulings(draw, principles) -> dict:
    """An arbiter answer. `principles` is a list of ids (or of `{id,...}` dicts). Includes malformed answers."""
    ids = [p["id"] if isinstance(p, dict) else str(p) for p in principles] or ["project:none"]
    outcome = draw(st.one_of(st.sampled_from(OUTCOMES), st.sampled_from(["", "revert", "SPEC_EDIT"]), st.integers(), st.none()))
    out: Dict[str, Any] = {
        "status": draw(st.sampled_from(["ruled", "ruled", "needs_stronger_look"])),
        "reasoning": draw(_sentence),
        "outcome": outcome,
        "principle": draw(st.one_of(st.sampled_from(ids), st.just("project:made-up"), st.just(""))),
    }
    if draw(st.booleans()):
        out["claim"] = draw(st.text(alphabet="0123456789|:tesr_./", min_size=1, max_size=20))
    if outcome == "test_repair":
        out["testRepair"] = {"file": draw(_test_ids).split("::")[0], "test": draw(_word), "change": draw(_sentence), "why": draw(_sentence)}
    if outcome == "spec_edit":
        out["changed"] = draw(_sentence)
    if outcome == "code":
        out["code"] = draw(_sentence)
    if outcome == "requirements":
        out["requirement"] = {"kind": draw(st.sampled_from(["add", "change"])), "wording": draw(_sentence), "reason": draw(_sentence)}
    if out["status"] == "needs_stronger_look":
        out["established"] = draw(_sentence)
    if draw(st.booleans()):
        out["reservedBy"] = draw(st.sampled_from(["project:ask-on-reserved", "plan:reserves", ""]))
    return out


@st.composite
def histories(draw) -> list:
    """A ledger of prior claims: statuses open, failed, exhausted, ruled; call counts; ruling numbers."""
    n = draw(st.integers(0, 6))
    rows = []
    for _ in range(n):
        status = draw(st.sampled_from(["open", "failed", "exhausted", "ruled"]))
        rows.append({"key": f"{draw(st.integers(1, 9))}|test:tests/test_{draw(_word)}.py::test_{draw(_word)}",
                     "status": status, "calls": draw(st.integers(0, 4)),
                     "ruling": draw(st.integers(1, 9)) if status == "ruled" else None})
    return rows


_SEGMENTS = st.sampled_from(["tasks.md", "design.md", "test-plan.md", "requirements.md", "PLAN.md", "decisions.md",
                             "TASKS.md", "tasks.md.bak", "tasks.mdx", "other.md"])


@st.composite
def spec_paths(draw, spec: str = "demo") -> str:
    """Paths an arbiter edit might name: in-spec files, other specs, `.`/`..` forms, absolute, case and prefix tricks."""
    name = draw(_SEGMENTS)
    form = draw(st.sampled_from(["plain", "dot", "dotdot-in", "dotdot-out", "other-spec", "absolute", "double-slash",
                                 "trailing", "src", "prefix-spec", "case"]))
    if form == "plain":
        return f"specs/{spec}/{name}"
    if form == "dot":
        return f"./specs/{spec}/{name}"
    if form == "dotdot-in":
        return f"specs/{spec}/sub/../{name}"
    if form == "dotdot-out":
        return f"specs/{spec}/../{spec}x/{name}"
    if form == "other-spec":
        return f"specs/{spec}-other/{name}"
    if form == "absolute":
        return f"/abs/specs/{spec}/{name}"
    if form == "double-slash":
        return f"specs//{spec}//{name}"
    if form == "trailing":
        return f"specs/{spec}/{name}/"
    if form == "src":
        return f"src/{name}"
    if form == "prefix-spec":
        return f"specs/{spec}x/{name}"
    return f"SPECS/{spec.upper()}/{name}"


@st.composite
def justification_lines(draw) -> tuple:
    """`(line, counts)`: a `**Without it:**` line and whether its content counts. Empty and markup-only forms do not."""
    empty = draw(st.sampled_from(["", " ", "   ", "\t", "**", "*", "_", "``", "> ", "- ", "<!-- -->", "**  **", "~~", "---"]))
    label = draw(st.sampled_from(["**Without it:**", "**Without it:** ", "**Without it**:", "**Without it:**\t"]))
    if draw(st.booleans()):
        return (f"{label}{empty}", False)
    return (f"**Without it:** {draw(_sentence)}", True)


@st.composite
def requirements_docs(draw) -> tuple:
    """`(text, missing)`: a requirements document and the story numbers whose justification is missing.

    Forms: a justified story, no line, a blank line, a line inside a fenced code block, and a line that sits under
    another heading (outside the story's own section). A document may have no stories at all.
    """
    count = draw(st.integers(0, 5))
    parts = ["# Requirements: Demo\n"]
    missing: List[int] = []
    for n in range(1, count + 1):
        kind = draw(st.sampled_from(["ok", "none", "blank", "fenced", "outside"]))
        parts.append(f"### Story {n}: Title {n}\n\n**As a** user **I want** x **So that** y.\n")
        if kind == "ok":
            parts.append(f"**Without it:** {draw(_sentence)}\n")
        elif kind == "blank":
            parts.append("**Without it:** \n")
            missing.append(n)
        elif kind == "fenced":
            parts.append("```\n**Without it:** inside a fence\n```\n")
            missing.append(n)
        elif kind == "outside":
            parts.append("**Acceptance Criteria:**\n\n- 1.1 WHEN x, THE SYSTEM SHALL y.\n\n## Notes\n\n**Without it:** outside the story\n")
            missing.append(n)
        else:
            missing.append(n)
        parts.append("")
    return ("\n".join(parts) + "\n", missing)


FAILURE_FORMS = ["throw", None, {}, 7, "a string", [], {"status": 3, "reasoning": {"bad": True}}]


def arbiter_failures() -> st.SearchStrategy:
    """Every way an arbiter call can fail: throw, null, `{}`, a number, a string, an array, a schema-invalid object."""
    return st.sampled_from(FAILURE_FORMS)
