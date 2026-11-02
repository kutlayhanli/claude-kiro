// Deterministic-plan example cases (Stories 1-4). Covered here: TC-0 smoke, TC-1,
// TC-3..TC-7, TC-9..TC-22. TC-2 and TC-26 arrive with the tasks that own them.
// Run: node tests/js/deterministic_plan.test.mjs [TC-id ...]  (no ids: run every case)
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow } from './load_workflow.mjs'
import { loadDecisions } from './load_decisions.mjs'
import { project } from './fake_project.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../plugin/workflows/spec-implement.js')

export const baseArgs = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.ck/worktrees/demo-integration' }
export const idx = (s, l) => s.events.indexOf(l)

// Story-3 schedule helpers (TC-11..TC-16): classify plan requests from the real
// trace without reading script internals. A non-retry plan is expected to be the
// start plan, the first plan after a landing completed, or an idle re-read (the
// script logs `plan N (idle):` when the idle trigger fires).
const planReqs = events => events.filter(e => /^plan \d+$/.test(e))
const planRetries = events => events.filter(e => /^plan \d+ retry$/.test(e))
const landReqs = events => events.filter(e => /^land /.test(e) && !e.endsWith(' end'))
const landEnds = events => events.filter(e => /^land .* end$/.test(e))
const idlePlans = logs => [...new Set([...logs.join('\n').matchAll(/plan (\d+) \(idle\)/g)].map(m => `plan ${m[1]}`))]
const checkPlanTriggers = (events, logs, label) => {
  const idle = new Set(idlePlans(logs))
  const plans = planReqs(events)
  assert.ok(plans.length > 0, `${label}: at least one plan agent ran`)
  assert.equal(plans[0], 'plan 1', `${label}: the first plan agent is the start plan`)
  let prev = events.indexOf(plans[0])
  for (const p of plans.slice(1)) {
    const at = events.indexOf(p, prev + 1)
    if (idle.has(p)) { prev = at; continue }
    const between = events.slice(prev + 1, at)
    assert.ok(between.some(e => /^land .* end$/.test(e)), `${label}: ${p} is the first plan after a landing (saw between: ${between})`)
    prev = at
  }
}

// Args as the skill builds them: deps and remaining copied from `ck plan --json`.
const strDeps = deps => Object.fromEntries(Object.entries(deps).map(([k, v]) => [String(k), v.map(String)]))
const withGraph = (deps, extra = {}) => {
  const d = strDeps(deps)
  return { ...baseArgs, deps: d, remaining: Object.keys(d), ...extra }
}

// TC-id argv filter: with ids, only cases whose name starts with one of them run.
const only = new Set(process.argv.slice(2).filter(a => /^TC-\d+$/.test(a)))
const seen = new Set()
async function test(name, fn) {
  const id = (name.match(/^(TC-\d+)/) || [])[1]
  if (id) seen.add(id)
  if (only.size && !(id && only.has(id))) return
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}

await test('TC-0 harness smoke: a healthy 2-task run completes', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: baseArgs })
  assert.equal(result.halted, false)
  assert.deepEqual(result.merged.sort(), ['1', '2'])
  assert.ok(idx(p.state, 'plan 1') >= 0, 'the real script requested a plan agent')
})

await test('TC-1 retry once then adopt: inconsistent first plan retries with the same inputs and the retry is adopted (1.1, 1.4)', async () => {
  const deps = { 1: [], 2: ['1'] }
  // First answer claims ready ['2'] against truthful deps (2 is not ready: 1 is not Done);
  // the retry is a healthy answer (default ck plan semantics: ready ['1']).
  const p = project({ deps, planSeq: [{ ready: ['2'] }, {}] })
  const { result, logs } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps) })
  assert.ok(idx(p.state, 'plan 1') >= 0, 'the start plan ran')
  assert.ok(idx(p.state, 'plan 1 retry') > idx(p.state, 'plan 1'), 'exactly one retry followed the inconsistent plan')
  assert.deepEqual(p.state.events.filter(e => e === 'plan 1' || e === 'plan 1 retry'), ['plan 1', 'plan 1 retry'], 'no third plan attempt after the retry')
  assert.equal(p.state.prompts['plan 1 retry'], p.state.prompts['plan 1'], 'the retry ran with the same inputs (same per-attempt exclude capture)')
  assert.deepEqual(result.merged.sort(), ['1', '2'], 'the retry plan was adopted and the run completed')
  assert.equal(result.halted, false)
  assert.ok(logs.some(l => l.includes('plan 1: the plan is inconsistent')), 'the log names the failed plan attempt')
  assert.equal(result.degraded, false, 'a run adopted from the retry counts no fallback')
  assert.ok(Array.isArray(result.planFallbacks), 'the result carries the fallback list')
  assert.equal(result.planFallbacks.length, 0, 'no fallback was recorded')
})

