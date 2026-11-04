# Feature: Run follow-ups (run ETA, analytics hardening, deterministic P-10, Verify-cycle lint)

## Overview

Runs in this project left four gaps (see [PLAN.md](PLAN.md), Status: Decided, D1-D18; D11-D18 settle the
review's open questions):

1. **Run ETA.** A `/spec:create` or `/spec:implement` run gives no finish time, although its structure is
   known at launch: create has fixed phases, implement has the task graph from `ck plan --json`. This
   feature gives every run a finish time and cost range at launch, and keeps them current. The estimate
   comes from a Monte Carlo simulation over the run's structure, using past local run records (D2). Task
   size (Files and acceptance criteria counts) scales the estimate (D4). A p10-p90 cost range comes from the
   same simulation (D5). Bundled aggregate defaults cover a machine with no history and are flagged low
   confidence (D6). The estimate shows in four places: `ck eta`, workflow log lines, a status-line command,
   and notifications through `run.notify_command` (D3). A run that is blocked, or waiting on me, says so
   instead of showing a time that drifts (D10). All of this reads one run progress file per run. That file
   also holds prompt-free-runs' park notes. ETA only observes and predicts. It never changes how a run
   schedules tasks.
2. **Agent-mode analytics hardening.** Four groups of small robustness fixes from the agent-mode-analytics
   review (D7).
3. **Deterministic P-10.** The finish-first scheduler property test runs on a virtual clock and makes an
   exact comparison, with no re-runs (D8).
4. **Verify-cycle lint.** `ck lint` reports an error when an impl task's Verify runs a test file that can
   only pass after a dependent impl task lands (D9). This is the agent-mode-analytics task 8 / task 13 case.

This spec builds on specs/prompt-free-runs and is implemented after it merges. It uses that spec's
notification command (JSON on stdin, 10 s cap), its park notes, which `ck worktree report` shows as
needs-input, and its interruption counts. It does not re-specify them.

Terms used below:
- **Run kind**: `implement` (a whole-spec `/spec:implement` run) or `create` (a `/spec:create` run).
- **Launch estimate**: the first estimate of a run, made before any task or phase starts.
- **Finish range**: p10, median (p50) and p90 of the simulated finish times.
- **Remaining work**: the simulated time from now until the run finishes, with no waiting on me included.
- **History**: the local run records `ck stats` keeps on this machine.

## User Stories

### Story 1: Implement-run estimate from the task graph (D2, D5)
**As a** developer launching `/spec:implement` **I want** a finish range and a cost range before anything
starts **So that** I know whether to wait or walk away.

**Acceptance Criteria:**
- 1.1 WHEN I ask for an estimate of an implement run of a spec, THE SYSTEM SHALL simulate the spec's open
  (not Done) tasks over the dependency graph that `ck plan --json` reports. The simulation SHALL use the
  agent-slot cap from `ck agents --json` and finish-first scheduling. It SHALL report the finish range and
  the p10-p90 cost range.
- 1.2 WHEN it simulates a task, THE SYSTEM SHALL sample the task's duration from past merged tasks of the
  same track, starting model and safety tag in history. It SHALL also sample the task's retries,
  escalations, review wait and landing wait from history, and scale the duration by the task's size (Story
  6).
- 1.3 WHEN fewer than a minimum number of past tasks match a task's track, model and safety tag, THE SYSTEM
  SHALL widen the match step by step: first drop the model, then the safety tag, then the track. If that
  still finds too few, it SHALL use the bundled defaults (Story 7). The output SHALL name the widest step
  used.
- 1.4 WHEN the estimate is printed, THE SYSTEM SHALL show the spec name, the number of open tasks, the slot
  count and the finish range as clock times, plus the median as minutes from launch. It SHALL also show the
  cost range in USD, the critical path with its median minutes, the two slowest tasks by median, the count
  of safety-tagged tasks, the historical retry and escalation rates, and the basis (the number of past
  tasks and runs sampled).
- 1.5 WHEN the same inputs (tasks.md, slot cap, history, seed) are given twice, THE SYSTEM SHALL print the
  same estimate both times.
- 1.6 WHEN the spec has no tasks.md, tasks.md does not parse, or every task is Done, THE SYSTEM SHALL say
  why no implement estimate can be made and exit non-zero, except that "every task is Done" SHALL print
  `nothing left to run` and exit 0.
- 1.7 WHEN the task graph has a dependency cycle or a dependency on an unknown task, THE SYSTEM SHALL report
  `blocked: <reason>` naming the tasks involved, instead of a finish time (Story 5).
- 1.8 WHEN the slot cap cannot be read, THE SYSTEM SHALL use the default cap that `/spec:implement` itself
  uses, and say so in the output.
- 1.9 WHEN history has no cost for the matched tasks, THE SYSTEM SHALL show the cost as unknown, never as
  $0.

