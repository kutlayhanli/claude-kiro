# Feature: Agent-Mode Analytics

## Overview

`ck stats` collects only runs of Claude Code's Workflow tool. When that tool is missing or blocked,
`/spec:implement`, `/spec:create`, `/spec:review` and `/spec:spawn-worktree` fall back to the Agent tool.
That path leaves no workflow run file and no run id, so nothing is collected and `ck stats show` stays
empty. Machines where the Workflow tool is blocked (typically work machines) produce no analytics at all.

This feature gives agent-mode runs (ones run with the Agent tool) the same records as workflow runs:

- At the start of a run, the orchestrator asks ck for a run id (`ag_` plus 8 lowercase hex digits). ck
  writes a local run manifest. This step is best-effort.
- Every Agent call's description carries a marker `[ck:<run_id>]` plus the role, the task number and the
  attempt or round. It never carries the spec name, a task title or a path.
- At the end, `ck stats collect --agents <run_id>` builds a local record from the session's agent
  transcripts and meta files. It reuses the existing collector core and pricing. It takes the outcome
  from `ck worktree report`, which the collector runs itself.
- Runs that never reached collect are reported by `ck doctor` and collected by `ck stats collect --all`.
- Implement-mode runs from before this feature (no marker) can be recovered, with a preview, by
  `ck stats collect --agents-backfill`. A later run whose `ck stats begin` failed carries no marker and is not
  recoverable (D10).
- The shareable schema stays `ck-run-analytics/1`. It gains an optional `source` field and one new
  cost-source value.

Decision record: [PLAN.md](PLAN.md). Decisions D1-D15 bind this document. Where this document and PLAN.md
disagree, PLAN.md wins.

Terms used below:
- **Workflow run**: a run of an allowlisted ck workflow through the Workflow tool, as defined in the
  run-analytics spec.
- **Agent-mode run**: one whole-spec `/spec:implement`, `/spec:create` or `/spec:review`, or one
  `/spec:spawn-worktree` batch over a spec, run by the orchestrator with the Agent tool.
- **Agent-mode run id**: `ag_` followed by exactly 8 lowercase hexadecimal digits.
- **Marker**: the text `[ck:<agent-mode run id>]` at the very start of an Agent description.
- **Run manifest**: a machine-local file, kept with the local records and never in a project directory.
  It records an agent-mode run's id, kind, start time, session id (when known), ck version, spec name,
  project root, target branch, the model, effort and enabled setting of each role, and (implement runs) the
  tasks already Done when the run started.
- **Open manifest**: a run manifest whose run has not been collected yet.
- **Spec agent type**: an implement-role agent type of ck: `spec:implementer`, `spec:test-writer`,
  `spec:fixer`, `spec:resolver`, `spec:reviewer`, or the vendored types `ck-implementer`,
  `ck-test-writer`, `ck-fixer`, `ck-resolver`, `ck-reviewer`.
- **Kind**: `implement`, `create` or `review`. Locally, `implement` is recorded as the spec-implement
  workflow. `create` and `review` are recorded as the spec-create workflow (review is spec-create in
  review mode, as with workflow runs).

## User Stories

### Story 1: Start an agent-mode run

**As a** ck user on a machine without the Workflow tool **I want** each agent-mode run to get its own run
id at launch **So that** every agent of the run can be grouped exactly afterwards

**Acceptance Criteria:**
- 1.1 WHEN I (or the orchestrator) run `ck stats begin --kind <implement|create|review> --spec <name>`,
  THE SYSTEM SHALL print a new agent-mode run id. The id SHALL match `ag_[0-9a-f]{8}` and SHALL differ from
  every run id that already has a run manifest or a local record on this machine.
