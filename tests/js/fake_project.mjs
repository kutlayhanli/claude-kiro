// A fake project for the spec-implement workflow tests: `ck plan` semantics over deps/done, task
// durations, failures, timestamps. Shared by spec_implement.test.mjs and the bridge.
import { sleep } from './load_workflow.mjs'

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
export function project({ deps, done = [], durations = {}, failUntil = {}, noCommitsFirst = [], busy = [], cycles = [], verifyCycles = [], verifyCyclesFromStart = false, checkRed = [], fullRed = false, relink = null, reviews = {}, mergeDurations = {}, reviewDurations = {}, recheckRed = 0, conflicts = [], redLands = {}, pairConflicts = [], reviewIssue = null, protectedTasks = [], gitReport = null, conflictRounds = {}, planDurations = {}, externalDone = null, planFail = [], worktree = {}, guardStops = {}, guardStopIn = {}, parkOutcome = null }) {
  let rechecksLeftRed = recheckRed
  // RED landings left per task: checkRed tasks are red on their first landing, redLands sets a count.
  const redLeft = { ...Object.fromEntries(checkRed.map(n => [n, 1])), ...redLands }
  const state = { deps: { ...deps }, done: new Set(done), merged: new Set(), events: [], prompts: {}, opts: {}, reviewCount: {}, attempts: {}, plans: 0, merging: 0, maxMerging: 0, running: 0, maxRunning: 0, landings: [], parks: [], revises: {}, resolved: new Set(), conflictsHit: {} }
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
  const agent = async (prompt, opts) => {
    const l = opts.label
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
      return { tasks: out, gates, bisected: gates.length > 1, startedAt, finishedAt: stamp() }
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
