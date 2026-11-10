# Feature: Principles and Arbiter for Spec Runs

## Overview

Long `/spec:implement` runs stop for mismatches between the plan, the spec and the implementation. On
`unattended-correctness`, about a dozen spec-level stops occurred across seven runs (about $170), and only one
needed real judgment. The rest had a clear right answer that no agent was allowed to act on. A second cost
shows up earlier: requirements nobody justified get specified, tested and built.

This feature makes runs stop only for decisions that are really mine, and makes every requirement say why it
exists. See [PLAN.md](PLAN.md) (Status: Decided, D1-D24), the source of truth for scope and decisions.

1. **Principles (D5).** A project-wide file holds defaults; each plan adds tie-breakers or overrides.
2. **Justifications (D6, D7).** Every user story carries a `**Without it:**` line. Planning asks for it, lint
   enforces it, a review lens attacks it, task reviewers flag code that serves no requirement. Cuts are only
   ever proposed to me.
3. **An arbiter in runs (D1, D2, D8-D13, D18).** A high-capability role rules on spec claims, citing a
   principle, and each ruling is logged. It may change task wiring, design, the test plan and code. It may only
   suggest requirements.
4. **The same arbiter in `/spec:create` (D14, D15).**
5. **Prevention (D16).** A static lint and a reading audit catch Verify-scope problems before a run spends
   attempts.
6. **Landing repairs (D23, D24).** Two landing gaps found while running unattended-correctness: an integration
   checkout left dirty by a refused raw merge, and bisection that blames a task by elimination.

Terms used below:

- **Spec claim**: a test conflict, a guard stop, or a review deadlock reported on a task.
- **Claim**: anything the arbiter is asked to rule on: a spec claim, a gate failure after the top model failed, an audit
  problem, or a Story 12 lint finding.
- **Safety-tagged task**: a task `ck plan`'s `risk` map marks `safety`, which is a `**Risk:** safety` tag or a file matching
  `risk_paths`, as the implement skill already treats it.
- **Review deadlock**: a task's blocking review findings remain after its configured revision rounds.
- **Ruling**: the arbiter's structured answer to one claim.
- **Needs-input entry**: today's route for a question to me: the task waits with its dependents, independent
  tasks continue, the entry is listed apart from failures and can notify me.
- **Normal tier / risky tier**: the arbiter's two model-and-effort settings.
- **Project layer / plan layer**: the project-wide principles file / a plan's `## Principles` section.
- **Implementation started**: at least one task in the spec is not Not Started.
- **Managed integration checkout**: a checkout of an `integrate/*` branch under ck's worktree directory, which is where
  `ck worktree land` merges.
- **Recorded refusal**: ck's git hook's record that it refused an update of an `integrate/*` branch, naming the branch, its
  head at the time, and the checkout the refused command ran in.
- **I, me**: the user running the spec.

## User Stories

### Story 1: Principles recorded in two layers (D5)

**As a** user running long spec runs **I want** principles recorded before a run starts **So that** a ruling
upholds something I decided, not an opinion.

**Without it:** the arbiter has nothing to uphold, its rulings are opinion, and Task 20's kind of contradiction
cannot be settled by a feature-specific tie-breaker.

**Acceptance Criteria:**

- 1.1 WHEN `/spec:plan`, `/spec:create`, `/spec:review` or a whole-spec `/spec:implement` launch runs in a project with no
  `specs/PRINCIPLES.md`, THE SYSTEM SHALL create it with starter text holding the defaults: priority order requirements >
  design > test plan > task wiring; a test keeps its intent and only selection and setup may move; fail closed on
  security; remove before you optimize. A `/spec:implement` launch SHALL create it on the checkout the run merges into, so
  the arbiter can read it there. It SHALL tell me it created the file.
- 1.2 WHEN one of those commands runs in a project that already has `specs/PRINCIPLES.md`, THE SYSTEM SHALL leave the file
  unchanged.
- 1.3 WHEN `/spec:plan` records a plan, THE SYSTEM SHALL ask me directly for tie-breaking values, overrides of
  project defaults, and what may change without asking, and SHALL write my answers to a `## Principles` section
  of the plan.
- 1.4 WHEN the plan's `## Principles` section is missing or empty, THE SYSTEM SHALL NOT set the plan's Status to
  Decided. A plan with nothing to add SHALL say so in that section.
- 1.5 WHEN I override a project default in a plan without giving a reason, `/spec:plan` SHALL ask me for the
  reason before recording the override.
- 1.6 WHEN the arbiter rules, THE SYSTEM SHALL give it both layers. A plan-layer override that states a reason
  SHALL take precedence over the project default for that spec. An override with no stated reason SHALL NOT.
- 1.7 WHEN a plan-layer principle reserves a change for me that the arbiter could otherwise make, THE SYSTEM
  SHALL send a ruling that needs that change to me as a needs-input entry instead of applying it.
