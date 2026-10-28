# Feature: Run Analytics

## Overview

Whole-spec runs are slow and costly, and nothing records why. ck 0.10.0 keeps no history: `ck cost`
recomputes spend when asked, and the workflow's detailed result is shown once and then only survives in
Claude Code's private run file. This feature gives every ck spec process (spec-implement, including runs
under `ck run`, and spec-create) a durable, comparable record, and lets records from several machines
(personal and work) reach the claude-kiro repository as reviewable draft pull requests, so improvements to
ck can be driven by data.

It works after the fact (PLAN decision 1). A `ck stats` command group reads Claude Code's run files and
agent transcripts once a run has finished. It writes a full-fidelity record to a machine-local store
(decision 2) and prints breakdowns and cross-run trends. From the local records it builds shareable
records that contain only allowlisted fields (decision 3), optionally with deterministic issue codes and a
leak-checked issues note that I approve (decision 4). It submits them as one draft PR per batch to
`kutlayhanli/claude-kiro` (decisions 5 and 9). Collection runs automatically and stays local. Export and
submission only happen when I run them (decision 7). spec:plan coverage is a stretch goal (decision 8).

Decision record: [PLAN.md](PLAN.md). Its decisions 1-9 and the review decisions D10-D17 are binding on this
document.

Terms used below:
- **Run**: one execution of an allowlisted ck workflow (spec-implement or spec-create), identified by
  Claude Code's workflow run id.
- **Local record**: the full-fidelity, machine-local record of one run. It may contain project-identifying
  data and never leaves the machine.
- **Shareable record**: the allowlisted, sanitized record built from a local record for the repository.
- **Issues note**: optional free text describing issues in ck-level terms. It is part of a shareable record
  only after I approve it.
- **Confidential term**: any project-identifying string known on this machine: spec names, task titles,
  task summaries, finding texts, verbatim agent labels, file paths and path segments, branch names, repo
  remote URLs and their owner/org/name parts, session, agent and run ids, emails, account and org ids,
  exact timestamps, and names of non-ck workflows. A string made only of decimal or hexadecimal digits is not a
  confidential term (9.3).
- **Role**: what a workflow agent did: implementer, test-writer, reviewer, fixer, resolver, plan, land,
  gate, cross-test, report, or `other`.

## User Stories

### Story 1: Collect a run's local record

**As a** ck user **I want** a run's timing, counts, tokens and cost saved to a machine-local record
**So that** the data survives after Claude Code prunes its files and I can compare runs later

**Acceptance Criteria:**
- 1.1 WHEN I run `ck stats collect --run <run id>` for a completed spec-implement or spec-create run, THE
  SYSTEM SHALL write exactly one local record for that run to the machine-local data store under my XDG
  data directory (`$XDG_DATA_HOME/claude-kiro`, falling back to the platform default) and print where it
  wrote it.
- 1.2 WHEN a local record is written, THE SYSTEM SHALL include a schema version, the ck version the run
  used where it can be tied to the run (otherwise unknown; a re-collect keeps a version tied by an earlier
  collect), the version of ck that wrote the record (always present), the Claude Code version where the
  transcripts show one, the workflow kind, the run's outcome, its start time and wall duration, the
  role-to-model/effort configuration the run used, and every field that criteria 1.3-1.8 call for. Anything
  reported per ck version SHALL use the tied version only, never the collector's version.
- 1.3 WHEN the run has agent transcripts, THE SYSTEM SHALL record tokens (input, output, cache read, cache
  write) and cost per agent, per model, per role, per phase and per task, pricing the usage counted once
  per request, the same way `ck cost` does.
- 1.4 WHEN a cost figure is recorded, THE SYSTEM SHALL label its source (Claude Code's own total or priced
  from transcripts) and the pricing date it used.
- 1.5 WHEN the run is a spec-implement run, THE SYSTEM SHALL record per task: attempts, retries,
  escalations, review rounds and verdict, conflict resolutions, landing fixes, agent minutes, minutes to
  merged, risk tag, and idle time between the task finishing and its merge. Per run it SHALL also record:
  landings (size, merged, red, conflict, bisected, minutes), full-gate and final-gate results, cross-test
  counts, fixer escalations, and the per-phase timing in the result.
