"""Properties P-20, P-21, P-22, P-23 and P-26 (unattended-correctness, TC-175): the spec graph.

- P-20 (11.4, 11.2): the invokes edges equal the longest table command that prefixes the invoked argument list,
  in every surface form, with near-misses adding none. Seams: `graph.invoked_commands` (the scan) and the
  `invokes` edges of `graph.build_graph`.
- P-21 (11.5, 11.7): two builds are equal; an incremental build after edits with mtimes restored equals a fresh
  copy's build.
- P-22 (11.8): malformed sources leave the well-formed subset's graph and are each named once in `problems`.
- P-23 (12.1, 12.2): `graph.missing_command_edges` reports exactly the missing (verified task, provider) pairs;
  `relink.plan_command_relink` + `relink.apply_command_relink` fix all of them except the ones that would close a
  cycle; a second apply is a no-op.
- P-26 (13.1): `graph.paths_overlap` / `graph.write_set_overlaps` by witness in a path universe.

Contract these tests rely on beyond design.md (the impl tasks 12, 13 and 33 own it):
- `SpecGraph.to_json()` edges are `{"kind", "from", "to"}` (Harness Contract; `src`/`dst` also accepted), node
  ids are `task:<n>` and `file:<path>`, and a command node id ends in the command path words
  (`command:mtr thesis new` or `cmd:mtr thesis new`).
- `invoked_commands(source, commands, programs)` takes command paths that include the program
  (`"mtr thesis new"`) and returns command paths from `commands` (with or without the program).
- A `MissingEdge` (a dataclass or a mapping) carries the providing task (`provider`, `provides`,
  `providing_task`, `provided_by`) and the task lacking the dependency (`task`, `verified`, `verified_task`,
  `dependent`, `lacking`).
"""

import dataclasses
import itertools
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

PROPERTIES = {
    "P-20": "test_tc175_p20_invokes_longest_table_prefix",
    "P-21": "test_tc175_p21_graph_deterministic_and_fresh",
    "P-22": "test_tc175_p22_malformed_sources_leave_the_rest",
    "P-23": "test_tc175_p23_lint_reports_exactly_missing_pairs",
    "P-26": "test_tc175_p26_overlap_by_witness",
}

SEAM = dict(max_examples=200, derandomize=True, database=None, deadline=None,
            suppress_health_check=list(HealthCheck))

PROGRAM = "mtr"
TABLE = ["mtr version", "mtr thesis new", "mtr thesis list"]


# --------------------------------------------------------------------------- project writing

def _write(root: Path, files) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)


def _block(num, title, *, track="impl", files=(), deps=(), verify="", vby=(), verifies=(), provides=()):
    lines = [f"### Task {num}: {title}", "**Status:** Not Started", f"**Track:** {track}", "**Files:**"]
    lines += [f"- `{f}` - x" for f in files]
    if verify:
        lines.append(f"**Verify:** `{verify}`")
    if vby:
        lines.append("**Verified by:** " + ", ".join(f"Task {n}" for n in vby))
    if verifies:
        lines.append("**Verifies:** " + ", ".join(f"Task {n}" for n in verifies))
    if provides:
        lines.append("**Provides:** " + ", ".join(f"`{p}`" for p in provides))
    lines.append("**Dependencies:** " + (", ".join(f"Task {d}" for d in deps) if deps else "None"))
    return "\n".join(lines) + "\n\n---\n\n"


def _tasks_md(blocks: str) -> str:
    return "# Implementation Tasks: Sim\n\n## Task Breakdown\n\n" + blocks


def _design(commands=(), cli_file="src/mtr/cli.py", with_table=True) -> str:
    text = "# Design: Sim\n\n## Components\n\n- the pieces\n"
    if with_table:
        rows = "".join(f"| `{c}` | `{cli_file}` |\n" for c in commands)
        text += "\n## CLI\n\n| Command | File |\n|---------|------|\n" + rows
    return text


