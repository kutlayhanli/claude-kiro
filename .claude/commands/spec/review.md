---
description: Adversarially review an existing spec and apply the findings that survive
argument-hint: [spec-name]
allowed-tools: Read, Edit, Write, Grep, Glob, AskUserQuestion, Workflow
---

Review the specification: $ARGUMENTS

# Spec-Review: Adversarial Review of an Existing Spec

Use this after the spec or the code has drifted, after hand edits, or for specs written before `/spec:create` reviewed its own output. It runs the review half of the `spec-create` workflow: four reviewers attack the spec (plan fidelity, codebase grounding, implementability, test adequacy), a separate skeptic tries to refute each finding, and the survivors are applied. On an older spec without `test-plan.md`, the test-adequacy lens flags it and the revision adds one.

**Calling the Workflow tool here is intended.** Running `/spec:review` is my opt-in.

## Steps

1. Resolve the spec directory: `specs/$ARGUMENTS/`. If it only exists under `.claude/specs/`, tell me to run `ck migrate` first and stop. If `$ARGUMENTS` is empty, list the specs in `specs/` and ask which one.
2. Confirm `requirements.md`, `design.md`, and `tasks.md` all exist. If one is missing, say so and suggest `/spec:create` instead.
3. Check that `.claude/workflows/spec-create.js` exists; if not, tell me to run `ck init` and stop.
4. Call the **Workflow** tool with `scriptPath` set to the absolute path of `.claude/workflows/spec-create.js` and `args`:
   ```json
   {
     "mode": "review",
     "feature": "[spec-name]",
     "projectRoot": "[absolute project root]",
     "specDir": "[absolute path to specs/spec-name]",
     "description": "Review of an existing spec",
     "planPath": "[absolute path to PLAN.md if it exists, else null]",
     "evolvesFrom": null,
     "context": "[anything I said about why we're reviewing, plus recent changes you know of]",
     "date": "[today, YYYY-MM-DD]"
   }
   ```
5. When it returns, report: result, how many findings were fixed, refuted, or need my decision, and any partial coverage. Ask me the open questions with AskUserQuestion, apply my answers consistently across the spec files, and record them in `review.md` (and PLAN.md if it exists).

The full record is in `specs/[spec-name]/review.md`.
