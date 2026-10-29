# Plan: Landing Speed

**Created:** 2026-10-29
**Status:** Decisions Made

## Problem Statement

Whole-spec `/spec:implement` runs feel slow. Run analytics (`ck stats show wf_8bca74f5-ecc`, the run-analytics spec: 41 tasks, 73 min wall) shows finished tasks waited a **median 7.1 min** (total 235.6 min) between the implementer finishing and the merge. We want that wait, and the cost of the mechanical plan/land/gate agents, down without weakening any safety property: the target branch is never red, red batches are bisected, protected paths stay protected, sandbox behaviour is unchanged, and the report still comes from git (`ck worktree report`).

## What the Data Says (measured, not assumed)

Split of the 7.1 min median wait per task (local record + Claude Code run file of `wf_8bca74f5-ecc`):

| Component | Median per task | Total |
|---|---|---|
| Review agent running | 2.3 min | 104 min |
| Revise agents running | 0 min | 10 min |
| Everything else (queueing, landing) | 4.4 min | 133 min |

- **The landing gate is not the bottleneck for this spec.** `ck worktree land --gate` took ~0 min per landing; land agents ran a median 0.2 min. With `verify_mode: "auto"` and a test track, the task-scoped gate skips the full project `verify` while the spec is incomplete (`src/claude_kiro/gate.py:324`, `:362`). Specs without a test track, or with `verify_mode: "full"`, do run the full suite on every batch.
- **Agents queue for a runtime slot.** Peak concurrency was **14 agents**; time from `queuedAt` to `startedAt`:

  | Agent kind | n | Median wait | p90 | Total |
  |---|---|---|---|---|
  | review | 62 | 77 s | 154 s | 77 min |
  | revise | 21 | 61 s | 101 s | 18 min |
  | task | 42 | 0 s | 102 s | 26 min |
  | plan | 62 | 0 s | 2 s | 10 min |
  | land | 17 | 0 s | 74 s | 5 min |

  Finished tasks mostly wait for a **reviewer slot**, while slots go to new task starts and 62 short planning agents.
- **Mechanical agents cost money.** Plan 62 agents / $5.37, land 17 / $2.11, gate 4 / $0.48 in this run; across 83 collected spec-implement runs, `plan` is ~20% of spend (older runs ran it on Opus).
- **Batches stay small** (mean 2.0, mostly 1) because landing is fast and only one runs at a time (`plugin/workflows/spec-implement.js:544`), not because of a gate cost; a batch-fill delay would only add latency.

## Codebase Facts

- Workflow scripts can only call `agent()`; no commands, no filesystem (`plugin/workflows/spec-implement.js:465`, `spec-create.js:575`).
- Plan agent runs after every slot release and every finished task record (`spec-implement.js:785-806`): `ck plan --json --exclude …` then `ck worktree create … --install`. The script does **not** receive the dependency graph (`args`: wave/titles/tracks/risk/complexity only), though `ck plan` already emits `deps`.
- What only the plan step learns: Done status on the target (incl. tasks finished before the run or committed directly), mid-run tasks.md edits, lint cycles, stale-claim takeover/BUSY, and the worktree creation itself.
- One landing at a time per target (`landingBusy`, `spec-implement.js:544`); the `ck worktree land` lock is a non-blocking flock that fails a second lander (`src/claude_kiro/worktrees.py:405-427`).
- Full gates every `fullGateEvery` merges run while `landingBusy` is set, blocking later landings (`spec-implement.js:585-598`).
- `ck stats` records per-agent `ms` but **not** queue wait (`queuedAt`→`startedAt`), so this bottleneck is invisible in `ck stats show` today.

## Research Summary

