// A fake project for the spec-implement workflow tests: `ck plan` semantics over deps/done, task
// durations, failures, timestamps. Shared by spec_implement.test.mjs and the bridge.
import { execFile } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { sleep as realSleep } from './load_workflow.mjs'

// --- execCk: the fake agents record progress events through the real CLI -------------------------------------
// The shipped prompts put their claim and land commands against paths the fake project never creates, and land
// carries --gate --install --release, so the commands are never run as written. The agent parses the progress
// flags out of them and runs `ck progress event ...` instead (the claim and landing kinds are accepted only under
// CK_PROGRESS_TEST_KINDS=1, which is set here). Everything else the CLI prints comes from the real subprocess.
const FLAG_VALUE = /^[^-\s"][^\s"]*$/
function parseFlags(text) {
  const toks = text.trim().split(/\s+/)
  const flags = {}
  for (let i = 0; i < toks.length; i++) {
    if (!toks[i].startsWith('--')) continue
    const next = toks[i + 1]
    if (next !== undefined && FLAG_VALUE.test(next)) { flags[toks[i].slice(2)] = next; i++ } else flags[toks[i].slice(2)] = true
  }
  return flags
}
// The text of the command after `after` up to the end of the line or the closing double quote.
const rest = (prompt, re) => { const m = re.exec(prompt); return m ? m : null }
export function findClaim(prompt) {
  const m = rest(prompt, /worktree claim (\S+) (\d+)([^\n"`]*)/)
  return m ? { spec: m[1], task: m[2], flags: parseFlags(m[3]) } : null
}
export function findLand(prompt) {
  const m = rest(prompt, /worktree land (\S+) ([^\n"`]*)/)
  if (!m) return null
  const tasks = []
  for (const t of m[2].trim().split(/\s+/)) { if (!/^\d+$/.test(t)) break; tasks.push(t) }
  return { spec: m[1], tasks, flags: parseFlags(m[2].slice(m[2].indexOf('--') < 0 ? m[2].length : m[2].indexOf('--'))) }
}
const eventArgv = (spec, kind, run, extra = []) => ['progress', 'event', spec, kind, ...(run ? ['--run', run] : []), ...extra]
// `ck ... plan ... --failed`, `ck ... progress event ...` and `ck --version` commands in a prompt, as written (minus -C DIR).
export function writtenCommands(prompt) {
  const out = []
  // A double-quoted value (a create phase title such as "Design + Test Plan") stays one argument.
  for (const m of prompt.matchAll(/\bck (?:-C \S+ )?((?:plan|progress event)(?: |"[^"\n]*"|[^\n"`])*|--version)/g)) {
    if (m[1].startsWith('plan') && !/--failed/.test(m[1])) continue
    out.push([...m[1].trim().matchAll(/"([^"]*)"|(\S+)/g)].map(t => (t[1] !== undefined ? t[1] : t[2])).filter(t => t !== ''))
  }
  return out
}

// Run one `ck` command through execCk {cmd, env, cwd, dry} (in dry mode, or with no execCk, only record it in
// log). The outcome {argv, code, error, stdout, stderr} is pushed to results, never swallowed: tests assert the
// CLI really ran. Returns the parsed --json output, else the trimmed stdout (null in dry mode).
export async function execCkRun(execCk, argv, { log, results, external = p => p, input = null }) {
  log.push(argv)
  if (!execCk || execCk.dry) return null
  const env = { ...process.env, ...(execCk.env || {}), CK_PROGRESS_TEST_KINDS: '1' }
  const [bin, ...pre] = execCk.cmd
  const res = await external(new Promise(resolve => {
    const child = execFile(bin, [...pre, ...argv], { env, cwd: execCk.cwd || process.cwd(), encoding: 'utf8' }, (err, out, errOut) => {
      resolve({ argv, code: err ? (typeof err.code === 'number' ? err.code : -1) : 0, error: err ? String(err.message) : null, stdout: out || '', stderr: errOut || '' })
    })
    if (input !== null) { child.stdin.on('error', () => {}); child.stdin.end(input) }
  }))
  results.push(res)
  try { return JSON.parse(res.stdout) } catch { return res.stdout.trim() }
}

// Same as cli/worktree.py::_chain_lengths: for each open task, the open tasks on its longest chain of dependents.
function chainLengths(deps, done) {
  const dependents = {}
  for (const n of Object.keys(deps)) dependents[n] = []
  for (const [n, ds] of Object.entries(deps)) for (const d of ds) if (d in dependents && !done.has(n)) dependents[d].push(n)
  const lengths = {}
  const length = (n, stack) => {
    if (!(n in lengths)) {
      const below = dependents[n].filter(m => !stack.has(m)).map(m => length(m, new Set([...stack, m])))
      lengths[n] = 1 + Math.max(0, ...below)
    }
    return lengths[n]
  }
  const out = {}
  for (const n of Object.keys(deps)) if (!done.has(n)) out[n] = length(n, new Set([n]))
  return out
}


// A call is a thin ck call when it carries the CK_CALL schema (an object with an exitCode property) or an agentType.
export const isCkCall = opts => !!opts && (!!opts.agentType || !!(opts.schema && opts.schema.properties && opts.schema.properties.exitCode))
export const THIN_LABEL = /^(plan \d+|land |report$|park \d+|relink \d+|full gate @|re-check |final gate$|gate$|run start$|audit$|testfix ledger |testfix prove |arbiter-)/
const GATE_LABEL = /^(full gate @|re-check |final gate$|gate$)/
// today's {green} answer as a ck-gate/1 object
export function gateJson(green, spec = 'demo') {
  const failure = { node: 'tests/test_x.py::test_tc1_x', tcId: 'TC-1', tasks: ['1'], attributedBy: 'tc', file: 'tests/test_x.py', kind: 'assertion', message: 'assert 0 == 1', truncated: false, command: 'pytest', status: 'regression', excusedBy: [], rerun: 'failed', class: null, check: null, missingCommand: null }
  return { schema: 'ck-gate/1', spec, tasks: [], ok: green, attribute: true, checks: [], failures: green ? [] : [failure], summary: { regressions: green ? 0 : 1, excused: 0, flaky: 0, unattributed: 0 }, warnings: [], durationS: 0 }
}
// A raw {code, stdout} gateAnswers entry versus a json one.
const isRawGate = e => e && typeof e === 'object' && 'code' in e && 'stdout' in e && Object.keys(e).length <= 3
const parseJson = t => { try { const v = JSON.parse(t); return v && typeof v === 'object' ? v : null } catch { return null } }
const never = () => new Promise(() => {})

// --- arbiter helpers -------------------------------------------------------------------------------------------
// The one `ck ... arbiter ...` (or `ck ... worktree land ...`) line of a thin step's prompt, as an argv.
export function arbiterArgv(prompt) {
  // Line-anchored (optionally behind a `$ ` or list marker): prose that mentions a ck command is not the command line.
  const m = /^[ \t$>*-]*ck ((?:-C (?:"[^"\n]*"|\S+) )?(?:arbiter|principles|worktree land)(?: |"[^"\n]*"|[^\n"`])*)/m.exec(prompt)
  if (!m) return null
  return [...m[1].trim().matchAll(/"([^"]*)"|(\S+)/g)].map(t => (t[1] !== undefined ? t[1] : t[2])).filter(t => t !== '')
}
// The prompt's one fenced JSON block, as the step's stdin (null when there is none).
export function arbiterStdin(prompt) {
  const m = /```(?:json)?[ \t]*\n([\s\S]*?)\n```/.exec(prompt)
  return m ? m[1] : null
}
const IDENT = { GIT_AUTHOR_NAME: 'arbiter', GIT_AUTHOR_EMAIL: 'arbiter@example.invalid', GIT_COMMITTER_NAME: 'arbiter', GIT_COMMITTER_EMAIL: 'arbiter@example.invalid' }
const git = (cwd, args) => new Promise((resolve, reject) => {
  execFile('git', ['-C', cwd, ...args], { encoding: 'utf8', env: { ...process.env, ...IDENT } }, (err, out, errOut) => (err ? reject(new Error(`git ${args.join(' ')}: ${errOut || err.message}`)) : resolve(out)))
})
// Really write and commit `files` in the arbiter checkout the prompt names (an absolute path ending `-task-arbiter<k>`).
async function arbiterWrite(prompt, files, state, n) {
  const m = /(\/[^\s"'`]*-task-arbiter\d+)/.exec(prompt)
  if (!m) throw new Error(`arbiterEdits[${n}]: the prompt names no arbiter checkout (a path ending -task-arbiter<k>)`)
  const dir = m[1]
  for (const [rel, text] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(dir, rel)), { recursive: true })
    fs.writeFileSync(path.join(dir, rel), text)
  }
  await git(dir, ['add', '--', ...Object.keys(files)])
  await git(dir, ['commit', '-q', '-m', `arbiter: task ${n} ruling`])
  state.arbiterEdited[n] = { dir, files: Object.keys(files) }
}

// Options beyond the scenario knobs:
//   planDurations: {k: ms}                 plan call k takes that long
//   planSeq: [{ready?, deps?, done?, remaining?, cycles?, verifyCycles?, error?}, ...]
//                                          canned plan answers consumed in invocation order (call k gets
//                                          planSeq[k-1]; exhausted or absent -> default `ck plan` semantics).
//                                          Each entry is merged over the default answer, so {ready} overrides
//                                          the ready list while keeping deps truthful and {} is a healthy answer.
//   badReady: [ids]                        shorthand: every plan answer's ready is this bad list (deps stay
//                                          truthful); a planSeq entry for the call takes precedence
//   risk: {n: 'safety'|'normal'}            passed through as the plan answer's risk data
//   externalDone: {atPlan: k, tasks}       from plan call k on, those tasks are Done on the target (never started here)
//   planFail: [k, ...]                     plan call k and its retry answer {error: 'boom'} with empty arrays
//   guardStops: {n: ceiling|'always'}      task n returns guardStop on every attempt whose model is at or below ceiling
//                                          (haiku < sonnet < opus < fable); 'always' never resolves
//   guardStopIn: {n: 'revise'}             the guard stop of task n happens in its first revise call, not its task call
//   parkOutcome: {parked, notification, detail}  answer of the `park <N>` agent (default parked, sent, '')
//   testConflicts: {n: {conflict, attempts}}  task n's first `attempts` agents (default all) return status blocked
//                                          with that testConflict; later ones pass
//   relinkOutcome: {fixable, applied, committed, reason}  answer of the `relink <N>` agent (default refused:
//                                          not mechanical); a fixable+applied answer drops N's verify cycle from
//                                          later plans, as the relinked tasks.md would
//   worktree: {n: {state, detail}}         the first task agent of n returns that worktree outcome (default CREATED)
//   -- unattended-correctness additions (all optional, additive) --
//   gateAnswers: [json | {code, stdout} | 'throw' | 'hang']   consumed in order by the gate calls: a direct `ck gate`
//                                          argv, or (with an envelope) the thin labels `full gate @k`, `re-check k`,
//                                          `final gate`. 'throw' makes the call throw, 'hang' never settles. An entry
//                                          may be a ck-gate/1 or ck-gate-compact/1 object. Exhausted: today's {green}
//                                          answer turned into a ck-gate/1 object.
//   taskFailures: {n: Failure[] | Failure[][]}  the `failures` of task n's non-passing attempts: one list for every attempt, or one list per attempt (attempt k gets the k-th; the last repeats)
//   testConflictsList: {n: TestConflict[] | {conflicts, attempts}}   task n's first attempts return status blocked
//                                          with that `testConflicts` list
//   testFix: {file: {verdict: 'fixed'|'test-correct'|'cannot-fix', proof, scope, review, finding}}
//                                          answers `testfix <k> (Task n)` (the k-th file), `testfix review <k>`,
//                                          `testfix prove <k>` and `ck testfix prove`
//   priorFixes: {file: truthy}             `ck testfix check ... --test NODE` answers {allowed, alreadyFixed: [{node, at, fix}]}
//   crossTest: answer | [answers]          replaces the cross-test reviewer's answer (a list: one per call, the last repeats)
//   fidelity: json                         answer of `ck lint --fidelity`
//   fixtureStatus: json                    answer of `ck fixtures status`
//   owns / overlaps / testVerifies / provides: merged into every plan answer (harness convenience: owns is not in the design's plan additions; missingCommand on the graph answer is likewise a convenience, the design carries it on the gate Failure)
//   missingCommand: {command, providers}   carried by the `ck graph` answer
//   audit: json | 'throw'                  answer of `audit` / `ck worktree audit`
//   runStart: json                         answer of `run start` / `ck worktree start`
// Thin labels: `plan <k>`, `land ...`, `report`, `park <n>`, `relink <n>`, `full gate @k`, `re-check k`, `final gate`,
// `run start`, `audit`, `testfix ledger <k>`, `testfix prove <k>`. Whenever the call carries `schema: CK_CALL` or an
// `agentType`, the answer is wrapped in {exitCode, json}; otherwise it is today's answer unchanged.
//   -- run-principles additions (all optional, additive) --
//   rulings: {n: [answer, ...]}            answers of `arbiter task <n>` and `arbiter task <n> risky`, in order (the
//                                          last repeats); null, 'throw' or any object. A call for a task with no
//                                          scripted answer throws, naming the label.
//   auditFindings: {n: answer}             answer of `audit task <n>` (default {problems: [], summary}: no problems)
//   arbiterEdits: {n: {files: {path: text}}}  what the `arbiter task <n>` agent really writes and commits (real git) in
//                                          the `...-task-arbiter<k>` checkout its prompt names
//   arbiterStepFailures: {record: true}    the `arbiter-<step>` thin step reports a failed command (exit 1, {error}),
//                                          after the real command has run
//   ledgerFail: true                       `testfix ledger <k>` returns exit 1 with {error}
//   reviews entries may carry {finding: 'serves-no-requirement'}: each finding then has noRequirement: true
// Labels starting `arbiter-` run the one `ck ... arbiter ...` (or `ck ... worktree land ...`) line of their prompt, with
// the prompt's one fenced JSON block as stdin, through execCkRun, and return {exitCode, json}; without execCk they throw.
export function project({ deps, done = [], durations = {}, failUntil = {}, noCommitsFirst = [], busy = [], cycles = [], verifyCycles = [], verifyCyclesFromStart = false, checkRed = [], fullRed = false, relink = null, reviews = {}, mergeDurations = {}, reviewDurations = {}, recheckRed = 0, conflicts = [], redLands = {}, pairConflicts = [], reviewIssue = null, protectedTasks = [], gitReport = null, conflictRounds = {}, planDurations = {}, externalDone = null, planFail = [], worktree = {}, guardStops = {}, guardStopIn = {}, parkOutcome = null, relinkOutcome = null, testConflicts = {}, execCk = null, etaAnswer = null, sleep = realSleep, external = p => p, planSeq = null, badReady = null, risk = null, gateAnswers = [], taskFailures = {}, testConflictsList = {}, testFix = {}, priorFixes = {}, crossTest = null, fidelity = null, fixtureStatus = null, owns = null, overlaps = null, testVerifies = null, provides = null, missingCommand = null, audit = null, runStart = null, rulings = {}, auditFindings = {}, arbiterEdits = {}, arbiterStepFailures = {}, ledgerFail = false }) {
  let rechecksLeftRed = recheckRed
  let planCalls = 0  // plan agent invocations so far: call k consumes planSeq[k-1]
  // RED landings left per task: checkRed tasks are red on their first landing, redLands sets a count.
  const redLeft = { ...Object.fromEntries(checkRed.map(n => [n, 1])), ...redLands }
  // Coverage of each review label: [n] for `review N [round k]`; the id list for
  // `review-wave <wave> tasks <ids>`; the prompt's task list as a fallback.
  const reviewTasksOf = (label, promptText) => {
    let m = label.match(/^review-wave \d+ tasks ([\d ]+)$/)
    if (m) return m[1].split(' ').filter(Boolean)
    m = label.match(/^review (\d+)/)
    if (m) return [m[1]]
    m = String(promptText).match(/[Tt]asks ([\d, ]+)/)
    if (m) return m[1].split(/[,\s]+/).filter(Boolean)
    return []
  }
  const state = { rulingCalls: {}, auditCalls: [], arbiterEdited: {}, arbiterSteps: [], execResults: [], deps: { ...deps }, done: new Set(done), merged: new Set(), events: [], prompts: {}, opts: {}, optsList: [], reviewCount: {}, reviewTasks: {}, relinked: new Set(), relinks: [], attempts: {}, plans: 0, merging: 0, maxMerging: 0, running: 0, maxRunning: 0, landings: [], parks: [], revises: {}, resolved: new Set(), conflictsHit: {}, execLog: [] }
  let clock = Date.parse('2026-10-04T10:00:00Z')
  const stamp = () => new Date((clock += 60000)).toISOString().replace('.000', '')
  const ready = exclude => {
    const chain = chainLengths(state.deps, state.done)
    return Object.keys(state.deps)
      .filter(n => !state.done.has(n) && !exclude.includes(n) && !verifyCycles.some(v => v.task === n && !state.relinked.has(n)) && state.deps[n].every(d => state.done.has(d) || !(d in state.deps)))  // dangling ids are dropped, as `ck plan` does
      // Longest remaining chain first, then numeric id: what `ck plan` prints.
      .sort((a, b) => (chain[b] ?? 1) - (chain[a] ?? 1) || Number(a) - Number(b))
  }
  const MODEL_RANK = { haiku: 0, sonnet: 1, opus: 2, fable: 3 }
  const guardStopFor = (n, opts) => {
    const ceiling = guardStops[n]
    const model = String(opts.model ?? '').toLowerCase()
    const rank = Object.keys(MODEL_RANK).find(m => model.includes(m))
    const within = ceiling === 'always' || (rank !== undefined && MODEL_RANK[rank] <= MODEL_RANK[ceiling])
    return within ? { file: 'tests/test_guarded.py', kind: 'test', test: 'tests/test_guarded.py::test_x', requirement: '1.1' } : null
  }
  // Run one `ck` command through execCk (or, in dry mode, only record it). Returns the parsed --json output, if any.
  // Calls run one at a time in call order, so agents resume in a fixed order however fast each subprocess is
  // (otherwise two identical runs stamp concurrent tasks in different orders).
  let ckChain = Promise.resolve()
  const runCk = (argv, input = null) => {
    const done = ckChain.then(() => execCkRun(execCk, argv, { log: state.execLog, results: state.execResults, external, input }))
    ckChain = done.catch(() => {})
    return done
  }
  const recordClaim = async prompt => {
    const c = findClaim(prompt)
    if (!c || !c.flags['progress-run'] || !c.flags['progress-event']) return
    const f = c.flags
    await runCk(eventArgv(c.spec, f['progress-event'], f['progress-run'], ['--task', c.task, ...(f['progress-model'] ? ['--model', f['progress-model']] : []), ...(f.attempt ? ['--attempt', f.attempt] : [])]))
  }
  const runWritten = async prompt => { for (const argv of writtenCommands(prompt)) await runCk(argv) }
  // Record the landing and one merge / hand-landing per task, from this fake project's own result. Returns the eta.
  const recordLanding = async (prompt, tasks, out) => {
    const land = findLand(prompt)
    const key = land && land.flags['progress-run']
    if (!key) return undefined
    const of = st => out.filter(o => o.state === st).map(o => o.task)
    const list = (flag, v) => (v.length ? [flag, v.join(',')] : [])
    let last = await runCk(eventArgv(land.spec, 'landing', key, ['--json', '--tasks', tasks.join(','), ...list('--merged', of('MERGED')), ...list('--red', of('RED')), ...list('--conflict', of('CONFLICT')), ...list('--protected', of('PROTECTED'))]))
    for (const o of out) {
      const kind = o.state === 'MERGED' ? 'merge' : o.state === 'PROTECTED' ? 'hand-landing' : null
      if (kind) last = await runCk(eventArgv(land.spec, kind, key, ['--json', '--task', o.task]))
    }
    return last && typeof last === 'object' ? last.eta : null
  }
  const rawAgent = async (prompt, opts) => {
    const l = opts.label
    // Land prompts get no claim (their landing events are recorded from the result below), but a command they
    // write out is still run as written.
    if (execCk) { if (!l.startsWith('land ')) await recordClaim(prompt); await runWritten(prompt) }
    state.events.push(l)
    state.prompts[l] = prompt
    state.opts[l] = opts
    state.optsList.push({ label: l, opts })  // every call in order: `opts` keeps only the last call of a label
    if (l.startsWith('plan')) {
      state.plans++
      const k = Number((l.match(/^plan (\d+)/) || [])[1])
      if (planFail.includes(k)) {
        if (planDurations[k]) await sleep(planDurations[k])
        return { error: 'boom', done: [], ready: [], busy: [], remaining: [], cycles: [], verifyCycles: [], deps: {} }
      }
      if (relink && state.plans === relink.atPlan) Object.assign(state.deps, relink.deps)
      if (externalDone && k >= externalDone.atPlan) for (const n of externalDone.tasks) state.done.add(String(n))
      const exclude = ((prompt.match(/--exclude ([\d,]+)/) || [])[1] || '').split(',').filter(Boolean)
      const slotsMatch = prompt.match(/Take the first (\d+|all) task/)
      let r = ready(exclude)
      if (slotsMatch && slotsMatch[1] !== 'all') r = r.slice(0, Number(slotsMatch[1]))
      // The answer is a snapshot taken when the call starts; planDurations lets the run move on meanwhile.
      const startedAt = stamp()
      const answer = {
        done: [...state.done],
        ready: r.filter(n => !busy.includes(n)),
        busy: r.filter(n => busy.includes(n)).map(n => ({ task: n, detail: 'claimed: someone' })),
        remaining: Object.keys(state.deps).filter(n => !state.done.has(n)),
        deps: Object.fromEntries(Object.entries(state.deps).map(([n, ds]) => [n, [...ds]])),
        cycles,
        verifyCycles: state.plans === 1 && !verifyCyclesFromStart ? [] : verifyCycles.filter(v => !state.relinked.has(v.task)),
        risk: { ...(risk ?? {}) },
        ...(owns ? { owns } : {}), ...(overlaps ? { overlaps } : {}), ...(testVerifies ? { testVerifies } : {}), ...(provides ? { provides } : {}),
        startedAt,
      }
      // Canned answers for the deterministic-plan harness: consumed in invocation order, each entry
      // merged over the default answer. badReady is the shorthand for "every answer is inconsistent".
      const entry = planSeq && planCalls < planSeq.length ? planSeq[planCalls] : null
      planCalls++
      if (entry && entry.error) {
        if (planDurations[k]) await sleep(planDurations[k])
        return { error: entry.error, done: [], ready: [], busy: [], remaining: [], cycles: [], verifyCycles: [], deps: {}, risk: answer.risk, startedAt, finishedAt: stamp() }
      }
      if (entry) Object.assign(answer, entry)
      else if (badReady) answer.ready = [...badReady]
      if (planDurations[k]) await sleep(planDurations[k])
      return { ...answer, finishedAt: stamp() }
    }
    if (l.startsWith('task')) {
      const n = l.match(/task (\d+)/)[1]
      state.attempts[n] = (state.attempts[n] || 0) + 1
      state.running++
      state.maxRunning = Math.max(state.maxRunning, state.running)
      const startedAt = stamp()
      const wt = worktree[n]
      if (wt && state.attempts[n] === 1 && (wt.state === 'BUSY' || wt.state === 'ERROR')) {
        state.running--
        state.events.push(`${l} end`)
        return { task: n, status: 'blocked', gatePassed: false, summary: `worktree ${wt.state}`, commits: '', startedAt, finishedAt: stamp(), worktree: { state: wt.state, detail: wt.detail ?? '' }, blocker: wt.state === 'BUSY' ? `worktree busy: ${wt.detail ?? ''}` : `worktree create failed: ${wt.detail ?? ''}` }
      }
      // CREATED / EXISTS: a normal attempt below, reporting the worktree outcome.
      await sleep(durations[n] ?? 5)
      state.running--
      state.events.push(`${l} end`)
      const wtField = wt && state.attempts[n] === 1 ? { worktree: { state: wt.state, detail: wt.detail ?? '' } } : {}
      if (noCommitsFirst.includes(n) && state.attempts[n] === 1) {
        return { task: n, status: 'blocked', gatePassed: false, summary: 'answered the wave question', commits: '', blocker: 'user asked a question', startedAt, finishedAt: stamp(), ...wtField }
      }
      const isRevise = / revise /.test(l)
      if (isRevise) state.revises[n] = (state.revises[n] || 0) + 1
      if (n in guardStops && (guardStopIn[n] === 'revise') === isRevise && (!isRevise || state.revises[n] === 1)) {
        const gs = guardStopFor(n, opts)
        if (gs) return { task: n, status: 'blocked', gatePassed: false, summary: 'guard stop', commits: '', blocker: 'guard stop', startedAt, finishedAt: stamp(), guardStop: gs, ...wtField }
      }
      const tc = testConflicts[n]
      if (tc && !isRevise && state.attempts[n] <= (tc.attempts ?? Infinity)) {
        return { task: n, status: 'blocked', gatePassed: false, summary: 'a test can never pass', commits: 'abc123', blocker: 'test conflict', testConflict: tc.conflict, startedAt, finishedAt: stamp(), ...wtField }
      }
      const tcl = testConflictsList[n]
      if (tcl && !isRevise) {
        const list = Array.isArray(tcl) ? tcl : tcl.conflicts
        if (state.attempts[n] <= (Array.isArray(tcl) ? Infinity : tcl.attempts ?? Infinity)) {
          return { task: n, status: 'blocked', gatePassed: false, summary: 'tests can never pass', commits: 'abc123', blocker: 'test conflict', testConflicts: list, startedAt, finishedAt: stamp(), ...wtField }
        }
      }
      const pass = state.attempts[n] > (failUntil[n] || 0)
      // taskFailures[n] is one list for every attempt, or a list per attempt (attempt k gets the k-th; the last repeats).
      const served = taskFailures[n]
      const failures = served && Array.isArray(served[0]) ? served[Math.min(state.attempts[n], served.length) - 1] : served
      return { task: n, status: pass ? 'done' : 'in_progress', gatePassed: pass, summary: `attempt ${state.attempts[n]}`, commits: 'abc123', startedAt, finishedAt: stamp(), ...(!pass && failures ? { failures } : {}), ...wtField }
    }
    if (l.startsWith('land ')) {
      // `ck worktree land`: precheck (conflicts, pairwise deferral), one gate, bisect a red batch.
      const tasks = l.slice(5).split(' ')
      state.landings.push(tasks)
      state.merging++
      state.maxMerging = Math.max(state.maxMerging, state.merging)
      const startedAt = stamp()
      await sleep(Math.max(...tasks.map(n => mergeDurations[n] ?? 2)))
      state.merging--
      state.events.push(`${l} end`)
      const out = []
      const landing = []
      for (const n of tasks) {
        if (protectedTasks.includes(n)) { out.push({ task: n, state: 'PROTECTED', detail: 'a human must merge it', files: ['.claude/settings.json'] }); continue }
        if (conflictRounds[n] && (state.conflictsHit[n] || 0) < conflictRounds[n]) { state.conflictsHit[n] = (state.conflictsHit[n] || 0) + 1; out.push({ task: n, state: 'CONFLICT', files: ['specs/demo/tasks.md'] }); continue }
        if (conflicts.includes(n) && !state.resolved.has(n)) { out.push({ task: n, state: 'CONFLICT', files: ['specs/demo/tasks.md'] }); continue }
        const mate = pairConflicts.find(([a, b]) => b === n && landing.includes(a))
        if (mate) { out.push({ task: n, state: 'DEFERRED', detail: `conflicts with Task ${mate[0]} in this batch`, files: ['x.py'] }); continue }
        landing.push(n)
      }
      const red = landing.filter(n => (redLeft[n] || 0) > 0)
      const gates = landing.length ? [{ tasks: landing, green: !red.length }] : []
      if (red.length && landing.length > 1) gates.push({ tasks: landing.slice(0, 1), green: true })
      for (const n of landing) {
        if (red.includes(n)) { redLeft[n]--; out.push({ task: n, state: 'RED', checkGreen: false, checkTail: `red: task ${n}` }) }
        else { state.done.add(n); state.merged.add(n); out.push({ task: n, state: 'MERGED', checkGreen: true, mergedAt: stamp() }) }
      }
      const answer = { tasks: out, gates, bisected: gates.length > 1, startedAt, finishedAt: stamp() }
      if (execCk) {
        const eta = await recordLanding(prompt, tasks, out)
        if (etaAnswer === 'null') answer.eta = null
        else if (etaAnswer === 'number') answer.eta = 42
        else if (etaAnswer !== 'missing' && eta !== undefined) answer.eta = eta
      }
      return answer
    }
    if (l.startsWith('resolve')) {
      const n = l.match(/resolve (\d+)/)[1]
      state.resolved.add(n)
      return { resolved: true, conflicts: 'specs/demo/tasks.md: kept both task sections' }
    }
    if (l.startsWith('relink ')) {
      const n = l.match(/relink (\d+)/)[1]
      const out = { fixable: false, applied: false, reason: 'after the relink nothing would verify the task', ...(relinkOutcome ?? {}) }
      state.relinks.push(n)
      if (out.fixable && out.applied) state.relinked.add(n)
      state.events.push(`${l} end`)
      return out
    }
    if (l.startsWith('park ')) {
      const n = l.match(/park (\d+)/)[1]
      state.parks.push(n)
      state.events.push(`${l} end`)
      return { parked: true, notification: 'sent', detail: '', ...(parkOutcome ?? {}) }
    }
    if (l.startsWith('arbiter task ')) {
      const n = l.match(/^arbiter task (\d+)/)[1]
      const list = rulings[n]
      if (!list || !list.length) throw new Error(`no ruling scripted for "${l}" (rulings: {${n}: [...]})`)
      const k = state.rulingCalls[n] = (state.rulingCalls[n] || 0) + 1
      state.events.push(`${l} end`)
      const answer = list[Math.min(k, list.length) - 1]
      if (answer === 'throw') throw new Error('arbiter agent failed')
      const edit = arbiterEdits[n]
      if (edit && answer !== null) await arbiterWrite(prompt, edit.files || {}, state, n)
      return answer
    }
    if (l.startsWith('audit task ')) {
      const n = l.match(/^audit task (\d+)/)[1]
      state.auditCalls.push(n)
      state.events.push(`${l} end`)
      const a = auditFindings[n]
      if (a === 'throw') throw new Error('auditor agent failed')
      return a ?? { problems: [], summary: 'no problems' }
    }
    if (l.startsWith('arbiter-')) {
      if (!execCk) throw new Error(`"${l}" is a thin step of the real ck CLI: pass execCk (a scenario cannot skip the real CLI)`)
      const argv = arbiterArgv(prompt)
      if (!argv) throw new Error(`"${l}": no \`ck ... arbiter ...\` line in its prompt`)
      const input = arbiterStdin(prompt)
      const before = state.execResults.length
      const json = await runCk(argv, input)
      const res = state.execResults[before] ?? { code: 0, stdout: '', stderr: '' }
      state.arbiterSteps.push({ label: l, argv, code: res.code })
      const step = l.slice('arbiter-'.length)
      if (arbiterStepFailures[step] || arbiterStepFailures[l]) return { exitCode: 1, json: { error: `${step} failed (injected)` }, error: `${step} failed (injected)`, stdout: '' }
      return { exitCode: res.code, json: json && typeof json === 'object' ? json : null, ...(res.code !== 0 ? { error: res.stderr || `ck exited ${res.code}` } : {}), stdout: res.stdout, stderr: res.stderr }
    }
    if (l.startsWith('review')) {
      const covered = reviewTasksOf(l, prompt)
      const n = covered[0]
      const key = n ?? l
      state.reviewTasks[l] = covered
      // A batched review IS each covered task's review (single-wave contract):
      // advance every covered task's counter while the verdict below is still
      // looked up by the first covered task's entry, so a flagged task's
      // single re-review reaches its second entry. Single-covered (individual)
      // labels behave exactly as before.
      for (const t of new Set(covered.length ? covered : [key])) state.reviewCount[t] = (state.reviewCount[t] || 0) + 1
      const startedAt = stamp()
      await sleep(n != null ? reviewDurations[n] ?? 0 : 0)
      state.events.push(`${l} end`)
      // A reviews entry is a verdict string (as before) or { verdict, findings } for a
      // batched verdict flagging individual tasks (design.md per-task findings shape
      // { task, severity, file, issue, fix }); object entries pass findings through verbatim.
      const spec = (n != null ? (reviews[n] || [])[state.reviewCount[key] - 1] : null) || 'approve'
      const verdict = typeof spec === 'string' ? spec : (spec.verdict || 'approve')
      const findings = typeof spec === 'string'
        ? (verdict === 'changes' ? [{ severity: 'blocking', issue: reviewIssue || 'contradicts design.md' }] : [])
        : spec.findings || (spec.finding === 'serves-no-requirement' ? [{ severity: 'blocking', file: 'specs/demo/tasks.md', issue: 'serves no requirement', noRequirement: true }] : [])
      return { verdict, findings, summary: typeof spec === 'string' ? verdict : (spec.summary || verdict), startedAt, finishedAt: stamp() }
    }
    if (l.startsWith('cross-test') && crossTest) {
      state.crossCalls = (state.crossCalls || 0) + 1
      return Array.isArray(crossTest) ? crossTest[Math.min(state.crossCalls, crossTest.length) - 1] : crossTest
    }
    if (l.startsWith('cross-test')) return { verdict: 'changes', findings: [{ severity: 'blocking', file: 'tests/test_b.py', issue: 'reads "state" where tests/test_a.py and design.md say "status"' }], summary: 'one drift' }
    if (l === 'report') {
      // `ck worktree report`: merge commits on the target plus tasks.md there, never agent claims.
      const out = { head: 'f00d', merged: [], done: [], failed: [], blocked: [], unstarted: [] }
      for (const n of Object.keys(state.deps)) {
        if (state.merged.has(n)) out.merged.push({ task: n, commit: `m${n}`, mergedAt: '2026-10-04T11:00:00Z' })
        else if (state.done.has(n)) out.done.push({ task: n })
        else if (state.attempts[n]) out.failed.push({ task: n, branch: `feat/demo-task-${n}`, ahead: 1 })
        else {
          const on = state.deps[n].filter(d => !state.done.has(d))
          if (on.length) out.blocked.push({ task: n, on })
          else out.unstarted.push({ task: n })
        }
      }
      return gitReport ? gitReport(out, state) : out
    }
    if (l.startsWith('fix')) return { green: true }
    if (l.startsWith('re-check')) return { green: rechecksLeftRed-- <= 0 }
    if (l.startsWith('full gate')) return { green: !fullRed }
    if (l === 'final gate') return { green: true }
    if (l === 'run start') return runStart ?? { integration: { branch: 'integrate/demo', state: 'ok' }, hooks: { installed: true, active: true, intact: true, chainedTo: null, problems: [] }, baseline: { commit: 'abc1234', files: 0 }, releasedClaims: [], keptClaims: [], warnings: [] }
    if (l === 'audit') {
      if (audit === 'throw') throw new Error('audit failed')
      return audit ?? { schema: 'ck-audit/1', spec: 'demo', into: 'integrate/demo', head: 'abc1234', complete: true, clean: true, drift: [], hooks: { installed: true, active: true, intact: true, chainedTo: null, problems: [] }, fixtures: { drift: [], newLocks: [], lockTampered: false, handcrafted: [] }, problems: [] }
    }
    if (l.startsWith('testfix ledger ')) {
      if (ledgerFail) return THIN_LABEL.test(l) && isCkCall(opts) ? { error: 'cannot read the test-fix ledger', __exitCode: 1 } : { exitCode: 1, json: { error: 'cannot read the test-fix ledger' }, error: 'cannot read the test-fix ledger' }
      // `ck testfix check SPEC --test NODE ...`: a node is already fixed when its file part is in priorFixes.
      const nodes = [...String(prompt).matchAll(/--test[ =](\S+)/g)].map(m => m[1].replace(/^["']|["']$/g, ''))
      const fixed = n => Object.keys(priorFixes).find(f => priorFixes[f] && n.split('::')[0] === f)
      const alreadyFixed = nodes.filter(fixed).map(node => ({ node, at: 'abc1234', fix: fixed(node) }))
      return { allowed: nodes.filter(n => !fixed(n)), alreadyFixed }
    }
    if (l.startsWith('testfix prove ')) {
      const file = fileOfArgs(prompt, testFix) ?? Object.keys(testFix)[0]
      const t = (file && testFix[file]) || {}
      // design.md Data Models: ScopeDecision and ProofVerdict; `against` is "blocked:<task>" or "integration" (4.11).
      const blocked = String(prompt).match(/--blocked[ =](\S+)/)
      const against = blocked ? `blocked:${blocked[1].replace(/^["']|["']$/g, '')}` : 'integration'
      const nodes = [...String(prompt).matchAll(/--test[ =](\S+)/g)].map(m => m[1].replace(/^["']|["']$/g, ''))
      const outcomes = (nodes.length ? nodes : file ? [file] : []).map(node => ({ node, passed: true, cls: null }))
      const scope = { in_scope: true, outside: [], requirements_edited: false, spec_docs_edited: [], reason: 'in scope', ...(t.scope || {}) }
      const proof = scope.in_scope === false ? null : { proved: true, against, outcomes, reason: 'the test fails on the blocked branch or integration and passes on the fix', ...(t.proof || {}) }
      return { scope, proof, changedTests: nodes, against: proof ? proof.against : against }
    }
    if (l.startsWith('testfix review ')) {
      const file = testFileOf(l)
      return (file && testFix[file] && testFix[file].review) || { verdict: 'approve', findings: [], summary: 'approve' }
    }
    if (l.startsWith('testfix ')) {
      const file = testFileOf(l)
      const t = (file && testFix[file]) || {}
      return { verdict: t.verdict ?? 'fixed', changedTests: [{ file: file ?? 'tests/test_x.py', test: 'test_x' }], commits: 'fix123', finding: t.finding ?? null, summary: `testfix ${file ?? ''}` }
    }
    throw new Error('unexpected agent ' + l)
  }
  // The k-th file of testFix for labels `testfix <k> (Task n)` and `testfix review <k>`.
  const testFileOf = label => { const m = label.match(/^testfix (?:review )?(\d+)/); return m ? Object.keys(testFix)[Number(m[1]) - 1] ?? null : null }
  const fileOfArgs = (text, table) => Object.keys(table).find(f => String(text).includes(f)) ?? null
  // gateAnswers, consumed in order. Returns undefined when exhausted.
  let gateIx = 0
  state.gateCalls = []
  const nextGate = () => { const e = gateIx < gateAnswers.length ? gateAnswers[gateIx] : undefined; gateIx++; return e }
  // Today's agent, plus the CK_CALL envelope for thin labels (and the new thin labels' answers).
  const agent = (prompt, opts) => {
    const l = opts.label
    // Calls that need no envelope go straight to today's agent, adding no microtask hop (the real-timer suites
    // order their agents by timing).
    if (l.startsWith('arbiter-')) return rawAgent(prompt, opts)  // always an envelope, and always the real CLI
    if (!(THIN_LABEL.test(l) && isCkCall(opts))) return rawAgent(prompt, opts)
    return thinAgent(prompt, opts)
  }
  const thinAgent = async (prompt, opts) => {
    const l = opts.label
    if (GATE_LABEL.test(l)) {
      let today
      if (l !== 'gate') today = await rawAgent(prompt, opts)
      else { state.events.push(l); state.prompts[l] = prompt; state.opts[l] = opts; today = { green: true } }
      const e = nextGate()
      state.gateCalls.push({ label: l, answer: e === undefined ? 'default' : e })
      if (e === 'throw') throw new Error('gate agent failed')
      if (e === 'hang') return never()
      if (e === undefined) return { exitCode: today.green ? 0 : 1, json: gateJson(today.green) }
      if (isRawGate(e)) return { exitCode: e.code, json: parseJson(e.stdout), stdout: e.stdout }
      return { exitCode: e.ok === false ? 1 : 0, json: e }
    }
    const out = await rawAgent(prompt, opts)
    if (out && typeof out === 'object' && '__exitCode' in out) { const { __exitCode, ...json } = out; return { exitCode: __exitCode, json } }
    // audit: exit 0 clean, 3 incomplete, 1 drift or hook problems (design.md ck-audit/1).
    if (l === 'audit' && out && typeof out === 'object') return { exitCode: out.complete === false ? 3 : out.clean === false ? 1 : 0, json: out }
    return { exitCode: 0, json: out }
  }
  const thinCall = async (label, argv) => {
    const out = await agent(`ck ${argv.join(' ')}`, { label, schema: { type: 'object', properties: { exitCode: {}, json: {} } } })
    return { code: out.exitCode, stdout: out.stdout ?? JSON.stringify(out.json), stderr: '', json: out.json }
  }
  return { agent, state, thinCall, nextGate, extras: { fidelity, fixtureStatus, missingCommand } }
}

// The OpenCode runner's `ck` global over a fake project: plan, land and relink commands are answered
// by the project's own plan/land/relink handlers (the JSON a real `ck ... --json` would print), and
// recorded in `calls` (argv lists). Anything else answers code 2 with no JSON.
export function directCk(p) {
  const calls = []
  let plans = 0
  let ledgers = 0
  let proves = 0
  const fn = async argv => {
    calls.push(argv)
    const rest = argv[0] === '-C' ? argv.slice(2) : argv
    let label = null
    if (rest[0] === 'plan') label = `plan ${++plans}`
    else if (rest[0] === 'worktree' && rest[1] === 'land') label = `land ${rest.slice(3, rest.indexOf('--into')).join(' ')}`
    else if (rest[0] === 'relink') label = `relink ${rest[2]}`
    if (label) {
      const json = await p.agent(`ck ${argv.join(' ')}`, { label })
      return { code: 0, stdout: JSON.stringify(json), stderr: '', json }
    }
    // The unattended-correctness commands, answered in the CK_CALL envelope.
    const ok = (json, code = 0) => ({ code, stdout: JSON.stringify(json), stderr: '', json })
    if (rest[0] === 'gate') return p.thinCall('gate', rest)
    if (rest[0] === 'worktree' && rest[1] === 'start') return p.thinCall('run start', rest)
    if (rest[0] === 'worktree' && rest[1] === 'audit') return p.thinCall('audit', rest)
    if (rest[0] === 'testfix' && rest[1] === 'check') return p.thinCall(`testfix ledger ${++ledgers}`, rest)
    if (rest[0] === 'testfix' && rest[1] === 'prove') return p.thinCall(`testfix prove ${++proves}`, rest)
    if (rest[0] === 'testfix' && rest[1] === 'record') return ok({ recorded: true })
    if (rest[0] === 'fixtures' && rest[1] === 'status') return ok(p.extras.fixtureStatus ?? { locked: [], drift: [], unlabeled: [], configError: null })
    if (rest[0] === 'lint' && rest.includes('--fidelity')) return ok(p.extras.fidelity ?? { checked: [], mismatches: [], unparsed: [], unmatched: [] })
    if (rest[0] === 'graph') return ok({ spec: rest[1] ?? null, nodes: [], edges: [], problems: [], ...(p.extras.missingCommand ? { missingCommand: p.extras.missingCommand } : {}) })
    return { code: 2, stdout: '', stderr: `unknown ck command: ${argv.join(' ')}`, json: null }
  }
  return { fn, calls }
}
