# Upgrading

- **From 0.10.1 to 0.10.2:** see the next section.
- **From 0.10 to 0.10.1:** see [0.10 → 0.10.1](#010--0101), then the 0.10.2 notes.
- **From 0.9.1 to 0.10:** see [0.9.1 → 0.10](#091--010), then the 0.10.1 and 0.10.2 notes.
- **From 0.9.0 to 0.9.1:** see [0.9.0 → 0.9.1](#090--091).
- **From 0.8 to 0.9:** see [0.8 → 0.9](#08--09), then the 0.9.1 and 0.10 notes.
- **From 0.7 to 0.8:** see [0.7 → 0.8](#07--08), then the 0.9 notes.
- **From 0.6 to 0.7:** see [0.6 → 0.7](#06--07), then the 0.8 and 0.9 notes.
- **From 0.5 to 0.6:** see [0.5 → 0.6](#05--06), then the 0.7 notes.
- **From 0.4 to 0.5:** see [0.4 → 0.5](#04--05).
- **From 0.3 to 0.4:** see [0.3 → 0.4](#03--04).
- **From 0.2:** apply the 0.3 notes below as well; the steps are the same.
- **From 0.1:** skip to [Upgrading to 0.2](#upgrading-to-02), then come back for the 0.3 and 0.4 notes (same commands).

## 0.10.1 → 0.10.2

Run analytics now covers agent mode: runs where the Workflow tool is unavailable and `/spec:implement`, `/spec:create`, `/spec:review` or `/spec:spawn-worktree` fall back to the Agent tool. On each machine:

```bash
ck update                                  # ck itself and the spec plugin
ck stats collect --agents-backfill --dry-run   # optional: list older agent-mode implement runs backfill would record
ck stats collect --agents-backfill             # optional: record them (partial records)
```

Then restart Claude Code sessions, and run `ck upgrade` (or `ck update --project`) in vendored projects so their `.claude/commands/` and `.claude/workflows/without-workflow-tool.md` copies get the new instructions.

- **`ck stats begin`.** Agent-mode runs start with `ck stats begin --kind implement|create|review --spec NAME [--into BRANCH] --json`, which prints a run id (`ag_<8 hex>`) and keeps a private manifest in `$XDG_DATA_HOME/claude-kiro/runs-open/`. The skills run it; it is best-effort and never blocks a run. If it fails, the run's report ends with `⚠ ck stats begin failed: this run is not recorded in ck stats`.
- **No content in agent descriptions.** Agent calls are described only as `[ck:<run_id>]` plus a fixed verb and task number (`implement task 3`, `review task 3 round 1`, `fix 1`, `create design`, ...). Anything else counts as role `other`.
- **`ck stats collect --agents <run_id>`** writes the run's record from its marked agent transcripts (`source: "agents"`, cost source `agent_transcripts`), with the outcome from `ck worktree report` for the run's own tasks. The skills run it at the end of the run.
- **Open runs.** `ck doctor` warns about manifests open for more than 6 hours, and `ck stats collect --all` collects them.
- **Backfill.** `ck stats collect --agents-backfill [--dry-run] [--gap MINUTES]` records implement runs from before this release, grouped by session, spec and time gap. Create and review runs from before this release can't be recovered.
- **Shared records** stay `ck-run-analytics/1` with an optional `source` field. The leak check now also blocks agent worktree paths and branches, the manifest's spec, root and target branch, and the run id.

## 0.10 → 0.10.1

Run analytics (`ck stats`) and faster landing. On each machine:

```bash
ck update                     # ck itself and the spec plugin
# If a machine installed a 0.10.0 build after these features merged, its plugin
# cache may still hold the old workflow (same version number). Refresh it once:
claude plugin uninstall spec@claude-kiro --scope user && claude plugin install spec@claude-kiro --scope user
ck stats label work           # optional: a label for this machine in shared records (personal, work, ...)
ck stats collect --all        # optional: backfill this machine's past runs into the local store
```

Then restart Claude Code sessions, and run `ck upgrade` (or `ck update --project`) in vendored projects so their `.claude/workflows/` copies get the new scheduler. Check with `ck agents --json` (it now has `runtime.agentSlots`) and `ck stats --help`.

- **Run analytics: `ck stats`.** `collect` writes one local record per finished ck run (`spec-implement`, `spec-implement-iso`, `spec-create`) to `$XDG_DATA_HOME/claude-kiro/runs/` (`CK_DATA_HOME` overrides), never into a project; other workflows are counted, never named. `show` breaks a run down by role, phase, model and task and shows cross-run trends; `export` writes allowlisted, leak-checked records into a claude-kiro checkout; `submit` opens one draft PR per batch against `kutlayhanli/claude-kiro`. Collection runs automatically after `ck run` and at the end of `/spec:implement`, `/spec:create` and `/spec:review` (`ck agents set run.analytics false` turns it off). See the README's [Run analytics](README.md#run-analytics).
- **Fewer planning agents.** The workflow gets `deps` from `ck plan --json` and works out ready tasks itself; a planning agent runs only at start, after each landing, and when nothing is ready. Task agents create and claim their own worktree. Mid-run tasks.md edits are picked up at the next landing (or when the run goes idle) instead of after every task.
- **Finish-first scheduling.** New tasks start only into free agent slots and never while a review, revision or landing is queued, so finished work isn't stuck behind new starts. The slot count is the Workflow runtime's documented cap, min(16, CPUs - 2); override it with `CK_AGENT_SLOTS`. `ck agents set run.finish_first false` restores the old behaviour.
- **Queue waits in `ck stats show`:** per-agent wait to start (`queueMs`) and each task's wait before merge split into review and landing. Records collected before 0.10.1 show "not recorded".
- **Parallel tests.** `ck gate` sets `PYTEST_XDIST_AUTO_NUM_WORKERS` so that concurrent gates on one machine share the cores (unless you set it), and `ck doctor` / `ck init` suggest `pytest-xdist` for pytest projects whose `verify` has no `-n`, with a check for test ids that differ between collections (they break xdist). This repo's own `verify` is now `uv run pytest -q -n auto`.
- **Safety tasks and escalation.** With `implementer.escalate` set and `implementer.risky_model` unset, safety-tagged tasks start on the top of the escalation list (often Opus). To keep them on a mid-tier model while Opus still reviews them: `ck agents set implementer.risky_model sonnet`.

## 0.9.1 → 0.10

From the sandboxed sihman benchmarks (`notes/handoff-2026-10-12-ck-next.md`). No new setup steps: `ck update`, then `ck upgrade` in vendored projects.

- **`ck update` no longer upgrades the project in the current directory by itself.** Pass `--project`, or answer yes at the prompt in a terminal; without a terminal it skips and prints the command. It prints a line before `ck setup` installs and enables the spec plugin at user scope (which edits `~/.claude/settings.json`).
- **Claims and the landing lock no longer trust process IDs**, which mean nothing across sandboxed commands. The landing lock is an `flock`, dropped when its holder exits. A task claim records an owner token and a time, and goes stale after `claim_stale_minutes` (default 60, in `specs/ck.json`) with no claim, `ck gate` run or commit. `ck worktree create` and `claim` take over a stale claim and keep its commits. Old pid claims are judged by the same age rule.
- **`done-verify` gates task worktrees.** Workflow subagents edit their task worktree's `tasks.md` while Claude Code reports the session's directory, so the hook never gated them before. It now gates in the worktree the file belongs to. A task gate's pass is stamped by tree, and `ck gate --task` on the same tree with the same checks reuses it (`--no-reuse` forces a run). Whole-spec and landing gates never reuse.
- **`stop-verify`** warns about this tree's specs as before. For a subagent's task worktree, which shares the session id, it reports only gates that ran and failed, naming the worktree, instead of every Done task in it.
- **`ck worktree report <spec> --json`**: merged, done, failed, blocked and unstarted tasks from merge commits and `tasks.md` on the target branch. `/spec:implement` builds its final report from it, never from agent claims. A run where nothing left can start now ends as `outcome: "blocked"` with the tasks it waits on, not "planning step failed".
- **Contract checks for test tasks.** `ck lint <spec> --contracts [--task N] [--base REF] [--strict] [--json]` flags string keys and status values in test files that design.md never names (warns by default; `contracts_ignore` in `specs/ck.json` lists keys to skip). The reviewer of a test-track task checks every field and status value against design.md's models and API tables. `ck agents set reviewer.cross_test on` adds one read-only agent per wave of test tasks that compares the merged test files with each other.
- **`ck cost [--session ID | --run wf_…] [--json]`**: real spend from transcripts. `ck run` prints it when the run ends. `ck agents set run.budget_usd N` sets a project's budget, and `run.budget_per_task_usd` scales it with the tasks not yet Done.
- **`ck doctor --unattended`** warns about PreToolUse hooks that can answer `permissionDecision: allow` (they bypass `--disallowedTools`) and about the sandbox proxy's start-up race.
- **Landing a task that edits protected `.claude/` paths** fails before merging when the sandbox makes them read-only, naming the paths. Land such tasks attended, outside the sandbox.
- Vendored projects go straight to `.claude/workflows/<name>.js` instead of trying the plugin first.

## 0.9.0 → 0.9.1

**The gate runs when a task is marked Done, not at every Stop.** Until 0.9.0 the Stop and SubagentStop hooks ran the gate at the end of every turn and every subagent, once a task touched in the session was Done. Any edit invalidated the last pass, so a large spec re-ran its whole gate (every Done task's Verify commands) turn after turn, more than ten minutes each on a 60-task spec, and when git could not answer it gated every spec in the worktree.

- New `done-verify` PostToolUse hook (Edit, Write, MultiEdit, Bash): when a task's **Status:** becomes Done, it runs `ck gate <spec> --task N` for that task, once, and reports a failure to Claude. A Bash command that only checks out, rebases or merges committed `tasks.md` content is not a claim and runs nothing.
- `stop-verify` (Stop) runs no tests and never blocks: it lists tasks still marked Done whose gate failed, or that no gate ran for. Its timeout drops from 600 to 30 seconds.
- The requirements guard asks only when an edit changes or removes existing text in `requirements.md`. Adding a requirement to a spec under implementation no longer prompts, even in auto mode.
- New `guard.test_plan` (default `"ask"`): `test-plan.md` gets the same rule as `requirements.md`. Once a spec's implementation has started, adding test cases passes, and rewording or deleting an existing row asks. In an unattended run nobody can answer, so the edit is refused and the agent reports it.
- The SubagentStop hook is gone: a subagent's Done claims are gated by `done-verify` when it makes them, and `/spec:implement` gates each task again before merging.

```bash
uv tool install --force --reinstall "git+https://github.com/kutlayhanli/claude-kiro"   # or: ck update
ck upgrade      # vendored projects: rewrites ck's hooks in .claude/settings.local.json and drops SubagentStop
```

Plugin projects get the new hooks with the plugin update (`ck setup`).

## 0.8 → 0.9

**The spec commands ship as a Claude Code plugin.** Until 0.8, `ck init` and `ck upgrade` copied the commands, workflows, output style and agent types into every project, and projects drifted (one was two versions behind). From 0.9 they live in the `spec` plugin, published by this repository as the `claude-kiro` marketplace, and a project keeps only its own choices: `.claude/settings.json` enables `spec@claude-kiro`, `specs/ck.json` holds the verify commands and agent models. The commands keep their names (`/spec:plan`, `/spec:create`, `/spec:review`, `/spec:implement`); `/spawn-worktree` becomes **`/spec:spawn-worktree`**.

### Each machine, once

```bash
uv tool install --force --reinstall "git+https://github.com/kutlayhanli/claude-kiro"   # or: ck update
ck setup
```

`ck setup` now installs the plugin at user scope (`claude plugin marketplace add kutlayhanli/claude-kiro`, then `claude plugin install spec@claude-kiro --scope user`; `ck setup --dry-run` prints them, `--source <path>` installs from a local clone). It still writes `~/.claude/CLAUDE.md` only if missing. It no longer installs `~/.claude/skills/spawn-worktree/`: the plugin's `/spec:spawn-worktree` replaces it, so delete that skill directory yourself if you never customized it (ck doesn't touch it). Restart open Claude Code sessions.

Later updates: `ck update` reinstalls ck and runs `ck setup`, which updates the plugin (`claude plugin marketplace update claude-kiro`, `claude plugin update spec@claude-kiro`). Keep the two in step: `ck doctor` warns when the installed plugin's version differs from ck's.

### Each repository

```bash
cd your-project
ck upgrade --dry-run     # what would be removed and what would be kept
ck upgrade
git status               # removals of .claude/commands, .claude/workflows, .claude/agents/ck-*, ...
git add -A .claude specs
git commit -m "Use the claude-kiro plugin instead of copies"
```

`ck upgrade` (no flags) migrates a project that has 0.8 copies:
- It removes each copy ck wrote that nobody edited: its hash matches `.claude/ck-manifest.json`, or (projects from before the manifest) it is identical to this version's rendering.
- It **keeps** every copy that was edited by hand, and lists it under "Kept". Move anything you need into your own command or a project `CLAUDE.md`, then delete the file: a project command named like a plugin one shadows it.
- It removes the generated `.claude/agents/ck-*.md` agent types (files carrying the "generated by `ck agents sync`" mark). Hand-written agents are left alone.
- It removes ck's hooks from `.claude/settings.local.json`; the plugin's `hooks/hooks.json` registers the same hooks (pre-tool guard, spec context, Done gate, Stop warning). Other tools' hooks stay. The plugin's hooks act only in claude-kiro projects (those with `specs/ck.json` or a ck manifest).
- It adds `enabledPlugins: {"spec@claude-kiro": true}` and the `claude-kiro` marketplace under `extraKnownMarketplaces` to `.claude/settings.json`, so teammates who open the repo are offered the plugin.
- It records `"mode": "plugin"` in `.claude/ck-manifest.json`.

Other clones: after `git pull`, run `ck setup` once on that machine. Nothing else; the hooks no longer live in each clone's `settings.local.json`.

### Repositories used from cloud sessions

Claude Code on the web doesn't load plugins. For such a repository keep copies instead:

```bash
ck upgrade --vendor      # or `ck vendor`; `ck init --vendor` for a new project
git add -A .claude && git commit -m "Vendor the claude-kiro spec commands"
```

The copies are rendered from the plugin: `.claude/commands/spec/*.md` (so still `/spec:*`, including `/spec:spawn-worktree`; the old `.claude/commands/spawn-worktree.md` is removed if unedited), `.claude/workflows/` (scripts, the implement brief, the no-Workflow guide), `.claude/output-styles/spec-driven.md`, `.claude/agents/ck-*.md` with this project's models, and the hooks in `.claude/settings.local.json`. It also sets `"spec@claude-kiro": false` in `.claude/settings.json`, so a machine with the plugin installed doesn't see every command twice. A vendored project stays vendored on `ck upgrade` (refreshing the copies); `ck upgrade --plugin` moves it to the plugin.

### Other changes

- **`ck init`** enables the plugin and writes `specs/ck.json`; it copies nothing and installs no hooks in `settings.local.json` (it removes ck hooks found there, which would run twice). `--vendor` copies as described above.
- **`ck doctor`** reports the mode (plugin, vendored, or legacy copies that need `ck upgrade`) and whether the plugin is installed, read-only from `claude plugin list --json` (or `~/.claude/plugins/installed_plugins.json` when the `claude` CLI isn't on PATH).
- **Agent types.** The plugin ships `spec:implementer`, `spec:test-writer`, `spec:fixer`, `spec:resolver` and `spec:reviewer` with the default models; without the Workflow tool, the skills pass each role's model and effort from `ck agents --json` on every Agent call, so per-project agent files are no longer needed. `ck agents set` and `ck agents sync` write `.claude/agents/ck-*.md` only in vendored projects.
- **Workflows** are launched by name, `spec:spec-implement` and `spec:spec-create` (vendored projects fall back to `scriptPath` `.claude/workflows/<file>`). The implement workflow takes the task brief's path in `args.brief`; without it, it reads `<root>/.claude/workflows/spec-implement-brief.md` as before.
- **The plugin needs the CLI.** Its `bin/ck` runs your installed `ck` (skipping itself on PATH, falling back to `~/.local/bin/ck`), and prints install instructions when there is none; hooks then do nothing rather than fail.
- **Packaging.** The build backend is now hatchling, so the wheel carries `plugin/` as package data (`ck vendor` works offline). Nothing changes for `uv tool install`.

### Rolling back

Reinstall 0.8 (`uv tool install --force "git+https://github.com/kutlayhanli/claude-kiro@<0.8 commit>"`), `claude plugin uninstall spec@claude-kiro`, and in each project `git revert` the migration commit (it restores the copies), then `ck upgrade`. Without the revert, 0.8's `ck upgrade --allow-downgrade` writes the copies again.

## 0.7 → 0.8

What changes:
- **Escalation to bigger models on failure.** Run cheap and pay for a bigger model only where the cheap one fails: `ck agents set implementer.escalate sonnet,opus` (or `--escalate sonnet,opus` for one `/spec:implement` run). Once a task's own budget runs out on its role's model, it gets one more attempt per listed model, in order, at the role's effort: a retry after its gate keeps failing, a revision after blocking review findings survive the rounds, a fix after a red branch survives the fix. The test writer and fixer follow the implementer's list unless set; `none` turns it off. Off by default, so nothing changes until you set it.
- **Orchestration runs on a cheaper model.** The workflow's mechanical steps (plan, conflict-free merges, the per-merge, full and final gates, re-checks) used to run on the session's model, usually Opus. They now use the new **orchestrator** role, Sonnet at low effort. In the October benchmark these steps cost $1.9–3.1 per run on Opus.
- **Merge conflicts go to a strong model.** A merge step that hits a conflict now stops and hands it to the new **resolver** role (Opus, high effort, agent type `ck-resolver`) instead of resolving it at low effort.
- **The fixer defaults to Opus at medium effort** instead of following the implementer. Repairing a red target means deciding whether the environment or the code is at fault; Haiku fix agents got this wrong in 2 of 5 benchmark runs.
- **`ck init` offers a Workflow allow rule.** In auto mode a classifier may stop to ask before a spec workflow launches; an unattended session (cloud, `claude -p`) then waits forever. `ck init` now asks whether to add `"Workflow"` to `permissions.allow` in `.claude/settings.json` (default no). For an existing project, `ck init --allow-workflow` adds just that rule and skips files that already exist.
- Restore the old behaviour with `ck agents set orchestrator.model inherit`, `ck agents set resolver.model inherit` and `ck agents set fixer.model default` (and the same for `.effort`).
- **Handoff notes.** An agent that finishes without a passing gate, or with blocking review findings unresolved, writes `<worktree dir>/<spec>-task-N.handoff.md` (what it tried, the exact errors, the branch state, what it would do next). Every retry, revision and escalated attempt reads it first, so a bigger model starts from the smaller one's findings instead of from scratch. The note sits next to the worktree, never in the branch.
- **Escalation never steps down.** A model in the list that is not above the role's own is skipped, so an Opus fixer following the implementer's `sonnet,opus` list does not escalate to Sonnet.
- **Unattended runs: `ck run <spec>`.** Starts the whole spec headless from the main checkout, in auto permission mode with no prompts, inside Claude Code's Bash sandbox, and keeps the process open until the workflow ends. Sandboxed shell commands are auto-allowed, so agents run what they need without a command allow list; Workflow, Agent, Read, Glob, Grep, TodoWrite, the task tools, SendMessage and edits inside the project are allowed by name. Pass `/spec:implement` flags after `--`, e.g. `ck run auth -- --escalate sonnet,opus`.
- **Guards for unattended runs.** The threat: a `ck run` session works for hours with nobody watching. Before, `Bash(git *)` let every git command skip the classifier (`push`, `reset --hard`, `-c core.sshCommand=...`), `~/.ssh`, `~/.aws` and `.env` were readable, nothing capped spend, and the sandbox was off. Now the sandbox is the boundary: the home directory is hidden from shell commands apart from toolchains, credential files (`~/.ssh`, `~/.aws`, `~/.config/gcloud`, `~/.config/gh`, `~/.azure`, `~/.kube`, `~/.docker/config.json`, `~/.netrc`, `~/.git-credentials`, `~/.npmrc`, `~/.pypirc`) and every credential-shaped environment variable present at launch (`*TOKEN*`, `*SECRET*`, `*KEY*`, `*PASSWORD*`, `*CREDENTIAL*`, `AWS_*`, `GH_*`, `GITHUB_*`, ...) are removed with `sandbox.credentials`, and the network is limited to `run.allowed_domains` (new setting, default PyPI, npm and GitHub; `ck agents set run.allowed_domains ...`) with `strictAllowlist`. On top: `--max-budget-usd` (`--budget`, default 40), `--max-turns` (default 400), and a short `--disallowedTools` list (`git push`, `git reset --hard`, `git clean`, `git worktree remove --force`, WebFetch, credential and `.env` files, `.claude/settings*.json` edits).
- **The sandbox is mandatory.** `ck run` needs `bwrap` and `socat` (`sudo apt-get install bubblewrap socat`) and refuses to start without them. `--unsandboxed` runs without the sandbox, with a loud warning in the terminal and the log: shell commands then see your files, every environment variable and the network, and only the auto-mode classifier and the deny rules stand in the way. (There is no `--isolation` option.) `ck doctor --unattended` checks a machine and recommends fixes.
- **No `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`.** An earlier 0.9 build set it; on Claude Code 2.1.29x it makes `<repo>/.git/worktrees/` read-only for every shell command (no task worktree could be created: anthropics/claude-code#101546) and forces the default permission mode (#51258). `sandbox.credentials` removes the same variables from sandboxed commands, and `ck run` now removes the scrub from the run's environment if your shell sets it.
- **Task worktrees moved to `.ck/worktrees`.** Claude Code's Bash sandbox keeps the project's whole `.claude/` directory read-only, so sandboxed agents could not create, commit in, or test inside `.claude/worktrees/`. New worktrees go to `.ck/worktrees/<spec>-task-N` (set `worktree_dir` in `specs/ck.json` to change it); `ck plan --json` reports the directory as `worktreeDir` and `/spec:implement` passes it to the workflow. A spec whose worktrees are still in `.claude/worktrees` keeps using them until they are landed or removed. `ck init` and `ck upgrade` add the directory to `.gitignore`.
- **`ck worktree land` never merges protected files.** A task branch that changes `.claude/**`, `.mcp.json`, `.githooks/**`, `.husky/**`, `.pre-commit-config.yaml`, `.envrc` or `specs/ck.json` comes back PROTECTED and the task fails as needing a human: these files run outside the sandbox later (hooks, MCP servers, permission rules, verify commands). A spec that legitimately edits them opts in with `"land": {"allow_paths": [".claude/commands/**"]}` in `specs/ck.json`, read from the target checkout (a task branch can't opt itself in). The workflow reports such a task as failed, not retried.
- **Deny rules are a backstop, not a boundary.** They match the command text, so `sh -c 'git push'` gets past them (and a PreToolUse hook that rewrites commands and returns `allow` can bypass them); the sandbox's network allowlist and credential removal are what stop a push. Hooks and MCP servers run outside the sandbox and still see your credentials. For defence in depth, run `ck run` as a dedicated low-privilege user (`sudo adduser ck-runner`, clone the repo there, `claude` login as that user, no `~/.ssh`, `~/.aws` or cloud logins, test credentials only): a test suite run with real credentials will send real email, charge real cards and call real APIs.
- **No more `cd ... &&` in agent commands.** Agents run `ck -C <dir> ...`, `command git -C <dir> ...` and `uv sync --directory <dir>` as single simple commands. Compound commands were the main source of permission prompts and of refusals in worktree-isolated sessions.
- **The report lists escalations:** `escalated` (task, models tried, merged or not) and `fixerEscalations`, so you can see which tasks needed more than the cheap model.
- **Risk routing: safety tasks start strong and always get the strong reviewer.** Escalation only fires on failures the gate detects. In the October benchmark the costly bugs were the undetected ones: a Haiku reviewer approved send-path and price-tripwire code with real safety bugs that an Opus reviewer caught. Now:
  - **Risk tags.** A task can carry `**Risk:** safety (one-line reason)` (default `normal`). `/spec:create` writes it on tasks whose requirements or files involve sending messages, money or payments, auth or credentials, deletion, or other irreversible external effects, and its implementability review flags untagged risky tasks. Older specs have no tags: add them by hand, or use `risk_paths`.
  - **`risk_paths`** in `specs/ck.json` (a list of globs, e.g. `["**/gate.py", "**/send*.py"]`; `**/` also matches the project root): a task whose **Files:** match one counts as safety even without the tag. `ck lint` warns "Task N touches risk path X but has no **Risk:** safety" (not fatal).
  - **`ck plan --json`** exposes `risk` and `complexity` maps; `/spec:implement` passes them to the workflow as `risk` and `complexity`.
  - **Reviewer tiers.** New `reviewer.risky_model` / `reviewer.risky_effort` (default Opus, high) review every safety task, never below `reviewer.model`/`effort`. `reviewer.model` stays Opus/high by default, so nothing changes until you set it; set it cheaper (e.g. Sonnet) to save on normal tasks.
  - **Starting tier.** New `implementer.risky_model` (default unset = the top of `implementer.escalate`, else the implementer's model): where safety tasks start, in both tracks. New `implementer.complexity_routing` (default false): when true, **Complexity:** High tasks start at the first `escalate` model, skipping the cheap one; Low and Medium start at the role's model. A start never steps below the role's model, escalation then climbs only through models above the start, and a one-run `--model` override wins.
  - **Report:** each task in `tasks[]` has `riskTag`, `startModel` and `reviewModel`, and its `review` summary records the reviewer `model`/`effort` behind the verdict, so a later audit can count escapes (bugs a reviewer approved) per reviewer tier. `agents.reviewer_risky` shows the risky reviewer.
  - **Recommended config** (cheap by default, strong where it matters): `ck agents set implementer.model haiku; ck agents set implementer.escalate sonnet,opus; ck agents set reviewer.model sonnet; ck agents set reviewer.risky_model opus`, plus `risk_paths` for your send, payment, gate and auth modules. Add `ck agents set implementer.complexity_routing true` to keep High tasks off Haiku.
- **Correctness properties in new specs.** `/spec:create`'s test plan now has a required `## Properties` section: one row `P-n | requirement | "for any X ..." | generator domain | expected decision` for every criterion with "any", "every", "all", "only", "never" or SHALL NOT. Generator domains enumerate the input's surface forms (`$x`, `x USD`, `x to y`, `x-y`, both ends of a range), allowlists draw negatives from values that are in the data but not permitted, "only"/"never" clauses get their complement, and metamorphic relations cover rules with no oracle. Properties name the language's library (Hypothesis, fast-check, ...) with deterministic settings, or become table-driven tests where there is none. Safety properties call a pure **decision seam** (`decide(inputs) -> decision`) that design.md now lists under Public Interfaces, so they run before any server or HTTP task. In a benchmark, example tests missed three safety bugs that these properties catch: a price check that ignored the upper end of "$4.15 to 4.50", an allowlist that admitted any number in the sheet (quantities too), and a recipient check that counted the agent's own draft as a participant.
- **Property and red-team test tasks.** One test task per property group, with a new `**Properties:** P-1, P-2` field and its property test file under **Verify:**. It verifies the impl task that owns the decision seam, not the server built on it. Each story with a SHALL NOT also gets a red-team test task, written first from the requirements alone, that tries to break each SHALL NOT clause with adversarial inputs. The verification review lens flags missing properties, missing surface forms and missing negative space.
- **The PROPERTIES map convention.** A property test file declares a module-level map from property to test name(s): `PROPERTIES = {"P-3": "test_name", "P-4": ["test_a", "test_b"]}` (JS/TS: `export const PROPERTIES = { 'P-3': 'test name' }`). `ck gate` reads these maps from every test file in the project. The whole-spec gate reports each P-n in test-plan.md that no map claims, or whose named test does not exist in that file. A test task's gate checks the P-ids under its **Properties:** in its own files. Properties owned by unfinished test tasks are reported as pending.
- **`"properties": "required"`** in `specs/ck.json` turns missing properties into gate failures; it also fails a spec whose test-plan.md has no `## Properties` section (write "None" with the reason if nothing is universally quantified). The default, `"warn"`, only reports them; `"off"` skips the check. Older specs without a Properties section are not checked unless you set `required`.
- **Properties are tests.** On a property failure, implementers report the shrunk counterexample and ask whether to fix the code, the spec or the property; they never edit a property silently. The tamper check and the test guard now treat dropping a P-n from a PROPERTIES map, or lowering `max_examples`/`numRuns`, as weakening a test.

- **Batch landing: `ck worktree land`.** Finished tasks no longer merge one at a time through an agent each (about 6-9 minutes per merge and gate, a queue floor near 8 hours for a 62-task spec). When a landing ends, every task waiting to merge (up to the new `mergeBatch` arg, default 4) lands in one `ck worktree land <spec> <tasks...> --gate` step: a `git merge-tree` precheck per branch (a conflict never touches the target), one merge per branch, and one gate for the whole batch, run in-process. A cheap orchestrator agent only runs that one command. Expected speedup on a busy queue: close to the batch size for the merge phase (4 tasks for the price of one gate) when the target is mostly green.
- **The safety trade-off.** A red batch costs extra gates: it is bisected by halving (the target checkout is reset to the commit before the batch, then the halves re-land) until the culprit is isolated, about 1 + log2(batch) gates instead of 1. Blame comes from the bisection, not from merge order. To keep that cheap the batch size adapts: it halves after a red batch (down to 1, the old one-at-a-time behaviour) and grows by one after a green one. Batching pays off while the red rate is low (Mergify suggests batches of 5-10 only below about 2%); pass `"mergeBatch": 1` to keep merging one at a time.
- **A task that turns the target red is no longer merged.** It comes back RED with the failing gate output; the target stays green and the task goes to the fixer, who fixes it in the task worktree, and then lands again (with the fixer's escalation ladder). If it is still red, the task fails like any other failed task instead of halting the run. A red full gate still halts.
- **Conflicts are found before merging.** The resolver now resolves in the task worktree only, and the workflow re-lands the task. A task that conflicts only with another task of the same batch is deferred to the next batch instead.
- **Critical path lands first.** Within a batch, tasks with the longest remaining dependency chain (from `ck plan`) merge first.
- **The report lists `landings`:** per landing, the tasks, batch size and window, merged, red, conflicting and deferred tasks, gate runs, whether it was bisected, and minutes.
- **`ck bench`: compare configurations properly.** `ck bench run bench.json` runs every (spec, config, rep) arm one at a time, in a seeded random order, each in a fresh clone at a pinned base with its agent settings committed; `ck bench score` overlays per-spec hidden acceptance tests and safety invariants (kept outside the repo) on each arm's final integration tree and reports hidden pass rate, USD per passing task, merged rate, escalations and wall time per arm with bootstrap CIs, plus paired per-task differences. Runs that needed a human (permission denial, `AskUserQuestion`) fail closed. `ck bench import` converts an existing workflow output file so pilot runs can be scored the same way. See the README section "Benchmarking model configurations".
- **`ck run` options for machine-readable runs:** `--output-format json|stream-json` (stdout to the log, stderr to `<log>.stderr`), `--session-id <uuid>` and `--log <path>`. The default is unchanged.
- **`ck --version`** prints the installed version.
- **Review fixes can no longer silently change governed behaviour.** In a real spec, one review fix made the system refuse a reply body changed after an operator's Approve, while requirement 9.16 said to re-evaluate it; nothing caught it until implementation agents disagreed. `/spec:create` and `/spec:review` now: (1) tell the reviser, before applying a fix that changes what the system does, refuses, sends or stores (or a state order, limit or actor), to find and quote every criterion and P-n that governs it, and either conform or not apply it and return it as a decision (`escalated`, listed in review.md's "Needs Your Decision" as "raised while applying fixes", result "Ready after decisions"); (2) snapshot the four spec files to `specs/<name>/.pre-revision/` before the revision and run a **consistency check** after it (session model, high effort) that diffs each file against the snapshot and checks every changed passage against requirements.md and the Properties section, and every changed criterion against design, test plan and tasks. It fixes wording and traceability drift itself (then runs once more, at most 2 passes) and appends behaviour conflicts to "Needs Your Decision" as "found by the consistency check"; the snapshot is deleted afterwards (if a run is interrupted, delete a leftover `.pre-revision/` by hand); (3) check properties and test cases over overlapping inputs for different expected outcomes, in the verification lens and the consistency check. Test-plan properties now name their governing criterion id. The workflow result gains `review.escalated`, a `source` (`review`, `revise`, `consistency`) plus `options` and `conflictsWith` on each `review.openQuestions` item, and `consistency: {passes, fixed, decisions, lost}`. A run with no fixes to apply skips the snapshot and the check. Run `ck upgrade` to get the new workflow and commands.

Steps: `ck update` from a project directory, then commit `.claude/workflows`, `.claude/commands`, `.claude/agents` and `.claude/ck-manifest.json`.

## 0.6 → 0.7

What changes:
- **`/spawn-worktree` has no waves.** It starts the tasks in `ck plan`'s `ready` list, merges each one as soon as its gate passes, and (if you choose "Keep going") starts whatever that makes ready, without waiting for the rest of a wave. You approve once at launch: keep going, just these tasks, or hold merges for your approval. The wave gate is gone; each merge runs `ck gate <spec> --task N` and the full gate runs at the end.
- **`ck plan` starts the critical path first.** Its `ready` list is ordered by the longest remaining chain of dependents (new `chain` field), so when there are more ready tasks than agent slots, the ones that hold up the most work start first.
- **Slots are held only while a task is being implemented.** A task in review or waiting in the merge queue no longer counts against the concurrency cap, and the scheduler starts the next ready task the moment one moves on. Before, a slow merge queue could leave ready tasks waiting behind finished ones.
- **One concurrency setting.** `ck agents set run.max_concurrent N` (global, or `--project`) caps how many tasks are being implemented at once, for `/spec:implement`, `/spawn-worktree` and the no-Workflow path alike. The default is unlimited; the no-Workflow path and `/spawn-worktree` used a hard-coded 6. `unlimited` lifts a cap set in a lower layer.
- **New specs have no `## Parallel Groups`.** `/spec:create` writes a `## Schedule` section instead (ready at start, critical path from `ck lint`). Older specs keep working: their Parallel Groups only label progress, and a spec without them no longer gets a warning. `ck lint` reports the critical path against open tasks and the ready set, instead of the wave count.

Steps: `ck update` from a project directory (or reinstall and `ck upgrade`), then commit `.claude/commands` and `.claude/workflows`. Refresh the spawn-worktree skill note with `ck setup --diff` (see the 0.3 → 0.4 steps for how).

## 0.5 → 0.6

What changes:
- **Works without the Workflow tool.** If your account blocks workflows, the spec commands run the same steps themselves: `/spec:create` and `/spec:review` play `spec-create.js` with the Agent tool; `/spec:implement <spec>` schedules task agents in worktrees, merges one at a time, gates, reviews, and tracks progress in the task list. See `.claude/workflows/without-workflow-tool.md`. Pass `--no-workflow` to force it.
- **Agent types per role.** `.claude/agents/ck-implementer.md`, `ck-test-writer.md`, `ck-fixer.md` and `ck-reviewer.md` carry each role's model and effort from `ck agents` (the Agent tool can't set effort per call). `ck agents sync` writes them. `ck upgrade` and `ck agents set` keep them current. Files you wrote yourself under those names are left alone.

Steps: `ck update` from a project directory (or reinstall and `ck upgrade`), then commit `.claude/commands`, `.claude/workflows`, `.claude/agents` and `.claude/ck-manifest.json`. Restart sessions: a session started before `.claude/agents/` existed doesn't see the new agent types.

## 0.4 → 0.5

What changes:
- **Models per role.** `/spec:implement` runs implementation agents on **Sonnet at medium effort** by default (test-writing tasks and red-branch fixes follow it), instead of inheriting the session's model. `ck agents` shows the effective settings; `ck agents set role.key value` saves a global preference (`~/.config/claude-kiro/config.json`), `--project` saves it to `specs/ck.json`, and `/spec:implement ... --model M --effort E` overrides one run. `inherit` restores the old behavior: `ck agents set implementer.model inherit` and `ck agents set implementer.effort inherit`.
- **A reviewer.** After each task's gate passes, an **Opus, high effort** reviewer checks its diff against `design.md` and `requirements.md` before it merges. Blocking findings go back to the implementer for one revision; findings that remain fail the task. Turn it off with `ck agents set reviewer.enabled false` or `/spec:implement ... --no-review`.
- **A model check for planning.** `/spec:plan` and `/spec:create` ask whether to switch if the session runs below Opus at medium effort. `ck agents set planning.ask false` stops the question.
- **Scoped task gates.** `ck gate <spec> --task N` runs that task's own Verify and its verifying tests while a test-first spec is in progress; `ck gate <spec>` still re-runs every suite that should already pass. `task_regression: true` in `specs/ck.json` brings back the per-task sweep.
- **Slow gates aren't red.** Workflow agents wait for a gate's real exit code instead of reporting a timeout as a failure.
- **`ck update`** reinstalls ck from `master`, adds missing global files, and runs `ck upgrade` in the current project.

Steps: once per machine, `uv tool install --force --reinstall "git+https://github.com/kutlayhanli/claude-kiro"` (later versions: `ck update`). Then in each repository, `ck upgrade` and commit `.claude/commands`, `.claude/workflows`, `.claude/ck-manifest.json`. Restart sessions.

## 0.3 → 0.4

What changes:
- **`/spec:implement <spec>` schedules by dependency, not by wave.** A task starts as soon as its own dependencies merge; waves only group the progress display.
  - After every task, the plan is re-read from tasks.md on the target branch (`ck plan`), so relinking tasks mid-run needs no restart.
  - Each merge is checked with `ck gate <spec> --task N` on the target, the full `ck gate <spec>` runs every `fullGateEvery` merges (default 10) and once at the end, and the result includes wall-clock time per task and per wave.
- **Pre-launch lint.** `ck lint <spec>` reports:
  - dependency cycles;
  - verify-order cycles: an impl task whose verifying tests import code owned by a task that depends on it (market-sim's Task 40);
  - dependencies without a stated reason;
  - the critical path.
  
  The workflow refuses to launch on a cycle.
- **Mandate in every task prompt.** Task agents ignore chat questions meant for the orchestrator, and an attempt with no commits is retried.
- **Reference fields parse only references.** In `**Dependencies:**`, `**Verified by:**`, and `**Verifies:**`, text in parentheses or after " - " is a reason or note, so "Task 39 (see also Task 69)" means Task 39 only. `/spec:create` now writes a reason for every dependency and a Harness Contract section in test-plan.md.
- **Resuming:** run `/spec:implement <spec>` again (a fresh run skips Done tasks). Don't use `resumeFromRunId`.

- **Version stamp (0.4.1).** `ck init` and `ck upgrade` write `.claude/ck-manifest.json`: the ck version and a hash of each managed file. Commit it with the other `.claude/` files.
  - An older ck refuses to `ck upgrade` (or `ck init --force`) a project stamped by a newer one, because it would replace newer files with older copies. Pass `--allow-downgrade` if you really mean it.
  - Upgrade also lists managed files you edited by hand since ck last wrote them.
  - `ck doctor` reports when the stamp and the installed ck differ.
  - Only ck 0.4.1 and later know about the stamp. An older install will still overwrite files, so reinstall on every machine.

Steps: reinstall `ck` on each machine (wait until any running `/spec:implement` workflow has finished, since its agents call `ck`). Then, **from each repository's directory**, run `ck upgrade` and commit the refreshed `.claude/` files, including `ck-manifest.json`.

## 0.2 → 0.3

What changes:
- **`/spec:implement <spec>`** with no task number now runs the whole spec as a workflow, wave by wave. It uses two new managed files: `.claude/workflows/spec-implement.js` and `spec-implement-brief.md`. `/spec:implement <N>` still runs one task.
  - Tasks merge into **`integrate/<spec>`**, in its own worktree, not into main. Land it when you're happy: `git merge --ff-only integrate/<spec>`. To merge straight into the checked-out branch instead, ask for it when you run the command.
  - A failed task doesn't stop the run. Tasks whose dependencies all merged keep going, and tasks that depend on the failure are skipped and reported. A red integration branch always halts. Ask for "halt on failure" to stop at the first failed wave.
  - Rerunning `/spec:implement <spec>` resumes: Done tasks are skipped and the integration worktree is reused.
- **The gate no longer requires the full suite mid-spec.** While a test-first spec is in progress, `ck gate` runs `verify_always`, a test-collection check, and only the test suites whose tasks are all Done. The full `verify` suite runs once every task is Done. Set `"verify_mode": "full"` in `specs/ck.json` to keep the old behavior. If you wrote a custom script to work around this (like `verify_green.py`), you can go back to plain `"verify": ["uv run pytest -q"]` and move lint into `"verify_always": ["uv run ruff check ."]`.
- **Test collection is checked** (derived from a pytest `verify` command, or set with `"collect"`). A test file that can't be imported now fails the gate.
- **Hooks:**
  - The spec-context hook names only the **In Progress** task that lists the exact file.
  - Scaffold files (`.gitignore`, `pyproject.toml`, `README`, lockfiles, `.github/`, and so on) and files outside the repo no longer trigger messages.
  - Add your own ignore patterns with `"context_ignore"`.
- **New commands:** `ck waves`, plus `ck worktree create|claim|release|merge|integration|status`, which replace hand-written worktree scripts.
- **`ck init` / `ck upgrade`** limit the ruff pre-commit hooks to Python files (`types_or: [python, pyi]`), so `ruff-format` stops rewriting code blocks inside spec Markdown.

Steps:
1. **Each machine:**
   - Reinstall: `uv tool install --force "git+https://github.com/kutlayhanli/claude-kiro@feat/specs-dir-and-workflow-create"`
   - Refresh the spawn-worktree skill, which now points to the wave workflow. Run `ck setup --diff` to see the change. If you never edited the skill, run `rm ~/.claude/skills/spawn-worktree/SKILL.md && ck setup` (`ck setup` only creates missing files, so your `~/.claude/CLAUDE.md` is untouched). Otherwise copy the new note by hand. Avoid `ck setup --force`: it also overwrites `~/.claude/CLAUDE.md`.
   - Restart Claude Code sessions.
2. **Each repository:** `ck upgrade --dry-run`, then `ck upgrade` (adds the two workflow files and patches `.pre-commit-config.yaml`). Commit, then restart sessions.
3. **Optional:** in `specs/ck.json`, move lint and type checks to `"verify_always"`, and drop any custom green-only verify script.

---

# Upgrading to 0.2

This guide is for machines and repositories already set up with claude-kiro 0.1.x.

## What changes

| Area | 0.1 | 0.2 |
|---|---|---|
| Spec location | `.claude/specs/<feature>/` (every write needs approval) | `specs/<feature>/` at the project root |
| `/spec:plan` | Research report, then ExitPlanMode | Interactive: decide one question at a time, PLAN.md is the decision record |
| `/spec:create` | Three phases with approval stops | A workflow: requirements, then design and test plan in parallel, then test and impl task tracks, then adversarial review (needs a Claude Code version with the Workflow tool) |
| Testing | Testing section inside design.md | `test-plan.md`, integration-first; test tasks are written before the code they verify |
| Done | Agent decides | `ck gate` decides; marking a task Done runs it, and a failure goes back to the agent |
| Tests | Unprotected | A PreToolUse guard asks before tests are weakened or deleted, or requirements are edited mid-implementation |
| Hooks | One PostToolUse hook; `ck init` replaced every PostToolUse hook and wrote a timeout of `5000` (read by Claude Code as seconds) | Four hooks, merged alongside your other hooks, timeouts in seconds |

Two parts are upgraded separately:

- **Each machine**: the `ck` tool and the global files in `~/.claude/`.
- **Each repository**: the files `ck init` wrote into the repo, plus the hooks in `.claude/settings.local.json`. Most of the repo files travel through git; the hooks file is local to each clone.

## 1. Each machine

### Install the new `ck`

PyPI only has the upstream 0.1.0, so install from the fork:

```bash
uv tool install --force "git+https://github.com/kutlayhanli/claude-kiro@feat/specs-dir-and-workflow-create"
```

Once the branch is merged, use `@master` instead. If you keep a local clone and want `ck` to follow it:

```bash
uv tool install --force --editable /path/to/claude-kiro
```

Check it worked: `ck --help` should list `gate`, `migrate`, and `upgrade`.

### Update the global files in `~/.claude/`

`ck setup` never overwrites existing files without `--force`, and `--force` replaces the whole file. Look first:

```bash
ck setup --diff
```

- **Your `~/.claude/CLAUDE.md` is customized** (most likely): don't use `--force`. Paste this block in instead, replacing any older claude-kiro section:

  ```markdown
  ## Spec-Driven Development (claude-kiro)

  1. `/spec:plan` - Discuss and decide the approach with me, interactively (writes PLAN.md)
  2. `/spec:create` - Workflow that writes requirements, design, test plan, and tasks, then adversarially reviews them
  3. `/spec:implement` - Implement a test-track or impl-track task; Done only when `ck gate` passes
  4. `/spawn-worktree` - Run a wave of tasks in parallel with git worktree isolation

  - Specs live in `specs/[feature-name]/` at the project root (not in `.claude/`)
  - Each spec has: PLAN.md, requirements.md, design.md, test-plan.md, tasks.md, review.md
  - Tests are integration-first and written before the code they verify; never weaken a test or edit requirements to get a pass
  - Commit messages for spec tasks use the `task [N]:` prefix
  ```

- **You never edited it:** `ck setup --force` is fine.
- **The spawn-worktree skill is missing or unchanged:** `ck setup` (without `--force`) installs anything missing.

### Restart Claude Code

Hooks and slash commands are loaded when a session starts. Close and reopen any running sessions after upgrading.

## 2. Each repository

Run this from the repository root:

```bash
ck upgrade --dry-run   # shows the plan, writes nothing
ck upgrade
```

`ck upgrade`:

- refreshes the files ck owns: `.claude/commands/spec/*`, `.claude/commands/spawn-worktree.md`, `.claude/output-styles/spec-driven.md`, and adds `.claude/workflows/spec-create.js`;
- merges the four hooks into `.claude/settings.local.json`, keeping other tools' hooks and permissions, and fixing the old `5000` timeout;
- creates `specs/ck.json` with a detected test command;
- moves `.claude/specs/*` to `specs/` with `git mv`, so history follows, and rewrites `.claude/specs/` references inside them. Use `--no-migrate` to skip this step.

It never touches `.claude/CLAUDE.md` or your spec content. If you customized a ck command file that git doesn't track, it is saved as `<file>.bak` before being replaced. Tracked files can be reviewed with `git diff`.

Then:

1. **Check the verify command.** Open `specs/ck.json` and make sure `"verify"` runs your test suite, for example `["uv run pytest -q"]`, `["npm test --silent"]`, or `["go test ./..."]`. The gate is only as good as this command. Leave it empty only if every task has its own `**Verify:**` line.
2. **Review and commit:**
   ```bash
   git status && git diff .claude specs
   git add .claude/commands .claude/workflows .claude/output-styles specs
   git commit -m "Upgrade claude-kiro to 0.2"
   ```
3. **Restart Claude Code sessions** in this repo, then run `ck doctor`.

### Other clones of the same repository

After pulling the upgrade commit, the commands, workflow, `specs/ck.json`, and moved specs arrive through git. The hooks don't, because `.claude/settings.local.json` is local to each clone. Run `ck upgrade` once in every other clone, on every machine. It will only add the hooks.

To share hooks through git instead, copy the `"hooks"` block that `ck hook config` prints into `.claude/settings.json` (the shared, committed settings file). Every clone then gets the hooks as soon as it pulls, but every clone also needs the 0.2 `ck` on its PATH.

### Specs already in progress

- **They keep working.** The task parser also reads the older format (`### Task1:`, and `✅ COMPLETE` in the title counts as Done). Tasks without `**Track:**` are treated as implementation tasks.
- **The gate applies to them.** When Claude marks one of their tasks Done, the acceptance boxes are checked and the verify commands run. For older specs without `**Verify:**` or `**Verified by:**` lines, only the project-wide verify commands run.
- **Optional: add a test plan.** `/spec:review <name>` runs the new four-lens review. On a spec without `test-plan.md`, it flags the gap and writes one. This is worth doing for specs with significant work still ahead.

### Tuning or switching things off

All of these settings live in `specs/ck.json`:

```json
{
  "verify": ["uv run pytest -q"],
  "verify_timeout": 540,
  "guard": { "tests": "ask", "requirements": "ask", "test_plan": "ask" },
  "base": null
}
```

- `guard.tests`, `guard.requirements` and `guard.test_plan` each take `"ask"` (prompt me), `"deny"` (block and tell Claude why), or `"off"`.
- `base` is the git ref the tamper check compares against. Leave it `null` to use the merge-base with `origin/HEAD`, `main`, or `master`.
- To list which files count as tests, add `"tests": {"dirs": [...], "files": [...]}`.
- To disable the gate entirely, remove the `done-verify` (PostToolUse) and `stop-verify` (Stop) entries from `.claude/settings.local.json`.

## Rolling back

```bash
uv tool install --force claude-kiro==0.1.0                          # old ck
git revert <upgrade-commit>                                          # repo files and spec move
```

Then remove the `PreToolUse`, `PostToolUse`, `Stop`, and `SubagentStop` entries that run `ck --hook ...` from `.claude/settings.local.json`.

## Checklist

**Each machine**
- [ ] `uv tool install --force "git+https://github.com/kutlayhanli/claude-kiro@feat/specs-dir-and-workflow-create"`
- [ ] `ck setup --diff`, then paste the snippet or run `ck setup --force`
- [ ] Restart Claude Code

**Each repository** (once)
- [ ] `ck upgrade --dry-run`, then `ck upgrade`
- [ ] Check `"verify"` in `specs/ck.json`
- [ ] Review the diff and commit `.claude/commands .claude/workflows .claude/output-styles specs`
- [ ] Restart sessions, then run `ck doctor`

**Each additional clone**
- [ ] `git pull`, then `ck upgrade`, then restart sessions
