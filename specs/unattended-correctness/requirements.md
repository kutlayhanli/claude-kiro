# Feature: Unattended correctness (attributed gates, test repair, protected-file hooks, spec graph, per-role models, harness registry)

## Overview

`/spec:implement` orchestrates well, but it does not yet produce working software unattended. In the OpenCode
equity E2E (`management-track-record`, 94 tasks, 14 passes, 333 agents), 92 tasks merged, yet I made 14
test-track repair commits and one code fix by hand. The project's own suite ended with 17 failures and 3
errors. An outer session merged a protected file with raw git after the workflow refused it. Agents reported
one blocker per pass. Tests depended on CLI commands that unfinished tasks own, and every role ran on one
model. See [PLAN.md](PLAN.md) (Status: Decided, D1-D15). PLAN.md is the source of truth for scope and
decisions.

This feature closes those gaps:

1. **Attributed gates (D1, D2).** Every failing test is pinned to the tasks it covers through its TC ID. It
   is a regression once all of those tasks are Done, and excused while any is not. The periodic gate
   (every N merges) and the final gate run the whole suite with attribution. ck runs them directly, or a
   lean agent runs them under Claude Code. Each failing test gets one rerun to screen out flakes.
2. **Structured failures (D3).** Gates emit one failure list (test, TC ID, tasks, file, kind, message).
   Agents, retries, the fixer and the test-fix loop share it, and agents report every failure, not the
   first.
3. **Automatic test repair (D4).** When two agents agree a test can never pass, a test-fixer repairs it on
   its own branch. It must prove the fix against the blocked task, and a risky-tier reviewer checks the fix
   against the test-plan row before it lands. Each test gets one automatic fix.
4. **Test-track checks (D5).** Before a test-track task lands, ck checks four things: its new tests fail
   for the right reason, the project's fixture validator passes, a blocking cross-test review finds no
   conflict, and a row-fidelity script checks the tests against the test plan.
5. **Protected-file enforcement (D6).** Git hooks refuse protected changes on integration branches unless
   ck approved them, `ck worktree merge` gets the same check as `land`, and a run-end audit reports drift.
6. **Unified spec graph (D7).** One graph of tasks, test cases, files and CLI commands. It drives
   dependencies (including CLI dispatch), write-set scheduling, lint and relink, runtime missing-command
   edges, and D1's attribution.
7. **Per-role models under OpenCode (D8).** These are set in normal runs and per bench arm.
8. **Fixture hash lock (D9)** and **run-ID claim release (D10).**
9. **Harness registry (D11)**, with a guarded DeepSeek Harness (`dsh`) adapter as the last phase.
10. **Lean thin agents under Claude Code (D14).**
11. **Success bar (D12, D13).** Four equity E2E reruns end with the project's full suite green and zero hand
    repairs, and none costs more per merged task or takes more wall minutes per task than the first E2E.

Terms used below:

- **TC ID:** a test case identifier from the test plan (TC-n). Test names carry it as a `tcNNN_` prefix, and
  Verify lines select it with `-k "tcNNN_"`.
- **Attributed tasks of a test:** the impl tasks a failing test is pinned to (Story 1).
- **Regression:** a failing test whose attributed tasks are all Done, confirmed by one rerun (Story 2).
- **Excused failure:** a failing test with at least one attributed task not Done.
- **Test conflict:** an agent's report that a test can never pass as written.
- **Protected change:** a change to a path in ck's protected-path list that the project's landing
  allow-list does not cover.
- **Hand repair:** a commit, merge, ref update or file edit that I make, or that an agent makes outside
  `/spec:implement`'s own loops, to get a run's tasks or suite to pass. A decision the product asks me for
  (such as approving a protected landing) is not a hand repair.
- **Thin ck agent:** a workflow agent whose only job is to run one ck command and return its JSON.

## User Stories

### Story 1: Per-test attribution of failing tests (D1)
**As a** developer running a test-first spec unattended **I want** each failing test pinned to the tasks it
covers **So that** a real regression is not hidden in a suite that also covers unfinished tasks.

**Acceptance Criteria:**
- 1.1 WHEN `ck gate` runs with attribution, THE SYSTEM SHALL run every verify suite that the gate would
  excuse today, as well as the suites it already runs. It SHALL read the node ID of each failing test and
  map it to its TC ID (`tcNNN_` in the test name). It SHALL then map the TC ID to its attributed tasks: the
  impl tasks whose Verify selects that TC ID, and the impl tasks that the test task covering that TC ID
  verifies.
- 1.2 WHEN every attributed task of a failing test is Done, or is being gated as Done, THE SYSTEM SHALL
  count the failure as a regression candidate (subject to the rerun in Story 2).
- 1.3 WHEN at least one attributed task of a failing test is not Done, THE SYSTEM SHALL count the failure as
  excused. It SHALL list the failure with the unfinished tasks that excuse it, and it SHALL not let that
  failure make the gate red.
- 1.4 WHEN a failing test has no TC ID in its name, THE SYSTEM SHALL attribute it through its file: the test
  task that owns the file and the impl tasks that test task verifies. The failure SHALL be marked as
  attributed by file.
- 1.5 WHEN a failing test has neither a TC ID nor an owning test task, THE SYSTEM SHALL count it as a
  regression candidate marked "unattributed". No unfinished task can excuse it.
- 1.6 WHEN a test file fails to import or collect, so no test node exists, THE SYSTEM SHALL attribute the
  failure by file as in 1.4, with kind `collection` or `import` (Story 3).
- 1.7 WHEN a verify command's runner is not pytest, THE SYSTEM SHALL attribute its failures at file level,
  if the runner's output names the failing files. WHEN neither failing nodes nor failing files can be read,
  THE SYSTEM SHALL fall back to today's per-test-task rule for that command and add a warning saying
  attribution was unavailable for it.
- 1.8 WHEN a TC ID in a failing test's name appears in no task's Verify selector and in no test task's
  coverage, THE SYSTEM SHALL fall back to file attribution (1.4) and name the unknown TC ID in a warning.
