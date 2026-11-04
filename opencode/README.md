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
- `ck` **0.11.0+** on `PATH` (`uv tool install claude-kiro`)

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
  "plugins": ["opencode-claude-kiro@0.11.0"]
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
| `.opencode/ck/status/<runId>.json` | live workflow status for the TUI footer |
| `<state>/runs/<runId>/record.json` | native telemetry (`ck-opencode-run/1`) |
| `<state>/runs/<runId>/bench-output.json` | `ck bench import` input |
| `<state>/runs/<runId>/run.log` | the run log |

`<state>` is `opencode.run.stateDir` or `$XDG_STATE_HOME/claude-kiro/opencode`
(`~/.local/state/...` when unset). Harness artefacts stay outside the worktree under test.

Ingest a finished run into the local stats store:

```sh
ck stats record <state>/runs/<runId>/record.json
ck stats show <runId>
ck cost --run <runId>
```

## Live workflow status

The package also exports a TUI part (`./tui`). In an interactive OpenCode session it appends a live
line to the prompt footer while a workflow runs:

```text
▶ ck store-release · 12/210 · task 5 · 1 failed · 8m · 14 agents
```

It shows the newest running workflow, or a finished one for five minutes. The server writes
`.opencode/ck/status/<runId>.json` on every phase and agent transition; the TUI part polls that
directory, so no RPC or extra key is needed. Headless `opencode run` has no TUI header, and a
workflow that was already running before the package update has no status file until it restarts.

For a terminal (including the headless case), the package ships `scripts/progress.mjs`:

```sh
node /path/to/opencode/scripts/progress.mjs --project . --watch
node /path/to/opencode/scripts/progress.mjs --project . --compact
node /path/to/opencode/scripts/progress.mjs --project . --all
```

`--watch` refreshes every two seconds, `--compact` is one line for a tmux status bar, and `--all`
lists every run including finished ones. If the project has no status files, it falls back to the
native `record.json` for runs started by an older plugin or the headless probe. A globally installed
package also exposes the same script as `ck-progress`.

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

## Benchmarking

`ck bench` can run the same arms through OpenCode:

```sh
ck bench run bench-opencode.json --harness opencode
ck bench score /path/to/bench-results
```

The bench config adds an `opencode` object:

```jsonc
{
  "opencode": {
    "plugin": "/absolute/path/to/opencode", // or a published package directory
    "bin": "opencode"
  },
  "configs": [
    { "name": "deepseek-free", "model": "opencode/deepseek-v4.1-flash-free",
      "agents": { "implementer.model": "sonnet", "reviewer.model": "sonnet" } }
  ]
}
```

Each role uses a ck alias; the runner writes `opencode.models.<alias> = <arm model>` into the clone,
so all roles are pinned to the arm for the first pass. It launches the workflow through a probe with
`CK_RUN_HEADLESS=1`, uses a private state directory outside the clone, imports
`bench-output.json` and overlays the native tokens/cost. A guard denial, a non-`done` record, a
timeout or a nonzero exit is fail-closed. See `notes/opencode-experiment-config.md` for the E0/E1/E2
template.

## Development

```sh
npm run sync-vendor   # copy ../../plugin/{workflows,skills,agents,resources,output-styles}
npm test              # node --test; sources are TypeScript, no build step
npm run typecheck
npm run vendor:check  # fail if vendor/ drifted from plugin/
```

`vendor/` is generated and git-ignored; `prepack` regenerates it. `src/` is loaded directly by
OpenCode's Bun runtime, so there is no build output.
