"""P-2: foreign (non-ck) workflows stay out of every stored file and every printed byte, through the real CLI."""
from __future__ import annotations

import json

from hypothesis import given, settings, strategies as st

from stats_helpers import CONFIDENTIAL, FakeClaudeHome, all_bytes, fresh_env, stats

PROPERTIES = {"P-2": "test_foreign_workflows_never_reach_the_store_or_the_output"}

CK_NAMES = ("spec-implement", "spec-implement-iso", "spec-create")

# P-1's negatives (near misses and non-ck names from the seeded data), plus non-strings.
P1_NEGATIVES = [
    "spec-implement-v2", "Spec-Implement", " spec-create", "spec-create\n", "spec_create", "spec-plan", "spec-implement-iso2",
    "my-spec-create", "spec-implement.js", "competitor-teardown-q4", "tax-review-2026",
]
NON_STRINGS = [None, 1, ["spec-create"]]


def _forms(t: str) -> list:
    """Surface forms of one confidential string."""
    esc = t.replace("/", "\\/")
    return [
        t, t.upper(), t.lower(), t.title(), esc, json.dumps(t)[1:-1], f"see {t}, then", f"({t}).",
        f"/x/{t}/y", f"{t}/y", f"./{t}", f"../{t}", f"~/{t}", f"C:\\{t}\\y", f"integrate/{t}", f"feat/{t}-1",
        f"https://github.com/{t}/r", f"Name <{t}@b.c>", f"a+tag@{t}.c", f"wf_{t}", f"a{t}",
    ]


def confidential():
    return st.sampled_from(CONFIDENTIAL).flatmap(lambda t: st.sampled_from(_forms(t)))


# remote forms, ids, timestamps and emails from the confidential() row
EXTRA = [
    "https://github.com/o/r", "https://github.com/o/r.git", "git@github.com:o/r.git", "ssh://git@github.com/o/r", "o/r",
    "Name <a@b.c>", "a+tag@b.c", "A@B.C", "wf_0123456789abcdef", "0A1B2C3D-0000-4000-8000-0000000000AA", "a0123456789abcdef",
    "2026-10-12T09:13:27Z", "2026-10-12T09:13:27+02:00", "2026-10-12T09:13:27.123Z", "2026-10-12 09:13:27", "1792000000000",
]

foreign_names = st.one_of(
    st.sampled_from(P1_NEGATIVES), st.sampled_from(NON_STRINGS), st.sampled_from(EXTRA), confidential(),
)


def _scannable(name) -> bool:
    """Names worth scanning for: strings long enough not to collide with ordinary output, and not a ck name in disguise."""
    if not isinstance(name, str) or len(name) < 6:
        return False
    return name.strip() not in CK_NAMES


@settings(derandomize=True, max_examples=25, database=None, deadline=None)
@given(
    names=st.lists(foreign_names, min_size=1, max_size=3),
    ck_kinds=st.lists(st.sampled_from(["implement", "create"]), min_size=0, max_size=2),
)
def test_foreign_workflows_never_reach_the_store_or_the_output(names, ck_kinds):
    with fresh_env() as env:
        home = FakeClaudeHome(env.claude)
        proj = env.root / "project"
        proj.mkdir()
        session = home.session("-work-proj", "5e55a10e-1111-4000-8000-00000000000" + "1", cwd=proj)
        ck_ids = []
        for i, kind in enumerate(ck_kinds):
            rid = f"wf_c0ffee{i:02d}beef"
            ck_ids.append(rid)
            start = f"2026-10-1{i + 2}T09:00:00Z"
            (session.implement_run if kind == "implement" else session.create_run)(rid, start=start)
        foreign_ids = []
        for i, name in enumerate(names):
            fid = f"wf_f0e1d2{i:02d}c3b4"
            foreign_ids.append(fid)
            session.foreign_run(fid, name=name, start=f"2026-10-0{i + 1}T07:00:00Z")

        outputs = []
        r = stats("collect", "--all", env=env)
        outputs.append((None, r))
        refusals = []
        for fid in foreign_ids:
            r = stats("collect", "--run", fid, env=env)
            refusals.append((fid, r))
            outputs.append((fid, r))

        # foreign runs are refused
        for fid, r in refusals:
            assert r.exit_code != 0, f"foreign run {fid} was not refused: {r.output!r}"

        # ck runs are stored
        for rid in ck_ids:
            assert (env.xdg / "claude-kiro" / "runs" / f"{rid}.json").is_file(), f"ck run {rid} not stored"
        # foreign runs are not
        for fid in foreign_ids:
            assert not (env.xdg / "claude-kiro" / "runs" / f"{fid}.json").exists()

        stored = all_bytes(env.xdg / "claude-kiro").decode("utf-8", "replace")
        for fid in foreign_ids:
            assert fid not in stored, f"foreign id {fid} stored"
        for name in names:
            if _scannable(name):
                assert name not in stored, f"foreign name {name!r} stored"

        for typed, r in outputs:
            text = r.stdout + r.stderr
            for fid in foreign_ids:
                # 2.2's one exception: the --run refusal echoes back the id I typed
                rest = text.replace(fid, "") if fid == typed else text
                assert fid not in rest, f"foreign id {fid} printed (typed={typed}): {text!r}"
            for name in names:
                if _scannable(name):
                    assert name not in text, f"foreign name {name!r} printed: {text!r}"