- 1.9 WHEN `ck lint` runs on a spec, THE SYSTEM SHALL warn about each test function in the spec's test files
  that has no TC ID in its name, and say which test task owns the file.
- 1.10 WHEN the same suite results, the same task list and the same task statuses are given twice, THE
  SYSTEM SHALL produce the same attribution, in the same order, both times.
- 1.11 WHEN `ck gate` runs without attribution, THE SYSTEM SHALL behave as it does today.

### Story 2: Attributed periodic and final gates, run without a full agent (D2)
**As a** developer **I want** the whole suite checked with attribution every N merges and at the end
**So that** regressions surface while the spec is still in progress, at a bounded cost.

**Acceptance Criteria:**
- 2.1 WHEN the run reaches every Nth merge (N configurable, default 10) and when it reaches its final gate,
  THE SYSTEM SHALL run `ck gate --attribute --json` on the spec.
- 2.2 WHEN the run's host can execute ck itself (OpenCode, and any harness that provides direct ck calls),
  THE SYSTEM SHALL run the attributed gate directly, with no agent.
- 2.3 WHEN the host cannot execute ck itself (Claude Code), THE SYSTEM SHALL run the attributed gate through
  a thin ck agent (Story 19). That agent SHALL return ck's JSON unchanged.
- 2.4 WHEN a regression candidate is found, THE SYSTEM SHALL rerun that failing test once. It SHALL count
  the test as a regression only if the rerun also fails. A test that passes on rerun SHALL be reported as
  flaky, SHALL not make the gate red, and SHALL not be sent to the fixer.
- 2.5 WHEN excused failures are found, THE SYSTEM SHALL not rerun them.
- 2.6 WHEN an attributed gate has one or more regressions, THE SYSTEM SHALL count the gate as red and send
  the structured regression list (Story 3) straight to the fixer. The recheck after each fix SHALL also be
  an attributed gate run as in 2.2 or 2.3.
- 2.7 WHEN the fixer ladder ends and the attributed gate is still red, THE SYSTEM SHALL handle it as a red
  gate is handled today (the run reports the red target and the regressions that remain).
- 2.8 WHEN an attributed gate has no regressions, THE SYSTEM SHALL count it as green. It SHALL record in the
  run's results its flaky tests, its excused failures as counts per excusing unfinished task, and the path of
  ck's full gate report, which lists each excused failure with the unfinished tasks that excuse it.
- 2.9 WHEN the attributed gate cannot run or prints no parseable JSON (ck missing, crash, timeout, agent
  error), THE SYSTEM SHALL not count the gate as green. It SHALL record a gate error with the reason in the
  run's results.
- 2.10 WHEN a landing batch is gated, THE SYSTEM SHALL keep the landing gate task-scoped, as today.
- 2.11 WHEN the run ends with every task Done, THE SYSTEM SHALL have run a final attributed gate after the
  last merge, in which no failure is excused.
- 2.12 WHEN a thin ck agent runs an attributed gate (2.3), THE SYSTEM SHALL have ck print a compact form of its
  result for the agent to copy unchanged: the regressions, unattributed failures and flaky tests in full, the
  excused failures as counts per excusing unfinished task, any failing non-command check, and the path of a
  file holding ck's full gate JSON (3.1, never truncated). The full JSON SHALL be written before the compact
  form is printed.
- 2.13 WHEN a task parked for my approval of a protected change (9.8) has been landed by that approval, THE
  SYSTEM SHALL continue the run in a later pass of the same spec started with the same run ID: that pass SHALL
  treat the approved task as Done, start its dependents, and, once every task is Done, run the final attributed
  gate of 2.11 after the last merge.

### Story 3: One structured failure format (D3)
**As a** developer **I want** every gate failure reported as one consistent list **So that** a file with
several bad tests is fixed in one pass, whatever model runs the agent.

**Acceptance Criteria:**
- 3.1 WHEN `ck gate --json` runs, THE SYSTEM SHALL emit a `failures` list with one entry per failing test
  node (or per failing file where 1.6 or 1.7 applies). Each entry SHALL give the test node, the TC ID (or
  null), the attributed tasks, the file, the kind, and a short message.
- 3.2 THE SYSTEM SHALL use exactly these kinds: `assertion`, `import`, `collection`, `timeout`, `error`. A
  failure it cannot classify SHALL be `error`.
- 3.3 WHEN a gate passes, THE SYSTEM SHALL emit an empty `failures` list. It SHALL never omit the field under
  `--json`.
- 3.4 WHEN `--attribute` is given, THE SYSTEM SHALL also mark each failure as regression, excused, flaky or
  unattributed.
- 3.5 WHEN a failure message is longer than the message limit (NFR), THE SYSTEM SHALL truncate it and mark
  it as truncated. It SHALL never drop the entry.
- 3.6 WHEN a task agent runs its Verify, THE SYSTEM SHALL have the agent run the whole Verify without stopping
  at the first failure. The agent SHALL return the failure list that ck produced, not a list it wrote
  itself.
- 3.7 WHEN an agent reports tests that can never pass, THE SYSTEM SHALL accept a `testConflicts` list (one
  entry per test: file, test, criterion, reason, fix) in place of the single `testConflict`. A result that
  still carries a single `testConflict` SHALL be read as a list of one.
- 3.8 WHEN a task is retried, THE SYSTEM SHALL pass the previous attempt's structured failure list to the
  retry, in place of free text trimmed to 3000 characters. WHEN the list has more than 50 entries, the
  prompt SHALL carry the first 50 and the count of the rest per file.
- 3.9 THE SYSTEM SHALL use this one failure format in task gates, landing gates, attributed gates, retries,
  the fixer, the recheck and the test-fix loop.
- 3.10 WHEN `ck gate --json` output is read by an existing consumer, THE SYSTEM SHALL still provide every
  field it provides today. The new fields are additive.

