# Plan: Run follow-ups (ETA, analytics hardening, flaky test, Verify-cycle lint)

**Status:** Decided
**Updated:** 2026-10-30
**Evolves:** none (follows up specs/agent-mode-analytics and specs/landing-speed; builds on specs/prompt-free-runs for notifications)

## Problem
Runs in this session showed four gaps. (1) There is no way to know when a `/spec:create` or `/spec:implement` run will finish; the workflow looks like it is being built as it goes, though its structure (create's fixed phases, implement's task graph) is known at launch. (2) The agent-mode-analytics review left minor robustness issues in agent-mode collection and backfill. (3) `test_p10_finish_first_changes_nothing_when_nothing_queues` fails about 1 full-suite run in 4 under load because it runs on real timers. (4) `ck lint` did not catch a task whose Verify ran a test file that needs a later task's CLI (agent-mode-analytics task 8 / task 13), so the run could only discover it after escalating to Opus.

Success: every run shows a finish time and cost range at launch and keeps it current (CLI, log lines, status line, notifications); analytics never double-counts or silently drops errors; the full suite is stable; lint reports Verify cycles of the task-8 kind before a run starts.

## Scope
**In:**
- Run ETA as in the mockup (`/home/kutlay/.agent/diagrams/ck-run-eta-mockup.html`).
- Agent-mode analytics follow-ups (four groups below).
- Deterministic-clock fix for the flaky scheduler property test.
- Verify-cycle lint check for test files owned by test tasks that verify a dependent impl task.

**Out:**
- Prompt and refusal handling (specs/prompt-free-runs).
- Changing how runs schedule tasks; ETA only observes and predicts.

## Constraints
- Pure code in ck: no new agents.
- Shared analytics schema stays `ck-run-analytics/1`, additive only; nothing new is shared unless listed.
- Works in Workflow mode and agent mode (as prompt-free-runs D12).
- Notifications reuse `run.notify_command` from prompt-free-runs, so this spec is implemented after prompt-free-runs merges.

## Decisions

### D1: ETA in this spec
**Choice:** Include run ETA, as in the mockup.
**Why:** same run-observability theme; the mockup's recommendations are accepted.
**Rejected:** separate spec after review.
**Decided by:** user

### D2: ETA estimator
**Choice:** Monte Carlo over the run's structure: implement simulates the task graph from `ck plan --json` under the agent-slot cap and finish-first scheduling, sampling each task's duration (and review and landing waits) from past local records; create sums per-phase durations from past create records. Output median, p10 and p90 finish.
**Why:** gives a range and handles parallel slack; pure Python, well under a second for 50 tasks.
**Rejected:** sum of medians along the critical path - one misleading number when slots are tight.
**Decided by:** accepted default (mockup recommendation, user chose "as in the mockup")

### D3: ETA surfaces
**Choice:** All three: `ck eta <spec> [--watch]` plus an ETA log line at start and after each landing; a Claude Code status-line command reading the progress file; notifications through `run.notify_command` when the ETA slips more than 15 min past the launch p90, when the run is blocked, and when it finishes.
**Why:** CLI for checking, status line for always-on, phone for when I'm away.
**Rejected:** baseline only.
**Decided by:** user

### D4: Per-task size signal
**Choice:** Besides track, model and safety tag, scale task durations by the number of Files and acceptance criteria in tasks.md, fitted from past records.
**Why:** separates 2-minute tasks from 15-minute ones; narrows the range.
**Rejected:** track/model/safety only - wider ranges.
**Decided by:** user

### D5: Cost estimate
**Choice:** Show a p10-p90 cost range next to the ETA, from the same simulation.
**Why:** free given the simulation; spend matters on long runs.
**Rejected:** time only.
**Decided by:** user

### D6: Cold start
**Choice:** Ship bundled default distributions from anonymised aggregate stats; output is flagged "low confidence: no local history" until local records exist.
**Why:** a new machine still gets a usable estimate.
**Rejected:** graph only until history exists.
**Decided by:** user

### D7: Analytics follow-ups in scope
**Choice:** All four groups:
- backfill's overlap guard must not treat an unreadable record store as empty (refuse or skip, never double-count);
- a backfill session with no readable transcript gets an `outcome` entry in `missing`;
- `ck stats collect --agents --json` prints the JSON failure object inside the sandbox, and an invalid `--agents` id under `--auto` is written to collect.log;
- `collect --all` reports open-manifest sweep errors instead of dropping them, picks the writer by record source rather than the `ag_` id prefix, drops the inline `__import__("time")`, and reads each transcript once when ordering agents.
**Why:** all are small and were found by the agent-mode-analytics reviewer.
**Decided by:** user

