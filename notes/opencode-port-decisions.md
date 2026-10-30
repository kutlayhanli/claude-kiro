# OpenCode port: decision register (2026-10-30)

Goal: run claude-kiro (`ck` + the `spec` plugin) in OpenCode v2.2.3 with the same commands,
prompts and orchestration, then benchmark it against Claude Code on supplied specs.

Based on **claude-kiro 0.10.3** (`3cdc751`, "prompt-free runs"). D1–D8 were agreed against 0.10.2 and
re-checked against 0.10.3; see "What 0.10.3 changed" at the end.

Decisions are locked in order. Anything not listed under "Locked" is still open.

## Locked

### D1 — Orchestration substrate: workflow-compatible bridge

- The plugin registers a tool literally named `workflow`, so `Call the Workflow tool with name:
  "spec:spec-create"` in the existing skill text needs no edits.
- The tool runs the existing `plugin/workflows/*.js` **verbatim** in a child `node` process with a
  runtime mapping `agent(prompt, {label, phase, model, effort, schema})`, `phase()`,
  `parallel()`, `pipeline()`, `log()`, `args`. No workflow edits.
- **The scripts are function bodies, not ES modules.** Each starts with `export const meta = {…}` and
  ends with a top-level `return {…}`; `import()` would reject both. The runner removes the `meta`
  declaration (preserving line numbers), evaluates it, and compiles the rest with
  `new AsyncFunction("args", "agent", "phase", "parallel", "pipeline", "log", body)`. Inline `script`
  bodies take the same path (they simply have no `meta`).
- Execution is **background**, like Claude Code: the tool returns a run id immediately and the parent
  session is not frozen (spawn-worktree launches several runs without waiting). Progress is a
  transcript-only `ctx.session.synthetic({sessionID, text})` message (no delivery, no model turn);
  completion and failure use `ctx.session.prompt({delivery:"queue"})` so the parent wakes exactly once
  to report the result. Verified live: two phase messages caused zero extra turns, the final one caused
  one.
- **`agent()` completion is observed, not awaited.** `session.prompt` only enqueues; `session.wait` can
  resolve before the run starts (verified: 3 ms with the child idle). `runChild` therefore watches
  `session.get().time.busy` appear and then `time.idle ≥ busy`, cross-checks
  `session.execution.succeeded` / `execution.failed`, and caps the whole thing with a wall clock.
- A child session **must** be created with an explicit `model` ref (`{id, providerID, variant?}`); an
  inherited/default model fails the run with 0 tokens (spike 17).
- Verified the scripts use no `isolation`, `budget`, `resumeFromRunId`, or anything outside `args`,
  the five helpers, `meta` and the JS stdlib.

### D2 — Structured output: per-call `submit_result_<seq>` tool

- Each `agent(..., {schema})` call registers a tool whose **input schema is the step's schema**, named
  uniquely per call, visible to the child's next model step, and disposed afterwards. Concurrent
  `parallel()` calls cannot collide.
- The executor validates; on failure the tool returns field-level errors so the model self-corrects.
  Repair budget: **one in-child rejection round + one whole-child retry**, then `throw`.
- Tolerant text-JSON parse is a fallback when no tool call arrives; final failure throws (never a
  `{error}` sentinel) so the workflow's own `try/catch` or the run fails visibly.
- Validator is hand-rolled (~60 lines) covering only what the schemas use: `type`, `properties`,
  `items`, `required`, `enum`, `additionalProperties`, `description` (no Ajv, no plugin dependencies).
- The schema's `description` strings are instructions the model sees, so they are passed verbatim.
- Verified (spike 7): a tool registered by `ctx.tool.transform` mid-call is immediately callable by a
  child created afterwards, its executor receives the input plus `{id, sessionID, agent}`, and
  `registration.dispose()` removes it again. That is the whole `submit_result_<seq>` lifecycle.

### D3 — Child agent identity: configurable hybrid

- Default map: `review*`, `refute*`, `cross-test*` labels → read-only `ck-reviewer`; everything else
  → generic `ck-worker` with full tools.
- **Agent ids are flat** (`ck-worker`, `ck-reviewer`, `ck-implementer`, `ck-fixer`, `ck-resolver`):
  OpenCode does not discover `.opencode/agents/<dir>/<name>.md`, only `.opencode/agents/<name>.md`
  (spike 12). Slashes work for commands, not agents.
