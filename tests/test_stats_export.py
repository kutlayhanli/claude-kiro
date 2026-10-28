"""`ck stats export`: shareable records, schema document, issue codes (TC-44..TC-54, TC-80, TC-81).

Every run goes through the real `ck stats collect` and `ck stats export --to <kiro checkout>` as subprocesses, with a
fake Claude home, a real git checkout (local bare remote) and nothing mocked that ck owns.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest

from stats_helpers import (
    CONFIDENTIAL,
    AgentSpec,
    FakeClaudeHome,
    TaskSpec,
    all_bytes,
    ck_sub,
    create_result,
    fake_claude_drafter,
    flatten_paths,
    fresh_env,
    git_project,
    implement_result,
    kiro_checkout,
    seeded_home,
    stats,
)

pytestmark = pytest.mark.usefixtures("no_network")

REPO = Path(__file__).resolve().parent.parent
SCHEMA_MD = REPO / "analytics" / "SCHEMA.md"
UPSTREAM_URL = "https://github.com/angelsen/claude-kiro.git"


# --------------------------------------------------------------------------- fixtures and helpers


@pytest.fixture
def w():
    with fresh_env() as env:
        home = FakeClaudeHome(env.claude)
        proj = git_project(env.root)
        checkout, bare = kiro_checkout(env.root)
        yield SimpleNamespace(env=env, home=home, proj=proj, checkout=checkout, bare=bare, xdg=env.xdg)


def add_run(w, kind: str = "implement", *, start: str, **kw) -> str:
    """One session holding one run; returns the run id."""
    s = w.home.session("-work-acme", cwd=w.proj)
    if kind == "implement":
        run = s.implement_run(start=start, args={"spec": "demo", "root": str(w.proj)}, **kw)
    else:
        run = s.create_run(start=start, args={"feature": "demo", "root": str(w.proj)}, **kw)
    return run.run_id


def collect(w, env: Dict[str, str] = None) -> None:
    r = stats("collect", "--all", env=env or w.env)
    assert r.exit_code == 0, r.output


def export(w, *extra: str, checkout: Path = None, env: Dict[str, str] = None):
    return stats("export", "--to", str(checkout or w.checkout), *extra, env=env or w.env)


def exported_files(checkout: Path, suffix: str = ".json") -> List[Path]:
    root = Path(checkout) / "analytics"
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix == suffix) if root.exists() else []


def exported_records(checkout: Path) -> List[Tuple[Path, dict]]:
    return [(p, json.loads(p.read_text())) for p in exported_files(checkout)]


def local_records(w) -> List[dict]:
    root = Path(w.xdg) / "claude-kiro" / "runs"
    return [json.loads(p.read_text()) for p in sorted(root.glob("*.json"))] if root.exists() else []


def local_ids(w) -> List[str]:
    out: List[str] = []
    for rec in local_records(w):
        out += [str(rec.get("runId") or ""), str(rec.get("sessionId") or "")]
        out += [str(a.get("agentId") or "") for a in rec.get("agents", [])]
    return [i for i in out if i]


def alias_of(model: str) -> str:
    m = re.match(r"^(?:claude-)?(haiku|sonnet|opus|fable)(?:-[0-9][0-9a-z-]*)?(?:\[1m\])?$", model.lower())
    return m.group(1) if m else "other"


def non_git_listing(root: Path) -> List[str]:
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*") if ".git" not in p.relative_to(root).parts)


def shareable_fields() -> Tuple[str, ...]:
    try:
        from claude_kiro.analytics import SHAREABLE_FIELDS
    except ImportError:
        from claude_kiro.analytics.share import SHAREABLE_FIELDS
    return tuple(SHAREABLE_FIELDS)


def schema_section(name: str) -> str:
    text = SCHEMA_MD.read_text()
    m = re.search(rf"^##\s+{re.escape(name)}\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    assert m, f"analytics/SCHEMA.md has no '## {name}' section"
    return m.group(1)


def documented_paths() -> List[str]:
    paths = []
    for line in schema_section("Fields").splitlines():
        if line.strip().startswith(("-", "*", "|")):
            m = re.search(r"`([^`]+)`", line)
            if m:
                paths.append(m.group(1))
    return paths


def _doc_regex(path: str) -> "re.Pattern[str]":
    return re.compile("^" + re.escape(path).replace(r"\*", r"[^.\[\]]+") + "$")


def assert_documented(records: List[dict]) -> None:
    """Every flattened path of every record matches a path documented in analytics/SCHEMA.md (7.7)."""
    docs = documented_paths()
    regexes = [_doc_regex(d) for d in docs]
    for rec in records:
        for p in flatten_paths(rec):
            ok = any(r.match(p) for r in regexes)
            if not ok:  # an empty container leaf is documented by its children
                ok = any(r.match(p + "[]") or r.pattern.startswith("^" + re.escape(p) + r"\.") for r in regexes)
            assert ok, f"exported path {p!r} is not documented in analytics/SCHEMA.md"


def three_runs(w) -> Dict[str, str]:
    """Three runs in two months -> {label: run id}."""
    ids = {
        "oct1": add_run(w, start="2026-10-12T09:00:00Z"),
        "oct2": add_run(w, start="2026-10-20T09:00:00Z"),
        "sep": add_run(w, "create", start="2026-09-20T08:00:00Z"),
    }
    return ids


# --------------------------------------------------------------------------- TC-44, TC-45


def test_tc44_three_files_under_the_right_months(w):
    three_runs(w)
    collect(w)
    r = export(w)
    assert r.exit_code == 0, r.output
    files = exported_files(w.checkout)
    assert len(files) == 3
    for f in files:
        assert f.parent.parent == w.checkout / "analytics" / "runs"
        assert re.fullmatch(r"\d{4}-\d{2}", f.parent.name)
    assert sorted(f.parent.name for f in files) == ["2026-09", "2026-10", "2026-10"]
    ids = [f.stem for f in files]
    assert len(set(ids)) == 3
    others = [i.lower() for i in local_ids(w)]
    assert others
    for rid in ids:
        for i in range(len(rid) - 5):
            window = rid[i : i + 6].lower()
            assert not any(window in o for o in others), f"record id {rid} shares {window!r} with a run/session/agent id"
    assert_documented([json.loads(f.read_text()) for f in files])


def test_tc45_record_fields_match_the_local_record(w):
    three_runs(w)
    collect(w)
    assert export(w).exit_code == 0
    locals_by_wf = {}
    for rec in local_records(w):
        locals_by_wf.setdefault(rec["workflow"], []).append(rec)
    assert sorted(locals_by_wf) == ["spec-create", "spec-implement"]
    seen = 0
    for path, d in exported_records(w.checkout):
        assert d["schema"] == "ck-run-analytics/1"
        assert d["month"] == path.parent.name
        assert d["id"] == path.stem
        assert d["workflow"] in ("spec-implement", "spec-create")
        assert d["outcome"]
        assert isinstance(d["counts"], dict) and isinstance(d["durations"], dict)
        local = next(r for r in locals_by_wf[d["workflow"]] if r["startTime"][:7] == d["month"])
        assert d["durations"]["wall_minutes"] == pytest.approx(local["wallMs"] / 60000, abs=0.1)
        assert d["counts"]["agents"] == len(local["agents"])
        if d["workflow"] == "spec-implement":
            n = len(local["tasks"])
            assert d["counts"]["tasks"] == n
            assert [t["index"] for t in d["tasks"]] == list(range(1, n + 1))
        # tokens and cost by model alias and role
        by_model = d["tokens"]["by_model"]
        assert set(by_model) == {alias_of(m) for m in local["byModel"]}
        for alias, usage in by_model.items():
            assert usage["input"] == sum(v["input"] for m, v in local["byModel"].items() if alias_of(m) == alias)
            assert usage["output"] == sum(v["output"] for m, v in local["byModel"].items() if alias_of(m) == alias)
        assert d["tokens"]["by_role"]
        run_cost = next(c for c in local["costs"] if c.get("scope") == "run")
        assert d["cost"]["usd"] == pytest.approx(run_cost["usd"], rel=1e-6, abs=1e-9)
        assert d["cost"]["source"] == run_cost["source"]
        assert d["cost"]["pricing_date"] == run_cost["pricingDate"]
        assert set(d["cost"]["by_model"]) == set(by_model)
        assert d["cost"]["by_role"]
        seen += 1
    assert seen == 3


# --------------------------------------------------------------------------- TC-46


def test_tc46_no_confidential_byte_leaves_the_machine(tmp_path):
    with fresh_env() as env:
        home, terms = seeded_home(env.root)
        checkout, _bare = kiro_checkout(env.root)
        env["CLAUDE_CONFIG_DIR"] = str(home.root)
        r = stats("collect", "--all", env=env)
        assert r.exit_code == 0, r.output
        r = stats("export", "--to", str(checkout), env=env)
        assert r.exit_code == 0, r.output
        files = exported_files(checkout)
        assert files
        assert exported_files(checkout, ".md") == []

        salt = json.loads((Path(env.config) / "config.json").read_text())["stats_salt"]
        runs_dir = Path(env.xdg) / "claude-kiro" / "runs"
        secrets: List[str] = list(terms) + [salt]
        for p in sorted(runs_dir.glob("*.json")):
            rec = json.loads(p.read_text())
            secrets += [str(rec.get(k)) for k in ("projectPseudonym", "runId", "sessionId") if rec.get(k)]
            secrets += [str(a["agentId"]) for a in rec.get("agents", []) if a.get("agentId")]
        secrets = [s for s in secrets if len(s) >= 6]
        assert salt in secrets
        blob = all_bytes(checkout / "analytics").decode("utf-8", "replace").lower()
        for s in secrets:
            assert s.lower() not in blob, f"leaked: {s!r}"
        for c in CONFIDENTIAL:
            assert c.lower() not in blob
        assert not re.search(r"\d{4}-\d{2}-\d{2}", blob)
        assert not re.search(r"\d{2}:\d{2}:\d{2}", blob)
        assert not re.search(r"\d{4}-\d{2}-\d{2}t", blob)
        assert_documented([json.loads(f.read_text()) for f in files])


# --------------------------------------------------------------------------- TC-47


def test_tc47_unknown_enum_values_read_other(w):
    result = implement_result([TaskSpec(task="1", verdict="meh")], outcome="exploded-acme")
    result["agents"]["implementer"] = "my-finetune/turbo"
    agents = [
        AgentSpec("task 1", model="us.anthropic.claude-opus-5-5-v1:0"),
        AgentSpec("review 1", model="my-finetune"),
        AgentSpec("land 1", model="claude-opus-5-5"),
    ]
    add_run(w, start="2026-10-12T09:00:00Z", result=result, agents=agents)
    collect(w)
    r = export(w)
    assert r.exit_code == 0, r.output
    recs = exported_records(w.checkout)
    assert len(recs) == 1
    d = recs[0][1]
    assert d["outcome"] == "other"
    assert d["verdicts"].get("other", 0) >= 1 and "meh" not in d["verdicts"]
    assert any(v.get("effort") == "other" for v in d["config"].values())
    assert set(d["tokens"]["by_model"]) == {"other", "opus"}
    blob = all_bytes(w.checkout / "analytics").decode("utf-8", "replace").lower()
    for s in ("exploded-acme", "meh", "turbo", "my-finetune", "us.anthropic.claude-opus-5-5-v1:0", "claude-opus-5-5"):
        assert s not in blob, s
    assert_documented([d])


# --------------------------------------------------------------------------- TC-48


def test_tc48_machine_label(w):
    run_id = add_run(w, start="2026-10-12T09:00:00Z")
    collect(w)
    config = Path(w.env.config) / "config.json"

    assert ck_sub("agents", "set", "run.machine_label", "work-laptop", env=w.env).exit_code == 0
    assert export(w).exit_code == 0
    (rec,) = [d for _p, d in exported_records(w.checkout)]
    assert rec["machine"] == "work-laptop"

    before = config.read_bytes()
    for bad in ("Work Laptop", "a" * 33):
        r = ck_sub("agents", "set", "run.machine_label", bad, env=w.env)
        assert r.exit_code != 0, bad
        assert config.read_bytes() == before

    long_slug = "work-laptop-" + "x" * 20
    assert len(long_slug) == 32
    assert ck_sub("agents", "set", "run.machine_label", long_slug, env=w.env).exit_code == 0
    r = export(w, "--re-export", run_id)
    assert r.exit_code == 0, r.output
    assert "leak" not in r.output.lower()
    (rec,) = [d for _p, d in exported_records(w.checkout)]
    assert rec["machine"] == long_slug

    assert ck_sub("agents", "set", "run.machine_label", "none", env=w.env).exit_code == 0
    r = export(w, "--re-export", run_id)
    assert r.exit_code == 0, r.output
    (rec,) = [d for _p, d in exported_records(w.checkout)]
    assert "machine" not in rec


def test_tc48_no_label_means_no_machine_field(w):
    add_run(w, start="2026-10-12T09:00:00Z")
    collect(w)
    assert export(w).exit_code == 0
    (rec,) = [d for _p, d in exported_records(w.checkout)]
    assert "machine" not in rec


# --------------------------------------------------------------------------- TC-49


def test_tc49_schema_document_is_the_published_allowlist(w):
    assert SCHEMA_MD.is_file(), "analytics/SCHEMA.md is missing"
    docs = documented_paths()
    assert docs
    assert set(docs) == set(shareable_fields())

    never = schema_section("Never exported").lower()
    for category in ("spec name", "title", "summar", "finding", "label", "path", "branch", "url", "pseudonym", "id", "prompt",
                     "email", "account", "date", "timestamp", "free text"):
        assert category in never, f"'## Never exported' does not name {category!r}"

    leak = schema_section("Leak check").lower()
    assert "whole word" in leak or "whole-word" in leak
    assert "digit" in leak and "hex" in leak

    three_runs(w)
    collect(w)
    assert export(w).exit_code == 0
    assert_documented([d for _p, d in exported_records(w.checkout)])


# --------------------------------------------------------------------------- TC-50, TC-80, TC-81


def test_tc50_second_export_writes_nothing_and_reexport_replaces_one(w):
    ids = three_runs(w)
    collect(w)
    assert export(w).exit_code == 0
    before = {p: p.read_bytes() for p in exported_files(w.checkout)}
    assert len(before) == 3

    r = export(w)
    assert r.exit_code == 0
    assert {p: p.read_bytes() for p in exported_files(w.checkout)} == before

    r = export(w, "--re-export", ids["sep"])
    assert r.exit_code == 0, r.output
    after = {p: p.read_bytes() for p in exported_files(w.checkout)}
    assert len(after) == 3
    added = set(after) - set(before)
    removed = set(before) - set(after)
    assert len(added) == 1 and len(removed) == 1
    (new,), (old,) = tuple(added), tuple(removed)
    assert new.parent.name == old.parent.name == "2026-09"
    assert new.stem != old.stem
    for p in set(before) & set(after):
        assert after[p] == before[p]


def test_tc80_reexport_is_deterministic_apart_from_the_id(w):
    run_id = add_run(w, start="2026-10-12T09:00:00Z")
    collect(w)
    assert export(w).exit_code == 0
    assert export(w, "--re-export", run_id).exit_code == 0
    ((_p1, first),) = exported_records(w.checkout)
    assert export(w, "--re-export", run_id).exit_code == 0
    ((_p2, second),) = exported_records(w.checkout)
    assert first["id"] != second["id"]
    first.pop("id")
    second.pop("id")
    assert first == second


def test_tc81_deleted_unsubmitted_file_is_exported_again(w):
    three_runs(w)
    collect(w)
    assert export(w).exit_code == 0
    before = {p: p.read_bytes() for p in exported_files(w.checkout)}
    victim = next(p for p in before if p.parent.name == "2026-09")
    victim.unlink()
    r = export(w)
    assert r.exit_code == 0, r.output
    assert "1 record" in r.output
    after = {p: p.read_bytes() for p in exported_files(w.checkout)}
    assert len(after) == 3
    (new,) = set(after) - set(before)
    assert new.parent.name == "2026-09" and new.stem != victim.stem
    for p in set(before) - {victim}:
        assert after[p] == before[p]
    state = (Path(w.xdg) / "claude-kiro" / "state.json").read_text()
    assert new.stem in state and victim.stem not in state


def test_tc81_submitted_record_is_not_exported_again(w):
    three_runs(w)
    collect(w)
    assert export(w).exit_code == 0
    before = {p: p.read_bytes() for p in exported_files(w.checkout)}
    victim = next(p for p in before if p.parent.name == "2026-09")
    # A submitted record's files live only on the PR branch. Mark it submitted in the ledger and drop its file
    # from this branch, as the checkout looks after a real submit.
    state_path = Path(w.xdg) / "claude-kiro" / "state.json"
    ledger = json.loads(state_path.read_text())
    ledger.setdefault("submitted", {})[victim.stem] = {
        "pr_url": "https://github.com/kutlayhanli/claude-kiro/pull/101", "branch": "ck-stats-batch-abcd1234",
        "submitted_at": "2026-10-13T00:00:00Z", "withheld": [],
    }
    state_path.write_text(json.dumps(ledger))
    victim.unlink()
    r = export(w)
    assert r.exit_code == 0, r.output
    assert "nothing new" in r.output.lower()
    assert not victim.exists()
    assert {p: p.read_bytes() for p in exported_files(w.checkout)} == {p: b for p, b in before.items() if p != victim}


# --------------------------------------------------------------------------- TC-51, TC-52


def test_tc51_refuses_anything_but_the_claude_kiro_checkout(w):
    add_run(w, start="2026-10-12T09:00:00Z")
    collect(w)
    (w.env.root / "upstream").mkdir()
    upstream, _ = kiro_checkout(w.env.root / "upstream", UPSTREAM_URL)
    no_remote = git_project(w.env.root, None, name="no-remote")
    plain = w.env.root / "plain"
    plain.mkdir()
    for target in (upstream, no_remote, plain):
        listing = non_git_listing(target)
        r = export(w, checkout=target)
        assert r.exit_code != 0, target
        assert r.output.strip(), "a refusal message is expected"
        assert non_git_listing(target) == listing
        assert exported_files(target) == []
    assert exported_files(w.checkout) == []


def test_tc52_nothing_new_says_so(w):
    add_run(w, start="2026-10-12T09:00:00Z")
    collect(w)
    assert export(w).exit_code == 0
    r = export(w)
    assert r.exit_code == 0
    assert "nothing new" in r.output.lower()


# --------------------------------------------------------------------------- TC-53


def test_tc53_no_network_and_no_drafter_without_a_tty(w, no_network):
    run_id = add_run(w, start="2026-10-12T09:00:00Z")
    bin_dir = w.env.root / "bin"
    drafter = fake_claude_drafter(bin_dir, "A drafted note.")
    env = dict(w.env)
    env["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]

    assert stats("collect", "--all", env=env).exit_code == 0
    assert stats("show", env=env).exit_code == 0
    r = export(w, env=env)
    assert r.exit_code == 0, r.output
    r = export(w, "--note", "--re-export", run_id, env=env)
    assert r.exit_code == 0, r.output
    assert exported_files(w.checkout, ".md") == []

    log = no_network
    assert not log.exists() or log.read_text() == ""
    assert drafter.calls() == []


# --------------------------------------------------------------------------- TC-54


def issue_run(w) -> str:
    tasks = [
        TaskSpec(task="1", attempts=2, escalations=[{"stage": "retry", "model": "opus"}]),
        TaskSpec(task="2", wave=2, ok=False, reason="merge failed: still conflicting after 3 resolver rounds"),
        TaskSpec(task="3", wave=2, ok=False, reason="review: blocking findings remain",
                 findings=[{"severity": "blocking", "issue": "x", "file": "f.py"}], verdict="changes"),
        TaskSpec(task="4", wave=2, ok=False, reason="merge failed: no commits", attempts=2),
    ]
    result = implement_result(tasks, full_gates=[{"afterMerges": 1, "green": False}], outcome="blocked")
    return add_run(w, start="2026-10-12T09:00:00Z", result=result)


def test_tc54_issue_codes_are_derived_and_stable(w):
    run_id = issue_run(w)
    add_run(w, start="2026-09-01T09:00:00Z")  # a clean run
    collect(w)
    assert export(w).exit_code == 0
    by_month = {p.parent.name: d for p, d in exported_records(w.checkout)}
    first = by_month["2026-10"]["issues"]
    assert by_month["2026-09"]["issues"] == []
    expected = {"escalated_to_opus", "gate_red_after_merge", "resolver_rounds_exceeded", "retry_no_commits"}
    assert expected <= set(first)
    assert first == sorted(set(first))

    assert export(w, "--re-export", run_id).exit_code == 0
    again = {p.parent.name: d for p, d in exported_records(w.checkout)}["2026-10"]["issues"]
    assert again == first
