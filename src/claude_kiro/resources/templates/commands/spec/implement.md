---
description: Implement a spec task, or a whole spec wave by wave as a workflow
argument-hint: [spec-name | task-number]
allowed-tools: Read, Write, Edit, Grep, Glob, Bash, TodoWrite, Workflow
---

Implement: $ARGUMENTS

# Two Modes

- **A task number** (for example `/spec:implement 3` or `/spec:implement auth 3`): implement that one task yourself, following the Implementation Guidelines below.
- **A spec with no task number** (for example `/spec:implement auth`, `/spec:implement auth all`, `/spec:implement auth in parallel`): run the whole spec as the `spec-implement` workflow, wave by wave. That's the default for a spec. Running this command for a whole spec is my opt-in to the multi-agent workflow; don't ask me before each wave.

## Whole Spec: The Wave Workflow

1. **Resolve the spec** in `specs/`. If it only exists in `.claude/specs/`, tell me to run `ck migrate` and stop.
2. **Check the setup.** If `.claude/workflows/spec-implement.js` or `.claude/workflows/spec-implement-brief.md` is missing, tell me to run `ck upgrade` and stop. Read `specs/ck.json`; if `"verify"` is empty, tell me, since the gate needs it.
3. **Plan.** Run `ck waves <spec> --json`. Show me a short plan: the number of waves, tasks per wave, how many are already Done (they'll be skipped), and any warnings (for example, waves recomputed from Dependencies).
4. **Choose the merge target:**
   - **Default: an integration branch.** Run `ck worktree integration <spec>`. It creates or reuses `.claude/worktrees/<spec>-integration` on branch `integrate/<spec>`, branched from the main checkout's current branch. Then set `into` = `integrate/<spec>` and `targetDir` = `<main checkout>/.claude/worktrees/<spec>-integration`. Task worktrees branch from it and every task merges into it, so the main branch is never touched mid-run. I fast-forward main at the end.
   - **If I asked to merge directly:** set `into` = the main checkout's current branch and `targetDir` = the main checkout. The main checkout must have no uncommitted changes to tracked files; if it has some, stop and tell me.
5. **Launch** the Workflow tool with `scriptPath` set to the absolute path of `.claude/workflows/spec-implement.js` and `args` as a JSON object:
   ```json
   {
     "spec": "<spec>",
     "root": "<absolute main checkout>",
     "into": "<target branch>",
     "targetDir": "<absolute checkout that has the target branch>",
     "waves": "<waves from ck waves>",
     "deps": "<deps from ck waves>",
     "titles": "<titles from ck waves>",
     "tracks": "<tracks from ck waves>",
     "maxRetries": 1,
     "onFailure": "continue",
     "maxConcurrent": null
   }
   ```
   By default a failed task doesn't stop the run: later tasks whose dependencies all merged keep running, tasks that depend on the failure are skipped and reported, and a red target branch always halts. Use `"onFailure": "halt"` if I asked to stop at the first failed wave. Set `maxConcurrent` (for example 4) if I said the test suite is memory-heavy.
6. **Tell me it's running.** Each wave sets up worktrees, runs task agents in parallel (with one retry when a gate fails), merges each task as it finishes (one merge at a time), then runs `ck gate` on the target. I can watch with `/workflows`.
7. **When it returns,** report per wave: tasks merged, tasks failed or skipped with their blockers, tests agents believe are wrong, and files touched outside their task. If it halted, say at which wave and why. Then give me the next step: fix the blockers (or answer the wrong-test questions), then run `/spec:implement <spec>` again. Done tasks are skipped and the integration worktree is reused, so a rerun picks up where it stopped.
8. **Landing the integration branch:** once I'm happy (ideally once every task is Done and `ck gate <spec>` passes in the integration worktree), tell me the command to land it: `git merge --ff-only integrate/<spec>` from the main checkout. If main moved in the meantime, use `git merge integrate/<spec>` instead. Afterwards: `git worktree remove .claude/worktrees/<spec>-integration && git branch -d integrate/<spec>`. Don't run these yourself unless I ask.

The rest of this file is the single-task guide, which every task agent in the workflow also follows (through `.claude/workflows/spec-implement-brief.md`).

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