- The mapping is **config, not code**, so strict Claude parity is one line away.
- Role agents carry **no `model:`** in their frontmatter; `ck agents` plus per-call opts stay the single
  source of truth.
- Read-only is enforced by the agent file's `permissions:` block (`edit`/`write`/`patch` → `deny`), which
  was verified to remove the tools entirely rather than merely refuse them (spike 8). **`shell` stays
  allowed**: the source reviewer has `Bash`, and its prompt runs `git diff` and `ck lint --contracts`.
  Denying shell would break the review. The Phase 3 guard is the shell-level control.
- Children do not inherit the parent's agent or permissions; they get the mapped agent's explicit set.
- OpenCode subagent nesting stays at its default depth of 1 (a known, recorded difference).

### D4 — Models

- ck keeps its aliases (`haiku`, `sonnet`, `opus`, `fable`, `inherit`) and the Claude Code path is
  untouched. The OpenCode side gets a `provider/model#variant` mapping layer in `specs/ck.json`.
- ck must accept opaque IDs containing `/` (D4b) so non-Anthropic models are usable at all, and rank
  them as unrankable; `ck agents check --model` fails open on an unrankable ID (D4c).
- `inherit` maps to "set no model on the child".

Model facts that drove the ladder discussion (models.dev, 2026-10-30):

| Ref | Context | Max output | $/1M in-out | Variants |
|---|---|---|---|---|
| `opencode/deepseek-v4.1-flash-free` | 200,000 | 32,000 | 0 / 0 | low, high, max |
| `opencode/deepseek-v4.1-flash` | 1,000,000 | 384,000 | 0.30 / 1.20 | low, high, max |
| `opencode-go/deepseek-v4.1-flash` | 1,000,000 | 384,000 | 0.15 / 0.60 | low, high, max |
| `opencode/muse-spark-1.3-contributor-free` | 1,048,576 | 131,072 | 0 / 0 | see catalog |
| `opencode-go/muse-spark-1.3-contributor` | 1,048,576 | 131,072 | 0.10 / 0.20 | see catalog |

`opencode models` on the same day listed `opencode/muse-spark-1.3` and
`opencode-go/muse-spark-1.3-contributor`; the `-contributor-free` id was **not** present. Confirm the
E2 id and its price before running that arm.

AA Coding Agent Index v2.0 (equal weight FrontierCode v1.1, Terminal-Bench 4.0, SWE-Atlas-QnA-AA):
Opus 5.5 61.7 ($11.42/task), Sonnet 5.5 59.3, GPT-6 Astra 54.7, GPT-6.1 Sol 54.1 ($1.50),
GLM 5.3 48.9 (measured in OpenCode), Mistral Large 4 48.3, Terra 48.2,
**DeepSeek V4.1 Flash 47.3** (FC 52.6, TB 28.3, Q&A 61.0, $0.39), Grok 4.7 46.6,
Kimi K3 41.4, Haiku 5.5 37.3, Gemini 3.8 Flash 33.3, GPT-6 Luna 31.1, Beam 1.0 20.5.
Muse Spark 1.3 (Muse Code) 45.7 — FC 46.7, TB 31.8; co-trained with its harness, so read as an upper bound.

Two consequences to keep in view:

- DeepSeek's weak axis is Terminal-Bench, which is exactly claude-kiro's shell-heavy surface
  (`ck worktree`, `ck plan/land/gate`, git). Failures there will look like red gates, not bad code.
- The free tier is 5× less context and 12× less output than the paid deployment of the same model.
  Log peak context per child; a mid-task compaction silently breaks parity.

### D5 — Effort/variant mapping: nearest, prefer up

- ck's ladder (`low, medium, high, xhigh, max`) resolves to a variant as: exact → nearest **higher**
  → nearest lower → drop the variant. Never error (an unknown variant is a hard model-resolution
  error in OpenCode, so failing open is mandatory).
- Resolution happens in the bridge at child spawn, after the workflow has done its own
  `EFFORT_RANK` comparisons in ck space.
- Per-model override table in `specs/ck.json`:
  `"opencode": { "effort": { "<provider/model>": { "medium": "high", "xhigh": "max" } } }`.
- `inherit` sends no variant. Models with no variants (`mimo-v2.6-flash-free`) simply drop effort.
- Run records store **both** `requested` and `resolved` effort.
- With the current pin, implementer/fixer effectively run at `high`, not `medium`; that is a recorded
  run condition, not a silent substitution.
