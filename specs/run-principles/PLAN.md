# Plan: Principles and Arbiter for Spec Runs

**Status:** Decided
**Updated:** 2026-11-10
**Evolves:** none directly; builds next to specs/unattended-correctness (its D4 test-fixer)

## Problem

Long `/spec:implement` runs stop for mismatches between the plan, the spec and the implementation, and each stop
costs a full run plus a round trip through the user. On `unattended-correctness`, about a dozen spec-level stops
occurred across seven runs (about $170), and only one needed real judgment (Task 20). The rest had a clear right
answer that no agent was allowed to act on.

A second cost shows up earlier: requirements nobody justified get specified, tested and implemented. "Good
engineers optimize something that should not exist; great engineers remove parts."

For: the user running long spec runs. It worked if runs stop only for decisions that are really the user's, and
if every requirement can say why it exists.

## Scope

**In:**
- Principles recorded at planning: priority order, tie-breaking values, and what may change without asking. *Without it:* the arbiter has nothing to uphold, and rulings are opinion.
- A justification for each requirement at planning, so parts that should not exist get removed. *Without it:* unneeded requirements get specified, tested and built.
- An arbiter role on a high-capability model in `/spec:implement`, at the points where tasks stop now. *Without it:* about 11 of 12 spec-level stops still reach the user, a run each.
- The same arbiter in `/spec:create`, when its adversarial review disagrees with the plan. *Without it:* mismatches surface later, when they cost a run.
- A `ck lint` check and a reading audit that catch Verify-scope problems before a run spends attempts. *Without it:* known-bad gates spend attempts.
- An integration checkout that a refused raw merge left dirty is restored before the next landing (D23). *Without it:* one agent's refused bypass stops every later landing and the run ends blocked.
- Bisection blames a task only when a gate with that task fails (D24). *Without it:* a task with nothing to fix goes to the fixer and the run ends blocked.

**Out:**
- The arbiter changing requirements on its own. It may find missing requirements and suggest them.

## Principles

- Requirements stay the user's: the arbiter may suggest, never decide.
- A ruling must cite the principle it used.
- A test keeps its intent; selection and setup may move.
- Remove before you optimize.

## Constraints

- Built now, in parallel with `unattended-correctness` (80 of 108 tasks merged), whose later tasks still edit
  `plugin/workflows/spec-implement.js`.
- The test-fixer the arbiter hands test repairs to (unattended-correctness Tasks 57, 86) is not built yet.
- The workflow runtime has no wall clock (`Date.now`, `new Date`, `Math.random` throw).
- Vendored copies under `.claude/` are protected files that an agent cannot land.

## Decisions

### D1: How does the arbiter relate to the unattended-correctness test-fixer?
**Choice:** The arbiter decides; the test-fixer executes test repairs, with its existing proof step and review.
**Why:** Keeps judgment separate from editing and reuses planned work.
**Rejected:** Arbiter replaces the fixer - loses the separation and reworks Tasks 57 and 86; separate with fixer first - wiring stops have no owner until the fixer gives up.
**Decided by:** user

### D2: How far may the arbiter go without asking?
**Choice:** It may change task-list wiring, design, the test plan and code, logging each change. Requirements are out of bounds: it may find missing requirements and suggest them to the user.
**Why:** Requirements are the record of what the user asked for.
**Rejected:** Everything including requirements - a run could quietly redefine the ask; wiring only - about a third of the stops would still reach the user; per-spec setting - one more decision every plan.
**Decided by:** user

### D3: What does this spec cover?
**Choice:** Principles in the plan, the arbiter in runs, the prevention lint, and the arbiter at spec-create. Added by the user: a justification for every requirement at planning.
**Decided by:** user

### D4: When is it built?
**Choice:** Now, in parallel with unattended-correctness.
**Decided by:** user

