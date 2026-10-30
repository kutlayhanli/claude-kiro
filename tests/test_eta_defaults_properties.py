"""Property P-19: the bundled-defaults builder never leaks record-level data (run-followups, requirement 7.2).

It calls the pure seams `build_defaults` and `defaults_shape_ok` of `claude_kiro.eta.history` over records seeded with
every `CONFIDENTIAL` value. Project modules are imported inside the test body so this file always collects.
"""

import copy
import json

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from eta_strategies import NO_NETWORK
from stats_helpers import CONFIDENTIAL, seeded_local_record

pytestmark = NO_NETWORK

PROPERTIES = {"P-19": "test_p19_bundled_defaults_hold_no_record_level_data"}

SETTINGS = settings(
    derandomize=True,
    max_examples=200,
    database=None,
    deadline=None,
    suppress_health_check=list(HealthCheck),
)


def _rotate(node, shift):
    """Re-plant the CONFIDENTIAL strings: every string equal to one of them becomes another one."""
    if isinstance(node, str):
        if node in CONFIDENTIAL:
            return CONFIDENTIAL[(CONFIDENTIAL.index(node) + shift) % len(CONFIDENTIAL)]
        return node
    if isinstance(node, list):
        return [_rotate(v, shift) for v in node]
    if isinstance(node, dict):
        return {(_rotate(k, shift) if isinstance(k, str) else k): _rotate(v, shift) for k, v in node.items()}
    return node


@st.composite
def stores(draw):
    count = draw(st.integers(0, 60))
    kinds = draw(st.lists(st.sampled_from(["spec-implement", "spec-create"]), min_size=count, max_size=count))
    shifts = draw(st.lists(st.integers(0, len(CONFIDENTIAL) - 1), min_size=count, max_size=count))
    return [_rotate(copy.deepcopy(seeded_local_record(k)), s) for k, s in zip(kinds, shifts)]


def _payload(result):
    if isinstance(result, dict):
        return result
    return getattr(result, "payload", None) or getattr(result, "defaults", None)


@SETTINGS
@given(records=stores(), min_runs=st.integers(1, 40))
def test_p19_bundled_defaults_hold_no_record_level_data(records, min_runs):
    from claude_kiro.eta import history

    if len(records) < min_runs:
        try:
            result = history.build_defaults(records, min_runs=min_runs)
        except ValueError:
            return
        assert not _payload(result), "refused below min_runs: no payload may be returned"
        return
    result = history.build_defaults(records, min_runs=min_runs)
    payload = _payload(result)
    assert payload, result
    ok, problems = history.defaults_shape_ok(payload)
    assert ok, problems
    dumped = json.dumps(payload)
    for secret in CONFIDENTIAL:
        assert secret not in dumped, secret