- 1.2 WHEN `ck stats begin` succeeds, THE SYSTEM SHALL write a run manifest for the new id. The manifest
  SHALL hold the kind, start time, spec name, project root (the current checkout's top level), target
  branch (given, or else the current branch), session id when it can be determined, ck version, and, per
  role (implementer, test_writer, reviewer, reviewer_risky, fixer, resolver, orchestrator), the model, effort
  and enabled values `ck agents --json` resolves. It SHALL NOT store `run.allowed_domains`, the machine label
  or runtime info. For an implement run it SHALL also record the task ids already Done in the spec's
  tasks.md on the target branch when `begin` ran, or record them as unknown when that file cannot be read.
  It SHALL be readable and writable by the owner only.
- 1.3 WHEN `--json` is given, THE SYSTEM SHALL print the run id and the manifest path as one JSON object
  and nothing else on standard output.
- 1.4 WHEN `--kind` is missing or not one of `implement`, `create`, `review`, or `--spec` is missing or
  empty, THE SYSTEM SHALL print usage that names the accepted values, write no manifest, and exit with
  status 2.
- 1.5 WHEN the manifest cannot be written (for example a read-only or sandboxed store), THE SYSTEM SHALL
  still print a valid new run id, warn on standard error that the run will be collected without a
  manifest, and exit zero (D2).
- 1.6 WHEN `ck stats begin` fails for any other reason or is not available, THE SYSTEM SHALL leave the run
  free to proceed: the agent-mode instructions SHALL tell the orchestrator to continue without a marker and
  never to stop or retry the run because of `begin` (D2).
- 1.7 WHEN `ck stats begin` runs, THE SYSTEM SHALL make no network call and SHALL finish in under one
  second on a developer laptop, excluding Python start-up.

### Story 2: Content-free Agent descriptions

**As a** ck user who runs specs for confidential projects **I want** Agent descriptions to say only which
run, role, task and attempt they are **So that** nothing about what is being built shows up in the agent
list, transcripts' metadata or analytics (D4)

**Acceptance Criteria:**
- 2.1 WHEN the agent-mode instructions for `/spec:implement`, `/spec:create`, `/spec:review` and
  `/spec:spawn-worktree` describe an Agent call, THE SYSTEM SHALL give a description template that starts
  with the marker and continues with only a fixed verb, a task number, and an attempt or round number,
  covering at least: `[ck:<run_id>] implement task <N>`, `[ck:<run_id>] implement task <N> retry <k>`,
  `[ck:<run_id>] review task <N> round <k>`, `[ck:<run_id>] fix <k>`, `[ck:<run_id>] resolve task <N>`
  and `[ck:<run_id>] create <step>`. `<step>` comes from a fixed list of spec-create step names.
- 2.2 WHEN any description template in those instructions is checked, THE SYSTEM SHALL fail an automated
  check if a template contains a placeholder for a spec name, feature name, task title, task summary,
  file path or branch name.
- 2.3 WHEN an automated check renders every description template in those instructions with sample
  values, THE SYSTEM SHALL fail if any rendered description is not parsed by the agent-mode role parser
  (Story 4) to a known role.
- 2.4 WHEN `/spec:spawn-worktree` runs on a spec without the Workflow tool, or uses plain Agent calls
  because every role is `inherit` and review is off, THE SYSTEM SHALL instruct the orchestrator to start
  the run with `ck stats begin --kind implement` and to use the marker templates of 2.1 for every Agent
  call.
- 2.5 WHEN `begin` gave no run id (1.6), THE SYSTEM SHALL instruct the orchestrator to use the same
  templates without the marker, still with no spec name, title or path, and, in place of the final collect
  step (5.1), to end its report with the one line `⚠ ck stats begin failed: this run is not recorded in ck
  stats`. Such a run's agents carry no marker, so no ck command collects it, then or later; backfill (Story
  7) is for runs from before this feature and SHALL NOT be described as recovering it (D10).
- 2.6 WHEN the vendored copies of the changed skills, commands and agent-mode instructions are compared
  with the packaged copies, THE SYSTEM SHALL find them identical apart from the existing plugin-only
  differences.

### Story 3: Collect an agent-mode run

**As a** ck user **I want** `ck stats collect --agents <run_id>` to build a local record of an agent-mode
run **So that** its time, cost and outcome sit next to my workflow runs

**Acceptance Criteria:**
- 3.1 WHEN I run `ck stats collect --agents <run_id>` for a run whose manifest exists, THE SYSTEM SHALL
  collect every agent whose meta description starts with that run's marker, and no other agent. It SHALL
  look in the manifest's session first. When the manifest has no session id, or that session has no
  marked agents, it SHALL look in every session under the Claude configuration directory.