- Rejected for now: synthesizing `medium`/`xhigh` variants in config (an unsupported `reasoningEffort`
  would fail every agent at once). Revisit only after a probe shows the provider accepts it.

### D6 — Success criteria and run count

- **Success** = the project's own tests pass on the merged result, judged by `ck gate` plus the spec's
  acceptance tests — not the agent's self-report and not reviewer approval.
- **3 runs per spec, both harnesses**, each from a fresh worktree and branch at the same base commit.
  Report `pass@1`, `pass^3`, wall clock, tokens, dollars, escalation count, throttle count and peak
  context per child.
- **Every failure gets one label:** `model`, `port`, `throttle`, `spec`, `env`. Unlabeled failures are
  excluded from the table.
- **Calibration first:** one spec on OpenCode with `claude-sonnet-5-5`; if that fails too, the port is
  broken and cheap-stack numbers are meaningless.
- Reset protocol: clean tree, no leftover agent state, hard wall-clock and escalation caps per run.
- **Harness artefacts live outside the worktree under test.** A model under test edited a log file
  inside the project to argue its own case (spike 20); run records and probe logs are written by the
  harness to a path outside the repo, and are never read back from a location the agent can write.

### D7 — Telemetry: native ingest into ck's run records

- The bridge emits run data and `ck` builds the record itself: a new `ck analytics record` entry point
  (working name) produces `ck-run-local/1` from the bridge's JSON (label, phase, role, model, requested
  and resolved effort, start/end, tokens by model, status) — **no Claude transcripts, no fabricated
  `.claude/` artifacts**.
- `_build_record` is refactored to accept pre-built agent rows through a small source interface, so the
  transcript path and the bridge path share pricing, `roles.match_label` and aggregation. No forked
  aggregation code.
- Pricing: an `opencode` section keyed by `provider/model`, with explicit `free: true` (zero rates) so a
  free run reports **$0.00** rather than "cost unknown". `bench.PRICING` today knows only
  haiku/sonnet/opus, so every non-Anthropic model currently prices as `None`.
- New fields: `harness: "opencode"` at run level; `requestedEffort` / `resolvedEffort` / `variant` per
  agent row; `throttles` / `retries` at run level; all added to `SHAREABLE_SCHEMA`.
- Identity: run id = the workflow run id, so `ck cost <runId>` works unchanged; `claudeCodeVersion` is
  null on OpenCode runs and `collectorVersion` carries the bridge version.
- Ingestion trigger: an `unfinished` / `partial` record is written at spawn and closed on completion, so
  a crashed run still appears (mirrors the existing open-manifest sweep).
- **Verified sources** (spikes 6, 10, 18): `session.get({sessionID})` returns cumulative
  `tokens{input, output, reasoning, cache{read, write}}`, `cost`, and `time{created, busy, idle}`;
  `session.usage.updated` carries the same `{sessionID, cost, tokens}` as an event; and
  `session.context({sessionID})` is the **message list** (`SessionMessageInfo[]`), not a context object —
  per-step detail comes from there. Peak context per child = max input+cache.read across steps.
- **Amendment resolved:** additive within `ck-run-local/1` and `ck-run-analytics/1`, extending
  `SHAREABLE_SCHEMA` and `analytics/SCHEMA.md` — the same pattern 0.10.3 used for `interruptions`. No
  migration, no version bump.

### D8 — Command naming: native `/spec/plan`, no `agent:` frontmatter

- Commands ship as `.opencode/commands/spec/{plan,create,review,implement,spawn-worktree}.md`, giving
  `/spec/plan` etc. Cross-references (`/spec:implement 3`) are rewritten to `/spec/implement 3` in the
  vendored copy at install time; the Claude Code sources stay untouched.
- **No `agent:` frontmatter** on any of the five: `agent:` switches the *current* session's agent, which
  would break the step that writes `PLAN.md` (`/spec/plan`) or the `ck` calls (`/spec/implement`). The
  session's agent and model stay the user's choice, which is also what makes the D4 model checks real.
- `argument-hint` is folded into `description`; `allowed-tools` is dropped (permissions belong to agents,
  D3), since OpenCode command frontmatter has only `description`, `agent`, `model`, `subagent`.
- Tool-name prose is rewritten in the same install-time pass: `AskUserQuestion`→`question`,
  `Agent`→`subagent`, `TodoWrite`→the todo tool, `SendMessage`→`subagent` with the returned `sessionID`.
  `Workflow` needs no rewrite because the bridge tool is literally named `workflow` (D1).
