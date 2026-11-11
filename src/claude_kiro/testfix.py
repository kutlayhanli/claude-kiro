"""Automatic test repair (Story 4): the one-fix ledger, the scope check, the proof and the landing gate scope.

Pure seams: `scope_decision` (4.5), `fix_allowed` (4.8), `proof_decision` (4.2, 4.11), `gate_scope` (4.4).
`prove` checks scope first and only then builds a temporary checkout and runs the fixed tests. No network.
"""

import contextlib
import dataclasses
import fcntl
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import worktrees as wt
from .config import load_config
from .paths import LEGACY_SPECS_DIR, SPECS_DIR, spec_roots

PLUGIN_DIR = Path(__file__).parent / "resources" / "pytest_plugin"
OUTCOMES_PLUGIN = "ck_testfix_outcomes"
PROOF_TIMEOUT = 600

Ledger = Mapping[str, Mapping[str, str]]


class TestFixError(Exception):
    """A test-fix command that cannot run (no such branch, a damaged ledger)."""

    __test__ = False


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Change:
    path: str
    status: str  # A M D R
    before: Optional[str] = None  # file text before the fix (M, D), for the TC-token check
    after: Optional[str] = None   # file text after the fix (A, M)


@dataclass(frozen=True)
class ScopeDecision:
    in_scope: bool
    outside: Tuple[str, ...]
    requirements_edited: bool
    spec_docs_edited: Tuple[str, ...]
    reason: str

    def as_json(self) -> Dict[str, Any]:
        return {"in_scope": self.in_scope, "outside": list(self.outside),
                "requirements_edited": self.requirements_edited,
                "spec_docs_edited": list(self.spec_docs_edited), "reason": self.reason}


@dataclass(frozen=True)
class ProofOutcome:
    node: str
    passed: bool
    cls: Optional[str]  # "not-built" | "bad" | "unclear" | "skipped" | "xfailed" | "xpassed" | "missing"


@dataclass(frozen=True)
class ProofVerdict:
    proved: bool
    against: str
    outcomes: Tuple[ProofOutcome, ...]
    reason: str

    def as_json(self) -> Dict[str, Any]:
        return {"proved": self.proved, "against": self.against, "reason": self.reason,
                "outcomes": [{"node": o.node, "passed": o.passed, "cls": o.cls} for o in self.outcomes]}


@dataclass(frozen=True)
class FixAllowance:
    allowed: Tuple[str, ...]
    already: Tuple[Mapping[str, str], ...]  # {node, at, fix}


@dataclass(frozen=True)
class ProveResult:
    scope: ScopeDecision
    proof: Optional[ProofVerdict]
    changed_tests: Tuple[str, ...]
    against: str

    def as_json(self) -> Dict[str, Any]:
        return {"scope": self.scope.as_json(), "proof": self.proof.as_json() if self.proof else None,
                "changedTests": list(self.changed_tests), "against": self.against}


# --------------------------------------------------------------------------- the one-fix ledger (4.8, 4.10)


def canonical_node(node: str) -> str:
    """The ledger key of a test node: a re-spelled id names the same entry (design: Node canonicalisation)."""
    text = node.strip().replace("\\", "/")
    path, sep, rest = text.partition("::")
    path = re.sub(r"/{2,}", "/", path)
    path = posixpath.normpath(path) if path else path
    rest = re.sub(r"\[.*\]\s*$", "", rest).strip()
    return (path + sep + rest).lower()


def ledger_path(root: Path, spec: str) -> Path:
    return wt._common_dir(root) / "ck" / spec / "testfix-ledger.json"


