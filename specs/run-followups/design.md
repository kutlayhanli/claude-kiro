# Design: Run follow-ups (run ETA, analytics hardening, deterministic P-10, Verify-cycle lint)

## Overview

This spec adds one observe-only subsystem (run ETA) and three small fixes to existing ck code. Every decision in
[PLAN.md](PLAN.md) (D1-D18) is binding. The spec branches from `specs/prompt-free-runs` and uses its interfaces
as given, with one additive change (D13: an optional `notification_type` keyword on `notify.send`, below):
`run.notify_command` and `claude_kiro.notify.send(command, message, cwd, timeout=10) -> NotifyOutcome`,
`ck worktree park` / `ck worktree resolve` with `worktrees.park / unpark / resolve`, the park note
`<worktreeDir>/<spec>-task-<N>.needs-input.json`, the `needsInput` list in `ck worktree report`, the
`CK_RUN_NOTIFY=parent` convention, and `ck run`'s notification watcher thread. None of them is re-specified here.

**Run ETA (D1-D6, D10).** Four facts about the existing system shape the design:

1. **Workflow scripts have no clock and no I/O.** `Date.now()` and argless `new Date()` throw inside a Workflow
   script, and the script can act only through `agent()` calls. So the script never computes an ETA itself. Every
   progress event is written, and every ETA line computed, by a `ck` command that some agent or the main session
   already runs: `ck worktree claim` (task agents), `ck worktree land` (the land agent), `ck plan` (the plan agent),
   `ck worktree park` (prompt-free-runs' park step), and new `ck progress ...` commands run by the skill (Workflow
   mode) or the session itself (agent mode). The script only puts the run key into the command lines it already
   builds, and logs the ETA text those commands print, which the agent copies into its structured result.
2. **`ck plan --json` is the graph** (deps, done, remaining, ready order, chain lengths, risk, complexity, critical
   path). The estimator calls the same function in-process (`cli.worktree._plan`) and does not parse tasks.md on
   its own. The slot cap comes from `agents.runtime_info()["agentSlots"]` and `run.max_concurrent`, the values
   `/spec:implement` passes as `agentSlots` and `maxConcurrent` (1.1, 1.8).
3. **History is the local record store** (`analytics.store.load_records`). Implement records hold `tasks[]` (track,
   riskTag, agentMinutes, retries, escalations, reviewWaitMs, landingWaitMs) and `agents[]` (role, task, attempt,
   model, usd, phase). Create records hold `phases: {title: minutes}`, built from each phase's agent spans. At
   launch the estimator **fits a model snapshot** from history: aggregate numbers grouped by fixed keys. The
   snapshot is written next to the progress file, and every later recomputation in that run (land, park, `ck eta`)
   simulates from it, never from raw records. This keeps recomputation fast, keeps one run's estimates consistent,
   and lets a sandboxed process estimate without reading the record store.
4. **`ck run` hides ck's data directory from the sandboxed child** (`sandbox_settings` denies `store.data_dir()`
   and `denyRead` covers `~/`). Under `ck run` the progress file is therefore written to a spool directory the
   child can write, inside the worktree directory and git-ignored (the one exception 3.8 allows). Because the
   child cannot read the record store either, `ck run` fits the model snapshot before spawning the child and
   leaves it in the spool under a key-independent name, which `ck progress begin` uses inside the child.
   `ck run`'s own process, outside the sandbox, mirrors the spool into the data directory and sends any
   deferred notifications. This extends prompt-free-runs' watcher thread.

The estimator is a seeded Monte Carlo (D2). For implement it simulates the open-task graph under the slot cap,
starting ready tasks longest chain first, as `ck plan` orders them. Each task's whole past outcome is resampled
jointly from matched history: agent-minutes, retries, escalations, review wait, landing wait and cost. The minutes
are scaled by task size (D4). For create it sums per-phase samples, and includes each phase with its observed
presence rate. Both report the finish p10/p50/p90 and a p10-p90 cost range (D5). With no local history the
bundled aggregate defaults are used, flagged low confidence (D6). Blocked and waiting-on-me states are decided by
pure functions over the folded progress events (D10). Notifications on slip, blocked and done go through
`run.notify_command` (D3).

**Analytics hardening (D7).** The fixes stay inside `analytics/store.py`, `analytics/agentcollect.py`,
`analytics/backfill.py`, `analytics/collect.py`, `cli/stats.py` and `cli/main.py`. D7 leaves one choice to the
spec, the refusal behaviour of the overlap guard. This design refuses the whole backfill write (requirements
Assumptions, 9.1).

**Deterministic P-10 (D8).** The shipped workflow uses no timers. Only the test fake (`tests/js/fake_project.mjs`)
sleeps on real `setTimeout`. A virtual clock is injected into the Node test harness, so the script under test is
unchanged (12.3).

**Verify-cycle lint (D9, D12).** This is a new pure check in `lint.py`, `verify_file_cycles`. Its entries are appended
to the existing `verifyCycles` list, so `ck lint` exit 1, `ck plan`'s `blocked` set, the implement skill's lint
refusal (step 3) and the workflow's start refusal all apply without new wiring. Two exemptions keep it to the
agent-mode-analytics task 8 shape (D12): a test task T that also verifies X is X's own oracle (13.9), and a Verify
that narrows F with `::` or `-k` runs only part of F (13.10). The prompt-free-runs 11/3/15 import-plus-ownership
cycle (requirements Assumptions) is a different shape: the existing import check reports it once Task 3's test
files exist, and nothing reports it at launch. TC-100 pins that behaviour.

## Components

### Existing Components to Modify

- `src/claude_kiro/notify.py` (from prompt-free-runs, D13, 8.11): `send` gains an optional keyword
  `notification_type: str = "ck_needs_input"` and sets `CK_NOTIFY_TYPE=<notification_type>` in the command's
  environment. The message, the stdin JSON, the 10 s cap and `NotifyOutcome` are unchanged, and every existing
  caller passes nothing, so prompt-free-runs' pages (park included) keep `ck_needs_input`. ETA notices pass
  `ck_eta_slip`, `ck_eta_blocked` or `ck_run_done` (`NOTICE_TYPES` in `eta/decide.py`). The `send` docstring says
  that ck sends every type the same way, and that a quieter route for done or slip notices needs the user's notify
  command to branch on `CK_NOTIFY_TYPE`.
- `src/claude_kiro/lint.py` adds `verify_file_cycles(tasks, config)` (D9, D12, 13.1-13.10). `lint_spec` appends its
  entries to `verifyCycles` after the import-based entries. The new check also runs when the graph has a
  dependency cycle, because it is cycle-safe. The import check stays skipped in that case, as today (13.7).
- `src/claude_kiro/cli/worktree.py`:
  - `lint` text output prints new entries as `✗ verify-file cycle: <message>`. The success line becomes
    `✓ no verify-order cycles found (checked Python imports of existing test files and Verify test files owned by
    test tasks)`. `--json` is unchanged in shape: the new entries sit in `verifyCycles` (13.8).
  - `claim` gains `--progress-run KEY`, `--progress-event {task-start,retry,escalation,revision,landfix,resolve}`,
    `--progress-model MODEL` and `--attempt N`. It appends one event only when the claim outcome state is
    `CLAIMED` (3.2). BUSY, ERROR and MISSING append nothing, so the retry prompt's create-then-claim-again path
    writes exactly one start event. A progress failure never changes claim's output or exit code (3.7).
  - `land` gains `--progress-run KEY`. After landing it appends one `landing` event plus one `merge`, `task-failed`
    or `hand-landing` event per task. MERGED gives `merge`, RED or CONFLICT give nothing (the task is retried by the
    run), and PROTECTED gives `hand-landing`, which opens an approval wait for that task (5.4): it closes on a later
    `merge` of the task or on `end`. It then runs `eta.service.after_event` within a 3 s budget, and adds
    `"eta": "<log line>" | null` to its JSON (8.1, 8.2).
  - `plan` gains `--progress-run KEY` and `--failed IDS`. It appends `task-failed` for each listed task not yet
    recorded as failed, then runs `after_event` (5.3, 8.6). The plan JSON gains nothing else.
  - `park` and `resolve` (from prompt-free-runs): when an active progress run exists for the spec (see
    `progress.active_run`), park appends `park` and resolve appends `unpark` (3.3). Then they run `after_event`.
    `worktrees.claim`'s implicit `unpark` also appends `unpark` when a park note was removed.
