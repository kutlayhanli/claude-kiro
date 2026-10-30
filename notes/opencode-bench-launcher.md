# OpenCode bench launcher: `ck bench run --harness opencode`

Status: implemented and **live-verified end to end** (demo spec, `opencode-go/beam-free`). The
launcher is `run_arm_opencode` in `src/claude_kiro/bench.py`; the args/probe assembly is
`src/claude_kiro/bench_opencode.py`; the final record imported as `merged 1, failed 0, $0.00`.

## Why not `opencode run` with a slash command

`opencode run "<text>"` sends the text to the model; it does **not** dispatch a slash command
(spike 2: `/spec/plan 7` arrived as ordinary user text).

## Two launch paths

**A. `session.command` on a dedicated server.** Start `opencode serve`, create a session in the
clone, dispatch `spec/implement` with `session.command`, wait for the record. OpenCode requires a
password for the serve API; `Service.discover()` gives `{url, basic auth}` for the background
service, and a dedicated server prints its password. **Rejected for bench:** a server started with
`opencode serve` did not activate the clone's `.opencode/plugins` (a session created with
`location.directory` still listed no commands). `scripts/arm.mjs` keeps this path for diagnostics,
but the benchmark does not depend on it.

**B. A harness probe tool (implemented).** Write a small `bench-probe.js` into the clone's
`.opencode/plugins/`. Its `bench_run` tool reads `.bench-args.json`, calls the `workflow` tool with
the recorded args, polls `<state>/runs/*/record.json` until it closes, and returns the record path.
Run `opencode run --standalone --model <arm> "First use the shell tool to run sleep 2. Then call
bench_run exactly once."`. The tool keeps the parent turn alive, so `--standalone` cannot exit and
kill the workflow.

## What the launcher does

```
ck bench run --harness opencode ...
  1. clone the repo at the arm's base, on bench/<config>-<rep> (same as the Claude arms)
  2. ck agents set <role.key> <value> --project for the arm's agents, commit the setup
  3. write specs/ck.json opencode.models[alias] = <arm model> for haiku/sonnet/opus/fable,
     so every role uses the arm's model
  4. create a private state root outside the clone:
       XDG_STATE_HOME=<bench>/opencode-state/<arm>
       CK_BENCH_STATE=<state>/claude-kiro/opencode
       CK_RUN_HEADLESS=1
       PWD=<clone>
     and a shell exec shim for a bare `opencode` command
  5. workflow_args: ck plan <spec> --json, ck agents --json, ck worktree integration <spec>,
     the brief path and the skill defaults
  6. prepare_probe: .bench-args.json, .bench-state.json,
     .opencode/plugins/claude-kiro.js (re-export the package), .opencode/plugins/bench-probe.js
  7. warmup: `opencode run --standalone ... "run sleep 30, then reply READY"`; require
     .opencode/agents/ck-worker.md before the real prompt
  8. real prompt: run sleep 2, then call bench_run once
  9. poll runs/*/record.json; when it reaches done/failed/partial, terminate the CLI's
     process group, then import bench-output.json
 10. overlay native tokens/cost, write results/<arm>.json, leave the clone for scoring
```

## Startup traps (each one cost a live run)

- **Async plugin setup races the first model turn.** OpenCode can start the model before setup has
  materialized the agents and registered the `workflow`/`bench_run` tools. The warmup prompt plus a
  shell `sleep` gives setup time; the `ck-worker.md` check is the authority, not a fixed delay.
- **Do not exclude the whole `.opencode/` directory.** `.git/info/exclude` also hides files from
  OpenCode's loader, so the probe plugin never loaded. Exclude only `.opencode/agents/`,
  `.opencode/ck/`, `AGENTS.md`, `.bench-args.json`, `.bench-state.json` and `.ck/`; the plugin files
  stay untracked but visible.
- **`subprocess(cwd=...)` does not update `PWD`.** OpenCode can use `PWD` for the project location;
  set it to the clone or setup writes nothing.
- **The CLI can outlive the record.** A model can keep talking after the probe returned. The record
  is authoritative: poll it, then terminate the session. Start the CLI in a new process group
  (`start_new_session=True`) and signal the group, or its `opencode serve --stdio` child survives.

## Why the record is authoritative

The command handler returns after it posts the rendered skill; the workflow runs in the background,
so the parent session can go idle before the record is closed. The probe blocks inside `bench_run`,
and the launcher additionally polls `runs/*/record.json` for `done`/`failed`/`partial`. Never stop at
the first idle and never trust the model's final prose as the result.

## Model refs

`parseRef` is the same splitter as `opencode/src/models.ts`: `provider/model#variant`. The launcher
passes the arm's full ref; the plugin's role agents resolve their own per-call refs. If the arm is
`inherit`, create the session with the explicit arm ref anyway; a child must never inherit a default
model.

## Guard and headless

`CK_RUN_HEADLESS=1` makes the plugin's guard treat `ask` as `deny` (a benchmark has no human), and
the `ck` hook classifies the run as a task agent. A guard denial, a post-check catch, a non-`done`
record, a failure label, a timeout or a nonzero exit marks the arm fail-closed; it is not scored as a
model result.

## Artefacts

Keep the clone, the record, the bench output and the log under the bench run directory, outside the
clone. `bench-output.json` is the `ck bench import` input; `record.json` is the native
`ck-opencode-run/1` telemetry and the source of the exact tokens/cost. Both feed the same
`ck bench score`.
