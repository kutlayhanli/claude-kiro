"""P-17, P-18, P-19, P-35, P-36: properties of the protected-file decision, landing tokens and the audit.

- P-17 calls the decision seam `protect.ref_update_decision` (no server, no git).
- P-35 calls `protect.approval_decision`; P-36 calls `protect.audit_decision`.
- P-18 and P-19 are Hypothesis stateful machines on real git and the real ck CLI (`hooks_repo`).

Hypothesis settings are deterministic: derandomized, no example database, no per-example deadline.
Project imports happen inside the test bodies, so this file collects before `claude_kiro.protect` exists.
"""
from __future__ import annotations

import os
import pty
import re
import select
import shutil
import signal
import sys
import tempfile
import time
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from hypothesis import HealthCheck, settings, strategies as st, given
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test

import unattended_helpers as h

PROPERTIES = {
    "P-17": "test_tc175_p17_ref_update_decision_refuses_exactly_protected_not_allowed",
    "P-18": "test_tc175_p18_integrate_ref_only_moves_to_allowed_or_landed_commits",
    "P-19": "test_tc175_p19_audit_drift_equals_the_oracle",
    "P-35": "test_tc175_p35_approval_decision_truth_table",
    "P-36": "test_tc175_p36_unreadable_approved_state_is_never_clean",
}

PURE = dict(max_examples=200, derandomize=True, database=None, deadline=None, suppress_health_check=list(HealthCheck))

# --------------------------------------------------------------------------- the oracle for "protected"

# Written out here, independently of worktrees.PROTECTED_PATHS (requirements 9.2: the protected set).
PROTECTED_PATTERNS = (
    ".claude/**", "**/.claude/**", ".mcp.json", "**/.mcp.json",
    ".githooks/**", ".husky/**", ".git-hooks/**", ".pre-commit-config.yaml", ".envrc",
    "specs/ck.json",
)


def git_unquote(p: str) -> str:
    """The path a git-quoted name (`"\\303\\244.md"`) denotes; any other string is returned unchanged."""
    if not (len(p) >= 2 and p[0] == '"' and p[-1] == '"'):
        return p
    inner, out, i = p[1:-1], bytearray(), 0
    simple = {"n": 10, "t": 9, '"': 34, "\\": 92}
    while i < len(inner):
        c = inner[i]
        if c == "\\" and i + 1 < len(inner):
            if re.fullmatch(r"[0-3][0-7]{2}", inner[i + 1:i + 4]):
                out.append(int(inner[i + 1:i + 4], 8))
                i += 4
                continue
            if inner[i + 1] in simple:
                out.append(simple[inner[i + 1]])
                i += 2
                continue
        out += c.encode("utf-8", "replace")
        i += 1
    return bytes(out).decode("utf-8", "replace")


def quote(p: str) -> str:
    """git's C-style quoting of a path with non-ASCII bytes."""
    body = "".join(c if ord(c) < 128 else "".join(f"\\{b:03o}" for b in c.encode()) for c in p)
    return f'"{body}"'


def glob(path: str, pattern: str) -> bool:
    return fnmatchcase(path, pattern) or (pattern.startswith("**/") and fnmatchcase(path, pattern[3:]))


def is_protected(path: str) -> bool:
    n = git_unquote(path)
    return any(glob(n, p) for p in PROTECTED_PATTERNS)


def is_allowed(path: str, allow) -> bool:
    n = git_unquote(path)
    return any(glob(n, a) for a in allow)


def refused_oracle(ref: str, changed, allow) -> Set[str]:
    """What the decision must refuse: the (unquoted) protected, not allowed changed paths of an integrate ref."""
    if not ref.startswith("refs/heads/integrate/"):
        return set()
    return {git_unquote(p) for p in changed if is_protected(p) and not is_allowed(p, allow)}


def test_tc175_p17_oracle_matches_the_listed_paths():
    """The oracle itself, on the rows' own examples (a wrong oracle would make every property below meaningless)."""
    for p in (".claude/x", "a/.claude/x", ".mcp.json", "a/.mcp.json", ".githooks/x", ".husky/x", ".git-hooks/x",
              ".pre-commit-config.yaml", ".envrc", "specs/ck.json", quote(".claude/ä.md")):
        assert is_protected(p), p
    for p in ("claude/x", ".claude-notes/x", "specs/ck.json.bak", "specs/demo/ck.json", "x.mcp.json",
              ".envrc.example", "src/a.py", quote("src/ä.py")):
        assert not is_protected(p), p
    assert git_unquote('"\\303\\244"') == "ä"


