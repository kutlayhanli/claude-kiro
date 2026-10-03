---
description: Write the spec (requirements → design → tasks) as a workflow, ending with an adversarial review
argument-hint: [feature-name-or-description]
allowed-tools: Read, Edit, Write, Grep, Glob, AskUserQuestion, Workflow
---

Create the specification for: $ARGUMENTS

# Spec-Create: Write It Down, Then Attack It

`/spec:plan` is where we decide. This command turns those decisions into a spec without another round of questions. It runs the `spec-create` workflow, which:

1. writes `requirements.md` (EARS criteria, numbered for traceability),
2. writes `design.md` grounded in the real codebase,
3. writes `tasks.md` in the format the hooks and `/spawn-worktree` parse,
4. runs an **adversarial review**: three reviewers attack the spec from different angles (plan fidelity, codebase grounding, implementability), and a separate skeptic tries to refute each finding,
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

Spend a few tool calls, no more, to give the workflow a head start: the 3-8 most relevant files, the test layout, and any conventions it must follow. Summarize in under 20 lines. The workflow agents will read the code themselves; this is a pointer list, not a design.

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
3. Tell me it's running: four phases (requirements, design, tasks, adversarial review and revision), and I can watch it with `/workflows`.

## Step 4: Close the loop

When the workflow returns:

1. If it returned an `error`, report what failed and stop. Don't hand-write the missing documents.
2. Report in this shape:
   ```
   Spec: specs/[feature-name]/  ·  Review result: [Ready | Ready after decisions | Needs work]

   requirements.md: [one-line summary]
   design.md:       [one-line summary]
   tasks.md:        [N tasks; parallel waves]

   Adversarial review: [N] fixed, [M] refuted, [K] need your decision
   Assumptions to check: [only the ones that matter]
   ```
   Mention any `unverified` findings or lost review lenses so I know coverage was partial.
3. **Open questions:** if `review.openQuestions` is non-empty, ask me with AskUserQuestion (up to 4 per call; put the reviewer's suggested fix first when it is a sensible default). Then apply my answers to the spec files, keeping requirements, design, and tasks consistent. Record each answer under a new "Decisions" entry in PLAN.md (if it exists) and in the "Needs Your Decision" table of `review.md`.
4. Finish with: "Spec ready. Use `/spec:implement [task-number]` to start, or `/spawn-worktree specs/[feature-name]` to run a parallel wave."

---

## Spec layout

```
specs/[feature-name]/
├── PLAN.md          # decisions (from /spec:plan)
├── requirements.md  # what: user stories, EARS criteria
├── design.md        # how: components, interfaces, traceability
├── tasks.md         # steps: ### Task N, files, acceptance, waves
└── review.md        # adversarial review record
```

Specs live at the project root, not in `.claude/`, so writing them never needs approval.
