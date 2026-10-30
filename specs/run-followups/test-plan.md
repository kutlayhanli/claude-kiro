# Test Plan: Run follow-ups (run ETA, analytics hardening, deterministic P-10, Verify-cycle lint)

Source of truth: [PLAN.md](PLAN.md) (Decided, D1-D18) and [requirements.md](requirements.md). D11-D18 settled the
review's open questions; the rows they changed say so (TC-57, TC-58, TC-59, TC-81, TC-84, TC-92, TC-96, TC-97,
P-17, P-20, P-21) and the rows they added are TC-99..TC-103 and P-27. This plan was first
written without design.md, from the requirements and from how this repository is tested today; the revision after
review aligned the Workflow-mode harness (Test Infrastructure item 2), TC-25/26, TC-51..TC-54, P-24 and the
changed criteria with design.md. Where a name below still differs from design.md, design.md wins and the tasks.md
preamble gives the mapping. Wherever the requirements
leave a name to the design (PLAN "Delegated to Spec"), the Harness Contract proposes one exact name. The tests read
every such name from one constant in `tests/eta_helpers.py`, so a different name in design.md is a one-line change
there and does not mean rewriting the tests.

## Strategy

**Levels used, and why.**

| Level | Used for | How it runs here |
|---|---|---|
| Integration (CLI) | Every criterion involving files, the record store, config, process boundaries or errors: `ck eta`, `ck progress`, the status-line command, `ck stats collect`, `ck lint`, `ck plan`, `ck worktree park`, notifications | The real `ck` CLI as a subprocess (`stats_helpers.ck_sub`, `python -m claude_kiro.cli`) against temp git repos, fake Claude homes (`FakeClaudeHome`), real `XDG_DATA_HOME` stores built by the real `ck stats collect`, real progress files and a real notify script. `ck lint` tests use the existing in-process `CliRunner` helper `ck()` from `tests/test_lint.py`, which is the real click entry point |
| Workflow (stub runtime) | The two shipped Workflow scripts writing progress events and ETA log lines (3.4, 8.1, 8.2), the P-10 scheduler property (12.x), refusal on a Verify cycle (13.3) | The real `plugin/workflows/spec-implement.js` / `spec-create.js` under the existing stub runtime (`tests/js/load_workflow.mjs`, `fake_project.mjs`, `bridge.mjs`, `tests/landing_bridge.py`). Agents are the only fakes. In `execCk` mode the fake agents parse the progress flags out of the claim, land, plan and `ck progress` commands in their prompts and record those events through the real CLI (Test Infrastructure item 2), so events reach a real progress file |
| Static document | Agent mode (3.4, 8.1, 13.3): the Agent-tool fallback is an LLM following `without-workflow-tool.md`, which cannot be run in a test | Reads of the packaged and vendored documents (the `test_stats_agents_docs.py` pattern), cross-checked against the commands the Workflow script actually emits, so both modes name the same events and fields |
| Property (Hypothesis) | Every universally quantified criterion (see Properties) | Pure seams (`claude_kiro.eta.*`, `claude_kiro.lint.lint_spec`) fed history records that the **real collector** produced once per worker (the pools in Test Infrastructure), plus two bridge properties for the Workflow script |
| End-to-end smoke | The critical paths: launch estimate, a live implement run with blocked/waiting/slip/done, a create run, status line, notifications, the lint refusal | Chains of the real CLI calls above, one test per path (Critical Paths) |
| Performance / NFR | Simulation speed, status-line cost, never slowing a run, privacy modes, no network, lint precision | Timed real subprocesses (with the interpreter start-up baseline subtracted), real file modes, the existing `no_network` fixture |

**The integration-first rule as applied here.**
- Every criterion whose behaviour crosses a boundary (store, progress file, spec files, config, notify subprocess,
  workflow prompts, collect.log, `ck lint` exit code) has a CLI-level case that crosses that boundary for real.
  History is never hand-written: it comes from running the real `ck stats collect --all` over fake Claude homes in
  the shape real runs leave behind.
- Nothing in `claude_kiro` is mocked or monkeypatched in any new test. The fakes stand in only for things ck does
  not own: Claude Code's home layout and agents (fake home, stub Workflow runtime), the clock (`CK_NOW`), the
  machine's slot count (`CK_AGENT_SLOTS`, which already exists), the notify command (a script), CPU load (busy
  processes) and randomness (`--seed`).
- Unit-level tests exist only as properties over pure seams with real branching: the simulator, the state machine
  (on-track/slipping/blocked/waiting/done), notification deduplication, the progress-line reader, bundled-default
  aggregation, and the lint rule. Each seam also has a CLI case showing that the boundary calls it.
- Refactor-only criteria (11.2 and the `__import__("time")` clean-up in D7) have no behaviour visible from
  outside, so they get regression guards that pass before and after. See Not Tested. 11.3 is observable through
  an open-count audit, so TC-71 fails first.

**Fails-first.** Every new case names an entry point, flag, event, file or field that does not exist yet (`ck eta`,
`ck progress`, the status-line command, `--json` keys, record fields, the new lint entries, the bridge's virtual
clock). So each case fails before the feature exists because the behaviour is missing, not because of a broken
fixture. The D7 cases fail today: the unreadable-store backfill writes records, a malformed record is skipped
rather than refused, the sandbox `--json` failure prints plain text, a whole-sweep failure is dropped, a missing
session transcript leaves no `outcome` entry, and agent-mode collection opens each transcript once more than a
workflow collect (TC-71).

## Test Infrastructure

### Exists (reuse as is)

| Path | What it gives |
|---|---|
| `tests/conftest.py` | autouse isolation of `CK_CONFIG_HOME`, `XDG_DATA_HOME` / `CK_DATA_HOME`, `CLAUDE_CONFIG_DIR`, git identity, gate registry; `no_network` (in-process and child-process network guard, `CK_TEST_NET_LOG`) |
| `tests/stats_helpers.py` | `FakeClaudeHome`, `FakeSession.implement_run` / `create_run` / `tool_agents`, `AgentSpec`, `ToolAgentSpec`, `TaskSpec`, `implement_result`, `create_result`, `fresh_env()`, `ck_sub`, `stats`, `load_record`, `store_files`, `all_bytes`, `flatten_paths`, `CONFIDENTIAL`, `seeded_home`, `seeded_local_record`, `begin`, `backdate_manifest`, `spec_repo`, `land`, `fail`, `mark_done`, `read_only`, `backfill_home`, `kiro_checkout`, `git_project` |
| `tests/test_stats_agents_collect.py` | `unreadable(path)` context manager (chmod 0, restored), `needs_user` skip-as-root marker, the `world` / `open_run` / `add_agents` / `rests` pattern for agent-mode runs |
| `tests/test_stats_agents_backfill.py` | `Home`, `impl`, `backfill`, `plan`, `records`, `count`, `run_usd` helpers |
| `tests/test_lint.py` | `ck(cwd, *args)` (in-process click), `block(...)` tasks.md block builder, `write_spec`, the `market` fixture (import-based Verify-order cycle) |
| `tests/landing_bridge.py`, `tests/js/bridge.mjs`, `fake_project.mjs`, `load_workflow.mjs` | the Node bridge (module-scoped `bridge` fixture), `scenarios()`, `dags()`, `subsets()` strategies, `prepare`, `run`, `events`, `with_finish_first` (in `test_landing_scheduler_properties.py`) |
| `tests/js/spec_create.test.mjs` | the `stub()` agent for spec-create.js (moved into a shared module, below) |
| `tests/test_stats_agents_docs.py` | static reads of packaged docs and `claude_kiro.plugin.vendor_files()` renderings |
| `tests/test_plugin.py` | packaged == vendored copies (covers the `.claude/workflows/` copies of every edited script and document) |
| prompt-free-runs (merged first; this spec branches from it) | `tests/guard_helpers.py`: `notify_script(bin_dir, mode, out)` (`ok` / `fail` / `slow` / `missing`), `set_notify(command)`, `claimed_task_worktree`; the CLI `ck worktree park SPEC N`, `ck worktree resolve`, and `needsInput` in `ck worktree report --json` |

### Add

