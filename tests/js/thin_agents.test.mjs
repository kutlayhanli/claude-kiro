// Thin ck agents, run start and run end in the workflow (spec unattended-correctness):
// TC-98b, TC-101, TC-131b, TC-148b, TC-159, TC-161, TC-162a, TC-163, TC-164a, TC-188.
// Runs the real script under the fake runtime (load_workflow.mjs) with the fake project (fake_project.mjs).
// Claude Code path: no `ck` global, thin calls are agents of the lean type `args.runnerAgent`.
// OpenCode path: the `ck` global is present (directCk) and ck runs directly.
// Run: node tests/js/thin_agents.test.mjs [name filter, e.g. tc159]
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow } from './load_workflow.mjs'
import { directCk, project, THIN_LABEL } from './fake_project.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../plugin/workflows/spec-implement.js')

const RUNNER = 'spec:runner'
const RUN_ID = 'demo--20261108T120000Z-abc123'
const baseArgs = { spec: 'demo', root: '/repo', into: 'integrate/demo', targetDir: '/repo/.ck/worktrees/demo-integration' }
const claudeArgs = (extra = {}) => ({ ...baseArgs, runnerAgent: RUNNER, runId: RUN_ID, progressRun: RUN_ID, ...extra })
const ROLES = {
  implementer: { model: 'sonnet', effort: 'medium' },
  test_writer: { model: 'opus', effort: 'high' },
  reviewer: { model: 'opus', effort: 'high', enabled: true, rounds: 1 },
  fixer: { model: 'sonnet', effort: 'medium' },
  orchestrator: { model: 'sonnet', effort: 'low' },
  resolver: { model: 'opus', effort: 'high' },
}
const graphArgs = deps => ({
  deps: Object.fromEntries(Object.entries(deps).map(([n, ds]) => [String(n), ds.map(String)])),
  remaining: Object.keys(deps).map(String),
})

const only = process.argv[2] ? process.argv[2].toLowerCase().replace(/-/g, '') : null
async function test(name, fn) {
  if (only && !name.toLowerCase().replace(/-/g, '').includes(only)) return
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}

