"""TC-34: every label the real workflow scripts emit, and every label of the pinned legacy
corpus, parses to a known role through claude_kiro.analytics.parse_label."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
EMIT = HERE / "js" / "emit_labels.mjs"
LEGACY = HERE / "fixtures" / "legacy_labels.json"


def _get(match, key):
    return match[key] if isinstance(match, dict) else getattr(match, key)


def _unmatched(entries):
    """Labels of (workflow, label) pairs whose parse is not a real role."""
    from claude_kiro.analytics import parse_label

    bad = []
    for workflow, label in entries:
        m = parse_label(label, workflow)
        if not _get(m, "matched") or _get(m, "role") == "other":
            bad.append(label)
    return bad


def _check_corpus(entries):
    """Fail naming every label that does not parse."""
    bad = _unmatched(entries)
    assert not bad, f"labels without a role: {bad!r}"


def _emitted():
    proc = subprocess.run(["node", str(EMIT)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_current_labels_all_parse_and_cover_every_role():
    from claude_kiro.analytics import ROLES, parse_label

    corpus = _emitted()
    assert corpus, "emit_labels.mjs produced no labels"
    assert {c["workflow"] for c in corpus} >= {"spec-implement", "spec-create"}
    _check_corpus([(c["workflow"], c["label"]) for c in corpus])
    seen = {_get(parse_label(c["label"], c["workflow"]), "role") for c in corpus}
    missing = (set(ROLES) - {"other"}) - seen
    assert not missing, f"roles never produced by the corpus: {sorted(missing)}"


def test_legacy_labels_all_parse_with_expected_role_and_task():
    from claude_kiro.analytics import parse_label

    legacy = json.loads(LEGACY.read_text())
    labels = {e["label"] for e in legacy}
    assert "merge 3" in labels
    assert any("/" in e["label"] for e in legacy)
    _check_corpus([(e["workflow"], e["label"]) for e in legacy])
    for e in legacy:
        m = parse_label(e["label"], e["workflow"])
        assert _get(m, "role") == e["role"], e["label"]
        assert _get(m, "task") == e["task"], e["label"]


def test_corpus_check_fails_naming_an_unknown_label():
    legacy = json.loads(LEGACY.read_text())
    entries = [(e["workflow"], e["label"]) for e in legacy]
    entries.append(("spec-implement", "frobnicate 99"))
    with pytest.raises(AssertionError, match="frobnicate 99"):
        _check_corpus(entries)


def test_review_wave_label_maps_to_reviewer_with_wave_and_tasks():
    """Task 36 (4.1, NFR-4, 5.2): the batched-review label is a review agent."""
    from claude_kiro.analytics import parse_label

    m = parse_label("review-wave 2 tasks 4 5 7", "spec-implement")
    assert _get(m, "role") == "reviewer"
    assert _get(m, "matched") is True
    assert _get(m, "wave") == 2
    assert list(_get(m, "tasks")) == ["4", "5", "7"]

    # Every review-wave label the real script emits (via the emit_labels
    # wave-merge scenario) parses as a review agent, never `other`, so the
    # W + R bound and the overhead reviewer share count it.
    corpus = _emitted()
    waves = [c["label"] for c in corpus if c["label"].startswith("review-wave ")]
    assert waves, "emit_labels.mjs produced no review-wave label"
    for label in waves:
        m = parse_label(label, "spec-implement")
        assert _get(m, "role") == "reviewer", label
        assert _get(m, "matched") is True, label
        assert isinstance(_get(m, "wave"), int), label
        assert len(list(_get(m, "tasks"))) >= 1, label


def test_malformed_review_wave_labels_stay_other():
    from claude_kiro.analytics import parse_label

    for label in ("review-wave", "review-wave 2", "review-wave 2 tasks",
                  "review-wave x tasks 1 2", "review-wave 2 tasks ",
                  "Review-Wave 2 tasks 1 2"):
        m = parse_label(label, "spec-implement")
        assert _get(m, "matched") is False, label
        assert _get(m, "role") == "other", label


def test_tc137_arbiter_audit_and_unreproduced_labels_parse_to_a_known_role():
    """TC-137 (10.8): every label the real scripts emit, including the arbiter, audit, `arbiter-` step and
    `full gate @<n> (unreproduced landing)` forms, parses to a known role through the real parse_label."""
    import re

    from claude_kiro.analytics import parse_label

    # The forms the workflows name, parsed directly.
    literal = [
        ("spec-implement", "arbiter task 3"),
        ("spec-implement", "arbiter task 3 risky"),
        ("spec-implement", "arbiter drift"),
        ("spec-implement", "audit task 3"),
        ("spec-implement", "full gate @3 (unreproduced landing)"),
    ]
    _check_corpus(literal)

    corpus = [(c["workflow"], c["label"]) for c in _emitted()]
    _check_corpus(corpus)
    labels = [label for _, label in corpus]
    families = {
        "arbiter task <n>": re.compile(r"^arbiter task \d+$"),
        "arbiter task <n> risky": re.compile(r"^arbiter task \d+ risky$"),
        "arbiter <step>": re.compile(r"^arbiter (?!task )\S"),
        "audit task <n>": re.compile(r"^audit task \d+$"),
        "arbiter- step": re.compile(r"^arbiter-"),
        "unreproduced landing": re.compile(r"^full gate @\S+ \(unreproduced landing\)$"),
    }
    absent = [name for name, rx in families.items() if not any(rx.search(l) for l in labels)]
    assert not absent, f"emit_labels.mjs never emits: {absent}"
    for workflow, l in corpus:
        if any(rx.search(l) for rx in families.values()):
            m = parse_label(l, workflow)
            assert _get(m, "matched") and _get(m, "role") != "other", l