# --------------------------------------------------------------------------- P-17

INTEGRATE_REFS = ["refs/heads/integrate/demo", "refs/heads/integrate/a/b", "refs/heads/integrate/équity"]
OTHER_REFS = ["refs/heads/integrate", "refs/heads/integrated/x", "refs/heads/feat/integrate/x",
              "refs/remotes/origin/integrate/x", "refs/tags/integrate/x", "HEAD", "refs/heads/main",
              "refs/heads/feature", "refs/heads/Integrate/demo", "refs/heads/integrate"]
PROTECTED_BASES = [".claude/x", "a/.claude/x", ".mcp.json", "a/.mcp.json", ".githooks/x", ".husky/x",
                   ".git-hooks/x", ".pre-commit-config.yaml", ".envrc", "specs/ck.json"]
NEAR_MISSES = ["claude/x", ".claude-notes/x", "specs/ck.json.bak", "specs/demo/ck.json", "x.mcp.json",
               ".envrc.example"]
ALLOW_LISTS = [[], [".claude/commands/**"], [".claude/**"], ["**/.mcp.json"],
               [".claude/commands/**", "**/.mcp.json"], [".claude/**", ".claude/commands/**", "**/.mcp.json"]]
ALL_ALLOW_GLOBS = [".claude/commands/**", ".claude/**", "**/.mcp.json"]


def surface_forms(base: str) -> List[str]:
    """The path in its plain, non-ASCII, space-containing and git-quoted forms."""
    head, sep, tail = base.rpartition("/")
    nonascii = f"{head}{sep}ä{tail}" if sep else f"ä{tail}"
    spaced = f"{head}{sep}a b{tail}" if sep else f"a b{tail}"
    deep_ascii = f"{head}{sep}sub dir/ä.md" if sep else base
    return [base, nonascii, spaced, quote(nonascii), quote(deep_ascii), quote(base)]


ALL_PATHS = sorted({f for b in PROTECTED_BASES + NEAR_MISSES for f in surface_forms(b)}
                   | {".claude/commands/x.md", ".claude/commands/ä.md", "src/a.py", "shared.txt", "a/.mcp.json"})
random_paths = st.builds(
    lambda parts: "/".join(parts),
    st.lists(st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters='"\\\n\0/'),
                     min_size=1, max_size=8), min_size=1, max_size=3))
changed_paths = st.lists(st.one_of(st.sampled_from(ALL_PATHS), random_paths), max_size=8)
refs = st.one_of(st.sampled_from(INTEGRATE_REFS), st.sampled_from(OTHER_REFS))
allow_lists = st.one_of(st.sampled_from(ALLOW_LISTS),
                        st.lists(st.sampled_from(ALL_ALLOW_GLOBS), max_size=3, unique=True))

OLD, NEW = "1" * 40, "2" * 40


def decide(ref: str, changed, allow):
    """The decision seam on an ordinary update (not a create or delete), with no token and no approved state."""
    from claude_kiro import protect

    update = protect.RefUpdate(ref=ref, old=OLD, new=NEW, new_tree="3" * 40, new_parents=(OLD,),
                               ancestor_of_old=False)
    # The caller hands over the protected changed paths; a path the filter lets through is decided by the allow-list.
    protected = [p for p in changed if is_protected(p)]
    return protect.ref_update_decision(update, protected, list(allow), None, None)


def refused_of(decision) -> Set[str]:
    return {git_unquote(f) for f in decision.files}


