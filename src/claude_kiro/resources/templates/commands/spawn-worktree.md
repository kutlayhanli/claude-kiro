---
description: Run parallel implementation tasks using git worktrees for filesystem isolation
argument-hint: [task-specification-or-spec-directory]
allowed-tools: Read, Write, Edit, Grep, Glob, Bash, Agent, AskUserQuestion
---

Run parallel implementation batch: $ARGUMENTS

# Spawn-Worktree: Parallel Implementation with Git Worktree Isolation

Run multiple implementation tasks concurrently, each in its own git worktree with a dedicated branch. This prevents working directory conflicts between parallel agents.

## When to Use

- **One wave**, or an ad-hoc batch of independent tasks, with my approval at each step
- Tasks that touch different files and have no dependencies on each other

**For a whole spec, use `/spec:implement <spec>` instead.** It runs every wave as one workflow: task agents in parallel, merges as tasks finish, a gate after each wave, and a halt on red. This command is for single waves and ad-hoc batches.

## Workflow

### Phase 1: Parse and Validate

1. **Read the spec** if a spec directory was provided:
   - Load `tasks.md` from `specs/[name]/`
   - Use its **Parallel Groups** section as the wave plan. Run one wave at a time: the next wave starts only after the current one is merged and passes the wave gate (Phase 7).
   - Pick the first wave whose tasks are not all Done, and skip tasks already Done.
   - Tasks have a **Track:** `test` or `impl`. Test tasks write the tests that later impl tasks must pass, and usually run in an earlier wave than the impl tasks they verify. Both tracks can share a wave when they touch different files.
   - Load `requirements.md`, `design.md`, and `test-plan.md` for context

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
   - How many agents will be spawned
   - Task names and branch names
   - Which spec tasks each agent will implement
   - Use AskUserQuestion to get approval

### Phase 2: Pre-create Worktrees

**Spec tasks:** create all of the wave's worktrees **before spawning any agents**:

```bash
ck worktree create <spec> <N> <M> ... --install
```

This creates `.claude/worktrees/<spec>-task-<N>` on branch `feat/<spec>-task-<N>` and installs dependencies. An existing worktree is reused. A worktree that is claimed by another agent, or has uncommitted changes, is reported **BUSY** and left alone: don't spawn an agent into it.

**Ad-hoc tasks (no spec):** `command git worktree add .claude/worktrees/<feature>-<n> -b feat/<feature>-<n>`.

Run git as `command git` (a shell hook may rewrite plain `git`). Verify with `ck worktree status <spec>` (or `command git worktree list`). **If any worktree fails to create, or is BUSY, abort the entire batch.** Do not proceed with partial isolation.

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

**Important agent prompt rules:**
- Include the FULL worktree path so the agent knows where to operate
- Point to the brief by absolute path; it covers claiming, track rules, the gate, committing, and staying inside the worktree
- Don't spawn into a worktree that `ck worktree create` reported BUSY

### Phase 4: Monitor Completion

1. Wait for all agents to complete
2. As each finishes, log its status
3. If an agent fails, note the error but let others continue

### Phase 5: Verify and Report

After all agents complete:

1. **Check each branch has commits**:
   ```bash
   git log feat/<task-name> --oneline -5
   ```

2. **Report results**:
   ```
   Parallel batch complete:
     [ok] <task-1>: <n> commits on feat/<task-1>
     [ok] <task-2>: <n> commits on feat/<task-2>
     [fail] <task-3>: <error summary>
   ```

3. **Ask me** how to proceed:
   - Merge all completed branches to current branch?
   - Review branches individually first?
   - Clean up failed worktrees?

### Phase 6: Merge (on approval)

If I approve merging:

```bash
ck worktree merge <spec> <N> <M> ...
```

This merges each task branch into the current branch one at a time (`--no-ff`), then removes the worktree and branch. It stops at the first conflict.

If a merge reports **CONFLICT**:
- The merge is already aborted. Nothing is half-merged on the current branch.
- Report which task and files conflicted, and let me decide. To resolve: run `command git -C .claude/worktrees/<spec>-task-<N> merge <current branch>` **inside the task worktree**, fix the conflicts there (keep both sides' tests), commit, then run `ck worktree merge <spec> <N>` again. Never resolve in the main checkout.

### Phase 7: Wave Gate

After a wave is merged, the test track and the impl track meet for the first time. Before starting the next wave, run:

```bash
ck gate specs/<name>
```

This verifies every Done task on the merged result: acceptance boxes, verify commands, each impl task's verifying tests, and that no existing test was weakened.

- **Pass:** report it, then offer to start the next wave.
- **Fail:** set the failing tasks back to In Progress in tasks.md, report the gate output, and ask me whether to fix it here or re-run those tasks. Never start the next wave on a red gate.

## Error Handling

| Situation | Response |
|-----------|----------|
| Worktree creation fails | Abort entire batch, clean up any created worktrees |
| Agent fails mid-task | Log error, continue other agents, report at end |
| Merge conflict | Stop, report conflict details, wait for my decision |
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

Claude: The auth-system spec has 3 waves. Wave 1 (test infrastructure) is Done.
Wave 2 has 3 tasks that touch different files:

Plan:
  Agent 1: worktrees/auth-login-tests (feat/auth-login-tests) - Task 2 [test]
  Agent 2: worktrees/auth-session-tests (feat/auth-session-tests) - Task 3 [test]
  Agent 3: worktrees/auth-user-model (feat/auth-user-model) - Task 4 [impl, no verifying tests]

Wave 3 (Tasks 5-6, impl verified by Tasks 2-3) starts after this wave merges
and passes `ck gate`.

Proceed? [asking user]

User: go

Claude: Creating worktrees... done.
Spawning 3 agents...

[agents work in background]

Results:
  [ok] auth-user-model: 3 commits
  [ok] auth-password-hash: 2 commits
  [ok] auth-middleware: 4 commits

All 3 tasks completed. Merge to current branch?

User: yes

Claude: Merged 3 branches. Wave gate: PASS (Tasks 1-4). Start wave 3?
```