### Story 4: Automatic repair of a test two agents agree can never pass (D4)
**As a** developer **I want** a confirmed bad test repaired automatically, with proof and review **So that**
runs no longer stop for hand repairs of tests.

**Acceptance Criteria:**
- 4.1 WHEN a task's test conflict is confirmed by a second agent (today's confirmation step), or a blocking
  cross-test finding is raised (Story 7), THE SYSTEM SHALL start a test-fixer agent in the test-writer
  role, using the test-writer's model and effort. The test-fixer works on a test-fix branch created from
  the integration branch and receives the structured conflict entries.
- 4.2 WHEN the test-fixer finishes, THE SYSTEM SHALL check its proof: each fixed test either passes against
  the blocked task's branch, or fails there only for a reason the blocked task still has to address. The
  fix SHALL not land without that proof.
- 4.3 WHEN the proof holds, THE SYSTEM SHALL have a reviewer at the risky tier (the reviewer's risky model
  and effort) check the fix's diff against the test-plan row of each changed test. The reviewer checks that
  no test was weakened and that the requirements document and existing test-plan rows were not edited.
- 4.4 WHEN the reviewer approves, THE SYSTEM SHALL land the fix through `ck worktree land`, with its gate and
  protected-file checks. It SHALL then retry the blocked task, and release the dependents that were waiting
  on it.
- 4.5 WHEN the fix's diff changes anything other than test files and test fixtures, or edits the requirements
  document or an existing test-plan row, THE SYSTEM SHALL refuse the fix before review and send the
  conflict to me.
- 4.6 WHEN the proof fails or the reviewer rejects the fix, THE SYSTEM SHALL not land it. It SHALL send the
  conflict to me (needs-input) with the proof result or the review findings.
- 4.7 WHEN the test-fixer concludes the test is right and the blocked task's code is wrong, THE SYSTEM SHALL
  make no test change and retry the blocked task, giving it that finding as a structured failure.
- 4.8 WHEN a test that has already had one automatic fix in this spec is reported in a confirmed conflict
  again, THE SYSTEM SHALL not start the test-fixer. It SHALL send the conflict to me. The one-fix count is
  kept per test node, and a later run of the same spec respects it.
- 4.9 WHEN several confirmed conflicts name tests in the same file, THE SYSTEM SHALL give them to one
  test-fixer run, not one run per test.
- 4.10 WHEN a test-fix lands or is sent to me, THE SYSTEM SHALL record in the run's results the tests
  changed, the proof outcome, the review verdict, and the blocked task.
- 4.11 WHEN a test-fix has no blocked task (it comes from a cross-test finding, 7.2, whose verified impl tasks
  are held by 7.4 and have no branch), THE SYSTEM SHALL run the proof of 4.2 against the integration branch
  with the fix applied: each fixed test passes, or fails only as "not built yet" (5.2). The fix SHALL not land
  without that proof, and the record of 4.10 SHALL name the held tasks in place of a blocked task. After the
  fix lands, THE SYSTEM SHALL release the held tasks.

### Story 5: Test-track tests fail for the right reason (D5.1)
**As a** developer **I want** new tests to fail only because the feature is not built yet **So that**
invalid fixtures and wrong values are caught before an implementer ever sees them.

**Acceptance Criteria:**
- 5.1 WHEN a test-track task is gated and the tasks its tests verify are not all Done, THE SYSTEM SHALL run
  its new tests against the integration branch with the task's test files applied. Each failure SHALL be
  classified as "not built yet" or not.
- 5.2 THE SYSTEM SHALL count these as "not built yet": a missing module or importable name from the code
  under test, a missing CLI command or unknown subcommand, an unrecognized argument or invalid choice of the
  project's CLI, and an explicit not-implemented error.
- 5.3 WHEN any failure is not "not built yet" (for example an assertion error on fixture data, or a schema
  or validation error raised while loading a fixture), THE SYSTEM SHALL keep the task from landing. It SHALL
  send the task back to the test writer with the D3 failure list, marking which entries failed the check.
- 5.4 WHEN a failure's class is unclear, THE SYSTEM SHALL count it as "not built yet" and report it as
  unclear in the gate's checks. Unclear failures do not block.
- 5.5 WHEN a new test already passes against the integration branch, THE SYSTEM SHALL report it as a
  warning. It does not block.
- 5.6 WHEN every task the test-track task verifies is already Done, THE SYSTEM SHALL require its tests to
  pass, as today, and not apply the "not built yet" rule.
- 5.7 WHEN a failure is an `AssertionError` raised by an `assert` in the test file, THE SYSTEM SHALL classify it
  from the failing assert expression. WHEN that expression, following the values it uses back through
  assignments in the same test function, reaches no name imported from the project's own modules, no pytest
  fixture argument and no call into project code (it compares only literals or data loaded from files, such as
  fixture files), THE SYSTEM SHALL count it as not "not built yet" (5.3), because the unbuilt feature cannot be
  its cause. WHEN the expression reaches project code, or cannot be parsed, THE SYSTEM SHALL count it as
  unclear (5.4).

### Story 6: Project-declared fixture validation (D5.2)
**As a** developer **I want** my project's own fixture or schema validator run on test-track gates **So
that** ck catches invalid fixtures while staying project-agnostic.

**Acceptance Criteria:**
- 6.1 WHEN the project's ck config declares a test-track validation command, THE SYSTEM SHALL run it on
  every test-track gate, in the task's worktree.
- 6.2 WHEN the command exits non-zero, THE SYSTEM SHALL fail the gate. The check SHALL show the command
  and the tail of its output, and the task SHALL go back to the test writer.
- 6.3 WHEN the command exceeds the gate's verify timeout, THE SYSTEM SHALL fail the check as a timeout.
- 6.4 WHEN no validation command is declared, THE SYSTEM SHALL skip the check and gate as before.
- 6.5 WHEN the declared value is not a string or a list of strings, THE SYSTEM SHALL report a config error
  that names the key. It SHALL not run the gate as if the key were absent.
- 6.6 WHEN an impl-track task is gated, THE SYSTEM SHALL not run the test-track validation command.

