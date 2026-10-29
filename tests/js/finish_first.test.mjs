// Finish-first scheduling (spec landing-speed, Story 3, script side): TC-23 to TC-29, TC-63, TC-65.
// Runs the real script under the fake runtime (load_workflow.mjs) with the fake project (fake_project.mjs).
// Every slot-pressure case passes args.agentSlots equal to the fake runtime's `slots`, so the script's
// queued estimate matches the fake queue. Assertions judge by the fake runtime's own `trace` and use
// only event order on it (never a wall-clock tolerance).
// Run: node tests/js/finish_first.test.mjs
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow, sleep } from './load_workflow.mjs'
import { project } from './fake_project.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../plugin/workflows/spec-implement.js')

const baseArgs = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.ck/worktrees/demo-integration' }
const REVIEW_ON = { reviewer: { model: 'opus', effort: 'high', enabled: true, rounds: 1 } }

async function test(name, fn) {
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}

// A run that never settles must fail this suite, not hang it.
const RUN_LIMIT_MS = 20000
async function run(p, args, slots) {
  let timer
  const limit = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`the run did not settle within ${RUN_LIMIT_MS} ms`)), RUN_LIMIT_MS) })
  try {
    return await Promise.race([runWorkflow(SCRIPT, { agent: p.agent, args, slots }), limit])
  } finally { clearTimeout(timer) }
}

// The args the skill passes: deps and remaining from `ck plan`, as strings.
const graphArgs = deps => ({
  deps: Object.fromEntries(Object.entries(deps).map(([n, ds]) => [String(n), ds.map(String)])),
  remaining: Object.keys(deps).map(String),
})

// --- trace helpers (event order only) -----------------------------------------

const isFirstAttempt = l => /^task \d+$/.test(l)
const finishKind = l => (/^review /.test(l) ? 'review' : /^task \d+ revise /.test(l) ? 'revise' : /^land /.test(l) ? 'land' : null)
const idxOf = (trace, ev, label, from = 0) => trace.findIndex((e, i) => i >= from && e.ev === ev && e.label === label)
const nextEnd = (trace, from) => { const j = trace.findIndex((e, i) => i > from && e.ev === 'end'); return j < 0 ? trace.length : j }
const endsBetween = (trace, a, b) => trace.slice(a + 1, b).filter(e => e.ev === 'end').length

// State just before each trace index: agents requested and not ended, and finish-type labels requested and not started.
function states(trace) {
  const out = []
  let open = 0
  const unstarted = new Map()
  for (const e of trace) {
    out.push({ open, queuedFinish: [...unstarted].filter(([, c]) => c > 0).map(([l]) => l) })
    if (e.ev === 'request') {
      open++
      if (finishKind(e.label)) unstarted.set(e.label, (unstarted.get(e.label) || 0) + 1)
    } else if (e.ev === 'start') {
      if (finishKind(e.label)) unstarted.set(e.label, unstarted.get(e.label) - 1)
    } else if (e.ev === 'end') open--
  }
  out.push({ open, queuedFinish: [...unstarted].filter(([, c]) => c > 0).map(([l]) => l) })
  return out
}

const firstAttemptRequests = trace => trace.map((e, i) => [e, i]).filter(([e]) => e.ev === 'request' && isFirstAttempt(e.label)).map(([, i]) => i)
const finishFirstLines = logs => logs.filter(l => String(l).includes('finish-first'))

// --- TC-23 scenario -------------------------------------------------------------
// slots 2, maxConcurrent 2, mergeBatch 1, tasks 1-2 short (5, 6 ms), task 3 long (200 ms), reviews 1-2 5 ms,
// land 1 30 ms, the plan after landing 1 (plan 2) 60 ms. When land 1 ends while task 3 runs, the landing
// plan and land 2 are requested together, and land 2 waits in the runtime queue behind that plan.
const TC23_DEPS = { 1: [], 2: [], 3: [], 4: [], 5: [] }
const tc23Project = (extra = {}) => project({ deps: TC23_DEPS, durations: { 1: 5, 2: 6, 3: 200, 4: 5, 5: 5 }, reviewDurations: { 1: 5, 2: 5 }, mergeDurations: { 1: 30 }, planDurations: { 2: 60 }, ...extra })
const tc23Args = (extra = {}) => ({ ...baseArgs, ...graphArgs(TC23_DEPS), maxConcurrent: 2, mergeBatch: 1, agents: REVIEW_ON, agentSlots: 2, finishFirst: true, ...extra })