// A run that never settles must fail this suite, not hang it.
const RUN_LIMIT_MS = 20000
async function run(agent, args, ck) {
  let timer
  const limit = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`the run did not settle within ${RUN_LIMIT_MS} ms`)), RUN_LIMIT_MS) })
  try { return await Promise.race([runWorkflow(SCRIPT, { agent, args, ck }), limit]) } finally { clearTimeout(timer) }
}
// Wraps an agent: every call is recorded in `calls` ({label, opts}) and `order` ("agent:<label>").
function recording(inner, order = []) {
  const calls = []
  const agent = (prompt, opts) => { calls.push({ label: opts.label, opts, prompt }); order.push(`agent:${opts.label}`); return inner(prompt, opts) }
  return { agent, calls, order }
}
// Wraps an agent: calls whose label matches `re` answer `answer` (a malformed thin answer) instead.
const malformed = (inner, re, answer) => (prompt, opts) => (re.test(opts.label) ? Promise.resolve(answer) : inner(prompt, opts))
const labelsOf = trace => trace.filter(e => e.ev === 'request').map(e => e.label)
const isTask = l => /^task \d+/.test(l)
// A claim command in a task prompt carries the run id.
const claimsIn = prompt => [...String(prompt).matchAll(/worktree claim[^\n"`]*/g)].map(m => m[0])

// The scenario of TC-159: a plan per landing, a landing, a relink, a park, a periodic gate that goes red and is
// re-checked, the final gate. Task 5 is stopped by the guard on every attempt, so it is parked.
const TC159_DEPS = { 1: [], 2: ['1'], 3: ['1'], 4: ['2', '3'], 5: [] }
const tc159Project = () => project({
  deps: TC159_DEPS,
  verifyCycles: [{ task: '4', message: 'Task 4 is verified by Task 9, whose tests import x (Task 5)' }],
  relinkOutcome: { fixable: true, applied: true, committed: true },
  guardStops: { 5: 'always' },
  fullRed: true,
})
const tc159Args = (extra = {}) => claudeArgs({ ...graphArgs(TC159_DEPS), fullGateEvery: 1, mergeBatch: 1, ...extra })

// --- TC-159 / TC-161 ------------------------------------------------------------------------------------------

await test('TC-159: on the Claude Code path every thin call carries the lean agent type; no task, review, fix, cross-test or resolver call does', async () => {
  const p = tc159Project()
  const rec = recording(p.agent)
  const { result } = await run(rec.agent, tc159Args({ agents: ROLES }))
  const thin = rec.calls.filter(c => THIN_LABEL.test(c.label) && !/^(report$|testfix )/.test(c.label))
  const kinds = new Set(thin.map(c => c.label.replace(/ .*$/, '').replace(/^re-check$/, 're-check')))
  for (const want of ['plan', 'land', 'relink', 'park', 'full', 're-check', 'final', 'run', 'audit']) {
    assert.ok(kinds.has(want), `a thin "${want}" call ran (saw: ${[...kinds]})`)
  }
  for (const c of thin) assert.equal(c.opts.agentType, RUNNER, `${c.label} carries agentType ${RUNNER}`)
  const heavy = rec.calls.filter(c => !THIN_LABEL.test(c.label) && c.label !== 'report' && !/^testfix /.test(c.label))
  assert.ok(heavy.some(c => isTask(c.label)) && heavy.some(c => /^review/.test(c.label)), 'task and review calls ran')
  for (const c of heavy) assert.equal(c.opts.agentType, undefined, `${c.label} is not a thin call and carries no agentType`)
  assert.ok(result.needsInput && result.needsInput.length >= 1, 'task 5 was parked')
})

await test('TC-161: every thin call carries the same schema, deep-equal', async () => {
  const p = tc159Project()
  const rec = recording(p.agent)
  await run(rec.agent, tc159Args({ agents: ROLES }))
  const thin = rec.calls.filter(c => c.opts.agentType === RUNNER)
  assert.ok(thin.length >= 8, `thin calls ran (${thin.length})`)
  const first = thin[0].opts.schema
  assert.ok(first && first.properties && first.properties.exitCode && first.properties.json, 'the shared schema has exitCode and json')
  for (const c of thin) assert.deepEqual(c.opts.schema, first, `${c.label}: schema deep-equals ${thin[0].label}'s`)
})

// --- TC-162a: the lean agent type is unavailable ----------------------------------------------------------------

await test('TC-162a: runnerAgent null runs thin calls with no agent type, and thinAgent.warning says so', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] } })
  const rec = recording(p.agent)
  const { result } = await run(rec.agent, { ...baseArgs, runnerAgent: null, runId: RUN_ID, progressRun: RUN_ID })
  assert.deepEqual(result.merged.sort(), ['1', '2'])
  assert.ok(rec.calls.some(c => /^(land|run start|audit)/.test(c.label)), 'thin calls still ran')
  for (const c of rec.calls) assert.equal(c.opts.agentType, undefined, `${c.label} has no agent type`)
  assert.ok(result.thinAgent && /lean thin agent type .*unavailable/.test(result.thinAgent.warning || ''), JSON.stringify(result.thinAgent))
})

await test('TC-162a: with the lean type available there is no thinAgent warning', async () => {
  const p = project({ deps: { 1: [] } })
  const { result } = await run(p.agent, claudeArgs())
  assert.ok(result.thinAgent, 'the result carries thinAgent')
  assert.equal(result.thinAgent.type, RUNNER)
  assert.ok(!result.thinAgent.warning, JSON.stringify(result.thinAgent))
})

// --- TC-163: malformed thin answers are failed calls -----------------------------------------------------------

