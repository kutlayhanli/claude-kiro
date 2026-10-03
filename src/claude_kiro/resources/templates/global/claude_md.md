# Claude Code Global Configuration

## Spec-Driven Development

This configuration is managed by Claude Kiro (`ck`).

When working on new features, follow the spec-driven workflow:

1. `/spec:plan` - Discuss and decide the approach with me, interactively (writes PLAN.md)
2. `/spec:create` - Workflow that writes requirements, design, and tasks, then adversarially reviews them
3. `/spec:implement` - Implement a test-track or impl-track task; Done only when `ck gate` passes
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
- Specs live in `specs/[feature-name]/` at the project root (not in `.claude/`, which needs approval for every write)
- Each spec has: PLAN.md, requirements.md, design.md, test-plan.md, tasks.md, review.md
- Tests are integration-first and written before the code they verify; never weaken a test or edit requirements to get a pass
