# OpenCode port: implementation plan

Companion to `notes/opencode-port-decisions.md` (D1–D13, spike results). Based on claude-kiro
**0.10.3** (`3cdc751`) and OpenCode **2.2.3** plugin API (Bun 1.4.4 / node 26.3.0).

Every claim below that starts with "verified" comes from a probe in
`/tmp/opencode/spike/.opencode/plugins/probe*.js` with logs in `/tmp/opencode/spike-logs/`.

---

## 0. Shape of the deliverable

One new npm package in this repo, `opencode/`, published as `opencode-claude-kiro`, with the
`ck` CLI staying an external dependency.

```
opencode/
├── package.json                   name, version (mirrors ck), "type": "module"
├── tsconfig.json
├── src/
│   ├── index.ts                   package entry: Plugin.define({id:"claude-kiro", setup})
│   ├── config.ts                  read/validate ck config + claude-kiro section
│   ├── preflight.ts               D12 checks, shared by setup and /spec/implement
│   ├── commands.ts                SKILL.md → runtime command registration (D8, D10)
│   ├── agents.ts                  plugin/agents/*.md → .opencode/agents/ck-*.md (D3, D10)
│   ├── instructions.ts            output-style → AGENTS.md managed block (D10d)
│   ├── workflow-tool.ts           the `workflow` tool: spawn + IPC (D1)
│   ├── runner.mjs                 the child entry: runtime globals, module load (D1)
│   ├── run-child.ts               session lifecycle, completion, result capture (D1, D2)
│   ├── result-tool.ts             submit_result_<seq> registration + validator (D2)
│   ├── schema.ts                  the ~60-line JSON-schema validator
│   ├── models.ts                  ck alias ⇄ provider/model#variant, effort mapping (D4, D5)
│   ├── guard.ts                   tool-event translation, deny/ask, latches (D9)
│   ├── record.ts                  run record emitter (D7)
│   └── log.ts                     structured log outside the worktree
├── vendor/                        copied from ../../plugin at build time
│   ├── workflows/*.js
│   ├── skills/*/SKILL.md
│   ├── agents/*.md
│   ├── resources/*.md
│   └── output-styles/*.md
├── scripts/sync-vendor.ts         plugin/ → vendor/, with a digest check
└── test/                          node --test + captured fixtures
```

Python side (in `src/claude_kiro/`): `ck analytics record`, the `_build_record` refactor, the
`opencode` pricing section, `ck doctor --harness opencode`, and the bench collector later.

**Why a separate package and not `ck init`:** D10 chose the npm package. `ck` keeps caring only
about specs and worktrees; it must not grow an installer for another harness. The package vendors
`plugin/` at build time so `plugin/` stays the single source for both harnesses (same philosophy as
`VENDOR_MAP`).

---

## Phase 0 — Spikes and fixtures (done)

| Task | State |
|---|---|
| Runtime reach, command registration, hook shape, deny delivery, latch | done |
| Per-child model/variant, prompt+wait, JIT tool, read-only agent, permission ask | done |
| Event stream (`execution.succeeded/failed`), token source | done |
| Decisions amended for findings 12–20 | done |

Exit artefact: `opencode/test/fixtures/tool-events.json` with the **captured** payloads from the
probes (`read`, `edit`, `shell`, inner/outer `execute`, deny, latch), so the translation table has
real inputs and not remembered ones. This is Phase 1 work, listed here because it is the only
deliverable the spikes leave behind.

---

## Phase 1 — Workflow bridge, child runs, structured output (D1, D2, D4, D5)

The gate for everything else: if `agent()` cannot run a child reliably, nothing downstream matters.

### 1.1 The `workflow` tool (D1)

- Input schema (JSON Schema, no dependencies):
  ```
  { name?: string, scriptPath?: string, script?: string, args?: object }
  ```
  `name` is mapped by the plugin: `spec:spec-create` → `vendor/workflows/spec-create.js`,
  `spec:spec-implement` → `vendor/workflows/spec-implement.js`. `scriptPath` is honoured as-is.
- `execute()` validates the target, spawns the runner, returns **immediately** with
  `{runId, workflow, logPath}`. Background work continues in the server process.
- The runner is `node <runner.mjs> <workflow.js> <argsJson>` with
  `stdio: ["ignore", "pipe", "pipe", "ipc"]`, `env: {...process.env, CK_RUN_HEADLESS: <inherited>}`,
  and `cwd` = the session's project directory. **Never `process.execPath`** (verified: it is the
  `opencode` binary). The binary is `node` from `PATH` (v22.22.3 here; `bun` is not on `PATH`), and
  `spawn` + stdio `"ipc"` + `child.send` was verified working from inside the Bun-hosted plugin
  (spike 21). Preflight requires `node --version >= 20`.