- 3.2 WHEN the record is written, THE SYSTEM SHALL compute per-agent model, token usage, cost, duration
  and wall time from the agent transcripts and meta files, with the same pricing and the same rules as
  workflow runs. Timing SHALL come from transcript timestamps. For the same transcripts, the run's priced
  total SHALL equal the sum that `ck cost` gives for those agents, to the cent.
- 3.3 WHEN the record is written, THE SYSTEM SHALL take the spec name, project root, target branch and
  kind from the manifest, never from Agent descriptions (D5). It SHALL store them in the local record only.
- 3.4 WHEN the kind is `implement`, THE SYSTEM SHALL run `ck worktree report` for the manifest's spec and
  target branch in the manifest's project root, and take tasks failed, blocked and not started, and the run
  outcome, from that report (D6), judged only over the tasks this run worked on: the task ids named by at
  least one of the run's agent rows (D11). The outcome SHALL be `failed` if any such task failed, else
  `blocked` if any such task is blocked or not started, else `complete`; tasks failed, blocked and not started
  SHALL count only such tasks, and tasks of the spec that no agent of the run named SHALL NOT affect them.
  When no agent row names a task, the outcome SHALL be unknown. Tasks merged SHALL count only this run's work: the tasks the report shows
  merged or done that were not Done when `begin` ran (1.2). When that set is unknown, tasks merged SHALL be
  the tasks merged at or after the run's start and no later than 30 minutes after its last agent finished.
- 3.5 WHEN the project root no longer exists, is not a git repository, or the target branch is gone, or
  the report fails, THE SYSTEM SHALL still write the record with the outcome and task counts unknown (not
  zero), and list the outcome as missing.
- 3.6 WHEN the kind is `create` or `review`, THE SYSTEM SHALL record the outcome as unknown and SHALL NOT
  run `ck worktree report`.
- 3.7 WHEN the run is collected, THE SYSTEM SHALL record that it came from agent transcripts (source
  `agents`). It SHALL record a run-scope cost source of `agent_transcripts` when every agent was priced
  from its transcript, and `estimate` otherwise.
- 3.8 WHEN collection succeeds, THE SYSTEM SHALL close the manifest so that it is no longer open, and
  print the record path. With `--json` it SHALL print the run id, result (`collected`, `unchanged` or
  `updated`), path and whether the record is partial.
- 3.9 WHEN the same run is collected again, THE SYSTEM SHALL behave as for workflow runs: an identical
  record is `unchanged`, a changed one replaces the old record and is `updated`.
- 3.10 WHEN no manifest exists for the id but marked agents exist, THE SYSTEM SHALL write a partial record
  from those agents. The record SHALL list the manifest as missing. The project root SHALL come from the
  session's working directory, and the spec and outcome SHALL be unknown. The kind SHALL be `create`
  (spec-create workflow, mode unknown) when every marked agent matched a `create <step>` template, otherwise
  `implement` (spec-implement workflow). The git report SHALL NOT run, because the spec is unknown.
- 3.11 WHEN neither a manifest nor any marked agent exists for the id, THE SYSTEM SHALL say the run was not
  found, name the directories it searched, write nothing, and exit non-zero.
- 3.12 WHEN the manifest exists but no marked agent is found, THE SYSTEM SHALL write no record, leave the
  manifest open, say that no agents carry the marker, and exit non-zero.
- 3.13 WHEN the id does not match `ag_[0-9a-f]{8}`, THE SYSTEM SHALL reject it with a message showing the
  expected form, write nothing, and exit with status 2. WHEN an agent-mode id is given to `--run`, the
  message SHALL point to `--agents`.
- 3.14 WHEN `--agents` is combined with `--run`, `--all` or `--agents-backfill`, THE SYSTEM SHALL reject
  the combination and exit with status 2.
- 3.15 WHEN some marked agents have missing, truncated or unparseable transcripts or meta files, THE SYSTEM
  SHALL write a record of what it could read, list what is missing, label any cost not backed by a
  transcript as an estimate, and never show a missing number as zero.
- 3.16 WHEN collect is run from a sandboxed shell that cannot read the Claude configuration directory, THE
  SYSTEM SHALL say it must run outside the sandbox and exit non-zero, without writing a record or closing
  the manifest.
- 3.17 WHEN a `general-purpose` agent's description carries no marker, or carries the marker of a run that
  has neither a manifest nor the id I named, THE SYSTEM SHALL never include it in any agent-mode record
  (D7).
