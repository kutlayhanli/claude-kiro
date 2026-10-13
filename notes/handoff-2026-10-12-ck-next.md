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
5. **The "planning step failed" halt.** Bench v2 ended with "the run halted … because the planning step failed" while every remaining task was legitimately blocked. Check whether the Haiku plan agent failed, or whether "no ready tasks, none busy" is being reported as a planning failure. Planning is mechanical (`ck plan --json`), so make it a plain step, not an agent; see the Haiku section.
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

(The analysis was in progress when this was written; results are appended below.)

## Pointers

- **Code:** `src/claude_kiro/cli/run.py` (ck run, doctor), `src/claude_kiro/worktrees.py` (claim/land/merge), `plugin/workflows/spec-implement.js` (scheduler, prompts, GATE_NOTE, LAND schema), `plugin/workflows/spec-create.js`, `plugin/skills/implement/SKILL.md`.
- **Tests:** `uv run pytest -q` (291 at `a2d00c4`), `node tests/js/spec_implement.test.mjs`, `node tests/js/spec_create.test.mjs`. After editing `plugin/`, run `ck vendor` in this repo (a drift test checks `.claude/`).
- **Memory:** `~/.claude/projects/-home-kutlay-Workspace-claude-kiro/memory/implementer-model-benchmark.md` (earlier per-model numbers).
- **Separate work:** a handoff for the mailtool send-integrity spec (sihman-shopify) was written for the mailtool session. It is not ck work.
