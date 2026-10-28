"""Property tests for the shareable record builder: P-6, P-17, P-18, P-21.

They call the decision seams `build_shareable` and `month_of` from `claude_kiro.analytics` directly (imported
inside the test bodies so the suite stays collectable before the implementation exists).
"""

import copy
import json
import re
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings, strategies as st

from stats_helpers import CONFIDENTIAL, seeded_local_record

PROPERTIES = {
    "P-6": "test_p6_free_text_changes_leave_output_equal_except_id",
    "P-17": "test_p17_record_id_is_fresh_and_unrelated_to_source_ids",
    "P-18": "test_p18_month_is_utc_year_month_only",
    "P-21": "test_p21_same_record_id_gives_identical_output",
}

SETTINGS = dict(derandomize=True, max_examples=200, database=None, deadline=None)

# ----------------------------------------------------------------------------------------------- generators

_ENUM_KEYS = {"outcome", "verdict", "role", "model", "effort", "stack"}
# Leaves the builder needs to recognise the record or its start time; not replaced by noise.
_PROTECTED_KEYS = {"schema", "workflow", "workflowName", "startTime", "ckVersion", "collectorVersion", "status"}

_WRAPS = [
    "{t}", "{t}".upper(), "{t}".lower(), "see {t}, then stop", "({t}).", "'{t}'", "{t}:", "x {t} y",
    "/x/{t}/y", "{t}/y", "./{t}", "../{t}", "~/{t}", "C:\\\\{t}\\\\y",
    "integrate/{t}", "feat/{t}-1",
    "https://github.com/o/{t}", "https://github.com/o/{t}.git", "git@github.com:o/{t}.git", "ssh://git@github.com/o/{t}",
    "o/{t}", "Name <{t}@b.c>", "a+tag@{t}.c", "A@B.C {t}",
    "wf_0123abcd {t}", "a0123456789abcdef {t}", "123e4567-e89b-12d3-a456-426614174000 {t}",
    "123E4567-E89B-12D3-A456-426614174000", "2026-10-12T09:13:27Z", "2026-10-12T09:13:27+02:00",
    "2026-10-12T09:13:27.123Z", "2026-10-12 09:13:27", "1760260407000",
]


@st.composite
def confidential(draw):
    term = draw(st.sampled_from(CONFIDENTIAL))
    form = draw(st.sampled_from(_WRAPS))
    case = draw(st.sampled_from(["as-is", "upper", "lower", "title"]))
    if case == "upper":
        term = term.upper()
    elif case == "lower":
        term = term.lower()
    elif case == "title":
        term = term.title()
    text = form.replace("{t}", term)
    if draw(st.booleans()):
        text = json.dumps(text)[1:-1].replace("/", "\\/")  # JSON-escaped form
    return text


def enum_noise():
    return st.one_of(
        st.sampled_from([
            "Complete", "completed", "complete ", "halted!", "approve", "approved", "APPROVE", "request_changes",
            "claude-opus-5-5", "claude-sonnet-5-5[1m]", "claude-haiku-5-5-20261001", "us.anthropic.claude-opus-5-5-v1:0",
            "arn:aws:bedrock:us-east-1:123:inference-profile/claude-sonnet", "claude-opus-5-5@20261001",
            "anthropic/claude-opus-5-5", "opus", "Opus", "inherited", "my-finetune",
            "max", "turbo", "HIGH", "python", "Python", "django", "acme-stack",
        ]),
        confidential(),
    )