- 1.6 WHEN the run is a spec-create run, THE SYSTEM SHALL record the review counts (fixed, not fixed,
  escalated, refuted, lenses lost), the consistency passes and fixes, and phase durations taken from the
  run file's agent timings.
- 1.7 WHEN a spec-implement result reports conflict resolutions per task, THE SYSTEM SHALL use that
  count. WHEN the result predates that field, THE SYSTEM SHALL infer the count from resolver agent labels
  and mark it as inferred.
- 1.8 WHEN the run's project has a git remote, THE SYSTEM SHALL store a project pseudonym in the local
  record. The pseudonym is a keyed hash of the remote, keyed with a salt that is generated once, kept as
  a non-role key in my global ck configuration file (the `config.json` that `ck agents set` writes, under
  `$CK_CONFIG_HOME` or `~/.config/claude-kiro`), and never written to any shareable record, export or PR
  (decision 6).
- 1.9 WHEN `ck stats collect` runs again for a run it has already collected, THE SYSTEM SHALL replace that
  run's local record rather than add a second one. The local store SHALL then hold one record for the run.
- 1.10 WHEN no run with the given id exists under the Claude configuration directory
  (`$CLAUDE_CONFIG_DIR`, default `~/.claude`), or 1.15 refuses a run whose run file is gone, THE SYSTEM
  SHALL print that the run was not found, name the directory it searched, write nothing, and exit non-zero.
- 1.11 WHEN the run is still in progress, THE SYSTEM SHALL write no record, say that the run has not
  finished, and exit zero.
- 1.12 WHEN the run file is readable and allowlisted and its agent list (`workflowProgress`) is a list, but
  its result is missing or not an object, some agent entries are not objects, some agent entries have
  missing or malformed fields, or some transcripts or meta files are missing, truncated or unparseable, THE
  SYSTEM SHALL write a record of what it could read. Agent entries that are not objects SHALL be skipped and
  listed as missing. The record SHALL say which parts are missing. Any cost not backed by transcripts SHALL
  carry an estimate label, and no missing number SHALL be shown as zero.
- 1.13 WHEN the run file's format is not one the collector recognises, including a run file that is not
  valid JSON and a run file whose agent list (`workflowProgress`) is missing or not a list, THE SYSTEM SHALL write no record, report that the format is unrecognised, and exit non-zero.
  The report SHALL name the run id only when I named that run with `--run` or its workflow name is known
  to be allowlisted, and SHALL name the workflow name only when it is allowlisted. During `--all` and
  `ck doctor`, a run file whose workflow name cannot be read is never named; it is counted with the
  skipped non-ck runs.
- 1.14 WHEN collect is invoked from a sandboxed shell that cannot read the Claude configuration directory,
  THE SYSTEM SHALL say that it must run outside the sandbox and exit non-zero, without writing a partial
  record.
- 1.15 WHEN `ck stats collect --run <id>` names a run whose run file is missing but whose agent transcripts
  remain, THE SYSTEM SHALL recover the workflow name from the Workflow launch in the main session transcript
  that is tied to that run id (the launch whose result names the id). WHEN the recovered name is
  allowlisted, it SHALL write a partial record from the agent transcripts and meta files alone (usage,
  models, cost, roles from the agent labels, phases, per-agent durations and the wall time from transcript
  timestamps) whose missing parts include the run file and the result. WHEN no name can be recovered or the
  name is not allowlisted, it SHALL treat the run as not found (1.10), write nothing, and never print the
  name.

### Story 2: Collect only ck workflows

**As a** ck user who also runs non-ck workflows on the same machine **I want** the collector to ignore
everything that is not a ck spec workflow **So that** the names and contents of unrelated (possibly
confidential) workflows never end up in any ck output

**Acceptance Criteria:**
- 2.1 WHEN the collector scans Claude Code's run files, THE SYSTEM SHALL only read past the workflow name
  of runs whose name is on a fixed allowlist of ck workflow names (spec-implement and its isolation
  variant, spec-create).
- 2.2 WHEN a run's workflow name is not on the allowlist, THE SYSTEM SHALL write nothing about it, neither
  its name nor its id, to any local record, shareable record, log or console output. A count of skipped
  runs may be printed. The one exception is echoing back a run id I typed myself in `--run` (2.3); runs
  the collector finds on its own (`--all`, `ck doctor`, `ck run`, leak-term gathering) are never named.