- 1.8 WHEN a plan-layer principle claims to let the arbiter change requirements, THE SYSTEM SHALL ignore that
  grant, and the limits of Story 9 SHALL still apply.
- 1.9 WHEN, during a run, `specs/PRINCIPLES.md` is missing or unreadable at the point the arbiter would be called (the
  launch could not create or read it, or it was lost later), THE SYSTEM SHALL NOT call it, and SHALL send the claim to me
  as a needs-input entry that names the missing file. An audit problem follows 13.8 instead.
- 1.10 WHEN a spec has no PLAN.md, or its PLAN.md has no `## Principles` section, THE SYSTEM SHALL let the arbiter
  rule under the project layer alone, and the ruling record SHALL say the plan layer was absent.
- 1.11 WHEN `ck lint` runs on a spec whose PLAN.md exists but has a missing or empty `## Principles` section, THE
  SYSTEM SHALL report a warning naming the plan, and that warning alone SHALL NOT make lint fail.
- 1.12 WHEN `specs/PRINCIPLES.md` is missing or unreadable at the point the arbiter would be called during `/spec:create`
  or `/spec:review`, THE SYSTEM SHALL NOT call the arbiter, SHALL leave the affected findings or cuts as they stood, and
  SHALL name the missing file and the command that creates it (`ck principles init`) in review.md and the workflow's report.
  11.12's rule on Ready applies.

### Story 2: A justification for every requirement (D6, D7)

**As a** user **I want** planning to ask what breaks without each item, and the answer carried into the spec **So
that** parts that should not exist are removed before anyone builds them.

**Without it:** unneeded requirements get specified, tested and built, and nothing records why a story exists.

**Acceptance Criteria:**

- 2.1 WHEN `/spec:plan` records an In-scope item, THE SYSTEM SHALL ask me what concretely breaks, or who loses, if
  the item is deleted, and SHALL record the answer as a `*Without it:*` note on that item.
- 2.2 WHEN my answer shows nothing concrete breaks, THE SYSTEM SHALL offer to cut the item, and SHALL remove it
  only if I accept.
- 2.3 WHEN `/spec:create` writes requirements, THE SYSTEM SHALL put a `**Without it:**` line on every user story,
  carrying the reason from the plan item the story comes from.
- 2.4 WHEN a story is added during spec writing and no plan item covers it, THE SYSTEM SHALL give that story its
  own `**Without it:**` line stating what breaks or who loses.
- 2.5 WHEN a story has a `**Without it:**` line, THE SYSTEM SHALL treat its acceptance criteria as inheriting that
  reason and SHALL NOT require a line per criterion.

### Story 3: Lint refuses a story with no justification (D7)

**As a** user **I want** a missing justification to fail on sight **So that** no story escapes the question.

**Without it:** a story written during spec drafting, or a justification left blank, reaches implementation
unchallenged.

**Acceptance Criteria:**

- 3.1 WHEN `ck lint` runs on a spec with a user story that has no `**Without it:**` line, THE SYSTEM SHALL exit
  non-zero and name the story's number and title.
- 3.2 WHEN a story's `**Without it:**` line has no content beyond whitespace or markup, THE SYSTEM SHALL treat it
  as missing (3.1).
- 3.3 WHEN every story has a non-empty `**Without it:**` line, THE SYSTEM SHALL report no justification finding,
  whatever the line says.
- 3.4 WHEN a `**Without it:**` line sits outside a story's own section, or inside a fenced code block, THE SYSTEM
  SHALL NOT count it for any story.
- 3.5 WHEN a spec has no requirements document, or one with no user stories, THE SYSTEM SHALL report no
  justification finding and SHALL complete the rest of lint.
- 3.6 WHEN `ck lint` runs with JSON output, THE SYSTEM SHALL list each justification finding with story number and
  title, and SHALL keep every existing output key unchanged.
- 3.7 WHEN the only lint findings on a spec are justification findings, THE SYSTEM SHALL NOT refuse a
  `/spec:implement` launch for them.

### Story 4: Should this exist? (D7)

**As a** user **I want** each justification attacked and the weak ones listed as proposed cuts **So that** I remove
parts before they cost anything, and nothing is removed without me.

**Without it:** a justification only sits on the page, and "nice to have" reasons survive to implementation.

**Acceptance Criteria:**

- 4.1 WHEN `/spec:create` or `/spec:review` runs its adversarial review, THE SYSTEM SHALL include a "should this
  exist?" lens that attacks each story's justification and lists what to cut.
- 4.2 WHEN that lens proposes a cut that survives the check in 11.4, THE SYSTEM SHALL list it in review.md and in
  the workflow's report, with the story, the weakness in its justification, and what the cut would save (at
  least the counts of criteria, tasks and test cases it removes).
- 4.3 WHEN a cut is proposed, THE SYSTEM SHALL NOT apply it to any spec document without my approval, including
  during Revise.
- 4.4 WHEN I approve a cut and implementation has not started, THE SYSTEM SHALL remove the story from the
  requirements, design, test plan and task list consistently, before the first task starts, and `ck lint` SHALL
  report no new failure afterwards.