1. **`tests/eta_strategies.py`** (test-owned, small, imports no project code at module level):
   - **Event and model constants**, the single place each is spelled: `ETA_SCHEMA_RE = r"^ck-eta/\d+$"`,
     `SIZE_KEYS = ("filesCount", "criteriaCount")`, `MATCH_STEPS` (design.md's `widestMatch` values), the design's
     `ck-progress/1` event kinds, `STALE_MIN = 120`, and `NO_NETWORK = pytest.mark.usefixtures("no_network")`.
   - The Hypothesis strategies `graphs()`, `events()`, `times()`, `malformed_lines()` (Properties), `busy(n)`, and
     `progress_lines(path) -> List[dict]`, the test's own JSON-lines reader. It does not use the project's reader.

   **`tests/eta_helpers.py`** (test-owned, never imports `claude_kiro.cli`, independent of `eta_strategies.py`):
   - **Command and environment names**, each spelled once: `PROGRESS_BEGIN`, `PROGRESS_EVENT`, `PROGRESS_END`,
     `STATUSLINE_CMD = ("status-line",)`, `STATUSLINE_BIN = "ck-status-line"`, `SLIP_KEY = "run.eta_slip_minutes"`,
     `DEFAULTS_ENV = "CK_ETA_DEFAULTS"`, `LINE_TIMEOUT_ENV = "CK_ETA_LINE_TIMEOUT_S"`, `NOW_ENV = "CK_NOW"`,
     `DEFAULTS_CMD = ("stats", "eta-defaults")`, `BACKTEST_CMD = ("stats", "eta-backtest")`,
     `RECHECK_ENV = "CK_ETA_RECHECK_S"` (time-only slip check interval, D14), `NOTIFY_TYPE_ENV = "CK_NOTIFY_TYPE"`
     (D13).
   - `typed_notify_script(bin_dir, out) -> str`: a test-owned notify command that appends, per call, one JSON line
     `{"message", "type"}` with the stdin `message` and the `CK_NOTIFY_TYPE` it was run with (null when unset).
     `typed_notices(out) -> List[Tuple[str, Optional[str]]]` reads them back. Used by TC-101 and TC-97.
   - `eta(env, cwd, spec, *args, now=None) -> Result`: runs `ck eta <spec> <args>` with `TZ=UTC` and `CK_NOW=now`.
     `eta_json(...) -> dict` adds `--json` and parses it.
   - `begin(...)`, `event(...)`, `end(...)`: run `ck progress begin|event|end` (design CLI) and return their JSON.
   - `eta_spec(root, tasks: List[TaskDef], *, done=()) -> Path`: writes a real git repo with
     `specs/<spec>/tasks.md` built from `TaskDef(id, track="impl", model=None, risk=None, files=1, criteria=1,
     deps=(), verifies=(), verify=None)`. Files and acceptance criteria are written as real `**Files:**` bullets and
     `**Acceptance:**` items that cite criteria ids, so `ck plan --json` and the size signal read the same tasks.md.
     Invented names only (`demo`, `ledger`, `orbit`).
   - `History` pools, built **by the real collector** once per xdist worker (`tmp_path_factory`, session scope),
     then copied into each test's `XDG_DATA_HOME` with `copytree`:
     - `POOL_IMPL`: 48 spec-implement runs, written with `FakeSession.implement_run` + `implement_result`. Each run
       has 4-8 tasks spread over track {impl, test} x start model {sonnet, opus} x risk {normal, safety}. Agent
       labels follow the real grammar (`task N`, `task N [test]`, `review N`, `land N,M`, `task N retry k`). The
       agent duration is `base(track, risk) * (1 + 0.5 * (files + criteria))` times a fixed jitter grid, so
       duration rises with size (6.6). About 15% of tasks have attempts > 1 and 5% have escalations. Half the runs
       carry review and landing waits (`queued_at_ms` on review/land agents, `landing` on tasks). Each run's
       project has a real `specs/<spec>/tasks.md` whose Files/criteria counts match. Then `ck stats collect --all`.
     - `POOL_CREATE`: 10 spec-create runs with agents in the real phases (`Requirements`, `Design + Test Plan`,
       `Task Tracks`, `Link`, `Adversarial Review`, `Revise`, `Consistency Check`), timed so that totals span
       58-96 min. 3 runs have a second consistency pass. 2 runs have agents with no timing (`omit_started=True`)
       for 2.5.
     - `POOL_IMPL_NOWAIT`: the runs of `POOL_IMPL` that carry no review or landing waits (a subset copy, for TC-2).
     - `POOL_COSTLESS`: 10 implement runs whose agents have `requests=[]` (no usage), so their records carry no cost.
     - `POOL_SIZED_FEW`: 4 runs with counts and 40 runs whose spec tasks.md has been deleted before collecting
       (unsized), for 6.3 and 6.4.
     - `history_records(pool) -> List[dict]`: the records as `json.loads` of the pool's `runs/*.json`, for the
       property seams.
   - `progress_file_of(env, spec) -> Path`: from `ck eta <spec> --json`'s `progressFile`.
   - `notify_messages(out) -> List[str]`: the `message` of each stdin JSON that `notify_script(..., "ok", out)`
     recorded.
   - `baseline_startup_s() -> float`: median of 5 timings of `ck --version`, subtracted in the `ck eta` timing NFR
     (TC-85).
   - `bare_startup_s() -> float`: median of 5 timings of `python -I -c "import claude_kiro"`, subtracted in the
     status-line timing (TC-86), so the click and CLI import cost the status line must avoid is not cancelled out.
   - `busy(n)` (in `eta_strategies.py`): a context manager that starts `n` CPU-bound `python -c "while True: pass"`
     processes and kills them on exit.
2. **Bridge extensions** (test-owned JS):
   - `tests/js/load_workflow.mjs`: `runWorkflow(script, {agent, args, slots, clock})`. With `clock: 'virtual'`,
     `sleep(ms)` and the trace's `t` come from a virtual-time queue. Timers fire in order of (due time, creation
     order), and the queue only advances once every pending microtask has drained. Without `clock`, real timers run
     exactly as today. `sleep` is exported as a function that reads the active clock.
   - `tests/js/fake_project.mjs`: option `execCk: {cmd: [python, "-m", "claude_kiro.cli"], env: {...}}`. The
     shipped prompts put their commands on bare `Command:` lines or in double quotes, against paths (`ROOT`,
     `WT(n)`) the fake project never creates, and `land` carries `--gate --install --release`. So execCk never runs
     the prompt's claim or land command as written. Instead, before answering, the fake agent:
     - finds the `ck ... worktree claim <spec> <n>` command in a task, retry, revise, landfix or resolve prompt
       (bare line or quoted), parses its `--progress-run`, `--progress-event`, `--progress-model` and `--attempt`
       flags, and records that event through the real CLI with the test-only form `ck progress event <spec>
       <kind> --run <key> --task <n> --model <m> --attempt <k>` (the claim kinds are accepted by `ck progress event`
       only when `CK_PROGRESS_TEST_KINDS=1` is set in the env, which execCk sets);
     - for a land prompt, parses `--progress-run` and the task ids from the `ck ... worktree land` command, and
       records `landing` (with `merged`, `red`, `conflict`, `protected` taken from the fake project's own scripted
       result for that landing) and one `merge` / `hand-landing` per task the same way, then answers with the
       `eta` the CLI printed;
     - runs any `ck ... plan ... --failed` and `ck ... progress event ...` command in the prompt as written, with the
       test's env (these touch only the progress file).
     `etaAnswer: 'null' | 'number' | 'missing'` overrides the answer's `eta` field to test 8.2.
   - `tests/js/fake_create.mjs`: the `stub()` of `spec_create.test.mjs`, moved into a module so both that file and
     the bridge share it. `spec_create.test.mjs` imports it and is otherwise unchanged.
   - `tests/js/bridge.mjs`: `run` accepts `clock` and `execCk`. New op `{op: 'create', args, overrides, execCk}`
     runs `plugin/workflows/spec-create.js` with the shared stub and returns `{result, logs, trace, prompts}`.
   - `tests/landing_bridge.py`: `Bridge.run(..., clock=None, exec_ck=None)` and `Bridge.create(args, overrides=None,
     exec_ck=None)`.
3. **`tests/openaudit/sitecustomize.py`** (like `tests/netguard/`): when `CK_TEST_OPEN_LOG` is set, it registers
   `sys.addaudithook` and appends the path of every `open` event that matches `agent-*.jsonl` to that log. Used
   only by TC-71.
4. **`tests/fixtures/lint/agent-mode-analytics-pre-fix/tasks.md`**: a byte copy of `git show
   74b7a7d^:specs/agent-mode-analytics/tasks.md` (task 8 Not Started, its Verify naming `tests/test_stats_perf.py`).
   This is real data from this repository, used for the lint-precision NFR.
5. **Test data rules**: every new `tests/test_eta_*.py` and `tests/test_lint_verify_*.py` file sets
   `pytestmark = pytest.mark.usefixtures("no_network")` (or `eta_strategies.NO_NETWORK`), which TC-89 checks.
   Invented spec, task and project names only. Confidential seeds come from
   `stats_helpers.CONFIDENTIAL`. All times are pinned with `CK_NOW` and `TZ=UTC`, except the `--watch` and
   real-timer cases. Every directory-permission case is skipped as root (`needs_user`).

## Test Cases

Unless a row says otherwise, the history is `POOL_IMPL` (or `POOL_CREATE`) copied into the test's store, the env sets
`CK_NOW=2026-10-30T15:00:00Z`, `TZ=UTC`, `CK_AGENT_SLOTS=4`, and the case runs under `no_network`. `T0` is `CK_NOW` at
launch.

