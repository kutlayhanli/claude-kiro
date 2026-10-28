"""P-22: the project pseudonym is stable, keyed by the salt, distinct per remote, and opaque.

Pure property test of the decision seam `claude_kiro.analytics.pseudonym(remote, salt) -> str`;
no CLI. Remotes come in every `confidential()` remote form (https, https with `.git`, scp-like,
ssh://, bare `o/r`) plus the seeded remotes; salts are 16-32 random bytes.
"""

import re

from hypothesis import HealthCheck, given, settings, strategies as st

from stats_helpers import ACME_REMOTE, ORION_REMOTE

PROPERTIES = {"P-22": "test_pseudonym_is_stable_keyed_and_opaque"}

SETTINGS = dict(
    derandomize=True,
    max_examples=200,
    database=None,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

_NAME = st.from_regex(r"[a-z][a-z0-9]{1,8}(-[a-z0-9]{1,6}){0,2}", fullmatch=True)
_HOST = st.sampled_from(["github.com", "gitlab.example.com", "git.corp.example.org", "bitbucket.org"])


@st.composite
def remotes(draw):
    """-> (remote, [name/host segments that must not leak], (host or None, owner, repo))."""
    host, owner, repo = draw(_HOST), draw(_NAME), draw(_NAME)
    form = draw(st.sampled_from(["https", "https.git", "scp", "ssh", "bare"]))
    remote = {
        "https": f"https://{host}/{owner}/{repo}",
        "https.git": f"https://{host}/{owner}/{repo}.git",
        "scp": f"git@{host}:{owner}/{repo}.git",
        "ssh": f"ssh://git@{host}/{owner}/{repo}",
        "bare": f"{owner}/{repo}",
    }[form]
    segs = [owner, repo] + ([] if form == "bare" else [host] + host.split("."))
    return remote, segs, (None if form == "bare" else host, owner, repo)


_SEEDED = st.sampled_from(
    [
        (ACME_REMOTE, ["acme-corp", "acme-ledger", "github.com", "github"], (None, "acme-corp", "acme-ledger")),
        (ORION_REMOTE, ["orion", "billing", "gitlab.example.com", "gitlab", "example"], (None, "orion", "billing")),
        ("o/r", ["o", "r"], (None, "o", "r")),
    ]
)
_REMOTES = st.one_of(remotes(), _SEEDED)
_SALTS = st.binary(min_size=16, max_size=32)


def _leaks(pseudo: str, segments) -> list:
    low = pseudo.lower()
    out = []
    for seg in segments:
        # A segment holding a non-hex character can never appear in a hex digest by chance.
        if re.search(r"[^0-9a-f]", seg.lower()) and seg.lower() in low:
            out.append(seg)
    return out


@settings(**SETTINGS)
@given(a=_REMOTES, b=_REMOTES, salt_a=_SALTS, salt_b=_SALTS)
def test_pseudonym_is_stable_keyed_and_opaque(a, b, salt_a, salt_b):
    from claude_kiro.analytics import pseudonym

    (remote_a, segs_a, id_a), (remote_b, _segs_b, id_b) = a, b
    p = pseudonym(remote_a, salt_a)
    assert isinstance(p, str) and p

    # stable for equal inputs
    assert pseudonym(remote_a, salt_a) == p

    # opaque: no owner / repo / host segment appears, in any case
    assert _leaks(p, segs_a) == []
    assert remote_a.lower() not in p.lower()

    # keyed: a different salt gives a different pseudonym
    if salt_a != salt_b:
        assert pseudonym(remote_a, salt_b) != p

    # a different project gives a different pseudonym under the same salt. Project identity, not
    # the raw string: the pseudonym hashes the normalised remote, so the https, .git, scp and ssh
    # forms of one project may (and should) share a pseudonym.
    hosts_differ = id_a[0] is not None and id_b[0] is not None and id_a[0] != id_b[0]
    if id_a[1:] != id_b[1:] or hosts_differ:
        assert pseudonym(remote_b, salt_a) != p


@settings(**SETTINGS)
@given(
    items=st.lists(remotes(), min_size=2, max_size=12, unique_by=lambda r: r[2]),
    salt=_SALTS,
)
def test_pseudonyms_do_not_collide_over_a_generated_set(items, salt):
    from claude_kiro.analytics import pseudonym

    # Distinct (host, owner, repo) identities. Two entries that differ only by host-less "bare"
    # form vs a hosted form of the same owner/repo are one project under normalisation: dedupe.
    seen, uniq = set(), []
    for remote, _segs, ident in items:
        if ident[1:] in seen:
            continue
        seen.add(ident[1:])
        uniq.append(remote)
    pseudos = [pseudonym(r, salt) for r in uniq]
    assert len(set(pseudos)) == len(pseudos)
