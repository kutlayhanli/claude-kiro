# Test Plan: Prompt-Free Runs

Requirements: [requirements.md](requirements.md). Decision record: [PLAN.md](PLAN.md) (D1-D20 bind this plan).
I wrote this from the requirements and the current test suite. After review, every name in this plan (hooks, seams,
CLI commands, record keys) is design.md's name; the Harness Contract below lists them. There is one source of names:
design.md.

## Strategy

The project is a Python CLI (`ck`, click), tested with pytest, pytest-xdist and Hypothesis
(`uv run pytest -q -n auto`, the `specs/ck.json` verify line). The Workflow scripts are tested in Node with stubbed
agents (`tests/js/*.test.mjs`, run by `tests/test_workflow_scripts.py`) and through the long-lived Node bridge
(`tests/landing_bridge.py`, `tests/js/bridge.mjs`) when Hypothesis drives them. This plan uses the same levels:

- **Integration first, through the real hook and CLI entry points.**
  - Guard behaviour is driven through `claude_kiro.cli.runner.execute_hook(name, payload)` with Claude Code-shaped
    input (as `tests/test_guard.py` does), against the real `project` fixture (a real git repo on branch `feature`
    off `main`, with `tests/test_calc.py` committed on `main`).
  - Each critical path also has one end-to-end case through `python -m claude_kiro.cli --hook ...` with JSON on
    stdin.
  - The post-check is tested as Claude Code runs it: the PreToolUse hook first, then the **real shell command**
    (`bash -c` in the repo), then the PostToolUse hook with the same `tool_use_id`. These tests work whether the
    implementation snapshots files before the command or diffs against git afterwards.
- **Analytics through the real CLI as a subprocess** (`stats_helpers.ck_sub` / `stats`). Each test gets a fake
  `$CLAUDE_CONFIG_DIR` written with the real transcript layout, plus its own store (`fresh_env()`).
  - Interruption lines copy the real shapes found in this machine's transcripts. A guard decision is an
    `attachment` line `{"type": "hook_success", "hookName": "PreToolUse:Bash", "hookEvent": "PreToolUse",
    "toolUseID": ..., "stdout": "<hook JSON>", "command": "<hook command>"}`. A Claude Code denial is a `user`
    line with a `tool_result` that has `is_error: true` and fixed phrasing.
  - Near-misses also come from real transcripts: RTK's `permissionDecision: "allow"` attachments, and non-error
    tool results that only quote the phrases (a `Read` of PLAN.md contains every one of them).
- **Workflow mode** (D12): Node tests run the real `plugin/workflows/spec-implement.js` under the stub runtime
  (`runWorkflow`, `fake_project.mjs`), extended with guard stops. Hypothesis properties go through the bridge.
- **Agent mode** (D12): the orchestrator is a Markdown document, so its contract is checked by static tests on the
  packaged files and their vendored renderings, as `tests/test_stats_agents_docs.py` does today. The behaviour the
  agent-mode instructions rely on lives in ck CLI commands that both modes call (`ck agents --guard-ladder`,
  `ck worktree park`, `ck worktree resolve`, `ck handoff`). Those commands get real-CLI tests, so both modes share one tested
  implementation.
- **Fakes only for what ck does not own**:
  - `claude`: a fake binary on `PATH`, as in `tests/test_run.py`.
  - `bwrap`/`socat` presence: `shutil.which` patched, as in `tests/test_run.py`, for in-process calls. A `ck run`
    child that `ck handoff` starts with `Popen` does not see that patch, so those tests put fake `bwrap`/`socat`
    executables on the child's `PATH` (or pass `--unsandboxed` where the case allows), so the child's preflight
    never depends on this machine.
  - The notification command: a test-owned shell script that writes its stdin to a file, fails, or sleeps.
  - Claude Code's transcript layout: written by the harness.
  - The network: the `no_network` fixture.
  - `git` misbehaving: a test-owned wrapper on `PATH` that fails or sleeps, only for the failure cases.

  Nothing ck owns is mocked: not the guard, the post-check, the store, the collector, git_utils or the gate.
- **Properties (Hypothesis)** for every universally quantified criterion.
  - Pure seams use `@settings(derandomize=True, max_examples=200, database=None)`. These are command parsing,
    family classification, caller classification, the ladder, the pre-filter decision and record shaping.
  - Properties that need a real repo, a real hook call or the CLI run with `@settings(derandomize=True,
    max_examples=40, database=None, deadline=None)`, on a module-scoped repo built once (no function-scoped
    fixture inside `@given`).
  - Every property test file declares a module-level `PROPERTIES = {"P-n": "test_name"}` map, which the gate reads
    (`claude_kiro.properties.map_ids`).
- **Unit tests** only as the table-driven half of a property (for example the family table, or the ladder table).

Every new test fails before the feature exists, for the right reason. Today:
- the guard asks on `sed -i` and redirects, and never denies by caller;
- there is no `post-bash-check` hook (`execute_hook` returns an unknown-hook error);
- `ck handoff`, `ck worktree park`, `ck worktree resolve`, `ck agents --guard-ladder` and `ck run --command` are
  unknown (click exit 2);
- the analytics seams raise `AttributeError` from `claude_kiro.analytics.__getattr__`;
- `guardLadder` is missing from the decisions block (`load_decisions.mjs` throws "decision function missing");
- records have no `interruptions` key.

Project modules are imported inside test bodies, so every file still collects before the implementation lands.

**Existing tests that change.** These existing tests assert behaviour this feature changes. All of them are edited by
one test-track task (Task 3), which owns `tests/test_guard.py` and `tests/test_plugin_modes.py` while In Progress,
so no two parallel tasks edit the same file:
- `tests/test_guard.py::test_bash_deleting_tests_asks` asserts asks this feature removes (`sed -i` and `echo '' >` on
  `tests/test_calc.py`). It is split: the `rm`, `cd . && rm -rf tests` and `git rm` lines keep asking for the main
  session (1.1, 4.2; `tests` is now judged by expanding it to its base-ref files); the `sed -i` and redirect lines
  move to TC-2 and now pass.
- Five tests send main-session input (no `agent_id`) and assert `ask`/`deny` on requirements.md and test-plan.md
  rewording or deletion after implementation started, which 5.1 now lets through:
  `test_requirements_frozen_once_implementation_starts`, `test_rewording_or_removing_a_requirement_still_asks`,
  `test_test_plan_frozen_once_implementation_starts`, `test_rewording_or_deleting_a_test_plan_row_asks` and
  `test_test_plan_guard_mode_deny_and_off`. Each becomes a task-agent test (the `guard()` call gains `agent_id`),
  expecting `deny` where it expected `ask` or `deny` (4.1, 5.2) and `None` where the kind is `off`. Each gets a
  main-session twin that expects `None` (5.1).
- `tests/test_plugin_modes.py::test_plugin_hooks_stay_silent_outside_ck_projects` expects a `permissionDecision`
  for `rm tests/test_x.py` in a directory with no git, where the base is unknown and 1.7 now passes. It is changed
  to build a real git repo with `tests/test_x.py` committed on the base branch before `specs/ck.json` is added, so
  the guarded half still expects a decision.

The NFR "Backward compatibility" allows these edits. They remove or change assertions in existing test files, so
they must be made by that test-track task while In Progress; otherwise the guard and the gate block them. All other
existing guard, collect, show, export and submit tests stay unchanged and must stay green.

## Test Infrastructure

### Exists (reuse as is)

| Path | What |
|---|---|
| `tests/conftest.py` | autouse isolation of `CK_CONFIG_HOME`, `XDG_DATA_HOME`/`CK_DATA_HOME`, `CLAUDE_CONFIG_DIR`, git identity, gate registry; `no_network`; `project` (real repo, `tests/test_calc.py` on `main`, branch `feature`); `session`; `write_tasks(root, t1=..., t2=...)` (Task 1 is the test-track task owning `tests/test_subtract.py`) |
| `tests/test_guard.py` | `guard(project, session, tool, **input)` and `decision(result)` helpers; the pattern for driving `pre-tool-guard` |
| `tests/stats_helpers.py` | `FakeClaudeHome`, `FakeSession` (orchestrator transcript, `implement_run`, `create_run`, `tool_agents`), `AgentSpec`, `ToolAgentSpec`, `TaskSpec`, `implement_result`, `fresh_env()`, `ck_sub`, `stats`, `load_record`, `store_files`, `all_bytes`, `flatten_paths`, `seeded_home`, `begin`, `spec_repo`, `land`, `fail`, `logging_git`, `CONFIDENTIAL` |
| `tests/test_run.py` | `repo(tmp_path)`, `fake_home`, `sandbox_tools(monkeypatch, present)`, `fake_claude(...)`, `plan_of(root, *args)` for `ck run --dry-run --json` |
| `tests/js/load_workflow.mjs`, `fake_project.mjs`, `load_decisions.mjs`, `bridge.mjs`; `tests/landing_bridge.py` | stub Workflow runtime, fake project, the pure decisions block (`NAMES`), JSON-lines bridge with Hypothesis strategies `dags()`, `scenarios()` |
| `tests/test_stats_export.py` | `schema_section`, `documented_paths`, `assert_documented` (SCHEMA.md vs records) |
| `tests/test_stats_leak.py` | the export/submit leak flow (`World`) |
| `tests/test_plugin.py` | `test_hooks_json_reproduces_the_hooks_ck_init_configured`, `test_dogfood_copies_in_this_repo_match_the_plugin_source` (packaged == vendored) |
| `tests/test_gate.py` | `ck gate` on a real repo, including the "No existing tests deleted or weakened" check |

### Add

1. **`tests/conftest.py`: autouse `_not_headless`.** This deletes `CK_RUN_HEADLESS` and `CLAUDE_CODE_ENTRYPOINT`
   (design S5 `is_headless`) and the variables `ck run` sets for its child (read once from `ck run --dry-run
   --json`'s `env`, a no-op while that fails). Without it, a suite run inside `ck run` or `claude -p` would classify
   every main-session test as a task agent.

2. **`tests/guard_helpers.py`** (test-owned; imports only `claude_kiro.cli.runner.execute_hook`):
   - `hook_input(project, session, tool, *, agent_id=None, agent_type=None, tool_use_id=None, event="PreToolUse",
     tool_response=None, **tool_input) -> dict` builds the documented fields: `session_id`, `transcript_path`,
     `cwd`, `permission_mode`, `hook_event_name`, `tool_name`, `tool_input`, `tool_use_id`, and `agent_id`/
     `agent_type` only when given.
   - `pre(project, session, tool, *, agent_id=None, env=None, **tool_input) -> Optional[dict]` calls
     `execute_hook("pre-tool-guard", ...)`. `env` temporarily sets environment variables, through `monkeypatch`
     passed in, never `os.environ` directly.
   - `run_bash(project, session, command, *, agent_id=None, cwd=None, via="post-bash-check") -> BashOutcome(pre,
     ran, returncode, post)` runs the full sequence: PreToolUse hook; then, unless it denied or asked,
     `subprocess.run(["bash", "-c", command], cwd=cwd or project)`; then `execute_hook(via, ...)` with
     `hook_event_name: "PostToolUse"`, the same `tool_use_id`, and `tool_response: {"stdout", "stderr",
     "interrupted": false}`. `via="done-verify"` sends the PostToolUse to the installed hook.
   - `post_message(result) -> Optional[str]` returns the text shown to Claude by a PostToolUse result. It accepts
     either `{"decision": "block", "reason": ...}` or `{"hookSpecificOutput": {"additionalContext": ...}}`, so
     the tests do not depend on which form the design picks. It returns `None` for no output.
   - `decision(result)`, re-exported from `tests/test_guard.py`.
   - `commit_on_base(project, rel, content)` adds a file on `main` and merges it into `feature`, so it "exists on
     the base branch".
   - `new_on_branch(project, rel, content)` commits on `feature` only.
   - `ck_json(project, **guard_modes)`.
   - `big_repo(tmp)` builds 1,000 tracked files: 950 source and 50 test files.
   - `worktree_of(project, name, under=".claude/worktrees")` runs `git worktree add`.
   - `claimed_task_worktree(project, spec, n)` runs the real `ck worktree create <spec> <n>` and `ck worktree claim`
     and returns the worktree's absolute path, so a task worktree exists the way task agents use it (cwd stays at
     the main checkout).
   - `fake_git(bin_dir, mode)` writes a `git` wrapper that `fail`s (exit 128) or `sleep`s 120 s.
   - `notify_script(bin_dir, mode, out)` writes `ok` (stdin, env and argv to `out`, exit 0), `fail` (exit 3) and
     `slow` (sleep 30) variants. It also writes a `missing` path that does not exist.
   - `set_notify(command)` runs `ck agents set run.notify_command <command>` against the isolated `CK_CONFIG_HOME`.
   - `guard_events(env=None) -> list[dict]` reads `claude_kiro.analytics.guardlog.log_path(env)` and its rotated
     `guard-events.1.jsonl`.
   - `fake_sandbox_tools(bin_dir)` writes executable `bwrap` and `socat` stubs for a child process's `PATH`.
   - `sleeping_fake_claude(bin_dir, record)` writes a fake `claude` that writes its argv and cwd to `record` and then
     sleeps 5 s, so a detached `ck run` child outlives the 3-s early-exit window; tests poll for `record` with a
     bounded wait, never a fixed sleep.
   - `parking_fake_claude(bin_dir, record, spec, task)` writes a fake `claude` that runs the real `ck worktree park
     <spec> <task> --json` with a fixed note on stdin (inheriting the env `ck run` gives it), writes that output to
     `record`, sleeps 3 s and exits 0 (TC-88).