def load_ledger(path: Path) -> Dict[str, Dict[str, str]]:
    """The ledger; a missing file is empty. Anything unreadable or malformed raises TestFixError (never grants)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TestFixError(f"the test-fix ledger {path} is damaged: {exc}")
    if not isinstance(data, dict):
        raise TestFixError(f"the test-fix ledger {path} is damaged: not a JSON object")
    for key, entry in data.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("fix"), str):
            raise TestFixError(f"the test-fix ledger {path} is damaged: bad entry for {key}")
    return data


def fix_allowed(nodes: Sequence[str], ledger: Ledger) -> FixAllowance:
    """Nodes already in the ledger are refused with their earlier fix; each node is reported once."""
    known = {canonical_node(k): v for k, v in ledger.items()}
    allowed: List[str] = []
    already: List[Mapping[str, str]] = []
    seen = set()
    for node in nodes:
        key = canonical_node(node)
        if key in seen:
            continue
        seen.add(key)
        entry = known.get(key)
        if entry is None:
            allowed.append(node)
        else:
            already.append({"node": node, "at": str(entry.get("at", "")), "fix": str(entry.get("fix", ""))})
    return FixAllowance(tuple(allowed), tuple(already))


def record_fix(root: Path, spec: str, fix: str, nodes: Sequence[str], outcome: str) -> Dict[str, Dict[str, str]]:
    """Write a ledger entry per node, under a lock; an earlier entry for a node is never rewritten."""
    path = ledger_path(root, spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / "testfix-ledger.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            ledger = load_ledger(path)
            known = {canonical_node(k) for k in ledger}
            at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for node in nodes:
                key = canonical_node(node)
                if key not in known:
                    ledger[key] = {"fix": fix, "at": at, "outcome": outcome}
                    known.add(key)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, path)
            return ledger
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


# --------------------------------------------------------------------------- scope (4.5)

_TC_TOKEN = re.compile(r"(?<![A-Za-z0-9])tc[-_]?(\d+)", re.IGNORECASE)
_SPEC_DOC = re.compile(r"(?:^|/)(requirements|test-plan)\.md$")
_CACHE = {"__pycache__", ".pytest_cache", "node_modules", ".mypy_cache"}


def _tc_ids(text: Optional[str]) -> set:
    return {int(m) for m in _TC_TOKEN.findall(text or "")}


def _is_test_path(path: str, config: Mapping[str, Any]) -> bool:
    from .hooks._shared.test_heuristics import is_test_file

    tests = config.get("tests", {}) or {}
    dirs = set(tests.get("dirs", []))
    parts = PurePosixPath(path).parts
    if is_test_file(path, dict(config)):
        by_dir = len(parts) > 1 and parts[0] in dirs
        by_name = any(PurePosixPath(path).match(p) for p in tests.get("files", []))
        if by_dir or by_name:
            return True
    return False


def _is_fixture_path(path: str, config: Mapping[str, Any]) -> bool:
    recorded = (config.get("fixtures", {}) or {}).get("recorded", []) or []
    if any(fnmatch(path, g) for g in recorded):
        return True
    dirs = set((config.get("tests", {}) or {}).get("dirs", []))
    parts = PurePosixPath(path).parts
    suffix = PurePosixPath(path).suffix
    return (len(parts) > 2 and parts[0] in dirs and any(p in ("fixtures", "fixture") for p in parts[1:-1])
            and suffix not in (".pyc", ".pyo") and not _CACHE.intersection(parts))


def scope_decision(changed: Sequence[Change], plan_before: str, plan_after: str, config: Mapping[str, Any]) -> ScopeDecision:
    """In scope only if every changed path is a test file or fixture, no spec requirements/test-plan changed at all
    (a new row included), and no changed test lost a TC id it carried before the fix."""
    outside: List[str] = []
    docs: List[str] = []
    retagged: List[str] = []
    requirements = False
    for change in changed:
        path = change.path.replace("\\", "/")
        if _SPEC_DOC.search(path):
            docs.append(path)
            requirements = requirements or path.endswith("requirements.md")
        is_test = not path.startswith("specs/") and _is_test_path(path, config)
        if path.startswith("specs/") or not (is_test or _is_fixture_path(path, config)):
            outside.append(path)
        elif is_test and change.before is not None and _tc_ids(change.before) - _tc_ids(change.after):
            retagged.append(path)
    if plan_before != plan_after and not any(d.endswith("test-plan.md") for d in docs):
        docs.append("test-plan.md")
    out = tuple(dict.fromkeys(outside + retagged))
    docs_t = tuple(dict.fromkeys(docs))
    reasons = []
    if outside:
        reasons.append("changes files that are not tests or fixtures: " + ", ".join(dict.fromkeys(outside)))
    if docs_t:
        reasons.append("changes the spec's " + ", ".join(docs_t))
    if retagged:
        reasons.append("drops a TC id from: " + ", ".join(retagged))
    return ScopeDecision(not reasons, out, requirements, docs_t, "; ".join(reasons) or "tests and fixtures only")


# --------------------------------------------------------------------------- proof (4.2, 4.11)


def proof_decision(outcomes: Sequence[ProofOutcome]) -> ProofVerdict:
    """Proved iff every node passed or failed only as `not-built`; skipped, xfailed, deselected or missing never pass."""
    if not outcomes:
        return ProofVerdict(False, "", (), "no test node was run")
    bad = [o for o in outcomes if not (o.passed or o.cls == "not-built")]
    if bad:
        return ProofVerdict(False, "", tuple(outcomes), "not proved: " + ", ".join(f"{o.node} ({o.cls})" for o in bad))
    return ProofVerdict(True, "", tuple(outcomes), "every fixed test passes or fails only for what the blocked task builds")


# --------------------------------------------------------------------------- gate scope (4.4)


def gate_scope(graph, changed: Sequence[str], nodes: Sequence[str]) -> List[str]:
    """The test tasks owning a changed file, plus those owning the file of a fixed node (covering its TC id)."""
    from .graph import paths_overlap

    files = {f"file:{n.label}" for n in graph.nodes if n.kind == "test_file"}
    wanted = [c.replace("\\", "/") for c in changed] + [n.split("::", 1)[0].replace("\\", "/") for n in nodes]
    tasks = set()
    for edge in graph.edges:
        if edge.kind != "owns" or edge.dst not in files:
            continue
        owned = edge.dst[len("file:"):]
        if any(paths_overlap(owned, w) for w in wanted):
            tasks.add(edge.src[len("task:"):])
    return sorted(tasks, key=lambda t: (int(t) if t.isdigit() else 0, t))


# --------------------------------------------------------------------------- prove


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_AUTHOR_NAME": "ck", "GIT_AUTHOR_EMAIL": "ck@localhost",
           "GIT_COMMITTER_NAME": "ck", "GIT_COMMITTER_EMAIL": "ck@localhost"}
    return wt.git(cwd, *args, check=check, env=env)


def _show(root: Path, rev: str, path: str) -> Optional[str]:
    proc = wt.git(root, "show", f"{rev}:{path}", check=False)
    return proc.stdout if proc.returncode == 0 else None


def _plan_texts(root: Path, spec: str, before: str, after: str) -> Tuple[str, str]:
    for base in (SPECS_DIR, LEGACY_SPECS_DIR):
        path = f"{base}/{spec}/test-plan.md"
        a, b = _show(root, before, path), _show(root, after, path)
        if a is not None or b is not None:
            return a or "", b or ""
    return "", ""


def scope_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    """The config a test fix is scoped by: a bare `conftest.py` glob would admit the root conftest.py, which
    changes how every test imports and collects, so only conftest files under a test directory count."""
    out = json.loads(json.dumps(dict(config)))
    tests = out.setdefault("tests", {})
    tests["files"] = [p for p in tests.get("files", []) if p != "conftest.py"]
    return out


def fix_changes(root: Path, spec: str, fix_branch: str, into: str) -> Tuple[List[Change], str, str]:
    """The fix branch's changes against its merge-base with `into`, and the test plan before and after."""
    base = wt.git(root, "merge-base", into, fix_branch).stdout.strip()
    out = wt.git(root, "diff", "--name-status", "--no-renames", "-z", base, fix_branch).stdout
    parts = [p for p in out.split("\0") if p != ""]
    changes: List[Change] = []
    for i in range(0, len(parts) - 1, 2):
        status, path = parts[i][:1], parts[i + 1]
        changes.append(Change(path, status, _show(root, base, path) if status in "MD" else None,
                              _show(root, fix_branch, path) if status in "AM" else None))
    before, after = _plan_texts(root, spec, base, fix_branch)
    return changes, before, after


