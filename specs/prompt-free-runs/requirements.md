# Feature: Prompt-Free Runs

## Overview

Unattended `/spec:implement`, `/spec:create` and `/spec:review` runs still stop for my manual approval.
Over three weeks there were about 49 real permission prompts during spec work. Most came from ck's own
pre-tool guard asking about harmless shell commands on test files (`sed -i`, `rm` of a scratch test,
python rewrites, `rm -rf tests/__pycache__`) and about my own edits to requirements.md. On top of that,
about 985 tool calls were refused by Claude Code's worktree-isolation check in sessions isolated in a
`.claude/worktrees/` worktree, plus about 38 auto-mode denials and 15 headless "needs approval" denials.

This feature makes those runs prompt-free and keeps the guard's safety:

- The guard judges shell commands by their result, not their text. Before a command it blocks only
  deleting or moving a test file that exists on the base branch. After the command, a post-check compares
  the test files the command changed and applies the same weakening check the edit path uses.
- Task agents are denied, never asked. A task stopped by the guard climbs a model ladder up to Fable. If
  Fable cannot resolve it, the task is parked as "needs input", I get a push notification, and the rest of
  the run continues. My own interactive session is still asked, as `specs/ck.json` says.
- My own interactive edits to requirements.md and test-plan.md pass.
- A whole-spec run started from a session isolated in a worktree is handed off to `ck run` from the main
  checkout.
- `ck stats` records, for each run, how many prompts, denials and refusals happened, by role and command
  family, and how long the run waited on me. This works for Workflow-tool runs and agent-mode runs.

Decision record: [PLAN.md](PLAN.md). Decisions D1-D20 bind this document. Where this document and PLAN.md
disagree, PLAN.md wins.

Terms used below:
- **Guard**: ck's pre-tool hook that protects tests, requirements.md and test-plan.md.
- **Post-check**: the new after-the-command check of test files changed by a shell command.
- **Test file**: a path that matches one of the project's test-file patterns, or a source-extension file
  under a test directory. Compiled and cache files are not test files, even under a test directory: for
  example `tests/__pycache__/*.pyc` and the contents of `tests/.pytest_cache`.
- **Existing test file**: a test file that exists on the base branch the guard already uses for the edit
  path. A test file that is new on the branch is not existing.
- **Owned test file**: a test file listed by a test-track task that is In Progress.
- **Weakening**: what the edit path already blocks: removing assertions or test cases, adding
  skip/xfail/only markers, or deleting an existing test.
- **Task agent**: a hook call whose input carries an agent id, or that comes from a headless session (a
  `ck run` or `claude -p` run), or, with no agent id, that carries the marker ck sets for the agents its
  workflows start (the fallback marker, Story 3).
- **Main session**: a hook call with no agent id, no fallback marker, from an interactive session. Where the
  call's paths point (the main checkout or a task worktree) does not change this.
- **Guard stop**: a guard denial of a task agent, or a post-check catch the task agent did not restore,
  that leaves the task unfinished.
- **Interruption**: a guard ask, guard denial, post-check catch, guard escalation, auto-mode denial,
  headless denial, user rejection or isolation refusal.
- **Command family**: one of `sed`, `rm`, `mv`, `git`, `python`, `redirect`, `other`.
- **Role**: one of `implementer`, `test-writer`, `reviewer`, `fixer`, `resolver`, `orchestrator`.

## User Stories

### Story 1: Shell commands on tests pass unless they delete or move an existing test (D1, D5)
**As a** developer running specs unattended **I want** the guard to let harmless shell commands on test
files through **So that** runs never stop to ask about a `sed -i` or a scratch file.

**Acceptance Criteria:**
- 1.1 WHEN a shell command deletes or moves an existing test file that is not owned with one of the
  commands `rm`, `mv`, `git rm`, `git mv`, `truncate` or `unlink` (this list is complete), THE SYSTEM SHALL
  block it before it runs: deny for a task agent (Story 4), and apply the configured `guard.tests` mode for
  the main session. Other deletions, for example `find ... -delete` or `find ... -exec rm`, are not blocked
  before they run; the post-check catches them after the command (2.10) and the gate re-checks at Done (D18).
