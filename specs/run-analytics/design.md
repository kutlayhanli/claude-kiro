# Design: Run Analytics

## Overview

Run analytics adds a `ck stats` command group to the existing click CLI (`src/claude_kiro/cli/main.py`)
and a new `claude_kiro.analytics` package that sits next to `bench.py` and reuses its transcript and
pricing code. Nothing changes in how runs behave, cost or schedule work. The only workflow change is one
new per-task result field.

How the PLAN.md decisions shape it:

- **D1 (post-hoc + `resolves`; D10 for a deleted run file).** The collector reads Claude Code's run file
  (`<home>/projects/<proj>/<sid>/workflows/<run_id>.json`) and the agent transcripts
  (`<sid>/subagents/workflows/<run_id>/agent-<id>.jsonl` + `.meta.json`) after the run has finished. It
  parses them with `bench.scan_transcript` and prices them with `bench.price_usage` / `PRICING` /
  `PRICING_DATE`, the same code behind `ck cost` and `ck bench`. `spec-implement.js` returns a new
  per-task `resolves` count (Story 11).
- **D2 (machine-local store).** Local records go to `$CK_DATA_HOME`, else `$XDG_DATA_HOME/claude-kiro`,
  else `~/.local/share/claude-kiro`. They never go in a project.
- **D3 (allowlist).** A shareable record is *built* field by field from a fixed schema
  (`analytics/share.py: SHAREABLE_SCHEMA`), with enum mapping and a validator. Nothing is copied and then
  scrubbed. The schema is published in `analytics/SCHEMA.md` at the repository root, and a check keeps the
  two in step.
- **D4 (issues).** Issue codes come from a pure function of the local record. The free-text note is
  drafted by a `claude -p` subprocess, leak-checked, then edited, accepted or discarded in the terminal.
  It is never included without interactive approval. The drafter is the only network use outside submit,
  and it starts only when I ask for a note in an interactive export (7.11, 7.12).
- **D5 / D9 (draft PR, one target).** `ck stats submit` exports, shows the diff, runs the leak check,
  asks for confirmation, then fetches `master` from the remote that points at `kutlayhanli/claude-kiro`,
  makes a branch from it, commits, pushes to that remote and runs `gh pr create --draft --repo
  kutlayhanli/claude-kiro --base master --head <branch>`. Dates are `yyyy-mm` only.
- **D6 (pseudonym local only).** The local record holds an HMAC-SHA256 of the normalised git remote,
  keyed with a salt stored as a non-role key in the global ck config file (`agents.global_config_path()`,
  mode 0600) (1.8). The shareable builder has no input for the pseudonym.
- **D7 (auto-collect local, manual export).** `ck run` collects in its outer process after `claude`
  exits. The implement, create and review skills run `ck stats collect --run <id> --auto` as their last
  reporting step (3.2).
  `ck agents set run.analytics false` turns this off.
- **D8 (spec:plan stretch).** v1 has no spec:plan collector. `WorkflowKind` reserves `spec-plan` in the
  shareable enum so a later version can add it without a schema bump. Until then the collector never
  produces it.

Every allow / refuse / hold rule is a pure function in the module that owns it (see Decision Seams). The
CLI layer (`cli/stats.py`) does I/O and calls those functions. It never re-implements a rule.

## Components

### Existing Components to Modify

- `src/claude_kiro/bench.py`: extract the per-model usage summing that `session_spend` does inline into
  `aggregate_usage(scans: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, int]]`, and have
  `session_spend` call it. The collector prices a run's total through the same function and
  `_price_by_model`, so `ck stats` and `ck cost --run` agree to the cent (NFR "Agreement"). There is no
  behaviour change for `ck cost` or `ck bench`. `claude_home()`, `scan_transcript`, `collect_agents`,
  `price_usage`, `normalize_model`, `PRICING_DATE` and `REFUSAL_RE` are reused as they are.
- `src/claude_kiro/agents.py`: add `"analytics": True` and `"machine_label": None` to `DEFAULTS["run"]`
  and both keys to `ROLE_KEYS["run"]`. Coerce `analytics` in `_coerce` with the other booleans (`ask`,
  `enabled`, ...). The boolean error message becomes `"{role}.{key} must be true or false (also yes/no,
  on/off)"`, which names the accepted values (3.6). Coerce `machine_label` through S13
  (`valid_machine_label`), handled before `_coerce`'s generic case-insensitive `null`/`none`/`default` -> None
  branch: exactly `none` or `default` unsets it, and every other value, including `null`, `NONE` or `Default`,
  goes through S13 (so `null` is stored as a slug and upper-case forms are rejected) (7.6, P-8). `write_setting` already validates before it
  writes, so a rejected value leaves the file unchanged. Add the salt helpers `read_salt()` /
  `ensure_salt() -> bytes`: the salt is a top-level non-role key (`"stats_salt"`, hex of 32 random bytes)
  in `global_config_path()`. It is created once with `secrets.token_bytes(32)`, every other key is
  preserved, and the file is written through a temp file at 0600 plus `os.replace`, with an explicit
  `os.chmod(path, 0o600)` so an existing 0644 `config.json` is tightened too (1.8, NFR Local record
  protection). `write_setting` keeps an existing 0600 mode when it rewrites the file. The salt key is never
  shown by `ck agents` or `ck agents --json`. Driven by 1.8, 3.4-3.6 and 7.6.
- `src/claude_kiro/cli/agents.py`: `shown()` prints `run.analytics` as `on`/`off` and
  `run.machine_label` (or `(none)`). Display only.
- `src/claude_kiro/cli/run.py`: (a) add `CK_RUN_SANDBOXED=1` to the child environment when the run is
  sandboxed. `collect` uses it to tell "sandbox hides the Claude home" (1.14) from "run not found" (1.10).
  (b) After `subprocess.call` and `spend_report`, call the new `auto_collect_after_run(cwd, session_id,
  started, header)`. It resolves `run.analytics`, finds the session (`--session-id`, else
  `bench.latest_session(home, cwd, since=started - 5)`), collects every allowlisted run the session
  started, and writes one log line per run. It is wrapped like `spend_report`: any exception becomes a
  log line plus the manual retry command, and `sys.exit(code)` is unchanged (3.1, 3.3, 3.4). (c) In
  `sandbox_settings`, add the resolved `store.data_dir()` to the `credentials.files` deny list, the same
  mechanism `CREDENTIAL_PATHS` uses, so sandboxed agents cannot read any project's local records (NFR Local
  record protection, decision 2). The salt lives in the global config file under `~/.config/claude-kiro`,
  which stays readable for `ck` itself; `collect --auto` inside the sandbox defers to the outer process,
  so nothing in the session needs the data dir.
- `src/claude_kiro/cli/main.py`: `cli.add_command(stats)`. `doctor()` calls
  `analytics.health.doctor_findings(...)` and routes the results into its existing `warnings` list and
  `ℹ️` lines (Story 12).
- `plugin/workflows/spec-implement.js`: in `landTask` (`:604`) set `rec.resolves = resolves` each time it
  is incremented. In `runTask` (`:646`) initialise `rec.resolves = 0`. In the `taskReport` map (`:830`)
  add `resolves: r.resolves || 0`. The records built in `launch`/`applyLint` (busy, verify-cycle) get
  `resolves: 0` through the `|| 0`. No other field, log line or control flow changes (11.1, 11.2).
- `.claude/workflows/spec-implement.js`: regenerated from the plugin copy with `ck vendor` in this repo.
  Both copies must stay byte-identical (11.2).
- `tests/js/spec_implement.test.mjs`: existing harness. It gets cases for `resolves` (the test plan owns
  them). Listed here only because the label-emission helper below plugs into it.
- `plugin/skills/implement/SKILL.md` step 9 (`:79-89`): add a final bullet after the spend bullet:
  "run `ck stats collect --run <the run's wf_ id> --auto` outside the sandbox and quote the line it
  prints (where the record went, or why it was skipped). Never let its result change what you report
  about the run." (3.2, 3.3, 3.4)
- `plugin/skills/create/SKILL.md` "Close the loop" (`:80-98`): the same step as item 2's last line, before
  open questions (3.2).
- `plugin/skills/review/SKILL.md` step 4 (the report step): the same step as its last reporting line,
  before the open questions. `/spec:review` runs spec-create in review mode, so its runs are allowlisted
  and collected like the others (3.2).
- `.claude/commands/spec/implement.md`, `.claude/commands/spec/create.md`,
  `.claude/commands/spec/review.md`: vendored copies, regenerated with `ck vendor`.

### New Components

- `src/claude_kiro/analytics/__init__.py` (new): package marker. Exports `SCHEMA_LOCAL`,
  `SCHEMA_SHARED` and `ALLOWLISTED_WORKFLOWS`.
- `src/claude_kiro/analytics/store.py` (new): local data store paths, atomic 0600 writes, the 0700
  directory, the export/submit ledger and the collect log. It reads the salt through
  `agents.ensure_salt()` and the machine label as the resolved `run.machine_label`; it keeps no settings
  file of its own. Covers 1.1, 1.8, 1.9, 7.8, 10.3 and the NFR on local record protection.
- `src/claude_kiro/analytics/runfile.py` (new): locate run files, read a workflow name without
  processing anything else, recognise the format, decide finished/unfinished, classify access to the
  Claude home, and, for a run whose run file is gone, find its agent directory and recover its workflow
  name from the session transcript's Workflow launch (S28). Covers 1.10, 1.11, 1.13, 1.14, 1.15 and 2.1-2.3.
- `src/claude_kiro/analytics/roles.py` (new): the label grammar for both workflows, current and legacy
  templates. It maps labels to role, task, attempt/round and step. Covers 5.1-5.3.
- `src/claude_kiro/analytics/collect.py` (new): build a `LocalRecord` from a run file, its transcripts
  and its result, including per-agent/model/role/phase/task tokens and cost, spec-implement and
  spec-create metrics, the pseudonym and the stack hint, and the transcripts-only partial record of 1.15.
  Covers 1.2-1.9, 1.12, 1.15 and 4.1-4.4.
- `src/claude_kiro/analytics/show.py` (new): breakdowns, trends, filters and duration coverage notes,
  with text and JSON renderers. Covers 6.1-6.8.
- `src/claude_kiro/analytics/share.py` (new): `SHAREABLE_SCHEMA`, enum mapping, model aliasing,
  machine-label validation, the shareable record builder and the validator. Covers 7.2-7.7.
- `src/claude_kiro/analytics/issues.py` (new): `ISSUE_CODES`, deterministic derivation, the note
  drafter subprocess and the note inclusion decision. Covers 8.1-8.5.
- `src/claude_kiro/analytics/leak.py` (new): build the confidential term set, the generic patterns, the
  matching rule and the scan. Covers 9.1-9.4.
- `src/claude_kiro/analytics/export.py` (new): pick the records to export, build them in memory, run the
  leak check, then write `analytics/runs/<yyyy-mm>/<record_id>.json` (+ `.md` note). Covers 7.1 and
  7.8-7.11.
- `src/claude_kiro/analytics/submit.py` (new): target-repo check, preflight, batch selection, branch
  name / commit message / PR text from allowlisted fields, confirmation decision, the git and `gh` steps
  and rollback. Covers 10.1-10.9.