def _project(root: Path, tasks_blocks: str, files, design: str) -> Path:
    spec = root / "specs" / "sim"
    spec.mkdir(parents=True, exist_ok=True)
    (spec / "design.md").write_text(design)
    (spec / "requirements.md").write_text("# Feature: Sim\n\n- 1.1 WHEN a THE SYSTEM SHALL b\n")
    (spec / "test-plan.md").write_text("# Test Plan: Sim\n")
    (spec / "tasks.md").write_text(_tasks_md(tasks_blocks))
    _write(root, files)
    return spec


def _build(root: Path):
    from claude_kiro.graph import build_graph

    return build_graph(root, root / "specs" / "sim")


def _dump(graph, root: Path) -> str:
    return json.dumps(graph.to_json(), sort_keys=True).replace(str(root), "<root>")


def _edges(graph):
    out = []
    for e in graph.to_json()["edges"]:
        out.append((e["kind"], e.get("from", e.get("src")), e.get("to", e.get("dst"))))
    return out


def _nodes(graph):
    return {n["id"] for n in graph.to_json()["nodes"]}


def _command_of(node_id: str) -> str:
    """`cmd:mtr thesis new` -> `thesis new` (program stripped, so both forms compare)."""
    return _strip_program(node_id.split(":", 1)[1] if ":" in node_id else node_id)


def _strip_program(path: str) -> str:
    path = path.strip()
    return path[len(PROGRAM) + 1:] if path.startswith(PROGRAM + " ") else path


# --------------------------------------------------------------------------- P-20

ARGVS = st.builds(
    lambda opts, path, tail: list(opts) + list(path) + list(tail),
    st.lists(st.sampled_from(["--verbose", "-v"]), max_size=2),
    st.sampled_from([["version"], ["thesis", "new"], ["thesis", "list"]]),
    st.sampled_from([[], ["--x", "1"], ["--name", "thesis"], ["--name", "new"], ["--name", "thesis", "new"],
                     ["--flag"], ["list"]]),
)

FORMS = (
    'run_cli({argv})', 'main({argv})', 'CliRunner().invoke(cli, {argv})', 'subprocess.run(["mtr", {words}])',
    'subprocess.run("mtr {joined} --x 1", shell=True)', 'shlex.split("mtr {joined}")',
    'subprocess.run(f"mtr {joined} {{opt}}", shell=True)', 'subprocess.run(["mtr", "--verbose", {words}])',
)

NEAR_MISSES = (
    'run_cli(["thesis", "newer"])', 'run_cli(["thesis-new"])', 'run_cli(["express", "--name", "thesis"])',
    'run_cli(["thesis"])', 'run_cli(["thesis", "--name", "new"])', 'assert ok, "thesis new"',
    'assert ok, "thesis new failed"', '# mtr thesis new', 'subprocess.run(["git", "thesis", "new"])',
    'subprocess.run("git thesis new", shell=True)', 'subprocess.run(["mtrx", "thesis", "new"])',
    'CliRunner().invoke(cli, ["thesis", "newer"])',
)


def _longest_prefix(argv):
    """The oracle: skip leading options, then the longest table command whose words prefix the rest."""
    words = list(argv)
    while words and words[0].startswith("-"):
        words.pop(0)
    best = None
    for cmd in TABLE:
        path = cmd.split()[1:]
        if words[:len(path)] == path and (best is None or len(path) > len(best.split())):
            best = " ".join(path)
    return best


@st.composite
def invocation_sources(draw):
    stmts, expected = [], set()
    for _ in range(draw(st.integers(1, 5))):
        if draw(st.integers(0, 3)) == 0:
            stmts.append(draw(st.sampled_from(NEAR_MISSES)))
            continue
        argv = draw(ARGVS)
        form = draw(st.sampled_from(FORMS))
        if form.startswith('subprocess.run(["mtr", "--verbose"'):
            argv = [w for w in argv if not w.startswith("-")] or ["version"]
            argv = [w for w in argv]
        words = ", ".join(json.dumps(w) for w in argv)
        stmts.append(form.format(argv=json.dumps(argv), words=words, joined=" ".join(argv)))
        found = _longest_prefix(argv)
        if found:
            expected.add(found)
    source = ("import shlex\nimport subprocess\n\nfrom click.testing import CliRunner\n\nopt = 'x'\n\n\n"
              "def test_tc1_cases():\n" + "".join(f"    {s}\n" for s in stmts) + "    pass\n")
    return source, expected


