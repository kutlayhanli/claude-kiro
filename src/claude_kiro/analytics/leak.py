"""The leak check (requirements 9.1-9.4): the confidential term set, generic patterns, the scan and the gate.

`build_terms` collects every confidential string from the local records, the git identity and the names of non-ck
workflows. `scan` matches text against them (design.md "leak.scan matching rule"):

- strong terms (4+ characters, not common, not ck vocabulary) match as case-insensitive substrings; every other term is
  weak and matches only as a whole word;
- free text is matched in full; a shareable-record JSON file is scanned leaf by leaf against the schema, and fixed
  tokens (schema field names, enum members in enum positions, the `schema` constant) are skipped;
- format-validated values (`month`, `id`, versions, `machine`) are skipped for generic patterns only;
- a hit carries the file, the field (dotted JSON path or `line N`), a kind and a rule, never the matched text.
"""

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from claude_kiro.analytics import SCHEMA_SHARED
from claude_kiro.analytics import share as _share
from claude_kiro.analytics.issues import ISSUE_CODES
from claude_kiro.analytics.roles import ROLES
from claude_kiro.analytics.runfile import ALLOWLISTED_WORKFLOWS, iter_run_files, peek_workflow_name

# ---------------------------------------------------------------- word lists

COMMON_WORDS: FrozenSet[str] = frozenset("""
about above after again against all also always and any app apps are around back base because been before being below
between big both build builds but can case cases change changes check class code come commit common config content
copy core could data date default delete did does doing done down each else end error even every example fail
failed false feature file files find first fix flag for form found from function get give going good group had has
have help here high home how idea if import in include info into is issue item just keep key kind know land last
later left less let level lib like line link list load local log long look low main make many map may merge might
mode more most move must name need needs never new next none not note notes now null number off old once one only
open option other our out over own page part path plan plans point port post pre print project pull push put
query read ready real reason release remove repo report request require result return review right root rule run
runs said same save scan scope see set setup should show side since size skip small some source spec split src
start state step steps still stop store string such sure table take task tasks test tests text than that the their
them then there these they thing this those through time to todo tool tools top track tree true try turn type
under until update use used user using value values very view want was way well were what when where which while
who will with without work works would write yes yet you your
git http https ssh www github gitlab com org net dev docs doc script scripts util utils tmp temp var bin lib
main master branch branches origin remote
""".split())

_TEMPLATE_WORDS = ("ck-stats-batch", "ck", "stats", "batch", "count", "kinds", "months", "machine")


def _vocabulary() -> FrozenSet[str]:
    words: Set[str] = set(_TEMPLATE_WORDS)
    words.add(SCHEMA_SHARED)
    for pattern in _share.SHAREABLE_FIELDS:
        words.update(seg for seg in re.split(r"[.\[\]]+", pattern) if seg and seg != "*")
    for group in (
        _share.MODEL_FAMILIES, _share.OUTCOMES, _share.VERDICTS, _share.EFFORTS, _share.STACK_HINTS, _share.WORKFLOWS,
        _share.VARIANTS, _share.CONFIG_ROLES, _share.TRACKS, _share.RISKS, _share.COST_SOURCES, _share.SOURCES, _share.MISSING_KINDS,
        _share.PHASES, _share.COUNT_KEYS, _share.USAGE_KEYS, ROLES, ISSUE_CODES, ALLOWLISTED_WORKFLOWS,
    ):
        words.update(str(m) for m in group)
    return frozenset(w.lower() for w in words)


VOCABULARY: FrozenSet[str] = _vocabulary()
_VOCAB_WORDS: FrozenSet[str] = frozenset(w for entry in VOCABULARY for w in re.split(r"[-_/: ]+", entry) if w)

# ---------------------------------------------------------------- generic patterns

