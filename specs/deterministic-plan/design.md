# Design: Deterministic Plan

## Overview

Whole-spec `/spec:implement` runs halt when a plan agent returns a ready list that
contradicts its own dependency graph, and plan/review/land agents dominate cost
(53 plan, 62 review, 49 land agents in the equity run). This change makes the
scheduler in `plugin/workflows/spec-implement.js` deterministic-first: an
inconsistent plan is retried once, then the run continues from a deterministically
computed ready list and is marked degraded instead of halting. It also cuts
mechanical overhead (fewer plan agents, wave-batched reviews) and surfaces the
saving in `ck stats` and the run report with a warn-only budget.

How the PLAN.md decisions shape it:

- **D2 (retry once, then deterministic fallback, degraded).** `runPlan` keeps its
  two attempts; on the second failure it no longer returns null into a halt but
  resolves a deterministic ready list and records a fallback entry.
- **D5 (layered fallback source).** Script-local `readyTasks` from the `deps` arg
  when present; else the last good plan's `ready`; halt only when neither exists.
- **D7 (wave-batched review, risky always individual).** One `review` agent per
  landing batch for all non-risky tasks, before it merges; each safety/risky task keeps its own review.
- **D3/D8/D6 (batching in scope, warn-only budget, full degraded marking).**
  Fewer plan triggers, batched reviews/landings, `degraded: true` + fallback count
  in the result, a report section, and `plan_fallbacks` next to the overhead share
  in `ck stats`.
- **D1/D4 (evolves landing-speed, fixture + equity re-run).** Landing batches,
  red-batch bisection, finish-first, queue-wait metrics and `readyTasks == ck plan`
  ordering are reused unchanged, not re-decided.

## Components

### Existing Components to Modify

- `plugin/workflows/spec-implement.js` — the scheduler (stories 1–4).
  - **Decisions block.** Add two pure seams (`fallbackReady`, `reviewGroups`,
    `overheadShare`/`overheadWarning` — see Decision Seams) next to the existing
    `readyTasks`, `chainLengths`, `planConsistent`, `planDecision`, `startDecision`.
    The block stays pure (no script globals, no I/O) and loadable by
    `tests/js/load_decisions.mjs`.
  - **`runPlan(trigger)`.** Keep two attempts with the same per-attempt exclude
    capture and `planConsistent` check. On second failure call
    `fallbackReady(...)`: append `{ planStep, trigger, source, ready }` to
    `planFallbacks` for every double-failure trigger — even when the yielded
    list is empty (an empty entry records a fallback that scheduled nothing) —
    set `degraded = true`, adopt the fallback as the scheduling ready list, and
    return a synthetic plan view (`{ fromFallback: true, ... }`) instead of null.
    Only when there is no deterministic data at all (`!hasDeps` and no usable
    last-good) does it return null (start-plan halt and the existing `PLAN_FAILED`
    path, requirement 2.3); an empty list with data present yields an empty
    synthetic view that waits for landings instead of halting.
  - **`planContext` / `schedule()`.** `planDecision` no longer runs a plan on
    `freed`/`finished` when `SCRIPT_READINESS` is true (requirements 3.2, 3.3);
    `schedule()` starts from script-side `readyTasks` directly. `idle` trigger
    keeps `idleArmed` gating (3.4) and `landing` trigger stays (3.1). When
    `SCRIPT_READINESS` is false, behaviour is unchanged: plan on every freed slot
    (3.6).
  - **`applyPlanView`.** On a consistent plan, replace remaining/deps/lint and set
    `done = plan.done ∪ mergedInRun` as today, and additionally store
    `lastGood = { ready, deps, order }` for the dep-less fallback (2.2). A
    fallback view never overwrites `lastGood`. First-attempt or retry success
    counts no fallback (1.4); `planFailures` still increments per failed attempt.
  - **Review path (before landing).** `reviewGroups([n], RISK)` decides per task.
    A risky (`safety`) task keeps the per-task `review` + revise loop in `runTask`,
    before it lands (4.2). A non-risky task registers in `pendingReview` and queues
    for landing; `landBatch` first calls `reviewBatch`, which runs one `review-wave`
    agent over the combined diff of the batch's not-yet-reviewed tasks (label
    `review-wave <wave> tasks <ids>`, e.g. `review-wave 2 tasks 4 5 7`, mapped to
    role `reviewer` by `roles.py` per 4.1) before the land step. Approved tasks stay
    in the batch and land (4.5). Each flagged task leaves the batch unmerged
    (`REVIEW_CHANGES`, with its own findings); its `runTask` runs the same revise +
    re-review loop as a risky task and lands it only once approved; blocking
    findings that survive fail it unmerged (4.4, 4.6). A task re-queued after a
    conflict, red landing or deferral is not reviewed again, and a batch with no
    unreviewed task gets no combined review (4.3). `REVIEW_SCHEMA` gains an optional
    `tasks: string[]` echo field so a batched verdict names its coverage.
  - **Report / return shape.** Result gains `degraded: boolean`,
    `planFallbacks: FallbackEntry[]`, `stats.planFallbacks: number`,
    `stats.planSteps/planFailures` unchanged, and `overhead: { share, threshold,
    warned }`. Report gains a "deterministic fallback" section (5.1) and an
    overhead line with the warn-only flag (5.4). Old records (no new fields) render
    as "not recorded" (5.5, 5.6).
  - **Overhead budget.** After the report is built, `overheadWarning(share)`
    sets `overhead.warned`; it never sets `stopLaunching` (5.4, NFR-5).