### Sources Consulted
- [Trunk merge queue batching](https://docs.trunk.io/merge-queue/optimizations/batching) - defaults: target batch 4, max wait 5 min; longer waits cut CI cost but raise time-in-queue.
- [Mergify: batching](https://mergify.com/learn/merge-queue/batching) and [bisection statistics](https://docs.mergify.com/changelog/2026-03-12-batch-bisection-statistics-api) - batch size should follow failure rate; frequent/deep bisections mean batches are too big.
- [Aviator batching](https://docs.aviator.co/mergequeue/concepts/batching) - extra pipelines fall from 10 (size 1) to 2 (size 5) for 10 PRs.
- [Graphite: merge queue batching](https://graphite.com/blog/merge-queue-batching) - one green batch run validates many changes; attribution only matters on red, when bisection runs.
- [Accelerating CI with parallel batch testing (arXiv 2308.13129)](https://arxiv.org/pdf/2308.13129.pdf) - batching plus parallel bisection keeps throughput when failures are rare.
- [DEV: eval gate 22 min, queue 3 h](https://dev.to/ethanwritesai/our-eval-gate-runs-22-minutes-the-queue-behind-it-hit-three-hours-4iim) - two-tier checks: light per change, heavy once per batch; post-merge full run as safety net.
- [Impact is not invalidation (arXiv 2609.25130)](https://arxiv.org/pdf/2609.25130) - pytest-testmon recall 0.868, precision 0.415 on 151 flips: good accelerator, unsafe as the only guard.
- [pytest flaky docs](https://docs.pytest.org/en/9.0.x/explanation/flaky.html) and [Mergify: xdist ordering races](https://mergify.com/blog/pytest-xdist-ordering-races) - xdist speeds suites but exposes shared-state/order bugs; per-project opt-in.

### Best Practices Found
- **Measure the queue, not only the work** - time-in-queue is the metric merge queues optimise (Trunk, Mergify).
- **Finish before starting** - prioritise work that unblocks merges (review, landing) over starting new work when capacity is scarce (queue/WIP-limit practice behind merge queues).
- **Heavy checks once per batch, full run as backstop** - per-change scoped checks plus periodic/post-merge full suite (DEV, Graphite).
- **Keep bisection; tune batch size by failure rate** (Mergify, Mathlib simulation).

### Patterns to Avoid
- **Batch-fill delays when the gate is cheap** - adds latency for no saving (this run: gate ~0 min).
- **Test-impact selection as the only guard** - ~13% of affected tests missed (arXiv 2609.25130).
- **Turning on xdist globally** - flakiness from shared state; a project-level choice, not ck's.

## Solution Options

### Option A: Fewer mechanical agents (script-side readiness)

**Description:** Pass `deps` from `ck plan --json` into the workflow args. The script computes ready tasks itself from its own merge records and starts them through a cheaper "create worktrees" step only when something is ready, instead of a full plan agent after every task. A real plan agent still runs at start, after each landing (Done status on target changes), and when a ready-set is empty but tasks remain (to catch tasks.md edits or external commits). Optionally fold "create worktrees" into the task agent's own first step.

**Pros:** ~45 fewer agents per 40-task run (~$4-5, historically ~20% of spend); frees runtime slots for reviewers; removes the "planning step failed" halt class.
**Cons:** Mid-run tasks.md edits are picked up later (at the next landing or idle point), not after every task; the script's readiness must match `ck plan` exactly (needs a contract test).
**Complexity:** Medium
**Precedent:** handoff-2026-10-12 #5 ("planning is mechanical, make it a plain step").

---

### Option B: Finish-first scheduling (slot reservation)

**Description:** The workflow keeps headroom in the runtime's agent slots for work that unblocks merges. New task starts are throttled when reviews, revisions or landings are waiting: e.g. `maxConcurrent` for implementers defaults to (observed runtime cap − reserve), and a new task launch is deferred while any review/land agent is queued. Reviews and landings never wait behind new implementer starts.

**Pros:** Directly targets the measured 77 s median review wait (77 min total); no change to safety logic; small code change in the scheduler.
**Cons:** Slightly lower implementer parallelism early in a run; the runtime cap is not documented (observed 14), so the reserve must be configurable.
**Complexity:** Low-Medium
**Precedent:** merge-queue/WIP-limit practice ("stop starting, start finishing").

---

### Option C: Tiered landing gate for full-verify specs

**Description:** New `specs/ck.json` option (e.g. `land_verify: "tasks"`) for specs where the task gate runs the full project `verify` every batch (no test track, or `verify_mode: "full"`): the batch gate runs only the batch's task Verify commands; the full `verify` runs at `fullGateEvery` and the final gate (both already exist). Red full gates already bisect/fix. Optional: document `pytest -n auto` as a project-level speed-up.

**Pros:** Large saving where it applies (~4 min × landings); keeps a full-suite backstop; reuses existing full-gate machinery.
**Cons:** No gain for specs like run-analytics (already skipped); a cross-task break can merge and be caught up to N merges later (fixer repairs, target can be red between full gates: **weakens "target never red"** unless the full gate gates the next landing).
**Complexity:** Medium
**Precedent:** two-tier CI, post-merge full runs (DEV, Graphite).

---

### Option D: Measure first (queue-wait metrics in `ck stats`)

**Description:** Add per-agent `queueMs` (from `queuedAt`/`startedAt`) and per-task "review wait" / "landing wait" splits to the collector and `ck stats show`, so before/after comparisons of A-C are possible and future bottlenecks show up.

**Pros:** Cheap; makes this whole analysis repeatable; needed to verify A-C.
**Cons:** No speed-up by itself.
**Complexity:** Low
**Precedent:** merge queues report time-in-queue as the primary metric.

### Option E: Run tests in parallel

**Description:** Three parts.
- **E1, this repo's suite under pytest-xdist.** Add `pytest-xdist` as a dev dependency and change `specs/ck.json` `verify` to `uv run pytest -q -n auto`. Measured on this machine (16 cores): **1,173 passed in 52 s vs ~253 s serial (4.8x)**, stable over two runs. One prerequisite fixed: `tests/test_worktree_cli.py` parametrized on `os.getpid()`, so xdist workers collected different test ids; the cases now have fixed ids (no behaviour change).
- **E2, ck caps workers when many gates run at once.** Up to 14 agents can run gates concurrently in separate worktrees; each `-n auto` would start one worker per core and oversubscribe the CPU. `ck gate` sets `PYTEST_XDIST_AUTO_NUM_WORKERS` (honoured by `-n auto`) from the core count and the number of gates running on the machine, unless the user set it.
- **E3, `ck doctor` / `ck init` hint.** For pytest projects whose `verify` has no `-n`, suggest xdist and the stable-id pitfall (collection mismatch error) with a one-line check (`pytest --collect-only` twice with different `PYTHONHASHSEED`).

**Pros:** Cuts every full-suite run (task gates, full gates, final gate, each fix re-check) ~4-5x; helps specs without a test track most, which do run the full suite on every landing.
**Cons:** Parallel runs expose shared-state or order-dependent tests (per-project); concurrent gates compete for cores (E2 mitigates); xdist is a pytest-only answer (other runners have their own flags).
**Complexity:** Low (E1, E3) to Medium (E2)
**Precedent:** standard pytest practice ([pytest-xdist docs](https://pytest-xdist.readthedocs.io/), [pytest flaky docs](https://docs.pytest.org/en/9.0.x/explanation/flaky.html)).

---

## Comparison Matrix

| Criteria | A: Fewer plan agents | B: Finish-first | C: Tiered gate | D: Queue metrics |
|---|---|---|---|---|
| Wait saved (this run) | some (slots freed) | most (review wait ~77 s/task) | none here; large on full-verify specs | none |
| Cost saved | ~$4-5/run | small | gate CPU only | none |
| Complexity | Medium | Low-Medium | Medium | Low |
| Safety risk | Low (contract-tested readiness) | None | Medium (red-between-full-gates) | None |
| Touches | spec-implement.js, `ck plan`, skill args | spec-implement.js scheduler | gate.py, worktrees.py, ck.json | collect.py, show.py |

## Complexity Assessment

**Overall: Medium.** Drivers: concurrency in the workflow scheduler (slots, queues, events), a readiness contract that must match `ck plan`, the undocumented runtime agent cap, and keeping every safety property (never red, bisection, protected paths, git-based report). No migration; all behaviour behind defaults or config.

## Key Decisions

1. **Scope for v1**
   - B + D (lowest risk, targets the measured bottleneck, measurable).
   - A + B + D (also the cost cut).
   - All four.
   - Recommendation: **A + B + D**; C as a separate opt-in later (it trades safety for speed on specs that do not have this problem today).

2. **Slot reserve for B**
   - Fixed reserve (e.g. keep 3 slots free for review/land) with `run.reserve_slots` setting.
   - Adaptive: never start a new task while a review/land/revise agent is queued.
   - Recommendation: **adaptive, with a configurable cap**; it needs no knowledge of the runtime limit.

3. **When the plan agent still runs under A**
   - Start, after each landing, and when nothing is ready but tasks remain.
   - Plus a timer (e.g. every 10 min) to pick up tasks.md edits.
   - Recommendation: **start + after each landing + idle**; landings are already frequent, so edits are seen within minutes.

4. **Where worktree creation happens under A**
   - Keep a small "create worktrees" agent (one command).
   - The task agent creates/claims its own worktree as its first step.
   - Recommendation: **task agent creates its own** (it already claims it; removes another agent per task).

## Open Questions

- [ ] Is the observed 14-agent peak a fixed Workflow runtime cap? (Determines whether B's reserve or a lower `maxConcurrent` default matters more.)
- [x] Measured with the next real runs via `ck stats` (D); `ck bench` only if results are unclear (D5).
- [x] C is out of scope for v1 (D1).

## Recommended Direction

**Approach:** D first, then B, then A, plus parallel tests (E), in one spec; C deferred.

**Rationale:**
- The measured bottleneck is reviewers waiting for an agent slot, not the gate: B removes it directly with no safety cost.
- A frees more slots and cuts ~20% of historical spend on a step that is one command.
- D makes the result provable with `ck stats` and keeps future bottlenecks visible.

**Key risks and mitigations:**
- Script readiness diverges from `ck plan` -> contract test: script readiness equals `ck plan --json` ready set for generated graphs; plan agent after every landing as a resync.
- Throttling starts slows a run with few reviews -> adaptive rule only defers while a review/land agent is actually queued.
- Mid-run tasks.md edits picked up later -> plan agent after each landing and when idle; documented in the skill.
- Undocumented runtime cap changes -> no hard-coded cap; B is adaptive.

## Decisions Log

| # | Decision | Choice | Rationale | Date |
|---|----------|--------|-----------|------|
| 1 | Scope for v1 | A + B + D (plus E, see D6): script-side readiness with fewer plan agents, finish-first scheduling, queue-wait metrics in `ck stats`; C (tiered landing gate) deferred | Targets the measured bottleneck (review slot wait) and the plan-agent cost with no safety trade-off; C can let the target go red between full gates | 2026-10-29 |
| 2 | Finish-first rule (B) | Adaptive: never start a new task while a review, revise or land agent is queued; configurable off; refined by D9 | Needs no knowledge of the undocumented runtime agent cap | 2026-10-29 |
| 3 | When the plan agent still runs (A) | At start, after each landing, and when nothing is ready but tasks remain | Landings are frequent, so tasks.md edits and Done status are seen within minutes | 2026-10-29 |
| 4 | Worktree creation (A) | The task agent creates and claims its own worktree as its first step | Removes another mechanical agent per task; the task agent already claims it | 2026-10-29 |
| 5 | How to measure | Next real runs via `ck stats` with the new queue metrics; `ck bench` only if results are unclear | Real runs are free to collect; D makes the before/after visible | 2026-10-29 (default, not contested) |
| 6 | Parallel tests (E) in scope | E1 + E2 + E3: this repo's verify runs `pytest -n auto`; ck caps xdist workers across concurrent gates; doctor/init suggest xdist for pytest projects | User asked for parallel tests; measured 4.8x on the full suite with all 1,173 tests passing | 2026-10-29 |
| 7 | Worker cap (E2) | `ck gate` sets `PYTEST_XDIST_AUTO_NUM_WORKERS = max(1, cores // gates running)` unless already set | Avoids 14 concurrent gates each starting 16 workers | 2026-10-29 (default, not contested) |
| 8 | Stable test ids | Fix the one pid-parametrized test now (fixed ids); E3 checks other projects for the same pitfall | It was the only collection mismatch; behaviour unchanged | 2026-10-29 (default, not contested) |
| 9 | Meaning of "queued" (B) | Documented runtime cap min(16, CPUs-2) via runtime_agent_slots, CK_AGENT_SLOTS override; agents beyond the first N in request order are queued | Script cannot observe the queue; documented cap matches the observed 14 on 16 cores; refines D2 | 2026-10-29 |
| 10 | Saturation (B) | Cap new task starts at free slots | Reviews never queue behind task starts; this is when the 77 s waits happened | 2026-10-29 |
| 11 | Idle plan timing (A) | Idle plan may follow the start or a landing plan; only a repeated idle plan waits for new progress | Matches 1.4(c)/1.5; same plan-count bound | 2026-10-29 |