- Bodies port verbatim otherwise: none of the five skills contains `@path` references or `!`cmd``
  blocks, which OpenCode would not expand (and would run outside the permission flow).
- `plugin/skills/*/SKILL.md` remains the single source for both harnesses.

### D9 — Hooks: subprocess shim plus latches

- Translate each OpenCode tool event into Claude Code's hook JSON (`cwd`, `tool_name`, `tool_input`,
  `session_id`, `agent_id`, `agent_type`, `tool_use_id`), run `ck --hook <name> --plugin-hook` unchanged,
  and map the answer back. All guard logic stays in Python; nothing is reimplemented.
- Permission mapping: `deny` → throw the reason verbatim; `ask` → `permission.assert`. **On a headless run
  (`CK_RUN_HEADLESS=1`) `ask` degrades to `deny`** with the same reason, because `assert` genuinely blocks
  waiting for a human (spike 9) and a benchmark arm has none.
- Post-hoc blocks are **latches**: `done-verify` / `stop-verify` returning `decision: block` stores a
  pending message against the session, and the next `execute.before` fails with that original reason.
  OpenCode has no way to retroactively block a completed tool, and this reproduces Claude Code's
  "you are not done yet" behavior using only available primitives (verified: spike 4).
- **Ignore the code-mode `execute` tool.** OpenCode routes tool calls through a built-in `execute` tool
  (`tools.edit({...})` in a JS string), so every call raises two hook events with the same call id: the
  outer `execute` (tool `"execute"`, input `{code}`) and the inner real tool (structured input). The shim
  acts only on inner events; the outer one is a no-op. Throwing on the inner event blocks the edit and is
  reported to the model as the code-mode error object (spike 14).
- Tool translation table, driven by the **inner** names and their real shapes: `edit {path, oldString,
  newString}`→`Edit {file_path, old_string, new_string}`, `write {path, content}`→`Write`, `patch`→
  `MultiEdit`, `shell {command, workdir?}`→`Bash` (`workdir` becomes a `cd` prefix only when it differs
  from the session directory). `read`/`grep`/`glob`/`subagent` pass through untouched. This table gets
  its own tests built from captured payloads — it is the most bug-prone piece of the port.
- Attribution: pass the OpenCode session id as `session_id` and the child's role agent as
  `agent_id`/`agent_type`, so guard-log events and role analytics land in the right bucket (shared
  mechanism with D7).
- Guard log: free — `ck --hook pre-tool-guard` writes it, which is what makes `interruptions.guard_denials`
  and `guard_escalations` available to `ck stats`.
- Timeouts: if OpenCode caps hook duration below `done-verify`'s 600s budget, run it in the background and
  deliver the verdict through the latch on the next call.

### D10 — Packaging: npm plugin package

- Ship `opencode-claude-kiro` as a published npm package with a **server part** (`Plugin.define({id, setup})`),
  loaded via `plugins: ["opencode-claude-kiro@<version>"]` in `opencode.json(c)`.
- **Commands are registered at runtime**, not vendored: `ctx.command.transform(editor.add({name, execute}))`
  reads `plugin/skills/*/SKILL.md` from the package, applies the D8 substitutions in memory, and prompts
  the session with the rendered body plus the user's arguments. `/spec/plan` therefore exists as soon as
  the plugin loads, with no file install step and no drift. Verified: `add({name:"spec/plan"})` registers,
  and `inv.prompt.text` carries the raw argument string (spike 2).
- **Skills** can likewise be registered with `ctx.skill.transform(editor.add({id, name, description, path,
  content}))` if we ever want them auto-invocable.
- **Agents are the exception:** `AgentEditor` exposes `list/get/default/update/remove` and no `add`, so the
  five role agents still have to be materialized as **flat** files — `.opencode/agents/ck-worker.md`,
  `.opencode/agents/ck-reviewer.md`, … (nested directories are silently ignored, spike 12). The plugin
  writes them on setup, digest-guarded, only when absent or unmodified. They are not visible in
  `agent.list`/`agent.get` either, so the preflight verifies the files, not the API (spike 13).
- `${CLAUDE_PLUGIN_ROOT}` needs no substitution scheme: the plugin root is the package directory, resolved
  from the module URL, so workflow and brief paths point inside `node_modules`.