await test('TC-3 healthy plan: no degraded mark and no fallback (1.4)', async () => {
  const deps = { 1: [], 2: ['1'], 3: ['2'] }
  const p = project({ deps })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps) })
  assert.ok(p.state.plans >= 1, 'plan agents ran')
  assert.deepEqual(result.merged.sort(), ['1', '2', '3'])
  assert.equal(result.halted, false)
  assert.equal(result.degraded, false, 'a healthy run carries no degraded mark')
  assert.ok(Array.isArray(result.planFallbacks), 'the result carries the fallback list')
  assert.equal(result.planFallbacks.length, 0, 'a healthy run records no fallback')
})

await test('TC-4 fallback excludes done/running tasks: always-inconsistent plan still completes without touching done/running records (1.5)', async () => {
  const deps = { 1: [], 2: [], 3: [] }
  // Every plan answer lists ready tasks in an order contradicting the dependency
  // graph (chain-then-numeric order is ['2', '3']): inconsistent against truthful deps.
  // Task 1 is already merged (done seed); task 2 is slow so it is still running
  // when the landing-triggered plan 2 runs and falls back (planDurations widens
  // the overlap so the running exclusion is deterministic, not timing luck).
  const p = project({ deps, done: ['1'], badReady: ['3', '2'], durations: { 2: 200 }, planDurations: { 2: 100 } })
  const { result, logs } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps) })
  assert.ok(idx(p.state, 'plan 1') >= 0, 'the start plan ran')
  assert.ok(idx(p.state, 'plan 1 retry') > idx(p.state, 'plan 1'), 'the inconsistent plan was retried once')
  assert.equal(result.halted, false, 'the run continued from the deterministic list instead of halting')
  assert.notEqual(result.reason, 'planning step failed')
  assert.deepEqual(result.merged.sort(), ['2', '3'], 'the remaining tasks merged')
  assert.ok(idx(p.state, 'plan 2') >= 0, 'a landing-triggered plan ran while task 2 was still in flight')
  assert.ok(!p.state.events.includes('task 1'), 'the Done task was never requested')
  for (const n of ['2', '3']) {
    assert.equal(p.state.events.filter(e => e === `task ${n}`).length, 1, `task ${n} started exactly once`)
  }
  const rows = Object.fromEntries((result.tasks || []).map(t => [t.task, t]))
  assert.deepEqual(Object.keys(rows).sort(), ['2', '3'], 'only scheduled tasks have records; the Done task record is untouched')
  assert.ok(rows['2'].ok && rows['3'].ok, 'scheduled tasks merged without their records changing')
  assert.equal(result.degraded, true, 'a fallback run is marked degraded')
  const fallbacks = result.planFallbacks || []
  assert.ok(fallbacks.length >= 1, 'at least one fallback was recorded')
  assert.deepEqual(fallbacks[0].ready, ['2', '3'], 'the start fallback scheduled the deterministic ready list')
  assert.equal(fallbacks[0].planStep, 1, 'the fallback names the triggering plan step (1.3)')
  assert.equal(fallbacks[0].trigger, 'start', 'the fallback names its trigger (1.3)')
  for (const e of fallbacks) {
    assert.equal(e.source, 'deps', 'every fallback came from the supplied deps layer (2.1)')
    assert.ok(!e.ready.includes('1'), 'no fallback list names the Done task')
  }
  assert.ok(logs.some(l => /plan 1/.test(l) && /inconsistent/.test(l)), 'the log names the triggering plan attempt')
})

await test('TC-5 fallback from deps: always-inconsistent plan schedules every task as head of readyTasks over args.deps (2.1)', async () => {
  const deps = { 1: [], 2: [], 3: [] }
  // Every plan answer lists the same tasks in an order contradicting the
  // dependency graph (chain-then-numeric is ['1','2','3']); truthful deps stay.
  const p = project({ deps, badReady: ['3', '2', '1'] })
  const args = withGraph(deps, { maxConcurrent: 1 })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args })
  assert.ok(idx(p.state, 'plan 1') >= 0, 'the start plan ran')
  assert.ok(idx(p.state, 'plan 1 retry') > idx(p.state, 'plan 1'), 'the inconsistent plan was retried once')
  assert.equal(result.halted, false, 'the run continued from the deterministic list instead of halting')
  assert.notEqual(result.reason, 'planning step failed')
  assert.deepEqual(result.merged.sort(), ['1', '2', '3'], 'every task merged')
  // Per-request check (landing-speed TC-55(b) pattern): each first task request is
  // the head of readyTasks over args.deps plus the run's Done/running sets.
  const { readyTasks } = loadDecisions(SCRIPT)
  const d = strDeps(deps)
  const order = Object.keys(d)
  const requested = []
  const done = new Set()
  const firstTask = l => (String(l).match(/^task (\d+)(?: \[test\])?$/) || [])[1]
  for (const e of trace) {
    if (e.ev === 'end' && String(e.label).startsWith('land ')) for (const n of String(e.label).slice(5).split(' ')) done.add(n)
    if (e.ev !== 'request') continue
    const n = firstTask(e.label)
    if (!n) continue
    const head = readyTasks({ order, deps: d, done: [...done], exclude: [...requested], blocked: [] })[0]
    assert.equal(n, head, `request #${requested.length + 1} (task ${n}) vs readyTasks head ${head}`)
    requested.push(n)
  }
  assert.deepEqual(requested, ['1', '2', '3'], 'all three tasks were requested in deterministic order')
  assert.equal(result.degraded, true, 'a fallback run is marked degraded')
  const fallbacks = result.planFallbacks || []
  assert.ok(fallbacks.length >= 1, 'at least one fallback was recorded')
  for (const e of fallbacks) {
    assert.equal(e.source, 'deps', 'every fallback came from the supplied deps layer (2.1)')
  }
})