### Story 2: Create-run estimate from fixed phases (D2)
**As a** developer launching `/spec:create` **I want** a finish range by phase **So that** I know when
the spec will be ready for my review.

**Acceptance Criteria:**
- 2.1 WHEN I ask for an estimate of a create run, THE SYSTEM SHALL simulate the create workflow's phases
  in order. It SHALL sample each phase's duration from the per-phase durations of past create records,
  where a phase's duration is derived from the timing of that phase's agents. It SHALL report the finish
  range and the p10-p90 cost range.
- 2.2 WHEN a phase runs only in some past runs (for example a second consistency pass), THE SYSTEM SHALL
  include it in each simulated run with the probability seen in history.
- 2.3 WHEN the estimate is printed, THE SYSTEM SHALL list each phase with its median minutes, mark the
  phases already done and the one running, and show the finish range and cost range.
- 2.4 WHEN a create run is in progress, THE SYSTEM SHALL simulate only the unfinished phases. A running
  phase's sampled duration SHALL be taken given that the phase has already run as long as it has.
- 2.5 WHEN past create records lack per-phase timing, THE SYSTEM SHALL leave them out of the phase samples
  and use their total wall time only to check the summed estimate. It SHALL not count them as zero-length
  phases.
- 2.6 WHEN `specs/<spec>/` exists without tasks.md and I do not name the run kind, THE SYSTEM SHALL
  estimate a create run. WHEN tasks.md exists and I do not name the kind, it SHALL estimate an implement
  run. Naming the kind explicitly SHALL override both.

### Story 3: One run progress file, written in both modes (D3, D10)
**As a** developer **I want** every run to write its progress to one local file **So that** ETA, the
status line and notifications all read the same truth.

**Acceptance Criteria:**
- 3.1 WHEN an implement or create run starts, THE SYSTEM SHALL create one progress file for that run and
  append a start event. The start event SHALL carry the run kind, the spec, the launch time, the slot cap
  and the launch estimate.
- 3.2 WHILE an implement run is going, THE SYSTEM SHALL append an event for each task start, merge, retry,
  escalation (with the model), landing (with the tasks landed and red or green), park and approval wait
  (start and end), and for the run's end with its outcome. WHILE a create run is going, it SHALL append an
  event for each phase start and phase end, any approval wait, and the run's end.
- 3.3 WHEN prompt-free-runs parks a task as needs-input, THE SYSTEM SHALL record the park note in the same
  progress file as a park event. `ck worktree report` SHALL go on showing it as needs-input. WHEN the park
  note is cleared, THE SYSTEM SHALL append an unpark event.
- 3.4 WHEN the run uses the Workflow tool, and WHEN it uses the Agent-tool fallback (agent mode), THE SYSTEM
  SHALL write the same events with the same fields. Automated tests SHALL cover both modes.
- 3.5 WHEN two writers append to the progress file at the same time (for example the run and a park
  command), THE SYSTEM SHALL keep every event whole. No event SHALL be interleaved with or lost to another.
- 3.6 WHEN a reader meets a malformed or unknown event line, THE SYSTEM SHALL skip it and read the rest.
- 3.7 WHEN writing a progress event fails (full disk, no permission), THE SYSTEM SHALL continue the run
  with the same outcome it would have had otherwise, and name the failure once in the final report.
- 3.8 THE SYSTEM SHALL store the progress file outside the repository, in ck's private local data area, as
  a private file. The file SHALL hold no prompt text, transcript content or file contents. One exception:
  under `ck run`, the sandboxed child cannot write ck's data area, so it writes its events to a private
  spool directory (mode 0700, files 0600) inside the configured worktree directory. That spool SHALL be
  git-ignored so it is never committed. `ck run` SHALL copy every spool line into the progress file in the
  data area within 5 seconds of it being written (and once more after the child exits), and SHALL delete
  the spool file after it has been fully copied once the child has exited.
- 3.9 WHEN a run is started for a spec that already has a progress file, THE SYSTEM SHALL start a new
  progress file. WHEN the earlier run has no end event, THE SYSTEM SHALL first append an end event with the
  outcome `superseded` to it. The earlier file SHALL stay readable; ck does not delete progress files.
- 3.10 WHEN a run that has not ended has had no event for more than 120 minutes, THE SYSTEM SHALL treat it
  as stale: `ck eta` SHALL show the state `stale` with `no progress since HH:MM` and no finish range, the
  status-line command SHALL leave it out, and no slip or blocked notification SHALL be sent for it. A new
  event SHALL end the stale state. Stale wins over blocked and waiting (D16): a run with an open approval wait,
  an open hand landing or a needs-input park that has had no event for more than 120 minutes SHALL show
  `stale`, not `blocked` (5.1) or `waiting on you N min` (5.4).

