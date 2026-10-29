"""Property tests for the leak scanner (P-12, P-13, P-14).

They call the decision seams `confidential_terms` and `leak_scan` from `claude_kiro.analytics` directly (no CLI, no
server). Project modules are imported inside test bodies so this file always collects.
"""

import copy
import json
import re
import uuid
from datetime import datetime, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from stats_helpers import CONFIDENTIAL, ORION_REMOTE, seeded_local_record

PROPERTIES = {
    "P-12": "test_p12_confidential_terms_are_always_caught",
    "P-13": "test_p13_generic_shapes_always_hit",
    "P-14": "test_p14_short_terms_only_hit_as_whole_words",
}

SETTINGS = settings(
    derandomize=True,
    max_examples=200,
    database=None,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow, HealthCheck.data_too_large],
)

NON_CK = ("competitor-teardown-q4", "tax-review-2026", "quarterly-bonus-audit")
IDENTITY = ("Dana Scully", "dana.scully@acme-corp.com")
WEAK = ("ui", "ops", "ledger")
RECORD_HEAD = '"schema": "ck-run-analytics/1", "id": "0123456789abcdef", "month": "2026-10"'


# --------------------------------------------------------------------------- local records and terms


def _records():
    """Two projects' local records, with the weak terms `ui`, `ops`, `ledger` planted as spec and path segments."""
    first = seeded_local_record("spec-implement")
    first["project"]["spec"] = "ui"
    first["project"]["root"] = "/work/ops/ledger/app"
    first["terms"] = list(first["terms"]) + list(WEAK)
    second = copy.deepcopy(seeded_local_record("spec-create"))
    second["project"]["root"] = "/srv/orion/billing"
    second["project"]["spec"] = "orion-billing"
    second["project"]["remote"] = ORION_REMOTE
    return [first, second]


def _terms(records=None, non_ck=NON_CK):
    from claude_kiro.analytics import confidential_terms

    return set(confidential_terms(records if records is not None else _records(), non_ck))


def _scan(files, terms, identity=()):
    from claude_kiro.analytics import leak_scan

    hits = leak_scan(files, terms, identity)
    for h in hits:
        assert {"file", "field", "kind"} <= set(h), h
    return hits


def _hits_for(hits, name):
    return [h for h in hits if h["file"] == name]


# --------------------------------------------------------------------------- file builders


def _jlit(value, escape=False):
    """A JSON string literal; with `escape`, `/` becomes `\\/` and every other alphanumeric char becomes `\\u00xx`."""
    if not escape:
        return json.dumps(value)
    out = []
    for i, ch in enumerate(value):
        if ch == "/":
            out.append("\\/")
        elif ch.isascii() and ch.isalnum() and i % 2 == 0:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(json.dumps(ch)[1:-1])
    return '"' + "".join(out) + '"'


def _json_file(where, value, escape=False):
    lit = _jlit(value, escape)
    if where == "key":
        return "{%s, %s: 1}" % (RECORD_HEAD, lit)
    if where == "machine":
        return "{%s, \"machine\": %s}" % (RECORD_HEAD, lit)
    if where == "issues":
        return "{%s, \"issues\": [%s]}" % (RECORD_HEAD, lit)
    return "{%s, \"extra\": {\"deep\": %s}}" % (RECORD_HEAD, lit)


def _term_hits(hits, name):
    """Hits in `name` that come from term or identity matching (not from a generic shape)."""
    return [h for h in hits if h["file"] == name and h["kind"] in ("term", "identity")]


def _check_field(hit, where, text):
    field = hit["field"]
    assert isinstance(field, str) and field, hit
    m = re.fullmatch(r"line (\d+)", field)
    if m:
        assert 1 <= int(m.group(1)) <= len(text.splitlines()) + 1, (hit, text)
    elif where in ("machine", "issues", "extra"):
        assert field.startswith(where), (hit, text)


# --------------------------------------------------------------------------- P-12


FORMS = [
    "asis", "upper", "lower", "title", "sentence", "paren", "quoted", "seg_abs", "seg_rel", "seg_dot", "seg_dotdot",
    "seg_home", "seg_win", "branch1", "branch2", "remote_https", "remote_git", "remote_scp", "remote_ssh", "embedded",
]