@settings(**SEAM)
@given(invocation_sources())
def test_tc175_p20_invokes_longest_table_prefix(case):
    from claude_kiro.graph import invoked_commands

    source, expected = case
    got = {_strip_program(c) for c in invoked_commands(source, TABLE, [PROGRAM])}
    assert got == expected, source

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        blocks = (_block(1, "Tests", track="test", files=["tests/test_inv.py"], verifies=[2], deps=[])
                  + _block(2, "Impl", files=["src/mtr/impl.py"], vby=[1], deps=[1]))
        _project(root, blocks, {"tests/test_inv.py": source, "src/mtr/impl.py": "x = 1\n"}, _design(TABLE))
        edges = {_command_of(dst) for kind, src, dst in _edges(_build(root))
                 if kind == "invokes" and src == "file:tests/test_inv.py"}
    assert edges == expected, source


# --------------------------------------------------------------------------- P-21 / P-22 spec generator

@st.composite
def specs(draw, max_tasks=30, max_tests=20, max_sources=10):
    ids = sorted(draw(st.sets(st.integers(1, 40), min_size=1, max_size=max_tasks)))
    n_tests = draw(st.integers(1, max_tests))
    n_sources = draw(st.integers(1, max_sources))
    test_paths = [f"tests/test_f{i}.py" for i in range(n_tests)]
    source_paths = [f"src/pkg/m{j}.py" for j in range(n_sources)]
    tracks = {i: draw(st.sampled_from(["impl", "test"])) for i in ids}
    impl_ids = [i for i in ids if tracks[i] == "impl"]
    blocks = ""
    for pos, i in enumerate(ids):
        deps = draw(st.lists(st.sampled_from(ids[:pos]), unique=True, max_size=3)) if pos else []
        if tracks[i] == "test":
            files = draw(st.lists(st.sampled_from(test_paths), unique=True, min_size=1, max_size=3))
            verifies = draw(st.lists(st.sampled_from(impl_ids), unique=True, max_size=3)) if impl_ids else []
            blocks += _block(i, f"Tests {i}", track="test", files=files, deps=deps, verifies=verifies)
        else:
            files = draw(st.lists(st.sampled_from(source_paths), unique=True, max_size=2))
            verify = ""
            if draw(st.booleans()):
                k = draw(st.integers(1, 9))
                verify = f'uv run pytest -q {draw(st.sampled_from(test_paths))} -k "tc{k}_"'
            blocks += _block(i, f"Impl {i}", files=files, deps=deps, verify=verify)
    files = {}
    for path in test_paths:
        tcs = draw(st.lists(st.integers(1, 9), unique=True, max_size=4))
        body = "".join(f"def test_tc{k}_case():\n    pass\n\n\n" for k in tcs) or "x = 1\n"
        if draw(st.booleans()):
            body += 'CMD = ["mtr", "thesis", "new"]\n'
        files[path] = body
    for path in source_paths:
        mods = draw(st.lists(st.integers(0, n_sources - 1), unique=True, max_size=2))
        files[path] = "".join(f"import pkg.m{m}\n" for m in mods) + "VALUE = 1\n"
    return blocks, files, ids


EDITS = ("add_task", "remove_task", "change_verify", "change_verify_same_size", "add_invocation", "rename_file",
         "flip_source_char", "drop_tc")