### Story 7: Blocking cross-test check (D5.3)
**As a** developer **I want** test files checked against each other by default, with findings that block
**So that** contradictory tests are repaired before implementers chase them.

**Acceptance Criteria:**
- 7.1 WHEN no config sets the cross-test option, THE SYSTEM SHALL treat it as on.
- 7.2 WHEN the cross-test check reports a finding, THE SYSTEM SHALL treat it as blocking, like a review
  finding. It SHALL send the finding to the test-fixer (Story 4) as a test conflict on the tests it names.
- 7.3 WHEN the cross-test option is set to false in global or project config, THE SYSTEM SHALL skip the
  check.
- 7.4 WHILE a cross-test finding is open, THE SYSTEM SHALL not start impl tasks that the tests it names
  verify.
- 7.5 WHEN the cross-test check finds nothing, THE SYSTEM SHALL proceed as today.

### Story 8: Row-fidelity check of tests against the test plan (D5.4)
**As a** developer **I want** a script to check each test's concrete values against its test-plan row and
the design's data models **So that** swapped thresholds, wrong enums and wrong dates are caught exactly,
and the reviewer sees only what the script could not check.

**Acceptance Criteria:**
- 8.1 WHEN a test-track task is gated or reviewed, THE SYSTEM SHALL extract from each new test the literal
  values, enum members, dates and numeric thresholds it asserts or feeds in. It SHALL compare them with the
  test's test-plan row (found by TC ID) and with the design's data models.
- 8.2 WHEN a value contradicts its row or the data model (for example an enum member the model does not
  define, or a threshold different from the row's), THE SYSTEM SHALL report a mismatch giving the test, the
  value, and the expected value or allowed set. The task SHALL go back to the test writer with the D3
  failure list.
- 8.3 WHEN a value or row cannot be parsed, THE SYSTEM SHALL pass only those unparsed items to the test-task
  reviewer for its row-fidelity check. Values the script checked SHALL not be sent to the reviewer.
- 8.4 WHEN a test has no TC ID, or its TC ID has no test-plan row, THE SYSTEM SHALL report it as unmatched
  and pass it to the reviewer.
- 8.5 WHEN every value parses and matches, THE SYSTEM SHALL send the reviewer no row-fidelity items.
- 8.6 WHEN the check runs, THE SYSTEM SHALL not modify the test plan, the design or the tests.

### Story 9: Protected-file hooks that plain git cannot bypass (D6)
**As a** developer **I want** protected changes refused at the git level on integration branches **So
that** an agent with a shell cannot merge a refused branch with raw git.

**Acceptance Criteria:**
- 9.1 WHEN ck creates or reuses an integration worktree, or a run starts, THE SYSTEM SHALL ensure its git
  hooks are installed for the repository's worktrees.
- 9.2 WHEN any ref update would move an `integrate/*` ref to a commit that adds protected changes not covered
  by the project's landing allow-list, THE SYSTEM SHALL refuse the update unless ck issued a one-shot
  landing token for that exact update. This also covers `--no-verify` merges and direct ref updates.
- 9.3 WHEN a landing token has been used once, THE SYSTEM SHALL refuse any further update that presents
  it. A token SHALL be valid only for the ref and target commit it was issued for.
- 9.4 WHEN a ref other than `integrate/*` is updated, THE SYSTEM SHALL let the update pass without the
  protected-change check, within the hook-cost target (NFR).
- 9.5 WHEN a merge commit is being created on an integration branch with protected changes not covered by
  the allow-list, THE SYSTEM SHALL refuse it earlier with a readable message. The message SHALL name the
  files and say how the change can be approved.
- 9.6 WHEN `ck worktree merge` merges a branch, THE SYSTEM SHALL apply the same protected-change check as
  `ck worktree land`. It SHALL refuse with state PROTECTED, naming the files.
- 9.7 WHEN `ck worktree land` or `ck worktree merge` passes its protected-change check (no protected changes,
  or all covered by the allow-list), THE SYSTEM SHALL issue the landing token its own ref update needs, so
  that allowed landings succeed with the hooks installed.
- 9.8 WHEN a protected change needs my approval, THE SYSTEM SHALL ask me through the product's
  needs-input or question path. On my approval, it SHALL land the change with a token. The approval SHALL
  need input from me that an agent's non-interactive shell cannot supply.
- 9.9 WHEN the repository already has hooks (its own hooks directory, a configured hooks path, or a
  pre-commit setup), THE SYSTEM SHALL chain to them rather than replace them. A refusal from the project's
  own hook SHALL still refuse.
- 9.10 WHEN hooks cannot be installed (for example a read-only git metadata directory), THE SYSTEM SHALL
  report that at run start and in the run's results, and make the run-end audit (Story 10) report it too.
- 9.11 WHILE the run executes inside the ck run sandbox, THE SYSTEM SHALL keep the hooks effective. An
  automated test SHALL show a raw `git merge` of a protected change into an integration branch being
  refused inside that sandbox.
- 9.12 WHEN an `integrate/*` ref is created, including re-created after a delete, THE SYSTEM SHALL compare the
  protected files of the new commit with ck's last approved protected state for that ref, and refuse the
  creation when they differ (or when no approved state exists) unless ck issued a one-shot landing token for
  that exact update (9.3). Deleting an `integrate/*` ref SHALL pass, because it lands nothing; the creation that
  follows is checked.

### Story 10: Run-end protected-file audit (D6)
**As a** developer **I want** the run to report any protected-file drift at its end **So that** whatever
gets past the hooks (such as an agent removing them) is still caught.

**Acceptance Criteria:**
- 10.1 WHEN a run ends (any outcome), THE SYSTEM SHALL compare the protected files on the integration branch
  with ck's last approved state. It SHALL report each file added, changed or deleted without approval.
- 10.2 WHEN the hooks are missing, altered or not active at run end, THE SYSTEM SHALL report that in the
  audit.
- 10.3 WHEN the audit finds no drift, THE SYSTEM SHALL say so in the run's results.
- 10.4 WHEN a protected change was landed with a token (allow-list or my approval), THE SYSTEM SHALL record
  it as approved, so the audit does not report it.
- 10.5 WHEN the audit cannot read the approved state, THE SYSTEM SHALL report the audit as incomplete. It
  SHALL not report the run as clean.

### Story 11: Unified spec graph (D7)
**As a** developer **I want** ck to build one graph of tasks, test cases, files and CLI commands **So
that** dependency detection, scheduling and attribution all agree.

**Acceptance Criteria:**
- 11.1 WHEN ck builds the graph for a spec, THE SYSTEM SHALL include nodes for tasks, test cases (TC-n), test
  files, source files and CLI commands.
- 11.2 THE SYSTEM SHALL include these edges: depends (task to task), verifies (test case to task, from Verify
  `-k` selectors and Verifies fields), owns (task to file, from Files lists), provides (task to command),
  invokes (test file to command, scanned from test code) and imports (today's import scan).
- 11.3 WHEN a task's provided commands can be inferred (for example from the design's CLI table and the files
  the task owns), THE SYSTEM SHALL add provides edges. WHEN an impl task has a `**Provides:**` field, THE
  SYSTEM SHALL use it, and it SHALL take precedence over inference.
- 11.4 WHEN a test file invokes the project's CLI with a command path (for example `mtr thesis new`), THE
  SYSTEM SHALL add an invokes edge to that command node.
- 11.5 WHEN I ask ck for a spec's graph, THE SYSTEM SHALL print its nodes and edges in machine-readable form,
  in a stable order.
- 11.6 WHEN D1 attribution maps a test to tasks, THE SYSTEM SHALL use the graph's verifies and owns edges.
  Attribution and dependency detection SHALL not use separate mappings.
- 11.7 WHEN tasks, test files or code change between two builds, THE SYSTEM SHALL reflect the change in the
  next build. It SHALL never serve a graph older than the files it reads.
- 11.8 WHEN a source the graph reads cannot be parsed (a malformed task list, or a test file with a syntax
  error), THE SYSTEM SHALL name the source and the problem. It SHALL build the rest of the graph, not fail
  silently.

### Story 12: Command dependencies in lint, relink, spec-create and at runtime (D7)
**As a** developer **I want** tests that call a CLI command to wait for the task that builds it **So that**
they stop failing on commands owned by unfinished tasks.

**Acceptance Criteria:**
- 12.1 WHEN a test file invokes a command whose providing task is not among the dependencies of the test's
  verified task, THE SYSTEM SHALL have `ck lint` report the missing edge, naming the test file, the command,
  the providing task and the task that lacks the dependency.
- 12.2 WHEN `ck relink` runs, THE SYSTEM SHALL add the missing dependency edges that 12.1 reports, as it does
  for import edges today.
- 12.3 WHEN a test invokes a command that no task provides and that does not exist in the project, THE SYSTEM
  SHALL have `ck lint` warn about it.
- 12.4 WHEN `/spec:create` writes impl tasks, THE SYSTEM SHALL write a `**Provides:**` field on every impl
  task whose provided commands ck cannot infer.
- 12.5 WHEN a task's gate fails with a missing-command failure (unknown subcommand, unrecognized arguments,
  invalid choice) for a command that another unfinished task provides, THE SYSTEM SHALL add that dependency
  edge to the run's graph. The task SHALL wait for the providing task to merge, then retry. It SHALL not
  raise a test conflict or count the attempt as a retry.