def _surface(t, form, strong):
    return {
        "asis": t,
        "upper": t.upper(),
        "lower": t.lower(),
        "title": t.title(),
        "sentence": "we checked %s, then left." % t,
        "paren": "(%s)!" % t,
        "quoted": "said \"%s\" twice" % t,
        "seg_abs": "/x/%s/y" % t,
        "seg_rel": "%s/y" % t,
        "seg_dot": "./%s" % t,
        "seg_dotdot": "../%s" % t,
        "seg_home": "~/%s" % t,
        "seg_win": "C:\\%s\\y" % t,
        "branch1": "integrate/%s" % t,
        "branch2": "feat/%s-1" % t,
        "remote_https": "https://github.com/o/%s" % t,
        "remote_git": "https://github.com/o/%s.git" % t,
        "remote_scp": "git@github.com:%s.git" % t,
        "remote_ssh": "ssh://git@github.com/%s" % t,
        "embedded": ("x%sy" % t) if strong else ("see %s!" % t),
    }[form]


def _sources():
    strong = list(CONFIDENTIAL) + ["quarterly-bonus-audit"]
    return [(t, True, "term") for t in strong] + [(t, False, "term") for t in WEAK] + [(t, True, "identity") for t in IDENTITY]


_PLANT = st.tuples(
    st.integers(0, len(_sources()) - 1),
    st.sampled_from(FORMS),
    st.sampled_from(["json_value", "json_key", "md"]),
    st.sampled_from(["machine", "issues", "extra"]),
    st.booleans(),
    st.integers(0, 3),
)


def test_p12_domain_setup():
    terms = {t.lower() for t in _terms()}
    missing = [c for c in CONFIDENTIAL if c.lower() not in terms]
    assert not missing, "confidential_terms must return every seeded string: %r" % missing
    assert {t.lower() for t in NON_CK} <= terms
    assert set(WEAK) <= terms
    # `review` is a whole word of ck vocabulary (review_rounds, ...), so it is dropped (design D18)
    assert "review" not in terms


@SETTINGS
@given(plants=st.lists(_PLANT, min_size=1, max_size=3))
def test_p12_confidential_terms_are_always_caught(plants):
    terms = _terms()
    sources = _sources()
    files, expect = {}, []
    for i, (si, form, kind, where, escape, pad) in enumerate(plants):
        term, strong, source = sources[si]
        planted = _surface(term, form, strong)
        if kind == "md":
            name = "analytics/runs/2026-10/n%d.md" % i
            lines = ["# Notes", "", "plain text"][: 1 + pad % 3] + [planted, "tail"]
            files[name] = "\n".join(lines) + "\n"
            expect.append((name, "md", source, term, len(lines) - 1))
        else:
            name = "analytics/runs/2026-10/r%d.json" % i
            files[name] = _json_file("key" if kind == "json_key" else where, planted, escape)
            expect.append((name, "key" if kind == "json_key" else where, source, term, None))
    for name, where, source, term, line in expect:
        use_terms = set() if source == "identity" else terms
        hits = _term_hits(_scan(files, use_terms, IDENTITY), name)
        assert hits, ("no hit for %r planted in %s: %s" % (term, name, files[name]))
        if where == "md":
            fields = [h["field"] for h in hits]
            assert "line %d" % line in fields, (fields, files[name])
        else:
            for h in hits:
                _check_field(h, where, files[name])
        # metamorphic: appending text never removes a hit
        for suffix in ("\nmore words here\n", " and a trailing note"):
            bigger = dict(files)
            if where == "md":
                bigger[name] = files[name] + suffix
            else:  # keep the JSON valid: add a member inside the object
                bigger[name] = files[name][:-1] + ', "zz_more": "%s"}' % suffix.strip()
            after = _term_hits(_scan(bigger, use_terms, IDENTITY), name)
            assert after, ("appending %r removed the hit for %r" % (suffix, term))
            if where == "md":
                assert "line %d" % line in [h["field"] for h in after]


# --------------------------------------------------------------------------- P-13


_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_word = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8)
_dates = st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2099, 12, 31))


def _flip(case, s):
    return {"asis": s, "upper": s.upper(), "lower": s.lower(), "mixed": "".join(c.upper() if i % 2 else c.lower() for i, c in enumerate(s))}[case]


_case = st.sampled_from(["asis", "upper", "lower", "mixed"])


