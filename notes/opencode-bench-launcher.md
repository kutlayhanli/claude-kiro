# OpenCode bench launcher: design for `ck bench run --harness opencode`

Status: design only. Phase 4.1 (`bench-output.json` + `ck bench import`) already works; this is the
controllable launcher/collector for Phase 4.2.

## Why not `opencode run` with a slash command

`opencode run "<text>"` sends the text to the model; it does **not** dispatch a slash command
(spike 2: `/spec/plan 7` arrived as ordinary user text).

## Two launch paths

**A. `session.command` on a dedicated server.** The original design: start `opencode serve`, create a
session in the clone, dispatch `spec/implement` with `session.command`, wait for the record. OpenCode
requires a password for the serve API; `Service.discover()` gives `{url, basic auth}` for the
background service, and a dedicated server prints its password. **Open problem:** a server started
with `opencode serve` did not activate the clone's `.opencode/plugins` (a session created with
`location.directory` still listed no commands). The TUI/`opencode run` path activates the project, so
this needs either a location-activation call or a session started by the CLI. Do not rely on it
until that is proven.

**B. A harness probe tool (recommended, proven).** Copy a small `bench-probe.js` into the clone's
`.opencode/plugins/` (and add it to `.git/info/exclude`). Its tool reads `.bench-args.json`, calls the
`workflow` tool with the recorded args, polls `<state>/runs/*/record.json` until it closes, and returns
the record path. Run `opencode run --standalone --model <arm> "Call bench_run now, exactly once."`.
The tool keeps the parent turn alive, so `--standalone` cannot exit and kill the workflow. This is the
same path the Phase 1/2 live tests used. The slash-command surface is tested separately
(`session.command` with `/spec/plan 7` in spike 2 and the live command list).

Whichever path is used, the launcher still has to assemble the workflow args. That is the skill's
setup: `ck plan <spec> --json`, `ck agents --json`, the worktree/integration choices, the brief path,
risk/complexity and the run limits. Assemble them in Python once, write `.bench-args.json`, and pass
exactly the object the skill would have passed.

## Shape

```
ck bench run --harness opencode ...
  1. existing bench clone (same base commit, same spec layout as the Claude arms)
  2. write <clone>/opencode.json:
       {"plugins": ["<absolute path to opencode/>"]}
     (OpenCode installs a bare package name from npm; an unpublished package is a path)
  3. add .opencode/ and AGENTS.md to <clone>/.git/info/exclude, so setup-generated files do not
     dirty the tree or leak into the gate
  4. start: opencode serve --standalone --port <free port>
       cwd=<clone>
       env: CK_RUN_HEADLESS=1
            XDG_STATE_HOME=<bench state>/opencode   (records outside the clone)
            PATH=...                                 (node, ck)
  5. wait for the server's /server/info
  6. node opencode/scripts/arm.mjs --url ... --dir <clone> --model <provider/model#variant>
       --command spec/implement --text <spec> --state <bench state>/opencode
  7. read the new <state>/runs/<runId>/record.json (native ingest) and/or bench-output.json
  8. ck analytics record <record.json>  (or ck bench import <bench-output.json>)
  9. kill the server; keep the clone until scoring, then remove it
```

## `arm.mjs`

Use `@opencode/client` from the package's dependency:

```js
const client = OpenCode.make({ baseUrl: url })
const session = await client.session.create({
  title: `ck bench ${spec} ${arm}`,
  model: parseRef(model),             // {providerID, id, variant?}
  location: { directory: dir },
})
await client.session.command({ sessionID: session.id, name: "spec/implement", text: spec })
```

Then poll:
- `client.session.get({sessionID})` for the parent's `outcome`/`time.busy`/`time.idle`;
- `<state>/runs/*/record.json` for the workflow run. The record is authoritative: wait until its
  `status` is `done` or `failed` (or the wall-clock cap). Print
  `{sessionID, runId, recordPath, benchPath, status}` as one JSON object.

The command handler returns after it posts the rendered skill to the session; the workflow runs in
the background, so the parent session can go idle before the record is closed. Never stop at the
first idle: wait for the record (D1's busy→idle trap in a different form).

## Model ref

`parseRef` is the same splitter as `opencode/src/models.ts`: `provider/model#variant`. The launcher
passes the arm's full ref; the plugin's role agents resolve their own per-call refs. If the arm is
`inherit`, create the session with the explicit arm ref anyway; a child must never inherit a default
model.

## Guard and headless

`CK_RUN_HEADLESS=1` makes the plugin's guard treat `ask` as `deny` (a benchmark has no human), and
the `ck` hook classifies the run as a task agent. A run that loses the guard must be marked failed,
not scored as a model result.

## Artefacts

Keep the clone, the record, the bench output and the log under the bench run directory, outside the
clone. The benchmark's result record should use the native `ck-opencode-run/1` path (the telemetry
track) or `bench import` for the first pass; both share `ck bench score`.