- 12.6 WHEN a missing-command failure names a command that no task provides, THE SYSTEM SHALL handle it as
  a test conflict on the tests that invoke it.
- 12.7 WHEN adding a live edge would create a dependency cycle, THE SYSTEM SHALL not add it. It SHALL report
  the task as blocked, naming the cycle.
- 12.8 WHEN a live edge is added, THE SYSTEM SHALL log it and list it in the run's results, so it can be
  written back to the task list later with `ck relink`.
- 12.9 WHEN lint and relink run, THE SYSTEM SHALL keep today's import-based and Verify-cycle checks
  unchanged.

### Story 13: Write-set scheduling (D7)
**As a** developer **I want** tasks that write the same files not to run at the same time **So that**
fewer agent branches conflict at landing.

**Acceptance Criteria:**
- 13.1 WHEN two ready tasks own overlapping files (by owns edges, with globs matched), THE SYSTEM SHALL not
  run them at the same time.
- 13.2 WHEN overlapping tasks are ready together, THE SYSTEM SHALL start the one the scheduler would pick
  first by its current priority. It SHALL hold the others until that task has merged or stopped.
- 13.3 WHILE a task is held for a write-set overlap, THE SYSTEM SHALL fill free slots with other ready tasks
  that do not overlap. It SHALL show the held task and the task it waits on in the run's progress.
- 13.4 WHEN a task has no Files list, THE SYSTEM SHALL schedule it without a write-set check, and lint SHALL
  warn that it has no write-set.
- 13.5 WHEN the only ready tasks all overlap with a running task, THE SYSTEM SHALL wait for it, and never
  stall once it ends: the next held task starts as soon as a slot is free.
- 13.6 WHEN the scheduler holds a task for overlap, THE SYSTEM SHALL not change its dependencies in the task
  list.

### Story 14: Per-role models under OpenCode (D8)
**As a** developer running on OpenCode **I want** each role on its own model **So that** a cheap
implementer can run with a stronger test writer and reviewer, and the bench can compare models by role.

**Acceptance Criteria:**
- 14.1 WHEN `opencode.roles` maps a role (implementer, test_writer, reviewer, reviewer_risky, fixer,
  orchestrator, resolver) to a `provider/model#variant` reference in global or project ck config, THE
  SYSTEM SHALL run that role's agents on that model and variant under OpenCode.