- `ck` remains an external dependency. The plugin checks `ck --version` at startup and fails loudly with an
  install hint if it is missing or older than the expected version, rather than silently running without a
  guard.
- Repeatability for the benchmark: a fresh worktree needs only the package entry (+ `ck`), which is the
  cleanest possible baseline for "same install, N runs".

### D11 — Scope: full interactive surface, no headless mode

- **In:** the five commands, both workflows verbatim, the five role agents plus the generic worker, the
  guard hooks and post-check, `AGENTS.md` from the output style, the `without-workflow-tool` fallback,
  `spawn-worktree` (launch several without waiting), notifications on park, and `ck cost` / `ck stats` /
  `ck share`.
- **Out:** `ck run` (headless, sandboxed — depends on Claude Code's Bash sandbox and `claude -p`),
  agent-mode analytics (`ck stats begin/collect --agents`), skills auto-invocation (commands only, so
  the same text isn't surfaced twice), and any TUI panel or `/workflows` browser.
- `ck handoff` self-disables on OpenCode and returns `interactive: true`, which is the desired behavior.
- Every excluded surface is recorded per run as a deviation, so a benchmark result never implies parity
  it doesn't have.

### D12 — Degradation: preflight, fail fast, labeled mid-run degradation

- **Preflight**, also run automatically at the start of `/spec/implement <spec>` and available as
  `ck doctor --harness opencode`: `ck --version` compatible, plugin loaded and the `workflow` tool
  registered, every configured `provider/model#variant` resolvable, agent **files** present with valid
  frontmatter (the agent API cannot see them and `session.create` accepts unknown ids silently —
  spike 13), hooks armed (verified with a self-test call that must be denied), target branch clean.
- **The guard is not degradable.** Everything else may degrade with a label in the run record; if the
  guard cannot run (ck missing, hook crash/timeout, unmappable tool shape) the run **halts**. A green run
  with the guard silently off is a measurement of a different product, and it is the one failure mode
  that can merge weakened tests.
- Models: a configured ref that doesn't resolve aborts with the nearest available ref as a suggestion.
  An opt-in `--allow-model-fallback` substitutes and labels the run arm instead.
- Throttles: bounded exponential backoff, counted separately; a task that exhausts retries is labeled
  `throttle`, never `model`.
- Context: a compaction during a task marks the run `degraded: compaction` with the task id so it can be
  excluded or reported as its own arm (the 200K free-tier window makes this likely).
- Fallback stays explicit: an unavailable/refused `workflow` tool says so and offers `--no-workflow`; it
  never switches paths silently.
- `ck doctor` prints a matrix: harness, plugin version, ck version, workflow tool, hooks, agents, models
  with requested→resolved variants (D5), and the D11 out-of-scope surfaces.

### D13 — Benchmark execution: `ck bench` import now, launcher later

- **Reuse `ck bench`** instead of building a benchmark driver. It already provides seeded arm order,
  resumable arms, fresh clones at a pinned base, hidden-suite scoring and clustered bootstrap CIs.
- **Step 1 (now):** the bridge emits a workflow output in the shape `ck bench import` accepts — `result`
  plus a `workflowProgress` list (label, phase, model, state, started/duration, tokens) built from the
  same data as D7. Arms run via a small script, then `ck bench import` + `ck bench score`.
- **Step 2 (later):** add `--harness opencode` to `ck bench` — a launcher that runs a non-interactive
  OpenCode session, plus a collector that reads the `ck-run-local` record by run id instead of Claude
  transcripts. Both collectors sit behind one interface so `ck bench score` is harness-agnostic.
- **`ck run` is not ported**, and the bench launcher is not `ck run`: it needs to launch a non-interactive
  session and nothing else. `ck run`'s value is its sandbox, which OpenCode has no equivalent for.
- **Headless flag:** the bench launcher sets `CK_RUN_HEADLESS=1`, which `ck.handoff.is_headless` already
  understands, so `ck handoff` reports `interactive: false`, guard-stopped tasks park, and nothing blocks
  on a prompt. Interactive `/spec/implement` keeps `interactive: true` and asks via `question`.
- **Scoring untouched:** hidden suites, `--baseline`, `--cluster`, `--seed`, bootstrap CIs.
- **Arm naming:** `deepseek-free`, `muse-free`, `opus-5-5`, `sonnet-5-5`, so E0–E2 and the baseline land in
  one `score.md`.

## Experiments to run next

Order matters: the calibration run gates the rest.

| # | Arm | Model (all roles) | Purpose |
|---|---|---|---|
| E0 | Calibration | `opencode/claude-sonnet-5-5` | Prove the port itself works. If E0 fails, stop — nothing below is interpretable. |
| E1 | Free tier A | `opencode/deepseek-v4.1-flash-free` | 200K/32K limit, FC 52.6 but TB 28.3. Expect gate/worktree failures. |
| E2 | Free tier B | `opencode-go/muse-spark-1.3-contributor` (confirm the free id) | 1M/131K, no AA agent score. Different family, better terminal use in the AA row (31.8). |

E1 and E2 are both **all roles pinned to one model** for the first pass, exactly as Claude Code's
all-Opus/all-Sonnet runs are single-model. Role-specific ladders come after these two numbers exist.

Each arm runs the same 3× protocol from D6 on the same specs, and is compared against the Claude Code
baseline (Opus 5.5 and Sonnet 5.5, 3× each).

Why these two first: they are the only $0 models with a plausible coding profile, and they differ on
the axis that matters most here — E1 has the better patch score and the smaller window, E2 has the
bigger window and the better terminal score. E1 vs E2 therefore separates "context starvation" from
"terminal weakness" as causes, which is the question that decides whether a paid escalation rung is
needed at all.

Caveats recorded with the results, not after them:
- E2's AA-adjacent numbers were measured in Muse Code (co-trained harness) — upper bound, not a
  prediction for this bridge.
- E1's AA numbers were measured in Codex (third-party harness) — a fairer predictor.
- Both free tiers may throttle; throttles are counted separately and never charged to the model.

## What 0.10.3 changed for this port

- **D1/D2 unchanged.** `spec-implement.js` grew ~220 lines (`interactive` arg, `guardStop` in `RESULT`,
  `PARKED` schema, `parkPrompt`, guard ladder, `needsInput` in `REPORT`) but uses only the same five
  runtime helpers and the same schema keywords — nested `properties`/`required` are already in the D2
  validator's set. No new globals, no new `agent()` options.
- **New hard requirement for D4/D5:** the guard ladder is computed by string-matching the attempted
  model's family (`haiku|sonnet|opus|fable`) and then passes literal `model: 'sonnet'` / `'opus'` /
  `'fable'` to `agent()`. On OpenCode every opaque ID is "unknown family", so the ladder always returns
  all three and the bridge **must** resolve those three aliases to configured `provider/model` refs.
  The mapping is therefore bidirectional: aliases → OpenCode refs, and opaque refs passed through.
- **D7:** records gained an `interruptions` block (schema untouched, `SHAREABLE_SCHEMA` extended).
  Its source is the guard log written by the hooks plus transcripts, so on OpenCode interruption counts
  are absent unless the ported hooks write the same log — a D9/D7 coupling to decide together.
- **D8:** `ck handoff <kind> $ARGUMENTS --json` now opens the `create` and `implement` commands.
  `decide_handoff` only acts on sessions under `.claude/worktrees/`, so on OpenCode it returns
  `{"handoff": false, "interactive": true}` — the text runs verbatim, and `interactive: true` is the
  correct behavior (guard-stopped tasks are asked via `question`, not parked).
- **D13:** `ck run` has no OpenCode equivalent; handoff self-disables, which is the desired behavior.

## Spike results (run 2026-10-30, OpenCode 2.2.3, Bun 1.4.4 / node 26.3.0)

Probes lived in `/tmp/opencode/spike/.opencode/plugins/probe*.js`, logs in
`/tmp/opencode/spike-logs/findings*.log` (deliberately outside the project so a model under test cannot
read or append to them — see finding 12). Each probe is a throwaway plugin against a real server.

### Passed, as designed

1. **Runtime reach (D1, D10).** `node:child_process`, `node:fs`, `node:vm`, `node:path`, `node:os` all
   import inside a server plugin; `spawnSync` and `vm.runInContext` work. `process.execPath` is the
   `opencode` binary (running it printed the CLI help), which confirms D1's "spawn `node`/`bun`
   explicitly, never `process.execPath`".
2. **Command registration (D8, D10).** `ctx.command.transform(editor.add({name:"spec/plan", execute}))`
   works; `ctx.command.list({})` returns `{location, data:[{name, description}]}` and shows `spec/plan`
   alongside the built-ins. Invocation through `ctx.session.command({sessionID, name:"spec/plan",
   text:"7"})` runs the handler with `prompt.text === "7"` — the **raw argument string**, exactly what
   `$ARGUMENTS` needs. `delivery` is `steer` on that path. (`opencode run "/spec/plan 7"` does *not*
   dispatch commands — the TUI/API does; that is a test-method artifact, not a port issue.)
3. **Pre-hook shape and deny delivery (D9).** `execute.before` receives
   `{tool, sessionID, agent, messageID, id, input}`; `id` is the provider's call id and `agent` is the
   child's agent id. A thrown `Error` (or `Tool.Error`) is delivered to the model as
   `{"error":{"type":"unknown","message":"<text>"},"content":[]}` — the message is **verbatim**, so
   `[ck guard]` and `[ck post-check]` survive.
4. **Latch (D9).** Armed in `execute.after`, delivered by throwing from the next `execute.before`: the
   next call was rejected and the model saw the original `[ck post-check] …` text. Nothing else fires.
5. **Per-child model and variant (D3, D4, D5).** `session.create({model:{id, providerID, variant},
   agent})` accepts the ref and **echoes the variant back** on the session (`model.variant === "high"`).
   A string model ref is rejected (`Expected Model.Ref | null`); `{providerID, modelID}` is rejected
   (`Missing key at ["model"]["id"]`). `Model.Ref.parse("provider/model#variant")` is the canonical
   splitter, so the bridge's `provider/model#variant` input maps directly.
6. **Running a child agent (D1, D2).** `session.prompt({sessionID, text, delivery:"queue"})` only
   enqueues (`SessionInboxUser`); `session.wait({sessionID})` then blocks until the loop finishes. A
   child prompted for `CHILD_OK` returned exactly that, with `tokens{input:1461, output:4,
   reasoning:12, cache.read:9088}` and `time{busy, idle}`.
7. **JIT tools (D2).** A tool registered by `ctx.tool.transform(editor.add(...))` **during a tool call**
   is immediately callable by a child session created afterwards; its `execute` received the validated
   input and `{id, sessionID, agent}`. `registration.dispose()` removes it from `ctx.tool.list()`. This
   is exactly the `submit_result_<seq>` lifecycle.
8. **Read-only role agent (D3).** `.opencode/agents/spec-reviewer.md` with
   `permissions: [{action: edit|write|shell, resource: "*", effect: deny}]` produced a child that has
   **no edit/write/shell tools at all** — it reported "no tool named edit is available". (The shipped
   reviewer denies edit/write/patch but keeps shell, for the `git diff`/`ck lint` reasons in D3.) Inner tool
   events carried `agent: "spec-reviewer"`, so attribution is per-child and automatic.
9. **Permission ask (D9c).** `ctx.permission.assert({sessionID, action, resources})` resolves when
   allowed (edit, shell) and **blocks waiting for a human** on `.env` reads — it did not resolve within
   20 s in a headless run. Denials reject with a `Tool.Error` ("Permission denied: edit").
10. **Completion and failure events (D12, D13).** `ctx.event.subscribe({types:["session.idle",
    "session.execution.succeeded", "session.execution.failed"]})` yields `{id, created, type, durable,
    data}`. `execution.failed` carried the provider error verbatim (`provider.invalid-request`, status
    400, upstream body) — that is the throttle/failure label source.
11. **`workflow` tool name (D1).** No collision: a plugin tool named `workflow` registers and lists
    normally; there is no built-in of that name.

### Findings that change a decision

12. **Agent ids cannot contain `/`.** `.opencode/agents/spec/reviewer.md` is **not** picked up: a session
    created with `agent:"spec/reviewer"` runs zero tokens, exactly like `agent:"nonexistent"`.
    Flat `.opencode/agents/reviewer.md` and `.opencode/agents/spec-worker.md` both work (and run at
    11k input tokens). Nested agent directories are not a thing, unlike commands.
    → **Amends D3/D10:** agent ids are flat, e.g. `ck-worker`, `ck-reviewer`, `ck-implementer`,
    `ck-fixer`, `ck-resolver`. Commands keep `/spec/plan` (slashes are fine there).
13. **`agent.list()` / `agent.get()` do not report custom agents.** The list showed only the seven
    built-ins; `agent.get({agentID:"reviewer"})` errored `Agent not found` for every custom agent, all
    the while the same agents ran successfully. `session.create` **silently accepts unknown agent ids**,
    so a typo fails deep inside a run with an empty reply.
    → **Amends D12:** the preflight must verify agent **files** (existence + frontmatter parse + a
    known-mode check), never the agent API. It must also refuse to start when a configured agent file is
    missing, because nothing downstream will tell us.
14. **Code mode is on by default and wraps tool calls.** The `execute` tool (empty input schema) lets the
    model call tools as `tools.edit({...})`; the plugin's hooks then fire **twice for one call id** — once
    for the outer `execute` with `input.code` (a JS string) and once for the inner real tool with the
    structured input. A throw on the outer call stops the inner call from ever running; a throw on the
    inner call blocks the edit and is reported to the model as the code-mode error object. Confirmed: a
    denied inner `edit` left the file untouched, and the model quoted `[ck guard] …` back.
    → **Amends D9:** the shim must **ignore `tool === "execute"`** and act only on inner events
    (otherwise a guard that reads `input.code` would deny on string heuristics). The D9a translation
    table is therefore driven by the inner names, and the outer `execute` event is a no-op.
15. **Concrete inner tool shapes** (camelCase, not Claude's):
    `edit {path, oldString, newString}`, `read {path}`, `shell {command}` (no `workdir` unless set),
    plus `write {path, content}`, `patch`, `glob {pattern}`, `grep`. `MultiEdit` has no equivalent.
    → **Confirms D9a**, with the mapping table corrected: `edit`→`Edit` (field rename), `write`→`Write`,
    `patch`→`MultiEdit`, `shell`→`Bash` (`workdir` → a `cd` prefix only when it differs from the
    session directory).
16. **`session.wait` can return before the run starts.** After `session.command`, `wait` resolved in
    3 ms with the child still idle, and the execution began afterwards. With `session.prompt` it behaved
    correctly, but that is timing, not a guarantee.
    → **Amends D1:** never trust a single `wait`. `runChild()` observes `session.get().time.busy`
    appear (started) and then `time.idle ≥ busy` (finished), with a wall-clock cap; `session.wait` is
    only an accelerator. Completion is also cross-checked against `session.execution.succeeded` /
    `execution.failed` and the last assistant message's `finish`.
17. **A child with no explicit model fails.** A child created without `model` inherited the session
    default and died with the upstream "This Go model trains on request data" error, i.e. 0 tokens, empty
    text. → **Confirms D4/D12:** every `agent()` call must pass a resolved model ref; the preflight
    verifies it first.
18. **`session.context({sessionID})` is the message list**, not a context object (`SessionContextInput`
    → `SessionMessageInfo[]`). Token totals live on `session.get().tokens`, and
    `session.usage.updated` gives `{sessionID, cost, tokens}`. → **Confirms D7's source.**
19. **`session.create` accepts `permissions` and `location.directory`.** Per-child permission rulesets
    and per-child directories are both available, which means the bridge can pin a child to a worktree
    without `cd` tricks. (`parentID` also exists for real nesting; D3 still keeps nesting at 1.)
20. **A model under test wrote into the log file it could see.** One run appended a fabricated
    "model observation" entry to `findings6.log` inside the project, arguing that the command had not
    fired. → **Amends D6/D13:** probe and run artefacts live outside the worktree (and the benchmark's
    own logs must be outside the repo under test), and run records are written by the harness, never read
    back from a path the agent can write.
21. **The IPC transport works, and the runtime to spawn is `node`.** From inside the Bun-hosted plugin,
    `spawn("node", [child], {stdio:["ignore","pipe","pipe","ipc"]})` plus `child.send` /
    `process.on("message")` exchanged three messages and exited cleanly. `node` on PATH is
    **v22.22.3**; `bun` is **not on PATH** at all. So the runner binary is `node`, not `bun`, and the
    preflight checks `node --version >= 20`. (The workflow scripts use only the stdlib, so node 22 is
    sufficient; `vm.SourceTextModule` is avoided in favour of `vm.runInThisContext` + dynamic import.)

### Still open (not blocking the plan)

- Whether a *custom* `reasoningEffort` can be declared as a variant on `deepseek-v4.1-flash-free`
  (D5 option C). Probe 5 only proved the passthrough of an existing variant (`high`).
- Whether `session.create({permissions})` can constrain a *primary* role agent (the workflow's implement
  step) as tightly as a file-based `permissions` block does.
