# Shareable run-analytics schema

Schema id: `ck-run-analytics/1`. This is the complete list of what `ck stats share` can write into a shareable record.
Nothing outside this list is ever exported. Pattern syntax: `*` = a segment whose value comes from a fixed enum,
`[]` = list items. This field list is generated from `claude_kiro.analytics.share.schema_field_paths()`.

## Fields

- `agents_by_role.*`: non-negative integer
- `ck_version`: format: version or other
- `claude_code_version`: format: version or other
- `config.*.effort`: enum: `high`, `inherit`, `low`, `max`, `medium`, `other`, `xhigh`
- `config.*.model`: enum: `fable`, `haiku`, `inherit`, `opus`, `other`, `sonnet`
- `cost.by_model.*`: non-negative number
- `cost.by_role.*`: non-negative number
- `cost.estimated`: boolean
- `cost.pricing_date`: format: yyyy-mm
- `cost.source`: enum: `estimate`, `priced_from_transcripts`
- `cost.usd`: non-negative number
- `counts.agents`: non-negative integer
- `counts.agents_other`: non-negative integer
- `counts.attempts`: non-negative integer
- `counts.bisects`: non-negative integer
- `counts.conflicts`: non-negative integer
- `counts.conflicts_inferred`: non-negative integer
- `counts.consistency_fixed`: non-negative integer
- `counts.consistency_passes`: non-negative integer
- `counts.cross_test_blocking`: non-negative integer
- `counts.cross_tests`: non-negative integer
- `counts.escalations`: non-negative integer
- `counts.fixes`: non-negative integer
- `counts.gates_green`: non-negative integer
- `counts.gates_red`: non-negative integer
- `counts.landings`: non-negative integer
- `counts.lenses_lost`: non-negative integer
- `counts.open_questions`: non-negative integer
- `counts.plan_failures`: non-negative integer
- `counts.plan_steps`: non-negative integer
- `counts.refuted`: non-negative integer
- `counts.retries`: non-negative integer
- `counts.review_escalated`: non-negative integer
- `counts.review_fixed`: non-negative integer
- `counts.review_not_fixed`: non-negative integer
- `counts.review_rounds`: non-negative integer
- `counts.tasks`: non-negative integer
- `counts.tasks_failed`: non-negative integer
- `counts.tasks_merged`: non-negative integer
- `counts.tasks_not_started`: non-negative integer
- `counts.waves`: non-negative integer
- `durations.by_role.*`: non-negative number
- `durations.phases[].minutes`: non-negative number
- `durations.phases[].phase`: enum: `adversarial_review`, `consistency_check`, `design_test_plan`, `final_gate`, `link`, `other`, `plan`, `report`, `requirements`, `revise`, `task_tracks`, `tasks`, `wave`
- `durations.phases[].wave`: non-negative integer
- `durations.task_span_minutes`: non-negative number
- `durations.wall_minutes`: non-negative number
- `id`: format: 16 lowercase hex characters
- `issues[]`: enum: `conflict_not_resolved`, `consistency_conflicts`, `cross_test_blocking`, `escalated`, `escalated_to_opus`, `gate_red_after_merge`, `landing_bisected`, `landing_red`, `permission_refusal`, `plan_cycle`, `planning_failed`, `red_on_landing_fixed`, `report_from_agents`, `resolver_rounds_exceeded`, `retry_no_commits`, `review_blocking_unresolved`, `review_contract_mismatch`, `review_escalated`, `review_lenses_lost`, `review_needs_work`, `run_blocked`, `run_halted`, `transcripts_missing`, `unknown_roles`, `unreviewed_merge`, `worktree_busy`
- `machine`: format: slug of 1-32 lowercase letters, digits, hyphens
- `missing[]`: enum: `agent_entries`, `ck_version`, `meta`, `result`, `runfile`, `timing`, `transcripts`
- `month`: format: yyyy-mm
- `outcome`: enum: `blocked`, `complete`, `failed`, `halted`, `needs_work`, `other`, `ready`, `ready_after_decisions`, `refused`
- `schema`: enum: `ck-run-analytics/1`
- `stack`: enum: `dotnet`, `go`, `java`, `node`, `other`, `python`, `ruby`, `rust`
- `tasks[].agent_minutes`: non-negative number
- `tasks[].attempts`: non-negative integer
- `tasks[].escalations`: non-negative integer
- `tasks[].idle_minutes`: non-negative number
- `tasks[].index`: non-negative integer
- `tasks[].landing_fixes`: non-negative integer
- `tasks[].ok`: boolean
- `tasks[].resolves`: non-negative integer
- `tasks[].review_rounds`: non-negative integer
- `tasks[].risk`: enum: `normal`, `other`, `safety`
- `tasks[].to_merged_minutes`: non-negative number
- `tasks[].track`: enum: `impl`, `other`, `test`, `unknown`
- `tasks[].verdict`: enum: `approve`, `changes`, `other`, `unreviewed`
- `tokens.by_model.*.cacheCreation`: non-negative integer
- `tokens.by_model.*.cacheRead`: non-negative integer
- `tokens.by_model.*.input`: non-negative integer
- `tokens.by_model.*.output`: non-negative integer
- `tokens.by_role.*.cacheCreation`: non-negative integer
- `tokens.by_role.*.cacheRead`: non-negative integer
- `tokens.by_role.*.input`: non-negative integer
- `tokens.by_role.*.output`: non-negative integer
- `variant`: enum: `default`, `iso`, `other`
- `verdicts.*`: non-negative integer
- `workflow`: enum: `other`, `spec-create`, `spec-implement`, `spec-plan`