3. **`tests/fixtures/hook_inputs/`**: hook inputs recorded from real Claude Code, one JSON file each:
   - `main-session-pretooluse.json`;
   - `agent-tool-subagent-pretooluse.json`;
   - `workflow-agent-pretooluse.json`;
   - `headless-pretooluse.json` and `headless-env.json` (the environment of a `claude -p` hook process, with the
     values of `HOME`/`PATH` replaced by placeholders).

   Plus `capture-summary.json`: `{"workflowAgentHasAgentId": bool, "agentToolSubagentHasAgentId": bool,
   "claudeCodeVersion": str, "capturedAt": str}`, the recorded answer to 3.4's question, which TC-23 asserts on, and
   `postcheck-block-attachment.json`, one real PostToolUse `hook_blocking_error` transcript line (from a
   `done-verify` block), which the stats line builders copy.

   Capture procedure (`tests/fixtures/hook_inputs/capture.sh`, run by me outside any sandbox, because it needs real,
   paid Claude Code sessions):
   1. Create a temp git project with `specs/ck.json` and `.claude/settings.json` registering
      `python3 <abs>/record_hook.py <out-dir>` on `PreToolUse` (matcher `Edit|Write|MultiEdit|Bash`).
      `record_hook.py` writes its stdin and its environment's `CLAUDE_*`/`CK_*` keys to one file per call.
   2. Headless and Workflow agent: `claude -p --permission-mode bypassPermissions "<prompt>"`, where the prompt runs
      one `Bash` call itself, then a one-agent Workflow script whose agent runs one `Bash` call, then one Agent-tool
      subagent that runs one `Bash` call. This yields `headless-pretooluse.json`, `headless-env.json`,
      `workflow-agent-pretooluse.json` and `agent-tool-subagent-pretooluse.json`.
   3. Main session: an interactive `claude` session in the same project, where I approve one `Bash` call. This
      yields `main-session-pretooluse.json`. This step is manual by nature.
   4. The script rewrites names to invented ones, replaces session and agent ids with stable fakes and `HOME`/`PATH`
      with placeholders, keeps every key, and writes `capture-summary.json` from what it saw.

   The live test TC-23 re-captures the Workflow-agent input when `CK_LIVE_CLAUDE=1` (opt-in, not in the gate); the
   gate keeps the answer through `capture-summary.json`.

4. **`tests/stats_helpers.py` additions** (test-owned):
   - `guard_attachment(tool_use_id, decision, reason, *, ts_ms, hook="pre-tool-guard", plugin=False) -> dict`
     writes the real `hook_success` attachment line. `command` is `ck --hook pre-tool-guard`, or the plugin shim
     form `"<root>/bin/ck" --hook pre-tool-guard --plugin-hook`. `stdout` is the hook JSON.
   - `postcheck_attachment(tool_use_id, message, *, ts_ms, gate_text="")` writes the real shape of a PostToolUse
     block, copied from `postcheck-block-attachment.json`: `{"type": "attachment", "attachment": {"type":
     "hook_blocking_error", "hookName": "PostToolUse:Bash", "hookEvent": "PostToolUse", "toolUseID": ...,
     "blockingError": {"blockingError": "<gate_text>\n[ck post-check] <message>"}}}` (gate text first when given).
   - `gate_block_attachment(tool_use_id, *, ts_ms)` is a near-miss: the same shape holding only a gate failure
     report, with no `[ck post-check]` text.
   - `rtk_allow_attachment(tool_use_id, *, ts_ms)` is a near-miss: another hook's `allow`.
   - `denial_result(tool_use_id, kind, *, ts_ms)` writes `is_error: true` with the real phrasing:
     - `auto`: "Permission for this action was denied by the Claude Code auto mode classifier. Reason: [x]. ...";
     - `headless`: "... requires approval, and this session has no approval surface";
     - `reject`: "The user doesn't want to proceed with this tool use. ...";
     - `isolation`: "This session is isolated in the worktree <path>, but ..." and the "This agent is isolated"
       variant.
   - `quoted_result(tool_use_id, kind, *, ts_ms)` is a near-miss: `is_error` absent or false, with the phrase in
     the middle of file content.
   - `tool_use(tool_use_id, tool, command=None, *, ts_ms)`.
   - `interruptions(target, events)` appends, per event `(kind, role-target, tool, command, ts_ms, wait_ms)`, the
     tool_use, attachment and result lines to an orchestrator transcript (`FakeSession`), a workflow agent
     (`FakeRun`, agent index) or an Agent-tool subagent (`FakeToolAgents`, index), in timestamp order.
   - `AgentSpec.extra_lines` and `ToolAgentSpec.extra_lines: List[dict]` merge raw lines into the agent
     transcript.
   - `write_guard_log(env, events)` writes guard-log events in the contract format, for the cross-check.
   - `interruption_strategy()` is a Hypothesis strategy that draws a list of events over roles × families ×
     categories, mixed with near-misses and malformed lines (`"{not json"`, `{}`, `{"type": "attachment"}`,
     unknown `type`). It returns the events and the expected counts.

5. **`tests/js/fake_project.mjs` options** (test-owned):
   - `guardStops: {n: ceiling}`: task `n` returns `guardStop: {file, kind, test, requirement}` on every attempt
     whose model is at or below `ceiling` in `haiku < sonnet < opus < fable`. `'always'` never resolves.
   - `parkOutcome: {parked, notification, detail}` is the answer of the `park <N>` agent (default `{parked: true,
     notification: 'sent', detail: ''}`).
   - `guardStopIn: {n: 'revise'}`: the guard stop happens in task `n`'s first revise call instead of its task call.

   Agents labelled `park <N>` are recorded like `land` agents (`state.parks`).

6. **`tests/js/bridge.mjs` op** `{op: 'ladder', model} -> {ok, ladder}` calls the decisions block's `guardLadder`.
   **`tests/js/load_decisions.mjs`** exports `GUARD_NAMES = ['guardLadder', 'guardStopOf', 'parkAction']` and loads
   them only when asked (`loadDecisions({extra: GUARD_NAMES})`), so existing tests stay green before the workflow
   lands.

## Test Cases

### Story 1: pre-check of shell commands (`tests/test_guard_bash.py`)

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-1 | 1.1, 4.1, 4.2 | integration | Given `tests/test_calc.py` exists on `main` and no task owns it. When the main session runs each of `rm tests/test_calc.py`, `rm -f`, `mv tests/test_calc.py /tmp/x.py`, `git rm -q`, `git mv`, `unlink`, `truncate -s 0`, `rm -rf tests`, `rm -rf tests/`, `git rm -r tests`, then `decision == "ask"` (ck.json `guard.tests: ask`). The directory forms are blocked because `tests` expands to its base-ref files, which include an existing test (design S7). With `deny`, each is `"deny"`. With `agent_id` set and `ask`, each is `"deny"`. The file is untouched after every call. | real git repo, `execute_hook("pre-tool-guard")` | `tests/test_guard_bash.py` |
| TC-2 | 1.2, NFR zero prompts | integration | Given the same repo. When a task agent (`agent_id`) or the main session runs `sed -i 's/3/3/' tests/test_calc.py`, `sed -i.bak ...`, `perl -pi -e 's/a/a/' ...`, `python - <<'EOF'\nopen('tests/test_calc.py','a').write('')\nEOF`, `echo x >> tests/test_calc.py`, `cat > tests/test_calc.py <<EOF ... EOF`, then the pre-check returns `None` for each. | real repo, hook | `tests/test_guard_bash.py` |
| TC-3 | 1.3 | integration | When a task agent runs `rm` / `mv` / `git rm` on: (a) `tests/test_scratch.py`, committed on `feature` only; (b) `tests/test_subtract.py` (exists on `main`) with Task 1 In Progress (owned); (c) `rm -rf tests/__pycache__` after a real `python -m pytest` created it (the directory expands to no base-ref test file); (d) `rm tests/.pytest_cache -rf`; (e) `find tests -name '*.pyc' -delete`. Then each returns `None`. | real repo, real pytest run, hook | `tests/test_guard_bash.py` |
| TC-4 | 1.4 | integration | Given a linked worktree `W` of the repo (branch off `feature`) with the hook's `cwd` at the main checkout. When the command is `cd W && rm tests/test_calc.py`, `cd W; rm tests/test_calc.py` or `cd "W" && git rm tests/test_calc.py`, the result is ask (main) or deny (task). When it is `cd W && rm tests/test_new_scratch.py` (new in `W`), the result is `None`. | real worktree, hook | `tests/test_guard_bash.py` |
| TC-5 | 1.5, 4.3 | integration | When a task agent is denied for `rm tests/test_calc.py`, the `permissionDecisionReason` names `tests/test_calc.py` and "delete". It says to keep the test, not to work around the block (for example "do not", "work around"), and to report the conflict quoting the test and the requirement. | hook | `tests/test_guard_bash.py` |
| TC-6 | 1.6 | integration | Given `guard.tests: "off"`. When the main session and a task agent run `rm tests/test_calc.py` and a weakening `sed -i '/assert add(1, 2)/d' tests/test_calc.py` through `run_bash`, then pre is `None` and post is `None` for both, and the guard log has no event. | real repo, both hooks, real bash | `tests/test_guard_bash.py` |
| TC-7 | 1.7 | integration | The command is `rm tests/test_calc.py`, run in each of these situations: (a) a plain directory with `tests/test_calc.py` and no `.git`; (b) a repo whose ck.json `base` names a missing ref; (c) a repo with an unborn HEAD; (d) a repo whose `.git/HEAD` is garbage. In each, pre returns `None` for the main session and for a task agent, and never `ask`. | real dirs/repos, hook | `tests/test_guard_bash.py` |
| TC-8 | NFR zero prompts | e2e | Replay as a task agent, through `python -m claude_kiro.cli --hook pre-tool-guard` (stdin JSON), each ask category from PLAN.md's transcript analysis: `sed -i` on a test; `rm` of a new scratch test; a python heredoc rewrite; `rm -rf tests/__pycache__`; `git rm` of a new test; `cd <worktree> && sed -i ...`. Each has exit 0 and empty stdout (no ask, no deny). | CLI subprocess, real repo | `tests/test_guard_bash.py` |
| TC-71 | 1.1, 4.1, 3.1 | integration | Given a claimed task worktree `W = claimed_task_worktree(project, "calc", 1)` and hook `cwd` at the main checkout. A task agent's `rm W/tests/test_calc.py`, `git -C W rm tests/test_calc.py` and `rm -rf W/tests` are each `deny` (judged against `W`'s own base and owned set), and `rm W/tests/test_new_scratch.py` (new in `W`) is `None`. | real claimed task worktree, hook | `tests/test_guard_bash.py` |