- 2.3 WHEN `ck stats collect --run <id>` names a run whose workflow is not allowlisted, THE SYSTEM SHALL
  refuse with a message that does not include the workflow's name, and exit non-zero.

### Story 3: Collect automatically after each run

**As a** ck user **I want** collection to happen on its own when a spec run ends **So that** no run is
lost to transcript pruning and I never have to remember a command

**Acceptance Criteria:**
- 3.1 WHEN a `ck run` session exits, THE SYSTEM SHALL collect every allowlisted run that the session
  started, from the outer (unsandboxed) ck process, and write one line to the `ck run` log for each
  collected run.
- 3.2 WHEN a skill that runs an allowlisted workflow (`/spec:implement`, `/spec:create`, or `/spec:review`,
  which runs spec-create in review mode) finishes a whole-spec workflow run, THE SYSTEM SHALL have the
  orchestrating session run the collect command for that run id outside the sandbox, as its last
  reporting step, and report where the record was written.
- 3.3 WHEN automatic collection fails for any reason, THE SYSTEM SHALL log the failure, leave the run's
  outcome, report and exit status exactly as they would be without collection, and print the manual
  collect command to retry. WHEN `ck run` finds run files in its session whose workflow name cannot be read,
  it SHALL report them with one count-only line that names no run id (2.2) and gives `ck stats collect --all`
  as the retry command.
- 3.4 WHEN the auto-collect setting is turned off with `ck agents set`, THE SYSTEM SHALL skip automatic
  collection after `ck run` and in the skills of 3.2. Manual `ck stats collect` SHALL keep working.
- 3.5 WHEN the auto-collect setting has never been set, THE SYSTEM SHALL treat automatic collection as on.
- 3.6 WHEN the auto-collect setting is given a value that is not a boolean, THE SYSTEM SHALL reject it
  with an error naming the accepted values and leave the stored configuration unchanged.

### Story 4: Backfill past runs

**As a** ck user with many past runs on disk **I want** to collect them all in one command **So that**
my history starts with the 80-plus runs already on this machine

**Acceptance Criteria:**
- 4.1 WHEN I run `ck stats collect --all`, THE SYSTEM SHALL collect every completed allowlisted run found
  under the Claude configuration directory, across all projects.
- 4.2 WHEN `--all` finishes, THE SYSTEM SHALL print how many runs it collected, already had and updated,
  skipped as unfinished, failed (each with its run id and reason), and skipped as non-ck (as a count only).
- 4.3 WHEN one run fails to collect during `--all`, THE SYSTEM SHALL continue with the remaining runs and
  exit non-zero at the end only if at least one run failed.
- 4.4 WHEN `--all` finds no allowlisted runs, THE SYSTEM SHALL say so and exit zero.

### Story 5: Attribute agents to roles

**As a** ck maintainer **I want** each agent's time and cost attributed to a role **So that** I can see
whether money goes to implementing, reviewing, resolving conflicts, fixing or gating

**Acceptance Criteria:**
- 5.1 WHEN an agent's label matches a label pattern that the spec-implement or spec-create workflow
  produces, THE SYSTEM SHALL attribute the agent to the matching role, task (when the label names one)
  and attempt or round.
- 5.2 WHEN an agent's label matches no known pattern, THE SYSTEM SHALL attribute it to `other` and report
  how many agents, minutes and dollars fell into `other`. It SHALL NOT guess a role.
- 5.3 WHEN either workflow's label templates change in a way the role parser does not recognise, THE
  SYSTEM SHALL fail an automated check that runs the role parser against the labels both workflows
  actually generate.

### Story 6: See breakdowns and trends locally

**As a** ck user **I want** `ck stats` to show where a run's time and money went and how runs compare
**So that** I can find what makes runs slow and expensive

**Acceptance Criteria:**
- 6.1 WHEN I run `ck stats` or `ck stats show` with no arguments, THE SYSTEM SHALL list the collected runs,
  newest first, each with date, workflow kind, outcome, wall time, cost with its source, and task counts.
