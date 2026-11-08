# Test Plan: Unattended Correctness

**Created:** 2026-11-07
**Inputs:** [requirements.md](requirements.md), [PLAN.md](PLAN.md) (D1-D15 binding). Written without reading design.md, so these tests check whatever the implementation turns out to be. Where a test needs a name the design has not fixed yet (a CLI subcommand, a JSON key, a seam), the Harness Contract proposes one. If the design picks another name, the test tasks follow the design, and the review checks that every row here still maps to it.

## Strategy

This repo has four real boundaries. Each criterion is tested at the boundary it crosses:

| Boundary | How tests reach it | Used for |
|---|---|---|
| The `ck` CLI on real git repos running real test runners | `python -m claude_kiro.cli` subprocesses (or `CliRunner` where in-process registration is the point) on temp git repos built by fixtures. These repos have real `pytest` suites, real `node --test` files, real git hooks and real worktrees. | Stories 1, 3, 5, 6, 8-12, 15-18; the ck side of 2, 4, 13, 14 |
| The Workflow script `plugin/workflows/spec-implement.js` (and `spec-create.js`) | Node runs the **real script source** under the fake runtime (`tests/js/load_workflow.mjs`). Agents and the OpenCode `ck` global are faked by `tests/js/fake_project.mjs`. Hypothesis drives scenarios through the JSON-lines bridge (`tests/js/bridge.mjs`, `tests/landing_bridge.py`). | Stories 2, 3.6-3.9, 4, 7, 12.5-12.8, 13, 19 |
| The OpenCode host (TypeScript) | `node --test opencode/test/*.test.ts` against the real `config.ts`, `models.ts`, `run-child.ts`, `record.ts`, `preflight.ts` and `runner.mjs` (with `$CK_BIN` pointing at the real ck or a recording shim). | Story 14, 2.2 under the real runner, 19.6 |
| Runs on real projects | Manual bench reruns, recorded in an evidence file that a test checks. | Story 20, the thin-agent context NFR, the `dsh` E2E |

**How integration-first applies here:**

- No test mocks a `claude_kiro` module, git, the filesystem or a test runner. A test reaches ck only through the CLI or through a named public decision seam (Harness Contract).
- The only fakes stand in for things this repo does not own:
  - the Claude Code Workflow runtime and its LLM agents (fake runtime, fake project);
  - OpenCode's server (the existing TS test doubles);
  - the `opencode`, `claude` and `dsh` binaries in bench tests (fake executables, as `tests/bench_helpers.py` does today);
  - the clock, through the existing `CK_NOW`/progress clock env and `os.utime`.
- When a prompt tells an agent to run a ck command, a test takes that command from the prompt the real script produced and runs it through the real `ck` on a real repo. This shows the instruction works; checking that a string appears in a prompt would not.
- Git hooks are tested by running plain git (`merge`, `merge --no-verify`, `update-ref`, `branch -f`, `reset`, `push .`, `commit`, `cherry-pick`) in real worktrees. Every refusal check also asserts that the ref did not move. The exit code alone is not enough.
- Pure logic with real branching gets unit-level property tests at a decision seam: attribution, kind and right-reason classification, truncation, protected-path decisions, write-set overlap, live edges, test-fix routing, claim release, gate verdicts and retry digests. Each seam also has one integration case showing that the CLI or the script really calls it.
- **Property library:** Hypothesis (in the `dev` group, 6.170). The repo has no npm dependencies, so there is no fast-check. Workflow properties are generated in Python and run by the real script through the bridge. OpenCode role parsing is table-driven in `node --test` over the enumerated domain. Settings:
  - pure seams: `@settings(derandomize=True, max_examples=200, database=None, deadline=None)`;
  - bridge-driven workflow properties: `max_examples=60`;
  - properties that drive real git or real pytest per example (P-5b, P-18, P-19): `max_examples=25` with stateful steps capped at 8.

  All of these use the same derandomized, database-free settings.
- **Naming (dogfoods D1):** every test function carries its TC ID as `test_tc<N>_<slug>`. Property tests are named `test_tc<N>_p<k>_<slug>`, where TC-N is the property-suite row that lists them. JS cases carry `tc<N>_` in their `test(...)` name. So the attributed gate and the 1.9 lint rule run on this spec's own suite.
- **Before the feature exists, each test fails for the right reason.** Today:
  - `ck gate` has no `--json` or `--attribute`;
  - `ck graph`, the audit, the fixture lock, the claim sweep, the test-fix commands and the harness registry do not exist;
  - no git hooks are installed, and `ck worktree merge` lands protected files;
  - the test-track gate passes tests that fail on fixture assertions;
  - `reviewer.cross_test` defaults to false;
  - the script's periodic and final gates are default agents running a non-attributed `ck gate`, and it reads only `testConflict`;
  - `opencode.roles` is an unknown config key;
  - `--harness` is a fixed `click.Choice`.

  TC-90 also fails on today's `protected_changes`: `git diff --name-only` quotes non-ASCII paths (verified: `".claude/\303\244.md"` is not matched by `.claude/**`).
- **xdist:** every new test is xdist-safe. Each uses its own `tmp_path` repo, the conftest-isolated `CK_CONFIG_HOME`, `XDG_DATA_HOME` and gate registry, and its own progress dir. Only timing budgets (TC-169, TC-170, TC-109) carry `@pytest.mark.serial`.

## Test Infrastructure

### Exists (reuse as is)

- `tests/conftest.py`:
  - the `project` fixture (git repo with `calc.py`, `tests/test_calc.py`, `specs/calc`, `specs/ck.json`), `write_tasks`, `git`;
  - `no_network` (blocks sockets in-process and, through `tests/netguard/sitecustomize.py`, in Python children);
  - autouse isolation of `CK_CONFIG_HOME`, `XDG_DATA_HOME`, `CLAUDE_CONFIG_DIR`, the git identity, `CK_GATE_REGISTRY` and the headless markers;
  - the `serial` marker.
- `tests/test_lint.py`: `block(n, title, track=, status=, files=, verify=, vby=, verifies=, deps=)`, `write_spec`, and the `ck(cwd, *args)` CliRunner helper.
- `tests/test_worktree_land.py` and `tests/test_worktree_cli.py`:
  - the `repo` fixture, `ck(cwd, *args)`, `wt(root, n)`, `commit_in`, `land`, `head`;
  - `age_claim`, `lock_file`, `legacy_claim` for claim ages.
- `tests/test_gate.py`: `run_ck_gate(project, *args)`, `implement_subtract`, `write_subtract_tests`.
- `tests/js/`:
  - `load_workflow.mjs` (`runWorkflow(script, {agent, args, slots, clock, ck})`, `loadFunction`);
  - `load_decisions.mjs` (the decisions block between the `// --- decisions` markers, `extra` names);
  - `fake_project.mjs` (`project({...})`, `directCk(p)`, `execCkRun`);
  - `bridge.mjs`, `virtual_clock.mjs`, `fake_create.mjs`.
- `tests/landing_bridge.py`: the module-scoped `bridge` fixture and the `dags()`, `subsets()`, `scenarios()` strategies.
- `tests/test_workflow_scripts.py` runs every `tests/js/*.test.mjs` (new suites are picked up by the glob).
- `tests/bench_helpers.py` (fake `ck run`, fake hidden tests). `tests/test_bench_opencode_run.py` covers the fake `opencode` binary flow.
- `tests/guard_helpers.py`: `bwrap`/`socat` stubs for PATH checks (not a real sandbox).
- `tests/test_plugin.py`: the plugin layout, vendored copies byte-identical to the source, and the generated agent types.
- `opencode/test/*.test.ts`: `config.test.ts`, `models.test.ts`, `run-child.test.ts`, `record.test.ts`, `preflight.test.ts`, `runner.test.ts` (`$CK_BIN` shim pattern), `workflow-tool.test.ts`.
- `src/claude_kiro/cli/run.py::sandbox_settings(project, allowed_domains, worktree_dir)` returns the sandbox filesystem rules that the sandbox reproduction mirrors.

### To add

1. **`tests/unattended_helpers.py`** (shared Python harness; every helper drives real git or the real CLI):
   - `ck_json(cwd, *args, env=None, input=None) -> (code, obj, stderr)`: runs `python -m claude_kiro.cli` as a subprocess and parses stdout as JSON (`obj=None` when it does not parse).
   - `attr_project(tmp_path, *, statuses, tests, extra_tasks="", config=None) -> Path`: a git repo (branch `main`, then `integrate/demo` created with `ck worktree integration demo`), with:
     - `calc.py`;
     - `specs/demo/{requirements.md, test-plan.md, design.md, tasks.md}`;
     - `specs/ck.json` (`verify: ["python -m pytest -q -p no:cacheprovider tests"]`).

     Its tasks.md has:
     - test task 1, which owns `tests/test_calc_tc.py`, Verifies Tasks 2 and 3, and has `**Verify:** python -m pytest -q tests/test_calc_tc.py`;
     - impl task 2, Verify `python -m pytest -q tests/test_calc_tc.py -k "tc1_ or tc2_"`;
     - impl task 3, Verify `... -k "tc3_"`;
     - test task 6, which owns `tests/test_more.py`, Verifies Tasks 7 and 8, and has Verify `-k "tc5_"`;
     - impl tasks 7 and 8, with no Verify.

     `tests` maps a test name to an outcome: `pass`, `fail`, `flaky` (fails on the first run and passes on the next, through a counter file under `$CK_TEST_COUNTERS`), `import-error`, `syntax-error`, `setup-error`, `sleep:<s>`, or `assert-msg:<n>` (an assertion message of n characters, including multibyte text). Every test appends one line to its counter file per run, so tests can count how many times a node ran.
   - `pytest_body(name, outcome)` and `node_test_file(failing: bool)` are the test-file writers behind `tests=`.
   - `hooks_repo(tmp_path) -> Path`: the `repo` shape plus an integration worktree from `ck worktree integration demo`. It adds task branches:
     - `feat/demo-task-1`, which adds `.claude/CLAUDE.md`;
     - task 2, which adds `.claude/commands/x.md`;
     - task 3, which changes only `src/a.py`;
     - task 4, which adds `.claude/commands/ä.md`;
     - task 5, which deletes `.claude/settings.json` (seeded on `main`).

     Helpers:
     - `ref(root, name)`;
     - `raw(cwd, *git_args) -> CompletedProcess` (plain git, never through ck);
     - `snapshot_ck_state(root) -> dict` (path -> bytes) and `restore_ck_state(root, snap)`. These cover every file under `git rev-parse --git-common-dir`, the ck data dir and the spec's worktree dir. A test uses them to replay whatever ck wrote during an approved landing, without knowing the token format.
   - `readonly_git_metadata(root)` (context manager): `chmod a-w` on `.git/worktrees/*`, the hooks location and `.git/config`, and restores them afterwards. This reproduces the sandbox's read-only metadata without namespaces (TC-87).
   - `sandboxed(argv, project, worktree_dir) -> CompletedProcess`: runs argv under real `bwrap`, with binds derived from `sandbox_settings(project, [], worktree_dir)["sandbox"]["filesystem"]`:
     - `/` read-only;
     - the project and worktree dir writable;
     - `~` hidden except `SANDBOX_HOME_READABLE`;
     - the ck data dir hidden;
     - `.git/hooks`, `.git/config` and `.git/worktrees` read-only, as observed in October.

     It calls `pytest.skip(reason)` only when `bwrap --ro-bind / / true` fails, for example nested inside another sandbox (as in this session).
   - `progress_run(spec, key, *, ended: bool, last_event_age_min: float)`: creates a real run record with `ck progress begin`/`event`/`end` (and `CK_NOW` for ages).
   - `spec_graph_project(tmp_path, *, tasks, design_cli, tests, sources) -> Path`. It writes:
     - design.md, with a CLI table in the format the design defines (proposed: `| Command | File |`);
     - tasks.md, through `test_lint.block`;
     - test files with the invocation forms of P-20;
     - a click CLI (`src/mtr/cli.py`) and an argparse CLI (`src/mtr2/cli.py`), both runnable, so missing-command messages are real.
   - `fake_claude(tmp_path) -> Path`: a `claude` executable for `PATH` that records its argv, the `--settings` JSON and `git config core.hooksPath` at launch, then exits 0 (TC-189).
   - `equity_sized_repo(tmp_path, files=3000, commits=500) -> Path`: for the hook-cost NFR. This is an assumption about the equity repo's size; adjust once it is measured.
2. **`tests/fixtures/unattended/`**:
   - `land-json-keys.json`: the key paths of today's `ck worktree land --json` output. The first test task generates it with the current ck, before any change, and commits it (TC-37).
   - `gate-text-baseline/*.txt`: today's `ck gate` text reports for the `attr_project` scenarios (TC-13). Captured the same way.
   - `failure.schema.json`: the one D3 failure schema (Harness Contract 2), used by both Python and JS (TC-36).
