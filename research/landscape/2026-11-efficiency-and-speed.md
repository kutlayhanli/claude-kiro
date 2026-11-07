# Making claude-kiro Faster and Cheaper: What Similar Harnesses Do, and What ck's Own Runs Show

> **Research conducted:** 2026-11-07 | **Sources consulted:** ~105 (web) + ck run transcripts | **Depth:** Deep
> **Scope:** efficiency and implementation speed only. The October study (`2026-10-spec-driven-and-autonomous-loops.md`) already covers the SDD landscape (Kiro, Spec Kit, OpenSpec, BMAD, Tessl) and autonomous-loop safety; this report does not repeat it.
> **Grounding:** ck 0.11.0 at `8fab5f5`; Claude Code 2.1.319; `ck stats show` over 96 collected spec-implement and 14 spec-create runs; transcript analysis of `wf_8bca74f5-ecc` (41-task run, 2026-10-28, 208 agents); `plugin/workflows/spec-implement.js`; `specs/{landing-speed,deterministic-plan,prompt-free-runs,run-followups}/PLAN.md`.

---

## Executive Summary

The biggest savings available to ck are not in model choice or task logic. They are in **what every agent is made to load before it does anything**.

- Measured in this session:
  - In the 41-task run, **cache writes were 51% of spend**, against 22% for cache reads and 27% for output, even though reads are over 90% of tokens.
  - **44% of those writes happen on each agent's first request** (about 24k tokens × 208 agents).
  - Of that startup load, about 15k tokens per agent are a skill listing (sales, legal, finance and other plugins) and a deferred MCP tool listing (QuickBooks, Gmail, Slack and so on) that no ck agent uses.
  - This is because `spec-implement.js` never passes `agentType`: every implementer, reviewer and mechanical agent runs as a default subagent and inherits the whole session environment.
  - Fixing this needs only ck changes (dedicated agent types with restricted tools and `omitClaudeMd`), plus disabling irrelevant plugins for ck projects.
  - **Expected saving: 15-25% of run cost, plus faster agent startup.**
- **A second free lever is concurrency.** The Workflow runtime caps agents at min(16, CPUs−2), which is 14 on this 16-CPU machine. That is exactly the peak of 14 the landing-speed analysis measured, and the cause of the 77 s median reviewer queue. `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` (v2.1.269) raises it, because the work is limited by inference rather than CPU.
- **Across the field, the published harnesses that scaled furthest keep LLMs out of coordination.**
  - Cursor saw 20 agents produce the throughput of 1-3 under locks. It removed its integrator and its judge [O3][O4].
  - Anthropic's 16-agent C compiler had no orchestrator agent [O5].
  - Factory's orchestrator used 2.3% of runtime, and Factory validates per milestone rather than per feature [O8].
  - A controlled study found deterministic orchestration used up to 3.5× fewer tokens than LLM orchestration, with comparable accuracy [O9].
  - Claude Code's new **mods** API (`$.process.run`, `$.tool.register`, `turn.step`) is the first in-harness way to run `ck` deterministically without an LLM turn [P13-P15].
- **Review is ck's largest cost share: 36% of spend across 96 runs.** The evidence supports three changes:
  - tiering review by risk and diff size (AI review F1 falls from 0.66 on diffs under 10 lines to 0.04 on diffs over 150 lines [V9]; small PRs average 0.5 findings [V6]);
  - running deterministic contract checks before review;
  - starting review speculatively, in parallel with the gate.
- **On task shape:** the evidence favors fewer, coherent tasks of about 30-90 human-minutes [E12][E14], interface-first "wave 0" contracts [V25-V27][E18], and fresh attempts over patch-up loops [O11]. No study shows that a separate test-writing agent pays for itself; tests that serve as the spec do [E15][E16].
- **Spec creation is a hidden cost.** Recent `/spec:create` runs cost $26-38 and 58-81 min each, run entirely on Opus, and mostly in sequence. Kiro's Quick Plan, BMAD's Quick Flow and OpenSpec's `ff` show the field moving to one-pass flows for small changes [E17][E26][E27].

---

## Table of Contents

1. What ck's own runs say (new measurements)
2. How comparable harnesses handle coordination
3. Platform levers added since October (Claude Code 2.1.240 to 2.1.319)
4. Verification and merging: review, gates, landing
5. Context, task size and model choice
6. Spec creation speed
7. Ranked recommendations
8. Open questions and limitations
9. Sources

---

## 1. What ck's own runs say

### 1.1 Aggregate (`ck stats show`, 96 spec-implement runs)

- **Cost per merged task:** $3.87. Wall time is 23.5 min per task (run wall time divided by task count).
- **Cost share by role:**

  | Role | Share of cost |
  |---|---|
  | Reviewer | 36% |
  | Implementer | 29% |
  | Test-writer | 25% |
  | Plan | 18% |
  | Land | 10% |
  | Gate | 7% |
  | Fixer | 5% |

  The shares overlap and the plan share predates `deterministic-plan`.
