"""P-10: `issue_codes(record)` is a deterministic, order-independent, text-independent function of a local record.

Calls the decision seam `claude_kiro.analytics.issue_codes` (pure, no I/O), never the CLI. Project modules are
imported inside the test bodies so this file collects before the implementation exists.
"""

import copy
import re
from typing import Any, List, Tuple

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from stats_helpers import CONFIDENTIAL, seeded_local_record

PROPERTIES = {"P-10": "test_p10_issue_codes"}

SETTINGS = settings(
    derandomize=True, max_examples=200, database=None, deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

# --------------------------------------------------------------------------- shared generators

_ID_HEX = "0123456789abcdef"


def _surface_forms(t: str) -> List[str]:
    esc = t.replace("/", "\\/")
    return [
        t, t.upper(), t.lower(), t.title(),
        esc, "".join(f"\\u{ord(ch):04x}" if ch in "-_./@:" else ch for ch in t),
        f"saw {t}, then stopped.", f"({t})", f"\"{t}\":",
        f"/x/{t}/y", f"{t}/y", f"./{t}", f"../{t}", f"~/{t}", f"C:\\\\{t}\\\\y",
        f"integrate/{t}", f"feat/{t}-1",
    ]


_FIXED_FORMS = [
    "https://github.com/o/r", "https://github.com/o/r.git", "git@github.com:o/r.git", "ssh://git@github.com/o/r", "o/r",
    "Name <a@b.c>", "a+tag@b.c", "A@B.C", " a@b.c ",
    "wf_0123456789abcdef", "5E55A10E-0000-4000-8000-0000000000A1", "5e55a10e-0000-4000-8000-0000000000a1",
    "a0123456789abcdef",
    "2026-10-12T09:13:27Z", "2026-10-12T09:13:27+02:00", "2026-10-12T09:13:27.123Z", "2026-10-12 09:13:27", "1760260407000",
]


def confidential() -> st.SearchStrategy:
    base = st.sampled_from(CONFIDENTIAL).flatmap(lambda t: st.sampled_from(_surface_forms(t)))
    return st.one_of(base, st.sampled_from(_FIXED_FORMS))


_ENUM_NOISE = [
    "Complete", "completed", "complete ", "halted!", "approve", "approved", "APPROVE", "request_changes",
    "claude-opus-5-5", "claude-sonnet-5-5[1m]", "claude-haiku-5-5-20261001", "us.anthropic.claude-opus-5-5-v1:0",
    "arn:aws:bedrock:us-east-1:123:inference-profile/claude-sonnet", "claude-opus-5-5@20261001", "anthropic/claude-opus-5-5",
    "opus", "Opus", "inherited", "my-finetune", "max", "turbo", "HIGH", "python", "Python", "django", "acme-stack",
]


def enum_noise() -> st.SearchStrategy:
    return st.one_of(st.sampled_from(_ENUM_NOISE), confidential())


# Keys whose value is structured input to the codes (not free text) and must not be replaced by arbitrary strings.
_REASON_KEY = "reason"
_FREE_TEXT_KEYS = {"title", "summary", "text", "preview", "path", "root", "issue", "file"}
_LIST_KEYS = ("tasks", "landings", "agents")


def _string_paths(obj: Any, path: Tuple = ()) -> List[Tuple]:
    out: List[Tuple] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_string_paths(v, path + (k,)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_string_paths(v, path + (i,)))
    elif isinstance(obj, str):
        out.append(path)
    return out


def _set(obj: Any, path: Tuple, value: Any) -> None:
    for p in path[:-1]:
        obj = obj[p]
    obj[path[-1]] = value


# --------------------------------------------------------------------------- P-10 reason classes

PERMISSION = [
    "Task 3 was refused by the permission layer",
    "permission denied: git merge",
    "the call was denied; auto mode classifier said no",
    "needs your approval to continue",
]
REASON_CLASSES = {
    "resolver_rounds": ["merge failed: still conflicting after 3 rounds", "merge failed: still conflicting after 12 rounds"],
    "conflict": ["merge failed: conflict not resolved", "merge failed: conflict not resolved"],
    "no_commits": ["merge failed: no commits", "merge failed: no commits"],
    "review_blocking": ["review: blocking findings remain", "review: blocking findings remain"],
    "planning": ["planning step failed", "planning step failed"],
    "permission": PERMISSION,
    "unclassified": ["gave up", "something odd happened", "retry later", "x"],
}
# Exemplars within a class that differ only by free text must still be the same class; make some textually distinct.
REASON_CLASSES["conflict"] = ["merge failed: conflict not resolved", "merge failed: conflict not resolved (see log)"]
REASON_CLASSES["no_commits"] = ["merge failed: no commits", "merge failed: no commits on branch"]
REASON_CLASSES["review_blocking"] = ["review: blocking findings remain", "review: blocking findings remain (2)"]
REASON_CLASSES["planning"] = ["planning step failed", "planning step failed: timeout"]


def _reason_class(reason: Any) -> str:
    from claude_kiro import bench

    if not isinstance(reason, str):
        return "none"
    if reason.startswith("merge failed: still conflicting after"):
        return "resolver_rounds"
    if reason.startswith("merge failed: conflict not resolved"):
        return "conflict"
    if reason.startswith("merge failed: no commits"):
        return "no_commits"
    if reason.startswith("review: blocking findings remain"):
        return "review_blocking"
    if reason.startswith("planning step failed"):
        return "planning"
    if bench.REFUSAL_RE.search(reason):
        return "permission"
    return "unclassified"


def test_reason_table_is_well_formed():
    """Each class has >= 2 exemplars; non-permission exemplars never match REFUSAL_RE; each exemplar classifies to its class."""
    from claude_kiro import bench

    for name, ex in REASON_CLASSES.items():
        assert len(set(ex)) >= 2, name
        for e in ex:
            assert _reason_class(e) == name, (name, e)
            if name != "permission":
                assert not bench.REFUSAL_RE.search(e), (name, e)


@st.composite
def local_record(draw):
    kind = draw(st.sampled_from(["spec-implement", "spec-create"]))
    rec = seeded_local_record(kind)
    paths = [p for p in _string_paths(rec) if p[-1] != _REASON_KEY and not (len(p) > 1 and p[-2] == "terms")]
    chosen = draw(st.lists(st.sampled_from(paths), max_size=12, unique=True))
    for p in chosen:
        key = p[-1]
        val = draw(enum_noise() if key in ("outcome", "verdict", "model", "role", "status", "stack", "effort", "riskTag", "what")
                   else confidential())
        _set(rec, p, val)
    # task reasons: structured input, drawn from the class table (or absent)
    for t in rec.get("tasks", []):
        cls = draw(st.sampled_from(sorted(REASON_CLASSES) + ["none"]))
        t["reason"] = None if cls == "none" else draw(st.sampled_from(REASON_CLASSES[cls]))
    return rec


def _check_codes(codes: Any, ISSUE_CODES: Any) -> None:
    assert isinstance(codes, list)
    assert all(isinstance(c, str) for c in codes)
    assert codes == sorted(codes)
    assert len(codes) == len(set(codes))
    assert set(codes) <= set(ISSUE_CODES)


@SETTINGS
@given(rec=local_record(), data=st.data())
def test_p10_issue_codes(rec, data):
    from claude_kiro.analytics import ISSUE_CODES, issue_codes

    original = copy.deepcopy(rec)
    codes = issue_codes(rec)
    _check_codes(codes, ISSUE_CODES)
    assert rec == original, "issue_codes mutated its input"

    # deterministic
    assert issue_codes(copy.deepcopy(rec)) == codes

    # order-independent: permute tasks, landings, agents
    perm = copy.deepcopy(rec)
    for key in _LIST_KEYS:
        if isinstance(perm.get(key), list):
            perm[key] = data.draw(st.permutations(perm[key]), label=f"perm-{key}")
    assert issue_codes(perm) == codes

    # text-independent: free-text leaves other than `reason`
    edited = copy.deepcopy(rec)
    for p in _string_paths(edited):
        if p[-1] in _FREE_TEXT_KEYS:
            _set(edited, p, data.draw(st.one_of(st.text(max_size=40), confidential()), label="free-text"))
    assert issue_codes(edited) == codes

    # metamorphic: replace a reason with another exemplar of the same class
    swapped = copy.deepcopy(rec)
    for t in swapped.get("tasks", []):
        cls = _reason_class(t.get("reason"))
        if cls != "none":
            t["reason"] = data.draw(st.sampled_from(REASON_CLASSES[cls]), label="same-class reason")
    assert issue_codes(swapped) == codes