def _apply_edit(root: Path, kind: str, salt: int) -> None:
    spec = root / "specs" / "sim"
    tasks = spec / "tasks.md"
    text = tasks.read_text()
    touched = []

    def put(path: Path, new: str) -> None:
        st_ = path.stat()
        path.write_text(new)
        os.utime(path, ns=(st_.st_atime_ns, st_.st_mtime_ns))

    if kind == "add_task":
        put(tasks, text + _block(70 + salt % 20, "Added", files=[f"src/pkg/added{salt}.py"]))
    elif kind == "remove_task":
        blocks = list(re.finditer(r"^### Task \d+:.*?(?=^### Task \d|^## |\Z)", text, re.DOTALL | re.MULTILINE))
        if blocks:
            m = blocks[salt % len(blocks)]
            put(tasks, text[:m.start()] + text[m.end():])
    elif kind in ("change_verify", "change_verify_same_size"):
        m = re.search(r'-k "tc(\d)_"', text)
        if m:
            new = str((int(m.group(1)) + 1) % 10) if kind.endswith("same_size") else f"{m.group(1)}{m.group(1)}"
            put(tasks, text[:m.start(1)] + new + text[m.end(1):])
    elif kind == "add_invocation":
        files = sorted((root / "tests").glob("test_f*.py"))
        if files:
            path = files[salt % len(files)]
            put(path, path.read_text() + 'CMD2 = ["mtr", "version"]\n')
    elif kind == "rename_file":
        files = sorted((root / "tests").glob("test_f*.py")) + sorted((root / "src" / "pkg").glob("m*.py"))
        if files:
            path = files[salt % len(files)]
            st_ = path.stat()
            target = path.with_name(f"renamed_{salt}{path.suffix}")
            path.rename(target)
            os.utime(target, ns=(st_.st_atime_ns, st_.st_mtime_ns))
    elif kind == "flip_source_char":
        files = sorted((root / "src" / "pkg").glob("m*.py"))
        if files:
            path = files[salt % len(files)]
            put(path, path.read_text().replace("VALUE = 1", "VALUE = 2"))
    elif kind == "drop_tc":
        files = sorted((root / "tests").glob("test_f*.py"))
        if files:
            path = files[salt % len(files)]
            put(path, re.sub(r"def test_tc(\d)_case", r"def test_xc\1_case", path.read_text()))
    del touched


@settings(**SEAM)
@given(specs(), st.lists(st.tuples(st.sampled_from(EDITS), st.integers(0, 50)), max_size=4))
def test_tc175_p21_graph_deterministic_and_fresh(spec, edits):
    blocks, files, _ids = spec
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "a"
        root.mkdir()
        _project(root, blocks, files, _design(TABLE))
        first = _dump(_build(root), root)
        assert _dump(_build(root), root) == first  # two builds are equal
        for kind, salt in edits:
            _apply_edit(root, kind, salt)
            _build(root)  # an incremental build in between, to populate the in-process memo
        incremental = _dump(_build(root), root)
        fresh_root = Path(tmp) / "fresh"
        shutil.copytree(root, fresh_root)
        assert incremental == _dump(_build(fresh_root), fresh_root)


# --------------------------------------------------------------------------- P-22

BAD_KINDS = ("no_number", "duplicate", "dep_no_number", "syntax_error", "non_utf8", "empty_cli_table")


@settings(**SEAM)
@given(specs(max_tasks=12, max_tests=6, max_sources=4),
       st.lists(st.sampled_from(BAD_KINDS), unique=True, min_size=1, max_size=6))
