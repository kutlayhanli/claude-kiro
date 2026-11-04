# Test Plan: Deterministic Plan

**Created:** 2026-11-01
**Inputs:** [requirements.md](requirements.md), [PLAN.md](PLAN.md) (D1–D8 binding; D-out-of-scope items stay out). Written without reading design.md, so these tests check whatever the implementation turns out to be.

## Strategy

This repo has three real boundaries, and each criterion is tested at the one it crosses.
The integration-first rule from landing-speed applies unchanged:

| Boundary | How tests reach it here | Used for |
|---|---|---|
| The Workflow script `plugin/workflows/spec-implement.js` | Node runs the **real script source** under the fake runtime in `tests/js/load_workflow.mjs`. Agents are stubbed by the fake project (extended with bad-ready injection and wave-review tracking). `tests/test_workflow_scripts.py` runs the Node suites from pytest. | Stories 1–4 (fallback, plan schedule, wave reviews) |
| The `ck` CLI on real repos / real stores | `CliRunner` or `python -m claude_kiro.cli` subprocesses on temp git repos; `ck stats` over `FakeClaudeHome` stores via `tests/stats_helpers.py` subprocess helpers. | Story 5 (stats/report), `ck plan` readiness oracle, fixture spec (Story 6) |
| The OpenCode path `opencode/` | `node --test test/*.test.ts` from `opencode/`; vendored-copy parity test. | Both-harnesses constraint (PLAN.md D-constraints) |

**How integration-first applies here:**

- No test mocks a `claude_kiro` module, git, or the filesystem. A test reaches ck only through the CLI or a named public decision seam (see Harness Contract). The only fakes are for things this repo does not own: the Workflow runtime and its LLM agents (fake runtime + fake project); Claude Code's on-disk run files (FakeClaudeHome); the equity workload itself (fixture spec stands in for CI, one manual re-run for the headline number).
- Workflow tests judge behaviour visible from outside the script: which agents are requested, in what order, with which prompts and labels, against the fake runtime's own queue trace, plus the script's returned result and log. They never read the script's internal variables. The single exception is the decision seams (`readyTasks`, `planConsistent`, and the new `fallbackReady`/`batchReview`/`overheadShare` seams), which are called directly only in contract/property tests plus one integration case each proving the script/CLI actually uses them.
- Unit-level tests cover only the pure decision seams. Every seam property uses Hypothesis (`@settings(derandomize=True, max_examples=200, database=None)`); bridge-driven scheduler properties use `max_examples=60` with the same derandomized, database-free settings because each example runs the whole workflow. JS-side: no fast-check in this repo (no npm deps for workflow tests), so scheduler properties are generated in Python and run by the real script through the Node JSON-lines bridge (`tests/js/bridge.mjs`), exactly as landing-speed does.
- **Before the feature exists, each test fails for the right reason:** the script halts (`planning step failed`) on a twice-inconsistent plan; plan agents re-run after every freed slot; reviews are one-per-task; no `degraded`/`plan_fallbacks`/overhead-share fields exist in results, stats records, or `ck stats show` output.

## Test Infrastructure

### Exists (reuse as is)

