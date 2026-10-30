// A fake project for the spec-implement workflow tests: `ck plan` semantics over deps/done, task
// durations, failures, timestamps. Shared by spec_implement.test.mjs and the bridge.
import { execFile } from 'node:child_process'
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
export async function execCkRun(execCk, argv, { log, results, external = p => p }) {
  log.push(argv)
  if (!execCk || execCk.dry) return null
  const env = { ...process.env, ...(execCk.env || {}), CK_PROGRESS_TEST_KINDS: '1' }
  const [bin, ...pre] = execCk.cmd
  const res = await external(new Promise(resolve => {
    execFile(bin, [...pre, ...argv], { env, cwd: execCk.cwd || process.cwd(), encoding: 'utf8' }, (err, out, errOut) => {
      resolve({ argv, code: err ? (typeof err.code === 'number' ? err.code : -1) : 0, error: err ? String(err.message) : null, stdout: out || '', stderr: errOut || '' })
    })
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

// Options beyond the scenario knobs:
//   planDurations: {k: ms}                 plan call k takes that long
//   externalDone: {atPlan: k, tasks}       from plan call k on, those tasks are Done on the target (never started here)
//   planFail: [k, ...]                     plan call k and its retry answer {error: 'boom'} with empty arrays
//   guardStops: {n: ceiling|'always'}      task n returns guardStop on every attempt whose model is at or below ceiling
//                                          (haiku < sonnet < opus < fable); 'always' never resolves
//   guardStopIn: {n: 'revise'}             the guard stop of task n happens in its first revise call, not its task call
//   parkOutcome: {parked, notification, detail}  answer of the `park <N>` agent (default parked, sent, '')
//   worktree: {n: {state, detail}}         the first task agent of n returns that worktree outcome (default CREATED)
export function project({ deps, done = [], durations = {}, failUntil = {}, noCommitsFirst = [], busy = [], cycles = [], verifyCycles = [], verifyCyclesFromStart = false, checkRed = [], fullRed = false, relink = null, reviews = {}, mergeDurations = {}, reviewDurations = {}, recheckRed = 0, conflicts = [], redLands = {}, pairConflicts = [], reviewIssue = null, protectedTasks = [], gitReport = null, conflictRounds = {}, planDurations = {}, externalDone = null, planFail = [], worktree = {}, guardStops = {}, guardStopIn = {}, parkOutcome = null, execCk = null, etaAnswer = null, sleep = realSleep, external = p => p }) {
  let rechecksLeftRed = recheckRed
  // RED landings left per task: checkRed tasks are red on their first landing, redLands sets a count.
  const redLeft = { ...Object.fromEntries(checkRed.map(n => [n, 1])), ...redLands }
  const state = { execResults: [], deps: { ...deps }, done: new Set(done), merged: new Set(), events: [], prompts: {}, opts: {}, reviewCount: {}, attempts: {}, plans: 0, merging: 0, maxMerging: 0, running: 0, maxRunning: 0, landings: [], parks: [], revises: {}, resolved: new Set(), conflictsHit: {}, execLog: [] }
  let clock = Date.parse('2026-10-04T10:00:00Z')
  const stamp = () => new Date((clock += 60000)).toISOString().replace('.000', '')
  const ready = exclude => {
    const chain = chainLengths(state.deps, state.done)
    return Object.keys(state.deps)
      .filter(n => !state.done.has(n) && !exclude.includes(n) && !verifyCycles.some(v => v.task === n) && state.deps[n].every(d => state.done.has(d) || !(d in state.deps)))  // dangling ids are dropped, as `ck plan` does
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
  const runCk = argv => execCkRun(execCk, argv, { log: state.execLog, results: state.execResults, external })
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
  const agent = async (prompt, opts) => {
    const l = opts.label
    // Land prompts get no claim (their landing events are recorded from the result below), but a command they
    // write out is still run as written.
    if (execCk) { if (!l.startsWith('land ')) await recordClaim(prompt); await runWritten(prompt) }
    state.events.push(l)
    state.prompts[l] = prompt
    state.opts[l] = opts
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
      const answer = {
        done: [...state.done],
        ready: r.filter(n => !busy.includes(n)),
        busy: r.filter(n => busy.includes(n)).map(n => ({ task: n, detail: 'claimed: someone' })),
        remaining: Object.keys(state.deps).filter(n => !state.done.has(n)),
        deps: Object.fromEntries(Object.entries(state.deps).map(([n, ds]) => [n, [...ds]])),
        cycles,
        verifyCycles: state.plans === 1 && !verifyCyclesFromStart ? [] : verifyCycles,
      }
      if (planDurations[k]) await sleep(planDurations[k])
      return answer
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
      const pass = state.attempts[n] > (failUntil[n] || 0)
      return { task: n, status: pass ? 'done' : 'in_progress', gatePassed: pass, summary: `attempt ${state.attempts[n]}`, commits: 'abc123', startedAt, finishedAt: stamp(), ...wtField }
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
    if (l.startsWith('park ')) {
      const n = l.match(/park (\d+)/)[1]
      state.parks.push(n)
      state.events.push(`${l} end`)
      return { parked: true, notification: 'sent', detail: '', ...(parkOutcome ?? {}) }
    }
    if (l.startsWith('review')) {
      const n = l.match(/review (\d+)/)[1]
      state.reviewCount[n] = (state.reviewCount[n] || 0) + 1
      await sleep(reviewDurations[n] ?? 0)
      state.events.push(`${l} end`)
      const verdict = (reviews[n] || [])[state.reviewCount[n] - 1] || 'approve'
      return { verdict, findings: verdict === 'changes' ? [{ severity: 'blocking', issue: reviewIssue || 'contradicts design.md' }] : [], summary: verdict }
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
    throw new Error('unexpected agent ' + l)
  }
  return { agent, state }
}
