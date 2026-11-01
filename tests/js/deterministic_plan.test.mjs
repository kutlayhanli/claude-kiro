// Deterministic-plan example cases (Stories 1-4: TC-0 smoke here; TC-1/TC-2/TC-4..TC-7,
// TC-9..TC-22 and TC-26 arrive with the tasks that own them).
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
  const deps = { 1: [], 2: [] }
  // No args.deps (dep-less). The start plan is good with ready ['1','2'], so it
  // is stored as last-good and both tasks start; task 1 is fast, task 2 is slow
  // (durations/planDurations overlap as in TC-4 so the in-flight halt is
  // deterministic, not timing luck). Task 1 lands, triggering plan 2, which
  // fails twice on unknown ids. The last-good fallback subtracts finished task
  // 1 and still-running task 2, leaving nothing — so the run halts while task 2
  // is still in flight, and task 2 still lands afterwards.
  const p = project({ deps, planSeq: [{ ready: ['1', '2'] }, { ready: ['9'] }, { ready: ['8'] }], durations: { 2: 200 }, planDurations: { 2: 100 } })
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, maxConcurrent: 2 } })
  assert.ok(idx(p.state, 'plan 1') >= 0, 'the start plan ran')
  assert.ok(!p.state.events.includes('plan 1 retry'), 'the good start plan needed no retry')
  assert.ok(idx(p.state, 'plan 2') >= 0, 'a landing-triggered plan ran while task 2 was still in flight')
  assert.ok(idx(p.state, 'plan 2 retry') > idx(p.state, 'plan 2'), 'the inconsistent plan was retried once')
  assert.equal(p.state.events.filter(e => e === 'task 2').length, 1, 'task 2 started exactly once, before the halt')
  assert.ok(idx(p.state, 'task 2') < idx(p.state, 'plan 2 retry'), 'task 2 was already in flight when the retry failed')
  const afterHalt = p.state.events.slice(idx(p.state, 'plan 2 retry') + 1)
  assert.ok(!afterHalt.some(e => e.startsWith('task ')), 'no new task label was requested after the failed retry')
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

const unknown = [...only].filter(id => !seen.has(id))
if (unknown.length) { console.log('FAIL unknown TC id(s):', unknown.join(' ')); process.exitCode = 1 }