const MALFORMED = {
  'missing exitCode': { json: {} },
  'json not an object': { exitCode: 0, json: 'oops' },
  'free text': 'I ran the command and it went fine.',
}
for (const [what, answer] of Object.entries(MALFORMED)) {
  await test(`TC-163: a thin answer with ${what} fails the call (plan falls back, land is not merged, the gate is an error)`, async () => {
    // plan
    const deps = { 1: [], 2: [], 3: ['1'] }
    const pp = project({ deps })
    const planned = await run(malformed(pp.agent, /^plan \d+/, answer), claudeArgs({ ...graphArgs(deps), mergeBatch: 1 }))
    assert.ok(planned.result.stats.planFailures >= 1, `a malformed plan answer is a plan failure: ${JSON.stringify(planned.result.stats)}`)
    // land
    const lp = project({ deps: { 1: [] } })
    const landed = await run(malformed(lp.agent, /^land /, answer), claudeArgs(graphArgs({ 1: [] })))
    assert.deepEqual(landed.result.merged, [], 'a malformed land answer is not counted as merged')
    assert.ok(landed.result.failed.some(f => f.task === '1'), JSON.stringify(landed.result.failed))
    // gate
    const gp = project({ deps: { 1: [] } })
    const gated = await run(malformed(gp.agent, /^final gate$/, answer), claudeArgs(graphArgs({ 1: [] })))
    assert.notEqual(gated.result.finalGate && gated.result.finalGate.green, true, 'a malformed gate answer is never green')
    const gate = (gated.result.gates || []).find(g => g.kind === 'final')
    assert.ok(gate && gate.state === 'error', `the final gate is an error: ${JSON.stringify(gated.result.gates)}`)
  })
}

// --- TC-98b / TC-101 / TC-148b: run start and run end ------------------------------------------------------------

// Position of the first agent or direct-ck event in `order` matching `re`, or -1.
const at = (order, re, from = 0) => order.findIndex((e, i) => i >= from && re.test(e))
const count = (order, re) => order.filter(e => re.test(e)).length

await test('TC-148b: the Claude Code run starts once (the claim sweep) before its first task agent, with the run id', async () => {
  const p = project({ deps: { 1: [], 2: ['1'], 3: [] } })
  const rec = recording(p.agent)
  await run(rec.agent, claudeArgs())
  assert.equal(count(rec.order, /^agent:run start$/), 1, 'one run start call')
  assert.ok(at(rec.order, /^agent:run start$/) < at(rec.order, /^agent:task /), 'run start precedes the first task agent')
  const start = rec.calls.find(c => c.label === 'run start')
  assert.match(start.prompt, /worktree start demo/)
  assert.ok(start.prompt.includes(`--run ${RUN_ID}`), start.prompt)
  assert.equal(start.opts.agentType, RUNNER)
})

await test('TC-148b: on the OpenCode path the run calls worktree start once, directly, before its first task agent', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] } })
  const d = directCk(p)
  const order = []
  const rec = recording(p.agent, order)
  const ck = async argv => { const rest = argv[0] === '-C' ? argv.slice(2) : argv; order.push(`ck:${rest[0]} ${rest[1] ?? ''}`.trim()); return d.fn(argv) }
  await run(rec.agent, { ...baseArgs, runId: RUN_ID, progressRun: RUN_ID }, ck)
  assert.equal(count(order, /^ck:worktree start$/), 1)
  assert.ok(at(order, /^ck:worktree start$/) < at(order, /^agent:task /), 'worktree start precedes the first task agent')
  const argv = d.calls.find(a => a.includes('start'))
  const rest = argv[0] === '-C' ? argv.slice(2) : argv
  assert.deepEqual(rest.slice(0, 5), ['worktree', 'start', 'demo', '--run', RUN_ID])
  assert.ok(rest.includes('--json'))
})

await test('TC-98b: a clean run ends with one audit after the last step, and the result carries it', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] } })
  const { result, trace } = await run(p.agent, claudeArgs())
  const reqs = labelsOf(trace)
  assert.equal(reqs.filter(l => l === 'audit').length, 1)
  assert.equal(reqs[reqs.length - 1], 'audit', `the audit is the last call: ${reqs.slice(-4)}`)
  const ends = trace.filter(e => e.ev === 'end')
  assert.equal(ends[ends.length - 1].label, 'audit', 'nothing ends after the audit')
  assert.ok(result.audit, 'the result carries the audit')
  assert.equal(result.audit.clean, true)
  assert.equal(result.audit.complete, true)
  assert.deepEqual(result.audit.drift, [])
})