## Never exported

A shareable record is built field by field and never contains any of these:

- spec name
- task titles
- task summaries
- findings text
- verbatim labels
- file paths
- branch names
- repo URL
- project pseudonym
- session ids
- agent ids
- run ids
- prompts
- emails
- org or account ids
- exact dates or timestamps
- any free text other than an approved issues note

A value outside an enumerated field's fixed set is written as `other`; the original string is never copied.

## Leak check

Before a submission, the whole export is scanned against the confidential terms of the local records, the git
identity and the names of non-ck workflows. Any hit blocks the submission.

- Strong terms (4 or more characters, not a common word, not ck vocabulary) match as case-insensitive substrings.
- Every other term is weak and matches only as a whole word (not inside a longer run of letters or digits).
- Digit-only and hex-only strings shorter than 16 characters are not terms; longer hex strings and ids are.
- A shareable-record JSON file is scanned leaf by leaf. Fixed tokens are skipped: schema field names, enum
  members in enum positions, and the `schema` constant. Format-validated values (`month`, `id`, versions,
  `machine`, `pricing_date`) are skipped for the generic patterns only, never for terms.
- Other text is scanned line by line in full. A hit reports file, field, kind and rule, never the matched text.
- The generic patterns are case-insensitive. Name, kind and regular expression:

  - `email` (email): `[\w.+-]+@[\w-]+\.[\w.-]+`
  - `url` (url): `[a-z][a-z0-9+.-]*://\S+`
  - `scp_remote` (url): `[\w.-]+@[\w.-]+:\S+`
  - `absolute_path` (path): `(?:^|[\s\"'(])(?:/(?:home|Users|root|var|tmp|mnt|opt|srv|etc)/|[A-Za-z]:\\|~/)`
  - `uuid` (id): `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`
  - `run_id` (id): `wf_[A-Za-z0-9_-]{6,}`
  - `timestamp` (timestamp): `\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2})?`
  - `epoch_ms` (timestamp): `\b1[5-9]\d{11}\b`
  - `key_shape` (secret): `(?:sk-|sk-ant-|ghp_|gho_|github_pat_|xox[abp]-|AKIA)[A-Za-z0-9_-]{8,}`
  - `long_run` (secret): `[A-Za-z0-9_\-]{32,}`

## Fixed enums

- `ISSUE_CODES`: `escalated`, `red_on_landing_fixed`, `run_blocked`, `review_lenses_lost`, `transcripts_missing`, `gate_red_after_merge`, `conflict_not_resolved`, `review_blocking_unresolved`, `review_contract_mismatch`, `review_needs_work`, `review_escalated`, `permission_refusal`, `consistency_conflicts`, `cross_test_blocking`, `unknown_roles`, `report_from_agents`, `resolver_rounds_exceeded`, `landing_bisected`, `worktree_busy`, `retry_no_commits`, `run_halted`, `escalated_to_opus`, `planning_failed`, `landing_red`, `plan_cycle`, `unreviewed_merge`
- stack hints: `python`, `node`, `go`, `rust`, `java`, `ruby`, `dotnet`, `other`
- model families: `haiku`, `sonnet`, `opus`, `fable`, `inherit`, `other`
- outcomes: `complete`, `blocked`, `halted`, `refused`, `ready`, `ready_after_decisions`, `needs_work`, `failed`, `other`
- verdicts: `approve`, `changes`, `unreviewed`, `other`
- efforts: `low`, `medium`, `high`, `xhigh`, `max`, `inherit`
- workflows: `spec-implement`, `spec-create`, `spec-plan`, `other`
- variants: `default`, `iso`, `other`
- config roles: `implementer`, `test_writer`, `reviewer`, `reviewer_risky`, `fixer`, `orchestrator`, `resolver`
- roles: `implementer`, `test-writer`, `reviewer`, `fixer`, `resolver`, `plan`, `land`, `gate`, `cross-test`, `report`, `other`
- tracks: `impl`, `test`, `unknown`, `other`
- risks: `normal`, `safety`, `other`
- cost sources: `priced_from_transcripts`, `estimate`
- missing kinds: `result`, `runfile`, `agent_entries`, `transcripts`, `meta`, `timing`, `ck_version`
- phases: `plan`, `wave`, `tasks`, `final_gate`, `report`, `requirements`, `design_test_plan`, `task_tracks`, `link`, `adversarial_review`, `revise`, `consistency_check`, `other`
- count keys: `tasks`, `tasks_merged`, `tasks_failed`, `tasks_not_started`, `waves`, `agents`, `attempts`, `retries`, `escalations`, `review_rounds`, `conflicts`, `conflicts_inferred`, `fixes`, `landings`, `bisects`, `gates_green`, `gates_red`, `cross_tests`, `cross_test_blocking`, `plan_steps`, `plan_failures`, `review_fixed`, `review_not_fixed`, `review_escalated`, `refuted`, `lenses_lost`, `consistency_passes`, `consistency_fixed`, `open_questions`, `agents_other`
- usage keys: `input`, `output`, `cacheRead`, `cacheCreation`
