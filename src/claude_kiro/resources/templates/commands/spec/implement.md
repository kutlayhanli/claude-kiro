---
description: Implement a spec task, or a whole spec as a workflow (each task starts when its dependencies merge)
argument-hint: [spec-name | task-number] [--model M] [--effort E] [--test-model M] [--review-model M] [--no-review] [--no-workflow]
allowed-tools: Read, Write, Edit, Grep, Glob, Bash, TodoWrite, Workflow, Agent, SendMessage, TaskCreate, TaskUpdate
---

Implement: $ARGUMENTS

# Two Modes

- **A task number** (for example `/spec:implement 3` or `/spec:implement auth 3`): implement that one task yourself, following the Implementation Guidelines below.
- **A spec with no task number** (for example `/spec:implement auth`, `/spec:implement auth all`, `/spec:implement auth in parallel`): run the whole spec as the `spec-implement` workflow. Tasks start as soon as their dependencies merge. That's the default for a spec. Running this command for a whole spec is my opt-in to the multi-agent workflow; don't ask me before each task.

**Without the Workflow tool.** If the Workflow tool isn't among your tools, or calling it is refused or blocked (some organizations disable it), or I pass `--no-workflow`: run the same steps yourself with the Agent tool and a task list, as `.claude/workflows/without-workflow-tool.md` describes. Everything else here still applies (agents and their models, lint, plan, merge target, report); only the launch differs. Tell me once that you're running without workflows. Don't ask whether to.

## Agents: Models, Effort, and Review

Each role has a model and effort, resolved by `ck agents --json` (run it first). Defaults: **implementer** Sonnet, medium effort, for impl-track tasks; **test_writer** and **fixer** follow the implementer unless set; **reviewer** Opus, high effort, reviews every task's diff against the spec before it merges, with up to 1 revision round. My preferences (`ck agents set ...`, global or `--project`) override the defaults; `ck agents` shows the effective values and where each comes from.

For this run only, turn what I ask into `--override` arguments to `ck agents --json`:
- `--model X` / `--effort Y` (or in words, "with haiku at low effort"): `implementer.model=X`, `implementer.effort=Y`
- `--test-model` / `--test-effort`: `test_writer.model=…`, `test_writer.effort=…`
- `--review-model` / `--review-effort`: `reviewer.model=…`, `reviewer.effort=…`; `--no-review`: `reviewer.enabled=false`
- any `role.key=value` I give: pass it through as is.

Models: `sonnet`, `opus`, `haiku`, `fable` (each means the newest model of that family; pass a full ID such as `claude-sonnet-5` only if I name a specific older version) or `inherit` (this session's model). Efforts: `low`, `medium`, `high`, `xhigh`, `max`, or `inherit`. `ck agents` refuses anything else; if it does, ask me rather than guess.

How each mode applies them:
- **Whole spec:** pass the `roles` object from `ck agents --json` as `agents` in the workflow args (step 6). Planning and merge steps keep their own low effort.
- **One task:** you can't change your own model mid-session. If the implementer (or the test_writer, for a test-track task) is `inherit` for both model and effort, implement the task yourself as usual. Otherwise launch the Workflow tool with an inline script that runs one `agent()` with that role's `{ model, effort }` (omit any that are `inherit`) and a prompt to implement task N of the spec in the current checkout, following `.claude/commands/spec/implement.md` from "Implementation Guidelines" on. If the reviewer is enabled and the task's gate passed, the same script then runs a reviewer `agent()` with the reviewer's `{ model, effort }`: it reads the task's changes (`git diff` against where the task started) and the spec, does not edit, and returns approve or blocking findings; on findings, one implementer `agent()` revises them (up to the reviewer's `rounds`) and the reviewer looks again. Relay the implementation Output and the review verdict to me. Without the Workflow tool, use the Agent tool with the `ck-*` agent types instead (see `.claude/workflows/without-workflow-tool.md`).

## Whole Spec: The Workflow

1. **Resolve the spec** in `specs/`. If it only exists in `.claude/specs/`, tell me to run `ck migrate` and stop.
2. **Check the setup.** If `.claude/workflows/spec-implement.js` or `.claude/workflows/spec-implement-brief.md` is missing, tell me to run `ck upgrade` and stop. Read `specs/ck.json`; if `"verify"` is empty, tell me, since the gate needs it.
3. **Lint the plan:** `ck lint <spec>`.
   - **A dependency cycle or verify-order cycle:** don't launch. Show me the cycle and the suggested relink (for a verify-order cycle: make the task a foundation task with a smoke Verify, and have the test task verify the later task that its tests need). Offer to apply the relink in tasks.md; if I agree, apply it, re-run `ck lint`, and continue. A spec bug like this would otherwise halt the run hours in.
   - **Dependencies without a stated reason:** list them. Each one serializes the run. Ask whether to drop the ones with no code, data, test, or shared-file reason before launching.
   - **The critical path:** report it, with the number of open tasks. It is the floor on wall-clock time however many agents run.
