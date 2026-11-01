// Deterministic-plan example cases (Stories 1-4: TC-0 smoke here; TC-1/TC-2/TC-4..TC-7,
// TC-9..TC-22 and TC-26 arrive with the tasks that own them).
// Run: node tests/js/deterministic_plan.test.mjs [TC-id ...]  (no ids: run every case)
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow } from './load_workflow.mjs'
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
  assert.ok(!p.state.events.some(e => /^plan 1 retry /.test(e)), 'no third plan attempt after the retry')
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

await test('TC-4 fallback excludes done tasks: always-inconsistent plan still completes without touching done records (1.5)', async () => {
  const deps = { 1: [], 2: [], 3: [] }
  // Every plan answer lists ready tasks in an order contradicting the dependency
  // graph (chain-then-numeric order is ['2', '3']): inconsistent against truthful deps.
  const p = project({ deps, done: ['1'], badReady: ['3', '2'] })
  const { result, logs } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps) })
  assert.ok(idx(p.state, 'plan 1') >= 0, 'the start plan ran')
  assert.ok(idx(p.state, 'plan 1 retry') > idx(p.state, 'plan 1'), 'the inconsistent plan was retried once')
  assert.equal(result.halted, false, 'the run continued from the deterministic list instead of halting')
  assert.notEqual(result.reason, 'planning step failed')
  assert.deepEqual(result.merged.sort(), ['2', '3'], 'the remaining tasks merged')
  assert.ok(!p.state.events.includes('task 1'), 'the Done task was never requested')
  for (const n of ['2', '3']) {
    assert.equal(p.state.events.filter(e => e === `task ${n}`).length, 1, `task ${n} started exactly once`)
  }
  assert.equal(result.degraded, true, 'a fallback run is marked degraded')
  assert.ok(result.planFallbacks.length >= 1, 'at least one fallback was recorded')
  assert.ok(result.planFallbacks.every(e => !e.ready.includes('1')), 'no fallback list names the Done task')
  assert.ok(logs.some(l => /plan 1/.test(l) && /inconsistent/.test(l)), 'the log names the triggering plan attempt')
})

const unknown = [...only].filter(id => !seen.has(id))
if (unknown.length) { console.log('FAIL unknown TC id(s):', unknown.join(' ')); process.exitCode = 1 }