### D8: Flaky scheduler property test
**Choice:** Run the scheduler bridge on a deterministic (virtual) clock so timer order is fixed, and turn the relation back into an exact comparison with no re-runs. The assertion stays as strong.
**Why:** removes the flake at the cause.
**Rejected:** more re-runs - still flaky under heavy load.
**Decided by:** user

### D9: Verify-cycle lint rule
**Choice:** For impl task X whose **Verify** runs test file F, `ck lint` reports an error when F is in the **Files** of a test task T that **Verifies** an impl task Y which depends on X, directly or transitively (Y ≠ X). It is an error: `ck lint` exits non-zero and `/spec:implement` refuses to start until tasks.md is fixed. The message names F, T, Y and the dependency chain, and suggests narrowing X's Verify to files that don't need Y.
**Why:** it is the agent-mode-analytics task 8 / task 13 case: X's gate could never pass, and the run only found out after escalating to Opus.
**Rejected:** warning only - the run would still start and waste the escalations.
**Decided by:** user

### D10: ETA for blocked and waiting states
**Choice:** A parked needs-input task or an unresolvable cycle makes the ETA `blocked: <reason>`, with the remaining work once unblocked shown; a pending approval shows `waiting on you N min` and holds the estimate instead of letting it slip. ETA reads one run progress file, which also holds prompt-free-runs' park notes.
**Why:** a time that keeps moving while the run waits on me is misleading; saying blocked or waiting is honest and actionable.
**Rejected:** ignore them and let the ETA slip.
**Decided by:** user

## Delegated to Spec
- Progress-file path, format and event list (start, merge, retry, escalation, landing, park, approval wait), and how the Workflow script and the agent-mode instructions write it.
- How create-phase durations are derived from spec-create records (agents per phase) and how many simulations to run.
- The fitting of the D4 size signal and the bundled cold-start defaults (D6): which aggregates, where they ship, how they are refreshed; no record-level data, no names.
- `ck eta` output and `--json` shape; the status-line command's name and one-line format; the slip threshold's config key (default 15 min past launch p90).
- How the deterministic clock is injected into the scheduler bridge (D8) without changing the workflow's real-time behaviour.
- Exact refusal behaviour for the D7 overlap guard (refuse the whole backfill vs skip affected groups).

## Risks
- ETA depends on prompt-free-runs (`run.notify_command`, park notes) -> implement after prompt-free-runs merges; the spec branches from it.
- Thin history for review and landing waits (recorded only since 0.10.1) -> wide ranges at first, flagged by confidence; they narrow as runs accumulate.
- Workflow and agent-mode both need to write progress events -> covered in both modes, as prompt-free-runs D12.
- The status line runs often -> it reads the progress file only, never simulates; `ck eta` caches the last simulation.
- Bundled defaults could drift from reality -> they are only used with no local history and are labelled low confidence.
- The lint rule could flag legitimate Verify lines -> it fires only on the exact ownership-plus-dependency pattern; tested against this repo's existing specs (none should fail except the agent-mode-analytics original task 8).

## Open Questions
- [x] D9: Verify-cycle lint rule shape and severity -> D9.
- [x] D10: ETA blocked and waiting states -> D10.

## Research Notes
### Codebase
- `src/claude_kiro/analytics/agentcollect.py:463` - `_existing_backfill` returns `[]` on `OSError`.
- `src/claude_kiro/lint.py:144` - `verify_order_cycles` checks only Python imports of existing test files ("checked Python imports of existing test files"); missed agent-mode-analytics task 8, whose Verify ran `tests/test_stats_perf.py` (TC-56/57 need task 13's `ck stats begin` / `--agents-backfill`).
- `tests/test_landing_scheduler_properties.py:337` - P-10 runs on real timers and already re-runs up to six times per configuration; still fails under `-n auto` load.
- Local history: 504 merged tasks (agent-minutes p10 0.8 / median 5.0 / p90 16.7), 10 spec-create runs (58-96 min, median 63) in `$XDG_DATA_HOME/claude-kiro/runs/`.
- ETA mockup: `/home/kutlay/.agent/diagrams/ck-run-eta-mockup.html`.