- 4.5 WHEN I approve a cut after implementation has started, THE SYSTEM SHALL NOT apply it automatically, and SHALL
  tell me the requirements are frozen and the change is mine to make.
- 4.6 WHEN the lens finds nothing to cut, THE SYSTEM SHALL say so in review.md.
- 4.7 WHEN the lens fails to return, THE SYSTEM SHALL count it as a lost lens, as it does for the other lenses.
- 4.8 WHEN a task reviewer finds code that serves no requirement, THE SYSTEM SHALL record a finding naming the code,
  list it in the run report as a proposed cut, leave the code in place, and SHALL NOT block the task's landing on
  that finding alone.

### Story 5: An arbiter on a model suited to the ruling (D9)

**As a** user **I want** the arbiter's model and effort set per machine and chosen by known signals **So that**
simple wiring rulings do not cost as much as real contradictions, and hard ones get a strong model.

**Without it:** every ruling costs the top rate, or a real contradiction gets a weak model and a confident wrong
answer.

**Acceptance Criteria:**

- 5.1 WHEN I set `arbiter.model`, `arbiter.effort`, `arbiter.risky_model` or `arbiter.risky_effort` through the
  existing agents settings, THE SYSTEM SHALL store and show them like any other role setting, and SHALL reject an
  invalid value with the same error as for other roles.
- 5.2 WHEN one of those four settings is unset, THE SYSTEM SHALL use the matching `reviewer.risky_model` or
  `reviewer.risky_effort` for it.
- 5.3 WHEN a claim is on a safety-tagged task, or is a guard stop, or is a repeat claim, THE SYSTEM SHALL start the
  ruling at the risky tier. WHEN none of these holds, THE SYSTEM SHALL start it at the normal tier.
- 5.4 WHEN a claim matches an earlier claim on the same task for which no ruling was recorded (the earlier arbiter
  call failed or returned nothing valid), THE SYSTEM SHALL treat it as a repeat claim for 5.3.
- 5.5 WHEN a task carries a High Complexity tag and none of the 5.3 signals, THE SYSTEM SHALL start the ruling at
  the normal tier.
- 5.6 WHEN a ruling started at the normal tier returns "needs a stronger look", THE SYSTEM SHALL re-run it once at
  the risky tier, with the first attempt's reasoning available to it.
- 5.7 WHEN a ruling at the risky tier returns "needs a stronger look", THE SYSTEM SHALL NOT call the arbiter again
  for that claim, and SHALL send the claim to me as a needs-input entry with what the arbiter did establish.

### Story 6: A run asks the arbiter at the points where tasks stop now (D8)

**As a** user **I want** spec claims to go straight to the arbiter and ordinary bugs to keep today's retries **So
that** runs stop paying for escalations that cannot fix a wrong test or a bad Verify line.

**Without it:** about 11 of 12 spec-level stops still reach me, a run each, and Tasks 12, 13, 19, 36, 44, 78 and
90's kind of failed escalation keeps being paid for.

**Acceptance Criteria:**

- 6.1 WHEN a task attempt reports a test conflict, THE SYSTEM SHALL send it to the arbiter with no further
  implementer attempt, no escalation and no second-agent confirmation.
- 6.2 WHEN a task attempt reports a guard stop, THE SYSTEM SHALL send it to the arbiter and SHALL NOT climb the guard
  ladder.
- 6.3 WHEN a review deadlock occurs, on a task reviewed alone or in a landing batch, THE SYSTEM SHALL send it to the
  arbiter and SHALL NOT escalate the revision up the model ladder.
- 6.4 WHEN a task's gate fails with no spec claim, THE SYSTEM SHALL apply today's retries and escalation unchanged
  and SHALL NOT call the arbiter until the top model has also failed.
- 6.5 WHEN a task has no escalation configured and its gate still fails after its retries, THE SYSTEM SHALL treat its
  last model as the top model for 6.4.
- 6.6 WHEN a retry or escalation makes the gate pass, THE SYSTEM SHALL NOT call the arbiter for that task's earlier
  failures.
- 6.7 WHEN a task ends because its worktree is busy or could not be created, THE SYSTEM SHALL NOT call the arbiter.
- 6.8 WHEN the arbiter is called, THE SYSTEM SHALL give it the stop type, the claim text, the failure evidence, the
  task, the models that already tried it, both principle layers and access to the spec documents.
- 6.9 WHILE a ruling is pending on a task, THE SYSTEM SHALL hold that task's dependents and SHALL keep running
  independent tasks.
- 6.10 WHEN a claim is reported and the arbiter cannot be called or returns nothing valid, THE SYSTEM SHALL send the claim
  to me as a needs-input entry and SHALL NOT fall back to the guard ladder or a confirmation agent. A finding that blocks a
  launch stays blocked under 12.7, and an audit problem follows 13.8.
- 6.11 WHEN the top model has failed the gate with no spec claim, THE SYSTEM SHALL send the failure to the arbiter as a
  claim.