- 3.18 WHEN the agent-mode run is collected, THE SYSTEM SHALL leave workflow-run collection unchanged
  except for the worktree terms of 9.1. A workflow record whose metas carry no `worktreePath` or
  `worktreeBranch` SHALL re-collect as `unchanged`. One whose metas carry them SHALL be `updated` once, and
  only its `terms` SHALL change.

### Story 4: Attribute agent-mode agents to roles

**As a** ck maintainer **I want** agent-mode agents attributed to the same roles as workflow agents **So
that** workflow and agent-mode numbers are comparable

**Acceptance Criteria:**
- 4.1 WHEN an agent's type is a spec agent type, THE SYSTEM SHALL take its role from the type
  (implementer, test-writer, fixer, resolver, reviewer). The verb SHALL refine it: for example an
  implementer-type agent whose description says `review` is a reviewer.
- 4.2 WHEN a marked description gives a task number, a retry number or a round number, THE SYSTEM SHALL
  record the task, the attempt (retry `k` is attempt `k + 1`) or the round, as for workflow labels.
- 4.3 WHEN a marked `general-purpose` agent says `create <step>`, THE SYSTEM SHALL give it the role and
  phase that the spec-create workflow gives the same step.
- 4.4 WHEN a marked description does not match any template, THE SYSTEM SHALL attribute the agent to
  `other`, count it in the run's unmatched agents, and SHALL NOT guess a role. `ck stats show` SHALL report
  how many agents, minutes and dollars fell into `other`.
- 4.5 WHEN a model is given in the agent's meta, THE SYSTEM SHALL use it. Otherwise the model SHALL come
  from the transcript, as for workflow agents.
- 4.6 WHEN a marked agent's type is not a spec agent type (for example `general-purpose`), THE SYSTEM SHALL
  take its role from the verb alone: `implement` or `revise` gives implementer, `review` gives reviewer,
  `resolve` gives resolver, `fix` gives fixer, and `create <step>` follows 4.3.

### Story 5: Auto-collect at the end of an agent-mode run

**As a** ck user **I want** agent-mode runs collected automatically when they finish **So that** no run is
lost to transcript pruning

**Acceptance Criteria:**
- 5.1 WHEN an agent-mode `/spec:implement`, `/spec:create`, `/spec:review` or `/spec:spawn-worktree` run
  finishes and `begin` gave a run id, THE SYSTEM SHALL have the orchestrator run
  `ck stats collect --agents <run_id> --auto` outside the sandbox as its last reporting step.
- 5.2 WHEN `--auto` is given with `--agents`, THE SYSTEM SHALL exit zero whatever happens. It SHALL print
  one line with the record path or the reason it failed and the manual retry command.
- 5.3 WHEN the auto-collect setting is off, THE SYSTEM SHALL skip collection under `--auto` and say so.
  The manifest SHALL stay open. Manual `ck stats collect --agents` SHALL keep working.
- 5.4 WHEN automatic collection fails, THE SYSTEM SHALL leave the run's outcome, report and exit status
  exactly as they would be without collection.
- 5.5 WHEN an agent-mode run happens inside a `ck run` session (sandboxed), THE SYSTEM SHALL have
  `ck stats collect --agents <run_id> --auto` print that `ck run` collects the run after the session exits
  (exit zero, manifest left open). After the session exits, `ck run` SHALL collect, as for workflow runs,
  the agent-mode runs of that session that have a manifest: each open manifest whose session id is that
  session, and each open manifest with no session id that started at or after the session began. It SHALL
  NOT collect or close an open manifest whose session id is another session's (D13). It SHALL NOT collect a
  marked run that has no manifest (3.17, 6.7); the deferral line SHALL name
  `ck stats collect --agents <run_id>` for that case, which collects it as a partial record (3.10) (D14).

### Story 6: Open manifests: doctor and collect --all

**As a** ck user whose session crashed or closed before collect **I want** unfinished collections found
and collected later **So that** those runs still leave data (D9)

**Acceptance Criteria:**
- 6.1 WHEN `ck doctor` runs and open manifests older than 6 hours exist, THE SYSTEM SHALL report how many,
  without naming specs, roots or branches, and suggest `ck stats collect --all`.
