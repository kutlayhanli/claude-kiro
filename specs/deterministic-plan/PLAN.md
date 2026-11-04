# Plan: Deterministic Plan

**Status:** Decided
**Updated:** 2026-11-01
**Evolves:** specs/landing-speed

## Problem

The equity E2E run (`wf-20261031145547-e45f5e`, 55/87 merged, 81.7 min wall) showed orchestration, not task work, dominating cost and wall time: 53 plan agents (26.0 min), 62 reviewers (96.3 min), 49 land agents (21.2 min), 135 min idle-before-merge. Worse, a single planner mistake was fatal: the planning agent returned a ready list contradicting its dependencies twice, the workflow halted, and 28 tasks never started — even though `ck plan --json` held a valid ready set the whole time. For OpenCode runs this is the top bottleneck (ck issues #5, #11).

How we'll know it worked: a fixture spec with an injected bad ready list runs to completion off the deterministic plan, and an equity re-run shows lower plan/review/land agent share with no planning halt.

## Scope

**In:**
- Planning-agent fallback: validate ready against dependencies, retry once, then schedule deterministically and mark the run degraded (issue #5).
- Overhead reduction: fewer plan agents (no re-read after every idle slot), batched reviews/landings by wave (issue #11).
- Overhead visibility: plan/review/landing share and fallback counts in `ck stats` and the run report.
- Fixture spec with a forced planning failure, asserting the run continues.

**Out:**
- The `#6` verify-cycle recheck and `#8` blocked:test handoff (separate spec).
- `/spec:create` oracle gaps from the equity spec issues (separate follow-up).
- Loading rates from the models.dev catalog (leftover of #9; separate change).
- Changing gate, guard, or escalation semantics.

## Constraints

- The target branch is never red; red batches are still bisected (inherited from landing-speed).
- Protected paths stay protected; sandbox behaviour unchanged.
- Workflow scripts cannot exec: every `ck` call goes through an agent (`opencode/src/runner.mjs` supplies only `agent`/`phase`/`log` globals). Determinism must come from data already in the script (`deps`, last good plan), not from running `ck plan` in-script.
- Both harnesses: changes to `plugin/workflows/spec-implement.js` must also reach the OpenCode path (vendored copy + `workflow` tool arg validation, now in `opencode/src/workflow-tool.ts:validateWorkflowArgs`).

## Decisions

### D1: Relationship to landing-speed
**Choice:** New spec, `Evolves: specs/landing-speed`.
**Why:** Landing-speed decided landing-batch mechanics and measured the baseline; this spec reuses both instead of re-deciding them. The new ground is the plan fallback and review batching.
**Decided by:** user

### D2: Planner fallback shape
**Choice:** Retry the plan agent once, then fall back to the deterministic ready list and mark the run degraded.
**Why:** Matches issue #5; preserves today's self-healing for transient agent errors while bounding the damage of a repeated inconsistency to one retry.
**Rejected:** deterministic-immediately (loses the cheap recovery); remove-plan-agents-entirely (bigger blast radius, less evidence).
**Decided by:** user

### D3: Review batching scope
**Choice:** In scope for this spec, together with the plan fallback.
**Why:** Review agents are the largest overhead slice (96.3 of 164 agent-min); landing alone won't move the headline number.
**Decided by:** user

### D4: Success signal
**Choice:** Both: fixture spec with injected planning failure in CI, plus one equity re-run as the headline number.
**Why:** The fixture gives a fast deterministic gate; the re-run proves it on the workload that produced the issue.
**Decided by:** user

## Delegated to Spec
- Exact degraded/report field names and stats output shape, within D2.
- Batching parameters (batch size, wave grouping, risky-task exclusion), within D3 and landing-speed's batching decisions.
- Which plan triggers survive (freed/landing/start) and the idle re-read policy.
- Fixture spec design (size, injection mechanism).

## Risks
- Deterministic fallback schedules a task the agent world can't see (stale tasks.md vs worktree reality) -> mitigation: task agents still report BUSY/blocked per task; fallback only replaces the ready list, not task-level checks.
- Batched reviews miss a finding a per-task review would catch -> mitigation: risky/safety tasks always reviewed individually; adversarial review of the spec must attack the batching rule.
- Equity re-run costs ~$7 and 80 min -> accepted (one run, headline number).

## Open Questions
(none - all resolved)

## Resolved
### D5: Deterministic source for the fallback
**Choice:** Layered: script-local `readyTasks` from the `deps` arg when present; else the last good plan's `fallbackReady`; halt only if neither exists (start-plan failure with hand-built args lacking `deps`).
**Why:** Script-local computation is always fresh and needs no agent; last-good-plan covers runs launched without `deps`; halting remains only where no deterministic data exists at all.
**Rejected:** script-local-only (can't save dep-less runs); last-good-only (stale, can't save start failures).
**Decided by:** user

### D6: Degraded marking
**Choice:** Full: result gets `degraded: true` plus a fallback count; the report gains a section; `ck stats` shows `plan_fallbacks` next to the overhead share.
**Why:** The equity comparison (D4) needs fallback counts queryable in stats, and the run reader needs the degraded flag in the report.
**Rejected:** report-only (less queryable); flag-only (can't distinguish one fallback from fifty).
**Decided by:** user

### D8: Overhead budget
**Choice:** Warn-only: the report flags overhead share above a threshold but never halts a run; the threshold number is delegated to the spec.
**Why:** Failing runs on a new metric risks halting good work; a warning establishes the baseline first.
**Rejected:** configurable warn/fail (premature strictness); report-only (no judgment at all).
**Decided by:** user

### D7: Batch-review eligibility
**Choice:** One reviewer reads each merged wave's combined diff for all non-risky tasks; safety/risky tasks are always reviewed individually.
**Why:** Largest saving against the 62-reviewer overhead; risk concentrates in risky tasks, which keep individual review.
**Rejected:** test-track-only batching (leaves most reviews in place); per-landing-batch review (more reviewers than wave batching).
**Decided by:** user

### D9: Batched review runs before the batch lands (fix after PR #13 review)
**Choice:** One `review-wave` agent per landing batch, run before the land step over the batch's not-yet-reviewed non-risky tasks. Safety tasks keep the per-task review before landing. A flagged task leaves the batch unmerged, is revised and re-reviewed alone, and lands only once approved; findings that survive fail it unmerged, as before this spec.
**Why:** The first implementation reviewed waves after they merged. A revision then sat on the task branch and never reached the target (nothing landed it again), and a task with surviving blocking findings, risky ones included, stayed merged. That changed gate semantics, which Scope lists as out. D7's saving (one reviewer per batch instead of one per task) is kept.
**Rejected:** post-merge review plus a re-land of revised branches (blocked code still reaches the target between merge and revision); reverting to per-task review (drops D7's saving).
**Decided by:** Claude, delegated by the user ("fix the PR and get it ready to merge", 2026-11-04)

## Research Notes
### Codebase
- `plugin/workflows/spec-implement.js:774-833` - `runPlan` (2 attempts, null on failure) and `requestPlan` (null → `stopLaunching`, `PLAN_FAILED` halt); line 798 keeps `fallbackReady` from the last good plan.
- `plugin/workflows/spec-implement.js:762-768` - `planContext` already computes script-side `ready` via `readyTasks()` when `SCRIPT_READINESS` (deps arg present); lines 475-532 visit tasks in `ck plan` order so ties match.
- `plugin/workflows/spec-implement.js:344-347` - `planPrompt`: the plan agent runs `ck plan --json` and copies fields back; inconsistency is a transcription/model error, not a data error.
- `plugin/workflows/spec-implement.js:67` - `MERGE_BATCH = 4`; landings already batch, reviews do not (62 reviewers for 49 landings in the equity run).
- `opencode/src/runner.mjs:1-80` - workflow globals are `agent`/`phase`/`log` only; no exec in-script.
- `opencode/src/workflow-tool.ts:207` - `validateWorkflowArgs` (new, #10 fix): spec-implement requires `spec`+`root`.
- `src/claude_kiro/cli/worktree.py:148` - `ck plan` CLI; `ck plan --json` yields `done/remaining/deps/ready/cycles/verifyCycles`.
- `src/claude_kiro/bench.py:111` - `rates()`/`price_usage()`; missing cacheRead rate now prices as unknown, not $0 (#9 remainder).
- `specs/landing-speed/` - prior decisions on landing batches, red-batch bisection, wait measurements.

### Sources
- ck issues #5 (deterministic fallback) and #11 (overhead) - problem statements and equity numbers.
- claude-kiro #12 - equity E2E run report (`wf-20261031145547-e45f5e`).
