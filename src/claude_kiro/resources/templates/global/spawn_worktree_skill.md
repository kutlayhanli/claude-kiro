---
name: spawn-worktree
description: "Run a parallel implementation batch using git worktrees for true filesystem isolation. Use when implementing multiple features/tasks concurrently. Each subagent gets its own git worktree and branch, preventing working directory conflicts. Trigger when user says 'parallel', 'batch', 'spawn-worktree', 'worktrees', or asks to run tasks concurrently."
---

# Spawn-Worktree Parallel Implementation Skill

Run parallel implementation batches with **actual git worktree isolation** for each subagent.

> **Implementing a whole claude-kiro spec?** Use `/spec:implement <spec>` instead. It runs every wave of `tasks.md` as one workflow, with parallel task agents, merges as tasks finish, a `ck gate` check after each wave, and a halt on red. Use this skill for a single wave or an ad-hoc batch. For spec tasks, create and merge worktrees with `ck worktree create <spec> <N...> --install` and `ck worktree merge <spec> <N...>` rather than raw git, and run git as `command git` if a shell hook rewrites it.

## Overview

This skill pre-creates git worktrees before spawning agents, ensuring true filesystem isolation — not just context isolation.

1. **Pre-creates** git worktrees before spawning agents
2. **Spawns agents** targeting their respective worktrees
3. **Verifies** worktree creation and isolation
4. **Merges** completed branches back

## Usage

```
/spawn-worktree [task-specification]
```

Or pass a structured task list:

```json
{
  "tasks": [
    {
      "name": "auth-login-task1",
      "branch": "feat/auth-login-task1",
      "prompt": "Implement login flow tasks 1-3 from SPEC.md..."
    },
    {
      "name": "auth-session-task2",
      "branch": "feat/auth-session-task2",
      "prompt": "Implement session management tasks 4-5 from SPEC.md..."
    }
  ]
}
```

## Workflow

### Phase 1: Validate Environment

1. **Verify git worktree support:**
   ```bash
   git worktree list
   ```
   If worktrees exist from previous runs, offer cleanup.

2. **Confirm target repo:**
   - Must be a git repository
   - Warn if uncommitted changes exist

3. **Parse task list** from user input or structured spec

4. **Confirm plan** with user before proceeding

### Phase 2: Pre-create Worktrees

For each task, execute **before spawning any agents**:

```bash
git worktree add worktrees/<name> -b <branch>
```

Verify ALL worktrees were created with `git worktree list`. **If any fail, abort the batch.**

### Phase 3: Spawn Parallel Agents

For each task, spawn an agent with:

```json
{
  "description": "implement <feature>: <task-summary>",
  "prompt": "<task.prompt>\n\nIMPORTANT: Work in the directory <path>/worktrees/<name>. Commit your changes to the branch <branch>. Prefix commits with 'task [N]:'.",
  "subagent_type": "general-purpose",
  "run_in_background": true
}
```

**Model and effort:** for claude-kiro spec tasks, take each role's model and effort from `ck agents --json` (implementer: Sonnet medium, reviewer: Opus high by default; see `/spec:implement`). For ad-hoc batches, omit both so agents inherit the session's unless the user asks; pass a family alias (`sonnet`, `opus`, `haiku`, `fable`; it resolves to that family's latest model) as `"model"`. The Agent tool can't set effort, so when an effort is set, launch the batch as one Workflow call whose inline script runs each task prompt in `parallel()` with `agent(prompt, { model, effort })`.

### Phase 4: Monitor and Wait

1. Track spawned agents by description
2. Log progress as agents complete
3. If an agent fails, note it but continue others

### Phase 5: Verify and Merge

After all agents complete:

1. **Check each branch has commits:**
   ```bash
   git log <branch> --oneline -5
   ```

2. **Report results:**
   ```
   [ok] <task-1>: <n> commits on feat/<task-1>
   [fail] <task-3>: <error summary>
   ```

3. **Ask user** how to proceed: merge all, review individually, or clean up

4. **Merge** (on approval):
   ```bash
   git merge <branch> --no-ff -m "Merge <branch>: <summary>"
   git branch -d <branch>
   git worktree remove worktrees/<name>
   ```

## Error Handling

| Error | Response |
|-------|----------|
| Worktree creation fails | Abort batch, clean up any created worktrees |
| Agent fails mid-task | Log error, continue others, report at end |
| Merge conflict | Stop, report conflict details, wait for user decision |
| Uncommitted changes | Warn user, ask whether to stash or abort |

## Cleanup

```bash
git worktree list              # List all worktrees
git worktree remove worktrees/<name>  # Remove a specific worktree
git branch -D feat/<name>      # Delete a branch
git worktree prune             # Prune stale references
```
