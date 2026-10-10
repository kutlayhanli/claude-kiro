---
description: Run parallel implementation tasks using git worktrees for filesystem isolation
argument-hint: [task-specification-or-spec-directory] [--model sonnet|opus|haiku|fable] [--effort low|medium|high|xhigh|max]
allowed-tools: Read, Write, Edit, Grep, Glob, Bash, Agent, SendMessage, AskUserQuestion, Workflow
---

Run parallel implementation batch: $ARGUMENTS

# Spawn-Worktree: Parallel Implementation with Git Worktree Isolation

Run multiple implementation tasks concurrently, each in its own git worktree with a dedicated branch. This prevents working directory conflicts between parallel agents.

## When to Use

- Spec tasks I want to run here, with my approval before launch and a say in merging
- An ad-hoc batch of independent tasks (no spec)

**For a whole spec, use `/spec:implement <spec>` instead.** It runs the same dependency-driven schedule as one workflow, with a reviewer, retries and a fixer for red merges. This command is for runs I want to steer.

**No waves.** A task starts as soon as its own dependencies are merged, not when every task of an earlier "wave" is. A task waiting on one slow, unrelated task wastes wall-clock time, and on a 60-task spec that adds up to hours.

## Workflow

### Phase 1: Parse and Validate

1. **Read the spec** if a spec directory was provided:
   - Run `ck plan <spec> --json`. Its `ready` list holds the open tasks whose dependencies are all Done, longest remaining chain first; `criticalPath` is the longest chain. Ignore any `## Parallel Groups` section in older specs: it only labels progress.
   - If I named tasks, run those that are ready. Report any I named that are still waiting, and what they wait for.
   - Tasks have a **Track:** `test` or `impl`. Test tasks write the tests that later impl tasks must pass, so an impl task lists its verifying test tasks in its Dependencies and becomes ready once they merge.
   - Load `requirements.md`, `design.md`, and `test-plan.md` for context
   - Run `ck lint <spec>`. On a dependency or verify-order cycle, stop and show it, as `/spec:implement` does.

2. **If no spec given**, parse the user's description into discrete tasks

3. **Validate environment**:
   ```bash
   git worktree list
   git status --short
   ```
   - Must be a git repository
   - Warn if uncommitted changes exist
   - Clean up stale worktrees from previous runs if found

4. **Confirm the plan** with me before proceeding. Show:
   - The tasks that start now (at most 6 at once unless I gave another limit), with branch names and tracks
   - The critical path, and how many tasks are still waiting on dependencies
   - Use AskUserQuestion to get approval, with these choices:
     - **Keep going** (recommended for a spec): merge each task as soon as its gate passes, and start every task that this makes ready, until nothing is ready or running
     - **Just these tasks**: run them, merge as they pass, then stop and report what became ready
     - **Hold merges**: run them, and ask me before merging anything (nothing new becomes ready until I approve)

### Phase 2: Pre-create Worktrees

**Spec tasks:** create the worktrees of the tasks you're about to start, **before spawning their agents**:

```bash
ck worktree create <spec> <N> <M> ... --install
```

This creates `.claude/worktrees/<spec>-task-<N>` on branch `feat/<spec>-task-<N>` and installs dependencies. An existing worktree is reused. A worktree that is claimed by another agent, or has uncommitted changes, is reported **BUSY** and left alone: don't spawn an agent into it.

**Ad-hoc tasks (no spec):** `command git worktree add .claude/worktrees/<feature>-<n> -b feat/<feature>-<n>`.

Run git as `command git` (a shell hook may rewrite plain `git`). Verify with `ck worktree status <spec>` (or `command git worktree list`). **If a worktree fails to create, or is BUSY, don't spawn into it.** At the first launch, abort the whole run and report: don't proceed with partial isolation. Later, report it and leave that task (and its dependents) unstarted.

### Phase 3: Spawn Parallel Agents

For each task, spawn an agent with `run_in_background: true`:

```json
{
  "description": "implement <feature>: <task-summary>",
  "prompt": "Your task: Task <N> of spec <name>.\nWorktree: <absolute main checkout>/.claude/worktrees/<name>-task-<N> (branch feat/<name>-task-<N>). Target branch for the final merge: <current branch>.\nRead your operating brief at <absolute main checkout>/.claude/workflows/spec-implement-brief.md and follow it exactly: claim the worktree with `ck worktree claim <name> <N>`, implement per your track, get `ck gate <name> --task <N>` passing, merge the target into your branch before finishing, release the worktree.\nReport: status, gatePassed, summary, commits, gate output tail, blocker, deviations, wrong tests, files outside your task.",
  "subagent_type": "general-purpose",
  "run_in_background": true
}
```

**Models, effort, and review:** run `ck agents --json` (adding `--override` for anything I asked, as described in `/spec:implement` under Agents) and use its roles: the implementer for impl-track tasks, the test_writer for test-track tasks, the reviewer after each task. Values are a family alias (`sonnet`, `opus`, `haiku`, `fable`, each the newest of its family) or `inherit`, and an effort from `low` to `max` or `inherit`.
- **Everything `inherit`, review off:** use the Agent calls above as they are.
- **Otherwise:** the Agent tool can't set effort, so launch the tasks you start together as one Workflow call instead of separate Agent calls (workflows run in the background, so a later launch never waits for an earlier one): an inline script that runs the same per-task prompts in `parallel()`, each `agent(prompt, { label: 'task <N>', model, effort })` with that task's role settings (omit any that are `inherit`). If the reviewer is enabled, the script runs a reviewer `agent()` with the reviewer's `{ model, effort }` for each task whose gate passed, reading `command git -C <worktree> diff <target>...<branch>` and the spec without editing, and gives blocking findings to one revising implementer `agent()` (up to the reviewer's `rounds`) before the merge phase. The worktrees are already created, so don't pass `isolation`. Running this command is my opt-in to that workflow.
- **No Workflow tool** (missing, refused, or blocked): run `ck agents sync` (with the same overrides) and spawn the Agent calls above with `subagent_type` set to the task's type, `ck-implementer` or `ck-test-writer`. Those types carry the role's model and effort. If the reviewer is enabled, run a `ck-reviewer` for each task whose gate passed, before the merge phase. Send blocking findings back to that task's agent with SendMessage, up to the reviewer's `rounds`. Details: `.claude/workflows/without-workflow-tool.md`.