def test_tc175_p22_malformed_sources_leave_the_rest(spec, bad):
    blocks, files, _ids = spec
    with tempfile.TemporaryDirectory() as tmp:
        good_root = Path(tmp) / "good"
        good_root.mkdir()
        _project(good_root, blocks, files, _design(with_table=False))
        good = _build(good_root)

        # The malformed sources are isolated: task numbers 81+, files nothing else owns.
        bad_blocks, bad_files, bad_ids = "", dict(files), set()
        task_problems = 0
        design = _design(with_table=False)
        if "no_number" in bad:
            bad_blocks += "### Task: Orphan\n**Status:** Not Started\n\n---\n\n"
            task_problems += 1
        extra = ""
        if "duplicate" in bad:
            extra += _block(81, "Dup", files=["src/dup81.py"]) + _block(81, "Dup again", files=["src/dup81.py"])
            bad_ids |= {"task:81", "file:src/dup81.py"}
            task_problems += 1
        if "dep_no_number" in bad:
            extra += ("### Task 82: Bad dep\n**Status:** Not Started\n**Track:** impl\n**Files:**\n"
                      "- `src/bad82.py` - x\n**Dependencies:** Task\n\n---\n\n")
            bad_ids |= {"task:82", "file:src/bad82.py"}
            task_problems += 1
        bad_test_paths = []
        if "syntax_error" in bad:
            bad_files["tests/test_bad_syntax.py"] = "def test_tc1_x(:\n    pass\n"
            extra += _block(83, "Bad syntax", track="test", files=["tests/test_bad_syntax.py"])
            bad_ids |= {"task:83", "file:tests/test_bad_syntax.py"}
            bad_test_paths.append("tests/test_bad_syntax.py")
        if "non_utf8" in bad:
            bad_files["tests/test_bad_bytes.py"] = b"def test_tc1_x():\n    pass\n# \xff\xfe\n"
            extra += _block(84, "Bad bytes", track="test", files=["tests/test_bad_bytes.py"])
            bad_ids |= {"task:84", "file:tests/test_bad_bytes.py"}
            bad_test_paths.append("tests/test_bad_bytes.py")
        if "empty_cli_table" in bad:
            design = _design(commands=(), with_table=True)

        bad_root = Path(tmp) / "bad"
        bad_root.mkdir()
        # The orphan header goes first, so it cannot absorb a well-formed block's fields.
        _project(bad_root, bad_blocks + blocks + extra, bad_files, design)
        graph = _build(bad_root)

    def subset(g):
        nodes = sorted(n["id"] for n in g.to_json()["nodes"] if n["id"] not in bad_ids)
        edges = sorted(e for e in _edges(g) if e[1] not in bad_ids and e[2] not in bad_ids)
        return nodes, edges

    assert subset(graph) == subset(good)

    problems = [(p.source, p.problem) for p in graph.problems]
    for path in bad_test_paths:
        assert sum(path in src for src, _ in problems) == 1, problems
    assert sum("tasks.md" in src for src, _ in problems) == task_problems, problems
    if "duplicate" in bad:
        assert sum("tasks.md" in src and re.search(r"\b81\b", src) is not None for src, _ in problems) == 1, problems
    if "dep_no_number" in bad:
        assert sum("tasks.md" in src and re.search(r"\b82\b", src) is not None for src, _ in problems) == 1, problems
    if "empty_cli_table" in bad:
        assert sum("design.md" in src for src, _ in problems) == 1, problems


# --------------------------------------------------------------------------- P-23

CMDS = [f"mtr c{i}" for i in range(1, 7)]
INVOKE_FORMS = ('subprocess.run(["mtr", "{w}"])', 'subprocess.run("mtr {w} --x 1", shell=True)',
                'shlex.split("mtr {w}")')


@st.composite
def command_specs(draw):
    ids = sorted(draw(st.sets(st.integers(1, 60), min_size=1, max_size=12)))
    deps = {}
    for pos, i in enumerate(ids):
        deps[i] = draw(st.lists(st.sampled_from(ids[:pos]), unique=True, max_size=3)) if pos else []
    provider = {c: draw(st.one_of(st.none(), st.sampled_from(ids))) for c in CMDS}
    tests = {}
    for k in range(draw(st.integers(1, 4))):
        verifies = draw(st.lists(st.sampled_from(ids), unique=True, min_size=1, max_size=3))
        invoked = draw(st.lists(st.sampled_from(CMDS), unique=True, max_size=3))
        forms = [draw(st.sampled_from(INVOKE_FORMS)) for _ in invoked]
        tests[201 + k] = (verifies, invoked, forms)
    return ids, deps, provider, tests


def _trans(deps, task):
    seen, stack = set(), list(deps.get(task, []))
    while stack:
        d = stack.pop()
        if d not in seen:
            seen.add(d)
            stack.extend(deps.get(d, []))
    return seen


def _oracle_pairs(deps, provider, tests):
    pairs = set()
    for _t, (verifies, invoked, _forms) in tests.items():
        for v in verifies:
            for cmd in invoked:
                p = provider[cmd]
                if p is not None and p != v and p not in _trans(deps, v):
                    pairs.add((str(v), str(p)))
    return pairs


