// Control-flow tests for spec_create.js with stub agents. Run: node tests/js/spec_create.test.mjs
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow } from './load_workflow.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../plugin/workflows/spec-create.js')
const args = mode => ({ mode, feature: 'f', projectRoot: '/p', specDir: '/p/specs/f', date: '2026-10-04' })

function stub(overrides = {}) {
  const calls = []
  const prompts = {}
  const agent = async (prompt, opts) => {
    calls.push(opts.label)
    prompts[opts.label] = prompt
    if (opts.label in overrides) return overrides[opts.label]
    const l = opts.label
    if (l.startsWith('write') || l.startsWith('link')) return { path: 'x', summary: 's', assumptions: [], concerns: [] }
    if (l === 'review: fidelity') return { findings: [
      { id: 'R1', file: 'requirements.md', location: '1', severity: 'major', problem: 'p', evidence: 'e', fix: 'f', needsUserDecision: false },
      { id: 'R2', file: 'requirements.md', location: '2', severity: 'minor', problem: 'p2', evidence: 'e', fix: 'f', needsUserDecision: true, question: 'q?' },
      { id: 'R3', file: 'requirements.md', location: '3', severity: 'minor', problem: 'p3', evidence: 'e', fix: 'f', needsUserDecision: false }] }
    if (l === 'refute: fidelity') return { verdicts: [{ id: 'R1', refuted: false, reason: 'real' }, { id: 'R2', refuted: false, reason: 'real' }, { id: 'R3', refuted: true, reason: 'style' }] }
    if (l.startsWith('review:')) return { findings: [] }
    if (l.startsWith('revise')) return { fixed: ['R1'], notFixed: [], escalated: [], result: 'Ready after decisions', schedule: 'Ready at start: Task 1. Critical path: Task 1' }
    if (l === 'snapshot spec before revision') return { copied: ['requirements.md', 'design.md', 'test-plan.md', 'tasks.md'] }
    if (l.startsWith('consistency check')) return { contradictions: [] }
    if (l === 'remove pre-revision snapshot') return { removed: true }
    throw new Error('unexpected ' + l)
  }
  return { agent, calls, prompts }
}

async function test(name, fn) {
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}

await test('create: authoring stages, 4 lenses, refuted/decision findings routed', async () => {
  const s = stub()
  const { result } = await runWorkflow(SCRIPT, { agent: s.agent, args: args('create') })
  assert.deepEqual(s.calls.slice(0, 6), ['write requirements.md', 'write design.md', 'write test-plan.md', 'write implementation track', 'write test track', 'link tracks into tasks.md'])
  for (const lens of ['fidelity', 'grounding', 'implementability', 'verification']) assert.ok(s.calls.includes(`review: ${lens}`))
  assert.deepEqual(result.review.fixed, ['R1'])
  assert.deepEqual(result.review.openQuestions.map(q => q.id), ['R2'])
  assert.equal(result.review.refutedCount, 1)
  assert.equal(result.files.length, 5)
})

await test('review mode skips authoring', async () => {
  const s = stub()
  const { result } = await runWorkflow(SCRIPT, { agent: s.agent, args: args('review') })
  assert.ok(!s.calls.some(c => c.startsWith('write')))
  assert.equal(result.authored, null)
})

await test('a dead test planner stops before task planning', async () => {
  const s = stub({ 'write test-plan.md': null })
  const { result } = await runWorkflow(SCRIPT, { agent: s.agent, args: args('create') })
  assert.match(result.error, /test plan/)
  assert.ok(!s.calls.some(c => c.startsWith('write implementation')))
})

await test('a dead reviewer is reported as a lost lens; a dead refuter leaves findings unapplied', async () => {
  const s = stub({
    'review: grounding': null,
    'review: implementability': { findings: [{ id: 'T1', file: 'tasks.md', location: 'Task 2', severity: 'critical', problem: 'p', evidence: 'e', fix: 'f', needsUserDecision: false }] },
    'refute: implementability': null,
  })
  const { result } = await runWorkflow(SCRIPT, { agent: s.agent, args: args('create') })
  assert.equal(result.review.lensesLost, 1)
  assert.deepEqual(result.review.unverified.map(f => f.id), ['T1'])
})