- 6.2 WHEN all open manifests are 6 hours old or younger, THE SYSTEM SHALL NOT report them.
- 6.3 WHEN `ck stats collect --all` runs, THE SYSTEM SHALL also collect every open manifest older than 6
  hours as a partial record (Story 3 rules) and close it. It SHALL leave younger open manifests alone and
  count them as unfinished.
- 6.4 WHEN `--all` finishes, THE SYSTEM SHALL count agent-mode runs in the same summary as workflow runs
  (collected, already had, updated, unfinished, abandoned, failed with id and reason).
- 6.5 WHEN `ck stats collect --all` meets an open manifest older than 6 hours that has no marked agent in
  any session, THE SYSTEM SHALL report it once as `abandoned` with the reason "no agents carry the marker",
  write no record, close the manifest, and SHALL NOT count it toward the failure exit code. Later `--all` and
  `ck doctor` runs SHALL NOT report it. It SHALL continue with the other runs.
- 6.6 WHEN a run manifest is unreadable or malformed, THE SYSTEM SHALL count it as failed during `--all`
  without printing its contents, and `ck doctor` SHALL count it in a separate warning.
- 6.7 WHEN `--all` collects agent-mode runs, THE SYSTEM SHALL collect only runs that have a manifest. It
  SHALL NOT collect marked agents without a manifest (those need `--agents <id>`) and SHALL NOT run the
  backfill heuristic.

### Story 7: Backfill older implement-mode runs

**As a** ck user with past agent-mode runs on a work machine **I want** to recover them as partial records
after a preview **So that** my history is not empty before this feature (D8)

**Acceptance Criteria:**
- 7.1 WHEN I run `ck stats collect --agents-backfill --dry-run`, THE SYSTEM SHALL list the groups it would
  collect: for each, the session, the spec parsed from old descriptions, the agent count, start time, wall
  time and estimated cost. It SHALL also list the agents it considered but left out, with the reason. It
  SHALL write nothing.
- 7.2 WHEN backfill considers agents, THE SYSTEM SHALL consider only agents of a spec agent type that carry
  no marker. Agents of Workflow-tool runs and `general-purpose` agents SHALL never be included.
- 7.3 WHEN backfill groups agents, THE SYSTEM SHALL group by session, then by spec name parsed from the old
  free-form descriptions (`implement <spec>: task <N>`, `fix <spec>: …`, `revise <spec> task <N>: …`,
  `review task <N> …`), then split a group where the gap between consecutive agents (from the end of one to
  the start of the next) exceeds the gap threshold. The default threshold is 2 hours, and `--gap` overrides it in minutes.
- 7.4 WHEN an agent's description names no spec, THE SYSTEM SHALL put it in the group of its session whose
  time span contains or lies within the gap threshold of it. When there is no such group, it SHALL leave
  the agent out and list it in the dry run as "no spec". A group's time span is that of its spec-named
  agents; a spec-less agent that joins SHALL NOT move it (7.5, D15).
- 7.5 WHEN I run `ck stats collect --agents-backfill` without `--dry-run`, THE SYSTEM SHALL write each
  group as a partial record. The record SHALL be marked partial, list the manifest as missing, have source
  `agents`, and take its outcome from `ck worktree report` under the same rules as 3.4-3.5, with the group's
  first spec-named agent's start and last spec-named agent's end standing for the run's (spec-less agents
  that joined under 7.4 add their cost and time to the record only, and never set the group's start, end,
  merge window or spacing, D15), the tasks named by its spec-named agents standing for the tasks this run
  worked on (3.4, D11), Done-at-begin unknown, and the 30-minute
  allowance cut to the gap threshold when that is smaller. The project root SHALL come from the session's
  working directory, and the target branch from the session's branch.
- 7.6 WHEN backfill runs twice over the same transcripts, THE SYSTEM SHALL produce the same record ids and
  no duplicate records. A group already collected SHALL be `unchanged`. WHEN a group shares an agent with an
  existing backfill record whose agents are not exactly the group's (for example after a `--gap` change),
  THE SYSTEM SHALL leave the group out with the reason `overlaps backfill record <id>` and keep the existing
  record, so no agent is counted in two records.
