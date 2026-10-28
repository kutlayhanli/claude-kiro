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
    suppress_health_check=list(HealthCheck),
)

_NAME = st.from_regex(r"[a-z][a-z0-9]{1,8}(-[a-z0-9]{1,6}){0,2}", fullmatch=True)
_HOST = st.sampled_from(["github.com", "gitlab.example.com", "git.corp.example.org", "bitbucket.org"])


@st.composite
def remotes(draw):
    """-> (remote, [name/host segments that must not leak])."""
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
    return remote, segs


_SEEDED = st.sampled_from(
    [
        (ACME_REMOTE, ["acme-corp", "acme-ledger", "github.com", "github"]),
        (ORION_REMOTE, ["orion", "billing", "gitlab.example.com", "gitlab", "example"]),
        ("o/r", ["o", "r"]),
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

    (remote_a, segs_a), (remote_b, _segs_b) = a, b
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

    # a different remote gives a different pseudonym under the same salt
    if remote_a != remote_b:
        assert pseudonym(remote_b, salt_a) != p