await test('risk tags: both tracks and the link step are told to tag safety tasks; the implementability lens checks them', async () => {
  const s = stub()
  await runWorkflow(SCRIPT, { agent: s.agent, args: args('create') })
  for (const l of ['write implementation track', 'write test track', 'link tracks into tasks.md']) {
    assert.match(s.prompts[l], /\*\*Risk:\*\* safety/, l)
    assert.match(s.prompts[l], /sending messages, money or payments, auth or credentials, deletion/, l)
  }
  assert.match(s.prompts['link tracks into tasks.md'], /risk path/)
  assert.match(s.prompts['review: implementability'], /untagged risky task/i)
})

function recording() {
  const s = stub()
  const prompts = {}
  const agent = async (prompt, opts) => { prompts[opts.label] = prompt; return s.agent(prompt, opts) }
  return { agent, prompts }
}

await test('test plan requires a Properties section with surface forms, negatives, complements, metamorphic relations', async () => {
  const r = recording()
  await runWorkflow(SCRIPT, { agent: r.agent, args: args('create') })
  const p = r.prompts['write test-plan.md']
  assert.match(p, /## Properties/)
  assert.match(p, /REQUIRED/)
  assert.match(p, /P-n \| /)
  for (const word of ['"any"', '"every"', '"only"', '"never"', 'SHALL NOT']) assert.ok(p.includes(word), word)
  assert.match(p, /surface form/i)
  assert.match(p, /x to y/)
  assert.match(p, /exist in the data source but are not permitted/i)
  assert.match(p, /complement/i)
  assert.match(p, /metamorphic/i)
  assert.match(p, /Hypothesis/)
  assert.match(p, /fast-check/)
  assert.match(p, /derandomize/)
  assert.match(p, /example database/i)
  assert.match(p, /decision seam/i)
  assert.match(p, /table-driven/i)
  assert.doesNotMatch(p, /design\.md first/) // still written without the design
})

await test('task tracks carry properties: Properties field, PROPERTIES map, red-team task, link wiring', async () => {
  const r = recording()
  await runWorkflow(SCRIPT, { agent: r.agent, args: args('create') })
  const t = r.prompts['write test track']
  assert.match(t, /\*\*Properties:\*\* P-1, P-2/)
  assert.match(t, /PROPERTIES = \{"P-3": "test_name"/)
  assert.match(t, /one test task per property group/i)
  assert.match(t, /red-team/i)
  assert.match(t, /SHALL NOT/)
  assert.match(t, /from the requirements only/i)
  const link = r.prompts['link tracks into tasks.md']
  assert.match(link, /\*\*Properties:\*\*/)
  assert.match(link, /every P-n/i)
  assert.match(link, /decision seam/i)
  assert.match(r.prompts['write design.md'], /decision seam/i)
})

await test('verification lens checks every universal SHALL / SHALL NOT has a property with surface forms and negative space', async () => {
  const r = recording()
  await runWorkflow(SCRIPT, { agent: r.agent, args: args('create') })
  const v = r.prompts['review: verification']
  assert.match(v, /universally quantified/i)
  assert.match(v, /SHALL NOT/)
  assert.match(v, /surface forms/i)
  assert.match(v, /negative space/i)
  assert.match(v, /PROPERTIES/)
})

// ---------------------------------------------------------------------------
// Review consistency: fixes must not contradict the criteria they touch.
// ---------------------------------------------------------------------------

const onlyR1 = { 'review: fidelity': { findings: [
  { id: 'R1', file: 'design.md', location: '4', severity: 'critical', problem: 'p', evidence: 'e', fix: 'refuse after Approve', needsUserDecision: false }] },
'refute: fidelity': { verdicts: [{ id: 'R1', refuted: false, reason: 'real' }] } }

await test('revise prompt: governing-criterion rule and escalation instead of a silent behaviour change', async () => {
  const s = stub()
  await runWorkflow(SCRIPT, { agent: s.agent, args: args('create') })
  const p = s.prompts['revise spec + write review.md']
  assert.match(p, /governs that behaviour/i)
  assert.match(p, /EARS id/i)
  assert.match(p, /P-n/)
  assert.match(p, /quote/i)
  assert.match(p, /do NOT apply it/)
  assert.match(p, /escalated/)
  assert.match(p, /raised while applying fixes/)
  assert.match(p, /Ready after decisions/)
})

await test('escalated revise items make the result "Ready after decisions" and reach openQuestions', async () => {
  const s = stub({ ...onlyR1,
    'revise spec + write review.md': { fixed: [], notFixed: [], result: 'Ready', schedule: 's',
      escalated: [{ id: 'R1', question: 'Refuse or re-evaluate a changed body after Approve?', options: ['refuse', 're-evaluate'], conflictsWith: '9.16' }] } })
  const { result } = await runWorkflow(SCRIPT, { agent: s.agent, args: args('create') })
  assert.equal(result.review.result, 'Ready after decisions')
  const q = result.review.openQuestions.find(x => x.id === 'R1')
  assert.ok(q, 'escalated item listed')
  assert.equal(q.source, 'revise')
  assert.equal(q.conflictsWith, '9.16')
  assert.deepEqual(q.options, ['refuse', 're-evaluate'])
  assert.equal(q.file, 'design.md') // carried from the finding
  assert.deepEqual(result.review.escalated.map(e => e.id), ['R1'])
})

await test('without decisions a "Ready" revise result stays Ready', async () => {
  const s = stub({ ...onlyR1, 'revise spec + write review.md': { fixed: ['R1'], notFixed: [], escalated: [], result: 'Ready', schedule: 's' } })
  const { result } = await runWorkflow(SCRIPT, { agent: s.agent, args: args('create') })
  assert.equal(result.review.result, 'Ready')
  assert.deepEqual(result.review.openQuestions, [])
})

await test('snapshot before revise; consistency check after it; snapshot removed at the end', async () => {
  const s = stub()
  const { result } = await runWorkflow(SCRIPT, { agent: s.agent, args: args('create') })
  const at = l => s.calls.indexOf(l)
  assert.ok(at('snapshot spec before revision') >= 0)
  assert.ok(at('snapshot spec before revision') < at('revise spec + write review.md'))
  assert.ok(at('revise spec + write review.md') < at('consistency check: pass 1'))
  assert.ok(at('consistency check: pass 1') < at('remove pre-revision snapshot'))
  assert.ok(!s.calls.includes('consistency check: pass 2'), 'nothing fixed, no second pass')
  assert.match(s.prompts['snapshot spec before revision'], /\/p\/specs\/f\/\.pre-revision/)
  assert.match(s.prompts['remove pre-revision snapshot'], /\/p\/specs\/f\/\.pre-revision/)
  assert.deepEqual(result.consistency, { passes: 1, fixed: [], decisions: [], lost: false })
})

await test('consistency prompt: diff against the snapshot, check criteria and properties, property conflicts', async () => {
  const s = stub()
  await runWorkflow(SCRIPT, { agent: s.agent, args: args('create') })
  const c = s.prompts['consistency check: pass 1']
  assert.match(c, /\.pre-revision/)
  assert.match(c, /diff/i)
  assert.match(c, /Properties/)
  assert.match(c, /refusal reasons/i)
  assert.match(c, /every changed criterion/i)
  assert.match(c, /file:line/)
  assert.match(c, /found by the consistency check/)
  assert.match(c, /overlapping input domains/i)
  assert.match(c, /same outcome/i)
})

await test('consistency fixes trigger a second pass, never a third; decisions are surfaced', async () => {
  const fixedOne = n => ({ contradictions: [{ id: `C${n}.1`, kind: 'drift', description: 'd', changedSide: 'design.md:10 x', otherSide: 'requirements.md:5 y', resolution: 'fixed', fix: 'reworded' }] })
  const s = stub({
    'consistency check: pass 1': { contradictions: [
      ...fixedOne(1).contradictions,
      { id: 'C1.2', kind: 'behaviour', description: 'refuse vs re-evaluate', changedSide: 'design.md:1171 refuse', otherSide: 'requirements.md:88 9.16 re-evaluate', resolution: 'decision', question: 'Refuse or re-evaluate?', options: ['refuse', 're-evaluate'] }] },
    'consistency check: pass 2': fixedOne(2),
  })
  const { result, logs } = await runWorkflow(SCRIPT, { agent: s.agent, args: args('create') })
  assert.ok(s.calls.includes('consistency check: pass 2'))
  assert.ok(!s.calls.includes('consistency check: pass 3'))
  assert.equal(result.consistency.passes, 2)
  assert.deepEqual(result.consistency.fixed.map(f => f.id), ['C1.1', 'C2.1'])
  assert.deepEqual(result.consistency.decisions.map(f => f.id), ['C1.2'])
  const q = result.review.openQuestions.find(x => x.id === 'C1.2')
  assert.equal(q.source, 'consistency')
  assert.match(q.conflictsWith, /9\.16/)
  assert.equal(result.review.result, 'Ready after decisions')
  assert.match(s.prompts['consistency check: pass 2'], /C1\.1/) // told what pass 1 changed
  assert.ok(logs.some(l => /consistency/i.test(l)))
})

await test('a decision-only consistency pass makes a Ready spec "Ready after decisions"', async () => {
  const s = stub({ ...onlyR1,
    'revise spec + write review.md': { fixed: ['R1'], notFixed: [], escalated: [], result: 'Ready', schedule: 's' },
    'consistency check: pass 1': { contradictions: [{ id: 'C1.1', kind: 'property-conflict', description: 'P-4 vs TC-105', changedSide: 'test-plan.md:300 TC-105', otherSide: 'test-plan.md:410 P-4', resolution: 'decision', question: 'q?' }] } })
  const { result } = await runWorkflow(SCRIPT, { agent: s.agent, args: args('create') })
  assert.equal(result.consistency.passes, 1)
  assert.equal(result.review.result, 'Ready after decisions')
})

await test('review mode runs the same consistency check after its revise step', async () => {
  const s = stub()
  const { result } = await runWorkflow(SCRIPT, { agent: s.agent, args: args('review') })
  assert.ok(s.calls.indexOf('snapshot spec before revision') < s.calls.indexOf('revise spec + write review.md'))
  assert.ok(s.calls.indexOf('revise spec + write review.md') < s.calls.indexOf('consistency check: pass 1'))
  assert.equal(result.consistency.passes, 1)
})

await test('nothing to fix: no snapshot and no consistency check', async () => {
  const s = stub({ 'review: fidelity': { findings: [] } })
  const { result } = await runWorkflow(SCRIPT, { agent: s.agent, args: args('create') })
  assert.ok(!s.calls.includes('snapshot spec before revision'))
  assert.ok(!s.calls.some(c => c.startsWith('consistency check')))
  assert.equal(result.consistency.passes, 0)
})

await test('a failed snapshot still runs the check, over the whole spec', async () => {
  const s = stub({ 'snapshot spec before revision': null })
  const { result } = await runWorkflow(SCRIPT, { agent: s.agent, args: args('create') })
  assert.ok(s.calls.includes('consistency check: pass 1'))
  assert.match(s.prompts['consistency check: pass 1'], /no snapshot/i)
  assert.equal(result.consistency.passes, 1)
})

await test('a dead consistency agent is reported, not fatal', async () => {
  const s = stub({ 'consistency check: pass 1': null })
  const { result } = await runWorkflow(SCRIPT, { agent: s.agent, args: args('create') })
  assert.equal(result.consistency.lost, true)
  assert.equal(result.consistency.passes, 0)
  assert.ok(s.calls.includes('remove pre-revision snapshot'))
})

await test('verification lens and test plan: property conflicts and governing criterion ids', async () => {
  const s = stub()
  await runWorkflow(SCRIPT, { agent: s.agent, args: args('create') })
  const v = s.prompts['review: verification']
  assert.match(v, /overlapping input domains/i)
  assert.match(v, /same outcome/i)
  assert.match(v, /both ids and the input/i)
  assert.match(s.prompts['write test-plan.md'], /governing criterion id/i)
})
