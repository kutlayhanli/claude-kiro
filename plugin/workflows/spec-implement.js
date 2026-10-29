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
function roleOpts(name, role, override) {
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
const escalated = (opts, model) => ({ ...opts, model })

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
  const risky = roleOpts('reviewer.risky', { model: REVIEWER.risky_model || REVIEWER.model, effort: REVIEWER.risky_effort || REVIEWER.effort }, null)
  const opts = { ...REVIEW }
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

const PLAN = {
  type: 'object',
  properties: {
    done: { type: 'array', items: { type: 'string' }, description: '"done" from ck plan' },
    remaining: { type: 'array', items: { type: 'string' }, description: '"remaining" from ck plan' },
    deps: { type: 'object', additionalProperties: { type: 'array', items: { type: 'string' } }, description: '"deps" from ck plan: task number -> its dependencies' },
    ready: { type: 'array', items: { type: 'string' }, description: '"ready" from ck plan: task numbers (the "task" field, e.g. "12", never a path)' },
    busy: {
      type: 'array',
      description: 'leave it empty: tasks are claimed by their own agents now',
      items: { type: 'object', properties: { task: { type: 'string' }, detail: { type: 'string' } }, required: ['task', 'detail'] },
    },
    cycles: { type: 'array', items: { type: 'array', items: { type: 'string' } }, description: '"cycles" from ck plan' },
    verifyCycles: {
      type: 'array',
      items: { type: 'object', properties: { task: { type: 'string' }, message: { type: 'string' } }, required: ['task', 'message'] },
      description: '"verifyCycles" from ck plan: task and message only',
    },
    error: { type: 'string', description: 'only if a command failed to run; leave it out otherwise (no notes or remarks here)' },
  },
  required: ['done', 'remaining', 'deps', 'ready', 'cycles', 'verifyCycles'],
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
    deviations: { type: 'string' },
    wrongTests: { type: 'string' },
    outsideFiles: { type: 'string' },
    startedAt: { type: 'string', description: `output of "${NOW}" when you started` },
    finishedAt: { type: 'string', description: `output of "${NOW}" when you finished` },
  },
  required: ['task', 'status', 'gatePassed', 'summary', 'commits'],
}
const LAND = {
  type: 'object',
  properties: {
    tasks: {
      type: 'array',
      description: '"tasks" from the ck worktree land JSON, every entry, copied as is',
      items: {
        type: 'object',
        properties: {
          task: { type: 'string' },
          state: { type: 'string', enum: ['MERGED', 'RED', 'CONFLICT', 'DEFERRED', 'PROTECTED', 'SKIP', 'MISSING', 'BUSY', 'ERROR'] },
          detail: { type: 'string' },
          files: { type: 'array', items: { type: 'string' } },
          checkGreen: { type: ['boolean', 'null'] },
          checkTail: { type: 'string' },
          mergedAt: { type: 'string' },
        },
        required: ['task', 'state'],
      },
    },
    gates: {
      type: 'array',
      items: { type: 'object', properties: { tasks: { type: 'array', items: { type: 'string' } }, green: { type: 'boolean' }, seconds: { type: 'number' } }, required: ['tasks', 'green'] },
    },
    bisected: { type: 'boolean' },
    startedAt: { type: 'string' },
    finishedAt: { type: 'string' },
    error: { type: 'string', description: 'only if the command printed no JSON (or was refused): what happened' },
  },
  required: ['tasks'],
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
          severity: { type: 'string', enum: ['blocking', 'minor'] },
          file: { type: 'string' },
          issue: { type: 'string', description: 'what is wrong, and the spec text it contradicts' },
          fix: { type: 'string', description: 'what the implementer should change' },
        },
        required: ['severity', 'issue'],
      },
    },
    summary: { type: 'string' },
  },
  required: ['verdict', 'findings', 'summary'],
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

const planPrompt = exclude => `Mechanical planning step for spec ${SPEC}. Do exactly this, nothing else. ${GIT_NOTE}
Run:  ck -C ${TARGET} plan ${SPEC} --json${exclude.length ? ` --exclude ${exclude.join(',')}` : ''}
Copy into your answer: "done", "remaining", "deps", "ready", "cycles", and for "verifyCycles" only each entry's task and message. "ready", "done" and "remaining" hold task numbers (e.g. "12"), never worktree paths.
Return the fields. Put a message in error only if the command itself failed to run; no notes or remarks anywhere.`

