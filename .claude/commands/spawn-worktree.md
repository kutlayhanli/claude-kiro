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
   - Load `tasks.md` from `.claude/specs/[name]/`
   - Identify tasks that can run in parallel (no dependencies between them)
   - Load `design.md` and `requirements.md` for context

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
  "prompt": "You are implementing a specific task in an isolated git worktree.\n\nWORKING DIRECTORY: <absolute-path>/worktrees/<task-name>\nBRANCH: feat/<task-name>\n\n## Context\n<paste relevant sections from requirements.md and design.md>\n\n## Task\n<paste specific task from tasks.md>\n\n## Instructions\n1. All file operations MUST be relative to your working directory\n2. Commit your changes to your branch when done\n3. Include tests as specified in the task\n4. Do NOT modify files outside your worktree",
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
User: /spawn-worktree .claude/specs/auth-system

Claude: I found 5 tasks in the auth-system spec. Tasks 1-3 are independent
and can run in parallel. Tasks 4-5 depend on 1-3.

Plan:
  Agent 1: worktrees/auth-user-model (feat/auth-user-model) - Task 1
  Agent 2: worktrees/auth-password-hash (feat/auth-password-hash) - Task 2
  Agent 3: worktrees/auth-middleware (feat/auth-middleware) - Task 3

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
```
