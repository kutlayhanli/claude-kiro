# Plan: Run Analytics

**Created:** 2026-10-28
**Status:** Decisions Made

## Problem Statement

Whole-spec runs produce good work but feel slow and are costly, and nothing tells us why. One recent
`spec-implement` run spawned 249 agents, took 1h16m wall time and cost $133.40 (Opus $91.65, Sonnet
$41.75) even though implementers run on Sonnet. ck 0.10.0 keeps no history: `ck cost` recomputes spend
on demand, the workflow's rich result object is shown once and then only survives in Claude Code's
private run file, and there is no cross-run view of where time and money go (implementing, review
rounds, retries, conflict resolution, gates, idle waits).

We want each spec process (spec:create, spec:implement / `ck run`, spec:plan) to leave a durable,
comparable record, and we want those records from several machines (personal and work) to flow into
the claude-kiro repository as reviewable pull requests, so improvements to ck can be driven by data.
Records must carry no confidential information about the project they were measured on, but may
describe issues found during development or speccing in sanitized, ck-level terms.

## What Exists Today (ck 0.10.0)

Everything needed for a full breakdown is already on disk after a run; nothing aggregates or keeps it.

| Source | Content | Notes |
|---|---|---|
| `~/.claude/projects/<proj>/<sid>/workflows/wf_*.json` (run file) | `durationMs`, `startTime`, `status`, `agentCount`, `workflowName`, `workflowProgress[]` (per agent: `label`, `phaseTitle`, `model`, `startedAt`, `durationMs`, `attempt`, `tokens`, `state`), and `result` = the workflow's return object | 75 completed spec-implement and 8 spec-create runs on this machine alone: a ready backfill corpus |
| `.../subagents/workflows/wf_*/agent-<id>.jsonl` + `agent-<id>.meta.json` | Real token usage per request; sidecar has `description` (= workflow label), `workflowPhase`, `model` | `bench.py:scan_transcript` already prices these, dedups by `requestId` |
| spec-implement `result` (`plugin/workflows/spec-implement.js:953-982`) | `outcome`, `timing{minutes, phases[]}`, `landings[{size, merged, red, conflict, bisected, minutes}]`, `escalated`, `fixerEscalations`, `fullGates`, `finalGate`, `crossTests`, `stats{merged, failed, fixes, landings, bisected, planSteps}`, per-task `attempts`, `review{verdict, rounds}`, `agentMinutes`, `toMergedMinutes`, `riskTag`, models | Missing: conflict-resolution count per task (`resolves` is local in `landTask`, `:605`), cost per agent |
| spec-create `result` (`plugin/workflows/spec-create.js:797-817`) | `review{result, fixed, notFixed, escalated, refutedCount, lensesLost}`, `consistency{passes, fixed}` | No timestamps in result; the run file has them |
| spec:plan | Interactive, no workflow; only output is `PLAN.md` | Measurable only via its session transcript |
| `ck bench` results (`ck-bench-result/1`) | Arm-level wall time, cost with `source`/`basis`, `report.tasks[]` | Closest existing schema; reuse its cost and report extraction |
| `ck run` log `.claude/ck-runs/*.log` | Text; does not learn the run id | Outer ck process can read `~/.claude` after the sandboxed session exits |

Constraints found in the code:
- The Workflow script API cannot write files (`spec-implement.js:465`); any write at run time must be an agent running a ck command.
- Inside `ck run` the session is sandboxed and cannot read `~/.claude` (`run.py:183`); only the outer process can.
- The run directory also contains workflows that are not ck's (names alone would leak: e.g. tax and competitor reviews). A collector must allowlist ck workflow names.

## Research Summary