- 1.2 WHEN a shell command edits test files in place (`sed -i`, `perl -i`), rewrites them with a script
  (for example a python heredoc), or writes to them through a `>` or `>>` redirect, THE SYSTEM SHALL let
  it run without asking or denying before it runs.
- 1.3 WHEN a shell command deletes or moves a test file that is new on the branch, an owned test file,
  or a file under a test directory that is not itself a test (for example `tests/__pycache__`), THE
  SYSTEM SHALL let it run.
- 1.4 WHEN a shell command starts with `cd <dir> &&` (or `;`), THE SYSTEM SHALL resolve the paths after
  it against `<dir>`, so a deletion of an existing test reached through a `cd` prefix is still blocked
  (1.1) and a deletion of a new file reached that way still passes (1.3).
- 1.5 WHEN a block reason is given to a task agent, THE SYSTEM SHALL tell it to keep the test, not to work
  around the block, and to report the conflict quoting the test and the requirement.
- 1.6 WHEN `guard.tests` is turned off in `specs/ck.json`, THE SYSTEM SHALL neither block (1.1) nor
  post-check (Story 2) shell commands.
- 1.7 WHEN the guard cannot tell whether a target exists on the base branch (no git, no base branch,
  unreadable repository), THE SYSTEM SHALL let the command run and leave the decision to the post-check
  and the gate, never asking because of the uncertainty.

### Story 2: Post-check catches weakening done through the shell (D5)
**As a** developer **I want** the effect of a shell command on existing tests checked after it runs
**So that** a `sed` or script that weakens a test is caught with the same accuracy as an edit.

**Acceptance Criteria:**
- 2.1 WHEN a shell command finishes and has changed one or more existing test files that are not owned,
  THE SYSTEM SHALL run the same weakening check the edit path uses on each changed file, comparing its
  content before the command with its content after. A changed file whose content after the command equals
  its content on the base branch, or on a branch or commit the command merged, rebased or pulled in (for
  example the target of a task's required `git merge <into>`), SHALL not be reported: that change came from
  there (D19). The files compared are those in the checkouts and paths the command names; a path a script
  builds at run time into another worktree is left to the gate (2.8, D20).
- 2.2 WHEN the post-check finds weakening in a task agent's command, THE SYSTEM SHALL tell the agent, in
  its next turn, which file and which kind of weakening (removed assertion, removed test case, added
  skip marker, deleted test), and that it must restore the test and report the conflict.
- 2.3 WHEN the post-check finds weakening in the main session and `guard.tests` is `ask`, THE SYSTEM SHALL
  tell Claude which file was weakened and that it must ask me before keeping the change.
- 2.4 WHEN the changed test files are new on the branch, owned, or changed without weakening (for example
  an added assertion or a renamed variable), THE SYSTEM SHALL report nothing.
- 2.5 WHEN a shell command cannot write files (the pre-filter decides from the command), or the repository
  has no changed test files after it, THE SYSTEM SHALL skip the comparison.
- 2.6 WHEN a test file was already changed before the command and the command did not change it further,
  THE SYSTEM SHALL not report it again for this command.
- 2.7 WHEN the post-check fails (git error, unreadable file, timeout), THE SYSTEM SHALL not block or delay
  the run, and SHALL leave the weakening check to the gate, which still re-checks at Done.
- 2.8 WHEN a task agent reaches Done with a weakened existing test, THE SYSTEM SHALL fail the gate as it
  does today.
- 2.9 WHEN the post-check finds weakening in the main session and `guard.tests` is `deny`, THE SYSTEM SHALL
  tell Claude which file was weakened and which kind of weakening, and that it must restore the test. The
  text SHALL not ask it to report a conflict (that wording is for task agents, 2.2).
- 2.10 WHEN a shell command deletes an existing test file that is not owned in a way 1.1 does not block
  before it runs (for example `find tests -name test_x.py -delete`), THE SYSTEM SHALL report it after the
  command as a deleted test, with the wording of 2.2, 2.3 or 2.9 for the caller and mode, and the gate SHALL
  still re-check at Done (2.8).

### Story 3: Tell a task agent from my main session (D7)
**As a** developer **I want** the guard to know whether the call comes from a task agent or from me
**So that** agents are denied and only I am asked.

**Acceptance Criteria:**
- 3.1 WHEN hook input carries an agent id, THE SYSTEM SHALL treat the call as a task agent.
- 3.2 WHEN the hook runs in a headless session (`ck run` or `claude -p`), THE SYSTEM SHALL treat the call
  as a task agent, also when it has no agent id.
- 3.3 WHEN hook input has no agent id, no fallback marker, and comes from an interactive session, THE
  SYSTEM SHALL treat the call as the main session.
- 3.4 WHEN an agent started by the Workflow tool calls a guarded tool, THE SYSTEM SHALL treat it as a task
  agent. An automated test SHALL establish whether such calls carry an agent id; if they do not, ck SHALL
  set a marker for the agents its workflows start (`/spec:implement`, `/spec:create` and `/spec:review`
  runs), and the guard SHALL read that marker only for calls that carry no agent id. The marker SHALL be
  set by ck, never inferred from the paths a call touches. The marker SHALL be cleared at the start of every
  skill run and every new prompt I submit in the session, and SHALL expire 15 minutes after the last guarded
  call that read it as active, so it never outlasts an interrupted Workflow run by more than that (D16).
- 3.5 WHEN hook input is missing fields or carries unknown ones, THE SYSTEM SHALL classify it from the
  fields present and never fail the hook because of the classification.
- 3.6 WHEN the main session (no agent id, no fallback marker, interactive) edits, deletes or moves a test
  inside a ck task worktree, for example to resolve a parked task, THE SYSTEM SHALL treat it as the main
  session and apply the configured guard mode (4.2). Being inside a task worktree SHALL not by itself make a
  call a task agent.

### Story 4: Task agents are denied, never asked (D6)
**As a** developer **I want** guard blocks of task agents to be denials **So that** no run waits on a
prompt nobody sees.

**Acceptance Criteria:**
- 4.1 WHEN the guard blocks a task agent's shell command or file edit (tests, requirements.md,
  test-plan.md), THE SYSTEM SHALL deny it, whatever `specs/ck.json` `guard` says, unless that guard kind
  is turned off.
