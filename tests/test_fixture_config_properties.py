"""Properties of config validators and fixture drift (unattended-correctness TC-175: P-13, P-32).

P-13 runs `config.test_track_validate` and `config.fixture_globs` over generated JSON values.
P-32 runs `fixtures.lock_decision` over generated landing events and handcraft labels.
Project modules are imported inside the test bodies so the file collects before the seams exist.
"""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

PROPERTIES = {
    "P-13": "test_tc175_p13_config_readers_accept_iff_string_or_list_of_strings",
    "P-32": "test_tc175_p32_drift_is_locked_files_changed_without_a_reasoned_label",
}

SETTINGS = dict(max_examples=200, derandomize=True, database=None, deadline=None,
                suppress_health_check=[HealthCheck.too_slow])

# --------------------------------------------------------------------------- P-13

_JSON = st.recursive(
    st.none() | st.booleans() | st.integers(-5, 5) | st.floats(allow_nan=False, allow_infinity=False)
    | st.just("") | st.text(max_size=8),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=5), inner, max_size=3),
    max_leaves=8,
)
_STRING_LISTS = st.lists(st.just("") | st.text(max_size=8), max_size=4)


def _is_str_list(value):
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def _check_reader(reader, config, key, accepted):
    from claude_kiro.config import ConfigError

    if accepted:
        assert isinstance(reader(config), list)
    else:
        try:
            reader(config)
        except ConfigError as exc:
            assert key in str(exc)
        else:
            raise AssertionError(f"{key}={config!r} was accepted")


@settings(**SETTINGS)
@given(value=_JSON | _STRING_LISTS)
def test_tc175_p13_config_readers_accept_iff_string_or_list_of_strings(value):
    from claude_kiro.config import fixture_globs, test_track_validate

    _check_reader(test_track_validate, {"test_track": {"validate": value}}, "test_track.validate",
                  isinstance(value, str) or _is_str_list(value))
    _check_reader(fixture_globs, {"fixtures": {"recorded": value}}, "fixtures.recorded", _is_str_list(value))


# --------------------------------------------------------------------------- P-32

FILES = ["tests/fixtures/a.json", "tests/fixtures/b.json", "tests/fixtures/c.json",
         "tests/other/d.json", "docs/e.txt"]
LOCKED = FILES[:3]
BASE = "0" * 64
REASONS = ["", " ", "\t\n", "captured by hand from the vendor page", "x"]

_SHA = st.sampled_from([None, BASE, "1" * 64, "2" * 64])
_EVENT = st.fixed_dictionaries({
    "changes": st.dictionaries(st.sampled_from(FILES), _SHA, min_size=1),
    "reasons": st.dictionaries(st.sampled_from(FILES), st.sampled_from(REASONS)),
})


@settings(**SETTINGS)
@given(events=st.lists(_EVENT, min_size=1, max_size=8))
def test_tc175_p32_drift_is_locked_files_changed_without_a_reasoned_label(events):
    from claude_kiro.fixtures import Label, LockEntry, lock_decision

    lock = {f: LockEntry(sha256=BASE, task="1", locked_at="2026-01-01T00:00:00Z", handcrafted=None)
            for f in LOCKED}
    reported, oracle = set(), set()
    for event in events:
        labels = {f: Label(source="handcrafted", reason=r, sha256=None, capture=None)
                  for f, r in event["reasons"].items()}
        report = lock_decision(lock, event["changes"], labels)
        reported |= {d["file"] for d in report.drift}
        oracle |= {f for f, sha in event["changes"].items()
                   if f in lock and sha != BASE
                   and not (f in labels and labels[f].reason.strip())}
    assert reported == oracle