- **Most runs end "blocked"** with 1-8 tasks failed, and every dependent task is stranded. The long tail decides whether a spec finishes, not the average.
- **spec-create costs as much as a mid-size implement run:**
  - Six recent runs cost $23-38 and took 58-81 min each.
  - `wf_2a55e2f7-b3c` used 19 agents, all Opus, for $38.02. Its phases ran in sequence:

    | Phase | Time |
    |---|---|
    | Requirements | 4.6 min |
    | Design + Test Plan | 28 min |
    | Task Tracks | 11 min |
    | Link | 12 min |
    | Adversarial Review | 30 min |
    | Revise | 19.5 min |
    | Consistency | 14 min |

### 1.2 Where the money goes inside a run (transcripts of `wf_8bca74f5-ecc`)

Measured from the 208 agent transcripts, deduplicated by `requestId` and priced at 5.5-generation list prices (models: Sonnet 5.5 for 1,164 requests and Opus 5.5 for 874):

| Component | $ | Share |
|---|---|---|
| Cache **writes** | 40.39 | **51.2%** |
| Output | 21.02 | 26.7% |
| Cache reads | 17.39 | 22.1% |
| Uncached input | 0.01 | 0.0% |

Cache writes split as follows:
- **First requests: 5.06M of 11.54M written tokens (44%).** The median first request writes about 25k and reads only about 8k.
- **The rest comes from growing conversations,** where each tool result is written once.

So only the base system prompt and tools are shared between agents. Everything after them is paid at the 1.25× write rate for every agent.

**What is in an agent's first request.** These are the attachment sizes before the first model call, from one implementer transcript:

| Attachment | Size | Approx. tokens | Needed by a ck agent? |
|---|---|---|---|
| `skill_listing` | 37.8 KB | ~9.5k | No: about 150 skills (sales, legal, finance, marketing, Cloudflare, Higgsfield…) |
| `deferred_tools_delta` | 23.6 KB | ~6k | No: QuickBooks, Gmail, Slack, Chrome DevTools, DataForSEO, Clarity… |
| `instructions` (CLAUDE.md hierarchy + RTK + VISION) | 8.9 KB | ~2.2k | Partly; [E1] finds such files often cost more than they help |
| `sandbox_instructions` | 5.4 KB | ~1.3k | Yes, under `ck run` |
| task prompt | 4.5 KB | ~1.1k | Yes |
| others (environment, session context, snapshot) | ~4 KB | ~1k | Mostly |

Why this happens: `spec-implement.js` never passes `agentType` (`grep agentType` finds nothing). Implementers, reviewers and mechanical agents therefore all run as the default workflow subagent, with only `{model, effort}` set (`LOW = { effort: 'low', ...ORCH }`, line ~121). The `plugin/agents/*.md` definitions (`reviewer`, `implementer` and the rest) are not used by the Workflow path.

**Plan agents** (62 of them in this run) made about 2 requests each, and 35% of their tokens were cache writes. They were almost pure startup cost. `deterministic-plan` has since removed most of them.

### 1.3 Concurrency is capped by CPU count, not by design

The Workflow runtime runs "up to 16 concurrent agents … fewer when Claude Code has fewer CPUs". The bundled skill gives this as min(16, CPUs−2) [P2][P3].
- This machine has 16 CPUs, so the cap is **14**. That is the exact peak landing-speed measured.
- That cap is what produced the review queue: 62 reviewers waited a median 77 s (p90 154 s), 77 agent-minutes in total.
- `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` (1-256, v2.1.269) lifts it. The string is present in the installed 2.1.319 binary.

---

## 2. How comparable harnesses handle coordination

