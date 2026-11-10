# Test Plan: Principles and Arbiter for Spec Runs

## Strategy

The feature has three kinds of behaviour, and each gets the level that can actually fail for it.

| Layer | What it covers | Level | Boundary |
|---|---|---|---|
| `ck` CLI and files | `ck lint`, `ck plan`, `ck agents`, `ck stats`, `ck principles`, `ck arbiter`, `decisions.md`, `specs/PRINCIPLES.md` | Integration | Real `python -m claude_kiro.cli` subprocess (or `CliRunner` where the file already uses it), real temp directories, real git |
| The guard | Story 9 and D11's lane | Integration through the hook | Real `execute_hook("pre-tool-guard", ...)`, real `bash -c` for shell forms, real git checkouts |
| Workflow control flow | Stories 4-8, 11, 13 | Workflow integration | Real `plugin/workflows/spec-implement.js` and `spec-create.js` under the fake runtime, with every `ck` step they write run through the real CLI against a real temp repo (`exec_ck`) |
| Agent judgment and skill text | What `/spec:plan` asks, what an arbiter concludes | Contract only | The real shipped `SKILL.md` and prompt text; see Not Tested |

**Integration-first, as applied here.** A criterion that touches a file, the CLI, git, the guard or a workflow step is tested at that boundary. The only stubs are the LLM agents (the fake runtime's `agent`) and the wall clock, which the project does not own. Nothing in `claude_kiro` is mocked. Fake agents answer as a model would; every deterministic step around them (validating a ruling, counting limits, writing `decisions.md`, landing a branch) is the real code.

**Unit level** is used only through the pure decision seams in the Harness Contract, and only by property tests. They carry the safety rules (ruling validity, limits, tier, lane, spec-edit refusals) so a run of 200 examples stays cheap. Each seam has one integration case proving the boundary calls it (TC-133 to TC-136).

**Selection.** Workflow scenarios are written as pytest cases that drive the bridge, not as new `tests/js/*.test.mjs` files. A `.test.mjs` file runs whole, so it cannot be split per task, which is exactly the Verify-scope problem Story 12 prevents. In pytest every case is named `test_tc<N>_...` or `test_p<N>_...`, so a task's **Verify:** line selects it with `-k tc<N>_`. `-k tc3_` does not match `tc13_`.

**Fails first.** Project modules are imported inside test bodies, as `tests/guard_helpers.py` does, so every file collects before the feature exists and each case fails on the missing behaviour.

**Branch.** All of this runs on `integrate/run-principles` (D17). Cases marked **[UC]** depend on code that exists only on `integrate/unattended-correctness`: `src/claude_kiro/graph.py` (`ck graph`, invokes and provides edges), `**Provides:**` parsing in `spec_parser.py`, lint's `commandEdges`, `ck gate --json` `failures[]` with `missingCommand`, and the `testConflicts` result list. They cannot pass on master.

## Test Infrastructure

### What exists

- `tests/conftest.py`: `git` helper, `no_network` fixture, the `serial` marker.
- `tests/landing_bridge.py`: the `bridge` fixture (`bridge.run`, `bridge.create`, `bridge.ready`, `bridge.ladder`), `dags()`, `git_repo()`, `write_spec()`, `ck_plan()`.
- `tests/js/bridge.mjs`, `tests/js/fake_project.mjs`, `tests/js/fake_create.mjs`, `tests/js/load_workflow.mjs` (`runWorkflow` with `scriptDate`), `tests/js/load_decisions.mjs`, `tests/js/virtual_clock.mjs`, `tests/js/emit_labels.mjs`.
- `tests/guard_helpers.py`: `hook_input`, `pre`, `run_bash`, `commit_on_base`; `tests/test_guard.py`: `decision`.
- `tests/fixtures/hook_inputs/`: recorded hook inputs. `workflow-agent-pretooluse.json` has `agent_type: "workflow-subagent"`.
- `tests/test_lint.py`: `block`, `write_spec`, `ck`. `tests/test_lint_verify_properties.py` is the model for a brute-force lint oracle.
- `tests/test_agents.py`: `home`, `ck`, `roles`. `tests/stats_helpers.py`: fake Claude home, `ck_sub`, `stats`.
- `src/claude_kiro/properties.py` matches the P-n rows here against `PROPERTIES` maps in the test files. Every property file must carry that map.
- Hypothesis 6.170 is a dev dependency. Node 22 runs the JS. There is no `package.json` and no fast-check.

### What must be added

1. **`tests/arbiter_helpers.py`** (new, test-owned). Holds:
   - `arbiter_project(tmp_path, *, tasks=None, principles=DEFAULT, plan=DEFAULT, requirements=DEFAULT) -> ArbiterProject`: a real git repo on `main` with `integrate/demo` checked out at `.ck/worktrees/demo-integration`, `specs/PRINCIPLES.md`, `specs/ck.json`, and `specs/demo/{PLAN,requirements,design,test-plan,tasks}.md`. Attributes: `root`, `target`, `spec_dir`, `exec_ck` (`{"cmd": [sys.executable, "-m", "claude_kiro.cli"], "cwd": str(root)}`), `run_args` (`root`, `into`, `targetDir`).
   - `arbiter_caller(project, spec="demo", phase="run", monkeypatch=None, checkout=None) -> dict`: the hook-input keyword arguments (and any environment or checkout set-up) that make the real hook see the real arbiter. **This is the only place the suite depends on how the arbiter is identified.** Its body comes from design.md's identity mechanism; the guard tests do not change with it.
   - `decisions_entries(project) -> list[dict]`, parsed by `ck arbiter log demo --json`, never by regex in the test.
   - Strategies: `claims()`, `rulings(principles)`, `histories()`, `spec_paths(spec)`, `justification_lines()`, `requirements_docs()`, `arbiter_failures()`.
2. **`tests/js/fake_project.mjs`** gains options, all defaulting to absent so existing suites behave as before:
   - `rulings: {n: [answer, ...]}`: answers of `arbiter <n>` and `arbiter <n> risky`, in order. An answer may be `null`, `'throw'`, or any object.
   - `auditFindings: {n: answer}`: the answer of `audit <n>` (default no problems).
   - `arbiterStepFailures: {record: true}`: the named `arbiter-` thin step reports a failed command after the real landing has run, so TC-155 can test a failed record.
   - `arbiterEdits: {n: {files: {path: text}}}`: what the fake `arbiter <n>` agent really writes and commits in the checkout its prompt names. Written with real git, so the landing is real.
   - `reviews` entries may carry `{finding: 'serves-no-requirement'}`.
   - Labels starting `arbiter-` run the one `ck ... arbiter ...` line in their prompt, with the prompt's one fenced JSON block as stdin, through `execCkRun`, and return its parsed JSON. They throw if `execCk` is absent, so a scenario cannot silently skip the real CLI.
3. **`tests/js/bridge.mjs`** gains: `{op:'call', name, args}`, which calls a named function from the decisions block of `spec-implement.js` (`handOffTestRepair`); and `throwingDate: true` on `run` and `create`, passing a `Date` and a `Math.random` that throw.
4. **`tests/js/fake_create.mjs`** gains `review: should-exist`, `arbiter: refuted <id>`, `arbiter: drift`, `arbiter: cut <id>` and `apply-cuts` answers through `overrides`.
5. **`tests/js/emit_labels.mjs`** must emit every new label so TC-137 sees them.
6. **Fixture** `tests/fixtures/hook_inputs/arbiter-agent-pretooluse.json`: a captured real hook input for the arbiter caller, added with the capture script's output, so `arbiter_caller` is checked against what Claude Code really sends. Task 1 derives a placeholder, and Task 91 replaces it with the real capture (TC-160).

### Existing tests that this feature must migrate

These encode behaviour the requirements' **Supersedes** list replaces. A grep for the ladder and confirmation found them; each needs triage by an explicit test-track task that lists the file under its **Files:**, so the guard treats the change as owned. None may be deleted to get a pass.

- `tests/js/guard_ladder.test.mjs` and `tests/test_guard_ladder_properties.py` (guard ladder and P-15).
- `tests/js/spec_implement.test.mjs` (the issue #8 confirmation cases, around lines 101-127).
- `tests/test_guard_park.py`, `tests/test_guard_agent_mode_docs.py`, `tests/test_stats_interruptions.py`, `tests/test_stats_submit.py` (hits to triage; they may only use the ladder's labels).
- `tests/js/spec_create.test.mjs` and `tests/js/spec_review_frozen.test.mjs` (their stub throws on unknown labels, and their default run has a refuted finding, so the new arbiter steps need default answers in `fake_create.mjs`).
- `tests/test_plugin.py` (`AGENTS` gains `arbiter` and `auditor`; this also makes the guide assertion require `spec:arbiter`).
- `tests/test_worktree_land.py::test_red_batch_bisects_to_the_culprit` (`len(gates) < 5`, "a green left half proves the right half red without re-gating it"): moves to the 2n-1 bound and asserts the right half's own gate (Story 15).
- `tests/test_unattended_smoke.py`: TC-178's scripted agent resets after its refused merge (commit a64c0aa). The shared `tc176_run` fixture keeps that reset, so the existing TC-176 and TC-178 tests stay green throughout; TC-169 gets its own `tc169_run` fixture whose agent does not reset, so it exercises the recovery (Story 14). TC-176's `landOrder` stays; TC-174 runs the same scenario without it.
- `tests/js/fake_project.mjs`: its simulated landing gains an `unreproduced` answer for TC-173. Its simulated bisection is a stand-in and need not change.

`bridge.ladder` and `agents.guard_ladder` stay valid as seams even once the workflow stops climbing.

## Test Cases

Files are abbreviated by name; all are under `tests/`. Level: **I** integration, **W** workflow integration (real script, real `ck`), **C** contract over shipped text, **E** end-to-end.

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-1 | 1.1 | I | Given no `specs/PRINCIPLES.md`, when `ck principles init --json` runs, then the file exists with all four defaults and the JSON says `created: true` | CLI, filesystem | test_principles.py |
| TC-2 | 1.1 | C | The `plan` skill runs `ck principles init` before recording decisions and tells me when it created the file | Shipped `plugin/skills/plan/SKILL.md` | test_skill_contracts.py |
| TC-3 | 1.2 | I | Given an edited `specs/PRINCIPLES.md`, when init runs, then bytes and mtime are unchanged and `created: false` | CLI, filesystem | test_principles.py |
| TC-4 | 1.3 | C | The plan skill asks directly for tie-breakers, overrides and what may change without asking, and its PLAN.md template has `## Principles` | Shipped skill | test_skill_contracts.py |
| TC-5 | 1.4 | C | The step that sets Status to Decided comes after and is conditional on a non-empty `## Principles`, and the skill says a plan with nothing to add states so | Shipped skill | test_skill_contracts.py |
| TC-6 | 1.5 | C | The skill asks for a reason before recording an override, and records it as `**Reason:** <text>` | Shipped skill | test_skill_contracts.py |
| TC-7 | 1.6 | W | Given both layers on disk, when a test conflict reaches the arbiter, then the `arbiter 1` prompt holds the text of both | Workflow, real `ck arbiter decide` | test_arbiter_run.py |
| TC-8 | 1.6 | I | Given a plan override with a reason and one without, when `ck arbiter decide` runs, then `effective` holds the plan's for the first and the project default for the second | CLI, real files | test_principles.py |
| TC-9 | 1.7 | W | Given a plan principle reserving design edits, when a ruling that names the reservation in `reservedBy` needs a design edit, then a needs-input entry appears and no `arbiter-land` runs. A ruling that leaves it out reaches the landing and is refused (TC-149) | Workflow, `ck arbiter route` | test_arbiter_run.py |
| TC-10 | 1.8 | I | Given a plan that grants the arbiter requirements changes, when `ck arbiter route` gets an outcome-requirements ruling, then the lane is `needs_input` | CLI | test_principles.py |
| TC-11 | 1.9 | W | Given `specs/PRINCIPLES.md` is missing, then a directory, then unreadable (mode 000), when a claim arises, then no `arbiter` label runs and the entry names the file | Workflow, real files | test_arbiter_run.py |
| TC-12 | 1.10 | W | Given no PLAN.md, then a PLAN.md with no section, when the arbiter rules, then it runs and the entry records the plan layer absent | Workflow, `decisions.md` | test_arbiter_run.py |
| TC-13 | 1.11 | I | Given a PLAN.md with an empty `## Principles`, when `ck lint demo --json` runs, then `planPrinciples` names the plan and exit is 0; a clean spec has an empty list | CLI | test_justification_lint.py |
| TC-14 | 2.1 | C | The plan skill asks what breaks without each In-scope item and its template has the `*Without it:*` note | Shipped skill | test_skill_contracts.py |
| TC-15 | 2.2 | C | The skill offers the cut when nothing concrete breaks, and removes the item only on acceptance | Shipped skill | test_skill_contracts.py |
| TC-16 | 2.3 | W | In create mode the Requirements prompt tells the writer to put a `**Without it:**` line on every story, carrying the plan item's reason | Real `spec-create.js` prompt | test_arbiter_create.py |
| TC-17 | 2.4 | W | The Requirements prompt tells the writer a story no plan item covers gets its own line | Real `spec-create.js` prompt | test_arbiter_create.py |
| TC-18 | 2.5 | I | Given stories with a line each and criteria with none, then lint reports no justification finding | CLI | test_justification_lint.py |
| TC-19 | 3.1 | I | Given Story 2 has no line, then exit is non-zero and the output names "Story 2" and its title | CLI | test_justification_lint.py |
| TC-20 | 3.2 | I | Given lines holding only whitespace, `**`, `_ _`, then each is reported as missing | CLI | test_justification_lint.py |
| TC-21 | 3.3 | I | Given every line is non-empty, including "n/a" and "nice to have", then no finding | CLI | test_justification_lint.py |
| TC-22 | 3.4 | I | Given a line above the first story, under another heading, or in a fence, then it counts for no story | CLI | test_justification_lint.py |
| TC-23 | 3.5 | I | Given no requirements.md, then one with no stories, then no finding and the other lint keys are still reported | CLI | test_justification_lint.py |
| TC-24 | 3.6 | I | JSON lists `justifications[]` with `story` and `title`; every key in today's output is present and equal to the same spec's output with requirements.md removed | CLI | test_justification_lint.py |
| TC-25 | 3.7 | W | Given only justification findings, when the workflow launches, then it is not refused and tasks start | Workflow, real `ck plan` | test_justification_lint.py |
| TC-26 | 4.1 | W | In create mode and in review mode, `review: should-exist` runs and its prompt attacks each story's justification | Real `spec-create.js` | test_arbiter_create.py |
| TC-27 | 4.2 | W | Given a surviving cut, then review.md's section and the result's `proposedCuts` hold the story, the weakness and criteria, tasks and test-case counts | Workflow | test_arbiter_create.py |
| TC-28 | 4.3 | W | Given a surviving cut, then no revise prompt lists it and the result says it was not applied | Workflow | test_arbiter_create.py |
| TC-29 | 4.4 | W | Given an approved cut with every task Not Started, when `apply-cuts` runs and the fake agent really removes the story from all four documents, then `ck lint` runs and reports no new failure | Workflow, real lint on real files | test_arbiter_create.py |
| TC-30 | 4.4 | W | Given the removal leaves a new lint failure, then the run does not report the cut as applied and shows the finding | Workflow, real lint | test_arbiter_create.py |
| TC-31 | 4.5 | W | Given a started task, when a cut is approved, then no `apply-cuts` runs and the message says requirements are frozen and the change is mine | Workflow | test_arbiter_create.py |
| TC-32 | 4.6 | W | Given the lens finds nothing, then review.md says so | Workflow | test_arbiter_create.py |
| TC-33 | 4.7 | W | Given the lens throws, then `lensesLost` is counted and the log says coverage is partial | Workflow | test_arbiter_create.py |
| TC-34 | 4.8 | W | Given a reviewer's serves-no-requirement finding, then the task lands, the code stays, and `proposedCuts` in the result names it | Workflow, real landing | test_arbiter_run.py |
| TC-35 | 5.1 | I | `ck agents set arbiter.model fable`, each of the four keys, shows in `ck agents --json`; an invalid value gets the same message as `reviewer.model` | CLI, real config | test_arbiter_agents.py |
| TC-36 | 5.2 | I | With the four unset, resolved values equal `reviewer.risky_model` and `risky_effort`, including after changing those | CLI, real config | test_arbiter_agents.py |
| TC-37 | 5.3 | W | Safety task, guard stop and repeat claim each start at the risky model; a plain conflict on a normal task starts at the normal model | Workflow, agent opts | test_arbiter_run.py |
| TC-38 | 5.4 | W | Given an earlier arbiter call on the same claim failed, when the claim returns, then the run is at the risky tier, across a resume | Workflow, real `ck` | test_arbiter_run.py |
| TC-39 | 5.5 | W | Given a High Complexity task with no other signal, then the normal tier | Workflow | test_arbiter_run.py |
| TC-40 | 5.6 | W | Given "needs a stronger look" at the normal tier, then `arbiter 1 risky` runs once and its prompt holds the first reasoning | Workflow | test_arbiter_run.py |
| TC-41 | 5.7 | W | Given "needs a stronger look" at the risky tier, then no further call and the entry holds what was established | Workflow | test_arbiter_run.py |
| TC-42 | 6.1 | W | Given a test conflict on the first attempt, then `task 1` is followed by the arbiter, with no retry, escalation or confirmation label | Workflow event order | test_arbiter_run.py |
| TC-43 | 6.2 | W | Given a guard stop, then the arbiter runs and no `guard <k>` label does | Workflow | test_arbiter_run.py |
| TC-44 | 6.3 | W | Given blocking findings after the configured rounds on an individually reviewed task, then the arbiter runs and no `revise (model)` label does | Workflow | test_arbiter_run.py |
| TC-45 | 6.3 | W | Same, for a task flagged by a landing batch's review | Workflow | test_arbiter_run.py |
| TC-46 | 6.4, 6.11 | W | Given a plain gate failure with retries and an escalate list, then today's labels run in today's order, and the arbiter runs only after the top model failed | Workflow | test_arbiter_run.py |
| TC-47 | 6.5, 6.11 | W | Given no escalate list, then the arbiter follows the last retry | Workflow | test_arbiter_run.py |
| TC-48 | 6.6 | W | Given the retry or escalation passes, then no arbiter label runs | Workflow | test_arbiter_run.py |
| TC-49 | 6.7 | W | Given BUSY, then ERROR worktree outcomes, then no arbiter label runs | Workflow | test_arbiter_run.py |
| TC-50 | 6.8 | W | The arbiter prompt holds the stop type, claim text, evidence, task, models tried, both layers and the spec paths | Workflow prompt | test_arbiter_run.py |
| TC-51 | 6.9 | W | Given a slow arbiter on task 1, then task 1's dependent has not started when independent task 3 finishes | Workflow, virtual clock | test_arbiter_run.py |
| TC-52 | 6.10 | W | For each `arbiter_failures()` form, on a test conflict and on a gate-failure claim, a needs-input entry appears and no `guard` or confirmation label runs | Workflow | test_arbiter_run.py |
| TC-53 | 7.1 | I | A ruling with each of the four outcomes, and one with status needs a stronger look, passes `ck arbiter route`; each lacking claim, reasoning or principle fails | CLI | test_arbiter_cli.py |
| TC-54 | 7.2 | W | For each invalid form, no `arbiter-land` runs, `decisions.md` is absent, and the entry carries the arbiter's output | Workflow, real files | test_arbiter_run.py |
| TC-55 | 7.3 | W | The same claim on attempt 2, and in a second run on the same repo, is treated as the same; a different test is not | Workflow, real `ck` | test_arbiter_run.py |
| TC-56 | 7.4 | W | Given a claim returns after its ruling was applied, then no arbiter call and the entry holds the ruling and the new evidence | Workflow | test_arbiter_run.py |
| TC-57 | 7.5 | W | A second, different claim on a task with one ruling gets its own ruling | Workflow | test_arbiter_run.py |
| TC-58 | 7.6 | W | Given 3 rulings, a 4th claim makes no arbiter call and the entry lists the 3 | Workflow | test_arbiter_run.py |
| TC-59 | 7.7 | W | A normal call plus a risky re-run counts one ruling toward the limit | Workflow | test_arbiter_run.py |
| TC-60 | 7.8 | W | Given rulings from an earlier run on disk, when a new run starts, then the limits count them | Workflow, real `decisions.md` | test_arbiter_run.py |
| TC-61 | 8.1 | I | A spec-edit ruling commits on a new branch made from the integration branch and lands through `ck arbiter land`, which takes the merge-queue lock | Real git, CLI | test_arbiter_land.py |
| TC-62 | 8.2 | W | After a spec edit lands, the task resumes and `attempts` and escalations are as before the ruling | Workflow | test_arbiter_run.py |
| TC-63 | 8.3 | W | A landed edit that adds a dependency on a not-started task changes the next plan's schedule | Workflow, real `ck plan` | test_arbiter_run.py |
| TC-64 | 8.4 | I | A conflicting edit: state CONFLICT, the target's HEAD unchanged, no re-ruling | Real git | test_arbiter_land.py |
| TC-65 | 8.4 | I | A red gate on landing: state RED, HEAD unchanged | Real git, real gate | test_arbiter_land.py |
| TC-66 | 8.4 | I | A branch touching a refused path: state PROTECTED or REFUSED, HEAD unchanged | Real git | test_arbiter_land.py |
| TC-67 | 8.5 | I | An edit creating a dependency cycle is not landed | Real git, real lint | test_arbiter_land.py |
| TC-68 | 8.5 | I | An edit creating a new blocking lint finding is not landed; one that leaves an existing finding as it was may land | Real git, real lint | test_arbiter_land.py |
| TC-69 | 8.6 | I | An edit changing a Status field, then an acceptance mark, is not landed | Real git | test_arbiter_land.py |
| TC-70 | 8.7 | I | An edit moving `tc3_` out of a task's Verify: the entry's what-changed names that test | Real git, `decisions.md` | test_arbiter_land.py |
| TC-71 | 8.8 | W | A test-repair ruling with no fixer: the entry holds the exact change and why, the test file is byte-identical and no edit label runs | Workflow, real files | test_arbiter_run.py |
| TC-72 | 8.9 | W | With `testFixerAvailable: true`, the fixer receives the ruling deep-equal; `handOffTestRepair` has a single call site in the script | Workflow, shipped script | test_arbiter_run.py |
| TC-73 | 8.10 | W | A test-repair ruling on a conflict runs no confirmation label | Workflow | test_arbiter_run.py |
| TC-74 | 8.11 | W | A code ruling: the retry prompt holds the ruling text; a failing gate after it leaves the task unfinished | Workflow | test_arbiter_run.py |
| TC-75 | 8.12 | W | The re-review prompt holds the ruling; the settled finding raised again does not block | Workflow | test_arbiter_run.py |
| TC-76 | 8.13 | W | A requirements ruling: an entry with the recommendation, requirements.md byte-identical | Workflow, real files | test_arbiter_run.py |
| TC-77 | 8.14 | W | A missing-requirement finding: an entry with wording and reason, the task and dependents wait, independents continue | Workflow | test_arbiter_run.py |
| TC-78 | 8.15 | W | A ruling's entry is in `needsInput`, not `failed`, does not halt on failure, and notifies when `notify_command` is set | Workflow, real notify command | test_arbiter_run.py |
| TC-79 | 9.1 | I | The arbiter edits tasks.md, design.md and test-plan.md, changing existing text, with a task In Progress: allowed | Real hook, real files | test_arbiter_guard.py |
| TC-80 | 9.2 | I | The arbiter, in a run, changes requirements.md by an edit and by an add-only append, under each task-status mix: denied | Real hook | test_arbiter_guard.py |
| TC-81 | 9.3 | I | A test, a source file, PLAN.md, PRINCIPLES.md, `specs/ck.json`, another spec's tasks.md, `.claude/x`, `/etc/x`: denied | Real hook | test_arbiter_guard.py |
| TC-82 | 9.4 | I | `rm`, `mv`, `>`, `tee`, `sed -i`, `python -c`, `cp` onto requirements.md and a test: denied. Each substitution form, `echo "$(rm tests/x.py)"`, a backtick form of `cat`, `git commit -m "$(rm x)"`, `<(...)` and `>(...)`: denied. `git commit -m 'text $(x)'` in single quotes is not denied | Real hook | test_arbiter_guard.py |
| TC-83 | 9.5 | I | A task agent with `CK_ARBITER=1`, a `ck-arbiter` argument, or prompt text claiming the lane: classified and decided as without the claim | Real hook | test_arbiter_guard.py |
| TC-84 | 9.6 | I | An input claiming the arbiter type with no agent id, or a malformed type: the same decision as the unclaimed input | Real hook | test_arbiter_guard.py |
| TC-85 | 9.7 | I | A task agent and a main session, on requirements.md and test-plan.md, before and after implementation started: today's decisions | Real hook | test_arbiter_guard.py |
| TC-86 | 9.8 | I | A denial's reason names the file and says it is outside the arbiter's lane | Real hook | test_arbiter_guard.py |
| TC-87 | 9.1 | I | The arbiter's `git add`, `git commit` and `ck lint` in its checkout are not denied; the commit touches only allowed files | Real hook, real git | test_arbiter_guard.py |
| TC-88 | 10.1 | I | `ck arbiter record` appends one numbered entry; `ck arbiter log` returns trigger, claim, ruling, principle, what changed and commit | CLI, real file | test_arbiter_cli.py |
| TC-89 | 10.2 | I | With no file changed, then with a `/spec:create` ruling, commit is none | CLI | test_arbiter_cli.py |
| TC-90 | 10.3 | W | A ruling whose edit did not land records that and why | Workflow, real files | test_arbiter_run.py |
| TC-91 | 10.4 | I | Earlier entries are byte-identical after an append; numbers keep rising in a second process | CLI, real file | test_arbiter_cli.py |
| TC-92 | 10.5 | I | 16 concurrent `ck arbiter record` processes: 16 entries, 16 distinct numbers | Real subprocesses, file lock | test_arbiter_cli.py |
| TC-93 | 10.6 | I | No file before the first ruling; a run with no rulings leaves none | CLI, workflow | test_arbiter_cli.py |
| TC-94 | 10.7 | W | Given rulings from an earlier run, the report lists only this run's | Workflow | test_arbiter_run.py |
| TC-95 | 10.8 | I | The result's `rulings` reach `ck stats show --json` as counts by trigger, outcome and tier | Real `ck stats` over a real run file | test_arbiter_stats.py |
| TC-96 | 10.9 | W | With `specs/demo` made read-only, the report says the entry was not written and a needs-input entry appears | Workflow, real files | test_arbiter_run.py |
| TC-97 | 11.1 | W | A refuted finding gets one `arbiter: refuted <id>` and is neither dropped nor majority-voted | Real `spec-create.js` | test_arbiter_create.py |
| TC-98 | 11.2 | W | A finding ruled standing is in the revise prompt; one ruled refuted stays listed as refuted with the ruling | Workflow | test_arbiter_create.py |
| TC-99 | 11.3 | W | With PLAN.md, `arbiter: drift` runs after Revise, with fixes and with none; with no PLAN.md it does not | Workflow | test_arbiter_create.py |
| TC-100 | 11.4 | W | Each cut gets an `arbiter: cut <id>`; a rejected cut is absent from the report and present in `decisions.md` | Workflow, real files | test_arbiter_create.py |
| TC-101 | 11.5 | I | The create-phase arbiter corrects requirements wording with every task Not Started: allowed | Real hook | test_arbiter_guard.py |
| TC-102 | 11.6 | W | A drift ruling that would change a decision is not applied and appears in review.md and the result | Workflow | test_arbiter_create.py |
| TC-103 | 11.7 | I | With a started task the create-phase arbiter is denied requirements.md, and the frozen rule still governs the reviser | Real hook, `spec_review_frozen` | test_arbiter_guard.py |
| TC-104 | 11.8 | W | An arbiter change with Revise applying no fixes still runs the consistency check | Workflow | test_arbiter_create.py |
| TC-105 | 11.9 | W | Create-phase rulings start normal, re-run once at risky, get one ruling per finding, fail validity on the same forms as TC-54, and reach `decisions.md` with commit none | Workflow, real files | test_arbiter_create.py |
| TC-106 | 11.10 | I | The arbiter editing PLAN.md, in create and in run: denied | Real hook | test_arbiter_guard.py |
| TC-107 | 11.11 | W | With no refuted findings and no cuts, no `arbiter: refuted` or `arbiter: cut` label runs | Workflow | test_arbiter_create.py |
| TC-108 | 11.12 | W | When the arbiter throws or returns junk, findings stay as they stood, the check is reported as not run and the result is not Ready | Workflow | test_arbiter_create.py |
| TC-109 | 12.1 | I | [UC] `ck lint --json` `verifyScope.checked` lists each not-Done task with its due suites and selections; Done tasks are absent | CLI, real test files | test_verify_scope_lint.py |
| TC-110 | 12.2 | I | Two test tasks share a file; the first's Verify has no filter and is due while the second's task is not Done: a finding names the task and suite | CLI | test_verify_scope_lint.py |
| TC-111 | 12.3 | I | [UC] A selected test invokes a command a later task's `**Provides:**` names: a finding names task, test and provider | CLI, real parse | test_verify_scope_lint.py |
| TC-112 | 12.4 | I | [UC] Not due, filtered out, a direct dependency and a transitive dependency: four sub-cases, no finding | CLI | test_verify_scope_lint.py |
| TC-113 | 12.5 | I | A finding makes `ck lint` exit non-zero and appears in `ck plan --json` `verifyScope` | CLI | test_verify_scope_lint.py |
| TC-114 | 12.5 | W | A plan with `verifyScope` refuses launch; no task agent runs | Workflow | test_verify_scope_lint.py |
| TC-115 | 12.6 | W | The launch block sends one claim per distinct problem to the arbiter; a wiring edit lands under Stories 7, 8 and 10 | Workflow, real git | test_verify_scope_lint.py |
| TC-116 | 12.7 | W | After a fix lint re-runs; a remaining finding keeps the launch blocked and is shown with the proposed fix | Workflow, real lint | test_verify_scope_lint.py |
| TC-117 | 12.8 | I | Running `ck lint` directly starts no agent; text and JSON each name task, suite or test, and provider where it applies | CLI | test_verify_scope_lint.py |
| TC-118 | 12.9 | I | A Verify using `$(...)` or a script: `verifyScope.notChecked` names the task and exit is 0 | CLI | test_verify_scope_lint.py |
| TC-119 | 12.10 | I | [UC] A problem already in `verifyCycles` or `commandEdges` appears once across the output | CLI | test_verify_scope_lint.py |
| TC-120 | 13.1 | W | The audit runs after the last verifying test task merged and before the impl task's first attempt, and reads the selected tests | Workflow event order | test_arbiter_audit.py |
| TC-121 | 13.2 | W | No problem: the task starts and no arbiter label runs | Workflow | test_arbiter_audit.py |
| TC-122 | 13.3 | W | A problem: the arbiter runs before any `task N` label, and the ruling is recorded | Workflow, real `ck` | test_arbiter_audit.py |
| TC-123 | 13.4 | W | The shipped `plugin/agents/auditor.md` frontmatter lists exactly `Read, Grep, Glob`, the generated `ck-auditor.md` keeps that list (generated into a temp directory, never read from `.claude/`), and the workflow starts the audit with `agentType` from `args.auditorAgent` | Shipped agent, generated agent, workflow | test_arbiter_audit.py |
| TC-124 | 13.5 | W | The audit throws: the task starts and the report notes the missed audit | Workflow | test_arbiter_audit.py |
| TC-125 | 13.6 | W | A retried and an escalated task has one audit | Workflow | test_arbiter_audit.py |
| TC-126 | 13.7 | W | No verifying tests, and a test-track task: no audit | Workflow | test_arbiter_audit.py |
| TC-127 | NFR Bounded cost | W | Repeated claims across 3 runs: at most 6 calls on a task and 2 on a claim | Workflow, real `ck` | test_arbiter_nfr.py |
| TC-128 | NFR Runtime | W | Both workflows run scenarios with the arbiter, audit and create rulings under a throwing `Date` and `Math.random` | Fake runtime | test_arbiter_nfr.py |
| TC-129 | NFR Lint safety | I | A canary module raising on import, under `no_network`, with `ck lint`: no import and no connection | CLI, canary | test_arbiter_nfr.py |
| TC-130 | NFR Lint performance | I | A generated 108-task spec: `ck lint` under 10 s. Marked `serial` | CLI | test_arbiter_nfr.py |
| TC-131 | NFR Compatibility | W | A spec with verifying tests, no claims and no findings: today's labels in today's order, with `audit task <n>` and its thin `ck` step inserted before each verified impl task's first attempt, no other new label, no `arbiter` label and no `decisions.md` | Workflow | test_arbiter_nfr.py |
| TC-132 | NFR Determinism | I | Two `ck lint --json` runs on a tree with findings are byte-identical | CLI | test_arbiter_nfr.py |
| TC-133 | 7.1, 5.3 | I | `ck arbiter decide` equals `arbiter.decide_call` on the same claim and history, shown by a fixture where only a recorded history changes the answer | CLI, seam | test_arbiter_cli.py |
| TC-134 | 9.1, 9.3 | I | The real hook and `guard_policy.arbiter_may_edit` agree on an allowed and a denied path | Hook, seam | test_arbiter_guard.py |
| TC-135 | 7.2, 8.8 | I | `ck arbiter route` equals `arbiter.route_ruling` for one valid and one invalid ruling | CLI, seam | test_arbiter_cli.py |
| TC-136 | 3.1, 12.2 | I | `ck lint` and `lint.justification_findings` / `lint.verify_scope` agree on one failing fixture each | CLI, seam | test_verify_scope_lint.py |
| TC-137 | 10.8 | I | Every label the workflows emit parses to a known role, including `arbiter`, `audit` and the `arbiter-` steps | Real scripts, `parse_label` | test_stats_label_corpus.py |
| TC-138 | 8.1 | I | While a task landing process holds the landing lock (its gate is slow on purpose), an arbiter landing waits on the lock, then both have merged | Real git, merge queue, real lock | test_arbiter_land.py |
| TC-139 | 1.1 | C | The create, review and implement skills run `ck principles init` (implement on the target checkout, with `--commit`) and tell me when it created the file | Shipped skills | test_arbiter_skill_contracts.py |
| TC-140 | 1.1, 1.2 | I | Given a checkout with no file, when `ck principles init --commit --json` runs, then the file is committed alone on the checkout's branch and `commit` is set; with the file present, no commit and `commit` is null | CLI, real git | test_principles.py |
| TC-141 | 1.12, 11.12 | W | Given `specs/PRINCIPLES.md` is unusable during `/spec:create`, with refuted findings present, then no arbiter label runs, the findings stand as they were, review.md and the report name the file and `ck principles init`, and the result is not Ready | Workflow, real `ck` | test_arbiter_create.py |
| TC-142 | 13.8 | W | An audit problem whose arbiter call throws, returns nothing usable, or asks because the principles file is missing: the task starts, `missedAudits` lists it, and no needs-input entry appears | Workflow, real `ck` | test_arbiter_audit.py |
| TC-143 | 12.6 | W | [UC] Two tests in one task call the same later command: one `lint` claim lists both, one landed fix clears both, no needs-input entry appears; a finding on another task that the fix cleared gets no ruling | Workflow, real git, real lint | test_verify_scope_lint.py |
| TC-144 | 12.11 | W | [UC] A finding first appears after a test task merges: it goes to the arbiter as a `lint` claim before the task it names starts, that task is held until the claim settles, and a later plan does not rule on it again | Workflow, real git, real lint | test_verify_scope_lint.py |
| TC-145 | 12.9 | I | A Verify selecting a test file not yet on disk: `notChecked` names the task and exit is 0 | CLI | test_verify_scope_lint.py |
| TC-146 | 7.3, 7.4, 7.5 | W | After a ruling on a review deadlock, a different blocking finding on the same file gets its own ruling; the same finding raised again unsettled makes no call and is an entry with the ruling; a deadlock mixing a ruled finding and a new one sends the ruled finding to me with its ruling and only the new one to the arbiter | Workflow, real `ck` | test_arbiter_run.py |
| TC-147 | 6.12, 12.12, 13.1 | C | The implement skill and `without-workflow-tool.md` run the read-only audit before the first attempt of a single-task or no-Workflow run, and say a claim there, including an audit problem, is a needs-input entry with no guard ladder and no confirmation agent, and the skill refuses a single-task run whose task a `verify-scope` entry names | Shipped skill, shipped resource | test_arbiter_skill_contracts.py |
| TC-148 | 11.9, 11.12 | W | Each create-phase arbiter step is started with `agentType` from `args.arbiterAgent` and tier model and effort from `args.agents.arbiter`, by plain `agent` (its prompt holds no `ck progress` line), and its `ck arbiter` steps run through `runnerAgent`; with no `arbiterAgent`, the steps with subjects are lost and the result is not Ready | Workflow, agent options | test_arbiter_create.py |
| TC-149 | 1.7, 8.4 | I | A branch changing a design.md the plan reserves, with a ruling that leaves `reservedBy` empty: state REFUSED, HEAD unchanged | Real git | test_arbiter_land.py |
| TC-150 | 5.6 | I | On route `rerun`, `ck arbiter settle` keeps the worktree and resets its branch to the starting commit, so a partial commit from the first attempt is gone; on `test_repair`, `code` and `needs_input` the worktree and branch are removed; on `spec_edit` they stay | CLI, real git | test_arbiter_cli.py |
| TC-151 | 8.3, 13.1, 13.7 | W | A landed spec edit that gives a not-started impl task its first verifying test: the audit runs for it; one that removes its last: no audit | Workflow, real `ck plan` | test_arbiter_audit.py |
| TC-152 | 10.9 | I | With the landing lock held past `--lock-wait`, `ck arbiter record` exits non-zero and writes nothing | CLI, real lock | test_arbiter_cli.py |
| TC-153 | 1.9 | I | Given `specs/PRINCIPLES.md` missing, a directory, a broken symlink, mode 000, non-UTF-8: `ck arbiter open` returns `ask`, `missing` names the file and no worktree is made; a readable or empty file may rule | CLI, real files | test_principles.py |
| TC-154 | 7.5, 7.6 | I | With 3 rulings on a task in a seeded ledger, `ck arbiter open` asks with `earlier` listing the 3; 3 rulings on another task do not count | CLI, real ledger | test_arbiter_cli.py |
| TC-155 | 10.9 | W | A spec edit lands and its record then fails: the task does not resume and a needs-input entry says the edit landed | Workflow, real landing | test_arbiter_run.py |
| TC-156 | 8.1, 9.1, 9.2 | I | Real `ck arbiter open` returns a worktree; an Edit of its `specs/demo/tasks.md` through the real hook with the arbiter's identity is allowed, an Edit of `requirements.md` is denied; real `git add` and `git commit` run, and `ck worktree land demo arbiter<k>` merges | Real hook, real git, CLI | test_arbiter_land.py |
| TC-157 | 5.3, 5.5 | W | A task matching `risk_paths` with no Risk tag starts at the risky tier; a High and a Low Complexity task with no other signal get the same tier | Workflow, agent opts | test_arbiter_run.py |
| TC-158 | 11.6, 11.12 | W | A drift ruling with a `requirements` item lands in Needs Your Decision and review.md's Result is `Ready after decisions`; a lost drift step sets it to `Needs work`, matching the workflow's `result` | Workflow, real files | test_arbiter_create.py |
| TC-159 | 1.1, 11.9 | C | The create and review skills run `ck agents --json` and pass `arbiterAgent`, `runnerAgent` and `agents.arbiter` | Shipped skills | test_arbiter_skill_contracts.py |
| TC-160 | 9.5, 9.6 | I | The captured real hook input of a Workflow agent started with `agentType` `spec:arbiter`, and of the vendored `ck-arbiter`, has no `_derivedFrom` key, and `is_arbiter_caller` is true on it | Recorded real hook input | test_arbiter_capture.py |
| TC-162 | 14.1, 14.2, 14.4 | I | Given a managed integration checkout with ck's hooks installed and a branch whose only change adds `.claude/CLAUDE.md` on top of `integrate/demo`, a raw `git merge --no-verify` of it fast-forwards, is refused, and leaves `A .claude/CLAUDE.md` staged; when `ck worktree land demo <n> --json` runs for a clean task, then the task is MERGED, `recovered.paths` lists `.claude/CLAUDE.md`, `recovered.mergeInProgress` is false, `recovered.head` equals `integrate/demo` before the landing, and `.claude/CLAUDE.md` is not in `integrate/demo` | Real git, real hooks, CLI | test_land_recovery.py |
| TC-163 | 14.1, 14.2 | I | As TC-162 with a non-fast-forward merge: the refused merge leaves MERGE_HEAD; the landing merges, `recovered.mergeInProgress` is true, and MERGE_HEAD, MERGE_MSG and AUTO_MERGE are gone | Real git, real hooks, CLI | test_land_recovery.py |
| TC-164 | 14.3 | I | An untracked file and an ignored file in the integration checkout before the refused merge are byte-identical after the recovering landing | Real git, real files | test_land_recovery.py |
| TC-165 | 14.3 | I | With a dirty tracked file in the main checkout, `ck worktree merge demo <n>` into the main branch exits non-zero with today's "has uncommitted changes to tracked files" message, and the file is unchanged | Real git, CLI | test_land_recovery.py |
| TC-166 | 14.5 | I | Given an integration checkout with a hand edit to a tracked file and a merge in progress made with the hooks' token (no refusal recorded), `ck worktree land` exits non-zero naming "no refused ref update is recorded at this head", and the edit and MERGE_HEAD are untouched; the same with a refusal recorded at an older head (the branch moved since), and with one recorded from another checkout | Real git, real hooks, CLI | test_land_recovery.py |
| TC-167 | 14.1, 14.4 | I | After a refused merge, with the checkout's `index.lock` held, `ck worktree land` exits non-zero naming the target and the git error, `integrate/demo` is unchanged, and `protect.refusal_for` still returns the record; once the lock is removed the next landing recovers | Real git, real lock file, CLI | test_land_recovery.py |
| TC-168 | 14.1, 14.5 | I | A refused raw merge, fast-forward and non-fast-forward, leaves a record `protect.refusal_for(root, "refs/heads/integrate/demo")` returns with that head and the checkout's realpath; a tokened landing (a `ck worktree land` of a clean task into a clean checkout) leaves none. Hook side only: no `tc168_` test depends on the recovery, so none runs `ck worktree merge` or `ck worktree land` after a refused merge | Real git, real hooks | test_land_recovery.py |
| TC-196 | 14.1, 14.2 | I | After a refused raw merge, `ck worktree merge demo <n> --into integrate/demo` succeeds and prints `restored <checkout> to <head>: discarded .claude/CLAUDE.md` first (a fast-forward refusal, so no `; a merge was in progress`; after a non-fast-forward refusal the line ends with it); `protect.refusal_for` returns no record after the recovery | Real git, real hooks, CLI | test_land_recovery.py |
| TC-197 | 14.1, 14.2 | I | After a refused raw merge, `ck worktree land demo arbiter<k> --into integrate/demo --gate --json` for a spec edit made through `ck arbiter open` restores the checkout (its entry carries `recovered` naming `.claude/CLAUDE.md`) and lands the edit; after another refused merge, `ck arbiter record` commits `decisions.md` on a restored checkout and its output carries `recovered` naming `.claude/CLAUDE.md`; `integrate/demo` never holds `.claude/CLAUDE.md` | Real git, real hooks, CLI | test_land_recovery.py |
| TC-169 | 14.1, 14.2 | E | On its own `tc169_run` fixture, the TC-176 run with TC-178's scripted agent making its refused raw merge and not resetting after it (the shared `tc176_run` fixture keeps its cleanup): the run goes on, a later landing entry carries `recovered` naming `.claude/CLAUDE.md`, and the run-end `ck worktree audit` is clean | Real workflow under the fake runtime, real `ck`, real hooks | test_unattended_smoke.py |
| TC-170 | 15.1, 15.2 | I | A batch of three tasks where only the third fails its own Verify: the third is RED, `gates` holds a gate whose `tasks` is the right half, and the RED task's `checkTail` and `failures` equal those of the gate whose `tasks` is exactly that task, which is red | Real git, real `ck gate`, CLI | test_land_bisect.py |
| TC-171 | 15.1, 15.2, 15.3 | I | A batch whose two tasks complete the spec, where the first breaks `tests/test_other.py`, which only the full suite in `verify` runs, and each task branch commits its own `**Status:** Done` in `tasks.md` as implementers do: the batch gate is red, no task is RED, both are MERGED, and `unreproduced` carries the batch gate's failure naming `tests/test_other.py`. The gate on the last single task, which sees the spec complete on the target, ran task-scoped: no `tests/test_other.py` in its failures, and no gate after the batch gate ran `verify` | Real git, real `ck gate`, CLI | test_land_bisect.py |
| TC-172 | 15.3 | I | A batch gate that fails once (a test that fails on its first run, keyed by a file it writes) and then passes, with task branches that commit their own Done: no task is RED, both tasks are MERGED and `unreproduced` is set | Real git, real `ck gate`, CLI | test_land_bisect.py |
| TC-173 | 15.3 | W | A landing answers with `unreproduced`: the run calls an attributed gate with `kind` `unreproduced` and label `full gate @<n> (unreproduced landing)`; red, a fixer gets the attributed entry and a recheck runs; no `fix <k> (Task <n> red on landing)` label runs; the landing entry has `unreproduced: true`; a green attributed gate runs no fixer | Workflow, real `ck` for the attributed gate | test_land_unreproduced_workflow.py |
| TC-174 | 15.2, 15.3 | E | The TC-176 scenario without `landOrder`: Task 2 lands in the batch that completes the spec; the landing reports `unreproduced` and no RED task; the attributed gate names `test_tc1_add` on Task 2; the fixer prompt carries it; the final gate is green | Real workflow under the fake runtime, real `ck` | test_unattended_smoke.py |
| TC-198 | 15.2 | I | A batch of one task that completes the spec, whose branch commits its own Done, while `tests/test_other.py` (run only by the full suite) fails because of an earlier task already on the target: the task's only gate ran task-scoped (no `tests/test_other.py` in it, no `verify` run), the task is MERGED, and there is no RED task and no `unreproduced` | Real git, real `ck gate`, CLI | test_land_bisect.py |
| TC-175 | Bounded landing cost, 15.2 | I | Batches of 4, with task branches that commit their own Done, with one culprit, two culprits, and none but a batch-only failure each run at most 7 gates; in the batch-only case no task is RED | Real git, real `ck gate`, CLI | test_land_bisect.py |
| TC-161 | 7.2, 13.3, 13.8 | W | An audit problem whose arbiter returns a malformed ruling (no principle, or an unknown outcome): no ruling is applied or recorded, the task does not start, a needs-input entry carries the arbiter's output, and the task is not in `missedAudits` | Workflow, real `ck` | test_arbiter_audit.py |

## Properties

Library: **Hypothesis** for all of them. JS decisions are reached through the bridge, since the project has no JS dependency set and no fast-check. Settings: `@settings(derandomize=True, max_examples=200, database=None, deadline=None)` for pure seams; `max_examples=30` for rows marked (W) or (G), which spend real subprocesses or git. Each file carries a `PROPERTIES` map.

| ID | Requirement | Property | Generator domain | Expected decision |
|---|---|---|---|---|
| P-1 | 1.6 | For any project defaults and plan overrides, an override with a stated reason takes precedence and one without never does | The reason form: `**Reason:** <text>`; negatives: absent, empty, whitespace, `**Reason:**` alone, `**` only, `Why: x`, plain prose, `Reason: x` with no bold label; override targets matching a default exactly, by case variant, and naming no default | `effective_principles` holds the plan's text exactly for reasoned overrides, the default otherwise |
| P-2 | 1.8 | For any plan text granting requirements changes, a ruling with outcome requirements never reaches a spec-edit lane | Grants: "arbiter may edit requirements", "requirements.md may change without asking", uppercase, in a list, in a `## Principles` table; plus plans with no grant | `route_ruling` lane is `needs_input` in every case; adding a grant never changes the result |
| P-3 | 1.7 | Adding a reservation to the plan layer can only move a ruling from applied to needs-input, never the reverse | At `route_ruling`: rulings over outcomes code, test repair and spec edit; reservations of `code` and `test repairs` (matched by outcome) and any scope the ruling itself reports in `reservedBy`; near-miss reservations naming another area. A file scope the ruling does not report is P-38 | Metamorphic: applied to needs-input when it names the area; unchanged otherwise |
| P-4 | 1.9 | For any unusable project file, the arbiter is not called | At `claim_facts` and `decide_claim`, over the read results `missing`, `directory`, `denied`, `decode-error`; negatives: `ok`. The real file forms and naming the file are TC-153 | `decide_claim` action `ask` with reason `principles-missing` for each unusable read; `ok` may rule |
| P-5 | 3.1, 3.2, 3.4 | A story is reported exactly when it has no valid line in its own section outside a fence | Line forms: `**Without it:** x`; empties (spaces, tabs, `**`, `_ _`, `<!-- -->`); near-misses that are not the marker (`*Without it:* x`, `**without it:** x`, `Without it: x`, `**Without it**: x`); placement in story, before the first story, under a later `##`; fences with ``` and ~~~, indented, unclosed; a story heading inside a fence | Reported set equals a brute-force oracle over the generated structure |
| P-6 | 3.3, 2.5 | For any non-empty content, no finding, and no criterion is ever required to carry a line | Content: "n/a", "nice to have", one character, unicode, a URL, criteria with and without lines of their own | `justifications` empty |
| P-7 | 3.5, 3.6 | For any spec, adding justification checks never changes another lint key | Specs with and without requirements.md, no stories, cycles, verify cycles | Metamorphic: every other key equal with requirements.md removed |
| P-8 | 3.7, 12.5 | Only a Story 12 finding blocks launch | Specs with justification findings alone, Story 12 findings alone, both, neither (real `ck plan --json`) (G) | `verifyScope` is non-empty exactly when a Story 12 finding exists; a justification finding never adds to it |
| P-9 | 5.1, 5.2 | For any subset of the four settings across global, project and override layers, an unset one follows the reviewer's | Valid and invalid models and efforts, `inherit`, aliases, an unset reviewer | Set value wins by layer; unset equals `reviewer.risky_*`; an invalid value exits with the reviewer's message |
| P-10 | 5.3, 5.4, 5.5 | Tier is risky exactly when the task is safety, or the stop is a guard stop, or the claim repeats an unruled one | At `claim_facts` and `decide_claim`: every combination of stop type, `TaskMeta.risk`, Complexity Low, Medium and High, a ledger with and without a failed call on the same key; plus a ledger with a recorded ruling (not a repeat) | When the action is `rule`; a ledger with a recorded ruling on the same key is `ask` with tier None. Metamorphic: changing Complexity never changes the tier. TC-157 covers a `risk_paths` match in a run |
| P-11 | 5.6, 5.7, NFR Bounded cost | For any answer sequence, a claim makes at most 2 calls and only normal then risky | Answer sequences over valid, needs a stronger look, malformed, null; any number of repeated claims (W) | Call order is normal, then at most one risky; a risky needs-a-stronger-look ends in needs-input |
| P-12 | 6.1, 6.2, 6.3 | For any spec claim, no implementer attempt or ladder step runs between the report and the arbiter | Stop type × attempt it occurs on × `escalate` lists × review rounds × individual or batched review (W) | The event immediately after the report that stops is the arbiter step; no `guard`, confirmation or `revise (model)` label for it |
| P-13 | 6.4, 6.5, 6.6, 6.11 | For any plain gate failure sequence, the arbiter runs exactly when every configured attempt failed | `failUntil`, retries, `escalate` of length 0-3 (W) | Complement: any attempt passing means no arbiter |
| P-14 | 6.7 | No worktree failure ever calls the arbiter | Worktree states BUSY, ERROR, on any task and DAG (W) | No `arbiter` label |
| P-15 | 6.9, 8.14, 8.15 | For any DAG and any waiting task, exactly its transitive dependents are held | `dags()`, claim on any task, slow arbiter (W) | Independents finish; dependents are not started; the entry is in `needsInput`, not `failed` |
| P-16 | 6.10 | For any failed arbiter call, the claim goes to me and no fallback runs | `arbiter_failures()`: throw, null, `{}`, a number, a string, an array, schema-invalid object (W) | Needs-input entry; no `guard`, no confirmation |
| P-17 | 7.1, 7.2 | Only a well-formed ruling citing an effective principle is valid | Valid rulings over 4 outcomes and needs-a-stronger-look. Negatives from real data: a principle id from another spec's plan, a D-n decision id, a reasonless override's id, a truncated id, a case variant, a principle's text in place of its id; two outcomes; unknown outcome; missing claim, reasoning, or principle; wrong types | `validate_ruling.valid` is true exactly for the well-formed set |
| P-18 | 7.3, 7.4, 7.8 | A claim that returns after its applied ruling is never ruled again, and a review finding covered by an applied ruling is never ruled again even when the set of blocking findings changes | At `claim_key`, `claim_facts` and `decide_claim`: ledgers of any size, with evidence text changed; claim identity fields varied one at a time; ledger entries in any order | `ask` with reason `repeat-after-ruling`; changing only evidence never changes identity; changing file, test, criterion or a review finding's file or issue does; reordering the ledger does not change the decision |
| P-19 | 7.5, 7.6, 7.7, 7.9, NFR Bounded cost | The arbiter is called for a new claim exactly when the task has fewer than 3 rulings and fewer than 6 arbiter calls | At `claim_facts` and `decide_claim`: 0-5 recorded rulings, 0-8 calls with failed calls among them, re-runs counted, other tasks' rulings and calls as negatives | Rulings and calls on other tasks never count; 4th claim is `ask` with reason `task-ruling-limit`; a claim on a task at 6 calls is `ask` with reason `task-call-limit`, whatever the ruling count. Listing the 3 is TC-154 |
| P-20 | 8.2 | A spec edit never spends a retry or escalation | Rulings at any attempt number, any retry budget (W) | Metamorphic: attempts and `escalations` equal the same run without the ruling |
| P-21 | 8.6 | Any diff changing a Status field or an acceptance mark is refused | Status forms (`**Status:** Done`, `In Progress`, added spaces, moved line); marks `- [x]`, `- [X]`, `* [x]`, `- [ ]`, on any criterion; negatives: Verify, Dependencies, description, a Status word inside prose | `spec_edit_refusals` lists it exactly when a field or mark changed |
| P-22 | 9.1, 9.2, 9.3, 9.8, 11.10 | The arbiter may edit only its own spec's three files | At `decide_arbiter_edit`, in an arbiter worktree with no lane ticket (the ticket case is P-45), over resolved `rel` values: the canonical paths, case variants (`TASKS.md`), suffix near-misses (`tasks.md.bak`, `tasks.md/x`), `specs/demo/sub/tasks.md`, another spec, `requirements.md`, `PLAN.md`, `specs/PRINCIPLES.md`, tests, source, `.claude/`. Path forms are P-39 | Allowed exactly for the three canonical paths of the ruling's spec; every denial names the file and the lane |
| P-23 | 9.4 | Every shell form of a denied change is denied | `rm`, `git rm`, `mv`, `>`, `>>`, `tee`, `cp`, `sed -i`, `perl -i`, `python -c`, `truncate`, `dd`, over denied paths and over lane files, chained with `&&` and `;`, in a `cd` compound; command substitution in `$(...)`, backticks, `<(...)`, `>(...)`, unquoted or double-quoted; negatives: reads of the same files, `git add` of lane files, `git commit -m '...'` (G) | Decision denied for every shell write, the arbiter edits lane files with Edit or Write only; negatives allowed |
| P-24 | 9.5, 9.6 | For any input that does not establish the arbiter, classification ignores the claim | Env names (`CK_ARBITER`, `CK_CALLER=ck-arbiter`), arguments, prompt text, tool input fields; agent types: near-misses (`ck-arbiter `, `CK-ARBITER`, `ck-arbiter2`, `workflow-subagent`, non-strings), no agent id | Result equals the unclaimed input's; never `arbiter` |
| P-25 | 9.7 | For any non-arbiter caller, the guard's decisions are today's | `task_agent` and `main_session` over `EditFacts` of all targets, started or not, only-adds or not | Equal to an oracle restating today's `decide_edit` |
| P-26 | 10.4, 10.5, 10.6 | Any sequence of records keeps earlier bytes and never repeats a number | Text with `## 3`, `---`, fences, CRLF, unicode, very long lines; runs split across processes (G) | Each prefix byte-identical; numbers strictly increasing; no file before the first record |
| P-27 | 10.7 | A run's report lists only its own rulings | Any partition of rulings across earlier and current runs (W) | Report rulings equal this run's set |
| P-28 | 11.1, 11.2 | Every refuted finding gets exactly one ruling and none is lost | Any mix of findings, refutations, verdicts, across lenses (W) | Conservation: confirmed plus refuted equals all findings |
| P-29 | 4.3, 11.6 | No proposed cut and no plan-changing ruling reaches a revise prompt | Any set of cuts and plan-changing drift rulings, mixed with normal fixes (W) | Revise prompts contain none of those ids; normal fixes still appear |
| P-30 | 11.4, 11.9, 11.12 | Each cut gets one ruling and a failed one leaves the cut as it stood | Any cut list, any `arbiter_failures()` on any subset (W) | A failed one is unchanged and the result is not Ready; shown cuts equal the soundly reasoned ones |
| P-31 | 12.1, 12.2, 12.4 | Verify-scope findings equal a brute-force oracle | Generated task graphs; Verify forms `uv run pytest`, `python -m pytest`, `pytest`, with `-q`, `-x`, `-n auto`, `-k x`, `-kx`, `-k=x`, node ids; shared and unshared files; suites not due; dependency chains; Done tasks | Findings equal the oracle's, in both directions |
| P-32 | 12.3, 12.4 | A test's call to a later task's command is found, and only then | [UC] Forms: `["ck", "x"]`, `"ck x"`, `invoke(cli, ["x"])`, flags `--y` and `--y=1`; near-misses: the words in a comment, a docstring, an unrelated string, a provided prefix of a longer command; a provider that is a transitive dependency | Finding exactly for real invocations of not-yet-provided commands |
| P-33 | 12.10 | No problem is reported twice | [UC] Generated specs where a problem matches an existing rule | Metamorphic: each (task, test or file, command) occurs once across keys |
| P-34 | NFR Determinism | Lint output is stable | Any generated spec; task block order shuffled in the file | Two runs byte-identical; findings sorted, independent of file order |
| P-35 | 13.1, 13.2, 13.6, 13.7 | Audit runs exactly once for impl tasks with verifying tests | `dags()`, tracks, tasks with and without Verified by, retries and escalations (W) | Exactly once before the first attempt; never for others |
| P-36 | 8.4, 8.5 | A refused landing never moves the integration branch | Conflict, red gate, refused path, cycle, new blocking finding (G) | Target HEAD equal before and after; no second ruling |
| P-37 | 8.8, 8.9, 8.11, 8.13 | Each outcome goes to exactly one lane | 4 outcomes × `testFixerAvailable` | Spec edit to `spec_edit`, code to `code`, requirements to `needs_input`, test repair to `needs_input` or `fixer` by availability, never `spec_edit` |
| P-38 | 1.7, 8.4 | A reserved file never lands, whatever the ruling says about it | `decide_spec_edit` over changed lane paths × `reserved` scopes `tasks.md`, `design.md`, `test-plan.md` in any mix, the ruling's `reservedBy` absent; negatives: a reservation of another lane file, of `code` or of `test repairs` | `land` false with a reason naming the file exactly when a changed path matches a reserved file scope |
| P-39 | 9.1, 9.2, 9.3, 11.10 | A path form never changes the lane's decision | Through the real hook and real files (G): relative, absolute, `./`, `..` that re-enters, `..` that escapes, a symlink into the lane, a symlink out of it | The decision equals the decision for the resolved path |
| P-40 | 1.4 | A plan with no usable `## Principles` section can never be Decided | `plan_may_be_decided` over a missing section, an empty one, whitespace, comments only, a `## Principles` heading inside a fence, one bullet, a `**Nothing to add:**` bullet | True exactly when the real section holds a bullet |
| P-41 | 1.11, 12.9 | A warning or an unchecked task alone never fails lint or blocks a launch | `lint_verdict` over any mix of `warnings` and `notChecked`, with and without blocking keys | Metamorphic: adding those entries never changes `exit_nonzero` or `launch_blockers` |
| P-42 | 4.3, 4.4, 4.5 | A cut is applied only when approved and nothing has started | `decide_cut` over all approved and started pairs | `apply` only for approved and not started, `frozen` for approved and started, `hold` otherwise |
| P-43 | 4.8 | A serves-no-requirement finding alone never blocks | `normalizeReview` (bridge) over severity mixes with `noRequirement`, including one the reviewer marked blocking, alone and beside a real minor or blocking finding | Such a finding is minor; the review is `approve` exactly when no other blocking finding remains |
| P-44 | 8.12 | A settled finding never blocks again | `settleFindings` (bridge) over findings settled by file and issue, with case and whitespace variants, and reworded ones the reviewer marks `settledBy`; negatives: a different issue on the same file | A matching or `settledBy` finding is removed; a different issue stays |
| P-45 | 11.5, 11.7, 11.10 | `requirements.md` is in the lane exactly under a ticket before implementation starts | `decide_arbiter_edit` over lane ticket present or absent × `implementation_started` × the ticket's spec or another × arbiter worktree or not × `requirements.md`, `PLAN.md` | `requirements.md` allowed exactly with a ticket for its own spec, not started, not in an arbiter worktree; `PLAN.md` never |
| P-46 | 11.11 | With no refuted findings and no cuts, those steps call no arbiter | Reviews with no refuted findings and no cuts, alone and mixed with confirmed findings, in create and review modes (W) | No `arbiter refuted` or `arbiter cuts` label; neither step in `review.arbiter.ran` or `lost` |
| P-47 | 14.1, 14.3, 14.4, 14.5 | Recovery happens only for a managed checkout with a matching refusal | `decide_recovery` over managed or not × dirty or clean × merge in progress or not × refusal absent, for another ref, at another head, from another checkout, or matching | `recover` exactly when managed, dirty or merging, and the refusal matches ref, head and checkout; `clean` when neither dirty nor merging; otherwise `refuse` |
| P-48 | 15.1, 15.2, 15.3, Bounded landing cost | A task is RED only on its own task-scoped red gate | `land` with an in-process gate function over real task branches that commit their own Done (G): batch size 1 to 6, the batch completing the spec or not, any set of bad tasks, and a full-suite-only failure on or off. The gate function models `gate.py`'s scope rule: a gate runs the full suite when it is called with `whole` true (the default) and the tasks Done on the target plus its group cover the spec; it is red when its group holds a bad task, or when the flag is on and it runs the full suite | Every RED task is bad and has a red gate called with `whole=False` on exactly itself; every gate after the batch gate, and the batch gate itself when only one task merged, is called with `whole=False`; with no bad task and the flag on, no task is RED, every task is MERGED, and `unreproduced` is set exactly when the batch gate ran the full suite; with no bad task and the flag off, all MERGED; at most 2n-1 gates |

## Harness Contract

Design is written in parallel and these names are this plan's requirement of it. Where design.md chooses otherwise, the two must be reconciled at Link, and the tests follow the outcome only by changing `tests/arbiter_helpers.py`.

### Python decision seams (pure: no file, clock or network)

Module `claude_kiro.arbiter`:

```python
def effective_principles(project_text: Optional[str], plan_text: Optional[str]) -> dict:
    """{"available": bool, "planLayer": "present" or "absent", "principles": [{"id": str, "text": str, "layer": "project" or "plan"}],
        "overrides": [{"id": str, "reasoned": bool}], "reserved": [str], "requirementsGrantIgnored": bool}
    project_text None means missing or unreadable."""

def claim_id(claim: dict) -> str:
    """Deterministic. Ignores claim["text"] and claim["evidence"], which change as a claim returns (7.4)."""

def decide_call(claim: dict, history: list[dict], task: dict, principles: dict) -> dict:
    """{"action": "rule" or "needs_input", "tier": "normal" or "risky" or None, "claimId": str, "reason": str, "earlier": [dict]}"""

def validate_ruling(ruling: dict, principles: dict) -> dict:
    """{"valid": bool, "problems": [str]}"""

def route_ruling(ruling: dict, principles: dict, *, test_fixer_available: bool) -> dict:
    """{"valid": bool, "problems": [str], "lane": "spec_edit" or "test_repair" or "code" or "needs_input", "reason": str}"""

def spec_edit_refusals(before_tasks: str, after_tasks: str) -> list[str]:
    """Status-field and acceptance-mark changes (8.6)."""
```

Shapes: `claim` = `{task: str, stop: "test_conflict" or "guard_stop" or "review_deadlock" or "gate_exhausted" or "verify_scope" or "audit" or "create_refuted" or "create_drift" or "create_cut", file: str, test: str, criterion: str, text: str, evidence: str}`. `task` = `{risk: "safety" or "normal", complexity: str}`. `history` entry = `{claimId, task, kind: "ruling" or "no_ruling", number, run}`. `ruling` = `{claim: str, reasoning: str, principle: str, outcome: "spec_edit" or "test_repair" or "code" or "requirements", status?: "needs_stronger_look", testChange?: str, recommendation?: str, proposedRequirement?: {wording, reason}}`.

`claude_kiro.hooks._shared.guard_policy`:

```python
Caller = Literal["task_agent", "main_session", "arbiter"]
def arbiter_may_edit(rel: str, spec: str, phase: Literal["run", "create"], implementation_started: bool) -> bool: ...
```

`classify_caller` keeps its signature and its existing results for every caller that is not the arbiter.

`claude_kiro.worktrees`: `decide_recovery(RecoveryFacts) -> ("clean" | "recover" | "refuse", reason)` (pure), and `land(..., gate=GateFn)` taking an in-process gate. `claude_kiro.protect`: `refusal_for(root, ref) -> Refusal | None`.

`claude_kiro.lint`: `justification_findings(requirements_text: str) -> list[dict]` (`{story: str, title: str, message: str}`); `verify_scope(tasks: dict, project_dir: Path, config: dict) -> dict` (`{"findings": [{task, suite, test, provider, kind, message}], "checked": [...], "notChecked": [...]}`).

### CLI (exit 0 unless stated; `--json` prints exactly one JSON object)

| Command | Input | Output |
|---|---|---|
| `ck principles init [--commit] [--json]` | | `{created, path, commit}` |
| `ck arbiter decide <spec> --json` | stdin `{claim, task}` | `decide_call`'s result plus `principles` (project text, plan text, `planLayer`, `effective`) |
| `ck arbiter route <spec> --json` | stdin `{claim, ruling, testFixerAvailable}` | `route_ruling`'s result |
| `ck arbiter record <spec> --run <id> --json` | stdin `{claim, ruling, tier, trigger, whatChanged, commit, landed, planLayer}`; a `kind: "no_ruling"` payload is kept outside decisions.md | `{number, path}`, plus `recovered: {paths, mergeInProgress, head}` when it restored an `integrate/*` `INTO` (14.2). Exit 1 with `{error}` when it cannot write |
| `ck arbiter log <spec> [--run <id>] --json` | | `{entries: [...]}` |
| `ck arbiter land <spec> --branch <b> --into <t> --json` | | `{state: "MERGED" or "RED" or "CONFLICT" or "PROTECTED" or "REFUSED", detail, commit}`, plus `recovered` when it restored the target (14.2) |
| `ck lint <spec> --json` | existing keys unchanged, plus `justifications`, `planPrinciples`, `verifyScope` | exit 1 when justification or Story 12 findings exist |
| `ck plan <spec> --json` | existing keys unchanged, plus `verifyScope` (Story 12 findings only) | |
| `ck stats show <run> --json` | | adds `rulings: {byTrigger, byOutcome, byTier}` |
| `ck worktree land <spec> <n>... --json` | | adds `recovered: {paths, mergeInProgress, head}` and `unreproduced: {tasks, tail, failures}`, each only when it applies |
| `ck worktree merge <spec> <n>...` | | first line `restored <checkout> to <head>: discarded <paths>` when it restored, ending `; a merge was in progress` when one was (14.2) |

Decisions block of `spec-implement.js`, via `loadDecisions({extra: [...]})`: `handOffTestRepair(ruling, {fixerAvailable}) -> {kind: 'needs_input', entry} or {kind: 'fixer', ruling}`.

### Workflow surfaces

- Labels: `arbiter <N>`, `arbiter <N> risky`, `audit <N>`, and `arbiter-decide <N>`, `arbiter-route <N>`, `arbiter-record <N>`, `arbiter-land <N>`; for create, `review: should-exist`, `arbiter: refuted <id>`, `arbiter: drift`, `arbiter: cut <id>`, each with an optional ` risky` suffix, and `apply-cuts`.
- Each `arbiter-*` step prompt holds one `ck -C <dir> arbiter <verb> <spec> ... --json` line and at most one fenced `json` block used as its stdin.
- Each arbiter agent call passes `model`, `effort` and `schema` from the resolved tier. The tier comes from `args.agents.arbiter`.
- `args.testFixerAvailable` (default false).
- Result keys: `rulings` (this run's, each `{number, task, trigger, outcome, tier, principle, commit}`), `proposedCuts`, and the existing `needsInput`. A ruling's needs-input entry keeps the fields of today's entry and adds `ruling`, and `evidence` when new evidence exists.
- Create result keys: `arbiterChecks: {refuted, drift, cuts}` each `"ran"` or `"not_run"`, and `proposedCuts`.
- `ck-arbiter` agent source: `plugin/agents/arbiter.md`, in `plugin.AGENT_ROLES`.
- Landing entries: `landings[].recovered` and `landings[].unreproduced`; a gate record with `kind: 'unreproduced'`; fake-project landing option `unreproduced: {batch: [n...]}`.
- Test names: `test_tc<N>_...` and `test_p<N>_...`.

## Critical Paths

1. **A wrong test stops a task and the run keeps going.** A task reports a test conflict (TC-42) → the arbiter is called once at the right tier with both layers (TC-37, TC-7, TC-50) → the ruling is validated (TC-53, TC-54) → it is a test repair, so a needs-input entry carries the exact change and no agent edits the test (TC-71, TC-72) → dependents wait, independents finish (TC-51, TC-78) → the entry is in `decisions.md` and the report (TC-88, TC-94, TC-95).
2. **A bad Verify line is fixed with no user involved.** A claim becomes a spec-edit ruling (TC-42, TC-53) → the arbiter commits on its own branch, inside the guard's lane (TC-79, TC-87, TC-61, TC-156) → it lands through the queue (TC-138) → the task resumes without spending a retry (TC-62) → the new wiring applies to later tasks (TC-63) → recorded with its commit (TC-88, TC-70).
3. **A repeated claim stops looping.** A ruling is applied (TC-61) → the same claim returns (TC-55, TC-56) → it goes to me with the ruling and new evidence → on a resume the limits still hold (TC-60, TC-127).
4. **Requirements stay mine.** The arbiter is denied requirements.md by edit and shell in a run (TC-80, TC-82) → a requirements ruling or missing requirement becomes a needs-input entry and requirements.md is unchanged (TC-76, TC-77) → a plan's grant changes nothing (TC-10).
5. **A plain bug keeps today's path.** A gate fails (TC-46) → retries and escalation run as today → a pass means no arbiter (TC-48) → only a failed top model reaches it (TC-47) → a clean spec behaves as before (TC-131).
6. **A spec gets written with the arbiter.** Lenses run, including should-this-exist (TC-26) → refuted findings get rulings (TC-97, TC-98) → cuts are checked and listed, never applied (TC-100, TC-27, TC-28) → drift is ruled on after Revise (TC-99) → the consistency check runs over changes (TC-104) → an arbiter failure means not Ready (TC-108).
7. **Bad wiring never launches.** `ck lint` finds a Story 12 problem (TC-110, TC-111, TC-113) → launch is refused (TC-114) → the arbiter fixes it (TC-115) → lint re-runs before launch (TC-116).
8. **A test needing a later task is caught early.** After the test tasks merge, the audit reads the selected tests (TC-120) → a problem goes to the arbiter before any attempt (TC-122).
9. **A refused raw merge does not stop the run.** The hook refuses and records it (TC-168) → the next landing, merge or arbiter landing restores the checkout to its head (TC-162, TC-163, TC-196, TC-197) → untracked files and other checkouts are untouched (TC-164, TC-165) → a hand merge is never discarded (TC-166) → in a full run (TC-169).
10. **The fixer gets the real culprit.** Every half is gated, task-scoped (TC-170) → a batch-only failure blames no task, even when the last task completes the spec (TC-171, TC-172) → the run sends it to the attributed full gate and fixer (TC-173) → in the TC-176 scenario (TC-174).

## Commands

Everything runs from the project root on `integrate/run-principles`.

| Level | Command |
|---|---|
| Whole suite, parallel | `uv run pytest -q -n auto -m "not serial"` |
| Serial (TC-130) | `uv run pytest -q -m serial` |
| Principles (TC-1, 3, 8, 10, 140, 153; P-1 to P-4, P-40) | `uv run pytest tests/test_principles.py -q` |
| Skill text (TC-2, 4, 5, 6, 14, 15) | `uv run pytest tests/test_skill_contracts.py -q` |
| Justification lint (TC-13, 18-25; P-5 to P-8, P-41) | `uv run pytest tests/test_justification_lint.py -q` |
| Agents (TC-35, 36; P-9) | `uv run pytest tests/test_arbiter_agents.py -q` |
| `ck arbiter` CLI (TC-53, 88, 89, 91-93, 133, 135, 150, 152, 154; P-10, 17, 18, 19, 26, 37) | `uv run pytest tests/test_arbiter_cli.py -q -n auto` |
| Landing (TC-61, 64-70, 138, 149, 156; P-21, P-36, P-38) | `uv run pytest tests/test_arbiter_land.py -q` |
| Workflow runs (TC-7, 9, 11, 12, 34, 37-52, 54-60, 62, 63, 71-78, 90, 94, 96, 146, 155, 157; P-11 to P-16, 20, 27, 43, 44) | `uv run pytest tests/test_arbiter_run.py -q -n auto` |
| Guard (TC-79-87, 101, 103, 106, 134; P-22 to P-25, P-39, P-45) | `uv run pytest tests/test_arbiter_guard.py -q -n auto` |
| Stats (TC-95) | `uv run pytest tests/test_arbiter_stats.py -q` |
| Label corpus (TC-137) | `uv run pytest tests/test_stats_label_corpus.py -q` |
| Skill text for implement, create, review (TC-139, 147, 159) | `uv run pytest tests/test_arbiter_skill_contracts.py -q` |
| Plugin agent set | `uv run pytest tests/test_plugin.py -q -k "not dogfood and not vendored_workflow"` |
| Real hook capture (TC-160) | `uv run pytest tests/test_arbiter_capture.py -q` |
| Create workflow (TC-16, 17, 26-33, 97-100, 102, 104, 105, 107, 108, 141, 148, 158; P-28, 29, 30, 42, 46) | `uv run pytest tests/test_arbiter_create.py -q -n auto` |
| Verify-scope lint (TC-109-119, 136, 143-145; P-31 to P-34) | `uv run pytest tests/test_verify_scope_lint.py -q` |
| Audit (TC-120-126, 142, 151; P-35) | `uv run pytest tests/test_arbiter_audit.py -q -n auto` |
| NFRs (TC-127-129, 131, 132) | `uv run pytest tests/test_arbiter_nfr.py -q -m "not serial"` |
| Landing recovery (TC-162-168, 196, 197; P-47) | `uv run pytest tests/test_land_recovery.py -q` |
| Recovery red team (Story 14) | `uv run pytest tests/test_land_recovery_redteam.py -q` |
| Bisection (TC-170-172, 175, 198; P-48) | `uv run pytest tests/test_land_bisect.py -q` |
| Unreproduced landing in a run (TC-173) | `uv run pytest tests/test_land_unreproduced_workflow.py -q` |
| Smoke runs (TC-169, 174) | `uv run pytest tests/test_unattended_smoke.py -q -k "tc169_ or tc174_"` |
| One case | `uv run pytest tests/<file> -q -k tc42_` |
| One property | `uv run pytest tests/<file> -q -k p12_` |
| Existing workflow control-flow suite | `uv run pytest tests/test_workflow_scripts.py -q` |
| Lint this spec | `ck lint run-principles` |

## Not Tested

- **Whether a model follows an instruction.** Criteria 1.1 (telling me), 1.3, 1.4, 1.5, 2.1, 2.2, 2.3 and 2.4 are carried out by a model following a skill or prompt. TC-2, 4, 5, 6, 14, 15, 16 and 17 prove the shipped text says so, not that a session obeys it. `ck lint` is the backstop for the file results (TC-13, TC-19).
- **Whether a ruling is right.** The fake arbiter returns canned rulings. The tests prove a ruling is validated, limited, routed and recorded; nothing here measures its quality.
- **The real Workflow runtime and real models.** The fake runtime stands in for both; TC-128 covers the known runtime difference, the missing clock.
- **The real test-fixer.** Tasks 57 and 86 of unattended-correctness are not built. TC-72 proves the hand-off point and the attached ruling; the fixer's proof step and review stay under that spec's tests.
- **Vendored copies under `.claude/`.** Agents cannot land them, so these tests read `plugin/` sources only. New tests must not assert on `.claude/commands/spec/*.md`, or their tasks could never go green. Vendoring is covered by the existing `ck update` tests. The existing dogfood copy tests go green only once Task 92 runs by hand.
- **Notification delivery.** TC-78 runs a real local `notify_command`; the phone-side delivery is out of the project's hands.
- **Adding a new acceptance box in a spec edit.** 8.6 speaks of changing a mark; whether adding an unchecked box counts is left to design, so P-21 does not generate it.
- **Performance beyond lint.** Only the NFR's 10-second lint bound is measured.