Name each role's model and effort, and whether review is on, in the Phase 1 plan you show me.

**Important agent prompt rules:**
- Include the FULL worktree path so the agent knows where to operate
- Point to the brief by absolute path; it covers claiming, track rules, the gate, committing, and staying inside the worktree
- Don't spawn into a worktree that `ck worktree create` reported BUSY

### Phase 4: As Each Task Finishes

Don't wait for the others. Handle each agent's report as it arrives:

1. **Log its status.** If it failed (not done, or gate not passed), note the error. Its dependents won't become ready; other tasks carry on.
2. **Merge it** (unless I chose "Hold merges"; then ask me first). One merge and its gate at a time; queue the rest:
   ```bash
   ck worktree merge <spec> <N>
   ck gate <spec> --task <N>      # in the background; wait for the real exit code, a slow gate isn't red
   ```
   `ck worktree merge` merges the branch into the current branch (`--no-ff`), then removes the worktree and branch. The task gate checks that task on the merged result, where its tests and the code they verify meet: its Verify, its verifying tests, and that no existing test was weakened.
   **Ad-hoc tasks (no spec):** `command git merge --no-ff feat/<feature>-<n>`, then run the project's tests. There's no dependency graph, so nothing new becomes ready.
3. **Start what became ready** (spec tasks, unless I chose "Just these tasks"): `ck plan <spec> --json --exclude <running and failed tasks>`, then Phases 2 and 3 for its `ready` tasks, keeping at most 6 running. Re-planning re-reads tasks.md, so a dependency we fix mid-run takes effect.

If a merge reports **CONFLICT**:
- The merge is already aborted. Nothing is half-merged on the current branch.
- Stop starting new tasks, report which task and files conflicted, and let me decide. To resolve: run `command git -C .claude/worktrees/<spec>-task-<N> merge <current branch>` **inside the task worktree**, fix the conflicts there (keep both sides' tests), commit, then run `ck worktree merge <spec> <N>` again. Never resolve in the main checkout.

If a task gate is **red**: stop starting new tasks, set that task back to In Progress in tasks.md, report the gate output, and ask me whether to fix it here or re-run the task. Agents already running may finish, but don't merge anything onto a red branch.

### Phase 5: Final Gate and Report

When nothing is running or ready (or I chose "Just these tasks" and they're all handled), run the full gate once:

```bash
ck gate <spec>
```

Then report:
```
Run complete (42 min):
  [merged] Task 2 [test]: 3 commits, gate PASS
  [merged] Task 4 [impl]: 5 commits, gate PASS
  [fail]   Task 3 [test]: <error summary>; dependents not started: 5, 6
  Waiting on dependencies: none · Full gate: PASS
```
List failed worktrees and offer to clean them up.

## Error Handling

| Situation | Response |
|-----------|----------|
| Worktree creation fails | First launch: abort and clean up any created worktrees. Later: skip that task and its dependents, report |
| Agent fails mid-task | Log error, keep running and launching tasks that don't depend on it, report at end |
| Merge conflict | Stop launching, report conflict details, wait for my decision |
| Red task gate | Stop launching and merging, report, wait for my decision |
| Uncommitted changes in main | Warn me, ask whether to stash or abort |
| Stale worktrees from previous run | Offer to clean up before starting |

## Cleanup Commands

If something goes wrong, these commands clean up manually:

```bash
# List all worktrees
git worktree list

# Remove a specific worktree
git worktree remove worktrees/<name>

# Force-remove if worktree has changes
git worktree remove --force worktrees/<name>

# Delete a branch
git branch -D feat/<name>

# Prune stale worktree references
git worktree prune
```

## Example

```
User: /spawn-worktree specs/auth-system

Claude: Task 1 (test infrastructure) is Done. Ready now, longest chain first:

Plan:
  Task 2 [test] login tests        -> .claude/worktrees/auth-system-task-2
  Task 4 [impl] user model         -> .claude/worktrees/auth-system-task-4
  Task 3 [test] session tests      -> .claude/worktrees/auth-system-task-3

Critical path: 2 -> 5 -> 7 (3 of 6 open tasks). Waiting: 5 (needs 2, 4),
6 (needs 3), 7 (needs 5). Implementer sonnet/medium, reviewer off.

Keep going / Just these tasks / Hold merges? [asking user]

User: keep going

Claude: Spawning 3 agents...
  [merged] Task 4: gate PASS
  [merged] Task 2: gate PASS -> Task 5 ready, started (Task 3 still running)
  [merged] Task 3: gate PASS -> Task 6 ready, started
  [merged] Task 5: gate PASS -> Task 7 ready, started
  ...
Run complete (51 min). 6 merged, 0 failed. Full gate: PASS.
```
