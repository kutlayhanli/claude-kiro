"""`ck run`: start a whole-spec /spec:implement run headless and unattended.

A spec run takes hours; it should not stall on a permission prompt. This starts
`claude -p` in the main checkout in auto permission mode with no prompts, inside
Claude Code's OS-level Bash sandbox: every shell command runs with the home
directory hidden (toolchains apart), credential files and credential-shaped
environment variables removed, and the network limited to `run.allowed_domains`.
Sandboxed commands are auto-allowed, so agents can type whatever shell they need
(`cd wt && ...`, `FOO=1 python ...`) without an allow list; the sandbox, not
command-text rules, is the boundary. It keeps the process open until the
workflow finishes. Run it from the main checkout, not from a worktree:
worktree-isolated sessions refuse commands they cannot prove stay inside that
worktree, and the workflow makes its own task worktrees anyway.

Nobody watches an unattended run, so it carries guards: the sandbox (mandatory;
`--unsandboxed` is an explicit, loudly warned opt-out), a spend cap, a turn cap,
a few deny rules for destructive git and for credential files, and
`ck worktree land` refusing branches that change .claude/**, .mcp.json or git
hooks (files that would run outside the sandbox later).

CLAUDE_CODE_SUBPROCESS_ENV_SCRUB is removed from the child environment: on Claude
Code 2.1.29x it makes <repo>/.git/worktrees read-only for every shell command
(anthropics/claude-code#101546) and forces the permission mode to default
(#51258). `sandbox.credentials` does the same job for sandboxed commands.

Claude Code docs these settings follow (checked October 2026):
  https://code.claude.com/docs/en/cli-reference     --max-budget-usd, --max-turns, --disallowedTools, --settings
  https://code.claude.com/docs/en/permissions       rule syntax; a Bash deny rule matches the command text as written
  https://code.claude.com/docs/en/sandboxing        sandbox.* keys, "Protect credentials", bubblewrap + socat on Linux/WSL2
  https://code.claude.com/docs/en/settings-reference  sandbox.credentials.files / .envVars, strictAllowlist (--settings ok)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import click

from .. import agents as ag
from .worktree import _spec_dir

# --- SECURITY -----------------------------------------------------------------

PERMISSION_MODE = "auto"

# Tools the orchestrating session, the workflow's agents and the /spec:implement skill use.
# Read-only tools are allowed everywhere: the brief and the workflow live in the plugin
# directory, outside the project; deny rules keep credential files out. Edits are allowed
# only inside the project (see file_rules); an Edit rule covers Write and NotebookEdit too.
# Bash needs no rule: sandboxed commands are auto-allowed (autoAllowBashIfSandboxed).
SESSION_TOOLS = ("Workflow", "Agent", "Read", "Glob", "Grep", "TodoWrite", "TaskCreate", "TaskUpdate", "SendMessage")

# Credential files and directories. Hidden from sandboxed commands (sandbox.credentials.files)
# and from the Read/Edit tools (deny rules); `ck doctor --unattended` reports the readable ones.
CREDENTIAL_DIRS = ("~/.ssh", "~/.aws", "~/.config/gcloud", "~/.config/gh", "~/.azure", "~/.kube")
CREDENTIAL_FILES = ("~/.docker/config.json", "~/.netrc", "~/.git-credentials", "~/.npmrc", "~/.pypirc")
CREDENTIAL_PATHS = CREDENTIAL_DIRS + CREDENTIAL_FILES

# git forms nobody in a spec run needs and that destroy work or leave the machine. Deny rules
# hold in every permission mode and win over allow rules; they match the command text as
# written, so each is listed bare and with `-C <dir>`. A backstop for what agents normally
# type (`sh -c 'git push'` is not matched): the sandbox's network allowlist and credential
# removal are what stop a push. ck's own git (land, worktree removal) runs as a subprocess.
_GIT_DENIED = ("push", "reset --hard", "clean", "worktree remove --force", "worktree remove -f")
DISALLOWED_TOOLS = (
    [f"Bash(git {g} *)" for g in _GIT_DENIED] + [f"Bash(git {g})" for g in _GIT_DENIED]
    + [f"Bash(git -C * {g} *)" for g in _GIT_DENIED] + [f"Bash(git -C * {g})" for g in _GIT_DENIED]
    + [f"{tool}({path})" for path in (*(f"{d}/**" for d in CREDENTIAL_DIRS), *CREDENTIAL_FILES, "**/.env", "**/.env.*")
       for tool in ("Read", "Edit")]
    + ["Edit(**/.claude/settings*.json)", "WebFetch"]
)

ENV = {
    # Keep the process open until the background workflow finishes.
    "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0",
}
# Removed from the child environment if the user's shell sets them (see the module docstring).
ENV_UNSET = ("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB",)

# Environment variable names that look like credentials. Each one present when `ck run`
# starts is unset inside the sandbox (sandbox.credentials.envVars, mode deny). This only
# affects sandboxed shell commands: Claude Code itself keeps ANTHROPIC_API_KEY,
# CLAUDE_CODE_OAUTH_TOKEN etc. to authenticate.
CREDENTIAL_ENV = re.compile(
    r"TOKEN|SECRET|KEY|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|COOKIE|^AWS_|^GH_|^GITHUB_|DATABASE_URL|_DSN$|^SSH_AUTH_SOCK$",
    re.IGNORECASE,
)
# Names that match the pattern but hold no secret, and that tools need inside the sandbox.
CREDENTIAL_ENV_KEEP = frozenset({"KEYTIMEOUT"})

DEFAULT_BUDGET_USD = 40.0
DEFAULT_MAX_TURNS = 400

SANDBOX_TOOLS = ("bwrap", "socat")
APT_INSTALL = "sudo apt-get install bubblewrap socat"
# The sandbox hides the home directory from shell commands. Toolchains live there too
# (uv and ck in ~/.local, caches in ~/.cache, nvm, cargo...), so these stay readable.
# None of them should hold credentials; ~/.npmrc and ~/.pypirc deliberately aren't here.
SANDBOX_HOME_READABLE = ("~/.local", "~/.cache", "~/.config/claude-kiro", "~/.config/git", "~/.gitconfig",
                         "~/.nvm", "~/.npm", "~/.cargo", "~/.rustup", "~/.pyenv", "~/.bun")
# Package caches written by `uv sync`, `npm install`; the repo itself is writable by default.
SANDBOX_HOME_WRITABLE = ("~/.cache", "~/.npm")


def file_rules(project: Path, worktree_dir: Optional[Path] = None) -> List[str]:
    """Edit allow rules: the project, plus the task worktree directory if it lies outside it.

    `//` marks an absolute path in a permission rule (a single `/` is relative to the
    settings source). Covers Edit, Write and NotebookEdit.
    """
    dirs = [project.resolve()]
    if worktree_dir is not None:
        wd = worktree_dir.resolve()
        if not wd.is_relative_to(dirs[0]):
            dirs.append(wd)
    return [f"Edit(/{d}/**)" for d in dirs]


def credential_env_names(environ: Mapping[str, str]) -> List[str]:
    """Names (never values) of credential-shaped variables in `environ`."""
    return sorted(n for n in environ if CREDENTIAL_ENV.search(n) and n not in CREDENTIAL_ENV_KEEP)


def sandbox_settings(project: Path, allowed_domains: List[str], worktree_dir: Optional[Path] = None,
                     environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """`--settings` JSON for an unattended run: the Bash sandbox on, no way around it.

    Key names follow https://code.claude.com/docs/en/sandboxing and the sandbox rows of
    https://code.claude.com/docs/en/settings-reference. Paths are absolute or `~/`:
    a relative path in --settings would not resolve against the project. strictAllowlist
    and credentials are honoured from --settings (not from a repository's settings files).
    """
    project = project.resolve()
    extra = []
    if worktree_dir is not None and not worktree_dir.resolve().is_relative_to(project):
        extra = [str(worktree_dir.resolve())]  # the project itself is writable by default
    env_names = credential_env_names(os.environ if environ is None else environ)
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,  # exit at startup instead of silently running unsandboxed
            "allowUnsandboxedCommands": False,  # ignore dangerouslyDisableSandbox retries
            "autoAllowBashIfSandboxed": True,
            "filesystem": {
                "denyRead": ["~/"],
                "allowRead": [str(project), *extra, *SANDBOX_HOME_READABLE],
                "allowWrite": [*extra, *SANDBOX_HOME_WRITABLE],
            },
            "network": {
                "allowedDomains": list(allowed_domains),
                "strictAllowlist": True,  # refuse other hosts instead of asking (nobody can answer)
            },
            # denyRead ~/ already hides these; listing them also blocks a repository's
            # settings from re-opening them with allowRead (a --settings lock).
            "credentials": {
                "files": [{"path": p, "mode": "deny"} for p in CREDENTIAL_PATHS],
                "envVars": [{"name": n, "mode": "deny"} for n in env_names],
            },
        }
    }


def missing_sandbox_tools() -> List[str]:
    return [t for t in SANDBOX_TOOLS if not shutil.which(t)]


def readable_credentials() -> List[str]:
    """Credential files and directories under HOME that exist and this user can read (existence only)."""
    home = Path.home()
    found = []
    for rel in CREDENTIAL_PATHS:
        path = home / rel[2:]
        if path.exists() and os.access(path, os.R_OK):
            found.append(rel)
    return found


def trust_warning(project: Path) -> Optional[str]:
    """Claude Code skips a project's settings.local.json (hooks, allow rules) in an untrusted workspace."""
    data_file = Path.home() / ".claude.json"
    try:
        data = json.loads(data_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return None
    resolved = project.resolve()
    for path in (resolved, *resolved.parents):  # trusting a parent directory trusts what is below it
        entry = projects.get(str(path))
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted"):
            return None
    return (f"{project} is not a trusted workspace in ~/.claude.json: Claude Code skips the project's "
            "settings.local.json (ck hooks, allow rules) there. Open `claude` in it once and accept the trust dialog.")


def sandbox_refusal(missing: List[str]) -> str:
    return (f"Claude Code's Bash sandbox needs {' and '.join(SANDBOX_TOOLS)} on PATH; missing: {', '.join(missing)}. "
            f"`ck run` only runs sandboxed: install them ({APT_INSTALL}; Ubuntu 24.04+ also needs bubblewrap "
            "user namespaces allowed in AppArmor, see https://code.claude.com/docs/en/sandboxing), "
            "or pass --unsandboxed to run without it (not recommended).")


UNSANDBOXED_WARNING = ("UNSANDBOXED run (--unsandboxed): shell commands can read every file this user can, see every "
                       "environment variable (API keys, tokens), and reach any host. Only the auto-mode classifier "
                       "and a few deny rules stand in the way.")


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


def _linked_worktree(cwd: Path) -> bool:
    git_dir, common = _git(cwd, "rev-parse", "--absolute-git-dir"), _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return bool(git_dir and common) and Path(git_dir).resolve() != Path(common).resolve()


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


@click.command(context_settings={"ignore_unknown_options": True})
@click.argument("spec")
@click.argument("extra", nargs=-1, type=click.UNPROCESSED)
@click.option("--model", default="sonnet", show_default=True, help="Model of the orchestrating session (it only launches and reports)")
@click.option("--effort", default="low", show_default=True, help="Effort of the orchestrating session")
@click.option("--budget", type=click.FloatRange(min=0, min_open=True), default=DEFAULT_BUDGET_USD, show_default=True,
              help="Stop the run once estimated API spend (subagents included) reaches this many USD")
@click.option("--max-turns", type=click.IntRange(min=1), default=DEFAULT_MAX_TURNS, show_default=True,
              help="Stop after this many turns of the orchestrating session")
@click.option("--unsandboxed", is_flag=True,
              help="Run without Claude Code's Bash sandbox (not recommended: shell commands see your files, "
                   "environment and network). Without it, `ck run` refuses to start when bwrap or socat is missing")
@click.option("--dry-run", is_flag=True, help="Print the command instead of running it")
@click.option("--json", "as_json", is_flag=True, help="With --dry-run: print the command as JSON")
@click.option("--output-format", type=click.Choice(["text", "json", "stream-json"]), default="text", show_default=True,
              help="claude -p output format; json/stream-json write stdout to the log and stderr to <log>.stderr (used by ck bench)")
@click.option("--session-id", default=None, help="Session UUID for the claude session (used by ck bench to find its transcripts)")
@click.option("--log", "log_path", type=click.Path(dir_okay=False), default=None, help="Log file (default .claude/ck-runs/<spec>-<time>.log)")
def run(spec: str, extra: tuple, model: str, effort: str, budget: float, max_turns: int, unsandboxed: bool,
        dry_run: bool, as_json: bool, output_format: str, session_id: str, log_path: str):
    """Run every task of SPEC unattended: headless, no permission prompts, sandboxed.

    \b
    Arguments after `--` go to /spec:implement, e.g.
      ck run auth -- --escalate sonnet,opus
    Agent models come from `ck agents` as usual. Output goes to
    .claude/ck-runs/<spec>-<time>.log; follow it with `tail -f`.
    `ck doctor --unattended` checks this machine for unattended runs.
    """
    cwd = Path.cwd()
    if _linked_worktree(cwd):
        click.echo("❌ Run `ck run` from the main checkout, not from a worktree: a worktree-isolated session refuses "
                   "the commands the workflow needs, and the workflow creates its own task worktrees.")
        sys.exit(2)
    name = _spec_dir(cwd, spec).name  # exits 2 if the spec has no tasks.md
    warnings: List[str] = []
    missing = missing_sandbox_tools()
    if missing and not unsandboxed:
        click.echo(f"❌ {sandbox_refusal(missing)}")
        sys.exit(2)
    sandboxed = not unsandboxed
    try:
        from ..worktrees import WorktreeError, main_root, worktrees_dir
        wt_dir: Optional[Path] = worktrees_dir(main_root(cwd), name)
    except WorktreeError:
        wt_dir = None
    if wt_dir is not None and (wt_dir.resolve() / "x").is_relative_to(cwd.resolve() / ".claude"):
        warnings.append(f"Task worktrees go to {wt_dir}, under .claude/, which Claude Code's Bash sandbox keeps "
                        "read-only: agents cannot commit or test there. Set \"worktree_dir\" in specs/ck.json "
                        "(default .ck/worktrees) once this spec's old worktrees are landed or removed.")
    settings = None
    if sandboxed:
        try:
            domains = ag.resolve(cwd)["roles"]["run"]["allowed_domains"]
        except ag.AgentConfigError as e:
            click.echo(f"❌ {e}")
            sys.exit(2)
        settings = sandbox_settings(cwd, domains, wt_dir)
    else:
        warnings.append(UNSANDBOXED_WARNING)
        creds = readable_credentials()
        if creds:
            warnings.append(f"Readable credentials on this machine: {', '.join(creds)}. Without the sandbox only "
                            "Read/Edit deny rules keep them from the agents; shell commands can read them.")
        names = credential_env_names(os.environ)
        if names:
            warnings.append(f"{len(names)} credential-shaped environment variable(s) are visible to shell commands "
                            "(sandbox.credentials only applies inside the sandbox).")
    trust = trust_warning(cwd)
    if trust:
        warnings.append(trust)

    prompt = " ".join([f"/spec:implement {name} all", *extra])
    argv = [
        "claude", "-p", prompt,
        "--permission-mode", PERMISSION_MODE,
        "--permission-prompts", "none",
        "--allowedTools", ",".join(list(SESSION_TOOLS) + file_rules(cwd, wt_dir)),
        "--disallowedTools", ",".join(DISALLOWED_TOOLS),
        "--max-budget-usd", _number(budget),
        "--max-turns", str(max_turns),
        "--model", model,
        "--effort", effort,
    ]
    if settings is not None:
        argv += ["--settings", json.dumps(settings, separators=(",", ":"))]
    if output_format != "text":
        argv += ["--output-format", output_format] + (["--verbose"] if output_format == "stream-json" else [])
    if session_id:
        argv += ["--session-id", session_id]
    env = dict(ENV)
    isolation = "sandbox" if sandboxed else "none"
    log = Path(log_path).absolute() if log_path else cwd / ".claude" / "ck-runs" / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    # A machine-readable result must not be interleaved with stderr or with our own header.
    err = Path(f"{log}.stderr") if output_format != "text" else None
    if dry_run:
        if as_json:
            click.echo(json.dumps({"argv": argv, "env": env, "unset": list(ENV_UNSET), "cwd": str(cwd), "log": str(log),
                                   "stderr": err and str(err), "isolation": isolation, "settings": settings,
                                   "warnings": warnings}, indent=2))
        else:
            for w in warnings:
                click.echo(f"⚠ {w}")
            click.echo(f"isolation: {isolation}")
            click.echo("env " + " ".join(f"-u {k}" for k in ENV_UNSET) + " "
                       + " ".join(f"{k}={v}" for k, v in env.items()) + " " + " ".join(argv[:2]) + f" {json.dumps(prompt)} "
                       + " ".join(json.dumps(a) if " " in a or "*" in a or "{" in a else a for a in argv[3:]))
            click.echo(f"log: {log}")
        return
    if not shutil.which("claude"):
        click.echo("❌ `claude` is not on PATH")
        sys.exit(2)
    for w in warnings:
        click.echo(f"⚠ {w}")
    log.parent.mkdir(parents=True, exist_ok=True)
    guard = (f"sandbox on ({len(settings['sandbox']['credentials']['envVars'])} credential env var(s) and "
             f"{len(CREDENTIAL_PATHS)} credential paths hidden, network: run.allowed_domains)" if settings else "UNSANDBOXED")
    click.echo(f"Running {name} unattended (orchestrator session {model}/{effort}, {guard}, permission mode "
               f"{PERMISSION_MODE}, budget ${_number(budget)}, max {max_turns} turns); log: {log}")
    child_env = {k: v for k, v in os.environ.items() if k not in ENV_UNSET}
    child_env.update(env)
    with log.open("w") as out, (err.open("w") if err else open(os.devnull, "w")) as errf:
        header = errf if err else out  # keep a JSON log pure; the header goes with stderr then
        header.write(f"ck run {name}: isolation: {isolation}, {guard}, permission mode: {PERMISSION_MODE}, "
                     f"budget ${_number(budget)}, max turns {max_turns}\n")
        for w in warnings:
            header.write(f"WARNING: {w}\n")
        header.write(f"argv: {json.dumps(argv)}\n\n")
        header.flush()
        code = subprocess.call(argv, cwd=cwd, env=child_env, stdout=out, stderr=errf if err else subprocess.STDOUT)
    click.echo(f"{'✓' if code == 0 else '❌'} claude exited {code}; log: {log}")
    sys.exit(code)


# --- ck doctor --unattended ----------------------------------------------------

def _blanket_bash(settings_file: Path) -> bool:
    try:
        data = json.loads(settings_file.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    allow = (data.get("permissions") or {}).get("allow") if isinstance(data, dict) else None
    return isinstance(allow, list) and any(r in ("Bash", "Bash(*)", "Bash(*:*)", "Bash(:*)") for r in allow)


def _user_sandbox(settings_file: Path) -> Dict[str, Any]:
    try:
        data = json.loads(settings_file.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    sb = data.get("sandbox") if isinstance(data, dict) else None
    return sb if isinstance(sb, dict) else {}


def doctor_unattended(project: Path) -> None:
    """Report what an unattended `ck run` would be exposed to on this machine. Warnings never fail."""
    click.echo("🔍 Checking this machine for unattended runs (`ck run`)...\n")
    recs: List[str] = []
    missing = missing_sandbox_tools()
    for tool in SANDBOX_TOOLS:
        click.echo(f"{'❌' if tool in missing else '✓'} {tool} {'not found' if tool in missing else 'found: ' + str(shutil.which(tool))}")
    if missing:
        click.echo("❌ `ck run` will refuse to start: it only runs inside Claude Code's Bash sandbox, which needs "
                   "bubblewrap (bwrap) and socat on Linux/WSL2 (`--unsandboxed` overrides, not recommended)")
        recs.append(f"Install the sandbox tools: {APT_INSTALL} (Ubuntu 24.04+: also allow bubblewrap user namespaces "
                    "in AppArmor, see https://code.claude.com/docs/en/sandboxing)")
    else:
        click.echo("✓ `ck run` will sandbox every shell command: home directory hidden apart from toolchains, "
                   "credential files and credential-shaped environment variables removed, network limited")
    names = credential_env_names(os.environ)
    click.echo(f"ℹ️  Credential-shaped environment variables a sandboxed command won't see: {len(names)} "
               "(names only via `ck run --dry-run --json`)")
    if os.environ.get("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"):
        click.echo("ℹ️  CLAUDE_CODE_SUBPROCESS_ENV_SCRUB is set in this shell; `ck run` removes it for the run (it makes "
                   ".git/worktrees read-only and forces default permission mode; sandbox.credentials replaces it)")
    click.echo(f"ℹ️  Permission mode: {PERMISSION_MODE}, no prompts; sandboxed Bash is auto-allowed; `ck run` also "
               f"allows {', '.join(SESSION_TOOLS)} and edits inside the project")
    try:
        from ..worktrees import WorktreeError, configured_dir, main_root
        wt_dir = configured_dir(main_root(project))
        under_claude = (wt_dir.resolve() / "x").is_relative_to(project.resolve() / ".claude")
        click.echo(f"{'⚠' if under_claude else '✓'} Task worktrees go to {wt_dir}"
                   + (" (under .claude/, which the Bash sandbox keeps read-only)" if under_claude else ""))
        if under_claude:
            recs.append('Move task worktrees out of .claude/: set "worktree_dir" in specs/ck.json (default .ck/worktrees)')
        legacy = sorted(p.name for p in (main_root(project) / ".claude" / "worktrees").glob("*") if p.is_dir())
        if legacy:
            click.echo(f"⚠ Worktrees left in .claude/worktrees ({', '.join(legacy[:5])}{'...' if len(legacy) > 5 else ''}): "
                       "a spec that still has some there keeps using that directory, where sandboxed agents cannot write")
            recs.append("Land or remove the worktrees in .claude/worktrees (`ck worktree status <spec>`) before a sandboxed `ck run`")
    except WorktreeError:
        pass

    user_settings = Path.home() / ".claude" / "settings.json"
    for f in (user_settings, project / ".claude" / "settings.json", project / ".claude" / "settings.local.json"):
        if _blanket_bash(f):
            click.echo(f"⚠ {f} allows every Bash command (blanket `Bash` / `Bash(*)` allow rule)")
            recs.append(f"Remove the blanket Bash allow rule from {f}: it lets any command skip the auto-mode classifier")

    creds = readable_credentials()
    if creds:
        click.echo(f"⚠ Readable credentials: {', '.join(creds)} (checked for existence only). `ck run` hides them "
                   "from sandboxed commands and the Read/Edit tools; hooks and MCP servers run outside the sandbox")
        recs.append("For defence in depth, keep real credentials off the runner: run `ck run` as a dedicated low-privilege user "
                    "(e.g. `sudo adduser ck-runner`, clone the repo there, log in to Claude Code as that user) "
                    "with no ~/.ssh, ~/.aws or cloud logins")
    else:
        click.echo("✓ No readable credential files found in the usual places")
    if (project / ".env").exists():
        click.echo(f"⚠ {project / '.env'} exists; tests that load it talk to real services (and may send real email)")
        recs.append("Point the test suite at test credentials or stubs, not the .env real services use")

    sb = _user_sandbox(user_settings)
    if sb:
        click.echo(f"ℹ️  ~/.claude/settings.json sandbox: enabled={sb.get('enabled')}, "
                   f"failIfUnavailable={sb.get('failIfUnavailable')}, allowUnsandboxedCommands={sb.get('allowUnsandboxedCommands')}")
        if sb.get("enabled") and not sb.get("failIfUnavailable") and missing:
            click.echo("⚠ sandbox.enabled is on but the tools are missing and failIfUnavailable is off: Claude Code "
                       "silently runs commands unsandboxed")
    else:
        click.echo("ℹ️  No sandbox settings in ~/.claude/settings.json (`ck run` passes its own with --settings)")
    try:
        domains = ag.resolve(project)["roles"]["run"]["allowed_domains"]
        click.echo(f"ℹ️  Sandboxed runs may reach: {', '.join(domains) or '(no hosts)'} "
                   "(change with `ck agents set run.allowed_domains ...`)")
    except ag.AgentConfigError as e:
        click.echo(f"⚠ {e}")

    trust = trust_warning(project)
    if trust:
        click.echo(f"⚠ {trust}")

    recs.append(f"Cap spend per run: `ck run <spec> --budget N` (default ${_number(DEFAULT_BUDGET_USD)})")
    click.echo("\n💡 Recommendations:")
    for r in recs:
        click.echo(f"  - {r}")
