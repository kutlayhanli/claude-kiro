---
description: Adversarially review an existing spec and apply the findings that survive
argument-hint: [spec-name]
allowed-tools: Read, Edit, Write, Grep, Glob, AskUserQuestion, Workflow, Agent, TaskCreate, TaskUpdate, Bash(ck agents --json:*), Bash(ck principles init:*)
---

Review the specification: $ARGUMENTS

## Step 0: Hand-off check

Before anything else, and before choosing between the Workflow tool and agent mode, run `ck handoff review $ARGUMENTS --json` (from where you are now). It decides whether this run belongs in the main checkout instead of this isolated session, and answers with one JSON object:
- `handoff: false`: nothing to hand off. Continue with the steps below. Its `interactive` field says whether I am at the session (true) or this is an unattended run (false).
- `handoff: true` and `launched: true`: the run now continues in the main checkout under `ck run`. Print `command`, `mainCheckout`, `runBranch`, `follow` (how to watch the log) and `report` to me, then stop. Do nothing else.
- `handoff: true` and `launched: false`, or the command exits non-zero or fails to run: print its `error` and the `manualCommand` it gives, then stop. Never start the run from this session after a failed hand-off.

# Spec-Review: Adversarial Review of an Existing Spec

Use this after the spec or the code has drifted, after hand edits, or for specs written before `/spec:create` reviewed its own output. It runs the review half of the `spec-create` workflow: four reviewers attack the spec (plan fidelity, codebase grounding, implementability, test adequacy), a separate skeptic tries to refute each finding, and the survivors are applied. A fix that would change behaviour a criterion or property governs comes back to me as a decision instead, and a consistency check then diffs the revised files against a pre-revision snapshot and checks the changes against the criteria and properties. On an older spec without `test-plan.md`, the test-adequacy lens flags it and the revision adds one.

**Calling the Workflow tool here is intended.** Running `/spec:review` is my opt-in.

**Without the Workflow tool** (it isn't among your tools, or calling it is refused or blocked, or I pass `--no-workflow`): run `.claude/workflows/spec-create.js` by hand with the Agent tool and the same args, as `.claude/workflows/without-workflow-tool.md` describes, and tell me once that you're doing so. The steps, prompts, and result are the same.

## Steps

1. Resolve the spec directory: `specs/$ARGUMENTS/`. If it only exists under `.claude/specs/`, tell me to run `ck migrate` first and stop. If `$ARGUMENTS` is empty, list the specs in `specs/` and ask which one.
2. Confirm `requirements.md`, `design.md`, and `tasks.md` all exist. If one is missing, say so and suggest `/spec:create` instead.
3. Run `ck principles init` (from the project root). If it created the principles file, tell me so in one line; otherwise say nothing. Then run `ck agents --json` and keep its `agents.arbiter` role. `arbiterAgent` is `ck-arbiter` in plugin mode and `ck-arbiter` in vendor mode; `runnerAgent` is `ck-runner` in plugin mode and `ck-runner` in vendor mode (pass `null` for one whose file is missing from `.claude/agents/` in vendor mode). Then call the **Workflow** tool with `args` as below, picking the script by the project's mode first: if `.claude/ck-manifest.json` records `"mode": "vendor"` (copies made by `ck vendor`; the plugin is off there), use `scriptPath` set to the absolute path of `.claude/workflows/spec-create.js` and don't try the plugin's name. Otherwise use `name: "spec:spec-create"` (the workflow this plugin ships); only if no workflow by that name is found, fall back to that `scriptPath`. If the file is missing too, tell me to run `ck setup` (installs the plugin) and stop.
   ```json
   {
     "mode": "review",
     "feature": "[spec-name]",
     "projectRoot": "[absolute project root]",
     "specDir": "[absolute path to specs/spec-name]",
     "description": "Review of an existing spec",
     "planPath": "[absolute path to PLAN.md if it exists, else null]",
     "evolvesFrom": null,
     "arbiterAgent": "[ck-arbiter, or ck-arbiter in vendor mode]",
     "runnerAgent": "[ck-runner, or ck-runner in vendor mode]",
     "agents": { "arbiter": "[agents.arbiter from ck agents --json]" },
     "context": "[anything I said about why we're reviewing, plus recent changes you know of]",
     "date": "[today, YYYY-MM-DD]"
   }
   ```
4. When it returns, report: result, how many findings were fixed, refuted, or need my decision, what the consistency check fixed or raised (`consistency`), and any partial coverage. Report each entry of `review.proposedCuts` with its story, its weakness and what it saves; a review never applies a cut, so say they wait for my approval, and that approving one is done through `/spec:create`. Then run `ck stats collect --run <wf_id> --auto` outside the sandbox and quote the line it prints (where the record went, or why it was skipped; if it failed, quote the retry command it gives). Never let its result change what you report about the run. In agent mode (no `wf_id`, the run was started with `ck stats begin`), run `ck stats collect --agents <run_id> --auto` instead, outside the sandbox, and quote its line the same way. Ask me the open questions (`review.openQuestions`, every `source`: review, revise, consistency; show `conflictsWith` and offer the `options` for the last two) with AskUserQuestion, apply my answers consistently across the spec files, and record them in `review.md` (and PLAN.md if it exists).

The full record is in `specs/[spec-name]/review.md`.
