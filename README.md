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
- **Test-tamper guard (PreToolUse hook):** Asks you before Claude removes tests or assertions, adds skip/xfail/only markers, drops a property from a `PROPERTIES` map or lowers its example count, deletes test files, or edits `requirements.md` once implementation has started. Set `"guard"` in `specs/ck.json` to `ask`, `deny`, or `off`.
- **Smart caching:** Shows messages only once per file per session (no spam!)

### Testing model

`/spec:create` writes a `test-plan.md` in parallel with the design, from the requirements and the codebase's real boundaries, never from the design. It is integration-first: anything touching I/O, persistence, network, or several components gets a test against the real boundary, and unit tests are reserved for pure logic. Tasks come in two tracks. **Test** tasks write those tests first; **impl** tasks are **Verified by** them and are Done only when they pass.

The test plan has a required **Properties** section. Every universally quantified criterion ("any", "every", "only", "never", SHALL NOT) becomes a row `P-n | requirement | "for any X ..." | generator domain | expected decision`. The generator domain enumerates every surface form of the input (for an amount: `$x`, `x USD`, `x to y`, `x-y`, checking both ends of a range). Allowlists draw negatives from values that exist in the data but are not permitted. Every "only"/"never" clause gets its complement, and where there is no oracle a metamorphic relation stands in (adding an unquoted amount can only move pass to held). Properties use the language's property-testing library (Hypothesis, fast-check, ...) with deterministic settings, or table-driven tests where there is none. Safety properties call a pure decision seam (`decide(inputs) -> decision`) that the design must expose, so they run before any server exists. Each property test file maps properties to tests in a `PROPERTIES = {"P-3": "test_name"}` map, and `ck gate` reports any P-n no map claims: a warning by default, a failure with `"properties": "required"` in `specs/ck.json`. Each story with a SHALL NOT also gets a red-team test task that tries to break it from the requirements alone.

## How It Works

1. **Initialize:** `ck init` sets up your project with all necessary files
2. **Plan:** `/spec:plan "feature"` - Discuss interactively, decide one question at a time, record decisions in `specs/<name>/PLAN.md`
3. **Create specs:** `/spec:create <name>` - A workflow writes requirements, then design and an integration-first test plan in parallel, then test and impl task tracks in parallel, then adversarially reviews and revises them
4. **Implement:** `/spec:implement task` - Test-track tasks write integration tests first; impl-track tasks make them pass. Done only when `ck gate` passes
5. **Parallelize:** `/spec:implement <spec>` (no task number) runs the whole spec as a workflow:
   - It lints the plan first and refuses to launch on a cycle.
   - Each task starts as soon as its own dependencies merge, in its own worktree, with a retry when a gate fails.
   - Finished tasks land in batches (`ck worktree land`): a `git merge-tree` precheck, one `ck gate --task` run for the whole batch, and bisection when the batch is red, so only the culprit goes to the fixer and the target stays green. One landing at a time; the batch size halves after a red batch and grows after a green one. A full gate runs every N merges and at the end.
   - The plan is re-read from tasks.md after every task, so relinks apply without a restart.
   - It reports wall-clock time per task and per wave group (a display label; nothing waits for a wave). By default tasks merge into an `integrate/<spec>` branch that you fast-forward into main when you're happy, and a failed task doesn't stop tasks that don't depend on it. `/spawn-worktree` runs the same dependency-driven schedule with you approving the launch and merges, or an ad-hoc batch
6. **No Workflow tool?** Some accounts block it. `/spec:create`, `/spec:review`, `/spec:implement` and `/spawn-worktree` then run the same steps with the Agent tool and a task list (`.claude/workflows/without-workflow-tool.md`): parallel agents in worktrees, merges one at a time, the same gates and reviewer. Models and efforts come from the `ck-*` agent types. Force it with `--no-workflow`.
7. **Track progress:** TodoWrite tracks implementation automatically
8. **Stay aligned:** Hooks provide context and maintain spec-driven discipline

## CLI Commands Reference