def _string_paths(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _string_paths(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _string_paths(v, path + (i,))
    elif isinstance(obj, str):
        yield path


def _set(obj, path, value):
    for p in path[:-1]:
        obj = obj[p]
    obj[path[-1]] = value


def _get(obj, path):
    for p in path:
        obj = obj[p]
    return obj


def _last_key(path):
    for p in reversed(path):
        if isinstance(p, str):
            return p
    return ""


@st.composite
def local_record(draw):
    kind = draw(st.sampled_from(["spec-implement", "spec-create"]))
    rec = copy.deepcopy(seeded_local_record(kind))
    paths = [p for p in _string_paths(rec) if not (set(p) & _PROTECTED_KEYS)]
    chosen = draw(st.lists(st.sampled_from(paths), unique=True, max_size=len(paths))) if paths else []
    for p in chosen:
        value = draw(enum_noise() if _last_key(p) in _ENUM_KEYS else confidential())
        _set(rec, p, value)
    return rec


def _source_ids(rec):
    ids = [rec.get("runId"), rec.get("sessionId")] + [a.get("agentId") for a in rec.get("agents", [])]
    return [i for i in ids if isinstance(i, str)]


# P-10's reason table: another reason of the same class (a reason carries the class in its prefix).
_REASON_CLASSES = [
    "merge failed: still conflicting after ",
    "merge failed: conflict not resolved",
    "merge failed: no commits",
    "review: blocking findings remain",
    "planning step failed",
    "permission denied:",
]


def _reason_class(reason):
    for c in _REASON_CLASSES:
        if reason.startswith(c):
            return c
    return None


@st.composite
def free_text_pair(draw):
    """(record, mutated record): one free-text leaf (task title, project path, or a reason within its class) differs."""
    rec = draw(local_record())
    other = copy.deepcopy(rec)
    targets = [("project", "root")]
    for i, t in enumerate(rec.get("tasks", [])):
        targets.append(("tasks", i, "title"))
        if _reason_class(t.get("reason") or "") is not None:
            targets.append(("tasks", i, "reason"))
    path = draw(st.sampled_from(targets))
    if path[-1] == "reason":
        cls = _reason_class(_get(rec, path))
        suffix = draw(confidential())
        # the class prefix is kept, so the derived issue code stays the same; only the trailing text changes
        _set(other, path, cls.rstrip() + " " + suffix)
    else:
        _set(other, path, draw(st.one_of(confidential(), st.text(max_size=30))))
    return rec, other


# ----------------------------------------------------------------------------------------------- P-6


@settings(**SETTINGS)
@given(free_text_pair())
def test_p6_free_text_changes_leave_output_equal_except_id(pair):
    from claude_kiro.analytics import build_shareable

    a, b = pair
    out_a = build_shareable(a)
    out_b = build_shareable(b)
    out_a.pop("id", None)
    out_b.pop("id", None)
    assert out_a == out_b


# ----------------------------------------------------------------------------------------------- P-17


@settings(**SETTINGS)
@given(local_record())
def test_p17_record_id_is_fresh_and_unrelated_to_source_ids(rec):
    from claude_kiro.analytics import build_shareable

    first = build_shareable(rec)
    second = build_shareable(rec)
    assert first["id"] != second["id"]
    for new_id in (first["id"], second["id"]):
        for src in _source_ids(rec):
            low = src.lower()
            for i in range(0, max(len(new_id) - 5, 0)):
                assert new_id[i:i + 6].lower() not in low, (new_id, src)


# ----------------------------------------------------------------------------------------------- P-18


_FIXED_STARTS = [
    "2026-10-31T23:30:00-02:00", "2026-12-31T23:59:59-12:00", "2026-01-01T00:00:00+14:00", "2027-01-01T00:30:00+01:00",
    "2026-03-01T00:00:00+14:00", "2026-02-28T23:59:59.999-12:00",
]


@st.composite
def start_time(draw):
    """-> (start value as handed to the builder, expected UTC yyyy-mm, forbidden day/time renderings)."""
    if draw(st.integers(0, 5)) == 0:
        raw = draw(st.sampled_from(_FIXED_STARTS))
        dt = datetime.fromisoformat(raw)
    else:
        base = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=draw(st.integers(0, 5 * 366 * 86_400_000)))
        dt = base
    form = draw(st.sampled_from(["z", "offset", "fraction", "epoch"]))
    if form == "epoch":
        utc = dt.astimezone(timezone.utc)
        ms = int(utc.timestamp() * 1000)
        return ms, utc.strftime("%Y-%m"), [str(ms)], utc
    if form == "z":
        utc = dt.astimezone(timezone.utc).replace(microsecond=0)
        return utc.strftime("%Y-%m-%dT%H:%M:%SZ"), utc.strftime("%Y-%m"), [], utc
    if form == "fraction":
        utc = dt.astimezone(timezone.utc)
        return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z", utc.strftime("%Y-%m"), [], utc
    offset_h = draw(st.sampled_from([14, -12, -2, 5, 0]))
    tz = timezone(timedelta(hours=offset_h))
    loc = dt.astimezone(tz).replace(microsecond=0)
    utc = loc.astimezone(timezone.utc)
    return loc.isoformat(), utc.strftime("%Y-%m"), [], utc


@settings(**SETTINGS)
@given(start_time())
def test_p18_month_is_utc_year_month_only(case):
    from claude_kiro.analytics import build_shareable, month_of

    raw, month, extra, utc = case
    assert month_of(raw) == month
    rec = seeded_local_record("spec-implement")
    rec["startTime"] = raw
    out = build_shareable(rec)
    assert out["month"] == month
    text = json.dumps(out)
    forbidden = list(extra)
    if isinstance(raw, str):
        forbidden.append(raw)
    # the day and time of the instant, in UTC and in the local rendering
    day = utc.strftime("%Y-%m-%d")
    forbidden += [day, utc.strftime("%H:%M:%S")]
    if isinstance(raw, str):
        m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})", raw)
        forbidden += [m.group(1), m.group(2)]
    for f in forbidden:
        assert f not in text, f


# ----------------------------------------------------------------------------------------------- P-21


@settings(**SETTINGS)
@given(local_record(), st.sampled_from(["0123456789abcdef", "ffffffffffffffff", "r-1"]), st.sampled_from([None, "work-laptop", "a"]))
def test_p21_same_record_id_gives_identical_output(rec, record_id, label):
    from claude_kiro.analytics import build_shareable

    before = copy.deepcopy(rec)
    one = build_shareable(rec, record_id=record_id, machine_label=label)
    two = build_shareable(rec, record_id=record_id, machine_label=label)
    assert one == two
    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)
    assert rec == before  # the builder does not mutate its input
