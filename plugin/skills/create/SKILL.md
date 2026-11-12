---
description: Write the spec (requirements → design → tasks) as a workflow, ending with an adversarial review
argument-hint: [feature-name-or-description]
allowed-tools: Read, Edit, Write, Grep, Glob, AskUserQuestion, Workflow, Agent, TaskCreate, TaskUpdate, Bash(ck agents check:*), Bash(ck agents --json:*), Bash(ck principles init:*)
---

Create the specification for: $ARGUMENTS

## Step 0: Hand-off check

Before anything else, and before choosing between the Workflow tool and agent mode, run `ck handoff create $ARGUMENTS --json` (from where you are now). It decides whether this run belongs in the main checkout instead of this isolated session, and answers with one JSON object:
- `handoff: false`: nothing to hand off. Continue with the steps below. Its `interactive` field says whether I am at the session (true) or this is an unattended run (false).
- `handoff: true` and `launched: true`: the run now continues in the main checkout under `ck run`. Print `command`, `mainCheckout`, `runBranch`, `follow` (how to watch the log) and `report` to me, then stop. Do nothing else.
- `handoff: true` and `launched: false`, or the command exits non-zero or fails to run: print its `error` and the `manualCommand` it gives, then stop. Never start the run from this session after a failed hand-off.

## First: Model Check

The spec written here is what every later agent builds and tests against, and the spec-create workflow's agents run on this session's model, so this should run on a strong model.

1. Find your model ID in your system prompt (for example "The exact model ID is claude-opus-5-5"), and your effort level if anything in your context states it.
2. Run `ck agents check --model <model id> --json`, adding `--effort <level>` only if you know it.
3. If it exits 0, or its `"ask"` is false, continue without mentioning it.
4. Otherwise ask me once with AskUserQuestion, before any other work: "This session runs <model, effort if known>. Writing the spec works best on <min_model> at <min_effort> effort or higher. Switch first?" Options:
   - **Switch (recommended)**: tell me to run `/model` and choose <min_model> with <min_effort> effort or higher, then run this command again. Stop here.
   - **Continue on <current model>**: go on.
   You can't change the model yourself; don't try.
5. If `ck` isn't installed or the check fails to run, skip it and say so in one line.

The minimum is configurable: `ck agents set planning.min_model <model>`, `ck agents set planning.min_effort <level>`, or `ck agents set planning.ask false` to stop asking (add `--project` to set it for this project only).

# Spec-Create: Write It Down, Then Attack It

`/spec:plan` is where we decide. This command turns those decisions into a spec without another round of questions. It runs the `spec-create` workflow, which:

1. writes `requirements.md` (EARS criteria, numbered for traceability),
2. in parallel, writes `design.md` grounded in the real codebase and `test-plan.md`. The test plan is integration-first: it tests real boundaries (database, filesystem, HTTP, CLI) and never sees the design, so the tests stay an independent check. Its required ## Properties section turns every universally quantified criterion (any, every, only, never, SHALL NOT) into a property test over all the input's surface forms and its negative space, against a pure decision seam,
3. in parallel, writes an implementation track and a test track of tasks, then links them into `tasks.md`. Each impl task is **Verified by** test tasks that run first, and each task starts as soon as its own dependencies merge (no waves). Tasks that send messages, move money, touch auth or credentials, delete, or have another irreversible external effect get `**Risk:** safety (reason)`: `/spec:implement` starts them on the strong model and always has the risky reviewer check them,
4. runs an **adversarial review**: four reviewers attack the spec (plan fidelity, codebase grounding, implementability, test adequacy), and a separate skeptic tries to refute each finding,
5. applies the findings that survive, and writes `review.md`. A fix that would change behaviour a requirement criterion or property governs is not applied; it comes back to me as a decision ("raised while applying fixes"),
6. runs a **consistency check** on what the revision changed: it diffs each file against a pre-revision snapshot and checks every changed passage against the criteria and the Properties section (and properties and test cases against each other on overlapping inputs). Wording and traceability drift it fixes itself (then checks once more); behaviour conflicts come back to me as decisions ("found by the consistency check").

Your job here is to set up the run, launch it, and bring back anything that needs my decision.

**Calling the Workflow tool here is intended.** Running `/spec:create` is my opt-in to this multi-agent workflow.

---

## Step 1: Resolve the spec