- 4.2 WHEN the guard blocks a call from the main session, THE SYSTEM SHALL apply the configured guard mode
  (ask or deny) as today.
- 4.3 WHEN a task agent is denied, THE SYSTEM SHALL return a reason that names the file and the kind of
  block, so the agent can report it.

### Story 5: My own spec edits pass (D4)
**As a** developer revising a spec during `/spec:plan` or `/spec:review` **I want** my edits to
requirements.md and test-plan.md to pass **So that** I am not asked about my own changes.

**Acceptance Criteria:**
- 5.1 WHEN the main session changes or removes existing text in requirements.md or test-plan.md of a spec
  whose implementation has started, THE SYSTEM SHALL let the edit through without asking.
- 5.2 WHEN a task agent makes the same edit, THE SYSTEM SHALL block it as today, as a denial (4.1).
- 5.3 WHEN a task agent only adds requirements or test cases, THE SYSTEM SHALL let it through as today.
- 5.4 WHEN `/spec:review` (Workflow tool or Agent-tool fallback, interactive or handed off to `ck run`)
  finds a fix that changes or removes existing text in requirements.md or test-plan.md of a spec whose
  implementation has started, THE SYSTEM SHALL not apply it, and SHALL return it to me as a decision item
  with the proposed change, in the review result's open questions and in review.md "Needs Your Decision",
  the same path behaviour-changing fixes already take. The guard SHALL keep treating the revise agents as
  task agents for these files (5.2), and the denial reason SHALL tell the agent to report the change as a
  decision instead of applying it. Add-only fixes are applied as today (5.3). Automated tests SHALL cover
  both modes (D14).

### Story 6: Guard-stopped tasks climb to Fable (D6, D12)
**As a** developer **I want** a task the guard stopped to be retried by every stronger model **So that**
a real attempt is made before I am bothered.

