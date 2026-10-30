"""Properties P-20..P-23 and P-27 for the Verify-file cycle rule (run-followups Story 13).

Generators write real tasks.md files into a temp dir and call `claude_kiro.lint.lint_spec`. The oracle below is a
brute-force restatement of requirements 13.1, 13.4, 13.5, 13.9 and 13.10 over the generator's own structure, not
over lint.py's helpers.
"""

import copy
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set, Tuple

import pytest
from hypothesis import HealthCheck, find, given, settings
from hypothesis import strategies as st

from claude_kiro.lint import lint_spec
from test_lint import block, write_spec

pytestmark = pytest.mark.usefixtures("no_network")

PROPERTIES = {
    "P-20": "test_p20_entries_equal_the_brute_force_oracle",
    "P-21": "test_p21_surface_forms_and_negatives",
    "P-22": "test_p22_marking_x_done_removes_exactly_its_entries",
    "P-23": "test_p23_cycles_and_dangling_ids_never_raise",
    "P-27": "test_p27_exemptions_are_exact_and_reversible",
}

SETTINGS = settings(
    derandomize=True, max_examples=200, database=None, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow]
)

FILES = ["tests/test_a.py", "tests/test_b.py", "tests/test_ab.py", "tests/unit/test_c.py"]
PREFIXES = ["uv run pytest", "python -m pytest", "pytest", "uv run pytest -q"]
FLAGS = ["-q", "-x", "-n auto", "-v"]
K_FORMS = ["-k slow", "-kslow", "-k=slow"]


# --------------------------------------------------------------------------- model

@dataclass
class Item:
    text: str
    file: Optional[str]  # the exact test file this token names, or None for a near-miss negative
    suffix: str = ""  # "" or a "::selector"

    def render(self) -> str:
        return self.text + self.suffix


@dataclass
class Cmd:
    prefix: str
    items: List[Item]
    flags: List[str] = field(default_factory=list)
    k: Optional[str] = None

    def render(self) -> str:
        parts = [self.prefix, *self.flags]
        if self.k:
            parts.append(self.k)
        parts += [i.render() for i in self.items]
        return " ".join(parts)

    def occurrences(self) -> List[Tuple[str, bool]]:
        """(file, narrowed) for every exact test-file token."""
        return [(i.file, bool(i.suffix) or self.k is not None) for i in self.items if i.file]


@dataclass
class Task:
    num: str
    track: str
    done: bool = False
    files: List[str] = field(default_factory=list)
    deps: List[str] = field(default_factory=list)
    cmds: List[Cmd] = field(default_factory=list)
    verifies: List[str] = field(default_factory=list)
    vby: List[str] = field(default_factory=list)


@dataclass
class Spec:
    tasks: List[Task]

    def get(self, num: str) -> Task:
        return next(t for t in self.tasks if t.num == num)

    def render(self) -> str:
        out = ""
        for t in self.tasks:
            files = list(t.files) + ([f"src/mod{t.num}.py"] if t.track == "impl" else [])
            out += block(
                int(t.num),
                f"task {t.num}",
                track=t.track,
                status="Done" if t.done else "Not Started",
                files=files,
                verify="` `".join(c.render() for c in t.cmds),
                vby=", ".join(f"Task {n}" for n in t.vby),
                verifies=", ".join(f"Task {n}" for n in t.verifies),
                deps=", ".join(f"Task {d} (x)" for d in t.deps) or "None",
            )
        return out


def write(spec: Spec, root: Path) -> Path:
    return write_spec(root, spec.render())


# --------------------------------------------------------------------------- oracle

def verifies_(spec: Spec, t: Task, y: Task) -> bool:
    return y.num in t.verifies or t.num in y.vby


def reaches(spec: Spec, y: str, x: str) -> bool:
    """x is an ancestor of y through known ids."""
    known = {t.num for t in spec.tasks}
    seen: Set[str] = set()
    stack = [d for d in spec.get(y).deps if d in known]
    while stack:
        d = stack.pop()
        if d in seen:
            continue
        seen.add(d)
        stack.extend(e for e in spec.get(d).deps if e in known)
    return x in seen


def oracle(spec: Spec) -> Set[Tuple[str, str, str, str]]:
    found = set()
    for x in spec.tasks:
        if x.track != "impl" or x.done:
            continue
        files = {f for c in x.cmds for f, narrowed in c.occurrences() if not narrowed}
        for f in files:
            for t in spec.tasks:
                if t.track != "test" or f not in t.files or verifies_(spec, t, x):
                    continue
                for y in spec.tasks:
                    if y.track == "impl" and y.num != x.num and verifies_(spec, t, y) and reaches(spec, y.num, x.num):
                        found.add((x.num, f, t.num, y.num))
    return found


def reported(root: Path):
    return [e for e in lint_lint(root)["verifyCycles"] if e.get("kind") == "verify-file"]