// A note the agent leaves for whoever picks the task up next (a retry, a revision,
// or a bigger model after escalation). It sits next to the worktree, not in it, so
// it never dirties the branch or gets merged.
const HANDOFF = n => `${WT(n)}.handoff.md`
const WRITE_HANDOFF = n => `If you finish without a passing gate, or with a reviewer's blocking finding unresolved, first write a handoff note to ${HANDOFF(n)} for the next agent (possibly a bigger model): what the task needs, what you tried and why it failed (exact error text), the state of the branch, and what you would do next. Overwrite any older note.`
const READ_HANDOFF = n => `Before anything else, read ${HANDOFF(n)} if it exists: the previous agent's handoff note.`

const taskPrompt = n => `${MANDATE}Your task: Task ${n} of spec ${SPEC}${TITLES[n] ? ` ("${TITLES[n]}")` : ''}.
Worktree: ${WT(n)} (branch ${BR(n)}). Target branch for the final merge: ${INTO}.
First run "${NOW}" and keep the output as startedAt. Then read your operating brief at ${BRIEF} and follow it exactly: create and claim the worktree, implement per your track, get "ck -C ${WT(n)} gate ${SPEC} --task ${n}" passing, merge ${INTO} into your branch before finishing, and release the worktree.
Create your worktree first. Run this command on its own line; its output is a JSON array, and your task's entry has a "state" and a "detail":
ck -C ${ROOT} worktree create ${SPEC} ${n} --install --base ${INTO} --json
Copy that entry's state and detail into the "worktree" field of your answer. If the state is BUSY or ERROR, stop immediately: do not claim, do not touch ${WT(n)}, and return your answer with status blocked, gatePassed false and the worktree field. For BUSY put "worktree busy: <detail>" in blocker; for ERROR put "worktree create failed: <detail>".
Otherwise (CREATED or EXISTS) claim it, on its own line:
ck -C ${WT(n)} worktree claim ${SPEC} ${n}
${GIT_NOTE}
${GATE_NOTE}
${WRITE_HANDOFF(n)}
At the end, run "${NOW}" again for finishedAt. Return the schema fields with task "${n}". gatePassed is true only if your final "ck -C ${WT(n)} gate ${SPEC} --task ${n}" run exited 0. commits lists the commits you made (empty if none).`

