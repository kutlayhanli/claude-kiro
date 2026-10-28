"""P-5 and P-24: a shareable record holds no confidential term, and the shareable vocabulary never trips the leak scanner.

Both properties call the pure decision seams of `claude_kiro.analytics` (`build_shareable`, `SHAREABLE_FIELDS`,
`confidential_terms`, `leak_scan`), never the CLI. Project modules are imported inside the test bodies so the file
collects before the implementation exists.
"""

import copy
import inspect
import json
import re
from typing import Any, Dict, List, Set

from hypothesis import HealthCheck, given, settings, strategies as st

from stats_helpers import CONFIDENTIAL, SEEDED_AGENT_IDS, SEEDED_RUN_IDS, SEEDED_SESSION_IDS, SEEDED_TIMESTAMP, flatten_paths, seeded_local_record

PROPERTIES = {
    "P-5": "test_p5_shareable_record_holds_no_confidential_term",
    "P-24": "test_p24_vocabulary_never_trips_the_scanner",
}

SETTINGS = dict(max_examples=200, derandomize=True, database=None, deadline=None,
                suppress_health_check=list(HealthCheck))

DAY_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
ID_SHAPE = re.compile(r"\b(?:wf_[A-Za-z0-9_-]{6,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|a5eedc0ffee[0-9a-f]{5})\b", re.I)

# top-level keys that give the record its identity as a ck run; planting them would make it a non-ck record
STRUCTURAL = {"schema", "workflow", "workflowName", "status"}

# mappings whose keys are data (model names, role names, phase names, task ids), so planting into the keys is realistic;
# every other nested key is schema-structural and is left alone
DATA_KEY_MAPS = {"byModel", "byRole", "byPhase", "byTask", "agentsConfig", "phases"}

# surface forms a confidential string can take inside a free-text slot
FORMS = [
    "{}", "{} ", " {}", "prefix {} suffix", "'{}'", '"{}"', "({})", "{}/sub", "dir/{}", "{}.py",
    "{}: failed", "https://host.example/{}", "/home/dana/{}/x",
]

CORES = st.sampled_from(sorted(set(CONFIDENTIAL) | {"Zephyr-Payroll", "ZEPHYR-PAYROLL", "zephyr payroll", "Globex", "Initech", "dana", "scully"}))
PLANT = st.tuples(st.sampled_from(FORMS), CORES, st.sampled_from(["same", "lower", "upper"]))
LABELS = st.one_of(
    st.from_regex(r"[a-z0-9-]{1,32}", fullmatch=True),
    st.sampled_from(["a", "-", "0", "a" * 31, "a" * 32, "0" * 32, "-" * 32, "a-" * 16, "zephyr-payroll"[:14]]),
)
RECORD_IDS = st.from_regex(r"[0-9a-f]{16}", fullmatch=True)


def render(plant) -> tuple:
    form, core, case = plant
    shown = core.lower() if case == "lower" else core.upper() if case == "upper" else core
    return form.format(shown), core


def mutate(obj: Any, data, planted: List[str], top: bool = False, parent: str = "") -> Any:
    """Copy of `obj` with a drawn subset of its string leaves (and the keys of data mappings) replaced by planted confidential strings."""
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if top and k in STRUCTURAL:
                out[k] = v
                continue
            new_k = k
            if isinstance(k, str) and not top and parent in DATA_KEY_MAPS and data.draw(st.booleans()):
                new_k, core = render(data.draw(PLANT))
                planted.append(core)
                if new_k in out:
                    new_k = k
            out[new_k] = mutate(v, data, planted, parent=k)
        return out
    if isinstance(obj, list):
        # list items are schema-structural rows (e.g. spec-implement phases {group, tasks, minutes}): plant values, never keys
        return [mutate(v, data, planted, parent="") for v in obj]
    if isinstance(obj, str) and data.draw(st.booleans()):
        value, core = render(data.draw(PLANT))
        planted.append(core)
        return value
    return obj


