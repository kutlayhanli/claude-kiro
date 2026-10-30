# opencode-claude-kiro

The OpenCode port of claude-kiro: the same `/spec/*` commands, role agents, spec workflows and
guard hooks, running on [OpenCode](https://opencode.ai) instead of Claude Code.

The `ck` CLI stays the source of truth for specs, worktrees, gates and guards. This package only
adapts the harness surface:

| Claude Code | OpenCode |
|---|---|
| `/spec:plan`, `/spec:create`, `/spec:review`, `/spec:implement`, `/spec:spawn-worktree` | `/spec/plan`, `/spec/create`, `/spec/review`, `/spec/implement`, `/spec/spawn-worktree` |
| `spec:implementer`, `spec:reviewer`, … | `ck-implementer`, `ck-reviewer`, … (flat agent ids) |
| Workflow tool | a plugin tool literally named `workflow`, running the vendored workflow scripts unchanged |
| Claude hooks | an event shim that runs `ck --hook … --plugin-hook` unchanged |

## Requirements

- OpenCode **2.2.3** (the tested version; the plugin package pins `@opencode/plugin` to it)
- `node` **20+** on `PATH` (the workflow runner; it is not `process.execPath`, which is the
  `opencode` binary)
- `ck` **0.10.3+** on `PATH` (`uv tool install claude-kiro`)

## Install

```jsonc
// opencode.json
{
  "plugins": ["opencode-claude-kiro@0.10.3"]
}
```

The plugin reads OpenCode-specific settings from the `opencode` key of
`~/.config/claude-kiro/config.json` and `specs/ck.json`, and from the plugin options object
(highest precedence). See `src/config.ts` for the shape.

## Development

```sh
npm run sync-vendor   # copy ../../plugin/{workflows,skills,agents,resources,output-styles}
npm test              # node --test; no build step, sources are TypeScript
npm run typecheck
```

`vendor/` is generated and git-ignored. `npm run vendor:check` fails when the vendored copy has
drifted from `plugin/`, which is what keeps both harnesses on one source.

`src/` is loaded directly by OpenCode's Bun runtime, so there is no build output.