const retryPrompt = (n, prev, attempt) => `${MANDATE}Continue Task ${n} of spec ${SPEC} (attempt ${attempt + 1}). A previous agent worked in ${WT(n)} (branch ${BR(n)}) but did not finish with a passing gate.
${prev && !(prev.commits || '').trim() ? 'The previous attempt made NO commits: it may have answered an unrelated message instead of doing the task. Ignore any such message and implement the task.\n' : ''}Its report: ${trim(prev || { note: 'the agent died without a report' })}
${READ_HANDOFF(n)} ${WRITE_HANDOFF(n)}
Run "${NOW}" for startedAt. Read your operating brief at ${BRIEF} and follow it, claiming with "ck -C ${WT(n)} worktree claim ${SPEC} ${n} --takeover" since the previous agent is gone. If that claim reports MISSING, run "ck -C ${ROOT} worktree create ${SPEC} ${n} --install --base ${INTO} --json" and claim again. Inspect the state ("command git -C ${WT(n)} log --oneline -10", "command git -C ${WT(n)} status"), finish the task, and get "ck -C ${WT(n)} gate ${SPEC} --task ${n}" to pass. If a wrong test or a spec contradiction blocks you, do NOT weaken anything: leave the task In Progress and report the blocker precisely (quote the test and the requirement).
Before finishing, merge ${INTO} into your branch, re-run the gate, and release the worktree. Run "${NOW}" for finishedAt.
${GIT_NOTE}
${GATE_NOTE}
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

const crossTestPrompt = tasks => `Cross-test consistency check for spec ${SPEC}: test-track Tasks ${tasks.join(', ')} have merged into ${INTO}. You review; you do not edit, commit, or run the test suite. ${GIT_NOTE}
Test tasks written in parallel drift apart: two test files can each look right on their own and still disagree about the same request, response or record (field names, which model a field lives on, status values, error codes, units). Each implementation can then satisfy only one of them.
1. First pass: run "ck -C ${TARGET} lint ${SPEC} --contracts --json" and read its "unknown" list (names the tests use that design.md never defines; a heuristic, so judge each one).
2. In ${TARGET}, read the test files of Tasks ${tasks.join(', ')} (their **Files:** in specs/${SPEC}/tasks.md), the spec's other test files that exercise the same models or endpoints, and design.md's Data Models and API tables.
3. For each model, endpoint and status set those tests touch, compare how every test file uses it, with each other and with design.md.
Report each disagreement with both files and lines and the design text. It is "blocking" when two tests (or a test and design.md) cannot both be satisfied by one implementation; otherwise "minor". verdict is "changes" only if there is at least one blocking finding. In summary, list the models and endpoints you compared.`

const revisePrompt = (n, review, round) => `${MANDATE}Revise Task ${n} of spec ${SPEC} (review round ${round}). The task's gate passed, but the reviewer found blocking problems:
${trim(review && review.findings, FINDINGS_LIMIT)}
${READ_HANDOFF(n)} ${WRITE_HANDOFF(n)}
Worktree: ${WT(n)} (branch ${BR(n)}). Run "${NOW}" for startedAt. Claim with "ck -C ${WT(n)} worktree claim ${SPEC} ${n} --takeover", fix each blocking finding without weakening any test or editing requirements.md or existing test-plan.md rows, get "ck -C ${WT(n)} gate ${SPEC} --task ${n}" passing again, merge ${INTO} into your branch, re-run the gate, and release the worktree. If a finding is wrong (it contradicts the spec), do not change the code for it: say why in deviations. Run "${NOW}" for finishedAt.
${GIT_NOTE}
${GATE_NOTE}
Return the schema fields with task "${n}".`

// Landing is one ck command: it prechecks with git merge-tree, merges the batch,
// gates it in-process, and bisects a red batch by itself. The agent only runs it.
const landPrompt = tasks => `Landing step for spec ${SPEC}: run ONE command and copy its JSON output. Nothing else: no git commands, no fixes, no retries, no edits. ${GIT_NOTE}
Command:  ck -C ${ROOT} worktree land ${SPEC} ${tasks.join(' ')} --into ${INTO} --gate --install --release --json
It merges the tasks into ${INTO}, gates them, and bisects a red batch itself. ${GATE_NOTE} The same holds for this command, which runs the gate: wait for its EXIT line.
A non-zero exit is normal (1: a task is RED, 2: a CONFLICT, 3: busy, missing or PROTECTED); still copy the JSON. Copy "tasks" (every entry: task, state, detail, files, checkGreen, checkTail, mergedAt), "gates", "bisected", "startedAt" and "finishedAt" exactly as printed. Put a message in error only if the command printed no JSON (say what it printed, or that it was refused).`

const resolvePrompt = (n, files) => `Merge conflict for Task ${n} of spec ${SPEC}: ${BR(n)} conflicts with ${INTO}${files ? ` (${files})` : ''}. ${INTO} was not touched. ${GIT_NOTE} ${GATE_NOTE}
1. Resolve it in the TASK WORKTREE, never in ${TARGET}:
     ck -C ${WT(n)} worktree claim ${SPEC} ${n} --takeover
     command git -C ${WT(n)} merge ${INTO}
   Resolve each conflicted file keeping both sides' intent: tasks.md, keep both sides' task sections; dependency manifests, take the union and re-lock (e.g. "uv lock"); test files, keep every test and assertion from both sides and never weaken one. Read the spec (specs/${SPEC}/design.md, requirements.md) when the two sides disagree about behaviour.
