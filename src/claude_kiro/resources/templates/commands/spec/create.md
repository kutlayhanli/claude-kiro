---
description: Write the spec (requirements → design → tasks) as a workflow, ending with an adversarial review
argument-hint: [feature-name-or-description]
allowed-tools: Read, Edit, Write, Grep, Glob, AskUserQuestion, Workflow, Agent, TaskCreate, TaskUpdate, Bash(ck agents check:*)
---

Create the specification for: $ARGUMENTS

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
2. in parallel, writes `design.md` grounded in the real codebase and `test-plan.md`. The test plan is integration-first: it tests real boundaries (database, filesystem, HTTP, CLI) and never sees the design, so the tests stay an independent check,
3. in parallel, writes an implementation track and a test track of tasks, then links them into `tasks.md`. Each impl task is **Verified by** test tasks that run first, and tasks are grouped into parallel waves,
4. runs an **adversarial review**: four reviewers attack the spec (plan fidelity, codebase grounding, implementability, test adequacy), and a separate skeptic tries to refute each finding,
5. applies the findings that survive, and writes `review.md`.

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

1. Check that `.claude/workflows/spec-create.js` exists in the project root. If it doesn't, tell me to run `ck init` (it installs missing files without overwriting others) and stop.
2. Call the **Workflow** tool with:
   - `scriptPath`: the absolute path to `.claude/workflows/spec-create.js`
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
       "date": "[today, YYYY-MM-DD]"
     }
     ```
3. Tell me it's running: requirements, then design and test plan in parallel, then two task tracks in parallel and a link step, then adversarial review and revision. I can watch it with `/workflows`.

**Without the Workflow tool** (it isn't among your tools, or calling it is refused or blocked, or I pass `--no-workflow`): run `.claude/workflows/spec-create.js` by hand with the Agent tool and the same args, as `.claude/workflows/without-workflow-tool.md` describes, and tell me once that you're doing so. The steps, prompts, and result are the same.

## Step 4: Close the loop

When the workflow returns:

1. If it returned an `error`, report what failed and stop. Don't hand-write the missing documents.
2. Report in this shape:
   ```
   Spec: specs/[feature-name]/  ·  Review result: [Ready | Ready after decisions | Needs work]

   requirements.md: [one-line summary]
   design.md:       [one-line summary]
   test-plan.md:    [N cases: X integration, Y e2e, Z unit/property; infrastructure to build]
   tasks.md:        [N tasks (T test, I impl); waves]

   Adversarial review: [N] fixed, [M] refuted, [K] need your decision
   Assumptions to check: [only the ones that matter]
   ```
   Mention any `unverified` findings or lost review lenses so I know coverage was partial.
3. **Open questions:** if `review.openQuestions` is non-empty, ask me with AskUserQuestion (up to 4 per call; put the reviewer's suggested fix first when it is a sensible default). Then apply my answers to the spec files, keeping requirements, design, and tasks consistent. Record each answer under a new "Decisions" entry in PLAN.md (if it exists) and in the "Needs Your Decision" table of `review.md`.
4. Finish with: "Spec ready. Use `/spec:implement [task-number]` to start, or `/spawn-worktree specs/[feature-name]` to run wave 1. Tasks are only Done when `ck gate` passes."

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
    ├── tasks.md         # steps: ### Task N, Track, Files, Verify, Verified by, waves
    └── review.md        # adversarial review record
```

Specs live at the project root, not in `.claude/`, so writing them never needs approval.
