"""Property tests P-19 and P-20 for the submit target and the PR text.

Both call the pure decision seams `is_target_repo` and `pr_text` of `claude_kiro.analytics`
(Harness Contract), never GitHub. Project modules are imported inside the test bodies so the
file collects before the implementation exists.
"""

import copy
import re
from typing import Any, List, Set

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from stats_helpers import CONFIDENTIAL, seeded_local_record

PROPERTIES = {
    "P-19": "test_p19_is_target_repo_iff_designates_kutlayhanli_claude_kiro",
    "P-20": "test_p20_pr_text_has_no_planted_string_and_depends_only_on_summary",
}

PROP = settings(derandomize=True, max_examples=200, database=None, deadline=None)

OWNER = "kutlayhanli"
REPO = "claude-kiro"


# --------------------------------------------------------------------------- P-19 generators


def _case_variant(text: str, flips: List[bool]) -> str:
    return "".join(c.swapcase() if flips[i % len(flips)] else c for i, c in enumerate(text))


@st.composite
def accepted_urls(draw: st.DrawFn) -> str:
    owner = _case_variant(OWNER, draw(st.lists(st.booleans(), min_size=1, max_size=11)))
    repo = _case_variant(REPO, draw(st.lists(st.booleans(), min_size=1, max_size=11)))
    form = draw(st.sampled_from(["https", "https.git", "https/", "scp.git", "ssh.git"]))
    if form == "https":
        return f"https://github.com/{owner}/{repo}"
    if form == "https.git":
        return f"https://github.com/{owner}/{repo}.git"
    if form == "https/":
        return f"https://github.com/{owner}/{repo}/"
    if form == "scp.git":
        return f"git@github.com:{owner}/{repo}.git"
    return f"ssh://git@github.com/{owner}/{repo}.git"


NEAR_MISSES = [
    "https://github.com/angelsen/claude-kiro",
    "https://github.com/angelsen/claude-kiro.git",
    "git@github.com:angelsen/claude-kiro.git",
    "https://github.com/kutlayhanli/claude-kiro-fork",
    "https://github.com/kutlayhanli/claude-kiro.git.evil",
    "https://github.com.evil.com/kutlayhanli/claude-kiro",
    "https://github.com.evil.com/kutlayhanli/claude-kiro.git",
    "https://gitlab.com/kutlayhanli/claude-kiro",
    "git@gitlab.com:kutlayhanli/claude-kiro.git",
    "https://github.com/kutlayhanli/claude-kiro/../other",
    "github.com/kutlayhanli/claude-kiro/../other",
    "https://github.com/xkutlayhanli/claude-kiro",
    "https://github.com/kutlayhanli/claude-kiro2",
    "https://github.com/kutlayhanli",
    "https://github.com/claude-kiro",
    "https://evil.com/https://github.com/kutlayhanli/claude-kiro",
    "https://github.com/kutlayhanli/claude-kiro?x=1",
    "https://github.com/kutlayhanli/claude-kiro#frag",
    "/home/kutlay/Workspace/claude-kiro",
    "../claude-kiro",
    "kutlayhanli/claude-kiro-local",
    "file:///srv/git/kutlayhanli/claude-kiro.git",
    "",
    " ",
]

_slug = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=12)


@st.composite
def rejected_urls(draw: st.DrawFn) -> str:
    kind = draw(st.sampled_from(["fixed", "other_repo", "evil_host", "suffix", "path"]))
    if kind == "fixed":
        return draw(st.sampled_from(NEAR_MISSES))
    if kind == "other_repo":
        owner, repo = draw(_slug), draw(_slug)
        assume((owner.lower(), repo.lower()) != (OWNER, REPO))
        return draw(st.sampled_from([
            f"https://github.com/{owner}/{repo}",
            f"https://github.com/{owner}/{repo}.git",
            f"git@github.com:{owner}/{repo}.git",
            f"ssh://git@github.com/{owner}/{repo}.git",
        ]))
    if kind == "evil_host":
        host = draw(_slug)
        assume(host != "github")
        return f"https://{host}.com/{OWNER}/{REPO}"
    if kind == "suffix":
        tail = draw(st.sampled_from(["-fork", "2", ".evil", "/../x", ".git.evil", "/tree/master", "x", "_old", ".git/x"]))
        base = draw(st.sampled_from(["https://github.com", "git@github.com:"]))
        sep = "" if base.endswith(":") else "/"
        return f"{base}{sep}{OWNER}/{REPO}{tail}"
    return "/" + "/".join(draw(st.lists(_slug, min_size=1, max_size=4)))


@PROP
@given(url=accepted_urls())
def _p19_accept(url: str) -> None:
    from claude_kiro.analytics import is_target_repo

    assert is_target_repo(url) is True, url


@PROP
@given(url=rejected_urls())
def _p19_reject(url: str) -> None:
    from claude_kiro.analytics import is_target_repo

    assert is_target_repo(url) is False, url


def test_p19_is_target_repo_iff_designates_kutlayhanli_claude_kiro() -> None:
    _p19_accept()
    _p19_reject()


