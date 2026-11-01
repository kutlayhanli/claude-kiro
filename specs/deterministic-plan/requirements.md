# Feature: Deterministic Plan

## Overview

Whole-spec implement runs halt when the planning agent returns a ready list that contradicts its own dependency graph, even though a correct ready set is already known. Plan, review and landing agents also dominate run cost and wall time. This feature makes planning deterministic-first: an inconsistent plan is retried once, then the run continues from a deterministically computed ready list and is marked degraded instead of halting. It cuts mechanical-agent overhead by running fewer plan agents and by reviewing merged work in waves, while keeping every safety property from the landing-speed spec. Overhead becomes visible in run statistics and the run report, with a warn-only budget.

Decision record: [PLAN.md](PLAN.md) (Decisions D1–D8 are binding). This spec evolves `specs/landing-speed` and reuses its landing, bisection and wait-measurement decisions.

## User Stories

### Story 1: Survive an inconsistent plan without halting

**As a** developer running a whole-spec implement **I want** the run to continue from a deterministic ready list when the planning agent contradicts itself **So that** one bad plan answer does not abandon dozens of unstarted tasks.

**Acceptance Criteria:**

- 1.1 WHEN a plan agent returns a ready list that does not follow from the dependency graph it returned alongside it, THE SYSTEM SHALL retry the plan agent exactly once with the same inputs.
- 1.2 WHEN the retry also returns an inconsistent or unusable plan, THE SYSTEM SHALL schedule further tasks from the deterministic ready list and continue the run instead of halting task launches.
- 1.3 WHEN the run continues from the deterministic ready list per 1.2, THE SYSTEM SHALL mark the run degraded, count the fallback occurrence, and record which plan attempt triggered it.
- 1.4 WHEN a plan agent returns a consistent usable plan on the first attempt or the retry, THE SYSTEM SHALL adopt that plan's view and SHALL NOT count a fallback.
- 1.5 WHEN tasks already running or already merged exist at fallback time, THE SYSTEM SHALL exclude them from the deterministic ready list and leave their records unchanged.

### Story 2: Deterministic ready source with a last resort

**As a** developer **I want** the fallback ready list to come from the freshest deterministic data available **So that** runs launched with or without dependency data both survive when possible, and halt only when no deterministic data exists.

**Acceptance Criteria:**

- 2.1 WHEN dependency data was supplied with the run inputs, THE SYSTEM SHALL compute the fallback ready list from that data combined with the run's own record of Done, running, failed and merged tasks.
- 2.2 WHEN no dependency data was supplied with the run inputs but a previous plan in the same run returned a consistent ready list, THE SYSTEM SHALL use that last good ready list as the fallback.
- 2.3 WHEN neither 2.1 nor 2.2 can produce a ready list (run started without dependency data and no good plan has returned yet), THE SYSTEM SHALL halt new task launches and report the planning failure as today, while already-running tasks finish and land.
- 2.4 WHEN the deterministic ready list is used, THE SYSTEM SHALL order it the same way the plan command orders ready tasks (longest remaining dependency chain first, then task number).
- 2.5 WHEN a task's dependency is unknown to the deterministic data, THE SYSTEM SHALL treat that task as not ready and never start it on a guess.
- 2.6 WHEN a task agent reports that a fallback-scheduled task cannot proceed (busy worktree, missing worktree, blocked), THE SYSTEM SHALL handle that report exactly as it handles the same report for a plan-scheduled task.

### Story 3: Fewer plan agents

**As a** developer **I want** plan agents to run only where they add new information **So that** mechanical re-reads stop consuming runtime slots and cost.

**Acceptance Criteria:**

- 3.1 THE SYSTEM SHALL run a plan agent at the start of the run, after each landing attempt completes, and when tasks remain but none are ready and no task is running.
- 3.2 WHEN a task finishes and frees an implementing slot while other tasks are deterministically ready, THE SYSTEM SHALL start the next ready task(s) without running a plan agent.
- 3.3 THE SYSTEM SHALL NOT run a plan agent after every freed implementing slot when the deterministic ready list already determines what starts next.
- 3.4 WHEN a re-read triggered by 3.1 finds nothing ready and no task is running, THE SYSTEM SHALL NOT trigger another such re-read until a landing completes or a task record arrives.
- 3.5 WHEN a consistent plan agent returns, THE SYSTEM SHALL replace its view of remaining tasks, dependencies and lint results with the plan's, and set Done to the plan's Done tasks plus every task merged in this run.
- 3.6 WHEN dependency data was not supplied with the run inputs, THE SYSTEM SHALL run a plan agent wherever it would otherwise use its own readiness computation, so the run still completes correctly.

### Story 4: Wave-batched reviews with risky tasks kept individual

**As a** developer **I want** non-risky tasks reviewed together per merged wave **So that** review-agent cost and queue wait fall without weakening review of risky work.

**Acceptance Criteria:**

- 4.1 WHEN a wave of tasks merges, THE SYSTEM SHALL review all non-risky tasks of that wave together in one review covering their combined changes.
- 4.2 WHEN a task is marked risky or safety-relevant, THE SYSTEM SHALL always review it individually, even when it merged as part of a larger wave.
- 4.3 WHEN a wave contains only risky tasks, THE SYSTEM SHALL review each of them individually and SHALL NOT run an additional combined review for that wave.
- 4.4 WHEN a batched review requests changes on some tasks of the wave, THE SYSTEM SHALL route each flagged task to revision individually and leave unflagged tasks of the same wave unaffected.
- 4.5 WHEN a batched review approves the wave, THE SYSTEM SHALL record approval for every task covered by that review.
- 4.6 WHEN no wave merged since the last review, THE SYSTEM SHALL NOT run an empty batched review.

