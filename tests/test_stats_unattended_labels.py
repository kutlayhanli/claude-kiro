"""TC-132 (stats side, 14.7): `ck stats` attributes the labels the unattended workflow adds to their roles.

The agent rows come from the committed `tests/fixtures/unattended/stats-labels-record.json`; the test writes them as
a real run (run file + transcripts) into a fake Claude home with an isolated stats home, then runs the real
`ck stats collect` and `ck stats show` subprocesses. Nothing of the project's own code is mocked.
"""

import json
from pathlib import Path

import pytest

from stats_helpers import (
    AgentSpec, FakeClaudeHome, fresh_env, git_project, implement_result, load_record, stats,
)

FIXTURE = Path(__file__).parent / "fixtures" / "unattended" / "stats-labels-record.json"
SPEC = json.loads(FIXTURE.read_text())
RID = SPEC["runId"]


@pytest.fixture
def env():
    with fresh_env() as e:
        yield e


def _collect(env) -> dict:
    home = FakeClaudeHome(env.claude)
    proj = git_project(env.root, "git@github.com:acme-corp/ledger-a.git", name="proj")
    s = home.session("-proj", cwd=proj)
    agents = [AgentSpec(a["label"], model=a["model"], phase=a["phase"], started_at_ms=1_791_795_600_000 + i * 120_000)
              for i, a in enumerate(SPEC["agents"])]
    s.implement_run(RID, name=SPEC["name"], result=implement_result(), agents=agents)
    out = stats("collect", "--run", RID, env=env)
    assert out.exit_code == 0, out.output
    return load_record(env.xdg, RID)


def _rows(rec: dict) -> dict:
    return {a["label"]: a for a in rec["agents"]}


def _show(env) -> dict:
    out = stats("show", RID, "--json", env=env)
    assert out.exit_code == 0, out.output
    return json.loads(out.stdout)


def test_tc132_fixture_has_every_label_of_the_task():
    labels = [a["label"] for a in SPEC["agents"]]
    assert len(labels) == len(set(labels)) == 14
    assert {a["role"] for a in SPEC["agents"] if a["new"]} == {"test-writer", "reviewer", "gate"}


def test_tc132_collect_keeps_every_agent(env):
    rec = _collect(env)
    assert sorted(_rows(rec)) == sorted(a["label"] for a in SPEC["agents"])


@pytest.mark.parametrize("entry", [a for a in SPEC["agents"] if a["new"]], ids=lambda a: a["label"])
def test_tc132_new_label_is_counted_under_its_role(env, entry):
    row = _rows(_collect(env))[entry["label"]]
    assert row["role"] == entry["role"], (entry["label"], row["role"])
    assert row["matched"] is True, entry["label"]


@pytest.mark.parametrize("entry", [a for a in SPEC["agents"] if not a["new"]], ids=lambda a: a["label"])
def test_tc132_existing_label_keeps_its_role(env, entry):
    row = _rows(_collect(env))[entry["label"]]
    assert row["role"] == entry["role"], (entry["label"], row["role"])
    assert row["matched"] is True, entry["label"]


def test_tc132_show_counts_agents_under_the_new_roles(env):
    rec = _collect(env)
    expected: dict = {}
    for a in SPEC["agents"]:
        expected[a["role"]] = expected.get(a["role"], 0) + 1
    got: dict = {}
    for row in rec["agents"]:
        got[row["role"]] = got.get(row["role"], 0) + 1
    assert got == expected
    shown = _show(env)
    assert shown.get("agents") is None or len(shown["agents"]) == len(SPEC["agents"])
    out = stats("show", RID, env=env)
    assert out.exit_code == 0, out.output
    assert "other" not in got
