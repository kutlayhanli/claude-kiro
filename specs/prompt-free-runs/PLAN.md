# Plan: Prompt-free runs

**Status:** Decided
**Updated:** 2026-10-29
**Evolves:** none (extends specs/run-analytics and specs/agent-mode-analytics for the analytics part)

## Problem
During `/spec:implement` (and `/spec:create`) I have to approve tool calls by hand, which breaks unattended runs. Over three weeks of transcripts there were about 49 real permission prompts during spec work, and most came from ck's own PreToolUse guard (`ck --hook pre-tool-guard`), not from Claude Code settings: the user settings already allow `Bash(*)` in auto mode. A further ~985 tool calls were refused outright by Claude Code's worktree-isolation check (no prompt, but agents lose time retrying), plus ~38 auto-mode classifier denials and ~15 headless "needs approval" denials.

Success: an unattended run never stops for my approval, the guard still blocks any change that actually weakens an existing test, and `ck stats` shows per run how many prompts, denials and refusals happened and how long the run waited on me, so the number can be driven to zero.

## Scope
**In:**
- The Bash path of the pre-tool guard: stop asking on harmless test-file commands, keep blocking real weakening (D5).
- Deny-and-escalate for task agents; only my own session is asked (D6, D7).
- Main-session edits to requirements.md / test-plan.md pass (D4).
- Hand-off to `ck run` when a whole-spec run starts from an isolated worktree session (D8).
- Analytics: counts of prompts, denials and refusals per run, by role and command family, plus time waiting on me; no command text in any shared record (D9, D10, D11).
- All of the above in both Workflow-tool mode and agent mode (D12).

**Out:**
- Changing Claude Code's own auto-mode classifier or isolation guard.
- The requirements.md / test-plan.md guard for task agents (kept as is, D4).
- Changing `implementer.escalate` for ordinary (non-guard) failures.
- The run ETA feature (separate spec; it can read the D9 counts later).

## Constraints
- Same safety: a change that removes assertions or test cases, adds skip/xfail/only markers, or deletes an existing test is still stopped.
- Privacy as in run analytics: shared records carry counts and categories only, never commands, paths or reason text.
- Schema stays `ck-run-analytics/1` (additive fields only), as decided in agent-mode-analytics D1.

## Decisions

### D1: Success bar for the guard
**Choice:** Zero prompts in unattended runs with the same safety. Harmless sed/rm/scratch edits pass; changes that really weaken an existing test are still blocked.
**Why:** prompts are what break unattended runs; the guard's value is catching weakening, not catching every shell command that touches a test path.
**Rejected:** fewer prompts but keep asking - still stalls runs; always deny - throws away the interactive case where I want to be asked.
**Decided by:** user

### D2: Isolation refusals (commands refused by Claude Code's worktree-isolation check)
**Choice:** In scope. Originally "fix the agent instructions"; research showed the instructions are not the cause, so D8 replaces that fix.
**Why:** ~985 refusals cost retries and time even though they never prompt.
**Rejected:** count only - leaves the waste; out of scope - same.
**Decided by:** user (fix revised to D8 by user)

### D3: How analytics captures prompts and refusals
**Choice:** Both: `ck stats collect` derives counts and wait time from the transcripts it already reads, and the ck guard also writes a local event per decision that collect joins in as a cross-check.
**Why:** transcripts see every source (ck guard, auto mode, headless, isolation, my rejections); the guard log is exact for ck's own decisions.
**Rejected:** transcripts only - no cross-check of the guard; guard log only - blind to Claude Code's own denials.
**Decided by:** user

### D4: Spec-file edits (requirements.md / test-plan.md) during implementation
**Choice:** Keep guarding task agents; edits from my main interactive session pass.
**Why:** 13 asks were mostly my own `/spec:plan` / `/spec:review` revisions; the guard exists to stop agents bending the spec.
**Rejected:** keep as is - asks me about my own edits; out of scope - leaves the noise.
**Decided by:** user

### D5: How the guard judges Bash commands that touch tests
**Choice:** Check the result, not the command. Before a Bash command, block only deleting or moving a test file that exists on the base branch (and is not owned by an In Progress test-track task). Let everything else run (`sed -i`, `perl -i`, python rewrites, `>` redirects, scratch and new files, `__pycache__`). After the command, a PostToolUse check diffs the test files it changed and runs the same `weakening()` check the Edit path uses; if an existing test got weaker, the agent is told to restore it. The gate still re-checks at Done.
**Why:** a command's text can't show what a sed or python script does; the diff can. Same precision as the Edit path, and no prompts on harmless edits.
**Rejected:** predict before (narrowed pre-check) - still asks on harmless edits or misses weakening; gate only - the agent learns at the very end.
**Decided by:** user