### D5: Where do principles live?
**Choice:** Two layers. A project-wide `specs/PRINCIPLES.md` holds defaults (priority order: requirements > design > test plan > task wiring; a test keeps its intent, only selection and setup may move; fail closed on security; remove before you optimize). Each PLAN.md has a required `## Principles` section that adds tie-breakers or overrides a default with a reason. The arbiter reads both.
**Why:** Some principles are universal and shouldn't drift between specs; some are feature-specific, which is what Task 20's contradiction needed.
**Rejected:** Each plan only - universal rules repeat and drift; project file only - can't express a feature-specific tie-breaker; built into ck - defaults can't change without changing ck.
**Decided by:** user

### D6: What form does a requirement's justification take?
**Choice:** A `**Without it:**` line on every user story: what concretely breaks, or who loses, if the story is deleted. Decided at planning and carried into requirements.md. Criteria inherit their story's reason.
**Why:** Falsifiable: "nice to have" fails on sight. "Good engineers optimize something that should not exist; great engineers remove parts."
**Rejected:** Per criterion - heavy, and mostly repeats the story's reason; plan-level only - stories added during spec writing escape the question.
**Decided by:** user

### D7: What acts on the justifications?
**Choice:** All four: `ck lint` fails a story with a missing or empty justification; a "should this exist?" lens in /spec:create's adversarial review attacks each justification and lists what to cut; /spec:plan asks what breaks without each In-scope item and offers to cut it; task reviewers flag code that serves no requirement. A proposed cut is listed for the user in review.md and the report, with what it would save; nothing is cut without approval, and approved cuts apply before implementation starts.
**Why:** A justification only pays off if something acts on it, and D2 keeps requirements under the user's control.
**Rejected:** Applying cuts with an undo - conflicts with D2.
**Decided by:** user

### D8: When does a run call the arbiter?
**Choice:** A spec claim (test conflict, guard stop, review deadlock) goes straight to the arbiter instead of climbing the ladder. A plain failing gate keeps today's retries and escalation, and reaches the arbiter only if the top model still fails. The arbiter's ruling replaces today's second-agent confirmation of a test conflict.
**Why:** On unattended-correctness, Tasks 12, 13, 19, 36, 44, 78 and 90 escalated to Opus and failed anyway: a stronger implementer can't fix a wrong test or a bad Verify line. The arbiter is the stronger check, so a second implementer pass adds nothing.
**Rejected:** Only after the ladder - keeps paying for escalations that failed every time; at the first failure of any kind - most failures are ordinary bugs; keep the confirmation step - one more agent per conflict.
**Decided by:** user
**Note:** unattended-correctness 4.1 starts the test-fixer on a conflict "confirmed by a second agent"; here the arbiter's ruling takes that role.

### D9: How are the arbiter's model and effort chosen?
**Choice:** Tiered, in the reviewer's shape: `arbiter.model`/`effort` and `arbiter.risky_model`/`risky_effort`, set per machine in the global config (Fable high then max here; Opus at work). A ruling starts at the risky tier for a safety-tagged task, a guard stop or a repeat claim; otherwise the normal tier. A ruling may return "needs a stronger look", which re-runs it once at the risky tier. Unset values follow `reviewer.risky_model`/`risky_effort`.
**Why:** Before the agent starts we know the stop type, Risk and Complexity tags, the claim text and which models failed, but not how hard the ruling is: Tasks 19 and 20 looked identical from outside. Known signals set the starting tier; the arbiter reports when it's out of its depth.
**Rejected:** One setting - simple wiring rulings cost as much as real contradictions; routing by Complexity tag - both Task 19 and Task 20 are High; defaulting to Fable max - new installs may not have it.
**Decided by:** user

### D10: What stops the arbiter looping on a task?
**Choice:** One ruling per distinct claim. If the same claim returns after the ruling was applied, it goes to the user with the ruling and the new evidence. A different claim on the same task gets its own ruling, up to 3 per task.
**Why:** Task 19 had two separate problems in a row, so a per-task limit of one would stop too early; a dollar cap alone lets a loop burn budget unseen.
**Rejected:** One ruling per task per run; no per-task limit with a run budget only.
**Decided by:** user