- `src/claude_kiro/cli/run.py` (as extended by prompt-free-runs):
  - Before spawning the child, for `--command implement|create`, it calls
    `eta.service.prepare_spool(main_root, spec)`. Because the child's `ck progress begin` cannot see the data-dir
    progress files, `prepare_spool` first closes any unfinished data-dir run of the spec with an `end` event of
    outcome `superseded` (3.9) and sends that run's done notice (8.7, D18) directly through `notify.send`, since
    the parent is outside the sandbox. It then creates the spool directory `<worktreeDir>/.ck-progress/`
    (mode 0700) with a `.gitignore` holding `*`, so the spool is never committed whatever the repository's own
    ignore rules say (3.8). It fits the model snapshot from the record store (which the parent can read) and
    writes it as `<spool>/<spec>.prepared-model.json` (0600, with its `digest`). It then sets the child env
    `CK_PROGRESS_DIR=<spool>`.
  - On each tick of the existing watcher thread (every 2 s, inside 3.8's 5 s limit), and once after the child
    exits, it calls `progress.mirror(spool, store_progress_dir, send=...)`. This appends new spool lines to the
    real progress file (mode 0600), copies each `<key>.model.json` and the latest `<key>.eta.json` sidecar into the
    data-dir progress directory when its bytes changed, and sends each `notify-pending` event once through
    `notify.send` with the notice's type (`NOTICE_TYPES[notice]`, 8.11), appending `notified`. When a spool file is
    fully mirrored after exit, it and its sidecars are deleted from the spool (3.8).
  - Time-only slip check (D14, 8.5): every 30th watcher tick (about once a minute; the test hook
    `CK_ETA_RECHECK_S` sets the interval in seconds), for each spool run with a `start` and no `end`, the watcher
    calls `eta.service.after_event(key, now=..., env=<env with CK_PROGRESS_DIR=spool>)` in the parent. It folds the
    spool file, simulates from the spool's model snapshot, and runs `notify_decision`. A due notice is sent
    directly through `notify.send` (the parent is outside the sandbox, and `CK_RUN_NOTIFY=parent` is set only for
    the child), and `notified` is appended to the spool file, so the child's own later decisions see it and the
    mirror copies it. A slip that comes from time alone, during a running task with no new event, is sent within
    about a minute.
- `src/claude_kiro/agents.py`: `DEFAULTS["run"]["eta_slip_minutes"] = 15` and `ROLE_KEYS["run"]` gains it. A value
  must be a whole number of 1 or more (`ck agents set run.eta_slip_minutes 20`) (8.5).
- `src/claude_kiro/hooks/_shared/spec_parser.py`: `SpecTask.requirements: List[str]` holds the criterion ids from
  the task's `**Requirements:**` field (for example `1.2`), deduplicated in order. This is the "acceptance criteria
  the task names" count for D4 (6.1).
- `src/claude_kiro/analytics/collect.py`:
  - `_build_record`, for implement records (workflow and agent mode), reads `<root>/specs/<spec>/tasks.md` once
    when it is readable. It adds `filesCount` and `criteriaCount` (int, or null when the task is not in tasks.md)
    to each `tasks[]` entry. These are local fields; `share.build_shareable` copies fields one by one and never
    copies them (6.2).
  - New pure `writer_for(record) -> Literal["agents", "workflow"]` (11.2).
  - `_Agent` gains `span: Optional[Tuple[Optional[int], Optional[int]]] = None`. When it is set, `_build_agent`
    uses it instead of calling `_timestamps` again (11.3).
- `src/claude_kiro/analytics/agentcollect.py`:
  - `collect_agent_run` reads `collect._timestamps` once per transcript into a dict. It sorts by the first
    timestamp and passes the pair as `_Agent.span` (11.3; records stay byte-identical).
  - `_existing_backfill(env) -> ExistingBackfill` uses `store.load_records_checked`. It never returns an empty list
    on a read failure (9.1).
  - `plan_backfill` stores `overlap_check` ("ok" | "unavailable"), `store_error` and `malformed_records` on
    `BackfillPlan`. A store error or any malformed record file makes `overlap_check == "unavailable"`: it keeps
    every group and checks no overlaps (9.2, 9.3).
  - `_collect_group` appends `{"agentId": None, "what": "outcome"}` when the group's session transcript is missing
    or unreadable and the record has no `outcome` entry yet (9.4).
- `src/claude_kiro/analytics/store.py`: new `load_records_checked(env) -> StoreRead`. It first calls
  `os.stat(runs_dir)`: `FileNotFoundError` (no store yet) means an empty store with no error; any other `OSError`
  (an untraversable data directory, for example) sets `error`. It never uses `Path.is_dir()`, which raises on
  Python 3.12 and returns False on 3.13+ for an untraversable parent. It then lists `runs/` with `os.scandir`, so a
  listing failure (chmod 000, `runs` being a regular file) also sets `error`. `glob` silently returns nothing on a
  listing failure. Record files that cannot be read or parsed, or hold no record schema, go into `malformed`.
  Existing `load_records*` are unchanged (9.1, 9.3).
- `src/claude_kiro/analytics/backfill.py`: new pure `overlap_guard(...)` (Decision Seams) (9.1, 9.2, 9.3).
- `src/claude_kiro/analytics/agentrun.py`: new pure `auto_id_log_line(raw_id, reason, now_iso)` (10.2).
- `src/claude_kiro/cli/stats.py`:
  - `collect --agents --json` inside the sandbox prints `{"run_id", "result": "failed", "error"}` and exits 1
    (10.1).
  - With `--auto`, an id refused by `decide_collect_args` (exit code 0) appends `auto_id_log_line(...)` to
    collect.log before printing the retry hint (10.2).
  - `_collect_backfill` applies `overlap_guard`, which refuses on a store error or a malformed record file and
    names it (9.1-9.3).
  - `_collect_all` turns an exception from `collect_open_manifests` into one failure,
    `("open-manifests", "open-manifest sweep failed: <Type>: <msg>")`, counted as failed (11.1). It picks the
    writer with `collect.writer_for(o.record)`, not `run_id.startswith("ag_")` (11.2).
- `src/claude_kiro/cli/main.py`: `import time` at module top replaces `__import__("time")` in `doctor` (D7). The
  module also registers the `eta`, `progress` and `status-line` commands, and attaches the two hidden maintainer
  commands from `cli/eta.py` to the existing `stats` group with `stats.add_command(...)`, so `cli/stats.py` is not
  edited for them.
- `plugin/workflows/spec-implement.js`, with the vendored `.claude/workflows/spec-implement.js` landed by hand in
  the same task:
  - New args: `progressRun` (string or null) and `etaLine` (string or null, from `ck progress begin`).
  - At start the script logs `etaLine || 'ETA unavailable'`, right after the existing `N task(s) remaining` line.
  - `taskPrompt`, `retryPrompt`, `revisePrompt`, `landFixPrompt` and `resolvePrompt` append to their claim command
    `--progress-run <key> --progress-event <kind> --progress-model <model or inherit> --attempt <k>`, when
    `progressRun` is set. The script picks `escalation` when the attempt runs on a model from the role's ladder,
    and `retry` otherwise.
  - `landPrompt` appends `--progress-run <key>`. `LAND` gains `eta: {type: 'string'}`, "the `eta` field from the
    JSON, copied as is". After each landing the script logs `res.eta || 'ETA unavailable'` (8.1, 8.2).
  - `planPrompt` appends `--progress-run <key> --failed <ids of failed tasks>` when there are any.
  - The scheduler, decisions block, labels and every other prompt stay unchanged. Without `progressRun` the script
    behaves exactly as today.
- `plugin/workflows/spec-create.js`, with the vendored copy: new args `progressRun` and `etaLine`. When
  `progressRun` is set, each phase's agent prompts gain two lines:
  - a first step: `ck -C <root> progress event <feature> phase-start --phase "<title>" --run <key>`. This is
    idempotent, and it closes the open phase with `phase-end`;
  - a last step: `ck -C <root> progress event <feature> step-end --phase "<title>" --run <key> --json`, with its
    `eta` copied into the result.

  `WRITE_RESULT`, `FINDINGS` and `VERDICTS` gain an optional `eta` string. When a phase's last agent returns, the
  script logs its `eta || 'ETA unavailable'`. At start it logs `etaLine`.
- `plugin/resources/without-workflow-tool.md`, with the vendored copy: agent mode runs the same commands itself.
  These are `ck progress begin` before the first Agent call, the claim and land flags in the prompts it builds, the
  `eta` line after each landing, `ck progress event <spec> task-failed` when a task fails,
  `approval-start/approval-end` around any question to me, and `ck progress end` at the end. For create, the
  session runs `phase-start`/`step-end` itself at each phase boundary (3.4).
- `plugin/skills/implement/SKILL.md` and `plugin/skills/create/SKILL.md`:
  - Before launching, the skill runs `ck progress begin <spec> --kind implement|create --mode workflow --json`. It
    passes `progressRun` and `etaLine` into the workflow args.
  - It wraps each AskUserQuestion of prompt-free-runs 7.6 in `ck progress event <spec> approval-start --reason
    question` and `... approval-end`.
  - After the run (and after `ck stats collect`), it runs `ck progress end <spec> --outcome <outcome>
    [--record <wf_id|ag_id>] --json` and quotes its line, including any `progress:` warning (3.7, 8.7, 8.8).
  - The implement skill's step 3 text names the new lint error with the suggested fix (13.3). Its single-task path
    (`/spec:implement <spec> X`) also runs `ck lint <spec> --json` and refuses to start when a `verify-file` entry
    has `task == X`, quoting that entry's message (13.3).
- Test harness, for D8 and 12.1-12.4:
  - `tests/js/load_workflow.mjs`: `runWorkflow(script, { agent, args, slots, clock })`. With `clock` it drives the
    run on that clock, and trace `t` is `clock.now()`. Without `clock`, behaviour is unchanged.
  - `tests/js/fake_project.mjs`: `project({..., sleep})` uses the injected `sleep` (default: the real one).
  - `tests/js/bridge.mjs`: `{op:'run', ..., clock: 'virtual' | 'real'}`, where `'virtual'` builds a `VirtualClock`.
  - `tests/landing_bridge.py`: `Bridge.run(project, args, slots, clock='virtual')`. The scheduler property tests use
    the virtual clock.
  - `tests/test_landing_scheduler_properties.py`: P-10 runs once per configuration and asserts
    `events(on) == events(off)` and `on.result == off.result` exactly. The re-run loop is gone.

### New Components

- `src/claude_kiro/progress.py` (new): the run progress file. Paths, run keys, locked append, tolerant read, the
  pure `fold(events) -> RunState`, active-run lookup, write-failure sidecar, and the spool mirror (3.1-3.9).
- `src/claude_kiro/eta/__init__.py` (new): package marker. It exports `estimate`, `after_event` and `render`
  lazily.
- `src/claude_kiro/eta/history.py` (new, pure apart from `load_model`/`load_defaults`): records into a model
  snapshot. It holds the task pools and match ladder (1.2, 1.3), the size fit (6.1-6.6), the create phase samples
  (2.1, 2.2, 2.5), the bundled defaults loader and builder (7.1-7.5), and the fixed key sets.
- `src/claude_kiro/eta/simulate.py` (new, pure): the seeded Monte Carlo for implement and create, conditioned on
  progress (1.1, 1.5, 2.1, 2.4, 4.3).
- `src/claude_kiro/eta/decide.py` (new, pure): the decision seams. These cover holds (blocked, waiting), state and
  slip, notifications, the line formats (status line, log line, notification text) and the cache check.
- `src/claude_kiro/eta/service.py` (new, I/O): `estimate(...)`, `after_event(...)`, `prepare_spool(...)`, the
  cache, and the time budget.
- `src/claude_kiro/cli/eta.py` (new): `ck eta <spec>` (a plain command, no subcommands), the `ck progress` group
  (`begin`, `event`, `end`), the hidden maintainer commands `eta_defaults` and `eta_backtest` (registered as
  `ck stats eta-defaults` and `ck stats eta-backtest`), and `ck status-line`.
- `src/claude_kiro/statusline.py` (new, stdlib only): the status-line entry point `ck-status-line`, registered in
  `pyproject.toml` `[project.scripts]`. It imports neither click nor the CLI, so it meets the 100 ms target (8.3,
  8.4).
- `src/claude_kiro/resources/eta_defaults.json` (new, generated): bundled cold-start aggregates,
  `ck-eta-defaults/1` (7.1, 7.2). It ships in the wheel with the package.
- `tests/js/virtual_clock.mjs` (new, test harness): `VirtualClock` with `now()`, `sleep(ms)` and
  `drive(promise)` (12.1).

## Public Interfaces

### CLI

- `ck eta SPEC [--kind implement|create] [--watch] [--json] [--seed N]`
  - Exit 0 for an estimate, `not started`, `nothing left to run`, blocked, waiting, done, a fallback after an
    unreadable progress file (4.7), and `no estimate: no history and no bundled defaults` (7.4).
  - Exit 2 for an unknown spec: no `specs/<spec>/` directory and no progress run for the name (4.7). A
    `specs/<spec>/` directory without tasks.md is a known spec, estimated as create (2.6).
  - Exit 1 when no implement estimate can be made: tasks.md missing for `--kind implement`, or tasks.md does not
    parse. This includes a file with no `### Task` blocks (1.6).
  - With no `--kind`, the kind is `implement` if tasks.md exists, else `create` (2.6).
  - Which form prints: no progress file for the spec gives the launch estimate plus `not started` (4.1); a latest
    run with an `end` event gives the ended block plus the launch estimate of the open tasks that remain, or
    `nothing left to run` (4.8); otherwise the live block, which is `stale` when the last event is more than
    `STALE_MIN` (120) minutes old (3.10).
  - `--watch` redraws when the progress file's size or mtime changes (polled every 1 s), and at least every 60 s.
    It exits 0 after printing the final block once an `end` event exists, or on Ctrl-C (4.4).
  - While `--watch` runs, about once a minute (`CK_ETA_RECHECK_S` overrides the interval) it calls
    `service.after_event(key, now=...)` on the live run, so a slip that comes from time alone is notified with no
    new event (8.5, D14). It skips this when the run's `start` event has `spool: true` (a `ck run` run), because
    `ck run`'s watcher already checks that run and owns its spool; the redraw still happens.
  - `--json` prints one `EtaView` (Data Models) (4.5).
  - Text output for an implement run in progress, matching the mockup:

    ```
    ck eta run-followups · implement · 12 open · 14 slots
    launched 15:02 (32 min ago) · last event 15:31 task 7 retry 1
    merged 4 · running 3 · waiting 5
    finish 17:20 – 17:35 – 17:58 (p10–p50–p90) · +4 min vs launch · on-track
    cost $38 – $61 (p10–p90)
    cause: task 7 retry 1
    critical: 7 → 9 → 12 → 13 (median 48 min)
    running: 7 retry 1 sonnet 12 min · 8 first opus 6 min · 10 first sonnet 3 min
    waiting: 9 on 7 · 11 on 8,10 · 12 on 9 · 13 on 12 · 14 on 13
    basis: 504 past tasks, 41 runs · size signal on · confidence normal
    ```

    The `not started` form adds the line `not started`. Its header shows `slowest:`, `safety-tagged:`,
    `retry rate` and `escalation rate` lines (1.4). The create form lists each phase with its median minutes and
    `✓` (done) or `▶` (running) (2.3). A blocked run replaces the `finish` line with `blocked: <reason>` and
    `remaining once unblocked: <p50> min`, and, when other tasks can still run, adds `runnable work done ~HH:MM`
    (5.1, 5.2). A stale run replaces it with `stale: no progress since HH:MM` (3.10).
- `ck progress begin SPEC --kind implement|create [--mode workflow|agents] [--json]`. This writes the model
  snapshot, simulates the launch estimate (4 s budget, 8.2), creates the progress file, and appends `start` (3.1).
  The snapshot comes from `<CK_PROGRESS_DIR>/<spec>.prepared-model.json` when `CK_PROGRESS_DIR` is set and that
  file exists with a valid digest (the `ck run` case, where the record store is unreadable); it is then copied to
  `<key>.model.json` and the record store is not read. Otherwise it is fitted from the record store and the
  bundled defaults. JSON: `{"run": key, "file": path, "etaLine": str|null, "estimate": EtaView|null}`. It exits 0
  even when progress cannot be written (`run: null`, `"progress": "failed: <reason>"`) (3.7). When an unfinished
  run of the spec exists, it is closed with `end` outcome `superseded` before the new file starts (3.9), and that
  run's done notice is dispatched like any notice (sent, or appended as `notify-pending` under
  `CK_RUN_NOTIFY=parent`) with outcome `superseded` (8.7, D18). This holds for a stale earlier run too. The `start`
  event records `spool: true` when `CK_PROGRESS_DIR` is set.