**Acceptance Criteria:**
- 6.1 WHEN a task ends with a guard stop, THE SYSTEM SHALL retry it once on each model above the one that
  attempted it, in the order sonnet, opus, fable, skipping models not above it, until an attempt finishes
  without a guard stop.
- 6.2 WHEN the guard ladder runs, THE SYSTEM SHALL use it whatever `implementer.escalate` lists, keep the
  role's effort, and start each attempt by reading the previous attempt's handoff note.
- 6.3 WHEN a task fails for any other reason (gate red, review findings, red branch), THE SYSTEM SHALL
  escalate it only as `implementer.escalate` says today.
- 6.4 WHEN the attempt that hit the guard stop already ran on Fable, THE SYSTEM SHALL go straight to
  Story 7 without more attempts.
- 6.5 WHEN the run uses the Workflow tool, and WHEN it uses the Agent-tool fallback (agent-mode scheduler
  instructions and `/spec:spawn-worktree`), THE SYSTEM SHALL apply 6.1-6.4 the same way. Automated tests
  SHALL cover both.
- 6.6 WHEN a guard escalation happens, THE SYSTEM SHALL record the model escalated to in the task's result
  and in the run's report.

### Story 7: Park as "needs input" and ping me (D6, D12)
**As a** developer away from my machine **I want** a task that even Fable could not resolve parked with
a push notification **So that** I can decide without the run stalling.

**Acceptance Criteria:**
- 7.1 WHEN the guard ladder ends unresolved in an unattended run, THE SYSTEM SHALL park the task as "needs
  input" with the quoted test and the quoted requirement, keep running every task that does not depend on
  it, and not start tasks that depend on it.
- 7.2 WHEN a task is parked, THE SYSTEM SHALL send one notification through the notification command I
  configured. The message SHALL name the spec, the task number and that a decision is needed, and SHALL
  not carry the quoted test or requirement text.
- 7.3 WHEN no notification command is configured, THE SYSTEM SHALL park the task without sending anything,
  and the final report SHALL say that no notification was sent and how to configure one.
- 7.4 WHEN the notification command fails or does not finish within 10 seconds, THE SYSTEM SHALL continue
  the run and name the failure in the final report.
- 7.5 WHEN the run ends with parked tasks, THE SYSTEM SHALL list each in the final report, in the run's
  result and in `ck worktree report`, with the quoted test, the quoted requirement and the decision
  needed, apart from merged, failed and blocked tasks.
- 7.6 WHEN the guard ladder ends unresolved in an interactive run, THE SYSTEM SHALL ask me with a question
  that quotes the test and the requirement, instead of parking.
- 7.7 WHEN the run uses the Workflow tool, and WHEN it uses the Agent-tool fallback, THE SYSTEM SHALL
  apply 7.1-7.6 and 7.9-7.11 the same way. Automated tests SHALL cover both.
- 7.8 WHEN I later run `/spec:implement <spec>` again, THE SYSTEM SHALL treat a parked task like any
  unfinished task (fresh attempt, worktree reused).
- 7.9 WHEN I answer the question of 7.6, or record a decision for a parked task, THE SYSTEM SHALL store my
  answer beside the task's worktree, clear the task's park note, and give the answer to every later attempt
  of that task, which SHALL read it before anything else.
- 7.10 WHEN my answer is that the code must satisfy the test, THE SYSTEM SHALL run the task's next attempt
  with that answer, under the same guard (4.1).
- 7.11 WHEN my answer is that the test or the requirement is wrong, THE SYSTEM SHALL tell me to change it
  from my main session (where 4.2 and 5.1 apply) before the task's next attempt. A task agent SHALL still
  not be allowed to weaken the test (4.1). WHEN the next attempt still ends with a guard stop, THE SYSTEM
  SHALL handle it by Stories 6 and 7 again, and the new park note SHALL say that an earlier answer exists.
- 7.12 WHEN a task is parked inside a `ck run` session, THE SYSTEM SHALL send its notification (7.2-7.4)
  from `ck run`'s own process outside the sandbox, exactly once per park note, and SHALL not widen the
  sandbox for the notification command (D15).

