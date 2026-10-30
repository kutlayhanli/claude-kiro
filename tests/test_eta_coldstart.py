"""Cold start: bundled defaults, per-part fallback, missing defaults, the maintainer command (TC-46..TC-50)."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Dict

import pytest

import eta_helpers as h
from eta_history import T, assert_aggregates_only, custom_history, install
from eta_strategies import CREATE_PHASES, DEFAULTS_SCHEMA
from stats_helpers import ck_sub, git_project

pytestmark = pytest.mark.usefixtures("no_network")

NOW = "2026-10-30T15:00:00Z"
REPO = Path(__file__).resolve().parent.parent
PACKAGED = REPO / "src" / "claude_kiro" / "resources" / "eta_defaults.json"
NO_ESTIMATE = "no estimate: no history and no bundled defaults"


def empty_env(tmp_path: Path, **extra: str) -> Dict[str, str]:
    xdg = tmp_path / "xdg"
    xdg.mkdir(parents=True, exist_ok=True)
    return {"XDG_DATA_HOME": str(xdg), "CK_AGENT_SLOTS": "4", **extra}


def tasks():
    return [h.TaskDef(1, model="sonnet"), h.TaskDef(2, model="sonnet", deps=(1,)), h.TaskDef(3, model="sonnet", deps=(2,)),
            h.TaskDef(4, model="sonnet", risk="safety")]


def orbit_repo(tmp_path: Path) -> Path:
    repo = git_project(tmp_path, name="orbit-repo")
    (repo / "specs" / "orbit").mkdir(parents=True)
    (repo / "specs" / "orbit" / "PLAN.md").write_text("# Plan\n")
    return repo


def view_of(env, repo, spec="demo", *args):
    out = h.eta(env, repo, spec, "--json", "--seed", "2", *args, now=NOW)
    assert out.exit_code == 0, out.output
    return json.loads(out.stdout)


# --------------------------------------------------------------------------- TC-46


def test_tc46_empty_store_uses_bundled_defaults_with_low_confidence(tmp_path):
    env = empty_env(tmp_path)
    repo = h.eta_spec(tmp_path / "ledger", tasks())
    out = h.eta(env, repo, "demo", now=NOW)
    assert out.exit_code == 0, out.output
    assert "low confidence: no local history" in out.stdout
    view = view_of(env, repo)
    assert view["confidence"] == "low" and view["basis"]["source"] == "bundled"
    assert view["finish"] is not None


def test_tc46_same_for_a_create_spec(tmp_path):
    env = empty_env(tmp_path)
    repo = orbit_repo(tmp_path)
    out = h.eta(env, repo, "orbit", now=NOW)
    assert out.exit_code == 0, out.output
    assert "low confidence: no local history" in out.stdout
    view = view_of(env, repo, "orbit")
    assert view["runKind"] == "create" and view["confidence"] == "low" and view["basis"]["source"] == "bundled"


# --------------------------------------------------------------------------- TC-47


def titles_and_spec_names():
    names, titles = [], []
    for d in sorted((REPO / "specs").iterdir()):
        if d.is_dir():
            names.append(d.name)
            tm = d / "tasks.md"
            if tm.exists():
                titles += re.findall(r"^### Task [^:\n]+: (.+)$", tm.read_text(), flags=re.M)
    return names, titles


def test_tc47_packaged_defaults_hold_aggregates_only():
    assert PACKAGED.is_file(), f"missing packaged resource {PACKAGED}"
    text = PACKAGED.read_text()
    payload = json.loads(text)
    assert_aggregates_only(payload, DEFAULTS_SCHEMA)
    assert set(payload["phases"]) <= set(CREATE_PHASES)
    names, titles = titles_and_spec_names()
    assert names and titles
    lowered = text.lower()
    for near_miss in names + titles:
        assert near_miss.lower() not in lowered, near_miss
    assert "/" not in text
    assert not re.search(r"\d{4}-\d{2}-\d{2}", text) and not re.search(r"\b(wf_|ag_)", text)


CONFIDENTIAL = "CONFIDENTIAL"
FREE_TEXT = {"title", "reason", "label", "description", "project", "root", "cwd", "branch", "spec", "name", "path", "summary",
             "feature", "slug"}


def taint(node):
    if isinstance(node, dict):
        return {k: (f"{v}-{CONFIDENTIAL}" if k in FREE_TEXT and isinstance(v, str) else taint(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [taint(v) for v in node]
    return node


def test_tc47_generated_defaults_carry_none_of_the_stored_text(tmp_path):
    xdg = h.install_pool(h.POOL_IMPL, tmp_path / "xdg")
    h.install_pool(h.POOL_CREATE, xdg)
    for path in (xdg / "claude-kiro" / "runs").glob("*.json"):
        path.write_text(json.dumps(taint(json.loads(path.read_text())), indent=2))
    assert CONFIDENTIAL in "".join(p.read_text() for p in (xdg / "claude-kiro" / "runs").glob("*.json"))
    out_path = tmp_path / "defaults.json"
    out = ck_sub(*h.DEFAULTS_CMD, "--out", str(out_path), env={"XDG_DATA_HOME": str(xdg)}, cwd=tmp_path)
    assert out.exit_code == 0, out.output
    text = out_path.read_text()
    assert CONFIDENTIAL not in text and CONFIDENTIAL not in out.output
    assert_aggregates_only(json.loads(text))


# --------------------------------------------------------------------------- TC-48


def test_tc48_missing_wait_distributions_come_from_the_bundled_defaults(tmp_path):
    xdg = h.install_pool(h.POOL_IMPL_NOWAIT, tmp_path / "xdg")
    env = {"XDG_DATA_HOME": str(xdg), "CK_AGENT_SLOTS": "4"}
    repo = h.eta_spec(tmp_path / "ledger", tasks())
    view = view_of(env, repo)
    parts = view["basis"]["bundledParts"]
    assert parts and all("wait" in p for p in parts), parts
    assert not any("duration" in p or "minutes" in p for p in parts)
    assert view["basis"]["source"] in ("mixed", "local") and view["basis"]["tasks"] > 100
    text = h.eta(env, repo, "demo", now=NOW).stdout
    for part in parts:
        assert part in text, part


# --------------------------------------------------------------------------- TC-49


@pytest.mark.parametrize("content", [None, "{bad"], ids=["missing-file", "malformed-file"])
def test_tc49_no_history_and_no_defaults_still_shows_the_graph(tmp_path, content):
    path = tmp_path / "defaults.json"
    if content is not None:
        path.write_text(content)
    env = empty_env(tmp_path, **{h.DEFAULTS_ENV: str(path)})
    repo = h.eta_spec(tmp_path / "ledger", tasks())
    out = h.eta(env, repo, "demo", now=NOW)
    assert out.exit_code == 0, out.output
    assert NO_ESTIMATE in out.stdout and "4 open" in out.stdout
    plan = json.loads(ck_sub("plan", "demo", "--json", cwd=repo, env={"TZ": "UTC"}).stdout)
    crit = [str(t) for t in plan["criticalPath"]]
    assert "→".join(crit) in out.stdout.replace(" ", "") or " → ".join(crit) in out.stdout
    view = view_of(env, repo)
    assert view["confidence"] == "none" and view["basis"]["source"] == "none"
    assert view["finish"] is None and view["criticalPath"]["tasks"] == crit and view["open"] == 4


# --------------------------------------------------------------------------- TC-50


def test_tc50_maintainer_command_writes_usable_aggregates(tmp_path):
    xdg = h.install_pool(h.POOL_IMPL, tmp_path / "full")
    h.install_pool(h.POOL_CREATE, xdg)
    target = tmp_path / "D.json"
    out = ck_sub(*h.DEFAULTS_CMD, "--out", str(target), env={"XDG_DATA_HOME": str(xdg)}, cwd=tmp_path)
    assert out.exit_code == 0, out.output
    assert_aggregates_only(json.loads(target.read_text()))
    env = empty_env(tmp_path / "blank", **{h.DEFAULTS_ENV: str(target)})
    repo = h.eta_spec(tmp_path / "ledger", tasks())
    view = view_of(env, repo)
    assert view["basis"]["source"] == "bundled"
    for q in ("p10", "p50", "p90"):
        assert math.isfinite(view["remainingMinutes"][q])
        datetime.fromisoformat(view["finish"][q].replace("Z", "+00:00"))


def test_tc50_below_the_minimum_run_count_nothing_is_written(tmp_path):
    xdg = install(custom_history(tmp_path / "few", lambda i: [T(), T()], runs=3), tmp_path / "xdg")
    target = tmp_path / "D.json"
    out = ck_sub(*h.DEFAULTS_CMD, "--out", str(target), env={"XDG_DATA_HOME": str(xdg)}, cwd=tmp_path)
    assert out.exit_code == 1 and not target.exists() and "No such command" not in out.output
    explicit = ck_sub(*h.DEFAULTS_CMD, "--out", str(target), "--min-runs", "4", env={"XDG_DATA_HOME": str(xdg)}, cwd=tmp_path)
    assert explicit.exit_code == 1 and not target.exists()
    enough = ck_sub(*h.DEFAULTS_CMD, "--out", str(target), "--min-runs", "3", env={"XDG_DATA_HOME": str(xdg)}, cwd=tmp_path)
    assert enough.exit_code == 0, enough.output
    assert_aggregates_only(json.loads(target.read_text()))