@settings(**PURE)
@given(refs, changed_paths, allow_lists, st.sampled_from(ALL_ALLOW_GLOBS), st.sampled_from(ALL_PATHS))
def test_tc175_p17_ref_update_decision_refuses_exactly_protected_not_allowed(ref, changed, allow, extra_glob, extra_path):
    d = decide(ref, changed, allow)
    want = refused_oracle(ref, changed, allow)
    assert refused_of(d) == want, (ref, changed, allow)
    assert d.allow is (not want), (ref, changed, allow, d)
    if not ref.startswith("refs/heads/integrate/"):
        assert d.allow and not d.files  # 9.4: any other ref is allowed, whatever it carries
    if want:
        assert d.message  # complement: a refusal explains itself
    # Complement: a protected path that is not allowed is always refused, and a refusal is never empty.
    for p in changed:
        if ref.startswith("refs/heads/integrate/") and is_protected(p) and not is_allowed(p, allow):
            assert not d.allow and git_unquote(p) in refused_of(d), (ref, p, allow)
    # Metamorphic: adding a changed path never turns a refusal into a pass.
    more = decide(ref, [*changed, extra_path], allow)
    if not d.allow:
        assert not more.allow
    assert refused_of(d) <= refused_of(more)
    # Metamorphic: adding an allow glob only removes the paths it matches.
    wider = decide(ref, changed, [*allow, extra_glob])
    assert refused_of(wider) <= refused_of(d)
    for removed in refused_of(d) - refused_of(wider):
        assert glob(removed, extra_glob), (removed, extra_glob)
    # The empty allow-list refuses at least what any allow-list refuses.
    assert refused_of(decide(ref, changed, [])) >= refused_of(d)


# --------------------------------------------------------------------------- P-35

LOOKALIKES = ["а", "ο", "е", "​", "Ａ", "0", "O", "l", "1"]


@st.composite
def approval_cases(draw):
    expected = draw(st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=6,
                            max_size=6))
    kind = draw(st.sampled_from([
        "exact", "exact-nl", "exact-crlf", "exact-two-nl", "lead-space", "trail-space", "tab", "swapcase",
        "one-char", "prefix", "twice", "yes", "empty", "newline-only", "lookalike", "lower", "upper", "extra-char",
        "leading-nl", "random"]))
    pos = draw(st.integers(0, 5))
    if kind == "exact":
        typed = expected
    elif kind == "exact-nl":
        typed = expected + "\n"
    elif kind == "exact-crlf":
        typed = expected + "\r\n"
    elif kind == "exact-two-nl":
        typed = expected + "\n\n"
    elif kind == "lead-space":
        typed = " " + expected
    elif kind == "trail-space":
        typed = expected + " "
    elif kind == "tab":
        typed = expected + "\t"
    elif kind == "swapcase":
        typed = expected.swapcase()
    elif kind == "one-char":
        c = "z" if expected[pos] != "z" else "y"
        typed = expected[:pos] + c + expected[pos + 1:]
    elif kind == "prefix":
        typed = expected[:draw(st.integers(0, 5))]
    elif kind == "twice":
        typed = expected * 2
    elif kind == "yes":
        typed = draw(st.sampled_from(["yes", "y", "Yes", "YES", "yes\n"]))
    elif kind == "empty":
        typed = ""
    elif kind == "newline-only":
        typed = "\n"
    elif kind == "lookalike":
        typed = expected[:pos] + draw(st.sampled_from(LOOKALIKES)) + expected[pos + 1:]
    elif kind == "lower":
        typed = expected.lower()
    elif kind == "upper":
        typed = expected.upper()
    elif kind == "extra-char":
        typed = expected + draw(st.sampled_from(["x", "0", "а"]))
    elif kind == "leading-nl":
        typed = "\n" + expected
    else:
        typed = draw(st.text(max_size=10))
    return draw(st.booleans()), draw(st.booleans()), typed, expected


def approval_oracle(stdin_tty: bool, stdout_tty: bool, typed: str, expected: str) -> bool:
    stripped = typed[:-1] if typed.endswith("\n") else typed
    return stdin_tty and stdout_tty and stripped == expected


@settings(**PURE)
@given(approval_cases())
def test_tc175_p35_approval_decision_truth_table(case):
    from claude_kiro.protect import approval_decision

    stdin_tty, stdout_tty, typed, expected = case
    got = approval_decision(stdin_tty, stdout_tty, typed, expected)
    assert got is approval_oracle(stdin_tty, stdout_tty, typed, expected), case
    if not (stdin_tty and stdout_tty):  # complement: the exact code on a pipe never approves
        assert approval_decision(stdin_tty, stdout_tty, expected, expected) is False
        assert approval_decision(stdin_tty, stdout_tty, expected + "\n", expected) is False
    else:
        assert approval_decision(True, True, expected, expected) is True
        assert approval_decision(True, True, expected + "\n", expected) is True


