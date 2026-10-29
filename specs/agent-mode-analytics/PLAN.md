# Plan: Agent-Mode Analytics

**Created:** 2026-10-29
**Status:** Pending Decision

## Problem Statement

`ck stats` only collects runs of Claude Code's Workflow tool. When an organisation disables that tool, `/spec:implement`, `/spec:create`, `/spec:review` and `/spec:spawn-worktree` fall back to the Agent tool (`plugin/resources/without-workflow-tool.md`). That path leaves no workflow run file and no run id, so nothing is collected: `ck stats collect --all` reports "no ck spec runs found … (N other workflow runs skipped)" and `ck stats show` stays empty. Found on ck 0.10.1. Machines where the Workflow tool is blocked (typically work machines) therefore produce no analytics at all.

## What Exists Today (ck 0.10.1)

- **Collector sources:** `iter_run_files` globs only `projects/*/*/workflows/wf_*.json` (`src/claude_kiro/analytics/runfile.py:83-85`); `ALLOWLISTED_WORKFLOWS = {spec-implement, spec-implement-iso, spec-create}` (`runfile.py:9`). `_collect_orphan` (`collect.py:908`) rebuilds a run from agent transcripts but only when the session log shows a Workflow launch for that id. It is the closest template: label from the meta `description`, timing from transcript timestamps, `partial=True`, `missing` lists what is absent.
- **Reusable core:** `_build_agent` (`collect.py:366`), `_aggregate` (`:451`), `_costs` (`:472`), `_build_record` (`:769`, accepts `run_data=None`). Costs are priced from transcripts with the same code as `ck cost`.
- **Agent-tool transcripts already exist:** `projects/<proj>/<session>/subagents/agent-<id>.jsonl` + `agent-<id>.meta.json` with `agentType`, `description`, `toolUseId`, `spawnDepth`, sometimes `model`, `parentAgentId`, `worktreePath`, `worktreeBranch`. No start/end in the meta; timing comes from transcript timestamps.
- **Descriptions are not standardised.** `without-workflow-tool.md` gives a template only for implement tasks (`implement <spec>: task <N>`, :45). Retries, reviews, revisions, fixer, resolver, and every create/review agent have none; real descriptions are free-form (`fix <spec>: task 24 …`, `revise <spec> task 21: …`, `review task 41 … diff`).
- **Agent types:** implement roles use `spec:implementer`, `spec:test-writer`, `spec:fixer`, `spec:resolver`, `spec:reviewer` (vendored projects: `ck-implementer` etc.). Every `/spec:create` and `/spec:review` agent runs as `general-purpose` (`without-workflow-tool.md:12-21`), indistinguishable from other work without a marker.
- **Roles:** `roles.match_label` (`roles.py:157`) parses only workflow labels (`task 3 [test] retry 1 (opus)`, `review 3 round 2`, …); agent-mode descriptions match nothing.
- **Outcome:** the implement fallback already ends with `ck worktree report <spec> --into <into> --json` (`without-workflow-tool.md:60`), the git-based source of truth for merged / failed / blocked.
- **Leak check:** agent `label`s go into the record's `terms` (`collect.py:826-831`), so descriptions are covered; **`worktreePath` / `worktreeBranch` are not**.
- **Shareable schema:** no `source` field; adding an optional enum means a value set in `share.py`, an entry in `_TREE` (not required), setting it in `build_shareable`, adding it to `leak._vocabulary()`, and documenting it in `analytics/SCHEMA.md` (tests check the doc).
- **Auto-collect:** the implement/create/review skills run `ck stats collect --run <wf_id> --auto` at the end (`plugin/skills/implement/SKILL.md:95`, `create/SKILL.md:99`, `review/SKILL.md:35`); agent mode has no `wf_id`. `ck doctor` (`analytics/health.py:61-84`) counts only uncollected workflow runs.

## Research Summary

