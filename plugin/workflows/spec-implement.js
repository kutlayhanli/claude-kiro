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
const ROLE_NAMES = ['implementer', 'test_writer', 'reviewer', 'reviewer_risky', 'fixer', 'orchestrator', 'resolver', 'arbiter', 'arbiter_risky']
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
// and with design.md. On unless reviewer.cross_test is false. A finding blocks: it becomes a test conflict
// for the test-fixer, and the impl tasks its tests verify wait until the fix lands (7.1-7.5).
const CROSS_TEST = !!A.agents && REVIEWER.cross_test !== false
const ORCH = roleOpts('orchestrator', ROLES.orchestrator, null)
const LOW = { effort: 'low', ...ORCH }
const RESOLVE = roleOpts('resolver', ROLES.resolver, null)
// The arbiter (run-principles Story 6): `arbiterAgent` is the agent type that rules, `agents.arbiter` its role
// settings. A claim starts at the normal tier, or at the risky one when `ck arbiter open` says so (5.3-5.5). An absent
// `arbiterAgent` means the arbiter cannot be called, so a stop that needs it is a needs-input entry (6.10).
const ARBITER_AGENT = typeof A.arbiterAgent === 'string' && A.arbiterAgent ? A.arbiterAgent : null
const ARBITER = ROLES.arbiter || {}
const arbiterOpts = tier => (tier === 'risky'
  ? roleOpts('arbiter.risky', { model: ARBITER.risky_model || ARBITER.model, effort: ARBITER.risky_effort || ARBITER.effort }, null, 'arbiter_risky')
  : roleOpts('arbiter', ARBITER, null))
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

