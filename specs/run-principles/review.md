# Spec Review: run-principles

**Date:** 2026-11-09
**Mode:** create
**Result:** Ready after decisions

## Fixed

| ID | File | Severity | Problem | Fix applied |
|---|---|---|---|---|
| R2 | requirements.md | major | `specs/PRINCIPLES.md` was created only by `/spec:plan`, so a project that skipped it could not get a Ready spec, with nothing saying why | 1.1 now covers `/spec:create`, `/spec:review` and a whole-spec launch (which creates and commits the file on the target checkout, `ck principles init --commit`). 1.9 stays for a file that cannot be read or created. New 1.12 for the `/spec:create` case. TC-139, TC-140, TC-141. |
| R3 | requirements.md | major | No positive criterion sent a top-model gate failure to the arbiter; "spec claim" left gate, audit and lint claims outside 6.10 | Added the term "Claim", 6.11 and 13.8, and widened 6.10 to claims with its two exceptions. TC-46, TC-47, TC-52 and P-13 updated, TC-142 added. |
| R5 | requirements.md | major | 9.6 demanded task-agent rules for input shaped like every real main-session call | 9.6 rewritten to give the unclaimed classification. TC-84 and Task 33 changed to match P-24. |
| R6 | design.md | major | Per-test findings under a per-task claim key made the second finding a needless needs-input entry, and each finding bought a ruling | `scopeClaims` groups by claim identity, one claim per group listing every test, re-lint after each landed fix. 12.6, Task 70, TC-115 and new TC-143. |
| R7 | design.md | major | A review deadlock keyed only by files merged different findings on one file | Key is now sorted finding identities (file plus normalized issue), `Claim.findings` replaces `finding_files`. Tasks 29, 69, P-18, new TC-146. |
| R8 | requirements.md | major | The single-task and no-Workflow boundary existed only in design | New 6.12 and 12.12, an Out of Scope line, design text and contract case TC-147. |
| R9 | requirements.md | minor | Compatibility NFR ignored the audit Story 13 adds to a clean spec | NFR now excepts the audit steps. TC-131 and design traceability updated. Same fix as V6. |
| D1 | design.md | critical | The `/spec:create` arbiter had no agent type, tier settings or way to run its `ck` steps | Added the `spec-create` args block (`arbiterAgent`, `runnerAgent`, `agents.arbiter`), a `thinCk` helper, and 11.12 for an absent `arbiterAgent`. Tasks 84, 86, TC-148, TC-159. |
| D2 | design.md | major | `pagent` would make the arbiter run `ck progress` commands its lane denies | Arbiter steps use plain `agent`; the workflow keeps their ETA with its own `thinCk` progress calls. TC-148 asserts it. |
| D3 | design.md | major | A reserved file could land if the arbiter left `reservedBy` empty | `SpecEditFacts.reserved`, enforced at `decide_spec_edit` by changed path, via the 8.4 path. `route_ruling` keeps `code` and `test repairs`. P-38, TC-149. |
| D4 | design.md | major | Only the start plan acted on `verifyScope`, though test-first files appear after launch | New 12.11, `applyLint` on non-start plans holds the task, one ruling per key; a selected file not yet on disk is `notChecked`. Task 70, TC-144, TC-145. |
| D5 | design.md | major | `_segment_read_only` passes double-quoted substitution and backticks, so the lane did not fail closed | `decide_arbiter_bash` denies `$(`, backticks, `<(` and `>(` outside single quotes before any allowlist; commit message is a plain single-quoted `-m`. TC-82, P-23. |
| D7 | design.md | major | `settle` removed the worktree on `rerun`, the path that needs it | Worktree removed only on `test_repair`, `code`, `needs_input`; on `rerun` kept and reset to the ledger's `base`. Task 38, TC-150. |
| D8 | design.md | minor | The drift step could leave review.md saying Ready when the workflow did not | `ck arbiter review --step drift` adds `requirements` items to Needs Your Decision and sets `**Result:**`, including on a lost step. Revise prompt names lost steps. Tasks 43, 85, TC-158. |
| D9 | design.md | minor | Re-plans dropped `verifiedBy`, leaving the audit decision on launch wiring | `PLAN` gains `verifiedBy`, kept by `applyPlanView`; `auditDue` reads the current view. Tasks 70, 74, TC-151. |
| T1 | tasks.md | critical | Task 6's gate could not pass | Task 6 depends on Task 5 and new Task 88, which owns `tests/test_plugin.py` and also verifies Task 76. Task 6's Verify deselects the dogfood, vendored-copy and guide tests. |
| T2 | tasks.md | major | Nothing regenerated the vendored copies | New Task 92 (lands by hand), and a Schedule note naming the only two tests expected red until it runs. |
| T3 | tasks.md | major | Nothing made the run start on the D17 base | New "Before launch" section (including bringing the spec onto the branch) and Task 89, which commits a real base check. Tasks 2, 18, 20 and 22 depend on it. |
| T4 | tasks.md | major | `land_lock` refuses, so TC-92 and TC-138 could not pass | `land_lock` gains `wait_s`; `ck arbiter record` and `land_spec_edit` use a bounded 120 s wait, task landings stay at 0. TC-92, TC-138 and Tasks 42, 44 restated. Contract shared with V2. |
| T5 | tasks.md | major | Properties asserted output `decide_claim` cannot produce | P-4, P-18 and P-19 restated at the seam's output; file forms, naming the file and "lists 3" moved to `ck arbiter open` (TC-153, TC-154). |
| T6 | tasks.md | major | P-22 required symlink forms at a seam that takes resolved paths | P-22 keeps resolved values; new P-39 runs path forms through the real hook, verified at Task 36. |
| T7 | tasks.md | major | P-1 listed prose and `Why: x` as reasons, which design does not read | Only `**Reason:** <text>` is positive; the others are negatives. Tasks 8, 9, 48, TC-6. |
| T8 | tasks.md | major | TC-123 required behaviour nobody owned | Reduced to the shipped tool list and `agentType`, per design. 13.4 added to Task 74. Shared with V8. |
| T9 | tasks.md | major | 1.7 depended on the arbiter reporting its own reservation | As D3; Tasks 8 and 40 now state which seam tests each reservation scope. |
| T10 | tasks.md | major | A failed record after a landed edit resumed the task | 10.9 added to Task 67: no resume, needs-input entry saying it landed. TC-155, `arbiterStepFailures` in the fake. |
| T11 | tasks.md | major | `decide_cut` could not be reached from the workflow | `ck arbiter context --approved-cut` returns `cut`, and `apply-cuts` branches only on it. Tasks 43, 86. |
| T13 | tasks.md | major | The arbiter's identity was never checked against what Claude Code sends | New Task 91 (lands by hand, paid), TC-160. Shared with V1. |
| T14 | tasks.md | minor | Tasks 3 and 45 held dependencies on Task 1 they do not use | Edges removed and the graph regenerated. |
| T15 | tasks.md | minor | The should-exist lens's refute step contradicted design | The refute loop skips it; `refute: should-exist` removed from Tasks 2, 83 and the test plan. |
| T16 | tasks.md | minor | Task 22's Verify omitted P-33 | `p33_` added. |
| V1 | test-plan.md | critical | No check confirmed `agent_type` for a Workflow-started arbiter | As T13; test-plan item 6 points at Task 91. |
| V2 | test-plan.md | major | TC-92 and TC-138 expected concurrent success from a refusing lock | Contract stated in design.md, TC-92, TC-138: bounded wait for `record` and spec-edit landings. TC-152 covers the timeout on the 10.9 path. |
| V3 | test-plan.md | major | P-23 allowed shell writes to lane files that design denies | P-23 and Task 34 deny them; negatives are reads, `git add` of lane files and `git commit -m '...'`. |
| V4 | test-plan.md | major | Properties ranged over inputs `decide_claim` never receives | New pure `claim_facts` seam, with P-4, P-10, P-18, P-19 pointed at it. TC-154 covers other tasks' rulings, TC-157 a `risk_paths` match and Complexity. "Safety-tagged" is defined in Terms as `ck plan`'s `risk` map. |
| V6 | test-plan.md | major | TC-131 conflicted with TC-120 on a clean spec | TC-131 expects today's labels plus the audit steps on a fixture with verifying tests. Tasks 74, 87. |
| V7 | test-plan.md | major | Create-side suites would break with no owner | New Task 90 migrates both suites and gives `fake_create` defaults, Task 2's Verify filters them, and the migrate list names them. |
| V8 | test-plan.md | major | TC-123 had no oracle for a write | As T8. |
| V9 | test-plan.md | major | Nothing connected the worktree `ck arbiter open` makes to the guard | TC-156 at the real boundary, Task 44 depends on Tasks 36 and 37, `arbiter_caller` takes a `checkout`. |
| V10 | test-plan.md | major | Universal SHALL NOT criteria had no property | Added P-40 (1.4), P-41 (1.11, 12.9), P-42 (4.5), P-43 (4.8), P-44 (8.12), P-45 (11.7), P-46 (11.11), each assigned to one test task. |

