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
# Install Claude Kiro globally
uv tool install claude-kiro

# Set up global config (once per machine)
ck setup

# Initialize your project
cd your-project
ck init

# Verify setup
ck doctor
```

That's it! Your project is now configured for spec-driven development.

## Installation

> **Already using claude-kiro 0.1?** Follow [UPGRADING.md](UPGRADING.md): one step per machine, then `ck upgrade` in each repository.

### Install Claude Kiro (Global Tool)

```bash
# Install from PyPI
uv tool install claude-kiro

# Or install from source in editable mode
git clone https://github.com/angelsen/claude-kiro.git
cd claude-kiro
uv tool install . --editable
```

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
   - It reports wall-clock time per task and per wave. By default tasks merge into an `integrate/<spec>` branch that you fast-forward into main when you're happy, and a failed task doesn't stop tasks that don't depend on it. `/spawn-worktree` covers a single wave or an ad-hoc batch
6. **Track progress:** TodoWrite tracks implementation automatically
7. **Stay aligned:** Hooks provide context and maintain spec-driven discipline

## CLI Commands Reference

### Main Commands
- `ck setup [--force]` - Install global config to ~/.claude/ (once per machine)
- `ck init [--force]` - Initialize a project with spec-driven setup
- `ck doctor` - Check your Claude Kiro setup health
- `ck migrate [--dry-run]` - Move specs from `.claude/specs/` to `specs/` (tracked files keep their history)
- `ck gate <spec> [--task N]` - Run the verification gate; exits 1 on failure
- `ck waves <spec> [--json]` - Show the wave plan (Parallel Groups, or computed from Dependencies)
- `ck lint <spec>` - Check the task plan: dependency cycles, verify-order cycles (an impl task whose verifying tests need code from a task that depends on it), dependencies without a stated reason, critical path. Exits 1 on a cycle
- `ck plan <spec> --json [--exclude N,M]` - Machine-readable plan with the tasks ready to start now; the workflow re-reads it after every task
- `ck worktree create|claim|release|merge|integration|status <spec> ...` - Per-task worktrees for parallel implementation (`.claude/worktrees/<spec>-task-N`, branch `feat/<spec>-task-N`); merges go one at a time and stop at the first conflict
- `ck upgrade [--dry-run] [--no-migrate] [--allow-downgrade]` - Update a project set up by an older ck (see [UPGRADING.md](UPGRADING.md)). It records a version stamp in `.claude/ck-manifest.json` and refuses to let an older ck downgrade newer files
- `ck setup --diff` - Show how your global `~/.claude` files differ from this version, without writing
- `ck hook list` - Show available hooks
- `ck hook status` - Display configured hooks
- `ck hook test <name>` - Test a hook with sample data
- `ck hook config` - Generate settings.json configuration

### Claude Code Slash Commands (Created by `ck init`)
- `/spec:plan <feature>` - Discuss and decide the approach interactively (writes PLAN.md)
- `/spec:create <name>` - Write the spec as a workflow, ending in adversarial review
- `/spec:implement <task>` - Implement a spec task (Done is enforced by `ck gate`)
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
