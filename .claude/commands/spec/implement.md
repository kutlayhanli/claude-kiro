---
description: Implement a task from the specification
argument-hint: [task-number-or-description]
allowed-tools: Read, Write, Edit, Grep, Glob, Bash, TodoWrite
---

Implement task: $ARGUMENTS

# Implementation Guidelines

## Before Starting

1. **Load the spec context:**
   - Read requirements.md to understand the "why"
   - Read design.md to understand the "how"
   - Read tasks.md to understand dependencies and current status

2. **Update task tracking:**
   - In tasks.md, change your task's status to `**Status:** In Progress`
   - Mark this task as "in_progress" in TodoWrite
   - Commit: `git commit -m "task [N]: mark in progress"`

3. **Verify prerequisites:**
   - Check that dependent tasks are marked completed in tasks.md
   - Ensure you have all necessary context

## During Implementation

### Follow the Design
- Implement exactly as specified in design.md
- Use the file paths specified
- Use the interfaces/types specified
- Follow the error handling strategy specified

### Test-Driven Approach
1. Write failing tests first (based on acceptance criteria)
2. Implement minimum code to pass tests
3. Refactor while keeping tests green

### Code Quality
- Follow project coding standards (check CLAUDE.md)
- Add clear comments for complex logic
- Use meaningful variable names
- Handle edge cases from requirements.md

### Granular Commits

Commit after each meaningful milestone, not just at the end. Use this pattern:

```
git add <specific-files>
git commit -m "task [N]: <what was done>"
```

**Commit after each of these milestones:**
1. Task marked in progress (tasks.md update)
2. Tests written (before implementation)
3. Core implementation complete
4. Edge cases and error handling added
5. Task marked done (tasks.md update)

**Commit message format:** Always prefix with `task [N]:` so it's clear which spec task the commit belongs to. This is critical when running in parallel — each agent's commits must be attributable.

## Task Tracking in tasks.md

### Updating Your Task

As you work, update **only your task's section** in tasks.md. Do not modify other tasks.

**Mark acceptance criteria done as you complete them:**

```markdown
### Task 3: Create authentication middleware
**Status:** In Progress

**Acceptance:**
- [x] Implementation complete
- [x] Unit tests written and passing
- [ ] Integration tests written and passing  ← still working
- [ ] Error handling implemented
- [ ] Edge cases covered
```

**When finished, update the status:**

```markdown
### Task 3: Create authentication middleware
**Status:** Done
```

### Parallel Safety

When running as a subagent via `/spawn-worktree`:
- You have your own copy of tasks.md in your worktree
- **Only update YOUR task** — never check/uncheck items in other tasks
- Your tasks.md changes will be merged back with others' changes
- Keep task updates minimal and confined to your section to reduce merge conflicts

## After Implementation

### Verification Checklist
- [ ] All unit tests passing
- [ ] Integration tests passing (if applicable)
- [ ] Error handling implemented
- [ ] Edge cases covered
- [ ] Code follows project standards
- [ ] Documentation updated

### Finalize

1. **Mark task done in tasks.md:**
   - Set `**Status:** Done`
   - Check all acceptance criteria: `- [x]`
   - Add completion note if there are deviations

2. **Update TodoWrite:**
   - Mark task as "completed" ONLY if ALL verification items pass
   - If blocked, keep as "in_progress" and create new task for blocker

3. **Final commit:**
   ```
   git add -A
   git commit -m "task [N]: complete — <brief summary>"
   ```

### Spec Sync
If implementation differs from design:
- Document the deviation in your task section of tasks.md
- Explain why the change was necessary
- Update design.md if the deviation is intentional

## Output

Provide:
1. Summary of what was implemented
2. Files changed/created with commit hashes
3. Test results
4. Any deviations from spec and why
5. Next recommended task (if any)