await test('TC-6 fallback from last-good: no deps, third plan failure schedules last-good-ready minus finished (2.2)', async () => {
  const deps = { 1: [], 2: [], 3: ['1'] }
  // Healthy ready is ['1','2'] (1 has the longer chain); the first plan is adopted
  // as last-good, the landing-triggered plan 2 fails twice on unknown ids.
  const p = project({ deps, planSeq: [{}, { ready: ['9'] }, { ready: ['8'] }] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxConcurrent: 1 } })
  assert.ok(idx(p.state, 'plan 2') >= 0, 'a landing-triggered plan ran after the good start plan')
  assert.ok(idx(p.state, 'plan 2 retry') > idx(p.state, 'plan 2'), 'the inconsistent plan was retried once')
  assert.equal(result.halted, false, 'the run continued from the last-good list instead of halting')
  assert.notEqual(result.reason, 'planning step failed')
  assert.deepEqual(result.merged.sort(), ['1', '2', '3'], 'every task merged')
  assert.equal(result.degraded, true, 'a fallback run is marked degraded')
  const fallbacks = result.planFallbacks || []
  assert.ok(fallbacks.length >= 1, 'at least one fallback was recorded')
  const lg = fallbacks.find(e => e.source === 'last-good')
  assert.ok(lg, 'a fallback came from the last-good layer (2.2)')
  // Exact remainder: last-good ready was ['1','2']; task 1 had already merged
  // when plan 2 fell back (see 'task 1 end' before 'plan 2' below), task 2 was
  // still open, and task 3 was never in the last-good list. Anything else —
  // re-scheduling finished task 1, adding task 3, or omitting task 2 — fails.
  assert.ok(idx(p.state, 'task 1 end') < idx(p.state, 'plan 2'), 'task 1 had finished before the fallback plan ran')
  assert.deepEqual(lg.ready, ['2'], 'the last-good fallback schedules exactly last-good-ready minus finished')
})

await test('TC-7 halt with no data: no deps and an emptied last-good list means no new task starts, running tasks still land, planning failure reported (2.3)', async () => {
  const deps = { 1: [], 2: [], 3: ['1', '2'] }
  // No args.deps (dep-less). The start plan is good with ready ['1','2'] (task 3
  // waits on 1 and 2), so it is stored as last-good and tasks 1 and 2 start;
  // task 1 is fast, task 2 is slow (durations/planDurations overlap as in TC-4
  // so the in-flight halt is deterministic, not timing luck). Task 1 lands,
  // triggering plan 2, whose ready names finished task 1 (inconsistent:
  // nothing is schedulable while the open tasks are excluded); the retry names
  // still-running task 2 (also inconsistent). The last-good fallback subtracts
  // finished task 1 and still-running task 2, leaving nothing — so the run
  // halts with task 3 unstarted while task 2 is still in flight, and task 2
  // still lands afterwards.
  const p = project({ deps, planSeq: [{ ready: ['1', '2'] }, { ready: ['1'] }, { ready: ['2'] }], durations: { 2: 400 }, planDurations: { 2: 100 } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxConcurrent: 2 } })
  assert.ok(idx(p.state, 'plan 1') >= 0, 'the start plan ran')
  assert.ok(!p.state.events.includes('plan 1 retry'), 'the good start plan needed no retry')
  assert.ok(idx(p.state, 'plan 2') >= 0, 'a landing-triggered plan ran while task 2 was still in flight')
  assert.ok(idx(p.state, 'plan 2 retry') > idx(p.state, 'plan 2'), 'the inconsistent plan was retried once')
  assert.equal(p.state.events.filter(e => e === 'task 2').length, 1, 'task 2 started exactly once, before the halt')
  assert.ok(!p.state.events.includes('task 3'), 'task 3 (deps unmet at halt) was never requested')
  assert.ok(idx(p.state, 'task 2') < idx(p.state, 'plan 2 retry'), 'task 2 was already in flight when the retry failed')
  const afterHalt = p.state.events.slice(idx(p.state, 'plan 2 retry') + 1)
  assert.ok(!afterHalt.some(e => /^task \d+$/.test(e)), 'no new task label was requested after the failed retry')
  const rows = Object.fromEntries((result.tasks || []).map(t => [t.task, t]))
  assert.ok(rows['2'] && rows['2'].ok, 'the in-flight task still landed and has a passing record')
  assert.deepEqual(result.merged.sort(), ['1', '2'], 'both tasks merged')
  assert.equal(result.halted, true, 'the run halted: no deterministic data remained')
  assert.equal(result.reason, 'planning step failed', 'the stop reason is the planning failure')
  assert.equal(result.degraded, false, 'a halted run with no fallback carries no degraded mark')
  assert.ok(Array.isArray(result.planFallbacks), 'the result carries the fallback list')
  assert.equal(result.planFallbacks.length, 0, 'no fallback was recorded')
})