- `src/claude_kiro/analytics/health.py` (new): the `ck doctor` findings. Covers 12.1-12.3.
- `src/claude_kiro/cli/stats.py` (new): the `ck stats` click group (`show`, `collect`, `export`,
  `submit`, and `label`, an alias of `ck agents set run.machine_label`). Thin: it parses options, does I/O, calls the modules above, and echoes with the
  CLI's `✓ / ⚠ / ❌ / ℹ️` style.
- `analytics/SCHEMA.md` (new, repository root of claude-kiro): the published allowlist. Every field path
  of `ck-run-analytics/1` with its type and enum, every excluded category, the issue-code set, the stack
  enum and the leak-check matching rule (7.7, 9.3). It is shipped in this repository, which is the PR
  target.
- `tests/js/emit_labels.mjs` (new): runs both workflow scripts through the existing `load_workflow.mjs`
  harness in a fixed set of scenarios (retry, escalation, review rounds, conflict rounds, landing fix,
  full/final gate, cross-test, plan retry, all spec-create phases) and prints every `opts.label` as JSON.
  The automated grammar check (5.3) feeds this output to `roles.parse_label`. It is listed as a component
  because it is the contract between the scripts and the grammar. The same check also feeds a pinned
  legacy corpus, `tests/fixtures/legacy_labels.json`, built from the label templates of the
  pre-batch-landing script (`git show f03d598^:src/claude_kiro/resources/templates/workflows/spec_implement.js`:
  `merge N`, `resolve N`, `plan K`, ...) plus path-shaped task ids, so historical runs keep parsing (5.3).

## Public Interfaces

### CLI

All commands follow existing conventions. Success lines start with `✓`, warnings with `⚠`, errors with
`❌`, information with `ℹ️`. Usage and validation errors exit 2, operational failures exit 1. `-C DIR`
works as for every other command.

```
ck stats [show] [RUN_ID] [--kind spec-implement|spec-create] [--since YYYY-MM[-DD]] [--until YYYY-MM[-DD]]
                         [--project PSEUDONYM] [--json]
ck stats collect (--run RUN_ID | --all) [--auto] [--json]
ck stats export [--to CHECKOUT] [--re-export RUN_ID ...] [--note] [--yes]     # --reexport / --notes accepted as aliases
ck stats submit [--to CHECKOUT] [--note] [--yes]
ck stats label [SLUG | --clear]                                                # alias of ck agents set run.machine_label
```

- `ck stats` with no subcommand runs `show` with no arguments (the group has `invoke_without_command=True`).
- `show` with no `RUN_ID` lists runs newest first (6.1). When at least two runs of a kind pass the
  filters, it then prints a trends block (6.3). `show RUN_ID` prints the run breakdown (6.2). `--json`
  prints `ShowList` / `ShowRun` (6.4). With no records it exits 0 and suggests `ck stats collect --all`
  (6.5). An unknown `RUN_ID` exits 1 (6.6).
- `collect --run RUN_ID`: exit 0 when a record is written (`✓ <run_id>: wrote <path>`) or when the run is
  unfinished (`ℹ️ <run_id> has not finished; nothing written`). Exit 1 for not found (names the Claude
  home searched), unrecognised format (including a run file that is not valid JSON), not allowlisted
  (message without the name), or sandbox-blocked. The id printed is the one I typed (2.2, 2.3). When the
  run file is gone but `subagents/workflows/<run_id>/` remains, collect takes the 1.15 path: S28 recovers
  the workflow name from the session transcript; an allowlisted name gives a partial record (`✓`, exit 0,
  `missing` holds `runfile` and `result`), anything else is reported exactly like not found (1.10), and an
  unknown name is never printed.
  Exit 2 when `RUN_ID` does not match `^wf_[A-Za-z0-9_-]{1,128}$`, which keeps glob characters out of
  path lookups.
- `collect --all`: prints the `CollectSummary` counts (4.2). Exit 1 iff `failed > 0` (4.3). With no
  allowlisted runs it says so and exits 0 (4.4). A run file whose workflow name cannot be read (not valid
  JSON, or no string `workflowName`) is counted in `skipped_non_ck` and never named (1.13, 2.2). A run file
  with an allowlisted name that fails S2 is `failed` with its id and the problem. Runs are collected concurrently (thread pool of 8) and
  reported in input order.
- `collect --auto` (used by `ck run` and the skills): the same as `--run`, plus three changes. It first
  checks `run.analytics` and prints `ℹ️ auto-collect is off (ck agents set run.analytics true)` and exits
  0 when it is off. It always exits 0. On failure it appends to the collect log and prints
  `⚠ could not collect <run_id>: <reason>; retry: ck stats collect --run <run_id>`. Inside a `ck run`
  sandbox (`CK_RUN_SANDBOXED=1` and the home unreadable) it prints that `ck run` collects the run after
  the session exits.