- 6.12 WHEN a claim arises in a single-task `/spec:implement` run or a run without the Workflow tool, THE SYSTEM SHALL send
  it to me as a needs-input entry and SHALL NOT climb the guard ladder or run a confirmation agent.

### Story 7: Rulings are checkable and cannot loop (D9, D10, plan principles)

**As a** user **I want** every ruling to name the principle it used and each claim to get one ruling **So that** a
wrong arbiter cannot act on opinion or burn budget unseen.

**Without it:** a ruling is unreviewable, and a task could loop through the arbiter with no one noticing.

**Acceptance Criteria:**

- 7.1 WHEN the arbiter rules, THE SYSTEM SHALL require exactly one outcome (spec edit, test repair, code, or
  requirements), or the status "needs a stronger look", together with the claim it answers, its reasoning, and the
  principle cited.
- 7.2 WHEN a ruling cites no principle, cites one found in neither layer, names an unknown outcome or more than one
  outcome, or is malformed, THE SYSTEM SHALL NOT apply it, SHALL NOT record it as a ruling, and SHALL send the
  claim to me as a needs-input entry with the arbiter's output.
- 7.3 WHEN a claim is identical to one already raised on the task, by the identity the design defines, THE SYSTEM
  SHALL treat them as the same claim, across attempts and across runs of the spec.
- 7.4 WHEN a claim returns after its ruling was applied, THE SYSTEM SHALL NOT rule again, and SHALL send it to me as a
  needs-input entry with the ruling and the new evidence.
- 7.5 WHEN a different claim arises on a task with fewer than 3 rulings and fewer than 6 arbiter calls, THE SYSTEM SHALL give it its own ruling.
- 7.6 WHEN a claim would be the 4th ruling on a task, THE SYSTEM SHALL NOT call the arbiter, and SHALL send the claim
  to me as a needs-input entry listing the 3 earlier rulings.
- 7.7 WHEN a "needs a stronger look" re-run settles a claim, THE SYSTEM SHALL count the two calls as one ruling.
- 7.8 WHEN a run resumes, THE SYSTEM SHALL count the rulings recorded by earlier runs toward the limits in 7.4-7.6.
- 7.9 WHEN a task has reached 6 arbiter calls, THE SYSTEM SHALL NOT call the arbiter, and SHALL send the claim to me as a
  needs-input entry.

### Story 8: Rulings are carried out by the right agent (D1, D2, D11, D13, D18)

**As a** user **I want** each ruling applied through the lane that fits it **So that** judgment stays separate from
editing and collisions are prevented by what already prevents them.

**Without it:** rulings remain advice, and a task still stops for a change someone must make by hand.

**Acceptance Criteria:**

- 8.1 WHEN a ruling's outcome is a spec edit, THE SYSTEM SHALL have the arbiter commit the edit on its own branch
  created from the integration branch and SHALL land that branch through the normal merge queue.
- 8.2 WHEN a spec edit has landed, THE SYSTEM SHALL resume the task against the changed spec, and the ruling SHALL NOT
  use up the task's retry or escalation attempts.
- 8.3 WHEN a landed spec edit changes task wiring, THE SYSTEM SHALL apply the change to the schedule of tasks not yet
  started.
- 8.4 WHEN a spec edit cannot land (conflict, red gate, or a refused path), THE SYSTEM SHALL NOT rule again, SHALL leave
  the integration branch unchanged, and SHALL send the claim to me as a needs-input entry with the ruling and the
  landing result.
- 8.5 WHEN a spec edit would leave the spec with a dependency cycle or a new blocking lint finding, THE SYSTEM SHALL NOT
  land it and SHALL treat it as in 8.4.
- 8.6 WHEN a spec edit changes a task's Status field or an acceptance mark, THE SYSTEM SHALL NOT land it and SHALL
  treat it as in 8.4.
- 8.7 WHEN a spec edit moves a test out of a task's Verify selection, THE SYSTEM SHALL name that test in the ruling's
  record under what changed.
- 8.8 WHEN a ruling's outcome is a test repair and the test-fixer is not available, THE SYSTEM SHALL create a needs-input
  entry carrying the exact test change and why, for me to apply or approve, and no agent SHALL edit the test.
- 8.9 WHEN a ruling's outcome is a test repair, THE SYSTEM SHALL send it through one hand-off point, so that the
  test-fixer's arrival changes only that point and the ruling reaches the fixer attached and unchanged.
- 8.10 WHEN a test-repair ruling is issued for a test conflict, THE SYSTEM SHALL treat the ruling as the confirmation the
  test-fixer starts on, with no second-agent confirmation.
- 8.11 WHEN a ruling's outcome is code, THE SYSTEM SHALL return the task to the implementer with the ruling's text in
  its prompt, and the task SHALL still need a passing gate to finish.
- 8.12 WHEN a ruling settles a review deadlock, THE SYSTEM SHALL give the ruling to the reviewer that re-reviews the
  task, and a finding the ruling settled SHALL NOT block the task again.
