"""Property P-29 (unattended-correctness, TC-175): harness name lookup.

The lookup succeeds iff the name equals a registered name exactly; otherwise the error lists every registered
name. Through the CLI, `--harness` on `ck bench run --dry-run` and `ck doctor` accepts exactly the same set.
The seam is `claude_kiro.harnesses.select`; the CLI part runs the real commands in a temp project.
"""

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

PROPERTIES = {"P-29": "test_tc175_p29_select_accepts_iff_exact"}

SEAM = dict(max_examples=200, derandomize=True, database=None, deadline=None,
            suppress_health_check=list(HealthCheck))
CLI = dict(SEAM, max_examples=25)

NEAR_MISSES = ["Claude", "CLAUDE", " claude", "claude ", "claude\n", "claude-code", "open-code", "opencode2",
               "OpenCode", "dsh", "Dsh", "", " ", "claude opencode", "claud", "opencod", "сlaude"]


def _registry():
    from claude_kiro import harnesses

    reg = harnesses.registered()
    if not isinstance(reg, Mapping):
        reg = {getattr(h, "name", h): h for h in reg}
    return dict(reg)


def _names():
    return list(_registry())


def _case_variant(name, flips):
    return "".join(c.swapcase() if f else c for c, f in zip(name, flips + [False] * len(name)))


@st.composite
def harness_names(draw):
    registered = _names()
    kind = draw(st.sampled_from(["exact", "near", "case", "pad", "text", "suffix"]))
    if kind == "exact":
        return draw(st.sampled_from(registered))
    if kind == "near":
        return draw(st.sampled_from(NEAR_MISSES))
    base = draw(st.sampled_from(registered))
    if kind == "case":
        return _case_variant(base, draw(st.lists(st.booleans(), min_size=len(base), max_size=len(base))))
    if kind == "pad":
        return draw(st.sampled_from([" ", "\t", "\n"])) * draw(st.integers(0, 2)) + base + \
            draw(st.sampled_from([" ", "\t", "\n"])) * draw(st.integers(0, 2))
    if kind == "suffix":
        return base + draw(st.text(min_size=0, max_size=3))
    return draw(st.text(max_size=12))


def test_tc175_p29_registry_has_claude_and_opencode():
    names = _names()
    assert "claude" in names and "opencode" in names


@settings(**SEAM)
@given(harness_names())
def test_tc175_p29_select_accepts_iff_exact(name):
    from claude_kiro import harnesses

    reg = _registry()
    if name in reg:
        h = harnesses.select(name, reg)
        assert h is reg[name]
        return
    try:
        harnesses.select(name, reg)
    except harnesses.HarnessError as e:
        msg = str(e)
        for n in reg:
            assert n in msg, (name, msg)
    else:
        raise AssertionError(f"select accepted unregistered name {name!r}")


_BENCH = {}


def _bench_cfg():
    if "cfg" not in _BENCH:
        from bench_helpers import bench_setup

        tmp = tempfile.mkdtemp(prefix="p29-bench-")
        _BENCH["cfg"] = bench_setup(Path(tmp))
    return _BENCH["cfg"]


def _check_cli(name, out, names):
    if name in names:
        assert "unknown harness" not in out.output and out.exit_code != 2, (name, out.exit_code, out.output)
    else:
        assert out.exit_code == 2, (name, out.exit_code, out.output)
        for n in names:
            assert n in out.output, (name, n, out.output)


@settings(**CLI)
@given(harness_names())
def test_tc175_p29_cli_bench_dry_run_accepts_same_set(name):
    from bench_helpers import invoke

    names = _names()
    out = invoke("bench", "run", str(_bench_cfg()), "--dry-run", "--harness", name,
                 env={"CK_OPENCODE_PLUGIN": os.environ.get("CK_OPENCODE_PLUGIN", "/nonexistent-plugin")})
    _check_cli(name, out, names)


@settings(**CLI)
@given(harness_names())
def test_tc175_p29_cli_doctor_accepts_same_set(name):
    from bench_helpers import invoke

    names = _names()
    old = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="p29-doctor-") as tmp:
        os.chdir(tmp)
        try:
            out = invoke("doctor", "--harness", name)
        finally:
            os.chdir(old)
    _check_cli(name, out, names)
