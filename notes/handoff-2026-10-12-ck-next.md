# Handoff: ck after the sandboxed sihman benchmarks (2026-10-12)

Written at the end of a long ck session, for whoever picks up the next ck changes. Read it before touching `ck run`, landing, the guard or review.

## Where things stand

- `master` == `feat/unattended-escalation` at `a2d00c4` (0.9.1); `ck update` installs it.
- 0.9.x contains, among other things:
  - **Dependency-graph scheduling** with a concurrency cap.
  - **The model escalation ladder** (Haiku → Sonnet → Opus, with handoff notes).
  - **Roles:** orchestrator and resolver.
  - **Risk routing:** `**Risk:** safety` tags and `risk_paths`.
  - **Properties.**
  - **Batched landing** (`ck worktree land`).
  - **`ck bench`.**
  - **Spec-create consistency checks:** governing criterion, post-revision consistency pass, property conflicts.
  - **Plugin distribution** (`plugin/` is the single source; `ck vendor` for cloud).
  - **The sandbox-first `ck run`.**
  - **Hook changes** from another session: `done-verify` replaces the slow Stop hook (`464a89e`), and the requirements guard lets pure additions through (`9b7ae13`).
- **`ck run` model:** Claude Code's Bash sandbox is mandatory, and is the security boundary.
  - **Launch:** auto permission mode, `--permission-prompts none`, about 10 allow rules and a short deny list.
  - **Credentials:** `sandbox.credentials` denies credential files and credential-shaped env vars found at launch. `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` is removed from the child env.
  - **Network:** a strict domain allowlist.
  - **Worktrees** live in `.ck/worktrees/` (`worktree_dir` in specs/ck.json).
  - **Landing** refuses task diffs touching `.claude/**`, `.mcp.json` or hook dirs (`land.allow_paths` opts in).
  - **Why it's built this way** (all measured on Claude Code 2.1.296):
    - The sandbox makes the project's whole `.claude/` read-only to Bash.
    - The env scrub forces the permission mode to `default` (anthropics/claude-code#51258, closed not planned).
    - The env scrub makes `.git/worktrees/` read-only (we filed anthropics/claude-code#101546).
    - The env scrub needs bwrap.
    - A sandboxed command's background children die when it returns.
    - Only `$TMPDIR` is writable, not `/tmp`.

## Benchmark results (spec `ai-first-email-tool`, 62 tasks, base 78ab7c7)

| Run | Merged | Time | Notes |
|---|---|---|---|
| Baseline: Sonnet impl, Opus review | 45 | ~182 min | operator answered prompts |
| All Haiku | 29 | ~62 min | shipped send-path safety bugs |
| Escalation pilot (0.8) | 8 | 34 min | |
| Sandboxed 0.9.0 | 19 | 82 min | stopped by the two bugs fixed in `161adf2`, `a2d00c4` |
| **Bench v2, sandboxed 0.9.1 with hooks** | **27** | **52 min** | ~$36 of $40; best unattended run |

- Bench v2 lives in `/home/kutlay/Workspaces/sihman/sihman-bench-v2`, on branch `integrate/ai-first-email-tool`. The log is `.claude/ck-runs/v2.log`, the transcripts are under `~/.claude/projects/-home-kutlay-Workspaces-sihman-sihman-bench-v2/`, and the workflow is `wf_24af0913-08d`.
- What blocked the rest: Task 13 (the Opus review rightly held a price-detection gap), Task 5 (a live spike only the operator can run), and Tasks 2 and 18 (bug 1 below). The other 31 tasks depend on those.
- An Opus audit of the merged code found nothing on the branch that can send email yet, plus:
  - Two test files contradict the design's field names (`command` / `intent_id` / `state`): `test_mailtool_gate_tripwires.py` and `test_mailtool_gate_limits_audit.py`. Parallel test writers drifted, and the Sonnet reviewer missed it.
  - The restart-recovery rule 9.20 is untested in practice: the harness runs recovery itself.
  - TC-119 stays red.
  - Task 26 rewrote test-plan rows TC-83/84/85 (one check weakened) without approval.

## Changes to make, in priority order

1. **Process-ID ownership breaks under the sandbox (bug).** Each sandboxed command runs in its own process-ID namespace, so recorded PIDs are meaningless across commands.
   - Bench v2 showed task claims recorded as owner "pid 2". The orchestrator reported Tasks 2 and 18 as "claimed by a possibly-dead agent" and would not take them over, although both had finished, committed work.
   - `worktrees.py` uses the same pattern for the landing lock (`_pid_alive`, `os.kill(pid, 0)`, about L351-378) and in `claim()`.
   - Replace PID liveness with something namespace-proof: `fcntl.flock` on a lock file (released when the holder exits), or an owner token plus a heartbeat timestamp with a staleness timeout. Claims via `git worktree lock --reason` should store a token and time, not a PID.
   - Add a test that simulates a foreign or recycled PID.
2. **Guard `test-plan.md`.** `guard.tests` covers test files, and `guard.requirements` covers `requirements.md` (pure additions pass since `9b7ae13`). `test-plan.md` has no guard at all, which is how Task 26 weakened TC-85 silently. Give it the same rule as requirements: additions pass, rewording or deleting existing rows asks. The setting is `guard.test_plan` (default `ask`). In unattended runs an "ask" becomes a denial plus a "needs a human" report. That's the desired behaviour.
3. **Contract check for test-track tasks.** Test tasks written in parallel drifted from the design's data models, and review didn't catch it. Two options, ideally both:
   - (a) Reviewer prompt for `[test]` tasks: list every request and response field and status value the new tests use, and check each against design.md's Data Models and API tables. Mismatches block.
   - (b) A cheap static `ck lint --contracts` heuristic: pull the field names from design.md code blocks, grep the new test files for string-literal keys used against API responses, and flag unknown ones.
   Also consider one Opus "cross-test consistency" agent once a wave of test tasks has merged, since that catches drift *between* test files.
4. **The orchestrator's report must come from git, not from agent claims.**
   - In the 0.9.0 run the final report said 11 merged while git showed 19.
   - In bench v2 it listed Task 17 as both merged and "merge failed: no branch".
   - Build the merged/failed/unstarted lists from `git log --merges` on the integration branch plus `tasks.md` statuses, and keep agent results as annotations only. `ck` could expose `ck worktree report <spec> --json`.
5. **The "planning step failed" halt, and plan agents mangling task ids.**
   - Bench v2 ended with "the run halted … because the planning step failed" while every remaining task was legitimately blocked.
   - Separately, the transcript analysis found Haiku plan agents returning worktree paths instead of task ids 10 times. That caused duplicate task runs and the "MISSING: no branch" lands for Tasks 17, 18 and 24.
   - Planning is mechanical (`ck plan --json`), so make it a plain step, not an agent; see the Haiku section.
   - Treat "no ready tasks, none busy" as a normal end of run, not a planning failure.
6. **Confirm the final gate works.** `161adf2` tells agents to start long gates with the Bash tool's `run_in_background`. Bench v2 is the first run with it. Check its final-gate agent transcript in `wf_24af0913-08d` to confirm the gate actually ran.
7. **Throughput cost of `done-verify`.** It's a PostToolUse hook with a 600 s timeout that gates a task synchronously when it's marked Done, and the workflow gates again afterwards. Bench v2 merged its first 6 tasks in 23 min, against 8 in 18 min without hooks. Measure it from the transcripts, and if it's double work, have the workflow reuse the hook's pass stamp (`c0e2f8a` stamps a passing suite by tree hash).
8. **`ck update` side effects.**
   - It upgrades the project in the current directory without saying so. It switched a stale claude-kiro worktree to plugin mode, which had to be restored by hand.
   - It installs and enables the `spec` plugin at user scope, editing `~/.claude/settings.json`.
   - Fix: only touch the current project when it's a ck project and the user passes `--project` (or ask), and print a clear line before installing the plugin.
9. **`ck doctor --unattended` gaps.**
   - Detect PreToolUse hooks that return `permissionDecision: allow` (e.g. the RTK rewrite hook `~/.claude/hooks/rtk-rewrite-wt.sh`). They bypass `--disallowedTools`; only the sandbox network stopped a test `git push`.
   - Warn about the sandbox proxy start-up race: a command that hits the network in its first ~0.3 s gets `Failed to connect to localhost port 3128`.
10. **Vendored workflow lookup.** In vendored projects the orchestrator first tries `spec:spec-implement` (the plugin name), fails, then falls back to `.claude/workflows/spec-implement.js`. Have the skill check the manifest mode and go straight to the right one.
11. **Specs that edit `.claude/`.** Even with `land.allow_paths`, the sandbox keeps the main checkout's protected `.claude/` paths read-only, so a sandboxed landing of such changes would fail ("unable to unlink old"). Document "attended merge only", or land those in a final unsandboxed step after the operator confirms.
12. **Cost visibility.** Both sandboxed runs used $36-37 of the $40 default. `workflowProgress` undercounts tokens 7-14x. Report the real spend from transcripts (`ck bench score` already parses them) in the run's final report, and consider raising the default budget or making it per-task.

## Haiku: where to use it

This section summarises a transcript analysis of all five runs (195 + 180 + 110 + 78 + 399 agents). Full tables, per-task outcomes and the scripts are in `notes/haiku-analysis-2026-10-12/`. Costs use assumed list prices, which are stated in `tables.md`. Treat the dollar figures as relative, not exact.

| role × model | n | median output tok/s | median wall s | outcome |
|---|---|---|---|---|
| impl Haiku (1st attempt) | 52 | **226** | 212 | 82% gate-green first try, 46% clean first review |
| impl Sonnet | 34 | 150 | 263 | 75% gate-green, 36% clean |
| impl Opus (safety) | 5 | 112 | 179 | 3/5 |
| test Haiku / Sonnet | 29 / 32 | 220 / 151 | 310 / 150 | 29% / 33% clean |
| review Haiku / Sonnet / Opus | 17 / 84 / 145 | 213 / 125 / 102 | 67 / 67 / 135 | Haiku too lenient (approved 7/10 on first look) |
| plan Haiku / Opus | 193 / 85 | 210 / 95 | 6 / 9 | Haiku mangled task ids 10× |
| land Haiku / Opus | 58 / 56 | 194 / 67 | 29 / 53 | Haiku invented results 7× |

**Findings:**
- **Haiku is fast, but uses more turns.** It generates about 1.5× Sonnet's tokens/s and 2.1× Opus's, but takes about 2.3× more turns (median 36 vs 16), so implementation wall time is roughly the same. Its advantage is cost per task (about 30% of Sonnet's) at equal or better gate-green rates, not raw latency.
- **Haiku implementation plus the escalation ladder works.** In bench v2, 5 tasks escalated to Sonnet on review and all merged. Exception: escalation wasted 4 attempts on Task 5, which only the operator can do. "Blocked on a human" should stop escalation.
- **Haiku review is too lenient.** Keep Sonnet high for normal review and Opus for safety.
- **No model caught the test field-name drift.** Both offending files came from Haiku test agents, and a Sonnet reviewer approved both. That makes change 3 (the contract check) necessary whichever model writes tests.
- **The mechanical agents (plan, land, gate) are the main problem.** They hold about 50% of wall time and caused every mechanical defect:
  - Haiku plan agents returned worktree paths instead of task ids (`ready:["5"]` → `.ck/worktrees/...-task-5`). That caused duplicate runs, the "MISSING: no branch" lands (Tasks 17, 18, 24) and busy-worktree failures. It also explains change 5.
  - Haiku land agents invented a `mergedAt`, dropped `ck` errors (returned `{"tasks":[]}` on exit 3) and built malformed commands.
  - In bench v2 a land agent was running for 48% of the wall time, although `ck` lands take about 3 s.
  - In the 0.9.0 run, gate waiting was 50% of the run.
  - In the baseline, Opus plan agents sat 6–10 min before their first token, although `ck plan` takes 1 s.
- **Token counts:** cache reads are 90–97% of input tokens, and `workflowProgress` leaves them out (about 7% of the true volume). 37% of Haiku requests go over the 100K-prompt price tier and make up 78% of Haiku spend. Claude Code's base context is about 63K, so trimming agent context would roughly halve the Haiku bill.

**What to change (Haiku):**
1. **Highest value: replace the plan, land and gate-wait agents with workflow code.**
   - Plan becomes a direct call to `ck plan --json` plus `ck worktree create --json`.
   - Land becomes `ck worktree land --json`.
   - Gate becomes `ck gate`, with its exit code read synchronously.
   - Check whether the Workflow script API can run a shell command without an agent. If it can't, use the smallest possible Haiku agent at low effort whose only job is to run one command and return its stdout verbatim, and have the workflow parse that JSON itself.
   This should cut wall time sharply and remove a whole class of misreports.
2. **Keep Haiku medium as the default for normal-risk implementation and test tasks**, with the ladder. Don't raise its effort; the earlier benchmark showed no gain, and xhigh broke.
3. **Don't use Haiku for review, fix, resolve or final-gate repair, or for writing safety and contract tests.** Consider routing `[test]` tasks that touch API contracts to Sonnet.
4. **Trim agent context for Haiku** (shorter briefs, fewer loaded files) to stay under the 100K-prompt tier.
5. **Set escalation and retries to zero** for tasks marked as needing the operator.

## Pointers

- **Code:** `src/claude_kiro/cli/run.py` (ck run, doctor), `src/claude_kiro/worktrees.py` (claim/land/merge), `plugin/workflows/spec-implement.js` (scheduler, prompts, GATE_NOTE, LAND schema), `plugin/workflows/spec-create.js`, `plugin/skills/implement/SKILL.md`.
- **Tests:** `uv run pytest -q` (291 at `a2d00c4`), `node tests/js/spec_implement.test.mjs`, `node tests/js/spec_create.test.mjs`. After editing `plugin/`, run `ck vendor` in this repo (a drift test checks `.claude/`).
- **Memory:** `~/.claude/projects/-home-kutlay-Workspace-claude-kiro/memory/implementer-model-benchmark.md` (earlier per-model numbers).
- **Separate work:** a handoff for the mailtool send-integrity spec (sihman-shopify) was written for the mailtool session. It is not ck work.