### Story 4: `ck eta` (D3)
**As a** developer **I want** a command that shows the current estimate, once or continuously **So that**
I can check a run at any time.

**Acceptance Criteria:**
- 4.1 WHEN I run `ck eta <spec>` and the spec has no progress file, THE SYSTEM SHALL print the launch
  estimate (Stories 1 and 2) and the line `not started`.
- 4.2 WHEN I run `ck eta <spec>` while a run is in progress, THE SYSTEM SHALL re-simulate only the
  remaining work from the progress file. It SHALL print the minutes since launch and the time of the last
  event, and the counts of merged, running and waiting tasks (or the current phase for create). It SHALL
  print the finish range and the change of the median against the launch median (for example `+4 min vs
  launch`). It SHALL print the reason for the latest change (for example `task 7 retry 1`), the remaining
  critical path, each running task with its step, model and minutes so far, and the waiting tasks with
  what each waits on. Exception (D17): while an approval wait is open, including a hand landing (5.4), the
  remaining work stays frozen at its value when the wait began, even when other tasks keep merging.
- 4.3 WHEN a run is in progress, THE SYSTEM SHALL condition each running task's sampled duration on the
  time it has already run, so that no simulated finish is earlier than now.
- 4.4 WHEN I add `--watch`, THE SYSTEM SHALL redraw the output when the progress file changes, and at
  least once a minute. While it is open it SHALL also check for a due slip notification about once a
  minute, with no new event needed (8.5, D14). It SHALL exit 0 when the run ends, after printing the final
  line (Story 8.4). It SHALL exit 0 when I interrupt it.
- 4.5 WHEN I add `--json`, THE SYSTEM SHALL print one JSON object with a schema version. The object SHALL
  hold the run kind, the spec, the state (`not-started`, `on-track`, `slipping`, `blocked`, `waiting`,
  `stale`, `done`), the launch time, the finish p10/p50/p90 as ISO-8601 times, and the remaining-work minutes
  p10/p50/p90. It SHALL also hold the cost p10/p90 (null when unknown), the confidence and basis, the
  critical path, the per-task or per-phase state, and the blocked reason or waiting minutes when they
  apply.
- 4.6 WHEN nothing in the progress file has changed since the last simulation, THE SYSTEM SHALL reuse the
  cached result instead of simulating again.
- 4.7 WHEN `specs/<spec>/` does not exist and no progress run names the spec, THE SYSTEM SHALL print an
  error naming the spec and exit non-zero.
  WHEN the progress file is unreadable, THE SYSTEM SHALL fall back to the launch estimate, say that live
  progress is unavailable, and exit 0.
- 4.8 WHEN the spec's latest run has an end event, THE SYSTEM SHALL print the outcome, the actual finish
  time and wall minutes, the merged count, the cost when known, and where the actual time fell against the
  launch range. It SHALL also print the launch estimate for the open tasks that remain (Stories 1 and 2),
  or `nothing left to run` when none remain.

### Story 5: Blocked and waiting-on-me states (D10)
**As a** developer **I want** the ETA to say "blocked" or "waiting on you" when that is the truth **So
that** a moving time never hides a run that needs me.

**Acceptance Criteria:**
- 5.1 WHILE any task of the run is parked as needs-input, and the run is not stale (3.10), THE SYSTEM SHALL
  show the run's finish as
  `blocked: task N parked (needs input)`, with no finish range, since the run cannot finish until I act. The
  reason SHALL name each parked task and the tasks waiting on it. The output SHALL also show the remaining
  work once unblocked, counting every open task including the parked ones and their dependents (p50, and
  p10-p90 in `--json`).
- 5.2 WHEN a task is parked and other tasks can still run, THE SYSTEM SHALL keep the run's finish as
  `blocked` (5.1). It MAY add, as extra information, when the still-runnable work is done (`runnable work
  done ~HH:MM`), and SHALL name the parked task and its dependents as `waiting on you`. The runnable-work
  time SHALL never be shown or reported as the run's finish range.
- 5.3 WHEN the run can make no further progress without me (a failed task with dependents still
  unfinished that holds every remaining task, or a dependency cycle), THE SYSTEM SHALL show `blocked:
  <reason>` naming the cause and the tasks it holds. It SHALL show the remaining work once unblocked.
- 5.4 WHILE an approval wait is open in the progress file, and the run is not stale (3.10), THE SYSTEM SHALL
  show `waiting on you N min`, where N is the minutes since the wait began. It SHALL freeze the
  remaining-work estimate at its value when the wait began. A task left for a protected-path hand landing
  opens an approval wait when the landing reports it, and that wait closes when the task merges or the run
  ends. Any open hand landing freezes the whole run's estimate (D17): the workflow keeps running other tasks
  after a protected-path landing, and while that wait stays open the remaining work does not move, landing
  log lines show `waiting on you N min` instead of a new median (8.1), and no slip notification is sent
  (8.5), even as other tasks merge.