- 7.7 WHEN no candidate agents exist, THE SYSTEM SHALL say so and exit zero.
- 7.8 WHEN `--gap` is not a positive integer, THE SYSTEM SHALL reject it and exit with status 2.
- 7.9 WHEN backfill finishes, THE SYSTEM SHALL print a summary of the counts. The documentation SHALL state
  that create and review runs from before this feature cannot be recovered, and that runs whose
  `ck stats begin` failed (no marker) cannot be recovered either: backfill covers only runs from before this
  feature (D10).

### Story 8: Shareable records of agent-mode runs

**As a** ck maintainer **I want** shared records to say whether a run came from a workflow or from agent
transcripts **So that** I can compare the two without any project content leaving the machine (D1, D5)

**Acceptance Criteria:**
- 8.1 WHEN a shareable record is built from an agent-mode local record, THE SYSTEM SHALL set `source` to
  `agents`. From a workflow local record it SHALL set `source` to `workflow`.
- 8.2 WHEN a shareable record is built from an agent-mode run priced fully from transcripts, THE SYSTEM
  SHALL set its run cost source to `agent_transcripts`.
- 8.3 WHEN a shareable record is validated, THE SYSTEM SHALL accept a record with or without `source`, and
  reject one whose `source` is not `workflow` or `agents`, or whose cost source is outside the documented
  set.
- 8.4 WHEN the schema documentation is checked against the validator by the existing automated test, THE
  SYSTEM SHALL find `source` and `agent_transcripts` documented, with the schema id unchanged at
  `ck-run-analytics/1`.
- 8.5 WHEN an agent-mode shareable record is built, THE SYSTEM SHALL include no run id, session id, agent
  id, description, spec name, path or branch, by the same allowlist construction as workflow records.
- 8.6 WHEN existing shareable records without `source` are read, validated or submitted, THE SYSTEM SHALL
  treat them exactly as before.

### Story 9: Leak check covers worktree paths and branches

**As a** ck user **I want** worktree paths and branch names from agent meta files treated as confidential
**So that** they can never slip into a shared record or note

**Acceptance Criteria:**
- 9.1 WHEN any run (workflow or agent mode) is collected and an agent's meta file has a `worktreePath` or
  `worktreeBranch`, THE SYSTEM SHALL add each value, and the path's segments under the same rules as other
  paths, to the record's confidential terms.
- 9.2 WHEN an agent-mode record is collected, THE SYSTEM SHALL add the manifest's spec name, project root,
  target branch and the agent-mode run id to its confidential terms.
- 9.3 WHEN an export or issues note contains a planted worktree path, worktree branch, or agent-mode run id
  from a local record, THE SYSTEM SHALL report a leak hit and refuse the export, as for other confidential
  terms.
- 9.4 WHEN the confidential vocabulary of shared values is built, THE SYSTEM SHALL include `workflow`,
  `agents` and `agent_transcripts`, so that they are never flagged as leaks.

## Specification Heritage

- **Evolves From:** `specs/run-analytics` (ck 0.10.x run analytics).
- **Preserves:** workflow-run collection, the allowlist of workflow names (its Story 2), local-only
  automatic collection, `--auto` never blocking a run, schema `ck-run-analytics/1`, shareable records
  built only from allowlisted fields, the leak check, and the run-analytics rules for partial records
  (missing parts listed, nothing missing shown as zero).
- **Extends:** run-analytics Story 1 (a second collection source), Story 3 (auto-collect in agent mode),
  Story 4 (`--all` also collects open manifests), Story 5 (an agent-mode role grammar), Story 9 (more
  confidential terms) and Story 12 (`ck doctor` reports open manifests).
- **Supersedes:** the free-form implement description `implement <spec>: task <N>` in the agent-mode
  instructions and `implement <feature>: <task-summary>` in `/spec:spawn-worktree`.
- **Evolution Reason:** machines without the Workflow tool produce no analytics today (PLAN.md problem
  statement).

## Non-Functional Requirements

- **Never blocks a run:** with `begin` and collect both forced to fail, an agent-mode run's report and exit
  status match a run with analytics off (1.5, 1.6, 5.2, 5.4).
- **Privacy:** no Agent description template contains a spec, title, path or branch placeholder (2.2). The
  seeded leak test passes with zero hits after planting worktree paths, branches and agent-mode ids (9.3).