### D6: What happens when the guard catches real weakening by a task agent
**Choice:** Deny for task agents, with a reason telling them to keep the test and report the conflict. A task stopped this way escalates through every model above its current one (sonnet, opus, then Fable) until it resolves. This ladder is for guard blocks only and does not change `implementer.escalate` for ordinary failures. If Fable still can't resolve it, ask me: in an unattended run the task is parked as "needs input" with the quoted test and requirement, I get an ntfy push, independent tasks keep running, and the final report names the decision needed; in an interactive run I'm asked with AskUserQuestion. My own main interactive session keeps the `guard` mode from specs/ck.json ("ask").
**Why:** zero prompts during runs, a real attempt with the strongest model before bothering me, and the run never stalls on one task.
**Rejected:** always deny, including my session - I want to be asked about my own edits; follow ck.json everywhere - prompts during runs; pause the whole run - one task would block the rest; report at end only - too late to act.
**Decided by:** user

### D7: How the guard tells a task agent from my main session
**Choice:** A task agent is a hook call whose input has `agent_id` (documented for subagents and teammates) or a headless session (`ck run`, `claude -p`). The main session has no `agent_id` and is interactive. The spec adds a test that Workflow-tool agents carry `agent_id`; if they don't, a ck-set marker is the fallback.
**Why:** uses documented Claude Code fields; no new state for plain Agent-tool runs.
**Rejected:** a ck-set marker only - misses Agent-tool runs that don't set it.
**Decided by:** user

### D8: Running a whole-spec run from an isolated worktree session
**Choice:** At start, `/spec:implement <spec>` (and whole-spec `/spec:create` / `/spec:review`, per D11) detects that the session is isolated in a `.claude/worktrees/` worktree and hands the run to `ck run` from the main checkout (headless, sandboxed, no prompts), then tells me where to follow it.
**Why:** every one of the ~985 isolation refusals came from top-level sessions isolated in a worktree (background jobs, EnterWorktree); none came from `ck run` or main-checkout sessions. `ck run` from the main checkout is the setup that works, and it's what we did by hand in the agent-mode-analytics run.
**Rejected:** warn and stop - extra round trip; warn and continue - the run then wastes hundreds of refused calls.
**Decided by:** user

### D9: What run records count
**Choice:** Per run, by role (implementer, test-writer, reviewer, fixer, resolver, orchestrator) and by command family (sed, rm, mv, git, python, redirect, other): prompts and minutes waiting on me, guard denials, guard post-check catches, guard escalations (and to which model), auto-mode denials, headless denials, my rejections, isolation refusals. Shared records get these counts and families, never command text, paths or reasons. `ck stats show` prints one interruptions line per run, for example `interruptions: 0 prompts (0 min waiting) · 3 guard denials → 1 escalation (fable) · 0 refusals`.
**Why:** counts by role and family show where the next fix is; wait time is the number that matters to me.
**Rejected:** totals only - can't see which role or command kind to fix; category × role without families - loses the pointer to the next fix.
**Decided by:** user

### D10: What the guard's local event log keeps
**Choice:** Full detail, locally only: session, agent id and type, tool, decision (ask / deny / post-check), reason category and text, repo-relative path, the command text, and timestamp. It lives with the other private analytics state under the ck data dir, never in a project, and nothing from it reaches a shared record except the D9 counts; the export leak check treats its values as confidential terms.
**Why:** enough to debug which command or test keeps tripping the guard.
**Rejected:** decision, file and time without command text - harder to debug; counts only - can't find the cause.
**Decided by:** user

### D11: /spec:create and /spec:review
**Choice:** Same treatment: their records carry the same interruption counts, and the D8 hand-off covers whole-spec create and review runs.
**Why:** those are started from background sessions too.
**Rejected:** implement only - leaves create and review blind.
**Decided by:** user

### D12: Workflow mode and agent mode
**Choice:** Everything works the same whether a run uses the Workflow tool or falls back to the Agent tool (`plugin/resources/without-workflow-tool.md` and its vendored copy, `/spec:spawn-worktree`). The guard and post-check are hooks, so they cover both by construction. The parts that live in run orchestration get both implementations: the guard-escalation ladder and the needs-input parking with ntfy ping (D6) go into `spec-implement.js` and into the no-Workflow scheduler instructions; the isolated-session hand-off (D8) runs in the skills before either path is chosen; the interruption counts (D9) are collected for workflow records and for agent-mode records (`ck stats collect --agents`). Tests cover each D6/D8/D9 behaviour in both modes.
**Why:** many runs fall back to agent mode, and run analytics already had to be fixed once for missing it.
**Rejected:** workflow mode only - repeats the agent-mode-analytics gap.
**Decided by:** user