### Story 2: post-check (`tests/test_guard_postcheck.py`)

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-9 | 2.1, 2.2, NFR same safety | integration | When a task agent runs `sed -i '/assert add(1, 2) == 3/d' tests/test_calc.py` through `run_bash`, then pre is `None`, the command ran, and `post_message` names `tests/test_calc.py` and "removed assertion" (or the `weakening()` wording "removes 1 assertion"), and says to restore the test and report the conflict. | both hooks, real sed, real git | `tests/test_guard_postcheck.py` |
| TC-10 | 2.1, 2.2, NFR same safety | integration | A python heredoc that adds `@pytest.mark.skip` above `def test_add():` gives a catch naming "skip". A `python -c` that drops `test_add_negative` gives a catch naming "test case". | both hooks, real python | `tests/test_guard_postcheck.py` |
| TC-11 | 2.3, 2.9 | integration | When the main session runs the TC-9 `sed` under `guard.tests: ask`, `post_message` names the file and tells Claude to ask me before keeping the change. Under `guard.tests: deny`, it names the file and the kind and tells Claude to restore the test. Neither tells it to report a conflict as a task agent would. | both hooks | `tests/test_guard_postcheck.py` |
| TC-12 | 2.4 | integration | Each of these gives `post` `None`: sed that renames a variable (`s/add(1, 2)/add(2, 1)/`); a heredoc that appends a new test with an assertion; sed that weakens `tests/test_scratch.py`, committed on `feature` only; sed that weakens owned `tests/test_subtract.py` while Task 1 is In Progress. | both hooks | `tests/test_guard_postcheck.py` |
| TC-13 | 2.5 | integration | `ls tests`, `cat tests/test_calc.py`, `git status`, `git branch`, `find tests -name '*.py'`, `grep -n assert tests/test_calc.py` give `post` `None`, and the pre-filter seam `guard_policy.may_write(cmd)` is `False`. `sed -i 's/x/x/' calc.py` (non-test) ran and leaves no changed test files, so `post` is `None`. | hooks, real git | `tests/test_guard_postcheck.py` |
| TC-14 | 2.6 | integration | Given `tests/test_calc.py` already weakened by an earlier sed (caught once), when the next command is `echo hi > notes.txt`, then `post` is `None`. When the next command weakens it further (removes the second assertion), then exactly one new catch is reported, for that command. | both hooks, real git | `tests/test_guard_postcheck.py` |
| TC-15 | 2.7, NFR never blocks | integration | Each of these returns no output and exit 0 within 5 s (the 3-s budget plus process start), through `python -m claude_kiro.cli --hook post-bash-check` and through `python -m claude_kiro.cli --hook done-verify` (the installed path) on a non-Done Bash call, and the run continues: (a) a `git` wrapper that exits 128 on `PATH` for the post hook; (b) the weakened test file `chmod 000` after the command (skipped as root); (c) a `git` wrapper that sleeps 120 s. | CLI subprocesses, fake git, real fs | `tests/test_guard_postcheck.py` |
| TC-16 | 2.8 | integration | Given a task branch where a sed weakened `tests/test_calc.py` and the post-check catch was ignored, when `ck gate calc` runs, then it exits non-zero with the "No existing tests deleted or weakened" check red. | real `ck gate` CLI, real git | `tests/test_guard_postcheck.py` |
| TC-17 | NFR hook overhead | integration | In `big_repo` (1,000 tracked files), for a command the pre-filter skips (`ls`), the post-check's in-process median over 5 runs is under 100 ms, through `post_bash_check.check` and through `done_verify`'s Bash path. For a sed that changes 5 test files, the median is under 1 s. | real git, real fs, timing | `tests/test_guard_postcheck.py` |
| TC-18 | 2.1-2.2 | e2e | Through `python -m claude_kiro.cli --hook pre-tool-guard`, then `bash -c`, then `python -m claude_kiro.cli --hook post-bash-check` (stdin JSON, real subprocesses), the TC-9 weakening is reported and `git diff` shows the sed's change. | CLI subprocesses | `tests/test_guard_postcheck.py` |
| TC-19 | 2.x registration, 3.4 | integration | `HookRegistry._hooks` has `post-bash-check`. `HOOK_SETTINGS` and `plugin/hooks/hooks.json` still register `done-verify` on `PostToolUse` with a matcher that includes `Bash` (the installed path of the post-check) and are otherwise unchanged, except that when `capture-summary.json` says Workflow agents lack `agent_id` (the marker is built) both have exactly one added `UserPromptSubmit` entry for `marker-clear` (changed by D16: it was "unchanged" in every case). The existing parity test and the dogfood test stay green. | real files, `ck init` | `tests/test_guard_postcheck.py` |
| TC-73 | 2.1, 2.2, NFR same safety | integration | Given a claimed task worktree `W` and hook `cwd` at the main checkout. A task agent's `sed -i '/assert add(1, 2) == 3/d' W/tests/test_calc.py`, and a python heredoc that opens `W/tests/test_calc.py` by absolute path and drops a test, are each caught through `run_bash` by `post-bash-check` and by `done-verify`, naming `tests/test_calc.py` and the kind. | real worktree, both hooks, real sed/python | `tests/test_guard_postcheck.py` |
| TC-75 | 2.1, NFR same safety | integration | Commands that move HEAD are still compared: (a) `sed -i '/assert add(1, 2) == 3/d' tests/test_calc.py && git commit -qam wip` is caught (removes 1 assertion) although the file is clean afterwards; (b) after a commit on `feature` that adds an assertion to `tests/test_calc.py`, `git checkout main -- tests/test_calc.py && git commit -qm x` is caught; (c) `git commit -qam y` after a non-test edit reports nothing. | real git, both hooks | `tests/test_guard_postcheck.py` |
| TC-76 | 2.1, 2.5, 2.10, 1.3 | integration | `find tests -name test_calc.py -exec sed -i '/assert/d' {} +` passes the pre-check and is caught by the post-check; `find tests -name test_calc.py -delete` passes the pre-check (no destroy target) and is caught as `deletes the test file`; `find tests -name '*.pyc' -delete` passes and reports nothing; `may_write` is True for every `find` with `-exec`, `-execdir`, `-ok`, `-delete` or `-fprint`. | real find, both hooks | `tests/test_guard_postcheck.py` |
| TC-77 | 2.1, 2.7, 2.8 | integration | (a) Eight pre/post hook pairs run concurrently in one checkout (a thread pool of subprocesses, one `session_id`, eight `agent_id`s and `tool_use_id`s), each a `sed -i` naming a different existing test file committed on `main`. Every weakening is caught exactly once, no snapshot file is left behind, and no pair reports another pair's file (each snapshot covers only the paths its command names, D20). (b) Given two claimed task worktrees `W1`, `W2` and hook `cwd` at the main checkout, a task agent's `python -c` that builds the path of `W2/tests/test_calc.py` at run time (`os.path.join` of pieces, no `W2` path in the command text) and drops an assertion there is not reported by the post-check, and `ck gate` on `W2`'s task branch then fails with "No existing tests deleted or weakened" (added by D20: the post-check no longer covers claimed worktrees the command does not name). | real hooks as subprocesses, real git, real `ck gate` | `tests/test_guard_postcheck.py` |
| TC-82 | 2.1, 2.4 | integration | Given a claimed task worktree `W` whose branch is behind `main`, and a commit on `main` that removes `assert add(1, 2) == 3` from `tests/test_calc.py` (a legitimate change, as after a 7.11 answer). A task agent's `git -C W merge -q --no-edit main` through `run_bash` (via `post-bash-check` and `done-verify`) reports nothing; the same with the change on a branch `integ` and `git -C W merge -q --no-edit integ` reports nothing. The agent's next command `sed -i '/assert add(2, 2)/d' W/tests/test_calc.py` is caught (removes 1 assertion), compared from the merged content (D19). | real worktree, real merge, both hooks | `tests/test_guard_postcheck.py` |

### Stories 3-5: caller classification, deny for task agents, main-session spec edits (`tests/test_guard_callers.py`)

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-20 | 3.1, 4.1 | integration | The input is the recorded `agent-tool-subagent-pretooluse.json` (fields rewritten to this repo, `tool_input` replaced by a weakening Edit of `tests/test_calc.py`), with ck.json `guard.tests: ask`. The result is `deny`. | recorded real input, hook | `tests/test_guard_callers.py` |
| TC-21 | 3.2 | integration | The input has no `agent_id`, with the environment from `headless-env.json`, and separately with the environment `ck run --dry-run --json` reports for its child. The TC-20 edit is denied in both. | recorded env, `ck run` dry-run env, hook | `tests/test_guard_callers.py` |
| TC-22 | 3.3, 4.2 | integration | The recorded `main-session-pretooluse.json` with the same edit gives `ask` under `ask` and `deny` under `deny`. | recorded input, hook | `tests/test_guard_callers.py` |
| TC-23 | 3.4 | integration (+ live opt-in) | The test reads `capture-summary.json`. If `workflowAgentHasAgentId` is true, the recorded `workflow-agent-pretooluse.json` with the TC-20 edit (an absolute path into a claimed task worktree, hook `cwd` at the main checkout) gives `deny`, and the marker cases are skipped with that reason. If it is false: (a) the main session runs the real Bash call `ck guard workflow-begin calc` through the hook; then the recorded Workflow-agent input (same `session_id`, no `agent_id`, cwd at the main checkout, absolute target in the worktree) gives `deny`; (b) after `ck guard workflow-end`, the same input gives the configured mode (`ask`); (c) a marker in another session never applies. Live variant (`CK_LIVE_CLAUDE=1`, skipped otherwise): capture a real one-agent Workflow under `claude -p` with `record_hook.py` and assert `agent_id` presence equals the summary. | recorded and live Claude Code hook input, real hook | `tests/test_guard_callers.py` |
| TC-24 | 3.5 | integration | Inputs with `cwd` missing, `tool_input: null`, `agent_id: ""`/`null`/`42`, and unknown keys (`"foo": {...}`) give no exception and a decision consistent with P-11. A CLI run with stdin `{}` and with stdin `not json` exits 0. | hook, CLI subprocess | `tests/test_guard_callers.py` |
| TC-25 | 4.1 | integration | Given ck.json `guard: {tests: ask, requirements: ask, test_plan: ask}` and implementation started. A task agent's weakening Edit, MultiEdit and Write of `tests/test_calc.py`, its rewording of requirements.md, and its row deletion in test-plan.md are each `deny`. With the kind `off`, each is `None`. | hook, real tasks.md | `tests/test_guard_callers.py` |
| TC-26 | 4.3 | integration | Each TC-25 denial reason contains the repo-relative path and the block kind ("removes 1 assertion", "requirement", "test case"). | hook | `tests/test_guard_callers.py` |
| TC-27 | 5.1 | integration | Given implementation started (Task 2 In Progress). Each of these from the main session gives `None` with no ask: rewording a requirement, deleting the "(Decided ...)" line, `Write` with the whole requirements.md replaced, and rewording/deleting a test-plan.md row. | hook | `tests/test_guard_callers.py` |
| TC-28 | 5.2, 5.3 | integration | The same edits from a task agent are `deny`. An add-only Edit and an appending Write from a task agent give `None`. | hook | `tests/test_guard_callers.py` |
| TC-72 | 3.6, 4.2 | integration | Given a claimed task worktree `W`. The recorded `main-session-pretooluse.json` input (no `agent_id`, interactive env, no workflow marker), rewritten to make a weakening Edit of `W/tests/test_calc.py` and to run `rm W/tests/test_calc.py`, gives `ask` under `guard.tests: ask` and `deny` under `deny`, with hook `cwd` at the main checkout and again with `cwd = W`. | recorded input, real worktree, hook | `tests/test_guard_callers.py` |
| TC-74 | 4.1, 3.1 | integration | Given a claimed task worktree `W` and hook `cwd` at the main checkout. A task agent's Edit, MultiEdit and Write with absolute paths that weaken `W/tests/test_calc.py` are each `deny`, and the reason names `tests/test_calc.py` and the kind. | real worktree, hook | `tests/test_guard_callers.py` |
| TC-86 | 5.2, 5.4, 3.1, 3.2, 3.4 | integration | Given implementation started (Task 2 In Progress). A rewording of an existing requirement in requirements.md and a row deletion in test-plan.md, sent as a /spec:review revise agent would send them, are each `deny` under `guard.requirements: ask` / `guard.test_plan: ask` in every mode: the recorded `workflow-agent-pretooluse.json` input (with the marker active when `capture-summary.json` says it lacks `agent_id`), the recorded `agent-tool-subagent-pretooluse.json` input (agent mode), and no `agent_id` with the `ck run --dry-run --json` child env (handed off to `ck run`). Each reason says the change must not be applied and must be returned as a decision ("escalated", "decision"). An add-only edit from the same inputs gives `None`. | recorded real inputs, `ck run` dry-run env, hook | `tests/test_guard_callers.py` |
| TC-87 | 5.4 | integration (workflow) + static doc | Workflow mode: the real `plugin/workflows/spec-create.js` in review mode under the stub runtime, on a spec whose tasks.md has a task In Progress, with one confirmed finding whose fix rewords an existing requirement and one add-only finding. The revise prompt carries the frozen-spec rule; when the stub reviser returns the rewording under `escalated`, the result's `openQuestions` holds it with `source: 'revise'` and the proposed text in `options`, it is in neither `fixed` nor `notFixed`, and `result` is `Ready after decisions`; the add-only fix is applied. With every task Not Started, the rule is absent. Agent mode: `plugin/resources/without-workflow-tool.md` and its `vendor_files()` rendering give the /spec:review revise agent the same rule (not applied, returned as a decision for review.md "Needs Your Decision"), and `.claude/workflows/spec-create.js` equals its source. | real `spec-create.js`, stub runtime; real Markdown files | `tests/js/spec_review_frozen.test.mjs`, `tests/test_guard_agent_mode_docs.py` |
| TC-89 | 3.3, 3.4 | integration | Only when `capture-summary.json` says Workflow agents lack `agent_id` (skipped otherwise with that reason). After `ck guard workflow-begin calc` through the hook: (a) a no-`agent_id` call is a task agent (`deny`) and refreshes the marker; (b) a marker file whose `refreshedAt` is planted 16 minutes old gives the configured mode (`ask`), and one planted 14 minutes old still gives `deny`; (c) `execute_hook("marker-clear", <UserPromptSubmit input of the same session>)` returns no output and the next no-`agent_id` call gets the configured mode; (d) a second `ck guard workflow-begin calc` replaces a stale marker with a fresh one; (e) a marker of another session is never cleared or read. | real hooks, real data dir | `tests/test_guard_callers.py` |

