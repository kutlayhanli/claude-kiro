// Control-flow tests for spec_create.js with stub agents. Run: node tests/js/spec_create.test.mjs
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow } from './load_workflow.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../src/claude_kiro/resources/templates/workflows/spec_create.js')
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
    if (l.startsWith('revise')) return { fixed: ['R1'], notFixed: [], result: 'Ready after decisions', schedule: 'Ready at start: Task 1. Critical path: Task 1' }
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