### Main Commands
- `ck setup [--force]` - Install global config to ~/.claude/ (once per machine)
- `ck init [--force] [--allow-workflow | --no-allow-workflow]` - Initialize a project with spec-driven setup. It asks whether to allow the Workflow tool in `.claude/settings.json`, so unattended runs (cloud sessions, `claude -p`) never stall on a permission prompt before a spec workflow launches. Run `ck init --allow-workflow` in an existing project to add just that rule
- `ck doctor [--unattended]` - Check your Claude Kiro setup health. `--unattended` instead checks this machine for unattended runs: bubblewrap/socat (the sandbox needs both; prints the apt command), blanket `Bash` allow rules in your settings, readable credential files (existence only), sandbox settings, workspace trust, with recommendations; it always exits 0
- `ck migrate [--dry-run]` - Move specs from `.claude/specs/` to `specs/` (tracked files keep their history)
- `ck gate <spec> [--task N]` - Run the verification gate; exits 1 on failure
  When it passes the full `verify` suite on a clean tree (no uncommitted or untracked files), it stamps the tree hash in `<git-common-dir>/ck-verified/<tree>.json`, so a merge script (e.g. the safe-merge skill) can skip re-running the same suite on the same tree.
- `ck waves <spec> [--json]` - Show tasks by dependency level (display only; tasks start when their own dependencies merge)
- `ck lint <spec>` - Check the task plan: dependency cycles, verify-order cycles (an impl task whose verifying tests need code from a task that depends on it), dependencies without a stated reason, tasks that touch a `risk_paths` file (in `specs/ck.json`, e.g. `["**/gate.py", "**/send*.py"]`) without `**Risk:** safety` (a warning; they are routed as safety anyway), critical path. Exits 1 on a cycle
- `ck run <spec> [--model M] [--effort E] [--budget USD] [--max-turns N] [--isolation auto|sandbox|none] [--output-format text|json|stream-json] [--session-id ID] [--log FILE] [-- <args for /spec:implement>]` - Run a whole spec unattended: starts `claude -p "/spec:implement <spec> all"` from the main checkout with auto permissions and no prompts (anything that would ask is denied and reported), allows the Workflow launch, `ck`, and the git forms the agents use (`git -C <dir> status|diff|log|add|commit|merge|...`, read-only git without `-C`), and stays open until the workflow ends. The orchestrating session runs on Sonnet at low effort; agents use `ck agents`. Logs go to `.claude/ck-runs/`; `--dry-run [--json]` prints the command, isolation, sandbox settings and warnings. Guards, always on: `--budget` (default 40) becomes `--max-budget-usd` (subagents count toward it), `--max-turns` (default 400) caps the orchestrating session, `--disallowedTools` denies `git push`, `git reset --hard`, `git clean`, `git worktree remove --force`, `git -c`, `curl`, `wget`, `ssh`, `scp`, `rsync`, `nc`, WebFetch, reads and edits of `~/.ssh`, `~/.aws`, `~/.config/gcloud` and `.env` files, and edits of `.claude/settings*.json`; `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` strips credential-shaped variables from shell commands. `--isolation sandbox` turns on Claude Code's Bash sandbox (needs `bwrap` and `socat`: `sudo apt-get install bubblewrap socat`) with no unsandboxed fallback: the home directory is unreadable apart from toolchains, the repo stays readable, and only the hosts in `run.allowed_domains` are reachable (`ck agents set run.allowed_domains pypi.org,github.com,...`; default: PyPI, npm, GitHub). `auto` (default) sandboxes when both tools are installed and otherwise runs unsandboxed with a loud warning, also written to the log; `sandbox` refuses to start without them; `none` never sandboxes. It also warns when the workspace isn't trusted in `~/.claude.json` (Claude Code then skips `settings.local.json`) and when credential directories are readable without a sandbox
- `ck bench run|score|import` - Compare agent configurations on whole-spec runs: sequential runs in fresh clones, scored on hidden acceptance tests with bootstrap CIs (see [Benchmarking model configurations](#benchmarking-model-configurations))
- `ck -C <dir> <command>` - Run any ck command as if started in `<dir>` (like `git -C`). The workflow's agents use it instead of `cd <dir> && ck ...`, which permission checks can't verify
- `ck plan <spec> --json [--exclude N,M]` - Machine-readable plan with the tasks ready to start now, plus each task's `risk` (`safety` or `normal`) and `complexity`, which route models; the workflow re-reads it after every task
- `ck worktree create|claim|release|merge|integration|status <spec> ...` - Per-task worktrees for parallel implementation (`.claude/worktrees/<spec>-task-N`, branch `feat/<spec>-task-N`); merges go one at a time and stop at the first conflict
- `ck worktree land <spec> <tasks...> [--into B] [--gate] [--install] [--release] [--keep] [--json]` - Land a batch of finished tasks: precheck each branch with `git merge-tree` (CONFLICT leaves the target untouched; a branch that only conflicts with an earlier one in the batch is DEFERRED), merge the clean ones longest-dependency-chain first, and with `--gate` run the task gates of the batch once in-process. A red batch is bisected by halving (the target checkout is reset to its pre-batch commit and the halves re-landed); culprits come back RED and stay off the target. Exit 0 ok, 1 RED, 2 CONFLICT, 3 busy/missing/failed. `/spec:implement` lands with it
- `ck agents [--json] [--override role.key=value]` - Show the model and effort each workflow role uses and where each value comes from. Defaults: implementer Sonnet at medium effort (the test writer follows it), reviewer Opus at high effort (reviews every task's diff against the spec before it merges), fixer Opus at medium effort, orchestrator Sonnet at low effort (plan, merge, gate steps), resolver Opus at high effort (merge conflicts), and `/spec:plan` / `/spec:create` ask to switch if the session is below Opus at medium effort
- `ck agents set <role.key> <value> [--project]` - Save a preference globally (`~/.config/claude-kiro/config.json`) or for this project (`specs/ck.json`), e.g. `ck agents set implementer.model haiku`, `ck agents set orchestrator.model haiku`, `ck agents set reviewer.enabled false`, `ck agents set planning.ask false`, `ck agents set implementer.escalate sonnet,opus` (when a task still fails after its retries or review rounds, try it once more on each bigger model in turn; off by default), `ck agents set run.max_concurrent 8` (how many tasks may have an implementing agent at work at once; unlimited by default, and tasks in review or waiting to merge don't count); `default` removes a setting.
  Risk routing: a task tagged `**Risk:** safety (reason)` in tasks.md (`/spec:create` tags tasks that send messages, move money, touch auth or credentials, delete, or have other irreversible effects), or touching a `risk_paths` file, starts at `implementer.risky_model` (unset: the top of `escalate`) and is always reviewed at `reviewer.risky_model`/`reviewer.risky_effort` (Opus, high). Escalation only reacts to failures the gate detects, and safety bugs usually pass the tests. That lets `reviewer.model` be cheaper for normal tasks. `ck agents set implementer.complexity_routing true` starts **Complexity:** High tasks at the first `escalate` model. A tiered setup: `ck agents set implementer.model haiku; ck agents set implementer.escalate sonnet,opus; ck agents set reviewer.model sonnet; ck agents set reviewer.risky_model opus`. The workflow report lists each task's `riskTag`, `startModel`, `reviewModel`, and the reviewer model behind each verdict
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

## Benchmarking model configurations

`ck bench` answers "is configuration B as good as A for less money?" with numbers you can defend. One pilot (n=1, concurrent runs, different ck versions, operator interventions, different merged task sets, unpriced tokens, quality judged only by LLM reviewers) can't. The protocol:

- **Sequential, fresh, pinned.** Each arm `(spec, config, rep)` runs alone, in a fresh `git clone --no-hardlinks` of the repo at a pinned base commit, on branch `bench/<config>-<rep>`, after `ck upgrade` with the installed ck (its version is recorded). Arms run in a seeded random order, so drift (API load, time of day) spreads across configurations. Never run two arms at once.
- **Configurations are agent settings.** Each config's `agents` are written with `ck agents set <role.key> <value> --project` and committed before launch; `run_args` go to `ck run` (put `/spec:implement` flags after `--`).
- **Hidden acceptance suite per spec.** Pytest files kept outside the repo and never shown to the agents. `ck bench score` copies them into a throwaway checkout of each arm's final integration branch and runs them; that, not the LLM reviewer, is the quality measure. Name tests `test_task<N>_...` (or map them with `task_map`) so results are per task.
- **Safety invariants.** A second pytest suite (things that must stay true: no weakened auth, no deleted data paths, the existing API still answers) run the same way.
- **Fail closed.** A run that hit a permission denial, called `AskUserQuestion`, had a task fail citing a permission refusal, or timed out is flagged `fail_closed` and scores 0 on the hidden suite: an unattended configuration that needs a human did not work unattended.
- **Cost.** The `claude -p --output-format json` result's `total_cost_usd` when present, plus every agent's transcript priced per request (input, output, cache read, cache write) with the list prices in `claude_kiro/bench.py` (2026-10; override with `"pricing"`). Both are kept; a disagreement over 25% is flagged.
- **How many runs.** Prefer more specs over more reps: specs differ more than reps of one spec. About 3 reps × 5–6 small specs (10–20 tasks each) per arm resolves a 20–30% paired difference in hidden pass rate; one spec resolves nothing, however many reps.

```json
{
  "repo": "../my-project", "base": "a1b2c3d", "reps": 3, "seed": 1,
  "specs": [{"name": "csv-export", "hidden_tests": "hidden/csv-export", "invariants": "invariants/",
             "test_cmd": ["uv", "run", "pytest"], "task_map": {"test_export_api.py": "4"}}],
  "configs": [
    {"name": "default", "agents": {}},
    {"name": "haiku-escalate", "agents": {"implementer.model": "haiku", "implementer.escalate": "sonnet,opus"},
     "run_args": ["--", "--max-concurrent", "4"]}
  ],
  "env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_METRICS_EXPORTER": "otlp"},
  "timeout_minutes": 240
}
```

Relative paths are relative to the config file. Optional keys: `bench_dir` (clones, default `bench-runs/`), `results_dir` (default `bench-results/`), `ck` (command, default `ck`), `env` (added to every run, e.g. OpenTelemetry settings for your own collector), `pricing`, `timeout_minutes`; per spec `hidden_dest` / `invariants_dest` (where the suites are copied in the checkout) and `test_cmd` (default `python -m pytest`). YAML works if PyYAML is installed.

```bash
ck bench run bench.json --dry-run     # the arms, in run order
ck bench run bench.json               # hours; re-run to resume (finished arms are skipped)
ck bench score bench-results --config bench.json
```

**Where the results come from.** `ck bench` launches `ck run <spec> --output-format json --session-id <uuid> --log <results>/logs/<arm>.json`. Knowing the session id, it reads what Claude Code persists for that session under `$CLAUDE_CONFIG_DIR` (default `~/.claude`): `projects/*/<session>/workflows/wf_*.json`, whose `result` is the `/spec:implement` report (merged, failed, notStarted, tasks with attempts, review rounds and escalations, timing, final gate), and the session and subagent transcripts (`projects/*/<session>.jsonl`, `projects/*/<session>/subagents/**/agent-*.jsonl`) for per-request token usage, `AskUserQuestion` calls and permission refusals. Each arm's `results/<arm>.json` holds that plus wall time, base and setup commits, the final integration commit, the ck version and the exact command.

**Scoring.** `ck bench score` writes `score.md` and `score.json`: per arm, the hidden pass rate, tasks passing their hidden tests, USD per passing task, USD per run, merged rate, escalations and review rounds per run, wall minutes and the invariant pass rate, each with a 95% percentile bootstrap CI that resamples specs (seeded, `--seed`). Against the baseline (the first config, or `--baseline`) it pairs tasks both arms merged and reports the hidden-pass difference, resampling tasks (`--cluster spec` to resample specs instead).

**Pilot data.** `ck bench import <workflow-output.json> --spec S --config C --rep N --out bench-results` turns an existing Workflow output (the JSON with `result` and `workflowProgress`) into a result so it can be scored the same way. Pass `--transcripts <session>/subagents/workflows/<runId>` for real token usage. Without it, each agent's `tokens` are priced at the input rate and marked as an estimate: `workflowProgress` tokens are the agent's final context size, not its cumulative usage (cache reads alone are typically 5–15× larger), so that estimate is low. Pass `--clone` (and `--ref`) to score the run's final tree.

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
