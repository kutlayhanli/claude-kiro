# OpenCode native run record: contract

Status: frozen 2026-10-30. The TS plugin writes this file; `ck` ingests it into `ck-run-local/1`.
No Claude transcripts are read on this path.

Two artefacts per workflow run, both under the run state directory (outside the worktree):

```
<state>/runs/<runId>/record.json   native record, written partial at spawn, closed on done/error
<state>/runs/<runId>/result.json   raw workflow return value (debugging / Phase 4 import)
<state>/runs/<runId>/run.log       human log
```

`<state>` is `opencode.run.stateDir` or `$XDG_STATE_HOME/claude-kiro/opencode`
(`~/.local/state/...` when unset). `runId` is the workflow run id (`wf-...`), so `ck cost <runId>`
and `ck stats` keep working.

## `record.json` — `ck-opencode-run/1`

```jsonc
{
  "schema": "ck-opencode-run/1",
  "runId": "wf-20261030192320-56aaa6",
  "harness": "opencode",
  "workflow": "spec-create" | "spec-implement" | "other",
  "collectorVersion": "0.10.3",          // the npm package version
  "ckVersion": "0.10.3",                 // `ck --version`, resolved by the plugin or by the ingester
  "claudeCodeVersion": null,             // always null on this harness
  "status": "running" | "done" | "failed" | "partial",
  "startedAt": 1793388200665,            // epoch ms
  "endedAt": null | 1793388233000,       // epoch ms; null while running
  "projectRoot": "/abs/main/checkout",
  "spec": "auth" | null,                 // args.spec, else args.feature
  "into": "integrate/auth" | null,       // args.into
  "args": { /* workflow args, verbatim */ },
  "result": null | { /* workflow return value, verbatim */ },

  "throttles": 0,                        // provider 429 / retry-after events observed
  "retries": 0,                          // whole-child retries (not in-child result rejections)
  "failureLabel": null | "model" | "port" | "throttle" | "spec" | "env",
  "degraded": [] ,                       // e.g. ["compaction"]; [] when clean

  "guard": {
    "denials": 0,
    "postCheckCatches": 0,
    "guardEscalations": 0,
    "headlessDenials": 0,
    "prompts": 0,                        // ask decisions a human answered
    "events": [                          // optional; bounded, no prompts or file contents
      { "at": 1793388200665, "kind": "deny" | "post_check" | "ask" | "failure", "agent": "ck-worker", "reason": "[ck guard] …" }
    ]
  },

  "agents": [
    {
      "seq": 1,                          // 1-based, the order the runner asked
      "label": "task 3",                 // the workflow's label; Python matches the role from it
      "phase": "Wave 1" | null,
      "role": null | "implementer" | "test_writer" | "reviewer" | "fixer" | "resolver" | "orchestrator" | "other",
      "agent": "ck-worker",              // the OpenCode agent id
      "parentSessionID": "ses_…",
      "sessionID": "ses_…",
      "requested": {
        "model": "sonnet" | "provider/model" | "inherit",
        "effort": "medium" | "inherit" | null
      },
      "resolved": {
        "providerID": "opencode-go",
        "id": "beam-free",
        "variant": "default" | null
      },
      "requestedEffort": "medium" | null, // normalized ck effort or null
      "resolvedEffort": "high" | null,    // the variant actually placed on the ref
      "variant": "high" | null,           // same as resolvedEffort; kept for the analytics join
      "startedAt": 1793388201000,
      "endedAt": 1793388202000,
      "durationMs": 1000,
      "status": "ok" | "failed" | "timeout" | "agent_error" | "result_error" | "model_error" | "session_error",
      "submission": "tool" | "text" | "text-empty" | null,
      "attempts": 1,                      // 1 + whole-child retries
      "rejections": 0,                    // in-child schema rejections
      "tokens": { "input": 0, "output": 0, "reasoning": 0, "cache": { "read": 0, "write": 0 } },
      "cost": 0,                          // session.get().cost; free models are 0
      "peakContext": 0,                   // max(input + cache.read) across assistant messages
      "error": null | "the provider returned 429 …"
    }
  ]
}
```

### Field rules

- **Identity:** `runId` is the workflow run id, not the OpenCode session id. `claudeCodeVersion` is
  always `null`; `collectorVersion` is the npm package version.
- **Agents:** one row per `agent()` call. `attempts` is the number of whole-child attempts (1 plus
  the retry); `status`/`error` describe the attempt that ended the call. A failed first attempt is not
  a separate row; the run-level `retries` sums `attempts - 1`. `seq` is unique per run.