- 5.5 WHEN an approval wait ends, THE SYSTEM SHALL move the finish range later by the waited minutes and
  resume normal estimating. The waited minutes SHALL not count toward slipping (5.7, Story 8).
- 5.6 WHEN a blocked run becomes unblocked (unpark, or a fixed task merges), THE SYSTEM SHALL return to
  normal estimating from that moment.
- 5.7 WHEN the current median finish, minus the minutes spent waiting on me, is later than the launch p90,
  THE SYSTEM SHALL show the state `slipping` with the new time and its cause. Otherwise the state SHALL be
  `on-track`, with the time and range only.

### Story 6: Task size signal (D4)
**As a** developer **I want** task durations scaled by task size **So that** a 2-minute task and a
15-minute task do not get the same estimate.

**Acceptance Criteria:**
- 6.1 WHEN it estimates a task, THE SYSTEM SHALL count the task's Files entries and the acceptance
  criteria the task names in tasks.md, and scale its sampled duration by a size factor fitted from past
  tasks with known counts.
- 6.2 WHEN `ck stats collect` builds a local record of an implement run, THE SYSTEM SHALL store each task's
  Files count and acceptance-criteria count in that task's entry. These SHALL be local fields only and
  SHALL not be added to shareable records.
- 6.3 WHEN a past record has no counts but the spec's tasks.md is still readable, THE SYSTEM SHALL count
  them from that tasks.md for the fit. WHEN neither is available, the task SHALL count as unsized: it
  stays in the duration samples and is left out of the fit.
- 6.4 WHEN fewer than a minimum number of sized past tasks exist, THE SYSTEM SHALL use the bundled size
  factor. WHEN neither local nor bundled fit is available, it SHALL use no scaling (factor 1), and the
  output SHALL say the size signal was not used.
- 6.5 WHEN a task has zero Files and zero named criteria, THE SYSTEM SHALL treat it as the smallest size
  seen in the fit, never as zero duration.
- 6.6 WHEN the fit is computed from a history where longer tasks have more Files and criteria, THE SYSTEM
  SHALL give a larger task a larger median than a smaller task of the same track, model and safety tag.

### Story 7: Cold start with bundled defaults (D6)
**As a** developer on a new machine **I want** a usable estimate with no local history **So that** ETA
works from my first run.

**Acceptance Criteria:**
- 7.1 WHEN history has no records, THE SYSTEM SHALL estimate from bundled default distributions and flag
  the output `low confidence: no local history`. In `--json`, the confidence SHALL be `low` and the basis
  `bundled`.
- 7.2 THE SYSTEM SHALL ship the bundled defaults as aggregate numbers only. These are quantiles of task
  agent-minutes per track and safety tag, retry and escalation rates, review and landing wait quantiles,
  per-phase create durations and phase-repeat rates, cost per agent-minute quantiles, and the size factor.
  They SHALL hold no record ids, run ids, session ids, spec names, task titles, paths, timestamps or other
  record-level data.
- 7.3 WHEN history has records but not enough for a given distribution, THE SYSTEM SHALL use local data
  wherever it has enough, use the bundled default for the rest, and name in the output which parts came
  from bundled defaults.
- 7.4 WHEN the bundled defaults are missing or unreadable, THE SYSTEM SHALL still show the graph (critical
  path and task count) with `no estimate: no history and no bundled defaults`, and exit 0.
- 7.5 THE SYSTEM SHALL provide a maintainer command that regenerates the bundled defaults from the local
  history. It SHALL write aggregates only (7.2), and refuse to write when the history has fewer than a
  minimum number of runs.

### Story 8: ETA in workflow logs, the status line and notifications (D3)
**As a** developer **I want** the estimate where I already look **So that** I do not have to ask for it.

**Acceptance Criteria:**
- 8.1 WHEN an implement or create run starts, THE SYSTEM SHALL write one log line with the launch finish
  time, the p90 and the critical path (for create: the phase count), for example `ETA 17:34 (p90 17:50),
  critical 1→2→3→7→8→13`. WHEN a landing finishes, or a create phase ends, it SHALL write one log line with
  what happened and the new median, plus the change when it is non-zero (for example `landed 4,9 · ETA
  17:35` or `7 retry 1 · ETA 17:38 (+3)`). Exception (D17): while an approval wait is open, including a hand
  landing (5.4), the line SHALL show `waiting on you N min` in place of the median (for example `landed 4 ·
  waiting on you 12 min`). This SHALL hold in Workflow mode and in agent mode.
