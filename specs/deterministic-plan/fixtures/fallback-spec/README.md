# Fallback fixture spec (deterministic-plan Story 6)

A 3-task fixture (`1 → 2`, `1 → 3`) for the Task 21 e2e gate. The plan-agent
shim (`plan_shim.mjs`) wraps the plan agent and selects its mode from the
constructor arg or `FALLBACK_SPEC_PLAN_MODE` (see `ck.json`):

- `healthy`: every plan answer passes through untouched (TC-32 control: zero
  fallbacks, no degraded mark).
- `forced-inconsistent`: the first two plan answers return a ready list
  contradicting their deps (`ready: ['999']`, an unknown id) while deps stay
  truthful, so the scheduler must retry once, fall back deterministically,
  complete, and report degraded (TC-31).

The hermetic CI gate (`tests/test_deterministic_fixture.py`) reproduces these
two modes through the Node bridge (`planSeq`) against the real workflow script:
no paid agents, no network, no fixture-branch mutation.

## TC-33 manual equity re-run (NOT a CI assertion)

TC-33 (6.3, NFR-2) is manual-only: run the equity workload once after this
change, then compare run statistics with the baseline run:

```bash
ck stats show <baseline-run-id> --json > /tmp/equity-baseline.json
ck stats show <rerun-run-id> --json > /tmp/equity-rerun.json
```

Record here (manual, one row per run):

| run | plan/review/land share | plan_fallbacks | planning halt? |
|-----|------------------------|----------------|----------------|
| baseline | _to fill_ | n/a (not recorded) | _to fill_ |
| rerun | _to fill_ | _to fill_ | no planning halt |

Expected: the rerun reports a lower plan/review/land agent share than the
baseline with no planning halt. Asserting a numeric threshold in CI would be
flaky and expensive, so CI gates on the fixture (TC-31/TC-32) instead.
