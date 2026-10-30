// TC-29..TC-35, TC-79 and the Workflow halves of TC-36 and TC-80: the guard-escalation ladder, parking and the
// decision note, run against the real plugin/workflows/spec-implement.js under the stub runtime.
// Run: node tests/js/guard_ladder.test.mjs
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

const roles = (model, escalate, extra = {}) => ({
  implementer: { model, effort: 'medium', escalate, ...(extra.implementer || {}) },
  test_writer: { model, effort: 'medium', escalate },
  fixer: { model, effort: 'medium', escalate },
  reviewer: { model: 'haiku', effort: 'medium', enabled: false, rounds: 1 },
  ...(extra.rest || {}),
})
const REVIEW_ON = { reviewer: { model: 'opus', effort: 'high', enabled: true, rounds: 2 } }
const attemptsOf = (s, n = '1') => s.events.filter(e => e.startsWith(`task ${n}`) && !e.endsWith(' end'))
const modelsOf = (s, n = '1') => attemptsOf(s, n).map(e => s.opts[e].model)
const run = (p, args) => runWorkflow(SCRIPT, { agent: p.agent, args: { ...baseArgs, ...args } })
const task = (result, n) => result.tasks.find(t => t.task === n)
const WT1 = '/repo/.ck/worktrees/demo-task-1'

await test('TC-29: a guard stop climbs sonnet, opus, fable at the role effort, whatever escalate says', async () => {
  const p = project({ deps: { 1: [] }, guardStops: { 1: 'opus' } })
  const { result } = await run(p, { agents: roles('haiku', []) })
  assert.deepEqual(modelsOf(p.state), ['haiku', 'sonnet', 'opus', 'fable'])
  assert.ok(attemptsOf(p.state).every(l => p.state.opts[l].effort === 'medium'), 'all attempts at effort medium')
  assert.deepEqual(attemptsOf(p.state).slice(1), ['task 1 guard 1 (sonnet)', 'task 1 guard 2 (opus)', 'task 1 guard 3 (fable)'])
  for (const l of attemptsOf(p.state).slice(1)) assert.match(p.state.prompts[l], /handoff/i, `${l} names the handoff note`)
  assert.match(p.state.prompts['task 1 guard 2 (opus)'], /\.handoff\.md/)
  assert.deepEqual(task(result, '1').escalations, [{ stage: 'guard', model: 'sonnet' }, { stage: 'guard', model: 'opus' }, { stage: 'guard', model: 'fable' }])
  assert.deepEqual(result.escalated, [{ task: '1', models: ['sonnet', 'opus', 'fable'], ok: true }])
  assert.deepEqual(result.merged, ['1'])
})

await test('TC-30: the ladder starts above the model that stopped and ends at the first attempt that does not', async () => {
  const a = project({ deps: { 1: [] }, guardStops: { 1: 'sonnet' } })
  const ra = await run(a, { agents: roles('sonnet', []) })
  assert.deepEqual(modelsOf(a.state), ['sonnet', 'opus'])
  assert.deepEqual(ra.result.merged, ['1'])

  const b = project({ deps: { 1: [] }, guardStops: { 1: 'opus' } })
  await run(b, { agents: roles('opus', []) })
  assert.deepEqual(modelsOf(b.state), ['opus', 'fable'])

  // A risky task starts at opus and never runs on sonnet.
  const c = project({ deps: { 1: [] }, guardStops: { 1: 'opus' } })
  const agents = roles('haiku', ['sonnet', 'opus'], { implementer: { risky_model: null, complexity_routing: false } })
  await run(c, { agents, risk: { 1: 'safety' } })
  assert.deepEqual(modelsOf(c.state), ['opus', 'fable'])
  assert.ok(!c.state.events.some(e => /guard \d \(sonnet\)/.test(e)))
})

await test('TC-31: guard attempts ignore implementer.escalate', async () => {
  const p = project({ deps: { 1: [] }, guardStops: { 1: 'sonnet' } })
  const { result } = await run(p, { agents: roles('haiku', ['opus']) })
  assert.deepEqual(modelsOf(p.state), ['haiku', 'sonnet', 'opus'])
  assert.deepEqual(task(result, '1').escalations, [{ stage: 'guard', model: 'sonnet' }, { stage: 'guard', model: 'opus' }])
})