- 6.2 WHEN I run `ck stats show <run id>`, THE SYSTEM SHALL print that run's time and cost per role, per
  phase and per model, its slowest and most expensive tasks, and its retry, escalation, review-round,
  conflict and fix rates, idle time before merge, and landing summary.
- 6.3 WHEN two or more runs of the same workflow kind are collected, THE SYSTEM SHALL show trends across
  them: cost per merged task, wall time per task, and the share of cost and time per role. It SHALL
  filter by workflow kind, by time range, and by project (by pseudonym, locally).
- 6.4 WHEN `--json` is given, THE SYSTEM SHALL print the same data as machine-readable JSON.
- 6.5 WHEN no runs are collected, THE SYSTEM SHALL say so, suggest `ck stats collect --all`, and exit zero.
- 6.6 WHEN the given run id has no local record, THE SYSTEM SHALL say so and exit non-zero.
- 6.7 WHEN the store holds records written by an older schema version, THE SYSTEM SHALL still show them.
  Fields an older record lacks SHALL be shown as unknown, not zero.
- 6.8 WHEN `ck stats show` prints a duration, THE SYSTEM SHALL state what that duration covers and what it
  excludes (for example, whether the plan and final-gate phases are counted in the wall time).

### Story 7: Export shareable records

**As a** ck maintainer **I want** to turn local records into shareable records that contain only
allowlisted fields **So that** data from client and work projects can go into the public ck repository
without leaking anything about those projects

**Acceptance Criteria:**
- 7.1 WHEN I run `ck stats export` pointing at a claude-kiro checkout, THE SYSTEM SHALL write one
  shareable record for each local record not yet exported into the checkout's analytics area, grouped by
  month (`yyyy-mm`). Each record SHALL get a newly generated random id that is unrelated to any run,
  session or agent id.
- 7.2 WHEN a shareable record is built, THE SYSTEM SHALL fill it only from the documented allowlist
  (decision 3). That is: schema version (`ck-run-analytics/1`), random record id, ck version, Claude Code
  version, workflow kind, outcome enum, month, optional machine label, role-to-model/effort
  configuration, counts (tasks, waves, agents by role, attempts, retries, escalations, review rounds and
  verdict enums, conflicts, fixes, landings, bisects, gate red/green), durations (wall, per phase, per
  role, per task as an anonymous index), tokens and cost by model and by role with cost source and pricing
  date, an optional stack hint from a fixed enum, and issue codes from a fixed enum.
- 7.3 WHEN a shareable record is built, THE SYSTEM SHALL contain none of the following: spec name, task
  titles, task summaries, findings text, verbatim labels, file paths, branch names, repo URL, project
  pseudonym, session/agent/run ids, prompts, emails, org or account ids, exact dates or timestamps, and
  any free text other than an approved issues note.
- 7.4 WHEN a value for an enumerated field (outcome, verdict, role, model, effort, stack hint, issue code)
  is not in that field's fixed set, THE SYSTEM SHALL write it as `other` and SHALL NOT copy the original
  string.
- 7.5 WHEN a model identifier is recorded, THE SYSTEM SHALL write a known model family or alias, or
  `other` for any custom or provider-specific identifier.
- 7.6 WHEN I set a machine label with `ck agents set run.machine_label <slug>`, THE SYSTEM SHALL accept
  only a short lowercase slug matching `^[a-z0-9-]{1,32}$` (lowercase letters, digits and hyphens, 1-32
  characters; leading, trailing and repeated hyphens are allowed). The values `none` and `default` clear
  the label instead of being stored. It SHALL reject any other value with an error, leave the stored
  configuration unchanged, and omit the field when no label is set.
- 7.7 WHEN the shareable record schema is published, THE SYSTEM SHALL ship a document in the claude-kiro
  repository that lists every allowed field and every excluded category. An automated check SHALL fail
  if the exporter can emit a field the document does not list.
- 7.8 WHEN a local record has already been exported, THE SYSTEM SHALL not export it again unless I ask to
  re-export it explicitly, or 10.10 applies.
- 7.9 WHEN the target directory is not a claude-kiro checkout (its remote is not
  `kutlayhanli/claude-kiro`), THE SYSTEM SHALL refuse, write nothing, and exit non-zero.
