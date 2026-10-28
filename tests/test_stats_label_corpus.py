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
        if _get(m, "role") == "other":
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