# --------------------------------------------------------------------------- P-20 generators

# Fields that legitimately reach the PR text: workflow kind and the start time's month.
_PROTECTED_TOP = {"schema", "workflow", "workflowName", "startTime"}

_PLANT_FORMS = [
    lambda t: t,
    lambda t: t.upper(),
    lambda t: t.lower(),
    lambda t: f"see {t}, then stop.",
    lambda t: f"/x/{t}/y",
    lambda t: f"./{t}",
    lambda t: f"~/{t}",
    lambda t: f"integrate/{t}",
    lambda t: f"feat/{t}-1",
    lambda t: f"Name <{t}@b.example>",
]
_STARTS = ["2026-10-12T09:13:27Z", "2026-11-03T00:00:01Z", "2026-10-31T23:30:00-02:00", "2027-01-15T12:00:00.123Z"]


def _surface(term: str, form: int) -> str:
    return _PLANT_FORMS[form % len(_PLANT_FORMS)](term)


def _plantable(term: str) -> bool:
    return len(term) >= 4 and not re.fullmatch(r"[0-9a-fA-F\W_]*", term)


@st.composite
def local_record(draw: st.DrawFn, kind: str, start: str, planted: Set[str]) -> dict:
    """A seeded local record of `kind` / `start` with a random subset of string leaves replaced by
    planted confidential strings (in several surface forms) or fresh unique markers."""
    record = copy.deepcopy(seeded_local_record(kind))
    pool = [t for t in CONFIDENTIAL if _plantable(t)]

    def replacement() -> str:
        if draw(st.booleans()):
            marker = "zq" + draw(st.text(alphabet="ghjkmnpqrstvwxyz", min_size=8, max_size=12))
            value = _surface(marker, draw(st.integers(0, 9)))
        else:
            term = draw(st.sampled_from(pool))
            value = _surface(term, draw(st.integers(0, 9)))
        planted.add(value)
        return value

    def walk(node: Any, top: bool) -> Any:
        if isinstance(node, dict):
            return {k: (v if top and k in _PROTECTED_TOP else walk(v, False)) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, False) for v in node]
        if isinstance(node, str) and draw(st.booleans()):
            return replacement()
        return node

    record = walk(record, True)
    record["workflow"] = kind
    record["startTime"] = start
    return record


@st.composite
def batches(draw: st.DrawFn):
    """(shareable batch A, shareable batch B, planted strings, label, token): same kinds and months, all else drawn apart."""
    from claude_kiro.analytics import build_shareable

    specs = draw(st.lists(st.tuples(st.sampled_from(["spec-implement", "spec-create"]), st.sampled_from(_STARTS)),
                          min_size=1, max_size=5))
    label = draw(st.one_of(st.none(), st.from_regex(r"[a-z0-9-]{1,32}", fullmatch=True)))
    token = draw(st.from_regex(r"[0-9a-f]{8}", fullmatch=True))
    planted: Set[str] = set()
    sides = []
    for _ in range(2):
        records = [build_shareable(draw(local_record(kind, start, planted)), machine_label=label) for kind, start in specs]
        sides.append(records)
    return sides[0], sides[1], planted, label, token


def _texts(out: dict) -> List[str]:
    assert set(out) >= {"branch", "commit", "title", "body"}, out
    return [out["branch"], out["commit"], out["title"], out["body"]]


@PROP
@given(data=batches())
def _p20_no_planted_and_summary_only(data) -> None:
    from claude_kiro.analytics import pr_text

    a, b, planted, label, token = data
    out_a = pr_text(a, label, token=token)
    out_b = pr_text(b, label, token=token)
    for p in planted:
        # a valid label may itself coincide with a planted string; that is the label, not a leak
        assume(p.lower() not in (label or "").lower())
    for text in _texts(out_a) + _texts(out_b):
        low = text.lower()
        for p in planted:
            assert p.lower() not in low, f"planted {p!r} leaked into {text!r}"
    assert out_a["branch"] == f"ck-stats-batch-{token}"
    # same count, kinds, months, label, token -> identical text, however every other field differs
    assert out_a == out_b


@PROP
@given(label=st.one_of(st.none(), st.from_regex(r"[a-z0-9-]{1,32}", fullmatch=True)),
       kind=st.sampled_from(["spec-implement", "spec-create"]),
       count=st.integers(1, 5))
def _p20_fresh_branch(label, kind, count) -> None:
    from claude_kiro.analytics import build_shareable, pr_text

    records = [build_shareable(seeded_local_record(kind), machine_label=label) for _ in range(count)]
    first = pr_text(records, label)
    second = pr_text(records, label)
    assert re.fullmatch(r"ck-stats-batch-[0-9a-f]{8}", first["branch"]), first["branch"]
    assert re.fullmatch(r"ck-stats-batch-[0-9a-f]{8}", second["branch"]), second["branch"]
    assert first["branch"] != second["branch"]


def test_p20_pr_text_has_no_planted_string_and_depends_only_on_summary() -> None:
    _p20_no_planted_and_summary_only()
    _p20_fresh_branch()
