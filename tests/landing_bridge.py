"""Harness for the landing-speed tests: the Node bridge fixture, Hypothesis strategies, and the real-`ck plan` helpers.

- `bridge` (module-scoped fixture): one long-lived `node tests/js/bridge.mjs`; `bridge.ready(...)` calls the
  script's `readyTasks`, `bridge.run(...)` runs the whole workflow against the fake project.
- `dags()`, `subsets()`, `scenarios()`: Hypothesis strategies. Use them with
  `@settings(derandomize=True, database=None, deadline=None)`; none of them needs a function-scoped fixture.
- `git_repo()`, `write_spec()`, `ck_plan()`: drive the real `ck plan` CLI on a real git repo.
"""

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pytest
from click.testing import CliRunner
from hypothesis import strategies as st

BRIDGE_JS = Path(__file__).parent / "js" / "bridge.mjs"
REQUEST_TIMEOUT_S = 120


class BridgeError(RuntimeError):
    """The bridge answered {ok: false, error}; the message is the error text."""


class Bridge:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            ["node", str(BRIDGE_JS)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
        )
        self._stderr: List[str] = []
        # Drain stderr so a chatty node process can never fill the pipe and block.
        self._drain = threading.Thread(target=lambda: self._stderr.extend(iter(self.proc.stderr.readline, "")), daemon=True)
        self._drain.start()

    def stderr_tail(self) -> str:
        return "".join(self._stderr[-50:])

    def request(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """Send one request and return the raw response object (including ok: false)."""
        timer = threading.Timer(REQUEST_TIMEOUT_S, self.proc.kill)
        timer.start()
        try:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
        finally:
            timer.cancel()
        if not line:
            self._drain.join(timeout=2)
            raise BridgeError("bridge process ended without answering: " + self.stderr_tail())
        return json.loads(line)

    def _ok(self, req: Dict[str, Any]) -> Dict[str, Any]:
        out = self.request(req)
        if not out.get("ok"):
            raise BridgeError(out.get("error", "unknown bridge error"))
        return out

    def ready(self, deps: Dict[str, Sequence[str]], done: Iterable[str] = (), exclude: Iterable[str] = (), blocked: Iterable[str] = (), order: Optional[Sequence[str]] = None) -> List[str]:
        req = {"op": "ready", "deps": {k: list(v) for k, v in deps.items()}, "done": list(done), "exclude": list(exclude), "blocked": list(blocked)}
        req["order"] = list(order) if order is not None else list(deps)
        return self._ok(req)["ready"]

    def run(self, project: Dict[str, Any], args: Optional[Dict[str, Any]] = None, slots: Optional[int] = None, clock: Optional[str] = None, exec_ck: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """clock: "virtual" (exact virtual-time sleeps and trace `t`), "real", or None (the bridge default).
        exec_ck: {"cmd": [...], "env": {...}, "cwd": ..., "dry": bool}; fake agents record progress events through the real CLI."""
        req: Dict[str, Any] = {"op": "run", "project": project, "args": args or {}, "slots": slots}
        if clock is not None:
            req["clock"] = clock
        if exec_ck is not None:
            req["execCk"] = exec_ck
        out = self._ok(req)
        out.pop("ok")
        return out

    def create(self, args: Optional[Dict[str, Any]] = None, overrides: Optional[Dict[str, Any]] = None, exec_ck: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run plugin/workflows/spec-create.js with the shared stub agent; returns result, logs, trace, prompts, execLog."""
        req: Dict[str, Any] = {"op": "create", "args": args or {}, "overrides": overrides or {}}
        if exec_ck is not None:
            req["execCk"] = exec_ck
        out = self._ok(req)
        out.pop("ok")
        return out

    def ladder(self, model: Optional[str]) -> List[Any]:
        """The decisions block's `guardLadder(model)`: the models a guard-stopped task is retried on."""
        return self._ok({"op": "ladder", "model": model})["ladder"]

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self._drain.join(timeout=2)
        for stream in (self.proc.stdout, self.proc.stderr):
            with contextlib.suppress(Exception):
                stream.close()


@pytest.fixture(scope="module")
def bridge():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    b = Bridge()
    yield b
    b.close()


# --- strategies -------------------------------------------------------------------------------

@st.composite
def dags(draw, max_tasks: int = 25, dangling: bool = True) -> Dict[str, List[str]]:
    """{task id: [dependency ids]} in tasks.md order (ascending). Ids come from 1..120, non-contiguous and
    multi-digit. Edges only run from a higher to a lower task, so the graph is acyclic; deps may repeat, and
    (with `dangling`) may name tasks that are not in the graph."""
    ids = sorted(draw(st.sets(st.integers(1, 120), min_size=1, max_size=max_tasks)))
    absent = [i for i in range(1, 131) if i not in ids]
    deps: Dict[str, List[str]] = {}
    for pos, tid in enumerate(ids):
        lower = [str(i) for i in ids[:pos]]
        chosen = draw(st.lists(st.sampled_from(lower), max_size=4)) if lower else []  # lists allow duplicates
        if dangling and absent and draw(st.integers(0, 9)) == 0:
            chosen = chosen + [str(draw(st.sampled_from(absent)))]
        deps[str(tid)] = chosen
    return deps


@st.composite
def subsets(draw, ids: Iterable[str]) -> List[str]:
    """A subset of `ids` (a list, in no particular order guarantee beyond the draw)."""
    pool = sorted(set(ids), key=lambda x: (len(x), x))
    return draw(st.lists(st.sampled_from(pool), unique=True, max_size=len(pool))) if pool else []


@st.composite
def scenarios(draw) -> Dict[str, Any]:
    """{'project': fake_project options, 'args': workflow args, 'slots': int | None} for `bridge.run`."""
    deps = draw(dags(max_tasks=8, dangling=False))
    ids = list(deps)
    dur = st.integers(1, 40)
    per_task = lambda strat: {t: draw(strat) for t in draw(subsets(ids))}  # noqa: E731
    project: Dict[str, Any] = {
        "deps": deps,
        "durations": {t: draw(dur) for t in ids},
        "reviewDurations": per_task(dur),
        "mergeDurations": per_task(dur),
        "reviews": {t: draw(st.lists(st.sampled_from(["changes", "approve"]), min_size=1, max_size=2)) for t in draw(subsets(ids))},
        "checkRed": draw(subsets(ids)),
        "failUntil": per_task(st.integers(1, 2)),
    }
    if len(ids) > 2 and draw(st.booleans()):
        later = draw(st.sampled_from(ids[1:]))
        earlier = [i for i in ids if int(i) < int(later)]
        project["relink"] = {"atPlan": draw(st.integers(2, 4)), "deps": {later: draw(st.lists(st.sampled_from(earlier), unique=True, max_size=2))}}
    max_concurrent = draw(st.sampled_from([1, 2, 3, 4, None]))
    args: Dict[str, Any] = {
        "mergeBatch": draw(st.integers(1, 4)),
        "fullGateEvery": draw(st.integers(1, 3)),
        "wave": {t: 1 for t in ids},
    }
    if max_concurrent is not None:
        args["maxConcurrent"] = max_concurrent
    if project["reviews"]:
        args["agents"] = {"reviewer": {"model": "opus", "effort": "high", "enabled": True, "rounds": 2}}
    return {"project": project, "args": args, "slots": draw(st.sampled_from([1, 2, 3, 4, 5, 6, None]))}


# --- the real `ck plan` -----------------------------------------------------------------------

def task_block(num: str, deps: Sequence[str], status: str = "Not Started") -> str:
    """One impl-track task block, shaped like tests/test_waves.py::task()."""
    dep_text = ", ".join(f"Task {d}" for d in deps) if deps else "None"
    return (
        f"### Task {num}: Task {num}\n**Status:** {status}\n**Track:** impl\n"
        f"**Files:**\n- `src/f{num}.py` - change\n\n**Dependencies:** {dep_text}\n\n---\n\n"
    )


def write_spec(directory: Path, deps: Dict[str, Sequence[str]], done: Iterable[str] = ()) -> Path:
    """Write specs/demo/tasks.md under `directory`: one impl block per task in `deps` order, Done ones marked,
    dangling deps written as references to absent numbers, no Parallel Groups section."""
    done = set(done)
    spec_dir = Path(directory) / "specs" / "demo"
    spec_dir.mkdir(parents=True, exist_ok=True)
    body = "".join(task_block(t, ds, "Done" if t in done else "Not Started") for t, ds in deps.items())
    (spec_dir / "tasks.md").write_text("# Implementation Tasks: Demo\n\n## Task Breakdown\n\n" + body)
    return spec_dir


@contextlib.contextmanager
def git_repo():
    """A throwaway git repo in a TemporaryDirectory (per example; tmp_path is function-scoped and Hypothesis refuses it)."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com"}
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q", tmp], check=True, env=env, capture_output=True)
        (Path(tmp) / "README.md").write_text("demo\n")
        subprocess.run(["git", "-C", tmp, "add", "-A"], check=True, env=env, capture_output=True)
        subprocess.run(["git", "-C", tmp, "commit", "-q", "-m", "init"], check=True, env=env, capture_output=True)
        yield Path(tmp)


def ck_plan(directory: Path, exclude: Iterable[str] = ()) -> Dict[str, Any]:
    """Run the real `ck plan demo --json --exclude ...` in `directory` (through CliRunner) and parse its JSON."""
    from claude_kiro.cli.main import cli

    args = ["plan", "demo", "--json"]
    excluded = ",".join(exclude)
    if excluded:
        args += ["--exclude", excluded]
    old = os.getcwd()
    os.chdir(directory)
    try:
        result = CliRunner().invoke(cli, args)
    finally:
        os.chdir(old)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)