- 8.13 WHEN a ruling's outcome is requirements, THE SYSTEM SHALL create a needs-input entry with the arbiter's
  recommendation and SHALL leave the requirements unchanged.
- 8.14 WHEN the arbiter finds a requirement that seems to be missing, THE SYSTEM SHALL create a needs-input entry with
  the arbiter's proposed wording and reason. The task it arose on and that task's dependents SHALL wait, and
  independent tasks SHALL continue.
- 8.15 WHEN a ruling produces a needs-input entry, THE SYSTEM SHALL route it as needs-input entries are routed today:
  listed apart from failures, not triggering a halt on failure, and notifying me where notification is configured.

### Story 9: The arbiter's lane in the guard (D2, D11)

**As a** user **I want** the guard to limit the arbiter to three spec files **So that** a wrong ruling cannot reach my
requirements, the tests or the code.

**Without it:** the arbiter cannot edit the test plan because the guard denies task agents, or widening the guard lets
an agent change what it should not.

**Acceptance Criteria:**

- 9.1 WHEN the arbiter edits the task list, design or test plan of the spec its ruling is for, THE SYSTEM SHALL allow
  the edit, including changes to existing text after implementation has started.
- 9.2 WHEN the arbiter, during a run, tries to change the requirements document in any way, including an add-only
  change, THE SYSTEM SHALL deny it, whatever the task statuses are.
- 9.3 WHEN the arbiter tries to change a test, source code, a plan, a principles file, project configuration, any other
  spec's documents, or any other path, THE SYSTEM SHALL deny it.
- 9.4 WHEN the arbiter tries a denied change through a shell command (delete, move, redirect, or another tool), THE
  SYSTEM SHALL deny it as it would an edit.
- 9.5 WHEN a caller that is not the arbiter claims the arbiter's lane through an environment variable, argument or
  prompt text, THE SYSTEM SHALL classify it as it would without the claim.
- 9.6 WHEN a caller's input does not establish the arbiter (for example no agent id or a malformed type), THE SYSTEM SHALL
  NOT give it the arbiter's lane and SHALL classify it as it would without the claim.
- 9.7 WHEN any other caller acts, THE SYSTEM SHALL apply the guard rules unchanged: a task agent stays denied changes to
  existing text in the requirements and test plan once implementation has started.
- 9.8 WHEN the guard denies the arbiter, THE SYSTEM SHALL name the file and state it is outside the arbiter's lane.

### Story 10: Every ruling on the record (D12)

**As a** user **I want** one append-only record of rulings per spec **So that** I can review each ruling in one place and
the spec shows why it changed.

**Without it:** a wrong ruling acts with authority and leaves no trace, and the spec changes with no recorded reason.

**Acceptance Criteria:**

- 10.1 WHEN the arbiter rules, THE SYSTEM SHALL append one numbered entry to `specs/<spec>/decisions.md` with the trigger,
  the claim, the ruling, the principle cited, what changed, and the commit.
- 10.2 WHEN a ruling changes no file, or was reached during `/spec:create` without a commit, THE SYSTEM SHALL record that
  the commit is none.
- 10.3 WHEN a ruling's edit could not land, THE SYSTEM SHALL record that it did not land and why.
- 10.4 WHEN an entry is appended, THE SYSTEM SHALL leave all earlier entries byte-identical, and entry numbers SHALL
  increase and never repeat, across runs.
- 10.5 WHEN several tasks receive rulings at the same time, THE SYSTEM SHALL record each exactly once, with no lost entry
  and no duplicated number.
- 10.6 WHEN the first ruling on a spec is recorded, THE SYSTEM SHALL create the record. A spec with no rulings SHALL have
  no record file.
- 10.7 WHEN a run ends, THE SYSTEM SHALL list that run's rulings, and no earlier run's, in the run report.
- 10.8 WHEN a run ends, THE SYSTEM SHALL carry that run's rulings in the workflow result, and `ck stats` SHALL report
  them by trigger, outcome and tier.
- 10.9 WHEN an entry cannot be written, THE SYSTEM SHALL report that in the run report and SHALL send the claim to me as a
  needs-input entry rather than continue as if it were recorded.

### Story 11: The arbiter in `/spec:create` (D14, D15)

**As a** user **I want** the arbiter to settle review disagreements and plan drift while the spec is still a draft **So
that** mismatches do not surface later, when they cost a run.

**Without it:** mismatches surface during implementation, and sound findings are dropped by a single refuter.

**Acceptance Criteria:**

- 11.1 WHEN a reviewer raises a finding and the refuter rejects it, THE SYSTEM SHALL have the arbiter rule on it instead of
  dropping it or taking a majority.
- 11.2 WHEN the arbiter rules that a refuted finding stands, THE SYSTEM SHALL handle it as a confirmed finding. WHEN it rules
  that the refutation stands, THE SYSTEM SHALL keep the finding listed as refuted, with the ruling.