- 7.10 WHEN there is nothing new to export, THE SYSTEM SHALL say so and exit zero.
- 7.11 WHEN export runs, THE SYSTEM SHALL make no network requests, except the issues-note drafter, which
  runs only when I ask for a note in an interactive export and sends only the local record's data to the
  same Claude access the run used.
- 7.12 WHEN export runs without a request for an issues note, or runs non-interactively (no terminal, or
  `--yes` given), THE SYSTEM SHALL start no drafter subprocess.

### Story 8: Record issues without leaking

**As a** ck maintainer **I want** shareable records to say why a run went badly **So that** the
repository data explains slow or failed runs, not just measures them

**Acceptance Criteria:**
- 8.1 WHEN a shareable record is built, THE SYSTEM SHALL derive issue codes deterministically from the
  local record's result fields, using a fixed, documented code set (for example `gate_red_after_merge`,
  `resolver_rounds_exceeded`, `review_contract_mismatch`, `planning_failed`,
  `retry_no_commits`, `escalated_to_opus`). The same local record SHALL always yield the same codes.
- 8.2 WHEN I ask for an issues note during an interactive export, THE SYSTEM SHALL provide a draft written
  by an agent in ck-level terms from the local record and its failure reasons. It SHALL run the leak
  check on the draft, show me the draft with any leak-check hits marked, and let me edit, accept or
  discard it.
- 8.3 WHEN I accept an issues note, THE SYSTEM SHALL run the leak check again on the final text and
  include the note only if the check passes.
- 8.4 WHEN export runs non-interactively (no terminal, or `--yes` given) and a free-text issues note would
  be included, THE SYSTEM SHALL leave the note out, keep the structured record and issue codes, and say
  that the note was withheld for lack of interactive approval.
- 8.5 WHEN I discard the note or the leak check fails on it, THE SYSTEM SHALL export the record without a
  note.

### Story 9: Leak check

**As a** ck maintainer **I want** a deterministic leak check on everything that leaves the machine
**So that** a bug in the allowlist or a careless note cannot publish confidential data

**Acceptance Criteria:**
- 9.1 WHEN export or submit runs, THE SYSTEM SHALL scan every file to be written or committed for the
  confidential terms known from all local records on this machine, the current git identity's name and
  email, and generic secret and identifier patterns (emails, URLs, API-key and token shapes, absolute
  paths, UUID-like run and session ids, exact timestamps).
- 9.2 WHEN the leak check finds a hit, THE SYSTEM SHALL block the export or submission, print each hit's
  file and field, write and commit nothing, and exit non-zero.
- 9.3 WHEN a confidential term is too short or too generic to match safely (for example a one-letter path
  segment or a common English word used as a spec name), THE SYSTEM SHALL check it as a whole word, so
  ordinary text and field names do not produce false hits. A term made only of decimal or hexadecimal digits
  (for example a path segment `1`, `80`, `2026` or `cafe`) identifies no project and SHALL NOT be a
  confidential term, so it never blocks an export or submission by matching a version, month or record id.
  Every position of every file is still scanned. The matching rule SHALL be documented.
- 9.4 WHEN fixture runs seeded with confidential strings go through collect, export and submit (spec
  names, task titles, paths, repo URL, emails, branch names, non-ck workflow names), THE SYSTEM SHALL
  produce shareable records, PR branch name, commit message, PR title and PR body that contain none of
  those strings.

### Story 10: Submit a batch as a draft PR

**As a** ck maintainer working on personal and work machines **I want** `ck stats submit` to open one
draft PR with this machine's new records **So that** the data arrives in the repository for review in the
same way from every machine

**Acceptance Criteria:**
- 10.1 WHEN I run `ck stats submit`, THE SYSTEM SHALL export any not-yet-exported records, show me the full
  diff of the files to be committed, run the leak check, and ask me to confirm before it creates a branch,
  commits, pushes or opens a PR.
- 10.2 WHEN I confirm, THE SYSTEM SHALL create a new branch whose name contains no confidential term,
  commit only the new shareable files, and open a draft PR with `gh` against `kutlayhanli/claude-kiro`.
  The PR title and body SHALL be generated from allowlisted fields only (record count, workflow kinds,
  months, machine label if set). The PR URL SHALL be printed.