def lint_lint(root: Path):
    return lint_spec(root / "specs/sim", root)


def keys(entries) -> Set[Tuple[str, str, str, str]]:
    return {(e["task"], e["file"], e["testTask"], e["needs"]) for e in entries}


def run(spec: Spec):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write(spec, root)
        return reported(root), lint_lint(root)


# --------------------------------------------------------------------------- strategies

@st.composite
def items(draw, with_negatives=True) -> Item:
    f = draw(st.sampled_from(FILES))
    kind = draw(st.sampled_from(["bare", "bare", "dot", "colon", "class", "other"] + (["glob", "dir", "bak", "prefix", "suffix"] if with_negatives else [])))
    if kind == "bare":
        return Item(f, f)
    if kind == "dot":
        return Item("./" + f, f)
    if kind == "colon":
        return Item(f, f, "::test_x")
    if kind == "class":
        return Item(f, f, "::Klass::test_y")
    if kind == "glob":
        return Item("tests/test_*.py", None)
    if kind == "dir":
        return Item("tests/", None)
    if kind == "bak":
        return Item(f + ".bak", None)
    if kind == "prefix":
        return Item(f + "x", None)
    if kind == "suffix":
        return Item("other/" + f, None)
    return Item("tests/unrelated.py", None)


@st.composite
def cmds(draw) -> Cmd:
    its = draw(st.lists(items(), min_size=0, max_size=3))
    flags = draw(st.lists(st.sampled_from(FLAGS), max_size=2, unique=True))
    k = draw(st.one_of(st.none(), st.none(), st.sampled_from(K_FORMS)))
    return Cmd(draw(st.sampled_from(PREFIXES)), its, flags, k)


@st.composite
def specs(draw, cyclic: bool = False) -> Spec:
    n = draw(st.integers(3, 14))
    ids = [str(i) for i in range(1, n + 1)]
    tasks = []
    for i, num in enumerate(ids):
        track = draw(st.sampled_from(["impl", "impl", "test"]))
        t = Task(num, track)
        t.done = draw(st.integers(0, 5)) == 0
        if track == "test":
            t.files = draw(st.lists(st.sampled_from(FILES), max_size=2, unique=True))
        else:
            if draw(st.integers(0, 6)) == 0:
                t.files = [draw(st.sampled_from(FILES))]  # near-miss: F in an impl task's Files
            t.cmds = draw(st.lists(cmds(), max_size=3))
        lower = ids[:i]
        t.deps = draw(st.lists(st.sampled_from(lower), max_size=3, unique=True)) if lower else []
        tasks.append(t)
    if cyclic:
        for t in tasks:
            if draw(st.integers(0, 3)) == 0:
                t.deps.append(draw(st.sampled_from(ids)))  # back edges and self-loops
            if draw(st.integers(0, 6)) == 0:
                t.deps.append("99")  # unknown task
    spec = Spec(tasks)
    impls = [t for t in tasks if t.track == "impl"]
    for t in tasks:
        if t.track != "test":
            continue
        for y in impls:
            link = draw(st.sampled_from(["none", "none", "verifies", "vby", "both"]))
            if link in ("verifies", "both"):
                t.verifies.append(y.num)
            if link in ("vby", "both"):
                y.vby.append(t.num)
    return spec


# --------------------------------------------------------------------------- the generator reaches the rows' cases

def test_generator_reaches_positive_cases():
    settings_ = settings(derandomize=True, database=None, max_examples=500, deadline=None, suppress_health_check=list(HealthCheck))
    spec = find(specs(), lambda s: bool(oracle(s)), settings=settings_)
    assert oracle(spec)
    cyclic = find(specs(cyclic=True), lambda s: bool(oracle(s)) and any("99" in t.deps for t in s.tasks), settings=settings_)
    assert oracle(cyclic)


# --------------------------------------------------------------------------- P-20

@SETTINGS
@given(spec=specs())
def test_p20_entries_equal_the_brute_force_oracle(spec):
    found, _ = run(spec)
    assert keys(found) == oracle(spec)
    assert len(found) == len(keys(found)), "no duplicate entries"
    for e in found:
        assert e["chain"][0] == e["needs"] and e["chain"][-1] == e["task"]
        for a, b in zip(e["chain"], e["chain"][1:]):
            assert b in spec.get(a).deps, e


# --------------------------------------------------------------------------- P-21

def fixed_spec(cmd_list: List[Cmd], f: str) -> Spec:
    x = Task("1", "impl", cmds=cmd_list)
    t = Task("2", "test", files=[f], verifies=["3"])
    y = Task("3", "impl", deps=["1"])
    return Spec([x, t, y])


