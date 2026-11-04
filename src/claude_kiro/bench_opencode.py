"""Prepare an OpenCode bench arm: assemble the `spec-implement` args and install the harness probe.

The Python bench runner already owns the clone, the arm order, the results and scoring. For an
OpenCode arm it still has to build the object `/spec/implement` would have passed to the workflow.
That is what `workflow_args` does: it asks the clone's `ck` for the plan and the role settings, picks
the integration branch exactly like the skill does, and returns the JSON-safe args object.

`prepare_probe` writes the three files the probe path needs inside the clone (and excludes them from
git), so `opencode run --standalone` can call the `workflow` tool without any human:
`.bench-args.json`, `.opencode/plugins/claude-kiro.js` and `.opencode/plugins/bench-probe.js`.
The probe's `bench_run` tool polls the native run record and returns its path.

This module is pure stdlib and never starts an OpenCode session or a model.
"""

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence, Tuple

from .bench import BenchError

ARGS_FILE = ".bench-args.json"
STATE_FILE = ".bench-state.json"
EXCLUDE_ENTRIES = (".opencode/agents/", ".opencode/ck/", "AGENTS.md", ARGS_FILE, STATE_FILE)

_UNAVAILABLE_RE = re.compile(
    r"no such (?:command|option)|unknown option|unexpected extra argument|invalid value",
    re.I,
)


