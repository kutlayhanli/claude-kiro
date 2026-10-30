# opencode-claude-kiro

The OpenCode port of claude-kiro: the same `/spec/*` commands, role agents, spec workflows and
guard hooks, running on [OpenCode](https://opencode.ai).

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

The plugin runs a preflight at startup. A missing `node`, old `ck`, incomplete `vendor/`, missing
agent file, unresolvable model, or dead guard hook fails the plugin load with a fix. A dirty git
tree is a warning: interactive work may proceed, a benchmark run should not.

The same checks are available without starting a server:

```sh
CK_OPENCODE_PLUGIN=/path/to/opencode ck doctor --harness opencode
```

## Install

```jsonc
// opencode.json
{
  "plugins": ["opencode-claude-kiro@0.10.3"]
}
```

For a local checkout, pass the package directory instead (OpenCode installs a bare name from the
npm registry, so an unpublished package must be a path):

```jsonc
{ "plugins": ["/absolute/path/to/opencode"] }
```

Setup registers the five commands, materializes six flat `.opencode/agents/ck-*.md` files, writes
two rewritten resources under `.opencode/ck/`, and installs an `AGENTS.md` block between
`<!-- ck:begin -->` and `<!-- ck:end -->`. Run it once and restart OpenCode.

## What it writes

| Path | Purpose |
|---|---|
| `.opencode/agents/ck-*.md` | role agents; digest-guarded, a user-edited file is kept |
| `.opencode/ck/without-workflow-tool.md` | the no-workflow fallback |
| `.opencode/ck/spec-implement-brief.md` | the task agents' operating brief |
| `AGENTS.md` | the spec-driven instructions, between markers |
| `<state>/runs/<runId>/record.json` | native telemetry (`ck-opencode-run/1`) |
| `<state>/runs/<runId>/bench-output.json` | `ck bench import` input |
| `<state>/runs/<runId>/run.log` | the run log |

`<state>` is `opencode.run.stateDir` or `$XDG_STATE_HOME/claude-kiro/opencode`
(`~/.local/state/...` when unset). Harness artefacts stay outside the worktree under test.

## Configuration

Settings are read from the `opencode` key of `~/.config/claude-kiro/config.json` and
`specs/ck.json`, then the plugin options object (highest precedence). Unknown keys fail loudly.

```jsonc
{
  "opencode": {
    "models": {
      "sonnet": "opencode/claude-sonnet-5-5",
      "opus": "opencode/claude-opus-5-5"
    },
    "efforts": {
      "default": { "high": "high" },
      "models": { "opencode/claude-sonnet-5-5": { "xhigh": "max" } }
    },
    "agents": {
      "default": "ck-worker",
      "rules": [{ "match": "^fix", "agent": "ck-fixer" }]
    },
    "guard": {
      "enabled": true,
      "timeouts": { "preToolGuard": 30, "postFileOps": 10, "doneVerify": 600, "stopVerify": 30 }
    },
    "run": {
      "childTimeoutMinutes": 90,
      "startTimeoutSeconds": 60,
      "pollIntervalMs": 250,
      "resultRepairAttempts": 1,
      "stateDir": null,
      "notifyCommand": null
    }
  }
}
```

- **Models:** ck aliases (`haiku`, `sonnet`, `opus`, `fable`) map to `provider/model[#variant]`.
  Opaque refs pass through. `inherit` uses the parent session's explicit model.
- **Effort:** exact variant first, then nearest higher, then nearest lower, then drop. A per-model
  override wins; the resolved variant is recorded. `inherit` sends no variant.
- **Agents:** flat ids only; nested `.opencode/agents/<dir>/` is silently ignored by OpenCode.
- **Headless:** set `CK_RUN_HEADLESS=1` for `opencode run`, the benchmark, or any unattended run.
  A guard `ask` would otherwise wait for a human exactly as OpenCode's permission prompt does.

## Guard

The guard translates OpenCode's inner tool events (`edit`, `write`, `shell`) to Claude hook JSON,
runs the real `ck --hook`, and maps the answer:

- `deny` → throw the reason verbatim; `ask` → `permission.assert` (headless degrades to deny).
- `done-verify` blocks are latches: the next call in that session fails with the original
  `[ck post-check] …` text. `runChild` waits for the background gate and refuses a result that did
  not report `guardStop`.
- `stop-verify` runs on `session.idle` for the main session and posts a synthetic warning.
- A guard failure is fatal. It never silently degrades.

`CK_GUARD_DEBUG=/path/to/log` traces the event translation, hook outputs and latches.

## Development

```sh
npm run sync-vendor   # copy ../../plugin/{workflows,skills,agents,resources,output-styles}
npm test              # node --test; sources are TypeScript, no build step
npm run typecheck
npm run vendor:check  # fail if vendor/ drifted from plugin/
```

`vendor/` is generated and git-ignored; `prepack` regenerates it. `src/` is loaded directly by
OpenCode's Bun runtime, so there is no build output.