## Needs Your Decision

| ID | Question | Options | Conflicts with | Raised by | Answer |
|---|---|---|---|---|---|
| V5 | When a task's arbiter calls keep failing on different claims with no ruling recorded, should the 6-calls-per-task bound stop further calls and send the claim to me (7.5 gains a limit), or should only recorded rulings count, so failed calls are not capped per task? | Not yet offered by the review. See the same choice under R4. | 7.5, NFR Bounded arbiter cost, P-19 | adversarial review | Answered (D19): add a 6-call limit. 7.5 amended, 7.9 added, P-10 and P-19 corrected. |
| R4 | 7.5 and the 6-call NFR disagree on a task whose calls end with no ruling. This is the same question as V5, so it is left unapplied: should 7.5 gain a 6-call limit, or should only recorded rulings count? | (a) Apply R4: 7.5 becomes "fewer than 3 rulings and fewer than 6 arbiter calls", add "WHEN a task has reached 6 arbiter calls, THE SYSTEM SHALL NOT call the arbiter and SHALL send the claim to me as a needs-input entry", and correct P-19. (b) Only recorded rulings count: failed calls are not capped per task, and the NFR is narrowed to match. (c) Follow 7.5 as written: leave the contradiction with the NFR. | 7.5 "fewer than 3 rulings ... SHALL give it its own ruling"; NFR Bounded arbiter cost "at most 6 arbiter calls"; P-19 "exactly when the task has fewer than 3 rulings" | raised while applying fixes | Answered (D19): applied R4. |
| C1.1 | An audit problem reaches the arbiter and it returns a malformed ruling (no principle, unknown outcome). 7.2, which 13.3 applies to audit claims, sends it to me as a needs-input entry with the arbiter's output. 13.8 starts the task, notes the missed audit and creates no entry. Which should happen? | (a) Follow 13.8 as written: the task starts with no entry, and 7.2 gains the same audit exception 1.9 and 6.10 have. (b) Follow 7.2 as written: a malformed ruling on an audit claim is a needs-input entry, and 13.8 covers only a call that failed or returned nothing usable. Design 13.8, Task 74 and TC-142 then change. | 7.2 "SHALL send the claim to me as a needs-input entry with the arbiter's output"; 13.3 "under the rules of Stories 7, 8 and 10"; 13.8 "returns nothing valid for an audit problem, THE SYSTEM SHALL start the task"; TC-142 | found by the consistency check | Answered (D20): follow 7.2; 13.8 narrowed, TC-161 added. |
| C2.3 | A review deadlock over findings A and B gets a ruling that settles only A, and B still blocks at re-review (or A stays and a new finding C joins it). The design's key is the sorted identities of the findings still blocking, so `review:B` is a new claim and gets a new ruling. 7.4, and design's own sentence, say a finding that comes back unsettled is not ruled again. Which should happen? | (a) Follow 7.4 as written: identity is per finding, so any finding already covered by a ruling goes to me as a needs-input entry, and only findings never ruled on reach the arbiter. `claim_key`, `claim_facts`, P-18 and TC-146 change. (b) Keep the set as the identity: a changed set is a new claim, limited only by the 3 rulings per task, and design's sentence is narrowed to "the same set raised again". | 7.4 "SHALL NOT rule again"; design.md:446 "an unsettled finding raised again is the same claim"; design.md:440 "`review:<sorted finding identities>`"; TC-146; P-18 | found by the consistency check | Answered (D21): identity is per finding. |
| C2.10 | In a single-task run or a run without the Workflow tool, 13.1 still says an audit runs before the impl task's first attempt, and 6.12's "claim" includes an audit problem. Design puts the audit only in the workflow's `runTask`, and no task or test covers it elsewhere. Should those runs audit? | (a) Follow 13.1 as written: the implement skill and `without-workflow-tool.md` run the auditor, and a problem becomes a needs-input entry under 6.12. Design, Task 76 and TC-147 change. (b) The audit is whole-spec only: 13.1 gains that scope and Out of Scope gains the line, as it did for the arbiter (R8). | 13.1 "before that impl task's first attempt, THE SYSTEM SHALL run a read-only audit"; 6.12; design.md:585 "At the start of `runTask`"; design.md:630 | found by the consistency check | Answered (D22): those runs audit too. |

