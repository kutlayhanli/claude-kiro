# Claude Code Global Configuration

## Spec-Driven Development

This configuration is managed by Claude Kiro (`ck`).

When working on new features, follow the spec-driven workflow:

1. `/spec:plan` - Research and compare approaches before committing to a direction
2. `/spec:create` - Write requirements, design, and task breakdown
3. `/spec:implement` - Implement tasks with granular commits and task tracking
4. `/spawn-worktree` - Run independent tasks in parallel with git worktree isolation

## Parallel Implementation

When running parallel implementation tasks (e.g., multiple subagents implementing separate features):

### Worktree Isolation

Use `/spawn-worktree` to run tasks in parallel. Each subagent gets its own git worktree and branch, preventing working directory conflicts.

### Naming Conventions

| Element | Pattern | Example |
|---------|---------|---------|
| Worktree dir | `worktrees/<feature>-<task-n>` | `worktrees/auth-login-task1` |
| Branch name | `feat/<feature>-<task-n>` | `feat/auth-login-task1` |
| Commit prefix | `task [N]:` | `task 3: add auth middleware` |

### Merge Workflow

After parallel agents complete:
1. Review each branch's changes
2. Merge completed branches: `git merge feat/<branch> --no-ff`
3. Clean up: `git branch -d feat/<branch> && git worktree remove worktrees/<name>`

## Conventions

- Commit messages for spec tasks use `task [N]:` prefix
- Specs live in `.claude/specs/[feature-name]/`
- Each spec has: requirements.md, design.md, tasks.md (and optionally PLAN.md)