- 11.3 WHEN Revise finishes and a PLAN.md exists, THE SYSTEM SHALL have the arbiter compare the spec with PLAN.md's decisions
  and principles and rule on any drift, whether or not Revise applied fixes.
- 11.4 WHEN the "should this exist?" lens proposes cuts, THE SYSTEM SHALL have the arbiter check each one, and SHALL show me
  only the cuts it finds soundly reasoned. A cut it rejects SHALL stay in the record of rulings.
- 11.5 WHILE every task is Not Started, THE SYSTEM SHALL let the arbiter and the reviser correct the wording of draft
  requirements to match PLAN.md.
- 11.6 WHEN a change would add, remove or change anything PLAN.md decided, THE SYSTEM SHALL NOT apply it, and SHALL report it
  to me in review.md and the workflow's report.
- 11.7 WHEN implementation has started, THE SYSTEM SHALL NOT let the arbiter change requirements, and the existing rule for
  frozen spec text SHALL hold for the reviser.
- 11.8 WHEN the arbiter changes a spec document during `/spec:create`, THE SYSTEM SHALL run the consistency check over the
  result, even if Revise applied no fixes.
- 11.9 WHEN the arbiter rules during `/spec:create`, THE SYSTEM SHALL apply Story 7's validity rules, start at the normal tier,
  allow one re-run at the risky tier on "needs a stronger look", give one ruling per finding, and record it per Story 10.
- 11.10 WHEN the arbiter would change PLAN.md, THE SYSTEM SHALL NOT allow it.
- 11.11 WHEN the review has no refuted findings and no proposed cuts, THE SYSTEM SHALL NOT call the arbiter for those two
  steps.
- 11.12 WHEN the arbiter fails or returns nothing valid during `/spec:create`, THE SYSTEM SHALL leave the affected findings or
  cuts as they stood, SHALL say the check did not run, and SHALL NOT report the spec as Ready.

### Story 12: Lint catches Verify-scope problems before launch (D16)

**As a** user **I want** lint to simulate each task's gate **So that** a run does not spend attempts on gates known to be
bad.

**Without it:** known-bad gates spend attempts, as Task 2, Tasks 3 and 4 at Task 19, and Tasks 44 and 19's CLI calls did.

**Acceptance Criteria:**

- 12.1 WHEN `ck lint` runs, THE SYSTEM SHALL simulate the gate of each task that is not Done: which suites are due at that
  point, and what each one selects.
- 12.2 WHEN a task's due suite is a shared suite with no filter, so that it selects tests for tasks that are not yet Done at
  that point, THE SYSTEM SHALL report a finding.
- 12.3 WHEN a test selected by a task's gate calls a CLI command or flag that only a later task provides, THE SYSTEM SHALL
  report a finding.
- 12.4 WHEN a suite is not due at that task's gate, or its filter excludes the tests in question, or the task depends directly
  or transitively on the task that provides the command or flag, THE SYSTEM SHALL report no finding for it.
- 12.5 WHEN a finding of this story exists, THE SYSTEM SHALL make `ck lint` exit non-zero, and SHALL block a `/spec:implement`
  launch the same way a verify-order cycle does.
- 12.6 WHEN a launch is blocked by such findings, THE SYSTEM SHALL send them to the arbiter as one claim per distinct problem,
  listing every test it covers, and the arbiter SHALL propose a fix or, within its wiring authority, apply one under Stories
  7, 8 and 10. After each landed fix THE SYSTEM SHALL re-run lint and drop findings that have gone before making the next
  ruling.
- 12.7 WHEN the arbiter has applied a fix to such a finding, THE SYSTEM SHALL re-run lint before launching, and SHALL keep the
  launch blocked and show me the remaining findings with the arbiter's proposed fix if any finding remains.
- 12.8 WHEN `ck lint` is run directly, THE SYSTEM SHALL report findings without calling the arbiter, each naming the task, the
  suite or test, and where it applies the task providing the command or flag, in text and JSON output.
- 12.9 WHEN a Verify command's selection cannot be resolved statically, including a selected test file that is not on disk yet,
  THE SYSTEM SHALL report that task as not checked and SHALL NOT block on it.
- 12.10 WHEN a problem is already reported by an existing lint rule, THE SYSTEM SHALL NOT report it a second time.
- 12.11 WHEN a finding of this story first appears after launch for a task not yet started, THE SYSTEM SHALL send it to the
  arbiter as a claim under Stories 7, 8 and 10 and SHALL hold that task until the claim is settled.
- 12.12 WHEN a finding of this story names the task of a single-task `/spec:implement` run, THE SYSTEM SHALL refuse that run with
  the finding.

### Story 13: A reading audit before an impl task starts (D16)

**As a** user **I want** an agent to read a task's selected tests against the spec before the task runs **So that** problems a
static check cannot prove are caught before an attempt is spent.

**Without it:** a test that needs a later task's behaviour, as in Tasks 71 and 12, spends attempts and then stops the task.

**Acceptance Criteria:**