### Story 5: Overhead visibility with a warn-only budget

**As a** developer tuning runs **I want** plan/review/landing overhead and fallback counts visible per run and across runs **So that** the saving from this change is provable and regressions show up.

**Acceptance Criteria:**

- 5.1 WHEN a run uses a deterministic fallback, THE SYSTEM SHALL include in the run report that the run is degraded, how many fallbacks occurred, and which plan attempts triggered them.
- 5.2 WHEN run statistics are shown for a run, THE SYSTEM SHALL show the count of deterministic fallbacks alongside the plan/review/landing agent share of agent time.
- 5.3 WHEN run statistics are requested as structured output, THE SYSTEM SHALL include the fallback counts and overhead shares with the same values the human-readable view shows.
- 5.4 WHEN a run's plan/review/landing agent share of agent time exceeds the published threshold, THE SYSTEM SHALL flag it as a warning in the report and SHALL NOT halt, fail or retry the run because of it.
- 5.5 WHEN a run has no fallback or overhead data (runs recorded before this change), THE SYSTEM SHALL show those sections as not recorded and still show every existing figure unchanged.
- 5.6 THE SYSTEM SHALL keep previously collected statistics records readable without re-collection.

### Story 6: Proof on a fixture and on a real run

**As a** maintainer **I want** a deterministic fixture with an injected planning failure plus one real-workload re-run **So that** the fallback is gated in CI and the headline saving is measured on the workload that motivated it.

**Acceptance Criteria:**

- 6.1 WHEN the fixture spec runs with a planning agent forced to return a ready list contradicting its dependencies, THE SYSTEM SHALL retry once, fall back deterministically, run to completion, and mark the run degraded.
- 6.2 WHEN the fixture spec runs with a healthy planning agent, THE SYSTEM SHALL complete without any fallback and without the degraded mark.
- 6.3 WHEN the equity workload is re-run after this change, THE SYSTEM SHALL report lower plan/review/landing agent share than the baseline run with no planning halt.

## Specification Heritage

**Evolves From:** `specs/landing-speed` (whole-spec implement scheduling: script-side readiness, finish-first scheduling, queue-wait metrics, parallel tests) and the `/spec:implement` whole-spec workflow.
**Preserves:** target branch never red; red batches still bisected; one landing at a time per target with existing batch windows; full gates and protected-path rules; sandbox behaviour; review, revision and escalation ladders; retries; `maxConcurrent`; `onFailure` semantics; gate, guard and escalation semantics unchanged; reports built from git; existing statistics figures and records.
**Supersedes:** "an inconsistent or twice-failed plan halts the run" and "one review agent per task" and "re-read the plan after every freed implementing slot".
**Evolution Reason:** the equity E2E run showed orchestration, not task work, dominating cost and wall time (53 plan agents, 62 reviewers, idle-before-merge 135 min), and a single planner inconsistency halted the run with 28 tasks unstarted while a valid ready set was available the whole time. Landing-speed measured and fixed landing batching and wait visibility; this spec reuses both and adds plan-failure survival plus review batching.

## Non-Functional Requirements

- **NFR-1 No halt on recoverable planner error:** the fixture with an injected inconsistent ready list runs to completion with at least one fallback recorded. Check: automated fixture test asserting completion plus degraded mark and fallback count ≥ 1.
- **NFR-2 Overhead falls on the headline workload:** the equity re-run reports a lower plan/review/landing agent share of agent time than the baseline run, with no planning halt. Check: manual comparison of statistics output for the two runs.
- **NFR-3 Fallback readiness matches the plan command:** the deterministic ready list equals the plan command's ready list (set and order) on the same dependency graph and Done set. Check: contract test over generated graphs reusing the landing-speed readiness oracle.
- **NFR-4 Review batching bound:** on a run with W merged waves containing at least one non-risky task and R risky tasks, review agents number at most W + R. Check: workflow test counting review-labelled agents.
- **NFR-5 Warn-only budget:** no run halts, fails or retries because the overhead share exceeded the threshold. Check: workflow test with overhead forced above the threshold completes with a warning and no stop reason naming overhead.

## Constraints

- The target branch is never red; red batches are still bisected.
- Protected paths stay protected; sandbox behaviour unchanged.
- Workflow scripts cannot execute commands directly: every external command goes through an agent. Determinism must come from data already in the script (run inputs, last good plan), not from running the plan command in-script.
- Both harnesses stay in sync: changes to the workflow script reach the OpenCode path, including its argument validation.
- Gate, guard and escalation semantics are unchanged.

## Out of Scope

- The verify-cycle recheck (ck issue #6).
- The blocked:test handoff (ck issue #8).
- `/spec:create` oracle gaps from the equity spec issues.
- Loading rates from the models.dev catalog.
- Changing gate, guard or escalation semantics.
- Tiered landing gates, fixed slot reserves, timer-based plan resyncs, batch-fill delays (decided in landing-speed).
- Failing or halting runs on overhead: the budget is warn-only.

## Assumptions

- The plan command keeps emitting dependency, chain, ready and remaining data with today's semantics.
- Task agents still report busy/blocked per task, so the fallback only replaces the ready list, not task-level checks.
- A risky/safety marking on tasks is available to the review scheduler.
- Run records carry enough per-agent timing data to compute plan/review/landing share; older records may lack it and are shown as not recorded.
- The equity re-run costs roughly one full workload run (time and spend) and is run once as the headline measurement.