# --------------------------------------------------------------------------- P-36

REF = "refs/heads/integrate/demo"
OID = {"a": "a" * 40, "b": "b" * 40, "c": "c" * 40}
DRIFT_PATHS = [".claude/x", ".mcp.json", ".githooks/pre-commit", ".envrc", "specs/ck.json", ".claude/ä.md"]
BAD_APPROVED = ["missing", "corrupt-empty", "corrupt-files", "corrupt-text", "corrupt-number", "corrupt-bytes"]


@st.composite
def audit_cases(draw):
    kind = draw(st.sampled_from(["valid", "valid", "valid", *BAD_APPROVED]))
    approved = {p: draw(st.sampled_from(sorted(OID))) for p in draw(st.lists(st.sampled_from(DRIFT_PATHS), unique=True, max_size=4))}
    if draw(st.booleans()):  # current starts from the approved state, then drifts
        current = dict(approved)
        for p in draw(st.lists(st.sampled_from(DRIFT_PATHS), unique=True, max_size=3)):
            change = draw(st.sampled_from(["add", "change", "delete"]))
            if change == "delete":
                current.pop(p, None)
            else:
                current[p] = draw(st.sampled_from(sorted(OID)))
    else:
        current = {p: draw(st.sampled_from(sorted(OID))) for p in draw(st.lists(st.sampled_from(DRIFT_PATHS), unique=True, max_size=4))}
    hooks = tuple(draw(st.booleans()) for _ in range(3))  # installed, active, intact
    fixture_drift = draw(st.lists(st.sampled_from(["tests/fixtures/a.json", "tests/fixtures/b.json"]), unique=True, max_size=2))
    tampered = draw(st.booleans())
    return kind, approved, current, hooks, fixture_drift, tampered


def drift_oracle(approved: Dict[str, str], current: Dict[str, str]) -> Set[Tuple[str, str]]:
    out = set()
    for p in set(approved) | set(current):
        a, c = approved.get(p), current.get(p)
        if a is None and c is not None:
            out.add((p, "added"))
        elif a is not None and c is None:
            out.add((p, "deleted"))
        elif a != c:
            out.add((p, "changed"))
    return out


def _entry_path(e) -> str:
    return e["file"] if "file" in e else e["path"]


@settings(**PURE)
@given(audit_cases())
def test_tc175_p36_unreadable_approved_state_is_never_clean(case):
    from claude_kiro import protect

    kind, approved_files, current, (installed, active, intact), fixture_drift, tampered = case
    bad = {
        "missing": None, "corrupt-empty": {}, "corrupt-files": {"ref": REF, "commit": "d" * 40, "files": 3},
        "corrupt-text": "not a state", "corrupt-number": 42, "corrupt-bytes": b"\x00\xff",
    }
    approved = (protect.ApprovedState(ref=REF, commit="d" * 40, files=approved_files, approvals=())
                if kind == "valid" else bad[kind])
    hooks = protect.HookStatus(installed=installed, active=active, intact=intact, chained_to=None, problems=())
    fixtures = {"new_locks": [], "drift": [{"file": f, "task": "3", "change": "changed"} for f in fixture_drift],
                "handcrafted": [], "lock_tampered": tampered, "unlabeled": []}
    report = protect.audit_decision(approved, current, [], hooks, fixtures)
    if kind != "valid":
        # 10.5: a state that cannot be trusted is incomplete, and an incomplete audit is never clean.
        assert report.complete is False and report.clean is False, (case, report)
        return
    want = drift_oracle(approved_files, current)
    assert report.complete is True, (case, report)
    assert {(_entry_path(e), e["change"]) for e in report.drift} == want, (case, report.drift)
    assert len(report.drift) == len(want)
    expect_clean = not want and installed and active and intact and not fixture_drift and not tampered
    assert report.clean is expect_clean, (case, report)


# --------------------------------------------------------------------------- real-git helpers

ALLOW_JSON = '{\n  "land": {\n    "allow_paths": [".claude/commands/**"]\n  }\n}\n'
NO_HOOKS = ("-c", "core.hooksPath=/dev/null")


