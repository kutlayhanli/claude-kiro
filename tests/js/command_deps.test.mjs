// Live command edges (Story 12) and write-set scheduling (Story 13) against the real
// plugin/workflows/spec-implement.js under the fake runtime; agents are the only fakes.
// Assertions use observable behaviour only: requested labels and their order (trace), the result and the log.
// Run: node tests/js/command_deps.test.mjs
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow } from './load_workflow.mjs'
import { project } from './fake_project.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../plugin/workflows/spec-implement.js')

const baseArgs = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.ck/worktrees/demo-integration' }

async function test(name, fn) {
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}

const strDeps = deps => Object.fromEntries(Object.entries(deps).map(([k, v]) => [String(k), v.map(String)]))
const withGraph = (deps, extra = {}) => {
  const d = strDeps(deps)
  return { ...baseArgs, deps: d, remaining: Object.keys(d), ...extra }
}
const sorted = a => [...a].map(String).sort((x, y) => Number(x) - Number(y))
const requestsOf = (trace, n) => trace.map((e, i) => ({ ...e, i })).filter(e => e.ev === 'request' && new RegExp(`^task ${n}( |$)`).test(e.label))
const landTasks = l => (l.startsWith('land ') ? l.slice(5).split(' ') : [])
const landEndOf = (trace, n) => trace.findIndex(e => e.ev === 'end' && landTasks(e.label).includes(n))
const taskEnd = (trace, n) => trace.findIndex(e => e.ev === 'end' && new RegExp(`^task ${n}( |$)`).test(e.label))
const taskStart = (trace, n) => trace.findIndex(e => e.ev === 'start' && new RegExp(`^task ${n}( |$)`).test(e.label))
const failedRec = (result, n) => (result.failed || []).find(t => String(t.task) === n)

const CMD = 'mtr thesis new'
const missing = (providers, file = 'tests/test_cli.py') => [{
  node: `${file}::test_thesis_new`, file, kind: 'error', message: "No such command 'thesis'.", truncated: false,
  command: 'uv run pytest -q', missingCommand: { command: CMD, providers },
}]

// Tasks 2 and 5 are independent; Task 2's gate misses `mtr thesis new`, which unfinished Task 5 provides.
const runLive = () => {
  const deps = { 2: [], 5: [] }
  const p = project({ deps, failUntil: { 2: 1 }, taskFailures: { 2: missing(['5']) }, provides: { [CMD]: ['5'] }, durations: { 2: 5, 5: 300 }, mergeDurations: { 5: 40 } })
  return runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 2 }) }).then(r => ({ ...r, p }))
}

await test('TC-114: a missing command of unfinished Task 5 adds a live edge: no conflict, no retry, Task 2 re-requested after land 5 (12.5)', async () => {
  const { result, trace, p } = await runLive()
  assert.deepEqual(sorted(result.merged), ['2', '5'])
  const reqs = requestsOf(trace, 2)
  assert.equal(reqs.length, 2, `task 2 requested twice: ${reqs.map(r => r.label).join(', ')}`)
  assert.ok(reqs.every(r => !/retry/.test(r.label)), `no retry label: ${reqs.map(r => r.label).join(', ')}`)
  const land5 = landEndOf(trace, '5')
  assert.ok(land5 >= 0, 'task 5 landed')
  assert.ok(reqs[1].i > land5, 'task 2 re-requested only after the landing of task 5 ended')
  assert.ok(!p.state.events.some(e => /^testfix|^task 2 retry/.test(e)), `no confirmation or test-fixer: ${p.state.events.join(', ')}`)
  assert.deepEqual(result.testFixes || [], [], 'no test conflict was raised')
  assert.ok(!failedRec(result, '2'), 'task 2 did not fail')
})

await test('TC-116: a missing command no task provides becomes a test conflict through confirmation, then the test-fixer (12.6)', async () => {
  const deps = { 2: [] }
  const p = project({ deps, failUntil: { 2: 99 }, taskFailures: { 2: missing([]) }, testFix: { 'tests/test_cli.py': { verdict: 'fixed' } } })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps) })
  assert.deepEqual(result.liveEdges || [], [], 'no live edge for a command nobody provides')
  const labels = trace.filter(e => e.ev === 'request').map(e => e.label)
  const confirm = labels.findIndex(l => /^task 2 retry/.test(l))
  const fixer = labels.findIndex(l => /^testfix \d+ \(Task 2\)/.test(l))
  assert.ok(confirm >= 0, `confirmation attempt requested: ${labels.join(' | ')}`)
  assert.ok(fixer > confirm, `the test-fixer comes after the confirmation: ${labels.join(' | ')}`)
})

await test('TC-117: a live edge that would close a cycle is not added; Task 2 is blocked naming it (12.7)', async () => {
  const deps = { 2: [], 5: ['2'] }
  const p = project({ deps, failUntil: { 2: 99 }, taskFailures: { 2: missing(['5']) }, provides: { [CMD]: ['5'] } })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps) })
  assert.deepEqual(result.liveEdges || [], [], 'no edge added')
  const f = failedRec(result, '2')
  assert.ok(f, 'task 2 failed')
  assert.match(f.reason, /blocked: command cycle Task 2 -> Task 5 -> Task 2/)
  assert.equal(requestsOf(trace, 2).length, 1, 'no retry, no wait')
  assert.ok(!result.merged.includes('5'), 'task 5 never ran')
})