| System | Coordination | Isolation | Integration | Reported numbers |
|---|---|---|---|---|
| **Cursor research harness** [O3][O4] | Recursive planners make tasks; workers take one each and return a handoff note | Repo copy per worker | Workers resolve their own conflicts; integrator and judge **removed**; per-commit green dropped | 20 agents under locks ran at the throughput of 1-3; ~1,000 commits/h at peak; disk I/O became the bottleneck |
| **Anthropic C compiler** [O5] | No orchestrator; claim a task by committing a lock file (git rejects the second claimant) | Docker + shared bare repo | Agents pull, merge and push themselves | 16 agents, ~2,000 sessions, ~$20k; test runner prints one grep-able `ERROR` line; `--fast` per-agent 1-10% test sample |
| **Factory Missions** [O8] | Orchestrator writes a **validation contract** (assertion IDs) first, then dispatches | Per-worker | Serial features; validators **per milestone**; findings become fix features | Orchestration 2.3% of runtime, implementation 60.5%, validation 37.2%; 95.7% of tokens were cache reads; 34% of features were fixes |
| **Gas Town + Beads** [O2][O19] | Mayor groups beads into convoys; `bd ready` is a query, not an inference; a config-driven scheduler caps workers | Worktree per worker | **Refinery** daemon: batch, gate, bisect, merge (close to ck's landing) | "20-30 agents comfortably"; no cost data |
| **Claude Code agent teams** [O1] | File-locked shared task list; dependencies unblock automatically | Optional worktree | Manual | Docs recommend 3-5 teammates with 5-6 tasks each; ~7× tokens [P7]; interactive only |
| **Kiro 0.12** [E17][O16] | Task graph planned up front; "Run all Tasks" runs dependency waves in parallel with a barrier per wave | Subagents | n/a | "Up to 4× faster" for 4+ independent tasks (vendor) |
| **OpenHands refactor SDK** [O15] | Static dependency analysis; **leaf nodes first** | Docker | Per-commit tasks | ~5 agents now, aiming for dozens |
| **Devin MultiDevin** [O14] | Manager + up to 10 workers; the manager reads child trajectories | VM per child | Manager merges successes and drops failures | none |
| **Codex cloud / Cursor 2.x** [O13][O20] | Best-of-N (1-4 attempts / up to 8 agents) | Container / worktree | **Manual** pick | Attempts often come out identical unless the prompt asks for diversity |
| Conductor, Claude Squad, Vibe Kanban, Crystal/Nimbalyst, Sculptor, container-use [O21][O22] | Human | Worktree or container | Human diff/merge UI | Nothing to borrow for automation |

Patterns across the systems that scaled:

1. **Code is the skeleton; LLMs are called at bounded decision points** [O3][O5][O8][O9]. The opposite pattern has a measured cost: in one vendor run, the LLM integrator cost $6.37 while all other roles together cost $3.67 [O7].
2. **Whole-run validation instead of per-unit perfection.** Cursor accepts a small error rate between full gates [O4]; Factory validates per milestone [O8]. ck already gates in full every N merges and at the end, so it can thin the per-batch work further.
3. **Lock-free or optimistic claiming.** Cursor's lock-based claiming collapsed throughput [O3]. ck claims through git worktree locks and flock, which is fine at 14 agents; the lesson is never to make agents *wait* on a lock.
4. **Prevent conflicts by giving each task a disjoint file scope** rather than resolving them afterwards. Agent PRs conflict textually 19.8% of the time within one agent and 41.7% across agents, and 84% of conflicts are in source files [O6].

---

## 3. Platform levers added since October

All verified against the 2.1.319 changelog and docs [P1-P16]; the setting names are confirmed present in the installed binary.

| Lever | Version | What it does for ck |
|---|---|---|
| `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` | 2.1.269 | Removes the CPU-derived 14-slot cap |
| `agent({agentType})` + subagent frontmatter (`tools`, `model`, `effort`, `maxTurns`, `skills`, **`omitClaudeMd`** (2.1.271), `experimental.cacheTtl`) | — | Per-role context that drops the session's skill/MCP/CLAUDE.md load; a custom agent replaces the full Claude Code system prompt with its own body [P4] |
| Prefix sharing in a fan-out | — | Siblings with the same model, effort, agent type, tools, schema **and cwd** share a cache; staggered by `CLAUDE_CODE_WORKFLOW_PREFIX_STAGGER_MS` [P2][P5] |
| `subagentPromptCacheTtl: "1h"` | — | Workflow agents default to a 5 min TTL; 1 h writes cost 2× base vs 1.25× [P5][P16] |
| **Mods** API: `$.process.run`, `$.fs`, `$.tool.register`, `agent.spawn` hook, `turn.step` hook with full `usage` | 2.1.290-2.1.300 | Deterministic `ck` calls without an LLM; exact per-agent cache-write/read accounting; rewriting a workflow agent's model or prompt at spawn [P13-P15] |
| `SubagentStop` hook, exit 2 = keep working | fixed 2.1.311 | Run `ck gate` when an implementer stops and push failures back into the *same* warm context [P6] |
| `permissions.denyInsteadOfPrompting` | 2.1.319 | Unattended runs never stall on a prompt |
| `CLAUDE_CODE_SUBAGENT_EFFORT`, per-model saved effort | 2.1.316/317 | Keep ck's explicit `effort:` fields, or a model switch silently changes effort |
| `--bare`, `--exclude-dynamic-system-prompt-sections`, `--json-schema`, `--max-budget-usd` | — | Building blocks for an external orchestrator; `--bare` needs API-key billing [P8][P9] |

Still true:
- Workflow scripts cannot run a shell or touch the filesystem [P2].
- `budget.spent()` counts output tokens only [P3], one cause of the `workflowProgress` undercount.
- Plugin subagents ignore `hooks`, `mcpServers` and `permissionMode` in frontmatter [P4], so ck's hooks must stay in the plugin's `hooks.json`.

**Pricing facts that matter** [P16]:
- On the 5.5 generation, cache reads cost 0.05× base, so cache writes (1.25×) are the input cost. This is why writes reach 51% in §1.2.
- Haiku 5.5 has a per-request 5× price step above 100K prompt tokens, cache hits included. ck's sessions crossed it on 37% of Haiku requests.

---

## 4. Verification and merging

### 4.1 Review

- **What AI review catches.**
  - The best tools catch about half of the bugs humans later fix. The Martian Code Review Bench, the closest thing to an independent benchmark, puts the top tools at F1 ≈ 58-61% and recall ≈ 50% [V7].
  - Industrial acceptance rates run 19-42% [V11].
- **Size dominates.** F1 fell from 0.657 on diffs under 10 lines to 0.043 on diffs over 150 lines [V9] (a single study). Anthropic's own Code Review found findings on 31% of PRs under 50 lines, averaging 0.5 issues each, against 84% of PRs over 1,000 lines, averaging 7.5 [V6].
- **Same-family judges share blind spots,** and every judge tested was swayed by surface cues such as claims of correctness [V12][V21][V22].
- **The recommended architecture is executable specs first, deterministic checks second, and an LLM for what is left** [V12][V23]. In a Tencent study, static analysis followed by LLM triage removed 94-98% of false positives [V23].
- **Precision tricks that work:** majority voting across passes (Bugbot [V10]) and a verification step that filters false positives (Anthropic, under 1% of findings marked incorrect [V6]).
- **Factory and Jules move review out of the per-unit path.** Factory validates per milestone [O8]; Jules runs a critic inside the worker [O18].

ck's own evidence points the same way:
- The reviewer is 36% of cost, about 2.3 min per task, and adds about 11 min to a 7-task run.
- It did correctly block 3 of 7 tasks in one benchmark.
- It missed the field-name drift between test files, which is a deterministic-check problem rather than a judgment problem.

### 4.2 Landing

- **Uber SubmitQueue** speculates on likely build orders and treats changes touching disjoint build targets as independent. Result: P95 wait down 32-45% and CPU down 35-52% [V1] (Uber only).
- **Zuul** tests each change on the assumption that everything ahead of it merges, and adapts its window like TCP: +1 on success, halved on failure [V2].
- **Trunk and Mergify** move failed batches to a bisection queue, reuse results for combinations they have already seen, and treat frequent bisection as a sign batches are too large [V4][V5].
- **Flakes:** at Google, 84% of transitions from pass to fail were flakes [V20]. Rerun once before bisecting.

### 4.3 Gate time

- **Test selection:**
  - Meta's predictive test selection catches over 99.9% of faulty changes at about half the testing cost [V15].
  - pytest-testmon reruns only the tests whose covered code changed [V17].
  - Bazel caches only passing results, keyed by input hash [V19].
  - ck already stamps a passing suite by tree hash (`c0e2f8a`). The remaining step is to use per-test coverage selection for intermediate batch gates.
- **Agent-facing test output** (Anthropic C compiler [O5]): print a few lines, write the detail to a file, put `ERROR <reason>` on one grep-able line, and offer a deterministic `--fast` sample per agent.
- **Masking old tool output** halves agent cost with no loss in solve rate [E5]. In ck, test and gate output is the bulk of the conversation-growth writes (56% of writes, §1.2).

---

## 5. Context, task size and model choice

- **Context files.** AGENTS.md-style files did not generally raise success and added over 20% cost; LLM-written ones slightly *hurt* [E1]. Each ck task agent currently inherits the user-global CLAUDE.md, RTK.md and VISION.md (§1.2).
- **Startup overhead.** Community measurements put subagent startup at 5-54k tokens [E3]. ck measures about 24k written plus 8k read per agent.
- **Search.** Agentic grep is fine at ck's typical repository sizes. Semantic search helps mainly above 1,000 files [E8][E9]. A small repo map (Aider) is optional [E10].
- **Task size.**
  - METR's 80%-success time horizon for frontier models is tens of minutes to hours of human work [E12][E13].
  - Success falls with files touched (18% for 1-2 files vs 2% for 7 or more) and with how much code must be read, not with how the work is labeled [E14].
  - Agent-teams docs and OpenHands both warn that tasks too small to justify their own commit lose more to overhead than they gain [O1][O15].
  - Each ck agent pays about 25k tokens of startup writes, plus a review and a landing.
- **Test-first.** No significant outcome change from prompting agents to write more or fewer tests [E15]. Tests supplied *as the spec* drive high resolution rates [E16]. No study isolates "separate test agent" against "same agent, red then green".
- **Interface first.** Shared contracts frozen before parallel work (wave 0) and single-owner files are the consistent practitioner advice [V25-V27][E18]. CooperBench (unverified) reports about 30% lower success when two agents must collaborate [E18].
- **Models and effort.**
  - The harness moves results by 5-20+ points with the model held fixed [E19][E20].
  - Effort beyond medium buys about 1 pp [E23].
  - A "light model first" cascade pays only if the light tier's pass rate exceeds the cost ratio, and switching models mid-session breaks the cache [E6][E22].
  - All three match ck's own findings ("higher effort never paid off"; Haiku takes 2.3× the turns).
- **Retries.** A placebo-controlled study found fresh whole-solution attempts beat fault-localized repair 40-3 [O11] (small benchmarks, via a blog summary). Deterministic consensus selection among N candidates reached 96% of the best-of-N ceiling [O12].

---

## 6. Spec creation speed

- **What the field ships:**
  - **Kiro 0.12:** Quick Plan, one pass with no approval gates, plus parallel waves [E17].
  - **BMAD v6:** Quick Flow, one tech-spec for 1-15 stories, escalating to the full method on signals that the work spans several components [E26].
  - **OpenSpec:** `/opsx:ff`, about 250 lines of output vs about 800 for Spec Kit [E27].
- **Critiques:**
  - A bug fix that became 4 user stories with 16 acceptance criteria [E29].
  - Spec Kit about 10× slower than iterative prompting, a secondary figure [E30].
- **ck's position.** ck's `/spec:create` runs about 60-80 min and costs $26-38 on Opus regardless of change size. Its adversarial review plus revise is about 50 min of that.

---

## 7. Ranked recommendations

"Est." figures are estimates from the measurements above, not measured results.

### Tier 1: cheap, measured, available now

1. **Give every workflow role a dedicated agent type and pass `agentType`.**
   - Roles: implementer, test-writer, reviewer, fixer, resolver, and a new `ck-runner` for plan, land and gate.
   - Frontmatter: `tools:` restricted to what the role needs, which drops the deferred MCP listing; `skills: []`, or no skills, which drops the about 9.5k-token skill listing; `omitClaudeMd: true` with the project's real conventions passed in the prompt; explicit `model`, `effort` and `maxTurns`.
   - Put the stable part of the brief in the agent body (its system prompt) so it is cached once per role, not read through a tool per agent.
   - *Est.:*
     - Startup writes per agent fall from about 25k to under 8k tokens.
     - About 3.5M fewer write tokens across a 208-agent run, roughly $9-12 (12-15%) of the $79 run, plus faster first turns.
     - Haiku agents stay under the 100K price step more often.
   - *Check first:* the exact `agentType` name for plugin agents (namespacing such as `spec:implementer`) in vendored and plugin modes, and that the sandbox instructions still arrive.
2. **Turn off unrelated plugins and MCP servers in ck projects.** Set `enabledPlugins: {"sales@…": false, …}` in the project `.claude/settings.json`, or have `ck doctor` warn when the session's skill or MCP listing exceeds a threshold. This helps the main session as well as every agent, and works even before item 1 lands.
3. **Raise the workflow concurrency cap.** Have `ck run` (and the implement skill's instructions) set `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` to 24-32, alongside the existing `max_concurrency` in specs/ck.json. *Est.:* removes the 77 s median and 154 s p90 reviewer queue, about 77 agent-minutes per 41-task run. Watch API rate limits and RAM, since each agent is a process.
4. **Terse, agent-facing gate output.** Make `ck gate` print a short summary and one `FAIL <test> <first error line>` per failure, and write full logs to a file the agent can read on demand. This is the C-compiler pattern [O5]; masking alone halves cost [E5]. *Est.:* cuts a large share of the conversation-growth cache writes (56% of writes) on tasks with many gate runs.
5. **Unify the mechanical agents' prefix.** Give plan, land and gate the same `agentType`, model, effort, tools and output schema (`{ok, json}`), so siblings share one cache [P2][P5].

### Tier 2: moderate changes, large effect

6. **Tier and speculate review.**
   - (a) Skip the LLM reviewer for diffs under about 50 lines, test-only diffs, and docs or config changes that pass the contract check. Keep Sonnet for normal tasks and Opus for `Risk: safety`.
   - (b) Start the reviewer when the implementer commits, in parallel with the gate. Discard its result if the gate is red.
   - (c) Require every blocking finding to cite a requirement or design clause plus a line, so blocks are verifiable.
   - (d) Add one interface-only review per wave, over the public-surface diff of all the wave's tasks, to catch drift between tasks.
   - Basis: [V6][V9][V1][O8].
   - *Est.:* the reviewer is 36% of cost; skipping 30-50% of reviews saves 10-18% of run cost; speculation removes about 2 min from each task's path to merge.
7. **Deterministic contract artifacts from `/spec:create`.** Emit a machine-readable contracts file plus typed stubs (fields, enums, signatures) from design.md, have tests import them, and add `ck lint --contracts` / gate checks. This removes the field-name drift class without a reviewer [V25-V27][V12]. It is also the "wave 0" that lets tasks fan out wider [E17][E18].
8. **Gate inside the implementer with a `SubagentStop` hook.** A plugin `hooks.json` `SubagentStop` hook (matcher: implementer and test-writer types) runs `ck gate --task`. On failure it exits 2 with the terse failures, and the same agent continues with a warm cache instead of a new retry agent paying about 25k of startup writes [P6]. Keep the fresh-attempt rule for the *second* failure, carrying a short failure note [O11].
9. **Smarter landing.**
   - Rerun failing tests once before bisecting [V20].
   - Let tasks with disjoint file and test scopes land independently, outside the serial batch order [V1][V13].
   - Size the batch window adaptively: +1 on green, halved on red [V2].
   - Use coverage-selected tests (testmon-style) for intermediate batch gates, keeping full gates every N merges and at the end [V15][V17].
10. **Reshape tasks at spec time.**
    - Declare each task's write-set (`Files:` already exists), and have `ck plan` refuse to run tasks with overlapping write-sets at the same time [O6].
    - Bundle chains of tiny tasks into one agent [O1][O15].
    - Target 1-3 files and about 30-90 human-minutes per task [E12][E14].
    - Let a test task and its impl task merge into one red-then-green agent when the test isn't a shared contract [E15][E16]. Gate evidence: the tests' red commit precedes the impl commit.
    - Order dispatch by longest remaining dependency chain (critical path first).
11. **Cheaper and faster `/spec:create`.**
    - (a) Route phases by need: Sonnet for Link, Task Tracks and the consistency pass; Opus for design and adversarial review.
    - (b) Add a `--quick` path for small changes: one-pass requirements, design and tasks, a single review lens, under a size threshold, the way Kiro Quick Plan and BMAD Quick Flow work [E17][E26].
    - (c) Run the Link and Task Tracks work alongside the review where the dependencies allow.
    - *Est.:* `/spec:create` for small changes drops from about 70 min and $30 to about 10-20 min.

### Tier 3: bigger bets

12. **A ck mod for exact accounting.** Add a `turn.step` hook that appends `{agentId, model, usage}` (cache write and read included) to a ck file [P15]. This replaces `workflowProgress` and `budget.spent()` (output tokens only) and makes cache writes, the real cost, visible in `ck stats`.
13. **A ck mod tool, `mcp__ck__run`.** It wraps allowlisted `ck` subcommands through `$.process.run` [P14], so mechanical steps need one small tool call and never touch the Bash classifier. A speculative variant has `turn.step` answer `ck-runner` agents without calling the model (zero tokens). The response shape for that is undocumented, so keep it behind a flag.
14. **Keep the external orchestrator as plan B.** A Python `ck run` driving `claude -p --agent … --json-schema … --exclude-dynamic-system-prompt-sections` per task, with `ck` as subprocesses [P8][P9][P10]. Choose it only if 1, 12 and 13 don't close the gap: it loses the `/workflows` UI, resume and prefix staggering.
15. **Best-of-2 only on critical-path stragglers.** Start a second, diversified attempt when a critical-path task exceeds its expected turn count, and take whichever passes the gate first [O12][E24].
16. **A harness eval loop.** Extend `ck bench` into a fixed replay set of 10-20 tasks that measures passes per dollar and per minute for each change above. The harness, not the model, is the 5-20 point lever [E19][E20]. Record cache-write share as a first-class metric.

### Not recommended (evidence against or weak)

- **A vector or semantic index:** little gain at ck's repository sizes [E8][E9].
- **Higher effort for implementers:** no measurable gain [E23]; ck's own benchmark agrees.
- **Agent teams as the execution model:** about 7× tokens and interactive only [P7].
- **A Haiku-first cascade for every task:** only pays where Haiku's first-try pass rate beats the cost ratio [E22]. Route by task features instead.
- **The 1-hour cache TTL by default:** most cost comes from startup writes, which a longer TTL doesn't avoid across *different* agents. Test it only for agents restarted on the same prefix more than 5 min apart.

---

## 8. Open questions and limitations

- The cache-write analysis covers **one** run (`wf_8bca74f5-ecc`). The startup attachment breakdown comes from one implementer transcript and could differ under `ck run` (sandboxed) or in other projects. Re-run `research/landscape/2026-11-cache-analysis.py` on 3-5 recent runs before committing to the estimates.
- I have not verified whether workflow agents with `agentType` still receive `skill_listing` and `deferred_tools_delta` when `tools:` and `skills:` are restricted. This is the key assumption behind recommendation 1. A 2-agent test workflow would settle it.
- Uber, Factory, Cursor and Anthropic numbers are self-reported. The AI-review benchmarks except Martian's are vendor-run. Several findings came from search summaries or blog write-ups of papers, notably [O6], [O11], [O12], [E1] and [E30]. Treat them as directional.
- The single-study finding that Haiku 4.5 beat Sonnet 4.6 at review [V9] contradicts ck's measured Haiku leniency (7/10 approvals). Do not generalize it without a seeded-bug A/B test.
- Kiro 0.12's release date and some of its claims (4× speedup) are unconfirmed.
- Per-role cost shares in `ck stats` overlap and predate `deterministic-plan`. Re-measure after 0.11.

---

## 9. Sources

Prefixes: **O** orchestrators, **P** platform, **V** verification/merging, **E** efficiency/granularity.

**Orchestrators**
- O1. Claude Code agent teams docs, https://code.claude.com/docs/en/agent-teams (accessed 2026-11-07)
- O2. Gas Town README, https://github.com/gastownhall/gastown (2026)
- O3. Cursor, "Scaling agents", https://cursor.com/blog/scaling-agents (2026-01-14)
- O4. Cursor, "Self-driving codebases", https://cursor.com/blog/self-driving-codebases (2026-02-05)
- O5. Anthropic, "Building a C compiler with parallel Claudes", https://www.anthropic.com/engineering/building-c-compiler (2026-02-05)
- O6. Summary of arXiv:2607.04697 (agent PR merge conflicts), https://codex.danielvaughan.com/2026/07/28/agent-pr-merge-conflicts-concurrent-coding-agents-codex-cli-worktree-isolation-coordination-defence/ (2026-07-28; blog)
- O7. MindStudio, sandbox pricing / integrator cost run, https://www.mindstudio.ai/blog/upstash-sandbox-pricing-ai-agents (2026; vendor, single run)
- O8. Factory, "Missions architecture", https://factory.com/news/missions-architecture (2026-04-10; self-reported)
- O9. arXiv:2605.09894, deterministic vs LLM orchestration (2026-05; abstract only)
- O11. Summary of arXiv:2609.00854 (fresh attempt vs localized repair), https://codex.danielvaughan.com/2026/09/08/fault-localization-vs-fresh-attempt-placebo-controlled-code-repair-codex-cli/ (2026-09-08)
- O12. Summary of arXiv:2607.01597 PatchFusion, https://codex.danielvaughan.com/2026/07/11/patchfusion-pass-k-pass-1-deterministic-patch-selection-codex-cli-multi-attempt-ensemble-workflows/ (2026-07-11)
- O13. Codex cloud best-of-N, https://codex.danielvaughan.com/2026/04/01/codex-cloud-exec-best-of-n-attempts/ (2026-04-01; snippet)
- O14. Devin MultiDevin docs and "Devin can manage Devins", https://docs.devin.ai/working-with-teams/multidevin, https://cognition.com/blog/devin-can-now-manage-devins (2026-03; snippets)
- O15. OpenHands, parallel refactors, https://openhands.dev/blog/automating-massive-refactors-with-parallel-agents (snippet)
- O16. Kiro subagents docs, https://kiro.dev/docs/cli/chat/subagents/ ; SiliconANGLE on Kiro Crew (2026-08-04)
- O18. Jules changelog, https://jules.google/docs/changelog (snippet)
- O19. Beads, https://github.com/steveyegge/beads (snippet)
- O20. SD Times, Cursor 2.0 parallel agents (2025-10; snippet)
- O21. Multi-agent tool comparisons, https://agentsroom.dev/blog/best-multi-agent-coding-tools (vendor-biased)
- O22. container-use, https://container-use.com/introduction ; Imbue Sculptor docs (snippets)

**Platform**
- P1. Claude Code CHANGELOG, https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md (to v2.1.319, 2026-11-06)
- P2. Dynamic workflows docs, https://code.claude.com/docs/en/workflows (accessed 2026-11-07)
- P3. Bundled `workflow-authoring` skill, Claude Code 2.1.319
- P4. Custom subagents docs, https://code.claude.com/docs/en/sub-agents
- P5. Prompt caching in Claude Code, https://code.claude.com/docs/en/prompt-caching
- P6. Hooks reference, https://code.claude.com/docs/en/hooks
- P7. Manage costs, https://code.claude.com/docs/en/costs
- P8. Headless mode, https://code.claude.com/docs/en/headless
- P9. Agent SDK system prompts, https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts
- P10. Agent SDK cost tracking, https://code.claude.com/docs/en/agent-sdk/cost-tracking
- P13. Mods reference, https://code.claude.com/docs/en/plugins/mods/reference
- P14. Mods API, https://code.claude.com/docs/en/plugins/mods/api
- P15. Mods events, https://code.claude.com/docs/en/plugins/mods/events
- P16. Pricing, https://platform.claude.com/docs/en/about-claude/pricing (accessed 2026-11-07)
- P18. "Prompt caching is everything", https://claude.dev/blog/lessons-from-building-claude-code-prompt-caching-is-everything/ (2026-04-30)
- P19. Anthropic, "Code execution with MCP", https://www.anthropic.com/engineering/code-execution-with-mcp (2025-11-04)
- P22. Anthropic, "Harness design for long-running application development", https://www.anthropic.com/engineering/harness-design-long-running-apps (2026-03-24)

**Verification and merging**
- V1. Uber, "CI at Scale: Lean, Green, and Fast", arXiv:2501.03440, https://arxiv.org/html/2501.03440v2 (2025)
- V2. Zuul project gating docs, https://zuul-ci.org/docs/zuul/9.4.0/gating.html
- V4. Trunk merge queue batching, https://docs.trunk.io/merge-queue/optimizations/batching
- V5. Mergify bisection statistics, https://docs.mergify.com/changelog/2026-03-12-batch-bisection-statistics-api/ (2026-03-12)
- V6. Anthropic, "Bringing Code Review to Claude Code", https://claude.com/blog/code-review (2026-03-09)
- V7. Greptile on Martian Code Review Bench, https://www.greptile.com/content-library/greptile-martian-code-review-benchmark (leaderboard 2026-07-30)
- V9. "Bigger Isn't Always Better", arXiv:2606.15689 (2026-04; single study)
- V10. Cursor, "Building Bugbot", https://cursor.com/blog/building-bugbot
- V11. BitsAI-CR arXiv:2501.15134; Ubisoft RevMate; SGCR arXiv:2512.17540; MetaMateCR arXiv:2507.13499
- V12. Zietsman, "The Specification as Quality Gate", arXiv:2603.25773 (2026-03)
- V13. Aviator affected targets, https://docs.aviator.co/mergequeue/concepts/affected-targets
- V15. Machalica et al., "Predictive Test Selection", arXiv:1810.05286 (ICSE-SEIP 2019)
- V17. pytest-testmon, https://pypi.org/project/pytest-testmon
- V19. Bazel remote cache, https://bazel.build/remote/cache-remote
- V20. Google Testing Blog, "Flaky Tests at Google" (2016-05)
- V21. "Don't Judge Code by Its Cover", EACL 2026 Findings
- V22. Minority-veto judges, arXiv:2510.11822
- V23. Tencent, LLM false-positive reduction, arXiv:2601.18844 (2026-01)
- V25. Proxify, contract chains, https://agentic.proxify.io/patterns/orchestration-patterns/contract-chains
- V26. AWS DEV, "8 agents wrote perfect components and nothing worked", https://dev.to/aws/8-agents-wrote-perfect-components-and-nothing-worked-5h2b
- V27. Vaughan, "The Multi-Agent Coordination Problem", https://codex.danielvaughan.com/2026/06/06/multi-agent-coordination-problem-concurrent-agents-semantic-conflicts-codex-cli/ (2026-06-06)

**Efficiency, granularity, SDD**
- E1. "Evaluating AGENTS.md", arXiv:2602.11988 (ICLR 2026), via https://the-decoder.com/context-files-for-coding-agents-often-dont-help-and-may-even-hurt-performance/
- E3. Subagent overhead measurements, https://dev.to/nomurasan/why-my-subagent-launches-cost-50k-tokens-before-doing-any-work-4em1 (2026; blog)
- E5. "The Complexity Trap", arXiv:2508.21433 (NeurIPS 2025 workshop)
- E6. Unblocked, model routing for coding agents, https://getunblocked.com/blog/model-routing-coding-agents (2026-06-09)
- E8. Why coding agents still use grep, https://yage.ai/share/why-coding-agents-still-use-grep-en-20260327.html (2026-03)
- E9. Cursor semantic search, https://cursor.com/blog/semsearch (2025-11)
- E10. Aider architect/editor, https://aider.chat/2024/09/26/architect.html
- E12. METR time horizons, arXiv:2503.14499; https://metr.org/blog/2026-1-29-time-horizon-1-1
- E13. METR modelling assumptions note, https://metr.org/notes/2026-03-20-impact-of-modelling-assumptions-on-time-horizon-results/
- E14. SWE-Bench Mobile arXiv:2602.09540; issue-solving failure study arXiv:2509.13941
- E15. "Rethinking the Value of Agent-Generated Tests", arXiv:2602.07900 (2026)
- E16. TDFlow arXiv:2510.23761; TENET arXiv:2509.24148
- E17. Kiro changelog (0.12), https://kiro.dev/changelog/ide/
- E18. Vaughan on CooperBench and contracts (2026-06; unverified figure)
- E19. CAIS, SWE-bench Pro harness comparison, https://safe.ai/blog/evaluating-ai-progress-on-swe-bench-pro (2026)
- E20. Terminal-Bench harness gap, https://qepo17.pages.dev/til/2026-04-02-terminal-bench-harness-gap (2026-04; blog)
- E22. Triage, arXiv:2604.07494 (2026-04; proposal)
- E23. Confucius Code Agent arXiv:2512.10398; "When More Thinking Hurts" arXiv:2604.10739
- E24. Large Language Monkeys, arXiv:2407.21787
- E26. BMAD Quick Flow, https://docs.bmad-method.org/explanation/quick-flow
- E27. Thoughtworks Radar on OpenSpec, https://www.thoughtworks.com/radar/tools/openspec
- E29. Böckeler, SDD tools, https://www.martinfowler.com/articles/exploring-gen-ai/sdd-3-tools.html (2025-10)
- E30. Scott Logic, Spec Kit, https://blog.scottlogic.com/2025/11/26/putting-spec-kit-through-its-paces-radical-idea-or-reinvented-waterfall.html (2025-11)

**Internal measurements (this session)**
- `ck stats show` (96 spec-implement, 14 spec-create runs) and `ck stats show wf_2a55e2f7-b3c`
- Transcripts of `wf_8bca74f5-ecc` under `~/.claude/projects/-home-kutlay-Workspace-claude-kiro/ccfd1ac8-…/subagents/workflows/` (208 agents; usage deduplicated by `requestId`; priced at 5.5-generation list prices)
- `strings`-level check of the installed Claude Code 2.1.319 binary for setting names; `nproc` = 16
