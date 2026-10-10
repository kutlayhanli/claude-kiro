# Claude Kiro: Spec-Driven Development for Claude Code

A unified CLI tool and methodology that brings spec-driven development workflow to Claude Code.

**📖 [Documentation](https://angelsen.github.io/claude-kiro/)** | **🚀 [Getting Started](https://angelsen.github.io/claude-kiro/getting-started.html)** | **⚙️ [CLI Reference](https://angelsen.github.io/claude-kiro/cli.html)**

## What This Is

Claude Kiro `ck` provides:
- **Automated project setup** for spec-driven development
- **Smart hooks** that provide spec context while coding
- **Slash commands** for structured workflows
- **Output styles** that enforce disciplined responses

**Prompt → Plan → Requirements → Design → Tasks → Implementation**

## Repository Structure

```
├── VISION.md                    # Project vision and goals
├── synthesis/                   # Implementation guides
│   ├── kiro-to-claude-mapping.md
│   └── phase1-implementation.md
├── research/                    # Feature research from docs
│   ├── claude-code/             # Claude Code capabilities
│   └── kiro/                    # Kiro methodology
└── resources/                   # Scraped documentation
    └── scraped/
        ├── docs.claude.com/     # 255 pages
        └── kiro.dev/            # 89 pages
```

## Quick Start

```bash
# Install Claude Kiro globally (this fork; PyPI only has the upstream 0.1.0)
uv tool install --force "git+https://github.com/kutlayhanli/claude-kiro"

# Set up global config (once per machine)
ck setup

# Initialize your project
cd your-project
ck init

# Verify setup
ck doctor
```

That's it! Your project is now configured for spec-driven development.

### Updating an existing setup

From ck 0.5 on, one command updates everything. Run it from a project directory:

```bash
cd your-project
ck update            # reinstall ck from master, add missing ~/.claude files, ck upgrade this project
git add .claude/commands .claude/workflows .claude/ck-manifest.json specs
git commit -m "Upgrade claude-kiro"
```

Then run `ck upgrade` in each other project, and restart Claude Code sessions. `ck update --ref <branch|tag|commit>` installs something other than `master`; `ck update --dry-run` prints the steps; `ck setup --diff` shows global files that changed but weren't overwritten.

**Coming from 0.4 or older** (no `ck update` yet), do it once by hand:

```bash
uv tool install --force --reinstall "git+https://github.com/kutlayhanli/claude-kiro"
ck setup --diff        # global files (~/.claude) that changed; never overwritten without --force
cd your-project
ck upgrade --dry-run   # shows what would change, writes nothing
ck upgrade
```

- Don't update `ck` while a `/spec:implement` workflow is running in some project: its agents call `ck`.
- `ck upgrade` never touches `.claude/CLAUDE.md` or your spec content, and it refuses to let an older `ck` downgrade files written by a newer one.
- Other clones of the same repo: after `git pull`, run `ck upgrade` once in each. Hooks live in `.claude/settings.local.json`, which is local to each clone.
- Avoid `ck setup --force` if you've customized `~/.claude/CLAUDE.md`: it overwrites the file.
- Preferences set with `ck agents set` (without `--project`) live in `~/.config/claude-kiro/config.json` on each machine; set them again on a new machine.

Details, version-by-version changes, and rollback are in [UPGRADING.md](UPGRADING.md).

## Installation

> **Already using claude-kiro?** See [Updating an existing setup](#updating-an-existing-setup): reinstall once per machine, then run `ck upgrade` in each repository.

### Install Claude Kiro (Global Tool)

The `claude-kiro` package on PyPI is the upstream 0.1.0, which lacks everything described here (specs in `specs/`, the workflows, `ck gate`, `ck worktree`, and the rest). Install this fork from GitHub:

```bash
# Install (later, `ck update` updates it)
uv tool install --force "git+https://github.com/kutlayhanli/claude-kiro"

# Or install from a local clone in editable mode (follows whatever branch the clone has checked out)
git clone https://github.com/kutlayhanli/claude-kiro.git
cd claude-kiro
uv tool install --force . --editable
```

Check it: `ck --help` should list `agents`, `gate`, `lint`, `plan`, `update`, `upgrade`, `waves`, and `worktree`.

This installs the `ck` command globally, which provides:
- `ck setup` - Install global config to ~/.claude/ (once per machine)
- `ck init` - Set up a project for spec-driven development
- `ck doctor` - Verify your setup is working
- `ck hook` - Manage Claude Code hook integration
- `ck --hook` - Hook runner for Claude Code (hidden command)

### Initialize Your Project

```bash
cd your-project
ck init
```

This creates:
- `.claude/output-styles/spec-driven.md` - Enforces structured responses
- `.claude/commands/spec/` - Slash commands for specs
- `.claude/workflows/spec-create.js` - Workflow behind `/spec:create` and `/spec:review`
- `specs/` - Where specs are written (project root, outside `.claude/`, so writes need no approval)
- `.claude/settings.local.json` - Hook configuration
- `.claude/CLAUDE.md` - Project context template

### What the Hooks Do

The hooks provide spec context and enforce the definition of done:
- **When editing spec files:** Shows which task you're implementing
- **When editing new files:** Suggests creating a spec first
- **Verification gate (Stop hook):** When a task is marked Done, runs `ck gate`: acceptance boxes, the verify commands in `specs/ck.json`, the task's `**Verify:**` commands, the tests that verify it, and a check that no existing test was weakened. A failing Done claim blocks Claude from finishing; moving the task back to In Progress is always allowed.
- **Test-tamper guard (PreToolUse hook):** Asks you before Claude removes tests or assertions, adds skip/xfail/only markers, deletes test files, or edits `requirements.md` once implementation has started. Set `"guard"` in `specs/ck.json` to `ask`, `deny`, or `off`.
- **Smart caching:** Shows messages only once per file per session (no spam!)

### Testing model

`/spec:create` writes a `test-plan.md` in parallel with the design, from the requirements and the codebase's real boundaries, never from the design. It is integration-first: anything touching I/O, persistence, network, or several components gets a test against the real boundary, and unit tests are reserved for pure logic. Tasks come in two tracks. **Test** tasks write those tests first; **impl** tasks are **Verified by** them and are Done only when they pass.

## How It Works

1. **Initialize:** `ck init` sets up your project with all necessary files
2. **Plan:** `/spec:plan "feature"` - Discuss interactively, decide one question at a time, record decisions in `specs/<name>/PLAN.md`
3. **Create specs:** `/spec:create <name>` - A workflow writes requirements, then design and an integration-first test plan in parallel, then test and impl task tracks in parallel, then adversarially reviews and revises them
4. **Implement:** `/spec:implement task` - Test-track tasks write integration tests first; impl-track tasks make them pass. Done only when `ck gate` passes
5. **Parallelize:** `/spec:implement <spec>` (no task number) runs the whole spec as a workflow:
   - It lints the plan first and refuses to launch on a cycle.
   - Each task starts as soon as its own dependencies merge, in its own worktree, with a retry when a gate fails.
   - Merges happen one at a time, each checked with `ck gate --task`, plus a full gate every N merges and at the end.
   - The plan is re-read from tasks.md after every task, so relinks apply without a restart.
   - It reports wall-clock time per task and per wave group (a display label; nothing waits for a wave). By default tasks merge into an `integrate/<spec>` branch that you fast-forward into main when you're happy, and a failed task doesn't stop tasks that don't depend on it. `/spawn-worktree` runs the same dependency-driven schedule with you approving the launch and merges, or an ad-hoc batch
6. **No Workflow tool?** Some accounts block it. `/spec:create`, `/spec:review`, `/spec:implement` and `/spawn-worktree` then run the same steps with the Agent tool and a task list (`.claude/workflows/without-workflow-tool.md`): parallel agents in worktrees, merges one at a time, the same gates and reviewer. Models and efforts come from the `ck-*` agent types. Force it with `--no-workflow`.
7. **Track progress:** TodoWrite tracks implementation automatically
8. **Stay aligned:** Hooks provide context and maintain spec-driven discipline

## CLI Commands Reference

### Main Commands
- `ck setup [--force]` - Install global config to ~/.claude/ (once per machine)
- `ck init [--force] [--allow-workflow | --no-allow-workflow]` - Initialize a project with spec-driven setup. It asks whether to allow the Workflow tool in `.claude/settings.json`, so unattended runs (cloud sessions, `claude -p`) never stall on a permission prompt before a spec workflow launches. Run `ck init --allow-workflow` in an existing project to add just that rule
- `ck doctor` - Check your Claude Kiro setup health
- `ck migrate [--dry-run]` - Move specs from `.claude/specs/` to `specs/` (tracked files keep their history)
- `ck gate <spec> [--task N]` - Run the verification gate; exits 1 on failure
  When it passes the full `verify` suite on a clean tree (no uncommitted or untracked files), it stamps the tree hash in `<git-common-dir>/ck-verified/<tree>.json`, so a merge script (e.g. the safe-merge skill) can skip re-running the same suite on the same tree.
- `ck waves <spec> [--json]` - Show tasks by dependency level (display only; tasks start when their own dependencies merge)
- `ck lint <spec>` - Check the task plan: dependency cycles, verify-order cycles (an impl task whose verifying tests need code from a task that depends on it), dependencies without a stated reason, critical path. Exits 1 on a cycle
- `ck run <spec> [--model M] [--effort E] [-- <args for /spec:implement>]` - Run a whole spec unattended: starts `claude -p "/spec:implement <spec> all"` from the main checkout with auto permissions and no prompts (anything that would ask is denied and reported), allows the Workflow launch and the agents' `ck`/`git` commands, and stays open until the workflow ends. The orchestrating session runs on Sonnet at low effort; agents use `ck agents`. Logs go to `.claude/ck-runs/`. `--dry-run` prints the command
- `ck -C <dir> <command>` - Run any ck command as if started in `<dir>` (like `git -C`). The workflow's agents use it instead of `cd <dir> && ck ...`, which permission checks can't verify
- `ck plan <spec> --json [--exclude N,M]` - Machine-readable plan with the tasks ready to start now; the workflow re-reads it after every task
- `ck worktree create|claim|release|merge|integration|status <spec> ...` - Per-task worktrees for parallel implementation (`.claude/worktrees/<spec>-task-N`, branch `feat/<spec>-task-N`); merges go one at a time and stop at the first conflict
- `ck agents [--json] [--override role.key=value]` - Show the model and effort each workflow role uses and where each value comes from. Defaults: implementer Sonnet at medium effort (the test writer follows it), reviewer Opus at high effort (reviews every task's diff against the spec before it merges), fixer Opus at medium effort, orchestrator Sonnet at low effort (plan, merge, gate steps), resolver Opus at high effort (merge conflicts), and `/spec:plan` / `/spec:create` ask to switch if the session is below Opus at medium effort
- `ck agents set <role.key> <value> [--project]` - Save a preference globally (`~/.config/claude-kiro/config.json`) or for this project (`specs/ck.json`), e.g. `ck agents set implementer.model haiku`, `ck agents set orchestrator.model haiku`, `ck agents set reviewer.enabled false`, `ck agents set planning.ask false`, `ck agents set implementer.escalate sonnet,opus` (when a task still fails after its retries or review rounds, try it once more on each bigger model in turn; off by default), `ck agents set run.max_concurrent 8` (how many tasks may have an implementing agent at work at once; unlimited by default, and tasks in review or waiting to merge don't count); `default` removes a setting
- `ck agents sync [--override role.key=value]` - Write the agent types `ck-implementer`, `ck-test-writer`, `ck-fixer`, `ck-resolver` and `ck-reviewer` to `.claude/agents/`, carrying each role's model and effort. They are used when the Workflow tool is unavailable. `ck init`, `ck upgrade` and `ck agents set` keep them current
- `ck agents check --model <id> [--effort <level>]` - Exit 0 if a session meets the planning minimum (used by `/spec:plan` and `/spec:create`)
- `ck upgrade [--dry-run] [--no-migrate] [--allow-downgrade]` - Update a project set up by an older ck (see [UPGRADING.md](UPGRADING.md)). It records a version stamp in `.claude/ck-manifest.json` and refuses to let an older ck downgrade newer files
- `ck setup --diff` - Show how your global `~/.claude` files differ from this version, without writing
- `ck hook list` - Show available hooks
- `ck hook status` - Display configured hooks
- `ck hook test <name>` - Test a hook with sample data
- `ck hook config` - Generate settings.json configuration

### Claude Code Slash Commands (Created by `ck init`)
- `/spec:plan <feature>` - Discuss and decide the approach interactively (writes PLAN.md)
- `/spec:create <name>` - Write the spec as a workflow, ending in adversarial review
- `/spec:implement <spec | task> [--model M] [--effort E] [--review-model M] [--no-review]` - Implement a task or a whole spec (Done is enforced by `ck gate`; models per role from `ck agents`)
- `/spec:review <spec>` - Adversarially review an existing spec and apply surviving fixes
- `/spawn-worktree <spec-or-tasks>` - Run tasks in parallel with git worktree isolation

## Key Features

- **EARS notation** for testable requirements: `WHEN [condition] THE SYSTEM SHALL [behavior]`
- **3-phase workflow** with approval gates between phases
- **TodoWrite integration** for native task tracking
- **Smart hook context** that tracks what you're working on
- **Zero configuration** after running `ck init`

## Documentation Sources

All research extracted from local scraped docs:
- Claude Code: 255 pages (docs.claude.com)
- Kiro: 89 pages (kiro.dev)
- Combined: 10 research docs, 2 synthesis guides

## Development

### Code Formatting

```bash
make format-docs  # Format HTML/CSS/JS with prettier
```

## Implementation Status

- ✅ CLI tool (`ck`) - Complete with all commands
- ✅ Hook system - Smart context injection working
- ✅ Slash commands - `/spec:plan`, `/spec:create`, `/spec:implement`, `/spec:review`
- ✅ Output styles - Spec-driven responses enforced
- ✅ Project setup automation - `ck init` configures everything

## Why This Exists

**Problem:** AI coding is fast but chaotic - implicit assumptions, undocumented requirements, hard to maintain.

**Solution:** Structured specs before code. Proven by Kiro, implemented in Claude Code.

**Result:** Production-ready development with AI assistance.

---

Built for developers who want structure without sacrificing speed.