### Story 8: Hand off whole-spec runs from an isolated worktree session (D8, D11)
**As a** developer who starts runs from a background or EnterWorktree session **I want** the run handed
to `ck run` from the main checkout **So that** it does not waste hundreds of refused calls.

**Acceptance Criteria:**
- 8.1 WHEN a whole-spec `/spec:implement <spec>`, `/spec:create <spec>` or `/spec:review <spec>` starts
  in a session whose working directory is a linked worktree under the project's `.claude/worktrees/`, THE
  SYSTEM SHALL launch the same run through `ck run` from the main checkout, before choosing Workflow or
  agent mode, and not run it in the isolated session.
- 8.2 WHEN the hand-off launches, THE SYSTEM SHALL print the main checkout path, the log path, the command
  to follow the log, and how to find the run's report when it ends.
- 8.3 WHEN `ck run` is used for a create or review run, THE SYSTEM SHALL run it headless, sandboxed and
  without prompts, with the same checks it applies to implement runs.
- 8.4 WHEN the launch fails (sandbox tools missing, `ck run` refuses, the main checkout cannot be found),
  THE SYSTEM SHALL print the reason and the exact command to run by hand from the main checkout, and not
  start the run in the isolated session.
- 8.5 WHEN the session is in the main checkout, in a worktree outside `.claude/worktrees/`, or the command
  works on a single task (`/spec:implement <spec> <N>`), THE SYSTEM SHALL run as today without a
  hand-off.
- 8.6 WHEN arguments follow the spec name, THE SYSTEM SHALL pass them through to the handed-off run.
- 8.7 WHEN the hand-off is needed and `specs/<spec>` at the isolated worktree's HEAD is missing from, or
  differs from, `specs/<spec>` at the main checkout's HEAD, and the main checkout has no uncommitted
  changes, THE SYSTEM SHALL create a run branch `<spec>-run` in the main checkout at the worktree's HEAD,
  switch the main checkout to it, launch `ck run` there, and print the run branch with the 8.2 fields, so
  the run's output lands on that branch (D13).
- 8.8 WHEN the hand-off of 8.7 is needed but the main checkout has uncommitted changes, `specs/<spec>` has
  uncommitted changes in the worktree, or the worktree's HEAD cannot be checked out as `<spec>-run` in the
  main checkout (the branch exists at another commit, or the switch fails), THE SYSTEM SHALL launch nothing,
  leave both checkouts unchanged, and print the reason and the exact recovery commands to run by hand.
- 8.9 WHEN `specs/<spec>` is the same at the worktree's HEAD and at the main checkout's HEAD, THE SYSTEM
  SHALL hand off on the main checkout's current branch as 8.1 says, without a run branch.

### Story 9: Local guard event log (D3, D10)
**As a** developer **I want** each guard decision logged locally with full detail **So that** I can find
which command or test keeps tripping the guard.

**Acceptance Criteria:**
- 9.1 WHEN the guard asks, denies, or the post-check catches weakening, THE SYSTEM SHALL append one event
  with session id, agent id and type (when present), tool, decision (ask / deny / post-check), reason
  category, reason text, repo-relative path, command text (shell commands) and timestamp.
- 9.2 WHEN the guard lets a call pass, THE SYSTEM SHALL write no event.
- 9.3 THE SYSTEM SHALL keep the log under the private ck data directory, never in a project directory,
  created with owner-only permissions.
- 9.4 WHEN the log reaches its size limit, THE SYSTEM SHALL rotate it so that its total size stays bounded.
- 9.5 WHEN the event cannot be written, THE SYSTEM SHALL return the same guard decision it would have
  returned otherwise, without error output that changes the decision.
- 9.6 WHEN an export or submit is leak-checked, THE SYSTEM SHALL treat command texts, paths and reason
  texts from the log as confidential terms, and refuse the export on a hit.

### Story 10: Run records count interruptions (D3, D9, D11, D12)
**As a** developer **I want** each run record to count interruptions and my wait time **So that** I can
drive them to zero.