1. Work out the feature name (kebab-case). If `$ARGUMENTS` matches a directory in `specs/`, use it. Otherwise derive a name from the description.
2. The spec directory is `specs/[feature-name]/` at the project root. If the project still has `.claude/specs/[feature-name]/`, tell me to run `ck migrate` first and stop.
3. Read `specs/[feature-name]/PLAN.md` if it exists.
   - **Status `Decided`:** good, continue.
   - **Status `Discussing` or open questions remain:** list the open items and ask me (AskUserQuestion) whether to resolve them now with `/spec:plan`, or proceed and let the spec record assumptions.
   - **No PLAN.md:** say that `/spec:plan` is where we normally decide the approach, and ask whether to run it first (recommended for anything non-trivial) or proceed from the description. If I proceed and the description is ambiguous, ask at most 3 shaping questions now and pass the answers along as context.
4. If `requirements.md`, `design.md`, or `tasks.md` already exist in that directory, ask whether to overwrite them, evolve them into a new spec (new name, `evolvesFrom` set), or stop.
5. If PLAN.md has an `Evolves:` entry, or the description says this extends, replaces, or improves an existing spec, set `evolvesFrom` to that spec's directory.

## Step 2: Gather brief context

Spend a few tool calls, no more, to give the workflow a head start: the 3-8 most relevant files, the test layout and how tests run (frameworks, fixtures, how the app/DB/CLI is started in tests), and any conventions it must follow. Summarize in under 20 lines. The workflow agents will read the code themselves; this is a pointer list, not a design.

Also read `specs/ck.json`. If it is missing or has no `"verify"` commands, tell me which command you'd use to run the project's tests and offer to add it; the verification gate relies on it.

## Step 3: Launch the workflow

1. Before the workflow, run `ck principles init` (from the project root). If it created the principles file, tell me so in one line (it holds the principles the arbiter cites; I can edit it). If the file was already there, say nothing.
   - Run `ck agents --json` and keep its `agents.arbiter` role. The arbiter is the agent that rules on refuted findings and proposed cuts. `arbiterAgent` is `spec:arbiter` in plugin mode and `ck-arbiter` in vendor mode; `runnerAgent` is `spec:runner` in plugin mode and `ck-runner` in vendor mode (it runs the thin `ck` calls). In vendor mode, if `.claude/agents/ck-arbiter.md` or `.claude/agents/ck-runner.md` is missing (run `ck agents sync` or `ck vendor`), pass `null` for it and tell me that the arbiter steps will be lost or run as default subagents.
2. Call the **Workflow** tool with:
   - First run `ck progress begin <spec> --kind create --mode workflow --json` (`<spec>` is the feature name) and keep its `run` and `etaLine` (a failure prints `run: null`; carry on without them).
   - The script, picked by the project's mode first: if `.claude/ck-manifest.json` records `"mode": "vendor"` (copies made by `ck vendor`; the plugin is off there), `scriptPath` set to the absolute path of `.claude/workflows/spec-create.js`, without trying the plugin's name. Otherwise `name: "spec:spec-create"` (the workflow this plugin ships); only if no workflow by that name is found, fall back to that `scriptPath`. If the file is missing too, tell me to run `ck setup` (installs the plugin) and stop.
   - `args` (a JSON object, not a string):
     ```json
     {
       "mode": "create",
       "feature": "[feature-name]",
       "projectRoot": "[absolute project root]",
       "specDir": "[absolute path to specs/feature-name]",
       "description": "[$ARGUMENTS, or the PLAN.md problem statement]",
       "planPath": "[absolute path to PLAN.md, or null]",
       "evolvesFrom": "[absolute path to the old spec dir, or null]",
       "context": "[your Step 2 summary, plus any answers I gave in Step 1]",
       "date": "[today, YYYY-MM-DD]",
       "arbiterAgent": "[spec:arbiter, or ck-arbiter in vendor mode]",
       "runnerAgent": "[spec:runner, or ck-runner in vendor mode]",
       "agents": { "arbiter": "[agents.arbiter from ck agents --json]" },
       "progressRun": "[run from ck progress begin]", "etaLine": "[etaLine from ck progress begin]"
     }
     ```
3. Tell me it's running, with the `etaLine`: requirements, then design and test plan in parallel, then two task tracks in parallel and a link step, then adversarial review and revision. I can watch it with `/workflows`.

