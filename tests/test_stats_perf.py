"""Performance of `ck stats collect` (TC-77, TC-78; NFR performance).

Fixtures are built before the clock starts; only the real CLI subprocess is timed.
"""

import time

import stats_helpers
from stats_helpers import AgentSpec, FakeClaudeHome, fresh_env, git_project


def _agents(count: int, tag: str) -> list:
    return [
        AgentSpec(
            f"task {i}",
            requests=[(f"req_{tag}_{i}_{r}", 1_000 + r, 500, 2_000, 100) for r in range(3)],
        )
        for i in range(count)
    ]


def _timed_collect(env, *args: str):
    t0 = time.monotonic()
    result = stats_helpers.stats("collect", *args, env=env)
    return result, time.monotonic() - t0


def test_collect_one_run_with_250_agents_under_10_seconds():
    """TC-77"""
    with fresh_env() as env:
        project = git_project(env.root, name="proj-0")
        home = FakeClaudeHome(env.claude)
        session = home.session(slug="-work-proj-0", cwd=project)
        run = session.implement_run(agents=_agents(250, "big"))

        result, elapsed = _timed_collect(env, "--run", run.run_id)

        assert result.exit_code == 0, result.output
        assert elapsed < 10, f"collect --run took {elapsed:.1f}s (limit 10s)"


def test_collect_all_100_runs_over_5_projects_under_120_seconds():
    """TC-78"""
    with fresh_env() as env:
        home = FakeClaudeHome(env.claude)
        n = 0
        for p in range(5):
            project = git_project(env.root, name=f"proj-{p}")
            session = home.session(slug=f"-work-proj-{p}", cwd=project)
            for _ in range(20):
                session.implement_run(
                    start=f"2026-10-{1 + n % 28:02d}T09:00:00Z",
                    agents=_agents(20, f"r{n}"),
                    args={"spec": "demo", "root": str(project)},
                )
                n += 1
        assert n == 100

        result, elapsed = _timed_collect(env, "--all")

        assert result.exit_code == 0, result.output
        assert elapsed < 120, f"collect --all took {elapsed:.1f}s (limit 120s)"
