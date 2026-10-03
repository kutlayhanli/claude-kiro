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

- `specs/` - Feature specifications (PLAN, requirements, design, tasks, review)
- `.claude/workflows/spec-create.js` - Workflow run by /spec:create and /spec:review
- `.claude/output-styles/` - Claude Code behavioral configuration
- `.claude/commands/spec/` - Slash commands for spec workflow
- `.claude/settings.local.json` - Hook configuration

## Quick Start

```bash
# Discuss and decide the approach (writes specs/<name>/PLAN.md)
/spec:plan [feature-description]

# Write the spec from PLAN.md as a workflow, ending in adversarial review
/spec:create [feature-name]

# Adversarially review an existing spec
/spec:review [spec-name]

# Implement a spec task
/spec:implement [spec-directory]

# Run tasks in parallel with worktree isolation
/spawn-worktree [spec-directory-or-task-list]
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