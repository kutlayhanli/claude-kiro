---
description: Implement a task from the specification
argument-hint: [task-number-or-description]
allowed-tools: Read, Write, Edit, Grep, Glob, Bash, TodoWrite
---

Implement task: $ARGUMENTS

# Implementation Guidelines

Every task in `tasks.md` has a **Track**:

- **test**: write the tests from `test-plan.md`. They are the oracle for the impl tasks listed under **Verifies:**.
- **impl**: write the code that makes the tests listed under **Verified by:** pass.

"Done" is checked by a machine, not by you. `ck gate` (and a Stop hook that runs it automatically) runs the task's verification. A task marked Done that fails the gate blocks you from finishing.

## Before Starting

1. **Load the spec context** from `specs/[spec-name]/`:
   - `requirements.md`: the "why", and the numbered criteria your task covers
   - `design.md`: the "how", especially Public Interfaces
   - `test-plan.md`: test levels, infrastructure, and the test cases (TC IDs)
   - `tasks.md`: your task block, its dependencies, and current status

2. **Check prerequisites:** every task under **Dependencies:** is Done. For an impl task this includes its verifying test tasks: their tests must exist before you start.

3. **Mark the task In Progress** in tasks.md (`**Status:** In Progress`), mark it in TodoWrite, and commit: `git commit -m "task [N]: mark in progress"`.

## Test Track Tasks

1. Build what the test plan's **Test Infrastructure** calls for if your task owns it: fixtures, temp databases or containers, a server or CLI harness, factories.
2. Write the test cases listed in your task (TC IDs) **against real boundaries**: the real CLI entry point, a real HTTP test client, a real database (in-memory or temp), real temp directories, real subprocesses. Mock only things the project doesn't own (third-party APIs, clocks, randomness). Never mock the project's own modules, database, or filesystem in an integration test.
3. Target public interfaces from `design.md` and the requirements, not private helpers, so the tests survive refactoring.
4. Run your **Verify:** command. Before the implementation exists, the tests should **fail for the right reason** (missing behavior, a missing command or endpoint), not because of a syntax error, a broken fixture, or a typo. Fix the test code until that's true.
5. Mark Done. The gate checks that your test files exist and contain test cases. It requires them to pass only once the impl tasks they verify are Done.

## Impl Track Tasks

1. Implement as specified in `design.md`: file paths, interfaces, error handling.
2. Run the verifying tests (the **Verify:** commands of the tasks under **Verified by:**) and your own **Verify:** command. Iterate until they pass.
3. Add unit tests only for pure logic you introduce, if useful. The integration tests from the test track are the real check.
4. **Tests and requirements are not yours to change.** A guard asks me before any edit that deletes tests, removes assertions, adds skip/xfail/only markers, or edits `requirements.md` mid-implementation. If a test seems wrong or contradicts the spec, **stop and tell me**: quote the test, the requirement, and what you think is wrong. Don't edit the test to get a pass, and don't change the code to match a wrong test.

## Granular Commits

Commit after each meaningful milestone, prefixed with `task [N]:` so commits stay attributable when tasks run in parallel:

1. Task marked in progress (tasks.md update)
2. Tests written (test track) or first passing verification (impl track)
3. Core work complete
4. Edge cases and error handling added
5. Task marked Done (tasks.md update)

## Finishing: The Gate

1. Check every acceptance box in your task block that you actually satisfied.
2. Run the gate yourself:
   ```bash
   ck gate [spec-name] --task [N]
   ```
   It checks your acceptance boxes, runs the project verify commands (`specs/ck.json`), your task's **Verify:** commands, and (for impl tasks) the verifying test tasks' commands. It also confirms no existing test was deleted or weakened on this branch.
3. **Pass:** set `**Status:** Done`, update TodoWrite, and commit: `git commit -m "task [N]: complete - <summary>"`.
4. **Fail:** fix the cause and re-run. If you can't make it pass without weakening a test or changing a requirement, leave the task In Progress and tell me what's blocking it. Stopping with a task In Progress is always fine; claiming Done on a red gate is not.

If `ck gate` reports that no verify commands are configured, tell me, and suggest the right command for `specs/ck.json` (for example `uv run pytest -q`, `npm test --silent`, `go test ./...`).

## Parallel Safety

When running as a subagent via `/spawn-worktree`:
- You have your own copy of tasks.md in your worktree. Only update YOUR task's section.
- Run `ck gate` in your worktree before marking Done; hooks may not be active there.
- An impl task's verifying tests come from an earlier wave and are already merged into your branch. If they're missing, stop and report it.

## Spec Sync

If the implementation must deviate from `design.md`:
- Document the deviation and the reason in your task section of tasks.md.
- Update `design.md` if the deviation is intentional.
- Never "sync" by editing `requirements.md` or weakening tests; that needs my decision.

## Output

Provide:
1. Summary of what was implemented
2. Files changed/created, with commit hashes
3. Gate result (paste the `ck gate` output)
4. Any deviations from the spec, and any tests you believe are wrong (with reasoning)
5. Next recommended task, from the next wave in tasks.md