- 10.3 WHEN the submission includes all records that are new since the last submission from this machine,
  THE SYSTEM SHALL put them in one PR and remember them as submitted so the next submit does not include
  them again.
- 10.4 WHEN I decline at the confirmation prompt, THE SYSTEM SHALL leave no branch, commit or PR, and
  leave the records not submitted.
- 10.5 WHEN `--yes` is given and the batch contains no free-text issues notes, THE SYSTEM SHALL skip the
  confirmation prompt but still print the diff and run the leak check. WHEN the batch contains free text,
  THE SYSTEM SHALL ignore `--yes` for that text: with a terminal it SHALL ask (10.11); without one it SHALL
  leave the notes out of the commit, submit the rest, and say that the notes were withheld for lack of
  interactive approval, following 8.4.
- 10.6 WHEN `gh` is missing, not logged in, or fails, THE SYSTEM SHALL report the error and leave the
  exported files in the checkout so I can commit them myself. It SHALL mark nothing as submitted and exit
  non-zero.
- 10.7 WHEN the claude-kiro checkout has uncommitted changes other than this machine's own
  exported-but-unsubmitted shareable files (a note withheld by 10.11 counts as one of them), or a destination file already exists in the checkout other
  than as one of those same files, THE SYSTEM SHALL refuse to submit and leave my working tree untouched.
- 10.8 WHEN there is nothing new to submit, THE SYSTEM SHALL say so and exit zero without creating a branch
  or PR.
- 10.9 WHEN submit targets any repository other than `kutlayhanli/claude-kiro`, THE SYSTEM SHALL refuse.
- 10.10 WHEN a record is marked exported to this checkout but not submitted, and its files are missing from
  the checkout, THE SYSTEM SHALL re-export that record (with a new record id) on the next export or submit,
  instead of skipping it or committing nothing.
- 10.11 WHEN the submit batch includes an issues note I approved during an earlier interactive export and
  a terminal is available, THE SYSTEM SHALL require interactive confirmation of the PR, ignoring `--yes`.
  With no terminal and `--yes` given, it SHALL leave the note out of the commit, submit the structured
  records and issue codes of the batch, and say that the note was withheld for lack of interactive approval
  (10.5, 8.4). The withheld note file stays uncommitted in the checkout as one of this machine's own files
  (10.7).

### Story 11: Conflict-resolution count in the spec-implement result

**As a** ck maintainer **I want** the spec-implement result to report how many conflict resolutions each
task needed **So that** the collector does not have to infer it from agent labels

**Acceptance Criteria:**
- 11.1 WHEN a spec-implement run returns, THE SYSTEM SHALL include, for every task, the number of conflict
  resolutions that task's landing needed (zero when none).
- 11.2 WHEN the new field is added, THE SYSTEM SHALL leave every other result field and the run's
  behaviour unchanged. The packaged and vendored copies of the workflow SHALL agree.

### Story 12: Health check of collection

**As a** ck user **I want** `ck doctor` to tell me when collection cannot work **So that** a change in
Claude Code's file format or a disabled setting does not lose data silently

**Acceptance Criteria:**
- 12.1 WHEN `ck doctor` runs and the newest allowlisted run on this machine has a run-file format the
  collector does not recognise, THE SYSTEM SHALL report a warning that names the problem.
- 12.2 WHEN `ck doctor` runs and auto-collection is turned off, THE SYSTEM SHALL mention it as information,
  not as an error.
- 12.3 WHEN `ck doctor` runs and there are completed allowlisted runs that have not been collected, THE
  SYSTEM SHALL report how many and suggest `ck stats collect --all`.

## Non-Functional Requirements

- **Privacy by construction:** shareable records can only contain allowlisted fields (7.2, 7.7). The
  seeded leak test (9.4) passes in CI with zero hits.
- **No network except submit and the note drafter:** collect, show and export make no network calls,
  except the issues-note drafter, which runs only when I ask for a note in an interactive export and sends
  only the local record's data to the same Claude access the run used (7.11, 7.12). In tests, submit
  reaches only a fake `gh` placed on PATH and a local bare repository, the drafter is a fake `claude`, and
  no test touches the network.