- `tests/js/load_workflow.mjs`: `runWorkflow(scriptPath, { agent, args, slots })` evaluates the real script source with stubbed `agent/parallel/pipeline/phase/log`, records request/start/end `trace` and the label corpus.
- `tests/js/fake_project.mjs`: `project({...})` fake project (`ck plan` semantics over `deps/done`, `planFail`, `planDurations`, `relink`, `externalDone`, `worktree`, `reviewDurations`, landings, conflicts). Extended below, not forked.
- `tests/js/bridge.mjs`: long-lived Node process; `{op:'ready',…}`, `{op:'run', project, args, slots}`. Uses virtual time so scenarios replay deterministically.
- `tests/landing_bridge.py`: module-scoped pytest fixture `bridge` (starts `node tests/js/bridge.mjs`); Hypothesis strategies `dags()`, `subsets(ids)`, `scenarios()`. Skips if `node` is missing.
- `tests/test_workflow_scripts.py`: runs each `tests/js/*.test.mjs` in Node, fails on `FAIL`/non-zero.
- `tests/test_landing_scheduler_properties.py`: bridge-driven scheduler properties P-6–P-11, P-21–P-23 with label-grammar helpers (`kind()`, `outstanding_walk`, `FIRST_TASK/REVIEW/PLAN` regexes) — reuse helpers, extend trace predicates for `plan N retry`, degraded logs, and wave-review labels.
- `tests/stats_helpers.py`: `FakeClaudeHome`, `session().implement_run(...)`, `AgentSpec` (with `queued_at_ms`, `raw_progress`), `implement_result()`; `stats()` / `ck_sub()` subprocess helpers, `load_record()`.
- `tests/test_waves.py::task()/spec()`: tasks.md block builders. `tests/test_landing_readiness.py`: the `ck plan` ↔ `readyTasks` contract pattern (write tasks.md to a temp git repo, run `ck plan --json`, compare).
- `tests/test_plugin.py`: vendored-copy parity (`test_dogfood_copies_in_this_repo_match_the_plugin_source`) — extend to the workflow script copy.
- `tests/test_stats_queue.py`, `tests/test_stats_show.py`: `ck stats show` text/JSON patterns, `not recorded` sections, `env` fixture pattern.
- `opencode/test/*.test.ts` run with `node --test test/*.test.ts` from `opencode/`; `src/workflow-tool.ts:validateWorkflowArgs`.

### To add

1. **`tests/js/fake_project.mjs` extensions** (same module, additive options):
   - `planSeq: [{ready, deps?, done?, remaining?, cycles?, verifyCycles?, error?}, …]`: canned plan-agent answers consumed in order (call k gets `planSeq[k-1]`; exhausted → default `ck plan` semantics). This is the bad-ready injector: a `ready` contradicting `deps` exercises the fallback. A `badReady: {ready}` shorthand sets every plan answer's `ready` to the bad list while keeping `deps` truthful.
   - `risk: {n: 'safety'|'normal'}`: passed through as the plan answer's risk data (args carry `risk` the same way `deps` do).
   - Review-call ledger: `state.reviews` already records `(label, prompt)`; add `state.reviewTasks: {label: [tasks]}` parsed from the review prompt's task list so tests can assert wave batching (one review covering a wave) vs individual reviews.
   - `timestamps` on plan/review/land agent calls (existing `stamp()` pattern) so overhead-share tests can build agent-time splits.
2. **`tests/js/deterministic_plan.test.mjs`** (new Node example suite, added to the `test_workflow_scripts.py` parametrize glob): Stories 1–4 example cases. Uses the extended `project({...})` and asserts on `events`/`prompts`/`result`/`logs` only.
3. **`tests/landing_bridge.py` extensions**: `scenarios()` gains `risk: {n: 'safety'|'normal'}` (10–20% safety) and `planSeq` draws (consistent / first-inconsistent-then-consistent / always-inconsistent); `bridge.run` already forwards arbitrary project/args keys, no change needed.
4. **`tests/test_deterministic_plan.py`** (new pytest module): bridge-driven properties P-new (fallback + batching + plan-schedule deltas) with `PROPERTIES = {...}` map for the gate properties coverage, following `test_landing_scheduler_properties.py`.
5. **`tests/test_deterministic_stats.py`** (new pytest module): Story 5 via `FakeClaudeHome` + `ck stats collect/show` subprocesses. Needs `AgentSpec` labels `plan N`, `review …`, `land …` with `queued_at_ms`/`started_at_ms`/`duration_ms` control (exists) plus new result fields (`degraded`, `planFallbacks`) threaded through `implement_result()` as optional kwargs (default absent, so pre-change records stay byte-identical).
6. **`tests/fixtures/stats/pre-deterministic-record.json`**: a stats record produced by today's collector from a FakeClaudeHome run (generated once, committed). Story 5's "not recorded" cases read it without re-collecting (mirrors the landing-speed `pre-queue-record.json` pattern).
7. **Fixture spec for Story 6** (`specs/deterministic-plan-canary/` or under `tests/fixtures/`): a 3–4 task spec with `specs/ck.json` verify `uv run pytest -q -n auto`, whose fake plan agent is forced inconsistent; plus the CI job that runs it. The exact directory and injection mechanism are the implementation's choice (PLAN.md delegates fixture design); the test plan only requires the run completes degraded with ≥1 fallback (TC-31) and healthy-run control (TC-32).