- Runner compiles the workflow **as a function body**, not a module: the scripts start with
  `export const meta` and end with a top-level `return`, so `import()` is impossible. It strips the
  `meta` declaration (keeping the line count so stack traces still line up), evaluates the literal,
  then runs `new AsyncFunction("args","agent","phase","parallel","pipeline","log", body)`. Inline
  `script` bodies take the same path; they simply have no `meta`.
- IPC protocol, one JSON object per line, both directions:
  - child → parent: `{t:"agent", id, prompt, opts}` · `{t:"phase", ...}` · `{t:"log", text}` ·
    `{t:"done", result}` · `{t:"error", message, stack}`
  - parent → child: `{t:"result", id, value|error}` · `{t:"cancel", id}`
- Parent on `phase`/`log`: append to the run log and post a transcript-only synthetic message
  (`ctx.session.synthetic({sessionID, text})`, no delivery). It is visible in the transcript but does
  not start a model turn; phase-level granularity only.
- Parent on `done`: post the final result with `ctx.session.prompt({delivery:"queue"})` so the parent
  wakes once, then close the record (D7) and notify if `run.notify_command` is configured. Failures
  wake the session the same way.
- Cancellation: `session.interrupt` on the parent, or the run's wall-clock cap, sends `cancel` and
  kills the child tree.

### 1.2 `runChild()` — one `agent()` call (D1, D2, D4, D5)

```
runChild({prompt, label, phase, model, effort, schema})
  1. resolve model  → {id, providerID, variant}        (models.ts, D4/D5)
  2. map agent      → ck-worker | ck-reviewer …         (D3)
  3. if schema: register submit_result_<seq>            (D2)
  4. session.create({model, agent, title: label})
  5. prompt(delivery: "queue") + observe completion     (see below)
  6. result = captured tool input | parsed JSON | null  (D2)
  7. dispose the tool; record tokens/cost/time          (D7)
```

- **Completion observation** (the spike-16 trap): after `prompt`, poll `session.get()` every 250 ms
  until `time.busy` is observed, then until `time.idle >= time.busy`; cap at
  `run.child_timeout_minutes` (default 90). `session.wait()` is an accelerator only, never the
  authority. Cross-check `session.execution.failed` for the error text and the last assistant
  message's `finish`.
- **Guard failure is fatal** (D12): if the session's run recorded a guard deny that the workflow did
  not turn into a `guardStop`, `runChild` throws — the guard ladder must be the only way past a stop.
- **Result capture** (D2): the `submit_result_<seq>` executor stores `{input}`; on invalid input it
  returns field-level errors as tool content and lets the model correct once; a second failure
  prompts one more time in-child, then one whole-child retry, then throws. If no tool call arrived,
  parse the last assistant text as JSON and validate. Never return `{error: …}`.