def _zsplit(text: str) -> List[str]:
    return [x for x in text.split("\0") if x]


def changed_between(root: Path, a: str, b: str) -> List[str]:
    out = h.raw(root, "-c", "core.quotepath=off", "diff", "--name-only", "--no-renames", "-z", a, b)
    return _zsplit(out.stdout)


def allow_of(root: Path, commit: str) -> List[str]:
    """`land.allow_paths` of the OLD commit's specs/ck.json: a merge never allows itself."""
    import json

    show = h.raw(root, "show", f"{commit}:specs/ck.json")
    if show.returncode != 0:
        return []
    try:
        cfg = json.loads(show.stdout)
    except ValueError:
        return []
    allow = (cfg.get("land") or {}).get("allow_paths")
    return [a for a in allow if isinstance(a, str)] if isinstance(allow, list) else []


def is_ancestor(root: Path, a: str, b: str) -> bool:
    return h.raw(root, "merge-base", "--is-ancestor", a, b).returncode == 0


def commit_without_hooks(iw: Path, message: str) -> None:
    """A write that goes straight into the ref (no git hook runs): the bypass the audit exists to catch."""
    out = h.raw(iw, *NO_HOOKS, "commit", "-q", "-m", message)
    assert out.returncode == 0, out.stderr


def setup_repo() -> Tuple[Path, Path, Path]:
    """(tmp dir, project root, integration worktree), with the `.claude/commands/**` allow-list on `integrate/demo`."""
    tmp = Path(tempfile.mkdtemp(prefix="ck-p18-"))
    root = h.hooks_repo(tmp)
    iw = h.integration_worktree(root)
    return tmp, root, iw


def add_allow_list(root: Path, iw: Path) -> None:
    (iw / "specs").mkdir(exist_ok=True)
    (iw / "specs" / "ck.json").write_text(ALLOW_JSON)
    h.git(iw, "add", "specs/ck.json")
    commit_without_hooks(iw, "baseline: allow-list")


def resync(iw: Path) -> None:
    """Make the integration worktree match its (possibly moved) branch; never moves the ref."""
    h.raw(iw, "merge", "--abort")
    h.raw(iw, "reset", "--hard", "HEAD")


def landed(root: Path, n: str) -> Tuple[int, dict, str]:
    return h.ck_json(root, "worktree", "land", "demo", n, "--into", "integrate/demo", "--keep", "--json")


def extract_code(text: str) -> Optional[str]:
    """The 6-character code the approval command prints (the line that mentions the code)."""
    lines = [ln for ln in text.replace("\r", "").splitlines() if re.search("code", ln, re.I)]
    for line in reversed(lines):
        tokens = re.findall(r"(?<![A-Za-z0-9])[A-Za-z0-9]{6}(?![A-Za-z0-9])", line)
        mixed = [t for t in tokens if not t.isalpha() or not t.islower()]
        if mixed:
            return mixed[-1]
        if tokens:
            return tokens[-1]
    return None


def approve_through_pty(root: Path, task: str, timeout: float = 120) -> Tuple[int, str]:
    """`ck worktree approve demo TASK` on a pseudo-terminal, typing the code it prints (TC-84b)."""
    argv = [sys.executable, "-m", "claude_kiro.cli", "worktree", "approve", "demo", task, "--into", "integrate/demo"]
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child
        os.chdir(root)
        os.execvpe(argv[0], argv, h._ck_env(root, None))
    text, typed, deadline = "", False, time.monotonic() + timeout
    status = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 1.0)
            if ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                text += data.decode("utf-8", "replace")
                code = extract_code(text)
                if code and not typed:
                    time.sleep(0.2)
                    os.write(fd, (code + "\n").encode())
                    typed = True
        _, status = os.waitpid(pid, 0)
    finally:
        if status is None:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass
    assert typed, f"the approval command printed no code to type:\n{text}"
    return os.waitstatus_to_exitcode(status), text


# --------------------------------------------------------------------------- P-18

CANDIDATE_BRANCHES = {"protected": ["1", "5"], "allowed": ["2", "4"], "clean": ["3"]}