await test('TC-98b: the OpenCode run audits directly with worktree audit, once, last', async () => {
  const p = project({ deps: { 1: [] } })
  const d = directCk(p)
  const { result } = await run(p.agent, { ...baseArgs, runId: RUN_ID, progressRun: RUN_ID }, d.fn)
  const audits = d.calls.filter(a => a.includes('audit'))
  assert.equal(audits.length, 1)
  assert.equal(d.calls[d.calls.length - 1], audits[0], 'the audit is the last direct ck call')
  assert.ok(result.audit && result.audit.clean === true)
})

const assertAuditedOnce = (rec, trace) => {
  const reqs = trace.filter(e => e.ev === 'request')
  assert.equal(reqs.filter(e => e.label === 'audit').length, 1, 'the audit ran exactly once')
  const last = trace.map((e, i) => [e, i]).filter(([e]) => e.ev === 'end' && e.label !== 'audit').map(([, i]) => i).pop()
  const audit = trace.findIndex(e => e.ev === 'request' && e.label === 'audit')
  assert.ok(audit > last, 'the audit was requested after every other agent ended')
}

await test('TC-101: a green run, a red run (stopReason) and a run with a thrown step error each audit once, after the last step', async () => {
  // green
  const g = project({ deps: { 1: [], 2: ['1'] } })
  const green = await run(g.agent, claudeArgs())
  assertAuditedOnce(null, green.trace)
  assert.equal(green.result.halted, false)
  assert.ok(green.result.audit, 'green result carries audit')

  // red: task 1 never passes and the run halts
  const r = project({ deps: { 1: [], 2: ['1'] }, failUntil: { 1: 99 } })
  const red = await run(r.agent, claudeArgs({ onFailure: 'halt', maxRetries: 0 }))
  assert.equal(red.result.halted, true)
  assertAuditedOnce(null, red.trace)
  assert.ok(red.result.audit, 'red result carries audit')

  // thrown: the final gate agent throws
  const t = project({ deps: { 1: [] }, gateAnswers: ['throw'] })
  const thrown = await run(t.agent, claudeArgs())
  assertAuditedOnce(null, thrown.trace)
  assert.ok(thrown.result.audit, 'thrown-step result carries audit')
})

await test('TC-101: a run whose step throws out of the script still audits once, last', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] } })
  const boom = (prompt, opts) => { if (opts.label === 'task 2') throw new TypeError('step blew up'); return p.agent(prompt, opts) }
  let out = null
  try { out = await run(boom, claudeArgs()) } catch (e) { out = { thrown: e } }
  const trace = out.trace
  if (trace) {
    assertAuditedOnce(null, trace)
    assert.ok(out.result.audit, 'the result carries audit')
  } else {
    // The script let the error out: the audit must still have been requested; the fake project saw it.
    assert.ok(p.state.events.includes('audit'), 'the audit ran before the error left the script')
  }
})

await test('TC-101: an audit that throws is recorded incomplete, never clean', async () => {
  const p = project({ deps: { 1: [] }, audit: 'throw' })
  const { result } = await run(p.agent, claudeArgs())
  assert.ok(result.audit, 'the result records the failed audit')
  assert.equal(result.audit.complete, false)
  assert.notEqual(result.audit.clean, true)
})

await test('TC-101: an audit that reports drift is carried in the result, not clean', async () => {
  const audit = { schema: 'ck-audit/1', spec: 'demo', into: 'integrate/demo', head: 'abc1234', complete: true, clean: false, drift: [{ file: '.claude/settings.json', change: 'changed' }], hooks: { installed: true, active: true, intact: true, chainedTo: null, problems: [] }, fixtures: { drift: [], newLocks: [], lockTampered: false, handcrafted: [] }, problems: [] }
  const p = project({ deps: { 1: [] }, audit })
  const { result } = await run(p.agent, claudeArgs())
  assert.equal(result.audit.clean, false)
  assert.deepEqual(result.audit.drift, audit.drift)
})