2. In the worktree, "ck -C ${WT(n)} gate ${SPEC} --task ${n}" must pass. Commit the merge, then run "ck -C ${WT(n)} worktree release ${SPEC} ${n}".
3. Do NOT merge into ${INTO} yourself: the workflow lands the task again.
Return resolved, conflicts (the files and how you resolved them), and error if it is not resolved.`

const landFixPrompt = (k, n, tail) => `${MANDATE}Task ${n} of spec ${SPEC} passed its own gate, but landing it on ${INTO} turns the gate red, so it was NOT merged (${INTO} stays green without it). Failure: ${trim(tail, 4000)}
${READ_HANDOFF(n)} ${WRITE_HANDOFF(n)}
Fix it without weakening any test and without editing requirements.md or existing test-plan.md rows. ${GIT_NOTE} ${GATE_NOTE}
1. Work only in the task worktree ${WT(n)} (branch ${BR(n)}). Claim it: ck -C ${WT(n)} worktree claim ${SPEC} ${n} --takeover
2. Bring in the target: command git -C ${WT(n)} merge ${INTO}  (resolve any conflict keeping both sides' tests).
3. Reproduce the failing check in the worktree, fix the code (or a clearly broken fixture or harness), commit with a message starting "fix:", and get "ck -C ${WT(n)} gate ${SPEC} --task ${n}" passing.
4. Release: ck -C ${WT(n)} worktree release ${SPEC} ${n}. Do NOT merge into ${INTO}: the workflow lands the task again (fix number ${k}).
If the only way to green is to weaken a test or change a requirement, stop and explain precisely instead.
Return green=true only if the task gate passes in ${WT(n)} after your commit, else failing with the reason.`

const verifyPrompt = what => `Verification (${what}) for spec ${SPEC}. In ${TARGET} (branch ${INTO}):
1. Refresh dependencies if the project has an install step (e.g. "uv sync -q --directory ${TARGET}").
2. Run:  ck -C ${TARGET} gate ${SPEC}
   It checks every Done task: acceptance boxes, the tests that should pass by now, test collection, and that no test was weakened.
${GATE_NOTE}
Do not modify anything. Return green=true only if "ck -C ${TARGET} gate ${SPEC}" exited 0; otherwise put the failing checks and the relevant output tail in failing.`

const recheckPrompt = n => `Re-check for spec ${SPEC}. In ${TARGET} (branch ${INTO}): refresh dependencies if needed (e.g. "uv sync -q --directory ${TARGET}"), then run "ck -C ${TARGET} gate ${SPEC}${n ? ` --task ${n}` : ''}". ${GATE_NOTE} Do not modify anything. Return green=true only if it exited 0; otherwise put the failing part in failing.`

const fixPrompt = (k, what, failure) => `${INTO} is red (${what}) for spec ${SPEC}. Failure: ${trim(failure, 4000)}
Fix it without weakening any test and without editing requirements.md or existing test-plan.md rows. ${GIT_NOTE} ${GATE_NOTE}
1. Run: ck -C ${ROOT} worktree create ${SPEC} fix${k} --install --base ${INTO}   (worktree ${WT(`fix${k}`)}). Work only there.
2. Diagnose with the failing check, fix the code (or a clearly broken fixture or harness), and confirm the failing check passes in the fix worktree.
3. Commit with a message starting "fix:", then run: ck -C ${ROOT} worktree merge ${SPEC} fix${k} --into ${INTO}
If the only way to green is to weaken a test or change a requirement, stop and explain precisely instead.
Return green=true only if ${INTO} is green after your merge, else failing with the reason.`

// --- decisions (pure; loaded by tests) ---
// Every scheduling rule that allows, refuses or holds something. No script globals, no agent/log/args,
// no clock and no I/O: tests/js/load_decisions.mjs cuts this block out and evaluates it on its own.

const decisionIds = v => [...(v || [])].map(x => String(x && typeof x === 'object' ? x.task : x))
const hasKey = (o, k) => !!o && Object.prototype.hasOwnProperty.call(o, k)

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

// --- end decisions ---

// --- scheduler ---------------------------------------------------------------

const running = new Map()    // task -> promise of its record
// Tasks holding a slot: an implementing agent (first attempt, retry, revision) is at work.
// Review and the merge queue don't hold one, so a slow merge never keeps a ready task waiting.
const working = new Set()
const records = {}           // task -> record
const failed = new Map()     // task -> reason
const mergedInRun = new Set()  // tasks this run landed: they stay Done whatever a plan snapshot says (1.6)
let stopLaunching = false
let stopReason = null
let redTarget = false
let mergedCount = 0
let fixCount = 0
let refills = 0
const fullGates = []
const fixerEscalations = []
// Landing queue: finished tasks wait here; one `ck worktree land` runs at a time and
// takes up to batchWindow of them. Waiting here never holds a slot.
const landQueue = []         // { n, resolve }
let landingBusy = false
let landingIdle = Promise.resolve()
let batchWindow = MERGE_BATCH
const landings = []
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
      r = await call('cross', crossTestPrompt(merged), { label: `cross-test wave ${w}`, phase: phaseOf(merged[0]), schema: REVIEW_SCHEMA, ...RISKY_REVIEW })
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
  return (async () => {
    try {
      return await agent(prompt, opts)
    } finally {
      const i = inFlight.indexOf(entry)
      if (i >= 0) inFlight.splice(i, 1)
      poke()
    }
  })()
}

// The scheduler's view of the plan (applyPlanView) and what runs now.
let view = { order: [], deps: {}, done: new Set(), cycles: [], verifyCycles: [] }
let current = null          // the last plan applied
let fallbackReady = []      // without args.deps: the ready list of the plan that just returned, used once
let planRun = null          // the plan agent in flight, if any
let queuedTrigger = null    // a trigger that arrived while one was in flight: one follow-up
let idleArmed = true
let deferring = false
const excluded = () => new Set([...running.keys(), ...failed.keys(), ...mergedInRun])
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
  for (let attempt = 0; attempt < 2; attempt++) {
    const label = `plan ${refills}${attempt ? ' retry' : ''}`
    const exclude = [...excluded()]
    let p = null
    try { p = await call('plan', planPrompt(exclude), { label, phase: 'Plan', schema: PLAN, ...LOW }) } catch (e) { p = { error: e && e.message } }
    // An "error" next to a usable plan is a remark, not a failure; an empty plan with an error is a failure.
    if (usablePlan(p) && !(p.error && !p.done.length && !p.remaining.length)) {
      const n = normalizePlan(p)
      // A task a plan reports as busy is left out of its ready list: it is not a mismatch.
      if (planConsistent(n, [...exclude, ...n.busy.map(b => b.task)])) {
        if (p.error) log(`${label}: the planning agent left a note (not a failure): ${trim(p.error, 300)}`)
        current = n
        view = applyPlanView(n, mergedInRun)
        for (const b of n.busy) {
          if (!failed.has(b.task) && !running.has(b.task)) {
            failed.set(b.task, `worktree busy: ${b.detail}`)
            records[b.task] = { task: b.task, wave: waveOf(b.task), ok: false, reason: `worktree busy: ${b.detail}` }
          }
        }
        applyLint(n)
        fallbackReady = n.ready
        if (trigger === 'landing') idleArmed = false  // this plan just re-read tasks.md after a landing
        return n
      }
      planFailures++
      log(`${label}: the plan is inconsistent (its ready list does not follow from its dependencies)${attempt ? '' : ', retrying'}`)
      continue
    }
    planFailures++
    log(`${label}: no usable plan (${(p && p.error && trim(p.error, 300)) || 'no result'})${attempt ? '' : ', retrying'}`)
  }
  return null
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
  if (!rec.ok) idleArmed = true  // a merged task was already followed by its landing plan; a failed one may change what is ready
  if (!rec.ok) {
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
    ready = fallbackReady.map(String).filter(n => !ctx.exclude.has(n) && !view.done.has(n))
    fallbackReady = []
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
    if (!failed.has(n) && !running.has(n)) {
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

async function fixAndRecheck(what, failure, n) {
  const ph = n ? phaseOf(n) : 'Final gate'
  fixCount++
  let fix = await call('fix', fixPrompt(fixCount, what, failure), { label: `fix ${fixCount} (${what})`, phase: ph, schema: VERIFY, ...FIX })
  let recheck = await call('recheck', recheckPrompt(n), { label: `re-check ${fixCount}`, phase: ph, schema: VERIFY, ...ORCH })
  for (const model of FIX_UP) {
    if (recheck && recheck.green) break
    fixCount++
    log(`${what}: still red after the fix, escalating the fixer to ${model}`)
    fix = await call('fix', fixPrompt(fixCount, what, recheck), { label: `fix ${fixCount} (${what}) [${model}]`, phase: ph, schema: VERIFY, ...escalated(FIX, model) })
    recheck = await call('recheck', recheckPrompt(n), { label: `re-check ${fixCount}`, phase: ph, schema: VERIFY, ...ORCH })
    fixerEscalations.push({ what, model, green: !!(recheck && recheck.green) })
  }
  return { fix, recheck, green: !!(recheck && recheck.green) }
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
  const tasks = batch.map(b => b.n)
  const used = batchWindow
  let res = null
  try {
    res = await call('land', landPrompt(tasks), { label: `land ${tasks.join(' ')}`, phase: phaseOf(tasks[0]), schema: LAND, ...LOW })
  } catch (e) {
    res = { tasks: [], error: e && e.message }
  }
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
    let v = await call('full', verifyPrompt(`full gate after ${mergedCount} merges`), { label: `full gate @${mergedCount}`, phase: phaseOf(tasks[0]), schema: VERIFY, ...ORCH })
    if (!v || !v.green) {
      log(`Full gate red after ${mergedCount} merges, one fix attempt`)
      const f = await fixAndRecheck(`full gate after ${mergedCount} merges`, v)
      v = { ...(v || {}), fix: f, green: f.green }
    }
    fullGates.push({ afterMerges: mergedCount, green: !!v.green })
    if (!v.green) {
      redTarget = true
      stopLaunching = true
      stopReason = stopReason || `${INTO} is red: full gate after ${mergedCount} merges`
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
    if (t.state === 'CONFLICT') {
      if (resolves >= MAX_RESOLVES) return { merged: false, error: `still conflicting after ${resolves} resolution(s): ${(t.files || []).join(', ')}` }
      resolves++
      rec.resolves = resolves
      log(`Task ${n}: conflicts with ${INTO}, handing it to the resolver`)
      const r = await call('resolve', resolvePrompt(n, (t.files || []).join(', ') || t.detail), { label: `resolve ${n}${resolves > 1 ? ` round ${resolves}` : ''}`, phase: phaseOf(n), schema: RESOLVED, ...RESOLVE })
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
        const f = await call('fix', landFixPrompt(fixCount, n, failure), { label: `fix ${fixCount} (Task ${n} red on landing)${model ? ` [${model}]` : ''}`, phase: phaseOf(n), schema: VERIFY, ...(model ? escalated(FIX, model) : FIX) })
        if (model) { lastEscalation = { what: `landing Task ${n}`, model, green: false }; fixerEscalations.push(lastEscalation) }
        fixedGreen = !!(f && f.green)
        if (!fixedGreen) failure = (f && f.failing) || failure
      }
      continue
    }
    if (t.state === 'SKIP') return { merged: false, error: 'no commits' }
    if (t.state === 'PROTECTED') return { merged: false, error: `needs a human: changes protected files (${(t.files || []).join(', ')}); not landed. ${t.detail || ''}` }
    return { merged: false, error: `${t.state}: ${t.detail || ''}` }
  }
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
    let r = await call('task', taskPrompt(n), { label: label(n), phase: phaseOf(n), schema: RESULT, ...codeOpts(n) })
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
    while (!ok(r) && rec.attempts <= MAX_RETRIES && !busy(r)) {
      log(`Task ${n}: ${r && !(r.commits || '').trim() ? 'no commits' : 'gate not passed'}, retry ${rec.attempts}/${MAX_RETRIES}`)
      r = await call('task', retryPrompt(n, r, rec.attempts), { label: `${label(n)} retry ${rec.attempts}`, phase: phaseOf(n), schema: RESULT, ...codeOpts(n) })
      rec.attempts++
      rec.startedAt = rec.startedAt || (r && r.startedAt)
    }
    for (const model of ladderOf(n)) {
      if (ok(r) || busy(r)) break
      log(`Task ${n}: still failing after ${rec.attempts} attempt(s), escalating to ${model}`)
      r = await call('task', retryPrompt(n, r, rec.attempts), { label: `${label(n)} retry ${rec.attempts} (${model})`, phase: phaseOf(n), schema: RESULT, ...escalated(codeOpts(n), model) })
      rec.attempts++
      rec.escalations.push({ stage: 'retry', model })
      rec.startedAt = rec.startedAt || (r && r.startedAt)
    }
    releaseSlot(n)
    if (ok(r) && REVIEW_ON) {
      let review = await call('review', reviewPrompt(n, r), { label: `review ${n}`, phase: phaseOf(n), schema: REVIEW_SCHEMA, ...reviewOpts(n) })
      let round = 0
      while (review && review.verdict === 'changes' && round < REVIEW_ROUNDS && ok(r)) {
        round++
        log(`Task ${n}: reviewer asked for changes, revision ${round}/${REVIEW_ROUNDS}`)
        working.add(n)  // a revision takes a slot but never waits for one: the task is nearly done
        r = await call('revise', revisePrompt(n, review, round), { label: `${label(n)} revise ${round}`, phase: phaseOf(n), schema: RESULT, ...codeOpts(n) })
        releaseSlot(n)
        if (ok(r)) review = await call('review', reviewPrompt(n, r), { label: `review ${n} round ${round + 1}`, phase: phaseOf(n), schema: REVIEW_SCHEMA, ...reviewOpts(n) })
      }
      for (const model of ladderOf(n)) {
        if (!(review && review.verdict === 'changes' && ok(r))) break
        round++
        log(`Task ${n}: blocking findings remain, escalating the revision to ${model}`)
        working.add(n)
        r = await call('revise', revisePrompt(n, review, round), { label: `${label(n)} revise ${round} (${model})`, phase: phaseOf(n), schema: RESULT, ...escalated(codeOpts(n), model) })
        releaseSlot(n)
        rec.escalations.push({ stage: 'revise', model })
        if (ok(r)) review = await call('review', reviewPrompt(n, r), { label: `review ${n} round ${round + 1}`, phase: phaseOf(n), schema: REVIEW_SCHEMA, ...reviewOpts(n) })
      }
      // model/effort: who judged the task, so an audit can count escapes (approved bugs) per reviewer tier.
      const by = { model: reviewOpts(n).model || 'inherited', effort: reviewOpts(n).effort || 'inherited', riskTag: rec.riskTag }
      rec.review = review ? { verdict: review.verdict, rounds: round, findings: review.findings, summary: review.summary, ...by } : { verdict: 'unreviewed', rounds: round, ...by }
      if (!review) log(`Task ${n}: review agent returned nothing; merging unreviewed`)
      else if (review.verdict === 'changes' && ok(r)) {
        rec.result = r
        rec.finishedAt = r && r.finishedAt
        rec.reason = `review: blocking findings remain after ${round} revision(s): ${trim(review.findings.filter(f => f.severity === 'blocking').map(f => f.issue), FINDINGS_LIMIT)}`
        return rec
      }
    }
    rec.result = r
    rec.finishedAt = r && r.finishedAt
    if (!ok(r)) {
      rec.reason = r ? r.blocker || `gate not passed (status ${r.status})` : 'agent died without a report'
      return rec
    }
    const m = await landTask(n, rec)
    rec.merge = m
    rec.mergedAt = m && m.mergedAt
    if (!m || !m.merged) {
      rec.reason = `merge failed: ${(m && m.error) || 'no result'}`
      return rec
    }
    rec.ok = true
    log(`Task ${n}: done and merged into ${INTO}`)
    return rec
  } catch (e) {
    rec.reason = `error: ${e && e.message}`
    return rec
  }
}

phase('Plan')
const startPlan = await requestPlan('start')
if (!startPlan) return { halted: true, refused: false, reason: PLAN_FAILED, into: INTO }
if (startPlan.cycles.length || startPlan.verifyCycles.length) {
  log('Refusing to launch: the plan has a cycle. Fix tasks.md (see `ck lint`) and run again.')
  return {
    halted: true,
    refused: true,
    reason: 'plan has a cycle; nothing was started',
    cycles: startPlan.cycles,
    verifyCycles: startPlan.verifyCycles,
    into: INTO,
  }
}
const initialPlan = startPlan
const initiallyDone = startPlan.done.length
doneAtStart = new Set(startPlan.done.map(String))
log(`${startPlan.remaining.length} task(s) remaining, ${initiallyDone} already Done; starting ${Math.min(startPlan.ready.length, MAX_CONCURRENT)}`)
schedule()
poked = false

// Wakes on a settled agent call, a freed slot, a plan result or a finished task. The wake-up waits a few
// microtask hops, so the continuation of the call that settled makes its own follow-up request (a review
// after an implementer, a land after an approval) before free slots are counted (3.9).
while (running.size || planRun) {
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
  finalGate = await call('full', verifyPrompt('final'), { label: 'final gate', phase: 'Final gate', schema: VERIFY, ...ORCH })
  if (!finalGate || !finalGate.green) {
    log(`Final gate red, one fix attempt`)
    const f = await fixAndRecheck('final gate', finalGate)
    finalGate = { ...(finalGate || {}), fix: f, green: f.green }
  }
  if (!finalGate.green) redTarget = true
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
  outsideFiles: r.result && r.result.outsideFiles,
  deviations: r.result && r.result.deviations,
  fixedAfterMerge: !!r.fixes,
  review: r.review,
  escalations: r.escalations || [],
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
const REPORT = {
  type: 'object',
  properties: {
    head: { type: 'string' },
    merged: { type: 'array', items: { type: 'object', properties: { task: { type: 'string' }, commit: { type: 'string' }, mergedAt: { type: 'string' } }, required: ['task'] } },
    done: { type: 'array', items: { type: 'object', properties: { task: { type: 'string' } }, required: ['task'] } },
    failed: { type: 'array', items: { type: 'object', properties: { task: { type: 'string' }, branch: { type: 'string' }, ahead: { type: 'number' }, claim: { type: 'string' } }, required: ['task'] } },
    blocked: { type: 'array', items: { type: 'object', properties: { task: { type: 'string' }, on: { type: 'array', items: { type: 'string' } } }, required: ['task', 'on'] } },
    unstarted: { type: 'array', items: { type: 'object', properties: { task: { type: 'string' } }, required: ['task'] } },
    error: { type: 'string', description: 'only if the command printed no JSON (or was refused): what happened' },
  },
  required: ['merged', 'done', 'failed', 'blocked', 'unstarted'],
}
const reportPrompt = () => `Report step for spec ${SPEC}: run ONE command and copy its JSON output. Nothing else: no other commands, no edits. ${GIT_NOTE}
Command:  ck -C ${ROOT} worktree report ${SPEC} --into ${INTO} --json
Copy "head", "merged" (task, commit, mergedAt), "done", "failed" (task, branch, ahead, claim), "blocked" (task, on) and "unstarted" exactly as printed, every entry. Put a message in error only if the command printed no JSON (say what it printed, or that it was refused).`
phase('Report')
let gitReport = null
try { gitReport = await call('report', reportPrompt(), { label: 'report', phase: 'Report', schema: REPORT, ...LOW }) } catch (e) { gitReport = { error: e && e.message } }
const GIT_LISTS = ['merged', 'done', 'failed', 'blocked', 'unstarted']
const fromGit = !!gitReport && !gitReport.error && GIT_LISTS.every(k => Array.isArray(gitReport[k]))
const ids = list => (list || []).map(e => taskId(e && typeof e === 'object' ? e.task : e))
const byNum = (a, b) => (Number(a) - Number(b)) || String(a).localeCompare(String(b))
const startDone = new Set((initialPlan.done || []).map(taskId))
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
  const failedTasks = [...new Set([...agentFailed.keys(), ...gitFailed.keys()])].filter(n => !landed.has(n))
  failedList = failedTasks.map(task => {
    const g = gitFailed.get(task)
    const reason = agentFailed.get(task) || `${g.ahead || 'some'} commit(s) on ${g.branch || BR(task)} never landed${g.claim ? `; claimed: ${g.claim}` : ''}`
    return g ? { task, reason, git: { branch: g.branch, ahead: g.ahead, claim: g.claim } } : { task, reason }
  })
  const failedSet = new Set(failedTasks)
  blocked = (gitReport.blocked || []).map(e => ({ task: taskId(e.task), on: ids(e.on) })).filter(b => !failedSet.has(b.task))
  readyLeft = ids(gitReport.unstarted).filter(n => !failedSet.has(n))
  notStarted = [...readyLeft, ...blocked.map(b => b.task)].sort(byNum)
} else {
  log(`Report: no usable git report (${(gitReport && gitReport.error && String(gitReport.error).slice(0, 300)) || 'no result'}); using the agents' results`)
  merged = taskReport.filter(t => t.ok).map(t => t.task)
  const mergedSet = new Set(merged)
  failedList = [...agentFailed.entries()].filter(([t]) => !mergedSet.has(t)).map(([task, reason]) => ({ task, reason }))
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
  ? [...roots.entries()].sort((a, b) => byNum(a[0], b[0])).map(([task, blocks]) => ({ task, reason: why(task), blocks: blocks.sort(byNum) }))
  : failedList.map(f => ({ task: f.task, reason: f.reason, blocks: [] }))
// Outcome: "complete" (nothing left), "blocked" (nothing left that can start: a clean stop with
// what it waits on), or "halted" (a red target, a halt on failure, or a failed plan step that
// left a ready task unstarted). A failed plan step with nothing ready left is not a halt.
let outcome = 'complete'
let reason = stopReason
const planFailed = stopReason === PLAN_FAILED
if (redTarget) outcome = 'halted'
else if (stopReason && !planFailed && notStarted.length) outcome = 'halted'
else if (planFailed && (!fromGit || readyLeft.length)) outcome = 'halted'
else if (notStarted.length || failedList.length) {
  outcome = 'blocked'
  if (planFailed) log('A planning step failed, but every remaining task was blocked anyway: nothing was left to start')
  const top = blockedOn.length ? blockedOn : failedList
  reason = top.length
    ? `blocked on: ${top.map(b => `Task ${b.task} (${String(b.reason).slice(0, 300)})`).join('; ')}`
    : `not started: ${notStarted.map(n => `Task ${n}`).join(', ')}`
} else if (planFailed) reason = null
if (outcome === 'blocked') log(reason)

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
  finalGate,
  crossTests,
  stats: { initiallyDone, merged: merged.length, failed: failedList.length, notStarted: notStarted.length, planSteps: refills, planFailures, fixes: fixCount, landings: landings.length, bisected: landings.filter(l => l.bisected).length },
}