@st.composite
def _shapes(draw):
    kind = draw(st.sampled_from(
        ["email", "url", "key", "path", "uuid", "wf", "timestamp", "date"]))
    if kind == "email":
        local = draw(_word) + draw(st.sampled_from(["", "+tag", "+a1"]))
        addr = "%s@%s.%s" % (local, draw(_word), draw(st.sampled_from(["com", "org", "io"])))
        addr = _flip(draw(_case), addr)
        return kind, draw(st.sampled_from(["%s", "Some Name <%s>", "\"Q Name\" <%s>", "mail %s now"])) % addr
    if kind == "url":
        host, path = draw(_word), draw(_word)
        return kind, draw(st.sampled_from([
            "http://%s.com/%s", "https://%s.com/%s", "ssh://git@%s.com/o/%s", "git@%s.com:o/%s.git"])) % (host, path)
    if kind == "key":
        body = lambda n, alpha=_ALNUM: draw(st.text(alphabet=alpha, min_size=n, max_size=n))  # noqa: E731
        return kind, draw(st.sampled_from([
            lambda: "sk-ant-api03-" + body(24, _ALNUM + "_-"),
            lambda: "ghp_" + body(36),
            lambda: "github_pat_" + body(30, _ALNUM + "_"),
            lambda: "AKIA" + body(16, "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"),
            lambda: "xoxb-" + body(20, _ALNUM + "-"),
        ]))()
    if kind == "path":
        a, b = draw(_word), draw(_word)
        return kind, " " + draw(st.sampled_from(["/home/%s/%s", "/Users/%s/%s", "~/%s/%s", "C:\\%s\\%s"])) % (a, b)
    if kind == "uuid":
        return kind, _flip(draw(_case), str(uuid.UUID(int=draw(st.integers(0, 2**128 - 1)))))
    if kind == "wf":
        return kind, "wf_" + draw(st.text(alphabet="0123456789abcdef", min_size=6, max_size=16))
    d = draw(_dates).replace(microsecond=0)
    if kind == "date":
        return kind, d.strftime("%Y-%m-%d")
    form = draw(st.sampled_from(["Z", "+02:00", "ms", "none", "space"]))
    if form == "space":
        return kind, d.strftime("%Y-%m-%d %H:%M:%S")
    base = d.strftime("%Y-%m-%dT%H:%M:%S")
    return kind, base + {"Z": "Z", "+02:00": "+02:00", "ms": ".123Z", "none": ""}[form]


@SETTINGS
@given(shape=_shapes(), where=st.sampled_from(["md", "extra", "machine_free"]))
def test_p13_generic_shapes_always_hit(shape, where):
    kind, text = shape
    sentence = "note %s end" % text
    if where == "md":
        name, content = "analytics/runs/2026-10/n.md", "# Notes\n\n%s\n" % sentence
    elif where == "extra":
        name, content = "analytics/runs/2026-10/r.json", _json_file("extra", sentence)
    else:
        name, content = "<pr-body>", sentence
    hits = _hits_for(_scan({name: content}, set()), name)
    assert hits, "no hit for %s shape %r in %s" % (kind, text, content)


# --------------------------------------------------------------------------- P-14


SHORT_TERMS = ["a", "ui", "api", "test", "review", "plan", "land"]
# `_` is a boundary under design.md's whole-word rule `(?<![A-Za-z0-9])term(?![A-Za-z0-9])`, so a snake_case name such as
# `ui_kit` is not a host: `ui` hits there (see test_p14_underscore_is_a_word_boundary). Snake_case names stay clean only
# as fixed tokens (see test_p14_enum_member_in_enum_position_never_hits).
HOSTS = ["reviewer", "reviewRounds", "attempts", "planSteps", "landings"]
STANDALONE = [
    "{t}", "the {t} spec", "x/{t}", "'{t}' ok", "{t}, next", "(see {t}).", "say \"{t}\"", "path/{t}/end", "ok {t}",
]
_alnum_pad = st.text(alphabet="bcdfghjkmnqrstvwxz0123456789", min_size=1, max_size=4)


@st.composite
def _embedded(draw):
    term = draw(st.sampled_from(SHORT_TERMS))
    mode = draw(st.sampled_from(["listed", "prefix", "suffix", "both", "camel"]))
    if mode == "listed":
        pairs = [h for h in HOSTS if term in h.lower()]
        if pairs:
            return term, draw(st.sampled_from(pairs))
        mode = "both"
    if mode == "prefix":
        return term, draw(_alnum_pad) + term
    if mode == "suffix":
        return term, term + draw(_alnum_pad)
    if mode == "camel":
        return term, term + draw(st.sampled_from(["Rounds", "Steps", "Kit", "Count"]))
    return term, draw(_alnum_pad) + term + draw(_alnum_pad)


def _contexts(text):
    """Files carrying `text` as free text, a non-enum JSON string value and a JSON key."""
    return {
        "notes.md": "# Notes\n\nthe %s field\n" % text,
        "<pr-body>": "count: 2 and %s" % text,
        "<commit>": "ck-stats-batch: 2 %s" % text,
        "analytics/runs/2026-10/v.json": _json_file("extra", text),
        "analytics/runs/2026-10/k.json": _json_file("key", text),
    }