## Refuted

| ID | Problem | Why it was refuted |
|---|---|---|
| R1 | The plan's principle is that a test keeps its intent and only selection and setup may MOVE. The spec only logs a selection change. A spec edit can narrow a task's Verify so a test is selected by no task's gate at all, or rewrite or delete a test-plan case, and it still lands. | The premise is wrong. A test deselected from every task's Verify is not lost: gate.py runs config verify (the full suite) once every task is Done (gate.py:372-386), and this repo's specs/ck.json sets it to the whole pytest suite. PLAN.md:164's 'tests can't be weakened (guard)' refers to the guard denying the arbiter test files, which 9.3 enforces. The second proposed criterion contradicts binding D2 and D11. |
| R10 | 1.3 and 1.7 create a per-plan control over what the arbiter may change, while Out of Scope forbids 'A per-spec setting for how far the arbiter may go' and D2 rejected exactly that. | The finding itself concedes the narrowing-only reading is defensible, and the proposed fix is an added assumption plus a reworded line. PLAN.md:23 scopes 'what may change without asking', and 1.7 and 1.8 already state that a plan may reserve a change but never widen the arbiter into requirements. The Out of Scope line restates D2's rejected option, so the conflict is apparent and no behaviour is left undefined. |
| R11 | When the arbiter fails on the cut list, the criteria disagree on whether I see those cuts. | The criteria do not disagree. 4.2, 11.4, P-30 and tasks.md:1921 all say only cuts found sound reach the report; 11.12's 'as they stood' leaves an unchecked cut unpromoted while the step is named in review.arbiter.lost and the result is not Ready. The fix's preferred option contradicts binding D14. The only stray is the 'unchecked' value in design.md on a failure path, where the run already reports the check did not run. |
| D6 | The design says moving `suite_due` means the gate and its simulation cannot disagree, but other config decides what a task's gate runs, so 12.1 can give wrong findings that block launch. | The stated failure does not follow from the code. Every task gate unconditionally runs task.verify plus the due suites of verified_by tasks, so a task-level simulation selects a subset of what the real gate runs and cannot invent a blocking finding from those configs. The final-task case is harmless because every task is Done at that point. task_regression and non-auto verify_mode could only cause missed findings, which the audit and gate still catch. |
| D10 | `ck principles init` writes specs/PRINCIPLES.md into the checkout where /spec:plan runs and nothing commits it, while the arbiter reads the target checkout. | The missing-file case is fail-safe and the design accepts it: 1.9 requires the entry to name the file. The same commit requirement applies to every spec file, since an integration branch comes from the checkout's current branch, and no skill commits PLAN.md or tasks.md either. |
| T12 | Task 65's Verify covers rulings it does not say how to act on. | The spec already covers this. design.md defines arbitrate returning { exit } or { resumed }, and Task 65 adds arbitrate, so the loop has an owner. Task 65 lists 1.7, 5.7 and 7.2 and calls settle, so settle's needs_input route is in scope. The TCs and P-11 assigned to Task 65 assert labels, models, call counts and entries, not completion of a valid ruling. |
| V11 | test-plan.md still specifies an interface that design.md does not build, and the spec documents do not agree. | The drift is real but the spec already handles it by the mechanism it declares: test-plan.md says design differences are reconciled at Link and tests follow only by changing tests/arbiter_helpers.py, and Task 1 holds that map. The task descriptions restate the cases with design's real names. The one substantive concern, a signature-converting adapter hiding seam logic, is V4. |

