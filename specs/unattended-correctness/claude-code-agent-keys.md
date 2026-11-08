# Claude Code sub-agent frontmatter keys (Task 66)

Checked for the lean `runner` agent (`plugin/agents/runner.md`, generated copy `.claude/agents/ck-runner.md`).

Source checked: Claude Code sub-agent documentation (the "subagents" page of the Claude Code docs, frontmatter field reference). This sandbox has no web access, so the page was not re-fetched during this task; the answers below are from the field reference as known at implementation time and should be re-verified against the live page.

| Key | Used | Documented | Source |
|---|---|---|---|
| `name`, `description` | yes | yes | Claude Code sub-agent docs |
| `tools` | yes (`Bash`) | yes | Claude Code sub-agent docs |
| `model` | yes (`haiku`) | yes | Claude Code sub-agent docs |
| `effort` | yes (`low`) | yes | Claude Code sub-agent docs |
| `maxTurns` | yes (`5`) | yes | Claude Code sub-agent docs |

## CLAUDE.md key: gap

No documented key omits CLAUDE.md from a sub-agent's context. The `omitClaudeMd` key named in the design is not in the sub-agent frontmatter reference, so it is left out of the runner file.

Source checked: the same Claude Code sub-agent docs frontmatter reference (no documented key for this).

Consequence: the runner's CLAUDE.md part of 19.2 is not available. 19.2 asks for it only where Claude Code supports it (PLAN D19, C1.10). This gap is reported to the orchestrator.