- `export`: `--to` defaults to the current directory. Exports every local record not yet exported, every
  record exported to this checkout but unsubmitted whose files are missing from it (10.10, new record id),
  plus each `--re-export RUN_ID` (7.1, 7.8). A re-export of a record whose earlier export to this checkout
  is unsubmitted first removes those earlier files (this machine's own pending files), so the batch never
  holds two records for one run (7.8, 10.10). `--note` asks for an issues note per exported record (8.2).
  `--yes`, or no TTY on stdin/stdout, means non-interactive (8.4); then no drafter subprocess starts
  (7.12).
- `submit`: as `export`, then the submit flow (10.1-10.11). `--yes` skips confirmation only when the batch
  has no free text (10.5). A batch holding an approved `.md` note from an earlier export always asks at a
  terminal, even with `--yes` (10.11). With no TTY and `--yes`, the notes are left out of the commit (they
  stay untracked in the checkout), the rest is submitted, and the note is reported as withheld (10.5,
  10.11, 8.4). With no TTY and no `--yes`, submit refuses (10.1); that refusal is decided before the export
  step runs, so it writes, restores and removes nothing in the checkout.
- `label SLUG` is an alias of `ck agents set run.machine_label SLUG`: it stores the label through
  `agents.write_setting` after S13 (7.6). `--clear` is `ck agents set run.machine_label none`. With no
  argument it prints the current label, or `(none)`.

`ck agents set run.analytics true|false` turns automatic collection on or off (3.4-3.6).
`ck agents set run.machine_label <slug>|none` sets or clears the machine label (7.6). Both keys appear in
`ck agents --json` under `run`.

### Python entry points (called by the CLI, `ck run`, `ck doctor` and tests)

```python
# analytics/store.py
def data_dir(env: Optional[Mapping[str, str]] = None) -> Path            # CK_DATA_HOME > XDG_DATA_HOME/claude-kiro > ~/.local/share/claude-kiro
def runs_dir(env=None) -> Path                                          # data_dir()/runs
def record_path(run_id: str, env=None) -> Path                          # runs_dir()/<run_id>.json
def write_private_json(path: Path, obj: Any) -> None                    # tmp file 0600 + os.replace; parent mkdir 0700 when created
def load_records(env=None) -> List[LocalRecord]                         # tolerant of older schema versions; unreadable files skipped and counted
def load_ledger(env=None) -> Ledger
def save_ledger(ledger: Ledger, env=None) -> None
def append_collect_log(line: str, env=None) -> None                     # data_dir()/collect.log, 0600
def ensure_salt() -> bytes                                              # = agents.ensure_salt(): "stats_salt" in global_config_path(), 0600
def machine_label() -> Optional[str]                                    # resolved run.machine_label

# agents.py (existing module, new helpers)
def valid_machine_label(value: object) -> bool                          # S13
def read_salt() -> Optional[bytes]
def ensure_salt() -> bytes                                              # creates once; temp file 0600 + os.replace + os.chmod 0600

# analytics/runfile.py
ALLOWLISTED_WORKFLOWS: FrozenSet[str] = frozenset({"spec-implement", "spec-implement-iso", "spec-create"})
def find_run_file(home: Path, run_id: str) -> Optional[Path]            # projects/*/*/workflows/<run_id>.json
def iter_run_files(home: Path) -> Iterator[Path]                         # projects/*/*/workflows/wf_*.json, sorted
def peek_workflow_name(path: Path) -> Optional[str]                     # parses the file, returns only workflowName (any string; None when
                                                                        # unreadable); nothing else is kept. Callers other than
                                                                        # leak.non_ck_workflow_names discard a non-allowlisted name at once
def session_run_ids(home: Path, session_id: str, since_ms: Optional[int]) -> Tuple[List[str], int]
                                                                        # (allowlisted runs of one session, count of its run files whose
                                                                        # workflow name cannot be read); used by ck run, never names those
def find_orphan_run(home: Path, run_id: str) -> Optional[OrphanRun]     # run file absent, projects/<proj>/<sid>/subagents/workflows/<run_id>/
                                                                        # present: (agent dir, session transcript <proj>/<sid>.jsonl or None)
def workflow_launches(session_jsonl: Path) -> List[Tuple[dict, str]]    # each Workflow tool_use input paired with its tool_result text
                                                                        # (joined by tool_use_id); in memory only, [] if unreadable
def recover_workflow_name(launches: Sequence[Tuple[Mapping, str]], run_id: str) -> Optional[str]   # S28

# analytics/collect.py
def collect_run(home: Path, run_id: str, *, now: datetime, pricing: Optional[dict] = None,
                fresh: bool = False) -> CollectOutcome                  # fresh: True for --auto (ck run, skills); S27 input;
                                                                        # no run file + find_orphan_run -> the 1.15 path
def collect_all(home: Path, *, now: datetime, workers: int = 8) -> CollectSummary
def write_outcome(outcome: CollectOutcome, env=None) -> Optional[Path]  # replace-by-run-id (1.9); returns path or None
def derive_outcome(result: object) -> Tuple[Optional[str], bool]        # S26: (outcome, inferred)
def tie_ck_version(manifest_version: Optional[str], manifest_mtime_ms: Optional[int], installed: str,
                   run_start_ms: Optional[int], fresh: bool,
                   previous: Optional[str] = None) -> Optional[str]     # S27; previous = ckVersion of the record being replaced

# analytics/show.py
def list_runs(records: Sequence[LocalRecord], filters: ShowFilters) -> ShowList
def run_breakdown(record: LocalRecord) -> ShowRun
def trends(records: Sequence[LocalRecord], filters: ShowFilters) -> List[Trend]
def render_list(view: ShowList) -> List[str]
def render_run(view: ShowRun) -> List[str]

# analytics/share.py
def build_shareable(local: LocalRecord, *, record_id: str, machine_label: Optional[str],
                    issue_codes: Sequence[str]) -> ShareableRecord      # ck_version comes from the local record (S27), never from the installed ck
def validate_shareable(record: Mapping[str, Any]) -> List[str]           # [] when every path/type/enum is in SHAREABLE_SCHEMA
def schema_field_paths() -> List[str]                                   # dotted paths, for SCHEMA.md and the 7.7 check
def new_record_id() -> str                                              # secrets.token_hex(8): 16 lowercase hex chars

# analytics/issues.py
def derive_issue_codes(local: LocalRecord) -> Tuple[str, ...]
def draft_note(local: LocalRecord, codes: Sequence[str], argv: Optional[List[str]] = None, timeout: float = 120) -> NoteDraft
DRAFTER_ARGV = ["claude", "-p", "--model", "sonnet", "--max-turns", "1", "--output-format", "text"]   # prompt on stdin; $CK_STATS_DRAFTER (shlex) overrides

# analytics/leak.py
def build_terms(records: Sequence[LocalRecord], identity: GitIdentity, non_ck_names: Sequence[str]) -> TermSet
def scan(files: Mapping[str, str], terms: TermSet) -> List[LeakHit]     # files: relative path or pseudo-path ("<branch>", "<pr-body>") -> text;
                                                                        # a .json file that parses as a ck-run-analytics record is scanned leaf
                                                                        # by leaf with SHAREABLE_SCHEMA classification; anything else is free text
def non_ck_workflow_names(home: Path) -> List[str]                       # in memory only, never written; [] if unreadable

# analytics/export.py
def plan_export(records, ledger, reexport: Sequence[str], checkout_id: str,
                present: FrozenSet[str]) -> ExportPlan                  # present: ledger file paths that exist in the checkout
def build_batch(plan: ExportPlan, *, machine_label, notes: Mapping[str, str]) -> List[PendingFile]
def write_batch(checkout: Path, files: Sequence[PendingFile]) -> None    # only after a clean leak check

# analytics/submit.py
TARGET_REPO = "kutlayhanli/claude-kiro"
def remote_urls(checkout: Path) -> List[Tuple[str, str]]                # (remote name, configured url) from `git config --get-regexp`
                                                                        # over remote.*.url: the raw configured URL, so an insteadOf
                                                                        # rewrite (used by the tests' bare remote) never hides it
def branch_name(token: str) -> str                                       # "ck-stats-batch-<token>", token = secrets.token_hex(4)
def pr_text(records: Sequence[dict], machine_label: Optional[str],
            token: Optional[str] = None) -> dict                        # {branch, commit, title, body}; token None -> a fresh token
def commit_message(summary: BatchSummary) -> str
def pr_title(summary: BatchSummary) -> str
def pr_body(summary: BatchSummary) -> str
def submit_batch(checkout: Path, remote: str, files: Sequence[PendingFile], summary: BatchSummary,
                 runner: Runner) -> SubmitResult                        # remote: the name S20 returned

# analytics/health.py
def doctor_findings(home: Path, auto_collect: bool, records: Sequence[LocalRecord]) -> List[DoctorFinding]

# cli/run.py
def auto_collect_after_run(cwd: Path, session_id: Optional[str], started: float, log: TextIO) -> List[str]  # never raises
```

### File formats

- Local record: `<data_dir>/runs/<run_id>.json`, schema `ck-run-local/1` (`LocalRecord` below), mode 0600.
- Ledger: `<data_dir>/state.json` (`Ledger`), mode 0600.
- Collect log: `<data_dir>/collect.log`, one line per auto-collect failure:
  `<utc iso> <run_id> <error_kind>: <message>`. Messages never hold a non-ck workflow name (2.2). A run that is not
  known to be allowlisted (`not_allowlisted`, or `unrecognised` with an unreadable name) is logged as
  `<utc iso> - <error_kind>` without its id, because 2.2's only exception is echoing a typed id back to the console.
- Salt and machine label: no file of their own. The salt is the top-level key `stats_salt` in
  `agents.global_config_path()` (`config.json`, mode 0600, tightened with `os.chmod` when it already
  exists). The machine label is the `run.machine_label` agent setting. The salt never leaves that file.
- Shareable record: `<checkout>/analytics/runs/<yyyy-mm>/<record_id>.json`, schema `ck-run-analytics/1`
  (`ShareableRecord`). JSON with `indent=2, sort_keys=True` and a trailing newline. It is byte-deterministic
  apart from `record_id` (NFR "Determinism").
- Issues note: `<checkout>/analytics/runs/<yyyy-mm>/<id>.md`, plain Markdown, only when approved, next to
  its record. The record's `issues` stays a list of codes (Harness Contract); a note is known by its file.
- Schema document: `analytics/SCHEMA.md`. Each field is one table row whose first cell is the dotted path
  in backticks. The 7.7 check parses those cells.

### Decision Seams

Each seam is a pure function: no I/O, no clock, no network, no environment reads. All inputs are
arguments. Callers never re-implement the rule.

| # | Seam (module) | Inputs -> Output | Decides | Owner |
|---|---|---|---|---|
| S1 | `runfile.is_allowlisted(name: object) -> bool` | any JSON value -> True only for an exact string in `ALLOWLISTED_WORKFLOWS` | whether anything past the name is read (2.1), written (2.2), or refused for `--run` (2.3) | `analytics/runfile.py` |
| S2 | `runfile.check_format(data: object) -> FormatCheck` | parsed run file -> `FormatCheck(ok, problems: Tuple[str, ...])` | recognised format: a dict with `workflowName: str`, `status: str`, `startTime: int\|str` and `workflowProgress: list`. A missing or non-list `workflowProgress` is unrecognised (1.13). Entries inside the list are not checked here: the collector skips a non-dict entry and lists it in `missing`, and odd fields inside a dict entry or a missing or non-dict `result` give a partial record (1.12). Extra keys allowed, missing optional keys allowed (1.13, 12.1) | `analytics/runfile.py` |
| S3 | `runfile.run_state(data: Mapping) -> RunState` | recognised run file -> `"finished" \| "unfinished"` | unfinished when `status` is in `{"running", "pending", "queued", "starting"}`, or when `status` is not in the terminal set `{"completed", "failed", "killed", "cancelled", "error", "stopped"}` and `durationMs` is absent (1.11) | `analytics/runfile.py` |
| S4 | `runfile.classify_home(exists: bool, listable: bool, sandbox_flag: bool) -> HomeAccess` | -> `"ok" \| "sandboxed" \| "missing"` | `sandboxed` when not listable, or when missing while `sandbox_flag` is set. Otherwise `missing` or `ok`. Picks between 1.14 and 1.10 | `analytics/runfile.py` |
| S5 | `roles.parse_label(label: object, workflow: str) -> LabelMatch` | label + workflow kind -> role/task/attempt/round/step/escalated_to/matched | role attribution. No match gives `role="other", matched=False`, never a guess (5.1, 5.2) | `analytics/roles.py` |
| S6 | `collect.conflict_count(result_task: Mapping, resolver_labels: Sequence[LabelMatch]) -> Tuple[Optional[int], bool]` | -> `(count, inferred)` | uses `resolves` when the result has it (`inferred=False`). Otherwise counts resolver matches for that task (`inferred=True`) (1.7) | `analytics/collect.py` |
| S7 | `collect.cost_label(transcript_agents: int, estimated_agents: int, unpriced_agents: int) -> CostSource` | -> `"priced_from_transcripts" \| "estimate"` | the run cost's provenance label (the Harness Contract spelling). Any agent without a transcript makes the run figure `estimate` (1.4, 1.12). The run figure is never `claude_total`: Claude Code's `budget_usd` total is session-wide (every `ck run` session has one, from `--max-budget-usd`), so it is stored as a separate figure with source `claude_total` and scope `session` and never enters the run cost or trend math (1.4, NFR Agreement) | `analytics/collect.py` |
| S8 | `collect.summarize_outcomes(outcomes: Sequence[OutcomeKind], skipped_non_ck: int) -> Tuple[CollectCounts, int]` | -> counts + exit code | exit 1 iff any `failed`; "no allowlisted runs" when all counts are zero (4.2-4.4) | `analytics/collect.py` |
| S9 | `agents._coerce("run", "analytics", value)` (existing function, new key) | str/bool -> bool or `AgentConfigError` | accepts only true/false/yes/no/on/off and booleans (3.6) | `agents.py` |
| S10 | `collect.auto_collect_enabled(run_role: Mapping) -> bool` | resolved `run` role -> bool | `run_role.get("analytics", True) is not False`, so unset means on (3.4, 3.5) | `analytics/collect.py` |
| S11 | `share.to_enum(value: object, allowed: FrozenSet[str]) -> str` | -> the value if it is an allowed member, else `"other"` | never copies an unknown string (7.4) | `analytics/share.py` |
| S12 | `share.model_alias(model: object) -> str` | -> `"haiku" \| "sonnet" \| "opus" \| "fable" \| "inherit" \| "other"` | a string, lower-cased, that matches the anchored `^(?:claude-)?(haiku\|sonnet\|opus\|fable)(?:-[0-9][0-9a-z-]*)?(?:\[1m\])?$` -> that family; `"inherit"`/`"inherited"` -> `inherit`; anything else -> `other`. It does not call `agents.model_family`, whose substring match would turn provider ids into a family. Provider forms such as `us.anthropic.claude-opus-5-5-v1:0`, `arn:aws:bedrock:...`, `claude-opus-5-5@20261001`, `anthropic/claude-opus-5-5` and custom ids (`my-finetune`) are `other` (7.5) | `analytics/share.py` |
| S13 | `agents.valid_machine_label(value: object) -> bool` (alias `share.validate_machine_label` returning `LabelDecision(ok, value, error)`) | -> accept or reject | a string matching `^[a-z0-9-]{1,32}$` (1-32 lowercase letters, digits or hyphens; leading, trailing and repeated hyphens allowed). `none`/`default` are handled before S13 as "clear" (7.6) | `agents.py` |
| S14 | `share.build_shareable(...)` + `share.validate_shareable(record) -> List[str]` | local record (minus pseudonym, which it never reads) -> record; record -> violations | only `SHAREABLE_SCHEMA` paths, types, enums and formats (`month` `^\d{4}-(0[1-9]\|1[0-2])$`, `record_id` `^[0-9a-f]{16}$`, versions `^\d+\.\d+\.\d+([.+-][0-9A-Za-z.]+)?$` else `"other"`) (7.2, 7.3, 7.7) | `analytics/share.py` |
| S15 | `issues.derive_issue_codes(local: LocalRecord) -> Tuple[str, ...]` | -> a sorted, de-duplicated subset of `ISSUE_CODES` | deterministic issue codes (8.1) | `analytics/issues.py` |
| S16 | `issues.decide_note(has_note: bool, interactive: bool, yes: bool, user_choice: Optional[str], final_hits: int) -> NoteDecision` | -> `"include" \| "withhold_noninteractive" \| "discarded" \| "blocked_by_leak" \| "none"` | include only when interactive, the user chose `accept`, and the final leak check found 0 hits. `yes=True` counts as non-interactive for the note (8.3-8.5, 10.5) | `analytics/issues.py` |
| S17 | `leak.build_terms(...) -> TermSet` and `leak.scan(files, terms) -> List[LeakHit]` | local records + git identity + non-ck names; text files -> hits | the confidential term set, strong vs weak terms, generic patterns, and the matching rule (9.1, 9.3) | `analytics/leak.py` |
| S18 | `leak.decide(hits: Sequence[LeakHit]) -> Gate` | -> `Gate(allowed: bool, hits)` | any hit blocks (9.2) | `analytics/leak.py` |
| S19 | `export.plan_export(records, ledger, reexport, checkout_id, present) -> ExportPlan` | -> run ids to export, re-exports, `restore` (exported to this checkout, unsubmitted, files missing from `present`), and `nothing_new` | not-yet-exported, plus `restore` ids (re-exported with a new record id, 10.10), plus explicit re-exports only (7.1, 7.8, 7.10). A run is restored when any of its ledger files is missing; its earlier files that are still present go into `replace`, so a restore never leaves two records (or an orphan `.md`) for one run. Submitted records are never restored: their files live on the PR branch, not in the checkout's branch | `analytics/export.py` |
| S20 | `submit.target_remote(remotes: Sequence[Tuple[str, str]]) -> Optional[str]` (and `is_target_repo(url: str) -> bool` for one URL) | (name, configured url) pairs -> the name of the first remote whose URL normalises to `github.com/kutlayhanli/claude-kiro` (https, ssh, `git@`, optional `.git`, case-insensitive owner/name), else None | the only allowed target and the remote that submit fetches from and pushes to (7.9, 10.9) | `analytics/submit.py` |
| S21 | `submit.decide_preflight(porcelain: Sequence[str], own_pending: FrozenSet[str], tracked_destinations: FrozenSet[str], target_ok: bool) -> Preflight` | `git status --porcelain=v1` lines, paths of this machine's exported-but-unsubmitted files and of notes withheld by an earlier `skip_withhold_notes` submit (`Ledger.submitted[].withheld`), destination paths already tracked at HEAD -> `Preflight(ok, reason)` | refuse on a wrong repo, on any change outside `own_pending`, or on any destination that already exists in the tree other than an `own_pending` file (10.7, 10.9) | `analytics/submit.py` |
| S22 | `submit.select_batch(ledger: Ledger, checkout_id: str) -> List[str]` | -> record ids exported to this checkout and not submitted | one PR per batch. Submitted ids are never reselected (10.3, 10.8) | `analytics/submit.py` |
| S23 | `submit.decide_confirmation(yes: bool, interactive: bool, has_free_text: bool) -> ConfirmMode` | -> `"ask" \| "skip" \| "skip_withhold_notes" \| "refuse_noninteractive"` | `has_free_text` is true when the batch holds any `.md` note file, which can only be one I approved interactively (in an earlier export, or in this submit's own export step with `--note` at a TTY): a note drafted in this run without a TTY or with `--yes` is withheld by S16 first. Every input is known before submit's export step, so a `refuse_noninteractive` result is decided before anything is written to the checkout. `skip` only for `yes and not has_free_text`. `ask` when interactive in every other case, so a note at a terminal is always confirmed, even with `--yes` (10.11). `skip_withhold_notes` when there is no TTY, `--yes` is given and the batch holds a note: no prompt, every `.md` is left out of the commit (it stays untracked in the checkout as this machine's own file), the records and issue codes are submitted, and `⚠ issues note withheld: needs interactive approval` is printed (10.5, 10.11, 8.4). `refuse_noninteractive` when there is no TTY and no `--yes` (10.1) | `analytics/submit.py` |
| S24 | `submit.branch_name / commit_message / pr_title / pr_body(BatchSummary)` and `pr_text(records, machine_label, token=None)` | `BatchSummary` (record count, workflow-kind counts, months, machine label) and a token -> strings | built only from allowlisted values. The branch is `ck-stats-batch-<token>`, token a fresh `secrets.token_hex(4)` unless one is passed, so two submits never share a branch; with the token held fixed the four strings depend only on the summary (10.2, 10.3). The fixed template words are chosen to be unlikely confidential terms (no common directory or file names such as `analytics` or `runs`): the commit message and PR title are `ck-stats-batch: <count> (<kind> <n>, ...) <yyyy-mm>[ <yyyy-mm>...][ <machine>]`, and the PR body is the lines `count: <n>`, `kinds: <kind> <n>, ...`, `months: <yyyy-mm> ...` and, when a label is set, `machine: <label>`. The leak check still scans all of this text in full (9.1) | `analytics/submit.py` |
| S25 | `health.decide(newest: Optional[FormatCheck], auto_collect: bool, uncollected: int) -> List[DoctorFinding]` | -> warning / info findings | the 12.1 warning, the 12.2 info, the 12.3 count and suggestion | `analytics/health.py` |
| S26 | `collect.derive_outcome(result: object) -> Tuple[Optional[str], bool]` | spec-implement result -> `(outcome, inferred)` | `result.outcome` when it is a string (`inferred=False`). Otherwise, as spec-implement.js decides it today: `refused` when `refused` is true, `halted` when `halted` is true, `blocked` when `failed` or `notStarted` is a non-empty list, else `complete`, with `inferred=True`. A result that is not a dict gives `(None, False)` and `result` in `missing` (1.2, 1.12) | `analytics/collect.py` |
| S27 | `collect.tie_ck_version(manifest_version, manifest_mtime_ms, installed, run_start_ms, fresh, previous=None) -> Optional[str]` | the project's `.claude/ck-manifest.json` `version` and file mtime, the installed `manifest.ck_version()`, the run start, whether this collect runs right after the run (`--auto` from `ck run` or a skill), and the `ckVersion` of the local record this collect replaces -> a version or None | the ck version the run used (`ckVersion`), only when it can be tied to the run: the manifest version when the manifest predates the run start; else, for a fresh collect, the manifest version or else the installed version; else `previous` when an earlier collect tied one (so a later `--all` re-collect of an `--auto`-collected run keeps it); else None, and `ckVersion` goes into `missing`. A backfill never credits an old run to the installed release. Separately, every record carries `collectorVersion` = the installed version that wrote it; it is never used where a ck version is reported or grouped (show, trends, shareable `ck_version`) (1.2, 1.12) | `analytics/collect.py` |
| S28 | `runfile.recover_workflow_name(launches, run_id) -> Optional[str]` | (Workflow tool_use input, tool_result text) pairs from the main session transcript `<home>/projects/<proj>/<sid>.jsonl`, and the run id -> an allowlisted workflow name or None | the launch tied to the run is the one whose tool_result text contains the run id. Its name is `input.name` with a leading `spec:` plugin prefix removed (`spec:spec-implement` -> `spec-implement`, `spec:spec-create` -> `spec-create`), else, when `input.scriptPath` ends in `/spec-implement.js` or `/spec-create.js`, that basename without `.js`. The result is returned only when S1 accepts it; no tied launch, no name, or a name S1 rejects gives None, and the rejected name is dropped at once (1.15, 2.1, 2.2) | `analytics/runfile.py` |

`leak.scan` matching rule (S17, documented in `analytics/SCHEMA.md`, 9.3):

- **Strong terms**: at least 4 characters, not in `leak.COMMON_WORDS` (a fixed list of about 300 common
  English and programming words), and not in `leak.VOCABULARY` (every schema field name, enum value and
  fixed template word of the commit message and PR text). They match case-insensitively as substrings.
- **Weak terms**: every other term (shorter, common, or vocabulary). They match case-insensitively as
  whole words (`(?<![A-Za-z0-9])term(?![A-Za-z0-9])`) (9.3).
- **Where terms are matched.** Free text (issues notes, commit message, PR title, PR body, branch name,
  and any file that is not a parseable shareable record) is matched in full, strong and weak terms alike.
  A shareable-record JSON file is scanned leaf by leaf against `SHAREABLE_SCHEMA`:
  - a *fixed token* is a key that is a schema field name or an enum-keyed segment holding a member of its
    enum, or a string value whose schema position is an enum or a const (`schema`) and which is a member
    of that set. S14 already guarantees a fixed token is one of a fixed, published set, so it can hold no
    confidential data. Fixed tokens are skipped for terms and for generic patterns. This is what keeps a
    confidential path segment such as `analytics` or `transcripts`, or a title word such as `permission`,
    from matching inside `ck-run-analytics/1`, `priced_from_transcripts` or `permission_refusal` and
    blocking every export on the machine (9.3);
  - every other key and string value (any value that is not a member of its enum, a free string, and the
    format-validated values `month`, `id`, the version strings and `machine`) is matched against strong
    terms as substrings and weak terms as whole words (9.1, 9.3). The machine label stays scanned for
    terms, so a label that is a confidential term (`zephyr-payroll`) is a hit (9.2);
  - format-validated values (`month`, `id`, version strings, `machine`) are skipped for generic patterns
    only, because their format is fixed and short enough that a 32-character `machine` slug would
    otherwise trip the 32+ run pattern.
  A `.json` file that does not parse as a `ck-run-analytics` record is free text.
- Path-like terms are split into segments. Each segment is added under the same strong/weak rule, and
  the whole path is added as a strong term.
- **Digit-only terms are dropped.** A candidate term made only of decimal digits (`1`, `80`, `2026`) or only
  of hexadecimal digits (`[0-9a-f]+`, case-insensitive: `cafe`, `beef`, `0a1b`) is not added, whatever it
  came from (path segment, spec name, branch, task id). Such a term identifies no project, and it would
  otherwise hit a version (`2.1.80`), a `month` (`2026-10`) or a random 16-hex `id`. Run, session and agent
  ids are the exception and are kept whole, because they identify a run and are too long to collide by
  chance. Every position is still scanned for the remaining terms (9.3).
- **Generic patterns** (always on, all compiled with `re.IGNORECASE`, matched in all text except the fixed
  tokens and format-validated values above):
  - email `[\w.+-]+@[\w-]+\.[\w.-]+`
  - URL `[a-z][a-z0-9+.-]*://\S+`
  - scp-style git remote `[\w.-]+@[\w.-]+:\S+`
  - absolute path `(?:^|[\s"'(])(?:/(?:home|Users|root|var|tmp|mnt|opt|srv|etc)/|[A-Za-z]:\\|~/)`
  - UUID `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`
  - run id `wf_[A-Za-z0-9_-]{6,}`
  - timestamp or exact date `\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2})?` and epoch milliseconds `\b1[5-9]\d{11}\b`
  - key and token shapes `(?:sk-|sk-ant-|ghp_|gho_|github_pat_|xox[abp]-|AKIA)[A-Za-z0-9_-]{8,}` and any
    `[A-Za-z0-9_\-]{32,}` run

  `record_id` (16 hex) and `month` (`yyyy-mm`) are designed to match none of these patterns. No field
  name, enum member or template word is 32 characters or longer (the longest issue code,
  `review_contract_mismatch`, is 24), and a property test checks that none of them matches any generic
  pattern (P-24), so fixed-token skipping is a second guard and not the only one.

`roles.parse_label` grammar (S5). It is matched anchored, on the label exactly as given (a label with
surrounding whitespace is `other`). `N` is a task id `[0-9A-Za-z_]+`, the rule spec-implement.js uses for
task ids (`taskId`, `:473`), and `(m)` / `[m]` is a model. `P` is a path-shaped task id (any token
containing `/` or starting with `.`), which older ck versions put in labels by mistake; its role comes from
the leading keyword and its task is None, and the path itself is never copied.

| Workflow | Label | role | fields |
|---|---|---|---|
| implement | `task N` / `task N [test]` | implementer / test-writer | task, attempt=1 |
| implement | `task N[ [test]] retry K[ (m)]` | implementer / test-writer | task, attempt=K+1, escalated_to=m |
| implement | `task N[ [test]] revise R[ (m)]` | implementer / test-writer | task, round=R, escalated_to=m |
| implement | `review N[ round R]` | reviewer | task, round=R (default 1) |
| implement | `resolve N[ round R]` | resolver | task, round=R (default 1) |
| implement | `fix K (Task N red on landing)[ [m]]` | fixer | task, attempt=K, escalated_to=m, step=landing |
| implement | `fix K (<what>)[ [m]]` | fixer | attempt=K, escalated_to=m, step=target (no task) |
| implement | `re-check K` | gate | attempt=K, step=recheck |
| implement | `full gate @M` / `final gate` | gate | step=full / final |
| implement | `land N[ N...]` | land | tasks |
| implement | `cross-test wave W` | cross-test | wave=W |
| implement | `plan K[ retry]` | plan | attempt |
| implement | `report` | report | |
| implement (legacy) | `merge N` | land | task, step=merge (the per-task merge agent before batch landing) |
| implement (legacy) | `task P[ ...]`, `review P[ ...]`, `resolve P[ ...]`, `land P[ ...]` | implementer, reviewer, resolver, land | task=None, step=path_id (the rest of the label follows the matching current template) |
| create | `write requirements.md`, `write design.md`, `write implementation track`, `link tracks into tasks.md` | implementer | step=requirements/design/impl_track/link |
| create | `write test-plan.md`, `write test track` | test-writer | step=test_plan/test_track |
| create | `review: <lens>`, `refute: <lens>` | reviewer | step=review/refute |
| create | `revise spec + write review.md` | fixer | step=revise |
| create | `consistency check: pass P` | gate | step=consistency, round=P |
| create | `snapshot spec before revision`, `remove pre-revision snapshot` | land | step=snapshot/cleanup |

`spec-implement-iso` uses the implement grammar. The 5.3 check runs `tests/js/emit_labels.mjs` and asserts
`parse_label(l, wf).matched` for every emitted label, and for every label of the pinned legacy corpus
`tests/fixtures/legacy_labels.json`.

## Data Models

Python 3 typing, stdlib only, matching `bench.py`. Records are plain JSON dicts at rest. The `TypedDict`s
below define their shape. Decision inputs and outputs are frozen dataclasses. A `None` value always means
"unknown or not available", never zero (1.12, 6.7).

```python
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Literal, Optional, Tuple, TypedDict

SCHEMA_LOCAL = "ck-run-local/1"
SCHEMA_SHARED = "ck-run-analytics/1"

WorkflowKind = Literal["spec-implement", "spec-create"]          # local; shareable adds "spec-plan" (reserved, D8) and "other"
Role = Literal["implementer", "test-writer", "reviewer", "fixer", "resolver", "plan", "land", "gate",
               "cross-test", "report", "other"]
CostSource = Literal["claude_total", "priced_from_transcripts", "estimate"]   # run cost: the last two only (S7); claude_total = session figure
RunState = Literal["finished", "unfinished"]
HomeAccess = Literal["ok", "sandboxed", "missing"]
OutcomeKind = Literal["collected", "unchanged", "updated", "unfinished", "failed", "skipped_non_ck"]

class Usage(TypedDict):
    input: int; output: int; cacheRead: int; cacheCreation: int   # same keys as bench.USAGE_KEYS

class Cost(TypedDict):
    usd: Optional[float]          # None when nothing could be priced
    source: CostSource            # provenance label (1.4)
    pricing_date: str             # bench.PRICING_DATE, e.g. "2026-10"
    basis: str                    # bench._cost_summary-style text: "transcripts", "context tokens priced at the input rate", "mixed"

class AgentRow(TypedDict):
    agent_id: Optional[str]       # Claude Code agentId (local only)
    label: Optional[str]          # verbatim label (local only)
    role: Role                    # roles.parse_label
    matched: bool                 # False => role "other"
    task: Optional[str]           # task id named by the label
    attempt: Optional[int]
    round: Optional[int]
    step: Optional[str]           # grammar step, see the grammar table
    escalated_to: Optional[str]   # model named in the label
    phase: Optional[str]          # phaseTitle, else meta.json workflowPhase
    model: Optional[str]          # bench.normalize_model(model)
    state: Optional[str]          # workflowProgress state
    started_at_ms: Optional[int]  # epoch ms
    duration_ms: Optional[int]
    usage: Optional[Usage]        # None when there is no transcript
    usage_by_model: Dict[str, Usage]
    cost: Cost                    # per agent: priced_from_transcripts or estimate (context-tokens-as-input)
    transcript: Literal["ok", "missing", "unparseable", "truncated"]

class Bucket(TypedDict):          # one aggregate row (per model / role / phase / task)
    agents: int
    minutes: Optional[float]      # sum of agent duration_ms / 60000; None if every agent lacks it
    usage: Usage
    cost_usd: Optional[float]
    estimated: bool               # some contributing agent had no transcript

class ImplementTask(TypedDict):   # spec-implement, per task (1.5)
    task: str; title: Optional[str]; wave: Optional[int]; track: Literal["impl", "test", "unknown"]
    ok: Optional[bool]
    attempts: Optional[int]
    retries: Optional[int]        # attempts - 1 - retry-stage escalations
    escalations: List[str]        # models
    review_rounds: Optional[int]
    verdict: Optional[str]        # approve | changes | unreviewed | ...
    resolves: Optional[int]
    resolves_inferred: bool       # 1.7
    landing_fixes: Optional[int]  # fix agents with step=landing for this task
    agent_minutes: Optional[float]; to_merged_minutes: Optional[float]
    idle_before_merge_minutes: Optional[float]   # mergedAt - finishedAt
    risk_tag: Optional[str]
    reason: Optional[str]         # failure reason (local only; input to issue codes and note drafts)

class ImplementMetrics(TypedDict):  # spec-implement, per run (1.5)
    outcome: Optional[str]          # S26 derive_outcome: result.outcome, else refused/halted/blocked/complete from the flags and lists
    outcome_inferred: bool          # True when S26 derived it (pre-0.10 results have no result.outcome)
    report_source: Optional[str]
    tasks: List[ImplementTask]
    landings: List[dict]            # {size, merged, red, conflict, bisected, minutes} counts/ints; task ids kept locally
    full_gates: List[dict]          # {after_merges, green}
    final_gate_green: Optional[bool]
    final_gate_fixed: Optional[bool]
    cross_tests: List[dict]         # {wave, tasks: int, verdict, blocking: int}
    fixer_escalations: List[dict]   # {model, green}
    stats: Dict[str, Optional[int]] # result.stats as given
    timing_phases: List[dict]       # result.timing.phases: {group, tasks, minutes}
    task_span_minutes: Optional[float]   # result.timing.minutes

class CreateMetrics(TypedDict):     # spec-create (1.6)
    mode: Optional[str]             # create | review
    review_result: Optional[str]    # Ready | Ready after decisions | Needs work
    fixed: Optional[int]; not_fixed: Optional[int]; escalated: Optional[int]
    refuted: Optional[int]; lenses_lost: Optional[int]; open_questions: Optional[int]
    consistency_passes: Optional[int]; consistency_fixed: Optional[int]
    phase_minutes: Dict[str, Optional[float]]   # phaseTitle -> span (min start to max end) from agent timings

class LocalRecord(TypedDict):
    schema: str                     # SCHEMA_LOCAL
    run_id: str
    session_id: Optional[str]
    workflow: WorkflowKind
    workflow_name: str              # exact allowlisted name (e.g. spec-implement-iso)
    ck_version: Optional[str]       # S27 tie_ck_version: the version the run used, or None ("ckVersion" in missing); on disk "ckVersion"
    collector_version: str          # installed ck that wrote this record, always set; never used per version (on disk "collectorVersion")
    claude_code_version: Optional[str]   # most common "version" in the run's transcripts
    status: str                     # run file status
    outcome: Optional[str]
    outcome_inferred: bool          # S26
    start_ms: Optional[int]         # run file startTime
    wall_ms: Optional[int]          # run file durationMs (whole workflow incl. plan, final gate, report)
    collected_at: str               # UTC ISO, local only
    project: dict                   # {root: Optional[str], spec: Optional[str], branch: Optional[str],
                                    #  remote: Optional[str], pseudonym: Optional[str], stack: Optional[str]}
    config: Dict[str, dict]         # role -> {model, effort}: implement from result.agents ("model/effort"), create from observed agent models
    agents: List[AgentRow]
    by_model: Dict[str, Bucket]; by_role: Dict[str, Bucket]; by_phase: Dict[str, Bucket]; by_task: Dict[str, Bucket]
    cost: Cost                      # run total: priced via bench.aggregate_usage over the run's transcripts;
                                    # source priced_from_transcripts or estimate only (S7)
    claude_session: Optional[dict]  # {usd, source: "claude_total", scope: "session", pricing_date}: the budget_usd
                                    # attachment total, session-wide; never added to the run cost or trends
    # On disk (Harness Contract) both figures go in `costs`: [run cost, session figure if any].
    other_role: Bucket              # 5.2 summary
    implement: Optional[ImplementMetrics]
    create: Optional[CreateMetrics]
    missing: List[str]              # e.g. "result", "runfile" (1.15), "agent_entry:4" (a non-dict workflowProgress entry, 1.12),
                                    # "transcripts:3/250", "transcript-unparseable:2", "meta"
    terms: List[str]                # extra confidential strings seen (task titles, labels, paths, branch, lens keys) for leak.build_terms

class Ledger(TypedDict):
    exported: Dict[str, dict]       # run_id -> {record_id, month, checkout_id, files: [rel paths], note: bool, exported_at}
    submitted: Dict[str, dict]      # record_id -> {pr_url, branch, submitted_at, withheld: [rel paths of .md notes left
                                    #  out of the commit by S23 skip_withhold_notes; they count as own_pending for S21]}
# checkout_id = sha256 of the checkout's resolved path (local only).

class ShareableRecord(TypedDict, total=False):   # ck-run-analytics/1; every path listed in analytics/SCHEMA.md
    schema: str                     # "ck-run-analytics/1"
    record_id: str                  # 16 hex, random, unrelated to any id (on disk: "id", Harness Contract)
    ck_version: str                 # version regex, else "other"; "other" also when the local value is None (S27)
    claude_code_version: Optional[str]
    workflow: str                   # spec-implement | spec-create | spec-plan | other
    variant: str                    # default | iso | other
    outcome: str                    # complete | blocked | halted | refused | ready | ready_after_decisions | needs_work | failed | other
    month: str                      # yyyy-mm of start
    machine: str                    # optional slug
    stack: str                      # optional: python | node | go | rust | java | ruby | dotnet | other
    config: Dict[str, Dict[str, str]]   # role in {implementer,test_writer,reviewer,reviewer_risky,fixer,orchestrator,resolver} -> {model: alias, effort: low..max|inherit|other}
    counts: Dict[str, int]          # tasks, tasks_merged, tasks_failed, tasks_not_started, waves, agents, attempts, retries,
                                    # escalations, review_rounds, conflicts, conflicts_inferred, fixes, landings, bisects,
                                    # gates_green, gates_red, cross_tests, cross_test_blocking, plan_steps, plan_failures,
                                    # review_fixed, review_not_fixed, review_escalated, refuted, lenses_lost,
                                    # consistency_passes, consistency_fixed, open_questions, agents_other
    agents_by_role: Dict[str, int]  # Role -> count
    verdicts: Dict[str, int]        # approve | changes | unreviewed | other -> count
    durations: dict                 # {wall_minutes, task_span_minutes,
                                    #  phases: [{phase: plan|wave|tasks|final_gate|report|requirements|design_test_plan|task_tracks|link|adversarial_review|revise|consistency_check|other, wave: int|null, minutes}],
                                    #  by_role: {Role: minutes}}
    tasks: List[dict]               # ordered by start; {index, track, ok, attempts, review_rounds, verdict, resolves, landing_fixes,
                                    #  escalations, risk, agent_minutes, to_merged_minutes, idle_minutes}
    tokens: dict                    # {by_model: {alias: Usage}, by_role: {Role: Usage}}
    cost: dict                      # {usd, source: priced_from_transcripts | estimate, pricing_date, estimated: bool,
                                    #  by_model: {alias: usd}, by_role: {Role: usd}}  (the session-wide claude_total is not exported)
    issues: List[str]               # ISSUE_CODES members, sorted (Harness Contract); an approved note is <id>.md next to the record
    missing: List[str]              # enum: result | runfile | agent_entries | transcripts | meta | timing | ck_version

ISSUE_CODES = frozenset({
    # spec-implement
    "gate_red_after_merge",           # a full gate or the final gate was red (before or after its fix)
    "landing_red",                    # a landing batch had a red task
    "landing_bisected",
    "resolver_rounds_exceeded",       # a task reason starts "merge failed: still conflicting after"
    "conflict_not_resolved",          # "merge failed: conflict not resolved"
    "review_blocking_unresolved",     # an impl-track task failed on "review: blocking findings remain"
    "review_contract_mismatch",       # the same, on a test-track task (contract check against design.md)
    "cross_test_blocking",            # a crossTests entry with a blocking finding
    "planning_failed",                # stats.planFailures > 0 or reason == "planning step failed"
    "plan_cycle",                     # result.refused
    "retry_no_commits",               # a retry followed "no commits", or reason "merge failed: no commits"
    "escalated_to_opus",              # any task or fixer escalation to the opus family
    "escalated",                      # any escalation at all
    "red_on_landing_fixed",           # landing fixes > 0 on a merged task
    "worktree_busy",
    "unreviewed_merge",               # verdict "unreviewed" on a merged task
    "report_from_agents",             # reportSource "agents"
    "permission_refusal",             # bench.REFUSAL_RE matches a failure reason
    "run_halted", "run_blocked",
    # spec-create
    "review_needs_work", "review_lenses_lost", "consistency_conflicts", "review_escalated",
    # collection quality
    "unknown_roles",                  # other_role.agents > 0
    "transcripts_missing",            # any agent without a usable transcript
})

@dataclass(frozen=True)
class LabelMatch:
    role: str; matched: bool; task: Optional[str] = None; tasks: Tuple[str, ...] = ()
    attempt: Optional[int] = None; round: Optional[int] = None; step: Optional[str] = None
    escalated_to: Optional[str] = None; wave: Optional[int] = None

@dataclass(frozen=True)
class FormatCheck:
    ok: bool; problems: Tuple[str, ...] = ()   # e.g. ("workflowProgress is not a list",); never the workflow name

@dataclass(frozen=True)
class CollectOutcome:
    run_id: str; kind: OutcomeKind; record: Optional[dict] = None
    reason: Optional[str] = None               # for failed/unfinished; never a non-ck name
    error: Optional[Literal["not_found", "not_allowlisted", "unrecognised", "sandboxed", "unreadable"]] = None

@dataclass(frozen=True)
class CollectCounts:
    collected: int; unchanged: int; updated: int; unfinished: int; failed: int; skipped_non_ck: int
    failures: Tuple[Tuple[str, str], ...]      # (run_id, reason)

@dataclass(frozen=True)
class LabelDecision:
    ok: bool; value: Optional[str]; error: Optional[str]

@dataclass(frozen=True)
class GitIdentity:
    name: Optional[str]; email: Optional[str]

@dataclass(frozen=True)
class TermSet:
    strong: FrozenSet[str]; weak: FrozenSet[str]   # lower-cased

@dataclass(frozen=True)
class LeakHit:
    file: str; field: str; kind: Literal["term", "pattern"]; rule: str   # rule: pattern name or "strong"/"weak"; never the matched value
    # The CLI prints file and field (JSON path, or "line N"). The term itself is not echoed, so the console stays clean.

@dataclass(frozen=True)
class Gate:
    allowed: bool; hits: Tuple[LeakHit, ...]

@dataclass(frozen=True)
class NoteDraft:
    text: Optional[str]; error: Optional[str]

NoteDecision = Literal["include", "withhold_noninteractive", "discarded", "blocked_by_leak", "none"]
ConfirmMode = Literal["ask", "skip", "skip_withhold_notes", "refuse_noninteractive"]

@dataclass(frozen=True)
class OrphanRun:                    # 1.15: run file gone, agent transcripts remain
    agent_dir: Path; session_jsonl: Optional[Path]

@dataclass(frozen=True)
class ExportPlan:
    new: Tuple[str, ...]; reexport: Tuple[str, ...]; restore: Tuple[str, ...]; nothing_new: bool
    # restore: exported to this checkout, unsubmitted, any of its files missing (10.10); replace: files of an
    # unsubmitted earlier export of a re-exported or restored run that are still present, removed before the
    # new files are written (10.10, 10.11)
    replace: Tuple[str, ...] = ()

@dataclass(frozen=True)
class PendingFile:
    rel_path: str; text: str; run_id: str; record_id: str; is_note: bool

@dataclass(frozen=True)
class BatchSummary:
    records: int; kinds: Dict[str, int]; months: Tuple[str, ...]; machine: Optional[str]

@dataclass(frozen=True)
class Preflight:
    ok: bool; reason: Optional[str]

@dataclass(frozen=True)
class SubmitResult:
    ok: bool; pr_url: Optional[str]; branch: Optional[str]; error: Optional[str]; rolled_back: bool

@dataclass(frozen=True)
class DoctorFinding:
    level: Literal["warning", "info"]; message: str

@dataclass(frozen=True)
class ShowFilters:
    kind: Optional[str]; since: Optional[str]; until: Optional[str]; project: Optional[str]

class ShowList(TypedDict):
    runs: List[dict]   # {run_id, date, workflow, outcome, wall_minutes, cost_usd, cost_source, tasks_merged, tasks_total}
    trends: List[dict] # Trend
    unreadable: int

class Trend(TypedDict):
    workflow: str; runs: int
    cost_per_merged_task: Optional[float]; wall_minutes_per_task: Optional[float]
    role_cost_share: Dict[str, float]; role_time_share: Dict[str, float]

class ShowRun(TypedDict):
    run: dict; by_role: dict; by_phase: dict; by_model: dict
    slowest_tasks: List[dict]; costliest_tasks: List[dict]   # top 5
    rates: dict        # retry, escalation, review_round, conflict, fix: per task, None when unknown
    idle_before_merge: dict    # total, median, max minutes
    landings: dict     # count, mean size, bisected, red
    other_role: dict
    coverage: Dict[str, str]   # duration name -> what it covers and excludes (6.8)
    missing: List[str]
```

Duration coverage strings (6.8, `show.COVERAGE`):

- `wall`: "run file durationMs: from workflow start to return; includes plan, final gate and report".
- `task_span`: "first task start to last merge or finish; excludes plan, final gate and report".
- Role and phase minutes: "sum of agent durations; parallel agents overlap, so the sum can exceed wall
  time; excludes waits between agents".
- `idle_before_merge`: "task finished to merged; waiting in the landing queue plus landing, resolve and
  fix time".

## Data Flow

```mermaid
sequenceDiagram
    autonumber
    participant U as Me / skill / ck run
    participant CLI as cli/stats.py
    participant RF as analytics.runfile
    participant C as analytics.collect (+bench)
    participant S as analytics.store
    participant X as analytics.export/share/issues
    participant L as analytics.leak
    participant SB as analytics.submit
    participant G as git + gh (fake in tests)

    U->>CLI: ck stats collect --run wf_x [--auto]
    CLI->>RF: classify_home / find_run_file / peek_workflow_name
    RF-->>CLI: is_allowlisted? check_format? run_state?
    alt not allowlisted / unrecognised / unfinished / missing / sandboxed
        CLI-->>U: refusal or info, nothing written
    else ok
        CLI->>C: collect_run(home, run_id)
        C->>C: scan_transcript per agent, parse_label per agent, aggregate_usage, price
        C-->>CLI: CollectOutcome(record)
        CLI->>S: write_private_json(runs/wf_x.json) (replace)
    end

    U->>CLI: ck stats submit --to ~/claude-kiro
    CLI->>SB: remote_urls, target_remote (-> remote name R), decide_preflight
    CLI->>SB: decide_confirmation (S23) from pending approved .md notes; refuse_noninteractive (no TTY, no --yes) -> exit 1 before any write (10.1)
    CLI->>X: plan_export(records, ledger) then build_shareable + derive_issue_codes
    opt --notes and interactive
        X->>X: draft_note (claude -p) then L.scan then edit/accept/discard then L.scan then decide_note
    end
    CLI->>L: build_terms(all records, git identity, non-ck names) then scan(files + branch + commit + PR text)
    alt hits
        CLI-->>U: file + field per hit, nothing written or committed, exit 1
    else clean
        CLI->>X: write_batch (files land in checkout), ledger.exported
        CLI-->>U: unified diff of every file
        CLI->>SB: decide_confirmation (S23, counts approved .md notes), gh preflight (gh --version, gh auth status)
        U->>CLI: confirm
        SB->>G: git fetch R master, git switch -c ck-stats-batch-<hex> R/master (untracked batch files carry over), add only the batch files (no .md under skip_withhold_notes), commit
        SB->>G: git push R ck-stats-batch-<hex>, gh pr create --draft --repo kutlayhanli/claude-kiro --base master --head ck-stats-batch-<hex>
        alt any step fails
            SB->>G: switch back, delete local branch, rewrite files untracked
            CLI-->>U: error, files left for manual commit, exit 1
        else ok
            SB->>G: switch back to original branch
            CLI->>S: ledger.submitted[record_id] = pr_url
            CLI-->>U: PR URL
        end
    end
```

The PR branch starts from `R/master`, never from the checkout's current HEAD, so unrelated or unpushed
commits on the branch I have checked out never reach the PR (10.2: "commit only the new shareable files").
Fetch and push name `R`, the remote S20 matched, so a fork `origin` next to an `upstream` that points at
`kutlayhanli/claude-kiro` is never the push target. After the PR is opened (or on any failure) the
original branch is checked out again and the batch files are left untracked in its working tree only on
failure (10.6).

`ck run` after-exit flow: `subprocess.call` returns, `spend_report`, then `auto_collect_after_run`. That
checks S10, finds the session, calls `runfile.session_run_ids(home, sid, since_ms=started*1000 - 5000)`,
then for each run `collect_run` + `write_outcome`, and writes a log line like `stats: wf_x collected ->
<path>` or `stats: wf_x not collected (<reason>); retry: ck stats collect --run wf_x`. When the session
holds run files whose workflow name cannot be read (for example truncated JSON), it prints one count-only
line to stdout and the `ck run` log, `stats: <n> run file(s) in this session could not be read; retry: ck
stats collect --all` (for one file: `stats: 1 run file in this session could not be read; retry: ck stats
collect --all`), with no run id (2.2, 3.3), and appends `<utc iso> - unrecognised` to the collect log once
per such file. The exit code is the `claude` exit code, whatever happens.

## Error Handling

| Criterion | Condition | Behaviour |
|---|---|---|
| 1.10 | `find_run_file` returns None and S4 = `ok`/`missing` | `❌ run wf_x not found under <home>` (the resolved `bench.claude_home()`), nothing written, exit 1 |
| 1.11 | S3 = `unfinished` | `ℹ️ wf_x has not finished; nothing written`, exit 0. `--all` counts it as `unfinished` |
| 1.12 | readable allowlisted run file that passes S2 (so `workflowProgress` is a list) whose `result` is missing or not a dict, whose list holds non-dict entries or dict entries with missing or malformed fields, some `agent-*.jsonl` or `.meta.json` absent, unparseable lines, truncated transcript JSON | record written with the parts that could be read. A non-dict entry is skipped and listed in `missing` (`agent_entry:<index>`). `missing` lists what is absent. Agents without transcripts get `cost.source="estimate"` (context tokens at the input rate, as `bench.collect_agents` does), and the run cost becomes `estimate` (S7). Fields that cannot be computed are `None`, never 0. `scan_transcript` already skips undecodable lines; an `OSError` on a file marks it `unparseable` |
| 1.13 | S2 not ok (including a missing or non-list `workflowProgress`), or the run file is not valid JSON | With `--run`: `❌ wf_x: run-file format not recognised (<problems>)`, where `wf_x` is the id I typed. The workflow name appears only when S1 is true. Nothing written, exit 1. During `--all` and `ck doctor`: an allowlisted run that fails S2 is `failed` with its id; a run file whose name cannot be read is counted in `skipped_non_ck`, never named (2.2). A run file that is missing while its transcripts remain is 1.15 |
| 1.15 | `find_run_file` is None, `find_orphan_run` finds `subagents/workflows/<run_id>/` | S28 over `workflow_launches(<sid>.jsonl)`. Allowlisted name: a partial record from `agent-*.jsonl` (usage, models, cost, per-agent `duration_ms` = last minus first transcript `timestamp`) and `agent-*.meta.json` (`description` = label -> role, `workflowPhase` -> phase); `start_ms` = earliest timestamp, `wall_ms` = latest minus earliest; `status` and `outcome` None; S27 with that start; `missing` holds `runfile` and `result` (Harness Contract `{agentId: null, what: "runfile"}` and `what: "result"`); `✓ wf_x: wrote <path> (partial: run file missing)`, exit 0. No tied launch, no session transcript, or a name S1 rejects: the 1.10 message and exit 1, nothing written, the name never printed or kept |
| 1.14 | S4 = `sandboxed` | `❌ ck stats collect must run outside the sandbox: <home> is not readable here`, exit 1, nothing written. With `--auto` under `ck run`: an info line, exit 0 |
| 2.2 | non-allowlisted (or unreadable-name) run met during `--all`, `doctor`, `ck run` or leak-term gathering | only a counter goes up. The name and id never reach records, logs or console. `CollectOutcome.reason` and `FormatCheck.problems` are built without the name. Under `ck run` an unreadable-name run file in the session is a collection failure (3.3) reported by count only: `stats: <n> run file(s) in this session could not be read; retry: ck stats collect --all` on stdout and in the `ck run` log, and `<utc iso> - unrecognised` in the collect log |
| 2.3 | `--run` names a non-allowlisted run | `❌ wf_x is not a ck spec workflow run; not collected` (the id was typed by me, the name is never printed), exit 1 |
| 3.3 | any exception in `auto_collect_after_run` or `collect --auto` | caught (`except Exception`, like `spend_report`), one log line plus `ck stats collect --run <id>` printed, exit status and report unchanged. The skills quote the line and change nothing else |
| 3.6 | `ck agents set run.analytics maybe` | `❌ run.analytics must be true or false (also yes/no, on/off)`, exit 2, file untouched (`_coerce` raises before `write_setting` writes) |
| 4.3 | one run raises during `--all` | caught per run in the worker and recorded as `failed` with its reason. Others continue. S8 gives exit 1 at the end |
| 4.4 | `--all` finds no allowlisted runs | `ℹ️ no ck spec runs found under <home> (N other workflow runs skipped)`, exit 0 |
| 6.5 | store empty | `ℹ️ no runs collected yet; run ck stats collect --all`, exit 0 |
| 6.6 | `show wf_x` with no record | `❌ no local record for wf_x (ck stats collect --run wf_x)`, exit 1 |
| 6.7 | record of an older schema, or missing keys | `load_records` accepts any `ck-run-local/<n>`. Readers use `.get`. Absent values show as `unknown` in text and `null` in JSON |
| 7.6 | invalid machine label (`ck agents set run.machine_label` or its alias `ck stats label`) | `❌ machine label must be 1-32 lowercase letters, digits or hyphens`, exit 2, config file byte-identical. `none`/`default` clear the label |
| 7.12 | export without `--note`, or with `--note` but non-interactive | `draft_note` is never called, so no `claude` subprocess starts. With `--note` non-interactive the 8.4 withheld line is printed |
| 7.9 / 10.9 | S20 false | `❌ <checkout> is not a kutlayhanli/claude-kiro checkout; nothing written`, exit 1. Checked before any write |
| 7.10 / 10.8 | S19 `nothing_new` and S22 empty | `ℹ️ nothing new to export` / `nothing new to submit`, exit 0, no branch or PR |
| 7.7 (bug guard) | `validate_shareable` returns violations at export time | export aborts before writing: `❌ internal: shareable record violates the schema (<paths>)`, exit 1. This should never happen; it guards the allowlist |
| 8.4 | note present and not interactive (no TTY or `--yes`) | note dropped (S16 `withhold_noninteractive`). Record and codes kept. `⚠ issues note withheld: needs interactive approval` |
| 8.5 | discard, or final leak hits on the note | record exported without the `.md`. The hits are printed |
| 8.2 (drafter failure) | `claude` missing, non-zero exit or timeout | `⚠ could not draft a note (<error>)`. Offer to write one by hand in `$EDITOR` (`click.edit`) or skip. Export goes on |
| 9.2 | S18 not allowed | each hit as `<file>: <field> (<rule>)`, `❌ leak check failed; nothing written or committed`, exit 1. Export writes nothing because files are built in memory first. Submit's second check (branch, commit and PR text) runs before any git mutation |
| 10.4 | I answer no | `ℹ️ not submitted`. No branch, commit or PR. Exported files stay untracked in the checkout as from `export`, and the ledger does not mark them submitted. A later submit accepts them as `own_pending` (10.7) |
| 10.6 | `gh` missing (`shutil.which`), `gh auth status` non-zero, or `git push` / `gh pr create` fails | preflight failures stop before any git change. A later failure rolls back: `git switch <original>`, `git branch -D <new>`, the files are rewritten untracked, and a pushed remote branch is named in the message. Nothing marked submitted, exit 1 |
| 10.7 | S21 not ok | `❌ <checkout> has uncommitted changes (<n> paths)` or `destination <path> already exists`. Changes that are this machine's own exported-but-unsubmitted files, or notes withheld by an earlier submit (10.11), do not count. Working tree untouched, exit 1 |
| 10.10 | ledger says exported to this checkout and unsubmitted, but its files are gone, or one of them is (I deleted them, or cleaned the checkout) | S19 lists the run in `restore` and any of its files still present in `replace`; export/submit removes those, builds the run again with a new record id and updates the ledger entry. `ℹ️ re-exported <n> record(s) whose files were missing` |
| 10.5 / 10.11 | the batch holds an approved `.md` note from an earlier export | With a TTY: S23 `ask` even with `--yes`; confirming commits the note with its record. With no TTY and `--yes`: S23 `skip_withhold_notes`: the `.md` files are left out of the commit and stay untracked in the checkout (S21 treats them as this machine's own files), the records and issue codes are submitted, and `⚠ issues note withheld: needs interactive approval` is printed; exit 0 when the PR opens. With no TTY and no `--yes`: `refuse_noninteractive` (10.1), decided before submit's export step, so nothing in the checkout is written, restored or removed |
| 12.1 | newest allowlisted run fails S2 | doctor warning `Run-file format of the newest ck run is not recognised (<problems>): ck stats cannot collect it - update ck` |
| 12.2 | `run.analytics` false | `ℹ️ Automatic run analytics is off (ck agents set run.analytics true)`. Not a warning |
| 12.3 | finished allowlisted runs without local records | warning `<n> finished ck run(s) not collected - run ck stats collect --all` |

## Security, Performance, Migration

### Security

- **Allowlist by construction.** `build_shareable` reads named fields from the local record into a new
  dict. Every string passes S11/S12 or a format regex (S14). It has no parameter for the pseudonym, the
  remote, the spec name, titles, labels or ids. `validate_shareable` runs on every record before writing.
- **Two independent guards.** Schema validation (structure), then the leak scan (content) over every file,
  plus branch name, commit message, PR title and PR body as pseudo-files. Terms come from *all* local
  records (spec names, task titles, summaries, finding texts, labels, paths and their segments, branch
  names, remote URL and its owner/org/name parts, session/agent/run ids, lens keys), from `git config
  user.name`/`user.email` of the checkout, and from in-memory non-ck workflow names read from the Claude
  home at export time (best effort; nothing is written).
- **Free text is held.** Only the issues note is free text. S16 makes interactive acceptance plus a clean
  final scan mandatory, and `--yes` can never include it.
- **No network outside submit and the drafter.** Collect, show and export never spawn `gh`, `git fetch`
  or any network client. Remote URLs are read from local git config. Submit's only network calls are
  `git fetch <R> master`, `git push <R>` and `gh`. The one exception in export is the drafter subprocess
  (`claude -p`), which runs only with `--note` in an interactive terminal, and sends only the local
  record's issue codes, counts and failure reasons to the same Claude access the run used (7.11, 7.12).
- **Local protection.** `write_private_json` creates files with `os.open(..., O_CREAT|O_WRONLY, 0o600)`
  and creates missing directories with mode 0700. The salt's `config.json` is written through a 0600 temp
  file and `os.replace`, then `os.chmod(0o600)`, so a `config.json` that `ck agents set` created earlier at
  the default umask (0644) is tightened the first time the salt is written.
- **Sandbox.** The data dir (`~/.local/share/claude-kiro`) lies under `SANDBOX_HOME_READABLE`'s `~/.local`,
  so `sandbox_settings` lists the resolved data dir in `credentials.files` with `mode: deny`, like
  `CREDENTIAL_PATHS`. A sandboxed agent in project A therefore cannot read project B's local records (task
  titles, paths, remotes, failure reasons) and copy them into A or out over an allowed domain. Collection
  needs `~/.claude`, which the sandbox hides, so it runs only in the outer process anyway.
- **Input hardening.** Run ids are validated before glob use. The checkout path is resolved. Export writes
  only under `<checkout>/analytics/runs/` and refuses symlinked `analytics`/`runs` directories.

### Performance

- One `scan_transcript` pass per agent file. The run total reuses those scans through
  `bench.aggregate_usage` instead of calling `session_spend` again. Target: a 250-agent fixture run in
  under 10 s.
- `collect --all` uses `concurrent.futures.ThreadPoolExecutor(max_workers=8)` over run files, collects
  results by index (deterministic order), and writes records from the main thread. Target: 100 fixture
  runs in under 2 minutes.
- `peek_workflow_name` parses each run file once and keeps only the name, so non-ck runs cost one JSON
  parse.

### Migration

- No migration of existing data. The first `ck stats collect --all` backfills the run files already on
  disk.
- `run.analytics` defaults to true (3.5). Existing configs are unaffected.
- Older spec-implement results lack `resolves`. The collector infers the count and marks it
  `resolves_inferred` (1.7). Shareable records count inferred conflicts separately in
  `counts.conflicts_inferred`.
- Older spec-implement results also lack `outcome`. S26 derives it with the script's own rule and marks it
  `outcome_inferred`, so backfilled runs are `complete`/`blocked`/`halted`/`refused`, not `other`.
- Backfilled runs are credited to a ck version only when S27 can tie one to the run (the project manifest
  predates the run, or an earlier collect tied one); otherwise `ck_version` is None locally and `other` in
  shareable records, never the installed version. The installed version is kept apart as
  `collector_version`.
- Older runs carry labels from earlier label templates (`merge N`, path-shaped task ids). The grammar's legacy
  rows and the pinned legacy corpus keep them out of `other`.
- Schema versions are `ck-run-local/1` and `ck-run-analytics/1`. Later fields are optional, and readers
  accept older records (6.7).

## Requirements Traceability

| Criterion | Design element |
|---|---|
| 1.1 | `ck stats collect --run`. `store.data_dir`/`record_path`/`write_private_json`. Prints the path |
| 1.2 | `LocalRecord` fields: schema, ck_version (S27, keeps an earlier tied value), collector_version, claude_code_version, workflow, outcome (S26), start_ms, wall_ms, config |
| 1.3 | `collect_run` with `bench.scan_transcript` per agent and `bench.aggregate_usage`. `by_model`/`by_role`/`by_phase`/`by_task` `Bucket`s |
| 1.4 | `Cost.source` + `pricing_date`. S7 `collect.cost_label`; session `claude_total` figure kept apart |
| 1.5 | `ImplementTask`, `ImplementMetrics` |
| 1.6 | `CreateMetrics` (review counts, consistency, `phase_minutes` from agent timings) |
| 1.7 | S6 `collect.conflict_count`. `resolves_inferred` |
| 1.8 | `agents.ensure_salt` (`stats_salt` in `global_config_path()`, 0600). `project.pseudonym` = HMAC-SHA256(salt, normalised remote)[:16]. `build_shareable` has no pseudonym input |
| 1.9 | `write_outcome` replaces `runs/<run_id>.json`. `unchanged`/`updated` outcome kinds |
| 1.10 | S4 + `find_run_file`. Error Handling 1.10 |
| 1.11 | S3 `runfile.run_state`. Error Handling 1.11 |
| 1.12 | S2 (list check only), skipped non-dict entries in `missing`, `AgentRow.transcript`, `LocalRecord.missing`, S7 `estimate`, S26 `outcome_inferred`, S27 `None` ck version, `None` for unknowns |
| 1.13 | S2 `runfile.check_format`, `peek_workflow_name` returning None for invalid JSON. Error Handling 1.13 |
| 1.14 | S4 `classify_home`. `CK_RUN_SANDBOXED` in `cli/run.py` |
| 1.15 | `runfile.find_orphan_run`, `workflow_launches`, S28 `recover_workflow_name`, the transcripts-only path of `collect_run`. Error Handling 1.15 |
| 2.1 | S1 `runfile.is_allowlisted`, `peek_workflow_name`, `ALLOWLISTED_WORKFLOWS` |
| 2.2 | S1 + counters only in `CollectCounts.skipped_non_ck` (also unreadable-name run files). `CollectOutcome.reason` built without names; only an id I typed in `--run` is echoed |
| 2.3 | S1 in `collect --run`. Error Handling 2.3 |
| 3.1 | `cli/run.py: auto_collect_after_run`, `runfile.session_run_ids`, log lines |
| 3.2 | `plugin/skills/implement/SKILL.md` step 9, `plugin/skills/create/SKILL.md` Close the loop, `plugin/skills/review/SKILL.md` step 4, `collect --auto` |
| 3.3 | `auto_collect_after_run` / `--auto` catch-all, `store.append_collect_log`, retry command; the count-only line for unreadable run files from `session_run_ids` |
| 3.4 | `agents.DEFAULTS["run"]["analytics"]`, S10 `collect.auto_collect_enabled` in `ck run` and `--auto` |
| 3.5 | Default `True`. S10 treats unset as on |
| 3.6 | S9 `agents._coerce` boolean branch, message naming accepted values |
| 4.1 | `collect.collect_all` over `runfile.iter_run_files` |
| 4.2 | `CollectCounts` printed by `collect --all` |
| 4.3 | Per-run try/except in the worker. S8 exit code |
| 4.4 | S8 empty case. Error Handling 4.4 |
| 5.1 | S5 `roles.parse_label` grammar table, including the legacy rows |
| 5.2 | S5 `matched=False` -> `other`. `LocalRecord.other_role`, shown in `ShowRun.other_role` |
| 5.3 | `tests/js/emit_labels.mjs` + `tests/fixtures/legacy_labels.json` + check that every label has `parse_label(...).matched` |
| 6.1 | `show.list_runs`, `render_list` |
| 6.2 | `show.run_breakdown` (`ShowRun`) |
| 6.3 | `show.trends`, `ShowFilters` (kind, since/until, project pseudonym) |
| 6.4 | `--json` printing `ShowList`/`ShowRun` |
| 6.5 | Error Handling 6.5 |
| 6.6 | Error Handling 6.6 |
| 6.7 | `store.load_records` version tolerance. `None` -> `unknown` |
| 6.8 | `show.COVERAGE`, `ShowRun.coverage` |
| 7.1 | `export.plan_export`/`build_batch`/`write_batch`. Path `analytics/runs/<yyyy-mm>/<record_id>.json`. `share.new_record_id` |
| 7.2 | S14 `share.build_shareable`, `SHAREABLE_SCHEMA`, `ShareableRecord` |
| 7.3 | S14 (no inputs for excluded categories) + S17 leak scan |
| 7.4 | S11 `share.to_enum` |
| 7.5 | S12 `share.model_alias` |
| 7.6 | S13 `agents.valid_machine_label`, `ck agents set run.machine_label` (alias `ck stats label`), `machine` omitted when unset |
| 7.7 | `analytics/SCHEMA.md`, `share.schema_field_paths`, `validate_shareable` |
| 7.8 | S19 + `Ledger.exported`, `--reexport` |
| 7.9 | S20 `submit.is_target_repo`, called by export too |
| 7.10 | S19 `nothing_new`. Error Handling 7.10 |
| 7.11 | Export uses only local git reads and file writes; the drafter is the only exception (Security: no network) |
| 7.12 | `draft_note` called only on the `--note` + interactive path. Error Handling 7.12 |
| 8.1 | S15 `issues.derive_issue_codes`, `ISSUE_CODES` |
| 8.2 | `issues.draft_note` (`DRAFTER_ARGV` / `$CK_STATS_DRAFTER`), leak scan with marked hits, edit (`click.edit`) / accept / discard prompt |
| 8.3 | Second `leak.scan` on the final text. S16 `include` only with 0 hits |
| 8.4 | S16 `withhold_noninteractive` |
| 8.5 | S16 `discarded` / `blocked_by_leak` |
| 9.1 | S17 `leak.build_terms` (all local records, git identity, non-ck names) + generic patterns. Run by export and submit |
| 9.2 | S18 `leak.decide`. Error Handling 9.2 |
| 9.3 | S17 strong/weak whole-word rule and fixed-token skipping, documented in `analytics/SCHEMA.md` |
| 9.4 | S14 + S17 + S24 together over shareable files, branch name, commit message, PR title and body |
| 10.1 | `ck stats submit` flow: export, diff, leak check, S23 confirmation, before any git mutation |
| 10.2 | S24 `branch_name`/`commit_message`/`pr_title`/`pr_body`. `submit_batch` (branch from `R/master`, push to `R`, `gh pr create --draft --repo kutlayhanli/claude-kiro --base master --head <branch>`). Prints URL |
| 10.3 | S22 `select_batch`. `Ledger.submitted` |
| 10.4 | S23 `ask` + decline path. Error Handling 10.4 |
| 10.5 | S23 (`skip`, `skip_withhold_notes`) + S16 |
| 10.6 | gh preflight + rollback in `submit_batch`. Error Handling 10.6 |
| 10.7 | S21 `decide_preflight` with `own_pending` |
| 10.8 | S22 empty. Error Handling 10.8 |
| 10.9 | S20 |
| 10.10 | S19 `restore`. Error Handling 10.10 |
| 10.11 | S23 with `has_free_text` from approved `.md` files (`ask` at a TTY, `skip_withhold_notes` without one). Error Handling 10.5 / 10.11 |
| 11.1 | `spec-implement.js` `rec.resolves` in `landTask`/`runTask`, `resolves` in `taskReport` |
| 11.2 | Only those lines change. `.claude/workflows/spec-implement.js` regenerated by `ck vendor` |
| 12.1 | S25 `health.decide` with S2 on the newest allowlisted run. Wired into `ck doctor` |
| 12.2 | S25 info finding |
| 12.3 | S25 uncollected count + suggestion |
| NFR Privacy by construction | S14 + S17 + `analytics/SCHEMA.md` |
| NFR No network except submit and the note drafter | Security: no network. The drafter is only used with `--note` interactively (7.12). Submit's `gh` is resolved on PATH, so tests use a fake |
| NFR Never blocks a run | 3.3 handling in `cli/run.py` and the skill steps |
| NFR Agreement with existing numbers | `bench.aggregate_usage` + `_price_by_model`, shared with `session_spend` |
| NFR Performance | Single scan per transcript. Thread pool of 8 for `--all` |
| NFR Local record protection | `store.write_private_json` (0600 / 0700), `agents.ensure_salt` (0600 with chmod), data dir denied in `sandbox_settings` |
| NFR Schema versioning | `SCHEMA_LOCAL`, `SCHEMA_SHARED`, tolerant readers |
| NFR Determinism | `json.dumps(sort_keys=True, indent=2)`. Only `record_id` is random. Builders are pure |