**Acceptance Criteria:**
- 10.1 WHEN `ck stats collect` builds a record for a workflow run or an agent-mode run of
  `/spec:implement`, `/spec:create` or `/spec:review`, THE SYSTEM SHALL count, from the run's transcripts:
  guard asks, minutes waiting on me, guard denials, post-check catches, guard escalations with the model
  escalated to, auto-mode denials, headless denials, my rejections and isolation refusals.
- 10.2 THE SYSTEM SHALL break each count down by role and, for shell commands, by command family. A
  non-shell interruption SHALL count under family `other`.
- 10.3 THE SYSTEM SHALL measure waiting minutes as the time between a guard ask and the result of the
  same tool call, summed over the run.
- 10.4 WHEN the guard event log has events for the run's sessions, THE SYSTEM SHALL join them in and, when
  the log and the transcripts disagree on guard counts, keep the transcript count and note the
  difference in the local record.
- 10.5 WHEN the guard log is unreadable, or has rotated and its oldest retained event is newer than the run
  start (events of the run were rotated away), THE SYSTEM SHALL still record the transcript counts and list
  the log as missing in the local record. A guard log that was never created SHALL count as read, with zero
  events, and SHALL add nothing to the missing list (D17).
- 10.6 WHEN a transcript line is malformed or unknown, THE SYSTEM SHALL skip it and keep counting.
- 10.7 WHEN a record was built before this feature, THE SYSTEM SHALL treat its interruption counts as not
  recorded, never as zero.
- 10.8 WHEN `ck stats collect --agents`, `--all` and auto-collect build records, THE SYSTEM SHALL include
  the same counts as workflow collection does. Automated tests SHALL cover both modes.

### Story 11: Shared records and `ck stats show` (D9)
**As a** developer **I want** interruption counts in shareable records and in `ck stats show` **So that**
the number is visible and comparable without leaking content.

**Acceptance Criteria:**
- 11.1 WHEN a shareable record is built, THE SYSTEM SHALL include the interruption counts, roles, command
  families, waiting minutes and escalation model names, and SHALL not include command text, paths, spec
  names or reason text.
- 11.2 THE SYSTEM SHALL keep the schema `ck-run-analytics/1`, with the new fields optional.
- 11.3 WHEN `ck stats show` prints a run that has counts, THE SYSTEM SHALL print one interruptions line,
  for example `interruptions: 0 prompts (0 min waiting) · 3 guard denials → 1 escalation (fable) · 0
  refusals`.
- 11.4 WHEN a run has no counts recorded, THE SYSTEM SHALL print `interruptions: not recorded`.
- 11.5 WHEN an older ck reads a record with the new fields, THE SYSTEM SHALL still load it (the fields are
  additive).

## Specification Heritage

- **Evolves From:** the guard behaviour shipped with the hook-scripts package, `specs/run-analytics` and
  `specs/agent-mode-analytics`.
- **Preserves:** the edit-path weakening check, the In Progress test-track exemption, the add-only rule for
  requirements.md and test-plan.md, the gate's re-check at Done, `implementer.escalate` for ordinary
  failures, schema `ck-run-analytics/1`, content-free shared records, the leak check, and the
  run-analytics rule that missing data is listed, never shown as zero.
- **Supersedes:** asking on any `sed -i`/`perl -i`/redirect/`rm`/`mv` command whose target is a test path;
  asking task agents (they are denied); guarding the main session's edits to requirements.md and
  test-plan.md.
- **Evolution Reason:** guard prompts and isolation refusals break unattended runs (PLAN.md problem).

## Non-Functional Requirements

- **Zero prompts on the known cases:** replaying, as a task agent, each ask category from PLAN.md's
  transcript analysis (`sed -i` on a test, `rm` of a new scratch test, python heredoc rewrite, `rm -rf
  tests/__pycache__`, `git rm` of a new test, a `cd <worktree> &&` prefixed `sed -i`) yields zero asks
  and zero denials (1.2-1.4).
- **Same safety:** a `sed -i` that deletes an assertion from an existing test, a python rewrite that adds a
  skip marker, and an `rm` of an existing test are each caught (1.1, 2.1-2.2), also when the test is in a
  task worktree reached by an absolute path and when the command also commits.