- 13.1 WHEN every test task that verifies an impl task has merged, and before that impl task's first attempt, THE SYSTEM SHALL
  run a read-only audit that reads the tests the task's gate selects against the spec.
- 13.2 WHEN the audit finds no problem, THE SYSTEM SHALL start the task without calling the arbiter.
- 13.3 WHEN the audit reports a problem, THE SYSTEM SHALL send it to the arbiter as a claim on that task before any attempt is
  spent, under the rules of Stories 7, 8 and 10.
- 13.4 WHEN the audit runs, THE SYSTEM SHALL ensure it changes no file.
- 13.5 WHEN the audit fails or returns nothing valid, THE SYSTEM SHALL start the task anyway and SHALL note the missed audit in
  the run report.
- 13.6 WHEN a task is retried or escalated, THE SYSTEM SHALL NOT run the audit again for that task in that run.
- 13.7 WHEN an impl task has no verifying tests, or a task is on the test track, THE SYSTEM SHALL NOT run the audit for it.
- 13.8 WHEN the arbiter cannot be called or returns nothing usable for an audit problem, THE SYSTEM SHALL start the task and note the
  missed audit, as in 13.5. A malformed ruling on an audit problem follows 7.2.

### Story 14: An integration checkout survives a refused raw merge (D23)

**As a** user **I want** ck to recover an integration checkout that a refused raw merge left dirty **So that** one agent's
refused bypass doesn't stop every later landing.

**Without it:** an agent's refused raw `git merge` into the integration checkout (the reference-transaction hook refuses the ref
update) still leaves staged files, or a merge in progress (MERGE_HEAD, AUTO_MERGE, MERGE_MSG). Every later `ck worktree land`
then refuses with "has uncommitted changes to tracked files", and the run ends blocked waiting for me. unattended-correctness
PLAN D6 records an agent doing such a merge in the equity E2E.

**Acceptance Criteria:**

- 14.1 WHEN a refused ref update leaves a managed integration checkout with a merge in progress or uncommitted changes to
  tracked files, THE SYSTEM SHALL restore that checkout to its branch head before the next landing or merge into it.
- 14.2 WHEN it restores a checkout, THE SYSTEM SHALL report the discarded paths, and whether a merge was in progress, in the
  landing result.
- 14.3 THE SYSTEM SHALL NOT restore any other checkout (a dirty main checkout is still refused, as today) and SHALL NOT touch
  untracked files.
- 14.4 THE SYSTEM SHALL restore only to the ref's current head, never moving the ref, so the recovery can't become a bypass of
  the protected-file hooks.
- 14.5 WHEN a managed integration checkout has a merge in progress or uncommitted changes to tracked files and no recorded
  refusal names its branch, its current head and that checkout, THE SYSTEM SHALL refuse the landing as today and leave the
  checkout unchanged, so a merge I am making there by hand is never discarded.

### Story 15: Bisection blames the task that broke the gate (D24)

**As a** user **I want** a red landing batch blamed only on a task whose gate fails with it **So that** the fixer gets the real
culprit and a correct task isn't held back.

**Without it:** when a landing batch completes the spec, ck gates the whole batch with the full suite (`spec_complete` turns off
the green-only, task-scoped mode). The bisection halves are smaller, so they run task-scoped gates that can't see the failure.
`land_group` then treats "a green left half on the same base proves the right half red" and blames the last task by
elimination. That task goes to a fixer with nothing to fix, and the run ends blocked. It was observed in the
unattended-correctness TC-176 smoke scenario: an earlier task's regression in `tests/test_other.py` was blamed on Task 7.

**Acceptance Criteria:**

- 15.1 WHEN a landing batch is bisected, THE SYSTEM SHALL run a gate on each half it merges, task-scoped as for a task in a
  spec that is not yet complete, and SHALL NOT infer a half's result from the other half's gate.
- 15.2 WHEN a single task is reported RED, THE SYSTEM SHALL have run a gate with that task on the base that fails, and SHALL
  report that gate's failing checks. The failing gate SHALL be scoped to that task, never the full suite that only spec
  completion turns on.
- 15.3 WHEN a landing batch's gate is red and no task ends RED, THE SYSTEM SHALL land every task whose own gates passed, SHALL
  report the batch unreproduced with the batch gate's failing checks, and SHALL run the attributed full gate and its fixer on
  the target, as after a red periodic gate.

## Specification Heritage

**Evolves From:** none directly. It builds next to specs/unattended-correctness and on the needs-input path of
specs/prompt-free-runs.

**Preserves:** the needs-input and park path; the merge queue; today's retries and escalation for plain gate failures; the guard
rules for every caller other than the arbiter; the frozen-spec rule for the reviser once implementation has started; the
test-fixer's proof step and review (unattended-correctness Story 4).