_I = re.IGNORECASE
GENERIC_PATTERNS: Tuple[Tuple[str, str, "re.Pattern"], ...] = (
    ("email", "email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+", _I)),
    ("url", "url", re.compile(r"[a-z][a-z0-9+.-]*://\S+", _I)),
    ("scp_remote", "url", re.compile(r"[\w.-]+@[\w.-]+:\S+", _I)),
    ("absolute_path", "path",
     re.compile(r"(?:^|[\s\"'(])(?:/(?:home|Users|root|var|tmp|mnt|opt|srv|etc)/|[A-Za-z]:\\|~/)", _I)),
    ("uuid", "id", re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", _I)),
    ("run_id", "id", re.compile(r"wf_[A-Za-z0-9_-]{6,}", _I)),
    ("agent_run_id", "id", re.compile(r"\bag_[0-9a-f]{8}\b", re.I)),
    ("timestamp", "timestamp", re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2})?", _I)),
    ("epoch_ms", "timestamp", re.compile(r"\b1[5-9]\d{11}\b", _I)),
    ("key_shape", "secret",
     re.compile(r"(?:sk-|sk-ant-|ghp_|gho_|github_pat_|xox[abp]-|AKIA)[A-Za-z0-9_-]{8,}", _I)),
    ("long_run", "secret", re.compile(r"[A-Za-z0-9_\-]{32,}", _I)),
)

# ---------------------------------------------------------------- types


@dataclass(frozen=True)
class GitIdentity:
    name: Optional[str]
    email: Optional[str]


@dataclass(frozen=True)
class TermSet:
    strong: FrozenSet[str]
    weak: FrozenSet[str]  # lower-cased
    identity: FrozenSet[str] = frozenset()


@dataclass(frozen=True)
class LeakHit:
    file: str
    field: str  # dotted JSON path or "line N"
    kind: str   # term, identity, email, url, secret, path, id, timestamp
    rule: str   # "strong", "weak", or the pattern name; never the matched value

    def as_dict(self) -> Dict[str, str]:
        return {"file": self.file, "field": self.field, "kind": self.kind, "rule": self.rule}


@dataclass(frozen=True)
class Gate:
    allowed: bool
    hits: Tuple[LeakHit, ...]


# ---------------------------------------------------------------- the term set

_HEX_ONLY = re.compile(r"[0-9a-f]+", _I)
_REMOTE_LIKE = re.compile(r"^[\w.+-]+@[\w.-]+:|://")
_SCHEMES = frozenset({"https", "http", "ssh", "git", "www", "file", "ftp"})


def _is_strong(term: str) -> bool:
    return len(term) >= 4 and term not in COMMON_WORDS and term not in VOCABULARY


def _classify(terms: Iterable[str]) -> Tuple[Set[str], Set[str]]:
    strong: Set[str] = set()
    weak: Set[str] = set()
    for t in terms:
        if not isinstance(t, str):
            continue
        low = t.strip().lower()
        if not low:
            continue
        (strong if _is_strong(low) else weak).add(low)
    return strong, weak


def _keep(cand: str, is_id: bool) -> bool:
    if len(cand) < 2 or not any(ch.isalnum() for ch in cand):
        return False
    if is_id:
        return True
    if _HEX_ONLY.fullmatch(cand) and len(cand) < 16:  # digit-only and hex-only terms identify nothing
        return False
    return cand.lower() not in _VOCAB_WORDS


def _candidates(value: str) -> List[str]:
    """The whole string plus, for paths and remotes, their segments and parts."""
    value = value.strip()
    out = [value]
    parts: List[str] = []
    if "/" in value or "\\" in value:
        parts += re.split(r"[/\\]", value)
    if _REMOTE_LIKE.search(value):
        parts += re.split(r"[/\\:@]", value)
    for p in parts:
        p = p.strip()
        if p.lower().endswith(".git"):
            out.append(p)
            p = p[:-4]
        if p and p.lower() not in _SCHEMES:
            out.append(p)
    return out


