"""P-7: enum mapping. For any enumerated field and any value, `enum_value(field, v)` is in the
field's fixed set or `other`, equals `v` only when `v` is in the set; `model_family` maps canonical
ids and bare aliases to the family and every provider-prefixed, `@`-suffixed, `arn:`/`us.`/`anthropic/`
or custom id to `other`; no noise string is ever copied into a shareable record.

Pure decision seams only (no CLI). Project modules are imported inside test bodies so this file
collects before the implementation exists. Generators are local."""

import copy
import json

from hypothesis import HealthCheck, given, settings, strategies as st

from stats_helpers import CONFIDENTIAL, seeded_local_record

PROPERTIES = {"P-7": "test_p7_enum_value_in_set_or_other"}

SETTINGS = settings(
    derandomize=True,
    max_examples=200,
    database=None,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

FIELDS = ("outcome", "verdict", "role", "model", "effort", "stack", "issue")


def _seams():
    try:
        import claude_kiro.analytics as mod
        mod.enum_value  # noqa: B018
    except (ImportError, AttributeError):
        import claude_kiro.analytics.share as mod
    return mod


def _sets(mod=None):
    mod = mod or _seams()
    return {
        "outcome": frozenset(mod.OUTCOMES),
        "verdict": frozenset(mod.VERDICTS),
        "role": frozenset(mod.ROLES),
        "model": frozenset(mod.MODEL_FAMILIES),
        "effort": frozenset(mod.EFFORTS),
        "stack": frozenset(mod.STACK_HINTS),
        "issue": frozenset(mod.ISSUE_CODES),
    }


# ---------------------------------------------------------------- generators

ENUM_NOISE = [
    "Complete", "completed", "complete ", " complete", "COMPLETE", "halted!", "approve", "approved", "APPROVE",
    "Approve", "request_changes", "request-changes", "Request_Changes", "reviewer ", "Reviewer", "implementers",
    "claude-opus-5-5", "claude-sonnet-5-5[1m]", "claude-haiku-5-5-20261001", "us.anthropic.claude-opus-5-5-v1:0",
    "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-sonnet",
    "claude-opus-5-5@20261001", "anthropic/claude-opus-5-5", "opus", "Opus", "inherited", "inherit", "my-finetune",
    "max", "MAX", "high ", "xhigh!", "medium-ish", "python3", "Python", "rust ", "cobol", "",
    "review_contract_mismatchX", "merge_failed ", "permission_refusal!", "other", "Other", "other ",
]

SURFACE_TEMPLATES = [
    "{t}", "the {t}, then", "{t}!", '"{t}"', "/x/{t}/y", "{t}/y", "./{t}", "../{t}", "~/{t}",
    "C:\\\\{t}\\\\y", "integrate/{t}", "feat/{t}-1", "https://github.com/o/{t}", "git@github.com:o/{t}.git",
    "Name <{t}@b.c>", "wf_{t}",
]


@st.composite
def confidential(draw):
    t = draw(st.sampled_from(CONFIDENTIAL))
    form = draw(st.sampled_from(SURFACE_TEMPLATES))
    case = draw(st.sampled_from([str, str.upper, str.lower, str.title]))
    t = case(t)
    return form.replace("{t}", t)


def enum_noise():
    return st.sampled_from(ENUM_NOISE)


def fixed_set_values(field):
    return st.sampled_from(sorted(_sets()[field]))


@st.composite
def any_value(draw, field):
    kind = draw(st.integers(0, 6))
    if kind == 0:
        return draw(enum_noise())
    if kind == 1:
        return draw(confidential())
    if kind == 2:
        return draw(fixed_set_values(field))
    if kind == 3:
        s = draw(fixed_set_values(field))
        return draw(st.sampled_from([s.upper(), s.title(), s + " ", " " + s, s + "!", s + "s", s[:-1] or "x"]))
    if kind == 4:
        return draw(st.text(max_size=40))
    if kind == 5:
        # a value from another field's fixed set
        other = draw(st.sampled_from(FIELDS))
        return draw(fixed_set_values(other))
    return draw(st.one_of(st.none(), st.integers(), st.booleans(), st.floats(allow_nan=False), st.lists(st.text(max_size=5), max_size=2)))


FAMILIES = ("haiku", "sonnet", "opus")


@st.composite
def canonical_model(draw):
    fam = draw(st.sampled_from(FAMILIES))
    shape = draw(st.integers(0, 6))
    major, minor = draw(st.integers(1, 9)), draw(st.integers(0, 9))
    date = draw(st.sampled_from(["20261001", "20250929", "20240229"]))
    if shape == 0:
        return fam, fam
    if shape == 1:
        return fam, f"claude-{fam}"
    if shape == 2:
        return fam, f"claude-{fam}-{major}-{minor}"
    if shape == 3:
        return fam, f"claude-{fam}-{major}-{minor}[1m]"
    if shape == 4:
        return fam, f"claude-{fam}-{major}-{minor}-{date}"
    if shape == 5:
        return fam, fam.title()
    return fam, f"claude-{fam}-{major}"


@st.composite
def non_family_model(draw):
    fam = draw(st.sampled_from(FAMILIES))
    base = draw(st.sampled_from([f"claude-{fam}-5-5", f"claude-{fam}-5-5-20261001", f"claude-{fam}-4-1"]))
    custom = draw(st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=20).filter(
        lambda s: not any(f in s for f in FAMILIES + ("fable", "claude", "inherit"))))
    forms = [
        f"us.anthropic.{base}-v1:0",
        f"eu.anthropic.{base}-v1:0",
        f"global.anthropic.{base}-v2:0",
        f"anthropic.{base}-v1:0",
        f"{base}@20261001",
        f"{base}@latest",
        f"{fam}@20261001",
        f"anthropic/{base}",
        f"anthropic/{fam}",
        f"openrouter/anthropic/{base}",
        f"arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.{base}-v1:0",
        f"arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/{custom}",
        f"vertex/{base}",
        f"{base}-v1:0",
        f"my-finetune-{custom}",
        f"ft:{base}:acme::{custom}",
        custom,
        f"{base} ",
        f" {base}",
        f"{base}\n",
    ]
    return draw(st.sampled_from(forms))