### D11: How are rulings applied?
**Choice:** A ruling has one of four outcomes. Spec wiring, design or test-plan edits: the arbiter is a new `ck-arbiter` caller that the guard allows on tasks.md, design.md and test-plan.md (denied on requirements.md, tests and code); it commits on its own branch from the integration branch, landed through the normal merge queue. Test repairs: to the test-fixer with the ruling attached (D1). Code: back to the implementer with the ruling in its prompt. Requirements: a needs-input entry with the arbiter's recommendation.
**Why:** The guard denies task agents test-plan edits, so the arbiter needs its own lane; the merge queue already prevents collisions with landings in flight.
**Rejected:** Arbiter only rules and ck applies - design and test-plan edits are prose and would need another agent anyway; direct commits on the integration branch - needs its own locking.
**Decided by:** user

### D12: Where are rulings recorded?
**Choice:** An append-only `specs/<spec>/decisions.md`: one numbered entry per ruling with the trigger, claim, ruling, principle cited, what changed and the commit. The run report lists that run's rulings, and the workflow result carries them for `ck stats`.
**Why:** The user can review every ruling in one place, and the spec itself shows why it changed.
**Rejected:** Run report only - the spec wouldn't show why it changed; extending PLAN.md - mixes the user's choices with the arbiter's.
**Decided by:** user

### D13: What happens when the arbiter finds a requirement that seems to be missing?
**Choice:** Always a needs-input question to the user, with the arbiter's wording and reason. The task it arose on waits, along with its dependents; independent tasks continue.
**Why:** Requirements are the user's; a run shouldn't decide on its own whether a gap matters.
**Rejected:** Noting it and stopping only if it blocks; report only, never blocks.
**Decided by:** user

### D14: Where does the arbiter plug into /spec:create?
**Choice:** Three places: it rules on findings the reviewer raised and the refuter rejected (instead of a majority or a drop); after revise it compares the spec with PLAN.md's decisions and principles and rules on drift; and it checks the "should this exist?" cut list so the user only sees cuts with a sound reason.
**Decided by:** user

### D15: What does "the arbiter doesn't change requirements" mean during /spec:create?
**Choice:** The plan is frozen; drafts aren't. While requirements are a draft, the arbiter and reviser may correct wording to match PLAN.md. Anything that adds, removes or changes what PLAN.md decided goes to the user in the report. The freeze on requirements starts when implementation starts.
**Why:** The reviser already edits drafts; the plan is the record of what the user decided.
**Rejected:** Never, even in drafts - the user would review wording fixes too.
**Decided by:** user