// 3.1: no first-attempt task request while any review/revise/land is requested and not started.
function assertNoTaskRequestWhileFinishQueued(trace) {
  const st = states(trace)
  for (const i of firstAttemptRequests(trace)) {
    assert.deepEqual(st[i].queuedFinish, [], `${trace[i].label} was requested at trace[${i}] while ${st[i].queuedFinish.join(', ')} waited for a runtime slot`)
  }
}

// TC-23 non-vacuity: land 2 really queued (another end between its request and start), task 4 waited for it.
function assertLand2Queued(trace) {
  const req = idxOf(trace, 'request', 'land 2')
  const start = idxOf(trace, 'start', 'land 2')
  assert.ok(req >= 0 && start > req, 'land 2 was requested and started')
  assert.ok(endsBetween(trace, req, start) >= 1, 'land 2 waited in the runtime queue: another agent ended between its request and its start')
  const task4 = idxOf(trace, 'request', 'task 4')
  assert.ok(task4 > start, `task 4 was requested only after land 2 started (request task 4 at ${task4}, start land 2 at ${start})`)
}

await test('TC-23: no new task is requested while a review/revise/land waits for a runtime slot', async () => {
  const p = tc23Project()
  const { result, trace } = await run(p, tc23Args(), 2)
  assert.equal(result.halted, false)
  assert.deepEqual([...result.merged].sort(), ['1', '2', '3', '4', '5'])
  assertNoTaskRequestWhileFinishQueued(trace)
  assertLand2Queued(trace)
})

await test('TC-24: once no finish-type agent is queued and a runtime slot is free, a ready task is requested before the next end', async () => {
  const p = tc23Project()
  const { trace } = await run(p, tc23Args(), 2)
  const slots = 2
  const startLand2 = idxOf(trace, 'start', 'land 2')
  assert.ok(startLand2 >= 0, 'land 2 started')
  const st = states(trace)
  let checked = false
  for (let e = startLand2 + 1; e < trace.length && !checked; e++) {
    if (trace[e].ev !== 'end') continue
    const next = nextEnd(trace, e)
    // Agents requested and not ended once e has ended, plus the non-task requests the script makes before the next end.
    const nonTask = trace.slice(e + 1, next).filter(x => x.ev === 'request' && !isFirstAttempt(x.label)).length
    if (st[e + 1].open + nonTask >= slots) continue
    checked = true
    const taskReq = trace.slice(e + 1, next).find(x => x.ev === 'request' && isFirstAttempt(x.label))
    assert.ok(taskReq, `a runtime slot was free after "${trace[e].label}" ended (trace[${e}]), but no first-attempt task was requested before the next end`)
  }
  assert.ok(checked, 'a runtime slot freed after land 2 started (non-vacuous)')
})