@contextlib.contextmanager
def _checkout(root: Path, rev: str):
    tmp = Path(tempfile.mkdtemp(prefix="ck-testfix-"))
    co = tmp / "co"
    try:
        wt.git(root, "worktree", "add", "--detach", str(co), rev)
        yield tmp, co
    finally:
        wt.git(root, "worktree", "remove", "--force", str(co), check=False)
        wt.git(root, "worktree", "prune", check=False)
        shutil.rmtree(tmp, ignore_errors=True)


def _read_outcomes(path: Path) -> Dict[str, str]:
    """node -> passed | skipped | xfailed | xpassed (failures are read from the gate report instead)."""
    status: Dict[str, str] = {}
    if not path.is_file():
        return status
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        node, when, outcome, xfail = obj.get("node"), obj.get("when"), obj.get("outcome"), obj.get("xfail")
        if not node:
            continue
        if outcome == "skipped":
            status[node] = "xfailed" if xfail else "skipped"
        elif outcome == "passed" and when == "call" and status.get(node) is None:
            status[node] = "xpassed" if xfail else "passed"
    return status


def _context(co: Path, spec: str, config: Mapping[str, Any], changed: Sequence[Change]):
    from . import gate
    from .failures import ProjectContext
    from .graph import build_graph

    spec_dir = next((r / spec for r in spec_roots(co) if (r / spec / "tasks.md").exists()), None)
    empty = ProjectContext(frozenset(), frozenset(), frozenset(), frozenset(), {})
    if spec_dir is None:
        return empty
    try:
        graph = build_graph(co, spec_dir)
        ctx = gate._project_context(co, graph, dict(config), SimpleNamespace(num="-"))
    except Exception:
        return empty
    paths = {c.path for c in changed if c.status != "D"}
    sources = {p: (co / p).read_text(errors="replace") for p in paths if (co / p).is_file() and _is_test_path(p, config)}
    return dataclasses.replace(ctx, test_paths=frozenset(paths), test_sources=sources)


