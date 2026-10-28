"""Properties P-8 (machine label validation) and P-9 (run.analytics boolean parsing).

Pure half calls `claude_kiro.analytics.valid_machine_label`; CLI halves run the real `ck agents set`
as a subprocess, each example in its own `fresh_env()`.
"""

import re
from pathlib import Path

from hypothesis import given, settings, strategies as st

import stats_helpers

PROPERTIES = {
    "P-8": "test_p8_valid_machine_label_matches_regex",
    "P-9": "test_p9_run_analytics_accepts_only_boolean_words",
}

PURE = dict(max_examples=200, derandomize=True, database=None, deadline=None)
CLI = dict(max_examples=25, derandomize=True, database=None, deadline=None)

LABEL_RE = re.compile(r"^[a-z0-9-]{1,32}$")

_slug = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=32)
_noise = st.text(alphabet="abcXYZ09 \t\n_./@-\uff57\u0430\u00e9", min_size=0, max_size=70)
label_strategy = st.one_of(
    _noise,
    _slug,
    st.sampled_from([0, 1, 31, 32, 33, 64]).map(lambda n: "a" * n),
    st.sampled_from(["Work", "WORK", "my laptop", "a\tb", "work\n", " work", "work ", "my_laptop", "my.laptop",
                     "a/b", "a@b", "\uff57ork", "work\u0430", "me@example.com", "/home/x/y", "~/x", "C:\\x",
                     "run-analytics", "claude-kiro", "work\n\n", "-", "0"]),
    st.sampled_from(["work", "w-1", "x" * 32]).flatmap(lambda s: st.sampled_from([s, s.upper(), s + "\n", " " + s, s + "_"])),
)


def _clean(s: str) -> str:
    """Drop characters that cannot travel through an argv (NUL, lone surrogates)."""
    return "".join(c for c in s if c != "\x00" and not 0xD800 <= ord(c) <= 0xDFFF)


@settings(**PURE)
@given(label_strategy)
def test_p8_valid_machine_label_matches_regex(s):
    from claude_kiro.analytics import valid_machine_label

    assert bool(valid_machine_label(s)) == bool(LABEL_RE.fullmatch(s))


def _snapshot(paths):
    return {p: (p.read_bytes() if p.exists() else None) for p in paths}


def _paths(env, project: Path):
    return [env.config / "config.json", project / "specs" / "ck.json"]


@settings(**CLI)
@given(label_strategy)
def test_p8_cli_rejected_label_leaves_config_unchanged(s):
    s = _clean(s)
    if LABEL_RE.fullmatch(s):
        return
    with stats_helpers.fresh_env() as env:
        project = env.root / "proj"
        (project / "specs").mkdir(parents=True)
        before = _snapshot(_paths(env, project))
        for extra in ([], ["--project"]):
            r = stats_helpers.ck_sub("agents", "set", *extra, "--", "run.machine_label", s, env=env, cwd=project)
            assert r.exit_code != 0, (s, r.output)
        assert _snapshot(_paths(env, project)) == before, s


_BOOLS = ["true", "false", "yes", "no", "on", "off"]
value_strategy = st.one_of(
    st.sampled_from(_BOOLS).flatmap(lambda b: st.sampled_from([b, b.upper(), b.capitalize(), b.swapcase()])),
    st.sampled_from(["1", "0", "y", "n", "enabled", "truee", " true", "true ", "", '"true"', "-1", "3.5", "1e3",
                     "None", "null", "yess", "of", "t", "f", "true\n"]),
    st.integers(-1000, 1000).map(str),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", max_size=12),
)


@settings(**CLI)
@given(value_strategy)
def test_p9_run_analytics_accepts_only_boolean_words(v):
    v = _clean(v)
    accepted = v.lower() in _BOOLS
    with stats_helpers.fresh_env() as env:
        project = env.root / "proj"
        (project / "specs").mkdir(parents=True)
        before = _snapshot(_paths(env, project))
        for extra in ([], ["--project"]):
            r = stats_helpers.ck_sub("agents", "set", *extra, "--", "run.analytics", v, env=env, cwd=project)
            if accepted:
                assert r.exit_code == 0, (v, r.output)
            else:
                assert r.exit_code != 0, (v, r.output)
                assert _snapshot(_paths(env, project)) == before, v