4. **Plan.** Run `ck plan <spec> --json` and show me a short summary: the tasks remaining, how many are already Done (skipped), how many can start right away, and the critical-path length.
5. **Choose the merge target:**
   - **Default: an integration branch.** Run `ck worktree integration <spec>`. It creates or reuses `.claude/worktrees/<spec>-integration` on branch `integrate/<spec>`, branched from the main checkout's current branch. Then set `into` = `integrate/<spec>` and `targetDir` = `<main checkout>/.claude/worktrees/<spec>-integration`. Task worktrees branch from it and every task merges into it, so the main branch is never touched mid-run. I fast-forward main at the end.
   - **If I asked to merge directly:** set `into` = the main checkout's current branch and `targetDir` = the main checkout. The main checkout must have no uncommitted changes to tracked files; if it has some, stop and tell me.
6. **Launch.** Without the Workflow tool: run `ck agents sync` (with the same `--override`s), follow `.claude/workflows/without-workflow-tool.md` with the values below, tell me it's running and that progress is in the task list, and report as in step 9 when it finishes. Otherwise launch the Workflow tool (a fresh run, not `resumeFromRunId`) with `scriptPath` set to the absolute path of `.claude/workflows/spec-implement.js` and `args` as a JSON object:
   ```json
   {
     "spec": "<spec>",
     "root": "<absolute main checkout>",
     "into": "<target branch>",
     "targetDir": "<absolute checkout that has the target branch>",
     "wave": "<wave from ck plan>",
     "titles": "<titles from ck plan>",
     "tracks": "<tracks from ck plan>",
     "maxRetries": 1,
     "onFailure": "continue",
     "maxConcurrent": null,
     "fullGateEvery": 10,
     "agents": "<the roles object from ck agents --json>"
   }
   ```
   - **agents:** see Agents above. Report the roles you passed (implementer, test_writer, reviewer) when you tell me it's running.
   - **onFailure:** by default a failed task doesn't stop the run. Tasks that don't depend on it keep starting, its dependents never start and are reported, and a red target branch always halts. Use `"onFailure": "halt"` if I asked to stop at the first failure.
   - **maxConcurrent:** set it (for example 4) if I said the test suite is memory-heavy.
7. **Tell me it's running:**
   - Each task starts as soon as its own dependencies are merged; waves only group the progress display.
   - After every task, a cheap planning step re-reads the plan from tasks.md on the target. If we fix tasks.md mid-run (for example, removing a spurious dependency, committed to the target branch), it takes effect at the next step, with no restart.
   - Each merge is checked with `ck gate <spec> --task N` on the target. The full `ck gate <spec>` runs every `fullGateEvery` merges and once at the end.
   - I can watch with `/workflows`.
8. **Questions while it runs** are for you, the orchestrator. Task agents are told to ignore chat messages and stick to their task.
9. **When it returns,** report:
   - tasks merged, failed (with reasons and blockers), and not started (and why);
   - tests agents believe are wrong, and files touched outside their task;
   - the timing: total wall-clock, minutes per wave group (dependency levels, a display label only), and the slowest tasks;
   - if it halted or refused, the reason.
   
   Then give me the next step: fix the blockers (or answer the wrong-test questions), then run `/spec:implement <spec>` again. That is a fresh run: Done tasks are skipped and worktrees and the integration branch are reused. Don't use `resumeFromRunId`, because its cached planning steps would be stale.
10. **Landing the integration branch:** once I'm happy (ideally once every task is Done and `ck gate <spec>` passes in the integration worktree), tell me the command to land it: `git merge --ff-only integrate/<spec>` from the main checkout. If main moved in the meantime, use `git merge integrate/<spec>` instead. Afterwards: `git worktree remove .claude/worktrees/<spec>-integration && git branch -d integrate/<spec>`. Don't run these yourself unless I ask.

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
- An impl task's verifying tests come from test tasks in your Dependencies and are already merged into your branch. If they're missing, stop and report it.

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
5. Next recommended task: the first of `ck plan <spec> --json`'s `ready` list