3. **`tests/js/fake_project.mjs` additions** (existing options unchanged):
   - `gateAnswers: [json | {code, stdout} | 'throw' | 'hang']`: the answers to attributed gates, consumed in order, through `directCk` (`gate` argv) or through the thin-agent labels `full gate @k`, `re-check k`, `final gate`.
   - `taskFailures: {n: [[failure...], ...]}`: per-attempt D3 failure lists that task n's agent returns.
   - `testConflictsList: {n: {conflicts: [...], attempts}}`, plus the existing `testConflicts` (single, for 3.7's legacy shape).
   - `testFix: {file: {verdict: 'fixed'|'code-wrong', proof: 'ok'|'fail', violations: [], review: 'approve'|'reject', finding}}`.
   - `priorFixes: [node...]`: what the persisted one-fix record answers.
   - `crossTest: [{tests: [node], finding}]`.
   - `fidelity: {n: {mismatches, unparsed, unmatched}}`.
   - `fixtureStatus: {unlabeled: [...]}`.
   - `owns: {n: [paths]}` and `overlaps: {n: [m]}` (passed in plan answers).
   - `provides: {command: task}`.
   - `missingCommand: {n: {command, attempts}}`.
   - `audit: json | 'throw'`.
   - `sweep: json`.

   `directCk` answers `gate`, `graph`, `worktree audit`, `worktree sweep`, `testfix *` and `fixtures status` argv as well as plan, land and relink.
4. **`tests/js/load_decisions.mjs`**: `UNATTENDED_NAMES = ['gateVerdict', 'thinResult', 'failureDigest', 'conflictsOf', 'testFixAction', 'liveEdge', 'writeSetStart']`, loaded only through `extra`, so the existing suites stay green before the workflow gains them.
5. **`tests/js/bridge.mjs`**: a new op `{op:'call', name, args}` that calls a decisions-block function by name (from the `UNATTENDED_NAMES` extra). `{op:'run'}` gains `realCk: {cmd, cwd, env}`: direct `ck` calls go to the real CLI (for TC-21, TC-33 and the smoke tests).
6. **`tests/js/real_agents.mjs`**: scripted agents for the smoke tests. For task n, an agent:
   1. claims the worktree with the command from its prompt;
   2. writes `scenario.files[n]` and commits;
   3. runs the gate command written in its prompt through the real ck;
   4. returns the RESULT built from ck's JSON.

   No LLM is involved.
7. **New JS suites:** `tests/js/attributed_gates.test.mjs`, `tests/js/test_fix.test.mjs`, `tests/js/test_fix_cross.test.mjs` (the cross-test cases TC-39, TC-65, TC-186, split out so `test_fix.test.mjs` runs whole at the task that builds the test-fix loop), `tests/js/test_track_routing.test.mjs` (the workflow parts of TC-55, TC-59, TC-64, TC-66 to TC-68, TC-71, TC-73 and TC-139, kept out of `test_fix.test.mjs` for the same reason), `tests/js/command_deps.test.mjs`, `tests/js/thin_agents.test.mjs`, `tests/js/spec_create_provides.test.mjs`.
8. **New Python suites:** listed in the Test file column below and in Commands.
9. **OpenCode:** `opencode/test/roles.test.ts` (new) and additions to `record.test.ts`, `preflight.test.ts` and `runner.test.ts`. Plus a recording `$CK_BIN` shim (`opencode/test/fixtures/ck-shim.mjs`) that writes argv to a file and prints canned JSON.
10. **Evidence files:** `specs/unattended-correctness/e2e-reruns.json` and `specs/unattended-correctness/dsh-verification.json`, written by the people and tasks that run the reruns and the `dsh` check (shapes in Harness Contract 14). Tests validate them; they are never generated by tests.

## Test Cases

IDs are grouped by story, not strictly ascending. TC-14 sits in Story 11, and TC-109 sits with the NFRs. TC-106 and TC-149 are intentionally unused: they were merged into TC-14 and TC-143. TC-181 to TC-195 were added by the spec review and sit with their stories.

Levels: **int-cli** = real ck CLI on a real git repo; **int-git** = plain git against installed hooks; **int-wf** = the real workflow script under the fake runtime; **int-wf+ck** = the workflow under the fake runtime with real ck subprocesses; **int-ts** = the real OpenCode host modules; **e2e** = smoke test across workflow, real ck and real git; **prop** = property suite; **manual** = recorded run.

### Story 1: Per-test attribution

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-1 | 1.1, 1.2, 1.3 | int-cli | Given `attr_project`: Tasks 1 and 2 Done, Task 3 Not Started, with `test_tc1_add` and `test_tc3_sub` both failing. Task 1's suite is one today's gate would excuse, because Task 3 is not Done. When `ck gate demo --attribute --json` runs. Then `failures` has both nodes: `tc1` with `tc:"TC-1"`, `tasks:["2"]`, `status:"regression"`; `tc3` with `tasks:["3"]`, `status:"excused"`, `excusedBy:["3"]`. `ok` is false, and the exit code is 1. | real pytest, real tasks.md | `tests/test_gate_attribution.py` |
| TC-2 | 1.1 | int-cli | Given `test_tc5_x` failing. No impl Verify selects tc5, but test task 6 (Verify `-k "tc5_"`) covers it and Verifies Tasks 7 and 8, both Done. When the gate runs with `--attribute --json`. Then the entry has `tasks:["7","8"]` and status `regression`. | same | `tests/test_gate_attribution.py` |
| TC-3 | 1.2 | int-cli | Given Task 3 Not Started and `test_tc3_sub` failing. When `ck gate demo --task 3 --attribute --json` runs, gating Task 3 as Done. Then the tc3 failure is `regression`, not excused. | same | `tests/test_gate_attribution.py` |
| TC-4 | 1.3 | int-cli | Given only `test_tc3_sub` failing, with Task 3 Not Started. When the attributed gate runs. Then `ok` is true, the exit code is 0, and the entry is `excused` with `excusedBy:["3"]`. | same | `tests/test_gate_attribution.py` |
| TC-5 | 1.4 | int-cli | Given `test_helper_roundtrip` (no TC ID) failing in `tests/test_calc_tc.py`, which test task 1 owns. (a) With Task 3 not Done: `attributedBy:"file"`, `tasks:["2","3"]`, `excused` by 3. (b) With all Done: `regression`. | same | `tests/test_gate_attribution.py` |
| TC-6 | 1.5 | int-cli | Given `tests/test_orphan.py::test_x` failing, in a file no task owns, while most tasks are Not Started. When the attributed gate runs. Then the entry is `unattributed` with `tasks:[]`, `ok` is false, and nothing excuses it. | same | `tests/test_gate_attribution.py` |
| TC-7 | 1.6, 3.2 | int-cli | Given `tests/test_more.py` with a top-level `from calc import nothing_here`, and another owned file with a syntax error. When the attributed gate runs. Then each gives one file-level entry: `node` is null or the file, `kind` is `import` or `collection` respectively, `attributedBy` is `file`, and `tasks` are the owning test task's Verifies. | real pytest collection | `tests/test_gate_attribution.py` |
| TC-8 | 1.7 | int-cli | Given a test task whose Verify is `node --test tests/js/calc.test.mjs`, with one failing case. When the attributed gate runs. Then there is one entry with `file:"tests/js/calc.test.mjs"`, `node:null`, `attributedBy:"file"`, and the owning test task's Verifies as tasks. | real `node --test` | `tests/test_gate_attribution.py` |
| TC-9 | 1.7 (error) | int-cli | Given a test task whose Verify is `sh -c 'echo boom; exit 1'`. (a) With its verified tasks not all Done: the gate is not red because of it, and `warnings` names the command and says attribution was unavailable. (b) With all Done: red, with the same warning. | real subprocess | `tests/test_gate_attribution.py` |
| TC-10 | 1.8 (error) | int-cli | Given a failing `test_tc99_x`, where TC-99 is in no Verify selector and no coverage. When the attributed gate runs. Then the entry is `attributedBy:"file"`, and `warnings` names `TC-99`. | real pytest | `tests/test_gate_attribution.py` |
| TC-11 | 1.9 | int-cli | Given test-track Files with `test_tc1_a`, `test_helper`, `TestX::test_y` and `test_tc2_b[param]`. When `ck lint demo --json` runs. Then there is one warning each for `test_helper` and `TestX::test_y`, naming the owning test task, and none for the TC-named tests. | real lint | `tests/test_gate_attribution.py` |
| TC-12 | 1.10, NFR determinism | int-cli | Given the TC-1 project and a verify command run with `-n 2` (real xdist). When the attributed gate runs 3 times. Then the `failures` arrays are identical, element by element (timing fields excluded). | real pytest-xdist | `tests/test_gate_attribution.py` |
| TC-13 | 1.11 | int-cli | Given each `attr_project` scenario. When `ck gate demo` runs without `--attribute`. Then the text report equals the captured baseline (`gate-text-baseline/*.txt`), with the same exit code. The excused suites did not run: their counter files are absent. | real gate | `tests/test_gate_attribution.py` |
| TC-193 | 1.1 | int-cli | Given `specs/ck.json` `verify: ["python -m pytest -q -p no:cacheprovider -m \"not e2e\" tests"]`, a test `test_tc2_e2e_flow` marked `e2e` that fails, and Tasks 2 and 3 Done. When `ck gate demo --attribute --json` runs. Then Task 2's Verify (which selects `tc2_`) is not treated as covered: the e2e node runs and is reported `regression` with `tasks:["2"]`. Every other node of Task 2's Verify, already run by `verify`, appears once in its counter file (no node runs twice). | real pytest, real markers | `tests/test_gate_attribution.py` |
| TC-195 | 1.4, 1.5 | int-cli | Given a wrapper test `tests/test_wrap.py::test_run_suite[thin.test.mjs]` (no TC ID, in `tests/test_wrap.py`, which no test task owns; parametrized over `tests/js/*.test.mjs` as this repo's `tests/test_workflow_scripts.py` is), whose `thin.test.mjs` case fails, and test task 6 owning `tests/js/thin.test.mjs` (Verifies 7 and 8). (a) With Task 8 not Done: the entry is `unattributed`, `attributedBy:"none"`, `tasks:[]`, not excused, `ok` is false, and its `hint` names `tests/js/thin.test.mjs` and test Task 6. (b) With 7 and 8 Done: the same `unattributed` entry and hint (the hint never depends on task status). (c) A parameter ID naming no owned file is `unattributed` with no `hint`. | real pytest wrapper | `tests/test_gate_attribution.py` |

### Story 2: Attributed periodic and final gates

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-15 | 2.4 | int-cli | Given all tasks Done and `test_tc1_add` set to `flaky`. When the attributed gate runs. Then the entry is `flaky`, `ok` is true, the exit code is 0, and the counter file shows exactly 2 runs. | real pytest rerun | `tests/test_gate_attribution.py` |
| TC-16 | 2.4 | int-cli | Given all tasks Done and `test_tc1_add` failing every time. Then the entry is `regression` after exactly 2 runs, and `ok` is false. | same | `tests/test_gate_attribution.py` |
| TC-17 | 2.5, NFR rerun bound | int-cli | Given one excused failing test, one regression candidate and 5 passing tests. When the attributed gate runs. Then the excused test's counter shows 1 run, the candidate's 2, and every passing test's 1. No suite was rerun as a whole. | same | `tests/test_gate_attribution.py` |
| TC-18 | 2.1, 2.2 | int-wf | Given the OpenCode path (the `ck` global is present) and 25 linear tasks with the default N. When the run goes. Then `ck` is called with argv containing `-C <integration dir> gate demo --attribute --json` after the 10th and 20th merges and once after the last merge. No agent's label or prompt runs a gate. | real script | `tests/js/attributed_gates.test.mjs` |
| TC-19 | 2.1 | int-wf | Given `args.fullGateEvery = 3` and 9 tasks. Then attributed gates run after merges 3, 6 and 9, plus the final gate. | real script | `tests/js/attributed_gates.test.mjs` |
| TC-20 | 2.3, 19.1 | int-wf | Given the Claude Code path (no `ck` global). Then each periodic and final gate is one agent call with the lean agent type, whose prompt holds `ck -C <dir> gate demo --attribute --json` on its own line, using the shared thin schema. The run uses the JSON the fake returns as is: its regressions reach the fixer prompt unchanged. | real script | `tests/js/attributed_gates.test.mjs` |
| TC-21 | 2.3 | int-wf+ck | Given the thin gate prompt from TC-20, rendered for a real `attr_project`. When the test extracts the command from the prompt and runs it through the real ck. Then it exits 0 or 1 and prints JSON that validates against `failure.schema.json`. | real script + real CLI | `tests/test_attributed_gate_prompt.py` |
| TC-22 | 2.6, 3.9 | int-wf | Given a gate answer with 2 regressions (nodes, TC IDs, tasks, kinds, messages). Then a fixer is started whose prompt carries both entries as JSON. The recheck is again an attributed gate: a direct `ck` call under OpenCode, or a thin agent under Claude Code. It is never a default agent. | real script | `tests/js/attributed_gates.test.mjs` |
| TC-23 | 2.7 | int-wf | Given gate answers that stay red through the whole fixer ladder. Then the result has today's red-target stop (`stopReason` naming the target), `regressions` lists the remaining entries, and no new task starts after it. | real script | `tests/js/attributed_gates.test.mjs` |
| TC-24 | 2.8 | int-wf | Given a green gate answer with 2 `excused` entries (each with `excusedBy`) and 1 `flaky` entry. Then no fixer starts, and the result's gate record holds `excusedByTask` counts per excusing task, the flaky test and the report path. Old readers' fields stay: every `gates[]` entry has `green`, `fullGates[]` entries have `afterMerges` and `green`, and `finalGate` has `green`. | real script | `tests/js/attributed_gates.test.mjs` |
| TC-25 | 2.9 (error), 19.5 | int-wf | Gate answers are each, in turn:<br>- `code:2` with no JSON;<br>- stdout that is not JSON;<br>- a thrown error;<br>- a hang past the step timeout (virtual clock);<br>- a thin agent error;<br>- a thin answer missing `json`;<br>- JSON without `failures`.<br>Then each run records a gate error with its reason in the result, never counts the gate green, and never reports the run as fully green. | real script | `tests/js/attributed_gates.test.mjs` |
| TC-26 | 2.10 | int-cli + int-wf | (a) In the workflow, every `worktree land` argv and prompt lacks `--attribute`. (b) In the CLI, `ck worktree land demo 2 --gate --json`, with a failing test outside Task 2's Verify and verifying suites, still MERGES Task 2. | real script; real land | `tests/js/attributed_gates.test.mjs`, `tests/test_gate_attribution.py` |
| TC-27 | 2.11 | int-wf + int-cli | (a) In the workflow, the final gate's request comes after the last land's end in the trace. (b) In the CLI, with every task Done and failures present, no entry has status `excused`. | real script; real gate | `tests/js/attributed_gates.test.mjs`, `tests/test_gate_attribution.py` |
| TC-183 | 2.12, 3.3 | int-cli | Given `attr_project` with 1 regression, 3 excused failures (2 excused by Task 3, 1 by Tasks 3 and 8) and 1 flaky test. When `ck gate demo --attribute --json --compact` runs. Then stdout is one `ck-gate-compact/1` object: `regressions` and `flaky` hold full failure entries (schema-valid), `excusedByTask` is `{"3": 3, "8": 1}`, `ok` and the exit code equal `--json`'s, and `report` names a file that exists. The file is a `ck-gate/1` object whose `failures` lists all 5 entries, equal (timing excluded) to a `--json` run's. A green gate's compact form has empty lists. | real pytest, real file | `tests/test_gate_failures.py` |
| TC-184 | 2.3, 2.8, 2.12 | int-wf | On the Claude Code path (no `ck()` global, `runnerAgent` set), every periodic, recheck and final gate prompt's ck command ends with `--compact`; on the OpenCode path the direct argv has no `--compact`. Given a red compact answer, the fixer prompt carries the `regressions` entries; given a green one, the gate record holds `excusedByTask` and `report`. A compact answer with `regressions` not an array is a gate `error` (2.9). | real script | `tests/js/attributed_gates.test.mjs` |
| TC-188 | 2.13, 2.11 | int-wf | Pass 1: Task 3 lands `PROTECTED` and is parked (needs-input, `kind: "protected"`); its dependent Task 4 does not start; the park note names `ck worktree approve demo 3` and the resume (same `runId`). Pass 2, started with the same `runId` and a plan in which Task 3 is Done: run start treats pass 1's claims as its own, Task 4 starts, and one final attributed gate is requested after Task 4's land, with outcome `complete`. | real script | `tests/js/thin_agents.test.mjs` |

### Story 3: One structured failure format

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-28 | 3.1, 3.6 (ck side) | int-cli | Given one file with 3 failing tests (assertion, `ValueError`, `assert-msg:40`) and 1 passing test. When `ck gate demo --task 2 --json` runs. Then there are exactly 3 entries, each with `node`, `tc`, `tasks`, `file`, `kind`, `message` and `truncated`. | real pytest | `tests/test_gate_failures.py` |
| TC-29 | 3.2 | int-cli | Table over real pytest outcomes:<br>- bare `assert` -> `assertion`;<br>- a subclass of `AssertionError` -> `assertion`;<br>- a class named `MyAssertionError(Exception)` -> `error`;<br>- a module-level `ImportError` or `ModuleNotFoundError` -> `import`;<br>- a `SyntaxError` -> `collection`;<br>- a fixture raising at setup -> `error`;<br>- a teardown error -> `error`;<br>- `RuntimeError` -> `error`;<br>- a verify command over `verify_timeout` -> `timeout`;<br>- `pytest.fail` -> one of the five kinds. | real pytest | `tests/test_gate_failures.py` |
| TC-30 | 3.3 | int-cli | (a) A passing gate with `--json` has `"failures": []`. (b) A gate whose only failing check is an unchecked acceptance box has `failures: []` and `ok: false`. | real gate | `tests/test_gate_failures.py` |
| TC-31 | 3.4 | int-cli | With `--attribute`, every entry's `status` is one of the four values. Without it, entries carry no `status`. | real gate | `tests/test_gate_failures.py` |
| TC-32 | 3.5, NFR message limit | int-cli | (a) `assert-msg:5000`, including multibyte characters: `message` is at most 500 characters, `truncated: true`, and the entry is present. (b) A 300-character message: `truncated: false`. (c) 120 failing tests: 120 entries. | real pytest | `tests/test_gate_failures.py` |
| TC-33 | 3.6 | int-wf+ck | (a) The task prompt holds `ck -C <wt> gate demo --task <n> --json`, tells the agent to run the whole Verify and return ck's `failures`, and the RESULT schema has a `failures` array with the D3 item properties. (b) The command, taken from the prompt and run on a real repo with a 3-failure file, prints 3 entries. | real script + real CLI | `tests/js/attributed_gates.test.mjs`, `tests/test_attributed_gate_prompt.py` |
| TC-34 | 3.7 | int-wf | (a) A task returns `testConflicts` with 2 entries: both go to the confirmation agent, and both are in the result. (b) A task returns the legacy single `testConflict`: the result's `testConflicts` has length 1, with the same fields. | real script | `tests/js/attributed_gates.test.mjs` |
| TC-35 | 3.8 | int-wf | Given attempt 1 returns 120 failures over 3 files. Then the retry prompt holds the first 50 entries as structured JSON, in order, plus per-file counts of the other 70, and no text trimmed to 3000 characters. | real script | `tests/js/attributed_gates.test.mjs` |
| TC-36 | 3.9 | int-cli + int-wf | Every failure object, wherever it appears, validates against `failure.schema.json`. The CLI sources are `ck gate --json`, `ck worktree land --gate --json` (`gates[].failures`) and `ck gate --attribute --json`. The JS sources are the retry, fixer, recheck and test-fix prompts and the result. | real CLI + real script | `tests/test_gate_failures.py`, `tests/js/attributed_gates.test.mjs` |
| TC-37 | 3.10 | int-cli | `ck worktree land --json` output still contains every key path in `land-json-keys.json`, captured from today's ck. New keys are allowed. | real land | `tests/test_gate_failures.py` |
| TC-191 | 3.1 | int-cli | Given a passing test `test_tc1_nested` in the gated suite that runs `python -m pytest` in a subprocess with `os.environ` copied, on a temp project whose own test `test_tc2_inner` fails. When `ck gate demo --attribute --json` runs. Then `failures` is empty and `ok` is true: the inner failure never reaches the gate's report. The same holds with `-n 2` (xdist). | real nested pytest | `tests/test_gate_failures.py` |

### Story 4: Automatic test repair

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-38 | 4.1 | int-wf | Given a confirmed conflict on Task 4, with `args.agents.test_writer = {model:'opus', effort:'high'}`. Then one `test-fix` agent call is made with `opus`/`high`. Its prompt names a test-fix branch created from INTO (the base in the command is the integration branch) and carries the structured conflict entries. | real script | `tests/js/test_fix.test.mjs` |
| TC-39 | 4.1, 7.2 | int-wf | Given a blocking cross-test finding naming 2 tests. Then a test-fixer starts with those 2 tests as conflict entries. | real script | `tests/js/test_fix_cross.test.mjs` |
| TC-40 | 4.2, 4.6 | int-wf | Given `testFix.proof = 'fail'`. Then no land and no review happen, and the conflict goes to the needs-input path with the proof result in the note. | real script | `tests/js/test_fix.test.mjs` |
| TC-41 | 4.2 | int-cli | `ck testfix prove demo --task 4 --branch <testfix> --json` on a real repo:<br>(a) the fixed test passes against Task 4's branch plus the fix: `ok: true`;<br>(b) it fails with `ImportError` for a name Task 4's Files own: `ok: true`, with reason "task still has to address";<br>(c) it fails with an assertion on fixture data: `ok: false`. | real git, real pytest | `tests/test_testfix_cli.py` |
| TC-42 | 4.3 | int-wf | Given proof ok. Then a `test-fix review` agent is called with the reviewer's `risky_model`/`risky_effort`. Its prompt holds the diff reference, the test-plan rows of the changed tests' TC IDs and the guard checklist (no weakening, no requirements or test-plan edits). | real script | `tests/js/test_fix.test.mjs` |
| TC-43 | 4.4 | int-wf + e2e | (a) In the workflow, after approval, the next steps are a `worktree land` of the test-fix branch with `--gate`, then a fresh task agent for Task 4, and a dependent Task 5 starts only after Task 4's merge. (b) In the e2e smoke test (TC-176), the land is real. | real script | `tests/js/test_fix.test.mjs` |
| TC-44 | 4.5 (error) | int-cli + int-wf | (a) `ck testfix check demo <branch> --json` on branches that change `src/calc.py`, `specs/demo/requirements.md` or `specs/demo/test-plan.md` gives `allowed: false`, with those paths; so does a branch whose only `test-plan.md` change appends one new row. One that changes only `tests/` files and `tests/fixtures/` files gives `allowed: true`. (b) In the workflow, a violation means no review agent and needs-input. | real git diff; real script | `tests/test_testfix_cli.py`, `tests/js/test_fix.test.mjs` |
| TC-45 | 4.6 | int-wf | Given the review rejects. Then nothing is landed, and needs-input carries the findings. | real script | `tests/js/test_fix.test.mjs` |
| TC-46 | 4.7 | int-wf | Given the fixer verdict `code-wrong` with a finding. Then there is no review and no land, and Task 4 is retried with a failures entry carrying the finding. | real script | `tests/js/test_fix.test.mjs` |
| TC-47 | 4.8 | int-cli + int-wf | (a) `ck testfix claim demo --node <id> --json` grants on the first call. A second call, from a new process and after a simulated new run, gives `granted: false` with the earlier fix. (b) In the workflow, a conflict on a node in `priorFixes` starts no fixer and goes to needs-input. | real persisted state; real script | `tests/test_testfix_cli.py`, `tests/js/test_fix.test.mjs` |
| TC-48 | 4.9 | int-wf | Given 3 conflicts in `tests/test_a.py` and 1 in `tests/test_b.py`. Then exactly 2 test-fixer calls are made, the first carrying all 3 tests. | real script | `tests/js/test_fix.test.mjs` |
| TC-49 | 4.10 | int-wf | The result's `testFixes[]` holds `{tests, proof, review, blockedTask, outcome: 'landed'|'sent-to-me'}` for a landed fix and for one sent to me. | real script | `tests/js/test_fix.test.mjs` |
| TC-185 | 4.11 | int-cli | `ck testfix prove demo testfix1 --held 2 --test <node> --json` on a real repo with a test-fix branch from `integrate/demo` and no Task 2 branch: (a) the fixed test fails with `ImportError` for a name Task 2's Files own: proved, `against: "integration"`; (b) it fails on a fixture assertion: not proved; (c) it is skipped by the fix: not proved. Neither `--blocked` nor `--held` given: usage error, no proof run. | real git, real pytest | `tests/test_testfix_cli.py` |
| TC-186 | 4.11, 7.4 | int-wf | Given a blocking cross-test finding whose tests verify Task 2. Then the `testfix prove` call carries `--held 2` and no `--blocked`; after an approved review and a MERGED land, Task 2 starts; the `testFixes[]` record has `source: 'cross-test'`, `blocked: null`, `held: ['2']`. With `proof: 'fail'`, nothing lands, Task 2 stays held, and the conflict goes to needs-input. | real script | `tests/js/test_fix_cross.test.mjs` |
| TC-187 | 4.4, 2.10 | int-cli | After a proved and reviewed fix on `testfix1` that changes `tests/test_calc_tc.py` (owned by test task 1), `ck worktree land demo testfix1 --gate --json` is MERGED, its entry names `gateTasks: ["1"]`, and the gate ran only test task 1's checks. A fix branch changing only a file no test task owns lands RED with "changes no file a test task owns", and the ref is unchanged. | real land, real gate | `tests/test_testfix_cli.py` |

### Story 5: Right-reason failures

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-50 | 5.1, 5.2 | int-cli | Given test task 1, whose verified Task 2 is not Done, gated in its worktree (branched from `integrate/demo`). Its tests fail with `ImportError: cannot import name 'subtract' from 'calc'`. When `ck gate demo --task 1 --json` runs. Then the right-reason check passes, and every entry is marked `rightReason:"not-built-yet"`. | real worktree, real pytest | `tests/test_right_reason.py` |
| TC-51 | 5.2 | int-cli | Table, each a real failing test against the fixture CLIs:<br>- click `No such command 'thesis'`;<br>- argparse `invalid choice: 'thesis'`;<br>- `unrecognized arguments: --since`;<br>- click `No such option`;<br>- `NotImplementedError` raised from `calc.py`;<br>- `AttributeError: module 'calc' has no attribute 'subtract'`.<br>All are `not-built-yet`, and the gate passes. | real pytest + real CLIs | `tests/test_right_reason.py` |
| TC-52 | 5.3 (error), 5.7 | int-cli | Given a test whose body loads `tests/fixtures/prices.json` with `json.load` and asserts a wrong value on that data only (`assert prices["AAPL"] == 4.15`, no project call and no name imported from project code in the assert's dependencies), which raises `AssertionError` while Task 2 is not Done. Then the gate fails, the entry is `class: "bad"` (wrong reason), the check names it, and the test-track task is sent back (it does not land). | real pytest | `tests/test_right_reason.py` |
| TC-53 | 5.3 (error) | int-cli | Given a pytest fixture whose project validator raises `ValueError("invalid enum 'Buy'")` while loading a fixture file. Then a setup error, marked `wrong-reason`, and the gate fails. | real pytest | `tests/test_right_reason.py` |
| TC-54 | 5.4 | int-cli | Given `ModuleNotFoundError: No module named 'numpy'` (third-party), or a `KeyError` from a dict built in the test. Then each is reported as `unclear` in checks, and the gate passes. | real pytest | `tests/test_right_reason.py` |
| TC-55 | 5.3 | int-wf | Given the test-track task's gate returns entries marked `wrong-reason`. Then its retry runs with the test-writer role's opts, and its prompt holds the failure list with the marked entries. | real script | `tests/js/test_track_routing.test.mjs` |
| TC-56 | 5.5 | int-cli | Given a new test that already passes on the integration branch. Then a warning check names it, and the gate passes. | real pytest | `tests/test_right_reason.py` |
| TC-57 | 5.6 | int-cli | Given every task test task 1 verifies is Done, and a test failing with `ImportError`. Then the gate fails (no leniency), with no right-reason check. | real pytest | `tests/test_right_reason.py` |
| TC-181 | 5.7, 5.4 | int-cli | Given a test asserting on project code while Task 2 is not Done: `assert calc.rate_floor(load("prices.json")) == 0.04`, where Task 2's stub returns a wrong value, and a test whose assert uses a pytest fixture argument. Then each `AssertionError` is `class: "unclear"`, reported in a warning check, and the gate passes. A third test whose assert line cannot be parsed back (source changed after the run) is also `unclear`. | real pytest | `tests/test_right_reason.py` |

### Story 6: Project fixture validation

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-58 | 6.1 | int-cli | Given `specs/ck.json` with `"test_track": {"validate": "python scripts/validate.py"}`, where the script writes its cwd to a marker. When a test-track gate runs in the task worktree. Then the marker equals that worktree's path. | real subprocess | `tests/test_test_track_validate.py` |
| TC-59 | 6.2 (error) | int-cli + int-wf | (a) The script prints 60 lines and exits 1: the gate fails, and the check shows the command and the tail. (b) In the workflow, that gate result sends the task back to the test writer. | real gate; real script | `tests/test_test_track_validate.py`, `tests/js/test_track_routing.test.mjs` |
| TC-60 | 6.3 (error) | int-cli | Given the script sleeps 5 s with `verify_timeout: 1`. Then the check fails and its detail says timeout. | real timeout | `tests/test_test_track_validate.py` |
| TC-61 | 6.4 | int-cli | Given no key. Then there is no validation check, and the gate's checks equal those of the same project gated before the change (baseline). | real gate | `tests/test_test_track_validate.py` |
| TC-62 | 6.5 (error) | int-cli | Given `validate` set to `42`, `{"a":1}`, `["ok", 3]` or `null`. Then the exit code is non-zero, the output names `test_track.validate`, and no test command ran (counter absent). `["a", "b"]` runs both commands. | real config load | `tests/test_test_track_validate.py` |
| TC-63 | 6.6 | int-cli | Given an impl-task gate with the key set. Then the marker is absent. | real gate | `tests/test_test_track_validate.py` |

### Story 7: Blocking cross-test check

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-64 | 7.1 | int-cli + int-wf | (a) `ck agents --json` (isolated config) shows `reviewer.cross_test: true`. (b) In the workflow, with args lacking `cross_test`, a cross-test agent is requested once a test-track wave has merged. | real `ck agents`; real script | `tests/test_cross_test_default.py`, `tests/js/test_track_routing.test.mjs` |
| TC-65 | 7.2 | int-wf | Given a finding. Then it is treated as blocking and turned into conflict entries for the test-fixer (see TC-39). | real script | `tests/js/test_fix_cross.test.mjs` |
| TC-66 | 7.3 | int-cli + int-wf | (a) `ck agents set reviewer.cross_test false`, global and then project: `ck agents --json` shows false. (b) In the workflow, `cross_test: false` means no cross-test agent. | real CLI; real script | `tests/test_cross_test_default.py`, `tests/js/test_track_routing.test.mjs` |
| TC-67 | 7.4 | int-wf | Given a finding open on tests that verify Task 2, while Task 9 is unrelated and ready. Then `task 2` is not requested before the test-fix lands, and Task 9 runs meanwhile. | real script | `tests/js/test_track_routing.test.mjs` |
| TC-68 | 7.5 | int-wf | Given no findings. Then there is no test-fixer, and the trace equals that of the same scenario with `cross_test` false, apart from the cross-test agent itself. | real script | `tests/js/test_track_routing.test.mjs` |

### Story 8: Row fidelity

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-69 | 8.1, 8.2 | int-cli | Given row TC-4 "threshold 4.50" with a test using `4.15`, and a design data model where `Side` is `BUY` or `SELL` with a test using `side="Buy"`. When `ck fidelity demo --task 1 --json` runs and a test-track gate runs. Then the mismatches are `{test, value: 4.15, expected: 4.50}` and `{test, value: "Buy", allowed: ["BUY","SELL"]}`. The gate fails with D3 entries for both. | real files, real gate | `tests/test_row_fidelity.py` |
| TC-70 | 8.2 | int-cli | Given the row's date 2026-01-31, a test with `date(2026, 1, 30)`, and a row range "4.15 to 4.50" with a test asserting `<= 4.15` as the upper bound. Then both are mismatches. | real files | `tests/test_row_fidelity.py` |
| TC-71 | 8.3 | int-cli + int-wf | (a) A value computed at runtime (`base * 1.1`) is in `unparsed`, and checked values are not. (b) In the workflow, the test-task review prompt holds exactly the `unparsed` and `unmatched` items, and none of the checked ones. | real files; real script | `tests/test_row_fidelity.py`, `tests/js/test_track_routing.test.mjs` |
| TC-72 | 8.4 | int-cli | A test with no TC ID, and a test whose TC-77 has no row, both appear in `unmatched`. | real files | `tests/test_row_fidelity.py` |
| TC-73 | 8.5 | int-cli + int-wf | When everything matches, all 3 lists are empty, and the review prompt has no row-fidelity section. | real files; real script | `tests/test_row_fidelity.py`, `tests/js/test_track_routing.test.mjs` |
| TC-74 | 8.6 | int-cli | After `ck fidelity` and the gate, the hashes of test-plan.md, design.md and the tests are unchanged, and `git status --porcelain` is empty. | real files | `tests/test_row_fidelity.py` |

### Story 9: Protected-file hooks

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-75 | 9.1 | int-git | (a) After `ck worktree integration demo` (CREATED), a raw protected update is refused (as in TC-76). (b) Remove the hooks, run `ck worktree integration demo` again (EXISTS): they are reinstalled. (c) The run-start step (the command the implement skill or run start calls; Harness Contract 7) also installs them. | real git hooks | `tests/test_protected_hooks.py` |
| TC-76 | 9.2 | int-git | In the integration worktree, `git merge --no-ff --no-verify feat/demo-task-1` exits non-zero, `integrate/demo` is unchanged, and `.claude/CLAUDE.md` is absent from the tree after `git merge --abort` or cleanup. | real git | `tests/test_protected_hooks.py` |
| TC-77 | 9.2 | int-git | Parametrized over plain-git ways to move `integrate/demo` to a protected commit:<br>- `update-ref`;<br>- `branch -f`;<br>- `reset --hard <c>`;<br>- `push . <c>:refs/heads/integrate/demo`;<br>- `commit` adding `.claude/x` in the integration worktree;<br>- `cherry-pick`;<br>- `merge --ff-only`.<br>Each is refused, and the ref is unchanged. | real git | `tests/test_protected_hooks.py` |
| TC-78 | 9.2 | int-git | Given `land.allow_paths: [".claude/commands/**"]` in the integration branch's `specs/ck.json`. Then a raw merge of Task 2 (only `.claude/commands/x.md`) succeeds, and Task 1 (`.claude/CLAUDE.md`) is still refused. | real git | `tests/test_protected_hooks.py` |
| TC-79 | 9.3 | int-git | (a) Snapshot ck's state, land an allowed protected change with `ck worktree land`, and restore the snapshot taken right after the land. Then a raw update of `integrate/demo` to another protected commit is refused. (b) Reset the ref back (as ck would) and replay the same update with raw git: refused. (c) The state from a landing into `integrate/a` does not let a raw update of `integrate/b` through. | real git + real ck state | `tests/test_protected_hooks.py` |
| TC-80 | 9.4 | int-git | Updates to `refs/heads/feature`, `refs/tags/integrate/x`, `refs/heads/integrated/x` and `refs/heads/feat/integrate/x`, to the protected commit, all succeed. | real git | `tests/test_protected_hooks.py` |
| TC-81 | 9.5 | int-git | `git merge --no-ff feat/demo-task-1` (hooks enabled) is refused before the commit exists. Stderr names `.claude/CLAUDE.md` and says how to approve (it names `land.allow_paths` or the approval command). | real git | `tests/test_protected_hooks.py` |
| TC-82 | 9.6 | int-cli | `ck worktree merge demo 1 --into integrate/demo --json` gives state `PROTECTED`, a detail naming `.claude/CLAUDE.md`, exit 3, and an unchanged ref. | real merge | `tests/test_protected_hooks.py` |
| TC-83 | 9.7 | int-cli | With hooks installed: (a) `land` and `merge` of Task 2 under the allow-list are MERGED, and the ref moves. (b) Task 3 (non-protected) is MERGED by both. | real ck + hooks | `tests/test_protected_hooks.py` |
| TC-84 | 9.8 | int-cli + int-wf | (a) The approval command run with a pipe as stdin (an agent's shell) is refused, and the ref is unchanged. (b) The same approval given through a pseudo-terminal (`pty.fork`) outside the sandbox lands Task 1 with a token, and it is recorded as approved (checked by TC-99). (c) In the workflow, a PROTECTED landing goes to the needs-input/question path, and no agent lands it. | real pty, real git; real script | `tests/test_protected_hooks.py`, `tests/js/test_fix.test.mjs` |
| TC-85 | 9.9 | int-git | Given a repo with its own hooks:<br>- `.git/hooks/pre-merge-commit` exits 1 with "project says no" when `NOPE` exists;<br>- `core.hooksPath=.githooks`, whose `reference-transaction` appends to a marker;<br>- a pre-commit-framework `pre-commit` hook.<br>After ck installs its hooks:<br>- the project hooks still run (the marker grows);<br>- a non-protected merge is refused when `NOPE` exists;<br>- a protected merge is still refused by ck;<br>- pre-commit still runs on commit. | real git | `tests/test_protected_hooks.py` |
| TC-86 | 9.10 (error) | int-cli | Given the hooks location made read-only before install. Then `ck worktree integration demo --json` (and run start) reports that hooks are not installed, with the reason. The audit reports it too (TC-97). | real fs perms | `tests/test_protected_hooks.py` |
| TC-87 | 9.11, NFR sandbox | int-git | Inside `readonly_git_metadata` (worktree metadata dir, hooks and config read-only):<br>- a raw `git merge --no-verify` of Task 1 into the integration worktree is refused;<br>- `ck worktree land` of an allowed change still succeeds (tokens work with read-only metadata).<br>When the suite runs under `ck run`, this test runs inside the real sandbox. | real git under read-only metadata | `tests/test_protected_hooks.py` |
| TC-88 | 9.11 | int-git | `sandboxed(["git","merge","--no-verify","feat/demo-task-1"], …)` under real bwrap, configured from `sandbox_settings`: refused, and the ref is unchanged. It skips, with the bwrap error, only where namespaces are unavailable. | real bwrap sandbox | `tests/test_protected_hooks.py` |
| TC-189 | 9.1, 9.11, NFR sandbox | int-git | Given a fresh `hooks_repo` with no hooks, and a fake `claude` on `PATH` that records its argv and exits 0. When `ck run demo` runs (unsandboxed parent). Then the hooks are installed before the fake `claude` starts (its record shows `core.hooksPath` set), and the `--settings` JSON passed to it lists `<common>/ck/hooks` and `<common>/ck/key` under `denyWrite`. Then inside `readonly_git_metadata`, `ck worktree start demo --json` reports `hooks.installed: true` and `active: true`, and a raw `git merge --no-verify feat/demo-task-1` in the integration worktree is refused. Where namespaces exist, the same `ck worktree start` and raw merge also run under `sandboxed(...)` with the same results. | real `ck run` pre-launch, real git | `tests/test_protected_hooks.py` |
| TC-194 | 9.1, 9.9 | int-git | Given `specs/ck.json` with an install command that runs `git config core.hooksPath .husky/_` (a husky `prepare`), and `.husky/_/pre-merge-commit` appending to a marker. When `ck worktree land demo 3 --gate --json` runs (which runs the install). Then after the land `core.hooksPath` points at ck's hooks again, `chain.json` names `.husky/_`, the land result reports the re-install, a raw protected merge is still refused, and the husky hook still runs (the marker grows). | real git, real install step | `tests/test_protected_hooks.py` |
| TC-89 | 9.10, 9.11 | manual | In each equity rerun under `ck run`, the run-start report shows the hooks installed, and the run-end audit shows the hooks active. Recorded in `e2e-reruns.json` (TC-166). | real `ck run` | `specs/unattended-correctness/e2e-reruns.json` |

### Red team: protected files

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-90 | 9.2, 9.6 | int-git + int-cli | Task 4 adds `.claude/commands/ä.md` (git quotes the path), plus a variant with a space in the name. Then raw merge, `ck worktree land` and `ck worktree merge` all refuse it, and PROTECTED names the unquoted path. Today's `protected_changes` returns `[]` for this case. | real git quoting | `tests/test_protected_hooks_redteam.py` |
| TC-91 | 9.2 | int-git | A branch edits `specs/ck.json` to allow `.claude/**` and adds `.claude/x` in the same commit, and again in two commits. Both are refused, because the allow-list is read from the ref's old value, never from the incoming commit. | real git | `tests/test_protected_hooks_redteam.py` |
| TC-92 | 10.1, 10.2 | int-git + int-cli | Bypasses hooks cannot stop:<br>- `git -c core.hooksPath=/dev/null merge`;<br>- deleting the hooks;<br>- writing the ref file directly.<br>Each lands a protected change, and `ck worktree audit demo --json` then reports the drift and the hook state. | real git; real audit | `tests/test_protected_hooks_redteam.py` |
| TC-93 | 9.2 | int-git | Task 5 deletes `.claude/settings.json`, and a rename moves `.claude/a` to `notes/a`. Both are refused. | real git | `tests/test_protected_hooks_redteam.py` |
| TC-94 | 9.2 | int-git | The protected change is buried two merges deep: the branch merges a side branch that added `.claude/x`, then makes a clean commit. Refused, because ck compares the old and new ref values, not the tip commit. | real git | `tests/test_protected_hooks_redteam.py` |
| TC-95 | 9.8 | int-git | An approval attempted inside `sandboxed(...)` (and inside `readonly_git_metadata` with the ck data dir unreadable), including when wrapped in `script -qc` to fake a TTY with "yes" piped in, is refused. The ref is unchanged. | real sandbox repro | `tests/test_protected_hooks_redteam.py` |
| TC-182 | 9.12, 9.2 | int-git | Plain git delete-then-create: `git update-ref -d refs/heads/integrate/demo` passes; then `git update-ref refs/heads/integrate/demo <commit with .claude/CLAUDE.md>` is refused and the ref stays absent. Re-creating it at the last approved commit passes. Also refused: `git branch integrate/demo <protected commit>` after `git branch -D`, `git push . <protected>:refs/heads/integrate/new` (a new ref with no approved state), and `git worktree add -b integrate/x <protected commit>`. `ck worktree integration demo` creating its own ref with hooks installed succeeds (token). | real git | `tests/test_protected_hooks_redteam.py` |
| TC-190 | 9.11, NFR sandbox | int-git | Sandboxed writes to ck's hook state are refused. (a) Always: `sandbox_settings(project, [], wt)["sandbox"]["filesystem"]["denyWrite"]` contains `<common>/ck/hooks` and `<common>/ck/key`, and not `<common>/ck/tokens`. (b) Where namespaces exist: under `sandboxed(...)`, writing `<common>/ck/hooks/pre-commit`, editing `chain.json` to point at a directory the agent controls, replacing the recorded interpreter and overwriting `<common>/ck/key` all fail, and their sha256s are unchanged; a sandboxed `ck worktree land` of an allowed change still succeeds (tokens writable). | real bwrap sandbox | `tests/test_protected_hooks_redteam.py` |

### Story 10: Run-end protected-file audit

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-96 | 10.1 | int-cli | After bypass writes (ref file written directly) that add `.claude/a`, change `.mcp.json` and delete `.claude/settings.json`, `ck worktree audit demo --json` lists exactly those 3, with `added`, `changed` and `deleted`. | real git | `tests/test_protected_audit.py` |
| TC-97 | 10.2 | int-cli | Separately: the hooks deleted, a hook's content altered, `core.hooksPath` repointed, and the hooks never installed (TC-86). Each is reported in the audit's hook section. | real fs | `tests/test_protected_audit.py` |
| TC-98 | 10.3 | int-cli + int-wf | (a) In a clean run, the audit says `clean: true` with a no-drift statement. (b) In the workflow, the result carries the audit, and run end calls it via direct `ck` or a thin agent. | real audit; real script | `tests/test_protected_audit.py`, `tests/js/thin_agents.test.mjs` |
| TC-99 | 10.4 | int-cli | Protected changes landed through the allow-list (TC-83) and through approval (TC-84b) are not reported. | real git | `tests/test_protected_audit.py` |
| TC-100 | 10.5 (error) | int-cli | Delete or corrupt the approved-state record (found by `snapshot_ck_state` diff before and after `ck worktree integration`). Then the audit has `complete: false` and `clean: false`, with the reason. | real fs | `tests/test_protected_audit.py` |
| TC-101 | 10.1 | int-wf | Runs ending green, red (stopReason) and with a thrown step error each call the audit once, after the last step. Each result has `audit`. An `audit: 'throw'` answer is recorded as an incomplete audit, never as clean. | real script | `tests/js/thin_agents.test.mjs` |

### Story 11: Unified spec graph

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-102 | 11.1, 11.2 | int-cli | Given `spec_graph_project` with tasks, Verify `-k`, Verifies, Files, a Provides field, a design CLI table, test files that invoke commands and test files that import modules. When `ck graph demo --json` runs. Then the node set (kinds task, tc, test-file, source-file, command) and the edge set (kinds depends, verifies, owns, provides, invokes, imports) equal the expected sets exactly. | real files | `tests/test_spec_graph.py` |
| TC-103 | 11.3 | int-cli | (a) The design CLI table maps `mtr thesis new` to `src/mtr/thesis.py`, and Task 71 owns that file: Task 71 provides the command. (b) Task 82 has `**Provides:** mtr thesis new`: only Task 82 provides it. | real files | `tests/test_spec_graph.py` |
| TC-104 | 11.4 | int-cli | One test file holds every invocation form in P-20's domain. The invokes edges equal the expected longest command paths, and the near-misses add none. | real files | `tests/test_spec_graph.py` |
| TC-105 | 11.5, NFR determinism | int-cli | Two runs give byte-identical stdout, with nodes and edges sorted by (kind, id). | real CLI | `tests/test_spec_graph.py` |
| TC-14 | 11.6, 1.1 | int-cli | Move `tc3_` from Task 3's Verify to Task 8's. Then `ck graph --json` changes its verifies edge, and `ck gate --attribute --json` changes tc3's `tasks` to `["8"]`. Both change together. | real graph + gate | `tests/test_spec_graph.py` |
| TC-107 | 11.7 | int-cli | Edit tasks.md, a test file and a source file between two `ck graph` runs, with `os.utime` restoring the old mtimes and keeping the same sizes where possible. The second output reflects every edit. | real fs | `tests/test_spec_graph.py` |
| TC-108 | 11.8 (error) | int-cli | Given a malformed task block (`### Task x:`, and a `**Dependencies:** Task` with no number), plus a test file with a syntax error. Then `problems` names each source and its problem, and every other node and edge is present (compared with the graph of the clean subset). | real files | `tests/test_spec_graph.py` |

### Story 12: Command dependencies

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-110 | 12.1 | int-cli | Test task 1's test file (it verifies Task 2) invokes `mtr thesis new`, which Task 5 provides, and Task 2 does not depend on Task 5. Then `ck lint demo --json` has one finding naming the test file, `mtr thesis new`, Task 5 and Task 2. | real lint | `tests/test_command_deps.py` |
| TC-111 | 12.2 | int-cli | `ck relink demo --apply --json` adds `Task 5` to Task 2's `**Dependencies:**` in tasks.md. A second lint has no finding, and a second relink changes nothing. | real tasks.md | `tests/test_command_deps.py` |
| TC-112 | 12.3 | int-cli | A test invokes `mtr bogus`, which no task provides and which the fixture CLI does not define: lint warns. Invoking `mtr version`, which already exists in `src/mtr/cli.py`: no warning. | real CLI introspection | `tests/test_command_deps.py` |
| TC-113 | 12.4 | int-wf | spec-create's tasks-writing prompt tells the writer to add `**Provides:**` to every impl task whose commands ck cannot infer, and to check with `ck graph` or `ck lint`. Plus `ck graph` on a tasks.md written that way (fixture) gives the provides edge. | real script; real CLI | `tests/js/spec_create_provides.test.mjs`, `tests/test_command_deps.py` |
| TC-114 | 12.5 | int-wf | Task 2's gate failure is a missing command (`mtr thesis new`) that unfinished Task 5 provides. Then there is no testConflict and no retry count used. The log has the live edge. Task 2 is re-requested only after Task 5's land ends. | real script | `tests/js/command_deps.test.mjs` |
| TC-115 | 12.5 | int-cli | Real failing tests against the click and argparse fixture CLIs: ck's failure entries mark the missing command (`missingCommand: "mtr thesis new"`), for `No such command`, `unrecognized arguments` and `invalid choice`. | real pytest + CLIs | `tests/test_command_deps.py` |
| TC-116 | 12.6 | int-wf | A missing command no task provides becomes a test conflict on the invoking tests, through confirmation, then the test-fixer. | real script | `tests/js/command_deps.test.mjs` |
| TC-117 | 12.7 (error) | int-wf | Task 5 depends on Task 2, and Task 2's gate misses Task 5's command. Then no edge is added, and Task 2 is blocked with a reason naming the cycle `2 -> 5 -> 2`. | real script | `tests/js/command_deps.test.mjs` |
| TC-118 | 12.8 | int-wf | Live edges are logged, and the result's `liveEdges[]` holds `{task, dependsOn, command}`. | real script | `tests/js/command_deps.test.mjs` |
| TC-119 | 12.9 | int-cli | The existing `tests/test_lint.py`, `tests/test_relink.py` and `tests/test_lint_verify_*.py` pass unchanged. | real lint/relink | existing files |

### Story 13: Write-set scheduling

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-120 | 13.1, 13.2 | int-wf | Tasks 2 and 3 are ready. Task 2 owns `src/a.py`, Task 3 owns `src/*.py`, and Task 2 has the higher priority. Then Task 2 starts, and Task 3's first request comes after Task 2's land ends. | real script | `tests/js/command_deps.test.mjs` |
| TC-121 | 13.3 | int-wf | Task 3 is held behind Task 2, and disjoint Task 4 is ready with a free slot. Task 4 starts while Task 3 is held. The log or progress names "3" held, waiting on "2". | real script | `tests/js/command_deps.test.mjs` |
| TC-122 | 13.4 | int-wf + int-cli | A task with no Files runs alongside others (no hold), and `ck lint demo --json` warns that it has no write-set. | real script; real lint | `tests/js/command_deps.test.mjs`, `tests/test_command_deps.py` |
| TC-123 | 13.5 | int-wf | Every ready task overlaps running Task 2, and Task 2 then fails (blocked). The next held task starts at the next free slot, and the run terminates with every other task merged. | real script | `tests/js/command_deps.test.mjs` |
| TC-124 | 13.6 | int-wf + int-cli | The plan answers' `deps` stay unchanged across the run, and no relink is called for overlap. Running `ck plan demo --json` before and after (real) leaves tasks.md byte-identical. | real script; real CLI | `tests/js/command_deps.test.mjs`, `tests/test_command_deps.py` |
| TC-125 | 13.1 | int-cli | `ck plan demo --json` (or `ck graph`) reports `overlaps` for the glob pairs `src/*.py` / `src/a.py` and `src/**` / `src/x/y.py`, and none for `src/a.py` / `src/ab.py`. | real CLI | `tests/test_command_deps.py` |

### Story 14: Per-role models under OpenCode

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-126 | 14.1 | int-ts | Project `specs/ck.json` sets `opencode.roles.reviewer = "opencode-go/deepseek-v4.1-flash#medium"`. Then an `agent()` call with `role: "reviewer"` and `model: "opus"` resolves to `opencode-go/deepseek-v4.1-flash` with variant `medium` (through `loadConfig` and model resolution). | real config.ts/models.ts | `opencode/test/roles.test.ts` |
| TC-127 | 14.2 | int-ts | Role `fixer` is unmapped. The call with `model: "sonnet"` resolves through the alias map exactly as today. | same | `opencode/test/roles.test.ts` |
| TC-128 | 14.3 | int-ts | Global (`$CK_CONFIG_HOME/config.json`) and project both map `implementer`. The project value wins, and the source records the project file. | real file loading | `opencode/test/roles.test.ts` |
| TC-129 | 14.4 (error) | int-ts | Over P-28's domain, `loadConfig` throws `ConfigError` naming `opencode.roles.<key>` and the value. The workflow tool refuses to start the run (no child session created). | real config + workflow tool | `opencode/test/roles.test.ts` |
| TC-130 | 14.5 | int-cli | A bench arm `{model: "opencode-go/muse-spark-1.3-contributor", roles: {reviewer: "opencode-go/deepseek-v4.1-flash#medium"}}`, run through the fake opencode flow, writes clone config with reviewer mapped and every other role on the arm model. Read back by the fake binary. | real bench prep + fake opencode | `tests/test_bench_opencode_roles.py` |
| TC-131 | 14.6 | int-cli + int-wf | With `opencode.roles` in `specs/ck.json`, `ck agents --json` is unchanged, and the Claude Code workflow's agent opts carry only Claude aliases. | real CLI; real script | `tests/test_bench_opencode_roles.py`, `tests/js/thin_agents.test.mjs` |
| TC-132 | 14.7 | int-ts + int-cli | The OpenCode run record has, per agent, the model and variant it ran on (roles-mapped and alias-resolved). `ck stats` collect over that record keeps them per role. | real record.ts; real stats | `opencode/test/record.test.ts`, `tests/test_opencode_record.py` |
| TC-133 | 14.8 | int-ts + int-cli | The preflight output lists each of the 7 roles with its resolved model and its source (`roles`, `alias` or `default`). `ck doctor --harness opencode`, on a project with the plugin, prints the same lines. | real preflight; real doctor | `opencode/test/preflight.test.ts`, `tests/test_doctor_opencode.py` |

### Story 15: Fixture hash lock

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-134 | 15.1 | int-cli | `fixtures.recorded: ["tests/fixtures/recorded/**"]`. The first landing of `prices.json` creates a lock entry with its sha256, visible in `ck fixtures status demo --json`. | real land | `tests/test_fixture_lock.py` |
| TC-135 | 15.2 | int-cli | A later landing changes `prices.json`. The land JSON reports fixture drift naming the file and the task, and the audit reports it. | real land + audit | `tests/test_fixture_lock.py` |
| TC-136 | 15.3 | int-cli | `ck fixtures handcraft demo tests/fixtures/recorded/prices.json --reason "trimmed to 3 rows"`, then the change lands: no drift, and the reason is recorded in status. | real CLI | `tests/test_fixture_lock.py` |
| TC-137 | 15.4 (error) | int-cli | The reasons `""`, `"   "` and `"\n"` all leave the change reported as drift. | real CLI | `tests/test_fixture_lock.py` |
| TC-138 | 15.5 | int-cli | Editing a lock entry's hash, or deleting the entry, for a file that still exists: the audit reports it. | real fs | `tests/test_fixture_lock.py` |
| TC-139 | 15.6 | int-cli + int-wf | (a) Status `unlabeled` lists recorded fixtures without capture metadata that are not marked handcrafted. (b) In the workflow, the test-task review prompt lists them, and a blocking finding on them sends the task back. | real CLI; real script | `tests/test_fixture_lock.py`, `tests/js/test_track_routing.test.mjs` |
| TC-140 | 15.7 | int-cli | With no globs, landings create no lock (no new file in the `snapshot_ck_state` diff), and land and audit JSON report no fixture items. | real land | `tests/test_fixture_lock.py` |
| TC-141 | 15.8 (error) | int-cli | Globs `42`, `null`, `["a", 1]` or `{"x": "y"}` give a config error naming the key, from land and from status. | real config | `tests/test_fixture_lock.py` |

### Story 16: Release claims by run ID

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-142 | 16.1 | int-cli | `ck worktree claim demo 1 --progress-run <key> …` records the run ID, shown in `ck worktree status demo --json`. | real git lock | `tests/test_claims_run_id.py` |
| TC-143 | 16.2, 16.6 | int-cli | Run A (begun and ended through `ck progress`) holds claims on Tasks 1 and 2, with commits. `ck worktree sweep demo --run B --json` releases both and lists `{task, run: A}`, and both branches keep their commits. | real progress records, real git | `tests/test_claims_run_id.py` |
| TC-144 | 16.2 | int-cli | Run A has no end event, and its last event is older than the stale threshold (`CK_NOW`). Its claim is released. | real records | `tests/test_claims_run_id.py` |
| TC-145 | 16.3 | int-cli | Run A is live (a recent event, no end), and its claim is 3 hours old (`age_claim`). The sweep keeps it, and `ck worktree claim demo 1` by B is BUSY. | real records | `tests/test_claims_run_id.py` |
| TC-146 | 16.4 | int-cli | A claim with no run ID (legacy owner string): kept when 30 minutes old, released when 61 minutes old. `claim_stale_minutes` is honoured. | real git lock | `tests/test_claims_run_id.py` |
| TC-147 | 16.5 | int-cli | Run A's record file is deleted. Its claim is released. | real fs | `tests/test_claims_run_id.py` |
| TC-148 | 16.7 | int-cli + int-wf | (a) A claim by B, swept by B, is kept as its own, and B's claim of the task succeeds without `--takeover`. (b) In the workflow, the run calls the sweep once, before its first task agent (direct `ck`, or a thin agent). | real records; real script | `tests/test_claims_run_id.py`, `tests/js/thin_agents.test.mjs` |
| TC-192 | 16.2, 16.5 | int-cli | A pass under the spool layout (`CK_PROGRESS_DIR=<worktrees>/.ck-progress`) claims Tasks 1 and 2 and is killed: its spool copy has no `end`, last event 10 minutes old, and the data-dir copy is absent. When the next pass's `prepare_spool` runs and then `ck worktree start demo --run B --json`. Then the spool copy has a `superseded` end, both claims are in `releasedClaims` with the old run, and their commits are kept. Without the `prepare_spool` step, `run_liveness` still reads the spool copy (no false `missing`). | real records, real locks | `tests/test_claims_run_id.py` |

### Story 17: Harness registry

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-150 | 17.1, 17.2 (error) | int-cli | `ck bench run cfg.json --harness nope --dry-run` and `ck doctor --harness nope` each exit non-zero with an error listing every registered name. `--help` lists exactly the registered names. | real CLI | `tests/test_harness_registry.py` |
| TC-151 | 17.3 | int-cli | A test registers the fake harness `fakeh` (in-process, `CliRunner`). `ck bench run cfg.json --harness fakeh` runs the arm through the adapter and writes its result to the results file. `ck doctor --harness fakeh` calls the adapter's doctor. | real bench/doctor CLI | `tests/test_harness_registry.py` |
| TC-152 | 17.4 | int-cli | The existing `tests/test_bench.py`, `tests/test_bench_opencode*.py` and `tests/test_doctor_opencode.py` pass unchanged. `--runner` with `--harness opencode` still fails with today's message. | real CLI | existing files + `tests/test_harness_registry.py` |
| TC-153 | 17.5 | int-cli | The arm results from the fake claude run, the fake opencode run and `fakeh` have the same key set. | real bench | `tests/test_harness_registry.py` |

### Story 18: Guarded DeepSeek Harness adapter

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-154 | 18.1 | int-cli | `dsh-verification.json` exists, with the official repository URL (`github.com/deepseek-ai/...`), the version or commit checked, the date, `canHostAgent`, `canHostCk`, `missing[]` and `evidence[]`. | evidence file | `tests/test_dsh_harness.py` |
| TC-155 | 18.2 | int-cli + manual | If `canHostAgent` and `canHostCk` are both true:<br>- `dsh` is registered;<br>- a fake `dsh` binary, implementing the verified API surface recorded in the evidence, runs a bench arm on the bench fake project;<br>- the probe shows the workflow's `agent()` and `ck()` calls reached it;<br>- the result has the 17.5 shape.<br>The real equity E2E on `dsh` is TC-166. | real bench + fake dsh | `tests/test_dsh_adapter.py` |
| TC-156 | 18.3 | int-cli | If either is false: a findings report (in the evidence file) has `checked`, `version` and `missing`, `dsh` is not registered, and `--harness dsh` is refused with the registered list. | real CLI | `tests/test_dsh_harness.py` |
| TC-157 | 18.4 (error) | int-cli | When `dsh` is registered: with no `dsh` on PATH, and with a fake `dsh --version` printing an unsupported version, `ck doctor --harness dsh` reports it, and `ck bench run --harness dsh` refuses the arm (non-zero, with the reason). | real CLI, PATH | `tests/test_dsh_adapter.py` |
| TC-158 | 18.5 | int-cli | The whole suite passes, whatever the `dsh` outcome (the full verify run, TC-171). | full suite | `specs/ck.json` verify |

### Story 19: Lean thin ck agents

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-159 | 19.1 | int-wf | On the Claude Code path, every thin call carries the lean agent type: plan, land, relink, park, the attributed gates (periodic, recheck, final), the claim sweep and the audit. Task, review, fix, test-fix, cross-test and resolver calls never carry it. | real script | `tests/js/thin_agents.test.mjs` |
| TC-160 | 19.2 | int-cli | The lean agent file in `plugin/agents/` (the name the workflow passes) has frontmatter `tools: Bash` and no other tool, `model: haiku`, `effort: low` and `maxTurns` ≤ 5. For CLAUDE.md, exactly one of two states holds, read from `specs/unattended-correctness/claude-code-agent-keys.md`: (a) it names a documented Claude Code frontmatter key that omits CLAUDE.md, with its source, and the frontmatter sets that key to true; or (b) it records the gap (no documented key, the source checked, 19.2's CLAUDE.md part not available) and the frontmatter carries no CLAUDE.md key. A missing record, a key in the frontmatter that the record does not name as documented, or a documented key left out fails. Its vendored copy (`ck vendor` into a temp project) carries the same frontmatter, and `claude plugin validate` passes (existing TC pattern). | real files, real vendor | `tests/test_lean_agent.py` |
| TC-161 | 19.3 | int-wf | Every thin call's `opts.schema` is deep-equal to every other. | real script | `tests/js/thin_agents.test.mjs` |
| TC-162 | 19.4 | int-wf + int-cli | (a) The args say the lean type is unavailable: thin calls run as today, with no agent type, and `result.warnings` says so. (b) The implement skill checks the vendored lean agent file and passes the flag: a doc test on `plugin/skills/implement/SKILL.md` and its vendored copy, plus `ck doctor` reporting a missing lean agent in a vendored project. | real script; real files | `tests/js/thin_agents.test.mjs`, `tests/test_lean_agent.py` |
| TC-163 | 19.5 (error) | int-wf | Thin answers that do not match the shared schema (missing `code`, `json` not an object, extra free text only) are treated as failed calls: plan uses its fallback path, land is not counted as merged, and the gate is an error (TC-25). | real script | `tests/js/thin_agents.test.mjs` |
| TC-164 | 19.6 | int-wf + int-ts | (a) On the OpenCode path (the `ck` global is present), the trace has no thin agent labels. (b) The real `runner.mjs`, running the real spec-implement with a `$CK_BIN` shim, calls `gate … --attribute --json` directly (argv recorded by the shim). | real script; real runner | `tests/js/thin_agents.test.mjs`, `opencode/test/runner.test.ts` |

### Story 20: Equity E2E reruns

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-165 | 20.1 | int-cli | The committed bench configs for the 4 reruns:<br>- use the spec ref `fix/mtr-spec-issues`;<br>- have `land.allow_paths` with exactly the 12 reviewed slash-command paths listed in 20.1 (compared as a set with that list);<br>- do not cover `.claude/CLAUDE.md` with any glob (`protected_changes` on a synthetic CLAUDE.md change, with that allow-list, still returns it).<br>OpenCode arms have no `roles` (20.4). | real config + real `protected_changes` | `tests/test_unattended_e2e_evidence.py` |
| TC-166 | 20.2-20.7, 2.13, 18.2, 9.10, 9.11 | manual + evidence check | Each rerun is recorded in `e2e-reruns.json` with: arm, harness, model, run ID and its passes (`passes`, each started with that run ID, 2.13), tasks Done/total, the final attributed gate's state, full-suite result (passed, failed, errors), `handRepairs: []`, `approvals` (Tasks 93 and 94 only), hooks at start and the audit at end, cost per merged task, wall minutes per task and the baseline values. The test asserts:<br>- 4 arms (or 3, plus the `dsh` findings when 18.3 applies);<br>- every task Done;<br>- failed + errors = 0;<br>- no hand repairs;<br>- cost and wall at or below the baseline;<br>- every failed bar names its criterion and run ID (20.7);<br>- the final gate is green and ran after the last merge of the last pass (2.11, 2.13);<br>- `sandboxTests` records TC-88 and TC-189 as `passed` (not `skipped`) on a host with namespaces (9.11). | evidence file | `tests/test_unattended_e2e_evidence.py` |
| TC-167 | NFR thin-agent context | manual | From the Claude Code rerun's records (`ck stats show <run> --json`), the first request's cache-write tokens of a lean thin agent are at least 50% below a default subagent running the same ck command, from a paired control run. Recorded in `e2e-reruns.json` and checked by TC-166's test. | real run records | `tests/test_unattended_e2e_evidence.py` |

### Cross-cutting and NFR

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-168 | NFR no network | int-cli + int-git | With `no_network`, these run with the network log empty:<br>- `ck gate --attribute --json`;<br>- `ck graph`, `ck lint` and `ck relink --apply`;<br>- a hooked raw `git merge`;<br>- `ck worktree land`, `ck worktree audit` and `ck worktree sweep`;<br>- `ck fixtures status`, `ck fidelity` and `ck testfix check`. | real CLI + netguard | `tests/test_unattended_no_network.py` |
| TC-169 | NFR attribution cost | int-cli (serial) | Given a parsed result of 2,000 tests and 200 failures, and a 100-task spec (generated tasks.md and graph, built before the clock starts). `attribute()` plus settling takes under 2 s. Also, with the run's pytest output pre-recorded by a stub verify command that prints it, `ck gate --attribute --json` minus the stub's own time is under 2 s. | real seam + real CLI | `tests/test_unattended_perf.py` |
| TC-170 | NFR hook cost | int-git (serial) | On `equity_sized_repo`, the median of 20 `git update-ref refs/heads/x` with hooks, minus the same without hooks, is under 50 ms. For `integrate/demo` (allowed change), it is under 1 s. | real git | `tests/test_unattended_perf.py` |
| TC-109 | NFR graph build | int-cli (serial) | For 100 tasks and 200 test files, `ck graph demo --json` wall time minus `ck --version` wall time is under 3 s. | real CLI | `tests/test_unattended_perf.py` |
| TC-171 | NFR backward compatibility, 18.5 | full suite | Both `specs/ck.json` verify commands pass. Only tests pinning superseded behaviour (requirements' Supersedes list) are changed, each in its superseding task. | full suite | all |
| TC-172 | Constraints (vendored copies) | int-cli | `tests/test_plugin.py::test_dogfood_copies_in_this_repo_match_the_plugin_source`, `test_claude_vendored_workflow_matches_plugin_source` and `test_opencode_vendored_workflow_matches_plugin_source` pass with the changed workflows and the new agent. | real files | `tests/test_plugin.py` |

### Property suites (each runs the P rows named; names carry the TC ID)

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-173 | see P-1 to P-6 | prop | Attribution, TC extraction, determinism, truncation, kinds and reruns. | seams + pytest oracle | `tests/test_gate_attribution_properties.py` |
| TC-174 | see P-7 to P-11, P-24, P-25, P-30, P-31, P-33, P-34 | prop | Workflow decisions through the bridge. | real script | `tests/test_unattended_workflow_properties.py` |
| TC-175 | see P-12 to P-23, P-26, P-27, P-29, P-32, P-35, P-36 | prop | Classifiers, the protect decision, the token and audit state machines, the graph, lint, overlap, claims, the registry and the fixture lock. | seams + real git | `tests/test_unattended_properties.py`, `tests/test_protected_hooks_properties.py` |
| TC-180 | see P-28 | prop (table) | OpenCode role references. | real config.ts | `opencode/test/roles.test.ts` |

### End-to-end smoke (critical paths, real ck under the real workflow)

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-176 | 2.2, 2.6, 2.11, 4.4, 3.6 | e2e | A real `attr_project` with 4 tasks. The workflow runs with `realCk` (the OpenCode path) and scripted `real_agents.mjs`: Task 2's first commit breaks `tc1`, and the fixer's commit repairs it. Then real `ck worktree land` merges, the real `ck gate --attribute --json` at merge 2 (N=2) is red with tc1 attributed to Task 2, the fixer gets that entry, the recheck is green, and the final real gate is green with no excused entries. | workflow + real ck + real git + real pytest | `tests/test_unattended_smoke.py` |
| TC-177 | 4.1-4.4, 4.10 | e2e | A real repo where test task 1's `tests/test_calc_tc.py::test_tc2_sub` asserts an unreachable value. The scripted Task 2 agent reports a conflict, the confirmation agrees, and the scripted test-fixer commits a correct test on the test-fix branch. Then the real `ck testfix check` and `prove` run, the scripted risky review approves, the real `ck worktree land` merges the fix, Task 2 is retried and merges, and the final real gate is green. | same | `tests/test_unattended_smoke.py` |
| TC-178 | 9.2, 9.6, 9.8, 10.1 | e2e | During the TC-176 run, a scripted agent attempts a raw `git merge --no-verify` of a protected branch into the integration worktree. It is refused, the workflow continues, and the run-end real `ck worktree audit` is clean. | same + hooks | `tests/test_unattended_smoke.py` |
| TC-179 | 12.1, 12.2, 12.5 | e2e | Test task 1 invokes `mtr thesis new` (from Task 5, the fixture click CLI). Then the real `ck lint` reports the edge, the real `ck relink --apply` adds it, and in a second scenario without relink, the run adds the live edge at runtime and Task 2 merges after Task 5. | same | `tests/test_unattended_smoke.py` |

## Properties

All Python properties use Hypothesis with `@settings(derandomize=True, max_examples=200, database=None, deadline=None)` unless noted. Bridge-driven ones use `max_examples=60`. Real-git or real-pytest ones use `max_examples=25` (stateful: `stateful_step_count=8`). TS properties are table-driven, because the repo has no fast-check.

| ID | Requirement | Property | Generator domain | Expected decision |
|---|---|---|---|---|
| P-1 | 1.1, 1.2, 1.3, 1.5 | For any spec graph, Done set, gated set and failure list: a failure is `excused` iff at least one attributed task is outside Done∪gated, and `excusedBy` is exactly those tasks. It is a regression candidate iff it has tasks and all are in Done∪gated. It is `unattributed` iff it has no tasks, and an unattributed failure is never excused. Sub-case P-1a (1.1): for a failing node with a TC ID, (selected) when at least one impl task's Verify selects the TC, `tasks` is exactly the set of selecting tasks, and a task that only its test task's Verifies lists is never added, so it cannot excuse the failure; (fallback) when no Verify selects the TC, `tasks` is exactly the covering test task's Verifies. Sub-case P-1b (1.5): a `hint` never changes `tasks`, status or `excusedBy`. | Tasks 1-30, with non-contiguous multi-digit IDs. TC IDs 1-120. Each impl task's Verify uses one selector form from P-3, or no `-k`. Test tasks own 1-3 files and Verify 0-4 tasks. Failures are drawn from nodes in owned files, nodes in unowned files (including wrapper nodes whose parameter ID names an owned file), and file-level entries. TC IDs are drawn both selected by some Verify and covered only by a test task, with the covering test task's Verifies a strict superset of the selecting tasks. Done and gated are subsets. | Seam `attribute()`. The three statuses partition the failures. P-1a: `tasks` equals the selecting set when it is non-empty, else the covering test task's Verifies, computed independently from tasks.md. |
| P-2 | 1.2, 1.3 (metamorphic) | Adding tasks to Done can only move a failure from excused to regression candidate. It never moves a regression candidate to excused, and it never changes `unattributed`. Removing tasks from Done moves only in the other direction. | P-1's domain plus a random superset of Done. | Monotone move only. |
| P-3 | 1.1, 1.4, 1.8 | For any node ID, the TC IDs read are exactly the `tc<digits>_` tokens of the test function name (case-insensitive, with the numeric value normalised so that `tc012_` and `tc12_` both give TC-12). Tokens that appear only in the param ID, class name, file or directory are ignored. A task "selects" TC-n iff pytest's own `-k` evaluator (`_pytest.mark.expression.Expression`, oracle) accepts some name containing `tcN_` built only from tokens in the expression. Attribution's Verify path agrees with that oracle for every generated pair. | Node forms: `path::func`, `path::Class::func`, `path::func[param]` (with `tc7_` inside the param), `sub/dir/test_tc3_file.py::func`, multiple tokens (`test_tc3_tc4_x`), `TC7_` uppercase, `tc012_` padded, `tc12x_` (near-miss, no token), `atc1_` and `xtc1_` (the token inside a word), the `tc1_` prefix of `tc12_` (`test_tc12_` never yields TC-1). Selector forms: `-k "tc3_"`, `-k 'tc3_'`, `-k tc3_`, `-ktc3_`, `-k "tc3_ or tc4_"`, `-k "(tc3_ or tc4_) and not slow"`, `-k "not tc3_"`, `-k "tc3_ and tc4_"`, two `-k` occurrences in one Verify (pytest keeps the last), and several commands in one Verify line. | Token set equals the oracle. Selection equals pytest's evaluator. |
| P-4 | 1.10, NFR determinism | For any P-1 input, two calls give identical output, and any permutation of the input failure order (as xdist produces) gives the same output, in the same order. | P-1's domain plus permutations. | Equal lists. |
| P-5 | 3.2, 3.5, NFR message limit | (a) For any message, `truncate` returns at most 500 characters, the output is a prefix of the input when not truncated, and `truncated` is true iff the input is over 500 characters. The output never splits a surrogate pair or a grapheme, and is never empty when the input is not. (b) Through the real CLI, over generated failing tests, every entry's `kind` is one of the five kinds, the number of entries equals the number of failing nodes plus failing files, and none is dropped (`max_examples=25`). | (a) Text from `st.text()` with emoji, CJK, combining marks, `\r\n`, NUL and ANSI escapes, at lengths 0, 1, 499, 500, 501 and 5,000. (b) Exception classes: builtins, `AssertionError` subclasses, near-miss names (`MyAssertionError(Exception)`, `AssertionErrorX`). Phases: collect, setup, call, teardown. 1-6 tests per file. | (a) Length bound and flag correct. (b) Kind in the set, and the counts equal. |
| P-6 | 2.4, 2.5, NFR rerun bound | For any attributed list and rerun outcomes: the rerun targets are exactly the regression candidates and unattributed failures, each once. Excused failures are never targets. After settling, a target that passed on rerun is `flaky`, and one that failed is `regression` (or stays `unattributed`). The gate is red iff any regression or unattributed failure remains. | P-1's output, with random pass/fail rerun outcomes per node. | Targets as defined. Red iff regression or unattributed. |
| P-7 | 2.6, 2.8, 2.9, 2.12, 19.5 | For any thin or direct gate output, `gateVerdict` is `error` unless the output is a JSON object whose `ok` is a boolean and which is either a `ck-gate/1` object whose `failures` is an array of schema-valid entries, or a `ck-gate-compact/1` object whose `regressions`, `unattributed` and `flaky` are arrays of schema-valid entries and whose `excusedByTask` maps task IDs to non-negative integers. When valid, it is red iff some entry is `regression` or `unattributed`, and green otherwise. A malformed or failed call is never green (complement). | JSON values: null, numbers, strings, arrays, `{}`, objects missing `failures`, `failures` as an object or a string, `ok` as a string, statuses outside the four, `code` ≠ 0 with valid JSON, entries missing `node` and `file`, thin envelopes with `json: null`, compact objects with `regressions` as an object, `excusedByTask` values negative or strings, and a compact object with a `failures` key instead of its own lists. | `error` for every malformed input, otherwise red or green by the rule. |
| P-8 | 3.7 | For any agent result, `conflictsOf(r)` returns the conflicts list. A single `testConflict` is equivalent to `testConflicts: [it]` (metamorphic: wrapping it gives an equal output). Both present gives the union, deduplicated by (file, test). Entries without `file` are reported as malformed, never silently dropped. | `testConflict` and `testConflicts` present or absent, empty arrays, duplicates, entries missing fields, non-object entries. | Normalised list as defined. |
| P-9 | 3.8 | For any failure list of length n, `failureDigest(list, 50)` lists the first min(n, 50) entries in input order. Per-file counts of the rest sum to n − 50 when n > 50. No entry is both listed and counted. | 0-300 entries over 1-10 files, with duplicate nodes. | As defined. |
| P-10 | 4.5 | For any set of changed paths, the scope check allows the fix iff every path is a test file or a test fixture of the spec (by the project's test globs and the test-track Files) and no changed test lost a TC ID it carried before the fix. Any change to `requirements.md` or `test-plan.md`, or to any non-test path, is a violation. Adding a new row to `test-plan.md` is a change, so it is refused too, and so is a fix that changes a test's TC token (a test that carried `tc52_` no longer does). Complement: adding a non-test path to an allowed set makes it refused. Adding a test path never turns refused into allowed. | Paths: `tests/test_x.py`, `tests/sub/conftest.py`, `tests/fixtures/a.json`. Near-misses: `src/tests.py`, `tests_old/x.py`, `src/tests/fixtures.py`, `specs/demo/test-plan.md`, `specs/demo/requirements.md`, `specs/other/test-plan.md`, `.claude/x`, `pyproject.toml`, `conftest.py` at the root (allowed only when the test globs match it). Non-ASCII and space-containing names, and git-quoted forms (`"tests/\303\244.py"`). | Allowed iff every path is a test or fixture path and no changed test lost a TC ID. |
| P-11 | 4.8, 4.9 | For any conflicts and `priorFixes`: every conflict appears exactly once, either in a fix group or in `toUser`. No node in `priorFixes` is in a fix group. Fix groups are exactly one per file. Complement: removing a node from `priorFixes` never moves another node to `toUser`. | 0-20 conflicts over 1-5 files, `priorFixes` drawn from the conflict nodes plus unrelated nodes, and duplicate conflicts. | Partition as defined. |
| P-12 | 5.2, 5.3, 5.4, 5.7 | For any failure in a not-built-yet form, the classifier returns `not-built-yet`. For any failure in a wrong-reason form, it returns `wrong-reason`. Everything else is `unclear` or `not-built-yet`, never `wrong-reason`. Only `wrong-reason` blocks. A test-body `AssertionError` is `wrong-reason` iff its failing assert runs no project code and uses no name imported from the project's own code (PLAN D19, V2); adding a project import or project call to the assert's dependencies turns it `unclear`. Metamorphic: ANSI colour codes, `\r\n`, an indented traceback and surrounding pytest noise never change the class. | Not-built-yet: `ModuleNotFoundError: No module named 'calc.sub'` (a project package), `ImportError: cannot import name 'x' from 'calc'`, `AttributeError: module 'calc' has no attribute 'x'`, click `No such command 'x'.` and `No such command “x”.` (click 8.2 quotes), argparse `invalid choice: 'x' (choose from …)`, `unrecognized arguments: --x`, `No such option: --x`, and `NotImplementedError` raised in project source. Wrong-reason (5.3, 5.7): an `AssertionError` from an assert in the test file whose expression depends only on literals and data loaded from files (through assignments in the test function, `json.load`, `open`, `pathlib` reads), a `ValueError` or `ValidationError` raised during fixture setup, and `ModuleNotFoundError` for a module under the test task's own Files. Unclear (5.4, 5.7): an `AssertionError` whose assert expression reaches a project import, a call into project code or a pytest fixture argument (directly or through an assignment), an assert line that cannot be parsed, third-party missing modules (`numpy`), `KeyError` and `TypeError` in the test body, and `NotImplementedError` raised from test code. | As defined. |
| P-13 | 6.5, 15.8 | For any JSON value of `test_track.validate`: accepted iff it is a string or a list of strings, and otherwise a config error naming the key. The same rule applies to the fixture globs key (P-32 uses it). | `st.recursive` JSON: strings, empty string, lists with mixed types, nested lists, dicts, numbers, bools, null. | Accept iff the type rule holds. |
| P-14 | 8.1, 8.2 | For any row value in any surface form, a test literal of equal value matches, and a literal of different value is a mismatch. Both ends of a range are checked. Metamorphic: replacing a matching literal with a different value moves match to mismatch, never the reverse, and reordering the tests in a file does not change the result. | Numbers: `4.15`, `$4.15`, `4.15 USD`, `USD 4.15`, `4,150`, `4.150`. Ranges: `4.15 to 4.50`, `4.15-4.50`, `between 4.15 and 4.50`, `4.15/share`. Percentages: `15%` against `0.15`. Dates: `2026-01-31`, `Jan 31, 2026`, `31 January 2026`, `date(2026, 1, 31)`, `"2026-01-31T00:00:00Z"`. Enums: `BUY`, `buy`, `Buy`, `Side.BUY`, `"BUY"`. Negatives: the other end of the range, a value one cent or one day off, a member of a sibling enum in the same design (e.g. `Status.OPEN` for a `Side` field), and a row quantity used as a price. | Match iff equal value, with both range ends checked. |
| P-15 | 8.3, 8.4, 8.5 | Every extracted item lands in exactly one of matched, mismatches, unparsed or unmatched. The reviewer items equal unparsed ∪ unmatched, so no matched or mismatched item reaches the reviewer. All matched means no reviewer items. | P-14's domain plus runtime expressions (`x * 1.1`, `f(y)`), tests without a TC ID, and TC IDs without a row. | Partition. Reviewer set as defined. |
| P-16 | 8.6 | For any inputs, the check seam takes strings and returns a value: the test-plan, design and test strings are unchanged (identity-compared copies). TC-74 covers the CLI's files. | P-14's domain. | Inputs unchanged. |
| P-17 | 9.2, 9.4 | For any ref name, changed-path set and allow-list, `refuse_paths(ref, changed, allow)` is empty for any ref outside `refs/heads/integrate/`. For integrate refs, it is exactly the changed paths that match a protected pattern and no allow glob. Complements: a protected path that is not allowed is always refused, and a refusal is never empty when one exists. Metamorphic: adding a changed path never turns a refusal into a pass, and adding an allow glob only removes the paths it matches. | Refs: `refs/heads/integrate/demo`, `refs/heads/integrate/a/b`, and the near-misses `refs/heads/integrate`, `refs/heads/integrated/x`, `refs/heads/feat/integrate/x`, `refs/remotes/origin/integrate/x`, `refs/tags/integrate/x`, `HEAD`. Paths: `.claude/x`, `a/.claude/x`, `.mcp.json`, `a/.mcp.json`, `.githooks/x`, `.husky/x`, `.git-hooks/x`, `.pre-commit-config.yaml`, `.envrc`, `specs/ck.json`. Near-misses: `claude/x`, `.claude-notes/x`, `specs/ck.json.bak`, `specs/demo/ck.json`, `x.mcp.json`, `.envrc.example`. Also non-ASCII, space-containing and git-quoted (`"\303\244"`) forms of each. Allow globs: `.claude/commands/**`, `.claude/**`, `**/.mcp.json`, and an empty list. | As defined. |
| P-18 | 9.2, 9.3, 9.7 | Stateful, on real git and real ck (`max_examples=25`, ≤8 steps). Over any interleaving of allowed `ck worktree land`, raw update attempts (update-ref, merge `--no-verify`, reset) to protected or clean commits, and `restore_ck_state` of any earlier snapshot, `integrate/demo` only ever points at commits whose protected changes from its previous value are allowed or that ck itself landed. A ck landing that passed its check always succeeds. | Rules: land(allowed task), land(clean task), raw(kind, target ∈ {protected, allowed, clean, previous}), restore(snapshot k), reset-back. | Invariant holds after every step. |
| P-19 | 10.1, 10.4 | Stateful, on real git (`max_examples=20`). After any mix of approved landings and bypass writes, the audit's drift equals the set of protected paths whose content differs from the last approved state, each with the right add, change or delete kind. Approved changes never appear. | Rules: allowed land, approval land, ref-file bypass adding, changing or deleting one of 6 protected paths, a clean commit. | Drift equals the oracle set. |
| P-20 | 11.4, 11.2 | For any invocation of a command path from the CLI table, in any form, the invokes edges equal the longest table command that prefixes the invoked argument list. Global options before the subcommand are skipped. An option value equal to a subcommand name never matches. Near-miss tokens never match. | Forms: `run_cli(["thesis","new",…])`, `main(["thesis","new"])`, `CliRunner().invoke(cli, ["thesis","new"])`, `subprocess.run(["mtr","thesis","new"])`, `subprocess.run("mtr thesis new --x 1", shell=True)`, `shlex.split("mtr thesis new")`, f-strings with a constant prefix, `["--verbose","thesis","new"]`. Near-misses: `["thesis","newer"]`, `["thesis-new"]`, `["express","--name","thesis"]`, the string `"thesis new"` in an assert message, a comment `# mtr thesis new`, and another program (`git thesis new`). | Edge set equals the oracle. |
| P-21 | 11.5, 11.7, NFR determinism | For any generated spec, `build_graph` run twice gives equal JSON. After any edit sequence with mtimes restored, an incremental build equals a from-scratch build in a fresh copy of the files. | Specs of 1-30 tasks, 1-20 test files and 1-10 source files. Edits: add or remove a task, change a Verify, add an invocation, rename a file, then `os.utime` back. | Equal outputs. |
| P-22 | 11.8 | For any spec with injected malformed sources, the graph's nodes and edges from well-formed sources equal the graph of the well-formed subset, and `problems` names every bad source exactly once. | Malformations: a task header with no number, duplicate task numbers, `Dependencies: Task` with no number, a test file with a syntax error or a non-UTF-8 byte, an empty design CLI table. | As defined. |
| P-23 | 12.1, 12.2 | For any graph, lint reports exactly the pairs (verified task V, provider P) where a test of a test task verifying V invokes a command P provides, P ≠ V, and P is not in V's transitive dependencies. After `relink --apply`, lint reports none of them, unless adding the edge would make a cycle (then it is reported, not added). A second apply changes nothing. | Random DAGs (`dags()`), provides maps, invokes maps (including a test task verifying several tasks), and a self-providing task. | As defined. |
| P-24 | 12.5, 12.6, 12.7 | For any failure, provides map, deps and done set, `liveEdge` adds (V → P) only if the failure is a missing-command failure, P is unfinished and provides the command, and the edge creates no cycle. If no task provides the command, it returns a conflict. If the edge would cycle, it returns blocked with the cycle. Complement: non-missing-command failures never produce an edge, and the deps after the call stay acyclic. | Message forms from P-12's CLI group plus near-misses (`git: 'x' is not a git command`, an assertion message quoting "invalid choice"), random DAGs, and providers that are Done, unfinished, the task itself or absent. | As defined. |
| P-25 | 13.1, 13.2, 13.5 | Bridge (`max_examples=60`). For any scenario with write-sets, no two tasks whose write-sets overlap are in flight at once (task start to land end, from the trace). Among overlapping tasks that are ready together, the first to start is the head of the priority order. Every task that merges with write-sets off also merges with them on (no stall). | `scenarios()` plus write-sets drawn from a universe of 12 paths and globs (`src/a.py`, `src/*.py`, `src/**`, `docs/x.md`, `./src/a.py`), slots 1-6, durations and failures. | Invariants hold. Merge sets are equal. |
| P-26 | 13.1 | For any two write-sets, `write_overlaps` reports overlap if some path in a 40-path universe (which includes every literal in either set) matches both. Two literal sets with no common normalised path never overlap. The relation is symmetric. | Literals with `./`, backticks, trailing ` - description`, trailing `/` directory forms. Globs `*`, `**` and `?`. Near-misses `src/a.py` and `src/ab.py`. | One-directional witness rule, plus the disjoint-literals rule. |
| P-27 | 16.2, 16.3, 16.4, 16.5, 16.7 | For any claim (run ID present or absent; run state running, ended, stale or missing; claim owned by the starting run or not; age 0-300 min; stale threshold 1-120 min), the decision is: `own` if the run is the starting run; `keep` if the run is running; `release` if the run is ended, stale or missing; and, with no run ID, `release` iff age > threshold. Complement: a running run's claim is never released, whatever its age. | The full cross product, with ages around the threshold (59, 60, 61). | Truth table. |
| P-28 | 14.1, 14.4 | Table-driven in TS over the enumerated domain: a role reference is accepted iff the role is one of the 7 names exactly and the value parses as `provider/model` with an optional non-empty `#variant`. Otherwise `ConfigError` names `opencode.roles.<role>` and the value. | Roles: the 7 names, plus `Implementer`, `test-writer`, `reviewer-risky`, `" reviewer"` and `""`. Values: `opencode-go/deepseek-v4.1-flash#medium`, `opencode-go/muse-spark-1.3-contributor`, `p/m#`, `/m`, `p/`, `p`, `p/m#a#b`, `" p/m "`, `""`, `42`, `null`, `{}`. | Accept iff valid. Error text names the key and value. |
| P-29 | 17.1, 17.2 | For any harness name, the lookup succeeds iff it equals a registered name exactly. Otherwise the error lists every registered name. Through the CLI, `--harness` accepts exactly the same set. | Registered names, case variants (`Claude`), whitespace (` claude`), near-misses (`claude-code`, `open-code`, `opencode2`, `dsh` when unregistered), and the empty string. | Accept iff exact. |
| P-30 | 19.1, 19.3, 19.6 | Bridge. For any scenario on the Claude Code path, every thin call has the lean agent type and the shared schema, and no other call has the lean type. On the OpenCode path, no thin agent is requested. | `scenarios()` × {ck global present, absent} × lean available {yes, no}. When it is not available: no lean type, and a warning. | As defined. |
| P-31 | 2.1, 2.11 | Bridge. For any scenario and N ≥ 1, an attributed gate is requested exactly when the merge count crosses a multiple of N, plus one final gate after the last merge (when every task is Done). There are none with N = 0, apart from the final gate. | `scenarios()`, N in 0-5, merge batch sizes 1-4. | Gate positions equal the oracle. |
| P-32 | 15.2, 15.3, 15.4 | For any sequence of landings that change locked or unlocked files and of handcraft marks (reason empty, whitespace or non-empty), the reported drift is exactly the locked files changed without a mark that has a non-blank reason. | 1-8 events over 5 files (3 matching the globs), reasons drawn from `""`, `" "`, `"\t\n"` and real text. | Drift equals the oracle. |
| P-33 | 4.2, 4.6, 4.11 | Bridge plus seam. (a) `proof_decision`: proved iff every outcome passed or failed only as `not-built`; a `skipped`, `xfail`, `deselected` or missing node never counts as passed. (b) In the workflow, over every combination of scope ok/out, proof proved/failed and review approve/reject (2x2x2), and with answers that arrive late or contradict each other (a proof that says proved with a failing outcome), a `worktree land` of the test-fix branch is requested iff scope ok AND proved AND approve. Every other combination ends in needs-input and no land. | (a) Outcome lists of 0-8 nodes with statuses passed, failed (`not-built`, `bad`, `unclear`), skipped, xfail, deselected, missing. (b) The 8 answer combinations × delay 0-3 virtual ticks × a contradictory flag. | Land iff all three hold; proof as defined. |
| P-34 | 7.4 | Bridge. For any open cross-test findings and `testVerifies` map, `heldByCrossTest` returns exactly the impl tasks verified by the files of the tests the findings name. In the run trace no task in that set starts while its finding is open, and no task outside it is ever held by it. | 0-4 findings naming 1-5 tests by node ID, bare function name or file, across 1-3 test files; `testVerifies` with extra and missing entries; tasks 1-12. | Held set equals the oracle; trace invariant holds. |
| P-35 | 9.8 | `approval_decision(stdin_tty, stdout_tty, typed, expected)` is true iff both are TTYs and `typed` equals `expected` exactly after stripping one trailing newline. Complement: any other combination is false. | Both TTY flags; `expected` 6 random characters; `typed` drawn from the exact code, the code with leading/trailing spaces, other case, one character changed, a prefix, the code twice, `"yes"`, `""`, and non-ASCII look-alikes. | Truth table. |
| P-36 | 10.5, 10.3 | `audit_decision`: whenever the approved state is missing, unreadable, of another spec or ref, or fails its mac, `complete` is false and `clean` is false; `clean` is true only when complete, with no drift, hooks installed, active and intact, and no fixture drift or lock tampering. | Approved state: None, corrupt mapping, wrong spec, wrong ref, mac failing, valid; drift 0-3 files; every `HookStatus` flag combination; fixture reports with and without drift or tampering. | `complete=false ⇒ clean=false`; clean iff all conditions. |

## Harness Contract

These are the names the tests rely on. If the design renames one, the test tasks follow the design, and the review must check that every row above still maps to it.

1. **`ck gate SPEC [--task N]... [--json] [--attribute]`** (1.x, 2.4, 3.x):
   - The text output without `--json` is unchanged.
   - `--json` prints one object: `{spec, tasks, ok, checks: [{name, ok, detail, warning}], failures: [...], warnings: [str]}`.
   - With `--attribute` it adds `regressions: int`, `excused: int` and `flaky: int`, and `report` (design: the path of the file holding the full object, 2.8).
   - `--json --compact` prints the compact object of 2.12 instead (design `ck-gate-compact/1`) and writes the full object to the file it names (TC-183).
   - The exit code is 0 iff `ok`, and 1 otherwise. A config error exits non-zero and names the key in a failing check.
   - `--attribute` works with and without `--task`.
   - Reruns are done by ck itself, inside the same invocation.
2. **Failure entry** (`tests/fixtures/unattended/failure.schema.json`):
   - `{node: str|null, tc: "TC-<n>"|null, tasks: [str], file: str, kind: "assertion"|"import"|"collection"|"timeout"|"error", message: str (≤500), truncated: bool}`.
   - Optional: `missingCommand: str` (12.5) and `rightReason: "not-built-yet"|"wrong-reason"|"unclear"` (5.x, test-track gates).
   - With `--attribute`: `status: "regression"|"excused"|"flaky"|"unattributed"`, `excusedBy: [str]` and `attributedBy: "tc"|"file"|"none"`, plus optional `hint: str` on an `unattributed` wrapper entry whose parameter ID names an owned file (1.5).
   - The same object appears in `ck worktree land --json` (`gates[].failures`) and in every workflow prompt or result that carries failures.
3. **Python seams:**
   - `claude_kiro.graph.build_graph(project_dir: Path, spec_dir: Path) -> SpecGraph`, where `SpecGraph.to_json() -> {"nodes": [{"id", "kind"}], "edges": [{"from", "to", "kind"}], "problems": [{"source", "problem"}]}`. Node ids are `task:<n>`, `tc:<n>`, `file:<path>` (kind `test-file` or `source-file`) and `command:<path words>`.
   - `claude_kiro.graph.invoked_commands(source: str, commands: Sequence[str], programs: Sequence[str]) -> List[str]`.
   - `claude_kiro.graph.write_overlaps(owns: Mapping[str, Sequence[str]]) -> Dict[str, List[str]]`.
   - `claude_kiro.attribution.tc_ids(node_id: str) -> List[str]` (`"TC-12"` form).
   - `claude_kiro.attribution.attribute(failures: Sequence[Mapping], graph: SpecGraph, done: Collection[str], gating: Collection[str] = ()) -> List[dict]`. Inputs are `{node, file, kind, message}`, and the output adds `tc`, `tasks`, `status` (`regression` for candidates, before settling), `excusedBy` and `attributedBy`. The order is canonical and independent of the input order.
   - `claude_kiro.attribution.rerun_targets(attributed) -> List[str]` and `claude_kiro.attribution.settle(attributed, rerun_passed: Mapping[str, bool]) -> List[dict]`.
   - `claude_kiro.failures.KINDS` (the 5-tuple), `claude_kiro.failures.MESSAGE_LIMIT == 500` and `claude_kiro.failures.truncate(text: str) -> Tuple[str, bool]`.
   - `claude_kiro.rightreason.classify(failure: Mapping, *, project_modules: Collection[str], cli_programs: Collection[str], own_test_files: Collection[str]) -> "not-built-yet"|"wrong-reason"|"unclear"`. `failure` carries `kind`, `message` and `detail` (the full failure text).
   - `claude_kiro.fidelity.check(test_source: str, test_plan_md: str, design_md: str) -> {"mismatches": [{test, value, expected|allowed}], "unparsed": [...], "unmatched": [...], "matched": [...]}`.
   - `claude_kiro.protect.refuse_paths(ref: str, changed: Sequence[str], allow: Sequence[str]) -> List[str]`.
   - `claude_kiro.testfix.scope_violations(project_dir: Path, spec_dir: Path, changed: Sequence[str]) -> List[str]`.
   - `claude_kiro.worktrees.claim_decision(claim_run: Optional[str], current_run: str, run_state: Optional[str], age_min: float, stale_min: float) -> "own"|"keep"|"release"`, where `run_state` is `running`, `ended`, `stale` or None (missing).
   - `claude_kiro.fixtures.drift(lock: Mapping[str, str], current: Mapping[str, str], marks: Mapping[str, str]) -> List[str]`.
   - `claude_kiro.harnesses.register(name: str, adapter)`, `claude_kiro.harnesses.names() -> List[str]` and `claude_kiro.harnesses.get(name) -> adapter` (raising an error that lists `names()`). The adapter has `run_arm(cfg, arm, echo) -> dict`, `doctor(project_dir: Path) -> int` and `check() -> Optional[str]` (None when usable).

   Every seam must be the one the CLI uses. TC-1, TC-14, TC-29, TC-50, TC-69, TC-76, TC-82, TC-104, TC-125, TC-143, TC-135 and TC-151 are its integration cases.
4. **New CLI surfaces** (all with `--json`, all offline):
   - `ck graph SPEC --json`;
   - `ck fidelity SPEC --task N --json`;
   - `ck testfix check SPEC BRANCH --json` -> `{allowed, violations}`;
   - `ck testfix prove SPEC --task N --branch B --json` -> `{ok, tests: [{node, outcome: "pass"|"fail-task"|"fail-test", failure?}]}`; for a cross-test fix with no blocked task (4.11, TC-185), design's form `ck testfix prove SPEC FIXID --held TASK ... --test NODE ... --json` -> `{scope, proof, changedTests, against}`, proving against the integration branch;
   - `ck testfix claim SPEC --node NODE --json` -> `{granted, previous}` (persisted per spec across runs);
   - `ck worktree audit SPEC --json` -> `{clean, complete, drift: [{path, change}], hooks: {installed, active, altered: [..], reason?}, fixtures: [...]}`;
   - `ck worktree sweep SPEC --run KEY --json` -> `{released: [{task, run}], kept: [{task, run, reason}]}`;
   - `ck fixtures status SPEC --json` -> `{lockFile, locked: [{path, sha256}], drift: [...], unlabeled: [...]}`;
   - `ck fixtures handcraft SPEC PATH --reason TEXT`;
   - an approval command (proposed `ck worktree approve SPEC N`) that must refuse without my input from outside the sandbox (TC-84, TC-95).
5. **Existing CLI extensions:**
   - `ck worktree claim … --progress-run KEY` records the run ID. `ck worktree status --json` shows it as `run`.
   - `ck worktree land/merge --json` gains `fixtureDrift` and the PROTECTED detail.
   - `ck worktree integration SPEC [--json]` reports `hooks: {installed, reason?}`.
   - `ck plan --json` gains `overlaps: {task: [task]}` and `provides: {command: task}`.
   - `ck lint --json` findings gain the kinds `no-tc-id` (1.9), `missing-command-dep` (12.1), `unknown-command` (12.3) and `no-write-set` (13.4).
   - `ck relink --apply` adds command edges.
   - `ck agents --json` shows `reviewer.cross_test` defaulting to true.
6. **Config keys** (in `specs/ck.json` or global config):
   - `test_track.validate`: str or [str];
   - `fixtures.recorded`: [glob];
   - `opencode.roles`: `{role: "provider/model[#variant]"}`;
   - bench arm `roles`: same shape;
   - the existing `fullGateEvery` workflow arg as N.
7. **Workflow (`plugin/workflows/spec-implement.js`):**
   - The decisions block exports `gateVerdict(out)`, `thinResult(out)`, `failureDigest(failures, limit)`, `conflictsOf(result)`, `testFixAction(conflicts, priorFixes)`, `liveEdge(failure, provides, deps, done)` and `writeSetStart(ready, running, overlaps, priority)`. Each is self-contained, so `loadDecisions({extra})` can evaluate it alone.
   - Agent opts carry `role` (14.1) and, for thin calls, the lean agent type (tests read `opts.agentType`, or the key the design names after checking Claude Code's Workflow API).
   - Labels:
     - existing: `full gate @k`, `re-check k`, `final gate`, `fix k (…)`, `task N`, `review N`, `land …`, `plan k`, `relink N`, `park N`;
     - new: `test-fix <file>`, `test-fix review <file>`, `cross-test`, `audit`, `sweep`.
   - Args: `fullGateEvery`, `agents` (with `test_writer`, `reviewer.risky_*`, `reviewer.cross_test`), and `thinAgent: string|null` (the lean type name, or null when it is not available; 19.4).
   - Result fields: `gates[]` (`{at, state: 'green'|'red'|'error', green, regressions, excused, flaky, reason?}`; design: `excusedByTask` and `report`), `fullGates[]` and `finalGate` keeping `green` (TC-24), `testConflicts`, `testFixes[]`, `liveEdges[]`, `held` (or log lines), `audit`, `sweep`, `warnings[]`.
   - Under the `ck` global, direct calls run `gate … --attribute --json`, `worktree audit`, `worktree sweep`, `testfix *` and `fixtures status`, with `-C <dir>` first.
8. **Thin agent output schema (19.3):** one object `{code: integer, json: object|null, error?: string}`, shared by every thin call.
9. **Lean agent file:** `plugin/agents/<name>.md`, where `<name>` is what the workflow passes. Frontmatter as in TC-160 (Claude Code's keys `tools`, `model`, `effort`, `maxTurns`, and the no-CLAUDE.md key only when `claude-code-agent-keys.md` records it as documented; otherwise that file records the gap). It is vendored like the other agents.
10. **Hooks:**
    - Installed through ck. Tests never write them; they only remove, alter or chmod them to simulate damage.
    - A refused update leaves the ref unchanged, and stderr names the protected files.
    - Tokens and the approved state live where the design chooses. Tests reach them only through `snapshot_ck_state`/`restore_ck_state` and the CLI.
11. **OpenCode (TS):**
    - `loadConfig` accepts `opencode.roles` and records `sources["roles.<role>"]`.
    - Model resolution takes the call's `role`: a seam `resolveRoleModel(role, requestedModel, config, catalog) -> {ref, variant, source: "roles"|"alias"|"default"}` in `models.ts` or `config.ts`.
    - Run records carry per-agent `model` and `variant`.
    - The preflight result has `roles: [{role, model, source}]`.
12. **Bench:** the arm result dict has the same keys for every harness. `run_arm_opencode` honours `arm["roles"]`.
13. **Bridge:** `{op:'call', name, args}` and `{op:'run', …, realCk:{cmd, cwd, env}}` (To add, items 4 and 5).
14. **Evidence files:**
    - `specs/unattended-correctness/e2e-reruns.json`: `{baseline: {run, costPerMergedTask, wallMinPerTask}, arms: [{name, harness, model, run, passes: [{progressRun, runId}], tasksDone, tasksTotal, finalGate: {state, afterLastMerge}, suite: {passed, failed, errors}, handRepairs: [], approvals: [{task, kind}], hooksAtStart, audit, costPerMergedTask, wallMinPerTask, failedCriteria: []}], thinAgentContext: {leanCacheWrite, defaultCacheWrite, runs}, sandboxTests: {host, tc88: 'passed'|'skipped'|'failed', tc189: 'passed'|'skipped'|'failed'}}`. Every pass's `runId` equals the arm's `run`; cost and wall figures cover every pass of the arm.
    - `specs/unattended-correctness/dsh-verification.json`: `{repository, version, checkedAt, canHostAgent, canHostCk, missing: [], evidence: []}`.

## Critical Paths

1. **Attributed periodic and final gates catch a regression while the spec is in progress** (TC-1, TC-15-17, TC-18, TC-20, TC-22, TC-24, TC-27, TC-176):
   1. A task lands.
   2. The 10th merge triggers `ck gate --attribute --json`, run directly or by the thin agent.
   3. A failing test whose tasks are all Done is rerun once, and is still red.
   4. The fixer gets the D3 entry.
   5. The attributed recheck is green.
   6. The final gate after the last merge has no excused entries and is green.
2. **A bad test is repaired without me** (TC-34, TC-38, TC-41-44, TC-47-49, TC-177):
   1. The task agent returns `testConflicts`.
   2. The confirmation agrees.
   3. A test-fixer (test-writer role) commits on a test-fix branch from integration.
   4. `ck testfix check` passes.
   5. `ck testfix prove` passes against the blocked branch.
   6. The risky review approves.
   7. `ck worktree land` merges the fix.
   8. The blocked task retries and merges, and dependents start.
   9. The one-fix record is written.
3. **A test-track task lands only with right-reason, valid fixtures, faithful values and no cross-test conflict** (TC-50, TC-52, TC-55, TC-58, TC-59, TC-64, TC-65, TC-69, TC-71, TC-139):
   1. The test-track gate runs.
   2. The new tests fail only as not-built-yet.
   3. `test_track.validate` passes.
   4. Row fidelity finds no mismatch, and the reviewer gets only the unparsed items.
   5. The task lands.
   6. A cross-test pass finds nothing (or the test-fixer resolves it).
   7. Impl tasks start.
4. **Protected files cannot reach integration without ck's approval** (TC-75-88, TC-90-95, TC-96-101, TC-178):
   1. Integration creation installs the hooks.
   2. A raw `git merge --no-verify` is refused.
   3. `ck worktree merge` is PROTECTED.
   4. An allowed `ck worktree land` succeeds with a token.
   5. `.claude/CLAUDE.md` waits for my approval, which lands it.
   6. The run-end audit is clean, or reports any bypass.
5. **CLI dependencies are known before and during the run** (TC-102-104, TC-110-111, TC-114, TC-117-118, TC-179):
   1. `ck graph` sees `invokes mtr thesis new` and `provides` from Task 5.
   2. `ck lint` reports the missing edge.
   3. `ck relink --apply` adds it.
   4. If one was missed, the runtime missing-command failure adds a live edge, the task waits for Task 5, then merges.
6. **Write-set scheduling avoids conflicts without stalling** (TC-120-125, P-25):
   1. Overlapping ready tasks run one at a time, in priority order.
   2. Free slots go to disjoint tasks.
   3. Held tasks start as soon as their blocker merges or stops.
7. **Re-running a spec after a crash** (TC-142-148):
   1. The run start sweeps claims of ended, stale or missing runs.
   2. It keeps claims of live runs.
   3. Tasks with released claims are taken.
   4. Their commits are kept.
8. **Per-role models and harnesses** (TC-126-133, TC-150-157):
   1. `opencode.roles` sets the reviewer to DeepSeek.
   2. Doctor shows each role and its source.
   3. The run records each agent's real model.
   4. A bench arm with `roles` runs on the registry-selected harness.
   5. `dsh` is either registered and runs an arm, or refused with the findings.
9. **Unattended success bar** (TC-89, TC-165-167): four equity reruns (or three plus the `dsh` findings), each ending with every task Done and the full suite green, with zero hand repairs, cost and wall time no worse than the first E2E, and hooks active with a clean audit.

## Commands

All from the project root. Python uses `uv run`. JS suites also run through `tests/test_workflow_scripts.py`.

| Level / file | Command |
|---|---|
| Whole suite (parallel part) | `uv run pytest -q -n auto -m "not serial"` |
| Whole suite (serial timing budgets) | `uv run pytest -q -m serial` |
| Attribution (CLI) | `uv run pytest -q tests/test_gate_attribution.py` |
| Failure format | `uv run pytest -q tests/test_gate_failures.py` |
| Gate prompt commands, real CLI | `uv run pytest -q tests/test_attributed_gate_prompt.py` |
| Right reason | `uv run pytest -q tests/test_right_reason.py` |
| Fixture validation | `uv run pytest -q tests/test_test_track_validate.py` |
| Cross-test default | `uv run pytest -q tests/test_cross_test_default.py` |
| Row fidelity | `uv run pytest -q tests/test_row_fidelity.py` |
| Hooks | `uv run pytest -q tests/test_protected_hooks.py` |
| Hooks red team | `uv run pytest -q tests/test_protected_hooks_redteam.py` |
| Audit | `uv run pytest -q tests/test_protected_audit.py` |
| Spec graph | `uv run pytest -q tests/test_spec_graph.py` |
| Command deps, overlaps (CLI) | `uv run pytest -q tests/test_command_deps.py` |
| Test-fix CLI | `uv run pytest -q tests/test_testfix_cli.py` |
| Fixture lock | `uv run pytest -q tests/test_fixture_lock.py` |
| Claims by run ID | `uv run pytest -q tests/test_claims_run_id.py` |
| Harness registry | `uv run pytest -q tests/test_harness_registry.py` |
| DeepSeek Harness | `uv run pytest -q tests/test_dsh_harness.py tests/test_dsh_adapter.py` |
| Bench roles | `uv run pytest -q tests/test_bench_opencode_roles.py tests/test_doctor_opencode.py tests/test_opencode_record.py` |
| Lean agent files | `uv run pytest -q tests/test_lean_agent.py tests/test_plugin.py` |
| No network | `uv run pytest -q tests/test_unattended_no_network.py` |
| Perf budgets | `uv run pytest -q -m serial tests/test_unattended_perf.py` |
| E2E smoke | `uv run pytest -q tests/test_unattended_smoke.py` |
| E2E evidence | `uv run pytest -q tests/test_unattended_e2e_evidence.py` |
| Properties (attribution) | `uv run pytest -q tests/test_gate_attribution_properties.py` |
| Properties (workflow, bridge) | `uv run pytest -q tests/test_unattended_workflow_properties.py` |
| Properties (other seams, git) | `uv run pytest -q tests/test_unattended_properties.py tests/test_protected_hooks_properties.py` |
| JS: attributed gates | `node tests/js/attributed_gates.test.mjs` (or `uv run pytest -q tests/test_workflow_scripts.py -k attributed_gates`) |
| JS: test fix | `node tests/js/test_fix.test.mjs` (TC argv filter: `node tests/js/test_fix.test.mjs tc84_`) |
| JS: test-track routing, cross-test blocking | `node tests/js/test_track_routing.test.mjs` |
| JS: cross-test fixes | `node tests/js/test_fix_cross.test.mjs` |
| Right-reason red team | `uv run pytest -q tests/test_right_reason_redteam.py` |
| Claims red team | `uv run pytest -q tests/test_claims_run_id_redteam.py` |
| JS: command deps, write-sets | `node tests/js/command_deps.test.mjs` |
| JS: thin agents, audit, sweep | `node tests/js/thin_agents.test.mjs` |
| JS: spec-create Provides | `node tests/js/spec_create_provides.test.mjs` |
| OpenCode: roles | `cd opencode && node --test test/roles.test.ts` |
| OpenCode: records, preflight, runner | `cd opencode && node --test test/record.test.ts test/preflight.test.ts test/runner.test.ts` |
| OpenCode: all | `cd opencode && npm test` |
| One TC by ID (dogfood) | `uv run pytest -q tests -k "tc76_"` |

## Not Tested

- **The equity app's own bugs:** out of scope (PLAN). The reruns surface them, and the product's loops fix them.
- **Real LLM behaviour of each new agent:**
  - whether a real test-fixer writes a correct fix;
  - whether a real risky reviewer catches a weakened test;
  - whether a real lean Haiku agent copies JSON faithfully.

  Automated tests fake the agents. The equity reruns (TC-166) are the only check of real agent quality. Reason: nondeterministic and costly.
- **Exact `dsh` adapter behaviour beyond the recorded API:** the fake `dsh` (TC-155) follows the verified surface in `dsh-verification.json`. Behaviour of the real binary is checked only by the `dsh` rerun.
- **Per-runner node-level attribution for runners other than pytest:** out of scope (PLAN). Only file-level attribution (TC-8) and the fallback (TC-9) are tested.
- **Proving where a fixture came from:** out of scope (D9). Only drift and labelling are tested.
- **Protected-change enforcement on refs other than `integrate/*`:** deliberately not enforced (D6). TC-80 checks that such refs pass.
- **Bypasses that no git hook can see:** `git -c core.hooksPath=…`, deleting the hooks, and writing ref files directly. TC-92 asserts the audit reports them, not that they are prevented (see concerns).
- **The 50% thin-agent cache-write reduction as an automated test:** it needs real Claude Code runs. It is a manual figure, recorded as evidence (TC-167).
- **The precise wording of agent prompts:** tests check the commands, structured data and opts that prompts carry, not their prose.
- **The bwrap reproduction where namespaces are unavailable** (for example inside another sandbox, as in this session): TC-88, TC-189's `sandboxed` part and TC-190 (b) skip there with the reason. TC-87 and TC-189's `readonly_git_metadata` part, which always run, cover the read-only-metadata condition, including hooks installed before launch by `ck run`. A non-skipped run of TC-88 and TC-189 on a host with namespaces is recorded as evidence (`sandboxTests`, TC-166).