- 14.2 WHEN a role is not mapped, THE SYSTEM SHALL resolve its model through the alias map, as today.
- 14.3 WHEN global and project config both map a role, THE SYSTEM SHALL use the project's value.
- 14.4 WHEN a role name is unknown or a reference is malformed (no provider, no model, an empty variant),
  THE SYSTEM SHALL report a config error naming the key and the value, before any agent starts.
- 14.5 WHEN a bench arm declares a `roles` map, THE SYSTEM SHALL run that arm's roles on those models. The
  arm's roles that are not mapped SHALL run on the arm's single model, as today.
- 14.6 WHEN the harness is Claude Code, THE SYSTEM SHALL ignore `opencode.roles` and keep its aliases.
- 14.7 WHEN a run records its agents, THE SYSTEM SHALL record the model and variant each agent actually ran
  on, so stats and bench reports can compare by role.
- 14.8 WHEN I run the OpenCode doctor, THE SYSTEM SHALL show each role's resolved model and where it came
  from (roles map, alias map or default).

### Story 15: Fixture hash lock and labeling review (D9)
**As a** developer **I want** recorded fixtures locked by hash and their labels reviewed **So that** later
hand edits and "recorded" fixtures that are really synthetic are caught.

**Acceptance Criteria:**
- 15.1 WHEN the project's ck config declares recorded-fixture globs, THE SYSTEM SHALL keep a hash lock for
  each matching file, recorded when the file first lands.
- 15.2 WHEN a landing changes a locked file, THE SYSTEM SHALL report the change in the landing result,
  naming the file and the task. The run-end audit SHALL report it as well.
- 15.3 WHEN a change to a locked file is marked handcrafted with a reason, THE SYSTEM SHALL record the reason
  and not report the change as drift.
- 15.4 WHEN a handcrafted mark has no reason, THE SYSTEM SHALL treat the change as unmarked drift.
- 15.5 WHEN a lock entry for an existing file is changed or removed, THE SYSTEM SHALL report that in the
  run-end audit, as for the file itself.
- 15.6 WHEN the test-task reviewer reviews a task with fixtures that have no capture metadata and are not
  labeled handcrafted, THE SYSTEM SHALL raise a finding that blocks like any review finding.
- 15.7 WHEN no recorded-fixture globs are declared, THE SYSTEM SHALL keep no lock and report nothing.
- 15.8 WHEN a declared glob is invalid (not a string), THE SYSTEM SHALL report a config error naming the key.

### Story 16: Release claims by run ID (D10)
**As a** developer re-running a spec **I want** claims left by finished or dead runs released at run start
**So that** I no longer release stale claims by hand before each pass.

**Acceptance Criteria:**
- 16.1 WHEN a run claims a task worktree, THE SYSTEM SHALL record the run's ID with the claim.
- 16.2 WHEN a run starts, THE SYSTEM SHALL release every claim on the spec whose recorded run is finished or
  not running. The claimed branch's commits SHALL be kept.
- 16.3 WHEN a claim's recorded run is running, THE SYSTEM SHALL keep the claim and not take the task.
- 16.4 WHEN a claim has no run ID (from an older ck, or made by hand), THE SYSTEM SHALL apply today's
  time-based rule (default 60 minutes without activity).
- 16.5 WHEN a claim's run record cannot be found, THE SYSTEM SHALL treat the run as not running.
- 16.6 WHEN claims are released at run start, THE SYSTEM SHALL list each released task with the run that
  held it.
- 16.7 WHEN a claim's recorded run is the starting run itself, THE SYSTEM SHALL treat the claim as its own.

### Story 17: Harness registry (D11)
**As a** developer **I want** harnesses registered rather than hard-coded **So that** adding a harness is an
addition, not a rewrite of bench and doctor.

**Acceptance Criteria:**
- 17.1 WHEN bench or doctor selects a harness, THE SYSTEM SHALL look it up in one registry. The registered
  harnesses SHALL be the only valid `--harness` values.
- 17.2 WHEN a harness name is not registered, THE SYSTEM SHALL fail with an error that lists the registered
  harnesses.
- 17.3 WHEN a new harness is registered, THE SYSTEM SHALL make it selectable in bench and doctor without
  changes to their dispatch code. An automated test SHALL register a fake harness and run a bench arm on it.
- 17.4 WHEN the `claude` or `opencode` harness is used, THE SYSTEM SHALL behave as it does today, including
  options that only apply to one harness.
- 17.5 WHEN a harness runs an arm, THE SYSTEM SHALL return the same arm result shape for every harness.

### Story 18: Guarded DeepSeek Harness adapter (D11)
**As a** developer **I want** to try DeepSeek Harness as a third harness **So that** I can compare it with
Claude Code and OpenCode, without risking the rest of the spec.

**Acceptance Criteria:**
- 18.1 WHEN the `dsh` phase starts, THE SYSTEM SHALL first record a verification of DeepSeek Harness's API
  from its official repository: the version checked, and whether it can host the workflow's `agent()` and
  `ck()` calls.
- 18.2 WHEN the verification shows `dsh` can host those calls, THE SYSTEM SHALL provide a registered `dsh`
  harness. It SHALL run the workflow's `agent()` and `ck()` calls, and the equity E2E SHALL be run on it.
- 18.3 WHEN the verification shows `dsh` cannot host those calls, THE SYSTEM SHALL stop the phase with a
  findings report: what was checked, the version, and what is missing. No adapter is built.
- 18.4 WHEN `dsh` is not installed or reports an unsupported version, THE SYSTEM SHALL have doctor and
  bench report that and refuse to run a `dsh` arm.
- 18.5 WHEN the `dsh` phase stops with a findings report, THE SYSTEM SHALL leave every other story's
  behaviour unaffected.

### Story 19: Lean thin ck agents under Claude Code (D14)
**As a** developer running on Claude Code **I want** thin ck agents to run as a lean agent type **So that**
they stop paying for context they never use.

**Acceptance Criteria:**
- 19.1 WHEN the workflow under Claude Code starts a thin ck agent (the attributed gate of Story 2 and the
  other agents that only run a ck command), THE SYSTEM SHALL start it as the dedicated lean agent type,
  never as a default subagent.