- **Agreement with existing numbers:** an agent-mode record's transcript-priced total equals `ck cost`
  over the same agent transcripts to the cent (3.2).
- **No network:** `begin`, collect `--agents`, `--agents-backfill` and `ck doctor` make no network calls.
  In tests, `ck worktree report` runs against local git repositories only.
- **Performance:** collecting one 100-agent agent-mode run from fixtures takes under 5 seconds. A backfill
  dry run over 50 fixture sessions takes under 30 seconds on a developer laptop.
- **Local file protection:** run manifests, like local records, are created with mode 0600, in a directory
  with mode 0700 when ck creates it.
- **Backward compatibility:** every existing test of workflow collection, show, export, submit and doctor
  passes unchanged. Records written before this feature still load and show.

## Constraints

- Read-only use of Claude Code's private transcript and meta-file layout. The parser tolerates missing and
  extra meta keys. Known keys include `agentType`, `description`, `toolUseId`, `spawnDepth`, `model`,
  `parentAgentId`, `worktreePath` and `worktreeBranch`, and the meta has no start or end time.
- Transcript parsing, pricing, aggregation and record building reuse the workflow collector's logic.
  There is no second pricing path.
- The shareable schema stays `ck-run-analytics/1`, and every addition is optional (D1).
- Agent descriptions carry only the marker, a fixed verb, a task number and an attempt or round (D4).
- Shared records stay content-free. Local records keep their detail for the leak check (D5).
- Changes to the skills, commands and agent-mode instructions are made in the packaged source and kept
  identical in the vendored copies. Edits under `.claude/` cannot land through the sandboxed task flow, so
  they are grouped into as few tasks as possible and landed by hand.
- Tests are integration-first. They build fake Claude configuration directories with Agent-tool subagent
  transcripts and meta files, and run the real CLI. Fixtures and docs use invented project names only.
- Collection runs outside the sandbox, as for workflow runs.

## Out of Scope

- Option B (rebuilding runs only from the orchestrator's session transcript) and Option C
  (OpenTelemetry), as PLAN.md rejected them.
- Timing-only grouping of agents for new runs. Grouping is by marker. The time-gap heuristic is used only
  by backfill (D3).
- Backfill of create and review runs, and of any `general-purpose` agent without a marker (D7, D8).
- Recovering runs from after this feature whose `ck stats begin` failed: their agents carry no marker, and
  backfill is for runs from before this feature only (D10). The run's report says it was not recorded (2.5).
- A new shareable schema version, or any new required shareable field (D1).
- Spec names, task titles or paths in Agent descriptions, in any form (D4).
- Passing a `ck worktree report` file to collect. The collector runs the report itself (D6).
- Ad-hoc `/spec:spawn-worktree` batches that have no spec.
- `/spec:plan` coverage, automatic export or submission, and any hosted service or daemon, as in
  run-analytics.
- Changing how agent-mode runs schedule, retry, review or land work, beyond adding `begin`, the marker
  and the final collect.

## Assumptions

- The meta `description` of an Agent-tool subagent is exactly the description the orchestrator passed, so
  the marker is found at its start.
- `ck stats begin` cannot always know the orchestrator's session id. When it can't, collect searches every
  session for the marker (3.1). The marker is random, so the search is still exact.
- D4 lists example templates. This document requires those as the minimum set (2.1). The exact list of
  `create <step>` names, and whether revisions, test-track work and land fixes get their own fixed verbs
  (for example `revise task <N> round <k>`), are design choices. Any such template must keep to D4's
  content rule and must parse (2.3).
- Outcome mapping for implement runs from the git report, over the tasks the run worked on (3.4, D11):
  every such task merged is `complete`. If none of them failed but some are blocked or not started, it is
  `blocked`. If any of them failed, it is `failed`. The design may refine this within the existing outcome set.
- The backfill gap default of 2 hours is a starting value (PLAN open question). `--dry-run` exists to tune
  it. Hand-run fixer agents of spec agent types may land in backfill groups. The dry run's per-group agent
  list is how I see and avoid that (PLAN open question).
- The 6-hour age for open manifests counts from the manifest's start time.
- An agent-mode create or review run has no git-based outcome, so its outcome stays unknown. It is
  `other` in shared records.