await test('TC-9 unknown dep never scheduled: deps {1:[],2:[99]} with always-inconsistent plans (2.5)', async () => {
  const deps = { 1: [], 2: ['99'] }
  const p = project({ deps, badReady: ['2'] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 1 }) })
  assert.ok(idx(p.state, 'plan 1 retry') > idx(p.state, 'plan 1'), 'the inconsistent plan was retried once')
  assert.ok(!p.state.events.includes('task 2'), 'task 2 (unknown dep 99) was never requested')
  assert.ok(p.state.events.includes('task 1'), 'task 1 proceeded')
  assert.deepEqual(result.merged.sort(), ['1'], 'task 1 merged and the run did not halt')
  assert.equal(result.halted, false)
  assert.equal(result.degraded, true, 'a fallback run is marked degraded')
  const fallbacks = result.planFallbacks || []
  assert.ok(fallbacks.length >= 1, 'at least one fallback was recorded')
  for (const e of fallbacks) {
    assert.equal(e.source, 'deps', 'every fallback came from the supplied deps layer')
    assert.ok(!e.ready.includes('2'), 'no fallback list names the unknown-dep task')
  }
  // The seam agrees on the same inputs: task 2 is never ready, even when 99 is Done.
  const { readyTasks } = loadDecisions(SCRIPT)
  const d = strDeps(deps)
  for (const done of [[], ['1'], ['1', '99']]) {
    const ready = readyTasks({ order: ['1', '2'], deps: d, done, exclude: [], blocked: [] })
    assert.ok(!ready.includes('2'), `readyTasks(done=${done}) = ${ready}`)
  }
})

await test('TC-10 fallback BUSY/ERROR handled like plan-scheduled: same reason, one agent, run continues (2.6)', async () => {
  const deps = { 1: [], 2: [] }
  const wt = { 2: { state: 'BUSY', detail: 'claimed: someone' } }
  // Fallback run: every plan answer contradicts the graph, so task 2 is fallback-scheduled.
  const p = project({ deps, badReady: ['2', '1'], worktree: wt })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 2 }) })
  // Control run: healthy plans, so task 2 is plan-scheduled with the same BUSY report.
  const c = project({ deps, worktree: wt })
  const { result: control } = await runWorkflow(SCRIPT, { agent: c.agent, args: withGraph(deps, { maxConcurrent: 2 }) })
  assert.ok(idx(p.state, 'plan 1 retry') > idx(p.state, 'plan 1'), 'the inconsistent plan was retried once')
  assert.equal(p.state.events.filter(e => e === 'task 2').length, 1, 'exactly one agent ran for the fallback-scheduled task')
  const rows = Object.fromEntries((result.tasks || []).map(t => [t.task, t]))
  assert.ok(rows['2'], 'task 2 has a record')
  assert.equal(rows['2'].ok, false)
  const controlRows = Object.fromEntries((control.tasks || []).map(t => [t.task, t]))
  assert.equal(rows['2'].reason, controlRows['2'].reason, 'fallback BUSY reason matches the plan-scheduled BUSY reason')
  assert.ok(String(rows['2'].reason).startsWith('worktree busy:'), `reason names the busy worktree: ${rows['2'].reason}`)
  assert.ok(result.merged.includes('1'), 'the run continues: task 1 merged')
  assert.equal(result.halted, false)
  assert.equal(result.degraded, true, 'a fallback run is marked degraded')
  // ERROR variant: a fallback-scheduled task whose worktree creation fails is reported the same way.
  const q = project({ deps, badReady: ['2', '1'], worktree: { 2: { state: 'ERROR', detail: 'gone' } } })
  const { result: r2 } = await runWorkflow(SCRIPT, { agent: q.agent, args: withGraph(deps, { maxConcurrent: 2 }) })
  const rows2 = Object.fromEntries((r2.tasks || []).map(t => [t.task, t]))
  assert.ok(rows2['2'] && !rows2['2'].ok, 'the ERROR task has a failing record')
  // Control run: healthy plans, so task 2 is plan-scheduled with the same ERROR report.
  const qc = project({ deps, worktree: { 2: { state: 'ERROR', detail: 'gone' } } })
  const { result: control2 } = await runWorkflow(SCRIPT, { agent: qc.agent, args: withGraph(deps, { maxConcurrent: 2 }) })
  const controlRows2 = Object.fromEntries((control2.tasks || []).map(t => [t.task, t]))
  assert.equal(rows2['2'].reason, controlRows2['2'].reason, 'fallback ERROR reason matches the plan-scheduled ERROR reason')
  assert.ok(r2.merged.includes('1'), 'the run continues after the ERROR report')
})

