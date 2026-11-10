export const meta = {
  name: 'spec-implement',
  description: 'Implement a spec: start each task as soon as its dependencies merge, gate-checked retry, finished tasks land in batches (one gate per batch, bisected when red), full gate every N merges and at the end',
  whenToUse: 'Invoked by /spec:implement <spec> with no task number',
}

// args (built by /spec:implement from `ck plan <spec> --json`):
// {
//   spec: 'market-sim',                    // spec directory name under specs/
//   root: '/abs/main/checkout',            // the main checkout `ck worktree` works from
//   worktreeDir: '/abs/main/checkout/.ck/worktrees',  // "worktreeDir" from ck plan: where this spec's task
//                                          //   worktrees live (specs/ck.json worktree_dir); relative paths resolve
//                                          //   against root; absent: <root>/.ck/worktrees
//   into: 'integrate/market-sim',          // branch task work is merged into (default flow: the integration branch)
//   targetDir: '/abs/checkout/of/into',    // checkout that has `into` checked out (integration worktree, or root)
//   wave: { '1': 1, ... },                 // display grouping only (from ck plan)
//   titles: { '1': '...' }, tracks: { '1': 'test' },
//   risk: { '1': 'safety' },               // from ck plan: "safety" (a **Risk:** safety tag or a risk_paths file) or "normal"
//                                          //   safety: starts at implementer.risky_model (or the top of escalate), always reviewed at reviewer.risky_model/risky_effort
//   complexity: { '1': 'high' },           // from ck plan: low | medium | high | null; with implementer.complexity_routing,
//                                          //   High starts at the first escalate model instead of the role's model
//   maxRetries: 1,                         // extra attempts per task after a failed gate or a no-op attempt
//   onFailure: 'continue' | 'halt',        // default continue: other ready tasks keep starting; a red target always halts
//   maxConcurrent: null,                   // cap on tasks with an implementing agent at work (null = none); review and merge queue don't count
//   fullGateEvery: 10,                     // run the full `ck gate <spec>` every N merges (0 = only at the end)
//   deps: { '1': [], '2': ['1'] },         // "deps" from ck plan: when present the script decides readiness itself
//   remaining: ['1', '2'],                 // "remaining" from ck plan (informational; the start plan is re-read)
//   agentSlots: 4,                         // runtime.agentSlots from ck agents --json (null/absent: nothing presumed queued)
//   finishFirst: true,                     // run.finish_first from ck agents --json (absent = true): hold new task starts
//                                          //   while a review, revision or landing waits for a runtime slot
//   mergeBatch: 4,                         // most tasks landed together (one `ck worktree land`, one gate); the window
//                                          //   halves after a red batch (min 1) and grows by 1 after a green one, up to this
//   agents: { implementer, test_writer, reviewer, fixer, orchestrator, resolver }  // "roles" from `ck agents --json`
//                                          //   orchestrator: plan, merge, gate and re-check steps; resolver: merge conflicts
//                                          //   ("inherit" = the session's). Absent: every agent inherits, no review.
//   model: null, effort: null,             // one-run override for every code-writing agent (task, retry, fix)
//   interactive: false,                    // true when I am at the session (ck handoff "interactive"): a task still guard-stopped after
//                                          //   the top of the guard ladder is listed in needsInput with action 'ask' and not parked
//   brief: '/abs/plugin/resources/spec-implement-brief.md',  // the task agents' brief (the plugin passes
//                                          //   ${CLAUDE_PLUGIN_ROOT}/resources/...); relative paths resolve against root;
//                                          //   absent: <root>/.claude/workflows/spec-implement-brief.md (vendored projects)
// }
//
// Scheduling is driven by the plan in tasks.md on the target branch, re-read
// (`ck plan`) every time a task finishes, so relinks and edited dependencies
// apply without restarting. A task starts as soon as all its Dependencies are
// Done on the target; waves are only used to group the progress display.
// Resuming: run /spec:implement again (a fresh run skips Done tasks); don't use
// resumeFromRunId, whose cached plan steps would be stale.
const A = args || {}
const SPEC = A.spec
const ROOT = A.root
const INTO = A.into || 'main'
const TARGET = A.targetDir || ROOT
const WTDIR = !A.worktreeDir
  ? `${ROOT}/.ck/worktrees`
  : String(A.worktreeDir).startsWith('/') ? String(A.worktreeDir).replace(/\/+$/, '') : `${ROOT}/${String(A.worktreeDir).replace(/^\.\//, '').replace(/\/+$/, '')}`
const WAVE = A.wave || {}
const TITLES = A.titles || {}
const TRACKS = A.tracks || {}
const RISK = A.risk || {}
const COMPLEXITY = A.complexity || {}
const MAX_RETRIES = Number.isInteger(A.maxRetries) ? A.maxRetries : 1
const CONTINUE = A.onFailure !== 'halt'
const MAX_CONCURRENT = A.maxConcurrent && A.maxConcurrent > 0 ? A.maxConcurrent : Infinity
const FULL_EVERY = Number.isInteger(A.fullGateEvery) ? A.fullGateEvery : 10
const MERGE_BATCH = Number.isInteger(A.mergeBatch) && A.mergeBatch > 0 ? A.mergeBatch : 4
const INTERACTIVE = A.interactive === true || A.interactive === 'true'
// Progress and ETA (run-followups): the skill runs `ck progress begin` and passes its run key and start line. Without
// a run key no prompt carries a progress flag and nothing else changes. No clock or timer code here.
const PROGRESS_RUN = typeof A.progressRun === 'string' && A.progressRun ? A.progressRun : null
const ETA_LINE = typeof A.etaLine === 'string' && A.etaLine ? A.etaLine : null
// runnerAgent: the lean agent type that runs thin ck calls under Claude Code (`spec:runner`, or `ck-runner` in a
// vendored project); null or absent means the default agent with a warning. runId defaults to the progress run key.
const RUNNER_AGENT = typeof A.runnerAgent === 'string' && A.runnerAgent ? A.runnerAgent : null
const RUN_ID = typeof A.runId === 'string' && A.runId ? A.runId : PROGRESS_RUN
const BRIEF = !A.brief
  ? `${ROOT}/.claude/workflows/spec-implement-brief.md`
  : String(A.brief).startsWith('/') ? String(A.brief) : `${ROOT}/${String(A.brief).replace(/^\.\//, '')}`

if (!SPEC || !ROOT) throw new Error('spec-implement needs args.spec and args.root')

// Scheduling args (from `ck plan --json` and `ck agents --json`). deps: the dependency map, so the script
// decides readiness itself; without it the run asks a plan agent on every freed slot (hand-built args).
// agentSlots: the runtime's agent slots (null = nothing presumed queued). finishFirst: absent means on.
const FINISH_FIRST = A.finishFirst == null ? true : A.finishFirst
if (typeof FINISH_FIRST !== 'boolean') throw new Error('spec-implement: finishFirst must be true or false')
const AGENT_SLOTS = A.agentSlots == null ? null : A.agentSlots
if (AGENT_SLOTS !== null && !(Number.isInteger(AGENT_SLOTS) && AGENT_SLOTS >= 1)) throw new Error('spec-implement: agentSlots must be a whole number of 1 or more')
const SCRIPT_READINESS = !!A.deps && typeof A.deps === 'object' && !Array.isArray(A.deps)

// Model and effort per role, from `ck agents --json` (args.agents). "inherit" or
// unset means the session's. args.model / args.effort override every code-writing
// agent for this run. Plan and merge steps run at low effort unless the
// orchestrator role sets one; a merge that hits a conflict goes to the resolver.
const ROLES = A.agents || {}
const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']
// On hosts that provide the ck() global (OpenCode), each base agent call also carries its role
// (`role` opt), so opencode.roles can map it to a model. Escalated ladder steps drop the tag
// (see escalated), and Claude Code, which has no ck() global, never sees a role opt.
const ROLE_TAGS = typeof ck === 'function'
const ROLE_NAMES = ['implementer', 'test_writer', 'reviewer', 'reviewer_risky', 'fixer', 'orchestrator', 'resolver']
function roleOpts(name, role, override, tag = name) {
  const model = (override && override.model) || (role && role.model)
  const effort = (override && override.effort) || (role && role.effort)
  const opts = {}
  if (model && model !== 'inherit') {
    if (!/^(sonnet|opus|haiku|fable)$|^claude-/.test(model)) throw new Error(`spec-implement: unknown model "${model}" for ${name} (use sonnet, opus, haiku, fable, or a claude-* ID)`)
    opts.model = model
  }
  if (effort && effort !== 'inherit') {
    if (!EFFORTS.includes(effort)) throw new Error(`spec-implement: unknown effort "${effort}" for ${name} (use ${EFFORTS.join(', ')})`)
    opts.effort = effort
  }
  if (ROLE_TAGS && ROLE_NAMES.includes(tag)) opts.role = tag
  return opts
}
const OVERRIDE = { model: A.model || null, effort: A.effort || null }
const IMPL = roleOpts('implementer', ROLES.implementer, OVERRIDE)
const TEST = roleOpts('test_writer', ROLES.test_writer || ROLES.implementer, OVERRIDE)
const FIX = roleOpts('fixer', ROLES.fixer || ROLES.implementer, OVERRIDE)
const REVIEWER = ROLES.reviewer || {}
const REVIEW_ON = !!A.agents && REVIEWER.enabled !== false
const REVIEW = roleOpts('reviewer', REVIEWER, null)
const EFFORT_RANK = e => EFFORTS.indexOf(e)
const REVIEW_ROUNDS = Number.isInteger(REVIEWER.rounds) ? REVIEWER.rounds : 1
// reviewer.cross_test: once every test-track task of a wave has finished, one read-only
// agent (the risky reviewer's settings) compares the merged test files with each other
// and with design.md. Report only: its findings go in the run's report.
const CROSS_TEST = !!A.agents && REVIEWER.cross_test === true
const ORCH = roleOpts('orchestrator', ROLES.orchestrator, null)
const LOW = { effort: 'low', ...ORCH }
const RESOLVE = roleOpts('resolver', ROLES.resolver, null)
// Escalation: bigger models to try, in order, once a role's own budget runs out
// (its retries for a failing gate, its review rounds for blocking findings, its
// one fix for a red branch). One extra attempt per model, at the role's effort.
// A model that is not above the role's own (e.g. sonnet for an opus fixer) is skipped.
const FAMILIES = ['haiku', 'sonnet', 'opus', 'fable']
const rank = m => FAMILIES.findIndex(f => String(m || '').includes(f))
function ladder(name, role, base) {
  const list = role && Array.isArray(role.escalate) ? role.escalate : []
  for (const model of list) roleOpts(`${name}.escalate`, { model }, null)  // validates
  return rank(base.model) < 0 ? list : list.filter(m => rank(m) > rank(base.model))
}
const followsImpl = role => (role && Array.isArray(role.escalate) ? role : ROLES.implementer)
const IMPL_UP = ladder('implementer', ROLES.implementer, IMPL)
const TEST_UP = ladder('test_writer', followsImpl(ROLES.test_writer), TEST)
const FIX_UP = ladder('fixer', followsImpl(ROLES.fixer), FIX)
const desc = o => `${o.model || 'inherited'}/${o.effort || 'inherited'}`
const up = list => (list.length ? ` (escalate ${list.join(' > ')})` : '')
const baseOpts = n => (TRACKS[n] === 'test' ? TEST : IMPL)
const baseLadder = n => (TRACKS[n] === 'test' ? TEST_UP : IMPL_UP)
// An escalated step runs the named model: it drops the role tag so the alias map (or the ck role map) resolves it.
const escalated = (opts, model) => {
  const out = { ...opts, model }
  delete out.role
  return out
}

// Risk and complexity routing. Escalation only fires on failures we detect, and
// the costly bugs (a send path or price tripwire that passes every test) are the
// ones nobody detects. So a safety task starts on the strong model and is always
// reviewed by the risky reviewer, and (with implementer.complexity_routing) a High
// complexity task skips the cheap model. A start never steps below the role's model;
// escalation then climbs only through models above the start. A one-run --model
// override wins for code-writing agents.
const IMPL_ROLE = ROLES.implementer || {}
const RISKY_START = IMPL_ROLE.risky_model ? roleOpts('implementer.risky_model', { model: IMPL_ROLE.risky_model }, null).model : null
const COMPLEXITY_ROUTING = IMPL_ROLE.complexity_routing === true
const isSafety = n => String(RISK[n] || '').toLowerCase() === 'safety'
const isHigh = n => String(COMPLEXITY[n] || '').toLowerCase() === 'high'
const above = (target, base) => !!target && (rank(base) < 0 || rank(target) > rank(base))
function startTarget(n) {
  if (OVERRIDE.model) return null
  const ladder = baseLadder(n)
  if (isSafety(n)) return RISKY_START || ladder[ladder.length - 1] || null
  if (COMPLEXITY_ROUTING && isHigh(n)) return ladder[0] || null
  return null
}
const codeOpts = n => {
  const base = baseOpts(n)
  const target = startTarget(n)
  return above(target, base.model) ? escalated(base, target) : base
}
const ladderOf = n => {
  const start = codeOpts(n).model
  return rank(start) < 0 ? baseLadder(n) : baseLadder(n).filter(m => rank(m) > rank(start))
}
// The risky reviewer: reviewer.risky_model/risky_effort, never below reviewer.model/effort.
const RISKY_REVIEW = (() => {
  const risky = roleOpts('reviewer.risky', { model: REVIEWER.risky_model || REVIEWER.model, effort: REVIEWER.risky_effort || REVIEWER.effort }, null, 'reviewer_risky')
  const opts = { ...REVIEW }
  if (ROLE_TAGS) opts.role = 'reviewer_risky'
  if (risky.model && (!REVIEW.model || rank(risky.model) >= rank(REVIEW.model))) opts.model = risky.model
  if (risky.effort && (!REVIEW.effort || EFFORT_RANK(risky.effort) >= EFFORT_RANK(REVIEW.effort))) opts.effort = risky.effort
  return opts
})()
const reviewOpts = n => (isSafety(n) ? RISKY_REVIEW : REVIEW)
if (A.agents || A.model || A.effort) {
  log(`Agents: impl ${desc(IMPL)}${up(IMPL_UP)}, test ${desc(TEST)}${up(TEST_UP)}, fix ${desc(FIX)}${up(FIX_UP)}, review ${REVIEW_ON ? `${desc(REVIEW)} (safety ${desc(RISKY_REVIEW)})` : 'off'}${COMPLEXITY_ROUTING ? ', complexity routing on' : ''}, orchestration ${desc(LOW)}, conflicts ${desc(RESOLVE)}`)
}

const WT = n => `${WTDIR}/${SPEC}-task-${n}`
const BR = n => `feat/${SPEC}-task-${n}`
const waveOf = n => WAVE[n] || 0
const phaseOf = n => (waveOf(n) ? `Wave ${waveOf(n)}` : 'Tasks')
const label = n => `task ${n}${TRACKS[n] === 'test' ? ' [test]' : ''}`
const NOW = 'date -u +%Y-%m-%dT%H:%M:%SZ'

// The one output schema of every thin ck call (19.3): the exit code and the command's JSON, as printed.
// A thin answer that does not match it is a failed call (thinResult).
const CK_CALL = {
  type: 'object',
  properties: {
    exitCode: { type: 'integer', description: 'the exit code of the ck command' },
    json: { type: ['object', 'null'], description: 'the JSON the command printed, copied exactly; null if it printed none' },
    error: { type: 'string', description: 'only if the command could not run or printed no JSON: what happened' },
  },
  required: ['exitCode', 'json'],
}
// One failure, as `ck gate --json` prints it (D3): the task agent copies ck's entries unchanged.
const FAILURE = {
  type: 'object',
  description: 'one entry of the "failures" array that ck gate --json prints, copied unchanged',
  properties: {
    node: { type: ['string', 'null'] },
    tcId: { type: ['string', 'null'] },
    tasks: { type: 'array', items: { type: 'string' } },
    attributedBy: { type: 'string' },
    file: { type: ['string', 'null'] },
    kind: { type: 'string', enum: ['assertion', 'import', 'collection', 'timeout', 'error'] },
    message: { type: 'string' },
    truncated: { type: 'boolean' },
    command: { type: ['string', 'null'] },
  },
}
// One test that can never pass with code that meets the spec: one entry per test.
const TEST_CONFLICT = {
  type: 'object',
  properties: { file: { type: 'string' }, test: { type: 'string' }, criterion: { type: 'string' }, reason: { type: 'string' }, fix: { type: 'string' } },
  required: ['file', 'reason'],
}
const RESULT = {
  type: 'object',
  properties: {
    task: { type: 'string' },
    status: { type: 'string', enum: ['done', 'in_progress', 'blocked'] },
    gatePassed: { type: 'boolean' },
    summary: { type: 'string' },
    commits: { type: 'string', description: 'hashes of commits you made; empty if none' },
    gateTail: { type: 'string' },
    worktree: {
      type: 'object',
      description: 'the entry for your task in the output of "ck worktree create": copy its state and detail',
      properties: { state: { type: 'string' }, detail: { type: 'string' } },
    },
    blocker: { type: 'string' },
    guardStop: {
      type: 'object',
      description: 'only when a guard denial or post-check message stopped you: the file, its kind, the test and the requirement, quoted',
      properties: { file: { type: 'string' }, kind: { type: 'string' }, test: { type: 'string' }, requirement: { type: 'string' } },
      required: ['file'],
    },
    deviations: { type: 'string' },
    wrongTests: { type: 'string' },
    testConflict: {
      type: 'object',
      description: 'only when a test your gate runs can never pass with code that meets requirements.md (the test contradicts a criterion, another test, or a schema rule the spec states): the test file, the test, the criterion it contradicts, why, and the correction the test track should make. Leave the test as it is.',
      properties: { file: { type: 'string' }, test: { type: 'string' }, criterion: { type: 'string' }, reason: { type: 'string' }, fix: { type: 'string' } },
      required: ['file', 'reason'],
    },
    testConflicts: {
      type: 'array',
      description: 'one entry per test that can never pass with code that meets requirements.md (file, test, criterion, reason, fix). Use this for more than one test; testConflict alone is also accepted.',
      items: TEST_CONFLICT,
    },
    failures: {
      type: 'array',
      description: 'the "failures" array of your last ck gate --json run, copied unchanged (every entry)',
      items: FAILURE,
    },
    outsideFiles: { type: 'string' },
    startedAt: { type: 'string', description: `output of "${NOW}" when you started` },
    finishedAt: { type: 'string', description: `output of "${NOW}" when you finished` },
  },
  required: ['task', 'status', 'gatePassed', 'summary', 'commits'],
}
const RESOLVED = {
  type: 'object',
  properties: {
    resolved: { type: 'boolean', description: 'the conflict is resolved and committed in the task worktree, and its task gate passes there' },
    conflicts: { type: 'string', description: 'the files that conflicted and how you resolved them' },
    error: { type: 'string', description: 'why it is not resolved, including a refused/blocked command' },
  },
  required: ['resolved'],
}

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['approve', 'changes'], description: 'changes only for blocking findings' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          task: { type: 'string', description: 'the task number this finding is about (required in a batched review-wave)' },
          severity: { type: 'string', enum: ['blocking', 'minor'] },
          file: { type: 'string' },
          issue: { type: 'string', description: 'what is wrong, and the spec text it contradicts' },
          fix: { type: 'string', description: 'what the implementer should change' },
        },
        required: ['severity', 'issue'],
      },
    },
    summary: { type: 'string' },
    tasks: { type: 'array', items: { type: 'string' }, description: 'the wave tasks this verdict covers (batched reviews echo their coverage)' },
  },
  required: ['verdict', 'findings', 'summary'],
}
const TESTFIX = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['fixed', 'test-correct', 'cannot-fix'], description: 'fixed: you changed the tests and committed; test-correct: the tests are right and the code is wrong; cannot-fix: you cannot repair them within the rules' },
    changedTests: { type: 'array', items: { type: 'object', properties: { file: { type: 'string' }, test: { type: 'string' } }, required: ['file'] } },
    commits: { type: 'string', description: 'hashes of commits you made; empty if none' },
    finding: { type: ['object', 'null'], description: 'test-correct only: what the blocked task\'s code gets wrong', properties: { file: { type: 'string' }, test: { type: 'string' }, issue: { type: 'string' } } },
    summary: { type: 'string' },
  },
  required: ['verdict', 'summary'],
}
const VERIFY = {
  type: 'object',
  properties: {
    green: { type: 'boolean' },
    failing: { type: 'string', description: 'failing command(s) and the relevant output tail' },
  },
  required: ['green'],
}

