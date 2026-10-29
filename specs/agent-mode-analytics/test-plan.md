# Test Plan: Agent-Mode Analytics

Requirements: [requirements.md](requirements.md). Decision record: [PLAN.md](PLAN.md) (D1-D9 bind this plan).
Written from the requirements and the current test suite. design.md was not read.

## Strategy

The project is a Python CLI (`ck`, click), tested with pytest + pytest-xdist + Hypothesis (`uv run pytest -q -n auto`,
the `specs/ck.json` verify line). The run-analytics tests set the pattern this plan follows:

- **Integration first, through the real CLI.** Every criterion that reads Claude transcripts, writes the store, runs
  git, or prints to the user is tested by running `python -m claude_kiro.cli ...` as a subprocess (`stats_helpers.ck_sub`
  / `stats`). Each test gets a fake `$CLAUDE_CONFIG_DIR` shaped like a real one, its own `XDG_DATA_HOME` store,
  `CK_CONFIG_HOME` and `GIT_CONFIG_GLOBAL` (`fresh_env()`), and real temp git repos. Nothing ck owns is mocked: not
  the collector, store, filesystem, git, `ck worktree report` or `ck cost`.
- **Fakes only for things ck does not own:** Claude Code's transcript and meta-file layout (written by the harness),
  the network (`no_network` fixture: in-process socket guard plus `tests/netguard/sitecustomize.py` in children),
  `claude` (drafter for notes), `gh`, and a logging `git` wrapper for the "SHALL NOT run" checks (TC-18, TC-20, Task 12's 3.6 cases). The clock is
  not faked at the CLI: age-based tests backdate a manifest's `startTime` by whole hours (5 h vs 7 h) so the clock
  never matters. The exact 6-hour boundary is tested at the pure seam.
- **Static document tests** for the orchestrator contract (templates, `begin`, final collect). The orchestrator is
  a Markdown document, so these tests read the packaged files and the vendored renderings from
  `claude_kiro.plugin.vendor_files()`, as `tests/test_stats_skills.py` does today.
- **Properties (Hypothesis)** for every universally quantified criterion. Safety and grammar properties call pure
  decision seams exported from `claude_kiro.analytics` (Harness Contract). Collector-wide properties (cost
  agreement, leak terms, `--auto` never fails) run the real CLI inside `fresh_env()` with a small example count.
- **Unit tests** only where a seam has real branching (outcome mapping, manifest age, grouping) and only as the
  table-driven half of a property.
- **Backward compatibility** is the existing suite unchanged (`tests/test_stats_*.py`), plus one metamorphic test
  that a workflow run's record is byte-identical whether or not agent-mode transcripts sit next to it.

Every new test fails before the feature exists for the right reason: `ck stats begin` and `collect --agents` /
`--agents-backfill` are unknown options (click exit 2 with "No such command/option"), the seams raise
`AttributeError` from `claude_kiro.analytics.__getattr__`, and the docs lack the templates. Project modules are
imported inside test bodies only, so every file still collects before the implementation lands.

## Test Infrastructure

### Exists (reuse as is)

| Path | What |
|---|---|
| `tests/conftest.py` | autouse isolation of `CK_CONFIG_HOME`, `XDG_DATA_HOME`, `CLAUDE_CONFIG_DIR`, git identity, gate registry; `no_network` fixture; `project` fixture (real repo with `specs/calc`) |
| `tests/stats_helpers.py` | `FakeClaudeHome`, `FakeSession` (orchestrator transcript, workflow runs), `AgentSpec`, `fresh_env()`, `ck_sub`, `stats`, `run_tty`, `git_project`, `kiro_checkout`, `fake_gh`, `fake_claude_drafter`, `store_files`, `load_record`, `all_bytes`, `flatten_paths`, `CONFIDENTIAL`, `seeded_home`, `seeded_local_record` |
| `tests/netguard/sitecustomize.py` | network guard for child Pythons (`$CK_TEST_NET_LOG`) |
| `tests/test_stats_export.py` | `schema_section`, `documented_paths`, `assert_documented` (SCHEMA.md vs records) |
| `tests/test_stats_leak.py` | `World` (checkout + fake gh + per-world store), `_export_b_then_note_a` note flow under a pty |
| `tests/test_plugin.py::test_dogfood_copies_in_this_repo_match_the_plugin_source` | checked-in `.claude/` copies == `vendor_files()` (covers 2.6) |
| `tests/test_worktree_report.py` | how `ck worktree report` is fed by real git (merge subjects `Merge feat/<spec>-task-N (task N)`) |

### Add to `tests/stats_helpers.py`

1. **`ToolAgentSpec`** (dataclass), one Agent-tool subagent:
   `description: Optional[str]` (meta `description`; `None` leaves the key out), `agent_type: Optional[str] =
   "spec:implementer"`, `model: str = "claude-sonnet-5-5"` (transcript model), `meta_model: Optional[str] = None`
   (meta `model` when set), `requests: Optional[List[Tuple[str, int, int, int, int]]] = None` (same tuple as
   `AgentSpec`), `duration_ms: int = 60_000`, `started_at_ms: Optional[int] = None`, `worktree_path:
   Optional[str] = None`, `worktree_branch: Optional[str] = None`, `extra_meta: Dict[str, Any] = {}` (merged last;
   used for unknown keys), `meta: bool = True`, `agent_id: Optional[str] = None`.
2. **`FakeSession.tool_agents(specs, *, start="2026-10-12T09:00:00Z") -> FakeToolAgents`** writes, per spec,
   `projects/<slug>/<sid>/subagents/agent-<id>.jsonl` (same line shape and timestamps as `_write_agent`) and
   `agent-<id>.meta.json` with `agentType`, `description`, `toolUseId`, `spawnDepth: 1`, `requestShape`,
   `requestNonInteractive`, `isBuiltIn: false`, plus `model`, `worktreePath`, `worktreeBranch` when set and
   `extra_meta`. No start/end in the meta. It also appends the orchestrator's `Agent` `tool_use` line (no usage) to
   the session transcript. `FakeToolAgents` has `agent_ids`, `transcript(i)`, `meta_path(i)`, `drop_transcript(i)`,
   `truncate_transcript(i)`, `drop_meta(i)`, `corrupt_meta(i)` (writes `{not json`).
3. **`AgentSpec.extra_meta: Dict[str, Any] = {}`** merged into a workflow agent's meta (9.1 for workflow runs).
4. **`marker(run_id) -> str`** = `f"[ck:{run_id}]"`; **`described(run_id, rest) -> str`** = `f"[ck:{run_id}] {rest}"`.
5. **`begin(env, cwd, kind="implement", spec="demo", into=None) -> Tuple[str, Path]`** runs
   `ck stats begin --kind <kind> --spec <spec> [--into <into>] --json` in `cwd`, asserts exit 0, returns
   `(run_id, manifest_path)` from the JSON.
6. **`backdate_manifest(path, hours)`** rewrites the manifest's `startTime` to now minus `hours` (ISO UTC `Z`).
   `startTime` is the manifest's only clock field (design A10 parses it), so this moves doctor, `--all` and the
   outcome window together; **`set_manifest(path, **keys)`** updates keys (e.g. `sessionId`, `doneAtStart`).
7. **`spec_repo(root, spec="demo", into="integrate/demo", tasks=4, deps=None) -> Path`** a real, unlanded repo:
   `specs/<spec>/tasks.md` with `tasks` tasks (`deps` = `{"3": ["2"]}`), committed on `main`, branch `into`.
   **`land(repo, spec, into, task, done=True)`** adds a `feat/<spec>-task-N` branch with one commit, merges it into
   `into` with `git merge --no-ff -m "Merge feat/<spec>-task-N (task N)"` and marks the task Done (`done=False`
   leaves tasks.md alone); **`fail(repo, spec, task)`** adds a branch with an unlanded commit;
   **`mark_done(repo, spec, into, task)`** marks a task Done without a merge (the report's `done` list). Tests call
   `begin` between `spec_repo` and the landing helpers, as a real run does, so `doneAtStart` holds only what was
   landed before `begin` (3.4). `ck worktree report <spec> --into <into> --json` over it gives known lists.
8. **`logging_git(bin_dir, log) -> Path`** a `git` on PATH that appends its argv as one JSON line to `log` and execs
   the real git (`shutil.which("git")` resolved before PATH changes).
9. **`read_only(path)`** context manager: `chmod 0o500` on a directory, restored on exit (tests skip as root).
10. **`backfill_home(env, sessions, per_session) -> FakeClaudeHome`** sessions of unmarked spec-type agents with
    old free-form descriptions (`implement demo: task <N>`, `fix demo: task <N> red`, `revise demo task <N>: x`,
    `review task <N> diff`), for the backfill perf test.
11. **`SPEC_TYPES`** = the ten spec agent types (`spec:implementer` ... `ck-reviewer`), and **`OLD_DESCRIPTIONS`**
    (the free-form forms above, with `{spec}` and `{n}` slots).

Fixture names and contents use invented names only (`demo`, `calc`, and the existing `CONFIDENTIAL` set such as
`zephyr-payroll`, `acme-ledger`). No real project or client names.

### New test files

| File | Covers |
|---|---|
| `tests/test_stats_agents_begin.py` | Story 1 |
| `tests/test_stats_agents_docs.py` | Story 2, 1.6, 5.1, 5.4, 7.9 (documents) |
| `tests/test_stats_agents_collect.py` | Story 3, Story 4, 5.2, 5.3 |
| `tests/test_stats_agents_open.py` | Story 6 |
| `tests/test_stats_agents_backfill.py` | Story 7 |
| `tests/test_stats_agents_share.py` | Story 8, Story 9 |
| `tests/test_stats_properties_agents.py` | P-1 to P-12 and P-18 (pure seams; P-6 lives in the docs tests, P-13/P-14 in the schema tests) |
| `tests/test_stats_properties_agents_cli.py` | P-15 to P-17 (real CLI) |
| `tests/test_stats_perf.py` (extend) | NFR performance |

Every new integration file sets `pytestmark = pytest.mark.usefixtures("no_network")` and, where a child ran,
asserts `$CK_TEST_NET_LOG` is empty (NFR no network).

## Test Cases

| ID | Criterion | Level | Scenario (Given / When / Then) | Real boundary exercised | Test file |
|---|---|---|---|---|---|
| TC-1 | 1.1, 1.2, 1.3 | integration | Given a `spec_repo` (spec `demo`, no task Done) on branch `integrate/demo`, run from a subdirectory. When `ck stats begin --kind implement --spec demo --json`. Then stdout is exactly one JSON object `{run_id, manifest}`; `run_id` matches `^ag_[0-9a-f]{8}$`; the manifest file holds `kind=implement`, `spec=demo`, `root` = the repo top level (not the subdirectory), `into=integrate/demo`, `startTime` within the test's wall window, `ckVersion` = installed version, `sessionId` null or a string, `doneAtStart` == `[]` (in a plain `git_project` with no `specs/demo/tasks.md` on the target it is `null`, 1.2), `agents` equal to the `{model, effort, enabled}` projection of `ck agents --json` `roles` (run in the same directory) for the config roles implementer, test_writer, reviewer, fixer, resolver, orchestrator, plus `reviewer_risky` = the reviewer's `risky_model` / `risky_effort` / `enabled` (`ck agents --json` has no `reviewer_risky` row; missing keys are `null`). With a `machine_label` and a `run.allowed_domains` entry set from `CONFIDENTIAL` beforehand, the manifest has no `allowed_domains`, `machine_label`, `sources` or `runtime` key and its bytes contain neither value (1.2); file mode 0600; its directory 0700 when created by ck | CLI subprocess, real git, real store | test_stats_agents_begin.py |
| TC-2 | 1.2 | integration | Given the same repo. When `begin --kind review --spec demo --into main` (no `--json`). Then stdout's first line is the run id, and the manifest has `into=main`, `kind=review` | CLI, store | test_stats_agents_begin.py |
| TC-3 | 1.1 | integration | Given `CK_TEST_RUN_ID_DRAWS=0a1b2c3d,0a1b2c3e,0a1b2c3f,0a1b2c40` and a store holding a local record `ag_0a1b2c3d`, an open manifest `ag_0a1b2c3e` and a closed manifest `ag_0a1b2c3f` (the first three draws). When `begin` runs. Then it prints `ag_0a1b2c40` (it skipped every taken id, including the closed one). Without the hook: 30 runs give 30 distinct valid ids | CLI, store, test draw hook | test_stats_agents_begin.py |
| TC-4 | 1.4 | integration (table) | When `begin` with: no `--kind`; `--kind plan`; `--kind Implement`; `--kind ""`; no `--spec`; `--spec ""`. Then exit 2, output names `implement`, `create` and `review`, and `store_files(xdg)` is empty | CLI, store | test_stats_agents_begin.py |
| TC-5 | 1.5 | integration | Given the store directory read-only (`read_only`). When `begin --kind implement --spec demo` (and with `--json`). Then exit 0, stdout carries a valid id (JSON: `manifest` null), stderr warns that the run will be collected without a manifest, no file was written | CLI, real filesystem permissions | test_stats_agents_begin.py |
| TC-6 | 1.7, NFR no network | integration | Given `no_network`. When `begin` is timed and `python -m claude_kiro.cli --version` is timed in the same env. Then begin minus the baseline is under 1 s, and the net log is empty | CLI, netguard | test_stats_agents_begin.py |
| TC-7 | 1.6, 2.5 | static doc | In `without-workflow-tool.md` and `spawn-worktree/SKILL.md` (packaged and vendored): the `begin` step says to continue without the marker when begin fails or is unavailable, and never to stop or retry the run because of it; the marker-less form uses the same templates without `[ck:<run_id>] ` and no spec, title or path | file read, `vendor_files()` | test_stats_agents_docs.py |
| TC-8 | 2.1 | static doc | Extract every inline-code template that starts with `[ck:<run_id>]` from the four agent-mode instruction sources (packaged + vendored). Then the set contains `implement task <N>`, `implement task <N> retry <k>`, `review task <N> round <k>`, `fix <k>`, `resolve task <N>`, and `create <step>` rendered once per step; the create steps listed equal the keys of `AGENT_CREATE_STEPS` | file read, seam constant | test_stats_agents_docs.py |
| TC-9 | 2.2, D4 | static doc | Every extracted template uses only the placeholders `<run_id>`, `<N>`, `<k>`; every `"description":` value and every "description `...`" phrase in those sources is a marker template (the old `implement <spec>: task <N>` and `implement <feature>: <task-summary>` are gone). A self-check runs the same checker on a planted `[ck:<run_id>] implement <spec>: task <N>` and asserts it fails naming the template | file read | test_stats_agents_docs.py |
| TC-10 | 2.3 | static doc + seam | Render every template with sample values and each agent type the doc's table gives that work; `parse_agent_description` returns a role that is not `other`, for the marked and the marker-less rendering (driven by P-6) | file read, seam | test_stats_agents_docs.py |
| TC-11 | 2.4 | static doc | `spawn-worktree/SKILL.md` (packaged + vendored): both the "No Workflow tool" path and the "Everything `inherit`, review off" path say to start with `ck stats begin --kind implement` and to use the marker templates for every Agent call | file read | test_stats_agents_docs.py |
| TC-12 | 2.6 | static | Existing `test_dogfood_copies_in_this_repo_match_the_plugin_source` passes; plus, for each new `begin` / collect `--agents` line, the vendored rendering contains the packaged line | `vendor_files()`, repo `.claude/` | test_stats_agents_docs.py, tests/test_plugin.py |
| TC-13 | 3.1, 3.3, 3.7, 3.8 | integration | Given `begin --kind implement --spec zephyr-payroll` in a repo, and a session holding marked agents (implementer, test-writer, reviewer, fixer, resolver), an unmarked `general-purpose` agent, a `spec:implementer` agent marked with another id, and a workflow run. When `collect --agents <id> --json`. Then `{run_id, result: "collected", path, partial: false}`; the record has `source: "agents"`, `workflow: "spec-implement"`, exactly the five marked agent ids, `project.spec/root/branch` from the manifest, no description containing the spec; run-scope cost `source: "agent_transcripts"`; `missing` has no `result` entry and `wallMs` is non-null (the latest agent end minus the manifest start); text mode prints the path; the agents are timed after the manifest's `startTime`, so `wallMs` is positive; afterwards the manifest is no longer open: with the manifest backdated by 7 h before the collect (agents timed after the backdated start), `ck doctor` afterwards reports no open run and `collect --all` neither collects it again nor counts it (`collected: 0`, `already had` unchanged, no line naming the id) | CLI, store, real git | test_stats_agents_collect.py |
| TC-14 | 3.2, NFR agreement | integration | Given marked agents over three models with known requests in a session with no other usage. When collected. Then each agent's `model`, token totals, `usd` and `ms` (from transcript first/last timestamps) are as the requests imply; the run-scope `usd` equals the sum of agent `usd` and equals `ck cost --session <sid> --json` `priced_usd`, both rounded to cents | CLI (`collect`, `cost`) | test_stats_agents_collect.py |
| TC-15 | 3.1 | integration | (a) manifest `sessionId` null, marked agents in session B: collected. (b) `sessionId` = A, marked agents in A and B: only A's. (c) `sessionId` = A with no marked agents, marked agents in B: B's | CLI, filesystem search | test_stats_agents_collect.py |
| TC-16 | 3.4, 1.2 | integration (parametrized) | Given a `spec_repo`, then `begin --kind implement`, then (after `begin`, with `land` / `fail` / `mark_done`) the states: all merged; 1 merged + 2 done + 3 blocked on 2-failed; 1 merged + 2 failed; 1 merged + rest unstarted. When collected. Then local `outcome` is `complete`, `failed`, `failed`, `blocked` respectively, and `build_shareable(record)["counts"]` has `tasks_merged` = the size of (report `merged` ∪ `done`) minus the manifest's `doneAtStart` (empty here), `tasks_failed` = report `failed`, `tasks_not_started` = report `unstarted` + `blocked`. Resumed spec: task 1 landed before `begin`, task 2 landed after: `doneAtStart == ["1"]`, `tasks_merged == 1`, and the record's `tasks` lists only task 2. Unknown Done-at-begin (`set_manifest(doneAtStart=None)`): only merges whose `mergedAt` is at or after the manifest start and no later than 30 minutes after the last agent's end count (a merge landed before `begin`, and one landed more than 30 minutes after the last agent, are not counted). The agent transcripts of every TC-16 case are timed after `begin` and span the landings, so the window is real time, not the fixture default date | CLI, real git, `ck worktree report` | test_stats_agents_collect.py |
| TC-17 | 3.5 | integration (parametrized) | Given a manifest whose root was deleted; is a plain directory; whose `into` branch was deleted; whose spec has no tasks.md. When collected. Then exit 0, record written, `outcome` null, `missing` has `{"what": "outcome"}`, and the shareable `counts` has no `tasks_merged` / `tasks_failed` / `tasks_not_started` keys (unknown, not 0) | CLI, real git | test_stats_agents_collect.py |
| TC-18 | 3.6 | integration | Given `begin --kind create` and `--kind review` in a `spec_repo` where a report would succeed, PATH with `logging_git`. When collected. Then `outcome` null, `workflow: "spec-create"`, `mode` = `create` / `review`, and the git log holds no `show <into>:specs/<spec>/tasks.md` call | CLI, git wrapper | test_stats_agents_collect.py |
| TC-19 | 3.9 | integration | Collect twice: second `result: "unchanged"`, file bytes identical. Append a request to one transcript, collect: `updated`, totals changed, one record file | CLI, store | test_stats_agents_collect.py |
| TC-20 | 3.10 | integration | Given marked agents for `ag_0a1b2c3d` and no manifest. When `collect --agents ag_0a1b2c3d --json`. Then `partial: true`; `missing` has `{"what": "manifest"}`; `project.root` = the session's `cwd`; `project.spec` null; `outcome` null; `workflow: "spec-implement"` (its agents are implement templates). Variant: every marked agent is a `general-purpose` `create <step>` agent: `workflow: "spec-create"`, `mode` null, and with `logging_git` on PATH no `show <into>:specs/` call is made (3.10) | CLI, store, git wrapper | test_stats_agents_collect.py |
| TC-21 | 3.11 | integration | Given neither. Then exit non-zero, message says not found and names the Claude projects directory and the manifest directory, `store_files` unchanged | CLI | test_stats_agents_collect.py |
| TC-22 | 3.12 | integration | Given a manifest and no marked agents. Then exit non-zero, message contains "no agents carry the marker", no record; after backdating, `ck doctor` still counts it open | CLI, doctor | test_stats_agents_collect.py |
| TC-23 | 3.13 | integration (table) | `--agents` with `ag_0123ABCD`, `ag_0123abc`, `ag_0123abcde`, `AG_0123abcd`, `wf_0123abcd`, `ag-0123abcd`, ` ag_0123abcd`, `[ck:ag_0123abcd]`: exit 2, message shows `ag_` + 8 lowercase hex, nothing written. `--run ag_0123abcd`: exit 2, message names `--agents` | CLI | test_stats_agents_collect.py |
| TC-24 | 3.14 | integration (table) | `--agents X --run wf_a`, `--agents X --all`, `--agents X --agents-backfill`: exit 2, nothing written | CLI | test_stats_agents_collect.py |
| TC-25 | 3.15 | integration | Given five marked agents: one transcript dropped, one truncated, one meta corrupted, one meta dropped, one intact. When collected. Then record written; `missing` names each by agent id and kind; run cost `source: "estimate"`; the dropped transcript's `usd`, `ms` and tokens are null; the truncated transcript (built with at least one complete request before the cut, so the collector's keep-partial-figures branch runs) has `transcript: "truncated"`, a `missing` `transcript-truncated` entry and no figure equal to 0 (non-zero partial figures are allowed, as the reused collector keeps them); the corrupted-meta and dropped-meta agents never crash the run and never appear as a zero row: either they are absent, or (if the collector recovers them from the orchestrator's `Agent` tool_use lines) they are listed with `what: "meta"` | CLI, real files | test_stats_agents_collect.py |
| TC-26 | 3.16 | integration | Given `projects/` chmod 000 (skip as root). When `collect --agents <id>`. Then exit non-zero, output says "outside the sandbox", no record; after restoring, the manifest is still open | CLI, real permissions | test_stats_agents_collect.py |
| TC-27 | 3.17, D7 | integration | Given `general-purpose` agents: unmarked with `create requirements`; marked with an id that has no manifest; with the marker not at the start; `[CK:<id>]`; `[ck: <id>]`. When `collect --agents <known id>` and `collect --all` (stale manifest). Then none of them is in any record | CLI | test_stats_agents_collect.py |
| TC-28 | 3.18, NFR compat | integration (metamorphic) | Given a workflow implement run collected in home H1; and H2 = same run plus an agent-mode run and its manifest. When both collected. Then the workflow run's record files are byte-identical apart from `collectedAt`-style timestamps the existing tests already ignore; `--run` output identical, and re-collecting it reports `unchanged`. Variant: the workflow agents' metas carry `worktreePath` / `worktreeBranch` (`AgentSpec.extra_meta`), and a record of the same run without those terms is already stored: the first re-collect is `updated`, the old and new records differ only in `terms` (which gains both values), and the next re-collect is `unchanged` | CLI, store | test_stats_agents_collect.py |
| TC-29 | 4.1, 4.2, 4.5, 4.6 | integration | Given agents: `spec:implementer` `implement task 3`; `ck-implementer` `implement task 3 retry 2`; `spec:test-writer` `implement task 1`; `spec:implementer` `review task 3 round 2`; `ck-reviewer` `review task 3 round 1`; `spec:fixer` `fix 1`; `spec:resolver` `resolve task 4`; `general-purpose` `implement task 3`, `review task 3 round 1`, `resolve task 4` and `fix 2` (4.6); one with meta `model` opus and transcript sonnet. Then rows have role/task/attempt/round implementer/3/1, implementer/3/3, test-writer/1/1, reviewer/3/-/2, reviewer/3/-/1, fixer/-/1, resolver/4, and for the `general-purpose` rows implementer/3/1, reviewer/3/-/1, resolver/4, fixer/-/2; the meta model wins when present, else the transcript model; each row equals `parse_agent_description(meta.description, meta.agentType)` (boundary uses the seam) | CLI, seam | test_stats_agents_collect.py |
| TC-30 | 4.3 | integration (metamorphic) | Given a create run of `general-purpose` agents, one per `AGENT_CREATE_STEPS` step, and a workflow spec-create run whose agents carry the mapped workflow labels with the same requests. When both collected. Then the two records' agent `role` and `phase` lists are equal | CLI, seam constant | test_stats_agents_collect.py |
| TC-31 | 4.4 | integration | Given marked agents `implement Task 3`, `implement demo: task 3`, `review task 3`, `deploy`. Then each row is `other`, the record's unmatched count is 4, and `ck stats show` prints the `other:` line with 4 agents, their minutes and dollars | CLI (`collect`, `show`) | test_stats_agents_collect.py |
| TC-32 | 5.1, 5.4 | static doc | In implement, create, review and spawn-worktree skills and `without-workflow-tool.md` (packaged + vendored): the agent-mode path ends with `ck stats collect --agents <run_id> --auto`, says outside the sandbox, comes after the spend line (implement) / before open questions (create, review), reports where the record went or the retry command, and says the result never changes the run's report | file read | test_stats_agents_docs.py |
| TC-33 | 5.2 | integration (table) | `collect --agents <id> --auto` when: collected; id invalid; not found; no marked agents; projects unreadable. Then exit 0 every time; stdout is one line holding the record path, or the reason plus `ck stats collect --agents <id>` | CLI | test_stats_agents_collect.py |
| TC-34 | 5.3 | integration | Given `ck agents set run.analytics false`. When `--agents <id> --auto`. Then exit 0, says skipped, no record, manifest open; manual `--agents <id>` then collects | CLI, config | test_stats_agents_collect.py |
| TC-35 | 6.1, 6.2 | integration | Given (in a `ck init` project) two manifests backdated 7 h and one 5 h, specs/roots/branches from `CONFIDENTIAL`. When `ck doctor`. Then a warning counts 2 open runs and suggests `ck stats collect --all`; no `CONFIDENTIAL` term in the output. With only the 5 h manifest: no such warning | CLI (`doctor`) | test_stats_agents_open.py |
| TC-36 | 6.3, 6.4, 6.7 | integration | Given a workflow run, a 7 h manifest with marked agents, a 5 h manifest with marked agents, marked agents of an id without manifest, and unmarked spec-type agents. When `collect --all`. Then the summary lines `collected: 2` (workflow + stale), `unfinished: 1`, `failed: 0` (existing spellings, `already had: N` / JSON `already_had` for re-runs); the stale record is partial and its manifest closed; the young one is untouched and open; nothing recorded for the no-manifest id or the unmarked agents | CLI, store | test_stats_agents_open.py |
| TC-37 | 6.5 | integration | Given a 7 h manifest without marked agents in any session and a collectable workflow run. When `collect --all`. Then the workflow run is collected; the manifest's id is reported once as `abandoned` with "no agents carry the marker"; `abandoned: 1`, `failed: 0`, exit 0; no record for the id. Then a second `collect --all` and `ck doctor` do not mention it (no `abandoned` line, no open-run warning) | CLI | test_stats_agents_open.py |
| TC-38 | 6.6, 3.3 | integration | Given a manifest file overwritten with `{"spec": "zephyr-payroll"` (truncated JSON) and one with a JSON list. When `collect --all`: both counted failed, output contains no `zephyr-payroll`. When `ck doctor`: a separate warning counts 2 unreadable manifests. When `collect --agents <that id>` (with marked agents present): exit 1, `run manifest not recognised`, no record, the manifest file's bytes unchanged, output contains no `zephyr-payroll`; with `--auto`: exit 0 and that reason as one line | CLI | test_stats_agents_open.py |
| TC-39 | 7.1 | integration | Given sessions with old-style implement agents. When `--agents-backfill --dry-run` (text and `--json`). Then per group: session id, spec, agent count, start, wall time, estimated cost; excluded agents with reasons; `store_files(xdg)` identical before and after | CLI | test_stats_agents_backfill.py |
| TC-40 | 7.2 | integration | Given in one session: unmarked spec-type agents, unmarked `general-purpose` agents with `implement demo: task 1`, marked spec-type agents, and a workflow run's `spec:implementer`-looking agents. When dry run. Then only the unmarked spec-type agents are in groups; the others never are (the workflow agents are not even listed as candidates) | CLI | test_stats_agents_backfill.py |
| TC-41 | 7.3 | integration | Given one session with `demo` agents at 09:00-10:00, `ledger` agents interleaved at 09:30, and `demo` agents again at 13:00. When dry run: three groups (demo, ledger, demo). With `--gap 300`: two groups. With `--gap 30` and a 45-minute hole inside the first block: it splits. A 150-minute `demo` implementer followed 10 minutes after its end by a `demo` fixer stays one group under the default 2 h gap (the gap runs from one agent's end to the next start) | CLI | test_stats_agents_backfill.py |
| TC-42 | 7.4 | integration | Given `review task 4 diff` at 09:40 (inside the demo span) and at 18:00 (beyond the gap of every group). Then the first joins the demo group; the second is listed excluded with "no spec" | CLI | test_stats_agents_backfill.py |
| TC-43 | 7.5 | integration | Given sessions whose `cwd` is a `spec_repo` and `gitBranch` its target branch, and one session whose cwd no longer exists. When `--agents-backfill`. Then one record per group, each partial with `missing` `manifest`, `source: "agents"`, outcome from the git report, `project.root` = session cwd, `project.branch` = session branch; the gone-cwd group has outcome null and `missing` `outcome` | CLI, real git | test_stats_agents_backfill.py |
| TC-44 | 7.6 | integration | Run backfill twice. Then the same record file names, no new files; the second summary's `already had: N` equals the group count, and under `--json` every `results[].result` is `"unchanged"` | CLI, store | test_stats_agents_backfill.py |
| TC-45 | 7.7 | integration | Given only workflow runs and general-purpose agents. When backfill (with and without `--dry-run`). Then it says there are no candidate agents and exits 0 | CLI | test_stats_agents_backfill.py |
| TC-46 | 7.8 | integration (table) | `--gap` `0`, `-5`, `abc`, `1.5`, `""`: exit 2, nothing written | CLI | test_stats_agents_backfill.py |
| TC-47 | 7.9 | integration + static doc | After backfill, stdout has the summary lines `collected: N`, `already had: N`, `updated: N`, `failed: N` and `left out: N` (`--json`: `collected`, `already_had`, `updated`, `failed`, `excluded`). README.md's run-analytics section states that create and review runs from before this feature cannot be recovered | CLI, file read | test_stats_agents_backfill.py |
| TC-48 | 8.1, 8.2, 8.5 | integration | Given an agent-mode run (spec, root, branch, worktree paths/branches and descriptions from `CONFIDENTIAL`) and a workflow run, both collected. When `ck stats export --to <kiro checkout> --yes`. Then the agent-mode file has `source: "agents"` and `cost.source: "agent_transcripts"`, the workflow file `source: "workflow"`; `all_bytes(analytics/)` contains no `CONFIDENTIAL` term, run id, session id, agent id, description or worktree path/branch; both pass `validate_shareable` and `assert_documented` | CLI (`collect`, `export`), real git checkout | test_stats_agents_share.py |
| TC-49 | 8.3 | unit (table, plus P-12) | `validate_shareable` accepts a valid record without `source`, with `workflow`, with `agents`; rejects `source` `Agents`, `agent`, `workflows`, `""`, `null`, `1`, and `cost.source` `claude_total`, `agent-transcripts`; error messages never echo the bad value | seam | test_stats_agents_share.py |
| TC-50 | 8.4 | static + existing | `analytics/SCHEMA.md` `## Fields` documents `source` (values `workflow`, `agents`) and lists `agent_transcripts` as a `cost.source` value; `SCHEMA_SHARED == "ck-run-analytics/1"` and SCHEMA.md still names it; the existing `tests/test_stats_export.py` doc checks pass | file read, seam | test_stats_agents_share.py |
| TC-51 | 8.6 | integration | Given a shareable record written by ck 0.10.1 (fixture JSON without `source`, `tests/fixtures/stats/shared_0_10_1.json`). Then `validate_shareable` returns []; a kiro checkout already holding it in `analytics/runs/` takes a new `ck stats export` and the existing record's bytes are unchanged; `tests/test_stats_submit.py` and `tests/test_stats_export.py` pass unchanged | seam, CLI, real git checkout | test_stats_agents_share.py |
| TC-52 | 9.1 | integration (parametrized: workflow, agent mode) | Given a workflow run and an agent-mode run whose agent metas carry `worktreePath` `/home/dana/acme-ledger/.ck/worktrees/zephyr-payroll-task-3` and `worktreeBranch` `feat/zephyr-payroll-task-3`. When collected. Then `record["terms"]` contains both values, and `confidential_terms([record])` is a superset of `confidential_terms([probe])` where `probe` has only `project.root` = that path and `project.branch` = that branch | CLI, seam | test_stats_agents_share.py |
| TC-53 | 9.2 | integration | Given an agent-mode run. Then `confidential_terms([record])` contains the manifest's spec, root, `into` and the `ag_` run id (lower-cased) | CLI, seam | test_stats_agents_share.py |
| TC-54 | 9.3 | integration (pty) | Given the `World` flow with an agent-mode run collected. When the fake drafter's note contains the worktree path; the worktree branch; the `ag_` run id (three cases). Then the export prints a leak hit with the note pseudo-path and a line number and writes no note | CLI under pty, fake `claude`, real git | test_stats_agents_share.py |
| TC-55 | 9.4 | unit | `claude_kiro.analytics.leak.VOCABULARY` contains `workflow`, `agents`, `agent_transcripts`; `leak_scan` of an exported agent-mode record against its own local terms has no hits | seam | test_stats_agents_share.py |
| TC-56 | NFR performance | integration | One agent-mode run of 100 agents (3 requests each) built before the clock; `collect --agents` under 5 s | CLI | test_stats_perf.py |
| TC-57 | NFR performance | integration | `backfill_home` with 50 sessions x 6 agents; `--agents-backfill --dry-run` under 30 s | CLI | test_stats_perf.py |
| TC-58 | NFR never blocks, 1.5, 5.2, 5.4 | integration | With the store read-only and the Claude home unreadable: `begin` exits 0 with an id and `collect --agents <id> --auto` exits 0 with one line (the doc side is TC-7 / TC-32) | CLI | test_stats_agents_collect.py |
| TC-60 | 7.6 | integration | Given one session whose `demo` block splits into two groups under `--gap 30` but is one group under the default. When `--agents-backfill` (default gap), then `--agents-backfill --gap 30 --dry-run`, then `--agents-backfill --gap 30`. Then the dry run lists the split groups' agents under `excluded` with `overlaps backfill record <id>` (the id of the first run's record); after the third run no agent id appears in more than one record file, the record files are the same as after the first run, and the total `usd` across records is unchanged | CLI, store | test_stats_agents_backfill.py |
| TC-61 | 5.5 | integration | (a) With `CK_RUN_SANDBOXED=1` and `projects/` unreadable: `collect --agents <id> --auto` exits 0, prints one line saying `ck run` collects the run after the session exits, and the manifest stays open (`ck doctor` after a 7 h backdate still counts it). (b) After restoring access, `claude_kiro.cli.run.auto_collect_after_run(cwd, sid, started)` called in-process (as `tests/test_run.py` drives `ck run` code) over a session holding the marked agents of a manifest whose `sessionId` is that session and the marked agents of a second id without a manifest: it returns one `stats:` line per id; both records exist (the second partial with `missing` `manifest`); the first manifest is closed; a workflow run in the same session is still collected | CLI, in-process `ck run` hook, store | test_stats_agents_collect.py |
| TC-59 | NFR local file protection | integration | After `begin` into a fresh `XDG_DATA_HOME`: every directory ck created on the manifest's path is 0700 and the manifest 0600; after `collect --agents`, the record 0600 | CLI, real permissions | test_stats_agents_begin.py |

## Properties

Library: **Hypothesis** (already a dev dependency). Pure-seam properties use
`@settings(derandomize=True, max_examples=200, database=None, deadline=None, suppress_health_check=list(HealthCheck))`.
CLI properties (P-15 to P-17) use the same with `max_examples=20` and build every example inside
`with fresh_env() as env:`. Each file carries a `PROPERTIES = {"P-n": "test_name"}` map, as the existing property
files do.

| ID | Requirement | Property | Generator domain | Expected decision |
|---|---|---|---|---|
| P-1 | 1.1 | For any set of taken ids and any candidate stream, `new_agent_run_id(taken, token=stream)` returns a value matching `^ag_[0-9a-f]{8}\Z` that is not in `taken` | `taken`: 0-50 ids drawn from valid `ag_` ids plus the stream's own first values (forced collisions); stream: lists whose head is taken ids, uppercase hex (`0123ABCD`), 7 and 9 chars, non-hex (`0123abcg`), fullwidth digits, `""`, followed by one free valid token | the first free, valid candidate; invalid and taken candidates are skipped, never returned |
| P-2 | 3.13, 1.1 | `is_agent_run_id(s)` is true exactly for `ag_` + 8 lowercase hex and nothing else (complement: every near-miss is false, and the CLI exits 2 on it, see TC-23) | positives: random 8-hex; negatives: `AG_`/`Ag_` prefix, uppercase or mixed hex, 7/9 hex, `ag-`, `ag__`, leading/trailing spaces, trailing `\n` (the `$` trap), `\t`, `wf_` + 8 hex, `wf_` + 12 hex, `[ck:ag_x]` wrapped, fullwidth/Arabic-Indic digits, `None`, ints | true for positives, false for every negative |
| P-3 | 2.1, 4.1, 4.2 | For any implement template, run id, task id N, k and agent type of the matching work, `parse_agent_description(described(id, rendered), type)` returns `run_id` = id and the expected role, task, attempt (`retry k` -> `k+1`, plain -> 1) and round | templates: the six D4 forms; N: 1-999 and ids `3_a`, `T1`; k: 1-99; type: all ten `SPEC_AGENT_TYPES` (verb refines: implementer-type + `review` -> reviewer; test-writer type + `implement` -> test-writer) | role, task, attempt, round as the template table; `matched` true |
| P-4 | 4.4 | For any marked description that is not exactly a template, the parse is `role: "other"`, `matched: false`, no task, attempt or round (never a guess) | near-misses from real data: old style with marker `implement demo: task 3`, `implement Task 3`, `implement task`, `implement task 3 retry`, `review task 3 round`, `review task 3 round 0x2`, `fix demo: 2`, trailing space, double space, tab, `implement task 3 (opus)`, workflow labels `task 3 [test]`, `land 1 2`, unknown `create` step, random text | `other`, `matched: false`, task/attempt/round null |
| P-5 | 3.1, 3.17 (D7) | `parse_agent_description(d, t)["run_id"]` is the id only when `d` starts with exactly `[ck:<valid id>]` followed by a space or the end; any other description gives `None`. Metamorphic: prefixing any text, changing the bracket, case or spacing of the marker never yields an id | `d`: valid markers + rest; negatives: marker after a word, `[CK:`, `[ck: `, `[ck:ag_X]` with invalid id, `(ck:...)`, `[ck:id]x` (no space), leading space, two markers (only the first counts), `general-purpose` type with old descriptions; `t`: spec types, `general-purpose`, `None`, `Explore` | id for exact markers, `None` otherwise, independent of `t` |
| P-6 | 2.2, 2.3 | For every template extracted from the docs (TC-8 extractor) and any values of its allowed placeholders, the rendered description (marked and marker-less) contains no `CONFIDENTIAL` term, contains no `<` or `>` left, and parses to a role other than `other` for the agent types the doc pairs with it | templates: all extracted; `<N>` 1-999, `<k>` 1-99, `<run_id>` valid ids; types from the doc's subagent table incl. vendored `ck-*` | parses to a known role; no confidential text |
| P-7 | 4.3 | For every step in `AGENT_CREATE_STEPS`, `parse_agent_description(described(id, "create " + step), "general-purpose")` has the same `role` and `step` as `parse_label(AGENT_CREATE_STEPS[step], "spec-create")`; for any string not in the keys, `create <s>` is `other` | keys of the mapping; negatives: key with case changes, trailing `.md`, the workflow label itself (`create write requirements.md`), `""` | equal role and step; `other` for negatives |
| P-8 | 3.4 (and the requirements assumption on outcome mapping), 1.2 | For any `ck worktree report` dict, `implement_outcome(r)` is `complete` iff every task is in `merged` or `done`; `failed` iff any task is in `failed`; otherwise `blocked`; `None` for `None`. Metamorphic: moving a task from `merged` to `failed` never improves the outcome (order complete > blocked > failed); permuting list order never changes it. Run scoping, over `implement_summary(r, done_at_start=D, start_ms=s, end_ms=e)`: with `D` given, `merged` = (`merged` ∪ `done`) minus `D` for every subset `D` of the task ids (never a task in `D`); with `D` None, `merged` holds exactly the `merged` entries whose `mergedAt` parses into `[s, e]`, so a merge one second before `s` or after `e` is never counted; `None` report gives every field `None`, never `[]` | reports with 0-20 tasks spread over the five lists, empty lists, an empty task list; `D` subsets incl. empty and all; `mergedAt` as `%cI` strings with `Z`, `+00:00`, `+02:00`, around `s` and `e` (+/- 1 s), unparseable `mergedAt` | as stated |
| P-9 | 6.1, 6.2, 6.3, 6.6 | For any start time and now, `open_manifest_due(start, now)` is true iff `now - start > 6 h`; exactly 6 h is false; unparseable start gives `None`. Same path as the CLI: for a manifest dict `m` valid apart from that `startTime`, `classify_manifest(m, now)` is `"collect"` iff `open_manifest_due` is true, `"young"` iff false, and `"malformed"` iff `None` (both go through `manifest_start_ms`, the parser doctor and `--all` use) | offsets around the boundary (6 h +/- 1 s, +/- 1 ms), far past, future starts; surface forms: `Z`, `+00:00`, `+02:00`, fractional seconds, no seconds; negatives: `""`, `"yesterday"`, numbers, `None`, naive time | true / false / None as stated; same instant in different offsets gives the same answer |
| P-10 | 7.1, 7.2 | For any candidate list, every agent in any `group_backfill` group is unmarked, of a `SPEC_AGENT_TYPES` type and not from a workflow run (complement: every marked, `general-purpose`, other-typed or workflow agent is in `excluded` with its reason, `carries a marker` / `not a spec agent type` / `workflow agent`, or absent for workflow agents) | agents mixing types (`spec:*`, `ck-*`, `general-purpose`, `Explore`, `None`, `spec:planner`), markers valid and invalid, `workflow` true/false, old descriptions with specs drawn from `demo`, `ledger`, `zephyr-payroll` | allowlist holds; no excluded type ever in a group |
| P-11 | 7.3, 7.4 | Groups partition the included agents (each in exactly one group); a group never mixes sessions or parsed specs; inside a group, ordered by start, each member starts at most `gap` after the latest end among the members before it; two consecutive groups of the same session and spec are separated by more than `gap` (the later group's first start minus the earlier group's last end); a spec-less agent joins a group only if within `gap` of its span, else is excluded with `no spec`. Metamorphic: permuting the input never changes the groups; raising `gap` never increases the number of groups | 1-40 agents over 1-4 sessions, 0-3 specs, starts over a day, durations 1 s-4 h (long agents included), gaps 1-600 min, spec-less share 0-50 % | as stated |
| P-12 | 7.6 | Group ids from `group_backfill` are a pure function of the group's members: same input -> same ids; any permutation -> same ids; adding an agent to another session leaves this session's group ids unchanged | as P-11 | ids stable |
| P-13 | 8.1, 8.2, 8.5 | For any local record (`seeded_local_record` variants with `source` `agents` and agent-mode extras: `ag_` run id, worktree terms, descriptions as labels), `build_shareable(rec)` has `source` = `agents` (or `workflow` for records without `source`), `cost.source` = `agent_transcripts` iff the local run-scope cost source is `agent_transcripts`, passes `validate_shareable`, and its JSON contains no `CONFIDENTIAL` term, no `ag_` id, session id, agent id or label | seeded records x kind (implement, create, review mode) x cost source (`agent_transcripts`, `estimate`) x missing/none `source` | as stated |
| P-14 | 8.3, 8.6 | `validate_shareable` accepts any valid record with `source` absent or in {`workflow`, `agents`} and rejects every other `source` and every `cost.source` outside the documented set. Metamorphic: deleting `source` from a valid record keeps it valid (old records unchanged) | valid records from P-13; `source` negatives: near-misses `Agents`, `agent`, `agents `, `workflows`, `wf`, `transcripts`, `agent_transcripts` (a cost value in the wrong field), `""`, `null`, `0`, `[]`; `cost.source` negatives: `claude_total` (a real local value), `agent-transcripts`, `Agent_Transcripts`, `priced_from_transcript` | accept / reject as stated |
| P-15 | 3.2 (NFR agreement) | For any set of marked agents (1-6, models from the three families plus one unknown model, request token counts 0-1e6, duplicated `requestId`s split across lines), the collected run-scope `usd` equals the sum of per-agent `usd` and `ck cost --session <sid> --json` `priced_usd` to the cent; with an unknown-model agent both are null/estimate together | as stated (real CLI, `fresh_env`) | equal to the cent, or both unknown |
| P-16 | 9.1, 9.2 | For any `worktreePath` / `worktreeBranch` surface form, after collection `confidential_terms([record])` is a superset of `confidential_terms([probe])` (probe with that path as `project.root` and branch as `project.branch`), and of the manifest's spec/root/into and run id | paths: absolute, relative `.ck/worktrees/x-task-1`, with `..`, trailing `/`, `~/`, spaces, unicode, Windows-style `C:\\work\\x`; branches: `feat/<spec>-task-N`, nested `a/b/c`, unicode; values taken from `CONFIDENTIAL` mixes (real CLI) | superset holds |
| P-17 | 5.2 | For any id string and any failure state, `ck stats collect --agents <id> --auto` exits 0 and prints exactly one line | ids: P-2 positives and negatives, shell-hostile strings (`$(x)`, `;`, quotes); states: no manifest, no agents, unreadable projects, malformed manifest, read-only store, setting off | exit 0, one stdout line |
| P-18 | 7.5, 7.6, 3.4 | For any backfill plan, two groups of the same session and spec never share a merged task: for each group `g` of `group_backfill(agents, gap_minutes=m)`, `implement_summary(r, done_at_start=None, start_ms=g.startMs, end_ms=g.endMs + min(30 min, m))["merged"]` is pairwise disjoint across those groups for any report `r` (pure seams) | the P-11 agent domain restricted to one spec per session; reports whose `mergedAt` values fall before, inside, between and after the groups; `m` 1-600 | disjoint merged sets |

## Harness Contract

Seams are exported lazily from `claude_kiro.analytics` (add them to `_EXPORTS`); the module that holds each is a
design choice. Tests import them inside test bodies.

```python
SPEC_AGENT_TYPES: FrozenSet[str]   # spec:implementer, spec:test-writer, spec:fixer, spec:resolver, spec:reviewer,
                                   # ck-implementer, ck-test-writer, ck-fixer, ck-resolver, ck-reviewer
AGENT_CREATE_STEPS: Mapping[str, str]   # create <step> name -> the spec-create workflow label of that step
                                        # (e.g. "requirements" -> "write requirements.md"); names are design's choice

def is_agent_run_id(value: object) -> bool          # fullmatch ag_[0-9a-f]{8}; False for non-str
def new_agent_run_id(taken: Collection[str], *, token: Optional[Callable[[], str]] = None) -> str
    # token() yields 8-char candidates (default: secrets.token_hex(4)); invalid or taken candidates are skipped

def parse_agent_description(description: object, agent_type: object) -> dict
    # keys: run_id (str | None), role (one of analytics.ROLES), matched (bool),
    #       task (str | None), attempt (int | None), round (int | None), step (str | None)
    # marker-less descriptions parse the same templates with run_id None

def implement_outcome(report: Optional[Mapping]) -> Optional[str]
    # report = the `ck worktree report --json` object; "complete" | "blocked" | "failed" | None
def implement_summary(report: Optional[Mapping], *, done_at_start: Optional[Collection[str]] = None,
                      start_ms: Optional[int] = None, end_ms: Optional[int] = None) -> dict
    # keys outcome, merged, failed, blocked, not_started (lists of task ids, or None when unknown, never [] for
    # unknown); merged is run-scoped: minus done_at_start, or (done_at_start None) mergedAt within [start_ms, end_ms]

def open_manifest_due(start_time: object, now: datetime, *, hours: float = 6) -> Optional[bool]
    # None when start_time does not parse (same parser as the CLI)
def classify_manifest(manifest: object, now: datetime) -> str
    # "collect" | "young" | "malformed": the exact check + classification ck doctor and collect --all use

def group_backfill(agents: Sequence[Mapping], *, gap_minutes: int = 120) -> dict
    # agent: {"agentId": str, "sessionId": str, "agentType": str | None, "description": str | None,
    #         "startMs": int | None, "endMs": int | None, "workflow": bool}
    # -> {"groups": [{"id": str, "sessionId": str, "spec": str, "agentIds": [str, ...] (by start),
    #                 "startMs": int, "endMs": int}],
    #     "excluded": [{"agentId": str, "reason": "workflow agent" | "not a spec agent type" |
    #                   "carries a marker" | "no spec" | "no timing"}]}
    # a dict view over the same candidate filter and grouping function the CLI's backfill uses; the gap runs
    # from one member's end to the next member's start; these reason strings are what the CLI prints
```

Existing seams the tests also call, signatures unchanged: `parse_label(label, workflow)`,
`build_shareable(local, ...)`, `claude_kiro.analytics.share.validate_shareable(record) -> List[str]`,
`confidential_terms(records)`, `leak_scan(files, terms)`, `claude_kiro.analytics.leak.VOCABULARY`,
`SCHEMA_SHARED`. `share.SOURCES == ("workflow", "agents")` and `share.COST_SOURCES` gains `"agent_transcripts"`.

CLI:

- `ck stats begin --kind {implement|create|review} --spec NAME [--into BRANCH] [--json]`. `--json` stdout:
  `{"run_id": str, "manifest": str | null}` and nothing else. Text: the run id is the first stdout line. Bad
  arguments exit 2; an unwritable store exits 0 with a stderr warning. Test hook: `CK_TEST_RUN_ID_DRAWS` (comma-
  separated 8-char tokens) is drawn from in order before random draws.
- `ck stats collect --agents RUN_ID [--auto] [--json]`; `--json`:
  `{"run_id", "result": "collected" | "unchanged" | "updated", "path", "partial": bool}`.
- `ck stats collect --agents-backfill [--dry-run] [--gap MINUTES] [--json]`; dry-run `--json`:
  `{"groups": [{"id", "session", "spec", "agents", "start", "wallMinutes", "usd"}], "excluded": [{"agentId",
  "reason"}]}`; the reason may also be `overlaps backfill record <id>`. Write mode prints `collected: N`,
  `already had: N`, `updated: N`, `failed: N`, `left out: N`; write `--json`: `{"results": [{"id", "result"}],
  "collected", "already_had", "updated", "failed", "excluded"}`.
- `ck stats collect --all` summary keeps its existing `label: N` lines and spellings (`collected`, `already had`
  (JSON `already_had`), `updated`, `unfinished`, `failed`) and counts agent-mode runs in them; it adds
  `abandoned: N` (JSON `abandoned`) only when a stale manifest without marked agents was closed (6.5).
- `collect --agents` on a malformed manifest: exit 1, `run manifest not recognised`, nothing written.
- `ck doctor` open-manifest warning names the count and `ck stats collect --all`; unreadable manifests have their own
  warning line.

Run manifest (JSON file; path printed by `begin --json`; mode 0600):
`{"schema": "ck-run-manifest/1", "runId", "kind", "startTime" (ISO UTC), "sessionId" (str | null), "ckVersion",
"spec", "root", "into", "doneAtStart" (list of task ids | null; implement), "agents" (per config role
implementer, test_writer, reviewer, reviewer_risky, fixer, resolver, orchestrator: `{"model", "effort", "enabled"}`
as `ck agents --json` `roles` resolves them, `reviewer_risky` from the reviewer's `risky_model` / `risky_effort` /
`enabled`, keys a role lacks `null`; never `allowed_domains`, `machine_label`, `sources` or `runtime`)}`.
`startTime` is the only clock field; there is no `startMs`. Tests write `startTime`, `sessionId` and `doneAtStart`.
A closed manifest is not reported by doctor and not collected by `--all` (whether it is deleted or marked is the
design's choice; tests only observe that).

Local record additions (schema `ck-run-local/1`, additive): `source: "agents"` on agent-mode records (workflow
records may carry `"workflow"` or leave it out); `workflow` `spec-implement` / `spec-create` and `mode`
`create` / `review` for the create kinds; `missing` entries `{"agentId": null, "what": "manifest"}` and
`{"agentId": null, "what": "outcome"}`; run-scope `costs[].source` may be `"agent_transcripts"`; `terms` include
worktree paths and branches; an unmatched-agent count readable by `ck stats show`'s existing `other:` line.

Documents: agent-mode description templates appear as inline code starting with `` `[ck:<run_id>] `` and use only
the placeholders `<run_id>`, `<N>`, `<k>`; each create step is written out as its own template
(`` `[ck:<run_id>] create requirements` ``), so the extractor sees the fixed step list.

## Critical Paths

1. **Agent-mode implement run, end to end:** `ck stats begin --kind implement` (TC-1) -> marked Agent
   descriptions per template (TC-8, TC-10) -> agents run (fixture) -> `ck stats collect --agents <id> --auto`
   (TC-13, TC-33) -> outcome from git (TC-16) -> record priced like `ck cost` (TC-14) -> `ck stats show` (TC-31)
   -> `ck stats export` content-free with `source: agents` (TC-48).
2. **Agent-mode create / review run:** `begin --kind create|review` (TC-2) -> `create <step>` agents of
   `general-purpose` (TC-30) -> collect without a git report (TC-18) -> unmarked general-purpose agents never
   included (TC-27).
3. **Crashed run:** `begin` -> agents -> session ends without collect -> after 6 h `ck doctor` reports it (TC-35)
   -> `ck stats collect --all` collects it partial and closes it (TC-36); a stale manifest without agents is closed
   once as `abandoned`, not a failure (TC-37). Under `ck run`, the session's agent-mode runs are collected after it
   exits (TC-61).
4. **Analytics never block a run:** unwritable store at `begin` (TC-5), unreadable home and every failure under
   `--auto` (TC-33, TC-58, P-17), docs say continue (TC-7, TC-32).
5. **Backfill of older runs:** `--agents-backfill --dry-run` preview (TC-39, TC-40, TC-41, TC-42) -> write
   (TC-43) -> rerun is idempotent (TC-44).
6. **Privacy:** templates carry no content (TC-9, P-6) -> worktree paths/branches and `ag_` ids are confidential
   terms (TC-52, TC-53) -> the leak check refuses a note that holds them (TC-54) -> shared records hold none of them
   (TC-48, P-13).

## Commands

```bash
# whole suite (the gate's verify line)
uv run pytest -q -n auto

# per file
uv run pytest -q tests/test_stats_agents_begin.py
uv run pytest -q tests/test_stats_agents_docs.py
uv run pytest -q -n auto tests/test_stats_agents_collect.py
uv run pytest -q tests/test_stats_agents_open.py
uv run pytest -q -n auto tests/test_stats_agents_backfill.py
uv run pytest -q -n auto tests/test_stats_agents_share.py
uv run pytest -q tests/test_stats_properties_agents.py
uv run pytest -q -n auto tests/test_stats_properties_agents_cli.py
uv run pytest -q tests/test_stats_perf.py

# backward compatibility and vendored copies
uv run pytest -q -n auto tests/test_stats_*.py tests/test_plugin.py tests/test_cost.py tests/test_worktree_report.py

# properties only
uv run pytest -q -n auto tests/test_stats_properties_agents.py tests/test_stats_properties_agents_cli.py
```

## Not Tested

- **A real Claude Code session using the Agent tool.** Tests write the transcript and meta layout Claude Code
  produces today (keys listed in the requirements' Constraints). Whether a live orchestrator follows the templates
  is covered only by the static document tests (TC-7 to TC-11, TC-32); a manual run on a machine without the
  Workflow tool is the acceptance check for that.
- **How `begin` finds the session id.** The requirements leave it open ("when it can be determined"). Tests check
  the manifest key is a string or null and cover collect for both (TC-15) by writing `sessionId` into the manifest.
- **The 1 s `begin` budget on slow CI machines.** TC-6 subtracts Python start-up measured in the same environment;
  it is a smoke check, not a benchmark.
- **The backfill gap default as a tuning question.** Tests check the 2 h default and `--gap`; whether 2 h is the
  right value is the PLAN's open question, answered with `--dry-run` on real data.
- **Hand-run fixer agents landing in backfill groups.** By design (PLAN open question) they may; the dry-run list
  (TC-39) is how a user sees them. No test asserts they are kept out.
- **Create/review backfill**, Option B/C, timing-only grouping for new runs, new schema versions: out of scope (D3,
  D7, D8, D1).
- **Network absence beyond the netguard.** Non-Python children (git) get file-only protocols and a dead proxy, as
  in the existing suite; no packet capture.