def build_terms(records: Sequence[Mapping], identity: Optional[GitIdentity] = None,
                non_ck_names: Sequence[str] = (), extra_terms: Sequence[str] = ()) -> TermSet:
    """S17: the confidential term set from every local record, the git identity and the non-ck workflow names."""
    found: Set[str] = set()
    ids: Set[str] = set()

    def add(value: Any, is_id: bool = False) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        if is_id:
            ids.add(value.strip())
            return
        for cand in _candidates(value):
            if _keep(cand, False):
                found.add(cand)

    for rec in records:
        if not isinstance(rec, Mapping):
            continue
        for t in rec.get("terms") if isinstance(rec.get("terms"), (list, tuple)) else ():
            add(t)
        project = rec.get("project")
        if isinstance(project, Mapping):
            for key in ("root", "spec", "branch", "remote"):
                add(project.get(key))
        for key in ("runId", "sessionId"):
            add(rec.get(key), True)
        for agent in rec.get("agents") if isinstance(rec.get("agents"), list) else ():
            if isinstance(agent, Mapping):
                add(agent.get("agentId"), True)
    for name in non_ck_names:
        add(name)
    for term in extra_terms:
        add(term)

    # ids are kept whole, even when hex-only: they identify a run and are too long to collide by chance
    found.update(i for i in ids if len(i) >= 2 and any(ch.isalnum() for ch in i))
    # a `terms` entry that is itself a long hex string is an id too
    found.update(t for t in _hex_terms(records))
    strong, weak = _classify(found)
    names: List[str] = []
    if identity is not None:
        names = [v for v in (identity.name, identity.email) if isinstance(v, str) and v.strip()]
    return TermSet(frozenset(strong), frozenset(weak), frozenset(n.strip().lower() for n in names))


def _hex_terms(records: Sequence[Mapping]) -> List[str]:
    out = []
    for rec in records:
        if isinstance(rec, Mapping) and isinstance(rec.get("terms"), (list, tuple)):
            out.extend(t.strip() for t in rec["terms"]
                       if isinstance(t, str) and len(t.strip()) >= 16 and _HEX_ONLY.fullmatch(t.strip()))
    return out


def confidential_terms(local_records: Iterable[Mapping], non_ck_names: Iterable[str] = ()) -> Set[str]:
    """Harness Contract seam: every confidential term (lower-cased) for these records and the guard log."""
    from claude_kiro.analytics import guardlog

    ts = build_terms(list(local_records), GitIdentity(None, None), list(non_ck_names), guardlog.leak_terms())
    return set(ts.strong) | set(ts.weak)


# ---------------------------------------------------------------- the scanner


class _Matcher:
    def __init__(self, terms: Union[TermSet, Iterable[str]], identity: Iterable[str]):
        if isinstance(terms, TermSet):
            strong, weak, ident = set(terms.strong), set(terms.weak), set(terms.identity)
        else:
            strong, weak = _classify(terms)
            ident = set()
        ident |= {i.strip().lower() for i in identity if isinstance(i, str) and i.strip()}
        id_strong = {i for i in ident if _is_strong(i)}
        self.strong = self._sub(strong)
        self.weak = self._word(weak)
        self.id_strong = self._sub(id_strong)
        self.id_weak = self._word(ident - id_strong)

    @staticmethod
    def _alt(terms: Iterable[str]) -> Optional[str]:
        ordered = sorted(set(terms), key=len, reverse=True)
        return "|".join(re.escape(t) for t in ordered) if ordered else None

    @classmethod
    def _sub(cls, terms: Iterable[str]) -> Optional["re.Pattern"]:
        alt = cls._alt(terms)
        return re.compile(alt, _I) if alt else None

    @classmethod
    def _word(cls, terms: Iterable[str]) -> Optional["re.Pattern"]:
        alt = cls._alt(terms)
        return re.compile(r"(?<![A-Za-z0-9])(?:" + alt + r")(?![A-Za-z0-9])", _I) if alt else None

    def text(self, text: str, generic: bool = True, exempt: Optional[Tuple[int, int]] = None) -> List[Tuple[str, str]]:
        """(kind, rule) pairs found in `text`, de-duplicated, in a fixed order.

        `exempt` is a span (the machine label of a PR/commit line, D19) skipped for generic patterns only."""
        out: List[Tuple[str, str]] = []
        if self.id_strong is not None and self.id_strong.search(text):
            out.append(("identity", "strong"))
        if self.id_weak is not None and self.id_weak.search(text):
            out.append(("identity", "weak"))
        if self.strong is not None and self.strong.search(text):
            out.append(("term", "strong"))
        if self.weak is not None and self.weak.search(text):
            out.append(("term", "weak"))
        if generic:
            gtext = text if exempt is None else text[:exempt[0]] + " " + text[exempt[1]:]
            for name, kind, rx in GENERIC_PATTERNS:
                if rx.search(gtext):
                    out.append((kind, name))
        return out


class _Pairs(list):
    """A parsed JSON object that keeps duplicate keys, so no value is hidden from the scan."""