- `.claude/workflows/spec-implement.js` — regenerated by `uv run ck vendor`,
  byte-identical to the plugin source.
- `plugin/skills/implement/SKILL.md` (and vendored `.claude/commands/spec/implement.md`):
  Step 6 keeps passing `deps`/`remaining`; Step 7 documents the new triggers
  (start / after each landing / idle-only) and wave-batched review.
- `opencode/src/workflow-tool.ts` — no arg-shape change (`deps` already passes
  through untouched inside `args`). `validateWorkflowArgs` stays `spec`+`root`
  only: `deps` is optional by design (dep-less runs are legal). The vendored
  `vendor/workflows/spec-implement.js` copy is refreshed so the fallback code
  ships to the OpenCode path.
- `src/claude_kiro/analytics/share.py` — `_counts` reads
  `stats.planFallbacks → counts.plan_fallbacks`; add `"plan_fallbacks"` to
  `COUNT_KEYS` (additive; shareable schema gains the dotted path).
- `src/claude_kiro/analytics/show.py` — `run_breakdown` adds `plan_fallbacks`
  (from `counts`) and `overhead` (`overheadShare` over `byRole` ms, see seams);
  `render_run` prints a `plan fallbacks:` line and an
  `overhead (plan/review/land share):` line, or `not recorded` when absent (5.2);
  `run_json` adds `planFallbacks` and `overhead` with the same values (5.3).
  All reads use `.get`, so pre-change records show `unknown`/`null` (5.5, 5.6).
- `src/claude_kiro/analytics/roles.py` — add a `review-wave` label pattern to the
  spec-implement grammar: `review-wave <wave> tasks <id>...` maps to role
  `reviewer` with `matched=True`, `wave=<wave>`, and `tasks` set to the covered
  task ids (criterion 4.1: the batched review is a review agent, never `other`).
  The W + R review-count bound (NFR-4) counts every role-`reviewer` label from
  spec-implement (`review N`, `review N round k`, `review-wave W tasks ...`) as
  one review agent, and the overhead reviewer share (5.2) includes `review-wave`
  agent time.
- `src/claude_kiro/analytics/issues.py` — `derive_issue_codes` adds
  `planning_fallback` when `stats.planFallbacks > 0` (alongside the existing
  `planning_failed` on `planFailures`); no halt/fail semantics change.
- `src/claude_kiro/bench.py` — `summarize_report` copies `degraded`,
  `planFallbacks`/`plan_fallbacks` and `overhead` through so the equity re-run
  comparison (6.3, NFR-2) can read them.

### New Components

- `specs/deterministic-plan/fixtures/fallback-spec/` (new) — tiny spec (3 tasks,
  `1 → 2`, `1 → 3`) plus a fixture plan-agent shim that returns a ready list
  contradicting its deps on demand (see Data Flow). Owned by the test plan; this
  design only fixes its contract: healthy run → no fallback, no degraded mark
  (6.2); forced-inconsistent run → retry once, fallback, completion, degraded (6.1).
- No new Python module. All scheduling rules live in the workflow decisions block;
  all stats rules live in existing `analytics/` modules.

## Public Interfaces

### Workflow args (`plugin/workflows/spec-implement.js`)