**Supersedes:** the second-agent confirmation of a test conflict; the guard ladder for guard stops; the model-ladder escalation of
a review deadlock; bisection's inference that a green left half proves the right half red; the refusal to land into an
integration checkout that only a refused ref update made dirty. unattended-correctness 4.1 starts the test-fixer on a conflict "confirmed by a second agent"; here the arbiter's
ruling takes that role (D8, 8.10).

**Evolution Reason:** a stronger implementer cannot fix a wrong test or a bad Verify line, and most spec-level stops had a clear
answer no agent was allowed to act on.

## Non-Functional Requirements

- **Bounded arbiter cost.** A task makes at most 6 arbiter calls across all runs of the spec (3 rulings, each with at most one
  re-run). A claim makes at most 2. Checked by a workflow test that drives repeated claims.
- **Runtime compatibility.** New workflow logic SHALL use no wall clock or randomness (`Date.now`, `new Date`, `Math.random`).
  Checked by running the workflows in the fake runtime, where those throw.
- **Lint performance and safety.** The checks of Stories 3 and 12 SHALL import no project code and use no network, and `ck lint`
  SHALL finish within 10 seconds on a 108-task spec on the development machine.
- **Compatibility.** A spec with no rulings and no findings under Stories 3 and 12 SHALL launch and run as it does today, apart from
  the stops Story 6 reroutes and the audit steps Story 13 adds.
- **Determinism.** Lint findings SHALL come out in a stable order, so two runs on the same tree produce identical output.
- **Bounded landing cost.** A bisected batch of n tasks SHALL run at most 2n-1 gates. Checked by a landing test over batches
  with one culprit, several, and none.

## Constraints

- Built on `integrate/run-principles`, branched from `integrate/unattended-correctness` and merged from it periodically. It lands
  only after unattended-correctness does (D17).
- The spec graph (`ck graph`, invokes and provides edges) and the structured failures from `ck gate --json` (`failures[]` with
  `missingCommand`) exist only on `integrate/unattended-correctness`. Any reference to them depends on that branch.
- The test-fixer (unattended-correctness Tasks 57 and 86) is not built yet. D18's interim route applies until it is.
- unattended-correctness's later tasks still edit the implement workflow, so conflicts must be merged in small steps.
- The workflow runtime has no wall clock.
- Vendored copies under `.claude/` are protected files that an agent cannot land. Any change to them is landed by hand, or through
  the vendoring command.
- Hook messages and slash-command instructions use we/you/me framing.
- Requirements stay mine: the arbiter may suggest, never decide.

## Out of Scope

- The arbiter changing requirements on its own. It may find missing requirements and suggest them.
- The arbiter editing tests, or building the test-fixer. The fixer belongs to unattended-correctness Tasks 57 and 86.
- The arbiter in a single-task `/spec:implement` run or a run without the Workflow tool (6.12, 12.12).
- Applying a proposed cut with an undo, or without my approval.
- A per-spec setting for how far the arbiter may go.
- Routing rulings by the Complexity tag.
- A run-level dollar budget in place of the per-task limit.
- Keeping the second-agent confirmation of a test conflict.
- Principles built into `ck`, in place of the project file.
- A justification per acceptance criterion.
- Retroactively adding justifications to specs written before this feature.
- Restoring a checkout left mid-rebase, mid-cherry-pick or mid-revert, or a task worktree.
- Telling a refused merge's residue apart from edits made in the same checkout after the refusal, at the same head.
- Naming more than one culprit for a failure that appears only when several tasks land together. The first task whose own
  gate fails with it is the one blamed.

## Assumptions

- Names PLAN.md decides are part of the behaviour and appear here: `specs/PRINCIPLES.md`, `specs/<spec>/decisions.md`, `## Principles`,
  `**Without it:**`, the `arbiter.*` settings, and the spec document names.
- The record of rulings is written by `ck` or the workflow, not by the arbiter, because D11 limits the arbiter's lane to three files.
- D9's "repeat claim" means 5.4's case, because under D10 a claim that returns after an applied ruling never gets a second ruling.
- D11 denies the arbiter the requirements document during runs. D15's wording corrections apply only in `/spec:create`, while every task
  is Not Started.
- D7 names only `ck lint` for justifications and D16 names launch-blocking only for Verify scope, so a justification finding fails lint
  without blocking a launch, and specs written earlier can still run.
- A review deadlock ruling that finds the reviewer wrong uses one of D11's four outcomes (a design edit or a code ruling), with 8.12
  keeping the settled finding from blocking again.
- The limits in D10 count across runs of a spec, so a resume does not reset them.
- A reviewer's "serves no requirement" finding does not block landing, because D7 makes cuts proposals.
- The audit agent's model and effort follow an existing role, chosen in design. D9 adds settings only for the arbiter.
- A failed audit lets the task start, because the audit only saves attempts and the task's own gate still guards it.
- Stories 14 and 15 came from the unattended-correctness run as drafts I asked for; 14.5 is this spec's reading of "a refused
  ref update" in 14.1.
- "The landing result" in 14.2 is `ck worktree land --json` (and the output of `ck worktree merge`), not the workflow result,
  which carries it along.