def _parse_record(text: str) -> Optional[_Pairs]:
    try:
        data = json.loads(text, object_pairs_hook=_Pairs)
    except (ValueError, RecursionError):
        return None
    if isinstance(data, _Pairs) and any(k == "schema" and v == SCHEMA_SHARED for k, v in data):
        return data
    return None


# D19: the machine label on the "machine:" line of the PR body, or at the end of the commit message / PR title
# line, is a format-validated slug (S13/S14); it is exempt from generic patterns (never from terms).
_MACHINE_LINE = re.compile(
    r"^(?:machine: |ck-stats-batch: \d+ \([^()]*\)(?: \d{4}-\d{2})* )(?P<label>[a-z0-9-]{1,32})$"
)


def _machine_span(line: str) -> Optional[Tuple[int, int]]:
    m = _MACHINE_LINE.match(line)
    return m.span("label") if m else None


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _walk(matcher: _Matcher, name: str, node: Optional[tuple], value: Any, path: str, hits: List[LeakHit]) -> None:
    if isinstance(value, _Pairs):
        for key, child in value:
            fixed: Optional[tuple] = None
            known = False
            if isinstance(key, str) and node is not None:
                if node[0] == "obj" and key in node[1]:
                    fixed, known = node[1][key], True
                elif node[0] == "map" and key in node[1]:
                    fixed, known = node[2], True
            if known:
                shown = key
            else:
                key_hits = matcher.text(str(key))
                for kind, rule in key_hits:
                    hits.append(LeakHit(name, _join(path, "<key>"), kind, rule))
                shown = "<key>" if key_hits else str(key)
            _walk(matcher, name, fixed, child, _join(path, shown), hits)
    elif isinstance(value, list):
        child_node = node[1] if node is not None and node[0] == "list" else None
        for i, item in enumerate(value):
            _walk(matcher, name, child_node, item, f"{path}[{i}]", hits)
    elif isinstance(value, str):
        generic = True
        if node is not None and node[0] == "enum" and value in node[1]:
            return  # a fixed token
        if node is not None and node[0] == "fmt" and node[2].fullmatch(value):
            generic = False  # format-validated: terms only
        for kind, rule in matcher.text(value, generic):
            hits.append(LeakHit(name, path or "<record>", kind, rule))


def scan(files: Mapping[str, str], terms: Union[TermSet, Iterable[str]], identity: Iterable[str] = ()) -> List[LeakHit]:
    """S17: hits in `files` (relative path or pseudo-path -> text). Hits never carry the matched text."""
    matcher = _Matcher(terms, identity)
    hits: List[LeakHit] = []
    for name in sorted(files):
        text = files[name]
        if not isinstance(text, str):
            continue
        record = _parse_record(text) if name.endswith(".json") else None
        if record is not None:
            _walk(matcher, name, _share._TREE, record, "", hits)
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for kind, rule in matcher.text(line, True, _machine_span(line)):
                hits.append(LeakHit(name, f"line {number}", kind, rule))
    return hits


def leak_scan(files: Mapping[str, str], terms: Union[TermSet, Iterable[str]], identity: Iterable[str] = ()) -> List[Dict[str, str]]:
    """Harness Contract seam: `scan` with each hit as `{file, field, kind, rule}`."""
    return [h.as_dict() for h in scan(files, terms, identity)]


def decide(hits: Sequence[LeakHit]) -> Gate:
    """S18: any hit blocks."""
    return Gate(not hits, tuple(hits))


# ---------------------------------------------------------------- inputs read from the machine


def non_ck_workflow_names(home: Path) -> List[str]:
    """Names of non-ck workflow runs under `home`, in memory only. `[]` when unreadable."""
    try:
        names = []
        for path in iter_run_files(Path(home)):
            name = peek_workflow_name(path)
            if isinstance(name, str) and name and name not in ALLOWLISTED_WORKFLOWS:
                names.append(name)
        return sorted(set(names))
    except Exception:  # unreadable home: no names, never an error
        return []


def git_identity(checkout: Path) -> GitIdentity:
    """`git config user.name` and `user.email` as seen from `checkout`."""

    def read(key: str) -> Optional[str]:
        try:
            done = subprocess.run(["git", "-C", str(checkout), "config", key], capture_output=True, text=True,
                                  timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        value = done.stdout.strip()
        return value if done.returncode == 0 and value else None

    return GitIdentity(read("user.name"), read("user.email"))