await test('TC-65: at saturation a finished implementer\'s review takes the next slot; no task start queues ahead of it', async () => {
  const deps = { 1: [], 2: [], 3: [], 4: [] }
  const mk = () => project({ deps, durations: { 1: 5, 2: 80, 3: 5, 4: 5 }, reviewDurations: { 1: 5 } })
  const args = { ...baseArgs, ...graphArgs(deps), maxConcurrent: 3, agents: REVIEW_ON, agentSlots: 2, finishFirst: true }
  const slots = 2
  const p = mk()
  const { result, trace } = await run(p, args, slots)

  // Only 2 task requests at start, although maxConcurrent allows 3.
  const firstTask = idxOf(trace, 'request', 'task 1')
  assert.ok(firstTask >= 0, 'task 1 was requested')
  const firstEndAfter = nextEnd(trace, firstTask)
  const atStart = trace.slice(0, firstEndAfter).filter(e => e.ev === 'request' && isFirstAttempt(e.label)).map(e => e.label)
  assert.deepEqual(atStart, ['task 1', 'task 2'], 'only 2 tasks requested at start (no runtime slot for a third)')

  // Task 1 ends with all slots busy: its review comes first and takes the freed slot.
  const endTask1 = idxOf(trace, 'end', 'task 1')
  const reqReview1 = idxOf(trace, 'request', 'review 1', endTask1)
  assert.ok(reqReview1 > endTask1, 'review 1 was requested after task 1 ended')
  const laterTask = firstAttemptRequests(trace).find(i => i > endTask1)
  assert.ok(laterTask === undefined || reqReview1 < laterTask, 'request review 1 precedes any further first-attempt task request')
  const startReview1 = idxOf(trace, 'start', 'review 1', endTask1)
  assert.ok(startReview1 > endTask1, 'review 1 started after task 1 ended')
  const startsBetween = trace.slice(endTask1 + 1, startReview1).filter(e => e.ev === 'start').map(e => e.label)
  assert.deepEqual(startsBetween, [], 'start review 1 follows end task 1 with no other start between')

  // At every first-attempt task request a runtime slot is free, and the task starts at once.
  const st = states(trace)
  for (const i of firstAttemptRequests(trace)) {
    assert.ok(st[i].open < slots, `${trace[i].label} requested while ${st[i].open} agents were requested and not ended (slots ${slots})`)
    const start = idxOf(trace, 'start', trace[i].label, i)
    assert.ok(start > i && endsBetween(trace, i, start) === 0, `${trace[i].label} started right after its request (no end between)`)
  }
  assert.equal(result.halted, false)
  assert.deepEqual([...result.merged].sort(), ['1', '2', '3', '4'])

  // Non-vacuous: with finish-first off, task 3 is requested before any agent of the run ends after the tasks start.
  const q = mk()
  const off = await run(q, { ...args, finishFirst: false }, slots)
  const t1 = idxOf(off.trace, 'request', 'task 1')
  const t3 = idxOf(off.trace, 'request', 'task 3')
  assert.ok(t3 >= 0 && t3 < nextEnd(off.trace, t1), 'with finishFirst: false, task 3 is requested before any end')
})

await test('TC-25: with nothing queued, finish-first on and off give the same request/start/end sequence and result', async () => {
  // TC-5 graph: 2 chains of 4 plus 4 independent tasks.
  const deps = { 1: [], 2: ['1'], 3: ['2'], 4: ['3'], 5: [], 6: ['5'], 7: ['6'], 8: ['7'], 9: [], 10: [], 11: [], 12: [] }
  const args = { ...baseArgs, ...graphArgs(deps), maxConcurrent: 3, mergeBatch: 2, agentSlots: 1000 }
  // Every timed fake agent sleeps the same 10 ms: Node fires timers of one duration in insertion order, so
  // the event order is the same on a loaded machine (other agents end without a timer).
  const same = Object.fromEntries(Object.keys(deps).map(n => [n, 10]))
  const mk = () => project({ deps, durations: same, mergeDurations: same })
  const on = await run(mk(), { ...args, finishFirst: true })
  const off = await run(mk(), { ...args, finishFirst: false })
  const seq = t => t.filter(e => ['request', 'start', 'end'].includes(e.ev)).map(e => `${e.ev} ${e.label}`)
  assert.deepEqual(seq(on.trace), seq(off.trace))
  assert.deepEqual(on.result, off.result)
  assert.deepEqual([...on.result.merged].sort((a, b) => a - b), Object.keys(deps))
})

// --- TC-26: finish-type and landing agents are never held back -----------------