### Sources Consulted
- [Claude Code monitoring (OpenTelemetry)](https://code.claude.com/docs/en/monitoring-usage) - metrics/events now carry `workflow.run_id`, `agent_id`, `agent.name`, `query_source`, and `api_request` has `cost_usd`, `duration_ms`. But `user.email` and `organization.id` are always included with no switch, and there is no file exporter (needs an OTel collector).
- [ccusage](https://npmjs.com/package/ccusage) - the established approach is post-hoc analysis of local `~/.claude/projects/**/*.jsonl`; explicitly notes multi-machine use needs the files synced.
- [Opt-in telemetry spec (go-tool-base)](https://go-tool-base-ff2fdf.gitlab.io/development/specs/2026-03-21-opt-in-telemetry/) and [anonymous CLI analytics (DEV)](https://dev.to/chengyixu/add-anonymous-usage-analytics-to-your-cli-tool-4f88) - opt-in default; exclude paths, command arguments, user ids; collect only what improves the tool; publish the field list.
- [Skybridge telemetry](https://docs.skybridge.tech/telemetry) and [Devbox telemetry](https://www.jetify.com/devbox/docs/telemetry/) - exclude project code, file contents, tool args/results; publish the telemetry code so users can verify claims.
- [releases.sh privacy](https://releases.sh/docs/privacy) - telemetry is best-effort and never blocks or fails the command.
- [GitHub CLI default-on telemetry backlash (The Register)](https://www.theregister.com/2026/04/22/github_opts_all_cli_users/) - default-on collection erodes trust.
- [Measure Before You Manage (arXiv 2608.31057)](https://arxiv.org/pdf/2608.31057) - their run file and cost ledger disagreed ($213.52 vs $235.99): record which source each cost came from.
- [Artificial Analysis coding-agent methodology](https://artificialanalysis.ai/agents/coding-agents) - define time-per-task and cost-per-task precisely and state what is excluded (harness, verifier time).
- [Fruxon observability](https://docs.fruxon.com/guides/observability/) - model a session as a tree with cost on each node, not only the total.
- [Ona daily-metrics template](https://ona.com/templates/daily-metrics) - precedent for metrics delivered as dated JSON files in a PR against a schema.

### Best Practices Found
- **Allowlist, don't redact.** Build the shareable record from enumerated fields (numbers, enums, ck versions, model aliases); never copy free text through a scrubber and hope (opt-in telemetry sources; Skybridge).
- **Opt-in and visible.** Nothing leaves the machine without an explicit command, and the user sees the exact bytes first (go-tool-base, GitHub CLI backlash).
- **Never block the run.** Collection failures are logged and swallowed (releases.sh).
- **Label cost provenance** (`claude_total` vs `priced_from_transcripts`, pricing date) (arXiv 2608.31057, existing `ck bench` `cost.source`).
- **Define metrics with their exclusions** (wall = first task start to last merge excludes plan and final gate today; say so) (Artificial Analysis).
- **Tree attribution:** cost and time per role and per phase, not just run totals (Fruxon).
- **Version the schema** (`ck-run-analytics/1`, like `ck-bench-result/1`).

### Patterns to Avoid
- **Raw OTel or transcript export to the repo** - carries email, org id, prompts, paths; one slip is permanent in git history.
- **Regex redaction of free text as the only guard** - misses business names, file names, domain terms.
- **Hashing project names as anonymization** - unsalted hashes of short names are trivially reversible by dictionary; salted hashes are fine only if the salt never leaves the machine.
- **Run-time capture inside the sandboxed session** - cannot see `~/.claude`; adds agent cost to every run.
- **Trusting workflow `tokens` for spend** - 7-14x low (already documented in the implement skill).

## Solution Options

### Option A: Post-hoc collector + allowlisted export + PR submission (recommended)

**Description:** A `ck stats` command group reads Claude Code's run files and agent transcripts after the fact.
- `ck stats collect` (also run automatically by `ck run` after the session exits, and offered at the end of `/spec:implement` and `/spec:create`) writes a full-fidelity local record per run to a machine-local store (`$XDG_DATA_HOME/claude-kiro/runs/<run_id>.json`), never in the project.
- `ck stats` / `ck stats show` prints breakdowns: time and cost per role (implementer, test-writer, reviewer, fixer, resolver, plan, land, gate, report), per phase, per task; retry/escalation/review-round/conflict/fix rates; idle time between task finish and merge; trends across runs.
- `ck stats export` builds the shareable record from an allowlisted schema only, plus an optional sanitized `issues` section, and writes it into a claude-kiro checkout as `analytics/runs/<yyyy-mm>/<random-id>.json` (+ `.md` for issues).
- `ck stats submit` opens a draft PR on a branch via `gh`, after showing the full diff and running a leak check.
- Backfills the existing corpus (75+8 runs here) on first collect.

**Pros:** zero change to run behaviour or cost; works on all past runs; privacy enforced by construction; reuses `bench.py` transcript and pricing code; same flow on every machine.
**Cons:** depends on Claude Code's private run-file format (already depended on by `ck cost`/`ck bench`); Claude Code may prune old transcripts, so collection should happen soon after a run.
**Complexity:** Medium
**Precedent:** ccusage (post-hoc local transcript analysis); ck bench (existing results schema).

---

### Option B: Run-time capture in the workflows

**Description:** The workflows gain a final "record" agent step that runs `ck stats record` with the result object; spec-implement is extended to track the missing counters (conflict resolutions per task) and clock times for plan/report phases.

**Pros:** captures counters the result lacks today; independent of Claude Code's run-file layout.
**Cons:** an extra agent per run (cost, time); cannot price spend inside the sandbox; no backfill; must touch two large workflow scripts; still needs Option A's export/submit for the repo side.
**Complexity:** Medium-High
**Precedent:** in-app instrumentation generally.

---

### Option C: OpenTelemetry pipeline

**Description:** Enable Claude Code OTel (`CLAUDE_CODE_ENABLE_TELEMETRY=1`) with a local OTel Collector writing a file; ck aggregates the events by `workflow.run_id` / `agent.name`.

**Pros:** official, per-request `cost_usd` and `duration_ms`; tool-level durations (where Bash/gates spend time).
**Cons:** requires running a collector on every machine (work machine may forbid or already route OTel to the employer); always-on `user.email`/`organization.id`; custom agent names collapse to `"custom"` so roles need the label anyway; no backfill.
**Complexity:** High
**Precedent:** enterprise Claude Code monitoring (Grafana, Honeycomb, OpenObserve guides).

---

### Option D: Ship raw-ish run records via a synced folder or private repo

**Description:** Copy local records (with paths and labels) to a private sync location; analyse centrally.

**Pros:** simplest; maximal detail.
**Cons:** violates the "no confidential info" and "PR for review" requirements; work data would leave the work machine raw.
**Complexity:** Low
**Precedent:** ccusage multi-machine sync advice. Rejected on requirements.

## Comparison Matrix

| Criteria | A: Post-hoc + export | B: Run-time capture | C: OTel | D: Raw sync |
|---|---|---|---|---|
| Implementation complexity | Medium | Medium-High | High | Low |
| Run cost/time overhead | None | +1 agent/run | Small | None |
| Backfill of past runs | Yes (83 here) | No | No | Yes |
| Per-role/phase attribution | Yes (labels + meta.json) | Yes | Partial (`custom`) | Yes |
| Privacy by construction | Yes (allowlist) | Needs A's export | Hard (email/org) | No |
| Works on work machine | Yes, no new services | Yes | Needs collector | Policy risk |
| Coupling | CC run-file format (already used) | Workflow internals | OTel schema (stable) | CC format |
| Risk level | Low | Medium | Medium-High | High |

## Complexity Assessment

**Overall: Medium.** Drivers:
- **Integration points:** Claude Code run file + transcripts (read-only), `ck run` post-exit hook, two skills' closing steps, `gh` for PRs.
- **Security surface (main driver):** the export/leak-check boundary is the whole value of the feature; it needs integration tests with seeded confidential strings.
- **Data quality:** role parsing from labels (`task 34 [test] revise 1`, `land`, `resolve`, `fix`, `re-check`, `full gate`, `cross-test`, `report`) is a contract with the workflow scripts; label changes must not silently misattribute.
- **Backwards compatibility:** old runs lack newer result fields; schema versioned and fields optional.
- **Concurrency:** none significant; multiple machines write distinct files (random ids), so PRs never conflict.

## Key Decisions

1. **Capture approach**
   - A: post-hoc collector reading run files and transcripts.
   - B: run-time recording inside workflows.
   - Recommendation: **A**, plus one small B-style change: expose `resolves` per task in the spec-implement result so conflict-resolution counts aren't inferred from labels.

2. **Where local full-fidelity records live**
   - Machine-local data dir (`$XDG_DATA_HOME/claude-kiro/runs/`), outside any project.
   - Inside the project (`.claude/ck-runs/`), gitignored.
   - Recommendation: **machine-local data dir.** Projects never accumulate analytics, and nothing can be committed to a client repo by accident.

3. **What the shareable record may contain (allowlist)**
   - Proposed allowed: schema version; random record id; ck version; Claude Code version; workflow kind (spec-implement / spec-create / spec-plan); outcome enum; month (not exact date); machine label chosen by user (e.g. `personal`, `work`) or omitted; role→model/effort config; counts (tasks, waves, agents by role, attempts, retries, escalations, review rounds and verdict enums, conflicts, fixes, landings, bisects, gate red/green); durations (wall, per phase, per role, per task as anonymous index); tokens and cost by model and by role with cost source and pricing date; stack hints from a fixed enum (`python`, `node`, ...), optional.
   - Proposed never: spec name, task titles, summaries, findings text, labels verbatim, file paths, branch names, repo URL, session/agent/run ids, prompts, email/org/account ids, exact timestamps.
   - Recommendation: adopt this list; the list itself is documented in the repo and enforced by the exporter's schema.

4. **How "issues found" are captured without leaking**
   - (a) Deterministic issue codes only: e.g. `gate_red_after_merge`, `resolver_rounds_exceeded`, `review_contract_mismatch`, `planning_failed`, `retry_no_commits`, `escalated_to_opus`, derived from result fields.
   - (b) (a) plus a free-text issues note drafted by an agent from the local record and failure reasons, written in ck-level terms ("resolver needed 3 rounds on a lockfile conflict"), then passed through a leak check (the project's own spec names, task titles, path segments, repo/org names, emails, branch names, plus generic secret patterns) and shown to me for editing before export.
   - Recommendation: **(b)**, with a hard rule that free text is never exported without my interactive approval (`--yes` disallowed for the issues section when free text is present).

5. **Submission mechanism**
   - `ck stats submit` creates a branch in a claude-kiro checkout, commits the files, opens a draft PR with `gh`.
   - Export only; I commit and open PRs myself.
   - Recommendation: **`submit` with draft PR**, preceded by a printed diff and leak check, one PR per batch (a machine's new records since last submit). Batch granularity keeps PR count low.

6. **Project identity across machines**
   - None (each record is standalone).
   - Salted pseudonym (HMAC of repo remote with a salt kept in global config, never exported) so I can see "same project over time" locally only.
   - Recommendation: **none in the shared record; pseudonym only in local records.** The repo-side analysis should be about ck, not about my projects.

7. **When collection runs**
   - Automatically after `ck run`, and as the last step of `/spec:implement` and `/spec:create` (the orchestrator runs `ck stats collect --run <id>` outside the sandbox like `ck cost` today); plus manual `ck stats collect --all` for backfill.
   - Manual only.
   - Recommendation: **automatic local collection** (it never leaves the machine), **manual export/submit**. Configurable off via `ck agents set run.analytics false`-style setting.

8. **spec:plan coverage**
   - Measure it from its session transcript (time between skill launch and PLAN.md decisions, research call count, cost) when the session id is known.
   - Skip it for v1.
   - Recommendation: **v1 covers spec-implement and spec-create; spec:plan is a stretch goal** because it has no run id and is interactive (wall time mostly reflects my thinking time).

## Open Questions

- [x] Work machine submits the same way as personal machines (draft PR via `gh`); records carry ck-level data only (D5).
- [x] Target repo: `kutlayhanli/claude-kiro` only (D9).
- [x] Month granularity (`yyyy-mm`) only (D9).
- [ ] Should the repo also get an aggregate report (e.g. `ck stats report` over `analytics/runs/` producing `analytics/SUMMARY.md`) in v1, or later?
- [ ] Technical: confirm Claude Code's run-file retention (how long until `workflows/*.json` and transcripts are pruned) to size the "collect soon" guarantee.
- [ ] Technical: label grammar is defined in spec-implement.js; should it move to a shared constant that the collector's tests pin?

## Recommended Direction

**Approach:** Option A (post-hoc collector, local store, allowlisted export, draft-PR submit), plus the small result-field addition from Decision 1.

**Rationale:**
- All needed data already exists after each run; collecting it costs nothing at run time and backfills 83 runs immediately.
- Privacy is enforced structurally (the export can only emit allowlisted fields), with a second deterministic leak check and my review for any free text.
- Same commands on personal and work machines; no services, collectors or network beyond `gh` at submit time.
- Reuses `bench.py` transcript parsing and pricing, so `ck cost`, `ck bench` and `ck stats` agree on numbers.

**Key risks and mitigations:**
- Claude Code changes its run-file format -> parse defensively, version the local record, integration tests on fixture run files, `ck doctor` reports when the format is unrecognised.
- Leak via free text -> allowlist schema; leak check seeded from the project's own identifiers; interactive approval mandatory; tests with planted confidential strings that must never reach output.
- Misattributed roles after label changes -> one label-grammar module with tests against both workflows' label templates; unknown labels counted as `other` and reported, never guessed.
- Transcript pruning loses data -> automatic collect right after each run.
- Work-machine policy -> export-to-file path that needs no `gh` access on that machine.

## Decisions Log

| # | Decision | Choice | Rationale | Date |
|---|----------|--------|-----------|------|
| 1 | Capture approach | Option A: post-hoc collector over Claude Code run files + transcripts; plus expose per-task conflict-resolution count in the spec-implement result | Zero run overhead, backfills past runs, reuses bench.py parsing/pricing | 2026-10-28 |
| 2 | Local full-fidelity store | Machine-local data dir (`$XDG_DATA_HOME/claude-kiro/runs/`), never inside a project | No analytics can be committed to a client repo by accident | 2026-10-28 (default, not contested) |
| 3 | Shareable record contents | Allowlist from Key Decision 3, enforced by the exporter's schema and documented in the repo | Privacy by construction | 2026-10-28 (default, not contested) |
| 4 | Issues found | Deterministic issue codes + agent-drafted ck-level note, leak-checked, and approved/edited by me interactively before export; free text never exported non-interactively | Lets records explain *why* a run was slow without leaking project details | 2026-10-28 |
| 5 | Submission | `ck stats submit` opens a draft PR via `gh`, after showing the diff and a passing leak check; one PR per batch; same flow on personal and work machines | Reviewable in the repo; single workflow everywhere | 2026-10-28 |
| 6 | Project identity | None in shared records; salted pseudonym only in local records (salt never exported) | Repo analysis is about ck, not my projects | 2026-10-28 (default, not contested) |
| 7 | When collection runs | Automatic local collect after `ck run` and at the end of `/spec:implement` and `/spec:create`, plus `ck stats collect --all` for backfill; export/submit manual; a setting disables auto-collect | Beat transcript pruning; nothing leaves the machine automatically | 2026-10-28 (default, not contested) |
| 8 | spec:plan coverage | v1 covers spec-implement and spec-create; spec:plan is a stretch goal | No run id, interactive wall time | 2026-10-28 (default, not contested) |
| 9 | Date precision and target repo | Month (`yyyy-mm`) only; PRs to `kutlayhanli/claude-kiro` only | Less fingerprintable; fork is where ck is developed | 2026-10-28 |
| D10 | Run file deleted, transcripts remain (review V10) | Write a partial record: recover the workflow name from the main session transcript's `Workflow` launch whose result names the run id (`name` such as `spec:spec-implement`, or a `scriptPath` ending in `spec-implement.js`/`spec-create.js`); if allowlisted, build the record from `agent-*.jsonl` and `.meta.json` with `runfile` and `result` in `missing`; otherwise refuse as not found and never print the name (1.15) | Keeps the data of runs whose run file was pruned, without ever reading past or printing a name that is not allowlisted | 2026-10-28 |
| D11 | Approved note in a `submit --yes` batch (review C1.1) | With a terminal, ask (10.11). Without one, leave the note out of the commit, submit the structured records and say the note was withheld (10.5, 8.4) | Free text never leaves without interactive approval, and an unattended submit still delivers the numbers | 2026-10-28 |
| D12 | Line between partial record and unrecognised format (review C1.2) | `workflowProgress` missing or not a list is unrecognised (1.13). Non-dict entries in the list are skipped and listed in `missing`; odd fields in dict entries and a missing or non-dict `result` give a partial record (1.12) | Refuse only when the overall structure is unknown; keep what can be read otherwise | 2026-10-28 |
| D13 | ck version of a local record (review C1.3) | Record both: nullable `ckVersion`, set only when tied to the run and kept from an earlier tied collect on re-collect, and an always-present `collectorVersion`. Per-version reporting uses `ckVersion` only (1.2, S27) | 1.2 always has a version, and backfilled runs are never credited to the installed release | 2026-10-28 |
| D14 | 33-character issue code vs the 32+ generic pattern (review C1.4) | Rename `review_blocking_contract_mismatch` to `review_contract_mismatch` everywhere; keep the 32+ pattern and P-24 strict | Vocabulary never needs an exemption from the generic patterns | 2026-10-28 |
| D15 | Template words in branch, commit and PR text (review C1.5) | Neutral wording unlikely to be a confidential term: branch `ck-stats-batch-<hex>` (replacing `analytics/runs-<token>`), commit/PR text `ck-stats-batch: ...` with `count`/`kinds`/`months`/`machine` keys; keep full scanning of all free text; the export directory in the checkout is unchanged | A path segment such as `analytics` no longer blocks every submit, and nothing is exempt from the scan | 2026-10-28 |
| D16 | Unreadable run file in a `ck run` session (review C2.7) | Print one count-only line, `stats: 1 run file in this session could not be read; retry: ck stats collect --all`, to stdout and the `ck run` log, with no run id, and log `<utc iso> - unrecognised` (3.3, P-23) | Meets 3.3 (log and retry) without naming a run that may not be ck's (2.2) | 2026-10-28 |
| D17 | Digit-only path segments as confidential terms (review C2.8) | Drop purely numeric and purely hex terms (path segments and similar) from the confidential term set; keep scanning every position (9.3) | Such terms identify no project and would otherwise hit versions, months and record ids | 2026-10-28 |
| D18 | ck-vocabulary words as confidential terms (raised after D15/D17) | Drop candidate terms equal to a whole word of `leak.VOCABULARY` (`spec`, `ck`, `stats`, `batch`, ...); keep full scanning of free text for all other terms (9.3) | A folder like Ruby's `spec/` would otherwise match `spec-implement` / `ck-stats-batch-` in every PR text and block all submits; such words identify no project | 2026-10-28 (my default, follows D17's rationale) |