### D16: How are Verify-scope problems caught before a run spends attempts on them?
**Choice:** Two layers. `ck lint` simulates each task's gate (which suites are due, what they select) and flags unfiltered shared suites and calls to CLI commands or flags a later task provides. Once test tasks have merged and before an impl task starts, a read-only audit agent reads that task's selected tests against the spec and sends problems to the arbiter before any attempt is spent. A static finding blocks launch like a verify-order cycle; the arbiter proposes or, under its wiring authority, applies the fix.
**Why:** Static checks can prove some cases (Task 2, Tasks 3/4 at Task 19, Tasks 44 and 19's CLI calls) but not a test that needs a later task's behaviour (Tasks 71, 12).
**Rejected:** Static only - misses those cases; reading audit only - spends a model on what a static check can prove; warning only - spends attempts on known problems.
**Decided by:** user

### D17: Which branch is it built on?
**Choice:** `integrate/run-principles`, branched from `integrate/unattended-correctness`, which is merged in periodically.
**Why:** It needs the spec graph, structured failures and the #15/#16 fixes, and conflicts in `spec-implement.js` surface early and small. It lands only after unattended-correctness does.
**Rejected:** Off fix/open-issues - misses what the lint and arbiter need, and faces one large merge later.
**Decided by:** user

### D18: What does a test-repair ruling do until the test-fixer exists?
**Choice:** It becomes a needs-input entry carrying the exact test change and why, for the user to apply or approve. A single hand-off function sends the same ruling to the fixer once Tasks 57 and 86 land.
**Why:** Keeps D1 (the arbiter doesn't edit tests) and handles the most common stop type from day one.
**Rejected:** Arbiter repairs tests itself - breaks D1 and the guard; waiting for the fixer - the most common stop stays unhandled.
**Decided by:** user

### D19: What caps a task's arbiter calls that end with no ruling?
**Choice:** 7.5 gains a limit of 6 arbiter calls per task; a task at 6 calls sends further claims to the user as needs-input entries (7.9, reason `task-call-limit`). Raised by the spec review (V5, R4).
**Why:** The bounded-cost requirement already promised 6 calls; failed calls shouldn't be free.
**Rejected:** Only recorded rulings count - failed calls would be unbounded per task.
**Decided by:** user

### D20: What happens when the arbiter returns a malformed ruling on an audit problem?
**Choice:** It's a needs-input entry with the arbiter's output (7.2), and the task does not start. 13.8 covers only a call that failed or returned nothing usable. Raised by the consistency check (C1.1).
**Why:** A malformed ruling is evidence something went wrong, so it shouldn't silently pass.
**Rejected:** Starting the task and noting a missed audit.
**Decided by:** user

### D21: Is a review deadlock claim identified per finding or by the whole set?
**Choice:** Per finding. A finding already covered by a ruling goes to the user with that ruling; only findings never ruled on reach the arbiter. Raised by the consistency check (C2.3).
**Why:** Follows 7.4: a claim that returns after its ruling is not ruled again, even if other findings changed around it.
**Rejected:** The whole set as identity - a changed set would earn a new ruling on findings already ruled on.
**Decided by:** user

### D22: Does a single-task or no-Workflow run audit its selected tests?
**Choice:** Yes. The implement skill and the no-Workflow guide run the read-only auditor before the first attempt; an audit problem is a needs-input entry (6.12), since those runs never call the arbiter. Raised by the consistency check (C2.10).
**Why:** Follows 13.1, and those runs benefit from the audit most, since nothing else checks them.
**Rejected:** Whole-spec runs only.
**Decided by:** user

### D23: How does ck recover an integration checkout that a refused raw merge left dirty?
**Choice:** Before it lands or merges into a managed integration checkout (an `integrate/*` branch checked out under the worktree directory), ck restores that checkout to its own head with the tokened reset (`git reset --hard` to the current head, so the ref never moves) and reports the discarded paths and whether a merge was in progress. It does this only when ck's git hook recorded a refused update of that branch, from that checkout, at that same head. A dirty checkout with no such record is refused as today (14.5). Raised by the unattended-correctness run: TC-178's scripted agent had to clean up after its refused merge (commit a64c0aa), and unattended-correctness PLAN D6 records an agent doing such a merge in the equity E2E.
**Why:** A refused merge leaves staged files (fast-forward) or a merge in progress (non-fast-forward), and every later landing refuses. `git reset --hard HEAD` clears both and passes the hook because the old and new values are equal; a reset to a protected commit is still refused (Fable max ruling, checked in a temporary repository). The refusal record keeps the recovery from discarding a merge I am doing by hand in the same checkout.
**Rejected:** `git merge --abort` - misses the fast-forward case. Restoring any dirty integration checkout - would discard a hand merge in progress. Leaving it to me - one refused agent ends the run.
**Decided by:** user (drafted in the unattended-correctness session; the refusal record and 14.5 are this spec's reading of "a refused ref update" in 14.1)

### D24: How does bisection decide which task broke a red landing batch?
**Choice:** Each half of a bisected batch is gated itself; nothing is inferred from the other half. A task is RED only when a gate on exactly that task, on the current target head, fails, and its result carries that gate's failures. A batch red as a whole that no half and no task reproduces is reported `unreproduced` with the batch gate's failures: the tasks whose own gates passed land, and the run sends the target to the full gate and its fixer, as after a red periodic gate.
**Why:** A batch that completes the spec is gated with the full suite (`gate.py` `spec_complete`), while its halves get task-scoped gates that cannot see that failure. `land_group`'s "a green left half on the same base proves the right half red" then blamed the last task by elimination (TC-176 smoke scenario: an earlier task's regression in `tests/test_other.py` was blamed on Task 7). The attributed full gate already finds the task a full-suite failure belongs to.
**Rejected:** Gating each half with the whole batch's scope - a half lacks the other half's code, so the batch's checks can fail on it with no culprit there. Keeping an unreproduced batch off the target - a regression already on the target would hold every later batch.
**Decided by:** user (drafted in the unattended-correctness session; the choice between the two forms of 15.1, and landing the green halves, are this spec's)

## Delegated to Spec

- The arbiter's prompt, and the structured ruling format (outcome, principle cited, "needs a stronger look").
- How a "distinct claim" is identified for D10 (for example file plus test plus criterion).
- The format of decisions.md entries, within D12's fields.
- The starter text of `specs/PRINCIPLES.md`, within D5's defaults.
- The audit agent's prompt, and which stops it reports to the arbiter.
- The exact lint rules and messages, within D16.

## Risks

- Two specs edit `spec-implement.js` at once -> D17: built on the other's integration branch, merged in periodically.
- An arbiter that is wrong acts with authority -> D12's decision log, tests can't be weakened (guard), requirements untouched (D2), and D10's one ruling per claim.
- Editing the guard to allow a new caller widens what an agent can change -> the `ck-arbiter` lane is limited to tasks.md, design.md and test-plan.md (D11).
- Principles written carelessly give confident wrong rulings -> /spec:plan asks for them directly; every ruling cites the principle it used.
- This work depends on an unfinished spec -> it can't land until unattended-correctness does (D17).
- Recovery discards files in an integration checkout -> only tracked files, only at the head a recorded refusal names, never moving the ref (D23).
- Gating every bisection half costs more gates -> at most 2n-1 gates for a batch of n, and a red batch halves the landing window, as today (D24).

## Open Questions

None.


## Research Notes

### Codebase
- `plugin/workflows/spec-implement.js` - stops: `testConflict` (confirmed by a second agent, ~1504-1521), `guardStop` and `climbGuard` (~1419), review deadlock in `reviewLoop` (~1538-1575)
- `plugin/workflows/spec-create.js` - phases: Requirements, Design + Test Plan, Task Tracks, Link, Adversarial Review, Revise, Consistency Check
- `src/claude_kiro/hooks/pre_tool_guard.py` - task agents are denied changes to existing text in requirements.md and test-plan.md; the main session gets the mode from specs/ck.json "guard"
- `src/claude_kiro/hooks/_shared/guard_policy.py` - `classify_caller` decides who is asking
- `src/claude_kiro/agents.py` - roles and their settings (planning, reviewer, fixer...)
- `src/claude_kiro/lint.py` - verify-order checks, already extended for issue #15
- `src/claude_kiro/gate.py` - `suite_due`: a test task's suite runs only once every task it verifies is Done

### Evidence for D23 and D24 (from the unattended-correctness run)
- `src/claude_kiro/worktrees.py` on `integrate/unattended-correctness`: `_git_reset` (~123) is the tokened reset; `merge` (~871) and `land` (~1102) refuse a target with uncommitted changes to tracked files; `land_group` (~1235-1260) skips gating the right half after a green left half.
- `src/claude_kiro/gate.py` (~1005-1006): `spec_complete` turns off the green-only, task-scoped mode, so the batch that completes a spec runs the full suite.
- `src/claude_kiro/cli/githooks.py`: `reference-transaction` and `pre-merge-commit` are where an `integrate/*` update is refused; `git merge --no-verify` skips only the second.
- `tests/test_unattended_smoke.py`: TC-178's scripted agent resets after its refused merge (a64c0aa); TC-176 pins `landOrder` because the spec-completing batch was bisected onto the wrong task.

### Evidence from unattended-correctness
- Verify scope selected tests needing a later task: Tasks 71, 12, 44, 78, 19 (twice), plus Task 2's suite
- Test or fixture bugs no correct code could pass: Tasks 13, 90, 19 (TC-115), 36
- Genuine spec contradiction: Task 20 (`-k "tc3_ and tc4_"`, P-3 against a red-team test)