await test('TC-11 plan triggers: healthy 12-task run plans only at start, after landings, or idle (3.1)', async () => {
  const deps = { 1: [], 2: ['1'], 3: ['1'], 4: ['2'], 5: ['2'], 6: ['3'], 7: ['4', '5'], 8: ['6'], 9: ['7', '8'], 10: ['9'], 11: ['9'], 12: ['10', '11'] }
  const p = project({ deps })
  const { result, logs } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 3 }) })
  assert.equal(result.halted, false)
  assert.deepEqual(result.merged.sort(), Object.keys(deps).sort(), 'every task merged')
  assert.deepEqual(planRetries(p.state.events), [], 'healthy plans need no retry')
  checkPlanTriggers(p.state.events, logs, 'TC-11')
  // The post-landing re-read exists ...
  const plans = planReqs(p.state.events)
  assert.ok(plans.length >= 2, 'at least one post-landing re-read ran')
  // ... and the per-freed-slot re-read does not: plans stay within 1 start + landings + idle.
  const lands = landReqs(p.state.events).length
  const idle = idlePlans(logs).length
  assert.ok(plans.length <= 1 + lands + idle, `plan count ${plans.length} stays within 1 start + ${lands} landings + ${idle} idle`)
})

await test('TC-12 freed slot starts ready tasks: task 2/task 3 requested with no intervening plan (3.2)', async () => {
  const deps = { 1: [], 2: [], 3: [] }
  // Task 1 is slow, so tasks 2 and 3 cycle through the other slot while it runs.
  const p = project({ deps, durations: { 1: 300 } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 2 }) })
  assert.equal(result.halted, false)
  assert.deepEqual(result.merged.sort(), ['1', '2', '3'])
  assert.deepEqual(planRetries(p.state.events), [], 'healthy plans need no retry')
  for (const n of ['1', '2', '3']) {
    assert.equal(p.state.events.filter(e => e === `task ${n}`).length, 1, `task ${n} requested exactly once`)
  }
  // Task 3 started from a freed slot while task 1 was still running.
  assert.ok(idx(p.state, 'task 3') < idx(p.state, 'task 1 end'), 'task 3 started from a freed slot while task 1 was still running')
  // A freed slot schedules directly: no plan agent runs between an implementer
  // finish and its land request (a landing plan only ever follows a land end).
  for (const n of ['1', '2', '3']) {
    const end = idx(p.state, `task ${n} end`)
    const land = p.state.events.findIndex((e, i) => i > end && /^land /.test(e) && !e.endsWith(' end'))
    assert.ok(land > end, `task ${n} went to landing after its implementer finished`)
    const between = p.state.events.slice(end + 1, land)
    assert.ok(!between.some(e => /^plan /.test(e)), `no plan between task ${n} end and its land request (saw: ${between})`)
  }
})

await test('TC-13 no plan after every freed slot: plan count stays within 1 + landings + idle (3.3)', async () => {
  const deps = { 1: [], 2: [], 3: [], 4: [], 5: [], 6: [] }
  // Staggered durations so every finish, landing and next start is separated in
  // time: any plan smuggled in between a finish and the next task start would show.
  const p = project({ deps, durations: { 1: 50, 2: 100, 3: 150, 4: 200, 5: 250, 6: 300 } })
  const { result, logs } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 2 }) })
  assert.equal(result.halted, false)
  assert.deepEqual(result.merged.sort(), ['1', '2', '3', '4', '5', '6'])
  assert.deepEqual(planRetries(p.state.events), [], 'healthy plans need no retry')
  for (const n of ['1', '2', '3', '4', '5', '6']) {
    assert.equal(p.state.events.filter(e => e === `task ${n}`).length, 1, `task ${n} requested exactly once`)
  }
  checkPlanTriggers(p.state.events, logs, 'TC-13')
  const plans = planReqs(p.state.events)
  const lands = landReqs(p.state.events).length
  const idle = idlePlans(logs).length
  assert.ok(plans.length <= 1 + lands + idle, `plan count ${plans.length} stays within 1 start + ${lands} landings + ${idle} idle`)
  // Freed slots recurred with ready tasks outstanding, yet no plan appears between
  // a finish and the next task request the deterministic list already determined.
  for (const e of p.state.events.filter(e => e.endsWith(' end') && /^task \d+ end$/.test(e))) {
    const end = p.state.events.indexOf(e)
    const next = p.state.events.findIndex((x, i) => i > end && /^task \d+$/.test(x))
    if (next < 0) continue // last finishes have no next task start
    const between = p.state.events.slice(end + 1, next)
    assert.ok(!between.some(x => /^plan /.test(x)), `no plan between ${e} and the next task request (saw: ${between})`)
  }
})

await test('TC-14 idle re-read disarms: at most one idle plan per arming, none until landing/record (3.4)', async () => {
  const deps = { 1: [], 2: ['1'] }
  const p = project({ deps })
  const { result, logs } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 1 }) })
  assert.equal(result.halted, false)
  assert.deepEqual(result.merged.sort(), ['1', '2'])
  assert.deepEqual(planRetries(p.state.events), [], 'healthy plans need no retry')
  // Nothing is ready and nothing is running while task 1 lands, so exactly one
  // idle re-read fires; the empty answer disarms it until the landing completes.
  const idle = idlePlans(logs)
  assert.deepEqual(idle, ['plan 2'], `exactly one idle plan fired (saw: ${idle})`)
  assert.ok(idx(p.state, 'plan 2') < idx(p.state, 'land 1 end'), 'the idle re-read ran while the landing was still in flight')
  checkPlanTriggers(p.state.events, logs, 'TC-14')
})