# ---------------------------------------------------------------- properties

@SETTINGS
@given(data=st.data())
def test_p7_enum_value_in_set_or_other(data):
    mod = _seams()
    sets = _sets(mod)
    field = data.draw(st.sampled_from(FIELDS))
    value = data.draw(any_value(field))
    out = mod.enum_value(field, value)
    allowed = sets[field]
    assert isinstance(out, str)
    assert out in allowed or out == "other", (field, value, out)
    if isinstance(value, str) and value in allowed:
        if field != "model":
            assert out == value, (field, value, out)
    if out == value:
        assert isinstance(value, str) and (value in allowed or value == "other"), (field, value, out)
    if not (isinstance(value, str) and value in allowed) and field != "model":
        assert out == "other", (field, value, out)


@SETTINGS
@given(data=st.data())
def test_p7_fixed_set_members_map_to_themselves(data):
    mod = _seams()
    sets = _sets(mod)
    field = data.draw(st.sampled_from(FIELDS))
    member = data.draw(fixed_set_values(field))
    assert mod.enum_value(field, member) == member


@SETTINGS
@given(pair=canonical_model())
def test_p7_model_family_canonical_ids_and_aliases(pair):
    mod = _seams()
    fam, model = pair
    assert fam in mod.MODEL_FAMILIES
    assert mod.model_family(model) == fam, model
    assert mod.enum_value("model", model) == fam, model


@SETTINGS
@given(model=non_family_model())
def test_p7_model_family_provider_and_custom_ids_are_other(model):
    mod = _seams()
    assert mod.model_family(model) == "other", model
    assert mod.enum_value("model", model) == "other", model


@SETTINGS
@given(model=st.one_of(enum_noise(), confidential(), st.text(max_size=40), st.none(), st.integers()))
def test_p7_model_family_never_copies_input(model):
    mod = _seams()
    out = mod.model_family(model)
    assert out in mod.MODEL_FAMILIES or out == "other"
    if isinstance(model, str) and any(c in model for c in "@:/") :
        assert out == "other", model
    if out == model:
        assert isinstance(model, str) and model in mod.MODEL_FAMILIES


def _leaves(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _leaves(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _leaves(v)
    elif isinstance(obj, str):
        yield obj


def _noisy_record(kind, noise):
    """seeded_local_record with enum leaves replaced by noise strings."""
    rec = copy.deepcopy(seeded_local_record(kind))
    n = len(noise)

    def pick(i):
        return noise[i % n]

    rec["outcome"] = pick(0)
    rec.setdefault("project", {})["stack"] = pick(1)
    for i, a in enumerate(rec.get("agents", [])):
        a["role"] = pick(2 + i)
        a["model"] = pick(6 + i)
    cfg = rec.get("agentsConfig", {})
    for i, role in enumerate(list(cfg)):
        cfg[role] = {"model": pick(10 + i), "effort": pick(12 + i)}
    for i, t in enumerate(rec.get("tasks", [])):
        t["verdict"] = pick(14 + i)
    rec["byModel"] = {pick(16 + i): v for i, v in enumerate(rec.get("byModel", {}).values())}
    rec["byRole"] = {pick(20 + i): v for i, v in enumerate(rec.get("byRole", {}).values())}
    return rec


@SETTINGS
@given(
    kind=st.sampled_from(["spec-implement", "spec-create"]),
    noise=st.lists(st.one_of(enum_noise(), confidential()), min_size=1, max_size=30),
)
def test_p7_no_noise_string_is_copied_into_shareable_record(kind, noise):
    mod = _seams()
    allowed = set()
    for s in _sets(mod).values():
        allowed |= s
    allowed.add("other")
    rec = _noisy_record(kind, noise)
    out = mod.build_shareable(rec, record_id="0123456789abcdef")
    blob = json.dumps(out, sort_keys=True)
    leaves = set(_leaves(out))
    for n in noise:
        if n in allowed or n.strip() == "":
            continue
        assert n not in leaves, f"noise copied as a value or key: {n!r}"
        if len(n) >= 6:
            assert n not in blob, f"noise copied into the record: {n!r}"