- 8.2 WHEN computing an ETA log line fails or takes longer than 3 seconds (4 seconds for the launch
  estimate), THE SYSTEM SHALL write `ETA unavailable` instead, and continue the run unchanged.
- 8.3 THE SYSTEM SHALL provide a status-line command for Claude Code. It SHALL print one line per active
  run of the current project: `ck <spec> <merged>/<total> ETA <HH:MM> ~$<cost p50>` for implement, and
  `ck <spec> create <phase> ETA <HH:MM>` for create. The cost is the median total cost of the latest
  cached estimate; the `~$<cost p50>` part SHALL be left out when that cost is unknown. In place of `ETA <HH:MM>` it SHALL show `blocked`
  or `waiting on you N min` when those apply. It SHALL read only the progress files and the latest cached
  estimate, and never simulate.
- 8.4 WHEN no run of the current project is active, THE SYSTEM SHALL have the status-line command print
  nothing and exit 0. WHEN a progress file is unreadable, it SHALL leave that run out and exit 0. The
  command SHALL never print an error to the status line.
- 8.5 WHEN the current median finish, minus the minutes spent waiting on me, first passes the launch p90 by
  more than the slip threshold, THE SYSTEM SHALL send one notification through `run.notify_command` naming
  the spec, the new finish time with its change, and the cause. Another slip notification SHALL be sent
  only when the slip grows by another full threshold. The threshold is configurable in ck's run settings,
  with a default of 15 minutes. The slip check runs at every progress event. A slip can also come from time
  alone, while a task overruns and nothing lands (D14): THE SYSTEM SHALL then check about once a minute with
  no new event, from `ck run`'s watcher while a `ck run` run is going and from `ck eta --watch` while it is
  open (4.4). With neither running, such a slip SHALL be noticed at the next event. No slip notification
  SHALL be sent while an approval wait is open, including a hand landing (5.4, D17), or while the run is
  stale (3.10).
- 8.6 WHEN a run becomes blocked by a failed task or a dependency cycle or unknown dependency (Story 5.3,
  1.7), THE SYSTEM SHALL send one notification naming the spec and the blocked reason. It SHALL not send a
  second one for the same reason. A park never gets a blocked notification (8.9).
- 8.7 WHEN a run ends, including a run closed with the outcome `superseded` because a new run of the same
  spec started (3.9, D18), THE SYSTEM SHALL send one notification naming the spec, the end time, merged over
  total tasks (or `spec written` for create), the wall minutes and the cost when known. When the outcome is
  not `completed`, the notification SHALL name the outcome (for example `superseded`).
- 8.8 WHEN `run.notify_command` is not configured, THE SYSTEM SHALL send nothing for 8.5-8.7 and run
  normally. WHEN the command fails or exceeds its time cap, it SHALL continue the run and name the failure
  in the final report, following prompt-free-runs' notification contract.
- 8.9 WHEN a task is parked as needs-input, prompt-free-runs' park message SHALL be the only notification
  for that park (D15). THE SYSTEM SHALL not send a blocked notification for a park, whether the park blocks
  the whole run (no open task outside the parked tasks and their dependents can still run) or only one
  branch.
- 8.10 THE SYSTEM SHALL keep every notification message free of test text, requirement text, prompt text
  and file paths.
- 8.11 WHEN THE SYSTEM sends a slip, blocked or done notification through `run.notify_command`, it SHALL set
  `CK_NOTIFY_TYPE` in the command's environment to `ck_eta_slip`, `ck_eta_blocked` or `ck_run_done` (D13).
  Prompt-free-runs' needs-input pages, including the park message, SHALL keep `ck_needs_input`, so their
  behaviour is unchanged. The message and the JSON on stdin SHALL not change. ck sends every notification
  the same way: a quieter route for done or slip notices needs my notify command to branch on
  `CK_NOTIFY_TYPE`.

### Story 9: Backfill never double-counts or hides a missing outcome (D7)
**As a** developer running `ck stats collect --agents-backfill` **I want** backfill to refuse when it
cannot check for overlaps, and to say when an outcome is unknown **So that** my history stays correct.

**Acceptance Criteria:**
- 9.1 WHEN backfill cannot read the local record store, so it cannot check for overlapping backfill records,
  THE SYSTEM SHALL write no records. It SHALL print an error naming the store and the read failure, and
  exit non-zero. It SHALL never treat an unreadable store as empty.
- 9.2 WHEN `--dry-run` is given and the store is unreadable, THE SYSTEM SHALL still list the planned
  groups, mark that overlaps could not be checked, and exit non-zero.
- 9.3 WHEN a record file in a readable store cannot be parsed, backfill cannot rule out an overlap with it,
  so THE SYSTEM SHALL write no records, name the file, and exit non-zero, as in 9.1. With `--dry-run` it
  SHALL list the planned groups, mark them `overlaps not checked`, name the file and exit non-zero.