## Unverified

None

## Schedule

- **Ready at start:** Tasks 89, 1, 3, 17, 88, 9, 75
- **Critical path:** Task 89 -> Task 2 -> Task 55 -> Task 56 -> Task 57 -> Task 58 -> Task 59 -> Task 60 -> Task 61 -> Task 62 -> Task 63 -> Task 65 -> Task 66 -> Task 67 -> Task 68 -> Task 69 -> Task 70 -> Task 74 -> Task 92 (19 of 92 tasks; the floor on wall-clock time however many agents run)

## Consistency Check

Pass 1:

- C1.1 (behaviour): 7.2 with 13.3 (a malformed ruling on an audit claim is a needs-input entry) against 13.8 and TC-142 (the task starts, no entry). Decision.
- C1.2 (property-conflict): P-22 (`requirements.md` denied at `decide_arbiter_edit`) against P-45 (allowed with a ticket before implementation starts). Fixed: P-22 and Task 34 now cover an arbiter worktree with no ticket.
- C1.3 (drift): TC-9 and Task 56 (a reserved design edit never reaches a landing) against design.md's `route_ruling` row and TC-149 (a file scope the ruling leaves out is refused at the landing). Fixed: TC-9 and Task 56 name `reservedBy`.
- C1.4 (drift): Task 65 (every `ask` on an audit claim lets the task start) against 13.8, Task 74 and 7.4, 7.6 (only `principles-missing` does). Fixed in Task 65.
- C1.5 (drift): Task 75 (any single-task run refuses on a `verify-scope` finding) against 12.12, TC-147 and Task 76 (only when the finding names its task). Fixed in Task 75.
- C1.6 (drift): design.md's `decide_arbiter_bash` row (the arbiter's prompt requires a plain single-quoted `-m`) against Task 6, which wrote no such instruction. Fixed in Task 6.
- C1.7 (drift): design.md's `Claim.safety` against `claim_facts`, which derives safety from `TaskMeta.risk`. Fixed: the field is removed.
- C1.8 (drift): test-plan.md's `arbiter_caller` signature against Task 1 and TC-156 (`checkout`). Fixed in test-plan.md.
- C1.9 (drift): Task 23's Verified by against Tasks 13 and 17 (P-41 at Task 23). Fixed in Task 23.

