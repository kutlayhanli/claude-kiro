# Running the spec workflows without the Workflow tool

`/spec:create`, `/spec:review`, `/spec:implement` and `/spawn-worktree` normally hand their multi-agent work to the Workflow tool. Some accounts don't have it: it's missing from your tools, or calling it is refused or blocked by policy. Then you run the same steps yourself, as the orchestrator, with the **Agent** tool and a **task list**. Use this file only in that case, or when I pass `--no-workflow`. Don't call the Workflow tool to find out: if it isn't in your tool list, it's unavailable.

The workflow scripts in `.claude/workflows/` stay the source of truth for what each agent is told. Read the script, build each prompt the way the script would (substituting its args), and send it with the Agent tool. Keep your own context small: act on each agent's report, and don't read agent transcripts.

## Agent types: model and effort per role

The Agent tool can pick a model per call but not an effort. The effort comes from the agent type, so ck writes one agent type per role:

```bash
ck agents sync            # add --override role.key=value for this run, as in /spec:implement
```

This writes `.claude/agents/ck-implementer.md`, `ck-test-writer.md`, `ck-fixer.md` and `ck-reviewer.md` with each role's model and effort from `ck agents`. The reviewer type has no edit tools. Use them as `subagent_type`:

| Work | `subagent_type` |
|---|---|
| impl-track task, its retry, its revision after review | `ck-implementer` |
| test-track task, its retry, its revision | `ck-test-writer` |
| merge conflict, red target branch | `ck-fixer` |
| review of a task's diff | `ck-reviewer` |
| spec-create agents (requirements, design, reviewers, refuters) | `general-purpose` (the session's model, as in the workflow) |

`ck init` and `ck upgrade` write them too, and `ck agents set` rewrites them. A running session picks up changed files within seconds. Only a session started before `.claude/agents/` existed needs a restart (`ck agents sync` says so). If the `ck-*` types are unknown to this session, use `general-purpose` and pass the role's model as the Agent call's `model` (`sonnet`, `opus`, `haiku` or `fable`; omit it for `inherit`). Tell me that efforts are the session's for this run, and that a restart fixes it.

A one-run override (`--model`, `--effort` and so on) rewrites the files for this run. Run `ck agents sync` without overrides when the run ends, so the next run gets my saved settings.

## Translating a script

| In the script | With the Agent tool |
|---|---|
| `agent(prompt, { schema })` | One Agent call with that prompt. Append: "End your answer with one JSON object with these fields: …", listing the schema's fields. Read the JSON from the report. |
| `parallel([...])` | All the Agent calls in **one message**, so they run at the same time. Wait for all of them. |
| `pipeline(items, a, b)` | Run stage `a` for every item in one message; start each item's stage `b` as soon as its own `a` returns. |
| `agent()` returned `null` (the agent died) | Treat it as the script does (usually: report it as lost). Don't redo the agent's work yourself. |
| `log(...)` | A one-line status update to me. |
| `phase(...)` | A task-list item (TaskCreate/TaskUpdate, or TodoWrite). |

Long-running agents (task agents, gates) go in the background (`run_in_background: true`); you're notified as each one finishes. Short steps you need right away (a planning call, a refuter) can run in the foreground.

## /spec:create and /spec:review

Follow `.claude/workflows/spec-create.js` from top to bottom with the args the command gave you (`mode` is `create` or `review`). In order: requirements; design and test plan in parallel; the two task tracks in parallel; the link step; the four review lenses in parallel, each lens's refuter as soon as that lens returns; the revision. Copy each prompt from the script verbatim, apart from the substitutions; they hold the rules that make the spec work (the test plan never sees the design, refuters default to refuting, and so on). Then return to the command's "close the loop" step with the result the script would have returned.

## /spec:implement (whole spec)

You are the scheduler that `.claude/workflows/spec-implement.js` would be. The task prompts are `taskPrompt`, `retryPrompt`, `reviewPrompt`, `revisePrompt` and `fixPrompt` in that script; build them with the same values (`spec`, `root`, `into`, `targetDir`, the worktree `<root>/.claude/worktrees/<spec>-task-<N>` on branch `feat/<spec>-task-<N>`, the brief `<root>/.claude/workflows/spec-implement-brief.md`). Do the merges and gates yourself instead of through merge agents.

1. **Task list.** One item per remaining task: `Task N: <title> [track]`. Mark each as it moves: running, reviewing, merging, merged, or failed (with the reason).
2. **Launch.** `ck plan <spec> --json --exclude <running and failed tasks>`. Take its `ready` tasks, keeping at most `run.max_concurrent` from `ck agents --json` (null means no cap) tasks with an implementing agent at work (fewer if I said the tests are memory-heavy). A task in review or waiting to merge doesn't count, so start the next ready task as soon as one moves on to review or the merge queue. From the main checkout: `ck worktree create <spec> <ready…> --install --base <into> --json`. For each CREATED or EXISTS entry, start a background Agent with the track's type (`ck-test-writer` or `ck-implementer`), description `implement <spec>: task <N>`, and the task prompt. Start them all in one message. BUSY and ERROR entries: report them, don't spawn into them.
3. **When a task agent finishes,** read its JSON report.
   - Not `done` with `gatePassed: true`: start one retry (retry prompt, same type, `--takeover`). If the retry fails too, the task fails. Its dependents never start. With `"onFailure": "halt"` (I asked to stop at the first failure), stop launching.
   - Done and the reviewer is enabled: start `ck-reviewer` in the background with the review prompt. On `changes`, send the revise prompt to the same implementer with SendMessage (it keeps its context), or start a new agent of its type with the revise prompt if SendMessage isn't available. Then review again, up to the reviewer's `rounds`. If blocking findings remain, the task fails: "review: blocking findings remain after N revision(s)".
4. **Merge, one at a time.** Only one merge and its gate run at any moment; queue the rest. From the main checkout: `ck worktree merge <spec> <N> --into <into> --json`.
   - MERGED: refresh dependencies in `targetDir` if the project has an install step, then run `ck gate <spec> --task <N>` there **in the background** (Bash `run_in_background`) and wait for its exit code. A slow gate is not a red gate.
   - BUSY: `ck worktree release <spec> <N>` once, then merge again.
   - CONFLICT (already aborted on the target): start `ck-fixer` with the CONFLICT instructions from `mergePrompt` (resolve in the task worktree, keep both sides' tests, gate passes there), then merge again.
   - SKIP: the task made no commits; it fails.
   - Gate red after a merge: start `ck-fixer` with the fix prompt, then re-run the gate yourself. Still red: stop launching, let running agents finish without merging them, and report. A red target branch always halts.
   - Every `fullGateEvery` merges, run the full `ck gate <spec>` the same way.
5. **After every merge or failure,** go back to step 2. Re-planning re-reads tasks.md, so fixes we commit to the target mid-run take effect.
6. **Finish** when nothing is running, queued or ready: run the full `ck gate <spec>` once more, then report as the command says (merged, failed with reasons, not started and why, wrong tests, files outside tasks, timing from each report's `startedAt` and `finishedAt`).

You orchestrate; you don't implement. Don't edit task code yourself, and don't merge a task whose gate or review didn't pass.

## /spec:implement (one task) and /spawn-worktree

- **One task, with a role that isn't `inherit`:** a foreground Agent of the track's type, working in the current checkout, with the prompt the command describes. If the reviewer is enabled and the gate passed, a foreground `ck-reviewer` reads `git diff` from where the task started; revisions go back to the implementer with SendMessage, up to `rounds`.
- **/spawn-worktree:** the Agent calls the command already shows, with `subagent_type` set to the track's type instead of `general-purpose`, and a `ck-reviewer` for each task whose gate passed, before the merge phase.