def _run_proof(co: Path, tmp: Path, spec: str, nodes: Sequence[str], config: Mapping[str, Any],
               changed: Sequence[Change]) -> List[ProofOutcome]:
    from . import gate
    from .failures import classify_not_built

    outcomes_file = tmp / "outcomes.jsonl"
    args = []
    only: List[str] = []
    for node in nodes:
        arg = gate._node_arg(node)
        args.append(arg or node.split("::", 1)[0])
        if arg is None:
            only.append(node)
    command = " ".join([shlex.quote(sys.executable), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        f"--rootdir={shlex.quote(str(co))}"] + [shlex.quote(a) for a in dict.fromkeys(args)])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(PLUGIN_DIR), env.get("PYTHONPATH", "")) if p)
    env["PYTEST_ADDOPTS"] = " ".join(p for p in (f"-p {OUTCOMES_PLUGIN}", env.get("PYTEST_ADDOPTS", "")) if p)
    env["CK_TESTFIX_OUTCOMES"] = str(outcomes_file)
    run = gate._run_command(command, co, PROOF_TIMEOUT, env, tmp, collect=True, only=only or None)
    status = _read_outcomes(outcomes_file)
    ctx = _context(co, spec, config, changed)
    result: List[ProofOutcome] = []
    for node in nodes:
        file = node.split("::", 1)[0]
        fails = [f for f in run.failures if f.node == node
                 or (f.node is None and f.file == file) or (f.phase == "collect" and f.file == file)]
        if fails:
            classes = {classify_not_built(f, ctx) for f in fails}
            cls = "not-built" if classes == {"not-built"} else "bad" if "bad" in classes else "unclear"
            result.append(ProofOutcome(node, False, cls))
        elif status.get(node) == "passed" and node in run.collected:
            result.append(ProofOutcome(node, True, None))
        elif status.get(node) in ("skipped", "xfailed", "xpassed"):
            result.append(ProofOutcome(node, False, status[node]))
        else:
            result.append(ProofOutcome(node, False, "missing"))
    return result


def prove(root: Path, spec: str, fix_id: str, blocked: Optional[str], held: Sequence[str], nodes: Sequence[str],
          into: Optional[str] = None) -> ProveResult:
    """Scope first; only an in-scope fix gets a proof run, on the blocked task's branch with the fix merged, or
    (cross-test fix, 4.11) on the integration branch with the fix merged."""
    root = Path(root)
    fix_branch = wt.task_branch(spec, fix_id)
    into = into or wt.integration_branch(spec)
    for branch in (fix_branch, into):
        if not wt.branch_exists(root, branch):
            raise TestFixError(f"branch {branch} does not exist")
    config = load_config(wt.checkout_of(root, into) or root)
    changes, plan_before, plan_after = fix_changes(root, spec, fix_branch, into)
    scope = scope_decision(changes, plan_before, plan_after, scope_config(config))
    changed_files = {c.path for c in changes}
    changed_tests = tuple(n for n in nodes if n.split("::", 1)[0] in changed_files)
    against = f"blocked:{blocked}" if blocked else "integration"
    if not scope.in_scope:
        return ProveResult(scope, None, changed_tests, against)

    def verdict(proved: bool, reason: str, outcomes: Tuple[ProofOutcome, ...] = ()) -> ProveResult:
        return ProveResult(scope, ProofVerdict(proved, against, outcomes, reason), changed_tests, against)

    base_branch = wt.task_branch(spec, blocked) if blocked else into
    if not wt.branch_exists(root, base_branch):
        return verdict(False, f"branch {base_branch} does not exist")
    with _checkout(root, base_branch) as (tmp, co):
        merged = _git(co, "merge", "--no-edit", "--no-ff", fix_branch, check=False)
        if merged.returncode != 0:
            _git(co, "merge", "--abort", check=False)
            return verdict(False, f"the fix does not merge into {base_branch}: "
                                  f"{(merged.stdout + merged.stderr).strip()[-300:]}")
        outcomes = _run_proof(co, tmp, spec, list(nodes), scope_config(config), changes)
    decided = proof_decision(outcomes)
    return ProveResult(scope, dataclasses.replace(decided, against=against), changed_tests, against)
