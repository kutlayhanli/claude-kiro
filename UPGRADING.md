# Upgrading

- **From 0.7 to 0.8:** see the next section.
- **From 0.6 to 0.7:** see [0.6 → 0.7](#06--07), then the 0.8 notes.
- **From 0.5 to 0.6:** see [0.5 → 0.6](#05--06), then the 0.7 notes.
- **From 0.4 to 0.5:** see [0.4 → 0.5](#04--05).
- **From 0.3 to 0.4:** see [0.3 → 0.4](#03--04).
- **From 0.2:** apply the 0.3 notes below as well; the steps are the same.
- **From 0.1:** skip to [Upgrading to 0.2](#upgrading-to-02), then come back for the 0.3 and 0.4 notes (same commands).

## 0.7 → 0.8

What changes:
- **Escalation to bigger models on failure.** Run cheap and pay for a bigger model only where the cheap one fails: `ck agents set implementer.escalate sonnet,opus` (or `--escalate sonnet,opus` for one `/spec:implement` run). Once a task's own budget runs out on its role's model, it gets one more attempt per listed model, in order, at the role's effort: a retry after its gate keeps failing, a revision after blocking review findings survive the rounds, a fix after a red branch survives the fix. The test writer and fixer follow the implementer's list unless set; `none` turns it off. Off by default, so nothing changes until you set it.
- **Orchestration runs on a cheaper model.** The workflow's mechanical steps (plan, conflict-free merges, the per-merge, full and final gates, re-checks) used to run on the session's model, usually Opus. They now use the new **orchestrator** role, Sonnet at low effort. In the October benchmark these steps cost $1.9–3.1 per run on Opus.
- **Merge conflicts go to a strong model.** A merge step that hits a conflict now stops and hands it to the new **resolver** role (Opus, high effort, agent type `ck-resolver`) instead of resolving it at low effort.
- **The fixer defaults to Opus at medium effort** instead of following the implementer. Repairing a red target means deciding whether the environment or the code is at fault; Haiku fix agents got this wrong in 2 of 5 benchmark runs.
- **`ck init` offers a Workflow allow rule.** In auto mode a classifier may stop to ask before a spec workflow launches; an unattended session (cloud, `claude -p`) then waits forever. `ck init` now asks whether to add `"Workflow"` to `permissions.allow` in `.claude/settings.json` (default no). For an existing project, `ck init --allow-workflow` adds just that rule and skips files that already exist.
- Restore the old behaviour with `ck agents set orchestrator.model inherit`, `ck agents set resolver.model inherit` and `ck agents set fixer.model default` (and the same for `.effort`).
- **Handoff notes.** An agent that finishes without a passing gate, or with blocking review findings unresolved, writes `.claude/worktrees/<spec>-task-N.handoff.md` (what it tried, the exact errors, the branch state, what it would do next). Every retry, revision and escalated attempt reads it first, so a bigger model starts from the smaller one's findings instead of from scratch. The note sits next to the worktree, never in the branch.
- **Escalation never steps down.** A model in the list that is not above the role's own is skipped, so an Opus fixer following the implementer's `sonnet,opus` list does not escalate to Sonnet.
- **Unattended runs: `ck run <spec>`.** Starts the whole spec headless from the main checkout, with auto permissions and no prompts, the Workflow launch and ck/git commands allowed, and the process kept open until the workflow ends. Pass `/spec:implement` flags after `--`, e.g. `ck run auth -- --escalate sonnet,opus`.
- **No more `cd ... &&` in agent commands.** Agents run `ck -C <dir> ...`, `command git -C <dir> ...` and `uv sync --directory <dir>` as single simple commands. Compound commands were the main source of permission prompts and of refusals in worktree-isolated sessions.
- **The report lists escalations:** `escalated` (task, models tried, merged or not) and `fixerEscalations`, so you can see which tasks needed more than the cheap model.
- **`ck bench`: compare configurations properly.** `ck bench run bench.json` runs every (spec, config, rep) arm one at a time, in a seeded random order, each in a fresh clone at a pinned base with its agent settings committed; `ck bench score` overlays per-spec hidden acceptance tests and safety invariants (kept outside the repo) on each arm's final integration tree and reports hidden pass rate, USD per passing task, merged rate, escalations and wall time per arm with bootstrap CIs, plus paired per-task differences. Runs that needed a human (permission denial, `AskUserQuestion`) fail closed. `ck bench import` converts an existing workflow output file so pilot runs can be scored the same way. See the README section "Benchmarking model configurations".
- **`ck run` options for machine-readable runs:** `--output-format json|stream-json` (stdout to the log, stderr to `<log>.stderr`), `--session-id <uuid>` and `--log <path>`. The default is unchanged.
- **`ck --version`** prints the installed version.

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
| Done | Agent decides | `ck gate` decides; a Stop hook blocks a Done claim that fails it |
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
  "guard": { "tests": "ask", "requirements": "ask" },
  "base": null
}
```

- `guard.tests` and `guard.requirements` each take `"ask"` (prompt me), `"deny"` (block and tell Claude why), or `"off"`.
- `base` is the git ref the tamper check compares against. Leave it `null` to use the merge-base with `origin/HEAD`, `main`, or `master`.
- To list which files count as tests, add `"tests": {"dirs": [...], "files": [...]}`.
- To disable the gate entirely, remove the `Stop` and `SubagentStop` entries from `.claude/settings.local.json`.

## Rolling back

```bash
uv tool install --force claude-kiro==0.1.0                          # old ck
git revert <upgrade-commit>                                          # repo files and spec move
```

Then remove the `PreToolUse`, `Stop`, and `SubagentStop` entries that run `ck --hook ...` from `.claude/settings.local.json`.

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