await test('TC-118: live edges are logged and liveEdges[] holds {task, dependsOn, command} (12.8)', async () => {
  const { result, logs } = await runLive()
  const e = (result.liveEdges || []).find(x => String(x.task) === '2')
  assert.ok(e, `liveEdges has task 2: ${JSON.stringify(result.liveEdges)}`)
  assert.equal(String(e.dependsOn), '5')
  assert.equal(e.command, CMD)
  assert.ok(logs.some(l => /live edge/i.test(l) && l.includes(CMD) && /\b2\b/.test(l) && /\b5\b/.test(l)), `log names the live edge: ${logs.join(' | ')}`)
})

const OV = { '2': ['3'], '3': ['2'] }

await test('TC-120: overlapping Tasks 2 and 3 run in priority order; Task 3 starts after Task 2 lands (13.1, 13.2)', async () => {
  const deps = { 2: [], 3: [] }
  const p = project({ deps, overlaps: OV, durations: { 2: 30 }, mergeDurations: { 2: 10 } })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 2 }) })
  assert.deepEqual(sorted(result.merged), ['2', '3'])
  const r2 = requestsOf(trace, 2)[0].i
  const r3 = requestsOf(trace, 3)[0].i
  assert.ok(r2 < r3, 'task 2 first')
  assert.ok(r3 > landEndOf(trace, '2'), 'task 3 first requested after task 2 land ended')
})

await test('TC-121: a held task waits while a disjoint task fills the free slot; the hold names the blocker (13.3)', async () => {
  const deps = { 2: [], 3: [], 4: [] }
  const p = project({ deps, overlaps: OV, durations: { 2: 100 }, mergeDurations: { 2: 10 } })
  const { result, trace, logs } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 2 }) })
  assert.deepEqual(sorted(result.merged), ['2', '3', '4'])
  assert.ok(taskStart(trace, '4') >= 0 && taskStart(trace, '4') < taskEnd(trace, '2'), 'task 4 started while task 2 ran')
  assert.ok(requestsOf(trace, 3)[0].i > landEndOf(trace, '2'), 'task 3 held until task 2 landed')
  const hold = (result.writeSetHolds || []).find(h => String(h.task) === '3')
  assert.ok(hold && String(hold.waitsOn) === '2', `writeSetHolds names 3 waiting on 2: ${JSON.stringify(result.writeSetHolds)}`)
  assert.ok(logs.some(l => /held|hold/i.test(l) && /\b3\b/.test(l) && /\b2\b/.test(l)), `log names the hold: ${logs.join(' | ')}`)
})

await test('TC-122a: a task with no Files (no overlaps entry) is not held (13.4)', async () => {
  const deps = { 2: [], 3: [], 4: [] }
  const p = project({ deps, overlaps: { '3': ['4'], '4': ['3'] }, durations: { 2: 60, 3: 60 }, mergeDurations: { 3: 5 } })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 3 }) })
  assert.deepEqual(sorted(result.merged), ['2', '3', '4'])
  assert.ok(taskStart(trace, '2') >= 0 && taskStart(trace, '2') < taskEnd(trace, '3'), 'task 2 ran alongside task 3')
  assert.ok(!(result.writeSetHolds || []).some(h => String(h.task) === '2'), 'task 2 is never held')
  assert.ok((result.writeSetHolds || []).some(h => String(h.task) === '4' && String(h.waitsOn) === '3'), 'the overlapping pair still holds')
})

await test('TC-123: when the running task fails the next held task starts and the run terminates (13.5)', async () => {
  const deps = { 2: [], 3: [], 4: [] }
  const all = { '2': ['3', '4'], '3': ['2', '4'], '4': ['2', '3'] }
  const p = project({ deps, overlaps: all, failUntil: { 2: 99 }, durations: { 2: 20 } })
  const { result, trace } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 3, maxRetries: 0 }) })
  assert.deepEqual(sorted(result.merged), ['3', '4'])
  assert.ok(failedRec(result, '2'), 'task 2 failed')
  assert.ok(requestsOf(trace, 3)[0].i > taskEnd(trace, '2'), 'task 3 started after task 2 stopped')
  assert.ok(requestsOf(trace, 4)[0].i > landEndOf(trace, '3'), 'task 4 waited for task 3')
})

await test('TC-124a: plan deps stay unchanged and no relink is called for overlap (13.6)', async () => {
  const deps = { 2: [], 3: [], 4: ['2'] }
  const p = project({ deps, overlaps: OV })
  const before = JSON.stringify(p.state.deps)
  const { result } = await runWorkflow(SCRIPT, { agent: p.agent, args: withGraph(deps, { maxConcurrent: 2 }) })
  assert.deepEqual(sorted(result.merged), ['2', '3', '4'])
  assert.equal(JSON.stringify(p.state.deps), before, 'the plan answers carried the same deps all run')
  assert.deepEqual(p.state.relinks, [], 'no relink agent ran')
  assert.ok(!p.state.events.some(e => /^relink/.test(e)), 'no relink call')
  assert.ok((result.writeSetHolds || []).some(h => String(h.task) === '3'), 'the overlap was handled by a hold instead')
})
