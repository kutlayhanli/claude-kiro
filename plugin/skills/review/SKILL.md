---
description: Adversarially review an existing spec and apply the findings that survive
argument-hint: [spec-name]
allowed-tools: Read, Edit, Write, Grep, Glob, AskUserQuestion, Workflow, Agent, TaskCreate, TaskUpdate
---

Review the specification: $ARGUMENTS

# Spec-Review: Adversarial Review of an Existing Spec

Use this after the spec or the code has drifted, after hand edits, or for specs written before `/spec:create` reviewed its own output. It runs the review half of the `spec-create` workflow: four reviewers attack the spec (plan fidelity, codebase grounding, implementability, test adequacy), a separate skeptic tries to refute each finding, and the survivors are applied. A fix that would change behaviour a criterion or property governs comes back to me as a decision instead, and a consistency check then diffs the revised files against a pre-revision snapshot and checks the changes against the criteria and properties. On an older spec without `test-plan.md`, the test-adequacy lens flags it and the revision adds one.

**Calling the Workflow tool here is intended.** Running `/spec:review` is my opt-in.

**Without the Workflow tool** (it isn't among your tools, or calling it is refused or blocked, or I pass `--no-workflow`): run `${CLAUDE_PLUGIN_ROOT}/workflows/spec-create.js` by hand with the Agent tool and the same args, as `${CLAUDE_PLUGIN_ROOT}/resources/without-workflow-tool.md` describes, and tell me once that you're doing so. The steps, prompts, and result are the same.

## Steps

1. Resolve the spec directory: `specs/$ARGUMENTS/`. If it only exists under `.claude/specs/`, tell me to run `ck migrate` first and stop. If `$ARGUMENTS` is empty, list the specs in `specs/` and ask which one.
2. Confirm `requirements.md`, `design.md`, and `tasks.md` all exist. If one is missing, say so and suggest `/spec:create` instead.
3. Call the **Workflow** tool with `args` as below, picking the script by the project's mode first: if `.claude/ck-manifest.json` records `"mode": "vendor"` (copies made by `ck vendor`; the plugin is off there), use `scriptPath` set to the absolute path of `.claude/workflows/spec-create.js` and don't try the plugin's name. Otherwise use `name: "spec:spec-create"` (the workflow this plugin ships); only if no workflow by that name is found, fall back to that `scriptPath`. If the file is missing too, tell me to run `ck setup` (installs the plugin) and stop.
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
4. When it returns, report: result, how many findings were fixed, refuted, or need my decision, what the consistency check fixed or raised (`consistency`), and any partial coverage. Then run `ck stats collect --run <wf_id> --auto` outside the sandbox and quote the line it prints (where the record went, or why it was skipped; if it failed, quote the retry command it gives). Never let its result change what you report about the run. In agent mode (no `wf_id`, the run was started with `ck stats begin`), run `ck stats collect --agents <run_id> --auto` instead, outside the sandbox, and quote its line the same way. Ask me the open questions (`review.openQuestions`, every `source`: review, revise, consistency; show `conflictsWith` and offer the `options` for the last two) with AskUserQuestion, apply my answers consistently across the spec files, and record them in `review.md` (and PLAN.md if it exists).

The full record is in `specs/[spec-name]/review.md`.