await test('TC-15 view replacement: consistent plan with externalDone plus relink at plan 2 replaces view, Done keeps merged-in-run (3.5)', async () => {
  const deps = { 1: [], 2: ['1'] }
  // From the second plan call on, task 7 is Done on the target and the plan data
  // relearns task 3 (relink). Both later answers omit merged task 1 from Done;
  // the view must still keep it Done via the merged-in-run union.
  const p = project({ deps, externalDone: { atPlan: 2, tasks: ['7'] }, relink: { atPlan: 2, deps: { 3: [] } }, planSeq: [{}, { done: ['7'], ready: ['3'] }, { done: ['7'], ready: [] }] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 1 }) })
  assert.equal(result.halted, false, 'the run completes on the replaced view')
  assert.deepEqual(result.merged.sort(), ['1', '2', '3'], 'the relinked task 3 merged with the rest')
  assert.ok(p.state.events.includes('task 3'), 'the relinked task was scheduled from the replaced view')
  for (const n of ['1', '2', '3']) {
    assert.equal(p.state.events.filter(e => e === `task ${n}`).length, 1, `task ${n} started exactly once (merged task 1 stayed Done even though the plans omitted it)`)
  }
  assert.ok(!p.state.events.includes('task 7'), 'externally-Done task 7 was never requested')
})

await test('TC-16 dep-less mode: a plan agent runs before the next task after freed slots (3.6)', async () => {
  const deps = { 1: [], 2: [], 3: [] }
  const p = project({ deps })
  // No args.deps: the script cannot use its own readiness and must re-plan per freed slot.
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxConcurrent: 1 } })
  assert.equal(result.halted, false)
  assert.deepEqual(result.merged.sort(), ['1', '2', '3'])
  assert.deepEqual(planRetries(p.state.events), [], 'healthy plans need no retry')
  for (const n of ['1', '2', '3']) {
    assert.equal(p.state.events.filter(e => e === `task ${n}`).length, 1, `task ${n} requested exactly once`)
  }
  // Every task after the first was preceded by a plan that ran after the
  // previous task finished: without dep data the freed slot always re-plans.
  for (const n of [2, 3]) {
    const prevEnd = idx(p.state, `task ${n - 1} end`)
    const start = idx(p.state, `task ${n}`)
    assert.ok(prevEnd >= 0 && start > prevEnd, `task ${n} started after task ${n - 1} finished`)
    const between = p.state.events.slice(prevEnd + 1, start)
    assert.ok(between.some(e => /^plan \d+$/.test(e)), `a plan agent ran between task ${n - 1} end and task ${n} (saw: ${between})`)
  }
})

// Story-4 helpers (TC-17..TC-22): wave batching is judged from the review ledger
// (state.reviewTasks: label -> covered tasks) and the event trace, never label text.
const REVIEWER = { model: 'sonnet', effort: 'low', risky_model: 'opus', risky_effort: 'high' }
const reviewArgs = (deps, { risk = {}, ...extra } = {}) =>
  withGraph(deps, { maxConcurrent: 4, agents: { reviewer: REVIEWER }, risk, ...extra })
// Timing that lands task 1 alone first, then tasks 2+ together in one later batch:
// task 1 ends at ~10ms and its landing takes 300ms, so tasks 2..4 (ending ~50ms)
// queue behind it and merge as one wave. Deterministic by a ~250ms margin.
const waveTiming = { durations: { 1: 10, 2: 50, 3: 50, 4: 50 }, mergeDurations: { 1: 300 } }
const reviewLabels = events => events.filter(e => e.startsWith('review') && !e.endsWith(' end'))
const individualLabels = events => events.filter(e => /^review \d/.test(e) && !e.endsWith(' end'))
const batchLabels = events => events.filter(e => e.startsWith('review-wave') && !e.endsWith(' end'))
const covers = (p, l) => [...(p.state.reviewTasks[l] || [])].sort()
const sameSet = (a, b) => a.length === b.length && a.every(x => b.includes(x))
const batchesCovering = (p, ids) => batchLabels(p.state.events).filter(l => sameSet(covers(p, l), [...ids].sort()))
const landEndOf = (p, n) => p.state.events.find(e => e.startsWith('land ') && e.endsWith(' end') && e.slice(5, -4).split(' ').includes(n))
const rowsByTask = result => Object.fromEntries((result.tasks || []).map(t => [t.task, t]))

await test('TC-17 batched review for a non-risky wave: one review-wave covering tasks 2-4, all approved (4.1)', async () => {
  const deps = { 1: [], 2: [], 3: [], 4: [] }
  const p = project({ deps, ...waveTiming })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: reviewArgs(deps) })
  assert.equal(result.halted, false)
  assert.deepEqual(result.merged.sort(), ['1', '2', '3', '4'], 'every task merged')
  assert.ok(landEndOf(p, '1'), 'task 1 landed in its own earlier batch')
  assert.equal(landEndOf(p, '2'), landEndOf(p, '3'), 'tasks 2 and 3 merged in the same wave batch')
  assert.equal(landEndOf(p, '3'), landEndOf(p, '4'), 'tasks 3 and 4 merged in the same wave batch')
  const wave = batchesCovering(p, ['2', '3', '4'])
  assert.equal(wave.length, 1, `exactly one batched review covers the wave (saw: ${batchLabels(p.state.events)})`)
  assert.equal(batchLabels(p.state.events).length, 2, 'one batched review per merged wave: [1] and [2,3,4]')
  assert.deepEqual(individualLabels(p.state.events), [], 'no per-task review labels for a non-risky run')
  const rows = rowsByTask(result)
  for (const n of ['2', '3', '4']) {
    assert.equal(rows[n].review.verdict, 'approve', `task ${n} is approved`)
  }
})