### Stories 6-7: guard ladder, park, notify, both modes

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-29 | 6.1, 6.2, 6.6 | integration (workflow) | Given implementer `haiku`, escalate `[]`, effort `medium`, and `guardStops: {1: 'opus'}`. Then the attempts of task 1 run on `haiku, sonnet, opus, fable`, all at effort `medium`. Each guard attempt's prompt names `.ck-handoff` / the previous attempt's handoff note. `result.tasks[0].escalations` is `[{stage:'guard',model:'sonnet'},{stage:'guard',model:'opus'},{stage:'guard',model:'fable'}]`. `result.escalated` lists the models. The task merges. | real `spec-implement.js`, stub runtime | `tests/js/guard_ladder.test.mjs` |
| TC-30 | 6.1 | integration (workflow) | Starting at `sonnet` with `guardStops: {1: 'sonnet'}`, the attempts are `sonnet, opus` and the run stops at opus. Starting at `opus`, the attempts are `opus, fable`. A risky task starting at `opus` never runs on `sonnet`. | stub runtime | `tests/js/guard_ladder.test.mjs` |
| TC-31 | 6.2 | integration (workflow) | With escalate `['opus']` and `guardStops: {1: 'sonnet'}` from `haiku`, the guard attempts are `sonnet, opus`. `sonnet` is used although it is not in `escalate`. | stub runtime | `tests/js/guard_ladder.test.mjs` |
| TC-32 | 6.3 | integration (workflow) | A plain failure (`failUntil`) with escalate `['sonnet','opus']` gives exactly the existing escalation sequence: the existing escalation tests stay green, and no `stage:'guard'` entry appears. A red landing and blocking review findings never trigger the guard ladder. | stub runtime | `tests/js/guard_ladder.test.mjs` |
| TC-33 | 6.4, 7.1 | integration (workflow) | Given a task starting on `fable` with `guardStops: {1:'always'}`, there is exactly one task attempt, then one `park 1` agent whose prompt contains `ck worktree park demo 1` with the quoted test and requirement. | stub runtime | `tests/js/guard_ladder.test.mjs` |
| TC-34 | 7.1, 7.5 | integration (workflow) | Given deps `{1:[], 2:['1'], 3:[]}`, `guardStops: {1:'always'}` and an unattended run. Then task 2 never starts, task 3 merges, and `result.needsInput` is `[{task:'1', test, requirement, decision}]`. Task 1 is in neither `merged`, `failed` nor `blockedOn`, and task 2 appears as blocked on 1. The `outcome` is not `complete`. | stub runtime | `tests/js/guard_ladder.test.mjs` |
| TC-35 | 7.3, 7.4 | integration (workflow) | With `parkOutcome: {parked: true, notification: 'not_configured', detail: <hint>}`, `result.notifications.notConfigured` is true and `hint` names `ck agents set run.notify_command`. With `{parked: true, notification: 'failed', detail: 'exit 3'}`, `notifications.failed[0].detail == 'exit 3'`. The run continues in both. | stub runtime | `tests/js/guard_ladder.test.mjs` |
| TC-36 | 7.6 | integration (workflow) | With `args.interactive: true` and `guardStops: {1:'always'}`, no `park` agent runs. `result.needsInput[0].action === 'ask'` with the quoted test and requirement. The implement skill (plugin + vendored) tells the session to ask with `AskUserQuestion` quoting both for each `ask` entry. | stub runtime; real skill files | `tests/js/guard_ladder.test.mjs`, `tests/test_guard_agent_mode_docs.py` |
| TC-79 | 6.1, 6.3 | integration (workflow) | With `implementer.escalate ['sonnet','opus']`, retries enabled, a `haiku` start and `guardStops: {1: 'sonnet'}`, the guard stop on attempt 1 produces no `retry` labels and no `stage:'retry'` escalations before the ladder; only `task 1 guard <k> (<model>)` attempts follow (sonnet, then opus), and the task merges. With `guardStopIn: {1: 'revise'}`, a guard stop in the first revise call reaches the ladder (and, with `'always'`, a `park 1` agent) instead of returning failed. | stub runtime | `tests/js/guard_ladder.test.mjs` |
| TC-80 | 7.9, 7.10, 7.11 | integration (workflow) | The task, retry, revise and guard prompts of every task each tell the agent, before anything else, to read `<WT>.decision.md` if it exists and follow it. With `args.interactive: true`, the implement skill (plugin + vendored) records each AskUserQuestion answer with `ck worktree resolve <spec> <N>`, tells me to change the test or requirement in the main session when that is my answer and waits for me to confirm the change before it re-runs once (7.11). | stub runtime; real skill files | `tests/js/guard_ladder.test.mjs`, `tests/test_guard_agent_mode_docs.py` |
| TC-37 | 7.1, 7.2, 7.5 | integration (CLI) | Given a real repo with `spec_repo` and `set_notify(notify_script ok)`. When `ck worktree park demo 2 --json` runs with stdin `{"file", "kind", "test": "<quoted test>", "requirement": "<quoted req>", "decision": "<q>"}`, then exit 0, `parked: true`, `notification: "sent"`, and the notify output has exactly one stdin JSON whose `message` names `demo`, task `2` and "decision", and contains neither quoted text. The park note is mode 0600. `ck worktree report demo --json` then has `needsInput: [{task:"2", file, kind, test, requirement, decision, parkedAt}]`, and task 2 is in no other list. The text report prints a `needs input` line. | real CLI, real git, real subprocess notify | `tests/test_guard_park.py` |
| TC-38 | 7.3 | integration (CLI) | With no `run.notify_command` configured, park exits 0, gives `parked: true, notification: "not_configured"`, and `detail` says how to configure it (`ck agents set run.notify_command`). | CLI | `tests/test_guard_park.py` |
| TC-39 | 7.4, NFR never blocks | integration (CLI) | With notify `fail` (exit 3), `slow` (sleeps 30 s) and `missing`, park exits 0 within 15 s, gives `parked: true, notification: "failed"`, `detail` names the failure (`exit 3` / `timed out after 10 s` / not found), and the report still lists the task. | CLI, real subprocess timeout | `tests/test_guard_park.py` |
| TC-88 | 7.12, 7.2 | integration (CLI) | Given `spec_repo` with `demo`, `set_notify(notify_script ok)`, `fake_sandbox_tools` and `parking_fake_claude(..., "demo", "2")` on `PATH`. When `ck run demo` runs (sandboxed settings), the park inside it answers `notification: "deferred"`, and after `ck run` exits the notify output holds exactly one message, naming `demo`, task `2` and "decision", sent by `ck run`'s own process (the run log has one `ck run: notification sent` line). A second `ck run` whose fake claude parks task 3 while task 2's note is still there gives exactly one more message (for task 3). `ck run demo --dry-run --json` settings (sandbox filesystem and network allowances) are identical with and without `run.notify_command` configured: the sandbox is not widened. | real `ck run`, real CLI, fake `claude`, real subprocess notify | `tests/test_guard_park.py` |
| TC-40 | 7.8 | integration (CLI) | Given task 2 parked with its worktree created by `ck worktree create demo 2`. Then `ck plan demo --json` lists 2 as ready (deps Done), and a second `ck worktree create demo 2 --json` returns `EXISTS` (the worktree is reused). | real `ck plan`, `ck worktree create` | `tests/test_guard_park.py` |
| TC-41 | 6.1, 6.5 (agent mode) | integration (CLI) | `ck agents --guard-ladder <m> --json` prints `["sonnet","opus","fable"]` for `haiku`, `["opus","fable"]` for `sonnet` and `claude-sonnet-5-5`, `["fable"]` for `opus`, and `[]` for `fable`, whatever `implementer.escalate` is set to in global prefs. | real CLI | `tests/test_guard_park.py` |
| TC-78 | 7.9, 7.11 | integration (CLI) | After `ck worktree park demo 2`, `ck worktree resolve demo 2 --json` with stdin `{"answer": "the code must satisfy the test"}` exits 0, prints `resolved: true`, `hadParkNote: true` and the decision-note path; the note is mode 0600 and holds the answer verbatim; the park note is gone and `ck worktree report demo --json` no longer lists task 2 under `needsInput`. A second resolve appends a second answer. A later `ck worktree park demo 2` writes `priorDecision: true`. Bad stdin exits 3. | real CLI, real git | `tests/test_guard_park.py` |
| TC-42 | 6.5, 7.7, 7.9, 7.11 | static doc | `plugin/resources/without-workflow-tool.md` and `/spec:spawn-worktree` (plugin), and their `vendor_files()` renderings, each contain: a task result with `guardStop` starts the guard ladder; models come from `ck agents --guard-ladder <model> --json`, independent of `escalate`; one attempt per model, same agent type and effort, reading the handoff note and the decision note; agent description `[ck:<run_id>] implement task <N> guard <k>`; when unresolved and unattended, `ck worktree park ...` with the quoted test and requirement, dependents never start, independents continue; when interactive, `AskUserQuestion` quoting both, then `ck worktree resolve`, and, when my answer is that the test or requirement is wrong, telling me to change it from my main session before the task's next attempt (7.11); the final report lists needs-input tasks apart from merged, failed and blocked tasks, names the escalation models, names a notification failure or missing config, and says how to resolve a parked task. | real Markdown files, `vendor_files()` | `tests/test_guard_agent_mode_docs.py` |
| TC-43 | 6.6, 10.1 (agent mode) | integration (CLI) | `parse_agent_description("[ck:<id>] implement task 3 guard 1", "spec:implementer")` gives role `implementer`, task `3` and `step == "guard"`. The Workflow label `park 3` maps to role `orchestrator`. Every template in TC-42 renders and parses (existing TC-10 pattern in `test_stats_agents_docs.py`). | real seam + real doc templates | `tests/test_guard_agent_mode_docs.py` |