Unchanged shape; `deps`/`remaining` keep their landing-speed meaning:

```js
{
  deps: { '1': [], '2': ['1'] },  // "deps" from ck plan --json; plain object = SCRIPT_READINESS on
  remaining: ['1', '2'],           // tasks.md order, the readiness order
}
```

A missing/non-object `deps` means dep-less mode (3.6): plans run on every freed
slot and the fallback uses the last good plan (2.2) or halts (2.3).

### Plan agent result (`PLAN` schema) — unchanged

`{ done, remaining, deps, ready, cycles, verifyCycles, error? }`; the prompt
(`ck plan --json --exclude …`) and `usablePlan`/`normalizePlan` are unchanged.

### Workflow return value — additive fields only

```js
{
  degraded: boolean,                    // true once any fallback fired (D6)
  planFallbacks: [                      // one entry per fallback (D6, 1.3, 5.1)
    { planStep: number,                 // 1-based plan counter (refills) that failed twice
      trigger: 'start'|'landing'|'idle'|'freed'|'finished',
      source: 'deps'|'last-good'|null,  // which D5 layer supplied the list (null: layers held data but nothing was schedulable)
      ready: string[] },                // the deterministic list actually scheduled
  ],
  overhead: { share: number|null,        // plan/review/land share of agent ms, null when unknown
              threshold: number,        // OVERHEAD_WARN_THRESHOLD (0.5)
              warned: boolean },        // share != null && share > threshold
  stats: { ..., planSteps: number, planFailures: number,
           planFallbacks: number },     // planFallbacks.length; pre-change runs omit it
  // ... everything else unchanged (merged, failed, notStarted, landings, tasks, timing)
}
```

`stats.planSteps` still counts plan agents; batched reviews reduce review-agent
count (NFR-4 bound: ≤ W + R). `overhead.warned` never halts/fails/retries (5.4).

### `ck stats show` (requirements 5.2, 5.3)

Text view adds (existing lines unchanged):

```
plan fallbacks: 2 (steps 3, 5; sources deps, last-good)   # or "plan fallbacks: not recorded"
overhead (plan/review/land share): 61% (warn: above 50% threshold)  # or "not recorded"; no warn suffix when under
```

JSON (`ck stats show <run> --json`) adds with identical values:

```json
"planFallbacks": {"recorded": true, "count": 2, "entries": [{"planStep": 3, "trigger": "landing", "source": "deps", "ready": ["4"]}]},
"overhead": {"recorded": true, "share": 0.61, "threshold": 0.5, "warned": true}
```

Absent data: `{"recorded": false, "count": null, ...}` / `share: null, warned: false`.
`ck stats collect` needs no change: it already carries `stats.*` through to
`counts.*`.

### Batched review prompt / result

```
review-wave <wave>: tasks 4, 5, 7 (non-risky, merged together). Read the combined
diff `git diff <merge-base>...<INTO> -- <their files>`, each task's block/requirements,
and design.md. Return verdict + per-task findings { task, severity, file, issue, fix }.
```

The agent label is `review-wave <wave> tasks <ids>` (e.g. `review-wave 2 tasks
4 5 7`); `roles.py` maps it to role `reviewer` with the wave number and the
covered task ids, so the NFR-4 review count and the overhead reviewer share
count it as a review agent (4.1).

`REVIEW_SCHEMA` gains `tasks?: string[]` (coverage echo). The scheduler fans a
batched verdict out to per-task `rec.review` rows before the batch lands: flagged
tasks keep their blocking findings, leave the batch unmerged and go to revision
(4.4); unflagged tasks record `{ verdict: 'approve', fromBatch: <wave> }` (4.5).
The review never runs after a merge: a revision is made on the task branch and
only reaches the target when that branch lands again.

### Decision Seams

Every rule that allows, refuses or holds something is a pure function: no I/O, no
clock, all inputs as arguments. Callers never re-implement the rule.