## Delegated to Spec
- Exact command-family classification rules, and the wait-time measurement (the gap between the guard's ask and the tool result, cross-checked with the guard log).
- The PostToolUse post-check mechanics: which files it diffs (test paths changed since the command started, via `git status` / `git diff`), a cheap pre-filter so it doesn't diff after every Bash call, and the restore message wording.
- How a parked "needs input" task appears in `ck worktree report`, the workflow result and the ntfy message.
- Where the D8 hand-off prints the log path, and how it reports a `ck run` launch failure.
- Local log file format and rotation.

## Risks
- Workflow-tool agents might not carry `agent_id` in hook input (only subagents are documented) -> the spec tests it first; the fallback is a ck-set marker (D7).
- The post-check runs after the command, so a weakening edit exists briefly on disk -> the agent is told to restore it and the gate still blocks Done; the files are in a task worktree, never the target branch.
- A git diff after Bash calls costs time -> pre-filter to commands that can write files under test paths (delegated to spec).
- Escalating to Fable is expensive -> only for guard-blocked tasks, after sonnet and opus; counted in D9 so the cost is visible.
- A genuinely wrong test (like TC-31 in agent-mode-analytics) won't be fixed by a stronger model -> the ladder ends by asking me with the quoted test and requirement.
- Command text in the local log can hold confidential names -> local only, under the private data dir; the leak check covers it before any export.

## Open Questions
- [x] How the guard tells a task agent from the main session -> D7.
- [x] Whether isolation refusals happen outside worktree-isolated parent sessions -> no; D8.
- [x] Ask vs deny when the guard catches real weakening in a headless run -> D6.

## Research Notes
### Codebase
- `src/claude_kiro/hooks/pre_tool_guard.py` - `DESTRUCTIVE_BASH` (rm, mv, git rm/mv, truncate, unlink) and `IN_PLACE_BASH` (sed/perl -i) at lines 31-32; `_check_bash` asks on any such command or `>` redirect whose target is a test path, minus files of an In Progress test-track task. The Edit path (`_check_file_edit`) instead runs `weakening()` and skips files new on the branch (`git_utils.exists_at`). Paths resolve against the hook's `cwd`, not a `cd` target; `__pycache__` under tests counts as a test dir.
- `src/claude_kiro/config.py:173` - `guard_mode(config, kind)` returns "ask" / "deny" / None from `specs/ck.json` `guard` (this repo: all "ask").
- `plugin/hooks/hooks.json` - PreToolUse `pre-tool-guard` on Edit|Write|MultiEdit|Bash; PostToolUse `post-file-ops` and `done-verify` already exist (the D5 post-check can sit beside them).
- `plugin/workflows/spec-implement.js:313` - `GIT_NOTE` tells agents to use `command git -C <dir>` and `ck -C <dir>` instead of `cd <dir> && ...`; fine outside isolated sessions.
- `src/claude_kiro/agents.py:26` - `MODELS = ("haiku", "sonnet", "opus", "fable")`, ascending; my `implementer.escalate` is `["sonnet", "opus"]`.
- Transcript evidence: a guard ask is an `attachment` `hook_success` with `permissionDecision: "ask"` and its reason; an approval leaves a normal tool_result, so the wait shows only as the timestamp gap (21-290 s seen). Auto-mode, headless and user-reject denials are error tool_results with fixed phrasing; isolation refusals start "This session/agent is isolated in the worktree".

### Transcript analysis (last 3 weeks, this machine)
- 49 ck guard asks (47 interactive, 2 headless): `sed -i` on tests 23, requirements.md edits 13, `rm` of just-created scratch tests 6, python heredoc rewrites ~4, `rm -rf tests/__pycache__` 2, `git rm` 2. 14 of the Bash asks had a `cd <worktree> &&` prefix.
- ~985 worktree-isolation refusals, all in top-level sessions isolated in a `.claude/worktrees/` worktree (884 after EnterWorktree, 94 in background-job sessions); none in `ck run` or main-checkout sessions.
- ~38 auto-mode classifier denials, ~15 headless "no approval surface" denials, 8 user rejections.

### Sources
- [Claude Code hooks reference](https://code.claude.com/docs/en/hooks) - common input fields include `agent_id` ("Unique identifier for the subagent or in-process teammate the hook fires in") and `agent_type`; PreToolUse fires in subagents with those fields; Workflow-tool agents are not mentioned. `deny` reasons are shown to Claude; in a `-p` run where no one can answer, Claude reads the reason in the tool result. No version note found.