// --- TC-131b / 14.1 / 14.6: agent opts per harness ---------------------------------------------------------------

await test('TC-131b: on the Claude Code path agent opts carry only Claude aliases, whatever opencode.roles says', async () => {
  const p = project({ deps: { 1: [], 2: ['1'] } })
  const rec = recording(p.agent)
  const opencode = { roles: { implementer: 'opencode-go/muse-spark-1.3-contributor#medium', reviewer: 'opencode-go/deepseek-v4.1-flash#medium' } }
  await run(rec.agent, claudeArgs({ agents: ROLES, opencode }))
  const withModel = rec.calls.filter(c => c.opts.model !== undefined)
  assert.ok(withModel.length >= 3, 'agents with a model ran')
  for (const c of withModel) {
    assert.match(c.opts.model, /^(sonnet|opus|haiku|fable)$|^claude-/, `${c.label}: model ${c.opts.model} is a Claude alias`)
    assert.ok(!String(c.opts.model).includes('/'), `${c.label}: no provider/model reference`)
  }
  for (const c of rec.calls) assert.ok(!('role' in c.opts), `${c.label}: no OpenCode role tag on the Claude Code path`)
  const impl = rec.calls.find(c => c.label === 'task 1')
  assert.equal(impl.opts.model, 'sonnet')
})

const HAIKU_LADDER = { implementer: { model: 'haiku', effort: 'medium', escalate: ['sonnet', 'opus'] }, reviewer: { model: 'sonnet', effort: 'medium', enabled: true, rounds: 1 }, fixer: { model: 'opus', effort: 'medium' }, test_writer: { model: 'haiku', effort: 'medium' }, orchestrator: { model: 'sonnet', effort: 'low' }, resolver: { model: 'opus', effort: 'high' } }

await test('14.1: on the OpenCode path every base agent call carries its role; escalated ladder steps do not', async () => {
  const p = project({ deps: { 1: [], 2: [] }, failUntil: { 1: 3 } })
  const d = directCk(p)
  const rec = recording(p.agent)
  await run(rec.agent, { ...baseArgs, ...graphArgs({ 1: [], 2: [] }), agents: HAIKU_LADDER, maxRetries: 1, runId: RUN_ID, progressRun: RUN_ID, tracks: { 2: 'test' } }, d.fn)
  const calls = rec.calls
  const task1 = calls.filter(c => /^task 1/.test(c.label))
  assert.ok(task1.length >= 4, `task 1 climbed the ladder: ${task1.map(c => c.label)}`)
  const base = task1.filter(c => c.opts.model === 'haiku')
  const escalated = task1.filter(c => c.opts.model !== 'haiku')
  assert.ok(base.length >= 2 && escalated.length >= 2, 'both base and escalated steps ran')
  for (const c of base) assert.equal(c.opts.role, 'implementer', `${c.label} is a base implementer call`)
  for (const c of escalated) assert.ok(!('role' in c.opts) || c.opts.role == null, `${c.label} is escalated and carries no role`)
  assert.equal(calls.find(c => c.label === 'task 2').opts.role, 'test_writer')
  const review = calls.find(c => /^review/.test(c.label))
  assert.ok(review, 'a review ran')
  assert.equal(review.opts.role, 'reviewer')
})

// --- TC-164a: OpenCode path has no thin agents ------------------------------------------------------------------