### Story 8: hand-off (`tests/test_handoff.py`)

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-44 | 8.1, 8.2 | integration (CLI) | Given the `repo()` from `test_run.py` with `demo`, a linked worktree `<root>/.claude/worktrees/bg-1`, `sleeping_fake_claude` and `fake_sandbox_tools` on the `PATH` the child inherits. When `ck handoff implement demo --json` runs in the worktree, then exit 0, `handoff: true`, `launched: true` and `mainCheckout == <root>`. Polling the record file (bounded wait), fake claude ran with cwd `<root>` and a prompt that is `/spec:implement demo all` and contains no `--log`. The printed `log` path exists under `<root>/.claude/ck-runs/demo-*.log` and is the one `ck run` writes. The output names a follow command (`tail -f <log>`) and how to find the report (`ck worktree report demo` / `ck stats show`). | real git worktree, real CLI, fake `claude`, fake sandbox tools | `tests/test_handoff.py` |
| TC-45 | 8.1, 8.3, D11 | integration (CLI) | `ck handoff create demo` and `ck handoff review demo` from the worktree launch `/spec:create demo` and `/spec:review demo`. `ck run demo --command create --dry-run --json` and `--command review` have argv identical to `--command implement` except the prompt (`--permission-mode auto`, `--permission-prompts none`, sandbox settings, budget and turn caps, `--allowedTools` with `Workflow`). The env is the same (with `CK_RUN_HEADLESS=1`), and the refusals (`unknown spec`, linked worktree, sandbox tools missing) apply the same way. | real CLI dry-run | `tests/test_handoff.py` |
| TC-46 | 8.4 | integration (CLI) | Each case exits 3 with `launched: false`, prints the reason (`error`) and the exact manual command (`manualCommand`: `cd <root> && ck run demo --command implement ...`), and fake claude never ran in the worktree: (a) sandbox tools missing; (b) `ck run` refuses (unknown spec); (c) the main checkout cannot be found (the worktree's `.git` admin dir under `<root>/.git/worktrees/bg-1` removed); (d) a fake `claude` that exits 1 at once (the child exits within 3 s). | real CLI, real git | `tests/test_handoff.py` |
| TC-47 | 8.5 | integration (CLI) | Each case gives `handoff: false`, exit 0 and no launch: the main checkout root; a subdirectory of it; a worktree under `<root>/.ck/worktrees/`; a worktree outside the project; and `ck handoff implement demo 3` (single task) from `.claude/worktrees/bg-1`. | real CLI, real git | `tests/test_handoff.py` |
| TC-48 | 8.6 | integration (CLI) | `ck handoff implement demo --escalate sonnet,opus --max-concurrent 6` from the worktree gives the prompt `/spec:implement demo all --escalate sonnet,opus --max-concurrent 6`, and the launched argv has `--command implement --log <log>` before `--`. An argument with a space and quotes survives `shlex.split`. | real CLI | `tests/test_handoff.py` |
| TC-83 | 8.7 | integration (CLI) | Given `repo()` with a linked worktree `<root>/.claude/worktrees/bg-1` on its own branch, where a commit adds `specs/newspec/` (requirements.md, design.md, test-plan.md, tasks.md) that the main checkout's HEAD does not have, a clean main checkout on `main`, `sleeping_fake_claude` and `fake_sandbox_tools`. When `ck handoff implement newspec --json` runs in the worktree, then exit 0, `launched: true`, `runBranch: "newspec-run"`; in `<root>`, `git branch --show-current` is `newspec-run` and its HEAD equals the worktree's HEAD; fake claude ran with cwd `<root>` and prompt `/spec:implement newspec all`. The same holds when the spec exists in both but a file differs. | real git worktree, real `git switch`, real CLI, fake `claude` | `tests/test_handoff.py` |
| TC-84 | 8.8 | integration (CLI) | The TC-83 setup, in each case: (a) an uncommitted change to a tracked file in `<root>`; (b) an uncommitted edit under `specs/newspec/` in the worktree; (c) a `newspec-run` branch already in `<root>` at another commit. Each exits 3 with `launched: false`, an `error` naming the cause, and `manualCommand` containing `git switch -c newspec-run <worktree head sha>` and `ck run newspec --command implement`. `<root>`'s branch, HEAD and `git status --porcelain` are unchanged, and fake claude never ran. | real git, real CLI | `tests/test_handoff.py` |
| TC-85 | 8.9, 8.1 | integration (CLI) | The worktree's branch has commits outside `specs/demo/` only (and, separately, sits at the main checkout's HEAD). `ck handoff implement demo --json` exits 0 with `launched: true` and `runBranch: null`, and `<root>` stays on `main` at the same HEAD. | real git, real CLI | `tests/test_handoff.py` |
| TC-49 | 8.1, 8.5, 3.4 | static doc | `plugin/skills/{implement,create,review}/SKILL.md` and `.claude/commands/spec/{implement,create,review}.md` each run `ck handoff <kind> <spec> <args>` as the first step of the whole-spec path, before the Workflow/agent-mode choice. They stop and relay the output on `handoff: true`, stop with the printed manual command on failure, and continue on `handoff: false`. The single-task path of implement has no hand-off step. When `capture-summary.json` says Workflow agents lack `agent_id` (3.4), each skill also runs `ck guard workflow-begin <spec>` right before the Workflow call and `ck guard workflow-end` right after it; otherwise this check is skipped with that reason. | real Markdown files | `tests/test_guard_agent_mode_docs.py` |

