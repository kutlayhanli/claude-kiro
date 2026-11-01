# Evolution: Landing Speed → Deterministic Plan

**From:** `specs/landing-speed` · **To:** `specs/deterministic-plan` · **Date:** 2026-11-01

## What Changed

- Planning failure handling: landing-speed stopped launching new tasks after two consecutive plan failures (`requirements.md` 1.8). Deterministic-plan retries an inconsistent plan once, then schedules from the deterministic ready list (script-local computation from run inputs when present, else the last good plan's ready) and marks the run degraded instead of halting. Halting remains only where no deterministic data exists at all.
- Plan triggers: the idle re-read policy is tightened — no plan re-read after every freed implementing slot; plan agents run at start, after each landing, and when tasks remain but none are ready and nothing is running.
- Reviews: landing-speed left one review per task. Deterministic-plan reviews each merged wave's non-risky tasks together in one review; risky/safety tasks are always reviewed individually.
- Visibility: fallback counts and plan/review/landing agent share appear in `ck stats` and the run report; an overhead-share threshold produces a warning only, never a halt.

## Why

The equity E2E run (`wf-20261031145547e45f5e`, 55/87 merged, 81.7 min wall) showed orchestration dominating cost and wall time (53 plan agents, 62 reviewers, 135 min idle-before-merge), and a single planner inconsistency halted the run with 28 tasks unstarted while `ck plan --json` held a valid ready set throughout (ck issues #5 and #11). Landing-speed's landing batches and wait measurements are reused, not re-decided.

## Lessons

- A plan agent's inconsistency is a transcription/model error, not a data error: the dependency graph it returns is usually correct, so the script can recover deterministically from data it already holds.
- Review agents, not plan or land agents, are the largest overhead slice (96.3 of 164 agent-min in the equity run), so review batching carries the headline saving.
- Warn-only budgets establish the baseline without risking good work on a new metric.

## What Remains Valid

- Everything in landing-speed's Heritage/Preserves list: target branch never red, red-batch bisection, one landing at a time, full gates, protected paths, sandbox behaviour, review/revision/escalation ladders, retries, `maxConcurrent`, `onFailure`, git-built reports, existing stats figures.
- Script-side readiness contract (`readyTasks` equals `ck plan --json` ready set and order), finish-first scheduling, queue-wait metrics, parallel-test behaviour.
- Out-of-scope items carry over and stay out: verify-cycle recheck (#6), blocked:test handoff (#8), `/spec:create` oracle gaps, catalog pricing, gate/guard/escalation changes.