// Labels whose end can trigger the request of `label`; the trigger is the latest such end before the request.
const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
function triggerOf(label) {
  let m
  if ((m = label.match(/^review (\d+)/))) {
    const n = esc(m[1])
    return l => new RegExp(`^task ${n}( |$)`).test(l)  // its implementer's (task, retry or revise) end
  }
  if ((m = label.match(/^task (\d+) revise /))) {
    const n = esc(m[1])
    return l => new RegExp(`^review ${n}( |$)`).test(l)
  }
  if ((m = label.match(/^land (.+)$/))) {
    const tasks = m[1].split(' ')
    const own = l => tasks.some(n => new RegExp(`^(task|review|resolve) ${esc(n)}( |$)`).test(l) || l.includes(`(Task ${n} red on landing)`))
    const step = l => /^land /.test(l) || /^full gate @/.test(l) || /^fix \d+ \(full gate /.test(l) || /^re-check /.test(l)
    return l => own(l) || step(l)  // approval (or fix/resolve) of a task in the batch, or the in-flight landing step's last agent
  }
  if (/^full gate @/.test(label)) return l => /^land /.test(l)
  if ((m = label.match(/^fix \d+ \(Task (\d+) red on landing\)/))) {
    const n = m[1]
    // The red land containing n (or the full gate, and its fix/re-check, that its landing step ran), or the previous fix.
    return l => (/^land /.test(l) && l.slice(5).split(' ').includes(n)) || l.includes(`(Task ${n} red on landing)`) || /^full gate @/.test(l) || /^fix \d+ \(full gate /.test(l) || /^re-check /.test(l)
  }
  if (/^fix \d+ \(full gate /.test(label)) return l => /^full gate @/.test(l) || /^re-check /.test(l)
  if ((m = label.match(/^re-check (\d+)$/))) {
    const k = esc(m[1])
    return l => new RegExp(`^fix ${k} `).test(l)
  }
  return null
}

// The fake project ends plan, full gate, fix, re-check and resolve agents without a timer. One of them that
// was queued would start and end in the same microtask run as the agent whose end freed its slot, before that
// agent's own follow-up request; a 1 ms sleep keeps every end in its own timer callback.
const timed = p => ({ ...p, agent: async (prompt, opts) => {
  if (/^(plan|full gate|fix|re-check|resolve)/.test(opts.label)) await sleep(1)
  return p.agent(prompt, opts)
} })

function assertNeverHeldBack(trace) {
  const seen = { review: 0, revise: 0, land: 0, full: 0, fix: 0, recheck: 0 }
  trace.forEach((e, i) => {
    if (e.ev !== 'request') return
    const trig = triggerOf(e.label)
    if (!trig) return
    let t = -1
    for (let j = i - 1; j >= 0; j--) if (trace[j].ev === 'end' && trig(trace[j].label)) { t = j; break }
    assert.ok(t >= 0, `no trigger found for "${e.label}" (trace[${i}])`)
    const between = trace.slice(t + 1, i).filter(x => x.ev === 'end').map(x => x.label)
    assert.deepEqual(between, [], `"${e.label}" was requested after its trigger "${trace[t].label}" ended, but only after ${between.join(', ')} also ended`)
    if (/^review /.test(e.label)) seen.review++
    else if (/ revise /.test(e.label)) seen.revise++
    else if (/^land /.test(e.label)) seen.land++
    else if (/^full gate/.test(e.label)) seen.full++
    else if (/^fix /.test(e.label)) seen.fix++
    else if (/^re-check/.test(e.label)) seen.recheck++
  })
  return seen
}

await test('TC-26: reviews, revisions, landings, full gates, fixes and re-checks are requested at their trigger, during a deferral', async () => {
  // TC-23's deferral plus a red landing of task 2, a full gate every 2 merges and one revision of task 1.
  const p = timed(tc23Project({ checkRed: ['2'], reviews: { 1: ['changes', 'approve'] } }))
  const { result, trace, logs } = await run(p, tc23Args({ fullGateEvery: 2 }), 2)
  assert.equal(result.halted, false)
  assert.deepEqual([...result.merged].sort(), ['1', '2', '3', '4', '5'])
  const seen = assertNeverHeldBack(trace)
  assert.ok(seen.review >= 5 && seen.revise >= 1 && seen.land >= 6 && seen.full >= 2 && seen.fix >= 1, `every kind was exercised: ${JSON.stringify(seen)}`)
  assert.ok(p.state.events.includes('fix 1 (Task 2 red on landing)'), 'task 2 was red on landing and got a fix')

  // A red full gate adds its fix and re-check.
  const q = timed(tc23Project({ fullRed: true }))
  const red = await run(q, tc23Args({ fullGateEvery: 2 }), 2)
  const seenRed = assertNeverHeldBack(red.trace)
  assert.ok(seenRed.full >= 1 && seenRed.fix >= 1 && seenRed.recheck >= 1, `full gate fix and re-check were exercised: ${JSON.stringify(seenRed)}`)

  // The deferral really was in progress (finish-first held task starts in these runs).
  assert.ok(finishFirstLines(logs).length >= 1, 'a finish-first deferral happened in the run')
  assert.ok(finishFirstLines(red.logs).length >= 1, 'a finish-first deferral happened in the red-full-gate run')
})

await test('TC-63: hand-built args (no agentSlots, finishFirst absent): nothing is presumed queued, start order as today', async () => {
  const deps = { 1: [], 2: [], 3: [] }
  const p = project({ deps, durations: { 1: 2, 2: 2, 3: 2 }, mergeDurations: { 1: 80 }, reviewDurations: { 2: 80 } })
  const { result, trace } = await run(p, { ...baseArgs, maxConcurrent: 1, agents: REVIEW_ON })
  assert.deepEqual([...result.merged].sort(), ['1', '2', '3'])
  const endLand1 = trace.findIndex(e => e.ev === 'end' && /^land /.test(e.label) && e.label.slice(5).split(' ').includes('1'))
  assert.ok(endLand1 >= 0, 'task 1 landed')
  assert.ok(idxOf(trace, 'request', 'task 2') < endLand1, 'task 2 was requested before task 1\'s land ended')
  assert.ok(idxOf(trace, 'request', 'task 3') < idxOf(trace, 'end', 'review 2'), 'task 3 was requested before review 2 ended')
})

await test('TC-27: finish-first off: a task start may queue, and nothing is logged about finish-first', async () => {
  const p = tc23Project()
  const { result, trace, logs } = await run(p, tc23Args({ finishFirst: false }), 2)
  assert.deepEqual([...result.merged].sort(), ['1', '2', '3', '4', '5'])
  const st = states(trace)
  const queuedStarts = firstAttemptRequests(trace).filter(i => st[i].open >= 2)
  assert.ok(queuedStarts.length >= 1, 'at least one first-attempt task was requested while all runtime slots were taken')
  assert.deepEqual(finishFirstLines(logs), [], 'no log line mentions finish-first')
})

await test('TC-28: no finishFirst arg and agents without `run`: finish-first is on (the deferral happens)', async () => {
  const p = tc23Project()
  const args = tc23Args()
  delete args.finishFirst
  assert.ok(!('run' in args.agents))
  const { result, trace, logs } = await run(p, args, 2)
  assert.deepEqual([...result.merged].sort(), ['1', '2', '3', '4', '5'])
  assertNoTaskRequestWhileFinishQueued(trace)
  assertLand2Queued(trace)
  assert.ok(finishFirstLines(logs).length >= 1, 'the deferral was logged')
})

await test('TC-29: one finish-first log line per deferral period, however many agents end in it', async () => {
  // One runtime slot: while task n's review, land and the landing plan run (3 ends), the next ready task is held.
  const one = { 1: [], 2: [] }
  const p = project({ deps: one })
  const a = await run(p, { ...baseArgs, ...graphArgs(one), maxConcurrent: 1, mergeBatch: 1, agents: REVIEW_ON, agentSlots: 1, finishFirst: true }, 1)
  assert.deepEqual([...a.result.merged].sort(), ['1', '2'])
  assert.equal(finishFirstLines(a.logs).length, 1, `one deferral period, one line: ${JSON.stringify(finishFirstLines(a.logs))}`)
  const held = (trace, n, next) => endsBetween(trace, idxOf(trace, 'end', `task ${n}`), idxOf(trace, 'request', `task ${next}`))
  assert.ok(held(a.trace, 1, 2) >= 3, `at least 3 agents ended while task 2 was held (${held(a.trace, 1, 2)})`)

  // Two separate periods: task 2 held during task 1's finish, task 3 during task 2's.
  const two = { 1: [], 2: [], 3: [] }
  const q = project({ deps: two })
  const b = await run(q, { ...baseArgs, ...graphArgs(two), maxConcurrent: 1, mergeBatch: 1, agents: REVIEW_ON, agentSlots: 1, finishFirst: true }, 1)
  assert.deepEqual([...b.result.merged].sort(), ['1', '2', '3'])
  assert.equal(finishFirstLines(b.logs).length, 2, `two deferral periods, two lines: ${JSON.stringify(finishFirstLines(b.logs))}`)
  assert.ok(held(b.trace, 1, 2) >= 3 && held(b.trace, 2, 3) >= 3, 'each period had at least 3 agent ends')
})

process.exit(process.exitCode ?? 0)