- 9.4 WHEN a backfill group's session transcript is missing or unreadable, THE SYSTEM SHALL write the
  group's record with an unknown outcome and an `outcome` entry in the record's missing list.

### Story 10: Agent-mode collect errors are visible (D7)
**As a** developer **I want** `collect --agents` failures to come out in the form I asked for, and logged
under `--auto` **So that** scripts and later checks see them.

**Acceptance Criteria:**
- 10.1 WHEN `ck stats collect --agents <id> --json` runs inside the sandbox, THE SYSTEM SHALL print one
  JSON failure object with the run id, result `failed` and an error naming the sandbox. It SHALL exit 1.
  It SHALL not print plain text.
- 10.2 WHEN `ck stats collect --agents <id> --auto` gets a malformed or unknown run id, THE SYSTEM SHALL
  append a line to collect.log naming the id and the reason, print the retry hint, and exit 0. This
  follows the existing `--auto` contract.
- 10.3 WHEN `ck stats collect --agents <id>` gets a malformed id without `--auto`, THE SYSTEM SHALL print
  the error (as JSON with `--json`) and exit 1, as today.

### Story 11: `collect --all` reports sweep errors and writes by source (D7)
**As a** developer **I want** `collect --all` never to drop an error, and to write each record with the
right writer **So that** a sweep failure is visible and records are never written the wrong way.

**Acceptance Criteria:**
- 11.1 WHEN the open-manifest sweep fails as a whole during `ck stats collect --all`, THE SYSTEM SHALL
  report it as a failure with its reason (in the text output, and in `failures` in `--json`), count it as
  failed, and exit 1. It SHALL still report every workflow-run outcome.
- 11.2 WHEN `collect --all` writes a collected record, THE SYSTEM SHALL pick the agent-mode writer for
  records built from an agent-mode run and the workflow writer for records built from a workflow run. The
  pick SHALL use the record's source, not its run-id prefix.
- 11.3 WHEN ck orders a run's agents for a record, THE SYSTEM SHALL read each agent transcript at most once.
- 11.4 THE SYSTEM SHALL keep every existing `collect`, `--all`, `--agents`, backfill and doctor result the
  same, apart from 6.2, 9.1-9.4, 10.1-10.2 and 11.1. Records SHALL be byte-identical where their inputs are
  the same. The spec's tasks.md, read at collection time, counts as an input for 6.2's counts, so a
  re-collect after upgrading may report a record `updated` once, and editing tasks.md later may change a
  re-collected record.

### Story 12: Deterministic finish-first property test (D8)
**As a** maintainer **I want** P-10 to run on a virtual clock with an exact comparison **So that** the
full suite never fails on timing.

**Acceptance Criteria:**
- 12.1 WHEN the scheduler property tests drive the scheduler, THE SYSTEM SHALL run it on a virtual clock,
  so that timer order depends only on the simulated times and not on machine load.
- 12.2 WHEN P-10 checks that finish-first changes nothing when nothing queues, THE SYSTEM SHALL compare the
  two schedules exactly, with no tolerance, in a single run per configuration and no re-runs. The relation
  it asserts SHALL be at least as strong as today's.
- 12.3 WHEN the workflow runs for real, THE SYSTEM SHALL use real time exactly as today. The virtual clock
  SHALL be available only to the test bridge, and the shipped workflow's behaviour SHALL not change.
- 12.4 WHEN P-10 runs under `-n auto` while the machine is under heavy CPU load, THE SYSTEM SHALL give the
  same result as on an idle machine.

### Story 13: Lint catches Verify cycles through test-task files (D9)
**As a** developer **I want** `ck lint` to reject a task whose Verify can never pass before a dependent
task lands **So that** a run does not discover it after escalations.

**Acceptance Criteria:**
- 13.1 WHEN impl task X's Verify names test file F, F is in the Files of test task T, T verifies impl task
  Y, and Y depends on X directly or through other tasks (Y ≠ X), THE SYSTEM SHALL report a lint error. T
  verifies Y when T's Verifies lists Y or Y's Verified by lists T.
- 13.2 WHEN this error is reported, THE SYSTEM SHALL name X, F, T and Y, show the dependency chain from Y
  back to X, and suggest narrowing X's Verify to test files that do not need Y.
- 13.3 WHEN any such error exists, THE SYSTEM SHALL make `ck lint` exit non-zero. `/spec:implement` (whole
  spec, Workflow and agent mode) SHALL refuse to start and show the error until tasks.md is fixed. A
  single-task `/spec:implement <spec> X` SHALL also refuse to start when X is the X of such an error.
- 13.4 WHEN F is in no test task's Files, or no impl task verified by an owning test task depends on X,
  THE SYSTEM SHALL not report this error. The exemptions in 13.9 and 13.10 also apply.