// The arbiter's answer (design.md RULING). `outcome` is unconstrained so that a malformed answer reaches `ck arbiter
// settle` and my needs-input entry instead of failing in the agent tool (7.2). The prompt states the claim on its own
// line (`Claim key: <key>`) and the arbiter copies it into `claim`; settle compares the two (7.1).
const RULING = {
  type: 'object',
  properties: {
    claim: { type: 'string', description: 'the claim key you were given, copied' },
    status: { type: 'string', description: 'ruled | needs_stronger_look' },
    outcome: { description: 'spec_edit | test_repair | code | requirements' },
    reasoning: { type: 'string' },
    principle: { type: 'string', description: 'the id of the principle you upheld' },
    reservedBy: { type: 'string' },
    established: { type: 'string', description: 'with needs_stronger_look: what you did establish' },
    changed: { type: 'string', description: 'spec_edit: what you changed' },
    commits: { type: 'string' },
    testRepair: { type: 'object', properties: { file: { type: 'string' }, test: { type: 'string' }, change: { type: 'string' }, why: { type: 'string' } } },
    code: { type: 'string', description: 'code: what the implementer must do' },
    requirement: { type: 'object', properties: { kind: { type: 'string' }, wording: { type: 'string' }, reason: { type: 'string' } } },
    settles: { type: 'array', items: { type: 'string' }, description: 'review deadlock: the findings this ruling settles' },
    verdict: { type: 'string', description: '/spec:create only' },
  },
  required: ['status', 'reasoning'],
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
          test: { type: 'string', description: 'the test the finding is about (a test finding names its file and test)' },
          issue: { type: 'string', description: 'what is wrong, and the spec text it contradicts' },
          fix: { type: 'string', description: 'what the implementer should change' },
          noRequirement: { type: 'boolean', description: 'true when the code serves no requirement or design.md row: it is minor, never blocks, and is a proposed cut' },
          settledBy: { type: 'integer', description: 'the ruling number of the arbiter ruling that already settled this finding, when you restate it' },
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
  const argv = ['-C', TARGET, 'plan', SPEC, '--json', '--compact']
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

const reviewPrompt = (n, r, extra = '') => `Review Task ${n} of spec ${SPEC}${TITLES[n] ? ` ("${TITLES[n]}")` : ''} before it merges. You review; you do not edit, commit, or run the test suite.
Worktree: ${WT(n)} (branch ${BR(n)}), to merge into ${INTO}. ${GIT_NOTE}
1. Read the change: command git -C ${WT(n)} diff ${INTO}...${BR(n)}  (and its file list with --stat).
2. Read Task ${n}'s block in ${WT(n)}/specs/${SPEC}/tasks.md, the requirements it covers in requirements.md, and the parts of design.md it implements (interfaces, file paths, error handling).
3. The task's gate already passed, so do not re-check what the tests check. Look for what tests miss: behaviour that contradicts design.md or a requirement, a public interface that differs from design.md, missing error handling the design specifies, edits outside the task's files, weakened or skipped tests, and security problems.
${TRACKS[n] === 'test' ? `${CONTRACT_REVIEW(n)}\n${extra}` : ''}4. Flag code that serves no requirement: a function, branch, file or setting that no requirement or design.md row asks for. Mark each such finding noRequirement true; it is minor, never blocks, and is reported as a proposed cut.
The implementer reported: ${trim(r && { summary: r.summary, deviations: r.deviations, outsideFiles: r.outsideFiles }, 2000)}
Mark a finding "blocking" only if it should stop the merge; style and taste are "minor". verdict is "changes" only if there is at least one blocking finding.`

// Task 87 (8.3-8.5, 15.6): what ck found out about a test task's files, for its review. Row fidelity: only the
// unparsed and unmatched items (a checked one needs no judgment); no section when both are empty. Fixtures: every
// unlabeled recorded fixture is a blocking finding. Read through the direct ck global; without one nothing is added.
const asList = v => (Array.isArray(v) ? v : [])
const WANT_EXTRA = n => TRACKS[n] === 'test' && !!DIRECT_CK
async function testReviewExtra(n) {
  if (TRACKS[n] === 'test' && !DIRECT_CK) {
    const w = `Task ${n}: no direct ck, so the row-fidelity and fixture checks were not added to its review`
    if (!runWarnings.includes(w)) runWarnings.push(w)
  }
  if (!WANT_EXTRA(n)) return ''
  let text = ''
  try {
    const fid = await runCk('lint', ['-C', WT(n), 'lint', SPEC, '--fidelity', '--task', String(n), '--json'], WT(n))
    const unparsed = fid.json ? asList(fid.json.unparsed) : []
    const unmatched = fid.json ? asList(fid.json.unmatched) : []
    if (unparsed.length || unmatched.length) {
      text += `Row fidelity: ck could not check these items against their test-plan rows, so judge each one: is the value or name the test uses what its row says? A mismatch is a blocking finding.\n${unparsed.length ? `Unparsed (a value the row states in a form ck cannot read): ${JSON.stringify(unparsed)}\n` : ''}${unmatched.length ? `Unmatched (a test no row names): ${JSON.stringify(unmatched)}\n` : ''}`
    }
  } catch (e) { /* no fidelity section without an answer */ }
  try {
    const fx = await runCk('fixtures', ['-C', WT(n), 'fixtures', 'status', SPEC, '--task', String(n), '--json'], WT(n))
    const unlabeled = fx.json ? asList(fx.json.unlabeled) : []
    if (unlabeled.length) text += `Unlabeled fixtures: these recorded fixtures have no capture metadata and are not marked handcrafted. Each one is a blocking finding (file: the fixture, fix: label it with "ck fixtures mark"): ${JSON.stringify(unlabeled)}\n`
  } catch (e) { /* no fixture section without an answer */ }
  return text
}

// Wave-batched review (4.1): one agent over the combined diff of the wave's
// non-risky tasks. Names the wave, its tasks, the per-task diffs (and the
// combined `git diff <merge-base>...<INTO>` range over their files), each
// task's block/requirements, and design.md. The verdict echoes its coverage
// in `tasks` and flags individual tasks via per-task findings.
const waveReviewPrompt = (wave, tasks, extras = {}) => `Review Wave ${wave} of spec ${SPEC} before it merges: non-risky Tasks ${tasks.join(', ')} are about to land together into ${INTO}. You review; you do not edit, commit, or run the test suite.
${GIT_NOTE}
1. Read the combined change: ${tasks.map(n => `command git -C ${WT(n)} diff ${INTO}...${BR(n)}`).join('; ')} (and each file list with --stat), or the combined range \`git diff <merge-base>...${INTO}\` over their files.
2. Read each task's block in specs/${SPEC}/tasks.md, the requirements it covers in requirements.md, and design.md (interfaces, file paths, error handling).
3. The tasks' gates already passed, so do not re-check what the tests check. Look for what tests miss: behaviour that contradicts design.md or a requirement, a public interface that differs from design.md, missing error handling the design specifies, edits outside the tasks' files, weakened or skipped tests, and security problems.
${tasks.map(n => TRACKS[n] === 'test' ? `${CONTRACT_REVIEW(n)}\n${extras[n] || ''}` : '').join('')}Return verdict ("approve" or "changes") with per-task findings [{ task, severity, file, issue, fix }] and name every covered task in "tasks". Mark a finding "blocking" only if it should stop the wave; style and taste are "minor". verdict is "changes" only if there is at least one blocking finding.`

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
const testFixPrompt = (k, n, entries) => `${MANDATE}You are a test-fixer for spec ${SPEC}. ${n != null ? `Task ${n} is blocked because` : 'A cross-test check found that'} these tests can never pass with code that meets the spec (every entry, one per test; the "fix" is the correction a first agent proposed, or, when the entry has a "ruling", the change the arbiter ruled on it: do that change):
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

// Task 87 (7.4): the impl tasks held while a cross-test finding is open: those verified by the test file each
// finding names. A finding names its file in `file`, or as the node-id part of `test` ("file::name"); a bare
// test name places nothing. Paths compare in one surface form (slashes, ./, case): holding too much is safe.
function heldByCrossTest({ findings, testVerifies } = {}) {
  const norm = p => String(p == null ? '' : p).trim().replace(/\\/g, '/').split('/').filter(x => x !== '' && x !== '.').join('/').toLowerCase()
  const tv = testVerifies && typeof testVerifies === 'object' && !Array.isArray(testVerifies) ? testVerifies : {}
  const byFile = new Map()
  for (const [k, v] of Object.entries(tv)) {
    const f = norm(k)
    if (!f || !Array.isArray(v)) continue
    byFile.set(f, [...(byFile.get(f) || []), ...v.map(String)])
  }
  const held = new Set()
  for (const x of Array.isArray(findings) ? findings : []) {
    if (!x || typeof x !== 'object') continue
    const test = x.test == null ? '' : String(x.test)
    const f = norm(x.file) || (test.includes('::') ? norm(test.split('::')[0]) : '')
    if (!f) continue
    for (const t of byFile.get(f) || []) held.add(t)
  }
  return held
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
// Live command edges (12.5, Task 93) join the deps: a waiting task depends on the provider it waits for. A provider
// the plan does not list as a key still gets one, so the edge can be met once the provider is Done.
function applyPlanView(plan, mergedInRun, liveDeps) {
  const deps = { ...(plan.deps || {}) }
  for (const [t, ps] of Object.entries(liveDeps || {})) {
    deps[t] = [...new Set([...decisionIds(deps[t]), ...decisionIds(ps)])]
    for (const p of decisionIds(ps)) if (!hasKey(deps, p)) deps[p] = []
  }
  return {
    order: decisionIds(plan.remaining),
    deps,
    done: new Set([...decisionIds(plan.done), ...decisionIds(mergedInRun)]),
    cycles: (plan.cycles || []).map(c => decisionIds(c)),
    verifyCycles: [...(plan.verifyCycles || [])],
    // Task 94: ck plan's write-set overlap map, read by writeSetHold; holds never change deps.
    overlaps: plan.overlaps && typeof plan.overlaps === 'object' && !Array.isArray(plan.overlaps) ? plan.overlaps : {},
  }
}

// Task 93 (12.5-12.7): what a task whose gate failed on a missing command does next. Pure: the caller records it.
//   none      no failure carries a missing-command mark, or every provider is Done or the task itself
//   wait      edges: [{task, dependsOn}] to the unfinished providers that do not close a cycle
//   conflict  no task provides the command: tests are the invoking tests (a test conflict)
//   blocked   every unfinished provider would close a cycle: cycle names it, e.g. [2, 5, 2]
// `refused` lists the providers whose edge would close a cycle. When it is not empty, the caller blocks the task
// (12.7) and records no edge, even if other providers are safe. A provider is a task of the run (in deps) only.
function liveEdgeDecision({ task, failures, deps, done, liveDeps } = {}) {
  const v = String(task)
  const marked = (Array.isArray(failures) ? failures : []).filter(f => f && typeof f === 'object' && f.missingCommand && typeof f.missingCommand === 'object')
  const none = { action: 'none', edges: [], cycle: null, command: null, tests: [], refused: [] }
  if (!marked.length) return none
  const command = marked[0].missingCommand.command == null ? null : String(marked[0].missingCommand.command)
  const providers = [...new Set(marked.flatMap(f => decisionIds(f.missingCommand.providers)))]
  if (!providers.length) {
    const tests = marked.map(f => ({ file: f.file || '', test: f.node || '', reason: f.message || '' }))
    return { ...none, action: 'conflict', command, tests }
  }
  const depMap = deps && typeof deps === 'object' ? deps : {}
  const doneSet = new Set(decisionIds(done))
  // The graph the edges are judged in: declared deps plus the live edges already added.
  const graph = new Map()
  for (const [k, ds] of Object.entries(depMap)) graph.set(String(k), decisionIds(ds))
  for (const [k, ds] of Object.entries(liveDeps || {})) graph.set(String(k), [...new Set([...(graph.get(String(k)) || []), ...decisionIds(ds)])])
  const reaches = (from, goal) => {
    const seen = new Set()
    const stack = [from]
    while (stack.length) {
      const t = stack.pop()
      if (t === goal) return true
      if (seen.has(t)) continue
      seen.add(t)
      stack.push(...(graph.get(t) || []))
    }
    return false
  }
  // The path from `from` to `goal` over the graph, as ids; [] when there is none.
  const pathTo = (from, goal) => {
    const parent = new Map([[from, null]])
    const queue = [from]
    while (queue.length) {
      const t = queue.shift()
      if (t === goal) {
        const path = []
        for (let x = goal; x !== null; x = parent.get(x)) path.unshift(x)
        return path
      }
      for (const m of graph.get(t) || []) if (!parent.has(m)) { parent.set(m, t); queue.push(m) }
    }
    return []
  }
  const candidates = providers.filter(p => p !== v && !doneSet.has(p) && hasKey(depMap, p))
  if (!candidates.length) return { ...none, command }
  // A provider that already depends on the task would close the cycle v -> p -> ... -> v.
  const refused = candidates.filter(p => reaches(p, v))
  const safe = candidates.filter(p => !refused.includes(p))
  const cycle = refused.length ? [v, ...pathTo(refused[0], v)] : null
  if (!safe.length) return { action: 'blocked', edges: [], cycle, command, tests: [], refused }
  return { action: 'wait', edges: safe.map(p => ({ task: v, dependsOn: p })), cycle, command, tests: [], refused }
}

// Task 94 (13.1-13.3, 13.5, 13.6): write-set holds. Pure: the caller records and logs them; no dependency changes.
//   start  the ready tasks that may start now, in the scheduler's order
//   held   [{task, waitsOn}]: the rest, each with the first earlier task it overlaps
// A task waits for every in-flight task it overlaps (`active`), and for every earlier ready task it overlaps, whether
// that one starts or is itself held (13.2: the higher-priority one goes first, the others wait for it). So a task
// that overlaps a held higher-priority task is held too, and `waitsOn` names that task.
// `overlaps` is ck plan's map {task: [task]}. An overlap counts in both directions, so an asymmetric map still keeps
// the pair apart; a task never overlaps itself. A task with no overlaps entry (no Files, 13.4) is never held.
function writeSetHold({ ready, active, overlaps } = {}) {
  const ov = overlaps && typeof overlaps === 'object' && !Array.isArray(overlaps) ? overlaps : {}
  const partners = new Map()  // task -> Set of tasks it overlaps, both directions
  const link = (a, b) => {
    if (a === b) return
    if (!partners.has(a)) partners.set(a, new Set())
    partners.get(a).add(b)
  }
  for (const [a, bs] of Object.entries(ov)) {
    for (const b of decisionIds(bs)) { link(String(a), b); link(b, String(a)) }
  }
  const ahead = decisionIds(active)  // in flight first, then every earlier ready task in priority order
  const start = []
  const held = []
  for (const n of decisionIds(ready)) {
    const mine = partners.get(n)
    const blocker = mine ? ahead.find(x => x !== n && mine.has(x)) : undefined
    if (blocker === undefined) start.push(n)
    else held.push({ task: n, waitsOn: blocker })
    ahead.push(n)
  }
  return { start, held }
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

const UNBLOCKER_KINDS = ['review', 'revise', 'land', 'arbiter']

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
// Nodes and files are compared by their canonical form (design: Node canonicalisation), as ck's ledger keys them.
function testFixPlan({ conflicts, alreadyFixed } = {}) {
  const nodeOf = c => canonicalNode(c.test ? `${c.file}::${c.test}` : String(c.file))
  const fixed = new Set((Array.isArray(alreadyFixed) ? alreadyFixed : []).map(e => canonicalNode(String(e && typeof e === 'object' ? e.node : e))))
  const seen = new Set()
  const groups = []
  const toMe = []
  for (const c of Array.isArray(conflicts) ? conflicts : []) {
    if (!c || typeof c !== 'object' || !c.file) continue
    const k = nodeOf(c)
    if (seen.has(k)) continue
    seen.add(k)
    if (fixed.has(k)) { toMe.push(c); continue }
    const fk = canonicalNode(String(c.file))
    let g = groups.find(x => x.key === fk)
    if (!g) { g = { file: c.file, conflicts: [] }; Object.defineProperty(g, 'key', { value: fk, enumerable: false }); groups.push(g) }
    g.conflicts.push(c)
  }
  return { groups, toMe }
}

// The JS mirror of ck's canonical_node (testfix.py): `\\` becomes `/`, repeated slashes and `./` / `..` segments are
// normalised, a trailing `[param]` id is dropped, and the result is lower-cased.
function canonicalNode(node) {
  const text = String(node ?? '').trim().replace(/\\/g, '/')
  const i = text.indexOf('::')
  let path = i < 0 ? text : text.slice(0, i)
  const sep = i < 0 ? '' : '::'
  let rest = i < 0 ? '' : text.slice(i + 2)
  path = path.replace(/\/{2,}/g, '/')
  if (path) {
    const abs = path.startsWith('/')
    const out = []
    for (const seg of path.split('/')) {
      if (seg === '' || seg === '.') continue
      if (seg === '..' && out.length && out[out.length - 1] !== '..') out.pop()
      else if (seg === '..' && abs) continue
      else out.push(seg)
    }
    path = (abs ? '/' : '') + out.join('/') || (abs ? '/' : '.')
  }
  rest = rest.replace(/\[.*\]\s*$/, '').trim()
  return (path + sep + rest).toLowerCase()
}

// Which stop of a task attempt goes to the arbiter (6.1-6.7, 6.11, 6.13), or null when none does. A busy or failed
// worktree never does (6.7). A live `wait` with nothing refused is today's live edge and never does (6.4); a `blocked`
// action or any refused provider is a command cycle (D29); a live `conflict` has joined `testConflicts` already.
// An ordinary gate failure goes only once its retries and escalations are spent (6.5, 6.11).
function claimTrigger({ result, guardStop, testConflict, live, worktreeKind, retryNo, maxRetries, rungsLeft, reviewDeadlock } = {}) {
  if (worktreeKind === 'busy' || worktreeKind === 'error') return null
  if (guardStop) return 'guard-stop'
  if (reviewDeadlock) return 'review-deadlock'
  const lv = live && typeof live === 'object' ? live : {}
  const refused = Array.isArray(lv.refused) ? lv.refused : []
  if (lv.action === 'blocked' || refused.length) return 'command-cycle'
  if (testConflict) return 'test-conflict'
  if (lv.action === 'wait') return null
  const passed = !!result && result.status === 'done' && result.gatePassed === true
  if (passed) return null
  const spent = Number.isInteger(retryNo) && Number.isInteger(maxRetries) ? retryNo > maxRetries : false
  if (spent && !(Number.isInteger(rungsLeft) && rungsLeft > 0)) return 'gate-failure'
  return null
}

const isText = v => typeof v === 'string'
const isTextList = v => Array.isArray(v) && v.every(isText)

// `ck arbiter open`'s answer (OpenResult, read from out.json): the object, or null when a required field is missing or
// has the wrong type. Null makes the call a failed `ck arbiter` call (6.10), never a guessed default.
function openResultOf(json) {
  const o = json && typeof json === 'object' && !Array.isArray(json) ? json : null
  if (!o) return null
  if (o.action !== 'rule' && o.action !== 'ask') return null
  if (!isText(o.key) || !o.key || !isText(o.reason)) return null
  if (!Array.isArray(o.principles) || !o.principles.every(p => p && typeof p === 'object' && isText(p.id) && isText(p.layer) && isText(p.text))) return null
  if (!isText(o.planLayer) || !Array.isArray(o.earlier)) return null
  if (o.missing != null && !isText(o.missing)) return null
  if (o.action === 'rule') {
    if (o.tier !== 'normal' && o.tier !== 'risky') return null
    if (!isText(o.worktree) || !o.worktree || !isText(o.branch) || !isText(o.number)) return null
  }
  return o
}

// `ck worktree land SPEC arbiter<k> --json`'s one landing entry: the object, or null when a required field is missing or
// has the wrong type. Null is a failed landing (8.4), never a guessed default.
function landEntryOf(json) {
  const o = json && typeof json === 'object' && !Array.isArray(json) ? json : null
  if (!o || !['MERGED', 'CONFLICT', 'RED', 'REFUSED', 'ERROR'].includes(o.state)) return null
  if (o.state === 'MERGED' && !(isText(o.commit) && o.commit)) return null
  if (o.moved != null && !(Array.isArray(o.moved) && o.moved.every(m => m && typeof m === 'object' && isText(String(m.task)) && isText(m.file) && (m.test == null || isText(m.test))))) return null
  return o
}

// `ck arbiter settle`'s answer (SettleResult): the object, or null as above.
function settleResultOf(json) {
  const o = json && typeof json === 'object' && !Array.isArray(json) ? json : null
  if (!o) return null
  if (!['spec_edit', 'test_repair', 'code', 'needs_input', 'rerun'].includes(o.route)) return null
  if (!isText(o.reason) || typeof o.record !== 'boolean' || !isTextList(o.problems)) return null
  if (o.principle != null && !(typeof o.principle === 'object' && isText(o.principle.id) && isText(o.principle.text))) return null
  return o
}

// Task 66 (8.9, 8.10): the conflict entries a settled test-repair ruling hands to the test-fixer, one per ruling. The
// ruling is attached unchanged; it is the confirmation the fixer starts on.
function testRepairEntries(rulings) {
  return (rulings || []).map(o => {
    const ruling = (o && o.ruling) || {}
    const claim = (o && o.claim) || {}
    const t = ruling.testRepair || {}
    return {
      file: String(t.file || claim.file || ''), test: String(t.test || claim.test || ''), criterion: String(claim.criterion || ''),
      reason: String(t.why || ruling.reasoning || ''), fix: String(t.change || ''), ruling,
    }
  }).filter(e => e.file)
}

// Task 69 (4.8, 8.11): a finding that serves no requirement is minor and never blocks a landing (the code stays and it is
// a proposed cut). A review left with no blocking finding approves. Findings keep their order.
function normalizeReview(review) {
  if (!review || typeof review !== 'object') return review
  const findings = (Array.isArray(review.findings) ? review.findings : []).map(f => (f && f.noRequirement === true ? { ...f, severity: 'minor' } : f))
  const blocking = findings.some(f => f && f.severity === 'blocking')
  return { ...review, findings, verdict: review.verdict === 'changes' && !blocking ? 'approve' : review.verdict }
}

// Task 69 (8.12): removes the findings a ruling settled. `settled` holds identities `<file>#<issue>`; a finding matches one
// by its file and issue text (case, whitespace and a leading ./ do not matter). A finding the reviewer marks `settledBy`
// restates a settled finding, so it is removed too, but only when something was settled (an empty `settled` matches
// nothing). Every other finding passes, in order.
function settleFindings(review, settled) {
  if (!review || typeof review !== 'object') return review
  const normPath = s => String(s || '').replace(/^(\.\/)+/, '')
  const collapse = s => String(s || '').split(/\s+/).filter(Boolean).join(' ').toLowerCase()
  const settledIds = new Set()
  for (const s of settled || []) {
    const text = String(s)
    const k = text.indexOf('#')
    const file = normPath(k < 0 ? text : text.slice(0, k))
    settledIds.add(`${file}#${collapse(k < 0 ? '' : text.slice(k + 1))}`)
  }
  const findings = (Array.isArray(review.findings) ? review.findings : []).filter(f => {
    if (!f) return true
    const file = normPath(f.file)
    if (settledIds.has(`${file}#${collapse(f.issue)}`)) return false
    if (f.settledBy != null && settledIds.size > 0) return false
    return true
  })
  return { ...review, findings }
}

// Task 69 (8.12): the findings of a review deadlock an applied ruling of this task already covers, each with that ruling.
// `earlier` is ck's list of this task's applied rulings; the claim key of each one names the finding it settled.
function coveredFindingsOf(findings, earlier) {
  const normPath = s => String(s || '').replace(/^(\.\/)+/, '')
  const collapse = s => String(s || '').split(/\s+/).filter(Boolean).join(' ').toLowerCase()
  return (findings || []).flatMap(([file, issue]) => {
    const id = `${normPath(file)}#${collapse(issue)}`
    const e = (earlier || []).find(x => x && String(x.claim || '').includes(id))
    return e ? [{ file: String(file || ''), issue: String(issue || ''), ruling: e.ruling, outcome: e.outcome, reasoning: e.reasoning }] : []
  })
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
// Live command edges (Task 93, 12.5-12.8). liveDeps: task -> providers it waits for (merged into the plan view);
// waitingLive: task -> { on, command } while a provider is unfinished, a task not counted as failed and not retried;
// liveEdges: every edge added this run, for the result and `ck relink --commands`.
const liveDeps = {}
const waitingLive = new Map()
const liveEdges = []
const providerDone = p => view.done.has(p) || mergedInRun.has(p)
// The waiting tasks whose providers are still unfinished. A task whose providers are all Done is released (and dropped).
function stillWaitingLive() {
  for (const [n, w] of [...waitingLive]) if (w.on.every(providerDone)) waitingLive.delete(n)
  return [...waitingLive.keys()]
}
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
const findings0 = r => ((r && r.findings) || []).filter(f => f && typeof f === 'object')
const crossTestRuns = []
const crossHeld = new Set()   // impl tasks the tests of an open cross-test finding verify: not started until the fix lands (7.4)
let crossOpen = 0             // cross-test fixes in flight: they keep the scheduler loop alive without holding a slot
const crossNeedsInput = []    // a cross-test fix that went to me (no blocked task to park)
const crossTestDone = new Set()
const crossInFlight = new Set()   // waves whose check is running or whose findings are still being routed: their impl dependents wait (7.4)
let doneAtStart = new Set()
const testTasksOf = w => Object.keys(TRACKS).filter(m => TRACKS[m] === 'test' && waveOf(m) === w && !doneAtStart.has(m))
// While a wave's test tasks are still running or landing, the impl tasks that depend on one of them wait: the
// cross-test check, which comes right after the last one merges, may name tests such a task verifies (7.4). A wave
// whose test tasks cannot all merge (one failed, was parked, or waits on a failed task) is not waited for: its
// check runs at the end.
function crossWaveDoomed(m, seen = new Set()) {
  if (failed.has(m) || parked.has(m)) return true
  if (seen.has(m)) return false
  seen.add(m)
  if (view.done.has(m) || mergedInRun.has(m) || running.has(m)) return false
  return ((view.deps && view.deps[m]) || []).some(d => !view.done.has(String(d)) && !mergedInRun.has(String(d)) && crossWaveDoomed(String(d), seen))
}
function crossPendingHeld() {
  const out = new Set()
  if (!CROSS_TEST || !Object.keys(TRACKS).length) return out
  if (Object.values(TRACKS).filter(t => t === 'test').length < 2) return out
  const waves = new Set(Object.keys(TRACKS).filter(m => TRACKS[m] === 'test' && !doneAtStart.has(m)).map(m => waveOf(m)))
  for (const w of waves) {
    if (crossTestDone.has(w) && !crossInFlight.has(w)) continue
    const wave = testTasksOf(w)
    if (!wave.length || (!crossInFlight.has(w) && wave.every(m => (records[m] && !running.has(m)) || mergedInRun.has(m) || view.done.has(m))) || (!crossInFlight.has(w) && wave.some(m => crossWaveDoomed(m)))) continue
    for (const x of Object.keys((view && view.deps) || {})) {
      if (TRACKS[x] !== 'test' && view.deps[x].some(d => wave.includes(String(d)))) out.add(x)
    }
  }
  return out
}
function maybeCrossTest(n, atEnd = false) {
  if (!CROSS_TEST || TRACKS[n] !== 'test') return
  const w = waveOf(n)
  if (crossTestDone.has(w) || Object.values(TRACKS).filter(t => t === 'test').length < 2) return
  const wave = testTasksOf(w)
  if (!atEnd && !wave.every(m => records[m] && !running.has(m))) return
  const merged = wave.filter(m => records[m] && records[m].ok)
  if (!merged.length) return
  crossTestDone.add(w)
  crossInFlight.add(w)
  crossOpen++
  crossTestRuns.push((async () => {
    let r = null
    const replan = () => { if (!SCRIPT_READINESS) { idleArmed = true; requestPlan('landing') } }
    try {
    try {
      r = await call('cross-test', crossTestPrompt(merged), { label: `cross-test wave ${w}`, phase: phaseOf(merged[0]), schema: REVIEW_SCHEMA, ...RISKY_REVIEW })
    } catch (e) {
      r = null
    }
    // 7.2: every finding of the cross-test check blocks; none proceeds as today (7.5).
    const findings = ((r && r.findings) || []).filter(f => f && typeof f === 'object')
    const entry = { wave: w, tasks: merged, verdict: r ? r.verdict : 'unchecked', findings: (r && r.findings) || [], summary: r && r.summary, blocking: findings.length, routed: false }
    crossTests.push(entry)
    log(`cross-test consistency (wave ${w}): ${r ? `${findings.length} blocking finding(s)${findings.length ? `: ${trim(findings.map(f => f.issue), 600)}` : ''}` : 'the check returned nothing'}`)
    // without args.deps the plan agent's ready list is used once: a task it was filtered out of needs a new plan
    if (findings.length) {
      entry.routed = true
      try { await routeCrossFindings(w, findings, replan, entry) } catch (e) { log(`cross-test fix for wave ${w} failed: ${trim(e && e.message, 300)}`) }
    }
    } finally {
      // the hold on the wave's dependents ends only now: crossHeld is set (or there was nothing to hold)
      crossInFlight.delete(w)
      crossOpen--
      replan()  // without args.deps the plan agent's list is read once: the released tasks need a new plan
      poke()
    }
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
let view = { order: [], deps: {}, done: new Set(), cycles: [], verifyCycles: [], overlaps: {} }
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
const excluded = () => new Set([...running.keys(), ...failed.keys(), ...parked.keys(), ...mergedInRun, ...crossHeld, ...crossPendingHeld(), ...stillWaitingLive()])
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
        view = applyPlanView(n, mergedInRun, liveDeps)
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
// While it holds the lock it publishes a promise that settles on release, so a second caller waiting in the loop
// below really waits (an already-settled landingIdle would spin on microtasks and starve the holder's I/O).
async function withLandingLock(fn) {
  while (landingBusy) await landingIdle
  let release
  landingIdle = new Promise(r => { release = r })
  landingBusy = true
  try {
    return await fn()
  } finally {
    landingBusy = false
    release()
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

// A task's implementing agent finished; its slot is free. A task waiting to take a slot back (acquireSlot) gets it
// first, before the scheduler can start a new task in it.
function releaseSlot(n) {
  if (!working.delete(n)) return
  const w = slotWaiters.shift()
  if (w) { working.add(w.n); w.resolve() }
  requestPlan('freed')
  poke()
}

// A task that gave its slot up (a test fix) takes one again before its next agent runs, so the run never holds more
// than its configured slots.
const slotWaiters = []   // { n, resolve }
function acquireSlot(n) {
  if (working.has(n)) return Promise.resolve()
  if (!(working.size >= MAX_CONCURRENT)) { working.add(n); return Promise.resolve() }
  return new Promise(resolve => slotWaiters.push({ n, resolve }))
}

function startTask(n) {
  working.add(n)
  running.set(n, runTask(n).then(rec => finishTask(n, rec)))
}

// Task 93 (12.5, 12.8): records the live edges of a wait and returns the task's record as waiting (not failed).
function recordLiveWait(n, rec, r, live) {
  const task = String(n)
  const on = []
  for (const e of live.edges) {
    const p = String(e.dependsOn)
    if (on.includes(p)) continue
    on.push(p)
    liveDeps[task] = [...new Set([...(liveDeps[task] || []), p])]
    if (!liveEdges.some(x => x.task === task && x.dependsOn === p)) liveEdges.push({ task, dependsOn: p, command: live.command, at: nowMs() })
    log(`live edge Task ${task} -> Task ${p} (${live.command}): Task ${task} waits for Task ${p} to merge; no retry spent`)
  }
  waitingLive.set(task, { on, command: live.command })
  rec.result = r
  rec.finishedAt = r && r.finishedAt
  rec.waiting = { on, command: live.command }
  rec.reason = `waiting for ${on.map(p => `Task ${p}`).join(', ')} (live edge: ${live.command})`
  return rec
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
  } else if (!rec.ok && rec.waiting) {
    // Task 93 (12.5): waits for its live providers; not failed, no retry spent. It starts again after their landings.
    log(`Task ${n}: waiting for ${rec.waiting.on.map(p => `Task ${p}`).join(', ')} (${rec.waiting.command}); no retry spent`)
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

// Task 94 (13.3, 13.5): write-set holds this run made, {task, waitsOn, at}; listed in the result when there are any.
const writeSetHolds = []
const heldOn = new Map()  // task -> the task it is currently held behind; a repeat of the same hold is not logged twice
function noteWriteSetHold({ task, waitsOn }) {
  const t = String(task)
  const w = String(waitsOn)
  if (heldOn.get(t) === w) return
  heldOn.set(t, w)
  writeSetHolds.push({ task: t, waitsOn: w, at: nowMs() })
  log(`Task ${t} held: its write-set overlaps Task ${w}, which runs or is queued ahead of it; it starts once Task ${w} lands or stops`)
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
  // Task 94: a ready task whose write-set overlaps an in-flight task waits; in-flight is every running task, up to its landing's end.
  const holds = writeSetHold({ ready, active: [...running.keys()], overlaps: view.overlaps })
  for (const h of holds.held) noteWriteSetHold(h)
  const d = startDecision({ finishFirst: FINISH_FIRST, inFlight, slots: AGENT_SLOTS, ready: holds.start, working: working.size, maxConcurrent: MAX_CONCURRENT, stopLaunching })
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
    review = await call('review-wave', waveReviewPrompt(waveNo, ids, Object.fromEntries(await Promise.all(ids.map(async n => [n, WANT_EXTRA(n) ? await testReviewExtra(n) : ''])))), { label: `review-wave ${waveNo} tasks ${ids.join(' ')}`, phase: phaseOf(ids[0]), schema: REVIEW_SCHEMA, ...REVIEW })
  } catch (e) {
    review = null
  }
  for (const n of ids) pendingReview.get(n).reviewed = true
  review = normalizeReview(attributeFindings(review, ids))
  // Task 69 (4.8): a serves-no-requirement finding of a batched review is a proposed cut of the task it names.
  if (((review && review.findings) || []).length) reviewedFindings = true
  for (const f of (review && review.findings) || []) {
    if (f && f.noRequirement && (f.task != null || ids.length === 1)) recordCut(ids.length === 1 ? ids[0] : f.task, f)
    else if (f && f.noRequirement) log(`Review wave ${waveNo}: a serves-no-requirement finding names no task, so it is not a proposed cut: ${f.file || '?'}: ${f.issue}`)
  }
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
const conflictKey = c => canonicalNode(conflictNode(c))   // the run ledger's key, as ck's ledger keys it (4.8)
const conflictEntry = c => ({ file: String(c.file ?? ''), test: String(c.test ?? ''), criterion: String(c.criterion ?? ''), reason: String(c.reason ?? ''), fix: String(c.fix ?? ''), ...(c.ruling ? { ruling: c.ruling } : {}) })
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
  const rec = { fix, blocked: blocked ?? null, held: [...held], source, tests: nodes, changedTests: [], outcome: 'to-me', proof: null, scope: null, review: null, reason: '', finding: null }
  const toMe = reason => { rec.outcome = 'to-me'; rec.reason = reason; return rec }
  const record = async outcome => {
    const out = await thinCk('testfix record', ['-C', ROOT, 'testfix', 'record', SPEC, fix, ...nodes.flatMap(x => ['--test', x]), '--outcome', outcome, '--json'], { label: `testfix record ${k}`, phase, cwd: ROOT, what: `Recording step for test fix ${fix} of spec ${SPEC}` }).catch(() => null)
    if (!out || out.code !== 0) {
      rec.recordError = `ck testfix record did not confirm (${out ? trim(out.stderr || out.code, 200) : 'no answer'}); the cross-run ledger may lack ${fix}`
      log(`Test fix ${fix}: ${rec.recordError}`)
      runWarnings.push(`Test fix ${fix}: ${rec.recordError}`)
    }
  }
  for (const c of conflicts) testFixLedger.add(conflictKey(c))
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
  if (Array.isArray(fx.changedTests)) rec.changedTests = fx.changedTests.map(String)
  if (fx.verdict !== 'fixed') return toMe(`the test-fixer cannot fix it: ${trim(fx.summary, 400)}`)
  if (fx.finding && typeof fx.finding === 'object') return toMe(`the test-fixer's report contradicts itself (verdict fixed with a finding about the code): ${trim(fx.finding, 300)}`)
  const proveArgv = ['-C', ROOT, 'testfix', 'prove', SPEC, fix, ...(blocked != null ? ['--blocked', String(blocked)] : held.flatMap(h => ['--held', String(h)])), ...nodes.flatMap(x => ['--test', x]), '--into', INTO, '--json']
  const pv = await thinCk('testfix prove', proveArgv, { label: `testfix prove ${k}`, phase, cwd: ROOT, what: `Proof step for test fix ${fix} of spec ${SPEC}: it checks the fix's scope, then that the tests pass with it` })
  if (!pv.json) { await record('to-me'); return toMe(`ck testfix prove gave no result: ${ckError(pv)}`) }
  rec.scope = pv.json.scope ?? null
  rec.proof = pv.json.proof ?? null
  // 4.10: the record names the tests the fix changed; ck's proof is the authority when it lists them
  const proved = pv.json.changedTests ?? (rec.proof && rec.proof.changedTests) ?? (rec.scope && rec.scope.changedTests)
  if (Array.isArray(proved)) rec.changedTests = proved.map(String)
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
  let res = null
  try {
    // the landing lock keeps this landing from running beside a batch landing (ck worktree land holds a non-blocking flock)
    const out = await withLandingLock(() => thinCk('land', landArgv([fix]), { label: `land ${fix}`, phase, cwd: ROOT, gate: true, what: `Landing step for test fix ${fix} of spec ${SPEC}: it merges the fix into ${INTO} and gates the tests it changes` }))
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
async function testFixFor(n, conflicts, { held = [] } = {}) {
  const src = n == null ? 'cross-test' : 'conflict'
  const nodes = [...new Set(conflicts.map(conflictNode))]
  const led = await thinCk('testfix ledger', ['-C', ROOT, 'testfix', 'check', SPEC, ...nodes.flatMap(x => ['--test', x]), '--json'], { label: `testfix ledger ${++ledgerSeq}`, phase: n != null ? phaseOf(n) : 'Test fix', cwd: ROOT, what: `Ledger check for the test fixes of spec ${SPEC}` })
  if (!led.json || !Array.isArray(led.json.alreadyFixed) || led.json.error || led.code !== 0) {
    // 4.8: a ledger that cannot be read never grants a fix (a damaged ledger exits 3 and still lists every node)
    const reason = `the test-fix ledger could not be read: ${led.json && led.json.error ? trim(String(led.json.error), 300) : ckError(led)}`
    const rec = { fix: null, blocked: n, held: [...held], source: src, tests: nodes, outcome: 'to-me', proof: null, scope: null, review: null, reason }
    testFixRecords.push(rec)
    return { records: [rec], fixed: [], findings: [], toMe: [reason] }
  }
  const prior = led.json.alreadyFixed
  const plan = testFixPlan({ conflicts, alreadyFixed: [...prior, ...conflicts.filter(c => testFixLedger.has(conflictKey(c))).map(c => ({ node: conflictNode(c), at: 'this run', fix: 'this run' }))] })
  const out = { records: [], fixed: [], findings: [], toMe: [] }
  for (const c of plan.toMe) {
    const reason = `${conflictNode(c)} was already fixed once (testfix ledger); a second fix goes to you`
    const rec = { fix: null, blocked: n, held: [...held], source: src, tests: [conflictNode(c)], outcome: 'to-me', proof: null, scope: null, review: null, reason }
    out.records.push(rec)
    out.toMe.push(reason)
  }
  for (const g of plan.groups) {
    const k = ++testFixSeq
    log(`${n != null ? `Task ${n}` : 'Cross-test'}: test-fixer ${k} for ${g.file} (${g.conflicts.length} test(s))`)
    const rec = await runTestFix({ k, blocked: n, held, source: src, conflicts: g.conflicts })
    out.records.push(rec)
    if (rec.outcome === 'landed') out.fixed.push(...rec.tests)
    else if (rec.outcome === 'test-correct') out.findings.push({ ...rec.finding, kind: 'assertion', message: rec.reason, tasks: n != null ? [String(n)] : held.map(String), command: null, node: rec.tests[0], file: rec.finding.file || g.file })
    else out.toMe.push(`${g.file}: ${rec.reason}`)
  }
  testFixRecords.push(...out.records)
  return out
}

// A task whose test fix goes to me: needs-input kind "test-fix" with the findings, parked (unattended) or asked (interactive).
const repairText = x => { const l = x.testRepairs || (x.testRepair ? [{ ruling: x.ruling, testRepair: x.testRepair }] : []); return l.length ? ` Ruled test change${l.length > 1 ? 's' : ''}: ${l.map(p => `${p.ruling != null ? `ruling ${p.ruling}, ` : ''}${p.testRepair.file}${p.testRepair.test ? `::${p.testRepair.test}` : ''}: ${trim(p.testRepair.change, 300)} (why: ${trim(p.testRepair.why, 200)})`).join(' | ')}.` : '' }
async function parkTestFix(n, rec, reasons, records, extra = {}) {
  const action = parkAction({ interactive: INTERACTIVE })
  const tests = [...new Set(records.flatMap(x => x.tests))]
  const files = [...new Set(tests.map(t => t.split('::')[0]))]
  const decision = `Task ${n} is blocked on tests that can never pass, and the automatic test fix did not land: ${reasons.join(' | ')}.${repairText(extra)} Fix the tests yourself on ${INTO} (a test-track change: you decide), or tell the agent how to proceed; then resume the run${RUN_ID ? ` with the same runId ${RUN_ID}` : ''}.`
  const entry = {
    task: n, file: files.join(', '), files, kind: 'test-fix', test: tests.join(', '), requirement: '', decision,
    findings: reasons, fixes: records.map(x => x.fix).filter(Boolean), runId: RUN_ID || null, action, notification: null, escalatedTo: [], ...extra,
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

// Task 87 (7.2, 7.4): the findings of a cross-test check are test conflicts with no blocked task. The impl tasks
// their tests verify wait (crossHeld) until the fix lands; a fix that does not land keeps them waiting and goes to me.
async function routeCrossFindings(w, findings, replan, entry0 = null) {
  const conflicts = findings.map(f => ({
    file: String(f.file || (f.test && String(f.test).includes('::') ? String(f.test).split('::')[0] : '') || ''),
    test: String(f.test || ''),
    criterion: String(f.criterion || ''),
    reason: String(f.issue || f.reason || ''),
    fix: String(f.fix || ''),
  })).filter(c => c.file)
  // The proof and the record name every verified task (ck refuses a proof with neither --blocked nor --held). A file
  // no impl task verifies falls back to the wave's test tasks, so the list is never empty.
  const verified = [...heldByCrossTest({ findings, testVerifies: current && current.testVerifies })]
  const proofHeld = verified.length ? verified : testTasksOf(w).map(String)
  // The scheduler holds only the verified tasks not yet done or merged.
  const held = verified.filter(t => !view.done.has(t) && !mergedInRun.has(t))
  for (const t of held) crossHeld.add(t)
  // crossHeld now says exactly what waits: the wave's other dependents no longer need the blanket hold of the check window
  crossInFlight.delete(w)
  poke()
  if (held.length) log(`cross-test (wave ${w}): holding Task ${held.join(', ')} until the test fix lands`)
  if (!conflicts.length) {
    log(`cross-test (wave ${w}): no finding names a test file; nothing to fix`)
    for (const t of held) crossHeld.delete(t)
    // no automatic fix exists for these findings: list them as needing my input, so none goes unseen
    const texts = findings.map(f => trim(String(f.issue || f.summary || f.reason || JSON.stringify(f)), 300))
    crossNeedsInput.push({
      task: held[0] != null ? held[0] : `cross-test wave ${w}`, file: '', files: [], kind: 'test-fix', test: '', requirement: '',
      decision: `A cross-test check found blocking findings that name no test file, so no automatic test fix can be made: ${texts.join(' | ')}. Fix the tests yourself on ${INTO} (a test-track change: you decide), then resume the run${RUN_ID ? ` with the same runId ${RUN_ID}` : ''}.`,
      findings: texts, fixes: [], runId: RUN_ID || null, action: 'ask', notification: null, escalatedTo: [], held,
    })
    if (held.length) replan()
    return
  }
  const fixed = await testFixFor(null, conflicts, { held: proofHeld })
  if (fixed.toMe.length) {
    // the fix did not land: the held tasks stay held and the conflict goes to me
    const reasons = fixed.toMe
    const tests = [...new Set(fixed.records.flatMap(x => x.tests))]
    const files = [...new Set(tests.map(t => t.split('::')[0]))]
    const action = parkAction({ interactive: INTERACTIVE })
    const decision = `A cross-test check found tests that disagree, and the automatic test fix did not land: ${reasons.join(' | ')}. ${held.length ? `Task ${held.join(', ')} stay held. ` : ''}Fix the tests yourself on ${INTO} (a test-track change: you decide), then resume the run${RUN_ID ? ` with the same runId ${RUN_ID}` : ''}.`
    const entry = { task: held[0] != null ? held[0] : `cross-test wave ${w}`, file: files.join(', '), files, kind: 'test-fix', test: tests.join(', '), requirement: '', decision, findings: reasons, fixes: fixed.records.map(x => x.fix).filter(Boolean), runId: RUN_ID || null, action: held.length ? action : 'ask', notification: null, escalatedTo: [], held }
    if (entry.action === 'park') {
      const note = JSON.stringify({ file: entry.file, kind: 'test-fix', test: entry.test, requirement: '', decision })
      const out = await thinCk('park', ['worktree', 'park', SPEC, entry.task, '--json'], { stdin: note, label: `park ${entry.task}`, phase: 'Test fix', cwd: ROOT, what: `Parking step for the held Task ${entry.task} of spec ${SPEC}` })
      const pk = out.json ? out.json : { parked: false, error: ckError(out) }
      entry.notification = pk && pk.notification ? pk.notification : 'failed'
      entry.notificationDetail = (pk && (pk.detail || pk.error)) || ''
      if (!pk || pk.parked === false) { entry.notification = 'failed'; entry.notificationDetail = 'park failed: ' + ((pk && (pk.error || pk.detail)) || 'the park step returned nothing') }
    }
    crossNeedsInput.push(entry)
    return
  }
  for (const t of held) crossHeld.delete(t)
  if (held.length) replan()  // re-read the plan: it lists the released tasks again
  if (fixed.findings.length) {
    // the test-fixer judged the tests correct: that is not a landed fix, and its reason reaches the report
    const why = fixed.findings.map(f => trim(String(f.message || f.issue || ''), 300))
    if (entry0) entry0.testCorrect = why
    log(`cross-test (wave ${w}): the tests are correct, no fix was needed (${why.join(' | ')})${fixed.fixed.length ? '; test fix landed for the rest' : ''}; ${held.length ? `Task ${held.join(', ')} released` : 'no task was held'}`)
    return
  }
  log(`cross-test (wave ${w}): test fix landed; ${held.length ? `Task ${held.join(', ')} released` : 'no task was held'}`)
}

// --- the arbiter (run-principles Story 6) ---
const ruledKeys = new Set()   // claim keys ruled on in this run
// Calls made to the arbiter per task in this run: a risky-tier call after the first carries the `risky` label, so each call of a task has its own.
const arbiterCalls = {}
const ruledAnswers = new Map()   // claim key -> the arbiter's answer, for the entry of a claim that comes back (7.4)
const runRulings = []   // RunRuling, one per ruling this run settled (10.7); Task 66 adds the decisions.md number from `ck arbiter record`
const proposedCuts = []   // Task 69 (4.8): each serves-no-requirement finding a review named, for the result's proposedCuts
const cutKeys = new Set()
let reviewedFindings = false   // a review named at least one finding (Task 69): proposedCuts is listed then
const recordCut = (n, f) => {
  // A batched review can name the task a finding belongs to; that task owns the cut.
  const task = String(f.task || n)
  const key = `${task}|${f.file || ''}|${f.issue || ''}`
  if (cutKeys.has(key)) return
  cutKeys.add(key)
  proposedCuts.push({ task, file: String(f.file || ''), issue: String(f.issue || '') })
}

// A guard stop as a claim (6.2): the test or file the hook protected, and the requirement it verifies.
function guardClaim(n, stop, tried) {
  return {
    trigger: 'guard-stop', file: String(stop.file || ''), test: String(stop.test || ''), criterion: String(stop.requirement || ''), models_tried: tried,
    text: `A hook stopped Task ${n} from changing ${stop.test || stop.file}${stop.requirement ? ` (requirement ${stop.requirement})` : ''}${stop.kind ? `; the protected file is a ${stop.kind}` : ''}.`,
  }
}

// What the implementer is told after the arbiter ruled: the text of each ruling, and that a landed edit is on INTO.
function rulingNote(outs, forReviewer = false) {
  const lines = (outs || []).map(o => {
    const r = o.ruling || {}
    const t = r.testRepair || {}
    const what = o.route === 'code' ? `What to do: ${r.code || r.reasoning}` : o.route === 'spec_edit' ? `The arbiter changed the spec (${r.changed || 'see the commit'}) and it landed on ${INTO}.` : o.route === 'test_repair' ? `The test ${t.file || o.claim.file || ''} ${t.test || o.claim.test || ''} was repaired: ${t.change || ''}` : ''
    return `The arbiter ruled on the ${o.claim.trigger} claim ${o.key} (${r.outcome}): ${trim(r.reasoning || '', 600)} ${what}`.trim()
  })
  if (!lines.length) return ''
  // The reviewer re-reviews the task against the ruling; it does not merge or finish anything, so it gets no implementer instruction.
  const tail = forReviewer ? 'Review the task with these rulings in mind: a finding a ruling settled is not blocking again.' : `Merge ${INTO} into your branch first, so the ruled changes are in it, then finish the task. The ruling does not excuse a failing gate.`
  return `${lines.join('\n')}\n${tail}\n`
}

const taskRisk = n => (isSafety(n) || String((current && current.risk && current.risk[n]) || '').toLowerCase() === 'safety' ? 'safety' : 'normal')

const rulingPrompt = (n, claim, open, tier, first) => `You are the arbiter for Task ${n} of spec ${SPEC}. Rule on one claim, by the principles below.
Stop type: ${claim.trigger}
Claim key: ${open.key}
Task: ${n}${TITLES[n] ? ` ("${TITLES[n]}")` : ''}; tier: ${tier}; models tried: ${(claim.models_tried || []).join(', ') || 'none'}
The claim:
${JSON.stringify(claim, null, 2)}
${claim.evidence ? `Evidence: ${claim.evidence}\n` : ''}Principles (project layer and plan layer; cite the id of the one you uphold in "principle"):
${open.principles.map(p => `- ${p.id} (${p.layer}): ${p.text}`).join('\n')}
Plan layer: ${open.planLayer}.
${open.earlier.length ? `Earlier rulings on this task: ${JSON.stringify(open.earlier)}\n` : ''}The spec: specs/${SPEC}/requirements.md, specs/${SPEC}/design.md, specs/${SPEC}/test-plan.md and specs/${SPEC}/tasks.md. Read what the claim needs.
Your worktree for a spec edit: ${String(open.worktree).startsWith('/') ? open.worktree : `${ROOT}/${open.worktree}`} (branch ${open.branch}). Edit only tasks.md, design.md and test-plan.md there and commit with a plain git commit -m '...'.
${first ? `You looked at this claim once already and asked for a stronger look. Your reasoning then: ${first.reasoning || ''}\nWhat you established: ${first.established || ''}\nThis is the last look: rule now.\n` : ''}Give exactly one outcome: spec_edit (you changed the spec in your worktree, name what in "changed"), test_repair (a test is wrong: "testRepair" {file, test, change, why}), code (the implementer must change: "code"), or requirements (a requirement must change or is missing: "requirement" {kind add|change|remove, wording, reason}). A reserved scope is mine, not yours: name it in "reservedBy". For a review deadlock, "settles" lists each of the claim's findings your ruling settles, as "<file>#<issue>" copied from the claim (leave it empty for a finding the ruling does not settle). Always give "claim" (copy the Claim key above), "status" (ruled, or needs_stronger_look with "established"), "reasoning" and "principle".`

// A stop the arbiter did not rule on, or ruled on in a way that needs me: one needs-input entry (7.2, 6.10). Parked
// (unattended) or asked (interactive); no fallback runs.
async function arbiterNeedsInput(n, rec, r, claim, { reason, text, ruling = null, open = null, number = null }) {
  const action = parkAction({ interactive: INTERACTIVE })
  const kind = reason === 'requirements' || reason === 'missing-requirement' ? reason : 'arbiter'
  const decision = `The arbiter ${ruling ? 'ruled' : 'did not rule'} on Task ${n}'s ${claim.trigger} claim (${reason}): ${text}${ruling && ruling.requirement ? ` Requirement ${ruling.requirement.kind || 'change'}: ${ruling.requirement.wording || ''} (${ruling.requirement.reason || ''}).` : ''} Decide from the main session, then resume the run${RUN_ID ? ` with the same runId ${RUN_ID}` : ''}.`
  const entry = {
    task: n, file: String(claim.file || ''), kind, test: String(claim.test || ''), requirement: String(claim.criterion || ''), decision, action, notification: null, escalatedTo: [],
    ruling: number, reason, claim: ruling ? (open ? open.key : (claim.text ? String(claim.text) : null)) : { ...(open ? { key: open.key } : {}), trigger: claim.trigger, text: String(claim.text || ''), file: String(claim.file || ''), test: String(claim.test || ''), ...(claim.cycle !== undefined ? { cycle: claim.cycle } : {}), ...(claim.command !== undefined ? { command: claim.command } : {}), ...(claim.provider !== undefined ? { provider: claim.provider } : {}) }, trigger: claim.trigger, output: ruling,
    ...(reason === 'repeat-after-ruling' ? { evidence: String(claim.text || ''), earlierRuling: open && ruledAnswers.has(open.key) ? ruledAnswers.get(open.key) : null } : {}),
    ...(open && open.missing ? { missing: open.missing } : {}),
  }
  rec.result = r
  rec.finishedAt = r && r.finishedAt
  releaseSlot(n)
  if (action === 'park') {
    const note = JSON.stringify({ file: entry.file, kind, test: entry.test, requirement: entry.requirement, decision })
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
  rec.reason = `needs input: arbiter (${reason})`
  return rec
}

// `ck arbiter record` for one settled ruling (10.1), inside the landing lock and before anything acts on it. Returns
// { number } or { error }; a failed record is logged in the run report and the claim goes to needs-input.
async function recordRuling(n, { claim, answer, res, open, tier, landing = null }) {
  const outcome = res.route === 'needs_input' ? (['requirements', 'spec_edit', 'test_repair', 'code'].includes(answer && answer.outcome) ? answer.outcome : 'requirements') : res.route
  const body = {
    trigger: String(claim.trigger || ''), claim_key: open.key, claim_text: String(claim.text || ''), task: String(n), tier: tier === 'risky' ? 'risky' : 'normal', outcome,
    reasoning: String((answer && answer.reasoning) || ''), principle_id: String((res.principle && res.principle.id) || (answer && answer.principle) || ''), principle_text: String((res.principle && res.principle.text) || ''),
    plan_layer: String(open.planLayer || ''), changed: 'nothing', moved: [], commit: null, landed: null,
    ...(landing ? { changed: String(landing.changed || 'the spec'), moved: landing.moved || [], commit: landing.commit || null, landed: [landing.landed, landing.why || ''] } : {}),
  }
  let out = null
  try {
    out = await withLandingLock(() => thinCk('arbiter-record', ['-C', ROOT, 'arbiter', 'record', SPEC, '--into', INTO, '--json'], { stdin: JSON.stringify(body), label: `arbiter-record ${n}`, phase: phaseOf(n), cwd: ROOT, what: `Arbiter step for Task ${n} of spec ${SPEC}: record the ruling in decisions.md` }))
  } catch (e) {
    out = null
  }
  const j = out && out.code === 0 && out.json && typeof out.json === 'object' ? out.json : null
  if (!j || !Number.isInteger(j.number)) {
    const error = `ck arbiter record did not confirm (${out ? (out.json && out.json.error ? trim(String(out.json.error), 300) : ckError(out)) : 'no answer'}); the entry for claim ${open.key} was not written to decisions.md`
    log(`Task ${n}: ${error}`)
    runWarnings.push(`Task ${n}: ${error}`)
    return { error }
  }
  if (j.warning) {
    log(`Task ${n}: ck arbiter record: ${trim(String(j.warning), 300)}`)
    runWarnings.push(`Task ${n}: ck arbiter record: ${trim(String(j.warning), 300)}`)
  }
  return { number: j.number }
}

// One claim to the arbiter: open, rule at the returned tier, settle, re-run once at the risky tier on `rerun` (5.7),
// and land a spec edit. Returns { exit: rec } when the claim went to me, else { route, ruling, claim, key, number }.
async function arbitrate(n, rec, r, claim) {
  const toMe = async (reason, text, more = {}) => ({ exit: await arbiterNeedsInput(n, rec, r, claim, { reason, text, ...more }) })
  if (!ARBITER_AGENT) return await toMe('no-arbiter', 'no arbiter agent type was given to the run (arbiterAgent), so the arbiter cannot be called.')
  const phase = phaseOf(n)
  const meta = { risk: taskRisk(n), complexity: String(COMPLEXITY[n] || '') }
  const opened = await thinCk('arbiter-open', ['-C', ROOT, 'arbiter', 'open', SPEC, '--task', String(n), '--into', INTO, '--json'], { stdin: JSON.stringify({ claim, task: meta }), label: `arbiter-open ${n}`, phase, cwd: ROOT, what: `Arbiter step for Task ${n} of spec ${SPEC}: open the claim` })
  const open = opened.code === 0 ? openResultOf(opened.json) : null
  if (!open) return await toMe('arbiter-call-failed', `ck arbiter open failed or answered something unusable: ${opened.json && opened.json.error ? trim(String(opened.json.error), 300) : ckError(opened)}`)
  if (open.action === 'ask') {
    const why = { 'principles-missing': `the principles file ${open.missing || 'specs/PRINCIPLES.md'} is missing, a directory or unreadable; run "ck principles init" or fix it`, 'repeat-after-ruling': 'the same claim was ruled on already and came back', exhausted: 'the arbiter asked for a stronger look at the risky tier already' }[open.reason] || `the arbiter may not be called again (${open.reason})`
    return await toMe(open.reason || 'ask', `${why}.${open.earlier.length ? ` Earlier rulings: ${open.earlier.map(e => `#${e.ruling} ${e.outcome}${e.reasoning ? ` (${trim(String(e.reasoning), 300)})` : ''}`).join('; ')}.` : ''}${ruledAnswers.has(open.key) ? ` The ruling on this claim: ${trim(JSON.stringify(ruledAnswers.get(open.key)), 600)}.` : ''}${claim.text ? ` The claim came back: ${trim(String(claim.text), 300)}.` : ''}`, { open })
  }
  // A claim this run already ruled on that comes back is never ruled on again (7.4): the arbiter worktree that open
  // started is discarded and the claim goes to me. (The ledger knows it only once the ruling is recorded.)
  if (ruledKeys.has(open.key)) {
    await thinCk('arbiter-settle', ['-C', ROOT, 'arbiter', 'settle', SPEC, open.key, '--tier', open.tier, '--json'], { stdin: 'null', label: `arbiter-settle ${n}`, phase, cwd: ROOT, what: `Arbiter step for Task ${n} of spec ${SPEC}: discard the repeated claim` })
    return await toMe('repeat-after-ruling', 'the same claim was ruled on already in this run and came back', { open })
  }
  // Task 69 (8.12): a deadlock finding an applied ruling already covers is not sent again. The arbiter rules on the rest; the
  // covered ones go to me with their earlier ruling once the rest is settled.
  const covered = claim.trigger === 'review-deadlock' ? coveredFindingsOf(claim.findings, open.earlier) : []
  let promptOpen = open
  if (covered.length) {
    const coveredKeys = new Set(covered.map(c => `${c.file}\u0000${c.issue}`))
    const left = claim.findings.filter(([f, i]) => !coveredKeys.has(`${f}\u0000${i}`))
    claim = { ...claim, findings: left, text: `Task ${n} still has blocking review findings: ${left.map(([f, i]) => `${f}: ${i}`).join(' | ')}` }
    // The covered findings' earlier rulings stay out of the arbiter's prompt too: only the rest of the deadlock is shown.
    const idText = c => `${c.file.replace(/^(\.\/)+/, '')}#${c.issue.split(/\s+/).filter(Boolean).join(' ').toLowerCase()}`
    promptOpen = { ...open, earlier: open.earlier.filter(e => !covered.some(c => String(e.claim || '').includes(idText(c)))) }
  }
  let tier = open.tier
  let first = null
  for (let pass = 0; pass < 2; pass++) {
    const rerun = pass > 0
    let answer = null
    let threw = null
    // The ruling runs without the task's slot: independent tasks continue and its dependents wait (6.9). The slot is
    // released once the arbiter's call is in flight, so the arbiter is the step right after the stop.
    const pending = call('arbiter', rulingPrompt(n, claim, promptOpen, tier, first), { label: `arbiter task ${n}${rerun || (tier === 'risky' && arbiterCalls[n] > 0) ? ' risky' : ''}`, phase, schema: RULING, agentType: ARBITER_AGENT, ...arbiterOpts(tier) })
    arbiterCalls[n] = (arbiterCalls[n] || 0) + 1
    releaseSlot(n)
    try { answer = await pending } catch (e) { threw = e }
    // A thrown or empty call is settled as `null` (invalid) so ck marks the claim failed and removes the worktree; it is never a ruling.
    const empty = threw !== null || answer === null || answer === undefined
    const settled = await thinCk('arbiter-settle', ['-C', ROOT, 'arbiter', 'settle', SPEC, open.key, '--tier', tier, '--json'], { stdin: empty ? 'null' : JSON.stringify(answer), label: `arbiter-settle ${n}${rerun ? ' risky' : ''}`, phase, cwd: ROOT, what: `Arbiter step for Task ${n} of spec ${SPEC}: settle the ruling` })
    const res = settled.code === 0 ? settleResultOf(settled.json) : null
    if (empty) return await toMe('arbiter-call-failed', threw ? `the arbiter agent failed: ${trim(threw && threw.message, 300)}` : 'the arbiter agent returned nothing', { open })
    if (!res) return await toMe('arbiter-call-failed', `ck arbiter settle failed or answered something unusable: ${settled.json && settled.json.error ? trim(String(settled.json.error), 300) : ckError(settled)}`, { open, ruling: answer })
    if (res.route === 'rerun') {
      if (rerun) return await toMe('exhausted', 'the arbiter asked for a stronger look again', { open, ruling: answer })
      first = answer && typeof answer === 'object' ? answer : {}
      tier = 'risky'
      continue
    }
    if (res.route === 'needs_input') {
      const why = {
        'invalid-ruling': `the answer is not a valid ruling (${res.problems.join('; ') || 'see the output'}); nothing was applied or recorded`,
        exhausted: `it still needs a stronger look at the risky tier${answer && answer.established ? `; established so far: ${answer.established}` : ''}`,
        reserved: 'it touches a scope a principle reserves for me',
        requirements: 'a requirement must change',
        'missing-requirement': 'a requirement is missing',
      }[res.reason] || res.reason
      let number = null
      if (res.record && res.reason !== 'exhausted') {   // a needs-a-stronger-look answer at the risky tier is no ruling
        const rec1 = await recordRuling(n, { claim, answer, res, open, tier })
        if (rec1.error) return await toMe('record-failed', `${rec1.error}. ${why}.`, { open, ruling: answer })
        number = rec1.number
        ruledKeys.add(open.key)
        ruledAnswers.set(open.key, answer)
        runRulings.push({ number, task: String(n), trigger: String(claim.trigger || ''), outcome: String((answer && answer.outcome) || 'requirements'), tier: tier === 'risky' ? 'risky' : 'normal', principle: String((answer && answer.principle) || ''), commit: null, landed: null })
      }
      return await toMe(res.reason, `${why}. Arbiter's reasoning: ${answer && typeof answer === 'object' ? trim(answer.reasoning || answer.changed || '', 500) : trim(answer, 300)}`, { open, ruling: answer, number })
    }
    const out = { route: res.route, ruling: answer, claim, key: open.key, number: open.number, tier, rulingNo: null }
    let recorded = null
    // Every ruling but a spec edit is recorded as it settles (10.1), so the task's ruling limit (7.6) counts it.
    if (res.route !== 'spec_edit' && res.record) {
      recorded = await recordRuling(n, { claim, answer, res, open, tier })
      if (recorded.error) return await toMe('record-failed', `${recorded.error}; the ${res.route} ruling was not acted on`, { open, ruling: answer })
      out.ruling = answer && typeof answer === 'object' ? { ...answer, number: recorded.number } : answer
      out.rulingNo = recorded.number
    }
    ruledKeys.add(open.key)
    ruledAnswers.set(open.key, answer)
    const runRuling = { number: recorded ? recorded.number : null, task: String(n), trigger: String(claim.trigger || ''), outcome: String(res.route), tier: tier === 'risky' ? 'risky' : 'normal', principle: String((answer && answer.principle) || ''), commit: null, landed: res.route === 'spec_edit' ? false : null }
    runRulings.push(runRuling)
    out.runRuling = runRuling
    if (res.route === 'spec_edit') {
      // The arbiter's commit lands on INTO alone (Task 67 moves this into the merge queue); a landing that fails is an entry for me.
      const landed = await withLandingLock(() => thinCk('arbiter-land', ['-C', ROOT, 'worktree', 'land', SPEC, open.number, '--into', INTO, '--gate', '--json'], { label: `arbiter-land ${n}`, phase, cwd: ROOT, gate: true, what: `Landing step for the arbiter's spec edit of spec ${SPEC}: it merges the edit into ${INTO}` }))
      // `ck worktree land SPEC arbiter<k> --json` prints the one landing entry (state, commit, moved, recovered); a refused call prints {error, tasks}.
      const item = landEntryOf(landed.json)
      const changed = answer && typeof answer === 'object' ? String(answer.changed || '') : ''
      if (item && item.recovered && typeof item.recovered === 'object') {
        const r = item.recovered
        runRuling.recovered = r
        log(`Landing arbiter spec edit ${open.number}: restored ${INTO} first (${Array.isArray(r.paths) ? r.paths.length : 0} path(s)${r.mergeInProgress ? ', merge in progress' : ''}, head ${trim(String(r.head || ''), 12)})`)
      }
      if (!item || item.state !== 'MERGED') {
        const why = `${item ? `${item.state}${item.detail ? `: ${trim(String(item.detail), 200)}` : ''}` : `no valid landing entry (${ckError(landed)})`}`
        // 8.4: recorded as not landed, with why, and the ruling and result go to me; no new ruling.
        const rn = await recordRuling(n, { claim, answer, res, open, tier, landing: { changed, landed: false, why } })
        if (rn.number !== undefined) { runRuling.number = rn.number; out.rulingNo = rn.number }
        return await toMe('landing-failed', `the arbiter's spec edit did not land (${why})${rn.error ? `; ${rn.error}` : ''}`, { open, ruling: answer, number: rn.number ?? null })
      }
      runRuling.landed = true
      runRuling.commit = isText(item.commit) && item.commit ? item.commit : null
      requestPlan('landing')   // the landing changed the wiring, whether or not the record below succeeds (8.3)
      const rl = await recordRuling(n, { claim, answer, res, open, tier, landing: { changed, landed: true, commit: runRuling.commit, moved: Array.isArray(item.moved) ? item.moved : [] } })
      if (rl.error) return await toMe('record-failed', `the arbiter's spec edit landed on ${INTO} (commit ${runRuling.commit || 'unknown'}), but ${rl.error}; the task was not resumed`, { open, ruling: answer })
      runRuling.number = rl.number
      out.rulingNo = rl.number
      out.ruling = answer && typeof answer === 'object' ? { ...answer, number: rl.number } : answer
    }
    if (covered.length) {
      const text = covered.map(c => `"${c.issue}" is settled by ruling #${c.ruling} (${c.outcome}): ${trim(String(c.reasoning || ''), 400)}`).join('; ')
      return { exit: await arbiterNeedsInput(n, rec, r, { trigger: 'review-deadlock', text: `Covered findings: ${text}`, findings: covered.map(c => [c.file, c.issue]) }, { reason: 'repeat-after-ruling', text: `${text}. The other findings of the deadlock were ruled on in this call.`, open }) }
    }
    return out
  }
  return await toMe('arbiter-call-failed', 'the arbiter did not settle')
}

// Task 66 (8.9, 8.10, D18): the single point a test-repair ruling goes through, called once, from `settleClaims`. The
// rulings are gathered `{claim, ruling}` outcomes; each becomes a conflict entry carrying the ruling unchanged, and the
// ruling is the confirmation the test-fixer starts on, so no second agent confirms. Returns { entries, fixed }.
async function handOffTestRepair(n, rec, rulings) {
  releaseSlot(n)
  const entries = testRepairEntries(rulings)
  if (!entries.length) {
    const reason = 'the ruled test repair names no test file'
    return { entries, fixed: { records: [], fixed: [], findings: [], toMe: [reason] } }
  }
  rec.testConflict = { ...entries[0] }
  rec.reason = `blocked: test (${entries[0].file}${entries[0].test ? ` ${entries[0].test}` : ''}): ${entries[0].reason || 'a ruled test repair'}`
  return { entries, fixed: await testFixFor(n, entries) }
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
    let stop = guardStopOf(r)   // set after every call; a guard stop leaves the retry and escalate loops at once and goes to the arbiter (6.2)
    let arbiterK = 0
    const retryNo = () => rec.attempts - arbiterK   // attempts that spent the retry budget: an attempt a ruling resumed does not count (8.2)
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
    // Task 93 (12.5-12.7): a missing command another unfinished task provides is a live edge (wait), one no task
    // provides is a test conflict, and an edge that would close a cycle is a command-cycle claim (D29). None is retried.
    const liveOf = res => (res && !ok(res) && !guardStopOf(res) ? liveEdgeDecision({ task: n, failures: res.failures, deps: view.deps, done: [...view.done, ...mergedInRun], liveDeps }) : { action: 'none', edges: [], cycle: null, command: null, tests: [], refused: [] })
    const liveConflicts = res => { const lv = liveOf(res); return lv.action === 'conflict' ? { ...res, testConflicts: [...testConflictsOf(res), ...lv.tests] } : res }
    const rungs = ladderOf(n)
    // Today's retry and escalation loops, for plain gate failures. Both stop on a guard stop, a test conflict or a live
    // action: those are claims for the arbiter, which spend no retry.
    const ladder = async () => {
      while (!ok(r) && !stop && retryNo() - budgetBase <= MAX_RETRIES && !busy(r) && !testConflictOf(r) && liveOf(r).action === 'none') {
        log(`Task ${n}: ${r && !(r.commits || '').trim() ? 'no commits' : 'gate not passed'}, retry ${retryNo()}/${MAX_RETRIES}`)
        r = await call('task', retryPrompt(n, r, retryNo(), claimFlags('retry', codeOpts(n), retryNo() + 1)), { label: `${label(n)} retry ${retryNo()}`, phase: phaseOf(n), schema: RESULT, ...codeOpts(n) })
        rec.attempts++
        lastModel = codeOpts(n).model
        stop = guardStopOf(r)
        rec.startedAt = rec.startedAt || (r && r.startedAt)
      }
      while (escIdx < rungs.length && !ok(r) && !stop && !busy(r) && !testConflictOf(r) && liveOf(r).action === 'none') {
        const model = rungs[escIdx++]
        log(`Task ${n}: still failing after ${retryNo()} attempt(s), escalating to ${model}`)
        r = await call('task', retryPrompt(n, r, retryNo(), claimFlags('escalation', escalated(codeOpts(n), model), retryNo() + 1)), { label: `${label(n)} retry ${retryNo()} (${model})`, phase: phaseOf(n), schema: RESULT, ...escalated(codeOpts(n), model) })
        rec.attempts++
        rec.escalations.push({ stage: 'retry', model })
        lastModel = model
        stop = guardStopOf(r)
        rec.startedAt = rec.startedAt || (r && r.startedAt)
      }
    }
    // The models the task has been tried on, for the arbiter's prompt (design.md Claim.models_tried).
    const modelsTried = () => [...new Set([codeOpts(n).model, ...rec.escalations.map(e => e.model), lastModel].filter(Boolean))]
    // The claims of an attempt that stopped (6.1-6.6, 6.11, 6.13): one per test conflict, else one for the stop.
    const claimsOf = (trigger, live) => {
      const tried = modelsTried()
      if (trigger === 'guard-stop') return [guardClaim(n, stop, tried)]
      if (trigger === 'test-conflict') {
        return testConflictsOf(r).map(c => ({
          trigger, text: String(c.reason || ''), file: String(c.file || ''), test: String(c.test || ''), criterion: String(c.criterion || ''),
          models_tried: tried, evidence: c.fix ? `the agent proposed this fix: ${c.fix}` : '',
        }))
      }
      if (trigger === 'command-cycle') {
        const provider = String((live.refused && live.refused[0]) || '')
        return [{
          trigger, text: `Task ${n} needs the command "${live.command}", which Task ${provider} provides, but waiting for it would close a cycle (${(live.cycle || []).map(x => `Task ${x}`).join(' -> ')}).`,
          command: String(live.command || ''), provider, cycle: (live.cycle || []).map(String), models_tried: tried,
        }]
      }
      const failures = Array.isArray(r && r.failures) ? r.failures : []
      const nodes = failures.map(f => String((f && (f.node || f.file)) || '')).filter(Boolean)
      const shown = failures.slice(0, 20).map(f => `${(f && (f.node || f.file)) || '?'}: ${trim((f && f.message) || '', 300)}`)
      return [{
        trigger: 'gate-failure', failing: nodes.length ? nodes : ['gate not passed'], models_tried: tried,
        text: `Task ${n} still fails its gate after ${rec.attempts} attempt(s) on ${tried.join(', ') || 'its model'}. ${shown.length ? `Failures: ${shown.join(' | ')}` : trim(r ? r.blocker || r.summary || 'no failures were reported' : 'the agent died without a report', 400)}`,
      }]
    }
    // The task goes back to the implementer with the rulings, spending no retry or escalation (8.2): `arbiterK` is
    // subtracted by retryNo(). Its prompt merges INTO first, so a landed spec edit or test fix is in the branch.
    const resumeWith = async outs => {
      await acquireSlot(n)
      arbiterK++
      // The resume stays on the model that made the attempt the ruling interrupted: it spends no escalation (8.2).
      const o = lastModel && lastModel !== codeOpts(n).model ? escalated(codeOpts(n), lastModel) : codeOpts(n)
      r = await call('task', `${retryPrompt(n, r, retryNo(), claimFlags('retry', o, rec.attempts + 1))}\n${rulingNote(outs)}`, { label: `${label(n)} resume ${arbiterK}`, phase: phaseOf(n), schema: RESULT, ...o })
      rec.attempts++
      stop = guardStopOf(r)
      rec.startedAt = rec.startedAt || (r && r.startedAt)
    }
    // Task 66 (D31-D33): the one claim loop. Each claim goes to the arbiter in order; a `spec_edit` lands as it settles, a
    // `code` ruling's text is kept and `test_repair` rulings are gathered. Whether or not a claim exited, the gathered
    // repairs go to `handOffTestRepair` (8.9), so a recorded ruling always reaches the fixer or me. Only when every
    // claim settled, none exited and the hand-off sent nothing to me does the task go on, once, with every ruling's text
    // and no retry or escalation spent (8.2, 8.16). Returns { exit: rec } or { exit: null }.
    let lastRepair = null   // the ruled repair of the last hand-off: its number and exact change, for a later park
    const settleClaims = async (n, rec, claims, next) => {
      const settled = []
      let exit = null
      for (const claim of claims) {
        const out = await arbitrate(n, rec, r, claim)
        if (out.exit) { exit = out.exit; break }
        settled.push(out)
      }
      const others = settled.filter(o => o.route !== 'test_repair')
      const repairs = settled.filter(x => x.route === 'test_repair')
      let fixed = null
      if (repairs.length) {
        rec.result = r
        rec.finishedAt = r && r.finishedAt
        const h = await handOffTestRepair(n, rec, repairs)
        fixed = h.fixed
        // Every ruling whose repair did not land, matched to its fixer record by test node: the entry carries each one's number and exact change (8.8).
        const landedNodes = new Set(fixed.records.filter(x => x.outcome === 'landed').flatMap(x => x.tests))
        const pending = repairs.map(x => {
          const e = testRepairEntries([x])[0]
          const t = (x.ruling && x.ruling.testRepair) || {}
          return { x, e, testRepair: { file: String(t.file || x.claim.file || ''), test: String(t.test || x.claim.test || ''), change: String(t.change || ''), why: String(t.why || '') }, ruling: x.rulingNo ?? null }
        }).filter(p => !p.e || !landedNodes.has(conflictNode(p.e)))
        const shown = pending.length ? pending : repairs.map(x => { const t = (x.ruling && x.ruling.testRepair) || {}; return { x, testRepair: { file: String(t.file || x.claim.file || ''), test: String(t.test || x.claim.test || ''), change: String(t.change || ''), why: String(t.why || '') }, ruling: x.rulingNo ?? null } })
        const lastOne = shown[0]
        lastRepair = { records: fixed.records, ruling: lastOne.ruling, testRepair: lastOne.testRepair, all: shown.map(p => ({ ruling: p.ruling, testRepair: p.testRepair })) }
        const finding = (fixed.records.find(x => x.outcome === 'test-correct' && x.finding) || {}).finding
        const reasons = [...fixed.toMe, ...fixed.findings.map(f => `the test-fixer found the test correct, so it is not changed: ${trim(String(f.message || f.issue || ''), 300)}`)]
        if (reasons.length) {
          if (exit) {
            runWarnings.push(`Task ${n}: the test repair did not land (${reasons.join(' | ')}) and a claim went to needs-input first`)
            // The task has one needs-input entry: the repair that did not land rides along in it, so its exact change reaches me (8.8, 10.9).
            if (exit.needsInput) exit.needsInput.alsoPending = [...(exit.needsInput.alsoPending || []), { ruling: lastRepair.ruling ?? null, testRepair: lastRepair.testRepair, ...(lastRepair.all.length > 1 ? { testRepairs: lastRepair.all } : {}), reasons }]
            return { exit }
          }
          return { exit: await parkTestFix(n, rec, reasons, fixed.records, { ruling: lastRepair.ruling ?? null, testRepair: lastRepair.testRepair, ...(lastRepair.all.length > 1 ? { testRepairs: lastRepair.all } : {}), ...(finding ? { finding } : {}) }) }
        }
      }
      if (exit) return { exit }
      // Task 69: a revision (`revise`) or a review (`re-review`) carries the rulings' text in its next prompt and spends no
      // attempt or escalation (8.2, 8.16): the caller re-runs it with `note`. Only `retry` resumes the task itself.
      if (next !== 'retry') {
        if (fixed) { rec.testConflictFixed = rec.testConflict; rec.testConflict = null }
        return { exit: null, settled, note: `\n${fixed ? afterFixNote(fixed.fixed, []) : ''}${rulingNote([...others, ...repairs])}` }
      }
      if (fixed) {
        rec.testConflictFixed = rec.testConflict   // the conflict is resolved: a later ordinary failure is handled like any other
        rec.testConflict = null
        log(`Task ${n}: test fix landed for ${fixed.fixed.join(', ')}; retrying the task`)
        budgetBase = retryNo()
        justFixed = true
        await acquireSlot(n)
        r = await call('task', `${retryPrompt(n, { ...r, failures: [] }, retryNo(), claimFlags('retry', codeOpts(n), retryNo() + 1))}\n${afterFixNote(fixed.fixed, [])}${rulingNote(others)}`, { label: `${label(n)} retry ${retryNo()}`, phase: phaseOf(n), schema: RESULT, ...codeOpts(n) })
        rec.attempts++
        lastModel = codeOpts(n).model
        stop = guardStopOf(r)
        rec.startedAt = rec.startedAt || (r && r.startedAt)
        return { exit: null }
      }
      await resumeWith(others)
      return { exit: null }
    }
    let justFixed = false   // the attempt that follows a landed test fix: a conflict it reports again goes straight to me (7.4)
    // Where each stop of an attempt goes. A spec edit lands as it settles; a code ruling's text is kept; test repairs
    // are gathered and go to the test-fixer. The task goes on only once every claim has settled (D32).
    for (;;) {
      await ladder()
      if (justFixed) {
        justFixed = false
        const again = testConflictOf(r)
        if (again) {
          rec.result = r
          rec.finishedAt = r && r.finishedAt
          releaseSlot(n)
          return await parkTestFix(n, rec, [`the task still reports a test that can never pass after its test fix: ${trim(again.file, 120)}: ${trim(again.reason, 300)}`], lastRepair ? lastRepair.records : [], { ruling: lastRepair ? lastRepair.ruling ?? null : null, testRepair: lastRepair ? lastRepair.testRepair : null, ...(lastRepair && lastRepair.all.length > 1 ? { testRepairs: lastRepair.all } : {}), evidence: { file: String(again.file || ''), test: String(again.test || ''), reason: String(again.reason || '') } })
        }
      }
      const live = liveOf(r)
      // 12.5, 12.8: the edges are recorded and logged; the task waits for them, spends no retry and never reaches the arbiter (6.4).
      if (live.action === 'wait' && !live.refused.length) {
        releaseSlot(n)
        return recordLiveWait(n, rec, r, live)
      }
      r = liveConflicts(r)
      const trigger = claimTrigger({ result: r, guardStop: stop, testConflict: testConflictsOf(r)[0] || null, live, worktreeKind: worktreeOutcome(r).kind, retryNo: retryNo() - budgetBase, maxRetries: MAX_RETRIES, rungsLeft: rungs.length - escIdx })
      if (!trigger) break
      if (trigger === 'command-cycle') log(`Task ${n}: command cycle ${(live.cycle || []).map(x => `Task ${x}`).join(' -> ')}; the live edge is not added, the claim goes to the arbiter`)
      else if (trigger === 'test-conflict') { const first = testConflictsOf(r)[0]; log(`Task ${n}: the agent reports a test that can never pass (${trim(first.file, 120)}: ${trim(first.reason, 200)}); the claim goes to the arbiter`) }
      if (trigger === 'test-conflict') {
        // The conflicts the attempt reported stay in the result's testConflicts after a landed fix makes a later attempt pass (3.7).
        const seen = new Set((rec.reportedConflicts || []).map(conflictNode))
        rec.reportedConflicts = [...(rec.reportedConflicts || []), ...testConflictsOf(r).filter(c => !seen.has(conflictNode(c)))]
      }
      const go = await settleClaims(n, rec, claimsOf(trigger, live), 'retry')
      if (go.exit) return go.exit
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
      // Task 69 (4.8, 8.12): a review is normalized before it is read, and the findings a ruling settled are dropped. A
      // serves-no-requirement finding is recorded as a proposed cut.
      const settledIds = []
      const reviewed = raw => {
        if (!raw) return raw
        const v = normalizeReview(raw)
        if ((v.findings || []).length) reviewedFindings = true
        for (const f of v.findings || []) if (f && f.noRequirement) recordCut(n, f)
        return normalizeReview(settleFindings(v, settledIds))
      }
      const reviewAgain = async (note = '') => reviewed(await call('review', `${reviewPrompt(n, r, WANT_EXTRA(n) ? await testReviewExtra(n) : '')}${note}`, { label: `review ${n} round ${round + 1}`, phase: phaseOf(n), schema: REVIEW_SCHEMA, ...opts }))
      let review = reviewed(first)
      let round = 0
      // A revision that guard-stops is a claim (6.2, D31): the arbiter rules, and a code ruling, a landed spec edit or a landed
      // test fix runs the same revision again with the ruling's text, spending no attempt. Settled by `settleClaims` (`revise`).
      const revised = async (prompt, lbl, o, model) => {
        let note = ''
        for (let again = 0; ; again++) {
          working.add(n)  // a revision takes a slot but never waits for one: the task is nearly done
          // A rerun after a ruling carries its own label (the same revision, spending no attempt).
          r = await call('revise', `${prompt}${note}`, { label: again ? `${lbl} (after ruling ${again})` : lbl, phase: phaseOf(n), schema: RESULT, ...o })
          lastModel = model
          stop = guardStopOf(r)
          if (!stop) { if (!ok(r)) await releaseClaim(n); releaseSlot(n); return null }
          releaseSlot(n)
          const tried = [...new Set([codeOpts(n).model, ...rec.escalations.map(e => e.model), model].filter(Boolean))]
          const got = await settleClaims(n, rec, [guardClaim(n, stop, tried)], 'revise')
          if (got.exit) return got.exit
          note = got.note
        }
      }
      while (review && review.verdict === 'changes' && round < REVIEW_ROUNDS && ok(r)) {
        round++
        log(`Task ${n}: reviewer asked for changes, revision ${round}/${REVIEW_ROUNDS}`)
        const parked1 = await revised(revisePrompt(n, review, round, claimFlags('revision', codeOpts(n), round + 1)), `${label(n)} revise ${round}`, codeOpts(n), codeOpts(n).model)
        if (parked1) return { exit: parked1 }
        if (ok(r)) review = await reviewAgain()
      }
      // Task 69 (8.9, 8.16, D33): blocking findings that survive the rounds are a review deadlock. The arbiter rules on them
      // (one claim, split per finding by ck), and the ruling goes to the fixer if it is a test repair; then the task is
      // reviewed again with the ruling in the prompt. A finding a ruling settled does not block again.
      while (review && review.verdict === 'changes' && ok(r)) {
        const blocking = review.findings.filter(f => f && f.severity === 'blocking')
        if (!blocking.length) break
        const tried = [...new Set([codeOpts(n).model, ...rec.escalations.map(e => e.model)].filter(Boolean))]
        const claim = {
          trigger: 'review-deadlock', findings: blocking.map(f => [String(f.file || ''), String(f.issue || '')]), models_tried: tried,
          text: `Task ${n} still has blocking review findings after ${round} revision(s): ${blocking.map(f => `${f.file || '?'}: ${f.issue}`).join(' | ')}`,
        }
        const go = await settleClaims(n, rec, [claim], 're-review')
        if (go.exit) return { exit: go.exit }
        // 8.12: only the findings the ruling names in `settles` are settled. A spec edit is the arbiter's finding that the
        // reviewer was wrong, so it settles every blocking finding of the claim.
        const outs = go.settled || []
        const named = outs.filter(o => o.route !== 'spec_edit').flatMap(o => (o.ruling && Array.isArray(o.ruling.settles) ? o.ruling.settles : []).map(String))
        const reviewerWrong = outs.some(o => o.route === 'spec_edit')
        // A code ruling is the arbiter ruling on the claim's findings (the implementer is told to fix them), so the findings
        // it ruled on are settled once the revision has run; a test repair or requirement settles only what it names.
        const codeRuled = outs.some(o => o.route === 'code')
        const settledNow = reviewerWrong || codeRuled ? blocking : blocking.filter(f => settleFindings({ findings: [f] }, named).findings.length === 0)
        // 8.11: a code ruling always returns the task to the implementer with the ruling's text, whether or not `settles` covers
        // every finding, and the task still needs a passing gate. Only then is the task re-reviewed with the ruling in the prompt.
        if (codeRuled) {
          const parked = await revised(`${revisePrompt(n, review, round, claimFlags('revision', codeOpts(n), round + 1))}${go.note}`, `${label(n)} revise after ruling`, codeOpts(n), codeOpts(n).model)
          if (parked) return { exit: parked }
        }
        for (const f of settledNow) settledIds.push(`${f.file || ''}#${f.issue || ''}`)
        // 8.12: the re-reviewer gets the ruling too
        if (ok(r)) review = await reviewAgain(codeRuled ? rulingNote(outs, true) : go.note)
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
      const first = await call('review', reviewPrompt(n, r, WANT_EXTRA(n) ? await testReviewExtra(n) : ''), { label: `review ${n}`, phase: phaseOf(n), schema: REVIEW_SCHEMA, ...reviewOpts(n) })
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
const resumedWaves = new Set()
for (;;) {
  while (running.size || planRun || crossOpen > 0 || (deferring && inFlight.length)) {
    await signal()
    poked = false
    for (let i = 0; i < 10; i++) await null
    schedule()
  }
  await landingIdle
  for (const n of Object.keys(records)) maybeCrossTest(n, true)  // waves whose other test tasks never finished
  await Promise.all(crossTestRuns)
  // A cross-test fix that landed after the loop went idle releases held tasks: start them (7.4).
  const resumed = crossTests.filter(c => c.routed && !resumedWaves.has(c.wave))
  if (!resumed.length) break
  for (const c of resumed) resumedWaves.add(c.wave)
  if (planRun) await planRun
  schedule()
  poked = false
  if (!(running.size || planRun)) break
}

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
  testConflict: r.testConflict || r.testConflictFixed || null,
  testConflicts: (() => { const now = testConflictsOf(r.result); const seen = new Set(now.map(conflictNode)); return [...(r.reportedConflicts || []).filter(c => !seen.has(conflictNode(c))), ...now] })(),
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
for (const e of crossNeedsInput) needsInput.push({ ...e, task: taskId(e.task) })
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
  // Task 69 (4.8): the serves-no-requirement findings a review named. Listed when a review named any finding, so a run whose reviews are clean keeps the pinned key set.
  ...(proposedCuts.length || reviewedFindings ? { proposedCuts } : {}),
  // Task 65 (10.7): the rulings this run settled, listed when there are any, so a run without any keeps the pinned key set.
  ...(runRulings.length ? { rulings: runRulings } : {}),
  // Task 93 (12.8): listed when the run added live edges; a run without any keeps the result's pinned key set.
  ...(liveEdges.length ? { liveEdges } : {}),
  // Task 94 (13.3): listed when the run held a task for an overlap; a run without any keeps the pinned key set.
  ...(writeSetHolds.length ? { writeSetHolds } : {}),
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