## Test Cases

Conventions: "real script" = `plugin/workflows/spec-implement.js` evaluated by `load_workflow.mjs`/`bridge.mjs`; "fake runtime" = stubbed `agent` with FIFO `slots` queue; "fake project" = extended `project({...})`. Stats tests use real `ck stats` subprocesses over `FakeClaudeHome` stores.

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-1 | 1.1 | integration (workflow) | Given `planSeq` = [inconsistent ready, consistent ready] (same deps both times). When the run starts. Then labels contain `plan 1` then `plan 1 retry`, the retry's answer is adopted, `result` has no `degraded` flag and fallback count 0. | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-2 | 1.2, 1.3 | integration (workflow) | Given `planSeq` = [inconsistent, inconsistent] with truthful `deps` in args. When the run goes. Then a second `plan 1 retry` is followed by **no** third plan attempt; every task merges; `result.degraded is True`, fallback count ≥ 1, and the log/result names the triggering plan attempt (`plan 1`). | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-3 | 1.4 | integration (workflow) | Given a healthy plan agent (default semantics). When the run completes. Then `result` carries no `degraded` mark and fallback count is 0, even though plan agents ran. | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-4 | 1.5 | integration (workflow) | Given `planSeq` always-inconsistent, tasks 1–2 already merged/running when fallback triggers (seeded via `done`/in-flight timing with `planDurations`). When fallback scheduling happens. Then the deterministic ready list excludes merged, running, failed tasks and none of their records change (their labels appear exactly once; their reasons are untouched). | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-5 | 2.1 | integration (workflow) | Given `args.deps` present and an always-inconsistent plan agent. When the run continues. Then every `task N` request is the head of `readyTasks` over `args.deps` + run's Done/running/merged sets (checked per request, as in landing-speed TC-55), and the run completes. | real script + `readyTasks` seam | `tests/js/deterministic_plan.test.mjs` |
| TC-6 | 2.2 | integration (workflow) | Given **no** `args.deps` and `planSeq` = [consistent (ready=[1,2]), inconsistent, inconsistent]. When the third plan fails. Then the run schedules from the last good ready list ([1,2] minus finished), continues, and marks degraded. | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-7 | 2.3 | integration (workflow) | Given no `args.deps` and `planSeq` = [inconsistent, inconsistent] (no good plan ever). When the start plan twice-fails. Then no `task` label is ever requested, already-running tasks still land, and the stop reason is the planning failure (today's `planning step failed` or its documented successor). | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-8 | 2.4 | integration (contract property, see P-4) | Given generated DAGs. When the fallback ready list is computed. Then its set **and order** equal `ck plan --json` `ready` on the same tasks.md (longest remaining chain first, then numeric task id). | real `ck plan` CLI + fallback seam | `tests/test_deterministic_plan.py` + readiness writer |
| TC-9 | 2.5 | integration (workflow) | Given `args.deps = {1:[], 2:['99']}` (99 absent) with always-inconsistent plans. When fallback scheduling runs. Then `task 2` is never requested (unknown dep never met), tasks 1/3 proceed, the run does not halt. | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-10 | 2.6 | integration (workflow) | Given fallback-scheduled task 2 whose agent reports `BUSY` (and separately `ERROR`/blocked). When the report arrives. Then the record is `ok:false` with the same `worktree busy: …` reason as a plan-scheduled BUSY task, exactly one agent ran for it, and the run continues (mirrors landing-speed TC-18/P-21 with fallback as the scheduler). | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-11 | 3.1 | integration (workflow) | Given the TC-5 graph of landing-speed (12 tasks, `maxConcurrent: 3`). When the run completes with healthy plans. Then every non-retry `plan` request is the start plan, the first plan after a `land … end`, or an idle plan (TC-6/3.4 conditions); the post-landing re-read exists, the per-freed-slot re-read does not. | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-12 | 3.2 | integration (workflow) | Given `maxConcurrent: 2`, deps `{1:[],2:[],3:[]}`, task 1 long. When task 1's implementer ends while tasks 2–3 are deterministically ready. Then `task 2`/`task 3` requests follow with **no** intervening `plan` request. | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-13 | 3.3 | integration (workflow) | Given a run where freed slots coincide with non-empty deterministic ready. When slots free repeatedly. Then no `plan` request appears between a finish and the next `task N` request that the deterministic list already determined (plan count stays ≤ 1 + landings + idle plans). | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-14 | 3.4 | integration (workflow) | Given an idle state (nothing ready, nothing running) that a re-read does not resolve, with a landing still in flight. When the re-read returns empty. Then no second idle `plan` request is made until a landing completes or a task record arrives (at most one idle plan per arming). | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-15 | 3.5 | integration (workflow) | Given `args.deps` plus a plan that reports `externalDone`-style Done tasks and a `relink` at plan 2. When the consistent plan returns. Then the script's view replaces remaining/deps/lint data, and Done = plan Done ∪ merged-in-run (a merged task stays Done even if the plan omits it). | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-16 | 3.6 | integration (workflow) | Given no `args.deps` with a healthy plan agent. When freed slots occur with ready tasks outstanding. Then a `plan` agent runs before the next `task N` request (the script does not use its own readiness without dep data). | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-17 | 4.1 | integration (workflow) | Given a landing batch of 3 non-risky tasks. When the batch is about to land. Then, before it merges, exactly one `review-wave <wave> tasks <ids>`-style batched review agent (e.g. `review-wave 2 tasks 4 5 7`) runs covering the wave's combined changes (one review label for the wave, not three `review N` labels), and all three tasks are approved. | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-18 | 4.2 | integration (workflow) | Given a run with 2 normal + 1 safety/risky task. When reviews run. Then the risky task gets its own individual `review N` agent before it lands and the two normal tasks share one batched review; the risky review uses the risky reviewer settings. | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-19 | 4.3 | integration (workflow) | Given a landing batch containing **only** risky tasks. When reviews run. Then each risky task gets an individual review that ends before its landing, and no combined review covers them. | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-20 | 4.4 | integration (workflow) | Given a batched wave review returning `changes` for task 2 only. When handled. Then task 2 alone leaves the batch unmerged and goes to `revise`, tasks 1/3 are recorded approved without revision and land, only task 2 is re-reviewed, and task 2 lands after its revision and re-review (not with its batch). | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-21 | 4.5 | integration (workflow) | Given a batched wave review returning `approve`. When handled. Then every task covered by that review records approval (verdict `approve`, same reviewer model/effort attribution as individual reviews). | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-22 | 4.3 | integration (workflow) | Given a task whose landing never merges (protected path). When the run settles. Then it was reviewed exactly once, before its landing, and no batched review covers zero tasks. (4.6, surviving findings never merge, is covered by the spec_implement reviewer tests.) | real script, fake runtime | `tests/js/deterministic_plan.test.mjs` |
| TC-23 | 5.1 | integration (CLI+files) | Given a collected run whose result is degraded with 2 fallbacks at `plan 2` / `plan 3 retry`. When `ck stats show <run>` renders. Then the report names the degraded mark, the count (2), and the triggering attempts. | real `ck stats show` over FakeClaudeHome | `tests/test_deterministic_stats.py` |
| TC-24 | 5.2 | integration (CLI) | Given the TC-23 run with plan/review/land agent rows. When `ck stats show <run>` renders. Then the plan/review/landing agent share of agent time appears next to the `plan_fallbacks` count. | real `ck stats show` | `tests/test_deterministic_stats.py` |
| TC-25 | 5.3 | integration (CLI) | Given the TC-23/24 run. When `ck stats show <run> --json` runs. Then the JSON fallback counts and overhead shares equal the human-readable values parsed from the text view. | real `ck stats show --json` | `tests/test_deterministic_stats.py` |
| TC-26 | 5.4 | integration (workflow + CLI) | Given a run whose plan/review/landing share is forced above the published threshold (tiny task durations, many plan/review agents). When the run completes. Then the report flags the overhead warning **and** the run still completes with no stop reason naming overhead (warn-only: no halt/fail/retry). | real script + real `ck stats show` | `tests/js/deterministic_plan.test.mjs` + `tests/test_deterministic_stats.py` |
| TC-27 | 5.5 | integration (CLI) | Given `tests/fixtures/stats/pre-deterministic-record.json` (pre-change record, no fallback/overhead data). When `ck stats show <run>` and `--json` run (no re-collect). Then fallback/overhead sections read `not recorded` and every existing figure prints unchanged. | real CLI, real store files | `tests/test_deterministic_stats.py` |
| TC-28 | 5.6 | integration (CLI) | Given the pre-change record plus a new post-change record in one store. When `collect --all` / `show` run. Then the old record reads without re-collection and its figures are byte-identical before/after (no migration rewrites). | real `ck stats` | `tests/test_deterministic_stats.py` |
| TC-29 | NFR-3 | integration (contract) | Given generated graphs (P-4 domain). When the deterministic ready list and `ck plan --json --exclude` run on the same tasks.md. Then set and order are identical. | real CLI + seam | `tests/test_deterministic_plan.py` |
| TC-30 | NFR-4 | integration (workflow property, see P-9) | Given runs with W merged waves (with ≥1 non-risky task) and R risky tasks. When reviews complete. Then review-agent count ≤ W + R, counted from labels. | real script, fake runtime | `tests/test_deterministic_plan.py` |
| TC-31 | 6.1, NFR-1 | e2e (fixture spec) | Given the fixture spec with the planning agent forced to return a ready list contradicting its dependencies. When the whole-spec implement runs in CI. Then it retries once, falls back deterministically, runs to completion, and the report shows degraded with fallback count ≥ 1. | real workflow script + real `ck plan` data path; fixture repo | CI job + `tests/test_deterministic_fixture.py` (or the fixture's own assert step) |
| TC-32 | 6.2 | e2e (fixture control) | Given the same fixture spec with a healthy planning agent. When it runs. Then it completes with zero fallbacks and no degraded mark. | same as TC-31 | same as TC-31 |
| TC-33 | 6.3, NFR-2 | manual (headline) | Given the equity workload re-run after this change. When `ck stats show` is compared with the baseline run. Then plan/review/landing agent share is lower and no planning halt occurred. | real runs, real `ck stats` | manual comparison (recorded in the run report, not a CI assertion) |
| TC-34 | constraints | contract (files) | When the workflow script changes. Then the OpenCode vendored copy is byte-identical and `validateWorkflowArgs` still requires `spec`+`root` for spec-implement (existing parity test extended). | real files | `tests/test_plugin.py` + `opencode/test/workflow-tool.test.ts` |
| TC-35 | seams | integration (seam called by the real code) | Given a fallback run and a batched-review run. Then the script's per-request scheduling agrees with `fallbackReady(…)` on the same inputs, and each batched review's coverage agrees with the batch-eligibility seam; `ck stats show` overhead numbers equal `overheadShare(…)` on the record. | real script/CLI + seams | `tests/test_deterministic_plan.py`, `tests/test_deterministic_stats.py` |

## Properties   (REQUIRED)

All Python properties use Hypothesis with `@settings(derandomize=True, max_examples=200, database=None)`; bridge-driven scheduler properties use `max_examples=60` with the same derandomized, database-free settings (each example runs the whole workflow). Every file declares `PROPERTIES = {"P-n": "test_name"}`.

| ID | Requirement | Property | Generator domain | Expected decision |
|---|---|---|---|---|
| P-1 | 1.1 | For any plan answer whose ready contradicts its deps, the script requests exactly one retry with the same inputs before any fallback or adoption; a consistent answer is never retried. Complement of P-2. | `scenarios()` × injected `planSeq`: first answer inconsistent (ready contains a non-ready id, omits a ready id, or lists an unknown id) vs consistent; healthy second answers. Count `plan k` / `plan k retry` labels. | exactly one retry iff first inconsistent |
| P-2 | 1.2 | For any run where the retry is also inconsistent/unusable, the run continues from the deterministic ready list and never halts with a planning stop reason; running tasks still land. Complement of P-1's fallback arm. | `scenarios()` × always-inconsistent `planSeq`, `maxConcurrent` 1–4, red landings, conflicts. | no `planning step failed` stop; merged set = deterministic-oracle merged set |
| P-3 | 1.3 | For any fallback run, `result.degraded is True`, the fallback count equals the number of plan attempts that triggered fallback, and each recorded trigger names a real `plan k`/`plan k retry` label from the trace. | Same draws as P-2. | count matches; triggers ⊆ trace labels |
| P-4 | 1.2, 2.4, NFR-3 | For any acyclic task graph, Done set and exclude set, the deterministic fallback ready list equals `ck plan --json --exclude` `ready` for the same tasks.md: same set, same order (longest remaining chain first, then numeric id). Metamorphic sub-check: reordering keys/dep lists/done/exclude never changes the list. | `dags()`: 1–25 tasks, non-contiguous multi-digit ids (`1,2,9,10,11,100`), empty dep lists, diamonds, long chains, duplicate deps, deps on Done/excluded tasks, dangling deps; Done/exclude any subsets incl. Done ids and absent ids. CLI-driven per example (table-driven over ≥60 draws + Hypothesis seam-side). | equal lists; order-stable |
| P-5 | 1.5, 2.1 | For any deps map, Done/running/merged/excluded sets, no merged, running, failed, or excluded task is ever in the fallback ready list, and scheduling one fallback task never alters another task's record. | `scenarios()` × always-inconsistent plans; exclude = running ∪ failed ∪ merged; running tasks seeded via `planDurations`. | excluded ∩ ready = ∅; records of non-scheduled tasks unchanged |
| P-6 | 2.2 | For any run without `args.deps`, fallback scheduling uses exactly the last good plan's ready list minus tasks finished/excluded since (never script-computed readiness, never a guess); with no good plan yet, no task is scheduled. | `scenarios()` without `args.deps` × `planSeq` shapes: [good, bad, bad], [bad, bad], [good]. | scheduled ⊆ last-good-ready − done/excluded; [bad,bad] schedules nothing |
| P-7 | 2.5 | For any deps map where task t lists a dependency absent from the map, t is never scheduled by fallback — even when that id appears in `done`. Negatives drawn from near-misses in the data source. Complement: every task whose deps are all known-and-Done and which is not excluded IS schedulable when it is the only open task. | Base graphs from `dags()`; unknown dep = max-id+1, a removed task id, or a `done`-only id. | t never scheduled; complement scheduled |
| P-8 | 3.1, 3.3 | For any scenario, with healthy plans, no `plan` request (non-retry) occurs between a task finish and the next `task N` request when the deterministic ready list is non-empty; plan requests are only start / first-after-landing / idle. (Landing-speed P-6 relation, tightened: idle-cause analysis unchanged.) | `scenarios()` with `args.deps`, healthy plans, `maxConcurrent` 1–4. | holds on every trace |
| P-9 | 4.1, 4.2, 4.3, NFR-4 | For any run with W landing batches containing ≥1 unreviewed non-risky task and R risky tasks, review-agent count ≤ W + R (plus re-reviews of flagged tasks); every risky task has its own individual review label; no batch review covers zero tasks. | `scenarios()` × `risk` maps (0–100% safety, incl. all-risky and no-risky waves), `reviews` verdict sequences incl. `changes`. | count ≤ W + R; risky tasks individually reviewed |
| P-10 | 4.4, 4.5 | For any batched review with per-task verdicts, each flagged task gets its own revise + re-review cycle while unflagged wave-mates record approval without extra agents; an all-approve batch records approval for every covered task. | `scenarios()` × wave sizes 1–6 × flagged subsets (none, some, all). | flagged revised exactly; unflagged approved untouched |
| P-11 | 5.2, 5.3 | For any collected run, `show --json` fallback counts and overhead shares equal the text view's parsed numbers and equal `overheadShare(record)` on the same record; runs with plan/review/land agents always show all three shares. | Table-driven FakeClaudeHome runs (plan/review/land mixes, zero-duration agents, missing `startedAt`, pre-change records) × JSON/text comparison. | equal values |
| P-12 | 5.4 | For any overhead share value (0–100%) and any run outcome, flagging the warning never changes scheduling: the flagged run's merged set, stop reason, and agent-request sequence equal the same run with the threshold disabled/at 100%. Metamorphic: warn-only. | `scenarios()` with forced overhead × thresholds {0%, 50%, 100%, absent}. | identical outcomes; warning iff share > threshold |
| P-13 | 5.1, 5.5, 5.6 | For any pre-change record (no fallback/overhead fields), `show` (text+JSON) prints `not recorded` for the new sections and every pre-existing key keeps its old value; re-collection never rewrites the stored record bytes. Complement: any post-change degraded run always shows all three of {degraded, count, triggers}. | 8+ table-driven FakeClaudeHome runs (failed task, revise, red landing, unfinished run, agents without meta) × the committed `pre-deterministic-record.json`. | unchanged / not recorded; degraded runs complete |
| P-14 | 3.2, 3.4 | For any scenario, a freed slot with non-empty deterministic ready starts the next ready task(s) before any `plan` request; an idle re-read that finds nothing ready arms no second re-read until a landing end or task record. | `scenarios()` with freed-slot timing (`planDurations`, slow tasks) and empty-ready draws. | start-before-plan; single idle plan per arming |

## Harness Contract

Interfaces the tests rely on; the implementation must provide them (names may differ if the test-task updates all call sites consistently, but the shapes must hold):

1. **Deterministic fallback seam** (new; pure, extracted like `readyTasks`, testable via `loadFunction`/`bridge {op:'ready'}`):
   `fallbackReady({ order, deps, done, exclude }) -> string[]` — same contract as `readyTasks` (chain-length order, unknown deps never met). If the implementation reuses `readyTasks` directly, that reuse plus TC-5/TC-35 satisfies this item; the property table then targets `readyTasks`.
2. **Plan-validity seam** (exists as `planConsistent(plan, exclude) -> bool`; keep its signature): inconsistent = ready ≠ script-computed ready (minus stricter-rule holds) or any `remaining` task lacking a `deps` entry. P-1/P-2 call it directly; TC-35 shows the script calls it (script behaviour agrees with the seam on the TC-6 dangling-dep scenario).
3. **Review-batching seam** (new; pure): `batchGroups(mergedWave: string[], risk: Record<string,string>) -> { batched: string[], individual: string[] }` — `individual` = tasks with `risk === 'safety'` (case-insensitive); `batched` = the rest (empty ⇒ no combined review). P-9/P-10 target it; TC-17–TC-19 show the script calls it (one review label per `batched` group, one per `individual` task).
4. **Overhead seams** (new; pure, in `claude_kiro.analytics` next to `queue_summary`/`wait_split_summary`):
   - `overhead_share(record) -> {plan: float|None, review: float|None, land: float|None, fallbackCount: int, triggers: string[]}` — shares of agent time (ms) over agents present; `None` (→ `not recorded`) when the record predates the data.
   - `overhead_warning(share, threshold) -> bool` — pure threshold compare; the workflow and `ck stats show` call it, and **nothing** halts/fails/retries on its result (P-12).
   - `ck stats show` text + `--json` carry the same numbers (P-11); pre-change records render `not recorded` (P-13).
5. **Bridge/fake-project options** (test-only, additive): `planSeq` / `badReady`, `risk`, `planDurations` (exist), review-task ledger `state.reviewTasks`. The bridge forwards unknown `project`/`args` keys unchanged and always passes `args.agentSlots = slots` when `slots` is set.
6. **Label grammar** (unchanged from landing-speed, extended): `plan N`, `plan N retry`, `task N`, `review N` (individual), one batched-review label per wave in the form `review-wave <wave> tasks <ids>` (e.g. `review-wave 2 tasks 4 5 7`), which `roles.py` maps to role `reviewer` with the wave number and covered task ids — the NFR-4 count and the overhead reviewer share count it as a review agent — plus `land …`, `report`. Tests parse wave coverage from `state.reviewTasks`/prompts, not from the label text. Result fields: `degraded: bool`, `planFallbacks: {count, triggers[]}` (exact field names are the implementation's choice within D6; tests read them via a single accessor helper so a rename touches one place).
7. **Fixture run contract**: the Story-6 fixture exposes (a) a forced-inconsistent plan mode, (b) a healthy mode, (c) an assertion step (CI) checking completion + `degraded` + fallback count ≥ 1 (TC-31) vs clean completion (TC-32).

## Critical Paths

1. **Survive a lying planner** (Stories 1–2): start plan inconsistent → exactly one retry (TC-1, P-1) → retry inconsistent → schedule from `args.deps`-computed ready (TC-2, TC-5, P-2) or last-good-ready without deps (TC-6, P-6), never guessing unknown deps (TC-9, P-7) → run completes degraded with count + triggers (TC-2, P-3) → BUSY/blocked reports handled as usual (TC-10).
2. **No planner when nothing changed** (Story 3): start plan → freed slots start ready tasks directly (TC-12, P-14) → no plan after every freed slot (TC-13, P-8) → landing triggers the re-read (TC-11) → empty idle re-read disarms until landing/record (TC-14) → consistent plans still replace the view (TC-15) → dep-less runs still plan per freed slot (TC-16).
3. **Cheaper reviews, same risk** (Story 4): wave merges → one combined review for non-risky tasks (TC-17) → risky tasks always individual (TC-18, TC-19) → flagged tasks revised alone (TC-20) → approvals recorded per task (TC-21) → no empty reviews (TC-22); bound W + R holds (P-9, TC-30).
4. **Provable saving** (Stories 5–6): degraded runs report fallbacks + triggers (TC-23) → overhead share beside fallback counts in text and JSON, equal (TC-24, TC-25, P-11) → over-threshold warns without halting (TC-26, P-12) → old records show `not recorded`, unchanged (TC-27, TC-28, P-13) → fixture proves the fallback in CI (TC-31, TC-32) → equity re-run shows the lower share (TC-33).

## Commands

Python suite (from repo root; the verify command this spec's own fixture uses):

```bash
uv run pytest -q -n auto
```

Focused files:

```bash
uv run pytest -q tests/test_deterministic_plan.py        # P-1..P-10, P-12, P-14 + TC-8/29/30/35 (Verify: bridge properties)
uv run pytest -q tests/test_deterministic_stats.py       # TC-23..28, P-11, P-13 (Verify: stats subprocesses)
uv run pytest -q tests/test_workflow_scripts.py          # runs tests/js/deterministic_plan.test.mjs + all existing Node suites (Verify: node suites green)
uv run pytest -q tests/test_plugin.py                    # TC-34 vendored parity (Verify: copies match)
uv run pytest -q tests/test_landing_readiness.py tests/test_landing_scheduler_properties.py  # regression: readiness oracle + scheduler relations
```

Node suites directly (from repo root):

```bash
node tests/js/deterministic_plan.test.mjs                # TC-1/2/4..7/9..22/26 (Verify: ok, no FAIL)
```

OpenCode path (from `opencode/`):

```bash
node --test test/*.test.ts                               # TC-34 workflow-tool validation (Verify: all pass)
node --test test/workflow-tool.test.ts                   # focused re-run
```

Manual headline (TC-33, not CI): run the equity workload once, then `ck stats show <baseline> --json` vs `ck stats show <rerun> --json` and compare plan/review/landing agent shares plus `plan_fallbacks`.

## Not Tested

- ck issues **#6** (verify-cycle recheck) and **#8** (blocked:test handoff): out of scope per PLAN.md/requirements; existing verify-cycle tests stay green but no new coverage.
- `/spec:create` oracle gaps and models.dev catalog pricing: explicitly out of scope.
- Gate, guard, escalation, landing-batch, red-batch bisection, and tiered-gate semantics: preserved, not re-decided; covered by existing suites (`test_gate*.py`, `test_guard*.py`, landing-speed suites) run as regression only.
- The equity re-run's exact saving number (TC-33): measured manually once (~$7/80 min); asserting a numeric threshold in CI would be flaky and expensive, so CI gates on the fixture (TC-31/32) and the W + R review bound (P-9) instead.
- Overhead-halt behaviour: there is none by design (D8 warn-only); P-12 proves warnings never halt rather than testing halt tuning.
- Review quality of batched vs individual reviews (whether a combined review catches the same findings): a human/adversarial-review concern per PLAN.md risk 2, not automatable here; automation covers routing, coverage, and per-task verdict recording only.