- 13.5 WHEN X is marked Done, THE SYSTEM SHALL not report this error for X, since its gate has already
  passed (as with the existing import-based check).
- 13.6 WHEN X's Verify names F by a path with a leading `./`, or as part of a longer test command (several
  files, flags), THE SYSTEM SHALL still match F. A `::test` selector on F, or a `-k` option in the same
  command, still identifies F but narrows it (13.10).
- 13.7 WHEN tasks.md has a dependency cycle or names an unknown task, THE SYSTEM SHALL report what it can
  and not crash. The existing cycle and unknown-task errors SHALL still be reported.
- 13.8 WHEN `ck lint --json` runs, THE SYSTEM SHALL include each such error as a structured entry with X, F,
  T, Y and the chain, beside the existing import-based Verify-order entries. The existing entries SHALL be
  unchanged.
- 13.9 WHEN T also verifies X itself (T's Verifies lists X, or X's Verified by lists T), THE SYSTEM SHALL not
  report this error for that X and T, because F is then X's own oracle (D12).
- 13.10 WHEN every place X's Verify names F narrows it, either with a `::` selector on F or with a `-k`
  option in the same command, THE SYSTEM SHALL not report this error for that X and F (D12). WHEN any of X's
  Verify commands names F without narrowing it, the error SHALL still be reported.

## Specification Heritage

- **Evolves From:** specs/agent-mode-analytics (backfill, `collect --agents`, `--all` sweep), specs/landing-speed
  (finish-first scheduler and its property tests), specs/run-analytics (records, store, collect.log), and
  the existing import-based Verify-order check in `ck lint`. Builds on specs/prompt-free-runs (notification
  command, park notes, interruption counts).
- **Preserves:** schema `ck-run-analytics/1` (additive only; nothing new in shareable records), content-free
  shared records, the run-analytics rule that missing data is listed and never shown as zero, the
  `--auto` "always exit 0, log and print the retry" contract, the import-based Verify-order check,
  scheduling behaviour of both workflows, and prompt-free-runs' notification and park contracts.
- **Supersedes:** backfill treating an unreadable record store as empty; plain-text failure output under
  `--json` in the sandbox; `collect --all` dropping open-manifest sweep errors; choosing the record writer
  by the `ag_` id prefix; P-10's real-timer re-run loop.
- **Evolution Reason:** the PLAN.md problem statement: no finish time for runs, review findings left open,
  a flaky full suite, and a lint gap that cost escalations.

## Non-Functional Requirements

- **Simulation speed:** an implement estimate for a 50-task graph with 14 slots and 500 history tasks
  finishes in under 1 second on this development machine. A create estimate finishes in under 0.5 seconds.
- **Status-line cost:** the status-line command finishes in under 100 ms with 3 active runs, each with a
  1,000-event progress file. It does no simulation and no network call.
- **Stable output:** with a fixed seed, repeated estimates are identical (1.5). With the default seed
  policy, two estimates on the same inputs differ in p10, p50 and p90 by no more than 1 minute each, for a
  60-minute run.
- **Calibration (manual check):** a backtest over this machine's past implement and create runs reports how
  often the actual wall time fell inside the launch p10-p90. The target is at least 60%, and the backtest
  prints the figure.
- **Never slows or breaks a run:** with the progress file, ETA computation and notification command all
  forced to fail, a run's merges, landings and outcome match a run where they succeed (3.7, 8.2, 8.8).
- **Privacy:** progress files and the ETA cache are mode 0600 in a 0700 directory when ck creates them.
  The seeded leak test finds zero record-level values (ids, names, paths, timestamps) in the bundled
  defaults (7.2). No new field enters shareable records.
- **No network:** ETA, the progress file, the status line and lint make no network calls. The configured
  notification command is the only outbound call.
- **Flake-free suite:** P-10 passes 50 consecutive runs of the scheduler property test file under `-n auto`
  while a CPU-saturating load runs alongside (12.4).
- **Lint precision:** on copies of every spec's tasks.md in this repository, with every task's Status reset to
  Not Started (so the Done exemption, 13.5, hides nothing), `ck lint` reports no new Verify-cycle error. This
  includes landing-speed tasks 9 and 17, whose gates passed (exempt by 13.9; task 17 also by 13.10). The
  agent-mode-analytics tasks.md as it stood before task 8 was fixed (task 8's Verify naming
  `tests/test_stats_perf.py`, task 8 Verified by: None) is the only one that fails, with exactly one (X, Y)
  pair: task 8 needing task 13. It gives one entry per owning test task (Tasks 9 and 10 both own that file),
  all for that pair.
- **Backward compatibility:** existing stats, lint, plan, worktree and scheduler tests pass unchanged,
  except P-10, which is rewritten by 12.2.

## Constraints