await test('TC-164a: on the OpenCode path the trace has no thin agent labels and no call carries an agent type or the thin schema', async () => {
  const deps = { 1: [], 2: ['1'], 3: ['1'] }
  const p = project({ deps, verifyCycles: [{ task: '3', message: 'Task 3 is verified by Task 9, whose tests import x (Task 2)' }], relinkOutcome: { fixable: true, applied: true, committed: true } })
  const d = directCk(p)
  const rec = recording(p.agent)
  const { result, trace } = await run(rec.agent, { ...baseArgs, agents: ROLES, fullGateEvery: 1, mergeBatch: 1, runId: RUN_ID, progressRun: RUN_ID }, d.fn)
  assert.deepEqual(result.merged.sort(), ['1', '2', '3'])
  const labels = labelsOf(trace)
  assert.ok(!labels.some(l => /^(plan \d+|land |relink \d+|full gate|re-check|final gate|run start|audit|gate$)/.test(l)), `thin labels in the trace: ${labels}`)
  for (const c of rec.calls) {
    assert.equal(c.opts.agentType, undefined, `${c.label}: no agent type`)
    assert.ok(!(c.opts.schema && c.opts.schema.properties && c.opts.schema.properties.exitCode), `${c.label}: not a thin ck call`)
  }
  assert.ok(d.calls.some(a => a.includes('gate')), 'gates ran as direct ck calls')
  assert.equal(result.thinAgent && result.thinAgent.warning ? 1 : 0, 0, 'no lean-type warning when ck runs directly')
})

// --- TC-188: protected park and resume -------------------------------------------------------------------------

await test('TC-188: pass 1 parks Task 3 on PROTECTED with the approve command and the resume; pass 2 with the same runId completes with one final gate', async () => {
  const deps = { 3: [], 4: ['3'] }
  // Pass 1
  const p1 = project({ deps, protectedTasks: ['3'] })
  const rec1 = recording(p1.agent)
  const one = await run(rec1.agent, claudeArgs(graphArgs(deps)))
  assert.ok(!one.result.merged.includes('3'), 'Task 3 is not merged')
  assert.ok(!p1.state.events.includes('task 4'), 'Task 4 does not start')
  assert.ok(p1.state.parks.includes('3'), `Task 3 was parked: ${p1.state.events}`)
  const park = rec1.calls.find(c => c.label === 'park 3')
  assert.equal(park.opts.agentType, RUNNER, 'the park call is thin')
  assert.ok(park.prompt.includes('ck worktree approve demo 3'), park.prompt)
  assert.ok(park.prompt.includes('/spec:implement') && park.prompt.includes(RUN_ID), 'the note names the resume with the same runId')
  const entry = (one.result.needsInput || []).find(e => String(e.task) === '3')
  assert.ok(entry, JSON.stringify(one.result.needsInput))
  assert.equal(entry.kind, 'protected')
  assert.ok(!p1.state.events.some(e => /^(resolve|fix) /.test(e)), 'no agent lands it')

  // Pass 2: same runId, Task 3 now Done (approved and landed by me)
  const p2 = project({ deps, done: ['3'] })
  const rec2 = recording(p2.agent)
  const two = await run(rec2.agent, claudeArgs({ ...graphArgs({ 4: ['3'] }), deps: { 3: [], 4: ['3'] }, remaining: ['4'] }))
  const start = rec2.calls.find(c => c.label === 'run start')
  assert.ok(start && start.prompt.includes(`--run ${RUN_ID}`), 'pass 2 starts with the same run id (pass 1 claims are its own)')
  assert.ok(p2.state.events.includes('task 4'), 'Task 4 starts')
  assert.ok(!p2.state.events.includes('task 3'), 'Task 3 is not run again')
  assert.ok(claimsIn(p2.state.prompts['task 4']).every(c => c.includes(RUN_ID)) && claimsIn(p2.state.prompts['task 4']).length >= 1, 'claims carry the run id')
  assert.deepEqual(two.result.merged, ['4'])
  const reqs = labelsOf(two.trace)
  assert.equal(reqs.filter(l => l === 'final gate').length, 1, 'one final gate')
  const land4End = two.trace.findIndex(e => e.ev === 'end' && e.label === 'land 4')
  const finalReq = two.trace.findIndex(e => e.ev === 'request' && e.label === 'final gate')
  assert.ok(land4End >= 0 && finalReq > land4End, 'the final gate is requested after Task 4 lands')
  const finalCall = rec2.calls.find(c => c.label === 'final gate')
  assert.match(finalCall.prompt, /--attribute/)
  assert.equal(two.result.outcome, 'complete')
})