Pass 2:

- C2.1 (drift): Task 44's bounded `land_lock` wait against Task 42, which adds `wait_s` to the same file, with no order between them. Fixed: Task 44 depends on Task 42, and the graph has the edge.
- C2.2 (drift): `ck arbiter open`'s stdin of Claim alone (design.md, Task 37) against `claim_facts` needing `TaskMeta` and Task 65 passing it, after C1.7 removed `Claim.safety`. Fixed: stdin is `{"claim": Claim, "task": TaskMeta}`, as the Harness Contract has it.
- C2.3 (behaviour): the review-deadlock key over the set still blocking against 7.4 and design's "an unsettled finding raised again is the same claim". Decision.
- C2.4 (drift): Task 86's skill removing the story itself against its own `apply-cuts` mode, Task 75 and T11's "branches only on that field". Fixed in Task 86 and design.md.
- C2.5 (drift): Task 76's acceptance (any single-task run refuses) against 12.12 and its own description (only when the finding names its task). Fixed in Task 76.
- C2.6 (drift): Task 74, Task 71 and TC-123 hard-coding `agentType` `spec:auditor` against design.md's `auditorAgent` arg and the vendored `ck-auditor`. Fixed: the type comes from `auditorAgent`.
- C2.7 (drift): TC-123 asserting the generated `ck-auditor.md` against Not Tested (tests never read `.claude/`), which would keep it red until Task 92. Fixed: generated into a temp directory.
- C2.8 (property-conflict): TC-148 and design.md (no `arbiterAgent`: the steps are lost, not Ready) against P-46 and 11.11 (no subjects: not lost), on no `arbiterAgent` and no subjects. Fixed: only steps with subjects are lost.
- C2.9 (drift): the Harness Contract's `ck principles init` row against TC-140 and Task 31 (`--commit`, `commit`). Fixed in test-plan.md.
- C2.10 (behaviour): 13.1 with 6.12 (an audit in every run) against design.md, which audits only in the workflow. Decision.
