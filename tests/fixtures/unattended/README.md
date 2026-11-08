# Baselines for the unattended-correctness spec

Captured by Task 1 from the unchanged base commit `f8e1d2559b227366404ba67df6d4fbe3a43508e8`
(`spec-implement: run without a wall clock ...`), before any impl task of the spec merged.

| File | What it pins | Checked by |
|------|--------------|------------|
| `land-json-keys.json` | every key path of `ck worktree land --json` (`paths` = union; `scenarios` = a clean gated landing and a conflict) | TC-37 |
| `gate-text-baseline/*.txt` | today's `ck gate demo` text report per `attr_project` scenario; first line `exit: <code>`, project path and timings normalised | TC-13 |
| `failure.schema.json` | the design's `ck-gate/1` failure entry (D3) | TC-36, TC-29, TC-183 and the red-team tests |

Scenarios live in `tests/unattended_helpers.py` (`GATE_BASELINE_SCENARIOS`, `capture_land_keys`).

## Regeneration

Never regenerate these to make a test pass. Only the base commit's ck may produce them:

    git switch --detach f8e1d2559b227366404ba67df6d4fbe3a43508e8   # or a worktree of it
    python tests/unattended_helpers.py capture

`tests/test_unattended_helpers.py` reads the files and regenerates nothing.
