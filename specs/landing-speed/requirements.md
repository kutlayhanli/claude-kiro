# Feature: Landing Speed

## Overview

Whole-spec `/spec:implement` runs are slow because finished tasks wait between "implementer done" and "merged" (median 7.1 min per task on run `wf_8bca74f5-ecc`), mostly while review agents wait for a runtime agent slot that goes to new task starts and to short mechanical plan agents. This feature cuts that wait and the cost of mechanical agents without weakening any safety property:

- **A. Script-side readiness:** the workflow receives the dependency graph and decides which tasks are ready itself; the plan agent runs only at start, after each landing, and when nothing is ready but tasks remain; each task agent creates and claims its own worktree.
- **B. Finish-first scheduling:** no new task starts while a review, revise or land agent is queued, and no new task start may itself have to queue (adaptive, can be turned off).
- **D. Queue-wait metrics:** `ck stats` records and shows how long agents waited for a slot and how a task's wait before merge splits into review and landing.
- **E. Parallel tests:** this project's verify runs the suite with pytest-xdist; `ck gate` caps xdist workers when several gates run on one machine; `ck doctor` / `ck init` suggest xdist for pytest projects and how to check for unstable test ids.

The tiered landing gate (option C) is out of scope. Decision record: [PLAN.md](PLAN.md) (decisions D1-D11 are binding).

## User Stories

### Story 1: Ready tasks start without a plan agent per event (A)
**As a** developer running `/spec:implement <spec>` on a whole spec **I want** the workflow to know from the dependency graph which tasks are ready **So that** fewer mechanical plan agents run, they cost less, and their runtime slots go to reviews and landings.

**Acceptance Criteria:**
- 1.1 WHEN `/spec:implement` starts a whole-spec run, THE SYSTEM SHALL pass each remaining task's dependency list (as emitted by `ck plan --json`) to the workflow alongside the existing per-task arguments.
- 1.2 WHEN the workflow decides which tasks are ready from a known set of Done tasks and a set of excluded tasks (running, failed, already merged), THE SYSTEM SHALL produce the same ready tasks, in the same order (longest remaining dependency chain first, then task number), as `ck plan --json --exclude` produces for the same tasks.md, Done set and exclusions.
- 1.3 WHEN a task's implementer finishes and frees an implementing slot, THE SYSTEM SHALL start the next ready task(s) from its own readiness computation without running a plan agent.
- 1.4 THE SYSTEM SHALL run a plan agent only (a) once at the start of the run, (b) after each landing attempt completes, and (c) when the workflow has a free implementing slot, no ready task, and tasks that are neither running, failed nor merged remain.
- 1.5 WHEN case 1.4(c) has triggered a plan agent, THE SYSTEM SHALL NOT trigger another idle plan agent until a landing completes or a task finishes.
- 1.6 WHEN a plan agent returns, THE SYSTEM SHALL replace its view of remaining tasks, dependencies and lint results with the plan agent's, and set Done to the plan's Done tasks plus every task merged in this run (a merge never un-happens because a plan read the target just before it), so tasks finished before the run, committed directly to the target, or added or relinked in tasks.md during the run are picked up at that point.
- 1.7 WHEN a plan agent's result reports a dependency cycle or a verify-order cycle, THE SYSTEM SHALL handle it exactly as today (refuse to launch at start; skip or halt mid-run per `onFailure`).
- 1.8 WHEN a plan agent fails twice in a row, THE SYSTEM SHALL stop launching new tasks and report the planning failure as today, while running tasks finish and land.
- 1.9 WHEN the workflow is given no dependency information (an older skill or a hand-built args object), THE SYSTEM SHALL fall back to running a plan agent wherever it would otherwise use its own readiness, so the run still completes correctly.
- 1.10 WHEN a task listed as a dependency does not exist in the dependency map, THE SYSTEM SHALL treat the dependent task as not ready (never start it on a guess).

### Story 2: Task agents create their own worktrees (A)
**As a** developer **I want** each task agent to create and claim its own worktree **So that** no separate mechanical agent runs per task to create it.