await test('TC-32: ordinary failures keep the escalate sequence and never use the guard ladder', async () => {
  const p = project({ deps: { 1: [] }, failUntil: { 1: 3 } })
  const { result } = await run(p, { maxRetries: 1, agents: roles('haiku', ['sonnet', 'opus']) })
  assert.deepEqual(modelsOf(p.state), ['haiku', 'haiku', 'sonnet', 'opus'])
  assert.deepEqual(task(result, '1').escalations, [{ stage: 'retry', model: 'sonnet' }, { stage: 'retry', model: 'opus' }])
  assert.ok(!p.state.events.some(e => / guard \d/.test(e)))

  const red = project({ deps: { 1: [] }, checkRed: ['1'] })
  const r2 = await run(red, { agents: roles('haiku', ['sonnet']) })
  assert.ok(!red.state.events.some(e => / guard \d/.test(e)))
  assert.ok(!task(r2.result, '1').escalations.some(e => e.stage === 'guard'))

  const rev = project({ deps: { 1: [] }, reviews: { 1: ['changes', 'approve'] } })
  const r3 = await run(rev, { agents: roles('haiku', ['sonnet'], { rest: REVIEW_ON }) })
  assert.ok(!rev.state.events.some(e => / guard \d/.test(e)))
  assert.ok(!task(r3.result, '1').escalations.some(e => e.stage === 'guard'))
})

await test('TC-33: unresolved through fable, the task is parked once with the quoted test and requirement', async () => {
  const p = project({ deps: { 1: [] }, guardStops: { 1: 'always' } })
  const { result } = await run(p, { agents: roles('fable', []) })
  assert.equal(attemptsOf(p.state).length, 1)
  assert.equal(p.state.events.filter(e => e === 'park 1').length, 1)
  const prompt = p.state.prompts['park 1']
  assert.match(prompt, /ck worktree park demo 1/)
  assert.ok(prompt.includes('tests/test_guarded.py::test_x'), 'quotes the test')
  assert.ok(prompt.includes('1.1'), 'quotes the requirement')
  assert.deepEqual(result.merged, [])
})

await test('TC-34: parking blocks dependents only; needs-input is listed apart from merged, failed and blocked', async () => {
  const p = project({ deps: { 1: [], 2: ['1'], 3: [] }, guardStops: { 1: 'always' } })
  const { result } = await run(p, { agents: roles('fable', []) })
  assert.ok(!p.state.events.includes('task 2'), 'task 2 never starts')
  assert.deepEqual(result.merged, ['3'])
  assert.equal(result.needsInput.length, 1)
  const ni = result.needsInput[0]
  assert.equal(ni.task, '1')
  assert.equal(ni.test, 'tests/test_guarded.py::test_x')
  assert.equal(ni.requirement, '1.1')
  assert.ok('decision' in ni, 'carries the decision needed')
  assert.equal(ni.action, 'park')
  assert.ok(!result.failed.some(f => f.task === '1'))
  assert.ok(!result.blockedOn.some(b => b.task === '1'))
  assert.ok(!result.merged.includes('1'))
  assert.ok(result.blocked.some(b => b.task === '2' && b.on.includes('1')), JSON.stringify(result.blocked))
  assert.notEqual(result.outcome, 'complete')
  assert.equal(task(result, '1').parked, true)
})

await test('TC-35: a missing or failed notification reaches the result and the run continues', async () => {
  const a = project({ deps: { 1: [], 2: [] }, guardStops: { 1: 'always' }, parkOutcome: { parked: true, notification: 'not_configured', detail: 'set run.notify_command with ck agents set run.notify_command' } })
  const ra = await run(a, { agents: roles('fable', []) })
  assert.equal(ra.result.notifications.notConfigured, true)
  assert.match(ra.result.notifications.hint, /ck agents set run\.notify_command/)
  assert.deepEqual(ra.result.merged, ['2'])

  const b = project({ deps: { 1: [], 2: [] }, guardStops: { 1: 'always' }, parkOutcome: { parked: true, notification: 'failed', detail: 'exit 3' } })
  const rb = await run(b, { agents: roles('fable', []) })
  assert.equal(rb.result.notifications.failed[0].detail, 'exit 3')
  assert.equal(rb.result.notifications.failed[0].task, '1')
  assert.deepEqual(rb.result.merged, ['2'])
})

