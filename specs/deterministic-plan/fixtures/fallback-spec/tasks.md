# Implementation Tasks: Fallback Spec

A 3-task fixture for the deterministic-plan Story 6 gate (Task 21): `1 → 2`, `1 → 3`.
The plan-agent shim (`plan_shim.mjs`) forces the first two plan answers to contradict
`deps` in `forced-inconsistent` mode and passes them through in `healthy` mode.

## Task Breakdown

### Task 1: Fixture task one
**Status:** Not Started
**Track:** impl
**Requirements:** 6.1
**Description:** Root task with no dependencies.
**Files:**
- `src/f1.py` - change
**Dependencies:** None
---

### Task 2: Fixture task two
**Status:** Not Started
**Track:** impl
**Requirements:** 6.1
**Description:** Depends on Task 1.
**Files:**
- `src/f2.py` - change
**Dependencies:** Task 1
---

### Task 3: Fixture task three
**Status:** Not Started
**Track:** impl
**Requirements:** 6.1
**Description:** Depends on Task 1.
**Files:**
- `src/f3.py` - change
**Dependencies:** Task 1
---
