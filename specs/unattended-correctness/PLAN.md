# Plan: Unattended Correctness

**Status:** Decided
**Updated:** 2026-11-07
**Evolves:** none (builds on deterministic-plan, prompt-free-runs, landing-speed and run-followups, all decided and fully done)

## Problem
`/spec:implement` orchestrates well but does not yet produce working software unattended. In the OpenCode equity E2E
(`management-track-record`, Muse Spark, 14 passes, 333 agents), 92 of 94 tasks merged, yet:

- the human (me, through Claude) made 14 test-track repair commits plus one code-branch fix,
- the project's own suite ended with 17 failures and 3 errors, including all 5 critical-path E2E tests, because task gates run scoped tests and the full gate excuses failures while a test-first spec is in progress,
- the outer OpenCode session merged a protected file (`.claude/CLAUDE.md`) with raw `git merge` after the workflow refused it, and called that a human review,
- task agents reported one blocker at a time, so files with several bad tests took several passes,
- tests depended on CLI commands owned by unfinished tasks, which `ck relink`'s import scan cannot see,
- every role ran on one model under OpenCode, though the run asked for Haiku/Sonnet/Opus by role.

For: me, running whole specs unattended on Claude Code and on OpenCode.

Success: a fresh equity E2E rerun ends with the project's full suite green and zero hand repairs. Human decisions the
product asks for (such as allowing protected paths) are still allowed and do not count as repairs.

## Scope
**In:** (all 8 improvements, user decision)
1. The full gate pins failing tests to their tasks and turns red once all of them are Done; red goes to the fixer.
2. Protected-file enforcement that plain git cannot bypass.
3. Stop bad tests early: fixture/schema validation, cross-test checks, test-task review against the spec row.
4. An automatic test-fix loop for tests two agents agree can never pass, under the existing guards.
5. Agents report every gate failure, not just the first.
6. Dependency detection through CLI dispatch (static and at runtime).
7. Per-role models under OpenCode.
8. Fixture provenance, and stale claims from a previous run at run start.

**Out:**
- Diagnosing or fixing the equity app's own remaining bugs; the reruns surface them, and the product's loops fix them.
- Proving where a fixture came from (D9 only detects drift and mislabels).
- New per-language test runners beyond pytest for node-level attribution; others fall back to file-level attribution.
- Any change to the guards: never weaken a test, never edit requirements.md or existing test-plan rows.

## Constraints
- Harnesses: Claude Code, and OpenCode with `opencode-go/muse-spark-1.3-contributor` (medium) and
  `opencode-go/deepseek-v4.1-flash` (medium). The user wants to try the DeepSeek harness (`dsh`) at the end.
- Builds on the unmerged `fix/open-issues` (0.11.1); this branch rebases onto master once that merges.
- The existing guards stay: never weaken a test, never edit requirements.md or existing test-plan rows.

## Decisions

### D1: How is a failing test pinned to the tasks it covers?
**Choice:** Per test, via TC IDs. The gate runs the suites it would excuse today, reads failing test node IDs, maps each to its TC ID (test names carry `tcNNN_`), and from there to the impl tasks whose Verify selects it or whose verifying test task covers it. A failing test is a regression (red) once all of its tasks are Done, and excused while any is not. A test with no TC ID falls back to its file's owning test task and that task's Verifies.
**Why:** It is precise and uses conventions specs already follow (TC IDs in test names, `-k "tcNNN_"` in Verify lines). In the equity run, real regressions such as TC-6 hid in suites that also covered unfinished tasks.
**Rejected:** per test file - still coarse when one file covers several tasks; an explicit per-file attribution map - puts more work on the test writer, the weakest link in the E2E.
**Decided by:** user

### D2: When does the whole suite run with attribution, and who runs it?
**Choice:** The periodic gate (every N merges, default 10) and the final gate run `ck gate --attribute --json`. Under OpenCode the runner calls it directly, with no agent; under Claude Code a thin agent runs it and returns the JSON verbatim, since the Workflow runtime has no exec. Each attributed failing test is rerun once before it counts as a regression, so flakes don't reach the fixer. Attributed regressions go straight to the fixer with the structured failures. Landing gates stay task-scoped for speed.
**Why:** It catches regressions while the spec is in progress, at bounded cost: the equity suite takes about 10 minutes, so running it per landing batch would add hours. It also extends the direct-ck pattern from deterministic-plan to gates.
**Rejected:** the full suite on every landing batch - about 10 minutes per batch over dozens of batches; keeping the agent-run gate and only adding attribution - keeps the agent's cost and free-text results.
**Decided by:** user

