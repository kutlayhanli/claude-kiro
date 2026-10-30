"""Property P-19: the bundled-defaults builder never leaks record-level data (run-followups, requirement 7.2).

It calls the pure seams `build_defaults` and `defaults_shape_ok` of `claude_kiro.eta.history` over records seeded with
every `CONFIDENTIAL` value. Project modules are imported inside the test body so this file always collects.
"""

import copy
import json

import pytest

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


def _store():
    """Up to 60 distinct seeded records: unique runId (a CONFIDENTIAL string plus an index), CONFIDENTIAL planted in every other slot."""
    out = []
    for i in range(60):
        rec = _rotate(copy.deepcopy(seeded_local_record("spec-implement" if i % 2 == 0 else "spec-create")), i)
        rec["runId"] = f"{CONFIDENTIAL[i % len(CONFIDENTIAL)]}-{i:03d}"
        out.append(rec)
    return out


STORE = _store()


@st.composite
def stores(draw):
    idx = draw(st.sets(st.integers(0, len(STORE) - 1), max_size=len(STORE)))
    return [STORE[i] for i in sorted(idx)]


@SETTINGS
@given(records=stores(), min_runs=st.integers(1, 40))
def test_p19_bundled_defaults_hold_no_record_level_data(records, min_runs):
    from claude_kiro.eta import history

    if len(records) < min_runs:  # design 7.5: every record is one run; fewer than min_runs refuses with ValueError
        with pytest.raises(ValueError):
            history.build_defaults(records, min_runs=min_runs)
        return
    result = history.build_defaults(records, min_runs=min_runs)
    payload = result.payload
    assert payload, result
    ok, problems = history.defaults_shape_ok(payload)
    assert ok, problems
    dumped = json.dumps(payload)
    for secret in CONFIDENTIAL:
        assert secret not in dumped, secret
