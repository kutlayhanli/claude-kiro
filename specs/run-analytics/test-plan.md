# Test Plan: Run Analytics

**Created:** 2026-10-28
**Inputs:** [requirements.md](requirements.md), [PLAN.md](PLAN.md) (decisions 1-9 and D10-D17 binding). Written without design.md, as an
independent check on the implementation.

## Strategy

This feature is almost all boundaries: it reads Claude Code's on-disk run files and transcripts, writes a machine-local
store, reads and writes ck's layered config, writes files into a git checkout, runs `git` and `gh`, hooks into the
`ck run` subprocess flow, changes a Workflow script, and adds steps to three skills (implement, create, review; 3.2). So the plan is **integration-first**:

- **Integration (most cases).** Every criterion that touches the filesystem, config, git, `gh`, `ck run` or console
  output runs the **real `ck` CLI** (click `CliRunner` in-process, or `python -m claude_kiro.cli` as a subprocess when a
  TTY, PATH or exit status of a child process matters). It runs against a **real fake Claude home**: a
  `$CLAUDE_CONFIG_DIR` tree in `tmp_path`, shaped like the real run files and transcripts (keys taken from the 85 spec
  run files on this machine: `runId, workflowName, status, startTime, durationMs, args, result, workflowProgress[]`
  with agent entries `agentId, label, phaseTitle, model, startedAt, durationMs, attempt, tokens, state,
  promptPreview, resultPreview, lastToolSummary`; transcripts carry `cwd, gitBranch, sessionId, version, requestId`).
  The local store is a real `$XDG_DATA_HOME` in `tmp_path`. The claude-kiro checkout is a real git repo whose `origin`
  is `https://github.com/kutlayhanli/claude-kiro.git` and whose fetches and pushes go to a local bare repo
  (`url.<bare>.insteadOf`), so the fetch, branch, commit and push are real git, offline. ck reads the
  configured remote URL from git config, so the rewrite does not hide it.
- **Only things we don't own are faked:** `claude` (a fake script on PATH, as `tests/test_cost.py` and
  `tests/test_run.py` already do), `gh` (a fake script on PATH that records argv and body), `$EDITOR`, the TTY (a real
  pseudo-terminal from the stdlib `pty` module, not a patched `isatty`), and the network (blocked, never mocked into
  success, in the pytest process and in every `ck` child process through a `sitecustomize` guard). Never mocked: ck's
  own modules, the store, the config files, git.
- **End-to-end smoke tests** for the critical flows: run -> auto-collect; backfill -> show; collect -> export -> submit
  with seeded confidential strings (the leak test, 9.4, which is the main point of the feature).
- **Workflow-script tests (Node).** Story 11 changes `plugin/workflows/spec-implement.js`; it is covered in the existing
  stubbed-agent suite `tests/js/spec_implement.test.mjs` (run through `tests/test_workflow_scripts.py`). The same
  suites, run with a label-recording hook, provide the real label corpus for the role-parser check (5.3).
- **Static contract checks** only where the artifact is a prompt: the three SKILL.md files that run an allowlisted
  workflow (implement, create, review; 3.2) and the published schema document (7.7). These read the packaged and vendored text; there is no runtime to drive.
- **Unit tests** only for pure logic with branching: the label parser, enum normalization, machine-label validation,
  remote-URL matching, month derivation, issue codes, the leak scanner. Each is a decision seam (Harness Contract) and is
  mostly covered by **property tests** (Hypothesis), with example tests for named cases.
- Every test fails today because `ck stats`, the seams, the setting keys and the `resolves` field do not exist; each
  asserts on behaviour (files written, exit codes, output, PR argv), not on internals.

## Test Infrastructure

### Exists today
- `pytest` via `uv run pytest -q` (`pyproject.toml` dev group). No CI config in the repo; the gate runs locally.
- `tests/conftest.py`: autouse `_isolated_preferences` sets `CK_CONFIG_HOME` to a temp dir; `project` fixture (git repo
  on branch `feature`); `git()` helper.
- `tests/test_cost.py`: `usage_line()`, `budget_line()`, `write_jsonl()`, `home` fixture (a `CLAUDE_CONFIG_DIR` with a
  session, a run file and two agent transcripts), fake `claude` on PATH for `ck run`, `sandboxed` fixture.
- `tests/test_run.py`: `fake_home` (empty `$HOME`), `sandbox_tools()` (fakes `bwrap`/`socat` presence),
  `fake_claude()`.
- `tests/bench_helpers.py`: `invoke()` (CliRunner with env), `FAKE_RUN` (writes a run file + transcripts the way Claude
  Code does), `CK = [sys.executable, "-m", "claude_kiro.cli"]`.
- `tests/js/load_workflow.mjs` (`runWorkflow` with stubbed `agent`), `tests/js/spec_implement.test.mjs` (its
  `project()` fake supports `conflicts`, `redLands`, `failUntil`, `reviews`, `fullRed`, `recheckRed`, ...),
  `tests/js/spec_create.test.mjs`; run by `tests/test_workflow_scripts.py` (skipped without `node`).
- `tests/test_plugin.py::test_dogfood_copies_in_this_repo_match_the_plugin_source` and
  `::test_vendored_rendering_has_no_plugin_placeholders_and_local_agent_types` (packaged vs vendored copies).
- `tests/test_gate_properties.py`: `ck gate` requires each `P-n` below to be a key of a `PROPERTIES` map in a test file.

### To add
1. **Dependency:** `uv add --dev hypothesis` (not in `uv.lock` today).
2. **`tests/conftest.py` additions (autouse, all tests):**
   - `_isolated_data_home`: `XDG_DATA_HOME=<tmp>/xdg-data`. Required globally because `ck run` (used by existing
     `test_run.py`, `test_cost.py`, `test_bench.py` tests) will now auto-collect, and must never write to the
     developer's real `~/.local/share`.
   - `_no_real_claude_home`: `CLAUDE_CONFIG_DIR=<tmp>/empty-claude-home` unless a test sets its own.
   - `_isolated_git_identity`: `GIT_CONFIG_GLOBAL=<tmp>/gitconfig` with `user.name`/`user.email` set per test, and
     `GIT_CONFIG_NOSYSTEM=1`.
3. **`tests/conftest.py` fixture `no_network`** (opt-in): patches `socket.socket.connect`/`connect_ex` and
   `socket.create_connection` in the pytest process to raise `AssertionError("network")` (the socket is not ours),
   and reaches child processes too: `tests/netguard/sitecustomize.py` patches the same three calls for `AF_INET` /
   `AF_INET6` sockets (Unix sockets stay allowed) to append one line to `$CK_TEST_NET_LOG` and raise; the fixture puts
   `tests/netguard` first on the child `PYTHONPATH` and sets `CK_TEST_NET_LOG=<tmp>/net.log`, so every
   `python -m claude_kiro.cli` child imports the guard at start-up. It also sets `GIT_ALLOW_PROTOCOL=file` and
   `HTTPS_PROXY=HTTP_PROXY=http://127.0.0.1:9` for non-Python children (git, gh). A test asserts the log is empty or
   absent at the end. Used by every stats test file via `pytestmark = pytest.mark.usefixtures("no_network")`. A
   harness self-check proves the guard reaches children: under the fixture,
   `python -c 'import socket; socket.create_connection(("1.1.1.1", 80))'` fails and leaves one line in the log.
   - **`fresh_env()`** (context manager in `tests/stats_helpers.py`): per call creates a
     `tempfile.TemporaryDirectory()` holding its own `CLAUDE_CONFIG_DIR`, `XDG_DATA_HOME`, `CK_CONFIG_HOME`,
     `GIT_CONFIG_GLOBAL` and `HOME`, and yields the env dict for `ck_sub`. Every CLI-driven property builds its fake
     home and runs `ck` inside `with fresh_env() as env:` within the `@given` body and never uses `tmp_path` (Hypothesis
     runs all examples inside one test call, so function-scoped fixtures would carry the store over between examples).
     A harness self-check asserts that two consecutive `fresh_env()` stores are disjoint.