@SETTINGS
@given(embedded=_embedded(), template=st.sampled_from(STANDALONE), term=st.sampled_from(SHORT_TERMS))
def test_p14_short_terms_only_hit_as_whole_words(embedded, template, term):
    emb_term, host = embedded
    # embedded: no hit, in every context
    files = _contexts(host)
    hits = _scan(files, {emb_term})
    assert not hits, "term %r hit inside %r: %r" % (emb_term, host, hits)
    # standalone: always a hit, in every context (and at the text edges)
    for text in (template.format(t=term), term):
        files = _contexts(text)
        for name in ("notes.md", "<pr-body>", "<commit>", "analytics/runs/2026-10/v.json"):
            assert _hits_for(_scan({name: files[name]}, {term}), name), "term %r standing alone in %r gave no hit" % (term, files[name])


def test_p14_listed_hosts_never_hit():
    for host in HOSTS:
        for term in SHORT_TERMS:
            if term in host.lower():
                assert not _scan(_contexts(host), {term}), (term, host)


def test_p14_underscore_is_a_word_boundary():
    """Design.md: `_` is outside [A-Za-z0-9], so a weak term standing between underscores is a whole word."""
    for text in ("ui_kit", "kit_ui", "my_ui_kit"):
        files = _contexts(text)
        for name in ("notes.md", "<pr-body>", "<commit>", "analytics/runs/2026-10/v.json"):
            assert _hits_for(_scan({name: files[name]}, {"ui"}), name), (name, text)


ENUM_CASES = [
    ("reviewer", "agents_by_role", {"reviewer": 2}, "reviewer"),
    ("reviewer", "config", {"reviewer": {"model": "opus", "effort": "high"}}, "reviewer"),
    ("needs", "issues", ["review_needs_work"], "review_needs_work"),
    ("after", "outcome", "ready_after_decisions", "ready_after_decisions"),
    ("implement", "workflow", "spec-implement", "spec-implement"),
    ("blocking", "issues", ["review_blocking_unresolved"], "review_blocking_unresolved"),
    ("decisions", "outcome", "ready_after_decisions", "ready_after_decisions"),
]


@SETTINGS
@given(case=st.sampled_from(ENUM_CASES))
def test_p14_enum_member_in_enum_position_never_hits(case):
    """A term that whole-word-matches part of a fixed token never hits where that token is an enum value, and hits elsewhere."""
    term, field, value, token = case
    head = {"schema": "ck-run-analytics/1", "id": "0123456789abcdef", "month": "2026-10"}
    name = "analytics/runs/2026-10/0123456789abcdef.json"
    text = json.dumps({**head, field: value}, indent=2)
    assert not _hits_for(_scan({name: text}, {term}), name), text
    # the same string in a non-enum position (an unknown key's value) and in free text does hit
    moved = json.dumps({**head, "extra": {"deep": "see " + token}}, indent=2)
    assert _hits_for(_scan({name: moved}, {term}), name), moved
    assert _hits_for(_scan({"notes.md": "note %s end\n" % token}, {term}), "notes.md")


_HEXDIGITS = st.text(alphabet="0123456789abcdefABCDEF", min_size=1, max_size=8)
_DIGITS = st.text(alphabet="0123456789", min_size=1, max_size=6)
_FIXED_SEGMENTS = ["1", "10", "80", "2026", "cafe", "beef", "0a1b"]


@SETTINGS
@given(
    extra=st.lists(st.one_of(_HEXDIGITS, _DIGITS), max_size=4),
    rid=st.text(alphabet="0123456789abcdef", min_size=16, max_size=16),
    where=st.sampled_from(["version", "month", "id", "free"]),
)
def test_p14_digit_and_hex_only_strings_are_not_terms(extra, rid, where):
    where_digits = {"version": "2.1.80", "month": "2026-10", "id": rid, "free": " ".join(extra)}[where]
    segments = _FIXED_SEGMENTS + extra
    rec = seeded_local_record("spec-implement")
    rec["project"]["root"] = "/" + "/".join(segments) + "/app"
    rec["terms"] = list(rec["terms"]) + segments
    terms = _terms([rec], ())
    low = {t.lower() for t in terms}
    kept = [s for s in segments if s.lower() in low]
    assert not kept, "digit-only or hex-only strings were returned as terms: %r" % kept
    head = '"schema": "ck-run-analytics/1", "id": "%s", "month": "2026-10", "claude_code_version": "2.1.80"' % rid
    files = {
        "analytics/runs/2026-10/%s.json" % rid: "{%s}" % head,
        "notes.md": "ran on port 80 with version 2.1.80 in 2026-10 for id %s and %s %s\n" % (rid, " ".join(segments[:7]), where_digits),
    }
    hits = _scan(files, terms)
    assert not hits, (where, hits)