@st.composite
def p21_cases(draw):
    f = draw(st.sampled_from(FILES))
    n = draw(st.integers(1, 3))
    cmd_list = []
    for _ in range(n):
        own = draw(st.lists(items(), max_size=2))
        pre = draw(st.lists(st.sampled_from(["tests/a.py", "tests/zzz.py"]), max_size=1))
        its = [Item(p, None) for p in pre] + own
        if draw(st.booleans()):
            its.insert(draw(st.integers(0, len(its))), draw(st.sampled_from([Item(f, f), Item("./" + f, f), Item(f, f, "::test_x"), Item(f, f, "::C::test_y")])))
        cmd_list.append(Cmd(draw(st.sampled_from(PREFIXES)), its, draw(st.lists(st.sampled_from(FLAGS), max_size=3, unique=True)), draw(st.one_of(st.none(), st.sampled_from(K_FORMS)))))
    return f, cmd_list


@SETTINGS
@given(case=p21_cases())
def test_p21_surface_forms_and_negatives(case):
    f, cmd_list = case
    spec = fixed_spec(cmd_list, f)
    found, _ = run(spec)
    expected = {("1", f, "2", "3")} if any(file == f and not narrowed for c in cmd_list for file, narrowed in c.occurrences()) else set()
    # Other generated files (not owned by T) never produce entries.
    assert keys(found) == expected, [c.render() for c in cmd_list]


# --------------------------------------------------------------------------- P-22

@SETTINGS
@given(spec=specs(), pick=st.integers(0, 1000))
def test_p22_marking_x_done_removes_exactly_its_entries(spec, pick):
    if not oracle(spec):
        return
    before, _ = run(spec)
    assert keys(before) == oracle(spec)
    x = sorted(keys(before))[pick % len(before)][0]
    after_spec = copy.deepcopy(spec)
    after_spec.get(x).done = True
    after, _ = run(after_spec)
    assert keys(after) == {k for k in keys(before) if k[0] != x}
    assert keys(before) - keys(after) and all(k[0] == x for k in keys(before) - keys(after))


# --------------------------------------------------------------------------- P-23

def has_cycle(spec: Spec) -> bool:
    known = {t.num for t in spec.tasks}
    state = {}

    def visit(n: str) -> bool:
        state[n] = 1
        for d in spec.get(n).deps:
            if d not in known:
                continue
            if state.get(d) == 1 or (d not in state and visit(d)):
                return True
        state[n] = 2
        return False

    return any(n not in state and visit(n) for n in known)


@SETTINGS
@given(spec=specs(cyclic=True))
def test_p23_cycles_and_dangling_ids_never_raise(spec):
    found, full = run(spec)  # must not raise
    if has_cycle(spec):
        assert full["cycles"], "a dependency cycle is still reported"
    assert keys(found) <= oracle(spec)
    for e in found:
        assert e["chain"][0] == e["needs"] and e["chain"][-1] == e["task"]


# --------------------------------------------------------------------------- P-27

def narrow_file(spec: Spec, x: str, f: str, how: str) -> None:
    task = spec.get(x)
    new_cmds: List[Cmd] = []
    for c in task.cmds:
        if not any(i.file == f for i in c.items):
            new_cmds.append(c)
            continue
        if how == "colon":
            for i in c.items:
                if i.file == f:
                    i.suffix = "::test_x"
            new_cmds.append(c)
        else:
            rest = [i for i in c.items if i.file != f]
            mine = [i for i in c.items if i.file == f]
            for i in mine:
                i.suffix = ""
            new_cmds.append(Cmd(c.prefix, mine, [], how_k(how)))
            if rest:
                new_cmds.append(Cmd(c.prefix, rest, c.flags, c.k))
    task.cmds = new_cmds


def how_k(how: str) -> str:
    return {"k": "-k fast", "kexpr": "-kfast", "keq": "-k=fast"}[how]


@SETTINGS
@given(spec=specs(), pick=st.integers(0, 1000), edit=st.sampled_from(["verifies", "vby", "colon", "k", "kexpr", "keq"]))
def test_p27_exemptions_are_exact_and_reversible(spec, pick, edit):
    if not oracle(spec):
        return
    before, _ = run(spec)
    assert keys(before) == oracle(spec)
    x, f, t, _y = sorted(keys(before))[pick % len(before)]
    base = keys(before)
    edited = copy.deepcopy(spec)
    if edit == "verifies":
        edited.get(t).verifies.append(x)
        expected = {k for k in base if not (k[0] == x and k[2] == t)}
    elif edit == "vby":
        edited.get(x).vby.append(t)
        expected = {k for k in base if not (k[0] == x and k[2] == t)}
    else:
        narrow_file(edited, x, f, edit)
        expected = {k for k in base if not (k[0] == x and k[1] == f)}
    after, _ = run(edited)
    assert keys(after) == expected
    if edit in ("colon", "k", "kexpr", "keq"):
        restored = copy.deepcopy(edited)
        restored.get(x).cmds.append(Cmd("uv run pytest", [Item(f, f)]))
        again, _ = run(restored)
        assert keys(again) == base
