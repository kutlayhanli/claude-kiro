# Claude Kiro - Spec-Driven Development Configuration

This project creates spec-driven development methodology for Claude Code using configuration files (slash commands, output styles, hooks).

## Hook Message Pattern

When writing hook messages or slash command instructions:

**Use "we/you/me" framing:**
- "We" = user and Claude collaborating
- "You" = Claude (the AI assistant)
- "Me" = the user

**Example:**
```
WHEN we work on new feature code, you should suggest to me creating a specification first.
```

**Not:**
```
WHEN user works on features, THE SYSTEM SHALL suggest...
```

Hook messages are standing instructions from user to Claude about how to collaborate.

## Project Structure

- `.claude/commands/spec/` - Slash commands for spec workflow
- `.claude/output-styles/` - Behavioral transformation (spec-driven mode)
- `.claude/hooks/` - Context injection hooks
- `specs/` - Generated specifications (project root, outside `.claude/`)
- `.claude/workflows/spec-create.js` - Workflow run by /spec:create and /spec:review

See @VISION.md for methodology overview.