**Without the Workflow tool** (it isn't among your tools, or calling it is refused or blocked, or I pass `--no-workflow`): run `${CLAUDE_PLUGIN_ROOT}/workflows/spec-create.js` by hand with the Agent tool and the same args, as `${CLAUDE_PLUGIN_ROOT}/resources/without-workflow-tool.md` describes, and tell me once that you're doing so. The steps, prompts, and result are the same.

## Step 4: Close the loop

When the workflow returns:

1. If it returned an `error`, report what failed and stop. Don't hand-write the missing documents.
2. Report in this shape:
   ```
   Spec: specs/[feature-name]/  ·  Review result: [Ready | Ready after decisions | Needs work]

   requirements.md: [one-line summary]
   design.md:       [one-line summary]
   test-plan.md:    [N cases: X integration, Y e2e, Z unit; P properties (P-1..P-n), library; infrastructure to build]
   tasks.md:        [N tasks (T test, I impl); critical path length]

   Adversarial review: [N] fixed, [M] refuted, [K] need your decision
   Consistency check: [passes] pass(es), [F] drift fixed, [D] behaviour conflicts for your decision
   Assumptions to check: [only the ones that matter]
   ```
   Proposed cuts: [only when `review.proposedCuts` is non-empty: one line per cut with the story, the weakness and what it saves]
   Mention any `unverified` findings, lost review lenses, or `consistency.lost` so I know coverage was partial.
   Then run `ck stats collect --run <wf_id> --auto` outside the sandbox and quote the line it prints (where the record went, or why it was skipped; if it failed, quote the retry command it gives). Never let its result change what you report about the run. In agent mode (no `wf_id`, the run was started with `ck stats begin`), run `ck stats collect --agents <run_id> --auto` instead, outside the sandbox, and quote its line the same way.
   Then close the progress run: `ck progress end <spec> --outcome <outcome> [--record <id>] --json` (`outcome` is `completed`, or `failed` if the workflow returned an `error`; `--record` is the record id `ck stats collect` printed, when it printed one). Quote the line it prints and every warning line (`⚠ progress: ...`, `⚠ notification failed: ...`). Never let its result change what you report about the run.
3. **Open questions:** if `review.openQuestions` is non-empty, ask me with AskUserQuestion (wrap each question: `ck progress event <spec> approval-start --reason question` just before it and `ck progress event <spec> approval-end` as soon as I answer; up to 4 questions per call; put the reviewer's suggested fix first when it is a sensible default). Each item has a `source`: `review` (a reviewer's question), `revise` (a fix that would have changed behaviour a criterion governs, so it was not applied) or `consistency` (a contradiction the consistency check found after the revision). For `revise` and `consistency` items, show the criterion or property in `conflictsWith` next to the question and offer the item's `options`; don't pick one yourself. Applying my answer may mean changing the criterion, the design, or both: keep requirements, design, test plan and tasks consistent. Then apply my answers to the spec files, keeping requirements, design, and tasks consistent. Record each answer under a new "Decisions" entry in PLAN.md (if it exists) and in the "Needs Your Decision" table of `review.md`.
4. **Proposed cuts:** for each entry of `review.proposedCuts`, tell me the story, the weakness of its "Without it" justification, and what cutting it saves (`saves`). A cut is never applied by the review; it waits for my approval. Ask me with AskUserQuestion which cuts I approve.
5. **Approved cuts:** when I approve a cut, launch the `spec-create` workflow again (same script pick as Step 3) with `args` `{ "mode": "apply-cuts", "feature", "projectRoot", "specDir", "cuts": [the approved entries of review.proposedCuts: id, story, title, weakness], "arbiterAgent", "runnerAgent", "agents" }`. You do not remove the story yourself: the `apply-cuts` mode decides from `ck arbiter context --approved-cut` and does the work. Report what it returns:
   - **No task has started:** the story was removed from requirements, design, test plan and task list consistently, and `ck lint` ran. Say so, and quote any new `ck lint` failure it reports. If lint failed, the cut is not finished: tell me.
   - **A task has started:** nothing was applied. Tell me the requirements are frozen because a task has started, and that removing the story is my change to make.
6. Finish with: "Spec ready. Use `/spec:implement [task-number]` to start, or `/spec:implement [feature-name]` to run the whole spec (`/spec:spawn-worktree specs/[feature-name]` to steer it yourself). Tasks are only Done when `ck gate` passes."

---

## Spec layout

```
specs/
├── ck.json              # verify commands, test patterns, guard modes (shared by all specs)
└── [feature-name]/
    ├── PLAN.md          # decisions (from /spec:plan)
    ├── requirements.md  # what: user stories, numbered EARS criteria
    ├── design.md        # how: components, public interfaces, traceability
    ├── test-plan.md     # proof: integration-first test cases, infrastructure, commands
    ├── tasks.md         # steps: ### Task N, Track, Files, Verify, Verified by, Dependencies, Risk, Schedule
    └── review.md        # adversarial review record
```

Specs live at the project root, not in `.claude/`, so writing them never needs approval.