| Seam | Module (owner) | Signature | Decides |
|---|---|---|---|
| `readyTasks` (existing, reused) | `plugin/workflows/spec-implement.js` decisions block (scheduler) | `readyTasks({ order, deps, done, exclude, blocked }) -> string[]` | Which tasks may start, in ck-plan order (longest chain first, then numeric). Dependency met only if key of `deps` AND in `done` (2.5); unknown deps never start on a guess. |
| `planConsistent` (existing, unchanged) | same | `planConsistent(plan, exclude) -> boolean` | Whether a plan copy holds together (1.1 trigger for retry). |
| `planDecision` (modified: idle/`freed` gating) | same | `planDecision({ trigger, scriptReadiness, planInFlight, idleArmed, freeSlot, readyCount, openCount, stopLaunching }) -> 'run'\|'coalesce'\|'none'` | Plan agents run at start, after each landing, and idle-only when `freeSlot && readyCount===0 && openCount>0 && idleArmed` (3.1, 3.4); with script readiness `freed`/`finished` give `none` (3.2, 3.3); without it they give `run` (3.6). |
| `fallbackReady` (new) | same | `fallbackReady({ hasDeps, scriptReady: string[], lastGoodReady: string[], exclude: string[], done: string[] }) -> { ready: string[], source: 'deps'\|'last-good'\|null }` | The D5 layering: `scriptReady` minus excluded/done when `hasDeps` and non-empty → `deps`; else `lastGoodReady` minus excluded/done when non-empty → `last-good`; else empty/`null` → halt path (1.2, 2.1–2.3). Never fabricates: ordering is the input's (already ck-plan order, 2.4); unknown-dep tasks never appear because `scriptReady` comes from `readyTasks` (2.5). |
| `reviewGroups` (new) | same | `reviewGroups({ merged: string[], risk: {[t]: string} }) -> { batched: string[], individual: string[] }` | Wave review split: `individual` = merged tasks whose risk tag is `safety` (case-insensitive); `batched` = the rest. Wave of only risky → `batched` empty, no combined review (4.3). Empty `merged` → both empty, no review (4.6). |
| `overheadShare` (new) | `src/claude_kiro/analytics/show.py` (stats view) | `overheadShare(byRoleMs: Mapping[str, float]) -> float \| None` | Plan/review/landing share of agent time: `(ms_plan + ms_reviewer + ms_land) / ms_total` over `byRole` ms cells; `None` when no ms recorded (old runs → not recorded, 5.5). |
| `overheadWarning` (new) | same | `overheadWarning(share: float \| None) -> bool` | `share is not None and share > OVERHEAD_WARN_THRESHOLD (0.5)`; warn-only, never halt (5.4, D8, NFR-5). |

`startDecision`, `chainLengths`, `applyPlanView`, `worktreeOutcome`, `queuedAgents`
are reused unchanged from landing-speed.

## Data Models

```python
# Local record stats block (ck-run-local/1, additive; cf. share._counts)
"stats": {
    "planSteps": int,        # plan agents run (existing)
    "planFailures": int,     # failed plan attempts (existing)
    "planFallbacks": int,    # fallbacks taken; absent on pre-change runs -> not recorded
    # ... unchanged
}
# Shareable counts (ck-run-analytics/1; COUNT_KEYS gains "plan_fallbacks")
"counts": { "plan_steps": int, "plan_failures": int, "plan_fallbacks": int, ... }
```

```js
// Workflow script state (additions to landing-speed's scheduler state)
// planFallbacks: FallbackEntry[]   // appended in runPlan, reported in result
// lastGood: { ready: string[], deps: object, order: string[] } | null
// degraded: boolean                 // true once planFallbacks is non-empty
// OVERHEAD_WARN_THRESHOLD = 0.5     // script-local + mirrored in show.py
```

```python
# Stats view additions (show.run_breakdown)
"plan_fallbacks": {"recorded": bool, "count": int | None, "entries": [...]},
"overhead": {"recorded": bool, "share": float | None, "threshold": 0.5, "warned": bool},
```

