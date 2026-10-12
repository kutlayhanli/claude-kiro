# Claude Kiro - Spec-Driven Development Configuration

This project uses Claude Kiro for spec-driven development methodology.

## Project Overview

[Add your project description here]

## Development Approach

This project follows spec-driven development:

1. **Specifications First**: All features start with a specification
2. **EARS Requirements**: Requirements use "WHEN ... THE SYSTEM SHALL ..." format
3. **Test-Driven**: Write tests before implementation
4. **Systematic Implementation**: Follow task breakdowns from specs

## Project Structure

- `specs/` - Feature specifications (PLAN, requirements, design, test-plan, tasks, review)
- `specs/ck.json` - Verify commands, test patterns, and guard settings for the verification gate
- `.claude/settings.json` - Enables the `spec@claude-kiro` plugin (the /spec:* commands, workflows, agents and hooks; `ck setup` installs it once per machine). Projects set up with `ck vendor` carry copies in `.claude/commands/spec/` and `.claude/workflows/` instead

## Quick Start

```bash
# Discuss and decide the approach (writes specs/<name>/PLAN.md)
/spec:plan [feature-description]

# Write the spec from PLAN.md as a workflow, ending in adversarial review
/spec:create [feature-name]

# Adversarially review an existing spec
/spec:review [spec-name]

# Implement a spec task
/spec:implement [task-number]

# Check a task is really done (also runs automatically when a task is marked Done)
ck gate [spec-name] --task [N]

# Run tasks in parallel with worktree isolation
/spec:spawn-worktree [spec-directory-or-task-list]
```

## Conventions

[Add your project-specific conventions here]

- Naming conventions
- File organization
- Testing approach
- Documentation standards

## Technical Stack

[List your key technologies]

## Contact

[Add team/maintainer information]