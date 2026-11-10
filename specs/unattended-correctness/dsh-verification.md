# DeepSeek Harness (dsh) API verification

Findings report for requirement 18.1 (verification record) and 18.3 (findings report). Task 99.

**Verdict:** `dsh` cannot host the workflow's `ck()` calls. Per 18.3 the `dsh` phase stops here: no `dsh` adapter is built, `dsh` is not registered, and nothing else in the repository changes (18.5). Task 100 is marked Done with no code change, citing this report.

## What was checked

- Repository: `https://github.com/deepseek-ai/deepseek-harness`, fetched with `git clone --depth 1` (github.com, the host in ck's default `run.allowed_domains`).
- Version: root `package.json` reports `0.2.2-rc.1`. The SDK client `packages/sdk/client/package.json` reports the same version. The README calls the project a developer preview with compatibility-breaking changes expected.
- Commit checked: `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159` (merge of `release/dsh-0.2.2-rc.1` to master), committed 2026-11-04T16:40:00+08:00.
- Check date: 2026-11-10 (UTC).
- Interface compared: `Harness` in `src/claude_kiro/harnesses.py` (`run_arm`, `doctor`, `available`), plus the workflow's `agent()` and `ck()` requirements (service agent requests, and run local processes for `ck()`).

## Answers

- **canHostAgent: true (with limits).** The Python SDK (`python/sdk/README.md`) drives a runtime over newline-delimited JSON-RPC on stdio. `DeepSeekHarness.run(prompt, session_id=...)` returns `RunResult.final_response`. A host can service an `agent()` request as a prompt on a fresh session, using `session/prompt` and `session/wait`. Limits: the model and provider are fixed per runtime process at `initialize` (`InitializeParams`, `packages/sdk/protocol/src/types.ts`), so a per-call model or role map needs one runtime process per model. The protocol exposes no per-call tool restriction.
- **canHostCk: false.** The protocol has no request for the host to run a local process. The only server-to-client requests are `approval/request` (`HarnessSdkClientRequestMap`, `types.ts:240`) and `user-question/ask` (`HarnessSdkHostRequestMap`, `types.ts:60`). The method table in `packages/sdk/protocol/README.md` (client→server and server→client rows) has no process or shell method. A shell command can only run through the agent's own Bash tool, inside a model turn the model chooses to take. That is not a deterministic `ck()` call the workflow can wait on and receive the result of.

## What is missing

- A protocol method (or host-side callback) that runs a local process on the client's request and returns its exit code and output, which `ck()` needs.
- A per-call model or role selection for `agent()` in one runtime, or a documented way to route calls to different models without one runtime process per model.
- A per-call tool restriction for `agent()`, if the workflow relies on it.

Note: the workflow runner (Node) can run `ck` itself in its own process, outside `dsh`. That does not change the answer, because 18.1 asks whether `dsh` hosts the `ck()` call.

## Evidence

- `README.md`: "developer preview", "THERE WILL BE COMPATIBILITY-BREAKING CHANGES", `npx @deepseek-ai/dsh web`.
- `package.json:3`: `"version": "0.2.2-rc.1"`.
- `python/README.md`: the Python SDK starts the bundled `dsh --profile sdk` CLI as a subprocess.
- `python/sdk/README.md`: `DeepSeekHarness(...)`, `harness.run(...)`, `RunResult.final_response`, `session.run(...)`, `resume_session`, `on_user_question`, `approval_requests=True`. Model and provider are set at initialize.
- `packages/sdk/protocol/README.md`, "The SDK methods" table: client→server `initialize`, `session/prompt`, `session/query`, `session/resume`, `session/wait`, `session/working-directory/*`, `shutdown`. server→client `approval/request`, `user-question/ask`, `session.event`, `session.status`, `session.query.completed`, `subagent.started`, `subagent.finished`.
- `packages/sdk/protocol/src/types.ts:60-62` (`HarnessSdkHostRequestMap`, only `user-question/ask`) and `:240-242` (`HarnessSdkClientRequestMap`, only `approval/request`). Neither has a process-execution entry.
- `docs/architecture.md`: subagents are in-runtime child sessions (`subagent.started` / `subagent.finished`), not host-side processes.

## Re-check

A rerun of this verification is needed when `dsh` adds a process-execution request or a per-call model/tool selection in its SDK protocol. Re-check `packages/sdk/protocol/src/types.ts` (the two request maps) and the method table in `packages/sdk/protocol/README.md`. Until then the `dsh` phase stays stopped.

```json
{
  "repository": "https://github.com/deepseek-ai/deepseek-harness",
  "version": "0.2.2-rc.1 (commit fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159)",
  "checkedAt": "2026-11-10T21:02:14Z",
  "canHostAgent": true,
  "canHostCk": false,
  "missing": [
    "No protocol method or host callback for the client to run a local process and receive its exit code and output, which ck() needs (packages/sdk/protocol/src/types.ts:60-62, 240-242).",
    "No per-call model or role selection for agent(): the model is fixed per runtime process at initialize, so each model needs its own process.",
    "No per-call tool restriction for agent()."
  ],
  "evidence": [
    "README.md: developer preview, compatibility-breaking changes expected",
    "package.json:3: version 0.2.2-rc.1; packages/sdk/client/package.json: 0.2.2-rc.1",
    "commit fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159, committed 2026-11-04T16:40:00+08:00",
    "python/sdk/README.md: DeepSeekHarness, harness.run(), RunResult.final_response, resume_session, approval_requests",
    "packages/sdk/protocol/README.md: SDK methods table (client-to-server and server-to-client)",
    "packages/sdk/protocol/src/types.ts:60 HarnessSdkHostRequestMap (user-question/ask only)",
    "packages/sdk/protocol/src/types.ts:240 HarnessSdkClientRequestMap (approval/request only)"
  ]
}
```