- `ck progress event SPEC KIND [--run KEY] [--task N] [--model M] [--attempt N] [--phase TITLE]
  [--reason R] [--json]`. KIND is one of `task-failed`, `approval-start`, `approval-end`, `phase-start`,
  `step-end`, `park`, `unpark`. It always exits 0. With `--json` it prints `{"ok": bool, "eta": str|null,
  "error": str|null}`. Without `--run` it uses `progress.active_run`. Test hook: when `CK_PROGRESS_TEST_KINDS=1` is
  set it also accepts the claim and landing kinds (`task-start`, `retry`, `escalation`, `revision`, `landfix`,
  `resolve`, `landing` with `--tasks/--merged/--red/--conflict/--protected`, `merge`, `hand-landing`), writing the
  same event the claim/land flags write. The Workflow-mode bridge tests use it to record the events a fake agent's
  claim and land commands carry (test-plan Test Infrastructure item 2); without the variable those kinds are refused
  with `ok: false`.
- `ck progress end SPEC [--run KEY] --outcome OUTCOME [--record RUN_ID] [--json]`. This appends `end`, runs the
  done notification (8.7), and prints the final block (4.8). It prints one warning line when the run's
  write-failure sidecar or notify outcomes show failures: `⚠ progress: N event(s) not written (<first reason>)`,
  `⚠ notification failed: <detail>` (3.7, 8.8). It always exits 0.
- `ck stats eta-defaults --out PATH [--min-runs 20]` (hidden, maintainer). This writes `ck-eta-defaults/1` from
  local history after `defaults_shape_ok` passes. With fewer runs it refuses: exit 1, nothing written (7.5).
- `ck stats eta-backtest [--json]` (hidden, maintainer; calibration NFR). It re-estimates each past implement and
  create run in local history from the records before it, and prints `{runs, inside, rate}`. Exit 0.
- `ck status-line` and the entry point `ck-status-line`. They read Claude Code's status-line JSON on stdin (uses
  `workspace.current_dir`, else `cwd`, else the process cwd). They print zero or more lines, leave out ended and
  stale runs, and always exit 0 (3.10, 8.3, 8.4).
- `ck worktree claim SPEC N [...] [--progress-run KEY --progress-event KIND [--progress-model M] [--attempt N]]`
- `ck worktree land SPEC N... [...] [--progress-run KEY]`. The JSON gains `"eta": str|null`.
- `ck plan SPEC [--exclude ...] [--progress-run KEY] [--failed IDS]`
- `ck lint SPEC [--json]`. The `verifyCycles` list gains entries with `"kind": "verify-file"` (Data Models).
- `ck agents set run.eta_slip_minutes N`

### Python

```python
# progress.py
def progress_dir(env: Optional[Mapping[str, str]] = None) -> Path      # CK_PROGRESS_DIR, else store.data_dir()/"progress"
def new_run_key(spec: str, now: datetime, token: Optional[str] = None) -> str   # "<spec>--20261030T150205Z-3f9a1c"
def run_file(key: str, env=None) -> Path                               # <progress_dir>/<key>.jsonl
def append(key: str, event: Mapping[str, Any], env=None) -> Optional[str]   # None ok, else the error text; never raises
def read_events(path: Path) -> Tuple[List[dict], int]                  # (events, skipped lines)
def fold(events: Sequence[Mapping]) -> RunState                        # pure
def effective_min(waits: Sequence[Tuple[float, Optional[float]]], t: float) -> float   # pure: t minus wait minutes before t (S12, Simulation)
def active_run(spec: str, root: Optional[Path] = None, env=None, *, now: datetime) -> Optional[str]
def runs_for_project(root: Path, env=None, *, now: datetime) -> List[Path]   # start event's root equals root, no end, not stale (3.10)
def is_stale(last_event_at: Optional[datetime], now: datetime) -> bool  # pure: no end and last event > STALE_MIN (120) min ago
def mirror(spool: Path, target: Path, *, send: Callable[[str, str], NotifyOutcome]) -> int   # lines + model/eta sidecars; send(message, notification_type)
def notice_lock(key: str, env=None) -> ContextManager[None]            # flock on <progress_dir>/<key>.lock around fold -> notify_decision -> send -> append notified

# notify.py (prompt-free-runs; D13 adds the keyword)
def send(command, message, cwd, timeout=10, *, notification_type: str = "ck_needs_input") -> NotifyOutcome   # sets CK_NOTIFY_TYPE

# eta/history.py
def fit_model(records: Sequence[Mapping], defaults: Optional[Mapping], *, tasks_md_reader: Callable[[str, str], Optional[Dict[str, Tuple[int, int]]]] = ...) -> Model
def load_defaults() -> Optional[dict]
def build_defaults(records: Sequence[Mapping], *, min_runs: int = MIN_DEFAULT_RUNS) -> DefaultsResult
def model_to_json(m: Model) -> dict ; def model_from_json(d: Mapping) -> Optional[Model]

# eta/simulate.py
def simulate_implement(graph: Graph, model: Model, state: RunState, *, now_min: float, n: int, seed: int) -> SimResult
def simulate_create(model: Model, state: RunState, *, now_min: float, n: int, seed: int) -> SimResult

# eta/service.py
def estimate(project_dir: Path, spec: str, kind: Optional[str] = None, *, now: datetime, seed: Optional[int] = None,
             env=None) -> EtaView
def after_event(key: str, *, now: datetime, budget_s: float = 3.0, env=None) -> Optional[str]   # the log line or None; also the time-only slip check (D14)
def send_done_for_superseded(key: str, *, now: datetime, env=None) -> None   # after begin/prepare_spool append end outcome superseded: the old run's done notice (D18)
def prepare_spool(main_root: Path, spec: str, env=None) -> Optional[Path]   # supersedes unfinished data-dir runs of spec (3.9) and sends their done notice (D18); spool dir with .gitignore and <spec>.prepared-model.json

# lint.py
def verify_file_cycles(tasks: Dict[str, SpecTask], config: dict) -> List[Dict[str, Any]]

# analytics
store.load_records_checked(env=None) -> StoreRead
backfill.overlap_guard(store_error: Optional[str], malformed: Sequence[str], dry_run: bool) -> GuardDecision
agentrun.auto_id_log_line(raw_id: str, reason: str, now_iso: str) -> str
collect.writer_for(record: Optional[Mapping]) -> Literal["agents", "workflow"]
```