### D3: How are gate failures reported?
**Choice:** `ck gate --json` emits `failures[]`: test node, TC ID, attributed tasks, file, kind (assertion, import, collection, timeout, error) and a short message. Agents run the whole Verify, never stopping at the first failure, and return that list. `testConflict` becomes `testConflicts[]`. Gates, retries, the fixer and the test-fix loop all share this one format.
**Why:** In the equity run a file with several bad tests took one pass per test. Lists that ck produces are consistent across models, and attribution (D1) needs node IDs anyway.
**Rejected:** agents writing the list - output drifts between models; ck only - leaves task agents reporting one blocker per pass.
**Decided by:** user

### D4: Who repairs a test that two agents agree can never pass?
**Choice:** A test-fixer agent in the test-writer role applies the confirmed fixes on a `testfix` branch from the integration branch. It must then show the fixed test passes against the blocked task's branch, or fails only for a reason the task still has to address. A reviewer at the risky tier checks the diff against the test-plan row before it lands through `ck worktree land`: no weakening, no requirement or test-plan edits. Each test gets one automatic fix; a second conflict on the same test goes to me.
**Why:** It removes the hand repairs that dominated the equity E2E. The proof step and the review guard against a wrong fix, as when Task 79's report blamed a test for a real `splice()` bug.
**Rejected:** the fixer editing tests with no proof - a wrong fix lands unchecked; keeping it human - runs stay attended.
**Decided by:** user

### D5: Which checks must a test-track task pass before it lands?
**Choice:** All four:
1. **Right-reason failures:** the test-track gate runs the new tests against the integration branch, and every failure must be a "not built yet" kind (missing module or command, NotImplemented, unknown subcommand). Assertion errors on fixtures, or schema and validation errors, send the task back to the test writer with the D3 failure list.
2. **Project fixture check:** a command the project declares in specs/ck.json (for example `test_track.validate`) runs on test-track gates, so ck stays project-agnostic.
3. **Blocking cross-test check:** `reviewer.cross_test` is on by default, and its findings block like review findings and route to the D4 test-fixer.
4. **Row-fidelity review:** a script first checks each test's values, enums, dates and thresholds against its test-plan row and the design's data models. The test-task reviewer gets only what the script can't parse, since LLM review accuracy drops sharply as diffs grow.
**Why:** The equity blockers were mostly invalid fixtures, wrong enum values, double quoting and a test that swapped its row's values for an unreachable threshold. Each check catches a class of these before an implementer ever sees them.
**Rejected:** none; all four were chosen.
**Decided by:** user

### D6: How are protected files enforced so plain git can't bypass it?
**Choice:** ck installs git hooks for its worktrees:
- A `reference-transaction` hook, which fires on every ref update and which `--no-verify` cannot skip, refuses to move `integrate/*` to a commit that adds protected changes not listed in `land.allow_paths`, unless ck issued a one-shot landing token.
- A `pre-merge-commit` hook gives an earlier, readable refusal.
- `ck worktree merge` gets the same check as `land`.
- A run-end audit compares protected files against ck's last approved state and reports any drift.
- The `reference-transaction` hook stays cheap and acts only on `integrate/*` refs. The spec must verify the hooks work under the ck run sandbox, where the worktree metadata directory was read-only in October.
**Why:** In the equity E2E an agent merged a refused branch with raw git. The check only existed in `land()`, and wording alone can't stop an agent with a shell. Hooks prevent it, and the audit catches whatever gets past them, such as an agent that removes the hook.
**Rejected:** audit only - the bad state exists until the audit runs; harness permission rules - specific to one harness, and they miss other shells and tools.
**Decided by:** user