- 19.2 THE SYSTEM SHALL define the lean agent type with Bash as its only tool, no project or user CLAUDE.md,
  Haiku at low effort, and a turn limit of at most 5.
- 19.3 THE SYSTEM SHALL give all thin ck agents one shared output schema.
- 19.4 WHEN the lean agent type is not available at run start (for example a stale vendored copy), THE SYSTEM
  SHALL report that, and run thin ck calls as before, with a warning in the run's results.
- 19.5 WHEN a thin ck agent's output does not match the shared schema, THE SYSTEM SHALL treat the call as
  failed (as in 2.9 for gates), not as success.
- 19.6 WHEN the harness is OpenCode, THE SYSTEM SHALL keep running ck directly, with no agent.

### Story 20: Equity E2E reruns as the success bar (D12, D13)
**As a** developer **I want** four fresh equity E2E runs to pass **So that** the fixes are shown to hold
across two models and three harnesses.

**Acceptance Criteria (manual checks, recorded with run IDs):**
- 20.1 WHEN the reruns are set up, THE SYSTEM SHALL use the equity spec from `fix/mtr-spec-issues`. Each
  bench's landing allow-list SHALL pre-approve exactly the 12 reviewed slash-command paths, and no others:
  `.claude/commands/digest.md`, `.claude/commands/express.md`, `.claude/commands/grade.md`,
  `.claude/commands/inbox.md`, `.claude/commands/ingest.md`, `.claude/commands/roles.md`,
  `.claude/commands/spotcheck.md`, `.claude/commands/sweep.md`, `.claude/commands/thesis/close.md`,
  `.claude/commands/thesis/new.md`, `.claude/commands/thesis/review.md` and
  `.claude/commands/thesis/revise.md`. These are the `.claude/commands/` files that
  `specs/management-track-record/tasks.md` marks `- new` at commit `119ba16` of `fix/mtr-spec-issues` in the
  equity repository. `.claude/CLAUDE.md` SHALL stay protected.
- 20.2 WHEN the four reruns are run (OpenCode with `opencode-go/muse-spark-1.3-contributor` at medium,
  OpenCode with `opencode-go/deepseek-v4.1-flash` at medium, Claude Code with default roles, and `dsh`),
  each SHALL end with every task Done and the project's full suite green, after the final attributed gate of
  2.11. A rerun MAY span several passes of one run ID (2.13); its result covers all of them.
- 20.3 WHEN a rerun is judged, it SHALL have zero hand repairs. Approving Tasks 93 and 94's protected
  change through the product's own approval path (9.8) SHALL not count as a hand repair. Starting the later
  pass of 2.13 with the same run ID, with no other change from me, SHALL not count as a hand repair either.
- 20.4 WHEN the two OpenCode reruns are configured, every role SHALL use that run's single model (no
  `opencode.roles` map).
- 20.5 WHEN a rerun is judged, its cost per merged task and its wall minutes per task (from `ck stats`)
  SHALL be no greater than the first equity E2E's.
- 20.6 WHEN 18.3 applies, the `dsh` findings report SHALL satisfy the `dsh` rerun. WHEN 18.2 applies, the
  `dsh` rerun SHALL meet 20.2-20.5 like the others.
- 20.7 WHEN a rerun fails the bar, the result SHALL record which criterion failed and the run ID.

## Specification Heritage

- **Evolves From:** specs/deterministic-plan (direct ck calls under OpenCode, `ck plan --json` dependency
  graph), specs/landing-speed (batched landing with one task-scoped gate, finish-first scheduler),
  specs/prompt-free-runs (needs-input and park path, protected-path hand landing as a wait on me,
  notifications), specs/run-followups (run progress file, Verify-cycle lint). It also evolves today's
  per-test-task gate excusing, the import-based dependency scan, and time-based stale claims.
- **Preserves:** task-scoped landing gates; `ck gate` without `--attribute`; every existing `ck gate
  --json` field (additive only); the protected-path list and `land.allow_paths` meaning; refusal of
  protected changes by `ck worktree land`; the import-based and Verify-cycle lint checks; Claude Code's
  model aliases and OpenCode's alias map as the fallback; the 60-minute claim rule for claims with no run
  ID; the fixer ladder and recheck; the guards (never weaken a test, never edit requirements or existing
  test-plan rows); the finish-first scheduler's priority order.
- **Supersedes:** agent-run periodic and final gates; per-test-task excusing in those gates (replaced by
  per-test attribution); a single `testConflict` (replaced by `testConflicts`); free-text failure reports
  trimmed to 3000 characters as the retry input; a confirmed test conflict ending in a report only;
  `ck worktree merge` skipping the protected-path check; cross-test checks off by default and report-only;
  the test-track gate checking only that tests exist and collect; dependency detection from imports only;
  claims released by time only; bench and doctor dispatch through a claude/opencode if/else; every
  OpenCode role sharing an alias's model; thin ck agents running as default subagents.
- **Evolution Reason:** the PLAN.md problem statement and the first equity E2E report: hand repairs, a red
  suite behind green gates, a raw-git merge of a protected file, one blocker per pass, unseen CLI
  dependencies, and a single model for all roles.

## Non-Functional Requirements

- **Attribution cost:** parsing and attributing the results of a 2,000-test run, with 200 failures, over a
  100-task spec takes under 2 seconds on this development machine, excluding the time to run the tests.
- **Flake reruns are bounded:** an attributed gate reruns at most one time per regression candidate. It
  never reruns excused failures or whole suites (2.4, 2.5).
- **Failure message limit:** each failure's message is at most 500 characters (3.5). The `failures` list is
  never truncated in ck's JSON.
- **Hook cost:** the ref-update hook adds under 50 ms per update to refs outside `integrate/*`, and under
  1 second per update to an `integrate/*` ref on a repository the size of the equity project.
- **Graph build time:** building the graph for a 100-task spec with 200 test files takes under 3 seconds
  on this development machine.