await test('TC-36: an interactive run parks nothing and lists the entry with action ask', async () => {
  const p = project({ deps: { 1: [] }, guardStops: { 1: 'always' } })
  const { result } = await run(p, { interactive: true, agents: roles('fable', []) })
  assert.ok(!p.state.events.some(e => e.startsWith('park')), 'no park agent runs')
  assert.equal(result.needsInput[0].action, 'ask')
  assert.equal(result.needsInput[0].test, 'tests/test_guarded.py::test_x')
  assert.equal(result.needsInput[0].requirement, '1.1')
})

await test('TC-79: a guard stop spends no retry or escalate step; a revise-call guard stop reaches the ladder', async () => {
  const p = project({ deps: { 1: [] }, guardStops: { 1: 'sonnet' } })
  const { result } = await run(p, { maxRetries: 2, agents: roles('haiku', ['sonnet', 'opus']) })
  assert.ok(!p.state.events.some(e => / retry \d/.test(e)), p.state.events.join(', '))
  assert.deepEqual(attemptsOf(p.state), ['task 1', 'task 1 guard 1 (sonnet)', 'task 1 guard 2 (opus)'])
  assert.ok(!task(result, '1').escalations.some(e => e.stage === 'retry'))
  assert.deepEqual(result.merged, ['1'])

  const r = project({ deps: { 1: [] }, guardStops: { 1: 'sonnet' }, guardStopIn: { 1: 'revise' }, reviews: { 1: ['changes', 'approve'] } })
  const rr = await run(r, { agents: roles('haiku', ['sonnet', 'opus'], { rest: REVIEW_ON }) })
  assert.ok(r.state.events.some(e => / revise /.test(e) && !e.endsWith(' end')), 'a revise call ran')
  assert.ok(r.state.events.some(e => / guard \d \(/.test(e)), 'the revise guard stop reached the ladder')
  assert.ok(!rr.result.failed.some(f => f.task === '1'))
  // Harness gap: the 'always' revise-call park is not asserted. fake_project stops only the revise call itself, and a
  // ladder attempt (label `task N guard k (model)`) is not a revise call, so it never stops again.
})

await test('TC-80: task, retry, revise and guard prompts read the decision note first', async () => {
  const note = `${WT1}.decision.md`
  const p = project({ deps: { 1: [] }, failUntil: { 1: 1 }, guardStops: { 1: 'haiku' }, reviews: { 1: ['changes', 'approve'] } })
  await run(p, { maxRetries: 1, agents: roles('haiku', ['sonnet'], { rest: REVIEW_ON }) })
  const labels = Object.keys(p.state.prompts).filter(l => /^task 1/.test(l))
  const kinds = new Set(labels.map(l => (l.match(/ (retry|revise|guard)\b/) || [, 'task'])[1]))
  assert.ok(kinds.has('task') && kinds.has('guard'), [...kinds].join(','))
  // A second project where a retry really runs (no guard stop), and a review round that asks for changes.
  const q = project({ deps: { 1: [] }, failUntil: { 1: 1 }, reviews: { 1: ['changes', 'approve'] } })
  await run(q, { maxRetries: 1, agents: roles('haiku', ['sonnet'], { rest: REVIEW_ON }) })
  const qLabels = Object.keys(q.state.prompts).filter(l => /^task 1/.test(l))
  const qKinds = new Set(qLabels.map(l => (l.match(/ (retry|revise|guard)\b/) || [, 'task'])[1]))
  assert.ok(qKinds.has('retry') && qKinds.has('revise'), [...qKinds].join(','))
  const entries = [...labels.map(l => [l, p.state.prompts[l]]), ...qLabels.map(l => [l, q.state.prompts[l]])]
  for (const [l, prompt] of entries) {
    const at = prompt.indexOf(note)
    assert.ok(at >= 0, `${l} names ${note}`)
    assert.ok(/read/i.test(prompt.slice(Math.max(0, at - 160), at + 160)), `${l} says to read it`)
    assert.ok(/follow/i.test(prompt.slice(at, at + 400)), `${l} says to follow it`)
    const gate = prompt.indexOf('gate')
    assert.ok(gate < 0 || at < gate, `${l} reads it before the work starts`)
  }
})