class LandingMachine(RuleBasedStateMachine):
    """`integrate/demo` only moves to commits whose protected changes are allowed, or that ck itself landed."""

    def __init__(self):
        super().__init__()
        self.tmp, self.root, self.iw = setup_repo()
        add_allow_list(self.root, self.iw)
        self.head = h.ref(self.root, "integrate/demo")
        self.prev = self.head
        self.valid = {self.head}
        self.snaps: List[Tuple[object, str]] = []
        self._snap()

    def teardown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- bookkeeping
    def _snap(self):
        self.snaps.append((h.snapshot_ck_state(self.root), self.head))
        del self.snaps[:-12]

    def _observe(self, ck_landed: bool = False, restored: bool = False):
        new = h.ref(self.root, "integrate/demo")
        if new != self.head:
            if not (ck_landed or restored or new in self.valid):
                refused = [p for p in changed_between(self.root, self.head, new)
                           if is_protected(p) and not is_allowed(p, allow_of(self.root, self.head))]
                assert not refused, f"integrate/demo moved {self.head[:8]} -> {new[:8]} carrying {refused}"
            self.valid.add(new)
            self.prev, self.head = self.head, new
        resync(self.iw)
        self._snap()

    def _candidate(self, branch_sha: str) -> Optional[str]:
        """A commit whose change from the head is exactly the branch's change (a merge commit, made in memory)."""
        mt = h.raw(self.root, "merge-tree", "--write-tree", self.head, branch_sha)
        if mt.returncode != 0:
            return None
        tree = mt.stdout.splitlines()[0].strip()
        out = h.raw(self.root, "commit-tree", tree, "-p", self.head, "-p", branch_sha, "-m", "raw candidate")
        return out.stdout.strip() if out.returncode == 0 else None

    def _land(self, n: str):
        branch = h.ref(self.root, f"feat/demo-task-{n}")
        if not branch or is_ancestor(self.root, branch, self.head):
            return
        code, obj, err = landed(self.root, n)
        states = [t["state"] for t in (obj or {}).get("tasks", [])]
        # A landing ck is willing to make always succeeds, with hooks installed (9.7).
        assert code == 0 and states == ["MERGED"], (n, code, obj, err)
        assert h.ref(self.root, "integrate/demo") != self.head
        self._observe(ck_landed=True)

    # ---- rules
    @rule(n=st.sampled_from(["2", "4"]))
    def land_allowed(self, n):
        self._land(n)

    @rule()
    def land_clean(self):
        self._land("3")

    @rule(kind=st.sampled_from(["update-ref", "merge", "reset"]),
          dest=st.sampled_from(["protected", "allowed", "clean", "previous"]), pick=st.integers(0, 1),
          with_token=st.booleans())
    def raw(self, kind, dest, pick, with_token):
        if dest == "previous":
            sha, branch = self.prev, None
        else:
            options = CANDIDATE_BRANCHES[dest]
            branch = f"feat/demo-task-{options[pick % len(options)]}"
            bsha = h.ref(self.root, branch)
            sha = self._candidate(bsha) if bsha else None
        if not sha:
            return
        env = {}
        if with_token:  # replaying a token id found in ck's state
            tokens = Path(h.raw(self.root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()) / "ck"
            ids = sorted(p.stem for d in ("tokens", "tokens/used") for p in (tokens / d).glob("*.json"))
            if ids:
                env["CK_LANDING_TOKEN"] = ids[pick % len(ids)]
        old_env = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            if kind == "update-ref":
                h.raw(self.root, "update-ref", "refs/heads/integrate/demo", sha)
            elif kind == "reset" or branch is None:
                h.raw(self.iw, "reset", "--hard", sha)
            else:
                h.raw(self.iw, "merge", "--no-verify", "--no-edit", branch)
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self._observe()

    @rule(k=st.integers(0, 50))
    def restore(self, k):
        snap, head = self.snaps[k % len(self.snaps)]
        h.restore_ck_state(self.root, snap)
        self._observe(restored=True)

    @rule()
    def reset_back(self):
        h.raw(self.iw, "reset", "--hard", self.prev)
        self._observe()

    @invariant()
    def ref_exists(self):
        assert h.ref(self.root, "integrate/demo")


def test_tc175_p18_integrate_ref_only_moves_to_allowed_or_landed_commits():
    run_state_machine_as_test(LandingMachine, settings=settings(
        max_examples=25, stateful_step_count=8, derandomize=True, database=None, deadline=None,
        suppress_health_check=list(HealthCheck)))


# --------------------------------------------------------------------------- P-19

BYPASS_PATHS = [".mcp.json", ".envrc", ".githooks/pre-commit", ".husky/pre-push", ".pre-commit-config.yaml",
                "sub/.claude/settings.local.json"]
TASK_FILES = {  # what each task branch of `hooks_repo` changes: task -> {path: content or None (deleted)}
    "1": {".claude/CLAUDE.md": "# changed by task 1\n"},
    "2": {".claude/commands/x.md": "x\n"},
    "4": {".claude/commands/ä.md": "umlaut\n"},
    "5": {".claude/settings.json": None},
}


class AuditMachine(RuleBasedStateMachine):
    """After any mix of approved landings and bypass writes, the audit's drift equals the oracle set."""

    def __init__(self):
        super().__init__()
        self.tmp, self.root, self.iw = setup_repo()
        # The baseline ck approved when it created the integration branch: the protected files of its head then.
        self.approved: Dict[str, Optional[str]] = {
            ".claude/settings.json": (self.iw / ".claude/settings.json").read_text()}
        add_allow_list(self.root, self.iw)  # written past the hooks: the audit must see it
        self.content: Dict[str, str] = {**self.approved, "specs/ck.json": ALLOW_JSON}
        self.counter = 0
        self.landed: Set[str] = set()

    def teardown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _apply_landing(self, task: str):
        for path, text in TASK_FILES[task].items():
            if text is None:
                self.content.pop(path, None)
                self.approved.pop(path, None)
            else:
                self.content[path] = text
                self.approved[path] = text
        self.landed.add(task)

    @rule(task=st.sampled_from(["2", "4"]))
    def allowed_land(self, task):
        if task in self.landed:
            return
        code, obj, err = landed(self.root, task)
        assert code == 0 and [t["state"] for t in obj["tasks"]] == ["MERGED"], (task, code, obj, err)
        self._apply_landing(task)

    @rule(task=st.sampled_from(["1", "5"]))
    def approval_land(self, task):
        if task in self.landed:
            return
        code, text = approve_through_pty(self.root, task)
        assert code == 0, (task, code, text)
        assert is_ancestor(self.root, h.ref(self.root, f"feat/demo-task-{task}"), h.ref(self.root, "integrate/demo"))
        self._apply_landing(task)

    @rule(path=st.sampled_from(BYPASS_PATHS), action=st.sampled_from(["write", "delete"]))
    def bypass(self, path, action):
        target = self.iw / path
        if action == "delete":
            if path not in self.content:
                return
            h.git(self.iw, "rm", "-q", "--", path)
            del self.content[path]
        else:
            self.counter += 1
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"v{self.counter}\n")
            h.git(self.iw, "add", "--", path)
            self.content[path] = f"v{self.counter}\n"
        commit_without_hooks(self.iw, f"bypass {action} {path}")

    @rule()
    def clean_commit(self):
        self.counter += 1
        note = self.iw / "notes" / f"n{self.counter}.txt"
        note.parent.mkdir(exist_ok=True)
        note.write_text("note\n")
        h.git(self.iw, "add", "--", f"notes/n{self.counter}.txt")
        out = h.raw(self.iw, "commit", "-q", "-m", "clean commit")  # hooks on: a clean commit is never refused
        assert out.returncode == 0, out.stderr

    @invariant()
    def drift_is_the_oracle(self):
        code, obj, err = h.ck_json(self.root, "worktree", "audit", "demo", "--into", "integrate/demo", "--json")
        assert isinstance(obj, dict), (code, err)
        assert obj["complete"] is True, obj
        got = {(_entry_path(e), e["change"]) for e in obj["drift"]}
        want = drift_oracle({k: v for k, v in self.approved.items() if v is not None}, self.content)
        assert got == want, f"audit drift {sorted(got)} != oracle {sorted(want)}"
        assert len(obj["drift"]) == len(want)
        assert obj["clean"] is False or not want


def test_tc175_p19_audit_drift_equals_the_oracle():
    run_state_machine_as_test(AuditMachine, settings=settings(
        max_examples=20, stateful_step_count=8, derandomize=True, database=None, deadline=None,
        suppress_health_check=list(HealthCheck)))