### Sources Consulted
- [Claude Code issue: OTLP telemetry lacks agent/subagent identity](https://claudeissues.com/issue/27346-feature-otlp-telemetry-lacks-agent-subagent-identity-for-team-spawns) - subagent events carried only the parent's session id; matching by timing was called fragile with parallel agents; the workaround was an explicit marker passed to each agent.
- [OpenTelemetry GenAI agent spans](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans) - `invoke_agent` spans carry `gen_ai.agent.id`/`gen_ai.agent.name`; identity is an explicit attribute, not inferred.
- [Honeycomb: instrumenting AI agents](https://www.honeycomb.io/blog/instrumenting-ai-agents-agent-timeline-opentelemetry-guide) - one conversation/run id stamped on every span of a run groups it.
- [Gravitee: trace context propagation](https://gravitee.io/corpus/gen-607/telemetry/telemetry-correlation-ids-and-distributed-trace-context-propagation-across-apis-event-streams-and-llm-gateways.html) - without a propagated id, async work splits into disconnected pieces; propagate a correlation id explicitly.
- [Improving Web API usage logging (arXiv 2103.10811)](https://arxiv.org/pdf/2103.10811) - time-gap-only sessionisation over-splits.
- [Agentic search in the wild (arXiv 2601.17617)](https://arxiv.org/pdf/2601.17617) - fast parallel agent requests break pure temporal cut-offs.
- [Optimal session interval for transaction logs](https://researchoutput.csu.edu.au/en/publications/determining-the-optimal-session-interval-for-transaction-log-anal/) - the usual 30-minute gap is rarely validated.

### Best Practices Found
- **Propagate an explicit run id** to every child (OTel GenAI, Claude Code issue): the parent creates it, every child carries it.
- **Role from declared type, not free text** (OTel `gen_ai.agent.name`): `agentType` is reliable, descriptions are not.
- **Heuristic grouping only as a labelled fallback** with a preview (sessionisation studies): show what it would group before writing.
- **Outcome from the system of record** (git via `ck worktree report`), as the workflow path already does.

### Patterns to Avoid
- **Timing-based matching of parallel agents** - fragile (Claude Code issue, arXiv 2601.17617).
- **Collecting `general-purpose` agents without a marker** - would sweep unrelated work into ck runs.
- **Storing paths/branches outside `terms`** - `worktreePath`/`worktreeBranch` would escape the leak check.

## Solution Options

### Option A: Run id + manifest + agent-transcript collector (the report's proposal)

**Description:** At launch the orchestrator runs `ck stats begin --kind implement|create|review --spec <name>`, which prints a run id `ag_<8 hex>` and writes `runs-open/<run_id>.json` (kind, start, session id, ck version, `ck agents --json` roles). Every Agent call's description starts with `[ck:<run_id>]` in a fixed per-role format. At the end it runs `ck stats collect --agents <run_id>`; a new `_collect_agent_run` reads the session's `subagents/agent-*.meta.json` whose description carries the id, builds rows with `_build_agent` (role from `agentType`), aggregates and prices as today, and takes the outcome from `ck worktree report`. `ck stats collect --agents-backfill [--dry-run]` recovers older implement-mode runs heuristically.

**Pros:** exact grouping; reuses the collector core; outcome from git; works for create/review (marker makes `general-purpose` agents identifiable); additive to schema `/1`.
**Cons:** the orchestrator must follow the description format (a doc contract, checked by a static test); `begin` adds one command per run.
**Complexity:** Medium
**Precedent:** OTel run/conversation ids; this repo's `_collect_orphan`.

---

### Option B: Rebuild runs from the orchestrator's session transcript

**Description:** No `begin`, no manifest. The collector reads the main session `<sid>.jsonl`: every Agent `tool_use` (description, `subagent_type`) and its `tool_result` (the task agent's JSON report), plus the Bash output of `ck worktree report --json`. Runs are delimited by the `/spec:implement`/`/spec:create` command message.

**Pros:** no orchestrator changes; works for past runs without a marker.
**Cons:** parsing free-form orchestrator transcripts is brittle; several runs in one session (spawn-worktree) are hard to separate; create/review agents still `general-purpose`.
**Complexity:** Medium-High
**Precedent:** `_collect_orphan` reads the session log for the Workflow launch.

---

### Option C: OpenTelemetry

**Description:** Enable Claude Code OTel and aggregate `api_request` events by `agent_id`/`agent.name`.
**Pros:** official per-request cost/duration.
**Cons:** needs a collector per machine; always includes `user.email`/`organization.id`; custom agent names collapse to `custom`; no backfill. Rejected for the same reasons as in run-analytics D1.
**Complexity:** High

## Comparison Matrix

| Criteria | A: run id + collector | B: session transcript | C: OTel |
|---|---|---|---|
| Grouping accuracy | exact (marker) | heuristic | by agent id, no run |
| create/review coverage | yes (marker) | weak | weak |
| Orchestrator changes | doc: begin + description format + collect | none | env setup |
| Backfill of old runs | heuristic, opt-in | yes, heuristic | no |
| Privacy | `terms` + leak check | same | email/org always sent |
| Complexity | Medium | Medium-High | High |

## Complexity Assessment

**Overall: Medium.** Drivers: a second collector source sharing `_build_record`; a description contract that two docs (`without-workflow-tool.md`, `spawn-worktree/SKILL.md`) must follow and a parser that accepts it plus vendored `ck-*` types; schema `/1` additive field touching validator, vocabulary and `SCHEMA.md`; leak terms for `worktreePath`/`worktreeBranch`; backfill heuristics.

## Key Decisions

1. **Approach** - A (recommended), B, or C. Recommendation: **A**, with B's session-transcript reading only where it helps backfill.

2. **Description format** - (a) fixed per-role templates in `without-workflow-tool.md`, e.g. `[ck:<id>] implement <spec>: task <N>`, `… retry <k>`, `[ck:<id>] review <spec>: task <N> round <k>`, `[ck:<id>] fix <spec>: <k>`, `[ck:<id>] resolve <spec>: task <N>`, `[ck:<id>] create <spec>: <step>`; role taken from `agentType` and the verb; or (b) only the `[ck:<id>]` prefix, rest free-form. Recommendation: **(a)**, so retries, review rounds and escalations are countable; a lenient parser keeps unknown suffixes as `other`.

3. **Outcome source** - (a) the orchestrator passes the `ck worktree report --json` output (`--report FILE`); or (b) `ck stats collect --agents` runs `ck worktree report <spec> --into <into>` itself from the manifest's root/target. Recommendation: **(b)**: one fewer orchestrator step, and the same code serves backfill; outcome stays null when the repo or branch is gone.

4. **`general-purpose` create/review agents** - include them only when their description carries a `[ck:<id>]` of a known run. Recommendation: **yes**; without a marker they are never collected.

5. **Backfill scope in v1** - (a) `--agents-backfill` with `--dry-run`, implement-mode only (spec agent types), grouped by session + spec parsed from the description + time gap, `partial: true`; or (b) defer backfill. Recommendation: **(a)**, small and opt-in; create/review backfill is impossible (no marker, `general-purpose`) and stated as a limit.

6. **Open manifests** - a run that never reaches `collect` (crash, session closed) leaves `runs-open/<id>.json`. Recommendation: **`ck doctor` reports open manifests older than 6 h and `ck stats collect --all` collects them as `partial`**.

## Open Questions

- [ ] Run id format: `ag_<8 hex>` (short, distinct from `wf_`) - confirm.
- [ ] Gap threshold for backfill grouping (e.g. 2 h) - tune with `--dry-run` on a machine with old agent-mode runs.
- [ ] Do my hand-run fixer agents (ck-* types without a `[ck:]` marker) belong in backfill? They are not spec runs; the gap/spec grouping may pick them up - `--dry-run` must make this visible.

## Recommended Direction

**Approach:** Option A with decisions 2a, 3b, 4 yes, 5a, 6 as recommended.

**Rationale:**
- Exact grouping and create/review coverage need a propagated id; research and Claude Code's own telemetry gap agree.
- Reuses the collector core and pricing, so workflow and agent-mode numbers are comparable.
- Additive to `ck-run-analytics/1`; existing records and readers unchanged.

**Key risks and mitigations:**
- Orchestrator drifts from the description format -> static test over `without-workflow-tool.md` and `spawn-worktree/SKILL.md` templates; lenient parser counts unknowns as `other` and `ck stats show` reports unmatched labels.
- Paths/branches leak via `worktreePath`/`worktreeBranch` -> add to `terms`; leak tests plant them.
- Backfill sweeps unrelated agents -> spec agent types only, `--dry-run` preview, `partial: true`.
- Transcripts pruned before collect -> collect at the end of every run (best-effort, decision 2 of the user), doctor reports open manifests.

## Decisions Log

| # | Decision | Choice | Rationale | Date |
|---|----------|--------|-----------|------|
| 1 | Shareable schema | Stay on `ck-run-analytics/1`; add optional `source: workflow \| agents` and a `cost.source` value `agent_transcripts` | Additive; existing readers and records unchanged | 2026-10-29 |
| 2 | `ck stats begin` | Best-effort; never blocks a run; backfill is the safety net | Analytics must never cost a run | 2026-10-29 |
| 3 | Grouping | By the run id in Agent descriptions; session + spec + time-gap heuristic only for backfill, shown with `--dry-run` | Exact for new runs; heuristic is labelled and previewed | 2026-10-29 |