- **Never blocks a run:** with collection forced to fail, `ck run` and the skills of 3.2 exit with the
  same status and report as with collection off (3.3).
- **Agreement with existing numbers:** for the same run, the transcript-priced total in the local record
  equals the `ck cost --run <id>` total to the cent.
- **Performance:** collecting one 250-agent run from fixtures takes under 10 seconds. `collect --all` over
  100 fixture runs takes under 2 minutes on a developer laptop.
- **Local record protection:** local records and the pseudonym salt are created readable and writable by
  the owner only (mode 0600; their directory 0700 where it is created).
- **Schema versioning:** local and shareable records carry a schema version. Fields added later are
  optional, and readers accept records from earlier versions (6.7).
- **Determinism:** exporting the same local record twice, with re-export forced, gives identical content
  apart from the random record id.

## Constraints

- Read-only use of Claude Code's private run-file and transcript layout, which `ck cost` and `ck bench`
  already depend on. The parser must tolerate missing and extra fields.
- Workflow scripts cannot write files. Anything written at run time must come from a ck command run by an
  agent or by the outer process.
- A sandboxed `ck run` session cannot read the Claude configuration directory, so collection after
  `ck run` happens in the outer ck process after the session exits.
- Transcript parsing and pricing must reuse the logic behind `ck cost` and `ck bench`, so the three
  commands report the same numbers.
- Only allowlisted ck workflow names may be collected (Story 2).
- Shared dates are month granularity only. PRs go only to `kutlayhanli/claude-kiro` (decision 9).
- Work machines submit the same way as personal machines: a draft PR via `gh` (decision 5).
- Tests are integration-first: they build fake Claude configuration directory trees and run the real CLI.
- Workflow changes are made in the packaged source, and the vendored copy is kept in sync.

## Out of Scope

- Run-time capture inside the workflows (an extra "record" agent step). The only workflow change is the
  per-task conflict-resolution count (decision 1).
- OpenTelemetry pipelines or collectors (Option C), and raw or synced transcript export (Option D).
- Storing analytics inside any project directory (decision 2).
- Any project identity in shared records, including salted pseudonyms (decision 6).
- Automatic export or submission. Only local collection is automatic (decision 7).
- Exporting free text without my interactive approval (decision 4).
- Exact dates or timestamps in shared records. PR targets other than `kutlayhanli/claude-kiro`
  (decision 9).
- spec:plan coverage in v1. Measuring it from its session transcript is a stretch goal (decision 8).
- A repository-side aggregate report over the submitted records (for example a generated summary file) in
  v1. This is a stretch goal; v1 has local cross-run trends only (6.3).
- Changing how runs behave, cost or schedule work.
- Any hosted service, telemetry endpoint or background daemon.

## Assumptions

- The open PLAN question on a repository-side aggregate report is settled as "later". v1 includes local
  cross-run trends in `ck stats show`, and the aggregate report is a stretch goal.
- Claude Code keeps run files and transcripts long enough (at least several days) for collection right
  after a run to succeed. Retention is unconfirmed (PLAN open question), so automatic collection right
  after each run is the mitigation.
- The open PLAN question of moving the label grammar into a shared constant is a design choice. These
  requirements only require that role parsing is checked automatically against the labels both workflows
  generate (5.3).
- The leak check's confidential terms come from all local records on the machine, not only those in the
  current batch, because one machine may hold records from several projects.
- The issues-note draft is written by the agent in the interactive session (or one it starts), using the
  same Claude access as the run itself. No project data goes to any other service.
- A run whose run file has been deleted but whose agent transcripts remain is collected only through 1.15
  (review decision D10): its workflow name is recovered from the main session transcript, and no record ever
  claims a workflow kind that was not read from an allowlisted name (2.1). `--all` walks run files, so it
  does not find such runs.
- `gh` is installed and logged in on machines that submit. Machines without it use export and commit by
  hand (10.6).
- The auto-collect setting lives in the existing layered agent configuration and is changed with
  `ck agents set`, as decision 7 suggests.
- The stack-hint enum and the issue-code set start small and are documented with the schema. A value
  outside them becomes `other`.
- Run ids found inside a `ck run` session can be discovered by the outer process from the session it
  launched, without the session reporting them.
