---
description: Run parallel implementation tasks using git worktrees for filesystem isolation
argument-hint: [task-specification-or-spec-directory]
allowed-tools: Read, Write, Edit, Grep, Glob, Bash, Agent, AskUserQuestion
---

Run parallel implementation batch: $ARGUMENTS

# Spawn-Worktree: Parallel Implementation with Git Worktree Isolation

Run multiple implementation tasks concurrently, each in its own git worktree with a dedicated branch. This prevents working directory conflicts between parallel agents.

## When to Use

- Implementing multiple independent tasks from a spec simultaneously
- Tasks that touch different files and have no dependencies on each other
- Batch work where speed matters more than sequential review

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

For each task, **before spawning any agents**:

```bash
# Create worktree with its own branch
git worktree add worktrees/<task-name> -b feat/<task-name>
```

**Naming conventions:**
- Worktree directory: `worktrees/<feature>-<task-n>` (e.g., `worktrees/auth-login-task1`)
- Branch name: `feat/<feature>-<task-n>` (e.g., `feat/auth-login-task1`)

Verify ALL worktrees were created:
```bash
git worktree list
```

**If any worktree fails to create, abort the entire batch.** Do not proceed with partial isolation.

### Phase 3: Spawn Parallel Agents

For each task, spawn an agent with `run_in_background: true`:

```json
{
  "description": "implement <feature>: <task-summary>",
  "prompt": "You are implementing Task <N> of spec <name> in an isolated git worktree.\n\nWORKING DIRECTORY: <absolute-path>/worktrees/<task-name>\nBRANCH: feat/<task-name>\n\n## Context\n<paste relevant sections from requirements.md, design.md, and (for test tasks) test-plan.md>\n\n## Task\n<paste the full task block from tasks.md, including Track, Verify, Verified by/Verifies>\n\n## Instructions\n1. Follow .claude/commands/spec/implement.md for this task (read it from your worktree). In short: mark the task In Progress, commit with the `task <N>:` prefix, and only mark it Done after `ck gate specs/<name> --task <N>` passes in your worktree.\n2. Track test: write the tests from test-plan.md against the real boundaries. They may fail until the implementation lands, but must fail for the right reason.\n3. Track impl: make the verifying tests pass. Never delete, skip, or weaken a test, and never edit requirements.md. If a test looks wrong, stop and report it in your final message.\n4. All file operations MUST be relative to your working directory. Do NOT modify files outside your worktree.\n5. Commit your changes to your branch when done.",
  "subagent_type": "general-purpose",
  "run_in_background": true
}
```

**Important agent prompt rules:**
- Include the FULL working directory path so the agent knows where to operate
- Paste relevant spec context directly into the prompt (agents can't read from main worktree)
- Tell the agent to commit when done
- Tell the agent NOT to modify files outside its worktree

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
# For each completed branch
git merge feat/<task-name> --no-ff -m "Merge feat/<task-name>: <task-summary>"

# Clean up
git branch -d feat/<task-name>
git worktree remove worktrees/<task-name>
```

If merge conflicts occur:
- Stop merging
- Report which branch conflicted and what files
- Let me decide how to resolve

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
