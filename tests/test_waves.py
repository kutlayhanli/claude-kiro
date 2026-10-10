"""Wave planning from tasks.md: Parallel Groups when consistent, Dependencies otherwise."""

import json
import os
from pathlib import Path

from click.testing import CliRunner

from claude_kiro.cli.main import cli
from claude_kiro.waves import parse_parallel_groups, plan_waves


def task(num, deps="None", files=("x.py",), status="Not Started", track="impl"):
    file_lines = "\n".join(f"- `{f}` - change" for f in files)
    return (
        f"### Task {num}: Task {num}\n**Status:** {status}\n**Track:** {track}\n"
        f"**Files:**\n{file_lines}\n\n**Dependencies:** {deps}\n\n---\n\n"
    )


def spec(tmp_path: Path, body: str, groups: str = "") -> Path:
    spec_dir = tmp_path / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    text = "# Implementation Tasks: Demo\n\n## Task Breakdown\n\n" + body
    if groups:
        text += "## Parallel Groups\n" + groups + "\n"
    (spec_dir / "tasks.md").write_text(text)
    return spec_dir


def test_parse_inline_and_bulleted_groups():
    inline = "## Parallel Groups\nWave 1: Task 1 · Wave 2: Tasks 2, 3 (after 1) · Wave 3: Task 4\n"
    assert parse_parallel_groups(inline) == [["1"], ["2", "3"], ["4"]]
    bullets = "## Parallel Groups\n- **Wave 1** (test infrastructure): Task 1\n- **Wave 2**: Tasks 2–4\n- Wave 3: Task 5 (depends on Task 2)\n"
    assert parse_parallel_groups(bullets) == [["1"], ["2", "3", "4"], ["5"]]
    assert parse_parallel_groups("## Task Breakdown\n") is None


def test_consistent_parallel_groups_are_used(tmp_path):
    body = task(1, files=("tests/t.py",), track="test") + task(2, "Task 1", ("a.py",)) + task(3, "Task 1", ("b.py",)) + task(4, "Task 2, Task 3")
    plan = plan_waves(spec(tmp_path, body, "Wave 1: Task 1\nWave 2: Tasks 2, 3\nWave 3: Task 4"))
    assert plan["source"] == "parallel-groups"
    assert plan["waves"] == [["1"], ["2", "3"], ["4"]]
    assert plan["deps"]["4"] == ["2", "3"]
    assert plan["tracks"]["1"] == "test"
    assert plan["warnings"] == []


def test_inconsistent_groups_fall_back_to_dependencies(tmp_path):
    body = task(1) + task(2, "Task 1", ("a.py",)) + task(3, "Task 2", ("b.py",))
    plan = plan_waves(spec(tmp_path, body, "Wave 1: Task 1\nWave 2: Tasks 2, 3"))  # 3 depends on 2, same wave
    assert plan["source"] == "dependencies"
    assert plan["waves"] == [["1"], ["2"], ["3"]]
    assert any("Task 3 (Wave 2) depends on Task 2 (Wave 2)" in w for w in plan["warnings"])


def test_missing_groups_compute_levels_and_split_shared_files(tmp_path):
    body = task(1, files=("base.py",)) + task(2, "Task 1", ("same.py",)) + task(3, "Task 1", ("same.py",)) + task(4, "Task 1", ("other.py",))
    plan = plan_waves(spec(tmp_path, body))
    assert plan["source"] == "dependencies"
    assert plan["waves"] == [["1"], ["2", "4"], ["3"]]  # 2 and 3 both edit same.py
    assert plan["warnings"] == []  # specs since 0.7 have no Parallel Groups; that's normal


def test_done_tasks_are_reported(tmp_path):
    plan = plan_waves(spec(tmp_path, task(1, status="Done") + task(2, "Task 1"), "Wave 1: Task 1\nWave 2: Task 2"))
    assert plan["done"] == ["1"]


def test_ck_waves_json(tmp_path):
    spec(tmp_path, task(1) + task(2, "Task 1", ("a.py",)), "Wave 1: Task 1\nWave 2: Task 2")
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = CliRunner().invoke(cli, ["waves", "demo", "--json"])
        human = CliRunner().invoke(cli, ["waves", "demo"])
    finally:
        os.chdir(old)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["waves"] == [["1"], ["2"]]
    assert "Wave 2: 2" in human.output