await test('TC-18 risky task reviewed individually sharing a wave with batched normals (4.2)', async () => {
  const deps = { 1: [], 2: [], 3: [], 4: [] }
  const risk = { 4: 'safety' }
  const p = project({ deps, ...waveTiming, risk })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: reviewArgs(deps, { risk }) })
  assert.equal(result.halted, false)
  assert.deepEqual(result.merged.sort(), ['1', '2', '3', '4'], 'every task merged')
  const wave = batchesCovering(p, ['2', '3'])
  assert.equal(wave.length, 1, `the two normal tasks share one batched review (saw: ${batchLabels(p.state.events)})`)
  assert.ok(!batchLabels(p.state.events).some(l => covers(p, l).includes('4')), 'no batched review covers the risky task')
  assert.deepEqual(individualLabels(p.state.events), ['review 4'], 'the risky task gets its own individual review')
  assert.equal(p.state.opts['review 4'].model, 'opus', 'the risky review runs at the risky reviewer model')
  assert.equal(p.state.opts['review 4'].effort, 'high', 'the risky review runs at the risky reviewer effort')
  assert.equal(p.state.opts[wave[0]].model, 'sonnet', 'the batched review runs at the normal reviewer model')
  assert.equal(p.state.opts[wave[0]].effort, 'low', 'the batched review runs at the normal reviewer effort')
  const rows = rowsByTask(result)
  for (const n of ['2', '3', '4']) {
    assert.equal(rows[n].review.verdict, 'approve', `task ${n} is approved`)
  }
})

await test('TC-19 risky-only wave: each risky task reviewed individually, no combined review (4.3)', async () => {
  const deps = { 1: [], 2: [], 3: [] }
  const risk = { 2: 'safety', 3: 'safety' }
  // Task 1 lands first alone; tasks 2-3 (both risky) merge as one wave behind it.
  const p = project({ deps, durations: { 1: 10, 2: 50, 3: 50 }, mergeDurations: { 1: 300 }, risk })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: reviewArgs(deps, { risk }) })
  assert.equal(result.halted, false)
  assert.deepEqual(result.merged.sort(), ['1', '2', '3'], 'every task merged')
  assert.equal(landEndOf(p, '2'), landEndOf(p, '3'), 'tasks 2 and 3 merged in the same wave batch')
  for (const n of ['2', '3']) {
    assert.equal(individualLabels(p.state.events).filter(l => l === `review ${n}`).length, 1, `risky task ${n} gets exactly one individual review`)
  }
  assert.ok(!batchLabels(p.state.events).some(l => covers(p, l).some(n => n === '2' || n === '3')), 'no combined review covers the risky-only wave')
  // Reviews happen after the wave merges, not per task before landing.
  for (const n of ['2', '3']) {
    assert.ok(idx(p.state, `review ${n}`) > idx(p.state, landEndOf(p, n)), `review ${n} runs after its wave merged`)
  }
})

await test('TC-20 batched changes flags one task: only task 3 revises, 2/4 approved untouched (4.4)', async () => {
  const deps = { 1: [], 2: [], 3: [], 4: [] }
  // The batched verdict (looked up by the wave's first covered task, whichever it
  // is, so every wave task carries the same first answer) flags only task 3.
  const flagged = { verdict: 'changes', findings: [{ task: '3', severity: 'blocking', file: 'x.py', issue: 'wave finding on 3', fix: 'fix 3' }] }
  const reviews = { 2: [flagged, 'approve'], 3: [flagged, 'approve'], 4: [flagged, 'approve'] }
  const p = project({ deps, ...waveTiming, reviews })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: reviewArgs(deps) })
  assert.equal(result.halted, false)
  assert.deepEqual(result.merged.sort(), ['1', '2', '3', '4'], 'every task merged after revision')
  const wave = batchesCovering(p, ['2', '3', '4'])
  assert.equal(wave.length, 1, `exactly one batched review covers the wave (saw: ${batchLabels(p.state.events)})`)
  assert.ok(p.state.events.includes('task 3 revise 1'), 'the flagged task goes to revision')
  for (const n of ['2', '4']) {
    assert.ok(!p.state.events.some(e => new RegExp(`^task ${n}( \\[test\\])? revise`).test(e)), `unflagged task ${n} is never revised`)
  }
  const covering = n => reviewLabels(p.state.events).filter(l => (p.state.reviewTasks[l] || []).includes(n))
  assert.equal(covering('3').length, 2, 'only the flagged task is re-reviewed (batch + re-review)')
  for (const n of ['2', '4']) {
    assert.equal(covering(n).length, 1, `unflagged task ${n} is reviewed once (the batch only)`)
  }
  const rows = rowsByTask(result)
  for (const n of ['2', '3', '4']) {
    assert.equal(rows[n].review.verdict, 'approve', `task ${n} ends approved`)
  }
})