- **Hook overhead:** for a shell command the pre-filter skips, the post-check adds under 100 ms. For a
  command that changes test files, it finishes in under 1 second in a repository with 1,000 tracked
  files and 5 changed test files.
- **Never blocks on its own failure:** with the event log, post-check and notification all forced to fail,
  the guard's decisions and the run's outcome match a run where they succeed (2.7, 7.4, 9.5).
- **Privacy:** the seeded leak test passes with zero hits after planting command texts and paths in the
  guard log (9.6, 11.1). The local log file is mode 0600 in a 0700 directory when ck creates it.
- **No network:** the guard, post-check, event log and collection make no network calls. The
  notification command is the only outbound call, and only when I configured it.
- **Backward compatibility:** existing guard, collect, show, export and submit tests pass, except those
  that assert the asks this feature removes, which are updated to the new rules.

## Constraints

- Uses only documented hook input fields (session id, transcript path, cwd, permission mode, event name,
  tool name, tool input, tool use id, agent id, agent type) plus the ck-set fallback marker if 3.4 needs it.
- Read-only use of Claude Code's private transcript layout; the parser tolerates missing and extra keys.
- No change to Claude Code's auto-mode classifier or isolation check.
- The schema stays `ck-run-analytics/1` with additive optional fields only.
- Changes to skills, commands, hooks registration, workflows and agent-mode instructions are made in the
  packaged source and kept identical in the vendored copies under `.claude/`. Those edits land by hand,
  so they are grouped into as few tasks as possible.
- The notification channel is project-agnostic: a command I configure, not a hardcoded service or topic.
- Tests are integration-first: they drive the real hooks and CLI against real git repositories and fake
  Claude homes with real transcript layouts, and are xdist-safe. Fixtures use invented names only.
- The spec stays lean: about 15 tasks at most.

## Out of Scope

- Changing Claude Code's auto-mode classifier or its worktree-isolation guard.
- The requirements.md / test-plan.md guard for task agents (kept as is, D4).
- Changing `implementer.escalate` for ordinary, non-guard failures.
- The run ETA feature (a separate spec may read these counts later).
- Rewriting agent instructions to avoid isolation refusals (D2 replaced by D8).
- Handing off single-task `/spec:implement <spec> <N>` runs, `/spec:plan` and `/spec:spawn-worktree`
  batches started by hand.
- A new shareable schema version, or command text, paths or reason text in any shared record.
- A built-in ntfy client or any hosted notification service.
- An interactive permission prompt to me from inside an agent (a Workflow or Agent-tool agent, or a `ck run`
  session). Changes that need my approval come back as decision items (5.4, Story 7).
- Widening the `ck run` sandbox for the notification command (7.12).

## Assumptions

- "Base branch" for 1.1 and the post-check is the base the edit path already uses (`specs/ck.json` `base`
  or its default).
- A headless session can be recognised from hook input or the environment ck run sets; the design picks
  the signal (3.2).
- The post-check's "content before the command" can be captured cheaply before the command runs or derived
  from git; the design picks the mechanism within the hook overhead targets.
- The notification command is configured once per machine (not per project) and receives the message as
  input; the design picks where it lives. My existing ntfy script can be that command.
- The notification message carries the spec name and task number. It goes to a channel I chose, so this
  is not a shared record; the quoted test and requirement stay out of it (7.2).
- In an interactive run, the main session asks me (7.6); a headless run is always unattended.
- Command-family classification looks at the first command of the shell line after any `cd` prefix:
  `sed`/`perl -i` -> `sed`; `rm`/`unlink`/`truncate` -> `rm`; `mv` -> `mv`; `git ...` -> `git`; `python`,
  `python3`, `uv run python` -> `python`; a write redirect with no other match -> `redirect`; else
  `other`. The design may refine this within the seven families.
- Role attribution for interruptions reuses the run-analytics and agent-mode-analytics role mapping; calls
  in the orchestrating session count as `orchestrator`.
- An isolation refusal, auto-mode denial, headless denial and user rejection are recognised by their fixed
  tool-result phrasing; a guard ask by its hook attachment with decision `ask`.