### D7: How does ck detect a test's dependency on a CLI command another task builds?
**Choice:** A unified spec graph, building on the user's earlier move from waves to a task dependency graph. ck builds one graph from tasks.md, test files and code:
- **Nodes:** tasks, test cases (TC-n), test files, source files, CLI commands.
- **Edges:** depends (task to task), verifies (test case to task, from Verify `-k` selectors and Verifies), owns (task to file), provides (task to command), invokes (test file to command, scanned from test code) and imports (today's scan).
- **Provides:** spec-create writes a `**Provides:**` field on impl tasks wherever it can't be inferred.

The scheduler also uses the owns edges to refuse running tasks whose write-sets overlap at the same time, since agent branches conflict 20-42% of the time and mostly in source files. Lint reports missing edges, such as a test invoking a command whose task isn't among its own task's dependencies, and `ck relink` adds them. At runtime, a missing-command failure (unknown subcommand, unrecognized arguments, invalid choice) adds the edge live and the task waits, instead of raising a test conflict. D1's attribution walks the same graph.
**Why:** The equity tests depended on `mtr thesis new` (Task 71) and `mtr express choose` (Task 82) through CLI dispatch, which import scans can't see. One graph serves dependency detection and attribution alike, rather than two separate code paths.
**Rejected:** a Provides field plus runtime checks without a shared graph - attribution and dependencies would stay separate code paths; runtime only - each case costs a failed attempt first.
**Decided by:** user

### D8: How do roles get their own models under OpenCode?
**Choice:** A per-role map for each harness. `opencode.roles` sets each role (implementer, test_writer, reviewer, reviewer_risky, fixer, orchestrator, resolver) to a `provider/model#variant`, in global or project ck config. Unset roles fall back to the alias map. Bench configs get the same `roles` map per arm, so one arm can be, for example, "Muse implements, DeepSeek reviews". Claude Code keeps its aliases.
**Why:** In the E2E every role ran on one model. A cheap implementer with a stronger test writer and reviewer is probably the cheapest quality gain, and it lets the bench compare Muse Spark and DeepSeek V4.1 Flash by role.
**Rejected:** alias maps for bench only - roles sharing an alias still can't diverge, and normal runs gain nothing; provider refs in the shared agents config - they break Claude Code or need special cases there.
**Decided by:** user

### D9: How far does ck go on fixture provenance?
**Choice:** A hash lock plus a labeling review. Projects declare recorded-fixture globs in specs/ck.json, and ck keeps a hash lock for them. Any later change to a locked file is reported at land and in the run-end audit, unless the change is marked handcrafted with a reason. The test-task reviewer checks that fixtures without capture metadata are labeled handcrafted. This detects drift and mislabels, but it doesn't prove where a fixture came from.
**Why:** Fixtures belong to each project, but the equity run showed "recorded" series that were really synthetic, and later hand edits went unrecorded. A lock and a review catch both without needing network access.
**Rejected:** a capture command (`ck fixtures record`) - needs network access and credentials in the test writer's environment, which runs often lack; leaving it out of scope - the mislabeling goes unchecked.
**Decided by:** user

### D10: What does a new run do with claims left by an earlier run?
**Choice:** Release them by run ID. Each claim records the run that made it. At run start, the run releases claims whose run is finished or no longer alive (its record isn't running), keeping their commits, and it respects claims from a live run. The 60-minute time rule stays as a fallback for claims with no run ID.
**Why:** In the E2E I released stale claims by hand before almost every pass. Time alone can't tell a crashed run from a slow live one.
**Rejected:** a shorter timeout - still a guess from time, so a slow live agent could lose its claim; taking over every claim at start - two runs on one spec could fight over worktrees.
**Decided by:** user

### D11: How do we try the DeepSeek harness?
**Choice:** Harness selection becomes a registry, replacing the if/else on claude|opencode in bench and doctor. The spec's last phase adds a `dsh` (DeepSeek Harness) adapter:
1. Verify the official repository's API first.
2. Build a host that provides the workflow's `agent()` and `ck()` calls.
3. Run the equity E2E on it as a third harness.

If that check finds `dsh` can't host the workflow, the phase stops with a findings report instead of an adapter.
**Why:** The user wants to try DeepSeek Harness at the end. A registry makes it, and any later harness, an addition rather than a rewrite. The guarded stop protects the rest of the spec, since current knowledge of `dsh` comes from third-party write-ups.
**Rejected:** registry plus a spike only, with the adapter in a follow-up spec - doesn't let the user try it in this effort; a separate spec later - same.
**Decided by:** user

### D12: Which equity E2E reruns must meet the success bar?
**Choice:** All four must pass: OpenCode with `opencode-go/muse-spark-1.3-contributor` (medium), OpenCode with `opencode-go/deepseek-v4.1-flash` (medium), Claude Code with default roles, and DeepSeek Harness (`dsh`). The bar is the project's full suite green with zero hand repairs, though decisions the product asks a human for are allowed. Each run must also not regress cost per merged task or wall minutes per task (from `ck stats`) compared with the first E2E, since this plan adds a test-fixer, risky reviews, blocking cross-test checks and attributed gates. Roles stay single-model in the two OpenCode runs, so they compare cleanly with the first E2E. For `dsh`: if the verified API shows it can't host the workflow's `agent()` and `ck()` calls, the documented findings report from D11 satisfies the requirement. If it can host them, its E2E must pass like the others.
**Why:** It shows the fixes hold across two models and three harnesses, not just one lucky run.
**Rejected:** OpenCode and Claude Code required with `dsh` best effort - the user wants all four; one OpenCode run - doesn't show the fixes hold across models and harnesses.
**Decided by:** user (the `dsh` findings-report rule too)

### D13: How are the reruns set up?
**Choice:** The reruns use the equity spec from `fix/mtr-spec-issues`, which has the E2E's link fixes. The 12 reviewed slash-command paths are pre-approved in each bench's `land.allow_paths`, so the protected-file decision doesn't count as a hand step. `.claude/CLAUDE.md` stays protected, so Tasks 93 and 94 need the user's approval in each run. That approval is a decision the product asks for, not a repair.
**Why:** It starts from the spec as fixed and keeps the human step to the one file that loads automatically into sessions.
**Rejected:** the original spec with protected files decided live - repeats known spec defects and adds hand steps.
**Decided by:** user

### D14: How do thin workflow agents avoid paying for context they don't use?
**Choice:** D2's gate agent and the other thin ck agents run as a dedicated lean agent type under Claude Code: Bash only, no CLAUDE.md, Haiku at low effort, a few turns, and one shared output schema. They stop being default subagents. Longer term, a ck mod running processes directly could drop the agent entirely, as OpenCode already does.
**Why:** An efficiency study of a 41-task Claude Code run found cache writes were 51% of spend, largely because every workflow agent inherits about 15k tokens of unrelated skill and tool listings. The workflow never passes an agent type. This plan adds agents, so it needs to start cheap.
**Rejected:** none considered; added from the efficiency study at the user's choice.
**Decided by:** user

### D15: One spec or staged?
**Choice:** Keep one spec. Tasks still run in dependency order.
**Why:** The user's call, after the efficiency study suggested staging or splitting.
**Rejected:** two phases with a measured rerun between them; splitting into two specs.
**Decided by:** user

### D16: Where do whole-spec create and review runs execute? (narrows prompt-free-runs D11)
**Choice:** In the session. From a `.claude/worktrees/` session, `/spec:create` and `/spec:review` no longer hand off to `ck run`; only `/spec:implement` does.
**Why:** The first `/spec:create` for this spec handed off to `ck run`, whose session ran on Sonnet at low effort, and spec-create's agents inherit the session model. So the spec was written below the planning minimum. Create and review only write spec files, so the isolation refusals behind prompt-free-runs D8 don't apply to them.
**Rejected:** keeping the hand-off for create and review.
**Decided by:** user (2026-11-07); implemented directly on this branch, not as a spec task.

### D17: What model does a `ck run` create or review session use by default?
**Choice:** The planning minimum (`planning.min_model` / `planning.min_effort`, Opus/medium). Implement keeps Sonnet/low, because its session only launches and reports. An explicit `--model` or `--effort` still wins.
**Why:** Same cause as D16: the session's model is the agents' model for create and review.
**Rejected:** none.
**Decided by:** user (2026-11-07); implemented directly on this branch, not as a spec task.

### D18: How do in-session spec runs avoid isolation refusals?
**Choice:** In-session workflows are the default on Claude Code for create, review and implement, as the user prefers live progress. Create and review run where they are (D16). A whole-spec implement started from an isolated `.claude/worktrees/` session leaves the isolation: `ck handoff` prepares the main checkout as a hand-off would (the same `<spec>-run` branch rules) but launches nothing, and answers `exitIsolation: true`. The skill then calls ExitWorktree (keeping the worktree) and runs the workflow from the main checkout in the session. If the session can't leave, it runs `ck handoff --force-handoff` and follows that run's progress here. `run.handoff: true` restores the old hand-off.
**Why:** The roughly 985 isolation refusals in prompt-free-runs all came from isolated top-level sessions (884 after EnterWorktree, 94 in background jobs), and none from main-checkout sessions or `ck run`. Claude Code's check can't be changed, and rewording agent instructions didn't help, so the only way to keep the run in the session is to leave isolation. The run's own work happens in `.ck/worktrees/` anyway.
**Rejected:** handing off with progress followed here - the work isn't in the session; giving each agent its own isolation under `.claude/worktrees/` - unproven, would need a spike first.
**Decided by:** user (2026-11-07); implemented directly on this branch, not as a spec task.

### D19: Answers to the spec review's open questions (2026-11-07)
- **V1:** a failing TC maps to the impl tasks whose Verify selects it; the test task's coverage is used only when no Verify selects it (1.1 reworded, P-1a added).
- **V2:** a test-body `AssertionError` is bad (blocks the test-track task) only when the failing assert runs no project code and uses no name imported from the project's own code; otherwise it is unclear, passes and is reported (5.3, 5.4, 5.7).
- **C1.9:** a wrapper test with no TC ID in an unowned file stays unattributed and red (1.5); when its parameter ID names an owned file, the entry gets a hint naming the owning test task, and nothing is excused through the parameter.
- **C1.10:** the lean runner omits CLAUDE.md only where Claude Code documents a frontmatter key for it (19.2 relaxed); otherwise the gap is recorded and TC-160 accepts the recorded gap.
- **C2.1:** a test fix may not change test-plan.md at all, a new row included; it is refused before the proof (4.5 amended to match D4).
**Decided by:** user

## Delegated to Spec
- The `ck gate --attribute --json` output schema, the failure `kind` taxonomy and how each test runner's node IDs are parsed (pytest first; other runners fall back to file-level attribution, D1).
- The spec graph's storage and API: built on demand or cached, and how inference fills `provides` and `invokes` edges before falling back to an explicit `**Provides:**` field.
- The hook installation details: where hooks live, how they coexist with a project's own `core.hooksPath` or pre-commit setup, and the landing-token format.
- The test-fixer's prompt, its branch naming and how proof against the blocked task's branch is checked.
- The "not built yet" failure classification rules for the right-reason check (D5).
- The key names for `test_track.validate`, the fixture lock file and `opencode.roles`.
- How claims record and compare run IDs, and how liveness is judged.
- The `dsh` adapter's shape once the official API is verified.

## Risks
- Node-ID attribution depends on test names carrying TC IDs -> fall back to file-level attribution, and lint warns about tests without TC IDs.
- Git hooks can collide with a project's own hooks or `core.hooksPath`, and an agent could remove them -> chain to existing hooks rather than replace them, and the run-end audit (D6) catches anything that gets past.
- An automatic test fix could wrongly change a test that was right -> require proof against the blocked task's branch and a risky-tier review against the row; each test gets one automatic fix, then it goes to the user.
- Running the full suite every N merges adds time (about 10 minutes per run on equity) -> N is configurable, and landing gates stay scoped.
- The right-reason check could misclassify a valid failure as a bad test -> classify conservatively; anything unclear counts as not built yet and is reported.
- DeepSeek Harness is a preview, and current knowledge comes from third parties -> verify the official API first, with the findings-report exit (D11, D12).
- Four required E2E reruns are long and costly -> the bench runs arms in sequence, with the D2 attribution catching regressions mid-run.
- `fix/open-issues` is unmerged -> this branch builds on it and rebases once it merges.

## Open Questions
- [x] All decisions settled (D1-D13); none deferred.

## Research Notes
### Efficiency study (2026-11, another session)
- Report: `/home/kutlay/Workspace/claude-kiro/.claude/worktrees/research-efficiency-2026-11/research/landscape/2026-11-efficiency-and-speed.md` (branch `worktree-research-efficiency-2026-11`). Its run wf_8bca74f5-ecc: cache writes 51% of spend, 44% of writes on each agent's first request; reviewers about 36% of spend across 96 runs.
- The Workflow concurrency cap is min(16, CPUs-2), which is 14 here; `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` raises it.
- Flakes: at Google, 84% of pass-to-fail transitions were flakes (basis for the D2 rerun).
- These figures are the study's, not re-measured here.

### Codebase
- `src/claude_kiro/gate.py` `_run_gate` 337-428 - excusing is per **test task**: `suite_due` runs a test task's Verify only when every task it verifies is Done; `green_only` (auto mode, test track, spec incomplete) skips the full `verify` suite until all tasks are Done. No test node is attributed to a task, so one real regression in a not-due suite is excused with it. `GateResult.checks` is already structured (name, ok, detail).
- `plugin/workflows/spec-implement.js` - periodic gate every `FULL_EVERY` (10) merges (1244) and the final gate (1647) go through an agent running `ck gate SPEC` (green-only while incomplete). Red goes to `fixAndRecheck` (1169): fixer ladder plus a recheck agent; still red sets `redTarget`. `DIRECT_CK` covers plan, land, relink only, not gates. The landing gate (`land --gate` -> `run_gate(target, spec, nums)`) is task-scoped too.
- testConflict (`RESULT.testConflict` {file, test, criterion, reason, fix}; confirmation 1472-1492; `result.testFixes` 1807) stops at a report: no agent acts on it; dependents wait.
- Protected paths (`worktrees.py` `PROTECTED_PATHS` 632, `protected_changes` 645, `land_allow_paths` 671) are enforced only in `land()`; `merge()` (the fixer's `ck worktree merge`) skips them; no git hooks are installed anywhere. Integration worktree: `create_integration` (326), created by the skill.
- Agent results carry free text (`blocker`, `gateTail`, `wrongTests`, one `testConflict`); `VERIFY` is `{green, failing}`; retries pass the previous report trimmed to 3000 chars.
- `lint.py` / `relink.py` - dependencies come from Python imports only (`IMPORT`, `_owners_reached`); no knowledge of design.md's CLI table or which task owns a command.
- `agents.py` `DEFAULTS` - roles implementer, test_writer (null = implementer), reviewer (+risky), fixer, orchestrator, resolver. Claude Code passes model/effort per call (`roleOpts` 94-123, which rejects anything but sonnet/opus/haiku/fable/claude-*). OpenCode maps the four aliases through `opencode.models` (`run-child.ts` `resolveModel` 504, `config.ts` 84/216), so roles sharing an alias can't diverge; `bench.py` `run_arm_opencode` forces all aliases to the arm's one model.
- Stale claims (`worktrees.py` 221-281): time-based (60 min, mtime of lock or branch tip); no run-start sweep, so a re-run within the hour finds tasks BUSY and skips them.
- Test-writer checks: `ck lint --contracts` (heuristic, warns), test-task review against design.md tables, `reviewer.cross_test` (off by default, report-only); the test-track gate only checks files exist, contain tests and collect - not that they fail for the right reason. No fixture/schema validation.
- Bench: `--harness` is `click.Choice(["claude","opencode"])` with if/else dispatch in `cli/bench.py` and `doctor`; a new harness is a new `run_arm_<harness>` returning the same result dict; the workflow needs a host providing `agent()` and optionally `ck()`.

### Sources
- [DeepSeek Open-Sources Harness: Everything Is a Plugin](https://www.digitalapplied.com/blog/deepseek-harness-open-source-agent-framework-2026) - `dsh`, MIT, Aug 2026; plugin runtime with headless CLI, ACP/JSON-RPC and a Python SDK; can call Claude Code or Codex as sub-agents. Third-party; verify against deepseek-ai/deepseek-harness.
- [DeepSeek V4.1 Flash on pi.dev (opencode-go)](https://pi.dev/models/opencode-go/deepseek-v4-1-flash) - OpenCode Go id `deepseek-v4.1-flash`, 1M context; ck's pricing table already lists `opencode-go/deepseek-v4.1-flash`.
- Equity E2E report: https://github.com/kutlayhanli/claude-kiro/issues/12#issuecomment-6701950471