- **Determinism:** attribution (1.10), graph output (11.5) and lint findings are identical across repeated
  runs on the same inputs.
- **Thin-agent context:** a lean thin ck agent's first request writes at least 50% fewer cache tokens than
  a default subagent running the same ck command, measured from run records on Claude Code (manual check).
- **Run cost and speed:** no equity rerun has a higher cost per merged task, or more wall minutes per
  task, than the first equity E2E (20.5).
- **No network:** gates, attribution, the graph, lint, relink, hooks, the audit and the fixture lock make no
  network calls.
- **Sandbox:** the hooks, landing tokens and audit work inside the ck run sandbox, where the worktree
  metadata directory may be read-only (9.10, 9.11).
- **Backward compatibility:** existing tests pass unchanged, except those that pin behaviour this spec
  supersedes. Those are updated in the task that supersedes the behaviour.

## Constraints

- The guards stay as they are: never weaken a test, never edit the requirements document or existing
  test-plan rows. The test-fixer and its reviewer enforce them (4.3, 4.5), and nothing in this spec relaxes
  them.
- ck stays project-agnostic. Project-specific validation and fixture globs come from the project's ck
  config (Stories 6, 15).
- Node-level attribution is for pytest only. Other runners get file-level attribution or the fallback in 1.7.
- Workflow changes are made in the packaged workflow source and kept identical in the vendored copies under
  `.claude/`. The vendored copies, `.claude/hooks` and settings are protected, so agents cannot write them
  and they land by hand. These edits are grouped into as few tasks as possible.
- Both harnesses (Claude Code, and OpenCode with `opencode-go/muse-spark-1.3-contributor` and
  `opencode-go/deepseek-v4.1-flash` at medium) are supported throughout. `dsh` is added last (D11).
- This spec builds on the unmerged `fix/open-issues` (0.11.1), and the branch rebases onto master once that
  merges.
- One spec (D15). Tasks run in dependency order, and the `dsh` phase comes last.
- Tests are integration-first: they drive the real `ck` CLI, real git repositories with real hooks, and the
  workflow scripts through the existing JS harness with fake agents. They are xdist-safe, with the `serial`
  marker only where needed, and fixtures use invented names.
- The key names for the test-track validation command, the fixture lock and `opencode.roles` details, the
  `--attribute` JSON schema, the hook layout and token format, the graph's storage and API, and the
  test-fixer's prompt and branch naming are design choices within these criteria (PLAN "Delegated to
  Spec").

## Out of Scope

- Diagnosing or fixing the equity app's own remaining bugs. The reruns surface them, and the product's
  loops fix them.
- Proving where a fixture came from. D9 only detects drift and mislabels. A capture command (`ck fixtures
  record`) was rejected.
- New per-language test runners for node-level attribution beyond pytest.
- Any change to the guards.
- An explicit per-file attribution map written by the test writer, and per-file-only attribution (rejected
  in D1).
- Running the full suite on every landing batch, and keeping agent-run gates with attribution added
  (rejected in D2).
- Agents writing their own failure lists, and ck-only lists without agents reporting every failure
  (rejected in D3).
- Test fixes with no proof, and keeping test repair human-only (rejected in D4).
- An audit-only approach to protected files, and harness-specific permission rules (rejected in D6).
- A Provides field plus runtime checks without a shared graph, and runtime-only detection (rejected in D7).
- Alias maps for bench only, and provider references in the shared agents config (rejected in D8).
- A shorter claim timeout, and taking over every claim at run start (rejected in D10).
- A `dsh` spike only, or a separate `dsh` spec (rejected in D11).
- Making `dsh` best effort when it can host the workflow (rejected in D12).
- Running the reruns on the original equity spec with protected files decided live (rejected in D13).
- A ck mod that runs processes directly so Claude Code needs no thin agent (a longer-term idea in D14).
- Staging this work in two phases with a measured rerun in between, or splitting it into two specs
  (rejected in D15).
- Per-role models in the two OpenCode reruns (D12 keeps them single-model).
- Protected-change enforcement on refs other than `integrate/*` (D6 keeps the hook scoped).

## Assumptions

- Test names in specs that follow the methodology carry `tcNNN_` prefixes, and Verify lines select test
  cases with `-k "tcNNN_"`. Where they do not, 1.4 and 1.9 apply.
- A failing test with no attributed task at all (1.5) is red, because no unfinished task can excuse it. This
  follows D1's rule ("excused while any [task] is not Done") when there is no task.
- "Rerun once" (2.4) reruns only that test node, in the same environment as the gate.
- The periodic gate's N reuses today's every-N-merges setting (default 10).
- The test-fixer's single automatic fix per test (4.8) is counted across runs of the same spec. Otherwise a
  restarted run could repeat a fix that failed, which D4's "one automatic fix, then me" rules out.
- A test-fixer outcome that ends with me counts as one of "the decisions the product asks for" only when I
  approve or decide something. A fix that I write by hand still counts as a hand repair under D12.
- The human approval path for protected changes (9.8) is new. PLAN.md requires hooks to refuse anything
  without a ck-issued token, but it does not say how I approve Tasks 93 and 94's `.claude/CLAUDE.md`
  change in each rerun (D13). This spec requires an approval that needs my own input. The mechanism is a
  design choice.
- Run liveness (16.2, 16.5) is judged from local run records on this machine. Two runs of one spec on
  different machines sharing one checkout are not supported.
- "Last approved state" for the audit (10.1) is what ck recorded at its last token-approved landing, or at
  integration-branch creation if none.
- Escalation ladders under OpenCode keep resolving their steps through the alias map. `opencode.roles` sets
  each role's starting model.
- The first equity E2E's cost per merged task and wall minutes per task are available from `ck stats` for
  that run's records. All four reruns are compared against those figures (20.5).
- The "not built yet" rules (5.2), the failure-kind parsing for pytest, and how inference fills provides
  and invokes edges are refined in design, within the criteria above.
- Current knowledge of `dsh` comes from third-party write-ups. 18.1 verifies it before anything is built.