await test('TC-21 batched approve records approval for every covered task (4.5)', async () => {
  const deps = { 1: [], 2: [], 3: [], 4: [] }
  const p = project({ deps, ...waveTiming })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: reviewArgs(deps) })
  assert.equal(result.halted, false)
  assert.deepEqual(result.merged.sort(), ['1', '2', '3', '4'], 'every task merged')
  const wave = batchesCovering(p, ['2', '3', '4'])
  assert.equal(wave.length, 1, `exactly one batched review covers the wave (saw: ${batchLabels(p.state.events)})`)
  assert.deepEqual(individualLabels(p.state.events), [], 'no per-task review labels for a non-risky run')
  const rows = rowsByTask(result)
  for (const n of ['2', '3', '4']) {
    assert.equal(rows[n].review.verdict, 'approve', `task ${n} records approval`)
    assert.equal(rows[n].review.model, 'sonnet', `task ${n} carries the reviewer attribution`)
    assert.equal(rows[n].review.riskTag, 'normal', `task ${n} carries its risk tag`)
  }
})

await test('TC-22 no wave merged: an unmerged task gets no review (4.6)', async () => {
  const deps = { 1: [], 2: [] }
  // Task 2 is protected: its landing never merges, so no wave containing it merges.
  const p = project({ deps, durations: { 1: 10, 2: 50 }, mergeDurations: { 1: 300 }, protectedTasks: ['2'] })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: reviewArgs(deps) })
  assert.deepEqual(result.merged.sort(), ['1'], 'only the unprotected task merged')
  assert.ok(!result.merged.includes('2'), 'the protected task never merged')
  const covering2 = reviewLabels(p.state.events).filter(l => (p.state.reviewTasks[l] || []).includes('2'))
  assert.deepEqual(covering2, [], 'no review agent is requested for the unmerged task (no empty batched review)')
  // The reviews are on: the merged wave was reviewed.
  assert.equal(batchesCovering(p, ['1']).length, 1, 'the merged wave still gets its batched review')
})

await test('TC-26 overhead above threshold warns without halting/failing/retrying (5.4)', async () => {
  // Forced overhead: tiny task work (1ms each) against many plan/review/land
  // agents (6 independent tasks at maxConcurrent 1: a plan plus a review plus
  // a landing per wave; plan/review/merge calls slowed so the plan/review/land
  // share dominates under any agent-time measure). Needs the Task 22 warn-only
  // wiring: the run still completes, nothing names overhead as a stop reason,
  // and the report flags the overhead warning.
  const deps = { 1: [], 2: [], 3: [], 4: [], 5: [], 6: [] }
  const slow = { 1: 50, 2: 50, 3: 50, 4: 50, 5: 50, 6: 50, 7: 50, 8: 50, 9: 50, 10: 50, 11: 50, 12: 50 }
  const p = project({ deps, durations: { 1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1 }, planDurations: slow, reviewDurations: slow, mergeDurations: slow })
  const { result, logs } = await runWorkflow(SCRIPT, { agent: p.agent, args: reviewArgs(deps, { maxConcurrent: 1 }) })
  assert.equal(result.halted, false, 'the run still completes under forced overhead')
  assert.equal(result.outcome, 'complete', 'the outcome is complete, not halted or blocked')
  assert.deepEqual(result.merged.sort(), ['1', '2', '3', '4', '5', '6'], 'every task merged')
  assert.ok(!result.reason || !/overhead/i.test(String(result.reason)), `no stop reason names overhead (saw: ${result.reason})`)
  assert.deepEqual(planRetries(p.state.events), [], 'healthy plans need no retry: overhead triggers no retry')
  checkPlanTriggers(p.state.events, logs, 'TC-26')
  assert.equal(result.degraded, false, 'a healthy run carries no degraded mark even under forced overhead')
  assert.ok(Array.isArray(result.planFallbacks), 'the result carries the fallback list')
  assert.equal(result.planFallbacks.length, 0, 'no fallback was recorded')
  const overhead = result.overhead
  assert.ok(overhead && typeof overhead === 'object', 'the result carries the overhead field (Task 22 warn-only wiring)')
  assert.equal(overhead.threshold, 0.5, 'the published threshold is 0.5')
  assert.ok(typeof overhead.share === 'number' && overhead.share > overhead.threshold, `the forced run exceeds the threshold (share=${overhead && overhead.share})`)
  assert.equal(overhead.warned, true, 'the over-threshold run flags the warning')
  const warnLine = logs.find(l => /overhead/i.test(l) && /warn/i.test(l))
  assert.ok(warnLine, 'the report flags the overhead warning')
})

const unknown = [...only].filter(id => !seen.has(id))
if (unknown.length) { console.log('FAIL unknown TC id(s):', unknown.join(' ')); process.exitCode = 1 }