### Story 9: guard event log (`tests/test_guard_log.py`)

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-50 | 9.1 | integration | Three calls each append exactly one event, after which `guard_events()` has 3 events: a main-session ask (`rm tests/test_calc.py`), a task deny (with `agent_id`/`agent_type`), and a post-check catch. Each event has `"v": 1`, `ts` (ISO UTC), `sessionId`, `agentId`/`agentType` (task only), `caller`, `tool`, `decision` ∈ {ask, deny, post-check}, `category`, `reason` (= the hook's reason text), `path: "tests/test_calc.py"` (repo-relative), `command` (Bash only), `family`, and `toolUseId`. | real hooks, real data dir | `tests/test_guard_log.py` |
| TC-51 | 9.2 | integration | 20 passing calls write no event and no log file: non-test edits, `sed -i` on tests, an add-only requirements edit, and a no-weakening post-check. | hooks, fs | `tests/test_guard_log.py` |
| TC-52 | 9.3, NFR privacy | integration | The log lives under `data_dir()` (`CK_DATA_HOME` / `XDG_DATA_HOME`), never under the project. The file mode is `0600` and its directory `0700` when ck created them. No file appears under `project/` (`git status --porcelain --ignored` unchanged). | real fs | `tests/test_guard_log.py` |
| TC-53 | 9.4 | integration | A log planted just under `guardlog.GUARD_LOG_LIMIT` bytes, then two more denials, rotates once: `guard-events.1.jsonl` holds the old content, the total size of both files stays ≤ 2 × `GUARD_LOG_LIMIT` plus one event, and the newest event is in `guard-events.jsonl`. | real fs | `tests/test_guard_log.py` |
| TC-54 | 9.5, NFR never blocks | integration | With `CK_DATA_HOME` pointing at a regular file (unwritable dir), each TC-50 call returns the same decision dict as with a writable dir. The CLI run's stdout is identical and exit is 0. | hooks, CLI subprocess, real fs | `tests/test_guard_log.py` |
| TC-55 | 9.6, NFR privacy | integration (CLI) | Plant guard-log events whose `command`, `path` and `reason` hold invented confidential values (`acme-ledger-internal`, `tests/test_payroll_acme.py`, "quarterly payroll ..."). An export whose note contains any of them is refused with a leak hit. The same export without them passes. `ck stats submit` refuses the same way. | real CLI, real store | `tests/test_guard_log.py` |

### Stories 10-11: interruption counts, shared records, show (`tests/test_stats_interruptions.py`)

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-56 | 10.1, 10.2 | integration (CLI) | Given a fake home with a `spec-implement` workflow run. Agent `task 1` (implementer) has: 2 ck guard asks on Bash `sed -i` (approved 90 s and 30 s later), 1 guard deny on `rm`, 1 post-check catch on a python heredoc (a `hook_blocking_error` whose gate text comes first), 1 auto-mode denial on `git push`, 1 isolation refusal, and 1 near-miss gate-only `hook_blocking_error`. Agent `review 1` has 1 headless denial. The orchestrator transcript has 1 user rejection on an Edit. When `ck stats collect` runs, then the local `interruptions.totals` equals: prompts 2, waitMs 120000, guardDenials 1, postCheckCatches 1, autoModeDenials 1, headlessDenials 1, userRejects 1, isolationRefusals 1 (the near-miss counts nothing), and `byRole[role][family][kind]` (implementer/reviewer/orchestrator × sed/rm/python/git/other) and `waitMsByRole` match. | real CLI, fake Claude home | `tests/test_stats_interruptions.py` |
| TC-57 | 10.1, 6.6 | integration (CLI) | A run with agent rows `task 1 guard 1 (opus)` and `task 1 guard 2 (fable)` gives `totals.guardEscalations: 2` and `escalationModels: {opus: 1, fable: 1}`. The `park 1` agent row is matched as role `orchestrator`. | real CLI | `tests/test_stats_interruptions.py` |
| TC-58 | 10.1, D11 | integration (CLI) | A `spec-create` run (and a review run) with a requirements.md guard ask in the orchestrator and an isolation refusal in a writer agent are counted with the same keys and roles (`orchestrator`, writer role per existing mapping). | real CLI | `tests/test_stats_interruptions.py` |
| TC-59 | 10.2 | integration (CLI) | Non-shell interruptions (Edit deny, Write ask) count under family `other`. A `cd W && sed -i ...` counts under `sed`, `uv run python x.py` under `python`, `echo > f` under `redirect`. | real CLI | `tests/test_stats_interruptions.py` |
| TC-60 | 10.3 | integration (CLI) | An ask at T and the same `tool_use_id`'s result at T+150 s adds 150000 to `waitMs` (2.5 min). An ask with no result line adds 0. A result timestamped before the ask adds 0. | real CLI | `tests/test_stats_interruptions.py` |
| TC-61 | 10.4 | integration (CLI) | With `write_guard_log` events for the run's sessions that agree, the local record has `guardLog.state: "ok"`, `guardLog.events` equal to their count, and no `differences`. With one extra deny in the log inside the run window, the transcript count is kept and `guardLog.differences.guardDenials == {"log": 2, "transcripts": 1}`. | real CLI, real data dir | `tests/test_stats_interruptions.py` |
| TC-62 | 10.5 | integration (CLI) | (a) With no guard log ever created, the counts are recorded, `guardLog` is `{"state": "ok", "events": 0}` and `missing` has no `guard_log` entry (changed by D17: it used to expect `missing`). (b) With the log path made unreadable (`chmod 000`, skipped as root), the counts are still recorded, `guardLog.state` is `unreadable`, and `missing` holds `{"what": "guard_log"}`. (c) With a `guard-events.1.jsonl` and a current log whose oldest event is newer than the run start, `guardLog.state` is `rotated` and `missing` holds `{"what": "guard_log"}`; with the oldest event older than the run start, the state is `ok`. | real CLI | `tests/test_stats_interruptions.py` |
| TC-63 | 10.6 | integration (CLI) | Malformed lines (`{not json`, `{}`, a half line at EOF, unknown `type`, attachment with `stdout: "not json"`) inserted among TC-56's lines give the same counts. | real CLI | `tests/test_stats_interruptions.py` |
| TC-81 | 10.4 | integration (CLI) | One session holds two runs (a create run, then an implement run). Guard-log events of the create run (inside its own window) and events of another agent outside the implement run's agents never enter the implement run's `guardLog.events` or `differences`. | real CLI, real data dir | `tests/test_stats_interruptions.py` |
| TC-64 | 10.7, 11.4 | integration (CLI) | A record from `seeded_local_record("implement")` (built before this feature, no `interruptions`) gives the `ck stats show` line `interruptions: not recorded`. Its shared record has no `interruptions` key, and the aggregate view does not add it as 0. | real CLI, real store | `tests/test_stats_interruptions.py` |
| TC-65 | 10.8, D12 | integration (CLI) | The same interruptions are written into an agent-mode run: `ck stats begin` plus Agent-tool subagents described `[ck:<id>] implement task 1`, `... review task 1` and `... implement task 1 guard 1` (run on fable, the design's `guard <k>` template). `ck stats collect --agents`, `--all` and auto-collect (`ck run` path, `--auto`) each give a record whose `interruptions` equals TC-56's workflow record (plus the guard escalation to fable). | real CLI, fake home | `tests/test_stats_interruptions.py` |
| TC-66 | 11.1, 11.2 | integration (CLI) | `ck stats export --dry-run` (shareable JSON) for TC-56's run has `schema: "ck-run-analytics/1"` and an `interruptions` block with `prompts`, `guard_denials`, `post_check_catches`, `guard_escalations`, `auto_mode_denials`, `headless_denials`, `user_rejects`, `isolation_refusals`, `wait_minutes`, `escalation_models`, `by_role`, `wait_minutes_by_role`: ints, floats and enum keys only. `validate_shareable` passes. `all_bytes` of the export contains no command text (`sed -i`, `git push`), no path (`tests/test_calc.py`), no spec name and no reason text. | real CLI | `tests/test_stats_interruptions.py` |
| TC-67 | 11.2 | integration | `analytics/SCHEMA.md` documents every new shared path (`assert_documented`). `SHAREABLE_SCHEMA` keeps every existing path unchanged (the set of old paths ⊆ new). | real files | `tests/test_stats_interruptions.py` |
| TC-68 | 11.3 | integration (CLI) | A record with prompts 0, waitMs 0, guardDenials 3, guardEscalations 1 (fable) and no refusals prints exactly `interruptions: 0 prompts (0 min waiting) · 3 guard denials → 1 escalation (fable) · 0 refusals` in `ck stats show`, once per run. Nonzero post-check, auto-mode, headless and rejection counts also appear on that one line. | real CLI | `tests/test_stats_interruptions.py` |
| TC-69 | 11.5 | integration | Extract release `0.10.2` (commit `6380867`) with `git archive` into a temp dir. Running `python -m claude_kiro.cli stats show` with `PYTHONPATH` set to it, on a store holding a new record with `interruptions`, exits 0 and lists the run. Skipped when the commit is absent (shallow clone). | real older ck code, real store | `tests/test_stats_interruptions.py` |
| TC-70 | NFR no network | integration | Under `no_network`, the guard (TC-1 calls), the post-check (TC-9), the guard log and `ck stats collect` (TC-56) make no network call (net log empty). `ck worktree park` with no notify config makes none. | `no_network` fixture, CLI subprocesses | `tests/test_stats_interruptions.py` |

## Properties

Library: **Hypothesis** (Python). The JS-side seams are reached through the Node bridge.
- Pure seams: `@settings(derandomize=True, max_examples=200, database=None)`.
- Real repo, hook or CLI: `@settings(derandomize=True, max_examples=40, database=None, deadline=None)`, with
  module-scoped repos.

Each file declares `PROPERTIES = {...}`.

| ID | Requirement | Property | Generator domain | Expected decision |
|---|---|---|---|---|
| P-1 | 1.1, 1.4, 4.1, 4.2 | For any shell command that deletes or moves an existing, unowned test file, the pre-check blocks it: deny for a task agent, the configured `guard.tests` mode for the main session. The file is unchanged. | Verbs: `rm`, `rm -f`, `rm -rf`, `rm --`, `unlink`, `truncate -s 0`, `mv <test> <dst>`, `mv <dst-dir>` with test as source, `git rm`, `git rm -q --cached`, `git mv`, `git -C <dir> rm`. Targets: the file or a directory containing it (`tests`, `tests/`, `./tests`; expanded to its base-ref files, design S7), and the file's absolute path inside a claimed task worktree with hook cwd at the main checkout. Path forms: relative, `./`, absolute, `tests/../tests/x`, single/double quoted, path with a space (`tests/test calc.py` committed), the test as the 2nd of several args. Prefixes: none, `cd D &&`, `cd D;`, `cd "D" &&`, chained after `pytest -q &&`, `true ||`, `x;`, or a newline. Caller ∈ {main, agent_id, headless env}; mode ∈ {ask, deny}. | task → `deny`; main → mode |
| P-2 | 1.2, 1.6, NFR zero prompts | For any in-place, script or redirect write to any test file (existing, new or owned), the pre-check returns no decision. | `sed -i`, `sed -i.bak`, `sed -i ''`, `sed --in-place=x`, `perl -pi -e`, `perl -i -pe`, `python - <<'EOF' ... EOF`, `python3 -c "open(...,'w')"`, `uv run python s.py`, `> f`, `>> f`, `cat > f <<EOF`, `tee f`, `tee -a f`, `cp x f`, `printf .. > f`, `xargs sed -i`, `find tests -name '*.py' -exec sed -i ... {} +`. Each with and without `cd D &&`, for both callers and both modes. | `None` (never ask, never deny) |
| P-3 | 1.3 | Complement of P-1: for any delete or move whose targets contain no existing unowned test file, the pre-check returns no decision. | P-1's verbs and forms over near-miss targets from the same repo: `tests/test_scratch.py` (committed on branch only), `tests/test_subtract.py` (owned, Task 1 In Progress), `tests/__pycache__/*.pyc` and the directories `tests/__pycache__`, `tests/sub/__pycache__` and `tests/.pytest_cache` (expanding to no base-ref test file), untracked `tests/tmp_x.py`, `calc.py`, `build/`, `specs/calc/notes.md`. | `None` |
| P-4 | 1.4 | Metamorphic: a `cd` prefix is resolved. For any command C and relative target r, `cd D && C r`, `cd D; C r` and `C D/r` (D absolute or relative to the hook cwd) get the same decision, including when D is a linked worktree whose base differs from the hook cwd's branch. | D ∈ {`.`, `tests`, absolute project path, linked worktree path, a claimed task worktree path with the hook cwd at the main checkout, worktree path with a space}; C, r from P-1 ∪ P-3 | equal decisions |
| P-5 | 1.6 | For any Bash command, with `guard.tests: off`, neither hook produces output and no guard event is written. | P-1 ∪ P-2 ∪ weakening commands from P-7 | `None` / no output |
| P-6 | 1.7 | For any command, when the base branch cannot be determined, the pre-check never asks, for either caller. | Repo states: no `.git`; ck.json `base` missing ref; unborn HEAD; garbage `.git/HEAD`; `git` wrapper failing. Commands from P-1. | `None` |
| P-7 | 2.1, 2.4, NFR same safety | Edit/Bash parity: for any (old, new) content of an existing unowned test file, with `new` different from the file's content on the base ref (D19 exempts that case; narrowed by that decision), the post-check reports weakening iff the edit path (`Write` with `new`) would block, and the kinds it names equal `weakening(old, new)`'s kinds. | Bodies built from pytest, jest/vitest, go and rspec test defs. Assertions from every `ASSERTION` form. Skip markers from every `SKIP_MARKER` form. Mutations: remove assertion, remove test, add each skip/only form, rename, add test, reorder, comment out (still counted). Applied via `sed -i`, a python rewrite, `>` redirect, `perl -pi` and `find ... -exec sed -i`, on a file in the project and on a file in a claimed task worktree addressed by absolute path from the main-checkout cwd, with and without a trailing `&& git commit -qam x`. | parity (report ⇔ block) |
| P-8 | 2.4, 2.6 | For any test file that is new on the branch, owned, or already changed before the command and left untouched by it, the post-check never reports it, however weakened it is. Metamorphic: appending a command that does not touch a reported file never reports that file again. | P-7's mutations applied to `tests/test_scratch.py` (branch-only), `tests/test_subtract.py` (owned), and `tests/test_calc.py` pre-weakened, followed by commands from P-9's read-only half or writes to other files | no report for those files |
| P-9 | 2.5 | Pre-filter safety: for any command, if the pre-filter `may_write(cmd)` says it cannot write, then actually running it changes no test file (`git status --porcelain tests` before == after, and file hashes equal). Complement: every write form is classified may-write. | Read-only: `ls`, `cat`, `grep`, `git status/diff/log/show`, `git branch`, `git branch -a`, `find tests -name '*.py'`, `head`, `wc`, `python -m pytest -q --co` (pycache only). Writes: every P-2 form plus `mv`, `cp`, `dd of=`, `install`, `git checkout -- f`, `git restore f`, `patch -p1 < d`, `ed`, `awk -i inplace`, `npm run x`, an unknown program `./tool`, `find tests -delete`, `find tests -name x -exec rm {} +`, `find ... -execdir`, `find ... -ok`, `find ... -fprint f`, `git branch -D x`, `git branch -m a b`. Each with and without `cd` and env-var prefixes (`FOO=1 cmd`). | skip ⇒ no change; write form ⇒ `may_write` true |
| P-10 | 2.7, 9.5, NFR never blocks | For any guard input and any forced failure, the PreToolUse decision equals the decision without the failure, and the post-check has no output and exit 0. | Inputs: P-1 ∪ P-2 ∪ TC-25 edits. Failures: unwritable data dir, unreadable test file, failing/sleeping `git` (post only), read-only project dir. | same decision; post `None` |
| P-11 | 3.1, 3.2, 3.3, 3.4, 3.5, 3.6 | For any hook input, environment and marker state, `guard_policy.classify_caller(input, env, workflow_marker)` never raises, and returns `task_agent` iff `agent_id` is a non-empty string, or `is_headless(env)`, or `agent_id` is unusable and `workflow_marker` is True; else `main_session`. Metamorphic: adding unknown keys, removing undocumented ones, or changing `cwd` and the tool's paths (main checkout ↔ task worktree) never changes the result. | Dicts over the documented keys, each present/absent/`None`/wrong type (int, list, dict). `agent_id` ∈ {absent, `None`, `""`, `"  "`, `"a1b2"`, `42`}. Unknown keys with arbitrary JSON. Env with/without `CK_RUN_HEADLESS=1` and `CLAUDE_CODE_ENTRYPOINT=sdk-*`, and near-miss values (`CLAUDE_CODE_ENTRYPOINT=cli`, `CK_RUN_HEADLESS=0`). `workflow_marker` ∈ {True, False}. The recorded fixtures as seeds (`@example`). | `task_agent` / `main_session` as stated; no exception |
| P-12 | 4.1, 4.2, 5.1, 5.2 | For any guarded call the guard blocks, a task agent is never asked: it gets `deny` whatever the configured mode, and `None` only when that guard kind is `off`. The main session gets the configured mode for tests and passes for requirements.md / test-plan.md. | Calls: weakening Edit/MultiEdit/Write of an existing test, P-1 deletions, rewording/deleting requirements.md and test-plan.md text after implementation started. Mode per kind ∈ {ask, deny, off, absent (default)}. Caller ∈ {agent_id, headless, main}. | task: `deny` (or `None` if off), never `ask`; main: mode (tests), `None` (spec files) |
| P-13 | 1.5, 2.2, 4.3 | For any task-agent denial or post-check catch, the reason contains the repo-relative path, the block kind, and the keep-the-test / report-the-conflict instruction. | Calls from P-12 and catches from P-7, over paths with spaces and nested dirs | reason contains all three |
| P-14 | 6.1, 6.2, 6.4, 6.5 | For any starting model, `agents.guard_ladder` (the source, S20) is the models of (sonnet, opus, fable) strictly above it, in that order, independent of `implementer.escalate`. The JS mirror `guardLadder` and `ck agents --guard-ladder` agree with it for every alias. | Models: `haiku`, `sonnet`, `opus`, `fable`, `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-fable-5-5`, `claude-opus-5-5[1m]`, upper-case variants, `None`, an unknown name. `escalate` ∈ {`[]`, `['opus']`, `['sonnet','opus']`, `['fable']`, `['haiku']`}. | the strict-above list; fable → `[]`; unknown/None → all three |
| P-15 | 6.1, 6.2, 6.3, 6.5 | For any scenario, each task's attempts follow these rules: ordinary failures get the normal retries and the `escalate` models; a guard stop (in a task, retry, revise or guard call) is followed at once by ladder attempts, each ladder model at most once, at the role's effort, from the model that stopped, ending at the first attempt without a guard stop, with no retry or `escalate` attempt spent on the guard stop; after that, ordinary rules apply again. No guard attempt happens without a guard stop. | Bridge `run` over `scenarios()` DAGs (1-6 tasks) with per-task `guardStops` ∈ {none, haiku, sonnet, opus, 'always'}, `guardStopIn` ∈ {task, revise}, `failUntil` 0-2, `escalate` lists from P-14, start models haiku/sonnet/opus. | sequence as stated |
| P-16 | 7.1, 7.5 | For any DAG and any set of tasks unresolved through fable in an unattended run: no task that (transitively) depends on a parked task starts; every task that depends on none starts; each parked task is in `needsInput` exactly once and in none of `merged`/`failed`/`blockedOn`; `park` runs once per parked task. | Bridge `run` over `dags()` with a random subset given `guardStops: 'always'` | as stated |
| P-17 | 7.2 | For any spec name, task number, quoted test and quoted requirement, the notification names the spec, the task and that a decision is needed, and contains no line or 12+ character substring of the quoted texts. The texts are never interpreted by a shell. | Spec names: kebab slugs, including one that is a substring of the test text. Quoted texts: multi-line, unicode, `$(touch PWNED)`, backticks, `'`/`"`, `; rm -rf x`, 1-2,000 chars. | message clean; no `PWNED` file |
| P-18 | 7.3, 7.4 | For any notify outcome, `ck worktree park` exits 0 within 15 s and records the park. `notification` is `"sent"` only for exit 0. The report names the failure or the missing config. | Outcomes: not configured; exit 0; exit 1-255; sleep 30 (timeout); missing executable; garbage on stderr; huge stdout | exit 0, parked; `sent` ⇔ exit 0 within 10 s |
| P-19 | 8.1, 8.5 | For any session cwd, kind and args, the hand-off fires iff the cwd is inside a linked worktree under the main checkout's `.claude/worktrees/` and the command is not single-task. It never fires from the main checkout, so a handed-off run cannot hand off again. | cwd ∈ {main root, main subdir, `.claude/worktrees/x` root and subdir, `.ck/worktrees/x`, worktree outside project, non-git dir}. Kind ∈ {implement, create, review}. Args ∈ {`[]`, `[N]`, `["all"]`, `["--escalate","opus"]`, `[N, "--x"]`}, with `N` only meaning single-task for implement. Via `handoff.decide_handoff(HandoffFacts(...))` (pure); a module-scoped sample through the real `ck handoff ... --json` with real worktrees in TC-47. | `handoff` true/false as stated |
| P-20 | 8.6 | For any argument list, the handed-off prompt is `/spec:<kind> <spec> <args>`, and `shlex.split` of the args part returns the list exactly (`all` added for implement as `ck run` does). | Args: words with spaces, quotes, `$`, `=`, unicode, empty strings, `--` | round-trip equal |
| P-21 | 9.1, 9.2 | For any guard call, exactly one event is appended iff the decision is ask/deny or the post-check catches. A passing call appends none. Each event has every 9.1 field. | Calls from P-1, P-2, P-3, P-7, P-12 | event count = blocking decisions |
| P-22 | 9.6, 11.1 | For any confidential value planted in the guard log (`command`, `path`, `reason`), the shareable record contains it nowhere, and an export or submit whose note contains it is refused. | Invented names (`acme-*`), paths with dirs and spaces, quoted test bodies, mixed case. Near-miss: ck vocabulary words (`sed`, `tests`, `implementer`), which must not refuse. | absent from record; refused on hit; ck vocabulary passes |
| P-23 | 10.1, 10.2 | Conservation: for any generated set of interruptions, each category total equals the generated count, and equals the sum over `byRole[role][family][kind]`, both summed by role and summed by family. Near-misses are never counted. | `interruption_strategy()`: categories × roles × families over workflow agents and the orchestrator. Near-misses: RTK `allow` attachments, `done-verify` `hook_blocking_error` attachments holding only a gate report, non-error results quoting each phrase, error results where the phrase is mid-text (a Bash failure printing it), guard attachments whose stdout has no decision. | totals equal; near-misses 0 |
| P-24 | 10.2 | For any shell command, `command_family` follows the rules: the first command after any `cd ... &&`/`;` prefix and env-var assignments. `sed`, `perl -i` → sed; `rm`, `unlink`, `truncate` → rm; `mv` → mv; `git ...` (incl. `git -C d rm`) → git; `python`, `python3`, `uv run python`, `python -m` → python; a write redirect with no other match → redirect; else other. Non-shell (None) → other. | Each family's surface forms. Near-misses: `2>&1` only and `> /dev/null` (not a write redirect → other); `pytest`, `grep sed` (other); `rmdir` (other); `gitk` (other); `pythonx` (other). Leading whitespace, tabs, `\` continuations. | the family per rule |
| P-25 | 10.3 | For any set of asks with result gaps, `totals.waitMs` = Σ max(0, result ts − ask ts) over matched pairs, never negative. Adding one more matched ask with gap g raises it by g. | Gaps 0-3,600 s, results missing, results before asks, duplicate attachments for one `toolUseID` | sum as stated, monotone |
| P-26 | 10.6 | Metamorphic: inserting malformed or unknown lines anywhere in any transcript never changes any interruption count. | P-23 sets plus insertions of `{not json`, `{}`, truncated last line, unknown `type`, attachment without `stdout` | counts unchanged |
| P-27 | 10.7, 11.4 | For any record without interruption data, `ck stats show` prints `interruptions: not recorded`, the shared record omits the block, and no aggregate counts it as zero. | Pre-feature seeded records, records with `interruptions: null`, mixed stores (old + new) | `not recorded`; omitted |
| P-28 | 10.8, D12 | Mode equivalence: for any interruption set, a workflow run and an agent-mode run carrying the same events in the same roles produce equal `interruptions` blocks. | `interruption_strategy()` rendered both ways (workflow agents by label vs Agent-tool subagents by `[ck:<id>]` description) | equal blocks |
| P-29 | 11.1, 11.2 | For any record with interruptions, the shareable record validates against `SHAREABLE_SCHEMA` with schema `ck-run-analytics/1`. Under `interruptions` it has only numbers and enum keys (roles, families, categories, model families). `flatten_paths` shows no value from the local record's command, path or reason fields. | P-23 sets with planted command texts, paths and spec names | valid; no planted strings |
| P-31 | 8.7, 8.8, 8.9 | For any hand-off facts where the hand-off is needed, `decide_handoff` gives: `handoff` with no run branch iff `spec_same` is True; `handoff` with `run_branch == "<spec>-run"` and `start_point == worktree_head` iff `spec_same` is False, `main_clean` is True, `worktree_spec_dirty` is False, `worktree_head` is set and `run_branch_at` is None or equals `worktree_head`; otherwise `refuse` with a non-empty `recovery` whose commands contain `git switch -c <spec>-run <worktree_head>` (when the head is known) and `ck run <spec>`. It never raises. | `HandoffFacts` with `spec_same`, `main_clean`, `worktree_spec_dirty` ∈ {True, False, None}, `spec_in_main`/`spec_in_worktree` ∈ {True, False}, heads as random shas or None, `run_branch_at` ∈ {None, worktree_head, other sha}, spec names from kebab slugs | as stated |
| P-32 | 3.4 | For any sequence of `mark`, `refresh`, `unmark` (and `marker-clear`) events with timestamps, `run_marker.active(session, now)` is True iff the last `mark` of that session is not followed by an `unmark`, and `now` minus the latest `mark`/`refresh` time is under `MARKER_IDLE_S` (900 s). Events of other sessions never change the result. Skipped when the marker is not built. | Sequences of 0-12 events over 2 sessions, gaps 0-3,600 s, `now` injected; module-scoped data dir | as stated |
| P-30 | 10.4 | Metamorphic: adding or removing guard-log events never changes the recorded counts (the transcript wins). A difference is noted iff log and transcript disagree. | P-23 sets with the log equal, missing events, extra events, or events for other sessions | counts unchanged; difference ⇔ disagreement |

Property test files:
- `tests/test_guard_properties.py` (pure seams): P-2, P-12, P-13, P-24.
- `tests/test_guard_properties_hook.py` (real hook and repo): P-1, P-3, P-4, P-5, P-6.
- `tests/test_caller_properties.py` (pure `classify_caller`, recorded fixtures as seeds; `run_marker` with an
  injected clock): P-11, P-32.
- `tests/test_postcheck_properties.py` (real hooks, repo and shell): P-7, P-8, P-9, P-10, P-21.
- `tests/test_guard_ladder_properties.py` (bridge plus CLI): P-14, P-15, P-16.
- `tests/test_park_properties.py`: P-17, P-18.
- `tests/test_handoff_properties.py`: P-19, P-20, P-31.
- `tests/test_stats_properties_interruptions.py`: P-23, P-25, P-26, P-28, P-30.
- `tests/test_stats_properties_shared.py`: P-22, P-27, P-29.

For each safety property (P-1, P-3, P-9, P-11, P-12, P-14, P-24), one integration case shows that the real
boundary calls the seam: TC-1/TC-3 (hook to `destroy_targets` and the directory expansion), TC-13 (post-check to
`may_write`), TC-20/TC-22 (hook to `classify_caller`), TC-41 (CLI to `agents.guard_ladder`), TC-59 (collect to
`command_family`).

## Harness Contract

These are design.md's names (Public Interfaces, Decision Seams, Data Models). The tests call exactly these.

1. **Caller classification** (`claude_kiro/hooks/_shared/guard_policy.py`, `claude_kiro/handoff.py`):
   - `classify_caller(hook_input: Mapping, env: Mapping[str, str], workflow_marker: bool) -> Literal["task_agent",
     "main_session"]`. It never raises and takes no path.
   - `handoff.is_headless(env) -> bool`: True for `CK_RUN_HEADLESS=1` (set by `ck run` for its child) or
     `CLAUDE_CODE_ENTRYPOINT` starting with `sdk`.
   - Fallback marker, only when `capture-summary.json` says Workflow agents lack `agent_id`:
     `claude_kiro.hooks._shared.run_marker.{mark, unmark, active}`, set through the hook by the Bash calls
     `ck guard workflow-begin <spec>` / `ck guard workflow-end`, read only for calls with no `agent_id`;
     `run_marker.refresh`, `MARKER_IDLE_S = 900`, and the `UserPromptSubmit` hook `marker-clear`
     (`execute_hook("marker-clear", input)`, no output).
2. **Bash pre-check parse seam**: `guard_policy.destroy_targets(command: str, cwd: str) -> List[DestroyTarget]`
   (`path`, `op` ∈ {delete, move}, `program`). It is pure: path math only, `cd` prefixes resolved, no filesystem or
   git access. Directory targets are expanded by the hook with `git_utils.tree_files`.
3. **Post-check**:
   - Registry name `post-bash-check` in `HookRegistry._hooks`, `claude_kiro.hooks.post_bash_check:hook`.
   - Installed path: `done-verify` (PostToolUse, matcher includes `Bash`) calls `post_bash_check.check`.
     `HOOK_SETTINGS` and `plugin/hooks/hooks.json` do not change.
   - Input: the documented PostToolUse fields, including `tool_use_id` and `tool_response`.
   - Output: `None` or `{"decision": "block", "reason": "[ck post-check] ..."}`; through `done-verify`, one `block`
     whose reason has the gate report first and the post-check message second.
   - Pre-filter seam `guard_policy.may_write(command: str) -> bool`.
   - Works when called after a PreToolUse call for the same `tool_use_id`, and when the PreToolUse call was absent
     (it must then still not fail). Snapshots are one file per tool call.
4. **Guard log** (`claude_kiro.analytics.guardlog`):
   - Path: `log_path(env=None)` = `<data_dir>/guard-events.jsonl`; rotated file `guard-events.1.jsonl`.
   - Size limit: the constant `GUARD_LOG_LIMIT = 1_048_576`; one rotated file is kept.
   - Format: one JSON object per line, keys `v` (1), `ts` (ISO UTC), `sessionId`, `agentId`, `agentType`, `caller`,
     `tool`, `decision` (`"ask"|"deny"|"post-check"`), `category`, `reason`, `path` (repo-relative), `command` (Bash
     or `null`), `family`, `toolUseId`.
   - `append_event(event, env=None) -> bool` (never raises), `read_events(session_ids, *, window, agent_ids, env)`,
     `leak_terms(env=None)`.
5. **Notification**:
   - Config: `run.notify_command`, set with `ck agents set run.notify_command "<command>"` (global prefs).
   - The command runs with `shell=True` in the main checkout. The message goes on its stdin as JSON
     `{"hook_event_name": "Notification", "notification_type": "ck_needs_input", "title": "ck", "message"}` and in
     `CK_NOTIFY_MESSAGE`; never in argv or the shell string.
   - Timeout: 10 s. `notify.notify_message(spec, task)` builds the message.
6. **CLI commands**:
   - `ck worktree park SPEC N [--no-notify] [--json]`, note JSON `{"file", "kind", "test", "requirement",
     "decision"}` on stdin. Exit 0 whenever the note is written. JSON: `{"parked": true, "note": str,
     "notification": "sent"|"not_configured"|"failed"|"skipped"|"deferred", "detail": str}`. `deferred` when the env
     has `CK_RUN_NOTIFY=parent` (inside `ck run`, whose own process then notifies once per note and logs a
     `ck run: notification ...` line).
   - `ck worktree resolve SPEC N [--json]`, stdin `{"answer": str}`. JSON: `{"resolved": true, "decisionNote": str,
     "hadParkNote": bool}`. Exit 3 on bad stdin.
   - `ck worktree report SPEC --json` gains `needsInput: [{"task", "file", "kind", "test", "requirement", "decision",
     "parkedAt"}]`. A parked task is in no other list. The text output adds a `needs input` line.
   - `ck plan SPEC --json` lists a parked task as unfinished (ready when its deps are Done).
   - `ck agents --guard-ladder MODEL [--json]` → JSON list of model aliases (`agents.guard_ladder`).
   - `ck run SPEC --command implement|create|review` (default `implement`). The prompt is `/spec:implement SPEC all
     ...`, `/spec:create SPEC ...` or `/spec:review SPEC ...`. The child env has `CK_RUN_HEADLESS=1`.
   - `ck handoff KIND [ARGS...] [--json]`. JSON: `{"handoff": false, "interactive": bool}` (exit 0); `{"handoff":
     true, "launched": true, "mainCheckout", "log", "follow", "report", "command", "runBranch"}` (exit 0;
     `runBranch` is `"<spec>-run"` or null); `{"handoff": true, "launched": false, "error", "manualCommand"}`
     (exit 3). Pure seam `handoff.decide_handoff(HandoffFacts(...))` with design.md's fields.
7. **Workflow (`plugin/workflows/spec-implement.js`)**:
   - The decisions block exports `guardLadder(model) -> string[]` (mirror of `agents.guard_ladder`), `guardStopOf`
     and `parkAction`.
   - The `RESULT` schema gains `guardStop: {file, kind, test, requirement}`.
   - Args gain `interactive: boolean` (default false, meaning unattended).
   - Guard attempts are labelled `task <N> guard <k> (<model>)` (`task <N> [test] guard <k> (<model>)` for test
     tasks). Escalation entries are `{stage: 'guard', model}`.
   - Parking is an agent labelled `park <N>`, whose prompt contains `ck worktree park <spec> <N>` and which answers
     `{parked, notification, detail}`.
   - Result gains `needsInput: [{task, file, kind, test, requirement, decision, action, notification}]`,
     `notifications: {sent, failed: [{task, detail}], notConfigured, hint}` and `tasks[].parked`.
   - Every prompt reads `<WT>.decision.md` first when it exists.
   - The vendored `.claude/workflows/spec-implement.js` is identical.
   - `plugin/workflows/spec-create.js` (review mode): with implementation started, the revise prompt holds the
     frozen-spec rule, and a change to existing requirements.md / test-plan.md text comes back under `escalated`,
     then in `openQuestions` with `source: 'revise'` (5.4). The vendored copy is identical.
8. **Agent mode**:
   - Description template `[ck:<run_id>] implement task <N> guard <k>`.
   - `analytics.roles.parse_agent_description` returns `step == "guard"` for it; the Workflow label `park <N>` maps
     to role `orchestrator`.
   - The instructions name `guardStop`, `ck agents --guard-ladder`, `ck worktree park`, `ck worktree resolve` and
     `AskUserQuestion`.
9. **Analytics**:
   - `guard_policy.command_family(command: Optional[str]) -> str`; `share.COMMAND_FAMILIES = ("sed", "rm", "mv",
     "git", "python", "redirect", "other")`, `share.INTERRUPTION_ROLES`, `share.INTERRUPTION_KINDS = ("prompts",
     "guard_denials", "post_check_catches", "guard_escalations", "auto_mode_denials", "headless_denials",
     "user_rejects", "isolation_refusals")`.
   - Local record key `interruptions`: `{"v": 1, "totals": {"prompts", "waitMs", "guardDenials", "postCheckCatches",
     "guardEscalations", "autoModeDenials", "headlessDenials", "userRejects", "isolationRefusals"}, "byRole":
     {role: {family: {kind: int}}}, "waitMsByRole": {role: int}, "escalationModels": {family: int}, "guardLog":
     {"state", "events", "differences": {kind: {"log", "transcripts"}}}, "skippedLines": int}`. Absent when not
     recorded. `guardLog.state` ∈ {`ok`, `rotated`, `unreadable`}; a never-created log is `ok` with 0 events.
     `missing` gets `{"what": "guard_log"}` only when the log is unreadable or rotated away.
   - Shared record key `interruptions` (snake_case, design "Shareable record"); `guardLog`, `differences` and
     `skippedLines` never leave the machine.
   - `ck stats show` prints one line starting `interruptions:`.

## Critical Paths

1. **Unattended sed on a test, no prompt, weakening caught.**
   1. A task agent runs `sed -i` on an existing test: no ask or deny (TC-2, TC-8).
   2. The sed removes an assertion: the post-check tells the agent to restore it (TC-9, TC-18) and logs an event
      (TC-50).
   3. The agent ignores it: the gate fails at Done (TC-16).
2. **Existing test deleted by a task agent.**
   1. The deletion is denied with a keep-and-report reason (TC-1, TC-5).
   2. It is logged (TC-50). The same holds for a test in a task worktree reached by an absolute path (TC-71, TC-73).
   3. The task reports `guardStop`, and the ladder climbs sonnet → opus → fable at once, spending no retry (TC-29,
      TC-30, TC-79).
   4. Still unresolved: the task is parked with a notification (TC-33, TC-34, TC-37).
   5. Independent tasks merge and dependents wait (TC-34, P-16).
   6. The report lists needs-input (TC-37).
   7. I record a decision with `ck worktree resolve`, and the re-run's fresh attempt reads it (TC-78, TC-80, TC-40).
3. **The same ladder and park in agent mode**: TC-41, TC-42, TC-43, TC-37 and TC-40 (shared CLI), then counted
   (TC-65).
4. **My own session.**
   - A spec edit passes (TC-27).
   - Deleting an existing test asks per ck.json (TC-1, TC-22), also inside a task worktree (TC-72).
   - Interactive unresolved guard stop: `AskUserQuestion` (TC-36).
5. **Hand-off.**
   1. `/spec:implement demo` in a `.claude/worktrees/` session runs `ck handoff` first (TC-49).
   2. `ck run` launches from the main checkout and prints the log and follow command (TC-44).
   3. create/review get the same treatment (TC-45).
   4. A launch failure prints the manual command (TC-46).
   5. The main checkout and single tasks are untouched (TC-47).
   6. A spec that exists only on the worktree's branch runs on a `<spec>-run` branch at the worktree's HEAD
      (TC-83); a dirty main checkout refuses with the recovery commands (TC-84); an identical spec runs as usual
      (TC-85).
6. **Interruptions in stats.**
   1. Workflow run collected (TC-56, TC-57).
   2. Agent-mode run collected (TC-65).
   3. Guard log joined (TC-61).
   4. Shared record clean (TC-66).
   5. Show line printed (TC-68); old records show "not recorded" (TC-64).

## Commands

| Level / file | Command |
|---|---|
| Whole suite (gate verify) | `uv run pytest -q -n auto` |
| Bash pre-check | `uv run pytest -q -n auto tests/test_guard_bash.py tests/test_guard.py` |
| Post-check | `uv run pytest -q -n auto tests/test_guard_postcheck.py tests/test_redteam_postcheck.py tests/test_postcheck_properties.py` |
| Callers, deny, spec edits | `uv run pytest -q -n auto tests/test_guard_callers.py tests/test_caller_properties.py` |
| Live Workflow-agent capture (opt-in, not in the gate) | `CK_LIVE_CLAUDE=1 uv run pytest -q tests/test_guard_callers.py -k live` |
| Guard properties | `uv run pytest -q -n auto tests/test_guard_properties.py tests/test_guard_properties_hook.py` |
| Guard log | `uv run pytest -q -n auto tests/test_guard_log.py` |
| Workflow ladder / park (Node) | `node tests/js/guard_ladder.test.mjs` (also via `uv run pytest -q tests/test_workflow_scripts.py`) |
| Review revise on a started spec (Node) | `node tests/js/spec_review_frozen.test.mjs` |
| Existing workflow tests stay green | `uv run pytest -q tests/test_workflow_scripts.py tests/test_landing_scheduler_properties.py` |
| Park, resolve, notify, report, ladder CLI | `uv run pytest -q -n auto tests/test_guard_park.py tests/test_park_properties.py tests/test_redteam_notify.py` |
| Ladder and park properties | `uv run pytest -q -n auto tests/test_guard_ladder_properties.py` |
| Agent-mode docs | `uv run pytest -q -n auto tests/test_guard_agent_mode_docs.py tests/test_stats_agents_docs.py tests/test_plugin.py` |
| Hand-off | `uv run pytest -q -n auto tests/test_handoff.py tests/test_handoff_properties.py tests/test_run.py` |
| Interruption stats | `uv run pytest -q -n auto tests/test_stats_interruptions.py` |
| Interruption properties | `uv run pytest -q -n auto tests/test_stats_properties_interruptions.py tests/test_stats_properties_shared.py tests/test_redteam_shared.py` |
| Backward compatibility of analytics | `uv run pytest -q -n auto tests/test_stats_*.py` |

## Not Tested

- **Real human waiting and real approvals.** Wait time is computed from the timestamps of real transcript shapes
  (TC-60, P-25). No test waits for a person.
- **Claude Code's own auto-mode classifier, isolation check and headless refusal.** These are out of scope to
  change. Only their recorded phrasing is parsed. If Claude Code changes the wording, the counts drop to zero.
  The near-miss tests show that wrong phrasing is never counted, but nothing detects the drift itself.
- **Real ntfy delivery.** The notification command is test-owned. The user's `~/.claude/hooks/notify.sh` is never
  run (`CK_CONFIG_HOME` is isolated in every test).
- **Whether Fable actually resolves a guard stop.** Model quality is not testable here. Only the ladder's control
  flow is tested.
- **The full headless run after a hand-off.** `claude` is faked. The launch argv, cwd, env and log are tested, not
  a real model session. The live Workflow-agent capture (TC-23) is opt-in, because the gate cannot call a real
  model.
- **Rotation at other sizes.** Rotation is tested by planting a log just under `GUARD_LOG_LIMIT`; the limit is a
  documented constant with no override.
- **Performance on very large repositories** beyond the NFR's 1,000 files / 5 changed test files.
- **Whether the main-session ask actually reaches the user.** The hook's output is tested. How Claude Code renders
  it is not.