### Story 1: implement estimate

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-1 | 1.1, 1.4, 4.1 | integration (CLI) | Given `eta_spec` with 6 open tasks (deps 1->2->3->5, 4->5, 6; task 4 `**Risk:** safety`) and `CK_AGENT_SLOTS=2`. When `ck eta demo` runs, then exit 0 and the text shows: `demo`, `6 open`, `2 slots`, a finish range `HH:MM-HH:MM` (p10-p90) with median `+N min`, a cost range `$a-$b`, the critical path equal to `ck plan --json`'s `criticalPath` with its median minutes, the two slowest tasks by median, `1 safety`, the historical retry and escalation rates, the basis (`N tasks, M runs`) and the line `not started`. `--json` holds the same values: `state: "not-started"`, `slots: 2`, p10 <= p50 <= p90 | real CLI, real tasks.md, real `ck plan`, store from the real collector | `tests/test_eta_implement.py` |
| TC-2 | 1.1 (slot cap) | integration (CLI) | Given 8 independent tasks and `POOL_IMPL_NOWAIT` (no review or landing waits, so all of a task's time holds a slot). When `ck eta --json --seed 1` runs with `CK_AGENT_SLOTS=1` and then with `=8`, then every quantile with 1 slot is >= the same quantile with 8 slots, and the 1-slot p50 is at least 0.8 x the sum of the eight tasks' `tasks[].medianMinutes` from the same output | real CLI, env slot cap read through `ck agents` logic | `tests/test_eta_implement.py` |
| TC-3 | 1.1 (open only) | integration (CLI) | Given TC-1's spec with tasks 1 and 2 marked Done in tasks.md, then `4 open`, the critical path has no 1 or 2, and `tasks[]` lists only open tasks | real tasks.md | `tests/test_eta_implement.py` |
| TC-4 | 1.2 | integration (CLI) | Given two collected histories that differ in one thing each: (a) safety/opus tasks run 30 min and normal/sonnet tasks 3 min; (b) attempts > 1 in 50% vs 0%; (c) review+landing waits of 10 min vs none. When one task of each kind is estimated with `--seed 3`, then in (a) the safety task's `tasks[].medianMinutes` is greater than 5x the normal one's; in (b) the p90 with retries is greater than without; in (c) the p50 with waits is greater than without by at least 10 min | real collector output for each history | `tests/test_eta_implement.py` |
| TC-5 | 1.3 | integration (CLI) | Parametrized over histories that leave fewer than the minimum matches at each step: all match; no record of the task's model; no record of model + risk; no record of the track; records exist but match nothing. Then `basis.match` is `exact`, `no-model`, `no-risk`, `no-track`, `bundled` in turn, and the text names the widest step used | real store | `tests/test_eta_implement.py` |
| TC-6 | 1.5, NFR stable output | integration (CLI) | `ck eta demo --seed 7` twice gives byte-identical stdout, and `--json` twice gives identical objects. Without `--seed`, two runs on the same inputs differ by at most 1 min in each of p10, p50 and p90 (on a history whose median run is about 60 min) | real CLI | `tests/test_eta_implement.py` |
| TC-7 | 1.6 | integration (CLI) | (a) A spec dir with requirements.md and no tasks.md, with `--kind implement`: non-zero exit, and the message says there is no tasks.md. (b) A tasks.md with no `### Task` blocks: non-zero, says it does not parse. (c) Every task Done: prints `nothing left to run`, exit 0 | real files | `tests/test_eta_implement.py` |
| TC-8 | 1.7, 5.3 (cycle) | integration (CLI) | Given tasks 2<->3 in a cycle, then `blocked:` names Task 2 and Task 3, no `ETA`/finish time is printed, exit 0, and `--json` has `state: "blocked"`, `finish: null`, `blockedReason` naming 2 and 3. A dependency on unknown Task 99 gives `blocked:` naming 99 and the task that needs it | real tasks.md, real `ck plan` cycle detection | `tests/test_eta_implement.py` |
| TC-9 | 1.8 | integration (CLI) | Given an in-progress run whose start event has `slots` missing or `"x"`. When `ck eta --json` runs, then `slots` equals `ck agents --json` `runtime.agentSlots` and `slotsSource: "default"`, and the text says the default cap was used | real progress file, real `ck agents` | `tests/test_eta_implement.py` |
| TC-10 | 1.9 | integration (CLI) | Given `POOL_COSTLESS` only. When `ck eta demo` runs, then the text says `cost unknown`, never contains `$0`, and `--json` has `cost: null`. With `POOL_IMPL` added, `cost.p10 > 0` | real collector records without usage | `tests/test_eta_implement.py` |

### Story 6: task size signal

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-11 | 6.1, 6.6 | integration (CLI) | Given `POOL_IMPL` (duration rises with size) and a spec with a 1-file/1-criterion task and an 8-file/10-criterion task, both impl/sonnet/normal. When `--json --seed 5` runs, then the big task's `medianMinutes` is greater than the small one's, and `sizeSignal: true` | real tasks.md counts, real records | `tests/test_eta_size.py` |
| TC-12 | 6.2 | integration (CLI) | Given one implement run collected with its spec tasks.md readable. Then each local record `tasks[]` entry has `filesCount` and `criteriaCount` equal to tasks.md. And `ck stats export --to <kiro_checkout>` writes records in which neither key appears at any path (`flatten_paths`) | real `ck stats collect`, real export | `tests/test_eta_size.py` |
| TC-13 | 6.3 | integration (CLI) | Given records collected **without** the size keys (collected before the size counts existed: the factory deletes them from the stored JSON) but whose projects' tasks.md is still readable. Then `sizeSignal: true` (the fit used counts read from tasks.md). With those tasks.md files deleted, the tasks still count in `basis.tasks` but the fit has too few sized tasks: `sizeSignal` falls back (TC-14) | real tasks.md reads | `tests/test_eta_size.py` |
| TC-14 | 6.4 | integration (CLI) | Given `POOL_SIZED_FEW`. Then `basis.bundledParts` includes `size`. With also `CK_ETA_DEFAULTS=<missing path>`, `sizeSignal: false` and the text says the size signal was not used | real env, real store | `tests/test_eta_size.py` |
| TC-15 | 6.5 | integration (CLI) | Given a task with no Files bullets and no named criteria. Then its `medianMinutes` > 0 and >= the median of the smallest sized task in the same history | real tasks.md | `tests/test_eta_size.py` |

### Story 2: create estimate

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-16 | 2.1, 2.3 | integration (CLI) | Given `POOL_CREATE` and `specs/orbit/` holding only PLAN.md. When `ck eta orbit` runs, then it lists each create phase with its median minutes, the finish range and cost range, and `not started`. `--json` has `kind: "create"`, `phases[]` (name, state `pending`, medianMinutes), and a p50 within the 58-96 min span of the pool | real store from create records | `tests/test_eta_create.py` |
| TC-17 | 2.2 | integration (CLI) | Given variants of the pool where the second consistency pass appears in 0/8, 3/8 and 8/8 runs (same seed). Then p50(0/8) <= p50(3/8) <= p50(8/8), the 0/8 variant never lists that pass as likely, and the 8/8 p50 exceeds the 0/8 p50 by about that phase's median (within 50%) | real records | `tests/test_eta_create.py` |
| TC-18 | 2.4, 2.3 | integration (CLI) | Given a create run started at T0, with phases 1-3 ended (phase-end events) and phase 4 started 10 min ago. When `ck eta orbit --json` runs at T0+40, then phases 1-3 have state `done`, phase 4 `running`, the rest `pending`. `finish.p10 >= now`, and the remaining time is at least phase 4's conditional remainder (> 0). The text marks done and running phases | real progress file | `tests/test_eta_create.py` |
| TC-19 | 2.5 | integration (CLI) | Adding the 2 runs without per-phase timing to the store leaves every `phases[].medianMinutes` unchanged (same seed), and no phase has median 0 | real records | `tests/test_eta_create.py` |
| TC-20 | 2.6 | integration (CLI) | `specs/orbit/` with only PLAN.md (no spec documents yet) -> `kind: "create"`. With tasks.md -> `kind: "implement"`. `--kind create` with tasks.md -> create. `--kind implement` without tasks.md -> TC-7(a) | real spec dirs | `tests/test_eta_create.py` |

### Story 3: one progress file, both modes

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-21 | 3.1, 3.8, NFR privacy | integration (CLI) | When `ck progress begin demo --kind implement --json` runs in the repo, then it prints `{"run": K, "file": P, ...}`, P is under the data dir (`$XDG_DATA_HOME/claude-kiro/...`) and not under the repo, P has mode 0600 and its directory 0700. The first line is a start event with `kind`, `spec`, `at` == `CK_NOW`, `slots` == 4 and a launch estimate (`finish` p10/p50/p90) | real CLI, real file modes | `tests/test_eta_progress.py` |
| TC-22 | 3.2 | integration (CLI) | Given start in a `spec_repo`. When these events are written through their real writers (design CLI; the claim and landing kinds are not accepted by `ck progress event` without the test hook): task-start 1, task-start 2, retry 2 and escalation 2 (model opus) through `ck worktree claim --progress-run`, landing [1] green and merge 1 through `ck worktree land --progress-run`, park 3 through `ck worktree park`, approval-start and approval-end through `ck progress event`, end through `ck progress end --outcome completed`. Then `progress_lines(P)` has them in order, with fields `task`, `model`, `tasks`, `red`, `outcome`, each with an `at` | real file | `tests/test_eta_progress.py` |
| TC-23 | 3.2 | integration (CLI) | Create run: start --kind create, phase-start/phase-end for 3 phases, approval-start/end, end. The lines are in order with `phase` names | real file | `tests/test_eta_progress.py` |
| TC-24 | 3.3 | integration (CLI) | Given an active run and a real repo (`spec_repo`). When `ck worktree park demo 3` (prompt-free-runs) runs with a note, then the progress file gains a park event for task 3, and `ck worktree report demo --json` still lists 3 in `needsInput`. When the note is cleared (`ck worktree resolve demo 3`, or a fresh `ck worktree claim`), an unpark event for 3 is appended | real park CLI, real progress file | `tests/test_eta_progress.py` |
| TC-25 | 3.4 (Workflow), 3.2 | workflow (bridge, execCk) | Given a scenario with tasks 1-4, task 2 failing once (retry) and escalating, task 3 red on its first landing (`checkRed`). The test runs `ck progress begin demo --kind implement --mode workflow --json` itself (the skill's step) and passes its `run` as `progressRun`. When the bridge runs spec-implement.js with `execCk` against a temp data home, then the real progress file has: the test's start, a task-start for each first attempt, retry 2, escalation 2 with its model, one landing per land agent with exactly its tasks and the fake project's red/merged sets, a merge per merged task, and then the test's own `ck progress end`. Merges equal the result's `merged`. The create op on spec-create.js gives phase-start/phase-end per phase actually run | real Workflow script, real `ck progress` subprocesses, real file | `tests/test_eta_workflow.py` |
| TC-26 | 3.4 (agent mode) | static + cross-check | The packaged and vendored `without-workflow-tool.md` hold backticked templates for every implement and create event: `ck progress begin ... --mode agents`, the claim command with `--progress-run --progress-event --progress-model --attempt`, the land command with `--progress-run`, `ck plan ... --progress-run --failed`, `ck progress event <spec> task-failed|approval-start|approval-end|phase-start|step-end`, and `ck progress end`. The set of (event kind, flag names) in the document equals the set the Workflow prompts carried in TC-25 | real documents, real script output | `tests/test_eta_agent_docs.py` |
| TC-27 | 3.5 | integration (subprocess) | Parametrized N in {2, 8}: N concurrent processes each append 50 events through `ck progress`, plus one `ck worktree park` running at the same time. Then the file has 1 + 50N + 1 lines after start, each a whole JSON object, and every (writer, sequence) pair appears exactly once | real concurrent subprocesses, real file | `tests/test_eta_progress.py` |
| TC-28 | 3.6 | integration (CLI) | Given a progress file and its copy with garbage lines inserted (truncated JSON, `[]`, `"text"`, an unknown event `warp`, NUL bytes, an empty line, a 10 KB line). Then `ck eta --json` on both gives identical `state`, counts, `finish` and `tasks` | real file | `tests/test_eta_progress.py` |
| TC-29 | 3.7 | integration (CLI) + workflow | (a) With the progress directory chmod 0500, or `CK_DATA_HOME` pointing at a regular file, `ck progress event demo approval-start --reason question --json` (a kind accepted without `CK_PROGRESS_TEST_KINDS`, so the refusal comes from the write failure) exits 0 with `{"ok": false, "error": ...}`. (b) Bridge with `execCk` against an unwritable data home: `events(out)` (labels in order), `merged`, landings and outcome equal a run against a writable home, and the progress failure is named exactly once, by the warning line of the test's own `ck progress end` after the run (the skill's step; the workflow result is otherwise equal) | real permissions, real Workflow script | `tests/test_eta_progress.py`, `tests/test_eta_workflow.py` |
| TC-30 | 3.8 | integration (CLI) | `--data` with extra keys `prompt`, `transcript`, `contents` holding `CONFIDENTIAL` values: none of the values appear in the progress file bytes. After TC-25, no prompt substring longer than 40 characters appears in the file | real file bytes | `tests/test_eta_progress.py` |
| TC-31 | 3.9 | integration (CLI) | Begin, end, begin again: there are two files, the first is byte-identical to its state before the second begin and still parses, and `ck eta demo --json`'s `progressFile` is the second. Begin, then begin again with no end: the first file gains exactly one `end` event with outcome `superseded`, and both files remain | real files | `tests/test_eta_progress.py` |
| TC-94 | 3.10 | integration (CLI) | Begin at T0, task-start 1 at T0+5. At T0+124 (119 min after the last event): `state` is not `stale`. At T0+126: `state: "stale"`, `finish: null`, `staleSince` = T0+5, the text shows `stale: no progress since 15:05`, the status line prints no line for the run, and a notify script set with `set_notify` receives nothing. An event at T0+130 ends the stale state | real progress file, real status line, real notify script | `tests/test_eta_live.py` |
| TC-102 | 3.10, 5.1, 5.4 (stale wins, D16) | integration (CLI) | Three runs, each with no event after T0+20: (a) approval-start at T0+20; (b) a `hand-landing` for task 2 at T0+20; (c) task 1 parked at T0+20 on a chain 1->2. At T0+139 (119 min of silence): (a) and (b) `state: "waiting"`, `waiting on you 119 min`; (c) `state: "blocked"`. At T0+141: all three `state: "stale"`, `finish: null`, `waitingOnYouMinutes: null`, `blocked: null`, `staleSince` = T0+20, the text shows `stale: no progress since 15:20` and no `waiting on you` or `blocked:` line, and the status line prints no line for them. An `approval-end` (a) or `unpark` (c) at T0+150 ends the stale state | real progress files, real status line | `tests/test_eta_live.py` |

### Story 4: `ck eta`

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-32 | 4.2, 4.3 | integration (CLI) | Given start at T0. Task-start 1 and 2 at T0. Merge 1 and landing [1] at T0+6. Task-start 3 at T0+6. Retry 2 at T0+10. When `ck eta demo` runs at T0+12, then it prints `12 min since launch`, the last event time (`15:10`), `merged 1, running 2, waiting N`, the finish range, `+X min vs launch` (sign and value equal `--json` p50 minus the start event's launch p50), the latest-change reason `task 2 retry 1`, the remaining critical path, the running tasks (2, 3) with step, model and minutes so far (10 and 6), and the waiting tasks with what each waits on. `--json` `finish.p10 >= now` | real progress file, real store | `tests/test_eta_live.py` |
| TC-33 | 4.3 | integration (CLI) | A running task that has run 60 min (history p90 about 17): `finish.p10 >= now`, `remainingMinutes.p10 > 0` | real file | `tests/test_eta_live.py` |
| TC-34 | 4.4, 4.8 | integration (subprocess, real time) | `ck eta demo --watch` runs as a subprocess (no `CK_NOW`). Appending an event makes a new frame that reflects it appear within 5 s. Appending end makes the process print the final line (TC-38) and exit 0 within 10 s. A second watcher sent SIGINT exits 0 | real subprocess, real file changes, real signal | `tests/test_eta_live.py` |
| TC-35 | 4.5 | integration (CLI) | Parametrized over event sets that produce each state: not-started, on-track, slipping, blocked, waiting, stale, done. `--json` has `schema` matching `ETA_SCHEMA_RE`, `kind`, `spec`, `state` (the expected one), `launchedAt` and `finish.p10/p50/p90` as ISO-8601 or null, `remainingMinutes.p10/p50/p90`, `cost` (`{p10, p90}` or null), `confidence`, `basis`, `criticalPath`, `tasks` or `phases`, and `blockedReason` / `waitingMinutes` non-null exactly in blocked / waiting | real CLI | `tests/test_eta_live.py` |
| TC-36 | 4.6 | integration (CLI) | Run `ck eta --json` twice without a `--seed` and with an unchanged progress file: the second has `cached: true` and is otherwise identical. After one more event is appended: `cached: false` | real cache file | `tests/test_eta_live.py` |
| TC-37 | 4.7 | integration (CLI) | No `specs/ghost/`: an error naming `ghost`, non-zero exit. A progress file chmod 000 (`needs_user`): the launch estimate, the text `live progress unavailable`, exit 0 | real permissions | `tests/test_eta_live.py` |
| TC-38 | 4.1, 4.8 | integration (CLI) | End (outcome `completed`) at T0+70 with 6 merged: the output shows the outcome, the end time `16:10`, `70 min`, `6 merged`, the cost (from history) and the place against the launch range (`inside p10-p90`, `after p90` or `before p10`, chosen to match the start event's launch range). `state: "done"`, and no `not started` line. With every task marked Done in tasks.md it also prints `nothing left to run` (`ended.nothingLeft` true). With two tasks still open it prints their launch estimate (`ended.next` non-null, `next.open == 2`) | real file | `tests/test_eta_live.py` |

### Story 5: blocked and waiting-on-me

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-39 | 5.1 | integration (CLI) | Chain 1->2->3, task 1 parked (park event). Then the text shows `blocked: task 1 parked (needs input)` naming tasks 2 and 3 waiting on it, no finish range, and the remaining work once unblocked (p50). `--json`: `state: "blocked"`, `finish: null`, `runnableDone: null`, `remainingMinutes` p10/p50/p90 non-null | real file | `tests/test_eta_blocked.py` |
| TC-40 | 5.1, 5.2 | integration (CLI) | Tasks 1->2 and 3->4 (independent), task 3 parked. Then `state: "blocked"`, `finish: null`, the reason `task 3 parked (needs input)`, `waitingOnYou` names 3 and 4, and `remainingMinutes` counts every open task: its p50 is >= the remaining p50 (same seed) of a spec with only tasks 1 and 2. `runnableDone` is non-null and equals (same seed) the finish p50 of a spec without tasks 3 and 4; the text shows it only as `runnable work done ~HH:MM`, never on the `finish` line | real file, real tasks.md | `tests/test_eta_blocked.py` |
| TC-41 | 5.3, 5.4 | integration (CLI) | (a) Task 1 failed (a `task-failed` event) with dependents 2 and 3 open and nothing else runnable: `blocked:` naming 1 and holding 2 and 3, remaining once unblocked shown. (b) A `hand-landing` event for task 2 at T0+20: at T0+27 `state: "waiting"`, `waiting on you 7 min`, not blocked; a `merge` of task 2 at T0+30 closes the wait. (c) A failed task with no dependents: not blocked | real file | `tests/test_eta_blocked.py` |
| TC-42 | 5.4 | integration (CLI) | Approval-start at T0+20. At T0+27 and at T0+40: `waiting on you 7 min` / `20 min`, and `remainingMinutes` is identical in both (`--seed 4`). Two variants: (a) the approval-start written through `ck progress event demo approval-start --reason question`, which runs `after_event` and writes the cache; (b) written with `append_events`, so no cache exists when `ck eta` first runs | real file | `tests/test_eta_blocked.py` |
| TC-43 | 5.5 | integration (CLI) | Task 1 runs 3 min of its own work around a 20-min approval wait (T0+2 to T0+22, merge at T0+23): with `--seed 4`, `finish` = the finish of the same events without the wait, with every later event and `CK_NOW` 20 min earlier, + 20 min; and `state` is not `slipping` even though p50 is now past the launch p90 by only the waited minutes | real file | `tests/test_eta_blocked.py` |
| TC-44 | 5.6 | integration (CLI) | Park then unpark of task 1 at T0+30 (and, separately, a failed task fixed and merged): `state` is back to `on-track` or `slipping`, `finish.p10 >= T0+30` | real file | `tests/test_eta_blocked.py` |
| TC-45 | 5.7 | integration (CLI) | Open tasks at a `CK_NOW` past the launch p90: `slipping` with the new time and its cause (the latest-change reason). Inside the range: `on-track`, and the text has the time and range but no slip wording | real file | `tests/test_eta_blocked.py` |
| TC-103 | 5.4, 4.2, 8.1, 8.5 (hand landing freezes the whole run, D17) | integration (CLI) | `set_notify(notify_script ok)`, tasks 1-4 independent. Through the real writers: `ck worktree land demo 2 --progress-run K --json` with task 2 PROTECTED at T0+20 (appends `hand-landing`), then `ck worktree land demo 3` at T0+25 and `ck worktree land demo 4` at P+20 (past launch p90 + the 15-min threshold), both merged. Each later land JSON `eta` is `landed 3 · waiting on you 5 min` / `landed 4 · waiting on you N min` (no ETA, no `(+N)`); `ck eta demo --json --seed 4` at T0+25 and at P+20 shows `state: "waiting"` and identical `remainingMinutes`; the notify script receives no slip message. After `ck progress end` the wait is closed (`ended` set) and one done message is sent | real land CLI, real progress file, real notify | `tests/test_eta_writers.py` |

### Story 7: cold start

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-46 | 7.1 | integration (CLI) | Empty store: an estimate with `low confidence: no local history`; `--json` `confidence: "low"`, `basis.source: "bundled"`. Same for a create spec | real empty store, packaged defaults | `tests/test_eta_coldstart.py` |
| TC-47 | 7.2, NFR privacy | static + seeded | The packaged defaults file parses, its keys are only the aggregate families of 7.2, and its leaves are only numbers (plus the fixed key vocabulary). It has no string matching run/agent/session id patterns (`wf_`, `ag_`, uuid, 12+ hex), no ISO timestamp, no `/`, and none of this repository's spec directory names or task titles (near-miss negatives read from `specs/*/tasks.md`). Running `DEFAULTS_CMD` over a store seeded with `CONFIDENTIAL` values writes a file that contains zero of them | real package resource, real repository data | `tests/test_eta_coldstart.py` |
| TC-48 | 7.3 | integration (CLI) | A store with many tasks but no review/landing waits: `basis.bundledParts` contains the wait families and not task durations, and the text names the parts that came from bundled defaults | real store | `tests/test_eta_coldstart.py` |
| TC-49 | 7.4 | integration (CLI) | Empty store with `CK_ETA_DEFAULTS` set to a missing path, then to a file holding `{bad`: the critical path and task count, `no estimate: no history and no bundled defaults`, exit 0 | real env, real files | `tests/test_eta_coldstart.py` |
| TC-50 | 7.5 | integration (CLI) | `DEFAULTS_CMD --out D` over `POOL_IMPL+POOL_CREATE` writes D with 7.2's families only. `ck eta` with `CK_ETA_DEFAULTS=D` and an empty store gives `basis.source: "bundled"` and finite quantiles. Over a store below the minimum run count it exits non-zero and D does not exist | real CLI | `tests/test_eta_coldstart.py` |

### Story 8: log lines, status line, notifications

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-51 | 8.1 (Workflow) | workflow (bridge, execCk) | spec-implement.js run with `etaLine` from the test's `ck progress begin --json` and landings of {1,2} and {3}: `logs` has exactly one start line equal to `etaLine` (matching `^ETA \d\d:\d\d \(p90 \d\d:\d\d\), critical \d+(→\d+)*$`) before any task agent ends, and after each landing exactly one line equal to that land answer's `eta` (matching `^landed [\d,]+ · ETA \d\d:\d\d( \([+-]\d+\))?$`). Create op: the start line names the phase count, and there is one ETA line after each phase end, equal to that phase's `step-end --json` `eta` | real script, real `ck progress` / land `eta` | `tests/test_eta_workflow.py` |
| TC-52 | 8.1 (agent mode) | static | Packaged and vendored `without-workflow-tool.md` tell the orchestrator to show `ck progress begin --json`'s `etaLine` at start, the land JSON's `eta` after each landing (implement), and `ck progress event ... step-end --json`'s `eta` after each phase end (create), or `ETA unavailable` when it is null | real documents | `tests/test_eta_agent_docs.py` |
| TC-53 | 8.1 (CLI format) | integration (CLI) | (a) `ck progress begin demo --kind implement --json` `etaLine` matches the start form, and `ck progress event orbit step-end --phase <title> --json` `eta` matches `<what> · ETA HH:MM`. (b) A landing through `ck worktree land demo 4 9 --progress-run K --json` that moves the median by +3 returns `eta` `landed 4,9 · ETA HH:MM (+3)`; with no change, no parenthesis | real CLI | (a) `tests/test_eta_surfaces.py`, (b) `tests/test_eta_writers.py` |
| TC-54 | 8.2, NFR never slows | integration (CLI) + workflow | `CK_ETA_LINE_TIMEOUT_S=0.001`, or an unreadable model snapshot: `ck progress event ... step-end --json` gives `eta: null`, exit 0, and `ck progress begin --json` gives `etaLine: null`. Bridge with `etaAnswer` = `null`, `number` and `missing`: each ETA log line is `ETA unavailable`, and `events(out)`, merges and result equal a normal run's | real CLI, real script | `tests/test_eta_surfaces.py`, `tests/test_eta_workflow.py` |
| TC-55 | 8.3 | integration (CLI) | Given in project A an active implement run (3/6 merged), an active create run in phase `Design + Test Plan`, a blocked run, a waiting run (approval open 7 min) and an ended run, plus an active run in project B, each with a cached estimate from `ck eta`. Also a stale run (last event 3 h old) in project A. When `ck-status-line` gets Claude Code's status JSON on stdin (`workspace.current_dir` = A), then it prints exactly 4 lines: `ck demo 3/6 ETA HH:MM ~$X` (X the cached `cost.p50` rounded; a run with unknown cost has no `~$` part), `ck orbit create Design + Test Plan ETA HH:MM`, `ck <s> ... blocked`, `ck <s> ... waiting on you 7 min`. The output is unchanged after the store's `runs/` is chmod 000 and `CK_ETA_DEFAULTS` points at a missing file (it never simulates) | real CLI, stdin JSON, real files | `tests/test_eta_surfaces.py` |
| TC-56 | 8.4 | integration (CLI) | No active run: empty stdout, exit 0. One unreadable progress file among two: only the other run's line, exit 0. Garbage on stdin: exit 0, and no line contains `error`, `Traceback` or `❌` | real files | `tests/test_eta_surfaces.py` |
| TC-57 | 8.5, 4.4 | integration (CLI, real-time watcher) | `set_notify(notify_script ok)`. Start at T0 (launch p90 = P from the start event). (a) Event-driven: `ck progress event` calls at times P+10, P+16, P+20, P+31, P+47. Exactly one slip message at P+16 (naming `demo`, the new finish time with its change and the cause), none at P+20, the second at P+31 and the third at P+47. With `ck agents set run.eta_slip_minutes 5` the first comes at P+6. A run with a 30-min approval wait inside it sends no slip message for those minutes. (b) Time alone, during a running task (rewritten for D14): task-start 1 at T0+2 and no later event. With no watcher, `ck eta demo` at P+16 (no `--watch`) sends nothing. Then `ck eta demo --watch` runs as a subprocess with `CK_NOW=P+16` and `RECHECK_ENV=1`: within 5 s exactly one slip message arrives, naming `demo` and the cause `task 1 running`, and after 3 more rechecks there is still exactly one. The same with an approval open since P+5: no message. Without a watcher, the next `ck progress event` at P+16 sends the slip (the event-driven path). The `ck run` watcher trigger is TC-97 | real notify subprocess, real config, real `--watch` subprocess | `tests/test_eta_notify.py` |
| TC-58 | 8.6, 8.9 | integration (CLI) | A failed task blocking dependents: one message naming `demo` and the reason. The same block observed again: no second message. A park of the only path (5.1) through `ck worktree park`: only prompt-free-runs' park message (1 total; changed by D15 from park + blocked). A park of one branch (5.2): only the park message (1 total) | real park CLI, real notify | `tests/test_eta_writers.py` |
| TC-59 | 8.7 | integration (CLI) | End of implement: one message naming `demo`, the end time, `6/6`, the wall minutes and `$` cost. End of create: `spec written` instead of the count. Superseded (added by D18): begin at T0 with 2 of 6 merged by T0+30, then a second `ck progress begin demo` at T0+40 with no end: exactly one message for the first run, naming `demo`, `superseded`, the end time `15:40`, `2/6` and `40 min`, and no `$` part when the cost is unknown. The same when the first run was stale (last event 3 h before the second begin). The second run's own `ck progress end` sends its own single done message | real notify | `tests/test_eta_notify.py` |
| TC-101 | 8.11 (D13) | integration (CLI) | `set_notify(typed_notify_script(...))`. A slip (TC-57a timeline), a failed-task block (`ck progress event demo task-failed --task 1` with dependents 2 and 3 open and nothing else runnable) and an end each deliver one notice, with `CK_NOTIFY_TYPE` `ck_eta_slip`, `ck_eta_blocked` and `ck_run_done` respectively; the superseded done of TC-59 is `ck_run_done` too. A park through `ck worktree park` delivers prompt-free-runs' park message with `ck_needs_input`, so its behaviour is unchanged. The stdin JSON keys are the same for every type | real notify subprocess, real env | `tests/test_eta_notify.py` |
| TC-60 | 8.8 | integration (CLI) + workflow | No notify command: none sent, all exits 0. A `fail` script: `ck progress end` exits 0 and prints the one-line warning `⚠ notification failed: <detail>`. A `slow` script (sleeps 30 s): returns within 13 s (10 s cap). Bridge with a failing command: the test's own `ck progress end` names the failure once, and merges and outcome are unchanged | real subprocess, 10 s cap | `tests/test_eta_notify.py`, `tests/test_eta_workflow.py` |
| TC-61 | 8.10 | integration (CLI) | Across every message in TC-57..TC-60, with task titles, park-note `test`/`requirement` fields and spec text seeded with `CONFIDENTIAL` values and quoted test names: no message contains `/`, `test_`, any seeded value or any prompt text | real messages | `tests/test_eta_notify.py` |

### Stories 9-11: agent-mode analytics hardening (D7)

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-62 | 9.1 | integration (CLI) | `backfill_home(env, 2, 3)` with an existing store holding one record. Parametrized: `runs/` chmod 000 (`needs_user`), `runs` replaced by a regular file, and the data directory itself chmod 000 (`needs_user`) so `runs/` cannot be stat'ed. Then `ck stats collect --agents-backfill` exits non-zero, the error names the store path and the read failure, and after the permissions are restored the store's files are byte-identical to before (no record written) | real permissions, real store | `tests/test_stats_followups.py` |
| TC-63 | 9.2 | integration (CLI) | The same, with `--dry-run` (text and `--json`): the planned groups are listed, overlaps are marked as not checked, exit non-zero | real CLI | `tests/test_stats_followups.py` |
| TC-64 | 9.3 | integration (CLI) | The TC-60 scenario of `test_stats_agents_backfill.py` (a narrower gap after a default backfill), plus one malformed file in `runs/` (parametrized: invalid JSON, a JSON list, wrong schema, empty, truncated). Then backfill exits 1, names the malformed file (JSON `malformedRecords`), and the store's files are byte-identical to before (no record written). With `--dry-run` the planned groups are listed with `overlaps not checked`, the file is named, exit 1. Without the malformed file the same scenario exits 0 as today | real store | `tests/test_stats_followups.py` |
| TC-65 | 9.4 | integration (CLI) | A backfill group whose session transcript `<sid>.jsonl` is deleted, or chmod 000: the group's record is written with `outcome: null` and an entry `{"what": "outcome"}` in `missing` | real files | `tests/test_stats_followups.py` |
| TC-66 | 10.1 | integration (CLI) | An open agent-mode run with marked agents and `projects/` chmod 000 (sandbox). `ck stats collect --agents <id> --json`: stdout is exactly one line, which parses as `{"run_id": <id>, "result": "failed", "error": <contains "sandbox">}`, exit 1, and stdout holds no other text | real permissions | `tests/test_stats_followups.py` |
| TC-67 | 10.2 | integration (CLI) | `--auto` with `bogus` and with unknown `ag_0a1b2c3d`: collect.log gains exactly one line naming the id and the reason, stdout is the retry hint, exit 0 | real collect.log | `tests/test_stats_followups.py` |
| TC-68 | 10.3 | integration (CLI) | `--agents bogus` without `--auto`: error text, exit 1. With `--json`: one JSON error object, exit 1 | real CLI | `tests/test_stats_followups.py` |
| TC-69 | 11.1 | integration (CLI) | A fake home with one completed workflow run, and an open-manifests directory that cannot be listed (`runs-open` chmod 000, `needs_user`). `ck stats collect --all`: the text names the sweep failure and its reason, the workflow run is reported as collected, exit 1. `--json`: `failures` has the sweep entry, `failed` >= 1 and `collected` == 1 | real permissions | `tests/test_stats_followups.py` |
| TC-70 | 11.2 (regression guard) | integration (CLI) | One workflow run and one agent-mode run (begin, marked agents, manifest backdated 7 h). `collect --all`: the workflow record is written with no `source: "agents"`, the agent-mode record has `source: "agents"` and its manifest has moved to `runs-closed` | real store | `tests/test_stats_followups.py` |
| TC-71 | 11.3 | integration (subprocess, audit hook) | With `PYTHONPATH=tests/openaudit` and `CK_TEST_OPEN_LOG`, `ck stats collect --agents <id>` of a 6-agent run: each agent transcript's open count equals its count in collecting a workflow run of the same agents (which does no transcript-based ordering). Fails today (agent mode opens each transcript once more) | real subprocess, Python audit events | `tests/test_stats_followups.py` |
| TC-72 | 11.4 | integration (CLI) | Collect one workflow run and one agent-mode run into store A and again into a fresh store B: every record file is byte-identical. Re-collecting into A gives `unchanged`. The existing files `tests/test_stats_*.py`, `test_stats_doctor.py` and `test_doctor_xdist.py` pass unchanged | real store | `tests/test_stats_followups.py` + existing files |

### Story 12: deterministic P-10

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-73 | 12.1 | workflow (bridge) | `bridge.run(..., clock="virtual")` for one task with duration 5, review 0, merge 2: `end.t - start.t == 5` exactly for the task agent, and two runs give identical traces **including** `t` | real script, virtual timers | `tests/test_scheduler_virtual_clock.py` |
| TC-74 | 12.2 | property (bridge) | P-10 (`test_p10_finish_first_changes_nothing_when_nothing_queues`) is rewritten: one on-run and one off-run per configuration on the virtual clock, with `events(on) == events(off)` and `on.result == off.result`. A source check (`inspect.getsource`) finds exactly two `run(` calls and no loop | real script | `tests/test_landing_scheduler_properties.py` |
| TC-75 | 12.3 | static + workflow | The clock hook (`clock` option, virtual queue) appears only under `tests/js/`: neither `plugin/workflows/*.js` nor `.claude/workflows/*.js` contains the code tokens `VirtualClock`, `clock.now(`, `clock:`, `setTimeout(` or `performance.now(` (prose such as "no clock" in prompts and comments is allowed). `spec_implement.test.mjs`, `landing_speed.test.mjs` and `finish_first.test.mjs` (real timers) pass unchanged | real files | `tests/test_scheduler_virtual_clock.py`, `tests/test_workflow_scripts.py` |
| TC-76 | 12.4 | workflow (bridge, load) | Under `busy(os.cpu_count())`, 20 P-10 scenarios on the virtual clock give traces identical to the same scenarios on an idle machine (recorded first). Opt-in stress (`CK_STRESS_P10=1`): 50 consecutive runs of `pytest -q -n auto tests/test_landing_scheduler_properties.py` under `busy(os.cpu_count())`, all green | real CPU load | `tests/test_scheduler_virtual_clock.py` |

### Story 13: Verify-cycle lint

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-77 | 13.1, 13.2 | integration (CLI) | X=8 impl (Verify `uv run pytest -q tests/test_perf.py`), T=9 test (Files `tests/test_perf.py`, Verifies Task 13), Y=13 depends on 8. Then `ck lint sim` exits 1 and the text names Task 8, `tests/test_perf.py`, Task 9, Task 13, the chain `13 -> 8` and a suggestion to narrow Task 8's Verify. The same holds with Y's `**Verified by:** Task 9` instead of T's Verifies. With the chain 13 -> 11 -> 8, the chain shows `13 -> 11 -> 8`. The test file need not exist on disk | real tasks.md, real CLI | `tests/test_lint_verify_files.py` |
| TC-78 | 13.3 | integration (CLI) + workflow | `ck plan sim --json` `verifyCycles` holds an entry with `task: "8"`, and 8 is not in `ready`. The bridge run with that entry from the start (`verifyCyclesFromStart`) logs `Refusing to launch` and starts no task agent. The packaged and vendored implement skill and `without-workflow-tool.md` still tell the orchestrator to stop on a `ck lint` cycle, name the verify-file error, and tell a single-task run `/spec:implement <spec> X` to refuse when a `verify-file` entry has `task` X | real `ck plan`, real script, real documents | `tests/test_lint_verify_files.py` (CLI, bridge), `tests/test_eta_agent_docs.py` (documents) |
| TC-79 | 13.4 | integration (CLI) | Negatives, each exiting 0 with no new entry: F in no test task's Files; T verifies Y but Y does not depend on X; T verifies only X itself (Y = X); F in an impl task's Files only | real CLI | `tests/test_lint_verify_files.py` |
| TC-80 | 13.5 | integration (CLI) | TC-77 with Task 8 `**Status:** Done`: no new entry for 8, exit 0 | real CLI | `tests/test_lint_verify_files.py` |
| TC-81 | 13.6, 13.10 | integration (CLI) | Table: Verify `./tests/test_perf.py`; `pytest -q -n auto tests/a.py tests/test_perf.py -x`; F only in a second backticked command -> each matched (entry). Narrowed forms (changed by D12, which made them exempt; they were "matched" before): `tests/test_perf.py::test_a`, `python -m pytest tests/test_perf.py::C::test_b`, `pytest -q tests/test_perf.py -k slow`, `pytest -kslow tests/test_perf.py` -> no entry. A narrowed `F::test_a` in one command plus a bare F in another backticked command -> entry. `tests/test_*.py` (glob) and `tests/` (directory) -> not matched | real CLI | `tests/test_lint_verify_files.py` |
| TC-82 | 13.7 | integration (CLI) | TC-77 plus a cycle 11<->12 and a dependency on unknown Task 99: no traceback, exit 1, `cycles` still lists 11/12, the unknown-task error is still reported, and the 8/13 entry is still reported | real CLI | `tests/test_lint_verify_files.py` |
| TC-83 | 13.8 | integration (CLI) | `ck lint sim --json`: the new entry has `task`, `file`, `testTask`, `needs`, `chain`, `message`. On the `market` fixture of `test_lint.py`, the import-based entry is exactly `{task, verifiedBy, needs, module, message}` with the same values as today | real CLI | `tests/test_lint_verify_files.py` |
| TC-84 | NFR lint precision, 13.9, 13.10 | integration (in-process `lint_spec`) | Rewritten for D12. Copy every `specs/*/tasks.md` of this repository into a temp project with the same paths, and reset every task's `**Status:**` to `Not Started` (so the Done exemption, 13.5, hides nothing). For each copy: no entry with a `file` key. This explicitly includes landing-speed tasks 9 and 17, whose gates passed: asserted by name, so a failure names them. The fixture `tests/fixtures/lint/agent-mode-analytics-pre-fix/tasks.md`, statuses reset the same way, is the only copy that fails: every new entry has X=8, Y=13, F=`tests/test_stats_perf.py`, T in {9, 10}, and there is exactly one distinct (X, Y) pair | real repository data | `tests/test_lint_verify_files.py` |
| TC-99 | 13.9, 13.10 (D12) | integration (CLI) | Starting from TC-77 (X=8, T=9 owns `tests/test_perf.py` and verifies 13, 13 depends on 8): (a) T also verifies X, as `**Verifies:** Task 8, Task 13` on T, and separately as `**Verified by:** Task 9` on X: no entry, exit 0; a second test task T2=10 owning the same file and verifying only 13 still gives one entry (X=8, T=10). (b) X's Verify narrowed: `tests/test_perf.py::test_fast` or `-k fast`: no entry; one more backticked command naming bare `tests/test_perf.py`: entry. (c) The agent-mode-analytics task 8 shape (X with `**Verified by:** None`, bare F): entry, exit 1 | real tasks.md, real CLI | `tests/test_lint_verify_files.py` |
| TC-100 | 13.1, 13.9, Assumptions (known import-plus-ownership cycle) | integration (CLI) | The prompt-free-runs 11/3/15 shape, with invented names: X=11 impl, Verify `uv run pytest -q tests/test_guardx.py`, Verified by Task 3; T=3 test owning `tests/test_guardx.py`, Verifies Task 11; Y=15 impl owning `src/demo/share.py`, depending on 11. (a) At launch, `tests/test_guardx.py` absent: no `verify-file` entry (13.9) and no import-based entry, so `ck lint sim` exits 0 (the documented launch gap). (b) Once `tests/test_guardx.py` exists and imports `src/demo/share.py` through an unowned helper `tests/guardx_helpers.py`: exactly one import-based entry `{task: "11", verifiedBy: "3", needs: "15", module: "src/demo/share.py", message}`, no `verify-file` entry, exit 1, and `ck plan sim --json` keeps 11 out of `ready` | real tasks.md, real files, real CLI | `tests/test_lint_verify_files.py` |

### Non-functional and end-to-end

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-85 | NFR simulation speed | perf | A 50-task graph, 14 slots, 500 history tasks (`POOL_IMPL` repeated): `claude_kiro.eta.estimate` in-process median of 3 < 1.0 s, and the create estimate < 0.5 s. `ck eta --json` wall time minus `baseline_startup_s()` < 1.5 s | real seam, real CLI | `tests/test_eta_perf.py` |
| TC-86 | NFR status-line cost, no network | perf | 3 active runs with 1,000-event progress files each and cached estimates, under `no_network`: `ck-status-line` median wall time minus `bare_startup_s()` < 100 ms, and the net log is empty. Running the entry point's module with `python -X importtime` shows neither `click` nor any `claude_kiro.cli` module imported | real entry point, network guard, interpreter import log | `tests/test_eta_perf.py` |
| TC-87 | NFR never slows or breaks a run | workflow (bridge, execCk) | One scenario run twice: (a) everything works; (b) unwritable progress dir, `etaAnswer: null`, notify `fail`. `events(out)`, `merged`, `landings` and `outcome` are equal. Only the failure fields differ | real script, real failures | `tests/test_eta_workflow.py` |
| TC-88 | NFR privacy | integration | The progress file, the ETA cache and their directories: 0600 / 0700 after `ck progress` and `ck eta` create them | real file modes | `tests/test_eta_progress.py` |
| TC-89 | NFR no network | integration | Every test in `test_eta_*.py` and `test_lint_verify_files.py` runs under `no_network` (module `pytestmark`), and `CK_TEST_NET_LOG` is empty at the end | network guard | all new files |
| TC-90 | NFR calibration | integration + opt-in | `BACKTEST_CMD --json` over `POOL_IMPL+POOL_CREATE` exits 0 and prints `{runs, inside, rate}` with 0 <= rate <= 1 and `runs` equal to the pool size. Opt-in `CK_ETA_BACKTEST=1`: over the real local history, it prints the figure and asserts rate >= 0.60 | real CLI | `tests/test_eta_perf.py` |
| TC-91 | NFR backward compatibility | regression | The whole existing suite passes unchanged except P-10 (TC-74) and the `stub()` move in `spec_create.test.mjs` (an import-only change) | full suite | all |
| TC-92 | Critical path 1-2 (smoke) | e2e | Chain of real CLI calls: launch estimate -> start -> events -> landing -> park blocking -> unpark -> approval wait -> end. At each step `ck eta --json` state, `ck statusline` line and notify messages agree with the step (not-started, on-track, blocked, on-track, waiting, done; prompt-free-runs' park message + 1 done message, and no blocked message for the park; changed by D15 from 1 blocked + 1 done) | real CLI end to end | `tests/test_eta_e2e.py` |
| TC-93 | Critical path 3 (smoke) | e2e | Create run: `ck eta orbit` before start, begin, phase events, `step-end --json` `eta` after each phase end, end -> `spec written` notification and `state: done` | real CLI end to end | `tests/test_eta_e2e.py` |
| TC-95 | 3.8 (spool exception), 3.5 | integration (`ck run`) | `ck run demo --command implement --unsandboxed` with a test-owned fake `claude` that runs the real `ck progress begin|event|end`. The child's `CK_PROGRESS_DIR` is `<worktreeDir>/.ck-progress` (0700, files 0600), `git check-ignore` reports the spool path ignored and `git status --porcelain` lists nothing under it, every spool line appears exactly once in the data-dir progress file within 5 s of being written (polled while the child sleeps), and the spool file and its sidecars are gone after `ck run` returns | real `ck run`, real child, real git | `tests/test_eta_run_spool.py` |
| TC-96 | 3.1, 3.9, 7.1 (prepared snapshot) | integration (`ck run`) | Same setup, with the record store holding `POOL_IMPL` and the child's env pointing `CK_DATA_HOME` at an unreadable directory (as the sandbox would): the child's `ck progress begin --json` gives `estimate.basis.source` `local` (not `bundled`), and after the run the data-dir progress directory holds `<key>.model.json` and `<key>.eta.json` copied from the spool. An unfinished data-dir run of the spec begun before `ck run` gains exactly one `end` with outcome `superseded` (3.9), and the parent sends exactly one done message for it naming `superseded` (8.7, added by D18) | real `ck run`, real files | `tests/test_eta_run_spool.py` |
| TC-97 | 8.5, 8.6, 8.7, 8.8, 8.9, 8.11 (`ck run`) | integration (`ck run`) | With `typed_notify_script`: a failed task holding every remaining task (`ck progress event demo task-failed` in the child) gives exactly one blocked message, sent by the parent with `CK_NOTIFY_TYPE=ck_eta_blocked` and recorded as `notified`; the done message is sent once, with `ck_run_done`. A whole-run park in the child (changed by D15, which used to expect a blocked message here) gives no blocked message. Time-only slip through the `ck run` watcher (added by D14): the fake child runs `begin` and `task-start 1` with its own `CK_NOW=T0`, then sleeps with no event, while `ck run` itself runs with `CK_NOW=P+16` and `RECHECK_ENV=1`: exactly one slip message (`ck_eta_slip`) arrives within 5 s, and the child's later `ck progress event` at P+16 does not send a second one. A `fail` notify script, or a spool made unreadable mid-run, leaves `ck run`'s exit code and outcome equal to a run without progress, and the failure is recorded in the progress file or its `.errors` sidecar. Mirrored lines hold no `CONFIDENTIAL` value passed in the child's flags | real `ck run`, real notify script | `tests/test_eta_run_spool.py` |
| TC-98 | 3.1, 5.4, 8.1, 8.7 (Workflow mode skill steps) | static | The packaged `plugin/skills/implement/SKILL.md` and `plugin/skills/create/SKILL.md` and their vendored renderings (`claude_kiro.plugin.vendor_files()`) hold: a backticked `ck progress begin <spec> --kind implement` (create: `--kind create`) `--mode workflow --json` before the Workflow call; `progressRun` and `etaLine` passed in the workflow args; `ck progress event <spec> approval-start --reason question` and `approval-end` around the AskUserQuestion step; and `ck progress end <spec> --outcome` after `ck stats collect`, with the instruction to quote its warning line | real documents | `tests/test_eta_agent_docs.py` |

## Properties

Library: **Hypothesis** (Python). Settings for pure-seam properties: `@settings(derandomize=True, max_examples=200,
database=None, deadline=None)`. Properties that run the simulator use `max_examples=50` with the same flags, and every
seam call passes a fixed `seed`. Bridge properties use the existing `SETTINGS = settings(derandomize=True,
max_examples=60, database=None, deadline=None)`. History inputs are drawn as subsets (`st.sets` of indices) of the
records the real collector wrote for the pools, never hand-shaped records. Run inputs (`run` dicts, H-10) are
generated by the strategies below: `graphs()` reuses `landing_bridge.dags()` with cycles and dangling ids added,
`events()` draws progress-event sequences consistent with a graph, and `times()` draws minute offsets in [0, 600].

The ids P-1.. are local to this spec. The scheduler test keeps its historical name `test_p10_...` (TC-74).

| ID | Requirement | Property | Generator domain | Expected decision |
|---|---|---|---|---|
| P-1 | 1.5 | For any run, history and seed, `estimate` returns equal objects on two calls; permuting the history list does not change the output | `graphs()` 1-30 tasks with track/model/risk/size; history subsets of `POOL_IMPL`/`POOL_COSTLESS`; seeds 0-2^31; permutations | identical dicts |
| P-2 | 1.1, 2.1, 4.5 | For any estimate that is not blocked: finish p10 <= p50 <= p90, remaining p10 <= p50 <= p90, all remaining >= 0, and when cost is non-null, cost p10 <= p90 and both > 0 | P-1 domain plus `events()` for in-progress runs; create runs from `POOL_CREATE` subsets | ordering holds |
| P-3 | 4.3, 2.4 | For any in-progress run where task or phase k has run e minutes (e in 0..600, including e far above history p90), finish p10 >= now and every running item's simulated remainder >= 0 | `events()` with running tasks/phases at drawn elapsed times; `times()` | no simulated finish earlier than now |
| P-4 | 1.1 (open only) | For any graph, marking a set D of tasks Done gives an estimate whose `tasks`, `criticalPath` and open count contain no member of D (complement: every open task appears exactly once in `tasks`) | `graphs()` x `subsets()` of ids for D, including all and none | D absent; open tasks each present once; all Done -> "nothing left" |
| P-5 | 1.7, 5.3, 5.4 | For any graph with a cycle or a dependency on an unknown id, state is `blocked`, `finish` is null and `blockedReason` names every task on one cycle (or the unknown id and its dependent). Complement: for any acyclic graph with no dangling ids, no park and no failure, state is never `blocked`; a `hand-landing` event never makes it `blocked` | `graphs()` with injected back edges, self-loops, dangling ids (multi-digit, non-contiguous), duplicated deps | blocked iff a structural block exists |
| P-6 | 5.1, 5.2 | For any acyclic graph and parked task p: the hold is `blocked`, its reason names p as `parked (needs input)`, `held` is exactly p and its dependents, and the remaining work counts every open task. `whole_run` is True iff no open task outside `held` can still run. (The view-level consequences, `finish` null, `runnableDone` and remaining work over every open task, are checked by TC-39/TC-40.) | `graphs()` x parked id drawn from open tasks; chains, fans, diamonds, singletons | as stated, both branches |
| P-7 | 5.4 | For any open approval wait (question, or `hand-landing`) that began at wall minute w, and any two times t1 < t2 after w: `effective_now` gives the same `now_eff` at t1 and t2 (so the simulator input, and with the same seed the remaining work, cannot differ; TC-42 checks the view), and the waiting minutes equal t - w (floored). This is a seam-level property: at the view level a run with no event for more than 120 minutes is `stale` instead (3.10 wins, D16, TC-102) | `events()` + approval-start or hand-landing at drawn w; t1, t2 from `times()` | frozen estimate, exact wait minutes |
| P-8 | 5.5, 5.7, 8.5 | Metamorphic: inserting a closed approval wait of W minutes (start and end) at an insertion point, shifting every later event and `now` by W, shifts finish p10/p50/p90 by exactly W (same explicit seed) and never moves the state from `on-track` to `slipping`, nor adds a slip notification | `events()` x W in 1..240 x insertion point, `now` within 120 min of the last event (no stale run, 3.10) | shift by W; state and slip set unchanged |
| P-9 | 6.6, 6.5 | For any history where durations rise with size (subsets of `POOL_IMPL` with >= the minimum sized tasks) and any two tasks equal in track/model/risk with size(a) < size(b): median(a) <= median(b). A task with size 0 has median > 0 | `POOL_IMPL` subsets; size pairs including (0,0), (0,1), (1,20), equal sizes | monotone; never zero |
| P-10 | 1.9 | For any history in which no matched task has a cost (subsets of `POOL_COSTLESS`), `cost` is null, never 0. Complement: if any matched record has a cost, `cost.p10 > 0` | `POOL_COSTLESS` subsets, mixed with 0..k `POOL_IMPL` records | null iff unknown |
| P-11 | 1.3, 7.3 | Metamorphic: removing records from the history never makes `basis.match` narrower (order exact < no-model < no-risk < no-track < bundled), and `basis.bundledParts` only grows | nested subset pairs H' ⊂ H of `POOL_IMPL` | monotone widening |
| P-12 | 2.2 | For any create history, the inclusion rate of an optional phase in the simulation equals its rate in history within ±0.1 at n >= 500 simulations; rate 0 never includes it, rate 1 always does | `POOL_CREATE` subsets with the second pass in 0..all runs | rates match; ends exact |
| P-13 | 2.5 | Adding any number of create records without per-phase timing leaves every phase median unchanged, and no phase median is 0 | `POOL_CREATE` timed subsets + 0..2 untimed records | unchanged |
| P-14 | 3.6 | For any sequence of valid events interleaved with malformed lines (truncated JSON at every cut point, non-object JSON, unknown event names, missing `event`, NUL/invalid UTF-8 bytes, empty lines, CRLF endings, very long lines), `read_progress` returns exactly the valid known events in file order | `events()` x malformed-line strategy x positions | valid events only, in order |
| P-15 | 3.8 | For any event and any `data` mapping whose extra keys (`prompt`, `transcript`, `contents`, `diff`, random keys) hold `CONFIDENTIAL` values or random text, the line `progress_line` builds contains none of those values. It keeps only the H-4 fields for that event | H-4 events x `st.dictionaries` mixing allowed and extra keys; values from `CONFIDENTIAL`, paths, multi-line prompts | extras dropped |
| P-16 | 8.5 | For any timeline of estimates and threshold s: slip notifications fire exactly when (p50 - waited) - launch p90 first exceeds s, 2s, 3s, ... (one per crossed multiple, in time order). Complement: a timeline whose (p50 - waited) never exceeds launch p90 + s fires none | timelines of 1..40 estimates with p50 moves in [-60, +120] min, waits 0..120, s in 1..60 | exact crossing set |
| P-17 | 3.10, 5.4, 8.6, 8.7, 8.9, 8.11 | For any timeline: at most one `done`, and it comes from the last estimate, whatever the end outcome (`superseded` included, D18); one `blocked` per distinct blocked reason, never repeated while the reason persists; no `blocked` for a park hold, whether `whole_run` is True or not (changed by D15, which used to allow one for a whole-run park); no slip or blocked notice while `waiting` (hand landings included, D17) or `stale`; a notice already pending (`notify-pending`) counts as sent; every notice's type is `ck_eta_slip`, `ck_eta_blocked` or `ck_run_done` to match its kind | timelines mixing on-track/slipping/waiting/stale/blocked (park whole-run or not, failure, cycle)/done (completed or superseded), repeated states, pending and sent notices | dedupe holds |
| P-18 | 8.10 | For any timeline whose reasons, spec name and task titles hold paths (`/a/b.py`, `C:\x`), quoted test names (`test_x`), requirement text and `CONFIDENTIAL` values, every notification message contains none of them except the spec name and task numbers | P-17 domain x seeded strings | messages clean |
| P-19 | 7.2 | For any store of records seeded with `CONFIDENTIAL` values (ids, names, paths, timestamps, titles), `bundle_defaults` output (JSON-dumped) contains none of them, holds only numeric leaves under the 7.2 families, and with fewer than the minimum runs raises `ValueError` | `seeded_local_record` variants x subset sizes 0..60 | leak-free; refuse below min |
| P-20 | 13.1, 13.4 | For any generated spec (3..14 tasks, tracks impl/test, test files in test tasks' Files, Verify lines naming 0..3 files, deps acyclic or not, Verifies/Verified by on either side), the set of new entries `(task, file, testTask, needs)` from `lint_spec` equals the brute-force oracle: X impl and not Done, F named in X's Verify at least once without a `::` selector or a `-k` option in that command (13.10), F in T's Files, T test, T does not verify X (13.9), T verifies Y (either form), Y ≠ X, X an ancestor of Y. Complement: no entry outside the oracle set (oracle extended by D12) | strategy writing real tasks.md files in a temp dir (`fresh_env` style); near-miss negatives: F in an impl task's Files, T verifying X as well as Y (either form), F narrowed by `::` or `-k`, Y not depending on X, X Done, F named by a different test task's file with a shared prefix | exact equality |
| P-21 | 13.6, 13.10 | For any matched pair (X, F), rewriting X's Verify into any un-narrowed surface form keeps the entry: `./F`, F among other files and flags (`-q`, `-n auto`, `-x`) in any order, `uv run pytest`, `python -m pytest`, `pytest`, F in the 2nd or 3rd backticked command. Narrowed forms remove it (changed by D12; they used to keep it): `F::test_x`, `F::Class::test_y`, or a `-k expr` / `-kexpr` in the same command, unless another of X's commands names F un-narrowed. Negatives never match: the glob `dir/test_*.py`, the directory `dir/`, `F.bak`, `Fx.py` (F as a prefix), `x/F` (F as a suffix of another path) | surface-form strategy over F drawn from the generated spec's test files, with and without narrowing | match iff the exact path is named un-narrowed at least once |
| P-22 | 13.5 | Marking X Done removes exactly the entries whose `task` is X and leaves every other entry unchanged | P-20 domain x X drawn from the reported tasks | exact removal |
| P-23 | 13.7 | For any spec with cycles (including self-loops) and unknown task references, `lint_spec` returns without raising, `cycles` is non-empty when a cycle exists, and every new entry it reports is in the P-20 oracle computed over the acyclic part | P-20 domain with injected cycles and dangling ids | no crash; sound entries |
| P-24 | 3.2, 3.4 | For any bridge scenario (`scenarios()`) run with `progressRun` set, in prompt order: every task, retry, revise, landfix and resolve prompt's claim command carries `--progress-run <key>` and `--attempt k`, with `--progress-event task-start` on a first attempt, `escalation` exactly when the attempt runs on a model from the role's ladder, and the attempt's model in `--progress-model`; every land prompt's command carries `--progress-run <key>` and names exactly that landing's tasks; and a retry/escalation for a task never comes before its task-start. (Start and end are the skill's steps, checked by TC-25 and TC-98, not by prompt inspection.) Without `progressRun` no prompt carries `--progress-run` | `scenarios()` x slots 1..6, finish-first on/off | valid flags, in order |
| P-25 | 12.1, 12.4 | For any scenario, two virtual-clock runs give identical traces including `t`, and each agent's `end.t - start.t` equals its scripted duration | `scenarios()` | identical, exact durations |
| P-26 | 12.2 | (TC-74) For any scenario and `agentSlots` in {1000, None}, with no runtime `slots`, finish-first on and off give identical `events(out)` and `result` on the virtual clock, in one run each | `scenarios()` x {1000, None} | exactly equal |
| P-27 | 13.9, 13.10 | Metamorphic (D12): for any generated spec and any reported entry (X, F, T, Y), (a) adding X to T's Verifies, or T to X's Verified by, removes exactly the entries with task X and testTask T and leaves every other entry unchanged; (b) narrowing every occurrence of F in X's Verify (a `::test_x` suffix, or `-k expr` in each command naming F) removes exactly the entries with task X and file F; (c) adding one more un-narrowed command naming F restores them | P-20 domain x a reported entry x edit kind (Verifies, Verified by, `::`, `-k`, `-kexpr`) | exact removal and restore |

**Decision seams** the properties call (they run before any CLI task exists): `claude_kiro.eta.estimate`,
`read_progress`, `progress_line`, `decide_notifications`, `bundle_defaults`, and the existing
`claude_kiro.lint.lint_spec`. The cases that show each boundary actually calls them: TC-1/TC-32 (`ck eta` output
equals `estimate` on the same inputs, compared field by field for one fixture), TC-28 (reader), TC-30 (line builder),
TC-57 (notifications), TC-50 (defaults), TC-77 (lint).

## Harness Contract

Names marked *(proposed)* are the design's to choose (PLAN "Delegated to Spec"). The tests spell each one once, in
`tests/eta_helpers.py`. If design.md picks another name, only that constant changes.

- **H-1 Clock and seed.** `CK_NOW=<ISO-8601 UTC>` *(proposed)* replaces "now" in `ck eta`, `ck progress`, the
  status-line command and ETA log lines. `ck eta --seed INT` fixes the simulation's random stream. Clock times are
  printed in local time, so the tests set `TZ=UTC`.
- **H-2 `ck eta SPEC [--kind implement|create] [--json] [--watch] [--seed INT] [--line]`.**
  - Exit codes. Exit 0 for: not started, in progress, blocked, waiting, stale, done, nothing left to run, unreadable
    progress file, and no defaults (7.4). Non-zero for: the spec directory is missing; and, for an implement
    estimate, no tasks.md or a tasks.md that does not parse (1.6, 4.7).
  - *(Superseded by design.md: there is no `--line`. Log lines come from `ck progress begin --json` `etaLine`, the
    land JSON's `eta` and `step-end --json` `eta`, within `CK_ETA_LINE_TIMEOUT_S`, default 3 s; 4 s for begin.)*
  - `--watch` redraws when the progress file changes and exits 0 at run end or on SIGINT.
- **H-3 `ck eta --json` object** *(key names proposed)*:
  - Run identity and times: `schema` (matches `^ck-eta/\d+$`), `kind`, `spec`, `state` (`not-started` |
    `on-track` | `slipping` | `blocked` | `waiting` | `stale` | `done`), `launchedAt`, `launch: {p10, p50, p90}`, `finish:
    {p10, p50, p90}` (ISO-8601 or null), `remainingMinutes: {p10, p50, p90}` (or null), `cost: {p10, p90}` | null.
  - Basis: `confidence` (`low` | `medium` | `high`), `basis: {source: "local" | "bundled" | "mixed", tasks, runs,
    match, bundledParts: [..]}`, `sizeSignal` (bool), `slots`, `slotsSource` (`agents` | `default`).
  - Graph and state: `criticalPath: [ids]`, `tasks: [{id, state, step, model, minutes, medianMinutes,
    waitingOn}]` (implement) or `phases: [{name, state, medianMinutes}]` (create), `blockedReason`,
    `waitingMinutes`, `latestChange`.
  - I/O fields: `progressFile`, `cached` (bool).
  - The `match` values are in `MATCH_STEPS`. The `bundledParts` families are `durations`, `retries`,
    `escalations`, `reviewWait`, `landingWait`, `phases`, `cost`, `size`.
- **H-4 `ck progress SPEC EVENT [--kind implement|create] [--data JSON]`** *(proposed)*.
  - Appends one JSON object per line: `{"v": 1, "event", "at", ...fields}`. `start` (with `--kind`) creates a new
    file for a new run and embeds `kind`, `spec`, `slots` and the launch estimate.
  - Events: `start`, `task-start` {task, model}, `retry` {task, attempt, model}, `escalation` {task, model},
    `landing` {tasks, red, hand}, `merge` {task}, `failed` {task}, `park` {task}, `unpark` {task},
    `approval-start` {what}, `approval-end` {what}, `phase-start` {phase}, `phase-end` {phase}, `end` {outcome}.
  - Unknown `--data` keys are dropped.
  - Prints `{"ok": true, "file": P}` or `{"ok": false, "error": E}`, plus `notification` (`sent` | `not_configured`
    | `failed` | `none`) when a notification was due. Always exits 0.
  - Files live under the data dir (`store.data_dir()`), mode 0600 in a 0700 directory, and record the project root
    so the status line can filter by project.
- **H-5 Park integration.** `ck worktree park SPEC N` (prompt-free-runs) appends `park` to the spec's active progress
  file. Clearing the note (`resolve`, or `claim` of a fresh attempt) appends `unpark`. Its own notification is
  unchanged.
- **H-6 Status line.** `ck statusline` *(proposed)* reads Claude Code's status JSON on stdin (`workspace.current_dir`,
  else `cwd`, else the process cwd). It prints the 8.3 lines for active runs of that project, reads only progress
  files and the cached estimate, and always exits 0 with nothing on stdout but those lines.
- **H-7 Slip threshold.** `ck agents set run.eta_slip_minutes N` *(proposed)*, default 15.
- **H-8 Bundled defaults.** A package resource (e.g. `claude_kiro/resources/eta_defaults.json`).
  `CK_ETA_DEFAULTS=<path>` *(proposed)* replaces it; a missing or unparseable file means "no bundled defaults".
- **H-9 Maintainer commands.** `ck stats eta-defaults --out PATH [--min-runs N]` *(proposed)* writes aggregates only
  and exits non-zero without writing below the minimum. `ck stats eta-backtest [--json]` *(proposed)* prints `{runs,
  inside, rate}`.
- **H-10 Python seams.** JSON-able inputs only, so the tests never depend on internal classes.
  - `claude_kiro.eta.estimate(run: Mapping, history: Sequence[Mapping], *, seed: int, now: datetime, defaults:
    Optional[Mapping] = None) -> dict`. It returns the H-3 object minus `progressFile` and `cached`.
    - Implement: `run = {"kind": "implement", "spec", "slots", "tasks": [{"id", "track", "model", "risk", "files",
      "criteria", "deps", "done"}], "events": [H-4 objects]}`.
    - Create: `run = {"kind": "create", "spec", "events": [...]}`.
    - `history` = local records as stored in `runs/*.json`.
  - `claude_kiro.eta.read_progress(path: Path) -> List[dict]`: valid known events in file order.
  - `claude_kiro.eta.progress_line(event: str, data: Mapping, now: datetime) -> str`: one line, without the newline.
  - `claude_kiro.eta.decide_notifications(timeline: Sequence[Mapping], *, threshold_minutes: float) ->
    List[dict]`. `timeline` entries are H-3 objects plus `waitedMinutes`. The result is `[{"kind": "slip" |
    "blocked" | "done", "index": int, "message": str}]`.
  - `claude_kiro.eta.bundle_defaults(records: Sequence[Mapping], *, min_runs: int) -> dict`. Raises `ValueError`
    below `min_runs`.
  - `claude_kiro.lint.lint_spec(spec_dir, project_dir) -> dict` (exists). New entries go in `verifyCycles` (so `ck
    plan`'s refusal and `ready` exclusion apply unchanged) with keys `task`, `file`, `testTask`, `needs`, `chain`
    (ids from Y back to X) and `message`. The tests tell them apart by the `file` key. Existing import-based
    entries keep exactly `{task, verifiedBy, needs, module, message}`.
- **H-11 Workflow scripts** (as design.md fixes them). Without new agents or labels, events come from flags on
  commands the existing agents already run: task-start, retry, escalation, revision, landfix and resolve from
  `--progress-run/--progress-event/--progress-model/--attempt` on the claim command; landing, merge and
  hand-landing from `--progress-run` on the land command; task-failed from `ck plan --progress-run --failed`;
  create phases from `ck progress event ... phase-start|step-end` steps in each phase's prompts. Start and end are
  run by the skill (Workflow mode) or the session (agent mode). The land answer and step-end carry a string field
  `eta`; the script logs it, or `ETA unavailable` when it is missing, null or not a string. The bridge's `execCk`
  mode records those events as Test Infrastructure item 2 describes.
- **H-12 Record fields.** Local implement records' `tasks[]` gain `filesCount` and `criteriaCount` *(proposed)*.
  They are absent from `ck stats export` output.
- **H-13 Notifications.** Sent through prompt-free-runs' `claude_kiro.notify.send` contract (stdin JSON with
  `message`, 10 s cap). Who sends is the design's choice: the tests only call `ck progress begin|event|end` and the
  worktree commands, and count messages. ETA notices carry `CK_NOTIFY_TYPE` (`ck_eta_slip`, `ck_eta_blocked`,
  `ck_run_done`); prompt-free-runs' pages keep `ck_needs_input` (D13, TC-101). The time-only slip check runs in
  `ck eta --watch` and `ck run`'s watcher, with `CK_ETA_RECHECK_S` setting its interval (D14, TC-57b, TC-97).
- **H-14 Agent-mode documents.** `plugin/resources/without-workflow-tool.md` and its vendored copy hold the H-4
  command templates for every event and the instruction to show `etaLine`, the land `eta` and the `step-end` `eta`
  (there is no `ck eta --line`).
- **H-15 Bridge.** `run(..., clock="virtual" | None, exec_ck=None)`, `create(args, overrides=None, exec_ck=None)`,
  and fake-project options `execCk` and `etaAnswer` (Test Infrastructure 2).

## Critical Paths

1. **Launch estimate, implement.** Write tasks.md -> `ck eta demo` (not started, finish and cost range, critical
   path, basis) -> `ck progress begin demo --kind implement --json` -> start log line (`etaLine`). TC-1, TC-6, TC-21, TC-51, TC-92.
2. **Live implement run to the end.** Task starts -> retry/escalation -> landing -> `ETA` log line -> park blocks
   -> prompt-free-runs' park message only (D15) -> unpark -> approval wait (estimate frozen) -> end -> done
   notification. `ck statusline` matches at each step. A slip from time alone reaches me through a watcher (D14).
   TC-22, TC-25, TC-32, TC-39, TC-42, TC-43, TC-44, TC-55, TC-57, TC-58, TC-59, TC-92, TC-98, TC-101, TC-102, TC-103.
3. **Create run.** `ck eta orbit` (create by default) -> phases advance -> running phase conditioned -> phase ETA lines
   -> `spec written` notification. TC-16, TC-18, TC-23, TC-51, TC-59, TC-93.
4. **Cold start.** Empty store -> low-confidence estimate from bundled defaults -> maintainer regenerates the defaults.
   TC-46, TC-47, TC-50.
5. **Failure never breaks a run.** Progress write fails, ETA unavailable, notify fails -> the same merges and outcome,
   with failures named once. TC-29, TC-54, TC-60, TC-87, TC-97.
6. **Backfill and collect stay correct.** Unreadable store refuses -> malformed record refuses (9.3) -> missing transcript
   gives an `outcome` entry -> sandbox JSON failure -> `--all` reports a sweep failure. TC-62..TC-69.
7. **Lint refusal.** tasks.md with the task-8 pattern -> `ck lint` exit 1 with X/F/T/Y/chain -> `ck plan` excludes X ->
   the workflow refuses to launch; the exempt shapes (T also verifies X, narrowed Verify) pass. TC-77, TC-78, TC-84,
   TC-99, TC-100.
8. **Stable suite.** P-10 on the virtual clock, exact, under load. TC-73, TC-74, TC-76.

## Commands

Full gate (as `specs/ck.json`): `uv run pytest -q -n auto`

| Scope | Command |
|---|---|
| ETA implement + size | `uv run pytest -q -n auto tests/test_eta_implement.py tests/test_eta_size.py` |
| ETA create | `uv run pytest -q -n auto tests/test_eta_create.py` |
| Progress file | `uv run pytest -q -n auto tests/test_eta_progress.py` |
| Live, blocked, waiting | `uv run pytest -q -n auto tests/test_eta_live.py tests/test_eta_blocked.py` |
| Cold start | `uv run pytest -q -n auto tests/test_eta_coldstart.py` |
| Log line + status line | `uv run pytest -q -n auto tests/test_eta_surfaces.py` |
| Notifications | `uv run pytest -q -n auto tests/test_eta_notify.py` |
| Workflow mode (bridge) | `uv run pytest -q tests/test_eta_workflow.py` |
| `ck run` spool | `uv run pytest -q tests/test_eta_run_spool.py` |
| Writer paths | `uv run pytest -q -n auto tests/test_eta_progress.py tests/test_eta_writers.py` |
| Agent-mode documents | `uv run pytest -q tests/test_eta_agent_docs.py` |
| ETA properties | `uv run pytest -q -n auto tests/test_eta_properties.py` |
| End to end | `uv run pytest -q tests/test_eta_e2e.py` |
| Performance NFRs | `uv run pytest -q tests/test_eta_perf.py` (not under `-n auto`, so timings are not skewed) |
| Analytics hardening (D7) | `uv run pytest -q -n auto tests/test_stats_followups.py` and, for 11.4, `uv run pytest -q -n auto tests/test_stats_*.py tests/test_stats_doctor.py tests/test_doctor_xdist.py` |
| P-10 and virtual clock | `uv run pytest -q -n auto tests/test_landing_scheduler_properties.py tests/test_scheduler_virtual_clock.py` and `uv run pytest -q tests/test_workflow_scripts.py` |
| JS control-flow suites directly | `node tests/js/spec_implement.test.mjs`, `node tests/js/spec_create.test.mjs`, `node tests/js/landing_speed.test.mjs`, `node tests/js/finish_first.test.mjs` |
| Lint | `uv run pytest -q -n auto tests/test_lint_verify_files.py tests/test_lint.py` |
| Packaged == vendored | `uv run pytest -q -n auto tests/test_plugin.py tests/test_plugin_modes.py` |
| Opt-in stress (12.4 NFR) | `CK_STRESS_P10=1 uv run pytest -q tests/test_scheduler_virtual_clock.py -k stress` |
| Opt-in calibration | `CK_ETA_BACKTEST=1 uv run pytest -q tests/test_eta_perf.py -k backtest` |

## Not Tested

- **The 60 s default interval of the `--watch` redraw and of the time-only slip check (4.4, 8.5).** Waiting it out
  needs more than 60 s. TC-34 covers redraw on change, exit at the end and SIGINT; TC-57(b) and TC-97 cover the
  time-only check itself with `CK_ETA_RECHECK_S=1`.
- **Claude Code actually rendering the status line, and a real phone notification (ntfy).** Both belong to Claude
  Code or the user's script. The tests check the command's stdout and the notify script's stdin.
- **An LLM following the agent-mode instructions, or the skill text in Workflow mode.** Agent mode is covered
  statically (TC-26, TC-52, TC-78) and cross-checked against the commands the Workflow script emits; the skill's
  begin/approval/end steps are covered statically by TC-98.
- **Permission prompts inside agents.** These are out of scope by assumption. The run cannot see them.
- **11.2 (writer by source rather than id prefix) and dropping the inline `__import__("time")`.** Workflow run
  files are discovered only as `wf_*.json`, so no public input separates "by source" from "by prefix". TC-70 is a
  regression guard that passes before and after. The `__import__` removal has no behaviour.
- **The calibration target (>= 60%) in the default suite.** It depends on the real machine's history, so it is
  opt-in (TC-90). The default suite checks that the backtest runs.
- **The 50-run stress under CPU saturation in the default suite.** It takes minutes, so it is opt-in (TC-76). The
  default suite runs a shorter load comparison.
- **A Gantt view.** Out of scope.
- **Exact simulated values.** There is no oracle for Monte Carlo output. The tests use ordering, determinism,
  monotonicity, conditioning and metamorphic relations instead (P-1..P-13).