def _render_command_spec(spec):
    ids, deps, provider, tests = spec
    blocks, files = "", {}
    verified_by = {i: [t for t, (vs, _c, _f) in tests.items() if i in vs] for i in ids}
    for i in ids:
        blocks += _block(i, f"Impl {i}", files=[f"src/mod{i}.py"], deps=deps[i], vby=verified_by[i],
                         provides=[c for c in CMDS if provider[c] == i])
    for t, (verifies, invoked, forms) in tests.items():
        blocks += _block(t, f"Tests {t}", track="test", files=[f"tests/test_t{t}.py"], verifies=verifies)
        body = "import shlex\nimport subprocess\n\n\ndef test_tc%d_x():\n" % t
        body += "".join("    " + f.format(w=c.split()[1]) + "\n" for c, f in zip(invoked, forms)) + "    pass\n"
        files[f"tests/test_t{t}.py"] = body
    return blocks, files


def _pair(missing):
    data = dataclasses.asdict(missing) if dataclasses.is_dataclass(missing) else dict(missing)
    def pick(*names):
        for name in names:
            if name in data and data[name] not in (None, ""):
                return str(data[name]).replace("Task ", "").replace("task:", "")
        raise AssertionError(f"MissingEdge has none of {names}: {data}")
    return (pick("task", "verified", "verified_task", "dependent", "lacking"),
            pick("provider", "provides", "providing_task", "provided_by"))


def _missing(root: Path):
    from claude_kiro.graph import missing_command_edges

    return {_pair(m) for m in missing_command_edges(_build(root))}


def _dep_map(text):
    from claude_kiro.hooks._shared.spec_parser import parse_tasks_text

    out = {}
    for t in parse_tasks_text(text):
        out[t.num] = [re.sub(r"\D", "", d) for d in t.dependencies if re.search(r"\d", d)]
    return out


def _reaches(deps, src, dst):
    return dst in _trans(deps, src)


@settings(**SEAM)
@given(command_specs())
def test_tc175_p23_lint_reports_exactly_missing_pairs(spec):
    from claude_kiro.hooks._shared.spec_parser import parse_tasks_text
    from claude_kiro.relink import apply_command_relink, plan_command_relink

    ids, deps, provider, tests = spec
    blocks, files = _render_command_spec(spec)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "r0"
        root.mkdir()
        _project(root, blocks, files, _design(CMDS))
        assert _missing(root) == _oracle_pairs(deps, provider, tests)

        text = (root / "specs" / "sim" / "tasks.md").read_text()
        plan = plan_command_relink(_build(root), {t.num: t for t in parse_tasks_text(text)})
        new_text = apply_command_relink(text, plan)

        root1 = Path(tmp) / "r1"
        shutil.copytree(root, root1)
        (root1 / "specs" / "sim" / "tasks.md").write_text(new_text)
        remaining = _missing(root1)
        before, after = _dep_map(text), _dep_map(new_text)

        # Only cycle-making pairs remain: the provider already depends on the dependent task.
        for v, p in remaining:
            assert _reaches(after, p, v), (v, p, after)
        # Nothing else changed: every new dependency is a reported provider of that task, and the graph stays acyclic.
        oracle = _oracle_pairs(deps, provider, tests)
        for task, new in after.items():
            assert set(before[task]) <= set(new)
            for dep in set(new) - set(before[task]):
                assert (task, dep) in oracle, (task, dep)
            assert task not in _trans(after, task)
        # Every pair that was fixable (no cycle in the result) was added.
        for v, p in oracle - remaining:
            assert p in after[v] or p in _trans(after, v), (v, p)

        # A second apply changes nothing.
        plan2 = plan_command_relink(_build(root1), {t.num: t for t in parse_tasks_text(new_text)})
        assert apply_command_relink(new_text, plan2) == new_text


# --------------------------------------------------------------------------- P-26

LITERALS = ["src/a.py", "src/ab.py", "src/x/y.py", "src/x/z.py", "docs/x.md", "tests/test_a.py", "README.md"]
GLOBS = ["src/*.py", "src/**", "src/?.py", "src/a?.py", "**/*.md", "docs/**", "src/x/*", "*.md", "src/x/**",
         "tests/*.py"]
