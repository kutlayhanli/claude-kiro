# Equity E2E findings

Run: `wf-20261031145547-e45f5e` on `opencode-go/muse-spark-1.3-contributor`.
Spec: `management-track-record` in `kutlayhanli/equity-anaylsis-tool`.
Result: 55/87 merged, 4 blocked, 28 not started, 81.7 min; planner halted.

## Spec/test issues (filed in the equity repo)

- [#1 Task 75: impossible tolerance for rounded inputs](https://github.com/kutlayhanli/equity-anaylsis-tool/issues/1)
- [#2 Task 57: TC-195 scanner contradicts FIXTURE_DENYLIST](https://github.com/kutlayhanli/equity-anaylsis-tool/issues/2)
- [#3 Task 85: horizon before created violates the validator](https://github.com/kutlayhanli/equity-anaylsis-tool/issues/3)
- [#4 Task 32: verify-order cycle](https://github.com/kutlayhanli/equity-anaylsis-tool/issues/4)
- [#5 Task 88: protected `.claude/commands/sweep.md`](https://github.com/kutlayhanli/equity-anaylsis-tool/issues/5)

These are real contradictions, not model failures. The implementation track correctly refused to
edit tests or bypass the schema gate.

## ck / workflow issues (filed in this repo)

- [#5 deterministic plan fallback](https://github.com/kutlayhanli/claude-kiro/issues/5)
- [#6 `ck lint` verify-order cycles](https://github.com/kutlayhanli/claude-kiro/issues/6)
- [#7 generated artifacts in the worktree busy check](https://github.com/kutlayhanli/claude-kiro/issues/7)
- [#8 test-correction handoff](https://github.com/kutlayhanli/claude-kiro/issues/8)
- [#9 cache-read pricing](https://github.com/kutlayhanli/claude-kiro/issues/9)
- [#10 OpenCode first-call args](https://github.com/kutlayhanli/claude-kiro/issues/10)
- [#11 planning/review overhead](https://github.com/kutlayhanli/claude-kiro/issues/11)

## Run analytics

| Metric | Value |
|---|---|
| Wall time | 81.7 min |
| Task span | 69.4 min |
| Attempted | 59 |
| Merged | 55 (93%) |
| Blocked | 4 |
| Not started | 28 |
| Agent calls | 249 |
| Agent minutes | 556.4 |
| Tokens | 56.2M input · 1.24M output · 496.5M cache read |
| Peak context | 283,106 |
| List-price estimate | $6.86 |
| Provider-reported cost | $0.00 |

The largest costs were test-writer (155 agent-min), implementer (233 agent-min) and reviewer
(96 agent-min). There were 53 plan agents and 62 reviewers for 55 merges, with 135 min
idle-before-merge. Planning/review/landing overhead is the main throughput limit, not raw codegen.

## Fixed locally

- Added the Muse cache-read rate (`$0.002/M`); the list-price estimate went from $5.87 to $6.86.
- In the E2E copy, made Task 32 a foundation with a smoke verify and moved its `Verifies` edge to
  Task 33.
- Added a Python `.gitignore` and cleaned `__pycache__` from the four dirty worktrees.

## Suggested order

1. Deterministic plan fallback + lint cycle check.
2. Artifact-aware dirty checks.
3. Test-correction handoff.
4. Dynamic pricing.
5. Planning/review overhead.

Then rerun `management-track-record` from the 55-task integration tree and compare the same metrics.