Workflow args (both scripts): `progressRun: string | null`, `etaLine: string | null`. Result shapes are unchanged.

### Decision Seams

Every rule that allows, refuses, holds or suppresses something is a pure function: no I/O, no clock, no network,
and every input passed as an argument. The CLI, the land/plan/claim commands, the skill commands and the
status line call these functions and never re-implement the rule.

| Seam | Module (owner) | Signature | Decides |
|---|---|---|---|
| S1 `match_pool` | `eta/history.py` | `match_pool(model: Model, track: str, start_model: str, risk: str, min_n: int = MIN_MATCH) -> PoolMatch` | The widening ladder: `(track, model, risk)` → `(track, risk)` → `(track)` → `(all)` → bundled → none. Each step is used only with ≥ `min_n` (8) samples. `PoolMatch.step` names the widest step used (1.3). |
| S2 `size_factor` | `eta/history.py` | `size_factor(fit: SizeFit, task_size: Optional[int], sample_size: Optional[int]) -> float` | `max(task_size, fit.min_size) / max(sample_size or fit.median_size, fit.min_size)` raised to `fit.exponent`. Gives 1.0 when `fit.source == "none"` or `task_size` is None. Never 0 (6.4, 6.5). |
| S3 `fit_size` | `eta/history.py` | `fit_size(samples: Sequence[Tuple[str, float, Optional[int]]], bundled: Optional[SizeFit], min_sized: int = MIN_SIZED) -> SizeFit` | A local fit only with ≥ 30 sized samples. Exponent b = least squares of log(minutes) on log(size) within groups (group-centred), clamped to [0, 1.5]. Otherwise bundled, otherwise `source="none"` (6.1, 6.3, 6.4, 6.6). |
| S4 `conditional_sample` | `eta/simulate.py` | `conditional_sample(values: Sequence[float], elapsed: float, u: float) -> float` | Picks uniformly (by `u`) among `values` > `elapsed`. If there are none, returns `elapsed` plus the pool's p10. The result is never < `elapsed` (2.4, 4.3). |
| S5 `classify_hold` | `eta/decide.py` | `classify_hold(graph: Graph, state: RunState) -> Hold` | `none`, or `blocked` when (a) any task is parked as needs-input: the reason is `task N parked (needs input)` (each parked task) with the tasks waiting on it, `held` is the parked tasks and their dependents, and `whole_run` is True only when no open task outside `held` can still run (5.1, 5.2); or (b) the graph has a dependency cycle or a dependency on an unknown task, anywhere (1.7): `held` is the tasks on the cycle (or the dependent of the unknown id) and their dependents, with `whole_run` computed as in (a); or (c) a failed task holds every remaining unfinished task (`whole_run` True). A `hand-landing` task is never a hold: it is an approval wait (5.4). Each case gives a `reason` naming the cause and the held tasks (1.7, 5.1-5.3, 5.6). |
| S6 `decide_state` | `eta/decide.py` | `decide_state(*, ended: bool, started: bool, stale: bool, hold: Hold, wait_open: bool, p50_finish_min: Optional[float], waited_min: float, launch_p90_min: Optional[float]) -> str` | One of `done`, `stale` (3.10), `blocked`, `not-started`, `waiting` (an approval wait open), `slipping` (`p50_finish - waited > launch_p90`), `on-track`, checked in that order, so a launch estimate over a graph with a cycle or an unknown dependency is `blocked`, not `not-started` (1.7, 3.10, 5.1, 5.4, 5.7). Because `stale` comes before `blocked` and `waiting`, a run with an open wait, an open hand landing or a park that has had no event for more than 120 minutes is `stale` (D16). |
| S7 `notify_decision` | `eta/decide.py` | `notify_decision(view: NotifyInput, sent: Sequence[SentNotice], threshold_min: int) -> List[Notice]` | Slip: `level` is the largest whole number k with `p50 - waited - launch_p90 > k × threshold` (strictly more than, 8.5, so a slip of exactly one threshold sends nothing); send when `level` is ≥ 1 and greater than the highest slip level sent. The same rule runs whether the call follows an event or is a time-only check (D14). Blocked: send once per `reason_key` (sorted cause and tasks) for a failed-task or dependency hold; never for a park hold, whole-run or not, since prompt-free-runs' park message is the only one for a park (8.9, D15). Done: send once per run on `end`, whatever the outcome, including `superseded` (8.7, D18). Never a slip or blocked notice when the state is `waiting` (any open wait, hand landings included, D17) or `stale` (3.10, 8.5-8.7). `sent` counts both `notified` and `notify-pending` events, so a notice queued for `ck run` is never queued twice. Each `Notice` carries its type from `NOTICE_TYPES = {"slip": "ck_eta_slip", "blocked": "ck_eta_blocked", "done": "ck_run_done"}` (8.11, D13). |
| S8 `notice_text` | `eta/decide.py` | `notice_text(n: Notice) -> str` | `ck: <spec> ETA <HH:MM> (+<N> min, <cause>)`, `ck: <spec> blocked: <reason>`, or `ck: <spec> done <HH:MM>: <merged>/<total> merged (or spec written), <wall> min, $<usd>`. When the end outcome is not `completed`, the outcome replaces `done` (for example `ck: <spec> superseded <HH:MM>: 3/6 merged, 70 min`, 8.7). For a time-only slip the cause is `task N running` (the longest-running open task). Built only from the spec name, task ids, enum causes, times and numbers, so it never holds paths or text (8.10). |
| S9 `status_lines` | `eta/decide.py` (imported by `statusline.py` without click) | `status_lines(runs: Sequence[StatusInput]) -> List[str]` | `ck <spec> <merged>/<total> ETA <HH:MM> ~$<cost p50>` (the cached view's `cost.p50`, rounded to whole dollars; the ` ~$…` part is left out when it is null), or `ck <spec> create <phase> ETA <HH:MM>`. `blocked` or `waiting on you N min` replaces `ETA <HH:MM>`. A run with no cache shows `ETA ?`. Ended and stale runs are never passed in (3.10, 8.3). |
| S10 `eta_log_line` | `eta/decide.py` | `eta_log_line(what: str, view: EtaView, prev_p50: Optional[str]) -> str` | `ETA 17:34 (p90 17:50), critical 1→2→3` at start (create: `ETA 17:34 (p90 17:50), 7 phases`). Later lines are `<what> · ETA HH:MM`, with ` (+N)` / ` (-N)` when the median moved by a whole minute or more, or `<what> · blocked`, or `<what> · waiting on you N min` whenever a wait is open, including a hand landing that stays open while other tasks land (8.1, D17). |
| S11 `cache_fresh` | `eta/decide.py` | `cache_fresh(cache: Optional[Mapping], digest: EventsDigest, model_digest: str) -> bool` | Reuse only when the cache's events digest (byte size, line count, last line hash) and model digest equal the current ones (4.6). |
| S12 `effective_now` | `eta/decide.py` (built on the pure `progress.effective_min`, which the simulator also uses for start times) | `effective_now(state: RunState, now_min: float) -> Tuple[float, float]` | Returns `(now_eff, waited_min)`. Effective time is wall time minus the minutes during which at least one approval wait was open before it. While a wait is open, `now_eff` is the effective time at which the current wait began, so a simulation at `now_eff` gives the same remaining work at any wall time during the wait: frozen by construction, with no cache needed (5.4). After it closes, `now_eff = effective(now)` and `waited_min` includes it (5.5). |
| S13 `verify_file_cycles` | `lint.py` | `verify_file_cycles(tasks: Dict[str, SpecTask], config: dict) -> List[dict]` | For each impl task X that is not Done, each test path F named explicitly in X's `**Verify:**` commands (see S14) with at least one occurrence not narrowed (13.10), each test task T with F in its `**Files:**` that does not also verify X (skip when X in `T.verifies` or T in `X.verified_by`, 13.9), and each impl task Y verified by T (T in `Y.verified_by` or Y in `T.verifies`): an error when Y ≠ X and X is an ancestor of Y. The chain is the shortest dependency path Y → … → X. Unknown task ids and cycles are skipped, never raised (13.1, 13.4, 13.5, 13.7, 13.9, 13.10). The agent-mode-analytics task 8 shape still fires: task 8 had Verified by: None and an un-narrowed Verify. |
| S14 `verify_paths` | `lint.py` | `verify_paths(command: str, config: dict) -> List[Tuple[str, bool]]` | `(path, narrowed)` pairs for the tokens of a Verify command (`shlex.split`, falling back to `str.split` on a parse error), with `::selector` cut and a leading `./` stripped, kept when `is_test_path(token, config)` and the token holds no glob character (`*?[`) and does not end in `/` (13.6, Assumptions). `narrowed` is True when the token carried a `::` selector, or when the command holds a `-k` option (`-k EXPR`, `-kEXPR`, `-k=EXPR`), which narrows every file in that command (13.10). |
| S15 `overlap_guard` | `analytics/backfill.py` | `overlap_guard(store_error: Optional[str], malformed: Sequence[str], dry_run: bool) -> GuardDecision` | `store_error is None` and no malformed record file → write allowed, exit unchanged. Otherwise the cause is the store error, or `malformed record <file>` (each file named). With no dry run: write nothing, exit 1, message `cannot check backfill overlaps: <cause>; nothing written`. With a dry run: list the groups, exit 1, note `overlaps not checked: <cause>` (9.1, 9.2, 9.3). |
| S16 `writer_for` | `analytics/collect.py` | `writer_for(record: Optional[Mapping]) -> Literal["agents", "workflow"]` | `"agents"` iff `record.get("source") == "agents"` (11.2). |
| S17 `auto_id_log_line` | `analytics/agentrun.py` | `auto_id_log_line(raw_id: str, reason: str, now_iso: str) -> str` | `<now_iso> <id> invalid_id: <reason>`. The id is cut to 64 characters, with characters other than `[A-Za-z0-9_.:-]` replaced by `?`, so a log line can never be broken or flooded (10.2). |
| S18 `defaults_shape_ok` | `eta/history.py` | `defaults_shape_ok(payload: Any) -> Tuple[bool, List[str]]` | Accepts only the `ck-eta-defaults/1` tree: fixed keys (Data Models) and finite numbers in the quantile and rate positions. Any other key, any string value other than the schema id, any list of non-numbers, or any int field named like an id or time is refused with its path (7.2, 7.5). |
| S19 `start_model` | `eta/history.py` | `start_model(track: str, risk: str, complexity: Optional[str], roles: Mapping) -> str` | Port of the workflow's `codeOpts` start model, as a family: the safety tag starts at `implementer.risky_model` or the top of `escalate`; complexity routing with High starts at the first `escalate` model; otherwise the role's model. `inherit` maps to `inherit` (1.2). |
| S20 `merge_spool_line` | `progress.py` | `mergeable(line: str, seen_hashes: Set[str]) -> bool` | A spool line is mirrored once, keyed by its hash. Malformed lines are mirrored as they are and skipped by readers (3.5, 3.6). |

## Data Models

### Progress file (`ck-progress/1`)

The progress file is `<progress_dir>/<key>.jsonl`, mode 0600, in a 0700 directory. `progress_dir` is
`CK_PROGRESS_DIR` when set (set by `ck run` for its child: `<worktreeDir>/.ck-progress`, git-ignored by its own
`.gitignore`, the 3.8 exception), else `store.data_dir()/progress` (3.8). Each line is one JSON object of at most 4 KiB. Lines are appended with
`O_APPEND` under `fcntl.flock(LOCK_EX)` on the file, written with one `os.write` (3.5).

Common fields:

```python
{"schema": "ck-progress/1", "at": str,  # ISO-8601 UTC, second precision
 "kind": str}                            # one of EVENT_KINDS below
```

| kind | extra fields (all typed) |
|---|---|
| `start` | `run: str` (key), `runKind: "implement" \| "create"`, `spec: str`, `root: str` (main checkout, absolute), `mode: "workflow" \| "agents"`, `slots: int`, `slotsSource: "runtime" \| "default"`, `maxConcurrent: int \| None`, `open: List[str]` (open task ids, or create phase titles), `total: int`, `launch: LaunchSummary`, `model: str` (model snapshot file name), `modelDigest: str`, `spool: bool` (True when written under `CK_PROGRESS_DIR`, a `ck run` run; `ck eta --watch` then leaves the time-only slip check to `ck run`, D14) |
| `task-start` / `retry` / `escalation` / `revision` / `landfix` / `resolve` | `task: str`, `attempt: int \| None`, `model: str \| None` (family or `inherit`) |
| `merge` | `task: str` |
| `landing` | `tasks: List[str]`, `merged: List[str]`, `red: List[str]`, `conflict: List[str]`, `protected: List[str]`, `green: bool \| None` |
| `task-failed` | `task: str`, `reason: "gate" \| "review" \| "landing" \| "conflict" \| "worktree" \| "other"` |
| `hand-landing` | `task: str` (opens an approval wait with id `hand-<task>`, closed by that task's `merge` or by `end`; 5.4) |
| `park` | `task: str`, `parkKind: str` (the park note's `kind` enum), `parkedAt: str` |
| `unpark` | `task: str` |
| `approval-start` | `id: str` (8 hex), `reason: "question"` |
| `approval-end` | `id: str` |
| `phase-start` / `phase-end` / `step-end` | `phase: str` (one of `CREATE_PHASES`) |
| `end` | `outcome: str` (run outcome enum: `completed`, `partial`, `halted`, `refused`, `failed`, `superseded`), `merged: int \| None`, `total: int \| None`, `usd: float \| None` |
| `notify-pending` | `notice: "slip" \| "blocked" \| "done"`, `key: str`, `message: str` (S8 text); the mirror sends it with type `NOTICE_TYPES[notice]` (8.11) |
| `notified` | `notice: str`, `key: str`, `level: int \| None`, `outcome: "sent" \| "not_configured" \| "failed" \| "deferred"`, `detail: str \| None` |

No event holds prompt text, transcript content, test or requirement quotes, or file contents (3.8). `LaunchSummary`
is `{"finish": [iso, iso, iso] | None, "remainingMin": [p10, p50, p90] | None, "usd": [p10, p90] | None,
"confidence": str}`.

Sidecars in the same directory, all mode 0600:
- `<key>.model.json`, the model snapshot.
- `<spec>.prepared-model.json` (spool only), the snapshot `ck run` fitted before spawning the child; `begin`
  copies it to `<key>.model.json`.
- `<key>.eta.json`, the last `EtaView` plus `digest`.
- `<key>.errors`: one line per failed write, `<iso> <kind> <error class>: <message ≤ 200>`, written with best-effort
  `O_APPEND` (3.7).
- `<key>.lock`: an empty lock file. `progress.notice_lock` holds `flock(LOCK_EX)` on it while a process folds,
  decides, sends and appends `notified`/`notify-pending`, so two processes checking the same run (a writer's
  `after_event`, `ck eta --watch`, `ck run`'s watcher, D14) never send the same notice twice.

`RunState` (fold result, pure):

```python
@dataclass(frozen=True)
class TaskProgress:
    task: str
    status: Literal["pending", "running", "merged", "failed", "parked", "hand"]
    step: Optional[str]          # last claim kind: first|retry|escalation|revision|landfix|resolve
    model: Optional[str]
    attempt: Optional[int]
    first_start_min: Optional[float]   # minutes since launch of the first claim
    last_start_min: Optional[float]

@dataclass(frozen=True)
class RunState:
    run: Optional[str]; run_kind: Optional[str]; spec: Optional[str]; launch_at: Optional[str]
    launch: Optional[dict]               # LaunchSummary
    tasks: Dict[str, TaskProgress]
    phases_done: Tuple[str, ...]; phase_running: Optional[str]; phase_started_min: Optional[float]
    approval_open: Optional[Tuple[str, float]]  # (id, start minute) of the earliest open wait (question or hand landing)
    waits: Tuple[Tuple[float, Optional[float]], ...]  # wait intervals in wall minutes since launch (end None while open)
    waited_min: float                    # minutes during which at least one closed wait was open (overlaps merged)
    last_event_min: Optional[float]      # minutes since launch of the last event (for 3.10 staleness)
    last_event: Optional[Tuple[str, str]]       # (iso, short description, e.g. "task 7 retry 1")
    cause: Optional[str]                 # latest change cause (enum-built text)
    ended: Optional[dict]                # the end event
    sent: Tuple["SentNotice", ...]       # from `notified` and `notify-pending` events (S7 dedupe)
    skipped: int                         # malformed or unknown lines (3.6)
```

### Model snapshot (`ck-eta-model/1`, `<key>.model.json`)

```python
{"schema": "ck-eta-model/1",
 "digest": str,                                     # sha256 of the canonical JSON below
 "basis": {"tasks": int, "runs": int, "createRuns": int, "source": "local" | "mixed" | "bundled" | "none"},
 "bundledParts": List[str],                          # e.g. ["review wait", "size factor"] (7.3)
 "taskPools": {"<track>|<model>|<risk>": [[agentMin: float, retries: int, escalations: int,
                                           reviewWaitMin: float | None, landingWaitMin: float | None,
                                           usd: float | None, size: int | None], ...]},
 "bundledTasks": {"<track>|<risk>": TaskQuantiles},  # from defaults, used at the ladder's last step
 "size": {"exponent": float, "minSize": int, "medianSize": int, "source": "local" | "bundled" | "none"},
 "phases": {"<phase title>": {"presence": float, "minutes": List[float], "usd": List[float | None]}},
 "phaseSource": "local" | "bundled" | "none",
 "createTotals": List[float],                        # total wall minutes of create records without phase timing (2.5)
 "rates": {"retry": float | None, "escalation": float | None}}
```

The snapshot holds numbers grouped by fixed keys only. Track is `impl|test`, model is the family
(`haiku|sonnet|opus|fable|inherit|unknown`), risk is `normal|safety`, and phase is a title from `CREATE_PHASES`.
Record values map onto these keys as follows (real local records: most `riskTag` values are null, some tracks
are `unknown`, and `escalations` is a list of model names):
- `riskTag` null or any value other than `safety` → `normal`.
- track `unknown` (or any value other than `impl|test`) → the tuple goes into the pool keyed `unknown|<model>|<risk>`,
  which S1 never matches at the keyed steps; it is used only at the `all` step.
- `escalations` = `len(list)`; a non-list value → 0. `retries` = `attempts - 1` when `attempts` is an int ≥ 1, else 0.
It holds no ids, names, paths or timestamps, so it is safe in the spool directory. The task tuples come from
`tasks[]` of implement records with `ok` true. The model is the family of the task's first implementer/test-writer
agent row. `usd` is the sum of that task's agent `usd`, or None when any of those rows is unpriced. `size` is
`filesCount + criteriaCount`, or a count from the spec's tasks.md (6.3), or None. Phase samples come from create
records' `phases` (non-null minutes). `usd` comes from the agents' `usd` grouped by `phase`.

### Bundled defaults (`ck-eta-defaults/1`, `src/claude_kiro/resources/eta_defaults.json`)

```python
{"schema": "ck-eta-defaults/1",
 "quantiles": [0, 5, 10, ..., 95, 100],                # 21 fixed points
 "tasks": {"impl|normal" | "impl|safety" | "test|normal" | "test|safety":
           {"agentMinutes": [21 floats], "retryRate": float, "escalationRate": float}},
 "waits": {"reviewMinutes": [21 floats], "landingMinutes": [21 floats]},
 "usdPerAgentMinute": [21 floats],
 "phases": {"<CREATE_PHASES title>": {"presence": float, "minutes": [21 floats]}},
 "size": {"exponent": float, "minSize": int, "medianSize": int}}
```

Sampling from a quantile array uses inverse-CDF linear interpolation on a uniform draw. Missing parts make
`load_defaults()` return the parts it has. An unreadable or malformed file gives None (7.4). Constants:
`MIN_MATCH = 8`, `MIN_SIZED = 30`, `MIN_DEFAULT_RUNS = 20`, `N_IMPLEMENT = 1000`, `N_CREATE = 4000`,
`CREATE_PHASES` = the seven `meta.phases` titles of `plugin/workflows/spec-create.js`, in order.

### Estimator types

```python
@dataclass(frozen=True)
class Graph:                     # from cli.worktree._plan (ck plan --json)
    order: Tuple[str, ...]       # remaining, tasks.md order
    deps: Dict[str, Tuple[str, ...]]
    done: FrozenSet[str]
    chain: Dict[str, int]        # longest dependent chain (ready order: chain desc, then id)
    tracks: Dict[str, str]; risk: Dict[str, str]; complexity: Dict[str, Optional[str]]
    sizes: Dict[str, Optional[int]]          # len(files) + len(requirements) per task
    critical: Tuple[str, ...]
    cycles: Tuple[Tuple[str, ...], ...]; unknown_deps: Dict[str, Tuple[str, ...]]
    slots: int; slots_source: Literal["runtime", "default"]

@dataclass(frozen=True)
class PoolMatch:
    samples: Tuple[Tuple, ...]            # task tuples, or () for bundled
    quantiles: Optional[dict]             # TaskQuantiles when bundled
    step: Literal["track+model+risk", "track+risk", "track", "all", "bundled", "none"]

@dataclass(frozen=True)
class SizeFit:
    exponent: float; min_size: int; median_size: int; source: Literal["local", "bundled", "none"]

@dataclass(frozen=True)
class SimResult:
    finish_min: Tuple[float, float, float] | None     # p10, p50, p90 minutes from launch
    remaining_min: Tuple[float, float, float] | None  # from now
    usd: Tuple[float, float] | None                   # p10, p90; None when any open task's pool is unpriced (1.9)
    task_median_min: Dict[str, float]                 # per task, sampled duration median
    critical_median_min: Optional[float]
    widest_step: str
    phase_median_min: Dict[str, float]                # create

@dataclass(frozen=True)
class Hold:
    kind: Literal["none", "blocked"]
    reason: Optional[str]                  # e.g. "task 6 parked (needs input); holds 9, 12, 13"
    reason_key: Optional[str]              # stable key for S7
    held: Tuple[str, ...]                  # parked tasks and their dependents (or the tasks a failure/cycle holds)
    waiting_on_you: Tuple[str, ...]        # parked tasks and dependents
    whole_run: bool                        # True when no open task outside `held` can still run (5.2: no runnableDone); never sends a notice (8.9, D15)

@dataclass(frozen=True)
class NotifyInput:                         # S7 input; built by the service from the fold, the hold and the view
    spec: str
    state: str                             # a `decide_state` (S6) value
    p50_finish_min: Optional[float]        # minutes from launch to the finish p50; None when there is no finish
    waited_min: float                      # closed approval-wait minutes (S12)
    launch_p90_min: Optional[float]        # minutes from launch to the launch estimate's finish p90; None when unknown
    hold: Hold                             # S5 result
    cause: Optional[str]                   # latest change cause (enum-built text, S8 slip cause)
    outcome: Optional[str]                 # the end outcome; None until the run ends
    merged: Optional[int]; total: Optional[int]
    wall_min: Optional[float]              # wall minutes since launch
    usd: Optional[float]
    now_hhmm: str                          # local HH:MM of `now`, for S8 text

@dataclass(frozen=True)
class SentNotice:                          # from `notified` / `notify-pending` events
    kind: Literal["slip", "blocked", "done"]
    key: str
    level: Optional[int]                   # the slip level, else None

@dataclass(frozen=True)
class Notice:                              # S7 output, rendered by S8
    kind: Literal["slip", "blocked", "done"]
    level: Optional[int]                   # the slip level, else None
    key: str                               # dedupe key: "slip", the hold's reason_key, or "done"
    type: str                              # NOTICE_TYPES[kind]
    source: NotifyInput                    # the input S8 renders the text from
```

Simulation (pure). One run of `simulate_implement` uses `random.Random(seed)`. The default seed is
`int(sha256(spec, model digest, events digest)[:8], 16)`, so equal inputs give an identical result (1.5, NFR
stable output). Each of `n` passes works as follows:
- Effective time (S12): `(now_eff, waited) = effective_now(state, now)`. A stale run (3.10) is not simulated. Every start time is converted to effective
  minutes the same way, so a wait inserted before a start shifts nothing in effective time. The simulation runs
  wholly in effective minutes from `now_eff`, and wall finish = effective finish + `waited` + (minutes the open
  wait has run, while one is open). Remaining work = finish − now. So while a wait is open the remaining work does
  not move (5.4), and after it closes the finish range is later by exactly the waited minutes (5.5), given the
  same explicit seed (the default seed hashes the events, so it changes when a wait is added).
- Every open task draws one tuple through S1. Its minutes are scaled by S2. Running tasks draw through S4, with
  `elapsed = now_eff - first_start_eff`. When S5 gives `blocked` the simulation still runs over every open task,
  parked branches included, giving the remaining work once unblocked (5.1, 5.3); a second pass over the tasks
  outside `held` gives `runnableDone` when `whole_run` is False (5.2).
- Durations are slot time = agentMin × factor, then review wait + landing wait, which hold no slot (history waits
  already include queueing). When the tuple has no wait, it is sampled from the pool's own wait values, else from
  bundled waits.
- An event heap starts ready tasks in `ck plan` order (chain desc, id) while running tasks < `min(slots,
  maxConcurrent)`.
- Cost = tuple `usd × factor`. For bundled tuples it is minutes × a draw from `usdPerAgentMinute`.

Quantiles are taken over the passes. `simulate_create` sums S4/plain draws for the running phase and the unfinished
phases. Each phase is included with probability `presence`.

### `EtaView` (`ck eta --json`, schema `ck-eta/1`, also the cache body)

```python
{"schema": "ck-eta/1",
 "runKind": "implement" | "create", "spec": str, "run": str | None,
 "state": "not-started" | "on-track" | "slipping" | "blocked" | "waiting" | "stale" | "done",
 "progressFile": str | None,                     # the run's progress file (I/O field, not in the cache digest)
 "cached": bool,                                 # True when this view was reused from <key>.eta.json (4.6; not in the digest)
 "launchAt": str | None,                         # ISO-8601 UTC
 "now": str,
 "finish": {"p10": str, "p50": str, "p90": str} | None,      # ISO-8601 with local offset; null when blocked or stale
 "runnableDone": str | None,                     # p50 time the still-runnable work is done, only for a park with other work runnable (5.2); never the finish
 "remainingMinutes": {"p10": float, "p50": float, "p90": float} | None,  # for blocked: once unblocked (5.1, 5.3)
 "deltaVsLaunchMinutes": float | None,
 "cost": {"p10": float | None, "p50": float | None, "p90": float | None},   # null when unknown (1.9); p50 feeds the status line (8.3)
 "confidence": "normal" | "low" | "none",
 "basis": {"source": "local" | "mixed" | "bundled" | "none", "tasks": int, "runs": int,
           "widestMatch": str, "bundledParts": List[str], "sizeSignal": "local" | "bundled" | "off"},
 "slots": int, "slotsSource": "runtime" | "default",
 "open": int, "merged": int, "running": int, "waiting": int,
 "criticalPath": {"tasks": List[str], "medianMinutes": float | None},
 "slowest": List[{"task": str, "medianMinutes": float}],     # two, at launch (1.4)
 "safetyTagged": int, "retryRate": float | None, "escalationRate": float | None,
 "cause": str | None, "lastEvent": {"at": str, "what": str} | None,
 "tasks": List[{"task": str, "state": "pending" | "running" | "merged" | "failed" | "parked" | "hand" | "held",
                "step": str | None, "model": str | None, "minutes": float | None, "medianMinutes": float | None,
                "waitsOn": List[str]}],
 "phases": List[{"phase": str, "state": "done" | "running" | "pending", "medianMinutes": float | None}],
 "blocked": {"reason": str, "held": List[str]} | None,
 "waitingOnYouMinutes": float | None,
 "waitingOnYou": List[str],
 "ended": {"outcome": str, "at": str, "wallMinutes": float, "merged": int | None, "total": int | None,
           "usd": float | None, "vsLaunch": "below p10" | "within p10-p90" | "above p90",
           "next": EtaView | None,          # launch estimate of the open tasks that remain; None when none remain (4.8)
           "nothingLeft": bool} | None,
 "staleSince": str | None,                       # time of the last event when state is stale (3.10)
 "notes": List[str]}   # e.g. "low confidence: no local history", "live progress unavailable: <reason>",
                       #       "slot cap unreadable: using the default 14", "size signal not used"
```

The cache body digest covers every key except `now`, `cached` and `progressFile`.

```python
```

### Lint entry (in `verifyCycles`)

```python
{"kind": "verify-file", "task": X, "file": F, "testTask": T, "needs": Y,
 "chain": [Y, ..., X],        # dependency path, Y first
 "message": "Task X's Verify runs F, which Task T (test track) owns and which verifies Task Y; "
            "Task Y depends on Task X (Y → … → X), so Task X's gate can't pass before Task Y lands. "
            "Narrow Task X's Verify to test files that don't need Task Y."}
```

Existing import-based entries keep their exact keys (`task, verifiedBy, needs, module, message`) and carry no
`kind` (13.8).

### Analytics additions

```python
@dataclass(frozen=True)
class StoreRead:
    records: List[dict]
    malformed: List[str]        # file names of unreadable or schema-less records (9.3)
    error: Optional[str]        # "<path>: <OSError text>" when os.stat(runs/) fails other than FileNotFoundError,
                                # or os.scandir(runs/) fails (9.1)

@dataclass(frozen=True)
class ExistingBackfill:
    pairs: List[Tuple[str, FrozenSet[str]]]; malformed: List[str]; error: Optional[str]

@dataclass(frozen=True)
class GuardDecision:
    write: bool; exit_code: Optional[int]; note: Optional[str]
```

Local record task entries gain `filesCount: int | None` and `criteriaCount: int | None`. These are local only, and
`ck-run-analytics/1` is unchanged (6.2). The backfill JSON gains `"overlapCheck": "ok" | "unavailable"` and, when
non-empty, `"malformedRecords": [file names]` (9.3).

## Data Flow

```mermaid
sequenceDiagram
    participant S as Skill / agent-mode session
    participant P as ck progress (cli/eta.py)
    participant W as spec-implement.js
    participant T as task agent (ck worktree claim)
    participant L as land agent (ck worktree land)
    participant F as progress file + model + cache
    participant E as eta.service (history, simulate, decide)
    participant N as notify.send (run.notify_command)
    participant SL as ck-status-line

    S->>P: ck progress begin spec --kind implement --json
    P->>E: fit_model(records, defaults); simulate (4 s budget)
    E->>F: write <key>.model.json, append start (launch estimate)
    P-->>S: {run, etaLine}
    S->>W: Workflow(args + progressRun, etaLine)
    W->>W: log(etaLine or "ETA unavailable")
    W->>T: taskPrompt (claim ... --progress-run key --progress-event task-start)
    T->>F: append task-start (locked)
    W->>L: landPrompt (land ... --progress-run key)
    L->>F: append landing, merge/hand-landing
    L->>E: after_event: fold, classify_hold, simulate from snapshot, decide_state
    E->>F: write <key>.eta.json
    E->>E: notify_decision(view, sent, threshold)
    alt notice due and not under ck run
        E->>N: send(message) (10 s cap)
        E->>F: append notified
    else under ck run (CK_RUN_NOTIFY=parent)
        E->>F: append notify-pending (spool); ck run mirror sends it, appends notified
    end
    L-->>W: JSON with eta "landed 4,9 · ETA 17:35 (+1)"
    W->>W: log(eta or "ETA unavailable")
    SL->>F: read start/last events + eta.json (no simulation)
    S->>P: ck progress end spec --outcome completed --record wf_x
    P->>F: append end; done notification
```

Besides the event-driven path above, two long-lived processes run the same `after_event` on time alone, about once
a minute (D14, 8.5): `ck run`'s watcher, for the spool runs of its child, and `ck eta --watch`, for a run that is
not spooled. Every path decides and sends under `progress.notice_lock`, and every send passes the notice's type
(8.11). A new run that supersedes an unfinished one sends the old run's done notice from `ck progress begin` or
`prepare_spool` (D18).

## Error Handling

- **1.6** No tasks.md (with `--kind implement`) or tasks.md with no parsable task blocks: `❌ no implement
  estimate for <spec>: <tasks.md missing | tasks.md has no tasks>`, exit 1. Every task Done: `nothing left to
  run`, exit 0.
- **1.7** A dependency cycle or unknown dependency in the graph: S5 gives `blocked`, and the reason names the tasks
  (`dependency cycle 3 → 5 → 3`, `task 7 depends on unknown task 99`). No finish time. Exit 0.
- **1.8** `agents.resolve` raises `AgentConfigError`: slots = `runtime_agent_slots(...)`, `maxConcurrent` = None,
  `slotsSource: "default"`, plus the note `slot cap unreadable: using the default <n>`.
- **1.9** An open task's pool has no priced sample (and no bundled `usdPerAgentMinute`): `cost` p10/p90 are null,
  and the text shows `cost unknown`.
- **2.5** Create records with `phases` null or with all-null minutes are left out of phase samples. Their
  `wallMs` goes to `createTotals`, used only in the calibration backtest and the text note `checked against N
  total-only runs`.
- **3.6** `read_events` skips lines that are not JSON objects or whose `schema`/`kind` is unknown, and counts them
  in `RunState.skipped`.
- **3.7** `progress.append` catches `OSError`/`ValueError` and returns the error text. It then writes one line to
  `<key>.errors` (best effort). Writers never change their exit code or output apart from an optional
  `"progress": "failed: ..."` key. `ck progress end` prints `⚠ progress: N event(s) not written (<first
  reason>)` once.
- **4.7** Unknown spec (no `specs/<spec>/` directory and no progress run naming it): `❌ no spec named <spec> (looked
  in specs/ and .claude/specs/)`, exit 2. Unreadable progress
  file: the estimate is made from the graph alone (launch estimate), with the note `live progress unavailable:
  <reason>`, exit 0.
- **6.3 / 6.4** Unsized past task: it stays in the pools with `size: None`, and S2 uses the pool's median size.
  Fewer than `MIN_SIZED` sized samples: bundled fit. Neither: factor 1 and the note `size signal not used`.
- **7.4** `load_defaults()` returns None and history is empty: `EtaView.state` follows progress. `finish`,
  `remainingMinutes` and `cost` are null, `confidence: "none"`, and the text shows the critical path, the task
  count and `no estimate: no history and no bundled defaults`. Exit 0.
- **7.5** Fewer than `--min-runs` runs: `❌ refusing to write defaults: <n> runs in history, need <min>`, exit 1,
  no file written. A `defaults_shape_ok` failure: refuse, list the paths, exit 1.
- **8.2** `after_event` runs the simulation in batches of 100 passes and checks a monotonic deadline (3 s; `begin`
  uses 4 s for the launch estimate) between batches. Past the deadline, or on any exception, it returns None, and the command prints `"eta": null`. The
  script logs `ETA unavailable`, and the run continues unchanged.
- **8.4** `ck-status-line` wraps everything in `try/except BaseException`. An unreadable progress file, cache or
  stdin leaves that run out (or prints nothing). Exit 0, and nothing is ever written to stderr.
- **8.8** `run.notify_command` unset: `notify.send` returns `not_configured`, and `notified` is recorded with that
  outcome, so it is not retried. Failure or timeout: `failed` with detail, recorded, and named by `ck progress
  end`. The run is unaffected.
- **9.1** `load_records_checked` returns `error` → `overlap_guard(error, malformed, False)` → `❌ cannot check backfill
  overlaps: record store <path> unreadable (<err>); nothing written`. With `--json`: `{"error": ..., "results":
  [], "collected": 0, ...}`. Exit 1.
- **9.2** Dry run with an unreadable store: groups are listed as today, followed by `⚠ overlaps not checked:
  record store <path> unreadable (<err>)`. With `--json`: `"overlapCheck": "unavailable"`. Exit 1.
- **9.3** A malformed record file: `overlap_guard` refuses as in 9.1, with `❌ cannot check backfill overlaps:
  malformed record <file>; nothing written` (JSON `malformedRecords`), exit 1. With `--dry-run` the groups are
  listed, followed by `⚠ overlaps not checked: malformed record <file>`, exit 1.
- **9.4** The group's session transcript is missing or unreadable: the record is written with `outcome: null` and
  `{"agentId": null, "what": "outcome"}` in `missing`.
- **10.1** `--agents --json` in the sandbox: `{"run_id": id, "result": "failed", "error": "sandboxed: ck stats
  collect must run outside the sandbox: <home> is not readable here"}`, exit 1.
- **10.2** `--agents <bad id> --auto`: `auto_id_log_line` is appended to collect.log, and `⚠ could not collect
  <id>: <reason>; retry: ck stats collect --agents <id>` is printed. Exit 0. An unknown well-formed id is already
  logged by `collect_agents_auto`.
- **10.3** Unchanged: `❌ RUN_ID must look like ag_ ...` (JSON with `--json`), exit 1, via `decide_collect_args`'s
  non-auto path.
- **11.1** Sweep exception: failure `open-manifests: open-manifest sweep failed: <Type>: <msg>`, counted in
  `failed` and listed in `failures`. Exit 1 from `summarize_outcomes`. Workflow-run outcomes are still reported.
- **3.10** A run whose last event is more than 120 minutes old and has no `end`: `state: "stale"`, `finish` null,
  `staleSince` set, text `stale: no progress since HH:MM`. `after_event` (event-driven or time-only) does nothing
  for it, and S7 sends no slip or blocked notice for it. The status line leaves it out. Exit 0. This holds while
  a wait, a hand landing or a park is open (D16). A later `begin` that supersedes it still sends its done notice
  (D18).
- **13.7** Dependency cycles or unknown ids: `verify_file_cycles` walks ancestors with a seen-set and skips unknown
  ids. `dependency_cycles` and the unknown-dependency reporting stay as they are.

## Security, Performance, Migration

**Security and privacy.**
- Progress files, model snapshots, caches and error sidecars use `store._mkdir_private` and are written with mode
  0600 (`O_CREAT` with 0o600, then `chmod`).
- Events, snapshots and defaults carry no prompt text, transcript content, quotes or file contents.
- Bundled defaults pass S18 before they are written. The seeded leak test can additionally run `analytics/leak.py`
  over the file.
- Notification text is built only by S8.
- The spool directory under `ck run` holds the same content-free events and a numbers-only model. It is 0700, its
  files are 0600, and its own `.gitignore` (`*`) keeps it out of commits (3.8).
- ETA, lint, the status line and the progress commands make no network calls. Only the configured notification
  command runs (NFR no network).

**Performance.**
- The model snapshot is fitted once per run. Fitting from about 100 records is under 100 ms, and loading records
  dominates.
- `simulate_implement`, 1000 passes × 50 tasks, runs on a heap with plain tuples, in about 0.4 s in CPython.
  `simulate_create`, 4000 passes × 7 phases, takes under 0.1 s.
- `ck eta` reuses the cache when S11 holds (4.6).
- `ck-status-line` reads the start line, seeks to read the last 8 KiB for the latest events, and reads the cache.
  It never imports click, never folds whole files and never simulates. That is 3 runs × 1000 events in under
  100 ms.
- The status line looks up the project by path prefix against the start event's `root` and `<worktreeDir>`, with
  no git subprocess.

**Migration.**
- Records collected before this spec lack `filesCount`/`criteriaCount`. The fit reads the spec's tasks.md when it
  is still readable (6.3).
- Re-collecting such a run adds the two local fields, so `collect` reports it as `updated` once (11.4 lists 6.2 as
  an exception). The counts are read from tasks.md at collection time, so tasks.md is an input: records whose
  inputs, tasks.md included, are unchanged and that already carry the fields stay byte-identical.
- `run.eta_slip_minutes` defaults to 15, so no config change is needed.
- The workflows ignore missing `progressRun`/`etaLine`, so an older skill calling a newer script, or the reverse,
  still runs.
- Vendored copies of `spec-implement.js`, `spec-create.js` and `without-workflow-tool.md` are protected paths. The
  edits are made in the plugin sources and copied byte for byte in one task that lands by hand.

## Requirements Traceability

| Criterion | Design element |
|---|---|
| 1.1 | `service.estimate` → `cli.worktree._plan` graph; slots from `agents.runtime_info` + `run.max_concurrent`; `simulate_implement` (heap, ck plan order); finish and cost quantiles |
| 1.2 | Joint task tuples in `taskPools` (minutes, retries, escalations, review/landing waits, usd); S19 start model; S2 size scaling |
| 1.3 | S1 `match_pool` ladder, `MIN_MATCH`; `basis.widestMatch` |
| 1.4 | `EtaView` fields + text format (spec, open, slots, clock range, median minutes, cost, critical path, slowest two, safety count, rates, basis) |
| 1.5 | Seeded `random.Random`; default seed from input digests; `--seed` |
| 1.6 | `ck eta` exit rules; Error Handling 1.6 |
| 1.7 | S5 `classify_hold` (cycle, unknown dep) → `blocked` |
| 1.8 | `slotsSource: "default"` + note; Error Handling 1.8 |
| 1.9 | `SimResult.usd = None` when unpriced; `cost` nulls |
| 2.1 | `phases` from create records' agent-span `phases`; `simulate_create`; finish + cost |
| 2.2 | `presence` per phase in snapshot and defaults |
| 2.3 | Create text form with ✓/▶ and phase medians; `EtaView.phases` |
| 2.4 | `RunState.phases_done/phase_running`; S4 for the running phase |
| 2.5 | Records without phase timing → `createTotals` only |
| 2.6 | `ck eta` kind rule (`specs/<spec>/` with tasks.md → implement, without → create; `--kind` overrides) |
| 3.1 | `ck progress begin` → `start` event with kind, spec, launch, slots, launch estimate |
| 3.2 | Event kinds: claim flags (start, retry, escalation, revision), land (landing, merge, hand-landing), plan `--failed` (task-failed), park/unpark, approval-start/end, phase-start/phase-end/step-end, end |
| 3.3 | `ck worktree park`/`resolve`/`claim` append park/unpark; park note and `needsInput` unchanged |
| 3.4 | Workflow: skill + script command lines; agent mode: `without-workflow-tool.md` runs the same commands |
| 3.5 | `progress.append`: `flock` + `O_APPEND` + single write ≤ 4 KiB; spool mirror dedupe S20 |
| 3.6 | `read_events` skips malformed/unknown lines (`skipped`) |
| 3.7 | `append` never raises; `.errors` sidecar; `ck progress end` warning; writer exit codes unchanged |
| 3.8 | `progress_dir` in `store.data_dir()/progress`, 0600/0700; content-free event schema; `ck run` spool (0700, own `.gitignore`), mirrored every 2 s and after exit, deleted after the final mirror; `prepared-model.json` |
| 3.9 | New key per `begin`; unfinished prior run closed `superseded` (by `prepare_spool` in the parent under `ck run`); old files kept, never deleted |
| 3.10 | `progress.is_stale` (`STALE_MIN` 120); S6 `stale`, checked before `blocked`/`waiting` (D16); S7 silent; status line leaves it out |
| 4.1 | `ck eta` with no progress file: launch estimate + `not started` |
| 4.2 | `fold` + re-simulate remaining; text and JSON fields (since launch, last event, counts, delta, cause, critical, running, waiting); frozen by S12 while any wait is open, hand landings included (D17) |
| 4.3 | S4 conditional sampling on elapsed time for running tasks |
| 4.4 | `--watch` polling (file change, ≥ once a minute), time-only `after_event` about once a minute for non-spooled runs (`CK_ETA_RECHECK_S`), exit 0 on end or Ctrl-C |
| 4.5 | `EtaView` `ck-eta/1` |
| 4.6 | `<key>.eta.json` + S11 `cache_fresh` |
| 4.7 | Exit 2 unknown spec; unreadable progress → launch estimate + note, exit 0 |
| 4.8 | `EtaView.ended` (+ `next` launch estimate of remaining open tasks, or `nothingLeft`) + final text block |
| 5.1 | S5 blocked on any needs-input park; `finish` null; `remainingMinutes` = once unblocked, every open task included |
| 5.2 | S5 `held`/`whole_run`; `runnableDone` extra field, never `finish`; `waitingOnYou` listed |
| 5.3 | S5 blocked on failed task / cycle holding the rest; remaining once unblocked |
| 5.4 | `approval_open` (questions and `hand-landing`) → S6 `waiting` unless stale; S12 effective time freezes remaining work for the whole run while any wait is open (D17); S10 waiting form; S7 no slip |
| 5.5 | S12 effective time: finish = effective finish + waited minutes; S6 subtracts waited minutes |
| 5.6 | `unpark` / `merge` events refold → S5 `none` |
| 5.7 | S6 slipping rule (p50 − waited > launch p90) |
| 6.1 | `SpecTask.requirements`; size = files + criteria; S2/S3 |
| 6.2 | `collect._build_record` adds local `filesCount`/`criteriaCount`; not in `share` |
| 6.3 | `fit_model` `tasks_md_reader` fallback; unsized tuples kept, out of fit |
| 6.4 | S3 bundled fallback, factor 1 + note |
| 6.5 | S2 clamps to `min_size` |
| 6.6 | S3 exponent ≥ 0 fitted within groups; S2 monotone in size |
| 7.1 | `basis.source = "bundled"`, `confidence: "low"`, note `low confidence: no local history` |
| 7.2 | `ck-eta-defaults/1` fixed tree; S18 |
| 7.3 | Per-part fallback in `fit_model`; `bundledParts` |
| 7.4 | `confidence: "none"`, graph-only output, exit 0 |
| 7.5 | `ck stats eta-defaults` + `MIN_DEFAULT_RUNS` + S18 |
| 8.1 | `etaLine` logged at start; land/step-end `eta` copied and logged; S10; agent mode same |
| 8.2 | `after_event` 3 s budget (begin 4 s) → null → `ETA unavailable` |
| 8.3 | `ck-status-line` / `ck status-line`, S9 (`~$<cost p50>` from the cache, left out when unknown); reads files + cache only |
| 8.4 | Status line: nothing when no runs, silent skip, always exit 0 |
| 8.5 | S7 slip levels; `run.eta_slip_minutes` (default 15); triggers: every event, `ck run` watcher and `ck eta --watch` about once a minute (D14); never while waiting or stale |
| 8.6 | S7 blocked once per `reason_key`, failed-task and dependency holds only |
| 8.7 | `ck progress end`, and `ck progress begin` / `prepare_spool` for a `superseded` end (D18) → S7 done notice; S8 text with the outcome |
| 8.8 | `notify.send` outcomes recorded; `ck progress end` names failures |
| 8.9 | S7 never sends blocked for a park hold, whole-run or not (D15) |
| 8.10 | S8 builds text from ids, enums, times and numbers only |
| 8.11 | `notify.send(..., notification_type=)` sets `CK_NOTIFY_TYPE`; S7 `NOTICE_TYPES`; mirror passes the type; default `ck_needs_input` keeps prompt-free-runs unchanged (D13) |
| 9.1 | `store.load_records_checked`; `ExistingBackfill.error`; S15 refuse |
| 9.2 | S15 dry-run branch; `overlapCheck: "unavailable"`; exit 1 |
| 9.3 | `StoreRead.malformed` → S15 refuses, names the file (`malformedRecords`) |
| 9.4 | `_collect_group` outcome missing entry |
| 10.1 | `collect_cmd` sandbox branch prints JSON failure under `--json` |
| 10.2 | S17 `auto_id_log_line` appended on auto refusal |
| 10.3 | Unchanged non-auto path |
| 11.1 | `_collect_all` sweep failure entry |
| 11.2 | S16 `writer_for` |
| 11.3 | `_Agent.span`; one `_timestamps` read per transcript in `collect_agent_run` |
| 11.4 | Refactors preserve record bytes; only listed outputs (6.2 included, tasks.md as an input) change; `main.py` `import time` |
| 12.1 | `VirtualClock` in `tests/js/virtual_clock.mjs`, `runWorkflow({clock})`, fake `sleep` injection |
| 12.2 | P-10 single run per config, exact `events`/`result` equality |
| 12.3 | Shipped scripts untouched by the clock; clock only in test harness |
| 12.4 | Timer order from `(due, seq)` heap only, independent of load |
| 13.1 | S13 `verify_file_cycles` (Verifies / Verified by both directions) |
| 13.2 | Entry `task/file/testTask/needs/chain/message` with narrowing suggestion |
| 13.3 | Entries in `verifyCycles` → `ck lint` exit 1, `ck plan` blocked, skill step 3 and workflow start refusal; single-task skill path refuses when an entry's `task` is X |
| 13.4 | S13 fires only on ownership + dependency |
| 13.5 | S13 skips Done X |
| 13.6 | S14 `verify_paths` tokenising (`./`, flags, multiple files); `::` and `-k` mark the path narrowed |
| 13.7 | Cycle-safe walk; existing cycle / unknown reports kept |
| 13.8 | `kind: "verify-file"` entries beside unchanged import entries in `--json` |
| 13.9 | S13 skips T that also verifies X (D12) |
| 13.10 | S14 `narrowed` flag; S13 needs one un-narrowed occurrence of F (D12) |
