# Spec Review: deterministic-plan

**Date:** 2026-11-01
**Mode:** create
**Result:** Ready

## Fixed
| ID | File | Severity | Problem | Fix applied |
|---|---|---|---|---|
| D3 | design.md | critical | The new review-wave label has no role mapping in `src/claude_kiro/analytics/roles.py`, so batched reviews fall into role `other`; overhead share and W+R review-count bound mismeasured. No task touches `roles.py`. | Pinned the batched label as `review-wave <wave> tasks <ids>` mapping to role `reviewer` with wave + tasks; added `roles.py` bullet to design Components, label-grammar pin in test-plan Harness Contract row 6, NFR-4/overhead counting defined over role-`reviewer`; new Task 36 implements/tests the mapping (Tasks 16/18/22/33 wired to it). |
| T7 | specs/deterministic-plan/test-plan.md | minor | P-7 cites criterion 1.10, which does not exist (Story 1 has 1.1-1.5). | Corrected P-7 Requirement column to 2.5 in test-plan.md; tasks.md Task 4 already lists 1.5, 2.1, 2.2, 2.5 with no 1.10, so no change needed there. |
| V7 | specs/deterministic-plan/test-plan.md | major | P-9 Expected decision covers 4.6 (no review on empty wave) but Requirement column omits 4.6. | Added 4.6 to P-9 Requirement column (now 4.1, 4.2, 4.3, 4.6, NFR-4) and to the owning Task 16 Requirements. |

## Needs Your Decision
None. (Empty: no review findings needed a decision, and no escalations.)

## Refuted
| ID | Problem | Why it was refuted |
|---|---|---|
| R1 | 6.3/NFR-2 SHALL on equity outcome | Equity number is manual (TC-33), not CI; implements binding D4 (fixture + re-run). |
| R2 | risky vs safety vocabulary | Codebase has only safety\|normal; isSafety IS the risky check; Assumptions use them interchangeably. |
| R3 | wave undefined | Wave = one landBatch MERGED set; defined via Story 4 + design + TC-17-19/P-9. |
| R4 | threshold not in requirements | Binding D8 delegates number to spec; design publishes 0.5. |
| R5 | 1.2 vs 2.3 contradiction | Layered reading: 1.2 presupposes data; 2.3/TC-7/P-6 cover empty case. |
| R6 | numeric vs lexical tie-break | Settled by ck plan oracle + design; restating is style. |
| D1 | review-after-landing inverts gate | Post-merge review is binding D7/4.1; fix contradicts it. |
| D2 | batched verdict fan-out | Design requires per-task {task,...} findings; Task 18 defines fan-out. |
| D4 | seam name mismatch | Harness Contract permits renames; Tasks 9/33 pin final names. |
| D5 | synthetic view undefined | View never overwrites lastGood/view; scheduling uses script-side ready. |
| D6 | nonexistent role keys | plan/reviewer/land are real keys; fixer/revise exclusion intended. |
| T1-T4 | parallel tasks share test files | Disjoint appends coordinated by Task 1 skeleton; serialization would inflate critical path. |
| T5-T6 | seam signature mismatch | Draft-vs-final; design authoritative, contract permits rename. |
| T8 | 2.6 coverage | Transitive via Task 10 + Task 6 (TC-10). |
| T9 | 6.2 impl owner | Negative criterion, transitively covered; test-owned by Task 21. |
| T10 | Task 1 Verifies reciprocity | Informational indirect proof; ordering via Dependencies. |
| V2 | seam signatures | Same as T5-T6; permitted rename, Tasks 9/33 canonical. |
| V3 | 1.4 missing property | Covered by TC-1/TC-3, Task 7 red-team, P-3. |
| V4 | 2.6 missing property | Shared paths by design; TC-10 covers shapes. |
| V5 | 3.5/3.6 missing properties | TC-15/TC-16 + P-6/P-8/P-14 cover. |
| V8 | P-5 omits parked/blocked | Parked owned by unchanged paths; beyond requirements. |
| V9 | wave ambiguity | Design + Task 18 define W as landBatch MERGED set. |
| V10 | Task 21 Verify cost | Repo-standard command; D4 binds fixture gate to CI; safety-flagged. |
| V11 | P-2 oracle | Metamorphic healthy-plan run; naming editorial. |

## Unverified
None

## Consistency Check

Pass 1 (post-revision consistency check, 2026-11-01): diffed design.md, test-plan.md, tasks.md against `.pre-revision/`; checked every changed passage against requirements.md criteria (EARS ids) and test-plan.md Properties, and checked property pairs over overlapping domains. No behaviour conflicts; no property conflicts. Three drift issues found and fixed:

- C1.1 (drift, fixed): TC-17 still said "`review wave …`-style" after Harness Contract row 6 pinned the label as `review-wave <wave> tasks <ids>`; updated TC-17 to the pinned form.
- C1.2 (drift, fixed): Task 22 said its in-script reviewer share counts `review-wave` agents "via the Task 36 role mapping", but the workflow script (JS) cannot import the Python `roles.py` mapping; reworded to "counted as review agents, consistent with the Task 36 role mapping".
- C1.3 (drift, fixed): Task 16 Verifies Task 36 but the dependency graph had no T16 --> T36 edge (every other test-Verifies-impl pair has one); added the edge.

Pass 2 (post-pass-1 consistency check, 2026-11-01): re-diffed design.md, test-plan.md, tasks.md against `.pre-revision/`; re-checked the three pass-1-touched passages (TC-17 label form, Task 22 in-script counting wording, T16 --> T36 edge) against requirements.md criteria (EARS ids) and test-plan.md Properties, plus property pairs over overlapping domains. No behaviour conflicts; no property conflicts. One drift issue found and fixed:

- C2.1 (drift, fixed): the pass-1 T16 --> T36 graph edge had no matching entry in Task 36 Dependencies text (which listed only Task 18) despite Task 36 Verified-by Task 16; added Task 16 as an oracle dependency so graph, Dependencies, and Verified-by agree.

## Schedule
- **Ready at start:** Tasks 1
- **Critical path:** Task 1 -> Task 2 -> Task 9 -> Task 10 -> Task 14 -> Task 18 -> Task 22 -> Task 24 -> Task 26 (9 of 36 tasks; the floor on wall-clock time however many agents run)