DIRS = ["src/", "src/x/", "docs/"]
UNIVERSE = [
    "src/a.py", "src/ab.py", "src/b.py", "src/abc.py", "src/x/y.py", "src/x/z.py", "src/x/y/z.py", "src/x.py",
    "src/pkg/mod.py", "src/a.txt", "docs/x.md", "docs/y.md", "docs/sub/z.md", "docs/x.txt", "tests/test_a.py",
    "tests/test_b.py", "tests/sub/test_c.py", "README.md", "CHANGELOG.md", "notes.txt", "a.py", "pyproject.toml",
    "src/ab/c.py", "src/a/b.py", "x/y.md", "x/y/z.md", "src/x/a.md", "src/x/b.py", "tests/a.txt", "lib/a.py",
]


def _normalise(raw: str) -> str:
    s = raw.strip()
    if s.startswith("`"):
        s = s[1:s.index("`", 1)]
    else:
        s = s.split(" - ")[0].strip()
    if s.startswith("./"):
        s = s[2:]
    if s.endswith("/"):
        s += "**"
    return s


def _is_glob(norm: str) -> bool:
    return any(c in norm for c in "*?")


def _regex(pattern: str):
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += r".*/"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def _matches(pattern: str, path: str) -> bool:
    return _regex(pattern).match(path) is not None


def _universe(*raws):
    extra = [n for n in (_normalise(r) for rs in raws for r in rs) if not _is_glob(n)]
    return list(dict.fromkeys(UNIVERSE + extra))


def _witness(a_raws, b_raws) -> bool:
    a, b = [_normalise(r) for r in a_raws], [_normalise(r) for r in b_raws]
    return any(any(_matches(x, p) for x in a) and any(_matches(y, p) for y in b)
               for p in _universe(a_raws, b_raws))


@st.composite
def entries(draw, pool=None):
    base = draw(st.sampled_from(pool if pool is not None else LITERALS + GLOBS + DIRS))
    if draw(st.booleans()):
        base = "./" + base
    if draw(st.booleans()):
        base = f"`{base}`"
    if draw(st.booleans()):
        base += " - " + draw(st.sampled_from(["the thing", "helpers", "x - y"]))
    return base


@settings(**SEAM)
@given(st.lists(st.lists(entries(), max_size=4), min_size=2, max_size=4))
def test_tc175_p26_overlap_by_witness(sets):
    from claude_kiro.graph import paths_overlap, write_set_overlaps

    for a, b in itertools.combinations(sets, 2):
        for x, y in itertools.product(a, b):
            assert paths_overlap(x, y) == paths_overlap(y, x), (x, y)
            if _witness([x], [y]):
                assert paths_overlap(x, y), (x, y)

    owns = {f"T{k}": list(s) for k, s in enumerate(sets)}
    result = write_set_overlaps(owns)
    for k, s in enumerate(sets):
        name = f"T{k}"
        if not s:
            assert name not in result, result
        got = set(result.get(name, [])) - {name}
        for j, other in enumerate(sets):
            if j == k:
                continue
            other_name = f"T{j}"
            if _witness(s, other):
                assert other_name in got, (name, other_name, s, other)
            assert (other_name in got) == (name in set(result.get(other_name, []))), (name, other_name)
            if not s or not other:
                assert other_name not in got


@settings(**SEAM)
@given(st.lists(st.lists(entries(pool=LITERALS), max_size=4), min_size=2, max_size=4))
def test_tc175_p26_disjoint_literals_never_overlap(sets):
    from claude_kiro.graph import paths_overlap, write_set_overlaps

    result = write_set_overlaps({f"T{k}": list(s) for k, s in enumerate(sets)})
    for (k, a), (j, b) in itertools.combinations(enumerate(sets), 2):
        common = {_normalise(x) for x in a} & {_normalise(y) for y in b}
        assert (f"T{j}" in set(result.get(f"T{k}", []))) == bool(common), (a, b)
        for x, y in itertools.product(a, b):
            assert paths_overlap(x, y) == (_normalise(x) == _normalise(y)), (x, y)
