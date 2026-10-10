"""`ck run`: start a whole-spec /spec:implement run headless and unattended.

A spec run takes hours; it should not stall on a permission prompt. This starts
`claude -p` in the main checkout with auto permissions and no prompts (anything
that would have asked is denied and shows up in the run's report), allows the
Workflow launch and the ck/git commands the agents run, and keeps the process
open until the workflow finishes. Run it from the main checkout, not from a
worktree: worktree-isolated sessions refuse commands they cannot prove stay
inside that worktree, and the workflow makes its own task worktrees anyway.

Nobody watches an unattended run, so it carries guards (see SECURITY below):
a spend cap, a turn cap, deny rules for destructive git and network commands
and for credential files, a scrubbed subprocess environment, narrow git allow
rules, and, where bubblewrap and socat are installed, Claude Code's OS-level
Bash sandbox with a strict network allowlist.

Claude Code docs these settings follow (checked October 2026):
  https://code.claude.com/docs/en/cli-reference   --max-budget-usd, --max-turns, --disallowedTools, --settings
  https://code.claude.com/docs/en/permissions     rule syntax; `command`/`timeout` wrappers are stripped before
                                                  matching; a Bash deny rule matches the command text as written
  https://code.claude.com/docs/en/sandboxing      sandbox.* keys, bubblewrap + socat on Linux/WSL2
  https://code.claude.com/docs/en/env-vars        CLAUDE_CODE_SUBPROCESS_ENV_SCRUB
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from .. import agents as ag
from .worktree import _spec_dir

# --- SECURITY -----------------------------------------------------------------

# git subcommands agents type. The brief mandates `command git -C <worktree> <sub> ...`
# (`command` is stripped before rule matching, so `git -C * <sub> *` covers it). ck runs
# its own git (worktree create/merge, gate) as a subprocess of `Bash(ck *)`, so only
# agent-typed git needs a rule here. Anything not listed still runs if auto mode's
# classifier approves it; it just doesn't skip the classifier any more, as `Bash(git *)` did.
GIT_AGENT_SUBCOMMANDS = (
    "status", "diff", "log", "show", "add", "commit", "merge", "rev-parse", "ls-files",
    "rm", "mv", "restore", "branch", "grep", "blame", "check-ignore",
    "worktree list", "worktree add",
)
# Read-only git without -C (the orchestrating session looks around the main checkout).
GIT_READ_ONLY = ("status", "diff", "log", "show", "rev-parse", "ls-files", "branch --show-current", "worktree list")

ALLOWED_TOOLS = (
    ["Workflow", "Bash(ck *)"]
    + [f"Bash(git -C * {sub} *)" for sub in GIT_AGENT_SUBCOMMANDS]
    + [f"Bash(git -C * {sub})" for sub in GIT_AGENT_SUBCOMMANDS]
    + [f"Bash(git {sub} *)" for sub in GIT_READ_ONLY]
)

# git forms nobody in a spec run needs and that destroy work, leave the machine, or run
# arbitrary programs (`git -c core.sshCommand=...`, `-c alias.x=!sh`).
_GIT_DENIED = ("push", "reset --hard", "clean", "worktree remove --force", "worktree remove -f", "-c")
# Deny rules hold in every permission mode and win over allow rules. A Bash deny rule
# matches the command text as written, so each git form is listed bare and with
# `-C <dir>`; it is a backstop for what agents normally type, not a boundary around the
# program (`sh -c 'git push'` is not matched: that is what the sandbox is for). The -C
# forms also match a commit message that contains, say, " push "; the brief tells
# agents to reword such a message.
DISALLOWED_TOOLS = (
    [f"Bash(git {g} *)" for g in _GIT_DENIED]
    + [f"Bash(git {g})" for g in _GIT_DENIED if g != "-c"]
    + [f"Bash(git -C * {g} *)" for g in _GIT_DENIED]
    + [f"Bash(git -C * {g})" for g in _GIT_DENIED if g != "-c"]
    + [f"Bash({prog} *)" for prog in ("curl", "wget", "ssh", "scp", "rsync", "nc", "ncat", "sftp")]
    + [f"{tool}({path})" for path in ("~/.ssh/**", "~/.aws/**", "~/.config/gcloud/**", "**/.env", "**/.env.*")
       for tool in ("Read", "Edit")]
    + ["Edit(**/.claude/settings*.json)", "WebFetch"]
)

ENV = {
    # Keep the process open until the background workflow finishes.
    "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0",
    # Strip credential-shaped variables (API keys, tokens) from Bash, hook and MCP subprocesses.
    "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
}

DEFAULT_BUDGET_USD = 40.0
DEFAULT_MAX_TURNS = 400

SANDBOX_TOOLS = ("bwrap", "socat")
APT_INSTALL = "sudo apt-get install bubblewrap socat"
CREDENTIAL_DIRS = ("~/.ssh", "~/.aws", "~/.config/gcloud", "~/.azure", "~/.kube", "~/.docker/config.json",
                   "~/.netrc", "~/.git-credentials", "~/.npmrc", "~/.pypirc")
# The sandbox hides the home directory from shell commands. Toolchains live there too
# (uv and ck in ~/.local, caches in ~/.cache, nvm, cargo...), so these stay readable.
# None of them should hold credentials; ~/.npmrc and ~/.pypirc deliberately aren't here.
SANDBOX_HOME_READABLE = ("~/.local", "~/.cache", "~/.config/claude-kiro", "~/.config/git", "~/.gitconfig",
                         "~/.nvm", "~/.npm", "~/.cargo", "~/.rustup", "~/.pyenv", "~/.bun")
# Package caches written by `uv sync`, `npm install`; the repo itself is writable by default.
SANDBOX_HOME_WRITABLE = ("~/.cache", "~/.npm")


def sandbox_settings(project: Path, allowed_domains: List[str]) -> Dict[str, Any]:
    """`--settings` JSON for an unattended run: the Bash sandbox on, no way around it.

    Key names follow https://code.claude.com/docs/en/sandboxing and the sandbox rows of
    https://code.claude.com/docs/en/settings-reference. Paths are absolute or `~/`:
    a relative path in --settings would not resolve against the project.
    """
    project = project.resolve()
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,  # exit at startup instead of silently running unsandboxed
            "allowUnsandboxedCommands": False,  # ignore dangerouslyDisableSandbox retries
            "autoAllowBashIfSandboxed": True,
            "filesystem": {
                "denyRead": ["~/"],
                "allowRead": [str(project), str(project / ".claude" / "worktrees"), *SANDBOX_HOME_READABLE],
                "allowWrite": list(SANDBOX_HOME_WRITABLE),
            },
            "network": {
                "allowedDomains": list(allowed_domains),
                "strictAllowlist": True,  # refuse other hosts instead of asking (nobody can answer)
            },
        }
    }


def missing_sandbox_tools() -> List[str]:
    return [t for t in SANDBOX_TOOLS if not shutil.which(t)]


def readable_credentials() -> List[str]:
    """Credential files and directories under HOME that exist and this user can read (existence only)."""
    home = Path.home()
    found = []
    for rel in CREDENTIAL_DIRS:
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


def choose_isolation(requested: str) -> Tuple[str, List[str], Optional[str]]:
    """(isolation, warnings, refusal). auto picks sandbox when bwrap and socat are on PATH."""
    missing = missing_sandbox_tools()
    if requested == "sandbox" and missing:
        return "none", [], (f"--isolation sandbox needs {' and '.join(SANDBOX_TOOLS)} on PATH; missing: "
                            f"{', '.join(missing)}. Install them ({APT_INSTALL}) or pass --isolation none.")
    if requested == "none":
        return "none", ["UNSANDBOXED run (--isolation none): shell commands can read every file this user can "
                        "and reach any host."], None
    if requested == "auto" and missing:
        return "none", [f"UNSANDBOXED run: {', '.join(missing)} not found, so Claude Code's Bash sandbox cannot "
                        f"start. Shell commands can read every file this user can and reach any host. "
                        f"Install bubblewrap and socat ({APT_INSTALL}) to sandbox runs."], None
    return "sandbox", [], None


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
@click.option("--isolation", type=click.Choice(["auto", "sandbox", "none"]), default="auto", show_default=True,
              help="sandbox: Claude Code's Bash sandbox (needs bwrap and socat); auto: sandbox if available, else none with a warning")
@click.option("--dry-run", is_flag=True, help="Print the command instead of running it")
@click.option("--json", "as_json", is_flag=True, help="With --dry-run: print the command as JSON")
def run(spec: str, extra: tuple, model: str, effort: str, budget: float, max_turns: int, isolation: str,
        dry_run: bool, as_json: bool):
    """Run every task of SPEC unattended: headless, no permission prompts.

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
    chosen, warnings, refusal = choose_isolation(isolation)
    if refusal:
        click.echo(f"❌ {refusal}")
        sys.exit(2)
    settings = None
    if chosen == "sandbox":
        try:
            domains = ag.resolve(cwd)["roles"]["run"]["allowed_domains"]
        except ag.AgentConfigError as e:
            click.echo(f"❌ {e}")
            sys.exit(2)
        settings = sandbox_settings(cwd, domains)
    else:
        creds = readable_credentials()
        if creds:
            warnings.append(f"Readable credentials on this machine: {', '.join(creds)}. Without the sandbox only "
                            "deny rules for the usual commands stand between them and the agents.")
    trust = trust_warning(cwd)
    if trust:
        warnings.append(trust)

    prompt = " ".join([f"/spec:implement {name} all", *extra])
    argv = [
        "claude", "-p", prompt,
        "--permission-mode", "auto",
        "--permission-prompts", "none",
        "--allowedTools", ",".join(ALLOWED_TOOLS),
        "--disallowedTools", ",".join(DISALLOWED_TOOLS),
        "--max-budget-usd", _number(budget),
        "--max-turns", str(max_turns),
        "--model", model,
        "--effort", effort,
    ]
    if settings is not None:
        argv += ["--settings", json.dumps(settings, separators=(",", ":"))]
    env = dict(ENV)
    log = cwd / ".claude" / "ck-runs" / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    if dry_run:
        if as_json:
            click.echo(json.dumps({"argv": argv, "env": env, "cwd": str(cwd), "log": str(log), "isolation": chosen,
                                   "settings": settings, "warnings": warnings}, indent=2))
        else:
            for w in warnings:
                click.echo(f"⚠ {w}")
            click.echo(f"isolation: {chosen}")
            click.echo(" ".join(f"{k}={v}" for k, v in env.items()) + " " + " ".join(argv[:2]) + f" {json.dumps(prompt)} "
                       + " ".join(json.dumps(a) if " " in a or "*" in a or "{" in a else a for a in argv[3:]))
            click.echo(f"log: {log}")
        return
    if not shutil.which("claude"):
        click.echo("❌ `claude` is not on PATH")
        sys.exit(2)
    for w in warnings:
        click.echo(f"⚠ {w}")
    log.parent.mkdir(parents=True, exist_ok=True)
    click.echo(f"Running {name} unattended (orchestrator session {model}/{effort}, isolation {chosen}, "
               f"budget ${_number(budget)}, max {max_turns} turns); log: {log}")
    with log.open("w") as out:
        out.write(f"ck run {name}: isolation: {chosen}, budget ${_number(budget)}, max turns {max_turns}\n")
        for w in warnings:
            out.write(f"WARNING: {w}\n")
        out.write(f"argv: {json.dumps(argv)}\n\n")
        out.flush()
        code = subprocess.call(argv, cwd=cwd, env={**os.environ, **env}, stdout=out, stderr=subprocess.STDOUT)
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
        click.echo("  Claude Code's Bash sandbox needs bubblewrap (bwrap) and socat on Linux/WSL2; without them "
                   "`ck run` runs unsandboxed.")
        recs.append(f"Install the sandbox tools: {APT_INSTALL} (Ubuntu 24.04+: also allow bubblewrap user namespaces "
                    "in AppArmor, see https://code.claude.com/docs/en/sandboxing)")
    else:
        click.echo("✓ `ck run` will sandbox Bash (isolation auto → sandbox)")

    user_settings = Path.home() / ".claude" / "settings.json"
    for f in (user_settings, project / ".claude" / "settings.json", project / ".claude" / "settings.local.json"):
        if _blanket_bash(f):
            click.echo(f"⚠ {f} allows every Bash command (blanket `Bash` / `Bash(*)` allow rule)")
            recs.append(f"Remove the blanket Bash allow rule from {f}: it lets any command skip the auto-mode classifier")

    creds = readable_credentials()
    if creds:
        click.echo(f"⚠ Readable credentials: {', '.join(creds)} (checked for existence only)")
        recs.append("Keep real credentials out of the runner: run `ck run` as a dedicated low-privilege user "
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
