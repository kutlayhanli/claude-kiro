// TC-87 (Workflow half): /spec:review's revise agent on a started spec must not reword existing requirements.md or
// test-plan.md text; it returns the change under `escalated`. Real plugin/workflows/spec-create.js in review mode
// under the stub runtime. Run: node tests/js/spec_review_frozen.test.mjs
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { runWorkflow } from './load_workflow.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const SCRIPT = path.join(here, '../../plugin/workflows/spec-create.js')
const args = { mode: 'review', feature: 'f', projectRoot: '/p', specDir: '/p/specs/f', date: '2026-10-30' }

const PROPOSED = 'The system SHALL reject an empty name with exit code 2.'
const finding = (id, file, fix) => ({ id, file, location: '1.1', severity: 'major', problem: 'p', evidence: 'e', fix, needsUserDecision: false })

function stub() {
  const calls = []
  const prompts = {}
  const agent = async (prompt, opts) => {
    const l = opts.label
    calls.push(l)
    prompts[l] = prompt
    if (l === 'review: fidelity') return { findings: [
      finding('R1', 'requirements.md', `Reword requirement 1.1 to: ${PROPOSED}`),
      finding('R2', 'requirements.md', 'Add requirement 1.9 covering the new flag') ] }
    if (l.startsWith('review:')) return { findings: [] }
    if (l === 'refute: fidelity') return { verdicts: [{ id: 'R1', refuted: false, reason: 'real' }, { id: 'R2', refuted: false, reason: 'real' }] }
    if (l.startsWith('revise')) return {
      fixed: ['R2'], notFixed: [], result: 'Ready', schedule: 's',
      escalated: [{ id: 'R1', question: 'Reword 1.1 although implementation has started?', options: [PROPOSED, 'leave the spec as is'] }] }
    if (l === 'snapshot spec before revision') return { copied: ['requirements.md'] }
    if (l.startsWith('consistency check')) return { contradictions: [] }
    if (l === 'remove pre-revision snapshot') return { removed: true }
    throw new Error('unexpected ' + l)
  }
  return { agent, calls, prompts }
}

async function test(name, fn) {
  try { await fn(); console.log('ok  ', name) } catch (e) { console.log('FAIL', name); console.log(e); process.exitCode = 1 }
}

await test('TC-87: the revise prompt carries the frozen-spec rule', async () => {
  const s = stub()
  await runWorkflow(SCRIPT, { agent: s.agent, args })
  const p = s.prompts['revise spec + write review.md']
  assert.match(p, /Not Started/, 'the rule is conditional on a started spec (a task not Not Started)')
  assert.match(p, /requirements\.md/)
  assert.match(p, /test-plan\.md/)
  assert.match(p, /existing (text|requirement)/i)
  assert.match(p, /do NOT apply/i)
  assert.match(p, /escalated/)
  assert.match(p, /leave the spec as is/i)
  assert.match(p, /add(ing)?[- ]only|only adds|adding/i, 'add-only fixes stay allowed')
})

await test('TC-87: a rewording comes back under escalated, reaches openQuestions and is never applied', async () => {
  const s = stub()
  const { result } = await runWorkflow(SCRIPT, { agent: s.agent, args })
  const q = result.review.openQuestions.find(x => x.id === 'R1')
  assert.ok(q, 'R1 is an open question')
  assert.equal(q.source, 'revise')
  assert.ok(q.options.includes(PROPOSED), 'the proposed text is an option')
  assert.ok(!result.review.fixed.includes('R1'))
  assert.ok(!(result.review.notFixed || []).some(x => (x.id ?? x) === 'R1'))
  assert.deepEqual(result.review.escalated.map(e => e.id), ['R1'])
  assert.equal(result.review.result, 'Ready after decisions')
})

await test('TC-87: the add-only fix is applied', async () => {
  const s = stub()
  const { result } = await runWorkflow(SCRIPT, { agent: s.agent, args })
  assert.deepEqual(result.review.fixed, ['R2'])
  assert.ok(!result.review.openQuestions.some(x => x.id === 'R2'))
})