def _run(argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(list(argv), cwd=str(cwd), env=dict(env), capture_output=True, text=True)
    except OSError as e:
        raise BenchError(f"`{shlex.join(argv)}` could not run in {cwd}: {e}") from e


def _detail(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout + proc.stderr).strip() or f"exit {proc.returncode}"


def _ck_json(
    ck_cmd: Sequence[str],
    clone: Path,
    env: Mapping[str, str],
    args: Sequence[str],
    label: str,
) -> Any:
    """Run one `ck` command and parse its JSON; any failure is a BenchError naming the command."""
    argv = [*ck_cmd, *args]
    proc = _run(argv, clone, env)
    if proc.returncode != 0:
        raise BenchError(f"{label} failed in {clone} (exit {proc.returncode}):\n{_detail(proc)}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise BenchError(
            f"{label} did not print JSON in {clone}: {e}\n"
            f"command: {shlex.join(argv)}\noutput: {proc.stdout.strip()[:2000]}"
        ) from e


# ---------------------------------------------------------------- integration branch


def _spec_name(spec: str) -> str:
    return Path(str(spec).rstrip("/")).name


def _default_target(clone: Path, worktree_dir: Any, spec_name: str) -> Path:
    base = Path(str(worktree_dir)) if worktree_dir else Path(".ck") / "worktrees"
    return (base if base.is_absolute() else clone / base) / f"{spec_name}-integration"


def _resolve_path(clone: Path, raw: Any) -> Path:
    path = Path(str(raw))
    return path if path.is_absolute() else clone / path


def _from_json(text: str) -> Tuple[Any, Any]:
    """(branch, path) from a `ck worktree integration --json` object or list, or (None, None)."""
    data = json.loads(text)
    if isinstance(data, dict):
        items = data.get("outcomes") or [data]
    elif isinstance(data, list):
        items = data
    else:
        return None, None
    for item in items:
        if isinstance(item, dict):
            return item.get("branch"), item.get("path")
    return None, None


def _from_line(text: str) -> Tuple[Any, Any]:
    """(branch, path) from `CREATED integration <path> (integrate/<spec>) - from <base>`."""
    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    branch = None
    match = re.search(r"\((integrate/[^)]+)\)", line)
    if match:
        branch = match.group(1)
    tokens = line.split()
    path = tokens[2] if len(tokens) >= 3 and tokens[1] == "integration" else None
    return branch, path


def _current_branch(clone: Path, env: Mapping[str, str]) -> str:
    proc = _run(["git", "-C", str(clone), "rev-parse", "--abbrev-ref", "HEAD"], clone, env)
    branch = proc.stdout.strip()
    return branch if proc.returncode == 0 and branch and branch != "HEAD" else "main"


def _integration_target(
    clone: Path,
    spec_name: str,
    ck_cmd: Sequence[str],
    env: Mapping[str, str],
    worktree_dir: Any,
) -> Tuple[str, Path]:
    """`ck worktree integration` as the skill runs it, falling back to a direct merge into the clone.

    The current `ck` prints a human line; a future one may grow `--json`, so try that shape first
    and parse it when it is there. Only an *unavailable* command falls back: a real failure (a git
    error, a busy worktree) is raised, because merging into the bench branch would not be the same run.
    """
    base = [*ck_cmd, "worktree", "integration", spec_name]
    worktree = _default_target(clone, worktree_dir, spec_name)
    branch = f"integrate/{spec_name}"

    json_proc = _run([*base, "--json"], clone, env)
    if json_proc.returncode == 0:
        try:
            parsed_branch, parsed_path = _from_json(json_proc.stdout)
        except json.JSONDecodeError:
            parsed_branch, parsed_path = None, None
        if parsed_branch or parsed_path:
            target = _resolve_path(clone, parsed_path) if parsed_path else worktree
            return str(parsed_branch or branch), target

    plain_proc = _run(base, clone, env)
    if plain_proc.returncode == 0:
        parsed_branch, parsed_path = _from_line(plain_proc.stdout)
        target = _resolve_path(clone, parsed_path) if parsed_path else worktree
        return str(parsed_branch or branch), target

    detail = _detail(plain_proc)
    if _UNAVAILABLE_RE.search(detail):
        return _current_branch(clone, env), clone
    raise BenchError(
        f"`{shlex.join(base)}` failed in {clone} (exit {plain_proc.returncode}):\n{detail}"
    )


# ---------------------------------------------------------------- the workflow args


def workflow_args(
    clone: Path,
    spec: str,
    ck_cmd: Sequence[str],
    env: Mapping[str, str],
    *,
    interactive: bool = False,
) -> Dict[str, Any]:
    """The `spec:spec-implement` args the implement skill would have launched, for one bench clone.

    Runs `ck plan <spec> --json` and `ck agents --json` in CLONE, then creates or reuses the
    integration worktree with `ck worktree integration`. Raises BenchError when either command fails.
    """
    clone = Path(clone).resolve()
    plan = _ck_json(ck_cmd, clone, env, ["plan", spec, "--json"], "ck plan")
    if not isinstance(plan, dict):
        raise BenchError(f"ck plan returned {type(plan).__name__}, not an object, for {spec!r} in {clone}")

    resolved = _ck_json(ck_cmd, clone, env, ["agents", "--json"], "ck agents")
    if not isinstance(resolved, dict):
        raise BenchError(f"ck agents returned {type(resolved).__name__}, not an object, in {clone}")
    roles = resolved.get("roles") or {}
    if not isinstance(roles, dict):
        raise BenchError(f"ck agents returned a non-object 'roles' value in {clone}")
    runtime = resolved.get("runtime") or {}
    run = roles.get("run") or {}

    spec_name = str(plan.get("spec") or _spec_name(spec))
    worktree_dir = plan.get("worktreeDir")
    into, target_dir = _integration_target(clone, spec_name, ck_cmd, env, worktree_dir)

    args: Dict[str, Any] = {
        "spec": spec_name,
        "brief": str(clone / ".opencode" / "ck" / "spec-implement-brief.md"),
        "root": str(clone),
        "worktreeDir": worktree_dir,
        "into": into,
        "targetDir": str(target_dir),
        "wave": plan.get("wave") or {},
        "titles": plan.get("titles") or {},
        "tracks": plan.get("tracks") or {},
        "risk": plan.get("risk") or {},
        "complexity": plan.get("complexity") or {},
        "deps": plan.get("deps") or {},
        "remaining": plan.get("remaining") or [],
        "agentSlots": runtime.get("agentSlots"),
        "finishFirst": run.get("finish_first", True),
        "interactive": bool(interactive),
        "maxRetries": 1,
        "onFailure": "continue",
        "maxConcurrent": run.get("max_concurrent"),
        "fullGateEvery": 10,
        "mergeBatch": 4,
        "agents": roles,
    }
    try:
        json.dumps(args)
    except (TypeError, ValueError) as e:  # ck output was not JSON-safe even though it parsed
        raise BenchError(f"could not make the workflow args JSON-safe for {clone}: {e}") from e
    return args


# ---------------------------------------------------------------- the probe files


def _exclude(clone: Path, entries: Sequence[str]) -> None:
    """Append ENTRIES to .git/info/exclude once each, so generated probe files never dirty the tree."""
    path = clone / ".git" / "info" / "exclude"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text().splitlines() if path.exists() else []
    present = {line.strip() for line in existing}
    missing = [entry for entry in entries if entry not in present]
    if not missing:
        return
    with path.open("a", encoding="utf-8") as fh:
        if existing and existing[-1].strip():
            fh.write("\n")
        fh.write("\n".join(missing) + "\n")


def prepare_probe(
    clone: Path,
    package_root: Path,
    args: Mapping[str, Any],
    state_dir: Path,
) -> Path:
    """Write the probe files into CLONE and return the path of the args file it reads.

    - `.bench-args.json`: the exact workflow args
    - `.bench-state.json`: where the plugin writes `<state>/runs/*/record.json`
    - `.opencode/plugins/claude-kiro.js`: re-exports the package entry (OpenCode loads TS directly)
    - `.opencode/plugins/bench-probe.js`: the `bench_run` tool
    """
    clone = Path(clone).resolve()
    package_root = Path(package_root).resolve()
    state_dir = Path(state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)

    args_path = clone / ARGS_FILE
    try:
        payload = json.dumps(dict(args), indent=2)
    except (TypeError, ValueError) as e:
        raise BenchError(f"bench args are not JSON-safe: {e}") from e
    args_path.write_text(payload + "\n", encoding="utf-8")
    (clone / STATE_FILE).write_text(
        json.dumps({"stateDir": str(state_dir)}, indent=2) + "\n", encoding="utf-8"
    )

    source = package_root / "scripts" / "bench-probe.mjs"
    if not source.is_file():
        raise BenchError(f"the bench probe script is missing: {source}")

    plugins = clone / ".opencode" / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    entry = json.dumps(str(package_root / "src" / "index.ts"))
    (plugins / "claude-kiro.js").write_text(f"export {{ default }} from {entry}\n", encoding="utf-8")
    shutil.copyfile(source, plugins / "bench-probe.js")

    _exclude(clone, EXCLUDE_ENTRIES)
    return args_path