def strings_of(obj: Any) -> Set[str]:
    if isinstance(obj, dict):
        out: Set[str] = set()
        for k, v in obj.items():
            out |= {k} | strings_of(v)
        return out
    if isinstance(obj, list):
        out = set()
        for v in obj:
            out |= strings_of(v)
        return out
    return {obj} if isinstance(obj, str) else set()


def whole_word(term: str, text: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", text, re.I) is not None


def build(analytics, local: dict, record_id: str, label, issue_codes) -> dict:
    # design S13/S14: build_shareable takes issue_codes; inspect the signature rather than hiding a TypeError
    params = inspect.signature(analytics.build_shareable).parameters
    if "issue_codes" in params:
        return analytics.build_shareable(local, record_id=record_id, machine_label=label, issue_codes=list(issue_codes))
    rec = dict(analytics.build_shareable(local, record_id=record_id, machine_label=label))
    rec["issues"] = sorted(issue_codes)
    return rec


def path_allowed(path: str, patterns: List[str]) -> bool:
    for pat in patterns:
        rx = "".join("[^.\\[\\]]+" if part == "*" else re.escape(part) for part in re.split(r"(\*)", pat))
        if re.fullmatch(rx, path):
            return True
    # an empty container is a leaf without `[]`: accept the bare path when SHAREABLE_FIELDS lists items or members under it
    for pat in patterns:
        if pat.startswith(path + "[]") or pat.startswith(path + "."):
            return True
    return False


@settings(**SETTINGS)
@given(kind=st.sampled_from(["spec-implement", "spec-create"]), data=st.data(), record_id=RECORD_IDS)
def test_p5_shareable_record_holds_no_confidential_term(kind, data, record_id):
    import claude_kiro.analytics as analytics

    planted: List[str] = []
    local = mutate(seeded_local_record(kind), data, planted, top=True)
    local = copy.deepcopy(local)
    original = seeded_local_record(kind)
    terms = analytics.confidential_terms([local])
    issues = data.draw(st.lists(st.sampled_from(sorted(analytics.ISSUE_CODES)), unique=True, max_size=6))
    # no salt assertion: the local record holds no salt and build_shareable has no salt input (design 1.8), so there is nothing to plant
    # no machine label: a confidential label is caught by the leak scan, not by build_shareable (design S13/S14)
    shareable = build(analytics, local, record_id, None, issues)

    allowed = list(analytics.SHAREABLE_FIELDS)
    for path in flatten_paths(shareable):
        assert path_allowed(path, allowed), f"path {path!r} is not in SHAREABLE_FIELDS"

    text = json.dumps(shareable, indent=2, sort_keys=True)
    lowered = text.lower()
    for term in terms:
        assert not whole_word(term, text), f"confidential term {term!r} found in the shareable record"
    for core in set(planted) | set(CONFIDENTIAL):
        assert core.lower() not in lowered, f"planted string {core!r} found in the shareable record"
    pseudonyms = {local.get("projectPseudonym"), (local.get("project") or {}).get("pseudonym"),
                  original.get("projectPseudonym"), (original.get("project") or {}).get("pseudonym")}
    for pseudo in pseudonyms - {None, ""}:
        assert pseudo not in text, f"pseudonym {pseudo!r} found in the shareable record"
    ids = {local.get("runId"), local.get("sessionId"), original["runId"], original["sessionId"], *SEEDED_RUN_IDS, *SEEDED_SESSION_IDS, *SEEDED_AGENT_IDS}
    ids |= {a.get("agentId") for a in local.get("agents", []) if isinstance(a, dict)}
    for ident in {i for i in ids if isinstance(i, str) and len(i) >= 8}:
        assert ident.lower() not in lowered, f"id {ident!r} found in the shareable record"
    assert not DAY_DATE.search(text), f"a day-precision date is in the shareable record: {DAY_DATE.search(text).group(0)}"
    assert SEEDED_TIMESTAMP not in text and SEEDED_TIMESTAMP[:10] not in text
    assert not ID_SHAPE.search(text.replace(record_id, "")), "a run, session or agent id shape is in the shareable record"


def fixed_vocabulary(analytics) -> Dict[str, Set[str]]:
    enums: Dict[str, Set[str]] = {
        "ISSUE_CODES": set(analytics.ISSUE_CODES),
        "MODEL_FAMILIES": set(analytics.MODEL_FAMILIES),
        "OUTCOMES": set(analytics.OUTCOMES),
        "VERDICTS": set(analytics.VERDICTS),
        "EFFORTS": set(analytics.EFFORTS),
        "STACK_HINTS": set(analytics.STACK_HINTS),
        "ROLES": set(analytics.ROLES),
        "ALLOWED_WORKFLOWS": set(analytics.ALLOWED_WORKFLOWS),
        "MISSING": {"result", "runfile", "agent_entries", "transcripts", "meta", "timing", "ck_version"},
        "VARIANT": {"default", "iso", "other"},
        "PHASE": {"plan", "wave", "tasks", "final_gate", "report", "requirements", "design_test_plan", "task_tracks", "link",
                  "adversarial_review", "revise", "consistency_check", "other"},
        "CONFIG_ROLES": {"implementer", "test_writer", "reviewer", "reviewer_risky", "fixer", "orchestrator", "resolver"},
        "COST_SOURCE": {"priced_from_transcripts", "estimate"},
        "WORKFLOW_EXTRA": {"spec-plan", "other"},
        "SCHEMA": {"ck-run-analytics/1"},
    }
    fields: Set[str] = set()
    for pat in analytics.SHAREABLE_FIELDS:
        fields |= {seg for seg in re.split(r"[.\[\]]+", pat) if seg and seg != "*"}
    enums["FIELD_NAMES"] = fields
    return {k: {str(m) for m in v} for k, v in enums.items()}


def generic_hits(analytics, text: str) -> list:
    return analytics.leak_scan({"vocabulary.txt": text}, [])


@settings(**SETTINGS)
@given(kind=st.sampled_from(["spec-implement", "spec-create"]), data=st.data(), record_id=RECORD_IDS, label=LABELS)
def test_p24_vocabulary_never_trips_the_scanner(kind, data, record_id, label):
    import claude_kiro.analytics as analytics

    # the fixed sets themselves
    for name, members in fixed_vocabulary(analytics).items():
        for member in sorted(members):
            assert len(member) < 32, f"{name} member {member!r} is 32 characters or longer"
            assert generic_hits(analytics, member) == [], f"{name} member {member!r} trips a generic pattern"
            assert generic_hits(analytics, f"{member}: {member}") == []

    # the fixed template words of the commit message and PR text
    token = data.draw(st.from_regex(r"[0-9a-f]{8}", fullmatch=True))
    kinds = data.draw(st.lists(st.sampled_from(sorted(analytics.ALLOWED_WORKFLOWS)), min_size=1, max_size=3))
    recs = []
    for k in kinds:
        base = seeded_local_record("spec-create" if k == "spec-create" else "spec-implement")
        base["workflowName"] = k  # includes spec-implement-iso, so the iso variant reaches pr_text
        recs.append(build(analytics, base, "0123456789abcdef", None, []))
    pr_label = data.draw(st.one_of(st.none(), LABELS))
    text = analytics.pr_text(recs, pr_label, token)
    assert text["branch"] == f"ck-stats-batch-{token}"
    for word in ("ck-stats-batch", "count", "kinds", "months", "machine", text["branch"]):
        assert generic_hits(analytics, word) == [], f"template word {word!r} trips a generic pattern"
    for part in ("commit", "title", "body"):
        for line in text[part].splitlines():
            assert generic_hits(analytics, line) == [], f"{part} line {line!r} trips a generic pattern"

    # a built shareable record: every ISSUE_CODES member in issues, any valid machine label
    local = seeded_local_record(kind)
    shareable = build(analytics, local, record_id, label, sorted(analytics.ISSUE_CODES))
    assert set(analytics.ISSUE_CODES) <= set(shareable["issues"])
    body = json.dumps(shareable, indent=2, sort_keys=True) + "\n"
    hits = analytics.leak_scan({f"analytics/runs/{shareable['month']}/{record_id}.json": body}, [])
    assert hits == [], f"leak_scan with no terms reported {hits}"