**Acceptance Criteria:**
- 2.1 WHEN the workflow starts a task, THE SYSTEM SHALL instruct the task agent to create the task worktree (with dependency install, based on the target branch) and claim it as its first steps, and the plan agent SHALL no longer create task worktrees.
- 2.2 WHEN worktree creation reports the worktree already exists and is not claimed by a live agent, THE SYSTEM SHALL let the task agent claim and use it.
- 2.3 WHEN worktree creation or claiming reports BUSY (a live claim by another agent), THE SYSTEM SHALL record the task as not merged with reason "worktree busy: <detail>", SHALL NOT retry or escalate it, SHALL NOT touch that worktree, and SHALL NOT apply `onFailure: halt` because of it (as with today's plan-step BUSY).
- 2.4 WHEN worktree creation fails with an error, THE SYSTEM SHALL record the task as not merged with the creation error as its reason, and apply `onFailure` as for any other failed task.
- 2.5 WHEN a retry, revision or review agent runs for a task, THE SYSTEM SHALL keep using the existing task worktree (no new creation).
- 2.6 WHEN a fixer worktree is needed after a red gate, THE SYSTEM SHALL create it as today.

### Story 3: Finish-first scheduling (B)
**As a** developer **I want** reviews, revisions and landings to get runtime slots before new tasks start **So that** finished tasks do not sit waiting for a reviewer while new work starts.

**Acceptance Criteria:**
- 3.1 WHILE any review, revise or land agent of the run is presumed queued, THE SYSTEM SHALL NOT start a new task. An agent is presumed queued when it lies beyond the first `agentSlots` in-flight agents of the run in request order (the runtime starts agents FIFO; the script cannot see the runtime's real queue), where `agentSlots` is the runtime cap from the Constraints (design.md, "Queued: the observable approximation").
- 3.2 WHEN an agent of the run settles and no review, revise or land agent is presumed queued any more, and a ready task, a free implementing slot and a free runtime slot (3.9) exist, THE SYSTEM SHALL start the ready task at that point, before any other agent of the run settles.
- 3.3 WHEN no review, revise or land agent is presumed queued and the run has a free runtime slot for each start (3.9), THE SYSTEM SHALL start ready tasks exactly as it would without finish-first (subject to `maxConcurrent`).
- 3.4 THE SYSTEM SHALL never delay a review, revise, land, gate, fix or re-check agent because of finish-first.
- 3.5 WHEN finish-first is turned off in the run settings, THE SYSTEM SHALL start ready tasks without regard to presumed-queued review, revise or land agents or to free runtime slots.
- 3.6 WHERE no setting is given, THE SYSTEM SHALL run with finish-first on.
- 3.7 WHEN finish-first defers a task start, THE SYSTEM SHALL say so in the run log at most once per deferral period (not once per event).
- 3.8 WHEN an invalid value is given for the finish-first setting, THE SYSTEM SHALL reject it with a message naming the allowed values and leave the stored setting unchanged.
- 3.9 WHILE finish-first is on and `agentSlots` is known, THE SYSTEM SHALL start at most max(0, `agentSlots` − inFlight) new tasks at a time, where inFlight counts the run's agents requested and not yet settled (including a review, revise or land the settling agent's own follow-up just requested), so that a new task start never itself queues and a review, revise or land requested later never waits behind it.

### Story 4: Queue-wait metrics in `ck stats` (D)
**As a** developer tuning runs **I want** `ck stats` to show how long agents waited for a runtime slot and what a finished task waited for **So that** I can see where wait time goes and compare runs before and after this change.

**Acceptance Criteria:**
- 4.1 WHEN `ck stats` collects a workflow run whose run file entries carry a queued time and a start time, THE SYSTEM SHALL record each agent's queue wait in milliseconds (start time minus queued time).
- 4.2 WHEN a run file entry lacks a queued time or a start time, or the start precedes the queued time, THE SYSTEM SHALL record the agent's queue wait as unknown (not zero) and not fail the collection.
- 4.3 WHEN `ck stats show <run>` displays a run with queue waits recorded, THE SYSTEM SHALL show, per agent kind (task, review, revise, plan, land, gate and any other kind present), the count, median, p90 and total queue wait.
- 4.4 WHEN `ck stats show <run>` displays a run, THE SYSTEM SHALL split each task's wait between finishing and merging into review time (review and revise agents running or queued) and landing time (the rest), and show the median and total of each part next to the existing idle-before-merge figures.
- 4.5 WHEN a run has no queue-wait data (collected before this change, or a run file without queued times), THE SYSTEM SHALL show the queue-wait section as "not recorded" and still show every existing figure unchanged.
- 4.6 WHEN `ck stats` output is requested as JSON, THE SYSTEM SHALL include the queue-wait summaries and the review/landing split with the same values the text view shows.
- 4.7 THE SYSTEM SHALL keep previously collected stats records readable by `ck stats show` without re-collection.

### Story 5: This project's suite runs in parallel (E1)
**As a** maintainer of this project **I want** the project's verify command to run the test suite with pytest-xdist **So that** every full-suite run (task gates, full gates, final gate, fix re-checks) is about 4-5x faster.

**Acceptance Criteria:**
- 5.1 THE SYSTEM SHALL declare pytest-xdist as a development dependency of this project.
- 5.2 THE SYSTEM SHALL configure this project's verify command to run the test suite with `-n auto`.
- 5.3 WHEN the test suite is collected twice with different `PYTHONHASHSEED` values, THE SYSTEM SHALL produce identical test ids (no test id depends on process id, hash seed, time or other per-process state).
- 5.4 WHEN the full suite runs under `-n auto`, THE SYSTEM SHALL pass with the same tests as a serial run (no test skipped, deselected or lost to parallel collection).

### Story 6: `ck gate` caps xdist workers across concurrent gates (E2)
**As a** developer running many task gates at once **I want** each gate's `-n auto` to use a fair share of the CPU cores **So that** 14 concurrent gates do not each start one worker per core and oversubscribe the machine.

**Acceptance Criteria:**
- 6.1 WHEN `ck gate` runs verify commands and `PYTEST_XDIST_AUTO_NUM_WORKERS` is not set in its environment, THE SYSTEM SHALL set it for those commands to `max(1, cores // gates running)`, where "gates running" counts the `ck gate` runs active on the machine including this one.
- 6.2 WHEN `PYTEST_XDIST_AUTO_NUM_WORKERS` is already set in the environment, THE SYSTEM SHALL pass it through unchanged.
- 6.3 WHEN more gates are running than there are cores, THE SYSTEM SHALL set the value to 1.
- 6.4 WHEN only this gate is running, THE SYSTEM SHALL set the value to the machine's core count.
- 6.5 WHEN a gate ends (pass, fail, timeout or crash), THE SYSTEM SHALL stop counting it toward later gates' worker caps; a gate whose process no longer exists SHALL NOT be counted.
- 6.6 WHEN the number of running gates cannot be determined, THE SYSTEM SHALL count only this gate, say so in one line of the gate's output (naming the registry path), and still run the verify commands.
- 6.7 THE SYSTEM SHALL leave verify commands themselves unchanged (no `-n` is added or removed), so projects without xdist are unaffected.

### Story 7: `ck doctor` and `ck init` suggest parallel tests (E3)
**As a** developer of another pytest project **I want** ck to tell me that xdist could speed up my gates and how to check my tests are safe for it **So that** I can opt in knowingly.

**Acceptance Criteria:**
- 7.1 WHEN `ck doctor` finds a pytest command in `verify` with no `-n` option, THE SYSTEM SHALL show a suggestion (a warning-level hint, not an issue) to add pytest-xdist and `-n auto`.
- 7.2 WHEN the suggestion is shown, THE SYSTEM SHALL name the unstable-test-id pitfall (xdist's collection mismatch error) and give a one-line check that collects the suite twice with different `PYTHONHASHSEED` values and compares the test ids.
- 7.3 WHEN `verify` already passes `-n` to pytest, or has no pytest command, THE SYSTEM SHALL NOT show the suggestion.
- 7.4 WHEN `ck init` detects a pytest verify command, THE SYSTEM SHALL show the same suggestion and SHALL NOT add `-n` or pytest-xdist itself.
- 7.5 THE SYSTEM SHALL NOT change `ck doctor`'s exit status because of this suggestion.

## Specification Heritage

**Evolves From:** the `/spec:implement` whole-spec workflow (plan step, launch, review and landing queue) and `specs/run-analytics` (`ck stats` collection and display).
**Preserves:** target branch never red; red batches bisected; one landing at a time per target with the batch window halving after red and growing after green; full gate every `fullGateEvery` merges and at the end; protected-path rules; sandbox behaviour and the agents' command rules; review, revision and escalation ladders; retries; `maxConcurrent`; `onFailure`; report built from git (`ck worktree report`); stale-claim takeover; existing `ck stats` figures and records.
**Supersedes:** "re-plan after every finished task and every freed slot" and "the plan agent creates task worktrees".
**Evolution Reason:** measured slot contention and plan-agent cost (PLAN.md, "What the Data Says").

## Non-Functional Requirements

- **NFR-1 Plan agents:** on a run with L landing attempts and R task records, (a) plan agents that are not retries number at most 1 + L + I, where I is the number of idle plan agents (1.4(c)); (b) an idle plan agent may run right after the start plan or a landing plan, and each idle plan agent after the first idle plan is preceded, since the previous idle plan agent, by a landing completion or a task record (1.4(c), 1.5), so I ≤ 1 + L + R; (c) retries number at most one per non-retry plan agent, so all plan agents number at most 2 × (2 + 2L + R); (d) no worktree-creation agents run. On a 10+ task graph with no plan failures, plan agents are fewer than the tasks. Check: fake-runtime workflow tests counting plan-labelled agents (TC-5, P-6).
- **NFR-2 Readiness contract:** script readiness equals `ck plan --json` ready list (set and order) on every generated graph in a contract test (at least 200 random DAGs, random Done/exclude sets).
- **NFR-3 Vendored copy:** the vendored workflow copy is byte-identical to the plugin source (existing sync test passes).
- **NFR-4 Suite time:** this project's full suite under `-n auto` finishes in at most half the serial time on a machine with 8+ cores (measured 52 s vs ~253 s on 16 cores).
- **NFR-5 Measured outcome (manual):** on the next real whole-spec run of comparable size, `ck stats show` reports review median queue wait below 30 s and fewer plan agents than tasks.
- **NFR-6 No added latency, no deadlock:** finish-first adds no delay when no review, revise or land agent is presumed queued and fewer than `agentSlots` agents are in flight (fake-runtime test: the same interleaving of agent request, start and end events as with it off, TC-25, P-10). At saturation (3.9) a held start costs the task nothing under a FIFO runtime: it could not have started before a slot freed, and it is requested at the settle that frees one unless a review, revise or land takes that slot first. Finish-first never deadlocks or starves a ready task: a hold only waits for agents already in flight, every in-flight agent settles and re-runs the start decision, finish-type agents arise only from a finite number of task, review and landing events, and once none is queued and inFlight < `agentSlots` (always true when nothing is in flight, since `agentSlots` ≥ 1) a ready task starts (P-11, TC-65).

## Constraints

- Workflow scripts can only call agents: no shell commands, no filesystem access. Anything needing a command (planning, worktree creation, gates) happens inside an agent.
- The vendored workflow copy must stay identical to the plugin source.
- Tests are integration-first: fake Claude homes for `ck stats`, the fake workflow runtime for the workflow script.
- The runtime cap is the documented min(16, available CPUs - 2) per workflow; ck computes it in one place (`runtime_agent_slots`, using the affinity-aware `xdist.core_count`), `CK_AGENT_SLOTS` overrides it, and the workflow script never hard-codes a number.
- xdist stays a per-project opt-in; ck never turns it on for other projects.
- Spec size: lean, at most ~20 tasks across both tracks, short critical path.

## Out of Scope

- Option C: tiered landing gate (task-only Verify per batch, full verify only at `fullGateEvery`) (D1).
- A fixed slot reserve or a `reserve_slots` setting (D2 chose the adaptive rule).
- A timer-based plan resync (D3).
- A separate "create worktrees" agent (D4).
- `ck bench` comparisons (D5: only if real-run results are unclear).
- Batch-fill delays to grow landing batches.
- Test-impact selection (e.g. pytest-testmon) as a gate.
- Enabling xdist automatically in other projects, and parallel flags for non-pytest runners.
- Changing the landing lock, bisection, protected-path or sandbox logic.

## Assumptions

- Claude Code run file `workflowProgress` entries keep their `queuedAt` and `startedAt` (epoch ms) fields; older files may lack them.
- The runtime does not tell the workflow when an agent it requested has started. "Presumed queued" in Story 3 rests on two assumptions: the runtime runs at most the documented cap of min(16, available CPUs - 2) agents per workflow (it matches the observed peak of 14 on 16 cores), and it starts queued agents FIFO, in request order. Under them the first `agentSlots` in-flight agents in request order are running and the rest are queued (design.md, "Queued: the observable approximation"); fake-runtime tests check that estimate against a FIFO runtime queue whose cap matches the `agentSlots` the script is given. If the runtime's cap differs, `CK_AGENT_SLOTS` corrects it.
- `ck plan --json` keeps emitting `deps`, `chain`, `ready` and `remaining` with today's semantics.
- `PYTEST_XDIST_AUTO_NUM_WORKERS` is honoured by `-n auto` in supported pytest-xdist versions; it has no effect on commands without `-n auto`.
- "Gates running on the machine" means `ck gate` processes of the same user on the same host, across all worktrees and projects, sandboxed or not; they meet in one per-user registry at a fixed path that does not depend on `TMPDIR` (design.md, `registry_dir`).
- `ck doctor` / `ck init` only suggest the stable-id check; they do not run the test suite.