- Pure code in ck: no new agents (PLAN constraint).
- ETA only observes and predicts. It never changes task scheduling, slot use, landing or retries.
- Shared schema stays `ck-run-analytics/1`, additive only. The new per-task size counts (6.2) are local
  only.
- Works in Workflow mode and agent mode (as prompt-free-runs D12). Both modes are covered by automated
  tests.
- Notifications reuse `run.notify_command` from prompt-free-runs and follow its contract (JSON on stdin,
  10 s cap). This spec is implemented after prompt-free-runs merges and branches from it.
- Changes to the workflows and to the agent-mode instructions are made in the packaged source and kept
  identical in the vendored copies under `.claude/`. The vendored copies are protected paths that land by
  hand, so these edits are grouped into as few tasks as possible.
- Tests are integration-first: they drive the real `ck` CLI against fake Claude homes, fixture specs and
  real progress files, with no mocks of project code. They are xdist-safe, and fixtures use invented
  names only.
- The spec stays lean in scope, but the task count is not capped (D11): the decided scope is kept whole, and
  no task is merged or deferred only to reach a size target.

## Out of Scope

- Prompt and refusal handling, park notes and the notification command itself (specs/prompt-free-runs).
  This spec only uses them.
- Changing how runs schedule tasks (PLAN scope "Out").
- ETA for `/spec:review` runs, single-task `/spec:implement <spec> <N>` runs, `/spec:plan` and
  `/spec:spawn-worktree` batches started by hand.
- A sum-of-medians critical-path estimator (rejected in D2), an ETA based only on track, model and safety
  (rejected in D4), and time-only output (rejected in D5).
- More re-runs as a fix for P-10 (rejected in D8). A warning-only Verify-cycle rule (rejected in D9).
- Letting the ETA slip while the run waits on me (rejected in D10).
- New agents, a hosted service, a built-in ntfy client, or any network call from ETA.
- Record-level data or names in the bundled defaults, and a new shareable schema version.
- A graphical timeline (Gantt) view. The mockup's planned-timeline chart is illustrative; the text and
  JSON outputs carry the same data.
- Detecting permission prompts inside agents that the run itself cannot observe (see Assumptions).

## Assumptions

- "Open tasks" are those not marked Done in tasks.md. `ck plan --json` is the source of the graph,
  critical path and ready set.
- The default slot cap and the finish-first rule the simulation uses are the ones `/spec:implement` uses
  today. The design takes them from the same configuration, not from a copy.
- "Approval wait" covers the waits the run itself can see and record: an interactive question to me
  (prompt-free-runs 7.6) and a protected-path hand landing waiting on me (5.4). A hand landing is never a
  `blocked` cause. A permission prompt inside an
  agent is not visible to the Workflow script, and is not counted as waiting.
- Review and landing waits have been recorded only since 0.10.1. Early ranges will be wide and are
  labelled by their basis.
- Per-phase create durations come from the timing of each phase's agents in spec-create records. The
  design picks the phase mapping and the number of simulations within the speed target.
- The minimum sample counts for 1.3, 6.4 and 7.5, the seed policy, and the cache location are design
  choices. The 120-minute stale limit (3.10) is fixed here: it is about seven times the history's p90 task
  length, so a live run almost never goes that long without an event.
- The status-line command's name, the progress-file path and format, the ETA `--json` schema name and the
  slip-threshold config key are design choices within the criteria above (PLAN "Delegated to Spec").
- For D7's overlap guard, this spec chooses to refuse the whole backfill write (9.1, 9.3) rather than skip
  the affected groups, because an unreadable store or record gives no way to know which groups are
  affected.
- "Verify names test file F" means F appears as an explicit path in the task's Verify commands. A glob or
  directory argument is not expanded for 13.1. This keeps the rule to the exact pattern, so legitimate
  broad Verify lines (for example foundation tasks running `tests/test_stats_*.py`) are not flagged.
- A known real-world Verify cycle of a different shape is not caught at launch: prompt-free-runs Task 11
  (impl) is verified by Task 3 (test), Task 3's tests import `analytics/share.py`, which Task 15 (impl) owns,
  and Task 15 depends on Task 11. The new rule (13.1) does not report it, because Task 3 verifies only Task 11
  itself (13.9). The existing import-based check reports it (Task 11 needing Task 15, through `share.py`), but
  only once Task 3's test files exist on disk, since it reads their imports: so `ck lint` passes at launch,
  and from the moment Task 3 has landed `ck plan` reports the entry and keeps Task 11 out of the ready set.
  Catching it at launch would need the imports of test files not yet written, which is out of scope. TC-100
  pins both halves, so neither check regresses on it.
- Cost per task is taken from the per-agent cost already in history. Where a record has no cost, that
  sample counts as unknown cost, not as zero.