Risk tags come from the existing `risk` arg (`'safety'` vs anything else,
case-insensitive via the existing `isSafety`); no new marking mechanism (assumes
the plan command's risk data, per requirements Assumptions).

## Data Flow

```mermaid
sequenceDiagram
    participant S as spec-implement.js scheduler
    participant P as plan agent (ck plan)
    participant T as task agent
    participant R as review-wave / review agent
    participant L as land agent

    S->>P: call('plan', trigger) [start | landing | idle-only]
    P-->>S: done, remaining, deps, ready
    alt consistent (planConsistent)
        S->>S: applyPlanView; lastGood = plan.ready; schedule from readyTasks (no fallback)
    else inconsistent / unusable
        S->>P: retry once with same inputs
        alt retry consistent
            S->>S: adopt; no fallback counted (1.4)
        else retry fails too
            S->>S: fallbackReady(scriptReady | lastGood)
            alt deterministic list exists
                S->>S: degraded=true; planFallbacks+=entry; schedule fallback (1.2, 1.3)
            else neither layer has data
                S->>S: stopLaunching (PLAN_FAILED); running tasks finish/land (2.3)
            end
        end
    end
    S->>T: start ready tasks (freed slot + deterministic ready: no plan agent) (3.2, 3.3)
    T-->>S: RESULT (busy/blocked handled as for plan-scheduled tasks) (2.6)
    S->>R: risky task: its own review + revise loop before it queues (4.2)
    S->>R: landing batch: one review-wave over its unreviewed non-risky tasks, before landing (4.1, 4.3)
    R-->>S: verdict; flagged tasks leave the batch -> revise + re-review, then land (4.4, 4.6); approve -> covered approved (4.5)
    S->>L: land the approved tasks of the batch
    L-->>S: MERGED
    S->>S: overheadShare(byRole ms) -> overheadWarning (warn-only) (5.4)
```

Fixture injection (test-plan contract): the shim forces the first two plan
results of the fixture run to return `ready` contradicting `deps`; the scheduler
must retry once (1.1), fall back (1.2), complete and report degraded (1.3, 6.1).

## Error Handling

- **1.1 Inconsistent/unusable plan.** Attempt fails (`planConsistent` false, or no
  usable result); exactly one retry with the same inputs; `planFailures++` per
  failed attempt.
- **1.2 Retry also fails, deterministic data exists.** No halt: `fallbackReady`
  supplies the list; run continues; fallback entry recorded (1.3). Task agents
  still own per-task BUSY/blocked handling (2.6).
- **2.3 Neither layer has data (no `deps` arg AND no last good plan).** Return
  null from `runPlan` → existing halt path (`stopLaunching`, `PLAN_FAILED`);
  already-running tasks finish and land; start-plan failure keeps the existing
  refused/halted return.
- **2.5 Unknown dependency.** `readyTasks` never meets a dep missing from `deps`
  keys, so the task is not ready and never starts on a guess; `planConsistent`
  filters such tasks from `plan.ready` before comparing, so this is not a planning
  failure. Report shows the task blocked (from git).
- **1.5 / 2.x Running/merged tasks at fallback time.** `fallbackReady` subtracts
  running, failed, parked and `mergedInRun`/`done`; their records are untouched.
- **2.6 Fallback-scheduled task reports busy/blocked.** Identical handling to a
  plan-scheduled report (`worktreeOutcome`, guard ladder, park/fail paths);
  fallback replaces only the ready list, never task-level checks.
- **3.4 Idle re-read with nothing ready.** One `idle` plan while `idleArmed`;
  disarm after; no re-trigger until a landing completes or a task record arrives.
- **4.4 Batched `changes`.** Only flagged tasks revise, outside the landing
  queue, and land after their re-review; unflagged tasks of the same batch land at
  once. Batched `approve` (4.5) approves all covered. Findings that survive the
  revisions fail the task unmerged (4.6).
- **5.4 Overhead above threshold.** `overhead.warned = true` in result + report
  warning line; never halts, fails or retries (NFR-5).
- **Cycles / red target / guards / protected paths.** Unchanged from
  landing-speed (`applyLint`, bisection, guard ladder, `onFailure`); fallback and
  batching never bypass them.

## Security, Performance, Migration

**Security.** No new commands, paths or permissions. The fallback computes from
in-script data only (no `ck plan` in-script, satisfying the no-exec constraint);
reviews read diffs via the existing review prompt; risky tasks keep the
risky-reviewer settings. Protected-path, sandbox, land-lock and gate/guard/
escalation semantics are unchanged.

**Performance.** Plan agents drop from ~1/freed-slot to
`1 + landings + idle-plans` (idle gated by `idleArmed`); reviews drop from one
per task to ≤ W + R per NFR-4 (one per merged wave with non-risky tasks, plus one
per risky task; counted over role-`reviewer` labels so each `review-wave` agent
counts as one review). The equity re-run must show a lower plan/review/land share with
no planning halt (6.3, NFR-2); the fixture gates the fallback in CI (NFR-1).

**Migration.** Additive only. Old workflow args (no `deps`) run the dep-less
path as today. Old stats records lack `planFallbacks`/`overhead` and render as
`not recorded` with every existing figure unchanged (5.5, 5.6); `SCHEMA_LOCAL`
stays `ck-run-local/1`. Vendored copies refresh via `uv run ck vendor`.

### OpenCode vendored-path note (requirements author's concern 1)

`workflow-tool.ts` passes `args` through unchanged, so `deps` survives on the
OpenCode path whenever the skill passes it (the normal case → `source: 'deps'`
fallback works identically). If hand-built args omit `deps` (or a future
validation drops it), `SCRIPT_READINESS` is false on **both** harnesses and the
fallback behaves dep-less: last-good ready (2.2) or halt (2.3). The vendored
`vendor/workflows/spec-implement.js` must be refreshed with the fallback code —
a stale vendored copy is the only way the two harnesses diverge, caught by the
existing dogfood byte-identity test.

## Requirements Traceability

| Criterion | Design element |
|---|---|
| 1.1 | `planConsistent` check + `runPlan` exactly-one-retry; `planFailures++` |
| 1.2 | `fallbackReady` (`deps` → `last-good`); schedule fallback, no halt |
| 1.3 | `degraded: true` + `planFallbacks[]` entry (planStep, trigger, source, ready); report section |
| 1.4 | Consistent first/retry plan adopted via `applyPlanView`; no fallback counted; `lastGood` updated |
| 1.5 | `fallbackReady` subtracts running/failed/parked/merged/done; records untouched |
| 2.1 | `fallbackReady` `deps` layer: script-side `readyTasks(deps arg, done ∪ running/failed/merged)` |
| 2.2 | `fallbackReady` `last-good` layer from `lastGood.ready` minus excluded/done |
| 2.3 | No deterministic data (!hasDeps, no adopted plan with usable ready) → runPlan null ⟺ dep-less AND (no adopted plan with usable ready) → PLAN_FAILED halt; running tasks finish/land. Contentful lastGood with an empty window → empty synthetic view + entry {ready: [], source: null} + degraded, waits for landings/future plans. |
| 2.4 | Both layers already in ck-plan order (`readyTasks` chain-then-numeric; stored `lastGood.ready`) |
| 2.5 | `readyTasks` met = key-of-`deps` AND in `done`; `planConsistent` filters dangling-dep tasks |
| 2.6 | `worktreeOutcome`/guard/park/fail paths shared for fallback- and plan-scheduled tasks |
| 3.1 | `planDecision`: `start`, `landing`, `idle` (gated) triggers only |
| 3.2 | `schedule()` starts from `readyTasks` on freed slot without a plan agent |
| 3.3 | `planDecision` returns `none` for `freed`/`finished` when `SCRIPT_READINESS` |
| 3.4 | `idleArmed` disarm; re-arm only on landing completion or task record |
| 3.5 | `applyPlanView` replaces remaining/deps/lint; `done = plan.done ∪ mergedInRun` |
| 3.6 | `SCRIPT_READINESS` false → plan on `freed`/`finished` (landing-speed fallback path) |
| 4.1 | `reviewGroups` batched set → one `review-wave <wave> tasks ...` agent over the combined diff; `roles.py` maps that label to `reviewer` |
| 4.2 | `reviewGroups` risky (`safety`) → always individual review |
| 4.3 | Batch with no unreviewed non-risky task (risky only, or re-queued) → no combined review |
| 4.4 | Batched `changes` → flagged tasks leave the batch, revise and re-review, then land; rest land unaffected |
| 4.5 | Batched `approve` → approval recorded for every covered task |
| 4.6 | Blocking findings after the revisions → task fails, never merges |
| 5.1 | Result `degraded` + `planFallbacks[]`; report fallback section |
| 5.2 | `show.run_breakdown`/`render_run`: `plan_fallbacks` + overhead share lines |
| 5.3 | `show.run_json`: `planFallbacks` + `overhead` with identical values |
| 5.4 | `overheadWarning` (threshold 0.5) sets `warned`; never halts/fails/retries |
| 5.5 | `.get` reads; missing fields render `not recorded`/`unknown`/`null` |
| 5.6 | Additive fields; `SCHEMA_LOCAL` unchanged; old records readable |
| 6.1 | Fixture spec + forced-inconsistent plan shim → retry, fallback, completion, degraded |
| 6.2 | Healthy fixture run → no fallback, no degraded mark |
| 6.3 | Equity re-run: lower plan/review/land share (`overhead.share`), no planning halt |