const ok = r => r && r.status === 'done' && r.gatePassed === true
// Issue #8: a task agent's structured claim that a test can never pass with correct code.
const testConflictOf = r => (r && !ok(r) ? testConflictsOf(r)[0] || null : null)
const trim = (v, n = 3000) => JSON.stringify(v || null).slice(0, n)
// Review findings go to the reviser and into the report whole: a finding cut
// mid-sentence leaves the reviser guessing what was wrong. The cap only guards
// against a runaway reviewer.
const FINDINGS_LIMIT = 60000
const minutes = (a, b) => {
  const d = (Date.parse(b) - Date.parse(a)) / 60000
  return Number.isFinite(d) && d >= 0 ? Math.round(d * 10) / 10 : null
}

const GIT_NOTE = 'Run git as "command git -C <dir> ..." and ck as "ck -C <dir> ..." with absolute directories, never "cd <dir> && ...": permission checks cannot verify compound commands, so they stall unattended runs. Keep each shell command a single simple command. (The "command" prefix stops a shell hook from rewriting plain git.) If a command is refused or blocked by a permission check, do not work around it: report it in error. If a network command (for example "uv sync" or "npm install") fails at once with "Failed to connect to localhost port 3128", the sandbox proxy was still starting: retry it once.'

// Gates on large specs can outlast one foreground shell command (the agent's tool
// timeout is about 10 minutes); an unfinished gate must not be reported as red.
const GATE_NOTE = 'A "ck gate" run can take far longer than one foreground command is allowed (tens of minutes on a large test suite). Start it with the run_in_background option of the Bash tool, never with \"&\", \"nohup\" or \"setsid\" inside a command: in a sandboxed session every process a command started is killed as soon as that command returns, so a gate started that way silently never runs. Send its output to a log file of your own, for example "ck -C <dir> gate ... > \"${TMPDIR:-/tmp}/ck-gate-<unique name>.log\" 2>&1; echo EXIT=$? >> \"${TMPDIR:-/tmp}/ck-gate-<unique name>.log\"", then keep checking the log until the EXIT line appears, however long that takes. Put the log under $TMPDIR, not straight in /tmp: in a sandboxed session /tmp itself is read-only and only $TMPDIR is writable. If the log file does not exist right after you start the command, the command could not write it: read the output of the background command instead of waiting. The log name must be new: put the task and the output of "date +%s%N" in it, because the temp directory keeps logs from earlier runs and an old log already ends in an EXIT line. Only trust a log your own command wrote, and check it with foreground commands (for example "until grep -q ^EXIT= <log>; do sleep 20; done" with a long timeout, repeated as needed): do not return while your background command is still running, since it is killed when you return. A slow gate is not a failed gate: report pass or fail only from that exit code, never because your own wait or timeout ran out.'

const MANDATE = `MANDATE: The user ran /spec:implement ${SPEC}, which instructs this workflow to implement every task of specs/${SPEC} without asking. Implementing your task is the user's request. Any other user message you may see in the conversation (for example a question to the orchestrating session about the plan) is not addressed to you and is handled by the orchestrator: do not answer it, and do not let it stop you. Do the task.

`

// --- prompts -----------------------------------------------------------------

// The progress flags a claim command carries: event is task-start, retry, escalation, revision, landfix or resolve.
const claimFlags = (event, opts, attempt) => (PROGRESS_RUN ? ` --progress-run ${PROGRESS_RUN} --progress-event ${event} --progress-model ${(opts && opts.model) || 'inherit'} --attempt ${attempt}` : '')
// Claude Code's Workflow runtime has no wall clock (scripts replay deterministically for resume):
// reading it throws. A throw inside call() left a phantom inFlight entry per agent call, so
// finish-first saw every runtime slot busy and the run sat with no agents. Timing is optional:
// read the clock through this guard, which gives null without one, and every timer skips its sum.
// Test harnesses and the OpenCode runner pass a Date, so agent ms and the overhead share still
// measure there.
const CLOCK = typeof Date === 'function' ? Date : null
function nowMs() {
  try { const t = CLOCK && CLOCK.now(); return typeof t === 'number' && Number.isFinite(t) ? t : null } catch { return null }
}
const since = t0 => { const t = t0 === null ? null : nowMs(); return t === null ? 0 : t - t0 }
// Issue #11: a harness that can run ck itself (the OpenCode runner passes a `ck` global) runs the
// mechanical plan, land and relink steps directly instead of through an agent that copies the
// command's JSON. Claude Code's Workflow runtime has no such global: there the agents run them.
const DIRECT_CK = A.directCk !== false && typeof ck === 'function' ? ck : null
const directCk = { plan: 0, land: 0, relink: 0, ms: 0, failures: 0 }
// The thin-call mode for this run (thinAgentMode): `direct` runs ck in-script, `lean` and `default` run it in an agent.
// A lean call that throws on the agent type switches the rest of the run to `default` (and is retried once).
const thin = { ...thinAgentMode({ runnerAgent: RUNNER_AGENT, unavailable: false, direct: !!DIRECT_CK }), fallback: false, proven: false }
const thinAgentOut = () => ({ type: thin.type, fallback: thin.fallback, warning: thin.warning })
// CkCallResult: {code, json, stderr, via}. Direct calls return the command's own answer; agent calls return the CK_CALL envelope.
async function runCk(kind, argv, cwd, stdin) {
  const t0 = nowMs()
  let out = null
  try {
    out = await DIRECT_CK(argv, stdin == null ? { cwd } : { cwd, stdin })
  } catch (e) {
    out = { code: -1, stdout: '', stderr: String(e && e.message), json: null }
  }
  directCk[kind] = (directCk[kind] || 0) + 1
  directCk.ms += since(t0)
  if (!out || !out.json || typeof out.json !== 'object') directCk.failures++
  return out ? { code: out.code, json: out.json, stderr: out.stderr || '', via: 'direct' } : { code: -1, json: null, stderr: 'no result', via: 'direct' }
}
// A thin answer is usable only if it carries an integer exit code and a JSON object (19.5); anything else is a failed call.
const thinAnswerOk = a => !!a && typeof a === 'object' && Number.isInteger(a.exitCode) && !!a.json && typeof a.json === 'object'
const thinResult = a => (thinAnswerOk(a)
  ? { code: a.exitCode, json: a.json, stderr: a.error || '', via: 'agent' }
  : { code: -1, json: null, stderr: `thin answer does not match CK_CALL: ${trim(a, 300)}`, via: 'agent' })