- **Model resolution** (D4/D5): `ck` aliases (`haiku|sonnet|opus|fable|inherit`) map through the
  config table to a `provider/model`, with `#variant` from the effort mapping ("nearest, prefer up").
  Opaque `provider/model` refs pass through. The **guard ladder** passes literal `sonnet`/`opus`/
  `fable` (verified in 0.10.3's `spec-implement.js`), so the alias→ref direction is mandatory, not
  optional. Record `requested` and `resolved` on every agent row.
- **Agent mapping** (D3): `review*`/`refute*`/`cross-test*` → `ck-reviewer`, else `ck-worker`;
  configurable. Unknown agent ids are accepted silently by OpenCode (spike 13), so the preflight is
  the only defence — `runChild` also asserts the file exists once per run.

### 1.3 Validator (D2)

Hand-rolled, ~60 lines, only the keywords the two workflows use: `type`, `properties`, `required`,
`items`, `enum`, `additionalProperties`, `description`. Returns `[{path, message}]`. Unit-tested
against every schema literal in `spec-implement.js` and `spec-create.js` (extract at test time so a
future workflow schema change fails loudly).

### 1.4 Acceptance

- [x] A trivial 2-phase workflow with three `agent()` calls runs end to end and returns its result.
      Live on 2026-10-30 with `opencode-go/beam-free`: `ALPHA`, `BETA`, `{ok:true}`, in 4.5 s.
- [x] `spec-implement.js` and `spec-create.js` are vendored **unmodified** (`sync-vendor --check`
      compares source and copy digests). Executing both real workflows is still pending E0.
- [x] A step with a schema gets a validated object; a deliberately wrong submission gets field errors
      and then succeeds; a never-submitting child fails the step with a clear error. (Live structured
      step plus the fake-session rejection/retry test.)
- [x] A child whose model is missing/invalid fails at resolution, not 20 minutes in.
- [ ] Guard-denied child halts the step; `[ck guard]` text is in the error. (Phase 3.)
- [ ] `deepseek-v4.1-flash-free` completes a 1-task workflow (smoke). Blocked: `provider.free-limit`
      429 until ~01:00Z; `claude-sonnet-5-5` returns `provider.quota` 402 (account unfunded).
- [x] Tokens, cost and wall clock per child appear in the run log.

### 1.5 Calibration gate

`/spec/implement` on one small spec with `opencode/claude-sonnet-5-5` (E0). **If this fails, stop
and fix the port** — every cheap-model number below it would be measuring the port, not the model.

Attempted on 2026-10-30. Sonnet failed before the first model step with `provider.quota` / HTTP 402
"Insufficient account funds"; `opencode/deepseek-v4.1-flash-free` failed with `provider.free-limit` /
HTTP 429, `retryAfterMs` ~19.7 M. The bridge itself was then proven live on `opencode-go/beam-free`.
E0 remains **blocked on quota/funding**, not on the port, and must be rerun before any benchmark
number is trusted.

A real `spec-create.js` run on `opencode-go/beam-free` reached `design.md` (requirements 18 s,
design 172 s, both structured results accepted) and was stopped at `test-plan.md` after the 10-minute
outer timeout. The record correctly captured the two successes and the killed child as a
`session_error` with `failureLabel: "port"`. The workflow source is bridge-compatible; this model is
simply too slow for a whole spec.

---

## Phase 2 — Commands, agents, instructions, fallback (D3, D8, D10, D11)

### 2.1 Commands

- Read `vendor/skills/*/SKILL.md`, parse frontmatter, and register each as
  `ctx.command.transform(editor.add({name: "spec/" + name, description, execute}))`.
- `description`: `argument-hint` folded in, e.g.
  `Implement a spec task or whole spec — args: [spec-name | task-number] [--model M] …`.
- `execute({sessionID, prompt, delivery})`: render the body with
  - `$ARGUMENTS` ← `prompt.text` (verified: the raw argument string),
  - `${CLAUDE_PLUGIN_ROOT}` ← the package root,
  - `/spec:x` → `/spec/x`, `AskUserQuestion`→`question`, `Agent`→`subagent`,
    `TodoWrite`→todo tool, `SendMessage`→`subagent` + returned `sessionID`,
    `spec:reviewer|implementer|test-writer|fixer|resolver` → `ck-reviewer|ck-implementer|…` (D3),
    and the "pick the script by the project's mode" paragraph collapses to
    "use `name: "spec:spec-create"`" (there is no `.claude/ck-manifest.json` on OpenCode, so the
    vendor branch can only confuse the model),
  then `ctx.session.prompt({sessionID, text, delivery})`.
- No `agent:` frontmatter (D8) — the session keeps the user's model, which is what makes the D4
  checks real.

### 2.2 Agents

- Emit **flat** `.opencode/agents/ck-<name>.md` (D3; nested dirs are silently ignored).
  - Frontmatter: `description`, `mode: subagent`, `permissions` (from the source agent's `tools:`),
    **no `model:`**.
  - Reviewer (`tools: Read, Grep, Glob, Bash`) → `edit`/`write`/`patch` deny; the model has no edit
    tool at all (verified).
  - Body = the source body verbatim.
- Digest-guarded write: skip if the file exists and is unmodified; refuse to overwrite a modified
  file and say which.
- Names: `ck-worker`, `ck-implementer`, `ck-test-writer`, `ck-reviewer`, `ck-fixer`, `ck-resolver`.

### 2.3 `AGENTS.md`

Managed block (`<!-- ck:begin -->` … `<!-- ck:end -->`) generated from
`vendor/output-styles/spec-driven.md`. Create if missing, replace only between markers, keep
everything else.

### 2.4 Fallback

`--no-workflow` and "workflow tool unavailable" route to `vendor/resources/without-workflow-tool.md`
with the agent-type names already rewritten by 2.1. Never switch silently (D12f).

### 2.5 Acceptance

- [x] `/spec/plan`, `/spec/create`, `/spec/review`, `/spec/implement`, `/spec/spawn-worktree` appear
      and dispatch with correct `$ARGUMENTS`. Live: `command.list` returned all five.
- [x] A second install run changes nothing (digest match), and an edited agent file is preserved.
- [x] `ck-reviewer` has no edit/write tools; `ck-worker` does. Live: the reviewer reported no
      `edit` tool and still ran `echo SHELL_OK`; the worker wrote `WORKER_WROTE`.
- [x] `AGENTS.md` keeps user content outside the markers.
- [x] Fallback resources are materialized and rewritten under `.opencode/ck/`; the model-driven
      launch itself is not exercised yet.

---

## Phase 3 — Guard, hooks, telemetry, preflight (D7, D9, D12)

Order matters: telemetry's `interruptions` block comes from the guard log the hooks write, so the
hooks land first.

### 3.1 Guard shim (D9)

**Status:** implemented in `opencode/src/guard.ts`; unit tests with a fake `ck`.
Live verified: a real `ck` pre-tool guard denied a test weakening with the verbatim
`[ck guard] Denied: this edit removes 1 assertion(s)…` text and the file stayed untouched.
Live verified: a sequential child session that ran `sed -i` got the post-check block on its next
shell call (`before latch fired` in the debug trace). The earlier miss was a parallel-tool-call
artifact: the model issued `echo NEXT` in the same step as `sed`, before the post-check could latch.
`runChild` now awaits the background done-verify (`awaitPendingGuard`) and throws a `guard` error if
the child hit the guard without reporting `guardStop`.

- Register `execute.before` / `execute.after`.
- **Ignore `tool === "execute"`** (code mode): every call raises an outer event with `{code}` and an
  inner event with the real tool + structured input (verified). Act only on the inner one.
- Translate and run `ck --hook <name> --plugin-hook` with
  `{cwd, tool_name, tool_input, session_id, agent_id, agent_type, tool_use_id}` on stdin.
- Map the answer: `deny` → throw the reason **verbatim** (the model sees it inside the code-mode
  error object, verified); `ask` → `ctx.permission.assert`, except on a headless run where it
  degrades to deny with the same reason (verified: `assert` blocks forever without a human).
- `execute.after`: `done-verify` / `stop-verify` returning `decision: block` become a **latch** keyed
  by session; the next `execute.before` throws the original text (verified end to end).
- Timeouts: `pre-tool-guard` 30 s inline; `done-verify` 600 s runs in the background and delivers via
  the latch, so a slow gate never blocks a tool call.
- Self-test at setup: run one synthetic deny through the same path and assert the text survives;
  fail the plugin load if not (D12).

### 3.2 Run record (D7)

**Status:** done. `opencode/src/record.ts` writes `<state>/runs/<runId>/record.json` at spawn and
closes it on done/error; live-verified with three agents. The Python ingester is
`ck stats record <file>` (`analytics/opencode_record.py` + the `_RecordSource` refactor); it
shares pricing/aggregation with the transcript path. Live-ingested the three-agent record:
`harness=opencode`, `ckVersion=0.10.3`, `ck cost --run` = **$0.00** for `opencode-go/beam-free`.

- `record.ts` writes `<state>/runs/<runId>/record.json` (outside the worktree) at spawn as `partial`,
  and closes it with the workflow result.
- Fields: label, phase, role, agent, `model`, `requestedEffort`, `resolvedEffort`, `variant`,
  `startedAt`/`endedAt`, tokens `{input, output, reasoning, cache{read, write}}`, cost, status,
  guard events, retries, throttles, peak context (max `input + cache.read` per step).
- Python side: `ck analytics record <file>` builds `ck-run-local/1` through the refactored
  `_build_record`; pricing gains an `opencode` section with explicit `free: true`; the new fields are
  added additively to `SHAREABLE_SCHEMA` + `analytics/SCHEMA.md` (D7 amendment).
- `harness: "opencode"`, `claudeCodeVersion: null`, `collectorVersion` = plugin version.

### 3.3 Preflight / `ck doctor --harness opencode` (D12)

**Status:** done. `opencode/src/preflight.ts` runs at startup after agent materialization and is
exposed as `ck doctor --harness opencode` (via `scripts/preflight.mjs`). Live: the startup preflight
passed with a dirty-tree warning; the CLI command reported the same result.

Checks, in order, each with an actionable message: `node --version >= 20` on PATH (the runner binary;
`bun` was not installed in testing) · `ck --version` compatible · plugin loaded and the
`workflow` tool registered · every configured `provider/model#variant` resolvable (compare against
`opencode models`) · agent **files** present and parseable (the agent API cannot see them, spike 13) ·
hooks armed (self-test deny) · target branch clean · D11 out-of-scope surfaces listed as
informational. Runs automatically at the start of `/spec/implement`.

### 3.4 Acceptance

- [x] A denied edit leaves the file untouched and the model quotes `[ck guard] …`. Live with the
      real `ck` hook.
- [x] A post-check block surfaces on the next call as `[ck post-check] …`. Live: the latch fired on
      the child's next shell call; `runChild` waits for the background gate and refuses a result that
      did not report `guardStop`.
- [x] `interruptions.guard_denials` > 0 in `ck stats` for a run with a deny. **Live:** an inline
      workflow's task agent was denied by the real `ck` guard, reported `guardStop`, the record
      carried `guard.denials=1`, and `ck stats show` printed "1 guard denials". `byRole` is empty
      because the native record has per-agent guard events, not role-level totals.
- [x] `ck cost --run <runId>` prices a free-tier run at $0.00, not "unknown". Live-ingested record.
- [x] A missing agent file, a bad model ref and a dead hook each abort the preflight with a message
      that names the fix. Preflight tests cover each.
- [ ] A run that loses the guard halts (test by removing `ck` from `PATH` mid-run).

---

## Phase 4 — Bench and experiments (D6, D13)

### 4.1 Step 1 (import path)

- `workflow-tool.ts` writes `bench-output.json`: `{result, status, durationMs, workflowProgress:[{
  type:"workflow_agent", agentId, label, model, state, durationMs, tokens}]}` — the shape
  `ck bench import` accepts. **Live-verified:** the generated file imported into
  `smoke--beam-free--r1.json` with all three agents, wall time and context-token estimates.
- `opencode/scripts/arm.mjs` starts one run against an OpenCode service and waits for the record.
  It can `--check` the registered commands, and dispatches `session.command` (with discovery/auth)
  for the launcher path.
- `ck bench import` + `ck bench score` unchanged.

### 4.2 Step 2 (launcher + collector, after the first results)

- `ck bench run --harness opencode`: launcher in `bench.py` (~line 635) that starts an
  OpenCode session instead of `ck run`, and a collector that reads the `ck-run-local` record by run
  id. Both collectors behind one interface.
- **Launch caveat:** a bare `opencode serve` did not activate the clone's project plugins (a session
  with `location.directory` still listed no commands). The proven path is the harness-probe tool:
  copy a small `bench-probe.js` into the clone, have it call the `workflow` tool and poll the record,
  and run it through `opencode run --standalone` so the turn stays alive. The launcher still has to
  assemble the `spec-implement` args from `ck plan`/`ck agents`; see
  `notes/opencode-bench-launcher.md`.

### 4.3 Experiments

| # | Arm | Purpose |
|---|---|---|
| E0 | `opencode/claude-sonnet-5-5` | calibration; gates everything |
| E1 | `opencode/deepseek-v4.1-flash-free` | free tier A (200K/32K, weak Terminal-Bench) |
| E2 | `opencode-go/muse-spark-1.3-contributor` (confirm the free id) | free tier B (1M/131K) |
| baseline | Opus 5.5 / Sonnet 5.5 | from `ck bench` on Claude Code |

3 reps per spec, same base commit, fresh worktree, pass@1 + pass^3 + wall clock + tokens + $ +
escalations + throttles + peak context, every failure labelled `model|port|throttle|spec|env`.

---

## Cross-cutting risks

| Risk | Mitigation |
|---|---|
| Code mode changes call shapes again in a future OpenCode release | translation table is data, fixtures are captured payloads, unknown `tool` is **fatal** (D12e) not ignored |
| A model calls tools outside the translated set (`patch`, `apply_patch`, MCP tools) | preflight enumerates `ctx.tool.list()` and refuses to run if an unguarded write path exists |
| `session.wait` races (spike 16) | busy→idle observation is the authority, never `wait` |
| 200K context on free tiers | peak context recorded; compaction marks the run degraded (D12d) |
| Agent ids silently accepted (spike 13) | file-existence assertion in preflight and in `runChild` |
| A model writes to harness artefacts (spike 20) | logs and records outside the worktree; records never read back from agent-writable paths |
| OpenCode updates the plugin API under us | pin `@opencode/plugin` and the tested OpenCode version in `package.json`/docs; preflight checks the runtime version |

## Out of scope (D11)

`ck run` (headless/sandboxed), agent-mode analytics, skills auto-invocation, TUI panel,
`ck handoff` (self-disables; returns `interactive: true`).

## First commit

`opencode/` skeleton + `sync-vendor.ts` + the captured fixtures + `schema.ts` + `models.ts` with
tests. No plugin behaviour yet, so the first reviewable diff is pure data and pure functions.