- **Model/effort:** `requested` is what ck asked for; `resolved` is the actual OpenCode ref.
  `requestedEffort` is the normalized ck effort (`low|medium|high|xhigh|max`) or `null`; `resolvedEffort`
  and `variant` are the actual variant, or `null` when the effort was dropped. Never infer the
  resolved model from the requested alias.
- **Tokens/cost:** `tokens` is the final `session.get().tokens` snapshot. `cost` is
  `session.get().cost`. `peakContext` is the maximum of `input + cache.read` over the child's
  assistant messages; `0` if there are none.
- **Status:** `ok` only when the child produced a usable result. `result_error` means the structured
  result was missing or invalid after the retry; `agent_error` means the zero-token/unknown-agent
  case; `model_error` / `session_error` are resolution/creation failures; `timeout` is the wall-clock
  or start watchdog.
- **Guard:** counts are cumulative; `events` is optional and bounded (the ingester ignores unknown
  keys). `reason` is the `[ck guard]` / `[ck post-check]` text, not a prompt or file body.
- **Degradation:** `degraded` is additive. `["compaction"]` marks a run whose child context was
  compacted; `["guard"]` would be a hard failure, not a degraded label.
- **Failure label:** at most one of `model|port|throttle|spec|env`. The plugin labels a provider
  quota/error as `model`, a 429/retry-after as `throttle`, a bridge/agent-file/session failure as
  `port`, and leaves workflow errors to the run report.

### Partial records

At spawn: `status: "running"`, `endedAt: null`, `result: null`, `agents: []` (or the rows completed
so far). On `done`/`error`: `status: "done"|"failed"`, `endedAt`, `result`, all agent rows. A process
that dies leaves a `running` record; `ck analytics record` accepts it and marks the run `partial`.
The plugin rewrites the file atomically (write a temp file, then rename) after each agent row, so a
crash never truncates the last completed row.

## `ck` ingestion

Add a CLI entry point (working name `ck analytics record <file>`, fold it into the existing `stats`
group if that fits the CLI better) that:

1. Reads and validates `ck-opencode-run/1`; a malformed file fails with a message, never silently
   produces an empty record.
2. Refactors `analytics/collect.py::_build_record` to accept pre-built agent rows through a small
   source interface. The transcript path and this path must share pricing, `roles.match_label`,
   guard/interruption aggregation, `_workflow_metrics` and the final `ck-run-local/1` assembly.
3. Sets `harness: "opencode"`, `claudeCodeVersion: null`, `collectorVersion` from the file,
   `source: "workflow"`, and the run id from `runId`.
4. Prices `opencode` models with an explicit `free: true` (zero rates) so a free run reports
   `$0.00`, not "cost unknown". Unknown paid models stay `null` and are labeled unknown, not $0.
5. Adds the new fields additively to `SHAREABLE_SCHEMA` + `analytics/SCHEMA.md`:
   run-level `harness`, `requestedEffort`/`resolvedEffort`/`variant` per agent row, `throttles`,
   `retries`, `degraded`, `failureLabel`. No schema version bump; follow the 0.10.3 `interruptions`
   precedent.
6. Writes the same local record as the transcript path (`store.record_path`), so `ck cost`,
   `ck stats` and `ck stats share` work unchanged. `ck cost <runId>` must price a free run at $0.00.

## Phase 4: `bench-output.json`

`workflow-tool.ts` also writes, next to `result.json`:

```jsonc
{
  "result": { /* workflow result */ },
  "workflowProgress": [
    { "label": "task 3", "phase": "Wave 1", "model": "sonnet", "state": "done",
      "startedAt": 1793388201000, "durationMs": 1000,
      "tokens": { "input": 0, "output": 0, "reasoning": 0, "cache": { "read": 0, "write": 0 } } }
  ]
}
```

This is the shape `ck bench import` already accepts (verify against `bench.import_output` before
changing either side). The bench launcher sets `CK_RUN_HEADLESS=1`; the workflow tool inherits it.

## Ownership

- **TS plugin:** `opencode/src/record.ts` (new) writes `record.json`; `workflow-tool.ts` supplies the
  run envelope and per-agent rows; `run-child.ts` supplies the child metrics.
- **Python:** `src/claude_kiro/analytics/collect.py`, `src/claude_kiro/bench.py` pricing,
  `src/claude_kiro/cli/main.py`, `analytics/SCHEMA.md`, and tests under `tests/`.
- **Do not** read Claude transcripts on the OpenCode path, and do not put run records inside the
  project worktree: a model under test appended a fabricated entry to a log it could see (spike 20).