4. **`tests/stats_helpers.py`** (test-owned code; defines the fixture shapes):
   - `FakeClaudeHome(root: Path)` -> `.session(slug="-work-acme", session_id=None, cwd: Path, git_branch="integrate/x",
     cc_version="2.1.80") -> FakeSession`. Writes `projects/<slug>/<sid>.jsonl` (orchestrator transcript).
   - `FakeSession.implement_run(run_id=None, *, name="spec-implement", status="completed", start="2026-10-12T09:00:00Z",
     duration_ms=..., args: dict, result: dict | None, agents: list[AgentSpec], extra_keys: dict = {}, launch=True) ->
     FakeRun`; `launch=True` also appends to the session transcript `<sid>.jsonl`, as Claude Code does, an assistant
     `tool_use` of the `Workflow` tool with input `{name: "spec:<name>", scriptPath: "<root>/.claude/workflows/<name>.js"}`
     and the matching `tool_result` whose text names the run id; `launch={...}` replaces that input (for example
     `{scriptPath: ".../spec-create.js"}` alone, or `{name: "tax-review-2026"}`), `launch=False` writes no launch;
     `.create_run(...)` (name `spec-create`, a `result` shaped like `spec-create.js:797-817`);
     `.foreign_run(name="competitor-teardown-q4", ...)` (a non-ck workflow with its own transcripts).
   - `AgentSpec(label, model="claude-sonnet-5-5", phase="Wave 1", requests=[(request_id, input, output, cache_read,
     cache_write)], split_blocks=True, duration_ms=60_000, started_at_ms=..., attempt=1, meta=True)`; `split_blocks`
     writes each request as two transcript lines with the same `requestId` (as Claude Code does), so dedup is exercised.
     Every transcript line carries an ISO `timestamp` (from `started_at_ms` to `started_at_ms + duration_ms`).
     Writes `<sid>/subagents/workflows/<run_id>/agent-<id>.jsonl` and `.meta.json` (`description`=label,
     `workflowPhase`, `model`).
   - `FakeRun` helpers: `.drop_transcript(agent_id)`, `.truncate_transcript(agent_id)` (cuts the last line mid-JSON),
     `.drop_meta(agent_id)`, `.drop_run_file()` (deletes only the run file; `subagents/workflows/<id>/` stays),
     `.set(**run_file_keys)`, `.corrupt_format()` (allowlisted name, `workflowProgress` as an object), `.add_budget(used)` (a `budget_usd` attachment line), `.path`, `.run_id`, `.session_id`.
   - `implement_result(tasks=[TaskSpec...], landings=..., full_gates=..., final_gate=..., cross_tests=...,
     fixer_escalations=..., phases=..., outcome="complete", resolves=True)`: a result dict shaped like
     `spec-implement.js:953-982` and per-task rec `:647-724` (`task, wave, title, attempts, ok, escalations, riskTag,
     startModel, reviewModel, review{verdict, rounds, findings, summary}, reason, startedAt, finishedAt, mergedAt,
     fixes, landing`); `resolves=False` omits the per-task `resolves` field (a pre-change run).
   - `git_project(tmp, remote="git@github.com:acme-corp/acme-ledger.git") -> Path`: a real repo the session's `cwd` and
     `args.root` point at.
   - `kiro_checkout(tmp, remote="https://github.com/kutlayhanli/claude-kiro.git", *, extra_remotes={},
     unpushed_commit=False) -> (checkout, bare)`: a real clone whose bare repo has one commit on `master`, `origin` =
     `remote`, `url.<bare>.insteadOf=<remote>` (fetch and push both reach the bare repo, offline); `extra_remotes` adds
     named remotes (for example a fork `origin` plus an `upstream` that is the target); `unpushed_commit=True` checks out
     a local branch `local-work` holding one commit that is not in the bare repo.
   - `fake_gh(bin_dir, log: Path)`: script `gh` that appends `{"argv": [...], "body": <--body or --body-file
     contents>}` as JSON lines to `log`; `gh auth status` exits `$FAKE_GH_AUTH_RC` (default 0); `gh pr create` prints
     `https://github.com/kutlayhanli/claude-kiro/pull/101` or exits `$FAKE_GH_FAIL_RC` with an error.
   - `path_without_gh(tmp) -> str`: a PATH holding only symlinks to `git`, `sh`, `python` (the real `/usr/bin/gh`
     exists on this machine, so "gh missing" needs a constructed PATH).
   - `fake_claude_drafter(bin_dir, note_text)`: `claude` that, called with `-p`, prints `note_text` and records its argv
     and stdin to a file; `.calls()` returns every recorded invocation, so a test can assert the drafter never started.
   - `fake_editor(bin_dir, replacement)`: `$EDITOR` script that overwrites the file it is given with `replacement`.
   - `run_tty(argv, *, env, cwd, script: list[tuple[str, str]], timeout=30) -> (exit_code, transcript)`: runs
     `python -m claude_kiro.cli ...` under `pty.fork()`, waits for each regex in `script` and sends its reply. Used for
     every "interactive" case (8.2-8.5, 10.1, 10.4).
   - `stats(*args, env=None, input=None)`: `CliRunner().invoke(cli, ["stats", *args])` with env; `ck_sub(*args, env,
     cwd, stdin=DEVNULL)`: subprocess form.
   - `store_files(xdg) -> list[Path]`, `load_record(xdg, run_id) -> dict`, `all_bytes(*roots) -> bytes` (every file
     under the roots, for leak scans), `flatten_paths(obj) -> set[str]` (dotted JSON paths, list indices as `[]`).
   - `CONFIDENTIAL`: the seeded strings and `seeded_home(tmp) -> (FakeClaudeHome, terms)`, which plants them in every
     free-text slot: spec name `zephyr-payroll`; task titles `Reconcile Globex invoices`, `Initech SSN column`;
     summaries and finding texts naming `/srv/acme/ledger.py`; file paths `/home/dana/acme-ledger/src/ledger.py`,
     `src/ledger_globex.py`; branch `integrate/zephyr-payroll` (also in transcripts' `gitBranch`); remote
     `git@github.com:acme-corp/acme-ledger.git` (parts `acme-corp`, `acme-ledger`); email `dana.scully@acme-corp.com`;
     git identity `Dana Scully <dana.scully@acme-corp.com>`; non-ck workflow names `competitor-teardown-q4`,
     `tax-review-2026`; session/agent/run ids; exact timestamps `2026-10-12T09:13:27Z`; `promptPreview`,
     `resultPreview`, `lastToolSummary`, `logs`, `summary`, `reason` texts quoting the above; plus a second project
     (`orion-billing`, remote `https://gitlab.example.com/orion/billing`) so "terms from all local records" is real.
5. **`tests/js/load_workflow.mjs`:** record every `opts.label` passed to `agent` with the script name; when
   `CK_DUMP_LABELS=<path>` is set, write `[{workflow, label, phase}]` to that path on process exit. The existing suites
   then double as the label corpus (5.3). **`tests/fixtures/legacy_labels.json`:** a pinned list of labels the
   pre-batch-landing script emitted (templates read from
   `git show f03d598^:src/claude_kiro/resources/templates/workflows/spec_implement.js`: `merge N`, `resolve N`,
   `plan K`, `task N retry K (m)`, ...), plus labels with path-shaped task ids (`task .ck/worktrees/demo-task-17`,
   `review .ck/worktrees/demo-task-17`, `land .ck/worktrees/demo-task-17`), so historical runs stay covered by 5.3.
6. **`tests/js/spec_implement.test.mjs`:** `project()` gains `conflictRounds: {task: k}` (a task conflicts on its first
   `k` landings) for multi-round resolves.

## Test Cases

All Python files are under `tests/`. "CLI" = the real `ck` entry point; "FS" = real temp dirs; "git" = real git repos;
"pty" = real pseudo-terminal; "fake X on PATH" = third-party executable replaced.

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|----|-----------|-------|--------------------------------|-------------------------|-----------|
| TC-1 | 1.1 | integration | Given a fake home with one completed spec-implement run, When `ck stats collect --run <id>`, Then exit 0, exactly one file `$XDG_DATA_HOME/claude-kiro/runs/<id>.json` exists, and its path is printed | CLI, FS | `test_stats_collect.py` |
| TC-2 | 1.1 | integration | Given `XDG_DATA_HOME` unset and `HOME=<tmp>/home`, When collect, Then the record is under `<tmp>/home/.local/share/claude-kiro/runs/` | CLI, FS, env | `test_stats_collect.py` |
| TC-3 | 1.2, 1.12 | integration | Given transcripts with `version: "2.1.80"`, When a fresh collect (`ck stats collect --run <id> --auto` right after the run, project without a manifest), Then the record has `schema`, `ckVersion` (= installed package version), `claudeCodeVersion` "2.1.80", `workflow`, `outcome`, `startTime`, `wallMs` (= run file `durationMs`), `agentsConfig` (= result `agents`); with no `version` in transcripts `claudeCodeVersion` is null. Given a backfill (`--all`) of a run whose project `.claude/ck-manifest.json` says `0.8.0` and predates the run start, Then `ckVersion` is `0.8.0`; Given a backfill where the manifest is newer than the run (or absent), Then `ckVersion` is null and `missing` has an entry with `what` `ckVersion`, never the installed version; in every case `collectorVersion` equals the installed package version. Given a run collected fresh with `--auto` (`ckVersion` = installed version) and then re-collected by `--all` (not fresh, no manifest) after a transcript gained a request, Then the replaced record keeps that `ckVersion` | CLI, FS | `test_stats_collect.py` |
| TC-4 | 1.3 | integration | Given agents of three models across roles, phases and tasks, each request split over two lines with one `requestId`, When collect, Then tokens (input, output, cacheRead, cacheCreation) and usd per agent, model, role, phase and task equal hand-computed values from `bench.PRICING`, each request counted once | CLI, FS | `test_stats_collect.py` |
| TC-5 | 1.3, NFR agreement | integration | Given the same run, When collect and `ck cost --run <id> --json`, Then the record's priced total equals `priced_usd` to the cent | CLI (two commands), FS | `test_stats_collect.py` |
| TC-6 | 1.4, NFR agreement | integration | Given a run without a budget attachment, Then cost `source` is `priced_from_transcripts` and `pricingDate` equals `bench.PRICING_DATE`; Given a `budget_usd` attachment (as every `ck run` session has), Then the run figure is still `priced_from_transcripts` and equals TC-5's total, and the record also carries a separate figure with source `claude_total` and `scope` `session`, and each figure names its source | CLI, FS | `test_stats_collect.py` |
| TC-7 | 1.5 | integration | Given a spec-implement result with tasks (attempts 1-3, one escalation, review rounds 0-2 with verdicts, fixes, riskTag, finishedAt/mergedAt 7 min apart), landings, fullGates, finalGate, crossTests, fixerEscalations, timing.phases, When collect, Then every per-task field and per-run field is recorded, idle = 7 min for that task, retries = attempts - 1 | CLI, FS | `test_stats_collect.py` |
| TC-8 | 1.6 | integration | Given a spec-create run (review fixed 3, notFixed 1, escalated 1, refutedCount 2, lensesLost 1, consistency passes 2) with agents in phases Requirements, Design + Test Plan, Adversarial Review, When collect, Then those counts and per-phase durations from `workflowProgress` timings are recorded | CLI, FS | `test_stats_collect.py` |
| TC-9 | 1.7 | integration | Given a result with per-task `resolves` 2 and labels showing only one resolver, Then the record says 2, not inferred; Given a pre-change result (no `resolves`) with agents `resolve 4` and `resolve 4 round 2`, Then task 4 has 2, marked inferred, and a task with no resolver agent has 0 marked inferred | CLI, FS | `test_stats_collect.py` |
| TC-10 | 1.8 | integration | Given the run's project is a git repo with a remote, When collect twice and a second run of the same project, Then both records carry the same non-empty pseudonym that is neither the remote nor any part of it; the salt is stored once in `$CK_CONFIG_HOME/config.json` and unchanged by the second collect; deleting the salt and collecting again in a fresh config dir gives a different pseudonym; a project without a remote gets a null pseudonym | CLI, FS, git, config | `test_stats_collect.py` |
| TC-11 | 1.9 | integration | Given a collected run, When transcripts gain a request and collect runs again, Then the store still holds one file for the run and it reflects the new total | CLI, FS | `test_stats_collect.py` |
| TC-12 | 1.10 | integration | Given no run `wf_nope`, When collect `--run wf_nope`, Then exit != 0, output says not found and names the searched `$CLAUDE_CONFIG_DIR`, the store dir is empty or absent | CLI, FS | `test_stats_collect.py` |
| TC-13 | 1.11 | integration | Given a run file with `status: "running"`, When collect, Then exit 0, output says the run has not finished, nothing written | CLI, FS | `test_stats_collect.py` |
| TC-14 | 1.12 | integration | Given one transcript deleted, one truncated mid-line, one meta.json missing, When collect, Then a record is written, its `missing` list names those agents (by index or agent, never by verbatim label in shared output), costs for the dropped agent are null in priced figures and any estimate from run-file tokens is labelled `estimate`, no missing number is 0. (How `show` prints those nulls is TC-42.) | CLI, FS | `test_stats_collect.py` |
| TC-15 | 1.15, 1.10, 2.1, 2.2 | integration | Given a spec-implement run (three agents with known labels, `workflowPhase` and timestamps) whose session transcript holds its `Workflow` launch (`name: "spec:spec-implement"`) and whose run file is then deleted (`.drop_run_file()`), When collect `--run <id>`, Then exit 0 and one record is written with `workflow` `spec-implement`, `missing` holding `what` `runfile` and `result`, each agent's role from its meta `description` and phase from `workflowPhase`, per-agent `ms` = last minus first transcript timestamp, `wallMs` = latest minus earliest timestamp, and `costs` priced from transcripts equal to `ck cost --run <id> --json`; Given a spec-create run whose launch has only `scriptPath` ending in `spec-create.js`, Then the record's `workflow` is `spec-create`. Given the run file deleted and (a) the launch named `tax-review-2026`, (b) no launch tied to the run id (`launch=False`, or a launch whose result names another id), (c) the session transcript deleted too, When collect `--run <id>`, Then exit != 0, output says not found and names the searched home, nothing is written, and no output or file contains `tax-review-2026` | CLI, FS | `test_stats_collect.py` |
| TC-16 | 1.13, 1.12, 2.2 | integration | Given a `spec-implement` run file with `workflowProgress` as an object, and separately with `workflowProgress` absent, When collect `--run`, Then exit != 0, output says format unrecognised and names the run id, nothing written; Given (the 1.12 side of the line) a `spec-implement` run file whose `workflowProgress` list holds the number `42` and a dict entry with odd fields (`durationMs: "abc"`, `label: 7`) next to good entries, and whose `result` is a string, When collect `--run`, Then exit 0, a record is written, the good entries' agents are in it, `missing` lists the skipped entry and `result`, and the odd entry's unreadable numbers are null, never 0; Given the same corruption on run `tax-review-2026`, Then the output has neither that name nor (per 2.3) any of its content; Given a run file that is not valid JSON, When collect `--run <its id>`, Then exit != 0, format unrecognised, the typed id is named, nothing written; When `collect --all` meets the same file, Then it is counted under `skipped non-ck`, and neither its id nor any of its content is printed | CLI, FS | `test_stats_collect.py` |
| TC-17 | 1.14 | integration | Given `$CLAUDE_CONFIG_DIR/projects` exists but is unreadable (chmod 000; skipped when running as root), When collect `--run` and `--all`, Then exit != 0, output says to run outside the sandbox, nothing written | CLI, FS permissions | `test_stats_collect.py` |
| TC-18 | 2.1, 2.2 | integration | Given two ck runs and foreign runs `competitor-teardown-q4`, `spec-implement-v2`, `Spec-Create`, `spec-plan` in the same session, When `collect --all`, Then only the ck runs are stored; no stored file, stdout, stderr or `ck run` log contains a foreign name or run id; a skipped count of 4 may appear | CLI, FS | `test_stats_collect.py` |
| TC-19 | 2.1 | integration | Given a foreign run whose `workflowProgress` is malformed and whose transcripts are unreadable (chmod 000), When `collect --all`, Then exit 0 and no error is reported for it (it was not read past its name) | CLI, FS permissions | `test_stats_collect.py` |
| TC-20 | 2.3 | integration | Given foreign run `wf_f1` named `tax-review-2026`, When `collect --run wf_f1`, Then exit != 0, output refuses and does not contain `tax-review-2026`; nothing written | CLI, FS | `test_stats_collect.py` |
| TC-21 | 3.1 | e2e | Given a fake `claude` on PATH that, inside `ck run demo`, writes a session (cwd = project) with two ck runs and one foreign run, When `ck run demo` exits, Then both ck runs are in the store, the `.claude/ck-runs/demo-*.log` has exactly one collect line per ck run (naming its id), and neither log nor stdout mentions the foreign run | CLI `ck run`, subprocess, FS | `test_stats_auto.py` |
| TC-22 | 3.3, NFR never blocks | e2e | Given fake `claude` exiting 0 (and, separately, 7) and `XDG_DATA_HOME` pointing at a regular file so every write fails, When `ck run demo --log <tmp>/a.log` and, with `run.analytics false`, `ck run demo --log <tmp>/b.log`, Then both exit codes equal claude's, the two stdouts are equal after each run's log path is replaced by a placeholder, and `a.log` equals `b.log`, except for added lines in the failing run that report the failure and print `ck stats collect --run <id>` | CLI `ck run`, subprocess, FS | `test_stats_auto.py` |
| TC-23 | 3.4 | e2e | Given `ck agents set run.analytics false`, When `ck run demo`, Then nothing is stored and the log has no collect line; Then manual `ck stats collect --run <id>` still stores the run; and `ck stats collect --run <id> --auto` exits 0, says auto-collection is off, stores nothing | CLI, config, FS | `test_stats_auto.py` |
| TC-24 | 3.5 | integration | Given no setting in any layer, When `ck agents --json`, Then `run.analytics` is true; and `ck run demo` collects | CLI, config | `test_stats_auto.py` |
| TC-25 | 3.6 | integration | Given a stored config, When `ck agents set run.analytics maybe` (global and `--project`), Then exit != 0, error names the accepted values (true/false), and both config files are byte-identical to before | CLI, config files | `test_stats_auto.py` |
| TC-26 | 3.2, 3.3, 3.4 | contract (static) | Given `plugin/skills/implement/SKILL.md` step 9, `plugin/skills/create/SKILL.md` "Close the loop" and `plugin/skills/review/SKILL.md` step 4, Then each tells the orchestrator to run `ck stats collect --run <run id> --auto` outside the sandbox as the last reporting step, to report where the record was written, and on failure to report it with the retry command without changing the run's report; the vendored renderings (`plugin.vendor_files()`) carry the same text | packaged and vendored files | `test_stats_auto.py` |
| TC-27 | 3.3 | integration | Given a collect that will fail (unwritable store), When `ck stats collect --run <id> --auto`, Then exit 0, output reports the failure and prints `ck stats collect --run <id>` | CLI, FS | `test_stats_auto.py` |
| TC-28 | 4.1 | integration | Given two projects with completed `spec-implement`, `spec-implement-iso` and `spec-create` runs, When `collect --all`, Then every one is stored | CLI, FS | `test_stats_collect.py` |
| TC-29 | 4.2 | integration | Given 2 new runs, 1 already collected and unchanged, 1 collected but with a new transcript since, 1 running, 1 corrupt-format allowlisted, 2 foreign, When `collect --all`, Then output reports collected 2, already had 1, updated 1, unfinished 1, failed 1 (with its id and reason), non-ck 2 (count only) | CLI, FS | `test_stats_collect.py` |
| TC-30 | 4.3 | integration | In TC-29's tree, Then all other runs are stored and exit != 0; after removing the corrupt run, Then `--all` exits 0 | CLI, FS | `test_stats_collect.py` |
| TC-31 | 4.4 | integration | Given only foreign runs (and, separately, an empty home), When `collect --all`, Then output says no ck runs were found, exit 0 | CLI, FS | `test_stats_collect.py` |
| TC-32 | 5.1 | integration | Given agents labelled with every template (`task 3`, `task 3 [test]`, `task 3 retry 1`, `task 3 retry 2 (opus)`, `task 3 revise 1`, `review 3`, `review 3 round 2`, `resolve 3`, `resolve 3 round 2`, `fix 1 (Task 3 red on landing)`, `fix 2 (full gate) [opus]`, `re-check 2`, `land 3 4`, `full gate @4`, `final gate`, `cross-test wave 2`, `plan 1`, `plan 2 retry`, `report`; spec-create `write requirements.md`, `review: fidelity`, `refute: fidelity`, `consistency check: pass 1`, ...), When collect, Then `byRole` and per-agent role/task/attempt/round match the template's meaning | CLI, FS | `test_stats_roles.py` |
| TC-33 | 5.2 | integration | Given two agents with labels `triage invoices` and `tasks 3` (labels no template produces), When collect and `ck stats show <id>`, Then both are role `other`, and show reports 2 agents, their minutes and dollars under `other` | CLI, FS | `test_stats_roles.py` |
| TC-34 | 5.3 | integration (Node + Python) | When the Node suites run with `CK_DUMP_LABELS`, Then every recorded label from both workflows parses to a role other than `other`, and every role in the role set appears at least once (so the corpus covers the templates); every label of the pinned legacy corpus `tests/fixtures/legacy_labels.json` parses too (`merge N` -> `land`, path-shaped ids -> the keyword's role with task null); and the same check, given the corpus plus one unknown label, fails naming it | node subprocess, real workflow scripts, legacy fixture | `test_stats_label_corpus.py` |
| TC-35 | 6.1 | integration | Given three collected runs (two kinds, different months), When `ck stats` and `ck stats show`, Then both list all three newest first, each with date, kind, outcome, wall time, cost with its source, task counts | CLI, store | `test_stats_show.py` |
| TC-36 | 6.2 | integration | Given a collected implement run with known numbers, When `ck stats show <id>`, Then output has time and cost per role, phase and model; the slowest and most expensive tasks (the fixture's known ones); retry, escalation, review-round, conflict and fix rates; idle before merge; landing summary (count, sizes, red, bisected) | CLI, store | `test_stats_show.py` |
| TC-37 | 6.3 | integration | Given three implement runs (two from project A, one from B; Sept and Oct) and one create run, When `ck stats show --json`, Then `trends["spec-implement"]` lists cost per merged task, wall time per task and role cost/time shares per run with the hand-computed values; `--kind spec-create` excludes implement runs; `--since 2026-10-01` excludes September; `--project <A's pseudonym>` excludes B | CLI, store | `test_stats_show.py` |
| TC-38 | 6.3 | integration | Given one run per kind, Then no trend is shown for either kind (fewer than two runs) and the command exits 0 | CLI, store | `test_stats_show.py` |
| TC-39 | 6.4 | integration | Given TC-35/36 stores, When adding `--json`, Then output parses as JSON and contains the same run ids, costs, sources and per-role figures as the text output | CLI, store | `test_stats_show.py` |
| TC-40 | 6.5 | integration | Given an empty store, When `ck stats`, Then output says no runs are collected and suggests `ck stats collect --all`, exit 0 | CLI, FS | `test_stats_show.py` |
| TC-41 | 6.6 | integration | When `ck stats show wf_missing`, Then exit != 0 and output says there is no local record | CLI, store | `test_stats_show.py` |
| TC-42 | 6.7, 1.12 | integration | Given a collected record rewritten without optional fields added later (per-task `resolves`, `claudeCodeVersion`, idle, `crossTests`), When `ck stats` and `ck stats show <id>` (text and `--json`), Then both succeed; those fields read `unknown` (text) / null (JSON), never 0. Given a record collected from TC-14's fixture (one transcript deleted, one truncated, one meta missing), When `ck stats show <id>` (text and `--json`), Then every null number of that record prints `unknown` in text and `null` in JSON, never 0 | CLI, store | `test_stats_show.py` |
| TC-43 | 6.8 | integration | When `ck stats show <id>`, Then the wall time line states what it covers and what it excludes (for example plan and final gate), and each per-phase/per-role duration block states its basis | CLI, store | `test_stats_show.py` |
| TC-44 | 7.1 | integration | Given three collected runs in two months and a kiro checkout, When `ck stats export --to <checkout>`, Then three files exist at `analytics/runs/<yyyy-mm>/<record id>.json` under the right months; ids are distinct and share no 6-char substring with any run, session or agent id | CLI, FS, git (remote check) | `test_stats_export.py` |
| TC-45 | 7.2 | integration | Given TC-44, Then each file has `schema` = `ck-run-analytics/1`, `month`, workflow kind, outcome, counts, durations, per-task entries indexed 1..n, tokens and cost by model family and role with source and pricing date, matching the local record's numbers | CLI, FS | `test_stats_export.py` |
| TC-46 | 7.3 | e2e | Given `seeded_home`, When `collect --all` then `export`, Then no exported byte contains any `CONFIDENTIAL` string, the salt, the pseudonym, any run/session/agent id, any `yyyy-mm-dd` date or timestamp, or any free text (no `.md` file) | CLI, FS | `test_stats_export.py` |
| TC-47 | 7.4, 7.5 | integration | Given a run with outcome `exploded-acme`, verdict `meh`, effort `turbo`, models `us.anthropic.claude-opus-5-5-v1:0`, `my-finetune`, `claude-opus-5-5`, When export, Then those fields read `other`, `other`, `other`, `other`, `other`, `opus`, and none of the original strings appear | CLI, FS | `test_stats_export.py` |
| TC-48 | 7.6 | integration | When `ck agents set run.machine_label work-laptop` then export, Then records carry `machine: "work-laptop"`; `ck agents set run.machine_label "Work Laptop"` and a 33-char slug are rejected and the config is unchanged; `ck agents set run.machine_label none` clears it; with no label set the field is absent; a 32-character valid slug exports without a leak-check hit | CLI, config, FS | `test_stats_export.py` |
| TC-49 | 7.7 | contract + integration | Then `analytics/SCHEMA.md` exists in this repo, lists every allowed field and every excluded category of 7.3, and documents the leak-check matching rule (9.3); the set of documented fields equals `SHAREABLE_FIELDS`; every flattened path of every record exported in TC-44/46/47 and of `build_shareable` over P-4's records matches a documented field | repo file, CLI output | `test_stats_export.py` |
| TC-50 | 7.8 | integration | Given TC-44 done, When export again, Then no new file; When `export --re-export <run id>`, Then one new file for that run (new id), its earlier unsubmitted file is gone, and other runs' files are untouched | CLI, FS | `test_stats_export.py` |
| TC-51 | 7.9 | integration | Given a checkout whose origin is `https://github.com/angelsen/claude-kiro.git` (the upstream, a real near-miss), a repo with no remote, and a plain directory, When export `--to` each, Then exit != 0, refusal message, no file written in any | CLI, FS, git | `test_stats_export.py` |
| TC-52 | 7.10 | integration | Given nothing new, When export, Then output says nothing new, exit 0 | CLI | `test_stats_export.py` |
| TC-53 | 7.11, 7.12, NFR no network | integration | Under `no_network` with a fake `claude` drafter on PATH, When collect, show, export, and `export --note` with stdin not a TTY run as `ck` subprocesses, Then they succeed, `$CK_TEST_NET_LOG` is empty or absent (the `sitecustomize` guard in each child would have recorded a connect), and the fake drafter's `.calls()` is empty | CLI subprocess, child socket guard, fake claude | `test_stats_export.py` |
| TC-54 | 8.1 | integration | Given a run with a red landing after a merge, a resolver hitting its round limit, an escalation to opus, a blocking review finding, a task with no commits, When export twice (with `--re-export`, reading the first record before the re-export replaces it), Then both records carry the same sorted codes `escalated_to_opus`, `gate_red_after_merge`, `resolver_rounds_exceeded`, `retry_no_commits`, ...; a clean run carries none | CLI, FS | `test_stats_export.py` |
| TC-55 | 8.2 | e2e (pty) | Given fake `claude` drafting `Resolver needed 3 rounds on zephyr-payroll lockfile`, When `ck stats export --to <c> --note` under a pty, Then the draft is shown with `zephyr-payroll` marked as a leak-check hit; choosing edit with `$EDITOR` replacing the text by `Resolver needed 3 rounds on a lockfile conflict`, then accept, Then `<id>.md` contains exactly that text; the drafter's prompt was sent only to the fake `claude` | pty, subprocess, fake claude/editor, FS | `test_stats_export.py` |
| TC-56 | 8.3 | e2e (pty) | Given the edited text still contains `acme-corp`, When accept, Then the final check reports the hit, no `.md` is written, the `.json` record is written | pty, FS | `test_stats_export.py` |
| TC-57 | 8.4, 7.12 | integration + pty | Given a note would be drafted, When export `--note` runs with stdin not a TTY, and again under a pty with `--yes`, Then the note is not written, records and issue codes are, and output says the note was withheld for lack of interactive approval; the fake drafter was never started (`.calls()` empty) and its text never appears in any file | CLI, subprocess, pty, FS | `test_stats_export.py` |
| TC-58 | 8.5 | e2e (pty) | When the user chooses discard, Then the record is exported without a `.md` | pty, FS | `test_stats_export.py` |
| TC-59 | 9.1 | integration | Given local records from projects A and B, When exporting A's records with a note naming B's spec `orion-billing`, or the git identity `Dana Scully`, or `dana.scully@acme-corp.com`, or `sk-ant-api03-...`, or `/home/dana/x`, or a UUID, or `2026-10-12T09:13:27Z`, Then each is reported as a hit and the note is excluded | pty, FS, git config | `test_stats_leak.py` |
| TC-60 | 9.2 | integration | Given `run.machine_label` set to `zephyr-payroll` (a valid slug that is a confidential term), When export, Then exit != 0, each hit printed with file and field (`machine`), no file written; When submit `--yes`, Then exit != 0, no branch locally or in the bare remote, no commit, fake `gh` never called | CLI, FS, git, fake gh | `test_stats_leak.py` |
| TC-61 | 9.3, 9.1 | integration | Given projects whose spec names are `ui` and `review` and a path segment `a`, When a normal export runs, Then no hit (the record's field names and enum values such as `reviewer` contain them only inside words); a note `the ui spec` is a hit; Given local records whose path segments include `analytics`, `transcripts` and `permission` (from `src/claude_kiro/analytics/`, `transcripts/`, `permission/`), When export, Then no hit and the files are written (fixed enum and const values such as `ck-run-analytics/1`, `priced_from_transcripts`, `permission_refusal` are not scanned for terms); Given a run whose issue codes include every `ISSUE_CODES` member (such as `review_contract_mismatch`), When export, Then no hit; Given local records whose path segments include `1`, `80`, `2026` and `cafe` (digit-only and hex-only segments, which are not confidential terms) and shareable records with `claudeCodeVersion` `2.1.80`, `month` `2026-10` and a random `id`, When export, Then no hit and the files are written; Given the records with the `analytics` path segment above and a kiro checkout, When `submit --yes` with fake `gh`, Then the leak check passes, a branch matching `^ck-stats-batch-[0-9a-f]{8}$` is pushed to the bare remote and `gh pr create` is called; Given local records whose path segments include `spec`, `ck`, `stats` and `batch` (ck-vocabulary words, which are not confidential terms; e.g. a Ruby project's `spec/`) and a kiro checkout, When `submit --yes` with fake `gh`, Then the leak check passes on the branch name, commit message and PR text (which contain `ck-stats-batch-…` and `spec-implement`) and the PR is created, while a note containing a non-vocabulary segment from the same records is still a hit; `analytics/SCHEMA.md` documents the whole-word rule, the digit-only rule, the ck-vocabulary rule and the fixed-token skipping | CLI, FS, pty, git, fake gh | `test_stats_leak.py` |
| TC-62 | 9.4, 10.2 | e2e | Given `seeded_home` and a kiro checkout, When `collect --all`, `export`, `submit --yes` with fake `gh`, Then the exported files, the pushed branch name, the commit message (`git log` in the bare repo), the commit's file list, and the PR title and body (from the `gh` log) contain none of the seeded strings, in any case | CLI, git, bare remote, fake gh | `test_stats_leak.py` |
| TC-63 | 10.1 | e2e (pty) | Given two unexported records, When `ck stats submit --to <c>` under a pty, Then before the confirmation prompt the output contains the full content of each new file and a passing leak-check line, and no branch exists yet | pty, git | `test_stats_submit.py` |
| TC-64 | 10.2 | e2e (pty) | When the user confirms, Then a new branch named `ck-stats-batch-<8 hex>` exists in the bare remote whose parent is the bare repo's `master`, its single commit adds only the new `analytics/runs/...` files, fake `gh` was called with `pr create --draft --repo kutlayhanli/claude-kiro --base master --head <branch>`, title and body mention only record count, kinds, months and machine label, the printed output has the PR URL, and the checkout is back on its original branch. Given `kiro_checkout(unpushed_commit=True)` (HEAD on `local-work` with one unpushed commit), Then that commit is not an ancestor of the PR branch. Given a fork `origin` (`https://github.com/someone/claude-kiro.git`) plus `upstream` = the target, Then the branch is pushed to `upstream` only and `origin` receives nothing | pty, git, fake gh | `test_stats_submit.py` |
| TC-65 | 10.3 | e2e | Given TC-64 done and one more run collected, When `submit --yes`, Then the second PR's commit contains only the new record; a third submit says nothing new | CLI, git, fake gh | `test_stats_submit.py` |
| TC-66 | 10.4 | e2e (pty) | When the user declines, Then no new branch locally or in the bare remote, no commit, `gh` not called, and a later `submit --yes` includes those records | pty, git, fake gh | `test_stats_submit.py` |
| TC-67 | 10.5 | integration | When `submit --yes` with no notes (stdin not a TTY), Then no prompt, the diff and leak-check lines are printed, PR opened; When `submit --note --yes` (stdin not a TTY) would draft a note in its own export step, Then no drafter starts, the note is withheld per 8.4 and the rest is submitted (an approved note from an earlier export is TC-82) | CLI, git, fake gh | `test_stats_submit.py` |
| TC-68 | 10.6, 10.7, 10.10 | integration | Given (a) PATH without `gh`, (b) `gh auth status` failing, (c) `gh pr create` failing, When `submit --yes`, Then exit != 0, the error is reported, the exported files are in the checkout's working tree, and after cleaning the checkout a `submit --yes` with a working `gh` still includes those records | CLI, PATH, git, fake gh | `test_stats_submit.py` |
| TC-69 | 10.7 | integration | Given (a) an uncommitted change in the checkout, (b) a destination file already present (tracked), When submit, Then exit != 0, refusal, `git status --porcelain`, HEAD, branch list and every file's bytes are unchanged; Given (c) only this machine's own exported-but-unsubmitted files untracked in the checkout (after a declined submit), When `submit --yes`, Then it is not refused and those files are the batch | CLI, git, FS | `test_stats_submit.py` |
| TC-70 | 10.8 | integration | Given nothing new, When submit, Then output says so, exit 0, no branch, `gh` not called | CLI, git, fake gh | `test_stats_submit.py` |
| TC-71 | 10.9 | integration | Given checkouts whose origin is `angelsen/claude-kiro`, `kutlayhanli/claude-kiro-fork`, `gitlab.com/kutlayhanli/claude-kiro`, When submit, Then refusal, nothing written, `gh` not called | CLI, git, fake gh | `test_stats_submit.py` |
| TC-72 | 11.1 | workflow (Node) | Given the stubbed project with no conflicts, one conflict on task 2, `conflictRounds: {3: 2}`, and a task over `MAX_RESOLVES`, When the workflow returns, Then every `result.tasks[i].resolves` is 0, 1, 2 and the limit respectively (also on failed tasks) | real workflow script under node | `js/spec_implement.test.mjs` |
| TC-73 | 11.2 | workflow (Node) + integration | Then every existing test in `spec_implement.test.mjs` passes unchanged; for one full scenario the result's key set equals today's plus `tasks[].resolves`; and `tests/test_plugin.py::test_dogfood_copies_in_this_repo_match_the_plugin_source` passes (vendored `.claude/workflows/spec-implement.js` equals `plugin/`) | node, repo files | `js/spec_implement.test.mjs`, `test_plugin.py` |
| TC-74 | 12.1 | integration | Given the newest allowlisted run has an unrecognised format (an older one is fine), When `ck doctor` in a ck project, Then a warning names the format problem (and only an allowlisted name) | CLI, FS | `test_stats_doctor.py` |
| TC-75 | 12.2 | integration | Given `run.analytics false`, When `ck doctor`, Then it mentions auto-collection is off as information; exit status and the issue count are the same as with it on | CLI, config | `test_stats_doctor.py` |
| TC-76 | 12.3 | integration | Given 3 completed allowlisted runs, 1 collected, When `ck doctor`, Then it reports 2 not collected and suggests `ck stats collect --all`; with all collected it says nothing about it | CLI, FS | `test_stats_doctor.py` |
| TC-77 | NFR performance | integration | Given one fixture run with 250 agents (3 requests each), When collect, Then it finishes in under 10 s | CLI, FS | `test_stats_perf.py` |
| TC-78 | NFR performance | integration | Given 100 fixture runs (20 agents each) across 5 projects, When `collect --all`, Then under 120 s | CLI, FS | `test_stats_perf.py` |
| TC-79 | NFR local record protection, 1.8 | integration | Given a fresh `XDG_DATA_HOME` and `CK_CONFIG_HOME`, When collect, Then each record file is mode 0600, the runs dir (created by ck) is 0700, and the config file holding the salt is 0600. Given `ck agents set implementer.model sonnet` was run first and `config.json` is chmod 0644, When `ck stats collect --run <id>`, Then `config.json` is 0600, its other keys are unchanged, and a later `ck agents set implementer.effort high` leaves it 0600 | CLI, FS modes | `test_stats_collect.py` |
| TC-80 | NFR determinism | integration | When `export --re-export <id>` twice, reading the first file before the second run (a re-export of an unsubmitted record replaces its earlier file, 10.11), Then the two contents are identical once `id` is removed | CLI, FS | `test_stats_export.py` |
| TC-81 | 10.10, 7.8 | integration | Given a record exported to a kiro checkout and not submitted, When its `.json` file is deleted from the checkout and `ck stats export` runs again, Then that record is exported again under a new id (the ledger points at the new file) and records whose files are present are not; Given a record that was submitted (its files live only on the PR branch), When export runs on the original branch, Then it is not exported again | CLI, FS, git | `test_stats_export.py` |
| TC-82 | 10.11, 10.5, 10.7, 8.4 | integration + pty | Given an issues note approved in an earlier interactive `export --note` (its `.md` is in the checkout, unsubmitted) and one more collected run not yet exported, When `submit --yes` with stdin not a TTY, Then exit 0, no prompt, output says the note was withheld for lack of interactive approval, the PR is opened, its single commit holds the earlier record and the newly exported record and no `.md`, the `.md` is still in the checkout untracked and unchanged, and a later `submit --yes` says nothing new instead of refusing over that file (10.7); separately, from the same start, When `submit --yes` under a pty, Then the `[y/N]` prompt still appears and confirming commits the `.md` with its record; and When `submit` without `--yes` and without a TTY, Then exit != 0, no branch locally or in the bare remote, `gh` not called, every checkout file unchanged and no file added for the unexported run | CLI, pty, git, fake gh | `test_stats_submit.py` |
| TC-83 | 1.2, 1.12 | integration | Given a backfilled spec-implement run whose result has the pre-0.10 shape (`halted`, `failed`, `notStarted`, no `outcome`): (a) `halted: false`, both lists empty; (b) `halted: false`, `failed` non-empty; (c) `halted: true`; (d) `refused: true`, When `collect --all`, Then `outcome` is `complete`, `blocked`, `halted`, `refused` respectively, each with `outcomeInferred` true; Given a current result with `outcome: "blocked"`, Then `outcome` is `blocked` with `outcomeInferred` false; the exported record's `outcome` is never `other` for (a)-(d) | CLI, FS | `test_stats_collect.py` |
| TC-84 | NFR local record protection | integration | Given a resolved data dir (`XDG_DATA_HOME=<tmp>/xdg-data`), When `ck run demo --dry-run --json` with the sandbox on (sandbox tools faked as in `test_run.py`), Then the printed `settings.sandbox.credentials.files` lists `<tmp>/xdg-data/claude-kiro` with `mode: "deny"` next to the existing credential paths | CLI `ck run`, generated settings | `test_stats_auto.py` |

## Properties

Library: **Hypothesis** (added as a dev dependency). Every property uses
`@settings(derandomize=True, max_examples=200, database=None, deadline=None)`; properties that drive the CLI over a
generated Claude home or config (P-2, the CLI halves of P-8 and P-9, P-11, P-15, P-16, P-23) use `max_examples=25`
with the same other settings, and build each example's home, store and config inside `with fresh_env() as env:` in the
`@given` body (never `tmp_path`), so no example sees another's store or config. They live in the
`tests/test_stats_properties*.py` files tasks.md names, each defining the `PROPERTIES` map `ck gate` checks. Safety properties call the
decision seams in the Harness Contract; TC-18, TC-46, TC-60, TC-62 show the CLI uses them.

Shared generators (in the test file):
- `confidential()`: every `CONFIDENTIAL` string in surface forms: as-is, upper/lower/title case, JSON-escaped (`\/`,
  `\u00xx`), inside a sentence with punctuation on either side, as a path segment (`/x/<t>/y`, `<t>/y`, `./<t>`,
  `../<t>`, `~/<t>`, `C:\\<t>\\y`), as a branch (`integrate/<t>`, `feat/<t>-1`), remote forms
  (`https://github.com/o/r`, `https://github.com/o/r.git`, `git@github.com:o/r.git`, `ssh://git@github.com/o/r`,
  `o/r`), emails (`Name <a@b.c>`, `a+tag@b.c`, `A@B.C`, with spaces), ids (`wf_<hex>`, UUID lower/upper, agent ids
  `a<hex>`), timestamps (`2026-10-12T09:13:27Z`, `+02:00` offset, `.123Z`, `2026-10-12 09:13:27`, epoch ms).
- `local_record()`: a real record produced by collecting `seeded_home` (session-scoped fixture), with Hypothesis
  replacing any subset of its string leaves by `confidential()` values and any enum leaves by `enum_noise()`.
- `enum_noise()`: near-miss values from the real data: `Complete`, `completed`, `complete `, `halted!`, `approve`
  vs `approved`, `APPROVE`, `request_changes`, model ids `claude-opus-5-5`, `claude-sonnet-5-5[1m]`,
  `claude-haiku-5-5-20261001`, `us.anthropic.claude-opus-5-5-v1:0`, `arn:aws:bedrock:...:claude-sonnet`,
  `claude-opus-5-5@20261001`, `anthropic/claude-opus-5-5`, `opus`, `Opus`, `inherited`, `my-finetune`; efforts `max`,
  `turbo`, `HIGH`; stack hints `python`, `Python`, `django`, `acme-stack`; plus the confidential strings.

| ID | Requirement | Property | Generator domain | Expected decision |
|----|-------------|----------|------------------|-------------------|
| P-1 | 2.1 | For any workflow name, `is_ck_workflow(name)` is true only for `spec-implement`, `spec-implement-iso`, `spec-create` exactly | The three names; near misses `spec-implement-v2`, `Spec-Implement`, ` spec-create`, `spec-create\n`, `spec_create`, `spec-plan`, `spec-implement-iso2`, `my-spec-create`, `spec-implement.js`; non-ck names from the seeded data (`competitor-teardown-q4`, `tax-review-2026`); non-strings (None, 1, list) | true for the three; false for every other value |
| P-2 | 2.2, 2.3 | Complement: for any fake home holding ck runs plus foreign runs with any non-allowlisted name, after `collect --all` and `collect --run <foreign id>`, no stored file contains the foreign name or id, and no stdout or stderr contains the foreign name, nor the foreign id apart from the `--run` refusal echoing back the id I typed (2.2's one exception) | Foreign names from P-1's negatives and `confidential()`; 1-3 foreign runs; ck runs 0-2 | refuse (foreign) / store (ck); zero occurrences |
| P-3 | 5.1 | For any label generated from the workflows' templates with any task id, attempt, round, fix count, model and wave, `parse_label` returns the template's role, task, attempt and round | Task ids drawn from the workflow's own id rule `[0-9A-Za-z_]+` (digits, letters, underscore; for example `3`, `17`, `3_a`, `x`), never dotted. Templates of `spec-implement.js` (`task N`, `task N [test]`, `... retry K`, `... retry K (M)`, `... revise R`, `... revise R (M)`, `review N`, `review N round R`, `resolve N`, `resolve N round R`, `fix K (WHAT)`, `fix K (Task N red on landing)`, `... [M]`, `re-check K`, `land N...`, `full gate @K`, `final gate`, `cross-test wave W`, `plan K`, `plan K retry`, `report`) and `spec-create.js` (`write requirements.md`, `write design.md`, `write test-plan.md`, `write implementation track`, `write test track`, `link tracks into tasks.md`, `review: L`, `refute: L`, `snapshot spec before revision`, `revise spec + write review.md`, `consistency check: pass P`, `remove pre-revision snapshot`), plus the legacy templates `merge N` (role `land`) and `resolve N`; N in 1..999 and ids from `[0-9A-Za-z_]+`, models `opus`, `sonnet`, `haiku`, `claude-opus-5-5` | exact role/task/attempt/round |
| P-4 | 5.2 | Complement: for any label no template produces, `parse_label` returns role `other` with task, attempt and round null | Near misses: `task`, `task 3!`, `task 3 retry x`, `Task 3`, `tasks 3`, `task 3 revise`, `task 3 retry`, `review`, `reviews 3`, `land`, `fix (x)`, `resolve`, `full gate`, `full gate @`, `plan`, `review:`, `consistency check`, `write README.md`, template labels with leading/trailing whitespace or a suffix (`task 3 extra`), and `confidential()` | `other`, never a guessed role |
| P-5 | 7.2, 7.3, 9.4 | For any local record, `build_shareable(record)`'s flattened paths are a subset of `SHAREABLE_FIELDS`, and its JSON serialization contains none of the record's confidential terms (as returned by `confidential_terms([record])`), nor the salt, nor the pseudonym, nor any run/session/agent id, nor any `dd`-precision date | `local_record()`, with planted strings in every string leaf (titles, summaries, findings, reasons, labels, previews, logs, args, paths, branch, remote, ids, timestamps) | subset holds; zero occurrences |
| P-6 | 7.3, 9.4 | Metamorphic: for any local record, changing any free-text leaf (title, summary, finding text, preview, path) to any other string leaves `build_shareable` output unchanged apart from `id`. Task `reason` and agent labels are not free text here: issue codes are derived from reasons (8.1) and labels decide roles (5.1), so they legitimately change the output; a `reason` is mutated only by replacing it with another reason of the same class (see P-10) | `local_record()` pairs differing in one free-text leaf | equal (except `id`) |
| P-7 | 7.4, 7.5 | For any enumerated field (outcome, verdict, role, model, effort, stack hint, issue code) and any value, `enum_value(field, v)` is in that field's fixed set or `other`; it equals `v` only when `v` is in the set; `model_family` maps canonical `claude-<family>-...` ids and bare aliases to the family and every provider-prefixed, `@`-suffixed, `arn:`/`us.`/`anthropic/` or custom id to `other` | `enum_noise()`, `confidential()`, the fixed sets themselves | in set or `other`; original never copied |
| P-8 | 7.6 | For any string, `valid_machine_label(s)` is true iff `s` matches `^[a-z0-9-]{1,32}$`; complement: `ck agents set run.machine_label <s>` for any rejected `s` exits != 0 and leaves config bytes unchanged (CLI half at `max_examples=25`) | Lengths 0, 1, 31, 32, 33, 64; uppercase; spaces, tabs, newline, leading/trailing space; `_`, `.`, `/`, `@`; unicode lookalikes (`ｗork`, Cyrillic `а`); emails; paths; the seeded spec names (valid slugs, accepted here and caught by the leak check, P-12/TC-60) | accept iff regex; rejected values never stored |
| P-9 | 3.6 | For any value, `ck agents set run.analytics <v>` is accepted iff `v.lower()` is one of `true`, `false`, `yes`, `no`, `on`, `off` (the forms existing boolean settings accept); every rejected value leaves global and project config bytes unchanged | Those six in mixed case; `1`, `0`, `y`, `n`, `enabled`, `truee`, ` true`, `true `, empty, `"true"`, numbers, random text | accept / reject with config unchanged |
| P-10 | 8.1 | For any local record, `issue_codes(record)` is a sorted list of unique values from `ISSUE_CODES`; calling it twice gives the same list; reordering tasks, landings or agents does not change it; changing free-text leaves other than `reason` (title, summary, finding text, preview, path) does not change it; metamorphic: replacing any task `reason` with another reason of the same class gives the same codes | `local_record()` and permutations of its lists; reason classes from a fixed table, one class per reason rule of `ISSUE_CODES` (`merge failed: still conflicting after <n> rounds`, `merge failed: conflict not resolved`, `merge failed: no commits`, `review: blocking findings remain`, `planning step failed`, a permission refusal matching `bench.REFUSAL_RE`, and an unclassified class), each with at least two exemplars, none of the non-permission exemplars matching `bench.REFUSAL_RE` | deterministic, order-independent, text-independent within a reason class |
| P-11 | 1.9, 4.1, 4.2 | Idempotence: for any fake home and any number k in 1..3 of repeated `collect --all` (interleaved with `collect --run` of any collected id), the store holds exactly one file per completed allowlisted run, and from the second pass on every run is reported as already had | Generated homes: 1-4 ck runs over 1-2 projects, mixed with running and foreign runs | one record per run; second pass all "already had" |
| P-12 | 9.1, 9.2, 9.3 | For any confidential term from any local record (from any project on the machine), the git identity, or a non-ck workflow name passed as `non_ck_names` to `confidential_terms`, planted in any surface form into any field of any file handed to `leak_scan`, the scan reports a hit naming that file and field. Digit-only and hex-only strings are not terms (9.3) and are left out of this domain; run, session and agent ids stay in it. A term planted into a JSON value hits wherever the value is not a fixed token (a member of its enum or const; planting a term makes it not a member). Weak terms (short or common) hit as whole words in non-enum JSON string values as well as in free text. Metamorphic: appending text to a file never removes a hit | `confidential()` terms (strong ones of length >= 4, and weak ones such as `ui`, `review`, `ledger`) inserted into JSON string values, JSON keys and Markdown lines of 1-3 files; foreign names `competitor-teardown-q4`, `tax-review-2026` | hit with correct file and field |
| P-13 | 9.1 | For any generic secret or identifier shape, `leak_scan` with no terms reports a hit | Emails (display name, plus tag, case), URLs (`http`, `https`, `ssh://`, `git@host:o/r`), key shapes (`sk-ant-api03-...`, `ghp_...`, `github_pat_...`, `AKIA...`, `xoxb-...`), absolute paths (`/home/x/y`, `/Users/x`, `~/x`, `C:\\x`), UUIDs (any case), `wf_<hex>` ids, ISO timestamps with and without zone, `yyyy-mm-dd` dates | hit |
| P-14 | 9.3 | For any short or generic term (length <= 3, or in the documented common-word list), embedding it inside a longer alphanumeric word or a camelCase/snake_case field name never yields a hit, while the term standing alone between non-alphanumeric characters (or at the text edges) always does, in free text and in a non-enum JSON string value alike; the same term as (part of) an enum member in an enum-typed position (`reviewer` as a role) never hits. A local-record string made only of decimal or hex digits is never returned by `confidential_terms` (ids apart), so a record holding it scans clean wherever the digits appear (a version, `month`, `id`, free text) | Terms `a`, `ui`, `api`, `test`, `review`, `plan`, `land`; hosts `reviewer`, `reviewRounds`, `attempts`, `planSteps`, `landings`, `ui_kit`; standalone in sentences, after `/`, in quotes, at start/end; a JSON record whose free string value is exactly `review`; digit-only and hex-only path segments `1`, `10`, `80`, `2026`, `cafe`, `beef`, `0a1b` against `claudeCodeVersion` `2.1.80`, `month` `2026-10` and random 16-hex `id`s | embedded: no hit; standalone: hit; digit-only/hex-only: not a term, no hit |
| P-15 | 1.3, NFR agreement | Conservation: for any generated run, sums of usd and of each token kind over agents, over models, over roles, over phases and over tasks plus untasked agents all equal the run total, which equals `ck cost --run <id> --json` `priced_usd` within $0.005; metamorphic: duplicating any transcript line with the same `requestId` changes nothing | 1-12 agents, models from the three families, 1-4 requests each, token counts 0..2M including zeros, split and unsplit lines, labels from P-3's templates and P-4's negatives | equal |
| P-16 | 1.12 | For any subset of a run's transcripts deleted or truncated, no affected number is stored as 0 in the collected record (it is null with a `missing` entry); metamorphic: deleting a transcript never increases the priced total. (How `show` renders nulls, 6.7, is TC-42.) | Subsets of 1-6 agents; truncation at any byte offset of the last line | null, never 0; total non-increasing |
| P-17 | 7.1 | For any local record, the record id of `build_shareable` is fresh: two builds give different ids, and the id shares no substring of length >= 6 with any run, session or agent id in the record | `local_record()` | distinct; unrelated |
| P-18 | 7.2, 7.3 | For any start time, the shareable `month` equals the UTC `yyyy-mm` of that instant and the serialized record contains no day or time of it | ISO with `Z`, offsets `+14:00`/`-12:00` across month and year boundaries (e.g. `2026-10-31T23:30:00-02:00` -> `2026-11`), fractional seconds, epoch ms | UTC month only |
| P-19 | 7.9, 10.9 | For any remote URL, `is_target_repo(url)` is true iff it designates GitHub `kutlayhanli/claude-kiro` | Accept: `https://github.com/kutlayhanli/claude-kiro`, with `.git`, trailing `/`, `git@github.com:kutlayhanli/claude-kiro.git`, `ssh://git@github.com/kutlayhanli/claude-kiro.git`, case variants of owner/repo. Reject (near misses from the real data): `angelsen/claude-kiro` (upstream in `pyproject.toml`), `kutlayhanli/claude-kiro-fork`, `kutlayhanli/claude-kiro.git.evil`, `github.com.evil.com/kutlayhanli/claude-kiro`, `gitlab.com/kutlayhanli/claude-kiro`, `github.com/kutlayhanli/claude-kiro/../other`, a local path, empty | accept / refuse |
| P-20 | 10.2, 10.3, 9.4 | For any batch of shareable records (built from `local_record()`), any valid machine label and a fixed token, `pr_text(records, label, token=T)`'s branch, commit message, title and body contain no confidential term of the source records and are a function of record count, kinds, months, label and `T` only (changing anything else leaves them unchanged); with `token=None`, `branch` matches `^ck-stats-batch-[0-9a-f]{8}$` and two calls give different branches, so two submits never reuse one branch | Batches of 1-5 records, label absent or valid, tokens of 8 hex | zero occurrences; invariant to other fields with the token held; fresh branch per call |
| P-21 | NFR determinism | For any local record, `build_shareable(r, record_id=X)` called twice is identical | `local_record()` | equal |
| P-22 | 1.8, 7.3 | For any remote and salt, `pseudonym(remote, salt)` is stable for equal inputs, differs across salts and remotes (no collisions over the generated set), and contains no path or name segment of the remote | Remotes in `confidential()` remote forms; salts 16-32 random bytes | stable, keyed, opaque |
| P-23 | 3.3, NFR never blocks | For any injected collection failure and any claude exit code, `ck run demo` exits with claude's code, prints no traceback, and its stdout equals the `run.analytics false` run's (log paths replaced by a placeholder, explicit `--log` per run) apart from the per-run collect lines: the collect-failure and `ck stats collect --run <id>` retry lines; for the run-file-JSON-truncated fault, exactly the count-only line `stats: 1 run file in this session could not be read; retry: ck stats collect --all` (which names no run id; 3.3, 2.2), also present in the `ck run` log, with `<utc iso> - unrecognised` in the collect log; and, for the injected faults that 1.12 turns into a partial record (a transcript chmod 000, a `.meta.json` that is not JSON), the collected line | Failures from {run-file JSON truncated, `workflowProgress` an object, a transcript chmod 000, a `.meta.json` that is not JSON, the store path a regular file, `CK_CONFIG_HOME` unwritable (salt cannot be created), the session dir deleted after claude exits} x claude exit codes {0, 7}; `max_examples=25` under `fresh_env()` | exit code equal; stdout equal modulo failure lines; no traceback |
| P-24 | 9.3, 8.1, 7.6 | Vocabulary never trips the scanner: no field name of `SHAREABLE_FIELDS`, no member of any shareable enum (including every `ISSUE_CODES` member) and no fixed template word of the commit message and PR text matches any generic pattern of `leak_scan`; and for any record built by `build_shareable` from `local_record()` with every `ISSUE_CODES` member in `issues` and any valid machine label of 1-32 characters, `leak_scan` with no terms reports no hit | The fixed sets themselves; `local_record()`; machine labels of lengths 1, 31, 32 | no hit |

## Harness Contract

The tests call only these names. Each is required of the implementation; design.md may add others but not rename these.

### CLI
- `ck stats` (same as `ck stats show`).
- `ck stats show [RUN_ID] [--json] [--kind spec-implement|spec-create] [--since YYYY-MM-DD] [--until YYYY-MM-DD]
  [--project PSEUDONYM]`. Text output prints `unknown` for null numbers. `--json` without RUN_ID prints
  `{"runs": [ {runId, startTime, workflow, outcome, wallMs, cost: {usd, source}, tasks: {total, merged, failed}} ...
  newest first ], "trends": {<kind>: {"runs": [{runId, costPerMergedTask, wallMinutesPerTask, roleShare: {<role>:
  {cost, time}}}]}}}` (a kind appears in `trends` only with >= 2 runs); with RUN_ID it prints the local record plus
  `byRole/byPhase/byModel`, `slowestTasks`, `costliestTasks`, `rates: {retry, escalation, reviewRound, conflict, fix}`,
  `idleBeforeMergeMs`, `landingSummary`, `other: {agents, minutes, usd}`.
- `ck stats collect (--run RUN_ID | --all) [--auto]`. Prints the record path per run. `--all` prints the summary
  lines `collected: N`, `already had: N`, `updated: N`, `unfinished: N`, `failed: N` (followed by `  <run id>:
  <reason>` lines), `skipped non-ck: N`. `--run` of a run whose run file is gone (1.15) prints the record path
  when the workflow name is recovered and allowlisted, else the not-found message. `--auto` = the form `ck run` and the skills use: does nothing and exits 0 when
  `run.analytics` is false; on any failure prints the error and `ck stats collect --run <id>` and exits 0.
- `ck stats export --to CHECKOUT [--yes] [--re-export RUN_ID]... [--note]`. `--note` asks for a drafted issues note
  (interactive only). Interactive prompt for the note accepts `accept`, `edit`, `discard` (or their first letter).
- `ck stats submit --to CHECKOUT [--yes]`. Interactive confirmation is a `[y/N]` prompt.
- Leak-check hits are printed one per line as `<path relative to checkout>: <field>: <kind>` where `<field>` is the
  dotted JSON path (or `line N` for `.md`); the matched text is shown only in interactive note review.
- Exit codes: 0 for success and for the "nothing to do" cases named in 1.11, 4.4, 6.5, 7.10, 10.8; non-zero otherwise
  as the criteria say.

### Settings (`ck agents set`, existing layered config in `agents.py`)
- `run.analytics`: boolean, default true; accepts the same forms as other boolean settings.
- `run.machine_label`: slug `^[a-z0-9-]{1,32}$` or `default`/`none` to unset; default unset (7.6).
  `ck stats label` is an alias.
- `ck agents --json` shows both under `run`.
- The pseudonym salt is stored in the global config file (`agents.global_config_path()`) under the top-level key
  `stats_salt`, never shown by `ck agents`; the file is 0600 after the salt is written, even when it existed at
  0644 before (1.8, NFR local record protection).

### Files
- Local store: `$XDG_DATA_HOME/claude-kiro/runs/<run_id>.json` (fallback `~/.local/share/claude-kiro/runs/`), 0600,
  directory 0700 when created. Export/submit bookkeeping may live in the same data dir; tests only observe behaviour.
- Local record keys the tests read: `schema` (string, `ck-run-local/<n>`), `runId`, `workflow`, `outcome`,
  `ckVersion` (string or null), `collectorVersion` (string, always), `claudeCodeVersion` (string or null), `startTime` (ISO), `wallMs`, `agentsConfig`, `projectPseudonym`
  (string or null), `outcomeInferred` (bool), `costs` (list of `{usd, source, pricingDate, scope}` with `source` in
  `claude_total`, `priced_from_transcripts`, `estimate` and `scope` `run` or `session`; the run figure is never
  `claude_total`), `byModel`, `byRole`, `byPhase`, `byTask` (each `{<key>: {usd, input,
  output, cacheRead, cacheCreation, agents, ms}}`), `agents` (list of `{agentId, label, role, task, attempt, round,
  model, phase, ms, usd, input, output, cacheRead, cacheCreation}`), `missing` (list of `{agentId, what}`; `what`
  includes `runfile` and `result` for a 1.15 record, and `agent_entry` for a skipped non-object agent entry), and for
  spec-implement `tasks` (list of `{task, attempts, retries, escalations, reviewRounds, verdict, resolves,
  resolvesInferred, fixes, agentMinutes, toMergedMinutes, riskTag, idleBeforeMergeMs}`), `landings`, `fullGates`,
  `finalGate`, `crossTests`, `fixerEscalations`, `phases`, `reportSource`, `taskSpanMinutes`, `stats` (design.md ImplementMetrics); for spec-create `mode`, `reviewResult`, `openQuestions` (design.md CreateMetrics), `review: {fixed, notFixed, escalated,
  refuted, lensesLost}`, `consistency: {passes, fixed}`, `phases`. Unknown numbers are null, never 0.
- Shareable record: `<checkout>/analytics/runs/<yyyy-mm>/<id>.json`, `schema` = `ck-run-analytics/1`, `id`, `month`,
  `machine` (absent when unset), `issues` (list of codes); an approved note is `<id>.md` next to it.
- `analytics/SCHEMA.md` in this repo: a `## Fields` section with one bulleted, backticked path per allowed field
  (`*` = one segment whose value comes from a fixed enum, `[]` = list items), a `## Never exported` section naming
  each category of 7.3, a `## Leak check` section with the matching rule (9.3), and the fixed enums including
  `ISSUE_CODES`.

### Decision seams (`claude_kiro.analytics`, pure, no I/O)
- `ALLOWED_WORKFLOWS: frozenset[str]` = `{"spec-implement", "spec-implement-iso", "spec-create"}`;
  `is_ck_workflow(name: object) -> bool`.
- `ROLES: frozenset[str]` = `{implementer, test-writer, reviewer, fixer, resolver, plan, land, gate, cross-test,
  report, other}` plus any spec-create roles design.md defines; `parse_label(label: str, workflow: str) -> dict` with
  keys `role, task (str|None), attempt (int|None), round (int|None)`.
- `build_shareable(local: dict, *, record_id: str | None = None, machine_label: str | None = None) -> dict`
  (generates a random id when `record_id` is None; the note is never an input; notes are added only by the
  interactive path).
- `SHAREABLE_FIELDS: tuple[str, ...]` (same pattern syntax as `SCHEMA.md`).
- `enum_value(field: str, value: object) -> str` for fields `outcome, verdict, role, model, effort, stack, issue`;
  `model_family(model: object) -> str`; `MODEL_FAMILIES, OUTCOMES, VERDICTS, EFFORTS, STACK_HINTS, ISSUE_CODES`.
- `valid_machine_label(value: object) -> bool`.
- `issue_codes(local: dict) -> list[str]`.
- `confidential_terms(local_records: Iterable[dict], non_ck_names: Iterable[str] = ()) -> set[str]`.
- `leak_scan(files: Mapping[str, str], terms: Iterable[str], identity: Iterable[str] = ()) -> list[dict]`, each hit
  `{file, field, kind}` (`kind` in `term`, `identity`, `email`, `url`, `secret`, `path`, `id`, `timestamp`).
- `is_target_repo(url: str) -> bool`.
- `pr_text(records: Sequence[dict], machine_label: str | None, token: str | None = None) -> dict` with `branch,
  commit, title, body` (`token=None` draws a fresh random token; `branch` is `ck-stats-batch-<token>`).
- `month_of(start: str | int) -> str` (UTC).
- `pseudonym(remote: str, salt: bytes) -> str`.

### Integration points
- `ck run` collects through the same code path as `ck stats collect --auto` after the child exits, for every
  allowlisted run in the session it launched, and writes one line per run to its log. Run files of the session
  whose workflow name cannot be read get one count-only line, `stats: <n> run file(s) in this session could not
  be read; retry: ck stats collect --all` (singular for one file), on stdout and in the log, with no run id.
- Note drafting runs `claude -p ...` found on PATH and reads the draft from its stdout.
- Submit uses `git` for fetch, branch, commit and push against the remote whose configured URL is
  `kutlayhanli/claude-kiro` (the branch starts from that remote's `master`), and `gh pr create --draft --repo
  kutlayhanli/claude-kiro --base master --head <branch> --title ... --body ...` (or `--body-file`).
- spec-implement result: every entry of `result.tasks` has integer `resolves`.
- `CK_DUMP_LABELS` hook in `tests/js/load_workflow.mjs` (test-owned).

## Critical Paths

1. **Unattended run is recorded without changing the run.** `ck run <spec>` launches Claude (fake), the session writes
   ck and non-ck runs, Claude exits, the outer process collects the ck runs only, logs one line each, and exits with
   Claude's status even when collection fails. -> TC-21, TC-22, TC-23, TC-18, TC-27.
2. **Skill-driven run is recorded.** `/spec:implement`, `/spec:create` or `/spec:review` ends; the orchestrator runs
   `ck stats collect --run <id> --auto` outside the sandbox and reports the path. -> TC-26, TC-1, TC-23, TC-27.
3. **Backfill and inspect.** `ck stats collect --all` over many projects and runs -> summary -> `ck stats` list ->
   `ck stats show <id>` breakdown -> trends with filters. -> TC-28, TC-29, TC-30, TC-35, TC-36, TC-37, TC-42, TC-78.
4. **Share safely (the seeded leak path).** Seeded runs -> `collect --all` -> `export --to <checkout>` (allowlisted
   records, issue codes, optional note reviewed in a pty) -> `submit` (diff, leak check, confirm, branch, commit,
   push to bare remote, draft PR via fake gh) -> zero seeded strings anywhere. -> TC-46, TC-55, TC-62, TC-63, TC-64,
   P-5, P-12, P-20.
5. **Refusals hold.** Leak hit, wrong repo, dirty checkout, declined prompt, missing gh: nothing committed or opened.
   -> TC-60, TC-51, TC-71, TC-69, TC-66, TC-68.
6. **Label contract stays pinned.** Workflow suites run -> labels dumped -> every label parses -> `resolves` present.
   -> TC-34, TC-72, TC-73.
7. **Leaving and resuming a batch.** Decline or a `gh` failure leaves files; the next submit accepts them as this
   machine's own pending files, restores ones I deleted, asks about an approved note at a terminal and withholds
   it without one.
   -> TC-66, TC-68, TC-69, TC-81, TC-82.

## Commands

- Everything: `uv run pytest -q`
- Label corpus (5.3): `uv run pytest -q tests/test_stats_label_corpus.py`
- Collect (Stories 1, 2, 4, NFR modes): `uv run pytest -q tests/test_stats_collect.py`
- Automatic collection (Story 3): `uv run pytest -q tests/test_stats_auto.py`
- Roles and label grammar (Story 5): `uv run pytest -q tests/test_stats_roles.py`
- Show and trends (Story 6): `uv run pytest -q tests/test_stats_show.py`
- Export and issues (Stories 7, 8): `uv run pytest -q tests/test_stats_export.py`
- Leak check (Story 9): `uv run pytest -q tests/test_stats_leak.py`
- Submit (Story 10): `uv run pytest -q tests/test_stats_submit.py`
- Doctor (Story 12): `uv run pytest -q tests/test_stats_doctor.py`
- Properties: `uv run pytest -q tests/test_stats_properties*.py`
- Performance: `uv run pytest -q tests/test_stats_perf.py`
- Workflow result field (Story 11): `node tests/js/spec_implement.test.mjs` and
  `uv run pytest -q tests/test_workflow_scripts.py tests/test_plugin.py`
- Existing suites that now auto-collect after `ck run` (regression): `uv run pytest -q tests/test_run.py
  tests/test_cost.py tests/test_bench.py`
- Property coverage in the gate: `uv run ck gate run-analytics`

## Not Tested

- **Real Claude Code and real GitHub.** No test runs `claude` or talks to GitHub; both are fakes on PATH, and the run
  file shape is pinned by fixtures copied from real files' keys. A Claude Code format change is caught at run time by
  1.13/12.1, not by these tests.
- **The real bubblewrap sandbox.** 1.14 is simulated with an unreadable `$CLAUDE_CONFIG_DIR/projects`; the real
  `denyRead ~/` behaviour inside `ck run` is not reproduced.
- **Transcript retention** (PLAN open question): not observable in tests.
- **Quality of the drafted issues note.** The drafting agent is faked; only the leak check, display, edit, approval
  and withholding around it are tested.
- **macOS/Windows default data dirs.** The fallback in TC-2 is tested for the Linux default only.
- **spec:plan coverage** and a repository-side aggregate report (decision 8; Out of Scope).
- **That `ck run`'s session discovery finds sessions started by a real Claude Code** beyond what `latest_session`
  already does in `test_cost.py`; the fake writes the session the same way.
- **Performance on real hardware variance:** TC-77/78 use generous bounds from the NFR, on fixture data only.