const shellWord = s => (/^[\w@%+=:,./-]+$/.test(s) ? s : `'${String(s).replace(/'/g, `'\\''`)}'`)
// One thin ck call. `argv` is ck's argument list; `what` says what the step is for. The agent prompt holds the ck command
// on its own line; `stdin` (the park note) is passed as a here-document in the prompt and as stdin when run directly.
async function thinCk(kind, argv, o = {}) {
  if (thin.mode === 'direct') return runCk(kind, argv, o.cwd || ROOT, o.stdin)
  const cmd = `ck ${argv.map(shellWord).join(' ')}`
  const doc = o.stdin == null ? cmd : `${cmd} <<'CK_PARK'\n${o.stdin}\nCK_PARK`
  const prompt = `${o.what || 'Mechanical step'}. Do exactly this, nothing else. ${GIT_NOTE}
Run this command on its own line${o.cwd ? ` (in ${o.cwd})` : ''}, with no other command:
${doc}
${o.gate ? `${GATE_NOTE}\n` : ''}A non-zero exit is an answer, not a failure: still return its exit code and JSON.
Return exitCode (the command's exit code) and json (its JSON output, copied exactly as printed; json must be an object). Put a message in error only if the command could not run or printed no JSON; no notes or remarks anywhere.`
  const opts = () => (thin.mode === 'lean'
    ? { label: o.label, phase: o.phase, schema: CK_CALL, agentType: thin.type }
    : { label: o.label, phase: o.phase, schema: CK_CALL, ...LOW })
  return call(kind, prompt, opts()).then(a => { thin.proven = true; return thinResult(a) }, e => {
    // 19.4: only the first thin call can show the agent type is unknown; once one has run, a throw is a failed call.
    if (thin.mode !== 'lean' || thin.proven) return thinResult({ error: e && e.message })
    thin.mode = 'default'
    thin.type = null
    thin.fallback = true
    thin.warning = `the lean thin agent type ${RUNNER_AGENT} is unavailable (${trim(e && e.message, 200)}); thin ck calls run on the default agent for the rest of the run`
    return call(kind, prompt, opts()).then(thinResult, e2 => thinResult({ error: e2 && e2.message }))
  })
}
// A revision that gives up may leave its claim, which would make the next run's task BUSY.
async function releaseClaim(n) {
  if (!DIRECT_CK) return
  try { await DIRECT_CK(['-C', WT(n), 'worktree', 'release', SPEC, String(n)], { cwd: WT(n) }) } catch {}
}
const directCkOut = () => (DIRECT_CK ? { ...directCk, minutes: Math.round(directCk.ms / 600) / 100 } : null)
const ckError = out => `ck exited ${out && out.code} with no JSON: ${trim((out && (out.stderr || out.stdout)) || '', 400)}`
const planArgv = exclude => {
  const argv = ['-C', TARGET, 'plan', SPEC, '--json']
  if (exclude.length) argv.push('--exclude', exclude.join(','))
  if (PROGRESS_RUN) {
    argv.push('--progress-run', PROGRESS_RUN)
    if (failed.size) argv.push('--failed', [...failed.keys()].join(','))
  }
  return argv
}
const landArgv = tasks => ['-C', ROOT, 'worktree', 'land', SPEC, ...tasks, '--into', INTO, '--gate', '--install', '--release', ...(PROGRESS_RUN ? ['--progress-run', PROGRESS_RUN] : []), '--json']


// A note the agent leaves for whoever picks the task up next (a retry, a revision,
// or a bigger model after escalation). It sits next to the worktree, not in it, so
// it never dirties the branch or gets merged.
const HANDOFF = n => `${WT(n)}.handoff.md`
const WRITE_HANDOFF = n => `If you finish without a passing gate, or with a reviewer's blocking finding unresolved, first write a handoff note to ${HANDOFF(n)} for the next agent (possibly a bigger model): what the task needs, what you tried and why it failed (exact error text), the state of the branch, and what you would do next. Overwrite any older note.`
const READ_HANDOFF = n => `Before anything else, read ${HANDOFF(n)} if it exists: the previous agent's handoff note.`

// A guard stop: a hook denied a change to a test, or the post-check caught one. The ladder (guardLadder) retries
// the task on each higher model once; unresolved after the top, the task is parked (or asked about, if interactive).
// Task 80 (3.6, 3.9): the gate runs whole (every Verify command, no stop at the first failure) and reports JSON, so
// the failures reach the retry, the test-fix loop and the report in one format.
const FAILURES_NOTE = n => `Run the gate as this command, on its own line:
ck -C ${WT(n)} gate ${SPEC} --task ${n} --json
and let the whole Verify run, without stopping at the first failure. Return ck's "failures" array unchanged in failures (every entry, as printed), and one testConflicts entry per test that can never pass.`
// The previous attempt's failures for a retry (3.8): the first 50 in order as JSON, and per-file counts of the rest.
const failuresBlock = prev => {
  const f = failuresForPrompt(prev && prev.failures)
  return `Its gate failures, as JSON ("shown" holds the first ${f.shown.length} of ${f.total} in order; "more" counts the rest per file): ${JSON.stringify(f)}`
}
const GUARD_NOTE = 'If a hook denies a command or edit with a "[ck guard]" message, or a "[ck post-check]" message says a test was changed, weakened or deleted: keep the test as it is, or restore it from the base branch if it was already changed, and never work around the denial (another tool, another path, a different command). Never run "ck guard". Stop, leave the task In Progress, and return status blocked with guardStop {file, kind, test, requirement}: the file, its kind (test, requirements, test_plan or other), the test quoted exactly as the denial names it, and the requirement it verifies quoted from specs. A bigger model will then try the task, or I will decide.'
// My answer to a parked guard conflict, kept by `ck worktree resolve`. Every attempt reads it before anything else.
const DECISION = n => `${WT(n)}.decision.md`
const READ_DECISION = n => `Before anything else, read ${DECISION(n)} if it exists: my decision on this task's guard conflict. Follow it.\n`

const taskPrompt = (n, cf = '') => `${MANDATE}${READ_DECISION(n)}Your task: Task ${n} of spec ${SPEC}${TITLES[n] ? ` ("${TITLES[n]}")` : ''}.
Worktree: ${WT(n)} (branch ${BR(n)}). Target branch for the final merge: ${INTO}.
First run "${NOW}" and keep the output as startedAt. Then read your operating brief at ${BRIEF} and follow it exactly: create and claim the worktree, implement per your track, get "ck -C ${WT(n)} gate ${SPEC} --task ${n}" passing, merge ${INTO} into your branch before finishing, and release the worktree.
Create your worktree first. Run this command on its own line; its output is a JSON array, and your task's entry has a "state" and a "detail":
ck -C ${ROOT} worktree create ${SPEC} ${n} --install --base ${INTO} --json
Copy that entry's state and detail into the "worktree" field of your answer. If the state is BUSY or ERROR, stop immediately: do not claim, do not touch ${WT(n)}, and return your answer with status blocked, gatePassed false and the worktree field. For BUSY put "worktree busy: <detail>" in blocker; for ERROR put "worktree create failed: <detail>".
Otherwise (CREATED or EXISTS) claim it, on its own line:
ck -C ${WT(n)} worktree claim ${SPEC} ${n}${cf}
${GIT_NOTE}
${GATE_NOTE}
${FAILURES_NOTE(n)}
${WRITE_HANDOFF(n)}
${GUARD_NOTE}
At the end, run "${NOW}" again for finishedAt. Return the schema fields with task "${n}". gatePassed is true only if your final "ck -C ${WT(n)} gate ${SPEC} --task ${n}" run exited 0. commits lists the commits you made (empty if none).`

const confirmConflictPrompt = (n, prev, attempt, cf = '') => `${MANDATE}${READ_DECISION(n)}Continue Task ${n} of spec ${SPEC} (attempt ${attempt + 1}). The previous agent in ${WT(n)} (branch ${BR(n)}) stopped because it believes these tests can never pass with code that meets the spec (every entry, one per test):
${JSON.stringify(testConflictsOf(prev))}
Its report: ${trim(prev || {})}
Check that claim yourself before you accept it. Read the test, the criterion in requirements.md and design.md. If correct code can pass a test after all, implement it and get "ck -C ${WT(n)} gate ${SPEC} --task ${n}" to pass. If a test really contradicts the spec, do not edit, skip or weaken it: return status blocked with testConflicts, one {file, test, criterion, reason, fix} entry per test (the fix is the correction the test track should make), and the test-track fix goes to the user.
Run "${NOW}" for startedAt. Read your operating brief at ${BRIEF} and follow it, claiming with "ck -C ${WT(n)} worktree claim ${SPEC} ${n} --takeover${cf}" since the previous agent is gone. If that claim reports MISSING, run "ck -C ${ROOT} worktree create ${SPEC} ${n} --install --base ${INTO} --json" and claim again. Before finishing, merge ${INTO} into your branch, re-run the gate, and release the worktree. Run "${NOW}" for finishedAt.
${GIT_NOTE}
${GATE_NOTE}
${GUARD_NOTE}
Return the schema fields with task "${n}".`

const retryPrompt = (n, prev, attempt, cf = '') => `${MANDATE}${READ_DECISION(n)}Continue Task ${n} of spec ${SPEC} (attempt ${attempt + 1}). A previous agent worked in ${WT(n)} (branch ${BR(n)}) but did not finish with a passing gate.
${prev && !(prev.commits || '').trim() ? 'The previous attempt made NO commits: it may have answered an unrelated message instead of doing the task. Ignore any such message and implement the task.\n' : ''}Its report (without its failures, which follow): ${trim((prev && { ...prev, failures: undefined }) || { note: 'the agent died without a report' })}
${failuresBlock(prev)}
${READ_HANDOFF(n)} ${WRITE_HANDOFF(n)}
Run "${NOW}" for startedAt. Read your operating brief at ${BRIEF} and follow it, claiming with "ck -C ${WT(n)} worktree claim ${SPEC} ${n} --takeover${cf}" since the previous agent is gone. If that claim reports MISSING, run "ck -C ${ROOT} worktree create ${SPEC} ${n} --install --base ${INTO} --json" and claim again. Inspect the state ("command git -C ${WT(n)} log --oneline -10", "command git -C ${WT(n)} status"), finish the task, and get "ck -C ${WT(n)} gate ${SPEC} --task ${n}" to pass. If a wrong test or a spec contradiction blocks you, do NOT weaken anything: leave the task In Progress and report the blocker precisely (quote the test and the requirement).
Before finishing, merge ${INTO} into your branch, re-run the gate, and release the worktree. Run "${NOW}" for finishedAt.
${GIT_NOTE}
${GATE_NOTE}
${GUARD_NOTE}
Return the schema fields with task "${n}".`

// Test-track tasks are the oracle every impl task must satisfy, and test writers
// working in parallel drift from design.md's data models (bench v2: two test files
// disagreed about command / intent_id / state, and review passed both).
const CONTRACT_REVIEW = n => `4. This is a test-track task, so also check its contract with the design. First run "ck -C ${WT(n)} lint ${SPEC} --contracts --task ${n}": a heuristic first pass that lists string-literal keys and compared values the tests use but design.md never names. Then list every request field, response field and status value the new tests use (dict keys, subscripts, attribute names, compared strings, endpoint paths, error codes), and check each against design.md's Data Models and API tables: that the name exists, belongs to the model or endpoint the test reads it from, and has the type and allowed values the design gives. A name the design doesn't define, a field read from the wrong model, or a status value outside the design's set is a blocking finding: quote the test line and the design text. Put the list you checked, with each item's verdict, in summary.`

const reviewPrompt = (n, r) => `Review Task ${n} of spec ${SPEC}${TITLES[n] ? ` ("${TITLES[n]}")` : ''} before it merges. You review; you do not edit, commit, or run the test suite.
Worktree: ${WT(n)} (branch ${BR(n)}), to merge into ${INTO}. ${GIT_NOTE}
1. Read the change: command git -C ${WT(n)} diff ${INTO}...${BR(n)}  (and its file list with --stat).
2. Read Task ${n}'s block in ${WT(n)}/specs/${SPEC}/tasks.md, the requirements it covers in requirements.md, and the parts of design.md it implements (interfaces, file paths, error handling).
3. The task's gate already passed, so do not re-check what the tests check. Look for what tests miss: behaviour that contradicts design.md or a requirement, a public interface that differs from design.md, missing error handling the design specifies, edits outside the task's files, weakened or skipped tests, and security problems.
${TRACKS[n] === 'test' ? `${CONTRACT_REVIEW(n)}\n` : ''}The implementer reported: ${trim(r && { summary: r.summary, deviations: r.deviations, outsideFiles: r.outsideFiles }, 2000)}
Mark a finding "blocking" only if it should stop the merge; style and taste are "minor". verdict is "changes" only if there is at least one blocking finding.`

// Wave-batched review (4.1): one agent over the combined diff of the wave's
// non-risky tasks. Names the wave, its tasks, the per-task diffs (and the
// combined `git diff <merge-base>...<INTO>` range over their files), each
// task's block/requirements, and design.md. The verdict echoes its coverage
// in `tasks` and flags individual tasks via per-task findings.
const waveReviewPrompt = (wave, tasks) => `Review Wave ${wave} of spec ${SPEC} before it merges: non-risky Tasks ${tasks.join(', ')} are about to land together into ${INTO}. You review; you do not edit, commit, or run the test suite.
${GIT_NOTE}
1. Read the combined change: ${tasks.map(n => `command git -C ${WT(n)} diff ${INTO}...${BR(n)}`).join('; ')} (and each file list with --stat), or the combined range \`git diff <merge-base>...${INTO}\` over their files.
2. Read each task's block in specs/${SPEC}/tasks.md, the requirements it covers in requirements.md, and design.md (interfaces, file paths, error handling).
3. The tasks' gates already passed, so do not re-check what the tests check. Look for what tests miss: behaviour that contradicts design.md or a requirement, a public interface that differs from design.md, missing error handling the design specifies, edits outside the tasks' files, weakened or skipped tests, and security problems.
${tasks.map(n => TRACKS[n] === 'test' ? `${CONTRACT_REVIEW(n)}\n` : '').join('')}Return verdict ("approve" or "changes") with per-task findings [{ task, severity, file, issue, fix }] and name every covered task in "tasks". Mark a finding "blocking" only if it should stop the wave; style and taste are "minor". verdict is "changes" only if there is at least one blocking finding.`

const crossTestPrompt = tasks => `Cross-test consistency check for spec ${SPEC}: test-track Tasks ${tasks.join(', ')} have merged into ${INTO}. You review; you do not edit, commit, or run the test suite. ${GIT_NOTE}
Test tasks written in parallel drift apart: two test files can each look right on their own and still disagree about the same request, response or record (field names, which model a field lives on, status values, error codes, units). Each implementation can then satisfy only one of them.
1. First pass: run "ck -C ${TARGET} lint ${SPEC} --contracts --json" and read its "unknown" list (names the tests use that design.md never defines; a heuristic, so judge each one).
2. In ${TARGET}, read the test files of Tasks ${tasks.join(', ')} (their **Files:** in specs/${SPEC}/tasks.md), the spec's other test files that exercise the same models or endpoints, and design.md's Data Models and API tables.
3. For each model, endpoint and status set those tests touch, compare how every test file uses it, with each other and with design.md.
Report each disagreement with both files and lines and the design text. It is "blocking" when two tests (or a test and design.md) cannot both be satisfied by one implementation; otherwise "minor". verdict is "changes" only if there is at least one blocking finding. In summary, list the models and endpoints you compared.`

const revisePrompt = (n, review, round, cf = '') => `${MANDATE}${READ_DECISION(n)}Revise Task ${n} of spec ${SPEC} (review round ${round}). The task's gate passed, but the reviewer found blocking problems:
${trim(review && review.findings, FINDINGS_LIMIT)}
${READ_HANDOFF(n)} ${WRITE_HANDOFF(n)}
Worktree: ${WT(n)} (branch ${BR(n)}). Run "${NOW}" for startedAt. Claim with "ck -C ${WT(n)} worktree claim ${SPEC} ${n} --takeover${cf}", fix each blocking finding without weakening any test or editing requirements.md or existing test-plan.md rows, get "ck -C ${WT(n)} gate ${SPEC} --task ${n}" passing again, merge ${INTO} into your branch, re-run the gate, and release the worktree. Release it even if you stop without finishing, so the next run can claim it. If a finding is wrong (it contradicts the spec), do not change the code for it: say why in deviations. Run "${NOW}" for finishedAt.
${GIT_NOTE}
${GATE_NOTE}
${GUARD_NOTE}
Return the schema fields with task "${n}".`

// Parking: one `ck worktree park` call. The note carries the quotes; the notification never does.
// Landing is one ck command: it prechecks with git merge-tree, merges the batch,
// gates it in-process, and bisects a red batch by itself. The agent only runs it.
const resolvePrompt = (n, files, cf = '') => `Merge conflict for Task ${n} of spec ${SPEC}: ${BR(n)} conflicts with ${INTO}${files ? ` (${files})` : ''}. ${INTO} was not touched. ${GIT_NOTE} ${GATE_NOTE}
1. Resolve it in the TASK WORKTREE, never in ${TARGET}:
     ck -C ${WT(n)} worktree claim ${SPEC} ${n} --takeover${cf}
     command git -C ${WT(n)} merge ${INTO}
   Resolve each conflicted file keeping both sides' intent: tasks.md, keep both sides' task sections; dependency manifests, take the union and re-lock (e.g. "uv lock"); test files, keep every test and assertion from both sides and never weaken one. Read the spec (specs/${SPEC}/design.md, requirements.md) when the two sides disagree about behaviour.
2. In the worktree, "ck -C ${WT(n)} gate ${SPEC} --task ${n}" must pass. Commit the merge, then run "ck -C ${WT(n)} worktree release ${SPEC} ${n}".
3. Do NOT merge into ${INTO} yourself: the workflow lands the task again.
Return resolved, conflicts (the files and how you resolved them), and error if it is not resolved.`

const landFixPrompt = (k, n, tail, cf = '') => `${MANDATE}Task ${n} of spec ${SPEC} passed its own gate, but landing it on ${INTO} turns the gate red, so it was NOT merged (${INTO} stays green without it). Failure: ${trim(tail, 4000)}
${READ_HANDOFF(n)} ${WRITE_HANDOFF(n)}
Fix it without weakening any test and without editing requirements.md or existing test-plan.md rows. ${GIT_NOTE} ${GATE_NOTE}
1. Work only in the task worktree ${WT(n)} (branch ${BR(n)}). Claim it: ck -C ${WT(n)} worktree claim ${SPEC} ${n} --takeover${cf}
2. Bring in the target: command git -C ${WT(n)} merge ${INTO}  (resolve any conflict keeping both sides' tests).
3. Reproduce the failing check in the worktree, fix the code (or a clearly broken fixture or harness), commit with a message starting "fix:", and get "ck -C ${WT(n)} gate ${SPEC} --task ${n}" passing.
4. Release: ck -C ${WT(n)} worktree release ${SPEC} ${n}. Do NOT merge into ${INTO}: the workflow lands the task again (fix number ${k}).
If the only way to green is to weaken a test or change a requirement, stop and explain precisely instead.
Return green=true only if the task gate passes in ${WT(n)} after your commit, else failing with the reason.`

const fixPrompt = (k, what, failure) => `${INTO} is red (${what}) for spec ${SPEC}. Failure: ${JSON.stringify(failure || null)}
Fix it without weakening any test and without editing requirements.md or existing test-plan.md rows. ${GIT_NOTE} ${GATE_NOTE}
1. Run: ck -C ${ROOT} worktree create ${SPEC} fix${k} --install --base ${INTO}   (worktree ${WT(`fix${k}`)}). Work only there.
2. Diagnose with the failing check, fix the code (or a clearly broken fixture or harness), and confirm the failing check passes in the fix worktree.
3. Commit with a message starting "fix:", then run: ck -C ${ROOT} worktree merge ${SPEC} fix${k} --into ${INTO}
If the only way to green is to weaken a test or change a requirement, stop and explain precisely instead.
Return green=true only if ${INTO} is green after your merge, else failing with the reason.`


// Task 86 (4.1): the test-fixer's brief. It is the test writer, started on a testfix<k> branch from INTO.
const testFixPrompt = (k, n, entries) => `${MANDATE}You are a test-fixer for spec ${SPEC}. ${n != null ? `Task ${n} is blocked because` : 'A cross-test check found that'} these tests can never pass with code that meets the spec (every entry, one per test; the "fix" is the correction a first agent proposed):
${JSON.stringify(entries)}
Work on branch ${BR(`testfix${k}`)} in worktree ${WT(`testfix${k}`)}. Create it from the integration branch, on its own line:
ck -C ${ROOT} worktree create ${SPEC} testfix${k} --install --base ${INTO} --json
${GIT_NOTE}
Read each test, the criterion in requirements.md and design.md. Then decide, for all of the tests together:
- the tests are wrong: change only those tests (and fixtures they need) the smallest way that makes them demand exactly what the criterion demands. Do not loosen a test beyond its criterion, do not weaken or delete one, do not edit source code, requirements.md or existing test-plan.md rows. Commit on the branch with a message starting "test-fix:". Do not merge or land it. Return verdict "fixed", changedTests [{file, test}] and commits.
- the tests are right and the code a task would write is what is wrong: change nothing. Return verdict "test-correct" and finding {file, test, issue} saying exactly what the code gets wrong.
- you cannot repair them within these rules (for example a guard denies it): return verdict "cannot-fix" and say why in summary.
Return the schema fields.`

// Task 86 (4.3): the risky-tier review of a proved fix against the test-plan rows and the guard checklist.
const testFixReviewPrompt = (k, entries, changed, scope) => {
  const ids = [...new Set([...entries, ...changed].map(x => `${x.file || ''} ${x.test || ''}`).join(' ').match(/tc[-_]?\d+[a-z]?/gi) || [])].map(t => `TC-${t.replace(/^tc[-_]?/i, '')}`)
  return `Review the test fix testfix${k} for spec ${SPEC} before it lands on ${INTO}. You review; you do not edit, commit, or run the test suite. ${GIT_NOTE}
1. Read the diff: command git -C ${ROOT} diff ${INTO}...${BR(`testfix${k}`)}  (and its file list with --stat). It must change only the tests named below and what they need.
2. The fix repairs these tests, which could never pass (every entry): ${JSON.stringify(entries)}
3. Read the rows of ${WT(`testfix${k}`)}/specs/${SPEC}/test-plan.md for ${ids.length ? ids.join(', ') : 'each changed test (find its TC ID by name)'} and the criteria they verify in requirements.md. Check the changed tests still demand everything their rows say and the criterion says.
4. Guard checklist, each a blocking finding if it fails: no test is weakened (a looser tolerance than the criterion allows, a removed or softened assertion, a skip or xfail, a deleted test); requirements.md is not edited; no existing test-plan.md row is reworded or deleted; no source file changes.
The scope check found: ${trim(scope || {}, 800)}
Mark a finding "blocking" only if the fix must not land. verdict is "changes" only if there is at least one blocking finding; name file and test in each finding.`
}

// Task 86: the retry prompt of a blocked task after its tests were fixed or found correct.
const afterFixNote = (fixedTests, findings) => `${fixedTests.length ? `The tests ${fixedTests.join(', ')} were fixed and landed on ${INTO}: merge ${INTO} into your branch first, then finish the task.\n` : ''}${findings.length ? `A test-fixer found the tests correct and the code wrong: ${JSON.stringify(findings)}\n` : ''}`

// --- decisions (pure; loaded by tests) ---
// Every scheduling rule that allows, refuses or holds something. No script globals, no agent/log/args,
// no clock and no I/O: tests/js/load_decisions.mjs cuts this block out and evaluates it on its own.

const decisionIds = v => [...(v || [])].map(x => String(x && typeof x === 'object' ? x.task : x))
const hasKey = (o, k) => !!o && Object.prototype.hasOwnProperty.call(o, k)

// Task 80 (3.7): every test conflict a result carries. `testConflicts` is a list; the older single `testConflict`
// is read as a list of one. An entry without a file is not a conflict; each (file, test) is kept once, first wins.
function testConflictsOf(result) {
  const r = result && typeof result === 'object' ? result : {}
  const single = r.testConflict && typeof r.testConflict === 'object' ? [r.testConflict] : []
  const many = Array.isArray(r.testConflicts) ? r.testConflicts : []
  const seen = new Set()
  const out = []
  for (const e of [...single, ...many]) {
    if (!e || typeof e !== 'object' || !e.file) continue
    const k = JSON.stringify([e.file, e.test || null])
    if (seen.has(k)) continue
    seen.add(k)
    out.push(e)
  }
  return out
}

// Task 80 (3.8): a failure list for a prompt: the first `limit` entries in order, and how many of the rest each file has.
function failuresForPrompt(failures, limit = 50) {
  const list = Array.isArray(failures) ? failures : []
  const more = {}
  for (const f of list.slice(limit)) {
    const file = f && f.file ? f.file : ''
    more[file] = Object.prototype.hasOwnProperty.call(more, file) ? more[file] + 1 : 1
  }
  return { shown: list.slice(0, limit), more, total: list.length }
}

// Port of cli/worktree.py::_chain_lengths: for each open task, the number of open tasks on its longest
// chain of dependents, itself included. Tasks are visited in `order` (tasks.md order, as ck plan's deps
// keys are), never in JS object-key order, so memoization, ties and cycles match ck plan.
function chainLengths(order, deps, done) {
  deps = deps || {}
  const doneSet = new Set(decisionIds(done))
  const keys = []
  const seen = new Set()
  for (const n of [...decisionIds(order), ...Object.keys(deps)]) {
    if (hasKey(deps, n) && !seen.has(n)) { seen.add(n); keys.push(n) }
  }
  const dependents = new Map(keys.map(n => [n, []]))
  for (const n of keys) {
    for (const d of decisionIds(deps[n])) {
      if (dependents.has(d) && !doneSet.has(n)) dependents.get(d).push(n)
    }
  }
  const lengths = new Map()
  const length = (n, stack) => {
    if (!lengths.has(n)) {
      const below = dependents.get(n).filter(m => !stack.has(m)).map(m => length(m, new Set([...stack, m])))
      lengths.set(n, 1 + Math.max(0, ...below))
    }
    return lengths.get(n)
  }
  const out = {}
  for (const n of keys) if (!doneSet.has(n)) out[n] = length(n, new Set([n]))
  return out
}

// Which tasks may start, in ck plan's order: longest open chain first, then numeric id, stable over `order`.
// A dependency is met only if it is a key of `deps` AND Done: a dependency the script has no entry for is
// never met, even when `done` lists it (never start on a guess, 1.10).
function readyTasks({ order, deps, done, exclude, blocked } = {}) {
  deps = deps || {}
  const doneSet = new Set(decisionIds(done))
  const skip = new Set([...decisionIds(exclude), ...decisionIds(blocked)])
  const met = d => hasKey(deps, d) && doneSet.has(d)
  const seen = new Set()
  const ready = []
  for (const n of decisionIds(order)) {
    if (seen.has(n)) continue
    seen.add(n)
    if (!hasKey(deps, n) || doneSet.has(n) || skip.has(n)) continue
    if (decisionIds(deps[n]).every(met)) ready.push(n)
  }
  const chain = chainLengths(order, deps, [...doneSet])
  const num = n => (/^[0-9]+$/.test(n) ? Number(n) : 0)
  const rank = new Map(ready.map((n, i) => [n, i]))
  return ready.slice().sort((a, b) =>
    ((chain[b] ?? 1) - (chain[a] ?? 1)) || (num(a) - num(b)) || (rank.get(a) - rank.get(b)))
}

const blockedOf = view => [
  ...decisionIds(view && view.verifyCycles),
  ...((view && view.cycles) || []).flatMap(c => decisionIds(c)),
]

// The scheduler's view after a plan returns. Merges never un-happen: Done = plan.done ∪ mergedInRun (1.6).
function applyPlanView(plan, mergedInRun) {
  return {
    order: decisionIds(plan.remaining),
    deps: plan.deps || {},
    done: new Set([...decisionIds(plan.done), ...decisionIds(mergedInRun)]),
    cycles: (plan.cycles || []).map(c => decisionIds(c)),
    verifyCycles: [...(plan.verifyCycles || [])],
  }
}

// Whether a plan agent's copy holds together: our readiness over its view equals its ready list (minus
// tasks the stricter 1.10 rule holds back), and every remaining task has a deps entry (1.6, 1.8).
function planConsistent(plan, exclude) {
  if (!plan || typeof plan !== 'object') return false
  const deps = plan.deps
  if (!deps || typeof deps !== 'object' || Array.isArray(deps)) return false
  if (![plan.remaining, plan.done, plan.ready].every(Array.isArray)) return false
  const remaining = decisionIds(plan.remaining)
  if (!remaining.every(n => hasKey(deps, n))) return false
  const ours = readyTasks({ order: remaining, deps, done: plan.done, exclude: exclude || [], blocked: blockedOf(plan) })
  const theirs = decisionIds(plan.ready).filter(n => decisionIds(deps[n]).every(d => hasKey(deps, d)))
  return ours.length === theirs.length && ours.every((n, i) => n === theirs[i])
}

// Whether a plan agent runs for this trigger. Only an eligible trigger coalesces while one is in flight.
function planDecision({ trigger, scriptReadiness, planInFlight, idleArmed, freeSlot, readyCount, openCount, stopLaunching } = {}) {
  if (stopLaunching && trigger !== 'start') return 'none'
  let eligible
  if (trigger === 'start' || trigger === 'landing') eligible = true
  else if (trigger === 'idle') eligible = !!freeSlot && readyCount === 0 && openCount > 0 && !!idleArmed
  else if (trigger === 'freed' || trigger === 'finished') eligible = !scriptReadiness
  else eligible = false
  if (!eligible) return 'none'
  return planInFlight ? 'coalesce' : 'run'
}

const isSlotCount = s => Number.isInteger(s) && s > 0

// In-flight agents presumed queued: those after the first `slots` in request order. None without slots.
function queuedAgents(inFlight, slots) {
  if (!isSlotCount(slots)) return []
  return [...(inFlight || [])].sort((a, b) => a.seq - b.seq).slice(slots)
}

const UNBLOCKER_KINDS = ['review', 'revise', 'land']

// Which ready tasks start now (3.1, 3.3, 3.5, 3.9). Asked only about new task starts.
function startDecision({ finishFirst, inFlight, slots, ready, working, maxConcurrent, stopLaunching } = {}) {
  const none = (deferred, waiting) => ({ start: [], deferred, waiting })
  if (stopLaunching) return none(false, 0)
  const list = [...(ready || [])]
  const flight = inFlight || []
  if (finishFirst) {
    const waiting = queuedAgents(flight, slots).filter(e => UNBLOCKER_KINDS.includes(e && e.kind)).length
    if (waiting > 0) return none(true, waiting)
  }
  const cap = typeof maxConcurrent === 'number' && Number.isFinite(maxConcurrent) ? maxConcurrent : Infinity
  const allowed = Math.max(0, cap - (working || 0))
  const free = finishFirst && isSlotCount(slots) ? Math.max(0, slots - flight.length) : Infinity
  const count = Math.min(allowed, free, list.length)
  return { start: list.slice(0, count), deferred: free < Math.min(allowed, list.length), waiting: 0 }
}

// How a task agent's worktree creation step ended (2.2, 2.3, 2.4).
function worktreeOutcome(result) {
  const proceed = { kind: 'proceed', reason: null }
  if (!result || typeof result !== 'object') return proceed
  const wt = result.worktree && typeof result.worktree === 'object' ? result.worktree : null
  const state = wt ? String(wt.state || '').toUpperCase() : ''
  const detail = wt && wt.detail != null ? String(wt.detail) : ''
  const blocker = typeof result.blocker === 'string' ? result.blocker : ''
  if (state === 'BUSY') return { kind: 'busy', reason: `worktree busy: ${detail}` }
  if (state !== 'ERROR' && /worktree busy/i.test(blocker)) {
    const fromBlocker = blocker.replace(/^\s*worktree busy\s*:?\s*/i, '') || blocker
    return { kind: 'busy', reason: `worktree busy: ${detail || fromBlocker}` }
  }
  if (state === 'ERROR') return { kind: 'error', reason: `worktree create failed: ${detail || blocker}` }
  return proceed
}

// The guard ladder (S17): the models of sonnet, opus, fable ranked above the attempted model, in order. An
// inherited or unknown model gives all three, fable gives none. A mirror of agents.guard_ladder; it ignores
// implementer.escalate.
function guardLadder(attemptedModel) {
  const ladder = ['sonnet', 'opus', 'fable']
  const families = ['haiku', 'sonnet', 'opus', 'fable']
  const text = typeof attemptedModel === 'string' ? attemptedModel.toLowerCase() : ''
  const family = families.find(f => text.includes(f))
  if (!family) return ladder
  return ladder.filter(m => families.indexOf(m) > families.indexOf(family))
}

// S18: the guard stop in a task agent's result, or null. A passing result is never a guard stop.
function guardStopOf(result) {
  if (!result || typeof result !== 'object') return null
  if (result.status === 'done' && result.gatePassed === true) return null
  const stop = result.guardStop
  if (!stop || typeof stop !== 'object' || Array.isArray(stop)) return null
  return typeof stop.file === 'string' && stop.file.trim() ? stop : null
}

// S19: what happens to a task still guard-stopped after the top of the ladder.
function parkAction({ interactive } = {}) {
  return interactive ? 'ask' : 'park'
}

// Warn-only overhead budget (5.4, D8, NFR-5): the plan/review/land share of agent
// ms above which the result/report warns without halting, failing or retrying.
const OVERHEAD_WARN_THRESHOLD = 0.5

// Warn-only overhead flag (5.4, D8, NFR-5): true when the plan/review/land
// share of agent ms exceeds the published threshold. Pure: callers only copy
// the flag into the result/report; it never halts, fails or retries the run.
function overheadWarning(share) {
  return share != null && share > OVERHEAD_WARN_THRESHOLD
}

// The D5 layered fallback source (1.2, 2.1-2.3): scriptReady minus excluded/done
// when hasDeps and non-empty yields source 'deps'; else lastGoodReady minus
// excluded/done when non-empty yields source 'last-good'; else empty/null for
// the halt path. Ordering is the input's (already ck-plan order); never re-sorted.
// Unknown-dep tasks never appear because scriptReady comes from readyTasks.
function fallbackReady({ hasDeps, scriptReady, lastGoodReady, exclude, done } = {}) {
  const skip = new Set([...decisionIds(exclude), ...decisionIds(done)])
  const clean = list => decisionIds(list).filter(n => !skip.has(n))
  if (hasDeps) {
    const fromDeps = clean(scriptReady)
    if (fromDeps.length > 0) return { ready: fromDeps, source: 'deps' }
  }
  const fromLastGood = clean(lastGoodReady)
  if (fromLastGood.length > 0) return { ready: fromLastGood, source: 'last-good' }
  return { ready: [], source: null }
}

// Wave review split (4.1-4.3, 4.6): individual = merged tasks whose risk tag is
// `safety` (case-insensitive, same check as isSafety); batched = the rest.
// Empty merged yields both empty (no review). Ordering is the input's.
function reviewGroups({ merged, risk } = {}) {
  const tags = risk && typeof risk === 'object' ? risk : {}
  const batched = []
  const individual = []
  for (const n of decisionIds(merged)) {
    if (String(tags[n] || '').toLowerCase() === 'safety') individual.push(n)
    else batched.push(n)
  }
  return { batched, individual }
}

// How thin ck calls run (19.1, 19.4, 19.6, 2.2, 2.3): `direct` when the host has the `ck` global (OpenCode:
// ck runs in-script), `lean` when the Claude Code run has a lean runner agent type, otherwise `default`
// (the session's agent) with a warning that the lean type is unavailable.
function thinAgentMode({ runnerAgent, unavailable, direct } = {}) {
  if (direct) return { mode: 'direct', type: null, warning: null }
  if (typeof runnerAgent === 'string' && runnerAgent && !unavailable) return { mode: 'lean', type: runnerAgent, warning: null }
  return { mode: 'default', type: null, warning: `the lean thin agent type ${runnerAgent || '(runnerAgent not set)'} is unavailable; thin ck calls run on the default agent` }
}

// The verdict of one `ck gate ... --attribute --json` call (Task 81). A thin answer and a direct answer both arrive as
// {code, json, stderr}; json is ck-gate/1 (full) or ck-gate-compact/1 (--compact). green: ck said ok, exit code 0, and
// nothing in the object contradicts it. red: regressions go to the fixer. error: no valid gate object of either shape
// or a self-contradicting one, so the gate never counts green (2.9, 19.5).
const isPlainObj = v => !!v && typeof v === 'object' && !Array.isArray(v)
// One failure entry must match the ck-gate/1 failure schema (design D3): required fields of the right type, a known status,
// and a node or a file to point at.
const FAILURE_KINDS = ['assertion', 'import', 'collection', 'timeout', 'error']
const FAILURE_STATUSES = ['regression', 'excused', 'flaky', 'unattributed']
const strOrNull = v => v === null || typeof v === 'string'
function failureProblem(f) {
  if (!isPlainObj(f)) return 'an entry that is not an object'
  for (const k of ['node', 'tcId', 'tasks', 'attributedBy', 'file', 'kind', 'message', 'truncated', 'command']) {
    if (!(k in f)) return `an entry without ${k}`
  }
  if (!strOrNull(f.node) || !strOrNull(f.file) || !strOrNull(f.command)) return 'an entry with a node, file or command of the wrong type'
  if (f.node === null && f.file === null) return 'an entry with neither node nor file'
  if (f.tcId !== null && !(typeof f.tcId === 'string' && /^TC-[0-9]+$/.test(f.tcId))) return 'an entry with a bad tcId'
  if (!Array.isArray(f.tasks) || !f.tasks.every(t => typeof t === 'string')) return 'an entry whose tasks are not strings'
  if (!['tc', 'file', 'command', 'none'].includes(f.attributedBy)) return 'an entry with a bad attributedBy'
  if (!FAILURE_KINDS.includes(f.kind)) return 'an entry with a bad kind'
  if (typeof f.message !== 'string' || f.message.length > 500) return 'an entry with a bad message'
  if (typeof f.truncated !== 'boolean') return 'an entry with a bad truncated flag'
  if (f.status !== undefined && !FAILURE_STATUSES.includes(f.status)) return 'an entry with an unknown status'
  if (f.excusedBy !== undefined && !(Array.isArray(f.excusedBy) && f.excusedBy.every(t => typeof t === 'string'))) return 'an entry with a bad excusedBy'
  return null
}
function gateOutcome(out) {
  const fail = error => ({ state: 'error', green: false, regressions: [], excusedByTask: {}, flaky: [], report: null, error, reason: error })
  if (!out || typeof out !== 'object') return fail('the gate call returned no result')
  const j = out.json
  if (!isPlainObj(j)) return fail(`ck gate printed no JSON object${out.stderr ? `: ${String(out.stderr).slice(0, 300)}` : ''}`)
  if (typeof j.ok !== 'boolean') return fail('the gate JSON has no ok flag')
  const report = typeof j.report === 'string' ? j.report : null
  let regressions
  let flaky
  let unattributed
  let excused
  const excusedByTask = {}
  if (j.schema === 'ck-gate/1') {
    if (!Array.isArray(j.failures)) return fail('ck-gate/1 JSON has no failures array')
    const entries = j.failures
    for (const f of entries) {
      const bad = failureProblem(f)
      if (bad) return fail(`ck-gate/1 failures hold ${bad}`)
      if (f.status === undefined) return fail('ck-gate/1 failures hold an entry without a status')
    }
    regressions = entries.filter(f => f.status === 'regression')
    unattributed = entries.filter(f => f.status === 'unattributed')
    flaky = entries.filter(f => f.status === 'flaky')
    const ex = entries.filter(f => f.status === 'excused')
    excused = ex.length
    for (const f of ex) {
      for (const t of Array.isArray(f.excusedBy) && f.excusedBy.length ? f.excusedBy : f.tasks) excusedByTask[t] = (excusedByTask[t] || 0) + 1
    }
  } else if (j.schema === 'ck-gate-compact/1') {
    if (!Array.isArray(j.regressions) || !Array.isArray(j.flaky) || !isPlainObj(j.excusedByTask)) {
      return fail('ck-gate-compact/1 JSON has a regressions, flaky or excusedByTask field of the wrong shape')
    }
    if (j.unattributed !== undefined && !Array.isArray(j.unattributed)) return fail('ck-gate-compact/1 unattributed is not an array')
    regressions = j.regressions
    flaky = j.flaky
    unattributed = j.unattributed || []
    for (const f of [...regressions, ...unattributed, ...flaky]) {
      const bad = failureProblem(f)
      if (bad) return fail(`ck-gate-compact/1 lists ${bad}`)
    }
    if (!Object.keys(j.excusedByTask).every(k => /^[0-9]+$/.test(k))) return fail('ck-gate-compact/1 excusedByTask has a key that is not a task number')
    if (!Object.values(j.excusedByTask).every(v => Number.isInteger(v) && v >= 0)) return fail('ck-gate-compact/1 excusedByTask holds a count that is not a non-negative integer')
    Object.assign(excusedByTask, j.excusedByTask)
    excused = Object.values(excusedByTask).reduce((n, v) => n + (Number.isInteger(v) ? v : 0), 0)
    if (Array.isArray(j.failedChecks) && j.ok && j.failedChecks.some(c => isPlainObj(c) && c.ok === false && !c.warning)) {
      return fail('ck-gate-compact/1 says ok but holds a failed check')
    }
  } else {
    return fail(`the gate JSON has an unknown schema ${JSON.stringify(j.schema)}`)
  }
  if (isPlainObj(j.summary)) {
    const want = { regressions: regressions.length, unattributed: unattributed.length, flaky: flaky.length, excused }
    for (const k of Object.keys(want)) {
      if (j.summary[k] === undefined) continue
      // The compact form sums one count per excusing task, while summary.excused counts failures: a failure excused by
      // two tasks gives 2 here and 1 there. So compact only requires the sum to be at least the summary count.
      if (k === 'excused' && j.schema === 'ck-gate-compact/1') {
        if (typeof j.summary[k] !== 'number' || want[k] < j.summary[k]) return fail(`the gate summary says ${k}=${j.summary[k]} but the excusedByTask counts hold ${want[k]}`)
        continue
      }
      if (j.summary[k] !== want[k]) return fail(`the gate summary says ${k}=${j.summary[k]} but the entries hold ${want[k]}`)
    }
  }
  if (j.ok && (regressions.length || unattributed.length)) return fail('the gate says ok but lists regressions')
  if (out.code !== (j.ok ? 0 : 1)) return fail(`the gate says ok=${j.ok} but ck exited ${out.code}`)
  // A red gate hands the fixer both regressions and unattributed failures.
  return { state: j.ok ? 'green' : 'red', green: j.ok === true, regressions: [...regressions, ...unattributed], excusedByTask, flaky, report, error: null, reason: null }
}

// Task 86 (4.8, 4.9): which conflicts a test-fixer may take. Each distinct (file, test) is kept once, first wins; one whose
// node is in `alreadyFixed` (ledger entries {node, at, fix}) goes to me; the rest are grouped by file, one group per file.
function testFixPlan({ conflicts, alreadyFixed } = {}) {
  const nodeOf = c => (c.test ? `${c.file}::${c.test}` : String(c.file))
  const fixed = new Set((Array.isArray(alreadyFixed) ? alreadyFixed : []).map(e => String(e && typeof e === 'object' ? e.node : e)))
  const seen = new Set()
  const groups = []
  const toMe = []
  for (const c of Array.isArray(conflicts) ? conflicts : []) {
    if (!c || typeof c !== 'object' || !c.file) continue
    const k = JSON.stringify([c.file, c.test || null])
    if (seen.has(k)) continue
    seen.add(k)
    if (fixed.has(nodeOf(c))) { toMe.push(c); continue }
    let g = groups.find(x => x.file === c.file)
    if (!g) { g = { file: c.file, conflicts: [] }; groups.push(g) }
    g.conflicts.push(c)
  }
  return { groups, toMe }
}

// --- end decisions ---

// --- scheduler ---------------------------------------------------------------

const running = new Map()    // task -> promise of its record
// Tasks holding a slot: an implementing agent (first attempt, retry, revision) is at work.
// Review and the merge queue don't hold one, so a slow merge never keeps a ready task waiting.
const working = new Set()
const records = {}           // task -> record
const failed = new Map()     // task -> reason
const parked = new Map()     // task -> needsInput entry: guard-stopped through the ladder; not failed, never retried, dependents wait
const mergedInRun = new Set()  // tasks this run landed: they stay Done whatever a plan snapshot says (1.6)
let stopLaunching = false
let stopReason = null
let redTarget = false
let mergedCount = 0
let fixCount = 0
let refills = 0
const fullGates = []      // the periodic GateRun records (Task 81)
const gates = []          // every attributed gate run: periodic, recheck and final GateRun records (Task 81)
let finalGateError = false
const GATE_ARGV = ['-C', TARGET, 'gate', SPEC, '--attribute', '--install', '--json']
const fixerEscalations = []
// Landing queue: finished tasks wait here; one `ck worktree land` runs at a time and
// takes up to batchWindow of them. Waiting here never holds a slot.
const landQueue = []         // { n, resolve }
let landingBusy = false
let landingIdle = Promise.resolve()
let batchWindow = MERGE_BATCH
const landings = []
// Batch review before landing (4.1-4.6): non-risky tasks whose implement step
// succeeded register here; the landBatch they join reviews them together before
// it merges (reviewBatch). Safety tasks are reviewed individually in runTask.
const pendingReview = new Map()  // n -> { rec, reviewed } awaiting (or past) its batch review
let reviewWaveCount = 0          // review-wave label counter (review-wave <n> tasks ...)
// Mid-run verify-order cycle repair (issue #6): agents.relink.auto (default on) or args.relink false.
const AUTO_RELINK = A.relink !== false && !(A.agents && A.agents.relink && A.agents.relink.auto === false)
const relinkTried = new Set()  // tasks a relink was attempted for (at most once each)
const relinks = []             // { task, cycle, reason } for every applied relink
const relinkedOk = new Set()   // relinked tasks whose next stale cycle report is ignored once
const MAX_RESOLVES = 3
// Cross-test consistency (reviewer.cross_test): one check per wave of test tasks,
// started when its last test task finishes (or at the end, for a wave that never
// finished). It runs beside the scheduler and never holds a slot or the landing.
const crossTests = []
const crossTestRuns = []
const crossTestDone = new Set()
let doneAtStart = new Set()
const testTasksOf = w => Object.keys(TRACKS).filter(m => TRACKS[m] === 'test' && waveOf(m) === w && !doneAtStart.has(m))
function maybeCrossTest(n, atEnd = false) {
  if (!CROSS_TEST || TRACKS[n] !== 'test') return
  const w = waveOf(n)
  if (crossTestDone.has(w) || Object.values(TRACKS).filter(t => t === 'test').length < 2) return
  const wave = testTasksOf(w)
  if (!atEnd && !wave.every(m => records[m] && !running.has(m))) return
  const merged = wave.filter(m => records[m] && records[m].ok)
  if (!merged.length) return
  crossTestDone.add(w)
  crossTestRuns.push((async () => {
    let r = null
    try {
      r = await call('cross-test', crossTestPrompt(merged), { label: `cross-test wave ${w}`, phase: phaseOf(merged[0]), schema: REVIEW_SCHEMA, ...RISKY_REVIEW })
    } catch (e) {
      r = null
    }
    const blocking = ((r && r.findings) || []).filter(f => f.severity === 'blocking')
    crossTests.push({ wave: w, tasks: merged, verdict: r ? r.verdict : 'unchecked', findings: (r && r.findings) || [], summary: r && r.summary })
    log(`cross-test consistency (wave ${w}): ${r ? `${blocking.length} blocking finding(s)${blocking.length ? `: ${trim(blocking.map(f => f.issue), 600)}` : ''}` : 'the check returned nothing'}`)
  })())
}

// The Workflow API has no non-agent step, so `ck plan` runs in a cheap agent. Its answer is
// checked, not trusted: bench v2 halted with "planning step failed" because a plan agent put a
// remark in "error" next to a valid plan, and other plan agents returned worktree paths
// (".ck/worktrees/<spec>-task-17") as "ready" entries, which launched Task 17 twice.
const PLAN_FAILED = 'planning step failed'
let planFailures = 0
const taskId = v => {
  const s = String(v == null ? '' : v).trim()
  const m = s.match(/-task-([0-9A-Za-z_]+)\/?$/) || s.match(/^(?:task\s+)?([0-9A-Za-z_]+)$/i)
  return m ? m[1] : s
}
const usablePlan = p => !!p && ['done', 'ready', 'remaining', 'cycles', 'verifyCycles'].every(k => Array.isArray(p[k])) && !!p.deps && typeof p.deps === 'object' && !Array.isArray(p.deps)
function normalizePlan(p) {
  const remaining = p.remaining.map(taskId)
  const known = n => !remaining.length || remaining.includes(n)
  // deps are kept exactly as ck plan printed them: a near-miss id ("Task 2", " 2") must stay unknown (1.10).
  const deps = {}
  for (const [k, v] of Object.entries(p.deps)) deps[k] = Array.isArray(v) ? v.map(String) : []
  return {
    ...p,
    done: p.done.map(taskId),
    remaining,
    deps,
    ready: [...new Set(p.ready.map(taskId))].filter(known),
    busy: (Array.isArray(p.busy) ? p.busy : []).map(b => ({ ...b, task: taskId(b && b.task) })).filter(b => known(b.task)),
    cycles: p.cycles.map(c => (Array.isArray(c) ? c.map(taskId) : [taskId(c)])),
    verifyCycles: p.verifyCycles.map(v => ({ ...v, task: taskId(v && v.task) })),
  }
}

// Every agent call goes through here: it keeps the list of calls in flight (request order), which
// is how the script estimates what waits for a runtime slot, and wakes the main loop when one settles.
const inFlight = []   // { seq, kind }
let callSeq = 0
const agentMs = {}     // kind -> ms of agent time (wall clock around agent calls): the overhead-share input (5.4)
let poked = false
let wakeLoop = null
function poke() {
  poked = true
  if (wakeLoop) { const w = wakeLoop; wakeLoop = null; w() }
}
const signal = () => (poked ? Promise.resolve() : new Promise(resolve => { wakeLoop = resolve }))
function call(kind, prompt, opts) {
  const entry = { seq: ++callSeq, kind }
  inFlight.push(entry)
  const t0 = nowMs()
  return (async () => {
    try {
      return await agent(prompt, opts)
    } finally {
      agentMs[kind] = (agentMs[kind] || 0) + since(t0)
      const i = inFlight.indexOf(entry)
      if (i >= 0) inFlight.splice(i, 1)
      poke()
    }
  })()
}

// The scheduler's view of the plan (applyPlanView) and what runs now.
let view = { order: [], deps: {}, done: new Set(), cycles: [], verifyCycles: [] }
let current = null          // the last plan applied
let lastPlanReady = []      // without args.deps: the ready list of the plan that just returned, used once
let planFallbacks = []    // one entry per fallback: { planStep, trigger, source, ready }
let degraded = false      // true once any fallback fired
let lastGood = null       // { ready, deps, order } of the last consistent plan: the dep-less fallback layer
let planRun = null          // the plan agent in flight, if any
let queuedTrigger = null    // a trigger that arrived while one was in flight: one follow-up
let idleArmed = true
let recordSeq = 0           // failed task records seen so far: a landing plan disarms the idle plan only if none arrived while it ran (an ok record waits for its landing, so it changes nothing)
let deferring = false
const excluded = () => new Set([...running.keys(), ...failed.keys(), ...parked.keys(), ...mergedInRun])
function planContext() {
  const exclude = excluded()
  const ready = SCRIPT_READINESS ? readyTasks({ order: view.order, deps: view.deps, done: view.done, exclude, blocked: blockedOf(view) }) : []
  const open = view.order.filter(n => !view.done.has(n) && !exclude.has(n)).length
  const freeSlot = working.size < MAX_CONCURRENT && (!isSlotCount(AGENT_SLOTS) || inFlight.length < AGENT_SLOTS)
  return { exclude, ready, open, freeSlot }
}

// The Workflow API has no non-agent step, so `ck plan` runs in a cheap agent. Its answer is
// checked, not trusted: it must hold together (planConsistent), and a failed attempt is retried once.
// The exclude list is captured when the prompt is built and used for that attempt's check, since
// tasks start and finish while the plan agent runs.
async function runPlan(trigger) {
  refills++
  const seqAtPrompt = recordSeq
  for (let attempt = 0; attempt < 2; attempt++) {
    const label = `plan ${refills}${attempt ? ' retry' : ''}`
    const exclude = [...excluded()]
    const out = await thinCk('plan', planArgv(exclude), { label, phase: 'Plan', cwd: ROOT, what: `Mechanical planning step for spec ${SPEC}` })
    const p = out.json ? { ...out.json } : { error: ckError(out) }
    if (out.json && out.stderr) p.error = out.stderr  // a note next to a usable plan is a remark, not a failure
    if (p.error === undefined || p.error === null) delete p.error
    // An "error" next to a usable plan is a remark, not a failure; an empty plan with an error is a failure.
    if (usablePlan(p) && !(p.error && !p.done.length && !p.remaining.length)) {
      const n = normalizePlan(p)
      // Done is ground truth even when the ready list is not: fold it in so the
      // fallback below never re-schedules an already-finished task (1.5). (Order
      // and deps stay on the args-seeded view per D5: the fallback is computed
      // from the deps arg, never from a failed plan's graph.)
      for (const d of n.done) view.done.add(d)
      // A plan that names no runnable task is inconsistent even when normalization
      // empties its ready list (1.1: a raw non-empty ready never follows from the
      // graph); raw-empty + truth-empty still adopts via the check below (1.4).
      const namesNothingRunnable = p.ready.length > 0 && n.ready.length === 0
      // Vacuous adoption is gated on raw-empty: a plan whose remaining is empty adopts
      // without a consistency check ONLY when its raw ready was already empty; a
      // non-empty claim against an empty remaining set is checked below (and fails:
      // nothing matches an empty order).
      if (!n.remaining.length && p.ready.length === 0) n.ready = []
      // A task a plan reports as busy is left out of its ready list: it is not a mismatch.
      if (!namesNothingRunnable && planConsistent(n, [...exclude, ...n.busy.map(b => b.task)])) {
        if (p.error) log(`${label}: the planning agent left a note (not a failure): ${trim(p.error, 300)}`)
        current = n
        view = applyPlanView(n, mergedInRun)
        // The dep-less fallback layer: the last consistent plan's ready/deps/order.
        // A consistent first attempt or retry success counts no fallback (1.4).
        lastGood = { ready: [...n.ready], deps: n.deps || {}, order: [...n.remaining] }
        // Task agents own BUSY detection; a busy list from the plan agent is ignored in script-readiness mode.
        for (const b of SCRIPT_READINESS ? [] : n.busy) {
          if (!failed.has(b.task) && !running.has(b.task)) {
            failed.set(b.task, `worktree busy: ${b.detail}`)
            records[b.task] = { task: b.task, wave: waveOf(b.task), ok: false, reason: `worktree busy: ${b.detail}` }
          }
        }
        if (trigger !== 'start') applyLint(n)  // the start plan's cycle refusal is explicit, below
        lastPlanReady = n.ready
        if (trigger === 'landing' && recordSeq === seqAtPrompt) idleArmed = false  // this plan just re-read tasks.md after a landing
        return n
      }
      planFailures++
      log(`${label}: the plan is inconsistent (its ready list does not follow from its dependencies)${attempt ? '' : ', retrying'}`)
      continue
    }
    planFailures++
    log(`${label}: no usable plan (${(p && p.error && trim(p.error, 300)) || 'no result'})${attempt ? '' : ', retrying'}`)
  }
  // Both attempts failed: resolve the deterministic fallback (D5) instead of halting when data exists.
  // scriptReady comes from script-side readyTasks, so unknown-dep tasks never appear (2.5); the fallback
  // subtracts running/failed/parked/merged/done, leaving their records untouched (1.5).
  const effOrder = view.order.length ? view.order : (Array.isArray(A.remaining) ? A.remaining.map(String) : Object.keys(A.deps || {}).map(String))
  const effDeps = view.deps && Object.keys(view.deps).length ? view.deps : (A.deps || {})
  const doneArr = [...view.done]
  const exclArr = [...excluded()]
  const scriptReady = SCRIPT_READINESS ? readyTasks({ order: effOrder, deps: effDeps, done: doneArr, exclude: exclArr, blocked: blockedOf(view) }) : []
  const fb = fallbackReady({ hasDeps: SCRIPT_READINESS, scriptReady, lastGoodReady: lastGood ? lastGood.ready : [], exclude: exclArr, done: doneArr })
  // A fallback view never overwrites lastGood.
  const synth = extra => {
    const s = { fromFallback: true, done: [...view.done], remaining: [...(view.order.length ? view.order : effOrder)], deps: (view.deps && Object.keys(view.deps).length ? view.deps : effDeps), ready: [], cycles: [], verifyCycles: [], busy: [], ...extra }
    if (!current) current = s
    return s
  }
  if (!fb.ready.length && !SCRIPT_READINESS && (!lastGood || !lastGood.ready.length)) {
    return null  // Halt: no deterministic data ever (no deps arg, no adopted plan with usable ready): the existing PLAN_FAILED halt path (2.3). No entry, degraded untouched.
  }
  // Every double-failure trigger with deterministic data records a fallback entry,
  // even when the resulting ready list is empty (an empty entry records a fallback
  // that scheduled nothing; 1.3 counts every trigger). The source is copied verbatim
  // from the seam (null when neither layer had anything schedulable).
  planFallbacks.push({ planStep: refills, trigger, source: fb.source, ready: [...fb.ready] })
  degraded = true
  if (!fb.ready.length) {
    // Data present but nothing schedulable (frontier in flight): wait for landings.
    log(`plan ${refills}: twice inconsistent, nothing schedulable yet; waiting for running tasks`)
    return synth()
  }
  log(`plan ${refills}: twice inconsistent, falling back to the deterministic ready list (${fb.source}): ${fb.ready.join(' ') || '(empty)'}`)
  if (!SCRIPT_READINESS) {
    lastPlanReady = [...fb.ready]  // the dep-less schedule path consumes this once
  } else if (!view.order.length) {
    // Seed the scheduling view from the run inputs so planContext/schedule sees the fallback list.
    view = { order: effOrder.map(String), deps: { ...effDeps }, done: new Set(doneArr), cycles: [], verifyCycles: [] }
  }
  return synth({ ready: [...fb.ready] })
}

// Run fn while no landing step runs (and keep new landings queued until it returns): a relink
// commits tasks.md on INTO, so it must never interleave with a merge into it.
async function withLandingLock(fn) {
  while (landingBusy) await landingIdle
  landingBusy = true
  try {
    return await fn()
  } finally {
    landingBusy = false
    if (landQueue.length) pumpLandings()
  }
}

async function relinkTask(n, vc) {
  let r = null
  try {
    r = await withLandingLock(async () => {
      const out = await thinCk('relink', ['-C', TARGET, 'relink', SPEC, n, '--apply', '--commit', '--json'], { label: `relink ${n}`, phase: phaseOf(n), cwd: TARGET, what: `Break a verify-order cycle at Task ${n} of spec ${SPEC}` })
      return out.json ? out.json : { fixable: false, applied: false, reason: ckError(out) }
    })
  } catch (e) {
    r = null
  }
  running.delete(n)
  if (r && r.fixable && r.applied && r.committed !== false) {
    relinks.push({ task: n, cycle: vc.message, reason: r.reason || 'relinked' })
    relinkedOk.add(n)
    log(`Task ${n}: relinked (${trim(r.reason || 'relinked', 200)}); it runs against the relinked tasks.md`)
  } else {
    const why = r ? (r.reason || 'relink refused') : 'the relink agent returned nothing'
    failed.set(n, `verify-order cycle: ${vc.message} (relink: ${why})`)
    records[n] = { task: n, wave: waveOf(n), ok: false, reason: `verify-order cycle: ${vc.message} (relink: ${why})` }
    log(`Task ${n}: skipped, verify-order cycle; relink not possible (${trim(why, 200)}): fix tasks.md`)
    if (!CONTINUE) { stopLaunching = true; stopReason = stopReason || `verify-order cycle at Task ${n}` }
  }
  idleArmed = true
  requestPlan('landing')  // re-read tasks.md: the relinked plan, or the skipped task's dependents
  poke()
}

// At most one plan agent in flight; an eligible trigger arriving meanwhile coalesces into one follow-up.
function requestPlan(trigger) {
  const ctx = planContext()
  const d = planDecision({ trigger, scriptReadiness: SCRIPT_READINESS, planInFlight: !!planRun, idleArmed, freeSlot: ctx.freeSlot, readyCount: ctx.ready.length, openCount: ctx.open, stopLaunching })
  if (d === 'none') return null
  if (d === 'coalesce') { queuedTrigger = queuedTrigger || trigger; return planRun }
  if (trigger === 'idle') {
    idleArmed = false
    log(`plan ${refills + 1} (idle): nothing ready, ${ctx.open} task(s) open; re-reading the plan`)
  }
  planRun = (async () => {
    let p = null
    try { p = await runPlan(trigger) } catch (e) { p = null } finally { planRun = null }
    if (!p && trigger !== 'start') { stopLaunching = true; stopReason = stopReason || PLAN_FAILED }
    const q = queuedTrigger
    queuedTrigger = null
    if (q) requestPlan(q)
    poke()
    return p
  })()
  return planRun
}

// A task's implementing agent finished; its slot is free.
function releaseSlot(n) {
  if (!working.delete(n)) return
  requestPlan('freed')
  poke()
}

function startTask(n) {
  working.add(n)
  running.set(n, runTask(n).then(rec => finishTask(n, rec)))
}

function finishTask(n, rec) {
  releaseSlot(n)
  running.delete(n)
  records[n] = rec
  maybeCrossTest(n)
  if (!rec.ok) { idleArmed = true; recordSeq++ }  // only a failure can change what is ready before its landing; a merged task was already followed by its landing plan; a failed one may change what is ready
  if (!rec.ok && rec.needsInput) {
    parked.set(n, rec.needsInput)  // listed apart from failed; never triggers onFailure: halt
    log(`Task ${n}: needs input (${rec.needsInput.action}); independent tasks continue, its dependents wait`)
  } else if (!rec.ok && rec.testConflict) {
    failed.set(n, rec.reason)  // its dependents wait; it never halts the run (issue #8)
    log(`Task ${n}: ${rec.reason}; independent tasks continue, the test-track fix is in the report`)
  } else if (!rec.ok) {
    failed.set(n, rec.reason)
    log(`Task ${n}: not merged (${rec.reason})`)
    if (!CONTINUE && !rec.busy) { stopLaunching = true; stopReason = stopReason || `Task ${n} failed` }
  }
  requestPlan('finished')
  poke()
  return { n, rec }
}

// Starts what may start now, and re-reads the plan when nothing is ready and a slot is free.
function schedule() {
  const ctx = planContext()
  let ready = ctx.ready
  if (!SCRIPT_READINESS) {
    // Without args.deps only the plan agent's ready list counts, and only once.
    ready = lastPlanReady.map(String).filter(n => !ctx.exclude.has(n) && !view.done.has(n))
    lastPlanReady = []
  }
  const d = startDecision({ finishFirst: FINISH_FIRST, inFlight, slots: AGENT_SLOTS, ready, working: working.size, maxConcurrent: MAX_CONCURRENT, stopLaunching })
  for (const n of d.start) if (!running.has(n)) startTask(n)
  if (d.start.length) deferring = false
  if (d.deferred) {
    if (!deferring) {
      deferring = true
      log(d.waiting > 0 ? `finish-first: holding new task starts while ${d.waiting} review/revise/land agent(s) wait for a slot` : `finish-first: holding new task starts while all ${AGENT_SLOTS} runtime slots are busy`)
    }
  } else deferring = false
  if (SCRIPT_READINESS && !stopLaunching && !planRun) requestPlan('idle')
}

function applyLint(p) {
  for (const vc of p.verifyCycles || []) {
    const n = String(vc.task)
    if (failed.has(n) || running.has(n)) continue
    // A plan that was already in flight when the relink committed still reports the old cycle once.
    if (relinkedOk.has(n)) { relinkedOk.delete(n); continue }
    // Issue #6: a cycle that only shows once a test task's files exist is repaired in place
    // (`ck relink`, once per task) instead of skipping the task; tracked in `running` so the run
    // waits for it, without holding an agent slot.
    if (AUTO_RELINK && !relinkTried.has(n)) {
      relinkTried.add(n)
      log(`Task ${n}: verify-order cycle found mid-run (${trim(vc.message, 300)}); relinking tasks.md on ${INTO}`)
      running.set(n, relinkTask(n, vc))
      continue
    }
    {
      failed.set(n, `verify-order cycle: ${vc.message}`)
      records[n] = { task: n, wave: waveOf(n), ok: false, reason: `verify-order cycle: ${vc.message}` }
      log(`Task ${n}: skipped, verify-order cycle (fix tasks.md; the next plan step picks it up)`)
      if (!CONTINUE) { stopLaunching = true; stopReason = stopReason || `verify-order cycle at Task ${n}` }
    }
  }
  if ((p.cycles || []).length) {
    stopLaunching = true
    stopReason = stopReason || `dependency cycle: ${p.cycles.map(c => c.join(' -> ')).join('; ')}`
  }
}

// --- Attributed gates (Task 81) ---------------------------------------------------------------------------------
// One GateRun record (Task 81): the verdict plus the gate's position and, when a fixer ran, its record.
const gateRun = (kind, afterMerges, out) => ({ kind, afterMerges, ...gateOutcome(out), fix: null })
// One attributed gate through thinCk. Claude Code asks for the compact answer (2.12); OpenCode reads the full JSON.
// An error is retried once; a second error is the run's record and sends nothing to the fixer.
async function attributedGate(kind, afterMerges, label, phaseName, what) {
  const argv = [...GATE_ARGV, ...(thin.mode === 'direct' ? [] : ['--compact'])]
  const ask = () => thinCk('gate', argv, { label, phase: phaseName, cwd: TARGET, gate: true, what })
  const first = await ask()
  let run = gateRun(kind, afterMerges, first)
  // Only a call-level failure (no JSON object at all) is worth a retry; a JSON object of the wrong shape would be
  // wrong again.
  if (run.state === 'error' && !(first && isPlainObj(first.json))) {
    log(`${label}: the gate call failed (${run.error}); retrying once`)
    run = gateRun(kind, afterMerges, await ask())
  }
  return run
}
// A red gate (2.6, 2.7): the regressions go to the fixer as Failure entries (3.9), each recheck is again an attributed
// gate, and the escalation ladder runs as before. Every recheck is recorded in `gates`.
async function fixAndRecheck(what, first, afterMerges, phaseName) {
  const ph = phaseName
  const failuresOf = g => failuresForPrompt(g.regressions)
  fixCount++
  let fix = await call('fix', fixPrompt(fixCount, what, failuresOf(first)), { label: `fix ${fixCount} (${what})`, phase: ph, schema: VERIFY, ...FIX })
  let recheck = await attributedGate('recheck', afterMerges, `re-check ${fixCount}`, ph, `Re-check for spec ${SPEC} in ${TARGET} (branch ${INTO})`)
  gates.push(recheck)
  for (const model of FIX_UP) {
    if (recheck.state !== 'red') break
    fixCount++
    log(`${what}: still red after the fix, escalating the fixer to ${model}`)
    fix = await call('fix', fixPrompt(fixCount, what, failuresOf(recheck)), { label: `fix ${fixCount} (${what}) [${model}]`, phase: ph, schema: VERIFY, ...escalated(FIX, model) })
    recheck = await attributedGate('recheck', afterMerges, `re-check ${fixCount}`, ph, `Re-check for spec ${SPEC} in ${TARGET} (branch ${INTO})`)
    gates.push(recheck)
    fixerEscalations.push({ what, model, green: recheck.green })
  }
  return { fix, recheck, green: recheck.green }
}

function requestLanding(n) {
  return new Promise(resolve => {
    landQueue.push({ n, resolve })
    pumpLandings()
  })
}

function pumpLandings() {
  if (landingBusy) return
  landingBusy = true
  landingIdle = (async () => {
    try {
      while (landQueue.length) await landBatch(landQueue.splice(0, batchWindow))
    } finally {
      landingBusy = false
    }
  })()
}

async function landBatch(batch) {
  if (REVIEW_ON) {
    batch = await reviewBatch(batch)
    if (!batch.length) return
  }
  const tasks = batch.map(b => b.n)
  const used = batchWindow
  let res = null
  try {
    const out = await thinCk('land', landArgv(tasks), { label: `land ${tasks.join(' ')}`, phase: phaseOf(tasks[0]), cwd: ROOT, gate: true, what: `Landing step for spec ${SPEC}: it merges the tasks into ${INTO}, gates them, and bisects a red batch itself` })
    res = out.json && Array.isArray(out.json.tasks) ? out.json : { tasks: [], error: ckError(out) }
  } catch (e) {
    res = { tasks: [], error: e && e.message }
  }
  if (PROGRESS_RUN) log(res && typeof res.eta === 'string' && res.eta ? res.eta : 'ETA unavailable')
  const byTask = {}
  for (const t of (res && res.tasks) || []) byTask[String(t.task)] = t
  const entry = { tasks, size: tasks.length, window: used, merged: [], red: [], conflict: [], deferred: [], other: [], gates: ((res && res.gates) || []).length, bisected: !!(res && res.bisected), minutes: minutes(res && res.startedAt, res && res.finishedAt) }
  const outcomes = []
  const deferred = []
  const before = mergedCount
  for (const b of batch) {
    const t = byTask[b.n] || { task: b.n, state: 'ERROR', detail: (res && res.error) || 'the landing step returned no result for this task' }
    if (t.state === 'DEFERRED') { deferred.push(b); entry.deferred.push(b.n); continue }
    if (t.state === 'MERGED') { entry.merged.push(b.n); mergedCount++ }
    else if (t.state === 'RED') entry.red.push(b.n)
    else if (t.state === 'CONFLICT') entry.conflict.push(b.n)
    else entry.other.push(b.n)
    outcomes.push([b, t])
  }
  landQueue.unshift(...deferred)  // they land in the next batch
  // Zuul-style window: halve after a red batch, grow by one after a green one.
  batchWindow = entry.red.length || entry.bisected ? Math.max(1, Math.floor(batchWindow / 2)) : Math.min(MERGE_BATCH, batchWindow + 1)
  landings.push(entry)
  if (entry.bisected || entry.red.length) log(`Landing ${tasks.join(' ')}: red, bisected; ${entry.red.length ? `culprit(s) ${entry.red.join(', ')}` : 'no culprit (flaky?)'}; window now ${batchWindow}`)

  if (entry.merged.length && FULL_EVERY > 0 && Math.floor(mergedCount / FULL_EVERY) > Math.floor(before / FULL_EVERY)) {
    const g = await attributedGate('periodic', mergedCount, `full gate @${mergedCount}`, phaseOf(tasks[0]), `Verification (full gate after ${mergedCount} merges) for spec ${SPEC}`)
    gates.push(g)
    fullGates.push(g)
    if (g.state === 'red') {
      log(`Full gate red after ${mergedCount} merges, one fix attempt`)
      g.fix = await fixAndRecheck(`full gate after ${mergedCount} merges`, g, mergedCount, phaseOf(tasks[0]))
      // A recheck that errors leaves the run going (2.9); only a verdict that is still red stops it.
      if (g.fix.recheck.state === 'red') {
        redTarget = true
        stopLaunching = true
        stopReason = stopReason || `${INTO} is red: full gate after ${mergedCount} merges`
      }
    } else if (g.state === 'error') {
      log(`Full gate after ${mergedCount} merges could not run (${g.error}); the run goes on`)
    }
  }
  for (const [b, t] of outcomes) {
    if (t.state === 'MERGED') { mergedInRun.add(b.n); view.done.add(b.n) }  // Done for the scheduler at once; the plan below confirms it
  }
  if (outcomes.length) {
    idleArmed = true
    requestPlan('landing')
    poke()
  }
  for (const [b, t] of outcomes) b.resolve(t)
}

// Batch review before landing (4.1, 4.3-4.6): one review-wave agent reads the
// combined change of the batch's not-yet-reviewed non-risky tasks before any
// of them merges. Approved tasks stay in the batch; each flagged task leaves it
// unmerged with state REVIEW_CHANGES and its own findings, and its runTask
// revises and re-reviews it alone (in parallel, not in the landing queue).
// Safety tasks were reviewed individually in runTask and are not in
// pendingReview; re-queued tasks (conflict, red, deferred) are not reviewed twice.
async function reviewBatch(batch) {
  const ids = batch.map(b => b.n).filter(n => pendingReview.has(n) && !pendingReview.get(n).reviewed)
  if (!ids.length) return batch
  const waveNo = ++reviewWaveCount
  let review = null
  try {
    review = await call('review-wave', waveReviewPrompt(waveNo, ids), { label: `review-wave ${waveNo} tasks ${ids.join(' ')}`, phase: phaseOf(ids[0]), schema: REVIEW_SCHEMA, ...REVIEW })
  } catch (e) {
    review = null
  }
  for (const n of ids) pendingReview.get(n).reviewed = true
  review = attributeFindings(review, ids)
  const flagged = flaggedOf(review, ids)
  if (review && review.verdict === 'changes' && ids.length > 1 && flagged.length === ids.length && !(review.findings || []).some(f => f && f.severity !== 'minor' && f.task != null)) {
    log(`Review wave ${waveNo}: its blocking findings name no covered task, so all of Tasks ${ids.join(', ')} revise`)
  }
  for (const n of ids.filter(t => !flagged.includes(t))) setBatchApproval(n, review, waveNo, ids)
  if (!review) log(`Review wave ${waveNo}: the review agent returned nothing; landing Tasks ${ids.join(', ')} unreviewed`)
  const keep = []
  for (const b of batch) {
    if (!flagged.includes(b.n)) { keep.push(b); continue }
    const findings = (review.findings || []).filter(f => f && (f.task == null || String(f.task) === b.n))
    log(`Task ${b.n}: review wave ${waveNo} asked for changes; revising before it lands`)
    b.resolve({ task: b.n, state: 'REVIEW_CHANGES', review: { ...review, findings, fromBatch: waveNo } })
  }
  return keep
}

// Issue #16: give each batched finding its covered task. A finding that carries no
// `task` (a structured output that dropped it) gets the one covered task its text
// names ("Task 74, P-7 ..."); a finding naming none or several stays unattributed.
function attributeFindings(review, batched) {
  if (!review || !Array.isArray(review.findings)) return review
  const findings = review.findings.map(f => {
    if (!f || (f.task != null && batched.includes(String(f.task)))) return f
    const text = [f.issue, f.fix, f.file].filter(x => typeof x === 'string').join(' ')
    const named = [...new Set([...text.matchAll(/\bTasks?\s+(\d+)\b/g)].map(m => m[1]).filter(t => batched.includes(t)))]
    return named.length === 1 ? { ...f, task: named[0] } : f
  })
  return { ...review, findings }
}

// Which batched tasks a batched verdict flags for revision (4.4): blocking findings
// that name a covered task flag it (a minor finding never flags another task); a
// `changes` verdict whose blocking findings name no covered task flags the whole wave
// (a blocking problem is re-verified, never silently recorded), and says so (#16).
// An `approve` (or no) verdict flags nobody.
function flaggedOf(review, batched) {
  if (!review || review.verdict !== 'changes') return []
  const findings = Array.isArray(review.findings) ? review.findings : []
  const blocking = findings.filter(f => f && f.severity !== 'minor')
  const named = [...new Set(blocking.map(f => (f.task != null ? String(f.task) : null)).filter(t => t != null && batched.includes(t)))]
  return named.length ? named : [...batched]
}

// Record a batched `approve` on one covered task (4.5): same reviewer
// attribution as an individual approval, plus the fromBatch wave number.
function setBatchApproval(n, review, waveNo, batched) {
  const slot = pendingReview.get(n)
  if (!slot) return
  const by = { model: REVIEW.model || 'inherited', effort: REVIEW.effort || 'inherited', riskTag: slot.rec.riskTag }
  slot.rec.review = review
    ? { verdict: 'approve', rounds: 0, findings: review.findings || [], summary: review.summary, fromBatch: waveNo, tasks: [...batched], ...by }
    : { verdict: 'unreviewed', rounds: 0, fromBatch: waveNo, tasks: [...batched], ...by }
}

// Land task n: queue it, then handle what the landing says. CONFLICT goes to the
// resolver and RED to the fixer (then up its ladder); both re-queue the task.
async function landTask(n, rec) {
  let resolves = 0
  let fixes = 0
  let lastEscalation = null
  while (true) {
    const t = await requestLanding(n)
    rec.landing = t
    if (t.state === 'MERGED') {
      if (lastEscalation) lastEscalation.green = true
      return { merged: true, mergedAt: t.mergedAt, checkGreen: t.checkGreen }
    }
    if (t.state === 'REVIEW_CHANGES') return { merged: false, review: t.review }
    if (t.state === 'CONFLICT') {
      if (resolves >= MAX_RESOLVES) return { merged: false, error: `still conflicting after ${resolves} resolution(s): ${(t.files || []).join(', ')}` }
      resolves++
      rec.resolves = resolves
      log(`Task ${n}: conflicts with ${INTO}, handing it to the resolver`)
      const r = await call('resolve', resolvePrompt(n, (t.files || []).join(', ') || t.detail, claimFlags('resolve', RESOLVE, resolves)), { label: `resolve ${n}${resolves > 1 ? ` round ${resolves}` : ''}`, phase: phaseOf(n), schema: RESOLVED, ...RESOLVE })
      if (!r || !r.resolved) return { merged: false, error: `conflict not resolved: ${(r && r.error) || 'no result'}` }
      continue
    }
    if (t.state === 'RED') {
      let failure = t.checkTail || t.detail
      let fixedGreen = false
      while (!fixedGreen) {
        if (fixes > FIX_UP.length) return { merged: false, error: `red on landing after ${fixes} fix(es): ${trim(failure, 600)}` }
        const model = fixes ? FIX_UP[fixes - 1] : null
        fixes++
        fixCount++
        rec.fixes = fixes
        log(`Task ${n}: red on landing (not merged; ${INTO} stays green), fix ${fixes}${model ? ` on ${model}` : ''}`)
        const f = await call('fix', landFixPrompt(fixCount, n, failure, claimFlags(model ? 'escalation' : 'landfix', model ? escalated(FIX, model) : FIX, fixes)), { label: `fix ${fixCount} (Task ${n} red on landing)${model ? ` [${model}]` : ''}`, phase: phaseOf(n), schema: VERIFY, ...(model ? escalated(FIX, model) : FIX) })
        if (model) { lastEscalation = { what: `landing Task ${n}`, model, green: false }; fixerEscalations.push(lastEscalation) }
        fixedGreen = !!(f && f.green)
        if (!fixedGreen) failure = (f && f.failing) || failure
      }
      continue
    }
    if (t.state === 'SKIP') return { merged: false, error: 'no commits' }
    if (t.state === 'PROTECTED') return { merged: false, protected: { files: t.files || [], detail: t.detail || '' }, error: `needs a human: changes protected files (${(t.files || []).join(', ')}); not landed. ${t.detail || ''}` }
    return { merged: false, error: `${t.state}: ${t.detail || ''}` }
  }
}

// A landing that comes back PROTECTED (2.13, 9.8): no agent lands it. The task is parked as needs-input with the files,
// the approve command for me to run in my own terminal, and the resume (the same runId).
async function parkProtected(n, rec, protectedInfo) {
  const action = parkAction({ interactive: INTERACTIVE })
  const files = (protectedInfo.files || []).map(String)
  const command = `ck worktree approve ${SPEC} ${n}`
  const resume = `/spec:implement ${SPEC}${RUN_ID ? ` with the same runId ${RUN_ID}` : ''}`
  const decision = `Task ${n} changes protected files (${files.join(', ') || 'see the landing'}), so no agent lands it. Review the change, then approve it in your own terminal: ${command}. Then resume the run: ${resume}.`
  const entry = {
    task: n, file: files.join(', '), files, kind: 'protected', test: '', requirement: '', decision,
    command, resume, runId: RUN_ID || null, action, notification: null, escalatedTo: [],
  }
  releaseSlot(n)
  if (action === 'park') {
    const note = JSON.stringify({ file: entry.file, kind: 'protected', test: '', requirement: '', decision })
    const out = await thinCk('park', ['worktree', 'park', SPEC, n, '--json'], { stdin: note, label: `park ${n}`, phase: phaseOf(n), cwd: ROOT, what: `Parking step for Task ${n} of spec ${SPEC}` })
    const p = out.json ? out.json : { parked: false, error: ckError(out) }
    entry.notification = p && p.notification ? p.notification : 'failed'
    entry.notificationDetail = (p && (p.detail || p.error)) || ''
    if (!p || p.parked === false) {
      entry.notification = 'failed'
      entry.notificationDetail = 'park failed: ' + ((p && (p.error || p.detail)) || 'the park step returned nothing')
    } else rec.parked = true
  }
  rec.needsInput = entry
  rec.reason = `needs input: protected files (${files.join(', ')})`
  return rec
}


// --- the test-fix loop (Task 86, Story 4) ---
// A confirmed conflict goes to a test-fixer (the test writer on a testfix<k> branch from INTO). A fix lands only when
// ck's scope check and proof pass and a risky-tier reviewer approves it; anything else goes to me with the findings.
const testFixRecords = []     // TestFixRecord[]: the result's testFixes
const testFixLedger = new Set()   // nodes a fix was tried for this run (the ck ledger holds the ones from earlier runs)
let testFixSeq = 0
let ledgerSeq = 0
const conflictNode = c => (c.test ? `${c.file}::${c.test}` : String(c.file))
const conflictEntry = c => ({ file: String(c.file ?? ''), test: String(c.test ?? ''), criterion: String(c.criterion ?? ''), reason: String(c.reason ?? ''), fix: String(c.fix ?? '') })
// 4.5: the workflow does not trust in_scope alone.
const scopeRefusal = sc => {
  if (!sc || typeof sc !== 'object') return 'ck returned no scope decision'
  const out = (sc.outside || []).map(String)
  const docs = (sc.spec_docs_edited || []).map(String)
  if (sc.in_scope !== true || out.length || docs.length || sc.requirements_edited) {
    return `out of scope: ${out.length ? `changes ${out.join(', ')}` : ''}${docs.length ? ` edits ${docs.join(', ')}` : ''}${sc.requirements_edited ? ' edits requirements.md' : ''}${sc.reason ? ` (${sc.reason})` : ''}`.replace(/\s+/g, ' ')
  }
  return null
}
const proofRefusal = pf => {
  if (!pf || typeof pf !== 'object') return 'ck returned no proof'
  const bad = (Array.isArray(pf.outcomes) ? pf.outcomes : []).filter(o => o && !(o.passed === true || o.cls === 'not-built'))
  if (pf.proved !== true || bad.length) return `the proof failed: ${pf.reason || ''}${bad.length ? ` ${JSON.stringify(bad)}` : ''}`.trim()
  return null
}
const reviewApproved = rv => !!rv && rv.verdict === 'approve' && !(rv.findings || []).some(f => f && f.severity === 'blocking')

// One test-fixer for one file group. `blocked` is the task the tests block (null for a cross-test fix, which holds `held`).
async function runTestFix({ k, blocked, held = [], source = 'conflict', conflicts }) {
  const fix = `testfix${k}`
  const entries = conflicts.map(conflictEntry)
  const nodes = conflicts.map(conflictNode)
  const phase = blocked != null ? phaseOf(blocked) : 'Test fix'
  const rec = { fix, blocked: blocked ?? null, held: [...held], source, tests: nodes, outcome: 'to-me', proof: null, scope: null, review: null, reason: '', finding: null }
  const toMe = reason => { rec.outcome = 'to-me'; rec.reason = reason; return rec }
  const record = async outcome => {
    const out = await thinCk('testfix record', ['-C', ROOT, 'testfix', 'record', SPEC, fix, ...nodes.flatMap(x => ['--test', x]), '--outcome', outcome, '--json'], { label: `testfix record ${k}`, phase, cwd: ROOT, what: `Recording step for test fix ${fix} of spec ${SPEC}` }).catch(() => null)
    if (!out || out.code !== 0) log(`Test fix ${fix}: ck testfix record did not confirm (${out ? trim(out.stderr || out.code, 200) : 'no answer'})`)
  }
  for (const n of nodes) testFixLedger.add(n)
  let fx = null
  try {
    fx = await call('testfix', testFixPrompt(k, blocked, entries), { label: blocked != null ? `testfix ${k} (Task ${blocked})` : `testfix ${k} (cross-test)`, phase, schema: TESTFIX, ...TEST })
  } catch (e) {
    return toMe(`the test-fixer agent failed: ${trim(e && e.message, 300)}`)
  }
  if (!fx || typeof fx !== 'object') return toMe('the test-fixer returned nothing')
  if (fx.verdict === 'test-correct') {
    rec.outcome = 'test-correct'
    rec.finding = fx.finding && typeof fx.finding === 'object' ? fx.finding : { file: conflicts[0].file, test: conflicts[0].test || '', issue: fx.summary || 'the test is correct' }
    rec.reason = trim(rec.finding.issue || fx.summary, 400)
    return rec
  }
  if (fx.verdict !== 'fixed') return toMe(`the test-fixer cannot fix it: ${trim(fx.summary, 400)}`)
  if (fx.finding && typeof fx.finding === 'object') return toMe(`the test-fixer's report contradicts itself (verdict fixed with a finding about the code): ${trim(fx.finding, 300)}`)
  const proveArgv = ['-C', ROOT, 'testfix', 'prove', SPEC, fix, ...(blocked != null ? ['--blocked', String(blocked)] : held.flatMap(h => ['--held', String(h)])), ...nodes.flatMap(x => ['--test', x]), '--into', INTO, '--json']
  const pv = await thinCk('testfix prove', proveArgv, { label: `testfix prove ${k}`, phase, cwd: ROOT, what: `Proof step for test fix ${fix} of spec ${SPEC}: it checks the fix's scope, then that the tests pass with it` })
  if (!pv.json) { await record('to-me'); return toMe(`ck testfix prove gave no result: ${ckError(pv)}`) }
  rec.scope = pv.json.scope ?? null
  rec.proof = pv.json.proof ?? null
  const refusedScope = scopeRefusal(rec.scope)
  if (refusedScope) { await record('to-me'); return toMe(refusedScope) }
  const refusedProof = proofRefusal(rec.proof)
  if (refusedProof) { await record('to-me'); return toMe(refusedProof) }
  let rv = null
  try {
    rv = await call('review', testFixReviewPrompt(k, entries, Array.isArray(fx.changedTests) ? fx.changedTests : [], rec.scope), { label: `testfix review ${k}`, phase, schema: REVIEW_SCHEMA, ...RISKY_REVIEW })
  } catch (e) {
    rv = null
  }
  rec.review = rv ? { verdict: rv.verdict, findings: rv.findings || [], summary: rv.summary } : null
  if (!reviewApproved(rv)) {
    await record('to-me')
    return toMe(rv ? `the reviewer rejected the fix: ${trim((rv.findings || []).filter(f => f && f.severity === 'blocking').map(f => f.issue), 800) || rv.summary || rv.verdict}` : 'the reviewer returned nothing')
  }
  while (landingBusy) await landingIdle   // the landing queue lands one batch at a time
  let res = null
  try {
    const out = await thinCk('land', landArgv([fix]), { label: `land ${fix}`, phase, cwd: ROOT, gate: true, what: `Landing step for test fix ${fix} of spec ${SPEC}: it merges the fix into ${INTO} and gates the tests it changes` })
    res = out.json && Array.isArray(out.json.tasks) ? out.json : { tasks: [], error: ckError(out) }
  } catch (e) {
    res = { tasks: [], error: e && e.message }
  }
  const t = (res.tasks || []).find(x => String(x.task) === fix) || { state: 'ERROR', detail: res.error || 'the landing step returned no result for the fix' }
  if (t.state !== 'MERGED') {
    await record('to-me')
    return toMe(`the fix did not land (${t.state}${t.detail ? `: ${trim(t.detail, 300)}` : ''}${t.files && t.files.length ? ` ${t.files.join(', ')}` : ''})`)
  }
  await record('landed')
  rec.outcome = 'landed'
  rec.reason = 'the fix landed'
  return rec
}

// A blocked task's confirmed conflicts: plan, run one test-fixer per file, record each outcome.
// Returns {records, fixed: [node], findings: [Failure], toMe: [reason]}.
async function testFixFor(n, conflicts) {
  const nodes = [...new Set(conflicts.map(conflictNode))]
  const led = await thinCk('testfix ledger', ['-C', ROOT, 'testfix', 'check', SPEC, ...nodes.flatMap(x => ['--test', x]), '--json'], { label: `testfix ledger ${++ledgerSeq}`, phase: phaseOf(n), cwd: ROOT, what: `Ledger check for the test fixes of spec ${SPEC}` })
  const prior = led.json && Array.isArray(led.json.alreadyFixed) ? led.json.alreadyFixed : []
  const plan = testFixPlan({ conflicts, alreadyFixed: [...prior, ...nodes.filter(x => testFixLedger.has(x)).map(node => ({ node, at: 'this run', fix: 'this run' }))] })
  const out = { records: [], fixed: [], findings: [], toMe: [] }
  for (const c of plan.toMe) {
    const reason = `${conflictNode(c)} was already fixed once (testfix ledger); a second fix goes to you`
    const rec = { fix: null, blocked: n, held: [], source: 'conflict', tests: [conflictNode(c)], outcome: 'to-me', proof: null, scope: null, review: null, reason }
    out.records.push(rec)
    out.toMe.push(reason)
  }
  for (const g of plan.groups) {
    const k = ++testFixSeq
    log(`Task ${n}: test-fixer ${k} for ${g.file} (${g.conflicts.length} test(s))`)
    const rec = await runTestFix({ k, blocked: n, conflicts: g.conflicts })
    out.records.push(rec)
    if (rec.outcome === 'landed') out.fixed.push(...rec.tests)
    else if (rec.outcome === 'test-correct') out.findings.push({ ...rec.finding, kind: 'assertion', message: rec.reason, tasks: [String(n)], command: null, node: rec.tests[0], file: rec.finding.file || g.file })
    else out.toMe.push(`${g.file}: ${rec.reason}`)
  }
  testFixRecords.push(...out.records)
  return out
}

// A task whose test fix goes to me: needs-input kind "test-fix" with the findings, parked (unattended) or asked (interactive).
async function parkTestFix(n, rec, reasons, records) {
  const action = parkAction({ interactive: INTERACTIVE })
  const tests = [...new Set(records.flatMap(x => x.tests))]
  const files = [...new Set(tests.map(t => t.split('::')[0]))]
  const decision = `Task ${n} is blocked on tests that can never pass, and the automatic test fix did not land: ${reasons.join(' | ')}. Fix the tests yourself on ${INTO} (a test-track change: you decide), or tell the agent how to proceed; then resume the run${RUN_ID ? ` with the same runId ${RUN_ID}` : ''}.`
  const entry = {
    task: n, file: files.join(', '), files, kind: 'test-fix', test: tests.join(', '), requirement: '', decision,
    findings: reasons, fixes: records.map(x => x.fix).filter(Boolean), runId: RUN_ID || null, action, notification: null, escalatedTo: [],
  }
  releaseSlot(n)
  if (action === 'park') {
    const note = JSON.stringify({ file: entry.file, kind: 'test-fix', test: entry.test, requirement: '', decision })
    const out = await thinCk('park', ['worktree', 'park', SPEC, n, '--json'], { stdin: note, label: `park ${n}`, phase: phaseOf(n), cwd: ROOT, what: `Parking step for Task ${n} of spec ${SPEC}` })
    const p = out.json ? out.json : { parked: false, error: ckError(out) }
    entry.notification = p && p.notification ? p.notification : 'failed'
    entry.notificationDetail = (p && (p.detail || p.error)) || ''
    if (!p || p.parked === false) {
      entry.notification = 'failed'
      entry.notificationDetail = 'park failed: ' + ((p && (p.error || p.detail)) || 'the park step returned nothing')
    } else rec.parked = true
  }
  rec.needsInput = entry
  rec.reason = `needs input: test fix (${trim(reasons.join(' | '), 300)})`
  return rec
}

async function runTask(n) {
  const rec = { task: n, wave: waveOf(n), title: TITLES[n], attempts: 0, ok: false, escalations: [], resolves: 0 }
  const busy = r => worktreeOutcome(r).kind === 'busy'
  rec.riskTag = isSafety(n) ? 'safety' : 'normal'
  rec.startModel = codeOpts(n).model || 'inherited'
  rec.reviewModel = REVIEW_ON ? reviewOpts(n).model || 'inherited' : null
  if (A.agents || A.model || A.effort) {
    const routed = isSafety(n) || (COMPLEXITY_ROUTING && isHigh(n))
    log(`Task ${n}: risk ${rec.riskTag}${COMPLEXITY[n] ? `, complexity ${String(COMPLEXITY[n]).toLowerCase()}` : ''}, start ${desc(codeOpts(n))}${routed ? ' (routed)' : ''}${up(ladderOf(n))}, review ${REVIEW_ON ? desc(reviewOpts(n)) : 'off'}`)
  }
  try {
    let r = await call('task', taskPrompt(n, claimFlags('task-start', codeOpts(n), 1)), { label: label(n), phase: phaseOf(n), schema: RESULT, ...codeOpts(n) })
    let lastModel = codeOpts(n).model
    let stop = guardStopOf(r)   // set after every call; a guard stop leaves the retry, escalate and revise loops at once
    let guardK = 0
    const triedGuard = new Set()
    const retryNo = () => rec.attempts - guardK   // attempts that spent the retry budget: guard attempts do not count
    // Climb the guard ladder (S17) from the model that stopped: each higher model once, at the role's effort, no
    // retry or escalate step spent. Returns the last result; `stop` stays set if the top of the ladder stopped too.
    const climbGuard = async attempted => {
      for (const model of guardLadder(attempted)) {
        if (triedGuard.has(model)) continue
        triedGuard.add(model)
        guardK++
        log(`Task ${n}: guard stop, trying ${model} (guard ${guardK})`)
        r = await call('task', retryPrompt(n, r, retryNo(), claimFlags('escalation', escalated(codeOpts(n), model), rec.attempts + 1)), { label: `${label(n)} guard ${guardK} (${model})`, phase: phaseOf(n), schema: RESULT, ...escalated(codeOpts(n), model) })
        rec.attempts++
        rec.escalations.push({ stage: 'guard', model })
        rec.startedAt = rec.startedAt || (r && r.startedAt)
        lastModel = model
        stop = guardStopOf(r)
        if (!stop) break
      }
      return r
    }
    // Still guard-stopped through the top of the ladder: park it (unattended) or list it for me to decide (interactive).
    const needInput = async () => {
      const action = parkAction({ interactive: INTERACTIVE })
      const entry = {
        task: n,
        file: stop.file,
        kind: stop.kind || 'test',
        test: stop.test || '',
        requirement: stop.requirement || '',
        decision: `Is ${stop.test || stop.file} wrong${stop.requirement ? ` (requirement ${stop.requirement})` : ''}, or must the implementation change? Change the test or requirement yourself from the main session, or tell the agent to keep it as is.`,
        action,
        notification: null,
        escalatedTo: rec.escalations.filter(e => e.stage === 'guard').map(e => e.model),
      }
      rec.result = r
      rec.finishedAt = r && r.finishedAt
      releaseSlot(n)
      if (action === 'park') {
        let p = null
        const note = JSON.stringify({ file: stop.file, kind: stop.kind || 'test', test: stop.test || '', requirement: stop.requirement || '', decision: entry.decision })
        const out = await thinCk('park', ['worktree', 'park', SPEC, n, '--json'], { stdin: note, label: `park ${n}`, phase: phaseOf(n), cwd: ROOT, what: `Parking step for Task ${n} of spec ${SPEC}` })
        p = out.json ? out.json : { parked: false, error: ckError(out) }
        entry.notification = p && p.notification ? p.notification : 'failed'
        entry.notificationDetail = (p && (p.detail || p.error)) || ''
        if (!p || p.parked === false) {
          entry.notification = 'failed'
          entry.notificationDetail = 'park failed: ' + ((p && (p.error || p.detail)) || 'the park step returned nothing')
        } else rec.parked = true
      }
      rec.needsInput = entry
      rec.reason = `needs input: ${stop.test || stop.file}`
      return rec
    }
    rec.attempts = 1
    rec.startedAt = r && r.startedAt
    // The task agent creates its own worktree first: a busy or failed creation ends the task here (2.3, 2.4).
    const wo = worktreeOutcome(r)
    if (wo.kind === 'busy' || wo.kind === 'error') {
      rec.result = r
      rec.finishedAt = r && r.finishedAt
      rec.reason = wo.reason
      if (wo.kind === 'busy') rec.busy = true
      releaseSlot(n)
      return rec
    }
    let escIdx = 0
    let budgetBase = 0   // retries spent before a landed test fix do not count against the fresh attempt
    const rungs = ladderOf(n)
    const ladder = async () => {
    for (;;) {
      while (!ok(r) && !stop && retryNo() - budgetBase <= MAX_RETRIES && !busy(r) && !testConflictOf(r)) {
        log(`Task ${n}: ${r && !(r.commits || '').trim() ? 'no commits' : 'gate not passed'}, retry ${retryNo()}/${MAX_RETRIES}`)
        r = await call('task', retryPrompt(n, r, retryNo(), claimFlags('retry', codeOpts(n), retryNo() + 1)), { label: `${label(n)} retry ${retryNo()}`, phase: phaseOf(n), schema: RESULT, ...codeOpts(n) })
        rec.attempts++
        lastModel = codeOpts(n).model
        stop = guardStopOf(r)
        rec.startedAt = rec.startedAt || (r && r.startedAt)
      }
      while (escIdx < rungs.length && !ok(r) && !stop && !busy(r) && !testConflictOf(r)) {
        const model = rungs[escIdx++]
        log(`Task ${n}: still failing after ${retryNo()} attempt(s), escalating to ${model}`)
        r = await call('task', retryPrompt(n, r, retryNo(), claimFlags('escalation', escalated(codeOpts(n), model), retryNo() + 1)), { label: `${label(n)} retry ${retryNo()} (${model})`, phase: phaseOf(n), schema: RESULT, ...escalated(codeOpts(n), model) })
        rec.attempts++
        rec.escalations.push({ stage: 'retry', model })
        lastModel = model
        stop = guardStopOf(r)
        rec.startedAt = rec.startedAt || (r && r.startedAt)
      }
      if (!stop) break
      await climbGuard(lastModel)
      if (stop) return await needInput()
      if (ok(r) || busy(r)) break
    }
    return null
    }
    const early = await ladder()
    if (early) return early
    if (testConflictOf(r)) {
      // Issue #8: one confirmation on the strongest rung (unless the claim already came from it):
      // a second agent that agrees makes the task blocked: test; one that passes the gate wins.
      const first = testConflictOf(r)
      const top = rungs.length ? rungs[rungs.length - 1] : null
      const onTop = !top || lastModel === top
      log(`Task ${n}: the agent reports a test that can never pass (${trim(first.file, 120)}: ${trim(first.reason, 200)}); ${onTop ? 'confirming' : `confirming on ${top}`}`)
      const o = onTop ? codeOpts(n) : escalated(codeOpts(n), top)
      r = await call('task', confirmConflictPrompt(n, r, retryNo(), claimFlags(onTop ? 'retry' : 'escalation', o, retryNo() + 1)), { label: `${label(n)} retry ${retryNo()}${onTop ? '' : ` (${top})`}`, phase: phaseOf(n), schema: RESULT, ...o })
      rec.attempts++
      if (!onTop) { rec.escalations.push({ stage: 'test-conflict', model: top }); lastModel = top }
      rec.startedAt = rec.startedAt || (r && r.startedAt)
      const confirmed = testConflictOf(r)
      if (confirmed) {
        // Task 86 (4.1-4.10): the confirmed conflicts go to a test-fixer; a landed fix or a test-correct verdict retries the task.
        releaseSlot(n)
        rec.result = r
        rec.finishedAt = r && r.finishedAt
        rec.testConflict = { ...confirmed, firstReport: first }
        rec.reason = `blocked: test (${confirmed.file}${confirmed.test ? ` ${confirmed.test}` : ''}): ${confirmed.reason}`
        const fixed = await testFixFor(n, testConflictsOf(r))
        if (fixed.toMe.length) return await parkTestFix(n, rec, fixed.toMe, fixed.records)
        log(`Task ${n}: ${fixed.fixed.length ? `test fix landed for ${fixed.fixed.join(', ')}` : 'the tests are correct'}; retrying the task`)
        budgetBase = retryNo()
        working.add(n)
        r = await call('task', `${retryPrompt(n, { ...r, failures: fixed.findings }, retryNo(), claimFlags('retry', codeOpts(n), retryNo() + 1))}\n${afterFixNote(fixed.fixed, fixed.findings)}`, { label: `${label(n)} retry ${retryNo()}`, phase: phaseOf(n), schema: RESULT, ...codeOpts(n) })
        rec.attempts++
        lastModel = codeOpts(n).model
        stop = guardStopOf(r)
        rec.startedAt = rec.startedAt || (r && r.startedAt)
        const again = await ladder()
        if (again) return again
        if (testConflictOf(r)) {
          rec.result = r
          rec.finishedAt = r && r.finishedAt
          releaseSlot(n)
          return await parkTestFix(n, rec, [`the task still reports a test that can never pass after its test fix: ${trim(testConflictOf(r).file, 120)}: ${trim(testConflictOf(r).reason, 300)}`], fixed.records)
        }
      }
    }
    if (!ok(r) && busy(r)) {
      const bo = worktreeOutcome(r)
      rec.busy = true
      rec.reason = bo.reason
    }
    releaseSlot(n)
    // Review before landing (4.1-4.6). A safety task is reviewed alone, here. A
    // non-risky task is reviewed with the other unreviewed tasks of its landing
    // batch (reviewBatch, in landBatch) before that batch merges; one it flags
    // comes back here (m.review) and goes through the same revise loop. A task
    // whose blocking findings survive the loop never merges.
    // reviewLoop returns { exit } when runTask must return that value, else null.
    const reviewLoop = async (first, opts) => {
      let review = first
      let round = 0
      // A revision that guard-stops climbs the ladder before the review goes on.
      const revised = async (prompt, lbl, o, model) => {
        working.add(n)  // a revision takes a slot but never waits for one: the task is nearly done
        r = await call('revise', prompt, { label: lbl, phase: phaseOf(n), schema: RESULT, ...o })
        lastModel = model
        stop = guardStopOf(r)
        if (stop) await climbGuard(lastModel)
        else if (!ok(r)) await releaseClaim(n)
        releaseSlot(n)
      }
      while (review && review.verdict === 'changes' && round < REVIEW_ROUNDS && ok(r)) {
        round++
        log(`Task ${n}: reviewer asked for changes, revision ${round}/${REVIEW_ROUNDS}`)
        await revised(revisePrompt(n, review, round, claimFlags('revision', codeOpts(n), round + 1)), `${label(n)} revise ${round}`, codeOpts(n), codeOpts(n).model)
        if (stop) return { exit: await needInput() }
        if (ok(r)) review = await call('review', reviewPrompt(n, r), { label: `review ${n} round ${round + 1}`, phase: phaseOf(n), schema: REVIEW_SCHEMA, ...opts })
      }
      for (const model of ladderOf(n)) {
        if (!(review && review.verdict === 'changes' && ok(r))) break
        round++
        log(`Task ${n}: blocking findings remain, escalating the revision to ${model}`)
        await revised(revisePrompt(n, review, round, claimFlags('escalation', escalated(codeOpts(n), model), round + 1)), `${label(n)} revise ${round} (${model})`, escalated(codeOpts(n), model), model)
        if (stop) return { exit: await needInput() }
        rec.escalations.push({ stage: 'revise', model })
        if (ok(r)) review = await call('review', reviewPrompt(n, r), { label: `review ${n} round ${round + 1}`, phase: phaseOf(n), schema: REVIEW_SCHEMA, ...opts })
      }
      // model/effort: who judged the task, so an audit can count escapes (approved bugs) per reviewer tier.
      const by = { model: opts.model || 'inherited', effort: opts.effort || 'inherited', riskTag: rec.riskTag }
      const fromBatch = first && first.fromBatch != null ? { fromBatch: first.fromBatch } : {}
      rec.review = review ? { verdict: review.verdict, rounds: round, findings: review.findings, summary: review.summary, ...fromBatch, ...by } : { verdict: 'unreviewed', rounds: round, ...fromBatch, ...by }
      if (!review) log(`Task ${n}: review agent returned nothing; merging unreviewed`)
      else if (review.verdict === 'changes' && ok(r)) {
        rec.result = r
        rec.finishedAt = r && r.finishedAt
        rec.reason = `review: blocking findings remain after ${round} revision(s): ${trim(review.findings.filter(f => f.severity === 'blocking').map(f => f.issue), FINDINGS_LIMIT)}`
        return { exit: rec }
      }
      return null
    }
    const individualReview = REVIEW_ON && reviewGroups({ merged: [n], risk: RISK }).individual.length > 0
    if (ok(r) && individualReview) {
      const first = await call('review', reviewPrompt(n, r), { label: `review ${n}`, phase: phaseOf(n), schema: REVIEW_SCHEMA, ...reviewOpts(n) })
      const out = await reviewLoop(first, reviewOpts(n))
      if (out) return out.exit
    }
    rec.result = r
    rec.finishedAt = r && r.finishedAt
    if (!ok(r)) {
      if (!rec.busy) rec.reason = r ? r.blocker || `gate not passed (status ${r.status})` : 'agent died without a report'
      return rec
    }
    if (REVIEW_ON && !individualReview) pendingReview.set(n, { rec, reviewed: false })
    let m = await landTask(n, rec)
    if (m && m.review) {
      // The batch review flagged this task: it left the batch unmerged. Revise and
      // re-review it alone, then land it (its batch review is done, so it skips the next).
      const out = await reviewLoop(m.review, REVIEW)
      if (out) {
        pendingReview.delete(n)
        return out.exit
      }
      rec.result = r
      rec.finishedAt = r && r.finishedAt
      if (!ok(r)) {
        pendingReview.delete(n)
        if (!rec.busy) rec.reason = r ? r.blocker || `gate not passed after revision (status ${r.status})` : 'revision agent died without a report'
        return rec
      }
      m = await landTask(n, rec)
    }
    pendingReview.delete(n)
    rec.merge = m
    if (m && m.protected) return parkProtected(n, rec, m.protected)
    rec.mergedAt = m && m.mergedAt
    if (!m || !m.merged) {
      rec.reason = `merge failed: ${(m && m.error) || 'no result'}`
      return rec
    }
    if (REVIEW_ON && !rec.review) log(`Task ${n}: merged without a recorded review`)
    rec.ok = true
    log(`Task ${n}: done and merged into ${INTO}`)
    return rec
  } catch (e) {
    rec.reason = `error: ${e && e.message}`
    return rec
  }
}

phase('Plan')
// Overhead share (5.4): plan/review/land agent ms over all agent ms, from the
// in-script wall-clock timing around agent calls. Reviewer time includes
// `review-wave` agents (each counts as one review agent, consistent with the
// role mapping). Null when no agent time is recorded yet. Warn-only: the flag
// below never sets stopLaunching.
function buildOverhead() {
  const at = k => agentMs[k] || 0
  const total = Object.values(agentMs).reduce((s, v) => s + v, 0)
  const share = total > 0 ? (at('plan') + at('review') + at('review-wave') + at('land')) / total : null
  return { share, threshold: OVERHEAD_WARN_THRESHOLD, warned: overheadWarning(share) }
}
// Run start (9.1, 9.10, 16.2, 16.6): `ck worktree start` once, before the first task agent. It sweeps the claims of
// earlier runs, installs or checks the hooks and records the baseline. A hook problem is a result warning.
let runStart = null
const runWarnings = []
async function startRun() {
  const argv = ['worktree', 'start', SPEC, ...(RUN_ID ? ['--run', RUN_ID] : []), '--json']
  const out = await thinCk('run start', argv, { label: 'run start', phase: 'Plan', cwd: ROOT, what: `Run start step for spec ${SPEC}` })
  runStart = out.json ? out.json : { error: ckError(out) }
  if (out.json) {
    for (const c of runStart.releasedClaims || []) log(`Run start released the claim on Task ${c.task}${c.reason ? ` (${c.reason})` : ''}`)
    const h = runStart.hooks
    const problems = (h && Array.isArray(h.problems) ? h.problems : []).map(String)
    if (problems.length) {
      runWarnings.push(...problems.map(x => `hooks: ${x}`))
      log(`Run start: hook problem: ${problems.join('; ')}`)
    }
    for (const w of runStart.warnings || []) if (!runWarnings.includes(`run start: ${w}`)) runWarnings.push(`run start: ${w}`)
  } else {
    runWarnings.push(`run start failed: ${runStart.error}`)
    log(`Run start failed: ${runStart.error}`)
  }
}

// Run end (10.1, 10.3, 10.5): one `ck worktree audit` after the last step, on every outcome. A failed or unparseable
// audit is incomplete, never clean.
async function auditRun() {
  let out = null
  try {
    out = await thinCk('audit', ['worktree', 'audit', SPEC, '--json'], { label: 'audit', phase: 'Report', cwd: ROOT, what: `Run-end audit for spec ${SPEC}` })
  } catch (e) {
    out = { code: -1, json: null, stderr: String(e && e.message) }
  }
  const j = out && out.json
  if (j && typeof j === 'object' && typeof j.complete === 'boolean' && typeof j.clean === 'boolean') return j
  const error = j && j.error ? String(j.error) : ckError(out || {})
  log(`Run-end audit could not complete: ${trim(error, 300)}`)
  return { schema: 'ck-audit/1', spec: SPEC, into: INTO, complete: false, clean: false, drift: [], problems: [`audit did not complete: ${trim(error, 300)}`] }
}

async function mainRun() {
const startPlan = await requestPlan('start')
if (!startPlan) return { halted: true, refused: false, reason: PLAN_FAILED, into: INTO, degraded, planFallbacks, overhead: buildOverhead(), directCk: directCkOut(), thinAgent: thinAgentOut() }
if (startPlan.cycles.length || startPlan.verifyCycles.length) {
  log('Refusing to launch: the plan has a cycle. Fix tasks.md (see `ck lint`; `ck relink <spec> <task> --apply` repairs a verify-order cycle when it can) and run again.')
  return {
    halted: true,
    refused: true,
    reason: 'plan has a cycle; nothing was started',
    cycles: startPlan.cycles,
    verifyCycles: startPlan.verifyCycles,
    into: INTO,
    degraded,
    planFallbacks,
    overhead: buildOverhead(),
    directCk: directCkOut(), thinAgent: thinAgentOut(),
  }
}
await startRun()
const initialPlan = startPlan
const initiallyDone = startPlan.done.length
doneAtStart = new Set(startPlan.done.map(String))
log(`${startPlan.remaining.length} task(s) remaining, ${initiallyDone} already Done; starting ${Math.min(startPlan.ready.length, MAX_CONCURRENT)}`)
if (PROGRESS_RUN) log(ETA_LINE || 'ETA unavailable')
schedule()
poked = false

// Wakes on a settled agent call, a freed slot, a plan result or a finished task. The wake-up waits a few
// microtask hops, so the continuation of the call that settled makes its own follow-up request (a review
// after an implementer, a land after an approval) before free slots are counted (3.9).
// A held start with agents still in flight stays alive too: any of them settling re-runs the start decision (NFR-6).
while (running.size || planRun || (deferring && inFlight.length)) {
  await signal()
  poked = false
  for (let i = 0; i < 10; i++) await null
  schedule()
}
await landingIdle
for (const n of Object.keys(records)) maybeCrossTest(n, true)  // waves whose other test tasks never finished
await Promise.all(crossTestRuns)

// Final full gate on the target, unless it is already known to be red.
let finalGate = null
if (mergedCount && !redTarget) {
  phase('Final gate')
  const g = await attributedGate('final', mergedCount, 'final gate', 'Final gate', `Verification (final) for spec ${SPEC}`)
  gates.push(g)
  let fix = null
  if (g.state === 'red') {
    log(`Final gate red, one fix attempt`)
    fix = await fixAndRecheck('final gate', g, mergedCount, 'Final gate')
  }
  g.fix = fix
  const green = g.green || !!(fix && fix.green)
  finalGate = { ...g, green, fix }
  if (g.state === 'error') {
    finalGateError = true
    log(`Final gate could not run (${g.error})`)
  }
  if (!green) redTarget = true
}

// --- report ------------------------------------------------------------------

const taskReport = Object.values(records).map(r => ({
  task: r.task,
  wave: r.wave,
  title: r.title,
  ok: r.ok,
  attempts: r.attempts,
  resolves: r.resolves || 0,
  startedAt: r.startedAt,
  finishedAt: r.finishedAt,
  mergedAt: r.mergedAt,
  agentMinutes: minutes(r.startedAt, r.finishedAt),
  toMergedMinutes: minutes(r.startedAt, r.mergedAt),
  reason: r.reason,
  summary: r.result && r.result.summary,
  wrongTests: r.result && r.result.wrongTests,
  testConflict: r.testConflict || null,
  testConflicts: testConflictsOf(r.result),
  failures: (r.result && Array.isArray(r.result.failures) && r.result.failures) || [],
  outsideFiles: r.result && r.result.outsideFiles,
  deviations: r.result && r.result.deviations,
  fixedAfterMerge: !!r.fixes,
  review: r.review,
  escalations: r.escalations || [],
  parked: !!r.parked,
  riskTag: r.riskTag || (isSafety(r.task) ? 'safety' : 'normal'),
  startModel: r.startModel || null,
  reviewModel: r.reviewModel || null,
}))
const spans = {}
for (const t of taskReport) {
  const key = t.wave ? `Wave ${t.wave}` : 'Tasks'
  const end = t.mergedAt || t.finishedAt
  if (!t.startedAt) continue
  const s = spans[key] || (spans[key] = { group: key, tasks: 0, start: t.startedAt, end })
  s.tasks++
  if (Date.parse(t.startedAt) < Date.parse(s.start)) s.start = t.startedAt
  if (end && (!s.end || Date.parse(end) > Date.parse(s.end))) s.end = end
}
const phases = Object.values(spans).map(s => ({ ...s, minutes: minutes(s.start, s.end) }))
const starts = taskReport.map(t => t.startedAt).filter(Boolean).sort((a, b) => Date.parse(a) - Date.parse(b))
const ends = taskReport.map(t => t.mergedAt || t.finishedAt).filter(Boolean).sort((a, b) => Date.parse(a) - Date.parse(b))
// What merged, failed, is blocked or never started comes from git (`ck worktree report`:
// merge commits on INTO plus tasks.md there), not from agent claims: the 0.9.0 run reported
// 11 merged while git had 19, and bench v2 listed Task 17 as merged and as "merge failed".
// Agent results only annotate it (reasons). Without a usable git report, the agents' results
// are used, deduplicated, and reportSource says so.
phase('Report')
let gitReport = null
{
  const out = await thinCk('report', ['-C', ROOT, 'worktree', 'report', SPEC, '--into', INTO, '--json'], { label: 'report', phase: 'Report', cwd: ROOT, what: `Report step for spec ${SPEC}` })
  gitReport = out.json ? out.json : { error: ckError(out) }
}
const GIT_LISTS = ['merged', 'done', 'failed', 'blocked', 'unstarted']
const fromGit = !!gitReport && !gitReport.error && GIT_LISTS.every(k => Array.isArray(gitReport[k]))
const ids = list => (list || []).map(e => taskId(e && typeof e === 'object' ? e.task : e))
const byNum = (a, b) => (Number(a) - Number(b)) || String(a).localeCompare(String(b))
const startDone = new Set((initialPlan.done || []).map(taskId))
// Tasks that need my input (guard-stopped through the ladder, parked or asked about) are listed apart from
// merged, failed and blocked, whatever else the git report says about them (7.5).
const needsInputIds = new Set([...parked.keys(), ...(fromGit ? ids(gitReport.needsInput) : [])].map(taskId))
const needsInput = [...parked.values()].map(e => ({ ...e, task: taskId(e.task) }))
for (const t of needsInputIds) if (!needsInput.some(e => e.task === t)) needsInput.push({ task: t, file: '', kind: '', test: '', requirement: '', decision: 'see the park note (ck worktree report)', action: 'park', notification: null })
needsInput.sort((a, b) => byNum(a.task, b.task))
const agentFailed = new Map([...failed.entries()].map(([t, r]) => [taskId(t), r]))
let merged, failedList, notStarted, blocked = [], readyLeft = null
if (fromGit) {
  const landed = new Set([...ids(gitReport.merged), ...ids(gitReport.done)])
  merged = [...landed].filter(n => !startDone.has(n))
  for (const [t, r] of agentFailed) {
    if (!landed.has(t)) continue
    log(`Task ${t}: an agent reported "${String(r).slice(0, 200)}", but git shows it merged; reporting it merged`)
    agentFailed.delete(t)
  }
  const gitFailed = new Map((gitReport.failed || []).map(e => [taskId(e.task), e]))
  const failedTasks = [...new Set([...agentFailed.keys(), ...gitFailed.keys()])].filter(n => !landed.has(n) && !needsInputIds.has(n))
  failedList = failedTasks.map(task => {
    const g = gitFailed.get(task)
    const reason = agentFailed.get(task) || `${g.ahead || 'some'} commit(s) on ${g.branch || BR(task)} never landed${g.claim ? `; claimed: ${g.claim}` : ''}`
    return g ? { task, reason, git: { branch: g.branch, ahead: g.ahead, claim: g.claim } } : { task, reason }
  })
  const failedSet = new Set(failedTasks)
  blocked = (gitReport.blocked || []).map(e => ({ task: taskId(e.task), on: ids(e.on) })).filter(b => !failedSet.has(b.task))
  readyLeft = ids(gitReport.unstarted).filter(n => !failedSet.has(n) && !needsInputIds.has(n))
  notStarted = [...readyLeft, ...blocked.map(b => b.task)].sort(byNum)
} else {
  log(`Report: no usable git report (${(gitReport && gitReport.error && String(gitReport.error).slice(0, 300)) || 'no result'}); using the agents' results`)
  merged = taskReport.filter(t => t.ok).map(t => t.task)
  const mergedSet = new Set(merged)
  failedList = [...agentFailed.entries()].filter(([t]) => !mergedSet.has(t) && !needsInputIds.has(t)).map(([task, reason]) => ({ task, reason }))
  const failedSet = new Set(failedList.map(f => f.task))
  notStarted = (current.remaining || []).map(taskId).filter(n => !records[n] && !running.has(n) && !mergedSet.has(n) && !failedSet.has(n))
}
// Blocked: the tasks every blocked task ultimately waits on, with why each one is stuck.
const onOf = new Map(blocked.map(b => [b.task, b.on]))
const rootsOf = (n, seen = new Set()) => (seen.has(n) ? [] : (seen.add(n), (onOf.get(n) || []).flatMap(d => (onOf.has(d) ? rootsOf(d, seen) : [d]))))
const roots = new Map()
for (const b of blocked) for (const r of new Set(rootsOf(b.task))) roots.set(r, [...(roots.get(r) || []), b.task])
const failedReason = new Map(failedList.map(f => [f.task, f.reason]))
const why = t => failedReason.get(t) || (readyLeft && readyLeft.includes(t) ? 'ready but not started' : 'not Done on the target')
const blockedOn = fromGit
  ? [...roots.entries()].filter(([task]) => !needsInputIds.has(task)).sort((a, b) => byNum(a[0], b[0])).map(([task, blocks]) => ({ task, reason: why(task), blocks: blocks.sort(byNum) }))
  : failedList.map(f => ({ task: f.task, reason: f.reason, blocks: [] }))
// Notification status of the parks (7.3, 7.4): a missing or failed notification never stops the run.
const NOTIFY_HINT = 'no notification command is set: set one once per machine with: ck agents set run.notify_command "<command>"'
const notifications = { sent: 0, failed: [], notConfigured: false, hint: null }
for (const e of needsInput) {
  if (e.action !== 'park') continue
  if (e.notification === 'sent') notifications.sent++
  else if (e.notification === 'not_configured') { notifications.notConfigured = true; notifications.hint = NOTIFY_HINT }
  else if (e.notification === 'failed') notifications.failed.push({ task: e.task, detail: e.notificationDetail || '' })
}
// Outcome: "complete" (nothing left), "blocked" (nothing left that can start: a clean stop with
// what it waits on), or "halted" (a red target, a halt on failure, or a failed plan step that
// left a ready task unstarted). A failed plan step with nothing ready left is not a halt.
let outcome = 'complete'
let reason = stopReason
const planFailed = stopReason === PLAN_FAILED
if (redTarget) {
  outcome = 'halted'
  if (finalGateError) reason = 'final gate could not run'
}
else if (stopReason && !planFailed && notStarted.length) outcome = 'halted'
else if (planFailed && (!fromGit || readyLeft.length)) outcome = 'halted'
else if (notStarted.length || failedList.length || needsInput.length) {
  outcome = 'blocked'
  if (planFailed) log('A planning step failed, but every remaining task was blocked anyway: nothing was left to start')
  const top = blockedOn.length ? blockedOn : failedList
  const waiting = needsInput.length ? `needs input: ${needsInput.map(e => `Task ${e.task}`).join(', ')}` : ''
  reason = top.length
    ? `blocked on: ${top.map(b => `Task ${b.task} (${String(b.reason).slice(0, 300)})`).join('; ')}${waiting ? `; ${waiting}` : ''}`
    : waiting || `not started: ${notStarted.map(n => `Task ${n}`).join(', ')}`
} else if (planFailed) reason = null
if (outcome === 'blocked') log(reason)
if (relinks.length) log(`Relinked mid-run (verify-order cycles): ${relinks.map(r => `Task ${r.task}`).join(', ')}`)
if (DIRECT_CK) log(`Direct ck steps (no agent): ${directCk.plan} plan, ${directCk.land} land, ${directCk.relink} relink, ${(directCk.ms / 60000).toFixed(1)} min${directCk.failures ? `, ${directCk.failures} without JSON` : ''}`)
// Task 86 (4.10): every test fix this run tried, as TestFixRecords (they replace the old report-only entries).
const testFixes = testFixRecords
if (testFixes.length) {
  log(`Test fixes (${testFixes.length}): ${testFixes.map(f => `${f.fix || 'ledger'} for ${f.blocked ? `Task ${f.blocked}` : 'cross-test'} ${f.outcome}`).join('; ')}`)
  for (const f of testFixes.filter(x => x.outcome === 'to-me')) log(`  ${f.fix || 'not started'}: ${f.tests.join(', ')}: ${trim(f.reason, 300)}`)
}
// Deterministic-fallback report section (5.1, 6.1): degraded mark, fallback
// count and the triggering plan attempts.
if (planFallbacks.length) {
  log(`Deterministic fallback: degraded, ${planFallbacks.length} fallback(s): ${planFallbacks.map(e => `plan ${e.planStep} (${e.trigger}, ${e.source || 'no data'})`).join('; ')}`)
} else {
  log('Deterministic fallback: clean run, no fallbacks')
}
// Overhead report line (5.4, warn-only): the flag is recorded and logged, and
// never sets stopLaunching.
const overhead = buildOverhead()
if (overhead.share == null) {
  log('Overhead (plan/review/land share): not recorded')
} else {
  const pct = `${(overhead.share * 100).toFixed(0)}%`
  const thr = `${overhead.threshold * 100}%`
  log(`Overhead (plan/review/land share): ${pct}${overhead.warned ? ` (warn: above ${thr} threshold)` : ''}`)
}

return {
  halted: outcome === 'halted',
  outcome,
  reason,
  into: INTO,
  reportSource: fromGit ? 'git' : 'agents',
  head: fromGit ? gitReport.head || null : null,
  taskAgents: { model: IMPL.model || 'inherited', effort: IMPL.effort || 'inherited' },
  agents: { implementer: desc(IMPL), test_writer: desc(TEST), fixer: desc(FIX), reviewer: REVIEW_ON ? desc(REVIEW) : 'off', reviewer_risky: REVIEW_ON ? desc(RISKY_REVIEW) : 'off', orchestrator: desc(LOW), resolver: desc(RESOLVE) },
  targetGreen: !redTarget,
  merged,
  failed: failedList,
  notStarted,
  blocked,
  blockedOn,
  needsInput,
  notifications,
  escalated: taskReport.filter(t => t.escalations.length).map(t => ({ task: t.task, models: t.escalations.map(e => e.model), ok: t.ok })),
  fixerEscalations,
  timing: {
    start: starts[0] || null,
    end: ends[ends.length - 1] || null,
    minutes: minutes(starts[0], ends[ends.length - 1]),
    phases,
  },
  tasks: taskReport,
  landings,
  fullGates,
  gates,
  testConflicts: taskReport.flatMap(t => t.testConflicts),
  finalGate,
  crossTests,
  degraded,
  planFallbacks,
  overhead,
  relinks,
  testFixes,
  directCk: directCkOut(), thinAgent: thinAgentOut(),
  stats: { initiallyDone, merged: merged.length, failed: failedList.length, notStarted: notStarted.length, planSteps: refills, planFailures, planFallbacks: planFallbacks.length, fixes: fixCount, landings: landings.length, bisected: landings.filter(l => l.bisected).length },
}
}

let mainResult = null
let mainError = null
try { mainResult = await mainRun() } catch (e) { mainError = e }
const audit = await auditRun()
if (mainError) {
  try { mainError.audit = audit } catch {}
  throw mainError
}
return { ...mainResult, runStart, runWarnings, sweep: runStart && !runStart.error ? { releasedClaims: runStart.releasedClaims || [], keptClaims: runStart.keptClaims || [] } : null, audit }
